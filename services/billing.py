"""Top-up crediting: deposit bonuses + referral bounty payout (single source).

Both credit points (wallet 'I have paid' and the payment poller) call
credit_topup so the bonus/referral logic lives in exactly one place.
"""
from __future__ import annotations

import logging
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING

from config import settings
from db import repo

if TYPE_CHECKING:
    from db.models import Payment

log = logging.getLogger(__name__)
_CENT = Decimal("0.01")


def _parse_tiers(raw: str) -> list[tuple[Decimal, Decimal]]:
    out: list[tuple[Decimal, Decimal]] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if ":" not in part:
            continue
        thr, pct = part.split(":", 1)
        try:
            out.append((Decimal(thr), Decimal(pct)))
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda t: t[0], reverse=True)  # biggest threshold first
    return out


async def deposit_bonus(user_id: int, amount: Decimal) -> Decimal:
    """Spend-only bonus for a top-up: the ladder bonus by amount, plus a
    first-ever-deposit bonus (capped)."""
    bonus = Decimal("0")
    for thr, pct in _parse_tiers(settings.topup_bonus_tiers):
        if amount >= thr:
            bonus += amount * pct / Decimal("100")
            break
    # The paying payment is already marked PAID at this point, so the first-ever
    # top-up is when the paid-payment count is exactly 1.
    if settings.topup_first_bonus_pct > 0 and await repo.count_paid_payments(user_id) <= 1:
        fb = amount * settings.topup_first_bonus_pct / Decimal("100")
        if settings.topup_first_bonus_cap > 0:
            fb = min(fb, settings.topup_first_bonus_cap)
        bonus += fb
    # Hard ceiling: total bonus never exceeds topup_bonus_max_pct of the amount.
    if settings.topup_bonus_max_pct > 0:
        bonus = min(bonus, amount * settings.topup_bonus_max_pct / Decimal("100"))
    return bonus.quantize(_CENT, rounding=ROUND_DOWN)


async def credit_topup(user_id: int, amount: Decimal) -> tuple[Decimal, Decimal]:
    """Credit a paid top-up WITH bonus and pay the referral bounty on the
    referee's first qualifying deposit. Returns (new_balance, bonus_credited)."""
    bonus = await deposit_bonus(user_id, amount)
    new_bal = await repo.credit(user_id, amount + bonus)
    try:
        if amount >= settings.referral_min_topup and settings.referral_bonus > 0:
            referrer = await repo.claim_referral_bonus(user_id)
            if referrer is not None:
                b = settings.referral_bonus
                new_bal = await repo.credit(user_id, b)        # referee bounty
                await repo.add_ref_earning(referrer, b)        # referrer bounty + earnings
                from services.context import get_ctx
                try:
                    await get_ctx().bot.send_message(
                        referrer,
                        f"🎉 Someone you invited just topped up — you earned "
                        f"{settings.currency_symbol}{b:.2f}! (now in your balance)",
                    )
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001 — never let referral logic break a real credit
        log.exception("referral payout failed for user %s", user_id)
    return new_bal, bonus


async def settle_payment(payment_id: int) -> tuple[bool, "Payment | None", Decimal, Decimal]:
    """Atomically mark a pending payment PAID and credit it. Idempotent: the
    atomic `mark_payment_paid` wins exactly once, so concurrent callers (webhook
    + payment poller + the 'I have paid' button) credit a top-up only once.

    Returns (credited_now, payment, new_balance, bonus). `credited_now` is True
    only on the first settlement, so the caller can notify the user exactly once.
    """
    payment = await repo.get_payment(payment_id)
    if payment is None:
        return False, None, Decimal("0"), Decimal("0")
    if not await repo.mark_payment_paid(payment_id):
        return False, payment, Decimal("0"), Decimal("0")  # already credited elsewhere
    new_bal, bonus = await credit_topup(payment.user_id, payment.amount)
    return True, payment, new_bal, bonus
