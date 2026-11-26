"""The Strands gate read-back's refusal says how the probe ended and what it printed.

A project worker that fails to start is reported by Strands CLI 0.2.4 as the error
it answers ``session/new`` with, on stdout; a CLI that dies says so on stderr and in
its exit code. The refusal quotes both, each through the stderr-tail check, and
tells an exit from the wait running out.
"""

from __future__ import annotations

import io
import json
import os
import select
import sys
import threading
from types import SimpleNamespace

import pytest

from kiro_crew.acp.harness import strands as strands_mod
from kiro_crew.acp.harness import strands_readback as readback
from kiro_crew.acp.stderr_tail import STDERR_WITHHELD
from kiro_crew.testing.synthetic_credentials import github_classic

_SESSION_ID = strands_mod._READBACK_SESSION_ID


def _answer(request_id: int, error: dict) -> bytes:
    return (json.dumps({"jsonrpc": "2.0", "id": request_id, "error": error}) + "\n").encode()


def test_the_session_new_error_and_its_details_are_quoted() -> None:
    stdout = b'{"jsonrpc": "2.0", "id": 1, "result": {}}\n' b"not json\n" + _answer(
        _SESSION_ID,
        {
            "message": "Internal error",
            "data": {"details": "No package metadata\nwas found for openai"},
        },
    )

    assert readback.session_new_error(stdout, _SESSION_ID) == (
        "Internal error: No package metadata was found for openai"
    )


def test_a_successful_or_missing_session_new_answer_quotes_nothing() -> None:
    ok = b'{"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s"}}\n'
    assert readback.session_new_error(ok, _SESSION_ID) == ""
    assert readback.session_new_error(_answer(1, {"message": "x"}), _SESSION_ID) == ""
    assert readback.session_new_error(b"", _SESSION_ID) == ""


def test_an_error_carrying_a_credential_is_withheld() -> None:
    stdout = _answer(_SESSION_ID, {"message": f"bad key {github_classic()}"})

    assert readback.session_new_error(stdout, _SESSION_ID) == STDERR_WITHHELD


def test_a_credential_wrapped_across_lines_of_the_error_is_withheld() -> None:
    token = github_classic()
    half = len(token) // 2
    stdout = _answer(
        _SESSION_ID,
        {"message": "Internal error", "data": {"details": f"{token[:half]}\n    {token[half:]}"}},
    )

    assert readback.session_new_error(stdout, _SESSION_ID) == STDERR_WITHHELD


def test_the_stderr_tail_keeps_the_last_lines_and_withholds_a_credential() -> None:
    lines = [f"line {n}" for n in range(10)]
    assert readback.stderr_tail("\n".join(lines).encode()) == " | ".join(lines[-5:])

    leaky = "\n".join([f"token {github_classic()}", *lines]).encode()
    assert readback.stderr_tail(leaky) == STDERR_WITHHELD
    assert readback.stderr_tail(b"\n \n") == ""


def test_a_capture_keeps_a_bounded_head_or_a_tail_starting_on_a_whole_line() -> None:
    cap = readback.READBACK_OUTPUT_MAX_BYTES
    tail = readback.OutputCapture(io.BytesIO(b"x" * cap * 3 + b"\nlast line\n"), keep_tail=True)
    head = readback.OutputCapture(io.BytesIO(b"first\n" + b"y" * cap * 3), keep_tail=False)

    assert tail.finish(5.0) == b"last line\n"
    kept = head.finish(5.0)
    assert len(kept) == cap and kept.startswith(b"first\n")
    assert readback.OutputCapture(None, keep_tail=True).finish(5.0) == b""


def _select_peek(fd: int):
    """A ``PeekNamedPipe`` stand-in: 1 while *fd* is readable (bytes or the end)."""

    def peek() -> int:
        return 1 if select.select([fd], [], [], 0)[0] else 0

    return peek


@pytest.mark.parametrize("windows", [False, True], ids=["posix-select", "windows-peek"])
def test_a_pipe_whose_writer_stays_open_is_stopped_and_closed(monkeypatch, windows) -> None:
    if windows:
        monkeypatch.setattr(readback.platform_compat, "IS_POSIX", False)
        if os.name != "nt":
            monkeypatch.setattr(readback, "_windows_pipe_peek", _select_peek)
    elif os.name == "nt":
        pytest.skip("select() takes no pipe on Windows")
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    try:
        os.write(write_fd, b"partial\n")
        capture = readback.OutputCapture(stream, keep_tail=True)

        assert capture.finish(0.05) == b"partial\n"
        assert capture._thread is not None and not capture._thread.is_alive()
        assert stream.closed
    finally:
        os.close(write_fd)


def test_a_peeked_pipe_whose_writer_closed_is_ready_for_its_end() -> None:
    def broken() -> int:
        raise BrokenPipeError

    stop = threading.Event()
    assert readback._peek_waiter(broken, stop)(0) is True
    assert readback._peek_waiter(lambda: 0, stop)(0) is False
    if os.name != "nt":
        assert readback._windows_pipe_peek(0) is None


def test_the_issue_tells_an_exit_from_a_timeout() -> None:
    timed_out = readback.gate_not_loaded_issue(None, 90.0, b"", b"", _SESSION_ID)
    exited = readback.gate_not_loaded_issue(3, 90.0, b"", b"Traceback\nImportError", _SESSION_ID)

    assert "did not load Kiro Crew's gate within 90s" in timed_out
    assert "session/new" not in timed_out and "stderr" not in timed_out
    assert "before the harness exited with code 3" in exited
    assert exited.endswith("; its stderr ended: Traceback | ImportError")


#: A Strands CLI stand-in that reads the two requests, answers ``session/new`` with
#: the worker's exception, writes a traceback line to stderr and exits with code 3.
_CLI_THAT_FAILS = (
    "import json, sys\n"
    "sys.stdin.readline(); sys.stdin.readline()\n"
    "print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {}}), flush=True)\n"
    f"print(json.dumps({{'jsonrpc': '2.0', 'id': {_SESSION_ID}, 'error': "
    "{'code': -32603, 'message': 'Internal error', "
    "'data': {'details': 'No package metadata was found for openai'}}}), flush=True)\n"
    "sys.stderr.write('ModuleNotFoundError: openai\\n')\n"
    "sys.exit(3)\n"
)


def test_a_probe_that_exits_reports_its_code_its_answer_and_its_stderr(tmp_path) -> None:
    issue, remedy = strands_mod._verify_strands_gate(
        SimpleNamespace(_spawn_work_dir=str(tmp_path)),
        [sys.executable, "-c", _CLI_THAT_FAILS],
        dict(os.environ),
        "/sealed/agent.py",
        str(tmp_path / "marker.json"),
        "nonce",
        child_scrub_names=(),
    )

    assert "before the harness exited with code 3" in issue
    assert (
        "the harness answered session/new with: Internal error: "
        "No package metadata was found for openai" in issue
    )
    assert issue.endswith("; its stderr ended: ModuleNotFoundError: openai")
    assert remedy


class _ClosedStdin(io.BytesIO):
    def write(self, data) -> int:
        raise BrokenPipeError("the probe went away")


class _ProbeThatDiedAtOnce:
    """A ``Popen`` stand-in for a CLI that exited with code 3 before taking the requests."""

    def __init__(self, *args, **kwargs) -> None:
        self.stdin = _ClosedStdin()
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"Traceback (most recent call last)\nImportError: openai\n")
        self.returncode = 3

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


def test_a_probe_that_died_before_the_requests_reports_its_code_and_stderr(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(strands_mod.subprocess_mod, "Popen", _ProbeThatDiedAtOnce)

    issue, _remedy = strands_mod._verify_strands_gate(
        SimpleNamespace(_spawn_work_dir=str(tmp_path)),
        ["strands"],
        {},
        "/sealed/agent.py",
        str(tmp_path / "marker.json"),
        "nonce",
        child_scrub_names=(),
    )

    assert issue == (
        "the gate's load marker could not be read back (the probe went away); the "
        "harness exited with code 3; its stderr ended: "
        "Traceback (most recent call last) | ImportError: openai"
    )
