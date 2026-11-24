---
title: "An app run may require a harness that holds its agent to its tools list"
status: accepted
author: keyejia, with kirocrew
created: 2026-11-23
last-audited: 2026-11-24
audited-at: 37f635e3e1
doc-pr: 41802
implementation-prs: []
tracking-issues: [41479]
supersedes: []
superseded-by: []
---

# RFC: App runs that require tool-allowlist enforcement

- Status: accepted — by maintainer bolichen97 on [#41802](https://github.com/kirodotdev/KiroCrew/pull/41802) ([comment 7145454103](https://github.com/kirodotdev/KiroCrew/pull/41802#issuecomment-7145454103), 2026-11-24). Nothing is built yet; this document changes no code.
- Tracking issue: [#41479](https://github.com/kirodotdev/KiroCrew/issues/41479). A maintainer confirmed the gap there and asked for a design through this process.
- Related: [#40671](https://github.com/kirodotdev/KiroCrew/pull/40671) (merged 2026-11-22, not in v0.13.1), [#39117](https://github.com/kirodotdev/KiroCrew/issues/39117) (open), [#21910](https://github.com/kirodotdev/KiroCrew/issues/21910) (open), [rfc-subagent-backend-selection.md](rfc-subagent-backend-selection.md), [rfc-app-own-agent-tools-grant.md](rfc-app-own-agent-tools-grant.md), [harness-parity.md](../system-specs/modules/harness-parity.md) (H3, H17).

This document lands first because it adds a new app contract (`ctx.spawn.run(require=...)`), a new manifest permission, and it changes which harness an app run can start on. CONTRIBUTING.md asks for an RFC first for that kind of change.

## Summary

An app can ask that one of its background runs start only where its agent is held to the agent's `tools` list. The app names a property, not a harness.

The property is checked on three things together: the harness and its route, the agent spec as it will be read at start, and the engine version. Kiro Crew picks a harness that governance allows, that is installed at or above a captured version, and that is declared to enforce the property. The spec must also be bounded: no wildcard, no blanket built-in grant, no user `mcp.json`, no Powers, no sub-agent tool, no tool search. The entry sent at session start must hold nothing beyond the projected list, so a tool the engine mounts outside the spec never reaches the run. If anything fails, the run is refused and audited. Nothing falls back, at spawn or at cold start.

The first and only claimed harness route is KAS on Crew's engine bundle. KAS through kiro-cli's relay is not claimed: it waits for the follow-up RFC's run-time check of the tools a session reports. The owner approves the request at install time, as an `explicit_only` manifest grant (`permissions.harnessRequirements`). An app that does not ask behaves exactly as today.

The design protects the owner from a prompt-injected model in an app run. It does not protect the owner from a malicious app.

## Motivation

### Current state

Verified at `37f635e3e1` (origin/main, 2026-11-24). Paths are under `src/kiro_crew/`. Since the first audit at `5e5ac0b4b7`, the only change to a fact below is item 13; the other cited files moved only in ways these items do not depend on.

1. `SpawnSDK.run` takes `task`, `agent`, `silent`, `model` and `keep` (`apps/spawn_sdk.py`). Its adapter `build_spawn_impl` passes no `backend`. `SubagentManager.spawn` takes `backend=` (`subagent_parts/admission_surface.py`), but apps cannot reach it. With no `backend`, an app run starts on `agent.acp_backend` (`subagent_start_backend` in `mate_worker.py`).
2. An override is not a promise. `select_provider_backend` (`members_parts/member_sessions.py`) is the one resolution gate. It sends an override through `resolve_selected_backend` (`agent_sdk/backends.py`), which degrades an unselectable value to `governance_floor_backend()` and never raises (harness-parity H3). A queued run re-enters with its stored `backend` and meets the same gate (`subagent_manager/admission/gate.py`).
3. The one value refused before the gate is a Bot's harness pin (H17): `crewmate_backend_pin` (`crewmate_harness.py`) raises before any process exists, and hands the gate a `StartableCrewPin` that it returns as is.
4. The floor is kiro-cli in a standard build and KAS in an LTS build. `apply_selectable_denials` always keeps it. A runtime ceiling (`policy_distribution.apply_ceiling`) does not re-run it, so `selectable_backends()` changes only at the next gateway start.
5. App runs pass `approval_mode="auto"`. Every permission request still reaches the hook gate first (`_consult_gate` in `subagent_manager/run_turn.py`). A call the harness approves by itself never reaches it.
6. The spec can widen `tools`. On KAS, `_project_tools` (`acp/kas_agents.py`) sends `"*"`, or a list holding `"*"`, as all tools, and expands each other row into its family of engine ids (`expand_kiro_cli_tool_names` in `platform/tool_names.py`). `to_client_custom_agent` forwards `includePowers`. `includeMcpJson` is framework-owned (`_FRAMEWORK_OWNED_AGENT_KEYS` in `apps/bridges_agents.py`): it is refreshed from the app template on every materialize, so a user edit cannot drop it. But the framework does not force it to `false`: a template that omits it materializes as absent. `includePowers` is not framework-owned, so a user hand-edit survives. `to_client_custom_agent` never forwards `includeMcpJson: true`, yet the opt-in still decides which `mcp.json` servers KAS may start, through `kas_settings_mcp.stamp_settings_mcp`. So "not on the wire" is not "not offered".
7. KAS reports offered tools in two places, and released versions differ (`KasMcpReadiness` in `acp/mcp_session_report.py`). Captured 2.22.0 (KAS 0.66.0) lists MCP tools only in the `tools` catalog of a server's `connected` status entry; its `_kiro/tools/didChange` snapshot carries only `builtin` tags.
8. KAS has two routes (`kas_route` in `agent_sdk/kas_bundle.py`): Crew's engine bundle, and kiro-cli's relay. The bundle has two pins of one engine release (`acp/kas_pin.py`): `KAS_PIN_VERSION`, and `KAS_PIN_AL2_VERSION` for a linux-x64 host below glibc 2.28. `host_pin_version()` picks one; `pinned_version()` returns it unless `KIROCREW_KAS_VERSION` overrides it. A session records the engine version per route (`kas_bundle_engine_version`, `kas_relay_engine_version`). `engine_discards_oauth` (`acp/kas_engine_features.py`) already gates a feature on that version, read through `parse_kiro_cli_version`, and treats an unknown version as not known.
9. On Claude Code, #40671 trims a named agent's built-ins to its `tools` rows plus `ToolSearch`. v0.13.1 predates it. `strictMcpConfig` is not set for a named app agent (`acp/client_claude_session.py`), so the user's `~/.claude.json` servers still mount. That is #39117. The lab capture under Evidence shows both on v0.13.1.2.
10. `probe_backend` (`agent_sdk/backend_install.py`) caches for 30 seconds and answers `installed`, `missing` or `unknown`, with a `restart_required` flag.
11. Manifest grants are staged at install and held back on a widening update; some are `explicit_only` (`register_permission_grants` in `apps/point_grants.py`). `APP_SDK_LEVEL` is 13.
12. A continuation reads `backend` from the live record or the run's `state.json` row (`subagent_manager/continuation.py`). `build_continue_impl` takes no requirement.
13. The `kas_subagents` feature preview (#39781, widened by #41972) adds a tool that is not in the agent spec. With the preview on, `KasHarness.session_extras` hands the projected entries to `native_batch` (`acp/kas_native_batch.py`), which mounts the engine's own sub-agent tool, `invoke_sub_agent`, on the session agent's entry, after `to_client_custom_agent` has projected it. So the `spec_not_bounded` rules, which read the spec, cannot see it. Whether a session gets the mount is decided by `engine_subagents_on` (`acp/kas_engine_subagents.py`): it is true only for chat sessions (dashboard, cron and channel keys) of a non-conductor agent, and false for every Kiro Crew sub-agent run, so today no app run gets it. That is a property of one function, not of the bound. `native_batch` can also add a per-agent `subagent_<id>` tool (`kas_subagent_targets.deny_own_agents`), and the sent-entry check below already refuses it, since it is outside the projected set.

### Problem

An app whose agent must use only the app's own tools cannot make that true on every owner's setup.

- On a harness that does not hold the agent to `tools`, the agent keeps the harness's built-ins (shell, file write, web) and the user's MCP servers. Most calls are auto-approved (item 5).
- Even on KAS, a spec with a wildcard, Powers, the user's `mcp.json` or a sub-agent tool is not bounded (item 6), and a tool the engine mounts outside the spec is not bounded by any spec check (item 13).
- The app cannot see or choose its run's harness (item 1). If it could, item 2 would still swap a vanished harness for the floor.

So an app has two choices today: run unsafely, or switch the feature off whenever `agent.acp_backend` is not one it trusts. The AI Game Room app does the second: AI seats stay off on a Claude Code setup, even when KAS is installed and allowed.

## Goals

- An app can ask, per run, for a session whose offered tools are bounded by its agent's `tools` list.
- The app asks for a property. It never names a harness.
- The owner approves it per app, at install or update. Off by default.
- No fallback: not at spawn, not at cold start, not when a queued run starts.
- Governance still decides which harnesses are eligible.
- A harness route claims the property only with a committed live capture and a version floor.
- An app can ask, without spawning, whether `require` would pass.
- No new shell-out per run: step 4 reads the 30-second `probe_backend` cache and `kas_route`; the claim table is static.

## Non-goals

- Changing `agent.acp_backend`, or any harness choice for chats or Bot workers.
- App crons. The cron turn passes no app to the gate today, so it needs its own design.
- Letting an app name a harness (`backend=`). See Alternatives.
- Fixing #39117 inside this RFC. It should land first, as its own small PR.
- A per-crewmate or per-template harness choice (#21910).
- Enforcing anything beyond the agent's own `tools` list (file paths, network, what the app's own MCP server does).
- Protecting the owner from a malicious app.
- Checking at run time the tools a session reports, and refusing out-of-list calls at the hook gate. Both move to a follow-up RFC (see Follow-up).

## Design

### 1. The property

A new closed enum in `providers/mirrors/registry.py`:

```python
class HarnessGuarantee(str, Enum):
    #: FROZEN MEANING. The model is offered only tools in the harness's own
    #: projection of the spec's `tools` list, and nothing outside Kiro Crew's
    #: control (a user settings file, a plugin, a Powers pack) can add one.
    #: A change to this meaning is a new member, never an edit to this one.
    TOOLS_ALLOWLIST = "tools_allowlist"
```

"The harness's own projection" is the set Crew sends, not the literal rows. On KAS it is what `_project_tools` returns: each row expanded by `expand_kiro_cli_tool_names`, so `fs_write` brings `str_replace`, `fs_append` and `delete_file`. A capture compares offered tools with that set.

The property is about tools **offered to the model**, not servers mounted. The meaning is frozen at Phase 1 merge.

### 2. Harness, route, spec and version are checked together

**The spec is bounded.** The host reads the materialized spec, after `apps/bridges_agents.py` has merged the template and user edits. The check runs twice: before spawn, and again at session start on the same spec object that `to_client_custom_agent` projects, so an edit to the agent file between the two (by the owner, or by another run that can write files) cannot widen a run that passed. It refuses with `spec_not_bounded` when:

- `tools` is `"*"`, holds `"*"`, or holds a blanket built-in grant such as `@builtin`;
- `tools` is absent or not a list (KAS would offer nothing: safe, but useless);
- `includeMcpJson` is not exactly `false` (the app template must set it, item 6), or `includePowers` is `true` (a user edit can set it);
- `tools` grants a sub-agent tool (`use_subagent`, or its per-harness names from `CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL` and `expand_kiro_cli_tool_names`);
- `tools` grants a tool-search tool (`tool_search`), until Phase 0 shows it finds nothing outside the bound (open question 1).

**The sent entry is bounded too, not only the spec.** Some tools are added after the spec is projected, so no spec rule can see them. Today that is `invoke_sub_agent`, which `native_batch` mounts under the `kas_subagents` preview (item 13). A `require` run never gets such a tool, and this does not rest on `engine_subagents_on` staying false for sub-agent runs:

- On the `require` path, `KasHarness.session_extras` tells `native_batch` to mount nothing on the session agent's entry, whatever the preview and `engine_subagents_on` say.
- At session start, before the first prompt, the host compares the session agent's entry in the final `customAgents` batch, after `native_batch`, with the set `_project_tools` returned for the spec. Any tool id outside that set, `invoke_sub_agent` or any later engine-mounted tool, closes the session and refuses the run with `spec_not_bounded`.

**The harness route declares it, with evidence.** A new table beside `PROJECTIONS`, not a field on `McpProjection`, because built-ins are not MCP:

```python
@dataclass(frozen=True)
class GuaranteeClaim:
    guarantee: HarnessGuarantee
    min_version: tuple[int, int, int]  # lowest engine version a live capture passed on
    fixtures: tuple[str, ...]          # committed captures under test/fixtures/acp_frames/

# Keyed by (harness id, route), e.g. (ACP_BACKEND_KAS, "bundle").
HARNESS_GUARANTEES: dict[tuple[str, str], tuple[GuaranteeClaim, ...]] = {...}
```

Empty is the default. A route nobody has captured enforces nothing. The KAS bundle and the KAS relay are separate keys, because they run different engine builds with different version schemes. This RFC claims only the bundle key.

**The engine version is at or above the floor.** Every version is read through `parse_kiro_cli_version`, as `engine_discards_oauth` does. An unknown or unparsable version does not qualify. It reads the first `major.minor.patch` and drops any suffix: `KAS_PIN_VERSION` `0.66.117-node22.23.4` and its `-al2` twin both read as `(0, 66, 117)`, so the two pins of one release share one floor. It is checked twice:

- Before spawn: `pinned_version()`. That is the version Crew will run on the bundle route, including a `KIROCREW_KAS_VERSION` override, so an override below the floor is refused. A claim needs this check, which is why only the bundle route is claimed: on the relay route the version is not known before spawn.
- At session start, before the first prompt: the session's own `kas_bundle_engine_version`. A session on the relay route, or with an unknown or below-floor version, is closed and the run refused with `no_enforcing_harness`. The `spec_not_bounded` rules run again here, on the projected spec; a failure closes the session and refuses the run with `spec_not_bounded`.

Proposed first claims, each only if Phase 0 passes:

| Harness, route | Claim | Why |
|---|---|---|
| KAS, bundle | yes, after Phase 0 | `tools` is always a list or `"*"` in `_meta.kiro.customAgents`; `"*"` is refused by the spec rule. Crew pins the engine. |
| KAS, relay | no | Same wire, but the engine is whatever kiro-cli unpacked: its version is not known before spawn, and kiro-cli can move it with no Crew change. It waits for the follow-up RFC's run-time check of the tools a session reports. |
| kiro-cli v2 | no | Sends no tool report, is retiring, and a stored Kiro CLI pick moves onto KAS (H1). |
| Pi | no | Its registry entry says "held to the spec's tools allowlist", but it does not meet the Phase 1 test: its capture header reads `"agent_version": "0.0.34 (pi 1.4.0)"`, which does not parse to its floor `PI_MIN_VERSION = (0, 99, 2)`, and the capture is about MCP mounting, not built-ins. It can join with its own capture. |
| Claude Code | not until #39117 is released and captured | Item 9. |
| Every other harness | no | Not captured. |

**What the guarantee rests on.** This RFC does not check, at run time, the tools a session reports. The guarantee rests on three things only: a committed live capture for that harness, route and version; the version floor, checked before spawn and at session start; and a re-capture rule. A newer engine above the floor is trusted until it is re-captured. On the bundle route Crew moves the engine only by changing `KAS_PIN_VERSION`, and **the PR that bumps `KAS_PIN_VERSION` must re-run the Phase 0 capture** and update the fixtures (a Phase 1 test fails when the pin has no capture at or below it). On the relay route kiro-cli can move the engine on its own, with no version known before spawn, which is why this RFC does not claim it.

### 3. The owner's consent is a manifest grant

```json
"permissions": { "harnessRequirements": ["tools_allowlist"] }
```

- A new family, registered with `register_permission_grants(..., explicit_only=True)`: staged at install, held back on a widening update, like `kiroOperations`. A stored set written before the family existed approves nothing in it. The grant goes with the installed app; a later app with the same name starts with nothing.
- The install and update screen says: "This app may ask that its background agents run only where they are held to their tool list. Such a run may use a harness other than your default."
- An unknown name is kept on disk and grants nothing; it is not a load error. `APP_SDK_LEVEL` goes to 14.

### 4. The app call and the host's steps

```python
run_id = await ctx.spawn.run(task, "player", require=["tools_allowlist"])
```

`require` is keyword-only. The SDK passes it to the impl only when non-empty, as it does `keep`. When it is non-empty, the host:

1. Checks every name is a `HarnessGuarantee` value. Else `unknown_requirement`.
2. Checks every name is approved in the app's grants. Else `requirement_not_enabled`.
3. Checks the materialized spec is bounded. Else `spec_not_bounded`.
4. Takes `selectable_backends()`, after any Kiro CLI pick is moved onto KAS. It drops a harness the probe reports as `missing` or `unknown`, or as `installed` with `restart_required`, one whose route has no claim for every name, and one whose pre-spawn version does not qualify.
5. If none is left, refuses `no_enforcing_harness`.
6. Picks `agent.acp_backend` (after the Kiro CLI move) if it is a candidate, else the first candidate in registry order. Today the only claimable harness is KAS, on its bundle route.
7. Spawns with `backend=<pick>` and `require`. Never consults `agent.acp_failover`.

The shared "which backends can start" check moves out of the dashboard handler into `agent_sdk/`. Nothing in `apps/` imports a handler module.

**Strict means no swap, and the refusal sits before the gate.** H3 says `select_provider_backend` never raises and is the one resolution gate. So this RFC adds a second pre-gate value, shaped like the Bot pin. A pre-gate check, `required_backend_pick`, re-runs steps 2-5 for the picked harness at cold start, raises `RequiredHarnessUnavailable` (code `no_enforcing_harness`) before any process exists, and otherwise hands the gate a `StartableRequiredPick`. The gate returns that value as is, exactly as it does a `StartableCrewPin`, and still never raises. A queued run meets the same check when it leaves the queue, because it cold-starts through it. The Phase 2 PR amends H3 and H17 to name this second pre-gate value, with a test like `test_the_gate_never_raises_for_a_crew_pin`.

**`require` never overrides a Bot's pin.** H17 has every `select_provider_backend` caller pass `crewmate_backend_pin` as `crew_backend`. When an app run starts from a Bot's session whose pin is a harness with no claim, `require` refuses with `no_enforcing_harness`; it does not ignore or replace the pin. When the pin is a claimed harness, that harness is the only candidate. `test_every_select_provider_backend_call_names_crew_backend` grows to check that every such caller also passes the required pick, and a new test pins "a pin with no claim refuses".

**LTS.** A `StartableRequiredPick` for KAS still meets the LTS per-session refusal after the gate (`lts_boot.refuse_unstartable_kas`). On the `require` path its `KasUnavailable` is reported to the app as `no_enforcing_harness`, with the LTS reason in the audit row, and nothing falls back.

**What the app sees.** A refusal raises `SpawnError` with its `code` only. The per-harness reasons go only into the owner's audit row, through `_audit_spawn_denied`. A started run records `require`, the pick, the route and the engine version in its audit row.

The model rule is unchanged. If the app's `model` pin cannot run on the pick, the existing withheld-model path applies.

### 5. A check before the run

`ctx.spawn.can_require(names, agent)` runs steps 1-5 without spawning, and returns `ok` or the same code, with no per-harness detail. AI Game Room needs it to show "AI seats off" before a game.

### 6. Continuing a run

The run's stored `state.json` row gains `require`, beside `backend`. A row with no field means "not required", so older rows read as today.

A continue of a row with `require` re-checks steps 2-5 for that run's harness, through the same pre-gate check `required_backend_pick` that spawn uses. It ships in the same PR as `require=` (Phase 2), so there is never a release where such a run cannot continue, or continues unchecked. If the harness no longer qualifies, the continue is refused (`no_enforcing_harness`). It never moves the conversation. A runtime ceiling is seen after the next restart, as for every other start (item 4).

## Migration plan

Each phase is one PR and can be dropped on its own. Each later phase's tests use a fake claim table, so none waits on Phase 0's verdicts.

**Before Phase 1: #39117, as its own PR.** Set `strictMcpConfig` for a named agent with a non-empty `tools` list.

**Phase 0: capture.** Add `scripts/capture_tools_allowlist.py`, modelled on `scripts/capture_acp_compaction.py`, and `docs/guides/tools-allowlist-capture.md` beside `acp-compaction-capture.md`. The runbook holds the spec variants (default, user and workspace `mcp.json` servers, absent and true `includeMcpJson`, wildcard, Powers, sub-agent, tool search, `kas_subagents` preview on), config paths, output shape and the dated verdict table per harness, route, version and variant. On KAS it reads offered tools from both sources `KasMcpReadiness` reads (the `connected` status `tools` catalog and the `didChange` tags), and states per version which one carried them. The `includeMcpJson` variants also record which `mcp.json` servers the settings stamped by `stamp_settings_mcp` let KAS start, not only what the wire carries. It also asks the model to call one tool outside the bound and records the result.
Exit: committed fixtures under `test/fixtures/acp_frames/kas/`, and the verdict table in the runbook. A route or variant marked "no" is not claimed.

**Phase 1: claims.** Blocked on open question 1. Add `HarnessGuarantee`, `GuaranteeClaim`, `HARNESS_GUARANTEES`, the pick order and the tests. No reader yet.
Exit, as tests:
- Each claim names fixtures that exist, and replaying them shows no offered tool outside the probe agent's projected set.
- Each claim's `min_version` equals the lowest `parse_kiro_cli_version` of its fixtures' `agent_version`. A header that does not parse fails the test.
- The bundle claim has a fixture at or below `parse_kiro_cli_version` of each pin in `KAS_PIN_MANIFESTS`, so a pin bump without a re-capture fails.
- One test per `spec_not_bounded` rule.

**Phase 2: the app path and continue.** The grant family, `APP_SDK_LEVEL` 14, `require=` on `SpawnSDK.run` and `build_spawn_impl`, `can_require`, the pre-gate `required_backend_pick` and the H3/H17 amendment, the session-start version check, the no-mount flag to `native_batch` and the sent-entry check, the stored `require` field and the continue re-check through `required_backend_pick`, the audit fields and the `docs/app-kit/api-reference.md` section. Continue is in this PR, not a later one, so apps that continue runs (planners, pollers) never lose that ability. It adds one call site, not a new check.
Exit, as tests:
- No `require`: spawn kwargs are byte-identical to today.
- No grant: `requirement_not_enabled`, nothing spawned, one audit row.
- Spec with `"*"`, `includeMcpJson` absent, `includePowers: true`, `use_subagent` or `tool_search`: `spec_not_bounded`.
- Standard build, owner on Claude Code, KAS selectable and `installed` on the bundle route, pin at or above a fake floor: the run starts on KAS.
- Standard build, KAS denied by governance: `no_enforcing_harness`.
- LTS build, KAS probe `unknown`: `no_enforcing_harness`.
- Pin, override or session engine version below the floor, or unknown: `no_enforcing_harness`.
- Picked harness made unselectable before cold start: `RequiredHarnessUnavailable` before the gate; the gate is never asked to degrade; nothing starts on the floor.
- Run queued, then its harness removed: refused when it leaves the queue.
- The shared startable check in `agent_sdk/` is called on this path, and `agent.acp_failover` is not read.
- A continue after restart reads `require` from the stored row, starts on the same harness when it still qualifies, and refuses with `no_enforcing_harness` when it drops out. It never moves the conversation. A row without `require` continues as today.
- `can_require` answers as `run` would, and spawns nothing.
- The agent file widened between spawn and session start: `spec_not_bounded` at session start, before the first prompt.
- `kas_subagents` preview on, and `engine_subagents_on` patched to answer true for the run: a `require` run's session agent entry carries no `invoke_sub_agent`, and the run starts. A second test makes the batch add a tool outside the projected set anyway: `spec_not_bounded` at session start, before the first prompt.
- App run from a Bot session pinned to a harness with no claim: `no_enforcing_harness`; the pin is not replaced.
- LTS build, KAS picked, `refuse_unstartable_kas` raises: the app sees `no_enforcing_harness`, nothing starts.

**Phase 3: Claude Code joins (after #39117 is released).** Capture it with the Phase 0 script and add its claim. No app changes.

### Rollout

- Off by default. Nothing changes until an app declares the grant, the owner approves it, and the app passes `require` (after checking `APP_SDK_LEVEL` 14 or more).
- The AI Game Room app is the first user. It calls `can_require` before a game, keeps AI seats off with the code as the reason when it fails, and passes `require=["tools_allowlist"]` for AI-player runs.

## Backward compatibility

- Optional additions: one keyword argument, one SDK method, one manifest grant family, one enum, one claim table, one stored-row field with a "not required" default, one pre-gate value.
- An app that does not pass `require` sends exactly today's spawn kwargs.
- `SpawnImpl` stays `Callable[..., Awaitable[str]]`. On an old impl, `require=` raises `TypeError`, which `SpawnSDK.run` turns into a `SpawnError` with an empty `code`. Nothing runs unbounded, but the app cannot tell "old host" from other faults by the error. The `APP_SDK_LEVEL` check is the only signal, so apps must make it.
- A manifest that declares `permissions.harnessRequirements` installs on an older host, which ignores the key.
- Downgrade: an older gateway ignores the stored `require` field and would continue such a run unchecked. This is stated in the upgrade notes.

## Security considerations

### Threat model

- **Protected against: a prompt-injected model in an app run.** Game moves, web pages or tool results may carry instructions. The bound limits what the model is offered, whatever it reads.
- **Not protected against: a malicious app.** An app's in-gateway hooks run inside the gateway (`apps/manifest.py`), and its own MCP server can do anything its code does. `require` only ever narrows a run.
- **Who decides: the owner,** at install or update. The app cannot grant itself the family or pick a harness.
- **What the app learns:** a refusal code only. `can_require` returns the same.

### Other properties

- **Governance still wins, with no fallback.** Candidates come from `selectable_backends()`. Failover is not consulted, and the pre-gate check stops the cold-start swap to the floor.
- **Claims rest on captures and a floor, not on a run-time check.** A unit test of Crew's output cannot see what the engine offers, so it is not evidence. An engine above the floor is trusted until re-captured. The bundle route moves only with a reviewed pin bump that must re-capture. The relay route can move under Crew, so it is not claimed until the follow-up RFC's run-time check exists.
- **Probe staleness.** A 30-second-old "installed" for a removed harness ends in a refused start, not a swap.

## Alternatives considered

### Enforce the list at Crew's own tool gate only

Every permission request of an app run reaches `_consult_gate`. But a call the harness approves by itself never reaches it, and the gate cannot stop a tool from being offered. It is a useful second layer, moved to the follow-up RFC because matching a call to a spec row per harness (`ToolCall.from_event` against `@server`, `@server/tool`, built-in families, Claude names) needs its own design.

### Fix #39117 first, declare Claude Code, and stop there

The cheapest useful step, and this RFC asks for it first. It leaves four gaps: owners on other harnesses have no bound; the app still cannot know its run's harness; a spec can still widen the list on any harness; and a harness or version has no way to join or drop out. `require=` exists for those.

### Let the app name a harness: `ctx.spawn.run(backend="kiro")`

The local prototype. Rejected: the app could move its run to a less bounded harness with no owner say, and every app would hard-code a list of "safe" harnesses that goes stale.

### An `apps.<name>` config key for consent

Rejected: keyed by name, it survives uninstall, so a later app with the same name inherits it, and the owner sees nothing at install.

### Others, one line each

- Tell the owner to change `agent.acp_backend`: moves every chat and worker to fix one app.
- A crewmate or agent harness pin (#21910): read for a Bot's own worker, not an app run, and it names a harness.
- The app starts kiro-cli itself: skips admission, governance, audit, budget and model settings.
- A boolean `enforce_tools=True`: a second property would need a second flag everywhere.

## Follow-up RFC

"App runs check the harness's reported tools." It adds a run-time check of a session's offered tools, read from both KAS sources per version, that stops a run with `offered_tools_exceeded` before its first prompt or mid-run. It adds the hook-gate second layer, with a stated rule for matching a call to a spec row on each harness. With that check, the KAS relay route can be captured and claimed.

## Evidence

One live capture, taken 2026-11-24 in a throwaway copy of Kiro Crew v0.13.1.2 with its own home directory, gateway and port, `agent.acp_backend` set to `claude`, and the in-product recorder on (`KIROCREW_ACP_RECORD_FRAMES`). v0.13.1.2 predates #40671. Harness: claude-agent-acp 0.107.3 (claude-agent-sdk 0.3.325), Claude Code on Bedrock.

Setup:

- A probe app with `permissions.spawn` and one stdio MCP server, `note`, with one tool, `probe_note`. Its agent `tools-probe-agent` declares `"tools": ["@tools-probe:note"]`, the same in `allowedTools`, and `includeMcpJson: false`.
- The lab home's `~/.claude.json` holds one user-scope stdio server, `lab-user`, with one harmless tool, `lab_echo`.
- One app run: `ctx.spawn.run(task, "tools-probe-agent", silent=True)`. The task asks the agent to list every tool it can call, call `probe_note`, then call `lab_echo` only if it exists.

| Case | Harness, version | Offered outside `tools` | Call outside `tools` |
|---|---|---|---|
| Named app agent, user MCP server present | Claude Code via claude-agent-acp 0.107.3, Crew v0.13.1.2 | Yes: the user's `mcp__lab-user__lab_echo`, and 28 built-ins (`Bash`, `Write`, `Edit`, `WebFetch`, `Agent`, `ToolSearch` and others) | Yes: `ToolSearch`, then `mcp__lab-user__lab_echo`. Each `session/request_permission` was approved and the tool ran. |

What each fact rests on:

- Crew's `session/new` for the run carried `_meta.claudeCode.options: {}`: no `tools`, no `strictMcpConfig`, one MCP server (the app's). The background helper session in the same gateway carried `tools: []` and `strictMcpConfig: true`, so the recorder and the options path worked.
- The user server's own log shows `tools/list` at session start and one `tools/call` with text `outside`.
- The agent listed the tools in its reply. Its list matches the two facts above.
- Crew logged for the run: "this backend does not apply the spec's `tools` list to its built-in tools ... the list still limits its MCP servers". The second half is false for user-scope servers.

Not captured: main at `37f635e3e1`. There #40671 trims the built-ins (item 9); the user server still mounts by code reading, since `strictMcpConfig` is not set for a named app agent. The recording is not committed: an RFC changes no code, and Phase 0 owns the committed fixtures.

## Open questions

1. **Tool search under `TOOLS_ALLOWLIST`.** Does the frozen meaning count a search tool that finds only tools inside the bound? Until Phase 0's tool-search variant shows that on KAS, a spec granting tool search is refused (`spec_not_bounded`). Phase 1 freezes the meaning, so it is blocked on this answer.

Decided in review: the KAS bundle route only (the KAS relay route and kiro-cli v2 are not claimed); a version floor with no ceiling, plus the re-capture rule on a `KAS_PIN_VERSION` bump; no owner setting for the pick order.

## Appendix: refusal codes

| Code | When | Where the detail goes |
|---|---|---|
| `unknown_requirement` | a name is not a `HarnessGuarantee` value | audit row |
| `requirement_not_enabled` | the owner has not approved the name | audit row |
| `spec_not_bounded` | the materialized spec widens its `tools` list, before spawn or at session start, or the sent entry holds a tool outside the projected set | audit row names the rule or the tool |
| `no_enforcing_harness` | no candidate is left, at spawn, cold start, session start, a queued start or a continue | audit row lists each harness and why |
