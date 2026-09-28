# Hooks

Source: https://kiro.dev/docs/hooks/, plus its `types` and `actions` subpages
(fetched 2026-09-06; the former `/docs/cli/hooks/` redirects there — hooks are
documented as one page across every surface).

> **Carries local additions.** Kiro Crew's fail-closed `PreToolUse` exit contract
> has no upstream counterpart. A re-fetch must preserve it.

Execute custom commands, or inject an agent prompt, at specific points during
agent lifecycle and tool execution.

## Hook files

CLI 3.0 added standalone hook files at `.kiro/hooks/<id>.json`, activated when a
session starts. Any `.json` filename works and one file can define several hooks.
The embedded `hooks` field in an agent config still loads, and
`kiro-cli agent migrate` converts a 2.x config to the standalone form.

```json
{
  "version": "v1",
  "hooks": [
    {
      "name": "guard-writes",
      "trigger": "PreToolUse",
      "matcher": "fs_write",
      "action": { "type": "command", "command": "./guard.sh" }
    }
  ]
}
```

`action.type` is `command` (a shell command, receiving the event on STDIN) or
`agent` (a prompt injected into the conversation, which costs credits because it
starts an agent loop). `enabled: false` skips a hook without deleting it.

## Hook event

Hooks receive JSON via STDIN:

```json
{
  "hook_event_name": "preToolUse",
  "cwd": "/current/working/directory",
  "session_id": "abc123-def456-789",
  "tool_name": "read",
  "tool_input": { ... }
}
```

An MCP tool arrives under its namespaced name, such as `@postgres/query`.

## Hook output

- **Exit 0**: succeeded, STDOUT captured. For AgentSpawn/UserPromptSubmit the
  STDOUT is injected into context; for PreToolUse this is a delivered "allow".
- **Exit 2**: deny, on the gating events (PreToolUse and UserPromptSubmit here;
  upstream also lists the IDE's PreTaskExec), with STDERR returned to the LLM.
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
- `"fs_read"` or `"read"` — match read tool
- `"execute_bash"` or `"shell"` — match shell
- `"use_aws"` or `"aws"` — match the AWS CLI tool
- `"@git"` — all tools from git MCP server
- `"@git/status"` — specific MCP tool
- `"*"` — all tools
- `"@builtin"` — all built-in tools only

No matcher applies the hook to every tool.

## Hook types

### AgentSpawn
Runs when agent is activated, with no tool context. Exit 0 → STDOUT added to context.

### UserPromptSubmit
Runs when user submits prompt. Receives `prompt` field. Exit 0 → STDOUT added to context; exit 2 blocks the submission.

### PreToolUse
Runs before tool execution and gates the tool call: exit 0 allows, exit 2
denies, and **any other exit** (timeout, crash, unexecutable command) also
blocks the tool — a hook that cannot deliver a verdict fails closed. Receives
`tool_name`, `tool_input`.

### PostToolUse
Runs after tool execution. Receives `tool_name`, `tool_input`, `tool_response`.

### Stop
Runs when assistant finishes responding (end of each turn). No matcher. Useful
for post-processing. Upstream documents a block decision: a Stop hook that exits
0 and prints `{"decision": "block", "reason": "..."}` on STDOUT sends the reason
back as a new user message and the agent keeps going, which is how a script
gates a turn on tests or lint.

## Timeout

Default 30s (30,000ms). Configure with `timeout_ms`.

## Caching

`cache_ttl_seconds`: 0 = no caching (default), >0 = cache successful results. AgentSpawn hooks never cached.
