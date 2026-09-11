---
title: A2A Remote Subagents — remote agents as first-class subagents over A2A
status: in-progress
author: jonmcox-aws
created: 2026-09-11
last-audited: 2026-09-11
audited-at: 707b8aef2
doc-pr: TBD
implementation-prs: []
tracking-issues: [10161]
supersedes: []
superseded-by: []
---
# RFC: A2A Remote Subagents

- Status: in-progress. A reference implementation exists on a branch against
  `efb9fc4ba` (public core 0.7.0): 10 commits, 11 files, +1,155/−4, 15 tests.
  Nothing is on main. The branch is evidence for this proposal, not a request
  to merge it as-is.
- Author: jonmcox-aws
- Related: [rfc-resumable-subagent-sessions.md](rfc-resumable-subagent-sessions.md)
  (continuable conversations are the substrate this builds on),
  [rfc-pluggable-model-providers.md](rfc-pluggable-model-providers.md) and
  #1693 (adjacent but distinct — see *Relationship to the provider rule*),
  [`../system-specs/oss-fork-boundaries.md`](../system-specs/oss-fork-boundaries.md).

## Summary

Let a session's primary agent delegate to a **remote agent** as an ordinary
subagent. The remote agent speaks [A2A](https://a2a-protocol.org) (Agent2Agent
v1.0); KiroCrew consumes it through a new `A2AProvider` plugged into the same
provider-creation seam every subagent run already passes through. Remote agents
live in their own registry (`a2a_agents` in config) and **membership in that
registry is the whole semantics**: spawnable via `spawn_run` /
`spawn_continue` / `spawn_steer`, never selectable as a session's primary agent.
The primary path stays KiroACP-only.

The user-visible change is small: a name in `a2a_agents` appears on the
subagent board like any local worker, streams, completes, and can be continued
days later. The one visible difference is that live mid-turn interrupt returns
the existing typed `steer_unsupported`.

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
  exists. None of them has an app hook. **This cannot be a KiroCrew app.**
- `agent.provider` is fixed to `acp` and
  [oss-fork-boundaries](../system-specs/oss-fork-boundaries.md) lists "Other
  providers" under *Never re-add*.

### Problems

1. **Capability lives on each machine.** When a team improves an agent
   (skills, runbooks, memory), every user must pull it locally. The fleet of
   local copies drifts; the engineer paged at 2am runs last month's playbook.
2. **No team-level agent.** A centrally operated agent — one deployment, one
   memory, one place to measure quality — has no way into a KiroCrew session.
   Users leave KiroCrew for that agent's own UI, or rebuild locally.
3. **Standard exists, unused.** A2A is the open protocol for agent-to-agent
   delegation (Linux Foundation; server support in Amazon Bedrock AgentCore
   Runtime since Nov 2025; 150+ organizations). KiroCrew has no client.

### Why not an MCP wrapper

An MCP tool that calls the remote agent works today and is the wrong shape:
the call is synchronous inside a tool turn, it does not stream to the board,
it is not a conversation the user can continue, it cannot be batched with
local workers in one `spawn_run`, and it is invisible to the subagent
lifecycle (tombstones, TTL, `spawn_status`). The point is that a remote agent
is a *teammate*, and teammates in KiroCrew are subagents.

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
- An A2A **server** in KiroCrew (exposing KiroCrew's own agents to others).
  Interesting, separate RFC.
- Any UI beyond what the board already renders for a subagent.

## Design

### The one mental model: the context is the conversation

A2A tasks are immutable once they reach a terminal state; a completed task
cannot receive new messages. Continuation happens at the *conversation*
level: the server groups related tasks under a shared `contextId`, and a
follow-up is a **new task in that context** carrying `referenceTaskIds`.

KiroCrew therefore maps:

| KiroCrew | A2A |
|---|---|
| subagent conversation (the thing `spawn_continue` targets) | `contextId` |
| one turn of that conversation | one task (`taskId`) |
| `spawn_continue` | `SendStreamingMessage{contextId, referenceTaskIds:[last]}` → new task |
| `spawn_steer mode=follow_up` | queued by the existing follow-up watcher; delivered as the next task in the context |
| turn complete (`EVENT_COMPLETE`) | terminal `TaskStatusUpdateEvent` (`COMPLETED`/`FAILED`/`CANCELED`/`REJECTED`) |

The provider keeps two pieces of state: `context_id` (durable, persisted to
the run's `state.json` after the first turn) and the current `task_id`
(in-flight turn; the `referenceTaskIds` anchor for the next). Task boundaries
are invisible to the user. KiroCrew's existing one-in-flight-turn-per-
conversation serialization is exactly the client-side discipline the A2A spec
asks for.

### Registry: `a2a_agents`

```json
"a2a_agents": [
  { "name": "investigator",
    "agent_card_url": "https://<host>/.well-known/agent-card.json",
    "auth": { "type": "bearer", "token_env": "INVESTIGATOR_TOKEN" } }
]
```

- Loader (`config/sections.py`, `config/loader.py`) validates the shape and
  refuses a name that collides with a local agent. On collision the subagent
  path fails closed.
- Membership is the semantics. There is no `kind` and no `role`: every local
  agent can be primary or subagent; every remote agent can only be a
  subagent. The primary picker reads `~/.kiro/agents/` only, so remote agents
  are structurally absent from it without any filtering code.
- `auth.type` is resolved through the provider registry
  (`current_context().providers`). Core ships `none` and `bearer`; an
  edition registers its own schemes through the same seam the public core
  already exposes for edition-supplied providers, so nothing
  organisation-specific enters core.

### `A2AProvider` (`providers/a2a.py`)

Implements `LLMProvider`. On `start`: fetch the agent card (once per run,
cached), require `capabilities.streaming` for the streaming path. On
`stream(prompt)`: `POST SendStreamingMessage` with header `A2A-Version: 1.0`,
including `contextId` + `referenceTaskIds` when continuing; consume SSE:

- first `Task` frame → adopt `taskId` and (on first turn) `contextId`;
- `TaskArtifactUpdateEvent` → `EVENT_TEXT_CHUNK` per delta;
- terminal `TaskStatusUpdateEvent` → `EVENT_COMPLETE`. A `FAILED` state
  surfaces the server's status message as a final text chunk so the parent
  sees why.

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

| # | File | Change | Size |
|---|---|---|---|
| 1 | `config/sections.py`, `config/loader.py` | `a2a_agents` section, accessors, collision validation | ~90 |
| 2 | `providers/a2a.py` | `A2AProvider`, `A2AStreamError`, SSE mapping | ~400 |
| 3 | `subagent_manager/run.py` | branch at the provider seam; exclude from session sharing; re-persist adopted `contextId` post-turn; stash provider on `info` for steer | ~110 |
| 4 | `subagent.py::_validate_agent` + roster hint | union remote names (subagent context only) | ~75 |
| 5 | `subagent_manager/continuation.py` | rebuild provider from stored label + `contextId`; inherit recorded agent when caller passes none; resolve direct providers on steer and return `steer_unsupported` | ~25 |
| 6 | `mcp_tools/spawn.py`, `acp/types.py` | provider label routing, `PROVIDER_LABEL_A2A` | ~30 |
| 7 | `test/test_a2a_provider.py` | 15 tests: SSE mapping, collision, validate-agent union, sharing exclusion, truncation raises, mid-stream error non-transient | ~360 |

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

- The agent card URL is operator configuration, not user input; it is read
  from `config.json` like any other endpoint. Cards are fetched over HTTPS
  and never followed to a third host.
- Credentials never enter core: `auth.type` resolves through the edition
  seam; core's `none`/bearer schemes read tokens from config, never from the
  prompt or the remote agent.
- The remote agent runs under its own approvals and identity. KiroCrew does
  not forward tool-permission prompts to it and does not grant it any local
  capability. Prompt text and task output are the only data crossing the
  wire, which is the same boundary as any subagent's transcript.
- Contexts are bound to the authenticated principal on the *server*; whether
  a deployment forwards per-user identity or calls as one gateway principal
  is an auth-scheme decision, made in the edition adapter.

## Testing

Three layers, matching what the repo already runs.

**Unit tests** (`test/test_a2a_provider.py`, 15 on the branch): SSE event mapping,
registry collision, `_validate_agent` union, session-sharing exclusion, truncated
stream raises, mid-stream connection error is non-transient.

**Test-mode fixture.** `kiro_crew.testing` gains `fake_a2a_server`, the A2A twin
of `fake_acp_backend`: a loopback a2a-sdk server with a card, streaming, per-
`contextId` state retention, and fault switches (`--drop-mid-stream`, `--never-
terminal`). `kirocrew gateway --test-mode --seed rich` registers it as
`a2a_agents[0]` under the name `remote-demo`, so every lane below runs with no
network and no credentials.

**Agentic "real user" scenarios** (`test/gui_user/scenarios/`, per
[`docs/build/gui-user-test.md`](../build/gui-user-test.md)). Adds one feature slug
to `scenarios.FEATURES` — `subagents: "Subagents & remote agents"` — and ships these
user stories:

| Scenario | `user_story` |
|---|---|
| `subagents-remote-spawn` (smoke) | As a user, I want to ask my agent something that it delegates to a remote agent, so that I see the remote worker stream and finish on the subagent board exactly like a local one. |
| `subagents-remote-continue` (smoke) | As a user, I want to continue a finished remote subagent conversation and have it remember what we discussed, so that a long investigation can be picked up later without re-explaining. |
| `subagents-remote-not-primary` (smoke) | As a user, I want the primary-agent picker to offer only local agents, so that I cannot accidentally run a whole session on a remote worker. |
| `subagents-remote-steer` (nightly) | As a user, I want to send a follow-up to a running remote subagent and be told plainly when live interrupt is not available, so that I know which steering works. |
| `subagents-remote-mixed-batch` (nightly) | As a user, I want one request to fan out to a local and a remote worker at once, so that I get both results in a single completion without choosing a backend. |
| `subagents-remote-connection-lost` (nightly) | As a user, I want a remote subagent whose connection drops to show as failed with the output it managed to produce, so that I never mistake a dead worker for a finished one. |

Steps are written as briefings to a human tester, not click paths (the board's
labels are mid-transition), and expectations are true/false facts on the final
screen. The `connection-lost` scenario uses the fixture's `--drop-mid-stream`
switch, which is the CI form of the shim-kill test that found bugs #4 and #5.

## Migration plan

The phases below are review checkpoints, each of which leaves main working.
They are the order the work is read and verified in, not a commitment to one PR
per phase: the implementation is offered as **one PR** alongside this document
(the reference branch, squashed), and splits along these boundaries if
maintainers prefer smaller units.

- **Phase 0 — RFC (this document).** Direction decision; agree the
  registry-membership-is-semantics rule and the call-site placement.
- **Phase 1 — Registry + gates.** `a2a_agents` section, collision validation,
  `_validate_agent` and roster union. Behaviour change: none until a remote
  agent is configured.
- **Phase 2 — `A2AProvider` + branch.** Provider, `run.py` branch, session-
  sharing exclusion, contextId persistence, failure discipline, unit tests, and
  the `fake_a2a_server` test-mode fixture. `spawn_run` of a remote agent works
  end to end.
- **Phase 3 — Continuation + steer.** `continuation.py` changes; `spawn_continue`
  and both steer modes behave as specified.
- **Phase 4 — Agentic scenarios + docs.** The `subagents` feature slug and the six
  `test/gui_user` scenarios above (first real run lands on the nightly after
  merge, per #9578); `docs/system-specs/modules/subagent.md` and
  `providers.md` gain the remote-agent contract; `subagents.md` gains the user
  section; a note in `oss-fork-boundaries.md` if maintainers want one.

The reference branch already carries Phases 1–3 (minus the `fake_a2a_server`
fixture) as commits in this order, so a split, if requested, is a rebase rather
than a rewrite. Phase 4 lands after the design is accepted, since the scenarios
and docs describe the agreed behaviour.

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

## Open questions

1. Should `oss-fork-boundaries.md` gain an explicit "remote A2A agents,
   subagent-only" clause, or is the RFC itself the record?
2. Should the roster hint show remote agents with a marker (e.g. `investigator
   (remote)`) so the primary agent can prefer local workers for
   filesystem-adjacent tasks?
3. Card caching: per run (current) vs a short TTL shared across runs.
4. Where a `FAILED` terminal state should surface: as a final text chunk (current)
   vs a distinct error tombstone cause.
