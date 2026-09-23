# Subagents & Parallel Work

Kiro Crew can spawn background subagents to handle tasks in parallel. This is
useful for fan-out work like reviewing multiple packages, running parallel
searches, or delegating independent tasks.

## How to Use

### Via Chat

Ask naturally:
- "Review these 3 packages in parallel"
- "Search for X, Y, and Z at the same time"
- "Run this task in the background"

Kiro Crew uses the `spawn_run` MCP tool to create subagents.

### Via Slack

```
spawn review the latest CR for MyPackage
spawn list
```

The grammar is `spawn <task>` (or `bg <task>`) — there is no `run` subcommand;
`spawn list` / `spawn status` render the running subagents.

### Via MCP Tool

The `spawn_run` tool accepts:
- `task` — single task description
- `tasks` — array of tasks for parallel execution
- `solo_reason` — `parent_parallel`, `bulk_data`, `fresh_context`, `specialist`, or `user_requested` (see below)
- `solo_details` — concrete benefit and ownership; required for `parent_parallel`, `specialist`, and `user_requested`
- `agent` / `agents` — optional agent name(s) for each task
- `include_memory` / `include_lessons` / `include_project` — booleans (default `true`) switching off a context group the sub-agent would otherwise inherit
- `max_turns` — per-spawn tool-call budget override (0 = unset, max 1000)
- `model` — model override for this spawn (e.g. `deepseek-3.2`)
- `reasoning_effort` — `low` / `medium` / `high` / `xhigh` / `max`, batch-wide
- `keep` — make the run a continuable conversation with guaranteed resumability and longer retention
- `cwd` — absolute launch directory, which must be under a configured `subagent_cwd_allowed_roots` entry

Setting `model` or `reasoning_effort`, or `keep: true`, forces the
dedicated-process path instead of session sharing.

#### The solo gate

Do focused work directly. Delegate when separate ready work can progress in
parallel, a large input needs distillation, independent verification adds value,
a specialist capability is needed, or the user explicitly asks. A long task,
empty slots or a different model name alone is not a reason to delegate.

One child is valid when the parent owns a separate useful workstream. For example:

```python
spawn_run(task="Update docs to the finalized API in api-v2.json; own docs/api.md; return the diff and link checks",
          solo_reason="parent_parallel",
          solo_details="I implement backend validation in server.py; docs inputs are finalized and disjoint")
```

`parent_parallel` needs asynchronous `spawn_run` and a dashboard-owned parent
turn. Its receipt says whether bounded parent work is supported. When supported,
finish at most one minute of ready non-overlapping work, then end the turn so
queued results can arrive. Otherwise yield immediately. This guidance is not a
timer enforced by the runtime. Blocking `spawn_sub_agents` cannot support
parent-child parallel work; `spawn_continue` still requires immediate yield.

For one child without separate parent work, use `bulk_data` for substantial
inputs, `fresh_context` for blind review/clean reproduction, `specialist` with a
concrete capability, or `user_requested` with the user's request quoted in
`solo_details`. The last two require details; existing bulk/fresh payloads keep
working. `fresh_context` alone does not turn off memory or project context.

An unexplained equivalent-worker solo call is refused without spawning. Do it
directly or give the real benefit; do not invent tasks or swap models to bypass
the gate. Reasons/details are retained in the run record as model claims, not
verified authorization. Existing direct SDK/API calls without the model solo
marker remain compatible.

Wait through completion events when no useful independent work remains. Do not
repeat a child's task to stay busy. Collect all outcomes in the batch, including
failures, then check artifacts and actual test results before reporting success.
Cancellation and late results must not restart an old task. Inspect side effects
before retrying. Parallel writers need separate ownership; a worktree does not
isolate shared databases, ports or external services.

The other spawn tools:
- `spawn_sub_agents` — same fan-out as `spawn_run`, but BLOCKS and returns the collected results; takes `agents` (array of `{agent_or_mode, prompt}`), `cwd`, and the same `include_*` switches
- `spawn_continue` — dispatch a follow-up turn into a completed run's conversation (`conversation`, `task`, optional `agent` / `max_turns` / `model`); context scope is inherited, so the `include_*` flags are not accepted
- `spawn_steer` — inject a message into a RUNNING subagent's in-flight turn (`agent_id`, `message`, `mode`: `interrupt` default or `follow_up`)
- `spawn_release` — end a continuable conversation (`conversation`) so it can no longer be continued
- `spawn_list` — list running and completed subagents
- `spawn_status` — read a completed run's retained transcript (see below)
- `resource_status` — advisory host headroom (available memory, CPU load, posture, and the current concurrent sub-agent cap)

## How It Works

1. Kiro Crew spawns one or more subagent processes
2. Each subagent gets its own agent session with full tool access
3. Results are automatically injected back as `[Subagent completion event]`
4. Kiro Crew synthesizes the results into a final response

## Limits

- **Max concurrent**: auto-sized at startup by default (`agent.max_subagents = 0`; floor 3, ceiling `agent.subagent_auto_max` = 32); set a positive integer to pin a fixed cap
- **Timeout**: 3 hours per subagent task (`agent.subagent_timeout_secs`, clamped to 60s..86400s at load; 0 means "use the default"), 20 minutes delivery (semaphore wait + injection), 15 minutes per injection attempt (`KIROCREW_INJECTION_TIMEOUT`, clamped to the delivery cap). A **blocking** `spawn_sub_agents` call collects for at most 2 hours regardless of that setting (`KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT`, itself capped at 7200s), so use `spawn_run` for work longer than 2 hours and read the results from its completion events
- **Turn limit**: 1000 tool calls per subagent by default (configurable via `agent.subagent_max_turns`, maximum 1000). A stored value, including 100, is preserved on upgrade; an unset key automatically uses the current default. Run `kirocrew config defaults` to inspect an older stored default, then use `kirocrew config defaults --adopt agent.subagent_max_turns` only if you want to replace that pin with the current default.
- **Memory guard**: admission preserves a 4 GB available-memory floor plus estimated startup memory for the next worker and dedicated workers still warming up. Observed RSS replaces the startup reservation; confirmed shared sessions add no dedicated-process cost. Work waits in the durable queue when headroom is insufficient (legacy spawns are refused). Configure the floor with `agent.spawn_min_memory_gb`; set it to 0 to disable this guard.
- **Nesting**: a subagent can spawn its own subagents. A nested spawn is tracked apart from the parent's wave, so its children are not counted against that wave's completion total
- **Redaction**: task strings in SubagentInfo are redacted (credentials + exfiltration URLs) before surfacing to Slack/dashboard

## Named Agents

You can specify which agent a subagent should use:

```
spawn_run(tasks=["review code", "check tests"], agents=["code-reviewer", "test-analyzer"])
```

Named agents use their own system prompt and skills.

## Results

Subagent results are posted to:
- The dashboard (via WebSocket notification)
- Slack DM (with an ack button)
- The parent conversation (as completion events)

Long results are split into multiple Slack messages (3900 chars per chunk).

## Completion Event Truncation

The completion event injected back into the parent conversation is a bounded
copy of the subagent's streamed transcript. When the cap drops content, the
event carries a **short preview + the transcript's file path** (not a bare
truncated blob), and the parent reads the rest on demand — the `read` tool
(offset/limit), `grep`, or the `spawn_status` MCP tool — instead of re-running
the subagent.

The full transcript lives at `~/.kiro/crew/subagents/<id>/result.txt` and is
**retained for a grace window after delivery** (default 1 hour) so those reads
succeed; the reaper then prunes it.

Three `agent.*` config knobs control what the parent session sees:

| Key | Values | Default | Effect |
|-----|--------|---------|--------|
| `agent.completion_keep` | `"head"` / `"tail"` / `"both"` | `"head"` | Which end of the transcript to keep when it exceeds the cap |
| `agent.completion_keep_chars` | int (`0` disables) | `3000` | Character cap applied after `completion_keep` |
| `agent.subagent_result_ttl_secs` | int (seconds) | `3600` | How long the delivered `result.txt` is kept before the reaper prunes it. The window starts when the completion reaches the parent, so a completion queued behind a long turn does not spend it waiting |

Pick the mode that matches how your agents emit their useful output:

- **`head`** — first N characters. Best for agents whose verdict appears
  up front (verdict-then-evidence).
- **`tail`** — last N characters. Best for agents that narrate throughout
  and summarize at the end (developer agents, code reviewers, on-call
  triage).
- **`both`** — roughly N/2 from the head, a middle marker, and N/2 from
  the tail. Best for parent agents that need both the task framing and
  the conclusion.

Set `completion_keep_chars: 0` to disable truncation entirely.

Set via `kirocrew config set agent.completion_keep tail` or by editing
`~/.kiro/crew/config.json` directly.

### Reading the full transcript on demand

`spawn_status` reads the retained transcript by agent ID and supports
line-oriented paging (like reading code) for large results:

- `spawn_status(agent_id, limit=200)` — first 200 lines
- `spawn_status(agent_id, offset=200, limit=200)` — next page
- `spawn_status(agent_id, grep="ERROR|FAIL")` — only lines matching the regex

A paged/filtered response is prefixed with a continuation header
(`showing lines X-Y of N | more available — call again with offset=Y`). With no
paging args it returns the full transcript. You can also point the generic
`read` / `grep` tools straight at the `result_path` from the completion event.
