from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from telegram_proxy_api.app import build_content_disposition, create_app
from telegram_proxy_api.config import Settings
from telegram_proxy_api.gateway import MediaBundle, MediaStream
from telegram_proxy_api.models import (
    ChatDetail,
    ChatSummary,
    ContactSummary,
    HealthStatus,
    MediaItem,
    MessageContext,
    MessageDetail,
    MessageSummary,
    PeerRef,
    ResolveResult,
    ResponseMeta,
)


async def _stream_bytes(payload: bytes):
    yield payload


class FakeGateway:
    def health_status(self):
        return HealthStatus(status="ok", telegram_connected=True, session_file="/tmp/test.session")

    async def get_me(self):
        return ContactSummary(
            id="1",
            display_name="Owner",
            username="owner",
            phone=None,
            mutual_contact=True,
            bot=False,
            telegram_raw={
                "file_reference": {
                    "__type__": "bytes",
                    "base64": "/wBhYmM=",
                }
            },
        )

    async def list_contacts(self, *, page: int, page_size: int):
        return [await self.get_me()], ResponseMeta(page=page, page_size=page_size, total=1)

    async def resolve(self, value: str):
        return ResolveResult(input=value, peer=PeerRef(id="42", display_name="Resolved", username="resolved", type="user"))

    async def list_chats(self, *, page: int, page_size: int):
        return [
            ChatSummary(
                id="100",
                title="Engineering",
                username=None,
                type="supergroup",
                unread_count=0,
                archived=False,
                last_message_at=None,
                telegram_raw={},
            )
        ], ResponseMeta(page=page, page_size=page_size, total=1)

    async def get_chat(self, chat_id: str):
        return ChatDetail(
            id=chat_id,
            title="Engineering",
            username=None,
            type="supergroup",
            unread_count=0,
            archived=False,
            last_message_at=None,
            participant_count=3,
            about="About",
            telegram_raw={},
        )

    async def list_messages(self, *, chat_ref: str, page: int, page_size: int):
        message = await self.get_message(chat_ref=chat_ref, message_id=10)
        return [MessageSummary(**message.model_dump(exclude={"media"}))], ResponseMeta(page=page, page_size=page_size)

    async def get_message(self, *, chat_ref: str, message_id: int):
        media = MediaItem(
            media_id=f"{message_id}:0",
            message_id=str(message_id),
            grouped_id=None,
            kind="photo",
            mime_type="image/jpeg",
            file_name="photo.jpg",
            extension=".jpg",
            size_bytes=3,
            width=10,
            height=10,
            duration_seconds=None,
            access_path=f"/chats/{chat_ref}/messages/{message_id}/media/{message_id}:0",
            telegram_raw={},
        )
        return MessageDetail(
            id=str(message_id),
            chat_id=chat_ref,
            date=None,
            edit_date=None,
            text="hello",
            sender=PeerRef(id="1", display_name="Owner", username="owner", type="user"),
            is_forwarded=True,
            forward_info={
                "date": None,
                "imported": False,
                "saved_out": False,
                "from_peer": {
                    "id": "-1000000000777",
                    "display_name": None,
                    "username": None,
                    "type": "channel",
                },
                "from_name": None,
                "channel_post": "55",
                "post_author": "Channel Admin",
                "saved_from_peer": None,
                "saved_from_message_id": None,
                "saved_from_name": None,
                "saved_date": None,
                "psa_type": None,
                "telegram_raw": {},
            },
            reply_to_message_id=None,
            grouped_id=None,
            views=1,
            forwards=0,
            has_media=True,
            media_count=1,
            media=[media],
            telegram_raw={},
        )

    async def get_message_context(self, *, chat_ref: str, message_id: int, context_size: int):
        message = await self.get_message(chat_ref=chat_ref, message_id=message_id)
        summary = MessageSummary(**message.model_dump(exclude={"media"}))
        return MessageContext(before=[summary], message=message, after=[summary])

    async def search_messages(self, *, query: str, chat_ref: str | None, page: int, page_size: int):
        message = await self.get_message(chat_ref=chat_ref or "global", message_id=10)
        return [MessageSummary(**message.model_dump(exclude={"media"}))], ResponseMeta(page=page, page_size=page_size)

    async def get_message_media(self, *, chat_ref: str, message_id: int, include_album: bool):
        message = await self.get_message(chat_ref=chat_ref, message_id=message_id)
        return message.media, ResponseMeta(include_album=include_album)

    async def stream_message_media(self, *, chat_ref: str, message_id: int, media_id: str):
        return MediaStream(file_name="фото.jpg", content_type="image/jpeg", size_bytes=3, chunks=_stream_bytes(b"abc"))

    async def bundle_message_media(self, *, chat_ref: str, message_id: int, include_album: bool):
        return MediaBundle(file_name="архив.zip", content=b"zip")


def create_test_client(*, auth_enabled: bool = False, bearer_token: str | None = None) -> TestClient:
    settings = Settings(
        TELEGRAM_API_ID=1,
        TELEGRAM_API_HASH="hash",
        API_AUTH_ENABLED=auth_enabled,
        API_BEARER_TOKEN=bearer_token,
    )
    app = create_app(settings=settings, gateway=FakeGateway())
    return TestClient(app)


def test_healthz():
    with create_test_client() as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ok"


def test_requires_bearer_token_when_enabled():
    with create_test_client(auth_enabled=True, bearer_token="secret") as client:
        response = client.get("/healthz")

    assert response.status_code == 401
    assert response.json()["title"] == "Unauthorized"


def test_accepts_bearer_token_when_enabled():
    with create_test_client(auth_enabled=True, bearer_token="secret") as client:
        response = client.get("/healthz", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200


def test_message_media_stream_response():
    with create_test_client() as client:
        response = client.get("/chats/100/messages/10/media/10:0")

    assert response.status_code == 200
    assert "filename*=UTF-8''%D1%84%D0%BE%D1%82%D0%BE.jpg" in response.headers["content-disposition"]
    assert response.content == b"abc"


def test_openapi_yaml_endpoint():
    with create_test_client() as client:
        response = client.get("/schema/openapi.yaml")

    assert response.status_code == 200
    assert "openapi:" in response.text


def test_messages_endpoint():
    with create_test_client() as client:
        response = client.get("/chats/100/messages")

    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "10"


def test_additional_json_endpoints():
    with create_test_client() as client:
        me = client.get("/me")
        contacts = client.get("/contacts")
        resolve = client.get("/resolve", params={"value": "@resolved"})
        chats = client.get("/chats")
        chat = client.get("/chats/100")
        message = client.get("/chats/100/messages/10")
        context = client.get("/chats/100/messages/10/context", params={"context_size": 1})
        search = client.get("/messages/search", params={"query": "hello"})
        media_manifest = client.get("/chats/100/messages/10/media", params={"include_album": True})
        bundle = client.get("/chats/100/messages/10/media/bundle")
        openapi_json = client.get("/schema/openapi.json")

    assert me.status_code == 200
    assert contacts.json()["meta"]["total"] == 1
    assert resolve.json()["data"]["peer"]["id"] == "42"
    assert chats.json()["data"][0]["id"] == "100"
    assert chat.json()["data"]["participant_count"] == 3
    assert message.json()["data"]["media"][0]["file_name"] == "photo.jpg"
    assert message.json()["data"]["is_forwarded"] is True
    assert message.json()["data"]["forward_info"]["from_peer"]["type"] == "channel"
    assert context.json()["meta"]["context_size"] == 1
    assert search.json()["data"][0]["id"] == "10"
    assert media_manifest.json()["meta"]["include_album"] is True
    assert bundle.content == b"zip"
    assert "filename*=UTF-8''%D0%B0%D1%80%D1%85%D0%B8%D0%B2.zip" in bundle.headers["content-disposition"]
    assert "paths" in openapi_json.json()


def test_me_endpoint_serializes_binary_telegram_raw():
    with create_test_client() as client:
        response = client.get("/me")

    assert response.status_code == 200
    assert response.json()["data"]["telegram_raw"]["file_reference"]["base64"] == "/wBhYmM="


def test_content_disposition_builds_ascii_fallback_and_utf8_filename():
    header = build_content_disposition('файл "1".jpg')

    assert 'attachment; filename="' in header
    assert "filename*=UTF-8''%D1%84%D0%B0%D0%B9%D0%BB%20%221%22.jpg" in header
