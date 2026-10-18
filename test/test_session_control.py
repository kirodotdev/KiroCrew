"""Session control: one chat session sending to, stopping, and reading another.

The suite is organized around the two things that can go wrong here. First,
authorization: every refusal is asserted against the REAL slot objects, because
the guards read ``memory_mode`` / ``workspace`` / ``_app`` off the production
class and a permissive test double would let a dead guard look alive. Second,
delivery: a message must land exactly once — the interesting failures are the
double-delivery and silent-drop paths around a steer that the live client
refuses mid-flight.

Besides target resolution and the authorization refusals, this file keeps the
steer-reconciliation cases of ``chat_delivery.steer_into_running_turn`` and the
limit-capped read cursor case that sit in that section. The shared fixtures live in
``session_control_helpers.py``, the audit cases in ``test_session_control_audit.py``,
route authentication and the config switch in ``test_session_control_routes.py``,
and the other verbs in ``test_session_control_read.py``, ``test_session_control_stop.py``,
``test_session_control_send.py``, ``test_session_control_steer_delivery.py``,
``test_session_control_create.py``, ``test_session_control_create_filing.py``,
``test_session_control_create_allocation.py`` and ``test_session_control_close.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _busy,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _peer_target,
    _slot,
    _steerable,
)

from kiro_crew.dashboard import chat_delivery as cd
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key

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
