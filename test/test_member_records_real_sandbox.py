"""Crewmate records, read and written from inside the product's own Linux sandbox.

Each test spawns a child under the real namespace sandbox while the test process plays
the gateway, then judges what the child could do and what reached the gateway's files.
The child reports its mount namespace and the launcher's marker, so a run that never
entered the sandbox fails instead of passing on the host's own view.

Linux needs unprivileged user and mount namespaces, which the namespace-sandbox CI job
provides and the shard runners do not, so there these tests skip with that reason.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import member_goals, member_records, members, sandbox
from kiro_crew.sandbox import userns_available

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only"),
    pytest.mark.skipif(
        not userns_available(),
        reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
        "namespaces)",
    ),
]

# The agent's side: try to read, then overwrite, every path it is handed.
_AGENT = r"""
import errno, json, os, sys

def attempt(step):
    try:
        return {"ok": True, "value": step()}
    except OSError as exc:
        return {"ok": False, "errno": errno.errorcode.get(exc.errno, str(exc.errno))}

def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()

def write(path):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"member": "Atlas", "rules": "rewritten"}')

report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "paths": {p: {"read": attempt(lambda p=p: read(p)), "write": attempt(lambda p=p: write(p))}
              for p in sys.argv[1:]},
}
print("REPORT " + json.dumps(report), flush=True)
"""

_SLUG = "atlas"
_MEMBER = "Atlas"


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    # The sandbox plans its masks from the resolved data home, so it is resolved here
    # too; ``$HOME`` is pinned so no step of the launch touches a real data home.
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    sandbox.reset_backend()
    yield home
    sandbox.reset_backend()


def _write_all_three() -> list[Path]:
    """The gateway's own writers, so the paths are the ones production uses."""
    members.write_member_rules(_SLUG, member=_MEMBER, text="never push to main")
    member_goals.set_goal(_SLUG, _MEMBER, "ship the release", by="user")
    members.write_dm_binding(_SLUG, member=_MEMBER, slot_key=members.member_slot_key(_SLUG, ""))
    return [
        members.member_rules_path(_SLUG),
        member_goals.member_goals_path(_SLUG),
        members.dm_binding_path(_SLUG),
    ]


def _run(mode: str, home: Path, paths: list[Path]) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    proc = subprocess.run(
        sandbox.namespace_argv([sys.executable, "-c", _AGENT, *map(str, paths)], mode),
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    (line,) = [ln for ln in proc.stdout.splitlines() if ln.startswith("REPORT ")]
    report = json.loads(line[len("REPORT ") :])
    assert report["marker"] == "1", output
    assert report["mnt_ns"] != os.readlink("/proc/self/ns/mnt"), output
    return report


@pytest.mark.parametrize("mode", ["standard", "strict"])
def test_a_sandboxed_process_can_neither_read_nor_change_the_records(home, mode):
    paths = _write_all_three()
    before = {p: p.read_bytes() for p in paths}

    report = _run(mode, home, paths)

    for path in paths:
        seen = report["paths"][str(path)]
        assert seen["read"]["ok"] is False, (mode, path, seen)
        # The write may land in the mask's own empty stand-in; it must never reach
        # the gateway's file.
        assert path.read_bytes() == before[path], (mode, path, seen)


@pytest.mark.parametrize("mode", ["standard", "strict"])
def test_records_left_in_the_old_layout_are_moved_before_the_child_starts(home, mode):
    paths = _write_all_three()
    contents = {p.parent.name: p.read_bytes() for p in paths}
    # Put the bytes back where an older build kept them, and forget the move.
    root = home / member_records.MEMBER_RECORDS_LEAF
    for kind, data in contents.items():
        old = home / "trust" / kind
        old.mkdir(parents=True, exist_ok=True)
        (old / f"{_SLUG}.json").write_bytes(data)
        (root / kind / f"{_SLUG}.json").unlink()
    (root / member_records.MIGRATION_MARKER).unlink()
    old_paths = [home / "trust" / kind / f"{_SLUG}.json" for kind in contents]

    report = _run(mode, home, [*old_paths, *paths])

    # Spawn prep moved every record before the child ran, so the child finds none
    # at either name, and the gateway's copies hold the original bytes.
    for path in [*old_paths, *paths]:
        assert report["paths"][str(path)]["read"]["ok"] is False, (mode, path, report)
    for path in paths:
        assert path.read_bytes() == contents[path.parent.name]
