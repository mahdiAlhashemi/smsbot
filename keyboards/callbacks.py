"""Typed callback-data factories (aiogram). Keep payloads short (<=64 bytes)."""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class Nav(CallbackData, prefix="nav"):
    to: str  # main | buy | wallet | orders | help | admin


class SvcPage(CallbackData, prefix="svp"):
    page: int


class SvcPick(CallbackData, prefix="svc"):
    code: str


class CtyPage(CallbackData, prefix="ctp"):
    code: str
    page: int


class CtyPick(CallbackData, prefix="cty"):
    """Selecting a country opens the confirmation screen (no charge yet)."""
    code: str
    country: str


class BuyConfirm(CallbackData, prefix="buy"):
    """The final 'Buy' tap on the confirmation screen — this charges the user."""
    code: str
    country: str


class OrderAct(CallbackData, prefix="ord"):
    action: str  # cancel | done | another | refresh | reactivate
    id: int


class TopupPick(CallbackData, prefix="top"):
    amount: str  # USD amount as string, or "custom"


class PayCheck(CallbackData, prefix="pay"):
    id: int  # payment row id


class AdminAct(CallbackData, prefix="adm"):
    action: str  # stats | give | markup | bid | esimcomm | finduser | broadcast | channelpost


class AdminUser(CallbackData, prefix="au"):
    action: str  # block | unblock
    id: int


# ─── Rent flow ──────────────────────────────────────────────────────────────
class RentDur(CallbackData, prefix="rdur"):
    h: int  # duration in hours


class RentCtyPage(CallbackData, prefix="rcp"):
    h: int
    page: int


class RentCty(CallbackData, prefix="rcty"):
    h: int
    country: str


class RentSvcPage(CallbackData, prefix="rsp"):
    h: int
    country: str
    page: int


class RentConf(CallbackData, prefix="rcf"):
    h: int
    country: str
    code: str


class RentBuy(CallbackData, prefix="rb"):
    h: int
    country: str
    code: str


class RentAct(CallbackData, prefix="ra"):
    action: str  # cancel | finish | refresh | extend | card
    id: int


class RentExt(CallbackData, prefix="rx"):
    """Picked an extension duration -> confirm screen (h=0 means 'back to menu')."""
    id: int
    h: int


class RentExtGo(CallbackData, prefix="rxg"):
    """Final confirm of a rental extension — this charges the user."""
    id: int
    h: int


# ─── eSIM flow ──────────────────────────────────────────────────────────────
class EsimRegPage(CallbackData, prefix="ep"):
    page: int


class EsimReg(CallbackData, prefix="er"):
    code: str          # location/region code (ISO-2 or region code)
    page: int = 0


class EsimPick(CallbackData, prefix="ek"):
    code: str          # region code (to re-list / look up the package)
    pkg: str           # packageCode


class EsimBuy(CallbackData, prefix="eb"):
    code: str          # region code
    pkg: str           # packageCode


class EsimAct(CallbackData, prefix="ea"):
    action: str        # qr | usage | done
    id: int
