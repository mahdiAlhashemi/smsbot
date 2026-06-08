"""Background loops: deliver SMS codes, refund expired orders, credit payments."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import signal
import time
from decimal import Decimal

from aiogram import Bot

from config import settings
from db import repo
from db.models import Order, Payment
from herosms import HeroSMSClient
from keyboards.menus import esim_order_keyboard, order_keyboard, wallet_keyboard
from services import orders as order_svc
from services.context import get_ctx
from utils import format_order, money

log = logging.getLogger(__name__)

# Throttle for collecting extra SMS/voice codes on already-RECEIVED orders
# (order_id -> last collect monotonic time). Display-only, so a coarse cadence
# is fine and keeps provider API volume low.
_last_code_collect: dict[int, float] = {}
_CODE_COLLECT_EVERY_SEC = 12


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime) -> dt.datetime:
    # SQLite may hand back naive datetimes; treat them as UTC.
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


async def order_poller(bot: Bot, hero: HeroSMSClient) -> None:
    log.info("order poller started (interval=%ss)", settings.poll_interval_sec)
    while True:
        try:
            await _poll_orders_once(bot, hero)
        except Exception:  # noqa: BLE001
            log.exception("order poller iteration failed")
        await asyncio.sleep(settings.poll_interval_sec)


async def _poll_orders_once(bot: Bot, hero: HeroSMSClient) -> None:
    # Self-heal: an order stuck in a transient provider-call status (a crash mid
    # 'another code' / 'reuse number') is excluded from get_open_orders, so it would
    # otherwise sit forever with a held-but-uncharged reservation. After 120s (well
    # past a single HTTP round-trip) roll it back and release the hold.
    for order in await repo.get_stuck_transient_orders():
        if _now() - _aware(order.updated_at) > dt.timedelta(seconds=120):
            target = Order.RECEIVED if order.status == Order.REQUESTING else Order.EXPIRED
            if await repo.close_order(order.id, target, (order.status,)):
                await repo.release_hold(order.user_id, order.price)
                log.warning("order %s stuck in %s — rolled back to %s + released hold",
                            order.id, order.status, target)

    for order in await repo.get_open_orders():
        try:
            await _handle_order(bot, hero, order)
        except Exception:  # noqa: BLE001
            log.exception("error handling order %s", order.id)
    # Retry releasing any HeroSMS numbers whose cancel was rejected earlier
    # (e.g. the SMS-Activate "too early to cancel" window).
    for order in await repo.get_orders_needing_release():
        try:
            await order_svc.retry_release(order, hero)
        except Exception:  # noqa: BLE001
            log.exception("error retrying release for order %s", order.id)


async def _handle_order(bot: Bot, hero: HeroSMSClient, order: Order) -> None:
    expired = _now() >= _aware(order.expires_at)

    if order.status == Order.WAITING:
        status, code = await hero.get_status(order.activation_id)
        if status == "OK" and code:
            # Charge-on-receive: this is where the customer actually pays.
            if await order_svc.deliver_code(order, code):
                # Grab any additional SMS / voice code already waiting in the same
                # window (display only — no extra charge).
                await order_svc.collect_codes(await repo.get_order(order.id), hero)
                fresh = await repo.get_order(order.id)
                await _sync_card(bot, fresh)  # auto-refresh the card in place
                await _notify(
                    bot, order.user_id,
                    f"🔑 <b>Code received</b>\n\n"
                    f"<code>{code}</code>\n\n"
                    f"💰 Charged: <b>{money(order.price)}</b>",
                )
            return
        if status == "CANCEL":
            await _close_unfilled(bot, hero, order, Order.CANCELED, "was cancelled by the provider")
            return
        if expired:
            await _close_unfilled(bot, hero, order, Order.EXPIRED, "expired")
            return
        # Still waiting — refresh the card so the countdown ticks down.
        await _sync_card(bot, order)

    elif order.status == Order.RECEIVED:
        if expired:
            # Code was delivered & charged; confirm usage and close.
            await order_svc.complete_order(order, hero)
            await _sync_card(bot, await repo.get_order(order.id))
            _last_code_collect.pop(order.id, None)
        else:
            # Keep showing any further SMS / voice codes that arrive on this number
            # (display only — no charge). Throttled to stay gentle on the provider.
            if time.monotonic() - _last_code_collect.get(order.id, 0.0) >= _CODE_COLLECT_EVERY_SEC:
                _last_code_collect[order.id] = time.monotonic()
                new = await order_svc.collect_codes(order, hero)
                if new:
                    await _sync_card(bot, await repo.get_order(order.id))
                    for e in new:
                        c = e.get("code") or ""
                        if not c:
                            continue
                        icon = "📞" if e.get("type") == "call" else "🔑"
                        await _notify(
                            bot, order.user_id,
                            f"{icon} <b>New code</b> — order #{order.id}\n\n<code>{c}</code>",
                        )


async def _sync_card(bot: Bot, order: Order) -> None:
    """Edit the order's live card message in place — the 'auto refresh'."""
    if not order or not order.chat_id or not order.message_id:
        return
    try:
        await bot.edit_message_text(
            format_order(order),
            chat_id=order.chat_id,
            message_id=order.message_id,
            reply_markup=order_keyboard(order),
            disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001 — unchanged / deleted / too old; pings still inform
        pass


async def _close_unfilled(
    bot: Bot, hero: HeroSMSClient, order: Order, status: str, reason: str
) -> None:
    # No code arrived -> release the hold (no charge). Atomic claim => runs once.
    if await order_svc.close_unfilled(order, hero, final_status=status):
        await _sync_card(bot, await repo.get_order(order.id))
        await _notify(
            bot,
            order.user_id,
            f"❌ Your number for order <b>#{order.id}</b> {reason} with no code.\n\n"
            f"<i>⚡ You were not charged — the held <b>{money(order.price)}</b> was released.</i>",
        )


# ─── Rent poller: deliver SMS to active rentals ─────────────────────────────
async def rent_poller(bot: Bot, hero: HeroSMSClient) -> None:
    interval = max(10, settings.poll_interval_sec * 3)
    log.info("rent poller started (interval=%ss)", interval)
    while True:
        try:
            await _poll_rents_once(bot, hero)
        except Exception:  # noqa: BLE001
            log.exception("rent poller iteration failed")
        await asyncio.sleep(interval)


async def _poll_rents_once(bot: Bot, hero: HeroSMSClient) -> None:
    from services import rent as rent_svc

    # Self-heal: a rental stuck in PROLONGING (crash mid-extend) is reset to WAITING
    # so normal polling resumes. The flip is sub-second, so anything older than 120s
    # is genuinely stuck. (get_open_rent_orders excludes PROLONGING, so an in-flight
    # extend is transparently skipped by normal SMS polling.)
    for order in await repo.get_prolonging_orders():
        if _now() - _aware(order.updated_at) > dt.timedelta(seconds=120):
            if await repo.close_order(order.id, Order.WAITING, (Order.PROLONGING,)):
                log.warning("rent order %s stuck in PROLONGING — reset to WAITING", order.id)
                await _sync_rent_card(bot, await repo.get_order(order.id))

    for order in await repo.get_open_rent_orders():
        try:
            if _now() >= _aware(order.expires_at):
                await rent_svc.finish_rent(order, hero)
                await _sync_rent_card(bot, await repo.get_order(order.id))
                await _notify(bot, order.user_id, f"✅ <b>Rental #{order.id}</b> has ended.")
                continue
            new = await rent_svc.poll_rent_sms(order, hero)
            if new:
                await _sync_rent_card(bot, await repo.get_order(order.id))
                for s in new:
                    await _notify(
                        bot, order.user_id,
                        f"🔑 <b>New OTP</b> — rental #{order.id}\n\n<code>{s.get('text', '')}</code>",
                    )
            else:
                # Re-render in place so the active-time countdown ticks and the
                # cancel button flips locked→refundable→closed on its own.
                await _sync_rent_card(bot, order)
        except Exception:  # noqa: BLE001
            log.exception("error polling rent order %s", order.id)


async def _sync_rent_card(bot: Bot, order: Order) -> None:
    if not order or not order.chat_id or not order.message_id:
        return
    from services import rent as rent_svc
    from keyboards.menus import rent_order_keyboard
    try:
        await bot.edit_message_text(
            rent_svc.format_rent_card(order), chat_id=order.chat_id,
            message_id=order.message_id, reply_markup=rent_order_keyboard(order),
            disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001
        pass


# ─── eSIM poller: deliver the QR once a profile finishes provisioning ────────
async def esim_poller(bot: Bot) -> None:
    interval = 8
    log.info("eSIM poller started (interval=%ss)", interval)
    while True:
        try:
            await _poll_esim_once(bot)
        except Exception:  # noqa: BLE001
            log.exception("eSIM poller iteration failed")
        await asyncio.sleep(interval)


async def _poll_esim_once(bot: Bot) -> None:
    ctx = get_ctx()
    if ctx.esim is None:
        return
    from services import esim as esim_svc

    for order in await repo.get_open_esim_orders():
        try:
            profile = await esim_svc.poll_esim_provision(order, ctx.esim)
            if not profile:
                # Past the provisioning deadline and still no QR → alert the user
                # and admin once, then back off so we don't re-alert every cycle.
                if _now() >= _aware(order.expires_at):
                    await _notify(
                        bot, order.user_id,
                        f"⏳ <b>eSIM order #{order.id}</b> is taking longer than usual to prepare.\n\n"
                        "We're still on it — the QR will appear here automatically.\n"
                        "<i>ℹ️ If it doesn't arrive soon, please contact support.</i>",
                    )
                    for aid in settings.admin_id_list:
                        await _notify(bot, aid,
                                      f"⚠️ <b>eSIM order #{order.id}</b> not provisioned past deadline — check esimaccess.")
                    await repo.update_order(order.id, expires_at=_now() + dt.timedelta(hours=6))
                continue
            fresh = await repo.get_order(order.id)
            # Update the live card in place, then push the QR image.
            if fresh and fresh.chat_id and fresh.message_id:
                try:
                    await bot.edit_message_text(
                        esim_svc.format_esim_card(fresh),
                        chat_id=fresh.chat_id, message_id=fresh.message_id,
                        reply_markup=esim_order_keyboard(fresh),
                        disable_web_page_preview=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
                if profile.qr_code_url:
                    try:
                        await bot.send_photo(
                            fresh.chat_id, profile.qr_code_url,
                            caption=(f"📡 <b>Scan to install</b> — <b>{profile.package_name or 'your eSIM'}</b>\n\n"
                                     "<i>💡 Settings → Mobile/Cellular → Add eSIM → Use QR Code.</i>"),
                        )
                    except Exception:  # noqa: BLE001
                        pass
            await _notify(bot, order.user_id,
                          f"✅ <b>eSIM order #{order.id}</b> is ready — QR code sent above.")
        except Exception:  # noqa: BLE001
            log.exception("error provisioning eSIM order %s", order.id)


# ─── Email poller: deliver the OTP (charge on receive) ───────────────────────
async def email_poller(bot: Bot) -> None:
    interval = max(5, settings.email_poll_interval_sec)
    log.info("email poller started (interval=%ss)", interval)
    while True:
        try:
            await _poll_emails_once(bot)
        except Exception:  # noqa: BLE001
            log.exception("email poller iteration failed")
        await asyncio.sleep(interval)


async def _poll_emails_once(bot: Bot) -> None:
    ctx = get_ctx()
    if ctx.herov1 is None:
        return
    from services import emails as email_svc

    for order in await repo.get_open_email_orders():
        try:
            expired = _now() >= _aware(order.expires_at)
            if order.status == Order.WAITING:
                status, code = await email_svc.poll_email_status(order, ctx.herov1)
                if status == "SUCCESS" and code:
                    # Charge-on-receive (atomic, exactly-once via deliver_code).
                    if await order_svc.deliver_code(order, code):
                        await _sync_email_card(bot, await repo.get_order(order.id))
                        await _notify(
                            bot, order.user_id,
                            f"🔑 <b>Email code received</b>\n\n<code>{code}</code>\n\n"
                            f"💰 Charged: <b>{money(order.price)}</b>",
                        )
                    continue
                if status == "CANCEL":
                    await email_svc.close_email_unfilled(order, ctx.herov1, final_status=Order.CANCELED)
                    await _sync_email_card(bot, await repo.get_order(order.id))
                    await _notify(bot, order.user_id,
                                  f"❌ <b>Email #{order.id}</b> was cancelled by the provider — not charged.")
                    continue
                if expired:
                    await email_svc.close_email_unfilled(order, ctx.herov1, final_status=Order.EXPIRED)
                    await _sync_email_card(bot, await repo.get_order(order.id))
                    await _notify(bot, order.user_id,
                                  f"⌛ <b>Email #{order.id}</b> expired with no code — not charged.")
                    continue
                await _sync_email_card(bot, order)  # tick the countdown
            elif order.status == Order.RECEIVED:
                if expired:
                    await email_svc.complete_email(order, ctx.herov1)
                    await _sync_email_card(bot, await repo.get_order(order.id))
        except Exception:  # noqa: BLE001
            log.exception("error polling email order %s", order.id)


async def _sync_email_card(bot: Bot, order: Order) -> None:
    if not order or not order.chat_id or not order.message_id:
        return
    from services import emails as email_svc
    from keyboards.menus import email_order_keyboard
    try:
        await bot.edit_message_text(
            email_svc.format_email_card(order), chat_id=order.chat_id,
            message_id=order.message_id, reply_markup=email_order_keyboard(order),
            disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001
        pass


# ─── Queue poller: fulfil PENDING orders by retrying getNumber ───────────────
async def queue_poller(bot: Bot, hero: HeroSMSClient) -> None:
    log.info("queue poller started (interval=%ss, give-up=%smin)",
             settings.queue_retry_sec, settings.queue_timeout_min)
    while True:
        try:
            await _poll_queue_once(bot, hero)
        except Exception:  # noqa: BLE001
            log.exception("queue poller iteration failed")
        await asyncio.sleep(settings.queue_retry_sec)


async def _poll_queue_once(bot: Bot, hero: HeroSMSClient) -> None:
    for order in await repo.get_pending_orders():
        try:
            if _now() >= _aware(order.expires_at):
                # Gave up: no number became available in time. Release the hold.
                if await order_svc.close_unfilled(
                    order, hero, final_status=Order.EXPIRED, from_statuses=(Order.PENDING,)
                ):
                    await _sync_card(bot, await repo.get_order(order.id))
                    await _notify(
                        bot, order.user_id,
                        f"❌ Sorry, we couldn't find a number for order <b>#{order.id}</b> in time.\n\n"
                        f"<i>⚡ You were not charged — <b>{money(order.price)}</b> released. Try again later.</i>",
                    )
                continue
            if await order_svc.try_fulfill_pending(order, hero):
                fresh = await repo.get_order(order.id)
                await _sync_card(bot, fresh)  # auto-refresh the queued card in place
                await _notify(
                    bot, order.user_id,
                    f"📲 <b>Your number is ready</b>\n\n<code>{fresh.phone}</code>\n\n"
                    "<i>⏳ Waiting for the OTP code — it appears automatically.</i>",
                )
        except Exception:  # noqa: BLE001
            log.exception("error fulfilling pending order %s", order.id)


async def payment_poller(bot: Bot, provider) -> None:
    log.info("payment poller started (%s, interval=%ss)",
             provider.name, settings.payment_poll_interval_sec)
    while True:
        try:
            await _poll_payments_once(bot, provider)
        except Exception:  # noqa: BLE001
            log.exception("payment poller iteration failed")
        await asyncio.sleep(settings.payment_poll_interval_sec)


async def _poll_payments_once(bot: Bot, provider) -> None:
    for payment in await repo.get_pending_payments():
        try:
            status = await provider.invoice_status(payment.invoice_id, f"nh{payment.id}")
        except Exception as exc:  # noqa: BLE001
            # Transient provider/network error — log and retry next cycle. NEVER
            # expire on an error: the invoice might actually be paid and we just
            # can't reach the provider, and expiring it would lose the user's funds.
            log.warning("payment %s status check failed: %s", payment.id, exc)
            continue
        if status == "paid":
            # The webhook may have credited this already; settle_payment is atomic
            # and idempotent, so only the first caller credits + we notify once.
            from services import billing
            credited, _p, new_bal, bonus = await billing.settle_payment(payment.id)
            if credited:
                bonus_line = f"\n🎁 Bonus: <b>+{money(bonus)}</b>" if bonus > 0 else ""
                await _notify(
                    bot,
                    payment.user_id,
                    f"✅ <b>Top-up received!</b>\n\n"
                    f"💳 Added: <b>{money(payment.amount)}</b>{bonus_line}\n"
                    f"💰 New balance: <b>{money(new_bal)}</b>",
                    wallet_keyboard(settings.payments_enabled),
                )
        elif status == "expired":
            await repo.expire_payment(payment.id)
        elif _now() - _aware(payment.created_at) > dt.timedelta(hours=24):
            # Reached ONLY after a successful 'pending' read: the provider still
            # reports it unpaid a full day later, so the user abandoned it (real
            # invoices expire long before 24h). Safe to stop polling it.
            log.info("expiring abandoned unpaid payment %s (age > 24h)", payment.id)
            await repo.expire_payment(payment.id)


# ─── Low-balance alerts: DM admins before a provider runs dry ────────────────
_last_balance_alert: dict[str, float] = {}


async def balance_alert_poller(bot: Bot, hero, esim) -> None:
    interval = 600  # 10 min
    log.info("balance alert poller started (interval=%ss)", interval)
    while True:
        try:
            await _check_provider_balances(bot, hero, esim)
        except Exception:  # noqa: BLE001
            log.exception("balance alert iteration failed")
        await asyncio.sleep(interval)


async def _check_provider_balances(bot: Bot, hero, esim) -> None:
    threshold = settings.low_balance_threshold
    admins = settings.admin_id_list
    if not admins:
        return

    async def alert(name: str, bal) -> None:
        # 1-hour cooldown per provider so admins aren't spammed.
        if time.monotonic() - _last_balance_alert.get(name, 0.0) < 3600:
            return
        _last_balance_alert[name] = time.monotonic()
        for aid in admins:
            await _notify(
                bot, aid,
                f"⚠️ <b>Low {name} balance</b>\n\n"
                f"💰 Balance: <b>{money(bal)}</b>\n"
                f"ℹ️ Alert below: {money(threshold)}\n\n"
                "<i>💡 Top it up to avoid failed orders.</i>",
            )

    try:
        hb = await hero.get_balance()
        if hb < threshold:
            await alert("HeroSMS", hb)
    except Exception:  # noqa: BLE001
        pass
    if esim is not None:
        try:
            eb = await esim.balance()
            if eb < threshold:
                await alert("eSIM", eb)
        except Exception:  # noqa: BLE001
            pass


# ─── Win-back: re-engage funded but idle users (weekly cadence) ──────────────
async def winback_poller(bot: Bot) -> None:
    interval = 6 * 3600  # check every 6h; the sweep itself runs at most weekly
    log.info("winback poller started (interval=%ss)", interval)
    while True:
        try:
            await _winback_once(bot)
        except Exception:  # noqa: BLE001
            log.exception("winback iteration failed")
        await asyncio.sleep(interval)


async def _winback_once(bot: Bot) -> None:
    last = await repo.get_setting("winback_last_run")
    now = time.time()
    if last:
        try:
            if now - float(last) < 7 * 86400:
                return
        except ValueError:
            pass
    await repo.set_setting("winback_last_run", str(now))
    cands = await repo.get_winback_candidates(min_balance=Decimal("0.5"), idle_days=7, limit=100)
    if not cands:
        return
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from keyboards.callbacks import Nav

    kb = InlineKeyboardBuilder()
    kb.button(text="📲 Buy a number", callback_data=Nav(to="buy"))
    kb.adjust(1)
    log.info("winback: nudging %s funded idle users", len(cands))
    for uid in cands:
        u = await repo.get_user(uid)
        if u is None:
            continue
        await _notify(
            bot, uid,
            f"👋 You still have <b>{money(u.available)}</b> waiting in your NumberHub wallet.\n\n"
            "<i>💡 Grab a number or eSIM anytime!</i>",
            kb.as_markup(),
        )
        await asyncio.sleep(0.05)


async def _notify(bot: Bot, user_id: int, text: str, reply_markup=None) -> None:
    try:
        await bot.send_message(user_id, text, reply_markup=reply_markup, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        log.debug("could not notify user %s", user_id)


def _supervise(task: asyncio.Task) -> None:
    """If a poller dies unexpectedly (not a clean shutdown cancel), crash the
    process so systemd/Docker restarts it — a silently-dead poller would stop
    delivering codes / crediting top-ups with no visible failure."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.critical("poller %s crashed (%s) — exiting for restart", task.get_name(), exc)
        os.kill(os.getpid(), signal.SIGTERM)


def start_pollers() -> list[asyncio.Task]:
    """Launch background tasks. Call once after the AppContext is set."""
    ctx = get_ctx()
    tasks = [
        asyncio.create_task(order_poller(ctx.bot, ctx.hero), name="order_poller"),
        asyncio.create_task(queue_poller(ctx.bot, ctx.hero), name="queue_poller"),
        asyncio.create_task(rent_poller(ctx.bot, ctx.hero), name="rent_poller"),
    ]
    if ctx.esim is not None:
        tasks.append(asyncio.create_task(esim_poller(ctx.bot), name="esim_poller"))
    if ctx.herov1 is not None:
        tasks.append(asyncio.create_task(email_poller(ctx.bot), name="email_poller"))
    if ctx.payments is not None:
        tasks.append(asyncio.create_task(payment_poller(ctx.bot, ctx.payments), name="payment_poller"))
    tasks.append(asyncio.create_task(
        balance_alert_poller(ctx.bot, ctx.hero, ctx.esim), name="balance_alert_poller"))
    tasks.append(asyncio.create_task(winback_poller(ctx.bot), name="winback_poller"))
    for t in tasks:
        t.add_done_callback(_supervise)
    return tasks
