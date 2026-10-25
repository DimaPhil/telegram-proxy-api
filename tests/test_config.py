from __future__ import annotations

import base64

import pytest

from telegram_proxy_api.config import Settings


def test_session_paths_are_derived(tmp_path):
    settings = Settings(
        TELEGRAM_API_ID=1,
        TELEGRAM_API_HASH="hash",
        TELEGRAM_SESSION_DIR=tmp_path,
        TELEGRAM_SESSION_NAME="main",
    )

    assert settings.session_path == tmp_path / "main"
    assert settings.session_file_path == tmp_path / "main.session"


def test_validate_telegram_credentials_requires_values():
    settings = Settings(TELEGRAM_API_ID=0, TELEGRAM_API_HASH="")

    with pytest.raises(ValueError):
        settings.validate_telegram_credentials()


def test_app_port_defaults_to_4040():
    settings = Settings(TELEGRAM_API_ID=1, TELEGRAM_API_HASH="hash")

    assert settings.app_port == 4040


def test_app_port_uses_port_env_var(monkeypatch):
    monkeypatch.setenv("PORT", "5050")
    monkeypatch.delenv("APP_PORT", raising=False)

    settings = Settings(_env_file=None, TELEGRAM_API_ID=1, TELEGRAM_API_HASH="hash")

    assert settings.app_port == 5050


def test_app_port_falls_back_to_app_port_env_var(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setenv("APP_PORT", "6060")

    settings = Settings(_env_file=None, TELEGRAM_API_ID=1, TELEGRAM_API_HASH="hash")

    assert settings.app_port == 6060


def test_bot_defaults_are_disabled():
    assert Settings(_env_file=None).telegram_bot_mode == "off"


@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_bot_requires_credentials(mode):
    with pytest.raises(ValueError, match="requires a token"):
        Settings(_env_file=None, TELEGRAM_BOT_MODE=mode)


@pytest.mark.parametrize("key", ["", "bad", base64.urlsafe_b64encode(b"short").decode()])
def test_bot_rejects_invalid_store_key(key):
    with pytest.raises(ValueError, match="32 random bytes"):
        Settings(
            _env_file=None,
            TELEGRAM_BOT_MODE="primary",
            TELEGRAM_BOT_TOKEN="secret",
            TELEGRAM_BOT_OWNER_USER_ID=42,
            TELEGRAM_BOT_STORE_KEY=key,
        )


def test_bot_secrets_are_redacted():
    key = base64.urlsafe_b64encode(b"x" * 32).decode()
    settings = Settings(
        _env_file=None,
        TELEGRAM_BOT_MODE="shadow",
        TELEGRAM_BOT_TOKEN="unique-bot-secret",
        TELEGRAM_BOT_OWNER_USER_ID=42,
        TELEGRAM_BOT_STORE_KEY=key,
    )
    assert "unique-bot-secret" not in repr(settings)
    assert key not in repr(settings)


def test_invalid_bot_settings_do_not_print_credentials():
    with pytest.raises(ValueError) as caught:
        Settings(
            _env_file=None,
            TELEGRAM_BOT_MODE="primary",
            TELEGRAM_BOT_TOKEN="unique-invalid-settings-secret",
            TELEGRAM_BOT_OWNER_USER_ID=42,
            TELEGRAM_BOT_STORE_KEY="unique-invalid-store-secret",
        )
    assert "unique-invalid-settings-secret" not in str(caught.value)
    assert "unique-invalid-store-secret" not in str(caught.value)


@pytest.mark.parametrize("days", [0, 366])
def test_retention_bounds(days):
    with pytest.raises(ValueError):
        Settings(_env_file=None, TELEGRAM_BOT_RETENTION_DAYS=days)


@pytest.mark.parametrize(
    "url", ["https://user:pass@example.com", "http://example.com", "https://example.com?token=bad", "file:///tmp/bot"]
)
def test_bot_rejects_unsafe_base_urls(url):
    with pytest.raises(ValueError):
        Settings(_env_file=None, TELEGRAM_BOT_API_BASE_URL=url)
