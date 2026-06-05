"""HeroSMS reseller Telegram bot — entrypoint."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent

from config import settings
from db import init_db
from esim import EsimAccessClient
from handlers import register_handlers
from herosms import HeroSMSClient
from services.catalog import Catalog
from services.context import AppContext, set_ctx
from services.esim import EsimCatalog
from services.payments import CryptoPay, Heleket
from services.pollers import start_pollers

log = logging.getLogger(__name__)

# ─── Bot profile branding (NumberHub) ───────────────────────────────────────
BOT_NAME = "NumberHub — Numbers & eSIM"
BOT_SHORT_DESCRIPTION = (
    "Virtual numbers for SMS codes (800+ apps) + travel eSIM data in 219 "
    "destinations. Pay only when it works."
)
BOT_DESCRIPTION = (
    "📱 NumberHub — your all-in-one hub for virtual numbers and travel data.\n\n"
    "📲 SMS numbers — receive codes for 800+ apps (Telegram, WhatsApp, OpenAI, "
    "Google…) across 190+ countries. Pay only when the code arrives.\n"
    "📱 Rent a number — keep it for days or weeks and get all its SMS.\n"
    "📶 eSIM data plans — instant QR-code eSIM for 219 destinations.\n\n"
    "✅ Crypto top-up · instant delivery · auto-refund if no code\n\n"
    "Tap Start 👇"
)


_BENIGN_ERRORS = ("query is too old", "message is not modified", "message to edit not found",
                  "message can't be edited", "MESSAGE_ID_INVALID")


async def _on_error(event: ErrorEvent) -> bool:
    """Swallow benign Telegram errors (stale buttons, no-op edits); log the rest
    concisely without dumping a full traceback for every transient issue."""
    exc = event.exception
    if isinstance(exc, TelegramBadRequest) and any(s in str(exc) for s in _BENIGN_ERRORS):
        return True
    log.error("update handling error: %s: %s", type(exc).__name__, exc)
    return True


async def _apply_branding(bot: Bot) -> None:
    """Set the bot's public name/description. Telegram rate-limits these, so
    failures are non-fatal (they just mean it was set recently)."""
    for label, coro in (
        ("name", bot.set_my_name(name=BOT_NAME)),
        ("short_description", bot.set_my_short_description(short_description=BOT_SHORT_DESCRIPTION)),
        ("description", bot.set_my_description(description=BOT_DESCRIPTION)),
    ):
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            log.warning("could not set bot %s: %s", label, exc)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    await init_db()

    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    register_handlers(dp)
    dp.errors.register(_on_error)

    await bot.set_my_commands([
        BotCommand(command="start", description="Open the bot / main menu"),
        BotCommand(command="menu", description="Main menu"),
        BotCommand(command="balance", description="Show your balance"),
        BotCommand(command="help", description="How it works"),
    ])
    await _apply_branding(bot)

    hero = HeroSMSClient(settings.herosms_api_key, settings.herosms_base_url)
    catalog = Catalog(hero)
    # Pick the crypto payment provider: Heleket preferred, then CryptoBot.
    if settings.heleket_enabled:
        payments = Heleket(settings.heleket_merchant, settings.heleket_api_key)
    elif settings.cryptobot_token:
        payments = CryptoPay(settings.cryptobot_token, settings.cryptobot_testnet,
                             settings.cryptobot_asset)
    else:
        payments = None
    # eSIM Access (optional) — data-plan eSIMs.
    if settings.esim_enabled:
        esim = EsimAccessClient(
            settings.esim_access_code, settings.esim_secret_key, settings.esim_base_url
        )
        esim_catalog = EsimCatalog(esim)
    else:
        esim = esim_catalog = None
    set_ctx(AppContext(
        bot=bot, hero=hero, catalog=catalog, payments=payments,
        esim=esim, esim_catalog=esim_catalog,
    ))

    # Connectivity sanity checks (non-fatal).
    try:
        log.info("HeroSMS connected. Master account balance: %s", await hero.get_balance())
    except Exception as exc:  # noqa: BLE001
        log.warning("HeroSMS balance check failed: %s", exc)
    if payments is not None:
        try:
            await payments.verify()
            log.info("Payments: %s connected.", payments.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("Payments (%s) check failed: %s", payments.name, exc)
    else:
        log.info("Crypto payments disabled. Admin manual top-up still works.")
    if esim is not None:
        try:
            log.info("eSIM Access connected. Merchant balance: $%s", await esim.balance())
        except Exception as exc:  # noqa: BLE001
            log.warning("eSIM Access check failed: %s", exc)

    tasks = start_pollers()
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for t in tasks:
            t.cancel()
        await hero.close()
        if payments is not None:
            await payments.close()
        if esim is not None:
            await esim.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped.")
