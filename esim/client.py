"""Async client for the eSIM Access (esimaccess.com / RedteaGO) reseller API.

Endpoint: ``POST {base}/api/v1/open/<action>`` with a JSON body.
All responses share the envelope ``{success, errorCode, errorMsg, obj}``.

Auth — every request carries four headers (see the published Postman collection):
  RT-AccessCode : the account access code
  RT-Timestamp  : current time in milliseconds
  RT-RequestID  : a fresh UUID v4
  RT-Signature  : hex( HMAC_SHA256( timestamp + requestId + accessCode + rawBody, secretKey ) )
The signature is computed over the EXACT raw body string that is sent, so we
serialise the body once and sign/send the same bytes.

Money: the API quotes prices in 1/10000 USD (e.g. 112500 == $11.25). This client
converts at the boundary — callers work in USD ``Decimal``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import httpx

log = logging.getLogger(__name__)

# API price unit: 1 USD == 10000 units.
PRICE_UNIT = Decimal("10000")


def units_to_usd(units) -> Decimal:
    try:
        return (Decimal(str(units)) / PRICE_UNIT)
    except Exception:  # noqa: BLE001
        return Decimal("0")


def usd_to_units(usd: Decimal) -> int:
    return int((Decimal(str(usd)) * PRICE_UNIT).to_integral_value())


def format_data(volume_bytes) -> str:
    """Human data size: '100MB', '1GB', '10GB' — never scientific notation."""
    try:
        v = int(volume_bytes or 0)
    except (TypeError, ValueError):
        return "—"
    if v <= 0:
        return "—"
    gb = Decimal(v) / Decimal(1024 ** 3)
    if gb < 1:
        mb = (Decimal(v) / Decimal(1024 ** 2)).quantize(Decimal("1"))
        return f"{format(mb, 'f')}MB"
    gb = gb.quantize(Decimal("0.01"))
    # format(..., 'f') forces fixed-point so 10 never becomes '1E+1'.
    return f"{format(gb.normalize(), 'f')}GB"


class EsimError(Exception):
    def __init__(self, code: str | None, message: str | None = None):
        self.code = code or "ERROR"
        super().__init__(message or self.code)


@dataclass
class EsimPackage:
    code: str                 # packageCode — what you order
    name: str
    cost: Decimal             # our wholesale cost in USD
    slug: str = ""
    location: str = ""        # ISO-2 codes, comma-separated (e.g. "US,CA")
    volume_bytes: int = 0
    duration: int = 0
    duration_unit: str = "DAY"
    speed: str = ""
    description: str = ""
    region_names: list[str] = field(default_factory=list)

    @property
    def gb(self) -> str:
        return format_data(self.volume_bytes)


@dataclass
class EsimProfile:
    esim_tran_no: str = ""
    order_no: str = ""
    transaction_id: str = ""
    iccid: str = ""
    ac: str = ""              # LPA activation string: "LPA:1$smdp$matchingId"
    qr_code_url: str = ""
    short_url: str = ""
    smdp_status: str = ""
    esim_status: str = ""
    expired_time: str = ""
    total_volume: int = 0
    total_duration: int = 0
    duration_unit: str = "DAY"
    package_name: str = ""

    @property
    def smdp_address(self) -> str:
        # "LPA:1$rsp.example.com$MATCHING" -> "rsp.example.com"
        parts = self.ac.split("$")
        return parts[1] if len(parts) > 2 else ""

    @property
    def matching_id(self) -> str:
        parts = self.ac.split("$")
        return parts[2] if len(parts) > 2 else ""

    @property
    def ready(self) -> bool:
        """True once the profile is provisioned and a QR code is available."""
        return bool(self.ac and self.qr_code_url)


class EsimAccessClient:
    def __init__(
        self,
        access_code: str,
        secret_key: str,
        base_url: str = "https://api.esimaccess.com",
        client: httpx.AsyncClient | None = None,
    ):
        self._access_code = access_code
        self._secret_key = secret_key.encode()
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=40.0)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ── low level ────────────────────────────────────────────────────────────
    def _headers(self, raw_body: str) -> dict:
        ts = str(int(time.time() * 1000))
        rid = str(uuid.uuid4())
        sign_str = ts + rid + self._access_code + raw_body
        sig = hmac.new(self._secret_key, sign_str.encode(), hashlib.sha256).hexdigest()
        return {
            "Content-Type": "application/json",
            "RT-AccessCode": self._access_code,
            "RT-Timestamp": ts,
            "RT-RequestID": rid,
            "RT-Signature": sig,
        }

    async def _post(self, action: str, payload: dict | None = None) -> dict:
        # Serialise once and sign/send the exact same bytes.
        raw = json.dumps(payload or {}, separators=(",", ":"), ensure_ascii=False)
        url = f"{self._base_url}/api/v1/open/{action}"
        resp = await self._client.post(url, content=raw.encode("utf-8"), headers=self._headers(raw))
        text = resp.text
        log.debug("eSIM %s -> %s", action, text[:300])
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EsimError(f"HTTP_{resp.status_code}", text[:200]) from exc
        if not isinstance(data, dict):
            raise EsimError("BAD_RESPONSE", text[:200])
        if not data.get("success"):
            raise EsimError(str(data.get("errorCode")), data.get("errorMsg") or "request failed")
        return data.get("obj") or {}

    # ── account ──────────────────────────────────────────────────────────────
    async def balance(self) -> Decimal:
        """Merchant balance in USD."""
        obj = await self._post("balance/query")
        # obj may be {balance: <units>} or {balanceAmount: ...}; tolerate both.
        for k in ("balance", "balanceAmount", "amount"):
            if k in obj:
                return units_to_usd(obj[k])
        return Decimal("0")

    # ── catalog ──────────────────────────────────────────────────────────────
    async def packages(
        self,
        location_code: str | None = None,
        package_type: str | None = None,
        slug: str | None = None,
        package_code: str | None = None,
        iccid: str | None = None,
    ) -> list[EsimPackage]:
        payload = {
            "locationCode": location_code or "",
            "type": package_type or "",
            "slug": slug or "",
            "packageCode": package_code or "",
            "iccid": iccid or "",
        }
        obj = await self._post("package/list", payload)
        out: list[EsimPackage] = []
        for p in obj.get("packageList", []) or []:
            if not isinstance(p, dict):
                continue
            regions = [
                r.get("locationName", "")
                for r in (p.get("locationNetworkList") or [])
                if isinstance(r, dict)
            ]
            out.append(
                EsimPackage(
                    code=str(p.get("packageCode", "")),
                    name=str(p.get("name", "")),
                    cost=units_to_usd(p.get("price", 0)),
                    slug=str(p.get("slug", "")),
                    location=str(p.get("location", "")),
                    volume_bytes=int(p.get("volume", 0) or 0),
                    duration=int(p.get("duration", 0) or 0),
                    duration_unit=str(p.get("durationUnit", "DAY")),
                    speed=str(p.get("speed", "")),
                    description=str(p.get("description", "")),
                    region_names=[r for r in regions if r],
                )
            )
        return out

    async def regions(self) -> list[dict]:
        """Supported regions/locations: [{locationName, locationCode, ...}]."""
        obj = await self._post("location/list", {})
        for key in ("locationList", "list", "regionList"):
            if isinstance(obj.get(key), list):
                return obj[key]
        # Some deployments return the list directly under obj.
        return obj.get("obj", []) if isinstance(obj.get("obj"), list) else []

    # ── ordering ─────────────────────────────────────────────────────────────
    async def order(
        self, transaction_id: str, package_code: str, price_usd: Decimal, count: int = 1
    ) -> str:
        """Place an order. Returns the orderNo. SPENDS REAL MONEY."""
        units = usd_to_units(price_usd)
        payload = {
            "transactionId": transaction_id,
            "amount": units * count,
            "packageInfoList": [
                {"packageCode": package_code, "count": count, "price": units}
            ],
        }
        obj = await self._post("esim/order", payload)
        return str(obj.get("orderNo") or obj.get("orderNumber") or "")

    async def query(
        self, order_no: str | None = None, iccid: str | None = None,
        esim_tran_no: str | None = None, page_size: int = 50,
    ) -> list[EsimProfile]:
        payload: dict = {"pager": {"pageNum": 1, "pageSize": page_size}}
        if order_no:
            payload["orderNo"] = order_no
        if iccid:
            payload["iccid"] = iccid
        if esim_tran_no:
            payload["esimTranNo"] = esim_tran_no
        obj = await self._post("esim/query", payload)
        out: list[EsimProfile] = []
        for e in obj.get("esimList", []) or []:
            if not isinstance(e, dict):
                continue
            pkg = (e.get("packageList") or [{}])
            pkg0 = pkg[0] if pkg and isinstance(pkg[0], dict) else {}
            out.append(
                EsimProfile(
                    esim_tran_no=str(e.get("esimTranNo", "")),
                    order_no=str(e.get("orderNo", "")),
                    transaction_id=str(e.get("transactionId", "")),
                    iccid=str(e.get("iccid", "")),
                    ac=str(e.get("ac", "")),
                    qr_code_url=str(e.get("qrCodeUrl", "")),
                    short_url=str(e.get("shortUrl", "")),
                    smdp_status=str(e.get("smdpStatus", "")),
                    esim_status=str(e.get("esimStatus", "")),
                    expired_time=str(e.get("expiredTime", "")),
                    total_volume=int(e.get("totalVolume", 0) or 0),
                    total_duration=int(e.get("totalDuration", 0) or 0),
                    duration_unit=str(e.get("durationUnit", "DAY")),
                    package_name=str(pkg0.get("packageName", "")),
                )
            )
        return out

    async def cancel(self, esim_tran_no: str) -> None:
        await self._post("esim/cancel", {"esimTranNo": esim_tran_no})

    async def usage(self, esim_tran_nos: list[str]) -> list[dict]:
        obj = await self._post("esim/usage/query", {"esimTranNoList": esim_tran_nos})
        return obj.get("esimList") or obj.get("list") or []
