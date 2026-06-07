"""eSIM data plans: browse packages, buy (paid upfront), deliver the QR code.

Money model: an eSIM is a FIXED-PRICE product paid UPFRONT (like rent, unlike
SMS activations). Customer price = wholesale cost x commission (no bid premium —
there's no demand auction). On a successful order the customer is charged and the
hold finalised; if the order call fails, the hold is released.

After a successful order the profile (QR code + activation string) is fetched via
`esim/query`. Provisioning is usually instant; a short inline poll covers the
common case and the background eSIM poller covers the rest.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import Decimal

from db import repo
from db.models import Order
from esim import EsimAccessClient, EsimError, EsimPackage, EsimProfile
from esim.client import format_data
from services import pricing
from services.orders import InsufficientFunds, PurchaseError

log = logging.getLogger(__name__)

# Popular destinations shown first when browsing (ISO-2 / region codes).
POPULAR_DESTINATIONS = [
    "US", "GB", "TR", "AE", "SA", "EG", "DE", "FR", "ES", "IT",
    "TH", "ID", "JP", "CN", "IN", "RU", "BR", "MX", "CA", "AU",
]

# eSIM order business errors mapped to friendly messages.
_FRIENDLY = {
    "balance": "The eSIM service is temporarily out of stock for top-ups. Please try again later.",
}


# ─── catalog cache ───────────────────────────────────────────────────────────
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


class EsimCatalog:
    """TTL cache over the eSIM Access catalog (regions + per-location packages)."""

    def __init__(self, client: EsimAccessClient):
        self._client = client
        self._regions = _TTL(900)
        self._pkgs: dict[str, _TTL] = {}
        self._lock = asyncio.Lock()

    async def regions(self) -> list[dict]:
        async with self._regions._lock:
            if not self._regions.fresh():
                try:
                    self._regions.set(await self._client.regions())
                except Exception:  # noqa: BLE001
                    if self._regions.value is None:
                        self._regions.set([])
            return self._regions.value

    async def packages_for(self, location_code: str) -> list[EsimPackage]:
        async with self._lock:
            ttl = self._pkgs.get(location_code)
            if ttl is None:
                ttl = self._pkgs[location_code] = _TTL(300)
        async with ttl._lock:
            if not ttl.fresh():
                pkgs = await self._client.packages(location_code=location_code)
                # Group by coverage (local → regional → global), then read like a
                # tariff menu within each: small → large data, shorter → longer,
                # cheaper first. Local plans (what most people want) surface first.
                pkgs.sort(key=lambda p: (p.scope_group, p.volume_bytes, p.duration, p.cost))
                ttl.set(pkgs)
            return ttl.value

    async def package(self, location_code: str, package_code: str) -> EsimPackage | None:
        for p in await self.packages_for(location_code):
            if p.code == package_code:
                return p
        return None

    async def region_name(self, code: str) -> str:
        for r in await self.regions():
            if str(r.get("code")) == code:
                return str(r.get("name", code))
        return code


# ─── stored profile (kept as JSON in Order.code) ─────────────────────────────
def _dump_profile(p: EsimProfile) -> str:
    return json.dumps({
        "esimTranNo": p.esim_tran_no, "iccid": p.iccid, "ac": p.ac,
        "qr": p.qr_code_url, "shortUrl": p.short_url, "smdp": p.smdp_address,
        "matchingId": p.matching_id, "smdpStatus": p.smdp_status,
        "expiredTime": p.expired_time, "pkg": p.package_name,
        "vol": p.total_volume, "dur": p.total_duration, "durUnit": p.duration_unit,
    })


def load_profile(order: Order) -> dict:
    try:
        d = json.loads(order.code or "{}")
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _gb(volume_bytes) -> str:
    return format_data(volume_bytes)


# ─── purchase ────────────────────────────────────────────────────────────────
async def esim_purchase(
    user_id: int, pkg: EsimPackage, esim: EsimAccessClient
) -> tuple[Order, EsimProfile | None]:
    """Charge the customer upfront and place the eSIM order.

    Returns (order, profile) where profile is the provisioned QR if it became
    available within the short inline poll, else None (the poller will deliver).
    """
    price = await pricing.esim_sell_price(pkg.cost)
    if not await repo.try_hold(user_id, price):
        raise InsufficientFunds()

    txn = f"nh{user_id}_{int(time.time())}"
    try:
        order_no = await esim.order(txn, pkg.code, pkg.cost, 1)
    except EsimError as exc:
        await repo.release_hold(user_id, price)
        msg = (exc.args[0] if exc.args else "") or ""
        log.warning("eSIM order failed for user %s: %s %s", user_id, exc.code, msg)
        friendly = PurchaseError()
        low = f"{exc.code} {msg}".lower()
        if "balance" in low or "insufficient" in low:
            friendly.user_message = (
                "This eSIM is <b>temporarily unavailable</b> (provider stock).\n"
                "<i>✅ You were not charged — please try another plan.</i>"
            )
        raise friendly from exc
    except Exception as exc:  # noqa: BLE001
        await repo.release_hold(user_id, price)
        log.exception("eSIM order crashed for user %s", user_id)
        raise PurchaseError() from exc

    if not order_no:
        await repo.release_hold(user_id, price)
        raise PurchaseError()

    # Order placed successfully -> finalise the charge (paid upfront).
    await repo.charge_hold(user_id, price)

    region = pkg.region_names[0] if pkg.region_names else pkg.location
    import datetime as dt
    order = await repo.create_order(
        user_id=user_id,
        kind="esim",
        activation_id=order_no,            # orderNo — used to query the profile
        service=pkg.code,
        service_name=pkg.name,
        country=(pkg.location or "")[:16],
        country_name=region[:120] if region else (pkg.location or ""),
        phone="",                          # filled with ICCID once provisioned
        cost=pkg.cost,
        price=price,
        status=Order.WAITING,              # WAITING = provisioning
        code=json.dumps({"pkg": pkg.name, "vol": pkg.volume_bytes,
                         "dur": pkg.duration, "durUnit": pkg.duration_unit}),
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
    )
    log.info("eSIM order %s placed: user=%s pkg=%s orderNo=%s price=%s",
             order.id, user_id, pkg.code, order_no, price)

    # Inline fast-path: try to grab the QR right away (usually ready in seconds).
    profile = None
    for _ in range(5):
        await asyncio.sleep(2)
        profile = await _try_provision(order, esim)
        if profile:
            break
    return order, profile


async def _try_provision(order: Order, esim: EsimAccessClient) -> EsimProfile | None:
    """Query the order's profile; if the QR is ready, persist it on the order."""
    try:
        profiles = await esim.query(order_no=order.activation_id)
    except EsimError:
        return None
    ready = next((p for p in profiles if p.ready), None)
    if not ready:
        return None
    # Persist the profile and mark delivered (WAITING -> RECEIVED, once). Only the
    # caller that WINS this transition returns the profile, so the inline buy poll
    # and the background esim_poller can never both send the QR for one order.
    if await repo.close_order(order.id, Order.RECEIVED, (Order.WAITING,)):
        await repo.update_order(order.id, phone=ready.iccid, code=_dump_profile(ready))
        log.info("eSIM order %s provisioned: iccid=%s", order.id, ready.iccid)
        return ready
    return None  # already delivered by another path — don't double-send the QR


async def poll_esim_provision(order: Order, esim: EsimAccessClient) -> EsimProfile | None:
    """Background-poller entry point (re-uses the inline provision logic)."""
    return await _try_provision(order, esim)


# ─── card ────────────────────────────────────────────────────────────────────
def format_esim_card(order: Order) -> str:
    from utils import money

    prof = load_profile(order)
    name = prof.get("pkg") or order.service_name or "eSIM"
    lines = [f"📡 <b>{name}</b>", f"💵 Paid: <b>{money(order.price)}</b>"]

    if order.status == Order.WAITING or not prof.get("qr"):
        lines.append("\n⏳ <i>Preparing your eSIM… the QR code appears here automatically.</i>")
        lines.append(f"\n<i>Order #{order.id}</i>")
        return "\n".join(lines)

    data = _gb(prof.get("vol"))
    dur = prof.get("dur")
    dur_unit = str(prof.get("durUnit", "DAY")).lower()
    if data != "—":
        lines.append(f"📦 Data: <b>{data}</b>")
    if dur:
        lines.append(f"📅 Validity: <b>{dur} {dur_unit}{'s' if dur != 1 else ''}</b> (from first use)")
    if prof.get("iccid"):
        lines.append(f"🔢 ICCID: <code>{prof['iccid']}</code>")
    lines.append("\n────────────────")
    lines.append("<b>📲 Install — easiest way</b>\nScan the QR code below in:")
    lines.append("Settings → Mobile/Cellular → Add eSIM → Use QR Code.")
    if prof.get("smdp") and prof.get("matchingId"):
        lines.append(
            "\n<b>✍️ Or enter manually</b>\n"
            f"SM-DP+ Address: <code>{prof['smdp']}</code>\n"
            f"Activation Code: <code>{prof['matchingId']}</code>"
        )
    if prof.get("shortUrl"):
        lines.append(f"\n🔗 Universal link: {prof['shortUrl']}")
    lines.append("\n💡 <i>Install over Wi-Fi. Keep this message — you'll need the QR to reinstall.</i>")
    lines.append(f"\n<i>Order #{order.id}</i>")
    return "\n".join(lines)
