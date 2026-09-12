"""Data models and constants for the task runner."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Mapping

# ── Constants ──

MAX_RETRIES = 3
MAX_RECOVERIES = 2  # process crash recovery budget per task
MAX_REPLAN = 2  # plan revision attempts after task exhausts retries
MAX_TOTAL_TASKS = 50  # hard cap on total tasks (including replans)
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


class ReviewFixState(str, enum.Enum):
    """Persisted lifecycle for a review finding fix task."""

    DRAFT = "draft"
    PLANNING = "planning"
    AWAITING_GROUP_CONFIRMATION = "awaiting_group_confirmation"
    RUNNING = "running"
    AWAITING_VALIDATION = "awaiting_validation"
    READY_TO_APPLY = "ready_to_apply"
    AWAITING_COMMIT = "awaiting_commit"
    COMMITTED = "committed"
    AWAITING_PUSH = "awaiting_push"
    PUSHED = "pushed"
    REREVIEWING = "rereviewing"
    DONE = "done"
    PAUSED = "paused"
    FAILED = "failed"
    BLOCKED_MODEL_RESOLUTION = "blocked_model_resolution"
    BLOCKED_DIRTY_OVERLAP = "blocked_dirty_overlap"
    BLOCKED_VALIDATION = "blocked_validation"


class ReviewFixTargetMode(str, enum.Enum):
    """Target mode selected for the user-approved Apply operation."""

    CURRENT_BRANCH = "current_branch"


class ReviewFixGroupState(str, enum.Enum):
    """Lifecycle of one dependency group inside a review-fix task."""

    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    EXECUTING = "executing"
    VALIDATING = "validating"
    READY_TO_APPLY = "ready_to_apply"
    APPLIED = "applied"
    COMMITTED = "committed"


@dataclass
class ReviewFixFindingSnapshot:
    """Immutable review finding data copied into a fix task."""

    key: str
    title: str = ""
    severity: str = ""
    body: str = ""
    file_path: str = ""
    line: int | None = None
    end_line: int | None = None
    fingerprint: str = ""
    suggested_fix: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReviewFixFindingSnapshot":
        return cls(
            key=str(raw.get("key", "")),
            title=str(raw.get("title", "")),
            severity=str(raw.get("severity", "")),
            body=str(raw.get("body", "")),
            file_path=str(raw.get("file_path", raw.get("path", ""))),
            line=int(raw["line"]) if raw.get("line") is not None else None,
            end_line=int(raw["end_line"]) if raw.get("end_line") is not None else None,
            fingerprint=str(raw.get("fingerprint", "")),
            suggested_fix=str(raw.get("suggested_fix", "")),
        )


@dataclass
class ReviewFixTargetSnapshot:
    """Identity and dirty-state snapshot used by target CAS checks."""

    mode: ReviewFixTargetMode = ReviewFixTargetMode.CURRENT_BRANCH
    repo_root: str = ""
    target_path: str = ""
    target_ref: str = ""
    branch_name: str = ""
    head_sha: str = ""
    dirty_fingerprint: str = ""
    tracked_paths: list[str] = field(default_factory=list)
    untracked_paths: list[str] = field(default_factory=list)
    upstream: str = ""
    remote: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "ReviewFixTargetSnapshot":
        raw = raw or {}
        try:
            mode = ReviewFixTargetMode(
                str(raw.get("mode", ReviewFixTargetMode.CURRENT_BRANCH.value))
            )
        except ValueError:
            mode = ReviewFixTargetMode.CURRENT_BRANCH
        return cls(
            mode=mode,
            repo_root=str(raw.get("repo_root", "")),
            target_path=str(raw.get("target_path", "")),
            target_ref=str(raw.get("target_ref", "")),
            branch_name=str(raw.get("branch_name", "")),
            head_sha=str(raw.get("head_sha", raw.get("target_head_sha", ""))),
            dirty_fingerprint=str(raw.get("dirty_fingerprint", "")),
            tracked_paths=[
                str(path) for path in raw.get("tracked_paths", []) if isinstance(path, str)
            ],
            untracked_paths=[
                str(path) for path in raw.get("untracked_paths", []) if isinstance(path, str)
            ],
            upstream=str(raw.get("upstream", "")),
            remote=str(raw.get("remote", "")),
        )


@dataclass
class ReviewFixModelResolution:
    """Concrete model pin captured before a review-fix task can execute."""

    requested_model: str = ""
    provider: str = ""
    resolved_model_id: str = ""
    advertised_model_ids: list[str] = field(default_factory=list)
    resolved_at: float = 0.0

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "ReviewFixModelResolution":
        raw = raw or {}
        return cls(
            requested_model=str(raw.get("requested_model", "")),
            provider=str(raw.get("provider", "")),
            resolved_model_id=str(raw.get("resolved_model_id", "")),
            advertised_model_ids=[
                str(model)
                for model in raw.get("advertised_model_ids", [])
                if isinstance(model, str)
            ],
            resolved_at=float(raw.get("resolved_at", 0.0) or 0.0),
        )


@dataclass
class ReviewFixValidationRun:
    """One full test/build validation attempt for a dependency group."""

    validation_id: str = ""
    group_id: str = ""
    group_revision: int = 0
    kind: str = ""
    command: list[str] = field(default_factory=list)
    exit_code: int | None = None
    passed: bool = False
    artifact_path: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_secs: float = 0.0

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReviewFixValidationRun":
        exit_code = raw.get("exit_code")
        return cls(
            validation_id=str(raw.get("validation_id", "")),
            group_id=str(raw.get("group_id", "")),
            group_revision=int(raw.get("group_revision", 0) or 0),
            kind=str(raw.get("kind", "")),
            command=[str(value) for value in raw.get("command", [])],
            exit_code=int(exit_code) if exit_code is not None else None,
            passed=bool(raw.get("passed", False)),
            artifact_path=str(raw.get("artifact_path", "")),
            started_at=float(raw.get("started_at", 0.0) or 0.0),
            finished_at=float(raw.get("finished_at", 0.0) or 0.0),
            duration_secs=float(raw.get("duration_secs", 0.0) or 0.0),
        )


@dataclass
class ReviewFixDependencyGroup:
    """A hard-atomic or soft dependency group proposed by the planner."""

    group_id: str
    finding_keys: list[str] = field(default_factory=list)
    hard_edges: list[dict[str, str]] = field(default_factory=list)
    soft_edges: list[dict[str, str]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    affected_files: list[str] = field(default_factory=list)
    hard: bool = False
    state: ReviewFixGroupState = ReviewFixGroupState.PROPOSED
    revision: int = 0
    candidate_patch_id: str = ""
    candidate_base_sha: str = ""
    candidate_head_sha: str = ""
    patch_path: str = ""
    diff_path: str = ""
    validation_runs: list[ReviewFixValidationRun] = field(default_factory=list)
    apply_confirmed: bool = False
    applied_at: float = 0.0
    commit_hash: str = ""
    commit_message: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReviewFixDependencyGroup":
        try:
            state = ReviewFixGroupState(str(raw.get("state", ReviewFixGroupState.PROPOSED.value)))
        except ValueError:
            state = ReviewFixGroupState.PROPOSED
        return cls(
            group_id=str(raw.get("group_id", "")),
            finding_keys=[str(value) for value in raw.get("finding_keys", [])],
            hard_edges=[
                dict(edge) for edge in raw.get("hard_edges", []) if isinstance(edge, Mapping)
            ],
            soft_edges=[
                dict(edge) for edge in raw.get("soft_edges", []) if isinstance(edge, Mapping)
            ],
            reasons=[str(value) for value in raw.get("reasons", [])],
            affected_files=[str(value) for value in raw.get("affected_files", [])],
            hard=bool(raw.get("hard", False)),
            state=state,
            revision=int(raw.get("revision", 0) or 0),
            candidate_patch_id=str(raw.get("candidate_patch_id", "")),
            candidate_base_sha=str(raw.get("candidate_base_sha", "")),
            candidate_head_sha=str(raw.get("candidate_head_sha", "")),
            patch_path=str(raw.get("patch_path", "")),
            diff_path=str(raw.get("diff_path", "")),
            validation_runs=[
                ReviewFixValidationRun.from_dict(item)
                for item in raw.get("validation_runs", [])
                if isinstance(item, Mapping)
            ],
            apply_confirmed=bool(raw.get("apply_confirmed", False)),
            applied_at=float(raw.get("applied_at", 0.0) or 0.0),
            commit_hash=str(raw.get("commit_hash", "")),
            commit_message=str(raw.get("commit_message", "")),
        )


@dataclass
class ReviewFixChatLink:
    """Link metadata for a chat that can discuss, but not mutate, a fix task."""

    session_key: str = ""
    slot_id: str = ""
    review_run_id: str = ""
    task_id: str = ""
    revision: int = 0

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "ReviewFixChatLink":
        raw = raw or {}
        return cls(
            session_key=str(raw.get("session_key", "")),
            slot_id=str(raw.get("slot_id", "")),
            review_run_id=str(raw.get("review_run_id", "")),
            task_id=str(raw.get("task_id", "")),
            revision=int(raw.get("revision", 0) or 0),
        )


@dataclass
class ReviewFixGitRecord:
    """Candidate/destination Git records retained for review and Git actions."""

    candidate_worktree_path: str = ""
    candidate_branch: str = ""
    candidate_ref: str = ""
    destination_worktree_path: str = ""
    destination_branch: str = ""
    proposed_branch: str = ""
    confirmed_branch: str = ""
    remote: str = ""
    upstream: str = ""
    push_preview: dict[str, Any] = field(default_factory=dict)
    push_result: dict[str, Any] = field(default_factory=dict)
    rereview_run_id: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "ReviewFixGitRecord":
        raw = raw or {}
        return cls(
            candidate_worktree_path=str(raw.get("candidate_worktree_path", "")),
            candidate_branch=str(raw.get("candidate_branch", "")),
            candidate_ref=str(raw.get("candidate_ref", "")),
            destination_worktree_path=str(raw.get("destination_worktree_path", "")),
            destination_branch=str(raw.get("destination_branch", "")),
            proposed_branch=str(raw.get("proposed_branch", "")),
            confirmed_branch=str(raw.get("confirmed_branch", "")),
            remote=str(raw.get("remote", "")),
            upstream=str(raw.get("upstream", "")),
            push_preview=(
                dict(raw.get("push_preview", {}))
                if isinstance(raw.get("push_preview"), Mapping)
                else {}
            ),
            push_result=(
                dict(raw.get("push_result", {}))
                if isinstance(raw.get("push_result"), Mapping)
                else {}
            ),
            rereview_run_id=str(raw.get("rereview_run_id", "")),
        )


@dataclass
class ReviewFixAuditEvent:
    """Bounded, redacted audit record for a review-fix transition/action."""

    action: str = ""
    from_state: str = ""
    to_state: str = ""
    revision: int = 0
    actor: str = "dashboard"
    timestamp: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReviewFixAuditEvent":
        return cls(
            action=str(raw.get("action", "")),
            from_state=str(raw.get("from_state", "")),
            to_state=str(raw.get("to_state", "")),
            revision=int(raw.get("revision", 0) or 0),
            actor=str(raw.get("actor", "dashboard")),
            timestamp=float(raw.get("timestamp", 0.0) or 0.0),
            details=dict(raw.get("details", {})) if isinstance(raw.get("details"), Mapping) else {},
        )


@dataclass
class ReviewFixMetadata:
    """Durable review-fix state attached optionally to a generic Project."""

    review_run_id: str = ""
    pr_url: str = ""
    source_head_sha: str = ""
    selected_finding_keys: list[str] = field(default_factory=list)
    finding_snapshots: list[ReviewFixFindingSnapshot] = field(default_factory=list)
    state: ReviewFixState = ReviewFixState.DRAFT
    revision: int = 0
    target: ReviewFixTargetSnapshot = field(default_factory=ReviewFixTargetSnapshot)
    model: ReviewFixModelResolution = field(default_factory=ReviewFixModelResolution)
    groups: list[ReviewFixDependencyGroup] = field(default_factory=list)
    git: ReviewFixGitRecord = field(default_factory=ReviewFixGitRecord)
    chat: ReviewFixChatLink = field(default_factory=ReviewFixChatLink)
    blocked_reason: str = ""
    attempts: dict[str, int] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    diff_paths: list[str] = field(default_factory=list)
    artifact_paths: list[str] = field(default_factory=list)
    audit_log: list[ReviewFixAuditEvent] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "ReviewFixMetadata":
        raw = raw or {}
        try:
            state = ReviewFixState(str(raw.get("state", ReviewFixState.DRAFT.value)))
        except ValueError:
            state = ReviewFixState.DRAFT
        return cls(
            review_run_id=str(raw.get("review_run_id", "")),
            pr_url=str(raw.get("pr_url", "")),
            source_head_sha=str(raw.get("source_head_sha", "")),
            selected_finding_keys=[str(value) for value in raw.get("selected_finding_keys", [])],
            finding_snapshots=[
                ReviewFixFindingSnapshot.from_dict(item)
                for item in raw.get("finding_snapshots", [])
                if isinstance(item, Mapping)
            ],
            state=state,
            revision=int(raw.get("revision", 0) or 0),
            target=ReviewFixTargetSnapshot.from_dict(raw.get("target")),
            model=ReviewFixModelResolution.from_dict(raw.get("model")),
            groups=[
                ReviewFixDependencyGroup.from_dict(item)
                for item in raw.get("groups", [])
                if isinstance(item, Mapping)
            ],
            git=ReviewFixGitRecord.from_dict(raw.get("git")),
            chat=ReviewFixChatLink.from_dict(raw.get("chat")),
            blocked_reason=str(raw.get("blocked_reason", "")),
            attempts={
                str(key): int(value)
                for key, value in raw.get("attempts", {}).items()
                if isinstance(key, str) and isinstance(value, (int, float))
            },
            logs=[str(value) for value in raw.get("logs", [])],
            diff_paths=[str(value) for value in raw.get("diff_paths", [])],
            artifact_paths=[str(value) for value in raw.get("artifact_paths", [])],
            audit_log=[
                ReviewFixAuditEvent.from_dict(item)
                for item in raw.get("audit_log", [])
                if isinstance(item, Mapping)
            ],
            created_at=float(raw.get("created_at", 0.0) or 0.0),
            updated_at=float(raw.get("updated_at", 0.0) or 0.0),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-safe data while preserving enum values."""
        return _json_value(self)


def _json_value(value: Any) -> Any:
    """Convert nested dataclasses/enums without exposing non-JSON objects."""
    if isinstance(value, enum.Enum):
        return value.value
    if is_dataclass(value):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


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
    git_enabled: bool = (
        True  # False when the workspace is not a git repo (run in place, no git ops)
    )
    lessons_learned: list[str] = field(default_factory=list)
    mode: str = "quick"  # "quick" (text only) | "spec" (has spec file/content)
    source_spec: str = ""  # original input text or spec content
    skip_planning: bool = False  # true = plan + execute immediately
    auto_approve: bool = (
        False  # per-run trust intent (UI flag); the live, expiring, audited grant is held in SafetyOverride (scope taskrunner:{task_id}:autoapprove)
    )
    workflow_run_id: str = ""  # shared workflow-history run driven by TaskRunner
    workflow_id: str = ""  # saved definition provenance, when invoked by name
    workflow_slug: str = ""
    workflow_revision: int = 0
    derived_from_workflow_id: str = ""  # saved ancestor after the plan is adapted
    derived_from_revision: int = 0
    review_fix: ReviewFixMetadata | None = (
        None  # optional review-fix state; generic runs leave this absent
    )
    revision: int = 0  # monotonic review-fix CAS revision; generic runs keep the default
    execution_mode: str = "standard"  # "standard" | "review_fix"
    commit_policy: str = "per_task"  # "per_task" | "manual_group"


# ``NotifyCallback`` moved to ``task_reporter``, which owns the notification
# contract and now carries a union of the session-aware and legacy shapes. Two
# aliases of that name with different shapes is how a caller ends up annotated
# against the one the reporter does not accept.
