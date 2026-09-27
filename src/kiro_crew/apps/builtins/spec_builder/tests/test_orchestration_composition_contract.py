"""Composition contract for the Spec Builder backend owners.

The backend is composed from a route facade (``handlers``), the private
``orchestration`` owners, the worker-session binding (``runtime``) and the stores
underneath them (``decisions``, ``repository``, ``parsers``). The route tests reach
every one of them through one facade that fans a patch out to each module holding
the patched name, so several properties only exist for the composition as a
whole. This file pins those: one object per shared name, a layered and acyclic
import graph, one facade hop for route entry points, and the global lock order
exercised end to end over HTTP.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import sys
import threading
import types
from pathlib import Path
from types import ModuleType

import pytest

from kiro_crew.apps.builtins.spec_builder import backend as backend_package
from kiro_crew.apps.builtins.spec_builder.backend import handlers
from kiro_crew.apps.builtins.spec_builder.backend import routes as composition
from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import BACKEND_MODULES, routes
from kiro_crew.apps.builtins.spec_builder.tests.test_routes import (
    _BASE,
    _decision_spec,
    _make_client,
    _redirect_state,
    _slot_stub,
)

_PACKAGE = backend_package.__name__
_BACKEND_DIR = Path(backend_package.__file__).resolve().parent

#: Lower layers never import higher ones. Orchestration owners share one layer and
#: may import each other only acyclically; the route composition sits on top.
_LAYERS: dict[str, int] = {
    "parsers": 0,
    "repository": 1,
    "decisions": 2,
    "runtime": 3,
    "orchestration": 4,
    "handlers": 5,
    "routes": 6,
}


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """Every test here writes only under its own temporary app state."""
    _redirect_state(monkeypatch, tmp_path / "_contract_state")


def _backend_sources() -> dict[str, Path]:
    """Dotted module name -> source file for every module in the backend package."""
    out: dict[str, Path] = {}
    for path in sorted(_BACKEND_DIR.rglob("*.py")):
        rel = path.relative_to(_BACKEND_DIR).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        out[".".join([_PACKAGE, *parts])] = path
    return out


def _layer(module_name: str) -> int:
    relative = module_name[len(_PACKAGE) + 1 :] if module_name != _PACKAGE else ""
    head = relative.split(".", 1)[0]
    return _LAYERS[head]


def _intra_package_imports(module_name: str, path: Path) -> list[tuple[str, str, int]]:
    """``(target module, imported name or "", line)`` for every backend import."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    is_package = path.name == "__init__.py"
    package_parts = module_name.split(".") if is_package else module_name.split(".")[:-1]
    edges: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level == 0:
            continue
        base = package_parts[: len(package_parts) - (node.level - 1)]
        target = ".".join([*base, *([node.module] if node.module else [])])
        for alias in node.names:
            submodule = f"{target}.{alias.name}"
            if node.module is None or submodule in _backend_sources():
                edges.append((submodule, "", node.lineno))
            else:
                edges.append((target, alias.name, node.lineno))
    return edges


def test_shared_names_bind_one_object_across_backend_modules():
    """The facade restores a patched name by writing its saved value into EVERY
    module holding that name. Two modules binding one name to different objects
    would be silently unified by the first test that patches it, and a patch aimed
    at one seam would leave the other copy live. Every shared name is one object."""
    holders: dict[str, list[ModuleType]] = {}
    for module in BACKEND_MODULES:
        for name in vars(module):
            if not name.startswith("__"):
                holders.setdefault(name, []).append(module)
    divergent = {
        name: sorted(module.__name__.rsplit(".", 1)[-1] for module in modules)
        for name, modules in holders.items()
        if len(modules) > 1 and len({id(vars(module)[name]) for module in modules}) > 1
    }
    assert not divergent, f"names bound to different objects: {divergent}"


def test_backend_import_graph_is_layered_and_acyclic():
    """Dependencies point down the stack and never loop.

    Stores never import the runtime, the runtime never imports an orchestration
    owner, and no owner imports the route facade. A cycle or an upward edge would
    make the owners' import order load-bearing and let a facade become a hidden
    dependency of the code it re-exports."""
    sources = _backend_sources()
    graph: dict[str, set[str]] = {name: set() for name in sources}
    upward: list[str] = []
    for module_name, path in sources.items():
        for target, _name, line in _intra_package_imports(module_name, path):
            if target not in sources or target == module_name:
                continue
            graph[module_name].add(target)
            if _layer(target) > _layer(module_name):
                upward.append(f"{module_name}:{line} -> {target}")
    assert not upward, f"imports point up the stack: {upward}"

    visiting: set[str] = set()
    done: set[str] = set()

    def _visit(node: str, trail: list[str]) -> None:
        if node in done:
            return
        assert node not in visiting, f"import cycle: {' -> '.join([*trail, node])}"
        visiting.add(node)
        for child in sorted(graph[node]):
            _visit(child, [*trail, node])
        visiting.discard(node)
        done.add(node)

    for node in sorted(graph):
        _visit(node, [])


def test_backend_imports_name_the_defining_module():
    """Facade depth one: a backend function or class is imported from the module
    that defines it. The one exception is the declared route facade: the route
    composition imports its entry points from ``handlers``, and ``handlers`` must
    in turn import each of them straight from its owner -- never a relay of a
    relay, so no hidden second facade can grow behind the first."""
    facade = f"{_PACKAGE}.handlers"
    sources = _backend_sources()
    handler_imports = {
        name: target
        for target, name, _line in _intra_package_imports(facade, sources[facade])
        if name
    }
    relayed: list[str] = []
    for module_name, path in sources.items():
        for target, name, line in _intra_package_imports(module_name, path):
            if not name or target not in sources:
                continue
            obj = getattr(sys.modules[target], name)
            owner = getattr(obj, "__module__", None)
            if not (inspect.isfunction(obj) or inspect.isclass(obj)):
                continue
            if not (isinstance(owner, str) and owner.startswith(_PACKAGE)):
                continue
            if owner == target:
                continue
            if target == facade and handler_imports.get(name) == owner:
                continue
            relayed.append(f"{module_name}:{line} imports {name} from {target}, owner {owner}")
    assert not relayed, f"re-export chains: {relayed}"


def test_every_route_handler_resolves_to_one_canonical_owner():
    """Each registered route entry point is the same object through the route
    composition, the handler facade and its defining module."""
    tree = ast.parse(inspect.getsource(composition))
    entry_points = [
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "handlers"
        for alias in node.names
    ]
    assert len(entry_points) == 18, entry_points
    for name in entry_points:
        entry = getattr(handlers, name)
        owner = sys.modules[entry.__module__]
        assert getattr(owner, name) is entry, name
        assert getattr(composition, name) is entry, name
        assert getattr(routes, name) is entry, name


class _OrderedLock:
    """A threading lock that records the order locks are taken in, per thread."""

    def __init__(self, rank: int, name: str, ledger: "_LockLedger") -> None:
        self._lock = threading.Lock()
        self._rank = rank
        self._name = name
        self._ledger = ledger

    def __enter__(self) -> "_OrderedLock":
        self._ledger.enter(self._rank, self._name)
        self._lock.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()
        self._ledger.exit()


class _LockLedger:
    def __init__(self, loop_thread: int) -> None:
        self._held: dict[int, list[tuple[int, str]]] = {}
        self._guard = threading.Lock()
        self._loop_thread = loop_thread
        self.inversions: list[str] = []
        self.on_loop: list[str] = []
        self.nested = 0
        self.acquired: dict[str, int] = {}

    def enter(self, rank: int, name: str) -> None:
        me = threading.get_ident()
        with self._guard:
            stack = self._held.setdefault(me, [])
            if any(held_rank >= rank for held_rank, _ in stack):
                self.inversions.append(f"{name} taken while holding {[n for _, n in stack]}")
            if stack:
                self.nested += 1
            if me == self._loop_thread:
                self.on_loop.append(name)
            self.acquired[name] = self.acquired.get(name, 0) + 1
            stack.append((rank, name))

    def exit(self) -> None:
        with self._guard:
            self._held[threading.get_ident()].pop()


async def _drain(state) -> None:
    """Wait (bounded) for the settlement tasks a delivered answer schedules."""
    for _ in range(200):
        pending = [task for task in state._background_tasks if not task.done()]
        if not pending:
            return
        await asyncio.wait(pending, timeout=0.05)
    raise AssertionError("settlement tasks never finished")


@pytest.mark.asyncio
async def test_index_lock_precedes_decision_lock_across_a_full_spec_lifecycle(
    tmp_path, monkeypatch
):
    """Global lock order, exercised end to end rather than read off the source.

    Detail, decision answer, relay, consumption, finalization and delete each take
    the index lock and the decision lock from a different owner. The order is
    index then decisions everywhere, both are only ever held on worker threads (a
    hold on the event loop would stall every other request), and the nested
    acquisitions actually happen -- so the check cannot pass vacuously."""
    client = _make_client(monkeypatch, tmp_path)
    ledger = _LockLedger(threading.get_ident())
    monkeypatch.setattr(routes, "_INDEX_LOCK", _OrderedLock(1, "index", ledger))
    monkeypatch.setattr(routes, "_DECISIONS_LOCK", _OrderedLock(2, "decisions", ledger))
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: None)
    spec_dir, slot_key = _decision_spec(tmp_path)

    dispatched: list[str] = []

    def _dispatch(*args, **kwargs):
        dispatched.append(args[2])
        kwargs["on_consumed"](True)

    monkeypatch.setattr(routes, "_dispatch_turn", _dispatch)
    state, _slot = _slot_stub()
    client.app["state"] = state
    await client.start_server()
    try:
        detail = await client.get(f"{_BASE}/specs/s")
        assert detail.status == 200, await detail.text()
        answer = await client.post(
            f"{_BASE}/specs/s/message",
            json={
                "text": "Decision — Inbound transport: HTTPS",
                "spec_dir": spec_dir,
                "slot_key": slot_key,
                "decision_id": "transport",
                "decision_option": "HTTPS",
            },
        )
        assert answer.status == 200, await answer.text()
        await _drain(state)
        entries = routes._decision_entries(routes._read_decisions()[0], spec_dir)
        assert [entry["status"] for entry in entries.values()] == ["final"], entries
        deleted = await client.delete(
            f"{_BASE}/specs/s", params={"spec_dir": spec_dir, "slot_key": slot_key}
        )
        assert deleted.status == 200, await deleted.text()
    finally:
        await client.close()

    assert len(dispatched) == 1
    assert routes._decision_entries(routes._read_decisions()[0], spec_dir) == {}
    assert not ledger.inversions, ledger.inversions
    assert not ledger.on_loop, f"locks held on the event loop: {ledger.on_loop}"
    assert ledger.nested >= 3, "the index-then-decisions path was never exercised"
    assert ledger.acquired.get("decisions", 0) >= 5, ledger.acquired


def _ledger_statuses(spec_dir: str) -> list[tuple[str, str]]:
    store, _usable = routes._read_decisions()
    return [
        (entry["option"], entry["status"])
        for entry in routes._decision_entries(store, spec_dir).values()
    ]


def test_concurrent_claims_on_one_decision_record_exactly_one_answer(tmp_path):
    """The claim is one transaction across threads, not merely under the turn lock.

    Eight worker threads claim the same decision with different options at once;
    exactly one records, every other claim is told which answer the agent has, and
    the protected ledger holds a single pending outbox row for the winner."""
    spec_dir, slot_key = _decision_spec(tmp_path)
    options = [f"option-{index}" for index in range(8)]
    barrier = threading.Barrier(len(options))
    outcomes: dict[str, tuple[str, str]] = {}
    guard = threading.Lock()

    def _claim(option: str) -> None:
        barrier.wait(timeout=10)
        result = routes._claim_decision_locked(
            "s", "transport", option, spec_dir, slot_key, "fp", f"prompt {option}", f"d-{option}"
        )
        with guard:
            outcomes[option] = result

    threads = [threading.Thread(target=_claim, args=(option,)) for option in options]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads), "a claim deadlocked"

    winners = [option for option, (outcome, _held) in outcomes.items() if outcome == "recorded"]
    assert len(winners) == 1, outcomes
    losers = {option: result for option, result in outcomes.items() if option != winners[0]}
    assert all(result == ("already_answered", winners[0]) for result in losers.values()), losers
    assert _ledger_statuses(spec_dir) == [(winners[0], "pending")]


def test_a_claim_holds_the_ledger_lock_from_its_read_to_its_write(tmp_path, monkeypatch):
    """A claim writes back the whole ledger it read, so no other writer may land in
    between: a relay or finalization of another row committed inside that window
    would be silently reverted by the claim's save.

    The claim is parked inside its window (at the directory self-check it runs
    between read and write) until a concurrent finalization has asked for the
    ledger lock; both rows must survive."""
    spec_dir, slot_key = _decision_spec(tmp_path)
    assert routes._claim_decision_locked(
        "s", "storage", "SQLite", spec_dir, slot_key, "fp2", "prompt", "d2"
    ) == ("recorded", "")
    real_lock = threading.Lock()
    in_window = threading.Event()
    requested = threading.Event()
    release = threading.Event()
    finalizers: list[threading.Thread] = []

    class _SignallingLock:
        def __enter__(self) -> "_SignallingLock":
            if threading.current_thread() in finalizers:
                requested.set()
            real_lock.acquire()
            return self

        def __exit__(self, *_exc: object) -> None:
            real_lock.release()

    real_verify = routes._verified_spec_dir

    def _verify_inside_the_window(path):
        in_window.set()
        assert release.wait(timeout=10), "the claim was never released"
        return real_verify(path)

    monkeypatch.setattr(routes, "_DECISIONS_LOCK", _SignallingLock())
    monkeypatch.setattr(routes, "_verified_spec_dir", _verify_inside_the_window)
    claimed: list[tuple[str, str]] = []
    claimer = threading.Thread(
        target=lambda: claimed.append(
            routes._claim_decision_locked(
                "s", "transport", "HTTPS", spec_dir, slot_key, "fp", "prompt", "d1"
            )
        )
    )
    finalizer = threading.Thread(
        target=routes._finalize_decision_locked, args=(spec_dir, "storage", "fp2", "d2")
    )
    finalizers.append(finalizer)
    claimer.start()
    assert in_window.wait(timeout=10)
    finalizer.start()
    assert requested.wait(timeout=10)
    release.set()
    claimer.join(timeout=10)
    finalizer.join(timeout=10)
    assert not claimer.is_alive() and not finalizer.is_alive()

    assert claimed == [("recorded", "")]
    assert sorted(_ledger_statuses(spec_dir)) == [("HTTPS", "pending"), ("SQLite", "final")]


@pytest.mark.asyncio
async def test_outbox_rows_move_one_way_and_a_final_answer_is_never_demoted(
    tmp_path, monkeypatch, caplog
):
    """The pending -> relayed -> final outbox, transition by transition.

    Relay and finalize are idempotent, a restore only undoes a relay, abandoning
    touches only an unconsumed pending row, a final row can never move back, a row
    is addressed only by its exact delivery, and a failed write reports failure
    without changing what is on disk."""
    spec_dir, slot_key = _decision_spec(tmp_path)
    outcome, _held = await routes._claim_decision(
        "s",
        "transport",
        "HTTPS",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint="fp",
        message="Decision: HTTPS",
        delivery_id="d1",
    )
    assert outcome == "recorded"
    row = (spec_dir, "transport", "fp", "d1")

    assert await routes._restore_decision_pending(*row) is True
    assert _ledger_statuses(spec_dir) == [("HTTPS", "pending")]
    assert await routes._mark_decision_relayed(*row) is True
    assert await routes._mark_decision_relayed(*row) is True
    assert _ledger_statuses(spec_dir) == [("HTTPS", "relayed")]
    assert await routes._abandon_pending_decision(*row) is True
    assert _ledger_statuses(spec_dir) == [("HTTPS", "relayed")], "a relayed row was abandoned"
    assert await routes._restore_decision_pending(*row) is True
    assert _ledger_statuses(spec_dir) == [("HTTPS", "pending")]
    assert await routes._mark_decision_relayed(*row) is True
    assert await routes._finalize_decision(*row) is True
    assert await routes._finalize_decision(*row) is True
    assert _ledger_statuses(spec_dir) == [("HTTPS", "final")]
    assert await routes._restore_decision_pending(*row) is False
    assert await routes._mark_decision_relayed(*row) is True
    assert _ledger_statuses(spec_dir) == [("HTTPS", "final")], "a final answer moved back"

    other_delivery = (spec_dir, "transport", "fp", "d-other")
    for transition in (
        routes._mark_decision_relayed,
        routes._restore_decision_pending,
        routes._finalize_decision,
    ):
        assert await transition(*other_delivery) is False, transition.__name__

    outcome, _held = await routes._claim_decision(
        "s",
        "storage",
        "SQLite",
        expect_spec_dir=spec_dir,
        expect_slot_key=slot_key,
        fingerprint="fp2",
        message="Decision: SQLite",
        delivery_id="d2",
    )
    assert outcome == "recorded"
    second = (spec_dir, "storage", "fp2", "d2")
    before = routes._decisions_path().read_bytes()

    def _unwritable(_store):
        raise OSError("read-only data home")

    monkeypatch.setattr(routes, "_save_decisions", _unwritable)
    caplog.set_level("WARNING", logger="kirocrew.app.spec-builder")
    assert await routes._mark_decision_relayed(*second) is False
    assert await routes._finalize_decision(*second) is False
    assert routes._decisions_path().read_bytes() == before
    messages = [record.getMessage() for record in caplog.records]
    assert f"could not mark decision delivery relayed for {spec_dir}" in messages
    assert f"could not finalize decision delivery for {spec_dir}" in messages


def _two_slot_creation(tmp_path, monkeypatch):
    """Spec ``s`` whose Stop/Delete barrier captures its own slot and a second one.

    The second slot is what an index rewrite leaves behind: a worker still running
    under an earlier slot key for the same creation, reachable only through the
    barrier's durable-loop scan."""
    spec_dir, slot_key = _decision_spec(tmp_path)
    extra_key = f"{slot_key}-earlier"
    slots = {
        slot_key: types.SimpleNamespace(key=slot_key, running=False, _app="spec-builder"),
        extra_key: types.SimpleNamespace(key=extra_key, running=False, _app="spec-builder"),
    }
    loops = {extra_key: "loop-earlier", slot_key: "loop-own"}
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: None)
    monkeypatch.setattr(routes, "_exec_loop_id_for_slot", lambda key: loops.get(key))
    monkeypatch.setattr(routes, "_matching_execution_loops", lambda *_a, **_k: dict(loops))
    state = types.SimpleNamespace(get_slot=slots.get, _background_tasks=set())
    return spec_dir, slot_key, extra_key, state


@pytest.mark.asyncio
async def test_stop_halts_every_captured_slot_once_and_every_other_captured_loop(
    tmp_path, monkeypatch
):
    """Stop acts on the whole creation the barrier captured.

    Captured slots come in capture order -- the creation's own slot, observed on
    this directory, first. That first slot is the one ``_halt_execution`` stops
    together with the spec's own loop; every other captured slot has its turn
    halted once, and every other captured loop is removed by its own slot key and
    pinned id."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key, extra_key, state = _two_slot_creation(tmp_path, monkeypatch)
    halted: list[tuple[str | None, str]] = []
    paused: list[str] = []
    removed: list[tuple[str, str | None, str]] = []

    async def _halt_execution(_state, _name, _spec_dir, *, only_loop_id, only_slot, **_kw):
        halted.append((only_loop_id, only_slot.key))

    async def _halt_active_turn(_state, _name, *, only_slot):
        paused.append(only_slot.key)
        return True

    async def _remove(key, *, only_loop_id=None, stop_reason=""):
        removed.append((key, only_loop_id, stop_reason))

    monkeypatch.setattr(routes, "_halt_execution", _halt_execution)
    monkeypatch.setattr(routes, "_halt_active_turn", _halt_active_turn)
    monkeypatch.setattr(routes, "_remove_nudge_loop_for_slot", _remove)
    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s/stop", json={"spec_dir": spec_dir, "slot_key": slot_key}
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200, payload
    assert payload == {"ok": True, "status": "planning"}
    assert halted == [("loop-own", slot_key)]
    assert paused == [extra_key]
    assert removed == [(extra_key, "loop-earlier", "spec_stopped")]


@pytest.mark.asyncio
async def test_delete_tears_down_every_captured_slot_once_and_every_captured_loop(
    tmp_path, monkeypatch
):
    """Delete archives each captured slot exactly once, in capture order, and removes
    the spec's own loop by name and every other captured loop by slot key, all
    before the entry leaves the index."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key, extra_key, state = _two_slot_creation(tmp_path, monkeypatch)
    order: list[tuple] = []

    async def _remove_by_name(name, *, only_loop_id=None, stop_reason=""):
        order.append(("loop-by-name", name, only_loop_id, stop_reason))

    async def _remove(key, *, only_loop_id=None, stop_reason=""):
        order.append(("loop-by-slot", key, only_loop_id, stop_reason))

    async def _teardown(_state, _name, *, only_slot, require_archive):
        assert "s" in routes._load_index(), "the entry left the index before its teardown"
        order.append(("teardown", only_slot.key, require_archive))
        return True

    monkeypatch.setattr(routes, "_remove_nudge_loop", _remove_by_name)
    monkeypatch.setattr(routes, "_remove_nudge_loop_for_slot", _remove)
    monkeypatch.setattr(routes, "_teardown_worker_slot", _teardown)
    client.app["state"] = state
    await client.start_server()
    try:
        resp = await client.delete(
            f"{_BASE}/specs/s", params={"spec_dir": spec_dir, "slot_key": slot_key}
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200, payload
    assert order == [
        ("loop-by-name", "s", "loop-own", "spec_deleted"),
        ("loop-by-slot", extra_key, "loop-earlier", "spec_deleted"),
        ("teardown", slot_key, True),
        ("teardown", extra_key, True),
    ]
    assert "s" not in routes._load_index()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage_result", "identity", "status", "code"),
    [
        ("unsupported_platform", None, 501, "doc_write_unsupported"),
        ("write_failed", None, 400, "doc_write_failed"),
        ("", None, 400, "doc_write_failed"),
    ],
)
async def test_a_duplicate_that_cannot_stage_reserves_nothing(
    tmp_path, monkeypatch, stage_result, identity, status, code
):
    """A copy whose stage cannot be created or verified never reaches the index.

    The refusal is audited as a failed duplicate, an unverifiable stage has its
    marker removed, and the response names the platform limit apart from an
    ordinary write failure."""
    client = _make_client(monkeypatch, tmp_path)
    spec_dir, slot_key = _decision_spec(tmp_path)
    (Path(spec_dir) / "requirements.md").write_text("# Requirements\n", encoding="utf-8")
    audits: list[tuple[str, str, str]] = []
    marker_removals: list[Path] = []
    monkeypatch.setattr(
        routes,
        "_audit",
        lambda operation, resources="", outcome="success": audits.append(
            (operation, resources, outcome)
        ),
    )
    monkeypatch.setattr(routes, "_create_duplicate_stage", lambda _stage, _token: stage_result)
    monkeypatch.setattr(routes, "_duplicate_stage_identity", lambda _stage, _token: identity)
    monkeypatch.setattr(
        routes,
        "_remove_duplicate_marker",
        lambda stage, _token, _identity=None: marker_removals.append(stage),
    )
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **_k: dispatched.append(a[2]))
    client.app["state"] = _slot_stub()[0]
    await client.start_server()
    try:
        resp = await client.post(
            f"{_BASE}/specs/s/duplicate",
            json={"new_name": "copy", "spec_dir": spec_dir, "slot_key": slot_key},
        )
        payload = await resp.json()
    finally:
        await client.close()

    assert resp.status == status, payload
    assert payload["code"] == code
    assert ("spec_duplicate_failed", "s -> copy", "failure") in audits
    assert "copy" not in routes._load_index()
    assert dispatched == []
    if stage_result:
        assert marker_removals == [], "a stage that was never created had a marker removed"
    else:
        assert [stage.name for stage in marker_removals] == [
            marker_removals[0].name
        ] and marker_removals[0].name.startswith(".copy.duplicate-")


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["no_pending_row", "stopping", "slot_running", "spec_moved"])
async def test_a_refused_crash_replay_leaves_the_durable_row_pending(
    tmp_path, monkeypatch, refusal
):
    """Recovery is a no-op, never a loss, whenever it cannot own the turn.

    With nothing to replay, with a Stop in progress for the creation, with the
    agent already mid-turn, or with the spec moved during the replay's own
    re-pin, the relay does not start: nothing is dispatched and the outbox row
    keeps its pending status for a later recovery."""
    spec_dir, slot_key = _decision_spec(tmp_path)
    if refusal != "no_pending_row":
        assert routes._claim_decision_locked(
            "s", "transport", "HTTPS", spec_dir, slot_key, "fp", "Decision: HTTPS", "d1"
        ) == ("recorded", "")
    state, slot = _slot_stub()
    if refusal == "stopping":
        monkeypatch.setitem(routes._EXECUTION_STOPS, "s", 1)
    if refusal == "slot_running":
        monkeypatch.setattr(slot, "running", True, raising=False)
    if refusal == "spec_moved":

        async def _moved(*_args, **_kwargs):
            return None

        monkeypatch.setattr(routes, "_touch_spec", _moved)
    dispatched: list[str] = []
    monkeypatch.setattr(routes, "_dispatch_turn", lambda *a, **_k: dispatched.append(a[2]))
    meta = routes._load_index()["s"]

    replay = asyncio.create_task(routes._replay_pending_decision(state, slot, "s", meta))
    assert await asyncio.wait_for(replay, timeout=10) is False
    assert dispatched == []
    expected = [] if refusal == "no_pending_row" else [("HTTPS", "pending")]
    assert _ledger_statuses(spec_dir) == expected
    # A claim is owned by the request task that reserved it and must not outlive it.
    routes._prune_finished_pending_dispatch_claims()
    assert routes._PENDING_DISPATCH_CLAIMS == {}, "a refused replay kept its dispatch claim"
