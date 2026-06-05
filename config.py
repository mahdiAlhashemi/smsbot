"""Application configuration, loaded from environment / .env file."""
from __future__ import annotations

from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Telegram
    bot_token: str
    admin_ids: str = ""
    # Channel the bot posts announcements to (e.g. @mychannel or -100123...).
    # The bot must be an admin of this channel.
    post_channel: str = ""

    # HeroSMS
    herosms_api_key: str
    herosms_base_url: str = "https://hero-sms.com/stubs/handler_api.php"

    # eSIM Access (esimaccess.com / RedteaGO) — data-plan eSIMs
    esim_access_code: str = ""
    esim_secret_key: str = ""
    esim_base_url: str = "https://api.esimaccess.com"

    # Pricing — "smart" two-knob model (see services/pricing.py):
    #   buy_ceiling    = max(default * (1 + bid_premium%), min_bid)  # bot bids this
    #   customer_price = buy_ceiling * (1 + markup%)                 # = commission
    # markup_percent is the reseller commission added on top of the bid ceiling.
    markup_percent: Decimal = Decimal("20")
    # How far ABOVE the provider's default/floor price the bot bids to win a
    # number faster (HeroSMS is a demand auction). Raise it to win high-demand
    # numbers the floor price can't get.
    bid_premium_percent: Decimal = Decimal("10")
    # Flat minimum bid: ensures cheap-floor but high-demand numbers (floor $0.03,
    # real market ~$0.85) are still won, without inflating already-expensive ones.
    # 0 = disabled (pure percentage bidding).
    min_bid: Decimal = Decimal("0")
    # eSIM commission (separate from SMS — eSIMs are fixed-price, no bid auction).
    esim_commission_percent: Decimal = Decimal("10")

    # Crypto payments — Heleket (formerly Cryptomus)
    heleket_merchant: str = ""
    heleket_api_key: str = ""

    # Crypto payments — Crypto Pay / @CryptoBot (alternative provider)
    cryptobot_token: str = ""
    cryptobot_testnet: bool = False
    cryptobot_asset: str = "USDT"

    # Behaviour
    order_timeout_min: int = 20
    poll_interval_sec: int = 5
    payment_poll_interval_sec: int = 12
    # Queue: when no numbers are in stock, retry getNumber every N seconds for
    # up to M minutes before giving up and releasing the hold.
    queue_retry_sec: int = 60
    queue_timeout_min: int = 30
    min_topup: Decimal = Decimal("1")
    currency_symbol: str = "$"

    # Storage
    db_url: str = "sqlite+aiosqlite:///smsbot.db"

    @property
    def admin_id_list(self) -> list[int]:
        return [int(x) for x in self.admin_ids.replace(" ", "").split(",") if x]

    @property
    def heleket_enabled(self) -> bool:
        return bool(self.heleket_merchant and self.heleket_api_key)

    @property
    def payments_enabled(self) -> bool:
        return self.heleket_enabled or bool(self.cryptobot_token)

    @property
    def esim_enabled(self) -> bool:
        return bool(self.esim_access_code and self.esim_secret_key)


settings = Settings()  # type: ignore[call-arg]
