"""Previous-stage-result reads must not run on the gateway event loop.

``_stage_loop`` builds a fresh context message before every stage, and
``_build_stage_context`` inlines the previous stages' results by reading each one
off disk. Those reads are synchronous, and the count grows with the plan: stage N
re-reads all N-1 earlier result files, so a long plan pays more at each boundary,
not less.

Only the filesystem work belongs off-loop. The path list comes from
``tracker._stage_results``, which the loop mutates via ``record_stage_result``,
so the paths are snapshotted on the loop thread and only immutable data crosses
into the worker.
"""

from __future__ import annotations

import builtins
import inspect
import pathlib
import threading

import pytest


async def _context(slot, tracker, stage_idx):
    """Drive the real production context builder, sync or async.

    Tolerant of both shapes so the thread assertion is what fails on an unfixed
    tree; a bare ``await`` against the synchronous version would raise "can't be
    used in 'await' expression", which proves only that the symbol changed.
    """
    from kiro_crew.dashboard.chat_orchestrator import _build_stage_context

    result = _build_stage_context(slot, tracker, stage_idx)
    if inspect.isawaitable(result):
        result = await result
    return result


def _fixture(tmp_path, monkeypatch, *, content="stage one output"):
    """A slot + tracker where stage 1 has a result file on disk."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_orchestrator.is_sensitive_path", lambda p: False
    )
    from kiro_crew.context_management import OrchestrationTracker
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("prev-result-slot", mode="orchestrator")
    # `_plan_stage_count` is derived from the titles, not settable.
    slot._stage_titles = ["First", "Second"]

    result_dir = tmp_path / "sessions" / "prev-result-slot"
    result_dir.mkdir(parents=True)
    result_file = result_dir / "stage_1_result.md"
    result_file.write_text(content, encoding="utf-8")

    tracker = OrchestrationTracker()
    tracker.record_stage_result(1, str(result_file))
    return slot, tracker, result_file


@pytest.mark.asyncio
async def test_previous_result_read_runs_off_the_loop_thread(tmp_path, monkeypatch):
    """The small-file read path executes on some thread other than the loop's."""
    slot, tracker, result_file = _fixture(tmp_path, monkeypatch)

    seen_threads: list[int] = []
    real_read = pathlib.Path.read_bytes

    def recording_read(self, *args, **kwargs):
        # Scoped to this stage's result file: unrelated reads run on the loop
        # legitimately and must not decide this assertion.
        if self == result_file:
            seen_threads.append(threading.get_ident())
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_bytes", recording_read)

    await _context(slot, tracker, 1)

    assert seen_threads, (
        "the stage 1 result was never read -- this test no longer exercises the "
        "previous-result read and would pass vacuously")
    assert threading.get_ident() not in seen_threads, (
        "the previous stage result was read on the event-loop thread; the "
        "filesystem work must be handed to asyncio.to_thread")


@pytest.mark.asyncio
async def test_previous_result_stat_runs_off_the_loop_thread(tmp_path, monkeypatch):
    """``stat`` decides the truncation branch and is a syscall of its own."""
    slot, tracker, result_file = _fixture(tmp_path, monkeypatch)

    seen_threads: list[int] = []
    real_stat = pathlib.Path.stat

    def recording_stat(self, *args, **kwargs):
        if self == result_file:
            seen_threads.append(threading.get_ident())
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", recording_stat)

    await _context(slot, tracker, 1)

    assert seen_threads, (
        "the stage 1 result was never stat'd -- this test no longer exercises "
        "the size probe and would pass vacuously")
    assert threading.get_ident() not in seen_threads, (
        "the previous stage result was stat'd on the event-loop thread; it must "
        "be handed to asyncio.to_thread with the read")


@pytest.mark.asyncio
async def test_truncated_read_runs_off_the_loop_thread(tmp_path, monkeypatch):
    """The large-file branch uses open/read/seek/read — also off-loop."""
    slot, tracker, result_file = _fixture(tmp_path, monkeypatch, content="z" * 5000)

    seen_threads: list[int] = []
    # The truncation branch uses the BUILTIN open, not Path.open, so that is what
    # has to be instrumented — patching Path.open records nothing and the test
    # would report "never opened" instead of the thread it ran on.
    real_open = builtins.open

    def recording_open(file, *args, **kwargs):
        if str(file) == str(result_file):
            seen_threads.append(threading.get_ident())
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", recording_open)

    ctx = await _context(slot, tracker, 1)

    assert seen_threads, (
        "the truncation branch never opened the file -- this test no longer "
        "exercises the large-result path and would pass vacuously")
    assert threading.get_ident() not in seen_threads, (
        "the truncated read ran on the event-loop thread")
    assert "...[truncated]..." in ctx


@pytest.mark.asyncio
async def test_tracker_state_is_read_on_the_loop_thread(tmp_path, monkeypatch):
    """The offload stops at the filesystem — live tracker state is not shared.

    ``tracker._stage_results`` is mutated on the loop by ``record_stage_result``
    as each stage finishes, so the path list is snapshotted before the hop and
    the worker never reaches the tracker.
    """
    slot, tracker, _ = _fixture(tmp_path, monkeypatch)

    seen_threads: list[int] = []
    real_results = tracker._stage_results

    class RecordingDict(dict):
        def get(self, *args, **kwargs):
            seen_threads.append(threading.get_ident())
            return super().get(*args, **kwargs)

        def items(self):
            seen_threads.append(threading.get_ident())
            return super().items()

    tracker._stage_results = RecordingDict(real_results)

    await _context(slot, tracker, 1)

    assert seen_threads, (
        "tracker._stage_results was never consulted -- this test no longer "
        "exercises the path selection and would pass vacuously")
    assert seen_threads == [threading.get_ident()] * len(seen_threads), (
        "tracker state was read off the event-loop thread; only the filesystem "
        "read may cross into a worker")


@pytest.mark.asyncio
async def test_context_preserves_content_and_missing_file_semantics(tmp_path, monkeypatch):
    """The offload changes scheduling only — every output contract holds."""
    slot, tracker, result_file = _fixture(tmp_path, monkeypatch, content="stage one body")

    ctx = await _context(slot, tracker, 1)
    assert "stage one body" in ctx
    assert f"Full result: `{result_file}`" in ctx

    # A recorded path that no longer exists degrades to the path-only form.
    tracker.record_stage_result(1, str(tmp_path / "sessions" / "gone" / "stage_1_result.md"))
    ctx_missing = await _context(slot, tracker, 1)
    assert "Full result:" in ctx_missing
    assert "stage one body" not in ctx_missing


@pytest.mark.asyncio
async def test_sensitive_path_is_not_read(tmp_path, monkeypatch):
    """The sensitive-path refusal still short-circuits before any read."""
    slot, tracker, result_file = _fixture(tmp_path, monkeypatch, content="secret body")
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_orchestrator.is_sensitive_path", lambda p: True
    )

    reads: list[str] = []
    real_read = pathlib.Path.read_bytes

    def recording_read(self, *args, **kwargs):
        reads.append(str(self))
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_bytes", recording_read)

    ctx = await _context(slot, tracker, 1)

    assert str(result_file) not in reads, "a sensitive path was read"
    assert "secret body" not in ctx
    assert "Full result:" in ctx
