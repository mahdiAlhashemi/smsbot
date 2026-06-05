"""SQLAlchemy ORM models. All money is stored as Numeric and handled as Decimal."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Numeric, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class User(Base):
    __tablename__ = "users"

    # Telegram user id is the primary key.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    # Funds reserved by open orders that have not yet received a code.
    # Spendable balance = balance - held. Money only leaves `balance` when a
    # code actually arrives (charge-on-receive).
    held: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    language: Mapped[str] = mapped_column(String(8), default="en")
    total_spent: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Referrals: who invited this user, whether their signup bounty was paid, and
    # how much this user has earned by referring others (for display).
    referred_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ref_bonus_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    ref_earnings: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))

    @property
    def available(self) -> Decimal:
        """Spendable balance = balance minus funds held by open orders."""
        return (self.balance or Decimal("0")) - (self.held or Decimal("0"))


class ServiceStat(Base):
    """Per-(service,country) delivery stats — powers the success-rate badges.
    Incremented when a code arrives (delivered) or an order expires (expired)."""

    __tablename__ = "service_stats"

    service: Mapped[str] = mapped_column(String(16), primary_key=True)
    country: Mapped[str] = mapped_column(String(16), primary_key=True)
    delivered: Mapped[int] = mapped_column(default=0)
    expired: Mapped[int] = mapped_column(default=0)


class Order(Base):
    """One HeroSMS activation purchased on behalf of a customer."""

    __tablename__ = "orders"

    # Order status lifecycle.
    PENDING = "pending"      # queued: no number yet, retrying getNumber every minute
    WAITING = "waiting"      # number issued, waiting for SMS code
    RECEIVED = "received"    # a code arrived
    COMPLETED = "completed"  # activation finished/confirmed
    CANCELED = "canceled"    # cancelled, hold released
    EXPIRED = "expired"      # timed out, hold released

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # "sms" = single-code activation (charge on receive); "rent" = number rented
    # for a period that receives many SMS (charged upfront).
    kind: Mapped[str] = mapped_column(String(8), default="sms", index=True)
    activation_id: Mapped[str] = mapped_column(String(64), index=True)
    service: Mapped[str] = mapped_column(String(16))
    service_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str] = mapped_column(String(16))
    country_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str] = mapped_column(String(32))
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 4))   # what HeroSMS charged us
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))  # what we charged the customer
    status: Mapped[str] = mapped_column(String(16), default=WAITING, index=True)
    code: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Where the live order card is shown, so background pollers can edit it
    # in place (auto-refresh) instead of relying on a manual refresh button.
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Whether the HeroSMS number was actually released (setStatus=8 succeeded).
    # False when a cancel was rejected (e.g. too early) and needs a retry.
    hero_released: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))

    @property
    def is_open(self) -> bool:
        return self.status in (self.PENDING, self.WAITING, self.RECEIVED)


class Payment(Base):
    """A top-up attempt via a payment provider (Crypto Pay)."""

    __tablename__ = "payments"

    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    provider: Mapped[str] = mapped_column(String(32), default="cryptobot")
    invoice_id: Mapped[str] = mapped_column(String(64), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))   # USD value credited
    asset: Mapped[str] = mapped_column(String(16), default="USDT")
    status: Mapped[str] = mapped_column(String(16), default=PENDING, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Setting(Base):
    """Simple key/value store for runtime-editable settings (e.g. markup)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
