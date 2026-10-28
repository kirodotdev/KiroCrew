"""A channel-thread turn drains the linked dashboard tab's queued subagent notices.

The gateway queues a subagent notice -- a restart orphan, or a completion whose
injection failed -- on the dashboard tab that shows a channel conversation, in
``slot._pending_subagent_failures`` keyed by ``dashboard_slot_key``. A dashboard
turn in that tab drains it; but a turn started in the Slack thread runs on the
channel path, holds the linked session's lease and never read that queue, so the
agent never saw the notice and an owed orphan notice stayed owed and was
re-announced on a later restart.

These pin that a channel-path turn now reads, clears and settles that queue
exactly as a dashboard turn does, prepends the notice to the turn's prompt, and
leaves a conversation with no linked tab untouched.
"""

from __future__ import annotations

import asyncio

import pytest
from slack_handler_helpers import FakeProvider, FakeSessionManager

from conftest import MockSlackClient
from kiro_crew.dashboard.chat_turn.queue_drain import (
    load_linked_tab_window,
    restore_taken_tab_notices,
    settle_taken_tab_notices,
    take_linked_tab_subagent_notices,
)
from kiro_crew.dashboard.chat_utils_slots import dashboard_slot_key
from kiro_crew.messaging.link import canonical_key
from kiro_crew.providers.base import LLMEvent
from kiro_crew.session_surface import set_dashboard_surfaced
from kiro_crew.slack import handler as _handler

# A thread turn: reply_ts == thread_ts, so the session key is this channel
# conversation's own ``slack:<ts>``.
_THREAD_TS = "1700000000.000100"
_SESSION_KEY = canonical_key(_THREAD_TS)  # slack:1700000000.000100

_NOTICE = (
    "🧵 Sub-agent `ag-7` ❌\nTask: crunch the logs\n\n"
    "⚠️ Result delivery failed — read it from disk."
)


class _PromptCapturingProvider(FakeProvider):
    """Records the prompt each ``stream`` call receives."""

    def __init__(self, events=None):
        super().__init__(events)
        self.prompts: list[str] = []

    async def stream(self, message, timeout=120.0):
        self.prompts.append(message)
        async for ev in super().stream(message, timeout):
            yield ev


class _QueueSlot:
    """A dashboard tab slot carrying a queued sub-agent notice.

    ``take_pending_subagent_deliveries`` mirrors the real slot's shape: a notice
    with no owed ``delivered`` tombstone hands back an empty list, which is the
    common case for a completion-injection-failed notice recorded without a
    delivery. The call is still recorded so the settlement attempt is observable.
    """

    def __init__(self, notices: list[str]):
        self._pending_subagent_failures = list(notices)
        self.window_pending = False
        self.taken: list[list[str]] = []

    def take_pending_subagent_deliveries(self, notices):
        self.taken.append(list(notices))
        return []  # no delivery debt owed for this notice


class _StubDashboardState:
    """Minimal dashboard state: a single named slot and no sub-agent manager."""

    def __init__(self, slot_name: str, slot: "_QueueSlot | None"):
        self._slots = {slot_name: slot} if slot is not None else {}
        self.subagents = None
        self._background_tasks: set = set()

    def get_slot(self, name):
        return self._slots.get(name)


def _run(slack, sessions, *, thread_ts=_THREAD_TS):
    asyncio.run(
        _handler.handle_message(
            slack,
            sessions,
            "C1",
            "what did the agent find?",
            thread_ts,
            "1700000000.000200",
            "U1",
        )
    )


def test_thread_turn_drains_and_settles_the_linked_tab_notice(monkeypatch):
    """The queued notice reaches the prompt, is cleared, and settlement is attempted."""
    set_dashboard_surfaced({_SESSION_KEY})
    monkeypatch.setattr("kiro_crew.session_surface._dashboard_surfaced", frozenset({_SESSION_KEY}))
    slot_name = dashboard_slot_key(_SESSION_KEY)
    assert slot_name, "the surfaced channel session should resolve a tab slot name"

    slot = _QueueSlot([_NOTICE])
    state = _StubDashboardState(slot_name, slot)
    monkeypatch.setattr(_handler, "get_dashboard_state", lambda: state)

    provider = _PromptCapturingProvider([LLMEvent(kind="text_chunk", text="here it is")])
    sessions = FakeSessionManager(provider)
    slack = MockSlackClient()
    slack._stream_enabled = True

    _run(slack, sessions)

    prompt = "\n\n".join(provider.prompts)
    assert _NOTICE in prompt, "the queued tab notice never reached the thread turn's prompt"
    assert slot._pending_subagent_failures == [], "the tab queue was not cleared (exactly-once)"
    assert slot.taken == [[_NOTICE]], "the drain did not settle the notice's delivery debt"


def test_thread_turn_with_no_linked_tab_is_untouched(monkeypatch):
    """A channel conversation with no open tab queues nothing and stays silent."""
    set_dashboard_surfaced(set())
    monkeypatch.setattr("kiro_crew.session_surface._dashboard_surfaced", frozenset())
    state = _StubDashboardState("slack_whatever", None)
    monkeypatch.setattr(_handler, "get_dashboard_state", lambda: state)

    provider = _PromptCapturingProvider([LLMEvent(kind="text_chunk", text="ok")])
    sessions = FakeSessionManager(provider)
    slack = MockSlackClient()
    slack._stream_enabled = True

    _run(slack, sessions)


# ── The take/settle/restore lifecycle. The channel seams take ONCE per turn
#    (carrying across any compaction replay), settle ONLY once the turn lands,
#    and restore the notices to the queue if the turn fails or is refused before
#    consumption — so a refused turn never loses the result. ──


@pytest.mark.parametrize(
    "session_key",
    [
        "slack:1700000000.000111",
        "discord:guild-1:chan-2:thread-3",
        "teams:19:aaa@thread.tacv2",
        "telegram:-100123:456",
        "webex:room-xyz",
        "whatsapp:15551234567",
    ],
)
def test_take_then_settle_for_each_transport(monkeypatch, session_key):
    """Take clears the queue (no settle yet); settle later writes the debt."""
    set_dashboard_surfaced({session_key})
    monkeypatch.setattr("kiro_crew.session_surface._dashboard_surfaced", frozenset({session_key}))
    slot_name = dashboard_slot_key(session_key)
    assert slot_name, f"{session_key} should surface a tab slot name"

    slot = _QueueSlot([_NOTICE])
    state = _StubDashboardState(slot_name, slot)

    asyncio.run(load_linked_tab_window(state, session_key))
    notices = take_linked_tab_subagent_notices(state, session_key)

    assert notices == [_NOTICE], "take did not return the queued notice to prepend"
    assert slot._pending_subagent_failures == [], "take did not clear the queue"
    assert slot.taken == [], "take must NOT settle — settlement waits for consumption"

    # Landed: settle writes the debt exactly once.
    settle_taken_tab_notices(state, session_key, notices)
    assert slot.taken == [[_NOTICE]], "settle did not claim the delivery debt"


def test_restore_puts_notices_back_with_debt_owed(monkeypatch):
    """A turn that fails before consumption restores the queue and never settles."""
    key = "discord:guild-9:chan-9:thread-9"
    set_dashboard_surfaced({key})
    monkeypatch.setattr("kiro_crew.session_surface._dashboard_surfaced", frozenset({key}))
    slot = _QueueSlot([_NOTICE])
    state = _StubDashboardState(dashboard_slot_key(key), slot)

    notices = take_linked_tab_subagent_notices(state, key)
    assert slot._pending_subagent_failures == []
    # Turn refused/failed before consumption: restore, do NOT settle.
    restore_taken_tab_notices(state, key, notices)

    assert slot._pending_subagent_failures == [_NOTICE], "notices were not restored to the queue"
    assert slot.taken == [], "a restored (never-consumed) notice must not be settled"


def test_restore_puts_notices_at_the_front(monkeypatch):
    """Restored notices go to the FRONT, ahead of anything queued meanwhile."""
    key = "slack:1700000000.000222"
    set_dashboard_surfaced({key})
    monkeypatch.setattr("kiro_crew.session_surface._dashboard_surfaced", frozenset({key}))
    slot = _QueueSlot([_NOTICE])
    state = _StubDashboardState(dashboard_slot_key(key), slot)

    notices = take_linked_tab_subagent_notices(state, key)
    slot._pending_subagent_failures.append("later notice")  # queued while the turn ran
    restore_taken_tab_notices(state, key, notices)

    assert slot._pending_subagent_failures == [_NOTICE, "later notice"]


def test_take_is_noop_for_a_non_channel_session():
    """A dashboard-born conversation drains in its own tab turn, never here."""
    state = _StubDashboardState("dashboard_chat-1", None)
    assert take_linked_tab_subagent_notices(state, "dashboard:chat-1") == []


def test_take_is_noop_without_a_dashboard_state():
    """A gateway with no dashboard state takes nothing."""
    assert take_linked_tab_subagent_notices(None, "discord:g:c:t") == []


def test_every_channel_turn_seam_drains_the_tab_queue():
    """Ratchet: every channel turn seam takes the notices through the owner and
    resolves it (settle/restore/hand_off); drive_turn forwards the dashboard state.

    Slack native and the shared ``ChannelTurns`` pipeline, Telegram, the Slack
    transport path and the autonudge wake all take the notices into a
    ``TakenTabNotices`` owner and resolve it exactly once. ``drive_turn`` forwards
    the dashboard state so its callers (Teams, Webex, …) are covered. A new
    channel turn path that skips the drain fails here.
    """
    from pathlib import Path

    import kiro_crew as _pkg

    src = Path(_pkg.__file__).resolve().parent
    # The shared pipeline, Telegram, transport and nudge build the owner via the
    # take_tab_notices factory; Slack native builds it directly (two-phase, so the
    # window load sits before its own lease gate).
    for s in [
        "messaging/dispatch.py",
        "telegram/dispatch/turn_engine.py",
        "slack/transport_dispatch.py",
        "slack/gateway_runtime/nudge_channel_fire.py",
    ]:
        text = (src / s).read_text(encoding="utf-8")
        assert "take_tab_notices" in text, f"{s} never takes the tab notices"
        assert ".prefix(" in text, f"{s} never prepends the taken notices"
        assert ".resolve(" in text or (
            ".settle()" in text and ".restore()" in text
        ), f"{s} never resolves the owner"
    native = (src / "slack" / "handler_runtime" / "turn.py").read_text(encoding="utf-8")
    assert (
        "TakenTabNotices" in native and ".hand_off()" in native
    ), "slack native must build the owner and hand it off before a replay"
    dispatch_src = (src / "messaging" / "dispatch.py").read_text(encoding="utf-8")
    assert (
        "slot_state=(lambda: turn.dashboard_state)" in dispatch_src
    ), "drive_turn no longer forwards dashboard_state; its callers would skip the drain"


# ── TakenTabNotices owner: the lifecycle every seam relies on. First of
#    settle/restore/hand_off wins; the rest are no-ops. ──


def _owner(monkeypatch, key="slack:1700000000.000900", notices=(_NOTICE,)):
    from kiro_crew.dashboard.chat_turn.queue_drain import TakenTabNotices

    set_dashboard_surfaced({key})
    monkeypatch.setattr("kiro_crew.session_surface._dashboard_surfaced", frozenset({key}))
    slot = _QueueSlot(list(notices))
    state = _StubDashboardState(dashboard_slot_key(key), slot)
    # The seam takes (clears) first; the owner then holds the taken notices.
    taken = take_linked_tab_subagent_notices(state, key)
    return TakenTabNotices(state, key, taken), slot, key


def test_owner_settle_then_restore_is_idempotent(monkeypatch):
    owner, slot, _ = _owner(monkeypatch)
    assert bool(owner) is True
    owner.settle()
    assert slot.taken == [[_NOTICE]], "settle did not write the debt"
    assert bool(owner) is False
    owner.restore()  # must be a no-op after settle
    assert slot._pending_subagent_failures == [], "restore ran after settle (double-acted)"


def test_owner_restore_then_settle_is_idempotent(monkeypatch):
    owner, slot, _ = _owner(monkeypatch)
    owner.restore()
    assert slot._pending_subagent_failures == [_NOTICE], "restore did not put the notices back"
    assert slot.taken == [], "restore must not settle"
    owner.settle()  # no-op after restore
    assert slot.taken == [], "settle ran after restore (double-acted)"


def test_owner_hand_off_restores_and_blocks_later_restore(monkeypatch):
    """The Slack-native replay trigger: hand_off puts the notices back for the
    nested replay to re-take, and the outer finally's restore is then a no-op."""
    owner, slot, key = _owner(monkeypatch)
    owner.hand_off()
    assert slot._pending_subagent_failures == [_NOTICE], "hand_off did not restore for the replay"
    # The nested replay re-takes them:
    retaken = take_linked_tab_subagent_notices(owner._state, key)
    assert retaken == [_NOTICE], "the nested replay could not re-take the notices"
    # The outer finally restore is a no-op (already handed off):
    owner.restore()
    # queue is empty because the nested take cleared it; hand_off did not re-add.
    assert slot._pending_subagent_failures == [], "outer finally restored after hand_off"


def test_owner_resolve_before_cancellable_await_keeps_queue(monkeypatch):
    """The Slack-transport trigger (shape): the seam resolves the owner
    synchronously the moment the outcome is known, so a later cancellation of
    post-turn bookkeeping cannot drop the notices — they are already restored."""
    owner, slot, _ = _owner(monkeypatch)
    # Outcome known: not landed -> restore NOW, before any cancellable await.
    owner.resolve(landed=False)
    assert slot._pending_subagent_failures == [_NOTICE]
    # A cancellation that interrupts later bookkeeping would re-enter a finally
    # that calls restore() again — idempotent, so the queue is unchanged.
    owner.restore()
    assert slot._pending_subagent_failures == [_NOTICE]
    assert slot.taken == [], "a never-consumed notice must not be settled"
