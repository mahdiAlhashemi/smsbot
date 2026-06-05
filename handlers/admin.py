"""Admin panel: stats, manual top-up, markup, broadcast."""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import settings
from db import repo
from handlers.common import is_admin, safe_edit
from handlers.states import AdminFlow
from keyboards.callbacks import AdminAct, Nav
from keyboards.menus import admin_keyboard, back_button
from services import pricing
from utils import money

log = logging.getLogger(__name__)
router = Router(name="admin")


def _guard(user_id: int) -> bool:
    return is_admin(user_id)


@router.callback_query(Nav.filter(F.to == "admin"))
async def open_admin(call: CallbackQuery, state: FSMContext) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Not authorised.", show_alert=True)
        return
    await state.clear()
    markup = await pricing.get_markup()
    premium = await pricing.get_bid_premium()
    esim_comm = await pricing.get_esim_commission()
    min_bid = settings.min_bid
    min_line = f"\n🧱 Min bid: <b>{money(min_bid)}</b>" if min_bid and min_bid > 0 else ""
    esim_line = (
        f"\n📶 eSIM commission: <b>{esim_comm}%</b>" if settings.esim_enabled else ""
    )
    await safe_edit(
        call,
        "🛠 <b>Admin panel</b>\n\n"
        "<b>Smart pricing</b>\n"
        f"🎯 Bid premium: <b>{premium}%</b> <i>(paid above floor to win numbers)</i>\n"
        f"📈 SMS commission: <b>{markup}%</b> <i>(your profit on top)</i>"
        f"{esim_line}"
        f"{min_line}\n\n"
        "<i>SMS: customer pays (default + bid premium) + commission. eSIM: cost + "
        "eSIM commission. Your commission is always kept.</i>\n\n"
        f"💳 Payments: {'on' if settings.payments_enabled else 'off'}",
        admin_keyboard(),
    )
    await call.answer()


@router.callback_query(AdminAct.filter(F.action == "stats"))
async def admin_stats(call: CallbackQuery) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Not authorised.", show_alert=True)
        return
    s = await repo.admin_stats()
    hero_balance = "?"
    try:
        from services.context import get_ctx

        hero_balance = money(await get_ctx().hero.get_balance())
    except Exception:  # noqa: BLE001
        pass
    text = (
        "📊 <b>Statistics</b>\n\n"
        f"👥 Users: <b>{s['users']}</b>\n"
        f"👛 Customer balances: <b>{money(s['balances'])}</b>\n"
        f"✅ Completed orders: <b>{s['completed']}</b>\n"
        f"⏳ Open orders: <b>{s['open_orders']}</b>\n\n"
        f"💰 Sold (gross): <b>{money(s['sold'])}</b>\n"
        f"🧾 HeroSMS cost: <b>{money(s['cost'])}</b>\n"
        f"📈 Profit: <b>{money(s['profit'])}</b>\n\n"
        f"🦸 HeroSMS account balance: <b>{hero_balance}</b>"
    )
    await safe_edit(call, text, back_button("admin"))
    await call.answer()


@router.callback_query(AdminAct.filter(F.action == "give"))
async def admin_give_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Not authorised.", show_alert=True)
        return
    await state.set_state(AdminFlow.give)
    await safe_edit(
        call,
        "💵 <b>Give balance</b>\n\nSend: <code>user_id amount</code>\n"
        "Example: <code>123456789 5.00</code>\n(use a negative amount to deduct)",
        back_button("admin"),
    )
    await call.answer()


@router.message(AdminFlow.give, F.text)
async def admin_give(message: Message, state: FSMContext) -> None:
    if not _guard(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Format: <code>user_id amount</code>")
        return
    try:
        target = int(parts[0])
        amount = Decimal(parts[1].replace(",", "."))
    except (ValueError, InvalidOperation):
        await message.answer("Invalid input. Format: <code>user_id amount</code>")
        return
    user = await repo.get_user(target)
    if user is None:
        await message.answer("That user has not started the bot yet.")
        return
    new_bal = await repo.credit(target, amount)
    await state.clear()
    await message.answer(f"✅ Done. New balance of <code>{target}</code>: <b>{money(new_bal)}</b>")
    try:
        verb = "added to" if amount >= 0 else "deducted from"
        await message.bot.send_message(
            target, f"💼 {money(abs(amount))} was {verb} your balance.\nNew balance: {money(new_bal)}"
        )
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(AdminAct.filter(F.action == "markup"))
async def admin_markup_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Not authorised.", show_alert=True)
        return
    current = await pricing.get_markup()
    await state.set_state(AdminFlow.markup)
    await safe_edit(
        call,
        f"📈 <b>Set commission</b>\n\nYour profit %, added on top of the bid ceiling.\n"
        f"Current: <b>{current}%</b>\n\nSend a new percentage, e.g. <code>20</code>:",
        back_button("admin"),
    )
    await call.answer()


@router.message(AdminFlow.markup, F.text)
async def admin_markup(message: Message, state: FSMContext) -> None:
    if not _guard(message.from_user.id):
        return
    try:
        value = Decimal(message.text.strip().replace("%", "").replace(",", "."))
    except InvalidOperation:
        await message.answer("Send a number, e.g. 20")
        return
    if value < 0 or value > 1000:
        await message.answer("Commission must be between 0 and 1000%.")
        return
    await pricing.set_markup(value)
    await state.clear()
    await message.answer(f"✅ Commission set to <b>{value}%</b>.")


@router.callback_query(AdminAct.filter(F.action == "bid"))
async def admin_bid_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Not authorised.", show_alert=True)
        return
    current = await pricing.get_bid_premium()
    await state.set_state(AdminFlow.bid)
    await safe_edit(
        call,
        "🎯 <b>Set bid premium</b>\n\n"
        "How much ABOVE the provider's default/floor price the bot bids to win a "
        "number. HeroSMS is a demand auction — a higher bid gets numbers faster "
        "(this is what the website does).\n\n"
        f"Current: <b>{current}%</b>\n\n"
        "Send a new percentage. Examples:\n"
        "• <code>10</code> — small premium (cheap, but loses high-demand numbers)\n"
        "• <code>200</code> — bids 3× the floor (wins most numbers)\n"
        "• <code>1000</code> — bids 11× the floor (wins almost anything)\n\n"
        "<i>Your commission is added on top of whatever you bid, so a higher "
        "premium never eats your profit — it just raises the customer price.</i>",
        back_button("admin"),
    )
    await call.answer()


@router.message(AdminFlow.bid, F.text)
async def admin_bid(message: Message, state: FSMContext) -> None:
    if not _guard(message.from_user.id):
        return
    try:
        value = Decimal(message.text.strip().replace("%", "").replace(",", "."))
    except InvalidOperation:
        await message.answer("Send a number, e.g. 200")
        return
    if value < 0 or value > 100000:
        await message.answer("Bid premium must be between 0 and 100000%.")
        return
    await pricing.set_bid_premium(value)
    await state.clear()
    await message.answer(
        f"✅ Bid premium set to <b>{value}%</b>.\n\n"
        "The bot will now bid that much above the floor price to win numbers."
    )


@router.callback_query(AdminAct.filter(F.action == "esimcomm"))
async def admin_esim_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Not authorised.", show_alert=True)
        return
    current = await pricing.get_esim_commission()
    await state.set_state(AdminFlow.esim_comm)
    await safe_edit(
        call,
        "📶 <b>Set eSIM commission</b>\n\nYour profit % added on top of the eSIM "
        f"wholesale cost.\nCurrent: <b>{current}%</b>\n\nSend a new percentage, e.g. <code>10</code>:",
        back_button("admin"),
    )
    await call.answer()


@router.message(AdminFlow.esim_comm, F.text)
async def admin_esim(message: Message, state: FSMContext) -> None:
    if not _guard(message.from_user.id):
        return
    try:
        value = Decimal(message.text.strip().replace("%", "").replace(",", "."))
    except InvalidOperation:
        await message.answer("Send a number, e.g. 10")
        return
    if value < 0 or value > 1000:
        await message.answer("eSIM commission must be between 0 and 1000%.")
        return
    await pricing.set_esim_commission(value)
    await state.clear()
    await message.answer(f"✅ eSIM commission set to <b>{value}%</b>.")


@router.callback_query(AdminAct.filter(F.action == "broadcast"))
async def admin_broadcast_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Not authorised.", show_alert=True)
        return
    await state.set_state(AdminFlow.broadcast)
    await safe_edit(
        call,
        "📣 <b>Broadcast</b>\n\nSend the message text to deliver to all users.",
        back_button("admin"),
    )
    await call.answer()


@router.callback_query(AdminAct.filter(F.action == "channelpost"))
async def admin_channelpost_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _guard(call.from_user.id):
        await call.answer("Not authorised.", show_alert=True)
        return
    if not settings.post_channel:
        await safe_edit(
            call,
            "📢 <b>Post to channel</b>\n\n⚠️ No channel is set yet.\n\n"
            "1. Create a channel in Telegram.\n"
            "2. Add <b>@TheNumberHubBot</b> as an <b>admin</b> (with 'Post messages').\n"
            "3. Set <code>POST_CHANNEL</code> in the bot's .env to the channel @username "
            "(or its -100… id), then restart.\n\n"
            "Then come back here to post.",
            back_button("admin"),
        )
        await call.answer()
        return
    await state.set_state(AdminFlow.channelpost)
    await safe_edit(
        call,
        f"📢 <b>Post to channel</b> ({settings.post_channel})\n\n"
        "Send the message (text/HTML) to publish to the channel:",
        back_button("admin"),
    )
    await call.answer()


@router.message(AdminFlow.channelpost, F.text)
async def admin_channelpost(message: Message, state: FSMContext) -> None:
    if not _guard(message.from_user.id):
        return
    await state.clear()
    try:
        sent = await message.bot.send_message(settings.post_channel, message.html_text)
        link = ""
        if str(settings.post_channel).startswith("@"):
            link = f"\nhttps://t.me/{settings.post_channel[1:]}/{sent.message_id}"
        await message.answer(f"✅ Posted to {settings.post_channel}.{link}")
    except Exception as exc:  # noqa: BLE001
        await message.answer(
            f"⚠️ Could not post: {exc}\n\n"
            "Make sure the bot is an <b>admin</b> of the channel with 'Post messages', "
            "and <code>POST_CHANNEL</code> is the correct @username or -100… id."
        )


@router.message(AdminFlow.broadcast, F.text)
async def admin_broadcast(message: Message, state: FSMContext) -> None:
    if not _guard(message.from_user.id):
        return
    await state.clear()
    ids = await repo.get_all_user_ids()
    await message.answer(f"Sending to {len(ids)} users…")
    sent = failed = 0
    for uid in ids:
        try:
            await message.bot.send_message(uid, message.html_text)
            sent += 1
        except Exception:  # noqa: BLE001
            failed += 1
        await asyncio.sleep(0.05)  # stay under Telegram rate limits
    await message.answer(f"✅ Broadcast done. Sent: {sent}, failed: {failed}.")
