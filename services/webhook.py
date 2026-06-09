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
    # DIAGNOSTIC: log the full body so we can see exactly what OxaPay reports for
    # invoice vs paid vs received amounts (to decide if overpayments should credit
    # the actual settled amount instead of the invoice amount).
    try:
        log.info("oxapay webhook body: %s", json.dumps(data)[:800])
    except Exception:  # noqa: BLE001
        pass

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


# ─── HeroSMS inbound webhook (instant code delivery) ─────────────────────────
def _client_ip(request: web.Request) -> str:
    """Real client IP — nginx forwards it as X-Forwarded-For; fall back to peer."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote or ""


def _herosms_activation_id(raw: bytes, request: web.Request) -> str | None:
    """Pull the activation id out of the webhook (JSON body, query, or form)."""
    keys = ("activationId", "activation_id", "id")
    try:
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            for k in keys:
                if data.get(k):
                    return str(data[k])
    except Exception:  # noqa: BLE001
        pass
    for k in keys:
        if request.query.get(k):
            return str(request.query[k])
    try:
        from urllib.parse import parse_qs
        form = parse_qs(raw.decode("utf-8"))
        for k in keys:
            if form.get(k):
                return str(form[k][0])
    except Exception:  # noqa: BLE001
        pass
    return None


async def _handle_herosms(request: web.Request) -> web.Response:
    """HeroSMS POSTs here when an SMS arrives. SECURITY + MONEY-SAFETY: we only
    accept whitelisted HeroSMS IPs, and we NEVER trust the payload's code — we just
    use it as a 'poke' to run the SAME authoritative getStatus→deliver_code path the
    5s poller uses (which charges exactly once via the atomic close_order gate). So
    a forged/replayed webhook can at most trigger a real status check, never a fake
    charge. The poller stays as the fallback if a webhook is missed."""
    ip = _client_ip(request)
    if ip not in settings.herosms_webhook_ip_list:
        log.warning("herosms webhook from non-whitelisted IP %s — rejected", ip)
        return web.Response(status=403, text="forbidden")
    raw = await request.read()
    aid = _herosms_activation_id(raw, request)
    if aid:
        try:
            from db import repo
            order = await repo.get_order_by_activation(aid)
            if order is not None:
                from services.pollers import _handle_order
                ctx = get_ctx()
                await _handle_order(ctx.bot, ctx.hero, order)
                log.info("herosms webhook: delivered/poked order %s (act=%s)", order.id, aid)
        except Exception:  # noqa: BLE001 — never fail the webhook; the poller is the safety net
            log.exception("herosms webhook: handling failed for act=%s", aid)
    return web.Response(text="ok")


async def _health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def start_webhook_server() -> web.AppRunner | None:
    """Start the localhost webhook listener when a callback URL is configured.
    Returns the runner (call .cleanup() on shutdown), or None if disabled."""
    oxapay_on = settings.oxapay_enabled and bool(settings.oxapay_callback_url)
    herosms_on = bool(settings.herosms_webhook_url)
    if not oxapay_on and not herosms_on:
        return None
    app = web.Application()
    if oxapay_on:
        app.router.add_post(settings.webhook_path, _handle_oxapay)
    if herosms_on:
        app.router.add_post(settings.herosms_webhook_path, _handle_herosms)
    app.router.add_get("/healthz", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.webhook_host, settings.webhook_port)
    await site.start()
    log.info("webhook server listening on http://%s:%s  (oxapay=%s herosms=%s)",
             settings.webhook_host, settings.webhook_port,
             settings.webhook_path if oxapay_on else "-",
             settings.herosms_webhook_path if herosms_on else "-")
    return runner
