# Pi frame corpus

Every file here is live. Read `../README.md` first for what a fixture is and what
the corpus does and does not prove.

| File | Provenance | Frame classes it carries |
|---|---|---|
| `session-live.jsonl` | live | initialize response with `agentInfo.version`, `session/new` response with a `sessionId` (a `model` select of `provider/model` ids, a `thought_level` select, a `modes` block), `available_commands_update`, a `tool_call` and two `tool_call_update` frames (`in_progress`, then `failed`) for a read the model called with no arguments, `agent_message_chunk`, and the `session/prompt` result carrying `stopReason` |
| `permission-request-live.jsonl` | live | `tool_call`, `tool_call_update`, the `session/request_permission` the adapter forwarded from Kiro Crew's gate extension, the updates through `completed`, and the result |
| `session-load-live.jsonl` | live | a `session/load` from a second process: the replayed `user_message_chunk` / `agent_message_chunk` / `tool_call` updates and the load result, which carries `configOptions`, `models` and `modes` |
| `compact-live.jsonl` | live | the initialize and `session/new` responses, four ordinary turns (`agent_message_chunk`, `usage_update`, `session/prompt` result), the `/compact` turn (pi's `Compaction completed` chunk and an `end_turn` result with no `usage_update` and no status frame), and one ordinary turn after it |
| `mcp-stdio-mount-live.jsonl` | live | the initialize and `session/new` responses, a `tool_call` for `mcp__probe_server__echo`, the `session/request_permission` the adapter forwarded from Kiro Crew's gate extension, the updates through `completed` carrying the server's own result, and the result |
| `member-panel-mount-live.jsonl` | live | the initialize and `session/new` responses for a crew member session, a `tool_call` for `mcp__kirocrew_panel__panel_publish`, the `session/request_permission` forwarded from the gate extension, the update through `completed` carrying the panel server's own answer, and the result |
| `zero-tools-refused-live.jsonl` | live | four turns on an agent spec declaring `"tools": []`: a `write`, a `bash` and an `mcp__probe_server__echo` call, each with the `session/request_permission` the gate extension raised and the `tool_call_update` that ended it `failed`, and a `frobnicate` call pi failed itself with no permission frame |
| `effort-live.jsonl` | live | the initialize and `session/new` responses on pi's own catalog model `claude-opus-4-8` (a `thought_level` select offering `max`), a `thought_level=max` write reading back `max`, a model write to `claude-sonnet-4-5` (select now off through `high`, current `high`), a `max` write there answered with success and read back `high`, and the `current_mode_update` / `config_option_update` after each write |
| `steering-live.jsonl` | live | the initialize, `session/new` and set-model responses, a `tool_call` for a `bash` sleep, the `session/request_permission` forwarded from Kiro Crew's gate extension, the updates through `completed`, the extension's consumed-steer notification (an `agent_message_chunk` carrying `_meta.piAcp.notify`), the model's answer to the steer, `usage_update`, and the `end_turn` result |
| `saved-spec-projection-live.jsonl` | live | the initialize and `session/new` responses of a crew member session on a saved spec the client confirmed, then one turn: a `tool_call` for the spec's own `mcp__probe_server__echo`, the `session/request_permission` forwarded from the gate extension, the update through `completed` carrying the server's answer, and the result |

Every file is captured off `pi-acp` 0.0.34 spawning `pi` 1.3.0, the pair the Real
Adapter lane pins in `test/real_adapters/package.json` (installed with `npm ci`
there), driving a scripted OpenAI-compatible endpoint on the loopback interface
named in pi's own `models.json`. `test/test_real_adapter_fixture_pins.py` reds when
either pin moves past a header. Agent-to-client lines are verbatim apart from the
reductions each `_meta.note` names (synthetic session and dialog ids, `<cwd>`, the
command inventory dropped). The working directory in the frames is a scratch
project, not a home directory. `session-live.jsonl` and `session-load-live.jsonl`
run without the gate extension, as the first captures did; the rest run through
Crew's own `PiLaunch` with the gate extension loaded.

## What the compaction capture establishes

`compact-live.jsonl` was taken with `scripts/capture_acp_compaction.py` off `pi-acp`
0.0.34 spawning `pi` 1.3.0, against a scripted OpenAI-compatible endpoint on the
loopback interface named in pi's own `models.json`. That endpoint reports `prompt_tokens`
as the character count of the messages pi sent, divided by four, so the `used` series
measures what pi itself put on the wire. Over four ordinary turns `used` climbed
13483 -> 25572 -> 37610 -> 49648. The `/compact` turn ended `end_turn` inside the same
`session/prompt` request with pi's own `Compaction completed` chunk and nothing after
it. The next ordinary turn added another ~12k-token prompt and read 13596, below the
pre-compact peak. That is the bar `ACP_BACKENDS_COMPACT` holds its members to, and the
absence of a status frame is what puts pi in `ACP_BACKENDS_INLINE_COMPACTION` too.

## What the MCP mount capture establishes

pi-acp accepts the `session/new` `mcpServers` array and never hands it to pi, so
`mcp-stdio-mount-live.jsonl` was driven with the array AND the per-session handoff
file the launch writes (`acp.harness.pi.pi_mcp_handoff_payload`), named in
`KIROCREW_PI_MCP_HANDOFF`. Taken off `pi-acp` 0.0.34 spawning `pi` 1.3.0 through
the gate launcher, against a scripted model on the loopback
interface that called `mcp__probe_server__echo`. The gate extension registered the
one stdio server with pi's built-in MCP support; the server's own log recorded
`initialize`, `notifications/initialized`, `tools/list` and `tools/call`; the call
raised `session/request_permission` through the gate first; and the server's result
(`ECHO:ping`) reached the turn. On the earlier pi 1.2.0 drive, the same drive with a
same-named server in pi's own `mcp.json` ended the call `failed` with the gate's
refusal, and the file-configured server was never asked `tools/call`; an array-only
drive spawned no server at all. Those two controls were not re-run on 1.3.0.

## What the member panel capture establishes

`member-panel-mount-live.jsonl` is the evidence `ACP_BACKENDS_MEMBER_PANEL` holds pi
to. Taken off `pi-acp` 0.0.34 spawning `pi` 1.3.0 through the gate launcher, for a
crew member session key. The array was not written by hand: `AcpClient`'s own resolver
composed it for that key on pi (`kirocrew-dashboard`, then `kirocrew-panel`, each with
the signed per-session token, `KIROCREW_SESSION_KEY` and the bound port of a
disposable gateway bound to loopback on a scratch data home),
`acp.harness.pi.pi_mcp_handoff_payload` sealed it into the
handoff file, and the gate extension registered both. The scripted model on the
loopback interface was offered all ten `mcp__kirocrew_panel__*` tools and called
`panel_publish`; the call raised `session/request_permission` through the gate
first; and the panel server's own answer reached the turn.

That answer is `identity_unattested`, and it is the expected one for this capture:
the server dialled the gateway with the session's token, and the gateway refused to
vouch for a session it had not created. A gateway-created pi session cannot run on
the recording host, which has no sandbox backend (no user namespaces), and Crew
refuses to spawn pi unsandboxed, so the capture was driven by a scratch stdio driver
outside the product path. What it pins is the half this membership adds: the panel
element reaches pi, is reachable, and is gated. The publish itself is the same route
every panel member uses (`api_agent_panel_publish`), and nothing in it reads the
backend.

## What the zero-tool capture establishes

`zero-tools-refused-live.jsonl` was recorded by the in-product recorder
(`KIROCREW_ACP_RECORD_FRAMES`) while an `AcpClient(acp_backend="pi")` session ran an
agent spec declaring `"tools": []`, off `pi-acp` 0.0.34 spawning `pi` 1.3.0 through the
gate launcher, against a scripted model on the loopback interface named in pi's own
`models.json`, with one stdio MCP server handed over through the per-session handoff.
The consumer driving it approved every permission event it was handed, the way a user
clicking yes would, and was handed none: the client answered each of the three
`session/request_permission` frames (`write`, `bash`, `mcp__probe_server__echo`) with
the advertised reject option itself (`AcpClient._deny_zero_tools`), pi ended each call
`failed`, no file was written, the `touch` never ran, and the MCP server's own log
recorded `initialize`, `notifications/initialized` and `tools/list` but no `tools/call`.
The fourth call named a tool pi does not have; pi failed it (`Tool frobnicate not
found`) before any extension saw it. The same drive on a spec with no `tools` key
handed the consumer all three requests and each took effect.

The recording host refuses user namespaces, so no sandbox backend runs there and the
routing's refuse-then-mask preflight refuses an enforced harness. The capture ran the
client with `sandbox_mode="off"`, replaced that preflight with "no mask", and stood in
for the sandbox wrap the pi Node probe runs in, as the Real Adapter lane does; the gate
extension seal, its read-back, the envelope nonce and the permission path ran as
shipped. The refusal is after the spawn and does not read the sandbox.

## What the saved-spec capture establishes

`saved-spec-projection-live.jsonl` is the evidence `ACP_BACKENDS_MEMBER_CAPABILITIES`
holds pi to. It was recorded by the in-product recorder while an
`AcpClient(acp_backend="pi")` session ran as a crew member on a saved spec, off
`pi-acp` 0.0.34 spawning `pi` 1.3.0 from a scratch npm prefix, through the gate
launcher, against a scripted model on the loopback interface. The saved intent was
stamped with the digest a save stamps over the spec. After `session/new` the client
confirmed the spec it consumed, which is the parse `_mirrorless_member_mounts`
recorded: the confirmation passed, the session reported the template loaded with no
projection gaps, and a turn then called the spec's own server, gated through
`session/request_permission` first, and got the server's answer. That server reached
pi the way a pooled server does, as a same-named element of the per-session handoff;
a server the gateway does not pool never reaches pi, and the Capabilities pane then
reads unverified rather than applied. The same drive with the spec edited after its
save was refused before its first prompt with `capability_runtime_unverified`, and the
server was never called. A third drive, whose saved `tools` list did not reference
the server, was confirmed with the `native_tools` gap (so the pane reads unverified),
and the pooled stub never reached pi: on a member session the stubs pass the spec's
`tools` allowlist, as a mirror's do, so the server was not spawned. The fixture holds
the first drive's frames; the other two are recorded in its header note.

The recording host refuses user namespaces, so as for the zero-tool capture the
routing's sandbox preflight was replaced with "no mask"; the gatewayd pooling was
replaced by the spec's own entry, so the call is observable at the server. The seal,
read-back, envelope nonce, permission path and member confirmation ran as shipped.

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

Two things it does *not* pin. It was observed on pi-acp 0.0.34 over pi 1.3.0, and
only the Real Adapter lane re-measures per-call forwarding on an adapter upgrade; the
read-back pins only that the extension loaded. And the ORDER of the permission frame
against the adapter's own `tool_call` / `tool_call_update` frames is not fixed. This
capture holds two calls, and the frame follows the `in_progress` update for both; the
first capture (pi-acp 0.0.33 over pi 0.85.1) had it arrive before the `tool_call` for
one of its two calls. The two orders are possible because the adapter emits those from pi's
`tool_execution_start` while the dialog is raised from the extension's `tool_call`
handler, and the two are not ordered against each other on the wire. The command
itself does not run until the dialog is answered, whichever frame lands first.

One more fact the capture records: a bash result on this adapter carries NO stdout
text. The `tool_call` frame's `content` is a `terminal` block naming the adapter's
terminal id, and the `completed` update carries `_meta.terminal_exit` with the exit
code and nothing else -- the output is meant to be read through the ACP
`terminal/*` methods, which Kiro Crew does not implement. The model saw the output
(its reply reports it); the transcript's tool pill does not.

## What the steering capture establishes

`steering-live.jsonl` is the evidence `ACP_BACKENDS_STEERING_REQUEST` holds pi to.
Taken off `pi-acp` 0.0.34 spawning `pi` 1.3.0, both installed into a scratch npm
prefix, through Kiro Crew's own `AcpClient` and `PiLaunch`: the gate extension sealed
and read back, the launcher, and this launch's steer channel
(`acp.harness.pi_steer`). The recording host has no sandbox backend (no user
namespaces), so the capture driver ran the session with the sandbox off and the
floor that refuses an unsandboxed pi neutered, in the driver only. The scripted model
on the loopback interface called `bash` with a four-second sleep; one second into it
the client steered, which writes a sealed file rather than a frame, so the steer
itself is not on the wire. After the tool result the extension queued the steer with
`pi.sendMessage(message, {deliverAs: "steer"})` at `turn_end`; when pi started that
message it notified the consumed envelope (the `kiro-crew-steer` marker, the gate
nonce in `_meta.gate_envelope_nonce`, sequence 1), which `AcpClient` reports as
`EVENT_STEER_CONSUMED` and never shows as text. The model's next request carried the
steer as its latest user message, its answer named the steered word, and the same
`session/prompt` ended `end_turn`. The client removed the steer file at that
terminal. `test_pi_steer.py` replays the turn through the client's real read loop.
