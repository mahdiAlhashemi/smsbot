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

    print("[reactivate / reuse number (Feature B)]")
    from herosms import Activation as _Act

    class _RHero:
        def __init__(self, fail=False):
            self.fail = fail
            self.canceled = []

        async def reactivate(self, activation_id, duration=None):
            if self.fail:
                from herosms import HeroSMSError
                raise HeroSMSError("NO_BALANCE")
            return _Act(id="NEW999", phone="79991112233", cost=Decimal("0.42"),
                        country="0", can_get_another=True)

        async def cancel(self, activation_id):
            self.canceled.append(activation_id)

    class _Cat:
        def invalidate_prices(self, service=None):
            pass

    async def _mk_done(uid, bal, status=Order.COMPLETED, aid="OLD1", kind="sms"):
        await repo.get_or_create_user(uid, None, None, False)
        await repo.credit(uid, bal)
        o = await repo.create_order(
            user_id=uid, kind=kind, activation_id=aid, service="zz", service_name="ZZ",
            country="99", country_name="Nowhere", phone="79990000000",
            cost=Decimal("0.30"), price=Decimal("0.45"), status=status, code="111222",
            expires_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1),
        )
        return o

    _rp = await _pricing.commission_price(Decimal("0.30"))

    # (a) happy: terminal -> WAITING, HELD (balance unchanged), number swapped, code cleared
    o = await _mk_done(301, Decimal("5.00"))
    res = await osvc.reactivate_number(o, _RHero(), _Cat())
    u = await repo.get_user(301); o = await repo.get_order(o.id)
    check("reactivate REACT_OK", res == osvc.REACT_OK)
    check("reactivate -> WAITING", o.status == "waiting")
    check("reactivate held not charged", u.held == _rp and u.balance == Decimal("5.00"))
    check("reactivate swapped number", o.activation_id == "NEW999" and o.phone == "79991112233")
    check("reactivate cleared code", o.code is None)
    check("reactivate fresh future expiry",
          o.expires_at.replace(tzinfo=dt.timezone.utc) > dt.datetime.now(dt.timezone.utc))
    # a delivered code now charges exactly once against the reactivate hold
    bal0 = u.balance
    await osvc.deliver_code(o, "777888")
    u = await repo.get_user(301)
    check("reactivate code charges once", u.balance == bal0 - _rp and u.held == Decimal("0"))

    # (b) provider error: rolled back to terminal, hold released
    o = await _mk_done(302, Decimal("5.00"), status=Order.EXPIRED)
    res = await osvc.reactivate_number(o, _RHero(fail=True), _Cat())
    u = await repo.get_user(302); o = await repo.get_order(o.id)
    check("reactivate provider-error REACT_ERROR", res == osvc.REACT_ERROR)
    check("reactivate provider-error rolled back", o.status == "expired")
    check("reactivate provider-error hold released", u.held == Decimal("0") and u.balance == Decimal("5.00"))

    # (c) insufficient funds: REACT_INSUFFICIENT, stays terminal
    o = await _mk_done(303, Decimal("0.01"))
    res = await osvc.reactivate_number(o, _RHero(), _Cat())
    u = await repo.get_user(303); o = await repo.get_order(o.id)
    check("reactivate insufficient", res == osvc.REACT_INSUFFICIENT)
    check("reactivate insufficient stays terminal", o.status == "completed")
    check("reactivate insufficient no hold", u.held == Decimal("0"))

    # (d) ineligible (rent / no activation_id) -> REACT_ERROR, untouched
    o = await _mk_done(304, Decimal("5.00"), kind="rent")
    res = await osvc.reactivate_number(o, _RHero(), _Cat())
    check("reactivate rejects non-sms", res == osvc.REACT_ERROR)

    print("[email OTP product (Feature D)]")
    from services import emails as esvc
    from herosms.v1 import HeroSMSV1Error

    class _MHero:
        def __init__(self, status="WAIT", value=None, fail=False):
            self.status = status
            self.value = value
            self.fail = fail
            self.canceled = []
            self.reordered = []

        async def email_purchase(self, site, domain):
            if self.fail:
                raise HeroSMSV1Error(422, "Validation failed", "bad")
            return {"data": {"id": 55501, "email": f"john@{domain}", "status": "WAIT", "cost": 0.045}}

        async def email_status(self, eid):
            return {"data": {"id": eid, "status": self.status, "value": self.value}}

        async def email_cancel(self, eid):
            self.canceled.append(eid)

        async def email_reorder(self, eid):
            self.reordered.append(eid)

    _ec = await _pricing.email_sell_price(Decimal("0.045"))

    # (a) purchase HOLDS (no charge), creates a WAITING email order
    await repo.get_or_create_user(401, None, None, False); await repo.credit(401, Decimal("5.00"))
    eo = await esvc.email_purchase(401, "instagram.com", "gmx.com", Decimal("0.045"), _MHero())
    u = await repo.get_user(401)
    check("email purchase WAITING", eo.status == "waiting" and eo.kind == "email")
    check("email held not charged", u.held == _ec and u.balance == Decimal("5.00"))
    check("email address stored", eo.phone == "john@gmx.com")

    # (b) code arrives -> charge exactly once
    st, code = await esvc.poll_email_status(eo, _MHero(status="SUCCESS", value="246810"))
    check("email poll SUCCESS", st == "SUCCESS" and code == "246810")
    await osvc.deliver_code(eo, code)
    u = await repo.get_user(401); eo = await repo.get_order(eo.id)
    check("email charged once", u.balance == Decimal("5.00") - _ec and u.held == Decimal("0"))
    check("email code shown", osvc.latest_code(eo) == "246810")

    # (c) no code -> close releases the hold + cancels at provider
    await repo.get_or_create_user(402, None, None, False); await repo.credit(402, Decimal("5.00"))
    eo2 = await esvc.email_purchase(402, "discord.com", "mail.com", Decimal("0.045"), _MHero())
    mh2 = _MHero()
    ok = await esvc.close_email_unfilled(eo2, mh2, final_status=Order.EXPIRED)
    u = await repo.get_user(402); eo2 = await repo.get_order(eo2.id)
    check("email close releases hold", ok and u.held == Decimal("0") and u.balance == Decimal("5.00"))
    check("email closed EXPIRED", eo2.status == "expired")
    check("email cancelled at provider", mh2.canceled == [eo2.activation_id])

    # (d) insufficient funds -> no hold
    await repo.get_or_create_user(403, None, None, False); await repo.credit(403, Decimal("0.01"))
    r = False
    try:
        await esvc.email_purchase(403, "instagram.com", "gmx.com", Decimal("0.045"), _MHero())
    except osvc.InsufficientFunds:
        r = True
    check("email insufficient raises", r)
    check("email insufficient no hold", (await repo.get_user(403)).held == Decimal("0"))

    # (e) provider error -> hold released
    await repo.get_or_create_user(404, None, None, False); await repo.credit(404, Decimal("5.00"))
    r = False
    try:
        await esvc.email_purchase(404, "instagram.com", "gmx.com", Decimal("0.045"), _MHero(fail=True))
    except osvc.PurchaseError:
        r = True
    u = await repo.get_user(404)
    check("email provider-error raises", r)
    check("email provider-error hold released", u.held == Decimal("0") and u.balance == Decimal("5.00"))

    print("[rent cancel refund money-back (user report)]")

    class _CHero:
        def __init__(self):
            self.canceled = []

        async def set_rent_status(self, aid, st):
            self.canceled.append((aid, st))

    await repo.get_or_create_user(501, None, None, False); await repo.credit(501, Decimal("2.00"))
    ro = await repo.create_order(
        user_id=501, kind="rent", activation_id="RC1", service="full", service_name="Rent",
        country="0", country_name="Russia", phone="79990000000",
        cost=Decimal("0.05"), price=Decimal("0.06"), status=Order.WAITING,
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1),
    )
    bal0 = (await repo.get_user(501)).balance  # 2.00 (already-paid state)
    ch = _CHero()
    ok = await rsvc.cancel_rent_refund(ro, ch)
    u = await repo.get_user(501); ro = await repo.get_order(ro.id)
    check("rent cancel refunds the price", ok and u.balance == bal0 + Decimal("0.06"))
    check("rent cancel status CANCELED", ro.status == "canceled")
    check("rent cancel hit provider setRentStatus=2", ch.canceled == [("RC1", 2)])
    # exactly-once: a second cancel must NOT double-refund
    ok2 = await rsvc.cancel_rent_refund(await repo.get_order(ro.id), _CHero())
    check("rent cancel no double refund",
          ok2 is False and (await repo.get_user(501)).balance == bal0 + Decimal("0.06"))

    print("[money-bug fixes from audit]")
    from herosms import RentActivation as _RA
    _now2 = lambda: dt.datetime.now(dt.timezone.utc)
    _exp = _now2() + dt.timedelta(minutes=20)

    # FIX #1: rent_purchase aborts + releases hold + cancels if charge_hold no-ops
    _pr = await _pricing.commission_price(Decimal("0.05"))

    class _DrainHero:
        def __init__(self):
            self.canceled = []

        async def rent_number(self, service, country, duration):
            await repo.try_debit(601, _pr)  # simulate concurrent balance drain
            return _RA(id="RD1", phone="79990000000", end_date="", cost=Decimal("0.05"))

        async def set_rent_status(self, aid, st):
            self.canceled.append((aid, st))

    class _Cat2:
        async def service_name(self, s):
            return "X"

        async def country_name(self, c):
            return "Y"

        def invalidate_prices(self, s=None):
            pass

    await repo.get_or_create_user(601, None, None, False); await repo.credit(601, _pr)
    dh = _DrainHero()
    r = False
    try:
        await rsvc.rent_purchase(601, "full", "0", 24, Decimal("0.05"), dh, _Cat2())
    except osvc.PurchaseError:
        r = True
    u = await repo.get_user(601)
    check("rent charge-fail raises", r)
    check("rent charge-fail releases hold (no stranded funds)", u.held == Decimal("0"))
    check("rent charge-fail cancels the number", dh.canceled == [("RD1", 2)])

    # FIX #2: request_another_code goes via REQUESTING transient, resets provider
    class _AHero:
        def __init__(self):
            self.reset = []

        async def request_another_code(self, aid):
            self.reset.append(aid)

    await repo.get_or_create_user(602, None, None, False); await repo.credit(602, Decimal("1.00"))
    ao = await repo.create_order(
        user_id=602, kind="sms", activation_id="AN1", service="zz", service_name="ZZ",
        country="99", country_name="N", phone="7999", cost=Decimal("0.05"), price=Decimal("0.06"),
        status=Order.RECEIVED, code='[{"type":"sms","code":"111","text":"111","at":""}]', expires_at=_exp,
    )
    ah = _AHero()
    res = await osvc.request_another_code(ao, ah)
    ao = await repo.get_order(ao.id); u = await repo.get_user(602)
    check("another_code OK -> WAITING", res == osvc.ANOTHER_OK and ao.status == "waiting")
    check("another_code placed hold", u.held == Decimal("0.06"))
    check("another_code reset provider", ah.reset == ["AN1"])
    check("another_code kept prior codes", osvc.latest_code(ao) == "111")
    check("another_code fresh future expiry",
          ao.expires_at.replace(tzinfo=dt.timezone.utc) > _now2())

    await repo.get_or_create_user(603, None, None, False)  # balance 0
    ao2 = await repo.create_order(
        user_id=603, kind="sms", activation_id="AN2", service="zz", service_name="ZZ",
        country="99", country_name="N", phone="7999", cost=Decimal("0.05"), price=Decimal("0.06"),
        status=Order.RECEIVED, code=None, expires_at=_exp,
    )
    res2 = await osvc.request_another_code(ao2, _AHero())
    ao2 = await repo.get_order(ao2.id)
    check("another_code insufficient rolls back to RECEIVED",
          res2 == osvc.ANOTHER_INSUFFICIENT and ao2.status == "received"
          and (await repo.get_user(603)).held == Decimal("0"))

    # FIX #4: reorder_email goes via REQUESTING transient
    class _MR:
        def __init__(self):
            self.reordered = []

        async def email_reorder(self, eid):
            self.reordered.append(eid)

    await repo.get_or_create_user(604, None, None, False); await repo.credit(604, Decimal("1.00"))
    meo = await repo.create_order(
        user_id=604, kind="email", activation_id="ME1", service="instagram.com",
        service_name="Instagram", country="gmx.com", country_name="gmx.com",
        phone="a@gmx.com", cost=Decimal("0.045"), price=Decimal("0.06"),
        status=Order.RECEIVED, code=None, expires_at=_exp,
    )
    mr = _MR()
    rres = await esvc.reorder_email(meo, mr)
    meo = await repo.get_order(meo.id)
    check("reorder_email OK -> WAITING", rres == osvc.ANOTHER_OK and meo.status == "waiting")
    check("reorder_email placed hold + reset provider",
          (await repo.get_user(604)).held == Decimal("0.06") and mr.reordered == ["ME1"])

    # FIX: stuck-transient self-heal query finds REQUESTING/REACTIVATING orders
    su = await repo.create_order(
        user_id=602, kind="sms", activation_id="ST1", service="zz", service_name="ZZ",
        country="99", country_name="N", phone="7", cost=Decimal("0.05"), price=Decimal("0.06"),
        status=Order.REACTIVATING, expires_at=_exp,
    )
    stuck = await repo.get_stuck_transient_orders()
    check("self-heal query finds stuck transient", any(o.id == su.id for o in stuck))

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
