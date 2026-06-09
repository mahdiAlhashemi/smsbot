"""Email OTP product: buy an email address that receives a verification code.

Money model — CHARGE ON RECEIVE (like SMS activations, NOT upfront like eSIM):
buying HOLDS the commission price; the customer is charged only when the OTP
arrives (status SUCCESS / a `value`). No code (cancel/expire) → hold released,
$0. Uses the HeroSMS **v1 REST** client (``herosms/v1.py``).

Lifecycle (v1): email_purchase → {id, email, status:"WAIT", value:null}; the OTP
appears via email_status when status becomes "SUCCESS" with a `value`. DELETE
cancels and frees the mailbox.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from decimal import Decimal, InvalidOperation

from config import settings
from db import repo
from db.models import Order
from herosms.v1 import HeroSMSV1Client, HeroSMSV1Error
from services import pricing
from services.orders import (
    ANOTHER_ERROR, ANOTHER_INSUFFICIENT, ANOTHER_OK,
    InsufficientFunds, PurchaseError,
)

log = logging.getLogger(__name__)

# Popular websites people register on (the `site` param is a free-form website;
# this is the curated browse list — domains are fetched live per site).
EMAIL_SITES = [
    ("instagram.com", "Instagram"),
    ("facebook.com", "Facebook"),
    ("discord.com", "Discord"),
    ("x.com", "X / Twitter"),
    ("tiktok.com", "TikTok"),
    ("google.com", "Google"),
    ("microsoft.com", "Microsoft"),
    ("amazon.com", "Amazon"),
    ("netflix.com", "Netflix"),
    ("paypal.com", "PayPal"),
    ("steampowered.com", "Steam"),
    ("reddit.com", "Reddit"),
    ("linkedin.com", "LinkedIn"),
    ("snapchat.com", "Snapchat"),
    ("twitch.tv", "Twitch"),
    ("ebay.com", "eBay"),
]
_SITE_NAMES = {s: n for s, n in EMAIL_SITES}


def _dec(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def site_name(site: str) -> str:
    return _SITE_NAMES.get(site, site)


# ─── catalog cache (live domains per site) ───────────────────────────────────
class _TTL:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self._value = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    def fresh(self) -> bool:
        return self._value is not None and (time.monotonic() - self._at) < self.ttl

    def set(self, value):
        self._value = value
        self._at = time.monotonic()

    @property
    def value(self):
        return self._value


class EmailCatalog:
    """TTL cache over email_domains(site) — [{name, cost: Decimal, count: int}]."""

    def __init__(self, client: HeroSMSV1Client):
        self._client = client
        self._domains: dict[str, _TTL] = {}
        self._lock = asyncio.Lock()

    def sites(self) -> list[dict]:
        return [{"site": s, "name": n} for s, n in EMAIL_SITES]

    async def domains_for(self, site: str) -> list[dict]:
        async with self._lock:
            ttl = self._domains.get(site)
            if ttl is None:
                ttl = self._domains[site] = _TTL(300)
        async with ttl._lock:
            if not ttl.fresh():
                ttl.set(await self._fetch_domains(site))
            return ttl.value

    async def _fetch_domains(self, site: str) -> list[dict]:
        try:
            resp = await self._client.email_domains(site)
        except Exception:  # noqa: BLE001
            return []
        rows = resp.get("data") if isinstance(resp, dict) else resp
        out: list[dict] = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            name = r.get("name") or r.get("domain")
            cost = _dec(r.get("cost") or r.get("price"))
            count = int(r.get("count") or 0)
            if name and cost > 0:
                out.append({"name": str(name), "cost": cost, "count": count})
        # Cheapest with stock first.
        out.sort(key=lambda d: (d["count"] == 0, d["cost"]))
        return out

    async def domain(self, site: str, name: str) -> dict | None:
        for d in await self.domains_for(site):
            if d["name"] == name:
                return d
        return None


# ─── purchase (HOLD only — charge on receive) ────────────────────────────────
async def email_purchase(
    user_id: int, site: str, domain: str, cost: Decimal, client: HeroSMSV1Client
) -> Order:
    """Hold the price and buy an email activation. NO charge here — the customer
    is billed when the OTP arrives (poller → orders.deliver_code). Releases the
    hold if the purchase can't be placed."""
    price = await pricing.email_sell_price(cost)
    if not await repo.try_hold(user_id, price):
        raise InsufficientFunds()
    try:
        data = (await client.email_purchase(site, domain) or {}).get("data") or {}
    except HeroSMSV1Error as exc:
        await repo.release_hold(user_id, price)
        low = f"{exc.code} {exc.message or ''}".lower()
        friendly = PurchaseError()
        if "balance" in low or "insufficient" in low:
            friendly.user_message = (
                "Email OTP is <b>temporarily unavailable</b> (provider stock).\n"
                "<i>✅ You were not charged — please try again later.</i>"
            )
        log.warning("email_purchase failed for user %s: %s %s", user_id, exc.code, exc.message)
        raise friendly from exc
    except Exception as exc:  # noqa: BLE001
        await repo.release_hold(user_id, price)
        log.exception("email_purchase crashed for user %s", user_id)
        raise PurchaseError() from exc

    eid = data.get("id")
    email = data.get("email")
    if not eid or not email:
        await repo.release_hold(user_id, price)
        raise PurchaseError()
    real_cost = _dec(data.get("cost")) or cost
    order = await repo.create_order(
        user_id=user_id, kind="email", activation_id=str(eid),
        service=site, service_name=site_name(site),
        country=domain, country_name=domain, phone=str(email),
        cost=real_cost, price=price, status=Order.WAITING, code=None,
        expires_at=_now() + dt.timedelta(minutes=settings.email_timeout_min),
    )
    log.info("Email order %s HELD: user=%s site=%s domain=%s email=%s price=%s",
             order.id, user_id, site, domain, email, price)
    return order


# ─── poll / deliver / close ──────────────────────────────────────────────────
async def poll_email_status(order: Order, client: HeroSMSV1Client) -> tuple[str, str | None]:
    """Returns (status, code). status ∈ {SUCCESS, WAIT, CANCEL, UNKNOWN};
    code is the OTP when SUCCESS."""
    try:
        data = (await client.email_status(order.activation_id) or {}).get("data") or {}
    except Exception:  # noqa: BLE001
        return "UNKNOWN", None
    status = str(data.get("status") or "").upper()
    value = data.get("value")
    if not value and status == "SUCCESS":
        from utils import extract_code
        value = extract_code(data.get("message"))
    if value:
        return "SUCCESS", str(value)
    if status == "CANCEL":
        return "CANCEL", None
    return "WAIT", None


async def close_email_unfilled(
    order: Order, client: HeroSMSV1Client, *,
    final_status: str, from_statuses: tuple[str, ...] = (Order.WAITING,),
) -> bool:
    """Close an email order that never produced a code: release the hold (no
    charge) and free the mailbox. Atomic claim → releases exactly once."""
    if not await repo.close_order(order.id, final_status, from_statuses):
        return False
    try:
        await client.email_cancel(order.activation_id)
    except Exception:  # noqa: BLE001 — best-effort free at the provider
        pass
    await repo.release_hold(order.user_id, order.price)
    log.info("Email order %s closed as %s (hold released)", order.id, final_status)
    return True


async def cancel_email(order: Order, client: HeroSMSV1Client) -> bool:
    """User/admin cancel of a WAITING email: release the hold, no charge."""
    return await close_email_unfilled(order, client, final_status=Order.CANCELED)


async def complete_email(order: Order, client: HeroSMSV1Client) -> None:
    """Finish a RECEIVED email order (already charged). No provider finish needed."""
    await repo.close_order(order.id, Order.COMPLETED, (Order.RECEIVED,))


async def reorder_email(order: Order, client: HeroSMSV1Client) -> str:
    """Request another email/code on the same activation (a fresh hold + charge).

    Mirrors orders.request_another_code: win RECEIVED→REQUESTING (a TRANSIENT
    status the email poller ignores) so the poller can't re-deliver the stale
    already-received OTP and double-charge during the email_reorder call. Reset the
    provider first, THEN expose it as WAITING. Release + roll back on failure."""
    if not await repo.close_order(order.id, Order.REQUESTING, (Order.RECEIVED,)):
        return ANOTHER_ERROR
    if not await repo.try_hold(order.user_id, order.price):
        await repo.close_order(order.id, Order.RECEIVED, (Order.REQUESTING,))
        return ANOTHER_INSUFFICIENT
    try:
        await client.email_reorder(order.activation_id)
    except Exception as exc:  # noqa: BLE001
        await repo.release_hold(order.user_id, order.price)
        await repo.close_order(order.id, Order.RECEIVED, (Order.REQUESTING,))
        log.warning("email reorder %s failed: %s", order.activation_id, exc)
        return ANOTHER_ERROR
    await repo.reopen_waiting(
        order.id, (Order.REQUESTING,),
        _now() + dt.timedelta(minutes=settings.email_timeout_min),
    )
    return ANOTHER_OK


# ─── card ────────────────────────────────────────────────────────────────────
def format_email_card(order: Order) -> str:
    from utils import money, STATUS_LABELS
    from services.orders import latest_code

    lines = [
        f"📧 <b>Email OTP — {order.service_name or order.service}</b>",
        f"✉️ Address: <code>{order.phone}</code>  <i>(tap to copy)</i>",
        f"💵 Price: <b>{money(order.price)}</b>",
        f"📌 Status: {STATUS_LABELS.get(order.status, order.status)}",
    ]
    if order.status == Order.WAITING and getattr(order, "expires_at", None):
        exp = order.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=dt.timezone.utc)
        secs = int((exp - _now()).total_seconds())
        if secs > 0:
            m, s = divmod(secs, 60)
            lines.append(f"⏱ Time left: <b>{m}:{s:02d}</b>")
        lines.append(
            f"\n<i>Use <code>{order.phone}</code> to sign up on "
            f"{order.service_name or order.service}; the code appears here automatically.</i>"
        )
    code = latest_code(order)
    if code:
        lines.append(f"\n🔑 <b>Code:</b> <code>{code}</code>  <i>(tap to copy)</i>")
    lines.append(f"\n<i>Order #{order.id}</i>")
    return "\n".join(lines)
