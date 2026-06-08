"""Offline tests for the extended HeroSMS clients (no network).

Exercises the *new* methods on both transports against canned API bodies taken
from the resolved spec (``_herosms_api_ref.md``), served by an ``httpx`` mock
transport — never a live HeroSMS endpoint.

  * Legacy SMS-Activate stubs : ``herosms.client.HeroSMSClient``
  * v1 REST                   : ``herosms.v1.HeroSMSV1Client``

Run:  python test_herosms_ext.py
Prints ``RESULT: N passed, M failed`` and exits non-zero on any failure.
No pytest, no DB (`DB_URL` not required — nothing here imports the db).
"""
import asyncio
import json
from decimal import Decimal

import httpx

from herosms.client import HeroSMSClient, Activation  # noqa: F401  (Activation: isinstance check)
from herosms.v1 import HeroSMSV1Client, HeroSMSV1Error

LEGACY_BASE = "https://hero-sms.com/stubs/handler_api.php"

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


# ── canned response bodies (exact shapes from _herosms_api_ref.md) ───────────
# Legacy stubs (keyed by ?action=)
_LEGACY = {
    # getStatusV2 — correctActivationStatusExample
    "getStatusV2": {
        "verificationType": 2,
        "sms": {"dateTime": "0000-00-00 00:00:00", "code": "code", "text": "sms text"},
        "call": {
            "from": "phone",
            "text": "voice text",
            "code": "12345",
            "dateTime": "0000-00-00 00:00:00",
            "url": "voice file url",
            "parsingCount": 1,
        },
    },
    # getAllSms — data array (+ meta), per the data/meta schema
    "getAllSms": {
        "data": [
            {
                "id": "1",
                "phoneFrom": "Telegram",
                "code": "12345",
                "text": "Your code is 12345",
                "service": "tg",
                "date": "2026-01-01 00:00:00",
                "type": 1,
            }
        ],
        "meta": {"total": 1},
    },
    # getActiveActivations — activationsSuccessfulExample
    "getActiveActivations": {
        "status": "success",
        "data": [
            {
                "activationId": "635468021",
                "serviceCode": "vk",
                "phoneNumber": "79********1",
                "activationCost": 12.5,
                "activationStatus": "4",
                "smsCode": "12345",
                "smsText": "Your code is 12345",
                "activationTime": "2022-06-01 16:59:16",
                "discount": "0.00",
                "repeated": "0",
                "countryCode": "2",
                "countryName": "Kazakhstan",
                "canGetAnotherSms": "1",
                "currency": 840,
            }
        ],
    },
    # getHistory — successfulActivationsHistoryExample (top-level array)
    "getHistory": [
        {
            "id": "635468024",
            "date": "0000-00-00 00:00:00",
            "phone": "7*********0",
            "sms": "Your code is ****",
            "cost": 0,
            "status": "4",
            "currency": 840,
        }
    ],
    # reactivate — getNumberV2-shaped success body
    "reactivate": {
        "activationId": "123456",
        "phoneNumber": "79991234567",
        "activationCost": 15.5,
        "currency": 840,
        "countryCode": 6,
        "activationOperator": "any",
        "canGetAnotherSms": True,
    },
    # prolong — getNumberV2-shaped success body
    "prolong": {
        "activationId": "123456",
        "phoneNumber": "79991234567",
        "activationCost": 15.5,
        "currency": 840,
        "countryCode": 6,
    },
    # getOperators — successfulGetOperatorsExample
    "getOperators": {
        "status": "success",
        "countryOperators": {"175": ["optus", "vodafone", "telstra", "lebara"]},
    },
}

# v1 REST bodies
_V1_OFFERS = {
    "data": {
        "go": {
            "6": {
                "prices": {"default": "0.0300", "retail": "0.2000", "min": "0.0300"},
                "counts": {"total": 7933, "physical": 5198, "defaultPrice": 5477},
                "map": {"0.0300": 24, "0.0589": 439, "0.1510": 3229},
            },
            "48": {
                "prices": {"default": "0.1500", "retail": "0.6000", "min": "0.1500"},
                "counts": {"total": 100, "physical": 50, "defaultPrice": 30},
                "map": {"0.1500": 10},
            },
        }
    }
}
_V1_EMAIL_CREATED = {
    "data": {
        "id": 12345,
        "site": "telegram.com",
        "email": "john.doe@gmail.com",
        "status": "PENDING",
        "value": None,
        "cost": 0.5,
        "currency": "USD",
        "domain": "gmail.com",
        "date": "2026-01-01T00:00:00Z",
    }
}
_V1_EMAIL_STATUS = {
    "data": {
        "id": 12345,
        "site": "telegram.com",
        "email": "john.doe@gmail.com",
        "status": "RECEIVED",
        "value": "123456",
        "cost": 0.5,
        "currency": "USD",
        "date": "2026-01-01T00:00:00Z",
    }
}
_V1_DOMAINS = {
    "data": [
        {"name": "gmail.com", "cost": 0.5, "count": 100},
        {"name": "outlook.com", "cost": 0.4, "count": 50},
    ]
}


def _mock_handler(request: httpx.Request) -> httpx.Response:
    """One handler serving both transports.

    Legacy stubs are dispatched on the ``?action=`` query param; v1 REST on the
    request path + method. Bodies are the canned shapes above. Unknown routes
    return 404 so a mis-wired test fails loudly.
    """
    url = request.url
    path = url.path
    method = request.method.upper()

    # ── legacy SMS-Activate stubs: /stubs/handler_api.php?action=... ──────────
    if path.endswith("handler_api.php"):
        action = url.params.get("action")
        if action in _LEGACY:
            return httpx.Response(200, text=json.dumps(_LEGACY[action]))
        return httpx.Response(404, text="BAD_ACTION")

    # ── v1 REST: /api/v1/... ──────────────────────────────────────────────────
    if path == "/api/v1/activations/offers" and method == "GET":
        return httpx.Response(200, text=json.dumps(_V1_OFFERS))
    if path == "/api/v1/emails/domains" and method == "GET":
        return httpx.Response(200, text=json.dumps(_V1_DOMAINS))
    if path == "/api/v1/emails" and method == "POST":
        return httpx.Response(201, text=json.dumps(_V1_EMAIL_CREATED))
    if path.startswith("/api/v1/emails/"):
        email_id = path.rsplit("/", 1)[-1]
        if method == "DELETE":
            return httpx.Response(204)  # cancel -> empty body
        if email_id == "401":  # auth failure case
            return httpx.Response(
                401, text=json.dumps({"title": "Unauthenticated", "details": "Unauthenticated."})
            )
        if email_id == "422":  # validation failure case (per-field errors map)
            return httpx.Response(
                422,
                text=json.dumps(
                    {
                        "title": "Validation failed",
                        "details": "The given data was invalid.",
                        "errors": {"site": ["The site field is required."]},
                    }
                ),
            )
        if method == "GET":
            return httpx.Response(200, text=json.dumps(_V1_EMAIL_STATUS))

    return httpx.Response(404, text=json.dumps({"title": "NOT_FOUND", "details": path}))


def _mock_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler))


# ── legacy client tests ──────────────────────────────────────────────────────
async def test_legacy_mock():
    print("[legacy stubs via MockTransport]")
    mock = _mock_client()
    c = HeroSMSClient(api_key="x", base_url=LEGACY_BASE, client=mock)
    try:
        # getStatusV2 — structured, sms.code surfaced
        sv2 = await c.get_status_v2("635468024")
        check("get_status_v2 sms.code", sv2["sms"]["code"] == "code")
        check("get_status_v2 verificationType", sv2["verificationType"] == 2)

        # getAllSms — data array length + fields
        sms = await c.get_all_sms("635468024")
        check("get_all_sms length", len(sms) == 1)
        check("get_all_sms code", sms[0].get("code") == "12345")

        # getActiveActivations — data parsed
        acts = await c.get_active_activations()
        check("get_active_activations length", len(acts) == 1)
        check("get_active_activations id", acts[0].get("activationId") == "635468021")

        # getHistory — top-level array parsed
        hist = await c.get_history()
        check("get_history length", len(hist) == 1)
        check("get_history id", hist[0].get("id") == "635468024")

        # reactivate (POST) -> Activation fields
        ra = await c.reactivate("123456")
        check("reactivate is Activation", isinstance(ra, Activation))
        check("reactivate id", ra.id == "123456")
        check("reactivate phone", ra.phone == "79991234567")
        check("reactivate cost", ra.cost == Decimal("15.5"))
        check("reactivate can_get_another", ra.can_get_another is True)

        # prolong (POST) -> Activation
        pr = await c.prolong("123456", 24)
        check("prolong id", pr.id == "123456")
        check("prolong cost", pr.cost == Decimal("15.5"))

        # getOperators -> dict
        ops = await c.get_operators()
        check("get_operators parsed", ops.get("countryOperators", {}).get("175", [None])[0] == "optus")
    finally:
        await mock.aclose()


# ── v1 REST client tests ──────────────────────────────────────────────────────
async def test_v1_mock():
    print("[v1 REST via MockTransport]")
    mock = _mock_client()
    c = HeroSMSV1Client(api_key="x", client=mock)
    try:
        # activation_offers -> data.go.6.prices
        offers = await c.activation_offers()
        go6 = offers.get("data", {}).get("go", {}).get("6", {})
        check("activation_offers data.go.6 present", bool(go6))
        check("activation_offers prices.default", go6.get("prices", {}).get("default") == "0.0300")
        check("activation_offers counts.physical", go6.get("counts", {}).get("physical") == 5198)

        # email_purchase (POST 201) -> data envelope
        created = await c.email_purchase("telegram.com", "gmail.com")
        check("email_purchase id", created.get("data", {}).get("id") == 12345)
        check("email_purchase email", created.get("data", {}).get("email") == "john.doe@gmail.com")

        # email_status (GET 200)
        st = await c.email_status("12345")
        check("email_status value", st.get("data", {}).get("value") == "123456")
        check("email_status status", st.get("data", {}).get("status") == "RECEIVED")

        # email_domains (GET 200) -> list under data
        dom = await c.email_domains()
        domains = dom.get("data") if isinstance(dom, dict) else dom
        check("email_domains length", len(domains) == 2)
        check("email_domains name", domains[0].get("name") == "gmail.com")

        # email_cancel (DELETE 204) -> None, no raise
        res = await c.email_cancel("555")
        check("email_cancel returns None", res is None)

        # error case: 401 -> HeroSMSV1Error(code='Unauthenticated')
        raised = None
        try:
            await c.email_status("401")
        except HeroSMSV1Error as e:
            raised = e
        except Exception as e:  # noqa: BLE001
            raised = e
        check(
            "401 -> HeroSMSV1Error",
            isinstance(raised, HeroSMSV1Error)
            and raised.status == 401
            and raised.code == "Unauthenticated",
        )

        # error case: 422 -> HeroSMSV1Error(code='Validation failed') with field errors folded in
        raised2 = None
        try:
            await c.email_status("422")
        except HeroSMSV1Error as e:
            raised2 = e
        except Exception as e:  # noqa: BLE001
            raised2 = e
        check(
            "422 -> HeroSMSV1Error",
            isinstance(raised2, HeroSMSV1Error)
            and raised2.status == 422
            and raised2.code == "Validation failed",
        )
        check(
            "422 message folds field errors",
            isinstance(raised2, HeroSMSV1Error) and "site" in (raised2.message or ""),
        )
    finally:
        await mock.aclose()


async def main():
    await test_legacy_mock()
    await test_v1_mock()
    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    return FAIL


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
