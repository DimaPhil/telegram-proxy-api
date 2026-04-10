from __future__ import annotations

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
