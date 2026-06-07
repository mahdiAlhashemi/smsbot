"""My orders: list active + history, and per-order actions."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from db import repo
from db.models import Order
from handlers.common import safe_edit
from keyboards.callbacks import Nav, OrderAct
from keyboards.menus import back_button, order_keyboard
from services import orders as order_svc
from services.context import get_ctx
from utils import STATUS_LABELS, format_order, money

log = logging.getLogger(__name__)
router = Router(name="orders")


@router.callback_query(Nav.filter(F.to == "orders"))
async def list_orders(call: CallbackQuery) -> None:
    uid = call.from_user.id
    open_orders = await repo.get_user_open_orders(uid)
    history = await repo.get_user_orders(uid, limit=10)

    lines = ["🧾 <b>Your Orders</b>", "────────────────"]
    if not history:
        lines.append("<i>You have no orders yet.</i>\n\n👇 Tap 📲 <b>Buy number</b> to get started.")
    else:
        lines.append("<b>Recent history</b>\n")
        for o in history:
            label = STATUS_LABELS.get(o.status, o.status)
            code = f" — 🔑 <code>{o.code}</code>" if o.code else ""
            lines.append(
                f"🧾 #{o.id} <b>{o.service_name or o.service}</b> "
                f"({o.country_name or o.country}) — <b>{money(o.price)}</b> — {label}{code}"
            )
    if open_orders:
        lines.append(f"\n⏳ <b>{len(open_orders)}</b> active order(s) below 👇")

    await safe_edit(call, "\n".join(lines), back_button("main"))
    for o in open_orders:
        if o.kind == "rent":
            from keyboards.menus import rent_order_keyboard
            from services.rent import format_rent_card
            sent = await call.message.answer(format_rent_card(o), reply_markup=rent_order_keyboard(o))
        elif o.kind == "esim":
            from keyboards.menus import esim_order_keyboard
            from services.esim import format_esim_card
            sent = await call.message.answer(format_esim_card(o), reply_markup=esim_order_keyboard(o))
        else:
            sent = await call.message.answer(format_order(o), reply_markup=order_keyboard(o))
        # Point the live card at this newest message so the poller updates it.
        await repo.update_order(o.id, chat_id=sent.chat.id, message_id=sent.message_id)
    await call.answer()


async def _owned(call: CallbackQuery, order_id: int) -> Order | None:
    order = await repo.get_order(order_id)
    if order is None or order.user_id != call.from_user.id:
        await call.answer("❌ Order not found.", show_alert=True)
        return None
    return order


@router.callback_query(OrderAct.filter(F.action == "refresh"))
async def refresh_order(call: CallbackQuery, callback_data: OrderAct) -> None:
    order = await _owned(call, callback_data.id)
    if order is None:
        return
    if not order.is_open:
        await call.answer("ℹ️ This order is already closed.")
        await _rerender(call, order)
        return
    ctx = get_ctx()
    if order.status == Order.PENDING:
        # Queued order: try to grab a number right now.
        if await order_svc.try_fulfill_pending(order, ctx.hero):
            order = await repo.get_order(order.id)
            await _rerender(call, order)
            await call.answer("✅ Number found!")
        else:
            await call.answer("⏳ Still searching — no number free yet. I'll keep trying.")
        return
    try:
        status, code = await ctx.hero.get_status(order.activation_id)
    except Exception:  # noqa: BLE001
        await call.answer("⚠️ Couldn't reach the provider — please try again.", show_alert=True)
        return
    if status == "OK" and code:
        # Charge-on-receive happens here (atomically, once).
        await order_svc.deliver_code(order, code)
        order = await repo.get_order(order.id)
        await _rerender(call, order)
        await call.answer(f"✅ Code received! Charged {money(order.price)}.")
    else:
        await call.answer("⏳ No code yet — please wait.")


@router.callback_query(OrderAct.filter(F.action == "cancel"))
async def cancel_order_cb(call: CallbackQuery, callback_data: OrderAct) -> None:
    order = await _owned(call, callback_data.id)
    if order is None:
        return
    # A queued (PENDING) order has no number yet → cancel is free immediately.
    # A WAITING order has a live number: HeroSMS only allows cancel after 2 min.
    if order.status == Order.WAITING:
        from utils import activation_cancel_in
        locked = activation_cancel_in(order)
        if locked > 0:
            await call.answer(
                f"⏳ Cancellation unlocks 2 minutes after the number is issued "
                f"— {locked}s left.",
                show_alert=True,
            )
            return
    ctx = get_ctx()
    ok = await order_svc.cancel_order(order, ctx.hero)
    if ok:
        order = await repo.get_order(order.id)
        await _rerender(call, order)
        await call.answer(f"✅ Canceled — not charged. {money(order.price)} released.", show_alert=True)
    else:
        await call.answer("⚠️ Can't cancel now — a code may have arrived.", show_alert=True)


@router.callback_query(OrderAct.filter(F.action == "replace"))
async def replace_order_cb(call: CallbackQuery, callback_data: OrderAct) -> None:
    order = await _owned(call, callback_data.id)
    if order is None:
        return
    if order.status != Order.WAITING:
        await call.answer("🔁 Replace is only available while waiting for a code.", show_alert=True)
        return
    ctx = get_ctx()
    await call.answer("🔁 Replacing number…")
    new_order = await order_svc.replace_number(order, ctx.hero, ctx.catalog)
    if new_order is None:
        await call.answer("⚠️ Couldn't replace right now — please try again.", show_alert=True)
        return
    await _rerender(call, new_order)


@router.callback_query(OrderAct.filter(F.action == "another"))
async def another_code_cb(call: CallbackQuery, callback_data: OrderAct) -> None:
    order = await _owned(call, callback_data.id)
    if order is None:
        return
    # Only a RECEIVED order can request another code; short-circuit stale taps
    # before any money is touched (defence-in-depth with the atomic gate in
    # request_another_code).
    if order.status != Order.RECEIVED:
        await call.answer("ℹ️ That request isn't available anymore.", show_alert=True)
        return
    ctx = get_ctx()
    result = await order_svc.request_another_code(order, ctx.hero)
    if result == order_svc.ANOTHER_OK:
        order = await repo.get_order(order.id)
        await _rerender(call, order)
        await call.answer(f"⏳ Requested another code — {money(order.price)} held. Please wait.")
    elif result == order_svc.ANOTHER_INSUFFICIENT:
        await call.answer("💳 Not enough balance for another code. Top up first.", show_alert=True)
    else:
        await call.answer("⚠️ Could not request another code.", show_alert=True)


@router.callback_query(OrderAct.filter(F.action == "done"))
async def done_cb(call: CallbackQuery, callback_data: OrderAct) -> None:
    order = await _owned(call, callback_data.id)
    if order is None:
        return
    ctx = get_ctx()
    await order_svc.complete_order(order, ctx.hero)
    order = await repo.get_order(order.id)
    await _rerender(call, order)
    await call.answer("✅ Order completed. Thank you!")


async def _rerender(call: CallbackQuery, order: Order) -> None:
    try:
        await call.message.edit_text(format_order(order), reply_markup=order_keyboard(order))
    except Exception:  # noqa: BLE001 — message too old / not modified / deleted
        # Send a fresh card and re-point the live card so the poller keeps it synced.
        try:
            sent = await call.message.answer(format_order(order), reply_markup=order_keyboard(order))
            await repo.update_order(order.id, chat_id=sent.chat.id, message_id=sent.message_id)
        except Exception:  # noqa: BLE001
            pass
