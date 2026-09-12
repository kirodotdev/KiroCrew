# A2A remote subagents — remote agents as subagent-only workers

Design record: RFC [#10162](https://github.com/kirodotdev/KiroCrew/pull/10162),
which adds the rfc-a2a-remote-subagents document under `docs/request-for-change/`.
This page is the contract for what is on main; the RFC is why.

## Overview

A **remote agent** is an agent reached over [A2A](https://a2a-protocol.org)
(Agent2Agent v1.0, HTTPS + SSE) instead of a local kiro-cli process. Kiro Crew
consumes it through `A2AProvider` (`providers/a2a.py`), which implements the same
`LLMProvider` ABC as the ACP provider and is created at the provider seam in
`subagent_manager/run.py::_run_inner_impl`. Above that seam — the subagent board,
completion events, `spawn_continue`, the follow-up watcher, tombstones — nothing
knows the worker is remote.

Two rules define the feature:

1. **Registry membership is the semantics.** A remote agent is an entry in the
   `a2a_agents` config section. Being in that registry makes the name spawnable
   via `spawn_run` / `spawn_continue` / `spawn_steer` and *never* selectable as a
   session's primary agent. There is no `kind` and no `role` field. The primary
   picker reads `~/.kiro/agents/` only, so remote agents are structurally absent
   from it rather than filtered.
2. **The context is the conversation.** An A2A task is immutable once terminal;
   continuation happens at the conversation level. Kiro Crew maps the A2A
   `contextId` to the subagent conversation and treats each turn as one task.

`agent.provider` is unchanged and stays `acp`; see
[`../oss-fork-boundaries.md`](../oss-fork-boundaries.md). This module adds a
provider for *subagents*, not a second primary-path provider.

## Config: `a2a_agents`

```json
"a2a_agents": [
  { "name": "investigator",
    "agent_card_url": "https://<host>/.well-known/agent-card.json",
    "auth": { "scheme": "bearer", "token_env": "KIROCREW_A2A_INVESTIGATOR" } }
]
```

| Field | Meaning |
|---|---|
| `name` | The spawnable name. A name that is also a local agent is ambiguous and is refused at the spawn (`_validate_agent`, code `agent_name_collision`), where the local roster is known; `KiroCrewConfig.validate_a2a_collisions` defines the rule. |
| `agent_card_url` | Where the Agent Card is fetched (`GET`, once per run, authenticated like every other request). The message endpoint is read from the card and must share the card URL's origin. `capabilities.streaming: true` is required. |
| `auth` | `A2aAuthConfig`: `scheme` is `none` (default) or `bearer`; with `bearer`, `token_env` NAMES the environment variable holding the credential, read on every request and sent as `Authorization: Bearer`. The name must be under the `KIROCREW_A2A_*` namespace -- `config.json` is agent-writable, so it may not select any other variable in the gateway's environment as a remote credential. The scheme table lives in `agent_sdk/drivers/a2a.py` (`register_auth_scheme`), so an edition adds a scheme without touching core. Only the object shape is accepted. |

Section types: `config/sections.py::A2aAgentConfig`, `A2aAuthConfig`. Accessors on
`KiroCrewConfig`: `a2a_agent_names()`, `a2a_agent_by_name(name)`,
`validate_a2a_collisions(local)`. The loader references the section types in the
`_sections.` module form; the frozen pre-split re-export block is not extended
(`test_config_module_boundaries`).

### Authentication

An A2A client authenticates the way the reference clients do: the Agent Card
declares the server's security schemes (`securitySchemes` /
`securityRequirements`), the credential is obtained out-of-band, and the client
sends it per scheme. HTTP bearer, OAuth2 and OIDC all resolve to one
`Authorization: Bearer` header, which is what `bearer` sends. After the card is
fetched the provider checks its requirements against the schemes it can satisfy
and **refuses to start** (`A2AStreamError`) when none of the card's alternatives
is satisfiable — an unauthenticated "try anyway" request is never sent. A card
with no requirements, or with an anonymous alternative, accepts `none`. The
credential value is never stored in config or on the provider; only the env var
NAME is. Everything sent to a remote travels only over TLS — the task text on every
message as much as any credential — so an `agent_card_url` that is not `https://`
refuses to start before any request is made, for `none` entries too (`http://` to
a loopback host is the one exception, for the test fixture); the message endpoint
is origin-pinned to the card URL, so this covers every request.

Two rules keep the agent-writable `config.json` from turning a credential into an
exfiltration channel, and both live in the operator's environment rather than in
config. **Selection:** `token_env` must be under the `KIROCREW_A2A_*` namespace,
so an entry cannot name the messaging bot token or a cloud credential.
**Destination:** a companion `<token_env>_ORIGIN` variable pins the one
`scheme://host[:port]` the credential may be sent to; `create_a2a_provider`
refuses an entry whose `agent_card_url` has any other origin before any request,
and `start()` repeats the check. An entry that keeps a valid `token_env` but
rewrites `agent_card_url` to another host therefore sends nothing. Registered
edition schemes return the same `ResolvedCredential(headers, origin)` shape and
are bound the same way.

## Gates that admit a remote name

Local-vs-remote is classified **once**, at admission (`admission.py::spawn_impl`
calls `_a2a_agent_entry(agent)`), and that one resolution is what the governance
vet, the collision refusal and the run path all consume: the entry is stashed on
the run record as `_a2a_entry` and `run.py` branches on the stash, never on a
fresh registry read. Three independent `KiroCrewConfig.load()` reads would be a
time-of-check/time-of-use hole — `config.json` is agent-writable and a spawn can
wait in the approval prompt between admission and run, so an `a2a_agents` entry
written in that window could re-route a spawn vetted as local off-box.

| Gate | Behaviour |
|---|---|
| `subagent.py::_a2a_agent_entry(name)` | The single resolution. Returns the `A2aAgentConfig` **only if** the loaded config returns a genuine `A2aAgentConfig` with a non-empty `agent_card_url`; anything else (a test double, a partial config) yields `None` and the local path. Called once per admission. |
| `subagent.py::_vet_spawn_governance(..., remote=)` | Two gates for a remote target: `capabilities.spawn` (as for any spawn) and then `capabilities.remote_spawn`, with its own `agents` ruleset over registry entries. Judges the admitted decision passed in. See *Governance* below. |
| `subagent.py::_validate_agent(..., remote=)` | An unknown name is refused fail-closed; a name that is BOTH a local agent and (per the admitted decision) a registry entry is refused with code `agent_name_collision`. This is where the collision rule runs — at spawn, for the requested name, not at config load. |
| roster hint (`visible_agent_names`) | Remote names appear in the subagent context only. |
| `run.py::_should_use_session_sharing_impl` | Returns `False` for a remote agent; a remote provider cannot host the shared local ACP runtime. |
| primary-agent picker | Not a gate: it never consults `a2a_agents`. |

## What leaves the host, and what does not

A remote agent is an operator-configured **trust boundary**, not another local
process. The message it receives is built by `subagent.build_remote_task_message`,
never by `ContextBuilder.build_message`:

- **Sent:** the task text (redacted for credentials and exfiltration URLs) and a
  short delegation preamble, as a single text part. On a post-cancel resume, the
  same one-line interruption notice a local worker gets.
- **Not sent:** the system prompt, memory (preferences, projects, history,
  semantic/episodic recall), lessons, project steering, the skills index, session
  context. The `include_memory` / `include_lessons` / `include_project` spawn
  flags do not apply to a remote run; there is no opt-in. Widening this is a
  design change for a later RFC, not a flag.
- **Never done:** dereferencing a URL from an artifact or status message (only
  text parts are rendered; file and data parts are ignored), registering a
  push-notification webhook, or answering `TASK_STATE_AUTH_REQUIRED` — a remote
  agent asking this client for a credential mid-task ends the run as failed.
- **Endpoint pinning:** the message endpoint named in the card must share the
  card URL's origin (scheme + host + port) or the start fails; redirects are
  refused on every request.

The result is subject to the same inbound redaction and memory ingestion as any
subagent result.

## Governance

`capabilities.remote_spawn` (`platform/governance.py::SCOPE_CATALOG`) is a
distinct capability row, opt-in like the other external-side-effect rows
(`publish`, `messaging`): an unnamed key is ungoverned and permitted, a policy that
names it without `enabled: true` denies, and two rulesets bound the target — `agents`
over registry entry names, and `origins` over the entries' card-URL origins
(`scheme://host[:port]`, host-glob matched like `network.egress`). An enterprise
POLICY can therefore allow local sub-agents (`capabilities.spawn`) while forbidding
or scoping off-box delegation. The `origins` ruleset is the one that pins the
**destination**: `config.json` is agent-writable, so a policy that only names
agents is satisfied by an entry that keeps an allowed name and points its URL at
another host; a policy that pins origins refuses it. Both gates are evaluated at
the spawn chokepoint on the admission-time classification — the same resolution
(entry and origin) the run path routes on (see *Gates*).

```json
"capabilities": {
  "spawn": { "enabled": true },
  "remote_spawn": { "enabled": true,
                    "scopes": { "agents":  { "mode": "allow", "allow": ["investigator"] },
                                "origins": { "mode": "allow", "allow": ["https://agents.example.com"] } } }
}
```

## Provider construction

Application code does not import `kiro_crew.providers` (see
`scripts/check_agent_sdk_boundary.py`). `run.py::_build_a2a_provider_impl` calls
`kiro_crew.agent_sdk.drivers.a2a.create_a2a_provider(entry, context_id=…)`, which
is the one construction point, then `start()`s the provider itself because the
caller decides what a card-fetch failure means: on a fresh spawn it raises and the
run tombstones; on a continuation it is left to the `resume_failed` guard.

## Wire mapping

| Kiro Crew | A2A |
|---|---|
| subagent conversation (`conversation_key`) | `contextId` |
| one turn | one task (`taskId`) |
| first turn | `SendStreamingMessage` with no `contextId`; the server mints `taskId` and `contextId`, the provider adopts both |
| `spawn_continue` | `SendStreamingMessage { contextId, referenceTaskIds: [last taskId] }` → new task in the same context |
| `spawn_steer mode=follow_up` | existing follow-up watcher; delivered as the next task in the context once the current one is terminal |
| `spawn_steer mode=interrupt` | `steer_unsupported` (typed), no wire call; `supports_steer` is `False` |
| reap / user stop / timeout | `CancelTask` (best-effort request; the server decides), then the HTTP session is closed — `run.py::_release_direct_provider_impl`, called from both normal teardown (shutdown only) and `_force_reap` (cancel + shutdown) |
| `TaskArtifactUpdateEvent` | `EVENT_TEXT_CHUNK` per delta |
| terminal `COMPLETED` / `CANCELED` | `EVENT_COMPLETE` |
| terminal `FAILED` / `REJECTED`, JSON-RPC error frame, `AUTH_REQUIRED` | **`A2AStreamError`** — a failed run, never a completion (see *Failure discipline*) |

Every request carries `A2A-Version: 1.0`; method names are the v1.0 gRPC-style
ones (`SendStreamingMessage`), not the 0.x `message/stream`. No tool-call or
permission events are emitted: the remote agent owns its own approvals.

## Persistence

The adopted `contextId` is stored as the run's `session_id` in `state.json`,
alongside `provider: "a2a"` (`PROVIDER_LABEL_A2A`). The acquisition-time session
record runs before any A2A message has been sent and stores an empty id, so the
handle is written by `run.py::_persist_a2a_context_impl` **after the first turn
completes** and again — idempotently — from `_release_direct_provider_impl`,
which every terminal path reaches (completion, a raising stream, timeout, user
stop, force-reap). A turn that fails after the remote adopted a `contextId`
therefore still leaves a resumable record instead of `conversation_gone`. Both
adoption points are gated on `_a2a_entry`, never on duck-typed attributes of the
client.

On `spawn_continue`, `continuation.py` inherits the recorded agent when the caller
passes none, reads the stored `contextId`, and rebuilds the provider with it.
`_resumed` is reported from `context_id`, so a card that no longer resolves
produces `resume_failed` rather than a silent fresh conversation.

## Failure discipline

A stream that raises mid-turn, ends without a terminal task state, **or reaches a
`FAILED` / `REJECTED` terminal state, a JSON-RPC error frame, or
`AUTH_REQUIRED`, a `CANCELED` state (cancelled work is not completed work), or a
frame whose `contextId` differs from the retained conversation's** raises
`A2AStreamError` (`transient = False`) carrying the
remote's own reason. This matters because `run.py` assembles the run's result from
text chunks, reads `EVENT_COMPLETE` only for billing and calls `record_success`
on it: a failure reported through the completion event surfaces as a clean ✅.
Raising routes the run through the ordinary error arm — tombstone `cause: error`,
partial output preserved in `result.txt` (every delta was streamed live) — and the
structural `transient` verdict keeps `acp_error_is_transient` from re-sending the
task to a dead server.

Two size caps bound what a remote can make this process hold, because the
subagent memory guard cannot measure a remote run: an Agent Card body over
256 KiB refuses the start, and a turn whose streamed text exceeds 4 MiB is
abandoned as a failed turn (`a2a task output cap`). The HTTP read timeout is an
**idle** bound (`sock_read`, 30 min of silence), never a total: a long, actively
streaming turn is bounded only by the manager's wall clock.

On a user stop or a reap, `terminal.py` claims the record (`reaped`) and cancels
the local run task **before** releasing the remote (CancelTask, then closing the
HTTP session): closing the session mid-stream wakes the run task, and a task that
wakes while the reap does not yet own the record would write its own outcome.

Two local-worker assumptions do not carry over and are stated rather than fixed:
the parent's turn budget counts tool-permission rounds, which a remote run never
emits, so only the wall-clock timeout bounds it; and the idle-stall badge is driven
by stream events, so a remote agent that works silently is shown as *stalled*
(advisory — the reaper never kills on it).

## What a remote run does not have

No pid, so the memory guard and the `/proc` reaper see an unmeasurable worker
(`None`), which the sizing code already tolerates. Not eligible for session
sharing. `billing_stats` is `None` (unmetered). `runtime_info()` is `(None, None)`
so abort-push is disabled. `context_usage_pct()` is `0.0`.

## Testing

Unit: `test/test_a2a_provider.py`; provider-against-fake: `test/test_fake_a2a_server.py`
(including bearer on the wire); governance: `test/test_governance_chokepoints.py`;
headless end-to-end (real gateway, fake primary with delegation directives, fake
remote): `test/test_e2e_remote_subagents.py`. Agentic GUI scenarios: the six
`test/gui_user/scenarios/subagents-remote-*.yaml` stories under the `subagents`
feature; they run against the `fake_a2a_server` test-mode fixture registered by
the `rich` seed as `remote-demo`.
