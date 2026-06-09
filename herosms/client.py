"""Async client for the HeroSMS (SMS-Activate-compatible) API.

Endpoint:  GET {base}?api_key=KEY&action=ACTION&...params
HeroSMS mixes two response styles depending on the action:
  * legacy plaintext, e.g.  ``ACCESS_BALANCE:12.34`` / ``STATUS_OK:123456``
  * JSON, e.g. getNumberV2 / getCountries / getServicesList / getPrices, and
    JSON error envelopes like ``{"title":"NO_NUMBERS","details":"..."}``.
This client parses both defensively so a small server-side format change won't
break the bot.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

log = logging.getLogger(__name__)

# Error tokens the SMS-Activate protocol can return as plaintext or JSON `title`.
_ERROR_TOKENS = {
    "BAD_KEY",
    "BAD_ACTION",
    "BAD_SERVICE",
    "BAD_STATUS",
    "ERROR_SQL",
    "NO_NUMBERS",
    "NO_BALANCE",
    "NO_ACTIVATION",
    "NO_ACTIVATIONS",
    "WRONG_ACTIVATION_ID",
    "WRONG_SERVICE",
    "WRONG_SECURITY",
    "BANNED",
    "NO_OPERATIONS",
    "NO_NUMBER",
    "ACCOUNT_INACTIVE",
    "NO_YULA_MAIL",
    "BANNED_DEVICE",
    "UNAUTHORIZED",
}


# getNumberV2 errors that are real business outcomes (must propagate, not fall
# back to legacy getNumber). Anything else (HTTP 404, ROUTE_NOT_FOUND, junk) means
# V2 isn't usable on this endpoint -> fall back.
_V2_PROPAGATE = {
    "NO_NUMBERS", "NO_BALANCE", "BAD_KEY", "BAD_SERVICE", "WRONG_SERVICE",
    "BAD_ACTION", "BANNED", "ACCOUNT_INACTIVE", "UNAUTHORIZED",
}


class HeroSMSError(Exception):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


class NoNumbersError(HeroSMSError):
    """No phone numbers available for the requested service/country."""


class NoBalanceError(HeroSMSError):
    """The HeroSMS account (your master account) is out of funds."""


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


@dataclass
class Activation:
    id: str
    phone: str
    cost: Decimal
    country: str | None = None
    operator: str | None = None
    can_get_another: bool = False


class HeroSMSClient:
    def __init__(self, api_key: str, base_url: str, client: httpx.AsyncClient | None = None):
        self._api_key = api_key
        self._base_url = base_url
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ── low level ───────────────────────────────────────────────────────────
    async def _raw(self, action: str, method: str = "GET", **params) -> str:
        """Transport for the legacy ``?api_key=&action=`` endpoint.

        ``method`` defaults to ``"GET"``; pass ``"POST"`` for the few actions
        the spec declares as POST (reactivate / prolong). Params stay in the
        query string either way (the spec marks them ``in=query``).
        """
        query = {"api_key": self._api_key, "action": action}
        for k, v in params.items():
            if v is not None:
                query[k] = v
        if str(method).upper() == "POST":
            resp = await self._client.post(self._base_url, params=query)
        else:
            resp = await self._client.get(self._base_url, params=query)
        text = resp.text.strip()
        log.debug("HeroSMS %s -> %s", action, text[:300])
        self._raise_for_error(text, resp.status_code)
        return text

    @staticmethod
    def _raise_for_error(text: str, status_code: int) -> None:
        token: str | None = None
        details: str | None = None

        # JSON error envelope: {"title": "...", "details": "..."}
        stripped = text.lstrip()
        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
                if isinstance(data, dict) and "title" in data and "status" not in data:
                    token = str(data.get("title", "")).upper()
                    details = data.get("details")
            except json.JSONDecodeError:
                pass

        # Plaintext token, possibly "TOKEN" or "TOKEN:extra"
        if token is None:
            head = text.split(":", 1)[0].strip().upper()
            if head in _ERROR_TOKENS:
                token = head

        if token is None and status_code >= 400 and not stripped.startswith("{"):
            token = "HTTP_%d" % status_code

        if token is None:
            return
        if token == "NO_NUMBERS":
            raise NoNumbersError(token, details)
        if token == "NO_BALANCE":
            raise NoBalanceError(token, details)
        raise HeroSMSError(token, details)

    @staticmethod
    def _json(text: str):
        return json.loads(text)

    @staticmethod
    def _to_dict(text: str) -> dict:
        """Parse a JSON response to a dict, tolerantly.

        A JSON array is wrapped as ``{"data": [...]}`` so the caller always
        gets a dict; anything unparseable (plaintext sentinel like
        ``OPERATORS_NOT_FOUND``, junk, empty) yields ``{}``.
        """
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return {}
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"data": data}
        return {}

    @staticmethod
    def _parse_activation(text: str) -> "Activation":
        """Parse a getNumberV2-shaped JSON body into an Activation.

        Used by reactivate / prolong (their success bodies mirror getNumberV2).
        Tolerates a ``{"data": {...}}`` envelope. Raises on a missing id/phone.
        """
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            raise HeroSMSError("UNEXPECTED_RESPONSE", str(text)[:200])
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict):
            raise HeroSMSError("UNEXPECTED_RESPONSE", str(data)[:200])
        act_id = data.get("activationId") or data.get("id")
        phone = data.get("phoneNumber") or data.get("phone")
        if not act_id or not phone:
            raise HeroSMSError("UNEXPECTED_RESPONSE", str(data)[:200])
        country = data.get("countryCode")
        return Activation(
            id=str(act_id),
            phone=str(phone),
            cost=_to_decimal(data.get("activationCost", 0)),
            country=str(country) if country is not None else None,
            operator=data.get("activationOperator"),
            can_get_another=bool(data.get("canGetAnotherSms", False)),
        )

    # ── account ─────────────────────────────────────────────────────────────
    async def get_balance(self) -> Decimal:
        text = await self._raw("getBalance")
        # Legacy:  ACCESS_BALANCE:12.34
        if text.upper().startswith("ACCESS_BALANCE"):
            return _to_decimal(text.split(":", 1)[1])
        # JSON:  {"balance": 12.34} or {"data": {"balance": ...}}
        try:
            data = self._json(text)
            if isinstance(data, dict):
                if "balance" in data:
                    return _to_decimal(data["balance"])
                if isinstance(data.get("data"), dict) and "balance" in data["data"]:
                    return _to_decimal(data["data"]["balance"])
        except json.JSONDecodeError:
            pass
        return _to_decimal(text)

    # ── catalog ─────────────────────────────────────────────────────────────
    async def get_services(self) -> list[dict]:
        """Returns a list of {code, name} dicts."""
        text = await self._raw("getServicesList")
        data = self._json(text)
        if isinstance(data, dict) and "services" in data:
            data = data["services"]
        services: list[dict] = []
        if isinstance(data, list):
            for item in data:
                code = item.get("code") or item.get("id") or item.get("service")
                name = item.get("name") or item.get("title") or code
                if code:
                    services.append({"code": str(code), "name": str(name)})
        elif isinstance(data, dict):
            for code, val in data.items():
                name = val.get("name") if isinstance(val, dict) else val
                services.append({"code": str(code), "name": str(name or code)})
        return services

    async def get_countries(self) -> dict[str, str]:
        """Returns {country_id: english_name}."""
        text = await self._raw("getCountries")
        data = self._json(text)
        countries: dict[str, str] = {}
        items = data.values() if isinstance(data, dict) else data
        for item in items:
            if not isinstance(item, dict):
                continue
            cid = item.get("id")
            if cid is None:
                continue
            name = item.get("eng") or item.get("name") or item.get("rus") or str(cid)
            countries[str(cid)] = str(name)
        return countries

    async def get_prices(self, service: str | None = None, country: str | None = None) -> dict:
        """Raw getPrices response: {country_id: {service: {cost, count}}}."""
        text = await self._raw("getPrices", service=service, country=country)
        data = self._json(text)
        return data if isinstance(data, dict) else {}

    async def country_prices_for_service(self, service: str) -> list[dict]:
        """Availability of a service across countries.

        Returns a list of {country, cost(Decimal), count(int)} sorted by cost,
        only including countries that currently have numbers in stock.
        """
        prices = await self.get_prices(service=service)
        out: list[dict] = []
        for country_id, services in prices.items():
            if not isinstance(services, dict):
                continue
            info = services.get(service)
            if not isinstance(info, dict):
                continue
            # `count` from HeroSMS is inflated/virtual; `physicalCount` is the
            # REAL number of numbers available right now — trust that.
            phys = int(info.get("physicalCount", 0) or 0)
            cost = _to_decimal(info.get("cost", 0))
            if cost > 0:
                out.append({"country": str(country_id), "cost": cost, "count": phys})
        # Really-in-stock countries first (physicalCount > 0), then by price.
        out.sort(key=lambda x: (x["count"] == 0, x["cost"]))
        return out

    async def get_operators(self, country: str | None = None) -> dict:
        """getOperators -> {status, countryOperators:{country_id:[operator,...]}}.

        Returns ``{}`` when the provider replies ``OPERATORS_NOT_FOUND`` (not an
        error token) or otherwise has nothing to report.
        """
        text = await self._raw("getOperators", country=country)
        return self._to_dict(text)

    async def get_top_countries_by_service(
        self, service: str, free_price: bool | None = None
    ) -> dict:
        """getTopCountriesByService (deprecated upstream) -> raw top-countries map."""
        free = None if free_price is None else ("true" if free_price else "false")
        text = await self._raw("getTopCountriesByService", service=service, freePrice=free)
        return self._to_dict(text)

    async def get_top_countries_by_service_rank(self, service: str) -> dict:
        """getTopCountriesByServiceRank (deprecated upstream) -> rank-adjusted map."""
        text = await self._raw("getTopCountriesByServiceRank", service=service)
        return self._to_dict(text)

    async def service_count_rent(
        self, service: str, country: str | None = None
    ) -> dict:
        """serviceCountRent -> {country_id: {duration: {count, price}}} for rent.

        ``service`` is required by the spec. Returns ``{}`` when the provider
        replies with an empty body (``{}``).
        """
        text = await self._raw("serviceCountRent", service=service, country=country)
        return self._to_dict(text)

    # ── activations ─────────────────────────────────────────────────────────
    async def get_number(
        self,
        service: str,
        country: str,
        max_price: Decimal | None = None,
        operator: str | None = None,
        url: str | None = None,
    ) -> Activation:
        """Order a number. Uses getNumberV2 (JSON, includes the exact cost).

        ``url`` registers a webhook: HeroSMS POSTs to it when an SMS arrives, so
        the bot delivers the code instantly instead of waiting for the next poll.
        """
        params = {"service": service, "country": country}
        if max_price is not None:
            params["maxPrice"] = str(max_price)
        if operator:
            params["operator"] = operator
        if url:
            params["url"] = url
        # Try getNumberV2. We fall back to legacy getNumber ONLY when V2 is
        # genuinely unavailable on this endpoint (HTTP/route error or non-JSON).
        # A V2 call that returns a parsed JSON body is AUTHORITATIVE — we return
        # it or raise; we NEVER fall through afterwards, because doing so would
        # place a SECOND order (a real number billed to the master account).
        v2_text: str | None = None
        try:
            v2_text = await self._raw("getNumberV2", **params)
        except HeroSMSError as exc:
            if exc.code in _V2_PROPAGATE:
                raise  # real business outcome (NO_NUMBERS, NO_BALANCE, BAD_KEY, …)
            log.info("getNumberV2 unavailable (%s) — falling back to getNumber", exc.code)
            v2_text = None
        if v2_text is not None:
            try:
                data = self._json(v2_text)
            except json.JSONDecodeError:
                log.info("getNumberV2 returned non-JSON — falling back to getNumber")
                data = None
            if data is not None:
                if isinstance(data, dict):
                    act_id = data.get("activationId") or data.get("id")
                    phone = data.get("phoneNumber") or data.get("phone")
                    if act_id and phone:
                        return Activation(
                            id=str(act_id),
                            phone=str(phone),
                            cost=_to_decimal(data.get("activationCost", 0)),
                            country=str(data.get("countryCode", country)),
                            operator=data.get("activationOperator"),
                            can_get_another=bool(data.get("canGetAnotherSms", False)),
                        )
                # Parsed V2 response but not a usable number — authoritative failure,
                # do NOT fall through to a second order.
                raise HeroSMSError("UNEXPECTED_RESPONSE", str(data)[:200])
        # Legacy:  ACCESS_NUMBER:activationId:phoneNumber (only when V2 unavailable)
        text = await self._raw("getNumber", **params)
        if text.upper().startswith("ACCESS_NUMBER"):
            parts = text.split(":")
            return Activation(
                id=parts[1],
                phone=parts[2] if len(parts) > 2 else "",
                cost=max_price or Decimal("0"),
                country=country,
            )
        raise HeroSMSError("UNEXPECTED_RESPONSE", text[:200])

    async def get_status(self, activation_id: str) -> tuple[str, str | None]:
        """Returns (status, code). status is one of:
        WAIT_CODE, WAIT_RETRY, WAIT_RESEND, OK, CANCEL, UNKNOWN.
        ``code`` is set when status is OK (or the last code on WAIT_RETRY)."""
        text = await self._raw("getStatus", id=activation_id)
        upper = text.upper()
        if upper.startswith("STATUS_OK"):
            return "OK", text.split(":", 1)[1] if ":" in text else None
        if upper.startswith("STATUS_WAIT_CODE"):
            return "WAIT_CODE", None
        if upper.startswith("STATUS_WAIT_RETRY"):
            return "WAIT_RETRY", text.split(":", 1)[1] if ":" in text else None
        if upper.startswith("STATUS_WAIT_RESEND"):
            return "WAIT_RESEND", None
        if upper.startswith("STATUS_CANCEL"):
            return "CANCEL", None
        # JSON fallback (getStatusV2-style)
        try:
            data = self._json(text)
            if isinstance(data, dict):
                sms = data.get("sms") or data
                code = sms.get("code") if isinstance(sms, dict) else None
                if code:
                    return "OK", str(code)
        except json.JSONDecodeError:
            pass
        return "UNKNOWN", None

    async def set_status(self, activation_id: str, status: int) -> str:
        """status codes: 1=ready, 3=request another code, 6=complete, 8=cancel."""
        return await self._raw("setStatus", id=activation_id, status=status)

    async def cancel(self, activation_id: str) -> None:
        await self.set_status(activation_id, 8)

    async def finish(self, activation_id: str) -> None:
        await self.set_status(activation_id, 6)

    async def request_another_code(self, activation_id: str) -> None:
        await self.set_status(activation_id, 3)

    async def get_status_v2(self, activation_id: str) -> dict:
        """getStatusV2 -> structured status.

        Returns ``{verificationType, sms:{dateTime,code,text}, call:{...}}`` for
        the JSON form. A plaintext status (``STATUS_CANCEL``, ``STATUS_WAIT_CODE``,
        …) maps to ``{"status": "<TOKEN>"}``; unparseable to ``{"status": "UNKNOWN"}``.
        """
        text = await self._raw("getStatusV2", id=activation_id)
        upper = text.upper()
        if upper.startswith("STATUS_"):
            return {"status": upper.split(":", 1)[0].replace("STATUS_", "", 1)}
        data = self._to_dict(text)
        if not data:
            return {"status": "UNKNOWN"}
        sms = data.get("sms") if isinstance(data.get("sms"), dict) else {}
        call = data.get("call") if isinstance(data.get("call"), dict) else {}
        return {
            "verificationType": data.get("verificationType"),
            "sms": {
                "dateTime": sms.get("dateTime"),
                "code": sms.get("code"),
                "text": sms.get("text"),
            },
            "call": call,
        }

    async def get_all_sms(self, activation_id: str, page: int = 1, size: int = 20) -> list[dict]:
        """getAllSms -> the ``data`` array of SMS dicts for one activation.

        Empty list when there are no messages or the body can't be parsed.
        """
        text = await self._raw("getAllSms", id=activation_id, page=page, size=size)
        data = self._to_dict(text).get("data")
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []

    async def get_active_activations(self, start: int = 0, limit: int = 10) -> list[dict]:
        """getActiveActivations -> the ``data`` array of currently-open activations.

        ``NO_ACTIVATIONS`` (the provider's empty sentinel) is mapped to an empty
        list rather than an error.
        """
        try:
            text = await self._raw("getActiveActivations", start=start, limit=limit)
        except HeroSMSError as exc:
            if exc.code in ("NO_ACTIVATIONS", "NO_ACTIVATION"):
                return []
            raise
        data = self._to_dict(text).get("data")
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []

    async def finish_activation(self, activation_id: str) -> str:
        """finishActivation -> raw provider response (e.g. ``ACCESS_ACTIVATION``)."""
        return await self._raw("finishActivation", id=activation_id)

    async def cancel_activation(self, activation_id: str) -> str:
        """cancelActivation -> raw provider response (e.g. ``ACCESS_CANCEL``)."""
        return await self._raw("cancelActivation", id=activation_id)

    async def reactivate(self, activation_id: str, duration: int | None = None) -> Activation:
        """reactivate (POST) -> a fresh Activation reusing a previously-used number.

        ``duration`` (hours) is optional and only relevant for rent-type numbers.
        """
        text = await self._raw("reactivate", method="POST", id=activation_id, duration=duration)
        return self._parse_activation(text)

    async def reactivate_options(self, activation_id: str) -> dict:
        """reactivateOptions -> ``{"data": {"options": [...]}}`` reactivation costs."""
        text = await self._raw("reactivateOptions", id=activation_id)
        return self._to_dict(text)

    # ── rentals (api/v1-style "duration" in hours; min 24) ───────────────────
    async def rent_services(self, duration: int, country: str) -> dict:
        """getRentServicesAndCountries -> {services:{code:{price,quantity}}, countries, operators}."""
        text = await self._raw("getRentServicesAndCountries", duration=duration, country=country)
        data = self._json(text)
        return data if isinstance(data, dict) else {}

    async def rent_service_prices(self, duration: int, country: str) -> list[dict]:
        """[{code, price(Decimal), quantity}] for a duration+country, in-stock first."""
        data = await self.rent_services(duration, country)
        services = data.get("services", {}) if isinstance(data, dict) else {}
        out: list[dict] = []
        for code, info in services.items():
            if not isinstance(info, dict):
                continue
            price = _to_decimal(info.get("price", info.get("retail_price", 0)))
            qty = int(info.get("quantity", 0) or 0)
            if price > 0:
                out.append({"code": str(code), "price": price, "quantity": qty})
        out.sort(key=lambda x: (x["quantity"] == 0, x["price"]))
        return out

    async def rent_number(
        self, service: str, country: str, duration: int, operator: str | None = None
    ) -> "RentActivation":
        params = {"service": service, "country": country, "duration": duration}
        if operator:
            params["operator"] = operator
        text = await self._raw("getRentNumber", **params)
        data = self._json(text)
        phone = data.get("phone", data) if isinstance(data, dict) else {}
        rent_id = phone.get("id") or phone.get("rentId") or phone.get("activationId")
        number = phone.get("number") or phone.get("phoneNumber") or phone.get("phone")
        if not rent_id or not number:
            raise HeroSMSError("UNEXPECTED_RESPONSE", str(data)[:200])
        return RentActivation(
            id=str(rent_id),
            phone=str(number),
            # HeroSMS returns either legacy ``endDate``/``cost`` or the v1 flat
            # ``activationEndTime``/``activationCost`` — read both so the rental's
            # real end time and price aren't silently lost (cost was parsing to 0).
            end_date=str(phone.get("endDate") or phone.get("end_date")
                         or phone.get("activationEndTime") or ""),
            cost=_to_decimal(phone.get("cost") or phone.get("activationCost") or 0),
        )

    async def rent_status(self, rent_id: str) -> list[dict]:
        """getRentStatus -> list of received SMS [{from, text, service, date}]."""
        text = await self._raw("getRentStatus", id=rent_id)
        try:
            data = self._json(text)
        except json.JSONDecodeError:
            return []
        values = data.get("values") if isinstance(data, dict) else None
        out: list[dict] = []
        if isinstance(values, dict):
            for v in values.values():
                if isinstance(v, dict):
                    out.append({
                        "from": v.get("phoneFrom") or v.get("from") or "",
                        "text": v.get("text") or v.get("smsText") or "",
                        "service": v.get("service") or "",
                        "date": v.get("date") or "",
                    })
        return out

    async def set_rent_status(self, rent_id: str, status: int) -> str:
        """status: 1 = finish, 2 = cancel."""
        return await self._raw("setRentStatus", id=rent_id, status=status)

    async def prolong(self, activation_id: str, duration: int) -> Activation:
        """prolong (POST) -> extend a Rent-type number's session by ``duration`` hours.

        Returns the (same-number) Activation; raises if the body is unusable.
        """
        text = await self._raw("prolong", method="POST", id=activation_id, duration=duration)
        return self._parse_activation(text)

    async def prolong_options(self, activation_id: str) -> dict:
        """prolongOptions -> ``{"data": {"options": [...]}}`` extension costs."""
        text = await self._raw("prolongOptions", id=activation_id)
        return self._to_dict(text)

    async def prolong_history(self, activation_id: str) -> dict:
        """prolongHistory -> ``{"data": [{userPrice,hours,createDate,payerType},...]}``."""
        text = await self._raw("prolongHistory", id=activation_id)
        return self._to_dict(text)

    # ── history ──────────────────────────────────────────────────────────────
    async def get_history(
        self,
        start: int | None = None,
        end: int | None = None,
        offset: int = 0,
        size: int = 10,
    ) -> list[dict]:
        """getHistory -> list of past activations [{id,date,phone,sms,cost,status,...}].

        ``start``/``end`` are optional Unix timestamps bounding the period.
        Returns an empty list if the body is empty or can't be parsed.
        """
        text = await self._raw("getHistory", start=start, end=end, offset=offset, size=size)
        try:
            data = self._json(text)
        except json.JSONDecodeError:
            return []
        if isinstance(data, dict):  # tolerate a {"data": [...]} envelope
            data = data.get("data", [])
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


@dataclass
class RentActivation:
    id: str
    phone: str
    end_date: str = ""
    cost: Decimal = Decimal("0")
