from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from telegram_proxy_api.bot_client import BotApiError
from telegram_proxy_api.errors import AuthenticationRequiredError, TelegramProxyError
from telegram_proxy_api.gateway import MediaBundle, MediaStream, TelegramGateway, TelethonGateway
from telegram_proxy_api.models import (
    ChatDetail,
    ContactSummary,
    MessageContext,
    MessageDetail,
    PeerRef,
    ResolveResult,
    ResponseMeta,
)

CONNECTION = {"id": "connection", "user": {"id": 1}, "is_enabled": True}
MESSAGE = {
    "message_id": 10,
    "date": 1_700_000_000,
    "business_connection_id": "connection",
    "chat": {"id": 200, "type": "private", "first_name": "Peer"},
    "text": "cached",
    "document": {"file_id": "opaque", "file_unique_id": "unique", "file_size": 3, "file_name": "test.txt"},
}


async def chunks():
    yield b"abc"


class FakeBot:
    def __init__(self):
        self.start = AsyncMock(return_value={"id": 2, "is_bot": True})
        self.close = AsyncMock()
        self.get_webhook_info = AsyncMock(return_value={"url": ""})
        self.get_business_connection = AsyncMock(return_value=CONNECTION)
        self.get_chat = AsyncMock(return_value={"id": -100, "type": "supergroup", "title": "Bot group"})
        self.get_file = AsyncMock(return_value={"file_path": "documents/file.txt"})
        self.open_file = AsyncMock(side_effect=lambda _: chunks())
        self.updates = asyncio.Queue()
        self.updates.put_nowait(
            [
                {"update_id": 1, "business_connection": CONNECTION},
                {"update_id": 2, "business_message": MESSAGE},
            ]
        )
        self.calls = []

    async def get_updates(self, offset, timeout):
        self.calls.append((offset, timeout))
        if timeout == 0 and self.updates.empty():
            return []
        result = await self.updates.get()
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def hybrid_settings(settings, tmp_path):
    return settings.model_copy(
        update={
            "telegram_bot_mode": "primary",
            "telegram_bot_token": SecretStr("123:secret"),
            "telegram_bot_owner_user_id": 1,
            "telegram_bot_store_key": SecretStr(base64.urlsafe_b64encode(b"x" * 32).decode()),
            "telegram_bot_store_path": tmp_path / "bot.sqlite3",
        }
    )


@pytest.fixture
def telethon():
    provider = MagicMock(spec=TelethonGateway)
    provider.start = AsyncMock()
    provider.stop = AsyncMock()
    provider.is_connected.return_value = True
    provider.get_me = AsyncMock(return_value=ContactSummary(id="1", display_name="Owner"))
    provider.get_chat = AsyncMock(return_value=ChatDetail(id="200", title="Telethon chat", type="private"))
    provider.get_message = AsyncMock(return_value=MessageDetail(id="10", chat_id="200", text="historical"))
    provider.resolve = AsyncMock(return_value=ResolveResult(input="200", peer=PeerRef(id="200")))
    provider.list_contacts = AsyncMock(return_value=([], ResponseMeta(page=2)))
    provider.list_chats = AsyncMock(return_value=([], ResponseMeta(page=2)))
    provider.list_messages = AsyncMock(return_value=([], ResponseMeta(page=2)))
    provider.search_messages = AsyncMock(return_value=([], ResponseMeta(page=2)))
    provider.get_message_media = AsyncMock(return_value=([], ResponseMeta(include_album=True)))
    provider.get_message_context = AsyncMock(
        return_value=MessageContext(before=[], message=MessageDetail(id="10", chat_id="200"), after=[])
    )
    provider.stream_message_media = AsyncMock(side_effect=lambda **_: MediaStream("old.txt", "text/plain", 3, chunks()))
    provider.bundle_message_media = AsyncMock(return_value=MediaBundle("bundle.zip", b"zip"))
    return provider


@pytest.fixture
async def hybrid(hybrid_settings, telethon):
    bot = FakeBot()
    gateway = TelegramGateway(hybrid_settings, telethon=telethon, bot=bot)
    await gateway.start()
    yield gateway, bot, telethon
    await gateway.stop()


async def test_primary_cache_and_metadata_provenance(hybrid):
    gateway, bot, telethon = hybrid
    result = await gateway.get_message(chat_ref="200", message_id=10)
    assert result.data.text == "cached"
    assert result.meta.provider == "bot_api" and result.meta.fallback_used is False
    assert "business_connection_id" not in result.data.telegram_raw
    assert (await gateway.get_chat("-100")).meta.provider == "bot_api"
    assert (await gateway.resolve("@public_group")).meta.provider == "bot_api"
    assert (await gateway.get_chat("200")).meta.provider == "telethon"
    assert (await gateway.resolve("200")).meta.fallback_used is False
    health = gateway.health_status()
    assert health.status == "ok" and health.bot_api.secretary_connection_verified
    assert bot.calls[:2] == [(None, 0), (3, 0)]


async def test_unsupported_capabilities_never_call_bot(hybrid):
    gateway, bot, _ = hybrid
    results = [
        await gateway.get_me(),
        await gateway.list_contacts(page=2, page_size=1),
        await gateway.list_chats(page=2, page_size=1),
        await gateway.list_messages(chat_ref="200", page=2, page_size=1),
        await gateway.search_messages(query="find", chat_ref=None, page=2, page_size=1),
        await gateway.get_message_context(chat_ref="200", message_id=10, context_size=3),
        await gateway.get_message_media(chat_ref="200", message_id=10, include_album=True),
        await gateway.get_message(chat_ref="-100", message_id=10),
    ]
    assert all(result.meta.provider == "telethon" and result.meta.fallback_used is False for result in results)
    assert results[-3].meta.context_size == 3
    assert (
        await gateway.bundle_message_media(chat_ref="200", message_id=10, include_album=True)
    ).provider == "telethon"
    bot.get_chat.assert_not_called()
    bot.get_file.assert_not_called()


async def test_cache_miss_is_telethon_fallback(hybrid):
    gateway, _, telethon = hybrid
    result = await gateway.get_message(chat_ref="200", message_id=999)
    assert result.data.text == "historical"
    assert result.meta.fallback_used is True
    telethon.get_message.assert_awaited_once_with(chat_ref="200", message_id=999)


async def test_media_manifest_stream_and_large_file_fallback(hybrid):
    gateway, bot, telethon = hybrid
    manifest = await gateway.get_message_media(chat_ref="200", message_id=10, include_album=False)
    assert manifest.data[0].media_id == "10:0" and manifest.meta.provider == "bot_api"
    stream = await gateway.stream_message_media(chat_ref="200", message_id=10, media_id="10:0")
    assert stream.provider == "bot_api"
    assert b"".join([chunk async for chunk in stream.chunks]) == b"abc"
    assert gateway.store is not None
    large = {**MESSAGE, "document": {**MESSAGE["document"], "file_size": 21 * 1024 * 1024}}
    gateway.store.ingest_updates([{"update_id": 3, "edited_business_message": large}])
    bot.get_file.reset_mock()
    stream = await gateway.stream_message_media(chat_ref="200", message_id=10, media_id="10:0")
    assert stream.provider == "telethon" and stream.fallback_used
    bot.get_file.assert_not_called()
    # An album sibling is known unsupported, not a failed bot request.
    stream = await gateway.stream_message_media(chat_ref="200", message_id=10, media_id="11:0")
    assert stream.provider == "telethon" and not stream.fallback_used


@pytest.mark.parametrize("reason", ["unavailable", "invalid-response", "not-found"])
async def test_whitelisted_metadata_errors_fall_back(hybrid, reason):
    gateway, bot, _ = hybrid
    bot.get_chat.side_effect = BotApiError(reason)
    result = await gateway.get_chat("-100")
    assert result.meta.provider == "telethon" and result.meta.fallback_used
    assert gateway.health_status().status == "degraded"


@pytest.mark.parametrize("reason,status", [("auth-required", 503), ("rate-limited", 429), ("bad-request", 400)])
async def test_auth_rate_and_input_errors_never_fall_back(hybrid, reason, status):
    gateway, bot, telethon = hybrid
    bot.get_chat.side_effect = BotApiError(reason, retry_after=12 if status == 429 else None)
    with pytest.raises(BotApiError) as error:
        await gateway.get_chat("-100")
    assert error.value.status_code == status
    telethon.get_chat.assert_not_awaited()
    if reason in {"auth-required", "rate-limited"}:
        with pytest.raises(BotApiError):
            await gateway.get_message(chat_ref="200", message_id=10)
        telethon.get_message.assert_not_awaited()
    assert (await gateway.get_me()).meta.provider == "telethon"


@pytest.mark.parametrize("reason", ["file-too-large", "unavailable"])
async def test_file_preflight_failure_falls_back_before_response(hybrid, reason):
    gateway, bot, _ = hybrid
    bot.open_file.side_effect = BotApiError(reason)
    stream = await gateway.stream_message_media(chat_ref="200", message_id=10, media_id="10:0")
    assert stream.provider == "telethon" and stream.fallback_used


async def test_shutdown_cancels_poller_and_closes_resources(hybrid_settings, telethon):
    bot = FakeBot()
    gateway = TelegramGateway(hybrid_settings, telethon=telethon, bot=bot)
    await gateway.start()
    await gateway.start()
    await asyncio.sleep(0)
    task = gateway._poll_task
    await gateway.stop()
    assert task.done()
    bot.close.assert_awaited_once()
    telethon.stop.assert_awaited_once()
    assert gateway.health_status().status == "degraded"


async def test_off_never_starts_bot(hybrid_settings, telethon):
    bot = FakeBot()
    gateway = TelegramGateway(
        hybrid_settings.model_copy(update={"telegram_bot_mode": "off"}), telethon=telethon, bot=bot
    )
    await gateway.start()
    assert (await gateway.get_chat("-100")).meta.provider == "telethon"
    bot.start.assert_not_awaited()
    assert gateway.store is None
    await gateway.stop()


async def test_shadow_keeps_reads_on_telethon_after_bot_auth_failure(hybrid_settings, telethon):
    bot = FakeBot()
    bot.start.side_effect = BotApiError("auth-required")
    gateway = TelegramGateway(
        hybrid_settings.model_copy(update={"telegram_bot_mode": "shadow"}), telethon=telethon, bot=bot
    )
    await gateway.start()
    assert gateway.health_status().status == "degraded"
    assert (await gateway.get_message(chat_ref="200", message_id=10)).meta.provider == "telethon"
    await gateway.stop()


async def test_bot_survives_telethon_start_failure(hybrid_settings, telethon):
    telethon.start.side_effect = AuthenticationRequiredError()
    gateway = TelegramGateway(hybrid_settings, telethon=telethon, bot=FakeBot())
    await gateway.start()
    assert (await gateway.get_message(chat_ref="200", message_id=10)).meta.provider == "bot_api"
    assert gateway.health_status().telegram_connected and gateway.health_status().status == "degraded"
    with pytest.raises(TelegramProxyError) as error:
        await gateway.list_chats(page=1, page_size=10)
    assert error.value.status_code == 503
    await gateway.stop()


async def test_owner_mismatch_disables_bot_without_hiding_it(hybrid_settings, telethon):
    telethon.get_me.return_value = ContactSummary(id="999", display_name="Other owner")
    bot = FakeBot()
    gateway = TelegramGateway(hybrid_settings, telethon=telethon, bot=bot)
    await gateway.start()
    assert gateway.health_status().bot_api.last_error == "owner-mismatch"
    with pytest.raises(TelegramProxyError) as error:
        await gateway.get_message(chat_ref="200", message_id=10)
    assert error.value.status_code == 503
    bot.start.assert_not_awaited()
    assert (await gateway.get_me()).data.id == "999"
    await gateway.stop()


async def test_webhook_is_not_deleted_or_polled(hybrid_settings, telethon):
    bot = FakeBot()
    bot.get_webhook_info.return_value = {"url": "https://example.test/webhook"}
    gateway = TelegramGateway(hybrid_settings, telethon=telethon, bot=bot)
    await gateway.start()
    assert gateway.health_status().bot_api.last_error == "webhook-configured"
    assert bot.calls == [] and gateway._poll_task is None
    await gateway.stop()


async def test_poll_failure_blocks_cache_until_drain(hybrid):
    gateway, bot, _ = hybrid
    gateway._record_error(BotApiError("unavailable"))
    assert (await gateway.get_message(chat_ref="200", message_id=10)).meta.provider == "telethon"
    bot.updates.put_nowait(
        [
            {
                "update_id": 3,
                "deleted_business_messages": {
                    "business_connection_id": "connection",
                    "chat": MESSAGE["chat"],
                    "message_ids": [10],
                },
            }
        ]
    )
    await gateway._drain()
    assert gateway._cache_ready
    assert (await gateway.get_message(chat_ref="200", message_id=10)).meta.provider == "telethon"
    assert gateway.store.next_update_offset() == 4


async def test_concurrent_auth_failure_cannot_be_cleared_by_poll_success(hybrid):
    gateway, _, _ = hybrid
    gateway._record_error(BotApiError("auth-required"))
    await gateway._drain()
    with pytest.raises(BotApiError):
        await gateway.get_message(chat_ref="200", message_id=10)


async def test_no_connection_stays_degraded(hybrid_settings, telethon):
    bot = FakeBot()
    bot.updates.get_nowait()
    gateway = TelegramGateway(hybrid_settings, telethon=telethon, bot=bot)
    await gateway.start()
    assert gateway.health_status().status == "degraded"
    assert not gateway.health_status().bot_api.secretary_connection_verified
    assert (await gateway.get_message(chat_ref="200", message_id=10)).meta.provider == "telethon"
    await gateway.stop()


async def test_poll_retry_backoff_and_terminal_auth(hybrid, monkeypatch):
    gateway, bot, _ = hybrid
    gateway._poll_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await gateway._poll_task
    for _ in range(7):
        bot.updates.put_nowait(BotApiError("unavailable"))
    bot.updates.put_nowait(BotApiError("auth-required"))
    pauses = []

    async def sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr("telegram_proxy_api.gateway.asyncio.sleep", sleep)
    await gateway._poll_loop()
    assert pauses == [1, 2, 4, 8, 16, 30, 30]
    assert gateway.health_status().bot_api.last_error == "auth-required"


async def test_poller_rate_limit_waits_without_telethon_bypass(hybrid, monkeypatch):
    gateway, bot, telethon = hybrid
    gateway._poll_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await gateway._poll_task
    bot.updates.put_nowait(BotApiError("rate-limited", retry_after=7))
    bot.updates.put_nowait(BotApiError("auth-required"))
    pauses = []

    async def sleep(seconds):
        pauses.append(seconds)
        with pytest.raises(BotApiError) as error:
            await gateway.get_message(chat_ref="200", message_id=10)
        assert error.value.status_code == 429
        gateway._retry_until = 0

    monkeypatch.setattr("telegram_proxy_api.gateway.asyncio.sleep", sleep)
    await gateway._poll_loop()
    assert pauses == [7]
    telethon.get_message.assert_not_awaited()


async def test_connection_revalidation_mismatch_clears_cache(hybrid):
    gateway, bot, _ = hybrid
    bot.get_business_connection.return_value = {**CONNECTION, "user": {"id": 999}}
    await gateway._drain()
    assert gateway.health_status().bot_api.last_error == "owner-mismatch"
    assert gateway.store.get_message("connection", 200, 10) is None
    with pytest.raises(TelegramProxyError):
        await gateway.get_chat("-100")


async def test_connection_disabled_clears_cache_and_routes_to_telethon(hybrid):
    gateway, bot, _ = hybrid
    bot.get_business_connection.return_value = {**CONNECTION, "is_enabled": False}
    await gateway._drain()
    assert not gateway.health_status().bot_api.secretary_connection_verified
    assert gateway.store.get_message("connection", 200, 10) is None
    assert (await gateway.get_message(chat_ref="200", message_id=10)).meta.provider == "telethon"


async def test_concurrent_failure_cannot_override_rate_limit(hybrid):
    gateway, _, _ = hybrid
    gateway._record_error(BotApiError("rate-limited", retry_after=20))
    gateway._record_error(BotApiError("unavailable"))
    with pytest.raises(BotApiError) as error:
        await gateway.get_message(chat_ref="200", message_id=10)
    assert error.value.status_code == 429


async def test_corrupted_cache_degrades_and_falls_back(hybrid, monkeypatch):
    gateway, _, _ = hybrid

    def corrupt(*args):
        raise ValueError("not public")

    monkeypatch.setattr(gateway.store, "get_message", corrupt)
    result = await gateway.get_message(chat_ref="200", message_id=10)
    assert result.meta.fallback_used
    assert gateway.health_status().bot_api.last_error == "invalid-response"


async def test_private_bot_username_is_not_used_as_personal_peer(hybrid):
    gateway, bot, telethon = hybrid
    bot.get_chat.return_value = {"id": 200, "type": "private", "first_name": "Peer"}
    assert (await gateway.resolve("@peer")).meta.provider == "telethon"
    assert (await gateway.get_chat("@peer")).meta.provider == "telethon"


async def test_invalid_cached_message_shape_falls_back(hybrid, monkeypatch):
    gateway, _, _ = hybrid
    monkeypatch.setattr(gateway.store, "get_message", lambda *args: {"message_id": 10})
    assert (await gateway.get_message(chat_ref="200", message_id=10)).meta.fallback_used


async def test_idle_epoch_reset_disables_reads_before_poll_and_revalidation(hybrid, monkeypatch):
    gateway, bot, _ = hybrid
    original_clock = gateway.store._clock
    future = original_clock() + 8 * 86400
    monkeypatch.setattr(gateway.store, "_clock", lambda: future)
    await gateway._poll_once(30)
    assert bot.calls[-1] == (None, 0)
    assert not gateway._cache_ready and gateway._verified_connection_id is None
    assert gateway.store.get_message("connection", 200, 10) is None
    await gateway._drain()
    assert gateway._cache_ready and gateway._verified_connection_id == "connection"


class ClosableChunks:
    def __init__(self, error=None):
        self.closed = False
        self.error = error

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.error is not None:
            raise self.error
        raise StopAsyncIteration

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("stage", ["get_file", "open_file"])
@pytest.mark.parametrize("change", ["delete", "revoke", "edit", "auth-required", "rate-limited"])
async def test_stream_rechecks_record_and_access_after_preflight(hybrid, stage, change):
    gateway, bot, telethon = hybrid
    opened = ClosableChunks()

    async def preflight(_):
        if change == "delete":
            gateway.store.ingest_updates(
                [
                    {
                        "update_id": 3,
                        "deleted_business_messages": {
                            "business_connection_id": "connection",
                            "chat": MESSAGE["chat"],
                            "message_ids": [10],
                        },
                    }
                ]
            )
        elif change == "revoke":
            gateway.store.ingest_updates(
                [
                    {
                        "update_id": 3,
                        "business_connection": {
                            **CONNECTION,
                            "is_enabled": False,
                        },
                    }
                ]
            )
        elif change == "edit":
            gateway.store.ingest_updates(
                [
                    {
                        "update_id": 3,
                        "edited_business_message": {
                            **MESSAGE,
                            "document": {**MESSAGE["document"], "file_id": "replacement"},
                        },
                    }
                ]
            )
        else:
            gateway._record_error(BotApiError(change, retry_after=12 if change == "rate-limited" else None))
        return {"file_path": "documents/file.txt"} if stage == "get_file" else opened

    getattr(bot, stage).side_effect = preflight
    if change in {"auth-required", "rate-limited"}:
        with pytest.raises(BotApiError) as error:
            await gateway.stream_message_media(chat_ref="200", message_id=10, media_id="10:0")
        assert error.value.reason == change
        telethon.stream_message_media.assert_not_awaited()
    else:
        result = await gateway.stream_message_media(chat_ref="200", message_id=10, media_id="10:0")
        assert result.provider == "telethon" and result.fallback_used
    if stage == "open_file":
        assert opened.closed
    else:
        bot.open_file.assert_not_awaited()


@pytest.mark.parametrize("method", ["get_chat", "resolve"])
@pytest.mark.parametrize("reason", ["auth-required", "rate-limited"])
@pytest.mark.parametrize("local_failure", [False, True])
async def test_metadata_obeys_concurrent_terminal_error(hybrid, method, reason, local_failure):
    gateway, bot, telethon = hybrid

    async def response(_):
        gateway._record_error(BotApiError(reason, retry_after=12 if reason == "rate-limited" else None))
        if local_failure:
            raise BotApiError("unavailable")
        return {"id": -100, "type": "supergroup", "title": "Group"}

    bot.get_chat.side_effect = response
    with pytest.raises(BotApiError) as error:
        await getattr(gateway, method)("-100")
    assert error.value.reason == reason
    getattr(telethon, method).assert_not_awaited()


@pytest.mark.parametrize("reason", ["unavailable", "auth-required", "rate-limited"])
async def test_midstream_failure_updates_health_without_fallback(hybrid, reason):
    gateway, bot, telethon = hybrid
    opened = ClosableChunks(BotApiError(reason, retry_after=10 if reason == "rate-limited" else None))
    bot.open_file.side_effect = lambda _: opened
    stream = await gateway.stream_message_media(chat_ref="200", message_id=10, media_id="10:0")
    with pytest.raises(BotApiError):
        await stream.chunks.__anext__()
    assert gateway.health_status().bot_api.last_error == reason
    assert not gateway.health_status().bot_api.healthy
    telethon.stream_message_media.assert_not_awaited()
    await stream.chunks.aclose()
    assert opened.closed


async def test_gateway_stream_closes_before_iteration(hybrid):
    gateway, bot, _ = hybrid
    opened = ClosableChunks()
    bot.open_file.side_effect = lambda _: opened
    stream = await gateway.stream_message_media(chat_ref="200", message_id=10, media_id="10:0")
    await stream.chunks.aclose()
    assert opened.closed
