"""Number rental: rent a number for a period (1 day+) and receive many SMS.

Charge model: a rental is paid UPFRONT for the whole period (unlike SMS
activations which charge on receive). SMS that arrive during the period are
free. The hold is released only if the rental can't be created.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal

from config import settings
from db import repo
from db.models import Order
from herosms import HeroSMSClient, HeroSMSError
from services import pricing
from services.catalog import Catalog
from services.orders import InsufficientFunds, PurchaseError

log = logging.getLogger(__name__)

# (hours, label) — HeroSMS supports 24/72/168/720/1440/2160/4320; minimum 1 day.
RENT_DURATIONS = [
    (24, "1 day"),
    (72, "3 days"),
    (168, "1 week"),
    (720, "1 month"),
]
DURATION_LABELS = {h: lbl for h, lbl in RENT_DURATIONS}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_end(end_date: str, duration_hours: int) -> dt.datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(end_date, fmt).replace(tzinfo=dt.timezone.utc)
        except (ValueError, TypeError):
            continue
    return _now() + dt.timedelta(hours=duration_hours)


async def rent_purchase(
    user_id: int,
    service: str,
    country: str,
    duration: int,
    cost_hint: Decimal,
    hero: HeroSMSClient,
    catalog: Catalog,
) -> Order:
    """Rent a number, charging the customer upfront for the whole period."""
    if await repo.has_open_order_for(user_id, service, country, kind="rent"):
        from services.orders import DuplicateOrder
        raise DuplicateOrder()

    price = await pricing.commission_price(cost_hint)
    if not await repo.try_hold(user_id, price):
        raise InsufficientFunds()

    try:
        rent = await hero.rent_number(service, country, duration)
    except HeroSMSError as exc:
        await repo.release_hold(user_id, price)
        log.warning("rent_number failed for user %s: %s", user_id, exc.code)
        raise PurchaseError() from exc
    except Exception as exc:  # noqa: BLE001
        await repo.release_hold(user_id, price)
        log.exception("rent_number crashed for user %s", user_id)
        raise PurchaseError() from exc

    # Rental is purchased -> finalize the charge now (paid for the period).
    # MUST check the result: charge_hold is a conditional UPDATE that no-ops if the
    # balance/held moved underneath us (concurrent buy/admin edit). If it doesn't
    # fire we'd hand out a free rental AND strand the hold forever ('money not
    # back') — so release the hold, give the number back, and abort.
    if not await repo.charge_hold(user_id, price):
        await repo.release_hold(user_id, price)
        try:
            await hero.set_rent_status(rent.id, 2)  # cancel — give the number back
        except Exception:  # noqa: BLE001
            pass
        log.critical("rent charge_hold FAILED user=%s price=%s — released + cancelled", user_id, price)
        raise PurchaseError()
    real_cost = rent.cost if rent.cost > 0 else cost_hint

    order = await repo.create_order(
        user_id=user_id,
        kind="rent",
        activation_id=rent.id,
        service=service,
        service_name=await catalog.service_name(service),
        country=country,
        country_name=await catalog.country_name(country),
        phone=rent.phone,
        cost=real_cost,
        price=price,
        status=Order.WAITING,
        code=json.dumps([]),
        expires_at=_parse_end(rent.end_date, duration),
    )
    log.info("Rent order %s: user=%s %s/%s phone=%s price=%s until=%s",
             order.id, user_id, service, country, rent.phone, price, order.expires_at)
    return order


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def parse_prolong_options(raw: dict) -> dict[int, Decimal]:
    """Flatten a hero.prolong_options() response into {hours: wholesale_cost}.

    Tolerates the {data:{options:[...]}} envelope, a bare {options:[...]}, or a
    dict-of-options; reads hours from 'hours'/'duration' and cost defensively from
    'price'/'cost'/'retail_price'. Rows with non-positive hours/cost are dropped.
    """
    options = []
    if isinstance(raw, dict):
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        options = (data or {}).get("options") or raw.get("options") or []
    if isinstance(options, dict):
        options = list(options.values())
    out: dict[int, Decimal] = {}
    for opt in options or []:
        if not isinstance(opt, dict):
            continue
        try:
            hours = int(opt.get("hours") or opt.get("duration") or 0)
        except (TypeError, ValueError):
            continue
        cost = _dec(opt.get("price") or opt.get("cost") or opt.get("retail_price") or 0)
        if hours > 0 and cost > 0:
            out[hours] = cost
    return out


async def prolong_rent(order: Order, duration: int, cost: Decimal, hero: HeroSMSClient) -> bool:
    """Extend an active rental's session, charged UPFRONT (rent-style).

    Money-safe sequence (mirrors rent_purchase + the activation hold pattern):
      1) win the WAITING/RECEIVED -> PROLONGING transition atomically (this is the
         exactly-once gate — it blocks a double-tap AND a racing cancel/finish/expire),
      2) try_hold the commission price,
      3) call hero.prolong,
      4) charge_hold,
      5) push out expires_at and restore the prior status in one write.
    Any failure releases the hold and restores the ORIGINAL status + expiry (the
    expiry is never written on an error path, so it can't be lost).
    """
    from services.orders import ProlongError

    price = await pricing.commission_price(cost)
    prev = order.status
    if not await repo.close_order(order.id, Order.PROLONGING, (Order.WAITING, Order.RECEIVED)):
        raise ProlongError()
    if not await repo.try_hold(order.user_id, price):
        await repo.update_order(order.id, status=prev)  # restore (expiry untouched)
        raise InsufficientFunds()
    try:
        await hero.prolong(order.activation_id, duration)
    except Exception as exc:  # noqa: BLE001
        await repo.release_hold(order.user_id, price)
        await repo.update_order(order.id, status=prev)  # ORIGINAL expiry preserved
        log.warning("prolong %s failed: %s", order.activation_id, exc)
        raise PurchaseError() from exc
    if not await repo.charge_hold(order.user_id, price):
        await repo.release_hold(order.user_id, price)
        await repo.update_order(order.id, status=prev)
        log.critical("prolong charge_hold FAILED for order %s — RECONCILE", order.id)
        raise PurchaseError()
    exp = order.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=dt.timezone.utc)
    new_expiry = exp + dt.timedelta(hours=duration)
    await repo.update_order(order.id, expires_at=new_expiry, status=prev)
    log.info("Rent order %s EXTENDED +%sh -> %s (charged %s)",
             order.id, duration, new_expiry, price)
    return True


def stored_sms(order: Order) -> list[dict]:
    try:
        data = json.loads(order.code or "[]")
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


async def poll_rent_sms(order: Order, hero: HeroSMSClient) -> list[dict]:
    """Fetch the rental's SMS; persist + return only the NEW ones since last poll."""
    try:
        received = await hero.rent_status(order.activation_id)
    except HeroSMSError:
        return []
    existing = stored_sms(order)
    if len(received) <= len(existing):
        return []
    new = received[len(existing):]
    await repo.update_order(order.id, code=json.dumps(received))
    return new


async def finish_rent(order: Order, hero: HeroSMSClient) -> None:
    if not await repo.close_order(order.id, Order.COMPLETED, (Order.WAITING, Order.RECEIVED)):
        return
    try:
        await hero.set_rent_status(order.activation_id, 1)  # finish
    except HeroSMSError as exc:
        log.warning("finish rent %s failed: %s", order.activation_id, exc.code)


async def cancel_rent_refund(order: Order, hero: HeroSMSClient) -> bool:
    """Cancel a rental and refund the upfront charge.

    Only valid inside the provider's window (after 2 min, before 20 min) — the
    caller checks that. Wins the close transition atomically so the refund runs
    exactly once, then cancels the number at HeroSMS and credits the customer.
    """
    if not await repo.close_order(order.id, Order.CANCELED, (Order.WAITING, Order.RECEIVED)):
        return False
    try:
        await hero.set_rent_status(order.activation_id, 2)  # cancel
    except HeroSMSError as exc:
        log.warning("cancel rent %s failed: %s", order.activation_id, exc.code)
    await repo.credit(order.user_id, order.price)  # refund the upfront payment
    log.info("Rent order %s CANCELED + refunded %s", order.id, order.price)
    return True


def format_rent_card(order: Order) -> str:
    from country_flags import flag
    from utils import money

    lines = [
        f"📱 <b>Rental — {order.service_name or order.service}</b> "
        f"({flag(order.country)} {order.country_name or order.country})",
        "────────────────",
        f"📲 Number: <code>{order.phone}</code>  <i>(tap to copy)</i>",
        f"💳 Paid: <b>{money(order.price)}</b>",
    ]
    exp = order.expires_at
    if exp:
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=dt.timezone.utc)
        secs = int((exp - _now()).total_seconds())
        if secs > 0:
            h, rem = divmod(secs, 3600)
            m = rem // 60
            lines.append(f"⏱️ Active for: <b>{h}h {m}m</b>")
        else:
            lines.append("⏱️ <i>Rental ended</i>")
    # Cancellation window status (after 2 min, before 20 min → refundable).
    if order.status in (Order.WAITING, Order.RECEIVED):
        from utils import rent_cancel_state

        state, wsecs = rent_cancel_state(order)
        if state == "locked":
            lines.append(f"🔒 Cancel / refund opens in <b>{wsecs}s</b>")
        elif state == "open":
            wm, ws = divmod(wsecs, 60)
            lines.append(f"💸 Cancel for a full refund — <b>{wm}m {ws}s</b> left")
    sms = stored_sms(order)
    if sms:
        from utils import extract_code, short

        lines.append(f"\n🔑 <b>Received OTP / messages ({len(sms)})</b>")
        for s in sms[-5:]:
            txt = s.get("text", "")
            code = extract_code(txt)
            if code:
                lines.append(f"• 🔑 <code>{code}</code>  <i>(tap to copy)</i> — {short(txt, 40)}")
            else:
                lines.append(f"• <code>{short(txt, 70)}</code>")
    else:
        lines.append("\n⏳ <i>Waiting for OTP — they appear here automatically.</i>")
    lines.append(f"\n<i>Order #{order.id}</i>")
    return "\n".join(lines)
