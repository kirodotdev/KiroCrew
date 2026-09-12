# DeepSeek Harness frame corpus

Five files: four live captures and one synthesized frame. Read `../README.md`
first for what a fixture is and what the corpus does and does not prove.

| File | Provenance | Frame classes it carries |
|---|---|---|
| `handshake-live.jsonl` | live | initialize response with `agentInfo.version`, `session/new` response with a `sessionId` and its config options, the `session/load` **rejection**, and the `session/list` result |
| `turn-live.jsonl` | live | `session/new` response, `usage_update`, `tool_call`, `tool_call_update`, `agent_message_chunk`, and the `session/prompt` result carrying `stopReason` |
| `mcp-stdio-mount-live.jsonl` | live | a stdio MCP mount completing a ROUND TRIP: `session/new` with a real server, then `tool_call` / `tool_call_update` for `mcp__crew-probe__crew_probe_echo` |
| `mcp-stdio-rollback-live.jsonl` | live | the same mount with an unstartable command: `session/new` fails WHOLE and names the server |
| `permission-request-synthesized.jsonl` | **synthesized** | `session/request_permission` -- the one required class this harness produced in no capture |

## What the MCP mount captures establish

This premise is load-bearing, so it is captured rather than declared. If stdio were
refused, `session/new` would fail WHOLE rather than degrade — the harness would be
broken, not merely tool-less — and its `initialize` advertises
`mcpCapabilities: {"http": true}` with no stdio flag, which reads like a refusal. It
is not one: ACP v1's `McpCapabilities` names only the OPTIONAL transports, and stdio
is the baseline every v1 agent may serve.

`mcp-stdio-mount-live.jsonl` settles it end to end. `session/new` is sent one element
shaped exactly as `mcp_gateway.session_servers._acp_server_entry` emits a pooled
broker stub — name, command, args, env — pointing at a real minimal stdio MCP server
whose one tool returns a fixed marker. The session is created, and the turn carries:

```
tool_call         title=mcp__crew-probe__crew_probe_echo  status=in_progress
tool_call_update  status=completed  content=[… "crew-stdio-mount-proved" …]
```

The server's own side agrees: it was asked `initialize`, `notifications/initialized`,
`tools/list` and `tools/call`. So the transport is mounted, the tools are enumerated,
and the tool is REACHABLE — not merely that an element was accepted.

Two things ride along. The tool title is the harness's own MCP grammar,
`mcp__<serverName>__<toolName>`, so the server name is in the title and the separator
is a double underscore rather than the `/` kiro-cli splits on. And no
`session/request_permission` fired for this call either, consistent with the routing
verdict below.

`mcp-stdio-rollback-live.jsonl` is the other half, and it is a hazard rather than a
capability. The same element with a command that cannot start fails `session/new`
outright, naming the server:

```
mcp-client(kirocrew-core): initial connection or tool synchronization failed
```

It does **not** drop the offending element the way codex-acp does. So one pooled
broker stub that cannot start costs a whole session here.

Captured off `dsh --profile acp` 0.0.1, driving a model served locally so no
provider credential was involved. Agent-to-client lines are verbatim except where
a `_meta.note` says a field was pruned.

## What the `session/load` rejection establishes

This is the fixture the restore path rests on. `handshake-live.jsonl` carries the
harness answering `session/load` with:

```
-32601  "Method not found": session/load
```

while its `initialize` result advertises `sessionCapabilities` of `close`, `list`
and `resume` and no `loadSession` flag. Both spellings are standard ACP v1: the
schema describes `session/resume` as resuming "an existing session without
returning previous messages (unlike `session/load`)", useful "for agents that can
resume sessions but don't implement full session loading".

So this harness is not a quirk to accommodate. It serves the optional standard
method for exactly this case, and `ACP_BACKENDS_RESUME_WITHOUT_LOAD` is what keys
both varying reads on the shared restore path: which capability advertises the
verb, and which verb is sent. Without membership a reopened session reads as
"cannot restore" and silently starts fresh every time.

## Why the permission frame is synthesized

`session/request_permission` is a required class, and no capture produced one.
That is a finding rather than a gap in the recording, and it is why this harness's
routing is `Routing.UNVERIFIED` and why it is absent from
`BASELINE_SELECTABLE_BACKENDS`.

Four live captures were taken with the approval policy pinned to `ask`, across
both of the harness's non-permissive sandbox postures:

| What the model called | Posture | What came back |
|---|---|---|
| `bash`, writing under the temp directory | `workspace-write` | `completed`, the file created |
| `bash`, writing under the home directory | `workspace-write` | `completed`, carrying `[sandbox: file access denied under workspace-write]` |
| `bash`, writing inside the workspace | `read-only` | `completed`, carrying `[sandbox: file access denied under read-only mode]` |
| the `write` tool, inside the workspace | `workspace-write` | `completed`, the file written |

None raised a permission request. The mechanism showed itself when the model
guessed an extra argument and the harness answered `invalid escalation:
sandbox_permissions requires a justification`: the approval seam is reached when
the MODEL asks to escalate past the sandbox, not once per tool call. An in-policy
action runs silently and an out-of-policy one is denied by the sandbox itself,
with the denial in the tool result and a `status` of `completed`.

So the synthesized frame is written from the shape the harness's own protocol
contract declares -- a one-shot allow/reject choice. It exists because the replay
corpus requires the class. It is **not** evidence that this harness asks.

## What was pruned, and by what

A live frame is host data until proved otherwise, so each frame was rebuilt field
by field rather than copied, and three things were pruned:

- the `session/list` summaries, each of which carried the recording host's
  absolute working directory;
- the `session/new` config options in `turn-live.jsonl`, which named the local
  provider route configured for the credential-free turn;
- the model catalog in `handshake-live.jsonl`, cut to one model option and one
  effort level -- a catalog is an inventory, and a corpus pins frame shapes.

Session ids are replaced with fixed synthetic values. Every reduction is named in
the file's own `_meta.note`.

The prune is enforced rather than remembered: the build script sweeps the written
files against host-marker patterns -- the recording username, `/home/`, the
scratch run id, the local provider and model names, and the catalog entries that
were cut -- and refuses to finish if any survives. Re-record through that sweep
rather than by hand.
