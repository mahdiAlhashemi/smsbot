"""Account: spending summary + order history / receipts."""
from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.types import CallbackQuery

from db import repo
from handlers.common import safe_edit
from keyboards.callbacks import Nav
from keyboards.menus import back_button
from utils import STATUS_LABELS, money

router = Router(name="account")

_KIND_ICON = {"rent": "📱", "esim": "📶", "sms": "📲"}


@router.callback_query(Nav.filter(F.to == "account"))
async def open_account(call: CallbackQuery) -> None:
    uid = call.from_user.id
    user = await repo.get_user(uid)
    orders = await repo.get_user_orders(uid, limit=12)

    lines = ["👤 <b>Your account</b>\n"]
    bal = user.balance if user else Decimal("0")
    lines.append(f"💼 Balance: <b>{money(bal)}</b>")
    if user and user.held and user.held > 0:
        lines.append(f"🔒 On hold: {money(user.held)} · Available: <b>{money(user.available)}</b>")
    spent = user.total_spent if user else Decimal("0")
    lines.append(f"💸 Total spent: <b>{money(spent)}</b>")
    lines.append(f"📦 Orders: <b>{len(orders)}{'+' if len(orders) == 12 else ''}</b>")

    lines.append("\n<b>Recent history</b>")
    if not orders:
        lines.append("<i>No orders yet — tap 📲 Buy number to start.</i>")
    else:
        for o in orders:
            icon = _KIND_ICON.get(o.kind, "📲")
            label = STATUS_LABELS.get(o.status, o.status)
            # Show the delivered code only for SMS (rent/eSIM store JSON in `code`).
            extra = f" · <code>{o.code}</code>" if o.code and o.kind == "sms" else ""
            name = o.service_name or o.service
            lines.append(f"{icon} #{o.id} {name} — {money(o.price)} — {label}{extra}")

    await safe_edit(call, "\n".join(lines), back_button("main"))
    await call.answer()
