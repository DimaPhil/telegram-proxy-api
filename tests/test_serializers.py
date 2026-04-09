from __future__ import annotations

from telethon.tl import types

from telegram_proxy_api.serializers import (
    build_media_descriptors,
    safe_telegram_dict,
    serialize_chat_summary,
    serialize_contact,
    serialize_message_detail,
    serialize_message_summary,
    unique_archive_name,
)

from tests.conftest import (
    make_chat,
    make_dialog,
    make_forward_header,
    make_media,
    make_message,
    make_user,
)


def test_serialize_chat_summary():
    message = make_message(9)
    chat = make_chat(200, title="Engineering")
    dialog = make_dialog(chat, unread_count=7, archived=True, message=message)

    result = serialize_chat_summary(dialog)

    assert result.id == "200"
    assert result.title == "Engineering"
    assert result.unread_count == 7
    assert result.archived is True


def test_serialize_contact():
    user = make_user(99, first_name="Ada", last_name="Lovelace", phone="+123")

    result = serialize_contact(user)

    assert result.id == "99"
    assert result.display_name == "Ada Lovelace"
    assert result.phone == "+123"


def test_serialize_message_detail_includes_media():
    media, file = make_media(payload=b"image-data", file_name="photo.jpg")
    message = make_message(11, media=media, file=file)
    descriptors = build_media_descriptors(chat_id="200", anchor_message_id=11, messages=[message])

    result = serialize_message_detail(message, chat_id="200", media_descriptors=descriptors)

    assert result.id == "11"
    assert result.has_media is True
    assert result.media_count == 1
    assert result.media[0].file_name == "photo.jpg"
    assert result.media[0].access_path.endswith("/media/11:0")


def test_serialize_message_summary_uses_sender():
    sender = make_user(55, first_name="Grace", last_name="Hopper")
    message = make_message(5, sender=sender)

    result = serialize_message_summary(message, chat_id="200")

    assert result.sender is not None
    assert result.sender.id == "55"
    assert result.sender.display_name == "Grace Hopper"


def test_unique_archive_name_deduplicates():
    used = {"photo.jpg"}

    assert unique_archive_name("photo.jpg", used) == "photo-1.jpg"
    assert unique_archive_name("photo.jpg", used) == "photo-2.jpg"


def test_safe_telegram_dict_sanitizes_bytes_and_nested_values():
    class RawValue:
        def to_dict(self):
            return {
                "file_reference": b"\xff\x00abc",
                "nested": {"items": [b"\x01\x02", 7]},
            }

    result = safe_telegram_dict(RawValue())

    assert result["file_reference"]["__type__"] == "bytes"
    assert result["file_reference"]["base64"] == "/wBhYmM="
    assert result["nested"]["items"][0]["base64"] == "AQI="


def test_serialize_forwarded_message_summary_exposes_forward_origin():
    forward_header = make_forward_header(
        from_peer=types.PeerChannel(777),
        channel_post=55,
        post_author="Channel Admin",
    )
    message = make_message(12, fwd_from=forward_header)

    result = serialize_message_summary(message, chat_id="200")

    assert result.is_forwarded is True
    assert result.forward_info is not None
    assert result.forward_info.from_peer is not None
    assert result.forward_info.from_peer.id == "-1000000000777"
    assert result.forward_info.from_peer.type == "channel"
    assert result.forward_info.channel_post == "55"
    assert result.forward_info.post_author == "Channel Admin"
