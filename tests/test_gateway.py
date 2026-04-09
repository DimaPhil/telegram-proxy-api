from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon.tl import types

from telegram_proxy_api.errors import (
    AuthenticationRequiredError,
    BadRequestError,
    ResourceNotFoundError,
    TelegramFloodWaitError,
)
from telegram_proxy_api.gateway import TelegramGateway

from tests.conftest import (
    make_chat,
    make_dialog,
    make_forward_header,
    make_media,
    make_message,
    make_user,
)


class FakeContactsResult:
    def __init__(self, users):
        self.users = users


class FakeClient:
    def __init__(self):
        self.connected = False
        self.authorized = True
        self.input_entities: list[tuple[object, object]] = []
        self.entities: dict[object, object] = {}
        self.dialogs = []
        self.messages_by_id: dict[int, object] = {}
        self.range_messages = []
        self.search_messages = []
        self.history_messages = []
        self.contacts_result = FakeContactsResult([])
        self.get_input_entity_calls = 0

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return self.authorized

    def is_connected(self):
        return self.connected

    async def get_me(self):
        return make_user(1, first_name="Owner")

    async def get_input_entity(self, lookup):
        self.get_input_entity_calls += 1
        for candidate, entity in self.input_entities:
            if candidate == lookup or repr(candidate) == repr(lookup):
                return entity
        raise ValueError("not cached")

    async def get_entity(self, lookup):
        if lookup not in self.entities:
            raise ValueError("not found")
        return self.entities[lookup]

    async def get_dialogs(self, limit=None):
        return self.dialogs[:limit]

    async def get_messages(self, entity=None, ids=None, search=None, limit=None, add_offset=0, **kwargs):
        if ids is not None:
            return self.messages_by_id.get(ids)
        if search is not None:
            return self.search_messages[add_offset : add_offset + (limit or len(self.search_messages))]
        if kwargs.get("min_id") is not None or kwargs.get("max_id") is not None:
            return self.range_messages[: limit or len(self.range_messages)]
        return self.history_messages[add_offset : add_offset + (limit or len(self.history_messages))]

    async def __call__(self, request):
        return self.contacts_result

    async def iter_download(self, media):
        payload = getattr(media, "payload", b"")
        yield payload[: max(1, len(payload) // 2)]
        yield payload[max(1, len(payload) // 2) :]


@pytest.fixture
def fake_client():
    return FakeClient()


@pytest.fixture
def gateway(settings, fake_client):
    service = TelegramGateway(settings, client_factory=lambda *args, **kwargs: fake_client)
    service._client = fake_client
    service._started = True
    return service


@pytest.mark.asyncio
async def test_start_requires_authorized_session(settings):
    fake_client = FakeClient()
    fake_client.authorized = False
    gateway = TelegramGateway(settings, client_factory=lambda *args, **kwargs: fake_client)

    with pytest.raises(AuthenticationRequiredError):
        await gateway.start()


@pytest.mark.asyncio
async def test_list_contacts_is_paginated(gateway, fake_client):
    fake_client.contacts_result = FakeContactsResult([make_user(1), make_user(2), make_user(3)])

    contacts, meta = await gateway.list_contacts(page=1, page_size=2)

    assert [contact.id for contact in contacts] == ["1", "2"]
    assert meta.total == 3


@pytest.mark.asyncio
async def test_list_chats_returns_page(gateway, fake_client):
    fake_client.dialogs = [
        make_dialog(make_chat(10, title="One")),
        make_dialog(make_chat(11, title="Two")),
        make_dialog(make_chat(12, title="Three")),
    ]

    chats, meta = await gateway.list_chats(page=2, page_size=1)

    assert [chat.id for chat in chats] == ["11"]
    assert meta.page == 2


@pytest.mark.asyncio
async def test_health_status_and_get_me(gateway):
    health = gateway.health_status()
    me = await gateway.get_me()

    assert health.telegram_connected is False
    assert me.display_name == "Owner User"


@pytest.mark.asyncio
async def test_get_chat(gateway, fake_client):
    entity = make_chat(200, title="Engineering")
    fake_client.input_entities.append((200, entity))

    chat = await gateway.get_chat("200")

    assert chat.id == "200"
    assert chat.title == "Engineering"


@pytest.mark.asyncio
async def test_resolve_uses_cache_after_first_lookup(gateway, fake_client):
    entity = make_chat(200, title="Engineering")
    fake_client.entities["@engineering"] = entity

    first = await gateway.resolve("@engineering")
    second = await gateway.resolve("@engineering")

    assert first.peer.id == "200"
    assert second.peer.id == "200"
    assert fake_client.get_input_entity_calls == 1


@pytest.mark.asyncio
async def test_resolve_marked_channel_id_uses_peer_channel_candidate(gateway, fake_client):
    entity = make_chat(3158415185, title="Channelish")
    fake_client.input_entities.append((types.PeerChannel(3158415185), entity))

    resolved = await gateway.get_chat("-1003158415185")

    assert resolved.title == "Channelish"
    assert fake_client.get_input_entity_calls == 1


@pytest.mark.asyncio
async def test_resolve_negative_raw_channel_id_falls_back_to_channel_candidate(gateway, fake_client):
    entity = make_chat(3158415185, title="Channelish")
    fake_client.input_entities.append((types.PeerChannel(3158415185), entity))

    messages, _ = await gateway.list_messages(chat_ref="-3158415185", page=1, page_size=1)

    assert messages == []
    assert fake_client.get_input_entity_calls == 1


@pytest.mark.asyncio
async def test_list_messages_is_paginated(gateway, fake_client):
    entity = make_chat(200)
    fake_client.input_entities.append((200, entity))
    fake_client.history_messages = [make_message(1), make_message(2), make_message(3)]

    messages, meta = await gateway.list_messages(chat_ref="200", page=1, page_size=2)

    assert [message.id for message in messages] == ["1", "2"]
    assert meta.page == 1


@pytest.mark.asyncio
async def test_get_message_context_returns_before_and_after(gateway, fake_client):
    entity = make_chat(200)
    fake_client.input_entities.append((200, entity))
    target = make_message(10)
    fake_client.messages_by_id[10] = target
    fake_client.range_messages = [make_message(9), make_message(11)]

    context = await gateway.get_message_context(chat_ref="200", message_id=10, context_size=1)

    assert context.message.id == "10"
    assert len(context.before) == 1
    assert len(context.after) == 1


@pytest.mark.asyncio
async def test_search_messages_returns_results(gateway, fake_client):
    fake_client.search_messages = [make_message(7), make_message(8)]

    messages, meta = await gateway.search_messages(query="hello", chat_ref=None, page=1, page_size=1)

    assert [message.id for message in messages] == ["7"]
    assert meta.page_size == 1


@pytest.mark.asyncio
async def test_get_message_media_handles_album(gateway, fake_client):
    entity = make_chat(200)
    fake_client.input_entities.append((200, entity))
    media1, file1 = make_media(payload=b"one", file_name="image.jpg")
    media2, file2 = make_media(payload=b"two", kind="video", file_name="clip.mp4", mime_type="video/mp4", ext=".mp4")
    anchor = make_message(10, media=media1, file=file1, grouped_id=77)
    sibling = make_message(11, media=media2, file=file2, grouped_id=77)
    fake_client.messages_by_id[10] = anchor
    fake_client.range_messages = [anchor, sibling]

    items, meta = await gateway.get_message_media(chat_ref="200", message_id=10, include_album=True)

    assert [item.message_id for item in items] == ["10", "11"]
    assert meta.include_album is True


@pytest.mark.asyncio
async def test_bundle_message_media_creates_zip(gateway, fake_client):
    import io
    import zipfile

    entity = make_chat(200)
    fake_client.input_entities.append((200, entity))
    media, file = make_media(payload=b"zip-payload", file_name="photo.jpg")
    message = make_message(10, media=media, file=file)
    fake_client.messages_by_id[10] = message

    bundle = await gateway.bundle_message_media(chat_ref="200", message_id=10, include_album=False)

    assert bundle.file_name.endswith(".zip")
    archive = zipfile.ZipFile(io.BytesIO(bundle.content))
    assert "manifest.json" in archive.namelist()
    assert archive.read("photo.jpg") == b"zip-payload"


@pytest.mark.asyncio
async def test_stream_message_media_returns_bytes(gateway, fake_client):
    entity = make_chat(200)
    fake_client.input_entities.append((200, entity))
    media, file = make_media(payload=b"payload", file_name="photo.jpg")
    message = make_message(10, media=media, file=file)
    fake_client.messages_by_id[10] = message

    stream = await gateway.stream_message_media(chat_ref="200", message_id=10, media_id="10:0")
    payload = b""
    async for chunk in stream.chunks:
        payload += chunk

    assert stream.file_name == "photo.jpg"
    assert payload == b"payload"


@pytest.mark.asyncio
async def test_forwarded_message_with_media_streams_normally(gateway, fake_client):
    entity = make_chat(200)
    fake_client.input_entities.append((200, entity))
    media, file = make_media(payload=b"forwarded-payload", file_name="forwarded.jpg")
    forward_header = make_forward_header(from_peer=types.PeerChannel(777), channel_post=11)
    message = make_message(10, media=media, file=file, fwd_from=forward_header)
    fake_client.messages_by_id[10] = message

    detail = await gateway.get_message(chat_ref="200", message_id=10)
    stream = await gateway.stream_message_media(chat_ref="200", message_id=10, media_id="10:0")
    payload = b""
    async for chunk in stream.chunks:
        payload += chunk

    assert detail.is_forwarded is True
    assert detail.forward_info is not None
    assert detail.forward_info.from_peer is not None
    assert detail.forward_info.from_peer.type == "channel"
    assert payload == b"forwarded-payload"


@pytest.mark.asyncio
async def test_get_message_raises_when_missing(gateway, fake_client):
    entity = make_chat(200)
    fake_client.input_entities.append((200, entity))

    with pytest.raises(ResourceNotFoundError):
        await gateway.get_message(chat_ref="200", message_id=999)


@pytest.mark.asyncio
async def test_invalid_page_size_raises(gateway):
    with pytest.raises(BadRequestError):
        await gateway.list_contacts(page=1, page_size=0)


@pytest.mark.asyncio
async def test_call_translates_flood_wait(gateway, monkeypatch):
    class FakeFloodWait(Exception):
        def __init__(self, seconds):
            self.seconds = seconds

    monkeypatch.setattr("telegram_proxy_api.gateway.FloodWaitError", FakeFloodWait)

    async def broken():
        raise FakeFloodWait(9)

    with pytest.raises(TelegramFloodWaitError):
        await gateway._call(broken())
