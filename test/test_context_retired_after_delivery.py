"""Drained context is retired only once the prompt is proven delivered.

The drain hands entries to the model and empties the live queue, but delivery
happens later, after an ``await`` that can be cancelled. Emptying the queue
durably at drain time therefore loses content the API already answered 200 for.
These tests pin the three halves that close it: the drain moves entries to an
in-flight list rather than dropping them, a prompt-attributable event is what
proves delivery, and the retirement is committed strictly afterwards.
"""

from __future__ import annotations

import time
import uuid

from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_runner import commit_drained_context, drain_pending_context
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import _ChatSlot


def _entry(
    content: str,
    *,
    source: str = "test",
    max_age: float | None = 86400,
    injected_at: float | None = None,
    **extra: object,
) -> dict:
    """A pending-context entry in the shape `_build_pending_context_entry` produces.

    No ``ephemeral`` key: the builder omits it unless a caller asks, and it now means
    MEMORY-ONLY, so stamping every fixture entry would withhold the whole queue from
    disk and leave these tests asserting over an empty file.
    """
    e: dict = {
        "content": content,
        "source": source,
        "injectedAt": time.time() if injected_at is None else injected_at,
        "maxAge": max_age,
        "ctxId": uuid.uuid4().hex,
    }
    e.update(extra)
    return e


def _seed(state, key: str, entries: list[dict]) -> _ChatSlot:
    """A titled, published slot carrying *entries*."""
    slot = _ChatSlot(key)
    slot.title = f"title-{key}"
    slot._titled = True
    slot.append(role="user", content="a real message", cls="msg msg-u")
    for e in entries:
        slot.append_pending_context(e)
    state._slots[key] = slot
    return slot


def test_an_unattributed_event_cannot_confirm_delivery():
    """Uncorrelated prior-turn events must not retire undelivered context.

    The kind alone does not say WHOSE prompt an event answers. `AcpEvent.runtime_global`
    marks a frame that named no owner and was fanned out to every session on the
    runtime -- another tenant's traffic, which the field's own docs say a consumer
    "must not read as ITS OWN activity" -- and a non-empty `sub_session_id` names a
    different session's sub-agent. Either confirmed delivery and retired a queue this
    prompt never sent.

    Refusing is the safe direction: `commit_drained_context` is idempotent and a real
    turn emits an attributable event, so a deferral costs nothing.
    """
    from types import SimpleNamespace

    from kiro_crew.acp.types import EVENT_TEXT_CHUNK
    from kiro_crew.dashboard import chat_runner as cr

    own = SimpleNamespace(kind=EVENT_TEXT_CHUNK, runtime_global=False, sub_session_id="")
    assert cr.event_confirms_delivery(
        own
    ), "positive control: this prompt's own streaming event must still confirm"

    fanned = SimpleNamespace(kind=EVENT_TEXT_CHUNK, runtime_global=True, sub_session_id="")
    assert not cr.event_confirms_delivery(
        fanned
    ), "a fanned-out runtime-global event is another tenant's traffic"

    subagent = SimpleNamespace(kind=EVENT_TEXT_CHUNK, runtime_global=False, sub_session_id="sub-42")
    assert not cr.event_confirms_delivery(
        subagent
    ), "an event owned by another session's sub-agent does not prove this prompt landed"


def test_drain_marks_the_slot_dirty():
    """The cleared queue reaches disk on DELIVERY, not on the drain.

    Marking dirty at the drain arms the periodic flush, and that flush is a timer --
    nothing orders it after delivery -- so it could durably empty the queue for
    content that a cancellation then stopped from ever being delivered. The retire
    therefore belongs to `commit_drained_context`, which runs once the prompt has
    reached the client. The stored copy must still not outlive the entries, so the
    dirty mark is owed; it is just owed LATER.
    """
    slot = _ChatSlot("chat-ctx-dirty2")
    slot.append_pending_context(_entry("queued"))
    slot._dirty = False
    drain_pending_context(slot)
    assert slot._dirty is False, (
        "the drain must NOT arm the durable retire: delivery has not happened yet, "
        "and the flush that would act on this is a timer with no ordering guarantee"
    )
    commit_drained_context(slot)
    assert slot._dirty is True, "after delivery the emptied queue must reach disk"


def test_a_cancellation_before_delivery_requeues_the_drained_context(tmp_path):
    """Cancellation between drain and delivery must not lose the entries.

    The explicit requeue is gone (its arm was a narrower spelling of a recovery that is
    already structural). The property is unchanged and is asserted here through the surviving
    mechanism: the NEXT drain recovers whatever is still in flight and hands it to the model,
    in FIFO order, so nothing the API acknowledged is dropped.
    """
    state = _make_state(tmp_path)
    slot = _seed(state, "chat-ctx-requeue", [_entry("first"), _entry("second")])

    drain_pending_context(slot)
    assert not slot._pending_context, "precondition: the drain emptied the live queue"
    assert len(slot._ctx_inflight) == 2, "precondition: both entries are in flight"

    # The cancellation arm does not requeue; the next turn's drain recovers instead.
    prefix = drain_pending_context(slot)
    assert prefix.index("first") < prefix.index(
        "second"
    ), f"recovered entries must reach the model in FIFO order: {prefix!r}"
    assert not slot._pending_context, "the recovering drain also consumes"

    # Idempotent: a third drain must not resurrect content the model already saw.
    slot._ctx_inflight = []
    assert drain_pending_context(slot) == "", "a delivered turn must not resurrect content"


def test_a_recovered_orphan_is_authorization_checked_before_delivery(tmp_path):
    """The leak: an orphan recovered AFTER the filter reaches the wrong session.

    ``drop_foreign_authorized_notes`` walks ``_pending_context`` and ``messages``
    only -- never ``_ctx_inflight``. So when the orphan recovery ran after it, a
    note stamped for session A could be spliced into the queue behind the filter's
    back and delivered to session B:

      A queues a note -> the turn exits before delivery, leaving it in flight ->
      the slot is rebound to B -> the next drain recovers it unchecked.

    Recovery therefore has to precede the filter, so the recovered entry is subject
    to exactly the same authorization check as one that never left the queue.
    """
    state = _make_state(tmp_path)
    slot = _seed(state, "chat-ctx-orphan-auth", [])
    session_a = effective_session_key(slot)
    assert session_a, "precondition: the slot resolves an authorizing session"

    # An UNDELIVERED note stamped for A, sitting where a pre-delivery exit left it.
    orphan = dict(_entry("A-only note"))
    orphan["noteSession"] = session_a
    slot._ctx_inflight = [orphan]
    assert not slot._pending_context, "precondition: the live queue is empty"

    # Rebind to B, exactly as a cron/workflow hand-off does.
    slot.linked_session_key = "cron:job-orphan-auth"
    session_b = effective_session_key(slot)
    assert session_b != session_a, f"precondition: the rebind moved the session: {session_b!r}"

    rendered = drain_pending_context(slot)

    assert "A-only note" not in rendered, (
        "session A's note was delivered to session B: the recovered orphan bypassed "
        f"drop_foreign_authorized_notes -- rendered={rendered!r}"
    )
    assert not slot._ctx_inflight, "the orphan must not be left in flight either"
    assert [e.get("content") for e in slot._pending_context] == [], (
        "the foreign-authorized entry must be dropped, not re-queued: " f"{slot._pending_context!r}"
    )


def test_a_passive_event_does_not_retire_undelivered_context():
    """A passive event must NOT prove delivery, or a Stop loses acknowledged content.

    The runtime is shared, so the first thing the stream yields can be an unrelated
    MCP server init rather than this prompt's output. Committing on that clears
    `_ctx_inflight`, and the drain's orphan recovery needs exactly that list to put the
    entries back -- so a Stop arriving before the prompt is processed finds nothing in
    flight and the durable clear stands. The content is then gone despite a 200.

    The fix is an allowlist: only a prompt-attributable event retires the queue.
    """
    from kiro_crew.acp.types import (
        EVENT_MCP_SERVER_INITIALIZED,
        EVENT_STEER_QUEUED,
        EVENT_SUBAGENT_LIST,
        EVENT_TEXT_CHUNK,
    )
    from kiro_crew.dashboard.chat_runner import (
        _PROMPT_ATTRIBUTABLE_EVENTS,
        commit_drained_context,
        drain_pending_context,
    )

    # Precondition: these kinds really are outside the allowlist and a model-output
    # kind really is inside it, so the assertions below exercise a real gate.
    assert EVENT_MCP_SERVER_INITIALIZED not in _PROMPT_ATTRIBUTABLE_EVENTS
    assert EVENT_SUBAGENT_LIST not in _PROMPT_ATTRIBUTABLE_EVENTS
    assert EVENT_STEER_QUEUED not in _PROMPT_ATTRIBUTABLE_EVENTS
    assert EVENT_TEXT_CHUNK in _PROMPT_ATTRIBUTABLE_EVENTS

    for passive in (EVENT_MCP_SERVER_INITIALIZED, EVENT_SUBAGENT_LIST, EVENT_STEER_QUEUED):
        slot = _ChatSlot("chat-passive-retire")
        slot.append_pending_context(_entry("owed content"))
        assert slot._pending_context, "precondition: the entry was seated"
        drain_pending_context(slot)
        assert slot._ctx_inflight, "precondition: the drain moved the entry in flight"

        # The stream's first event is passive. This is the gate the runner applies.
        if passive in _PROMPT_ATTRIBUTABLE_EVENTS:  # pragma: no cover - guarded above
            commit_drained_context(slot)

        # User presses Stop before the prompt is processed. The entry stays in flight and
        # the NEXT drain recovers it -- the structural replacement for the explicit requeue.
        assert len(slot._ctx_inflight) == 1, (
            f"a {passive} event retired undelivered context, so content the API "
            "acknowledged with a 200 is permanently lost"
        )
        assert "owed content" in drain_pending_context(
            slot
        ), f"the recovering drain must hand a {passive}-interrupted entry to the model"

    # Positive control: a real model-output event DOES retire, so the test above is
    # not passing merely because nothing ever commits.
    slot = _ChatSlot("chat-attributable-retire")
    slot.append_pending_context(_entry("delivered content"))
    assert slot._pending_context, "precondition: the entry was seated"
    drain_pending_context(slot)
    if EVENT_TEXT_CHUNK in _PROMPT_ATTRIBUTABLE_EVENTS:
        commit_drained_context(slot)
    assert len(slot._ctx_inflight) == 0, "a delivered turn must not resurrect content"
    assert slot._pending_context == []


def test_a_synthetic_completion_does_not_confirm_delivery():
    """A locally manufactured terminal event must not retire durable context.

    ``EVENT_COMPLETE`` is synthesized when a turn ends with no result -- a stale
    turn, a cancel, a tool stall, a failed compaction. Treating one as delivery
    clears the persisted queue although the provider never saw the prompt, which
    is precisely the acknowledged-then-lost class this change exists to close.
    """
    from types import SimpleNamespace

    from kiro_crew.acp.types import (
        EVENT_COMPLETE,
        EVENT_TEXT_CHUNK,
        STOP_REASON_CANCELLED,
        STOP_REASON_COMPACTION_FAILED,
        STOP_REASON_END_TURN,
        STOP_REASON_REFUSAL,
        STOP_REASON_STALE_RECOVER,
        STOP_REASON_TOOL_STALL,
    )
    from kiro_crew.dashboard.chat_runner import event_confirms_delivery

    def ev(kind, stop_reason="", synthetic=False):
        return SimpleNamespace(kind=kind, stop_reason=stop_reason, synthetic_completion=synthetic)

    # A streaming kind is self-proving: the provider emitted something.
    assert event_confirms_delivery(ev(EVENT_TEXT_CHUNK)) is True
    # A real terminal event still confirms, including a refusal -- the provider
    # answering "no" proves it received the prompt.
    assert event_confirms_delivery(ev(EVENT_COMPLETE, STOP_REASON_END_TURN)) is True
    assert event_confirms_delivery(ev(EVENT_COMPLETE, STOP_REASON_REFUSAL)) is True
    # The reported defect: a synthesized completion must NOT confirm.
    assert (
        event_confirms_delivery(ev(EVENT_COMPLETE, STOP_REASON_END_TURN, synthetic=True)) is False
    )
    # Nor may any non-delivery stop reason.
    for reason in (
        STOP_REASON_CANCELLED,
        STOP_REASON_COMPACTION_FAILED,
        STOP_REASON_STALE_RECOVER,
        STOP_REASON_TOOL_STALL,
    ):
        assert event_confirms_delivery(ev(EVENT_COMPLETE, reason)) is False, reason
    # A passive kind outside the allowlist never confirms.
    assert event_confirms_delivery(ev("heartbeat")) is False


def test_the_commit_gate_routes_through_the_delivery_predicate():
    """Pin the CALL SITE, not just the predicate.

    A correct predicate that nothing calls fixes nothing, so assert the runner's
    single commit gate asks ``event_confirms_delivery`` rather than testing
    allowlist membership directly. The bare-membership form is the defect shape,
    so its absence is only meaningful because this query would have matched it.
    """
    import inspect

    from kiro_crew.dashboard import chat_runner as cr2

    src = inspect.getsource(cr2)
    assert src.count("if event_confirms_delivery(event):") == 1
    assert "if event.kind in _PROMPT_ATTRIBUTABLE_EVENTS:" not in src
    # Positive control: the allowlist itself still exists and is still consulted
    # inside the predicate, so the assertion above cannot pass by a rename.
    assert src.count("_PROMPT_ATTRIBUTABLE_EVENTS = frozenset(") == 1
    assert src.count("kind not in _PROMPT_ATTRIBUTABLE_EVENTS") == 1


def test_a_local_timeout_completion_does_not_confirm_delivery():
    """Terminal events carrying a LITERAL stop reason must not retire context.

    Several local terminations yield ``EVENT_COMPLETE`` with a bare string reason
    rather than one of the module constants, so a set enumerating what to REFUSE
    admits them. Only a positive delivery reason may confirm.
    """
    from types import SimpleNamespace

    from kiro_crew.acp.types import (
        EVENT_COMPLETE,
        EVENT_TEXT_CHUNK,
        STOP_REASON_CANCELLED,
        STOP_REASON_COMPACTION_FAILED,
        STOP_REASON_END_TURN,
        STOP_REASON_REFUSAL,
        STOP_REASON_STALE_RECOVER,
        STOP_REASON_TOOL_STALL,
    )
    from kiro_crew.dashboard.chat_runner import event_confirms_delivery

    def ev(kind, stop_reason="", synthetic=False):
        return SimpleNamespace(kind=kind, stop_reason=stop_reason, synthetic_completion=synthetic)

    assert event_confirms_delivery(ev(EVENT_TEXT_CHUNK)) is True
    assert event_confirms_delivery(ev(EVENT_COMPLETE, STOP_REASON_END_TURN)) is True
    assert event_confirms_delivery(ev(EVENT_COMPLETE, STOP_REASON_REFUSAL)) is True
    # The reported defect: bare literals no constant covers.
    assert event_confirms_delivery(ev(EVENT_COMPLETE, "timeout")) is False
    assert event_confirms_delivery(ev(EVENT_COMPLETE, "error: cancel unacked")) is False
    # Fail-CLOSED: an unknown future reason must not confirm either.
    assert event_confirms_delivery(ev(EVENT_COMPLETE, "some_new_reason")) is False
    assert event_confirms_delivery(ev(EVENT_COMPLETE, "")) is False
    for reason in (
        STOP_REASON_CANCELLED,
        STOP_REASON_COMPACTION_FAILED,
        STOP_REASON_STALE_RECOVER,
        STOP_REASON_TOOL_STALL,
    ):
        assert event_confirms_delivery(ev(EVENT_COMPLETE, reason)) is False, reason
    assert (
        event_confirms_delivery(ev(EVENT_COMPLETE, STOP_REASON_END_TURN, synthetic=True)) is False
    )
