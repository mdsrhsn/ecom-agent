"""Application settings loaded from environment variables."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

    SHOPIFY_STORE_URL: str = ""
    SHOPIFY_ACCESS_TOKEN: str = ""
    SHOPIFY_WEBHOOK_SECRET: str = ""

    POSTEX_API_KEY: str = ""
    POSTEX_BASE_URL: str = "https://api.postex.pk/services/integration/api/order/v3"
    DAEWOO_API_KEY: str = ""
    DAEWOO_BASE_URL: str = ""
    DIGIDOKAAN_API_KEY: str = ""
    DIGIDOKAAN_BASE_URL: str = ""
    AVIX_API_KEY: str = ""
    AVIX_BASE_URL: str = ""

    WHATSAPP_PHONE_ID: str = ""
    WHATSAPP_ACCESS_TOKEN: str = ""

    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASS: str = ""

    OWNER_PHONE: str = ""
    TEAM_PHONES: str = ""
    OWNER_EMAIL: str = ""
    TEAM_EMAILS: str = ""

    DATABASE_URL: str = "sqlite:///./dev.db"

    APP_SECRET: str = "change-me"
    TIMEZONE: str = "Asia/Karachi"

    @property
    def team_phone_list(self) -> list[str]:
        return [p.strip() for p in self.TEAM_PHONES.split(",") if p.strip()]

    @property
    def all_notify_phones(self) -> list[str]:
        phones = []
        if self.OWNER_PHONE:
            phones.append(self.OWNER_PHONE)
        phones.extend(self.team_phone_list)
        return phones


settings = Settings()
