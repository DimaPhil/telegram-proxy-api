from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = Field(default="Telegram Proxy API", alias="APP_NAME")
    app_env: str = Field(default="development", alias="APP_ENV")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(
        default=4040,
        validation_alias=AliasChoices("PORT", "APP_PORT"),
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    telegram_api_id: int = Field(default=0, alias="TELEGRAM_API_ID")
    telegram_api_hash: str = Field(default="", alias="TELEGRAM_API_HASH")
    telegram_session_name: str = Field(
        default="telegram_proxy",
        alias="TELEGRAM_SESSION_NAME",
    )
    telegram_session_string: str | None = Field(
        default=None,
        alias="TELEGRAM_SESSION_STRING",
    )
    telegram_session_dir: Path = Field(
        default=Path("/data/telegram"),
        alias="TELEGRAM_SESSION_DIR",
    )
    telegram_request_timeout: int = Field(default=10, alias="TELEGRAM_REQUEST_TIMEOUT")
    telegram_request_retries: int = Field(default=3, alias="TELEGRAM_REQUEST_RETRIES")
    telegram_connection_retries: int = Field(
        default=3,
        alias="TELEGRAM_CONNECTION_RETRIES",
    )
    telegram_retry_delay: int = Field(default=1, alias="TELEGRAM_RETRY_DELAY")
    telegram_flood_sleep_threshold: int = Field(
        default=5,
        alias="TELEGRAM_FLOOD_SLEEP_THRESHOLD",
    )
    telegram_entity_cache_limit: int = Field(
        default=5000,
        alias="TELEGRAM_ENTITY_CACHE_LIMIT",
    )
    telegram_history_wait_time: float = Field(
        default=1.0,
        alias="TELEGRAM_HISTORY_WAIT_TIME",
    )
    telegram_history_concurrency: int = Field(
        default=1,
        alias="TELEGRAM_HISTORY_CONCURRENCY",
    )
    telegram_album_window: int = Field(default=24, alias="TELEGRAM_ALBUM_WINDOW")

    default_page_size: int = Field(default=25, alias="DEFAULT_PAGE_SIZE")
    max_page_size: int = Field(default=100, alias="MAX_PAGE_SIZE")
    default_context_size: int = Field(default=5, alias="DEFAULT_CONTEXT_SIZE")
    max_context_size: int = Field(default=20, alias="MAX_CONTEXT_SIZE")

    api_auth_enabled: bool = Field(default=False, alias="API_AUTH_ENABLED")
    api_bearer_token: str | None = Field(default=None, alias="API_BEARER_TOKEN")

    @property
    def session_path(self) -> Path:
        return self.telegram_session_dir / self.telegram_session_name

    @property
    def session_file_path(self) -> Path:
        return self.session_path.with_suffix(".session")

    def validate_telegram_credentials(self) -> None:
        if not self.telegram_api_id or not self.telegram_api_hash:
            raise ValueError("Telegram API credentials are required")


@lru_cache
def get_settings() -> Settings:
    return Settings()
