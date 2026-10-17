"""The packaged ``postToolUse`` audit hook writes one whole line per tool call.

A record split across several appends lets concurrent tools interleave, and a
call cut off between the appends leaves a header with no payload. These tests
run the shipped command string through ``sh`` and check that each call adds
exactly one terminated line that carries both the time and the payload, on
both of its paths: the ``python3`` one-write path and the ``printf`` fallback
used when no ``python3`` is on ``PATH``.

``PATH`` is a directory each test builds, so the hook never resolves a
``python3`` this test did not choose: ours is a wrapper that execs
``sys.executable``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

DEFAULTS_JSON = (
    Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "config" / "defaults.json"
)
SH = shutil.which("sh")

pytestmark = [
    pytest.mark.skipif(SH is None, reason="needs a POSIX sh to run the hook command"),
    # The hook is POSIX shell, and the PATH each test builds holds shebang
    # wrapper scripts. Off a POSIX host Crew runs hooks with ``cmd /c`` and a
    # Windows sh does not exec those wrappers (exit 127), so neither applies.
    pytest.mark.skipif(os.name != "posix", reason="the hook and its PATH wrappers need POSIX"),
]

RECORD = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ BASH: (?P<payload>\{.*\})$")
MODES = ["python3", "printf-fallback"]


def _audit_command() -> str:
    hooks = json.loads(DEFAULTS_JSON.read_text(encoding="utf-8"))["hooks"]["postToolUse"]
    commands = [h["command"] for h in hooks if "audit.log" in h["command"]]
    assert len(commands) == 1, commands
    return commands[0]


def _wrapper(bindir: Path, name: str, target: str) -> None:
    script = bindir / name
    script.write_text(f'#!{SH}\nexec "{target}" "$@"\n', encoding="utf-8")
    script.chmod(0o755)


def _bin_dir(tmp_path: Path, mode: str) -> Path:
    """A ``PATH`` holding ONLY ``python3``, or only the fallback's ``tr`` and ``date``.

    Python mode carries no ``tr``/``date``, so a hook that fell through to the
    ``printf`` path there would log an empty payload and fail the test.
    """
    bindir = tmp_path / f"bin-{mode}"
    bindir.mkdir(exist_ok=True)
    if mode == "python3":
        _wrapper(bindir, "python3", sys.executable)
        return bindir
    for tool in ("tr", "date"):
        found = shutil.which(tool)
        assert found, f"{tool} not found"
        _wrapper(bindir, tool, found)
    return bindir


def _hook(home: Path, bindir: Path, payload: bytes) -> subprocess.Popen[bytes]:
    assert SH is not None
    env = {**os.environ, "KIROCREW_HOME": str(home), "PATH": str(bindir)}
    proc = subprocess.Popen([SH, "-c", _audit_command()], stdin=subprocess.PIPE, env=env, cwd=home)
    assert proc.stdin is not None
    proc.stdin.write(payload)
    proc.stdin.close()
    return proc


def _run_hook(home: Path, bindir: Path, payload: str) -> None:
    assert _hook(home, bindir, payload.encode("utf-8")).wait(timeout=30) == 0


def test_hook_is_one_line_of_shell() -> None:
    assert "\n" not in _audit_command()


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "payload",
    [
        '{"tool_input":{"command":"ls"}}\n',
        '{"tool_input":{"command":"ls"}}',
        '{\n  "tool_input": {"command": "echo a\\nb"}\n}\r\n',
    ],
    ids=["trailing-newline", "no-newline", "pretty-printed"],
)
def test_one_call_appends_exactly_one_terminated_record(
    tmp_path: Path, payload: str, mode: str
) -> None:
    _run_hook(tmp_path, _bin_dir(tmp_path, mode), payload)

    data = (tmp_path / "audit.log").read_bytes().decode("utf-8")
    assert data.endswith("\n")
    lines = data[:-1].split("\n")
    assert len(lines) == 1, data
    match = RECORD.match(lines[0])
    assert match, lines[0]
    assert json.loads(match.group("payload")) == json.loads(payload)


@pytest.mark.parametrize("mode", MODES)
def test_each_call_is_its_own_line(tmp_path: Path, mode: str) -> None:
    bindir = _bin_dir(tmp_path, mode)
    for n in range(3):
        _run_hook(tmp_path, bindir, json.dumps({"n": n}) + "\n")

    lines = (tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()
    matches = [RECORD.match(line) for line in lines]
    assert all(matches), lines
    assert [json.loads(m.group("payload"))["n"] for m in matches if m] == [0, 1, 2]


def test_large_concurrent_records_do_not_interleave(tmp_path: Path) -> None:
    bindir = _bin_dir(tmp_path, "python3")
    size = 1 << 20  # far past any stdio or pipe buffer
    payloads = {c: json.dumps({"c": c * size}).encode() for c in "abcdefgh"}
    procs = [_hook(tmp_path, bindir, p) for p in payloads.values()]
    assert [p.wait(timeout=60) for p in procs] == [0] * len(procs)

    lines = (tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(payloads)
    seen = set()
    for line in lines:
        match = RECORD.match(line)
        assert match, line[:80]
        value = json.loads(match.group("payload"))["c"]
        assert value == value[0] * size
        seen.add(value[0])
    assert seen == set(payloads)


@pytest.mark.parametrize("mode", MODES)
def test_the_log_is_created_owner_only_under_a_permissive_umask(tmp_path: Path, mode: str) -> None:
    """``audit.log`` records every shell command, so it is created ``0600``.

    Both paths: the ``python3`` one passes ``0o600`` to ``os.open``, and the
    ``printf`` fallback sets ``umask 077`` in the hook's own shell only (that
    shell runs nothing but the one append, so nothing else inherits it).
    """
    bindir = _bin_dir(tmp_path, mode)
    previous = os.umask(0o022)  # the hook's shell inherits it, like kiro-cli's would
    try:
        _run_hook(tmp_path, bindir, '{"tool_input":{"command":"ls"}}\n')
    finally:
        os.umask(previous)
    assert stat.S_IMODE((tmp_path / "audit.log").stat().st_mode) == 0o600
