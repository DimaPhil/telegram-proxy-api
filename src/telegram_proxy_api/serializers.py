from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from telethon import utils
from telethon.tl import types

from .models import (
    ChatDetail,
    ChatSummary,
    ContactSummary,
    ForwardInfo,
    MediaItem,
    MessageDetail,
    MessageSummary,
    PeerRef,
    ResolveResult,
)


@dataclass(slots=True)
class MediaDescriptor:
    item: MediaItem
    message: Any
    file_name: str
    content_type: str
    size_bytes: int | None


def safe_telegram_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "to_dict"):
        raw = value.to_dict()
        if isinstance(raw, dict):
            return _sanitize_json_value(raw)
    if isinstance(value, dict):
        return _sanitize_json_value(value)
    return {}


def _sanitize_json_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, bytes):
        return {
            "__type__": "bytes",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value
    if isinstance(value, dict):
        return {str(key): _sanitize_json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_sanitize_json_value(item) for item in value]
    if hasattr(value, "to_dict"):
        raw = value.to_dict()
        if isinstance(raw, dict):
            return _sanitize_json_value(raw)
    return str(value)


def peer_id_as_str(peer: Any) -> str:
    if peer is None:
        return ""
    try:
        return str(utils.get_peer_id(peer))
    except Exception:
        if hasattr(peer, "id"):
            return str(peer.id)
    return str(peer)


def infer_peer_type(entity: Any) -> str:
    if isinstance(entity, types.PeerChannel):
        return "channel"
    if isinstance(entity, types.PeerChat):
        return "chat"
    if isinstance(entity, types.PeerUser):
        return "user"
    if getattr(entity, "broadcast", False):
        return "channel"
    if getattr(entity, "megagroup", False):
        return "supergroup"
    if getattr(entity, "bot", False):
        return "bot"
    if getattr(entity, "first_name", None) or getattr(entity, "last_name", None):
        return "user"
    return "chat"


def display_name(entity: Any) -> str:
    if entity is None:
        return "Unknown"
    if isinstance(entity, (types.PeerUser, types.PeerChat, types.PeerChannel)):
        return None
    full_name = " ".join(
        part for part in [getattr(entity, "first_name", None), getattr(entity, "last_name", None)] if part
    ).strip()
    if full_name:
        return full_name
    try:
        name = utils.get_display_name(entity)
        if name:
            return name
    except Exception:
        pass

    return (
        getattr(entity, "title", None)
        or getattr(entity, "first_name", None)
        or getattr(entity, "username", None)
        or "Unknown"
    )


def serialize_peer_ref(entity: Any) -> PeerRef | None:
    if entity is None:
        return None
    return PeerRef(
        id=peer_id_as_str(entity),
        display_name=display_name(entity),
        username=getattr(entity, "username", None),
        type=infer_peer_type(entity),
    )


def serialize_forward_info(forward_header: Any) -> ForwardInfo | None:
    if forward_header is None:
        return None
    return ForwardInfo(
        date=getattr(forward_header, "date", None),
        imported=bool(getattr(forward_header, "imported", False)),
        saved_out=bool(getattr(forward_header, "saved_out", False)),
        from_peer=serialize_peer_ref(getattr(forward_header, "from_id", None)),
        from_name=getattr(forward_header, "from_name", None),
        channel_post=str(getattr(forward_header, "channel_post", None))
        if getattr(forward_header, "channel_post", None) is not None
        else None,
        post_author=getattr(forward_header, "post_author", None),
        saved_from_peer=serialize_peer_ref(getattr(forward_header, "saved_from_peer", None))
        or serialize_peer_ref(getattr(forward_header, "saved_from_id", None)),
        saved_from_message_id=str(getattr(forward_header, "saved_from_msg_id", None))
        if getattr(forward_header, "saved_from_msg_id", None) is not None
        else None,
        saved_from_name=getattr(forward_header, "saved_from_name", None),
        saved_date=getattr(forward_header, "saved_date", None),
        psa_type=getattr(forward_header, "psa_type", None),
        telegram_raw=safe_telegram_dict(forward_header),
    )


def serialize_chat_summary(dialog: Any) -> ChatSummary:
    entity = getattr(dialog, "entity", dialog)
    return ChatSummary(
        id=peer_id_as_str(entity),
        title=display_name(entity),
        username=getattr(entity, "username", None),
        type=infer_peer_type(entity),
        unread_count=getattr(dialog, "unread_count", 0) or 0,
        archived=(getattr(dialog, "folder_id", None) == 1),
        last_message_at=getattr(getattr(dialog, "message", None), "date", None),
        telegram_raw=safe_telegram_dict(entity),
    )


def serialize_chat_detail(entity: Any, *, about: str | None = None) -> ChatDetail:
    return ChatDetail(
        id=peer_id_as_str(entity),
        title=display_name(entity),
        username=getattr(entity, "username", None),
        type=infer_peer_type(entity),
        unread_count=0,
        archived=False,
        last_message_at=None,
        participant_count=getattr(entity, "participants_count", None),
        about=about,
        telegram_raw=safe_telegram_dict(entity),
    )


def serialize_contact(entity: Any) -> ContactSummary:
    return ContactSummary(
        id=peer_id_as_str(entity),
        display_name=display_name(entity),
        username=getattr(entity, "username", None),
        phone=getattr(entity, "phone", None),
        mutual_contact=getattr(entity, "mutual_contact", None),
        bot=bool(getattr(entity, "bot", False)),
        telegram_raw=safe_telegram_dict(entity),
    )


def infer_media_kind(message: Any) -> str:
    if getattr(message, "gif", False):
        return "gif"
    if getattr(message, "video", False):
        return "video"
    if getattr(message, "photo", False):
        return "photo"
    if getattr(message, "voice", False):
        return "voice"
    if getattr(message, "audio", False):
        return "audio"
    if getattr(message, "sticker", False):
        return "sticker"
    if getattr(message, "document", None):
        return "document"
    return "media"


def default_file_name(message: Any, kind: str, extension: str | None) -> str:
    suffix = extension or ""
    return f"message-{message.id}-{kind}{suffix}"


def serialize_media_descriptor(
    *,
    chat_id: str,
    anchor_message_id: int,
    message: Any,
    ordinal: int = 0,
) -> MediaDescriptor:
    file = getattr(message, "file", None)
    kind = infer_media_kind(message)
    extension = getattr(file, "ext", None) if file else None
    file_name = getattr(file, "name", None) if file else None
    final_name = file_name or default_file_name(message, kind, extension)
    content_type = getattr(file, "mime_type", None) if file else None
    if not content_type:
        content_type = "application/octet-stream"

    media_id = f"{message.id}:{ordinal}"
    item = MediaItem(
        media_id=media_id,
        message_id=str(message.id),
        grouped_id=str(message.grouped_id) if getattr(message, "grouped_id", None) else None,
        kind=kind,
        mime_type=getattr(file, "mime_type", None) if file else None,
        file_name=final_name,
        extension=extension,
        size_bytes=getattr(file, "size", None) if file else None,
        width=getattr(file, "width", None) if file else None,
        height=getattr(file, "height", None) if file else None,
        duration_seconds=getattr(file, "duration", None) if file else None,
        access_path=f"/chats/{chat_id}/messages/{anchor_message_id}/media/{media_id}",
        telegram_raw=safe_telegram_dict(getattr(message, "media", None)),
    )
    return MediaDescriptor(
        item=item,
        message=message,
        file_name=final_name,
        content_type=content_type,
        size_bytes=getattr(file, "size", None) if file else None,
    )


def serialize_message_summary(message: Any, *, chat_id: str) -> MessageSummary:
    media_count = 1 if getattr(message, "media", None) else 0
    forward_info = serialize_forward_info(getattr(message, "fwd_from", None))
    return MessageSummary(
        id=str(message.id),
        chat_id=chat_id,
        date=getattr(message, "date", None),
        edit_date=getattr(message, "edit_date", None),
        text=getattr(message, "message", None),
        sender=serialize_peer_ref(getattr(message, "sender", None))
        or (
            PeerRef(id=str(getattr(message, "sender_id", "")))
            if getattr(message, "sender_id", None) is not None
            else None
        ),
        is_forwarded=forward_info is not None,
        forward_info=forward_info,
        reply_to_message_id=str(message.reply_to.reply_to_msg_id)
        if getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
        else None,
        grouped_id=str(message.grouped_id) if getattr(message, "grouped_id", None) else None,
        views=getattr(message, "views", None),
        forwards=getattr(message, "forwards", None),
        has_media=bool(getattr(message, "media", None)),
        media_count=media_count,
        telegram_raw=safe_telegram_dict(message),
    )


def serialize_message_detail(
    message: Any,
    *,
    chat_id: str,
    media_descriptors: list[MediaDescriptor] | None = None,
) -> MessageDetail:
    media = [descriptor.item for descriptor in media_descriptors or []]
    summary = serialize_message_summary(message, chat_id=chat_id)
    payload = summary.model_dump()
    payload["media_count"] = len(media) or summary.media_count
    return MessageDetail(**payload, media=media)


def serialize_resolve_result(input_value: str, entity: Any) -> ResolveResult:
    peer = serialize_peer_ref(entity)
    if peer is None:
        raise ValueError("Resolved entity cannot be null")
    return ResolveResult(input=input_value, peer=peer)


def build_media_descriptors(
    *,
    chat_id: str,
    anchor_message_id: int,
    messages: list[Any],
) -> list[MediaDescriptor]:
    descriptors: list[MediaDescriptor] = []
    for message in messages:
        if getattr(message, "media", None) is None:
            continue
        descriptors.append(
            serialize_media_descriptor(
                chat_id=chat_id,
                anchor_message_id=anchor_message_id,
                message=message,
            )
        )
    return descriptors


def unique_archive_name(preferred_name: str, existing: set[str]) -> str:
    candidate = preferred_name
    stem = Path(preferred_name).stem
    suffix = Path(preferred_name).suffix
    index = 1
    while candidate in existing:
        candidate = f"{stem}-{index}{suffix}"
        index += 1
    existing.add(candidate)
    return candidate
