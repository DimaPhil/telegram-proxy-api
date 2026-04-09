from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from .config import Settings, get_settings
from .errors import UnauthorizedError
from .gateway import TelegramGateway


def get_settings_from_request(request: Request) -> Settings:
    return getattr(request.app.state, "settings", get_settings())


def get_gateway(request: Request) -> TelegramGateway:
    return request.app.state.gateway


async def require_access_token(
    authorization: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings_from_request),
) -> None:
    if not settings.api_auth_enabled:
        return

    expected = settings.api_bearer_token
    if not expected:
        raise UnauthorizedError("API auth is enabled but no bearer token is configured.")
    if authorization != f"Bearer {expected}":
        raise UnauthorizedError()
