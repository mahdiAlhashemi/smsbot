"""Validate the DB/repo money-safety logic against a real SQLite database.

Run:  python test_db.py
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("HEROSMS_API_KEY", "testkey")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ["DB_URL"] = "sqlite+aiosqlite:///./_test_smsbot.db"

from decimal import Decimal  # noqa: E402

from db import init_db  # noqa: E402
from db import repo  # noqa: E402
from db.models import Order, Payment  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


async def main():
    # fresh db
    if os.path.exists("_test_smsbot.db"):
        os.remove("_test_smsbot.db")
    await init_db()

    print("[users + balance]")
    u = await repo.get_or_create_user(100, "alice", "Alice", False)
    check("created", u.id == 100 and u.balance == Decimal("0"))

    await repo.credit(100, Decimal("10.00"))
    u = await repo.get_user(100)
    check("credit 10", u.balance == Decimal("10.00"))

    ok = await repo.try_debit(100, Decimal("3.50"))
    u = await repo.get_user(100)
    check("debit 3.50 ok", ok and u.balance == Decimal("6.50"))
    check("total_spent tracked", u.total_spent == Decimal("3.50"))

    ok = await repo.try_debit(100, Decimal("999"))
    u = await repo.get_user(100)
    check("overspend blocked", ok is False and u.balance == Decimal("6.50"))

    await repo.refund(100, Decimal("3.50"))
    u = await repo.get_user(100)
    check("refund restores", u.balance == Decimal("10.00") and u.total_spent == Decimal("0"))

    print("[concurrent debits never go negative]")
    await repo.credit(100, Decimal("0"))  # balance is 10.00
    # 20 concurrent debits of 1.00 against a 10.00 balance -> exactly 10 succeed.
    results = await asyncio.gather(*[repo.try_debit(100, Decimal("1.00")) for _ in range(20)])
    u = await repo.get_user(100)
    check("exactly 10 debits succeed", sum(results) == 10)
    check("balance is 0 not negative", u.balance == Decimal("0.00"))

    print("[orders]")
    order = await repo.create_order(
        user_id=100, activation_id="A1", service="tg", service_name="Telegram",
        country="0", country_name="Russia", phone="79990001122",
        cost=Decimal("0.40"), price=Decimal("0.60"), status=Order.WAITING,
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=20),
    )
    check("order created", order.id is not None and order.status == "waiting")
    check("dedup detects open order", await repo.has_open_order_for(100, "tg", "0") is True)
    check("dedup ignores other service", await repo.has_open_order_for(100, "wa", "0") is False)
    open_orders = await repo.get_open_orders()
    check("open orders found", any(o.id == order.id for o in open_orders))
    await repo.update_order(order.id, status=Order.RECEIVED, code="123456")
    o = await repo.get_order(order.id)
    check("order updated", o.status == "received" and o.code == "123456")

    # release-retry tracking (HeroSMS cancel that was rejected -> retry queue)
    await repo.set_hero_released(order.id, False)
    needing = await repo.get_orders_needing_release()
    check("order needs release", any(x.id == order.id for x in needing))
    await repo.set_hero_released(order.id, True)
    needing = await repo.get_orders_needing_release()
    check("released order cleared", all(x.id != order.id for x in needing))

    print("[payments idempotency]")
    pay = await repo.create_payment(
        user_id=100, provider="cryptobot", invoice_id="INV1",
        amount=Decimal("5.00"), asset="USDT",
    )
    first = await repo.mark_payment_paid(pay.id)
    second = await repo.mark_payment_paid(pay.id)
    check("first mark paid", first is True)
    check("second mark paid blocked", second is False)
    p = await repo.get_payment(pay.id)
    check("status paid", p.status == Payment.PAID)

    print("[charge-on-receive holds]")
    # Create the user first, THEN fund it: balance 5, held 0.
    await repo.get_or_create_user(200, "bob", "Bob", False)
    await repo.credit(200, Decimal("5.00"))
    u2 = await repo.get_user(200)
    check("u2 starts at 5", u2.balance == Decimal("5.00") and u2.held == Decimal("0"))

    ok = await repo.try_hold(200, Decimal("2.00"))
    u2 = await repo.get_user(200)
    check("hold 2 ok", ok and u2.held == Decimal("2.00") and u2.balance == Decimal("5.00"))
    check("available = 3", u2.available == Decimal("3.00"))

    ok = await repo.try_hold(200, Decimal("4.00"))  # only 3 available
    u2 = await repo.get_user(200)
    check("over-hold blocked", ok is False and u2.held == Decimal("2.00"))

    ok = await repo.charge_hold(200, Decimal("2.00"))  # code arrived -> charge
    u2 = await repo.get_user(200)
    check("charge moves money", ok and u2.balance == Decimal("3.00") and u2.held == Decimal("0"))
    check("charge tracks spend", u2.total_spent == Decimal("2.00"))

    await repo.try_hold(200, Decimal("1.50"))
    await repo.release_hold(200, Decimal("1.50"))  # no code -> release
    u2 = await repo.get_user(200)
    check("release restores", u2.held == Decimal("0") and u2.balance == Decimal("3.00"))
    check("release = no charge", u2.total_spent == Decimal("2.00"))

    # Concurrent holds: balance 3 available -> exactly 3 of 10 $1 holds succeed.
    holds = await asyncio.gather(*[repo.try_hold(200, Decimal("1.00")) for _ in range(10)])
    u2 = await repo.get_user(200)
    check("exactly 3 holds succeed", sum(holds) == 3)
    check("held never exceeds balance", u2.held == Decimal("3.00") and u2.available == Decimal("0"))

    print("[atomic close — exactly-once refund]")
    # New WAITING order; 5 concurrent close attempts must let only ONE win.
    o2 = await repo.create_order(
        user_id=100, activation_id="A2", service="tg", service_name="Telegram",
        country="0", country_name="Russia", phone="79990002233",
        cost=Decimal("0.40"), price=Decimal("0.60"), status=Order.WAITING,
        expires_at=dt.datetime.now(dt.timezone.utc),
    )
    wins = await asyncio.gather(
        *[repo.close_order(o2.id, Order.CANCELED, (Order.WAITING,)) for _ in range(5)]
    )
    check("exactly one close wins", sum(wins) == 1)
    # Only the winner refunds: simulate that by refunding once for the single win.
    bal_before = (await repo.get_user(100)).balance
    for won in wins:
        if won:
            await repo.refund(100, Decimal("0.60"))
    bal_after = (await repo.get_user(100)).balance
    check("refunded exactly once", bal_after - bal_before == Decimal("0.60"))

    print("[settings]")
    await repo.set_setting("markup_percent", "42")
    check("setting roundtrip", await repo.get_setting("markup_percent") == "42")
    await repo.set_setting("markup_percent", "55")
    check("setting overwrite", await repo.get_setting("markup_percent") == "55")

    print("[admin stats]")
    await repo.update_order(order.id, status=Order.COMPLETED)
    stats = await repo.admin_stats()
    check("stats users", stats["users"] == 2)  # users 100 and 200
    check("stats completed", stats["completed"] == 1)
    check("stats profit", stats["profit"] == Decimal("0.20"))

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    # cleanup
    from db import engine
    await engine.dispose()
    try:
        os.remove("_test_smsbot.db")
    except OSError:
        pass
    return FAIL


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
