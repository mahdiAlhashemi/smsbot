"""OxaPay payment webhook — instant top-up crediting via callback_url.

OxaPay POSTs a JSON body to our callback URL whenever an invoice changes state.
Flow: payer pays → OxaPay calls us → we verify the `HMAC` header (HMAC-SHA512 of
the EXACT raw body, keyed by the merchant API key) → on status "Paid" we credit.

Security: the signature is the only thing standing between a real callback and a
forged one that would mint free credit, so an unverified/forged body is rejected
before we touch the DB. Crediting goes through `billing.settle_payment`, which is
atomic + idempotent — the payment poller stays as a fallback, so a missed or
duplicate webhook never loses or double-credits funds.

The server binds to localhost only; nginx (hooks.numberhub.io, TLS) proxies to it.
"""
from __future__ import annotations

import json
import logging

from aiohttp import web

from config import settings
from keyboards.menus import wallet_keyboard
from services import billing
from services.context import get_ctx
from services.payments import OxaPay
from utils import money

log = logging.getLogger(__name__)


def _payment_id_from_order(order_id: str) -> int | None:
    """Invoices set order_id = "nh<payment.id>" (handlers/wallet.py)."""
    if not order_id or not order_id.startswith("nh"):
        return None
    try:
        return int(order_id[2:])
    except ValueError:
        return None


async def _handle_oxapay(request: web.Request) -> web.Response:
    raw = await request.read()
    sig = request.headers.get("HMAC", "")
    if not OxaPay.verify_callback(raw, sig, settings.oxapay_api_key):
        log.warning("oxapay webhook: bad/missing HMAC — rejected")
        return web.Response(status=401, text="invalid signature")

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return web.Response(status=400, text="bad request")

    order_id = str(data.get("order_id", ""))
    status = OxaPay.normalize_status(data.get("status", ""))
    pid = _payment_id_from_order(order_id)
    log.info("oxapay webhook: order=%s raw_status=%s -> %s", order_id, data.get("status"), status)

    if status == "paid" and pid is not None:
        try:
            credited, payment, new_bal, bonus = await billing.settle_payment(pid)
            if credited and payment is not None:
                bonus_line = f"\n🎁 Bonus: <b>+{money(bonus)}</b>" if bonus > 0 else ""
                try:
                    await get_ctx().bot.send_message(
                        payment.user_id,
                        f"✅ <b>Top-up received!</b>\n\n"
                        f"💳 Added: <b>{money(payment.amount)}</b>{bonus_line}\n"
                        f"💰 New balance: <b>{money(new_bal)}</b>",
                        reply_markup=wallet_keyboard(settings.payments_enabled),
                        disable_web_page_preview=True,
                    )
                except Exception:  # noqa: BLE001
                    log.debug("oxapay webhook: could not DM user %s", payment.user_id)
                log.info("oxapay webhook: credited payment %s (+%s, bonus %s)", pid, payment.amount, bonus)
        except Exception:  # noqa: BLE001
            # Don't fail the webhook on a transient DB error — the payment poller
            # is the safety net and will credit on its next cycle.
            log.exception("oxapay webhook: settle failed for %s", order_id)

    # OxaPay needs HTTP 200 + body "ok" to mark delivery successful (else it retries).
    return web.Response(text="ok")


async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def start_webhook_server() -> web.AppRunner | None:
    """Start the localhost webhook listener when a callback URL is configured.
    Returns the runner (call .cleanup() on shutdown), or None if disabled."""
    if not settings.oxapay_enabled or not settings.oxapay_callback_url:
        return None
    app = web.Application()
    app.router.add_post(settings.webhook_path, _handle_oxapay)
    app.router.add_get("/healthz", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.webhook_host, settings.webhook_port)
    await site.start()
    log.info("oxapay webhook listening on http://%s:%s%s (public: %s)",
             settings.webhook_host, settings.webhook_port, settings.webhook_path,
             settings.oxapay_callback_url)
    return runner
