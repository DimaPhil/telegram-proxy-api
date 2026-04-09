from __future__ import annotations

import asyncio
import io
import json
import math
import re
import zipfile
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from telethon import TelegramClient, utils
from telethon.errors import (
    AuthKeyDuplicatedError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    RPCError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl import types
from telethon.sessions import SQLiteSession
from telethon.tl.functions.contacts import GetContactsRequest

from .config import Settings
from .errors import (
    AuthenticationRequiredError,
    BadRequestError,
    ResourceNotFoundError,
    TelegramFloodWaitError,
    TelegramProxyError,
)
from .models import (
    ChatDetail,
    ChatSummary,
    ContactSummary,
    HealthStatus,
    MessageContext,
    MessageDetail,
    MessageSummary,
    ResolveResult,
    ResponseMeta,
)
from .serializers import (
    MediaDescriptor,
    build_media_descriptors,
    serialize_chat_detail,
    serialize_chat_summary,
    serialize_contact,
    serialize_message_detail,
    serialize_message_summary,
    serialize_resolve_result,
    unique_archive_name,
)
from .session import normalize_string_session


_INTEGER_REF = re.compile(r"^-?\d+$")
_MAX_SIGNED_CHAT_ID = 2_147_483_647


@dataclass(slots=True)
class MediaStream:
    file_name: str
    content_type: str
    size_bytes: int | None
    chunks: AsyncIterator[bytes]


@dataclass(slots=True)
class MediaBundle:
    file_name: str
    content: bytes


class TelegramGateway:
    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[..., Any] = TelegramClient,
    ) -> None:
        self.settings = settings
        self._client_factory = client_factory
        self._client: Any | None = None
        self._started = False
        self._start_lock = asyncio.Lock()
        self._history_limiter = asyncio.Semaphore(settings.telegram_history_concurrency)
        self._entity_cache: dict[str, Any] = {}

    @property
    def client(self) -> Any:
        if self._client is None:
            raise AuthenticationRequiredError("Telegram client has not been started.")
        return self._client

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return

            try:
                self.settings.validate_telegram_credentials()
            except ValueError as exc:
                raise AuthenticationRequiredError(str(exc)) from exc

            self.settings.telegram_session_dir.mkdir(parents=True, exist_ok=True)
            normalize_string_session(
                self.settings.telegram_session_string,
                self.settings.session_path,
            )

            session = SQLiteSession(str(self.settings.session_path))
            self._client = self._client_factory(
                session,
                self.settings.telegram_api_id,
                self.settings.telegram_api_hash,
                auto_reconnect=True,
                connection_retries=self.settings.telegram_connection_retries,
                retry_delay=self.settings.telegram_retry_delay,
                flood_sleep_threshold=self.settings.telegram_flood_sleep_threshold,
                receive_updates=False,
                request_retries=self.settings.telegram_request_retries,
                timeout=self.settings.telegram_request_timeout,
            )
            await self.client.connect()
            if not await self.client.is_user_authorized():
                await self.client.disconnect()
                if hasattr(session, "close"):
                    session.close()
                raise AuthenticationRequiredError(
                    "Telegram session is missing or expired. Run telegram-proxy-auth first."
                )

            self._started = True

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            session = getattr(self._client, "session", None)
            if session is not None and hasattr(session, "close"):
                session.close()
        self._client = None
        self._started = False

    def is_connected(self) -> bool:
        if self._client is None:
            return False
        is_connected = getattr(self._client, "is_connected", None)
        if callable(is_connected):
            return bool(is_connected())
        return False

    def health_status(self) -> HealthStatus:
        return HealthStatus(
            status="ok" if self._started and self.is_connected() else "degraded",
            telegram_connected=self._started and self.is_connected(),
            session_file=str(self.settings.session_file_path),
        )

    async def get_me(self) -> ContactSummary:
        user = await self._call(self.client.get_me())
        if user is None:
            raise AuthenticationRequiredError("Telegram user could not be loaded.")
        return serialize_contact(user)

    async def list_contacts(
        self,
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[ContactSummary], ResponseMeta]:
        self._validate_page_size(page_size)
        self._validate_page(page)
        result = await self._call(self.client(GetContactsRequest(hash=0)))
        contacts = [serialize_contact(user) for user in getattr(result, "users", [])]
        start = (page - 1) * page_size
        end = start + page_size
        sliced = contacts[start:end]
        return sliced, ResponseMeta(
            page=page,
            page_size=page_size,
            total=len(contacts),
        )

    async def list_chats(
        self,
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[ChatSummary], ResponseMeta]:
        self._validate_page_size(page_size)
        self._validate_page(page)
        limit = page * page_size + 1
        dialogs = await self._call(self.client.get_dialogs(limit=limit))
        start = (page - 1) * page_size
        end = start + page_size
        items = [serialize_chat_summary(dialog) for dialog in dialogs[start:end]]
        return items, ResponseMeta(
            page=page,
            page_size=page_size,
            total=len(dialogs) if len(dialogs) < limit else None,
        )

    async def get_chat(self, chat_ref: str) -> ChatDetail:
        entity = await self._resolve_entity(chat_ref)
        about = getattr(entity, "about", None)
        return serialize_chat_detail(entity, about=about)

    async def list_messages(
        self,
        *,
        chat_ref: str,
        page: int,
        page_size: int,
    ) -> tuple[list[MessageSummary], ResponseMeta]:
        self._validate_page_size(page_size)
        self._validate_page(page)
        entity = await self._resolve_entity(chat_ref)
        chat_id = self._entity_key(chat_ref, entity)
        offset = (page - 1) * page_size
        messages = await self._history_call(
            self.client.get_messages(
                entity,
                limit=page_size + 1,
                add_offset=offset,
                wait_time=self.settings.telegram_history_wait_time,
            )
        )
        items = [serialize_message_summary(message, chat_id=chat_id) for message in messages[:page_size]]
        total = None
        if len(messages) <= page_size and page == 1:
            total = len(messages)
        return items, ResponseMeta(page=page, page_size=page_size, total=total)

    async def get_message(self, *, chat_ref: str, message_id: int) -> MessageDetail:
        entity = await self._resolve_entity(chat_ref)
        chat_id = self._entity_key(chat_ref, entity)
        message = await self._fetch_message(entity, message_id)
        media_descriptors = build_media_descriptors(
            chat_id=chat_id,
            anchor_message_id=message_id,
            messages=[message],
        )
        return serialize_message_detail(message, chat_id=chat_id, media_descriptors=media_descriptors)

    async def get_message_context(
        self,
        *,
        chat_ref: str,
        message_id: int,
        context_size: int,
    ) -> MessageContext:
        if context_size < 1 or context_size > self.settings.max_context_size:
            raise BadRequestError(
                f"context_size must be between 1 and {self.settings.max_context_size}"
            )
        entity = await self._resolve_entity(chat_ref)
        chat_id = self._entity_key(chat_ref, entity)
        message = await self._fetch_message(entity, message_id)

        before = await self._history_call(
            self.client.get_messages(
                entity,
                limit=context_size,
                max_id=message_id,
                wait_time=self.settings.telegram_history_wait_time,
            )
        )
        after = await self._history_call(
            self.client.get_messages(
                entity,
                limit=context_size,
                min_id=message_id,
                reverse=True,
                wait_time=self.settings.telegram_history_wait_time,
            )
        )
        media_descriptors = build_media_descriptors(
            chat_id=chat_id,
            anchor_message_id=message_id,
            messages=[message],
        )
        return MessageContext(
            before=[serialize_message_summary(item, chat_id=chat_id) for item in reversed(before)],
            message=serialize_message_detail(
                message,
                chat_id=chat_id,
                media_descriptors=media_descriptors,
            ),
            after=[serialize_message_summary(item, chat_id=chat_id) for item in after],
        )

    async def search_messages(
        self,
        *,
        query: str,
        chat_ref: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[MessageSummary], ResponseMeta]:
        if not query.strip():
            raise BadRequestError("query is required")
        self._validate_page_size(page_size)
        self._validate_page(page)
        offset = (page - 1) * page_size
        entity = await self._resolve_entity(chat_ref) if chat_ref else None
        chat_id = self._entity_key(chat_ref, entity) if chat_ref else "global"
        messages = await self._history_call(
            self.client.get_messages(
                entity,
                limit=page_size + 1,
                add_offset=offset,
                search=query,
                wait_time=self.settings.telegram_history_wait_time,
            )
        )
        items = [serialize_message_summary(message, chat_id=chat_id) for message in messages[:page_size]]
        return items, ResponseMeta(page=page, page_size=page_size, total=None)

    async def resolve(self, peer_ref: str) -> ResolveResult:
        entity = await self._resolve_entity(peer_ref, allow_remote_lookup=True)
        return serialize_resolve_result(peer_ref, entity)

    async def get_message_media(
        self,
        *,
        chat_ref: str,
        message_id: int,
        include_album: bool,
    ) -> tuple[list[Any], ResponseMeta]:
        entity = await self._resolve_entity(chat_ref)
        chat_id = self._entity_key(chat_ref, entity)
        target = await self._fetch_message(entity, message_id)
        descriptors = await self._media_descriptors(
            entity=entity,
            chat_id=chat_id,
            anchor_message=target,
            include_album=include_album,
        )
        return [descriptor.item for descriptor in descriptors], ResponseMeta(
            include_album=include_album,
        )

    async def stream_message_media(
        self,
        *,
        chat_ref: str,
        message_id: int,
        media_id: str,
    ) -> MediaStream:
        entity = await self._resolve_entity(chat_ref)
        chat_id = self._entity_key(chat_ref, entity)
        target = await self._fetch_message(entity, message_id)
        descriptors = await self._media_descriptors(
            entity=entity,
            chat_id=chat_id,
            anchor_message=target,
            include_album=True,
        )
        descriptor = next((item for item in descriptors if item.item.media_id == media_id), None)
        if descriptor is None:
            raise ResourceNotFoundError(f"Media item {media_id} was not found.")

        async def iterator() -> AsyncIterator[bytes]:
            async for chunk in self.client.iter_download(descriptor.message.media):
                yield chunk

        return MediaStream(
            file_name=descriptor.file_name,
            content_type=descriptor.content_type,
            size_bytes=descriptor.size_bytes,
            chunks=iterator(),
        )

    async def bundle_message_media(
        self,
        *,
        chat_ref: str,
        message_id: int,
        include_album: bool,
    ) -> MediaBundle:
        entity = await self._resolve_entity(chat_ref)
        chat_id = self._entity_key(chat_ref, entity)
        target = await self._fetch_message(entity, message_id)
        descriptors = await self._media_descriptors(
            entity=entity,
            chat_id=chat_id,
            anchor_message=target,
            include_album=include_album,
        )
        if not descriptors:
            raise ResourceNotFoundError(f"Message {message_id} does not contain downloadable media.")

        buffer = io.BytesIO()
        used_names: set[str] = set()
        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            manifest = [descriptor.item.model_dump(mode="json") for descriptor in descriptors]
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            for descriptor in descriptors:
                file_name = unique_archive_name(descriptor.file_name, used_names)
                payload = await self._read_media_bytes(descriptor)
                archive.writestr(file_name, payload)

        bundle_name = f"telegram-message-{message_id}-media.zip"
        return MediaBundle(file_name=bundle_name, content=buffer.getvalue())

    async def _media_descriptors(
        self,
        *,
        entity: Any,
        chat_id: str,
        anchor_message: Any,
        include_album: bool,
    ) -> list[MediaDescriptor]:
        messages = [anchor_message]
        grouped_id = getattr(anchor_message, "grouped_id", None)
        if include_album and grouped_id:
            lower_bound = max(anchor_message.id - self.settings.telegram_album_window, 0)
            upper_bound = anchor_message.id + self.settings.telegram_album_window
            siblings = await self._history_call(
                self.client.get_messages(
                    entity,
                    limit=(self.settings.telegram_album_window * 2) + 1,
                    min_id=lower_bound,
                    max_id=upper_bound,
                    reverse=True,
                    wait_time=self.settings.telegram_history_wait_time,
                )
            )
            messages = [
                message
                for message in siblings
                if getattr(message, "grouped_id", None) == grouped_id
            ]
            if not messages:
                messages = [anchor_message]

        return build_media_descriptors(
            chat_id=chat_id,
            anchor_message_id=anchor_message.id,
            messages=sorted(messages, key=lambda item: item.id),
        )

    async def _read_media_bytes(self, descriptor: MediaDescriptor) -> bytes:
        chunks = bytearray()
        async for chunk in self.client.iter_download(descriptor.message.media):
            chunks.extend(chunk)
        return bytes(chunks)

    async def _fetch_message(self, entity: Any, message_id: int) -> Any:
        message = await self._call(self.client.get_messages(entity, ids=message_id))
        if message is None:
            raise ResourceNotFoundError(f"Message {message_id} was not found.")
        return message

    async def _resolve_entity(
        self,
        peer_ref: str | None,
        *,
        allow_remote_lookup: bool = False,
    ) -> Any:
        if peer_ref is None:
            return None
        cache_key = str(peer_ref)
        if cache_key in self._entity_cache:
            return self._entity_cache[cache_key]

        candidates = self._lookup_candidates(peer_ref)
        entity = None
        last_lookup_error: Exception | None = None

        for candidate in candidates:
            try:
                entity = await self.client.get_input_entity(candidate)
                break
            except (TypeError, ValueError) as exc:
                last_lookup_error = exc

        if entity is None:
            remote_candidate = candidates[0]
            if not allow_remote_lookup and not self._is_numeric_reference(peer_ref):
                raise ResourceNotFoundError(
                    f"Unknown chat reference '{peer_ref}'. Resolve it once and use its numeric id."
                ) from None
            entity = await self._call(self.client.get_entity(remote_candidate))

        self._remember_entity(cache_key, entity)
        self._remember_entity(self._entity_key(peer_ref, entity), entity)
        return entity

    @staticmethod
    def _is_numeric_reference(peer_ref: str | None) -> bool:
        return bool(peer_ref is not None and _INTEGER_REF.match(str(peer_ref)))

    def _lookup_candidates(self, peer_ref: str) -> list[Any]:
        value = str(peer_ref).strip()
        if not self._is_numeric_reference(value):
            return [value]

        numeric = int(value)
        candidates: list[Any] = []

        if numeric < 0:
            if abs(numeric) > 1_000_000_000_000:
                candidates.append(utils.get_peer(numeric))
            else:
                if abs(numeric) > _MAX_SIGNED_CHAT_ID:
                    candidates.append(types.PeerChannel(abs(numeric)))
                candidates.append(utils.get_peer(numeric))
        else:
            candidates.extend(
                [
                    numeric,
                    types.PeerChannel(numeric),
                    types.PeerChat(numeric),
                    types.PeerUser(numeric),
                ]
            )

        deduped: list[Any] = []
        seen: set[str] = set()
        for candidate in candidates:
            marker = repr(candidate)
            if marker in seen:
                continue
            seen.add(marker)
            deduped.append(candidate)
        return deduped

    def _remember_entity(self, key: str, entity: Any) -> None:
        self._entity_cache[key] = entity
        overflow = len(self._entity_cache) - self.settings.telegram_entity_cache_limit
        if overflow > 0:
            for stale_key in list(self._entity_cache.keys())[:overflow]:
                self._entity_cache.pop(stale_key, None)

    def _entity_key(self, peer_ref: str | None, entity: Any) -> str:
        if entity is None:
            return str(peer_ref)
        entity_id = getattr(entity, "channel_id", None) or getattr(entity, "chat_id", None) or getattr(
            entity,
            "user_id",
            None,
        )
        if entity_id is None and hasattr(entity, "id"):
            entity_id = entity.id
        return str(entity_id if entity_id is not None else peer_ref)

    def _validate_page_size(self, page_size: int) -> None:
        if page_size < 1 or page_size > self.settings.max_page_size:
            raise BadRequestError(
                f"page_size must be between 1 and {self.settings.max_page_size}"
            )

    @staticmethod
    def _validate_page(page: int) -> None:
        if page < 1:
            raise BadRequestError("page must be greater than or equal to 1")

    async def _history_call(self, awaitable: Any) -> Any:
        async with self._history_limiter:
            return await self._call(awaitable)

    async def _call(self, awaitable: Any) -> Any:
        try:
            return await awaitable
        except FloodWaitError as exc:
            raise TelegramFloodWaitError(int(math.ceil(exc.seconds))) from exc
        except AuthKeyDuplicatedError as exc:
            raise AuthenticationRequiredError(
                "Telegram rejected this session because it is being used elsewhere."
            ) from exc
        except (
            UsernameInvalidError,
            UsernameNotOccupiedError,
            InviteHashInvalidError,
            InviteHashExpiredError,
        ) as exc:
            raise ResourceNotFoundError(str(exc)) from exc
        except RPCError as exc:
            raise TelegramProxyError(
                title="Telegram RPC error",
                detail=str(exc),
                status_code=502,
                type="https://telegram-proxy/errors/telegram-rpc",
            ) from exc
