"""Rent flow: duration → country → service → confirm → rent (paid upfront)."""
from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from country_flags import flag
from db import repo
from handlers.common import safe_edit
from keyboards.callbacks import (
    Nav, RentAct, RentBuy, RentConf, RentCty, RentCtyPage, RentDur, RentSvcPage,
)
from keyboards.menus import (
    back_button, rent_confirm_keyboard, rent_countries_keyboard, rent_durations_keyboard,
    rent_order_keyboard, rent_services_keyboard,
)
from services import pricing, rent as rent_svc
from services import orders as order_svc
from services.context import get_ctx
from services.rent import DURATION_LABELS
from utils import money, short

log = logging.getLogger(__name__)
router = Router(name="rent")

# Probe country (rentable) used to fetch the global rent-country list.
_PROBE_COUNTRY = "6"


@router.callback_query(Nav.filter(F.to == "rent"))
async def open_rent(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(
        call,
        "📱 <b>Rent a number</b>\n\nRent a number for a period and receive "
        "<b>many SMS</b> the whole time (great for ongoing use).\n\n"
        "Choose how long you want it:",
        rent_durations_keyboard(),
    )
    await call.answer()


async def _show_rent_countries(call: CallbackQuery, h: int, page: int) -> None:
    ctx = get_ctx()
    await call.answer("Loading…")
    try:
        data = await ctx.hero.rent_services(h, _PROBE_COUNTRY)
        names = await ctx.catalog.countries()
    except Exception:  # noqa: BLE001
        await safe_edit(call, "⚠️ Could not load rent countries. Try again.", back_button("rent"))
        return
    ids = list({int(v) for v in (data.get("countries") or {}).values()})
    if not ids:
        await safe_edit(call, "😔 No rent countries available right now.", back_button("rent"))
        return
    # Sort countries A → Z by name.
    ids.sort(key=lambda cid: names.get(str(cid), "zzz").lower())
    await safe_edit(
        call,
        f"📱 <b>Rent — {DURATION_LABELS.get(h, str(h) + 'h')}</b>\n\nChoose a country:",
        rent_countries_keyboard(h, ids, names, page),
    )


@router.callback_query(RentDur.filter())
async def pick_duration(call: CallbackQuery, callback_data: RentDur) -> None:
    await _show_rent_countries(call, callback_data.h, 0)


@router.callback_query(RentCtyPage.filter())
async def page_rent_countries(call: CallbackQuery, callback_data: RentCtyPage) -> None:
    await _show_rent_countries(call, callback_data.h, callback_data.page)


from services.catalog import POPULAR_SERVICES

_POPULAR_CODES = {c for c, _ in POPULAR_SERVICES}


async def _service_names(ctx) -> dict:
    try:
        return {s["code"]: s["name"] for s in await ctx.catalog.services()}
    except Exception:  # noqa: BLE001
        return {}


async def _rent_cost(ctx, h: int, country: str, code: str) -> Decimal | None:
    rows = await ctx.hero.rent_service_prices(h, country)
    match = next((r for r in rows if r["code"] == code), None)
    return match["price"] if match else None


async def _show_rent_services(call: CallbackQuery, h: int, country: str, page: int) -> None:
    ctx = get_ctx()
    await call.answer("Loading…")
    try:
        rows = await ctx.hero.rent_service_prices(h, country)
    except Exception:  # noqa: BLE001
        await safe_edit(call, "⚠️ Could not load rent prices. Try again.", back_button("rent"))
        return
    if not rows:
        await safe_edit(call, "😔 No rentals available for this country. Pick another.", back_button("rent"))
        return
    # Show popular apps available for rent here; fall back to the cheapest if none.
    popular = [r for r in rows if r["code"] in _POPULAR_CODES]
    display = popular if popular else rows[:40]
    markup = await pricing.get_markup()
    for r in display:
        r["sell"] = pricing.apply_markup(r["price"], markup)
    names = await _service_names(ctx)
    cty = (await ctx.catalog.countries()).get(country, country)
    await safe_edit(
        call,
        f"📱 <b>Rent {DURATION_LABELS.get(h)} — {flag(country)} {cty}</b>\n\n"
        "Choose what the number is for:\n<i>name • price for the whole period</i>",
        rent_services_keyboard(h, country, display, names, page),
    )


@router.callback_query(RentCty.filter())
async def pick_rent_country(call: CallbackQuery, callback_data: RentCty) -> None:
    await _show_rent_services(call, callback_data.h, callback_data.country, 0)


@router.callback_query(RentSvcPage.filter())
async def page_rent_services(call: CallbackQuery, callback_data: RentSvcPage) -> None:
    await _show_rent_services(call, callback_data.h, callback_data.country, callback_data.page)


@router.callback_query(RentConf.filter())
async def confirm_rent(call: CallbackQuery, callback_data: RentConf) -> None:
    ctx = get_ctx()
    h, country, code = callback_data.h, callback_data.country, callback_data.code
    await call.answer()
    cost = await _rent_cost(ctx, h, country, code)
    if cost is None:
        await safe_edit(call, "😔 That just sold out. Pick another.", back_button("rent"))
        return
    price = await pricing.commission_price(cost)
    user = await repo.get_user(call.from_user.id)
    available = user.available if user else Decimal("0")
    can_afford = available >= price
    names = await _service_names(ctx)
    cty = (await ctx.catalog.countries()).get(country, country)
    text = (
        "🧾 <b>Confirm rental</b>\n\n"
        f"📱 Service: <b>{short(names.get(code, code.upper()), 30)}</b>\n"
        f"🌍 Country: {flag(country)} <b>{cty}</b>\n"
        f"📅 Duration: <b>{DURATION_LABELS.get(h)}</b>\n"
        f"💵 Price: <b>{money(price)}</b>\n"
        f"👛 Available: <b>{money(available)}</b>\n\n"
        "📩 Receive codes during the whole rental period.\n"
        "❌ Cancellation (full refund) available <b>after 2 min</b> and "
        "<b>no later than 20 min</b>.\n\n"
        "<i>Paid upfront for the whole period. The number receives all its SMS during that time.</i>"
    )
    if not can_afford:
        text += f"\n\n⚠️ Not enough balance — you need {money(price - available)} more."
    await safe_edit(call, text, rent_confirm_keyboard(h, country, code, can_afford=can_afford))


@router.callback_query(RentBuy.filter())
async def do_rent(call: CallbackQuery, callback_data: RentBuy) -> None:
    ctx = get_ctx()
    h, country, code = callback_data.h, callback_data.country, callback_data.code
    await call.answer("Renting…")
    cost = await _rent_cost(ctx, h, country, code)
    if cost is None:
        await safe_edit(call, "😔 That service just sold out. Pick another.", back_button("rent"))
        return
    try:
        order = await rent_svc.rent_purchase(call.from_user.id, code, country, h, cost, ctx.hero, ctx.catalog)
    except order_svc.DuplicateOrder as exc:
        await safe_edit(call, f"⛔ {exc.user_message}", back_button("orders"))
        return
    except order_svc.InsufficientFunds:
        await safe_edit(call, "💸 Not enough balance. Top up your wallet and try again.", back_button("wallet"))
        return
    except order_svc.PurchaseError as exc:
        await safe_edit(call, f"⚠️ {exc.user_message}", back_button("rent"))
        return

    await repo.update_order(order.id, chat_id=call.message.chat.id, message_id=call.message.message_id)
    order = await repo.get_order(order.id)
    await safe_edit(call, rent_svc.format_rent_card(order), rent_order_keyboard(order))


@router.callback_query(RentAct.filter(F.action == "finish"))
async def finish_rent_cb(call: CallbackQuery, callback_data: RentAct) -> None:
    order = await repo.get_order(callback_data.id)
    if order is None or order.user_id != call.from_user.id:
        await call.answer("Order not found.", show_alert=True)
        return
    await rent_svc.finish_rent(order, get_ctx().hero)
    order = await repo.get_order(order.id)
    try:
        await call.message.edit_text(rent_svc.format_rent_card(order), reply_markup=rent_order_keyboard(order))
    except Exception:  # noqa: BLE001
        pass
    await call.answer("Rental finished.")


@router.callback_query(RentAct.filter(F.action == "cancel"))
async def cancel_rent_cb(call: CallbackQuery, callback_data: RentAct) -> None:
    order = await repo.get_order(callback_data.id)
    if order is None or order.user_id != call.from_user.id:
        await call.answer("Order not found.", show_alert=True)
        return
    from utils import rent_cancel_state

    state, secs = rent_cancel_state(order)
    if state == "locked":
        await call.answer(
            f"⏳ Cancellation opens 2 minutes after renting — {secs}s left.", show_alert=True
        )
        return
    if state == "closed":
        await call.answer(
            "Cancellation is only allowed within the first 20 minutes. "
            "Your number stays active for the full period — use 🛑 Finish rental.",
            show_alert=True,
        )
        return
    ok = await rent_svc.cancel_rent_refund(order, get_ctx().hero)
    order = await repo.get_order(order.id)
    try:
        await call.message.edit_text(rent_svc.format_rent_card(order), reply_markup=rent_order_keyboard(order))
    except Exception:  # noqa: BLE001
        pass
    await call.answer(
        f"✅ Rental cancelled — {money(order.price)} refunded to your balance." if ok
        else "Could not cancel — it may have just ended.",
        show_alert=True,
    )
