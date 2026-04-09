from __future__ import annotations

from pathlib import Path

from telethon.sessions import SQLiteSession, StringSession


def normalize_string_session(session_string: str | None, session_path: Path) -> bool:
    if not session_string or session_path.with_suffix(".session").exists():
        return False

    session_path.parent.mkdir(parents=True, exist_ok=True)
    source = StringSession(session_string)
    if source.auth_key is None:
        return False

    target = SQLiteSession(str(session_path))
    target.set_dc(source.dc_id, source.server_address, source.port)
    target.auth_key = source.auth_key
    if hasattr(source, "takeout_id"):
        target.takeout_id = source.takeout_id
    target.save()
    target.close()
    return True
