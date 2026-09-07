"""Task-runner migration adapter — plan side.

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

from kiro_crew.migration import protocol as P

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


class TaskRunMigrationAdapter:
    """MigrationUnitAdapter for task-runner runs.

    Source-side takes ``run_lookup``; the resume/restart classifier and the
    real git-probe wiring stay separate (circle 1 / a later circle). This
    circle covers serialize + quiesce-at-boundary.
    """

    bundle_kind = "taskrun"
    bundle_version = 1

    def __init__(self, *, run_lookup: dict | None = None) -> None:
        self._runs = run_lookup or {}

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

    async def refuse_if_mid_run(self, unit_id: str) -> None:
        """Raise ``MidRunError`` when a task is mid-execution (Req 6.6).

        Reachable at PLAN time: describing a move for a run with a task in
        flight would describe something that cannot happen, because the boundary
        the transfer needs has not been reached. Kept as its own check rather
        than as a side effect of the transfer's quiesce step, so the plan half
        does not have to call a transfer step to get an error out of it.
        """
        run = self._run(unit_id)
        mid = {"in_progress", "reviewing"}
        if any(_task_status(t) in mid for t in _tasks(run)):
            raise P.MidRunError(f"task run {unit_id!r} has a task mid-execution")

    async def serialize(self, unit_id: str) -> dict:
        return serialize_project(self._run(unit_id))
