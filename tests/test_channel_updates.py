"""Channel join, batched latest-message checks and passive update wiring on TelethonGateway."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from telethon import errors as rpc_errors
from telethon import events, utils
from telethon.tl import types
from telethon.tl.functions.account import UpdateNotifySettingsRequest
from telethon.tl.functions.channels import GetChannelsRequest, JoinChannelRequest
from telethon.tl.functions.folders import EditPeerFoldersRequest
from telethon.tl.functions.messages import GetPeerDialogsRequest

from telegram_proxy_api import gateway as gateway_module
from telegram_proxy_api.errors import (
    BadRequestError,
    CapabilityUnavailableError,
    LocalRateLimitError,
    NotJoinableError,
    ResourceNotFoundError,
    TelegramFloodWaitError,
)
from telegram_proxy_api.gateway import TelethonGateway
from tests.test_gateway import FakeClient

CHANNEL = -1001234567890
OTHER = -1009876543210
THIRD = -1005555555555
GROUP = -4242
USER = 777


def input_peer(marked: int):
    peer = utils.get_peer(marked)
    if isinstance(peer, types.PeerChannel):
        return types.InputPeerChannel(peer.channel_id, 1000 + peer.channel_id % 1000)
    if isinstance(peer, types.PeerChat):
        return types.InputPeerChat(peer.chat_id)
    return types.InputPeerUser(peer.user_id, 5)


def channel(marked: int, *, left: bool = True, username: str | None = "public", **flags):
    return types.Channel(
        id=utils.resolve_id(marked)[0],
        title="Channel",
        photo=types.ChatPhotoEmpty(),
        date=None,
        left=left,
        broadcast=True,
        access_hash=1,
        username=username,
        **flags,
    )


def dialog(marked: int, top: int, *, folder_id: int | None = None, mute_until: datetime | None = None):
    return types.Dialog(
        peer=utils.get_peer(marked),
        top_message=top,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        notify_settings=types.PeerNotifySettings(mute_until=mute_until),
        folder_id=folder_id,
    )


class ChannelClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.requests: list[object] = []
        self.channels: dict[int, object] = {}
        self.tops: dict[int, int] = {}
        self.dialog_folders: dict[int, int] = {}
        self.dialog_mutes: dict[int, datetime] = {}
        self.left: set[int] = set()
        self.rejected: set[int] = set()
        self.failures: dict[type, Exception] = {}

    def know(self, *marked_ids: int) -> None:
        for marked in marked_ids:
            self.input_entities.append((utils.get_peer(marked), input_peer(marked)))

    def requests_of(self, kind):
        return [request for request in self.requests if isinstance(request, kind)]

    async def get_entity(self, lookup):
        try:
            return await super().get_entity(lookup)
        except KeyError:
            raise ValueError("not found") from None

    async def __call__(self, request):
        self.requests.append(request)
        failure = self.failures.get(type(request))
        if failure is not None:
            raise failure
        if isinstance(request, GetChannelsRequest):
            marked = utils.get_peer_id(types.PeerChannel(request.id[0].channel_id))
            return types.messages.Chats(chats=[self.channels[marked]])
        if isinstance(request, GetPeerDialogsRequest):
            marked_ids = [utils.get_peer_id(item.peer) for item in request.peers]
            if any(marked in self.rejected for marked in marked_ids):
                raise rpc_errors.ChannelPrivateError(request=None)
            present = [marked for marked in marked_ids if marked in self.tops]
            return types.messages.PeerDialogs(
                dialogs=[
                    dialog(
                        marked,
                        self.tops[marked],
                        folder_id=self.dialog_folders.get(marked),
                        mute_until=self.dialog_mutes.get(marked),
                    )
                    for marked in present
                ],
                messages=[],
                chats=[channel(marked, left=marked in self.left) for marked in present if marked < 0],
                users=[],
                state=types.updates.State(pts=1, qts=0, date=datetime.now(UTC), seq=1, unread_count=0),
            )
        return types.Updates(updates=[], users=[], chats=[], date=datetime.now(UTC), seq=0)


@pytest.fixture
def client():
    return ChannelClient()


@pytest.fixture
def gateway(settings, client):
    service = TelethonGateway(settings, client_factory=lambda *args, **kwargs: client)
    service._client = client
    service._started = True
    return service


@pytest.mark.asyncio
async def test_join_public_channel_then_mutes_and_archives(gateway, client):
    client.know(CHANNEL)
    client.channels[CHANNEL] = channel(CHANNEL)

    result = await gateway.join_chat(str(CHANNEL))

    assert result.model_dump() == {"chat_id": str(CHANNEL), "already_member": False, "muted": True, "archived": True}
    [join] = client.requests_of(JoinChannelRequest)
    assert join.channel.channel_id == 1234567890
    [mute] = client.requests_of(UpdateNotifySettingsRequest)
    assert mute.settings.mute_until == datetime.fromtimestamp(2**31 - 1, UTC)
    assert utils.get_peer_id(mute.peer.peer) == CHANNEL
    [archive] = client.requests_of(EditPeerFoldersRequest)
    assert archive.folder_peers[0].folder_id == 1
    assert client.remote_lookups == []


@pytest.mark.asyncio
async def test_join_is_idempotent_and_leaves_existing_membership_settings_alone(gateway, client):
    client.know(CHANNEL)
    client.channels[CHANNEL] = channel(CHANNEL, left=False)
    client.tops[CHANNEL] = 50
    client.dialog_folders[CHANNEL] = 1
    client.dialog_mutes[CHANNEL] = datetime.now(UTC) + timedelta(days=365)

    result = await gateway.join_chat(str(CHANNEL))

    assert result.model_dump() == {"chat_id": str(CHANNEL), "already_member": True, "muted": True, "archived": True}
    assert client.requests_of(JoinChannelRequest) == []
    assert client.requests_of(UpdateNotifySettingsRequest) == []
    assert client.requests_of(EditPeerFoldersRequest) == []


@pytest.mark.asyncio
async def test_join_race_with_already_participant_reports_membership(gateway, client):
    client.know(CHANNEL)
    client.channels[CHANNEL] = channel(CHANNEL)
    client.tops[CHANNEL] = 5
    client.failures[JoinChannelRequest] = rpc_errors.UserAlreadyParticipantError(request=None)

    result = await gateway.join_chat(str(CHANNEL))

    assert result.already_member is True
    assert (result.muted, result.archived) == (False, False)
    assert client.requests_of(UpdateNotifySettingsRequest) == []


@pytest.mark.asyncio
async def test_mute_and_archive_failures_do_not_fail_the_join(gateway, client):
    client.know(CHANNEL)
    client.channels[CHANNEL] = channel(CHANNEL)
    client.failures[UpdateNotifySettingsRequest] = rpc_errors.FloodWaitError(request=None, capture=30)
    client.failures[EditPeerFoldersRequest] = rpc_errors.RPCError(request=None, message="INTERNAL", code=500)

    result = await gateway.join_chat(str(CHANNEL))

    assert result.model_dump() == {"chat_id": str(CHANNEL), "already_member": False, "muted": False, "archived": False}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("marked", "error"),
    [(USER, "user"), (GROUP, "Basic groups")],
)
async def test_join_rejects_users_and_basic_groups_without_requests(gateway, client, marked, error):
    client.know(marked)

    with pytest.raises(BadRequestError, match=error):
        await gateway.join_chat(str(marked))
    assert client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        channel(CHANNEL, username=None),
        channel(CHANNEL, join_request=True),
        types.ChannelForbidden(id=1234567890, access_hash=1, title="Gone"),
    ],
)
async def test_join_refuses_private_approval_and_forbidden_channels(gateway, client, state):
    client.know(CHANNEL)
    client.channels[CHANNEL] = state

    with pytest.raises(NotJoinableError) as raised:
        await gateway.join_chat(str(CHANNEL))
    assert raised.value.status_code == 403
    assert client.requests_of(JoinChannelRequest) == []


@pytest.mark.asyncio
async def test_join_accepts_collectible_usernames_as_public(gateway, client):
    client.know(CHANNEL)
    client.channels[CHANNEL] = channel(CHANNEL, username=None, usernames=[types.Username(username="nft", active=True)])

    assert (await gateway.join_chat(str(CHANNEL))).already_member is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (rpc_errors.ChannelPrivateError(request=None), NotJoinableError),
        (rpc_errors.InviteRequestSentError(request=None), NotJoinableError),
        (rpc_errors.ChannelsTooMuchError(request=None), NotJoinableError),
        (rpc_errors.UserBannedInChannelError(request=None), NotJoinableError),
        (rpc_errors.ChannelInvalidError(request=None), ResourceNotFoundError),
        (rpc_errors.FloodWaitError(request=None, capture=600), TelegramFloodWaitError),
    ],
)
async def test_join_maps_telegram_refusals_and_surfaces_flood_waits(gateway, client, failure, expected):
    client.know(CHANNEL)
    client.channels[CHANNEL] = channel(CHANNEL)
    client.failures[JoinChannelRequest] = failure

    with pytest.raises(expected) as raised:
        await gateway.join_chat(str(CHANNEL))
    if expected is TelegramFloodWaitError:
        assert raised.value.headers["Retry-After"] == "600"
    assert client.requests_of(UpdateNotifySettingsRequest) == []


@pytest.mark.asyncio
async def test_join_unknown_chat_is_not_found(gateway, client):
    with pytest.raises(ResourceNotFoundError):
        await gateway.join_chat(str(CHANNEL))
    assert client.requests_of(JoinChannelRequest) == []


@pytest.mark.asyncio
async def test_join_budget_limits_new_joins_locally(settings, client):
    service = TelethonGateway(settings.model_copy(update={"telegram_join_limit": 1}))
    service._client = client
    service._started = True
    client.know(CHANNEL, OTHER)
    client.channels[CHANNEL] = channel(CHANNEL)
    client.channels[OTHER] = channel(OTHER)

    await service.join_chat(str(CHANNEL))
    with pytest.raises(LocalRateLimitError):
        await service.join_chat(str(OTHER))
    assert len(client.requests_of(JoinChannelRequest)) == 1


@pytest.mark.asyncio
async def test_latest_answers_all_members_with_one_request_in_request_order(gateway, client):
    client.know(CHANNEL, OTHER, USER, THIRD)
    client.tops.update({CHANNEL: 10, OTHER: 20, USER: 30})
    client.left.add(THIRD)
    client.tops[THIRD] = 99  # Telegram may still describe a channel the account left.
    ids = [str(OTHER), "-1001111111111", str(CHANNEL), str(USER), str(THIRD)]

    result = await gateway.latest_messages(ids)

    assert [item.model_dump() for item in result] == [
        {"chat_id": str(OTHER), "member": True, "top_message_id": 20},
        {"chat_id": "-1001111111111", "member": False, "top_message_id": None},
        {"chat_id": str(CHANNEL), "member": True, "top_message_id": 10},
        {"chat_id": str(USER), "member": True, "top_message_id": 30},
        {"chat_id": str(THIRD), "member": False, "top_message_id": None},
    ]
    [request] = client.requests_of(GetPeerDialogsRequest)
    assert len(request.peers) == 4  # The id unknown to the session costs nothing.
    assert client.remote_lookups == [] and client.get_input_entity_calls == 0


@pytest.mark.asyncio
async def test_latest_reports_dialogs_missing_from_the_result_as_non_members(gateway, client):
    client.know(CHANNEL, OTHER)
    client.tops[CHANNEL] = 10

    result = await gateway.latest_messages([str(CHANNEL), str(OTHER)])

    assert [(item.member, item.top_message_id) for item in result] == [(True, 10), (False, None)]


@pytest.mark.asyncio
async def test_latest_bisects_a_rejected_batch_and_then_skips_the_bad_peer(gateway, client):
    members = [CHANNEL, OTHER, THIRD]
    client.know(*members, -1002222222222)
    client.tops.update({CHANNEL: 1, OTHER: 2, THIRD: 3})
    client.rejected.add(-1002222222222)
    ids = [str(value) for value in (CHANNEL, OTHER, -1002222222222, THIRD)]

    first = await gateway.latest_messages(ids)

    assert [item.top_message_id for item in first] == [1, 2, None, 3]
    assert not first[2].member
    # [4] fails -> [2] ok + [2] fails -> [1] fails + [1] ok
    assert len(client.requests_of(GetPeerDialogsRequest)) == 5

    client.requests.clear()
    second = await gateway.latest_messages(ids)
    assert [item.top_message_id for item in second] == [1, 2, None, 3]
    [request] = client.requests_of(GetPeerDialogsRequest)
    assert len(request.peers) == 3


@pytest.mark.asyncio
async def test_latest_rechecks_skipped_peers_after_the_ttl_and_after_a_join(gateway, client, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(gateway_module.time, "monotonic", lambda: clock[0])
    client.know(CHANNEL)
    client.rejected.add(CHANNEL)
    await gateway.latest_messages([str(CHANNEL)])
    assert str(CHANNEL) in gateway._latest_skip

    clock[0] += gateway.settings.telegram_latest_nonmember_ttl + 1
    client.rejected.clear()
    client.tops[CHANNEL] = 7
    result = await gateway.latest_messages([str(CHANNEL)])
    assert result[0].top_message_id == 7

    client.rejected.add(CHANNEL)
    await gateway.latest_messages([str(CHANNEL)])
    client.rejected.clear()
    client.channels[CHANNEL] = channel(CHANNEL)
    await gateway.join_chat(str(CHANNEL))
    assert str(CHANNEL) not in gateway._latest_skip


@pytest.mark.asyncio
async def test_latest_surfaces_flood_waits_without_bisecting(gateway, client):
    client.know(CHANNEL, OTHER)
    client.failures[GetPeerDialogsRequest] = rpc_errors.FloodWaitError(request=None, capture=42)

    with pytest.raises(TelegramFloodWaitError):
        await gateway.latest_messages([str(CHANNEL), str(OTHER)])
    assert len(client.requests_of(GetPeerDialogsRequest)) == 1


@pytest.mark.asyncio
async def test_latest_skips_the_request_when_no_chat_is_known(gateway, client):
    result = await gateway.latest_messages([str(CHANNEL)])

    assert result[0].member is False
    assert client.requests == []


@pytest.mark.asyncio
async def test_start_enables_updates_and_registers_a_new_message_handler(settings):
    client = ChannelClient()
    captured = {}

    def factory(*args, **kwargs):
        captured.update(kwargs)
        return client

    service = TelethonGateway(settings, client_factory=factory)
    await service.start()

    assert captured["receive_updates"] is True
    [(callback, builder)] = client.event_handlers
    assert isinstance(builder, events.NewMessage)

    message = types.Message(id=9, peer_id=types.PeerChannel(1234567890), date=datetime(2026, 1, 1), message="hi")
    with service.updates_hub().subscribe(frozenset({str(CHANNEL)})) as subscription:
        await callback(events.NewMessage.Event(message))
        await callback(events.NewMessage.Event(types.Message(id=1, peer_id=types.PeerUser(5), date=None, message="")))
        event = await subscription.next(0.1)
        assert event is not None and event.message_id == 9 and event.chat_id == str(CHANNEL)
        assert subscription.pending == 0

        await service.stop()
        assert subscription.closed_reason == "shutdown"


@pytest.mark.asyncio
async def test_disabled_updates_skip_the_handler_and_refuse_streams(settings):
    client = ChannelClient()
    captured = {}

    def factory(*args, **kwargs):
        captured.update(kwargs)
        return client

    service = TelethonGateway(settings.model_copy(update={"telegram_receive_updates": False}), client_factory=factory)
    await service.start()

    assert captured["receive_updates"] is False
    assert client.event_handlers == []
    with pytest.raises(CapabilityUnavailableError):
        service.updates_hub()
