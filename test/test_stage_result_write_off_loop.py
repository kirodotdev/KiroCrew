"""Writing a stage result must not run on the gateway event loop.

``_stage_loop`` captures each finished stage to disk. That capture used to be one
synchronous call on the loop: it walked ``slot.messages``, redacted every
assistant segment, created the session directory and wrote the file — so a slow
disk or a large stage blocked every other session on the gateway (issue #1783).

Only the parts that CAN cross a thread boundary do. The message walk stays on the
loop because ``slot.messages`` is live state the loop mutates; the redaction and
the filesystem work take an immutable tuple of strings, which is what makes them
safe to hand to a worker. Same split as ``_previous_result_paths`` /
``_read_previous_results``.
"""

from __future__ import annotations

import pathlib
import threading
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import _ChatSlot


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    for module in ("state", "chat", "chat_orchestrator"):
        monkeypatch.setattr(f"kiro_crew.dashboard.{module}.config_dir", lambda: tmp_path)


def _make_state():
    state = MagicMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.subagents = MagicMock()
    state.subagents.running_agents_for = MagicMock(return_value=[])
    return state


def _make_slot(titles=("First",)):
    slot = _ChatSlot("stage-write-slot", mode="orchestrator")
    slot._auto_run = True
    slot._stage_titles = list(titles)
    slot._plan_goal = "Test goal"
    slot._orch_tracker = None
    return slot


def _stage_texts(monkeypatch, texts):
    box = {"n": 0}

    async def _mock_run_chat(state, slot, message, **kwargs):
        idx = box["n"]
        box["n"] += 1
        if idx < len(texts):
            slot.append("assistant", texts[idx], "msg msg-a")

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)


async def _run_plan(monkeypatch, titles, texts):
    from kiro_crew.dashboard.chat import _stage_loop

    slot = _make_slot(titles)
    _stage_texts(monkeypatch, texts)
    await _stage_loop(_make_state(), slot, auto_run=True)
    return slot


@pytest.mark.asyncio
async def test_stage_result_write_runs_off_the_loop_thread(monkeypatch, tmp_path):
    """RED BEFORE: the write was a bare synchronous call in the stage loop."""
    seen_threads: list[int] = []
    real_write = pathlib.Path.write_text
    target = tmp_path / "sessions" / "stage-write-slot" / "stage_1_result.md"

    def recording_write(self, *args, **kwargs):
        # Scoped to this stage's result file: history and metadata writes on the
        # loop are legitimate and must not decide this assertion.
        if self == target:
            seen_threads.append(threading.get_ident())
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", recording_write)

    await _run_plan(monkeypatch, ["First"], ["alpha done"])

    assert seen_threads, (
        "the stage result was never written -- this test no longer exercises the "
        "capture and would pass vacuously"
    )
    assert threading.get_ident() not in seen_threads, (
        "the stage result was written on the event-loop thread; the filesystem "
        "work must be handed to asyncio.to_thread"
    )


@pytest.mark.asyncio
async def test_session_directory_is_created_off_the_loop_thread(monkeypatch, tmp_path):
    """``mkdir`` is its own syscall and travels with the write."""
    seen_threads: list[int] = []
    real_mkdir = pathlib.Path.mkdir
    target = tmp_path / "sessions" / "stage-write-slot"

    def recording_mkdir(self, *args, **kwargs):
        if self == target:
            seen_threads.append(threading.get_ident())
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "mkdir", recording_mkdir)

    await _run_plan(monkeypatch, ["First"], ["alpha done"])

    assert seen_threads, "the session directory was never created"
    assert threading.get_ident() not in seen_threads


@pytest.mark.asyncio
async def test_redaction_runs_off_the_loop_thread(monkeypatch):
    """The regex pass over a whole stage's output is CPU work, and it moves too."""
    from kiro_crew.dashboard import chat_orchestrator

    seen_threads: list[int] = []
    real = chat_orchestrator.redact_credentials
    stage_body = "alpha done"

    def recording(text):
        # Scoped to the stage body: the loop also redacts the separator, the
        # injected context and the completion summary, and those calls belong on
        # the loop.
        if text == stage_body:
            seen_threads.append(threading.get_ident())
        return real(text)

    monkeypatch.setattr(chat_orchestrator, "redact_credentials", recording)

    await _run_plan(monkeypatch, ["First"], [stage_body])

    assert seen_threads, (
        "the stage body was never redacted -- this test no longer exercises the "
        "capture's redaction pass and would pass vacuously"
    )
    assert (
        threading.get_ident() not in seen_threads
    ), "stage-result redaction ran on the event-loop thread"


@pytest.mark.asyncio
async def test_the_message_walk_stays_on_the_loop(monkeypatch):
    """Live slot state must NOT be reachable from the worker.

    ``_collect_stage_result_parts`` reads ``slot.messages``, which the loop
    mutates, so it is the half that must stay put -- handing the slot itself to a
    thread is the bug this split exists to avoid.
    """
    from kiro_crew.dashboard import chat_orchestrator

    seen_threads: list[int] = []
    real = chat_orchestrator._collect_stage_result_parts

    def recording(slot):
        seen_threads.append(threading.get_ident())
        return real(slot)

    monkeypatch.setattr(chat_orchestrator, "_collect_stage_result_parts", recording)

    await _run_plan(monkeypatch, ["First"], ["alpha done"])

    assert seen_threads == [threading.get_ident()]


# ── Preservation ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_result_file_content_is_unchanged(monkeypatch, tmp_path):
    slot = await _run_plan(monkeypatch, ["First", "Second"], ["alpha done", "beta done"])

    body = (tmp_path / "sessions" / slot.key / "stage_2_result.md").read_text(encoding="utf-8")
    assert "beta done" in body
    # Only this stage's output: the walk stops at the stage separator.
    assert "alpha done" not in body


@pytest.mark.asyncio
async def test_credentials_are_still_redacted_before_disk(monkeypatch, tmp_path):
    """Preservation: this writes a NEW file outside the history log's redaction."""
    slot = await _run_plan(monkeypatch, ["First"], ["key AKIAIOSFODNN7EXAMPLE here"])

    body = (tmp_path / "sessions" / slot.key / "stage_1_result.md").read_text(encoding="utf-8")
    assert "AKIAIOSFODNN7EXAMPLE" not in body


def test_the_two_halves_compose_to_the_same_result(tmp_path, monkeypatch):
    """Preservation: walking then writing off-loop still produces the same file.

    The split is only sound if the halves compose, so this drives them in sequence
    the way ``_stage_loop`` does and checks the bytes that land.
    """
    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.config_dir", lambda: tmp_path)
    from kiro_crew.dashboard.chat_orchestrator import (
        _collect_stage_result_parts,
        _write_stage_result,
    )

    slot = _ChatSlot("sync-capture-slot", mode="orchestrator")
    slot.append("assistant", "written synchronously", "msg msg-a")

    path = _write_stage_result(slot.key, 1, _collect_stage_result_parts(slot))

    assert pathlib.Path(path).read_text(encoding="utf-8") == "written synchronously"
