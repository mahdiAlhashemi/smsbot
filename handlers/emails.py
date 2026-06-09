"""Email OTP flow: site → mail domain → confirm → buy (charge on receive)."""
from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from db import repo
from handlers.common import safe_edit
from keyboards.callbacks import (
    EmailAct, EmailBuy, EmailDomPage, EmailDomain, EmailSite, EmailSitePage, Nav,
)
from keyboards.menus import (
    back_button, email_confirm_keyboard, email_domains_keyboard,
    email_order_keyboard, email_sites_keyboard,
)
from services import emails as email_svc
from services import orders as order_svc
from services import pricing
from services.context import get_ctx
from utils import money

log = logging.getLogger(__name__)
router = Router(name="emails")


def _cat():
    return get_ctx().email_catalog


@router.callback_query(Nav.filter(F.to == "emails"))
async def open_emails(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if get_ctx().herov1 is None or _cat() is None:
        await safe_edit(call, "📧 <b>Email OTP</b> is not available right now.", back_button("main"))
        await call.answer()
        return
    await _show_sites(call, 0)


async def _show_sites(call: CallbackQuery, page: int) -> None:
    await call.answer()
    await safe_edit(
        call,
        "📧 <b>Email OTP</b>\n"
        "────────────────\n"
        "Get a disposable email address that receives the verification code for "
        "the website you choose. Charged only when the code arrives.\n\n"
        "👇 <b>Which website is the email for?</b>",
        email_sites_keyboard(_cat().sites(), page),
    )


@router.callback_query(EmailSitePage.filter())
async def page_sites(call: CallbackQuery, callback_data: EmailSitePage) -> None:
    await _show_sites(call, callback_data.page)


async def _priced_domains(site: str) -> list[dict]:
    domains = await _cat().domains_for(site)
    for d in domains:
        d["sell"] = await pricing.email_sell_price(d["cost"])
    return domains


@router.callback_query(EmailSite.filter())
async def show_domains(call: CallbackQuery, callback_data: EmailSite) -> None:
    await call.answer("Loading domains…")
    site = callback_data.code
    try:
        domains = await _priced_domains(site)
    except Exception:  # noqa: BLE001
        await safe_edit(call, "⚠️ Couldn't load mail domains. Please try again.", back_button("emails"))
        return
    if not domains:
        await safe_edit(
            call,
            f"😔 No mail domains available for <b>{email_svc.site_name(site)}</b> right now.\n"
            "Try another website.",
            back_button("emails"),
        )
        return
    await safe_edit(
        call,
        f"📧 <b>{email_svc.site_name(site)}</b> — choose a mail domain\n"
        "────────────────\n"
        "<i>domain • price (you're charged only when the code arrives)</i>",
        email_domains_keyboard(site, domains, 0),
    )


@router.callback_query(EmailDomPage.filter())
async def page_domains(call: CallbackQuery, callback_data: EmailDomPage) -> None:
    await call.answer()
    try:
        domains = await _priced_domains(callback_data.site)
    except Exception:  # noqa: BLE001
        await safe_edit(call, "⚠️ Couldn't load mail domains. Please try again.", back_button("emails"))
        return
    await safe_edit(
        call,
        f"📧 <b>{email_svc.site_name(callback_data.site)}</b> — choose a mail domain",
        email_domains_keyboard(callback_data.site, domains, callback_data.page),
    )


@router.callback_query(EmailDomain.filter())
async def confirm_email(call: CallbackQuery, callback_data: EmailDomain) -> None:
    await call.answer()
    site, name = callback_data.site, callback_data.domain
    dom = await _cat().domain(site, name)
    if dom is None:
        await safe_edit(call, "😔 That domain just changed. Pick another.", back_button("emails"))
        return
    price = await pricing.email_sell_price(dom["cost"])
    user = await repo.get_user(call.from_user.id)
    available = user.available if user else Decimal("0")
    can_afford = available >= price
    text = (
        "🧾 <b>Confirm email</b>\n"
        "────────────────\n"
        f"🌐 For: <b>{email_svc.site_name(site)}</b>\n"
        f"✉️ Domain: <b>{name}</b>\n"
        "────────────────\n"
        f"💵 Price: <b>{money(price)}</b>\n"
        f"💰 Available: <b>{money(available)}</b>\n\n"
        "<i>⚡ You get an email address now; you're charged only when the "
        "verification code arrives. No code → no charge.</i>"
    )
    if not can_afford:
        text += f"\n\n⚠️ <b>Not enough balance</b> — you need <b>{money(price - available)}</b> more."
    await safe_edit(call, text, email_confirm_keyboard(site, name, can_afford=can_afford))


@router.callback_query(EmailBuy.filter())
async def buy_email(call: CallbackQuery, callback_data: EmailBuy) -> None:
    site, name = callback_data.site, callback_data.domain
    await call.answer("Reserving your email…")
    ctx = get_ctx()
    dom = await _cat().domain(site, name)
    if dom is None:
        await safe_edit(call, "😔 That domain just changed. Pick another.", back_button("emails"))
        return
    try:
        order = await email_svc.email_purchase(call.from_user.id, site, name, dom["cost"], ctx.herov1)
    except order_svc.InsufficientFunds:
        price = await pricing.email_sell_price(dom["cost"])
        await safe_edit(
            call,
            f"💸 <b>Not enough balance</b>\n\n💵 Price: <b>{money(price)}</b>\n\n💳 Top up and try again.",
            back_button("wallet"),
        )
        return
    except order_svc.PurchaseError as exc:
        await safe_edit(call, f"⚠️ {exc.user_message}", back_button("emails"))
        return
    await repo.update_order(order.id, chat_id=call.message.chat.id, message_id=call.message.message_id)
    order = await repo.get_order(order.id)
    await safe_edit(call, email_svc.format_email_card(order), email_order_keyboard(order))


async def _owned(call: CallbackQuery, order_id: int):
    order = await repo.get_order(order_id)
    if order is None or order.user_id != call.from_user.id:
        await call.answer("❌ Order not found.", show_alert=True)
        return None
    return order


@router.callback_query(EmailAct.filter(F.action == "cancel"))
async def cancel_email_cb(call: CallbackQuery, callback_data: EmailAct) -> None:
    order = await _owned(call, callback_data.id)
    if order is None:
        return
    ok = await email_svc.cancel_email(order, get_ctx().herov1)
    order = await repo.get_order(order.id)
    await safe_edit(call, email_svc.format_email_card(order), email_order_keyboard(order))
    await call.answer(
        f"✅ Cancelled — not charged. {money(order.price)} released." if ok
        else "⚠️ Can't cancel now — a code may have arrived.",
        show_alert=True,
    )


@router.callback_query(EmailAct.filter(F.action == "another"))
async def another_email_cb(call: CallbackQuery, callback_data: EmailAct) -> None:
    order = await _owned(call, callback_data.id)
    if order is None:
        return
    if order.status != order_svc.Order.RECEIVED:
        await call.answer("ℹ️ That isn't available anymore.", show_alert=True)
        return
    result = await email_svc.reorder_email(order, get_ctx().herov1)
    if result == order_svc.ANOTHER_OK:
        order = await repo.get_order(order.id)
        await safe_edit(call, email_svc.format_email_card(order), email_order_keyboard(order))
        await call.answer(f"⏳ Requested another email — {money(order.price)} held.")
    elif result == order_svc.ANOTHER_INSUFFICIENT:
        await call.answer("💳 Not enough balance. Top up first.", show_alert=True)
    else:
        await call.answer("⚠️ Could not request another email.", show_alert=True)


@router.callback_query(EmailAct.filter(F.action == "done"))
async def done_email_cb(call: CallbackQuery, callback_data: EmailAct) -> None:
    order = await _owned(call, callback_data.id)
    if order is None:
        return
    await email_svc.complete_email(order, get_ctx().herov1)
    order = await repo.get_order(order.id)
    await safe_edit(call, email_svc.format_email_card(order), email_order_keyboard(order))
    await call.answer("✅ Done. Thank you!")
