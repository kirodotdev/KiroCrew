"""A managed MCP server exits on SIGTERM/SIGINT without interpreter finalization.

Tool work runs on a daemon thread, so a normal shutdown finalizes the stdio
streams while that thread may still be using them, and CPython can abort with
``_enter_buffered_busy`` (SIGABRT). ``run_mcp_stdio_loop`` installs the same
hard exit the gateway stub uses, and only on the main thread, because
``signal.signal`` refuses any other.
"""

from __future__ import annotations

import signal
import threading

import pytest

from kiro_crew import mcp_shared


class _Stream:
    def __init__(self, name: str, calls: list[str]) -> None:
        self._name = name
        self._calls = calls

    def flush(self) -> None:
        self._calls.append(f"flush:{self._name}")

    def write(self, _text: str) -> int:
        self._calls.append(f"write:{self._name}")
        return 0


class TestHardExitOnSignal:
    def test_drains_logging_flushes_stderr_exits_zero_and_leaves_stdout(self, monkeypatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(mcp_shared.logging, "shutdown", lambda: calls.append("logging"))
        monkeypatch.setattr(mcp_shared.sys, "stdout", _Stream("out", calls))
        monkeypatch.setattr(mcp_shared.sys, "stderr", _Stream("err", calls))
        monkeypatch.setattr(mcp_shared.os, "_exit", lambda code: calls.append(f"exit:{code}"))

        mcp_shared._hard_exit_on_signal(signal.SIGTERM, None)

        # logging first so its records reach stderr before the flush; stdout is
        # never touched, because another thread may hold its lock.
        assert calls == ["logging", "flush:err", "exit:0"]

    @pytest.mark.parametrize(
        "error",
        [
            ValueError("I/O operation on closed file"),
            # The signal landed while the main thread was inside a stderr write.
            RuntimeError("reentrant call inside <_io.BufferedWriter name='<stderr>'>"),
        ],
    )
    def test_exit_still_fires_when_the_stderr_flush_fails(self, monkeypatch, error) -> None:
        codes: list[int] = []

        class _Closed:
            def flush(self) -> None:
                raise error

        monkeypatch.setattr(mcp_shared.logging, "shutdown", lambda: None)
        monkeypatch.setattr(mcp_shared.sys, "stderr", _Closed())
        monkeypatch.setattr(mcp_shared.os, "_exit", codes.append)

        mcp_shared._hard_exit_on_signal(signal.SIGTERM, None)

        assert codes == [0]


class TestLoopInstallsTheHandler:
    @pytest.fixture
    def seen_during_loop(self, monkeypatch) -> dict:
        seen: dict = {}

        def fake_dispatch(*_args, **_kwargs) -> bool:
            seen["term"] = signal.getsignal(signal.SIGTERM)
            seen["int"] = signal.getsignal(signal.SIGINT)
            return False

        monkeypatch.setattr(mcp_shared, "_run_stdio_dispatch_loop", fake_dispatch)
        monkeypatch.setattr(mcp_shared, "snapshot_stdout_fd", lambda: None)
        monkeypatch.setattr(mcp_shared, "release_stdout_fd", lambda: None)
        return seen

    @staticmethod
    def _run() -> None:
        mcp_shared.run_mcp_stdio_loop("test-server", "0", lambda: [], lambda _n, _a: "")

    def test_installed_while_serving_and_restored_after(self, seen_during_loop) -> None:
        before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))

        self._run()

        assert seen_during_loop["term"] is mcp_shared._hard_exit_on_signal
        assert seen_during_loop["int"] is mcp_shared._hard_exit_on_signal
        assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == before

    def test_off_the_main_thread_it_serves_without_installing(self, seen_during_loop) -> None:
        errors: list[BaseException] = []

        def body() -> None:
            try:
                self._run()
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        worker = threading.Thread(target=body)
        worker.start()
        worker.join(timeout=10)

        assert errors == []
        assert seen_during_loop["term"] is not mcp_shared._hard_exit_on_signal
