"""The KIROCREW_READY line must survive a concurrent stdout suppression window.

Regression for the Phase 2 sidecar wedge: llama-cpp's vendored
``suppress_stdout_stderr`` dup2's /dev/null over fd 1 on a worker thread while
the embedding model loads; a plain ``print`` on the main thread during that
window was swallowed and the supervising parent never saw READY.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

from kiro_crew.ready_line import capture_stdout_fd, emit_ready_line, format_ready_line


def test_format_ready_line_prefix_and_newline() -> None:
    line = format_ready_line({"port": 1, "pid": 2})
    assert line.startswith("KIROCREW_READY:")
    assert line.endswith("\n")
    assert json.loads(line[len("KIROCREW_READY:") : -1]) == {"port": 1, "pid": 2}


def test_emit_ready_line_survives_dup2_devnull_window() -> None:
    # Run in a subprocess so we can freely clobber fd 1 the way llama-cpp does.
    script = textwrap.dedent(
        """
        import os, sys
        from kiro_crew.ready_line import capture_stdout_fd, emit_ready_line
        fd = capture_stdout_fd()              # taken before the suppression window
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 1)                   # llama-cpp suppress_stdout_stderr
        print("swallowed", flush=True)        # proves fd 1 is really /dev/null now
        emit_ready_line({"port": 4242, "pid": os.getpid()}, fd)
        os.write(devnull, b"")  # keep fd 1 pointed at devnull until exit
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=True,
        timeout=30,
    )
    out = proc.stdout.decode()
    assert "swallowed" not in out
    assert out.startswith("KIROCREW_READY:")
    assert json.loads(out.split(":", 1)[1])["port"] == 4242


def test_emit_ready_line_falls_back_to_print_without_fd(capsys) -> None:
    emit_ready_line({"port": 7}, None)
    assert capsys.readouterr().out == 'KIROCREW_READY:{"port": 7}\n'


def test_emit_ready_line_closes_fd() -> None:
    r, w = os.pipe()
    emit_ready_line({"ok": True}, w)
    assert os.read(r, 1024) == b'KIROCREW_READY:{"ok": true}\n'
    os.close(r)
    try:
        os.fstat(w)
    except OSError:
        pass
    else:  # pragma: no cover - failure path
        raise AssertionError("out_fd was not closed")


def test_capture_stdout_fd_is_a_duplicate() -> None:
    fd = capture_stdout_fd()
    assert fd is not None and fd != sys.stdout.fileno()
    os.close(fd)
