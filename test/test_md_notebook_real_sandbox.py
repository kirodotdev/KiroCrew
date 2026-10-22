"""Notes state writes, run inside the product's own Linux sandbox.

The bug this guards only shows in a real mount namespace: the Notes backend's state
writer renamed its temp from one bind mount onto a file in another, and every write
failed with ``EXDEV``. Each test here spawns a child under the real namespace sandbox
while the test process plays the gateway, then judges what the child did by what is on
disk afterwards.

The backend child gets the Notes carve-out (``app_backend_visible_targets``) and runs the
real state writers. An agent child gets no carve-out and must see an empty stand-in. Each
child reports its mount namespace, so a run that never entered the sandbox fails instead
of passing on the host's own view.

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

from kiro_crew import md_notebook_state as mns
from kiro_crew import sandbox
from kiro_crew.sandbox import userns_available

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only"),
    pytest.mark.skipif(
        not userns_available(),
        reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
        "namespaces)",
    ),
]

# The backend's side: the real state writers, each step reporting its own outcome.
_BACKEND = r"""
import errno, json, os, sys

from kiro_crew.apps.builtins.md_notebook import server_store as store

def attempt(step):
    try:
        return {"ok": True, "value": step()}
    except OSError as exc:
        return {"ok": False, "errno": errno.errorcode.get(exc.errno, str(exc.errno))}

report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "read_moved_pat": attempt(store._read_pat_sync),
    "pat": attempt(lambda: store._write_pat_sync("ghp_written_inside")),
    "vaults": attempt(lambda: store._write_vaults_sync([{"id": "v1"}])),
    "settings": attempt(lambda: store._write_settings_sync({"autoSync": False})),
    "read_back": attempt(store._read_pat_sync),
}
print("REPORT " + json.dumps(report), flush=True)
"""

# The agent's side: no carve-out, so the state directory must be an empty stand-in.
_AGENT = r"""
import errno, json, os, sys

state_dir = sys.argv[1]

def attempt(step):
    try:
        return {"ok": True, "value": step()}
    except OSError as exc:
        return {"ok": False, "errno": errno.errorcode.get(exc.errno, str(exc.errno))}

def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()

report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "listing": attempt(lambda: sorted(os.listdir(state_dir))),
    "read": attempt(lambda: read(os.path.join(state_dir, "pat"))),
}
print("REPORT " + json.dumps(report), flush=True)
"""


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    # The sandbox plans its masks from the resolved data home, so the home is resolved
    # here too, and every path the tests compare is spelled the way the plan spells it.
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    # Spawn prep also sweeps the ``$HOME``-joined crew homes, so ``$HOME`` is pinned too:
    # no step of the launch may touch the developer's real data home.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    sandbox.reset_backend()
    yield home
    sandbox.reset_backend()


def _run(argv: list[str], home: Path, **kwargs) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    proc = subprocess.run(
        sandbox.namespace_argv(argv, "standard", **kwargs),
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
    # It really ran confined: the launcher marked it, in a mount namespace of its own.
    assert report["marker"] == "1", output
    assert report["mnt_ns"] != os.readlink("/proc/self/ns/mnt"), output
    return report


def test_the_backend_writes_every_state_file_inside_the_sandbox(home):
    """The EXDEV repro: the old layout failed every one of these writes."""
    legacy = home.joinpath(*mns.LEGACY_DIR_PARTS)
    legacy.mkdir(parents=True)
    (legacy / "pat").write_text("ghp_from_the_old_layout")
    state = home / mns.STATE_DIR_LEAF

    report = _run(
        [sys.executable, "-c", _BACKEND],
        home,
        extra_visible_dirs=sandbox.app_backend_visible_targets("md-notebook"),
    )

    # Spawn prep moved the old PAT in before the child started, and the child saw it.
    assert report["read_moved_pat"] == {"ok": True, "value": "ghp_from_the_old_layout"}, report
    for step in ("pat", "vaults", "settings"):
        assert report[step] == {"ok": True, "value": None}, report
    assert report["read_back"] == {"ok": True, "value": "ghp_written_inside"}, report
    # And the writes reached the gateway's own files.
    assert (state / "pat").read_text() == "ghp_written_inside"
    assert json.loads((state / "vaults.json").read_text()) == [{"id": "v1"}]
    assert json.loads((state / "settings.json").read_text()) == {"autoSync": False}
    assert (legacy / "pat").read_bytes() == b"", "the moved name holds only its stub"
    assert not [p for p in state.iterdir() if p.suffix == ".tmp"], "a temp was left behind"


def test_an_agent_sees_an_empty_state_directory(home):
    state = home / mns.STATE_DIR_LEAF
    state.mkdir()
    (state / "pat").write_text("ghp_secret")

    report = _run([sys.executable, "-c", _AGENT, str(state)], home)

    assert report["listing"] == {"ok": True, "value": []}, report
    assert report["read"] == {"ok": False, "errno": "ENOENT"}, report
    assert (state / "pat").read_text() == "ghp_secret"
