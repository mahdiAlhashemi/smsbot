"""Temp / disposable email inboxes via Mail.tm (free, keyless public API).

Charge model = CHARGE ON RECEIVE (same as SMS OTP): the flat fee is HELD when
the inbox is created and only CHARGED when the first email arrives. If no email
arrives within the window, the hold is released and nothing is charged.

The inbox token + received messages are stored as JSON in Order.code; the email
address is Order.phone; kind="email".
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
from decimal import Decimal

import httpx

from config import settings
from db import repo
from db.models import Order
from services.orders import InsufficientFunds, PurchaseError

log = logging.getLogger(__name__)

BASE = "https://api.mail.tm"
_TIMEOUT = httpx.Timeout(20.0)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def _api(method: str, path: str, *, token: str | None = None, body: dict | None = None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.request(method, f"{BASE}{path}", headers=headers, json=body)
        r.raise_for_status()
        return r.json() if r.content else {}


def _members(data) -> list:
    """Mail.tm collections come back either as a JSON-LD {hydra:member:[…]} or a
    plain list depending on Accept negotiation — handle both."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("hydra:member", []) or data.get("member", [])
    return []


async def _pick_domain() -> str:
    data = await _api("GET", "/domains?page=1")
    members = _members(data)
    for d in members:
        if d.get("isActive"):
            return d["domain"]
    if members:
        return members[0]["domain"]
    raise PurchaseError()


async def create_inbox() -> dict:
    """Create a fresh Mail.tm account + token. Returns {address, token, account_id}."""
    domain = await _pick_domain()
    address = f"{secrets.token_hex(5)}@{domain}"
    password = secrets.token_urlsafe(12)
    await _api("POST", "/accounts", body={"address": address, "password": password})
    tok = await _api("POST", "/token", body={"address": address, "password": password})
    return {"address": address, "token": tok.get("token", ""), "account_id": str(tok.get("id", ""))}


def _load(order: Order) -> dict:
    try:
        d = json.loads(order.code or "{}")
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


async def _fetch_messages(token: str) -> list[dict]:
    """Return [{id, from, subject, text, code}] for the inbox (newest first)."""
    try:
        data = await _api("GET", "/messages?page=1", token=token)
    except Exception:  # noqa: BLE001 — transient; treat as no new mail this cycle
        return []
    from utils import extract_code

    out: list[dict] = []
    for m in _members(data):
        mid = m.get("id")
        subject = m.get("subject", "") or ""
        frm = (m.get("from") or {}).get("address", "")
        text = m.get("intro", "") or ""
        try:
            full = await _api("GET", f"/messages/{mid}", token=token)
            text = (full.get("text") or text or subject)
        except Exception:  # noqa: BLE001
            pass
        out.append({
            "id": mid, "from": frm, "subject": subject,
            "text": (text or "")[:500],
            "code": extract_code(f"{subject} {text}") or "",
        })
    return out


async def email_purchase(user_id: int) -> Order:
    """Create an inbox and HOLD the fee (charged only when the first email lands)."""
    price = settings.temp_email_price
    if not await repo.try_hold(user_id, price):
        raise InsufficientFunds()
    try:
        inbox = await create_inbox()
    except (InsufficientFunds, PurchaseError):
        await repo.release_hold(user_id, price)
        raise
    except Exception as exc:  # noqa: BLE001
        await repo.release_hold(user_id, price)
        log.warning("create_inbox failed for user %s: %s", user_id, exc)
        raise PurchaseError() from exc

    window = max(5, settings.temp_email_window_min)
    order = await repo.create_order(
        user_id=user_id,
        kind="email",
        activation_id=inbox.get("account_id") or "email",
        service="email",
        service_name="Temp Email",
        country="",
        country_name="Mail.tm",
        phone=inbox["address"],
        cost=Decimal("0"),
        price=price,
        status=Order.WAITING,
        code=json.dumps({"token": inbox["token"], "address": inbox["address"], "messages": []}),
        expires_at=_now() + dt.timedelta(minutes=window),
    )
    log.info("Email order %s: user=%s address=%s price=%s", order.id, user_id, inbox["address"], price)
    return order


async def poll_email(order: Order) -> tuple[str | None, list[dict]]:
    """Poll the inbox. On the FIRST message, atomically win WAITING→RECEIVED and
    charge the held fee (charge-on-receive). Returns (event, new_messages) where
    event is 'charged' (first mail) | 'more' (later mail) | None (nothing new)."""
    data = _load(order)
    token = data.get("token", "")
    if not token:
        return None, []
    messages = await _fetch_messages(token)
    stored = data.get("messages", [])
    if len(messages) <= len(stored):
        return None, []
    new = messages[len(stored):]
    if order.status == Order.WAITING:
        # First email → charge exactly once (the atomic transition is the guard).
        if await repo.close_order(order.id, Order.RECEIVED, (Order.WAITING,)):
            await repo.charge_hold(order.user_id, order.price)
            await repo.update_order(order.id, code=json.dumps({**data, "messages": messages}))
            return "charged", new
        await repo.update_order(order.id, code=json.dumps({**data, "messages": messages}))
        return None, []
    # Already RECEIVED → additional emails are free.
    await repo.update_order(order.id, code=json.dumps({**data, "messages": messages}))
    return "more", new


async def expire_email(order: Order) -> bool:
    """No email arrived in the window → release the hold (no charge). Runs once."""
    if await repo.close_order(order.id, Order.EXPIRED, (Order.WAITING,)):
        await repo.release_hold(order.user_id, order.price)
        log.info("Email order %s EXPIRED, hold released", order.id)
        return True
    return False


async def cancel_email(order: Order) -> bool:
    """User closes a waiting inbox → release the hold (no charge)."""
    if await repo.close_order(order.id, Order.CANCELED, (Order.WAITING,)):
        await repo.release_hold(order.user_id, order.price)
        return True
    return False


def format_email_card(order: Order) -> str:
    from utils import money, short

    data = _load(order)
    address = data.get("address") or order.phone
    lines = [
        "📧 <b>Temp Email Inbox</b>",
        "────────────────",
        f"📬 Address: <code>{address}</code>  <i>(tap to copy)</i>",
        f"💳 Fee: <b>{money(order.price)}</b> <i>(charged only when the first email arrives)</i>",
    ]
    exp = order.expires_at
    if exp:
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=dt.timezone.utc)
        secs = int((exp - _now()).total_seconds())
        if order.status == Order.WAITING and secs > 0:
            lines.append(f"⏳ Waiting for email — <b>{secs // 60}m {secs % 60}s</b> left")
    msgs = data.get("messages", [])
    if msgs:
        lines.append(f"\n🔑 <b>Received ({len(msgs)})</b>")
        for m in msgs[-5:]:
            code = m.get("code")
            subj = short(m.get("subject", "") or "(no subject)", 40)
            if code:
                lines.append(f"• 🔑 <code>{code}</code> <i>(tap to copy)</i> — {subj}")
            else:
                lines.append(f"• <b>{subj}</b>: {short(m.get('text', ''), 60)}")
    elif order.status == Order.WAITING:
        lines.append("\n<i>⏳ No email yet — it appears here automatically. You're charged only when one arrives.</i>")
    lines.append(f"\n<i>Order #{order.id}</i>")
    return "\n".join(lines)
