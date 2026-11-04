from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon import utils
from telethon.tl import types
from telethon.tl.functions.messages import GetCommonChatsRequest
from telethon.tl.tlobject import TLObject

from telegram_proxy_api import gateway as gateway_module
from telegram_proxy_api.errors import (
    AuthenticationRequiredError,
    BadRequestError,
    ResourceNotFoundError,
    TelegramFloodWaitError,
)
from telegram_proxy_api.gateway import MessageFilter
from telegram_proxy_api.gateway import TelethonGateway as TelegramGateway
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
        self.full_entities: dict[int, object] = {}
        self.full_entity_lookups: list[object] = []
        self.iter_calls: list[dict[str, object]] = []
        self.yielded = 0
        self.common_chats = []
        self.common_chats_max_ids: list[int] = []

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
        if isinstance(lookup, TLObject):
            self.full_entity_lookups.append(lookup)
            return self.full_entities[utils.get_peer_id(lookup)]
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

    async def iter_messages(
        self,
        entity=None,
        limit=None,
        *,
        offset_date=None,
        offset_id=0,
        add_offset=0,
        search=None,
        from_user=None,
        wait_time=None,
    ):
        self.iter_calls.append(
            {
                "limit": limit,
                "offset_date": offset_date,
                "offset_id": offset_id,
                "search": search,
                "from_user": from_user,
            }
        )
        source = self.search_messages if search is not None or entity is None else self.history_messages
        sender_id = utils.get_peer_id(from_user) if from_user is not None else None
        selected = [
            message
            for message in source
            if (not offset_id or message.id < offset_id)
            and (offset_date is None or message.date < offset_date)
            and (sender_id is None or message.sender_id == sender_id)
        ][add_offset:]
        for message in selected if limit is None else selected[:limit]:
            self.yielded += 1
            yield message

    async def __call__(self, request):
        if isinstance(request, GetCommonChatsRequest):
            self.common_chats_max_ids.append(request.max_id)
            matching = [chat for chat in self.common_chats if not request.max_id or chat.id < request.max_id]
            return SimpleNamespace(chats=matching[: request.limit])
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
    assert fake_client.full_entity_lookups == []


@pytest.mark.asyncio
async def test_resolve_of_session_cached_user_reports_name_username_and_user_type(gateway, fake_client):
    cached = types.InputPeerUser(user_id=278851442, access_hash=7)
    fake_client.input_entities.append(("@norbusha", cached))
    fake_client.full_entities[278851442] = types.User(
        id=278851442, access_hash=7, first_name="Norbert", last_name="Fedaka", username="Norbusha"
    )

    result = await gateway.resolve("@norbusha")

    assert result.peer.model_dump() == {
        "id": "278851442",
        "display_name": "Norbert Fedaka",
        "username": "Norbusha",
        "type": "user",
    }
    assert fake_client.full_entity_lookups == [cached]


@pytest.mark.asyncio
async def test_get_chat_of_session_cached_supergroup_reports_title_and_supergroup_type(gateway, fake_client):
    cached = types.InputPeerChannel(channel_id=1625820829, access_hash=9)
    fake_client.input_entities.append((types.PeerChannel(1625820829), cached))
    fake_client.full_entities[-1001625820829] = types.Channel(
        id=1625820829,
        title="Builders",
        photo=types.ChatPhotoEmpty(),
        date=datetime(2024, 1, 1, tzinfo=UTC),
        megagroup=True,
        access_hash=9,
        participants_count=42,
    )

    chat = await gateway.get_chat("-1001625820829")

    assert (chat.id, chat.title, chat.type, chat.participant_count) == ("-1001625820829", "Builders", "supergroup", 42)


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


AUTHOR = types.InputPeerUser(user_id=300, access_hash=1)


def dated_message(message_id: int, *, day: int, sender_id: int):
    message = make_message(message_id, sender_id=sender_id)
    message.date = datetime(2026, 5, day, tzinfo=UTC)
    return message


@pytest.mark.asyncio
async def test_group_sender_filter_runs_server_side_and_pages_by_cursor_until_since(gateway, fake_client):
    fake_client.input_entities.extend([(200, make_chat(200)), (300, AUTHOR)])
    fake_client.history_messages = [
        dated_message(9, day=9, sender_id=300),
        dated_message(8, day=8, sender_id=400),
        dated_message(7, day=7, sender_id=300),
        dated_message(6, day=6, sender_id=300),
        dated_message(5, day=1, sender_id=300),
    ]
    filters = MessageFilter(from_user="300", since=datetime(2026, 5, 5))

    first, first_meta = await gateway.list_messages(chat_ref="200", page=1, page_size=2, filters=filters)
    second, second_meta = await gateway.list_messages(
        chat_ref="200",
        page=1,
        page_size=2,
        filters=MessageFilter(from_user="300", since=datetime(2026, 5, 5), before_id=first_meta.next_before_id),
    )

    assert [message.id for message in first] == ["9", "7"]
    assert first_meta.next_before_id == 7
    assert [message.id for message in second] == ["6"]
    assert second_meta.next_before_id is None
    assert fake_client.iter_calls[0]["from_user"] is AUTHOR
    assert fake_client.iter_calls[1]["offset_id"] == 7


@pytest.mark.asyncio
async def test_private_sender_filter_stops_at_since_instead_of_scanning_older_history(gateway, fake_client):
    fake_client.input_entities.append((300, AUTHOR))
    fake_client.history_messages = [
        dated_message(5, day=20, sender_id=1),
        dated_message(4, day=19, sender_id=300),
        dated_message(3, day=2, sender_id=1),
        dated_message(2, day=1, sender_id=1),
        dated_message(1, day=1, sender_id=1),
    ]

    messages, meta = await gateway.list_messages(
        chat_ref="300",
        page=1,
        page_size=10,
        filters=MessageFilter(from_user="300", since=datetime(2026, 5, 10, tzinfo=UTC)),
    )

    assert [message.id for message in messages] == ["4"]
    assert meta.total == 1
    assert fake_client.iter_calls[0]["from_user"] is None
    assert fake_client.yielded == 3


@pytest.mark.asyncio
async def test_search_passes_query_and_until_bound_to_telegram(gateway, fake_client):
    fake_client.input_entities.append((200, make_chat(200)))
    fake_client.search_messages = [dated_message(3, day=3, sender_id=300)]
    until = datetime(2026, 5, 4, tzinfo=UTC)

    messages, meta = await gateway.search_messages(
        query="plans", chat_ref="200", page=1, page_size=5, filters=MessageFilter(until=until)
    )

    assert [message.id for message in messages] == ["3"]
    assert fake_client.iter_calls[0]["search"] == "plans"
    assert fake_client.iter_calls[0]["offset_date"] == until


@pytest.mark.asyncio
async def test_include_raw_false_omits_message_and_forward_raw_payloads(gateway, fake_client):
    fake_client.input_entities.append((200, make_chat(200)))
    fake_client.history_messages = [make_message(1, fwd_from=make_forward_header(from_name="Origin"))]

    messages, _ = await gateway.list_messages(chat_ref="200", page=1, page_size=5, include_raw=False)

    assert messages[0].telegram_raw == {}
    assert messages[0].forward_info is not None
    assert messages[0].forward_info.telegram_raw == {}


@pytest.mark.asyncio
async def test_filtered_queries_reject_offset_pages_and_cross_chat_sender_search(gateway, fake_client):
    fake_client.input_entities.append((200, make_chat(200)))

    with pytest.raises(BadRequestError, match="page must be 1"):
        await gateway.list_messages(chat_ref="200", page=2, page_size=5, filters=MessageFilter(from_user="300"))
    with pytest.raises(BadRequestError, match="require chat_id"):
        await gateway.search_messages(query=None, chat_ref=None, page=1, page_size=5, filters=MessageFilter("300"))
    with pytest.raises(BadRequestError, match="query or from_user"):
        await gateway.search_messages(query="  ", chat_ref="200", page=1, page_size=5)
    with pytest.raises(BadRequestError, match="since must be earlier"):
        MessageFilter(since=datetime(2026, 5, 2), until=datetime(2026, 5, 1))


@pytest.mark.asyncio
async def test_common_chats_follow_max_id_batches(gateway, fake_client, monkeypatch):
    monkeypatch.setattr(gateway_module, "_COMMON_CHATS_BATCH", 2)
    fake_client.input_entities.append((300, AUTHOR))
    fake_client.common_chats = [make_chat(30, title="A"), make_chat(20, title="B"), make_chat(10, title="C")]

    chats, meta = await gateway.list_common_chats(user_ref="300", page=1, page_size=25)

    assert [chat.title for chat in chats] == ["A", "B", "C"]
    assert meta.total == 3
    assert fake_client.common_chats_max_ids == [0, 20]


@pytest.mark.asyncio
async def test_common_chats_reject_non_user_reference(gateway, fake_client):
    fake_client.input_entities.append((500, types.InputPeerChannel(channel_id=500, access_hash=1)))

    with pytest.raises(BadRequestError, match="must reference a user"):
        await gateway.list_common_chats(user_ref="500", page=1, page_size=25)


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
