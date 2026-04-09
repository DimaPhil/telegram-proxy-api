from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon.tl import types

from telegram_proxy_api.config import Settings


def make_raw_dict(**values):
    class RawDict:
        def __init__(self, payload):
            self.payload = payload

        def to_dict(self):
            return self.payload

    return RawDict(values)


def make_user(
    user_id: int = 100,
    *,
    first_name: str = "Test",
    last_name: str = "User",
    username: str | None = "testuser",
    phone: str | None = None,
    bot: bool = False,
):
    return SimpleNamespace(
        id=user_id,
        first_name=first_name,
        last_name=last_name,
        username=username,
        phone=phone,
        bot=bot,
        mutual_contact=True,
        to_dict=lambda: {"id": user_id, "username": username},
    )


def make_chat(chat_id: int = 200, *, title: str = "Chat Title", username: str | None = None):
    return SimpleNamespace(
        id=chat_id,
        title=title,
        username=username,
        megagroup=True,
        to_dict=lambda: {"id": chat_id, "title": title},
    )


def make_dialog(chat, *, unread_count: int = 0, archived: bool = False, message=None):
    return SimpleNamespace(
        entity=chat,
        unread_count=unread_count,
        folder_id=1 if archived else None,
        message=message,
    )


def make_message(
    message_id: int = 1,
    *,
    chat_id: int = 200,
    text: str = "hello",
    sender=None,
    sender_id: int | None = None,
    grouped_id: int | None = None,
    fwd_from=None,
    media=None,
    file=None,
):
    sender = sender if sender is not None else make_user(sender_id or 100)
    sender_id = sender_id if sender_id is not None else getattr(sender, "id", None)
    return SimpleNamespace(
        id=message_id,
        chat_id=chat_id,
        message=text,
        date=datetime(2024, 1, 1, tzinfo=UTC),
        edit_date=None,
        sender=sender,
        sender_id=sender_id,
        fwd_from=fwd_from,
        reply_to=None,
        grouped_id=grouped_id,
        views=10,
        forwards=2,
        media=media,
        file=file,
        photo=bool(media and getattr(media, "kind", "") == "photo"),
        video=bool(media and getattr(media, "kind", "") == "video"),
        gif=bool(media and getattr(media, "kind", "") == "gif"),
        audio=False,
        voice=False,
        sticker=False,
        document=media,
        to_dict=lambda: {"id": message_id, "text": text},
    )


def make_forward_header(
    *,
    from_peer=None,
    from_name: str | None = None,
    channel_post: int | None = None,
    post_author: str | None = None,
    saved_from_peer=None,
    saved_from_msg_id: int | None = None,
):
    return types.MessageFwdHeader(
        date=datetime(2023, 12, 31, tzinfo=UTC),
        from_id=from_peer,
        from_name=from_name,
        channel_post=channel_post,
        post_author=post_author,
        saved_from_peer=saved_from_peer,
        saved_from_msg_id=saved_from_msg_id,
    )


def make_media(
    payload: bytes = b"payload",
    *,
    kind: str = "photo",
    file_name: str = "photo.jpg",
    mime_type: str = "image/jpeg",
    ext: str = ".jpg",
    size: int | None = None,
    duration: float | None = None,
):
    size = size or len(payload)
    file = SimpleNamespace(
        name=file_name,
        mime_type=mime_type,
        ext=ext,
        size=size,
        width=800,
        height=600,
        duration=duration,
    )
    media = SimpleNamespace(
        kind=kind,
        payload=payload,
        to_dict=lambda: {"kind": kind, "file_name": file_name},
    )
    return media, file


@pytest.fixture
def settings(tmp_path):
    return Settings(
        TELEGRAM_API_ID=12345,
        TELEGRAM_API_HASH="hash",
        TELEGRAM_SESSION_DIR=tmp_path / "telegram",
        TELEGRAM_SESSION_NAME="telegram_proxy_test",
    )
