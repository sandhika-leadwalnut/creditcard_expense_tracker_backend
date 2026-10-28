from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "expense_tracker"

    jwt_secret: str = "change-me"
    jwt_expire_minutes: int = 720

    admin_email: str = "admin@leadwalnut.com"
    admin_password: str = "ChangeMe@123"
    admin_name: str = "Admin"
    allowed_email_domains: str = "leadwalnut.com,codewalnut.com"

    invoice_threshold: float = 10000
    upload_dir: str = "./uploads"
    max_upload_mb: int = 20
    cors_origins: str = "http://localhost:5173"

    @property
    def domains(self) -> list[str]:
        return [d.strip().lower() for d in self.allowed_email_domains.split(",") if d.strip()]

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
