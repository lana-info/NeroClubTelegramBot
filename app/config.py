from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/nero.db")
    admin_api_token: str = os.getenv("ADMIN_API_TOKEN", "")
    stripe_webhook_secret: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    stripe_signature_tolerance_seconds: int = int(
        os.getenv("STRIPE_SIGNATURE_TOLERANCE_SECONDS", "300")
    )
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    grace_period_days: int = int(os.getenv("GRACE_PERIOD_DAYS", "3"))
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_webhook_secret: str = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    wordpress_base_url: str = os.getenv("WORDPRESS_BASE_URL", "").rstrip("/")
    wordpress_shared_secret: str = os.getenv("WORDPRESS_SHARED_SECRET", "")
    app_keys_encryption_key: str = os.getenv("APP_KEYS_ENCRYPTION_KEY", "")


settings = Settings()
