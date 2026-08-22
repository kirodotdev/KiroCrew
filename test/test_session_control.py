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
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.config import loader
from kiro_crew.dashboard import chat_delivery as cd
from kiro_crew.dashboard import session_control as sc
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


def test_scheduled_caller_cannot_control_anyone(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "cron-abc123")
    _slot(state, "chat-2")
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="chat-2", operation="read"
        )
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


def test_the_trust_switch_needs_a_positive_grant():
    """``agent.session_control`` must not enable itself from absence OR from junk.

    Two independent ways this switch could grant cross-session reach without
    anyone asking for it, and both are pinned here:

    * **Absent.** The three tools ride on the existing assignable
      `kirocrew-dashboard` server, so an operator who assigned that server for
      folder work would gain peer stop-and-read purely by upgrading. Both the
      ``.get`` default and the dataclass field default must therefore be
      ``False`` -- a grant has to be written down.
    * **Malformed.** ``bool("false")`` is ``True``, so a plain coercion loads a
      quoted opt-out as ENABLED and a user who wrote it in an editor that quotes
      values would keep cross-session control on while believing it off.
      ``_safe_bool`` accepts only a real bool and falls back to ``False``.

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
    assert wiring.endswith(
        "False)"
    ), f"the fallback must be False so a malformed value fails closed, got: {wiring}"
    assert '"session_control", False' in wiring, (
        "an absent setting must read as DISABLED -- otherwise an existing "
        f"kirocrew-dashboard assignment gains peer stop/read on upgrade, got: {wiring}"
    )
    # The field default is the second absent path: it is what a config object
    # built without going through the loader resolves to.
    assert loader.AgentConfig().session_control is False, (
        "the dataclass default must also be False, or a config built outside the "
        "loader grants cross-session reach with nothing written down"
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
    the cap, so a caller can no longer tell how much history exists.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    for i in range(3):
        target.append("assistant", f"live {i}", "msg msg-a")
    # Stand in for the trim: 5,000 rows already aged into the frozen prefix.
    target._disk_older_count = 5000

    out = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")

    assert out["total"] == 5003, "total must count the frozen prefix, not just the window"
    assert [m["index"] for m in out["messages"]] == [5000, 5001, 5002]
    # No cursor is offered once trimming has started, because positions built on
    # `_disk_older_count` (which counts transient rows) cannot line up with
    # durable-only indexes. Handing back a cursor the next call would reject is
    # worse than admitting it is gone, and the ABSENCE of `next_since` is the
    # whole signal -- no separate flag restates it.
    assert "next_since" not in out
    assert "cursor_exact" not in out, "absence of next_since is the only signal"


def test_cursor_pagination_is_refused_once_rows_have_been_trimmed(tmp_path):
    """A `since` read on a trimmed session must fail loudly, not duplicate a row.

    `_disk_older_count` counts every trimmed row including transient ones, while
    positions here are durable-only. Once a transient row is trimmed into the
    frozen prefix the two disagree, every position shifts, and a `since` read
    serves a durable message the caller already had.

    Mutation guard: dropping the refusal silently returns the shifted window.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _peer_target(state, "chat-2", caller)
    for i in range(3):
        target.append("assistant", f"live {i}", "msg msg-a")
    target._disk_older_count = 5000

    with pytest.raises(sc.SessionControlError) as exc:
        sc.read_messages(state, caller_session_key=_key(caller), target="chat-2", since=5000)
    assert exc.value.code == "cursor_unavailable"
    assert exc.value.status == 409

    # The tail read is the documented fallback and still works.
    tail = sc.read_messages(state, caller_session_key=_key(caller), target="chat-2")
    assert [m["content"] for m in tail["messages"]] == ["live 0", "live 1", "live 2"]


def test_an_untrimmed_session_still_reports_a_gap_free_cursor(tmp_path):
    """With nothing trimmed the cursor is exact, which is the common case.

    The old version of this test asserted a `trimmed` gap report on a session
    whose rows HAD aged out — that path is now refused outright (see
    `cursor_unavailable`), because the gap count was derived from the same mixed
    counter that made the positions wrong.
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

    async def _fake_stop(_state, slot, *, source):
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

    async def _fake_stop(_state, slot, *, source):
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

    async def _fake_stop(_state, slot, *, source):
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

    async def _fake_stop(_state, slot, *, source):
        seen["slot"] = slot.key
        seen["source"] = source
        return {"ok": True}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", _fake_stop)

    out = asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-2"))

    assert out["target"] == "chat-2"
    # No `force`: `stop_slot_turn` escalates on a second press regardless of one,
    # so the tool does not advertise a flag a first call cannot honour.
    assert seen == {"slot": "chat-2", "source": "session_control"}


def test_stop_is_refused_for_a_session_out_of_bounds(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _slot(state, "chat-hidden", memory_mode="incognito")
    with pytest.raises(sc.SessionControlError):
        asyncio.run(sc.stop_target(state, caller_session_key=_key(caller), target="chat-hidden"))


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

    Mutation guard: checking the cap only before the await lets two concurrent
    creations land over the ceiling.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")

    def _resolve_then_fill(_workspace):
        for i in range(sc._MAX_SLOTS_FOR_CREATE):
            _slot(state, f"filler-{i}")
        return str(tmp_path)

    monkeypatch.setattr(sc, "default_project_dir", _resolve_then_fill)

    with pytest.raises(sc.SessionControlError) as err:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    assert err.value.code == "slot_cap_reached"


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
