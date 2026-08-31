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
local `X-Internal-Secret` only when the configured gateway URL is loopback. A
non-loopback gateway requires an explicit presigned token, so an operator-supplied
URL can never receive the host's internal IPC credential.

`--standalone` is an offline diagnostic fallback that runs turns through an
in-process `SessionManager` (`gateway.make_prompt_handler`); those turns are not
visible in the dashboard. The ACP-specific code never reads or mutates session
history files directly — session state is owned by the backend.

## Method Surface

Client→agent requests answered:

| Method | Behaviour |
|--------|-----------|
| `initialize` | Negotiates integer ACP **v1**; advertises capabilities derived from the backend |
| `session/new` | Validates `cwd` (absolute) + `mcpServers`; mints/creates a session; replies `{sessionId}` |
| `session/prompt` | Validates `sessionId` + `prompt` blocks; delegates to the `PromptHandler`; replies `{stopReason}` |
| `session/load` | **Backend-gated.** Activates a session and replays its history as `session/update`s |
| `session/list` | **Backend-gated.** Lists sessions, project-scoped by `cwd` |
| `session/resume` | **Backend-gated.** Resumes a session without replaying history |
| `session/set_mode` / `set_model` / `set_config_option` | `-32601 Method not found` — not implemented, never no-oped |
| anything else | `-32601 Method not found` |

`session/cancel` is a notification; it sets the session's cancel event and, when
a backend is present, stops the backing turn.

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

### Available slash commands

After `session/new`, `session/load`, or `session/resume` succeeds, a backend with
`get_available_commands` emits the standard `available_commands_update` session
notification. The default HTTP backend reads the gateway's provider-aware
`GET /api/slash-commands` catalog, removes the display-only leading `/`, filters
malformed or duplicate entries, and advertises only commands the dashboard can
execute. Discovery is best-effort: an unavailable catalog never fails session
creation or recovery, and standalone mode advertises nothing until it has an
equivalent command-discovery backend.

## JSON-RPC framing strictness (`transport.py`)

The transport answers malformed input instead of silently discarding it:

| Condition | Response |
|-----------|----------|
| Unparseable bytes | `-32700 Parse error`, `id: null` |
| Valid JSON but not an object (array/scalar) | `-32600 Invalid Request`, `id: null` |
| Missing/invalid `jsonrpc` (`!= "2.0"`) | `-32600 Invalid Request`, id echoed if usable |
| Frame with neither `method` nor a usable id | `-32600 Invalid Request`, `id: null` |
| `method` present but not a string | `-32600 Invalid Request` |
| Request `id` present but not a string/number | `-32600 Invalid Request`, `id: null` |

An id is "usable" only if it is a string or a non-bool number (`_safe_id`); an
error whose triggering frame carried no usable id is answered with `id: null`
per the spec. None of these are fatal — one bad line from a noisy peer must not
end the session, and a following valid frame is still processed. Production
stdio readers accept frames up to 10 MiB so ordinary editor media does not hit
asyncio's 64 KiB default. A larger line is drained exactly to its newline,
answered with bounded `-32600 Frame too large`, and the next frame remains usable;
a line that exceeds the bounded drain budget closes the pipe.

A genuine **response** to one of our outbound requests (id set, no method) is
still routed to the pending-future resolver and dropped if unknown; it is never
mistaken for an invalid request.

## Parameter validation

Requests are validated structurally before any work; a bad shape earns
`-32602 Invalid params`:

- `cwd` on `session/new` / `session/load` / `session/resume` must be a
  non-empty **absolute** path.
- `sessionId` on `session/prompt` / `session/load` / `session/resume` must be a
  non-empty string. A well-formed request for a *nonexistent* session is a
  distinct case answered `-32601` (unknown session).
- `prompt` on `session/prompt` must be an array of content blocks; each block
  must be an object with a non-empty string `type`. An empty array is a valid
  (contentless) turn.
- `mcpServers` is validated as below.

`PromptRequest` carries the original content blocks (`content_blocks`) alongside
the flattened `text`, so a backend that can act on structured content (e.g.
resource links) has it at the boundary rather than only the lossy text
projection. `prompt_blocks_to_text` performs the documented, preserve-what-we-can
flattening for the text-only chat core (text verbatim; `resource_link`→uri;
`resource`→embedded text or uri; image/audio→a placeholder when a handle exists).

## Client-supplied MCP servers (`mcp_config.py`)

An ACP client passes `mcpServers` per session. `parse_mcp_servers` validates the
array structurally and returns typed, session-scoped `StdioMcpServer` configs:

- Baseline **stdio** transport only — `command` + `args` + `env` (the ACP
  array-of-`{name,value}` shape or a plain object). The parsed config is stored
  on the session and handed to the backend through an optional
  `configure_session_mcp` hook.
- **Unsupported transports (HTTP/SSE) are rejected with `-32602`**, not silently
  ignored: a client that asked for a server it will not get must be told, not
  left believing a tool is available.
- Malformed entries, a missing `command`, or a duplicate `name` are `-32602`.
  Error messages name the offending index/field and never echo a secret value.

**Process supervision (`mcp_supervisor.py`).** The daemon-backed
`http_backend.py` hosts each editor-supplied stdio server once under the ACP
adapter's ownership:

1. `SessionMcpSupervisor.host` resolves the command, applies Kiro Crew's sandbox,
   credential-environment scrub, process-group isolation, and resource limits,
   then retains the child for that ACP session.
2. Each child is exposed through a token-guarded Unix socket. Every socket and
token lives under one supervisor-owned root that is hidden from every untrusted
child sandbox, so a child cannot discover another session's proxy capability.
The gateway slot receives only a trusted `mcp_proxy.py` stdio spec; the original
command, arguments, and environment never reach kiro-cli.
3. The MCP `initialize` exchange flows end-to-end through the proxy, so the real
   child is initialized exactly once and startup errors surface to the editor.

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
  session creation/load.
- **Ownership and isolation** — configuration is scoped to one dashboard slot.
  `session/new`, `session/load`, and `session/resume` replace that slot's set;
  an empty registration clears it.
- **Failure surfacing** — spawn or initialization failure reaps all partial
  children and returns `-32603` with a secret-safe message. Unsupported or
  malformed transports return `-32602` before any spawn.
- **Teardown** — reconfiguration, cancellation, adapter EOF, and shutdown close
  proxy sockets and reap complete process groups with SIGTERM→SIGKILL on POSIX.

There is no dedicated `session/close` in ACP v1. Mid-life teardown occurs on
reconfiguration; adapter shutdown reaps every remaining hosted child. The
black-box conformance gate drives this through the public stdio adapter against
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

Diff bodies, file paths, and tool titles forwarded to the editor originate from
kiro-cli and are untrusted by `AUTOSDE.yaml` rules. They pass
`redact_credentials()` / `redact_exfiltration_urls()` before crossing the
transport boundary.

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

## CLI Entrypoint (`cli_acp.py`)

`kirocrew acp` is what an editor spawns. Flags: `--agent`, `--verbose`,
`--gateway-url`, `--standalone`, `--no-jail`.

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

### Black-box conformance gate

`test/test_acp_conformance_blackbox.py` is the release gate. It treats `kirocrew
acp` as an **external binary**: it spawns the real entrypoint as a subprocess and
speaks newline-delimited JSON-RPC 2.0 over its stdio, exactly as an ACP editor
does. It imports **no** server internals (`AcpAgentServer` / `AgentTransport` are
never imported); the only coupling is the pinned wire surface in
`test/acp_bb_schema.py`, through which the harness validates **every** emitted
frame automatically (`AcpEditor.assert_conformant`).

Run it:

```
python -m pytest test/test_acp_conformance_blackbox.py \
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
- `acp_bb_editor.AcpEditor` — the black-box client (subprocess spawn, stdio
  framing, per-frame schema validation, permission auto/manual answering, bounded
  waits, EOF/killpg cleanup).
- `acp_bb_schema` — the ACP v1 validator (see "Dependency decision" below).

**Dependency decision.** No official ACP v1 **Python SDK** or **JSON Schema** is
installable in this offline test environment (`acp` / `agent_client_protocol` are
absent; `jsonschema` is absent — the vendored `@agentclientprotocol/claude-agent-acp`
npm package is the *client*-side adapter, not an agent-role schema/SDK). Per the
plan's "record the exact ACP schema and SDK revisions", the authoritative pinned
surface this repo already ships is **`kiro_crew.acp.types`** (protocol version,
methods, error codes, stop reasons, capability keys, session-update kinds,
permission option ids) negotiated to **integer v1** by
`kiro_crew.acp_server.server.SUPPORTED_PROTOCOL_VERSION`. `acp_bb_schema` is built
from that surface. It is the single seam to swap for official-schema validation
once an SDK is vendored; the harness already routes every frame through it.

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
