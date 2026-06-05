"""Heleket (formerly Cryptomus) crypto payment provider.

Auth: headers `merchant` (UUID) + `sign` = md5(base64(json_body) + API_KEY).
Docs: https://doc.heleket.com/

Unified provider interface (shared with CryptoPay):
  name
  async make_invoice(amount: Decimal, order_id: str) -> {"invoice_id", "pay_url"}
  async invoice_status(invoice_id: str, order_id: str) -> "paid" | "pending" | "expired"
  async verify() -> None         # sanity-check credentials at startup
  async close()
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from decimal import Decimal

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.heleket.com/v1"

# Heleket payment_status values -> our normalized state.
_PAID = {"paid", "paid_over"}
_FAILED = {"cancel", "fail", "system_fail", "expired"}


class HeleketError(Exception):
    pass


class Heleket:
    name = "Heleket"

    def __init__(self, merchant: str, api_key: str, client: httpx.AsyncClient | None = None):
        self._merchant = merchant
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _sign(self, payload: str) -> str:
        b64 = base64.b64encode(payload.encode()).decode()
        return hashlib.md5((b64 + self._api_key).encode()).hexdigest()

    async def _call(self, method: str, body: dict) -> dict:
        # The signed string must be byte-identical to what we send.
        payload = json.dumps(body)
        headers = {
            "merchant": self._merchant,
            "sign": self._sign(payload),
            "Content-Type": "application/json",
        }
        resp = await self._client.post(f"{BASE}/{method}", headers=headers, content=payload)
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise HeleketError(f"bad response [{resp.status_code}]: {resp.text[:200]}") from exc
        if data.get("state") != 0:
            raise HeleketError(data.get("message") or str(data)[:200])
        return data.get("result", {})

    async def verify(self) -> None:
        """Raise if the merchant/api key are wrong (used at startup)."""
        await self._call("payment/services", {})

    async def make_invoice(self, amount: Decimal, order_id: str, callback: str | None = None) -> dict:
        # Price fixed in USD, but the customer pays ONLY in USDT (to_currency locks
        # the invoice to a single crypto — no coin picker on the payment page).
        body = {
            "amount": str(amount),
            "currency": "USD",
            "to_currency": "USDT",
            "order_id": order_id,
        }
        if callback:
            body["url_callback"] = callback
        r = await self._call("payment", body)
        return {"invoice_id": str(r.get("uuid", "")), "pay_url": r.get("url", "")}

    async def invoice_status(self, invoice_id: str, order_id: str) -> str:
        body = {"uuid": invoice_id} if invoice_id else {"order_id": order_id}
        try:
            r = await self._call("payment/info", body)
        except HeleketError:
            return "pending"  # transient — try again next cycle
        status = str(r.get("payment_status", "")).lower()
        if status in _PAID:
            return "paid"
        if status in _FAILED:
            return "expired"
        return "pending"
