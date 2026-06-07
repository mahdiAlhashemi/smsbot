"""Buy flow: choose service → choose country → purchase."""
from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db import repo
from handlers.common import safe_edit
from handlers.states import BuyFlow
from keyboards.callbacks import BuyConfirm, CtyPage, CtyPick, Nav, SvcPage, SvcPick
from keyboards.menus import (
    back_button,
    confirm_keyboard,
    countries_keyboard,
    services_keyboard,
)
from services import orders, pricing
from services.context import get_ctx
from utils import format_order, money, short

log = logging.getLogger(__name__)
router = Router(name="buy")


async def _show_services(call: CallbackQuery, page: int) -> None:
    ctx = get_ctx()
    try:
        services = await ctx.catalog.services()
    except Exception:  # noqa: BLE001
        await safe_edit(call, "⚠️ Could not load services. Try again shortly.", back_button("main"))
        return
    await safe_edit(
        call,
        "🧩 <b>Choose a service</b>\n\n👇 Pick the app you need a code for:",
        services_keyboard(services, page),
    )


@router.callback_query(Nav.filter(F.to == "buy"))
async def open_buy(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show_services(call, 0)
    await call.answer()


@router.callback_query(SvcPage.filter())
async def page_services(call: CallbackQuery, callback_data: SvcPage) -> None:
    await _show_services(call, callback_data.page)
    await call.answer()


async def _show_countries(call: CallbackQuery, service: str, page: int) -> None:
    ctx = get_ctx()
    await call.answer("Loading availability…")
    try:
        rows = await ctx.catalog.prices_for_service(service)
        names = await ctx.catalog.countries()
    except Exception:  # noqa: BLE001
        await safe_edit(call, "⚠️ Could not load prices. Try again shortly.", back_button("buy"))
        return
    # Show ALL countries that offer this service (like the website). In-stock
    # ones (physicalCount > 0) deliver instantly; the rest are marked ⏳ and will
    # queue — the bot keeps bidding (smart pricing) to win one, then delivers it
    # here automatically. Only bail if the service has no countries at all.
    if not rows:
        await safe_edit(
            call,
            "😔 This service isn't available right now.\nTry another service.",
            back_button("buy"),
        )
        return
    # Attach the customer-facing SMART price to each row (bid ceiling + per-service
    # commission + surge on out-of-stock) so the list matches the confirm screen.
    from config import settings as _s
    premium = await pricing.get_bid_premium()
    commission = await pricing.get_markup()
    overrides = await pricing.get_markup_overrides()
    stats = await repo.get_all_stats()
    for r in rows:
        cc = r["country"]
        comm = pricing.commission_for(service, cc, overrides, commission)
        surge = _s.queued_surge_pct if r.get("count", 0) == 0 else Decimal("0")
        r["sell"] = pricing.sell_from(r["cost"], premium, comm, _s.min_bid, surge)
        d, e = stats.get((service, cc), (0, 0))
        if d + e >= 5:  # show a success badge once there's a meaningful sample
            r["rate"] = round(100 * d / (d + e))
    # Sort countries A → Z by name.
    rows = sorted(rows, key=lambda r: names.get(r["country"], "zzz").lower())
    name = await ctx.catalog.service_name(service)
    in_stock = sum(1 for r in rows if r.get("count", 0) > 0)
    await safe_edit(
        call,
        f"🌍 <b>{name}</b> — choose a country\n\n"
        f"<i>Name • price</i> — <b>{in_stock}</b> in stock now.\n"
        "<i>⏳ = none free right now; we'll search &amp; deliver it automatically.</i>",
        countries_keyboard(service, rows, names, page),
    )


@router.callback_query(SvcPick.filter())
async def pick_service(call: CallbackQuery, callback_data: SvcPick) -> None:
    await _show_countries(call, callback_data.code, 0)


@router.callback_query(CtyPage.filter())
async def page_countries(call: CallbackQuery, callback_data: CtyPage) -> None:
    await _show_countries(call, callback_data.code, callback_data.page)


@router.callback_query(CtyPick.filter())
async def confirm_screen(call: CallbackQuery, callback_data: CtyPick) -> None:
    """Show an order summary and ask the user to confirm before charging."""
    ctx = get_ctx()
    service, country = callback_data.code, callback_data.country
    try:
        rows = await ctx.catalog.prices_for_service(service)
    except Exception:  # noqa: BLE001
        await safe_edit(call, "⚠️ Could not reach HeroSMS. Try again.", back_button("buy"))
        await call.answer()
        return
    match = next((r for r in rows if r["country"] == country), None)
    if match is None:
        await safe_edit(call, "😔 That number just sold out. Pick another.", back_button("buy"))
        await call.answer()
        return

    queued = match.get("count", 0) == 0
    price = await pricing.sell_price(match["cost"], service=service, country=country, queued=queued)
    user = await repo.get_user(call.from_user.id)
    available = user.available if user else Decimal("0")
    can_afford = available >= price
    svc_name = await ctx.catalog.service_name(service)
    cty_name = await ctx.catalog.country_name(country)

    from country_flags import flag

    # Success badge from delivery history.
    d, e = (await repo.get_all_stats()).get((service, country), (0, 0))
    badge = f"\n✅ Success rate: <b>{round(100 * d / (d + e))}%</b>" if d + e >= 5 else ""
    queued_line = ("\n<i>⚡ Out of stock — we'll bid to source one (priority pricing).</i>" if queued else "")

    text = (
        "🧾 <b>Confirm your order</b>\n"
        "────────────────\n"
        f"🧩 Service: <b>{svc_name}</b>\n"
        f"🌍 Country: {flag(country)} <b>{cty_name}</b>\n"
        f"💵 Price: <b>{money(price)}</b>{badge}{queued_line}\n"
        f"💰 Available: <b>{money(available)}</b>\n"
        "────────────────\n"
        "📩 Receive codes for <b>20 min</b>.\n"
        "❌ Cancel available <b>after 2 min</b>.\n"
        "↩️ No code → funds return to your balance.\n\n"
        "<i>⚡ {} is held now and charged only when a code arrives — "
        "no code, no charge.</i>".format(money(price))
    )
    if not can_afford:
        text += f"\n\n⚠️ <b>Not enough balance</b> — you need <b>{money(price - available)}</b> more."
    await safe_edit(call, text, confirm_keyboard(service, country, can_afford=can_afford))
    await call.answer()


@router.callback_query(BuyConfirm.filter())
async def confirm_buy(call: CallbackQuery, callback_data: BuyConfirm) -> None:
    ctx = get_ctx()
    service, country = callback_data.code, callback_data.country
    await call.answer("Purchasing…")

    # Re-read the current cost for this country (cached) to bill accurately.
    try:
        rows = await ctx.catalog.prices_for_service(service)
    except Exception:  # noqa: BLE001
        await safe_edit(call, "⚠️ Could not reach HeroSMS. Try again.", back_button("buy"))
        return
    match = next((r for r in rows if r["country"] == country), None)
    if match is None:
        await safe_edit(call, "😔 That number just sold out. Pick another.", back_button("buy"))
        return

    queued = match.get("count", 0) == 0
    try:
        order = await orders.purchase(
            call.from_user.id, service, country, match["cost"], ctx.hero, ctx.catalog, queued=queued
        )
    except orders.InsufficientFunds:
        price = await pricing.sell_price(match["cost"], service=service, country=country, queued=queued)
        user = await repo.get_user(call.from_user.id)
        avail = user.available if user else 0
        await safe_edit(
            call,
            "💸 <b>Not enough balance</b>\n\n"
            f"💵 Price: <b>{money(price)}</b>\n"
            f"💰 Available: <b>{money(avail)}</b>\n\n"
            "💳 Top up your wallet and try again.",
            _topup_or_back_kb(),
        )
        return
    except orders.DuplicateOrder as exc:
        await safe_edit(call, f"⛔ {exc.user_message}", back_button("orders"))
        return
    except orders.OutOfStock:
        await safe_edit(call, "😔 No numbers available right now. Try another country.", back_button("buy"))
        return
    except orders.PurchaseError as exc:
        await safe_edit(call, f"⚠️ {exc.user_message}", back_button("buy"))
        return

    # Remember where this card lives so the poller can auto-refresh it in place.
    await repo.update_order(
        order.id, chat_id=call.message.chat.id, message_id=call.message.message_id
    )

    from keyboards.menus import order_keyboard
    from db.models import Order as _Order

    if order.status == _Order.PENDING:
        from config import settings as _s
        qt = _s.queue_timeout_min
        window = (f"{qt // 60} hour" + ("s" if qt // 60 != 1 else "")) if qt >= 60 else f"{qt} minutes"
        await safe_edit(
            call,
            "🧾 <b>Order received — searching for your number…</b>\n"
            "────────────────\n"
            f"🧩 Service: <b>{order.service_name}</b>\n"
            f"🌍 Country: <b>{order.country_name}</b>\n"
            f"💵 Price: <b>{money(order.price)}</b>\n"
            "────────────────\n"
            "⏳ No numbers are free right now, so we're searching for one. "
            f"We'll keep trying for up to <b>{window}</b> — your number appears here "
            "automatically the moment one is found. You can cancel anytime.\n\n"
            f"<i>⚡ {money(order.price)} is on hold; you're charged only when the OTP code arrives. "
            "If no number is found, it's released — no charge.</i>",
            order_keyboard(order),
        )
        return

    await safe_edit(
        call,
        format_order(order)
        + f"\n\n⏳ <b>Waiting for the OTP code</b> — it appears here automatically.\n"
        f"<i>⚡ {money(order.price)} is on hold; you're charged only when the code arrives.</i>",
        order_keyboard(order),
    )


def _topup_or_back_kb():
    from config import settings

    b = InlineKeyboardBuilder()
    if settings.payments_enabled:
        from keyboards.callbacks import TopupPick

        b.button(text="➕ Top up", callback_data=TopupPick(amount="menu"))
    b.button(text="⬅️ Back", callback_data=Nav(to="main"))
    b.adjust(1)
    return b.as_markup()


# ─── Service search ─────────────────────────────────────────────────────────
@router.callback_query(Nav.filter(F.to == "search"))
async def start_search(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BuyFlow.searching)
    await safe_edit(
        call,
        "🔍 <b>Search a service</b>\n\n👇 Type a service name or code (e.g. <code>telegram</code>, "
        "<code>wa</code>, <code>openai</code>):",
        back_button("buy"),
    )
    await call.answer()


@router.message(BuyFlow.searching, F.text)
async def do_search(message: Message, state: FSMContext) -> None:
    ctx = get_ctx()
    results = await ctx.catalog.search_services(message.text, limit=20)
    if not results:
        await message.answer("😔 No services matched. Try another word.")
        return
    await state.clear()
    b = InlineKeyboardBuilder()
    for svc in results:
        b.button(text=short(svc["name"], 26), callback_data=SvcPick(code=svc["code"]))
    b.button(text="⬅️ Back", callback_data=Nav(to="buy"))
    b.adjust(2)
    await message.answer(
        f"🔍 Results for <b>{short(message.text, 30)}</b>:", reply_markup=b.as_markup()
    )
