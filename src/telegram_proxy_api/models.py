from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ProblemDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str = "about:blank"
    title: str
    status: int
    detail: str


class ResponseMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page: int | None = None
    page_size: int | None = None
    total: int | None = None
    context_size: int | None = None
    include_album: bool | None = None


class ApiResponse(BaseModel, Generic[T]):
    data: T
    meta: ResponseMeta = Field(default_factory=ResponseMeta)


class PeerRef(BaseModel):
    id: str
    display_name: str | None = None
    username: str | None = None
    type: str | None = None


class ForwardInfo(BaseModel):
    date: datetime | None = None
    imported: bool = False
    saved_out: bool = False
    from_peer: PeerRef | None = None
    from_name: str | None = None
    channel_post: str | None = None
    post_author: str | None = None
    saved_from_peer: PeerRef | None = None
    saved_from_message_id: str | None = None
    saved_from_name: str | None = None
    saved_date: datetime | None = None
    psa_type: str | None = None
    telegram_raw: dict[str, Any] = Field(default_factory=dict)


class ChatSummary(BaseModel):
    id: str
    title: str
    username: str | None = None
    type: str
    unread_count: int = 0
    archived: bool = False
    last_message_at: datetime | None = None
    telegram_raw: dict[str, Any] = Field(default_factory=dict)


class ChatDetail(ChatSummary):
    participant_count: int | None = None
    about: str | None = None


class ContactSummary(BaseModel):
    id: str
    display_name: str
    username: str | None = None
    phone: str | None = None
    mutual_contact: bool | None = None
    bot: bool = False
    telegram_raw: dict[str, Any] = Field(default_factory=dict)


class MediaItem(BaseModel):
    media_id: str
    message_id: str
    grouped_id: str | None = None
    kind: str
    mime_type: str | None = None
    file_name: str | None = None
    extension: str | None = None
    size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: int | None = None
    access_path: str
    telegram_raw: dict[str, Any] = Field(default_factory=dict)


class MessageSummary(BaseModel):
    id: str
    chat_id: str
    date: datetime | None = None
    edit_date: datetime | None = None
    text: str | None = None
    sender: PeerRef | None = None
    is_forwarded: bool = False
    forward_info: ForwardInfo | None = None
    reply_to_message_id: str | None = None
    grouped_id: str | None = None
    views: int | None = None
    forwards: int | None = None
    has_media: bool = False
    media_count: int = 0
    telegram_raw: dict[str, Any] = Field(default_factory=dict)


class MessageDetail(MessageSummary):
    media: list[MediaItem] = Field(default_factory=list)


class MessageContext(BaseModel):
    before: list[MessageSummary]
    message: MessageDetail
    after: list[MessageSummary]


class ResolveResult(BaseModel):
    input: str
    peer: PeerRef


class HealthStatus(BaseModel):
    status: str
    telegram_connected: bool
    session_file: str
