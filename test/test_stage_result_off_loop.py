"""The autopilot stage-result write must not run on the gateway event loop.

``_stage_loop`` is async and drives the whole autopilot run, but it persists each
stage's result with a synchronous ``mkdir`` + ``write_text``. Every other task on
the loop — chat streaming, WebSocket frames, cron dispatch — is stalled for the
duration of that filesystem work, once per stage boundary.

Only the filesystem mutation belongs off-loop. Reading ``slot.messages`` and
redacting stays on the loop thread: it is live mutable slot state, and handing it
to a worker would buy nothing while widening the change into cross-thread access.
These tests pin both halves of that boundary.
"""

from __future__ import annotations

import inspect
import os
import pathlib
import threading

import pytest


async def _capture(slot, stage_num):
    """Drive the real production capture, sync or async.

    Deliberately tolerant of both shapes so the thread assertions below are what
    fails on an unfixed tree. A bare ``await`` would raise "can't be used in
    'await' expression" against the synchronous version, which proves only that
    the symbol changed — not that the write was ever on the wrong thread.
    """
    from kiro_crew.dashboard.chat_orchestrator import _capture_stage_result

    result = _capture_stage_result(slot, stage_num)
    if inspect.isawaitable(result):
        result = await result
    return result


def _slot_with(*assistant_texts: str):
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("chat-1-stage")
    for i, text in enumerate(assistant_texts):
        slot.append("assistant", text, f"msg msg-a{i}", broadcast=False)
    return slot


@pytest.mark.asyncio
async def test_stage_result_write_runs_off_the_loop_thread(tmp_path, monkeypatch):
    """The bytes reach disk on some thread other than the loop's."""
    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.config_dir", lambda: tmp_path)
    slot = _slot_with("stage one output")

    seen_threads: list[int] = []
    real_write = pathlib.Path.write_text
    target_dir = tmp_path / "sessions" / "chat-1-stage"

    def recording_write(self, *args, **kwargs):
        # Scoped to the stage-result payload write (canonical or temp name,
        # so the assertion tracks the write wherever the implementation puts
        # the bytes) -- unrelated writes cannot decide this.
        if self.parent == target_dir and "stage_1_result" in self.name:
            seen_threads.append(threading.get_ident())
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", recording_write)

    await _capture(slot, 1)

    assert seen_threads, (
        "the stage result file was never written -- this test no longer "
        "exercises the write and would pass vacuously"
    )
    assert threading.get_ident() not in seen_threads, (
        "the stage result was written on the event-loop thread; the filesystem "
        "work must be handed to asyncio.to_thread"
    )


@pytest.mark.asyncio
async def test_stage_result_mkdir_runs_off_the_loop_thread(tmp_path, monkeypatch):
    """``mkdir`` is the other blocking syscall, and it is first.

    Offloading only the write would leave a directory creation — which on a cold
    or networked session directory is the slower of the two — still on the loop.
    """
    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.config_dir", lambda: tmp_path)
    slot = _slot_with("stage one output")

    seen_threads: list[int] = []
    real_mkdir = pathlib.Path.mkdir
    target = tmp_path / "sessions" / "chat-1-stage"

    def recording_mkdir(self, *args, **kwargs):
        # Scoped to the stage-result directory: unrelated mkdir traffic from
        # fixtures or lazily-created config dirs runs on the loop legitimately
        # and would make a global assertion fail for the wrong reason.
        if self == target:
            seen_threads.append(threading.get_ident())
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "mkdir", recording_mkdir)

    await _capture(slot, 1)

    assert seen_threads, (
        "the session directory was never created -- this test no longer "
        "exercises the stage-result mkdir and would pass vacuously"
    )
    assert threading.get_ident() not in seen_threads, (
        "the session directory was created on the event-loop thread; it must be "
        "handed to asyncio.to_thread with the write"
    )


@pytest.mark.asyncio
async def test_slot_messages_are_read_on_the_loop_thread(tmp_path, monkeypatch):
    """The offload stops at the filesystem — live slot state is not shared.

    ``slot.messages`` is mutable and owned by the loop. Moving the traversal into
    a worker would make an unrelated append during the capture a cross-thread
    read, so the boundary is asserted from the safe side too, not just the fast
    one.
    """
    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.config_dir", lambda: tmp_path)
    slot = _slot_with("stage one output")

    seen_threads: list[int] = []

    class RecordingList(list):
        def __reversed__(self):
            seen_threads.append(threading.get_ident())
            return super().__reversed__()

    slot.messages = RecordingList(slot.messages)

    await _capture(slot, 1)

    assert seen_threads, (
        "slot.messages was never traversed -- this test no longer exercises the "
        "extraction and would pass vacuously"
    )
    assert seen_threads == [threading.get_ident()] * len(seen_threads), (
        "slot.messages was traversed off the event-loop thread; only the "
        "filesystem write may cross into a worker"
    )


@pytest.mark.asyncio
async def test_capture_preserves_path_ordering_and_redaction(tmp_path, monkeypatch):
    """The offload changes scheduling only — every output contract holds."""
    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.config_dir", lambda: tmp_path)
    secret = "AKIAIOSFODNN7EXAMPLE"
    slot = _slot_with("first part", f"second part with {secret}")

    path = await _capture(slot, 3)

    assert path == str(tmp_path / "sessions" / "chat-1-stage" / "stage_3_result.md")
    written = pathlib.Path(path).read_text(encoding="utf-8")
    # Oldest first: the extraction walks backwards then reverses.
    assert written.index("first part") < written.index("second part")
    assert secret not in written, "stage result persisted an unredacted credential"


@pytest.mark.asyncio
async def test_stop_landing_during_the_offloaded_write_does_not_advance(tmp_path, monkeypatch):
    """A stop that lands while the worker is writing must halt the run.

    The offloaded write is a suspension point between the loop's last stop
    check and the next stage. The user cancel path sets ``tracker.stopped``
    (and ``slot._auto_run``), not ``slot._stopping`` -- and ``auto_run`` is a
    call-time snapshot -- so without a re-check after the await the loop
    resumes and executes the next stage against a revoked approval.
    """
    from unittest.mock import MagicMock

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.config_dir", lambda: tmp_path)
    from kiro_crew.dashboard.chat_orchestrator import _stage_loop
    from kiro_crew.dashboard.state import _ChatSlot

    state = MagicMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.subagents = MagicMock()
    state.subagents.running_agents_for = MagicMock(return_value=[])

    slot = _ChatSlot("stop-mid-write", mode="orchestrator")
    slot._stage_titles = ["A", "B"]
    slot._orch_tracker = None

    executed: list[int] = []

    async def _turn(s, sl, msg, **kw):
        executed.append(len(executed) + 1)
        sl.append("assistant", f"stage {len(executed)} output", "msg msg-a", broadcast=False)

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _turn)

    # The cancel lands while the stage-1 result is being written: the worker
    # is mid-write when the user's stop is processed on the loop thread.
    real_write = pathlib.Path.write_text

    def stopping_write(self, *args, **kwargs):
        if "stage_1_result" in self.name:
            slot._orch_tracker.stop()
            slot._auto_run = False
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", stopping_write)

    await _stage_loop(state, slot, auto_run=True)

    assert executed == [1], (
        "stage 2 executed after the user's stop landed during the stage-1 "
        "result write -- the loop must re-check the stop flags after the "
        "offloaded write instead of advancing on a stale snapshot"
    )


@pytest.mark.asyncio
async def test_cancelled_capture_never_publishes_the_stage_file(tmp_path, monkeypatch):
    """A cancelled capture must leave the canonical stage file untouched.

    ``asyncio.to_thread`` cannot interrupt a worker mid-syscall, so a
    cancelled await abandons a still-running writer. If that writer owned
    the canonical ``stage_N_result.md``, its bytes could land AFTER a
    resumed plan (or a new slot reusing the key) wrote its own result —
    silent corruption. The structural guarantee pinned here: the worker
    only ever writes a uniquely-named temp file, and publication to the
    canonical path happens on the loop thread strictly after an uncancelled
    return. An abandoned worker's entire blast radius is an orphan temp
    file.
    """
    import asyncio
    import threading

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.config_dir", lambda: tmp_path)
    slot = _slot_with("slow stage output")

    session_dir = tmp_path / "sessions" / slot.key
    final = session_dir / "stage_1_result.md"

    started = threading.Event()
    release = threading.Event()
    real_write = pathlib.Path.write_text

    def slow_write(self, *args, **kwargs):
        if self.parent == session_dir:
            started.set()
            release.wait(timeout=10)
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", slow_write)

    task = asyncio.ensure_future(_capture(slot, 1))
    for _ in range(500):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set(), "the worker never reached the write -- test scaffold broke"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The new plan's result lands on the canonical path...
    session_dir.mkdir(parents=True, exist_ok=True)
    real_write(final, "NEW PLAN RESULT", encoding="utf-8")

    # ...then the abandoned worker finishes. It must not clobber the file.
    release.set()
    for _ in range(500):
        await asyncio.sleep(0.01)
        if threading.active_count() == 1:
            break
    await asyncio.sleep(0.05)

    assert final.read_text(encoding="utf-8") == "NEW PLAN RESULT", (
        "an abandoned stage-result writer overwrote the canonical stage file "
        "written by a resumed plan -- the worker must only ever write a "
        "uniquely-named temp file, with publication happening on the loop "
        "thread after an uncancelled return"
    )


@pytest.mark.asyncio
async def test_stage_result_publication_runs_off_the_loop_thread(tmp_path, monkeypatch):
    """The rename that PUBLISHES the result must not run on the loop either.

    ``os.replace`` reads as a free metadata-only syscall, which is why it is easy
    to leave behind on the loop after the payload write has been offloaded. On a
    network-backed session directory it is a round trip like any other, so it
    stalls chat streaming, WebSocket frames and cron dispatch exactly as the
    write did — the same ``no-blocking-call-on-event-loop`` anchor covers both.

    Pinned separately from the write because the two moved at different times:
    an implementation can satisfy the write assertion above while still
    publishing on the loop, which is precisely the state this test was added for.
    """
    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.config_dir", lambda: tmp_path)
    slot = _slot_with("stage one output")

    seen_threads: list[int] = []
    real_replace = os.replace
    target_dir = tmp_path / "sessions" / "chat-1-stage"

    def recording_replace(src, dst, *args, **kwargs):
        dst_path = pathlib.Path(dst)
        if dst_path.parent == target_dir and "stage_1_result" in dst_path.name:
            seen_threads.append(threading.get_ident())
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", recording_replace)

    final = await _capture(slot, 1)

    assert seen_threads, (
        "the stage result was never published through os.replace -- this test no "
        "longer exercises publication and would pass vacuously"
    )
    assert threading.get_ident() not in seen_threads, (
        "the stage result was published on the event-loop thread; the rename is "
        "filesystem I/O and belongs in the same worker as the payload write"
    )
    assert (
        pathlib.Path(final).read_text(encoding="utf-8") == "stage one output"
    ), "publication moved off the loop but stopped producing the canonical file"


@pytest.mark.asyncio
async def test_no_filesystem_call_reaches_the_loop_thread(tmp_path, monkeypatch):
    """Whole-boundary check: mkdir, write and replace are all off the loop.

    The per-call tests above each pin one syscall, so a future fourth one could
    be added on the loop without failing any of them. This asserts the boundary
    itself rather than its current members.
    """
    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.config_dir", lambda: tmp_path)
    slot = _slot_with("stage one output")

    loop_thread = threading.get_ident()
    offenders: list[str] = []

    real_mkdir = pathlib.Path.mkdir
    real_write = pathlib.Path.write_text
    real_replace = os.replace
    target_dir = tmp_path / "sessions" / "chat-1-stage"

    def recording_mkdir(self, *args, **kwargs):
        if threading.get_ident() == loop_thread and self == target_dir:
            offenders.append("mkdir")
        return real_mkdir(self, *args, **kwargs)

    def recording_write(self, *args, **kwargs):
        if threading.get_ident() == loop_thread and self.parent == target_dir:
            offenders.append("write_text")
        return real_write(self, *args, **kwargs)

    def recording_replace(src, dst, *args, **kwargs):
        if threading.get_ident() == loop_thread and pathlib.Path(dst).parent == target_dir:
            offenders.append("os.replace")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "mkdir", recording_mkdir)
    monkeypatch.setattr(pathlib.Path, "write_text", recording_write)
    monkeypatch.setattr(os, "replace", recording_replace)

    await _capture(slot, 1)

    assert offenders == [], (
        "stage-result capture performed filesystem work on the event-loop "
        "thread: %s" % ", ".join(offenders)
    )
