"""Small formatting helpers shared across handlers."""
from __future__ import annotations

import datetime as _dt
import re as _re
from decimal import Decimal

from config import settings

# Matches a 4–8 digit verification code (the common OTP shape).
_OTP_RE = _re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


def extract_code(text) -> str | None:
    """Best-effort OTP extraction from an SMS body (the first 4–8 digit run)."""
    if not text:
        return None
    m = _OTP_RE.search(str(text))
    return m.group(1) if m else None

# HeroSMS cancellation policy (mirrors the provider's own rules):
#   Activation: cancel allowed only AFTER 2 min (within its 20-min code window).
#   Rent:       cancel allowed AFTER 2 min and NO LATER than 20 min (refundable).
CANCEL_AFTER_MIN = 2
RENT_CANCEL_AFTER_MIN = 2
RENT_CANCEL_BEFORE_MIN = 20


def money(amount) -> str:
    # None/empty -> $0.00 so a half-built order can never crash a card/keyboard.
    return f"{settings.currency_symbol}{Decimal(amount or 0):.2f}"


def _aware(value: _dt.datetime) -> _dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)


def activation_cancel_in(order) -> int:
    """Seconds until a WAITING activation may be cancelled.

    > 0  → still locked (HeroSMS rejects an early cancel); show a countdown.
    <= 0 → unlocked. The number was issued at expires_at - order life, and
    cancellation opens 2 minutes after issue.
    """
    if getattr(order, "expires_at", None) is None:
        return 0  # unknown issue time → don't block cancellation
    exp = _aware(order.expires_at)
    issued = exp - _dt.timedelta(minutes=settings.order_timeout_min)
    unlock = issued + _dt.timedelta(minutes=CANCEL_AFTER_MIN)
    return int((unlock - _dt.datetime.now(_dt.timezone.utc)).total_seconds())


def rent_cancel_state(order) -> tuple[str, int]:
    """Cancellation window for a rental, measured from when it started.

    Returns (state, seconds):
      "locked" → too early (within first 2 min); seconds until it opens.
      "open"   → refundable cancel allowed; seconds until the window closes.
      "closed" → past 20 min; no refund, only finish.
    """
    if getattr(order, "created_at", None) is None:
        return "open", 0  # unknown start → allow cancel/refund, fail-safe for customer
    start = _aware(order.created_at)
    now = _dt.datetime.now(_dt.timezone.utc)
    opens = start + _dt.timedelta(minutes=RENT_CANCEL_AFTER_MIN)
    closes = start + _dt.timedelta(minutes=RENT_CANCEL_BEFORE_MIN)
    if now < opens:
        return "locked", int((opens - now).total_seconds())
    if now <= closes:
        return "open", int((closes - now).total_seconds())
    return "closed", 0


def short(text: str, length: int = 24) -> str:
    text = text or ""
    return text if len(text) <= length else text[: length - 1] + "…"


# Human-readable order status labels.
STATUS_LABELS = {
    "pending": "🔎 Finding a number…",
    "waiting": "⏳ Waiting for code",
    "received": "✅ Code received",
    "completed": "✔️ Completed",
    "canceled": "❌ Cancelled (not charged)",
    "expired": "⌛ No number/code (not charged)",
}


def format_order(order) -> str:
    """Render an order card. `order` is a db.models.Order."""
    import datetime as _dt

    from country_flags import flag

    country = f"{flag(order.country)} {order.country_name or order.country}"
    lines = [
        f"📲 <b>{order.service_name or order.service}</b> — {country}",
    ]
    if order.status == "pending" or not order.phone:
        lines.append("🔎 <i>No number free yet — getting one for you…</i>")
    else:
        lines.append(f"📞 Number: <code>{order.phone}</code>  <i>(tap to copy)</i>")
    lines.append(f"💵 Price: <b>{money(order.price)}</b>")
    lines.append(f"📌 Status: {STATUS_LABELS.get(order.status, order.status)}")
    # Countdown: only for an ACTIVE number (its 20-minute life). A PENDING order
    # has no number yet, so it shows no timer.
    if order.status == "waiting" and getattr(order, "expires_at", None):
        exp = order.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=_dt.timezone.utc)
        secs = int((exp - _dt.datetime.now(_dt.timezone.utc)).total_seconds())
        if secs > 0:
            m, s = divmod(secs, 60)
            lines.append(f"⏱ Time left: <b>{m}:{s:02d}</b>")
    if order.code:
        lines.append(f"\n💬 Code: <code>{order.code}</code>")
    lines.append(f"\n<i>Order #{order.id}</i>")
    return "\n".join(lines)
