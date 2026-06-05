"""Data-access helpers. Each function manages its own transaction.

Money safety: balance debits use a conditional UPDATE (``balance >= amount``) so a
customer can never be charged below zero even under concurrent requests.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select, update

from db import session_factory
from db.models import Order, Payment, Setting, User


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ─── Users ──────────────────────────────────────────────────────────────────
async def get_or_create_user(
    user_id: int, username: str | None, full_name: str | None, is_admin: bool
) -> User:
    async with session_factory() as s:
        user = await s.get(User, user_id)
        if user is None:
            user = User(
                id=user_id, username=username, full_name=full_name, is_admin=is_admin
            )
            s.add(user)
        else:
            user.username = username
            user.full_name = full_name
            if is_admin and not user.is_admin:
                user.is_admin = True
        await s.commit()
        await s.refresh(user)
        return user


async def get_user(user_id: int) -> User | None:
    async with session_factory() as s:
        return await s.get(User, user_id)


async def credit(user_id: int, amount: Decimal) -> Decimal:
    """Add funds to a user's balance. Returns the new balance."""
    async with session_factory() as s:
        await s.execute(
            update(User).where(User.id == user_id).values(balance=User.balance + amount)
        )
        await s.commit()
        user = await s.get(User, user_id)
        return user.balance if user else Decimal("0")


async def try_debit(user_id: int, amount: Decimal) -> bool:
    """Atomically subtract `amount` only if the balance covers it.

    Returns True on success, False if funds were insufficient.
    """
    async with session_factory() as s:
        result = await s.execute(
            update(User)
            .where(User.id == user_id, User.balance >= amount)
            .values(balance=User.balance - amount, total_spent=User.total_spent + amount)
        )
        await s.commit()
        return result.rowcount == 1


# ── Charge-on-receive holds ─────────────────────────────────────────────────
# Buying a number reserves (holds) the price. The customer is only actually
# charged (money leaves `balance`) when a code arrives. If no code arrives, the
# hold is released and nothing is charged.
async def try_hold(user_id: int, amount: Decimal) -> bool:
    """Reserve `amount` if the spendable balance (balance - held) covers it."""
    async with session_factory() as s:
        result = await s.execute(
            update(User)
            .where(User.id == user_id, (User.balance - User.held) >= amount)
            .values(held=User.held + amount)
        )
        await s.commit()
        return result.rowcount == 1


async def charge_hold(user_id: int, amount: Decimal) -> bool:
    """Finalize a hold into a real charge: money leaves balance, hold released."""
    async with session_factory() as s:
        result = await s.execute(
            update(User)
            .where(User.id == user_id, User.held >= amount, User.balance >= amount)
            .values(
                balance=User.balance - amount,
                held=User.held - amount,
                total_spent=User.total_spent + amount,
            )
        )
        await s.commit()
        return result.rowcount == 1


async def release_hold(user_id: int, amount: Decimal) -> None:
    """Release a hold without charging (clamped at 0)."""
    async with session_factory() as s:
        await s.execute(
            update(User)
            .where(User.id == user_id)
            .values(held=func_max_zero(User.held - amount))
        )
        await s.commit()


async def refund(user_id: int, amount: Decimal) -> None:
    """Return funds and roll back the recorded spend."""
    async with session_factory() as s:
        await s.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                balance=User.balance + amount,
                total_spent=func_max_zero(User.total_spent - amount),
            )
        )
        await s.commit()


def func_max_zero(expr):  # noqa: ANN001
    from sqlalchemy import case

    return case((expr < 0, 0), else_=expr)


async def set_blocked(user_id: int, blocked: bool) -> None:
    async with session_factory() as s:
        await s.execute(update(User).where(User.id == user_id).values(is_blocked=blocked))
        await s.commit()


# ─── Orders ─────────────────────────────────────────────────────────────────
async def create_order(**kwargs) -> Order:
    async with session_factory() as s:
        order = Order(**kwargs)
        s.add(order)
        await s.commit()
        await s.refresh(order)
        return order


async def get_order(order_id: int) -> Order | None:
    async with session_factory() as s:
        return await s.get(Order, order_id)


async def update_order(order_id: int, **values) -> None:
    values["updated_at"] = _now()
    async with session_factory() as s:
        await s.execute(update(Order).where(Order.id == order_id).values(**values))
        await s.commit()


async def close_order(order_id: int, to_status: str, from_statuses: tuple[str, ...]) -> bool:
    """Atomically transition an order's status. Returns True only for the caller
    that actually performed the transition (the order was in `from_statuses`).

    This is the single guard that makes refunds exactly-once: only the winner of
    this conditional UPDATE proceeds to refund.
    """
    async with session_factory() as s:
        result = await s.execute(
            update(Order)
            .where(Order.id == order_id, Order.status.in_(from_statuses))
            .values(status=to_status, updated_at=_now())
        )
        await s.commit()
        return result.rowcount == 1


async def fulfill_pending(
    order_id: int, activation_id: str, phone: str, cost: Decimal, expires_at: dt.datetime
) -> bool:
    """Atomically turn a PENDING order into a WAITING one once a number is found.

    Wins the PENDING→WAITING transition so a queued order is fulfilled exactly
    once even if the queue poller and a manual refresh both grab a number.
    """
    async with session_factory() as s:
        result = await s.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == Order.PENDING)
            .values(
                status=Order.WAITING,
                activation_id=activation_id,
                phone=phone,
                cost=cost,
                expires_at=expires_at,
                updated_at=_now(),
            )
        )
        await s.commit()
        return result.rowcount == 1


async def get_pending_orders() -> list[Order]:
    async with session_factory() as s:
        result = await s.execute(select(Order).where(Order.status == Order.PENDING))
        return list(result.scalars().all())


async def set_hero_released(order_id: int, released: bool) -> None:
    async with session_factory() as s:
        await s.execute(
            update(Order).where(Order.id == order_id).values(hero_released=released)
        )
        await s.commit()


async def get_orders_needing_release() -> list[Order]:
    """Closed orders whose HeroSMS number was never successfully released."""
    async with session_factory() as s:
        result = await s.execute(
            select(Order).where(
                Order.hero_released == False,  # noqa: E712
                Order.activation_id != "",
            )
        )
        return list(result.scalars().all())


async def get_open_orders() -> list[Order]:
    """Open SMS activations (the order poller uses getStatus on these)."""
    async with session_factory() as s:
        result = await s.execute(
            select(Order).where(
                Order.status.in_([Order.WAITING, Order.RECEIVED]),
                Order.kind == "sms",
            )
        )
        return list(result.scalars().all())


async def get_open_rent_orders() -> list[Order]:
    """Active rentals (the rent poller uses getRentStatus on these)."""
    async with session_factory() as s:
        result = await s.execute(
            select(Order).where(
                Order.status.in_([Order.WAITING, Order.RECEIVED]),
                Order.kind == "rent",
            )
        )
        return list(result.scalars().all())


async def get_open_esim_orders() -> list[Order]:
    """eSIM orders still being provisioned (the eSIM poller fetches their QR)."""
    async with session_factory() as s:
        result = await s.execute(
            select(Order).where(
                Order.status.in_([Order.WAITING]),
                Order.kind == "esim",
            )
        )
        return list(result.scalars().all())


async def has_open_order_for(user_id: int, service: str, country: str, kind: str = "sms") -> bool:
    """True if the user already has an open (pending/waiting/received) order of
    this kind for this exact service + country — used to block duplicate orders.
    Scoped by kind so an open SMS order doesn't block a rental of the same app."""
    async with session_factory() as s:
        result = await s.execute(
            select(Order.id)
            .where(
                Order.user_id == user_id,
                Order.service == service,
                Order.country == country,
                Order.kind == kind,
                Order.status.in_([Order.PENDING, Order.WAITING, Order.RECEIVED]),
            )
            .limit(1)
        )
        return result.first() is not None


async def get_user_orders(user_id: int, limit: int = 10) -> list[Order]:
    async with session_factory() as s:
        result = await s.execute(
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def get_user_open_orders(user_id: int) -> list[Order]:
    async with session_factory() as s:
        result = await s.execute(
            select(Order)
            .where(
                Order.user_id == user_id,
                Order.status.in_([Order.PENDING, Order.WAITING, Order.RECEIVED]),
            )
            .order_by(Order.id.desc())
        )
        return list(result.scalars().all())


# ─── Payments ───────────────────────────────────────────────────────────────
async def create_payment(**kwargs) -> Payment:
    async with session_factory() as s:
        payment = Payment(**kwargs)
        s.add(payment)
        await s.commit()
        await s.refresh(payment)
        return payment


async def get_payment(payment_id: int) -> Payment | None:
    async with session_factory() as s:
        return await s.get(Payment, payment_id)


async def mark_payment_paid(payment_id: int) -> bool:
    """Flip a pending payment to paid. Returns True only on the first transition
    (so we never credit a top-up twice)."""
    async with session_factory() as s:
        result = await s.execute(
            update(Payment)
            .where(Payment.id == payment_id, Payment.status == Payment.PENDING)
            .values(status=Payment.PAID, paid_at=_now())
        )
        await s.commit()
        return result.rowcount == 1


async def set_payment_invoice(payment_id: int, invoice_id: str) -> None:
    async with session_factory() as s:
        await s.execute(
            update(Payment).where(Payment.id == payment_id).values(invoice_id=invoice_id)
        )
        await s.commit()


async def expire_payment(payment_id: int) -> None:
    async with session_factory() as s:
        await s.execute(
            update(Payment)
            .where(Payment.id == payment_id, Payment.status == Payment.PENDING)
            .values(status=Payment.EXPIRED)
        )
        await s.commit()


async def get_pending_payments() -> list[Payment]:
    async with session_factory() as s:
        result = await s.execute(
            select(Payment).where(Payment.status == Payment.PENDING)
        )
        return list(result.scalars().all())


# ─── Settings ───────────────────────────────────────────────────────────────
async def get_setting(key: str) -> str | None:
    async with session_factory() as s:
        row = await s.get(Setting, key)
        return row.value if row else None


async def set_setting(key: str, value: str) -> None:
    async with session_factory() as s:
        row = await s.get(Setting, key)
        if row is None:
            s.add(Setting(key=key, value=value))
        else:
            row.value = value
        await s.commit()


async def get_all_user_ids() -> list[int]:
    async with session_factory() as s:
        result = await s.execute(select(User.id).where(User.is_blocked == False))  # noqa: E712
        return [row[0] for row in result.all()]


# ─── Stats (admin) ──────────────────────────────────────────────────────────
async def admin_stats() -> dict:
    from sqlalchemy import func

    async with session_factory() as s:
        users = await s.scalar(select(func.count(User.id)))
        balances = await s.scalar(select(func.coalesce(func.sum(User.balance), 0)))
        revenue = await s.scalar(select(func.coalesce(func.sum(User.total_spent), 0)))
        completed = await s.scalar(
            select(func.count(Order.id)).where(Order.status == Order.COMPLETED)
        )
        cost = await s.scalar(
            select(func.coalesce(func.sum(Order.cost), 0)).where(
                Order.status == Order.COMPLETED
            )
        )
        sold = await s.scalar(
            select(func.coalesce(func.sum(Order.price), 0)).where(
                Order.status == Order.COMPLETED
            )
        )
        open_orders = await s.scalar(
            select(func.count(Order.id)).where(
                Order.status.in_([Order.WAITING, Order.RECEIVED])
            )
        )
        return {
            "users": users or 0,
            "balances": Decimal(balances or 0),
            "revenue": Decimal(revenue or 0),
            "completed": completed or 0,
            "cost": Decimal(cost or 0),
            "sold": Decimal(sold or 0),
            "profit": Decimal(sold or 0) - Decimal(cost or 0),
            "open_orders": open_orders or 0,
        }
