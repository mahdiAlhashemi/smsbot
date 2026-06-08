"""Async client for the HeroSMS **v1 REST** API.

This is a *separate transport* from ``herosms/client.py`` (the SMS-Activate
compatible legacy stubs). Here every call is JSON over REST:

  * Server:   ``https://hero-sms.com/api/v1``
  * Auth:     header ``Authorization: ApiKey {token}`` on **every** request
  * Success:  ``2xx`` with a JSON body; the payload lives under ``data`` and
              list endpoints add a ``meta`` block (pagination). DELETE returns
              an empty body (``None`` here).
  * Errors:   non-2xx with an envelope ``{"title": <code>, "details": <msg>}``
              (422 adds an ``errors`` map of per-field messages; 500/429 pin
              ``title`` to ``SERVER_ERROR`` / ``RATE_LIMIT``). Any non-2xx is
              raised as :class:`HeroSMSV1Error` carrying ``status`` / ``code``
              / ``message``.

Parsing is defensive: a missing/odd body never throws past the error wrapper —
it degrades to ``HTTP_<status>`` / ``INVALID_JSON`` codes instead.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation

import httpx

log = logging.getLogger(__name__)


class HeroSMSV1Error(Exception):
    """Raised for any non-2xx response from the HeroSMS v1 REST API.

    ``status``  - HTTP status code (int)
    ``code``    - the envelope ``title`` (e.g. ``Unauthenticated``, ``RATE_LIMIT``,
                  ``OFFER_NOT_FOUND``) or a synthetic ``HTTP_<n>`` / ``INVALID_JSON``.
    ``message`` - the envelope ``details`` (human-readable), if any.
    """

    def __init__(self, status: int, code: str, message: str | None = None):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"[{status}] {code}: {message}" if message else f"[{status}] {code}")


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


class HeroSMSV1Client:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://hero-sms.com/api/v1",
        client: httpx.AsyncClient | None = None,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ── low level ───────────────────────────────────────────────────────────
    @staticmethod
    def _json(text: str):
        return json.loads(text)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> dict | list | None:
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"ApiKey {self._api_key}",
            "Accept": "application/json",
        }
        # Drop unset query params so we never send ``?x=None``.
        clean_params: dict | None = None
        if params:
            clean_params = {k: v for k, v in params.items() if v is not None}
        resp = await self._client.request(
            method, url, params=clean_params, json=json, headers=headers
        )
        text = (resp.text or "").strip()
        log.debug("HeroSMSV1 %s %s -> %s %s", method, path, resp.status_code, text[:300])

        if resp.status_code >= 400:
            self._raise_for_status(resp.status_code, text)

        # 204 No Content / empty body (e.g. DELETE) -> nothing to parse.
        if not text:
            return None
        try:
            return self._json(text)
        except json.JSONDecodeError:
            raise HeroSMSV1Error(resp.status_code, "INVALID_JSON", text[:200])

    def _raise_for_status(self, status_code: int, text: str) -> None:
        """Translate a non-2xx response into :class:`HeroSMSV1Error`.

        Parses the ``{"title", "details", "errors"?}`` envelope defensively;
        falls back to ``HTTP_<status>`` when the body is missing or not JSON.
        """
        code: str | None = None
        message: str | None = None

        stripped = text.lstrip()
        if stripped.startswith("{"):
            try:
                body = self._json(stripped)
            except json.JSONDecodeError:
                body = None
            if isinstance(body, dict):
                title = body.get("title")
                if title:
                    code = str(title)
                details = body.get("details")
                if details:
                    message = str(details)
                # 422 carries a per-field ``errors`` map — fold it into the
                # message so callers see *which* field failed.
                errors = body.get("errors")
                if errors:
                    message = f"{message} {errors}".strip() if message else str(errors)

        if not code:
            code = f"HTTP_{status_code}"
        raise HeroSMSV1Error(status_code, code, message)

    # ── activations ─────────────────────────────────────────────────────────
    async def activation_offers(
        self, services: str | None = None, countries: str | None = None
    ) -> dict:
        """GET /activations/offers — service/country price+count offer map."""
        return await self._request(
            "GET",
            "/activations/offers",
            params={"services": services, "countries": countries},
        )

    # ── emails ──────────────────────────────────────────────────────────────
    async def emails(
        self,
        search: str | None = None,
        size: int | None = None,
        page: int | None = None,
        sort=None,
        status=None,
        active: bool | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        """GET /emails — list active email activations.

        ``date_from``/``date_to`` map to the API's ``from``/``to`` query params
        (ISO-8601). Returns the envelope ``{"data": [...], "meta": {...}}``.
        """
        params = {
            "search": search,
            "size": size,
            "page": page,
            "sort": sort,
            "status": status,
            "active": active,
            "from": date_from,
            "to": date_to,
        }
        return await self._request("GET", "/emails", params=params)

    async def email_purchase(self, site: str, domain: str) -> dict:
        """POST /emails — buy one email activation for ``site`` on ``domain``.

        The spec marks BOTH ``site`` and ``domain`` required (omitting domain
        returns 422), so both are mandatory here."""
        return await self._request("POST", "/emails", json={"site": site, "domain": domain})

    async def email_batch(self, site: str, domain: str, count: int,
                          service: str | None = None) -> dict:
        """POST /emails/batch — buy ``count`` (1-10) email activations in bulk.

        Only ``site``/``domain``/``count`` are required; ``service`` is optional."""
        body: dict = {"site": site, "domain": domain, "count": count}
        if service is not None:
            body["service"] = service
        return await self._request("POST", "/emails/batch", json=body)

    async def email_status(self, email_id) -> dict:
        """GET /emails/{emailId} — status of one email activation."""
        return await self._request("GET", f"/emails/{email_id}")

    async def email_cancel(self, email_id) -> None:
        """DELETE /emails/{emailId} — cancel an email activation (no body)."""
        await self._request("DELETE", f"/emails/{email_id}")
        return None

    async def email_reorder(self, email_id) -> dict:
        """POST /emails/{emailId}/reorder — reorder a finished email activation."""
        return await self._request("POST", f"/emails/{email_id}/reorder")

    async def email_domains(self, site: str | None = None) -> dict | list:
        """GET /emails/domains — available mail domains (opt. filtered by ``site``)."""
        return await self._request("GET", "/emails/domains", params={"site": site})
