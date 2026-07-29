# Subagents & Parallel Work

KiroCrew can spawn background subagents to handle tasks in parallel. This is
useful for fan-out work like reviewing multiple packages, running parallel
searches, or delegating independent tasks.

## How to Use

### Via Chat

Ask naturally:
- "Review these 3 packages in parallel"
- "Search for X, Y, and Z at the same time"
- "Run this task in the background"

KiroCrew uses the `spawn_run` MCP tool to create subagents.

### Via Slack

```
spawn run "review the latest CR for MyPackage"
spawn list
```

### Via MCP Tool

The `spawn_run` tool accepts:
- `task` — single task description
- `tasks` — array of tasks for parallel execution
- `agent` / `agents` — optional agent name(s) for each task

## How It Works

1. KiroCrew spawns one or more subagent processes
2. Each subagent gets its own agent session with full tool access
3. Results are automatically injected back as `[Subagent completion event]`
4. KiroCrew synthesizes the results into a final response

## Limits

- **Max concurrent**: auto-sized at startup by default (`agent.max_subagents = 0`; floor 3, ceiling `agent.subagent_auto_max` = 32); set a positive integer to pin a fixed cap
- **Timeout**: 30 minutes per subagent task, 20 minutes delivery, 5 minutes per injection attempt
- **Turn limit**: 100 turns per subagent (configurable via `agent.subagent_max_turns`, UI max 200)
- **Memory guard**: spawns are refused when available memory drops below 4 GB (configurable via `agent.spawn_min_memory_gb`, set to 0 to disable)
- **No nesting**: subagents cannot spawn their own subagents
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
| `agent.subagent_result_ttl_secs` | int (seconds) | `3600` | How long the delivered `result.txt` is kept before the reaper prunes it |

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
