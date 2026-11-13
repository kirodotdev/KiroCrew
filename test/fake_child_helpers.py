"""A real, tiny child process for tests that need to see what a spawn handed over.

``write_recording_child`` writes a Python script under the test's ``tmp_path``.
Run by the code under test, the script appends one JSON line per run to a record
file -- its argv, cwd, pid, session and process-group ids, the names of its
environment variables and the values of the ones asked for -- and exits with the
chosen code. The test then reads the record with ``read_child_records``.

The child never forks, never reads stdin and never touches the network, so it
exits on its own as soon as it has written its line. ``reap_recorded_children``
is still called from a ``finally``: it SIGKILLs the group of any recorded child
that is somehow still alive, and only when that child leads its own group, so a
child that shares the test's group can never take pytest down with it.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import platform_compat

_CHILD_SOURCE = """\
import json, os, sys
record = {{
    "argv": sys.argv,
    "cwd": os.getcwd(),
    "pid": os.getpid(),
    "sid": os.getsid(0) if hasattr(os, "getsid") else None,
    "pgid": os.getpgid(0) if hasattr(os, "getpgid") else None,
    "env_keys": sorted(os.environ),
    "env": {{key: os.environ.get(key) for key in {env_values!r}}},
}}
with open({record!r}, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\\n")
sys.stdout.write({stdout!r})
sys.stdout.flush()
sys.exit({exit_code!r})
"""


@dataclass(frozen=True)
class RecordingChild:
    """Where the child lives and where it writes."""

    script: Path
    record: Path

    @property
    def argv(self) -> list[str]:
        """The command that runs the child with this interpreter."""
        return [sys.executable, str(self.script)]


def write_recording_child(
    directory: Path,
    *,
    name: str = "child",
    exit_code: int = 0,
    stdout: str = "",
    env_values: tuple[str, ...] = (),
) -> RecordingChild:
    """Write the child script into *directory* and return its paths."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / f"{name}.py"
    record = directory / f"{name}.record.jsonl"
    script.write_text(
        _CHILD_SOURCE.format(
            record=str(record),
            stdout=stdout,
            exit_code=exit_code,
            env_values=tuple(env_values),
        ),
        encoding="utf-8",
    )
    return RecordingChild(script=script, record=record)


def write_child_launcher(child: RecordingChild, name: str = "launcher") -> Path:
    """A file that runs *child* when executed directly, for a code path that
    takes ONE executable path (no interpreter argv in front of it)."""
    if platform_compat.IS_WINDOWS:
        launcher = child.script.parent / f"{name}.cmd"
        launcher.write_text(f'@"{sys.executable}" "{child.script}" %*\r\n', encoding="utf-8")
        return launcher
    launcher = child.script.parent / name
    launcher.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{child.script}" "$@"\n', encoding="utf-8"
    )
    launcher.chmod(0o755)
    return launcher


def read_child_records(child: RecordingChild) -> list[dict]:
    """Every run's record, oldest first; empty when the child never ran."""
    if not child.record.exists():
        return []
    lines = child.record.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def reap_recorded_children(child: RecordingChild) -> None:
    """SIGKILL the group of any recorded child still alive that leads its own group."""
    if not platform_compat.IS_POSIX:
        return
    for record in read_child_records(child):
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 1 or record.get("pgid") != pid:
            continue
        if platform_compat.pid_liveness(pid) == platform_compat.PID_DEAD:
            continue
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
