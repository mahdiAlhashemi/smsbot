"""Inline keyboard builders."""
from __future__ import annotations

import math
from decimal import Decimal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from country_flags import flag
from db.models import Order
from keyboards.callbacks import (
    AdminAct,
    BuyConfirm,
    CtyPage,
    CtyPick,
    EsimAct,
    EsimBuy,
    EsimPick,
    EsimReg,
    EsimRegPage,
    Nav,
    OrderAct,
    PayCheck,
    RentAct,
    RentBuy,
    RentConf,
    RentCty,
    RentCtyPage,
    RentDur,
    RentSvcPage,
    SvcPage,
    SvcPick,
    TopupPick,
)
from services.rent import RENT_DURATIONS
from utils import money, short

# Telegram allows up to ~100 buttons per inline keyboard; use a long page so the
# whole country list is essentially scrollable on one screen.
SERVICES_PER_PAGE = 50
COUNTRIES_PER_PAGE = 90
TOPUP_PRESETS = ["2", "5", "10", "20", "50", "100"]


def main_menu(is_admin: bool, payments_enabled: bool, esim_enabled: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📲 Buy OTP number", callback_data=Nav(to="buy"))
    b.button(text="📱 Rent number", callback_data=Nav(to="rent"))
    layout = [1]
    if esim_enabled:
        b.button(text="📡 eSIM data", callback_data=Nav(to="esim"))
        layout.append(2)          # Rent + eSIM share a row
    else:
        layout.append(1)
    b.button(text="👛 Wallet", callback_data=Nav(to="wallet"))
    b.button(text="🧾 My orders", callback_data=Nav(to="orders"))
    b.button(text="👤 Account", callback_data=Nav(to="account"))
    b.button(text="ℹ️ Help", callback_data=Nav(to="help"))
    layout += [2, 2]             # Wallet+Orders, Account+Help
    if is_admin:
        b.button(text="🛠 Admin", callback_data=Nav(to="admin"))
        layout.append(1)
    b.adjust(*layout)
    return b.as_markup()


def back_button(to: str = "main") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Back", callback_data=Nav(to=to))
    return b.as_markup()


def services_keyboard(services: list[dict], page: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    pages = max(1, math.ceil(len(services) / SERVICES_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = services[page * SERVICES_PER_PAGE : (page + 1) * SERVICES_PER_PAGE]
    for svc in chunk:
        b.button(
            text=f"{short(svc['name'], 26)}",
            callback_data=SvcPick(code=svc["code"]),
        )
    b.adjust(2)

    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️", callback_data=SvcPage(page=page - 1))
    nav.button(text=f"{page + 1}/{pages}", callback_data=SvcPage(page=page))
    if page < pages - 1:
        nav.button(text="▶️", callback_data=SvcPage(page=page + 1))
    nav.adjust(3)

    b.attach(nav)
    tail = InlineKeyboardBuilder()
    tail.button(text="🔍 Search service", callback_data=Nav(to="search"))
    tail.button(text="⬅️ Back", callback_data=Nav(to="main"))
    tail.adjust(2)
    b.attach(tail)
    return b.as_markup()


def countries_keyboard(
    service: str, items: list[dict], names: dict[str, str], page: int
) -> InlineKeyboardMarkup:
    """items: [{country, cost(Decimal), count}] already marked-up cost? No — we
    pass the SELL price in 'sell'. Each row: name — price (stock)."""
    b = InlineKeyboardBuilder()
    pages = max(1, math.ceil(len(items) / COUNTRIES_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = items[page * COUNTRIES_PER_PAGE : (page + 1) * COUNTRIES_PER_PAGE]
    for it in chunk:
        cid = it["country"]
        name = short(names.get(cid, f"#{cid}"), 16)
        # ⏳ marks countries with no real stock right now (will be queued).
        mark = "" if it.get("count", 0) > 0 else "⏳ "
        rate = it.get("rate")
        badge = f" ✅{rate}%" if rate is not None else ""
        label = f"{mark}{flag(cid)} {name} • {money(it['sell'])}{badge}"
        b.button(text=label, callback_data=CtyPick(code=service, country=cid))
    b.adjust(1)

    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️", callback_data=CtyPage(code=service, page=page - 1))
    nav.button(text=f"{page + 1}/{pages}", callback_data=CtyPage(code=service, page=page))
    if page < pages - 1:
        nav.button(text="▶️", callback_data=CtyPage(code=service, page=page + 1))
    nav.adjust(3)
    b.attach(nav)

    tail = InlineKeyboardBuilder()
    tail.button(text="⬅️ Services", callback_data=Nav(to="buy"))
    b.attach(tail)
    return b.as_markup()


def confirm_keyboard(service: str, country: str, *, can_afford: bool = True) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if can_afford:
        b.button(text="✅ Confirm & buy", callback_data=BuyConfirm(code=service, country=country))
    else:
        b.button(text="➕ Top up", callback_data=TopupPick(amount="menu"))
    b.button(text="⬅️ Back", callback_data=SvcPick(code=service))
    b.adjust(1)
    return b.as_markup()


def order_keyboard(order: Order) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if order.status == Order.WAITING:
        # No refresh button — the card auto-updates from the background poller.
        b.button(text="🔁 Replace number", callback_data=OrderAct(action="replace", id=order.id))
        # HeroSMS allows cancellation only AFTER 2 min. Show a live countdown
        # until then; the poller re-renders this card so it unlocks on its own.
        from utils import activation_cancel_in
        locked = activation_cancel_in(order)
        if locked > 0:
            b.button(text=f"🔒 Cancel in {locked}s", callback_data=OrderAct(action="cancel", id=order.id))
        else:
            b.button(text="❌ Cancel (no charge)", callback_data=OrderAct(action="cancel", id=order.id))
        b.adjust(1)
    elif order.status == Order.PENDING:
        b.button(text="❌ Cancel (no charge)", callback_data=OrderAct(action="cancel", id=order.id))
        b.adjust(1)
    elif order.status == Order.RECEIVED:
        # Show the price — each extra code is a fresh charge, so make the cost visible.
        b.button(
            text=f"🔁 Another code ({money(order.price)})",
            callback_data=OrderAct(action="another", id=order.id),
        )
        b.button(text="✅ Done", callback_data=OrderAct(action="done", id=order.id))
        b.adjust(2)
    b.row(InlineKeyboardButton(text="🧾 My orders", callback_data=Nav(to="orders").pack()))
    return b.as_markup()


def wallet_keyboard(payments_enabled: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if payments_enabled:
        b.button(text="➕ Top up", callback_data=TopupPick(amount="menu"))
    b.button(text="⬅️ Back", callback_data=Nav(to="main"))
    b.adjust(1)
    return b.as_markup()


def topup_amounts_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for amt in TOPUP_PRESETS:
        b.button(text=f"{money(amt)}", callback_data=TopupPick(amount=amt))
    b.button(text="✏️ Custom amount", callback_data=TopupPick(amount="custom"))
    b.button(text="⬅️ Back", callback_data=Nav(to="wallet"))
    b.adjust(3, 3, 1, 1)
    return b.as_markup()


def payment_keyboard(pay_url: str, payment_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💳 Pay now", url=pay_url)
    b.button(text="✅ I have paid", callback_data=PayCheck(id=payment_id))
    b.button(text="⬅️ Wallet", callback_data=Nav(to="wallet"))
    b.adjust(1)
    return b.as_markup()


def rent_durations_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for h, lbl in RENT_DURATIONS:
        b.button(text=f"📅 {lbl}", callback_data=RentDur(h=h))
    b.button(text="⬅️ Back", callback_data=Nav(to="main"))
    b.adjust(2, 2, 1)
    return b.as_markup()


def rent_countries_keyboard(h: int, country_ids: list, names: dict, page: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    per = 90
    pages = max(1, math.ceil(len(country_ids) / per))
    page = max(0, min(page, pages - 1))
    for cid in country_ids[page * per:(page + 1) * per]:
        cid = str(cid)
        name = short(names.get(cid, f"#{cid}"), 22)
        b.button(text=f"{flag(cid)} {name}", callback_data=RentCty(h=h, country=cid))
    b.adjust(2)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️", callback_data=RentCtyPage(h=h, page=page - 1))
    nav.button(text=f"{page + 1}/{pages}", callback_data=RentCtyPage(h=h, page=page))
    if page < pages - 1:
        nav.button(text="▶️", callback_data=RentCtyPage(h=h, page=page + 1))
    nav.adjust(3)
    b.attach(nav)
    tail = InlineKeyboardBuilder()
    tail.button(text="⬅️ Durations", callback_data=Nav(to="rent"))
    b.attach(tail)
    return b.as_markup()


def rent_services_keyboard(h: int, country: str, items: list, names: dict, page: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    per = 80
    pages = max(1, math.ceil(len(items) / per))
    page = max(0, min(page, pages - 1))
    for it in items[page * per:(page + 1) * per]:
        code = it["code"]
        name = short(names.get(code, code.upper()), 22)
        b.button(text=f"{name} • {money(it['sell'])}", callback_data=RentConf(h=h, country=country, code=code))
    b.adjust(1)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️", callback_data=RentSvcPage(h=h, country=country, page=page - 1))
    nav.button(text=f"{page + 1}/{pages}", callback_data=RentSvcPage(h=h, country=country, page=page))
    if page < pages - 1:
        nav.button(text="▶️", callback_data=RentSvcPage(h=h, country=country, page=page + 1))
    nav.adjust(3)
    b.attach(nav)
    tail = InlineKeyboardBuilder()
    tail.button(text="⬅️ Countries", callback_data=RentDur(h=h))
    b.attach(tail)
    return b.as_markup()


def rent_confirm_keyboard(h: int, country: str, code: str, *, can_afford: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if can_afford:
        b.button(text="✅ Rent now", callback_data=RentBuy(h=h, country=country, code=code))
    else:
        b.button(text="➕ Top up", callback_data=TopupPick(amount="menu"))
    b.button(text="⬅️ Back", callback_data=RentCty(h=h, country=country))
    b.adjust(1)
    return b.as_markup()


def rent_order_keyboard(order: Order) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if order.status in (Order.WAITING, Order.RECEIVED):
        # Rent cancel policy: refundable only AFTER 2 min and NO LATER than 20 min.
        from utils import rent_cancel_state
        state, secs = rent_cancel_state(order)
        if state == "locked":
            b.button(text=f"🔒 Cancel in {secs}s", callback_data=RentAct(action="cancel", id=order.id))
        elif state == "open":
            b.button(text="💸 Cancel & refund", callback_data=RentAct(action="cancel", id=order.id))
        else:  # window closed — keep it for the period; finishing gives no refund
            b.button(text="🛑 Finish rental", callback_data=RentAct(action="finish", id=order.id))
        b.adjust(1)
    b.row(InlineKeyboardButton(text="🧾 My orders", callback_data=Nav(to="orders").pack()))
    return b.as_markup()


def esim_regions_keyboard(regions: list[dict], page: int) -> InlineKeyboardMarkup:
    """regions: ordered list of {code, name}. Paginated, 2 per row."""
    from country_flags import iso_flag

    b = InlineKeyboardBuilder()
    per = 60
    pages = max(1, math.ceil(len(regions) / per))
    page = max(0, min(page, pages - 1))
    for r in regions[page * per:(page + 1) * per]:
        code = str(r.get("code", ""))
        name = short(str(r.get("name", code)), 20)
        b.button(text=f"{iso_flag(code)} {name}", callback_data=EsimReg(code=code, page=0))
    b.adjust(2)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️", callback_data=EsimRegPage(page=page - 1))
    nav.button(text=f"{page + 1}/{pages}", callback_data=EsimRegPage(page=page))
    if page < pages - 1:
        nav.button(text="▶️", callback_data=EsimRegPage(page=page + 1))
    nav.adjust(3)
    b.attach(nav)
    tail = InlineKeyboardBuilder()
    tail.button(text="⬅️ Back", callback_data=Nav(to="main"))
    b.attach(tail)
    return b.as_markup()


def esim_packages_keyboard(region_code: str, packages: list, page: int) -> InlineKeyboardMarkup:
    """packages: list of EsimPackage already priced (each has .sell Decimal)."""
    b = InlineKeyboardBuilder()
    per = 40
    pages = max(1, math.ceil(len(packages) / per))
    page = max(0, min(page, pages - 1))
    chunk = packages[page * per:(page + 1) * per]
    # Section headers between coverage groups (tapping one just re-renders the
    # page — harmless — and visually separates local / regional / global).
    _HDR = {
        0: "──  🏠 LOCAL · this country  ──",
        1: "──  🌍 REGIONAL · nearby  ──",
        2: "──  🌐 GLOBAL · worldwide  ──",
    }
    last_g = None
    for p in chunk:
        g = getattr(p, "scope_group", 0)
        if g != last_g:
            b.button(text=_HDR.get(g, ""), callback_data=EsimReg(code=region_code, page=page))
            last_g = g
        unit = str(getattr(p, "duration_unit", "DAY")).lower()[:1]
        badge = getattr(p, "scope_badge", "")
        label = f"{badge} {p.gb} · {p.duration}{unit} · {money(p.sell)}"
        b.button(text=label, callback_data=EsimPick(code=region_code, pkg=p.code))
    b.adjust(1)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️", callback_data=EsimReg(code=region_code, page=page - 1))
    nav.button(text=f"{page + 1}/{pages}", callback_data=EsimReg(code=region_code, page=page))
    if page < pages - 1:
        nav.button(text="▶️", callback_data=EsimReg(code=region_code, page=page + 1))
    nav.adjust(3)
    b.attach(nav)
    tail = InlineKeyboardBuilder()
    tail.button(text="⬅️ Destinations", callback_data=Nav(to="esim"))
    b.attach(tail)
    return b.as_markup()


def esim_confirm_keyboard(region_code: str, pkg_code: str, *, can_afford: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if can_afford:
        b.button(text="✅ Buy eSIM", callback_data=EsimBuy(code=region_code, pkg=pkg_code))
    else:
        b.button(text="➕ Top up", callback_data=TopupPick(amount="menu"))
    b.button(text="⬅️ Back", callback_data=EsimReg(code=region_code, page=0))
    b.adjust(1)
    return b.as_markup()


def esim_order_keyboard(order: Order) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if order.status == Order.RECEIVED:
        b.button(text="📊 Check usage", callback_data=EsimAct(action="usage", id=order.id))
        b.button(text="🔳 Resend QR", callback_data=EsimAct(action="qr", id=order.id))
        b.adjust(2)
    b.row(InlineKeyboardButton(text="🧾 My orders", callback_data=Nav(to="orders").pack()))
    return b.as_markup()


def admin_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📊 Stats", callback_data=AdminAct(action="stats"))
    b.button(text="💵 Give balance", callback_data=AdminAct(action="give"))
    b.button(text="📈 OTP commission %", callback_data=AdminAct(action="markup"))
    b.button(text="🎯 Bid premium %", callback_data=AdminAct(action="bid"))
    b.button(text="📡 eSIM commission %", callback_data=AdminAct(action="esimcomm"))
    b.button(text="🎚 Service price", callback_data=AdminAct(action="svcprice"))
    b.button(text="🔍 Find user", callback_data=AdminAct(action="finduser"))
    b.button(text="📣 Broadcast", callback_data=AdminAct(action="broadcast"))
    b.button(text="📢 Post to channel", callback_data=AdminAct(action="channelpost"))
    b.button(text="⬅️ Back", callback_data=Nav(to="main"))
    b.adjust(2, 2, 2, 2, 1)
    return b.as_markup()
