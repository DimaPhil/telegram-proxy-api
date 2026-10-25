"""Translate validated Bot API payloads into the existing public DTOs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .bot_client import BotApiError
from .models import ChatDetail, ForwardInfo, MediaItem, MessageDetail, PeerRef


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class _Peer(_Payload):
    id: int
    type: Literal["private", "group", "supergroup", "channel"] | None = None
    title: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    is_bot: bool = False


class _Chat(_Peer):
    type: Literal["private", "group", "supergroup", "channel"]
    description: str | None = None
    bio: str | None = None


class _File(_Payload):
    file_id: str = Field(min_length=1)
    file_unique_id: str = Field(min_length=1)
    file_size: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    duration: float | None = Field(default=None, ge=0)
    file_name: str | None = None
    mime_type: str | None = None


class _Origin(_Payload):
    type: Literal["user", "hidden_user", "chat", "channel"]
    date: int
    sender_user: _Peer | None = None
    sender_user_name: str | None = None
    sender_chat: _Chat | None = None
    chat: _Chat | None = None
    message_id: int | None = None
    author_signature: str | None = None


class _Reply(_Payload):
    message_id: int = Field(gt=0)


class _Message(_Payload):
    message_id: int = Field(gt=0)
    chat: _Chat
    date: int
    edit_date: int | None = None
    text: str | None = None
    caption: str | None = None
    sender: _Peer | None = Field(default=None, alias="from")
    sender_chat: _Chat | None = None
    forward_origin: _Origin | None = None
    reply_to_message: _Reply | None = None
    media_group_id: str | None = None
    views: int | None = Field(default=None, ge=0)
    forwards: int | None = Field(default=None, ge=0)


_MEDIA_ORDER = (
    "document",
    "video",
    "animation",
    "audio",
    "voice",
    "video_note",
    "sticker",
)
_DEFAULT_MEDIA: dict[str, tuple[str, str]] = {
    "photo": (".jpg", "image/jpeg"),
    "video": (".mp4", "video/mp4"),
    "animation": (".mp4", "video/mp4"),
    "video_note": (".mp4", "video/mp4"),
    "voice": (".ogg", "audio/ogg"),
}


def _public_raw(value: dict[str, Any]) -> dict[str, Any]:
    """Remove internal connection/file locator fields, including nested messages."""

    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: clean(child) for key, child in item.items() if key not in {"business_connection_id", "file_path"}
            }
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item

    return {key: clean(item) for key, item in value.items() if key not in {"business_connection_id", "file_path"}}


def _name(peer: _Peer) -> str:
    return (
        peer.title or " ".join(part for part in (peer.first_name, peer.last_name) if part) or peer.username or "Unknown"
    )


def _peer(peer: _Peer | None) -> PeerRef | None:
    if peer is None:
        return None
    return PeerRef(
        id=str(peer.id),
        display_name=_name(peer),
        username=peer.username,
        type=peer.type or ("bot" if peer.is_bot else "user"),
    )


def _timestamp(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, tz=UTC) if value is not None else None


def serialize_bot_chat(raw: dict[str, Any]) -> ChatDetail:
    try:
        chat = _Chat.model_validate(raw)
        return ChatDetail(
            id=str(chat.id),
            title=_name(chat),
            username=chat.username,
            type=chat.type,
            about=chat.description or chat.bio,
            telegram_raw=_public_raw(raw),
        )
    except (ValidationError, ValueError, TypeError, OverflowError):
        raise BotApiError("invalid-response") from None


def _selected_media(raw: dict[str, Any]) -> tuple[str, dict[str, Any], _File] | None:
    for kind in _MEDIA_ORDER:
        if kind in raw:
            value = raw[kind]
            if not isinstance(value, dict):
                raise BotApiError("invalid-response")
            return kind, value, _File.model_validate(value)
    if "photo" in raw:
        photos = raw["photo"]
        if not isinstance(photos, list) or not photos:
            raise BotApiError("invalid-response")
        candidates: list[tuple[dict[str, Any], _File]] = []
        for value in photos:
            if not isinstance(value, dict):
                raise BotApiError("invalid-response")
            candidates.append((value, _File.model_validate(value)))
        value, file = max(
            candidates,
            key=lambda pair: (
                pair[1].file_size or 0,
                (pair[1].width or 0) * (pair[1].height or 0),
            ),
        )
        return "photo", value, file
    return None


def bot_media_object(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Return the selected file object for getFile, without adding public fields."""
    try:
        selected = _selected_media(raw)
        return selected[1] if selected else None
    except (ValidationError, ValueError, TypeError):
        raise BotApiError("invalid-response") from None


def _media(raw: dict[str, Any], message: _Message) -> list[MediaItem]:
    selected = _selected_media(raw)
    if selected is None:
        return []
    kind, value, file = selected
    extension, default_mime = _DEFAULT_MEDIA.get(kind, ("", "application/octet-stream"))
    # Telegram filenames are data, never paths or control characters in headers.
    filename = (file.file_name or "").replace("\\", "/").rsplit("/", 1)[-1]
    filename = "".join(char if ord(char) >= 32 and ord(char) != 127 else "_" for char in filename)
    if not filename.strip(". "):
        filename = f"message-{message.message_id}-{'gif' if kind == 'animation' else kind}{extension}"
    final_extension = PurePosixPath(filename).suffix or None
    media_id = f"{message.message_id}:0"
    return [
        MediaItem(
            media_id=media_id,
            message_id=str(message.message_id),
            grouped_id=message.media_group_id,
            kind="gif" if kind == "animation" else kind,
            mime_type=file.mime_type or default_mime,
            file_name=filename,
            extension=final_extension,
            size_bytes=file.file_size,
            width=file.width,
            height=file.height,
            duration_seconds=file.duration,
            access_path=f"/chats/{message.chat.id}/messages/{message.message_id}/media/{media_id}",
            telegram_raw=_public_raw(value),
        )
    ]


def serialize_bot_message(raw: dict[str, Any]) -> MessageDetail:
    try:
        message = _Message.model_validate(raw)
        origin = message.forward_origin
        forward = None
        if origin is not None:
            # Require the origin variant's identity instead of publishing a partial forward.
            identities = {
                "user": origin.sender_user,
                "hidden_user": origin.sender_user_name,
                "chat": origin.sender_chat,
                "channel": origin.chat,
            }
            if identities[origin.type] is None or (origin.type == "channel" and origin.message_id is None):
                raise BotApiError("invalid-response")
            forward = ForwardInfo(
                date=_timestamp(origin.date),
                from_peer=_peer(origin.sender_user or origin.sender_chat or origin.chat),
                from_name=origin.sender_user_name,
                channel_post=str(origin.message_id) if origin.message_id is not None else None,
                post_author=origin.author_signature,
                telegram_raw=_public_raw(raw["forward_origin"]),
            )
        media = _media(raw, message)
        return MessageDetail(
            id=str(message.message_id),
            chat_id=str(message.chat.id),
            date=_timestamp(message.date),
            edit_date=_timestamp(message.edit_date),
            text=message.text if message.text is not None else message.caption,
            sender=_peer(message.sender or message.sender_chat),
            is_forwarded=forward is not None,
            forward_info=forward,
            reply_to_message_id=str(message.reply_to_message.message_id) if message.reply_to_message else None,
            grouped_id=message.media_group_id,
            views=message.views,
            forwards=message.forwards,
            has_media=bool(media),
            media_count=len(media),
            media=media,
            telegram_raw=_public_raw(raw),
        )
    except (ValidationError, ValueError, TypeError, OverflowError, OSError):
        raise BotApiError("invalid-response") from None
