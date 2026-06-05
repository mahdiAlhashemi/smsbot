"""Catch-all for unrecognised free text — nudge the user back to the menu.

MUST be registered LAST so it never shadows command handlers or FSM-state
handlers (those live in routers registered earlier and match first)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message

from config import settings
from handlers.common import is_admin
from keyboards.menus import main_menu

router = Router(name="fallback")


@router.message(F.text)
async def unknown_text(message: Message) -> None:
    await message.answer(
        "🤔 I didn't catch that. Use the menu below 👇",
        reply_markup=main_menu(
            is_admin(message.from_user.id), settings.payments_enabled, settings.esim_enabled
        ),
    )
