from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
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

    telegram_bot_mode: Literal["off", "shadow", "primary"] = Field(default="off", alias="TELEGRAM_BOT_MODE")
    telegram_bot_token: SecretStr = Field(default=SecretStr(""), alias="TELEGRAM_BOT_TOKEN")
    telegram_bot_owner_user_id: int | None = Field(default=None, alias="TELEGRAM_BOT_OWNER_USER_ID", gt=0)
    telegram_bot_store_key: SecretStr = Field(default=SecretStr(""), alias="TELEGRAM_BOT_STORE_KEY")
    telegram_bot_api_base_url: str = Field(default="https://api.telegram.org", alias="TELEGRAM_BOT_API_BASE_URL")
    telegram_bot_request_timeout: float = Field(default=10, alias="TELEGRAM_BOT_REQUEST_TIMEOUT", gt=0)
    telegram_bot_poll_timeout: int = Field(default=30, alias="TELEGRAM_BOT_POLL_TIMEOUT", ge=1, le=50)
    telegram_bot_store_path: Path = Field(
        default=Path("/data/telegram/bot-cache.sqlite3"), alias="TELEGRAM_BOT_STORE_PATH"
    )
    telegram_bot_retention_days: int = Field(default=30, alias="TELEGRAM_BOT_RETENTION_DAYS", ge=1, le=365)

    @model_validator(mode="after")
    def validate_bot_configuration(self) -> Self:
        url = urlsplit(self.telegram_bot_api_base_url)
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Bot API base URL must be an HTTP(S) origin without credentials, query or fragment")
        if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Bot API base URL requires HTTPS except on localhost")
        if self.telegram_bot_mode != "off":
            if not self.telegram_bot_token.get_secret_value().strip() or not self.telegram_bot_owner_user_id:
                raise ValueError("Bot mode requires a token and positive owner user ID")
            try:
                key = base64.b64decode(self.telegram_bot_store_key.get_secret_value(), altchars=b"-_", validate=True)
            except (ValueError, binascii.Error):
                raise ValueError("Bot store key must be base64-encoded 32 random bytes") from None
            if len(key) != 32:
                raise ValueError("Bot store key must be base64-encoded 32 random bytes")
        return self

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
