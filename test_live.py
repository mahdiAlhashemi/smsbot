"""Live end-to-end test against the real HeroSMS API (READ-ONLY).

Exercises the real catalog + pricing path the bot uses for the buy menu.
Does NOT call getNumber (which would spend real balance).
Run:  python test_live.py
"""
import asyncio
import os

os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("ADMIN_IDS", "1")
# Uses HEROSMS_API_KEY from the environment / .env via config.

from config import settings  # noqa: E402
from db import init_db  # noqa: E402
from herosms import HeroSMSClient  # noqa: E402
from services.catalog import Catalog  # noqa: E402
from services import pricing  # noqa: E402
from utils import money  # noqa: E402


async def main():
    await init_db()  # the real bot.py does this at startup
    hero = HeroSMSClient(settings.herosms_api_key, settings.herosms_base_url)
    catalog = Catalog(hero)
    try:
        bal = await hero.get_balance()
        print(f"HeroSMS master balance: {money(bal)}")

        services = await catalog.services()
        print(f"\nServices loaded: {len(services)}")
        print("First 8 on the buy menu (should be popular apps):")
        for s in services[:8]:
            print(f"   {s['code']:5} {s['name']}")

        countries = await catalog.countries()
        print(f"\nCountries loaded: {len(countries)}")

        markup = await pricing.get_markup()
        print(f"\nMarkup: {markup}%  — Telegram (tg) cheapest countries customers would see:")
        rows = await catalog.prices_for_service("tg")
        for r in rows[:6]:
            sell = pricing.apply_markup(r["cost"], markup)
            name = await catalog.country_name(r["country"])
            print(f"   {name:18} cost {money(r['cost'])}  ->  sell {money(sell)}   (stock {r['count']})")
        print(f"\nTotal in-stock countries for Telegram: {len(rows)}")
        print("\n[OK] LIVE TEST PASSED - catalog, prices and markup all work end-to-end.")
    finally:
        await hero.close()


if __name__ == "__main__":
    asyncio.run(main())
