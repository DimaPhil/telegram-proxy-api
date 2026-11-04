from __future__ import annotations

from secrets import compare_digest
from typing import Annotated, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings
from .errors import UnauthorizedError
from .gateway import TelegramGateway


def get_settings_from_request(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    return cast(Settings, settings) if settings is not None else get_settings()


def get_gateway(request: Request) -> TelegramGateway:
    return cast(TelegramGateway, request.app.state.gateway)


bearer_scheme = HTTPBearer(auto_error=False)


async def require_access_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
    settings: Settings = Depends(get_settings_from_request),
) -> None:
    if not settings.api_auth_enabled:
        return

    expected = settings.api_bearer_token
    if not expected:
        raise UnauthorizedError("API auth is enabled but no bearer token is configured.")
    if credentials is None or not compare_digest(credentials.credentials.encode(), expected.encode()):
        raise UnauthorizedError()
