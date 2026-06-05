"""Crypto Pay API client (@CryptoBot / @CryptoTestnetBot).

Docs: https://help.crypt.bot/crypto-pay-api

We create an invoice for a top-up and poll its status. Polling means the bot
needs no public webhook URL, so it runs anywhere (even behind NAT).
"""
from __future__ import annotations

import logging
from decimal import Decimal

import httpx

log = logging.getLogger(__name__)

MAINNET = "https://pay.crypt.bot/api"
TESTNET = "https://testnet-pay.crypt.bot/api"


class CryptoPayError(Exception):
    pass


class CryptoPay:
    name = "CryptoBot"

    def __init__(self, token: str, testnet: bool = False, asset: str = "USDT",
                 client: httpx.AsyncClient | None = None):
        self._token = token
        self._asset = asset
        self._base = TESTNET if testnet else MAINNET
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _call(self, method: str, **params) -> dict:
        resp = await self._client.post(
            f"{self._base}/{method}",
            headers={"Crypto-Pay-API-Token": self._token},
            json={k: v for k, v in params.items() if v is not None},
        )
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise CryptoPayError(f"Bad response: {resp.text[:200]}") from exc
        if not data.get("ok"):
            err = data.get("error", {})
            raise CryptoPayError(f"{err.get('code')}: {err.get('name')}")
        return data["result"]

    async def get_me(self) -> dict:
        return await self._call("getMe")

    async def create_invoice(self, amount: Decimal, asset: str, description: str, payload: str) -> dict:
        """Returns the invoice. Key fields: invoice_id, status, bot_invoice_url."""
        return await self._call(
            "createInvoice",
            currency_type="crypto",
            asset=asset,
            amount=str(amount),
            description=description,
            payload=payload,
            allow_comments=False,
            allow_anonymous=True,
            expires_in=3600,
        )

    async def get_invoice(self, invoice_id: str | int) -> dict | None:
        result = await self._call("getInvoices", invoice_ids=str(invoice_id))
        items = result.get("items", [])
        return items[0] if items else None

    async def get_invoices(self, status: str | None = None, count: int = 100) -> list[dict]:
        result = await self._call("getInvoices", status=status, count=count)
        return result.get("items", [])

    # ── unified provider interface (shared with Heleket) ────────────────────
    async def verify(self) -> None:
        await self.get_me()

    async def make_invoice(self, amount, order_id: str) -> dict:
        inv = await self.create_invoice(
            amount=amount, asset=self._asset,
            description=f"Top up {order_id}", payload=order_id,
        )
        url = (inv.get("bot_invoice_url") or inv.get("mini_app_invoice_url")
               or inv.get("web_app_invoice_url") or inv.get("pay_url"))
        return {"invoice_id": str(inv["invoice_id"]), "pay_url": url}

    async def invoice_status(self, invoice_id: str, order_id: str) -> str:
        try:
            inv = await self.get_invoice(invoice_id)
        except CryptoPayError:
            return "pending"
        if inv and inv.get("status") == "paid":
            return "paid"
        if inv and inv.get("status") == "expired":
            return "expired"
        return "pending"
