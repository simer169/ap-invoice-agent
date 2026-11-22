"""Settings loaded from .env. The only place secrets are read."""

from functools import lru_cache

from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=True)

    OPENROUTER_API_KEY: SecretStr
    EXTRACT_MODEL: str
    STRONG_MODEL: str
    DATABASE_URL: SecretStr
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: SecretStr
    AWS_PROFILE: str
    AWS_REGION: str = "ca-central-1"
    S3_BUCKET: str
    LANGFUSE_PUBLIC_KEY: SecretStr
    LANGFUSE_SECRET_KEY: SecretStr
    LANGFUSE_HOST: str
    SLACK_WEBHOOK_URL: SecretStr | None = None
    IMAP_HOST: str
    IMAP_USER: str
    IMAP_APP_PASSWORD: SecretStr

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_missing(cls, v: object) -> object:
        """Treat blank values (as in .env.example) as unset."""
        if isinstance(v, str) and not v.strip():
            return None
        return v


class DatabaseSettings(BaseSettings):
    """Only DATABASE_URL, so DB tooling doesn't need every other key configured."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=True)

    DATABASE_URL: SecretStr | None = None

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _blank_is_missing(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v


def get_database_url() -> str | None:
    """Return DATABASE_URL, or None when it is not configured."""
    url = DatabaseSettings().DATABASE_URL
    return url.get_secret_value() if url else None


class SettingsError(RuntimeError):
    """Raised when required settings are missing or invalid."""


@lru_cache
def get_settings() -> Settings:
    """Load settings, failing fast with a message listing every missing key."""
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        names = sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})
        raise SettingsError(
            "Missing or invalid settings in .env: "
            + ", ".join(names)
            + ". See .env.example for the full list of keys."
        ) from None
