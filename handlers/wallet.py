"""Wallet: balance, crypto top-up via Crypto Pay (@CryptoBot)."""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import settings
from db import repo
from db.models import Payment
from handlers.common import safe_edit
from handlers.states import WalletFlow
from keyboards.callbacks import Nav, PayCheck, TopupPick
from keyboards.menus import (
    back_button,
    payment_keyboard,
    topup_amounts_keyboard,
    wallet_keyboard,
)
from services.context import get_ctx
from utils import money

log = logging.getLogger(__name__)
router = Router(name="wallet")


@router.callback_query(Nav.filter(F.to == "wallet"))
async def open_wallet(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user = await repo.get_user(call.from_user.id)
    bal = user.balance if user else Decimal("0")
    text = [f"👛 <b>Wallet</b>\n\nBalance: <b>{money(bal)}</b>"]
    if user and user.held and user.held > 0:
        text.append(f"On hold (open orders): {money(user.held)}")
        text.append(f"Available to spend: <b>{money(user.available)}</b>")
    if not settings.payments_enabled:
        text.append("\n<i>Automatic top-up is disabled. Ask an admin to add funds.</i>")
    await safe_edit(call, "\n".join(text), wallet_keyboard(settings.payments_enabled))
    await call.answer()


@router.callback_query(TopupPick.filter(F.amount == "menu"))
async def topup_menu(call: CallbackQuery) -> None:
    if not settings.payments_enabled:
        await call.answer("Top-up is disabled.", show_alert=True)
        return
    blurb = ["➕ <b>Top up</b>\n\nChoose an amount (in USD, paid in USDT):"]
    from services.billing import _parse_tiers
    tiers = _parse_tiers(settings.topup_bonus_tiers)
    if tiers:
        ladder = " · ".join(f"+{p}% over {money(t)}" for t, p in sorted(tiers))
        blurb.append(f"\n🎁 <b>Deposit bonus:</b> {ladder}")
    if settings.topup_first_bonus_pct > 0:
        blurb.append(f"✨ <b>First top-up boosted</b> — up to "
                     f"{settings.topup_bonus_max_pct}% bonus!")
    await safe_edit(call, "\n".join(blurb), topup_amounts_keyboard())
    await call.answer()


@router.callback_query(TopupPick.filter(F.amount == "custom"))
async def topup_custom(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WalletFlow.custom_amount)
    await safe_edit(
        call,
        f"✏️ Enter the amount to top up in USD (minimum {money(settings.min_topup)}):",
        back_button("wallet"),
    )
    await call.answer()


@router.message(WalletFlow.custom_amount, F.text)
async def topup_custom_amount(message: Message, state: FSMContext) -> None:
    raw = message.text.strip().replace(",", ".").lstrip("$")
    try:
        amount = Decimal(raw).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        await message.answer("Please send a valid number, e.g. 7.50")
        return
    if amount < settings.min_topup:
        await message.answer(f"Minimum top-up is {money(settings.min_topup)}.")
        return
    if amount > Decimal("10000"):
        await message.answer("That amount is too large.")
        return
    await state.clear()
    await _create_invoice(message, message.from_user.id, amount)


@router.callback_query(TopupPick.filter())
async def topup_preset(call: CallbackQuery, callback_data: TopupPick) -> None:
    # Reaches here only for numeric presets (menu/custom handled above).
    try:
        amount = Decimal(callback_data.amount).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        await call.answer("Invalid amount.", show_alert=True)
        return
    await call.answer("Creating invoice…")
    await _create_invoice(call.message, call.from_user.id, amount, edit_call=call)


async def _create_invoice(message: Message, user_id: int, amount: Decimal, edit_call: CallbackQuery | None = None) -> None:
    ctx = get_ctx()
    provider = ctx.payments
    if provider is None:
        await message.answer("Top-up is currently disabled.")
        return

    # Create the payment row first so its id can be the (unique) order_id.
    payment = await repo.create_payment(
        user_id=user_id, provider=provider.name, invoice_id="", amount=amount, asset="crypto",
    )
    try:
        inv = await provider.make_invoice(amount, order_id=f"nh{payment.id}")
    except Exception as exc:  # noqa: BLE001
        log.warning("create invoice (%s) failed: %s", provider.name, exc)
        await repo.expire_payment(payment.id)
        if "not active" in str(exc).lower():
            msg = ("⏳ Crypto top-up is being activated (the payment provider is "
                   "reviewing the account). It'll work very soon — please try again later, "
                   "or ask an admin to add funds for now.")
        else:
            msg = "⚠️ Could not create the invoice. Please try again later."
        await message.answer(msg)
        return

    await repo.set_payment_invoice(payment.id, inv["invoice_id"])
    pay_url = inv["pay_url"]
    text = (
        "🧾 <b>Invoice created</b>\n\n"
        f"Amount: <b>{money(amount)}</b> (pay in <b>USDT</b>)\n\n"
        "Tap <b>Pay now</b>, pay with <b>USDT</b>, then press <b>I have paid</b>.\n"
        "<i>Your balance is also credited automatically within ~1 minute.</i>"
    )
    kb = payment_keyboard(pay_url, payment.id)
    if edit_call is not None:
        await safe_edit(edit_call, text, kb)
    else:
        await message.answer(text, reply_markup=kb)


@router.callback_query(PayCheck.filter())
async def check_payment(call: CallbackQuery, callback_data: PayCheck) -> None:
    ctx = get_ctx()
    payment = await repo.get_payment(callback_data.id)
    if payment is None or payment.user_id != call.from_user.id:
        await call.answer("Payment not found.", show_alert=True)
        return
    if payment.status == Payment.PAID:
        await call.answer("Already credited ✅")
        return
    provider = ctx.payments
    if provider is None:
        await call.answer("Payments disabled.", show_alert=True)
        return
    try:
        status = await provider.invoice_status(payment.invoice_id, f"nh{payment.id}")
    except Exception:  # noqa: BLE001
        await call.answer("Could not check right now, try again.", show_alert=True)
        return
    if status == "paid":
        if await repo.mark_payment_paid(payment.id):
            from services import billing
            new_bal, bonus = await billing.credit_topup(payment.user_id, payment.amount)
            bonus_line = f"\n🎁 Bonus: <b>+{money(bonus)}</b>" if bonus > 0 else ""
            await safe_edit(
                call,
                f"✅ <b>Payment received!</b>\n\n{money(payment.amount)} added.{bonus_line}\n"
                f"New balance: <b>{money(new_bal)}</b>",
                wallet_keyboard(settings.payments_enabled),
            )
            await call.answer("Balance updated ✅")
        else:
            await call.answer("Already credited ✅")
    else:
        await call.answer("Not paid yet. Complete the payment, then retry.", show_alert=True)
