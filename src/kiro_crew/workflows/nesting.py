"""Depth and fan-out caps for workflow runs started from inside other runs.

A run started from a session that belongs to no run is depth 1. A run started
from one of run R's worker sessions, from a dashboard session that R opened
(``bind_session``), from a session that ``run_of_session`` maps to R (a task's
step sessions), or from a sub-agent whose spawn chain leads back to any of
them, is depth ``depth(R) + 1`` and counts against R's fan-out:
the number of runs R has started that are still in flight. Admission refuses a
start past either cap with an error. Without the caps one run can start runs
that start runs, and the cascade outlives the run that began it.

Lineage lives in process memory. Runs do not survive a gateway restart, so
nothing in flight needs it afterwards. A parent run the ledger does not know
counts as depth 1, so a run started from it is depth 2.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from kiro_crew.config.sections_defaults import (
    WORKFLOW_MAX_DEPTH_CEILING,
    WORKFLOW_MAX_DEPTH_DEFAULT,
    WORKFLOW_MAX_NESTED_RUNS_CEILING,
    WORKFLOW_MAX_NESTED_RUNS_DEFAULT,
)

logger = logging.getLogger(__name__)

#: The key prefixes a run's worker sessions sit under: ``<prefix>:<run_id>:...``.
WORKER_KEY_PREFIXES = ("wf", "wf-pool", "wf-unpooled", "wf-worker")

# Lineage also reads a run's authoring session (``wf-author:<run_id>:a<n>``),
# which an intent start opens; the admission binding above leaves it out.
_RUN_KEY_PREFIXES = WORKER_KEY_PREFIXES + ("wf-author",)

DEFAULT_MAX_DEPTH = WORKFLOW_MAX_DEPTH_DEFAULT
MAX_DEPTH_CEILING = WORKFLOW_MAX_DEPTH_CEILING
DEFAULT_MAX_NESTED_RUNS = WORKFLOW_MAX_NESTED_RUNS_DEFAULT
MAX_NESTED_RUNS_CEILING = WORKFLOW_MAX_NESTED_RUNS_CEILING

# Bound on the sub-agent hops walked from a caller to its run, so a cycle in
# recorded parents cannot hang admission. Sub-agent depth is itself capped
# far below this.
_MAX_LINEAGE_HOPS = 64

_SUBAGENT_PREFIX = "subagent:"

ERROR_CODE_DEPTH = "workflow_nesting_depth"
ERROR_CODE_FANOUT = "workflow_nested_runs"


def run_id_of_worker_key(session_key: str) -> str:
    """The run whose worker or author *session_key* is, or ``""`` when it is neither."""
    prefix, sep, rest = session_key.partition(":")
    if not sep or prefix not in _RUN_KEY_PREFIXES:
        return ""
    return rest.split(":", 1)[0]


def _clamp(value: object, default: int, ceiling: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(max(value, 1), ceiling)


class NestingLedger:
    """Each run's parent and depth, and the starts not yet registered.

    ``parent_session_of`` maps a sub-agent session key to the session that
    spawned it, and ``run_of_session`` maps any other session key to the run it
    works for; each returns ``None`` when it does not know the key.
    """

    def __init__(
        self,
        *,
        max_depth: Optional[int] = None,
        max_nested_runs: Optional[int] = None,
        parent_session_of: Optional[Callable[[str], Optional[str]]] = None,
        run_of_session: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        self.max_depth = _clamp(max_depth, DEFAULT_MAX_DEPTH, MAX_DEPTH_CEILING)
        self.max_nested_runs = _clamp(
            max_nested_runs, DEFAULT_MAX_NESTED_RUNS, MAX_NESTED_RUNS_CEILING
        )
        self._parent_session_of = parent_session_of
        self._run_of_session = run_of_session
        self._depth: dict[str, int] = {}
        self._children: dict[str, set[str]] = {}
        self._pending: set[str] = set()
        # Dashboard sessions a run opened (``ctx.open_session``, or
        # ``session_create`` from a session that already works for a run),
        # keyed by slot key: a run started from one counts as that run's child.
        self._opened: dict[str, str] = {}
        # Placeholder -> the host run ``record`` moved it onto, until settled.
        self._moved: dict[str, str] = {}

    def set_max_depth(self, value: object) -> None:
        self.max_depth = _clamp(value, DEFAULT_MAX_DEPTH, MAX_DEPTH_CEILING)

    def set_max_nested_runs(self, value: object) -> None:
        self.max_nested_runs = _clamp(value, DEFAULT_MAX_NESTED_RUNS, MAX_NESTED_RUNS_CEILING)

    def bind_session(self, slot_key: str, run_id: str) -> None:
        """Record that *run_id* opened the dashboard session *slot_key*."""
        if slot_key and run_id:
            self._opened[slot_key] = run_id

    def opened_by(self, slot_key: str) -> str:
        """The run that opened dashboard session *slot_key*, or ``""``."""
        return self._opened.get(slot_key, "")

    @property
    def tracks_opened_sessions(self) -> bool:
        return bool(self._opened)

    def depth_of(self, run_id: str) -> int:
        return self._depth.get(run_id, 0)

    def parent_run_of(self, session_key: str) -> str:
        """The run *session_key* works for, directly or through sub-agents, or ``""``."""
        key = session_key or ""
        for _ in range(_MAX_LINEAGE_HOPS):
            run_id = (
                run_id_of_worker_key(key)
                or self._opened.get(key, "")
                or self._lookup(self._run_of_session, key)
            )
            if run_id:
                return run_id
            if not key.startswith(_SUBAGENT_PREFIX):
                return ""
            parent = self._lookup(self._parent_session_of, key)
            if not parent or parent == key:
                return ""
            key = parent
        return ""

    @staticmethod
    def _lookup(resolve: Optional[Callable[[str], Optional[str]]], key: str) -> str:
        if resolve is None or not key:
            return ""
        try:
            return resolve(key) or ""
        except Exception:  # noqa: BLE001 - an unreadable record reads as no lineage
            logger.warning("workflow lineage lookup failed for %s", key, exc_info=True)
            return ""

    def admit(
        self, session_key: str, run_id: str, *, is_active: Callable[[str], bool]
    ) -> Optional[dict]:
        """Reserve *run_id* under its parent run, or return the refusal.

        The check and the reservation happen without a yield, so two starts
        racing for the last fan-out slot cannot both pass. ``is_active`` says
        whether a registered run is still in flight.
        """
        parent = self.parent_run_of(session_key)
        depth = self._child_depth(parent)
        if depth > self.max_depth:
            logger.warning(
                "workflow start refused: depth %d exceeds the cap of %d (parent run=%s)",
                depth,
                self.max_depth,
                parent,
            )
            return {
                "error": (
                    f"workflow_run refused: this run would be nested {depth} deep, past "
                    f"the workflow nesting cap of {self.max_depth} "
                    "(agent.workflow_max_depth); do this work in the current run "
                    "instead of starting another"
                ),
                "error_code": ERROR_CODE_DEPTH,
            }
        if parent:
            siblings = self._children.setdefault(parent, set())
            siblings.intersection_update(
                {c for c in siblings if c in self._pending or is_active(c)}
            )
            if len(siblings) >= self.max_nested_runs:
                logger.warning(
                    "workflow start refused: run %s already has %d nested runs in flight",
                    parent,
                    len(siblings),
                )
                return {
                    "error": (
                        f"workflow_run refused: run {parent} already has {len(siblings)} "
                        "nested runs in flight, the cap set by "
                        "agent.workflow_max_nested_runs; wait for one to finish or do "
                        "this work in the current run"
                    ),
                    "error_code": ERROR_CODE_FANOUT,
                }
            siblings.add(run_id)
        self._depth[run_id] = depth
        self._pending.add(run_id)
        return None

    def record(self, session_key: str, run_id: str, *, reserved_id: str = "") -> None:
        """Track *run_id* under its parent run without checking the caps.

        For host runs (a task runner's shared run), which this ledger does not
        admit: the task already runs, and tracking the host run keeps the
        depth and fan-out of the runs its steps start. *reserved_id* is the
        placeholder an admitted start holds for this run; the run takes over
        its slot instead of adding a second one.
        """
        if run_id in self._depth:
            return
        if reserved_id and reserved_id in self._pending:
            self.rename(reserved_id, run_id)
            self._moved[reserved_id] = run_id
            return
        parent = self.parent_run_of(session_key)
        self._depth[run_id] = self._child_depth(parent)
        if parent:
            self._children.setdefault(parent, set()).add(run_id)

    def _child_depth(self, parent: str) -> int:
        return max(self._depth.get(parent, 1), 1) + 1 if parent else 1

    def release_placeholder(self, reserved_id: str) -> str:
        """The run ``record`` moved *reserved_id* onto, or ``""``; forgets the move."""
        return self._moved.pop(reserved_id, "")

    def rename(self, reserved_id: str, run_id: str) -> None:
        """Move a reservation made before its run id existed onto *run_id*."""
        depth = self._depth.pop(reserved_id, None)
        if depth is not None:
            self._depth[run_id] = depth
        if reserved_id in self._pending:
            self._pending.discard(reserved_id)
            self._pending.add(run_id)
        for siblings in self._children.values():
            if reserved_id in siblings:
                siblings.discard(reserved_id)
                siblings.add(run_id)

    def settle(self, run_id: str, *, started: bool) -> None:
        """End *run_id*'s reservation; a start that produced no run is forgotten."""
        self._pending.discard(run_id)
        if started:
            return
        self._depth.pop(run_id, None)
        for siblings in self._children.values():
            siblings.discard(run_id)
