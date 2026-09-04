# TaskRunner Module

## Overview

Autonomous task executor that reads a spec file, decomposes it into
ordered steps via LLM, and executes each step through ACP sessions
with test verification, retries, and progress checkpointing.

TaskRunner is a product-layer superset of the workflow run substrate. It keeps
ownership of planning, approval gates, retries, replanning, test verification,
git/worktree coordination, persistence, pause/resume, and cleanup. The workflow
service supplies the common run identity, event history, source/provenance, and
saved-definition invocation used by all workflow-like execution. It does not
replace or reinterpret TaskRunner execution.

Supports multiple concurrent tasks, interactive tool approval, per-step session isolation with full memory injection, git-coordinated step commits and reverts, independent review via actual diffs, cycle detection, disk persistence across restarts, activity-aware stall detection, and semaphore-bounded parallel execution to prevent resource exhaustion.

## Module Architecture

The task runner is split into an orchestrator plus 4 focused helper modules under `src/kiro_crew/`:

```
taskrunner.py        (orchestrator)
├── task_models.py   (data models + constants)
├── task_planner.py  (LLM decomposition + task parsing + parallel grouping)
├── task_executor.py (task execution + retries + tests + self-review)
└── task_reporter.py (status + notifications + progress checkpoints + resume context)
```

### Module Responsibilities

| Module | Class/Functions | Responsibility |
|--------|----------------|----------------|
| `task_models.py` | `TaskStatus`, `Task`, `WorkingMemory`, `Project`, `ReviewFixState`, `ReviewFixMetadata`, `NotifyCallback`, constants | Shared data types and configuration constants, including the review-fix state machine models |
| `task_planner.py` | `decompose()`, `parse_tasks()`, `normalize_cross_group_deps()`, `group_parallel_tasks()`, `plan_to_chat_context()`, `update_plan_tasks()`, `auto_name()` | LLM spec decomposition, task parsing, dependency normalization, parallel grouping, plan-to-chat formatting |
| `task_executor.py` | `execute_task()`, `build_task_prompt()`, `self_review()`, `run_tests()`, `check_context()` | Task execution with retry/recovery budgets, prompt building, context compaction, test running, self-review |
| `task_reporter.py` | `notify()`, `build_status()`, `save_progress()`, `load_checkpoint()`, `build_resume_context()`, `format_completion_summary()` | Notifications, status reporting, TASK_PROGRESS.md checkpointing, resume context |
| `taskrunner.py` | `TaskRunner` | Orchestrator — owns run lifecycle, `_try_replan`, watchdog, run persistence (`_persist_runs`/`_load_runs`); delegates decomposition/execution/reporting to the helper modules |

### Workflow substrate attachment

The gateway attaches its singleton `WorkflowService` and `TaskRunner` after both
are constructed. The dependency is optional so CLI, tests, and headless callers
retain the existing TaskRunner behavior when no workflow service is present.
Workflow publication is best-effort: an unavailable registry cannot fail task
planning or execution. Every host lifecycle checkpoint — registration, source,
rebind, phase/step events, pause, terminal state, and deletion — awaits the workflow
service's off-loop durable mirror, so a maximum-size YAML plan cannot block the
gateway event loop while its shared run record is written.

The chat-to-plan dashboard route registers its placeholder project with this
port before applying steps, and the dashboard delete route delegates to
`TaskRunner.delete_run`, so those established entrypoints cannot leave an
unlinked or orphaned common run.

Each `Project` persists `workflow_run_id` plus optional saved-definition
provenance (`workflow_id`, `workflow_slug`, `workflow_revision`). Planning
registers one host-driven workflow run, publishes the exact canonical plan YAML,
and pauses that same run while the project awaits execution. `execute_plan`,
retry, and restart recovery rebind the existing run rather than allocating a
second identity. A terminal project marks the linked workflow run terminal;
deleting the project removes the linked workflow record. The common terminal
transition occurs only after TaskRunner has written its durable state and
completed git/worktree finalization, so the shared view cannot report completion
ahead of the product-layer owner.

Task execution emits common workflow lifecycle and agent-step events around the existing `_execute_tasks` path. Step result summaries use the workflow event contract's bounded summary field; complete TaskRunner results remain in TaskRunner storage. Cancellation binds the workflow handle to the actual TaskRunner asyncio task. The workflow handle disables chat completion injection because TaskRunner retains its existing reporting and notification path, preventing duplicate completion messages.

Direct `run()` setup performs workflow registration, task binding, and the initial
TaskRunner registry write inside the same lifecycle `try` block as execution. A
cancellation at any of those awaits therefore reaches the established cleanup and
terminal-projection path; neither the TaskRunner project nor its shared workflow run
can remain `running` after its driver task exits.

Planning treats workflow publication plus the first TaskRunner registry write as one
ownership handoff. If cancellation or persistence failure occurs before that handoff
commits, TaskRunner removes the in-memory placeholder, its owned plan directory, and
the linked workflow run. A workflow identity therefore cannot survive as active when
the corresponding project was never returned or durably registered.

Retry and recovery preserve the linked workflow identity when it remains available.
If eviction or an incompatible restored record requires a replacement, TaskRunner
durably writes the replacement `workflow_run_id` before rebinding or publishing more
progress. A gateway crash therefore cannot leave the project pointing at the rejected
identity while the replacement survives as an orphaned workflow run.

Background admission uses the same ownership rule: its placeholder is durably written
before the execution task is registered, and rollback deletes the linked workflow run
before removing the placeholder. Cancellation at that persistence await cannot leave
an active workflow with no TaskRunner task capable of driving it.

Saved definitions whose immutable `format` is `task-plan` are invoked through
`TaskRunner.start_workflow_definition`. The saved YAML is parsed exactly; it is
not re-decomposed by an LLM. The resulting project then follows the normal
TaskRunner execution pipeline, including `requires_approval` and
`force_approval`. Free-form `/workflow` input is recorded as the project's
original input for run context and provenance; it does not mutate the saved plan.
If execution admission rejects the invocation, TaskRunner deletes the newly planned
project and its linked workflow run, then returns the admission error to the workflow
caller so chat can complete normally without exposing an orphaned run.

### Import Graph (no cycles)

```
task_models ← task_planner
task_models ← task_executor (+ task_planner for parallel grouping)
task_models ← task_reporter (+ task_planner for parallel grouping)
task_models ← taskrunner (+ all above modules)
```

### Backward Compatibility

The domain model was renamed `Step` → `Task`, `StepStatus` → `TaskStatus`, and
`TaskRun` → `Project`. `taskrunner.py` re-exports the real symbols from
`task_models` and also defines back-compat aliases so existing imports keep working:
```python
from kiro_crew.task_models import Task, TaskStatus, WorkingMemory, Project, NotifyCallback  # noqa: F401

# ── Backward-compat re-exports ──
Step = Task
StepStatus = TaskStatus
TaskRun = Project
```

These files import from `kiro_crew.taskrunner` and require no changes:
- `dashboard/handlers/taskrunner.py` → `StepStatus`, `TaskRun`
- `dashboard/server.py` → `TaskRunner`
- `dashboard/state.py` → `TaskRunner`
- `git_coord.py` → `Step`, `TaskRun`
- `slack/gateway.py` → `TaskRunner`
- `slack/handler.py` → `TaskRunner`
- `cli.py` → `TaskRunner`

## Public API

### `TaskRunner`

```python
class TaskRunner:
    def __init__(
        self,
        sessions: SessionManager,
        context_builder: ContextBuilder | None = None,
        on_notify: NotifyCallback | None = None,
        auto_test: bool = True,
        auto_commit: bool = False,
        work_dir: Path | None = None,
        conversation_log: ConversationLog | None = None,
        consolidator: HistoryConsolidator | None = None,
        lesson_store: LessonStore | None = None,
        fresh: bool = False,
        global_timeout: float = 0.0,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
        on_approval: Callable[[Task], Awaitable[bool]] | None = None,
        max_parallel_steps: int | None = None,  # None/0 -> host-safe ceiling
        workspace_dir: str = "",
        workflow_service: WorkflowRunPublisher | None = None,
    ) -> None: ...

    # Delegates to module-level functions: task_planner.decompose(),
    # task_executor.execute_task()/self_review(), task_reporter.build_status()

    def attach_workflow_service(self, service: WorkflowRunPublisher | None) -> None
    async def run(self, spec_path: str | Path, task_id: str = "", name: str = "", source: str = "", workspace_dir: str = "", auto_approve: bool = False) -> Project
    async def start_background(self, spec_path: str | Path, agent: str = "", name: str = "", source: str = "", workspace_dir: str = "", auto_approve: bool = False, *, session_key: str = "") -> str
    def cancel(self, task_id: str | None = None) -> None  # None = cancel all
    def status(self) -> dict

    @property
    def running(self) -> bool
    @property
    def current_run(self) -> TaskRun | None

    # Mutation APIs await fsync-backed persistence off the event loop.
    async def update_plan(task_id: str, tasks: list[dict]) -> TaskRun
    async def update_task(task_id: str, index: int, updates: dict) -> dict
    async def execute_plan(task_id: str, agent: str = "", fresh: bool = False, workspace_dir: str = "", auto_approve: bool = False) -> str
    async def retry_from_task(task_id: str, from_task: int, agent: str = "") -> str
    async def delete_run(task_id: str) -> bool

    # Review-fix lifecycle — every write goes through mutate_review_fix (CAS)
    def get_review_fix(task_id: str) -> Project
    async def create_review_fix(metadata: ReviewFixMetadata, *, task_id: str = "", name: str = "", spec_content: str = "", source: str = "code_review_sage", tasks: list[Task] | None = None) -> Project
    async def execute_review_fix(task_id: str, *, agent: str = "", fresh: bool = False, auto_approve: bool = False) -> str
    async def mutate_review_fix(task_id: str, *, expected_revision: int, action: str, mutate: Callable[[ReviewFixMetadata], None], to_state=None, expected_state=None, group_id: str = "", expected_group_revision: int | None = None, expected_target_fingerprint: str = "") -> Project

    # Internal but accessed by handlers for read-only projection
    _runs: dict[str, TaskRun]
    async def _apersist_runs() -> None
    _persist_runs() -> None  # synchronous compatibility/testing helper only
```

### Task Source & Visibility

`TaskRun.source` tracks where a task was started from. The dashboard Tasks page
filters runs by source to avoid showing cron-triggered background tasks:

```python
dashboard_sources = {"text", "spec", "file", "chat", "dashboard", "mcp", "yaml"}
```

| Entry Point | Source Value | Visible on Tasks Page |
|-------------|-------------|----------------------|
| Dashboard UI | `"dashboard"` | ✅ |
| Slack `run <path>` | `"chat"` | ✅ |
| MCP `task_run` tool | `"mcp"` | ✅ |
| CLI `kirocrew run` | `"file"` (default) | ✅ |
| `plan()` API | `"text"`, `"spec"`, `"file"` | ✅ |
| Cron job | must pass `source="cron"` | ❌ (filtered out) |

### Data Types

Named `TaskStatus`/`Task`/`Project` in `task_models.py`; `StepStatus`/`Step`/`TaskRun`
remain as back-compat aliases exported from `taskrunner.py`.

```python
class TaskStatus(Enum):
    PENDING, IN_PROGRESS, REVIEWING, PASSED, FAILED, SKIPPED, CANCELLED

@dataclass
class Task:
    index: int
    title: str
    description: str
    status: TaskStatus = PENDING
    attempts: int = 0
    error: str = ""
    result: str = ""  # updated during streaming (partial results visible)
    requires_approval: bool = False
    force_approval: bool = False  # blocks even in YOLO mode
    depends_on: list[int] = field(default_factory=list)

@dataclass
class Project:
    spec_path: str
    spec_content: str
    tasks: list[Task]
    started_at: float
    finished_at: float
    status: str  # pending, planned, running, completed, failed, cancelled
    current_task: int
    error: str
    tokens_used: int
    replan_count: int
    memory: WorkingMemory
    task_id: str
    work_dir: str
    last_task_time: float  # tracks activity for watchdog
    branch_name: str       # git branch for task (e.g. kirocrew/task/{task_id})
    base_branch: str       # original branch before task started
    commit_hashes: list[str]  # per-step commit SHAs
    worktree_path: str     # git worktree path (empty if git init)
    repo_root: str         # original repo root (for worktree cleanup)
    auto_approve: bool = False  # per-run trust: auto-approve tool permission requests
                                # (deny-lists + force_approval gates still apply)
    workflow_run_id: str = ""   # shared workflow-run identity
    workflow_id: str = ""       # exact saved-definition provenance
    workflow_slug: str = ""
    workflow_revision: int = 0
    derived_from_workflow_id: str = ""  # saved ancestor after an edit or replan
    derived_from_revision: int = 0
    review_fix: ReviewFixMetadata | None = None  # governed review-fix state; absent on generic runs
    revision: int = 0                  # monotonic review-fix CAS revision
    execution_mode: str = "standard"   # "standard" | "review_fix"
    commit_policy: str = "per_task"    # "per_task" | "manual_group"
```

## Concurrent Tasks

- `_runs: dict[str, TaskRun]` — keyed by task_id
- `_tasks: dict[str, asyncio.Task]` — background asyncio tasks
- `start_background()` accepts optional `agent` param, returns a collision-resistant task ID (`{spec_stem}_{time_ns}`)
- `_start_lock` serializes concurrency admission, completed-run pruning, ID allocation, durable planning-placeholder persistence, and `_tasks` registration
- All `get_or_create()` calls pass `agent=self._agent` so the task runs with the specified agent
- Each step gets its own session: `taskrunner:{task_id}:task{N}` (fresh per step, reset after)
- Each task gets its own work dir: `{work_dir}/{spec_stem}/`
- `cancel(task_id)` cancels specific task; `cancel()` cancels all
- Completed cron runs are pruned on new start; other completed runs retain bounded history.
- `_tasks` cleaned in `finally` block (no leaks)
- `start_background()` and `execute_plan()` enforce `_MAX_CONCURRENT_TASKS` before changing run state, so rejected admission cannot leave a partially started run.
- Replanned steps also reset sessions after execution (no leaks in `_try_replan`)

## Pause / Resume

Tasks can be paused and resumed without losing progress:

- `pause(task_id)` — sets `run.status = "pausing"` and cancels the asyncio task gracefully; the `_execute()` `finally` block promotes `"pausing"` → `"paused"` after session cleanup
- Resume is not a dedicated method — call `execute_plan(task_id, agent="", fresh=False)` to restart a run whose status is `"planned"`, `"paused"`, `"cancelled"`, or `"failed"`. It resets incomplete (non-passed/non-skipped) tasks to `PENDING` and re-runs from there (with `fresh=True`, resets all tasks)
- Paused status visible in dashboard UI as distinct color/icon
- API: `POST /api/taskrunner/{task_id}/pause`, `POST /api/taskrunner/{task_id}/execute` (resume/restart) — there is no `/resume` route

### Crash Recovery

On gateway restart, any task with `status == "running"` is automatically transitioned to `"paused"`:

- Prevents zombie tasks that appear running but have no backing asyncio task
- User can resume manually from dashboard
- Persisted via `runs.json` — status survives restart

### Force Approval Gates

`task_executor.execute_single_task()` evaluates task-level approval before agent execution.

- With an `on_approval` callback, either `requires_approval` or `force_approval` prompts the owning surface; a denial pauses the project for editing.
- Without that callback, `requires_approval` logs a warning and continues, while `force_approval` fails closed and prevents replanning around the gate.
- `cli_server.py` constructs the standalone `kirocrew run TASK.md` runner without an approval callback. Use `force_approval`, not `requires_approval`, for an action that must not execute unattended.
- The dashboard supplies the callback and renders Approve/Deny controls in the project detail view.

## Review-Fix Lifecycle

A review-fix run is a governed Task Runner project that applies Code Review Sage
findings in a retained candidate worktree. `Project.execution_mode` is
`"review_fix"` and `Project.commit_policy` is `"manual_group"` (generic runs are
`"standard"` / `"per_task"`): the per-step auto-commit in
[Git Coordination](#git-coordination) does not run, and the candidate reaches the
target only through explicit user-confirmed actions. The Sage-side endpoints and
git mechanics live in `apps/builtins/code_review_sage/backend/fix_tasks.py`,
`review_fix.py`, and `review_fix_git.py`; the Task Runner owns the state.

### Persisted state model

`Project.review_fix` carries a `ReviewFixMetadata` record (`task_models.py`)
whose `state` is a `ReviewFixState`. Every write is routed through
`TaskRunner.mutate_review_fix` — no caller mutates fix state directly.

`ReviewFixState` (task level):

| Group | States |
|-------|--------|
| Flow | `DRAFT`, `PLANNING`, `AWAITING_GROUP_CONFIRMATION`, `RUNNING`, `AWAITING_VALIDATION`, `READY_TO_APPLY`, `AWAITING_COMMIT`, `COMMITTED`, `AWAITING_PUSH`, `PUSHED`, `REREVIEWING` |
| Terminal | `DONE` |
| Halted | `PAUSED`, `FAILED` |
| Blocked | `BLOCKED_MODEL_RESOLUTION`, `BLOCKED_DIRTY_OVERLAP`, `BLOCKED_VALIDATION` |

Each dependency group carries its own coexisting `ReviewFixGroupState`
(`PROPOSED`, `CONFIRMED`, `VALIDATING`, `READY_TO_APPLY`, `APPLIED`, `COMMITTED`)
on `ReviewFixDependencyGroup`. Task state and group state advance independently
and both are CAS-checked.

`ReviewFixMetadata` persists `review_run_id`, `pr_url`, `source_head_sha`,
`selected_finding_keys`, `finding_snapshots`, `state`, `revision`, `target`
(`ReviewFixTargetSnapshot`: mode, repo root, branch, HEAD sha, dirty
fingerprint, tracked/untracked paths), `model` (`ReviewFixModelResolution`),
`groups`, `git` (`ReviewFixGitRecord`: candidate worktree/branch, destination
branch, remote/upstream, push preview and result, re-review run id), `chat`,
`blocked_reason`, per-action `attempts`, bounded `logs`, `diff_paths`,
`artifact_paths`, the `audit_log`, and timestamps.

### CAS mutation (`mutate_review_fix`)

```python
async def mutate_review_fix(
    task_id: str, *,
    expected_revision: int,
    action: str,
    mutate: Callable[[ReviewFixMetadata], None],
    to_state: ReviewFixState | None = None,
    expected_state: ReviewFixState | str | None = None,
    group_id: str = "",
    expected_group_revision: int | None = None,
    expected_target_fingerprint: str = "",
) -> Project
```

A stale task revision, task state, target dirty fingerprint, or — when
`group_id` is given — group revision raises `ReviewFixConflict` (surfaced as
HTTP 409 with the current revision/state/group revisions). The mutator runs
against a deep copy, so a failed check leaves the live `Project` untouched and
emits no audit entry. An applied mutation bumps `run.revision` and
`metadata.revision` together to `max(run.revision, metadata.revision) + 1` and
appends a `ReviewFixAuditEvent` (action, from/to state, revision, actor,
redacted details).

### Transitions

| Action | Transition | Guard / side effect |
|--------|-----------|---------------------|
| create | → `DRAFT` | Candidate worktree created from the captured target HEAD before any agent runs. Model resolution failure → `BLOCKED_MODEL_RESOLUTION` (recoverable via the `resolve_model` action); dirty-path overlap between the target and the groups' owned files → `BLOCKED_DIRTY_OVERLAP`. |
| grouping proposed | → `PLANNING` → `AWAITING_GROUP_CONFIRMATION` | `confirm_grouping` / `edit_soft_grouping` stay in-state; hard dependency groups cannot be split. |
| execute (`resume` / `retry`) | → `RUNNING` | Requires confirmed groups and a resolved model pin. `auto_approve` must pass the dashboard `_gate_auto_approve` provenance gate like any other launch. A failed start restores the prior state instead of persisting `RUNNING`. |
| execution complete | → `AWAITING_VALIDATION` | Execution failure → `FAILED`; pause → `PAUSED`. |
| `validate_group` | pass → `READY_TO_APPLY`, fail → `BLOCKED_VALIDATION` | Retry/resume re-enter execution from the blocked state. |
| `apply_group` | `READY_TO_APPLY` → `AWAITING_COMMIT` | The live candidate patch id is compared against the validated `candidate_patch_id`; a mismatch is rejected, so candidate drift after validation cannot be applied. |
| `commit_group` | `AWAITING_COMMIT` → `COMMITTED` | Stages exactly the group's owned paths; the target fingerprint must be unchanged. |
| `push_preview` | `COMMITTED` → `AWAITING_PUSH` | Stores the preview (remote, branch, upstream, commits, files, diverged). |
| `push` | `AWAITING_PUSH` → `PUSHED` | The push re-computes the preview first and rejects when it is stale — HEAD advanced or the upstream moved. Force-push is unavailable. |
| `review_again` | `PUSHED` → `REREVIEWING` | A failed review start restores `PUSHED`. |
| `discard_candidate` | any non-`RUNNING` lifecycle state → `DONE` | The CAS transition happens BEFORE the `git worktree remove --force` side effect, so a rejected transition can never leave a destroyed worktree behind. A failed destroy logs and surfaces in the task logs while the state stays `DONE`. |

`RUNNING` is never a valid discard source, and `DONE` is terminal.

### Model pinning

`resolve_pinned_model` (`review_fix.py`) accepts only a concrete advertised model
id. An empty advertised set is allowed (the repo-wide convention), but `auto` is
rejected: a fix task must keep one concrete model across retries and resumes.

### Target mode and artifacts

`ReviewFixTargetMode` is `CURRENT_BRANCH` only — fixes apply back to the branch
the review ran against.

Group ids are restricted to `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` because they name
patch/log artifact files (`{group_id}.patch`, `{group_id}-{kind}-{ts}.log`) under
the candidate worktree's `.kirocrew-review-fix-artifacts/` directory. Artifact
directories must be non-link and contained in the candidate worktree. Validation
output is redacted (`redact_credentials` + `redact_exfiltration_urls`) and capped
at 256KB (`_MAX_ARTIFACT_BYTES` in `review_fix.py`) before it is written or
persisted.

### Routes and security posture

`register_fix_task_routes` (`fix_tasks.py`) registers only the app-scoped
endpoints — `POST /api/apps/code-review-sage/fix-tasks`,
`GET /api/apps/code-review-sage/fix-tasks/{task_id}`, and
`POST /api/apps/code-review-sage/fix-tasks/{task_id}/review-again`. The
`GET /api/taskrunner/{task_id}/review-fix` and
`POST /api/taskrunner/{task_id}/review-fix/actions` routes are registered by
`dashboard/routes/taskrunner.py` through the `dashboard/handlers/review_fix.py`
adapter, which imports `fix_tasks` through its canonical package path so the
review-fix enums are a single module identity.

`fix_tasks.py` is a registered egress sink: review-fix error bodies quote git
stderr, plan/validation refusals, and agent failures, so `_safe_error` scrubs
them before they reach the dashboard or the audit record. `review_fix.py` and
`review_fix_git.py` are allowlisted instead — they scrub patch output and git
stderr where it is persisted INTO task state, and the user-readable surfaces are
registered sinks downstream (`security_posture.py`).

## Parallel Execution

Parallel groups are throttled to prevent resource exhaustion from simultaneous kiro-cli cold starts. Each kiro-cli cold start spawns MCP server child processes, so concurrent tasks multiply startup pressure.

Every resolved task in a parallel group is dispatched at once and an
`asyncio.Semaphore` caps how many run simultaneously, so a slot freed by a
finished task is refilled immediately (`taskrunner.py`):

```python
sem = asyncio.Semaphore(self._max_parallel_steps)

async def _run_bounded(t: Task) -> bool:
    async with sem:
        return await self._execute_single_task(run, t, history_key, session_key=...)

results = await asyncio.gather(
    *(_run_bounded(t) for t in resolved),
    return_exceptions=True,
)
```

The limit is `self._max_parallel_steps`, computed once in `__init__` as
`min(taskrunner.max_parallel_steps, compute_max_subagents(cfg))`:

- `compute_max_subagents` is the **host-safe ceiling** (derived from
  `agent.subagent_auto_max`, clamped to host memory/CPU headroom). It exists to
  prevent OOM, so it is always the upper bound.
- A positive `taskrunner.max_parallel_steps` may only **lower** it (intentional
  throttling for cost / rate limits). `0` or unset means "use the ceiling".
- An explicit knob value can therefore never raise concurrency above the
  host-safe maximum. A test that asserts a specific concurrency **must** pin
  `compute_max_subagents`, or it measures the runner's hardware rather than the
  knob — a small CI runner computes 3.

Per-task sessions (`taskrunner:{task_id}:task{N}`) are reset in a `finally`
block after the gather, so sessions are cleaned up even if `CancelledError`
interrupts it.

There is no per-index stagger delay or `os.getloadavg()` load guard — the
semaphore and the host-safe ceiling are the only throttling mechanisms.


## Runs Persistence

Finished runs saved to `{work_dir}/runs.json` as JSON array.
Loaded on `__init__` — survives gateway restarts.

- Persisted on: task completion, task delete
- Each run stores: task_id, spec_path, status, timestamps, error, tokens, replans, and bounded step results.
- Delete via `DELETE /api/taskrunner/{task_id}` removes from memory and disk
- A plan's default work directory is provisional until the plan is accepted. A
  failed attempt removes that taskrunner-owned directory; an explicit caller
  workspace is never removed.

## Access Paths

| Path | Entry Point | Behavior |
|------|-------------|----------|
| CLI | `kirocrew run TASK.md` | Blocking, stdout progress, `--no-test` flag |
| Slack | `run <path>`, `run status`, `run cancel` | Keyword interception in handler |
| Dashboard | REST API + Tasks UI panel | See API Endpoints below |

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/taskrunner` | Status with all runs, step_details |
| POST | `/api/taskrunner` | Start from file path or inline (`__inline__:` prefix) |
| POST | `/api/taskrunner/cancel` | Cancel specific (`{task_id}` in body) or all |
| POST | `/api/taskrunner/plan` | Decompose input into a planned project |
| POST | `/api/taskrunner/plan/cancel` | Cancel planning |
| POST | `/api/taskrunner/from-chat` | Create or update a plan from chat-provided steps |
| DELETE | `/api/taskrunner/{task_id}` | Delete finished run from memory + disk |
| PATCH | `/api/taskrunner/{task_id}/name` | Rename a project |
| PATCH | `/api/taskrunner/{task_id}/tasks/{index}` | Edit a pending task |
| PUT | `/api/taskrunner/{task_id}/plan` | Replace the planned task list |
| POST | `/api/taskrunner/{task_id}/retry` | Retry from step N (`{from_step}` in body) |
| POST | `/api/taskrunner/{task_id}/pause` | Pause a running project |
| POST | `/api/taskrunner/{task_id}/execute` | Execute or resume a planned project |
| GET | `/api/taskrunner/{task_id}/review-fix` | Read one review-fix task's governed state |
| POST | `/api/taskrunner/{task_id}/review-fix/actions` | Review-fix actions (confirm grouping, execute, validate, apply, commit, push, discard) |
| POST | `/api/taskrunner/{task_id}/to-chat` | Open task results in a new chat slot for manual review |
| GET | `/api/taskrunner/{task_id}/plan-context` | Return plan text for chat pre-fill |
| GET | `/api/taskrunner/{task_id}/plan.yaml` | Export the plan as YAML |
| POST | `/api/taskrunner/refine` | Refine user input → task spec (SSE stream) |
| GET | `/api/taskrunner/refine` | Refine status |
| POST | `/api/taskrunner/refine/cancel` | Cancel refine |
| POST | `/api/taskrunner/refine/answer` | Answer clarifying question during refine |
| POST | `/api/reveal` | Reveal file path in Finder (`open -R` macOS, `xdg-open` Linux) |
| POST | `/api/apps/code-review-sage/fix-tasks` | Create a review-fix task from Sage findings |
| GET | `/api/apps/code-review-sage/fix-tasks/{task_id}` | Read a review-fix task |
| POST | `/api/apps/code-review-sage/fix-tasks/{task_id}/review-again` | Request re-review after a review-fix push |

### Status Response

```json
{
  "running": true,
  "runs": [{
    "task_id": "my-task_1771822344",
    "running": true,
    "status": "running",
    "spec": "/path/to/spec.md",
    "spec_name": "my-task",
    "started_at": 1771822344.0,
    "finished_at": 0,
    "steps": 3,
    "current_task": 2,
    "completed": 1,
    "failed": 0,
    "skipped": 0,
    "error": "",
    "tokens_used": 5000,
    "replan_count": 0,
    "step_details": [{
      "index": 1, "title": "Create handler", "description": "...",
      "status": "passed", "error": "", "result": "...(bounded)...", "attempts": 1
    }],
    "work_dir": "/path/to/work/dir",
    "branch_name": "kirocrew/task/my-task_1771822344"
  }]
}
```

## Execution Limits

`task_models.py` owns retry, recovery, replan, total-task, timeout, token-budget, progress-file, and session-prefix constants; `TaskRunner` owns the concurrent-run fallback and persistence filename. `task_executor.execute_task()`, `TaskRunner._try_replan()`, `TaskRunner._watchdog()`, and `task_executor.run_tests()` consume those bounds. `test_scenarios_v2_logic.py` pins retry exhaustion and `test_taskrunner.py::test_replan_blocked_by_step_limit` pins the total-task boundary, so documentation names the enforcement seams instead of copying tunables.

## Notifications

All notifications prefixed with `[spec_name]` via `_notify(title, body, run=run)`.

| Event | Title | Body |
|-------|-------|------|
| Task started | 🚀 Task started | Spec name |
| Plan ready | 📋 Plan ready | Step list |
| Step passed | ✅ Step N/M | Title + bounded result preview |
| Step failed | ❌ Step N/M failed | Title + error |
| Task completed | ✅ Task completed | Steps passed/failed, elapsed, tokens, work dir, full step list |
| Task error | ❌ Task error | Exception message |
| Stall warning | ⚠️ Task may be stalled | Minutes since last activity |
| Session reset | 🔧 Watchdog: cancelling stalled step | Minutes + resetting |
| Process died | 💀 Step N: process died | Recovery count |
| Lesson learned | 📝 Lesson learned | Rule text |
| Replan started | 🔄 Re-planning (N/2) | Failed step title + error |
| Revised plan | 📋 Revised plan | New step count + titles |
| Possible loop | ⚠️ Possible loop | Same error repeated Nx |
| Token budget | 💰 Token budget exceeded | Usage vs budget |
| Branch ready | 🌿 Branch: `name` | Shown in completion summary |

### Where a notification lands: the originating conversation

`start_background(..., session_key=)` records the conversation the run was
started FROM in `TaskRunner._run_session_keys` (task_id → key, in memory only —
a persisted channel key would outlive the binding it names and send a restart's
first notice into a conversation that may no longer resolve). `_notify` resolves
it from `run.task_id` and hands it to `task_reporter.notify`, which forwards it
to the sink. It is dropped when the run is pruned or deleted; a notification with
no run attached carries no key.

The sink is what decides where a notice goes, and the one notice a run cannot
proceed without is an approval request. The gateway's `_task_notify` therefore
tries the governed cross-surface channel ladder first (`_deliver_channel_reply`,
see [slack-gateway](slack-gateway.md)) and keeps the owner Slack DM as the
fallback — before this, that DM was the only escalation, so a Telegram-only
operator's task stalled on an approval they were never told about.

`task_reporter.NotifyCallback` is a **union of two shapes** during the
transition, not one widened signature:

- `SessionAwareNotify` — `(title, body, task_id="", *, session_key="")`;
- `LegacyNotify` — `Callable[[str, str, str], Awaitable[None]]`, which the CLI's
  printer and a dozen test doubles still are.

No single signature is satisfied by both, so the union is what keeps mypy
checking the arity of each. `notify()` widens the CALL only when there is a
conversation to carry AND `_accepts_session_key(callback)` confirms the sink
takes the keyword; otherwise it makes the exact three-argument call every
pre-existing sink was written against. The probe is not paranoia:
`notify()` swallows sink failures at debug level, so an unconditional keyword
handed to a legacy sink would silently stop that sink's notifications with
nothing logged above debug, and a `TypeError` retry cannot tell an arity
mismatch from one raised inside the sink's own body.

## Git Coordination

Each task runs on an isolated git branch via `git_coord.py`:

- **Existing repo**: `git worktree add` creates isolated working directory; user's checkout untouched
- **No repo**: `git init` in work_dir, then `git checkout -b kirocrew/task/{task_id}`
- **Per-step commits**: `git add -A && git commit` after each passed step
- **Revert on failure**: `git reset --hard HEAD~1` when review fails (before retry)
- **State summary**: `git log --oneline` + `git diff --stat` injected into step prompts
- **Review diff**: `git diff HEAD~1` fed to independent review session
- **Finalize**: worktree cleaned up on task completion

Git init failure is non-fatal — task continues without git coordination.

## Cycle Detection

Tracks consecutive identical errors within `_execute_step`:

- 2nd identical error → ⚠️ warning notification
- 3rd identical error → step FAILED with "Loop detected" message
- Different error resets the counter
- `AcpProcessDied` (process crash) does NOT count — crashes don't pollute the error tracker

Applies to both exception errors and test failure outputs.

## Step Prompt Context

`_build_step_prompt` assembles context for each step (async):

1. **Role prompt** — autonomous execution agent identity + git branch awareness
2. **Git context** (if available) — `git_coord.get_state_summary()` (log + diff stat)
3. **Working memory fallback** (if no git) — text-based file/decision tracking
4. **Completed steps** — titles of passed steps
5. **Current step** — title, description, spec content
6. **Retry context** (if attempt > 1) — previous error message

## Self-Review

Independent review using separate session (`taskrunner:{task_id}:review`):

- Step set to `REVIEWING` status before review starts (visible in UI as 🔍)
- Only set to `PASSED` after review succeeds
- Reads actual `git diff HEAD~1` (not LLM's self-report)
- Separate session = no bias from having written the code
- Falls back to generic review prompt when no git diff available
- Review failure → revert commit → retry step → re-commit on success
- Review exceptions are non-fatal (returns True to avoid blocking)

## Tool Approval

Two-layer approval during step execution:

1. `task_executor.execute_task()` evaluates hook rules first; an explicit hook auto-approval remains eligible, while a deny remains a denial. A hook auto-approval for a **shell** command is honoured only after `name_grant.refusal_for_event(event)` confirms each program name in the command still resolves to the program it appears to name; a refusal downgrades to the interactive prompt (or the headless deny-by-default) and is audited as `outcome=auto_approve_declined` with `reason=name_grant`.
2. When no hook grants the request, `on_tool_approval` decides it if the runner has a callback; otherwise the headless path rejects the tool with `headless_no_authorization`.

### Per-run auto-approve (trust) toggle

`Project.auto_approve` is a per-run trust flag (default `False`). It is opt-in
at execute time via the dashboard (`auto_approve` in the execute/start request
body) and threaded through `execute_plan()`, `run()`, and `start_background()`.

- **Default off** → current interactive behavior (tool permission requests
  prompt via `on_tool_approval`, or deny-by-default when headless).
- **On** → the run's tool permission requests are auto-approved WITHOUT the
  interactive prompt, and the SEL tool-invocation audit records the approval
  with reason `run_auto_approve` (vs `hook_auto_approve` for an explicit hook
  trust).

Two guardrails remain intact for a trusted run:

- **Hook deny-lists / sensitive-path blocks** are evaluated BEFORE the
  auto-approve check, so a `TOOL_DENY` still rejects the tool.
- **`force_approval`** is a separate task-level path at the top of `execute_single_task()` and fails closed when no approval handler exists. **`requires_approval`** only prompts when that handler exists; without it, the task continues after a warning.

The mid-stream context-overflow check still runs before final approval.

### Provenance gate & fail-closed audit (`_gate_auto_approve`)

Every launch endpoint (`/start`, `/execute`) routes the requested `auto_approve`
through the shared async `_gate_auto_approve()` provenance gate before honoring
it. Per-run trust is a human-at-the-dashboard decision, so a grant is honored
ONLY for a dashboard-context request (`request["app"] == ""`); an app/proxy
caller cannot mint trust even while claiming `source: "dashboard"`.

The grant decision is **SEL-audited fail-closed**. The audit is written
`critical=True` (a synchronous, raise-on-failure write) but **offloaded via
`asyncio.to_thread`** so the synchronous flush does not block the gateway event
loop while the `await` still surfaces a write failure. The write is contained in
the gate itself (not per-endpoint), so if the grant cannot be persisted to the
SEL trail it is **downgraded to denied** — an un-auditable grant is never
honored — and no unsanitized exception escapes as an HTTP 500 (CWE-755). This
invariant holds for every current and future launch caller.

Hardening measures scope the trust tightly. It is not the global `SafetyOverride`
singleton (which would leak trust to every session), but the authoritative grant
IS held by `SafetyOverride` — as a **task-scoped grant** — so per-run trust is
audited and expires through the same primitive the `backend-security-controls`
rule mandates, with no independent approval state living on the run:

- **SafetyOverride scoped grant (audited, TTL-bounded, slide-renewed)** — enabling `auto_approve` calls `safety_override().activate_scoped("taskrunner:{task_id}:autoapprove", source="dashboard")`, which fail-closed audits the activation to the SEL before committing and stamps the dashboard-window TTL. `is_scope_active(scope)` authorizes every approval; when the grant lapses or is absent after restart, the run intent is revoked and the tool falls through to interactive approval or denial. Each auto-approved tool call slides the grant within its hard ceiling, so an idle run lapses. `scope_remaining_secs()` feeds `build_status`; `Project.auto_approve` is only persisted UI intent, while `TaskRunner._grant_run_trust(run, enabled)` owns both intent and grant so they cannot diverge, and `_release_run_runtime()` revokes the grant at teardown.
- **Deny-by-default parsing** — the API reads `auto_approve` as `body.get(...)
  is True`, so only a literal JSON `true` enables trust; truthy non-booleans
  (`"false"`, `"0"`, `[]`, `{}`) do NOT.
- **Provenance gated at the boundary (label-based, shared by every launch endpoint)**
  — a single `_gate_auto_approve()` helper is applied by BOTH `api_taskrunner_start`
  AND `api_taskrunner_execute_plan` (and any future launch surface), so the gate can't
  drift between routes. It honors `auto_approve` only when the request is not
  app/proxy-embedded (`request["app"] == ""`, set by `token_auth_middleware` for the
  dashboard itself) — blocking an embedded app/proxy from minting trust even while
  claiming `source: "dashboard"` — and, on `start` (which carries a source claim),
  only when the caller EXPLICITLY declared `source == "dashboard"` (checked on the raw
  claimed value, so an omitted/unknown source cannot inherit trust via coercion). The
  decision is SEL-audited (`auto_approve_grant` with endpoint + claimed-vs-resolved
  source + `request["app"]`). Residual: a raw token-holder is indistinguishable from
  the dashboard UI (the gateway's trust model is "token == user"), so this remains a
  declared-label gate; a sub-principal auth model would be a platform-level follow-up.
- **Reset on crash-recovery + affirmative re-grant on resume** — a run recovered from
  an active state (`running`/`pausing`/`cancelling`) on gateway restart has
  `auto_approve` forced `False` and its scoped grant deactivated in `_load_runs()`;
  and because a grant is torn down at run teardown, the dashboard toggle re-syncs from
  the *live* grant (`auto_approve_remaining_secs > 0`), not stale persisted intent —
  so resuming a paused/planned run shows the toggle UNCHECKED and requires an
  affirmative re-grant rather than a click on a pre-checked box.

### Scope limitation (cron / MCP unattended runs)

Per-run trust is reachable only through the dashboard launch endpoints' `_gate_auto_approve()` check. `cli_server.py` does not request it when it constructs the standalone runner, so `kirocrew run TASK.md` cannot turn on run-scoped tool approval. With no `on_tool_approval` callback, `task_executor.execute_task()` rejects every tool request that lacks explicit hook approval; `test_taskrunner_autoapprove.py::test_headless_no_authorization_rejects` pins this fail-closed posture.

This tool-authorization default does not convert `requires_approval` into an unattended task gate: `execute_single_task()` continues a `requires_approval` task when no `on_approval` callback exists. A spec that needs an attended task boundary uses `force_approval`; the standalone CLI then stops as failed rather than proceeding.

## Watchdog

Activity-aware stall detection. Tracks `run.last_task_time` which is bumped on:
- Every text chunk during LLM streaming
- Every tool approval (auto or interactive)
- Step/approval gate entry
- AcpProcessDied recovery

Only fires when there is truly ZERO activity for the stall period.

- Sustained inactivity first emits a warning notification, then resets the session and enters `AcpProcessDied` recovery.
- Resets the current step session: `taskrunner:{task_id}:task{current_task}`
- Stall flag cleared on recovery (can fire again if retry also stalls)
- `last_task_time` reset after recovery (fresh window for retry)
- Watchdog cancelled in `finally` block when task finishes
- **Cannot delete or cancel a task** — only resets ACP session

## Session Management

- Each step: `taskrunner:{task_id}:task{N}` — fresh session per step, reset after completion (owned by `task_executor.py`)
- Decomposition: `taskrunner:{task_id}:decompose` (throwaway, reset in finally) (owned by `task_planner.py`)
  - Returns `{"steps": [...], "acceptance_criteria": [...]}` — criteria shown in final acceptance step
  - Backward compatible with plain JSON arrays (no criteria → step-title fallback)
- Self-review: `taskrunner:{task_id}:review` (separate session, reset in finally) (owned by `task_executor.py`)
- Context compaction between steps routes through `SessionManager.compact_if_needed(key)`, preserving the gateway's deduplication, cooldown, turn-semaphore exclusion, and skills reinjection. A `"busy"` decline is retried later with no direct `provider.compact()` fallback. Its shared post-check uses the attempt's immediate effect verdict (`_POST_COMPACT_RESET_PCT`) and awaits a reset before the next step cold-starts; deferred readings only damp later growth, while the mid-stream overflow guard covers the interim.

Every step gets `is_new=True` on its first message, which triggers full `ContextBuilder` injection: user preferences, active projects, recent history, semantic memory, lessons, episodic memory queried by the step prompt, and triggered skills. The budget matches a normal chat session.

## Dynamic Refine

The "✨ Compose" tab uses a single-shot LLM call to rewrite the user's rough
natural language input into a structured task specification. No tools, no file
reading, no clarifying questions — just a fast spec rewrite.

1. User describes task in natural language
2. LLM rewrites it into a structured spec (Goal / Requirements / Acceptance Criteria)
3. Spec appears in editable textarea — user can edit before clicking "▶ Run This Spec"

**No tools allowed during refine** — all tool calls are rejected. The refiner's
only job is to produce a better-written spec from the user's input.

**WS events**: `refine` type with `{status, text, error}` fields.

## Dashboard UI (Projects Page)

Left/right split layout: 260px sidebar + detail/compose area.

- **Sidebar** (visible when runs exist): compact project cards with status icon, name, progress bar, cancel/delete buttons. "＋ New Project" button at top.
- **Compose area** (no project selected): ✨ Compose | 📄 From Spec tabs, shared `AgentSelector`, `ProjectAnimation` shown in empty state
- **Compose mode**: textarea + "✨ Refine into Spec" + "📋 Plan" buttons, `PlanningBanner` with cancel
- **From Spec mode**: textarea + file upload (`<input type="file">`) + "▶ Run" + "📋 Plan" buttons
- **Project detail** (`ProjectDetailPage`): Idea/Tasks tab bar with 🎮 button (right-aligned)
  - **Idea tab**: read-only spec content + "✏️ Edit in Chat" button
  - **Tasks tab**: DAG/Phased view toggle with `DagView` and `PhasedView` components
  - **🎮 button**: opens modal with pixel-art office animation (`PixelCanvasWidget` + `PixelCanvas`). 7 character sprites animate based on task status (typing/looking/celebrate). Badge shows active agent count.
- **Action buttons**: Execute/Chat/Discard (planned), ■ Cancel (running), ↻ Restart/⏰ Schedule (completed/failed)
- **`SubAgentActivity`**: shown below running projects — live subagent table with status pills (Running/Done/Failed)
- **WS-driven updates**: `push_refresh("taskrunner")` on every notification, 3s auto-refresh polling
