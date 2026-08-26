# Heartbeat Module

## Overview

The heartbeat service (`kiro_crew/heartbeat.py`) runs periodic background tasks on a configurable interval (default 60s).

## Responsibilities

1. **Task processing** — reads `~/.kiro/crew/workspace/HEARTBEAT.md`, sends non-empty tasks to the agent
2. **FTS index rebuild** — every 15 ticks (~15 min at default interval)

## HEARTBEAT.md Format

```md
# Heartbeat Tasks

<!-- Add tasks below (one per line). KiroCrew picks them up on next heartbeat. -->
- check my pipeline status
- summarize open PRs
```

- One task per line (no multiline support)
- Lines starting with `#`, empty lines, and HTML comments (`<!-- -->`) are ignored
- List markers (`-`, `*`, `- [ ]`, `- [x]`) are stripped
- File is auto-created on first start with an empty template

## Task Lifecycle

1. Tick fires → read file → extract tasks
2. For each task: call `on_task` callback (gateway sends through ACP, posts result)
3. Callback returns response text; heartbeat checks for `HEARTBEAT_KEEP` sentinel
4. Tasks with `HEARTBEAT_KEEP` in response are retained for next tick (incomplete)
5. Tasks without the sentinel are removed (complete)
6. Tasks that raise exceptions are retained automatically (retry)

### Task Retention (`HEARTBEAT_KEEP`)

The agent can include `HEARTBEAT_KEEP` anywhere in its response to signal incomplete:

```
Progress: 3/5 files processed. HEARTBEAT_KEEP
```

- `_should_keep(result)` checks for the sentinel (case-insensitive)
- `None` return (legacy) treated as complete (removed)
- Sentinel stripped from display text before posting
- Deliver tags (`<!-- deliver:channel_id -->`) preserved on retention

## Concurrency

- `_processing` flag prevents overlapping ticks from double-processing tasks
- FTS rebuild runs independently of task processing
- Uses dedicated `heartbeat:task` session key — no interference with user or cron sessions

## Per-Task Timeout (Unattended Turn Bound)

A heartbeat turn runs without a human present. The `_heartbeat_task` callback in
`slack/gateway.py` wraps `stream_and_collect` in
`asyncio.wait_for(..., timeout=HEARTBEAT_TASK_TIMEOUT_SECS)` (1800s / 30 min,
mirroring cron's `_JOB_TIMEOUT_SECS`). This is the analogue of cron's
`_execute_with_timeout`.

Without it, if the agent calls a non-allowlisted tool, the interactive-approval
callback would block on the human-approval wait with no human present — wedging
`HeartbeatService._processing=True` and freezing the whole heartbeat subsystem.

On `asyncio.TimeoutError`:
1. Reset the heartbeat session (`sessions.reset(HEARTBEAT_KEY)`) BEFORE the
   `finally` releases it — kills the lingering `claude-agent-acp` turn/process so
   it does not outlive the timeout. A failing reset is logged and swallowed. This
   is safe because `asyncio.wait_for` has already cancelled the in-flight
   `stream_and_collect`, and any concurrent sibling task in the same cycle is
   blocked on the per-key semaphore (held until our `finally` releases) — they
   pick up the freshly-recreated session.
2. Log a warning.
3. Return a graceful incomplete result string (the loop is NOT crashed).
4. The `finally` block calls `release(HEARTBEAT_KEY)` ONLY — no per-task reset.
   Cycle-end recycle is owned by `SessionManager.recycle_heartbeat`, called
   once via `HeartbeatService.on_cycle_end` after `asyncio.gather` completes.

Background approvals additionally deny-fast (see security / slack-gateway specs),
so the common "non-allowlisted tool" case resolves in minutes; this hard timeout
is the backstop for any other long-running turn.

## Cycle-End Recycle

Heartbeat runs all tasks for a cycle through `asyncio.gather` so the wall clock
stays bounded.  Concurrent tasks share `HEARTBEAT_KEY`, so a per-task
`reset(HEARTBEAT_KEY)` in `finally` would tear down the session a sibling task
is still using (the per-key semaphore guarantees serialization, but resetting
inside the critical section means the next holder receives a torn-down provider).

The fix: per-task `finally` only releases the semaphore.  Cycle-end recycle
is `SessionManager.recycle_heartbeat`, invoked once via
`HeartbeatService(on_cycle_end=...)` after gather completes — and only when the
session has crossed the `_BG_RECYCLE_PCT` (70%) or `_BG_BLIND_RECYCLE_PROMPTS`
(40 prompt) threshold.  Healthy cycles reuse the warm session; the MCP toolbelt
is cold-started ~once every N cycles instead of every cycle.

## Gateway Wiring

`HeartbeatService` is started in `slack/gateway.py` after cron service:
- `on_task` callback: prepends a `HEARTBEAT_KEEP` reminder to the task text, opens a session under `HEARTBEAT_KEY` with `agent="kirocrew-heartbeat"`, streams the response (gated by `HEARTBEAT_SAFE_TOOLS`), posts the result, and releases the per-key semaphore
- `on_cycle_end` callback: invokes `SessionManager.recycle_heartbeat` once after `asyncio.gather` completes — recycles the session only when it has crossed the context / prompt-count threshold
- Callback re-raises exceptions so heartbeat can track failures
- Stopped during gateway shutdown

### Session Identity

Heartbeat runs in its own session (`HEARTBEAT_KEY = "_hb"` in `session.py`), distinct from the shared `BACKGROUND_KEY = "_bg"` used by cron / consolidator / chat-title. The session uses the dedicated `kirocrew-heartbeat` agent (installed by `_install_heartbeat_agent` in `agent.py`) — a minimal MCP surface (`kirocrew-core` only on public installs; the enterprise internal MCP server wiring is omitted, matching `_install_research_agent` / `_install_knowledge_agent`) so cycle cold-starts stay cheap. SEL audit logging stays gateway-side in `_heartbeat_approval` regardless; the per-agent narrowing is purely a cold-start cost reduction.

The session is shared across all tasks in one cycle (so concurrent gather'd tasks reuse the warm provider) and conditionally recycled by `recycle_heartbeat` between cycles when context grows past the threshold.

### HEARTBEAT_KEEP Injection

Every heartbeat task text is prepended with a fixed instruction at the gateway (`_HEARTBEAT_KEEP_INJECTION` in `slack/gateway.py`) before `ctx_builder.build_message`. The instruction tells the agent it must include `HEARTBEAT_KEEP` in its response when the task is incomplete. Inline injection survives context compaction and webhook-restored sessions where skill / system-prompt copies of the same instruction can drift out of effective context.

### Tool Approval (`HEARTBEAT_SAFE_TOOLS`)

Heartbeat is unattended — there is no human to click an approval button. Tool approval uses `HOOK_BASED` policy with a heartbeat-scoped `HookManager` (built once at init by `_build_heartbeat_hooks`) and a custom callback (`GatewayOrchestrator._heartbeat_approval`) that auto-approves only tools whose name **exact-matches** a member of `HEARTBEAT_SAFE_TOOLS` and rejects everything else with a SEL audit event (`outcome=denied`, `reason=not_in_heartbeat_safe_tools`). Both approve and deny outcomes emit `log_tool_invocation` so every permission decision is auditable.

The heartbeat-scoped hooks drop the user's `auto_approve_tools` so the allowlist is the **sole approval authority** — `llm_helpers._resolve_permission` would otherwise consult the hooks BEFORE the `_heartbeat_approval` callback, and a user config like `auto_approve_tools=["*"]` would auto-approve any tool, bypassing `HEARTBEAT_SAFE_TOOLS` entirely. The user's `auto_deny_tools` IS preserved (denies can only narrow what runs in heartbeat, never widen).

The allowlist is name-based and exact-match only — no verb / heuristic fallback. Heartbeat polls untrusted external content (CR comments, ticket bodies) where prompt-injection could try to widen approval via a clever read-shaped tool name (`get_all_credentials`, `list_env_secrets`, etc.). Strict enforcement is auditable and cannot be widened that way; this is deny-by-default per the security-controls guideline.

The allowlist is curated for read-only / observation tools — local file reads (`Read`, `Grep`, `Glob`), `WorkspaceSearch`, and side-effect-free KiroCrew-core reads (`learn_list`, `cron_list`, `spawn_list`, `spawn_status`, `artifact_list`, `artifact_get`, `artifact_versions`, `local_knowledge_search`). (The enterprise-internal read APIs — internal code/knowledge search, code-review/ticketing/pipeline/deploy/on-call reads, `recall` — were removed from the public fork's allowlist; an internal companion re-adds them out of band.) Write tools (`send_message`, `file_send`, `cron_add`, `Edit`, `Write`, shell `execute`/`run`) are not in the list and are rejected.

When a legitimate new read tool needs to run in heartbeat, operators observe SEL `denied` events (or the gateway-log warning `Heartbeat blocked tool call: <name>`) and explicitly add the name to `HEARTBEAT_SAFE_TOOLS`.

## Constants

| Constant | Value | Location |
|----------|-------|----------|
| `_DEFAULT_INTERVAL` | 60 | `heartbeat.py` |
| `_FTS_REBUILD_TICKS` | 15 | `heartbeat.py` |
| `HEARTBEAT_TASK_TIMEOUT_SECS` | 1800 | `heartbeat.py` |
| `HEARTBEAT_FILE` | `HEARTBEAT.md` | `heartbeat.py` |
| `HEARTBEAT_KEY` | `_hb` | `session.py` |
| `HEARTBEAT_SAFE_TOOLS` | curated frozenset | `slack/gateway.py` |
| `_HEARTBEAT_KEEP_INJECTION` | reminder string | `slack/gateway.py` |
| `kirocrew-heartbeat` agent | minimal-MCP agent JSON | installed by `agent.py:_install_heartbeat_agent` |
| `_BG_RECYCLE_PCT` | 70.0 (shared with background) | `session.py` |

## Known Limitations

- No multiline tasks — each line is a separate task
- If user edits file while tasks are processing, new additions may be lost
- Exception-retried tasks have no max retry count

## Delivery Modes

Tasks can specify a delivery target via HTML comment tags:

```md
- [ ] Check CR-123 <!-- deliver:prompt:dashboard:chat-0 -->
```

### Supported Modes

| Mode | Syntax | Behavior |
|------|--------|----------|
| Slack DM (default) | _(no tag)_ | Posts result to owner's Slack DM |
| Dashboard slot | `<!-- deliver:prompt:dashboard:<slot> -->` | Injects result into a specific dashboard chat slot (e.g., `chat-0`, `chat-3`) |
| Channel | `<!-- deliver:<channel_id> -->` | Posts to a specific Slack channel |

### Dashboard Delivery (`prompt:dashboard:<slot>`)

Resolves `chat-N` slot names to active session keys. The result is injected as a user message into the target slot, triggering an LLM response in that session. Useful for heartbeat tasks that should report back into an active dashboard conversation.

Slot resolution: `chat-0` → first active slot, `chat-3` → fourth slot. Falls back to Slack DM if slot not found.

### Slack Suppression for Incomplete Tasks

When a task response contains `HEARTBEAT_KEEP`, Slack delivery is suppressed. The task is retained for the next tick without notifying the user. Only completed tasks (no `HEARTBEAT_KEEP`) trigger Slack/dashboard delivery.
