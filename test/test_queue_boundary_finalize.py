"""The queue turn boundary finalizes the predecessor's assistant bubble.

The end-of-turn flush suppresses the ``chat_segment`` broadcast
(``broadcast=False``), deferring the client-side streaming->assistant finalize
to the ``chat_done`` that ``_finish_queue_cycle`` emits. But when the
tail-drain starts a SUCCESSOR turn from the queue -- or ``_run_pending_synthesis``
dispatches a synthesis turn -- ``_finish_queue_cycle`` never runs for that
boundary. The flush's ``slot.append`` does emit a
``chat_message{role:assistant}`` frame whose reducer branch also finalizes, but
that frame is CONDITIONAL (suppressed while an HTTP SSE reader drains the slot,
absent when the final segment is empty because the text was already flushed at
a tool boundary, droppable by the client's mid-keyed redelivery guard) -- so a
client that misses it keeps its ``streaming`` row open, the successor's chunks
append into it, two turns render as one bubble, and a line-final
``[OPTIONS: ...]`` marker in the first turn loses its end-of-line anchor and
degrades to literal prose.

These tests pin the fix -- both successor-dispatch boundaries broadcast the
unconditional, idempotent ``chat_segment`` finalize once a successor is certain
to dispatch -- and its designed non-fires: a boundary where no successor
dispatches (empty queue, dropped entry, synthesis not eligible) keeps
``_finish_queue_cycle``'s ``chat_done`` as the sole finalizer, so there is no
double finalize on the ordinary single-turn path.

The full-turn test also confirms the persistence side: each turn lands as its
own assistant row in ``slot.messages``, so a session reload renders the two
turns correctly even without the live finalize frame -- the defect is
live-render only.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import SUBAGENT_COMPLETION_KIND
from kiro_crew.providers.base import LLMEvent


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """Run in the shipped (enabled) session-control state without reading config."""
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _inline_audit(monkeypatch):
    """Route SEL writes inline to a mock: no executor thread outlives the test."""
    fake = MagicMock()
    monkeypatch.setattr(sc, "sel", lambda: fake)
    monkeypatch.setattr(sc, "_sel_off_loop", lambda write, what: write())
    return fake


@pytest.fixture(autouse=True)
def _no_cycle_background_tasks(monkeypatch):
    """Neutralize ``_finish_queue_cycle``'s detached tasks (title refresh,
    session summary): both hop to executor threads (config load, transcript
    flush) that would outlive the test and can re-create the per-test dir
    after teardown."""
    monkeypatch.setattr(cr, "maybe_refresh_title", AsyncMock())
    monkeypatch.setattr(cr, "generate_session_summary", AsyncMock())


def _busy(slot):
    """``running`` is derived (``task is not None and not task.done()``)."""
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


async def _never_runs(state, slot, prompt):  # pragma: no cover - queued, not run
    raise AssertionError("a queued prompt must not start a turn at enqueue")


def _recorded_state(tmp_path):
    """A ``_make_state`` state whose WS broadcasts land in an ordered list."""
    state = _make_state(tmp_path)
    state.subagents = None
    frames: list[tuple[str, object]] = []
    state.broadcast_ws = MagicMock(side_effect=lambda t, d: frames.append((t, d)))
    return state, frames


def _stub_dispatch(monkeypatch, frames):
    """Stub the successor dispatch, recording WHEN it happens relative to frames."""

    async def _stub_run_chat(_state, _slot, _prompt, **_kwargs):
        return None

    def _fake_spawn(_state, _slot, coro):
        coro.close()
        frames.append(("spawned", None))
        task = MagicMock()
        task.done.return_value = True
        return task

    monkeypatch.setattr(cr, "_run_chat", _stub_run_chat)
    monkeypatch.setattr(cr, "spawn_guarded_turn", _fake_spawn)


# ── The boundary fires the finalize, before the successor ───────────────────


@pytest.mark.asyncio
async def test_queued_user_message_boundary_emits_finalize_before_dispatch(tmp_path, monkeypatch):
    """A queued USER message: the boundary broadcasts ``chat_segment`` for the
    slot, and it precedes the successor's dispatch (so it precedes the
    successor's first chunk, which can only be emitted by the spawned turn)."""
    state, frames = _recorded_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot.enqueue_or_run_prompt("queued follow-up", _never_runs, state)
    slot.task = None  # the turn ended; the tail-drain runs
    _stub_dispatch(monkeypatch, frames)

    started = await cr._start_next_queued_turn(state, slot)

    assert started is True
    kinds = [t for t, _ in frames]
    assert "chat_segment" in kinds
    seg = kinds.index("chat_segment")
    assert frames[seg][1] == {"slot": slot.key}
    assert "spawned" in kinds
    assert seg < kinds.index("spawned")


@pytest.mark.asyncio
async def test_kind_tagged_continuation_boundary_emits_finalize(tmp_path, monkeypatch):
    """A continuation-card (``kind``-tagged) entry -- the structural successor
    every recovery/continuation producer enqueues -- gets the same finalize."""
    state, frames = _recorded_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot.queue_append("[Subagent completion event] agent finished", kind=SUBAGENT_COMPLETION_KIND)
    _stub_dispatch(monkeypatch, frames)

    started = await cr._start_next_queued_turn(state, slot)

    assert started is True
    kinds = [t for t, _ in frames]
    assert "chat_segment" in kinds
    assert kinds.index("chat_segment") < kinds.index("spawned")


@pytest.mark.asyncio
async def test_synthesis_dispatch_emits_finalize_before_its_row(tmp_path, monkeypatch):
    """``_run_pending_synthesis`` is the other successor-dispatch boundary with
    no ``chat_done``: the synthesis turn's finalize fires once eligibility is
    settled, before the synthesis ``inject`` row is appended."""
    state, frames = _recorded_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    slot._pending_synthesis = True
    state.subagents = MagicMock(running_agents_for=MagicMock(return_value=[]))
    state._slots[slot.key] = slot

    def _fake_spawn(_state, _slot, coro):
        coro.close()
        frames.append(("spawned", None))

        async def _done():
            return None

        return asyncio.ensure_future(_done())

    monkeypatch.setattr(cr, "spawn_guarded_turn", _fake_spawn)

    await cr._run_pending_synthesis(state, slot)

    kinds = [t for t, _ in frames]
    assert "chat_segment" in kinds
    seg = kinds.index("chat_segment")
    assert frames[seg][1] == {"slot": slot.key}
    assert seg < kinds.index("spawned")
    # The finalize precedes the synthesis row's own broadcast-visible effects:
    # the inject row lands in the transcript after the frame went out.
    inject_rows = [m for m in slot.messages if m.get("role") == "inject"]
    assert inject_rows and inject_rows[-1]["meta"]["injectKind"] == "synthesis"


@pytest.mark.asyncio
async def test_ineligible_synthesis_emits_no_finalize(tmp_path, monkeypatch):
    """Synthesis NOT pending -> ``_run_pending_synthesis`` degrades to
    ``_finish_queue_cycle``: no ``chat_segment``, one ``chat_done``."""
    state, frames = _recorded_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    slot._pending_synthesis = False
    state._slots[slot.key] = slot

    await cr._run_pending_synthesis(state, slot)
    await asyncio.sleep(0)

    kinds = [t for t, _ in frames]
    assert "chat_segment" not in kinds
    assert kinds.count("chat_done") == 1


# ── The designed non-fires: no successor, no finalize ───────────────────────


@pytest.mark.asyncio
async def test_empty_queue_boundary_emits_no_finalize(tmp_path, monkeypatch):
    """Queue empty -> no successor dispatches -> no ``chat_segment``; the
    ordinary single-turn path keeps ``_finish_queue_cycle``'s ``chat_done`` as
    the sole finalizer (no double finalize)."""
    state, frames = _recorded_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True  # keep the cycle off the real auto-title path

    started = await cr._start_next_queued_turn(state, slot)
    assert started is False

    cr._finish_queue_cycle(state, slot)
    await asyncio.sleep(0)

    kinds = [t for t, _ in frames]
    assert "chat_segment" not in kinds
    assert kinds.count("chat_done") == 1


@pytest.mark.asyncio
async def test_dropped_only_entry_emits_no_finalize(tmp_path, monkeypatch):
    """The drain dropping the only entry (admission re-validation) is a
    boundary where no successor dispatches: the finalize must not fire there
    either -- ``_finish_queue_cycle`` still owns that path's ``chat_done``."""
    state, frames = _recorded_state(tmp_path)
    slot = _busy(state.get_or_create_slot("chat-1"))
    slot.enqueue_or_run_prompt("admitted while unlinked", _never_runs, state)
    slot.task = None
    slot.linked_session_key = "C0LINKED|1700000000.000100"  # constraint newly holds

    def _no_spawn(_state, _slot, coro):  # pragma: no cover - must not be reached
        coro.close()
        raise AssertionError("no successor may dispatch for a dropped entry")

    monkeypatch.setattr(cr, "spawn_guarded_turn", _no_spawn)

    started = await cr._start_next_queued_turn(state, slot)

    assert started is False
    assert slot._queue == []
    assert "chat_segment" not in [t for t, _ in frames]


# ── Full-turn frame sequence: finalize lands between the two turns' chunks ──


async def _async_iter(items):
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_two_turn_frame_sequence_has_finalize_between_chunks(tmp_path):
    """Through the real ``_run_chat`` tail: with a queued user message, the
    client sees ``chat_segment`` after turn N's last chunk and before turn
    N+1's first chunk, and exactly one ``chat_done`` (after the final turn).
    Records BOTH egress channels -- ``broadcast_ws`` AND the ``_broadcast``
    path that carries ``chat_message`` frames -- so the boundary's full frame
    pair is pinned: the conditional ``chat_message{role:assistant}`` from the
    flush AND the unconditional ``chat_segment``, both before the successor's
    first chunk. Also confirms the reload prediction: each turn persists as
    its OWN assistant row, so the merged bubble is a live-render defect only."""
    state = _make_state(tmp_path)
    state.subagents = None
    frames: list[tuple[str, object]] = []
    state.broadcast_ws = MagicMock(side_effect=lambda t, d: frames.append((t, d)))
    real_broadcast = state._broadcast

    def _recording_broadcast(note):
        # `_broadcast` fans out to SSE queues + WS clients; record the frame
        # type on the same ordered list so cross-channel ordering is asserted.
        frames.append((f"bc:{note.get('_type')}", dict(note)))
        return real_broadcast(note)

    state._broadcast = _recording_broadcast  # type: ignore[method-assign]
    state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.record_failure = AsyncMock()
    state.is_yolo_active = MagicMock(return_value=False)
    slot = state.get_or_create_slot("chat-e2e")
    slot._titled = True  # keep the end-of-turn cycle off the real auto-title path
    slot.append("user", "first prompt", "msg msg-u")
    slot.queue_append("queued follow-up")

    client = state.sessions.get_or_create.return_value[0]
    client.shutdown = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=0.0)
    client._client = client
    client.last_prompt_stats = None
    turn1 = [
        LLMEvent(kind=EVENT_TEXT_CHUNK, text="Pick a path.\n"),
        LLMEvent(kind=EVENT_TEXT_CHUNK, text="[OPTIONS: Alpha | Bravo]"),
        LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    turn2 = [
        LLMEvent(kind=EVENT_TEXT_CHUNK, text="Second turn reply."),
        LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    calls = {"n": 0}

    def _stream(*_a, **_kw):
        calls["n"] += 1
        return _async_iter(turn1 if calls["n"] == 1 else turn2)

    client.stream = MagicMock(side_effect=_stream)
    client.stream_command = MagicMock(side_effect=_stream)

    await cr._run_chat(state, slot, "first prompt")
    successor = slot.task
    assert successor is not None, "the tail-drain must have dispatched the queued turn"
    await asyncio.wait_for(successor, timeout=10)

    kinds = [t for t, _ in frames]
    assert kinds.count("chat_segment") == 1, kinds
    seg = kinds.index("chat_segment")
    before = "".join(
        d.get("content", "") for t, d in frames[:seg] if t == "chat_chunk" and isinstance(d, dict)
    )
    after = "".join(
        d.get("content", "")
        for t, d in frames[seg + 1 :]
        if t == "chat_chunk" and isinstance(d, dict)
    )
    # Turn N's text (OPTIONS marker included) fully precedes the finalize;
    # turn N+1's text fully follows it -- nothing glues onto the open bubble.
    assert "[OPTIONS: Alpha | Bravo]" in before
    assert "Second turn reply." not in before
    assert "Second turn reply." in after

    # The boundary's frame pair: the flush's conditional chat_message
    # (role=assistant, turn N's text) precedes the unconditional chat_segment,
    # and both precede the successor's chunks.
    t1_assistant = [
        i
        for i, (t, d) in enumerate(frames)
        if t == "bc:chat_message"
        and isinstance(d, dict)
        and d.get("role") == "assistant"
        and "[OPTIONS: Alpha | Bravo]" in str(d.get("content", ""))
    ]
    assert t1_assistant and t1_assistant[0] < seg

    # Exactly one chat_done for the two-turn cycle, and it follows the finalize:
    # the boundary did not double-finalize the ordinary end-of-cycle path.
    assert kinds.count("chat_done") == 1
    assert kinds.index("chat_done") > seg

    # Reload correctness (the reporter's prediction, confirmed): the backend
    # persisted a SEPARATE assistant row per turn, first one ending line-final
    # on its [OPTIONS: ...] marker.
    assistants = [m for m in slot.messages if m.get("role") == "assistant"]
    assert len(assistants) == 2
    assert assistants[0]["content"].endswith("[OPTIONS: Alpha | Bravo]")
    assert assistants[1]["content"] == "Second turn reply."
