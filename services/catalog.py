"""In-memory, TTL-cached view over the HeroSMS catalog.

HeroSMS service/country/price endpoints are slow and rate-limited, so we cache
them. ``services``/``countries`` change rarely; per-service price+stock lists are
cached briefly because stock moves.
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from herosms import HeroSMSClient

# Popular service codes shown first (verified against the live HeroSMS catalog).
POPULAR_SERVICES = [
    ("ot", "Any other"),
    ("tg", "Telegram"),
    ("wa", "WhatsApp"),
    ("dr", "OpenAI / ChatGPT"),
    ("ig", "Instagram / Threads"),
    ("fb", "Facebook"),
    ("go", "Google / Gmail / YouTube"),
    ("tw", "Twitter / X"),
    ("lf", "TikTok"),
    ("ds", "Discord"),
    ("wx", "Apple"),
    ("mt", "Steam"),
    ("am", "Amazon"),
    ("nf", "Netflix"),
    ("ts", "PayPal"),
    ("oi", "Tinder"),
    ("ub", "Uber"),
    ("vi", "Viber"),
    ("mm", "Microsoft"),
    ("bw", "Signal"),
    ("fu", "Snapchat"),
    ("tn", "LinkedIn"),
    ("wb", "WeChat"),
]


class _TTL:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self._value = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    def fresh(self) -> bool:
        return self._value is not None and (time.monotonic() - self._at) < self.ttl

    def set(self, value) -> None:
        self._value = value
        self._at = time.monotonic()

    @property
    def value(self):
        return self._value


class Catalog:
    def __init__(self, client: HeroSMSClient):
        self._client = client
        self._services = _TTL(600)
        self._countries = _TTL(600)
        self._prices: dict[str, _TTL] = {}
        self._prices_lock = asyncio.Lock()
        # Per-duration list of country ids that actually have rentals in stock.
        self._rent_stock: dict[int, _TTL] = {}
        self._rent_stock_lock = asyncio.Lock()

    async def services(self) -> list[dict]:
        async with self._services._lock:
            if not self._services.fresh():
                try:
                    self._services.set(self._prioritize(await self._client.get_services()))
                except Exception:  # noqa: BLE001
                    if self._services.value is None:
                        self._services.set([{"code": c, "name": n} for c, n in POPULAR_SERVICES])
            return self._services.value

    # Services that are not SMS activations (rentals etc.) — hidden from Buy SMS.
    HIDDEN_SERVICES = {"full"}

    @staticmethod
    def _prioritize(services: list[dict]) -> list[dict]:
        """Put popular services first (in our order), then the rest as-is.

        The live catalog has ~800 services in arbitrary order; without this the
        first page of the buy menu would be unusable.
        """
        services = [s for s in services if s["code"] not in Catalog.HIDDEN_SERVICES]
        by_code = {s["code"]: s for s in services}
        order = [code for code, _ in POPULAR_SERVICES]
        front = [by_code[c] for c in order if c in by_code]
        promoted = {c for c in order if c in by_code}
        rest = [s for s in services if s["code"] not in promoted]
        return front + rest

    async def service_name(self, code: str) -> str:
        for svc in await self.services():
            if svc["code"] == code:
                return svc["name"]
        for c, n in POPULAR_SERVICES:
            if c == code:
                return n
        return code.upper()

    async def countries(self) -> dict[str, str]:
        async with self._countries._lock:
            if not self._countries.fresh():
                try:
                    self._countries.set(await self._client.get_countries())
                except Exception:  # noqa: BLE001
                    if self._countries.value is None:
                        self._countries.set({})
            return self._countries.value

    async def country_name(self, country_id: str) -> str:
        return (await self.countries()).get(str(country_id), f"Country {country_id}")

    async def prices_for_service(self, service: str) -> list[dict]:
        """[{country, cost, count}] for a service, sorted cheapest first."""
        async with self._prices_lock:
            ttl = self._prices.get(service)
            if ttl is None:
                ttl = self._prices[service] = _TTL(60)
        async with ttl._lock:
            if not ttl.fresh():
                ttl.set(await self._client.country_prices_for_service(service))
            return ttl.value

    async def search_services(self, query: str, limit: int = 30) -> list[dict]:
        query = query.strip().lower()
        if not query:
            return []
        results = []
        for svc in await self.services():
            if query in svc["code"].lower() or query in svc["name"].lower():
                results.append(svc)
            if len(results) >= limit:
                break
        return results

    def invalidate_prices(self, service: str | None = None) -> None:
        if service is None:
            self._prices.clear()
        else:
            self._prices.pop(service, None)

    # ── rentals: which countries actually have stock for a given duration ──────
    async def rent_countries_in_stock(self, duration: int, all_ids: list[int]) -> list[int]:
        """Subset of ``all_ids`` that genuinely have rentable services for
        ``duration``. HeroSMS lists the SAME static country set for every
        duration, but longer rentals (3d/1w/1m) are out of stock in many of them
        — so the raw list dead-ends with 'no rentals here'. We probe each country
        once (bounded concurrency) and cache per duration. Falls back to the full
        list if the scan can't run, so we never wrongly show an empty menu."""
        async with self._rent_stock_lock:
            ttl = self._rent_stock.get(duration)
            if ttl is None:
                ttl = self._rent_stock[duration] = _TTL(300)
        async with ttl._lock:
            if not ttl.fresh():
                ttl.set(await self._scan_rent_stock(duration, all_ids))
            return ttl.value or list(all_ids)

    async def _scan_rent_stock(self, duration: int, all_ids: list[int]) -> list[int]:
        sem = asyncio.Semaphore(5)  # HeroSMS is rate-limited; keep it gentle

        async def has_stock(cid: int):
            async with sem:
                try:
                    rows = await self._client.rent_service_prices(duration, str(cid))
                    return cid if rows else None
                except Exception:  # noqa: BLE001
                    return None  # treat probe errors as "unknown" -> drop from list

        results = await asyncio.gather(*(has_stock(c) for c in all_ids))
        return [c for c in results if c is not None]
