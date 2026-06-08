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
    # Support contact shown to users for help (@username or a t.me/https link).
    support_contact: str = ""
    # Public channel @username shown to users (separate from post_channel's -100 id).
    channel_username: str = ""
    # Public marketing/legal website (Terms, Privacy, Refund, etc.).
    website_url: str = "https://numberhub.io"
    # Public support email shown to users.
    support_email: str = "info@numberhub.io"
    # Admin gets a DM when a provider's master balance drops below this (USD).
    low_balance_threshold: Decimal = Decimal("3")

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
    # Top-up deposit bonuses (spend-only credit). Base 5% on any top-up, scaling
    # to a hard max of 10%. First-ever deposit is bumped toward the max.
    topup_first_bonus_pct: Decimal = Decimal("10")
    topup_first_bonus_cap: Decimal = Decimal("0")   # 0 = no $ cap (the % cap governs)
    # "threshold:bonus%" pairs, biggest first wins. e.g. $100→+10%, $50→+7%, $1→+5%.
    topup_bonus_tiers: str = "1:5,50:7,100:10"
    # Hard ceiling on the TOTAL deposit bonus, as a % of the top-up amount.
    topup_bonus_max_pct: Decimal = Decimal("10")
    # Surge: extra commission % added when a number is out-of-stock (queued ⏳),
    # since the bot must bid higher to source it.
    queued_surge_pct: Decimal = Decimal("25")
    # Referral bounty (credit) to BOTH referrer and referee on the referee's first
    # qualifying top-up (>= referral_min_topup).
    referral_bonus: Decimal = Decimal("0.5")
    referral_min_topup: Decimal = Decimal("5")

    # Crypto payments — OxaPay (no-KYC). Merchant API key from the OxaPay
    # dashboard → your Merchant (payment gateway) → API key.
    oxapay_api_key: str = ""
    oxapay_asset: str = "USDT"

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
    def oxapay_enabled(self) -> bool:
        return bool(self.oxapay_api_key)

    @property
    def payments_enabled(self) -> bool:
        return self.oxapay_enabled or bool(self.cryptobot_token)

    @property
    def esim_enabled(self) -> bool:
        return bool(self.esim_access_code and self.esim_secret_key)

    @property
    def support_url(self) -> str:
        """Normalized https link to the support contact, or '' if unset."""
        c = self.support_contact.strip()
        if not c:
            return ""
        if c.startswith("http"):
            return c
        return f"https://t.me/{c.lstrip('@')}"

    @property
    def channel_url(self) -> str:
        """Normalized https link to the public channel, or '' if unset."""
        c = self.channel_username.strip()
        if not c:
            return ""
        if c.startswith("http"):
            return c
        return f"https://t.me/{c.lstrip('@')}"

    @property
    def contact_footer(self) -> str:
        """' 💬 Support: @x · 📢 Channel: @y · 🌐 site' line for the bot description."""
        parts = []
        if self.support_contact.strip():
            parts.append(f"💬 Support: {self.support_contact.strip()}")
        if self.support_email.strip():
            parts.append(f"📧 {self.support_email.strip()}")
        if self.channel_username.strip():
            parts.append(f"📢 Channel: {self.channel_username.strip()}")
        if self.website_url.strip():
            parts.append(f"🌐 {self.website_url.strip()}")
        return ("\n\n" + " · ".join(parts)) if parts else ""


settings = Settings()  # type: ignore[call-arg]
