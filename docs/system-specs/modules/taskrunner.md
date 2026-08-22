# TaskRunner Module

## Overview

Autonomous task executor that reads a spec file, decomposes it into
ordered steps via LLM, and executes each step through ACP sessions
with test verification, retries, and progress checkpointing.

Supports multiple concurrent tasks, interactive tool approval,
per-step session isolation with full memory injection, git-coordinated
step commits and reverts, independent review via actual diffs,
cycle detection, disk persistence across restarts, activity-aware
stall detection, and batched parallel execution to prevent resource
exhaustion.

## Module Architecture

The task runner is split into an orchestrator plus 4 focused helper modules under `src/kiro_crew/`:

```
taskrunner.py        (orchestrator, ~1270 lines)
├── task_models.py   (data models + constants, 127 lines)
├── task_planner.py  (LLM decomposition + task parsing + parallel grouping, ~510 lines)
├── task_executor.py (task execution + retries + tests + self-review, ~720 lines)
└── task_reporter.py (status + notifications + progress checkpoints + resume context, ~234 lines)
```

### Module Responsibilities

| Module | Class/Functions | Responsibility |
|--------|----------------|----------------|
| `task_models.py` | `TaskStatus`, `Task`, `WorkingMemory`, `Project`, `NotifyCallback`, constants | Shared data types and configuration constants |
| `task_planner.py` | `decompose()`, `parse_tasks()`, `normalize_cross_group_deps()`, `group_parallel_tasks()`, `plan_to_chat_context()`, `update_plan_tasks()`, `auto_name()` | LLM spec decomposition, task parsing, dependency normalization, parallel grouping, plan-to-chat formatting |
| `task_executor.py` | `execute_task()`, `build_task_prompt()`, `self_review()`, `run_tests()`, `check_context()` | Task execution with retry/recovery budgets, prompt building, context compaction, test running, self-review |
| `task_reporter.py` | `notify()`, `build_status()`, `save_progress()`, `load_checkpoint()`, `build_resume_context()`, `format_completion_summary()` | Notifications, status reporting, TASK_PROGRESS.md checkpointing, resume context |
| `taskrunner.py` | `TaskRunner` | Orchestrator — owns run lifecycle, `_try_replan`, watchdog, run persistence (`_persist_runs`/`_load_runs`); delegates decomposition/execution/reporting to the helper modules |

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
- `dashboard/handlers.py` → `StepStatus`, `TaskRun`
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
        on_approval: ApprovalCallback | None = None,
        auto_test: bool = True,
        auto_commit: bool = False,
        work_dir: Path | None = None,
        conversation_log: ConversationLog | None = None,
        consolidator: HistoryConsolidator | None = None,
        lesson_store: LessonStore | None = None,
        fresh: bool = False,
        global_timeout: float = 0.0,
        token_budget: int = 0,
        max_parallel_steps: int | None = None,  # None/0 -> host-safe ceiling
    ) -> None: ...

    # Delegates to module-level functions: task_planner.decompose(),
    # task_executor.execute_task()/self_review(), task_reporter.build_status()

    async def run(self, spec_path: str | Path, task_id: str = "", name: str = "", source: str = "file") -> TaskRun
    async def start_background(self, spec_path: str | Path, agent: str = "", name: str = "", source: str = "file", *, session_key: str = "") -> str
    def cancel(self, task_id: str | None = None) -> None  # None = cancel all
    def status(self) -> dict

    @property
    def running(self) -> bool
    @property
    def current_run(self) -> TaskRun | None

    # Mutation APIs await fsync-backed persistence off the event loop.
    async def update_plan(task_id: str, tasks: list[dict]) -> TaskRun
    async def update_task(task_id: str, index: int, updates: dict) -> dict
    async def execute_plan(task_id: str, ...) -> str
    async def retry_from_task(task_id: str, from_task: int, agent: str = "") -> str
    async def delete_run(task_id: str) -> bool

    # Internal but accessed by handlers for read-only projection
    _runs: dict[str, TaskRun]
    async def _apersist_runs() -> None
    _persist_runs() -> None  # synchronous compatibility/testing helper only
```

### Task Source & Visibility

`TaskRun.source` tracks where a task was started from. The dashboard Tasks page
filters runs by source to avoid showing cron-triggered background tasks:

```python
dashboard_sources = {"text", "spec", "file", "chat", "dashboard"}
```

| Entry Point | Source Value | Visible on Tasks Page |
|-------------|-------------|----------------------|
| Dashboard UI | `"dashboard"` | ✅ |
| Slack `run <path>` | `"chat"` | ✅ |
| MCP `task_run` tool | `"file"` (default) | ✅ |
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
- Completed runs pruned on new start (keep last 10)
- `_tasks` cleaned in `finally` block (no leaks)
- Max `_MAX_CONCURRENT_TASKS` (3) running tasks — enforced in `start_background()` and `execute_plan()`
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

Steps can be marked with `force_approval: true` in the spec. These gates block execution even in YOLO mode:

- Task pauses at the gate, shows inline Approve/Deny buttons in dashboard
- User must explicitly approve before the step executes
- Useful for destructive operations (deploy, delete, publish)
- Frontend: inline approval buttons rendered in project detail view

## Parallel Execution

Parallel groups are throttled to prevent resource exhaustion from simultaneous
kiro-cli cold starts. Each kiro-cli process spawns ~4-5 MCP server child
processes, so N parallel tasks = ~5N processes all initializing at once.

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

**Superseded design.** Tasks were previously chunked into fixed batches of
`_MAX_PARALLEL_TASKS` (3), with the next batch starting only after the whole
current one finished — so one slow task left the rest of its batch's slots idle.
The knob was read into `self._max_parallel_steps` but never consulted by that
loop, so it had no effect on batch size.

## Runs Persistence

Finished runs saved to `{work_dir}/runs.json` as JSON array.
Loaded on `__init__` — survives gateway restarts.

- Persisted on: task completion, task delete
- Each run stores: task_id, spec_path, status, timestamps, error, tokens, replans, step_details (result truncated to 2K)
- Delete via `DELETE /api/taskrunner/{task_id}` removes from memory and disk

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
| DELETE | `/api/taskrunner/{task_id}` | Delete finished run from memory + disk |
| POST | `/api/taskrunner/{task_id}/retry` | Retry from step N (`{from_step}` in body) |
| POST | `/api/taskrunner/{task_id}/to-chat` | Open task results in a new chat slot for manual review |
| POST | `/api/taskrunner/refine` | Refine user input → task spec (SSE stream) |
| GET | `/api/taskrunner/refine` | Refine status |
| POST | `/api/taskrunner/refine/cancel` | Cancel refine |
| POST | `/api/taskrunner/refine/answer` | Answer clarifying question during refine |
| POST | `/api/reveal` | Reveal file path in Finder (`open -R` macOS, `xdg-open` Linux) |

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
      "status": "passed", "error": "", "result": "...(up to 2K)...", "attempts": 1
    }],
    "work_dir": "/path/to/work/dir",
    "branch_name": "kirocrew/task/my-task_1771822344"
  }]
}
```

## Constants (in `task_models.py`)

| Constant | Value | Purpose |
|----------|-------|---------|
| `MAX_RETRIES` | 3 | Logic/test failure attempts per step |
| `MAX_RECOVERIES` | 2 | Process crash recovery budget per step |
| `MAX_REPLAN` | 2 | Plan revision attempts after step exhausts retries |
| `MAX_TOTAL_TASKS` | 50 | Hard cap on total tasks (including replans) |
| `_MAX_PARALLEL_TASKS` (in `taskrunner.py`) | 3 | Ctor fallback only, used when `compute_max_subagents` raises; the live cap is `self._max_parallel_steps` |
| `_MAX_CONCURRENT_TASKS` (in `taskrunner.py`) | 3 | Max simultaneous task runs |
| `CONTEXT_COMPACT_PCT` | 80.0 | Compact threshold |
| `TEST_TIMEOUT` | 5400 | 90 min for test command |
| `STALL_TIMEOUT` | 3600 | 60 min with no activity → warn |
| `STALL_CANCEL_TIMEOUT` | 7200 | 2h with no activity → reset session |
| `DEFAULT_TOKEN_BUDGET` | 0 | 0 = unlimited |
| `PROGRESS_FILE` | `TASK_PROGRESS.md` | Written next to spec file |
| `SESSION_PREFIX` | `taskrunner` | Session key prefix |
| `_RUNS_FILE` (in `taskrunner.py`) | `runs.json` | Persisted runs file |

Note: constants were renamed from `_MAX_RETRIES` → `MAX_RETRIES` etc. when moved
to `task_models.py` (no longer private to a single file).

## Notifications

All notifications prefixed with `[spec_name]` via `_notify(title, body, run=run)`.

| Event | Title | Body |
|-------|-------|------|
| Task started | 🚀 Task started | Spec name |
| Plan ready | 📋 Plan ready | Step list |
| Step passed | ✅ Step N/M | Title + result preview (500 chars) |
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

1. **Hook rules** checked first: `hooks.on_tool_call(title)` → DENY/ALLOW
2. **Interactive approval** via `on_tool_approval` callback (if set):
   - In gateway: routes through `_interactive_approval` → checks YOLO/Trust mode → `DashboardState.request_approval()` → WS broadcast → user clicks ✅/🚫
   - 2-hour timeout on interactive approval (auto-reject)
   - YOLO mode: auto-approves all
   - Trust mode: auto-approves when all slots trusted

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
- **`force_approval` / `requires_approval` task gates** are a separate
  task-level path (top of `execute_single_task`) and are unaffected — they
  still block/prompt regardless of `auto_approve`.

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

- **SafetyOverride scoped grant (audited, TTL-bounded, slide-renewed)** — enabling
  `auto_approve` calls `safety_override().activate_scoped("taskrunner:{task_id}:autoapprove",
  source="dashboard")`, which fail-closed audits the activation to the SEL BEFORE
  committing and stamps a TTL (dashboard window, 6h, under the 24h hard ceiling).
  The permission branch authorizes via `is_scope_active(scope)` before EVERY
  approval; when the grant lapses (or is absent, e.g. after a gateway restart) the
  run's intent is revoked (`auto_approve` cleared, SEL `task.auto_approve_expired`)
  and the tool falls through to interactive / deny-by-default. To avoid a long but
  *actively-progressing* run losing trust mid-flight at the base TTL, each
  auto-approved tool call slides the grant forward via `renew_scoped()` — capped at
  the 24h hard ceiling from first activation, so an abandoned (idle) run still lapses
  after the base window. `scope_remaining_secs()` is surfaced in `build_status`
  (`auto_approve_remaining_secs`) so the dashboard can warn before expiry.
  `Project.auto_approve` is only the persisted UI-intent flag; the live authorization
  is the scoped grant. Setting/clearing both sides is owned by a single
  `TaskRunner._grant_run_trust(run, enabled)` so the intent flag and grant cannot
  diverge; grants are revoked at run teardown (`_release_run_runtime`).
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

Per-run trust is intentionally reachable **only** from the dashboard toggle, so
cron-scheduled and MCP/chat-launched runs — which are headless by construction —
cannot carry it and still hit the `headless_no_authorization` deny-by-default
branch. Auto-granting recurring trust to a cron is a deliberately larger risk, so
that surface is **not** covered by this feature and continues to rely on the
operator's existing global controls. Extending unattended trust to recurring runs
is a possible follow-up, not a current goal.

## Watchdog

Activity-aware stall detection. Tracks `run.last_task_time` which is bumped on:
- Every text chunk during LLM streaming
- Every tool approval (auto or interactive)
- Step/approval gate entry
- AcpProcessDied recovery

Only fires when there is truly ZERO activity for the stall period.

- 60 min no activity → ⚠️ warning notification
- 2h no activity → 🔧 session reset → `AcpProcessDied` → recovery retry
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
- Context compaction between steps routes through the shared
  `SessionManager.compact_if_needed(key)` path (#4686) — same dedup, failure/
  ineffective cooldown, turn-semaphore exclusion, and skills reinjection as
  gateway compaction; a `"busy"` decline is left alone and retried on a later
  check (no direct `provider.compact()` fallback). The reset-if-still-≥95%
  post-check now lives in the shared path: it fires on the attempt's own
  IMMEDIATELY-MEASURED effect verdict (`_POST_COMPACT_RESET_PCT`), awaited
  on this seam (outcome `"reset"`) so the next step cold-starts instead of
  racing the recovery; deferred next-reading settles only damp (they cannot
  distinguish a failed compaction from later turn growth), with the
  mid-stream overflow guard covering the interim.

Every step gets `is_new=True` on its first message, which triggers full `ContextBuilder`
injection: user preferences, active projects, recent history, semantic memory, lessons,
episodic memory (queried by step prompt text), and triggered skills. Same ~15k budget
as a normal chat session.

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
