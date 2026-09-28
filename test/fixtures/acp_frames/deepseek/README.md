# DeepSeek Harness frame corpus

Six files, all live. Read `../README.md` first for what a fixture is and what
the corpus does and does not prove.

| File | Provenance | Frame classes it carries |
|---|---|---|
| `handshake-live.jsonl` | live | initialize response with `agentInfo.version`, `session/new` response with a `sessionId` and its config options, the `session/load` **rejection**, and the `session/list` result |
| `turn-live.jsonl` | live | `session/new` response, `usage_update`, `tool_call`, `tool_call_update`, `agent_message_chunk`, and the `session/prompt` result carrying `stopReason` |
| `mcp-stdio-mount-live.jsonl` | live | a stdio MCP mount completing a ROUND TRIP: `session/new` with a real server, then `tool_call` / `tool_call_update` for `mcp__crew-probe__crew_probe_echo` |
| `mcp-stdio-rollback-live.jsonl` | live | the same mount with an unstartable command: `session/new` fails WHOLE and names the server |
| `permission-request-live.jsonl` | live | `tool_call`, the `session/request_permission` the harness sends with Kiro Crew's gate plugin composed, and the `tool_call_update` through `completed` |
| `tool-call-id-reuse-live.jsonl` | live | the SAME `toolCallId` (`call_0`) across two consecutive turns of one session: `tool_call`, `session/request_permission` and a terminal `tool_call_update` for each, with the first turn's `session/prompt` result between them |

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

## What the permission capture establishes, and why it needed a plugin

`session/request_permission` is a required class, and on this harness's own
default composition nothing produces one. `permission-request-live.jsonl` is that
class captured live -- and what makes it fire is Kiro Crew's gate plugin
(`src/kiro_crew/agent_sdk/gate_extensions/deepseek/kiro_crew_tool_gate.mjs`)
composed through the harness's own per-launch `--patch` overlay. The plugin
answers the harness's `tools/pre-execute` waterfall with `{kind: 'ask'}`, its
tools core resolves an `ask` through `ctx.approval`, and its ACP bridge answers
that by emitting the frame.

Two control runs are what make the plugin the CAUSE rather than a coincidence,
and both were taken against the same prompt and the same locally served model:

| Run | Plugin composed | Client answered | Permission frame | The `bash` command |
|---|---|---|---|---|
| the fixture | yes | `allow-once` | **sent** | ran, output `kiro-crew-gate-probe` |
| negative control | **no** | -- | **none** | **ran anyway**, ungated |
| deny control | yes | `reject-once` | **sent** | **never ran** -- `failed`, `Error: the user rejected tool "bash"` |

The middle row is the gap this closes: with no plugin the harness runs the side
effect and asks nobody. The third is the half a permissive gate would fail --
denial reaches the tool, not just the transcript -- and it is the harness's own
contract doing it rather than anything Crew re-implements: in its tools core every
approval outcome other than `allowed-once` becomes a `deny`, and a missing
approval service denies too.

**What this fixture is, and is not.** It pins the WIRE SHAPE -- which frames the
harness sends, in what order, carrying which fields -- and it was captured by
driving the harness over stdio directly, without Crew's OS sandbox in the path.
That is the same way the rest of this corpus was taken and it is the right scope
for a replay fixture, but it is not evidence that a real session starts: the
sandbox is what decides whether the gate's load marker can be written at all, and
that question is settled by a test rather than by this file
(`test_the_load_marker_is_never_written_into_a_sealed_leaf`, plus the
marker-location reasoning in `agent-host-contract.md`). Read this fixture as
"these are the frames", not as "a session is admitted".

Two more things the capture establishes, both about what does NOT reach the gate.
Every tool asks -- there is no name allowlist -- so a `read` produces a permission
frame exactly as `bash` does; an earlier revision exempted the passive reads by
name, which left this harness's own credential files reachable with no frame and
therefore no sensitive-path check. And the composed patch pins the tool
presentation to `native`, so a `run_code` call is answered `Error: unknown tool
"run_code"`: under `ptc` the program inside it would reach Node's filesystem,
network and subprocess APIs directly, which are not tool calls and which no
`tools/pre-execute` listener can see.

Read the frame's `toolCall` for what this harness does NOT send. It carries only
`toolCallId` -- no title, no kind, no status, no `rawInput`. So the tool identity
and arguments the host gate judges come from the `tool_call` update immediately
before it, which is why the fixture keeps all three frames in wire order: the
correlation between them is the fact being pinned, and it is the toolCallId-keyed
cache `_dispatch.build_permission_event` already reads.

What #10373 recorded about the harness's own behaviour still holds unchanged, and
it is why the plugin is needed at all. Four live captures were taken then with the
approval policy pinned to `ask`, across both non-permissive sandbox postures:

| What the model called | Posture | What came back |
|---|---|---|
| `bash`, writing under the temp directory | `workspace-write` | `completed`, the file created |
| `bash`, writing under the home directory | `workspace-write` | `completed`, carrying `[sandbox: file access denied under workspace-write]` |
| `bash`, writing inside the workspace | `read-only` | `completed`, carrying `[sandbox: file access denied under read-only mode]` |
| the `write` tool, inside the workspace | `workspace-write` | `completed`, the file written |

None raised a permission request. The mechanism showed itself when the model
guessed an extra argument and the harness answered `invalid escalation:
sandbox_permissions requires a justification`: without a plugin the approval seam
is reached when the MODEL asks to escalate past the sandbox, not once per tool
call. An in-policy action runs silently and an out-of-policy one is denied by the
sandbox itself, with the denial in the tool result and a `status` of `completed`.

One thing this fixture does *not* pin. It was observed on the ACP bridge
reporting `agentInfo.version` 0.0.1, and nothing in this repository pins per-call
emission across upgrades the way the load-marker read-back pins that the plugin
loaded -- `agent-host-contract.md` records the version.

## What the id-reuse capture establishes

The shared gate tripwire (`acp/client.py`, `_tripwire_pi_gate` and
`_note_pi_gate_asked`) consumes an id's ask and deny at that call's terminal frame,
on the premise that a tool-call id is NOT unique for the life of a session.
`tool-call-id-reuse-live.jsonl` is that premise captured rather than asserted. The
model behind it was scripted to mint the same `tool_use` id, `call_0`, on the first
response of every turn -- what an OpenAI-compatible provider that numbers ids per
response does -- and the harness forwarded it unchanged: dsh's agent loop uses the
provider's id as its own `callId` and dsh-acp emits that as `toolCallId`. So one
session carries:

```
turn 1   tool_call call_0 -> session/request_permission call_0 -> tool_call_update call_0 completed
         session/prompt result  stopReason=end_turn
turn 2   tool_call call_0 -> session/request_permission call_0 -> tool_call_update call_0 completed
```

Two things follow, and both are what the tripwire rests on. An approval that
outlived its call WOULD vouch for the next call wearing the same id, so the state
has to be scoped to the call rather than to the session or the turn. And the
second terminal frame for an id arrives only with that id's second call, so
consuming at the terminal frame drops nothing a live call still needs.

## What was pruned, and by what

A live frame is host data until proved otherwise, so each frame was rebuilt field
by field rather than copied, and three things were pruned:

- the `session/list` summaries, each of which carried the recording host's
  absolute working directory;
- the `session/new` config options in `turn-live.jsonl`, which named the local
  provider route configured for the credential-free turn;
- the model catalog in `handshake-live.jsonl`, cut to one model option and one
  effort level -- a catalog is an inventory, and a corpus pins frame shapes.

Session ids, and the per-message `messageId` the harness mints on every
`agent_message_chunk`, are replaced with fixed synthetic values that are
deliberately not uuid-shaped: `scripts/check_acp_frame_host_data.py` reads any
uuid in a frame as a run id the recording host leaked, and a synthetic value that
looks like one cannot be told apart from one. The one path the model probed
outside its working directory in `turn-live.jsonl` was an absolute scratch path
on the recording host and is replaced with a relative placeholder that still lies
outside the working directory. Every reduction is named in the file's own
`_meta.note`.

The prune is enforced rather than remembered: the build script sweeps the written
files against host-marker patterns -- the recording username, `/home/`, the
scratch run id, the local provider and model names, the catalog entries that
were cut, and every pattern the repository's own host-data gate names -- and
refuses to finish if any survives. Re-record through that sweep
rather than by hand.
