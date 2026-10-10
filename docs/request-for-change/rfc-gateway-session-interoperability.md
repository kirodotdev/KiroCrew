---
title: Gateway Session Interoperability — protocol-neutral local client contract
status: draft
author: twpedersen
created: 2026-10-09
last-audited: 2026-10-09
audited-at: adfd71b8a27e
doc-pr:
implementation-prs: []
tracking-issues: [16891]
supersedes: []
superseded-by: []
---

# RFC: Gateway Session Interoperability — protocol-neutral local client contract

Status: draft. Nothing in this document is an accepted product decision or an
implementation on `main`. The existing question-card routes, session MCP
projection, WebSocket hub, and Python Gateway client are prior art only. The
prototype in [#14999](https://github.com/kirodotdev/KiroCrew/pull/14999) and the
complete source snapshot in
[#7415](https://github.com/kirodotdev/KiroCrew/pull/7415) are evidence used to
falsify the design; neither is an implementation dependency or a PR this RFC
assumes will merge.

## Summary

Let an owner-authorized local client drive an existing Kiro Crew session through
a small protocol-neutral Gateway contract. The contract covers four things that
a protocol adapter cannot safely reconstruct on its own:

1. session-scoped MCP **requests**, filtered through the selected agent's grants
   and the active harness's fail-closed narrowing;
2. reading and answering stateless question cards;
3. correlating a client-originated turn with its streamed and finalized output;
4. subscribing to a bounded, redacted event stream for explicit session keys.

The Gateway remains the only owner of authentication, admission, transcript and
queue mutation, provider replacement, MCP projection, and session lifecycle.
Clients translate their own protocol into this contract. The contract contains
no ACP vocabulary and gives no client a new grant.

This is deliberately narrower than the prototype. It does not standardize a new
presigned-token header. A client presents an owner-subject dashboard token as
the existing port-scoped cookie, which `KiroCrewClient` already does for a
caller-supplied `token`. Obtaining that token is a stated prerequisite, not an
assumed client feature: the client's only built-in exchange mints an app token,
which the owner predicate refuses, and the owner-subject mint that exists today
is the loopback bootstrap route behind `kirocrew token` (open question 9). If
that path is insufficient for a consumer, changing it is a separate
authentication decision rather than a side effect of session interoperability.

The intersection with the selected agent's MCP declarations is keyed by canonical
server name, and the declaration owns the launched command, arguments, and
environment. A client entry never substitutes any of them (open question 8).

## Motivation

### Current state on `main`

Audited at `adfd71b8a27e`.

| Surface | What exists | Missing interoperability contract |
|---|---|---|
| Owner identity | `is_owner_dashboard_request` in `src/kiro_crew/dashboard/handlers/source_providers.py` requires the empty app claim and the configured owner subject | No narrower local-client principal exists; `X-Internal-Secret` is a transport credential, not owner identity |
| Owner token bootstrap | `api_token_local` in `src/kiro_crew/dashboard/handlers/core.py` serves `GET /api/token/local`: a loopback or same-principal unix-socket caller that presents `X-Local-Secret` and passes `local_owner_bootstrap_allowed` (`src/kiro_crew/member_memory_auth.py`, host-process provenance) receives `generate_token(owner_id or "local-app")`, an owner-subject token valid as the `mc_token_<port>` cookie. `kirocrew token` (`_token` in `src/kiro_crew/cli_server.py`) and `src/kiro_crew/app_lifecycle_client.py` use it today | This is the only owner-subject mint a headless local process can call without a browser. The provenance gate refuses a sandboxed or foreign-namespace process, and `kirocrew-client-py` has no method for this bootstrap |
| Internal authentication | `token_auth_middleware` in `src/kiro_crew/dashboard/token_auth.py` marks a matching loopback `X-Internal-Secret` request as `internal_auth` | A child process holding the internal secret must not gain owner-only session mutation authority |
| Question cards | `_register_mcp_routes` in `src/kiro_crew/dashboard/server_runtime/mcp_routes.py` registers the aggregate pending, dismiss, and blocking-answer routes; `api_ask_question_pending`, `api_ask_question_dismiss`, and `api_ask_question_answer` share `_deny_non_owner` in `src/kiro_crew/dashboard/handlers/ask_question.py` | The blocking `ask_id` route and global rehydration list do not provide a slot/card-scoped stateless adapter contract |
| Session MCP | Agent-owned MCP projection is resolved in `src/kiro_crew/acp/session_mcp.py`: `session_mcp_servers` reads the selected agent spec's `mcpServers` map keyed by server name, `acp_server_element` derives each launched command, argument list, and environment from that declaration, and the `tools` allowlist and disabled-server state are also name-keyed. `AcpClient._resolve_session_mcp_servers` and `AcpRuntime._mirrored_session_mcp` consume it | No client-supplied command, arguments, or environment reach a launch today. No authenticated route owns a client request, generation, idempotent mutation, or conditional release/restore |
| Session events | `/api/ws` supports dashboard snapshots, `slot_patch`, question cards, approvals, and privileged subscriptions; `SLOT_PATCH_CAPABILITY` is declared in `src/kiro_crew/dashboard/websocket_hub.py` | No dedicated socket can subscribe to only a bounded set of sessions and receive only minimal redacted message/plan events |
| Python client | `KiroCrewClient.stream_chat` streams chat and `WsClient` provides authenticated WebSockets in `packages/kirocrew-client-py/kirocrew_client/`. They landed in [#14866](https://github.com/kirodotdev/KiroCrew/pull/14866). A caller-supplied `token` is presented as the port-scoped `mc_token_<port>` cookie | The client's only built-in exchange, `_exchange_app_token` against `/api/apps/{app}/token`, mints an app-scoped token that `is_owner_dashboard_request` refuses. The client intentionally excludes question cards, per-session MCP, and turn-origin metadata |
| Neutral vocabulary | No `src/kiro_crew/gateway/` package exists on this audited tree | Shared constants and parsers would otherwise drift between server, client, and adapters |
| ACP adapter | No `src/kiro_crew/acp_server/` package exists on this audited tree | Any ACP implementation is a later consumer, not part of this Gateway decision |

### Problems

1. **An adapter otherwise becomes a second session manager.** It must decide how
   to queue a completed answer, replace a live provider, retain pending messages,
   and restore configuration after a failed session start. Those decisions
   already belong to the Gateway.
2. **MCP configuration is a permission boundary.** A client can name a command,
   arguments, and environment, but that input cannot become a grant merely
   because it arrived on an authenticated route. The selected agent and harness
   remain the authorities.
3. **The ordinary WebSocket is too broad.** A protocol adapter needs a few
   session events, not owner-wide logs, dashboard snapshots, app events, or
   `slot_patch` traffic.
4. **Question cards have two lifecycles.** A blocking in-turn `ask_question`
   call and a stateless/native card are not interchangeable. A client that
   clears the card before the Gateway has accepted the answer can leave a session
   stuck or cause two turns.
5. **Correlation metadata is not authorization.** A client needs to recognize
   its own turn, but a caller-controlled field must not overwrite server-owned
   transcript metadata or survive a restart as an authority claim.
6. **The existing Python client should remain canonical.** Adding another HTTP,
   SSE, or WebSocket stack would recreate the transport duplication this RFC is
   intended to remove.

## Goals

- Define one protocol-neutral wire vocabulary shared by Gateway producers and
  local clients.
- Require the canonical dashboard-owner identity for every new capability.
- Preserve the distinction between a client MCP request and an effective MCP
  grant.
- Bound request bodies, collections, strings, retained receipts, and socket
  subscriptions.
- Keep provider replacement transactional with respect to active leases and
  queued messages.
- Make question-card mutation atomic and compatible with running turns,
  admission reservations, and subagent holds.
- Publish only explicit, redacted session events; no implicit owner-wide feed.
- Extend `kirocrew-client-py` rather than introducing another transport client.
- Keep protocol adapters thin, independently reviewable, and unable to bypass
  Gateway admission.
- Work with every selectable ACP harness through its existing projection and
  narrowing seams.

## Non-goals

- Add an ACP server, editor integration, or adapter protocol to the Gateway.
- Define ACP session IDs, methods, elicitation, permissions, cancellation, or
  content blocks.
- Treat an authenticated client as a source of MCP grants.
- Let a client replace, extend, or override the command, arguments, or
  environment of a server the selected agent declares.
- Add a new owner-token mint route or bearer carrier; owner credential
  acquisition reuses what exists (open question 9).
- Add HTTP/SSE/WebSocket code outside `kirocrew-client-py`.
- Make app tokens, arbitrary dashboard members, or claimless internal-secret
  callers equivalent to the owner.
- Expose owner-wide logs, approvals, subagent events, or dashboard snapshots on
  the dedicated session-event socket.
- Support remote MCP transports in the first phase. The initial request shape is
  bounded stdio only.
- Persist client registration authority across a Gateway restart.
- Change existing dashboard question-card, `slot_patch`, or ordinary `/api/ws`
  behavior.
- Merge, replace, or depend on the source snapshot in #7415.

## Terminology and trust model

### Four identities that must not be conflated

| Identity | Meaning | Authority |
|---|---|---|
| Dashboard owner | The authenticated subject accepted by `is_owner_dashboard_request` | May use the new routes |
| Internal transport | A loopback process that proves `X-Internal-Secret` | Existing exact internal routes only; no new owner authority |
| Registration owner | A bounded opaque string chosen by a client to coordinate replace/restore/clear | Compare-and-swap metadata only; never authorization |
| Turn origin | A bounded opaque value reflected on output so a client can correlate its turn | Correlation only; never authorization or session selection |

Authentication is evaluated first. Registration ownership and turn-origin fields
are ignored unless the request is already an owner request. An app-derived
request remains an app request even if it can also reach loopback, and cannot
assert either field.

### Client MCP entries are requests

A request entry says: "for this session, ask to mount the declared stdio server
with this name." It does not say:

- the server is declared by the selected agent;
- the server is enabled by policy;
- its tools are allowed;
- the active harness can enforce per-tool narrowing;
- the server started successfully.

The effective set is:

```text
validated client request
∩ selected agent declarations
∩ server enable policy
∩ tool allow/deny policy
∩ harness-supported narrowing
∩ runtime launch success
```

No layer may replace an intersection with a union. Diagnostics report requested,
admitted, and ready state separately.

#### Join key and override rule

The intersection with "selected agent declarations" compares **canonical server
name only**, because that is the identity the current projection, `tools`
allowlist, and disabled-server state already use. The declaration owns the
launch tuple — command, argument list, and environment — for every name it
declares.

- A requested name with no declaration is requested but never admitted.
- A requested name with a declaration is launched with the **declared** tuple.
- A client entry that carries `command`, `args`, or `env` is admitted only when
  each present field equals the declaration's canonical value (environment
  compared by one-way digest). Any difference refuses the whole request with
  `declaration_mismatch`; nothing is substituted, merged, or launched.
- **No client field overrides a declared command, argument, or environment in
  any phase of this RFC.** An override would be a grant, and a grant needs its
  own RFC.

Under this rule the request's job is to select which declared servers a session
mounts and to coordinate that selection across clients through `replace`,
`clear_if_owner`, and `restore_if_owner`. Whether the tuple fields stay on the
wire as an equality attestation or Phase 1 accepts name-only entries is open
question 8.

## Design

### 1. Shared Gateway vocabulary

A small `kiro_crew.gateway` package owns transport-neutral constants, bounded
parsers, and typed value objects. It imports no ACP adapter package. Dashboard
handlers, `kirocrew-client-py`, and later adapters consume the same vocabulary
or a generated/public equivalent.

The package owns only wire semantics. Session state remains in the dashboard and
provider state remains in ACP/runtime owners.

### 2. Authentication and admission

Every route and the dedicated socket require a request for which
`is_owner_dashboard_request(request)` is true.

The accepted carrier is the existing port-scoped `mc_token_<port>` cookie
holding an owner-subject dashboard token, plus the WebSocket `Origin`
requirement. The Python client already presents a caller-supplied token that
way. This RFC adds no new bearer carrier and no new mint route.

How a headless local client obtains that token is a Phase 1 dependency, not an
assumption. Today the only browserless owner-subject mint is
`GET /api/token/local`, gated on loopback or same-principal unix-socket
transport, `X-Local-Secret`, and the `local_owner_bootstrap_allowed` provenance
check; `kirocrew token` is its CLI consumer. A client that is refused there — a
sandboxed or foreign-namespace process — has no owner path in this RFC. Open
question 9 records whether Phase 2 adds that bootstrap to `kirocrew-client-py`
or requires the caller to supply the token out of band.

The following callers are refused before any body or subscription mutation:

- claimless `X-Internal-Secret` callers;
- app tokens, including app-derived internal requests;
- authenticated non-owner dashboard members;
- presigned credentials scoped to another app or subject;
- remote plaintext callers that fail existing dashboard authentication.

The refusal does not reveal whether a slot, card, registration owner, or
subscription key exists. Existing legacy routes that deliberately admit
`internal_auth` are unchanged.

### 3. Session-scoped MCP registration

#### Route

```text
POST /api/chat/slots/{slot}/mcp
```

The route accepts a bounded JSON object:

```json
{
  "mode": "replace",
  "owner": "client-instance-a",
  "servers": [
    {
      "name": "workspace-tools",
      "type": "stdio",
      "command": "/absolute/path/to/server",
      "args": ["--stdio"],
      "env": {"MODE": "read-only"}
    }
  ],
  "mutation_id": "opaque-idempotency-key",
  "return_previous": false
}
```

`owner` is registration coordination metadata, not the authenticated principal.
It is required for every operation that can establish or clear a registration.

`name` is the join key against the selected agent's declarations. `command`,
`args`, and `env` are optional and, when present, must equal the declared tuple
for that name; they never replace it (see "Join key and override rule"). The
example above is therefore admitted only if the selected agent declares
`workspace-tools` with exactly that command, argument list, and environment.

#### Modes

| Mode | Effect |
|---|---|
| `replace` | Validate and replace the slot's requested array, minting a new generation |
| `clear_if_owner` | Clear only when `owner` matches the current registration owner; a mismatch is a successful no-op |
| `restore_if_owner` | Restore a previous value only when the current owner and optional generation still match the caller's expected values |

`restore_if_owner` exists for transactional client flows: a client may switch a
project or recreate a backing session, then restore its prior MCP request only if
nobody else changed the slot in the meantime. It is not a restart-persistent
claim.

A bounded `mutation_id` makes transport replay idempotent while the Gateway
process lives. Reusing one ID with a different canonical request is a conflict.
Receipts are count- and age-bounded, restart-ephemeral, and must not be presented
as durable exactly-once delivery.

#### Parser and bounds

The initial phase accepts only stdio entries. Each entry has an explicit name and
optional command, string argument list, and string environment map. The server
rejects:

- unsupported transports;
- duplicate names under the canonical comparison used by the target harness;
- reserved Kiro Crew server names and app-style names;
- a present `command`, `args`, or `env` that differs from the selected agent's
  declaration for that name (`declaration_mismatch`);
- unknown fields where accepting them would imply unsupported behavior;
- over-budget arrays, names, commands, arguments, environment entries, values,
  and total request bodies.

The exact constants live in one parser and are tested at their boundary. Error
messages identify the field and index but do not echo commands, arguments,
environment values, or credentials.

#### Projection

Validation does not make a request effective. After parsing, the Gateway resolves
each admitted name to the selected agent's declaration, calls the agent's
existing projection path, and then the selected harness's narrowing path. The
launched tuple is always the declared one. A backend with no safe representation
for a restriction withholds the affected server rather than mounting it broadly.

Name normalization is evaluated before provider reuse. If a target harness folds
two names to the same wire identity, the request is rejected or deterministically
narrowed; it never gains the first or last entry by accidental ordering. The
provider reuse fingerprint includes the ordered canonical request when order can
change harness behavior.

#### Remote slots

A remote-crew slot is refused in the first phase. The local Gateway does not own
that slot's provider lifecycle, process environment, or authorization ceiling.
Remote support requires a peer-owned equivalent contract and is a separate RFC.

### 4. Stateless question-card API

#### Routes

```text
GET  /api/chat/slots/{slot}/questions
POST /api/chat/slots/{slot}/questions/{card_id}/answer
```

The read returns only stateless cards associated with that slot. Blocking
`ask_id` questions continue through the existing blocking tool lifecycle and are
not silently converted.

The answer body is a non-empty bounded map from question text or stable question
ID to a string or list of strings. The Gateway validates the entire answer,
slot, card identity, optional turn origin, and registration-owner freshness
before mutating the card.

#### Delivery rules

- A native card belonging to the currently running turn may be steered into that
  turn through one atomic answer claim.
- A completed ordinary stateless answer arriving while a turn, admission
  reservation, or subagent hold exists is queued through the ordinary held-turn
  path.
- An origin-scoped stateless answer is refused with `slot_busy` when its immutable
  admission snapshot can no longer be used safely.
- An incomplete multi-question answer updates no turn and leaves the card
  available.
- A delivery failure releases the atomic answer claim so the owner can retry.
- A concurrent second completed answer receives a coded conflict and cannot
  start another turn.

A missing card remains distinct from malformed input and from a busy slot. The
client clears its UI only after the Gateway confirms mutation or reports that the
card no longer exists.

### 5. Turn-origin correlation

An owner client may attach bounded correlation metadata to `/api/chat`. The
server records it under a reserved namespace that ordinary prompt content and
client-supplied metadata cannot overwrite.

The value is reflected in:

- the SSE stream for that admitted request;
- finalized `session_message` events for the resulting user and assistant rows;
- queued successors only when they were admitted as part of the same client
  request.

The value is not a permission, a slot selector, or an MCP registration owner. It
is ignored for non-owner callers.

Admission-only MCP snapshots and authority-bearing coordination data remain
process-local. Durable queue serialization strips them, and restore drops any
hand-edited copy. After restart, a queued row may retain inert display
correlation only if the persistence contract explicitly types it as such; it
cannot recover registration authority from that value.

The exact carrier — header or a reserved request object — remains open question
1. The server-owned persisted namespace and the authorization rule do not.

### 6. Dedicated session-event socket

A client requests the `session_events` capability during the authenticated
`/api/ws` upgrade, then sends:

```json
{"type": "subscribe_sessions", "keys": ["slot-a", "slot-b"]}
```

The subscription is replacement, not accumulation: each accepted frame replaces
the connection's prior key set. The key count and each key length are bounded.
Unknown or unauthorized keys are omitted without revealing existence.

A dedicated socket receives only:

| Event | Payload |
|---|---|
| `session_message` | slot key, redacted user or assistant row, inert turn correlation when present |
| `session_plan` | slot key and complete redacted plan snapshot; `null` withdraws the plan |
| `slot_title` | slot key and redacted title, if maintainers accept open question 4 |

Plan updates are full snapshots, not deltas. That lets clients recover after a
dropped frame without replaying a private dashboard state machine.

The dedicated socket receives no initial dashboard snapshot, owner-wide log
ring, approvals, subagent events, app events, `slot_patch`, or unscoped
broadcast. Its inbound control plane accepts only session subscription changes
and close/ping behavior required by the WebSocket implementation. An attempt to
send an ordinary privileged subscription such as `subscribe_logs` is refused.

Redaction runs independently for each event type at publication time. The
Gateway never trusts a client to redact.

### 7. Session and provider lifecycle

A configured MCP request is attached to the live slot and survives:

- cold provider creation;
- session load/resume;
- provider compaction and recreation;
- a project switch when the client conditionally restores it.

It does not become a new durable grant and does not survive Gateway restart as an
authoritative owner claim. A client reconnects, observes no live registration,
and establishes a new generation.

Provider identity includes the effective request state. A change that could alter
the launched MCP array cannot reuse a provider created for the old state.
Replacement follows this order:

1. admit and validate the new request;
2. wait for active provider leases to quiesce;
3. detach accepted queued entries from the old provider;
4. park them in the existing bounded queue-transfer mechanism;
5. replace the provider;
6. restore entries in original order, preserving attachments;
7. publish requested/admitted/ready diagnostics separately.

Failure before the new provider is ready either keeps the old provider and
registration or leaves a coded, recoverable no-provider state. It never reports
success while silently running the old MCP set.

Ordinary queued or synthesized successor turns use the slot's current MCP
configuration in a clean request context. They do not inherit a predecessor's
turn origin or request-local MCP snapshot through task-local context propagation.

### 8. Canonical Python client and adapters

The server contract lands before any adapter. The next phase extends
`packages/kirocrew-client-py` with typed methods and event models for these
routes. It reuses the client's existing authentication refresh, request error,
SSE, reconnect, and path-escaping behavior.

A later adapter:

- depends on the typed client;
- translates adapter session identity into an existing Gateway slot;
- translates question cards and session events into its own protocol;
- never reads the internal secret directly when an owner token is required, and
  obtains its owner token only through the path open question 9 accepts;
- contains no duplicate provider, queue, or MCP admission logic.

ACP is the first expected consumer, but ACP names and schemas stay outside the
Gateway package and outside this RFC's implementation phases.

### 9. Error contract

Every refusal has a stable machine code and a human-readable message. At minimum
the contract distinguishes:

- unauthenticated / stale owner / not owner;
- slot not found;
- malformed or over-budget request;
- unsupported transport or remote slot;
- declaration mismatch on a declared server name;
- stale registration owner or generation;
- mutation replay conflict;
- question not found, invalid answer, answer in progress, and slot busy;
- provider replacement failure;
- unsupported event capability or malformed subscription.

Opaque authorization failures do not reveal whether the named slot, card, or
owner exists. Validation errors never include environment values or other
credential-bearing input.

## Security considerations

### Internal secret is not owner identity

The Gateway gives its internal transport secret to trusted host components, and
some gateway-spawned contexts can present it back to exact internal routes. That
proves local transport, not that a human owner chose the mutation. Granting the
new routes on `internal_auth` would let a child that can issue local HTTP mutate
question answers, session MCP processes, provenance, or subscriptions.

The new surfaces therefore use the canonical owner predicate with no internal
secret exemption. Existing internal routes keep their current behavior.

### MCP command execution

A client request entry may carry an executable path, arguments, and environment,
but none of them is ever launched. The launched tuple is the selected agent's
declaration for the matching canonical name; client values are compared to it
and a mismatch refuses the request. A name-only match therefore cannot run an
undeclared binary or alter a declared one's environment under a granted name.
Owner authentication is necessary but not sufficient. The selected agent
declaration, governance, disabled-server state, disabled-tool state, harness
capabilities, sandbox, and runtime launch checks all remain in force.

Client input must never be copied into a provider-global config file. It is
session-scoped and runtime-owned. Environment values are excluded from logs,
events, errors, and equality diagnostics; fingerprints and the declaration
comparison use a one-way canonical digest where needed.

### Event confidentiality

The dedicated socket is owner-only, key-scoped, count-bounded, and redacted. It
has no owner-wide default subscription. Subscriptions are held per connection
and released on close. Message, title, and plan publishers each apply their own
allowlist and redaction rather than forwarding internal row dictionaries.

### Replay and persistence

Mutation receipts prevent benign transport replay but are bounded and ephemeral.
They are not a security nonce. Registration owner and generation checks prevent
one cooperating client from clearing or restoring another client's current
configuration, but owner authentication remains the security boundary.

Turn-origin metadata is inert. Persistence must not make it an admission token.

### Denial of service

The request body, server array, arguments, environment, receipts, subscriptions,
and event payloads are bounded. Repeated provider replacement is serialized per
slot and cannot evict a provider with an active lease. Event publication uses the
existing backpressure and dead-socket cleanup path.

## Backward compatibility

Compatible by default. All new behavior is opt-in and owner-authenticated.
Existing dashboard routes, question cards, ordinary `/api/ws`, `slot_patch`, app
isolation, queue behavior, and provider-global MCP configuration keep their
current semantics.

The first implementation phase adds no new authentication carrier and changes no
existing internal-secret allowlist. Existing clients that do not request the new
capability receive no new frames.

A later adapter may expose these capabilities through its own protocol, but that
adapter has its own compatibility review.

## Migration plan

Each phase is independently shippable and independently abandonable. No phase
begins until this RFC is accepted and merged to `main`.

### Phase 0 — decision record

Land this RFC alone as `draft`. A maintainer records answers to the open questions
in this document and changes `status` to `accepted` in a separate decision PR.

**Exit criteria**

- A maintainer has recorded acceptance in the RFC.
- The auth principal, correlation carrier, event vocabulary, and restart posture
  have explicit answers.
- The MCP join key and override rule (open question 8) and the owner-credential
  acquisition path (open question 9) have explicit answers, so no implementer
  chooses between the two readings of "intersected with declarations" or
  assumes a token source the client does not have.
- No implementation code is in the decision PR.

### Phase 1 — Gateway server contract

A team-owned PR adds the neutral vocabulary, bounded parser, owner-only routes,
transactional slot state, provider-lifecycle integration, and dedicated event
socket. It updates the owning MCP, session, dashboard-auth, security, and feature
map documents in the same commit.

**Exit criteria**

- Client entries are demonstrably intersected with declarations by canonical
  name and with every selectable harness's narrowing.
- A declared name is launched with the declared command, arguments, and
  environment; a client tuple that differs is refused with
  `declaration_mismatch`, and an undeclared name is never launched. No test
  path launches client-supplied values.
- Claimless internal, app, and non-owner subjects are denied before mutation.
- Every new route and the dedicated socket are exercised by a headless process
  holding a token minted through the path open question 9 accepts, with no
  browser involved.
- Replace/clear/restore, generation, receipt replay/conflict, provider reuse,
  lease wait, queue transfer, restart loss, and remote-slot refusal are covered.
- Question answers cover running turns, admission reservations, subagent holds,
  incomplete answers, concurrent answers, and failed delivery.
- Dedicated sockets receive only explicit redacted events for bounded keys and
  cannot subscribe to logs or `slot_patch`.
- Cross-platform CI and the security/design review lanes pass.

### Phase 2 — extend `kirocrew-client-py`

A separate PR adds typed methods and event objects to the existing client. It
adds no Gateway behavior. If open question 9 accepts it, the same PR adds the
owner-token bootstrap helper, reusing the route and secret resolution that
`kirocrew token` already uses rather than a new carrier.

**Exit criteria**

- Every new method is checked against the real router and wire response shape.
- The new methods refuse to run with an app token, and the bootstrap helper, if
  accepted, mints only through the existing loopback route and surfaces its
  provenance refusal as a coded error rather than falling back to the internal
  secret.
- Authentication refresh does not replay an already-applied mutation or turn.
- SSE correlation, WebSocket reconnect/resubscribe, error-code mapping, path
  escaping, and malformed-frame handling are covered.
- Existing client methods and supported Python versions remain green.

### Phase 3 — first protocol adapter

A team-owned adapter PR consumes only the typed client and the accepted Gateway
contract. ACP is a candidate, not a requirement of Phases 1 or 2.

**Exit criteria**

- The adapter contains no duplicate auth, queue, provider-replacement, or MCP
  grant logic.
- Adapter protocol conformance is tested separately from Gateway behavior.
- Session create/load/resume, turn streaming, cancellation, questions, events,
  and MCP request restoration have closed-box coverage.
- Removing the adapter leaves the Gateway and Python client complete and useful.

## Alternatives considered

### Keep server, client, and adapter in one PR

Rejected. It couples three review boundaries, makes protocol vocabulary leak into
the Gateway, and prevents maintainers from accepting or replacing one layer
without the others. The review history of #7415 is evidence of that cost.

### Merge the existing prototype, then document it

Rejected. The issue is classified as a new concept. The decision must exist on
the base branch before implementation, and maintainers stated that the team owns
the implementation decision. #14999 remains useful falsification evidence only.

### Let each adapter call existing dashboard routes directly

Rejected. It duplicates body shapes, auth carriers, event filtering, and
question-card lifecycle. It also leaves per-session MCP and correlation without a
server-owned contract.

### Use the internal secret as local-client authorization

Rejected. Locality is not owner intent. The secret is shared with host components
and cannot authorize executable MCP configuration or answers an agent will act
on.

### Give clients a direct MCP grant API

Rejected. It inverts the trust model. A client may request an already-declared
server by name; it cannot declare permission on behalf of an agent, profile,
governance policy, or harness, and it cannot substitute the executable,
arguments, or environment the declaration names.

### Let a matching name carry the client's command, args, and env

Rejected. Joining on name while launching the client's tuple would let an owner
client run an undeclared binary, or a declared one with a different environment,
under a granted name. That is a grant by another route. Joining on the full
tuple without an override rule would leave implementers to pick one of the two
readings. The RFC therefore joins on name, launches the declared tuple, and
refuses any client tuple that differs.

### Add a second typed Gateway client

Rejected. `kirocrew-client-py` already owns HTTP, SSE, WebSocket, auth refresh,
path escaping, and route-contract tests. It is the extension point.

### Send all ordinary dashboard WebSocket events and filter client-side

Rejected. It discloses owner-wide state, creates an unbounded subscription shape,
and makes redaction and least privilege depend on every client implementation.

## Prototype evidence

The prototype in #14999 exercised the proposed boundary across multiple review
rounds. It found defects that this RFC turns into explicit requirements:

- request-owned MCP names can collide after harness-specific folding;
- provider reuse must include the effective ordered MCP identity;
- replacing a provider must detach and park accepted queued entries;
- a question card needs an atomic completed-answer claim;
- request-local context must be cleared before a queued successor starts;
- a dedicated event socket must be excluded from owner-wide and `slot_patch`
  fan-out;
- subscription key sets need a hard bound;
- message events cannot depend on an unrelated SSE-reader predicate;
- claimless internal-secret authorization is too broad for these capabilities.

Those findings support the invariants above. They do not make the prototype's
exact code or wire spelling accepted.

## Open questions

1. **Correlation carrier.** Should turn origin be an HTTP header, a reserved
   request object, or a server-minted ID returned by the chat admission receipt?
   Recommendation: server-minted ID, with an optional bounded client correlation
   value echoed separately. That removes any appearance that a client value is
   provenance.
2. **Registration owner spelling.** Is `owner` sufficiently clear when it is not
   an authenticated principal? Recommendation: use `registration_id` on the wire
   and reserve `owner` for the authenticated dashboard owner.
3. **Restart posture.** Should a session MCP request be restart-ephemeral or
   persisted as non-authoritative configuration requiring fresh owner adoption?
   Recommendation: restart-ephemeral in Phase 1; persistence is a separate
   decision with a protected store and migration contract.
4. **Title events.** Does the minimal event stream include redacted `slot_title`,
   or only message and full-plan snapshots? Recommendation: include title because
   an adapter otherwise must poll after the existing auto-title lifecycle runs.
5. **Question identity.** Should answers key by question text, stable question ID,
   or both during migration? Recommendation: mint stable per-question IDs and
   retain text only for display; text-keyed compatibility can be temporary.
6. **MCP restore modes.** Does Phase 1 need `restore_if_owner`, or can the client
   always issue a fresh `replace` after a failed project/session transition?
   Recommendation: keep conditional restore because unconditional replace can
   overwrite a newer client's registration.
7. **First consumer.** Should the first adapter be ACP, or should Phase 1 and 2
   prove reuse with a smaller closed-box client first? Recommendation: do not
   block the neutral layers on ACP, but require one non-adapter integration test
   through `kirocrew-client-py`.
8. **MCP declaration identity and client fields.** The join key is settled as
   canonical server name with the declaration owning the launch tuple, and no
   client override exists in any phase. Open is the wire shape: (a) Phase 1
   accepts name-only entries and rejects `command`, `args`, and `env` as unknown
   fields, or (b) the tuple fields stay optional and are admitted only when they
   equal the declaration, as drafted in §3. Recommendation: (b) for Phase 1,
   because a client that expected a different executable then learns so from
   `declaration_mismatch` instead of silently running the declared one; (a) is
   the acceptable smaller shape if maintainers prefer fewer fields. Either choice
   must keep the rule that a name match never launches client-supplied values.
9. **Owner credential acquisition.** `kirocrew-client-py` presents a
   caller-supplied token as the port-scoped cookie but can mint only an app
   token. Should Phase 2 add a helper that performs the existing
   `GET /api/token/local` bootstrap — same `X-Local-Secret` and
   `local_owner_bootstrap_allowed` gates as `kirocrew token` — or must callers
   supply an owner token out of band? Recommendation: add the helper in Phase 2
   with no new route or carrier, and state that a process the provenance gate
   refuses (sandboxed, foreign namespace, remote) has no owner path under this
   RFC; widening that gate is a separate authentication decision. Phase 1 must
   not start until this answer exists, because every new route depends on it.
