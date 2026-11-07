"""A sandboxed agent keeps its scratch window when the host empties the stand-in roots.

The launcher masks the scratch root with an empty stand-in and mounts the spawn's own
scratch directory back inside it as a private window. While the agent runs, the host can
delete what sits in the shared stand-in roots: systemd-logind removes ``/run/user/$UID``
at a user's last logout and, with ``RemoveIPC=yes``, deletes the uid's ``/dev/shm``
entries, directories included. A stand-in deleted there takes the window's mount point
along, and the agent's ``$TMPDIR`` is gone.

Each test runs a child under the real sandbox. The child writes into its window, then
waits while the test deletes every entry the launcher could have left under the host
roots for that pid, children first, the way logind does. The child then creates a
directory in its window and reads its earlier file back.

Linux needs unprivileged user and mount namespaces, which the namespace-sandbox CI job
provides and the shard runners do not, so there these tests skip with that reason.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from kiro_crew import sandbox
from kiro_crew.sandbox import userns_available
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

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

own = sys.argv[1]
probe = os.path.join(own, "probe")
with open(probe, "w", encoding="utf-8") as fh:
    fh.write("before")
print("READY %d" % os.getpid(), flush=True)
sys.stdin.readline()

def attempt(step):
    try:
        return {"ok": True, "value": step()}
    except OSError as exc:
        return {"ok": False, "errno": errno.errorcode.get(exc.errno, str(exc.errno))}

def read():
    with open(probe, encoding="utf-8") as fh:
        return fh.read()

report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "mkdir": attempt(lambda: os.mkdir(os.path.join(own, "after")) or True),
    "read": attempt(read),
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


def _delete_like_logind(pid: int) -> list[str]:
    """Remove every entry tagged with *pid* under the host stand-in roots; return them."""
    prefix = "kirocrew_sb_%d_" % pid
    found: list[str] = []
    for root in (f"/run/user/{os.getuid()}", "/dev/shm", tempfile.gettempdir()):
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if not name.startswith(prefix):
                continue
            path = os.path.join(root, name)
            found.append(path)
            if os.path.isdir(path) and not os.path.islink(path):
                for parent, dirs, files in os.walk(path, topdown=False):
                    for leaf in files:
                        os.unlink(os.path.join(parent, leaf))
                    for sub in dirs:
                        os.rmdir(os.path.join(parent, sub))
                os.rmdir(path)
            else:
                os.unlink(path)
    return found


@pytest.mark.parametrize("tier", TIERS)
def test_the_scratch_window_survives_the_host_deleting_the_stand_in_roots(home, tier):
    own = home / "scratch" / "session-aaaa"
    own.mkdir(parents=True)
    argv = [sys.executable, "-c", _CHILD, str(own)]
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    proc = subprocess.Popen(
        sandbox.namespace_argv(argv, tier, extra_private_dirs=(str(own),)),
        cwd=home,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert proc.stdout is not None
        ready = proc.stdout.readline()
        assert ready.startswith("READY "), (
            ready + proc.communicate(timeout=FALLBACK_TIMEOUT_SECS)[1]
        )
        found = _delete_like_logind(int(ready.split()[1]))
        out, err = proc.communicate("go\n", timeout=FALLBACK_TIMEOUT_SECS)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    output = out + err
    assert proc.returncode == 0, output
    (line,) = [ln for ln in out.splitlines() if ln.startswith("REPORT ")]
    report = json.loads(line[len("REPORT ") :])
    # It really ran confined, in a mount namespace of its own.
    assert report["marker"] == "1", output
    assert report["mnt_ns"] != os.readlink("/proc/self/ns/mnt"), output
    # The window still takes writes and still holds what was written before.
    assert report["mkdir"] == {"ok": True, "value": True}, output
    assert report["read"] == {"ok": True, "value": "before"}, output
    assert (own / "after").is_dir()
    # And the launcher left nothing on the host roots for a cleaner to delete.
    assert found == [], output
