"""OxaPay crypto payment provider (oxapay.com) — no-KYC, instant.

Auth: header `merchant_api_key`. Docs: https://docs.oxapay.com/api-reference/payment
  POST https://api.oxapay.com/v1/payment/invoice    -> create invoice
  GET  https://api.oxapay.com/v1/payment/{track_id}  -> payment info / status

Unified provider interface (shared with CryptoPay):
  name
  async make_invoice(amount: Decimal, order_id: str) -> {"invoice_id", "pay_url"}
  async invoice_status(invoice_id: str, order_id: str) -> "paid" | "pending" | "expired"
  async verify() -> None         # sanity-check the merchant key at startup
  async close()
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from decimal import Decimal

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.oxapay.com/v1"

# OxaPay invoice status (lower-cased) -> our normalized state. Only fully-settled
# states map to "paid" (never "waiting"/"confirming"), so we credit exactly once.
_PAID = {"paid", "completed", "confirmed"}
_FAILED = {"expired", "failed", "canceled", "cancelled", "refunded"}


class OxaPayError(Exception):
    pass


class OxaPay:
    name = "OxaPay"

    def __init__(self, api_key: str, asset: str = "USDT", client: httpx.AsyncClient | None = None,
                 general_api_key: str = ""):
        self._api_key = api_key
        self._asset = asset
        # OxaPay's account-balance endpoint needs the *General* API key (separate
        # from the merchant key). Optional — only used for the admin balance view.
        self._general_api_key = general_api_key
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict:
        return {"merchant_api_key": self._api_key, "Content-Type": "application/json"}

    def _unwrap(self, resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise OxaPayError(f"bad response [{resp.status_code}]: {resp.text[:200]}") from exc
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise OxaPayError((err.get("message") if isinstance(err, dict) else str(err)) or "request failed")
        if resp.status_code >= 400:
            raise OxaPayError(f"HTTP {resp.status_code}: {str(data)[:200]}")
        return data.get("data", data) if isinstance(data, dict) else {}

    async def _create_invoice(self, amount, order_id: str, *, sandbox: bool = False,
                              callback: str | None = None) -> dict:
        # Price fixed in USD. We do NOT pin to_currency, so the payer can pay in
        # ANY accepted coin; the merchant's "Auto-Convert to USDT" setting settles
        # the balance in USDT. (The bot credits the fixed USD amount regardless.)
        body = {
            "amount": float(Decimal(str(amount))),
            "currency": "USD",
            "lifetime": 60,                # minutes (15–2880)
            "fee_paid_by_payer": 1,        # payer covers the network fee
            "order_id": order_id,
            "description": f"NumberHub top-up {order_id}",
            "sandbox": sandbox,
        }
        if callback:
            body["callback_url"] = callback
        resp = await self._client.post(f"{BASE}/payment/invoice", headers=self._headers(), json=body)
        return self._unwrap(resp)

    async def verify(self) -> None:
        """Validate the merchant key at startup with a SANDBOX invoice (no real charge)."""
        await self._create_invoice(1, "nh-verify", sandbox=True)

    async def make_invoice(self, amount: Decimal, order_id: str, callback: str | None = None) -> dict:
        r = await self._create_invoice(amount, order_id, callback=callback)
        return {"invoice_id": str(r.get("track_id", "")), "pay_url": r.get("payment_url", "")}

    async def balance(self) -> dict[str, Decimal]:
        """Merchant account balance per currency (non-zero only). Needs the OxaPay
        General API key (GET /v1/general/account/balance). Raises if unavailable."""
        if not self._general_api_key:
            raise OxaPayError("OxaPay General API key not configured")
        resp = await self._client.get(
            f"{BASE}/general/account/balance",
            headers={"general_api_key": self._general_api_key},
        )
        data = self._unwrap(resp)  # {currency: amount, ...}
        out: dict[str, Decimal] = {}
        if isinstance(data, dict):
            for cur, amt in data.items():
                try:
                    d = Decimal(str(amt))
                except Exception:  # noqa: BLE001
                    continue
                if d > 0:
                    out[str(cur)] = d
        return out

    async def invoice_status(self, invoice_id: str, order_id: str) -> str:
        if not invoice_id:
            return "pending"
        try:
            resp = await self._client.get(f"{BASE}/payment/{invoice_id}", headers=self._headers())
            r = self._unwrap(resp)
        except OxaPayError:
            return "pending"  # transient provider/network error — retry next cycle
        return self.normalize_status(r.get("status", ""))

    @staticmethod
    def normalize_status(raw_status) -> str:
        """Map an OxaPay status (e.g. 'Paid'/'Paying'/'Expired') to our normalized
        'paid' | 'pending' | 'expired'. Used by both polling and the webhook so the
        two crediting paths agree on what 'settled' means."""
        s = str(raw_status or "").lower()
        if s in _PAID:
            return "paid"
        if s in _FAILED:
            return "expired"
        return "pending"

    @staticmethod
    def verify_callback(raw_body: bytes, signature: str, api_key: str) -> bool:
        """Verify an OxaPay webhook. OxaPay signs the EXACT raw POST bytes with
        HMAC-SHA512 keyed by the merchant API key and sends the hex digest in the
        `HMAC` header. Constant-time compare; reject on any missing piece — an
        unverified callback could otherwise mint free credit."""
        if not signature or not api_key or not raw_body:
            return False
        expected = hmac.new(api_key.encode(), raw_body, hashlib.sha512).hexdigest()
        return hmac.compare_digest(expected, signature.strip())
