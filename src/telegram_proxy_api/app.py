from __future__ import annotations

from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import Any, AsyncIterator
from urllib.parse import quote

import yaml
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .config import Settings, get_settings
from .dependencies import get_gateway, require_access_token
from .errors import TelegramProxyError
from .gateway import TelegramGateway
from .models import (
    ApiResponse,
    ChatDetail,
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


def build_content_disposition(file_name: str) -> str:
    ascii_fallback = "".join(char if 32 <= ord(char) < 127 and char not in {'"', "\\"} else "_" for char in file_name)
    if not ascii_fallback.strip("._ "):
        ascii_fallback = "download"
    encoded = quote(file_name, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


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

    @app.get("/chats/{chat_id}", operation_id="chats_get", response_model=ApiResponse[ChatDetail])
    async def get_chat(
        chat_id: str,
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> ApiResponse[ChatDetail]:
        return api_response(await gateway.get_chat(chat_id))

    @app.get(
        "/chats/{chat_id}/messages", operation_id="messages_list", response_model=ApiResponse[list[MessageSummary]]
    )
    async def get_messages(
        chat_id: str,
        page: int = 1,
        page_size: int = 25,
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> ApiResponse[list[MessageSummary]]:
        return api_response(await gateway.list_messages(chat_ref=chat_id, page=page, page_size=page_size))

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
        query: str,
        chat_id: str | None = None,
        page: int = 1,
        page_size: int = 25,
        gateway: TelegramGateway = Depends(get_gateway),
    ) -> ApiResponse[list[MessageSummary]]:
        data = await gateway.search_messages(
            query=query,
            chat_ref=chat_id,
            page=page,
            page_size=page_size,
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
