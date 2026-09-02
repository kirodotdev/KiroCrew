"""The stub must not exit through interpreter finalization.

``stub-stdin`` is a daemon thread that parks in ``sys.stdin.buffer.readline()``
for the life of the process, holding that ``BufferedReader``'s lock. Finalization
flushes and closes the same stream, cannot take the lock, and aborts the process
with ``Fatal Python error: _enter_buffered_busy`` (SIGABRT) instead of returning
the exit code. :func:`_hard_exit` bypasses finalization after doing its two
relevant jobs by hand.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
import textwrap
from pathlib import Path

from kiro_crew.mcp_gateway import stub


class TestHardExit:
    def test_drains_logging_then_flushes_stderr_then_exits(self, monkeypatch) -> None:
        calls: list[str] = []

        class _Stream:
            def __init__(self, name: str) -> None:
                self._name = name

            def flush(self) -> None:
                calls.append(f"flush:{self._name}")

        monkeypatch.setattr(stub.logging, "shutdown", lambda: calls.append("logging"))
        monkeypatch.setattr(stub.sys, "stdout", _Stream("out"))
        monkeypatch.setattr(stub.sys, "stderr", _Stream("err"))
        monkeypatch.setattr(stub.os, "_exit", lambda code: calls.append(f"exit:{code}"))

        stub._hard_exit(3)

        # logging.shutdown() drains handler buffers into stderr, so it has to run
        # before stderr is flushed or those records are lost. stdout is owned by
        # the stub-stdout writer thread (which flushes per frame and may hold the
        # buffer lock while blocked on a stalled pipe), so _hard_exit must NOT
        # touch it -- flushing it here could deadlock before os._exit.
        assert calls == ["logging", "flush:err", "exit:3"]

    def test_exit_still_fires_when_teardown_raises(self, monkeypatch) -> None:
        codes: list[int] = []

        class _BrokenStream:
            def flush(self) -> None:
                raise ValueError("I/O operation on closed file")

        def _boom() -> None:
            raise RuntimeError("handler already closed")

        monkeypatch.setattr(stub.logging, "shutdown", _boom)
        monkeypatch.setattr(stub.sys, "stdout", _BrokenStream())
        monkeypatch.setattr(stub.sys, "stderr", _BrokenStream())
        monkeypatch.setattr(stub.os, "_exit", lambda code: codes.append(code))

        stub._hard_exit(0)

        assert codes == [0]

    def test_main_does_not_exit_through_finalization(self) -> None:
        # Regression guard: sys.exit() on this path is what caused the abort.
        src = inspect.getsource(stub.main)
        assert "_hard_exit(rc)" in src
        assert "sys.exit(rc)" not in src


class TestHardExitEndToEnd:
    """End-to-end proof, in a real subprocess, that the pattern the stub uses
    exits cleanly under ``_hard_exit`` with the requested code intact.

    Deliberately NOT exercised here: the ``sys.exit`` failure mode this PR
    fixes. Reproducing it means aborting a child with SIGABRT, and every
    platform's crash tooling persists an artifact somewhere no tmp dir can
    contain (the kernel core pattern or a piped collector such as
    systemd-coredump on Linux, ReportCrash on macOS, WER on Windows). The
    regression is instead locked in by
    ``TestHardExit.test_main_does_not_exit_through_finalization`` (main() must
    call ``_hard_exit``, never ``sys.exit``) plus the ``_hard_exit`` unit
    tests above -- together they fail if the fix is reverted, without ever
    crashing a process.
    """

    _PROGRAM = textwrap.dedent("""
        import os, sys, threading, time

        def _reader():
            sys.stdin.buffer.readline()   # parks holding the BufferedReader lock

        threading.Thread(target=_reader, name="stub-stdin", daemon=True).start()
        time.sleep(0.4)                   # let the thread reach the read
        try:
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
        os._exit(7)
        """)

    def _run(self, cwd: Path) -> tuple[int, str]:
        # stdin must stay OPEN and silent: subprocess.run(stdin=PIPE) closes it
        # immediately, readline() returns b"" at EOF, the thread exits, and the
        # scenario no longer models the real stub. Holding the write end here
        # is what parks the reader in the same state the real stub sits in.
        proc = subprocess.Popen(
            [sys.executable, "-c", self._PROGRAM],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=cwd,
        )
        try:
            rc = proc.wait(timeout=60)
            assert proc.stderr is not None
            return rc, proc.stderr.read().decode("utf-8", "replace")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            for handle in (proc.stdin, proc.stderr):
                if handle is not None:
                    handle.close()

    def test_hard_exit_preserves_code(self, tmp_path: Path) -> None:
        rc, err = self._run(cwd=tmp_path)
        assert rc == 7, err
        assert "_enter_buffered_busy" not in err
