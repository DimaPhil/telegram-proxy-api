from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from telethon import events
from telethon.tl import types

from telegram_proxy_api.models import UpdateMessageEvent
from telegram_proxy_api.updates import PING_FRAME, READY_FRAME, UpdateHub, message_event, sse_frames

CHANNEL = "-1001234567890"
OTHER = "-1009876543210"
PRIVATE = "555"


def event(chat_id: str = CHANNEL, message_id: int = 1) -> UpdateMessageEvent:
    return UpdateMessageEvent(chat_id=chat_id, message_id=message_id, date=datetime(2026, 1, 1, tzinfo=UTC))


def channel_message(message_id: int = 7, *, grouped_id: int | None = None, date: datetime | None = None):
    return types.Message(
        id=message_id,
        peer_id=types.PeerChannel(1234567890),
        date=date or datetime(2026, 11, 6, 12, 0, tzinfo=UTC),
        message="post",
        grouped_id=grouped_id,
    )


def test_message_event_uses_marked_ids_and_utc():
    built = message_event(
        channel_message(grouped_id=123, date=datetime(2026, 11, 6, 15, 0, tzinfo=timezone(timedelta(hours=3))))
    )
    assert built is not None
    assert json.loads(built.model_dump_json()) == {
        "chat_id": CHANNEL,
        "message_id": 7,
        "grouped_id": "123",
        "date": "2026-11-06T12:00:00Z",
    }
    group = message_event(types.Message(id=1, peer_id=types.PeerChat(42), date=datetime(2026, 1, 1), message=""))
    assert group is not None and group.chat_id == "-42" and group.date.tzinfo is not None
    assert (
        message_event(types.Message(id=1, peer_id=types.PeerUser(555), date=datetime(2026, 1, 1), message="")).chat_id
        == PRIVATE
    )


def test_message_event_ignores_service_empty_and_dateless_messages():
    service = types.MessageService(
        id=2,
        peer_id=types.PeerChannel(1),
        date=datetime(2026, 1, 1, tzinfo=UTC),
        action=types.MessageActionPinMessage(),
    )
    assert message_event(service) is None
    assert message_event(types.MessageEmpty(id=3, peer_id=types.PeerChannel(1))) is None
    assert message_event(types.Message(id=4, peer_id=types.PeerChannel(1), date=None, message="")) is None
    assert message_event(None) is None


def test_new_message_builder_accepts_new_posts_but_not_edits():
    post = channel_message()
    new = events.NewMessage.build(types.UpdateNewChannelMessage(message=post, pts=1, pts_count=1))
    edit = events.NewMessage.build(types.UpdateEditChannelMessage(message=post, pts=2, pts_count=1))
    deleted = events.NewMessage.build(
        types.UpdateDeleteChannelMessages(channel_id=1234567890, messages=[7], pts=3, pts_count=1)
    )

    assert new is not None and message_event(new.message) == message_event(post)
    assert edit is None and deleted is None


@pytest.mark.asyncio
async def test_hub_delivers_only_requested_chats_to_each_subscriber():
    hub = UpdateHub(queue_limit=10)
    with hub.subscribe(frozenset({CHANNEL})) as first, hub.subscribe(frozenset({OTHER, CHANNEL})) as second:
        hub.publish(event(CHANNEL, 1))
        hub.publish(event(OTHER, 2))
        hub.publish(event(PRIVATE, 3))

        assert [(await first.next(0.01)).message_id, await first.next(0.01)] == [1, None]
        assert [(await second.next(0.01)).message_id, (await second.next(0.01)).message_id] == [1, 2]
        assert await second.next(0.01) is None
        assert hub.subscriber_count == 2
    assert hub.subscriber_count == 0
    assert first.closed_reason == second.closed_reason == "disconnected"


@pytest.mark.asyncio
async def test_overflow_drops_the_backlog_closes_only_that_subscriber_and_stops_its_stream():
    hub = UpdateHub(queue_limit=2)
    with hub.subscribe(frozenset({CHANNEL})) as slow, hub.subscribe(frozenset({CHANNEL})) as fast:
        hub.publish(event(message_id=1))
        assert (await fast.next(0.01)).message_id == 1
        hub.publish(event(message_id=2))
        assert (await fast.next(0.01)).message_id == 2
        hub.publish(event(message_id=3))

        assert slow.closed_reason == "overflow"
        assert slow.pending == 0
        assert hub.subscriber_count == 1
        with pytest.raises(StopAsyncIteration):
            await slow.next(0.01)
        assert (await fast.next(0.01)).message_id == 3


@pytest.mark.asyncio
async def test_close_wakes_a_waiting_subscriber_after_draining():
    hub = UpdateHub(queue_limit=10)
    with hub.subscribe(frozenset({CHANNEL})) as subscription:
        waiter = asyncio.create_task(subscription.next(30))
        await asyncio.sleep(0)
        hub.publish(event(message_id=5))
        assert (await asyncio.wait_for(waiter, 1)).message_id == 5

        hub.publish(event(message_id=6))
        hub.close_all()
        assert (await subscription.next(30)).message_id == 6
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(subscription.next(30), 1)
        assert hub.subscriber_count == 0


@pytest.mark.asyncio
async def test_sse_frames_subscribe_lazily_then_emit_ready_messages_and_pings():
    hub = UpdateHub(queue_limit=10)
    frames = sse_frames(hub, frozenset({CHANNEL}), ping_interval=0.01)
    assert hub.subscriber_count == 0

    assert await anext(frames) == READY_FRAME
    assert hub.subscriber_count == 1
    assert await anext(frames) == PING_FRAME
    hub.publish(event(OTHER, 1))
    hub.publish(event(CHANNEL, 2))
    frame = await anext(frames)
    name, data = frame.decode().removesuffix("\n\n").split("\n")
    assert name == "event: message"
    assert json.loads(data.removeprefix("data: "))["message_id"] == 2

    await frames.aclose()
    assert hub.subscriber_count == 0


@pytest.mark.asyncio
async def test_sse_frames_end_when_the_subscription_overflows():
    hub = UpdateHub(queue_limit=1)
    frames = sse_frames(hub, frozenset({CHANNEL}), ping_interval=30)
    assert await anext(frames) == READY_FRAME
    hub.publish(event(message_id=1))
    hub.publish(event(message_id=2))

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(frames), 1)
    assert hub.subscriber_count == 0
