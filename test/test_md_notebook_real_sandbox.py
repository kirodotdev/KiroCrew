"""Notes state writes, run inside the product's own Linux sandbox.

The bug this guards only shows in a real mount namespace: the Notes backend's state
writer renamed its temp from one bind mount onto a file in another, and every write
failed with ``EXDEV``. Each test here spawns a child under the real namespace sandbox
while the test process plays the gateway, then judges what the child did by what is on
disk afterwards.

The backend child gets back what the Notes manifest declares (``credentialMasks``:
``"state/"``, through ``app_declared_mask_targets``) and runs the real state writers. An
agent child gets nothing back and must see an empty stand-in. Each
child reports its mount namespace, so a run that never entered the sandbox fails instead
of passing on the host's own view.

Linux needs unprivileged user and mount namespaces, which the namespace-sandbox CI job
provides and the shard runners do not, so there these tests skip with that reason.
"""

from __future__ import annotations

import json
import os
import shutil
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
    # One worker for the module: a held sandbox is a live process on the host's own
    # process table, which the older-sandbox scans of the other cases read.
    pytest.mark.xdist_group("md_notebook_real_sandbox"),
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
    state = home.joinpath(*mns.APP_STATE_PARTS)

    # The gateway's gate before the backend spawns: both moves, with no older sandbox alive.
    assert mns.sealed_default_legacy_state() is True
    report = _run(
        [sys.executable, "-c", _BACKEND],
        home,
        extra_visible_dirs=sandbox.app_declared_mask_targets("md-notebook", builtin=True),
    )

    # The old PAT went through both moves before the child started, and the child saw it.
    assert report["read_moved_pat"] == {"ok": True, "value": "ghp_from_the_old_layout"}, report
    for step in ("pat", "vaults", "settings"):
        assert report[step] == {"ok": True, "value": None}, report
    assert report["read_back"] == {"ok": True, "value": "ghp_written_inside"}, report
    # And the writes reached the gateway's own files.
    assert (state / "pat").read_text() == "ghp_written_inside"
    assert json.loads((state / "vaults.json").read_text()) == [{"id": "v1"}]
    assert json.loads((state / "settings.json").read_text()) == {"autoSync": False}
    assert (legacy / "pat").read_bytes() == b"", "the moved name holds only its stub"
    assert (home / mns.STATE_DIR_LEAF / "pat").read_bytes() == b"", "so does the middle one"
    assert not [p for p in state.iterdir() if p.suffix == ".tmp"], "a temp was left behind"


def test_an_agent_sees_an_empty_state_directory(home):
    # Already moved, so spawn prep leaves it alone.
    staging = home / mns.STATE_DIR_LEAF
    staging.mkdir()
    (staging / mns.MIGRATION_MARKER).write_text("{}\n")
    (staging / mns.APP_MOVE_MARKER).write_text("{}\n")
    state = home.joinpath(*mns.APP_STATE_PARTS)
    state.mkdir(parents=True)
    (state / "pat").write_text("ghp_secret")

    report = _run([sys.executable, "-c", _AGENT, str(state)], home)

    assert report["listing"] == {"ok": True, "value": []}, report
    assert report["read"] == {"ok": False, "errno": "ENOENT"}, report
    assert (state / "pat").read_text() == "ghp_secret"


# Waits on stdin, so the test decides when it ends; reports once it is confined.
# With "undumpable" it first hands its ``/proc`` entry to root, the way a process trying
# not to be counted would. With "leader-exited" its main thread exits and a second
# thread holds on, so the process's own ``/proc`` entry is a zombie with no mount view;
# it reports once the kernel shows the leader as one.
_HOLDER = r"""
import ctypes, os, sys, threading
ns = os.readlink("/proc/self/ns/mnt")
if sys.argv[1:] == ["undumpable"]:
    assert ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE
if sys.argv[1:] == ["leader-exited"]:
    def hold():
        leader = "/proc/%d/stat" % os.getpid()
        for _ in range(10_000_000):
            if open(leader).read().rsplit(")", 1)[1].split()[0] == "Z":
                break
            os.sched_yield()
        else:
            print("the leader never exited", flush=True)
            os._exit(1)
        print("READY " + ns, flush=True)
        sys.stdin.read()
        os._exit(0)
    threading.Thread(target=hold).start()
    ctypes.CDLL(None).pthread_exit(None)
print("READY " + ns, flush=True)
sys.stdin.read()
"""


def _start_holder(home: Path, *args: str, **kwargs) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    proc = subprocess.Popen(
        sandbox.namespace_argv([sys.executable, "-c", _HOLDER, *args], "standard", **kwargs),
        cwd=home,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert line.startswith("READY "), line
        assert line.split()[1] != os.readlink("/proc/self/ns/mnt"), "it never entered the sandbox"
    except BaseException:
        _reap(proc)
        raise
    return proc


def _reap(proc: subprocess.Popen) -> None:
    """End *proc* on any path: its stdin closes, then it is killed if still running."""
    if proc.stdin is not None and not proc.stdin.closed:
        proc.stdin.close()
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=60)


def _stop(proc: subprocess.Popen) -> None:
    assert proc.stdin is not None
    proc.stdin.close()
    try:
        proc.wait(timeout=60)
    finally:
        _reap(proc)


@pytest.mark.parametrize(
    "shape", [[], ["undumpable"], ["leader-exited"]], ids=["plain", "undumpable", "leader-exited"]
)
def test_the_move_waits_for_a_sandbox_that_masks_only_the_old_dir(home, shape):
    """The kernel's own record of a sandbox's mounts decides. A sandbox that masks
    ``md-notebook-staging/`` and not ``data/state/`` (what a build before the app's own
    directory spawned; here, a spawn handed that directory back) holds the move, and one
    of this build, which masks both, does not."""
    staging = home / mns.STATE_DIR_LEAF
    staging.mkdir()
    (staging / mns.MIGRATION_MARKER).write_text("{}\n")
    (staging / "pat").write_text("ghp_still_here")

    agent = _start_holder(home)
    try:
        assert mns.older_sandboxes_masking_only_the_old_dir() == []
    finally:
        _stop(agent)

    older = _start_holder(
        home,
        *shape,
        extra_visible_dirs=sandbox.app_declared_mask_targets("md-notebook", builtin=True),
    )
    try:
        assert mns.older_sandboxes_masking_only_the_old_dir(), "the older-shaped sandbox"
        assert mns.sealed_default_legacy_state() is False
        assert (staging / "pat").read_text() == "ghp_still_here"
    finally:
        _stop(older)

    assert mns.older_sandboxes_masking_only_the_old_dir() == []
    assert mns.sealed_default_legacy_state() is True
    assert (home.joinpath(*mns.APP_STATE_PARTS) / "pat").read_text() == "ghp_still_here"


# Reads each path once the test writes a line, and reports what it saw.
_READER = r"""
import json, os, sys
print("READY " + os.readlink("/proc/self/ns/mnt"), flush=True)
sys.stdin.readline()
seen = {}
for path in sys.argv[1:]:
    try:
        seen[path] = {"read": open(path, encoding="utf-8").read()}
    except OSError as exc:
        seen[path] = {"errno": exc.errno}
print(json.dumps(seen), flush=True)
"""


@pytest.mark.parametrize("state_at_spawn", [True, False], ids=["state-kept", "state-not-yet"])
def test_a_sandbox_started_while_the_data_dir_is_moved_aside_keeps_the_mask_after_it_returns(
    home, state_at_spawn
):
    """The lifecycle moves ``data/`` to ``apps/.<app>-data-tmp/``, replaces or deletes the
    app's tree, and moves it back. A sandbox started in between finds only the move-aside
    spelling, so its mask lands on ``state/`` there (spawn prep creates it when the tree
    has none yet); the mount stays on that directory through the rename back, so a token
    kept there, or written there later by the backend, is out of its sight at the
    ``data/`` spelling."""
    app = home / "apps" / "md-notebook"
    aside = home / "apps" / ".md-notebook-data-tmp"
    aside.mkdir(parents=True)
    if state_at_spawn:
        (aside / "state").mkdir()
        (aside / "state" / "pat").write_text("ghp_kept_on_uninstall")
    live_pat = app / "data" / "state" / "pat"

    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    proc = subprocess.Popen(
        sandbox.namespace_argv(
            [sys.executable, "-c", _READER, str(live_pat), str(aside / "state" / "pat")],
            "standard",
        ),
        cwd=home,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert proc.stdout is not None and proc.stdin is not None
        line = proc.stdout.readline()
        assert line.startswith("READY "), line
        assert line.split()[1] != os.readlink("/proc/self/ns/mnt"), "it never entered the sandbox"

        # The uninstall's restore: the app's tree is deleted, its directory made, data/ back.
        shutil.rmtree(app, ignore_errors=True)
        app.mkdir(parents=True, exist_ok=True)
        os.rename(aside, app / "data")
        if not state_at_spawn:
            # The backend, enabled after the update, writes its first token.
            live_pat.parent.mkdir(exist_ok=True)
            live_pat.write_text("ghp_kept_on_uninstall")
        assert live_pat.read_text() == "ghp_kept_on_uninstall"

        proc.stdin.write("go\n")
        proc.stdin.flush()
        seen = json.loads(proc.stdout.readline())
    finally:
        _reap(proc)
    assert "read" not in seen[str(live_pat)], seen
    assert "read" not in seen[str(aside / "state" / "pat")], seen
