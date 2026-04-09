from __future__ import annotations

import builtins

import pytest

from telegram_proxy_api import auth_cli, main
from telegram_proxy_api.config import Settings


def test_main_runs_uvicorn(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: Settings(TELEGRAM_API_ID=1, TELEGRAM_API_HASH="hash", APP_HOST="127.0.0.1", APP_PORT=9000),
    )
    monkeypatch.setattr(
        main.uvicorn,
        "run",
        lambda app, host, port, log_level: captured.update(
            {"app": app, "host": host, "port": port, "log_level": log_level}
        ),
    )

    main.main()

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9000


def test_auth_cli_main_runs_asyncio(monkeypatch):
    captured = {}

    def fake_run(coro):
        captured["ran"] = True
        coro.close()

    monkeypatch.setattr(auth_cli.asyncio, "run", fake_run)

    auth_cli.main()

    assert captured["ran"] is True


@pytest.mark.asyncio
async def test_authenticate_uses_string_session_normalization(monkeypatch, tmp_path):
    settings = Settings(
        TELEGRAM_API_ID=1,
        TELEGRAM_API_HASH="hash",
        TELEGRAM_SESSION_DIR=tmp_path,
        TELEGRAM_SESSION_NAME="proxy",
        TELEGRAM_SESSION_STRING="session",
    )

    monkeypatch.setattr(auth_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(auth_cli, "normalize_string_session", lambda session, path: True)

    await auth_cli._authenticate()


@pytest.mark.asyncio
async def test_authenticate_returns_when_session_already_authorized(monkeypatch, tmp_path):
    settings = Settings(
        TELEGRAM_API_ID=1,
        TELEGRAM_API_HASH="hash",
        TELEGRAM_SESSION_DIR=tmp_path,
        TELEGRAM_SESSION_NAME="proxy",
    )

    class FakeClient:
        def __init__(self, *args, **kwargs):
            session = args[0]
            if hasattr(session, "close"):
                session.close()
            self.disconnected = False

        async def connect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def disconnect(self):
            self.disconnected = True

    monkeypatch.setattr(auth_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(auth_cli, "normalize_string_session", lambda session, path: False)
    monkeypatch.setattr(auth_cli, "TelegramClient", FakeClient)

    await auth_cli._authenticate()


@pytest.mark.asyncio
async def test_authenticate_interactive_sign_in(monkeypatch, tmp_path):
    settings = Settings(
        TELEGRAM_API_ID=1,
        TELEGRAM_API_HASH="hash",
        TELEGRAM_SESSION_DIR=tmp_path,
        TELEGRAM_SESSION_NAME="proxy",
    )

    events = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            session = args[0]
            if hasattr(session, "close"):
                session.close()
            self.authorized = False

        async def connect(self):
            events["connected"] = True

        async def is_user_authorized(self):
            return self.authorized

        async def send_code_request(self, phone):
            events["phone"] = phone

        async def sign_in(self, phone=None, code=None, password=None):
            events["sign_in"] = {"phone": phone, "code": code, "password": password}
            self.authorized = True

        async def disconnect(self):
            events["disconnected"] = True

    monkeypatch.setattr(auth_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(auth_cli, "normalize_string_session", lambda session, path: False)
    monkeypatch.setattr(auth_cli, "TelegramClient", FakeClient)
    monkeypatch.setattr(
        builtins,
        "input",
        lambda prompt="": "+123456789" if "phone" in prompt.lower() else "12345",
    )

    await auth_cli._authenticate()

    assert events["phone"] == "+123456789"
    assert events["sign_in"]["code"] == "12345"
    assert events["disconnected"] is True
