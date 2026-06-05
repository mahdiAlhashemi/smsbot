"""Register all handler routers on the dispatcher."""
from __future__ import annotations

from aiogram import Dispatcher

from handlers import admin, buy, common, esim, orders, rent, wallet


def register_handlers(dp: Dispatcher) -> None:
    dp.include_router(common.router)
    dp.include_router(buy.router)
    dp.include_router(rent.router)
    dp.include_router(esim.router)
    dp.include_router(orders.router)
    dp.include_router(wallet.router)
    dp.include_router(admin.router)
