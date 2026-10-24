# Copilot CLI frame corpus

Five files, all live. Read `../README.md` first for what a fixture is and what the
corpus does and does not prove.

| File | Frame classes it carries |
|---|---|
| `handshake-live.jsonl` | `initialize` response with `agentInfo.version`, the `session/new` response with its `mode` and `allow_all` selects and `modes` block, `session/set_config_option` answers (including the two `-32602` refusals), `current_mode_update`, `config_option_update`, and an `/allow-all on` prompt the harness answers itself |
| `turn-live.jsonl` | `session/new` response, `usage_update`, `agent_message_chunk`, `tool_call`, `session/request_permission`, `tool_call_update` (`completed` and `failed`), and `session/prompt` results carrying `stopReason` |
| `mcp-stdio-mount-live.jsonl` | a stdio MCP server supplied at launch completing a ROUND TRIP: `tool_call` / `session/request_permission` / `tool_call_update` for the probe server's `marker` tool, then an edit-tool call that also asks |
| `session-load-live.jsonl` | a SECOND process's `session/load`: the conversation replayed as `user_message_chunk` / `tool_call` / `tool_call_update` before the answer, which carries `configOptions` and `modes`, then a prompt answered from the restored history |
| `compact-live.jsonl` | three ordinary turns with their `usage_update`, a `/compact` turn (the `Compacted conversation history` chunk, its `usage_update`, `end_turn`), and one ordinary turn after it |

**Nothing here is synthesized.** Captured off Copilot CLI 1.0.100
(`@github/copilot`, `copilot --acp --stdio`) driven directly over stdio, signed in
through the host's GitHub CLI login, which Copilot CLI reads as its lowest-priority
credential source. Session and tool-call ids are replaced with fixed synthetic
values and the recording host's scratch directory with `/work/copilot-probe`;
`available_commands_update`, `session_info_update` and `agent_thought_chunk` frames
are dropped (the host's command and skill inventory, the prompt's own title, and the
model's private reasoning). Each file's `_meta.note` says the same.

## What the permission capture establishes

`session/request_permission` fires PER TOOL CALL while `allow_all` is `off`, for a
builtin shell command and edit (`turn-live.jsonl`, `mcp-stdio-mount-live.jsonl`) and
for an MCP tool, so the host gate covers Crew's own tools and not only the harness's.
A `reject_once` answer fails the call with `code: rejected`. A file VIEW inside the
session's own directory runs without asking, the same residual Codex has.

What turns the asking off is the `allow_all` session config option. It is advertised
on every `session/new` and `session/load`, so Crew writes `off` before the first
prompt (`Routing.SESSION_CONFIG`). A restored session comes back as it was left, and
a prompt beginning `/allow-all`, `/yolo` or `/permissions allow-all` turns it on with
the `config_option_update` saying so arriving only on the next config write -- which
is why Crew refuses every slash command outside a short allowlist before the prompt
reaches the wire (`ACP_BACKEND_PROMPT_COMMAND_ALLOWLIST`), and stops the harness if a
session reports `allow_all` on or the `autopilot` mode mid-session
(`ACP_BACKEND_FORBIDDEN_MODES`).

## What the MCP capture establishes

Copilot CLI accepts only `http` and `sse` elements in the `session/new`
`mcpServers` array; a stdio element there is dropped with a line in its own log and
nothing on the wire. A stdio server named in `--additional-mcp-config` at launch is
started and served, which is how Crew mounts its stdio servers for this harness
(`acp/harness/copilot.py`).

## What the compact capture establishes

```
ordinary turns   used 22058 -> 30579 -> 39041
/compact turn    used 19913, "Compacted conversation history ...", stopReason end_turn
next turn        used 20843
```

The turn after `/compact` reads below the pre-compact peak, and nothing arrives after
the `/compact` turn's `end_turn`. Those two facts are the memberships in
`ACP_BACKENDS_COMPACT` and `ACP_BACKENDS_INLINE_COMPACTION`.
