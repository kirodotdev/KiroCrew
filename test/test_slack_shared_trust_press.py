"""A shared user's "Trust session" press: their own turns in that thread, nothing else.

On the native and the transport approval card, the press of a shared user
(``slack.shared_access``) makes the per-sender grant their ``!trust`` makes
(``session_trust.add_trusted_sender``): that person's later turns in the thread stop
asking, and the owner's turns, another shared user's and helper agents keep asking.
The owner's press still trusts the session. A linked dashboard slot's card is not
changed here: its thread runs the dashboard chat's session, which has no per-sender
check, so the press there stays the session's Trust.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_handler_helpers import _clean_approval_state  # noqa: F401
from test_slack_sender_trust import _shared_users  # noqa: F401 - the people and clean grants
from test_slack_sender_trust import (
    _NOT_OWN_IDS,
    _NOT_THE_TURNS_OWN,
    FRIEND,
    KEY,
    OTHER,
    OWNER,
    Sessions,
    _call,
    _native_call,
    _transport_predicate,
)
from test_slack_shared_bang_trust import _say

from kiro_crew.acp.types import AcpEvent
from kiro_crew.messaging import session_trust
from kiro_crew.messaging.driver import TurnDriver
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST
from kiro_crew.slack import handler, interactions, thread_trust
from kiro_crew.slack.approval_tokens import build_approval_token
from kiro_crew.slack.handler import _pending_approvals, _PendingApproval, handle_interaction
from kiro_crew.slack.renderer import TOOL_TRUST_ACTION_PREFIX, SlackApprovalDecider

#: The thread ``KEY`` is the session of, and a DM it sits in.
THREAD = KEY.split(":", 1)[1]
DM = "D0000000001"
#: What the presser reads, word for word: whose turns, where, and what still asks.
_ON = (
    "🔓 Trusted for your messages in {where}: their tools run without asking. "
    "Helper agents still ask. "
    "Blocked tools stay blocked. "
    "Type `!trust off` to stop."
)
ON_THREAD = _ON.format(where="this thread")
ON_FLAT_DM = _ON.format(where="this whole DM")
ONE_TOOL = (
    "✅ Approved this one tool. Trust is not on for your messages here, "
    "so the next tool asks again."
)
SESSION_LABEL = "🔓 Trusted this session — tools auto-approved"


class _Slack:
    """The Slack calls a press can make, recorded."""

    def __init__(self) -> None:
        self.ephemerals: list[tuple[str, str, str, str | None]] = []
        self.updates: list[tuple[str, str, str]] = []

    async def post_ephemeral(self, channel, user_id, text, blocks=None, thread_ts=None):
        self.ephemerals.append((channel, user_id, text, thread_ts))

    async def update_message(self, channel, ts, text="", blocks=None):
        self.updates.append((channel, ts, text))

    async def is_dm(self, channel: str) -> bool:
        return channel.startswith("D")


@pytest.fixture
def gateway(monkeypatch) -> SimpleNamespace:
    """The gateway a press reaches: its Slack client and its session manager."""
    orch = SimpleNamespace(slack=_Slack(), sessions=Sessions(), dashboard_state=None)
    monkeypatch.setattr(interactions, "_orch", orch)
    monkeypatch.setattr(handler, "_dashboard_state", None)
    return orch


def _native_card(requester: str, key: str = KEY) -> MagicMock:
    """A native prompt *requester*'s turn on *key* posted; the provider it asks."""
    provider = MagicMock(approve_tool=AsyncMock(return_value=True), reject_tool=AsyncMock())
    _pending_approvals[f"{DM}:card1"] = _PendingApproval(provider, "r0", key, requester=requester)
    return provider


def _press(user: str, action: str = "trust_tool", thread: str = THREAD, value: str = "7") -> dict:
    return {
        "type": "block_actions",
        "user": {"id": user},
        "channel": {"id": DM},
        "message": {"ts": "card1", "thread_ts": thread},
        "actions": [{"action_id": action, "value": value}],
    }


async def _press_transport(decider: SlackApprovalDecider, user: str, thread: str = THREAD) -> bool:
    """*user* presses Trust on *decider*'s open card; whether the prompt was answered."""
    key = decider.session_key
    nonce = decider.reserve(7)
    try:
        press = _press(user, f"{TOOL_TRUST_ACTION_PREFIX}{key}:7", thread)
        press["actions"][0]["value"] = build_approval_token(key, 7, nonce)
        await interactions.dispatch(press)
        return decider._futures["7"].done()
    finally:
        decider.discard(7)


def _friends_grant(orch: SimpleNamespace, key: str = KEY) -> dict:
    """The whole grant store after FRIEND's press on *key*: one sender, one running session."""
    return {key: {FRIEND: thread_trust.session_binding(orch.sessions, key)}}


def _own_turns_trusted(orch: SimpleNamespace, key: str, user: str) -> bool:
    """Whether a tool call of *user*'s own turn on *key* runs without asking."""
    return thread_trust.sender_trusted(orch.sessions, key, user, _call())


def _no_session_grant(orch: SimpleNamespace, key: str = KEY) -> bool:
    """Nothing the owner's turns or a helper agent read was written for *key*."""
    return not (session_trust.is_session_trusted(key) or orch.sessions.set_approval_policy.called)


# ── The native card ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sender, runs",
    [(FRIEND, True), (OTHER, False), (OWNER, False)],
    ids=["the presser", "another shared user", "the owner"],
)
async def test_a_native_press_trusts_only_the_pressers_own_turns(
    monkeypatch, gateway, sender: str, runs: bool
) -> None:
    provider = _native_card(FRIEND)
    await interactions.dispatch(_press(FRIEND))
    provider.approve_tool.assert_awaited_once_with("r0")
    # A helper agent reads the session's approval policy: none was written.
    assert _no_session_grant(gateway)
    assert gateway.slack.updates == [(DM, "card1", ON_THREAD)]
    assert gateway.slack.ephemerals == [(DM, FRIEND, ON_THREAD, THREAD)]
    client, prompt = await _native_call(monkeypatch, sender, gateway.sessions)
    if runs:
        client.approve_tool.assert_awaited_once_with("r1")
        prompt.assert_not_awaited()
    else:
        client.approve_tool.assert_not_awaited()
        prompt.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_native_press_covers_that_thread_only(gateway) -> None:
    _native_card(FRIEND)
    await interactions.dispatch(_press(FRIEND))
    assert session_trust._trusted_senders == _friends_grant(gateway)
    assert not _own_turns_trusted(gateway, "slack:1795569999.000100", FRIEND)


@pytest.mark.asyncio
async def test_the_owners_native_press_still_trusts_the_session(monkeypatch, gateway) -> None:
    provider = _native_card(FRIEND)
    await interactions.dispatch(_press(OWNER))
    provider.approve_tool.assert_awaited_once_with("r0")
    assert session_trust.is_session_trusted(KEY)
    gateway.sessions.set_approval_policy.assert_called_once_with(KEY, "auto")
    assert session_trust._trusted_senders == {}
    assert gateway.slack.updates == [(DM, "card1", "🤝 Trusted")]
    assert gateway.slack.ephemerals == []
    # The owner's turn runs; the session grant is the owner's, so a shared user's asks.
    client, prompt = await _native_call(monkeypatch, OWNER, gateway.sessions)
    client.approve_tool.assert_awaited_once_with("r1")
    client, prompt = await _native_call(monkeypatch, FRIEND, gateway.sessions)
    prompt.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("requester", [OWNER, OTHER, ""])
async def test_a_native_press_on_someone_elses_prompt_grants_nothing(
    gateway, requester: str
) -> None:
    provider = _native_card(requester)
    await interactions.dispatch(_press(FRIEND))
    provider.approve_tool.assert_not_awaited()
    assert f"{DM}:card1" in _pending_approvals  # still waiting for whoever may answer
    assert session_trust._trusted_senders == {} and _no_session_grant(gateway)
    assert gateway.slack.updates == [] and len(gateway.slack.ephemerals) == 1


@pytest.mark.asyncio
async def test_a_native_press_on_an_answered_prompt_grants_nothing(gateway) -> None:
    gateway.slack.fetch_thread_replies = AsyncMock(return_value=[{"user": FRIEND}])
    await interactions.dispatch(_press(FRIEND))
    assert session_trust._trusted_senders == {} and _no_session_grant(gateway)
    assert gateway.slack.updates == []
    assert gateway.slack.ephemerals == [
        (DM, FRIEND, interactions.APPROVAL_NOT_WAITING_TEXT, THREAD)
    ]


@pytest.mark.asyncio
async def test_a_native_press_under_a_trust_deny_approves_the_one_tool(
    monkeypatch, gateway
) -> None:
    monkeypatch.setattr(handler, "trust_permitted", lambda: False)
    monkeypatch.setattr(session_trust, "trust_permitted", lambda: False)
    provider = _native_card(FRIEND)
    await interactions.dispatch(_press(FRIEND))
    provider.approve_tool.assert_awaited_once_with("r0")
    assert session_trust._trusted_senders == {} and _no_session_grant(gateway)
    assert gateway.slack.updates == [(DM, "card1", ONE_TOOL)]
    assert gateway.slack.ephemerals == [(DM, FRIEND, ONE_TOOL, THREAD)]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["dashboard:chat-1", "cron:nightly"])
async def test_a_native_press_in_a_thread_that_runs_another_session_approves_the_one_tool(
    gateway, key: str
) -> None:
    """``!trust`` refuses such a thread, so a grant there could never be taken off."""
    provider = _native_card(FRIEND, key)
    assert (
        await handle_interaction(
            DM, "card1", "trust_tool", user_id=FRIEND, thread_ts=THREAD, sessions=gateway.sessions
        )
        == "approve_tool"
    )
    provider.approve_tool.assert_awaited_once_with("r0")
    assert session_trust._trusted_senders == {} and _no_session_grant(gateway, key)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "registry", [None, Sessions(live=False)], ids=["no registry", "no session"]
)
async def test_a_native_press_with_no_running_session_to_bind_to_approves_the_one_tool(
    gateway, registry
) -> None:
    """The grant is bound to the session as it runs: with none, there is nothing to trust."""
    gateway.sessions = registry
    provider = _native_card(FRIEND)
    await interactions.dispatch(_press(FRIEND))
    provider.approve_tool.assert_awaited_once_with("r0")
    assert session_trust._trusted_senders == {}
    assert gateway.slack.updates == [(DM, "card1", ONE_TOOL)]


@pytest.mark.asyncio
async def test_a_native_press_in_a_conductor_thread_approves_the_one_tool(
    monkeypatch, gateway
) -> None:
    monkeypatch.setattr(thread_trust, "_has_own_approval_setting", lambda channel, ts: True)
    provider = _native_card(FRIEND)
    await interactions.dispatch(_press(FRIEND))
    provider.approve_tool.assert_awaited_once_with("r0")
    assert session_trust._trusted_senders == {} and _no_session_grant(gateway)
    assert gateway.slack.updates == [(DM, "card1", ONE_TOOL)]


@pytest.mark.asyncio
async def test_a_native_press_grants_nothing_on_a_session_replaced_during_the_approval(
    gateway,
) -> None:
    """The grant is for the session pressed on: one that took its place meanwhile gets none."""
    provider = _native_card(FRIEND)

    async def _approve_while_the_session_is_replaced(_request_id) -> bool:
        gateway.sessions.replace()
        return True

    provider.approve_tool = AsyncMock(side_effect=_approve_while_the_session_is_replaced)
    await interactions.dispatch(_press(FRIEND))
    provider.approve_tool.assert_awaited_once_with("r0")
    assert session_trust._trusted_senders == {} and _no_session_grant(gateway)
    assert gateway.slack.updates == [(DM, "card1", ONE_TOOL)]


@pytest.mark.asyncio
async def test_a_native_press_whose_tool_is_refused_leaves_no_grant(gateway) -> None:
    provider = _native_card(FRIEND)
    provider.approve_tool = AsyncMock(return_value=False)  # the security floor said no
    await interactions.dispatch(_press(FRIEND))
    assert session_trust._trusted_senders == {} and _no_session_grant(gateway)
    assert gateway.slack.updates == [(DM, "card1", "🚫 Rejected")]
    assert gateway.slack.ephemerals == []


@pytest.mark.asyncio
async def test_a_native_grant_is_audited_as_the_pressers_own(monkeypatch, gateway) -> None:
    audit = MagicMock()
    monkeypatch.setattr(handler, "sel", lambda: audit)
    _native_card(FRIEND)
    assert (
        await handle_interaction(
            DM, "card1", "trust_tool", user_id=FRIEND, thread_ts=THREAD, sessions=gateway.sessions
        )
        == "trust_own_turns"
    )
    rows = [c.kwargs for c in audit.log_api_access.call_args_list]
    assert [r["operation"] for r in rows] == [
        "slack.interactive.trust_own",
        "slack.interactive.approval",
    ]
    assert rows[0] == {
        "caller": FRIEND,
        "operation": "slack.interactive.trust_own",
        "outcome": "allowed",
        "source": "slack",
        "resources": KEY,
        "error": "",
    }
    assert rows[1]["resources"] == "trust_own_turns"


@pytest.mark.asyncio
async def test_a_native_press_that_fails_after_the_approval_answers_the_tool_once(
    monkeypatch, gateway
) -> None:
    """The grant comes after the tool is approved. A raise there must not make the
    handler's guard reject the approved request: a second answer cancels the turn."""
    provider = _native_card(FRIEND)
    pending = _pending_approvals[f"{DM}:card1"]

    def audit_sink_down(**row) -> None:
        if row["operation"] == "slack.interactive.trust_own":
            raise RuntimeError("audit sink down")

    monkeypatch.setattr(handler, "sel", lambda: MagicMock(log_api_access=audit_sink_down))
    with pytest.raises(RuntimeError, match="audit sink down"):
        await handle_interaction(
            DM, "card1", "trust_tool", user_id=FRIEND, thread_ts=THREAD, sessions=gateway.sessions
        )
    provider.approve_tool.assert_awaited_once_with("r0")
    provider.reject_tool.assert_not_awaited()
    assert pending.future.result() == "approved"


# ── The transport card ──


@pytest.mark.parametrize(
    "sender, runs",
    [(FRIEND, True), (OTHER, False), (OWNER, False)],
    ids=["the presser", "another shared user", "the owner"],
)
def test_a_transport_press_trusts_only_the_pressers_own_turns(
    monkeypatch, gateway, sender: str, runs: bool
) -> None:
    predicate, key = _transport_predicate(monkeypatch, sender, gateway.sessions)
    thread = key.split(":", 1)[1]
    answered = asyncio.run(
        _press_transport(SlackApprovalDecider(key, requester=FRIEND), FRIEND, thread)
    )
    assert answered and _no_session_grant(gateway, key)
    assert gateway.slack.updates == [(DM, "card1", ON_THREAD)]
    assert predicate() is runs


@pytest.mark.asyncio
async def test_the_owners_transport_press_still_trusts_the_session(gateway) -> None:
    assert await _press_transport(SlackApprovalDecider(KEY, requester=FRIEND), OWNER)
    assert session_trust.is_session_trusted(KEY)
    gateway.sessions.set_approval_policy.assert_called_once_with(KEY, "auto")
    assert session_trust._trusted_senders == {}
    assert gateway.slack.updates == [(DM, "card1", SESSION_LABEL)]


@pytest.mark.asyncio
@pytest.mark.parametrize("requester", [OWNER, OTHER, ""])
async def test_a_transport_press_on_someone_elses_prompt_grants_nothing(
    gateway, requester: str
) -> None:
    assert not await _press_transport(SlackApprovalDecider(KEY, requester=requester), FRIEND)
    assert session_trust._trusted_senders == {} and _no_session_grant(gateway)
    assert gateway.slack.updates == []


@pytest.mark.asyncio
async def test_a_transport_press_on_an_answered_prompt_grants_nothing(gateway) -> None:
    decider = SlackApprovalDecider(KEY, requester=FRIEND)
    nonce = decider.reserve(7)
    try:
        assert decider.resolve(7, True)  # answered first
        press = _press(FRIEND, f"{TOOL_TRUST_ACTION_PREFIX}{KEY}:7")
        press["actions"][0]["value"] = build_approval_token(KEY, 7, nonce)
        await interactions.dispatch(press)
    finally:
        decider.discard(7)
    assert session_trust._trusted_senders == {} and _no_session_grant(gateway)
    assert gateway.slack.updates == [(DM, "card1", "⏱ This approval already expired.")]


@pytest.mark.asyncio
async def test_a_transport_press_under_a_trust_deny_approves_the_one_tool(
    monkeypatch, gateway
) -> None:
    monkeypatch.setattr(interactions, "trust_permitted", lambda: False)
    monkeypatch.setattr(session_trust, "trust_permitted", lambda: False)
    decider = SlackApprovalDecider(KEY, requester=FRIEND)
    assert await _press_transport(decider, FRIEND)
    assert session_trust._trusted_senders == {} and _no_session_grant(gateway)
    assert gateway.slack.updates == [(DM, "card1", ONE_TOOL)]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["dashboard:chat-1", "cron:nightly"])
async def test_a_transport_press_in_a_thread_that_runs_another_session_approves_the_one_tool(
    gateway, key: str
) -> None:
    assert await _press_transport(SlackApprovalDecider(key, requester=FRIEND), FRIEND)
    assert session_trust._trusted_senders == {} and _no_session_grant(gateway, key)
    assert gateway.slack.updates == [(DM, "card1", ONE_TOOL)]


@pytest.mark.asyncio
@pytest.mark.parametrize("card", ["native", "transport"])
async def test_a_press_loads_the_code_channel_store_off_the_event_loop(
    monkeypatch, gateway, card: str
) -> None:
    """The grant asks the store with nothing awaited, so the press loads it first, in a thread."""
    from kiro_crew.slack.code_channels import store as store_module

    loop_thread = threading.get_ident()
    cold_reads_on_the_loop: list[int] = []
    load = store_module.CodeChannelStore._ensure_loaded

    def _ensure_loaded(self) -> None:
        if not self._loaded and threading.get_ident() == loop_thread:
            cold_reads_on_the_loop.append(1)
        load(self)

    monkeypatch.setattr(store_module.CodeChannelStore, "_ensure_loaded", _ensure_loaded)
    cold = store_module.CodeChannelStore()
    monkeypatch.setattr(store_module, "_STORE", cold)
    if card == "native":
        _native_card(FRIEND)
        await interactions.dispatch(_press(FRIEND))
    else:
        assert await _press_transport(SlackApprovalDecider(KEY, requester=FRIEND), FRIEND)
    assert cold._loaded and cold_reads_on_the_loop == []
    assert session_trust._trusted_senders == _friends_grant(gateway)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_kind", ["bot", "conductor"])
@pytest.mark.parametrize("card", ["native", "transport"])
async def test_a_press_in_a_channel_with_a_bot_or_conductor_approves_the_one_tool(
    monkeypatch, gateway, handler_kind: str, card: str
) -> None:
    """Where ``!trust`` refuses too: the channel's one chat runs the thread's next message."""
    from kiro_crew.slack import channel_handler

    if handler_kind == "bot":
        monkeypatch.setattr(channel_handler, "channel_bot", lambda _c: "helper")
    else:
        monkeypatch.setattr(channel_handler, "channel_conductor", lambda _c: object())
    if card == "native":
        provider = _native_card(FRIEND)
        await interactions.dispatch(_press(FRIEND))
        provider.approve_tool.assert_awaited_once_with("r0")
    else:
        assert await _press_transport(SlackApprovalDecider(KEY, requester=FRIEND), FRIEND)
    assert session_trust._trusted_senders == {} and _no_session_grant(gateway)
    assert gateway.slack.updates == [(DM, "card1", ONE_TOOL)]


@pytest.mark.asyncio
async def test_a_transport_press_in_a_single_session_dm_says_it_covers_the_whole_dm(
    gateway,
) -> None:
    flat = f"slack:{DM}"
    assert await _press_transport(SlackApprovalDecider(flat, requester=FRIEND), FRIEND, "")
    assert _own_turns_trusted(gateway, flat, FRIEND) and _no_session_grant(gateway, flat)
    assert gateway.slack.updates == [(DM, "card1", ON_FLAT_DM)]


# ── Helper agents still ask ──


@pytest.mark.asyncio
@pytest.mark.parametrize("marks", _NOT_THE_TURNS_OWN, ids=_NOT_OWN_IDS)
async def test_after_a_native_press_a_helper_agents_call_still_asks(
    monkeypatch, gateway, marks: dict
) -> None:
    """A helper agent's call arrives on the presser's own turn. The notice says such
    calls still ask, and so does a backend that cannot mark them."""
    _native_card(FRIEND)
    await interactions.dispatch(_press(FRIEND))
    assert gateway.slack.updates == [(DM, "card1", ON_THREAD)] and "Helper agents" in ON_THREAD
    client, prompt = await _native_call(monkeypatch, FRIEND, gateway.sessions, **marks)
    client.approve_tool.assert_not_awaited()
    prompt.assert_awaited_once()
    # The presser's own call in the same turn runs.
    client, prompt = await _native_call(monkeypatch, FRIEND, gateway.sessions)
    client.approve_tool.assert_awaited_once_with("r1")


@pytest.mark.parametrize("marks", _NOT_THE_TURNS_OWN, ids=_NOT_OWN_IDS)
def test_after_a_transport_press_a_helper_agents_call_still_asks(
    monkeypatch, gateway, marks: dict
) -> None:
    predicate, key = _transport_predicate(monkeypatch, FRIEND, gateway.sessions)
    thread = key.split(":", 1)[1]
    assert asyncio.run(
        _press_transport(SlackApprovalDecider(key, requester=FRIEND), FRIEND, thread)
    )
    assert predicate(_call(**marks)) is False
    assert predicate(_call()) is True


class _PressingRenderer(Renderer):
    """Shows the prompt as the Slack renderer does, then has FRIEND press Trust on it."""

    def __init__(self, decider: SlackApprovalDecider) -> None:
        super().__init__(TransportCapabilities())
        self.decider = decider
        self.presses: list[asyncio.Future] = []

    async def on_text_chunk(self, text):
        pass

    async def on_done(self, stop_reason=""):
        pass

    async def on_thinking(self, text):
        pass

    async def on_tool_call(self, tool_call_id, title, tool_kind="", tool_purpose=""):
        pass

    async def on_tool_result(self, tool_call_id, tool_status):
        pass

    async def on_compaction(self, pct):
        pass

    async def on_prompt_choice(
        self, options, request_id, tool_title="", tool_purpose="", tool_input=""
    ):
        key = self.decider.session_key
        nonce = self.decider.reserve(request_id)
        press = _press(FRIEND, f"{TOOL_TRUST_ACTION_PREFIX}{key}:{request_id}")
        press["actions"][0]["value"] = build_approval_token(key, request_id, nonce)
        self.presses.append(asyncio.ensure_future(interactions.dispatch(press)))


@pytest.mark.asyncio
async def test_a_transport_press_keeps_the_grant_when_a_gate_then_blocks_the_tool(gateway) -> None:
    """The turn driver sends the tool only after the press is answered, so the press
    cannot know a gate will block it. The grant stands, as the owner's does, and the
    card's words are true of it: Trust is on, and blocked tools stay blocked."""

    class _Blocking:
        approved: list[str] = []

        async def stream(self, message):
            yield AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="7", options=[{"id": "ok"}])
            yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        async def approve_tool(self, request_id, *, always=False):
            self.approved.append(request_id)
            return False  # the security floor said no

        async def reject_tool(self, request_id):
            raise AssertionError("an approved press is never rejected")

        async def steer(self, *a, **k):
            return None

    decider = SlackApprovalDecider(KEY, requester=FRIEND)
    renderer = _PressingRenderer(decider)
    provider = _Blocking()
    await TurnDriver(provider, renderer, approval_mode="interactive", decider=decider).run("hi")
    await asyncio.gather(*renderer.presses)
    assert provider.approved == ["7"]
    assert session_trust._trusted_senders == _friends_grant(gateway)
    assert gateway.slack.updates == [(DM, "card1", ON_THREAD)]
    assert "Blocked tools stay blocked." in ON_THREAD and _no_session_grant(gateway)


# ── What the grant is ──


def test_the_notice_names_the_pressers_own_turns_and_what_still_asks() -> None:
    assert interactions.SHARED_TRUST_ON_TEXT == _ON
    assert interactions.SHARED_TRUST_IN_THREAD == "this thread"
    assert interactions.SHARED_TRUST_IN_FLAT_DM == "this whole DM"
    assert interactions.SHARED_TRUST_ONE_TOOL_TEXT == ONE_TOOL
    # Never "session": the grant is the presser's own turns, not the session's.
    assert "session" not in _ON + ONE_TOOL


@pytest.mark.asyncio
async def test_the_grant_lives_in_memory_only_so_a_restart_clears_it(gateway) -> None:
    """Nothing durable is written: the press touches no session manager, and the one
    record is the in-process map every clear of session Trust empties."""
    _native_card(FRIEND)
    await interactions.dispatch(_press(FRIEND))
    assert session_trust._trusted_senders == _friends_grant(gateway)
    gateway.sessions.set_approval_policy.assert_not_called()
    session_trust.clear_trusted_sessions()
    assert not _own_turns_trusted(gateway, KEY, FRIEND)


@pytest.mark.asyncio
@pytest.mark.parametrize("card", ["native", "transport"])
async def test_trust_status_shows_the_pressed_grant_and_trust_off_removes_it(
    monkeypatch, gateway, card: str
) -> None:
    assert (
        await _say("!trust status", FRIEND, gateway.sessions, channel=DM)
        == thread_trust.OWN_STATUS_OFF_TEXT
    )
    if card == "native":
        _native_card(FRIEND)
        await interactions.dispatch(_press(FRIEND))
    else:
        assert await _press_transport(SlackApprovalDecider(KEY, requester=FRIEND), FRIEND)
    assert (
        await _say("!trust status", FRIEND, gateway.sessions, channel=DM)
        == thread_trust.OWN_STATUS_ON_TEXT
    )
    # The other shared user and the owner read no grant of their own from it.
    assert (
        await _say("!trust status", OTHER, gateway.sessions, channel=DM)
        == thread_trust.OWN_STATUS_OFF_TEXT
    )
    assert await _say(
        "!trust status", OWNER, gateway.sessions, channel=DM
    ) == thread_trust.STATUS_OFF_TEXT + (thread_trust.SENDERS_TEXT.format(count=1))
    assert (
        await _say("!trust off", FRIEND, gateway.sessions, channel=DM) == thread_trust.OWN_OFF_TEXT
    )
    assert session_trust._trusted_senders == {}
    client, prompt = await _native_call(monkeypatch, FRIEND, gateway.sessions)
    client.approve_tool.assert_not_awaited()
    prompt.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_grant_ends_when_the_thread_moves_to_another_agent(monkeypatch, gateway) -> None:
    _native_card(FRIEND)
    await interactions.dispatch(_press(FRIEND))
    assert session_trust._trusted_senders == _friends_grant(gateway)
    gateway.sessions.replace(agent="another-agent")
    client, prompt = await _native_call(monkeypatch, FRIEND, gateway.sessions)
    client.approve_tool.assert_not_awaited()
    prompt.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_presser_taken_off_shared_access_loses_the_grant(monkeypatch, gateway) -> None:
    from test_slack_sender_trust import _cfg

    _native_card(FRIEND)
    await interactions.dispatch(_press(FRIEND))
    monkeypatch.setattr(handler, "_orch_cfg", _cfg(ids=(OTHER,)))
    client, prompt = await _native_call(monkeypatch, FRIEND, gateway.sessions)
    client.approve_tool.assert_not_awaited()
    prompt.assert_awaited_once()
