# Pi frame corpus

Four files, all live. Read `../README.md` first for what a fixture is and what
the corpus does and does not prove.

| File | Provenance | Frame classes it carries |
|---|---|---|
| `session-live.jsonl` | live | initialize response with `agentInfo.version`, `session/new` response with a `sessionId` (a `model` select of `provider/model` ids, a `thought_level` select, a `modes` block), `available_commands_update`, a `tool_call` and two `tool_call_update` frames (`in_progress`, then `failed`) for a read the model called with no arguments, `agent_message_chunk`, and the `session/prompt` result carrying `stopReason` |
| `permission-request-live.jsonl` | live | `tool_call`, `tool_call_update`, the `session/request_permission` the adapter forwarded from Kiro Crew's gate extension, the updates through `completed`, and the result |
| `session-load-live.jsonl` | live | a `session/load` from a second process: the replayed `user_message_chunk` / `agent_message_chunk` / `tool_call` updates and the load result, which carries `configOptions`, `models` and `modes` |
| `compact-live.jsonl` | live | the initialize and `session/new` responses, four ordinary turns (`agent_message_chunk`, `usage_update`, `session/prompt` result), the `/compact` turn (pi's `Compaction completed` chunk and an `end_turn` result with no `usage_update` and no status frame), and one ordinary turn after it |
| `mcp-stdio-mount-live.jsonl` | live | the initialize and `session/new` responses, a `tool_call` for `mcp__probe_server__echo`, the `session/request_permission` the adapter forwarded from Kiro Crew's gate extension, the updates through `completed` carrying the server's own result, and the result |

Captured off `pi-acp` 0.0.33 spawning `pi` 0.85.1 and driving a locally
served model configured in pi's own `models.json`, with agent-to-client lines
verbatim. The working directory in the frames is a scratch project, not a home
directory.

## What the compaction capture establishes

`compact-live.jsonl` was taken with `scripts/capture_acp_compaction.py` off `pi-acp`
0.0.34 spawning `pi` 1.2.0, against a scripted OpenAI-compatible endpoint on the
loopback interface named in pi's own `models.json`. That endpoint reports `prompt_tokens`
as the character count of the messages pi sent, divided by four, so the `used` series
measures what pi itself put on the wire. Over four ordinary turns `used` climbed
12773 -> 25127 -> 37481 -> 49834. The `/compact` turn ended `end_turn` inside the same
`session/prompt` request with pi's own `Compaction completed` chunk and nothing after
it. The next ordinary turn added another ~12k-token prompt and read 37518, below the
pre-compact peak. That is the bar `ACP_BACKENDS_COMPACT` holds its members to, and the
absence of a status frame is what puts pi in `ACP_BACKENDS_INLINE_COMPACTION` too.

## What the MCP mount capture establishes

pi-acp accepts the `session/new` `mcpServers` array and never hands it to pi, so
`mcp-stdio-mount-live.jsonl` was driven with the array AND the per-session handoff
file the launch writes (`acp.harness.pi.pi_mcp_handoff_payload`), named in
`KIROCREW_PI_MCP_HANDOFF`. Taken off `pi-acp` 0.0.34 spawning `pi` 1.2.0 through
the gate launcher, against a scripted model on the loopback
interface that called `mcp__probe_server__echo`. The gate extension registered the
one stdio server with pi's built-in MCP support; the server's own log recorded
`initialize`, `notifications/initialized`, `tools/list` and `tools/call`; the call
raised `session/request_permission` through the gate first; and the server's result
(`ECHO:ping`) reached the turn. The same drive with a same-named server in pi's own
`mcp.json` ended the call `failed` with the gate's refusal, and the file-configured
server was never asked `tools/call`. An array-only drive spawns no server at all.

## What the permission capture establishes

Pi has no permission gate of its own: `pi` runs every tool call without asking, and
`pi-acp` sends `session/request_permission` only when an extension inside `pi`
raises a confirm dialog. So the frame in `permission-request-live.jsonl` exists
because Kiro Crew's gate extension (`agent_sdk/gate_extensions/pi/`) was loaded
into that `pi` through the gate launcher, and it is the observation the enforcement
story needs: the extension intercepted the model's `bash` call, the adapter
forwarded the dialog as `session/request_permission`, and the command ran only after
the client answered `yes`. The same prompt against the same `pi` without the
extension produced no permission frame at all and the command ran regardless.

Read the frame's `toolCall` for what the adapter is actually describing: it is the
DIALOG — `kind: "other"`, a `pi-ui-…` `toolCallId`, `rawInput` of `method` /
`title` / `message` — and not the tool call. The tool call is in `message`, as the
JSON envelope the extension wrote (`"kiro-crew-gate": 1`, the per-session `nonce`
the host placed in the pi process's environment, the harness's own `toolCallId`,
the tool name, an ACP `kind`, the arguments). The dispatch parser reads it back out
only for a session that issued that nonce — the fixture's `_meta.gate_envelope_nonce`
is the value the recording session used, and the replay hands it to the parser the
way `AcpClient` does — and the `.expected.json` beside the fixture shows the event
that results, with the real tool name as its title and the arguments as its trusted
params. Replayed WITHOUT the nonce, the same frame is read as the dialog it is.

Two things it does *not* pin. It was observed on pi-acp 0.0.33 over pi 0.85.1, and
nothing in this repository pins per-call forwarding across adapter upgrades the way
the read-back pins that the extension loaded — `agent-host-contract.md` records the
versions. And the ORDER of the permission frame against the adapter's own
`tool_call` / `tool_call_update` frames is not fixed: the capture holds two calls,
and the frame arrives before the `tool_call` for the first and after the
`in_progress` update for the second, because the adapter emits those from pi's
`tool_execution_start` while the dialog is raised from the extension's `tool_call`
handler, and the two are not ordered against each other on the wire. The command
itself does not run until the dialog is answered, whichever frame lands first.

One more fact the capture records: a bash result on this adapter carries NO stdout
text. The `tool_call` frame's `content` is a `terminal` block naming the adapter's
terminal id, and the `completed` update carries `_meta.terminal_exit` with the exit
code and nothing else -- the output is meant to be read through the ACP
`terminal/*` methods, which Kiro Crew does not implement. The model saw the output
(its reply reports it); the transcript's tool pill does not.
