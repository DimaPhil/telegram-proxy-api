from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, AsyncIterator, Callable, Generic, Literal, TypeVar

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
from telethon.sessions import SQLiteSession
from telethon.tl import types
from telethon.tl.functions.contacts import GetContactsRequest

from .bot_client import BotApiClient, BotApiError, BotFileStream
from .bot_serializers import bot_media_object, serialize_bot_chat, serialize_bot_message
from .bot_store import BotUpdateStore
from .config import Settings
from .errors import (
    AuthenticationRequiredError,
    BadRequestError,
    ResourceNotFoundError,
    TelegramFloodWaitError,
    TelegramProxyError,
)
from .models import (
    BotHealth,
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
    TelethonHealth,
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
    provider: Literal["bot_api", "telethon"] = "telethon"
    fallback_used: bool = False


@dataclass(slots=True)
class MediaBundle:
    file_name: str
    content: bytes
    provider: Literal["bot_api", "telethon"] = "telethon"
    fallback_used: bool = False


class TelethonGateway:
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
            raise BadRequestError(f"context_size must be between 1 and {self.settings.max_context_size}")
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
            messages = [message for message in siblings if getattr(message, "grouped_id", None) == grouped_id]
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

        for candidate in candidates:
            try:
                entity = await self.client.get_input_entity(candidate)
                break
            except (TypeError, ValueError):
                pass

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
        entity_id = (
            getattr(entity, "channel_id", None)
            or getattr(entity, "chat_id", None)
            or getattr(
                entity,
                "user_id",
                None,
            )
        )
        if entity_id is None and hasattr(entity, "id"):
            entity_id = entity.id
        return str(entity_id if entity_id is not None else peer_ref)

    def _validate_page_size(self, page_size: int) -> None:
        if page_size < 1 or page_size > self.settings.max_page_size:
            raise BadRequestError(f"page_size must be between 1 and {self.settings.max_page_size}")

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


T = TypeVar("T")
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GatewayResult(Generic[T]):
    data: T
    meta: ResponseMeta


class _ObservedBotStream(AsyncIterator[bytes]):
    """Preserve response ownership and report failures after headers were sent."""

    def __init__(self, stream: BotFileStream, record_error: Callable[[BotApiError], None]) -> None:
        self._stream = stream
        self._record_error = record_error

    def __aiter__(self) -> _ObservedBotStream:
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self._stream.__anext__()
        except BotApiError as exc:
            self._record_error(exc)
            raise

    async def aclose(self) -> None:
        await self._stream.aclose()


class TelegramGateway:
    """Capability router. Provider provenance belongs to each result, never global state."""

    def __init__(
        self,
        settings: Settings,
        *,
        telethon: TelethonGateway | None = None,
        bot: BotApiClient | None = None,
        store: BotUpdateStore | None = None,
    ) -> None:
        self.settings = settings
        self.telethon = telethon or TelethonGateway(settings)
        self.bot = bot
        self.store = store
        self._telethon_started = False
        self._bot_initialized = False
        self._store_initialized = False
        self._started = False
        self._cache_ready = False
        self._verified_connection_id: str | None = None
        self._identity_error = False
        self._bot_error: BotApiError | None = None
        self._last_error: str | None = None
        self._last_update_at: datetime | None = None
        self._retry_until = 0.0
        self._poll_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            if self.settings.telegram_bot_mode == "off":
                await self.telethon.start()
                self._telethon_started = self._started = True
                return
            self.bot = self.bot or BotApiClient(
                self.settings.telegram_bot_token.get_secret_value(),
                base_url=self.settings.telegram_bot_api_base_url,
                request_timeout=self.settings.telegram_bot_request_timeout,
            )
            assert self.settings.telegram_bot_owner_user_id is not None
            self.store = self.store or BotUpdateStore(
                self.settings.telegram_bot_store_path,
                self.settings.telegram_bot_store_key.get_secret_value(),
                self.settings.telegram_bot_owner_user_id,
                self.settings.telegram_bot_retention_days,
            )
            try:
                self.store.initialize()
                self.store.prepare_startup()
                self._store_initialized = True
            except Exception:
                self._last_error = "store-unavailable"
                logger.error("bot_startup_failed reason=store-unavailable")
            try:
                await self.telethon.start()
                self._telethon_started = True
                me = await self.telethon.get_me()
                self._identity_error = me.id != str(self.settings.telegram_bot_owner_user_id)
                if self._identity_error:
                    self._last_error = "owner-mismatch"
                    if self._store_initialized:
                        self.store.clear_messages()
            except Exception:
                self._telethon_started = False
                logger.warning("telethon_startup_failed")
            if self._store_initialized and not self._identity_error:
                try:
                    await self._initialize_bot()
                    await self._drain()
                    logger.info("bot_startup_succeeded")
                except BotApiError as exc:
                    self._record_error(exc)
                except Exception:
                    self._record_error(BotApiError("unavailable"))
                if self._last_error != "webhook-configured" and not (
                    self._bot_error and self._bot_error.reason == "auth-required"
                ):
                    self._poll_task = asyncio.create_task(self._poll_loop(), name="telegram-bot-poller")
            self._started = True

    async def _initialize_bot(self) -> None:
        assert self.bot is not None
        await self.bot.start()
        webhook = await self.bot.get_webhook_info()
        if webhook.get("url"):
            self._last_error = "webhook-configured"
            logger.error("bot_startup_failed reason=webhook-configured remove-webhook-manually")
            raise BotApiError("bad-request")
        self._bot_initialized = True

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._cache_ready = False
            self._verified_connection_id = None
            if self._poll_task is not None:
                self._poll_task.cancel()
                try:
                    await self._poll_task
                except asyncio.CancelledError:
                    pass
                self._poll_task = None
            try:
                if self.bot is not None:
                    await self.bot.close()
            finally:
                try:
                    await self.telethon.stop()
                finally:
                    if self.store is not None:
                        self.store.close()
                    self._started = self._telethon_started = self._bot_initialized = False
                    self._store_initialized = False

    async def _verify_connection(self) -> None:
        assert self.store is not None and self.bot is not None
        self._verified_connection_id = None
        connection = self.store.get_active_connection()
        if connection is None:
            return
        current = await self.bot.get_business_connection(str(connection["id"]))
        if current["user"]["id"] != self.settings.telegram_bot_owner_user_id:
            self.store.clear_messages()
            self._identity_error = True
            self._last_error = "owner-mismatch"
            return
        if self.store.revalidate_connection(current):
            self._verified_connection_id = str(current["id"])

    async def _poll_once(self, timeout: int) -> bool:
        assert self.bot is not None and self.store is not None
        if self.store.prepare_poll(timeout):
            self._cache_ready = False
            self._verified_connection_id = None
            timeout = 0
        updates = await self.bot.get_updates(self.store.next_update_offset(), timeout)
        # Cache lookups cannot interleave with this synchronous SQLite transaction.
        self.store.ingest_updates(updates)
        self.store.record_poll_success()
        self._last_update_at = datetime.now(UTC)
        if updates:
            self._cache_ready = False
            logger.debug("bot_cache_batch count=%d offset=%s", len(updates), self.store.next_update_offset())
        return bool(updates)

    async def _drain(self) -> None:
        self._cache_ready = False
        assert self.store is not None
        self.store.prepare_startup()
        while await self._poll_once(0):
            pass
        await self._verify_connection()
        if self._bot_error is not None and (
            self._bot_error.reason == "auth-required"
            or (self._bot_error.reason == "rate-limited" and self._retry_until > asyncio.get_running_loop().time())
        ):
            return
        self._cache_ready = not self._identity_error
        if not self._identity_error:
            self._bot_error = None
            self._last_error = None

    def _record_error(self, error: BotApiError) -> None:
        self._cache_ready = False
        # Auth failures remain terminal until restart; another concurrent request
        # must not replace them with an otherwise recoverable error.
        if self._bot_error is not None and self._bot_error.reason == "auth-required":
            return
        if (
            self._bot_error is not None
            and self._bot_error.reason == "rate-limited"
            and error.reason not in {"auth-required", "rate-limited"}
            and self._retry_until > asyncio.get_running_loop().time()
        ):
            return
        self._bot_error = error
        if self._last_error != "webhook-configured":
            self._last_error = error.reason
        if error.reason == "rate-limited":
            self._retry_until = max(self._retry_until, asyncio.get_running_loop().time() + (error.retry_after or 0))
        logger.warning("bot_provider_failed reason=%s", error.reason)

    async def _poll_loop(self) -> None:
        delay = 1
        while True:
            if self._identity_error:
                return
            if self._bot_error is not None and self._bot_error.reason == "auth-required":
                return
            remaining = self._retry_until - asyncio.get_running_loop().time()
            if remaining > 0:
                await asyncio.sleep(remaining)
            try:
                if not self._bot_initialized:
                    await self._initialize_bot()
                if not self._cache_ready:
                    await self._drain()
                else:
                    changed = await self._poll_once(self.settings.telegram_bot_poll_timeout)
                    if changed:
                        await self._drain()
                delay = 1
                # Also yields for deterministic fake transports and empty polls.
                await asyncio.sleep(0)
            except BotApiError as exc:
                self._record_error(exc)
                if exc.reason == "auth-required" or self._last_error == "webhook-configured":
                    return
                pause = exc.retry_after if exc.reason == "rate-limited" else delay
                logger.warning("bot_poller_retry reason=%s delay=%s", exc.reason, pause)
                await asyncio.sleep(pause or 0)
                delay = min(delay * 2, 30)
            except Exception:
                self._record_error(BotApiError("unavailable"))
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    def _telethon(self) -> TelethonGateway:
        if not self._telethon_started or not self.telethon.is_connected():
            raise TelegramProxyError(
                title="Telegram capability unavailable",
                detail="This operation requires a healthy Telethon session.",
                status_code=503,
                type="https://telegram-proxy/errors/telegram-capability-unavailable",
            )
        return self.telethon

    def _bot_available(self) -> bool:
        if self.settings.telegram_bot_mode != "primary":
            return False
        if self._last_error == "webhook-configured":
            raise TelegramProxyError(
                title="Telegram bot configuration required",
                detail="Remove the configured bot webhook manually before using the polling service.",
                status_code=503,
                type="https://telegram-proxy/errors/telegram-bot-configuration",
            )
        if self._identity_error:
            raise TelegramProxyError(
                title="Telegram owner mismatch",
                detail="Bot and Telethon owner identities do not match.",
                status_code=503,
                type="https://telegram-proxy/errors/telegram-bot-owner-mismatch",
            )
        if self._bot_error is not None:
            if self._bot_error.reason == "auth-required":
                raise self._bot_error
            if self._bot_error.reason == "rate-limited":
                remaining = self._retry_until - asyncio.get_running_loop().time()
                if remaining > 0:
                    raise BotApiError("rate-limited", retry_after=math.ceil(remaining))
            if not self._bot_error.fallback_allowed and self._bot_error.reason != "rate-limited":
                raise self._bot_error
        return self._bot_initialized and self._cache_ready and self._verified_connection_id is not None

    @staticmethod
    def _result(
        data: T,
        *,
        provider: Literal["bot_api", "telethon"] = "telethon",
        fallback: bool = False,
        meta: ResponseMeta | None = None,
    ) -> GatewayResult[T]:
        return GatewayResult(
            data, (meta or ResponseMeta()).model_copy(update={"provider": provider, "fallback_used": fallback})
        )

    @staticmethod
    def _bot_chat_ref(reference: str) -> bool:
        return bool(re.fullmatch(r"-\d+|@[A-Za-z0-9_]+", reference))

    @staticmethod
    def _private_ref(reference: str) -> bool:
        return bool(re.fullmatch(r"[1-9]\d*", reference))

    @staticmethod
    def _fallback(method: str, reason: str) -> None:
        logger.info("telegram_provider_fallback method=%s reason=%s", method, reason)

    def health_status(self) -> HealthStatus:
        telethon_healthy = self._telethon_started and self.telethon.is_connected()
        enabled = self.settings.telegram_bot_mode != "off"
        bot_healthy = (
            enabled
            and self._bot_initialized
            and self._cache_ready
            and self._verified_connection_id is not None
            and self._bot_error is None
            and not self._identity_error
        )
        return HealthStatus(
            status="ok" if telethon_healthy and (not enabled or bot_healthy) else "degraded",
            telegram_connected=telethon_healthy or bot_healthy,
            session_file=str(self.settings.session_file_path),
            primary_provider="bot_api" if self.settings.telegram_bot_mode == "primary" else "telethon",
            bot_api=BotHealth(
                mode=self.settings.telegram_bot_mode,
                healthy=bot_healthy,
                poller_running=self._poll_task is not None and not self._poll_task.done(),
                secretary_connection_verified=self._verified_connection_id is not None and not self._identity_error,
                last_update_at=self._last_update_at,
                last_error=self._last_error,
            ),
            telethon=TelethonHealth(healthy=telethon_healthy),
        )

    async def get_me(self) -> GatewayResult[ContactSummary]:
        return self._result(await self._telethon().get_me())

    async def list_contacts(self, *, page: int, page_size: int) -> GatewayResult[list[ContactSummary]]:
        data, meta = await self._telethon().list_contacts(page=page, page_size=page_size)
        return self._result(data, meta=meta)

    async def list_chats(self, *, page: int, page_size: int) -> GatewayResult[list[ChatSummary]]:
        data, meta = await self._telethon().list_chats(page=page, page_size=page_size)
        return self._result(data, meta=meta)

    async def get_chat(self, chat_ref: str) -> GatewayResult[ChatDetail]:
        eligible = self.settings.telegram_bot_mode == "primary" and self._bot_chat_ref(chat_ref)
        if eligible and self._bot_available():
            assert self.bot is not None
            try:
                chat = serialize_bot_chat(await self.bot.get_chat(chat_ref))
                if self._bot_available() and chat.type != "private":
                    return self._result(chat, provider="bot_api")
            except BotApiError as exc:
                self._record_error(exc)
                if not exc.fallback_allowed:
                    raise
        if eligible:
            self._bot_available()  # A concurrent terminal error must also block this request's fallback.
            self._fallback("get_chat", self._last_error or "not-ready")
        return self._result(await self._telethon().get_chat(chat_ref), fallback=eligible)

    async def resolve(self, peer_ref: str) -> GatewayResult[ResolveResult]:
        eligible = self.settings.telegram_bot_mode == "primary" and self._bot_chat_ref(peer_ref)
        if eligible and self._bot_available():
            assert self.bot is not None
            try:
                chat = serialize_bot_chat(await self.bot.get_chat(peer_ref))
                # getChat for a private username is the bot's own visibility,
                # not proof of the personal account's private peer identity.
                if self._bot_available() and chat.type != "private":
                    return self._result(
                        ResolveResult(
                            input=peer_ref,
                            peer=PeerRef(id=chat.id, display_name=chat.title, username=chat.username, type=chat.type),
                        ),
                        provider="bot_api",
                    )
            except BotApiError as exc:
                self._record_error(exc)
                if not exc.fallback_allowed:
                    raise
        if eligible:
            self._bot_available()
            self._fallback("resolve", self._last_error or "not-ready")
        return self._result(await self._telethon().resolve(peer_ref), fallback=eligible)

    async def list_messages(self, *, chat_ref: str, page: int, page_size: int) -> GatewayResult[list[MessageSummary]]:
        data, meta = await self._telethon().list_messages(chat_ref=chat_ref, page=page, page_size=page_size)
        return self._result(data, meta=meta)

    def _cached_message(self, chat_ref: str, message_id: int) -> dict[str, Any] | None:
        if message_id <= 0:
            raise BadRequestError("message_id must be positive")
        if not self._bot_available():
            return None
        assert self.store is not None and self._verified_connection_id is not None
        try:
            return self.store.get_message(self._verified_connection_id, int(chat_ref), message_id)
        except Exception:
            self._record_error(BotApiError("invalid-response"))
            return None

    async def get_message(self, *, chat_ref: str, message_id: int) -> GatewayResult[MessageDetail]:
        eligible = self.settings.telegram_bot_mode == "primary" and self._private_ref(chat_ref)
        if eligible:
            raw = self._cached_message(chat_ref, message_id)
            if raw is not None:
                try:
                    return self._result(serialize_bot_message(raw), provider="bot_api")
                except BotApiError as exc:
                    self._record_error(exc)
                    if not exc.fallback_allowed:
                        raise
            self._fallback("get_message", self._last_error or "cache-miss")
        return self._result(
            await self._telethon().get_message(chat_ref=chat_ref, message_id=message_id), fallback=eligible
        )

    async def get_message_context(
        self, *, chat_ref: str, message_id: int, context_size: int
    ) -> GatewayResult[MessageContext]:
        data = await self._telethon().get_message_context(
            chat_ref=chat_ref, message_id=message_id, context_size=context_size
        )
        return self._result(data, meta=ResponseMeta(context_size=context_size))

    async def search_messages(
        self, *, query: str, chat_ref: str | None, page: int, page_size: int
    ) -> GatewayResult[list[MessageSummary]]:
        data, meta = await self._telethon().search_messages(
            query=query, chat_ref=chat_ref, page=page, page_size=page_size
        )
        return self._result(data, meta=meta)

    async def get_message_media(
        self, *, chat_ref: str, message_id: int, include_album: bool
    ) -> GatewayResult[list[MediaItem]]:
        eligible = self.settings.telegram_bot_mode == "primary" and self._private_ref(chat_ref) and not include_album
        if eligible:
            result = await self.get_message(chat_ref=chat_ref, message_id=message_id)
            return self._result(
                result.data.media,
                provider=result.meta.provider or "telethon",
                fallback=bool(result.meta.fallback_used),
                meta=ResponseMeta(include_album=False),
            )
        data, meta = await self._telethon().get_message_media(
            chat_ref=chat_ref, message_id=message_id, include_album=include_album
        )
        return self._result(data, meta=meta)

    async def stream_message_media(self, *, chat_ref: str, message_id: int, media_id: str) -> MediaStream:
        eligible = (
            self.settings.telegram_bot_mode == "primary"
            and self._private_ref(chat_ref)
            and media_id == f"{message_id}:0"
        )
        if eligible:
            raw = self._cached_message(chat_ref, message_id)
            if raw is not None:
                assert self.bot is not None
                try:
                    message = serialize_bot_message(raw)
                    media = bot_media_object(raw)
                    if media is not None and message.media:
                        item = message.media[0]
                        hosted = self.settings.telegram_bot_api_base_url.rstrip("/") == "https://api.telegram.org"
                        if not hosted or item.size_bytes is None or item.size_bytes <= 20 * 1024 * 1024:
                            file = await self.bot.get_file(str(media["file_id"]))
                            if self._cached_message(chat_ref, message_id) == raw:
                                chunks = await self.bot.open_file(str(file["file_path"]))
                                accepted = False
                                try:
                                    if self._cached_message(chat_ref, message_id) == raw:
                                        accepted = True
                                        return MediaStream(
                                            file_name=item.file_name or f"telegram-{message_id}",
                                            content_type=item.mime_type or "application/octet-stream",
                                            size_bytes=item.size_bytes,
                                            chunks=_ObservedBotStream(chunks, self._record_error),
                                            provider="bot_api",
                                        )
                                finally:
                                    if not accepted:
                                        await chunks.aclose()
                except BotApiError as exc:
                    self._record_error(exc)
                    if not exc.fallback_allowed:
                        raise
            self._bot_available()
            self._fallback("stream_message_media", self._last_error or "cache-miss-or-file-limit")
        stream = await self._telethon().stream_message_media(
            chat_ref=chat_ref, message_id=message_id, media_id=media_id
        )
        stream.fallback_used = eligible
        return stream

    async def bundle_message_media(self, *, chat_ref: str, message_id: int, include_album: bool) -> MediaBundle:
        return await self._telethon().bundle_message_media(
            chat_ref=chat_ref, message_id=message_id, include_album=include_album
        )
