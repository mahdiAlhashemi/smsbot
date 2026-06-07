"""Register all handler routers on the dispatcher."""
from __future__ import annotations

from aiogram import Dispatcher

from handlers import account, admin, buy, common, email, esim, fallback, orders, rent, wallet
from services.middleware import BlockMiddleware


def register_handlers(dp: Dispatcher) -> None:
    # Outer middleware: drop updates from blocked users before any handler.
    dp.update.outer_middleware(BlockMiddleware())
    dp.include_router(common.router)
    dp.include_router(buy.router)
    dp.include_router(rent.router)
    dp.include_router(esim.router)
    dp.include_router(email.router)
    dp.include_router(orders.router)
    dp.include_router(account.router)
    dp.include_router(wallet.router)
    dp.include_router(admin.router)
    # Fallback MUST be last so it can't shadow commands or FSM-state handlers.
    dp.include_router(fallback.router)
