"""HTTP contract of chats_join, chats_latest and the updates_stream server-sent events."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from telegram_proxy_api.app import create_app
from telegram_proxy_api.updates import UpdateHub
from tests.test_app import FakeGateway, create_test_client, make_settings, update_event


def parse_sse(body: str) -> list[tuple[str, object]]:
    frames = []
    for block in body.strip().split("\n\n"):
        lines = block.split("\n")
        if lines[0].startswith(":"):
            frames.append(("comment", lines[0]))
            continue
        name = lines[0].removeprefix("event: ")
        frames.append((name, json.loads(lines[1].removeprefix("data: "))))
    return frames


def test_join_endpoint_returns_the_envelope():
    gateway = FakeGateway()
    with create_test_client(gateway=gateway) as client:
        response = client.post("/chats/-1001234567890/join")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "chat_id": "-1001234567890",
        "already_member": False,
        "muted": True,
        "archived": True,
    }
    assert response.json()["meta"]["provider"] == "telethon"
    assert gateway.telethon.calls["join_chat"] == {"chat_ref": "-1001234567890"}


def test_latest_endpoint_dedupes_in_order_and_is_not_captured_by_chat_lookup():
    gateway = FakeGateway()
    with create_test_client(gateway=gateway) as client:
        response = client.get("/chats/latest", params={"chat_ids": " -1002,-1001,,-1002, 42 "})

    assert response.status_code == 200
    assert [item["chat_id"] for item in response.json()["data"]] == ["-1002", "-1001", "42"]
    assert gateway.telethon.calls["latest_messages"] == {"chat_ids": ["-1002", "-1001", "42"]}


@pytest.mark.parametrize(
    "chat_ids",
    [",".join(str(-1000000000000 - index) for index in range(101)), "", " , ", "@channel", "-100abc", "0", "-0"],
)
def test_latest_endpoint_validates_ids_and_count(chat_ids):
    gateway = FakeGateway()
    with create_test_client(gateway=gateway) as client:
        response = client.get("/chats/latest", params={"chat_ids": chat_ids})

    assert response.status_code == 422
    assert "latest_messages" not in gateway.telethon.calls


def test_latest_endpoint_accepts_exactly_one_hundred_ids():
    ids = ",".join(str(-1000000000000 - index) for index in range(100))
    with create_test_client() as client:
        response = client.get("/chats/latest", params={"chat_ids": ids})

    assert response.status_code == 200
    assert len(response.json()["data"]) == 100


def test_stream_sends_ready_then_only_requested_chats():
    with create_test_client() as client:
        response = client.get("/updates/stream", params={"chat_ids": "100,300"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    frames = parse_sse(response.text)
    assert frames[0] == ("ready", {})
    assert frames[1:] == [
        ("message", {"chat_id": "100", "message_id": 1, "grouped_id": None, "date": "2026-01-01T00:00:00Z"})
    ]


def test_stream_requires_auth_validates_ids_and_reports_disabled_updates():
    with create_test_client(auth_enabled=True, bearer_token="secret") as client:
        unauthorized = client.get("/updates/stream", params={"chat_ids": "100"})
        too_many = client.get(
            "/updates/stream",
            params={"chat_ids": ",".join(str(index) for index in range(1, 502))},
            headers={"Authorization": "Bearer secret"},
        )
        allowed = client.get(
            "/updates/stream",
            params={"chat_ids": ",".join(str(index) for index in range(1, 501))},
            headers={"Authorization": "Bearer secret"},
        )
    gateway = FakeGateway()
    gateway.hub = None
    with create_test_client(gateway=gateway) as client:
        disabled = client.get("/updates/stream", params={"chat_ids": "100"})

    assert unauthorized.status_code == 401
    assert too_many.status_code == 422
    assert allowed.status_code == 200
    assert disabled.status_code == 503
    assert disabled.json()["type"] == "https://telegram-proxy/errors/telegram-capability-unavailable"


def test_openapi_declares_event_stream_and_new_operations():
    with create_test_client() as client:
        paths = client.get("/schema/openapi.json").json()["paths"]

    stream = paths["/updates/stream"]["get"]
    assert stream["operationId"] == "updates_stream"
    assert set(stream["responses"]["200"]["content"]) == {"text/event-stream"}
    assert paths["/chats/latest"]["get"]["operationId"] == "chats_latest"
    assert paths["/chats/{chat_id}/join"]["post"]["operationId"] == "chats_join"


@pytest.mark.asyncio
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_live_stream_pings_delivers_and_unsubscribes_on_disconnect(spec_version):
    hub = UpdateHub(queue_limit=10)
    settings = make_settings(TELEGRAM_UPDATES_PING_INTERVAL=0.05)
    app = create_app(settings=settings, gateway=FakeGateway(hub))
    app.state.settings = settings
    app.state.gateway = FakeGateway(hub)
    disconnect = asyncio.Event()
    body = bytearray()
    received = asyncio.Queue()

    async def receive():
        if not hasattr(receive, "sent"):
            receive.sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if disconnect.is_set():
            # ASGI 2.4 servers report a vanished client by failing the next send.
            raise OSError("client disconnected")
        if message["type"] == "http.response.body":
            body.extend(message.get("body", b""))
            await received.put(bytes(body))
        elif message["type"] == "http.response.start":
            assert message["status"] == 200

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/updates/stream",
        "raw_path": b"/updates/stream",
        "query_string": b"chat_ids=-1001",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
        "app": app,
    }
    task = asyncio.create_task(app(scope, receive, send))

    async def wait_for(fragment: bytes) -> None:
        while fragment not in bytes(body):
            await asyncio.wait_for(received.get(), 2)

    await wait_for(b"event: ready")
    assert hub.subscriber_count == 1
    await wait_for(b": ping")
    hub.publish(update_event("-1002", 1))
    hub.publish(update_event("-1001", 2))
    await wait_for(b'"message_id":2')
    assert b'"message_id":1' not in body

    disconnect.set()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(task, 2)
    assert task.done()
    assert hub.subscriber_count == 0
