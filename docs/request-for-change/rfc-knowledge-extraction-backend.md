---
title: Knowledge Library extraction gets its own harness choice, read by the extraction pool only
status: draft
author: bolichen97
created: 2026-11-24
last-audited: 2026-11-24
audited-at: 5128cdb12f
doc-pr: null
implementation-prs: []
tracking-issues: [39494]
supersedes: []
superseded-by: []
---

# RFC: A harness choice for Knowledge Library extraction

Status: draft. Nothing is built and this document changes no code. It answers
the first ask of [#39494](https://github.com/kirodotdev/KiroCrew/issues/39494),
the follow-up to [#39087](https://github.com/kirodotdev/KiroCrew/issues/39087),
where a maintainer asked for a design sign-off and pointed at this process.
Acceptance is requested from a maintainer, who records it on this Status line.
Every code reference below was read at `5128cdb12f`.

It lands on its own because it moves a trust boundary in the sense of
CONTRIBUTING.md: running extraction on KAS while chats run on another harness
needs a second tool-less KAS process, and that process receives untrusted
document text. The maintainer reply on #39087 also asked for a design sign-off
before a separate extraction backend is built.

## 1. Summary

Add `knowledge.extraction_backend`. Empty (the default) keeps today's behaviour
byte for byte. A non-empty value picks the harness the Knowledge Library's
extraction workers run on, without moving chats, background turns or Research
Lab. Phase 1 serves the pick only through the worker paths that exist today.
Phase 2, which waits on open question 1, adds a pool-owned tool-less KAS
runtime so KAS can be picked while the gateway's background runtime runs
elsewhere.

## 2. Motivation

### Current state

- The extraction pool is one `LLMPool` built by the gateway
  (`setup_knowledge_routes` in `dashboard/handlers/knowledge_lifecycle.py`).
  Research Lab builds a second one, `LLMPool(pool_size=1)` with no session
  manager (its `auto_research_llm_pool` in
  `apps/builtins/auto_research/handlers.py`). There is no other caller.
- Each worker reads raw `config.json` `agent.acp_backend` (`_get_acp_backend`
  in `knowledge/llm_pool.py`), passes it through the selection gate
  (`resolve_selected_backend`, harness-parity H3) and keeps it only when it
  answers `honors_zero_tool_ban` and either `acp_client_spawnable` or "the
  gateway's background runtime runs it" (`_session_path_serves`). Anything
  else falls back through `_kiro_fallback`: Kiro CLI in an `oss` build, a
  `KnowledgeHarnessUnavailable` refusal in LTS.
- KAS reaches extraction only as `SessionWorker`
  (`knowledge/llm_pool_session.py`, built by `LLMPool._create_worker`): one
  `run_bg_oneliner` per chunk on the gateway's shared background runtime. The
  background runtime spawns under the loaded `agent.acp_backend`
  (`_configured_bg_backend` in `session_background.py`). So KAS extraction
  exists only when chats' own harness setting runs KAS.
- A stored `"kiro"` is read raw, so the pool keeps the Kiro CLI launch even
  where the config loader moves the same pick onto KAS (`_read_backend_choice`
  in `config/loader_builders.py`, decision
  [2026-10-28](../decisions/2026-10-28-a-kiro-cli-choice-runs-on-kas.md)).
  `resolve_selected_backend` in `agent_sdk/backends.py` maps the Kiro CLI id to
  itself. This RFC does not change that default reading (non-goal 3).
- The extraction model is `knowledge.extraction_model`, else `agent.model`,
  else `"auto"` (`knowledge_agent_model` in
  `agent_materialization/service_agents.py`). The installer writes it into the
  shared `kirocrew-knowledge` spec, which kiro-cli reads, and the pool hands it
  as `model=` to every other harness (`_knowledge_spec_model`, passed by
  `AcpWorker._spawn_client`). Research Lab's pool reads the same spec and the
  same rule.
- Settings → Knowledge lists the extraction model from the default harness's
  catalogue (`useAvailableModels()` with no backend, in
  `website/src/pages/knowledge/SettingsTab.tsx`), and the PATCH schema checks
  `knowledge.extraction_model` for grammar only for that reason
  (`_EDITABLE_CONFIG` in `dashboard/handlers/core_config_schema.py`).

### Problems

1. A user cannot run extraction on a different harness than chats. With KAS
   signed in and chats on another harness, extraction cannot use KAS at all.
2. Any harness choice for extraction today also moves chats, background turns
   and Research Lab, because they all read `agent.acp_backend`.

## 3. Goals

- G1. One key that picks the extraction harness, with the default meaning
  "no choice": today's behaviour, unchanged for every existing config.
- G2. Only the Knowledge Library's extraction pool reads it.
- G3. A pick is read the way every other harness input is read (H1, H3, H4)
  and still cannot reach a harness that fails the pool's zero-tool questions.
- G4. A Settings control and docs for it.

## 4. Non-goals

1. Research Lab, or any `LLMPool` other than the extraction pool, changing its
   harness or its model for any value of the new key.
2. Changing the gateway's background runtime, or what chats run on.
3. Changing how an empty key reads `agent.acp_backend`, including the raw
   `"kiro"` reading above. If that reading is wrong it is its own issue.
4. A per-document or per-source harness choice.
5. New tools, permissions or MCP servers for extraction workers.

## 5. Design

### D1. The key

`knowledge.extraction_backend`, a string, default `""`. `""` and an absent key
mean "no choice" and run exactly today's path. The stored vocabulary is the one
`agent.acp_backend` uses: policy ids, Kiro CLI stored as `"kiro"`
(`backend_from_stored`, H1). A config that never sets it behaves as today; an
older build that reads a file with the key set ignores it.

### D2. Bound to the extraction pool only

The key reaches a pool only through a constructor argument, the way
`effort_key` and `config_pool_size_key` already do
(`LLMPool.__init__`); the gateway's extraction pool passes it
and Research Lab's pool does not. A pool without it keeps reading
`agent.acp_backend` as today.

### D3. How a pick is read

- A non-empty value is a pick and goes through the one gate,
  `resolve_selected_backend` (H3): an unselectable or unknown value degrades to
  the build's floor with a logged reason, as an `agent.acp_backend` pick does.
- A stored `"kiro"` is read the way H1 reads a stored `agent.acp_backend`
  pick, and at the same point: on the stored value, before the gate, as
  `_read_backend_choice` does, so a value the gate already degraded to the Kiro
  CLI floor is never moved. It runs on KAS where `kiro_cli_pick_backend()`
  answers KAS (this process holds a confirmed kiro-cli v3 start), the saved
  `agent.kas_bundle` is off (`_resolved_kas_bundle`, as `_read_backend_choice`
  checks), and the pool can run KAS here on kiro-cli's relay; it keeps the Kiro
  CLI launch otherwise. The key itself never triggers the gateway floor's start
  check, so it keeps Kiro CLI unless the gateway already holds that verdict. In
  Phase 1 the pool can run KAS only where the background runtime runs it, which
  follows chats' own harness setting, and only where that runtime's KAS spawn
  took kiro-cli's relay rather than Kiro Crew's own engine (decision 2026-10-28
  keeps a Kiro CLI pick on kiro-cli). A Kiro CLI pick that the build's policy allows is
  never refused by the harness rule (decision 2026-10-28) and never reaches
  `_kiro_fallback`.
- The result then meets the pool's two capability questions unchanged
  (`honors_zero_tool_ban`, and `acp_client_spawnable` or a KAS path the pool can
  use). A pick that fails them takes the existing fallback, which warns once per
  backend because the user declared it.
- `docs/system-specs/modules/harness-parity.md` names the new input in H1,
  beside the stored `agent.acp_backend` pick it is read like, and in H17's list
  of inputs that keep H3's degrade, in the PR that adds it.

### D4. The values offered

Derived live, never listed (H4): `selectable_backends()` that answer
`honors_zero_tool_ban` and can run in the pool now (`acp_client_spawnable`, or
KAS where a KAS path is available). `GET /api/config/schema` supplies them the
way it supplies `agent.acp_backend`'s live values
(`_supply_live_enum` in `dashboard/handlers/agents.py`). No identity list and
no exclusion list.

### D5. The model

With a pick set and `knowledge.extraction_model` empty, extraction runs the
picked harness's own default, never `agent.model`, which names the chat
harness's catalogue. A set `knowledge.extraction_model` applies as today. The
Extraction model select lists the picked harness's models
(`useAvailableModels` already takes a `backend`,
`website/src/hooks/useAvailableModels.ts`). Constraint: the rule lives with
the extraction pool. `knowledge_agent_model` and the shared spec's model stay
as they are, so Research Lab's model does not move (non-goal 1). How the pool
gives a spec-reading harness (Kiro CLI) a different model than the shared spec
is decided in the Phase 1 PR, without a second agent spec: the Kiro CLI
worker's zero-tool confirmation reads the one `kirocrew-knowledge` spec. This
holds even when the pick names the harness chats run on: with a pick set, an
empty extraction model no longer follows `agent.model`, and the Extraction
model row says so.

### D6. The control

A select on Settings → Knowledge, above Extraction model, labelled
"Extraction harness". Its first option is "Default" and writes `""`. Its
description says what the default runs, and must stay true where it differs
from chats: with the key empty, a stored `agent.acp_backend: "kiro"` runs Kiro
CLI here while chats may run KAS, and a harness that cannot run a tool-less
worker falls back (Kiro CLI in `oss`, a named refusal in LTS). The exact copy is
set in the Phase 1 PR. A change is applied the way an `extraction_pool_size`
change is: the pool watches the key and switches at its next idle boundary,
recycling its workers once it is fully idle when it has no idle TTL. An ingest
already running keeps its workers.

### D7. KAS while the background runtime runs elsewhere (Phase 2)

A pool-owned, tool-less KAS runtime, started only for an explicit pick that
resolves to KAS when `_session_path_serves` answers False. Constraints, each
pinned by a test in that PR:

- It carries every layer `SessionWorker` has: the pre-session spec verdict
  with one repair, the registered-entry check
  (`kas_tool_view`), tool requests refused and audited under
  `knowledge_extraction`, admission-closed raising `LLMPoolClosed`, the
  background memory gate, the KAS sign-out mapping, the cancellation shield,
  the spec-refusal cooldown and KAS's MCP subsystem switched off
  (`knowledge/llm_pool_session.py`, knowledge spec §3).
- It passes the gates every KAS spawn passes: `await_kas_floor_verdict`
  and, in LTS, `refuse_unstartable_kas`, both called on the background
  runtime's spawn path in `session_background.py`.
- It runs under the operator's `agent.sandbox` tier, the one `AcpWorker`
  applies (`_get_sandbox_mode`), and is retired when that setting is tightened
  past the tier it was spawned at, as the background runtime is
  (`sandbox_tier_tightened`).
- It belongs to the pool: the idle scale-to-zero and `shutdown()` stop it, and
  no process outlives the pool.
- It carries the spawn identity stamp the background runtime carries, so a
  changed Kiro account retires it the way `flag_identity_stamp_mismatches`
  retires that runtime (`pre_spawn_identity` and `stamp_spawn_identity` on its
  spawn path in `session_background.py`). With
  `knowledge.pool_idle_ttl_secs: 0` the pool never scales to zero, so this is
  the only path that replaces it after an account switch.
- Its spec repair touches only its own spec. The background turns' repair
  (`background_turns.repair_lite_spec`) also resets the background runtime's
  `_bg` and `_consolidate` slots, so it is not reused here (non-goal 2).
- Only an explicit pick that resolves to KAS starts it. A value the gate
  degrades to the floor (a typo, a denied harness; KAS in LTS) is not a pick,
  as H1 and H3 say, and never starts it.
- A key of `"kiro"` that moved onto KAS keeps kiro-cli's relay, as a stored
  `agent.acp_backend: "kiro"` does. The route rule that keeps a Kiro CLI pick
  on kiro-cli (`automatic_switch_allowed` in `acp/kas_route.py`) reads only
  `agent.acp_backend` and Bot pins today, so the Phase 2 PR extends it: for the
  pool's own spawn the key takes the place of `agent.acp_backend`, the way a Bot
  pin does, rather than being read beside it.
- Otherwise it takes the KAS route and identity a KAS spawn on this host takes
  (`acp/kas_route.py`): kiro-cli's relay, or Kiro Crew's own engine and sign-in
  where that route applies (decision
  [2026-10-30](../decisions/2026-10-30-choosing-kas-runs-kiro-crews-own-engine-when-kiro-cli-cannot.md)).

Until Phase 2 ships, KAS is offered only where the background runtime runs it
(D4). A stored `"kas"` elsewhere takes the existing fallback: Kiro CLI with a
logged warning in `oss`, a named `KnowledgeHarnessUnavailable` refusal in LTS.

## 6. Migration plan

### Phase 1: the key, the pool binding, the control

D1-D6 in one PR. No new process.

Exit criteria:

- With the key absent or `""`, `_get_acp_backend` answers the same for every
  `agent.acp_backend` value it answers today (one parametrized test), and the
  existing knowledge pool tests pass unchanged.
- With `agent.acp_backend: "kas"` and the key set to a spawnable harness that
  honours the zero-tool ban, extraction workers are `AcpWorker` on that harness.
- For every key value, Research Lab's pool resolves the same harness and model
  as with the key absent.
- A stored `"kiro"` pick the build's policy allows never reaches
  `_kiro_fallback` or the policy refusal; the sign-in and memory refusals are
  unchanged.
- A key change reaches a running pool at its next idle boundary, with and
  without an idle TTL.
- With a pick set and `knowledge.extraction_model` empty, the extraction
  workers ask for no model pin, Kiro CLI included, while the shared
  `kirocrew-knowledge` spec's model and `knowledge_agent_model()` answer as
  with the key absent.
- The schema endpoint offers only live values; the Settings select renders them
  and writes `knowledge.extraction_backend`.
- The knowledge spec (§3 and "LLM worker-pool policy" in
  `docs/system-specs/modules/knowledge.md`), harness-parity H1 and H17,
  the `knowledge.*` keys in `docs/system-specs/modules/config.md`, the settings
  registry and the UI index say so.

### Phase 2: KAS through a pool-owned runtime

Blocked on open question 1. D7 in one PR.

Exit criteria:

- With `agent.acp_backend` on a harness other than KAS, the key `"kas"`, Kiro
  Crew's own KAS sign-in present and Kiro CLI signed out, a document added
  through `knowledge_add_document` is extracted on the pool's runtime and found
  by `local_knowledge_search` with its source citation, on the in-process
  integration harness the MCP end-to-end test of #39494 already uses.
- Each D7 layer has a test that fails when the layer is removed.
- After the pool scales to zero or shuts down, no KAS process it started is
  alive.

## 7. Backward compatibility

The default is "no choice", so no existing config changes behaviour. Removing
the key later is a revert: an older build ignores it.

## 8. Security considerations

Extraction sends untrusted document text to a model. Phase 1 adds no worker
path: a pick reaches only the workers and confirmations the pool already runs,
and cannot reach a harness that fails `honors_zero_tool_ban`. Phase 2 adds a
process that receives that text, so it must carry every layer listed in D7
before it sends any. Nothing here widens a permission or mounts a tool.

## 9. Alternatives considered

- **No key; users change `agent.acp_backend`.** That moves chats, background
  turns and Research Lab with it (problem 2).
- **Run extraction wherever the background runtime runs, and let the key move
  that runtime.** That moves every background turn (non-goal 2).
- **One key for every `LLMPool`.** That moves Research Lab (non-goal 1).
- **Phase 1 only.** Kept as the answer if open question 1 is no: the key still
  lets extraction run on another spawnable harness, but not on KAS while chats
  run elsewhere.

## 10. Open questions

1. Is Phase 2 wanted? It is the only phase that serves #39087's case (KAS
   signed in, chats elsewhere), and it costs a second KAS process while an
   ingest runs. Proposed: yes, after Phase 1 ships, because without it the key
   cannot reach KAS in the one case the request was filed for.
2. Is "Extraction harness" / "Default" the wording? Proposed: yes. "Harness"
   is the word Settings → Agent Harness uses for `agent.acp_backend`, and
   "Default" avoids claiming "same as chats", which is false for a stored
   `"kiro"`.
