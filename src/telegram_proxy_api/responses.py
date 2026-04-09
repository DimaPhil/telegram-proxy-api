from __future__ import annotations

from .models import ApiResponse, ResponseMeta


def api_response(data: object, *, meta: ResponseMeta | None = None) -> ApiResponse[object]:
    return ApiResponse(data=data, meta=meta or ResponseMeta())
