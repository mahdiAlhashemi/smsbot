"""eSIM flow: destination → data package → confirm → buy (paid upfront) → QR."""
from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from country_flags import iso_flag
from db import repo
from handlers.common import safe_edit
from keyboards.callbacks import EsimAct, EsimBuy, EsimPick, EsimReg, EsimRegPage, Nav
from keyboards.menus import (
    back_button, esim_confirm_keyboard, esim_order_keyboard,
    esim_packages_keyboard, esim_regions_keyboard,
)
from services import esim as esim_svc
from services import orders as order_svc
from services import pricing
from services.context import get_ctx
from utils import money

log = logging.getLogger(__name__)
router = Router(name="esim")


def _catalog():
    return get_ctx().esim_catalog


async def _ordered_regions() -> list[dict]:
    """Regions with popular destinations first, then the rest A→Z by name."""
    regions = await _catalog().regions()
    by_code = {str(r.get("code")): r for r in regions if r.get("code")}
    pop = [by_code[c] for c in esim_svc.POPULAR_DESTINATIONS if c in by_code]
    promoted = {c for c in esim_svc.POPULAR_DESTINATIONS if c in by_code}
    rest = sorted(
        (r for c, r in by_code.items() if c not in promoted),
        key=lambda r: str(r.get("name", "")).lower(),
    )
    return pop + rest


@router.callback_query(Nav.filter(F.to == "esim"))
async def open_esim(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if get_ctx().esim is None:
        await safe_edit(call, "📡 <b>eSIM data plans</b> are not available right now.", back_button("main"))
        await call.answer()
        return
    await _show_regions(call, 0)


async def _show_regions(call: CallbackQuery, page: int) -> None:
    await call.answer("Loading destinations…")
    try:
        regions = await _ordered_regions()
    except Exception:  # noqa: BLE001
        await safe_edit(call, "⚠️ Could not load destinations. Try again shortly.", back_button("main"))
        return
    if not regions:
        await safe_edit(call, "😔 No eSIM destinations available right now.", back_button("main"))
        return
    await safe_edit(
        call,
        "📡 <b>eSIM data plans</b>\n"
        "────────────────\n"
        "Instant data eSIM for your trip — delivered as a QR code, no physical SIM.\n\n"
        "👇 <b>Choose a destination</b>",
        esim_regions_keyboard(regions, page),
    )


@router.callback_query(EsimRegPage.filter())
async def page_regions(call: CallbackQuery, callback_data: EsimRegPage) -> None:
    await _show_regions(call, callback_data.page)


@router.callback_query(EsimReg.filter())
async def show_packages(call: CallbackQuery, callback_data: EsimReg) -> None:
    await call.answer("Loading plans…")
    code = callback_data.code
    try:
        packages = await _catalog().packages_for(code)
    except Exception:  # noqa: BLE001
        await safe_edit(call, "⚠️ Could not load plans for this destination. Try again.", back_button("esim"))
        return
    if not packages:
        await safe_edit(
            call,
            "😔 No eSIM plans for this destination right now.\nTry another one.",
            back_button("esim"),
        )
        return
    commission = await pricing.get_esim_commission()
    for p in packages:
        p.sell = pricing.apply_markup(p.cost, commission)
    name = await _catalog().region_name(code)
    await safe_edit(
        call,
        f"📡 <b>{iso_flag(code)} {name}</b> — choose a data plan\n"
        "────────────────\n"
        "🏠 = this country only (cheapest)\n"
        "🌍 = regional · 🌐 = global (the number = countries covered)\n\n"
        "<i>💡 coverage · data · validity · price</i>",
        esim_packages_keyboard(code, packages, callback_data.page),
    )


@router.callback_query(EsimPick.filter())
async def confirm_esim(call: CallbackQuery, callback_data: EsimPick) -> None:
    await call.answer()
    region, pkg_code = callback_data.code, callback_data.pkg
    pkg = await _catalog().package(region, pkg_code)
    if pkg is None:
        await safe_edit(call, "😔 That plan just changed. Pick another.", back_button("esim"))
        return
    price = await pricing.esim_sell_price(pkg.cost)
    user = await repo.get_user(call.from_user.id)
    available = user.available if user else Decimal("0")
    can_afford = available >= price
    name = await _catalog().region_name(region)
    n = pkg.country_count
    if pkg.is_local:
        cov_line = f"🏠 <b>Local</b> — works in {name} only"
    else:
        shown = ", ".join(pkg.region_names[:6]) or pkg.location
        more = f" +{n - 6} more" if n > 6 else ""
        cov_line = f"🌍 <b>{n} countries</b> — {shown}{more}"
    unit = pkg.duration_unit.lower()
    text = (
        "🧾 <b>Confirm your eSIM</b>\n"
        "────────────────\n"
        f"🧩 Plan: <b>{pkg.name}</b>\n"
        f"🌍 Destination: {iso_flag(region)} <b>{name}</b>\n"
        f"📦 Data: <b>{pkg.gb}</b>\n"
        f"⏱️ Validity: <b>{pkg.duration} {unit}{'s' if pkg.duration != 1 else ''}</b> (from first use)\n"
        f"📡 Coverage: {cov_line}\n"
        "────────────────\n"
        f"💵 Price: <b>{money(price)}</b>\n"
        f"💰 Available: <b>{money(available)}</b>\n\n"
        "<i>⚡ Delivered instantly as a QR code. Paid upfront. Needs an "
        "eSIM-capable phone — install over Wi-Fi before you travel.</i>"
    )
    if not can_afford:
        text += f"\n\n⚠️ <b>Not enough balance</b> — you need <b>{money(price - available)}</b> more."
    await safe_edit(call, text, esim_confirm_keyboard(region, pkg_code, can_afford=can_afford))


@router.callback_query(EsimBuy.filter())
async def buy_esim(call: CallbackQuery, callback_data: EsimBuy) -> None:
    region, pkg_code = callback_data.code, callback_data.pkg
    await call.answer("Purchasing your eSIM…")
    ctx = get_ctx()
    pkg = await _catalog().package(region, pkg_code)
    if pkg is None:
        await safe_edit(call, "😔 That plan just changed. Pick another.", back_button("esim"))
        return
    try:
        order, profile = await esim_svc.esim_purchase(call.from_user.id, pkg, ctx.esim)
    except order_svc.InsufficientFunds:
        price = await pricing.esim_sell_price(pkg.cost)
        await safe_edit(
            call,
            f"💸 <b>Not enough balance</b>\n\n"
            f"💵 Price: <b>{money(price)}</b>\n\n"
            "💳 Top up and try again.",
            back_button("wallet"),
        )
        return
    except order_svc.PurchaseError as exc:
        await safe_edit(call, f"⚠️ {exc.user_message}", back_button("esim"))
        return

    await repo.update_order(order.id, chat_id=call.message.chat.id, message_id=call.message.message_id)
    order = await repo.get_order(order.id)
    await safe_edit(call, esim_svc.format_esim_card(order), esim_order_keyboard(order))
    if profile:
        await _send_qr(call, order)
    else:
        await call.answer("✅ eSIM ordered — your QR code will appear here in a moment.")


async def _send_qr(call_or_msg, order) -> None:
    """Send the QR-code image as a photo message (the card text is shown separately)."""
    prof = esim_svc.load_profile(order)
    qr = prof.get("qr")
    if not qr:
        return
    msg = getattr(call_or_msg, "message", call_or_msg)
    try:
        await msg.answer_photo(
            qr,
            caption=(
                f"📲 <b>Scan to install</b> — <b>{prof.get('pkg', 'your eSIM')}</b>\n\n"
                "Settings → Mobile/Cellular → Add eSIM → Use QR Code.\n\n"
                "<i>💡 Install over Wi-Fi.</i>"
            ),
        )
    except Exception:  # noqa: BLE001 — bad/unreachable QR url; the text card still has manual codes
        log.warning("could not send eSIM QR photo for order %s", order.id)


@router.callback_query(EsimAct.filter(F.action == "qr"))
async def resend_qr(call: CallbackQuery, callback_data: EsimAct) -> None:
    order = await repo.get_order(callback_data.id)
    if order is None or order.user_id != call.from_user.id:
        await call.answer("Order not found.", show_alert=True)
        return
    await call.answer("Sending QR…")
    await _send_qr(call, order)


@router.callback_query(EsimAct.filter(F.action == "usage"))
async def check_usage(call: CallbackQuery, callback_data: EsimAct) -> None:
    order = await repo.get_order(callback_data.id)
    if order is None or order.user_id != call.from_user.id:
        await call.answer("Order not found.", show_alert=True)
        return
    prof = esim_svc.load_profile(order)
    tran = prof.get("esimTranNo")
    if not tran:
        await call.answer("Usage not available yet.", show_alert=True)
        return
    try:
        rows = await get_ctx().esim.usage([tran])
    except Exception:  # noqa: BLE001
        await call.answer("Could not fetch usage right now. Try again.", show_alert=True)
        return
    if not rows:
        await call.answer("No usage data yet.", show_alert=True)
        return
    r = rows[0]
    used = int(r.get("orderUsage", 0) or 0)
    total = int(r.get("totalVolume", prof.get("vol", 0)) or 0)
    from esim.client import format_data

    left = max(0, total - used)
    await call.answer(
        f"Used {format_data(used)} of {format_data(total)} · {format_data(left)} left.",
        show_alert=True,
    )
