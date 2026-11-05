"""Every loop reader that decides from run liveness to stop work is named here.

Between the claimer's ``admitted -> starting`` write and its registration of
the run (``claim_and_start``), a row has no ``_agents`` record: its only loop
record is ``_starting_claims``. A stopper that asks ``_agents`` alone whether a
run is live misses that row, answers "nothing running", and the claimer then
registers a run nobody stops.

So each function that reads run liveness (``_agents`` or a helper over it) and
stops work either consults the in-flight starts (``_starting_claims`` itself,
``starting_claim_of``, ``starting_claim_ids_for`` or ``_live_agent_ids``), or is
named in ``EXEMPT`` with why a row whose start is in flight is not its to stop.
A new one fails here and its author decides which it is. The unconditional
store cancels are pinned separately (``test_taskq_unconditional_cancel_callers``).
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path
from typing import Iterator

SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

#: Attributes that read whether a run is live in this process.
LIVENESS = frozenset({"_agents", "_live_agent_ids", "_live_run_ids", "_registered_run"})

#: Calls that stop a run, a waiting row, or a run's tasks.
STOPS = frozenset(
    {
        "cancel",
        "cancel_tree",
        "_force_reap",
        "_stop_queued",
        "_schedule_cancel",
        "_cancel_live_or_row",
        "_cancel_children_db",
        "_cancel_children_apply",
        "_cancel_task_intentionally",
        "_end_orphan_process",
        "cancel_for_parent",
        "cancel_for_teardown",
    }
)

#: Names through which a reader sees a row whose start is in flight.
STARTING = frozenset(
    {"_starting_claims", "starting_claim_of", "starting_claim_ids_for", "_live_agent_ids"}
)

#: ``(path under src/kiro_crew, qualname)`` of the readers that consult them.
CONSULTS: dict[tuple[str, str], str] = {
    ("shared_crew/admission_entry.py", "subagent_cancel.stop_child"): (
        "stops an admitted child through manager.cancel when starting_claim_of names its run"
    ),
    ("subagent_manager/cancellation.py", "CancellationCoordinator.cancel_impl"): (
        "a single stop joins the start (starting_claim_of), then reaps what registered"
    ),
    ("subagent_manager/cancellation.py", "CancellationCoordinator.cancel_for_parent_impl"): (
        "Stop all's running sweep takes starting_claim_ids_for and stops each through cancel"
    ),
    ("subagent_manager/admission/waits.py", "_WaitsMixin._cancel_live_or_row"): (
        "failed-sibling cascade: a row in _live_agent_ids goes to cancel"
    ),
    ("subagent_manager/admission/waits.py", "_WaitsMixin.taskq_cancel_children_of"): (
        "parent-cancel cascade: live_ids from _live_agent_ids go to cancel"
    ),
    ("subagent_manager/admission/waits.py", "_WaitsMixin.taskq_cancel_children_of_async"): (
        "parent-cancel cascade: live_ids from _live_agent_ids go to cancel"
    ),
}

#: Readers that stop work but never a row whose start is in flight, with why.
EXEMPT: dict[tuple[str, str], str] = {
    ("subagent_manager/admission/pump.py", "_PumpMixin.retry_retained_claims"): (
        "the claimer itself: it reads _agents to see whether its own retry registered"
    ),
    ("subagent_manager/admission/waits.py", "_WaitsMixin.taskq_expire_waits_apply"): (
        "stops runs whose wait deadline passed; only a running parent enters a wait"
    ),
    (
        "subagent_manager/cancellation.py",
        "CancellationCoordinator.snapshot_teardown_children_impl",
    ): (
        "a parent end leaves a claimed row to its claimer (allow_admitted=False); "
        "its task cancels are follow-up watchers"
    ),
    ("subagent_manager/cancellation.py", "CancellationCoordinator.cancel_for_teardown_impl"): (
        "a parent end leaves a claimed row to its claimer (allow_admitted=False)"
    ),
    ("subagent_manager/cancellation.py", "CancellationCoordinator.cancel_all_impl"): (
        "shutdown: a start left in flight leaves a starting row for the boot reconcile"
    ),
    ("subagent_manager/monitoring.py", "OrphanStallMonitor._reaper_loop_impl"): (
        "reaps registered runs past their own deadline; an unregistered row has none"
    ),
    ("subagent_manager/monitoring.py", "OrphanStallMonitor._reconcile_orphans_impl"): (
        "ends a prior gateway run's recorded process; it cancels no store row"
    ),
    ("subagent_manager/monitoring.py", "OrphanStallMonitor._end_leaked_processes_impl"): (
        "ends a prior gateway run's leaked process; it cancels no store row"
    ),
}

#: The helpers ``STARTING`` names, each of which must read the registry itself.
HELPERS: dict[tuple[str, str], frozenset[str]] = {
    ("subagent_manager/admission/pump.py", "_PumpMixin.starting_claim_of"): frozenset(
        {"_starting_claims"}
    ),
    ("subagent_manager/admission/pump.py", "_PumpMixin.starting_claim_ids_for"): frozenset(
        {"_starting_claim_parents"}
    ),
    ("subagent_manager/admission/waits.py", "_WaitsMixin._live_agent_ids"): frozenset(
        {"_starting_claims"}
    ),
}

_Func = ast.FunctionDef | ast.AsyncFunctionDef


def _functions(node: ast.AST, scope: tuple[str, ...] = ()) -> Iterator[tuple[str, _Func]]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield ".".join((*scope, child.name)), child
            yield from _functions(child, (*scope, child.name))
        elif isinstance(child, ast.ClassDef):
            yield from _functions(child, (*scope, child.name))
        else:
            yield from _functions(child, scope)


def _own_nodes(func: _Func) -> Iterator[ast.AST]:
    """The nodes of *func*'s body, without those of a nested def or lambda."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _names(nodes: list[ast.AST]) -> set[str]:
    """Attribute names and string constants: ``__dict__.get("_starting_claims")``
    reads the registry through a string."""
    out = {n.attr for n in nodes if isinstance(n, ast.Attribute)}
    out |= {n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    return out


def _stops(nodes: list[ast.AST]) -> bool:
    for node in nodes:
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in STOPS:
            continue
        if node.func.attr == "cancel":
            # ``SubagentManager.cancel``, not ``asyncio.Task.cancel``.
            receiver = ast.unparse(node.func.value)
            if receiver not in ("self", "manager", "mgr") and not receiver.endswith("_manager"):
                continue
        return True
    return False


@functools.cache
def _scan() -> tuple[dict[tuple[str, str], set[str]], dict[tuple[str, str], set[str]]]:
    """``(stopping readers, every function)`` -> the names each one reads.

    Read once per process: the tree does not change under the test."""
    readers: dict[tuple[str, str], set[str]] = {}
    everything: dict[tuple[str, str], set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if not any(name in text for name in (*LIVENESS, "_starting_claim")):
            continue
        tree = ast.parse(text, filename=str(path))
        rel = path.relative_to(SRC).as_posix()
        for qualname, func in _functions(tree):
            nodes = list(_own_nodes(func))
            names = _names(nodes)
            everything[(rel, qualname)] = names
            if names & LIVENESS and _stops(nodes):
                readers[(rel, qualname)] = names
    return readers, everything


def test_every_stopping_liveness_reader_is_accounted_for() -> None:
    readers, _ = _scan()
    unknown = sorted(set(readers) - set(CONSULTS) - set(EXEMPT))
    assert not unknown, (
        "a function reads run liveness and stops work, but is not named: a row whose "
        "start is in flight has no _agents record, so a stop decided from _agents alone "
        "answers 'nothing running' while the claimer registers the run. Consult "
        "starting_claim_of / starting_claim_ids_for / _live_agent_ids and add it to "
        "CONSULTS, or add it to EXEMPT with why such a row is not its to stop: "
        + "; ".join(f"{p}:{q}" for p, q in unknown)
    )


def test_every_named_reader_still_reads_liveness_and_stops() -> None:
    readers, _ = _scan()
    gone = sorted((set(CONSULTS) | set(EXEMPT)) - set(readers))
    assert not gone, f"named readers that no longer read liveness and stop work: {gone}"
    assert not set(CONSULTS) & set(EXEMPT)


def test_each_consulting_reader_still_consults_the_starts_in_flight() -> None:
    readers, _ = _scan()
    lost = sorted(key for key in CONSULTS if key in readers and not readers[key] & STARTING)
    assert not lost, (
        f"readers listed as consulting _starting_claims no longer name any of {sorted(STARTING)}: "
        f"{lost}"
    )


def test_each_helper_still_reads_the_registry() -> None:
    _, everything = _scan()
    lost = sorted(key for key, needed in HELPERS.items() if not everything.get(key, set()) & needed)
    assert not lost, f"helpers that no longer read the in-flight start registry: {lost}"
