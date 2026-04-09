from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class TelegramProxyError(Exception):
    title: str
    detail: str
    status_code: int
    type: str = "about:blank"
    headers: dict[str, str] = field(default_factory=dict)


class AuthenticationRequiredError(TelegramProxyError):
    def __init__(self, detail: str = "Telegram session is not authenticated.") -> None:
        super().__init__(
            title="Telegram authentication required",
            detail=detail,
            status_code=503,
            type="https://telegram-proxy/errors/telegram-auth-required",
        )


class TelegramFloodWaitError(TelegramProxyError):
    def __init__(self, seconds: int) -> None:
        super().__init__(
            title="Telegram flood wait",
            detail=f"Telegram asked the client to wait for {seconds} seconds.",
            status_code=429,
            type="https://telegram-proxy/errors/flood-wait",
            headers={"Retry-After": str(seconds)},
        )


class ResourceNotFoundError(TelegramProxyError):
    def __init__(self, detail: str) -> None:
        super().__init__(
            title="Resource not found",
            detail=detail,
            status_code=404,
            type="https://telegram-proxy/errors/not-found",
        )


class BadRequestError(TelegramProxyError):
    def __init__(self, detail: str) -> None:
        super().__init__(
            title="Bad request",
            detail=detail,
            status_code=400,
            type="https://telegram-proxy/errors/bad-request",
        )


class UnauthorizedError(TelegramProxyError):
    def __init__(self, detail: str = "Bearer token is invalid or missing.") -> None:
        super().__init__(
            title="Unauthorized",
            detail=detail,
            status_code=401,
            type="https://telegram-proxy/errors/unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
