"""Data models and constants for the task runner."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Awaitable, Callable

# ── Constants ──

MAX_RETRIES = 3
MAX_RECOVERIES = 2  # process crash recovery budget per task
MAX_REPLAN = 2  # plan revision attempts after task exhausts retries
MAX_TOTAL_TASKS = 50  # hard cap on total tasks (including replans)
CONTEXT_COMPACT_PCT = 80.0
SESSION_PREFIX = "taskrunner"
TEST_TIMEOUT = 5400  # 90 min for test command
PROGRESS_FILE = "TASK_PROGRESS.md"
STALL_TIMEOUT = 3600  # 60 min with no task progress → notify
STALL_CANCEL_TIMEOUT = 7200  # 2 hours → watchdog resets stuck session
DEFAULT_TOKEN_BUDGET = 0  # 0 = unlimited


class TaskStatus(enum.Enum):
    """Lifecycle status for a task."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    REVIEWING = "reviewing"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


@dataclass
class Task:
    """A single task decomposed from the spec."""

    index: int
    title: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    error: str = ""
    result: str = ""
    requires_approval: bool = False
    force_approval: bool = False  # blocks even in YOLO mode
    depends_on: list[int] = field(default_factory=list)
    priority: str = "medium"
    story_points: int = 0
    task_type: str = "original"  # 'original' | 'fix'
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0


@dataclass
class WorkingMemory:
    """Structured state that survives context compaction."""

    files_changed: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """Render as LLM-readable text."""
        parts: list[str] = ["## Working Memory"]
        if self.files_changed:
            parts.append("### Files Changed")
            for f in self.files_changed[-20:]:
                parts.append(f"- {f}")
        if self.decisions:
            parts.append("### Key Decisions")
            for d in self.decisions[-10:]:
                parts.append(f"- {d}")
        if self.blockers:
            parts.append("### Blockers")
            for b in self.blockers[-5:]:
                parts.append(f"- {b}")
        return "\n".join(parts) if len(parts) > 1 else ""

    def update_from_result(self, result: str) -> None:
        """Extract file paths and decisions from task result text."""
        for line in result.splitlines():
            stripped = line.strip()
            if any(
                stripped.startswith(p)
                for p in ("Created ", "Modified ", "Updated ", "Deleted ", "Wrote ")
            ):
                self.files_changed.append(stripped[:200])


@dataclass
class Project:
    """Tracks the full task execution."""

    spec_path: str
    spec_content: str
    tasks: list[Task] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    status: str = "pending"  # pending, planned, running, completed, failed, cancelled
    original_input: str = ""  # raw user text that produced this plan
    source: str = ""  # "text", "spec", "file", or "chat"
    current_task: int = 0
    error: str = ""
    tokens_used: int = 0
    replan_count: int = 0
    memory: WorkingMemory = field(default_factory=WorkingMemory)
    task_id: str = ""
    name: str = ""  # human-readable name (optional, display label)
    work_dir: str = ""
    last_task_time: float = 0.0
    branch_name: str = ""
    base_branch: str = ""
    commit_hashes: list[str] = field(default_factory=list)
    worktree_path: str = ""
    repo_root: str = ""  # original repo root (for worktree cleanup)
    git_enabled: bool = True  # False when the workspace is not a git repo (run in place, no git ops)
    lessons_learned: list[str] = field(default_factory=list)
    mode: str = "quick"  # "quick" (text only) | "spec" (has spec file/content)
    source_spec: str = ""  # original input text or spec content
    skip_planning: bool = False  # true = plan + execute immediately


# Callback for notifications: (title, body, task_id) -> None
NotifyCallback = Callable[[str, str, str], Awaitable[None]]
