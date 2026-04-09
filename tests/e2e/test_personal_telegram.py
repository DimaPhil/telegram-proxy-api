from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from telegram_proxy_api.app import create_app
from telegram_proxy_api.config import Settings


pytestmark = pytest.mark.e2e


def _e2e_settings() -> tuple[Settings, dict[str, str]]:
    env_file = os.getenv("TELEGRAM_E2E_ENV_FILE")
    if not env_file:
        pytest.skip("Set TELEGRAM_E2E_ENV_FILE to run Telegram e2e tests.")

    settings = Settings(_env_file=env_file)
    required = {
        "chat_id": os.getenv("E2E_CHAT_ID"),
        "text_message_id": os.getenv("E2E_TEXT_MESSAGE_ID"),
        "media_message_id": os.getenv("E2E_MEDIA_MESSAGE_ID"),
    }
    if not all(required.values()):
        pytest.skip("E2E_CHAT_ID, E2E_TEXT_MESSAGE_ID, and E2E_MEDIA_MESSAGE_ID are required.")
    return settings, required


def test_real_account_endpoints():
    settings, values = _e2e_settings()
    settings.telegram_session_dir.mkdir(parents=True, exist_ok=True)

    app = create_app(settings=settings)
    with TestClient(app) as client:
        health = client.get("/healthz")
        me = client.get("/me")
        text_message = client.get(
            f"/chats/{values['chat_id']}/messages/{values['text_message_id']}"
        )
        media_manifest = client.get(
            f"/chats/{values['chat_id']}/messages/{values['media_message_id']}/media",
            params={"include_album": True},
        )

    assert health.status_code == 200
    assert me.status_code == 200
    assert text_message.status_code == 200
    assert media_manifest.status_code == 200
    assert isinstance(media_manifest.json()["data"], list)
