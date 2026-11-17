"""The operator's read-only layer, seen from inside the product's own sandbox.

The operator sets ``agent.sandbox_readonly_paths`` to a directory and
``agent.sandbox_readonly_exceptions`` to one inside it. Each test runs a child under the
real namespace sandbox while the test process plays the gateway, and judges what the
child could write by what is on disk afterwards: a write into the read-only directory
fails with ``EROFS`` and leaves nothing behind, a write into the exception lands, and a
write in an unlisted sibling lands too. That last write is the control: without it, a
child that could write nothing at all would pass. The child also reports its mount
namespace, so a run that never entered the sandbox fails instead of passing on the
host's own view.

Linux needs unprivileged user and mount namespaces, which the namespace-sandbox CI job
provides and the shard runners do not, so there these tests skip with that reason. The
planner and launcher halves run everywhere in ``test_sandbox_readonly_layer.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import sandbox
from kiro_crew.config.loader_locations import config_path
from kiro_crew.sandbox import userns_available
from kiro_crew.testing.wait import default_timeout

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only"),
    pytest.mark.skipif(
        not userns_available(),
        reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
        "namespaces)",
    ),
]

TIERS = ("standard", "cc", "strict")

_CHILD = r"""
import errno, json, os, sys

def attempt(path):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("written")
        return "ok"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))

report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "writes": {path: attempt(path) for path in sys.argv[1:]},
}
print("REPORT " + json.dumps(report), flush=True)
"""


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    sandbox.reset_backend()
    yield home
    sandbox.reset_backend()


def _configure(paths: list[Path], exceptions: list[Path]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "agent": {
                    "sandbox_readonly_paths": [str(p) for p in paths],
                    "sandbox_readonly_exceptions": [str(p) for p in exceptions],
                }
            }
        ),
        encoding="utf-8",
    )


def _run_child(home: Path, tier: str, targets: list[Path]) -> dict[str, str]:
    argv = [sys.executable, "-c", _CHILD, *(str(t) for t in targets)]
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    proc = subprocess.run(
        sandbox.namespace_argv(argv, tier),
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    (line,) = [ln for ln in proc.stdout.splitlines() if ln.startswith("REPORT ")]
    report = json.loads(line[len("REPORT ") :])
    assert report["marker"] == "1", output
    assert report["mnt_ns"] != os.readlink("/proc/self/ns/mnt"), output
    return report["writes"]


@pytest.mark.parametrize("tier", TIERS)
def test_the_drive_is_read_only_and_its_documents_stay_writable(home, tmp_path, tier):
    drive = (tmp_path / "drive").resolve()
    docs = drive / "Users" / "me" / "Documents"
    docs.mkdir(parents=True)
    sibling = (tmp_path / "sibling").resolve()
    sibling.mkdir()
    _configure([drive], [docs])

    locked, opened, control = drive / "file", docs / "file", sibling / "file"
    writes = _run_child(home, tier, [locked, opened, control])

    assert writes == {str(locked): "EROFS", str(opened): "ok", str(control): "ok"}
    assert not locked.exists()
    assert opened.read_text(encoding="utf-8") == "written"
    assert control.read_text(encoding="utf-8") == "written"


def test_a_read_only_entry_inside_an_exception_stays_read_only(home, tmp_path):
    """``Documents`` re-opened inside the drive, ``Documents/Taxes`` read-only again."""
    drive = (tmp_path / "drive").resolve()
    docs = drive / "Documents"
    taxes = docs / "Taxes"
    taxes.mkdir(parents=True)
    _configure([drive, taxes], [docs])

    opened, locked = docs / "file", taxes / "file"
    writes = _run_child(home, "standard", [opened, locked])

    assert writes == {str(opened): "ok", str(locked): "EROFS"}
    assert not locked.exists()


def test_each_nested_level_decides_over_the_one_holding_it(home, tmp_path):
    """Read-only X, writable X/A, read-only X/A/B, writable X/A/B/C, read-only X/A/B/C/D."""
    root = (tmp_path / "X").resolve()
    a, b, c, d = root / "A", root / "A" / "B", root / "A" / "B" / "C", root / "A" / "B" / "C" / "D"
    d.mkdir(parents=True)
    _configure([root, b, d], [a, c])

    targets = [p / "file" for p in (root, a, b, c, d)]
    writes = _run_child(home, "standard", targets)

    expected = ["EROFS", "ok", "EROFS", "ok", "EROFS"]
    assert writes == {str(t): e for t, e in zip(targets, expected)}


def test_unset_keys_leave_the_same_directory_writable(home, tmp_path):
    """The control for the whole layer: without the keys, the drive is writable."""
    drive = (tmp_path / "drive").resolve()
    drive.mkdir()
    _configure([], [])

    target = drive / "file"
    assert _run_child(home, "standard", [target]) == {str(target): "ok"}
    assert target.read_text(encoding="utf-8") == "written"


def test_an_exception_cannot_reopen_a_crew_ceiling(home, tmp_path):
    """Read-only home with the data home's own ``profiles`` ceiling as the exception."""
    profiles = home / "profiles"
    profiles.mkdir()
    _configure([home], [profiles])

    target = profiles / "planted.json"
    assert _run_child(home, "standard", [target]) == {str(target): "EROFS"}
    assert not target.exists()


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 0)() == 0, reason="root traverses a mode-000 directory"
)
def test_a_locked_parent_does_not_drop_the_read_only_entry_below_it(home, tmp_path):
    """An agent that ran ``chmod 000`` on a writable parent cannot drop the seal below it.

    The gateway's probe of the entry then fails with ``EACCES``. The plan keeps the entry
    rather than dropping it as "not a directory", and the launcher seals it, so the
    child that restores the parent's mode still cannot write into the directory.
    """
    parent = (tmp_path / "parent").resolve()
    archive = parent / "archive"
    archive.mkdir(parents=True)
    _configure([archive], [])
    target = archive / "file"
    child = "import os, sys\nos.chmod(sys.argv[1], 0o755)\n" + _CHILD.replace(
        "sys.argv[1:]", "sys.argv[2:]"
    )
    argv = [sys.executable, "-c", child, str(parent), str(target)]
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    parent.chmod(0)
    try:
        proc = subprocess.run(
            sandbox.namespace_argv(argv, "standard"),
            cwd=home,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=default_timeout(),
        )
    finally:
        parent.chmod(0o755)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    (line,) = [ln for ln in proc.stdout.splitlines() if ln.startswith("REPORT ")]
    report = json.loads(line[len("REPORT ") :])
    assert report["marker"] == "1", output
    assert report["writes"] == {str(target): "EROFS"}, output
    assert not target.exists()


def test_the_agent_cannot_rename_a_read_only_entry_s_parent(home, tmp_path):
    """A renamed parent would make the next session seal whatever then holds the name.

    The child tries to move the entry's parent aside. The launcher pins that parent, so
    the rename fails with ``EBUSY`` and the entry stays where the next session finds it.
    """
    proj = (tmp_path / "proj").resolve()
    data = proj / "data"
    data.mkdir(parents=True)
    moved = tmp_path / "proj2"
    _configure([data], [])
    child = (
        "import errno, json, os, sys\n"
        "try:\n"
        "    os.rename(sys.argv[1], sys.argv[2])\n"
        "    result = 'ok'\n"
        "except OSError as exc:\n"
        "    result = errno.errorcode.get(exc.errno, str(exc.errno))\n"
        "print('REPORT ' + json.dumps({'rename': result}), flush=True)\n"
    )
    argv = [sys.executable, "-c", child, str(proj), str(moved)]
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    try:
        proc = subprocess.run(
            sandbox.namespace_argv(argv, "standard"),
            cwd=home,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=default_timeout(),
        )
    finally:
        if moved.exists():
            moved.rename(proj)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    (line,) = [ln for ln in proc.stdout.splitlines() if ln.startswith("REPORT ")]
    assert json.loads(line[len("REPORT ") :]) == {"rename": "EBUSY"}, output
    assert data.is_dir()
