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
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.config import loader
from kiro_crew.dashboard import chat_delivery as cd
from kiro_crew.dashboard import chat_runner as cr
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
async def test_a_requeued_steer_records_queued_and_never_steered(tmp_path, monkeypatch):
    """The append-only log must not claim a steer cut a turn that never got it.

    ``steered`` only means the client accepted the write. If the turn ends during
    the await, the teardown requeues the text and it runs LATER -- so a steer entry
    written on the RPC's return is a permanent false statement about a turn, and the
    body would also be logged twice once the requeue path records it. That is why the
    vocabulary carries no steer type at all.

    Exactly one entry, and it is ``message/queued``: the requeue moves the text
    straight into the slot queue without passing the append that records a queued
    message, so this is the only place that can record it at all.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    import json

    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import emit as crew_log_emit

    crew_log_emit.reset_caches()
    sid = "sess-steer-requeue"
    crew_log_emit.on_session_opened(sid, agent="kirocrew", slot="chat-1")
    crew_log_emit.on_turn_started(sid, 1, "user")
    assert crew_log_emit.flush()

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-1"))

    async def _steer(msg):
        # The turn's teardown runs while the RPC is in flight and requeues the text.
        did = slot._steer_delivery_ids.pop(msg, "")
        slot._pending_steers.remove(msg)
        slot._queue.append({"id": "q1", "content": msg, "meta": {"steer_delivery_id": did}})
        return True

    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(side_effect=_steer)
    client.session_id = sid
    slot._acp_client = client

    outcome = await cd.steer_into_running_turn(state, slot, "run this instead")

    assert outcome == cd.STEER_REQUEUED
    assert crew_log_emit.flush()
    body = [
        json.loads(line)
        for line in lg.crew_log_path("session", sid).read_text(encoding="utf-8").splitlines()[1:]
    ]
    # Every message-domain entry, not one named type: an entry claiming the turn
    # received the steer would be caught whatever it was called.
    mine = [e for e in body if e["type"].startswith("message/")]
    assert len(mine) == 1, f"expected one entry for one message, got {[e['type'] for e in mine]}"
    assert mine[0]["type"] == "message/queued"
    assert mine[0]["data"]["source"] == "steer"
    crew_log_emit.reset_caches()


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
    """A cron caller is admitted but fenced to its own children.

    The ``created_by`` fence refuses the case that matters -- a scheduled job
    reaching the user's own conversation -- while letting it drive the sessions
    it dispatched. The positive half, and a cron's other gates, are in
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
        # Both marks together are what an internal caller on the unix socket
        # arrives with: the secret admits it, and the kernel peer attestation is
        # what lets a session-scoped route act on the key it declares.
        request.get = lambda key, default=None: (
            True if (key in ("internal_auth", "peer_verified") and internal) else default
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

    def test_send_rejects_a_steer_that_is_not_a_boolean(self, tmp_path):
        """`steer` decides whether the message cuts into a running turn, so the
        route type-checks it instead of reading truthiness: the string "false" is
        truthy, and coercing it would interrupt a turn for a caller that asked for
        the queue."""
        req = self._request(tmp_path, internal=True, path="/api/session-control/send")

        async def _json():
            return {"target": "chat-2", "message": "hello", "steer": "false"}

        req.json = _json
        resp = asyncio.run(handlers_sc.api_session_control_send(req))

        assert resp.status == 400
        assert self._body(resp)["code"] == "invalid_steer"

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
    does not match, and that fragment ships to the caller.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    secret = "ghp_" + "B" * 36
    # Straddle the boundary: only the first 10 chars of the secret survive a
    # naive slice, and a 10-char fragment does not match the credential
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
    the cap, so a caller cannot tell how much history exists. Basing it on
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


# ── session_stop is safe to re-send ──────────────────────────────────


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
    """Read from the other side: the absorbed retry must be visible too.

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

    assert out == {"ok": True, "target": "chat-2", "started": True, "steered": False}
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


# ── session_send: the mid-turn steer arm ─────────────────────────────────────


@pytest.mark.asyncio
async def test_steer_cuts_into_the_running_turn_instead_of_queueing(tmp_path):
    """`steer=True` on a busy target delivers NOW, and delivers exactly once.

    The real ``steer_into_running_turn`` runs here against a steer-capable client,
    because the whole value of the arm is that the target reads the text during
    the turn — a stubbed helper would assert the wiring and prove nothing about
    the delivery. The queue must stay EMPTY: a steered message that is also queued
    runs twice.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))
    target._acp_client = _steerable(accepted=True)

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="stop, that file is already fixed",
        steer=True,
    )

    assert out["steered"] is True
    assert out["started"] is False
    assert not target._queue, "a steered message must not ALSO be queued"
    rows = [m for m in target.messages if "already fixed" in m.get("content", "")]
    assert len(rows) == 1, "a steered delivery leaves exactly one transcript row"
    # Provenance rides on the TEXT, so it reaches the target agent and the reader
    # alike: this is what keeps an injected steer from posing as human typing.
    injected = target._acp_client.steer.await_args.args[0]
    assert injected.startswith("[sent by session ")
    assert rows[0]["content"].startswith("[sent by session ")


@pytest.mark.asyncio
async def test_a_steer_the_client_cannot_take_falls_back_to_the_queue(tmp_path):
    """Steering is best-effort; the message is not.

    With no steer-capable client on the slot the helper answers UNAVAILABLE, and
    the message must land on the queue rather than be dropped — the caller is told
    it queued, so it never reads a silent loss as a delivery.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="check the other branch first",
        steer=True,
    )

    assert out["steered"] is False
    assert out["started"] is False
    assert any("check the other branch first" in q.get("content", "") for q in target._queue)


@pytest.mark.asyncio
async def test_a_steer_requeued_by_the_teardown_is_not_queued_a_second_time(tmp_path):
    """The turn ended during the steer RPC and its teardown requeued the text.

    It WILL run, so taking the queue arm as well would deliver it twice. The
    requeue is reproduced the way ``_requeue_unconsumed_steers`` does it: the
    pending entry becomes a queue entry carrying the steer's delivery id.

    Mutation guard: treating REQUEUED like UNAVAILABLE leaves two entries.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    async def _teardown_requeues(msg):
        target._pending_steers.remove(msg)
        target._queue.append(
            {
                "id": "q-requeued",
                "content": msg,
                "meta": {"steer_delivery_id": target._steer_delivery_ids[msg]},
            }
        )
        return True

    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(side_effect=_teardown_requeues)
    target._acp_client = client

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="use the other fixture",
        steer=True,
    )

    assert out["steered"] is False
    assert out["started"] is False
    assert len(target._queue) == 1, "the requeued entry is the delivery; do not add another"


@pytest.mark.asyncio
async def test_the_queue_fallback_re_runs_the_gate_after_the_steer_rpc(tmp_path):
    """The steer RPC is a suspension, so the gate that admitted the send is stale.

    The queue arm stamps the containment holding at APPEND time for the drain to
    re-assert. If the target gains a channel link while the RPC is in flight, an
    unchecked fallback would record that link as "held at admission" and the drain
    would then read a widened audience as the authorized one. So the fallback
    re-authorizes and the send is refused instead.

    Mutation guard: drop the re-gate and this queues the message.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    async def _link_then_fail(_msg):
        # The window this test is about: the target becomes channel-linked while
        # the steer RPC is suspended, and the RPC then fails to place the text.
        target.linked_session_key = "telegram:9001"
        return False

    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(side_effect=_link_then_fail)
    target._acp_client = client

    with pytest.raises(sc.SessionControlError) as err:
        await sc.send_to_target(
            state,
            caller_session_key=_key(caller),
            target="chat-2",
            message="land it on the branch",
            steer=True,
        )

    assert err.value.code == "linked_session_target"
    assert not target._queue, "a target the gate now refuses must not hold the message"


@pytest.mark.asyncio
async def test_a_same_key_replacement_during_the_rpc_is_refused(tmp_path):
    """The re-gate compares slot IDENTITY, not its key string.

    A target closed and resumed during the steer RPC comes back as a FRESH slot
    object under the same name. Delivery below uses the object this call captured,
    so a key-string comparison passes while the message lands on the replaced
    object -- gone from the live session, with `ok: True` reported to the caller.

    Mutation guard: compare `regated.key != slot.key` instead of identity and this
    queues the message onto the detached slot.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    async def _replace_then_fail(_msg):
        # Close and resume under the same name: `get_or_create_slot` mints a new
        # object, so `regated` and the captured `slot` differ while both keys read
        # "chat-2". The RPC then fails to place the text, taking the queue arm.
        state._slots.pop("chat-2", None)
        _busy(_peer_target(state, "chat-2", caller))
        return False

    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(side_effect=_replace_then_fail)
    target._acp_client = client

    with pytest.raises(sc.SessionControlError) as err:
        await sc.send_to_target(
            state,
            caller_session_key=_key(caller),
            target="chat-2",
            message="land it on the live session",
            steer=True,
        )

    assert err.value.code == "target_moved"
    assert not target._queue, "the replaced slot must not hold the message"
    assert not state._slots["chat-2"]._queue, "nor may the live replacement"


@pytest.mark.asyncio
async def test_a_mirror_linked_during_the_rpc_stops_the_steered_turn(tmp_path, monkeypatch):
    """A successful steer whose audience changed under it does not get to keep going.

    `STEER_STEERED` means kiro-cli acknowledged consumption, so the text cannot be
    recalled and the fallback arm's refusal is unavailable. What is still
    preventable is the turn's REMAINING output: `_deliver_cross_surface_reply`
    resolves the mirror live at reply-delivery time, so a mirror linked while the
    RPC was suspended would receive a reply shaped by a peer's text that
    `authorize_target` would have refused outright.

    Success semantics are kept deliberately -- the delivery did happen, and
    reporting `ok: False` would invite a retry that double-sends -- so the stop is
    recorded in the audit trail instead.

    Mutation guard: drop the post-RPC comparison and the turn runs on.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    stops: list[dict] = []

    async def _fake_stop(_state, _slot, **kwargs):
        stops.append({"slot": _slot.key, **kwargs})
        return {"stopped": True}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", _fake_stop)

    async def _mirror_then_accept(_msg):
        # The window: an operator attaches an outbound mirror while the steer RPC is
        # suspended on stdin.drain(). The gate that admitted this send saw none.
        state.sessions.set_mirror_link(_key(target), "C0FFEE", "1758.0001")
        return True

    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(side_effect=_mirror_then_accept)
    target._acp_client = client

    audits: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="the audience changed under this one",
        steer=True,
    )

    assert out["ok"] is True, "the delivery happened; a retry-inducing failure would double-send"
    assert out["steered"] is True
    assert stops, "a changed audience must stop the turn it was steered into"
    assert stops[0]["slot"] == "chat-2"
    assert stops[0]["escalate"] is False, "one automatic decision, not a person pressing again"
    assert stops[0]["source"] == "session_send_steer_containment"

    allowed = [a for a in audits if a.get("outcome") == "allowed"]
    assert allowed, "the send is still recorded as allowed"
    assert "mirrored" in allowed[-1]["detail"]["steer_stopped_on"]


@pytest.mark.asyncio
async def test_an_unchanged_audience_leaves_the_steered_turn_alone(tmp_path, monkeypatch):
    """The stop is for a real change only, never the ordinary successful steer.

    Without this the guard above would be indistinguishable from "every peer steer
    cancels the turn it lands in", which would make the feature useless.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    stops: list[dict] = []

    async def _fake_stop(_state, _slot, **kwargs):
        stops.append({"slot": _slot.key, **kwargs})
        return {"stopped": True}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", _fake_stop)

    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(return_value=True)
    target._acp_client = client

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="nothing moved under this one",
        steer=True,
    )

    assert out["steered"] is True
    assert not stops, "an unchanged audience must not cancel the target's turn"


@pytest.mark.asyncio
async def test_a_peer_steer_does_not_inherit_the_composers_linked_exemption(tmp_path):
    """A requeued peer steer is plain queued speech, not the session owner's.

    `directive_user_origin` exempts a queue entry from the drain's LINKED drop
    because "the author typed into the session's own surface". That is the composer;
    a `session_send` steer has no such author. The slot cannot tell the two callers
    apart, so the requeue reads the provenance the caller recorded instead of
    deriving it.

    Mutation guard: restore `directive_user_origin=not bool(slot._app)` and the
    entry comes back exempt.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    text = "requeued by the teardown"
    # What `steer_into_running_turn` records before it suspends, for a peer send.
    target._steer_user_origin[text] = False
    target._pending_steers.append(text)

    cr._requeue_unconsumed_steers(state, target)

    entry = next(q for q in target._queue if q["content"] == text)
    # Read exactly as the drain reads it (`q.get(...) is True`): the repository
    # omits the key for a falsy flag, and absent means not exempt.
    assert (
        entry.get("_directive_user_origin") is not True
    ), "a peer's steer must face the LINKED drop like any other queued prompt"
    assert sc.newly_held_constraints(
        {"linked": True},
        entry.get("meta"),
        directive_user_origin=entry.get("_directive_user_origin") is True,
    ) == ["linked"], "and the drain must actually drop it once a link appears"


@pytest.mark.asyncio
async def test_a_composer_steer_keeps_its_linked_exemption(tmp_path):
    """The other half: the composer's own steer is still designed behaviour.

    Its author typed into this session's surface, so linking the session is that
    owner's deliberate act and dropping their already-typed text would destroy user
    speech on a supported flow.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    text = "typed into this session's own composer"
    target._steer_user_origin[text] = True
    target._pending_steers.append(text)

    cr._requeue_unconsumed_steers(state, target)

    entry = next(q for q in target._queue if q["content"] == text)
    assert entry.get("_directive_user_origin") is True
    assert (
        sc.newly_held_constraints(
            {"linked": True},
            entry.get("meta"),
            directive_user_origin=entry.get("_directive_user_origin") is True,
        )
        == []
    ), "the owner linking their own session must not destroy what they already typed"


@pytest.mark.asyncio
async def test_a_requeued_steer_keeps_the_admission_the_gate_cleared(tmp_path):
    """The requeued entry is stamped with the SEND's containment, not the teardown's.

    The requeue runs in the turn's teardown, past the steer RPC's suspension. Reading
    the slot there folds a mirror linked during that suspension into the entry's
    admission baseline, after which `newly_held_constraints` sees nothing new and the
    drain forwards the output to an audience `authorize_target` refuses outright. The
    stamp has to come from the moment the gate cleared the send.

    Mutation guard: stamp `containment_meta(state, slot)` at the requeue and the
    drain reads the widened audience as admitted, so the entry survives.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    text = "requeued while the audience widened"
    # What `send_to_target` records before the RPC: the target was UNMIRRORED when its
    # gate cleared. Then the mirror appears while the RPC is suspended.
    target._steer_admissions[text] = sc.containment_meta(state, target)
    target._steer_user_origin[text] = False
    state.sessions.set_mirror_link(_key(target), "C0FFEE", "1758.0002")
    target._pending_steers.append(text)

    cr._requeue_unconsumed_steers(state, target)

    entry = next(q for q in target._queue if q["content"] == text)
    recorded = entry["meta"][sc.QUEUED_CONTAINMENT_META_KEY]
    assert recorded["mirrored"] is False, "the gate cleared an UNMIRRORED target"
    assert sc.newly_held_constraints(
        sc.containment_snapshot(state, target, on_probe_failure=True),
        entry.get("meta"),
        directive_user_origin=entry.get("_directive_user_origin") is True,
    ) == ["mirrored"], "so the drain must see the mirror as newly held and drop it"


@pytest.mark.asyncio
async def test_the_send_records_its_admission_before_the_rpc_suspends(tmp_path, monkeypatch):
    """End of the wire: `send_to_target` hands its gate's snapshot to the steer.

    Asserted from INSIDE the RPC, which is the only vantage point where "before the
    suspension" is observable. Without this the requeue's stamp would be correct in
    isolation and never reach it from the send.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    seen: dict = {}

    async def _inspect_then_accept(msg):
        seen["recorded"] = target._steer_admissions.get(msg)
        seen["origin"] = target._steer_user_origin.get(msg)
        return True

    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(side_effect=_inspect_then_accept)
    target._acp_client = client

    await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="check what the gate handed over",
        steer=True,
    )

    assert seen["recorded"] is not None, "the send must record the containment it cleared"
    assert seen["recorded"][sc.QUEUED_CONTAINMENT_META_KEY]["mirrored"] is False
    assert seen["origin"] is False, "and mark the text as not the session's own human's"


@pytest.mark.asyncio
async def test_a_requeued_steer_with_no_recorded_admission_carries_no_baseline(tmp_path):
    """No stamp means no containment key, not a slot read at teardown.

    The requeue has exactly ONE source for the baseline. Reading the slot as a
    fallback would reintroduce the hole for whichever caller omitted the stamp, and
    the LINKED exemption would not cover it: a new outbound mirror is never exempt,
    because the author does not control mirror links. An entry with no key lands on
    the drain's documented fail-closed floor instead, checked against every
    currently held constraint.

    Mutation guard: restore `else containment_meta(state, slot)` and the entry comes
    back stamped with the widened audience.
    """
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))

    text = "registered without a stamp"
    slot._pending_steers.append(text)
    state.sessions.set_mirror_link(_key(slot), "C0FFEE", "1758.0003")

    cr._requeue_unconsumed_steers(state, slot)

    entry = next(q for q in slot._queue if q["content"] == text)
    assert sc.QUEUED_CONTAINMENT_META_KEY not in (
        entry.get("meta") or {}
    ), "an unstamped entry must not be handed a baseline built at teardown"
    assert "mirrored" in sc.newly_held_constraints(
        sc.containment_snapshot(state, slot, on_probe_failure=True), entry.get("meta")
    ), "so the drain checks it against the current constraints and drops it"


@pytest.mark.asyncio
async def test_an_inter_stage_send_queues_instead_of_racing_the_plan(tmp_path):
    """Between a plan's stages the target is busy even though `running` says no.

    Each stage's `_run_chat` closes its own turn, so `slot.running` reads False in
    the gap while the plan is still live. Handing the prompt to a gate that read
    `running` alone started a SECOND turn racing the plan, with no recovery once two
    turns own the same slot. Every producer that must not do that reads
    `slot.running or slot._in_stage_execution`, and `enqueue_or_run_prompt` -- the
    admission point this path delivers through -- now does too, so this path needs no
    branch of its own.

    Mutation guard: drop `or self._in_stage_execution` from the gate in
    `state.enqueue_or_run_prompt` and this starts a turn.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    # The inter-stage shape exactly: no task in flight, plan still executing.
    target.task = None
    target._in_stage_execution = True

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="do not race the plan",
    )

    assert out["started"] is False, "a mid-plan send must not start a turn"
    assert target.task is None, "and must not have created one"
    entry = next(q for q in target._queue if q["content"].endswith("do not race the plan"))
    assert sc.QUEUED_CONTAINMENT_META_KEY in (
        entry.get("meta") or {}
    ), "the held prompt still carries its admission stamp for the drain"


@pytest.mark.asyncio
async def test_a_changed_audience_withholds_the_cross_surface_reply(tmp_path, monkeypatch):
    """The stop cannot outrun the turn, so the fence is what actually holds.

    `_deliver_cross_surface_reply` runs from the turn's own completion path and
    resolves the mirror live, so a turn that finishes while this send is suspended on
    `stdin.drain()` would publish to the new audience before any post-RPC check
    resumes -- and a sent reply cannot be recalled. The token is therefore added
    BEFORE the RPC and kept when a constraint newly holds, so the turn withholds its
    cross-surface leg regardless of who wins the race.

    Mutation guard: drop the pre-RPC `add`, or discard on the changed path, and the
    fence is empty here.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    async def _fake_stop(_state, _slot, **kwargs):
        return {"stopped": True}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", _fake_stop)

    fence_during_rpc: list[int] = []

    async def _mirror_then_accept(_msg):
        # Observed from inside the RPC: the fence is already up before the window
        # this test is about can open.
        fence_during_rpc.append(len(target._steer_audience_fences))
        state.sessions.set_mirror_link(_key(target), "C0FFEE", "1758.0004")
        return True

    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(side_effect=_mirror_then_accept)
    target._acp_client = client

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="the audience widened under this one",
        steer=True,
    )

    assert fence_during_rpc == [1], "the admission must be recorded before the RPC"
    assert out["steered"] is True
    assert target._steer_audience_fences, "the record is retained for the whole turn"
    assert (
        cr.cross_surface_withheld(state, target) is True
    ), "and the publisher, asked at delivery, withholds the cross-surface leg"


@pytest.mark.asyncio
async def test_an_unchanged_audience_still_gets_its_cross_surface_reply(tmp_path):
    """The fence is a hold, not a mute: an ordinary steer must not cost a mirror copy.

    Without this the safe direction would be indistinguishable from "any peer steer
    silences the target's channel audience for that turn".

    Mutation guard: stop discarding the token on the unchanged path and this fails.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(return_value=True)
    target._acp_client = client

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="nothing moved under this one",
        steer=True,
    )

    assert out["steered"] is True
    assert target._steer_audience_fences, (
        "the record is retained even when nothing moved: a mirror could still be "
        "bound before the reply publishes"
    )
    assert (
        cr.cross_surface_withheld(state, target) is False
    ), "but an unchanged audience gets its cross-surface reply, at no cost"


@pytest.mark.asyncio
async def test_the_reply_leg_consults_the_fence_before_publishing(tmp_path):
    """The predicate the turn's cross-surface leg is gated on, and the gate itself.

    Two halves, because either alone can be green while the feature is broken: the
    decision must be right, and the call site must actually ask it. The source check
    is a ratchet rather than a behavioural assertion -- `_run_chat` is not unit
    -testable at this seam -- and it pins the one line that makes the fence load
    -bearing.
    """
    state = _make_state(tmp_path)
    slot = _busy(_peer_target(state, "chat-2", _slot(state, "chat-1")))

    assert cr.cross_surface_withheld(state, slot) is False, "no peer steer, publish"
    slot._steer_audience_fences["tok"] = sc.containment_meta(state, slot)
    assert (
        cr.cross_surface_withheld(state, slot) is False
    ), "a recorded steer alone withholds nothing: the comparison is exact"
    state.sessions.set_mirror_link(_key(slot), "C0FFEE", "1758.0005")
    assert (
        cr.cross_surface_withheld(state, slot) is True
    ), "a constraint that newly holds since that admission withholds the leg"

    src = Path(cr.__file__).read_text(encoding="utf-8")
    deliver_calls = [
        line
        for line in src.splitlines()
        if "await _deliver_cross_surface_reply(" in line and not line.strip().startswith("#")
    ]
    assert len(deliver_calls) == 1, (
        "one channel-neutral call site only; a second would need its own fence "
        f"check: {deliver_calls}"
    )
    # EVERY cross-surface publication asks, not just the channel-neutral leg: Slack
    # is an audience too, and it resolves its thread owner live. Four sites -- the
    # channel-neutral reply, the Slack reply, the mid-turn tool stream, and the
    # teardown's final task append, which would otherwise publish a title whose
    # in-progress append was withheld.
    asks = src.count("cross_surface_withheld(state, slot)")
    assert asks == 4, f"expected four fenced publication sites, found {asks}"


@pytest.mark.asyncio
async def test_steer_on_an_idle_target_just_starts_the_turn(tmp_path, monkeypatch):
    """An idle slot has no turn to cut into, so `steer` changes nothing.

    The steer path is not merely unused here, it is never entered: on an idle slot
    the turn-scoped client is already cleared, so entering it would hand the text
    to nothing.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    entered: list[str] = []

    async def _never(_state, _slot, message):
        entered.append(message)
        return cd.STEER_UNAVAILABLE

    monkeypatch.setattr(cd, "steer_into_running_turn", _never)

    async def _fake_run_chat(_state, _slot, _prompt):
        return None

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="pick this up",
        steer=True,
    )
    await asyncio.sleep(0)

    assert out["started"] is True
    assert out["steered"] is False
    assert entered == [], "an idle target must not reach the steer path"
    assert not target._queue


@pytest.mark.asyncio
async def test_a_busy_target_still_queues_when_steer_is_not_asked_for(tmp_path):
    """The default is unchanged: no `steer`, no injection, even with a live client.

    Mutation guard: defaulting the flag on turns every existing caller's send into
    a mid-turn interruption.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))
    target._acp_client = _steerable(accepted=True)

    out = await sc.send_to_target(
        state, caller_session_key=_key(caller), target="chat-2", message="after this turn"
    )

    assert out["steered"] is False
    assert out["started"] is False
    assert target._acp_client.steer.await_count == 0
    assert any("after this turn" in q.get("content", "") for q in target._queue)


def test_send_to_a_remote_bound_target_is_refused_not_run_locally(tmp_path, monkeypatch):
    """A crew-bound target executes on the peer; session_send must not run its
    turn on THIS machine.

    ``send_to_target`` hands ``_run_chat`` to ``enqueue_or_run_prompt``, which has
    no remote/executor branch — so a bound target would run the crew's work here
    and diverge the local and peer transcripts. It is refused with a
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


@pytest.fixture
def private_dispatch(tmp_path, monkeypatch):

    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
    from kiro_crew.memory_stores import provision_member_memory

    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew", triggers="write")
    store = provision_member_memory(cfg, "writer")
    cfg.save()
    state = _make_state(tmp_path)
    from kiro_crew.history import ConversationLog

    state.conversation_log = ConversationLog()
    caller = _slot(state, "chat-conductor")
    return state, caller, store


@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_create_refuses_unreadable_delegation_identity(private_dispatch, monkeypatch, error_type):
    state, caller, _store = private_dispatch
    before = set(state._slots)

    def unavailable(*args):
        raise error_type("/private/identity/path")

    monkeypatch.setattr(sc, "read_session_execution", unavailable)
    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller), agent="writer"))
    assert exc.value.code == "memory_unavailable"
    assert "/private/identity/path" not in str(exc.value)
    assert set(state._slots) == before


@pytest.mark.parametrize("explicit", [False, True])
def test_private_creator_keeps_its_memory(private_dispatch, explicit):
    from kiro_crew.member_memory_auth import bind_private_session_store, read_private_session_store

    state, caller, store = private_dispatch
    caller.agent = "writer"
    caller.memory_store = store
    bind_private_session_store(_key(caller), store)
    state.conversation_log.update_metadata(_key(caller), {"memory_store": store})
    created = asyncio.run(
        sc.create_session(
            state, caller_session_key=_key(caller), agent="writer" if explicit else ""
        )
    )
    child = state.get_slot(created["target"])
    assert child.memory_store == store
    assert read_private_session_store(_key(child)) == store


@pytest.mark.parametrize("changed_field", ["agent", "memory_store", "linked_session_key"])
def test_create_rechecks_caller_identity_after_delegation(
    private_dispatch, monkeypatch, changed_field
):
    state, caller, _store = private_dispatch
    original = sc.read_session_execution
    before = set(state._slots)
    threads = []

    async def exercise():
        loop = asyncio.get_running_loop()
        changed = threading.Event()

        def check(key):
            threads.append(threading.get_ident())
            result = original(key)

            def mutate():
                setattr(caller, changed_field, "changed-selection")
                changed.set()

            loop.call_soon_threadsafe(mutate)
            assert changed.wait(5)
            return result

        monkeypatch.setattr(sc, "read_session_execution", check)
        with pytest.raises(sc.SessionControlError) as exc:
            await asyncio.wait_for(
                sc.create_session(state, caller_session_key=_key(caller), agent="writer"), 10
            )
        assert exc.value.code == "caller_memory_changed"

    asyncio.run(exercise())
    assert set(state._slots) == before
    assert threads and threading.get_ident() not in threads


@pytest.mark.parametrize("restored", [False, True])
def test_created_private_worker_can_start_after_birth_history(private_dispatch, tmp_path, restored):
    from kiro_crew.dashboard.chat_runner import _require_session_memory_assignment
    from kiro_crew.history import ConversationLog
    from kiro_crew.member_memory_auth import read_private_session_store

    state, caller, store = private_dispatch
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller), agent="writer"))
    child = state.get_slot(created["target"])
    key = _key(child)
    log = state.conversation_log
    assert log.has_log(key), "the test must exercise the persisted-at-birth path"
    if restored:
        log = ConversationLog()
    # The first turn confirms the protected assignment written before birth
    # metadata. Reopening the log must not be what grants that assignment.
    _require_session_memory_assignment(key, store)
    assert read_private_session_store(key) == store
    assert log.get_metadata(key)["memory_store"] == store


def test_private_worker_assignment_precedes_birth_history(private_dispatch, monkeypatch):
    from kiro_crew.member_memory_auth import read_private_session_store

    state, caller, store = private_dispatch
    log = state.conversation_log
    real_update = log.update_metadata
    assignments = []
    loop_threads = []

    def persist(key, metadata):
        loop_threads.append(threading.get_ident())
        assignments.append(read_private_session_store(key))
        return real_update(key, metadata)

    monkeypatch.setattr(log, "update_metadata", persist)
    asyncio.run(sc.create_session(state, caller_session_key=_key(caller), agent="writer"))
    assert assignments == [store]
    assert loop_threads and threading.get_ident() not in loop_threads


@pytest.mark.parametrize("explicit", [False, True])
def test_created_template_keeps_namespace_after_member_discovery(tmp_path, monkeypatch, explicit):

    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, resolve_agent_bindings
    from kiro_crew.member_memory_auth import read_private_session_store
    from kiro_crew.memory_stores import provision_member_memory
    from kiro_crew.session_agent_selection import resolve_session_agent_bindings

    template = "dispatched-template"
    monkeypatch.setattr(
        loader,
        "_materialized_kiro_agent",
        lambda name, project_dir=None: template if name == template else "",
    )
    cfg = KiroCrewConfig.load()
    assert template not in cfg.agents
    cfg.save()
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-conductor", agent=template)
    created = asyncio.run(
        sc.create_session(
            state,
            caller_session_key=_key(caller),
            agent=template if explicit else "",
        )
    )
    child = state.get_slot(created["target"])
    key = _key(child)
    assert state.conversation_log.get_metadata(key)["agent"] == template

    # Discovery imports the same template as a private roster member before
    # this empty conversation's first turn. The earlier choice must survive.
    cfg = KiroCrewConfig.load()
    cfg.agents[template] = KiroCrewAgentConfig(kiro_agent=template)
    member_store = provision_member_memory(cfg, template)
    cfg.save()
    cfg = KiroCrewConfig.load()
    assert resolve_agent_bindings(cfg, template, child.project).memory_store_name == member_store
    chosen = resolve_session_agent_bindings(
        resolve_agent_bindings, cfg, key, template, child.project
    )
    assert chosen.memory_store_name in ("", "default")
    assert chosen.selection_kind == "template"
    assert read_private_session_store(key) is None


@pytest.mark.parametrize("preexisting", [False, True])
@pytest.mark.parametrize("started_turn", [False, True])
def test_failed_private_birth_retains_its_permanent_assignment(
    private_dispatch, monkeypatch, preexisting, started_turn
):
    from kiro_crew.dashboard.chat_runner import _require_session_memory_assignment
    from kiro_crew.member_memory_auth import bind_private_session_store, read_private_session_store
    from kiro_crew.memory_stores import UnknownMemoryStore
    from kiro_crew.session_agent_selection import session_agent_selection_kind

    state, caller, store = private_dispatch
    before = set(state._slots)
    child_name = "chat-failed-private-birth"
    key = f"dashboard:{child_name}"
    monkeypatch.setattr("kiro_crew.dashboard.state._mint_slot_key", lambda *args: child_name)
    if preexisting:
        bind_private_session_store(key, store)

    def fail_history(requested_key, metadata):
        assert requested_key == key
        assert read_private_session_store(key) == store
        if started_turn:
            _require_session_memory_assignment(key, store)
            state._slots[child_name].append("user", "work already started", "msg msg-u")
        raise OSError("birth metadata could not be written")

    monkeypatch.setattr(state.conversation_log, "update_metadata", fail_history)
    with pytest.raises(OSError, match="birth metadata"):
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller), agent="writer"))
    assert set(state._slots) == (before | {child_name} if started_turn else before)
    # The birth was authorized and its slot was already addressable. History
    # failure cannot prove no turn consumed this identity, so the lifetime pin
    # must remain; neither Global nor another member may adopt the key later.
    assert read_private_session_store(key) == store
    assert session_agent_selection_kind(key, "writer") == "member"
    _require_session_memory_assignment(key, store)
    for other in ("default", "another-member-store"):
        with pytest.raises(UnknownMemoryStore, match="memory assignment"):
            _require_session_memory_assignment(key, other)


def test_cancelled_private_birth_drains_before_returning(private_dispatch, monkeypatch):
    from kiro_crew.member_memory_auth import read_private_session_store
    from kiro_crew.session_agent_selection import session_agent_selection_kind

    state, caller, store = private_dispatch
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    keys = []
    real_update = state.conversation_log.update_metadata

    def blocked_history(key, metadata):
        keys.append(key)
        entered.set()
        assert release.wait(5), "the test must release the birth writer"
        try:
            return real_update(key, metadata)
        finally:
            finished.set()

    monkeypatch.setattr(state.conversation_log, "update_metadata", blocked_history)

    async def exercise():
        task = asyncio.create_task(
            sc.create_session(state, caller_session_key=_key(caller), agent="writer")
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "cancellation must not abandon the history writer"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert finished.is_set()
        finally:
            release.set()
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    assert len(keys) == 1
    key = keys[0]
    child = next(slot for slot in state._slots.values() if _key(slot) == key)
    assert child._created_by == caller.key
    assert state.conversation_log.get_metadata(key)["memory_store"] == store
    assert read_private_session_store(key) == store
    assert session_agent_selection_kind(key, "writer") == "member"


@pytest.mark.parametrize("prior_context", ["history", "native"])
def test_private_worker_creation_cannot_adopt_an_old_session(
    private_dispatch, monkeypatch, prior_context
):
    from kiro_crew.member_memory_auth import read_private_session_store
    from kiro_crew.memory_stores import UnknownMemoryStore

    state, caller, _store = private_dispatch
    before = set(state._slots)
    child_name = "chat-private-worker-collision"
    key = f"dashboard:{child_name}"
    monkeypatch.setattr("kiro_crew.dashboard.state._mint_slot_key", lambda *args: child_name)
    if prior_context == "history":
        state.conversation_log.append(key, "user", "Existing conversation", agent="default")
    else:
        state.sessions.resumable_sid.side_effect = lambda requested: (
            "old-native-session" if requested == key else None
        )
    with pytest.raises(UnknownMemoryStore, match="new conversation"):
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller), agent="writer"))
    assert read_private_session_store(key) is None
    assert set(state._slots) == before


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
    that has been revoked. Simulated by revoking inside the project-dir
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


# ── session_create: filing at birth ─────────────────────────────────


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
        # Written only for a NON-DEFAULT memory store, because absence is what
        # means "the global store" -- so a newborn on the default store must NOT
        # carry it, and writing "default" here would make a session that predates
        # per-agent memory stores read differently from one saved today. The
        # named-store half is pinned by the next test, which is the direction that
        # can lose data: the key is slot-owned, so a merge that failed to write it
        # would drop the binding and silently return that session to the global
        # store.
        "memory_store",
    }
    for key in sorted(SLOT_OWNED_META_KEYS - excluded):
        assert key in meta, f"slot-owned field {key!r} missing after an empty-window forced save"
    assert meta.get("artifact") == "my-artifact"
    assert meta.get("reasoning_effort") == "high"
    assert meta.get("color_index") == 3
    assert meta.get("title") == "Pinned title"
    assert meta.get("title_origin") == "user"
    from kiro_crew.context import store_of_session

    assert (
        store_of_session(state.conversation_log, slot_history_key(child)) == ""
    ), "a newborn on the default store names no silo"


def test_the_empty_window_merge_keeps_a_named_memory_store(tmp_path):
    """A crew's silo must survive the merge, and the default must stay absent.

    ``memory_store`` is slot-owned, so ``carry_unowned_metadata`` will NOT
    preserve it from the previous record -- the merge has to write it or the key
    is gone. Losing it does not fail loudly: the session simply consolidates into
    the operator's global memory from then on, which is the one outcome per-crew
    isolation exists to prevent. Asserted in both directions, because the retract
    path (rebinding a crew back to the default store) depends on absence.
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])

    child.memory_store = "coding"
    asyncio.run(save_slot_off_loop(state, child, force=True))
    meta = state.conversation_log.get_metadata(slot_history_key(child))
    assert meta.get("memory_store") == "coding"

    # Rebinding to the default RETRACTS it. The merge cannot delete a key, so the
    # cleared form is a falsy value; what must hold is that the consolidator
    # resolves it to the global store again.
    from kiro_crew.context import store_of_session

    child.memory_store = "default"
    asyncio.run(save_slot_off_loop(state, child, force=True))
    meta = state.conversation_log.get_metadata(slot_history_key(child))
    assert not meta.get("memory_store"), meta.get("memory_store")
    assert store_of_session(state.conversation_log, slot_history_key(child)) == ""


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
        lambda cfg, agent_name=None, project_dir=None, **kwargs: dataclasses.replace(
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

    def _unresolved(cfg, agent_name=None, project_dir=None, **kwargs):
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

    def _record(cfg, agent_name=None, project_dir=None, **kwargs):
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


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["usable", "unavailable", "caller_closed"])
async def test_agent_binding_resolution_is_off_loop_and_precedes_allocation(
    tmp_path, monkeypatch, outcome
):
    from kiro_crew.memory_stores import UnknownMemoryStore

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = set(state._slots)
    allocate = MagicMock(wraps=state.get_or_create_slot)
    monkeypatch.setattr(state, "get_or_create_slot", allocate)
    real_resolve = sc.resolve_agent_bindings
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    lookup_threads = []

    def resolve(*args, **kwargs):
        lookup_threads.append(threading.get_ident())
        if outcome == "unavailable":
            raise UnknownMemoryStore("member memory cannot be read")
        bindings = real_resolve(*args, **kwargs)
        if outcome == "caller_closed":
            loop.call_soon_threadsafe(state._slots.pop, caller.key, None)
        return bindings

    monkeypatch.setattr(sc, "resolve_agent_bindings", resolve)

    if outcome == "usable":
        created = await sc.create_session(state, caller_session_key=_key(caller))
        allocate.assert_called_once()
        child = state.get_slot(created["target"])
        assert child is not None and child.workspace == caller.workspace
    else:
        with pytest.raises(sc.SessionControlError) as error:
            await sc.create_session(state, caller_session_key=_key(caller))
        assert error.value.code == (
            "agent_unverifiable" if outcome == "unavailable" else "caller_not_open"
        )
        allocate.assert_not_called()
        assert not set(state._slots) - before
    assert len(lookup_threads) == 1
    assert lookup_threads[0] != loop_thread


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
    boundary its creator does not sit behind.
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


def test_the_creator_session_id_is_frozen_at_mint_not_read_live(tmp_path, monkeypatch):
    """The child's parent lineage must cite the creator that was live AT MINT.

    The creator SID is stamped on the child at ``session_create`` time, from the
    live caller handle. If instead it were read live at the child's first turn,
    a creator slot closed and replaced in between (a distinct handle with its own
    session id) would make the child cite the REPLACEMENT's crew log -- and that id
    lands in the append-only, immutable ``session/opened`` entry with no recovery.

    Mutation guard: re-read the creator SID live at emit (from the current slot
    handle) and this test reddens, because the replacement below carries a
    different session id than the one frozen at mint.
    """
    from unittest.mock import MagicMock

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller.agent = "researcher"
    creator_client = MagicMock()
    creator_client.session_id = "acp-sess-creator-at-mint"
    caller._acp_client = creator_client
    _agent_resolves(monkeypatch, "default")

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])
    assert child is not None

    # Frozen at mint from the live caller handle, and witnessed by this process.
    assert getattr(child, "_created_by_sid", "") == "acp-sess-creator-at-mint"
    assert getattr(child, "_lineage_minted", False) is True

    # The sid is NOT written into the birth metadata: the transcript is a file an
    # agent's file tools can edit, so nothing read back from it may become the
    # gateway-authored crew-log lineage. Only the attribution rides the metadata,
    # for the ownership boundary.
    written = state.conversation_log.get_metadata(sc.slot_history_key(child))
    assert written.get("created_by") == caller.key
    assert "created_by_sid" not in written

    # Now the creator's handle is replaced with a distinct session id -- the exact
    # window the finding names. The frozen value on the child must NOT follow it.
    replacement = MagicMock()
    replacement.session_id = "acp-sess-replacement"
    caller._acp_client = replacement
    assert getattr(child, "_created_by_sid", "") == "acp-sess-creator-at-mint"


def test_a_slot_nobody_minted_in_this_process_carries_no_lineage_witness(tmp_path):
    """A slot that was not created through ``session_create`` in THIS process --
    a person's own tab, a fork, a restore -- has no lineage witness, whatever its
    ``_created_by`` says. The crew-log ``session/opened.parent`` write is gated on
    the witness, so restored or hand-edited attribution never becomes lineage.

    Mutation guard: default the flag to True, or set it on the plain
    ``get_or_create_slot`` path, and this test reddens.
    """
    state = _make_state(tmp_path)
    plain = _slot(state, "chat-9")
    plain._created_by = "chat-1"  # what a restore from transcript metadata sets
    assert getattr(plain, "_lineage_minted", False) is False
    assert getattr(plain, "_created_by_sid", "") == ""


def test_the_opened_entry_cites_lineage_only_from_a_witnessed_mint():
    """``_crew_log_lineage`` is the one seam between the slot and the crew-log
    ``session/opened.parent`` write. It yields the creator only when this process
    minted the slot; attribution that arrived any other way -- restored from a
    transcript an agent's file tools can edit, or set by hand -- yields nothing,
    so the emitter writes no ``parent`` and no metadata edit can forge lineage.

    Mutation guard: drop the witness check and the second case reddens; read the
    sid live instead of the frozen field and the first case reddens.
    """
    from types import SimpleNamespace

    from kiro_crew.dashboard.chat_runner import _crew_log_lineage

    minted = SimpleNamespace(
        _created_by="chat-1", _created_by_sid="acp-sess-creator-at-mint", _lineage_minted=True
    )
    assert _crew_log_lineage(minted) == ("chat-1", "acp-sess-creator-at-mint")

    restored = SimpleNamespace(
        _created_by="chat-1", _created_by_sid="acp-forged-by-editing-the-transcript"
    )
    assert _crew_log_lineage(restored) == ("", "")
    restored_explicit = SimpleNamespace(
        _created_by="chat-1", _created_by_sid="acp-sess-x", _lineage_minted=False
    )
    assert _crew_log_lineage(restored_explicit) == ("", "")

    minted_without_handle = SimpleNamespace(
        _created_by="chat-1", _created_by_sid="", _lineage_minted=True
    )
    assert _crew_log_lineage(minted_without_handle) == ("chat-1", "")


def test_the_opened_entry_records_the_session_class_from_the_live_slot(monkeypatch):
    """``_crew_log_class`` is the seam between the slot and the ``class`` write.

    The facts a reader needs to decide whether one session may read this one's crew
    log, taken at the moment the log is opened because the reader that asks is
    usually asking about a session that has since closed. A cron tab's link is not a
    channel: it names the job's own run and republishes to nobody, which is the
    exemption ``CRON_LINK_PREFIX`` carries wherever that boundary is enforced.

    Mutation guard: drop the cron exemption and the third case reddens; drop the
    mirror probe and the fourth reddens; let the probe's exception escape and the
    fifth reddens instead of recording a channel.
    """
    from types import SimpleNamespace

    from kiro_crew.dashboard import session_control as sc
    from kiro_crew.dashboard.chat_runner import _crew_log_class
    from kiro_crew.dashboard.session_control import CRON_LINK_PREFIX

    mirrored: dict[str, bool] = {"value": False}
    monkeypatch.setattr(sc, "_has_channel_mirror", lambda state, slot: mirrored["value"])
    state = SimpleNamespace()

    plain = SimpleNamespace(memory_mode="persistent", _app="", linked_session_key="", key="chat-9")
    assert _crew_log_class(state, plain) == ("persistent", "", False)

    owned = SimpleNamespace(
        memory_mode="incognito", _app="travel-desk", linked_session_key="", key="chat-9"
    )
    assert _crew_log_class(state, owned) == ("incognito", "travel-desk", False)

    cron = SimpleNamespace(
        memory_mode="persistent", _app="", linked_session_key=f"{CRON_LINK_PREFIX}nightly", key="c"
    )
    assert _crew_log_class(state, cron) == ("persistent", "", False)

    linked = SimpleNamespace(
        memory_mode="persistent", _app="", linked_session_key="slack:C123", key="chat-9"
    )
    assert _crew_log_class(state, linked) == ("persistent", "", True)

    # An OUTBOUND mirror lives in the session store rather than on the slot, so a
    # slot with an empty link can still be republishing every turn.
    mirrored["value"] = True
    assert _crew_log_class(state, plain) == ("persistent", "", True)

    # A probe that cannot answer at all records a channel rather than recording
    # "unpublished": opening a log must not raise, and the safe direction costs a
    # dispatcher one refusal it can ask about.
    def _raises(state, slot):
        raise RuntimeError("no store")

    monkeypatch.setattr(sc, "_has_channel_mirror", _raises)
    assert _crew_log_class(state, plain) == ("persistent", "", True)


def test_an_oversize_creator_session_id_is_dropped_at_mint_not_retained(tmp_path, monkeypatch):
    """The creator sid is backend-authored, so it is bounded where it is RETAINED.

    An id past ``MAX_ACP_SESSION_ID_LEN`` is not stored on the child -- dropped,
    never truncated, so it cannot push the child's ``session/opened`` entry over
    the crew log's size cap and lose the whole entry. The sid is optional: absent
    is a legal record, a clipped id would be a wrong one. (The sid never reaches
    the birth metadata in any case; the bound is about the in-memory slot and the
    entry it feeds.)
    """
    from unittest.mock import MagicMock

    from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller.agent = "researcher"
    creator_client = MagicMock()
    creator_client.session_id = "s" * (MAX_ACP_SESSION_ID_LEN + 1)
    caller._acp_client = creator_client
    _agent_resolves(monkeypatch, "default")

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])
    assert child is not None
    assert getattr(child, "_created_by_sid", "") == ""
    # Still a witnessed mint: the slot half of the lineage is recorded, sid absent.
    assert getattr(child, "_lineage_minted", False) is True
    written = state.conversation_log.get_metadata(sc.slot_history_key(child))
    assert "created_by_sid" not in written
    # The attribution itself is unaffected: the slot key is ours, not the backend's.
    assert getattr(child, "_created_by", "") == caller.key


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
    # this test requires.
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


def test_the_delivery_path_never_claims_a_turn_consumed_a_steer(tmp_path, monkeypatch):
    """The steer RPC proves the bytes left, not that any turn received them.

    A steer reported delivered can still be sitting pending when its turn ends, and
    that turn's teardown requeues it to a LATER turn. A ledger entry written from
    here would already be on disk saying the earlier turn received it, and an
    append-only entry cannot be moved the way the transcript row can. So this path
    writes no ledger entry at all, which is why the vocabulary carries no steer type.

    Mutation guard: recording the delivered case here -- from a live ordinal or any
    other guess -- reddens this.
    """
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "use the other branch"
    slot._acp_client = _steerable(accepted=True)

    appended: list[str] = []
    queued_for: list[str] = []
    monkeypatch.setattr(crew_log_emit, "session_id_of", lambda _client: "acp-1")
    # Spied on the shared write seam rather than on one entry point, so an entry
    # written under ANY type is caught rather than only a steer-shaped one.
    monkeypatch.setattr(
        crew_log_emit,
        "_write",
        lambda _sid, entry_type, *a, **kw: appended.append(entry_type),
    )
    monkeypatch.setattr(
        crew_log_emit, "on_message_queued", lambda sid, **_kw: queued_for.append(sid)
    )
    # A turn IS running, so a guess would have had something plausible to record.
    monkeypatch.setattr(crew_log_emit, "live_turn", lambda _sid: 13)

    def _consume_inside_the_rpc(*_a, **_kw):
        # The running turn takes the registration, which is what makes the
        # reconciliation report delivered.
        slot._pending_steers.clear()
        slot._steer_confirmed.add(slot._steer_delivery_ids[text])
        return True

    slot._acp_client.steer = AsyncMock(side_effect=_consume_inside_the_rpc)

    result = asyncio.run(chat_delivery.steer_into_running_turn(state, slot, text))

    assert result == chat_delivery.STEER_STEERED
    assert appended == [], "the delivery path must not assert consumption"
    assert queued_for == [], "and it is not a queued message either"


def test_a_stop_race_that_only_expects_a_requeue_records_nothing(tmp_path, monkeypatch):
    """An expected requeue is a prediction, and this log records observation.

    On this path the steer is still pending and a stop has landed, so the teardown
    is expected to requeue the text. It may not: a second stop can hard-kill and
    discard the pending steers first, and then a `message/queued` written here
    permanently claims a queue entry that was never made. The text still reaches
    the log if it runs, as the `message/received` of the turn that runs it.

    Mutation guard: recording the queued outcome here reddens this.
    """
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "stop and do this instead"
    slot._acp_client = _steerable(accepted=True)

    queued: list[str] = []
    monkeypatch.setattr(crew_log_emit, "session_id_of", lambda _client: "acp-1")
    monkeypatch.setattr(crew_log_emit, "on_message_queued", lambda sid, **_kw: queued.append(sid))

    def _stop_without_requeueing(*_a, **_kw):
        # A stop lands while the steer is still registered, and nothing has moved
        # it into the queue: exactly the state where a requeue is only expected.
        slot._stop_generation = int(getattr(slot, "_stop_generation", 0) or 0) + 1
        return True

    slot._acp_client.steer = AsyncMock(side_effect=_stop_without_requeueing)

    result = asyncio.run(chat_delivery.steer_into_running_turn(state, slot, text))

    assert result == chat_delivery.STEER_REQUEUED
    assert queued == [], "an expected requeue is not an observed one"


def test_a_requeued_steer_is_recorded_as_a_queued_message(tmp_path, monkeypatch):
    """The one ledger fact this path CAN prove, named by the right id.

    The requeue moves the text straight into the slot queue without passing the
    append that records `message/queued`, so nothing else in the system knows it
    happened. The id recorded is the QUEUE ENTRY's own -- the same quantity
    `queue_for_next_turn` records, so one reader joins both against the queue. The
    client's `sendId` is a different namespace minted by a different party and
    would look like a queue id without being one. No turn rides on it: a queued
    message belongs to no turn until the one that runs it starts.

    Mutation guard: recording `send_id` reddens this, because the two differ here.
    """
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.dashboard import chat_delivery

    state = _make_state(tmp_path)
    slot = _busy(_slot(state, "chat-2"))
    text = "run the deploy"
    slot._acp_client = _steerable(accepted=True)

    queued: list[dict] = []
    monkeypatch.setattr(crew_log_emit, "session_id_of", lambda _client: "acp-1")
    monkeypatch.setattr(
        crew_log_emit, "on_message_queued", lambda sid, **kw: queued.append(dict(kw))
    )
    seen: dict[str, str] = {}

    def _requeue_like_the_teardown(*_a, **_kw):
        did = slot._steer_delivery_ids.get(text, "")
        slot._pending_steers.clear()
        seen["qid"] = str(slot.queue_insert(0, text, meta={"steer_delivery_id": did}))
        return True

    slot._acp_client.steer = AsyncMock(side_effect=_requeue_like_the_teardown)

    result = asyncio.run(
        chat_delivery.steer_into_running_turn(state, slot, text, send_id="s-client-side")
    )

    assert result == chat_delivery.STEER_REQUEUED
    assert len(queued) == 1 and queued[0]["source"] == "steer"
    assert seen["qid"], "the harness never queued anything -- the test proves nothing"
    assert (
        queued[0]["queued_seq"] == seen["qid"]
    ), "the entry must name the queue entry it became, not the client's send id"
    assert queued[0]["queued_seq"] != "s-client-side"


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
    constructs the log -- trust-dir creation, key validation, and on Windows a
    DACL write that can block on a network volume round-trip. This can genuinely
    be that first call, because
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

    The live-slot ceiling has one home -- ``state.MAX_LIVE_SLOTS`` in the module
    that owns ``live_slot_count()`` -- and each slot-creating door (session
    create, chat fork, session import) imports that one name, so the effective
    limit cannot diverge by which door the caller came through. This
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
    assert slot.is_closing is False  # failed close must not fence future monitor admission
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


@pytest.mark.asyncio
async def test_a_failed_containment_stop_still_reports_the_delivery(tmp_path, monkeypatch):
    """A stop that raises must not turn a consumed steer into a reported failure.

    By the time the containment stop runs, `STEER_STEERED` means kiro-cli has already
    taken the text: the turn will act on it whatever happens next. The stop only
    narrows what that turn goes on to publish. If it raises and the exception escapes,
    the caller is told the send failed for text that was in fact delivered, and the
    obvious client response -- send it again -- delivers the same message twice.

    Containment does not depend on the stop succeeding: the audience fence recorded
    before the RPC stays on the slot, and the publisher consults it at delivery.

    Mutation guard: drop the `try/except` around `stop_slot_turn` and this raises
    instead of returning, and the audit loses `steer_containment_stop_failed`.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    async def _exploding_stop(_state, _slot, **kwargs):
        raise RuntimeError("session was already gone")

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", _exploding_stop)

    audits: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))

    client = MagicMock()
    client.supports_steer = True

    async def _steer_then_replace_the_slot(*_a, **_kw):
        # A change the code names explicitly: the slot object behind this key is
        # swapped while the RPC is suspended, so the turn holding the text belongs
        # to a session the name has stopped resolving to. Chosen over binding a
        # mirror because it needs no session-store fixture to be deterministic.
        state._slots["chat-2"] = _peer_target(state, "chat-2-replacement", caller)
        return True

    client.steer = _steer_then_replace_the_slot
    target._acp_client = client

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="the turn already has this",
        steer=True,
    )

    assert out["ok"] is True, "a delivered steer must not be reported as failed"
    assert out["steered"] is True, "the turn consumed the text; say so"
    sends = [a for a in audits if a.get("operation") == "send"]
    assert sends, "the successful delivery must still be audited"
    detail = sends[-1]["detail"]
    assert detail.get("steer_containment_stop_failed") is True, (
        "a stop that raised must be visible in the trail, not swallowed: " f"{detail}"
    )
    assert detail.get("steer_stopped_on"), "the constraint that newly held must be named"


@pytest.mark.asyncio
async def test_a_rolled_over_turn_is_not_cancelled_by_containment(tmp_path, monkeypatch):
    """The stop must reach the turn this text entered, not whichever runs later.

    `slot.running` is true for ANY turn. If the steered turn ends while the RPC is
    suspended and a queued prompt starts the next one, an unguarded stop cancels work
    that never received this steer -- someone else's turn, killed for a constraint
    that has nothing to do with it. `_turn_generation` increments on every task
    assignment, so comparing it identifies the turn across the suspension.

    The steered turn is over by then, so nothing is lost by not stopping it: whatever
    it published is published, and the fence still withholds the legs of the turn that
    holds it.

    Mutation guard: drop the generation comparison and the stop fires here.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    stops: list[str] = []

    async def _fake_stop(_state, _slot, **kwargs):
        stops.append(_slot.key)
        return {"stopped": True}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", _fake_stop)

    audits: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))

    client = MagicMock()
    client.supports_steer = True

    async def _steer_then_roll_the_turn(*_a, **_kw):
        # The turn that took the text finishes and the next one starts, which is what
        # a queue drain does between turns. Driven through the same setter the runtime
        # uses so the generation moves exactly as it would in production.
        target.task = asyncio.current_task()
        # ...and an audience change, so the stop would otherwise fire.
        state._slots["chat-2"] = _peer_target(state, "chat-2-replacement", caller)
        return True

    client.steer = _steer_then_roll_the_turn
    target._acp_client = client

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="this belonged to the turn that already ended",
        steer=True,
    )

    assert out["steered"] is True
    assert not stops, f"a successor turn must not be cancelled: {stops}"
    detail = [a for a in audits if a.get("operation") == "send"][-1]["detail"]
    assert "turn_rolled_over_stop_skipped" in detail["steer_stopped_on"], (
        "the skipped stop must be visible in the trail rather than looking like a "
        f"containment that never triggered: {detail}"
    )


def test_the_post_rpc_regate_warms_the_config_first():
    """The fallback gate sits after an await, so it needs its own warm.

    `authorize_target` reaches `session_control_enabled`, which on a cache invalidated
    by a config edit runs `KiroCrewConfig.load()` synchronously and stalls the shared
    loop for every other session. The warm at the top of `send_to_target` predates the
    steer RPC, so the suspension voids its guarantee -- which is the reopened hole
    `prewarm_enabled_check`'s own docstring describes.

    Asserted on source order because what is being guarded is the function's own
    `await`, which a runtime probe cannot observe without racing it.

    Mutation guard: delete the second warm and the regate is the first thing after
    the RPC again.
    """
    src = Path(sc.__file__).read_text(encoding="utf-8")
    send = src[src.index("async def send_to_target") :]
    send = send[: send.index("\ndef read_messages")]
    rpc_at = send.index("await steer_into_running_turn(")
    regate_at = send.index("regated = authorize_target(")
    warm_at = send.index("await prewarm_enabled_check()", rpc_at)
    assert rpc_at < warm_at < regate_at, (
        "the post-RPC regate must be preceded by its own prewarm: "
        f"rpc={rpc_at} warm={warm_at} regate={regate_at}"
    )


@pytest.mark.asyncio
async def test_the_inter_stage_append_persists_before_returning_success(tmp_path):
    """An acknowledged prompt must not live only in memory.

    A mid-plan send queues the prompt and the function then returns a success
    receipt. Until the plan's drain reaches it the queue is its only record, so a
    restart inside the ordinary flush interval loses a message the sender was told had
    landed. Every other producer that appends and reports success writes immediately.

    Asserted on BEHAVIOUR, not on the order of two lines in this module's source: the
    append and the write both moved into `state.enqueue_or_run_prompt` when the
    inter-stage branch here was deleted in favour of the central gate, and a
    source-text pin would have reported that as a lost guarantee rather than a moved
    one. What the sender is owed is the write, wherever it is started from.

    Mutation guard: remove the `start_queue_persist` call from
    `enqueue_or_run_prompt`'s queue branch and no write starts here.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    # The inter-stage shape exactly: no task in flight, plan still executing.
    target.task = None
    target._in_stage_execution = True
    flushed: list = []
    loop = asyncio.get_running_loop()
    target_flushed = asyncio.Event()

    def _record(slot):
        # Runs in an executor thread: hand the signal back to the loop.
        flushed.append(slot)
        if slot is target:
            loop.call_soon_threadsafe(target_flushed.set)

    state.flush_slot_now = _record

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="acknowledged, so it must be durable",
    )

    assert out["started"] is False, "the prompt was queued, so a receipt was given"
    # Started, not awaited, and it runs in an executor thread, so event-loop ticks
    # do not measure it: wait on the target's own write, bounded, then pin the
    # recorder exactly -- one write, for this slot and no other.
    try:
        await asyncio.wait_for(target_flushed.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass
    assert flushed == [target], (
        "the immediate queue write must have been started for the slot whose queue "
        f"now holds the acknowledged prompt: {flushed}"
    )


@pytest.mark.asyncio
async def test_a_requeue_onto_a_replaced_slot_is_refused_not_reported_as_sent(
    tmp_path, monkeypatch
):
    """A requeue that lands on a detached slot must not report success.

    `STEER_REQUEUED` means the turn ended under the RPC and its teardown moved the
    text onto THIS slot object's queue. If the key stopped resolving to this object
    in that window -- the target closed and recreated -- that queue belongs to
    something no drain will reach, so the prompt is gone. Reporting `queued` for it
    tells the sender their words are waiting when they are not, and the sender has
    no way to find out.

    The sibling arms both guard this: the steered arm records `slot_replaced` and
    the fallback arm re-runs the gate. This arm was the gap.

    Mutation guard: drop the identity check and this returns ok instead of raising.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    client = MagicMock()
    client.supports_steer = True

    async def _requeue_then_replace_the_slot(*_a, **_kw):
        # The teardown's requeue, then the close-and-recreate that detaches the
        # object holding it. Ordered this way because that is the sequence the
        # finding describes: the text is already on the old queue when the key
        # moves.
        target._pending_steers.clear()
        state._slots["chat-2"] = _peer_target(state, "chat-2-replacement", caller)
        return True

    client.steer = _requeue_then_replace_the_slot
    target._acp_client = client

    # Patched on chat_delivery, NOT on sc: `send_to_target` imports this name
    # through a deferred import inside the function, so a patch on the importing
    # module is never consulted.
    import kiro_crew.dashboard.chat_delivery as cd

    async def _fake_steer(*_a, **_kw):
        await _requeue_then_replace_the_slot()
        return cd.STEER_REQUEUED

    monkeypatch.setattr(cd, "steer_into_running_turn", _fake_steer)

    with pytest.raises(sc.SessionControlError) as caught:
        await sc.send_to_target(
            state,
            caller_session_key=_key(caller),
            target="chat-2",
            message="this was requeued onto a slot that no longer exists",
            steer=True,
        )

    assert caught.value.code == "target_moved", (
        "a requeue onto a detached slot must be refused as target_moved, not "
        f"reported as delivered: {caught.value.code}"
    )


# ── the channel mark travels with a session_send ──
#
# ``chat_folder_steering_set`` refuses a conversation channel words have entered.
# A tab that took a linked thread's replies and was then unlinked can relay those
# words to a clean peer with ``session_send``; the peer must be marked before it
# runs them, on every delivery arm.


@pytest.mark.asyncio
@pytest.mark.parametrize("taint", ["channel_turn_seen", "channel_origin"])
async def test_a_send_from_a_marked_caller_marks_the_target(tmp_path, taint):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    if taint == "channel_turn_seen":
        caller._channel_turn_seen = True
    else:
        caller.channel_origin = True
    target = _busy(_peer_target(state, "chat-2", caller))
    assert target.to_dict()["channel_turn_seen"] is False

    await sc.send_to_target(
        state, caller_session_key=_key(caller), target="chat-2", message="set the steering"
    )

    assert target._channel_turn_seen is True
    assert target.to_dict()["channel_turn_seen"] is True


@pytest.mark.asyncio
async def test_a_steered_send_from_a_marked_caller_marks_the_target(tmp_path):
    """The steer arm hands the text to the running turn, so it is marked too."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._channel_turn_seen = True
    target = _busy(_peer_target(state, "chat-2", caller))
    target._acp_client = _steerable(accepted=True)

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="set the steering",
        steer=True,
    )

    assert out["steered"] is True
    assert target._channel_turn_seen is True


@pytest.mark.asyncio
async def test_a_send_from_a_clean_caller_leaves_the_target_clean(tmp_path):
    """Contrast: the mark is carried, never invented."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))

    await sc.send_to_target(
        state, caller_session_key=_key(caller), target="chat-2", message="hello"
    )

    assert target._channel_turn_seen is False


def _off_loop_mirror(monkeypatch):
    """A session-store mirror visible ONLY to a probe made off the event loop.

    ``SessionMap``'s accessors hold a threading lock across its disk save, so the
    mark's mirror probe must run in a worker thread. The stand-in answers True
    there and False on the loop thread, so a mark that lands proves the probe ran
    off the loop (and the on-loop gates, which read the same helper, see no
    mirror and admit the call).
    """
    import threading

    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _probe(_state, _slot, **_kw):
        seen.append(threading.get_ident())
        return threading.get_ident() != loop_thread

    monkeypatch.setattr(sc, "_has_channel_mirror", _probe)
    return loop_thread, seen


@pytest.mark.asyncio
async def test_a_send_from_a_store_mirrored_caller_marks_the_target_off_the_loop(
    tmp_path, monkeypatch
):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _busy(_peer_target(state, "chat-2", caller))
    loop_thread, seen = _off_loop_mirror(monkeypatch)

    await sc.send_to_target(
        state, caller_session_key=_key(caller), target="chat-2", message="set the steering"
    )

    assert target._channel_turn_seen is True
    assert any(t != loop_thread for t in seen), "the mirror must be probed off the loop"


@pytest.mark.asyncio
async def test_a_read_of_a_store_mirrored_target_marks_the_reader_off_the_loop(
    tmp_path, monkeypatch
):
    state = _make_state(tmp_path)
    reader = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", reader)
    loop_thread, seen = _off_loop_mirror(monkeypatch)

    result = sc.read_messages(state, caller_session_key=_key(reader), target="chat-2")
    # The synchronous read judges only the target's in-memory facts.
    assert reader._channel_turn_seen is False

    await sc.mark_reader_for_mirrored_target(
        state, caller_session_key=_key(reader), target_slot_key=result["target"]
    )

    assert reader._channel_turn_seen is True
    assert target is state.get_slot(result["target"])
    assert any(t != loop_thread for t in seen)


def test_the_read_handler_runs_the_off_loop_mirror_mark():
    """The async read endpoint is the caller that completes the read direction."""
    import inspect

    from kiro_crew.dashboard.handlers import session_control as handler

    assert "mark_reader_for_mirrored_target" in inspect.getsource(handler.api_session_control_read)


def test_a_restored_newborn_is_marked_like_every_hydrated_slot(tmp_path, monkeypatch):
    """A created-then-idle session restored from its birth line fails closed too.

    The birth line is transcript metadata an agent can write, so nothing in it
    may certify the conversation clean; the live newborn itself stays clean.
    """
    from kiro_crew.dashboard.chat_persistence import restore_channel_mark

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)

    result = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    created = state.get_slot(result["target"])
    written = state.conversation_log.get_metadata(sc.slot_history_key(created))

    assert created._channel_turn_seen is False
    restored = type(created)("restored-newborn")
    restore_channel_mark(restored, written)
    assert restored._channel_turn_seen is True


@pytest.mark.asyncio
async def test_a_send_that_delivers_nothing_leaves_the_target_unmarked(tmp_path):
    """Steer unavailable, then the re-gate refuses: nothing reached the target.

    The mark is set on the arm that delivers, so a send refused after a steer
    that landed nowhere must not permanently taint the target.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._channel_turn_seen = True
    target = _busy(_peer_target(state, "chat-2", caller))

    async def _replace_then_fail(_msg):
        state._slots.pop("chat-2", None)
        _busy(_peer_target(state, "chat-2", caller))
        return False

    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(side_effect=_replace_then_fail)
    target._acp_client = client

    with pytest.raises(sc.SessionControlError):
        await sc.send_to_target(
            state,
            caller_session_key=_key(caller),
            target="chat-2",
            message="set the steering",
            steer=True,
        )

    assert target._channel_turn_seen is False
    assert target._channel_marks_in_flight == 0
    assert target.to_dict()["channel_turn_seen"] is False


@pytest.mark.asyncio
async def test_a_steer_is_refused_while_its_rpc_is_in_flight(tmp_path):
    """The running turn reads a steer the moment it lands, so the row must read
    as marked DURING the RPC, before the delivery outcome is known."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._channel_turn_seen = True
    target = _busy(_peer_target(state, "chat-2", caller))
    seen_during_rpc: list[bool] = []

    async def _observe(_msg):
        seen_during_rpc.append(target.to_dict()["channel_turn_seen"])
        return True

    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(side_effect=_observe)
    target._acp_client = client

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="set the steering",
        steer=True,
    )

    assert seen_during_rpc == [True]
    assert out["steered"] is True
    assert target._channel_turn_seen is True
    assert target._channel_marks_in_flight == 0


@pytest.mark.asyncio
async def test_an_unavailable_steer_that_falls_back_to_the_queue_marks(tmp_path):
    """No steer client: the fallback queue arm is the delivery, and it marks."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller._channel_turn_seen = True
    target = _busy(_peer_target(state, "chat-2", caller))

    out = await sc.send_to_target(
        state,
        caller_session_key=_key(caller),
        target="chat-2",
        message="set the steering",
        steer=True,
    )

    assert out["steered"] is False
    assert target._queue
    assert target._channel_turn_seen is True


# ── the channel mark travels with a session_read_message ──


@pytest.mark.parametrize("taint", ["channel_turn_seen", "channel_origin"])
def test_reading_a_marked_conversation_marks_the_reader(tmp_path, taint):
    """Rows read from a marked target land in the caller's context."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.append("user", "set the steering to /etc", "msg msg-u")
    if taint == "channel_turn_seen":
        target._channel_turn_seen = True
    else:
        target.channel_origin = True

    out = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")

    assert out["messages"]
    assert caller._channel_turn_seen is True
    assert caller.to_dict()["channel_turn_seen"] is True


def test_reading_a_clean_conversation_leaves_the_reader_clean(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target.append("user", "hello", "msg msg-u")

    sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")

    assert caller._channel_turn_seen is False


def test_an_empty_read_of_a_marked_conversation_still_marks_the_reader(tmp_path):
    """No new rows, but the response still carries the target's title, which a
    channel-origin tab takes from its channel conversation."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    target._channel_turn_seen = True

    out = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")

    assert not out["messages"]
    assert "title" in out
    assert caller._channel_turn_seen is True
