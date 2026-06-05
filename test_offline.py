"""Dependency-light validation of the core logic (no aiogram needed).

Exercises HeroSMS response parsing + pricing using canned API responses.
Run:  python test_offline.py
"""
import asyncio
import os
from decimal import Decimal

# Provide the env config so `config` imports cleanly.
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("HEROSMS_API_KEY", "testkey")
os.environ.setdefault("ADMIN_IDS", "1")

from herosms.client import (  # noqa: E402
    HeroSMSClient,
    HeroSMSError,
    NoBalanceError,
    NoNumbersError,
)
from services.pricing import apply_markup  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def expect_error(name, fn, exc, code=None):
    try:
        fn()
        check(name, False)
    except exc as e:  # noqa: PERF203
        check(name, code is None or getattr(e, "code", None) == code)
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL {name}: wrong exc {type(e).__name__}")
        global FAIL
        FAIL += 1


def test_error_parsing():
    print("[error parsing]")
    r = HeroSMSClient._raise_for_error
    expect_error("json BAD_KEY", lambda: r('{"title":"BAD_KEY","details":"Unauthorized"}', 401),
                 HeroSMSError, "BAD_KEY")
    expect_error("plain NO_NUMBERS", lambda: r("NO_NUMBERS", 200), NoNumbersError, "NO_NUMBERS")
    expect_error("plain NO_BALANCE", lambda: r("NO_BALANCE", 200), NoBalanceError, "NO_BALANCE")
    expect_error("json title NO_NUMBERS", lambda: r('{"title":"NO_NUMBERS"}', 200),
                 NoNumbersError, "NO_NUMBERS")
    # These must NOT raise:
    for ok in ("ACCESS_BALANCE:12.34", "STATUS_OK:123456", "ACCESS_NUMBER:55:79991234567",
               '{"activationId":"1","phoneNumber":"79991234567"}', "STATUS_WAIT_CODE"):
        try:
            r(ok, 200)
            check(f"no-error {ok[:20]}", True)
        except Exception:  # noqa: BLE001
            check(f"no-error {ok[:20]}", False)


def _client_returning(mapping):
    """Build a client whose _raw returns a canned string per action."""
    c = HeroSMSClient("k", "http://x", client=object())  # client unused (we patch _raw)

    async def fake_raw(action, **params):
        val = mapping[action]
        return val(params) if callable(val) else val

    c._raw = fake_raw  # type: ignore[assignment]
    return c


async def test_balance():
    print("[get_balance]")
    c = _client_returning({"getBalance": "ACCESS_BALANCE:12.34"})
    check("legacy balance", await c.get_balance() == Decimal("12.34"))
    c = _client_returning({"getBalance": '{"balance": 5.5}'})
    check("json balance", await c.get_balance() == Decimal("5.5"))


async def test_status():
    print("[get_status]")
    c = _client_returning({"getStatus": "STATUS_OK:99999"})
    check("OK+code", await c.get_status("1") == ("OK", "99999"))
    c = _client_returning({"getStatus": "STATUS_WAIT_CODE"})
    check("wait", await c.get_status("1") == ("WAIT_CODE", None))
    c = _client_returning({"getStatus": "STATUS_CANCEL"})
    check("cancel", await c.get_status("1") == ("CANCEL", None))
    c = _client_returning({"getStatus": "STATUS_WAIT_RETRY:111"})
    s, code = await c.get_status("1")
    check("retry", s == "WAIT_RETRY" and code == "111")


async def test_get_number():
    print("[get_number]")
    js = '{"activationId":"777","phoneNumber":"79991234567","activationCost":"0.40","countryCode":"0","canGetAnotherSms":true}'
    c = _client_returning({"getNumberV2": js})
    act = await c.get_number("tg", "0")
    check("v2 id", act.id == "777")
    check("v2 phone", act.phone == "79991234567")
    check("v2 cost", act.cost == Decimal("0.40"))
    check("v2 another", act.can_get_another is True)

    # A parsed-but-incomplete V2 response is AUTHORITATIVE — it must raise, never
    # fall through to a second getNumber (which would double-order a real number).
    # 'getNumber' is intentionally absent: a fall-through would KeyError, not raise.
    c2 = _client_returning({"getNumberV2": '{"status":"error","msg":"bad"}'})
    code = None
    try:
        await c2.get_number("tg", "0")
    except HeroSMSError as e:
        code = e.code
    except Exception as e:  # noqa: BLE001
        code = "WRONG:" + type(e).__name__
    check("v2 malformed raises (no double-order)", code == "UNEXPECTED_RESPONSE")

    # V2 genuinely unavailable (HTTP/route error) -> legacy getNumber fallback.
    def _v2_404(_params):
        raise HeroSMSError("HTTP_404")
    c3 = _client_returning({"getNumberV2": _v2_404, "getNumber": "ACCESS_NUMBER:55:79991234567"})
    act3 = await c3.get_number("tg", "0")
    check("v2 unavailable -> legacy fallback", act3.id == "55" and act3.phone == "79991234567")

    # NO_NUMBERS should propagate even from the V2 attempt.
    def boom(_):
        HeroSMSClient._raise_for_error("NO_NUMBERS", 200)
    c = _client_returning({"getNumberV2": boom, "getNumber": boom})
    try:
        await c.get_number("tg", "0")
        check("no_numbers raises", False)
    except NoNumbersError:
        check("no_numbers raises", True)


async def test_catalog_parsing():
    print("[services/countries/prices]")
    c = _client_returning({
        "getServicesList": '{"status":"success","services":[{"code":"tg","name":"Telegram"},{"code":"wa","name":"WhatsApp"}]}',
        "getCountries": '{"0":{"id":0,"rus":"Россия","eng":"Russia"},"6":{"id":6,"eng":"Indonesia"}}',
        "getPrices": '{"0":{"tg":{"cost":0.40,"count":150,"physicalCount":150}},"6":{"tg":{"cost":0.20,"count":99,"physicalCount":0}}}',
    })
    svcs = await c.get_services()
    check("services count", len(svcs) == 2 and svcs[0]["code"] == "tg")
    countries = await c.get_countries()
    check("country name", countries.get("0") == "Russia")
    prices = await c.country_prices_for_service("tg")
    # Full list: both countries included (country 6 is out of stock -> queues).
    check("full list incl out-of-stock", len(prices) == 2)
    # In-stock country (0) sorts before out-of-stock (6).
    check("in-stock sorts first", prices[0]["country"] == "0" and prices[0]["cost"] == Decimal("0.40"))
    check("out-of-stock last", prices[1]["country"] == "6")


def test_pricing():
    print("[pricing]")
    check("30% of 0.50", apply_markup(Decimal("0.50"), Decimal("30")) == Decimal("0.65"))
    check("ceil 0.23*1.3", apply_markup(Decimal("0.23"), Decimal("30")) == Decimal("0.30"))
    check("0% markup", apply_markup(Decimal("1.00"), Decimal("0")) == Decimal("1.00"))
    check("100% markup", apply_markup(Decimal("2.00"), Decimal("100")) == Decimal("4.00"))


async def main():
    test_error_parsing()
    await test_balance()
    await test_status()
    await test_get_number()
    await test_catalog_parsing()
    test_pricing()
    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    return FAIL


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
