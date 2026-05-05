from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "development"
    log_level: str = "INFO"

    database_url: str
    database_url_sync: str | None = None

    jwt_secret: str = Field(..., min_length=16)
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 720

    supabase_url: str | None = None
    supabase_service_role_key: str | None = None
    supabase_incident_bucket: str = "incident-attachments"

    cors_origins: str = "http://localhost:3000"

    # AI agents (Anthropic Claude). Optional — when unset, the agents log
    # a warning and fall through gracefully so the workflow keeps working.
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-haiku-4-5-20251001"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    # Driver normalisation. SQLAlchemy picks a dialect from the URL prefix —
    # if the user pastes the bare `postgresql://...` URL from Supabase's
    # connection-string panel, the async engine rejects it because the
    # default psycopg2 dialect is sync-only. We rewrite the prefix so the
    # right driver is always selected.
    @property
    def async_database_url(self) -> str:
        return _force_driver(self.database_url, "asyncpg")

    @property
    def sync_database_url(self) -> str:
        return _force_driver(self.database_url_sync or self.database_url, "psycopg2")


def _force_driver(url: str, driver: str) -> str:
    """Rewrite the URL's driver prefix to match `driver`. Accepts:
      postgres://...                  (legacy Heroku-style)
      postgresql://...                (no driver — SQLAlchemy default)
      postgresql+asyncpg://...
      postgresql+psycopg2://...
    """
    target = f"postgresql+{driver}://"
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql+asyncpg://") or url.startswith("postgresql+psycopg2://"):
        # Replace whatever driver they wrote with the one this caller wants
        rest = url.split("://", 1)[1]
        return target + rest
    if url.startswith("postgresql://"):
        return target + url[len("postgresql://") :]
    return url  # let SQLAlchemy raise if the scheme is something else entirely


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
