"""Smoke-test the aiogram layer: imports, router registration, keyboards,
callback-data pack/unpack. No network/polling.

Run:  python test_handlers.py
"""
import os

os.environ.setdefault("BOT_TOKEN", "123456:AAHtesttoken_ABCDEFGHIJKLMNOPQRSTUV")
os.environ.setdefault("HEROSMS_API_KEY", "testkey")
os.environ.setdefault("ADMIN_IDS", "1")

from decimal import Decimal  # noqa: E402

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

from db.models import Order  # noqa: E402
from handlers import register_handlers  # noqa: E402
from keyboards import callbacks as cb  # noqa: E402
from keyboards import menus  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def main():
    print("[router registration]")
    dp = Dispatcher(storage=MemoryStorage())
    register_handlers(dp)
    updates = dp.resolve_used_update_types()
    check("routers registered", "message" in updates and "callback_query" in updates)

    print("[bot construction]")
    bot = Bot(os.environ["BOT_TOKEN"])
    check("bot built", bot is not None)

    print("[callback pack/unpack]")
    packed = cb.BuyConfirm(code="tg", country="0").pack()
    un = cb.BuyConfirm.unpack(packed)
    check("BuyConfirm roundtrip", un.code == "tg" and un.country == "0")
    check("BuyConfirm short", len(packed.encode()) <= 64)
    check("CtyPick roundtrip", cb.CtyPick.unpack(cb.CtyPick(code="tg", country="6").pack()).country == "6")
    check("OrderAct roundtrip", cb.OrderAct.unpack(cb.OrderAct(action="cancel", id=5).pack()).id == 5)
    check("Nav roundtrip", cb.Nav.unpack(cb.Nav(to="wallet").pack()).to == "wallet")
    check("TopupPick", cb.TopupPick.unpack(cb.TopupPick(amount="10").pack()).amount == "10")
    check("RentExt roundtrip", cb.RentExt.unpack(cb.RentExt(id=7, h=24).pack()).h == 24)
    check("RentExtGo roundtrip", cb.RentExtGo.unpack(cb.RentExtGo(id=7, h=168).pack()).h == 168)
    check("RentExt short", len(cb.RentExt(id=999999, h=4320).pack().encode()) <= 64)

    print("[keyboards build]")
    services = [{"code": "tg", "name": "Telegram"}, {"code": "wa", "name": "WhatsApp"}] * 10
    rows = [{"country": str(i), "cost": Decimal("0.40"), "count": 100, "sell": Decimal("0.60")} for i in range(20)]
    names = {str(i): f"Country{i}" for i in range(20)}

    kbs = {
        "main_menu": menus.main_menu(True, True),
        "main_menu_user": menus.main_menu(False, False),
        "back": menus.back_button("main"),
        "services_p0": menus.services_keyboard(services, 0),
        "services_p1": menus.services_keyboard(services, 1),
        "countries": menus.countries_keyboard("tg", rows, names, 0),
        "confirm_afford": menus.confirm_keyboard("tg", "0", can_afford=True),
        "confirm_poor": menus.confirm_keyboard("tg", "0", can_afford=False),
        "wallet_on": menus.wallet_keyboard(True),
        "wallet_off": menus.wallet_keyboard(False),
        "topup": menus.topup_amounts_keyboard(),
        "payment": menus.payment_keyboard("https://t.me/CryptoBot?start=x", 7),
        "admin": menus.admin_keyboard(),
        "order_waiting": menus.order_keyboard(Order(id=1, status=Order.WAITING)),
        "order_received": menus.order_keyboard(Order(id=2, status=Order.RECEIVED)),
        "order_done": menus.order_keyboard(Order(id=3, status=Order.COMPLETED)),
        "rent_extend_menu": menus.rent_extend_keyboard(9, [(24, Decimal("0.60")), (168, Decimal("1.20"))]),
        "rent_extend_confirm": menus.rent_extend_confirm_keyboard(9, 24),
    }
    import datetime as _dt
    _active_rent = Order(id=9, kind="rent", status=Order.WAITING,
                         created_at=_dt.datetime.now(_dt.timezone.utc),
                         expires_at=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1))
    kbs["rent_card_active"] = menus.rent_order_keyboard(_active_rent)
    check("rent card shows Extend button",
          any("Extend" in (btn.text or "")
              for row in kbs["rent_card_active"].inline_keyboard for btn in row))
    for name, kb in kbs.items():
        ok = kb is not None and hasattr(kb, "inline_keyboard")
        # validate every button has exactly one action set
        for rowi in kb.inline_keyboard:
            for btn in rowi:
                if not (btn.callback_data or btn.url):
                    ok = False
        check(f"kb {name}", ok)

    print("[format_order]")
    from utils import format_order
    o = Order(id=9, service="tg", service_name="Telegram", country="0",
              country_name="Russia", phone="79990001122", price=Decimal("0.60"),
              status=Order.RECEIVED, code="123456")
    txt = format_order(o)
    check("format_order has phone", "79990001122" in txt)
    check("format_order has code", "123456" in txt)

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    return FAIL


if __name__ == "__main__":
    raise SystemExit(main())
