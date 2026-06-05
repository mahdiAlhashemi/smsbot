"""Aiogram outer middleware: silently ignore updates from blocked users.

Registered as an outer middleware on the dispatcher so it runs before any
router/state handler. Blocking is rare, and a user PK lookup is cheap (and the
DB is WAL with a busy_timeout), so a per-update check is acceptable."""
from __future__ import annotations

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from db import repo


class BlockMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user is not None:
            u = await repo.get_user(user.id)
            if u is not None and u.is_blocked:
                return  # drop the update entirely
        return await handler(event, data)
