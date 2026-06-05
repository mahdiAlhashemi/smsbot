"""Order lifecycle: purchase, deliver code, cancel/release, complete.

Money model — CHARGE ON RECEIVE:
  * Buying a number HOLDS the price (reserves it from spendable balance) but does
    NOT charge the customer.
  * The customer is charged (money leaves `balance`) only when an SMS code
    actually arrives — see `deliver_code`.
  * If no code arrives (cancel / expire / provider-cancel), the hold is released
    and the customer pays nothing.
  * A number can deliver several codes: each new code is a new hold + charge, so
    the customer can end up paying more than one number's base price.
"""
from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from db import repo
from db.models import Order
from herosms import HeroSMSClient, HeroSMSError, NoNumbersError
from services import pricing
from services.catalog import Catalog
from config import settings

log = logging.getLogger(__name__)


class PurchaseError(Exception):
    user_message = "Could not get a number. Please try again."


class InsufficientFunds(PurchaseError):
    user_message = "Not enough balance. Please top up your wallet."


class OutOfStock(PurchaseError):
    user_message = "No numbers available for this service/country right now."


class DuplicateOrder(PurchaseError):
    user_message = (
        "You already have an active order for this service and country. "
        "Finish or cancel it first (see 📦 My orders)."
    )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def purchase(
    user_id: int,
    service: str,
    country: str,
    cost_hint: Decimal,
    hero: HeroSMSClient,
    catalog: Catalog,
) -> Order:
    """Reserve (hold) the price, order a number, and persist the order.

    No money is charged here — only held. The customer is billed later, when a
    code arrives. The hold is released if the number can't be obtained.
    """
    # One open order per service+country — block duplicates.
    if await repo.has_open_order_for(user_id, service, country):
        raise DuplicateOrder()

    price = await pricing.sell_price(cost_hint)

    # 1) Hold the funds (atomic; fails if spendable balance too low). NOT a charge.
    if not await repo.try_hold(user_id, price):
        raise InsufficientFunds()

    # 2) Order the number from HeroSMS. We BID the buy-ceiling (default + bid
    #    premium), NOT the full customer price — so the commission always survives
    #    even if we pay our max bid. No fixedPrice => HeroSMS charges the real
    #    market price up to the ceiling, which is how we win high-demand numbers.
    #    Release the hold on any failure.
    max_price = await pricing.buy_ceiling(cost_hint)
    try:
        activation = await hero.get_number(service, country, max_price=max_price)
    except NoNumbersError:
        # No stock right now -> QUEUE the order and keep retrying in the
        # background. The hold stays; the customer is told it's processing.
        order = await repo.create_order(
            user_id=user_id,
            activation_id="",
            service=service,
            service_name=await catalog.service_name(service),
            country=country,
            country_name=await catalog.country_name(country),
            phone="",
            cost=cost_hint,  # expected cost, used to cap maxPrice on retries
            price=price,
            status=Order.PENDING,
            expires_at=_now() + dt.timedelta(minutes=settings.queue_timeout_min),
        )
        log.info("Order %s QUEUED (no stock): user=%s %s/%s price=%s",
                 order.id, user_id, service, country, price)
        return order
    except HeroSMSError as exc:
        await repo.release_hold(user_id, price)
        log.warning("get_number failed for user %s: %s", user_id, exc.code)
        raise PurchaseError() from exc
    except Exception as exc:  # noqa: BLE001 — network etc.
        await repo.release_hold(user_id, price)
        log.exception("get_number crashed for user %s", user_id)
        raise PurchaseError() from exc

    real_cost = activation.cost if activation.cost > 0 else cost_hint

    order = await repo.create_order(
        user_id=user_id,
        activation_id=activation.id,
        service=service,
        service_name=await catalog.service_name(service),
        country=country,
        country_name=await catalog.country_name(country),
        phone=activation.phone,
        cost=real_cost,
        price=price,
        status=Order.WAITING,
        expires_at=_now() + dt.timedelta(minutes=settings.order_timeout_min),
    )
    catalog.invalidate_prices(service)
    log.info(
        "Order %s HELD: user=%s %s/%s phone=%s cost=%s price=%s",
        order.id, user_id, service, country, activation.phone, real_cost, price,
    )
    return order


async def deliver_code(order: Order, code: str) -> bool:
    """A code arrived: charge the held funds and record the code.

    Wins the WAITING→RECEIVED transition atomically so the charge happens exactly
    once even if the poller and a manual refresh race. Returns True if THIS call
    delivered the code.
    """
    if not await repo.close_order(order.id, Order.RECEIVED, (Order.WAITING,)):
        return False
    charged = await repo.charge_hold(order.user_id, order.price)
    if not charged:
        # Hold missing (shouldn't happen) — deliver the code anyway, log it.
        log.warning("charge_hold failed for order %s (hold missing)", order.id)
    await repo.update_order(order.id, code=code)
    log.info("Order %s RECEIVED, charged=%s amount=%s", order.id, charged, order.price)
    return True


async def close_unfilled(
    order: Order,
    hero: HeroSMSClient,
    *,
    final_status: str,
    from_statuses: tuple[str, ...] = (Order.WAITING,),
    release_customer_hold: bool = True,
) -> bool:
    """Close an order that never produced a code: release the hold, no charge.

    Handles both WAITING (a number was issued — release it at HeroSMS) and
    PENDING (still queued — nothing to release). Atomic claim => releases once.
    `release_customer_hold=False` keeps the hold (used by replace_number, which
    carries the hold to the replacement order).
    """
    if not await repo.close_order(order.id, final_status, from_statuses):
        return False
    if order.activation_id:  # a number was issued -> release it at HeroSMS
        try:
            await hero.cancel(order.activation_id)
        except HeroSMSError as exc:
            # SMS-Activate rejects a cancel in the first ~2 min (and similar).
            # If the number is already gone, treat it as released; otherwise
            # flag it for the background release-retry loop.
            if exc.code in ("WRONG_ACTIVATION_ID", "NO_ACTIVATION"):
                pass
            else:
                log.warning("release %s rejected (%s) — will retry", order.activation_id, exc.code)
                await repo.set_hero_released(order.id, False)
    if release_customer_hold:
        await repo.release_hold(order.user_id, order.price)
    log.info("Order %s closed as %s (hold_released=%s)", order.id, final_status, release_customer_hold)
    return True


async def replace_number(order: Order, hero: HeroSMSClient, catalog: Catalog) -> Order | None:
    """Swap a WAITING order's number for a fresh one (same service/country/price).

    The customer hold carries over (no extra charge). The old number is released
    at HeroSMS (with retry). If no number is free, the replacement is queued.
    Returns the new order, or None if the swap couldn't be done.
    """
    service, country, price = order.service, order.country, order.price
    cost_hint = order.cost if order.cost and order.cost > 0 else None
    max_price = await pricing.buy_ceiling(cost_hint) if cost_hint else None

    # 1) Get the replacement number FIRST so the hold always has an owner.
    new_activation = None
    try:
        new_activation = await hero.get_number(service, country, max_price=max_price)
    except NoNumbersError:
        new_activation = None  # none free -> we'll queue the replacement
    except HeroSMSError as exc:
        log.warning("replace get_number failed for order %s: %s", order.id, exc.code)
        return None
    except Exception:  # noqa: BLE001
        log.exception("replace get_number crashed for order %s", order.id)
        return None

    # 2) Close the old order (release old number w/ retry), KEEP the customer hold.
    closed = await close_unfilled(
        order, hero, final_status=Order.CANCELED,
        from_statuses=(Order.PENDING, Order.WAITING), release_customer_hold=False,
    )
    if not closed:
        # Old order was closed by a racing poller — don't leave the new number orphaned.
        if new_activation:
            try:
                await hero.cancel(new_activation.id)
            except HeroSMSError:
                pass
        return None

    # 3) Create the replacement order, carrying the same hold and the same card.
    common = dict(
        user_id=order.user_id, service=service, service_name=order.service_name,
        country=country, country_name=order.country_name, price=price,
        chat_id=order.chat_id, message_id=order.message_id,
    )
    if new_activation:
        real_cost = new_activation.cost if new_activation.cost > 0 else (cost_hint or Decimal("0"))
        new_order = await repo.create_order(
            activation_id=new_activation.id, phone=new_activation.phone, cost=real_cost,
            status=Order.WAITING,
            expires_at=_now() + dt.timedelta(minutes=settings.order_timeout_min), **common,
        )
    else:
        new_order = await repo.create_order(
            activation_id="", phone="", cost=(cost_hint or Decimal("0")),
            status=Order.PENDING,
            expires_at=_now() + dt.timedelta(minutes=settings.queue_timeout_min), **common,
        )
    catalog.invalidate_prices(service)
    log.info("Order %s REPLACED by order %s (new phone=%s)", order.id, new_order.id, new_order.phone)
    return new_order


async def retry_release(order: Order, hero: HeroSMSClient) -> bool:
    """Retry cancelling a HeroSMS number that an earlier cancel couldn't release.
    Returns True once the number is released (or already gone)."""
    try:
        await hero.cancel(order.activation_id)
    except HeroSMSError as exc:
        if exc.code in ("WRONG_ACTIVATION_ID", "NO_ACTIVATION"):
            await repo.set_hero_released(order.id, True)  # already gone
            return True
        return False  # still too early / transient — try again next cycle
    except Exception:  # noqa: BLE001
        return False
    await repo.set_hero_released(order.id, True)
    log.info("Order %s number released at HeroSMS (retry succeeded)", order.id)
    return True


async def cancel_order(order: Order, hero: HeroSMSClient) -> bool:
    """User/admin cancel of a PENDING or WAITING order: release hold, no charge."""
    return await close_unfilled(
        order, hero, final_status=Order.CANCELED, from_statuses=(Order.PENDING, Order.WAITING)
    )


async def try_fulfill_pending(order: Order, hero: HeroSMSClient) -> bool:
    """Retry getting a number for a queued (PENDING) order. Returns True if a
    number was obtained and the order is now WAITING."""
    max_price = await pricing.buy_ceiling(order.cost) if order.cost > 0 else None
    try:
        activation = await hero.get_number(order.service, order.country, max_price=max_price)
    except (NoNumbersError, HeroSMSError):
        return False
    except Exception:  # noqa: BLE001 — network etc.
        log.exception("queue get_number crashed for order %s", order.id)
        return False
    real_cost = activation.cost if activation.cost > 0 else order.cost
    ok = await repo.fulfill_pending(
        order.id, activation.id, activation.phone, real_cost,
        _now() + dt.timedelta(minutes=settings.order_timeout_min),
    )
    if not ok:
        # Order is no longer pending (user cancelled / it expired). Release the
        # number we just grabbed so it isn't wasted.
        try:
            await hero.cancel(activation.id)
        except HeroSMSError:
            pass
        return False
    log.info("Order %s FULFILLED from queue: phone=%s cost=%s",
             order.id, activation.phone, real_cost)
    return True


async def complete_order(order: Order, hero: HeroSMSClient) -> None:
    """Finish a RECEIVED order (already charged) and confirm usage to HeroSMS."""
    if not await repo.close_order(order.id, Order.COMPLETED, (Order.RECEIVED,)):
        return
    try:
        await hero.finish(order.activation_id)
    except HeroSMSError as exc:
        log.warning("finish %s failed: %s", order.activation_id, exc.code)


# Result codes for request_another_code.
ANOTHER_OK = "OK"
ANOTHER_INSUFFICIENT = "INSUFFICIENT"
ANOTHER_ERROR = "ERROR"


async def request_another_code(order: Order, hero: HeroSMSClient) -> str:
    """Ask for another SMS on the same number. Holds the price again (each code
    is charged), then flips the order back to WAITING so the poller bills the
    next code on arrival."""
    if not await repo.try_hold(order.user_id, order.price):
        return ANOTHER_INSUFFICIENT
    try:
        await hero.request_another_code(order.activation_id)
    except HeroSMSError as exc:
        await repo.release_hold(order.user_id, order.price)
        log.warning("request_another_code %s failed: %s", order.activation_id, exc.code)
        return ANOTHER_ERROR
    # Reopen for the next code (clear the previous code so the new one shows).
    await repo.close_order(order.id, Order.WAITING, (Order.RECEIVED,))
    await repo.update_order(order.id, code=None)
    return ANOTHER_OK
