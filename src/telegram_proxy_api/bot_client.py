"""Small, token-safe HTTP transport for Telegram's Bot API."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx

from .errors import TelegramProxyError

ALLOWED_UPDATES = [
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
]


class _TokenRedactor(logging.Filter):
    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = record.getMessage().replace(self._token, "[REDACTED]")
        record.args = ()
        return True


class BotApiError(TelegramProxyError):
    def __init__(
        self,
        reason: str,
        *,
        retry_after: int | None = None,
        migrate_to_chat_id: int | None = None,
    ) -> None:
        self.reason = reason
        self.retry_after = retry_after
        self.migrate_to_chat_id = migrate_to_chat_id
        self.fallback_allowed = reason in {"unavailable", "invalid-response", "not-found", "file-too-large"}
        status = {"auth-required": 503, "rate-limited": 429, "not-found": 404, "bad-request": 400}.get(reason, 502)
        super().__init__(
            title="Telegram Bot API error",
            detail={
                "auth-required": "The Telegram bot token is invalid or revoked.",
                "rate-limited": "Telegram requested a cooldown.",
                "not-found": "The bot cannot access this resource.",
                "bad-request": "Telegram rejected the bot request.",
                "file-too-large": "The file exceeds the Bot API download limit.",
                "invalid-response": "Telegram returned an invalid Bot API response.",
            }.get(reason, "The Telegram Bot API is unavailable."),
            status_code=status,
            type=f"https://telegram-proxy/errors/telegram-bot-{reason}",
            headers={"Retry-After": str(retry_after)} if retry_after is not None else {},
        )


class BotFileStream(AsyncIterator[bytes]):
    """Own a preflighted response even before the first byte is requested."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self._chunks = response.aiter_bytes()
        self._closed = False

    def __aiter__(self) -> BotFileStream:
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await self._chunks.__anext__()
        except httpx.HTTPError:
            await self.aclose()
            raise BotApiError("unavailable") from None
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                await self._response.aclose()
            except httpx.HTTPError:
                raise BotApiError("unavailable") from None


class BotApiClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        request_timeout: float = 10,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or any(ord(character) < 32 or ord(character) == 127 for character in base_url)
        ):
            raise ValueError("Invalid Bot API base URL")
        if (
            not token
            or any(character in token for character in "/?#\\")
            or any(ord(character) < 33 or ord(character) == 127 for character in token)
        ):
            raise ValueError("Invalid Bot API token")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = request_timeout
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._migrations: dict[str, str] = {}
        self._redactor = _TokenRedactor(token)
        self._loggers = [
            logging.getLogger(name)
            for name in (
                "httpx",
                "httpcore.connection",
                "httpcore.http11",
                "httpcore.http2",
                "httpcore.proxy",
                "httpcore.socks",
            )
        ]
        for logger in self._loggers:
            logger.addFilter(self._redactor)

    def _url(self, suffix: str, *, file: bool = False) -> str:
        return f"{self._base_url}/{'file/' if file else ''}bot{self._token}/{suffix}"

    async def close(self) -> None:
        await self._client.aclose()
        for logger in self._loggers:
            logger.removeFilter(self._redactor)

    async def start(self) -> dict[str, Any]:
        return await self.get_me()

    async def _call(self, method: str, payload: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        try:
            response = await self._client.post(self._url(method), json=payload or {}, timeout=timeout or self._timeout)
        except httpx.HTTPError:
            raise BotApiError("unavailable") from None
        if response.status_code == 401:
            raise BotApiError("auth-required")
        if response.status_code >= 500:
            raise BotApiError("unavailable")
        if response.status_code == 429:
            try:
                limit_envelope = response.json()
                retry = limit_envelope.get("parameters", {}).get("retry_after")
            except (ValueError, AttributeError, UnicodeError):
                retry = None
            if not isinstance(retry, int) or isinstance(retry, bool) or retry < 0:
                retry = 30
            raise BotApiError("rate-limited", retry_after=retry)
        try:
            envelope = response.json()
        except (ValueError, UnicodeError):
            raise BotApiError("invalid-response") from None
        if not isinstance(envelope, dict) or not isinstance(envelope.get("ok"), bool):
            raise BotApiError("invalid-response")
        if envelope["ok"]:
            if not response.is_success or "result" not in envelope:
                raise BotApiError("invalid-response")
            return envelope["result"]
        code = envelope.get("error_code", response.status_code)
        parameters = envelope.get("parameters", {})
        if not isinstance(parameters, dict):
            raise BotApiError("invalid-response")
        if code == 401:
            raise BotApiError("auth-required")
        if code == 429 or response.status_code == 429:
            retry = parameters.get("retry_after")
            if not isinstance(retry, int) or isinstance(retry, bool) or retry < 0:
                # Never turn malformed rate limits into fallback eligibility.
                retry = 30
            raise BotApiError("rate-limited", retry_after=retry)
        if isinstance(code, int) and code >= 500:
            raise BotApiError("unavailable")
        migration = parameters.get("migrate_to_chat_id")
        if method == "getChat" and isinstance(migration, int) and not isinstance(migration, bool):
            raise BotApiError("bad-request", migrate_to_chat_id=migration)
        description = str(envelope.get("description", "")).lower()
        if method == "getFile" and "file is too big" in description:
            raise BotApiError("file-too-large")
        if code == 403 or (
            code == 400
            and any(text in description for text in ("chat not found", "bot is not a member", "bot was kicked"))
        ):
            raise BotApiError("not-found")
        raise BotApiError("bad-request")

    async def _object(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        result = await self._call(method, payload)
        if not isinstance(result, dict):
            raise BotApiError("invalid-response")
        return result

    async def get_me(self) -> dict[str, Any]:
        result = await self._object("getMe")
        if not isinstance(result.get("id"), int) or result.get("is_bot") is not True:
            raise BotApiError("invalid-response")
        return result

    async def get_chat(self, chat_ref: str) -> dict[str, Any]:
        reference = self._migrations.get(chat_ref, chat_ref)
        try:
            result = await self._object("getChat", {"chat_id": reference})
        except BotApiError as exc:
            if exc.migrate_to_chat_id is None:
                raise
            reference = str(exc.migrate_to_chat_id)
            result = await self._object("getChat", {"chat_id": reference})
            self._migrations[chat_ref] = reference
        if not isinstance(result.get("id"), int) or result.get("type") not in {
            "private",
            "group",
            "supergroup",
            "channel",
        }:
            raise BotApiError("invalid-response")
        return result

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "limit": 100, "allowed_updates": ALLOWED_UPDATES}
        if offset is not None:
            payload["offset"] = offset
        result = await self._call("getUpdates", payload, timeout=timeout + 5)
        if not isinstance(result, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("update_id"), int) for item in result
        ):
            raise BotApiError("invalid-response")
        return result

    async def get_webhook_info(self) -> dict[str, Any]:
        result = await self._object("getWebhookInfo")
        if not isinstance(result.get("url"), str):
            raise BotApiError("invalid-response")
        return result

    async def get_business_connection(self, connection_id: str) -> dict[str, Any]:
        result = await self._object("getBusinessConnection", {"business_connection_id": connection_id})
        if (
            result.get("id") != connection_id
            or not isinstance(result.get("user"), dict)
            or not isinstance(result["user"].get("id"), int)
            or not isinstance(result.get("is_enabled"), bool)
        ):
            raise BotApiError("invalid-response")
        return result

    async def get_file(self, file_id: str) -> dict[str, Any]:
        result = await self._object("getFile", {"file_id": file_id})
        if not isinstance(result.get("file_path"), str) or not result["file_path"]:
            raise BotApiError("invalid-response")
        return result

    async def open_file(self, file_path: str) -> BotFileStream:
        # Accept only Telegram's relative file path, never arbitrary URLs or
        # traversal paths. Redirects must never forward the bot token.
        if (
            not file_path
            or file_path.startswith("/")
            or any(part in {"..", "."} for part in file_path.split("/"))
            or any(c in file_path for c in ("?", "#", "\\", ":", "%"))
            or any(ord(character) < 32 or ord(character) == 127 for character in file_path)
        ):
            raise BotApiError("invalid-response")
        try:
            request = self._client.build_request("GET", self._url(file_path, file=True), timeout=self._timeout)
            response = await self._client.send(request, stream=True, follow_redirects=False)
        except httpx.HTTPError:
            raise BotApiError("unavailable") from None
        if not response.is_success:
            await response.aclose()
            if response.status_code == 401:
                raise BotApiError("auth-required")
            if response.status_code == 429:
                retry_header = response.headers.get("Retry-After", "30")
                retry = int(retry_header) if retry_header.isdecimal() else 30
                raise BotApiError("rate-limited", retry_after=retry)
            raise BotApiError("unavailable")

        return BotFileStream(response)

    async def stream_file(self, file_path: str) -> AsyncIterator[bytes]:
        chunks = await self.open_file(file_path)
        try:
            async for chunk in chunks:
                yield chunk
        finally:
            await chunks.aclose()
