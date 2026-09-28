# ACP Server Module

## Overview

`kiro_crew/acp_server/` is the mirror image of [acp-client](acp-client.md). The
client half spawns kiro-cli and *drives* ACP; this half *serves* ACP, so an
ACP-aware editor (VS Code, Zed) can spawn `kirocrew acp` and drive it as an ACP
agent.

The motivation is editor-native review and chat. An ACP-aware editor renders a
tool call's diff and offers accept/reject natively, so serving the protocol gets
Kiro Crew editor-native review without Kiro Crew shipping any UI. The alternative —
pointing the editor straight at `kiro-cli acp` — loses everything the gateway
adds (memory, lessons, crons, heartbeat, subagents, context assembly), because
those are injected on the gateway side, not by the agent config.

Kiro Crew targets **ACP v1 baseline conformance**. The wire contract is strict:
the protocol version is negotiated, malformed frames and invalid parameters are
answered with the correct JSON-RPC error, capabilities are derived from what the
backend actually implements, and only valid ACP stop reasons ever reach the
editor.

## Layout

| File | Responsibility |
|------|----------------|
| `transport.py` | Bounded JSON-RPC 2.0 framing over newline-delimited streams; strict frame validation; agent-role id correlation |
| `server.py` | Method dispatch, session registry, parameter/version validation, `SessionSink`, permission gate |
| `mcp_config.py` | Parse/validate a session's client-supplied `mcpServers`; accept stdio, reject other transports |
| `cleanup_receipts.py` | Persist owner-only, idempotent MCP/partial-slot cleanup for replay after adapter exit |
| `mcp_supervisor.py` | Spawn, sandbox, proxy, and reap each session's untrusted stdio MCP servers |
| `gateway.py` | In-process `PromptHandler` that runs an editor turn through the gateway `LLMProvider` seam (`--standalone`) |
| `http_backend.py` | Default daemon-backed `SessionBackend` + `PromptHandler` proxying to the running gateway over HTTP/SSE; local-secret auth is loopback-only |

`types.py` (under `kiro_crew/acp/`) is shared with the client half and is
role-neutral — protocol strings, JSON-RPC error codes, capability keys, and the
set of valid ACP stop reasons live there, never inline in this module.

## Execution model: daemon-backed by default

`kirocrew acp` is a **stdio adapter to the running Kiro Crew gateway**. The
gateway owns model execution, context, memory, permissions, tools, and session
state; the adapter only translates the ACP wire protocol onto the gateway's
HTTP/SSE API (`http_backend.HttpGatewayBackend`). An editor session is therefore
a first-class dashboard session — persisted history, auto-title, tools, Slack
mirroring — visible in the dashboard sidebar. The adapter reads and sends the
local `X-Internal-Secret` only when the configured gateway URL is loopback. It
resolves the per-listener credential again before each HTTP request and WebSocket
connection, so a gateway restart that interrupts an SSE response does not leave the
long-lived editor adapter pinned to the prior generation's secret. A transiently
unreadable credential retains the last usable value until the per-port credential is
available. Gateway URLs must be absolute HTTP(S) URLs and cannot contain userinfo,
query parameters, or fragments; connection logs reconstruct only the scheme/host/port
origin and never include a configured path. Authenticated HTTP requests and
WebSocket handshakes send that validated origin and refuse redirects; gateway
response/transport details are companion-aware redacted before ACP egress. A 200
prompt stream is complete only after its `[DONE]` SSE sentinel; premature EOF is
surfaced as a turn error instead of committing partial output as `end_turn`. An
authenticated ACP prompt bound to the named slot returns `409 slot_busy` when that slot
is running or held by live subagents, rather than entering the dashboard's asynchronous
queue and executing after the editor reports failure.
A non-loopback gateway requires an explicit presigned token, so an
operator-supplied URL can never receive the host's internal IPC credential.
Both daemon-backed and standalone modes load configuration synchronously before
`asyncio.run`, so startup filesystem I/O cannot block protocol frame draining after
stdio attaches.

While connected, the adapter also holds an authenticated `/api/ws` subscription.
A dashboard `slot_title` frame for a session the ACP process registered becomes the
standard `session/update` `session_info_update` notification, so an editor refreshes
the title when the dashboard auto-titler or an operator renames the same session.
Malformed frames and titles for unregistered slots are ignored; a closed WebSocket
reconnects without interrupting active prompt SSE streams. Finalized user and
assistant rows use task-local ACP origin metadata: authenticated ACP SSE streams
carry each durable row id and origin, so rows produced by the initiating editor
turn are suppressed on their return path, while a concurrent dashboard steer runs
in a separate task context and reaches the editor as a user-message update with the
same durable id. Orchestrator
`go`/`stop` paths return JSON instead of an assistant SSE stream, so they clear that
origin before starting stage work or appending the stop confirmation; their output
then reaches the initiating editor through the authenticated ACP row relay.

`--standalone` is an offline diagnostic fallback that runs turns through an
in-process `SessionManager` (`gateway.make_prompt_handler`) built through the same
companion-aware provider registry as other CLI entry points; those turns are not
visible in the dashboard. Configuration, memory, lessons, skills, scripted hooks,
and the session registry are constructed synchronously before `asyncio.run`, so
startup filesystem I/O cannot block protocol frame draining after stdio attaches.
Raw provider text and thinking use independent rolling stream redactors, including a
final flush, so credentials split across provider chunks cannot cross the ACP boundary.
The validated `session/new` cwd is carried on every
`PromptRequest` and into `SessionManager.get_or_create`, so the standalone
provider is bound to the editor workspace rather than the launcher directory.
Lifecycle CWD validation resolves the sensitive-path decision off the event loop and
rejects protected credential, policy, and Kiro Crew state paths before a provider or
backing slot is created. Standalone startup warms the SEL singleton off-loop before
stdio frame processing, so the first tool audit cannot perform trust-root and log I/O
on the protocol loop. The standalone bridge emits SEL `invoked` records for
observed tool calls and `approved` / `rejected` / `denied` records for every
permission decision before acting on it. Both the declarative `HookManager` gate
and the persisted scripted `PreToolUse` hooks run before an editor permission
request. Script matchers consider the adapter-authored canonical tool name and the
display title in one pass, while hook input falls back to validated JSON when raw
parameters are absent; each matching script executes at most once per event. An
exit-2 denial, hook failure, invalid result, or unavailable store rejects the tool
without consulting the editor. The daemon-backed path retains the dashboard runner as
its audit and scripted-hook owner, so the two modes never double-run hooks or double-log.
Kiro Crew also projects its dashboard task snapshots
into the standard ACP `session/update` `plan` notification. Each complete
`todo_update` snapshot is sent only to the ACP adapter registered for that slot;
completed tasks map to `completed`, the first unfinished task to `in_progress`,
and later unfinished tasks to `pending`. The adapter sends the full entry list on
every change, as required by ACP plan replacement semantics. The underlying
`todo_list` bookkeeping call is internal and is not projected as an ordinary ACP
tool card, so editors show the useful plan entry without a duplicate tool-activity
row. Browser and app WebSocket clients never receive this ACP-only event. The
ACP-specific code never reads or mutates session history files directly — session
state is owned by the backend.

## Method Surface

Client→agent requests answered:

| Method | Behaviour |
|--------|-----------|
| `initialize` | Negotiates integer ACP **v1**; advertises capabilities derived from the backend |
| `session/new` | Validates `cwd` (absolute) + `mcpServers`; mints/creates a session; replies `{sessionId}` |
| `session/prompt` | Validates `sessionId` + `prompt` blocks; delegates to the `PromptHandler`; replies `{stopReason}` |
| `session/load` | **Backend-gated.** Activates a session and replays its history as companion-aware redacted `session/update`s |
| `session/list` | **Backend-gated.** Lists project-scoped sessions by `cwd`, plus ACP-created relocation candidates; path canonicalization runs off the event loop |
| `session/resume` | **Backend-gated.** Resumes a session without replaying history |
| `session/set_mode` | **Backend-gated.** Applies an advertised reasoning mode; returns `{}` and emits `current_mode_update` |
| `session/set_config_option` | **Backend-gated.** Applies an advertised select option; returns and emits the complete `configOptions` snapshot |
| `session/set_model` | `-32601 Method not found` — a kiro client-side extension, not an agent-role ACP method |
| anything else | `-32601 Method not found` |

`session/cancel` is a notification; it sets the session's cancel event and, when
an active backend turn is present, starts one authoritative backing stop. The
pending `session/prompt` result waits for that stop: confirmed success returns
`cancelled`, while a failed gateway stop returns `-32603` instead of claiming the
backing turn ended. Cancellation remains authoritative over an editor response
already buffered in the same event-loop turn: a pending permission denies and a
pending elicitation returns no answer.

Backend-gated methods answer `-32601` when no backend is attached (or the
backend does not advertise that capability), so an editor that calls an
unadvertised method gets a definite answer rather than hanging.

**Every unrecognised request is answered, never dropped.** JSON-RPC gives the
peer no timeout: an unanswered request blocks the editor forever. This mirrors
the client half's `_reject_unknown_server_request` discipline.

## Protocol Version

Negotiated, not echoed. Kiro Crew supports exactly integer **v1**
(`SUPPORTED_PROTOCOL_VERSION = 1`) and always responds with it, so the client
learns the version the agent will actually speak and can decide whether to
proceed. An unrecognised value a peer offers (for example kiro-cli's
`"2025-08-22"` date string, used only on the *client* half) is **not** echoed
back — echoing it would claim to speak a protocol variant the agent does not.
`DEFAULT_PROTOCOL_VERSION` remains as a back-compat alias equal to the supported
version.

## Capabilities

Advertised capabilities are **derived from implemented backend behaviour**, so
they exactly match end-to-end reality:

- `agentCapabilities.loadSession` is `true` only when the backend advertises
  `supports_load`.
- `agentCapabilities.sessionCapabilities` gains `list` / `resume` only when the
  backend advertises `supports_list` / `supports_resume`.

Without a backend, only the self-contained surface is advertised
(`loadSession: false`, no `sessionCapabilities`) and the optional methods
`-32601`. A capability is never advertised for a method that would not succeed.

### Session selectors

A backend that exposes `get_session_selectors`, `set_session_mode`, and
`set_session_config_option` enables `session/set_mode` and
`session/set_config_option`. New, loaded, and resumed sessions advertise the
current `modes` and `configOptions` snapshot; without that backend contract both
methods return `-32601`.

Selector changes are serialized with prompts. A prompt while a selector mutation
is running, or a selector mutation while a prompt is running, returns `-32602`.
Requests may select only advertised mode IDs and select-option values. A successful
mode change returns `{}` and emits `current_mode_update`; a successful config
change returns and emits the full `configOptions` snapshot. Backend failures return
`-32603` without an update, so the editor retains the last known-good selection.
`session/set_model` always returns `-32601`: it is a kiro client extension, not an
agent-role ACP method.

### Available slash commands

After `session/new`, `session/load`, or `session/resume` succeeds, a backend with
`get_available_commands` emits the standard `available_commands_update` session
notification. The default HTTP backend reads the gateway's provider-aware
`GET /api/slash-commands` catalog through the loopback internal-auth route, removes the
display-only leading `/`, filters malformed or duplicate entries, and advertises only commands
the dashboard can execute. Discovery is best-effort: an unavailable catalog never fails session
creation or recovery, and standalone mode advertises nothing until it has an equivalent
command-discovery backend.

### Editor elicitation

A client that explicitly advertises `clientCapabilities.elicitation.form` can
receive standard `elicitation/create` form requests. `SessionSink.create_elicitation`
uses a session-scoped request without a tool-call id, has no wall-clock deadline while
waiting for a human response, races the request against `session/cancel`, and treats
malformed, declined, cancelled, EOF, and transport errors as no answer. Adapter shutdown
also cancels pending forms. It is separate from `session/request_permission`: elicitation
never grants a tool or changes trust state.

The daemon-backed HTTP bridge reads only the mapped slot's pending, validated
question-card state. It projects each unanswered question to one object form
with an `answer` field (single-select strings, or multi-select string arrays),
and tracks the request by slot, card id, and question index. The slot-scoped read
and answer routes accept either a verified loopback internal-secret caller or the
authenticated dashboard owner used by a remote presigned-token adapter; app tokens
and non-owner dashboard users remain denied. An accepted value uses the gateway's
idempotent answer operation; the gateway validates and records it first. A partial
multi-question answer returns a successful JSON acknowledgment, which the bridge
consumes without entering the SSE parser. The completing answer streams the
resulting ordinary user turn through the same ACP session. Before the card is
consumed, the gateway refuses completion while an orchestrator stage or live
subagent holds the slot, preserving the pending card and the plan's task ownership
for retry after the hold clears. The trailing `[OPTIONS:]` fallback likewise submits
and streams its selected labels as an ordinary user turn. Before that follow-up
starts, the answered options request releases its slot-scoped task key; a recursively
returned options trailer can therefore create the next elicitation, and
generation-checked cleanup prevents the completed task from deleting its replacement.
Adapter close or session cancellation leaves a card pending for recovery after
reconnect.

Load/resume rollback is conditional across adapters. A locked loopback project
assignment returns both an opaque per-mutation slot generation and the exact project
it replaced, so a failed adapter restores that atomic predecessor only while its
generation remains current. Predecessor disclosure remains internal-secret-only. A
remote owner-authorized adapter snapshots the prior project before assignment and may
submit the same generation-checked restore without gaining predecessor disclosure.
A project selected while either adapter is activating is therefore never replaced by
a stale rollback. Generations do not repeat across gateway reconstruction. A
transport failure retains the project restore target and generation; later activation
is fenced until retry receives a confirmed applied-or-stale response. Project
assignments and slot MCP registrations carry client-minted mutation IDs; the gateway
retains a bounded set of exact responses per slot, so a transport-ambiguous retry
recovers the original generation or prior-registration snapshot without reapplying
over newer ownership. Re-hosting a prior local MCP set uses the same atomic owner
check as registration snapshots. A newer adapter's project or MCP owner is never
overwritten by stale cleanup.

Adapter cleanup is durable across process exit. Before publishing a non-empty
adapter-owned MCP registration, the HTTP backend writes an owner-only clear receipt
under `acp-cleanup-receipts/` and retains it for that registration's lifetime. The
receipt pins the adapter PID and process-start identity; concurrent adapters skip it
while that exact process remains live, and PID reuse cannot make a stale process look
like the owner. Partial
`session/new` slot deletion likewise records its cleanup intent before the remote
mutation. Every later adapter for the exact same gateway base replays bounded batches
of receipts whose owner has exited until the gateway confirms completion. MCP
receipts retain the original owner and mutation id, so a newer owner is untouched and
an ambiguous response is idempotent. Partial-slot deletion carries a stable hash of
the assigned project when available and always requires the slot to remain
message-free; a missing slot or a project/message mismatch retires the receipt without
deleting newer or adopted work. The receipt directory is on the shared sensitive-path floor
because forging a cleanup receipt would be a slot-deletion capability.

The package initializer resolves ACP server exports lazily, so gateway imports of the
shared location helper do not load HTTP, subprocess-supervision, or protocol runtime
modules on the boot path. When the editor pipe closes, the server requests and awaits
backing cancellation for every in-flight turn before draining local handlers. Lifecycle
project activation is shielded through generation receipt capture; cancellation then
performs the same generation-checked rollback before it propagates.

The existing `_meta.kirocrew.options` extension remains for clients without the
form capability. For elicitation-capable clients, a bounded streaming filter
removes only a complete trailing `[OPTIONS:]` marker before it reaches the
editor; the active slot's structured options are then a multi-select array
fallback only when no canonical question is pending. `[OPTION:]` remains the
explicit single-select form. Dashboard and messaging renderers remain
unchanged.

## JSON-RPC framing strictness (`transport.py`)

The transport answers malformed input instead of silently discarding it:

| Condition | Response |
|-----------|----------|
| Unparseable bytes | `-32700 Parse error`, `id: null` |
| Valid JSON but not an object (array/scalar) | `-32600 Invalid Request`, `id: null` |
| Missing/invalid `jsonrpc` (`!= "2.0"`) | `-32600 Invalid Request`, id echoed if usable |
| Frame with neither `method` nor an id member | `-32600 Invalid Request`, `id: null` |
| `method` present but not a string | `-32600 Invalid Request` |
| Request `id` present but neither null, string, nor number | `-32600 Invalid Request`, `id: null` |

A request id may be a string, a non-bool number, or explicit null. Presence is retained
separately from the decoded value: an absent id is a notification, while an explicit-null
id is a discouraged but valid request that is dispatched and answered with `id: null`.
An error whose triggering frame carried no valid id is answered with `id: null` per the
spec. None of these are fatal — one bad line from a noisy peer must not
end the session, and a following valid frame is still processed. Production
stdio readers accept frames up to 10 MiB so ordinary editor media does not hit
asyncio's 64 KiB default. A larger line is drained exactly to its newline,
answered with bounded `-32600 Frame too large`, and the next frame remains usable;
a line that exceeds the bounded drain budget closes the pipe.

POSIX hosts attach stdin/stdout to asyncio pipe transports. Windows standard
anonymous pipes are synchronous handles, so reads and complete descriptor writes run
through `asyncio.to_thread`; they are never registered with Proactor IOCP. Both paths
feed the same bounded `StreamReader` and serialized frame writer.

A genuine **response** to one of our outbound requests (id set, no method) is
still routed to the pending-future resolver and dropped if unknown; it is never
mistaken for an invalid request.

## Parameter validation

Requests are validated structurally before any work; a bad shape earns
`-32602 Invalid params`:

- `cwd` on `session/new` / `session/load` / `session/resume` must be a
  non-empty **absolute, non-sensitive** path. Sensitive-path resolution runs off the
  event loop and rejects the request before provider or backing-slot creation.
- `sessionId` on `session/prompt` / `session/load` / `session/resume` must be a
  non-empty string. A well-formed request for a *nonexistent* session is a
  distinct case answered `-32602 Invalid params` (unknown session).
- `prompt` on `session/prompt` must be an array of content blocks. Every block
  must use a known ACP v1 type and satisfy that variant's required payload types
  before text projection; malformed or unknown blocks fail with `-32602` rather
  than silently disappearing. An empty array is a valid (contentless) turn.
- `mcpServers` is validated as below.

`PromptRequest` carries the validated session `cwd` and the original content
blocks (`content_blocks`) alongside the flattened `text`. Standalone allocation
uses the cwd directly; a backend that can act on structured content (e.g.
resource links) has it at the boundary rather than only the lossy text
projection. `prompt_blocks_to_text` performs the documented, preserve-what-we-can
flattening for the text-only chat core (text verbatim; `resource_link`→uri;
`resource`→embedded text or uri; image/audio→a placeholder when a handle exists).

`session/load` and `session/resume` reserve the session id across every awaited
backend and MCP step. Prompts, selector changes, and sibling lifecycle requests
are rejected while that reservation is held. Backend activation snapshots the
prior project before mutation: a failed or cancelled create deletes its partial
slot, while a failed or cancelled load/resume restores the prior project
(including an intentionally empty value), local session, and MCP registration
before cancellation propagates. The loopback gateway returns the pre-replacement
MCP registration atomically with the mutation only to its internal adapter; rollback
restores that snapshot only while the failed adapter still owns the slot, so setup
failure cannot clear another adapter and a newer owner cannot be overwritten.
Successful response delivery is the
commit point: if history or the result frame cannot be delivered, the same rollback
runs and a newly created slot is deleted. Command advertisement happens only after
that commit. A lifecycle request cannot silently rebind the next dashboard turn.

## Client-supplied MCP servers (`mcp_config.py`)

An ACP client passes `mcpServers` per session. `parse_mcp_servers` validates the
array structurally and returns typed, session-scoped `StdioMcpServer` configs:

- Baseline **stdio** transport only — `command` + `args` + `env` (the ACP
  array-of-`{name,value}` shape or a plain object). The parsed config is stored
  on the session and handed to the backend through an optional
  `configure_session_mcp` hook. Because the trusted proxies use adapter-local
  executable, Unix-socket, and token-file paths, a non-empty set requires a
  loopback gateway; remote gateways reject it before spawning any child rather
  than registering capabilities the gateway host cannot access.
- **Unsupported transports (HTTP/SSE) are rejected with `-32602`**, not silently
  ignored: a client that asked for a server it will not get must be told, not
  left believing a tool is available.
- Malformed entries, a missing `command`, or duplicate names (compared
  case-insensitively) are `-32602`. Managed server names and the entire
  `<app>:<server>` namespace are reserved case-insensitively, so client servers
  cannot shadow a trusted control-plane or shipped builtin-app identity. Error
  messages name the offending index/field and never echo a secret value.

**Process supervision (`mcp_supervisor.py`).** The daemon-backed
`http_backend.py` hosts each editor-supplied stdio server once under the ACP
adapter's ownership:

1. `SessionMcpSupervisor.host` prepares the augmented executable search path,
   removes loader/interpreter injection and reserved Kiro Crew environment keys,
   and resolves the command off the ACP event loop. Sanitization happens before
   launcher construction, so `LD_PRELOAD`, `DYLD_INSERT_LIBRARIES`, and related
   variables cannot execute client code before confinement. The supervisor then
   applies Kiro Crew's **strict** sandbox (all host credential stores hidden),
   credential-environment scrub, process-group isolation, and resource limits and
   retains the child for that ACP session. Before the first child starts, the
   supervisor opens and validates the workspace as a pinned directory identity;
   every initial and reconnect spawn enters that retained descriptor after sandbox
   preparation. Renaming the workspace pathname or replacing it with a symlink
   therefore cannot redirect a later child's inherited CWD into a credential tree.
   Relative MCP file operations remain bound to the workspace selected for that
   session. Recursive proxy-directory cleanup is dispatched off the ACP event loop during replacement,
   failure, and shutdown.
2. Each child is exposed through a token-guarded Unix socket. Every socket and
token lives under one supervisor-owned root that is hidden from every untrusted
child sandbox, so a child cannot discover another session's proxy capability.
The gateway slot receives only a trusted `mcp_proxy.py` stdio spec; the original
command, arguments, and environment never reach kiro-cli.
3. The MCP `initialize` exchange flows end-to-end through the proxy. The first
   proxy uses the hosted child; every later authenticated proxy generation reaps
   and replaces that child before relaying bytes. Each real child is therefore
   initialized exactly once, while provider crash/recreation still reconnects
   through the stable socket capability and startup errors surface to the editor.

The slot MCP registration endpoint accepts only the dashboard owner or a verified
loopback internal-secret caller. App tokens cannot register commands, including
for a slot owned by that app; this prevents App Kit isolation from becoming a
host-process execution path.

The next prompt threads the proxy set through `SessionManager.get_or_create` →
`create_provider_factory` → `AcpProvider` → `runtime.create_session` or
`runtime.load_session`. Kiro-cli owns only the trusted proxy process; the ACP
adapter remains the sole owner of the untrusted child.

- **Config-identity reuse** — `get_or_create` fingerprints the client-supplied
  set (`_mcp_fingerprint`, order-independent). An unchanged or empty set reuses
  the live provider; a changed set recreates it because ACP binds MCP servers at
  session creation/load. Both a live-session replacement and a concurrent cold-start
  mismatch acquire the session lease before eviction, then recreate instead of
  aborting an active turn or inheriting the wrong MCP set.
- **Ownership and isolation** — configuration is scoped to one dashboard slot.
  `session/new`, `session/load`, and `session/resume` replace that slot's set;
  an empty registration clears it. Before a non-empty gateway registration can
  begin, the adapter durably records an owner-scoped clear intent and retains it
  until that generation is confirmed cleared. Each adapter replacement carries an opaque
  owner generation, so adapter EOF and failure cleanup compare-and-clear only
  their own registration and cannot erase a newer adapter's set. Lifecycle
  rollback uses the same comparison before restoring the atomic prior-registration
  snapshot; it neither clears a registration it never replaced nor overwrites a
  later replacement. A surviving adapter retains only its trusted proxy specs when
  the gateway restarts; per-port credential rotation marks those registrations
  stale, and the first subsequent turn atomically restores each set only while the
  reconstructed slot is still unowned. A newer adapter owner is never overwritten,
  and failed restoration rejects the turn rather than silently dropping editor tools.
  Gateway-backed
  prompts and streamed elicitation answers present that same generation before
  either a turn starts or a pending card is consumed, and snapshot its proxy set
  at admission; the identity headers are honored only for an internal-secret
  caller or the canonical authenticated dashboard owner, including a remote
  presigned-token adapter. App and non-owner requests cannot claim ACP ownership.
  Once another adapter replaces the slot registration, a stale adapter receives
  `409 mcp_owner_stale`, and a replacement racing later setup cannot substitute
  its proxies into the admitted turn.
- **Failure surfacing** — spawn, initialization, or proxy-registration failure
  reaps all partial children, clears the slot's proxy registration so dead
  sockets cannot remain configured, and returns `-32603` with a secret-safe
  message. Unsupported or malformed transports return `-32602` before any
  spawn.
- **Teardown** — reconfiguration, cancellation, adapter EOF, SIGTERM, and shutdown close
  proxy sockets and reap complete process trees with SIGTERM→SIGKILL. The CLI turns
  the first SIGTERM into cooperative serve-task cancellation and awaits backend
  cleanup before exiting; a later hard exit leaves the lifetime registration receipt
  for owner-checked replay by the next adapter. Cancellation
  during a multi-child teardown synchronously reaps every remaining owned child;
  cancellation during replacement is shielded through receipt of the atomic prior
  registration, restores it only if the cancelled adapter still owns the slot, and
  then propagates cancellation. Cleanup retains
  adapter ownership so `close()` retries if the strict clear cannot finish. POSIX
  retains the isolated group id so stubborn descendants remain reachable after
  the leader exits, but signals it only while the live leader still belongs to
  that group or no process occupies the reaped leader PID; the id is cleared after
  one signal so a recycled group is never targeted. Windows uses the platform
  tree-kill helper.

There is no dedicated `session/close` in ACP v1. Mid-life teardown occurs on
reconfiguration; adapter shutdown reaps every remaining hosted child. The
closed-box conformance gate drives this through the public stdio adapter against
an isolated gateway stub.

## Invariants

### Requests dispatch OFF the read loop

`AgentTransport._dispatch` starts each request as a task and never awaits the
handler inline. Awaiting inline deadlocks: a handler may itself await *inbound*
data — `request_permission` waits for the editor's answer — so the read loop
would be blocked on the very frame it must read to make progress. It also makes
`session/cancel` unobservable until the turn it cancels has already ended.

In-flight tasks are held in `AgentTransport._tasks`. asyncio keeps only a weak
reference to a running task, so a fire-and-forget task can be garbage-collected
mid-flight; on EOF, `_drain_tasks()` gives handlers 5s then cancels them.

### Request-id namespaces are independent

Our agent→client request ids (`session/request_permission`) and the peer's
client→agent request ids are separate counters that collide on small integers.
Response correlation therefore requires `id` match **and** `method is None`
(`JsonRpcMessage.is_response_for`). Regression-guarded by
`TestFramingRobustness::test_inbound_request_id_collision_is_not_a_response`.

### Permission is fail-closed

`SessionSink.request_permission` returns True **only** for
`outcome == "selected"` with `optionId == allow_once`. A cancelled outcome, a
reject option, a JSON-RPC error, a malformed result, or a transport failure all
deny.

### Hooks run before the editor is consulted

Per [acp-client](acp-client.md), per-call `session/request_permission` exists so
Kiro Crew's PreToolUse hooks (`auto_deny_tools`, sensitive-path checks, credential
redaction) fire on every tool, and **Kiro Crew — not the peer — owns trust
scope**. An editor approval must never *replace* hook evaluation: evaluate hooks
first, consult the editor only if they pass, and a hook DENY is final. The
standalone bridge passes the namespaced session and agent identity, semantic tool
kind, trusted raw parameters, raw shell command, and canonical MCP server/tool
identity into `HookManager.on_tool_call`; a display title alone is never a
security boundary.

### Untrusted text leaving Kiro Crew

Diff bodies, file paths, tool titles, MCP failure text, dashboard rows, and task
plans forwarded to the editor are untrusted. Every string crosses the transport
boundary through `platform.redact_via_context`, whose standalone policy performs
the exfiltration and credential passes and whose composed policy adds companion
patterns fail-closed.

## Turn Delegation and stop reasons

```python
PromptHandler = Callable[[PromptRequest, SessionSink], Awaitable[str]]
```

The handler returns a stop reason and drives the editor through `SessionSink`
(`send_text`, `send_thought`, `send_tool_call`, `send_tool_call_update`,
`request_permission`, `send_options`, and the `cancelled` flag). Keeping turn
execution behind this callable is what stops gateway internals leaking into the
protocol layer.

Only **valid ACP stop reasons** (`ACP_VALID_STOP_REASONS`: `end_turn`,
`max_tokens`, `max_turn_requests`, `refusal`, `cancelled`) reach the editor:

- A handler **exception** is a JSON-RPC `-32603 Internal error`, not an
  out-of-schema `stopReason: "error"`. The editor keeps the session usable and
  can prompt again.
- A handler that returns a non-ACP sentinel — the HTTP backend returns the bare
  `"error"` when the gateway is unreachable, and Kiro Crew has internal
  `"error: tool stall"` / `"stale_recover"` sentinels — is likewise mapped to
  `-32603`, so an editor never receives an invalid stop reason.
- A cancellation observed for the turn resolves to `cancelled`.

## Gateway Bridge (`gateway.py`) — `--standalone`

`make_prompt_handler(services, *, agent=None)` returns the `PromptHandler` that
runs an editor turn in-process. It integrates at the **`LLMProvider` seam**
(`SessionManager.get_or_create(key) → provider.stream(message)`), not via
`dashboard.chat_runner._run_chat` (~1500 lines of transport-specific concerns
assuming a dashboard slot). `services` is narrowed to a two-attribute
`GatewayServices` Protocol so the handler is unit-testable with a stub.

Invariants: the per-session semaphore is released in a `finally`; session keys
are namespaced `acp:<sessionId>`; hooks run before the editor
(`EVENT_PERMISSION_REQUEST` → `hooks.on_tool_call` first, `TOOL_DENY` reports a
`failed` tool-call update and never asks the editor); all model-originated text
is redacted; a `HOOK_REPLY` short-circuits the model. `diff_content` rebuilds an
ACP `{"type":"diff", path, oldText, newText}` block so the editor renders an
inline diff with accept/reject.

### Tool-call locations (editor follow-along)

An editor implements "follow the agent" by watching the `locations` field on
each `session/update` of kind `tool_call` / `tool_call_update` (see
`ToolCallLocation` in the vendored `acp-v1` schema): the agent attaches
`[{path, line?}, …]` and Zed jumps its cursor to the target. kiro-cli does not
forward a native location on its tool events, so `acp_server.locations.
extract_tool_locations(tool_name, raw_params)` derives it from the tool's raw
params — kiro-cli's file tools (`fs_read`, `str_replace`, `write`, …) use
`path`; Anthropic-style tools (`Edit`, `Read`) use `file_path`. Only absolute
paths reach the wire (schema requirement); shell-tool names always yield no
location so a `cat /tmp/x` doesn't drag the editor away from the file the agent
is actually editing. Before serialization, canonical paths pass through the
companion-aware path redactor; when redaction would alter a path, the location
or structured diff is omitted rather than leaking the original or navigating to
a synthetic redacted path. Production callers run extraction through
`asyncio.to_thread`, so path canonicalization and best-effort edit-line
derivation never block the ACP/dashboard event loop. Line enrichment screens the
canonical target with `is_sensitive_path` before any metadata or content access,
then reads only a bounded regular-file prefix through `hooks.safe_read_prefix`;
a refused, non-regular, or oversized target remains a path-only location and no
file content reaches the editor. The standalone
gateway calls the extractor at the two
tool-emit sites (`EVENT_TOOL_CALL` and `EVENT_PERMISSION_REQUEST`) and passes
the result to `sink.send_tool_call{,_update}`; on the daemon-backed path the
dashboard runner stores derived locations under `msg.meta.locations` at each
`slot.append("tool", …)` site, `_build_stream_chunk` promotes them to a
top-level `locations` field on the SSE `type: "tool"` frame, and
`HttpGatewayBackend._translate` forwards them through `send_tool_call` after
`_sanitize_locations` drops any malformed entries. Provider tool-call ids are
correlated with later refinements by `(session, tool id)`, because providers
promise uniqueness only within a session; each turn completion discards that
session's correlation entries.

## Daemon Backend (`http_backend.py`)

`HttpGatewayBackend` maps ACP sessions onto dashboard chat slots via the gateway
HTTP API: `session/new`→create a slot, `session/load`→activate + replay history,
`session/list`→enumerate slots (project-scoped), `session/resume`→activate,
`session/cancel`→soft stop, and `prompt`→drive `/api/chat` SSE, translating
chunks/thoughts/tools/permissions/options onto the editor.

Project filtering compares **canonical** filesystem paths (`realpath` + platform
case normalization) via `_project_paths_match`, so an editor workspace opened
through a logical or symlinked path (e.g. `/home/user/project`) matches a slot
persisted with its physical path (e.g. `/local/home/user/project`).
Canonicalization is comparison-only: the slot's original `project` spelling is
returned as the `cwd`.

For an ACP session that is open in an editor, the authenticated dashboard
WebSocket subscription also carries a sanitized `acp_message` event for finalized
user and assistant rows in that registered slot. The event contains only slot,
role, redacted content, and durable `meta.mid`; the adapter maps it to standard
user/agent message updates and keeps a bounded message-id cache across reconnects.
An internal ACP turn stamps its own slot id onto its durable rows, so the adapter
records but does not echo that request or its streamed reply back to the editor.
The browser-facing `chat_message` payload is never reused because it can carry
metadata that is not safe for the editor subscription.

An ACP-created slot (`acp-*`) whose persisted project no longer matches the
requested CWD is a relocation fallback only when no normally project-scoped
session matches. Its list descriptor is projected onto the requested CWD,
allowing the editor to select it; `session/load` then persists that CWD before
replaying history. Ordinary dashboard slots remain strictly project-scoped, so
moving one editor workspace does not expose unrelated dashboard conversations.

## CLI Entrypoint (`cli_acp.py`)

`kirocrew acp` is what an editor spawns. Flags: `--agent`, `--verbose`,
`--gateway-url`, `--standalone`, `--no-jail`. A non-loopback `--gateway-url`
must use `https` and requires a presigned dashboard token in
`KIROCREW_GATEWAY_TOKEN`; the adapter removes that variable from its environment
before loading configuration or starting any client-supplied MCP process. It presents
the raw token once to the dashboard root, where the ordinary five-minute link-expiry
check exchanges it for a distinct HttpOnly session cookie, then drops the raw bearer.
All API and WebSocket traffic uses that bounded cookie; an unredeemed expired link
cannot become a session credential.

```
editor --spawn--> kirocrew acp --stdio--> AgentTransport -> AcpAgentServer
                                              |
                          default: HttpGatewayBackend -> gateway /api/chat
                          --standalone: make_prompt_handler -> in-process SessionManager
```

### Invariants

**stdout is the protocol.** Nothing may write to stdout but JSON-RPC frames — a
stray `print` corrupts the stream and the editor drops the session. Logging is
pinned to stderr with `basicConfig(..., force=True)`. Guarded by
`test_logging_goes_to_stderr_not_stdout`.

**`acp` jailing is mode-dependent.** `acp` is in `cli._JAILED_COMMANDS`, but the
default gateway-proxy mode is **exempted** from the jail (like `gateway`): it
only makes loopback HTTP calls to the already-isolated gateway, and the jail's
private netns would sever that loopback. Only `acp --standalone`, which drives
kiro-cli locally, is jailed. Guarded by `test_acp_is_jailed`.

**Sessions/backends are closed on exit.** When the editor closes the pipe,
`serve()` returns; the standalone path runs `sessions.close_all()` under a 10s
timeout so kiro-cli children are not orphaned, and the gateway path closes the
HTTP backend. The Playwright shim is neutralised at startup on the standalone
path for the same reason `cli_chat` does it.

### Machinery construction

`_build_services` (standalone) mirrors the CLI-side construction in `cli_server`:
`MemoryStore().init()`, `SkillsLoader()`, `LessonStore()`,
`HookManager(HooksConfig.from_dict(cfg.hooks))` into a `ContextBuilder`, plus
`SessionManager(cfg, provider_factory=...)`. Memory, lessons, and skills read
from `KIROCREW_HOME` on disk, so an editor session sees the same accumulated
state as the dashboard and Slack.

## Conformance testing

The protocol contract is covered in-process by `test/test_acp_server_protocol.py`
(strict framing errors, version negotiation, parameter validation, capability
discipline, stop-reason conformance, permission gating, MCP-hosting failure
surfacing), `test/`
`test_acp_server_mcp_config.py` (MCP parse/reject),
`test_acp_server_mcp_supervisor.py` (stdio MCP spawn/initialize/ownership/
teardown lifecycle against real fixture servers), `test_acp_server_gateway.py`,
and `test_acp_server_http_backend.py` (daemon-backed lifecycle — including
`configure_session_mcp` hosting + close-time reaping — against a live aiohttp
gateway stub).

### Closed-box conformance gate

`test/test_acp_conformance_closed_box.py` is the release gate. It treats `kirocrew
acp` as an **external binary**: it spawns the real entrypoint as a subprocess and
speaks newline-delimited JSON-RPC 2.0 over its stdio, exactly as an ACP editor
does. It imports **no** server internals (`AcpAgentServer` / `AgentTransport` are
never imported); the only coupling is the pinned wire surface in
`test/acp_bb_schema.py`, through which the harness validates **every** emitted
frame automatically (`AcpEditor.assert_conformant`).

Run it:

```
python -m pytest test/test_acp_conformance_closed_box.py \
    -o addopts="" -p no:cacheprovider -q
```

(`-o addopts=""` drops the repo's coverage/xdist defaults for a fast, focused
run; the file also works under the default `-n auto` because every test is pinned
to one `xdist_group`.)

**Harness (`test/acp_bb_*.py`):**

- `acp_bb_gateway.FakeGateway` — a deterministic, offline, 127.0.0.1-only threaded
  HTTP server that stubs the daemon at the exact seam `HttpGatewayBackend` calls
  (`/api/chat/slots[/*]`, `/api/chat` SSE). This is the plan's "isolated test
  gateway" without the 5–15s real-gateway startup (that path stays in the
  `KIROCREW_E2E`-gated `test_e2e_smoke.py`). SSE replies are keyed on prompt
  sentinels (`[[TOOL]]`, `[[PERMISSION]]`, `[[THINK]]`, `[[OPTIONS]]`, `[[SLOW]]`,
  `[[GWERROR]]`). Every adapter call is recorded for assertions.
- `acp_bb_editor.AcpEditor` — the closed-box client (subprocess spawn, stdio
  framing, per-frame schema validation, permission auto/manual answering, bounded
  waits, EOF/killpg cleanup).
- `acp_bb_schema` — the ACP v1 validator (see "Dependency decision" below).

**Dependency decision.** The closed-box harness validates frames against the
vendored ACP v1 schema snapshot in `test/conformance/vendor/acp-v1/`.
`acp_bb_schema` imports only `acp_v1_vendor`, which exposes that snapshot; it
imports nothing from `kiro_crew`, so the protocol oracle cannot agree with the
server merely because they share Python constants. The snapshot metadata records
the pinned schema revision, and the harness remains intentionally independent of
an offline Draft 2020-12 validator.

`agent-client-protocol==0.12.1` is a development dependency for the separate
official-SDK interoperability smoke test. It requires the SDK's unstable
elicitation opt-in but does not replace the vendored schema oracle: the SDK is a
client implementation, whereas the vendored schema validates every emitted frame.

**Coverage matrix (all green: 29 passed, 2 skipped, deterministic across repeated
runs):**

| ACP v1 requirement | Test |
|---|---|
| initialize / version negotiation / capabilities | `TestInitialize` (incl. never echoing an unsupported offered version) |
| prompt streaming + valid `stopReason` | `TestPromptTurn::test_new_prompt_streams_reply_and_ends_turn` |
| thought chunks | `TestPromptTurn::test_thinking_chunk_is_thought_update` |
| resource-link / content-block fidelity | `TestPromptTurn::test_resource_link_block_preserved_across_boundary` |
| malformed JSON → `-32700` | `TestJsonRpcErrors::test_malformed_json_is_parse_error` |
| non-object / bad `jsonrpc` → `-32600` | `TestJsonRpcErrors::test_non_object_frame_*`, `test_bad_jsonrpc_version_*` |
| invalid params → `-32602` | `TestJsonRpcErrors::test_missing_absolute_cwd_*`, `test_bad_prompt_shape_*` |
| unknown session / unadvertised method → `-32601` | `TestJsonRpcErrors::test_prompt_unknown_session_*`, `test_unadvertised_methods_*` |
| permission allow / deny bridged to gateway | `TestPermission` |
| cancel while a daemon tool is running | `TestCancellation::test_cancel_while_tool_running` |
| cancel while a permission request is pending | `TestCancellation::test_cancel_while_permission_pending` |
| gateway disconnect/error → `-32603` (no out-of-schema stopReason) | `TestTransportLifecycle::test_gateway_error_maps_to_internal_error` |
| adapter EOF + clean shutdown / child reaping | `TestTransportLifecycle::test_adapter_eof_clean_shutdown` |
| two simultaneous clients / session isolation | `TestConcurrencyAndCapabilities::test_two_clients_*` |
| capability discipline (advertised ⇔ works E2E) | `TestConcurrencyAndCapabilities::test_advertised_optional_methods_all_work_end_to_end` |
| large history replay ordering | `TestHistoryAndListing::test_session_load_replays_history_in_order` |
| `session/list` bounded first release (no cursor paging) | `TestHistoryAndListing::test_session_list_sorted_and_bounded_first_release` |
| stdio MCP: real spawn + `initialize` preflight + registration | `TestStdioMcp::test_session_new_with_stdio_mcp_preflights_and_registers` |
| stdio MCP: unsupported transport → `-32602`; bad command → `-32603`; duplicate name secret-safe | `TestStdioMcp::*` |
| reply-options extension (namespaced `_meta.kirocrew.options`) | `TestReplyOptions` |

**Known exclusions / blockers (honest scope — not certified beyond what is
tested):**

- **Official Python & TypeScript ACP SDK-driven smokes** are written but
  **skipped-with-reason** (`test_official_python_sdk_smoke`,
  `test_official_typescript_sdk_smoke`): the SDKs are not installable/resolvable
  offline here. When vendored, they slot onto the same `acp_bb_schema` seam.
- **Model-side `tools/call` + streamed tool result** through a client-supplied MCP
  server is not exercised here: the adapter's `session/new` preflight performs a
  real spawn + `initialize` only (it never hosts the child — the model-side
  provider binary does), so this suite proves spawn+initialize+registration and
  defers the full model-driven `tools/list`/`tools/call` to the real-gateway
  `KIROCREW_E2E` path (`test_e2e_smoke.py`, `test_acp_session_mcp_flow.py`).
- **Manual Zed / live-editor smoke** remains a manual step recorded in the CR
  description, per the plan.
- The `unshare … EPERM` line printed during the run is the OS-sandbox userns probe
  declining nested isolation inside an already-sandboxed dev host; the MCP
  preflight still spawns within the existing boundary (the positive MCP test
  skips only if the returned error names sandbox-infra unavailability).
