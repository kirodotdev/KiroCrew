"""How a native skill projection is ended when nobody calls ``release()``.

A dropped projection's finalizer only queues the end, and one daemon worker runs
the queue. These tests pin what that hand-over must still guarantee: the worker
runs without anyone waiting on it, survives an end that fails, is replaced when
an end kills it, and leaves nothing behind at interpreter exit.
"""

from __future__ import annotations

import gc
import json
import queue
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.acp import skill_projection as projection
from kiro_crew.testing.wait import default_timeout, wait_until

#: The tree a probe interpreter projects into, set up the way ``projection_tree``
#: sets up this process: every home the projection resolves points into *root*.
_PROBE_SETUP = """
import gc, json, os, sys, threading
from pathlib import Path
from types import SimpleNamespace
sys.path[:0] = {syspath!r}
root = Path(sys.argv[1])
home, crew, project = root / "kiro", root / "crew", root / "project"
agents = home / "agents"
agents.mkdir(parents=True)
project.mkdir()
source = agents / "custom.json"
source.write_text('{{"name":"custom"}}', encoding="utf-8")
import kiro_crew.agent
from kiro_crew.acp import skill_projection as p
p.kiro_home = lambda: home
p.data_home = lambda: crew
p.kiro_agents_dir = lambda: agents
p.platform_compat.path_volume_is_remote = lambda path: False
p.platform_compat.first_linked_ancestor = lambda path: None
kiro_crew.agent.managed_mcp_spec_entry = lambda name: {{"command": "test-core", "args": []}}
kiro_crew.agent._KIRO_MCP_JSON = home / "settings" / "mcp.json"
p.list_agents = lambda **kw: [SimpleNamespace(name="custom", filename="custom.json", scope="global")]
p._PER_SPAWN_PRUNE_MIN_INTERVAL_SECS = 0.0


def stop_worker():
    raise SystemExit


def leases():
    return sorted(path.name for path in p._projection_lease_dir().iterdir())
"""

#: Bounds a probe's exit: started as the probe's last statement, it kills an
#: exit that has not finished within *budget* seconds and names the hang on
#: stderr, so a drain that hangs fails by name, never at the test's own timeout.
_EXIT_WATCHDOG = """
def hung():
    sys.stderr.write("the exit drain hung\\n")
    sys.stderr.flush()
    os._exit(3)


watchdog = threading.Timer({budget!r}, hung)
watchdog.daemon = True
watchdog.start()
"""

#: Exit with a projection still referenced and one dropped while no worker can
#: run it. ``threading``'s exit hooks run last-registered first, all before
#: ``concurrent.futures`` shuts its pools and before any ``atexit`` hook. The
#: drain runs the queued end; the live projection keeps its lease until the
#: ``atexit`` release. The
#: second argument picks what runs around the module's drain: ``finalize-first``
#: runs the live projection's finalizer ahead of the drain, as a collection
#: racing the exit would, and ``drain-first`` drops a third
#: projection after the drain, which must be queued for the worker like any other. The third argument, ``thread``, makes every
#: resolver answer be read on a pool thread, the only transport Windows has. A
#: watchdog bounds the exit.
_EXIT_PROBE = (
    """
import concurrent.futures.thread
import sys
import threading

late = {{}}


def drop_after_the_drain():
    # Registered before the projection module is imported, so it runs AFTER the
    # module's own drain. The worker is held busy, so a lease released by the
    # time the drop returns was ended inline, on the collecting thread.
    gate = threading.Event()
    p._ensure_ended_projection_worker()
    p._ENDED_PROJECTIONS.put(lambda: gate.wait(30))
    source.write_text('{{"name":"custom","prompt":"v3"}}', encoding="utf-8")
    after = p.prepare_native_skill_projection(project)
    del after
    gc.collect()
    late["leases_after_drop"] = leases()
    gate.set()
    late["settled"] = p._settle_ended_projections(30.0)
    late["leases_after_settle"] = leases()
    late["kept_leases"] = kept_leases
    (root / "late.json").write_text(json.dumps(late), encoding="utf-8")


if sys.argv[2] == "drain-first":
    threading._register_atexit(drop_after_the_drain)
"""
    + _PROBE_SETUP
    + """
if sys.argv[3] == "thread":
    import kiro_crew.subprocess_pool.executor as transport
    transport._CAN_SELECT_PIPES = False
if sys.argv[2] == "finalize-first":
    # Registered after the module's drain, so it runs first.
    threading._register_atexit(lambda: kept._lease_finalizer())
kept = p.prepare_native_skill_projection(project)
kept_leases = leases()
source.write_text('{{"name":"custom","prompt":"v2"}}', encoding="utf-8")
dropped = p.prepare_native_skill_projection(project)
worker = p._ENDED_PROJECTION_WORKER
p._ENDED_PROJECTIONS.put(stop_worker)
worker.join(30)
assert not worker.is_alive()
del dropped
gc.collect()
assert len(leases()) == 4, leases()
"""
    + _EXIT_WATCHDOG
    + """
print(json.dumps({{
    "lease_dir": str(p._projection_lease_dir()),
    "agents": str(agents),
    "kept_view": kept.agent("custom") + ".json",
    "cwd": os.getcwd(),
}}))
"""
)

#: Run after ``_PROBE_SETUP``: with ``thread`` as the probe's second argument,
#: every resolver answer is read on a pool thread, the only transport Windows
#: has, so a prune started once ``concurrent.futures`` has shut its pools fails
#: closed and leaves its views, as it does on Windows.
_POOL_TRANSPORT = """
if sys.argv[2] == "thread":
    import kiro_crew.subprocess_pool.executor as transport
    transport._CAN_SELECT_PIPES = False
"""

#: Exit with an unreleased projection kept alive only by a reference cycle, and
#: a garbage collection that fires while the exit release holds the active-
#: projections lock for its snapshot. The lock is a stand-in that collects once
#: inside its first hold, the few instructions where an allocation in the
#: snapshot could trigger a real collection. The prune is due and the worker is
#: stopped with no replacement able to start, so the release itself has to run
#: the end, and an end run on the
#: collecting thread, or by a release still holding the lock, would take the
#: same lock again. A watchdog bounds the exit: a
#: drain that hangs is killed and named, never left to the test's own timeout.
_EXIT_GC_IN_LOCK_PROBE = (
    _PROBE_SETUP
    + _POOL_TRANSPORT
    + """
import weakref


class CollectsInsideTheLock:
    def __init__(self):
        self._lock = threading.Lock()
        self.collected = None

    def __enter__(self):
        self._lock.acquire()
        if self.collected is None:
            gc.collect()
            self.collected = dropped() is None
            (root / "collected.json").write_text(json.dumps(self.collected), encoding="utf-8")
        return self

    def __exit__(self, *exc):
        self._lock.release()


gc.disable()
prepared = p.prepare_native_skill_projection(project)
cycle = [prepared]
cycle.append(cycle)
dropped = weakref.ref(prepared)
del prepared, cycle
assert dropped() is not None
assert len(leases()) == 2, leases()
# With the worker stopped and no new one able to start, as when the interpreter
# refuses new threads, only the exit release can run the queued end, so it must
# do so outside the lock it held for the snapshot.
worker = p._ENDED_PROJECTION_WORKER
p._ENDED_PROJECTIONS.put(stop_worker)
worker.join(30)
assert not worker.is_alive()
p._ensure_ended_projection_worker = lambda: False
p._ACTIVE_PROJECTIONS_LOCK = CollectsInsideTheLock()
"""
    + _EXIT_WATCHDOG
    + """
print(json.dumps({{"lease_dir": str(p._projection_lease_dir()), "agents": str(agents)}}))
sys.stdout.flush()
"""
)

#: The same exit with no stand-in: the real active-projections lock and a real
#: collection. Garbage collection stays off until an ``atexit`` hook that runs
#: just ahead of the release turns it back on with a threshold of one, so the
#: first container the release allocates, inside its locked snapshot, starts
#: the collection. A weakref callback records whether the lock was held when the
#: projection was collected.
_EXIT_REAL_GC_IN_LOCK_PROBE = (
    _PROBE_SETUP
    + _POOL_TRANSPORT
    + """
import atexit
import weakref

lock = p._ACTIVE_PROJECTIONS_LOCK


def on_collect(ref):
    (root / "collected.json").write_text(json.dumps(lock.locked()), encoding="utf-8")


def collect_at_the_next_allocation():
    # Registered after the module's release, so it runs just before it.
    gc.set_threshold(1)
    gc.enable()


gc.disable()
prepared = p.prepare_native_skill_projection(project)
cycle = [prepared]
cycle.append(cycle)
dropped = weakref.ref(prepared, on_collect)
del prepared, cycle
assert dropped() is not None
assert len(leases()) == 2, leases()
worker = p._ENDED_PROJECTION_WORKER
p._ENDED_PROJECTIONS.put(stop_worker)
worker.join(30)
assert not worker.is_alive()
p._ensure_ended_projection_worker = lambda: False
atexit.register(collect_at_the_next_allocation)
"""
    + _EXIT_WATCHDOG
    + """
print(json.dumps({{"lease_dir": str(p._projection_lease_dir()), "agents": str(agents)}}))
sys.stdout.flush()
"""
)


#: Exit while a non-daemon thread still uses a live projection. The interpreter
#: joins that thread only after ``threading``'s exit hooks have run. A hook
#: registered before the projection module is imported runs after the module's
#: own, and tells the thread the main thread has returned. The thread then
#: records the leases and its view, and ends; the projection stays referenced,
#: so it is still live when the interpreter exits.
_EXIT_NON_DAEMON_PROBE = (
    """
import sys
import threading

main_returned = threading.Event()
threading._register_atexit(main_returned.set)
"""
    + _PROBE_SETUP
    + """
kept = p.prepare_native_skill_projection(project)
view = agents / (kept.agent("custom") + ".json")


def still_using():
    signalled = main_returned.wait(30)
    during = {{"signalled": signalled, "leases": leases(), "view": view.exists()}}
    (root / "during.json").write_text(json.dumps(during), encoding="utf-8")


assert len(leases()) == 2, leases()
threading.Thread(target=still_using, name="still-using-the-projection").start()
"""
    + _EXIT_WATCHDOG
    + """
print(json.dumps({{"lease_dir": str(p._projection_lease_dir()), "view": str(view)}}))
sys.stdout.flush()
"""
)


class _WorkerKilled(BaseException):
    """Raised by a queued end to end the worker thread, the way only a
    ``BaseException`` can."""


def _kill_worker(ended: queue.SimpleQueue, worker: threading.Thread) -> None:
    """End *worker* with a queued end that raises past ``Exception``."""

    def kill():
        raise _WorkerKilled

    def expected(args):
        if args.exc_type is not _WorkerKilled:
            previous(args)

    previous = threading.excepthook
    threading.excepthook = expected
    try:
        ended.put(kill)
        worker.join(default_timeout())
    finally:
        threading.excepthook = previous
    assert not worker.is_alive(), "the worker outlived an end that raised past Exception"


def _point_projection_at(root: Path, monkeypatch) -> tuple[Path, Path]:
    """Point every home the projection resolves into *root*, as a probe does."""
    monkeypatch.delenv("KIROCREW_NATIVE_SKILL_PROJECTION", raising=False)
    home = root / "kiro"
    agents = home / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    project = root / "project"
    project.mkdir(exist_ok=True)
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(projection, "kiro_home", lambda: home)
    monkeypatch.setattr(projection, "data_home", lambda: root / "crew", raising=False)
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(projection.platform_compat, "path_volume_is_remote", lambda path: False)
    monkeypatch.setattr(projection.platform_compat, "first_linked_ancestor", lambda path: None)
    monkeypatch.setattr(
        "kiro_crew.agent.managed_mcp_spec_entry",
        lambda name: {"command": "test-core", "args": []},
    )
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="custom", filename="custom.json", scope="global")],
    )
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", home / "settings" / "mcp.json")
    monkeypatch.setattr(projection, "_PER_SPAWN_PRUNE_MIN_INTERVAL_SECS", 0.0)
    monkeypatch.setattr(projection, "_LAST_PER_SPAWN_PRUNE", {})
    return agents, project


@pytest.fixture
def projection_tree(tmp_path, monkeypatch):
    """Point every home the projection resolves into *tmp_path*."""
    return _point_projection_at(tmp_path, monkeypatch)


@pytest.fixture
def own_end_worker(monkeypatch):
    """A queue and worker of this test's own.

    Whether a worker runs is then down to what this test did, never to a worker
    an earlier test in the same process happened to start.
    """
    ended: queue.SimpleQueue = queue.SimpleQueue()
    monkeypatch.setattr(projection, "_ENDED_PROJECTIONS", ended)
    monkeypatch.setattr(projection, "_ENDED_PROJECTION_WORKER", None)
    yield ended
    worker = projection._ENDED_PROJECTION_WORKER
    if worker is not None and worker.is_alive():
        _kill_worker(ended, worker)


def _leases():
    return sorted(path.name for path in projection._projection_lease_dir().iterdir())


def _probe(
    script: str, tmp_path: Path, *args: str, **fields: object
) -> subprocess.CompletedProcess:
    root = tmp_path / "probe"
    return subprocess.run(
        [sys.executable, "-c", script.format(syspath=sys.path, **fields), str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout() * 6,
        check=False,
        cwd=tmp_path,
    )


def test_a_dropped_projection_is_ended_with_nobody_waiting_for_it(projection_tree, own_end_worker):
    """Nothing in production waits for a dropped projection's end, so the
    worker must already be running by the time the projection can be dropped:
    arming the finalizer starts it."""
    agents, project = projection_tree
    prepared = projection.prepare_native_skill_projection(project)
    alias = agents / f"{prepared.agent('custom')}.json"
    assert _leases()

    del prepared
    gc.collect()

    wait_until(lambda: not _leases(), describe=_leases)
    wait_until(lambda: not alias.exists(), describe=lambda: "the dropped view was never pruned")


def test_an_end_that_fails_does_not_stop_the_ends_queued_after_it(
    projection_tree, own_end_worker, caplog
):
    """One failing end is logged, and the worker goes on to the next."""
    _agents, project = projection_tree
    prepared = projection.prepare_native_skill_projection(project)

    def failing_end():
        raise RuntimeError("this end failed")

    own_end_worker.put(failing_end)
    del prepared
    gc.collect()

    wait_until(lambda: not _leases(), describe=_leases)
    assert "ending a dropped projection failed" in caplog.text


def test_a_worker_an_end_killed_is_replaced_at_the_next_arm(projection_tree, own_end_worker):
    """An end that raises past ``Exception`` ends the worker thread. The next
    projection to arm its finalizer finds the thread dead, not merely present,
    and starts another, so that projection's end still runs."""
    assert projection._ensure_ended_projection_worker()
    _kill_worker(own_end_worker, projection._ENDED_PROJECTION_WORKER)

    _agents, project = projection_tree
    prepared = projection.prepare_native_skill_projection(project)
    del prepared
    gc.collect()

    wait_until(lambda: not _leases(), describe=_leases)


@pytest.mark.parametrize("transport", ["native", "thread"])
@pytest.mark.parametrize("order", ["finalize-first", "drain-first"])
def test_interpreter_exit_ends_every_projection_still_alive_or_queued(tmp_path, order, transport):
    """The worker is a daemon, so at exit nothing would run what is queued.
    The exit drain runs the queued end itself, and so prunes the dropped
    projection's views; a projection dropped after the drain is only queued,
    never ended on the collecting thread, and the worker ends it. A projection
    still referenced was never dropped: it keeps its lease through the drain,
    and the ``atexit`` release drops that lease, leaving its view for the next
    spawn's prune, unless its finalizer already queued its end. No lease record
    is left behind as if the process had crashed. With the ``thread`` transport
    each prune reads its resolver answers on a pool thread, as on Windows, so
    the drain must run while the pools still take work."""
    result = _probe(_EXIT_PROBE, tmp_path, order, transport, budget=default_timeout())
    assert "the exit drain hung" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    paths = json.loads(result.stdout.strip().splitlines()[-1])
    lease_dir, agents = Path(paths["lease_dir"]), Path(paths["agents"])
    assert Path(paths["cwd"]) == tmp_path, "the probe ran outside its own directory"

    assert sorted(path.name for path in lease_dir.iterdir()) == [], "a lease outlived the exit"
    views = sorted(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))
    dropped_views = [view for view in views if view.name != paths["kept_view"]]
    assert dropped_views == [], f"a dropped projection's view outlived the exit: {views}"
    if order == "drain-first":
        late = json.loads((tmp_path / "probe" / "late.json").read_text(encoding="utf-8"))
        kept_leases = late["kept_leases"]
        assert (
            len(late["leases_after_drop"]) == len(kept_leases) + 2
        ), "a drop after the exit drain was ended inline"
        assert late["settled"], "the worker never ran the end of a drop after the exit drain"
        assert (
            late["leases_after_settle"] == kept_leases
        ), "the live projection's lease went before atexit"


def test_a_live_projection_keeps_its_lease_until_the_non_daemon_threads_end(tmp_path):
    """A non-daemon thread can still be driving a session when the main thread
    returns, and the interpreter joins it only after ``threading``'s exit hooks.
    So the projection it uses keeps its lease and view until that thread ends;
    only then, from ``atexit``, is the lease released. The view may stay: as
    after any exit, the next spawn's prune reclaims it."""
    result = _probe(_EXIT_NON_DAEMON_PROBE, tmp_path, budget=default_timeout())
    assert "the exit drain hung" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    during = json.loads((tmp_path / "probe" / "during.json").read_text(encoding="utf-8"))
    assert during["signalled"], "the thread was never told the main thread had returned"
    assert len(during["leases"]) == 2, "the lease went while a non-daemon thread still used it"
    assert during["view"], "the view went while a non-daemon thread still used it"
    paths = json.loads(result.stdout.strip().splitlines()[-1])
    lease_dir = Path(paths["lease_dir"])
    assert sorted(path.name for path in lease_dir.iterdir()) == [], "a lease outlived the exit"


@pytest.mark.parametrize("transport", ["native", "thread"])
@pytest.mark.parametrize(
    "probe",
    [_EXIT_GC_IN_LOCK_PROBE, _EXIT_REAL_GC_IN_LOCK_PROBE],
    ids=["stand-in-lock", "real-collection"],
)
def test_a_collection_inside_the_exit_releases_locked_snapshot_does_not_hang_the_exit(
    tmp_path, monkeypatch, probe, transport
):
    """A dropped projection collected while the exit release holds the
    active-projections lock must only be queued, exactly as at any other time:
    its end prunes, the prune reads the active aliases under that same lock, and
    the lock is not reentrant. The release then ends it itself, outside the lock,
    so the exit finishes and leaves no lease behind. That end runs after the
    ``threading`` exit hooks, once ``concurrent.futures`` has shut its pools, so
    with the ``thread`` transport its prune fails closed and the view stays, as
    any view left at exit does; the next spawn's prune reclaims it. A stand-in
    lock that collects inside its hold pins the case exactly; a real
    collection on the real lock witnesses it with nothing stood in."""
    result = _probe(probe, tmp_path, transport, budget=default_timeout())
    assert "the exit drain hung" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    collected = json.loads((tmp_path / "probe" / "collected.json").read_text(encoding="utf-8"))
    assert collected, "the projection was not collected inside the release's locked snapshot"
    paths = json.loads(result.stdout.strip().splitlines()[-1])
    lease_dir, agents = Path(paths["lease_dir"]), Path(paths["agents"])
    assert sorted(path.name for path in lease_dir.iterdir()) == [], "a lease outlived the exit"
    left = sorted(agents.glob(f"{projection.NATIVE_SKILL_ALIAS_PREFIX}*.json"))

    _agents, project = _point_projection_at(tmp_path / "probe", monkeypatch)
    projection.prepare_native_skill_projection(project).release()
    kept = [view for view in left if view.exists()]
    assert kept == [], f"the next spawn's prune left a view the exit left: {kept}"


@pytest.mark.parametrize("hook", ["present", "missing", "refused"])
def test_the_exit_drain_is_registered_where_the_pools_still_take_work(monkeypatch, hook):
    """``threading``'s exit hook when there is one, ``atexit`` otherwise, and
    ``atexit`` too when ``threading`` is already shutting down."""
    registered: list[str] = []

    def refuse(fn):
        raise RuntimeError("can't register atexit after shutdown")

    if hook == "missing":
        monkeypatch.delattr(threading, "_register_atexit")
    else:
        take = (lambda fn: registered.append("threading")) if hook == "present" else refuse
        monkeypatch.setattr(threading, "_register_atexit", take)
    monkeypatch.setattr(projection.atexit, "register", lambda fn: registered.append("atexit"))

    projection._register_exit_drain()

    assert registered == (["threading"] if hook == "present" else ["atexit"])


def test_the_live_release_is_registered_once_on_atexit(monkeypatch):
    """A plain ``atexit`` hook, so it runs after non-daemon threads are joined,
    and only once however many projections arm their finalizers."""
    registered: list[object] = []
    monkeypatch.setattr(projection, "_LIVE_RELEASE_ARMED", False)
    monkeypatch.setattr(projection.atexit, "register", registered.append)
    monkeypatch.setattr(
        threading,
        "_register_atexit",
        lambda fn: registered.append(("threading", fn)),
        raising=False,
    )

    projection._arm_live_release_at_exit()
    projection._arm_live_release_at_exit()

    assert registered == [projection._release_live_projections_at_exit]
