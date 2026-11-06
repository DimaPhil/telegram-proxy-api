"""Fan out passively received Telegram updates to server-sent event subscribers."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC
from typing import Any

from telethon import utils
from telethon.tl import types

from .models import UpdateMessageEvent

logger = logging.getLogger(__name__)

READY_FRAME = b"event: ready\ndata: {}\n\n"
PING_FRAME = b": ping\n\n"


def message_event(message: Any) -> UpdateMessageEvent | None:
    """Build the public event for a new regular message; service and empty messages are ignored."""
    if not isinstance(message, types.Message) or message.peer_id is None or message.date is None:
        return None
    date = message.date if message.date.tzinfo is not None else message.date.replace(tzinfo=UTC)
    return UpdateMessageEvent(
        chat_id=str(utils.get_peer_id(message.peer_id)),
        message_id=message.id,
        grouped_id=str(message.grouped_id) if message.grouped_id else None,
        date=date.astimezone(UTC),
    )


class Subscription:
    """One stream's bounded backlog. Overflow drops the backlog and ends the stream."""

    def __init__(self, chat_ids: frozenset[str], limit: int) -> None:
        self.chat_ids = chat_ids
        self._limit = limit
        self._items: deque[UpdateMessageEvent] = deque()
        self._signal = asyncio.Event()
        self.closed_reason: str | None = None

    @property
    def closed(self) -> bool:
        return self.closed_reason is not None

    @property
    def pending(self) -> int:
        return len(self._items)

    def offer(self, event: UpdateMessageEvent) -> None:
        if self.closed:
            return
        if len(self._items) >= self._limit:
            # The client re-syncs on reconnect, so stale events are not worth the memory.
            self._items.clear()
            self.close("overflow")
            logger.warning("updates_subscriber_overflow limit=%d", self._limit)
            return
        self._items.append(event)
        self._signal.set()

    def close(self, reason: str) -> None:
        if self.closed_reason is None:
            self.closed_reason = reason
        self._signal.set()

    async def next(self, timeout: float) -> UpdateMessageEvent | None:
        """Return the next event, None after `timeout` idle seconds; raise StopAsyncIteration once closed and drained."""
        while True:
            if self._items:
                return self._items.popleft()
            if self.closed:
                raise StopAsyncIteration
            self._signal.clear()
            try:
                await asyncio.wait_for(self._signal.wait(), timeout)
            except TimeoutError:
                return None


class UpdateHub:
    def __init__(self, queue_limit: int) -> None:
        self._queue_limit = queue_limit
        self._subscriptions: set[Subscription] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    def publish(self, event: UpdateMessageEvent) -> None:
        for subscription in list(self._subscriptions):
            if event.chat_id not in subscription.chat_ids:
                continue
            subscription.offer(event)
            if subscription.closed:
                self._subscriptions.discard(subscription)

    @contextmanager
    def subscribe(self, chat_ids: frozenset[str]) -> Iterator[Subscription]:
        subscription = Subscription(chat_ids, self._queue_limit)
        self._subscriptions.add(subscription)
        try:
            yield subscription
        finally:
            self._subscriptions.discard(subscription)
            subscription.close("disconnected")

    def close_all(self, reason: str = "shutdown") -> None:
        for subscription in list(self._subscriptions):
            subscription.close(reason)
        self._subscriptions.clear()


async def sse_frames(hub: UpdateHub, chat_ids: frozenset[str], ping_interval: float) -> AsyncIterator[bytes]:
    """Subscribe on first iteration, so an unstarted response never registers a subscriber."""
    with hub.subscribe(chat_ids) as subscription:
        yield READY_FRAME
        while True:
            try:
                event = await subscription.next(ping_interval)
            except StopAsyncIteration:
                return
            if event is None:
                yield PING_FRAME
            else:
                yield f"event: message\ndata: {event.model_dump_json()}\n\n".encode()
