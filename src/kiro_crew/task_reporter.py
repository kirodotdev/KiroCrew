"""Reporting, progress persistence, and output formatting for the task runner."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from kiro_crew.security import redact_exfiltration_urls
from kiro_crew.task_models import (
    PROGRESS_FILE,
    NotifyCallback,
    Project,
    Task,
    TaskStatus,
)
from kiro_crew.task_planner import group_parallel_tasks

logger = logging.getLogger(__name__)


# ── Notifications ──


async def notify(
    title: str,
    body: str,
    run: Project | None = None,
    callback: NotifyCallback | None = None,
) -> None:
    """Send notification via callback if registered."""
    if run:
        label, _ = redact_exfiltration_urls(run.name or Path(run.spec_path).stem)
        title = f"[{label}] {title}"
    title = redact_exfiltration_urls(title)[0]
    body = redact_exfiltration_urls(body)[0]
    if callback:
        try:
            await callback(title, body, run.task_id if run else "")
        except Exception:
            logger.debug("Notification failed", exc_info=True)


# ── Progress Persistence ──


def save_progress(run: Project) -> None:
    """Write TASK_PROGRESS.md next to the spec file."""
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
