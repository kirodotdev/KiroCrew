---
title: A2A Remote Subagents — remote agents as first-class subagents over A2A
status: draft
author: jonmcox-aws
created: 2026-09-11
last-audited: 2026-09-11
audited-at: 707b8aef2
doc-pr: 10162
implementation-prs: [10171]
tracking-issues: [10161]
supersedes: []
superseded-by: []
---
# RFC: A2A Remote Subagents

- Status: draft. A reference implementation is open as
  [#10171](https://github.com/kirodotdev/KiroCrew/pull/10171) alongside this
  document. Nothing is on main. The PR is evidence for this proposal, not a
  request to merge it before this RFC is accepted; its security section is
  being revised against that implementation (see Open questions).
- Author: jonmcox-aws
- Related: [rfc-resumable-subagent-sessions.md](rfc-resumable-subagent-sessions.md)
  (continuable conversations are the substrate this builds on),
  [rfc-pluggable-model-providers.md](rfc-pluggable-model-providers.md) and
  #1693 (adjacent but distinct — see *Relationship to the provider rule*),
  [`../system-specs/oss-fork-boundaries.md`](../system-specs/oss-fork-boundaries.md).

## Summary

Let a session's primary agent delegate to a **remote agent** as an ordinary
subagent. The remote agent speaks [A2A](https://a2a-protocol.org) (Agent2Agent
v1.0); Kiro Crew consumes it through a new `A2AProvider` plugged into the same
provider-creation seam every subagent run already passes through. Remote agents
live in their own registry (`a2a_agents` in config) and **membership in that
registry is the whole semantics**: spawnable via `spawn_run` /
`spawn_continue` / `spawn_steer`, never selectable as a session's primary agent.
The primary path stays KiroACP-only.

The user-visible change is small: a name in `a2a_agents` appears on the
subagent board like any local worker, streams, completes, and can be continued
days later. The one visible difference is that live mid-turn interrupt returns
the existing typed `steer_unsupported`.

A remote agent is an operator-configured **trust boundary**, not another local
process: only the redacted task text and a fixed delegation preamble cross it
(never memory, lessons, steering, the skills index or the system prompt), and
off-box delegation is a distinct, policy-deniable capability
(`capabilities.remote_spawn`) under the admin ceiling. *Security
considerations* states the boundary against the implementation.

## Motivation

### Current state (verified at `707b8aef2`)

- Every subagent is a local kiro-cli process reached over ACP.
  `subagent_manager/run.py::_run_inner_impl` obtains the worker with
  `self._manager._sessions.get_or_create(session_key, agent=…)` and then
  consumes a provider event stream (`EVENT_TEXT_CHUNK`, `EVENT_TOOL_CALL`,
  `EVENT_PERMISSION_REQUEST`, `EVENT_COMPLETE` from `providers/base.py`).
  Everything after that call — the board, completion events, `spawn_continue`,
  the follow-up watcher, tombstones — is provider-agnostic.
- `providers/base.py::LLMProvider` has six abstract methods (`start`,
  `shutdown`, `stream`, `approve_tool`, `reject_tool`, `context_usage_pct`).
  Its defaults already describe a remote worker: `supports_steer=False`,
  `is_session_sharing_eligible=False`, `runtime_info()=(None, None)`,
  `session_id=''`, `billing_stats=None`, `is_alive=True`.
- Heterogeneous providers already flow through this path: `run.py` calls
  `_is_cc_provider(client)` and skips the permission loop for Claude Code. A
  third provider type is a pattern, not an exception.
- Four fail-closed gates decide whether a name may be spawned:
  `subagent.py::_validate_agent` (refuses names not in `list_agents()`), the
  `visible_agent_names` roster hint, the provider factory, and the
  session-sharing / lifecycle decisions in `run.py` made before a provider
  exists. None of them has an app hook. **This cannot be a Kiro Crew app.**
- `agent.provider` is fixed to `acp` and
  [oss-fork-boundaries](../system-specs/oss-fork-boundaries.md) lists "Other
  providers" under *Never re-add*.

### Problems

1. **Capability lives on each machine.** When a team improves an agent
   (skills, runbooks, memory), every user must pull it locally. The fleet of
   local copies drifts; the engineer paged at 2am runs last month's playbook.
2. **No team-level agent.** A centrally operated agent — one deployment, one
   memory, one place to measure quality — has no way into a Kiro Crew session.
   Users leave Kiro Crew for that agent's own UI, or rebuild locally.
3. **Standard exists, unused.** A2A is the open protocol for agent-to-agent
   delegation (Linux Foundation; server support in Amazon Bedrock AgentCore
   Runtime since Nov 2025; 150+ organizations). Kiro Crew has no client.

### Why not an MCP wrapper

An MCP tool that calls the remote agent works today and is the wrong shape:
the call is synchronous inside a tool turn, it does not stream to the board,
it is not a conversation the user can continue, it cannot be batched with
local workers in one `spawn_run`, and it is invisible to the subagent
lifecycle (tombstones, TTL, `spawn_status`). The point is that a remote agent
is a *teammate*, and teammates in Kiro Crew are subagents.

## Goals

- A remote A2A agent is spawnable by name from `spawn_run` and appears on the
  subagent board with streaming output and a normal completion event.
- `spawn_continue` on a finished remote run resumes the same conversation with
  state retained, including across a gateway restart.
- `spawn_steer mode=follow_up` works; `mode=interrupt` returns the existing
  typed `steer_unsupported` without touching the wire.
- Local and remote tasks mix freely in one `spawn_run` batch.
- Remote agents are never offered as a session's primary agent.
- A remote run that loses its connection mid-turn **fails**, with partial
  output preserved; it never reports an empty success.
- Generic A2A in core; authentication schemes pluggable through the existing
  provider-registry edition seam so no organisation-specific auth lands in
  the public repo.

## Non-goals

- Changing `agent.provider` or the primary-agent path in any way.
- Tool-call or permission-request events from remote agents (A2A has no
  equivalent of ACP's permission round-trip; the remote agent owns its own
  approvals). The provider emits text and completion only.
- Live mid-turn interrupt over A2A. `CancelTask` exists in the protocol as a
  best-effort *request*, and is used only on the reap/timeout path.
- Push notifications (`capabilities.pushNotifications`). Streaming covers the
  need; polling `GetTask` is the fallback for a card without streaming.
- An A2A **server** in Kiro Crew (exposing Kiro Crew's own agents to others).
  Interesting, separate RFC.
- Any UI beyond what the board already renders for a subagent.

## Design

### The one mental model: the context is the conversation

A2A tasks are immutable once they reach a terminal state; a completed task
cannot receive new messages. Continuation happens at the *conversation*
level: the server groups related tasks under a shared `contextId`, and a
follow-up is a **new task in that context** carrying `referenceTaskIds`.

Kiro Crew therefore maps:

| Kiro Crew | A2A |
|---|---|
| subagent conversation (the thing `spawn_continue` targets) | `contextId` |
| one turn of that conversation | one task (`taskId`) |
| `spawn_continue` | `SendStreamingMessage{contextId, referenceTaskIds:[last]}` → new task |
| `spawn_steer mode=follow_up` | queued by the existing follow-up watcher; delivered as the next task in the context |
| turn complete (`EVENT_COMPLETE`) | terminal `TaskStatusUpdateEvent` (`COMPLETED`/`FAILED`/`CANCELED`/`REJECTED`) |

The provider keeps two pieces of state: `context_id` (durable, persisted to
the run's `state.json` after the first turn) and the current `task_id`
(in-flight turn; the `referenceTaskIds` anchor for the next). Task boundaries
are invisible to the user. Kiro Crew's existing one-in-flight-turn-per-
conversation serialization is exactly the client-side discipline the A2A spec
asks for.

### Registry: `a2a_agents`

```json
"a2a_agents": [
  { "name": "investigator",
    "agent_card_url": "https://<host>/.well-known/agent-card.json",
    "auth": { "scheme": "bearer", "token_env": "INVESTIGATOR_TOKEN" } }
]
```

- Loader (`config/sections.py`, `config/loader.py`) validates the shape.
  `auth` is a typed object (`A2aAuthConfig`): `scheme` is `none` or `bearer`,
  and `token_env` NAMES the environment variable holding the credential — the
  credential value is never in config. Only the object spelling is accepted.
- A name that also names a local agent is refused **at spawn time**, with the
  typed code `agent_name_collision`, in `_validate_agent` — the one place the
  local roster is in hand. The subagent path fails closed; nothing is refused
  at config load.
- Membership is the semantics. There is no `kind` and no `role`: every local
  agent can be primary or subagent; every remote agent can only be a
  subagent. The primary picker reads `~/.kiro/agents/` only, so remote agents
  are structurally absent from it without any filtering code.
- `auth.scheme` resolves through a scheme table in the SDK driver
  (`agent_sdk/drivers/a2a.py`), outside the ACP layer. Core ships `none` and
  `bearer`; an edition registers its own schemes (SigV4, an SSO token) against
  that table, so nothing organisation-specific enters core. Whether that table
  should instead hang off the platform-context seam (`current_context()`) is
  open question 6.

### `A2AProvider` (`providers/a2a.py`)

Implements `LLMProvider`. On `start`: fetch the agent card (once per run,
cached), require `capabilities.streaming` for the streaming path. On
`stream(prompt)`: `POST SendStreamingMessage` with header `A2A-Version: 1.0`,
including `contextId` + `referenceTaskIds` when continuing; consume SSE:

- first `Task` frame → adopt `taskId` and (on first turn) `contextId`;
- `TaskArtifactUpdateEvent` → `EVENT_TEXT_CHUNK` per delta;
- terminal `TaskStatusUpdateEvent` in `COMPLETED` → `EVENT_COMPLETE`. A
  `FAILED` or `REJECTED` terminal state, a JSON-RPC error, or an
  `AUTH_REQUIRED` state **raises** `A2AStreamError` carrying the server's
  status message, so the run is recorded as failed with the remote's reason
  and its partial output — never as a success (see *Failure discipline*).

**Failure discipline.** A stream that raises mid-turn, or ends without a
terminal state, **raises `A2AStreamError`** (structural `transient=False`, so
`acp_error_is_transient` will not re-send the task to a dead server). This
matters because `run.py` builds the run's result only from text chunks and
reads `EVENT_COMPLETE` only for billing; an error carried in the completion
event is silently dropped and the board shows ✅ *No response*. The branch
found this the hard way (see *Evidence*).

`supports_steer=False` (ABC default) gives the typed interrupt rejection.
`is_session_sharing_eligible=False` keeps remote runs out of the shared
runtime. No pid: the memory guard and `/proc` reaper already tolerate an
unmeasurable worker (`None`).

### Seams touched in core

Sizes are added lines on the reference branch at `bd9e83aac`.

| # | File | Change | Size |
|---|---|---|---|
| 1 | `config/sections.py`, `config/loader.py` | `a2a_agents` section, `A2aAuthConfig`, accessors, `validate_a2a_collisions` | ~160 |
| 2 | `providers/a2a.py`, `providers/base.py` | `A2AProvider`, `A2AStreamError`, SSE mapping, origin pin, TLS-only credentials, card security-requirement check, cancel; `provider_label` on the ABC | ~640 |
| 3 | `agent_sdk/drivers/a2a.py` | scheme table (`none`, `bearer`), `register_auth_scheme`, provider construction | ~140 |
| 4 | `subagent.py` | `_a2a_agent_entry`, `_validate_agent(remote=)` with collision refusal, `_vet_spawn_governance(remote=)` remote gate, `build_remote_task_message` (the egress boundary), roster union | ~200 |
| 5 | `subagent_manager/admission.py` | classify local-vs-remote **once** and stash the entry on the run record; both gates consume that decision | ~20 |
| 6 | `subagent_manager/run.py`, `terminal.py` | branch on the stashed entry at the provider seam; remote message builder instead of `build_message`; exclude from session sharing; persist `contextId` post-turn and on every release; CancelTask + shutdown on stop/reap | ~220 |
| 7 | `subagent_manager/continuation.py` | rebuild provider from stored label + `contextId`; inherit recorded agent; `steer_unsupported` for direct providers | ~25 |
| 8 | `platform/governance.py` | `capabilities.remote_spawn` catalog row (opt-in, `agents` ruleset) | ~15 |
| 9 | `mcp_tools/spawn.py`, `acp/types.py` | provider label routing, `PROVIDER_LABEL_A2A` | ~30 |
| 10 | `testing/fake_a2a_server.py`, `testing/fake_acp_backend.py` | loopback A2A fixture (fault switches, `--require-bearer-env`); opt-in delegation directives in the fake ACP backend | ~650 |
| 11 | tests | `test_a2a_provider.py` (43), `test_fake_a2a_server.py` (15), `test_fake_acp_delegation.py` (15), `TestRemoteSpawnGate` (7), headless E2E (2) | ~1,650 |

Placement decision: the branch is at the `get_or_create` **call site** in
`run.py`, keeping the ACP provider factory pure. The alternative (branch in
`build_provider_factory`) is a smaller diff but that factory also serves
primary, dashboard and cron sessions and would need a subagent-shaped-key
guard. Call-site placement makes "subagent-only" a property of *where* the
code is, not of a check.

### Relationship to the provider rule

[oss-fork-boundaries](../system-specs/oss-fork-boundaries.md) says: "Kiro
Crew is KiroACP-only: `agent.provider` is fixed to `acp` … a second provider
would route around every harness-parity invariant." This RFC agrees with that
rule and does not ask to amend it:

- `agent.provider` is untouched. The primary agent, dashboard sessions, cron
  sessions and every local subagent remain ACP.
- The invariants the rule protects — steer, approvals, session lifecycle,
  memory sizing — are honoured by *returning typed capability answers*
  (`steer_unsupported`, no permission events, not sharing-eligible) rather
  than by emulating kiro-cli.
- It is therefore narrower than
  [rfc-pluggable-model-providers](rfc-pluggable-model-providers.md) / #1693,
  which asks for provider *choice* on the primary path. Whatever the
  maintainers decide there, this proposal stands or falls on its own.

If maintainers would rather see this recorded as a scoped exception in
`oss-fork-boundaries.md` ("remote A2A agents as subagents only"), that is a
one-paragraph companion change and I will include it.

### Security considerations

A remote agent is an **operator-configured trust boundary**, not another local
process. A local subagent runs on the same host, under the same user, inside the
same sandbox and approval flow as the primary agent; a remote agent runs under
someone else's identity on someone else's infrastructure, and everything sent
to it is gone. Forwarding task text there is therefore not equivalent to a
model-provider request, and this section states exactly what crosses that
boundary, what does not, and how an enterprise forbids or scopes it. Every
claim below names the code that enforces it in #10171.

**What leaves the host.** The message a remote agent receives is built by
`subagent.build_remote_task_message`, never by `ContextBuilder.build_message`:

- **Sent:** the task text, redacted for credentials and exfiltration URLs
  (`security.redact_credentials`, `redact_exfiltration_urls`), plus a short
  fixed delegation preamble, as a single A2A text part. On a post-cancel
  resume, the same one-line interruption notice a local worker gets.
- **Not sent:** the system prompt, memory (preferences, projects, history,
  semantic and episodic recall), lessons, project steering, the skills index,
  session context, tool schemas. The `include_memory` / `include_lessons` /
  `include_project` spawn flags do not apply to a remote run and there is no
  opt-in: the local sub-agent envelope stays local. Widening this (for example
  letting an operator share selected memory with a trusted remote) is a
  separate RFC, deliberately — it is a two-way door that this one does not
  open.
- **Inbound:** only text parts of artifacts and status messages are rendered;
  file and data parts are ignored, and no URL carried in a response is ever
  dereferenced. The result is subject to the same inbound redaction and memory
  ingestion as any subagent result.

**Governance: remote spawn is its own, deniable capability.** Off-box
delegation sits under the admin ceiling as a distinct catalog row,
`capabilities.remote_spawn` (`platform/governance.py::SCOPE_CATALOG`), opt-in
like the other external-side-effect rows (`publish`, `messaging`) and carrying
its own `agents` ruleset over registry entries. The layering is:

- `capabilities.spawn` off → no sub-agents at all, local or remote;
- `capabilities.remote_spawn` named in a POLICY without `enabled: true`, or
  `enabled: false` → local sub-agents work, every remote spawn is refused with
  a reason that names the agent and the capability;
- `remote_spawn.scopes.agents` → only the listed registry entries may be
  targeted.

Both gates run at the spawn chokepoint (`subagent._vet_spawn_governance`). The
local-vs-remote classification is made **once**, at admission
(`admission.spawn_impl` → `_a2a_agent_entry`), and that single resolution is
what the governance vet, the collision refusal and the run path's branch all
consume — the entry travels on the run record. `config.json` is
agent-writable and a spawn can wait in the approval prompt between admission
and run, so re-reading the registry at each step would let an `a2a_agents`
entry written in that window re-route a spawn vetted as local off-box.
Continuations go through the same admission and inherit the same rule.
`TestRemoteSpawnGate` pins deny-remote-while-allowing-local, opt-in-when-named,
the agents scope, and that the gate judges the admitted decision.

**Authentication and transport.** The client authenticates the way the A2A
reference clients do (spec §7.3): the Agent Card declares the server's
`securitySchemes` and `securityRequirements`, the credential is obtained
out-of-band, and the client sends it per scheme. HTTP bearer, OAuth2 and OIDC
all resolve to one `Authorization: Bearer` header, which is what `bearer`
sends — on the card fetch and on every message — and is the mode AgentCore
Runtime exposes for JWT-authenticated A2A servers. The provider then:

- refuses to start when the card requires a scheme this client is not
  configured for — an unauthenticated "try anyway" request is never sent, so a
  `none` entry against an authenticated card fails at spawn time with a
  reason, not later with a 401;
- sends credentials only over TLS: with any scheme other than `none`, an
  `agent_card_url` that is not `https://` refuses to start before any socket
  is opened (`http://` to a loopback host is the one exception, for the test
  fixture);
- pins the message endpoint named in the card to the card URL's origin
  (scheme, host, port) and refuses redirects on every request, so an
  operator-configured host cannot hand the conversation — or the header — to a
  third host;
- reads the credential from the named environment variable per request, so a
  rotated value is picked up and the value is never stored in config or on the
  provider;
- never answers `TASK_STATE_AUTH_REQUIRED`: a remote agent asking this client
  for a credential mid-task ends the run as failed.

Identity granularity — per-user tokens versus one gateway principal — is a
scheme decision made where the scheme is registered, not in core.

**What A2A allows that this design does not use.** The protocol is wider than
prompt-in / artifact-out in both directions, and each unused channel is a
deliberate exclusion rather than an omission: file and data parts (both
directions), protocol extensions, push-notification webhooks (a remote would
otherwise be handed a URL to call back on), and credential fulfilment for
`AUTH_REQUIRED`. Adding any of them is a change to this section.

**Run lifecycle.** A user stop or a reap issues `CancelTask` before closing the
connection, so the remote does not keep working on a task nobody is reading. A
failed or dropped stream is recorded as a failed run with the remote's reason
and partial output — never as a silent success — and the conversation handle
is persisted on every terminal path so a failed turn stays resumable rather
than being reported gone. The wall-clock timeout is the bound on a remote run;
the turn budget counts permission events, which a remote never emits.

**Residual risks, stated plainly.**

- The remote agent sees the task text. An operator who registers a remote is
  trusting it with whatever users delegate to it, redaction notwithstanding.
- Conversation memory across `spawn_continue` is the *server's* policy (the
  spec says a server MAY retain context); this client reports `resume_failed`
  when the card no longer resolves, but cannot detect a server that silently
  forgot.
- `config.json` being agent-writable means the registry is as trustworthy as
  the host's own config; the governance ceiling, not the registry, is the
  control an enterprise relies on.
- Bearer-via-environment is the public core's only credential source; an
  environment that leaks to sub-processes leaks the token. A `command`-style
  credential provider is a candidate for a later change.

## Testing

Three layers, matching what the repo already runs.

**Unit tests** (`test/test_a2a_provider.py`, 43 on the branch): SSE event
mapping, registry accessors and round-trip, `_validate_agent` union and the
admitted-decision contract, session-sharing exclusion, `contextId` persistence
on every terminal path, truncated / failed / `AUTH_REQUIRED` streams raise,
cancel spelling, egress boundary (origin pin, scheme downgrade, no redirects,
task-text-only message, outbound redaction), auth schemes (bearer from env,
rotation, unset or unknown refused, card-requirement checks) and credential
transport (TLS-only, loopback exemption, session closed on a failed start).
`TestRemoteSpawnGate` (7, in `test_governance_chokepoints.py`) pins the
governance layering.

**Test-mode fixture.** `kiro_crew.testing` gains `fake_a2a_server`, the A2A twin
of `fake_acp_backend`: a loopback A2A server with a card, streaming, per-
`contextId` state retention, fault modes triggered by directives in the task
text (`[[SLOW]]`, `[[DROP]]`, `[[NEVER_TERMINAL]]`, `[[FAIL]]`, `[[ERROR]]`),
and a `--require-bearer-env NAME` switch that makes every request need
`Authorization: Bearer <value of NAME>`. `test/test_fake_a2a_server.py`
(15) runs the **real `A2AProvider`** against it, including bearer on the wire:
admitted with the right credential, 401 on the card fetch without it, a wrong
credential never retried unauthenticated. `kirocrew gateway --test-mode --seed
rich` registers the fixture as `a2a_agents[0]` under the name `remote-demo`, so
every lane below runs with no network and no credentials.

**Agentic "real user" scenarios** (`test/gui_user/scenarios/`, per
[`docs/build/gui-user-test.md`](../build/gui-user-test.md)). Adds one feature slug
to `scenarios.FEATURES` — `subagents: "Subagents & remote agents"` — and ships these
user stories:

| Scenario | `user_story` |
|---|---|
| `subagents-remote-spawn` (nightly) | As a user, I want to ask my agent something that it delegates to a remote agent, so that I see the remote worker stream and finish on the subagent board exactly like a local one. |
| `subagents-remote-continue` (nightly) | As a user, I want to continue a finished remote subagent conversation and have it remember what we discussed, so that a long investigation can be picked up later without re-explaining. |
| `subagents-remote-not-primary` (nightly) | As a user, I want the agent picker for my chat to offer only local agents, so that I cannot accidentally run a whole session on a remote worker that is meant to be delegated to. |
| `subagents-remote-steer` (nightly) | As a user, I want to send a follow-up to a running remote subagent and be told plainly when live interrupt is not available, so that I know which kinds of steering work with a remote agent. |
| `subagents-remote-mixed-batch` (nightly) | As a user, I want one request to fan out to a local and a remote worker at once, so that I get both results in a single completion without choosing a backend. |
| `subagents-remote-connection-lost` (nightly) | As a user, I want a remote subagent whose connection drops to show as failed with the output it managed to produce, so that I never mistake a dead worker for a finished one. |

Steps are written as briefings to a human tester, not click paths (the board's
labels are mid-transition), and expectations are true/false facts on the final
screen. The `connection-lost` scenario uses the fixture's `[[DROP]]`
directive, which is the CI form of the shim-kill test that found bugs #4 and #5.

## Migration plan

The phases below are review checkpoints, each of which leaves main working.
They are the order the work is read and verified in, not a commitment to one PR
per phase: the implementation is offered as **one PR** alongside this document
(the reference branch, squashed), and splits along these boundaries if
maintainers prefer smaller units.

- **Phase 0 — RFC (this document).** Direction decision; agree the
  registry-membership-is-semantics rule, the trust boundary in *Security
  considerations*, and the call-site placement.
- **Phase 1 — Registry + gates.** `a2a_agents` section with the typed `auth`
  object, spawn-time collision refusal, `_validate_agent` and roster union, the
  `capabilities.remote_spawn` catalog row and its gate, single admission-time
  classification. Behaviour change: none until a remote agent is configured.
- **Phase 2 — `A2AProvider` + branch.** Provider (origin pin, TLS-only
  credentials, card security-requirement check, cancel), `run.py` branch,
  `build_remote_task_message` as the egress boundary, session-sharing
  exclusion, `contextId` persistence on every terminal path, failure
  discipline, teardown, unit tests, and the `fake_a2a_server` fixture.
  `spawn_run` of a remote agent works end to end.
- **Phase 3 — Continuation + steer.** `continuation.py` changes; `spawn_continue`
  and both steer modes behave as specified.
- **Phase 4 — Agentic scenarios + docs.** The `subagents` feature slug, the six
  `test/gui_user` scenarios above (first real run lands on the nightly after
  merge, per #9578), the headless E2E, and
  `docs/system-specs/modules/a2a-subagents.md` as the module contract, with the
  `governance.md` chokepoint row; a note in `oss-fork-boundaries.md` if
  maintainers want one.

The reference branch (#10171) carries all four phases as one squashed commit, so
a split, if requested, is a rebase rather than a rewrite. Deferred to later,
separate changes, each its own RFC or issue: sharing selected memory or lessons
with a trusted remote (a widening of the boundary in *Security
considerations*); a `command`-style credential provider for rotating tokens;
the per-request session header some hosted A2A runtimes require; and
surfacing `TASK_STATE_WORKING` as activity so the idle-stall badge does not fire
on a silent remote.

## Evidence

Validated live against a small A2A server (a2a-sdk) wrapping kiro-cli on
loopback, so the *client* could be proven without infrastructure or a
dependency on any production agent. Eight scenarios driven from the dashboard,
all passing:

| Scenario | Outcome |
|---|---|
| spawn → stream → complete | ✓ |
| continue with retained context | ✓ (state survived a failed first resume and a gateway restart) |
| interrupt steer | ✓ typed `steer_unsupported`, no wire call |
| follow-up steer | ✓ delivered as next task; output `STEERED-OK` |
| mixed local + remote batch | ✓ one batch-completion event |
| remote absent from primary picker | ✓ |
| continue across gateway restart | ✓ |
| connection lost mid-turn | ✓ run fails; 13,952 chars of partial output kept |

The live matrix surfaced five integration defects the 15 unit tests did not,
all contract mismatches between components and all fixed on the branch:
contextId written before adoption (empty → `conversation_gone`); continuation
dropping the agent name and falling into the ACP `session/load` path; steer
unable to resolve a directly-constructed provider (perpetual
`session_starting`); and a two-stage silent-empty-success on truncated streams
(first fix reported through `EVENT_COMPLETE`, which is billing-only). The
last one is why *Failure discipline* above is specified as a raise.

The deterministic `fake_a2a_server` fixture then found two more on its first
run — both invisible to the kiro-cli shim because the shim happened to stream
its output as status messages and never cancelled: artifact text (the
protocol's actual result channel) was accumulated and reported only on the
billing-only completion event, so a spec-following server produced an empty
success; and `TASK_STATE_CANCELED` (the v1.0 spelling the reference SDK
emits) was not recognised as terminal, so a real cancel read as a lost
connection. Seven defects in total, none reachable by unit tests written from
the implementation's own assumptions — which is the argument for the fixture
and the agentic scenarios being part of the change rather than a follow-up.

## Open questions

1. Should `oss-fork-boundaries.md` gain an explicit "remote A2A agents,
   subagent-only" clause, or is the RFC itself the record? The Design Review
   suggested making the clause part of acceptance; I am happy to include the
   one-paragraph companion change in #10171 if maintainers want it there.
2. Should the roster hint show remote agents with a marker (e.g. `investigator
   (remote)`) so the primary agent can prefer local workers for
   filesystem-adjacent tasks?
3. Card caching: per run (current) vs a short TTL shared across runs.
4. *Resolved in #10171:* a `FAILED` or `REJECTED` terminal state raises, so the
   run is recorded as failed with the remote's reason and partial output rather
   than completing with an error chunk.
5. *Resolved in #10171 and folded into Security considerations:* the trust
   boundary, what egresses, and `capabilities.remote_spawn` as a distinct,
   policy-deniable capability with single admission-time classification.
6. Where the edition auth-scheme extension point should live: the SDK driver's
   scheme table (`agent_sdk/drivers/a2a.py::register_auth_scheme`, current) or
   the platform-context seam (`current_context()`), which is the repo's
   documented mechanism for edition-supplied providers. Either is a small
   mechanical move; the public core ships the same two schemes regardless.
