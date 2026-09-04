"""Drain-time re-validation of queued prompts (issue #5911).

Authorization is decided at ADMISSION — ``authorize_target`` for
``session_send``, the authenticated composer for a human typing into a busy
session — but a busy target QUEUES the prompt and delivers it later, and the
containment those decisions rest on can change in between: a target authorized
while unlinked can gain a channel or mirror link before its queue drains.

These tests pin the three-part fix end to end: producers stamp the
admission-time containment on the queue entry (``containment_meta``), the drain
re-asserts the same constraints and drops what no longer qualifies, and a drop
is loud (queue card retracted, visible transcript notice, SEL record) — never a
silent vanish. They also pin the two designed non-drops: a constraint already
held at admission is not a change (channel-born sessions keep draining), and
structural orchestration entries are the runner's own machinery, exempt.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import (
    CRON_NOTIFICATION_KIND,
    SUBAGENT_COMPLETION_KIND,
    SYNTHETIC_RECOVERY_KIND,
    slot_history_key,
)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """Run in the shipped (enabled) session-control state without reading config."""
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _inline_audit(monkeypatch):
    """Route SEL writes inline to a mock: assertable, and no executor thread
    outlives the test."""
    fake = MagicMock()
    monkeypatch.setattr(sc, "sel", lambda: fake)
    monkeypatch.setattr(sc, "_sel_off_loop", lambda write, what: write())
    return fake


def _busy(slot):
    """``running`` is derived (``task is not None and not task.done()``)."""
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def _link(slot):
    slot.linked_session_key = "C0LINKED|1700000000.000100"
    return slot


async def _never_runs(state, slot, prompt):  # pragma: no cover - queued, not run
    raise AssertionError("a queued prompt must not start a turn at enqueue")


def _snapshot_of(entry):
    return entry.get("meta", {}).get(sc.QUEUED_CONTAINMENT_META_KEY)


# ── Producers stamp the admission-time snapshot ──────────────────────────────


def test_human_typed_enqueue_stamps_admission_snapshot(tmp_path):
    """The composer path: ``enqueue_or_run_prompt`` on a busy slot records the
    constraints that held at admission on the entry it queues."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))

    started = slot.enqueue_or_run_prompt("hello there", _never_runs, state)

    assert started is False
    snap = _snapshot_of(slot._queue[0])
    assert snap == {
        "linked": False,
        "mirrored": False,
        "mirror_identity": "",
        "crew": False,
        "ephemeral": False,
        "app": False,
        "unattended": False,
        "workspace": "default",
    }


def test_session_send_enqueue_stamps_admission_snapshot(tmp_path):
    """The ``session_send`` path shares the same seam and the same stamp."""
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))

    out = asyncio.run(
        sc.send_to_target(
            state,
            caller_session_key=slot_history_key(caller),
            target="chat-2",
            message="queued message",
        )
    )

    assert out["started"] is False
    snap = _snapshot_of(target._queue[0])
    assert snap is not None
    assert not any(v for k, v in snap.items() if k != "workspace")
    assert isinstance(snap["workspace"], str) and snap["workspace"]


def test_channel_born_enqueue_records_linked_true(tmp_path):
    """A slot that is ALREADY channel-linked at admission records that fact, so
    its own channel's queued messages keep draining (designed behaviour)."""
    state = _make_state(tmp_path)
    slot = _busy(_link(state.get_or_create_slot("chat-1")))

    slot.queue_append("from the thread", meta=sc.containment_meta(state, slot))

    assert _snapshot_of(slot._queue[0])["linked"] is True


def test_requeued_steer_is_stamped(tmp_path):
    """A steer degraded to a queue card is plain user speech re-entering the
    queue; the requeue stamps it like any other plain producer."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._pending_steers = ["steer me"]

    cr._requeue_unconsumed_steers(state, slot)

    assert _snapshot_of(slot._queue[0]) is not None


# ── The drain drops what no longer qualifies, loudly ─────────────────────────


def test_linked_after_enqueue_drops_with_visible_notice(tmp_path, _inline_audit):
    """The issue's core scenario: authorized while unlinked, linked before the
    drain. The entry is dropped, the transcript says why, and the SEL records it."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot.enqueue_or_run_prompt("exfil me", _never_runs, state)

    _link(slot)
    cr._drop_stale_admissions(state, slot)

    assert slot._queue == []
    notices = [m for m in slot.messages if m.get("role") == "notice"]
    assert notices and "linked to a channel" in notices[-1]["content"]
    assert "dropped" in notices[-1]["content"].lower()
    call = _inline_audit.log_tool_invocation.call_args
    assert call.kwargs["outcome"] == "denied"
    assert "linked" in call.kwargs["metadata"]["newly_held"]


def test_mirror_added_after_enqueue_drops(tmp_path):
    """The other mechanism the issue names: an OUTBOUND mirror link appearing
    between enqueue and drain (the ``_has_channel_mirror`` gap)."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot.enqueue_or_run_prompt("exfil me", _never_runs, state)

    state.sessions.set_mirror_link(slot_history_key(slot), "C0MIRROR", "1700000000.000200")
    cr._drop_stale_admissions(state, slot)

    assert slot._queue == []
    notices = [m for m in slot.messages if m.get("role") == "notice"]
    assert notices and "outbound channel mirror" in notices[-1]["content"]


def test_mirror_retargeted_while_queued_drops(tmp_path, _inline_audit):
    """A mirror RETARGETED between enqueue and drain (A -> B) keeps the boolean
    true at both ends while substituting the audience — the identity comparison
    must catch what the boolean cannot. Directive provenance does not exempt it:
    the message's author does not control mirror links."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    state.sessions.set_mirror_link(slot_history_key(slot), "C0AUDIENCE_A", "1700000000.000400")
    slot.enqueue_or_run_prompt("meant for audience A", _never_runs, state)
    slot._queue[0]["_directive_user_origin"] = True

    state.sessions.set_mirror_link(slot_history_key(slot), "C0AUDIENCE_B", "1700000000.000500")
    cr._drop_stale_admissions(state, slot)

    assert slot._queue == []
    notices = [m for m in slot.messages if m.get("role") == "notice"]
    assert notices and "retargeted" in notices[-1]["content"]
    call = _inline_audit.log_tool_invocation.call_args
    assert call.kwargs["outcome"] == "denied"
    assert "mirror_retarget" in call.kwargs["metadata"]["newly_held"]


def test_mirror_unchanged_identity_still_drains(tmp_path):
    """The identity comparison must not turn an UNCHANGED admitted mirror into a
    drop: a session mirrored to the same channel at both ends keeps its queue."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    state.sessions.set_mirror_link(slot_history_key(slot), "C0AUDIENCE_A", "1700000000.000400")
    slot.enqueue_or_run_prompt("same audience", _never_runs, state)

    cr._drop_stale_admissions(state, slot)

    assert [q["content"] for q in slot._queue] == ["same audience"]


def test_unchanged_containment_drains(tmp_path):
    """No containment change, no drop — including a constraint that already
    held at admission (a channel-born session keeps its queue)."""
    state = _make_state(tmp_path)
    plain = _busy(state.get_or_create_slot("chat-1"))
    plain.enqueue_or_run_prompt("still fine", _never_runs, state)
    born_linked = _busy(_link(state.get_or_create_slot("chat-2")))
    born_linked.queue_append("thread msg", meta=sc.containment_meta(state, born_linked))

    cr._drop_stale_admissions(state, plain)
    cr._drop_stale_admissions(state, born_linked)

    assert [q["content"] for q in plain._queue] == ["still fine"]
    assert [q["content"] for q in born_linked._queue] == ["thread msg"]


def test_unmarked_legacy_entry_fails_closed(tmp_path):
    """An entry with no snapshot is checked against the FULL current-constraint
    set: an untagged producer can never ride a queued prompt past a boundary
    the tagged paths respect."""
    state = _make_state(tmp_path)
    slot = _link(state.get_or_create_slot("chat-1"))
    slot._queue = [{"id": "legacy1", "content": "hi"}]

    cr._drop_stale_admissions(state, slot)

    assert slot._queue == []


def test_unmarked_entry_in_unconstrained_slot_survives(tmp_path):
    """Fail-closed is about constraints that HOLD: with none holding, an
    unmarked entry drains exactly as before this change."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._queue = [{"id": "legacy1", "content": "hi"}]

    cr._drop_stale_admissions(state, slot)

    assert [q["content"] for q in slot._queue] == ["hi"]


def test_cron_and_subagent_entries_are_exempt(tmp_path):
    """Cron notifications and sub-agent completions are minted fresh by trusted
    internal producers for THIS slot's own turn lifecycle — channel-born
    sessions receive them by design, so the sweep must not touch them even
    unmarked."""
    state = _make_state(tmp_path)
    slot = _link(state.get_or_create_slot("chat-1"))
    slot.queue_append("[cron] tick", kind=CRON_NOTIFICATION_KIND)
    slot.queue_append("[Subagent completion event] done", kind=SUBAGENT_COMPLETION_KIND)

    cr._drop_stale_admissions(state, slot)

    assert len(slot._queue) == 2


def test_unmarked_recovery_entry_fails_closed(tmp_path):
    """A synthetic-recovery entry replays externally admitted content verbatim
    under a fresh queue id, so it is NOT exempt: unmarked, it is checked
    against the full boolean constraint set like any untagged producer — the
    retry window must not ride past a link the original admission never saw."""
    state = _make_state(tmp_path)
    slot = _link(state.get_or_create_slot("chat-1"))
    slot.queue_append("continue", kind=SYNTHETIC_RECOVERY_KIND)

    cr._drop_stale_admissions(state, slot)

    assert slot._queue == []


def test_stamped_recovery_entry_follows_its_admission(tmp_path):
    """A recovery requeue stamps admission context at requeue time: linked
    recorded True keeps draining (channel-born recovery machinery survives),
    while a link appearing AFTER the requeue drops the retry."""
    state = _make_state(tmp_path)
    born_linked = _link(state.get_or_create_slot("chat-1"))
    born_linked.queue_append(
        "continue",
        kind=SYNTHETIC_RECOVERY_KIND,
        meta=sc.containment_meta(state, born_linked),
    )
    cr._drop_stale_admissions(state, born_linked)
    assert [q["content"] for q in born_linked._queue] == ["continue"]

    plain = state.get_or_create_slot("chat-2")
    plain.queue_append(
        "replayed send prompt",
        kind=SYNTHETIC_RECOVERY_KIND,
        meta=sc.containment_meta(state, plain),
    )
    _link(plain)
    cr._drop_stale_admissions(state, plain)
    assert plain._queue == []


# ── Helper semantics ─────────────────────────────────────────────────────────


def test_snapshot_probe_failure_directions(tmp_path):
    """The mirror probe fails closed in BOTH directions: at enqueue an
    unreadable link records not-mirrored (least-authorized admission, so the
    drain re-validates), at drain it reads as mirrored (refuse delivery) and
    carries the ``mirror_unverified`` marker so the notice can say the state
    could not be verified instead of asserting a mirror appeared."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    state.sessions.get_mirror_link = MagicMock(side_effect=RuntimeError("store down"))

    at_enqueue = sc.containment_snapshot(state, slot, on_probe_failure=False)
    at_drain = sc.containment_snapshot(state, slot, on_probe_failure=True)

    assert at_enqueue["mirrored"] is False
    assert "mirror_unverified" not in at_enqueue
    assert at_drain["mirrored"] is True
    assert at_drain["mirror_unverified"] is True
    # The marker is wording/telemetry, never a constraint to compare.
    assert "mirror_unverified" not in sc.newly_held_constraints(
        at_drain, {sc.QUEUED_CONTAINMENT_META_KEY: dict(at_drain)}
    )
    # An unverifiable drain-side probe fails closed EVEN for an entry admitted
    # under a mirror: the audience may have been retargeted since admission and
    # there is no identity to compare, so "mirrored at both ends" is not
    # positive authorization.
    assert "mirrored" in sc.newly_held_constraints(
        at_drain,
        {sc.QUEUED_CONTAINMENT_META_KEY: {"mirrored": True, "mirror_identity": "slack:C0A:1"}},
    )


def test_admitted_mirrored_entry_drops_on_drain_probe_failure(tmp_path):
    """End to end: a session mirrored at admission whose store stops answering
    by drain time REFUSES delivery — an unverifiable mirror state may hide a
    retarget, and `authorize_target`'s posture is refuse-not-open. The notice
    says the state could not be verified."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    state.sessions.set_mirror_link(slot_history_key(slot), "C0AUDIENCE_A", "1700000000.000600")
    slot.enqueue_or_run_prompt("admitted under mirror A", _never_runs, state)

    state.sessions.get_mirror_link = MagicMock(side_effect=RuntimeError("store down"))
    cr._drop_stale_admissions(state, slot)

    assert slot._queue == []
    notices = [m for m in slot.messages if m.get("role") == "notice"]
    assert notices and "could not be verified" in notices[-1]["content"]


def test_malformed_snapshot_fails_closed():
    """Queue meta is untrusted plumbing: any shape other than a dict snapshot
    degrades to the all-False baseline."""
    now = {"linked": True, "mirrored": False}
    assert sc.newly_held_constraints(now, None) == ["linked"]
    assert sc.newly_held_constraints(now, {"other": 1}) == ["linked"]
    assert sc.newly_held_constraints(now, {sc.QUEUED_CONTAINMENT_META_KEY: "junk"}) == ["linked"]
    assert sc.newly_held_constraints(now, {sc.QUEUED_CONTAINMENT_META_KEY: {"linked": True}}) == []


def test_workspace_change_invalidates_admission(tmp_path):
    """`authorize_target`'s seventh refusal is `workspace_mismatch`, and
    `slot.workspace` is mutable while a queue waits (the agent-switch endpoint
    re-derives it): a prompt admitted under workspace A must not run with
    workspace B's memory, lessons and project context."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot.workspace = "team-a"
    slot.enqueue_or_run_prompt("workspace-a prompt", _never_runs, state)

    slot.workspace = "team-b"
    cr._drop_stale_admissions(state, slot)

    assert slot._queue == []
    notices = [m for m in slot.messages if m.get("role") == "notice"]
    assert notices and "different workspace" in notices[-1]["content"]


def test_workspace_not_compared_for_unmarked_entries(tmp_path):
    """An unmarked entry has no least-authorized workspace to assume, so its
    fail-closed floor stays the boolean set — it drains in any workspace when
    no boolean constraint holds."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.workspace = "team-b"
    slot._queue = [{"id": "legacy1", "content": "hi"}]

    cr._drop_stale_admissions(state, slot)

    assert [q["content"] for q in slot._queue] == ["hi"]


def test_directive_user_origin_exempts_linked_only(tmp_path):
    """A human linking their OWN busy session must not destroy the messages
    they already typed (`api_chat` applies no linked refusal to composer
    input) — but the exemption covers ONLY the link: a NEW outbound mirror
    (which the message's author does not control) and a workspace change both
    still drop a directive entry."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot.workspace = "team-a"
    slot.queue_append(
        "typed by the owner",
        meta=sc.containment_meta(state, slot),
        directive_user_origin=True,
    )

    _link(slot)
    cr._drop_stale_admissions(state, slot)
    assert [q["content"] for q in slot._queue] == ["typed by the owner"]

    state.sessions.set_mirror_link(slot_history_key(slot), "C0MIRROR", "1700000000.000300")
    cr._drop_stale_admissions(state, slot)
    assert slot._queue == []

    slot2 = _busy(state.get_or_create_slot("chat-2"))
    slot2.workspace = "team-a"
    slot2.queue_append(
        "typed by the owner",
        meta=sc.containment_meta(state, slot2),
        directive_user_origin=True,
    )
    slot2.workspace = "team-b"
    cr._drop_stale_admissions(state, slot2)
    assert slot2._queue == []


def test_drop_retracts_the_queue_card_unconditionally(tmp_path):
    """The frontend's queue card comes from the producer's queue_push, not a
    transcript placeholder row, so the retraction broadcast must not be gated
    on a placeholder existing."""
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot.enqueue_or_run_prompt("exfil me", _never_runs, state)
    qid = slot._queue[0]["id"]

    _link(slot)
    cr._drop_stale_admissions(state, slot)

    pops = [c for c in state.broadcast_ws.call_args_list if c.args and c.args[0] == "queue_pop"]
    assert pops and pops[-1].args[1]["queue_id"] == qid


def test_probe_failure_drop_says_unverified_not_gained(tmp_path):
    """When the drain-side mirror probe fails, the refusal stands (fail closed)
    but the notice must describe an unverifiable state, not assert a mirror
    appeared."""
    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot.enqueue_or_run_prompt("exfil me", _never_runs, state)

    state.sessions.get_mirror_link = MagicMock(side_effect=RuntimeError("store down"))
    cr._drop_stale_admissions(state, slot)

    assert slot._queue == []
    notices = [m for m in slot.messages if m.get("role") == "notice"]
    assert notices and "could not be verified" in notices[-1]["content"]
    assert "gained an outbound channel mirror" not in notices[-1]["content"]


def test_drop_audit_uses_the_effective_session_key(tmp_path, _inline_audit):
    """A linked slot's turns run under `linked_session_key`; filing the drop
    under the slot key would hide exactly the drops this feature records."""
    from kiro_crew.dashboard.chat_utils import effective_session_key

    state = _make_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot.enqueue_or_run_prompt("exfil me", _never_runs, state)

    _link(slot)
    cr._drop_stale_admissions(state, slot)

    call = _inline_audit.log_tool_invocation.call_args
    assert call.kwargs["session_key"] == effective_session_key(slot)
    assert call.kwargs["metadata"]["target"] == slot.key


@pytest.mark.asyncio
async def test_consumed_entry_emits_an_allowed_audit(tmp_path, monkeypatch, _inline_audit):
    """The ALLOW side of the drain's permission decision is audited too,
    matching `authorize_target`'s convention: an entry that passes
    re-validation and becomes a turn leaves an `allowed` SEL row naming its
    queue id — emitted at consumption, so waiting across sweeps does not
    multiply rows."""
    state = _make_state(tmp_path)
    state.subagents = None
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot.enqueue_or_run_prompt("still fine", _never_runs, state)
    qid = slot._queue[0]["id"]
    slot.task = None

    async def _stub_run_chat(_state, _slot, _prompt, **_kwargs):
        return None

    def _fake_spawn(_state, _slot, coro):
        coro.close()
        task = MagicMock()
        task.done.return_value = True
        return task

    monkeypatch.setattr(cr, "_run_chat", _stub_run_chat)
    monkeypatch.setattr(cr, "spawn_guarded_turn", _fake_spawn)

    assert await cr._start_next_queued_turn(state, slot) is True

    allowed = [
        c
        for c in _inline_audit.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "allowed"
        and c.kwargs.get("tool_name") == "queue_drain_revalidation"
    ]
    assert allowed and qid in allowed[-1].kwargs["metadata"]["queue_ids"]


def test_requeued_steer_in_a_plain_slot_carries_human_provenance(tmp_path):
    """The only steer producer is the api_chat composer branch, and app
    isolation confines app requests to app slots — so a non-app slot's
    requeued steer is human speech and keeps the audience exemption."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._pending_steers = ["steer me"]

    cr._requeue_unconsumed_steers(state, slot)

    assert slot._queue[0].get("_directive_user_origin") is True


# ── The full drain path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_drops_stale_entry_and_starts_nothing(tmp_path, monkeypatch):
    """Through ``_start_next_queued_turn``: a stale entry is dropped before the
    dequeue ever sees it, and no turn is spawned for it."""
    state = _make_state(tmp_path)
    state.subagents = None
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot.enqueue_or_run_prompt("exfil me", _never_runs, state)
    slot.task = None  # the turn ended; the tail-drain runs
    _link(slot)

    spawned = MagicMock()

    def _no_spawn(_state, _slot, coro):  # pragma: no cover - must not be reached
        coro.close()
        spawned()
        return MagicMock()

    monkeypatch.setattr(cr, "spawn_guarded_turn", _no_spawn)
    started = await cr._start_next_queued_turn(state, slot)

    assert started is False
    assert not spawned.called
    assert slot._queue == []
    assert any(m.get("role") == "notice" for m in slot.messages)


@pytest.mark.asyncio
async def test_drain_strips_snapshot_from_the_persisted_row(tmp_path, monkeypatch):
    """A surviving entry's snapshot is queue plumbing, consumed at the drain —
    it must not ride into the transcript row's persisted meta."""
    state = _make_state(tmp_path)
    state.subagents = None
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot.enqueue_or_run_prompt("still fine", _never_runs, state)
    slot.task = None

    async def _stub_run_chat(_state, _slot, _prompt, **_kwargs):
        return None

    def _fake_spawn(_state, _slot, coro):
        coro.close()
        task = MagicMock()
        task.done.return_value = True
        return task

    monkeypatch.setattr(cr, "_run_chat", _stub_run_chat)
    monkeypatch.setattr(cr, "spawn_guarded_turn", _fake_spawn)

    started = await cr._start_next_queued_turn(state, slot)

    assert started is True
    user_rows = [m for m in slot.messages if m.get("role") == "user"]
    assert user_rows, "the drained entry must land as a user row"
    row_meta = user_rows[-1].get("meta") or {}
    assert sc.QUEUED_CONTAINMENT_META_KEY not in row_meta


# ── Constraint-set parity with authorize_target (#5994) ──────────────────────
#
# The constraint set now has two hand-maintained spellings: `authorize_target`
# refuses admission inline, and `containment_snapshot` re-derives the same
# predicates so the drain can re-assert them. Drift between them fails OPEN --
# a refusal added to `authorize_target` alone is enforced at enqueue and NOT at
# delivery, silently reopening the enqueue->drain window this file exists to
# close, with nothing red. These tests make that divergence fail CI instead.
#
# ADDING A REFUSAL TO `authorize_target`? Classify it in exactly one of the two
# tables below. Either it is a per-slot containment constraint -- then it needs
# a `containment_snapshot` key, or the drain cannot re-check it -- or it is not,
# and it belongs in `_NON_CONTAINMENT_REFUSALS` with a reason.

# Target-side containment refusals, mapped to the snapshot key that re-asserts
# each one at drain time. `workspace_mismatch` is the seventh (see
# `test_workspace_change_invalidates_admission`); it is an identity rather than
# a boolean, but it is still a constraint the drain compares.
_TARGET_CONTAINMENT_REFUSALS = {
    "linked_session_target": "linked",
    "mirrored_target": "mirrored",
    "crew_mode_target": "crew",
    "ephemeral_target": "ephemeral",
    "app_scoped_target": "app",
    "unattended_target": "unattended",
    "workspace_mismatch": "workspace",
}

_NON_CONTAINMENT_REFUSALS = {
    # Caller-side: a property of who is asking, not of the target slot. The
    # drain has nothing to re-validate -- by then the caller's turn is over.
    "caller_unidentified",
    "unattended_caller",
    "app_scoped_caller",
    "ephemeral_caller",
    "linked_session_caller",
    "mirrored_caller",
    "caller_gone",
    # Caller-relative ownership: fires only for member callers, comparing the
    # caller's key to the target's created_by. Both are written once at birth
    # and never mutate, so the relation that admitted an entry cannot flip
    # while it waits -- and the drain has no caller left to re-evaluate.
    "not_creator",
    # Not containment: global config state, a resolution failure, and the
    # self-target guard. None of the three can change while an entry waits in a
    # queue in a way the drain could act on.
    "session_control_disabled",
    "target_not_found",
    "self_target",
}

# Comparison detail rather than constraints: both are set by the mirror probe to
# tell the drain HOW to compare `mirrored`, and neither has its own refusal.
_SNAPSHOT_NON_CONSTRAINT_KEYS = {"mirror_identity", "mirror_unverified"}


def _authorize_target_refusal_codes() -> set[str]:
    """Every literal ``deny(..., code)`` in :func:`authorize_target`, from source.

    Parsed rather than hand-listed on purpose. A hand-listed copy would be a
    THIRD spelling of the constraint set, free to drift from the other two --
    which is the failure this test exists to catch, not to reproduce.

    One ``deny`` call re-raises a resolution failure with ``exc.code`` rather
    than a literal; it carries no new constraint, so a non-literal code is
    skipped instead of failing the parse.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(sc.authorize_target)))
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "deny"):
            continue
        positional = node.args[1] if len(node.args) >= 2 else None
        keyword = next((kw.value for kw in node.keywords if kw.arg == "code"), None)
        for candidate in (keyword, positional):
            if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
                codes.add(candidate.value)
                break
    return codes


def _base_snapshot(tmp_path) -> dict:
    """A snapshot of an unconstrained slot: every constraint key, no probe extras."""
    state = _make_state(tmp_path)
    return sc.containment_snapshot(
        state, state.get_or_create_slot("chat-1"), on_probe_failure=False
    )


def test_the_parse_finds_the_refusals_it_is_asked_to_pin():
    """Guard the guard: an empty or tiny parse would make the tests below vacuous.

    If ``authorize_target``'s refusals ever stop being spelled as ``deny(...,
    "code")`` the extraction silently returns less, and a parity test that
    compares against nothing passes while pinning nothing.
    """
    codes = _authorize_target_refusal_codes()
    assert len(codes) >= len(_TARGET_CONTAINMENT_REFUSALS) + len(_NON_CONTAINMENT_REFUSALS)
    assert "workspace_mismatch" in codes, "the seventh refusal must be visible to the parse"


def test_every_refusal_is_classified():
    """A new refusal in ``authorize_target`` must be classified, not ignored."""
    classified = set(_TARGET_CONTAINMENT_REFUSALS) | _NON_CONTAINMENT_REFUSALS
    unclassified = _authorize_target_refusal_codes() - classified
    assert not unclassified, (
        f"unclassified refusal(s) in authorize_target: {sorted(unclassified)}. "
        "Add each to _TARGET_CONTAINMENT_REFUSALS with the containment_snapshot key "
        "that re-asserts it at drain time, or to _NON_CONTAINMENT_REFUSALS with a "
        "reason it needs no drain-time check."
    )


def test_every_classified_refusal_still_exists():
    """A removed refusal must not leave a stale mapping claiming coverage."""
    codes = _authorize_target_refusal_codes()
    stale = (set(_TARGET_CONTAINMENT_REFUSALS) | _NON_CONTAINMENT_REFUSALS) - codes
    assert not stale, (
        f"refusal(s) mapped here but no longer raised by authorize_target: {sorted(stale)}. "
        "Drop the entry so the tables keep describing the code."
    )


def test_every_containment_refusal_has_a_snapshot_key(tmp_path):
    """The fail-open direction: enforced at admission, unchecked at drain."""
    snap = _base_snapshot(tmp_path)
    missing = set(_TARGET_CONTAINMENT_REFUSALS.values()) - set(snap)
    assert not missing, (
        f"containment_snapshot does not record {sorted(missing)}, so the drain cannot "
        "re-assert the matching refusal(s) -- a queued prompt rides past a constraint "
        "the enqueue side enforces."
    )


def test_the_snapshot_carries_no_unmapped_constraint(tmp_path):
    """The reverse drift: a snapshot key with no refusal behind it."""
    snap = _base_snapshot(tmp_path)
    extra = set(snap) - set(_TARGET_CONTAINMENT_REFUSALS.values()) - _SNAPSHOT_NON_CONSTRAINT_KEYS
    assert not extra, (
        f"containment_snapshot key(s) with no authorize_target refusal behind them: "
        f"{sorted(extra)}. Map each to its refusal, or add it to "
        "_SNAPSHOT_NON_CONSTRAINT_KEYS if it is comparison detail rather than a constraint."
    )
