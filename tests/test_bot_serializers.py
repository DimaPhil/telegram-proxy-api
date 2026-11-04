from datetime import UTC, datetime
from typing import Any

import pytest

from telegram_proxy_api.bot_client import BotApiError
from telegram_proxy_api.bot_serializers import (
    bot_media_object,
    serialize_bot_chat,
    serialize_bot_message,
)


def message(**extra: Any) -> dict[str, Any]:
    return {
        "message_id": 42,
        "date": 1704067200,
        "chat": {"id": 123, "type": "private", "first_name": "Alice"},
        "business_connection_id": "private-connection",
        **extra,
    }


def file(file_id: str = "file", **extra: Any) -> dict[str, Any]:
    return {"file_id": file_id, "file_unique_id": f"unique-{file_id}", **extra}


def test_chat_mapping_preserves_existing_defaults_and_large_ids() -> None:
    chat = serialize_bot_chat(
        {
            "id": -1001234567890,
            "type": "supergroup",
            "title": "Group",
            "description": "About",
        }
    )
    assert chat.id == "-1001234567890"
    assert chat.title == "Group"
    assert chat.about == "About"
    assert chat.participant_count is None
    assert chat.unread_count == 0
    assert chat.archived is False
    assert (
        serialize_bot_chat(
            {
                "id": 1,
                "type": "private",
                "first_name": "A",
                "last_name": "B",
                "bio": "Bio",
            }
        ).title
        == "A B"
    )
    assert serialize_bot_chat({"id": 1, "type": "private", "username": "user"}).title == "user"
    assert serialize_bot_chat({"id": 1, "type": "private"}).title == "Unknown"


def test_text_sender_reply_and_recursive_sanitization_without_mutating_cache() -> None:
    raw = message(
        text="Hello",
        edit_date=1704067260,
        **{"from": {"id": 456, "first_name": "Bob", "username": "bob"}},
        reply_to_message={
            "message_id": 40,
            "business_connection_id": "nested",
            "photo": [{"file_path": "secret-path"}],
        },
    )
    result = serialize_bot_message(raw)
    assert result.id == "42"
    assert result.chat_id == "123"
    assert result.text == "Hello"
    assert result.date == datetime(2024, 1, 1, tzinfo=UTC)
    assert result.edit_date == datetime(2024, 1, 1, 0, 1, tzinfo=UTC)
    assert result.reply_to_message_id == "40"
    assert result.sender is not None and result.sender.id == "456"
    assert result.sender.type == "user"
    assert "business_connection_id" not in str(result.telegram_raw)
    assert "file_path" not in str(result.telegram_raw)
    assert raw["business_connection_id"] == "private-connection"
    assert raw["reply_to_message"]["business_connection_id"] == "nested"


def test_document_caption_album_and_safe_filename() -> None:
    raw = message(
        caption="caption",
        media_group_id="album-7",
        document=file(
            file_size=123,
            file_name="../../folder\\report\n.pdf",
            mime_type="application/pdf",
        ),
    )
    result = serialize_bot_message(raw)
    assert result.text == "caption"
    assert result.grouped_id == "album-7"
    assert result.has_media and result.media_count == 1
    media = result.media[0]
    assert media.file_name == "report_.pdf"
    assert media.media_id == "42:0"
    assert media.message_id == "42"
    assert media.access_path == "/chats/123/messages/42/media/42:0"
    assert media.size_bytes == 123
    assert media.extension == ".pdf"
    assert bot_media_object(raw) == raw["document"]


def test_photo_selection_uses_size_then_dimensions() -> None:
    raw = message(
        photo=[
            file("small", file_size=10, width=1000, height=1000),
            file("large", file_size=20, width=200, height=200),
            file("largest", file_size=20, width=300, height=300),
        ]
    )
    selected = bot_media_object(raw)
    assert selected is not None and selected["file_id"] == "largest"
    photo = serialize_bot_message(raw).media[0]
    assert photo.width == 300
    assert photo.file_name == "message-42-photo.jpg"
    assert photo.mime_type == "image/jpeg"


@pytest.mark.parametrize(
    "kind",
    ["document", "video", "animation", "audio", "voice", "video_note", "sticker"],
)
def test_media_types_and_priority(kind: str) -> None:
    raw = message(**{kind: file(duration=3, width=10, height=20)}, photo=[file("photo")])
    media = serialize_bot_message(raw).media[0]
    assert media.kind == ("gif" if kind == "animation" else kind)
    assert media.duration_seconds == 3
    assert bot_media_object(raw) == raw[kind]
    if kind != "document":
        raw["document"] = file("document")
        assert serialize_bot_message(raw).media[0].kind == "document"


@pytest.mark.parametrize(
    "origin,peer_id,name,post",
    [
        (
            {"type": "user", "sender_user": {"id": 99, "first_name": "Author"}},
            "99",
            None,
            None,
        ),
        ({"type": "hidden_user", "sender_user_name": "Hidden"}, None, "Hidden", None),
        (
            {
                "type": "chat",
                "sender_chat": {"id": -5, "type": "group", "title": "G"},
                "author_signature": "A",
            },
            "-5",
            None,
            None,
        ),
        (
            {
                "type": "channel",
                "chat": {"id": -1005, "type": "channel", "title": "C"},
                "message_id": 6,
            },
            "-1005",
            None,
            "6",
        ),
    ],
)
def test_forward_origins(origin: dict[str, Any], peer_id: str | None, name: str | None, post: str | None) -> None:
    result = serialize_bot_message(message(forward_origin={"date": 1704067200, **origin}))
    assert result.is_forwarded
    forward = result.forward_info
    assert forward is not None
    assert (forward.from_peer.id if forward.from_peer else None) == peer_id
    assert forward.from_name == name
    assert forward.channel_post == post
    assert forward.telegram_raw["type"] == origin["type"]


def test_sender_chat_and_no_media() -> None:
    result = serialize_bot_message(message(sender_chat={"id": -1, "type": "channel", "title": "Channel"}))
    assert result.sender is not None and result.sender.type == "channel"
    assert result.has_media is False
    assert result.media == []
    assert bot_media_object(message()) is None


@pytest.mark.parametrize(
    "raw",
    [
        {},
        message(message_id=True),
        message(chat={"id": "123", "type": "private"}),
        message(date=10**100),
        message(document={"file_id": "secret-provider-content"}),
        message(photo=[]),
        message(photo=["invalid"]),
        message(document="invalid"),
        message(forward_origin={"type": "user", "date": 1}),
    ],
)
def test_invalid_messages_raise_safe_provider_error(raw: dict[str, Any]) -> None:
    with pytest.raises(BotApiError) as captured:
        serialize_bot_message(raw)
    assert captured.value.reason == "invalid-response"
    assert captured.value.fallback_allowed
    assert "secret-provider-content" not in str(captured.value)


def test_invalid_chat_and_media_raise_typed_provider_errors() -> None:
    with pytest.raises(BotApiError):
        serialize_bot_chat({"id": True, "type": "private"})
    with pytest.raises(BotApiError):
        bot_media_object(message(document={"file_id": "id"}))
