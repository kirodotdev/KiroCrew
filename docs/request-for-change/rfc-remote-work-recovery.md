---
title: Remote work recovery -- what an App's MCP server must provide so Kiro Crew can find its work after a disconnect
status: draft
author: kseam
created: 2026-10-16
last-audited: 2026-10-18
audited-at: 310ad48b5f
doc-pr: 21975
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Remote work recovery

- Status: draft. Nothing is built. Code facts are measured at `310ad48b5f`
  unless a sentence names the open Session Environments stack, which is read at
  `aff2e76354` on `feat/session-environments-coder`.
- Author: kseam
- Raised in review of [#19833](https://github.com/kirodotdev/KiroCrew/pull/19833)
  (Session Environments, Coder) and the PRs stacked on it:
  [#21256](https://github.com/kirodotdev/KiroCrew/pull/21256),
  [#21257](https://github.com/kirodotdev/KiroCrew/pull/21257),
  [#21258](https://github.com/kirodotdev/KiroCrew/pull/21258),
  [#21259](https://github.com/kirodotdev/KiroCrew/pull/21259),
  [#21260](https://github.com/kirodotdev/KiroCrew/pull/21260).
  That stack is the context that raised the question, not a dependency: the
  contract holds on main as it stands, and the only sentences that read the
  stack (§2.2's last row, §4.11's Session Environment and Kiro Cloud points)
  say so and stay true whether or not it merges.
- Related: [rfc-mcp-resource-subscriptions.md](rfc-mcp-resource-subscriptions.md)
  (a server-side holder claimed as the session, which this RFC reuses),
  [rfc-app-sdk-durable-jobs-and-view-state.md](rfc-app-sdk-durable-jobs-and-view-state.md)
  (`JobSDK`, the in-gateway half of the same problem),
  [rfc-app-backend-adoption-provenance.md](rfc-app-backend-adoption-provenance.md)
  (whether an App backend outlives the gateway),
  [mcp-gateway-daemon-lifecycle.md](../system-specs/modules/mcp-gateway-daemon-lifecycle.md),
  [mcp-shareability.md](../system-specs/modules/mcp-shareability.md),
  [mcp-apps.md](../system-specs/modules/mcp-apps.md).

## 1. Summary

When the gateway stops, the agent stops with it. Work the agent started through
an App's MCP server may not: a deploy, a CI run, a batch job or a review an App
hands to a remote system keeps going. Reconnecting the conversation does not
bring that work back. Nothing in Kiro Crew records that the call started a job,
so after a restart Kiro Crew cannot say whether the job finished, failed or is
waiting for an answer, and the resumed agent may simply start it again.

This RFC defines the contract between Kiro Crew and any MCP server, local or
remote, App-provided or user-configured, for work that must survive a
disconnect. It builds on MCP Tasks: the App's server returns a durable task id
instead of blocking, keeps the task's state where the work lives, and answers
status and result queries from a fresh process. Kiro Crew's half is to act as the
task requestor in the MCP gateway, persist each task id against the session that
started it, poll after a reconnect, turn `input_required` into a card the user
answers, show results when the user returns, and never re-send a call whose
outcome it did not see unless the server has promised to deduplicate it. A
server that does not offer Tasks gets no recovery, and Kiro Crew says so rather
than guessing.

## 2. Motivation

### 2.1 The question

The review of #19833 asked: *what must an App provide so Kiro Crew can find out
whether its work finished, failed, or needs input, and resume without starting it
twice?* The answer has to hold for every place a session runs: on the gateway
host, in a Session Environment, and through KAS. It has to hold for local stdio
servers too, because a local server can start remote work as easily as a hosted
one.

### 2.2 What survives a disconnect today

| What | Survives a gateway restart? | Evidence |
|---|---|---|
| The conversation | Yes | A slot's previous engine conversation is resumed by its session key (the resume pointer; see [app-kit-platform.md](../system-specs/modules/app-kit-platform.md), uninstall step 6). |
| The turn in flight | No, but it is shown | `close_all` in `src/kiro_crew/session_lifecycle_parts/fleet.py` drains active turns for a bounded time, then closes every runtime. A turn still running is cut off. The transcript keeps it, and `build_interrupted_turn_preamble` (`src/kiro_crew/context_assembly/replay.py`) restores its request when the user presses Resume, because `session/load` brings the conversation back without it. |
| A tool call in flight on a routed (stubbed) server | No | The MCP broker daemon exits when its owning gateway exits (`_owner_liveness_sweeper` in `src/kiro_crew/mcp_gateway/daemon/sweepers.py`). Before then, a stub that disconnects has every in-flight request it owned cancelled with `notifications/cancelled` (`cancel_in_flight_for_stub` in `src/kiro_crew/mcp_gateway/backend_stubs.py`, called from the connection teardown in `daemon/connection.py` and from the `abort` frame in `daemon/control.py`). The request table, `Backend._pending_requests`, is in memory. |
| A tool call in flight on an unrouted server | Not by any Kiro Crew mechanism | `mcp_gateway.stub_servers` is empty by default (`src/kiro_crew/config/integration_sections.py`), so a server is launched by the engine itself. Kiro Crew holds no handle on it, and an HTTP server entry is never stubbed ([mcp-apps.md](../system-specs/modules/mcp-apps.md)). What the engine does with that call when it exits is the engine's behaviour and is not verified here. |
| A `JobSDK` run in the gateway | No, and it says so | `JobSDK.reconcile` (`src/kiro_crew/apps/job_sdk.py`) marks a run left by an earlier gateway process `interrupted`, a terminal state, recording how far it got and why it cannot resume. Work running in an App's own backend process is invisible to it. |
| An App backend process | No | `_reap_stale_app_backends` (`src/kiro_crew/apps/backend_runtime/stale_reap.py`) terminates the previous generation's backends at startup. |
| Work an App handed to a remote system | Usually yes | Nothing in Kiro Crew stops it, and nothing in Kiro Crew records it. |
| A Session Environment's workspace (#19833, not on main) | Its files, yes; its processes, no by default | `session_environments.stop_on_shutdown` (default true) stops the Coder workspace at a graceful shutdown. A worker with no gateway exits after `wire.ORPHAN_TIMEOUT_SECONDS` (600 s), and `RemoteWorkerLaunch.reap_stale_workers` stops a binding's workers another gateway process started. A `run_command` in flight goes with its worker. |

Two protocol facts frame the rest:

- No code under `src/` sends or reads `tasks/get`, `tasks/result`, `tasks/cancel`,
  `tasks/list` or `notifications/tasks/status`. Kiro Crew's own MCP servers answer
  `initialize` with `protocolVersion` `2024-11-05` (`src/kiro_crew/mcp_shared_dispatch.py`),
  and discovery asks for `2025-06-18` (`MCP_CLIENT_PROTOCOL_VERSION`, defined in
  `src/kiro_crew/mcp_discovery_probe_state.py`). The broker's eager handshake,
  `send_initialize` in `src/kiro_crew/mcp_gateway/backend_handshake.py`, offers
  the same constant and retries once with the newest older version the server
  lists if it refuses (`downgrade_protocol_version`), but production spawns take
  the lazy path: for a
  routed server the gateway forwards the first stub's own `initialize`, so the
  version a session actually negotiates is whatever the engine asked for.
- The gateway would mishandle a task-capable server today. A
  `notifications/tasks/status` carries no `progressToken` and, per the spec,
  should carry no related-task `_meta`, so `_notification_owner` cannot
  attribute it, it is not in `_GLOBAL_BROADCAST_NOTIFICATIONS`, and
  `_route_backend_message` (`src/kiro_crew/mcp_gateway/backend_routing.py`)
  drops it, even with one stub attached, and records
  `HAZARD_UNATTRIBUTABLE_NOTIFICATION`, which counts against the server once the
  backend is shared. The same happens to a task's `notifications/progress`: the
  spec keeps the call's `progressToken` valid for the task's whole life, but the
  gateway removes the call from `_pending_requests` as soon as the server answers
  it with `CreateTaskResult`, so later progress has no owner. A `tasks/get` from
  one stub is forwarded like any request, so on a pooled backend whose server
  does not bind tasks to the caller, one session could read another's task by
  id.

### 2.3 What is missing

1. A protocol-level record that a call became long-running work, kept somewhere
   that outlives the gateway, the broker daemon and the server process.
2. A way to ask the server about that work from a fresh process, as the session
   that started it.
3. A rule for what happens to a call whose outcome Kiro Crew never saw, so a
   resumed agent does not run it a second time.
4. A screen that tells the user, on return, which work finished, which failed,
   which needs them, and which Kiro Crew cannot account for.

## 3. Goals and non-goals

Goals:

- An MCP server that follows §4.1 has its long-running calls recovered after an
  engine restart, a broker restart, a gateway restart and a gateway crash: Kiro
  Crew learns the final status, shows the result, and asks the user any question
  the work is blocked on.
- No call is sent twice by Kiro Crew unless the server has declared it
  deduplicates by key. A repeat the model asks for is held for the user.
- A server that does not follow §4.1 is named as unrecoverable, per call where
  Kiro Crew can see the call and per turn where it cannot.
- One contract for every target: a session on the gateway host, a Session
  Environment, and KAS. It does not depend on which engine runs the session.
- Nothing changes for a server that does not declare Tasks, until Phase 3's
  notices, which only add text.

Non-goals:

- Keeping the agent's own turn alive across a restart. The turn ends; this RFC
  recovers the work the turn handed off.
- Recovering work on a server Kiro Crew does not route. The first version covers
  stdio servers behind a gateway stub (§4.9).
- Recovering `JobSDK` runs or App backend processes. Those are the subjects of
  the two related RFCs; an App that wants its backend's work recovered exposes
  it through its MCP server as tasks.
- Running unattended turns on a result. A recovered result is shown, not acted
  on, in the first version (§9 Q4).
- Designing for MCP's stateless `2026-07-28` core. Tasks are covered in both
  revisions (§4.1.1); the removal of `initialize` is a broker-wide change that
  belongs in its own RFC.

## 4. Design

Three parties take part. The **engine** (kiro-cli, KAS, or another harness) is
the MCP client the model talks through. The **gateway** is Kiro Crew's MCP broker
and its stubs. The **server** is the App's MCP server, which receives task-
augmented requests and runs the work. The gateway, not the engine, is the task
requestor: it upgrades the engine's ordinary `tools/call` to a task, waits for the
result while the engine waits for the call, and owns the task once the engine is
gone. The engine needs no task support, which is why the contract holds for every
harness.

### 4.1 The App contract

An App, or any MCP server, whose tools start work that can outlive a gateway
provides the following. Requirements 1 to 6 are what recovery needs. 7 is what
lets Kiro Crew retry safely. 8 is optional.

1. **Declare Tasks.** Under the `2025-11-25` revision: the server capability
   `tasks.requests.tools.call`, `tasks.cancel`, and `execution.taskSupport` of
   `optional` or `required` on each tool that starts such work. Under the
   `2026-07-28` revision: the `io.modelcontextprotocol/tasks` extension (§4.1.1).
   A tool that finishes quickly declares nothing and is called as today.
2. **Durable task ids.** Return `CreateTaskResult` only once the task exists
   durably, so that `tasks/get` for the returned id resolves. The `2026-07-28`
   extension makes this a MUST; Kiro Crew requires it under `2025-11-25` as well.
   "Durably" means where the work lives (the remote job system, or the App's own
   store), never only in the memory of the server process: after a restart Kiro
   Crew asks a newly started process of the same server about a task the previous
   process created.
3. **Bind tasks to the caller, not the connection.** A server that declares
   `kirocrew.caller-identity` (`CALLER_CAPABILITY_KEY` in
   `src/kiro_crew/mcp_caller.py`) binds each task to the caller block's
   `principal_id` and `session_key`, and refuses `tasks/get`, `tasks/cancel` and
   result retrieval for a task outside that binding. A third-party server never
   receives the caller block's `session_token` (`mcp_caller.py` withholds it),
   and the binding must survive a restart, so `principal_id` and `session_key`
   are the whole key. A server without caller identity generates unguessable task ids and
   does not offer `tasks/list`: the `2025-11-25` spec makes the first a MUST and
   the second a SHOULD NOT for a server that cannot bind tasks.
4. **Report status in the spec's five states.** `working`, `input_required`,
   `completed`, `failed`, `cancelled`, with the spec's transitions, and a
   `statusMessage` a person can read. Kiro Crew shows that message as untrusted
   text. The two forms disagree on one meaning: under `2025-11-25` a tool result
   with `isError: true` makes the task `failed`, while the extension keeps
   `failed` for a JSON-RPC error and reports a tool error as `completed` with
   that result. Kiro Crew reads the result, not the status alone, to tell
   "finished" from "failed" (§4.8).
5. **Keep results for the requested TTL.** Kiro Crew asks for seven days
   (`mcp_gateway.tasks.requested_ttl_secs`, proposed), sent in milliseconds as
   the spec's `ttl` (`2025-11-25`) and read back as `ttlMs` (extension). The
   server may shorten it and reports the TTL it chose. A result is retrievable,
   from a fresh process, for that long.
6. **Ask for input through the task.** When the work needs a person, the task
   moves to `input_required` and the request is an elicitation: under
   `2025-11-25` an `elicitation/create` request from the server carrying the
   related-task `_meta`, which the requestor receives after calling
   `tasks/result`; under the extension an `elicitation/create` entry in the
   `inputRequests` of `tasks/get`. Form mode with the spec's flat primitive
   schema, or URL mode. Kiro Crew renders those (§4.6). Any other request type
   while `input_required` is shown as "needs input in the App" and is not
   answered.
7. **Deduplicate by key.** A server that declares `kirocrew.idempotency-key`
   under `capabilities.experimental` (proposed name, §9 Q2) treats
   `_meta["kirocrew.idempotency-key"]` on a `tools/call` as the identity of one
   logical call: a second call with the same key, from the same caller, within
   the task's TTL, returns the existing task and starts nothing. MCP has no
   standard for this; `idempotentHint` in `ToolAnnotations` is a hint about
   arguments, untrusted by the spec's own wording, and not a key.
8. **Optionally push status.** `notifications/tasks/status` (`2025-11-25`), or
   `notifications/tasks` on a `subscriptions/listen` stream (extension). Kiro
   Crew polls either way: the `2025-11-25` spec says a requestor must not rely
   on the notification, and although the extension lets a client rely on it,
   a poll is the only reading a fresh process can make after a restart.

Cancellation follows the form the task was created under. Under `2025-11-25`,
`tasks/cancel` returns the task, the server must move it to `cancelled` before
answering, and it stays `cancelled`. Under the extension `tasks/cancel` is an
empty acknowledgement and cancellation is cooperative: the task may stay
`working` for a while and may still end `completed` or `failed`, so Kiro Crew
keeps polling until a terminal reading (§4.4).

#### 4.1.1 The two Tasks revisions

Tasks were added, experimental, in the
[`2025-11-25` specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks)
([SEP-1686](https://modelcontextprotocol.io/seps/1686-tasks)). The current
revision, [`2026-07-28`](https://modelcontextprotocol.io/specification/2026-07-28/changelog),
removes them from the core and replaces them with the
[`io.modelcontextprotocol/tasks` extension](https://modelcontextprotocol.io/extensions/tasks/overview)
([SEP-2663](https://modelcontextprotocol.io/seps/2663-tasks-extension)). The two
are not wire-compatible:

| | `2025-11-25` core | `2026-07-28` extension |
|---|---|---|
| Opt-in | `tasks` capability in `initialize`, `task` parameter per request, `execution.taskSupport` per tool | Server advertises the extension in `server/discover`; the client lists it in each request's `_meta` client capabilities; the server decides per request whether to return a task |
| Task reply | `CreateTaskResult` carrying a `task` object | `CreateTaskResult` with `resultType: "task"` |
| Result | `tasks/result`, which blocks until terminal | The `result` or `error` field of `tasks/get` once terminal |
| Input | A server `elicitation/create` with related-task `_meta`, received after calling `tasks/result` and answered as an ordinary response | `inputRequests` in `tasks/get`, keyed per request, answered with `inputResponses` in `tasks/update` |
| `failed` | Includes a tool result with `isError: true` | A JSON-RPC error only; a tool error is `completed` |
| Push status | `notifications/tasks/status`, never to be relied on | `notifications/tasks` on `subscriptions/listen`, may be relied on |
| Listing | `tasks/list`, if declared | Removed |
| Cancel reply | The task, already `cancelled` | An empty ack; the task may still end otherwise |
| TTL and poll fields | `ttl`, `pollInterval` | `ttlMs`, `pollIntervalMs` |
| Durable before reply | Not required | Required |

The extension is not defined under `2025-11-25`, and a server must not advertise
the legacy capabilities under a protocol version that includes the extension,
so the negotiated protocol version picks the form.

Kiro Crew implements the `2025-11-25` form first (Phase 2), because the broker
speaks `initialize`-based MCP today, and the extension form when the broker gains
`2026-07-28` support (Phase 6). The persisted record (§4.3) stores which form a
task uses, so a task is always polled the way it was created. Kiro Crew never
calls `tasks/list`: requirement 3 already forbids it for servers without caller
binding, the extension removes it, and every task Kiro Crew polls is one it
recorded.

### 4.2 How a call flows

For a routed stdio server whose captured `initialize` result declares
`tasks.requests.tools.call`:

1. The gateway injects the client `tasks` capability into the `initialize` it
   forwards, as it already injects the in-flight report and MCP Apps extensions
   (`_inject_inflight_capability`, `_inject_client_extensions`). Whether the
   negotiated `protocolVersion` must also be raised is Phase 0(a).
2. The engine sends an ordinary `tools/call`. If the tool's `execution.taskSupport`
   is `optional` or `required`, the gateway writes a `sent` record (§4.3) and
   only then forwards the call with `params.task` (`ttl` from requirement 5) and,
   when the server declares requirement 7, an idempotency key.
3. The server answers `CreateTaskResult`. The gateway records the task id on the
   same record (`task`) and does not pass `CreateTaskResult` to the engine, which
   did not ask for a task.
4. While the stub is attached, the gateway waits for the result (`tasks/result`,
   polling `tasks/get` beside it at `pollInterval`) and answers the engine's
   original request with exactly what the call would have returned. To the engine
   and the model this is an ordinary, slow tool call. This holds only while the
   engine waits longer than the task runs. Phase 0(b) measures how long kiro-cli
   and KAS wait, and Phase 2 does not start until that verdict is recorded: if
   the engine gives up first, Phase 2 ships the early answer of §9 Q3 and keeps
   the task on its record, and the result is delivered as in §4.8, never by
   holding the call past the engine's wait.
5. The record moves to `delivered` when the engine has the result, and is kept
   until it expires (§4.3).

A tool whose `execution.taskSupport` is absent or `forbidden` is forwarded as
today. A server that does not declare Tasks is forwarded as today.

### 4.3 What Kiro Crew persists

One record per task-backed call, written by the broker daemon in a gateway-owned
store under the data home (`mcp-tasks/`, proposed), owner-only from the moment it
is created, as the MCP Apps spool is (`write_spool` in
`src/kiro_crew/mcp_gateway/apps.py`). File mode alone does not keep an agent
out, because the agent runs as the same user and the data-home root is writable
in-sandbox. The fence is the one the spool has: `mcp-tasks` joins `mcp-apps` in
`_CREW_HIDDEN_LEAVES` (`src/kiro_crew/sandbox_leaves.py`) and in the hidden
directories `src/kiro_crew/sandbox_precreate.py` creates, so the directory is
masked in every agent sandbox. An agent-writable record could name another session's task
and have Kiro Crew poll it as that session.

| Field | Meaning |
|---|---|
| `session_key`, `channel_id` | The caller the call was made as; the reconciler is claimed as this caller |
| `server`, `app` | The server name, and the App that ships it when `_collect_app_mcp_servers` (`src/kiro_crew/agent_materialization/mcp_sources.py`) attributes it to one |
| `tool`, `tool_call_id` | The tool name and the engine's call id, to tie the record to its transcript row |
| `args_digest`, `summary` | A digest of the redacted arguments, for the repeat guard (§4.7), and a short redacted summary for display. The arguments themselves are not stored |
| `idempotency_key` | The key sent, or empty |
| `flavor` | `2025-11-25` or `extension` |
| `task_id`, `created_at`, `ttl` | As the server reported them; `task_id` is empty in `sent` |
| `recorded_at`, `last_polled_at` | Kiro Crew's own clock |
| `status`, `status_message` | The last reading; the message is redacted and bounded |
| `state` | `sent`, `task`, `detached`, `input`, `delivered`, `closed` |

`sent` is written before the call leaves the gateway, so a crash between the
server creating a task and the gateway recording it leaves a record that says so.
Disk writes run off the event loop. A record is removed once it is `delivered` or
`closed` and its TTL has passed, and when its session is deleted. A session holds
at most 50 open records and the store at most 1,000 (proposed); a call that would
exceed either is forwarded without `params.task`, as a normal call, and the
session is told once that further work from that server cannot be recovered.

### 4.4 Stop is not a disconnect

Today any stub teardown cancels its in-flight calls, which is correct for work
that dies with the connection. For a task-backed call the two causes need
different answers:

- **A person's Stop**: the gateway sends `tasks/cancel`. Under `2025-11-25` the
  reply is the cancelled task and the record closes as `cancelled`. Under the
  extension the reply is only an acknowledgement, so the record stays open and
  is polled until a terminal reading, which may be `completed` if the work
  finished first; the session is told which. The daemon learns of a session
  hard-stop today through the `abort`
  frame (`_apply_abort` in `src/kiro_crew/mcp_gateway/daemon/control.py`), which
  says only which runtime's PIDs to cancel. The frame gains a field saying whether
  a person asked for the stop, so a teardown that also sends `abort` is not read
  as one.
- **Every other teardown**, an engine recycle, a gateway shutdown, a crash: the
  gateway sends neither `notifications/cancelled` nor `tasks/cancel` for the
  task-backed call, and marks the record `detached`. The work goes on and the
  record is how Kiro Crew finds it again.

Calls that are not task-backed keep today's behaviour.

### 4.5 Reconnect

A reconciler in the gateway reads the store at startup and whenever a session's
runtime attaches. For each `task` or `detached` record it polls the server as the
session that started the work: through a gateway-internal stub on that server's
backend, as `app_call` (`src/kiro_crew/mcp_gateway/app_call.py`) forwards an MCP
Apps callback today, carrying a caller built from the record's `session_key` and
`channel_id`, as the holder in rfc-mcp-resource-subscriptions §4.1 proposes. It
polls with `tasks/get`, which answers at once, and does not reuse `app_call`'s
20-second round trip for a blocking `tasks/result`: that round trip cancels
whatever is still in flight when it ends. Polls respect `pollInterval`, run with
bounded concurrency, and back off on errors. A backend that cannot be started,
for example because its launch approval changed, leaves the record `detached`
with that reason shown.

| Reading | Kiro Crew does | The record becomes |
|---|---|---|
| `working` | Polls again | unchanged |
| `input_required` | Fetches the request and shows an input card (§4.6) | `input` |
| `completed` or `failed` | Retrieves the result and delivers it to the session (§4.8) | `delivered` |
| `cancelled` | Tells the session | `closed` |
| `-32602`, task not found or expired | Tells the session the App no longer has it | `closed` |
| Server refuses the caller | Tells the session; does not retry as anyone else | `closed` |

A `sent` record with no task id means the call left the gateway and Kiro Crew
never learned whether a task was created. If the server declares requirement 7,
the reconciler re-sends the same call with the same key, which returns the
existing task or creates it once. If not, the record is closed as **unknown** and
the user is told to check the App before running the work again. It is never
re-sent.

### 4.6 Input required

The card is drawn in the session's chat, modelled on the `ask_question` card:
an enum field becomes options, a boolean a yes/no, a string or number a text
field; URL mode shows the target host and opens it only on the user's press, as
the elicitation spec requires. The card names the App and the tool and shows the
server's message as untrusted text.

Only the session and the user that started the task can answer it. The card is
drawn only in the chat of the record's `session_key`, the answer is accepted
only from that session's owner, and it is sent to the server as the record's
caller, the same identity §4.5 polls as. A reply from any other session or user,
or a card replayed into another chat, is refused. The model is not offered the
request either, because an elicitation is the server asking a person. Under
`2025-11-25` the answer is the response to the server's `elicitation/create`
request; under the extension it is an `inputResponses` entry in `tasks/update`,
keyed as the server keyed the request. Extension `inputRequests` are repeated on
every poll until answered, so a card is keyed by task id and request key and
drawn once. A card left unanswered stays until the task's TTL; declining it
sends the spec's `decline`.

When the engine is attached and the call is still live (§4.2 step 4), the same
card is used rather than forwarding the elicitation to the engine, so the
experience does not depend on whether the engine supports elicitation.

### 4.7 Never replay

Three rules keep a call from running twice:

1. Kiro Crew re-sends a call only as §4.5 describes, under a key the server has
   promised to honour. Nothing else in the recovery path sends a `tools/call`,
   and a call whose outcome Kiro Crew did not see is never re-sent to a server
   that has not declared requirement 7. The only other new call for the same
   work is one the user approves under rule 3, as a new call, after being told
   the first may still be running.
   The gateway's existing one-shot retry after a lost-session refusal
   (`_REHANDSHAKE_RETRY_METHODS` in `backend_handshake.py`) is unaffected: it
   fires only on a refusal the server issues before dispatching anything.
2. A resumed or new turn is told what is outstanding. The interrupted-turn
   preamble and the turn context gain an `[APP WORK]` block listing the
   session's open records: App, tool, summary, status, and that the result will
   be delivered. It is data, framed like other injected blocks.
3. A repeat is held for the user. When a session calls a task-capable tool whose
   `args_digest` matches an open record of the same session, the gateway does not
   forward it. It raises an approval card ("*App* is still running this, started
   *time*. Start it again?"). Approving forwards it as a new call with a new key;
   refusing answers the tool call with the open task's status. A repeat the user
   intended costs one press; a repeat the model made after a restart costs
   nothing.

### 4.8 What the user sees on return

Each session with records shows them in the chat, after the interrupted-turn
notice and before the composer, and the session list marks a session holding a
result, a question, or an unknown. A result also raises a notification. Copy is
illustrative:

| State | Shown as |
|---|---|
| Still running | "*App* is still working on *summary*, started *time*." with the server's status message |
| Needs you | The input card (§4.6) |
| Finished | "*App* finished *summary* while Kiro Crew was offline." with the result, rendered as that tool's result would have been |
| Failed | "*App* reported that *summary* failed: *status message*." |
| Cancelled | "*App* cancelled *summary*." |
| Gone | "*App* no longer has *summary*; it kept results for *TTL*." |
| Unknown | "Kiro Crew sent *summary* to *App* but stopped before *App* confirmed it. Check *App* before running it again." |
| No recovery | See §4.9 |

A delivered result does not start a turn. The next turn, or Resume, receives it
in the `[APP WORK]` block, so the agent continues from it the moment the user
does.

### 4.9 Servers that cannot be recovered

"No recovery: Kiro Crew says so" has three cases:

- **A routed server that does not declare Tasks.** The gateway sees each call, so
  for each call that was in flight when its stub tore down without a person's
  Stop, it writes a `sent`-shaped record marked `unrecoverable`. Calls the tool's
  annotations mark `readOnlyHint: true` are skipped, since the worst case of
  re-running them is cost, and a server that marks a tool falsely only hides its
  own work. On return the session shows "*tool* on *App* was running when Kiro
  Crew stopped. *App* does not report on its work, so Kiro Crew cannot tell
  whether it finished."
- **An unrouted stdio server or an HTTP server.** Kiro Crew does not see the call
  at the protocol level. The interrupted-turn notice adds one line: tool calls on
  servers Kiro Crew does not route cannot be recovered. The MCP management page
  marks a server that declares Tasks but is unrouted, so the user can route it.
- **A server that declares Tasks without meeting §4.1.** Kiro Crew cannot test
  durability from outside. A task id that comes back not found after a restart
  is shown as "Gone" with the App named, which is what the user needs to judge
  the App.

Whether an App that declares durable work should be routed by default, rather
than by the user's per-server choice, is §9 Q1.

### 4.10 Isolation in a shared backend

Before any of the above, the broker must route task traffic per session, as it
already routes resource updates by URI:

- `Backend` keeps a `task_id -> stub_uuid` table, filled from each
  `CreateTaskResult` it forwards or records.
- `notifications/tasks/status` (and the extension's `notifications/tasks`) is
  routed by `params.taskId` through that table, and is consumed by the gateway
  when the task belongs to a record whose engine has gone. One for an unknown id
  is dropped without recording a hazard, so a correct task-capable server is not
  marked per-client for using Tasks.
- `notifications/progress` for a task-backed call keeps its original
  `progressToken` after the call has been answered with `CreateTaskResult`, so
  the gateway keeps a `progressToken -> stub_uuid` entry for each open task and
  routes progress by it until the task is terminal.
- `tasks/get`, `tasks/result`, `tasks/update` and `tasks/cancel` from a stub are
  forwarded only for a task id the table assigns to that stub or to its session's
  records, and refused otherwise. `tasks/list` from a stub is refused on a shared
  backend. This holds whether or not the server binds tasks itself.
- A server-to-client request that carries `io.modelcontextprotocol/related-task`,
  which is how an `input_required` elicitation arrives under `2025-11-25`, is
  attributed by that task id and answered by the gateway (§4.6). Today such a
  request is routed only by `_meta.relatedRequestId` or to a lone attached stub;
  on a backend shared by several stubs anything else makes the gateway record
  `HAZARD_UNROUTABLE_SERVER_REQUEST` and recycle the backend, so without this rule
  a task asking for input on a shared backend would take the backend down.

### 4.11 Session Environments and Kiro Cloud

The contract is the same on every target, because the gateway is the requestor
everywhere a session runs. What differs is which servers exist:

- **On the gateway host**, every routed stdio server qualifies.
- **In a Session Environment** (#19833 stack, not on main), the engine stays on
  the gateway, so the gateway's routed servers and their records behave exactly
  as for a local session. The guest's own workspace server
  (`remote_session/workspace_tools.py`) is a stdio server launched through a
  bridge, not routed, and its `run_command` is bounded at 30 minutes and dies with
  its worker. It declares no Tasks, so it falls under §4.9's second case. A
  `run_command` that should outlive a gateway restart would make it a task whose
  output the worker writes to the workspace; that is a separate change to the
  workspace server, not part of this RFC. The environment itself survives as
  #19833 already specifies: its files across a stop, its processes not while
  `stop_on_shutdown` is on.
- **A KAS session on the gateway host** carries its agent definition, servers
  included, on every start and resume (`src/kiro_crew/acp/harness/kas.py`), and
  the harness projects routed servers through their stubs
  (`session_servers.injection_server_names`), so the contract applies unchanged.
- **A Kiro Cloud Sessions sandbox** (#21260, not on main) is started with no Kiro
  Crew MCP servers and no custom agent, so no App work can start there through
  Kiro Crew and there is nothing for this contract to record. What survives in
  that sandbox is Kiro's to say; on return Kiro Crew shows the conversation
  `session/load` restores and nothing more.

### 4.12 Kiro Crew's own servers

Kiro Crew's own servers park slow calls in-process (`DeferredTool` in
`src/kiro_crew/mcp_shared_calls.py`, re-exported by `mcp_shared.py`), and
`DeferredTool.abandon` already distinguishes
a call that did something a retry would repeat from one that is safe to retry.
They advertise no Tasks and are unaffected. Moving a long Kiro Crew tool onto
Tasks is possible later and not proposed here: sub-agents and workflows already
have their own durable path.

## 5. Phases

| Phase | Delivers | Exit criteria | Blocked on |
|---|---|---|---|
| 0 | This RFC, a reference server, and two probes | A test fixture server (beside `test/fake_pool_mcp_server.py`) implements §4.1 in both forms and keeps tasks in a file, so a second process answers for the first. Verdicts recorded in §9: (a) the `protocolVersion` and client capabilities kiro-cli and KAS send in `initialize` as the broker captures them, and whether a server answers a `tasks` capability under that version; (b) how long kiro-cli and KAS wait on a `tools/call` before giving up | nothing |
| 1 | Task routing and isolation in the broker (§4.10), no persistence | A status notification reaches only the owning stub and records no hazard; a `tasks/get`, `tasks/result` or `tasks/cancel` for another stub's task id is refused on a shared backend; `tasks/list` is refused there; existing routing tests pass unchanged | nothing |
| 2 | The gateway as requestor (§4.2), the store (§4.3) and Stop versus teardown (§4.4), for the `2025-11-25` form, behind `mcp_gateway.tasks.enabled` (default false) | Against the fixture: a task-backed call returns its result to the engine as a plain result; killing the engine, restarting the broker, and killing the gateway each leave a `detached` record and no `tasks/cancel`; a person's Stop sends `tasks/cancel`; a crash between forward and record leaves a `sent` record; the `mcp-tasks` store is masked in an agent sandbox and an agent-planted record is never polled; a call that outlasts the engine's measured wait gets the early answer and its task stays recorded for Phase 3 to deliver | Phase 0(a), Phase 0(b), Phase 1 |
| 3 | The reconciler (§4.5), delivery and the return view (§4.8), and the notices of §4.9 | After a gateway restart a completed fixture task's result appears in its session and raises one notification; a failed one shows its message; an expired one shows "Gone"; an unacknowledged `sent` record shows "Unknown" and is never re-sent; an in-flight call on a non-Tasks routed server shows "No recovery" | Phase 2 |
| 4 | The idempotency key (§4.1 item 7) and the repeat guard (§4.7) | A `sent` record on a server that declares the key is re-sent once with the same key and resolves to one task; a model's repeat of an open call raises the approval card and forwards nothing until approved | Phase 3, §9 Q2 |
| 5 | Input cards (§4.6) | An `input_required` fixture task shows the card after a restart, in the starting session only; the answer resumes the task; an answer from another session or user is refused; a repeated extension `inputRequests` key draws no second card; a URL-mode request opens only on a press; the model never receives the request | Phase 3 |
| 6 | The `2026-07-28` extension form | A fixture task created under the extension is polled with `tasks/get`, answered with `tasks/update`, and never sent `tasks/result` or `tasks/list` | Broker support for `2026-07-28` |

Phases 1 and 2 can ship before any App uses Tasks: Phase 1 only refuses traffic
that would cross sessions, and Phase 2 is off by default.

## 6. Backward compatibility

Compatible. With `mcp_gateway.tasks.enabled` off, the only change is Phase 1's
routing, which affects only task methods and notifications, none of which any
server Kiro Crew ships sends. With it on, a server that does not declare Tasks is
forwarded exactly as today; Phase 3 adds notices for its interrupted calls but
changes no call. For a server that does declare Tasks, a call that used to be
cancelled when its stub tore down now keeps running unless a person pressed Stop,
which is the purpose of the change and is called out in the setting's
description.

## 7. Security considerations

- **No new trust in the server.** Status messages, results and elicitation text
  are untrusted data: redacted before they are stored or shown, framed when they
  reach the model, and never treated as instructions. An elicitation reaches the
  user, not the model.
- **The record is the authority.** The reconciler polls only task ids in the
  gateway-owned store, as the caller each record names. The store is masked in
  every agent sandbox (§4.3), so an agent cannot write it and cannot have Kiro
  Crew poll or cancel another session's task.
  Phase 1's refusals stop a stub from doing the same directly.
- **Binding survives restarts without reusing a credential.** The reconciler's
  stub is claimed with a token minted for it, as the session's own engine is;
  requirement 3 binds tasks to `principal_id` and `session_key`, which the claim
  reproduces, and never to `session_token`, which no third-party server sees.
- **Arguments are not persisted.** Only a digest of the redacted arguments and a
  bounded summary are kept, since tool arguments can carry secrets.
- **Unattended work stays bounded.** A recovered result does not start a turn. A
  repeat of an open call needs the user. The per-session and store caps bound the
  records an agent can create.
- **Governance.** Re-sending a `sent` call (§4.5) re-evaluates the tool against
  the current governance ceiling and approval policy before it is forwarded; a
  call that would now be refused is closed as unknown instead.

## 8. Alternatives considered

### 8.1 Let the engine be the requestor

If kiro-cli and KAS supported Tasks, they could hold task ids themselves. Kiro
Crew controls neither, every harness would differ, and an engine that holds the
id loses it in the same restart that loses the turn. The gateway sees every
routed call for every harness and already outlives the engine within a gateway
lifetime.

### 8.2 An App convention instead of the protocol

Apps could expose `get_job_status`-style tools and the model could be told to
call them on resume. That leaves the record in the transcript, dedupe to the
model's judgement, input to free text, and every App to its own shape. MCP Tasks
already defines the state machine, TTL, input and cancellation.

### 8.3 Keep the server alive across a gateway restart

A broker daemon that outlived its gateway would keep stdio servers and their
in-flight calls running. It would also outlive the gateway's approvals,
governance and owner-liveness guarantee, and does nothing for a crash or a host
reboot. Requirement 2 puts durability where the work is instead.

### 8.4 Extend `JobSDK`

`JobSDK` records runs of runners registered inside the gateway, and its
reconcile pass correctly marks them interrupted, because the gateway process that
ran them is gone. Work an App hands to a remote system does not run in the
gateway, and the MCP server is the one interface every App and user-configured
server shares.

### 8.5 Replay the call on reconnect

The simplest recovery, and the one this RFC exists to rule out for any call the
server has not promised to deduplicate.

## 9. Open questions

1. **Routing by default.** Recovery needs a stub. Should an App manifest be able
   to declare that a server starts durable work, so the server is routed by
   default (the user can still turn the stub off), or should routing stay the
   user's per-server choice with §4.9's nudge?
2. **The idempotency key's name and scope.** `kirocrew.idempotency-key` follows
   the `kirocrew.caller-identity` precedent under `capabilities.experimental`.
   Is a vendor key acceptable until MCP defines one, and should Kiro Crew propose
   one upstream?
3. **Probe verdicts.** Phase 0 records (a) and (b) here. If kiro-cli waits less
   than a long task takes, §4.2 step 4 needs an early answer to the engine,
   with the result delivered as in §4.8 even while the session is attached.
   Under `2025-11-25` that answer can be the server's
   `io.modelcontextprotocol/model-immediate-response` text, which the spec
   marks non-binding and provisional; SEP-2663 does not carry that key over,
   so under the extension Kiro Crew would supply its own text.
4. **Waking on a result.** A finished task could start a turn, under the same
   unattended-turn posture as the webhook RFC's `session` consumer. Is "shown,
   not acted on" right for the first version?
5. **TTL.** Seven days is a guess at how long a person may be away. Should it
   follow the session's retention instead?
6. **HTTP servers.** They are never stubbed, so they are out of the first
   version. Should the reconciler connect to them directly with the session's
   caller identity, given that nothing would record their task ids without a
   gateway in the path?
7. **Cloud sandboxes.** Does Kiro's cloud sandbox keep running while the gateway
   is down, and does `session/load` report work it did meanwhile? That decides
   whether §4.11's last point needs a notice of its own.
