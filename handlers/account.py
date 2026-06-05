"""Account: spending summary, order history, referrals, one-tap buy-again."""
from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote

from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from country_flags import flag
from db import repo
from handlers.common import safe_edit
from keyboards.callbacks import CtyPick, Nav
from services.context import get_ctx
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

    # Referral / invite section.
    ctx = get_ctx()
    ref_cnt, ref_earn = await repo.get_referral_stats(uid)
    link = ""
    if ctx.bot_username:
        link = f"https://t.me/{ctx.bot_username}?start=ref_{uid}"
        lines.append(
            f"\n🎁 <b>Invite &amp; earn</b>\nYou and your friend BOTH get "
            f"<b>{money(settings.referral_bonus)}</b> when they top up "
            f"{money(settings.referral_min_topup)}+."
        )
        lines.append(f"👥 Invited: <b>{ref_cnt}</b> · Earned: <b>{money(ref_earn)}</b>")
        lines.append(f"🔗 <code>{link}</code>")

    lines.append("\n<b>Recent history</b>")
    if not orders:
        lines.append("<i>No orders yet — tap 📲 Buy number to start.</i>")
    else:
        for o in orders:
            icon = _KIND_ICON.get(o.kind, "📲")
            label = STATUS_LABELS.get(o.status, o.status)
            extra = f" · <code>{o.code}</code>" if o.code and o.kind == "sms" else ""
            name = o.service_name or o.service
            lines.append(f"{icon} #{o.id} {name} — {money(o.price)} — {label}{extra}")

    # Keyboard: one-tap buy-again for recent SMS combos + share invite.
    b = InlineKeyboardBuilder()
    seen: set[tuple[str, str]] = set()
    for o in orders:
        if o.kind == "sms" and (o.service, o.country) not in seen:
            seen.add((o.service, o.country))
            b.button(
                text=f"🔁 {flag(o.country)} {o.service_name or o.service}",
                callback_data=CtyPick(code=o.service, country=o.country),
            )
        if len(seen) >= 3:
            break
    if link:
        share = f"https://t.me/share/url?url={quote(link)}&text={quote('Get SMS numbers & eSIM data on NumberHub')}"
        b.button(text="📤 Share invite link", url=share)
    b.button(text="⬅️ Back", callback_data=Nav(to="main"))
    b.adjust(1)
    await safe_edit(call, "\n".join(lines), b.as_markup())
    await call.answer()
