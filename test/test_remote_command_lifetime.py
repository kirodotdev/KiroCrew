"""A timed-out or retired guest command cannot leave its process group running."""

import os
import signal
import subprocess
import sys

import pytest

from kiro_crew.remote_session import workspace_tools as wt
from kiro_crew.testing.wait import wait_until

posix_groups = pytest.mark.skipif(
    not hasattr(os, "killpg"), reason="guest commands run on a POSIX workspace"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); it is dead for this purpose.
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            return handle.read().split(") ", 1)[1][:1] != "Z"
    except OSError:
        return True


def _background_pid(path) -> int:
    text = wait_until(
        lambda: path.exists() and path.read_text(encoding="ascii").strip(),
        describe=lambda: "the command never started its background job",
    )
    return int(text)


def _wait_dead(pid: int) -> None:
    wait_until(lambda: not _alive(pid), describe=lambda: f"pid {pid} still alive")


@posix_groups
def test_a_timed_out_command_takes_its_whole_group_with_it(tmp_path):
    marker = tmp_path / "bg.pid"
    ws = wt.Workspace(str(tmp_path))
    out = ws.run_command(f"sleep 60 & echo $! > {marker}; echo partial; sleep 60", timeout=1)
    assert out.startswith("partial\n") and out.endswith("[timed out after 1s]")
    _wait_dead(_background_pid(marker))


@posix_groups
def test_a_retired_command_takes_its_whole_group_with_it(tmp_path, monkeypatch):
    marker = tmp_path / "bg.pid"
    real_popen = subprocess.Popen

    class RetiredWhileWaiting(real_popen):
        def wait(self, timeout=None):
            if timeout is not None and timeout > 10:
                _background_pid(marker)
                raise SystemExit(0)  # what main()'s SIGTERM handler raises
            return super().wait(timeout=timeout)

    monkeypatch.setattr(wt.subprocess, "Popen", RetiredWhileWaiting)
    ws = wt.Workspace(str(tmp_path))
    with pytest.raises(SystemExit):
        ws.run_command(f"sleep 60 & echo $! > {marker}; sleep 60", timeout=60)
    _wait_dead(_background_pid(marker))


@posix_groups
def test_a_finished_command_reports_its_output_and_code(tmp_path):
    ws = wt.Workspace(str(tmp_path))
    assert ws.run_command("echo ok; exit 3", timeout=10) == "ok\n\n[exit 3]"


@posix_groups
def test_endless_output_is_kept_as_a_bounded_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(wt, "MAX_OUTPUT_BYTES", 1024)
    seen = []
    real_snapshot = wt._OutputTail.snapshot

    def snapshot(self):
        seen.append(len(self._buf))
        return real_snapshot(self)

    monkeypatch.setattr(wt._OutputTail, "snapshot", snapshot)
    ws = wt.Workspace(str(tmp_path))
    total = 4 * 1024 * 1024
    script = f"import sys; sys.stdout.write('x' * {total - 4} + 'END\\n')"
    out = ws.run_command(f'{sys.executable} -c "{script}"', timeout=60)
    assert out.startswith(f"[{total - 1024} earlier bytes omitted]\n")
    assert out.endswith("END\n\n[exit 0]")
    assert seen == [1024]


def test_the_tail_never_holds_more_than_its_limit():
    tail = wt._OutputTail(10)
    chunks = [b"a" * 7, b"b" * 7, b"c" * 30, b""]
    sizes = []

    class Stream:
        def read1(self, _n):
            if tail._buf:
                sizes.append(len(tail._buf))
            return chunks.pop(0)

        def close(self):
            sizes.append("closed")

    tail.drain(Stream())
    assert all(s == "closed" or s <= 10 for s in sizes) and sizes[-1] == "closed"
    assert tail.snapshot() == (b"c" * 10, 34)


def test_a_command_runs_under_coreutils_timeout_when_present(tmp_path, monkeypatch):
    # The guest calls no kill primitive: timeout signals the command's group itself.
    seen = []
    real_popen = subprocess.Popen

    def popen(argv, **kw):
        seen.append(argv)
        return real_popen(argv, **kw)

    monkeypatch.setattr(wt.subprocess, "Popen", popen)
    wt.Workspace(str(tmp_path)).run_command("true", timeout=7)
    timer = __import__("shutil").which("timeout")
    assert seen[0][:4] == ([timer, "-k", str(wt._KILL_GRACE_SECS), "7"] if timer else seen[0][:4])
    assert seen[0][-3:] == ["/bin/sh", "-c", "true"]


def test_stdio_retirement_unwinds_and_restores_signal_handler(tmp_path, monkeypatch):
    handlers = []
    previous = signal.getsignal(signal.SIGTERM)
    real_signal = signal.signal

    def install(sig, handler):
        if sig != signal.SIGTERM:
            return real_signal(sig, handler)
        handlers.append(handler)
        return previous

    def serve(_server):
        handlers[0](signal.SIGTERM, None)
        pytest.fail("SIGTERM must unwind the command")

    monkeypatch.setattr(signal, "signal", install)
    monkeypatch.setattr(wt, "serve", serve)
    with pytest.raises(SystemExit):
        wt.main(["--root", str(tmp_path)])
    assert handlers[-1] is previous


def test_retirement_ignores_a_second_sigterm_during_command_cleanup(tmp_path, monkeypatch):
    # The worker sends TERM on stop and again on exit; the second must not raise
    # inside run_command's cleanup and let the command outlive the server.
    handlers = []
    previous = signal.getsignal(signal.SIGTERM)
    real_signal = signal.signal

    def install(sig, handler):
        if sig != signal.SIGTERM:
            return real_signal(sig, handler)
        handlers.append(handler)
        return previous

    def serve(_server):
        retire = handlers[0]
        with pytest.raises(SystemExit):
            retire(signal.SIGTERM, None)
        # Retirement switched TERM off before unwinding.
        assert handlers[1] is signal.SIG_IGN
        raise SystemExit(0)

    monkeypatch.setattr(signal, "signal", install)
    monkeypatch.setattr(wt, "serve", serve)
    with pytest.raises(SystemExit):
        wt.main(["--root", str(tmp_path)])
    assert handlers[-1] is previous
