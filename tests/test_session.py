from __future__ import annotations

from telegram_proxy_api import session as session_module


def test_normalize_string_session_copies_auth(monkeypatch, tmp_path):
    events = {}

    class FakeStringSession:
        def __init__(self, value):
            self.value = value
            self.auth_key = "auth-key"
            self.dc_id = 2
            self.server_address = "149.154.167.51"
            self.port = 443
            self.takeout_id = 77

    class FakeSQLiteSession:
        def __init__(self, path):
            events["path"] = path
            self.auth_key = None
            self.takeout_id = None

        def set_dc(self, dc_id, server, port):
            events["dc"] = (dc_id, server, port)

        def save(self):
            events["saved"] = True

        def close(self):
            events["closed"] = True

    monkeypatch.setattr(session_module, "StringSession", FakeStringSession)
    monkeypatch.setattr(session_module, "SQLiteSession", FakeSQLiteSession)

    result = session_module.normalize_string_session("session", tmp_path / "telegram_proxy")

    assert result is True
    assert events["dc"] == (2, "149.154.167.51", 443)
    assert events["saved"] is True
    assert events["closed"] is True


def test_normalize_string_session_skips_when_session_exists(tmp_path):
    session_path = tmp_path / "telegram_proxy"
    session_path.with_suffix(".session").touch()

    assert session_module.normalize_string_session("session", session_path) is False
