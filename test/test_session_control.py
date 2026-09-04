"""Session control: one chat session sending to, stopping, and reading another.

The suite is organized around the two things that can go wrong here. First,
authorization: every refusal is asserted against the REAL slot objects, because
the guards read ``memory_mode`` / ``workspace`` / ``_app`` off the production
class and a permissive test double would let a dead guard look alive. Second,
delivery: a message must land exactly once — the interesting failures are the
double-delivery and silent-drop paths around a steer that the live client
refuses mid-flight.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.config import loader
from kiro_crew.dashboard import chat_delivery as cd
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import stop_retry
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc

# The autouse fixture below replaces ``sc.session_control_enabled`` so every
# other test runs in the shipped (enabled) state without reading config. Keep a
# handle on the real function so the tests that are ABOUT that function can call
# it — it still resolves ``KiroCrewConfig`` through module globals, so patching
# the config class continues to work through this reference.
_REAL_ENABLED = sc.session_control_enabled


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """Default every test to the shipped state (enabled) without reading config."""
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _fresh_stop_windows():
    """The stop-retry window is process-wide module state.

    Left behind, one test's stop makes a later test's FIRST stop read as a repeat
    — so escalation would be withheld from a test that never retried anything.
    """
    stop_retry.reset_for_tests()
    yield
    stop_retry.reset_for_tests()


@pytest.fixture(autouse=True)
def _fresh_create_budget():
    """The per-caller create-rate window is process-wide module state.

    Nearly every create test here calls as the same caller (``chat-1``), so the
    20-per-window budget is consumed by the file itself: left behind, the 21st
    create in a worker process is refused with ``create_rate_limited`` and the
    test that happens to be 21st fails for a reason it never asserted. Which
    test that is depends on xdist distribution, which is what made it flaky.
    """
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


def _slot(state, name: str, **kwargs):
    return state.get_or_create_slot(name, **kwargs)


def _key(slot) -> str:
    """The session key the MCP process would present for *slot*."""
    return slot_history_key(slot)


def _peer_target(state, name: str, caller, **kwargs):
    """A peer session in the caller's workspace, addressable by the other verbs."""
    return state.get_or_create_slot(name, **kwargs)


def _busy(slot):
    """Make *slot* look like a turn is in flight.

    ``running`` is derived (``task is not None and not task.done()``), so a busy
    slot is expressed by its task — assigning ``running`` would raise.
    """
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def _steerable(accepted: bool = True) -> MagicMock:
    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(return_value=accepted)
    return client


# ── Target resolution ────────────────────────────────────────────────────────


def test_resolves_target_by_slot_key(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    resolved = sc.authorize_target(
        state, caller_session_key=_key(caller), target="chat-2", operation="read"
    )
    assert resolved is target


def test_resolves_target_by_exact_title(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.title = "Rebase the watchdog PR"
    resolved = sc.authorize_target(
        state,
        caller_session_key=_key(caller),
        target="rebase the watchdog pr",
        operation="read",
    )
    assert resolved is target


def test_resolves_target_by_the_key_list_sessions_reports(tmp_path):
    """``list_sessions`` hands out FILENAME STEMS, and the tools say to pass them.

    Mutation guard: matching only ``slot.key`` refuses the documented happy path
    with ``target_not_found`` — the caller does the thing the description tells
    it to do and the tool says the session does not exist.
    """
    from kiro_crew.history import transcript_stem

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    stem = transcript_stem(slot_history_key(target))
    assert stem != target.key, "fixture must exercise the differing-form case"

    resolved = sc.authorize_target(
        state, caller_session_key=_key(caller), target=stem, operation="read"
    )
    assert resolved is target


def test_the_caller_is_also_resolvable_by_its_stem(tmp_path):
    """Symmetry: the MCP process may present either form as the caller identity."""
    from kiro_crew.history import transcript_stem

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    stem = transcript_stem(slot_history_key(caller))
    assert sc.caller_slot_key(state, stem) == caller.key


def test_ambiguous_title_is_refused_not_guessed(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    for name in ("chat-2", "chat-3"):
        _slot(state, name).title = "Shared Title"
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="Shared Title", operation="read"
        )
    assert exc.value.status == 409
    # The refusal covers a collision in ANY addressing form, not titles alone,
    # so it names the forms rather than saying "share the title".
    assert exc.value.code == "ambiguous_target"
    assert "sessions match" in exc.value.message


def test_unknown_target_is_404(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="chat-nope", operation="read"
        )
    assert exc.value.status == 404


def test_caller_is_resolved_from_its_history_key(tmp_path):
    """The caller presents a HISTORY key, which is not always the slot key.

    Mutation guard: resolving on ``slot.key`` alone would fail to identify the
    caller here, and an unidentifiable caller is refused outright — so the
    self-send guard would stop protecting anything.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    history_key = _key(caller)
    assert history_key != caller.key, "fixture must exercise the differing-key case"
    assert sc.caller_slot_key(state, history_key) == caller.key


# ── Authorization refusals ───────────────────────────────────────────────────


def test_a_session_cannot_control_itself(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="chat-1", operation="read"
        )
    assert "cannot control itself" in exc.value.message


def test_unidentifiable_caller_is_refused(tmp_path):
    state = _make_state(tmp_path)
    _slot(state, "chat-2")
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key="who:knows", target="chat-2", operation="read"
        )
    assert "could not be identified" in exc.value.message


def test_incognito_target_is_not_addressable(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _slot(state, "chat-secret", memory_mode="incognito")
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="chat-secret", operation="read"
        )
    assert "incognito" in exc.value.message


def test_temporary_target_is_not_addressable(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _slot(state, "chat-temp", memory_mode="temporary")
    with pytest.raises(sc.SessionControlError):
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="chat-temp", operation="read"
        )


def test_app_scoped_target_is_not_addressable(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _slot(state, "chat-app", app="issue-radar")
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="chat-app", operation="read"
        )
    assert "app-scoped" in exc.value.message


def test_between_plan_stages_the_target_still_reports_running(tmp_path):
    """An orchestrator between stages is busy, and `read` must say so.

    `slot.running` is derived from the task, and each stage's `_run_chat` closes
    its own turn — so between stages it reads False while the plan is very much
    alive. A poller following the documented "send, then read until not running"
    loop would stop here and miss every later stage.

    Mutation guard: reporting `slot.running` alone returns False.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.messages.append({"role": "assistant", "content": "stage one done"})
    # Between stages: no task in flight, but the plan is still orchestrating.
    target.task = None
    target._in_stage_execution = True

    out = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")

    assert out["running"] is True, "a mid-plan target must not look idle"


@pytest.mark.asyncio
async def test_an_identical_queue_entry_does_not_masquerade_as_our_requeue(tmp_path):
    """A pre-existing identical queue entry must not be read as OUR requeue.

    The turn consumes our steer (so it leaves `_pending_steers`) while an unrelated
    queue entry happens to carry the same text. Testing the queue for mere
    PRESENCE reports REQUEUED and skips the transcript row, losing the bubble for
    a message that really was delivered.

    Mutation guard: comparing presence instead of a rising count fails this.
    """
    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-1"))
    # Someone queued the same words earlier; nothing to do with our steer.
    slot._queue.append({"id": "q-old", "content": "same words"})

    async def _steer(msg):
        # The running turn consumes our registration, as the settle path does.
        slot._pending_steers.remove(msg)
        return True

    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(side_effect=_steer)
    slot._acp_client = client

    outcome = await cd.steer_into_running_turn(state, slot, "same words")

    assert outcome == cd.STEER_STEERED
    # The delivery is real, so it must leave exactly one row.
    assert len([m for m in slot.messages if m.get("content") == "same words"]) == 1


@pytest.mark.asyncio
async def test_a_consumed_steer_is_not_discarded_when_the_rpc_raises(tmp_path):
    """`steer()` writing and THEN raising must not be reported as discarded.

    `stdin.drain()` can raise after the bytes already reached the child, so the
    exception says nothing about delivery. The evidence does: the registration is
    gone, nothing queued it, and no stop ran — and only the running turn consuming
    it produces that state. Answering 409 here makes the caller resend a message
    the target already has, and it runs twice.

    Mutation guard: trusting the exception over the evidence returns DISCARDED.
    """
    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-1"))

    async def _consume_then_raise(msg):
        slot._pending_steers.remove(msg)  # the turn took it
        raise ConnectionResetError("drain failed after the write landed")

    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(side_effect=_consume_then_raise)
    slot._acp_client = client

    outcome = await cd.steer_into_running_turn(state, slot, "do the thing")

    assert outcome == cd.STEER_STEERED
    # Delivered, so exactly one row — the same persisting tail as a clean steer.
    assert len([m for m in slot.messages if m.get("content") == "do the thing"]) == 1


@pytest.mark.asyncio
async def test_a_merged_row_satisfies_every_delivery_it_stands_for(tmp_path):
    """When the drain merges two steers into one row, BOTH ids must be on it.

    The drain unions each consumed entry's meta, and a plain dict update keeps only
    the last `steer_delivery_id` — so the other caller would find no row for its
    delivery and append a duplicate. The row stands for both messages, so it names
    both ids and each caller recognises it.

    Mutation guard: overwriting instead of accumulating makes the earlier caller
    miss its row.
    """
    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-1"))

    # Another caller's steer is already pending with its own id.
    other_id = "other-delivery-id"
    slot._pending_steers.append("other text")
    slot._steer_delivery_ids["other text"] = other_id

    async def _merge_both(msg):
        mine = slot._steer_delivery_ids.get(msg, "")
        slot._pending_steers.clear()
        # One row for both messages, naming both deliveries.
        slot.append(
            "user",
            f"other text\n\n{msg}",
            "msg msg-u",
            meta={"steer_delivery_ids": [other_id, mine]},
        )
        return True

    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(side_effect=_merge_both)
    slot._acp_client = client

    outcome = await cd.steer_into_running_turn(state, slot, "my text")

    assert outcome == cd.STEER_REQUEUED
    # Only the merged row exists — no standalone duplicate was appended.
    assert len(slot.messages) == 1


@pytest.mark.asyncio
async def test_a_requeued_and_drained_message_is_not_persisted_twice(tmp_path):
    """The whole requeue-then-drain sequence can complete during the await.

    Teardown moves the pending steer into the queue and the NEXT turn drains it,
    appending the row — all while this call is suspended in `steer()`. By then the
    entry is in neither list, which is indistinguishable from the running turn
    having consumed it, so a reconciliation that reads only those lists appends a
    second row for the same message.

    What makes it decidable is the delivery id: the requeue moves it onto the queue
    entry and the drain unions entry meta onto the row it writes, so a row carrying
    the id proves the delivery is already persisted. The simulation below carries
    the id exactly as `_requeue_unconsumed_steers` and the drain do — without that,
    the test would be asserting against a drain that does not exist.

    Mutation guard: dropping the id check appends a second row.
    """
    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-1"))

    async def _teardown_then_drain(msg):
        # Teardown requeues, carrying the delivery id onto the entry...
        did = slot._steer_delivery_ids.get(msg, "")
        slot._pending_steers.remove(msg)
        slot._queue.append({"id": "q1", "content": msg, "meta": {"steer_delivery_id": did}})
        # ...and the next turn drains it, unioning entry meta onto the row.
        entry = slot._queue.pop(0)
        slot.append("user", msg, "msg msg-u", meta=entry["meta"])
        return True

    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(side_effect=_teardown_then_drain)
    slot._acp_client = client

    outcome = await cd.steer_into_running_turn(state, slot, "one message only")

    assert outcome == cd.STEER_REQUEUED
    # Exactly one row: the drain's. A second would be the duplicate.
    assert len([m for m in slot.messages if m.get("content") == "one message only"]) == 1


@pytest.mark.asyncio
async def test_a_natural_teardown_during_the_steer_reports_requeued(tmp_path):
    """The turn ends normally mid-RPC: the teardown requeues, so we must not persist.

    This is the case a `steered`-gated reconciliation cannot see. A natural end
    touches neither `_stop_generation` nor `_stop_state`, so the old code returned
    STEERED and appended a transcript row while the queue drain appended the same
    text again — one message, two bubbles.

    Mutation guard: gating the queue check on `stopped` or on `not steered` makes
    this return STEERED.
    """
    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-1"))

    async def _steer(msg):
        # The turn's teardown runs while the RPC is in flight: it moves the
        # pending steer into the queue. No stop is involved. The entry carries the
        # delivery id, because that is what `_requeue_unconsumed_steers` writes --
        # and it is how reconciliation tells OUR requeue from an unrelated client
        # queueing the same words.
        did = slot._steer_delivery_ids.pop(msg, "")
        slot._pending_steers.remove(msg)
        slot._queue.append({"id": "q1", "content": msg, "meta": {"steer_delivery_id": did}})
        return True

    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(side_effect=_steer)
    slot._acp_client = client

    outcome = await cd.steer_into_running_turn(state, slot, "hello there")

    assert outcome == cd.STEER_REQUEUED
    # The drain owns the append now; a row here would be the duplicate.
    assert not [m for m in slot.messages if m.get("content") == "hello there"]


@pytest.mark.asyncio
async def test_a_second_identical_steer_is_refused_rather_than_registered(tmp_path):
    """Two overlapping identical steers: the second must not register at all.

    `_pending_steers` holds plain strings and every consumer matches by content, so
    with two identical entries in flight nothing downstream can say whose survived.
    The failing case: the FIRST is consumed while the SECOND is refused — the count
    falls back exactly as it would if the second's own entry had gone, so the second
    got persisted as delivered and then requeued by the teardown. One message, two
    bubbles.

    The guard removes the ambiguity instead of resolving it downstream: the second
    steer is refused up front and its caller queues it, so nothing is lost.

    Mutation guard: dropping the guard lets the second register, and with the first
    consumed during the await it is persisted as delivered.
    """
    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-1"))
    # A first caller's identical steer is already in flight.
    slot._pending_steers = ["same text"]
    slot._acp_client = _steerable(accepted=False)

    outcome = await cd.steer_into_running_turn(state, slot, "same text")

    assert outcome == cd.STEER_UNAVAILABLE
    # It never registered, so the first caller's entry is untouched...
    assert slot._pending_steers == ["same text"]
    # ...and nothing was persisted as delivered.
    assert not [m for m in slot.messages if m.get("content") == "same text"]
    # The RPC was never attempted — refusing before the await is the point.
    slot._acp_client.steer.assert_not_awaited()


def test_the_cursor_does_not_skip_rows_beyond_the_limit(tmp_path):
    """A window capped by `limit` must advance the cursor by what it RETURNED.

        Mutation guard: polling with `total` jumps to the end, so every row between
        the window's end and `total` is skipped permanently — the documented
    @pytest.mark.parametrize(
        "send, then poll" loop would silently lose the middle of a long reply.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    for i in range(10):
        target.messages.append({"role": "assistant", "content": f"row-{i}"})

    first = sc.read_messages(
        state, caller_session_key=_key(caller), target="chat-2", limit=4, since=0
    )
    assert [m["content"] for m in first["messages"]] == ["row-0", "row-1", "row-2", "row-3"]
    assert first["total"] == 10
    # The cursor trails the backlog — that difference is the caller's signal to
    # read again rather than wait.
    assert first["next_since"] == 4

    second = sc.read_messages(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        limit=4,
        since=first["next_since"],
    )
    assert [m["content"] for m in second["messages"]] == ["row-4", "row-5", "row-6", "row-7"]
    assert second["next_since"] == 8

    third = sc.read_messages(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        limit=4,
        since=second["next_since"],
    )
    # Every row seen exactly once, nothing skipped.
    assert [m["content"] for m in third["messages"]] == ["row-8", "row-9"]
    assert third["next_since"] == 10 == third["total"]


def test_a_channel_linked_caller_is_refused(tmp_path):
    """The exfiltration direction: a linked caller's reads land in a channel.

    Mutation guard: without this, `session_read_message` from a Slack/Discord-linked
    slot hands a private dashboard transcript to whoever reads that thread.
    `CHANNEL_AGENT_BLOCKED_TOOLS` does not cover it — that keys on the agent
    identity, and a linked slot is a second route to the same surface.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-linked-caller")
    caller.linked_session_key = "slack:1786300000.000200"
    _slot(state, "chat-victim")

    for op in ("send", "stop", "read"):
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=_key(caller),
                target="chat-victim",
                operation=op,
            )
        assert exc.value.code == "linked_session_caller", op


def test_a_channel_linked_target_is_refused(tmp_path):
    """A linked session is mirrored to Slack/Telegram AND cannot be stopped correctly.

    Mutation guard: allowing it lets a relay surface into a channel other humans
    read, and `session_stop` would report success while the target keeps running —
    the stop path addresses `dashboard:<slot>` but a linked slot's turns run under
    its `linked_session_key`.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    linked = _slot(state, "chat-linked")
    linked.linked_session_key = "slack:1786300000.000100"

    for op in ("send", "stop", "read"):
        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state, caller_session_key=_key(caller), target="chat-linked", operation=op
            )
        assert exc.value.code == "linked_session_target", op


def test_target_in_another_workspace_is_refused(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1", workspace="default")
    _slot(state, "chat-other", workspace="research")
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="chat-other", operation="read"
        )
    assert "different workspace" in exc.value.message


def test_scheduled_target_is_refused(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _slot(state, "cron-abc123")
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="cron-abc123", operation="read"
        )
    assert "unattended" in exc.value.message


def test_scheduled_caller_cannot_control_a_session_it_did_not_create(tmp_path):
    """A cron caller is admitted but fenced to its own children (issue #8332).

    The refusal it used to get was ``unattended_caller``, keyed on the slot-key
    prefix. That was replaced by the ``created_by`` fence, which refuses the case
    the prefix check existed for -- a scheduled job reaching the user's own
    conversation -- while letting it drive the sessions it dispatched. The
    positive half, and a cron's other gates, are in
    ``test_cron_session_control.py``.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "cron-abc123")
    # Ownership is read from the JOB, and an unfindable one fails closed, so the
    # fence is only reached once the registry can produce a non-app owner.
    state.crons.list_jobs.return_value = [SimpleNamespace(id="abc123", created_by="U0123ABCD")]
    _slot(state, "chat-2")
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="chat-2", operation="read"
        )
    assert exc.value.code == "not_creator"


def test_workflow_caller_cannot_control_anyone(tmp_path):
    """The unattended refusal still stands for the caller class with no fence.

    A ``workflow-<run_id>`` slot is minted only once its originating tab is gone,
    so there is no owning session to bound it to.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "workflow-run7")
    _slot(state, "chat-2")
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="chat-2", operation="read"
        )
    assert exc.value.code == "unattended_caller"
    assert "cannot control other sessions" in exc.value.message


def test_an_incognito_caller_cannot_reach_a_persistent_peer(tmp_path):
    """Caller-side isolation, which the target-side checks cannot see.

    Mutation guard: without this the incognito session the user asked to leave
    no trace can launder a persistent peer's content in either direction.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-secret", memory_mode="incognito")
    _slot(state, "chat-2")
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="chat-2", operation="read"
        )
    assert exc.value.code == "ephemeral_caller"


def test_an_app_scoped_caller_cannot_reach_a_dashboard_peer(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-app", app="issue-radar")
    _slot(state, "chat-2")
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="chat-2", operation="read"
        )
    assert exc.value.code == "app_scoped_caller"


def test_a_workflow_result_slot_is_unattended(tmp_path):
    """``workflow-<run_id>`` is the real prefix workflow_inject creates.

    Mutation guard: the guard listed ``wf-`` and was dead for this whole class,
    so a peer could start a fresh agent turn in a display-only slot.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _slot(state, "workflow-abc123")
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="workflow-abc123", operation="read"
        )
    assert exc.value.code == "unattended_target"


def test_a_workflow_result_slot_cannot_control_anyone(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "workflow-abc123")
    _slot(state, "chat-2")
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="chat-2", operation="read"
        )
    assert exc.value.code == "unattended_caller"


def test_config_switch_off_refuses_everything(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _slot(state, "chat-2")
    monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="chat-2", operation="read"
        )
    assert "disabled" in exc.value.message


# ── Route authentication ─────────────────────────────────────────────────────


class TestTheRoutesRequireTheInternalSecret:
    """Strict-internal is not self-enforcing at the handler.

    With ``X-Internal-Secret`` ABSENT the middleware falls through to cookie
    auth, and a ``local_only=False`` deployment reclassifies strict paths as
    mixed. Since these routes authorize on the ``X-Session-Key`` the caller
    sends, a browser holding only a dashboard cookie could otherwise act AS any
    of the user's sessions. Mutation guard: dropping the ``internal_auth`` check
    makes every case below reach the operation.
    """

    def _request(self, tmp_path, *, internal: bool, path: str, method: str = "POST"):
        from unittest.mock import MagicMock

        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _slot(state, "chat-2")
        request = MagicMock()
        request.app = {"state": state}
        request.path = path
        request.method = method
        request.headers = {"X-Session-Key": _key(caller)}
        request.query = {"target": "chat-2"}
        request.get = lambda key, default=None: (
            True if (key == "internal_auth" and internal) else default
        )

        async def _json():
            return {"target": "chat-2", "message": "hi"}

        request.json = _json
        return request

    def _body(self, response):
        import json

        return json.loads(response.body.decode())

    def test_create_without_the_secret_is_forbidden(self, tmp_path):
        req = self._request(tmp_path, internal=False, path="/api/session-control/create")
        resp = asyncio.run(handlers_sc.api_session_control_create(req))
        assert resp.status == 403
        assert self._body(resp)["code"] == "internal_secret_required"

    def test_create_returns_a_session_it_actually_opened(self, tmp_path):
        """The create ROUTE needs its own test: a handler-only defect ships it dead.

        An earlier round proved that once -- the route was missing from the strict
        internal path list, so every call refused while the handler-level tests
        still passed.
        """
        req = self._request(tmp_path, internal=True, path="/api/session-control/create")

        async def _json():
            return {"title": "worker"}

        req.json = _json
        resp = asyncio.run(handlers_sc.api_session_control_create(req))

        assert resp.status == 200
        payload = self._body(resp)
        assert payload["ok"] is True
        state = req.app["state"]
        created = state.get_slot(payload["target"])
        assert created is not None, "the route reported a target it did not create"
        assert created.title == "worker"

    def test_create_renders_a_refusal_as_its_status_not_a_500(self, tmp_path, monkeypatch):
        """A SessionControlError from create must come back as its own refusal."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/create")

        async def _boom(*_a, **_kw):
            raise sc.SessionControlError("nope", status=429, code="slot_cap_reached")

        monkeypatch.setattr(sc, "create_session", _boom)
        resp = asyncio.run(handlers_sc.api_session_control_create(req))

        assert resp.status == 429
        assert self._body(resp)["code"] == "slot_cap_reached"

    def test_stop_without_the_secret_is_forbidden(self, tmp_path):
        req = self._request(tmp_path, internal=False, path="/api/session-control/stop")
        resp = asyncio.run(handlers_sc.api_session_control_stop(req))
        assert resp.status == 403

    def test_a_failing_audit_still_refuses_rather_than_erroring(self, tmp_path, monkeypatch):
        """Auditing the refusal must not be able to turn it into a 500.

        The first `sel()` of a process CONSTRUCTS the log, and construction can
        raise (a trust root too short to sign the chain). An unguarded write would
        lose the denial in order to report it -- the caller would see a server
        error where it should see a clean 403.
        """

        def _boom():
            raise RuntimeError("trust root too short")

        monkeypatch.setattr(handlers_sc, "sel", _boom)
        req = self._request(tmp_path, internal=False, path="/api/session-control/stop")
        resp = asyncio.run(handlers_sc.api_session_control_stop(req))
        assert resp.status == 403, "a broken audit must not escalate a refusal to 500"
        assert self._body(resp)["code"] == "internal_secret_required"

    def test_stop_with_the_secret_reaches_the_operation(self, tmp_path, monkeypatch):
        """The stop ROUTE's success path, for the reason create's docstring gives:
        only the 403 arm was covered, so a route-level defect on the reaching path
        would ship dead."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/stop")

        async def _ok(*_a, **_kw):
            return {"ok": True, "target": "chat-2", "stopped": True}

        monkeypatch.setattr(sc, "stop_target", _ok)
        resp = asyncio.run(handlers_sc.api_session_control_stop(req))

        assert resp.status == 200
        assert self._body(resp)["target"] == "chat-2"

    def test_stop_renders_a_refusal_as_its_status_not_a_500(self, tmp_path, monkeypatch):
        """Same refusal contract the create and send routes hold."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/stop")

        async def _boom(*_a, **_kw):
            raise sc.SessionControlError(
                "not addressable", status=403, code="linked_session_target"
            )

        monkeypatch.setattr(sc, "stop_target", _boom)
        resp = asyncio.run(handlers_sc.api_session_control_stop(req))

        assert resp.status == 403
        assert self._body(resp)["code"] == "linked_session_target"

    def test_close_without_the_secret_is_forbidden(self, tmp_path):
        req = self._request(tmp_path, internal=False, path="/api/session-control/close")
        resp = asyncio.run(handlers_sc.api_session_control_close(req))
        assert resp.status == 403
        assert self._body(resp)["code"] == "internal_secret_required"

    def test_close_with_the_secret_reaches_the_operation(self, tmp_path, monkeypatch):
        """The close ROUTE's success path, for the reason create's docstring gives:
        a handler wired only at the business layer ships dead if the route itself
        refuses or never reaches the operation."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/close")

        async def _ok(*_a, **_kw):
            return {"ok": True, "target": "chat-2"}

        monkeypatch.setattr(sc, "close_target", _ok)
        resp = asyncio.run(handlers_sc.api_session_control_close(req))

        assert resp.status == 200
        assert self._body(resp)["target"] == "chat-2"

    def test_close_renders_a_refusal_as_its_status_not_a_500(self, tmp_path, monkeypatch):
        """Same refusal contract the other routes hold — including the close-path
        failure codes, which arrive as their own 500 rather than an unhandled crash."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/close")

        async def _boom(*_a, **_kw):
            raise sc.SessionControlError(
                "failed to save history", status=500, code="history_save_failed"
            )

        monkeypatch.setattr(sc, "close_target", _boom)
        resp = asyncio.run(handlers_sc.api_session_control_close(req))

        assert resp.status == 500
        assert self._body(resp)["code"] == "history_save_failed"

    def test_send_without_the_secret_is_forbidden(self, tmp_path):
        req = self._request(tmp_path, internal=False, path="/api/session-control/send")
        resp = asyncio.run(handlers_sc.api_session_control_send(req))
        assert resp.status == 403
        assert self._body(resp)["code"] == "internal_secret_required"

    def test_send_with_the_secret_delivers_to_the_target(self, tmp_path, monkeypatch):
        """The send ROUTE needs its own test for the reason create's docstring gives:
        a handler wired only in tests at the business layer ships dead if the route
        itself refuses or never reaches the operation."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/send")
        state = req.app["state"]
        caller = state.get_slot("chat-1")
        assert caller is not None
        # Make chat-2 an addressable peer of the caller through the same helper the
        # business-layer tests use, so this exercises the route rather than a
        # hand-built slot that authorize_target would refuse for unrelated reasons.
        _peer_target(state, "chat-2", caller)

        async def _fake_run_chat(_state, _slot, _prompt):
            return None

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

        resp = asyncio.run(handlers_sc.api_session_control_send(req))

        assert resp.status == 200, self._body(resp)
        payload = self._body(resp)
        assert payload["ok"] is True
        assert payload["target"] == "chat-2"

    def test_send_requires_a_non_empty_message(self, tmp_path):
        """The message check lives in the ROUTE, not the business layer, so a
        whitespace-only body must be refused here rather than delivered as a
        blank turn."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/send")

        async def _json():
            return {"target": "chat-2", "message": "   "}

        req.json = _json
        resp = asyncio.run(handlers_sc.api_session_control_send(req))

        assert resp.status == 400
        assert self._body(resp)["code"] == "message_required"

    def test_send_renders_a_refusal_as_its_status_not_a_500(self, tmp_path, monkeypatch):
        """A SessionControlError from send comes back as its own refusal, the same
        contract create's route holds."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/send")

        async def _boom(*_a, **_kw):
            raise sc.SessionControlError("busy", status=409, code="target_busy")

        monkeypatch.setattr(sc, "send_to_target", _boom)
        resp = asyncio.run(handlers_sc.api_session_control_send(req))

        assert resp.status == 409
        assert self._body(resp)["code"] == "target_busy"

    def test_read_without_the_secret_is_forbidden(self, tmp_path):
        req = self._request(
            tmp_path, internal=False, path="/api/session-control/read", method="GET"
        )
        resp = asyncio.run(handlers_sc.api_session_control_read(req))
        assert resp.status == 403

    def test_read_with_the_secret_reaches_the_operation(self, tmp_path):
        """The guard must not refuse an authentic caller."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/read", method="GET")
        resp = asyncio.run(handlers_sc.api_session_control_read(req))
        assert resp.status == 200
        assert self._body(resp)["target"] == "chat-2"


# ── The config switch ────────────────────────────────────────────────────────


def test_the_switch_is_on_by_default_and_an_explicit_false_still_disables():
    """``agent.session_control`` defaults ON; the agent config is the real grant.

    Who may reach a peer session is decided by the AGENT CONFIG, not by this
    switch: the tools come from the `kirocrew-dashboard` MCP server, so an agent
    that does not mount it never has them -- the same rule as every other MCP
    server. A second default-off gate on top of that only made the capability
    unreachable for an agent whose spec had already been given it deliberately.

    What the switch is still for is a single withdrawal: an operator who wants it
    gone from every agent at once, without editing each spec. So the one direction
    that must keep working is an EXPLICIT ``false``:

    * **Absent.** Both the ``.get`` default and the dataclass field default are
      ``True``, so a mounted server works with nothing else written down.
    * **Malformed.** ``bool("false")`` is ``True``, so a plain coercion loads a
      quoted opt-out as ENABLED and a user who wrote it in an editor that quotes
      values would keep cross-session control on while believing it off.
      ``_safe_bool`` accepts only a real bool.

    Asserted on the source rather than through ``KiroCrewConfig.load()``:
    ``load()`` merges the real data home's ``config.local.json`` and serves a
    fingerprint-cached dict, so a per-field assertion through it depends on the
    developer's own config rather than on the payload under test. The parse is
    one inline expression with no seam to call directly, so the wiring itself is
    what gets pinned.
    """
    src = Path(loader.__file__).read_text(encoding="utf-8")
    parse = re.search(r"^\s*session_control=(.+)$", src, re.MULTILINE)
    assert parse is not None, "the session_control parse line is gone"
    wiring = parse.group(1).strip().rstrip(",")
    assert wiring.startswith(
        "_safe_bool("
    ), f"session_control must be parsed through _safe_bool, got: {wiring}"
    assert (
        '"session_control", True' in wiring
    ), f"an absent setting must read as ENABLED -- the mount is the grant, got: {wiring}"
    # The field default is the second absent path: it is what a config object
    # built without going through the loader resolves to, and it must agree with
    # the loader or the answer depends on which path produced the config.
    assert loader.AgentConfig().session_control is True, (
        "the dataclass default must also be True, or a config built outside the "
        "loader disables a capability the agent's own spec was given"
    )
    # An explicit opt-out is the direction that still has to hold, including the
    # quoted form `_safe_bool` exists for.
    assert loader._safe_bool(False, True) is False
    assert loader._safe_bool("false", True) is True, (
        "a quoted value is not a bool, so it falls back rather than being coerced "
        "-- the operator who means it writes a real false"
    )


def test_a_config_read_that_raises_disables_the_feature(monkeypatch):
    """Unrelated config corruption must not undo an explicit opt-out.

    Mutation guard: returning True here means a malformed section elsewhere in
    config.json silently re-enables cross-session control.
    """

    class _Exploding:
        @staticmethod
        def load():
            raise ValueError("malformed knowledge.auto_ingest_artifact_kinds")

    monkeypatch.setattr(sc, "KiroCrewConfig", _Exploding)
    assert _REAL_ENABLED() is False


def test_safe_bool_rejects_every_non_bool():
    """The helper the wiring above depends on: only a real bool survives."""
    for bad in ("false", "true", "yes", 1, 0, [], {}, None):
        assert loader._safe_bool(bad, False) is False, bad
    assert loader._safe_bool(True, False) is True
    assert loader._safe_bool(False, True) is False


# ── Reading: cursor semantics ────────────────────────────────────────────────


def test_completion_markers_never_advance_the_cursor(tmp_path):
    """``done`` rows are appended but never persisted, so counting them drifts.

    Mutation guard: a cursor that counts them names a position rehydration
    cannot reproduce, so ``since=total`` skips real messages after a restart.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.append("assistant", "the answer", "msg msg-a")
    target.append("done", "", "done")

    out = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")

    assert out["total"] == 1, "a done marker must not count toward the cursor"
    assert [m["role"] for m in out["messages"]] == ["assistant"]


def test_a_cursor_past_the_end_is_refused_rather_than_clamped(tmp_path):
    """A shrunk transcript must not silently drop the rows that replaced the old tail.

    Rewind and regenerate move ``total`` backwards under a caller still holding the
    old position. Clamping that cursor to ``total`` starts the read at the end, so
    every replacement row below it is skipped permanently and nothing in the
    response says so -- the same silent-mis-window failure the trimmed-session
    guard already refuses loudly.

    Mutation guard: restoring `min(since, total)` returns rows instead of raising.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    for n in range(4):
        target.append("user", f"m{n}", "msg msg-u")

    with pytest.raises(sc.SessionControlError) as exc:
        sc.read_messages(state, caller_session_key=_key(caller), target="chat-2", since=99)

    assert exc.value.code == "cursor_unavailable"
    assert exc.value.status == 409


def test_a_credential_at_the_truncation_boundary_is_still_redacted(tmp_path):
    """Redaction runs over the whole message, then the slice happens.

    Mutation guard: truncating first cuts the secret into a prefix the scanner
    no longer matches, and that fragment ships to the caller.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    secret = "ghp_" + "B" * 36
    # Straddle the boundary: only the first 10 chars of the secret survive a
    # naive slice, and a 10-char fragment no longer matches the credential
    # scanner — so it is exactly what leaks when the order is wrong.
    filler = "x" * (sc.MAX_READ_CONTENT_CHARS - 10)
    surviving_fragment = secret[:10]
    target.append("assistant", filler + secret, "msg msg-a")

    row = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")["messages"][0]

    assert (
        surviving_fragment not in row["content"]
    ), "a truncated credential prefix escaped redaction"
    assert row["truncated"] is True


def test_the_cursor_stops_before_the_streaming_tail(tmp_path):
    """Chunk rows are deleted when the segment flushes, so the cursor skips them.

    Mutation guard: counting them inflates ``total``; the flush shrinks the list
    back under it, and the next ``since=total`` read misses the finished reply
    permanently.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.append("user", "go", "msg msg-u")
    for piece in ("par", "tial", " reply"):
        target.append("chunk", piece, "")

    mid = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")

    assert mid["total"] == 1, "streaming chunks must not advance the cursor"
    assert [m["role"] for m in mid["messages"]] == ["user"]
    assert mid["streaming"] is True

    # Stand in for _flush_segment: drop the trailing chunk run, append the real
    # assistant message in its place.
    del target.messages[1:]
    target.append("assistant", "partial reply", "msg msg-a")

    after = sc.read_messages(
        state, caller_session_key=_key(caller), target="chat-2", since=mid["total"]
    )

    assert [m["content"] for m in after["messages"]] == ["partial reply"]
    assert "streaming" not in after


# ── Reading ──────────────────────────────────────────────────────────────────


def test_read_returns_the_tail_with_a_total_to_poll_from(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    for i in range(5):
        target.append("assistant", f"line {i}", "msg msg-a")

    out = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2", limit=2)

    assert out["total"] == 5
    assert [m["content"] for m in out["messages"]] == ["line 3", "line 4"]
    assert [m["index"] for m in out["messages"]] == [3, 4]
    assert out["running"] is False


def test_read_since_returns_only_what_arrived_after(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    for i in range(3):
        target.append("assistant", f"old {i}", "msg msg-a")
    first = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")
    target.append("assistant", "brand new", "msg msg-a")

    second = sc.read_messages(
        state, caller_session_key=_key(caller), target="chat-2", since=first["total"]
    )

    assert [m["content"] for m in second["messages"]] == ["brand new"]


def test_a_stale_cursor_after_a_shrink_is_recoverable(tmp_path):
    """A compacted transcript shrinks; the poller must be able to SEE what replaced it.

    Clamping the stale cursor to ``total`` looks friendlier -- it answers
    ``messages: []`` and hands back a plausible cursor -- but the rows below the
    clamp are the replacement content, and a cursor never moves backwards, so they
    are skipped permanently while the response reads as "nothing new". Refusing
    sends the caller to a tail read, which actually returns that content, so the
    loud answer is the recoverable one.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.append("assistant", "what replaced the old tail", "msg msg-a")

    with pytest.raises(sc.SessionControlError) as exc:
        sc.read_messages(state, caller_session_key=_key(caller), target="chat-2", since=999)
    assert exc.value.code == "cursor_unavailable"

    # The refusal names the fallback, and the fallback shows the replacement row.
    recovered = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")
    assert [m["content"] for m in recovered["messages"]] == ["what replaced the old tail"]

    # A cursor exactly AT the end is not stale -- it is an up-to-date poller.
    caught_up = sc.read_messages(
        state, caller_session_key=_key(caller), target="chat-2", since=recovered["total"]
    )
    assert caught_up["messages"] == []


def test_read_truncates_a_huge_message_and_says_so(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.append("assistant", "y" * (sc.MAX_READ_CONTENT_CHARS + 500), "msg msg-a")

    row = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")["messages"][0]

    assert row["truncated"] is True
    assert len(row["content"]) <= sc.MAX_READ_CONTENT_CHARS


def test_read_rejects_an_out_of_range_limit(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _slot(state, "chat-2")
    with pytest.raises(sc.SessionControlError):
        sc.read_messages(
            state,
            caller_session_key=_key(caller),
            target="chat-2",
            limit=sc.MAX_READ_MESSAGES + 1,
        )


def test_the_read_cursor_is_absolute_across_a_trimmed_window(tmp_path):
    """Window length freezes at the retention cap; `total` and the indexes must not.

    Mutation guard: deriving `total` from `len(slot.messages)` makes it freeze at
    the cap, so a caller can no longer tell how much history exists. Basing it on
    `_disk_older_count` instead of the durable counter shifts every position by
    the transient rows that were trimmed (here: 20), which this pins.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    for i in range(3):
        target.append("assistant", f"live {i}", "msg msg-a")
    # Stand in for the trim: 5,000 rows aged into the frozen prefix, of which
    # 20 were transient (never returned by a durable read).
    target._disk_older_count = 5000
    target._disk_older_durable_count = 4980

    out = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")

    assert out["total"] == 4983, "total must count the DURABLE frozen prefix + the window"
    assert [m["index"] for m in out["messages"]] == [4980, 4981, 4982]
    # Positions are durable-only on both sides of the trim boundary, so the
    # cursor stays exact and is still offered on a trimmed session.
    assert out["next_since"] == 4983
    assert "cursor_exact" not in out, "an exact cursor needs no caveat"


def test_cursor_pagination_stays_exact_once_rows_have_been_trimmed(tmp_path):
    """Two consecutive `since` pages on a trimmed session never overlap.

    This is the regression the durable counter exists for: `_disk_older_count`
    counts every trimmed row including transient ones, so basing positions on it
    advances the base with no durable row behind it — every position shifts and
    a `since` read serves a durable message the caller already had. Basing on
    the durable-only counter keeps the two spaces aligned.

    Mutation guard: swapping the base back to `_disk_older_count` makes the
    second page re-serve the first page's rows (an overlap this asserts away);
    on the pre-fix code the first `since` read raised `cursor_unavailable`.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    for i in range(4):
        target.append("assistant", f"live {i}", "msg msg-a")
    # A trim that folded transient rows into the frozen prefix: the all-rows
    # counter and the durable counter disagree by 20.
    target._disk_older_count = 5000
    target._disk_older_durable_count = 4980

    first = sc.read_messages(
        state, caller_session_key=_key(caller), target="chat-2", since=4980, limit=2
    )
    assert [m["content"] for m in first["messages"]] == ["live 0", "live 1"]
    assert first["next_since"] == 4982

    second = sc.read_messages(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        since=first["next_since"],
        limit=2,
    )
    assert [m["content"] for m in second["messages"]] == ["live 2", "live 3"]

    served_once = {m["index"] for m in first["messages"]}
    assert served_once.isdisjoint(
        {m["index"] for m in second["messages"]}
    ), "consecutive since pages must never re-serve a row"


def test_a_since_read_on_a_trimmed_session_returns_a_cursor_not_a_409(tmp_path):
    """A trimmed session answers `since` reads exactly instead of refusing.

    The old behaviour raised 409 `cursor_unavailable` for ANY `since` read once
    the frozen-prefix base was non-zero, and withheld `next_since`, so pollers
    on exactly the long-lived sessions worth polling fell back to inexact tail
    reads forever. With a durable-only base the positions are exact again.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.append("assistant", "the reply", "msg msg-a")
    target._disk_older_count = 100
    target._disk_older_durable_count = 90

    out = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2", since=90)

    assert [m["content"] for m in out["messages"]] == ["the reply"]
    assert out["next_since"] == 91
    assert out["total"] == 91


def test_a_cursor_under_the_trimmed_prefix_is_refused_not_skipped_over(tmp_path):
    """A `since` below the durable base cannot be served from memory.

    The rows in ``[since, base)`` exist only on disk; starting the read at the
    window instead would silently skip them, which is the same silent-gap
    failure the past-the-end refusal exists for. Refusing loudly sends the
    caller to a tail read.

    Mutation guard: clamping the offset to 0 returns the window as if it were
    the rows asked for.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.append("assistant", "newest", "msg msg-a")
    target._disk_older_count = 100
    target._disk_older_durable_count = 90

    with pytest.raises(sc.SessionControlError) as exc:
        sc.read_messages(state, caller_session_key=_key(caller), target="chat-2", since=50)
    assert exc.value.code == "cursor_unavailable"
    assert exc.value.status == 409

    # The tail read is the documented fallback and still works.
    tail = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")
    assert [m["content"] for m in tail["messages"]] == ["newest"]


def test_an_untrimmed_session_still_reports_a_gap_free_cursor(tmp_path):
    """With nothing trimmed the cursor is exact, which is the common case.

    An even older version of this test asserted a `trimmed` gap report on a
    session whose rows HAD aged out — that gap count was derived from the mixed
    all-rows counter that made the positions wrong. Positions are now based on
    the durable-only prefix counter, so trimmed sessions get exact cursors too
    (see the trimmed-window tests above) and no gap report exists on any path.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    for i in range(4):
        target.append("assistant", f"row {i}", "msg msg-a")

    out = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2", since=2)

    assert [m["content"] for m in out["messages"]] == ["row 2", "row 3"]
    assert out["next_since"] == 4 == out["total"]
    assert "trimmed" not in out
    assert "cursor_exact" not in out, "an exact cursor needs no caveat"


def test_read_reports_the_targets_queue_depth(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.queue_append("waiting")

    out = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")

    assert out["queue_depth"] == 1


# ── Stopping ─────────────────────────────────────────────────────────────────


def test_birth_metadata_carries_the_slots_own_identity_and_origin(tmp_path, monkeypatch):
    """A created-then-idle session's only disk record is this metadata line.

    `_save_slot_to_history` returns early on an empty message window, so a session
    that is created and never messaged never reaches the normal save path. Omitting
    `tab_id` makes rehydrate mint a fresh one -- which breaks the ownership match
    this design depends on -- and omitting `origin` makes it come back
    unattributed, dropping it out of `slots:user`.

    Mutation guard: removing either key from the dict fails here.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)

    result = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    created = state.get_slot(result["target"])
    written = state.conversation_log.get_metadata(sc.slot_history_key(created))

    assert written.get("tab_id") == created._tab_id, (
        "tab_id must reach the birth metadata -- without it a restart remints the "
        "slot's identity and ownership stops matching"
    )
    assert written.get("origin") == created._origin, (
        "origin must reach the birth metadata -- without it the session comes back "
        "unattributed and leaves slots:user"
    )


def test_nothing_suspends_between_the_stop_gate_and_the_stop(tmp_path, monkeypatch):
    """The SEL prewarm must run BEFORE authorization, not between it and the act.

    `await` is a suspension point. With the prewarm sitting between
    `authorize_target` and `stop_slot_turn`, a user action landing in that window
    -- linking the target to a channel -- makes the decision stale, and a turn gets
    cancelled on a session that became channel-backed after `mirrored_target`
    already passed. `send_message` documents the same rule for its cooldown claim.

    Mutation guard: moving the prewarm back below the gate flips this order.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    order: list[str] = []

    def _sel():
        order.append("sel")
        return MagicMock()

    real_authorize = sc.authorize_target

    def _authorize(*a, **kw):
        order.append("authorize")
        return real_authorize(*a, **kw)

    async def _fake_stop(_state, slot, *, source, escalate=True):
        order.append("stop")
        return {"ok": True}

    monkeypatch.setattr(sc, "sel", _sel)
    monkeypatch.setattr(sc, "authorize_target", _authorize)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", _fake_stop)

    asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target=target.key))

    assert order[:1] == ["sel"], f"the prewarm must precede authorization; got {order}"
    assert order.index("authorize") < order.index("stop")
    # The decisive assertion: authorization and the act must be ADJACENT, so no
    # await can separate them.
    assert (
        order.index("stop") - order.index("authorize") == 1
    ), f"something ran between the gate and the act it authorizes: {order}"


def test_the_config_warm_is_the_last_suspension_before_the_stop_gate(tmp_path, monkeypatch):
    """A warm with an `await` after it is no warm at all.

    `session_control_enabled` is synchronous and its `KiroCrewConfig.load()`
    re-reads and validates the file on the first call after a config edit. The warm
    exists so the gate's own read is a cache hit -- but a config edit landing in any
    suspension BETWEEN the warm and the gate changes the fingerprint, so the gate
    misses the cache and does that read on the loop regardless.

    `stop_target` has its own SEL prewarm await, so warming in the handler (before
    the body read AND before that prewarm) left two suspensions in the gap. The warm
    therefore belongs immediately after the SEL prewarm.

    Mutation guard: moving the warm back above the SEL prewarm, or into the handler,
    puts `sel` between it and `authorize`.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    order: list[str] = []

    def _sel():
        order.append("sel")
        return MagicMock()

    real_authorize = sc.authorize_target

    def _authorize(*a, **kw):
        order.append("authorize")
        return real_authorize(*a, **kw)

    def _enabled():
        order.append("warm")
        return True

    async def _fake_stop(_state, slot, *, source, escalate=True):
        order.append("stop")
        return {"ok": True}

    monkeypatch.setattr(sc, "sel", _sel)
    monkeypatch.setattr(sc, "authorize_target", _authorize)
    monkeypatch.setattr(sc, "session_control_enabled", _enabled)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", _fake_stop)

    asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target=target.key))

    assert "warm" in order, "the config was never warmed"
    # The warm runs off-loop via to_thread, so it is a suspension; nothing that
    # suspends may follow it before the gate reads config.
    assert order.index("sel") < order.index("warm"), (
        "the config warm must come AFTER the SEL prewarm -- that prewarm is an "
        f"await, and it would otherwise invalidate the warm: {order}"
    )
    assert order.index("warm") < order.index("authorize"), f"warm must precede the gate: {order}"


def test_create_warms_the_config_after_reading_the_body():
    """The body read suspends, so a warm above it can be stale by the gate.

    `create_session`'s first statement is the synchronous enabled check, so the warm
    has to sit below `_body` -- which awaits `request.json()` -- and above the call.
    Asserted on source order because the suspension being guarded against is the
    handler's own `await`, which a runtime probe cannot observe without racing it.

    Mutation guard: moving the warm back above `_body` flips these positions.
    """
    src = Path(handlers_sc.__file__).read_text(encoding="utf-8")
    create = src[src.index("async def api_session_control_create") :]
    create = create[: create.index("async def api_session_control_stop")]
    body_at = create.index("await _body(request)")
    warm_at = create.index("await sc.prewarm_enabled_check()")
    call_at = create.index("await sc.create_session(")
    assert body_at < warm_at < call_at, (
        "the config warm must sit between the body read and create_session, so no "
        "await separates it from create_session's own synchronous gate"
    )


def test_stop_constructs_the_sel_off_loop_before_stopping(tmp_path, monkeypatch):
    """`stop_slot_turn`'s idle branch logs with no await before it.

    So a first `session_stop` on a fresh gateway would construct the log on the
    loop. Mutation guard: dropping the prewarm runs `sel()` on the loop thread.
    """
    import threading

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    seen: dict[str, int] = {}

    def _sel():
        seen.setdefault("thread", threading.get_ident())
        return MagicMock()

    monkeypatch.setattr(sc, "sel", _sel)

    async def _fake_stop(_state, slot, *, source, escalate=True):
        return {"ok": True}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", _fake_stop)

    async def _drive() -> int:
        await sc.stop_target(state, caller_session_key=_key(caller), target=target.key)
        return threading.get_ident()

    loop_thread = asyncio.run(_drive())

    assert "thread" in seen, "the SEL was never constructed"
    assert (
        seen["thread"] != loop_thread
    ), "the SEL must be constructed off the loop thread before the stop"


def test_stop_goes_through_the_same_path_as_the_stop_button(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    _busy(target)

    seen: dict[str, object] = {}

    async def _fake_stop(_state, slot, *, source, escalate=True):
        seen["slot"] = slot.key
        seen["source"] = source
        seen["escalate"] = escalate
        return {"ok": True}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", _fake_stop)

    out = asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))

    assert out["target"] == "chat-2"
    # No `force`: `stop_slot_turn` escalates on a second press regardless of one,
    # so the tool does not advertise a flag a first call cannot honour. A FIRST
    # stop still carries `escalate=True` — the retry guard withholds it only for a
    # repeat, so the button's semantics are unchanged for a call that is not one.
    assert seen == {"slot": "chat-2", "source": "session_control", "escalate": True}


def test_stop_is_refused_for_a_session_out_of_bounds(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _slot(state, "chat-hidden", memory_mode="incognito")
    with pytest.raises(sc.SessionControlError):
        asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-hidden"))


# ── session_stop is safe to re-send (#5074) ──────────────────────────────────


def _stoppable(state, slot):
    """A target with a live turn and unconsumed work behind it.

    Both lists are what the hard-kill path clears, so they are the evidence a
    retry read as an escalation would destroy. ``stop_turn`` answers "cancelled"
    rather than "idle" so the soft stop stays PENDING — the state a retry arrives
    into, and the only state from which escalation is reachable at all.
    """
    _busy(slot)
    slot._queue.append({"id": "q1", "content": "the next thing"})
    slot._pending_steers.append("steer-1")
    slot._steer_delivery_ids["steer-1"] = "delivery-1"
    state.sessions.stop_turn = AsyncMock(return_value="cancelled")
    return slot


def test_a_retried_stop_keeps_the_queue_instead_of_escalating(tmp_path, monkeypatch):
    """The defect: a timeout retry is read as a second press and discards work.

    `stop_slot_turn` hard-kills on any second stop while the first is still
    pending, and the kill clears `_queue` and `_pending_steers`. An MCP client
    that got no response inside `_post`'s 30s timeout re-sends the same request,
    so a caller that asked once silently got the destructive variant — the
    exposure is worst for the unattended agent this verb exists for, which
    retries without anyone deciding anything.

    Mutation guard: dropping `escalate=` from `stop_target`'s call, or the
    `escalate and` guard in `stop_slot_turn`, empties both lists here and leaves
    `_stop_state` at "killing".
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _stoppable(state, _peer_target(state, "chat-2", caller))
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    first = asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))
    assert target._stop_state == "soft_pending", "fixture must leave a stop pending"

    second = asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))

    assert first.get("info") is None, "the first stop is the real cooperative one"
    assert second["info"] == "stop already in progress"
    assert target._stop_state == "soft_pending", "the retry escalated to a hard kill"
    assert [q["id"] for q in target._queue] == ["q1"], "the retry discarded queued work"
    assert target._pending_steers == ["steer-1"], "the retry discarded a pending steer"
    assert target._steer_delivery_ids == {"steer-1": "delivery-1"}


def test_a_repeat_still_stops_a_target_that_started_running_again(tmp_path, monkeypatch):
    """Withholding the escalation must not withhold the STOP.

    The retry guard suppresses a kill, not a cancel. A repeat that arrives after
    the first stop has settled and the target has picked up its next turn is a
    plain first stop as far as that turn is concerned, and has to cancel it.

    Mutation guard: short-circuiting `stop_target` on a repeat — returning the
    first call's answer without calling `stop_slot_turn` — leaves this second turn
    running.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _stoppable(state, _peer_target(state, "chat-2", caller))
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))
    # The first stop lands and the target drains its queue into a new turn.
    target._stop_state = "idle"
    target._stop_event_id = None

    out = asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))

    assert out.get("info") is None, "a repeat against a fresh turn is a real stop"
    assert target._stop_state == "soft_pending"
    assert state.sessions.stop_turn.await_count == 2


def test_a_stop_after_the_window_still_escalates(tmp_path, monkeypatch):
    """Escalation is delayed, not removed.

    A stop that STILL finds the target winding down once the window has closed is
    the case escalating was written for, and the capability has to survive: the
    alternative contract (never escalate from the RPC) was rejected because it
    takes the hard kill away from the agent surface entirely.

    Mutation guard: withholding escalation unconditionally leaves `_stop_state` at
    "soft_pending" and the queue intact.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _stoppable(state, _peer_target(state, "chat-2", caller))
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))
    # The window closes, so the next stop is a decision rather than a retry.
    monkeypatch.setattr(stop_retry, "WINDOW_SECS", 0.0)

    asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))

    assert target._stop_state == "killing"
    assert list(target._queue) == [], "an escalation discards the queue, by design"


def test_a_withheld_escalation_is_recorded(tmp_path, monkeypatch):
    """#5074 read from the other side: the absorbed retry must be visible too.

    The issue's complaint is that queued messages went "with no record that a
    retry rather than a decision caused it". Suppressing the kill silently would
    leave the same gap inverted — an audit in which a de-duplicated retry is
    indistinguishable from a stop nobody made.

    Mutation guard: dropping the metadata key.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _stoppable(state, _peer_target(state, "chat-2", caller))
    logged: list[dict] = []
    fake_sel = MagicMock()
    fake_sel.log_tool_invocation = lambda **kw: logged.append(kw)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: fake_sel)

    asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))
    asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))

    noop = [row for row in logged if row.get("outcome") == "noop"]
    assert noop, f"the retry did not reach the no-op branch: {[r.get('outcome') for r in logged]}"
    assert noop[-1]["metadata"].get("escalation_withheld") is True


def test_the_stop_button_still_escalates_on_a_second_press(tmp_path, monkeypatch):
    """The button's contract is unchanged, and that is the point of the default.

    A person pressing Stop again has watched the cooperative stop fail to take, so
    the second press IS a decision. Only the RPC, which cannot tell a decision
    from a re-sent request, gives that up.

    Mutation guard: defaulting `escalate` to False in `stop_slot_turn` breaks this
    without touching the session-control tests above.
    """
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _make_state(tmp_path)
    slot = _stoppable(state, _slot(state, "chat-1"))
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    asyncio.run(stop_slot_turn(state, slot))
    assert slot._stop_state == "soft_pending"

    asyncio.run(stop_slot_turn(state, slot))

    assert slot._stop_state == "killing"
    assert list(slot._queue) == []


def test_the_no_op_reply_says_which_of_its_two_facts_it_hit(tmp_path, monkeypatch):
    """`info` alone merges "was never running" with "its cancel is in flight".

    The de-duplicated retry lands on the second one routinely now, and a caller
    that renders both alike tells that caller the opposite of what happened.

    Mutation guard: hardcoding `already_stopping` either way collapses the two.
    """
    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _make_state(tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    stopping = _stoppable(state, _slot(state, "chat-1"))
    asyncio.run(stop_slot_turn(state, stopping))
    still_stopping = asyncio.run(stop_slot_turn(state, stopping, escalate=False))
    assert still_stopping == {
        "ok": True,
        "info": "stop already in progress",
        "already_stopping": True,
    }

    idle = _slot(state, "chat-2")
    assert asyncio.run(stop_slot_turn(state, idle)) == {
        "ok": True,
        "info": "not running",
        "already_stopping": False,
    }


def test_the_window_is_anchored_at_the_first_stop_not_slid_by_repeats():
    """Escalation is suppressed for ONE window, not for as long as retries arrive.

    A sliding window would put a hard kill out of reach of any caller polling
    faster than the window — trading a silent queue loss for a capability that can
    never be reached again.

    Mutation guard: refreshing the stored timestamp on a repeat makes the third
    call read as a repeat as well.
    """
    first_at = 100.0
    assert stop_retry.allow_escalation("chat-1", "chat-2", now=first_at) is True
    assert stop_retry.allow_escalation("chat-1", "chat-2", now=first_at + 80.0) is False
    assert (
        stop_retry.allow_escalation("chat-1", "chat-2", now=first_at + stop_retry.WINDOW_SECS)
        is True
    )


def test_the_window_outlasts_the_request_timeout_it_absorbs():
    """A window at or under `_post`'s 30s timeout expires before its own retry.

    The retry this exists to absorb cannot be sent until the first request has
    timed out, so the window is sized against that number rather than picked.
    """
    assert stop_retry.WINDOW_SECS > 30.0


def test_the_window_is_keyed_per_caller_and_target():
    """A different caller's FIRST stop is its own decision, not somebody's retry.

    Keying on the target alone would suppress that call — removing escalation from
    the RPC rather than making a retry safe.
    """
    assert stop_retry.allow_escalation("chat-1", "chat-2", now=100.0) is True
    assert stop_retry.allow_escalation("chat-9", "chat-2", now=100.0) is True
    assert stop_retry.allow_escalation("chat-1", "chat-3", now=100.0) is True
    assert stop_retry.allow_escalation("chat-1", "chat-2", now=100.0) is False


def test_an_unattributable_stop_cannot_escalate():
    """Fails closed: an empty key cannot be matched against a first call.

    Granting the kill there would hand the destructive variant to exactly the
    caller whose retries cannot be recognized. Withholding costs only the
    escalation — the cooperative stop still lands.
    """
    assert stop_retry.allow_escalation("", "chat-2", now=100.0) is False
    assert stop_retry.allow_escalation("chat-1", "", now=100.0) is False


def test_expired_windows_are_swept_rather_than_accumulated():
    """The map must not grow for the gateway's lifetime.

    Mutation guard: dropping the sweep keeps both keys, so a long-lived gateway
    holds one entry per pair of slots that ever stopped each other.
    """
    stop_retry.allow_escalation("chat-1", "chat-2", now=100.0)
    stop_retry.allow_escalation("chat-3", "chat-4", now=100.0)
    stop_retry.allow_escalation("chat-5", "chat-6", now=100.0 + stop_retry.WINDOW_SECS + 1.0)
    assert list(stop_retry._windows) == [("chat-5", "chat-6")]


# ── session_send ──


def test_send_to_an_idle_target_starts_a_turn_with_provenance(tmp_path, monkeypatch):
    """The delivered prompt carries the caller tag, and an idle target runs now.

    Provenance is the load-bearing part: the target renders the message as a
    user row, and without the tag it is indistinguishable from something the
    person typed into that session themselves.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)

    ran: dict[str, str] = {}

    async def _fake_run_chat(_state, slot, prompt):
        ran["slot"] = slot.key
        ran["prompt"] = prompt

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

    async def _drive():
        out = await sc.send_to_target(
            state, caller_session_key=_key(caller), target="chat-2", message="do the thing"
        )
        # Let the enqueue_or_run_prompt task actually execute the fake turn.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return out

    out = asyncio.run(_drive())

    assert out == {"ok": True, "target": "chat-2", "started": True}
    assert ran["slot"] == "chat-2"
    assert ran["prompt"].startswith("[sent by session ")
    assert ran["prompt"].endswith("do the thing")
    # The transcript shows the message as a user row, tag included.
    assert any(
        m.get("content", "").endswith("do the thing")
        for m in target.messages
        if m.get("role") == "user"
    )


def test_the_sent_body_passes_through_the_outbound_guard(tmp_path, monkeypatch):
    """The message goes through `sanitize_outbound` before it is persisted.

    It arrives from ANOTHER session's model, is appended to the target's
    transcript as a user row and broadcast to every dashboard client, so this is
    the same surface the steer path sanitizes before `slot.append`
    (`chat_delivery`: "raw content must never reach an external surface"). A
    credential-shaped string would otherwise be stored and displayed.

    Mutation guard: dropping the `sanitize_outbound` call leaves the raw value.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    monkeypatch.setattr(sc, "sanitize_outbound", lambda s: f"SANITIZED:{s}")

    ran: dict[str, str] = {}

    async def _fake_run_chat(_state, slot, prompt):
        ran["prompt"] = prompt

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

    async def _drive():
        out = await sc.send_to_target(
            state, caller_session_key=_key(caller), target="chat-2", message="tok=abc"
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return out

    asyncio.run(_drive())

    assert ran["prompt"].endswith(
        "SANITIZED:tok=abc"
    ), "the sent body must pass through the outbound guard before it is delivered"
    # The provenance tag is built by this function, not caller-supplied, so it is
    # deliberately OUTSIDE the sanitized span.
    assert ran["prompt"].startswith("[sent by session ")
    assert any(
        m.get("content", "").endswith("SANITIZED:tok=abc")
        for m in target.messages
        if m.get("role") == "user"
    ), "the persisted transcript row must carry the sanitized body"


def test_the_length_gate_measures_the_raw_body(tmp_path, monkeypatch):
    """Validation happens on the RAW body, before redaction.

    Redaction can only shrink the text, so gating the raw form is the honest
    limit: a caller that sends 60K of credentials gets `message_too_long` rather
    than silently passing because redaction squeezed it under the cap.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _peer_target(state, "chat-2", caller)
    # A redactor that collapses everything would hide an oversized body.
    monkeypatch.setattr(sc, "sanitize_outbound", lambda _s: "x")

    with pytest.raises(sc.SessionControlError) as err:
        asyncio.run(
            sc.send_to_target(
                state,
                caller_session_key=_key(caller),
                target="chat-2",
                message="a" * (sc.MAX_SEND_MESSAGE_CHARS + 1),
            )
        )
    assert err.value.code == "message_too_long"


def test_send_to_a_busy_target_queues_instead_of_racing(tmp_path):
    """A mid-turn target must not get a second concurrent turn — the message
    queues, and the caller is told so (`started: False`), because "ran" and
    "will run later" are different answers to a coordinator."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    _busy(target)

    out = asyncio.run(
        sc.send_to_target(
            state, caller_session_key=_key(caller), target="chat-2", message="queued message"
        )
    )

    assert out["started"] is False
    assert any("queued message" in q.get("content", "") for q in target._queue)


def test_send_to_a_remote_bound_target_is_refused_not_run_locally(tmp_path, monkeypatch):
    """A crew-bound target executes on the peer; session_send must not run its
    turn on THIS machine.

    ``send_to_target`` hands ``_run_chat`` to ``enqueue_or_run_prompt``, which has
    no remote/executor branch — so a bound target would run the crew's work here
    and diverge the local and peer transcripts (GPT #7693). It is refused with a
    409 before any dispatch, and nothing is queued.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.executor = "remote"  # bound to a peer crew for execution

    ran: dict[str, str] = {}

    async def _fake_run_chat(_state, slot, prompt):
        ran["slot"] = slot.key

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

    with pytest.raises(sc.SessionControlError) as err:
        asyncio.run(
            sc.send_to_target(
                state, caller_session_key=_key(caller), target="chat-2", message="do it there"
            )
        )
    assert err.value.code == "remote_target_unsupported"
    # Refused before dispatch: no local turn ran and nothing was queued.
    assert ran == {}
    assert target._queue == []


def test_send_is_refused_for_a_session_out_of_bounds(tmp_path):
    """The same deny-by-default guard the other verbs share gates send too."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _slot(state, "chat-hidden", memory_mode="incognito")
    with pytest.raises(sc.SessionControlError):
        asyncio.run(
            sc.send_to_target(
                state, caller_session_key=_key(caller), target="chat-hidden", message="hi"
            )
        )


def test_send_refuses_an_empty_or_oversized_message(tmp_path):
    """Size gates live in the business layer, not only in the tool schema —
    the HTTP route is callable without the MCP layer's validation."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _peer_target(state, "chat-2", caller)

    with pytest.raises(sc.SessionControlError) as empty_exc:
        asyncio.run(
            sc.send_to_target(state, caller_session_key=_key(caller), target="chat-2", message="  ")
        )
    assert empty_exc.value.code == "message_empty"

    with pytest.raises(sc.SessionControlError) as long_exc:
        asyncio.run(
            sc.send_to_target(
                state,
                caller_session_key=_key(caller),
                target="chat-2",
                message="x" * (sc.MAX_SEND_MESSAGE_CHARS + 1),
            )
        )
    assert long_exc.value.code == "message_too_long"


def test_session_control_routes_are_strict_internal():
    """Every registered session-control route must sit in the strict bucket.

    The route table and `_STRICT_INTERNAL_API_PATHS` are two hand-maintained
    lists, and nothing else pairs them. An unlisted path falls through
    `token_auth`'s general branch, which honors only cookie/query tokens, so the
    MCP caller's `X-Internal-Secret` is ignored and the handler's own
    `internal_auth` re-assert refuses the call -- the tool is unreachable in
    production. The handler-level suites cannot catch that: they stub
    `request["internal_auth"]` and call handlers directly, bypassing the
    middleware entirely, so they stay green while the route is dead.

    Derived from the router rather than a literal list, so a fifth route fails
    here instead of shipping unreachable.
    """
    from aiohttp import web

    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS, _register_mcp_routes

    app = web.Application()
    _register_mcp_routes(app)

    registered = {
        resource.canonical
        for resource in app.router.resources()
        if str(getattr(resource, "canonical", "")).startswith("/api/session-control/")
    }
    assert registered, "no session-control routes found -- the derivation broke, not the wiring"

    missing = sorted(registered - set(_STRICT_INTERNAL_API_PATHS))
    assert not missing, (
        "session-control routes registered but absent from _STRICT_INTERNAL_API_PATHS "
        f"(unreachable in production, X-Internal-Secret ignored): {missing}"
    )


def test_create_refuses_the_caller_classes_authorize_target_refuses(tmp_path):
    """`create_session` must apply the SAME caller refusals as the other verbs.

    An owned child is sendable by construction, so a caller class refused a
    target but allowed to CREATE one gets there anyway: create, then send to
    what it just made. Mutation guard: dropping either check below lets an
    app-scoped or incognito session manufacture a persistent peer it owns.
    """
    state = _make_state(tmp_path)

    app_caller = _slot(state, "chat-app")
    app_caller._app = "some-app"
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(app_caller)))
    assert exc.value.code == "app_scoped_caller"

    ghost = _slot(state, "chat-ghost")
    ghost.memory_mode = "incognito"
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(ghost)))
    assert exc.value.code == "ephemeral_caller"

    linked = _slot(state, "chat-linked")
    linked.linked_session_key = "channel:1786300000.000100"
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(linked)))
    assert exc.value.code == "linked_session_caller"


def test_created_session_inherits_the_callers_workspace(tmp_path, monkeypatch):
    """The child lands in the caller's workspace, not the `default` one.

    Two consequences ride on this, and the second is why a plausible-looking
    default is not harmless: workspace is the memory boundary, AND
    `authorize_target` refuses a cross-workspace target -- so a child left in
    `default` is both a boundary crossing and unsendable by its own creator,
    which would make `session_create` useless outside the default workspace.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller.workspace = "research"
    # The caller's own agent is bound to its workspace by construction, and the
    # child inherits it -- so the binding check passes without naming an agent.
    caller.agent = "researcher"
    _agent_resolves(monkeypatch, "research")

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])
    assert child is not None
    assert child.workspace == "research", "the child must not fall back to 'default'"
    assert child.agent == "researcher", "an unnamed agent inherits the caller's"

    # And the creator can actually address it -- the property the default breaks.
    assert (
        sc.authorize_target(
            state,
            caller_session_key=_key(caller),
            target=created["target"],
            operation="read",
        ).key
        == created["target"]
    )


def test_create_checks_the_workspace_binding_even_when_no_agent_is_named(tmp_path, monkeypatch):
    """An omitted agent is not an unchecked agent.

    `resolve_agent_bindings` falls through to `config.default_agent` when no name
    is given, so the agent that ANSWERS may be bound to another workspace even
    though the caller named nothing. Authorization reads `slot.workspace` while
    execution follows that binding, so the same check has to cover this branch --
    which is exactly the branch the first version of this guard skipped.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller.workspace = "research"

    # The effective default resolves to a DIFFERENT workspace than the caller's.
    monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: "default")

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    assert exc.value.code == "agent_workspace_mismatch"
    assert "default agent" in str(exc.value), "the message should name what would answer"


def test_created_session_gets_its_workspace_project_dir(tmp_path):
    """cwd follows the workspace, or project-scoped resolution uses the wrong tree."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])
    assert child is not None
    assert child.project == loader.default_project_dir(child.workspace)


# ── session_create: the creator's trust grant ───────────────────────────────


def test_created_session_inherits_the_callers_trust(tmp_path):
    """A trusted creator's worker starts trusted, or dispatch stalls on a prompt.

    The whole point of dispatching a session is that the work proceeds without the
    operator sitting on it, and `spawn_run` subagents already start auto-approved
    off the parent's policy (`parent_trusted`). A child born interactive blocks on
    the first tool call with nobody watching -- the same failure, one layer up.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._trust = True

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])

    assert child is not None
    assert child._trust is True, "the creator's posture must follow the work"


def test_command_grants_never_transfer_even_under_full_trust(tmp_path):
    """`_trusted_patterns` are per-command grants, not a posture, so they stay put.

    A pattern is judged against the session the operator was LOOKING at, while a
    dispatched worker runs model-authored work they have not seen -- so the same
    glob can admit a command the grant was never asked about. Inheriting them also
    buys nothing where it would be safe: with `_trust` set the child already
    auto-approves through `_slot_is_trusted`, so the list is dead weight here and
    changes the outcome only in the case that must keep asking (see the sibling
    test below).
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._trust = True
    caller._trusted_patterns = {"npm test", "git status"}

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])

    assert child._trusted_patterns == set(), "command grants must not be delegated"
    # And nothing was aliased on the way past: the caller keeps its own grants.
    assert caller._trusted_patterns == {"npm test", "git status"}


def test_a_pattern_only_creator_produces_a_child_that_still_asks(tmp_path):
    """The case the exclusion exists for: grants without session trust.

    An operator who approved single commands and deliberately did NOT click trust
    has said "ask me". `chat_runner` matches `_trusted_patterns` independently of
    `_trust` (so an inherited pattern would auto-approve on its own), which is why
    a child of such a caller must be born with nothing.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._trust = False
    caller._trusted_patterns = {"npm test"}

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])

    assert child._trust is False
    assert child._trusted_patterns == set(), "a withheld posture must not leak as a grant"


def test_created_session_inherits_trust_reads(tmp_path):
    """`trust_reads` is a grant too, and it is the narrower one.

    Inheriting only the full grant would leave the read-only mode -- the setting a
    cautious operator picks -- as the one that still stalls its own workers.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._trust = False
    caller._trust_reads = True

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])

    assert child._trust_reads is True
    assert child._trust is False, "the narrow grant must not widen on the way down"


def test_an_untrusted_creator_makes_an_untrusted_child(tmp_path):
    """Negative control: nothing is granted that the creator did not hold.

    Without this the inheritance could be a constant `True` and every test above
    would still pass.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])

    assert child._trust is False
    assert child._trust_reads is False
    assert child._trusted_patterns == set()


def test_the_scoped_safety_override_grant_is_never_inherited(tmp_path):
    """`_trust_scope` is a revocable credential, not a setting to copy.

    Its entire value is being re-checked on every approval against a TTL-bounded,
    SEL-audited `SafetyOverride` scope. Forking the key hands a second session a
    grant whose revocation the create path cannot observe -- and the child would
    keep auto-approving after the scope that justified it is gone.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._trust_scope = "app:worker:abc123"

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])

    assert child._trust_scope == "", "a scoped grant must not fork onto the child"
    assert child._trust is False, "nor may it be laundered into the durable flag"


def test_trust_revoked_mid_create_is_not_inherited(tmp_path, monkeypatch):
    """The grant that transfers is the one held at ALLOCATION, not at entry.

    `create_session` suspends several times before the slot exists (project dir,
    config load, folder confirmation), and the operator can pick `normal` in any
    of those windows. Reading the entry-time slot would hand the child a grant
    that no longer exists. Simulated by revoking inside the project-dir
    resolution, the same interleaving the folder-delete test uses.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._trust = True

    def _revoke_then_resolve(_workspace):
        caller._trust = False
        caller._trust_reads = False
        return str(tmp_path)

    monkeypatch.setattr(sc, "default_project_dir", _revoke_then_resolve)

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])

    assert child._trust is False, "a revoked grant must not be resurrected by a create"
    assert child._trust_reads is False


def test_the_create_audit_records_what_the_child_was_born_with(tmp_path):
    """An auto-approval in the child must be traceable to the creator's grant.

    Without it, the SEL shows a session whose tools approve themselves and no
    record of where that authority came from. Recorded on both outcomes, so
    "false" is positive evidence the grant did not transfer.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._trust = True

    seen: dict[str, object] = {}

    def _capture(**kwargs):
        seen.update(kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sc, "_audit", _capture)
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    detail = seen["detail"]
    assert detail["inherited_trust"] == "true"
    assert detail["inherited_trust_reads"] == "false"


# ── session_create: filing at birth (#6118) ─────────────────────────────────


def test_create_schema_bounds_the_folder_reference():
    """`folder` takes an id OR a human path, bounded like every folder ref.

    The two readings share no charset, so the schema checks only the length --
    the same contract `chat_folder_move_session.folder` carries. Over-length is
    refused at validation, before any resolution work.
    """
    from kiro_crew.validation import SESSION_CREATE_SCHEMA, ValidationError, validate_tool_args

    out = validate_tool_args({"folder": "aaaaaaaaaaaa"}, SESSION_CREATE_SCHEMA)
    assert out["folder"] == "aaaaaaaaaaaa", "an id-shaped reference must pass"
    out = validate_tool_args({"folder": "Goals/Q3 push"}, SESSION_CREATE_SCHEMA)
    assert out["folder"] == "Goals/Q3 push", "a '/'-separated human path must pass"
    out = validate_tool_args({}, SESSION_CREATE_SCHEMA)
    assert out["folder"] == "", "omitted means unfiled, not an error"
    with pytest.raises(ValidationError):
        validate_tool_args({"folder": "x" * 4097}, SESSION_CREATE_SCHEMA)


def _folder(state, fid: str, name: str, **extra):
    row = {"id": fid, "name": name, "parent_id": "", **extra}
    state._folders.append(row)
    return row


def test_create_files_the_slot_at_birth(tmp_path):
    """A named folder is applied at creation AND rides the birth metadata.

    The disk half is the load-bearing one: the normal save path returns early on
    an empty message window, so for a created-then-idle session the birth
    metadata line is the only durable record of the placement. Dropping
    `folder_id` from that dict files the session in memory and loses it on the
    next restart.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )

    child = state.get_slot(created["target"])
    assert child is not None
    assert child.folder_id == "fold00000001", "the slot must be filed, not left for a move"
    written = state.conversation_log.get_metadata(slot_history_key(child))
    assert written.get("folder_id") == "fold00000001", (
        "the placement must reach the persist-at-birth metadata -- the save path "
        "writes nothing for an empty session, so this line is the only record"
    )


def test_create_refuses_an_unknown_folder(tmp_path):
    """An unresolvable folder refuses the WHOLE create, allocating nothing.

    The caller asked for a session filed in this folder; 'created but unfiled'
    would silently honor half of that. No session exists yet, so refusal loses
    nothing -- the same posture the move path takes on an unknown folder.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = state.live_slot_count()

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.create_session(state, caller_session_key=_key(caller), folder_id="nope00000000")
        )

    assert exc.value.code == "folder_not_found"
    assert state.live_slot_count() == before, "a refused create must not leave a slot behind"


def test_a_folder_deleted_mid_create_is_refused_under_the_lock(tmp_path, monkeypatch):
    """Folder existence is decided under the folder-store lock, late.

    `create_session` suspends before the allocation (project dir, config load),
    and a folder delete can land in those windows. Existence is therefore
    confirmed READ-ONLY under the folder-store lock (`state.read_folders`) as
    the last suspension before the re-gate. Simulated by deleting the folder
    inside the project-dir resolution. Mutation guard: a check that reads the
    unlocked list before those awaits would let this create succeed into a
    folder that is already gone.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")

    def _delete_folder_then_resolve(_workspace):
        # Stand in for the interleaving: the delete lands while the project
        # directory is still being resolved off-loop.
        state._folders[:] = [f for f in state._folders if f["id"] != "fold00000001"]
        return str(tmp_path)

    monkeypatch.setattr(sc, "default_project_dir", _delete_folder_then_resolve)

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
        )
    assert exc.value.code == "folder_not_found"


def test_filing_at_birth_unhides_the_folder_like_a_move(tmp_path):
    """Model-B semantics apply at create exactly as they do on a move.

    Moving a session into a hidden folder un-hides it (`_unhide_folder`), so a
    session filed at creation must not land invisibly inside a folder the user
    cannot see -- that would be a session the sidebar hides by construction.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    row = _folder(state, "fold00000001", "Goal", hidden=True)

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )

    assert state.get_slot(created["target"]).folder_id == "fold00000001"
    assert row["hidden"] is False, "filing into a hidden folder must un-hide it"


def test_a_folder_cannot_smuggle_creation_past_the_caller_refusals(tmp_path):
    """Caller eligibility precedes every folder consideration.

    The move path's app-ownership rule never has to run here because an
    app-scoped caller cannot create a session at all -- that refusal is the
    guard the filing inherits, and it must keep firing FIRST so the folder
    argument cannot become a probe for folder existence.
    """
    state = _make_state(tmp_path)
    app_caller = _slot(state, "chat-app")
    app_caller._app = "some-app"
    _folder(state, "fold00000001", "Goal")

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.create_session(state, caller_session_key=_key(app_caller), folder_id="fold00000001")
        )
    assert (
        exc.value.code == "app_scoped_caller"
    ), "the caller refusal must precede folder handling -- not folder_not_found"


def test_a_refused_create_leaves_a_hidden_folder_hidden(tmp_path, monkeypatch):
    """No durable folder-tree mutation may survive a refused create.

    The Model-B un-hide persists `hidden = False` to the folder store, and the
    re-gate can still refuse AFTER the folder was confirmed -- so un-hiding
    early would durably reverse a choice the user made, for a call that failed.
    Simulated with the caller closing mid-create (the same interleaving the
    re-gate exists for). Mutation guard: moving the un-hide back before the
    re-gate flips the folder visible here.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    row = _folder(state, "fold00000001", "Goal", hidden=True)

    def _close_caller_then_resolve(_workspace):
        state._slots.pop(caller.key, None)
        return str(tmp_path)

    monkeypatch.setattr(sc, "default_project_dir", _close_caller_then_resolve)

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(
            sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
        )
    assert exc.value.code == "caller_not_open"
    assert row["hidden"] is True, "a refused create must not un-hide the folder"


def test_moving_an_empty_newborn_before_its_first_message_survives_a_restart(tmp_path):
    """A filed-at-birth session that is re-filed while still empty persists it.

    Birth metadata made empty sessions durable, which made the save path's
    empty-window early return newly consequential: the folder PATCH route and
    the folder-delete sweep persist via `save_slot_off_loop(force=True)`, whose
    full save has no window to write for a message-less slot -- without the
    metadata merge, a restart would resurrect the BIRTH placement the user
    already changed (or point at a folder they deleted).
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")
    _folder(state, "fold00000002", "Other")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )
    child = state.get_slot(created["target"])

    # The user drags it into another folder before any message lands.
    child.folder_id = "fold00000002"
    asyncio.run(save_slot_off_loop(state, child, force=True))
    moved = state.conversation_log.get_metadata(slot_history_key(child))
    assert moved.get("folder_id") == "fold00000002", "the move must overwrite the birth filing"

    # And unfiling durably clears it (a falsy value reads as unfiled).
    child.folder_id = ""
    asyncio.run(save_slot_off_loop(state, child, force=True))
    unfiled = state.conversation_log.get_metadata(slot_history_key(child))
    assert not unfiled.get("folder_id"), "unfiling must not resurrect the birth filing"

    # An ordinary empty tab has no metadata line, and a forced save must not
    # materialize one -- a session with no line does not survive a restart, so
    # there is nothing to reconcile.
    plain = _slot(state, "chat-plain")
    asyncio.run(save_slot_off_loop(state, plain, force=True))
    meta, readable = state.conversation_log.get_metadata_status(slot_history_key(plain))
    assert readable and not meta, "no metadata line may be invented for a plain empty tab"


def test_metadata_mutations_on_an_empty_newborn_survive_a_restart(tmp_path):
    """Tags and a pin acknowledged before the first message persist.

    The tag routes and the pin route persist ONLY through
    ``save_slot_off_loop(force=True)`` -- for a message-less newborn that is
    the empty-window merge, so a folder-only merge would acknowledge the
    mutation and then silently drop it on restart. Clears must be explicit:
    the merge cannot delete a key, so untag/unpin write falsy values that
    rehydrate reads as cleared.
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )
    child = state.get_slot(created["target"])

    # The user tags, pins, mode-switches, and binds it before any message lands.
    child.tags = ["tag00000001"]
    child.pinned = True
    child.mode = "orchestrator"
    child._artifact = "my-artifact"
    asyncio.run(save_slot_off_loop(state, child, force=True))
    meta = state.conversation_log.get_metadata(slot_history_key(child))
    assert meta.get("tags") == ["tag00000001"], "an acknowledged tag must reach disk"
    assert meta.get("pinned") is True, "an acknowledged pin must reach disk"
    assert meta.get("mode") == "orchestrator", "an acknowledged mode switch must reach disk"
    assert meta.get("artifact") == "my-artifact", "an acknowledged binding must reach disk"
    assert meta.get("folder_id") == "fold00000001", "the merge must not drop the birth filing"

    # And the clears are explicit -- a restart must not resurrect them.
    child.tags = []
    child.pinned = False
    child.mode = ""
    child._artifact = ""
    asyncio.run(save_slot_off_loop(state, child, force=True))
    meta = state.conversation_log.get_metadata(slot_history_key(child))
    assert meta.get("tags") == [], "untagging must overwrite the persisted tags"
    assert not meta.get("pinned"), "unpinning must overwrite the persisted pin"
    assert not meta.get("mode"), "a mode reset must overwrite the persisted mode"
    assert not meta.get("artifact"), "an unbind must overwrite the persisted binding"


def test_the_empty_window_merge_mirrors_the_full_saves_slot_owned_fields(tmp_path):
    """Drift guard: every restart-relevant slot-owned field survives the merge.

    The empty-window merge must persist what the FULL save's metadata line
    would persist, or whichever route happens to flow through it silently
    drops an acknowledged mutation on restart. Keyed to
    ``SLOT_OWNED_META_KEYS`` so a field added to the full save later fails
    here instead of regressing quietly. Exclusions are the history-layer /
    conditional-identity keys the merge legitimately writes only when the
    slot carries them.
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
    from kiro_crew.history import SLOT_OWNED_META_KEYS

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )
    child = state.get_slot(created["target"])

    child.tags = ["tag00000001"]
    child.pinned = True
    child.mode = "orchestrator"
    child._artifact = "my-artifact"
    child.reasoning_effort = "high"
    child.color_index = 3
    child.title = "Pinned title"
    child._titled = True
    child._title_origin = "user"
    asyncio.run(save_slot_off_loop(state, child, force=True))
    meta = state.conversation_log.get_metadata(slot_history_key(child))

    # History-layer bookkeeping the merge must NOT touch, the closed pair
    # (exercised by its own test), and identity fields a plain user newborn
    # does not carry.
    excluded = {
        "_type",
        "created_at",
        "last_consolidated",
        "closed",
        "closed_at",
        "app",
        "forked_from",
        "linked_session_key",
        # The remote-execution binding, written all-three-or-none by both the
        # full save and the merge. A plain local newborn carries none of it; the
        # bound case is covered by
        # test_remote_crew_execution.py::test_the_empty_window_merge_persists_a_complete_binding.
        "executor",
        "instance_id",
        "remote_slot",
        # In-flight relay marker: written ONLY while a relay is running and
        # omitted once the turn ends (absence = "not in flight"), so a plain
        # newborn never carries it. Its clear-on-completion behaviour is covered by
        # test_remote_crew_execution.py::
        # test_the_marker_is_cleared_on_disk_when_a_relay_completes.
        "relay_in_flight",
    }
    for key in sorted(SLOT_OWNED_META_KEYS - excluded):
        assert key in meta, f"slot-owned field {key!r} missing after an empty-window forced save"
    assert meta.get("artifact") == "my-artifact"
    assert meta.get("reasoning_effort") == "high"
    assert meta.get("color_index") == 3
    assert meta.get("title") == "Pinned title"
    assert meta.get("title_origin") == "user"


def test_the_empty_window_merge_reads_slot_state_at_write_time(tmp_path):
    """The merged fields are evaluated under the history lock, not snapshotted.

    Two concurrent force-saves of the same empty newborn can commit in either
    order; a dict snapshotted before the lock would let the older save land
    second and silently revert an acknowledged newer mutation (a tag save
    restoring ``pinned=False`` over a pin that already committed). Pinning the
    guard-time read: a slot mutation landing after the save call but before
    the locked write must be what reaches disk.
    """
    from unittest.mock import patch

    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )
    child = state.get_slot(created["target"])

    real = state.conversation_log.update_metadata_if

    def _mutate_then_write(key, fields, guard):
        # Simulates a concurrent pin committing between this save's call and
        # its locked write: the merge must pick up the NEW value.
        child.pinned = True
        return real(key, fields, guard)

    child.pinned = False
    with patch.object(state.conversation_log, "update_metadata_if", _mutate_then_write):
        asyncio.run(save_slot_off_loop(state, child, force=True))
    meta = state.conversation_log.get_metadata(slot_history_key(child))
    assert meta.get("pinned") is True, "the merge must write the slot state current at lock time"


def test_closing_an_empty_newborn_does_not_resurrect_it_open(tmp_path):
    """A close acknowledged for a message-less newborn persists to its line.

    Birth metadata made empty sessions durable -- without the closed merge the
    line stays open-shaped and the next restart resurrects a tab the user
    dismissed.
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )
    child = state.get_slot(created["target"])

    asyncio.run(save_slot_off_loop(state, child, closed=True, closed_at=123.0))
    meta = state.conversation_log.get_metadata(slot_history_key(child))
    assert meta.get("closed") is True, "the close must reach the birth metadata line"
    assert meta.get("closed_at") == 123.0, "the close instant must be the caller-supplied one"


def test_an_unreadable_record_fails_the_empty_window_merge_loudly(tmp_path):
    """A merge skipped because the record could not be READ must raise.

    ``update_metadata_if`` fails closed on an unreadable record without
    invoking the guard; swallowing that would report the save as durable -- a
    close would remove the tab while the on-disk line stays open-shaped, and
    the next restart resurrects it. The by-design skip (guard ran, record
    empty: a line-less plain tab) must stay silent.
    """
    from unittest.mock import patch

    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )
    child = state.get_slot(created["target"])

    def _unreadable(key, fields, guard):
        # Mirrors update_metadata_if's fail-closed path: guard NOT invoked.
        return False

    with patch.object(state.conversation_log, "update_metadata_if", _unreadable):
        with pytest.raises(Exception):
            asyncio.run(save_slot_off_loop(state, child, closed=True, best_effort=False))

    # The by-design skip stays silent: a plain empty tab has no metadata line,
    # the guard runs, sees an empty record, and refuses without error.
    plain = _slot(state, "chat-plain2")
    assert asyncio.run(save_slot_off_loop(state, plain, force=True, best_effort=False)) is True


def test_an_unhide_failure_does_not_fail_a_committed_create(tmp_path, monkeypatch):
    """The Model-B un-hide is best-effort once the create has committed.

    By the time it runs, the slot is published and persisted at birth -- a
    folder-store write failure propagating from here would return 500 for a
    session that EXISTS, and the caller's natural retry would create a
    duplicate. Mutation guard: letting the exception escape fails this create.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal", hidden=True)

    async def _boom(_state, _fid):
        raise RuntimeError("folder store write failed")

    monkeypatch.setattr(sc, "_unhide_folder", _boom)

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )

    child = state.get_slot(created["target"])
    assert child is not None, "the committed create must be reported as a success"
    assert child.folder_id == "fold00000001"
    written = state.conversation_log.get_metadata(slot_history_key(child))
    assert written.get("folder_id") == "fold00000001", "the filing itself must have landed"


def test_the_empty_window_merge_cannot_resurrect_a_deleted_session(tmp_path):
    """The existence guard and the merge run under ONE lock.

    The plain metadata update is an upsert, so a checked-then-written pair
    would let a permanent deletion land between the read and the write and be
    recreated as a fresh file. `update_metadata_if` re-makes the decision inside
    the cross-process lock; a session file deleted before the forced save stays
    deleted.
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _folder(state, "fold00000001", "Goal")

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), folder_id="fold00000001")
    )
    child = state.get_slot(created["target"])
    history_key = slot_history_key(child)
    path = state.conversation_log._path(history_key)
    assert path.exists(), "birth metadata must be on disk before the deletion"

    # A permanent deletion lands, then the racing forced save arrives.
    path.unlink()
    child.folder_id = ""
    asyncio.run(save_slot_off_loop(state, child, force=True))

    assert not path.exists(), "the merge must not resurrect a deleted session file"


def test_every_session_control_refusal_is_audited_as_failed():
    """A refused tool call must not be recorded as a completed one.

    `call_tool_with_logging` classifies by prefix -- `outcome="failed"` only when
    the result starts with "Error:". A refusal without it lands in the audit as a
    successful invocation, which inverts the record for exactly the calls a
    reviewer would go looking for. Derived from the source so a new refusal that
    forgets the prefix fails here.
    """
    import re
    from pathlib import Path

    src = Path(sc.__file__).parent.parent / "mcp_dashboard.py"
    body = src.read_text(encoding="utf-8")

    # The dispatch's own refusal returns: a return whose string opens with the
    # cross mark is a refusal that will be audited as completed.
    bare = re.findall(r"return[^\n]*\\u274c[^\n]*", body)
    assert not bare, f"session-control refusals not prefixed with 'Error:': {bare}"


def _agent_resolves(monkeypatch, workspace: str) -> None:
    """Make the effective agent resolve and be bound to *workspace*.

    Tests whose subject is not agent resolution still have to get past the binding
    check, which refuses a name nothing would dispatch. Forcing the honored flag
    keeps those tests focused without weakening the check -- the refusal itself is
    covered by `test_an_agent_name_that_does_not_resolve_is_refused`.
    """
    real = sc.resolve_agent_bindings
    monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: workspace)
    monkeypatch.setattr(
        sc,
        "resolve_agent_bindings",
        lambda cfg, agent_name=None, project_dir=None: dataclasses.replace(
            real(cfg, None, project_dir), requested_resolved=True
        ),
    )


def test_an_agent_name_that_does_not_resolve_is_refused(tmp_path, monkeypatch):
    """A session must not advertise an agent that is not the one answering.

    An unknown name falls back to the default agent's bindings, which pass the
    workspace check because they ARE the caller's default -- so no memory boundary
    is crossed, but `slot.agent` would store a name that nothing dispatches.
    `ResolvedBindings.requested_resolved` states exactly that contract for callers
    which store the requested name.

    Mutation guard: without the check the session is created and reports the
    unresolved name as its agent.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = set(state._slots)

    real = sc.resolve_agent_bindings

    def _unresolved(cfg, agent_name=None, project_dir=None):
        bindings = real(cfg, None, project_dir)
        return dataclasses.replace(bindings, requested_resolved=False)

    monkeypatch.setattr(sc, "resolve_agent_bindings", _unresolved)

    with pytest.raises(sc.SessionControlError) as err:
        asyncio.run(
            sc.create_session(state, caller_session_key=_key(caller), agent="no-such-agent")
        )

    assert err.value.code == "agent_unresolved"
    assert set(state._slots) == before, "a refused creation leaves no slot behind"


def test_the_binding_is_resolved_with_the_childs_project_dir(tmp_path, monkeypatch):
    """A materialized kiro agent is declared per project directory, not in config.

    Resolving without the project directory reports an app's own agent as
    unresolvable, so the refusal above would reject a name that does resolve for
    the session being created.

    Mutation guard: dropping the argument passes None and the assertion fails.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    seen: dict[str, object] = {}

    real = sc.resolve_agent_bindings

    def _record(cfg, agent_name=None, project_dir=None):
        seen["project_dir"] = project_dir
        return dataclasses.replace(real(cfg, None, project_dir), requested_resolved=True)

    monkeypatch.setattr(sc, "resolve_agent_bindings", _record)

    asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    # Asserted as identity-against-None plus equality rather than truthiness: a
    # workspace with no configured project directory resolves to "", which is a
    # correct value to pass on. Dropping the argument yields None, which fails both
    # halves in every environment.
    assert seen["project_dir"] is not None, "the project dir must be passed, not omitted"
    assert seen["project_dir"] == loader.default_project_dir(caller.workspace)


def test_a_caller_that_closes_during_the_await_cannot_still_create(tmp_path, monkeypatch):
    """A removed slot stays usable as an object, so presence must be re-read.

    Closing the caller's tab removes its slot from the table, but the reference
    resolved before the await keeps working -- every attribute still answers. Gating
    on that object would authorize against a caller whose authority already ended
    and publish a persistent session behind it.

    Mutation guard: gating the stale object instead of re-resolving publishes the
    session.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = set(state._slots)

    def _resolve_then_close(_workspace):
        # The caller's tab closes while the project directory is being resolved.
        state._slots.pop(caller.key, None)
        return str(tmp_path)

    monkeypatch.setattr(sc, "default_project_dir", _resolve_then_close)

    with pytest.raises(sc.SessionControlError) as err:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    assert err.value.code == "caller_not_open"
    assert set(state._slots) - before == set(), "no session may outlive its creator's authority"


def test_a_caller_that_moves_workspace_during_the_await_is_refused(tmp_path, monkeypatch):
    """The workspace fed the agent-binding decision, so a move invalidates it.

    Mutation guard: carrying the pre-await workspace forward puts the child on a
    boundary its creator no longer sits behind.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")

    def _resolve_then_move(_workspace):
        caller.workspace = "somewhere-else"
        return str(tmp_path)

    monkeypatch.setattr(sc, "default_project_dir", _resolve_then_move)

    with pytest.raises(sc.SessionControlError) as err:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    assert err.value.code == "caller_workspace_changed"


def test_a_mirror_link_landing_during_the_await_still_refuses(tmp_path, monkeypatch):
    """Eligibility decided before a suspension point says nothing at allocation time.

    The project directory is resolved in a worker thread, so the coroutine suspends
    between the caller gate and the allocation. `_has_channel_mirror` reads the live
    session store, and a dashboard-born session can be given an outbound mirror link
    at any moment -- so a link registered inside that window would otherwise let a
    now-channel-backed caller publish a persistent session outside its containment.

    Mutation guard: without the re-assert next to the allocation, the slot is
    created and the refusal never fires.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = set(state._slots)
    mirrored = {"now": False}

    monkeypatch.setattr(sc, "_has_channel_mirror", lambda _state, _slot: mirrored["now"])

    def _resolve_then_mirror(_workspace):
        # Stand in for the interleaving: the mirror link lands while the project
        # directory is still being resolved off-loop.
        mirrored["now"] = True
        return str(tmp_path)

    monkeypatch.setattr(sc, "default_project_dir", _resolve_then_mirror)

    with pytest.raises(sc.SessionControlError) as err:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    assert err.value.code == "mirrored_caller"
    assert set(state._slots) == before, "no slot may be published for a refused caller"


def test_the_slot_cap_is_re_checked_after_the_await(tmp_path, monkeypatch):
    """The ceiling is read from live state, so it can fill inside the window too.

    The ceiling is lowered for this test because what is under test is WHEN it is
    read, not what it is: minting the production 500 slots is superlinear and cost
    ~17s of pure setup for no extra property. The caller's own slot stays under the
    lowered cap -- asserted below -- so the refusal can only come from the fill that
    lands inside the await.

    Mutation guard: checking the cap only before the await lets two concurrent
    creations land over the ceiling.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    monkeypatch.setattr(sc, "MAX_LIVE_SLOTS", 3)
    # Without this the test could pass on a cap checked only BEFORE the await.
    assert state.live_slot_count() < sc.MAX_LIVE_SLOTS

    def _resolve_then_fill(_workspace):
        for i in range(sc.MAX_LIVE_SLOTS):
            _slot(state, f"filler-{i}")
        return str(tmp_path)

    monkeypatch.setattr(sc, "default_project_dir", _resolve_then_fill)

    with pytest.raises(sc.SessionControlError) as err:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    assert err.value.code == "slot_cap_reached"


def test_a_created_slot_records_the_caller_that_asked_for_it(tmp_path, monkeypatch):
    """Attribution is what makes the per-creator ceiling countable at all.

    Mutation guard: drop the ``_created_by`` write and every caller's count stays
    0, so ``MAX_SLOTS_PER_CREATOR`` can never be reached and the cap is decorative.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller.agent = "researcher"
    _agent_resolves(monkeypatch, "default")

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    child = state.get_slot(created["target"])
    assert child is not None
    # The resolved slot key, which is the identity `create_session` also audits --
    # not the history key the caller presents. Write and count must agree, or the
    # ceiling silently never binds.
    assert getattr(child, "_created_by", "") == caller.key
    assert state.creator_slot_count(caller.key) == 1


def test_one_caller_cannot_consume_everybody_elses_slots(tmp_path, monkeypatch):
    """The per-creator ceiling bounds the DISTRIBUTION, not just the total.

    The global cap alone leaves an automated creator on a nudge loop able to hold
    all ``MAX_LIVE_SLOTS`` itself, after which every later create -- the person
    opening a new chat tab included -- gets the 429. A resource one caller can
    exhaust is not bounded from anybody else's point of view, so this is the half
    of the bound that keeps the verb safe to auto-approve.

    Mutation guard: test the caller's count against ``MAX_LIVE_SLOTS`` instead of
    ``MAX_SLOTS_PER_CREATOR`` and the first assertion stops refusing.
    """
    state = _make_state(tmp_path)
    hog = _slot(state, "chat-hog")
    hog.agent = "researcher"
    other = _slot(state, "chat-other")
    other.agent = "researcher"
    _agent_resolves(monkeypatch, "default")

    # Exactly what create_session writes, without paying for 50 real creates.
    for i in range(sc.MAX_SLOTS_PER_CREATOR):
        _slot(state, f"held-{i}")._created_by = hog.key

    with pytest.raises(sc.SessionControlError) as err:
        asyncio.run(sc.create_session(state, caller_session_key=_key(hog)))
    assert err.value.code == "creator_slot_cap_reached"
    assert err.value.status == 429, "a cap breach is a 429, not a 500"

    # The global ceiling is nowhere near full, which is the point: the refusal came
    # from this caller's own share, and everyone else still has room.
    assert state.live_slot_count() < sc.MAX_LIVE_SLOTS
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(other)))
    assert created["ok"] is True, "one caller's full share must not starve another"


def test_slots_nobody_asked_for_are_charged_to_nobody(tmp_path):
    """A person's own tab and a fork reach ``get_or_create_slot`` unattributed.

    Two failure modes this pins at once: charging those to a caller would let
    ordinary human use burn an automated caller's budget, and counting an empty
    creator key would make every unattributed slot match at once -- so the first
    ``create_session`` call would refuse on a tree the conductor never touched.
    """
    state = _make_state(tmp_path)
    person = _slot(state, "chat-person")

    assert state.creator_slot_count("") == 0, "an empty key must match nothing"
    assert state.creator_slot_count(person.key) == 0


def test_creation_never_loads_the_config_on_the_event_loop():
    """A cache miss reads and validates the config file, stalling every task.

    `KiroCrewConfig.load()` is synchronous filesystem work. Called directly from a
    coroutine it blocks the whole gateway, not just this request -- the loop cannot
    run anything else while it reads. Offloading it is also what the repository's
    no-blocking-call-on-event-loop rule requires.

    Comments are stripped before the check, so the prose explaining WHY the call is
    offloaded cannot satisfy or break the assertion about the call itself.

    Mutation guard: calling it directly fails the first assertion.
    """
    import inspect

    src = inspect.getsource(sc.create_session)
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    assert "KiroCrewConfig.load()" not in code, "the config load must not run inline on the loop"
    assert (
        "await asyncio.to_thread(KiroCrewConfig.load)" in code
    ), "the config load must be offloaded to a worker thread"


def test_nothing_suspends_while_the_created_slot_is_half_configured():
    """`get_or_create_slot` publishes, so the agent must not be set after an await.

    The slot is in the table the moment it is constructed. `await` is a suspension
    point, so a slot published with no agent can be addressed in that window, and
    `/api/chat` would resolve bindings from a blank agent -- running the turn
    against the DEFAULT workspace's memory store instead of this one. The agent
    therefore rides in the constructor, and the project directory is resolved
    BEFORE the slot exists.

    Asserted on ORDER rather than on the finished slot: every field is correct by
    the time `create_session` returns either way, so a state assertion passes under
    the very interleaving this pins.
    """
    import inspect

    src = inspect.getsource(sc.create_session)
    publish = src.index("get_or_create_slot(")
    assert "await asyncio.to_thread(default_project_dir" in src
    resolve = src.index("await asyncio.to_thread(default_project_dir")
    assert resolve < publish, "the project directory must be resolved before the slot is published"
    # The agent is a constructor argument, not a later assignment.
    assert "agent=agent_name" in src, "the agent must be set at construction"
    assert (
        "slot.agent = " not in src
    ), "assigning the agent after construction reopens the half-configured window"
    # Nothing may suspend between publishing the slot and the last field it needs.
    configured = src.index("slot._titled = True")
    assert "await" not in src[publish:configured], (
        "an await between publishing the slot and configuring it makes a "
        "half-built session addressable"
    )
    # The eligibility gate must be the last thing before the allocation, with no
    # suspension between them, or its answer is stale by the time it is acted on.
    gate = src.rindex("_refuse_ineligible_creator(")
    assert (
        resolve < gate < publish
    ), "the caller gate must be re-asserted after the await and before the slot"
    assert "await" not in src[gate:publish], (
        "an await between the eligibility gate and the allocation lets a caller "
        "that has since become ineligible publish a session"
    )
    # The re-check must read the slot TABLE again, not the object resolved before
    # the await: a closed tab's slot is removed while the reference keeps working.
    reresolve = src.index("live_caller = state.get_slot(caller_key)")
    assert resolve < reresolve < gate, "the caller must be re-resolved from its key after the await"
    assert (
        "_refuse_ineligible_creator(state, live_caller)" in src
    ), "the gate must run on the re-resolved slot, not the pre-await object"
    # Nothing may suspend from the re-resolve onward: this span re-reads every input
    # the allocation rests on, so an await inside it puts the decisions back out of
    # date. Asserted from the re-resolve rather than the gate because more than one
    # await precedes it, and the property is about the LAST one.
    assert "await" not in src[reresolve:publish], (
        "an await between re-resolving the caller and allocating the slot makes "
        "every re-read decision stale again"
    )
    # The folder confirmation is a suspension (it takes the folder-store lock),
    # so it must sit BEFORE the re-resolve: after it, nothing suspends until the
    # slot is configured, which is what makes "confirmed under the lock" still
    # true at the assignment (folder mutations run on this loop).
    exists_check = src.index("await state.read_folders(")
    assert exists_check < reresolve, (
        "the folder existence check suspends, so it must precede the caller "
        "re-resolve -- after the re-gate nothing may suspend"
    )
    # And the filing itself happens inside the synchronous configuration window,
    # so no caller ever observes the published slot unfiled -- the atomicity
    # #6118 exists for.
    filed = src.index("slot.folder_id = folder_id")
    assert publish < filed < configured, (
        "the folder must be assigned between publishing the slot and the end of "
        "its synchronous configuration, or a caller can observe it unfiled"
    )
    # The Model-B un-hide is a DURABLE folder-store mutation, so it may run only
    # after the filing actually landed: before the persist, any of the re-gate's
    # refusals (or the write itself failing) would leave a folder the user hid
    # permanently visible for a call that failed.
    unhide = src.index("await _unhide_folder(")
    persist = src.index("log.update_metadata")
    assert persist < unhide, (
        "un-hiding must follow the persist -- a refused create must leave no "
        "durable folder-tree mutation behind"
    )
    # The allocation-to-persist span is broadcast-atomic: `get_or_create_slot`
    # broadcasts on a leading edge, so without the suspend an idle gateway sends
    # the new slot to every client BEFORE folder_id is assigned -- the session
    # renders at the top level for a frame, the observable unfiled state this
    # feature removes. Same pattern the move path pins for its own filing.
    suspend = src.index("with state.suspend_slots_push():")
    assert gate < suspend < publish, (
        "the slot allocation must sit inside suspend_slots_push, so the slot's "
        "first broadcast frame already shows it filed"
    )


def test_create_refuses_at_the_same_slot_cap_as_fork_and_import(tmp_path, monkeypatch):
    """A third creation path must not make the shared ceiling advisory.

    `chat_fork` and `session_transfer` both refuse at 500 live slots. Nothing else
    bounds how many sessions one caller may open, so a creator that skipped the cap
    would let a looping agent exhaust slots and transcript files.

    Mutation guard: dropping the check lets this create succeed.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    monkeypatch.setattr(type(state), "live_slot_count", lambda self: 500)

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    assert exc.value.code == "slot_cap_reached"
    assert exc.value.status == 429


def test_a_failed_persist_retracts_the_slot(tmp_path, monkeypatch):
    """A session whose ownership did not reach disk must not be left behind.

    Reporting the failure while leaving the slot in the table is the worst outcome:
    the caller sees an error and the session exists anyway -- usable in memory,
    addressable by its creator, and gone on the next restart.

    Mutation guard: without the retraction the slot survives the raise.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = set(state._slots)

    def _boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(state.conversation_log, "update_metadata", _boom)

    with pytest.raises(OSError):
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    assert set(state._slots) == before, "the unpersisted slot must not survive"


def test_retraction_spares_a_slot_a_turn_already_started_on(tmp_path, monkeypatch):
    """Retraction must not detach work that began inside the persist window.

    `get_or_create_slot` publishes, and the birth write is awaited, so a turn can
    start on the slot while that write is in a worker thread. Popping the slot then
    leaves the turn running with nothing pointing at it -- unreachable by the read
    verb and unstoppable by the stop verb, because both resolve through the slot
    table. A phantom session that vanishes on the next restart is the lesser harm.

    Mutation guard: an unconditional pop removes the slot and loses the turn.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = set(state._slots)

    def _boom(*_a, **_kw):
        # Stand in for the interleaving: the turn lands on the freshly published
        # slot while the birth write is still in flight, and only then does the
        # write fail.
        born = [k for k in state._slots if k not in before]
        assert born, "the slot is published before the birth write, so it exists here"
        state._slots[born[0]].append("user", "work that must not be orphaned", "msg msg-u")
        raise OSError("disk full")

    monkeypatch.setattr(state.conversation_log, "update_metadata", _boom)

    with pytest.raises(OSError):
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    survivors = set(state._slots) - before
    assert len(survivors) == 1, "a slot carrying a started turn must not be retracted"
    assert state._slots[survivors.pop()].messages, "the turn's work is still reachable"


def test_an_identical_steer_is_refused_while_the_first_is_still_in_flight(tmp_path):
    """In flight means either marker, not just the pending list.

    A steer whose pending entry the running turn has already consumed is still
    awaiting and still owns its `_steer_delivery_ids` entry. Admitting a second
    identical steer at that moment overwrites the first caller's live id, and
    reconciliation then removes the second's -- letting the first's row persist
    twice.

    Mutation guard: checking only `_pending_steers` admits the second steer.
    """
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "run the deploy"
    # A steerable client is required or the function returns STEER_UNAVAILABLE from
    # its top guard and never reaches the one under test -- which is how the first
    # version of this test passed against the unfixed code.
    slot._acp_client = _steerable(accepted=True)

    # The first steer is mid-flight with its pending entry already consumed.
    slot._pending_steers = []
    slot._steer_delivery_ids[text] = "id-of-the-first-caller"

    result = asyncio.run(chat_delivery.steer_into_running_turn(state, slot, text))

    assert (
        result == chat_delivery.STEER_UNAVAILABLE
    ), "the second identical steer must be refused down the queue path"
    assert (
        slot._steer_delivery_ids[text] == "id-of-the-first-caller"
    ), "the first caller's delivery id must not be overwritten"
    slot._acp_client.steer.assert_not_awaited()


def test_an_unrelated_identical_queue_item_is_not_read_as_our_requeue(tmp_path):
    """Requeue detection is by delivery id, so identical text cannot impersonate it.

    Counting queue entries that match the TEXT cannot tell this steer's requeue
    apart from another client queueing the same words in the same window. Reading
    that as "mine was requeued" makes the caller skip persisting, and the steer the
    turn actually consumed loses its transcript row.

    Mutation guard: a content count sees the queue grow and returns the requeued
    outcome, so nothing persists.
    """
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "run the deploy"
    slot._acp_client = _steerable(accepted=True)

    def _consume_and_let_someone_else_queue(*_a, **_kw):
        # The running turn consumes our registration, and an unrelated client
        # queues the very same words while the RPC is still suspended. That entry
        # carries no delivery id, so it is not ours.
        slot._pending_steers.clear()
        slot.queue_append(text)
        return True

    slot._acp_client.steer = AsyncMock(side_effect=_consume_and_let_someone_else_queue)

    result = asyncio.run(chat_delivery.steer_into_running_turn(state, slot, text))

    assert (
        result == chat_delivery.STEER_STEERED
    ), "an identical queue entry without our delivery id must not read as our requeue"


def test_our_own_requeue_is_still_detected_by_its_delivery_id(tmp_path):
    """The other side: a real requeue carries the id and must report requeued.

    Mutation guard: dropping the id probe reports this as delivered and the drain
    then appends the row a second time.
    """
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "run the deploy"
    slot._acp_client = _steerable(accepted=True)

    def _requeue_like_the_teardown(*_a, **_kw):
        # The teardown moves the pending steer into the queue, carrying the id it
        # was registered under -- exactly what `_requeue_unconsumed_steers` does.
        did = slot._steer_delivery_ids.get(text, "")
        slot._pending_steers.clear()
        slot.queue_insert(0, text, meta={"steer_delivery_id": did})
        return True

    slot._acp_client.steer = AsyncMock(side_effect=_requeue_like_the_teardown)

    result = asyncio.run(chat_delivery.steer_into_running_turn(state, slot, text))

    assert result == chat_delivery.STEER_REQUEUED, "our own requeue must be recognised"


def test_a_steer_a_hard_kill_discarded_is_not_reported_as_delivered(tmp_path):
    """A hard kill discards the text, so no row may claim it was delivered.

    A hard kill clears `_pending_steers` AND drops the matching
    `_steer_delivery_ids` entry, while the running turn CONSUMING a steer leaves
    that entry in place. The delivery id is therefore what tells the two apart --
    without it, absence alone is ambiguous and the reconciliation has to guess.

    Reporting delivery for a discarded steer persists a transcript row for text
    that never ran and answers the caller `steered: true`. `STEER_UNAVAILABLE`
    instead falls through to the queue, so the message becomes a visible,
    cancellable card; resending cannot duplicate anything, because the killed turn
    did not run it.

    Mutation guard: without the delivery-id check this returns the delivered path.
    """
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "change the plan"
    slot._acp_client = _steerable(accepted=True)

    # The hard kill ran while the steer RPC was suspended: pending entry cleared,
    # delivery id dropped with it, stop generation advanced.
    def _clear_like_a_hard_kill(*_a, **_kw):
        slot._pending_steers.clear()
        slot._steer_delivery_ids.clear()
        slot._stop_generation = int(getattr(slot, "_stop_generation", 0) or 0) + 1
        return True

    slot._acp_client.steer = AsyncMock(side_effect=_clear_like_a_hard_kill)

    result = asyncio.run(chat_delivery.steer_into_running_turn(state, slot, text))

    assert (
        result == chat_delivery.STEER_UNAVAILABLE
    ), "a discarded steer must not be reported as delivered"


def test_a_steer_consumed_before_a_stop_is_still_reported_as_delivered(tmp_path):
    """The other side of the same discrimination -- and the more dangerous one.

    A consume leaves the delivery id in place. Telling that caller to resend is
    the one answer that can run the text twice, so a surviving id keeps the
    delivered path even though the turn was stopped afterwards.

    Mutation guard: reading absence as discard would send this down the queue path
    and risk a duplicate execution.
    """
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "change the plan"
    slot._acp_client = _steerable(accepted=True)

    def _consume_then_stop(*_a, **_kw):
        # The running turn consumed the registration; the delivery id survives.
        slot._pending_steers.clear()
        slot._stop_generation = int(getattr(slot, "_stop_generation", 0) or 0) + 1
        return True

    slot._acp_client.steer = AsyncMock(side_effect=_consume_then_stop)

    result = asyncio.run(chat_delivery.steer_into_running_turn(state, slot, text))

    assert (
        result != chat_delivery.STEER_UNAVAILABLE
    ), "a consumed steer must never be answered with 'resend'"


def test_session_control_is_not_imported_on_the_gateway_boot_path():
    """A feature-flagged subsystem must not be eagerly imported by `server.py`.

    The enabled check (`session_control_enabled`) lives inside the handlers, so a
    module-level import in `server.py` is an import whose gate runs after it --
    an operator who set `agent.session_control=false` would still pay to load the
    subsystem on every launch. Route registration stays at boot; only the import
    is deferred to first request.

    Mutation guard: restoring the module-level import fails this.
    """
    from pathlib import Path

    from kiro_crew.dashboard import server as dashboard_server

    src = Path(dashboard_server.__file__).read_text(encoding="utf-8")
    for line in src.splitlines():
        if line.startswith("from kiro_crew.dashboard.handlers import"):
            assert "session_control" not in line, (
                "session_control must not be imported at server module level -- " f"found: {line!r}"
            )


def test_the_created_agent_name_is_sanitized_before_storage(tmp_path, monkeypatch):
    """The caller-supplied agent goes through the same outbound guard as the title.

    It arrives from the calling model, is persisted verbatim to the metadata line
    and pushed to every dashboard client, so the schema's length cap is not
    enough -- a credential-shaped string would otherwise be stored and displayed.

    Mutation guard: dropping the `sanitize_outbound` call leaves the raw value.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    monkeypatch.setattr(sc, "sanitize_outbound", lambda s: f"SANITIZED:{s}")
    # Keep the binding check satisfied so the test reaches the storage under test.
    _agent_resolves(monkeypatch, caller.workspace)

    created = asyncio.run(
        sc.create_session(state, caller_session_key=_key(caller), agent="secret-looking")
    )
    child = state.get_slot(created["target"])
    assert child is not None
    assert child.agent.startswith(
        "SANITIZED:"
    ), "the agent value must pass through the outbound guard before storage"


def test_the_audit_write_does_not_run_on_the_event_loop(tmp_path, monkeypatch):
    """Constructing the SEL must not happen on the loop.

    `log_tool_invocation` only enqueues, but the FIRST `sel()` of a process
    constructs the log -- trust-dir creation, key validation, and on Windows an
    `icacls` subprocess. This can genuinely be that first call, because
    `sel_audit_middleware` logs AFTER `await handler(...)`: on a fresh gateway the
    first authenticated request constructs the log inside whatever handler runs
    first.

    Mutation guard: calling `sel()` inline runs it on the calling thread.
    """
    import threading
    import time

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    seen: dict[str, int] = {}

    class _Recorder:
        def log_tool_invocation(self, **_kw):
            seen["thread"] = threading.get_ident()

    monkeypatch.setattr(sc, "sel", lambda: _Recorder())
    monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)

    async def _drive() -> int:
        await sc.create_session(state, caller_session_key=_key(caller))
        return threading.get_ident()

    loop_thread = asyncio.run(_drive())

    # The offload is fire-and-forget, so wait for it rather than racing it.
    deadline = time.monotonic() + 5
    while "thread" not in seen and time.monotonic() < deadline:
        time.sleep(0.01)

    assert "thread" in seen, "the audit never ran"
    assert (
        seen["thread"] != loop_thread
    ), "the audit must be dispatched off the loop's thread, not called inline"


def test_the_denial_audit_does_not_run_on_the_event_loop(tmp_path, monkeypatch):
    """The same property for the DENIAL path, which is the likelier first `sel()`.

    A fresh gateway refuses before it ever allows -- a disabled feature, an
    unidentified caller -- so the denial audit is the more probable site of the
    first-ever `sel()` construction, not the rarer one.

    Mutation guard: calling `sel()` inline in `deny` runs it on the loop thread.
    """
    import threading
    import time

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    seen: dict[str, int] = {}

    class _Recorder:
        def log_api_access(self, **_kw):
            seen["thread"] = threading.get_ident()

    monkeypatch.setattr(sc, "sel", lambda: _Recorder())

    async def _drive() -> int:
        with pytest.raises(sc.SessionControlError):
            sc.authorize_target(
                state,
                caller_session_key=_key(caller),
                target="does-not-exist",
                operation="read",
            )
        return threading.get_ident()

    loop_thread = asyncio.run(_drive())

    deadline = time.monotonic() + 5
    while "thread" not in seen and time.monotonic() < deadline:
        time.sleep(0.01)

    assert "thread" in seen, "the denial was never audited"
    assert (
        seen["thread"] != loop_thread
    ), "the denial audit must be dispatched off the loop's thread"


def test_the_denial_audit_does_not_persist_caller_supplied_credentials(tmp_path, monkeypatch):
    """A credential passed as `target` must not survive into the audit sink.

    `target` is raw MCP input. The audit is durable AND served back: the SEL
    writer does not redact on-disk records (`sel.py` says so outright) and
    `/api/sel/events` returns `recent()` rows verbatim to the dashboard, so an
    unredacted write is readable long after the refusal.

    Both fields are asserted because both carry caller text: `resources`
    interpolates `target` directly, and the `target_not_found` refusal
    interpolates it into `reason`, which becomes `error`. A fix that redacted
    only one of them would leave the other serving the secret.

    Mutation guard: dropping either `redact` call in `deny` fails this.
    """
    import time

    # Assembled rather than written literally: a real-looking token in source
    # trips secret scanners for no benefit. `redact_credentials` matches the
    # `ghp_` + 36 shape.
    noisy_target = "ghp_" + ("a" * 36)

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    captured: dict[str, str] = {}

    class _Recorder:
        def log_api_access(self, **kw):
            captured.update({k: str(v) for k, v in kw.items()})

    monkeypatch.setattr(sc, "sel", lambda: _Recorder())

    async def _drive() -> None:
        with pytest.raises(sc.SessionControlError):
            sc.authorize_target(
                state,
                caller_session_key=_key(caller),
                target=noisy_target,
                operation="read",
            )

    asyncio.run(_drive())

    deadline = time.monotonic() + 5
    while "resources" not in captured and time.monotonic() < deadline:
        time.sleep(0.01)

    assert "resources" in captured, "the denial was never audited"
    assert noisy_target not in captured["resources"], (
        "the raw target reached the audit's `resources`; /api/sel/events would "
        "serve the credential back to the dashboard"
    )
    assert noisy_target not in captured.get("error", ""), (
        "the target_not_found reason interpolates the target, so `error` carried "
        "the credential even though `resources` was redacted"
    )
    # The refusal must still be diagnosable: the code survives redaction.
    assert "target_not_found" in captured["resources"]


def test_slot_cap_has_one_owning_constant() -> None:
    """Every slot-creating path reads the SAME owning ceiling constant.

    The live-slot ceiling used to be declared independently as ``= 500`` in
    three modules (session create, chat fork, session import); raising it then
    took three edits and the effective limit depended on which door the caller
    came through. It now has one home -- ``state.MAX_LIVE_SLOTS`` in the module
    that owns ``live_slot_count()`` -- and each door imports that one name. This
    pins that no door has re-introduced its own literal: all three modules must
    expose the identical owning object.
    """
    from kiro_crew.dashboard import chat_fork
    from kiro_crew.dashboard import session_control as sc_mod
    from kiro_crew.dashboard import session_transfer
    from kiro_crew.dashboard import state as state_mod

    owning = state_mod.MAX_LIVE_SLOTS
    # Each door imported the owning constant into its own namespace; assert they
    # are the very same object, so a re-introduced per-door literal is caught.
    assert sc_mod.MAX_LIVE_SLOTS is owning
    assert chat_fork.MAX_LIVE_SLOTS is owning
    assert session_transfer.MAX_LIVE_SLOTS is owning


# ── Close (archive) ──────────────────────────────────────────────────────────


def test_close_archives_a_peer_and_removes_the_slot(tmp_path):
    """The happy path end to end: a peer in the caller's workspace is closed via
    the real ``close_slot`` extraction, so the slot leaves ``_slots`` and the
    per-tab session is torn down — exactly what the tab ✕ does."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)

    result = asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=target.key))

    assert result == {"ok": True, "target": target.key}
    assert target.key not in state._slots
    # The per-tab kiro-cli session is torn down through the shared close path.
    state.sessions.remove.assert_awaited()


def test_close_refuses_a_self_target(tmp_path):
    """Close routes through ``authorize_target`` like stop and read, so a session
    cannot close itself (the guard is operation-agnostic)."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=caller.key))
    assert exc.value.code == "self_target"
    # The self-close was refused, so the caller's own slot survives.
    assert caller.key in state._slots


def test_close_maps_a_close_failure_to_its_code(tmp_path, monkeypatch):
    """A ``SlotCloseError`` from the shared path surfaces as a
    ``SessionControlError`` carrying the SAME code and status — so a caller can
    tell "history could not be saved" from a generic failure — and the failed
    close is audited as denied."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)

    from kiro_crew.dashboard import chat_handlers

    async def _boom(_state, _slot, _name, *, pre_pop_check=None):
        raise chat_handlers.SlotCloseError("failed to notify the app", code="app_close_hook_failed")

    audited: list[tuple[str, str]] = []
    real_audit = sc._audit

    def _audit(*, caller_session_key, operation, slot_key, outcome, detail=None):
        audited.append((operation, outcome))
        return real_audit(
            caller_session_key=caller_session_key,
            operation=operation,
            slot_key=slot_key,
            outcome=outcome,
            detail=detail,
        )

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.close_slot", _boom)
    monkeypatch.setattr(sc, "_audit", _audit)

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=target.key))
    assert exc.value.code == "app_close_hook_failed"
    assert exc.value.status == 500
    # The target was NOT removed — the failing close rolled back, and the trail
    # records the attempt as denied.
    assert target.key in state._slots
    assert ("close", "denied") in audited


def test_close_authorizes_then_acts_with_nothing_in_between(tmp_path, monkeypatch):
    """Same adjacency contract stop keeps: the SEL prewarm precedes authorization,
    and no ``await`` separates the gate from the act it authorizes.

    Mutation guard: moving the prewarm below the gate, or slipping an await
    between ``authorize_target`` and ``close_slot``, reorders this list."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    order: list[str] = []

    def _sel():
        order.append("sel")
        return MagicMock()

    real_authorize = sc.authorize_target

    def _authorize(*a, **kw):
        order.append("authorize")
        return real_authorize(*a, **kw)

    async def _fake_close(_state, _slot, _name, *, pre_pop_check=None):
        order.append("close")

    monkeypatch.setattr(sc, "sel", _sel)
    monkeypatch.setattr(sc, "authorize_target", _authorize)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.close_slot", _fake_close)

    asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=target.key))

    assert order[:1] == ["sel"], f"the prewarm must precede authorization; got {order}"
    assert (
        order.index("close") - order.index("authorize") == 1
    ), f"something ran between the gate and the act it authorizes: {order}"


def test_close_reauthorizes_at_the_point_of_no_return(tmp_path, monkeypatch):
    """A target that gains a channel mirror DURING close_slot's awaits is refused
    at the pre-pop re-check, so the now-channel-backed session is not archived.

    This is the GPT-flagged race: `authorize_target` runs before `close_slot`,
    whose nudge-retirement + app-hook awaits are a window in which the target can
    gain a mirror link — the same class `create_session` re-gates for.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)

    async def _fake_close(_state, _slot, _name, *, pre_pop_check=None):
        # Simulate the race: the target gains an outbound channel mirror while
        # close_slot is mid-await, then the point-of-no-return check runs.
        state.sessions.set_mirror_link(slot_history_key(target), "C123", "T1")
        assert pre_pop_check is not None, "close_target must arm a pre-pop re-check"
        pre_pop_check()  # re-runs authorize_target -> raises for the new mirror
        raise AssertionError("close must not reach the pop after a stale-auth abort")

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.close_slot", _fake_close)

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=target.key))
    # The re-check's mirrored_target refusal round-trips with its own 403, not a
    # generic close failure, and the session survives.
    assert exc.value.code == "mirrored_target"
    assert exc.value.status == 403
    assert target.key in state._slots


def test_close_slot_pre_pop_abort_rolls_back_and_does_not_pop(tmp_path):
    """A `pre_pop_check` that raises `SlotCloseError` aborts the close at the point
    of no return: the slot stays in `_slots` and the per-tab session is never torn
    down. The human ✕ path (pre_pop_check=None) is unaffected."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")

    def _abort():
        raise chat_handlers.SlotCloseError(
            "target became unreachable", code="mirrored_target", status=403
        )

    with pytest.raises(chat_handlers.SlotCloseError) as exc:
        asyncio.run(chat_handlers.close_slot(state, slot, slot.key, pre_pop_check=_abort))

    assert exc.value.code == "mirrored_target"
    assert slot.key in state._slots  # not popped
    state.sessions.remove.assert_not_awaited()  # teardown never ran


def test_close_aborts_if_the_key_was_reminted_during_the_close(tmp_path, monkeypatch):
    """A concurrent close+reopen re-mints the same key onto a DIFFERENT session
    while close_slot awaits. The pre-pop re-check compares slot identity (not mere
    presence) and aborts with `target_replaced`, so close_slot never pops the
    replacement — the GPT-flagged data-corruption race."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    original_key = target.key

    async def _fake_close(_state, _slot, _name, *, pre_pop_check=None):
        # Simulate the re-mint: drop the original and put a fresh slot object
        # under the SAME key, then run the point-of-no-return check.
        state._slots.pop(original_key, None)
        replacement = state.get_or_create_slot(original_key)
        assert replacement is not target
        assert pre_pop_check is not None
        pre_pop_check()  # authorize_target resolves the replacement -> identity mismatch
        raise AssertionError("close must not pop after a re-mint abort")

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.close_slot", _fake_close)

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=original_key))
    assert exc.value.code == "target_replaced"
    assert exc.value.status == 409
    # The replacement survives.
    assert original_key in state._slots


def test_close_slot_runs_the_pre_pop_check_synchronously_after_retirement(tmp_path, monkeypatch):
    """`pre_pop_check` runs SYNCHRONOUSLY after the (awaited) nudge retirement and
    immediately before the pop, so there is no suspension between the last
    retirement, the re-check, and the removal — a concurrent `monitor_start`
    cannot arm a loop in a window that would leave a timer to rehydrate the
    archived tab, and a mirror/link cannot land between the re-authorization and
    the archival.

    Asserted by the call order (retire is the last AWAIT; the sync check follows,
    then the pop) and by the callback being a plain synchronous function."""
    import inspect

    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    order: list[str] = []

    async def _retire(_name):
        order.append("retire")
        return None

    def _check():  # synchronous by contract — no await before the pop
        order.append("check")

    assert not inspect.iscoroutinefunction(_check)
    monkeypatch.setattr(chat_handlers, "_retire_slot_nudge_loop", _retire)

    asyncio.run(chat_handlers.close_slot(state, slot, slot.key, pre_pop_check=_check))

    # Retirement (the last await) then the synchronous check, then the pop. No
    # second retirement is needed because the check itself suspends nothing.
    assert order == ["retire", "check"], order
    assert slot.key not in state._slots  # closed
