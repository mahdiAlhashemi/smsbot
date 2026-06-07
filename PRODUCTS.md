# New product lines (branch `feature/products`)

Three non-AI products, built on the existing Order/wallet/poller pattern. Each is
**config-gated** so it's inert on the live bot until you switch it on.

---

## 1. ✅ Long-term / dedicated numbers — DONE
Just extended the existing HeroSMS rent durations with 2/3/6-month options
(`services/rent.py` `RENT_DURATIONS`). No new provider, no new API. Sells
"account survival". Live as soon as this branch is merged — nothing to enable.

## 2. ✅ Temp email inboxes — DONE & TESTED
Disposable verification inboxes via **Mail.tm** (free, keyless public API).
**Charge-on-receive** (same money model as OTP): the flat fee is *held* on
creation and only *charged* when the first email arrives; released if none comes.

- New: `services/email_inbox.py`, `handlers/email.py`
- Wired: `config.py`, `keyboards/{callbacks,menus}.py`, `db/repo.py`
  (`get_open_email_orders`), `services/pollers.py` (`email_poller`),
  `handlers/__init__.py`, `handlers/common.py`, `handlers/fallback.py`
- Order: `kind="email"`, `phone`=address, `code`=JSON `{token,address,messages}`
- **Tested live**: inbox creation + message fetch against the real Mail.tm API ✅

**To enable** (in `.env`):
```
TEMP_EMAIL_ENABLED=true
TEMP_EMAIL_PRICE=0.15        # flat fee, charged only on first email
TEMP_EMAIL_WINDOW_MIN=20     # inbox lifetime
```
Then restart — a "📧 Temp email" button appears on the main menu.

## 3. 🟡 Proxies (IPRoyal) — PLAN (needs your reseller account)
Not built yet because the IPRoyal **reseller API** differs per account, and I
won't ship a provider client I can't verify/test. The moment you create an
[IPRoyal reseller account](https://iproyal.com) and share the API key, this is a
~half-day build following the **exact pattern** the eSIM line uses:

| Layer | File | What to add |
|---|---|---|
| Client | `proxies/client.py` (new) | IPRoyal reseller API: buy bulk GB/ports, mint per-user sub-credentials, query usage |
| Service | `services/proxies.py` (new) | `proxy_purchase()` → `try_hold` → API order → `charge_hold` (paid upfront like eSIM); `format_proxy_card()` |
| Handler | `handlers/proxies.py` (new) | browse plan (country/type/GB) → confirm → deliver credentials card |
| Config | `config.py` | `iproyal_api_key`, `iproyal_enabled`, `proxy_commission_percent` |
| UI | `keyboards/{callbacks,menus}.py` | `ProxyBuy`/`ProxyAct` callbacks, menu button, `proxy_order_keyboard` |
| Context | `services/context.py` + `bot.py` | wire the client when enabled |
| Order | `kind="proxy"`, `phone`=host:port, `code`=JSON `{user,pass,host,port,expires}` |

Money model: **paid upfront** (like eSIM) — proxies are a reserved resource, not
charge-on-receive. Margin = wholesale GB/port cost × `(1 + proxy_commission)`.

---

### Status
- Branch: `feature/products` (off `main`; live bot untouched)
- Long-term numbers + Temp email: **ready to merge** (temp email stays off until
  `TEMP_EMAIL_ENABLED=true`)
- Proxies: provide an IPRoyal reseller API key and I'll finish it
- Reminder: no product earns until wallet top-ups work — swap rejected Heleket
  for a no-KYC processor (OxaPay/NOWPayments) first.
