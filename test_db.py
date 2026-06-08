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
    # fresh db (also clear WAL/SHM sidecars)
    for ext in ("", "-wal", "-shm"):
        p = "_test_smsbot.db" + ext
        if os.path.exists(p):
            os.remove(p)
    await init_db()

    print("[users + balance]")
    u, created = await repo.get_or_create_user(100, "alice", "Alice", False)
    check("created", u.id == 100 and u.balance == Decimal("0") and created is True)

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

    print("[multi-code display (Feature A)]")
    from services import orders as osvc
    import json as _json

    class _FO:
        def __init__(self, code):
            self.code = code

    check("stored_codes empty", osvc.stored_codes(_FO(None)) == [])
    check("stored_codes legacy bare str", osvc.stored_codes(_FO("ABC123"))[0]["code"] == "ABC123")
    check("stored_codes legacy numeric", osvc.stored_codes(_FO("123456"))[0]["code"] == "123456")
    _lst = _json.dumps([{"type": "sms", "code": "111", "text": "a", "at": ""},
                        {"type": "call", "code": "222", "text": "b", "at": ""}])
    check("stored_codes json list len", len(osvc.stored_codes(_FO(_lst))) == 2)
    check("latest_code returns last", osvc.latest_code(_FO(_lst)) == "222")
    _m = osvc._merge_codes([{"type": "sms", "code": "111"}],
                           [{"type": "sms", "code": "111"}, {"type": "sms", "code": "333"}])
    check("merge dedups + appends", [e["code"] for e in _m] == ["111", "333"])
    check("merge drops empty code", osvc._merge_codes([], [{"type": "sms", "code": ""}]) == [])

    # deliver_code: appends to the JSON list AND charges the hold exactly once
    await repo.credit(100, Decimal("5.00"))
    await repo.try_hold(100, Decimal("0.60"))
    # Use a throwaway service/country so deliver_code's stat doesn't collide with
    # the [service stats badges] section's exact-count assertion below.
    mc = await repo.create_order(
        user_id=100, activation_id="MC1", service="zz", service_name="ZZ",
        country="99", country_name="Nowhere", phone="79990002233",
        cost=Decimal("0.40"), price=Decimal("0.60"), status=Order.WAITING,
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=20),
    )
    bal_before = (await repo.get_user(100)).balance
    d1 = await osvc.deliver_code(mc, "555111")
    o2 = await repo.get_order(mc.id)
    check("deliver_code first ok", d1 and osvc.latest_code(o2) == "555111")
    bal_after = (await repo.get_user(100)).balance
    check("deliver_code charged once", bal_before - bal_after == Decimal("0.60"))

    class _FakeHero:
        async def get_all_sms(self, aid, page=1, size=20):
            return [{"code": "555111", "text": "Your code 555111", "date": "t1"},
                    {"code": "777222", "text": "Second 777222", "date": "t2"}]

        async def get_status_v2(self, aid):
            return {"verificationType": 1, "sms": {},
                    "call": {"code": "999333", "text": "voice", "dateTime": "t3"}}

    new = await osvc.collect_codes(await repo.get_order(mc.id), _FakeHero())
    o2 = await repo.get_order(mc.id)
    codes = [e["code"] for e in osvc.stored_codes(o2)]
    check("collect appends only new", set(e["code"] for e in new) == {"777222", "999333"})
    check("collect dedups existing", codes == ["555111", "777222", "999333"])
    check("collect_codes is display-only", (await repo.get_user(100)).balance == bal_after)

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

    print("[rent extend / prolong (Feature C)]")
    from services import rent as rsvc, pricing as _pricing

    check("parse_prolong_options envelope + defensive keys + drop non-positive",
          rsvc.parse_prolong_options({"data": {"options": [
              {"hours": 24, "price": 0.46}, {"hours": 72, "cost": 1.2},
              {"hours": 168, "price": 0}]}}) == {24: Decimal("0.46"), 72: Decimal("1.2")})

    class _PHero:
        def __init__(self, fail=False):
            self.fail = fail

        async def prolong(self, aid, duration):
            if self.fail:
                raise Exception("provider down")
            return None

    async def _mk_rent(uid, bal, status=Order.WAITING):
        await repo.get_or_create_user(uid, None, None, False)
        await repo.credit(uid, bal)
        t0 = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
        o = await repo.create_order(
            user_id=uid, kind="rent", activation_id=f"R{uid}", service="full",
            service_name="Rent", country="0", country_name="Russia",
            phone="79990000000", cost=Decimal("0.40"), price=Decimal("0.50"),
            status=status, expires_at=t0,
        )
        return o, t0

    def _exp_eq(o, target):
        return abs((o.expires_at.replace(tzinfo=dt.timezone.utc) - target).total_seconds()) < 2

    _cost = Decimal("0.40")
    _exp_price = await _pricing.commission_price(_cost)

    # (a) happy path — charged once, status restored, expiry +24h
    o, t0 = await _mk_rent(201, Decimal("5.00"))
    ok = await rsvc.prolong_rent(o, 24, _cost, _PHero())
    u = await repo.get_user(201); o = await repo.get_order(o.id)
    check("prolong happy True", ok is True)
    check("prolong charged once", u.balance == Decimal("5.00") - _exp_price and u.held == Decimal("0"))
    check("prolong status restored", o.status == "waiting")
    check("prolong expiry +24h", _exp_eq(o, t0 + dt.timedelta(hours=24)))

    # (b) insufficient funds — no charge, status + expiry untouched
    o, t0 = await _mk_rent(202, Decimal("0.01"))
    r = False
    try:
        await rsvc.prolong_rent(o, 24, _cost, _PHero())
    except osvc.InsufficientFunds:
        r = True
    u = await repo.get_user(202); o = await repo.get_order(o.id)
    check("prolong insufficient raises", r)
    check("prolong insufficient no charge", u.balance == Decimal("0.01") and u.held == Decimal("0"))
    check("prolong insufficient status+expiry intact", o.status == "waiting" and _exp_eq(o, t0))

    # (c) provider error — hold released, ORIGINAL expiry preserved
    o, t0 = await _mk_rent(203, Decimal("5.00"))
    r = False
    try:
        await rsvc.prolong_rent(o, 24, _cost, _PHero(fail=True))
    except osvc.PurchaseError:
        r = True
    u = await repo.get_user(203); o = await repo.get_order(o.id)
    check("prolong provider-error raises", r)
    check("prolong provider-error no charge + hold released",
          u.balance == Decimal("5.00") and u.held == Decimal("0"))
    check("prolong provider-error status+expiry intact", o.status == "waiting" and _exp_eq(o, t0))

    # (d) guard — a closed rental can't be extended (no charge)
    o, t0 = await _mk_rent(204, Decimal("5.00"), status=Order.CANCELED)
    r = False
    try:
        await rsvc.prolong_rent(o, 24, _cost, _PHero())
    except osvc.ProlongError:
        r = True
    u = await repo.get_user(204)
    check("prolong guard raises on closed rental", r)
    check("prolong guard no charge", u.balance == Decimal("5.00") and u.held == Decimal("0"))

    print("[deposit bonus + referral]")
    from services import billing  # noqa: PLC0415
    await repo.get_or_create_user(300, "carol", "Carol", False)
    await repo.set_referrer(300, 100)
    check("referrer set once", await repo.set_referrer(300, 999) is False)  # already set
    pay2 = await repo.create_payment(
        user_id=300, provider="x", invoice_id="INV2", amount=Decimal("20.00"), asset="USDT",
    )
    await repo.mark_payment_paid(pay2.id)
    new_bal, bonus = await billing.credit_topup(300, Decimal("20.00"))
    # ladder 20->+5% = 1.00, first +10% = 2.00 => 3.00, hard-capped at 10% (2.00)
    check("deposit bonus = 2.00 (10% cap)", bonus == Decimal("2.00"))
    u3 = await repo.get_user(300)
    check("referee credited + bounty (22.50)", u3.balance == Decimal("22.50"))
    u1 = await repo.get_user(100)
    check("referrer earned 0.50", u1.ref_earnings == Decimal("0.50"))
    check("referral pays once", await repo.claim_referral_bonus(300) is None)

    print("[service stats badges]")
    await repo.record_stat("tg", "0", True)
    await repo.record_stat("tg", "0", True)
    await repo.record_stat("tg", "0", False)
    smap = await repo.get_all_stats()
    check("stats recorded", smap.get(("tg", "0")) == (2, 1))

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    # cleanup
    from db import engine
    await engine.dispose()
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove("_test_smsbot.db" + ext)
        except OSError:
            pass
    return FAIL


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
