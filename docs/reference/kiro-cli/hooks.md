# Hooks

Source: https://kiro.dev/docs/cli/hooks/

Execute custom commands at specific points during agent lifecycle and tool execution.

## Hook event

Hooks receive JSON via STDIN:

```json
{
  "hook_event_name": "preToolUse",
  "cwd": "/current/working/directory",
  "tool_name": "read",
  "tool_input": { ... }
}
```

## Hook output

- **Exit 0**: succeeded, STDOUT captured
- **Exit 2**: (PreToolUse only) block tool execution, STDERR returned to LLM
- **Other**: failed, STDERR shown as warning

> **Kiro Crew divergence** (script hooks in `hooks.json`, managed from the
> dashboard's Hooks page): for a `preToolUse` hook, any exit that is neither 0
> nor 2 — including a timeout, a crash, or an unexecutable command — **blocks
> the tool** instead of warning. Exit 0 is a delivered allow and exit 2 a
> delivered deny; every other outcome is an undelivered verdict, and the gate
> fails closed (`BLOCKED:<hook>:<detail>`). There is no per-hook opt-out, so a
> `preToolUse` hook must reserve nonzero exits for "block" — a hook that exits
> 1 to mean "issues found, but proceed" will deny the tool. Non-gating events
> (`postToolUse`, `userPromptSubmit`, `stop`) keep the warn-only behavior
> above. See [learn-cron-dashboard](../../system-specs/modules/learn-cron-dashboard.md),
> § Tool-Refusal Recovery.

## Tool matching

Use `matcher` field. Supports canonical names and aliases:

- `"fs_write"` or `"write"` — match write tool
- `"execute_bash"` or `"shell"` — match shell
- `"@git"` — all tools from git MCP server
- `"@git/status"` — specific MCP tool
- `"*"` — all tools
- `"@builtin"` — all built-in tools only

## Hook types

### AgentSpawn
Runs when agent is activated. Exit 0 → STDOUT added to context.

### UserPromptSubmit
Runs when user submits prompt. Receives `prompt` field. Exit 0 → STDOUT added to context.

### PreToolUse
Runs before tool execution. Can block (exit 2). Receives `tool_name`, `tool_input`.

> **Kiro Crew:** any non-0/2 exit also blocks — see the divergence note under
> [Hook output](#hook-output).

### PostToolUse
Runs after tool execution. Receives `tool_name`, `tool_input`, `tool_response`.

### Stop
Runs when assistant finishes responding (end of each turn). No matcher. Useful for post-processing.

## Timeout

Default 30s (30,000ms). Configure with `timeout_ms`.

## Caching

`cache_ttl_seconds`: 0 = no caching (default), >0 = cache successful results. AgentSpawn hooks never cached.
