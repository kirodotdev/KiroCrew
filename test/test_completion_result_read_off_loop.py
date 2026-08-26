"""Plan-completion stage-result reads must not run on the gateway event loop.

When ``_stage_loop`` finishes every stage it builds one final summary message,
and it does that by re-reading each captured stage-result file off disk. That is
a separate lifecycle boundary from the per-stage context build: it happens once,
after the loop, and the number of files it reads is the whole plan's stage count.

Only the filesystem work belongs off-loop. The paths come from
``tracker._stage_results``, which the loop mutates via ``record_stage_result``,
so they are snapshotted on the loop thread and only immutable data crosses into
the worker; the summary text, the slot append, and the broadcast stay on the loop.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import _ChatSlot


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    """Stage results are written under ``config_dir()`` — keep them per-test.

    ``chat_orchestrator`` imports ``config_dir`` into its own namespace, so
    patching only ``state`` would leave results writing to the live data home
    and let parallel workers race on the shared slot key.
    """
    for module in ("state", "chat", "chat_orchestrator"):
        monkeypatch.setattr(f"kiro_crew.dashboard.{module}.config_dir", lambda: tmp_path)


def _make_state():
    state = MagicMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.subagents = MagicMock()
    state.subagents.running_agents_for = MagicMock(return_value=[])
    return state


def _make_slot(titles):
    slot = _ChatSlot("completion-read-slot", mode="orchestrator")
    slot._auto_run = False
    # `_plan_stage_count` is derived from the titles, not settable.
    slot._stage_titles = list(titles)
    slot._plan_goal = "Test goal"
    slot._orch_tracker = None
    return slot


def _stage_texts(monkeypatch, texts):
    """Make each stage's captured result file hold ``texts[stage_num - 1]``.

    The real ``_capture_stage_result`` harvests assistant messages appended by
    ``_run_chat``, which is mocked here; giving the mock a per-stage body is what
    puts distinguishable content on disk for the completion read to find.
    """
    stage_box = {"n": 0}

    async def _mock_run_chat(state, slot, message, **kwargs):
        idx = stage_box["n"]
        stage_box["n"] += 1
        if idx < len(texts):
            slot.append("assistant", texts[idx], "msg msg-a")

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)


def _completion_message(slot):
    """The single '✅ All N stages complete.' summary the loop emits."""
    matches = [
        m.get("content", "")
        for m in slot.messages
        if m.get("content", "").startswith("✅ All ")
    ]
    assert len(matches) == 1, f"expected exactly one completion summary, got {len(matches)}"
    return matches[0]


async def _run_plan(monkeypatch, titles, texts):
    from kiro_crew.dashboard.chat import _stage_loop

    state = _make_state()
    slot = _make_slot(titles)
    _stage_texts(monkeypatch, texts)
    await _stage_loop(state, slot, auto_run=True)
    return slot


def _record_reads(monkeypatch, seen_threads, paths_read):
    """Wrap the read the completion branch actually performs.

    ``chat_orchestrator`` binds ``safe_read_file`` at import, so replacing that
    module attribute intercepts the production call site itself. The wrapper
    delegates to the real implementation, so the recorded thread is the thread
    that genuinely opened and read the file — not merely the thread that reached
    a call site.
    """
    from kiro_crew import hooks

    def recording(path):
        seen_threads.append(threading.get_ident())
        paths_read.append(path)
        return hooks.safe_read_file(path)

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.safe_read_file", recording)


@pytest.mark.asyncio
async def test_completion_result_read_runs_off_the_loop_thread(monkeypatch):
    """The plan-completion result reads execute on some thread other than the loop's."""
    seen_threads: list[int] = []
    paths_read: list[str] = []
    _record_reads(monkeypatch, seen_threads, paths_read)

    slot = await _run_plan(
        monkeypatch,
        ["First", "Second", "Third"],
        ["alpha done", "beta done", "gamma done"],
    )

    assert paths_read, (
        "no stage result was read at plan completion -- this test no longer "
        "exercises the completion read and would pass vacuously")
    assert threading.get_ident() not in seen_threads, (
        "a completed stage result was read on the event-loop thread; the "
        "filesystem work must be handed to asyncio.to_thread")
    # The summary really is built from what was read off disk, so the assertion
    # above covers the read that produces the user-visible text.
    assert "alpha done" in _completion_message(slot)


@pytest.mark.asyncio
async def test_completion_reads_every_captured_stage_result(monkeypatch):
    """One read per captured stage — the cost the offload is there to move."""
    seen_threads: list[int] = []
    paths_read: list[str] = []
    _record_reads(monkeypatch, seen_threads, paths_read)

    await _run_plan(
        monkeypatch,
        ["First", "Second", "Third"],
        ["alpha done", "beta done", "gamma done"],
    )

    assert len(paths_read) == 3
    assert all("stage_" in p for p in paths_read)
    assert threading.get_ident() not in seen_threads


@pytest.mark.asyncio
async def test_completion_summary_lists_every_stage_in_order(monkeypatch):
    """Preservation: ordering, titles, and one line per stage are unchanged."""
    slot = await _run_plan(
        monkeypatch,
        ["First", "Second", "Third"],
        ["alpha done", "beta done", "gamma done"],
    )

    lines = _completion_message(slot).splitlines()
    assert lines[0] == "✅ All 3 stages complete."
    assert lines[1:] == [
        "  Stage 1: First — alpha done",
        "  Stage 2: Second — beta done",
        "  Stage 3: Third — gamma done",
    ]


@pytest.mark.asyncio
async def test_completion_summary_skips_stage_separator_lines(monkeypatch):
    """Preservation: the excerpt is the first non-empty, non-separator line."""
    slot = await _run_plan(
        monkeypatch,
        ["First"],
        ["\n\n───── Stage 1: First ─────\n\nreal first line\nsecond line"],
    )

    assert _completion_message(slot).splitlines()[1] == "  Stage 1: First — real first line"


@pytest.mark.asyncio
async def test_completion_summary_truncates_the_excerpt_at_120_chars(monkeypatch):
    """Preservation: the 120-char excerpt cap is unchanged."""
    slot = await _run_plan(monkeypatch, ["First"], ["x" * 300])

    assert _completion_message(slot).splitlines()[1] == "  Stage 1: First — " + "x" * 120


@pytest.mark.asyncio
async def test_completion_summary_falls_back_to_done_for_blank_result(monkeypatch):
    """Preservation: an empty result file yields '— done', not a crash."""
    slot = await _run_plan(monkeypatch, ["First"], [""])

    assert _completion_message(slot).splitlines()[1] == "  Stage 1: First — done"


@pytest.mark.asyncio
async def test_completion_summary_survives_a_deleted_result_file(monkeypatch, tmp_path):
    """Preservation: an unreadable path degrades to '— done' and does not raise.

    The read error must stay contained per stage: stage 2 still gets its excerpt.
    """
    from kiro_crew.dashboard.chat import _stage_loop

    state = _make_state()
    slot = _make_slot(["First", "Second"])
    _stage_texts(monkeypatch, ["alpha done", "beta done"])

    real_capture = None

    def _capture_then_delete_stage_1(s, stage_num):
        path = real_capture(s, stage_num)
        if stage_num == 1:
            (tmp_path / "sessions" / s.key / "stage_1_result.md").unlink()
        return path

    from kiro_crew.dashboard import chat_orchestrator

    real_capture = chat_orchestrator._capture_stage_result
    monkeypatch.setattr(chat_orchestrator, "_capture_stage_result", _capture_then_delete_stage_1)

    await _stage_loop(state, slot, auto_run=True)

    assert _completion_message(slot).splitlines()[1:] == [
        "  Stage 1: First — done",
        "  Stage 2: Second — beta done",
    ]


@pytest.mark.asyncio
async def test_completion_summary_redacts_credentials_from_the_excerpt(monkeypatch):
    """Preservation: redaction still runs on the loop, after the reads return."""
    slot = await _run_plan(monkeypatch, ["First"], ["key AKIAIOSFODNN7EXAMPLE here"])

    summary = _completion_message(slot)
    assert "AKIAIOSFODNN7EXAMPLE" not in summary
    assert "Stage 1: First" in summary


@pytest.mark.asyncio
async def test_no_worker_hop_when_no_stage_results_were_captured(monkeypatch):
    """A plan with nothing on disk must not pay for an empty thread round-trip."""
    from kiro_crew.dashboard import chat_orchestrator
    from kiro_crew.dashboard.chat import _stage_loop

    def _capture_fails(s, stage_num):
        raise OSError("disk full")

    monkeypatch.setattr(chat_orchestrator, "_capture_stage_result", _capture_fails)

    hops: list[object] = []
    real_to_thread = asyncio.to_thread

    async def counting_to_thread(func, /, *args, **kwargs):
        hops.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", counting_to_thread)

    state = _make_state()
    slot = _make_slot(["First", "Second"])
    _stage_texts(monkeypatch, ["alpha done", "beta done"])

    await _stage_loop(state, slot, auto_run=True)

    assert hops == []
    assert _completion_message(slot).splitlines() == [
        "✅ All 2 stages complete.",
        "  Stage 1: First — done",
        "  Stage 2: Second — done",
    ]


@pytest.mark.asyncio
async def test_completion_summary_is_emitted_once_with_auto_run_cleared(monkeypatch):
    """Preservation: terminal state around the reads is unchanged."""
    slot = await _run_plan(monkeypatch, ["First", "Second"], ["alpha done", "beta done"])

    assert slot._auto_run is False
    assert _completion_message(slot).startswith("✅ All 2 stages complete.")
