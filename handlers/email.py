"""Temp email inbox: create a disposable inbox, receive verification emails.
Charge-on-receive — the fee is held and charged only when the first email lands.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from config import settings
from db import repo
from handlers.common import safe_edit
from keyboards.callbacks import EmailAct, EmailBuy, Nav
from keyboards.menus import back_button, email_buy_keyboard, email_order_keyboard
from services import email_inbox as email_svc
from services.orders import InsufficientFunds, PurchaseError
from utils import money

log = logging.getLogger(__name__)
router = Router(name="email")


@router.callback_query(Nav.filter(F.to == "email"))
async def open_email(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not settings.temp_email_enabled:
        await safe_edit(call, "📧 <b>Temp Email</b> is not available right now.", back_button("main"))
        await call.answer()
        return
    await safe_edit(
        call,
        "📧 <b>Temp email inbox</b>\n"
        "────────────────\n"
        "Get a disposable inbox to receive verification emails — perfect alongside an OTP number.\n\n"
        f"💳 Fee: <b>{money(settings.temp_email_price)}</b> — <i>charged only when the first email arrives.</i>\n"
        f"⏳ Inbox lives for <b>{settings.temp_email_window_min} min</b>.\n\n"
        "👇 Tap to create one.",
        email_buy_keyboard(),
    )
    await call.answer()


@router.callback_query(EmailBuy.filter())
async def buy_email(call: CallbackQuery, callback_data: EmailBuy) -> None:
    if not settings.temp_email_enabled:
        await call.answer("Not available.", show_alert=True)
        return
    await call.answer("Creating inbox…")
    try:
        order = await email_svc.email_purchase(call.from_user.id)
    except InsufficientFunds:
        await safe_edit(
            call,
            "💳 <b>Not enough balance</b>\n\n"
            f"The fee is <b>{money(settings.temp_email_price)}</b> (held, charged only on the first email).\n\n"
            "💳 Top up and try again.",
            back_button("wallet"),
        )
        return
    except PurchaseError:
        await safe_edit(call, "⚠️ Couldn't create an inbox right now — please try again.", back_button("main"))
        return
    await repo.update_order(order.id, chat_id=call.message.chat.id, message_id=call.message.message_id)
    order = await repo.get_order(order.id)
    await safe_edit(call, email_svc.format_email_card(order), email_order_keyboard(order))


@router.callback_query(EmailAct.filter(F.action == "cancel"))
async def cancel_email_cb(call: CallbackQuery, callback_data: EmailAct) -> None:
    order = await repo.get_order(callback_data.id)
    if order is None or order.user_id != call.from_user.id:
        await call.answer("❌ Inbox not found.", show_alert=True)
        return
    ok = await email_svc.cancel_email(order)
    order = await repo.get_order(order.id)
    try:
        await call.message.edit_text(
            email_svc.format_email_card(order),
            reply_markup=email_order_keyboard(order),
            disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001
        pass
    await call.answer("✅ Inbox closed — not charged." if ok else "ℹ️ Can't close now.", show_alert=True)
