"""Background loops: deliver SMS codes, refund expired orders, credit payments."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import signal
import time

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
                fresh = await repo.get_order(order.id)
                await _sync_card(bot, fresh)  # auto-refresh the card in place
                await _notify(
                    bot, order.user_id,
                    f"💬 <b>Code received:</b> <code>{code}</code>  (charged {money(order.price)})",
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
            f"⌛ Your number for order #{order.id} {reason} with no code.\n"
            f"You were <b>not charged</b> (the held {money(order.price)} was released).",
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

    for order in await repo.get_open_rent_orders():
        try:
            if _now() >= _aware(order.expires_at):
                await rent_svc.finish_rent(order, hero)
                await _sync_rent_card(bot, await repo.get_order(order.id))
                await _notify(bot, order.user_id, f"⌛ Your rental #{order.id} has ended.")
                continue
            new = await rent_svc.poll_rent_sms(order, hero)
            if new:
                await _sync_rent_card(bot, await repo.get_order(order.id))
                for s in new:
                    await _notify(
                        bot, order.user_id,
                        f"💬 <b>New SMS</b> on rental #{order.id}:\n<code>{s.get('text', '')}</code>",
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
                        f"⏳ Your eSIM (order #{order.id}) is taking longer than usual to "
                        "prepare. We're still on it — the QR will appear here automatically. "
                        "If it doesn't arrive soon, please contact support.",
                    )
                    for aid in settings.admin_id_list:
                        await _notify(bot, aid,
                                      f"⚠️ eSIM order #{order.id} not provisioned past deadline — check esimaccess.")
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
                            caption=("📲 <b>Scan to install</b> — "
                                     f"{profile.package_name or 'your eSIM'}\n"
                                     "Settings → Mobile/Cellular → Add eSIM → Use QR Code."),
                        )
                    except Exception:  # noqa: BLE001
                        pass
            await _notify(bot, order.user_id,
                          f"✅ Your eSIM (order #{order.id}) is ready — QR code sent above.")
        except Exception:  # noqa: BLE001
            log.exception("error provisioning eSIM order %s", order.id)


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
                        f"😔 Sorry, we couldn't find a number for order #{order.id} in time.\n"
                        f"You were <b>not charged</b> ({money(order.price)} released). Try again later.",
                    )
                continue
            if await order_svc.try_fulfill_pending(order, hero):
                fresh = await repo.get_order(order.id)
                await _sync_card(bot, fresh)  # auto-refresh the queued card in place
                await _notify(
                    bot, order.user_id,
                    f"✅ <b>Your number is ready:</b> <code>{fresh.phone}</code>\n"
                    "Waiting for the SMS code — it appears automatically.",
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
            if await repo.mark_payment_paid(payment.id):
                # Invoices are amount-locked (currency=USD, to_currency=USDT), so
                # the requested amount is what the customer paid.
                new_bal = await repo.credit(payment.user_id, payment.amount)
                await _notify(
                    bot,
                    payment.user_id,
                    f"✅ <b>Top-up received!</b>\n\n{money(payment.amount)} added.\n"
                    f"New balance: <b>{money(new_bal)}</b>",
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
                f"⚠️ <b>Low {name} balance: {money(bal)}</b> (alert below {money(threshold)}).\n"
                "Top it up to avoid failed orders.",
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
    if ctx.payments is not None:
        tasks.append(asyncio.create_task(payment_poller(ctx.bot, ctx.payments), name="payment_poller"))
    tasks.append(asyncio.create_task(
        balance_alert_poller(ctx.bot, ctx.hero, ctx.esim), name="balance_alert_poller"))
    for t in tasks:
        t.add_done_callback(_supervise)
    return tasks
