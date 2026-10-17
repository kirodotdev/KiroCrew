"""A child that drives a real durable store, parks at a named failpoint, and waits to die.

The task store restart tests (``test/test_taskq_real_crash.py``) prove restart
properties against the PRODUCTION writers, so the process that owns the store
has to be a real process that a real ``SIGKILL`` ends. This child opens the
named store, drives its public functions to the failpoint named on the command
line, announces the failpoint, and then blocks forever.

The announcement is a TERMINAL marker: the exact value ``"<store>:<failpoint>"``
written to the marker file in one call. The parent waits for that exact value
rather than for the file to exist, so a partly written marker can never let the
kill land while the child is still setting up.

Usage: crash_child.py <store> <failpoint> <marker-path>

Everything else arrives in the environment the parent declares, never inherited:

  CRASH_DIR    directory that holds the store under test
  CRASH_CLOCK  the virtual clock base; the child never reads wall time
  CRASH_TASK   task id, default ``t1``
  CRASH_CLASS  side-effect class, default ``unknown``
  CRASH_GATE   a file the parent holds locked until every racer is ready
  CRASH_WHO    this racer's name
  CRASH_IDS    how many rows a racer claims

Stores and failpoints:

  taskq     claimed, running, result-written, claim-race
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path


def _park(marker: Path, value: str) -> None:
    """Announce the failpoint, then wait to be killed.

    The value is written in one call so a reader cannot see half of it.
    """
    marker.write_text(value, encoding="utf-8")
    while True:  # only SIGKILL ends this
        time.sleep(3600)


def _announce(marker: Path, value: str) -> None:
    """Announce a point the child passes through and then continues from."""
    marker.write_text(value, encoding="utf-8")


def _taskq_store():
    from kiro_crew.taskq.store import TaskStore

    base = float(os.environ["CRASH_CLOCK"])
    return TaskStore(
        Path(os.environ["CRASH_DIR"]) / "tasks.db", clock=lambda: base, network_fs=False
    ).open()


def _taskq_record(task_id: str):
    from kiro_crew.taskq import model

    return model.TaskRecord(
        id=task_id,
        kind=model.KIND_SUBAGENT,
        params={"task": task_id},
        side_effect_class=os.environ.get("CRASH_CLASS", model.SIDE_EFFECT_UNKNOWN),
    )


def _taskq(failpoint: str, marker: Path) -> None:
    from kiro_crew.taskq import model

    if failpoint in ("claimed", "running", "result-written"):
        store = _taskq_store()
        task_id = os.environ.get("CRASH_TASK", "t1")
        store.insert_if_absent(_taskq_record(task_id))
        claim = store.claim(task_id)
        if claim is None:
            raise SystemExit(f"the child could not claim {task_id!r}")
        if failpoint != "claimed":
            store.transition(task_id, model.STARTING, generation=claim.generation)
            store.transition(task_id, model.RUNNING, generation=claim.generation)
        if failpoint == "result-written":
            (Path(os.environ["CRASH_DIR"]) / "result.json").write_text("{}", encoding="utf-8")
        _park(marker, f"taskq:{failpoint}")
    elif failpoint == "claim-race":
        _claim_race(marker)
    else:
        raise SystemExit(f"unknown taskq failpoint {failpoint!r}")


def _claim_race(marker: Path) -> None:
    """Open the store, say so, wait on the parent's lock, then claim every row."""
    store = _taskq_store()
    who = os.environ["CRASH_WHO"]
    count = int(os.environ["CRASH_IDS"])
    _announce(marker, "taskq:claim-race")
    with open(os.environ["CRASH_GATE"], "rb") as gate:
        fcntl.flock(gate, fcntl.LOCK_SH)  # blocks until the parent lets go
        won = []
        for i in range(count):
            got = store.claim(f"r{i}", owner=who)
            if got is not None:
                won.append(got.record.id)
    out = Path(os.environ["CRASH_DIR"]) / f"won-{who}.json"
    out.write_text(json.dumps(won), encoding="utf-8")
    store.close()


def main() -> int:
    store, failpoint, marker_path = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    handlers = {"taskq": _taskq}
    handler = handlers.get(store)
    if handler is None:  # a typo in a test; the fail-fast test exercises it on purpose
        raise SystemExit(f"unknown store {store!r}")
    handler(failpoint, marker_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
