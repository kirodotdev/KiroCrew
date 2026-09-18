# goose frame corpus

Five files, all live. Read `../README.md` first for what a fixture is and what the
corpus does and does not prove.

| File | Frame classes it carries |
|---|---|
| `handshake-live.jsonl` | `initialize` response with `agentInfo.version`, `session/new` response with a `sessionId` and its `modes` block, the `session/load` answer for an absent id, and the `session/list` result |
| `turn-live.jsonl` | `session/new` response, `usage_update`, `agent_message_chunk`, `tool_call`, `session/request_permission`, `tool_call_update` (`in_progress` then `completed`), and the `session/prompt` result carrying `stopReason` |
| `mcp-stdio-mount-live.jsonl` | a stdio MCP mount completing a ROUND TRIP: `tool_call` / `session/request_permission` / `tool_call_update` for `crew-probe__crew_probe_echo` |
| `mcp-stdio-dropped-live.jsonl` | the same mount with an unstartable command: `session/new` succeeds and drops the element |
| `session-load-live.jsonl` | `current_mode_update`, the `session/load` result for a live id, and the `session/resume` rejection |

**Nothing here is synthesized.** This harness produced all seven required classes on
its own wire, including the permission request, so there is no class written from a
declared shape.

Captured off `goose acp` 1.50.1 driven over stdio against a model served locally, so
no provider credential was involved.

## What the permission capture establishes

`session/request_permission` fires PER TOOL CALL, and it fires for both kinds of
tool: `turn-live.jsonl` carries one for a builtin `shell` command and
`mcp-stdio-mount-live.jsonl` carries one for an MCP tool, so the host gate covers
Crew's own tools and not only the harness's. All four ACP option kinds are offered —
`allow_always`, `allow_once`, `reject_once`, `reject_always` — which is the fullest
set in this corpus.

What makes it fire is the session's mode. This harness asks only in `approve`, and
its own default is `auto`, which auto-approves. Crew supplies `GOOSE_MODE=approve` in
the child's ENVIRONMENT, which this harness resolves above its own config file, and
the mode it resolved is reported in `modes.currentModeId` on the `session/new`
response — visible in both files. So the route is established before the first prompt
is sent rather than applied to a session that already exists, and the read-back needs
no second child.

## What the load capture establishes, and why it is the security-relevant one

The environment seed governs a session this harness CREATES. It does not govern one
it RESTORES.

`session-load-live.jsonl` carries `session/set_mode` being accepted on a live session
— including for `auto` — and then a `session/load` for that id returning the mode the
session was LEFT in. Driven a second time across processes: a session created in
`approve`, moved to `auto`, then loaded from a FRESH process whose environment still
said `approve`, came back `auto`.

So the read-back runs on the load path as well as on `session/new`. Without it a
resumed session would come back permissive and nothing would say so. This is the real
form of the ungated interval this harness has been described as having at session
start — which it does not have, because the mode is settled in the response that
opens the session.

`session/resume` answers `-32601 Method not found` while `session/load` is served and
`initialize` advertises `loadSession: true`; an absent id answers `-32002 Resource not
found` rather than `-32601`. That is the inverse of a harness that serves resume
without load, and it is why this backend needs no resume-without-load membership.

## What the MCP captures establish

`initialize` advertises `mcpCapabilities: {"http": true, "sse": false}` with no stdio
flag, which reads like a refusal and is not one: ACP v1's `McpCapabilities` names only
the OPTIONAL transports, and stdio is the baseline every v1 agent may serve. So the
premise is captured rather than declared.

`mcp-stdio-mount-live.jsonl` settles it end to end. `session/new` is sent one element
shaped exactly as `acp.session_mcp.acp_server_element` emits — name, command, args,
env — pointing at a minimal stdio MCP server whose one tool returns a fixed marker.
The turn then carries:

```
tool_call         title=crew-probe: crew probe echo   toolName=crew-probe__crew_probe_echo
tool_call_update  status=completed  content=[… "crew-stdio-mount-proved" …]
```

The server's own side agrees: it was asked `initialize`, `notifications/initialized`,
`tools/list` and `tools/call`. So the transport is mounted, the tools are enumerated,
and the tool is REACHABLE — not merely that an element was accepted.

Two things ride along. The tool grammar is `<serverName>__<toolName>` — a DOUBLE
underscore and no `mcp__` prefix — and the pair is carried BOTH in the human-readable
title and, separately, as `toolName` and `extensionName` under `_meta.goose.toolCall`.
That second channel is why this harness's `whole-server` per-tool-deny verdict is a
missing reader rather than a missing channel.

`mcp-stdio-dropped-live.jsonl` is the other half, and it is a hazard. The same element
with a command that cannot start does not fail `session/new`: the session is created
normally, in the required mode, with that element dropped. One pooled broker stub that
cannot start therefore costs no session here — and leaves a healthy-looking session
carrying none of Crew's tools, which is the state an outright failure never produces.

## What was pruned, and by what

A live frame is host data until proved otherwise, so each frame was rebuilt field by
field rather than copied, and these reductions were made:

- the provider and model CATALOG on every `session/new` and `session/load` result, cut
  to one option per select — a catalog is an inventory of what the recording host could
  reach, and a corpus pins frame shapes;
- the `agent_message_chunk` frames, 133 down to two, because a chunk's shape is what
  the parser reads and the model's prose was not written for this corpus;
- `session_info_update` and `available_commands_update` frames dropped whole: no
  required class needs them, and they carry a run id, wall-clock timestamps and the
  harness's own command inventory;
- `_meta.goose` pruned from the `tool_call` frame everywhere except the one field pair
  the MCP file cites it for.

Session ids, permission ids and the absent-session id are replaced with fixed
synthetic values. Every reduction is named in the file's own `_meta.note`.

The prune is enforced rather than remembered: the capture script sweeps these files
against eleven host-marker patterns — a username, absolute host paths, the scratch run
id, the local provider route, the local model name, the local endpoint, cut catalog
entries, the recording toolchain, real session ids, run and request uuids, and internal
markers — and refuses to finish if any survives. Re-record through that sweep rather
than editing a committed frame by hand: an edited frame is no longer evidence of what
the wire carried, and the `_meta` header claims it is.

These five files carry none of those markers, which is a property worth stating because
it is not true of the corpus as a whole. A repository gate holding every fixture to it
is a change to the SHARED corpus with its own revert path, so it lands separately from
this harness.
