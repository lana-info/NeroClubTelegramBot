from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()


def _telegram_ids(value: str) -> tuple[int, ...]:
    result = []
    for item in value.split(","):
        item = item.strip()
        if item:
            result.append(int(item))
    return tuple(result)


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/nero.db")
    admin_api_token: str = os.getenv("ADMIN_API_TOKEN", "")
    payment_source: str = os.getenv("PAYMENT_SOURCE", "sheet").strip().lower()
    stripe_webhook_secret: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    stripe_secret_key: str = os.getenv("STRIPE_SECRET_KEY", "")
    stripe_price_id: str = os.getenv("STRIPE_PRICE_ID", "")
    checkout_success_url: str = os.getenv("CHECKOUT_SUCCESS_URL", "")
    checkout_cancel_url: str = os.getenv("CHECKOUT_CANCEL_URL", "")
    stripe_signature_tolerance_seconds: int = int(
        os.getenv("STRIPE_SIGNATURE_TOLERANCE_SECONDS", "300")
    )
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    reminders_dry_run: bool = os.getenv("REMINDERS_DRY_RUN", "false").lower() == "true"
    invites_dry_run: bool = os.getenv("INVITES_DRY_RUN", "false").lower() == "true"
    grace_period_days: int = int(os.getenv("GRACE_PERIOD_DAYS", "3"))
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_webhook_secret: str = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    telegram_webhook_url: str = os.getenv("TELEGRAM_WEBHOOK_URL", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    telegram_channel_id: str = os.getenv("TELEGRAM_CHANNEL_ID", "")
    admin_telegram_ids: tuple[int, ...] = _telegram_ids(os.getenv("ADMIN_TELEGRAM_IDS", ""))
    wordpress_base_url: str = os.getenv("WORDPRESS_BASE_URL", "").rstrip("/")
    wordpress_shared_secret: str = os.getenv("WORDPRESS_SHARED_SECRET", "")
    app_keys_encryption_key: str = os.getenv("APP_KEYS_ENCRYPTION_KEY", "")
    payment_url: str = os.getenv("PAYMENT_URL", "")
    new_member_price_usd: str = os.getenv("NEW_MEMBER_PRICE_USD", "20")
    returning_member_price_usd: str = os.getenv("RETURNING_MEMBER_PRICE_USD", "10")
    new_member_one_time_payment_url: str = os.getenv("NEW_MEMBER_ONE_TIME_PAYMENT_URL", "")
    new_member_recurring_payment_url: str = os.getenv("NEW_MEMBER_RECURRING_PAYMENT_URL", "")
    returning_member_one_time_payment_url: str = os.getenv("RETURNING_MEMBER_ONE_TIME_PAYMENT_URL", "")
    returning_member_recurring_payment_url: str = os.getenv("RETURNING_MEMBER_RECURRING_PAYMENT_URL", "")


settings = Settings()
