from __future__ import annotations

from typing import TypeVar

from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from .gateway import GatewayResult
from .models import ApiResponse, ResponseMeta

T = TypeVar("T")


class ClosingStreamingResponse(StreamingResponse):
    """Release an upstream response even if ASGI disconnects before the first byte."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                await close()


def api_response(data: T | GatewayResult[T], *, meta: ResponseMeta | None = None) -> ApiResponse[T]:
    if isinstance(data, GatewayResult):
        return ApiResponse(data=data.data, meta=data.meta)
    return ApiResponse(data=data, meta=meta or ResponseMeta())
