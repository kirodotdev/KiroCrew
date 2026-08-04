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

### PostToolUse
Runs after tool execution. Receives `tool_name`, `tool_input`, `tool_response`.

### Stop
Runs when assistant finishes responding (end of each turn). No matcher. Useful for post-processing.

## Timeout

Default 30s (30,000ms). Configure with `timeout_ms`.

## Caching

`cache_ttl_seconds`: 0 = no caching (default), >0 = cache successful results. AgentSpawn hooks never cached.
