"""One-off: brand the channel + post launch marketing via the Telegram Bot API.
UTF-8 safe (emoji/em-dash) because httpx encodes the form body as UTF-8.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from config import settings  # noqa: E402

T = settings.bot_token
CH = "-1003840285433"
API = f"https://api.telegram.org/bot{T}"

TITLE = "NumberHub — Numbers & eSIM"
DESC = ("📱 Virtual numbers for OTP codes (800+ apps, 190+ countries) + 📶 travel "
        "eSIM data in 219 destinations. Pay only when it works. 👉 @TheNumberHubBot")

WELCOME = (
    "📱 <b>Welcome to NumberHub</b>\n\n"
    "Your all-in-one hub for virtual numbers and travel data:\n\n"
    "📲 <b>OTP numbers</b> — receive codes for 800+ apps (Telegram, WhatsApp, "
    "Instagram, OpenAI, Google…), 190+ countries\n"
    "📱 <b>Rent a number</b> — keep a number for days/weeks, get all its OTP\n"
    "📶 <b>eSIM data plans</b> — instant QR-code eSIM for 219 destinations\n\n"
    "✅ OTP: pay <b>only when the code arrives</b> — no code, no charge\n"
    "✅ Instant delivery · crypto top-up\n\n"
    "👉 Start now: @TheNumberHubBot"
)
LAUNCH = (
    "🚀 <b>NumberHub is LIVE</b> — now with eSIM!\n\n"
    "Everything you need to verify accounts and stay online abroad:\n\n"
    "• 📲 OTP numbers — 800+ services, 190+ countries, from <b>$0.10</b>\n"
    "• 📱 Rentals — one number, many OTP, for days or weeks\n"
    "• 📶 eSIM data — 219 destinations, delivered as a QR code\n"
    "• Auto-refund if no OTP code · replace a number anytime\n\n"
    "👉 @TheNumberHubBot"
)
HOWITWORKS = (
    "❓ <b>How NumberHub works</b>\n\n"
    "1️⃣ Top up your wallet (crypto)\n"
    "2️⃣ Pick a service + country (or a rental, or an eSIM)\n"
    "3️⃣ Get your number / QR instantly\n"
    "4️⃣ OTP codes arrive in the bot automatically\n\n"
    "💡 For OTP you're charged <b>only when the code arrives</b>. No code = no charge.\n\n"
    "👉 @TheNumberHubBot"
)
ESIM = (
    "📶 <b>NEW: Travel eSIM data plans</b>\n\n"
    "Land with data already working — no roaming, no physical SIM.\n\n"
    "• 219 destinations 🌍\n"
    "• Plans from 100MB to 100GB\n"
    "• Delivered instantly as a <b>QR code</b> — scan & go\n"
    "• Works on any eSIM-capable phone\n\n"
    "Install over Wi-Fi before you fly. 👉 @TheNumberHubBot"
)
WHYUS = (
    "🛡 <b>Why NumberHub?</b>\n\n"
    "✅ Pay-per-result on OTP — charged only when the code arrives\n"
    "✅ Instant auto-refund if no code\n"
    "✅ Replace non-working numbers in 1 tap\n"
    "✅ Rentals + travel eSIM data, all in one bot\n"
    "✅ Fast crypto top-up\n\n"
    "👉 @TheNumberHubBot"
)


async def call(client: httpx.AsyncClient, method: str, **data) -> dict:
    r = await client.post(f"{API}/{method}", data=data)
    j = r.json()
    print(f"{method:20} -> {'OK' if j.get('ok') else 'FAIL: ' + str(j.get('description'))}")
    return j


async def main() -> None:
    async with httpx.AsyncClient(timeout=30.0) as c:
        await call(c, "setChatTitle", chat_id=CH, title=TITLE)
        await call(c, "setChatDescription", chat_id=CH, description=DESC)
        w = await call(c, "sendMessage", chat_id=CH, text=WELCOME,
                       parse_mode="HTML", disable_web_page_preview="true")
        if w.get("ok"):
            mid = w["result"]["message_id"]
            await call(c, "pinChatMessage", chat_id=CH, message_id=mid,
                       disable_notification="true")
        for text in (LAUNCH, HOWITWORKS, ESIM, WHYUS):
            await call(c, "sendMessage", chat_id=CH, text=text,
                       parse_mode="HTML", disable_web_page_preview="true")
    print("\nDONE.")


if __name__ == "__main__":
    asyncio.run(main())
