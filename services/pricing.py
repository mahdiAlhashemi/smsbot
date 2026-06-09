"""Selling-price calculation and the runtime-editable pricing knobs.

SMART PRICING — two knobs (both admin-editable, stored in the settings table):

  * bid_premium %  — how far ABOVE the provider's default/floor price the bot is
    willing to BID for a number. HeroSMS is a demand auction: the API shows a low
    "default" price, but the real market price is higher and whoever bids more
    gets a number faster (this is the website's `priceMinAvailable`). Bidding
    above the floor is how the bot wins numbers the floor price can't get.
  * markup % (commission) — the reseller's profit, added on top of the bid ceiling.

  buy_ceiling    = max(default * (1 + bid_premium/100), min_bid)   # bot pays UP TO this
  customer_price = buy_ceiling * (1 + markup/100)                  # customer pays this

The bot bids `buy_ceiling` (maxPrice, NO fixedPrice) so it pays the real market
price *up to* that ceiling — usually less. Commission is therefore ALWAYS
preserved: the customer price is the ceiling + markup, and the ceiling is the
most the bot can ever pay, so profit >= ceiling * markup% > 0 on every order.
"""
from __future__ import annotations

import json
from decimal import ROUND_CEILING, Decimal

from config import settings
from db import repo

_MARKUP_KEY = "markup_percent"        # SMS/rent commission %
_BID_KEY = "bid_premium_percent"      # bid premium %
_ESIM_KEY = "esim_commission_percent"  # eSIM commission %
_EMAIL_KEY = "email_commission_percent"  # Email OTP commission %
_OVERRIDES_KEY = "markup_overrides"   # JSON {"svc:tg": 45, "cc:187": 30}
_CENT = Decimal("0.01")


async def get_markup_overrides() -> dict:
    """Per-service / per-country commission overrides (commission %, not price)."""
    try:
        raw = await repo.get_setting(_OVERRIDES_KEY)
    except Exception:  # noqa: BLE001
        return {}
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


async def set_markup_overrides(overrides: dict) -> None:
    await repo.set_setting(_OVERRIDES_KEY, json.dumps(overrides))


def commission_for(service, country, overrides: dict, global_commission: Decimal) -> Decimal:
    """Resolve the commission for a buy: service override > country override >
    global. `overrides` is the pre-fetched map (one read for a whole list)."""
    if service is not None and f"svc:{service}" in overrides:
        try:
            return Decimal(str(overrides[f"svc:{service}"]))
        except Exception:  # noqa: BLE001
            pass
    if country is not None and f"cc:{country}" in overrides:
        try:
            return Decimal(str(overrides[f"cc:{country}"]))
        except Exception:  # noqa: BLE001
            pass
    return global_commission


async def _get_pct(key: str, default: Decimal) -> Decimal:
    """Read a percentage setting, never letting a bad/missing value break a buy."""
    try:
        raw = await repo.get_setting(key)
    except Exception:  # noqa: BLE001
        return default
    if raw is None:
        return default
    try:
        return Decimal(raw)
    except Exception:  # noqa: BLE001
        return default


# ── commission (markup) ──────────────────────────────────────────────────────
async def get_markup() -> Decimal:
    return await _get_pct(_MARKUP_KEY, settings.markup_percent)


async def set_markup(percent: Decimal) -> None:
    await repo.set_setting(_MARKUP_KEY, str(percent))


# ── bid premium ──────────────────────────────────────────────────────────────
async def get_bid_premium() -> Decimal:
    return await _get_pct(_BID_KEY, settings.bid_premium_percent)


async def set_bid_premium(percent: Decimal) -> None:
    await repo.set_setting(_BID_KEY, str(percent))


# ── core maths ───────────────────────────────────────────────────────────────
def _ceiling(default_cost: Decimal, premium: Decimal, min_bid: Decimal) -> Decimal:
    """Max price the bot will pay for a number (the bid)."""
    ceiling = default_cost * (Decimal("1") + premium / Decimal("100"))
    if min_bid and min_bid > 0 and ceiling < min_bid:
        ceiling = min_bid
    return ceiling.quantize(_CENT, rounding=ROUND_CEILING)


def apply_markup(cost: Decimal, markup_percent: Decimal) -> Decimal:
    """cost -> customer price, rounded UP to the cent to protect the margin."""
    price = cost * (Decimal("1") + markup_percent / Decimal("100"))
    return price.quantize(_CENT, rounding=ROUND_CEILING)


async def buy_ceiling(default_cost: Decimal) -> Decimal:
    """The maxPrice the bot bids to win this number (real cost paid is <= this)."""
    return _ceiling(default_cost, await get_bid_premium(), settings.min_bid)


def sell_from(default_cost: Decimal, premium: Decimal, commission: Decimal,
              min_bid: Decimal, surge: Decimal = Decimal("0")) -> Decimal:
    """Smart customer price from pre-fetched knobs (sync — for pricing many rows
    at once without an await per row). `surge` is added to the commission for
    out-of-stock/queued routes. Mirrors sell_price()."""
    return apply_markup(_ceiling(default_cost, premium, min_bid), commission + surge)


async def sell_price(default_cost: Decimal, service=None, country=None, queued: bool = False) -> Decimal:
    """Price the CUSTOMER pays for an SMS number: bid ceiling + commission, where
    the commission honours per-service/country overrides and a surge premium for
    out-of-stock (queued) numbers (the bot must bid higher to source those)."""
    ceiling = await buy_ceiling(default_cost)
    overrides = await get_markup_overrides()
    comm = commission_for(service, country, overrides, await get_markup())
    if queued and settings.queued_surge_pct > 0:
        comm += settings.queued_surge_pct
    return apply_markup(ceiling, comm)


async def commission_price(cost: Decimal) -> Decimal:
    """Customer price for RENTALS: commission only, no bid premium (rentals are
    bought at a fixed published price, not via the activation demand auction)."""
    return apply_markup(cost, await get_markup())


# ── eSIM commission (separate from SMS/rent) ─────────────────────────────────
async def get_esim_commission() -> Decimal:
    return await _get_pct(_ESIM_KEY, settings.esim_commission_percent)


async def set_esim_commission(percent: Decimal) -> None:
    await repo.set_setting(_ESIM_KEY, str(percent))


async def esim_sell_price(cost: Decimal) -> Decimal:
    """Customer price for an eSIM: wholesale cost + the eSIM commission."""
    return apply_markup(cost, await get_esim_commission())


# ── Email OTP commission (separate from SMS/rent/eSIM) ────────────────────────
async def get_email_commission() -> Decimal:
    return await _get_pct(_EMAIL_KEY, settings.email_commission_percent)


async def set_email_commission(percent: Decimal) -> None:
    await repo.set_setting(_EMAIL_KEY, str(percent))


async def email_sell_price(cost: Decimal) -> Decimal:
    """Customer price for an email OTP: wholesale cost + the email commission (10%)."""
    return apply_markup(cost, await get_email_commission())


async def price_breakdown(default_cost: Decimal) -> dict:
    """All the numbers behind a quote (for admin display / confirm screens)."""
    premium = await get_bid_premium()
    commission = await get_markup()
    ceiling = _ceiling(default_cost, premium, settings.min_bid)
    return {
        "default": default_cost,
        "premium": premium,
        "commission": commission,
        "ceiling": ceiling,
        "price": apply_markup(ceiling, commission),
        "min_bid": settings.min_bid,
    }
