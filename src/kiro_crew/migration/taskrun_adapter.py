"""Task-runner migration adapter (slice 4 of issue #7577).

First circle: the resume-vs-restart classifier only. The design's key
correction to the issue is that a task run's blocker is NOT its run record
(``Project`` in runs.json is fully serializable) but its GIT STATE. So the
classifier probes git reproducibility on the target:

  * does ``repo_root`` resolve there,
  * is ``branch_name`` reachable,
  * can ``worktree_path`` be recreated.

All three reproducible -> ``resume`` (the target can continue without
re-executing completed tasks, Task 4.5). Any one unreproducible -> ``restart``,
and every unreproducible reference is NAMED (Req 6.3) so the confirmation gate
(Task 4.4, a later circle) can tell the user exactly what is lost.

The three git checks are INJECTED as callables (``GitReproProbe``) so this
layer is pure and testable — the real probe wiring is a later circle.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from typing import Callable

from kiro_crew.migration import protocol as P


@dataclass(frozen=True)
class GitReproProbe:
    """Injected git-reproducibility checks, run against the TARGET host."""

    repo_root_resolves: Callable[[str], bool]
    branch_reachable: Callable[[str], bool]
    worktree_recreatable: Callable[[str], bool]


def classify_resume_or_restart(run_state: dict, probe: GitReproProbe) -> P.PreflightReport:
    """Classify a task-runner run as ``resume`` or ``restart``.

    Returns a PreflightReport whose ``resume_class`` is the verdict and whose
    findings NAME each unreproducible git reference (advisory — the hard
    confirmation gate for a restart lives in Task 4.4, not here).
    """
    findings: list[P.Finding] = []

    checks = (
        ("repo_root", "git_repo", probe.repo_root_resolves, run_state.get("repo_root", "")),
        ("branch_name", "git_repo", probe.branch_reachable, run_state.get("branch_name", "")),
        (
            "worktree_path",
            "git_repo",
            probe.worktree_recreatable,
            run_state.get("worktree_path", ""),
        ),
    )

    for key, kind, check, value in checks:
        if not check(value):
            findings.append(
                P.Finding(
                    kind=kind,
                    detail=f"git reference '{key}' ({value!r}) is not reproducible "
                    f"on the target; the run must restart rather than resume",
                    severity="advisory",
                    detail_key=key,
                )
            )

    resume_class = "restart" if findings else "resume"
    return P.PreflightReport(findings=findings, resume_class=resume_class)


# ----------------------------------------------- circle 2: serialize / resume

# Task statuses that count as "done" — a resume must not re-execute these.
_DONE_STATUSES = frozenset({"passed", "skipped"})

# Durable, portable run state — shipped in the bundle.
PROJECT_SHIP_FIELDS: tuple[str, ...] = (
    # the plan and where the run is in it
    "spec_path",
    "spec_content",
    "tasks",
    "current_task",
    "replan_count",
    "memory",
    "task_id",
    "name",
    "status",
    "mode",
    # how the run was created (needed to rebuild an equivalent run)
    "original_input",
    "source",
    "source_spec",
    "skip_planning",
    "lessons_learned",
    "auto_approve",
    # portable git identity: branch names and commits are meaningful anywhere
    # the repo is reachable; the PATHS are not (see drop list)
    "branch_name",
    "base_branch",
    "commit_hashes",
    "git_enabled",
    # workflow provenance
    "workflow_run_id",
    "workflow_id",
    "workflow_slug",
    "workflow_revision",
    "derived_from_workflow_id",
    "derived_from_revision",
)

# Everything not shipped — dropped by explicit decision.
PROJECT_DROP_FIELDS: tuple[str, ...] = (
    # SOURCE-host filesystem locations: the target resolves/recreates its own,
    # and the preflight git-reproducibility probe is what decides whether it can
    "work_dir",
    "worktree_path",
    "repo_root",
    # source-host execution timing and failure text — observations of a run on
    # a different machine, meaningless once it moves
    "started_at",
    "finished_at",
    "last_task_time",
    "error",
    "tokens_used",
)


def _status_str(status) -> str:
    """Normalize a TaskStatus enum or plain string to its value."""
    return getattr(status, "value", status)


# A run reaches this module in one of two equally legitimate shapes: the live
# ``Project`` dataclass the runner holds in memory, and the plain dict that same
# record becomes in ``runs.json``. The CLI reads the persisted form off disk and
# the gateway has the live object, so every accessor below tolerates both rather
# than forcing one side to rehydrate.


def _field(run, name: str, default=None):
    """Read ``name`` from a Project dataclass or its persisted dict form."""
    if isinstance(run, dict):
        value = run.get(name, default)
    else:
        value = getattr(run, name, default)
    return default if value is None else value


def _tasks(run) -> list:
    """The run's task list, from either shape.

    The live ``Project`` holds it on ``.tasks``; ``runs.json`` stores it under
    ``task_details`` (verified against taskrunner.py's ``_serialize_runs``). Both
    are the same list, so callers should never have to know which they hold.
    """
    if isinstance(run, dict):
        return run.get("tasks") or run.get("task_details") or []
    return getattr(run, "tasks", []) or []


def _task_status(task) -> str:
    return _status_str(
        task.get("status") if isinstance(task, dict) else getattr(task, "status", "")
    )


def _task_title(task) -> str:
    return task.get("title", "") if isinstance(task, dict) else getattr(task, "title", "")


def serialize_project(run) -> dict:
    """Serialize a task run to a portable dict (Req 6.1, 3.4).

    Accepts either the live ``Project`` dataclass or its persisted ``runs.json``
    dict form — the CLI reads the latter off disk.

    Allow-list, not exclude-list: only ``PROJECT_SHIP_FIELDS`` travel, so a
    field added to ``Project`` later is dropped until someone makes an explicit
    decision about it — and the drift-guard test fails until they do.

    Carries the full task list with per-task status/attempts/approval flags,
    ``current_task``, ``replan_count``, ``WorkingMemory`` and the spec content.
    Host-local git PATHS (``repo_root``, ``worktree_path``, ``work_dir``) are
    dropped: the target resolves its own, and preflight's git-reproducibility
    probe is what decides whether it can. Enum statuses are normalized to their
    string value so the payload is plain JSON-safe data.
    """
    raw = run if isinstance(run, dict) else dataclasses.asdict(run)
    payload = P.allow_list_serialize(raw, allowed=PROJECT_SHIP_FIELDS)
    # One task-list key on the wire regardless of source shape: the persisted
    # form calls it task_details, the live dataclass calls it tasks.
    tasks = _tasks(run)
    if tasks:
        payload["tasks"] = [
            dict(t) if isinstance(t, dict) else dataclasses.asdict(t) for t in tasks
        ]
    for t in payload.get("tasks", []):
        if isinstance(t, dict):
            t["status"] = _status_str(t.get("status"))
    payload.pop("task_details", None)
    return payload


# State the live Project holds but runs.json does not persist. A migration
# sourced from disk cannot carry these, so it must SAY so rather than quietly
# arriving without them (the same rule as the Layer B fidelity warning).
_UNPERSISTED_RUN_STATE: tuple[tuple[str, str], ...] = (
    ("memory", "WorkingMemory (files changed, decisions, blockers)"),
    ("current_task", "the index of the task the run was on"),
)


def run_fidelity_findings(run) -> list[P.Finding]:
    """Report run state the source could not supply (Req 5.6-style honesty).

    ``runs.json`` persists task status but not ``WorkingMemory`` or
    ``current_task``. A run read from disk therefore migrates with less context
    than one taken from the live runner. Advisory, not blocking: the move is
    still useful — completed tasks are still not re-executed — but the loss must
    be visible.
    """
    findings: list[P.Finding] = []
    for key, human in _UNPERSISTED_RUN_STATE:
        if _field(run, key, None) in (None, ""):
            findings.append(
                P.Finding(
                    kind="taskrun_state",
                    detail=f"run state '{key}' ({human}) is not available from this "
                    f"source and will not travel; the target resumes without it",
                    severity="advisory",
                    detail_key=key,
                )
            )
    return findings


def remaining_tasks_after_resume(run):
    """Return the tasks a resume still has to run — completed ones excluded.

    A task recorded ``passed`` or ``skipped`` is NOT re-executed (Req 6.5).
    Pending tasks keep their ``requires_approval`` flag: migration is not an
    approval channel (Req 6.7), so an approval-gated task still awaits approval
    on the target. Accepts either run shape and returns tasks in that shape.
    """
    return [t for t in _tasks(run) if _task_status(t) not in _DONE_STATUSES]


class RestartNotConfirmed(RuntimeError):
    """Raised when a restart-classified migration lacks explicit confirmation.

    Requirement 6.4: a restart throws away completed work, so it may never be
    silent. The exception message names exactly what would be discarded.
    """


def describe_discarded_progress(run) -> dict:
    """Summarize what a RESTART would throw away (Req 6.4).

    A restart re-runs the whole plan on the target, so everything already
    recorded complete is lost work. Naming it — count, titles, and any commits
    the run produced — is what turns a destructive default into an informed
    choice. Accepts either run shape.
    """
    done = [t for t in _tasks(run) if _task_status(t) in _DONE_STATUSES]
    return {
        "completed_count": len(done),
        "completed_titles": [_task_title(t) for t in done],
        "commit_count": len(_field(run, "commit_hashes", []) or []),
        "replan_count": _field(run, "replan_count", 0),
    }


def require_restart_confirmation(run, *, confirmed: bool) -> dict:
    """Gate a restart on explicit confirmation; return the discarded summary.

    Never silent (Req 6.4): without ``confirmed`` this raises and the message
    names the loss. With it, the summary is returned so the caller can record
    what the user agreed to discard.
    """
    desc = describe_discarded_progress(run)
    if not confirmed:
        raise RestartNotConfirmed(
            f"restart would discard {desc['completed_count']} completed task(s) "
            f"({', '.join(desc['completed_titles']) or 'none'}) and "
            f"{desc['commit_count']} commit(s); confirm explicitly to proceed"
        )
    return desc


class TaskRunMigrationAdapter:
    """MigrationUnitAdapter for task-runner runs.

    Source-side takes ``run_lookup``; the resume/restart classifier and the
    real git-probe wiring stay separate (circle 1 / a later circle). This
    circle covers serialize + quiesce-at-boundary.
    """

    bundle_kind = "taskrun"
    bundle_version = 1

    def __init__(
        self,
        *,
        run_lookup: dict | None = None,
        create_run: Callable[[dict], str] | None = None,
        registry=None,
    ) -> None:
        self._runs = run_lookup or {}
        self._create_run = create_run
        # Durable tombstone registry (Req 7.3) — before this, a moved run
        # recorded its destination nowhere that outlived the process.
        self._registry = registry
        self._quiesced: set[str] = set()
        self._tombstones: dict[str, P.Tombstone] = {}

    def _run(self, unit_id: str):
        try:
            return self._runs[unit_id]
        except KeyError as exc:
            raise KeyError(f"no task run {unit_id!r} on this crew") from exc

    async def describe(self, unit_id: str) -> dict:
        run = self._run(unit_id)
        return {
            "unit_id": unit_id,
            "kind": self.bundle_kind,
            "name": _field(run, "name", "") or unit_id,
        }

    async def requirements(self, unit_id: str) -> list[P.HostRequirement]:
        run = self._run(unit_id)
        reqs: list[P.HostRequirement] = []
        repo_root = _field(run, "repo_root", "")
        if repo_root:
            reqs.append(P.HostRequirement(kind="git_repo", identity=repo_root, severity="blocking"))
        return reqs

    async def quiesce(self, unit_id: str) -> P.QuiesceToken:
        """Pause at a task boundary; refuse if a task is mid-execution.

        Never serialize a run with a task in flight (Req 6.6): an
        ``in_progress`` (or ``reviewing``) task means the boundary has not been
        reached, so quiesce raises MidRunError instead.
        """
        run = self._run(unit_id)
        mid = {"in_progress", "reviewing"}
        if any(_task_status(t) in mid for t in _tasks(run)):
            raise P.MidRunError(f"task run {unit_id!r} has a task mid-execution")
        self._quiesced.add(unit_id)
        return P.QuiesceToken(unit_id=unit_id, token="taskrun-quiesced")

    async def unquiesce(self, unit_id: str, token: P.QuiesceToken) -> None:
        """Roll back a quiesce: the run is runnable in place again (Req 6.8).

        Nothing about the run record was mutated by quiesce — it only stopped
        being scheduled — so un-quiescing restores it exactly, with every
        completed task still recorded complete.
        """
        self._quiesced.discard(unit_id)

    async def serialize(self, unit_id: str) -> dict:
        return serialize_project(self._run(unit_id))

    async def materialize(self, payload: dict) -> str:
        """Re-create the run on the target; returns the new local run id."""
        if self._create_run is None:
            raise RuntimeError("materialize requires a target-side create_run")
        new_id = self._create_run(payload)
        if self._registry is not None:
            # Live here now — drop a tombstone left by a previous move away
            # (the move-back case, Req 7.4).
            self._registry.clear(self.bundle_kind, new_id)
        return new_id

    async def tombstone(self, unit_id: str, target: P.CrewRef, remote_id: str) -> None:
        """Retain the run, non-executing, naming its new home (Req 2.8).

        A tombstone without a remote id would claim the work moved while naming
        nowhere it moved to — the one state that loses the unit — so it is
        refused.
        """
        if not remote_id:
            raise ValueError("tombstone requires the target's remote unit id")
        self._tombstones[unit_id] = P.Tombstone(
            unit_kind=self.bundle_kind,
            target_crew=target,
            remote_unit_id=remote_id,
            migrated_ts=time.time(),
        )
        if self._registry is not None:
            # After the empty-remote-id refusal above, so a refused tombstone
            # never leaves a durable record claiming the run moved.
            self._registry.record(self.bundle_kind, unit_id, self._tombstones[unit_id])

    def tombstone_of(self, unit_id: str) -> P.Tombstone:
        return self._tombstones[unit_id]

    def is_resumable_in_place(self, unit_id: str) -> bool:
        """True when the SOURCE can still run this unit itself (Req 6.8).

        False while quiesced (paused for a migration in flight) and false once
        tombstoned (released to the target). Every pre-ack failure path calls
        ``unquiesce``, so it returns True again — the invariant that a failed
        migration leaves the source owning executable work.
        """
        if unit_id in self._tombstones:
            return False
        return unit_id in self._runs and unit_id not in self._quiesced
