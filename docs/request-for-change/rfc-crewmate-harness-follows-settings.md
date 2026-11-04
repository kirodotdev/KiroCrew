---
title: A crewmate's harness follows Settings → Agent Harness when the person has not picked one
status: draft
author: iamwhatever, with kirocrew-worker
created: 2026-11-04
last-audited: 2026-11-04
audited-at: 3609b8dd57
doc-pr: null
implementation-prs: []
tracking-issues: [21910]
supersedes: []
superseded-by: []
---

# RFC: A crewmate's harness follows Settings → Agent Harness when unset

- Status: draft. The owner decided the direction on 2026-11-04: crewmates can run on different harnesses, and a crewmate the person has not pinned follows Settings → Agent Harness (`agent.acp_backend`). The owner also decided O1 to O5 in section 5.0 the same day. This document records how, so the implementation PRs have a design for the First Principles and Design lanes to check against: it changes which harness existing crewmates run on.
- Tracking issue: [#21910](https://github.com/kirodotdev/KiroCrew/issues/21910)
- Related: [harness-parity](../system-specs/modules/harness-parity.md) (H3, H6, H13, H17), [rfc-crew-enterprise-edition](rfc-crew-enterprise-edition.md) (D6, the enterprise KAS floor), [rfc-subagent-backend-selection](rfc-subagent-backend-selection.md) and [rfc-chat-backend-selection](rfc-chat-backend-selection.md) (the other per-session harness choices), decision record [2026-10-28 a Kiro CLI choice runs on KAS](../decisions/2026-10-28-a-kiro-cli-choice-runs-on-kas.md).
- Measured at: `origin/main` `3609b8dd57`. Every symbol below was checked at that commit.

## 1. Summary

Today a crewmate's own chat and the workers it opens run on `agent.member_acp_backend`, a field with default `"kas"` and no Settings control, while the same crewmate's Slack threads, cron jobs and spawned sessions run on `agent.acp_backend`. One crewmate can run on two harnesses, and the person's Settings pick does not reach the crewmate's chat.

This RFC makes the crewmate route resolve, in order: the crewmate's own pin `agents.<name>.acp_backend`, then a legacy explicit `agent.member_acp_backend`, then `agent.acp_backend`. A Kiro CLI value maps to KAS on the crewmate route, as `member_acp_backend` already does. An inherited value that cannot carry a crewmate falls back to KAS, logged and shown to the person as a dashboard warning. Settings → Agent Harness shows which harness crewmates actually run on and where that value comes from. The crewmate harness picker offers only harnesses crewmates can run on. A pin that cannot start still refuses (H17). The enterprise KAS floor and governance harness locks still win because every value still crosses the one selection gate.

The change is made at one place: the loader resolves `cfg.agent.member_acp_backend` from `agent.acp_backend` when it is not stored. The 20+ readers of that field keep reading it and need no edit.

## 2. Motivation

### 2.1 Current state (verified at `3609b8dd57`)

- `AgentConfig.member_acp_backend` has default `"kas"` (`config/sections_agent.py`). No Settings page or crew editor field writes it.
- `_read_backend_choice` (`config/loader_builders.py`) resolves both harness fields. For `member_acp_backend` a stored `"kiro"` becomes KAS unconditionally (its `_MEMBER_HARNESS_KEY` branch, logged once by `_note_member_remap`). For `acp_backend` a stored `"kiro"` becomes `kiro_cli_pick_backend()` (`agent_sdk/backends.py`) only off the KAS bundle route: KAS once the gateway probe confirmed kiro-cli serves v3 and KAS is selectable, else the v2 Kiro CLI launch.
- `members.select_provider_backend` (`members_parts/member_sessions.py`) is the per-session half of the one selection gate. Precedence: `spawn_run(backend=)` override, then the crewmate pin (`crew_backend`), then the member route `agent.member_acp_backend` for a crewmate DM or crewmate worker key (the `is_member_session_key` / `is_crewmate_worker_session` arm), then the configured default `agent.acp_backend`.
- `agent_capabilities.member_backend_warning` already checks both arms, because a crewmate's Slack threads, cron jobs and spawned sessions take the default arm.
- The pin `agents.<name>.acp_backend` exists (`crewmate_harness.py`). An unstartable pin refuses that crewmate's DM and workers with `crewmate_harness_unavailable` before the gate (H17). A stored `kiro` pin resolves by `kiro_cli_pick_backend()` off the bundle route (`CrewBackendPin.resolve`).
- The pin has a UI, but only behind a Feature Preview: the crewmate profile's harness row (`harnessRowOn`) in `website/src/pages/members/CrewProfileSettings.tsx`, shown when `PREVIEW_CREWMATE_HARNESS` (`crewmate-harness`, `website/src/utils/previewFlags.ts`) is on, from #25485. The crew editor dialog (`CrewEditorNextDialog`) has no harness field. The record already carries `harnesses`, `worker_harnesses` and `inherited_harness` (`dashboard/agent_admin/crew_records.py`).
- Why the split exists: kiro-cli v2 cannot mount per-session dispatch tools, so it is not in `ACP_BACKENDS_MEMBER_DISPATCH` (`agent_sdk/backends_members.py`). Crewmates were put on KAS to get those tools.
- Capability sets (`agent_sdk/backends_members.py`):

| Set | Members | What it gates |
|---|---|---|
| `ACP_BACKENDS_MEMBER_CAPABILITIES` | kiro, kas, claude, codex, deepseek, pi | a crewmate chat with a saved spec starts (`capability_harness_unsupported`) |
| `ACP_BACKENDS_MEMBER_DISPATCH` | claude, kas, codex, opencode, goose, deepseek, pi | the session-control tools mount, and the operating-mode block is injected (`context._member_backend_can_dispatch`) |
| `ACP_BACKENDS_MATE_WORKER_KIT` | kas, claude | a crewmate's worker can start (`mate_worker.worker_kit_backend_refusal`) |

### 2.2 Problems

1. The person's Settings pick does not reach their crewmates' own chats. A person on Claude Code still gets KAS crewmates, with no control saying so.
2. One crewmate runs on two harnesses: its DM on `member_acp_backend`, its Slack and cron turns on `acp_backend`. Memory, model lists and tool behaviour differ between them.
3. The refusal texts point at a field nobody can see. `worker_kit_backend_refusal` and the `capability_harness_unsupported` text in `dashboard/chat_turn/turn_failures.py` tell the person to "set agent.member_acp_backend".
4. The per-crewmate pin that would fix this is hidden behind a preview flag and absent from the crew editor.

## 3. Goals

1. A crewmate with no pin runs its DM, its workers, and its Slack, cron and spawned sessions on one harness, the one Settings → Agent Harness names, whenever that harness can carry a crewmate.
2. A person who explicitly stored `agent.member_acp_backend` sees no silent change, and Settings shows them that value and where it comes from.
3. Kiro CLI keeps meaning KAS on the crewmate route, with the `kas_bundle` and `kiro_cli_pick_backend()` semantics intact.
4. The enterprise KAS floor (D6) and governance harness locks win over every value in this design.
5. The crew editor and the crewmate profile show and write the pin through one picker, offer only harnesses crewmates can run on, and say before a pick which harnesses refuse workers.
6. A fallback to KAS is shown to the person, not only logged.

## 4. Non-goals

- No change to `spawn_run(backend=)` overrides, ordinary (non-crewmate) chats, the per-chat picker of `rfc-chat-backend-selection`, or `acp_failover`.
- No new capability grant. Adding codex (or any harness) to `ACP_BACKENDS_MATE_WORKER_KIT` is a follow-up that needs its own live capture (H6). See section 5.4.
- No removal of `agent.member_acp_backend` in this RFC. Section 5.2 deprecates it; deletion is a later decision.
- No change to the pin's own resolution, including a stored `kiro` pin's `kiro_cli_pick_backend()` behaviour.

## 5. Design

### 5.0 Owner decisions (2026-11-04)

- **O1.** An explicitly stored `agent.member_acp_backend` keeps working with its old value. Settings → Agent Harness shows the harness crewmates actually run on and where that value comes from, so it is no longer hidden (5.2).
- **O2.** When the inherited Settings value is one crewmates cannot run on, crewmates fall back to KAS and the person sees a dashboard warning naming the harness and the fallback, not only a log line (5.3).
- **O3.** Harnesses crewmates cannot run on are not offered in the crewmate harness picker. A hand-edited pin this gateway cannot start still refuses with `crewmate_harness_unavailable` (H17), as today (5.7; see the note there on hand-edited pins that can start).
- **O4.** The enterprise KAS floor (D6) and governance harness locks win, unchanged (5.5).
- **O5.** The crew editor gets a harness field. It is built on the existing preview-gated profile row from #25485, which graduates out of Feature Preview; there is one picker, not two (5.7).

### 5.1 Resolution order

For a crewmate DM key or a crewmate worker key, the harness is the first of:

| Tier | Source | Unusable value |
|---|---|---|
| 0 | `spawn_run(backend=)` override (unchanged) | degrades, H3 |
| 1 | pin `agents.<name>.acp_backend` (unchanged) | refuses, H17 |
| 2 | stored `agent.member_acp_backend` (legacy, 5.2) | degrades, H3 (unchanged) |
| 3 | `agent.acp_backend`, crewmate-mapped (new) | falls back to KAS, logged (5.3) |

Tiers 0 and 1 and the gate itself (`select_provider_backend`) are not edited. Tiers 2 and 3 both produce the one in-memory value `cfg.agent.member_acp_backend`, which the gate's member arm already reads.

**Where the change lives.** In `_read_backend_choice`, the `member_acp_backend` key with no stored value (absent or `""`) stops reading the field default `"kas"`. It reads the crewmate-mapped value of `agent.acp_backend` instead. The loader resolves `acp_backend` first (`_build_agent_config` in `config/loader_builders.py` builds both in one call; the order is made explicit), then derives the member value from it. The degraded-config path in `config/loader_pipeline.py` gets the same order. Every reader of `cfg.agent.member_acp_backend` (the gate's member arm, `context._member_backend_can_dispatch`, `mate_worker.crewmate_worker_backend`, `crew_records`, the model and effort slot readers, `cli_doctor._selected_roles_run_kiro_cli`, the gateway KAS floor in `slack/gateway_runtime/process_entry.py`) then follows Settings with no edit of its own.

**The crewmate mapping** of a resolved `agent.acp_backend` value *v*:

1. *v* is the Kiro CLI id (`""`) → KAS, crossing `resolve_selected_backend` with no warning. This holds on both routes. Off the bundle route a confirmed v3 kiro-cli already resolved the pick to KAS, and an unconfirmed one resolved it to the v2 launch, which cannot mount dispatch tools; either way the crewmate gets KAS, as a stored `member_acp_backend: "kiro"` does today. On the bundle route (`agent.kas_bundle: true`) the main harness stays on kiro-cli and the crewmate gets KAS on Crew's bundled engine, which is again what a stored member `"kiro"` gives today. `kiro_cli_pick_backend()` is not called on this route and is not changed. A Kiro CLI id that is the H3 floor of an unusable `acp_backend` maps the same way: the crewmate result is the member default either way.
2. *v* is in the new set `ACP_BACKENDS_CREWMATE_INHERITABLE` (5.3) → *v*.
3. Otherwise → KAS, with one info log per process naming the stored `agent.acp_backend`, the harness it resolved to, and the missing capability.

When KAS itself is not selectable (an oss policy denies `kas`), steps 1 and 3 cross the gate as "no choice" does and take the floor silently, which is today's behaviour for the `"kas"` default. The operating-mode block is then withheld by `_member_backend_can_dispatch`, as today.

**Saves never freeze the inherited value.** `_restore_stored_harness_fields` already writes every harness key back verbatim from disk and drops a key the file does not hold, so a full-document write cannot persist the derived member value. Phase 1 adds a test that pins this.

**Live re-resolution.** `_reconcile_live_config_with_kiro_verdict` moves a stored Kiro CLI `acp_backend` onto KAS after the probe. The crewmate value derived from that pick is KAS before and after, so nothing moves. Any other path that re-resolves `acp_backend` on the live config must re-derive the member value in the same step; Phase 1 names each one in a test.

### 5.2 An explicitly stored `agent.member_acp_backend`

A stored non-empty value keeps winning over `agent.acp_backend` (tier 2), with today's resolution, including `"kiro"` → KAS (O1). Nobody who set it sees a change. A stored `"kas"` counts as explicit too: Kiro Crew cannot tell a hand-written `"kas"` from one an older build or a support instruction wrote, so it does not guess.

**Settings shows it (O1).** Settings → Agent Harness (`website/src/pages/developer/AgentBackendTab.tsx`) gets a read-only "Crewmates run on" line under the harness picker. It names the harness the crewmate route resolves to and its source, one of:

| Source shown | When |
|---|---|
| "Same as above" | tier 3, the inherited value is used |
| "KAS, because <harness> cannot run crewmates" | tier 3 fell back (5.3); shown as a warning |
| "Set by agent.member_acp_backend in config.json" | tier 2; with the one-line fix "remove it to follow this setting" |
| "KAS, the enterprise harness" | enterprise build, when D6 decides it |

Per-crewmate pins are not listed there; the line says "crewmates with their own harness keep it" and links the crewmates page. The value comes from one new read-only field, `crewmate_harness: {backend, source}`, on the config response the Agent Harness panel already loads. The resolver of 5.1 returns `source` beside the value, so the panel never re-derives it.

It is deprecated: the field's schema description says so and names the pin and Settings → Agent Harness as the replacements, and `kirocrew doctor` adds the same tier-2 line as an advisory row. `member_acp_backend_declared()` (`config/loader_document.py`) already answers "stored", so no new disk read is needed.

### 5.3 An inherited value that cannot carry a crewmate

`ACP_BACKENDS_CREWMATE_INHERITABLE` is a new named set in `agent_sdk/backends_members.py`, written as an explicit literal (H6), re-exported like the others: kas, claude, codex, deepseek, pi. It is the harnesses in both `ACP_BACKENDS_MEMBER_CAPABILITIES` and `ACP_BACKENDS_MEMBER_DISPATCH`, so an inherited harness can both load a saved crewmate spec and mount the dispatch tools. kiro is left out because step 1 maps it to KAS. opencode and goose are left out because they cannot load a saved spec. strands and any harness in neither set are left out. A test asserts the literal equals that intersection minus kiro, so a later grant to either parent set is an explicit decision here too.

An inherited value outside the set **falls back to KAS with a logged reason and a visible warning** (O2). A pin outside the startable set **refuses** (H17, unchanged). The reason for the difference:

- The pin is the person's choice for this crewmate. Running it elsewhere is what H17 forbids.
- An inherited value is the person's choice for ordinary chats. They may have picked goose for good reasons that say nothing about crewmates. Refusing would break every crewmate the moment Settings moves, and H1/H3 forbids a refusal reaching `agent.acp_backend`.
- Falling back to KAS is today's behaviour for these people, so the fallback is never worse than now.

The fallback is shown to the person, not only logged (O2):

- **Settings → Agent Harness**: the "Crewmates run on" line (5.2) turns into a warning, "Crewmates can't run on goose, so they run on KAS", the moment the person picks such a harness, before they save.
- **Dashboard notice**: one dismissible notice, raised when the gateway loads a config whose tier-3 result fell back, naming the Settings harness and KAS, with a link to Settings. It is keyed on the pair (Settings harness, fallback), so it reappears if the person later picks a different unsupported harness, and does not repeat on every load.
- **Crewmate surfaces**: the crew record's `inherited_harness` (which already names what Inherited resolves to) plus a new `inherited_reason`, shown on the picker's Inherited option ("Inherited: KAS, because goose cannot run crewmates").
- **Log**: one info line per process, for support.

The warning is never a refusal: crewmates keep working on KAS.

### 5.4 Worker chats on a harness outside `ACP_BACKENDS_MATE_WORKER_KIT`

A crewmate that inherits codex, deepseek or pi chats on that harness, and a worker it opens is refused before spawn with `capability_harness_unsupported` from `worker_kit_backend_refusal` (raised in `dashboard/session_control_parts/session_create.py`). This is today's behaviour for a stored `member_acp_backend: "codex"`. It is a capability refusal after selection, not a selection refusal.

The refusal text changes. It stops naming `agent.member_acp_backend` and names the two fixes a person can reach: pin this crewmate to KAS or Claude Code in its profile, or change Settings → Agent Harness. The matching text in `dashboard/chat_turn/turn_failures.py` is reworded the same way.

The follow-up to add codex: record a live worker-kit capture on codex-acp (a worker base plus a crewmate file that excludes a tool and adds a prompt, as `test/fixtures/acp_frames/kas/worker-kit-live.jsonl` does for KAS), commit it under `test/fixtures/acp_frames/codex/`, and add `ACP_BACKEND_CODEX` to `ACP_BACKENDS_MATE_WORKER_KIT` with that capture cited in the comment. It is its own PR, gated only on the capture. deepseek and pi follow the same path later.

Not chosen: requiring tier-3 values to be in `ACP_BACKENDS_MATE_WORKER_KIT` too. That would keep codex users' crewmates on KAS, which is the problem this RFC exists to fix.

### 5.5 Enterprise edition and governance locks

- **D6 floor.** In an enterprise build the floor is KAS and Kiro CLI is selectable only when a remote rule names it. Step 1 maps an allowed Kiro CLI pick to KAS, and steps 1 and 3 land on KAS, so the crewmate route lands on the enterprise floor. A KAS that cannot start on its route still stops the gateway at boot (D6), and nothing here falls back to kiro.
- **Per-session KAS refusal.** The open enterprise work in [#29284](https://github.com/kirodotdev/KiroCrew/pull/29284) refuses a crewmate session on KAS when the kiro-cli that hosts it is unavailable. This design produces KAS on the same paths, so that refusal applies unchanged. Either PR can land first.
- **Governance harness locks.** Policy narrows `selectable_backends()`. Every tier still crosses either `resolve_selected_backend` (tiers 0, 2, 3) or the pin's pre-gate check (tier 1), and both read `selectable_backends()` per call. A locked-out inherited harness degrades through the gate as any explicit value does; a locked-out pin refuses as today.
- The edition is read only through `kiro_crew.edition.is_enterprise()` inside the existing gate. This design adds no edition read.

### 5.6 Harness-parity compliance (`AUTOSDE.yaml` rule `harness-parity`)

| Id | How the design complies |
|---|---|
| H1/H3 | Kiro stays the oss floor. Tier 3 adds no refusal: an unusable inherited value degrades, with a logged reason. The gate does not raise. No refusal reaches `agent.acp_backend`, `agent.member_acp_backend` or `spawn_run(backend=)`. |
| H13 | The kiro construction path does not change. Non-crewmate sessions take `configured_default` exactly as now. The new code is input derivation in the loader for the member field, in the same place the existing `"kiro"` → KAS member remap lives (`_read_backend_choice`), and the derived value still crosses the one gate. |
| H6/H7 | The new predicate is membership in a named set, `ACP_BACKENDS_CREWMATE_INHERITABLE`, with every harness's membership listed in the diff. It grants nothing new: each member already has both parent capabilities. |
| H17 | Unchanged. The pin still outranks every inherited value and still refuses when unstartable. |
| D6 | Section 5.5. |

**No rule exception is needed in my reading.** The one place a reviewer could read it differently is section 5.4: a crewmate that inherits codex has its worker refused. That refusal exists today for a stored `member_acp_backend: "codex"` and is a capability refusal, not a refusal of the persisted harness. If the harness-parity lane or the owner reads tier 3 plus that refusal as "a refusal that reaches agent.acp_backend", the exception goes in its own docs PR to `AUTOSDE.yaml` and `harness-parity.md`, following the #25798 precedent, and not inside an implementation PR.

### 5.7 UI

**One picker, built on #25485 (O5).** The harness row in `CrewProfileSettings.tsx` graduates out of Feature Preview: `PREVIEW_CREWMATE_HARNESS` and its `usePreviewFlag` check are removed. Its option list, labels and refusal hints move into one shared field (`CrewHarnessField`, with the logic now inline in `CrewProfileSettings.tsx`), which the profile row and `CrewEditorNextDialog` both render. There is no second picker and no second option list. Both write `agents.<name>.acp_backend` through the existing update route (`dashboard/agent_admin/crew_update.py`).

**Only harnesses crewmates can run on (O3).** The record's `harnesses` (in `crew_records.py`, today `selectable_harnesses()`: every selectable harness) narrows to the selectable members of `ACP_BACKENDS_CREWMATE_INHERITABLE` plus KAS. goose, opencode, strands and Kiro CLI are not offered. Kiro CLI is not offered because on the crewmate route it means KAS (5.1), so KAS is the one entry. A crewmate whose saved pin is outside that list still shows its current value (the existing `withCurrent`), marked as not runnable, so the person can move off it.

The save route enforces the same list, because the API is the trust boundary and the UI is not: `harness_pin_rejected` (`crewmate_harness.py`) rejects a value outside it with a message naming the allowed values. Hiding it in the picker alone is not enough.

**Hand-edited pins.** A pin this gateway cannot start (unknown, denied by policy, dropped from `selectable_backends()`) refuses at start with `crewmate_harness_unavailable` before any process exists (H17), as today. One correction to the premise of O3: a hand-edited pin to a harness that this gateway *can* start but crewmates cannot run on (goose, opencode) is not refused by H17 today, because `CrewBackendPin.resolve` checks only `selectable_backends()`. Such a crewmate starts on that harness; goose and opencode mount the dispatch tools, and a saved spec there is refused with `capability_harness_unsupported`. This RFC keeps that start behaviour and marks the pin as not runnable in the picker and in `kirocrew doctor`. Making `CrewBackendPin.resolve` also refuse a pin outside `ACP_BACKENDS_CREWMATE_INHERITABLE` would widen H17 beyond "a pin this gateway cannot start". If the owner wants that, the H17 wording change goes in its own docs PR to `AUTOSDE.yaml` and `harness-parity.md` first, and the code change follows as its own PR; it is not part of the phases below. An existing hand-edited `kiro` pin keeps today's `kiro_cli_pick_backend()` resolution.

**Known refusals shown before a pick**, from fields the record already serves:

- Inherited: names the resolved harness (`inherited_harness`), and the fallback reason (`inherited_reason`, new) when 5.3 applied.
- A harness not in `worker_harnesses`: "chats work; workers it opens are refused" (the existing `profile_harness_no_workers` string). This covers codex, deepseek and pi until 5.4.
- A saved pin that cannot start here: the existing `profile_harness_cannot_start` text, from `harness_error`.
- A saved spec with single-tool limits on a harness outside `ACP_BACKENDS_MEMBER_TOOL_LIMITS`: the existing `member_backend_warning` result, shown under the field.

`inherited_harness` in `crew_records.py` already reads `cfg.agent.member_acp_backend`, so it follows Phase 1 with no edit.

## 6. Behaviour change for existing users and notice

| Settings → Agent Harness | `member_acp_backend` stored | Crewmate DM before | After | Workers after |
|---|---|---|---|---|
| absent or KAS | no | KAS | KAS | KAS |
| Kiro CLI | no | KAS | KAS | KAS |
| Claude Code | no | KAS | Claude Code | Claude Code |
| Codex | no | KAS | Codex | refused until 5.4 follow-up |
| deepseek or pi | no | KAS | that harness | refused |
| goose, opencode, strands | no | KAS | KAS (logged) | KAS |
| any | yes | that value | that value | unchanged |
| any | pin set | pin | pin | unchanged |

The only people who move are those who picked Claude Code, Codex, deepseek or pi in Settings and never stored `member_acp_backend`. Their crewmate DMs move from KAS to that harness. A crewmate's KAS conversation does not resume on another harness, so its next turn starts a new native session; Crew's own transcript and memory carry over as they do for any harness change.

Notice: besides the Settings line (5.2), a once-per-install dashboard notice, shown the first time the moved person opens a crewmate chat after upgrade: "Crewmates now follow Settings → Agent Harness (Claude Code). To keep one on KAS, pin it in its profile." It is dismissible and stored like the existing once-only KAS notice. The release notes name the change. For codex, deepseek and pi the notice also says that workers are refused until supported and how to pin KAS.

## 7. Migration plan

Each phase is one PR. None needs a flag except where named; each is abandonable without undoing earlier ones.

**Phase 1 — resolver (behaviour change, flag-free).** Derive the member value in `_read_backend_choice` and the degraded path; add `ACP_BACKENDS_CREWMATE_INHERITABLE`; the resolver returns `source` beside the value and the config response serves `crewmate_harness: {backend, source}`; deprecation text on the field; doctor advisory row. Status of this RFC must be `accepted` first (First Principles reads it from the base).
Exit criteria (tests in `test/test_config_member_backend_inherit.py` unless named):
- `test_unset_member_follows_acp_backend_claude` — `acp_backend: "claude"`, no member key → `member_acp_backend == claude`.
- `test_kiro_pick_maps_to_kas_off_bundle_unconfirmed`, `..._off_bundle_confirmed`, `..._on_bundle` — all three give KAS, and `cfg.agent.acp_backend` is unchanged from today in each.
- `test_degraded_acp_backend_floor_maps_to_member_default` — an unknown `acp_backend` gives KAS on the crewmate route.
- `test_uninheritable_falls_back_to_kas_with_one_log[goose|opencode|strands]`.
- `test_kas_unselectable_takes_floor_silently` — policy denies kas; no warning logged.
- `test_stored_member_value_still_wins[kas|claude|kiro]` — legacy tier 2, `"kiro"` still → KAS.
- `test_full_document_save_keeps_member_key_absent` — save after load; the file still has no `member_acp_backend`.
- `test_harness_parity.py::test_crewmate_inheritable_is_capabilities_and_dispatch_minus_kiro`.
- `test_crewmate_harness.py` pin tests pass unchanged (H17).
- `test_enterprise_crewmate_inherit_lands_on_kas` — enterprise build, policy allows kiro, Settings = Kiro CLI → KAS.
- `test_config_member_backend_inherit.py::test_crewmate_harness_source[inherited|fallback|member_field|enterprise]` — the served `source` for each row of the 5.2 table.
- `test_cli_doctor.py::test_member_backend_deprecation_row_only_when_it_differs`.

**Phase 2 — refusal text (flag-free).** Reword `worker_kit_backend_refusal` and the two member-backend texts in `dashboard/chat_turn/turn_failures.py` to name the pin and Settings. Exit: `test_mate_worker.py::test_worker_refusal_names_pin_and_settings_not_member_field`; no string in `src/` tells a person to set `agent.member_acp_backend`.

**Phase 3 — crewmate picker (removes preview flag `crewmate-harness`).** Extract the profile row into the shared `CrewHarnessField`; render it in `CrewEditorNextDialog`; remove `PREVIEW_CREWMATE_HARNESS`; narrow the record's `harnesses` and `harness_pin_rejected` to the crewmate list (O3); add `inherited_reason`. Exit: `website/src/test/CrewEditorNextDialog.test.tsx::writes the harness pin`, `::offers only crewmate harnesses`, `::shows workers refused for a harness outside worker_harnesses`, `CrewProfileSettings.test.tsx::harness row renders without the preview flag`, `test_crewmate_harness.py::test_pin_save_rejects_a_harness_crewmates_cannot_run_on[goose|opencode|kiro]`, `::test_hand_edited_unstartable_pin_still_refuses` (H17 unchanged), `test_crew_records.py::test_harnesses_lists_only_crewmate_harnesses`, `::test_inherited_reason_names_fallback`. The picker parts can land before Phase 1; `inherited_reason` needs Phase 1's `source`.

**Phase 4 — Settings line and notices (flag-free, after Phase 1).** The "Crewmates run on" line on Settings → Agent Harness with its fallback warning (5.2, 5.3); the fallback notice (5.3); the once-per-install notice for moved users (section 6). Exit: `website/src/test/SettingsPage.agentHarness.test.tsx::shows the crewmate harness and its source`, `::warns before save when the pick cannot run crewmates`, `test_crewmate_harness_notice.py::fallback_notice_names_harness_and_kas`, `::fallback_notice_once_per_harness_pair`, `::moved_notice_shown_once_when_inherited_differs_from_kas`, `::no_notice_when_member_field_stored`, `::no_notice_when_pinned`.

**Phase 5 — codex workers (gated only on evidence).** Live capture plus set membership (5.4). Exit: the capture fixture exists and `test_mate_worker.py::test_codex_worker_kit_projects_excluded_tools` replays it.

## 8. Backward compatibility

- Stored `member_acp_backend`: unchanged behaviour (5.2).
- Pins: unchanged (H17).
- Config files are never rewritten by this change.
- Rollback: reverting Phase 1 restores the `"kas"` default for unset member values. No stored data needs undoing.

## 9. Security considerations

- No capability is granted. Worker-kit membership changes only in Phase 5, with live evidence.
- A crewmate moving to Claude Code or Codex moves its prompts, memory context and tool calls to that vendor's harness and bill. That is the person's Settings choice, and the Phase 4 notice says it happened. In an enterprise build only harnesses policy allows are reachable.
- Sandbox treatment follows the harness, as for any session on it (`ACP_BACKENDS_INTERNAL_SANDBOX`, the adapter mask); nothing here changes it.

## 10. Alternatives considered

1. **Surface `member_acp_backend` as a second Settings control.** Keeps two harness settings people must keep in step; the owner chose one.
2. **Remap `member_acp_backend` silently to follow Settings even when stored.** Changes behaviour for people who chose it. Rejected by the owner (O1).
3. **Refuse an inherited harness that cannot carry a crewmate.** Breaks every crewmate on a Settings change for ordinary chats, and puts a refusal on `agent.acp_backend` (H1/H3). Rejected by the owner in favour of fallback plus a visible warning (O2).
4. **Change `select_provider_backend` to read `acp_backend` directly.** Puts new branching in the gate (H13) and leaves the 20+ direct readers of `member_acp_backend` wrong. Rejected in favour of resolving the field at load.
5. **A second picker in the crew editor beside the profile row.** Two option lists drift. Rejected by the owner (O5).

## 11. Open questions

1. Should Phase 1 wait for Phase 5, so codex users never see refused workers? This RFC says no: Phase 2's text and Phase 4's notices name the fix, and Phase 5 is evidence-gated with no known end date.
2. Should a hand-edited pin to a startable harness crewmates cannot run on (goose, opencode) refuse at start, like an unstartable one? This RFC keeps today's start behaviour (5.7). Saying yes needs an H17 rule change in its own docs PR first. No phase depends on the answer.
