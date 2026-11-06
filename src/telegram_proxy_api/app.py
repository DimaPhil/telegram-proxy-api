from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import datetime
from importlib.metadata import version
from typing import Any, AsyncIterator, Callable
from urllib.parse import quote

import yaml
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .config import Settings, get_settings
from .dependencies import get_gateway, require_access_token
from .errors import TelegramProxyError
from .gateway import MessageFilter, TelegramGateway
from .models import (
    ApiResponse,
    ChatDetail,
    ChatJoinResult,
    ChatLatest,
    ChatSummary,
    ContactSummary,
    HealthStatus,
    MediaItem,
    MessageContext,
    MessageDetail,
    MessageSummary,
    ProblemDetail,
    ResolveResult,
)
from .responses import ClosingStreamingResponse, api_response
from .updates import sse_frames

_MARKED_CHAT_ID = re.compile(r"-?[1-9]\d{0,19}")
LATEST_CHAT_LIMIT = 100
STREAM_CHAT_LIMIT = 500


def build_content_disposition(file_name: str) -> str:
    ascii_fallback = "".join(char if 32 <= ord(char) < 127 and char not in {'"', "\\"} else "_" for char in file_name)
    if not ascii_fallback.strip("._ "):
        ascii_fallback = "download"
    encoded = quote(file_name, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def message_filter(
    from_user: str | None = Query(
        default=None,
        description="Only messages sent by this user (numeric ID or @username). Search requires chat_id with it.",
    ),
    since: datetime | None = Query(
        default=None, description="Only messages sent at or after this ISO 8601 time; naive times are UTC."
    ),
    until: datetime | None = Query(
        default=None, description="Only messages sent before this ISO 8601 time; naive times are UTC."
    ),
    before_id: int | None = Query(
        default=None,
        ge=1,
        description="Cursor: only messages older than this message ID. Pass meta.next_before_id for the next page.",
    ),
) -> MessageFilter:
    return MessageFilter(from_user=from_user, since=since, until=until, before_id=before_id)


def chat_id_list(limit: int) -> Callable[[str], list[str]]:
    """Parse comma-separated marked chat ids (as /resolve returns them), deduplicated in first-seen order."""

    def parse(
        chat_ids: str = Query(
            description=f"Comma-separated marked chat ids as returned by /resolve, e.g. -1001234567890; 1 to {limit}.",
        ),
    ) -> list[str]:
        def invalid(message: str) -> RequestValidationError:
            return RequestValidationError(
                [{"type": "value_error", "loc": ("query", "chat_ids"), "msg": message, "input": chat_ids}]
            )

        values: list[str] = []
        for item in (part.strip() for part in chat_ids.split(",")):
            if not item:
                continue
            if not _MARKED_CHAT_ID.fullmatch(item):
                raise invalid(f"Invalid chat id {item[:32]!r}; use numeric marked ids.")
            if item not in values:
                values.append(item)
        if not values or len(values) > limit:
            raise invalid(f"Provide between 1 and {limit} distinct chat ids.")
        return values

    return parse


def create_app(
    *,
    settings: Settings | None = None,
    gateway: TelegramGateway | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    managed_gateway = gateway is None
    app_gateway = gateway or TelegramGateway(app_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = app_settings
        app.state.gateway = app_gateway
        if managed_gateway:
            await app_gateway.start()
        try:
            yield
        finally:
            if managed_gateway:
                await app_gateway.stop()

    app = FastAPI(
        title=app_settings.app_name,
        version=version("telegram-proxy-api"),
        lifespan=lifespan,
        dependencies=[Depends(require_access_token)],
    )

    @app.exception_handler(TelegramProxyError)
    async def handle_proxy_error(_: Request, exc: TelegramProxyError) -> JSONResponse:
        payload = ProblemDetail(
            type=exc.type,
            title=exc.title,
            status=exc.status_code,
            detail=exc.detail,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=payload.model_dump(mode="json"),
            headers=exc.headers,
        )

    @app.get("/healthz", operation_id="health", response_model=ApiResponse[HealthStatus])
    async def healthz(gateway: TelegramGateway = Depends(get_gateway)) -> ApiResponse[HealthStatus]:
        return api_response(gateway.health_status())

    @app.get("/me", operation_id="me", response_model=ApiResponse[ContactSummary])
    async def get_me(gateway: TelegramGateway = Depends(get_gateway)) -> ApiResponse[ContactSummary]:
        return api_response(await gateway.get_me())

    @app.get("/contacts", operation_id="contacts_list", response_model=ApiResponse[list[ContactSummary]])
    async def get_contacts(
        page: int = 1,
        page_size: int = 25,
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> ApiResponse[list[ContactSummary]]:
        return api_response(await gateway.list_contacts(page=page, page_size=page_size))

    @app.get("/resolve", operation_id="resolve", response_model=ApiResponse[ResolveResult])
    async def resolve_peer(
        value: str,
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> ApiResponse[ResolveResult]:
        return api_response(await gateway.resolve(value))

    @app.get("/chats", operation_id="chats_list", response_model=ApiResponse[list[ChatSummary]])
    async def get_chats(
        page: int = 1,
        page_size: int = 25,
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> ApiResponse[list[ChatSummary]]:
        return api_response(await gateway.list_chats(page=page, page_size=page_size))

    # Registered before /chats/{chat_id} so "latest" is not captured as a chat id.
    @app.get("/chats/latest", operation_id="chats_latest", response_model=ApiResponse[list[ChatLatest]])
    async def get_latest(
        chat_ids: list[str] = Depends(chat_id_list(LATEST_CHAT_LIMIT)),
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> ApiResponse[list[ChatLatest]]:
        """Newest message id per chat in one Telegram request; non-member or unknown chats report member=false."""
        return api_response(await gateway.latest_messages(chat_ids))

    @app.post("/chats/{chat_id}/join", operation_id="chats_join", response_model=ApiResponse[ChatJoinResult])
    async def join_chat(
        chat_id: str,
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> ApiResponse[ChatJoinResult]:
        """Join a public channel or supergroup, then mute and archive it. Idempotent; never sends messages."""
        return api_response(await gateway.join_chat(chat_id))

    @app.get(
        "/updates/stream",
        operation_id="updates_stream",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": (
                    "Server-sent events. `event: ready` (data `{}`) once subscribed; `event: message` per new "
                    "message in a requested chat with JSON data {chat_id, message_id, grouped_id, date}; "
                    "`: ping` comments while idle. Edits, deletions and other chats are never sent. The stream "
                    "closes if the client falls too far behind; reconnect and re-sync with chats_latest."
                ),
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            },
            503: {"description": "Passive updates are disabled or Telethon is unavailable.", "model": ProblemDetail},
        },
    )
    async def stream_updates(
        chat_ids: list[str] = Depends(chat_id_list(STREAM_CHAT_LIMIT)),
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> StreamingResponse:
        """Best-effort push of new messages; Telegram may throttle passive updates, so keep polling as fallback."""
        hub = gateway.updates_hub()
        return ClosingStreamingResponse(
            sse_frames(hub, frozenset(chat_ids), app_settings.telegram_updates_ping_interval),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/chats/{chat_id}", operation_id="chats_get", response_model=ApiResponse[ChatDetail])
    async def get_chat(
        chat_id: str,
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> ApiResponse[ChatDetail]:
        return api_response(await gateway.get_chat(chat_id))

    @app.get(
        "/chats/{chat_id}/photo",
        operation_id="chats_photo",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "The chat's small profile photo; Telegram serves JPEG.",
                "content": {
                    media_type: {"schema": {"type": "string", "format": "binary"}}
                    for media_type in ("image/jpeg", "image/png", "image/webp", "application/octet-stream")
                },
            },
            404: {"description": "The chat is unknown or has no profile photo.", "model": ProblemDetail},
        },
    )
    async def download_chat_photo(
        chat_id: str,
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> StreamingResponse:
        photo = await gateway.download_chat_photo(chat_id)
        headers = {
            "Content-Disposition": build_content_disposition(photo.file_name),
            "Content-Length": str(len(photo.content)),
            "X-Telegram-Provider": photo.provider,
            "X-Telegram-Fallback": str(photo.fallback_used).lower(),
        }
        return StreamingResponse(iter([photo.content]), media_type=photo.content_type, headers=headers)

    @app.get(
        "/users/{user_id}/common-chats",
        operation_id="users_common_chats",
        response_model=ApiResponse[list[ChatSummary]],
    )
    async def get_common_chats(
        user_id: str,
        page: int = 1,
        page_size: int = 25,
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> ApiResponse[list[ChatSummary]]:
        return api_response(await gateway.list_common_chats(user_ref=user_id, page=page, page_size=page_size))

    @app.get(
        "/chats/{chat_id}/messages", operation_id="messages_list", response_model=ApiResponse[list[MessageSummary]]
    )
    async def get_messages(
        chat_id: str,
        page: int = 1,
        page_size: int = 25,
        filters: MessageFilter = Depends(message_filter),
        include_raw: bool = Query(default=True, description="Include telegram_raw payloads."),
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> ApiResponse[list[MessageSummary]]:
        return api_response(
            await gateway.list_messages(
                chat_ref=chat_id, page=page, page_size=page_size, filters=filters, include_raw=include_raw
            )
        )

    @app.get(
        "/chats/{chat_id}/messages/{message_id}", operation_id="messages_get", response_model=ApiResponse[MessageDetail]
    )
    async def get_message(
        chat_id: str,
        message_id: int,
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> ApiResponse[MessageDetail]:
        return api_response(await gateway.get_message(chat_ref=chat_id, message_id=message_id))

    @app.get(
        "/chats/{chat_id}/messages/{message_id}/context",
        operation_id="messages_context",
        response_model=ApiResponse[MessageContext],
    )
    async def get_message_context(
        chat_id: str,
        message_id: int,
        context_size: int = 5,
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> ApiResponse[MessageContext]:
        data = await gateway.get_message_context(
            chat_ref=chat_id,
            message_id=message_id,
            context_size=context_size,
        )
        return api_response(data)

    @app.get("/messages/search", operation_id="messages_search", response_model=ApiResponse[list[MessageSummary]])
    async def search_messages(
        query: str | None = Query(default=None, description="Text to search for; optional when from_user is set."),
        chat_id: str | None = None,
        page: int = 1,
        page_size: int = 25,
        filters: MessageFilter = Depends(message_filter),
        include_raw: bool = Query(default=True, description="Include telegram_raw payloads."),
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> ApiResponse[list[MessageSummary]]:
        data = await gateway.search_messages(
            query=query,
            chat_ref=chat_id,
            page=page,
            page_size=page_size,
            filters=filters,
            include_raw=include_raw,
        )
        return api_response(data)

    @app.get(
        "/chats/{chat_id}/messages/{message_id}/media",
        operation_id="media_list",
        response_model=ApiResponse[list[MediaItem]],
    )
    async def get_message_media(
        chat_id: str,
        message_id: int,
        include_album: bool = False,
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> ApiResponse[list[MediaItem]]:
        data = await gateway.get_message_media(
            chat_ref=chat_id,
            message_id=message_id,
            include_album=include_album,
        )
        return api_response(data)

    @app.get(
        "/chats/{chat_id}/messages/{message_id}/media/bundle",
        operation_id="media_bundle",
        response_class=StreamingResponse,
        responses={200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}},
    )
    async def bundle_message_media(
        chat_id: str,
        message_id: int,
        include_album: bool = True,
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> StreamingResponse:
        bundle = await gateway.bundle_message_media(
            chat_ref=chat_id,
            message_id=message_id,
            include_album=include_album,
        )
        headers = {
            "Content-Disposition": build_content_disposition(bundle.file_name),
            "Content-Length": str(len(bundle.content)),
            "X-Telegram-Provider": bundle.provider,
            "X-Telegram-Fallback": str(bundle.fallback_used).lower(),
        }
        return StreamingResponse(
            iter([bundle.content]),
            media_type="application/zip",
            headers=headers,
        )

    @app.get(
        "/chats/{chat_id}/messages/{message_id}/media/{media_id}",
        operation_id="media_download",
        response_class=StreamingResponse,
        responses={200: {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}},
    )
    async def stream_message_media(
        chat_id: str,
        message_id: int,
        media_id: str,
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> StreamingResponse:
        stream = await gateway.stream_message_media(
            chat_ref=chat_id,
            message_id=message_id,
            media_id=media_id,
        )
        headers = {
            "Content-Disposition": build_content_disposition(stream.file_name),
            "X-Telegram-Provider": stream.provider,
            "X-Telegram-Fallback": str(stream.fallback_used).lower(),
        }
        if stream.size_bytes is not None:
            headers["Content-Length"] = str(stream.size_bytes)
        return ClosingStreamingResponse(stream.chunks, media_type=stream.content_type, headers=headers)

    @app.get("/schema/openapi.json", include_in_schema=False)
    async def explicit_openapi_json() -> dict[str, Any]:
        return app.openapi()

    @app.get("/schema/openapi.yaml", include_in_schema=False)
    async def explicit_openapi_yaml() -> PlainTextResponse:
        payload = yaml.safe_dump(app.openapi(), sort_keys=False)
        return PlainTextResponse(payload, media_type="application/yaml")

    return app


app = create_app()
