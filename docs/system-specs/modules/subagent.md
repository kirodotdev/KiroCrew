# Subagent Module

Last Updated: 2026-07-13 (removed stale duplicated spawn_status param block; PostToolUse hook firing, subagent_id/parent_session_key/agent_role in hook payloads)

## Overview

The subagent module (`kiro_crew/subagent.py`) spawns isolated background agents for parallel task execution. Each subagent gets its own LLM session via `SessionManager`, runs a focused task, and announces the result via callback.

Supports `on_tool_approval` callback for interactive tool approval (routed through gateway's approval system in Normal/Trust modes).

## Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `_MAX_CONCURRENT` | 3 | Legacy fallback / auto-size floor. `agent.max_subagents` now defaults to `0` = auto-size the cap at startup (floor 3, ceiling `agent.subagent_auto_max`, default 32); a positive value pins a fixed cap. Session-shared subagents are cost-sampled as the runtime's measured RSS/CPU divided by the live shared-session count on that PID (`_live_shared_count`), so the memory term no longer binds and the cap rises to the provider-concurrency ceiling. |
| `_TIMEOUT_SECS` | 1800 | Hard timeout per subagent (30 minutes) |
| `_ON_DONE_TIMEOUT` | 1200 | Outer cap: max total seconds for semaphore wait + injection (20 minutes) |
| `INJECTION_TIMEOUT` | 900 | Inner cap: max seconds for a single `stream_and_collect` call (15 minutes); default `_DEFAULT_INJECTION_TIMEOUT = 900.0`, tunable via `KIROCREW_INJECTION_TIMEOUT` (float seconds, clamped to `_ON_DONE_TIMEOUT`) |
| `_RESET_TIMEOUT` | 30 | Max seconds for session reset in finally block |
| `_TURN_LIMIT` | 100 | Default tool-call budget per subagent (configurable via `agent.subagent_max_turns`, per-spawn via `max_turns`) |
| `_STALL_IDLE_SECS` | 120 | Seconds with no stream activity before a running subagent is surfaced as **stalled** in the running-card (configurable via `agent.subagent_stall_idle_secs`). Surface-only — a stalled subagent is never auto-terminated; the user closes it from the UX (per-row stop / Stop-all) and the 30-min `_TIMEOUT_SECS` ceiling still applies. |
| `_SYSTEM_PREFIX` | (string) | Injected before task text to prevent spawn recursion |
| `COMPLETION_KEEP_DEFAULT_CHARS` | 3000 | Default character cap for the completion event injected into the parent session (configurable via `agent.completion_keep_chars`). Lives in `context_management.py` alongside the helper. |

### Turn Limit Resolution Chain

Priority (highest wins): **per-spawn `max_turns`** → **config `agent.subagent_max_turns`** → **hardcoded default (100)**

A value of `0` means "not set" and falls through to the next level. Implemented as `info.max_turns or self._default_turn_limit or _TURN_LIMIT` in `_run_subagent()`.

### Concurrency Auto-Sizing — Memory Probe (per platform)

When `agent.max_subagents == 0`, `compute_max_subagents()` sizes the cap from
host memory and CPU, clamped to `[3, agent.subagent_auto_max]`. The
available-memory term is read by `_available_memory_gb()`, which is dispatched
per operating system (see `dynamic-subagent-sizing.md`):

- **Linux** — `/proc/meminfo` `MemAvailable`, then clamped by cgroup headroom.
- **macOS** — reclaimable memory (free + inactive + speculative + purgeable
  pages) via the Mach `host_statistics64` syscall through `ctypes`/`libSystem`
  (`_macos_vm_reclaimable_pages`), combined with the `os.sysconf` page size.
  This is **in-process, non-blocking, no subprocess** — required because the
  probe runs on the gateway event loop at startup and the spawn-audit guard
  rejects unrouted subprocess spawns.
- **Other (e.g. Windows)** — no probe yet; returns `-1.0` and the cap fails
  open to the legacy floor of 3.

Hard floor: the auto-sized cap is always ≥ 3 — `compute_max_subagents` clamps to
`[3, hard_cap]` and the config loader clamps `subagent_auto_max` UP to 3 (with a
warning + `config_bounds_clamped` SEL event, mirroring the > 64 ceiling clamp).
Applies only to auto-sizing (`max_subagents=0`); an explicit `max_subagents` pin
is unrestricted (any 0..64).

Limitation: the per-spawn `spawn_min_memory_gb` admission gate
(`check_memory_available`) still reads `/proc/meminfo` and so remains inert
(fails open) on non-Linux hosts. Auto-sizing and the runtime gate are
independent guards; unifying them is out of scope for the sizing probe.

## APIs

### `SubagentManager.__init__(sessions, ctx_builder, on_done, max_concurrent)`
- `sessions: SessionManager` — provides isolated LLM sessions
- `ctx_builder: ContextBuilder` — builds context with memory/skills/hooks
- `on_done: AnnounceCallback | None` — called with `SubagentInfo` when done
- `max_concurrent: int` — capacity limit (default 3)

### `spawn(task, parent_session_key="") -> SubagentInfo | None`
Spawns a background agent. Returns `SubagentInfo` or `None` if at capacity. Uses atomic `_running_count` to prevent race conditions. `parent_session_key` tracks the originating session for completion injection.

Spawn flow:
1. **YOLO mode**: skips approval, runs immediately
2. **Parent trusted**: parent session has `approval_policy="auto"` (set by
   dashboard trust toggle) → skips approval, runs immediately
3. **Non-YOLO, non-trusted**: enters `_spawn_with_approval`, which re-checks
   YOLO (defense-in-depth against toggle race), then requests interactive
   approval with a 2-minute timeout. Timeout or rejection frees the
   concurrency slot.

### Tool Approval Cascade

When a subagent's tool call triggers `EVENT_PERMISSION_REQUEST`, approval
is decided in strict priority order:

1. **Hook deny** — `hooks.on_tool_call()` returns `TOOL_DENY` → reject
2. **YOLO mode** — `is_yolo()` (live check) → auto-approve
3. **Parent policy** — `parent_policy == "auto"` (snapshot at spawn) → auto-approve
4. **Interactive callback** — `on_tool_approval` (races dashboard + Slack, 2h timeout)
5. **Deny by default** — none of the above matched → reject

`parent_policy` is resolved once when `_run_inner` starts, using this chain:
1. Read from parent session via `get_approval_policy(parent_session_key)`
2. If empty and YOLO mode active → `"auto"`
3. If still empty **and subagent has no parent session key** → use the cached `KiroCrewConfig.agent.approval_mode` (snapshotted at `SubagentManager` init); if `"auto"` → `"auto"`

Step 3 ensures parentless subagents (e.g. cron jobs) respect the user's
global approval mode instead of falling through to interactive approval.

The `is_yolo()` check in the cascade is live (reads current gateway state),
providing coverage if YOLO is toggled mid-execution.

### `cancel_all() -> None`
Cancels all running subagents, stops the reaper loop, and awaits their cleanup. Handles `CancelledError` gracefully — sessions released, count decremented.

### Properties
- `running -> list[SubagentInfo]` — currently running agents
- `count -> int` — number of running agents
- `max_concurrent -> int` — capacity limit

## SubagentInfo

```python
@dataclass
class SubagentInfo:
    id: str               # 8-char hex UUID
    task: str             # original task text
    started: float        # time.time() at spawn
    done: bool            # True when finished (success or error)
    result: str           # LLM response text (trimmed to completion_keep for the event)
    result_path: str      # ~/.kirocrew/subagents/<id>/result.txt (full transcript)
    result_truncated: bool  # completion copy dropped content → event carries summary+path
    error: str            # error message if failed
    elapsed: float        # seconds from start to completion (set in _run finally)
    tool_count: int       # observed tool calls (incl. auto-approved); drives running-card progress
    last_activity: float  # time.time() of last stream event; reset to _exec_started; drives idle-stall
    stalled: bool         # reaper flagged this subagent as idle/stalled (UI signal)
    _awaiting_approval: bool  # blocked on a human tool-approval prompt → exempt from idle-stall
```

## Session Lifecycle

1. `spawn()` increments `_running_count`, creates asyncio task
2. `_spawn_with_approval()` (non-YOLO): re-checks YOLO, requests approval with 2-min timeout
3. `_run()` wraps `_run_inner()` with `asyncio.wait_for(_TIMEOUT_SECS)`
4. `_run_inner()` resolves `parent_policy` (parent session → YOLO fallback → config fallback), creates session `subagent:{id}` via `SessionManager.get_or_create(approval_policy=parent_policy)` — policy is persisted on the new session
5. Streams through ACP with context injection, tool approval cascade, and turn counting
6. On completion (in `_run` finally block): fire `subagent_done` WS event immediately (before slow reset + on_done), then `sessions.release()` → `_running_count -= 1` → `sessions.reset()` → call `on_done` callback
7. On timeout: `error = "Timed out after 30 minutes"`
8. On turn limit: `error = "turn_limit:{turn_limit}"` (default 100)
9. On `CancelledError`: `error = "cancelled"`

**Early WS event firing**: `subagent_done` WS event is fired in the `_run` finally block BEFORE the slow `reset()` + `on_done()` path. This ensures the dashboard receives completion status within seconds, not 30-90s later when `stream_and_collect` finishes processing.

## Reaper Loop

`start_reaper()` launches a periodic loop (60s interval) that force-kills subagents exceeding the 30-minute timeout deadline. Defense-in-depth for cases where `asyncio.wait_for` fails to fire due to event-loop saturation or orphaned tasks.

- `_reaper_loop`: sweeps every 60s, calls `_force_reap` on expired agents
- `_force_reap`: reset with 30s timeout → SIGKILL fallback → mark done → fire `subagent_done` WS event
- `_sigkill_session`: best-effort SIGKILL when graceful reset hangs
- After decrementing `_running_count`, `_force_reap` calls `_drain_queue()` so the freed slot immediately starts a queued spawn. Normal completion pumps the queue via its `finally` block, but that block is gated on `not info.reaped`; a reap sets `reaped=True` and decrements the count itself, so without this explicit drain a queued spawn would sit stranded until an unrelated agent finished or a new spawn arrived.
- Wired up in `gateway.py` after `SubagentManager` init

### Idle-Stall Detection

The main-agent watchdog stack (liveness oracle, `tool_stall_suspect`) does **not** govern subagents; `_maybe_flag_stall(agent_id, info, now)` (called from the reaper sweep) is their equivalent. Each stream event calls `_touch_activity(info)`, which updates `info.last_activity` and clears a prior `stalled` flag (re-emitting `subagent_stalled {stalled: false}` when work resumes). `info.last_activity` is (re)initialised to `_exec_started` at the top of `_run_inner` so a queue / spawn-approval wait is never counted as idle.

Per sweep, for an agent that has actually started (`turns > 0` or a live `_pid`) and is **not** blocked on a human approval prompt (`_awaiting_approval`):
- `idle > _stall_idle_secs` and not already flagged → set `info.stalled = True`, emit `subagent_stalled {stalled: true, idle_secs}` (surface-only; the card shows a "no activity" warning), and append a record of the slow command to `~/.kirocrew/subagents/slow_commands.jsonl` for later analysis.
- Detection is **surface-only**: `_maybe_flag_stall` never terminates the agent. A genuinely-hung subagent is closed by the user from the UX (per-row stop → `spawnDelete` → `SubagentManager.cancel(agent_id)`, or header Stop-all). This is a deliberate choice — because session-sharing subagents share the parent's runtime PID, no per-PID liveness oracle can distinguish a wedged tool from a slow-but-healthy one, so the system surfaces + records rather than guessing and killing.

The slow-command record (`record_slow_command`, `subagent_persistence.py`) is append-only and deliberately NOT a tombstone: a tombstone marks an agent dead and is consumed by orphan-reconciliation / TTL cleanup, whereas a stalled subagent is still running. Fields: `id`, `flagged` (ts), `last_tool` (redacted), `tool_count`, `turns`, `idle_secs`, `elapsed_secs`, `parent_session`, `session_sharing`.

`_awaiting_approval` is set around the human tool-approval await in the `EVENT_PERMISSION_REQUEST` branch (reset in `finally`, which also refreshes `last_activity`), so a slow approval never looks stalled.

### Running-card progress events

`subagent_tool` is fired on **`EVENT_TOOL_CALL`** (not only `EVENT_PERMISSION_REQUEST`) — kiro-auto-allowed tools surface only as informational `tool_call` updates, so this is the sole progress signal a simple/read-only task emits. Payload carries `{tool, tool_kind, turns, tool_count}`; `info.tool_count` increments per observed tool call. The `subagent_snapshot` reconnect payload (`dashboard/ws.py`) also carries `tool_count` and `stalled` so a reloading client recovers progress/stall state (a transition-only WS signal always needs a matching snapshot field).

## Completion Injection

Subagent results are routed back to the **originating session** via
`_subagent_done` in `gateway.py`. The `parent_session_key` on `SubagentInfo`
tracks which session spawned the subagent.

### Two-Level Timeout

| Timeout | Location | Duration | Scope |
|---|---|---|---|
| Outer cap | `subagent.py _run()` | 1200s (20 min) | Semaphore wait + injection combined |
| Inner cap | `gateway.py _subagent_done()` | 900s (`INJECTION_TIMEOUT`, tunable via `KIROCREW_INJECTION_TIMEOUT`) | Single `stream_and_collect` call |

On timeout (inner or outer):
1. Kill stuck kiro-cli process via `sessions.reset()`
2. Queue failure event into `slot._pending_subagent_failures`
3. Next `_run_chat` drains the queue into LLM context with `result_path`
4. LLM reads result from disk if needed

### Prompt-Busy Recovery

`_inject_with_retry()` in `gateway.py` makes up to 3 attempts (1 initial + 2 retries) of `stream_and_collect` on AcpError. Between retries: cancels orphaned prompt, exponential backoff. On `PromptBusyExhaustedError`: kills provider, queues failure event. Note: the 1200s outer cap (`_ON_DONE_TIMEOUT`) bounds total wall-clock time, so not all retries may fire if earlier attempts consume the budget.

**Reconnect recovery**: `subscribe_subagents` in `ws.py` restores both managed and native subagent cards. Managed subagents are authoritative in `SubagentManager`: running records replay as `subagent_snapshot`, and recently completed records replay as `subagent_done`. Managed results remain disk-backed and are not copied into inline Redux card payloads.

Native kiro-cli subagents run inside the parent ACP turn and are owned by the parent dashboard slot. `DashboardState.native_subagent_snapshots()` replays running native cards as `subagent_snapshot` and recent terminal cards as `subagent_done`. A native `subagent_done` payload may include optional `task`, `agent`, and `result` fields. `result` is a redacted output tail bounded to 8,000 characters, with an explicit truncation marker when earlier output was dropped. Running output retained for replay is bounded to 40,000 characters, with an 80,000-character hard accumulation ceiling. Terminal native records are retained globally up to 50 cards for at most one hour. The client treats `done` and `error` as monotonic terminal states, so a stale running snapshot interleaved after a live completion cannot demote the card.

**Redaction**: All subagent event payloads (running snapshots and done events) have the `agent` field redacted before sending to the dashboard. Task text is redacted before truncation to prevent credential patterns spanning the boundary.

| Parent Session | Backend Delivery | Client Follow-up | User Sees |
|---|---|---|---|
| Dashboard (`dashboard:*`) | Append as user message + broadcast via WS | TUI/web re-injects via `sendMessage` → LLM round-trip | LLM's response summarizing the result |
| Slack (thread ts) | Post to Slack channel thread + dashboard notification | _(none — raw result posted directly)_ | Raw subagent result text |
| Cron/no parent | Dashboard notification only | _(none)_ | Notification panel entry |

### Post-fan-out Synthesis Turn

After a fan-out of sub-agents, a single dedicated **synthesis turn** produces
the user-facing summary (restate goal → synthesize across all results →
recommend next actions), instead of leaving the last visible message as a
per-sub-agent completion note. Dashboard chat only (orchestrator mode has its
own stage synthesis).

- **Arm** — in `_subagent_done` (chat mode, `not _is_orchestrator`), when the
  last outstanding sub-agent for the parent completes
  (`running_agents_for(parent_key) == []`), set `slot._pending_synthesis = True`.
- **Fire** — in `chat_runner._run_chat`'s drain/idle branch, once the queue is
  empty, no agents are running, `_pending_synthesis` is set, **and**
  `slot._subagent_deliveries_inflight == 0`, launch exactly one `_run_chat` turn
  with `SUBAGENT_SYNTHESIS_PROMPT`. The flag is cleared before firing so it runs
  once and the synthesis turn cannot re-arm it.
- **Per-result turns kept** — each completion is still processed in its own turn
  (no raw buffering) to avoid a context-window blowup; the synthesis works over
  the already-condensed per-result turns.
- **Delivery-race guard** — `_subagent_deliveries_inflight` is incremented in
  `_subagent_done` from entry until the completion is queued/launched
  (try/finally). Because a concurrently-finishing sibling holds this count while
  it awaits the current turn (busy path), an earlier turn cannot fire synthesis
  before that sibling's result is delivered.
- **Cancellation** — a real user message draining first clears
  `_pending_synthesis` (user takes over); a newer in-flight batch defers
  synthesis until it too completes (only one synthesis fires, after all work).
- **Linked surfaces** — `SUBAGENT_SYNTHESIS_PROMPT` begins with
  `SUBAGENT_SYNTHESIS_PREFIX`, marking it a synthetic continuation that is NOT
  mirrored to Slack/Telegram as a user message (only its reply is delivered).

### Parent Session Discovery

The gateway sets the `KIROCREW_SESSION_KEY` env var when spawning kiro-cli,
and `mcp_core.py` reads it via `os.environ.get()`. If the env var is missing
(e.g. older gateway), it falls back to reading
`~/.kirocrew/session_pid_{getppid()}.txt` for backward compatibility. The
session key flows through the `/api/spawn` endpoint as `parent_session`.

## Hook Integration

### PostToolUse Firing

The subagent loop fires both `PreToolUse` (on `EVENT_TOOL_CALL`) and
`PostToolUse` (on `EVENT_TOOL_RESULT`), mirroring `chat_runner.py`. The
tool name is cached on `EVENT_TOOL_CALL` by `tool_call_id` and looked up
when the result arrives. The `Running: ` prefix is stripped so both hooks
receive identical tool_name strings. Hook errors are caught at debug level
to prevent misbehaving hooks from breaking the subagent loop.

### Hook Payload Metadata

Three optional fields are passed to `ScriptHookStore.fire()` and the
`fire_tool_hooks()` wrapper when called from subagent context:

| Field | Source | Description |
|-------|--------|-------------|
| `subagent_id` | `SubagentInfo.id` | 8-char hex ID of the firing subagent (None for parent) |
| `parent_session_key` | `SubagentInfo.parent_session_key` | Session key of the parent that spawned this subagent |
| `agent_role` | `SubagentInfo.agent` | Agent role name configured for the subagent |

All three default to `None` and are only emitted into `hook_event` when
truthy. Payloads are byte-identical for callers that do not supply them,
preserving backward compatibility for existing hook scripts.

Caller sites:
- `subagent.py`: passes all three at both `fire_tool_hooks` (PreToolUse)
  and `hook_store.fire` (PostToolUse) call sites
- `task_executor.py`: passes `session_key` and `agent` (no `subagent_id`)
- `chat_runner.py` / `llm_helpers.py`: unchanged (parent context, defaults to None)

## Skill Integration

`skills/subagent/SKILL.md` (project-level) triggers on keywords: `background`, `spawn`, `bg`, `subtask`, `parallel`, `separately`, `concurrently`. Instructs the LLM to use `kirocrew spawn "task"` via bash to spawn subagents.

### CLI: `kirocrew spawn "task"`

POSTs to `http://localhost:5476/api/spawn` (dashboard API). Returns immediately with subagent ID. Gateway runs the task async and posts result to Slack when done.

### MCP Tool: `spawn_run`

Exposed via `kirocrew-core` MCP server. Always fire-and-forget — results
are delivered back to the calling session via completion event injection.

**Single task:**
```python
spawn_run(task="search docs for X")
```

**Batch parallel:**
```python
spawn_run(tasks=["search docs for X", "check pipeline status", "review CR-123"])
```

All agents spawn at once. The tool returns immediately with agent IDs.
Results arrive as `[Subagent completion event]` messages in the session,
processed by the LLM automatically.

Parameters:
- `task` (str): single task description
- `tasks` (list[str]): multiple tasks for parallel execution
- `cwd` (str, optional): absolute path to launch subagent in. Must be under a configured `subagent_cwd_allowed_roots` entry (default: `~/workspace`). Validated via realpath + prefix match. Pool skipped when cwd is set.
- `max_turns` (int, optional): override tool-call budget for this spawn (default: config or 100)
- `agent` (str, optional): agent name for the subagent

### MCP Tool: `spawn_sub_agents`

Exposed via `kirocrew-core` MCP server. Unlike fire-and-forget `spawn_run`,
`spawn_sub_agents` is **blocking**: it spawns one or more sub-agents in
parallel, waits until all of them finish, then returns their collected
results inline to the calling tool invocation.

Each sub-agent runs as its own KiroCrew-owned ACP session (via
`SubagentManager`), so its text and tool calls stream live to the Activity
tab (`subagent_spawn` / `subagent_chunk` / `subagent_tool` / `subagent_done`
WS events) while the parent blocks.

Native kiro-cli `subagent`/`use_subagent` crews run inside the parent's
kiro-cli process rather than as KiroCrew-owned sessions. KiroCrew surfaces
those in the Activity tab too, by observing kiro-cli's sub-agent
notifications — one card per sub-agent, with each inner tool call and its
output attributed to the right card.

```python
spawn_sub_agents(agents=[
    {"agent_or_mode": "gpu-multiagent-explorer", "prompt": "list python modules"},
    {"agent_or_mode": "gpu-multiagent-explorer", "prompt": "summarize last 5 commits"},
])
```

Parameters:
- `agents` (list[dict], required): each item is `{prompt: str, agent_or_mode?: str}`. `prompt` is truncated to `MAX_MEDIUM_STRING`; `agent_or_mode` to `MAX_SHORT_STRING`. Entries with an empty prompt are skipped.
- `cwd` (str, optional): absolute path to launch all sub-agents in. Must be under a configured `subagent_cwd_allowed_roots` entry (default: `~/workspace`), same validation as `spawn_run`.

Blocking poll semantics:
- Each sub-agent is spawned via `POST /api/spawn` (with `parent_session`), then the handler polls `GET /api/spawn/{id}` every 2s until every sub-agent reports `done` (or `error`).
- An errored/crashed sub-agent is treated as settled so one bad agent cannot keep the loop spinning until the deadline.
- The loop pings `POST /api/session-keepalive` every 60s so the gateway's `is_responsive()` does not flag the (legitimately long-blocked) session as stale and SIGTERM the ACP subprocess mid-poll — same mechanism as the `wait` tool.
- `max_wait` defaults to 7200s (2 hours), clamped to `[60, 7200]`, and is configurable via the `KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT` environment variable. The deadline uses `time.monotonic()`.
- Returns a newline-separated list of per-agent JSON results (`status`: `completed` / `error` / `timed_out`), all redacted for credentials and exfiltration URLs.

Difference from `spawn_run`: `spawn_run` returns immediately and delivers
results later via completion-event injection; `spawn_sub_agents` blocks and
returns the aggregated results directly, so the calling agent can reason over
them in the same turn.

## Orphan Recovery & Tombstoning

Folder-per-agent persistence at `~/.kirocrew/subagents/{id}/`:

```
~/.kirocrew/subagents/{id}/
  state.json      # {task, parent_session_key, started, pid}
  result.txt      # full result text (written on completion)
  tombstone.json  # {error, elapsed, timestamp} (written on failure/orphan)
```

### Gateway Restart Reconciliation

On startup, `SubagentManager` scans `~/.kirocrew/subagents/` and reconciles:

1. **PID alive** → kill process group, deliver result if available, tombstone if not
2. **PID dead + result.txt exists** → deliver result to parent session
3. **PID dead + no result** → write tombstone with "orphaned" error

### Tombstone Lifecycle

- Created on: process death without result, delivery failure, timeout (`cause` =
  `error` / `timeout` / `cancelled` / `reaped` / `gateway_restart`), **and on
  successful delivery** (`cause="delivered"`, via `mark_delivered`) so `result.txt`
  is retained for the grace window instead of deleted immediately.
- Pruned by reaper: `delivered` tombstones after `agent.subagent_result_ttl_secs`
  (default 1h); all other tombstones after 7 days. `prune_stale_tombstones` takes
  a per-cause cutoff for this.
- `spawn_status` falls back to persistence layer for completed/tombstoned agents,
  reading the retained `result.txt` (and honoring offset/limit/grep).

### MCP Tool: `spawn_status`

Retrieves a completed subagent's transcript by ID. The completion event now
carries a **summary + the `result_path`** whenever the completion copy was
truncated (`result_truncated`) or in orchestrator mode, so the parent reads the
full transcript on demand instead of re-running the subagent.

The full transcript stays in `~/.kirocrew/subagents/<id>/result.txt` for a
**retention grace window** after delivery — on success the folder is *not*
deleted immediately; `mark_delivered` writes a `cause="delivered"` tombstone and
the reaper prunes it after `agent.subagent_result_ttl_secs` (default 3600s / 1h).
This fixes the prior day-1 bug where `delete_agent_folder` ran immediately on
delivery, so a later `spawn_status` found no file and silently fell back to the
truncated in-memory `info.result` ("truncated at the same place").

Parameters:
- `agent_id` (str, required): subagent ID from the completion event (alnum, max 64 chars)
- `offset` (int, optional): 0-based start line for a paged read (line-oriented, like reading code)
- `limit` (int, optional): max lines to return (1–2000). Omit for the full transcript.
- `grep` (str, optional): case-insensitive regex; return only matching transcript lines (offset/limit then apply to the matches)

When any of `offset`/`limit`/`grep` is set, the `/api/spawn/{id}` response
includes a `result_meta` block (`total_lines`, `matched_lines`, `offset`,
`returned_lines`, `has_more`) and the tool output is prefixed with a one-line
continuation header (`showing lines X-Y of N | more available — call again with
offset=Y`). With no paging params the full-transcript contract is unchanged. The
line split + regex run via `asyncio.to_thread` so a pathological pattern never
stalls the event loop.

### Completion Event Truncation Modes

The character cap and which end of the transcript to keep are both
configurable. Defaults preserve original behavior — opt-in to the others
when a particular agent style benefits from the change.

When truncation drops content (`SubagentInfo.result_truncated`), the completion
event is not a raw truncated blob: it carries a **first+last-words preview + the
`result_path`** (via `context_management.summarize_result`) so the parent reads
the full transcript on demand (read / grep / `spawn_status`) instead of
re-running the subagent. This is the same shape orchestrator-mode deliveries
have always used, now applied to chat mode too (gated on `result_truncated` so
small results still inline in full).

| Config key | Values | Default | Effect |
|------------|--------|---------|--------|
| `agent.completion_keep` | `head` / `tail` / `both` | `head` | Which end of the transcript to keep when the cap is exceeded |
| `agent.completion_keep_chars` | int (`0` disables truncation) | `3000` | Character cap applied after `completion_keep` |

The helper `apply_completion_keep(text, mode, max_chars)` lives in
`context_management.py`. `head` is identical to the earlier
behavior. `tail` is appropriate for agents that summarize at the end
(developer/reviewer/on-call). `both` keeps roughly half the budget at
each end with a middle elision marker.

Unknown `agent.completion_keep` values cause `kirocrew gateway` to fail
at startup via `_validated_completion_keep` in `config/loader.py`. The
dashboard PATCH endpoint enforces the same enum via
`_EDITABLE_CONFIG["agent.completion_keep"]`.

The values are threaded into `SubagentManager.__init__` from
`gateway.py` (`completion_keep=`, `completion_keep_chars=` constructor
kwargs sourced from `cfg.agent.*`). User-facing docs:
[`docs/configuration.md`](../../../src/kiro_crew/docs/configuration.md),
[`docs/subagents.md`](../../../src/kiro_crew/docs/subagents.md),
[`docs/troubleshooting.md`](../../../src/kiro_crew/docs/troubleshooting.md).

### Dashboard API: `POST /api/spawn`

Request: `{"task": "..."}`
Response: `{"id": "abc123", "task": "...", "status": "spawned"}`
Errors: 400 (missing task), 429 (capacity reached), 503 (subagents not available)

### Handler keywords (instant, no LLM)

User-typed `spawn <task>`, `bg <task>`, `spawn list`, `spawn status` are intercepted by the handler for instant execution.


## Session sharing (shared AcpRuntime)

When `agent.session_sharing` is enabled (default **on** for the kiro backend) and
the parent session is kiro-backed, subagents no longer spawn a fresh `kiro-cli`
process each. Instead they open an additional ACP session on a **shared
`AcpRuntime`** — one process multiplexes the parent session plus all of its
subagents. Startup drops from ~3–5 s to ~200 ms and per-subagent memory from
~400 MB to near-zero.

Decision + lifecycle:

- `SubagentManager._should_use_session_sharing(info)` gates the path: config flag
  on, parent session eligible (`SessionManager.is_session_sharing_eligible`), and
  no backend-specific overrides (`model` / `allowed_tools` / `bare`).
- `_create_shared_session()` resolves the parent's `AcpRuntime` via
  `_get_parent_runtime()` (falling back to `SessionManager.get_subagent_runtime()`
  — a companion runtime), calls `runtime.create_session()`, and wraps the handle
  in `AcpSessionProvider`. `SubagentInfo._session_sharing` / `_shared_provider`
  record the shared path.
- On any failure the code falls back transparently to the legacy
  per-process path (`get_or_create`).
- Cleanup (`_run` finally + `_force_reap`) calls `_shared_provider.shutdown()` to
  tear down only the session — it never kills the shared runtime, which other
  subagents may still use. The runtime is killed when the parent session ends
  (`SessionManager.release_subagent_runtime`, invoked from `reset()`).

Non-kiro (alternate ACP backend) parents are never eligible and always use the
legacy `AcpClient` per-process path regardless of the flag.
