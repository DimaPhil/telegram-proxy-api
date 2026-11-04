from __future__ import annotations

import base64
import sqlite3

import pytest

from telegram_proxy_api.bot_store import BotStoreError, BotUpdateStore

KEY = base64.urlsafe_b64encode(b"x" * 32).decode()
OWNER = 12345678901
CHAT = 998877665544
CONNECTION = "private-connection-identity"


def connection(update_id=1, *, enabled=True, owner=OWNER, connection_id=CONNECTION):
    return {
        "update_id": update_id,
        "business_connection": {"id": connection_id, "user": {"id": owner}, "is_enabled": enabled},
    }


def message(update_id=2, *, text="private-message-text", connection_id=CONNECTION, kind="business_message"):
    return {
        "update_id": update_id,
        kind: {
            "business_connection_id": connection_id,
            "chat": {"id": CHAT, "type": "private"},
            "message_id": 77889900,
            "text": text,
            "document": {"file_id": "secret-file-id"},
        },
    }


def deletion(update_id=3):
    return {
        "update_id": update_id,
        "deleted_business_messages": {
            "business_connection_id": CONNECTION,
            "chat": {"id": CHAT, "type": "private"},
            "message_ids": [77889900],
        },
    }


@pytest.fixture
def store(tmp_path):
    instance = BotUpdateStore(tmp_path / "cache.sqlite3", KEY, OWNER)
    instance.initialize()
    yield instance
    instance.close()


def test_encrypted_exact_message_and_durable_restart(store):
    store.ingest_updates([message(), connection()])
    assert store.next_update_offset() == 3
    assert store.get_message(CONNECTION, CHAT, 77889900)["text"] == "private-message-text"
    assert store.get_message("different", CHAT, 77889900) is None
    assert store.get_message(CONNECTION, CHAT, 42) is None
    path = store._path
    store.close()
    data = path.read_bytes()
    for secret in (str(OWNER), str(CHAT), CONNECTION, "77889900", "private-message-text", "secret-file-id"):
        assert secret.encode() not in data
    assert path.stat().st_mode & 0o777 == 0o600
    reopened = BotUpdateStore(path, KEY, OWNER)
    reopened.initialize()
    assert reopened.next_update_offset() == 3
    assert reopened.get_message(CONNECTION, CHAT, 77889900)["text"] == "private-message-text"
    reopened.close()


def test_edits_replay_and_deletion_do_not_resurrect(store):
    store.ingest_updates([connection(), message()])
    store.ingest_updates([message(3, text="edited", kind="edited_business_message")])
    store.ingest_updates([message()])
    assert store.get_message(CONNECTION, CHAT, 77889900)["text"] == "edited"
    store.ingest_updates([deletion(4)])
    store.ingest_updates([message(3, text="edited", kind="edited_business_message")])
    assert store.get_message(CONNECTION, CHAT, 77889900) is None
    assert store.next_update_offset() == 5


def test_regular_bot_messages_and_other_owner_never_enter_store(store):
    store.ingest_updates(
        [connection(owner=222), message(), message(3, kind="message"), message(4, kind="edited_message")]
    )
    assert store.get_active_connection() is None
    assert store.get_connection(CONNECTION) is None
    assert store.next_update_offset() == 5
    store.ingest_updates([connection(5), message(6, connection_id="wrong"), message(7, kind="message")])
    assert store.get_message(CONNECTION, CHAT, 77889900) is None


def test_connection_update_disable_and_replacement_clear_cache(store):
    store.ingest_updates([connection(), message()])
    store.ingest_updates([connection(3)])  # Even equal JSON may mean chat selection changed.
    assert store.get_message(CONNECTION, CHAT, 77889900) is None
    store.ingest_updates([message(4), connection(5, enabled=False)])
    assert store.get_active_connection() is None
    assert store.get_message(CONNECTION, CHAT, 77889900) is None
    store.ingest_updates([connection(6), message(7), connection(8, connection_id="replacement")])
    assert store.get_active_connection()["id"] == "replacement"
    assert store.get_message(CONNECTION, CHAT, 77889900) is None


def test_failed_batch_rolls_back_records_and_offset(store):
    store.ingest_updates([connection()])
    bad = message(3)
    bad["business_message"]["message_id"] = "bad-id"
    with pytest.raises(BotStoreError):
        store.ingest_updates([message(), bad])
    assert store.next_update_offset() == 2
    assert store.get_message(CONNECTION, CHAT, 77889900) is None


def test_failed_sql_transaction_does_not_advance_cursor(store):
    store.ingest_updates([connection()])
    store._db.execute(
        "CREATE TRIGGER fail_message BEFORE INSERT ON bot_messages BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.ingest_updates([message()])
    assert store.next_update_offset() == 2


def test_expiration_and_startup_poll_continuity(tmp_path):
    now = [100000.0]
    store = BotUpdateStore(tmp_path / "cache", KEY, OWNER, retention_days=1, clock=lambda: now[0])
    store.initialize()
    store.ingest_updates([connection(), message()])
    store.record_poll_success()
    now[0] += 86399
    store.prepare_startup()
    assert store.get_message(CONNECTION, CHAT, 77889900)
    now[0] += 1
    assert store.get_message(CONNECTION, CHAT, 77889900) is None
    store.prepare_startup()
    assert store.get_active_connection()
    assert store._db.execute("SELECT COUNT(*) FROM bot_messages").fetchone()[0] == 0
    store.close()


def test_missing_poll_success_clears_messages(store):
    store.ingest_updates([connection(), message()])
    store.prepare_startup()
    assert store.get_message(CONNECTION, CHAT, 77889900) is None


def test_revalidation_preserves_unchanged_but_disables_wrong_owner(store):
    payload = connection()["business_connection"]
    store.ingest_updates([connection(), message()])
    assert store.revalidate_connection(payload)
    assert store.get_message(CONNECTION, CHAT, 77889900)
    assert not store.revalidate_connection(connection(owner=999)["business_connection"])
    assert store.get_active_connection() is None
    assert store.get_message(CONNECTION, CHAT, 77889900) is None


def test_corruption_and_wrong_key_fail_closed(store):
    store.ingest_updates([connection(), message()])
    store._db.execute("UPDATE bot_messages SET encrypted_json = randomblob(50)")
    store._db.commit()
    with pytest.raises(BotStoreError, match="authentication"):
        store.get_message(CONNECTION, CHAT, 77889900)
    wrong = BotUpdateStore(store._path, base64.urlsafe_b64encode(b"y" * 32).decode(), OWNER)
    with pytest.raises(BotStoreError):
        wrong.initialize()
    wrong.close()


def test_owner_change_requires_new_cache(store):
    other_owner = BotUpdateStore(store._path, KEY, OWNER + 1)
    with pytest.raises(BotStoreError, match="another owner"):
        other_owner.initialize()
    other_owner.close()


@pytest.mark.parametrize("key", ["not-base64", base64.b64encode(b"short").decode()])
def test_invalid_key_rejected(tmp_path, key):
    with pytest.raises(ValueError):
        BotUpdateStore(tmp_path / "cache", key, OWNER)


def test_nonprivate_and_unverified_messages_ignored(store):
    store.ingest_updates([message()])
    update = message(4)
    update["business_message"]["chat"]["type"] = "group"
    store.ingest_updates([connection(3), update])
    assert store.get_message(CONNECTION, CHAT, 77889900) is None


def test_week_without_updates_resets_cursor_despite_successful_empty_polls(tmp_path):
    now = [100000.0]
    store = BotUpdateStore(tmp_path / "cache", KEY, OWNER, clock=lambda: now[0])
    store.initialize()
    assert not store.prepare_poll()
    store.ingest_updates([connection(10000), message(10001)])
    for day in range(1, 7):
        now[0] = 100000.0 + day * 86400
        store.ingest_updates([])
        store.record_poll_success()
        assert not store.prepare_poll()
    now[0] = 100000.0 + 7 * 86400
    store.record_poll_success()
    assert store.prepare_poll()
    assert store.next_update_offset() is None
    assert store.get_active_connection()["id"] == CONNECTION
    assert store.get_message(CONNECTION, CHAT, 77889900) is None
    assert not store.prepare_poll()  # Do not repeatedly reset a fresh epoch.
    store.ingest_updates([connection(10), message(11, text="new epoch")])
    assert store.next_update_offset() == 12
    assert store.get_message(CONNECTION, CHAT, 77889900)["text"] == "new epoch"
    store.close()


def test_week_idle_restart_and_long_poll_boundary(tmp_path):
    now = [100000.0]
    path = tmp_path / "cache"
    store = BotUpdateStore(path, KEY, OWNER, clock=lambda: now[0])
    store.initialize()
    store.ingest_updates([connection(10000), message(10001)])
    store.close()
    now[0] += 7 * 86400 - 20
    restarted = BotUpdateStore(path, KEY, OWNER, clock=lambda: now[0])
    restarted.initialize()
    assert not restarted.prepare_poll()
    assert restarted.prepare_poll(poll_timeout=30)
    restarted.ingest_updates([connection(50, enabled=False)])
    assert restarted.get_active_connection() is None
    assert restarted.next_update_offset() == 51
    restarted.close()


def test_legacy_cursor_without_epoch_timestamp_resets_conservatively(store):
    store.ingest_updates([connection(), message()])
    store._db.execute("DELETE FROM bot_state WHERE key = 'last_nonempty_update_at'")
    store._db.commit()
    assert store.prepare_poll()
    assert store.next_update_offset() is None
