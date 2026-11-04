from __future__ import annotations

import logging

import httpx
import pytest

from telegram_proxy_api.bot_client import ALLOWED_UPDATES, BotApiClient, BotApiError

TOKEN = "123:private-token"
BASE = f"https://api.telegram.org/bot{TOKEN}"


@pytest.fixture
async def bot():
    client = BotApiClient(TOKEN)
    yield client
    await client.close()


async def test_start_and_safe_request_logging(bot, respx_mock, caplog):
    respx_mock.post(f"{BASE}/getMe").respond(json={"ok": True, "result": {"id": 1, "is_bot": True}})
    caplog.set_level(logging.INFO, logger="httpx")
    assert (await bot.start())["id"] == 1
    assert TOKEN not in caplog.text
    assert "[REDACTED]" in caplog.text


@pytest.mark.parametrize(
    "status,payload,reason,fallback",
    [
        (200, {"ok": False, "error_code": 401}, "auth-required", False),
        (401, {}, "auth-required", False),
        (503, {}, "unavailable", True),
        (200, {"ok": False, "error_code": 429, "parameters": {"retry_after": 17}}, "rate-limited", False),
        (429, {"ok": False, "parameters": {"retry_after": "wrong"}}, "rate-limited", False),
        (200, {"ok": False, "error_code": 403}, "not-found", True),
        (200, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}, "not-found", True),
        (200, {"ok": False, "error_code": 400, "description": "unrelated bad request"}, "bad-request", False),
        (200, {"ok": "true", "result": {}}, "invalid-response", True),
        (200, {"ok": True}, "invalid-response", True),
        (200, {"ok": True, "result": []}, "invalid-response", True),
        (200, {"ok": False, "parameters": []}, "invalid-response", True),
        (200, {"ok": False, "error_code": 500}, "unavailable", True),
    ],
)
async def test_envelopes(bot, respx_mock, status, payload, reason, fallback):
    respx_mock.post(f"{BASE}/getChat").respond(status, json=payload)
    with pytest.raises(BotApiError) as caught:
        await bot.get_chat("-123")
    assert caught.value.reason == reason
    assert caught.value.fallback_allowed is fallback
    assert TOKEN not in str(caught.value)
    if reason == "rate-limited":
        assert caught.value.headers["Retry-After"] in {"17", "30"}


async def test_invalid_json_and_timeout_are_sanitized(bot, respx_mock):
    route = respx_mock.post(f"{BASE}/getMe").respond(text="not json")
    with pytest.raises(BotApiError, match="invalid"):
        await bot.get_me()
    route.mock(side_effect=httpx.ReadTimeout(f"secret URL {BASE}"))
    with pytest.raises(BotApiError) as caught:
        await bot.get_me()
    assert caught.value.reason == "unavailable"
    assert TOKEN not in str(caught.value)
    assert caught.value.__suppress_context__


async def test_migration_retried_once_and_remembered(bot, respx_mock):
    route = respx_mock.post(f"{BASE}/getChat").mock(
        side_effect=[
            httpx.Response(400, json={"ok": False, "error_code": 400, "parameters": {"migrate_to_chat_id": -100123}}),
            httpx.Response(200, json={"ok": True, "result": {"id": -100123, "type": "supergroup"}}),
            httpx.Response(200, json={"ok": True, "result": {"id": -100123, "type": "supergroup"}}),
        ]
    )
    assert (await bot.get_chat("-123"))["id"] == -100123
    await bot.get_chat("-123")
    assert b'"chat_id":"-100123"' in route.calls[2].request.content


async def test_migration_cannot_loop(bot, respx_mock):
    route = respx_mock.post(f"{BASE}/getChat").respond(
        400, json={"ok": False, "error_code": 400, "parameters": {"migrate_to_chat_id": -100123}}
    )
    with pytest.raises(BotApiError):
        await bot.get_chat("-123")
    assert route.call_count == 2


async def test_poll_payload_and_timeout(bot, respx_mock):
    import json

    route = respx_mock.post(f"{BASE}/getUpdates").respond(json={"ok": True, "result": [{"update_id": 40}]})
    assert await bot.get_updates(40, 30) == [{"update_id": 40}]
    assert json.loads(route.calls.last.request.content) == {
        "offset": 40,
        "timeout": 30,
        "limit": 100,
        "allowed_updates": ALLOWED_UPDATES,
    }
    assert route.calls.last.request.extensions["timeout"]["read"] == 35
    route.respond(json={"ok": True, "result": [{"update_id": "40"}]})
    with pytest.raises(BotApiError):
        await bot.get_updates(None, 0)


async def test_connection_webhook_and_file_validation(bot, respx_mock):
    for method, result, call in [
        ("getWebhookInfo", {"url": ""}, lambda: bot.get_webhook_info()),
        (
            "getBusinessConnection",
            {"id": "conn", "user": {"id": 123}, "is_enabled": True},
            lambda: bot.get_business_connection("conn"),
        ),
        ("getFile", {"file_path": "photos/test.jpg"}, lambda: bot.get_file("file")),
    ]:
        route = respx_mock.post(f"{BASE}/{method}").respond(json={"ok": True, "result": result})
        assert await call() == result
        route.respond(json={"ok": True, "result": {}})
        with pytest.raises(BotApiError):
            await call()


async def test_file_too_large_only_for_get_file(bot, respx_mock):
    respx_mock.post(f"{BASE}/getFile").respond(
        400, json={"ok": False, "error_code": 400, "description": "Bad Request: file is too big"}
    )
    with pytest.raises(BotApiError) as caught:
        await bot.get_file("file")
    assert caught.value.reason == "file-too-large"


async def test_file_preflight_and_stream(bot, respx_mock):
    route = respx_mock.get(f"https://api.telegram.org/file/bot{TOKEN}/photos/a.jpg").respond(content=b"photo")
    chunks = await bot.open_file("photos/a.jpg")
    assert route.called
    assert b"".join([chunk async for chunk in chunks]) == b"photo"
    assert b"".join([chunk async for chunk in bot.stream_file("photos/a.jpg")]) == b"photo"
    route.respond(401)
    with pytest.raises(BotApiError) as caught:
        await bot.open_file("photos/a.jpg")
    assert caught.value.reason == "auth-required"
    route.respond(429, headers={"Retry-After": "8"})
    with pytest.raises(BotApiError) as caught:
        await bot.open_file("photos/a.jpg")
    assert caught.value.retry_after == 8
    route.respond(302, headers={"Location": "https://untrusted.test/file"})
    with pytest.raises(BotApiError):
        await bot.open_file("photos/a.jpg")


@pytest.mark.parametrize(
    "path", ["../secret", "/absolute", "https://evil.test/a", "a?token=x", "a%2fb", "a\\b", "a\n.jpg", "a\x7f.jpg", ""]
)
async def test_file_path_rejects_unsafe_values(bot, path):
    with pytest.raises(BotApiError):
        await bot.open_file(path)


@pytest.mark.parametrize("url", ["file:///tmp/bot", "https://user:pass@example.test", "https://example.test?a=1"])
def test_base_url_validation(url):
    with pytest.raises(ValueError):
        BotApiClient(TOKEN, base_url=url)


@pytest.mark.parametrize("token", ["", "123:token\t", "123:token\x00", "123:token\x7f"])
def test_token_rejects_controls(token):
    with pytest.raises(ValueError, match="token"):
        BotApiClient(token)


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, *, fail=False):
        self.closed = False
        self.close_count = 0
        self.fail = fail

    async def __aiter__(self):
        yield b"first"
        if self.fail:
            raise httpx.ReadError(f"request failed at {BASE}")
        yield b"second"

    async def aclose(self):
        self.closed = True
        self.close_count += 1


@pytest.mark.parametrize("mode", ["unstarted", "normal", "error", "partial"])
async def test_preflighted_stream_closes_even_without_starting(mode):
    upstream = TrackingStream(fail=mode == "error")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=upstream)))
    bot = BotApiClient(TOKEN, client=client)
    chunks = await bot.open_file("photos/a.jpg")
    assert not upstream.closed
    if mode == "normal":
        assert [chunk async for chunk in chunks] == [b"first", b"second"]
        assert upstream.closed
    elif mode == "error":
        with pytest.raises(BotApiError) as caught:
            _ = [chunk async for chunk in chunks]
        assert TOKEN not in str(caught.value)
        assert upstream.closed
    elif mode == "partial":
        assert await anext(chunks) == b"first"
        assert not upstream.closed
    await chunks.aclose()
    await chunks.aclose()
    assert upstream.closed
    assert upstream.close_count == 1
    with pytest.raises(StopAsyncIteration):
        await anext(chunks)
    await bot.close()
