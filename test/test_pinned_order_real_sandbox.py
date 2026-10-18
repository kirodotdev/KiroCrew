"""The pinned-session order store, seen from inside the product's own sandbox.

Only the gateway reads and writes ``pinned-order/``: the order is the person's own, so
the Linux namespace sandbox hides that directory from every agent it confines. Each
test here runs a child under that real sandbox while the test process plays the
gateway, then judges what the child did by what is on disk afterwards.

The child lists and reads the order file, writes a forged order over it, and then
writes the same file name in an unfenced sibling directory. The fence holds when the
child saw no stored order, its forged order never reached the gateway's file, and the
sibling write did land. That last write is the control: without it, a child that could
write nothing at all would pass. The child also reports its mount namespace, so a run
that never entered the sandbox fails instead of passing on the host's own view.

Linux needs unprivileged user and mount namespaces, which the namespace-sandbox CI job
provides and the shard runners do not, so there these tests skip with that reason. The
gateway's own half -- the order it stored is the one it serves after a restart -- is in
``test_pinned_session_order.py``, which runs everywhere.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import sandbox
from kiro_crew.dashboard import pinned_session_order as pso
from kiro_crew.sandbox import userns_available

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only"),
    pytest.mark.skipif(
        not userns_available(),
        reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
        "namespaces)",
    ),
]

TIERS = ("standard", "cc", "strict")

# The agent's side. Every step reports its own outcome instead of stopping at the
# first error, so one run shows the whole view. A write goes through a sibling temp
# and a rename, the way the gateway's own atomic write publishes the file.
_CHILD = r"""
import errno, json, os, sys

fence_file, control_file, forged = sys.argv[1:4]

def attempt(step):
    try:
        return {"ok": True, "value": step()}
    except OSError as exc:
        return {"ok": False, "errno": errno.errorcode.get(exc.errno, str(exc.errno))}

def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()

def write(path):
    temp = path + ".forged"
    with open(temp, "w", encoding="utf-8") as fh:
        fh.write(forged)
    os.replace(temp, path)
    return True

report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "listing": attempt(lambda: sorted(os.listdir(os.path.dirname(fence_file)))),
    "read": attempt(lambda: read(fence_file)),
    "write": attempt(lambda: write(fence_file)),
    "control": attempt(lambda: write(control_file)),
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
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    sandbox.reset_backend()
    yield home
    sandbox.reset_backend()


def _run_agent(home: Path, tier: str, control: Path, forged: str) -> dict:
    """Run the agent's side under the real *tier* sandbox; return what it saw."""
    store = pso.store_path(home)
    argv = [sys.executable, "-c", _CHILD, str(store), str(control), forged]
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    proc = subprocess.run(
        sandbox.namespace_argv(argv, tier),
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
    # The control: the same child, the same write, one directory over, landed.
    assert report["control"] == {"ok": True, "value": True}, output
    assert control.read_text(encoding="utf-8") == forged
    return report


@pytest.mark.parametrize("tier", TIERS)
def test_a_sandboxed_agent_cannot_read_or_rewrite_the_stored_order(home, tier):
    store = pso.store_path(home)
    pso.save(store, ["slot-a", "slot-b"])
    saved = store.read_bytes()
    control = home / "pinned-order-control" / pso.PINNED_ORDER_FILE
    control.parent.mkdir()
    forged = json.dumps(["slot-forged"])

    report = _run_agent(home, tier, control, forged)

    # Inside, the directory is an empty stand-in: there is no stored order to read.
    assert report["listing"] == {"ok": True, "value": []}, report
    assert report["read"] == {"ok": False, "errno": "ENOENT"}, report
    # Whatever the forged write did inside, it never reached the gateway's file.
    assert store.read_bytes() == saved
    assert sorted(p.name for p in store.parent.iterdir()) == [pso.PINNED_ORDER_FILE]
    assert pso.load_stored(store) == (["slot-a", "slot-b"], True)


@pytest.mark.parametrize("tier", TIERS)
def test_a_sandbox_started_before_the_first_reorder_cannot_plant_one(home, tier):
    """The store does not exist until the first reorder; an agent spawned before it
    must not be able to create the order the gateway would load at its next start."""
    store = pso.store_path(home)
    assert not store.parent.exists()
    control = home / "pinned-order-control" / pso.PINNED_ORDER_FILE
    control.parent.mkdir()
    forged = json.dumps(["slot-forged"])

    report = _run_agent(home, tier, control, forged)

    assert report["read"] == {"ok": False, "errno": "ENOENT"}, report
    # The spawn made the directory so the mask had a target; the plant stayed inside.
    assert store.parent.is_dir()
    assert list(store.parent.iterdir()) == []
    assert pso.load_stored(store) == ([], False)
    # And the gateway's own first write still lands in the directory the spawn made.
    pso.save(store, ["slot-a"])
    assert pso.load_stored(store) == (["slot-a"], True)
