"""How the parent handles the memory index build child, with no real process.

A fake spawn context stands in for ``multiprocessing``, so each outcome (a
result, a timeout, a child that dies, a child that cannot start) is chosen
by the test and nothing waits on a real process.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew.memory_index import build_child


class _Receiver:
    def __init__(self, ready: bool, result: object, raises: bool) -> None:
        self.ready, self.result, self.raises = ready, result, raises
        self.waited: list[float] = []

    def poll(self, timeout: float) -> bool:
        self.waited.append(timeout)
        return self.ready

    def recv(self) -> object:
        if self.raises:
            raise EOFError
        return self.result


class _Sender:
    def close(self) -> None:
        pass


class _Process:
    def __init__(self, alive_after_join: bool, exitcode: int | None) -> None:
        self.alive_after_join = alive_after_join
        self.exitcode = exitcode
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def join(self, timeout: float | None = None) -> None:
        self.calls.append("join")

    def is_alive(self) -> bool:
        return self.alive_after_join and "kill" not in self.calls

    def terminate(self) -> None:
        self.calls.append("terminate")

    def kill(self) -> None:
        self.calls.append("kill")


class _Context:
    def __init__(self, receiver: _Receiver, process: _Process) -> None:
        self.receiver, self.process = receiver, process

    def Pipe(self, duplex: bool = True):  # noqa: N802 - mirrors multiprocessing
        return self.receiver, _Sender()

    def Process(self, **_kwargs):  # noqa: N802 - mirrors multiprocessing
        return self.process


def _run(monkeypatch, *, ready=True, result=None, raises=False, alive=False, exitcode=0):
    receiver = _Receiver(ready, result, raises)
    process = _Process(alive, exitcode)
    monkeypatch.setattr(
        build_child.multiprocessing, "get_context", lambda _method: _Context(receiver, process)
    )
    out = build_child.run_build_in_child(
        "db", "root", "sig", 3, timeout=_DEADLINE  # flake-ok: the fake pipe never waits
    )
    return out, receiver, process


#: The fake receiver only records the deadline; nothing waits on it.
_DEADLINE = build_child.CHILD_TIMEOUT_SECS


def test_a_child_that_sends_its_build_returns_the_generation(monkeypatch):
    out, receiver, process = _run(monkeypatch, result=("gen-abc", 12))
    assert out == ("gen-abc", 12)
    assert receiver.waited == [_DEADLINE]
    assert "terminate" not in process.calls


def test_a_child_past_its_deadline_is_stopped_and_the_parent_builds(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=build_child.logger.name)
    out, _receiver, process = _run(monkeypatch, ready=False, alive=True)
    assert out is None
    # Asked to stop, given the grace period, then killed when it is still alive.
    assert process.calls == ["start", "terminate", "join", "kill", "join"]
    assert f"timed out after {_DEADLINE:.0f}s" in caplog.text


@pytest.mark.parametrize(("raises", "exitcode"), [(True, 1), (False, -9)])
def test_a_child_that_dies_without_a_result_falls_back(monkeypatch, caplog, raises, exitcode):
    caplog.set_level(logging.WARNING, logger=build_child.logger.name)
    out, _receiver, _process = _run(monkeypatch, raises=raises, exitcode=exitcode)
    assert out is None
    assert f"exited with code {exitcode}" in caplog.text


def test_a_child_that_lingers_after_sending_is_stopped(monkeypatch):
    out, _receiver, process = _run(monkeypatch, result=("gen-x", 3), alive=True)
    assert out == ("gen-x", 3)
    assert "terminate" in process.calls


def test_a_child_that_cannot_start_falls_back(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=build_child.logger.name)

    def no_spawn(_method):
        raise OSError("no spawn here")

    monkeypatch.setattr(build_child.multiprocessing, "get_context", no_spawn)
    assert build_child.run_build_in_child("db", "root", "sig", 3) is None
    assert "could not start (OSError: no spawn here)" in caplog.text
