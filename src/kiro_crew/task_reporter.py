"""Reporting, progress persistence, and output formatting for the task runner."""

from __future__ import annotations

import inspect
import logging
import re
import time
from pathlib import Path
from typing import Awaitable, Callable, Protocol, Union, cast

from kiro_crew.safety_override import safety_override
from kiro_crew.security import redact_exfiltration_urls
from kiro_crew.task_models import (
    PROGRESS_FILE,
    SESSION_PREFIX,
    Project,
    Task,
    TaskStatus,
)
from kiro_crew.task_planner import group_parallel_tasks

logger = logging.getLogger(__name__)


# ── Notifications ──


class SessionAwareNotify(Protocol):
    """A notification sink that also accepts the run's originating conversation.

    ``session_key`` is keyword-only **with a default** so a sink can adopt it
    without changing its positional shape. It names the conversation the run was
    started FROM, which is what lets a sink route a stall-worthy notice back to
    the surface the operator is watching instead of one hard-wired destination.
    Empty means "no originating conversation" (a dashboard or CLI start), never
    "route it anywhere".
    """

    def __call__(
        self, title: str, body: str, task_id: str = "", *, session_key: str = ""
    ) -> Awaitable[None]: ...


LegacyNotify = Callable[[str, str, str], Awaitable[None]]

# A union, not one widened signature, because no single signature is satisfied by
# both shapes: a Protocol declaring ``session_key`` rejects every sink that
# predates it (the CLI's printer, a dozen test doubles), while
# ``Callable[[str, str, str], ...]`` rejects the caller for passing the keyword.
# Spelling the transition in the type keeps mypy checking the arity of both,
# where a ``Callable[..., Awaitable[None]]`` escape hatch would check neither.
NotifyCallback = Union[SessionAwareNotify, LegacyNotify]


def _accepts_session_key(callback: NotifyCallback) -> bool:
    """Whether *callback* takes the ``session_key`` keyword.

    Probed rather than assumed, and probed rather than discovered by letting the
    ``TypeError`` fly: :func:`notify` swallows sink failures at debug level, so
    an unconditional keyword handed to a legacy three-argument sink would not
    surface anywhere — that sink's notifications would simply stop arriving. A
    ``TypeError`` retry is no better, since it cannot tell an arity mismatch
    from one raised inside the sink's own body. An unintrospectable callable
    (a C builtin) reads as legacy, which is the lossless direction.
    """
    try:
        inspect.signature(callback).bind("", "", "", session_key="")
    except (TypeError, ValueError):
        return False
    return True


async def notify(
    title: str,
    body: str,
    run: Project | None = None,
    callback: NotifyCallback | None = None,
    *,
    session_key: str = "",
) -> None:
    """Send notification via callback if registered."""
    if run:
        label, _ = redact_exfiltration_urls(run.name or Path(run.spec_path).stem)
        title = f"[{label}] {title}"
    title = redact_exfiltration_urls(title)[0]
    body = redact_exfiltration_urls(body)[0]
    if callback:
        task_id = run.task_id if run else ""
        try:
            # Widen the call only when there is a conversation to carry, so a
            # notification with no origin reaches every sink through the exact
            # call it received before this parameter existed.
            if session_key and _accepts_session_key(callback):
                await cast(SessionAwareNotify, callback)(
                    title, body, task_id, session_key=session_key
                )
            else:
                await cast(LegacyNotify, callback)(title, body, task_id)
        except Exception:
            logger.debug("Notification failed", exc_info=True)


# ── Progress Persistence ──


def save_progress(run: Project) -> None:
    """Write TASK_PROGRESS.md next to the spec file, when the run has one."""
    if not run.spec_path:
        # Ad-hoc plans have no spec directory.  Treating Path("").parent as a
        # destination leaks TASK_PROGRESS.md into the gateway's current working
        # directory (and into the repository during tests).
        return
    spec_dir = Path(run.spec_path).parent
    progress_path = spec_dir / PROGRESS_FILE
    try:
        elapsed = (run.finished_at or time.time()) - run.started_at
        lines: list[str] = [
            "# Task Progress\n",
            f"**Spec:** `{Path(run.spec_path).name}`\n",
            f"**Status:** {run.status}\n",
            f"**Elapsed:** {int(elapsed)}s\n",
            f"**Tokens:** {run.tokens_used}\n",
            f"**Replans:** {run.replan_count}\n",
            "",
            "## Tasks\n",
        ]
        for task in run.tasks:
            icon = {
                TaskStatus.PASSED: "✅",
                TaskStatus.FAILED: "❌",
                TaskStatus.IN_PROGRESS: "🔄",
                TaskStatus.REVIEWING: "🔍",
                TaskStatus.PENDING: "⬜",
                TaskStatus.SKIPPED: "⏭️",
            }[task.status]
            line = f"- {icon} **Task {task.index}:** {task.title}"
            if task.attempts > 1:
                line += f" (attempts: {task.attempts})"
            if task.error:
                line += f"\n  - Error: {task.error[:200]}"
            lines.append(line)

        if run.error:
            lines.append(f"\n## Error\n{run.error}\n")

        progress_path.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        logger.debug("Failed to save progress", exc_info=True)


# ── Checkpoint Resume ──


def load_checkpoint(spec_path: Path) -> set[str] | None:
    """Read TASK_PROGRESS.md and return set of completed task titles (lowercase).

    Returns None if no checkpoint exists or it cannot be parsed.
    """
    progress = spec_path.parent / PROGRESS_FILE
    if not progress.exists():
        return None
    try:
        content = progress.read_text(encoding="utf-8")
    except OSError:
        return None

    completed: set[str] = set()
    for line in content.splitlines():
        match = re.match(r"^- ✅ \*\*Task \d+:\*\* (.+?)(?:\s*\(attempts:.*\))?$", line)
        if not match:
            # Backward compat: also match old "Step N:" format
            match = re.match(r"^- ✅ \*\*Step \d+:\*\* (.+?)(?:\s*\(attempts:.*\))?$", line)
        if match:
            completed.add(match.group(1).lower().strip())
    return completed if completed else None


def build_resume_context(completed: list[Task]) -> str:
    """Build LLM context summarizing previously completed work."""
    lines = ["## Previously Completed (from checkpoint)"]
    for task in completed:
        lines.append(f"- ✅ {task.title}")
    lines.append("")
    lines.append(
        "These tasks were completed in a previous run. "
        "Their changes are already on disk. Continue from the next task."
    )
    return "\n".join(lines)


# ── Status Summary ──


def build_status(
    runs: dict[str, Project],
    tasks: dict,
    agent: str = "",
) -> dict:
    """Return current run status as a dict."""
    runs_data = []
    for run in runs.values():
        runs_data.append(
            {
                "task_id": run.task_id,
                "name": redact_exfiltration_urls(run.name)[0],
                "running": run.task_id in tasks and not tasks[run.task_id].done(),
                "status": run.status,
                "spec": run.spec_path,
                "spec_name": Path(run.spec_path).stem if run.spec_path else run.task_id,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "tasks": len(run.tasks),
                "current_task": run.current_task,
                "completed": sum(1 for t in run.tasks if t.status == TaskStatus.PASSED),
                "failed": sum(1 for t in run.tasks if t.status == TaskStatus.FAILED),
                "skipped": sum(1 for t in run.tasks if t.status == TaskStatus.SKIPPED),
                "error": run.error,
                "tokens_used": run.tokens_used,
                "replan_count": run.replan_count,
                "auto_approve": getattr(run, "auto_approve", False),
                "auto_approve_remaining_secs": (
                    safety_override().scope_remaining_secs(
                        f"{SESSION_PREFIX}:{run.task_id}:autoapprove"
                    )
                    if getattr(run, "auto_approve", False)
                    else 0
                ),
                "original_input": run.original_input[:2000],
                "source": run.source,
                "task_details": [
                    {
                        "index": t.index,
                        "title": t.title,
                        "description": t.description if t.description != t.title else "",
                        "status": t.status.value,
                        "error": t.error or "",
                        "result": (t.result or "")[:2000],
                        "attempts": t.attempts,
                        "depends_on": t.depends_on,
                        "requires_approval": t.requires_approval,
                        "force_approval": t.force_approval,
                        "created_at": t.created_at or None,
                        "started_at": t.started_at or None,
                        "finished_at": t.finished_at or None,
                    }
                    for t in run.tasks
                ],
                "work_dir": run.work_dir,
                "branch_name": run.branch_name,
                "revision": run.revision,
                "execution_mode": run.execution_mode,
                "commit_policy": run.commit_policy,
                "review_fix": run.review_fix.to_dict() if run.review_fix else None,
                "spec_content": run.spec_content[:4000] if run.spec_content else "",
                "lessons_learned": run.lessons_learned,
                "commits": len(run.commit_hashes),
                "groups": (
                    [
                        [t.index for t in group]
                        for group in group_parallel_tasks(
                            run.tasks,
                            {
                                t.index
                                for t in run.tasks
                                if t.status in (TaskStatus.PASSED, TaskStatus.SKIPPED)
                            },
                        )
                    ]
                    if run.tasks
                    else []
                ),
            }
        )
    return {
        "running": any(not t.done() for t in tasks.values()),
        "agent": agent,
        "runs": runs_data,
    }


# ── Completion Summary Formatting ──


def format_completion_summary(run: Project) -> str:
    """Format the completion notification body."""
    passed = sum(1 for t in run.tasks if t.status == TaskStatus.PASSED)
    failed = sum(1 for t in run.tasks if t.status == TaskStatus.FAILED)
    elapsed = int(time.time() - run.started_at)
    mins, secs = divmod(elapsed, 60)
    hours, mins = divmod(mins, 60)
    time_str = f"{hours}h {mins}m" if hours else f"{mins}m {secs}s"

    lines = [
        f"**{passed}/{len(run.tasks)}** tasks passed" + (f" · {failed} failed" if failed else ""),
        f"⏱ {time_str}",
    ]
    if run.tokens_used:
        lines.append(f"🪙 ~{run.tokens_used // 1000}K tokens")
    if run.replan_count:
        lines.append(f"🔄 {run.replan_count} replan(s)")
    lines.append(f"📁 `{run.work_dir}`")
    if run.branch_name:
        lines.append(f"🌿 Branch: `{run.branch_name}`")
    task_list = "\n".join(
        f"  {'✅' if t.status == TaskStatus.PASSED else '❌'} {t.title}" for t in run.tasks
    )
    if task_list:
        lines.append(f"\n**Tasks:**\n{task_list}")
    return "\n".join(lines)
