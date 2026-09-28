"""``kiro_crew.taskq`` -- the durable task queue behind every accepted unit of work.

Public surface:

* :class:`TaskStore` / :class:`TaskStoreUnavailable` (``store``): the SQLite
  file at ``$KIROCREW_HOME/tasks/tasks.db``, write-before-ack, atomic claim,
  lease + generation fencing, the bounded dispatch-window reads.
* :class:`TaskRecord`, the state constants and :func:`check_transition`
  (``model``): the one validated state machine.
* :func:`import_legacy` (``migrate``): one-time, idempotent import of the
  subagent folders and TaskRunner ``runs.json`` records that predate the store.
* :func:`reconcile_on_boot` (``reconcile``): settle the rows a dead
  incarnation left active before dispatching anything new.
* :class:`WaitRecord` / :class:`WaitLedger` (``waits``): every pause is a wait
  with a reason; the ledger enters, wakes, parks and rebuilds them.
* :class:`DependencySignal` / :class:`DependencyCoordinator` (``dependency``):
  one retry schedule per external dependency scope; :func:`classify_exception`
  turns an adapter-known exception into a signal, :func:`shared_retry_at` lets
  a caller with no task row read a scope's cooldown.
* :func:`open_default_store`: the gateway's one-call boot sequence.

Specification: ``docs/system-specs/modules/taskq.md``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from .dependency import (
    DependencyCoordinator,
    DependencySignal,
    classify_exception,
    coordinator_from_config,
    current_coordinator,
    register_coordinator,
    shared_retry_at,
)
from .migrate import SCHEMA_VERSION, ImportReport, import_legacy
from .model import (
    ACTIVE,
    ADMITTED,
    CANCELLED,
    CLAIMABLE,
    DONE,
    FAILED,
    KIND_SUBAGENT,
    KINDS,
    QUEUED,
    RECOVERING,
    RETRY_WAIT,
    RUNNING,
    SIDE_EFFECT_CLASSES,
    SIDE_EFFECT_IDEMPOTENT_KEY,
    SIDE_EFFECT_NONE,
    SIDE_EFFECT_UNKNOWN,
    STARTING,
    STATES,
    SUCCEEDED,
    TERMINAL,
    TRANSITIONS,
    UNKNOWN_SIDE_EFFECT,
    WAITING,
    WAITING_CHILDREN,
    WAITING_DEPENDENCY,
    WAITING_INFRA,
    WAITING_INPUT,
    WAITING_PERMISSION,
    InvalidTransition,
    TaskEvent,
    TaskRecord,
    check_transition,
    is_terminal,
    steps_to,
)
from .reconcile import ReconcileReport, reconcile_on_boot
from .store import (
    DEFAULT_DISPATCH_WINDOW,
    ClaimResult,
    TaskStore,
    TaskStoreUnavailable,
    detect_network_filesystem,
)
from .waits import (
    ON_CHILD_FAILURE_CONTINUE,
    ON_CHILD_FAILURE_FAIL_PARENT,
    ON_CHILD_FAILURE_PARAM,
    ChildOutcome,
    ResumeCondition,
    WaitLedger,
    WaitRecord,
    on_child_failure_policy,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ACTIVE",
    "ADMITTED",
    "CANCELLED",
    "CLAIMABLE",
    "DEFAULT_DISPATCH_WINDOW",
    "DONE",
    "FAILED",
    "KIND_SUBAGENT",
    "KINDS",
    "QUEUED",
    "RECOVERING",
    "RETRY_WAIT",
    "RUNNING",
    "SCHEMA_VERSION",
    "SIDE_EFFECT_CLASSES",
    "SIDE_EFFECT_IDEMPOTENT_KEY",
    "SIDE_EFFECT_NONE",
    "SIDE_EFFECT_UNKNOWN",
    "STARTING",
    "STATES",
    "SUCCEEDED",
    "TERMINAL",
    "TRANSITIONS",
    "UNKNOWN_SIDE_EFFECT",
    "WAITING",
    "WAITING_CHILDREN",
    "WAITING_DEPENDENCY",
    "WAITING_INFRA",
    "WAITING_INPUT",
    "WAITING_PERMISSION",
    "ON_CHILD_FAILURE_CONTINUE",
    "ON_CHILD_FAILURE_FAIL_PARENT",
    "ON_CHILD_FAILURE_PARAM",
    "ChildOutcome",
    "ClaimResult",
    "DependencyCoordinator",
    "DependencySignal",
    "ImportReport",
    "InvalidTransition",
    "ReconcileReport",
    "ResumeCondition",
    "WaitLedger",
    "WaitRecord",
    "TaskEvent",
    "TaskRecord",
    "TaskStore",
    "TaskStoreUnavailable",
    "check_transition",
    "classify_exception",
    "coordinator_from_config",
    "current_coordinator",
    "detect_network_filesystem",
    "import_legacy",
    "is_terminal",
    "on_child_failure_policy",
    "open_default_store",
    "reconcile_on_boot",
    "register_coordinator",
    "shared_retry_at",
    "steps_to",
]


def open_default_store(
    home: Path,
    *,
    window: int = DEFAULT_DISPATCH_WINDOW,
    artifact_probe: "Callable[[TaskRecord], str | None] | None" = None,
    import_legacy_records: bool = True,
    taskrunner_runs_path: Path | None = None,
    journal_mode: str = "auto",
) -> TaskStore:
    """Open ``<home>/tasks/tasks.db``, import legacy records, reconcile, return.

    This is the order the RFC fixes: schema, import (keyed, so a second boot
    adds nothing), reconcile (settle dead owners' rows), wait rebuild, and only
    then may the caller dispatch. Raises :class:`TaskStoreUnavailable` when the
    file cannot be opened; a failure in any of the three later phases is logged,
    never fatal, because a store that opens can still accept new work.

    The two sweeps run ONCE here and the store this returns carries no record of
    what they could not settle, so the rows they refused are named in the log
    (:func:`_log_unsettled`): nothing in the process re-examines a row a boot
    sweep failed on -- the pump's repeated sweep reads wait DEADLINES alone -- so
    the log is the only place an operator can learn which row waits for the next
    restart. (A legacy-import error is different: the import is keyed and
    idempotent, so the next boot retries the row, and ``import_legacy`` counts
    its own.)

    TaskRunner keeps ``runs.json`` in its own work directory, which only the
    runner resolves, so its path is a parameter rather than a guess; ``None``
    imports subagent folders only. ``journal_mode`` is
    ``agent.task_store_journal_mode``: ``auto`` detects a network filesystem
    and picks DELETE there (WAL otherwise); ``wal`` / ``delete`` force one.
    """
    network_fs = {"wal": False, "delete": True}.get(str(journal_mode or "auto").lower())
    store = TaskStore(TaskStore.default_path(home), window=window, network_fs=network_fs).open()
    if import_legacy_records:
        try:
            import_legacy(
                store.insert_if_absent,
                subagents_dir=home / "subagents",
                taskrunner_runs_path=taskrunner_runs_path,
            )
        except Exception:  # noqa: BLE001 - import is best-effort by contract
            logger.warning("taskq legacy import failed", exc_info=True)
    try:
        _log_unsettled("reconcile", reconcile_on_boot(store, artifact_probe=artifact_probe).errors)
    except Exception:  # noqa: BLE001 - reconcile is best-effort by contract
        logger.warning("taskq boot reconcile failed", exc_info=True)
    try:
        _log_unsettled("wait rebuild", WaitLedger(store).rebuild().errors)
    except Exception:  # noqa: BLE001 - rebuild is best-effort by contract
        logger.warning("taskq wait rebuild failed", exc_info=True)
    return store


def _log_unsettled(phase: str, errors: list[str]) -> None:
    """Name the rows one boot phase could not settle, at WARNING.

    A phase reports per-row refusals instead of raising, so the report is the
    only place they exist; discarding it silently leaves a parent that will
    never wake and an orphan that will never be cancelled, both indistinguishable
    from a clean boot until the next restart.
    """
    if not errors:
        return
    logger.warning(
        "taskq boot %s left %d row(s) unsettled: %s",
        phase,
        len(errors),
        "; ".join(errors[:10]),
    )
