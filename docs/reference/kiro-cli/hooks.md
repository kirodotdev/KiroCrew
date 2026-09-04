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

- **Exit 0**: succeeded, STDOUT captured. For AgentSpawn/UserPromptSubmit the
  STDOUT is injected into context; for PreToolUse this is a delivered "allow".
- **Exit 2**: (PreToolUse only) deny tool execution, STDERR returned to LLM.
  On non-gating events an exit 2 is not a gate — its block marker is surfaced
  as injected context text rather than denying anything.
- **Any other exit** — including a timeout, a crash, or an unexecutable
  command:
  - **PreToolUse**: the tool call is **blocked** (fail closed). A gating hook
    that cannot deliver a verdict resolves to deny, so breaking, slowing, or
    deleting a deny hook cannot silently disable the policy it enforces. The
    block detail prefers `result.error`, then STDERR, then `exited with code N`.
  - **All other events**: warn-only — STDERR is shown as a warning and
    execution continues.

There is currently no per-hook advisory/fail-open opt-out for PreToolUse; that
is tracked in [#7547](https://github.com/kirodotdev/KiroCrew/issues/7547).

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
Runs before tool execution and gates the tool call: exit 0 allows, exit 2
denies, and **any other exit** (timeout, crash, unexecutable command) also
blocks the tool — a hook that cannot deliver a verdict fails closed. Receives
`tool_name`, `tool_input`.

### PostToolUse
Runs after tool execution. Receives `tool_name`, `tool_input`, `tool_response`.

### Stop
Runs when assistant finishes responding (end of each turn). No matcher. Useful for post-processing.

## Timeout

Default 30s (30,000ms). Configure with `timeout_ms`.

## Caching

`cache_ttl_seconds`: 0 = no caching (default), >0 = cache successful results. AgentSpawn hooks never cached.
