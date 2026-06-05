"""Process-wide singletons (bot, API clients, catalog), wired up in bot.py."""
from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot

from esim import EsimAccessClient
from herosms import HeroSMSClient
from services.catalog import Catalog
from services.payments import CryptoPay, Heleket


@dataclass
class AppContext:
    bot: Bot
    hero: HeroSMSClient
    catalog: Catalog
    # Active crypto payment provider (Heleket or CryptoPay), or None if disabled.
    payments: Heleket | CryptoPay | None
    # eSIM Access client, or None if eSIM credentials aren't configured.
    esim: EsimAccessClient | None = None
    # eSIM catalog cache (set in bot.py alongside `esim`).
    esim_catalog: "object | None" = None


_ctx: AppContext | None = None


def set_ctx(ctx: AppContext) -> None:
    global _ctx
    _ctx = ctx


def get_ctx() -> AppContext:
    if _ctx is None:
        raise RuntimeError("AppContext is not initialised yet")
    return _ctx
