"""A wait that reaches its budget, or runs long, logs one line naming it; a quick one logs nothing.

Three gateway waits: the embedding child's stop wait, the ``chat --help``
probe, and the unbounded embed-pool admission wait. Each slow step advances a fake
clock from inside itself, so host load cannot change the result.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import subprocess
from types import SimpleNamespace

import pytest

from kiro_crew import embeddings_child, executors, kiro_cli, kiro_prerequisite
from kiro_crew import sandbox as sandbox_mod
from kiro_crew.kiro_prerequisite_process import ProcessResult
from kiro_crew.testing.wait import await_until


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _records(caplog: pytest.LogCaptureFixture, logger: str, needle: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == logger and needle in r.getMessage()]


def _one(caplog: pytest.LogCaptureFixture, logger: str, needle: str) -> logging.LogRecord:
    records = _records(caplog, logger, needle)
    assert len(records) == 1, [r.getMessage() for r in records]
    return records[0]


# ── embedding child stop ──


class _Proc:
    def __init__(self, clock: _Clock, takes: float, exits: bool) -> None:
        self.clock, self.takes, self.exits = clock, takes, exits

    def wait(self, timeout: float) -> int:
        self.clock.now += self.takes
        if not self.exits:
            raise subprocess.TimeoutExpired("child", timeout)
        return 0


@pytest.fixture
def stop(monkeypatch):
    """Run ``_ChildState.shutdown`` on a fake child; return what it killed."""
    clock = _Clock()
    monkeypatch.setattr(embeddings_child, "time", SimpleNamespace(monotonic=clock))
    killed: list[object] = []
    monkeypatch.setattr(embeddings_child, "_kill", killed.append)

    def _run(takes: float, exits: bool) -> list[object]:
        state = embeddings_child._ChildState({}, "factory")
        proc = _Proc(clock, takes, exits)
        state.conn = SimpleNamespace(send=lambda _msg: None, outbox=queue.Queue(), proc=proc)
        state.shutdown()
        assert killed[-1] is proc
        return killed

    return _run


def test_a_child_that_outlives_the_stop_wait_logs_one_warning(stop, caplog):
    caplog.set_level(logging.DEBUG, logger="kiro_crew.embeddings")
    stop(takes=embeddings_child._CHILD_STOP_WAIT_SECS, exits=False)
    record = _one(caplog, "kiro_crew.embeddings", "embedding child")
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "embedding child did not exit within 5.0s of the stop request; killing it (elapsed_s=5.0)"
    )


def test_a_slow_stop_logs_info_and_a_quick_one_logs_nothing(stop, caplog):
    caplog.set_level(logging.DEBUG, logger="kiro_crew.embeddings")
    stop(takes=0.01, exits=True)
    assert _records(caplog, "kiro_crew.embeddings", "embedding child") == []
    stop(takes=2.5, exits=True)
    record = _one(caplog, "kiro_crew.embeddings", "embedding child")
    assert record.levelno == logging.INFO
    assert record.getMessage() == (
        "embedding child took 2.5s to exit after the stop request (budget 5.0s)"
    )


# ── chat --help probe ──


@pytest.fixture
def probe(tmp_path, monkeypatch):
    """A pinned stand-in binary whose sandboxed ``chat --help`` answers *result* after *takes* s."""
    binary = tmp_path / "kiro-cli"
    binary.write_bytes(b"")
    binary.chmod(0o755)
    monkeypatch.setattr(kiro_cli, "_chat_engines_cache", {})
    monkeypatch.setattr(sandbox_mod, "configured_sandbox_mode", lambda: "standard")
    clock = _Clock()
    monkeypatch.setattr(kiro_cli, "time", SimpleNamespace(monotonic=clock))
    answer: dict = {}

    async def _run(_command, _args, **_kwargs):
        clock.now += answer["takes"]
        if answer["result"] is None:
            raise OSError("spawn failed")
        return answer["result"]

    monkeypatch.setattr(kiro_prerequisite, "_run_process", _run)

    def _probe(result: ProcessResult | None, takes: float = 0.1):
        answer.update(result=result, takes=takes)
        return kiro_cli.chat_agent_engines_at(str(binary))

    return _probe


_ENGINES_HELP = "      --agent-engine <E>  engine [possible values: v2, v3]\n"


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (ProcessResult(ok=False, timed_out=True, error="process timed out"), "timeout"),
        (ProcessResult(ok=False, output="error: bad flag", returncode=2), "nonzero_exit"),
        (None, "no_output"),
    ],
)
def test_a_probe_without_an_engine_list_logs_its_reason_once(probe, caplog, result, reason):
    caplog.set_level(logging.DEBUG, logger="kiro_crew.kiro_cli")
    assert probe(result, takes=15.0) is None
    record = _one(caplog, "kiro_crew.kiro_cli", "chat --help probe")
    assert record.levelno == logging.INFO
    assert record.getMessage() == (
        f"chat --help probe gave no engine list; reason={reason} elapsed_s=15.0 budget_s=15; "
        "cached until the binary changes"
    )
    # The cached answer is unchanged, and a second read of the same binary logs nothing.
    assert probe(ProcessResult(ok=True, output=_ENGINES_HELP, returncode=0)) is None
    assert len(_records(caplog, "kiro_crew.kiro_cli", "chat --help probe")) == 1


def test_a_probe_that_answers_logs_nothing(probe, caplog):
    caplog.set_level(logging.DEBUG, logger="kiro_crew.kiro_cli")
    assert probe(ProcessResult(ok=True, output=_ENGINES_HELP, returncode=0)) == ("v2", "v3")
    assert _records(caplog, "kiro_crew.kiro_cli", "chat --help probe") == []


# ── embed admission wait ──


@pytest.mark.asyncio
async def test_a_long_unbounded_admission_wait_logs_one_debug_line(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="kiro_crew.executors")
    clock = _Clock()
    monkeypatch.setattr("kiro_crew.embed_admission_wait.time", SimpleNamespace(monotonic=clock))
    loop = asyncio.get_running_loop()
    admission = executors._loop_admission(
        loop, "_kirocrew_memory_background_admission", executors._MAX_MEMORY_BACKGROUND_WORKERS
    )
    held = executors._MAX_MEMORY_BACKGROUND_WORKERS
    for _ in range(held):
        await admission.acquire()
    run = asyncio.ensure_future(executors.run_in_memory_background_pool(lambda: "done"))
    await asyncio.sleep(0)
    assert not run.done()
    clock.now += 6.0
    for _ in range(held):
        admission.release()
    assert await await_until(run, what="the admitted call") == "done"
    record = _one(caplog, "kiro_crew.executors", "embed admission")
    assert record.levelno == logging.DEBUG
    assert record.getMessage() == f"embed admission waited 6.0s; budget=none workers={held}"


@pytest.mark.asyncio
async def test_a_quick_admission_wait_logs_nothing(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="kiro_crew.executors")
    clock = _Clock()
    monkeypatch.setattr("kiro_crew.embed_admission_wait.time", SimpleNamespace(monotonic=clock))
    assert await executors.run_in_memory_background_pool(lambda: "done") == "done"
    assert _records(caplog, "kiro_crew.executors", "embed admission") == []
