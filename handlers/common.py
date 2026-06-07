"""/start, main menu, help, and shared rendering helpers."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import settings
from db import repo
from keyboards.callbacks import Nav
from keyboards.menus import back_button, main_menu
from utils import money

log = logging.getLogger(__name__)
router = Router(name="common")


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_id_list


async def safe_edit(call: CallbackQuery, text: str, reply_markup=None) -> None:
    """Edit the message a callback came from; fall back to sending a new one."""
    try:
        await call.message.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001 — message unchanged / too old / not editable
        try:
            await call.message.answer(text, reply_markup=reply_markup, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            pass


async def main_menu_text(user_id: int) -> str:
    user = await repo.get_user(user_id)
    rule = "─" * 16
    bal_block = f"💰 Balance: <b>{money(user.balance if user else 0)}</b>"
    if user and user.held and user.held > 0:
        bal_block += (
            f"\n🔒 On hold: {money(user.held)}"
            f"\n✅ Available: <b>{money(user.available)}</b>"
        )
    contact_lines = []
    if settings.support_contact:
        contact_lines.append(f"💬 Support: {settings.support_contact}")
    if settings.channel_username:
        contact_lines.append(f"📢 Channel: {settings.channel_username}")
    if settings.website_url.strip():
        site = settings.website_url.strip().replace("https://", "").replace("http://", "").rstrip("/")
        contact_lines.append(f"🌐 {site}")
    contact = ("\n\n" + rule + "\n" + "\n".join(contact_lines)) if contact_lines else ""
    return (
        "📱  <b>NumberHub</b> — Numbers &amp; eSIM\n"
        f"{rule}\n"
        f"{bal_block}\n\n"
        "Buy a temporary number to receive <b>OTP codes</b> for "
        "<b>800+</b> services across <b>190+</b> countries.\n\n"
        "<i>⚡ You only pay when the code actually arrives.</i>"
        f"{contact}\n\n"
        "👇  <b>Choose an option below</b>"
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, command: CommandObject) -> None:
    await state.clear()
    admin = is_admin(message.from_user.id)
    _user, created = await repo.get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
        admin,
    )
    # Referral attribution: only on first /start, only via ref_<uid> deep link.
    payload = (command.args or "").strip()
    if created and payload.startswith("ref_"):
        try:
            await repo.set_referrer(message.from_user.id, int(payload[4:]))
        except ValueError:
            pass
    await message.answer(
        await main_menu_text(message.from_user.id),
        reply_markup=main_menu(admin, settings.payments_enabled, settings.esim_enabled),
        disable_web_page_preview=True,
    )


@router.callback_query(Nav.filter(F.to == "main"))
async def nav_main(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(
        call,
        await main_menu_text(call.from_user.id),
        main_menu(is_admin(call.from_user.id), settings.payments_enabled, settings.esim_enabled),
    )
    await call.answer()


HELP_TEXT = (
    "ℹ️ <b>How it works</b>\n\n"
    "1. <b>Top up</b> your wallet in the 👛 Wallet section.\n"
    "2. Tap 📲 <b>Buy number</b>, choose a service (Telegram, WhatsApp, …) and a country.\n"
    "3. You get a phone number. Enter it on the website/app you are registering on.\n"
    "4. The OTP code is delivered to you here automatically.\n\n"
    "📲 <b>Activation</b>\n"
    "• Receive codes from the chosen service for <b>20 minutes</b>.\n"
    "• Cancellation available <b>after 2 minutes</b>.\n"
    "• If no code is received, funds return to your balance.\n\n"
    "📱 <b>Rent</b>\n"
    "• Receive codes for the whole rental period (from 24 hours).\n"
    "• Cancellation (full refund) available <b>after 2 min and no later than 20 min</b>.\n\n"
    "• One number = one service. You only pay an activation when the code arrives."
)


def help_text() -> str:
    t = HELP_TEXT
    site = settings.website_url.strip()
    if site:
        t += (
            "\n\n🔒 <b>Trust & safety</b>\n"
            "• No KYC for ordinary use — your SMS codes are deleted after delivery.\n"
            "• Charged only when a code is delivered — no code, no charge, auto-refund.\n"
            "• Crypto wallet — no card or bank details.\n"
            f"• Terms, Privacy & Refund policy: {site}"
        )
    if settings.support_url:
        return t + "\n\n💬 Need help? Tap <b>Contact support</b> below."
    return t + "\n\nNeed help? Contact the bot administrator."


def help_keyboard():
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    b = InlineKeyboardBuilder()
    if settings.support_url:
        b.button(text="💬 Contact support", url=settings.support_url)
    if settings.channel_url:
        b.button(text="📢 Our channel", url=settings.channel_url)
    site = settings.website_url.strip()
    if site:
        b.button(text="🌐 Website", url=site)
        b.button(text="📜 Terms & policies", url=f"{site.rstrip('/')}/terms/")
    b.button(text="⬅️ Back", callback_data=Nav(to="main"))
    b.adjust(1)
    return b.as_markup()


@router.callback_query(Nav.filter(F.to == "help"))
async def nav_help(call: CallbackQuery) -> None:
    await safe_edit(call, help_text(), help_keyboard())
    await call.answer()


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        await main_menu_text(message.from_user.id),
        reply_markup=main_menu(is_admin(message.from_user.id), settings.payments_enabled, settings.esim_enabled),
        disable_web_page_preview=True,
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(help_text(), reply_markup=help_keyboard(), disable_web_page_preview=True)


@router.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    user = await repo.get_user(message.from_user.id)
    bal = user.balance if user else 0
    await message.answer(
        f"👛 Your balance: <b>{money(bal)}</b>",
        reply_markup=main_menu(is_admin(message.from_user.id), settings.payments_enabled, settings.esim_enabled),
    )
