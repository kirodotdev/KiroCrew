---
title: Gateway Session Interoperability — protocol-neutral local client contract
status: draft
author: twpedersen
created: 2026-10-09
last-audited: 2026-10-23
audited-at: 33cdb8f1ee68
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

The Gateway remains the only owner of authentication, authorization, admission,
transcript and queue mutation, provider replacement, MCP projection, and session
lifecycle. Clients translate their own protocol into this contract. The contract
contains no ACP vocabulary and gives no client a new grant.

This is deliberately narrower than the prototype. The new surfaces accept only
an existing owner-subject token minted through `GET /api/token/local`, carrying
signed local-mint provenance, and only from a direct-local peer that is not a
gateway-managed agent or app process. Authentication establishes the owner
subject; a separate route authorization rejects app-minted tokens and requests
from an app or agent process tree. `X-Internal-Secret`, an app token, and the
owner predicate alone are insufficient.

The MCP intersection is keyed by canonical server name. Phase 1 accepts names
only: command, argument, environment, and transport fields are rejected as
unknown. The selected agent declaration owns the complete launch tuple. This
removes both an override path and an equality oracle over declared environment
values.

Every new route and the dedicated socket are registered unconditionally and
refused at request time while `dashboard.session_interop_enabled` is `false`.
The key is read from live configuration on every request and subscription change;
it has no restart mark. An otherwise authorized caller receives
`session_interop_disabled` while it is off. The switch remains off by default
through the server and typed-client phases; a protocol adapter that proposes
enabling it by default needs its own compatibility decision.

Phase 1 has a hard external prerequisite: a separate managed-execution
containment RFC must be accepted and implemented first. That decision owns the
system-wide process-isolation mechanism and its compatibility, performance, and
failure analysis; this RFC specifies only the attestation properties the local
client contract consumes. The initial contract is Linux-only when the prerequisite reports an eligible
attestation and this feature is explicitly enabled. A default installation has
no usable Phase 1 consumer, and macOS and Windows refuse every new capability.

## Motivation

### Current state on `main`

Audited at `33cdb8f1ee68`.

| Surface | What exists | Missing interoperability contract |
|---|---|---|
| Owner identity | `is_owner_dashboard_request` in `src/kiro_crew/dashboard/handlers/source_providers.py` requires an empty app claim and the configured or implicit local-owner subject | It proves the authenticated subject, not that a host process is outside a gateway-managed agent or app execution tree |
| Owner token bootstrap | `api_token_local` in `src/kiro_crew/dashboard/handlers/core_local_token.py` serves `GET /api/token/local`: a loopback or same-principal unix-socket caller presents `X-Local-Secret`, then `local_owner_bootstrap_provenance` in `src/kiro_crew/member_memory_auth.py` admits either a verified ordinary host process or a live app backend launched by this gateway. `LocalOwnerProvenance.app` is written to the SEL mint record but not to the token. Windows admits any positively identified local peer; macOS requires an unsandboxed peer; Linux requires matching namespaces unless the app-backend registry owns the process | An app backend or agent process that qualifies as an ordinary host peer can mint a claimless owner token. The token and later request carry no signed mint classification that the proposed routes can authorize against |
| Internal authentication | `token_auth_middleware` in `src/kiro_crew/dashboard/token_auth.py` marks a matching loopback `X-Internal-Secret` request as `internal_auth`; because it sets no dashboard-user claim, `is_owner_dashboard_request` already refuses it | The new routes must preserve that refusal with no internal-secret exemption and fold it into the route family's opaque authorization response |
| Question cards | `_register_mcp_routes` in `src/kiro_crew/dashboard/server_runtime/mcp_routes.py` registers aggregate pending, dismiss, and blocking-answer routes; `api_ask_question_pending`, `api_ask_question_dismiss`, and `api_ask_question_answer` share the owner gate in `src/kiro_crew/dashboard/handlers/ask_question.py` | The blocking `ask_id` route and global rehydration list do not provide a slot/card-scoped stateless adapter contract |
| Session MCP | `session_mcp_projection` in `src/kiro_crew/acp/session_mcp.py` resolves the selected agent spec, disabled servers, disabled tools, allowlist, safe server elements, and the source snapshot from one parse. `providers/mirrors/registry.py` declares how every selectable harness projects that result | No authenticated route owns a client request, generation, idempotent mutation, conditional release/restore, or provider replacement deadline |
| Harnesses | `BASELINE_SELECTABLE_BACKENDS` in `src/kiro_crew/agent_sdk/backends.py` contains Kiro CLI, KAS, Claude Code, Codex, OpenCode, Pi, Goose, and DeepSeek; KAS is the default when selectable | The harnesses have different projection and per-tool narrowing seams. A generic “every selectable harness” criterion does not say what Phase 1 must test |
| Session events | `/api/ws` supports dashboard snapshots, `slot_patch`, question cards, approvals, and privileged subscriptions; `SLOT_PATCH_CAPABILITY` is declared in `src/kiro_crew/dashboard/websocket_hub.py` | No dedicated socket can subscribe to only a bounded set of sessions and receive only minimal redacted message/plan events |
| Python client | `KiroCrewClient.stream_chat` streams chat and `WsClient` provides authenticated WebSockets in `packages/kirocrew-client-py/kirocrew_client/` | The client's built-in exchange mints an app-scoped token. It has no eligible local-owner bootstrap helper, question-card methods, per-session MCP methods, or turn-origin model |
| Neutral vocabulary | No `src/kiro_crew/gateway/` package exists on this audited tree | Shared constants and parsers would otherwise drift between server, client, and adapters |
| ACP adapter | No `src/kiro_crew/acp_server/` package exists on this audited tree | Any ACP implementation is a later consumer, not part of this Gateway decision |

### Problems

1. **An adapter otherwise becomes a second session manager.** It must decide how
   to queue a completed answer, replace a live provider, retain pending messages,
   and restore configuration after a failed session start. Those decisions
   already belong to the Gateway.
2. **MCP configuration is a permission boundary.** A client may select only a
   declared server name. No authenticated route may turn client input into a
   command, argument, environment, transport, or tool grant. The selected agent
   and harness remain the authorities.
3. **The ordinary WebSocket is too broad.** A protocol adapter needs a few
   session events, not owner-wide logs, dashboard snapshots, app events, or
   `slot_patch` traffic.
4. **Question cards have two lifecycles.** A blocking `ask_id` card with a
   server-side wait and a stateless `card_id` card are not interchangeable. A
   client that clears either before the Gateway has accepted the answer can leave
   a session stuck or cause two turns.
5. **Correlation metadata is not authorization.** A client needs to recognize
   its own turn, but a caller-controlled field must not overwrite server-owned
   transcript metadata or survive a restart as an authority claim.
6. **The existing Python client should remain canonical.** Adding another HTTP,
   SSE, or WebSocket stack would recreate the transport duplication this RFC is
   intended to remove.

## Goals

- Define one protocol-neutral wire vocabulary shared by Gateway producers and
  local clients.
- Require an owner-subject local-bootstrap token with signed mint provenance,
  then authorize the request separately from authentication.
- Refuse every new capability to a gateway-managed app backend or agent process
  tree, including self-action against its own slot.
- Preserve the distinction between a client MCP request and an effective MCP
  grant.
- Accept MCP server names only; no caller-supplied launch tuple or declaration
  equality test exists.
- Bound request bodies, collections, strings, retained receipts, provider
  mutation attempts, and socket subscriptions.
- Keep provider replacement transactional with respect to active leases and
  queued messages, returning immediately when the provider is busy.
- Make question-card mutation atomic and compatible with running turns,
  admission reservations, and subagent holds.
- Publish only explicit, redacted session events; no implicit owner-wide feed.
- Audit every accepted mutation and subscription change with the authenticated
  token's mint provenance before committing the side effect.
- Extend `kirocrew-client-py` rather than introducing another transport client.
- Keep protocol adapters thin, independently reviewable, and unable to bypass
  Gateway admission.
- Define and verify the session-scoped projection or explicit fail-closed
  outcome for every selectable ACP harness. A missing carrier returns
  `harness_unsupported`; it never silently inherits another harness's behavior.
- Keep every new surface default-off through the server and typed-client phases.

## Non-goals

- Add an ACP server, editor integration, or adapter protocol to the Gateway.
- Define ACP session IDs, methods, elicitation, permissions, cancellation, or
  content blocks.
- Treat an authenticated client as a source of MCP grants.
- Let a client send, compare, replace, extend, or override the command,
  arguments, environment, or transport of a server the selected agent declares.
- Add a new owner-token mint route or bearer carrier.
- Grant these routes to app tokens, app-minted owner tokens, gateway-managed app
  backends, agent process trees, arbitrary dashboard members, or claimless
  internal-secret callers.
- Prove a fresh human gesture. The narrower claim is that the caller is an
  authenticated owner automation process outside gateway-managed execution
  trees; interactive presence would require a separate consent protocol.
- Add HTTP/SSE/WebSocket code outside `kirocrew-client-py`.
- Expose owner-wide logs, approvals, subagent events, or dashboard snapshots on
  the dedicated session-event socket.
- Support remote MCP transports or remote slots in the first phase.
- Persist client registration authority across a Gateway restart.
- Change question-card paging, answer labels, blocking `ask_question` delivery,
  auto-submit gestures, `slot_patch`, or ordinary `/api/ws` behavior.
- Enable the new route family by default before a separately reviewed consumer
  requires it.
- Merge, replace, or depend on the source snapshot in #7415.

## Terminology and trust model

### Identities that must not be conflated

| Identity | Meaning | Authority |
|---|---|---|
| Dashboard owner | The authenticated subject accepted by `is_owner_dashboard_request` | Necessary for every new route; insufficient on its own |
| Token mint provenance | Signed `mint_source`, `mint_process`, and optional `mint_app` claims copied through link-to-session exchange | Restricts the token to an eligible local-client mint; never grants owner identity |
| Request peer provenance | Kernel-resolved process identity and ancestry for the direct-local HTTP or socket peer | Refuses current calls from gateway-managed app or agent trees; never authenticates the subject |
| Internal transport | A loopback process that proves `X-Internal-Secret` | Existing exact internal routes only; no new owner authority |
| Registration id | A bounded opaque string chosen by a client to coordinate replace/restore/clear | Compare-and-swap metadata only; never authorization |
| Turn origin | A bounded opaque value reflected on output so a client can correlate its turn | Correlation only; never authorization or session selection |

### Question-card kinds

Main has two kinds, defined by `src/kiro_crew/dashboard/handlers/ask_question.py`:

- A **blocking** card carries `ask_id` and has a server-side wait. The MCP
  `ask_question` call remains parked until the existing answer route resolves it.
  This RFC's stateless routes never list or answer that kind.
- A **stateless** card carries `card_id`, blocks no server-side call, and is kept
  in the slot's pending-question record. Its existing `native` boolean is a
  delivery hint on this kind, not a third kind: it says the card came from the
  currently running provider turn and may be steered back into that same turn.

Authentication and authorization are separate gates. Normal token middleware
first verifies the signed owner token. The route then requires the local-client
mint claims and current peer provenance described in section 2. Registration and
turn-origin fields are read only after both gates pass.

Normal token middleware keeps its existing authentication responses. Once a
request reaches this route family, every route-specific authorization failure
uses the same status, code, and message, `403 client_not_authorized`, whether the
missing fact is owner identity, mint provenance, direct-local transport, process
identity, app ancestry, or agent ancestry. The SEL record carries the specific
reason. Slot, card, registration, and subscription existence are not looked up
first.

### Client MCP entries are requests

A request entry says: “for this session, ask to mount the declared stdio server
with this name.” It does not say:

- the server is declared by the selected agent;
- the server is enabled by policy;
- its tools are allowed;
- the active harness can enforce per-tool narrowing;
- the server started successfully.

The client selection governs only non-control-plane servers declared by the
selected agent. `CONTROL_PLANE_SERVERS` is outside the client intersection: when
the selected agent and policy mount Kiro Crew's control plane, omitting its name
from a request cannot withdraw it. The control plane remains subject to the
agent's `tools` allowlist, whole-server disable, governance, and the harness's
existing per-tool enforcement; the client gains no say over it.

The effective client-selected set is:

```text
validated name-only client request
∩ non-control-plane selected agent declarations
∩ server enable policy
∩ tool allow/deny policy
∩ harness-supported projection and narrowing
∩ runtime launch success
```

No layer may replace an intersection with a union. The intersection is applied
again at the final composition point, after ordinary projection and pooled
broker-stub generation. For an interop slot, every non-control-plane spec entry,
pooled stub, KAS `stub_server_names` entry, Pi handoff entry, and session-array
entry is dropped unless its canonical name is admitted; a whole-server
restriction drops every representation of that name. Member session-control
mounts follow the separate control-plane rule above. This prevents a pooled stub
from reintroducing an unselected or withheld server. Diagnostics report
requested, admitted, withheld, and ready state separately.

#### Join key and launch ownership

The intersection compares canonical server name only. The current projection,
`tools` allowlist, disabled-server state, and disabled-tool state already use
that identity. The selected agent declaration owns the full launch tuple.

- A request carries exactly one field, `name`.
- A requested name with no declaration refuses the mutation as
  `undeclared_server`; no registration or provider state changes.
- A declared stdio name is projected using only the declaration's command,
  argument list, environment, and transport.
- `command`, `args`, `env`, `type`, `url`, and any other entry field are rejected
  as unknown before declaration lookup.
- A declared non-stdio server is reported as `unsupported_transport` and is not
  launched in Phase 1.
- No response distinguishes guesses about a declaration's command, arguments,
  environment, or credential values because the request contains no value to
  compare.

Under this rule the request selects among already-declared server names and
coordinates that selection across clients through `replace`,
`clear_if_registration`, and `restore_if_registration`.

## Design

### 1. Shared Gateway vocabulary

A small `kiro_crew.gateway` package owns transport-neutral constants, bounded
parsers, and typed value objects. It imports no ACP adapter package. Dashboard
handlers, `kirocrew-client-py`, and later adapters consume the same vocabulary
or a generated/public equivalent.

The package owns only wire semantics. Session state remains in the dashboard and
provider state remains in ACP/runtime owners.

### 2. Authentication and authorization

Normal dashboard token middleware runs first. A request it admits then passes
three route-family gates in this order:

1. `is_owner_dashboard_request` with an empty app claim;
2. a new `gateway_session_client_authorized` check over the validated token's
   signed mint claims and the current direct-local peer process;
3. the live `dashboard.session_interop_enabled` value.

`GET /api/token/local` remains the only mint route used by this contract. Phase 1
adds signed process-class mint provenance only after a trusted containment
attestation classifies the minting peer.

Before any Phase 1 implementation begins, a separate managed-execution
containment RFC must be accepted and its implementation must be on `main`. This
interop RFC does not choose its sandbox mechanism, process-launch architecture,
configuration keys, restart behavior, broker controls, or network policy. The
prerequisite owns those decisions, their complete `Reader:` inventory, and their
failure, performance, compatibility, and cross-platform analysis.

For use by this contract, that prerequisite must publish one authenticated,
generation-bound, fail-closed attestation with these properties:

- Its initial attesting implementation is available only on Linux.
- It covers every Gateway-managed root that can execute or relay model-directed
  work, including ACP providers, subagents, app backends, pooled MCP backends and
  tool children, gatewayd helpers, cron/task/workflow scripts, and equivalent
  future launchers.
- It positively distinguishes an ordinary host peer from every covered managed
  root and descendant at both token mint and request time. Detaching,
  reparenting, or invoking a host broker cannot turn managed work into an
  eligible host peer or proxy an eligible request.
- It publishes a complete live root set, a monotonic `root_set_revision`, and
  a stable `attestation_epoch`. A root-set change advances the revision and
  requires revalidation; it does not by itself rotate the epoch. The epoch
  changes when the containment posture is replaced, restarted, or can no longer
  vouch for prior classifications. Missing, stale, incomplete,
  unsupported-platform, or indeterminate evidence produces no eligible
  attestation.
- A managed process cannot reach the local bootstrap, these interop routes, or
  an uncontained launcher that can call them on its behalf.

Those are requirements on the prerequisite, not a mechanism decision here. A
namespace design, a virtual-machine design, or another reviewed platform
boundary may satisfy them; ancestry or cgroup membership alone does not.

No accepted prerequisite with those properties exists at the audit SHA.
Consequently, Phase 1 of this RFC cannot start until that dependency lands.
Even afterward, the interoperability feature remains default-off and requires
explicit operator enablement. macOS and Windows receive the same opaque
`client_not_authorized` refusal in Phase 1; a later platform implementation
requires its own accepted containment decision and attestation support.

The token carries signed `mint_source=local-bootstrap`,
`mint_process=host|app|agent`, `mint_attestation_epoch`, and optional
`mint_app=<name>` claims. A mint without an eligible attestation never emits
`mint_process=host`. No session key or PID is put in the token. Link-to-session
exchange copies those claims exactly and never re-derives them. Existing owner
tokens without the claims continue to authenticate every existing dashboard
route but receive `client_not_authorized` on this route family.

The authorization check admits only a local-bootstrap token with
`mint_process=host`, no app classification, and a current matching
`mint_attestation_epoch` from the external containment attestation. It asks that
prerequisite to classify
the current direct-local peer and refuses any managed root or descendant,
whether the target is its own slot or another slot. It does not reimplement the
root registry or platform boundary in this RFC.

An absent or stale generation, unresolvable peer or process incarnation,
incomplete attestation, unsupported platform, or indeterminate classification
refuses. The prerequisite must keep a detached or reparented managed process
classified as managed; how it guarantees that property belongs to its own RFC.
Browser possession of another owner token does not widen the result. This is
authorization, not a claim that `is_owner_dashboard_request` proves a human
gesture. The mint check stops a managed process from minting an eligible token;
the request check stops one from using an eligible token obtained elsewhere.

The following route-specific failures therefore receive the same opaque
`403 client_not_authorized` before enablement, target lookup, or body mutation:

- claimless `X-Internal-Secret` callers admitted to an existing internal path;
- app-claim tokens and app-minted owner tokens;
- a gateway-managed app backend or agent process tree;
- authenticated non-owner dashboard members;
- owner tokens with missing or malformed mint provenance;
- remote or forwarded callers;
- any caller whose local process provenance cannot be established.

The external containment RFC owns the exhaustive launch-surface and spawn-site
inventory, registration lifecycle, attestation publication, broker and egress
controls, and their ratchet tests. This RFC's implementation consumes only its
authenticated generation and peer classification. An absent, stale, incomplete,
or non-Linux attestation fails closed before the feature switch or target lookup.

Only after those checks pass does the route read the switch. While it is off, an
otherwise authorized caller receives `409 session_interop_disabled` before
target lookup. The switch is read per request and has no restart mark. Existing
routes that admit `internal_auth`, existing owner tokens, and the local
bootstrap's other consumers are unchanged.

### 3. Session-scoped MCP registration

#### Route

```text
POST /api/chat/slots/{slot}/mcp
```

The route accepts a bounded JSON object:

```json
{
  "mode": "replace",
  "registration_id": "client-instance-a",
  "servers": [
    {"name": "workspace-tools"}
  ],
  "mutation_id": "opaque-idempotency-key",
  "return_previous": false
}
```

`registration_id` is coordination metadata, not the authenticated principal. It
is required for every operation that can establish or clear a registration.
`name` is the sole server-entry field and the join key against the selected
agent's declarations.

#### Modes

| Mode | Effect |
|---|---|
| `replace` | Validate and replace the slot's requested names, minting a new generation |
| `clear_if_registration` | Clear only when `registration_id` matches the current registration; a mismatch is a successful no-op |
| `restore_if_registration` | Restore a previous value only when the current registration and optional generation still match the caller's expected values |

`restore_if_registration` exists for transactional client flows: a client may
switch a project or recreate a backing session, then restore its prior request
only if nobody else changed the slot in the meantime. It is not a
restart-persistent claim. The mismatch asymmetry is deliberate:
`clear_if_registration` is cleanup and a stale cleanup is a successful no-op,
while `restore_if_registration` would reinstate launch authority and therefore
returns `registration_changed` on a mismatch rather than hiding a lost race.

A bounded `mutation_id` makes transport replay idempotent while the Gateway
process lives. Reusing one ID with a different canonical request is a conflict.
Receipts are count- and age-bounded, restart-ephemeral, and must not be presented
as durable exactly-once delivery.

#### Parser and bounds

Phase 1 accepts only an array of `{name}` objects. The server rejects:

- unknown fields, including `command`, `args`, `env`, `type`, and `url`;
- duplicate names under the canonical comparison used by the target harness;
- reserved Kiro Crew server names and app-style names;
- unsupported declared transports;
- over-budget arrays, names, registration ids, mutation ids, and request bodies.

The exact constants live in one parser and are tested at their boundary. Error
messages identify the field and index but never echo declaration content or
credentials.

#### Projection and narrowing

Parsing a name does not make it effective. The Gateway obtains one
`session_mcp_projection` snapshot, intersects the requested names with that
snapshot, and then uses the selected harness's declared projection. A backend
with no safe representation for a restriction withholds the affected server
rather than mounting it broadly.

The audited baseline at `33cdb8f1ee68` is:

| Harness | `projection_for()` seam at the audit SHA | Phase 1 support and narrowing result |
|---|---|---|
| Kiro CLI | `ProjectionKind.NATIVE`; it reads the agent file itself and has no subtractive session-scoped MCP selection channel | **Unsupported for `replace` and `restore_if_registration` in Phase 1.** Those operations return `harness_unsupported` before lifecycle reservation, with registration and provider unchanged. Carrier-independent `clear_if_registration` remains available for recovery. Ordinary Kiro sessions remain supported and unchanged |
| KAS | `ProjectionKind.EXTERNAL` in `kiro_crew.acp.kas_agents`, over `_meta.kiro.customAgents` | Phase 1 adds one filtered-snapshot input to that projection and hoists only admitted names. KAS discards `disabledTools`, so any requested server carrying a per-tool restriction is withheld whole. KAS is the default; both behaviors are Phase 1 entry requirements, not follow-ups |
| Claude Code | `ClaudeCodeMirror`: `session/new` array plus workspace settings | `PerToolDeny.SETTINGS_FILE`; the deny rule reaches Claude settings and the server remains mounted |
| Codex | `CodexMirror`: `session/new` array | `PerToolDeny.PER_CALL` for the managed control plane; a restricted third-party server is withheld because read-only tools may not ask |
| OpenCode | `OpenCodeMirror`: `session/new` array plus inline resolved config | `PerToolDeny.SETTINGS_FILE`; per-tool deny rules remain in the verified asking posture |
| Pi | `ProjectionKind.BROKER_ONLY`: its sealed handoff carries existing member/broker mounts, its session array is inert, and no projection translates agent-spec servers or deny sets | **Unsupported for `replace` and `restore_if_registration` in Phase 1.** Those operations return `harness_unsupported` before lifecycle reservation. Carrier-independent `clear_if_registration` remains available for recovery. Membership in `ACP_BACKENDS_SESSION_MCP_ARRAY` and construction of the existing handoff are explicitly insufficient evidence of agent-spec support |
| Goose | `GooseMirror`: measured `session/new` stdio array | `PerToolDeny.WHOLE_SERVER`; any per-tool restriction withholds the whole server |
| DeepSeek | `DeepSeekMirror`: measured `session/new` stdio array | `PerToolDeny.WHOLE_SERVER`; any per-tool restriction withholds the whole server |

This table is an audit snapshot, not a second registry. The implementation
derives the deployment's active list from `selectable_backends()` and reads each
answer from `projection_for()`. Phase 1 tests all eight baseline rows: supported
rows must prove their carrier and narrowing, and unsupported rows must prove
side-effect-free `harness_unsupported` refusal for replace/restore plus safe
carrier-independent clear. A backend added
after this audit receives the same refusal until it has an explicit, verified
row; it never inherits another backend's behavior.

The unsupported Kiro and Pi rows are deliberate conservative outcomes, not
permanent backend policy. Kiro needs a subtractive session carrier that composes
with shared-runtime mode discovery, native skill views, launch copies, semantic
agent identity, and checkout shadow protection. Pi needs an agent-spec
projection, whole-server deny semantics, exact server-identity handling, and a
verified mount acknowledgment rather than its inert array. Those designs require
separate harness RFCs and implementation evidence. This RFC does not write
per-session Kiro aliases, widen Pi's ordinary MCP set, or infer support from an
array/set membership. A future row becomes supported only after
`projection_for()` and the harness corpus prove its carrier and fail-closed
narrowing.

For each supported interop provider, ambient harness MCP discovery is disabled
or refused before readiness. KAS projects `includeMcpJson: false` and
`includePowers: false`; the five mirror-backed rows receive generated
session-local settings containing only the final set. Per-harness tests seed
user and project MCP configuration and prove that no unselected ambient server
is callable. This rule governs callable MCP capabilities, not merely what a
harness happens to list internally.

Name normalization is evaluated before provider reuse. If a target harness folds
two names to the same wire identity, the request is rejected or deterministically
narrowed; it never gains the first or last entry by ordering. The provider reuse
fingerprint includes the ordered canonical request where order can change harness
behavior.

#### Harness identity and recreation

The registration stores the resolved per-slot ACP backend and the
`projection_for()` revision in its provider fingerprint; neither is inferred
from the current global default. After both lifecycle reservations are held, the
Gateway re-resolves the effective backend from the slot/member/session binding
and rechecks the fingerprint before building a candidate. A changed result
restarts validation from that immutable snapshot or returns
`harness_changed`; it never reuses the earlier verdict.

Every cold start, load/resume, compaction, relaunch, and recreation consults a
live registration before provider creation. If the now-resolved backend is Kiro,
Pi, newly unknown, or otherwise cannot carry the registration, provider start
fails closed as `harness_unsupported` and leaves the registration available for
`clear_if_registration`; no ordinary provider starts with the selection silently
ignored. A supported backend change may recreate only after its new projection
passes the same final intersection and narrowing checks. Tests race global,
member, and explicit per-session backend changes through this bracket.

#### Busy provider contract

A mutation never waits for an active provider lease. Phase 1 adds one
`SessionManager.try_reserve_provider_mutation(key)` owner primitive at the
provider registry's allocation boundary. In one non-blocking operation it
refuses an existing lease, allocation/start reservation, ending fence, linked
turn, compaction, or competing mutation and otherwise holds exclusive lifecycle
ownership of the folded session key through candidate start and publication. It
works for both an existing and cold provider; `try_acquire` alone is not used as
a cold-start reservation.

After that succeeds, the handler acquires `ChatSlot._turn_admission_reserved`
under the slot state lock only when `turn_running` and the reservation are both
false. If this second zero-wait reservation fails, it releases the manager
reservation and returns `409 provider_busy`. This fixed manager-then-slot order
is used everywhere. Phase 1 refuses remote, channel-linked, cron-linked, and workflow-bound slots
as `slot_kind_unsupported` rather than assuming those producers observe the
dashboard flag. The initial check and the under-reservation recheck use the same
code and leave registration/provider state unchanged; `provider_busy` remains
reserved for transient active ownership. Thus no channel turn or compaction can
acquire the old provider during the transaction.

The manager reservation canonicalizes aliases through the allocation
boundary's existing `_fold_key` rule before it checks or records ownership. All
provider reset, relaunch, discard, model-switch, compaction, and companion-runtime
paths use this one reservation owner; a CI ownership test rejects a direct
provider-registry mutation outside it. Link and mirror-link writers consult the
same token and cannot attach a slot while mutation is reserved; linked/remote
state is rechecked after both reservations are held. A live companion/subagent
runtime causes `provider_busy`; an idle reusable companion is retired, and any
successor is created from the new effective registration.

`ChatSlot._turn_admission_reserved` becomes an owned opaque reservation token,
not a shared boolean. Chat send, stop, and responsive paths compare-and-clear
only the token they acquired, so one path cannot release another's mutation
reservation. While it is held, ordinary dashboard sends continue through
`SlotQueueRepository`, whose `MAX_LIVE_QUEUE_ENTRIES` bound and durable ordering
remain unchanged. The provider is owned and atomically replaced in
`SessionManager`; the `ChatSlot` holds only slot registration metadata and its
queue. Replacement does not detach, copy, clear, or restore that queue and does
not call the unrelated folded-messaging `SessionManager.detach_queue` /
`restore_queue` primitives. Provider startup is bounded by the configured
session-start timeout; both reservations are released in all outcomes, and
neither acquisition waits. After release, every outcome rechecks the slot under
its lock and invokes one canonical idle-queue kick (factored from
`drain_idle_parent_queue_for_stall`) when queued work exists and no turn or
admission token owns it. Thus a send accepted during mutation starts on the new
or retained provider without waiting for another user send.

#### Remote slots

A remote-crew slot is refused in Phase 1. The local Gateway does not own that
slot's provider lifecycle, process environment, or authorization ceiling.
Remote support requires a peer-owned equivalent contract and a separate RFC.

### 4. Stateless question-card API

#### Routes

```text
GET  /api/chat/slots/{slot}/questions
POST /api/chat/slots/{slot}/questions/{card_id}/answer
```

The read returns only stateless cards carrying `card_id`. Blocking cards carrying
`ask_id` continue through the existing blocking tool lifecycle and never appear
in this list or reach this answer route.

This section does not amend
[rfc-question-card-pager.md](rfc-question-card-pager.md): paging, labelled answer
serialization, and its accepted section 3.6 blocking-return contract remain
unchanged. It also does not amend
[rfc-question-card-auto-submit.md](rfc-question-card-auto-submit.md): the
dashboard's completing-click gesture is unchanged. The new API is a second
transport into the same server-owned validation and pending-card state for
stateless cards, not a second card lifecycle.

Phase 1 mints a stable ID for every question when a stateless card is published.
It also adds `origin_turn_generation` to each native stateless record, captured
from `ChatSlot._turn_generation` while `InteractionCoordinator.post_card` /
`mark_pending` publishes it; this field does not exist at the audit SHA. The
answer body is a non-empty bounded map from question IDs to a string or list of
strings. Question text is display data, never the mutation key. A legacy
stateless card lacking stable question IDs, or a native card lacking the origin
turn generation, remains usable through the existing dashboard path but this
route refuses it with `question_identity_unavailable`; there is no text-keyed or
current-turn fallback.

The Gateway validates the entire answer, slot, card identity, question IDs,
optional turn origin, and registration freshness before mutating the card.

#### Delivery rules

- A stateless card with `native: true` belongs to the
  `origin_turn_generation` captured when the card was published. Its completed
  answer may be steered into that same still-running turn through one atomic
  claim. If the generation is absent or no longer matches, the route returns
  `question_identity_unavailable` or `slot_busy`, respectively; it does not
  start a new turn.
- A stateless card with `native: false` becomes an ordinary user message. If a
  turn, admission reservation, or subagent hold exists, it enters the ordinary
  bounded queue rather than starting a second turn.
- An incomplete multi-question answer updates no turn and leaves the card
  available.
- A delivery failure releases the atomic answer claim so the owner can retry.
- A concurrent second completed answer receives a coded conflict and cannot
  start another turn.

A missing card remains distinct from malformed input and from a busy slot only
after authorization and enablement succeed. The client clears its UI only after
the Gateway confirms mutation or reports that the card no longer exists.

### 5. Turn-origin correlation

The correlation field is a new protected surface on the existing `/api/chat`
route. Its presence invokes the same ordering as the new routes: normal token
middleware, then owner/mint/current-peer provenance authorization, then the live
feature switch, all before chat admission. An authorization failure returns the
same opaque `403 client_not_authorized`; an otherwise authorized request while
the switch is off returns `409 session_interop_disabled`. A request without the
field retains ordinary `/api/chat` behavior. After those checks pass, an owner
client may attach bounded correlation metadata. The server records it under a
reserved namespace that ordinary prompt content and other client-supplied
metadata cannot overwrite.

The value is reflected in:

- the SSE stream for that admitted request;
- finalized `session_message` events for the resulting user and assistant rows;
- queued successors only when they were admitted as part of the same client
  request.

The value is not a permission, a slot selector, or an MCP registration id. A
non-owner caller presenting the field receives `client_not_authorized`; the
server never silently strips it and admits the chat.

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
Only existing local slot keys are eligible. An ineligible or unknown key is
omitted, and the server replies with the fixed frame
`{"type":"subscriptions_changed"}` — no echoed keys, counts, or per-key reasons.
The first event for a key is the only positive evidence that it was accepted.

Established sockets remain authorized only while their opening token, peer
process incarnation, feature switch, and external containment
`attestation_epoch` remain valid. Connections are registered centrally with
token expiry, the captured epoch, and the observed `root_set_revision`. A switch
transition to false, token expiry, peer revalidation failure, or epoch change
immediately drops all keys and closes the socket. A root-set revision change
triggers peer revalidation against the new root set and closes only on refusal;
it does not revoke an otherwise eligible socket merely because a managed child
started or stopped. Each publication rechecks the epoch, root-set revision, peer,
and switch before serializing, so watcher delay cannot leak another event. An
absent, stale, or incomplete attestation closes existing connections as well as
refusing new ones. Tests cover switch-off, expiry, peer loss/PID reuse, root-set
change, and epoch rotation with an already subscribed socket.

`session_message` and `session_plan` are new external event names, not names
already emitted at the audit SHA. Their producers are adapters over the existing
finalized `_ChatSlot.append` path in `dashboard/state_parts/slot_transcript.py`
and complete `_ChatSlot.set_todo` snapshots in
`dashboard/state_parts/slot_todo.py`; they do not introduce a second transcript
or plan store.

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

It does not become a durable grant and does not survive Gateway restart as an
authoritative registration. A client reconnects, observes no live registration,
and establishes a new generation.

Provider identity includes the effective request state. A change that could alter
the launched MCP array cannot reuse a provider created for the old state.
Replacement follows this order after the zero-wait reservation in section 3:

1. authenticate, authorize, parse, and validate the new name-only request;
2. reject unsupported linked/remote slot kinds, then non-blockingly reserve the
   folded provider key through `SessionManager.try_reserve_provider_mutation`;
3. non-blockingly reserve the owned `ChatSlot` admission token; on failure,
   release the manager reservation and return `provider_busy` unchanged;
4. recheck the live feature switch, containment epoch and current peer against
   the latest root-set revision, linked/remote and companion-runtime state, and
   the resolved per-slot harness/projection fingerprint under both reservations,
   then leave the
   `ChatSlot` and its `SlotQueueRepository` untouched while ordinary dashboard
   sends append under the existing queue bound;
5. build and start the candidate provider under the configured session-start
   timeout while the manager reservation excludes turns, starts, compaction,
   ending, and competing mutation;
6. immediately before commit, recheck the live switch, containment epoch,
   root-set revision and peer under both reservations. On refusal, stop the
   candidate and leave the old provider/registration unchanged; otherwise
   atomically replace the provider in `SessionManager`, then publish the matching
   effective registration/generation on the slot;
7. release both reservations in all outcomes and run the canonical idle-queue
   kick exactly once when accepted work is parked;
8. publish requested/admitted/withheld/ready diagnostics separately.

Failure before the new provider is ready either keeps the old provider and
registration or leaves a coded, recoverable no-provider state. It never reports
success while silently running the old MCP set, and it never leaves a new
registration active on the old provider.

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
  obtains its owner token only through the provenance-authorized local bootstrap;
- contains no duplicate provider, queue, or MCP admission logic.

ACP is the first expected consumer, but ACP names and schemas stay outside the
Gateway package and outside this RFC's implementation phases.

### 9. Error contract

Every refusal has a stable machine code and a human-readable message. The
contract distinguishes:

- existing middleware authentication failures, unchanged;
- the opaque `client_not_authorized` response for every route-specific owner,
  local-mint, locality, app-lineage, agent-lineage, and provenance failure;
- `session_interop_disabled` while the rollout switch is off;
- slot not found, only after authorization and enablement pass;
- malformed or over-budget request;
- unsupported transport, `harness_unsupported`, `harness_changed`, or
  `slot_kind_unsupported` for a remote, channel-linked, cron-linked, or
  workflow-bound slot, including when discovered by the under-reservation
  recheck;
- undeclared server and stale registration or generation;
- mutation replay conflict;
- question not found, `question_identity_unavailable`, invalid answer, answer in
  progress, and slot busy;
- `provider_busy` with registration unchanged;
- provider replacement failure;
- unsupported event capability or malformed subscription;
- `audit_unavailable` when the pre-mutation SEL record cannot be committed.

Client-visible authorization failures do not reveal which underlying check
failed or whether the named slot, card, registration, or subscription exists.
Validation errors never include environment values or other declaration content.

## Security considerations

### Authentication does not prove authorization

The existing owner token authenticates a subject. `is_owner_dashboard_request`
checks that subject and refuses app claims; it does not establish that a local
request came from a human gesture or from a process outside the Gateway's own
agent and app trees.

The new surfaces therefore require all of the following: owner authentication,
signed eligible local-bootstrap mint provenance, a direct-local request peer,
and a current positive host-peer classification from the separately accepted
managed-execution containment prerequisite. That prerequisite must cover pooled
MCP backends and tool children and remain valid across detachment or reparenting;
a current ancestry walk alone is insufficient. The mint and request checks are
both required. The mint check stops an app or agent from minting a token and
handing it off; the request check stops one from using an eligible token obtained
elsewhere. Unknown, stale, incomplete, non-Linux, or unavailable attestation
fails closed.

This RFC does not select or approve the mechanism that establishes that
classification. Its Phase 1 is blocked until the separate containment RFC is
accepted and implemented.

The token remains the existing HMAC-signed dashboard credential. The added
claims authorize only this route family and do not create an identity provider,
new carrier, or new mint route. A claimless legacy token retains all existing
behavior and gains none of these capabilities.

### Internal secret is not owner identity

The Gateway gives its internal transport secret to trusted host components, and
some gateway-spawned contexts can present it back to exact internal routes. That
proves local transport, not owner identity or eligible process provenance. The
new surfaces have no internal-secret exemption. Existing internal routes keep
their current behavior.

### MCP command execution

A client request carries a server name only. The selected agent declaration
supplies the command, arguments, environment, and transport after the name is
intersected with enable policy, tool restrictions, harness projection, and
runtime checks. Client input is never compared with or copied into that tuple.
Name-only requests cannot run an undeclared binary, alter a declared server's
environment, or probe a secret declaration value through a mismatch response.

Client input must never be copied into a provider-global config file. Supported
harnesses keep the filtered request session-scoped in memory or in generated
session-local settings. Kiro and Pi are refused precisely because Phase 1 has no
verified subtractive carrier for them. Declaration values are excluded from
logs, events, errors, and request diagnostics.

### Mutation audit

Each `mcp.replace`, `mcp.clear`, `mcp.restore`, `question.answer`, and
`session_events.subscribe` attempt produces an SEL record. After validation and
before a side effect, the handler writes an audit-or-deny `invoked` record through
`sel().log_tool_invocation(..., critical=True)` or the canonical equivalent; if
that write fails, it returns `audit_unavailable` and commits nothing. A terminal
record then reports success, no-op, busy, or error without reversing the first
record's authorization evidence.

Records carry the owner subject, signed mint source, app/agent mint
classification, current peer process class, operation, opaque slot digest,
generation where applicable, and outcome. They never carry token bytes, card
text, answers, server commands, arguments, environment values, or declaration
content. A refusal records the specific server-side authorization reason while
the client receives only `client_not_authorized`.

### Event confidentiality

The dedicated socket is owner-only, provenance-authorized, key-scoped,
count-bounded, default-off, and redacted. It has no owner-wide default
subscription. Subscriptions are held per connection and released on close.
Message, title, and plan publishers each apply their own allowlist and redaction
rather than forwarding internal row dictionaries.

### Replay and persistence

Mutation receipts prevent benign transport replay but are bounded and ephemeral.
They are not a security nonce. Registration and generation checks prevent one
cooperating client from clearing or restoring another client's current
configuration; authentication and process authorization remain the security
boundary.

Turn-origin metadata is inert. Persistence must not make it an admission token.

### Denial of service

Request bodies, server arrays, receipts, subscriptions, and event payloads are
bounded. Provider replacement is serialized per slot and immediately refuses an active
lease instead of waiting behind a long or stuck turn. The bounded slot queue is
never copied into the mutation transaction. Event publication
uses the existing backpressure and dead-socket cleanup path.

## Backward compatibility

Compatible by default because `dashboard.session_interop_enabled` is a new
boolean whose default is `false`. The routes remain registered unconditionally.
After normal authentication and route-specific provenance authorization, they
return `409 session_interop_disabled` before target lookup. The dedicated socket
refuses its capability during upgrade under the same rule. The key is read live
per request and carries no `restart=True` mark, so changing it cannot leave the
router and configuration disagreeing.

Existing dashboard routes, question cards, ordinary `/api/ws`, `slot_patch`, app
isolation, queue behavior, provider-global MCP configuration, token carriers,
and local token consumers keep their current semantics. Existing owner tokens
without local-mint provenance continue to authenticate those surfaces and are
ineligible only for the new route family.

The switch remains off by default through Phase 2. Phase 3 may propose changing
the default only in the first consumer's own compatibility review, with its
rollback and audience stated. Merely installing the server or typed client never
enables it.

Known limitation: Phase 1 is usable only on Linux after the separate containment
prerequisite has landed, reports an eligible attestation, and the operator has
explicitly enabled this feature. A default installation has no usable consumer.
macOS and Windows always receive
the opaque authorization refusal for these new capabilities in Phase 1; all
existing dashboard and agent behavior on those platforms remains unchanged.

## Migration plan

Each phase is independently shippable and independently abandonable. No phase
begins until this RFC is accepted and merged to `main`.

### Phase 0 — decision record

Land this RFC alone as `draft`. A maintainer records answers to the remaining
open questions and changes `status` to `accepted` in a separate decision PR.

**Exit criteria**

- A maintainer has recorded acceptance in the RFC.
- A separately reviewed managed-execution containment RFC is linked and accepted;
  its implementation must land before Phase 1 begins. Acceptance of this RFC does
  not imply acceptance of that platform/security decision.
- Correlation carrier, registration spelling, title events, restore mode,
  restart posture, and first consumer have explicit answers.
- The accepted contract includes name-only MCP requests, the opaque
  authorization response, the app/agent process-lineage refusal, immediate
  `provider_busy`, the default-off switch, and the mutation audit vocabulary.
- No implementation code is in the decision PR.

### Phase 1 — Gateway server contract

Only after the external containment prerequisite is implemented, a team-owned
PR adds the neutral vocabulary, bounded parser, mint-provenance claims,
attestation-backed authorization, default-off routes, transactional slot state,
provider-lifecycle integration, mutation audit, and dedicated event socket. It
updates the owning MCP, session, dashboard-auth, security, SEL, configuration,
and feature-map documents in the same commit.

**Exit criteria**

- Every new route and socket authenticates, authorizes owner and process
  provenance, then returns `session_interop_disabled` while the switch is off
  without inspecting a slot. The switch is live-read and has no restart mark.
- Phase 1 starts only after the separately accepted containment RFC is
  implemented on `main`. On Linux only, a token minted and presented by an
  attested ordinary host process can use the routes after explicit enablement.
  Missing, stale, incomplete, indeterminate, or non-Linux attestations fail closed
  as `client_not_authorized` before feature enablement or target lookup.
- App-claim tokens, app-minted owner tokens, agent-minted owner tokens,
  gateway-managed app peers, gateway-managed agent peers, remote peers,
  claimless internal callers, and unresolvable provenance all receive the same
  `client_not_authorized` response before target lookup.
- Link-to-session exchange preserves the signed mint claims exactly. Existing
  claimless owner tokens retain existing route access and gain no new access.
- Client MCP entries accept `name` only. Every launch value comes from the one
  `session_mcp_projection` snapshot; `command`, `args`, `env`, `type`, and `url`
  are unknown fields and no equality response exists.
- Tests cover all eight rows in the harness table and the post-projection final
  composition for supported rows, including pooled stubs and seeded ambient
  user/project MCP files. KAS uses only its filtered custom-agent snapshot.
  Kiro and Pi prove side-effect-free `harness_unsupported` refusal for
  replace/restore before any provider reservation while carrier-independent clear
  remains available; Pi's inert array and existing broker handoff do not count as
  agent-spec support. Per-tool restrictions are preserved or withhold every
  representation of the server. A newly selectable backend without a complete
  `projection_for()` answer fails closed the same way.
- `replace`, `clear`, and `restore` reserve the canonical `SessionManager` key
  and then the owned dashboard slot token without waiting; a provider lease,
  initialization, ending, compaction, linked turn, or mutation returns
  `provider_busy` with all state byte-for-byte unchanged. Linked and remote slot
  kinds are refused. A backend change is rechecked under both reservations and
  no recreation starts an unsupported harness with a live registration.
- Replace/clear/restore, the deliberate clear/restore mismatch asymmetry,
  generation, receipt replay/conflict, untouched bounded slot queue,
  provider-start failure, restart loss, and remote-slot refusal are covered.
  Tests rotate the switch, attestation epoch, and root-set revision during a
  candidate start and prove the under-reservation pre-commit recheck leaves the
  old provider and registration unchanged on refusal.
- A `/api/chat` request carrying turn-origin correlation receives the same
  opaque authorization-then-enablement errors as the new routes; a request
  without the field is unchanged. Tests prove the reserved namespace cannot be
  overwritten, accepted SSE/final rows correlate, and queued successors not
  admitted by that request do not inherit it.
- Stateless question publication mints stable per-question IDs and records the
  new native `origin_turn_generation`. Answers cover matching native turns,
  stale or identity-less native turns, admission reservations, subagent holds,
  ordinary queueing, incomplete answers, concurrent answers, failed delivery,
  legacy identity refusal, and exclusion of blocking `ask_id` cards.
- The question routes reuse the canonical answer validator without changing the
  pager or auto-submit contracts.
- Dedicated sockets receive only explicit redacted events for bounded keys and
  cannot subscribe to logs or `slot_patch`; switch-off, token expiry, peer or
  attestation failure, or `attestation_epoch` change closes an existing socket
  before another event. A `root_set_revision` change revalidates the peer and
  closes only on refusal.
- Every mutation and subscription change writes the required provenance-bearing
  SEL record before its side effect; a forced SEL failure commits nothing.
- Every new error code is covered by `test_error_code_contract.py` and the
  shrink-only error-code baseline.
- This RFC's tests consume the prerequisite only through its authenticated
  generation and peer-classification seam: eligible Linux host, managed peer,
  detached/reparented managed peer, stale generation, incomplete evidence, and
  unsupported platform. The containment RFC and its implementation own exhaustive
  launch and broker inventories, mechanism tests, `Reader:` coverage, performance,
  compatibility, and cross-platform evidence.
- Lifecycle tests cover canonical alias folding, owned slot tokens, every
  reset/relaunch/discard/model-switch/compaction path, effective-harness races,
  concurrent linking, companion runtimes, and a queued-during-mutation turn that
  drains immediately after release. All CI and security/design lanes pass.

### Phase 2 — extend `kirocrew-client-py`

A separate PR adds typed methods and event objects to the existing client. It
adds an owner-token bootstrap helper that reuses `GET /api/token/local` and the
same local-secret resolution as `kirocrew token`; it adds no route, carrier, or
weaker fallback. The feature remains default-off.

**Exit criteria**

- Every new method is checked against the real router and wire response shape.
- The bootstrap helper surfaces `client_not_authorized` and
  `session_interop_disabled` without falling back to `X-Internal-Secret`.
- Authentication refresh does not replay an already-applied mutation or turn.
- SSE correlation, WebSocket reconnect/resubscribe, error-code mapping, path
  escaping, and malformed-frame handling are covered.
- Existing client methods and supported Python versions remain green.

### Phase 3 — first protocol adapter

A team-owned adapter PR consumes only the typed client and the accepted Gateway
contract. ACP is a candidate, not a requirement of Phases 1 or 2. Any proposal
to enable the route family by default belongs in this PR's compatibility review,
not in the server or client phases.

**Exit criteria**

- The adapter contains no duplicate auth, queue, provider-replacement, MCP grant,
  question-card, or audit logic.
- Adapter protocol conformance is tested separately from Gateway behavior.
- Session create/load/resume, turn streaming, cancellation, questions, events,
  MCP request restoration, disabled-server behavior, and `provider_busy` retry
  have closed-box coverage.
- Removing or disabling the adapter leaves the Gateway and Python client complete
  and useful.
- The route family remains default-off unless a maintainer separately accepts the
  consumer's enablement and rollback plan.

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

### Use the owner predicate or internal secret as the whole authorization

Rejected. The internal secret proves transport locality, and the owner predicate
proves the signed subject. Neither says whether a gateway-managed agent or app
process is making the request. The separate mint and current-peer provenance
checks are required to refuse self-action.

### Let app or agent processes mutate other slots but not their own

Rejected for Phase 1. A target-relative check requires looking up the target
before authorization, creates a slot-existence oracle, and makes a transferred
credential's authority depend on mutable ownership metadata. Refusing all
managed app and agent process trees is smaller and fail-closed.

### Give clients a direct MCP grant API

Rejected. It inverts the trust model. A client may request an already-declared
server by name; it cannot declare permission on behalf of an agent, profile,
governance policy, or harness, and it cannot substitute the executable,
arguments, or environment the declaration names.

### Let a matching name carry or compare the client's launch tuple

Rejected. Launching a client tuple is a direct grant. Comparing it instead is
also unsafe: a caller can vary an environment value and use
`declaration_mismatch` as an equality oracle over a declaration that may contain
credentials. Phase 1 accepts the canonical name only and obtains the complete
tuple from the selected agent declaration.

### Wait for provider leases to quiesce

Rejected. A turn or stuck tool call can hold the lease for an unbounded period,
and a second replacement can queue behind the first. Phase 1 makes the attempt
non-blocking and returns `provider_busy` without changing state.

### Enable the routes when the server lands

Rejected. The first two phases have no protocol consumer that needs the surface.
A default-off switch makes the server and client independently shippable and
keeps enablement with the consumer that can demonstrate its rollback.

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
- replacing a provider must preserve the `ChatSlot` queue in place rather than
  transferring it through provider state;
- an active provider lease cannot be waited on without a bound;
- a question card needs an atomic completed-answer claim;
- request-local context must be cleared before a queued successor starts;
- a dedicated event socket must be excluded from owner-wide and `slot_patch`
  fan-out;
- subscription key sets need a hard bound;
- message events cannot depend on an unrelated SSE-reader predicate;
- claimless internal-secret authorization is too broad;
- an owner token alone does not distinguish an ordinary local client from a
  gateway-managed agent or app process;
- declaration tuple comparison creates an equality oracle over environment
  values;
- harness projection and per-tool narrowing differ and must be tested by named
  seam rather than by a generic parity statement;
- security-sensitive mutations need an audit record and a rollout switch.

Those findings support the invariants above. They do not make the prototype's
exact code or wire spelling accepted.

## Open questions

1. **Correlation carrier.** Should turn origin be an HTTP header, a reserved
   request object, or a server-minted ID returned by the chat admission receipt?
   Every choice must include an explicit request-side opt-in that triggers the
   section 5 authorization and enablement order before chat admission.
2. **Registration spelling.** Is `registration_id` sufficiently distinct from
   the authenticated owner, or should the coordination field be named
   `client_registration_id`?
3. **Restart posture.** Should a session MCP request remain restart-ephemeral, or
   be persisted as non-authoritative configuration requiring fresh adoption?
4. **Title events.** Does the minimal event stream include redacted `slot_title`,
   or only message and full-plan snapshots?
5. **Conditional restore.** Does Phase 1 need `restore_if_registration`, or can
   a client always issue a fresh `replace` after a failed project/session
   transition?
6. **First consumer.** Should the first adapter be ACP, or should Phase 1 and 2
   prove reuse with a smaller closed-box client first?

## Recommendation

Accept the protocol-neutral contract with the conservative boundaries specified
above: eligible local-bootstrap owner tokens only; app and agent process trees
refused at mint and request time; name-only MCP selection; no-wait
`provider_busy`; explicit per-harness fail-closed projection; provenance-bearing
mutation audit; and a default-off rollout through Phase 2. Treat the separately
accepted and implemented managed-execution containment RFC as a hard Phase 1
prerequisite, not as a decision embedded here. Phase 1 is Linux-only and has no
usable default-install consumer; macOS and Windows refuse these capabilities.
Resolve the six remaining product-shape questions before changing the RFC status
from `draft`.
