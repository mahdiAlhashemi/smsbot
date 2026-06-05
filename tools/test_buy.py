"""Real end-to-end purchase test against HeroSMS (spends real balance ONLY if a
code is received and confirmed; cancelling before a code costs nothing).

Usage:  python tools/test_buy.py [service] [wait_minutes]
        service default 'tg' (Telegram), wait default 10 min.
Buys the cheapest in-stock number for the service, prints it, waits for an SMS,
then completes (if a code arrives) or cancels+refunds (if not).
"""
import asyncio
import os
import sys
import time

# Make the project root importable when running this from tools/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("ADMIN_IDS", "1")

from config import settings  # noqa: E402
from herosms import HeroSMSClient, HeroSMSError, NoNumbersError  # noqa: E402

SERVICE = sys.argv[1] if len(sys.argv) > 1 else "tg"
COUNTRY = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].lower() != "cheapest" else None
WAIT_MIN = int(sys.argv[3]) if len(sys.argv) > 3 else 10


async def main():
    hero = HeroSMSClient(settings.herosms_api_key, settings.herosms_base_url)
    try:
        rows = await hero.country_prices_for_service(SERVICE)
        if not rows:
            print(f"No stock for service '{SERVICE}'.", flush=True)
            return
        if COUNTRY is not None:
            chosen = next((r for r in rows if r["country"] == COUNTRY), None)
            if chosen is None:
                print(f"No stock for '{SERVICE}' in country {COUNTRY}.", flush=True)
                return
        else:
            chosen = rows[0]
        cheapest = chosen
        country, cost = cheapest["country"], cheapest["cost"]
        bal0 = await hero.get_balance()
        print(f"Balance before: ${bal0}", flush=True)
        print(f"Buying '{SERVICE}' in country {country} (HeroSMS cost ~${cost}, stock {cheapest['count']})...", flush=True)

        act = await hero.get_number(SERVICE, country)
        print("=" * 40, flush=True)
        print(f"NUMBER:  +{act.phone}", flush=True)
        print(f"activation_id: {act.id}   cost: ${act.cost}", flush=True)
        print("=" * 40, flush=True)
        print(f"Use this number on the service now. Waiting up to {WAIT_MIN} min for the SMS code...", flush=True)

        deadline = time.monotonic() + WAIT_MIN * 60
        code = None
        while time.monotonic() < deadline:
            status, c = await hero.get_status(act.id)
            if status == "OK" and c:
                code = c
                print(f"*** SMS CODE RECEIVED: {c} ***", flush=True)
                break
            if status == "CANCEL":
                print("Activation cancelled by provider.", flush=True)
                break
            await asyncio.sleep(6)

        if code:
            await hero.finish(act.id)
            print("Activation COMPLETED (this consumes the number; cost applies).", flush=True)
        else:
            await hero.cancel(act.id)
            print("No code within the window -> CANCELLED (no charge).", flush=True)

        bal1 = await hero.get_balance()
        print(f"Balance after: ${bal1}", flush=True)
        print("DONE.", flush=True)
    except NoNumbersError:
        print(f"NO_NUMBERS available for '{SERVICE}' right now.", flush=True)
    except HeroSMSError as e:
        print(f"HeroSMS error: {e.code}", flush=True)
    finally:
        await hero.close()


if __name__ == "__main__":
    asyncio.run(main())
