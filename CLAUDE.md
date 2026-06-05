# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **Telegram bot (aiogram 3) that resells HeroSMS virtual numbers**. One HeroSMS master API key sits on the backend; each Telegram customer has an in-bot wallet, picks a service + country, gets a number, and the SMS code is delivered automatically. Selling price = HeroSMS cost × admin markup. Crypto top-ups via **Heleket** (preferred) or **Crypto Pay / @CryptoBot** (fallback). Live bot: `@TheNumberHubBot`.

## Commands

```bash
python bot.py                       # run the bot (long-running poller)

# Tests — ALWAYS override DB_URL so they don't touch the live smsbot.db:
DB_URL="sqlite+aiosqlite:///./_t.db" python test_offline.py    # HeroSMS parsing + pricing
DB_URL="sqlite+aiosqlite:///./_t.db" python test_handlers.py   # aiogram keyboards/callbacks
python test_db.py                   # repo/money logic (uses its own _test_smsbot.db already)
python -m py_compile <file.py>      # quick syntax check after edits
```

There is no lint/build step and no test runner — the `test_*.py` files are plain scripts that print `RESULT: N passed, M failed` and exit non-zero on failure. To run "one test", edit the relevant `test_*.py` `main()` (no pytest).

### Restart pattern (Windows, deps live in **system** Python — no venv)
```bash
taskkill //F //IM python.exe   # kill ALL python first — see "single instance" below
python bot.py                  # relaunch
```
On startup the log prints `Run polling`, `order poller started`, `queue poller started`, and (if payments configured) `payment poller started`.

## Architecture (the parts that span multiple files)

### Money model = CHARGE ON RECEIVE (the core invariant — do not regress)
The customer is **never charged at purchase**. Buying *holds* the price; the charge happens only when an SMS code arrives.
- `User.balance` = real funds; `User.held` = reserved by open orders; `User.available = balance - held`.
- Buy → `repo.try_hold` (atomic `UPDATE … WHERE balance-held >= amount`). No charge.
- Code arrives → `orders.deliver_code` → `repo.charge_hold` (`balance -= amt, held -= amt`). Charged exactly once.
- No code / cancel / expire → `orders.close_unfilled` → `repo.release_hold`. Charged $0.
- All money transitions are **atomic conditional UPDATEs** in `db/repo.py` (`try_hold`, `charge_hold`, `release_hold`, `close_order`, `mark_payment_paid`). `close_order` wins a status transition so refunds/charges happen once even when the poller and a manual action race. `try_debit`/`refund` still exist but are legacy (tests only).

### Order lifecycle (`db/models.py` `Order.status`)
`PENDING` (queued — no number yet, retrying `getNumber`) → `WAITING` (number issued, ~20 min life) → `RECEIVED` (code arrived, charged) → `COMPLETED` / `CANCELED` / `EXPIRED`. `replace_number` swaps a WAITING order's number for a fresh one, carrying the same hold.

### Cancellation windows (mirror HeroSMS's own policy — enforced UI + server-side)
Timing helpers live in `utils.py` (`activation_cancel_in`, `rent_cancel_state`); both the keyboard (`menus.py`) and the handler enforce them, and the pollers re-render cards so the buttons flip on their own.
- **Activation:** code window 20 min; **cancel only allowed after 2 min** (HeroSMS rejects an earlier cancel). Issue time is derived as `expires_at − ORDER_TIMEOUT_MIN`; cancel unlocks 2 min later. PENDING (no number yet) cancels free immediately.
- **Rent:** codes for the whole period; **cancel+refund only between 2 and 20 min** from start (`created_at`). `services/rent.cancel_rent_refund` closes the order atomically, cancels at HeroSMS (`setRentStatus=2`), and `repo.credit`s the upfront charge back. After 20 min, only `finish_rent` (no refund).

### Background pollers (`services/pollers.py`, started in `start_pollers()`)
- **order_poller** (`POLL_INTERVAL_SEC`, 5s): polls `getStatus` for WAITING orders → delivers code / charges; closes + releases on expiry or provider-cancel; re-renders the card to tick the 20-min countdown; **retry_release** re-attempts HeroSMS cancels that were rejected (SMS-Activate refuses cancel in the first ~2 min — tracked by `Order.hero_released`).
- **queue_poller** (`QUEUE_RETRY_SEC`, 60s): retries `getNumber` for PENDING orders up to `QUEUE_TIMEOUT_MIN`, then gives up + releases the hold.
- **payment_poller** (`PAYMENT_POLL_INTERVAL_SEC`, 12s): polls the payment provider per pending payment; credits on `paid`.

### Auto-refreshing cards
Order cards have **no refresh button**. When a card is shown, its `chat_id`/`message_id` are saved on the Order; the pollers edit that message in place (`_sync_card`) as status changes. A global error handler (`bot._on_error`) swallows benign Telegram errors ("query is too old", "message is not modified").

### HeroSMS client (`herosms/client.py`)
SMS-Activate-compatible API at `GET {base}?api_key=…&action=…` (base = `https://hero-sms.com/stubs/handler_api.php`). Responses are **mixed**: legacy plaintext (`ACCESS_BALANCE:…`, `STATUS_OK:code`, `ACCESS_NUMBER:id:phone`) and JSON (`getNumberV2`, `getCountries`, `getServicesList`, `getPrices`) — parsing tolerates both. **`getNumberV2` falls back to legacy `getNumber` on HTTP/route errors** (it returns 404 on this endpoint); only true business errors (`NO_NUMBERS`, `NO_BALANCE`, `BAD_KEY`, …) propagate.

### eSIM Access (`esim/client.py`, `services/esim.py`, `handlers/esim.py`)
A second product line: data-plan **eSIMs** from esimaccess.com (RedteaGO), independent of HeroSMS. Optional — only wired when `ESIM_ACCESS_CODE`/`ESIM_SECRET_KEY` are set (`settings.esim_enabled`); `AppContext.esim` + `.esim_catalog` are `None` otherwise.
- **API**: `POST https://api.esimaccess.com/api/v1/open/<action>`, JSON, envelope `{success, errorCode, errorMsg, obj}`. Prices are in **1/10000 USD** (112500 = $11.25) — converted at the client boundary. Actions used: `package/list`, `location/list`, `esim/order`, `esim/query`, `esim/usage/query`, `esim/cancel`.
- **Auth (every request)**: four headers — `RT-AccessCode`, `RT-Timestamp` (ms), `RT-RequestID` (uuid4), `RT-Signature = hex(HMAC_SHA256(timestamp+requestId+accessCode+rawBody, secretKey))`. Sign the **exact** body bytes sent (serialise once).
- **Money = paid upfront** (like rent, NOT charge-on-receive): `services/esim.esim_purchase` holds → `esim/order` → `charge_hold` (commission-only price via `pricing.commission_price`, no bid premium — eSIMs aren't an auction). On order failure the hold is released.
- **Delivery**: order returns an `orderNo`; the profile (QR) is fetched via `esim/query` — usually ready in seconds. `esim_purchase` does a short inline poll; the **esim_poller** (8s, `repo.get_open_esim_orders`, kind=`esim`, status WAITING) delivers late ones. The eSIM Order reuses existing columns: `activation_id`=orderNo, `phone`=ICCID, `code`=JSON of the profile (ac/qrCodeUrl/smdp/matchingId/…). WAITING→RECEIVED once the QR is delivered. The QR is sent as a **photo** (`send_photo` with `qrCodeUrl`); the text card carries the manual SM-DP+/activation codes.
- **Merchant balance** ($0 until the esimaccess account is funded): real orders fail with an `insufficient balance` error, mapped to a friendly "temporarily unavailable" message — browsing still works.

### Payment providers (`services/payments/`)
Pluggable; both implement the same interface: `make_invoice(amount, order_id) -> {invoice_id, pay_url}`, `invoice_status(invoice_id, order_id) -> "paid"|"pending"|"expired"`, `verify()`, `name`. `Heleket` (Cryptomus-style: `merchant` header + `sign = md5(base64(json_body)+api_key)`) is preferred over `CryptoPay`. `bot.py` picks one into `AppContext.payments`. Heleket returns `Api not active` until the merchant is approved by Heleket.

### Wiring
`services/context.py` holds a process-wide `AppContext` (bot, hero, catalog, payments), set once in `bot.py` and read via `get_ctx()` in handlers/pollers. Handlers are aiogram routers in `handlers/` registered by `handlers/__init__.register_handlers`; callbacks use typed factories in `keyboards/callbacks.py`. `services/catalog.py` caches services/countries (600s) and per-service prices (60s); the price list is the **full** country list (includes out-of-stock, which queue) with flag emojis from `country_flags.py`. Markup is runtime-editable (stored in the `settings` table, read live by `services/pricing.py`).

## Conventions & gotchas

- **No migration framework**, but `db.init_db()` now runs an **idempotent ADD COLUMN backfill** (`db/__init__.py` `_BACKFILL`) for the hand-added columns (`held`, `is_blocked`, `kind`, `chat_id`, `message_id`, `hero_released`, …) so a copied/older DB self-heals at startup. A genuinely new column still needs adding to both the model AND `_BACKFILL`. SQLAlchemy `create_all` alone does not alter existing tables.
- **SQLite runs in WAL** with `busy_timeout=30000` + `synchronous=NORMAL` (connect-event listener in `db/__init__.py`) so 6 concurrent pollers + handlers don't hit `database is locked` mid money-transition. Copying the DB live now means copying `smsbot.db` + `-wal` + `-shm` (or checkpoint first).
- **Money gates are atomic-first.** Any path that holds funds must win its status transition (`repo.close_order`) BEFORE `try_hold`, and release/roll-back on failure — see `orders.request_another_code`, `deliver_code` (releases hold if `charge_hold` fails), `replace_number` (try/except → release + cancel). Don't reorder these.
- **Routers**: `fallback` (free-text catch) MUST stay registered LAST; `BlockMiddleware` (drops blocked users) is an outer middleware on `dp`. `account` router = spending history. A `balance_alert_poller` DMs admins when a provider balance < `low_balance_threshold`.
- **Single polling instance only.** Two `python bot.py` processes against the same token cause endless `TelegramConflictError`. Always `taskkill //F //IM python.exe` before relaunching; verify the new log has `conflicts: 0`.
- **Tests can hit the live DB.** `test_offline.py`/`test_handlers.py` default to `smsbot.db` via `.env`; always pass an isolated `DB_URL`. **Never `rm smsbot.db`** while the bot runs (it holds the admin balance).
- **UTF-8 / emoji:** passing emoji/em-dash to the Telegram API via shell `curl` corrupts encoding (`strings must be encoded in UTF-8`). Do Telegram API calls that contain non-ASCII from Python/httpx (see `tools/setup_channel.py`).
- **Secrets** live in `.env` (gitignored): `BOT_TOKEN`, `HEROSMS_API_KEY`, `HELEKET_MERCHANT`/`HELEKET_API_KEY`, `ADMIN_IDS`, `POST_CHANNEL`. `config.py` (pydantic-settings) maps each to an env var of the upper-cased name.
- **Bot branding** (name/about/description) is re-applied on every startup by `bot._apply_branding`; `setMyName` is heavily flood-limited, so a warning there on restart is expected and harmless.
