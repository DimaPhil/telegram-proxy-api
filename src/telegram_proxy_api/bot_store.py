"""Disposable encrypted Secretary cache with a transactional update cursor."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class BotStoreError(Exception):
    """Cache corruption or invalid updates; safe to expose as a category only."""


class BotUpdateStore:
    def __init__(
        self,
        path: Path,
        key: str,
        owner_user_id: int,
        retention_days: int = 30,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        try:
            root_key = base64.b64decode(key, altchars=b"-_", validate=True)
        except (ValueError, UnicodeError):
            raise ValueError("Bot store key must be base64-encoded 32 bytes") from None
        if len(root_key) != 32:
            raise ValueError("Bot store key must be base64-encoded 32 bytes")
        if owner_user_id <= 0 or not 1 <= retention_days <= 365:
            raise ValueError("Invalid Bot store owner or retention")
        derived = HKDF(
            algorithm=hashes.SHA256(), length=64, salt=b"telegram-proxy-bot-store-v1", info=b"record-and-index-keys"
        ).derive(root_key)
        self._cipher = AESGCM(derived[:32])
        self._index_key = derived[32:]
        self._path = path
        self._owner = owner_user_id
        self._retention = retention_days * 86400
        self._clock = clock
        self._connection: sqlite3.Connection | None = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise BotStoreError("Bot store is not initialized")
        return self._connection

    def _index(self, kind: str, value: str | int) -> bytes:
        return hmac.new(self._index_key, f"{kind}\0{value}".encode(), hashlib.sha256).digest()

    def _encrypt(self, table: str, key: bytes, value: dict[str, Any]) -> bytes:
        nonce = os.urandom(12)
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        return nonce + self._cipher.encrypt(nonce, data, table.encode() + b"\0" + key)

    def _decrypt(self, table: str, key: bytes, value: bytes) -> dict[str, Any]:
        try:
            decoded = json.loads(self._cipher.decrypt(value[:12], value[12:], table.encode() + b"\0" + key))
        except (InvalidTag, ValueError, UnicodeError):
            raise BotStoreError("Bot cache authentication failed") from None
        if not isinstance(decoded, dict):
            raise BotStoreError("Invalid Bot cache record")
        return decoded

    def initialize(self) -> None:
        if self._connection is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create with private permissions before SQLite writes any content.
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self._path.chmod(0o600)
        self._connection = sqlite3.connect(self._path)
        self._db.executescript("""
            PRAGMA journal_mode=DELETE;
            CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS bot_connections (
                connection_key BLOB PRIMARY KEY, owner_key BLOB NOT NULL,
                update_id INTEGER NOT NULL, is_enabled INTEGER NOT NULL,
                encrypted_json BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bot_messages (
                connection_key BLOB NOT NULL, chat_key BLOB NOT NULL, message_key BLOB NOT NULL,
                update_id INTEGER NOT NULL, expires_at INTEGER NOT NULL, encrypted_json BLOB NOT NULL,
                PRIMARY KEY (connection_key, chat_key, message_key)
            );
            CREATE INDEX IF NOT EXISTS bot_messages_expiry ON bot_messages(expires_at);
        """)
        with self._db:
            marker = self._state("key_check")
            if marker is None:
                self._set_state("key_check", self._encrypt("bot_state", b"key_check", {"owner": self._owner}))
            elif self._decrypt("bot_state", b"key_check", bytes(marker)).get("owner") != self._owner:
                raise BotStoreError("Bot cache belongs to another owner")
            self._expire()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _state(self, key: str) -> Any:
        row = self._db.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set_state(self, key: str, value: str | bytes) -> None:
        self._db.execute(
            "INSERT INTO bot_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def next_update_offset(self) -> int | None:
        value = self._state("next_update_offset")
        return int(value) if value is not None else None

    def last_poll_success_at(self) -> float | None:
        value = self._state("last_poll_success_at")
        return float(value) if value is not None else None

    def prepare_poll(self, poll_timeout: int = 0) -> bool:
        """Discard ordering from an update-ID epoch that Telegram may reset.

        Telegram can randomize the next update ID after a week without new
        updates. Empty successful polls do not extend that epoch. Reset before
        sending an old offset, including a long poll crossing the boundary.
        """
        if self.next_update_offset() is None:
            return False
        last_update = self._state("last_nonempty_update_at")
        if last_update is not None and self._clock() + max(poll_timeout, 0) - float(last_update) < 7 * 86400:
            return False
        with self._db:
            self._db.execute("DELETE FROM bot_messages")
            self._db.execute("UPDATE bot_connections SET update_id = -1")
            self._db.execute("DELETE FROM bot_state WHERE key IN ('next_update_offset', 'last_nonempty_update_at')")
        return True

    def record_poll_success(self) -> None:
        with self._db:
            self._set_state("last_poll_success_at", str(self._clock()))
            self._expire()

    def prepare_startup(self) -> None:
        last_success = self.last_poll_success_at()
        with self._db:
            if last_success is None or self._clock() - last_success >= 86400:
                self._db.execute("DELETE FROM bot_messages")
            self._expire()

    def clear_messages(self) -> None:
        with self._db:
            self._db.execute("DELETE FROM bot_messages")

    def _expire(self) -> None:
        self._db.execute("DELETE FROM bot_messages WHERE expires_at <= ?", (int(self._clock()),))

    @staticmethod
    def _integer(value: Any) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise BotStoreError("Invalid Bot update identifier")
        return value

    @staticmethod
    def _string(value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise BotStoreError("Invalid Bot connection identifier")
        return value

    def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        key = self._index("connection", connection_id)
        row = self._db.execute("SELECT encrypted_json FROM bot_connections WHERE connection_key = ?", (key,)).fetchone()
        return self._decrypt("bot_connections", key, row[0]) if row else None

    def get_active_connection(self) -> dict[str, Any] | None:
        key = self._state("active_connection")
        if key is None:
            return None
        row = self._db.execute(
            "SELECT encrypted_json FROM bot_connections WHERE connection_key = ? AND is_enabled = 1", (key,)
        ).fetchone()
        return self._decrypt("bot_connections", bytes(key), row[0]) if row else None

    def _connection_update(self, connection: dict[str, Any], update_id: int, *, revalidation: bool = False) -> bool:
        user = connection.get("user")
        if not isinstance(user, dict):
            raise BotStoreError("Invalid Bot connection owner")
        if self._integer(user.get("id")) != self._owner:
            return False
        connection_id = self._string(connection.get("id"))
        enabled = connection.get("is_enabled")
        if not isinstance(enabled, bool):
            raise BotStoreError("Invalid Bot connection state")
        key = self._index("connection", connection_id)
        row = self._db.execute(
            "SELECT update_id, encrypted_json FROM bot_connections WHERE connection_key = ?", (key,)
        ).fetchone()
        if row and update_id <= row[0] and not revalidation:
            return enabled
        active_key = self._state("active_connection")
        changed = row is not None and self._decrypt("bot_connections", key, row[1]) != connection
        # Every new connection update can reflect changed selected chats even
        # when its JSON is identical; Telegram does not enumerate selections.
        if active_key != key or (row and (changed or not revalidation)) or not enabled:
            self._db.execute("DELETE FROM bot_messages")
        self._db.execute("UPDATE bot_connections SET is_enabled = 0")
        self._db.execute(
            "INSERT INTO bot_connections VALUES (?, ?, ?, ?, ?) ON CONFLICT(connection_key) DO UPDATE SET update_id=excluded.update_id, is_enabled=excluded.is_enabled, encrypted_json=excluded.encrypted_json",
            (
                key,
                self._index("owner", self._owner),
                max(update_id, row[0] if row else -1),
                int(enabled),
                self._encrypt("bot_connections", key, connection),
            ),
        )
        self._set_state("active_connection", key)
        return enabled

    def revalidate_connection(self, connection: dict[str, Any]) -> bool:
        with self._db:
            valid = self._connection_update(connection, -1, revalidation=True)
            if not valid:
                self._db.execute("DELETE FROM bot_messages")
                self._db.execute("UPDATE bot_connections SET is_enabled = 0")
            return valid

    def get_message(self, connection_id: str, chat_id: int, message_id: int) -> dict[str, Any] | None:
        active = self.get_active_connection()
        if active is None or active.get("id") != connection_id:
            return None
        keys = (
            self._index("connection", connection_id),
            self._index("chat", chat_id),
            self._index("message", message_id),
        )
        row = self._db.execute(
            "SELECT encrypted_json FROM bot_messages WHERE connection_key = ? AND chat_key = ? AND message_key = ? AND expires_at > ?",
            (*keys, int(self._clock())),
        ).fetchone()
        return self._decrypt("bot_messages", b"".join(keys), row[0]) if row else None

    def ingest_updates(self, updates: list[dict[str, Any]]) -> None:
        ordered = sorted(updates, key=lambda update: self._integer(update.get("update_id")))
        with self._db:
            offset = self.next_update_offset()
            previous_offset = offset
            for update in ordered:
                update_id = self._integer(update.get("update_id"))
                # A durable high-water mark also protects against resurrection
                # when an old message update is replayed after its deletion.
                if offset is not None and update_id < offset:
                    continue
                connection = update.get("business_connection")
                if connection is not None:
                    if not isinstance(connection, dict):
                        raise BotStoreError("Invalid Bot connection update")
                    self._connection_update(connection, update_id)
                for kind in ("business_message", "edited_business_message", "deleted_business_messages"):
                    payload = update.get(kind)
                    if payload is None:
                        continue
                    if not isinstance(payload, dict):
                        raise BotStoreError("Invalid Bot message update")
                    active = self.get_active_connection()
                    if active is None or payload.get("business_connection_id") != active.get("id"):
                        continue
                    chat = payload.get("chat")
                    if not isinstance(chat, dict):
                        raise BotStoreError("Invalid Bot message chat")
                    if chat.get("type") != "private":
                        continue
                    connection_key = self._index("connection", self._string(active.get("id")))
                    chat_key = self._index("chat", self._integer(chat.get("id")))
                    if kind == "deleted_business_messages":
                        message_ids = payload.get("message_ids")
                        if not isinstance(message_ids, list):
                            raise BotStoreError("Invalid Bot deletion update")
                        for message_id in message_ids:
                            self._db.execute(
                                "DELETE FROM bot_messages WHERE connection_key=? AND chat_key=? AND message_key=?",
                                (connection_key, chat_key, self._index("message", self._integer(message_id))),
                            )
                    else:
                        message_key = self._index("message", self._integer(payload.get("message_id")))
                        self._db.execute(
                            "INSERT INTO bot_messages VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(connection_key, chat_key, message_key) DO UPDATE SET update_id=excluded.update_id, expires_at=excluded.expires_at, encrypted_json=excluded.encrypted_json WHERE excluded.update_id > bot_messages.update_id",
                            (
                                connection_key,
                                chat_key,
                                message_key,
                                update_id,
                                int(self._clock()) + self._retention,
                                self._encrypt("bot_messages", connection_key + chat_key + message_key, payload),
                            ),
                        )
                offset = update_id + 1
                self._set_state("next_update_offset", str(offset))
            if offset != previous_offset:
                self._set_state("last_nonempty_update_at", str(self._clock()))
            self._expire()
