---
title: Default for Minimal context on new scheduled jobs -- a user setting and an LTS governance configuration entry
status: draft
author: kyleseaman, with kirocrew-worker
created: 2026-11-23
last-audited: 2026-11-23
audited-at: ba935b4498
doc-pr: null
implementation-prs: []
tracking-issues: [38590]
supersedes: []
superseded-by: []
---

# RFC: Default for Minimal context on new scheduled jobs

- Status: draft. Nothing built; it changes no code. Verified at `ba935b4498`. Acceptance requested from a maintainer. Section 4 proposes an amendment to [rfc-crew-lts-edition](rfc-crew-lts-edition.md) in this document, so that accepted design is not edited here; the amendment takes effect only once that RFC's owner agrees, and until then Phase 3 is blocked.
- Tracking issue: [#38590](https://github.com/kirodotdev/KiroCrew/issues/38590) (parked 2026-11-17: "a new setting and a new entry in the governance configuration change a recorded design ... so it needs a design before code").
- Related: [rfc-crew-lts-edition](rfc-crew-lts-edition.md) D3 (open controls, including `capabilities.cron`) and D7 (`crew_sandbox_min_level` read from `body.configuration`), the model for the entry proposed here.

## Summary

A new scheduled job starts with Minimal context off, and nothing lets a person or an organisation change that starting value. This RFC proposes:

1. A user setting, `cron.default_minimal_context` (bool, default `false`). The job form, `cron_add` and `kirocrew cron add` start a new agent job from it. A value given for the job always wins. Existing jobs are never changed.
2. In the LTS edition, a governance configuration entry that sets the organisation's starting value, read like D7: from `body.configuration`, covered by the manifest hash when one is declared, read leniently when not, and kept with the cached policy. It sets a default only. It never forces Minimal context on and never stops a person turning it off for a job.

How the organisation's value and a person's own setting combine is Open question 1; the recommendation is that the organisation's value, when its reply carries one, is the starting value.

## Motivation

Measured at `ba935b4498`.

Every wake of an agent job with Minimal context off loads the full saved context: memory, lessons, steering, skills and prior history. The cron tool's own description puts that at "~200 tokens vs ~30-55k" (the `minimal_context` property of `cron_add` in `src/kiro_crew/mcp_cron_tool_list.py`). Polling, digest and checker jobs rarely need it.

The starting value is `false` everywhere a job is created, and nothing reads a default for it:

| Where a job is created | Today | Citation |
|---|---|---|
| Job form, create | `minimalContext: false` in the empty-job defaults | `parseJobDefaults` in `website/src/components/JobForm.tsx` |
| `POST /api/crons` with the field absent | `body.get("minimal_context", False)` | `api_crons_create` in `src/kiro_crew/dashboard/handlers/cron_jobs.py` |
| `cron_add` with the argument absent or `null` | `... else False`; any other non-bool value is refused by the tool's argument validation (`_validate_args`) before it gets here | the `cron_add` branch of `_call_tool_inner` in `src/kiro_crew/mcp_cron.py` |
| `kirocrew cron add` without the flag | argparse `default=False`, so "absent" and `--no-minimal-context` look the same | the `cron add` parser's `--minimal-context` in `src/kiro_crew/cli_parser_agent.py`; `_cron_add` in `src/kiro_crew/cli_commands_cron.py` |
| App jobs (`ctx.cron.add_job`) | no `minimal_context` parameter; the store default applies | `CronSDK.add_job` in `src/kiro_crew/apps/cron_sdk.py` |
| Store default and load | `False` | `CronJob.minimal_context` in `src/kiro_crew/cron_service/model.py`; `_job_from_record` in `src/kiro_crew/cron_service/store.py` |

Three things follow:

- An organisation that wants cheap wakes has to ask every person to tick the box on every job.
- A job an agent creates inherits the costly default unless the agent sets the field. The agent instructions ask it to (the `CRON` text in `src/kiro_crew/mcp_instructions.py`), but nothing enforces that.
- Org governance today can only narrow controls (LTS D3), not set a feature's starting value, so the LTS edition has no place to put this either. D7 is the one configuration entry Crew reads, and it is a tighten-only floor.

Some jobs already run with Minimal context and are not part of this change: the Bot check-ins (`sync_schedule` in `src/kiro_crew/dashboard/goal_reminder.py` calls `add_managed_job_async(..., minimal_context=True)`) and the stale-Dashboard wake (`sync_job` in `src/kiro_crew/dashboard/stale_wake.py`).

## Goals

1. A person can choose the starting value of Minimal context for new agent jobs, once.
2. An LTS organisation can choose the same starting value for its people through its governance service.
3. A value given for a job, in any create or edit path, always wins over any default.
4. No existing job changes, and nothing changes for anyone who sets neither default.

## Non-goals

- Forcing Minimal context on, or blocking anyone from turning it off. No new governance control and no new row in the LTS appendix.
- Changing what Minimal context drops or keeps (`member_lifecycle` in `src/kiro_crew/members_parts/member_sessions.py` and the context builders stay as they are).
- Changing the store default, the load default or any stored job.
- Defaults for other job fields (`persistent_session`, `hide_in_chat`, `silent`).
- Bot check-ins and other jobs Crew manages itself.

## Design

### 1. The user setting

A new config section `cron` with one key:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `cron.default_minimal_context` | bool | `false` | The starting value of Minimal context for a new agent job |

There is no `cron` section today: the only cron config is `cron_history` (`KiroCrewConfig.cron_history` in `src/kiro_crew/config/loader_config.py`, `CronHistoryConfig` in `src/kiro_crew/config/service_sections.py`). The new section is built and loaded like `cron_history`.

A config field's `_meta()` label does not create a Settings control; each Settings panel is hand-written. The control is a toggle written for it, next to the comparable "default for new things" control in Settings > Chat (`DefaultSessionFolderSetting`, rendered by `website/src/pages/settings/ChatPanel.tsx`). Its description says it applies to new jobs only.

### 2. One resolver, read only when a job is created

One function in `cron_service` answers "what does a new agent job start with, and why": `(value, source)`, where `source` is `org`, `user` or `builtin`. Every create path below calls it only when the caller gave no value:

| Path | Proposed |
|---|---|
| Job form, create | The toggle starts at the resolved value. The form still sends `minimal_context` explicitly for an agent job, as today (`buildBody` in `JobForm.tsx`), so the backend stores what the person saw |
| Job form, edit | Unchanged: starts at the job's stored value |
| `POST /api/crons`, field absent | The resolver, in place of `False` (`api_crons_create`) |
| `cron_add`, argument absent or `null` | The resolver, in place of `False` (`_call_tool_inner`). Any other non-bool value stays a validation error, as today |
| `cron_update`, `PATCH /api/crons/{id}`, `kirocrew cron update` | Unchanged: only an explicit value changes the job (the `cron_update` branch of `_call_tool_inner`; `apply_job_update` in `cron_service/fields.py`) |
| `kirocrew cron add` | The flag's argparse default becomes `None`; absent means the resolver, `--minimal-context` and `--no-minimal-context` are explicit. This keeps the "one defaulting policy across every create surface" the CLI already states (the comment above the session-shape fields in `_cron_add`) |
| App jobs (`ctx.cron.add_job`) | Unchanged (Open question 2) |
| Bot check-ins, stale-Dashboard wake | Unchanged: they pass `True` explicitly |
| Imports and restores of existing jobs | Unchanged: they carry the job, not a new choice |
| Script and command jobs | The default does not apply: they take no agent turn, so a stored flag could never do anything, the same reason the form omits it for them (the comments in `buildBody`). An explicit value is stored as today |

The store default (`CronJob.minimal_context`) and the load default (`_job_from_record`) stay `False`, so a stored job that never wrote the key keeps loading as it does today.

`GET /api/crons` already returns server-side defaults for new jobs (`default_tz` from `api_crons` in `src/kiro_crew/dashboard/handlers/cron_board.py`). It gains `default_minimal_context` and `default_minimal_context_source`, which the form reads. The form's unsaved-changes check compares against that starting value, not `false` (the `dirty` check in `JobForm`).

### 3. What the person and the agent see

**The form.** When the starting value is on, the toggle's description names where it came from: "On by default from your setting" or "On by default for your organisation". The per-job toggle is always editable.

**The mode advisor.** `adviseCronMode` (`website/src/utils/cronModeAdvice.ts`) suggests Minimal context for a judgement prompt, and stays quiet when the toggle is already on. With a default, the toggle is on more often, so the risk flips: a prompt that needs memory or a skill can now start without it, and today the advisor returns `'none'` for such a prompt (its `NEEDS_CONTEXT` check). It gains one value, `'full-context'`, returned when Minimal context is on and the prompt matches `NEEDS_CONTEXT`, a `$skill` token or the word "skill". Its hint says the job mentions saved context or a skill, which Minimal context drops. The advisor stays off for a private member's job (the `advice` memo in `JobForm`), whose minimal wake keeps the member's core guidance ("Skip optional context. This member's core guidance stays available.", `components.jobForm.private_minimal_context_description`).

**Private-member jobs.** The default applies to them like any other agent job created through these paths. Their minimal wake still keeps the member's core guidance (`member_lifecycle` returns `MINIMAL`, which `member_turn_context` builds from).

**The agent.** The instructions tell agents that conversational reminders "keep all three defaults for continuity" (the `CRON` text in `mcp_instructions.py`). With a default that can be on, keeping the default no longer means full context. The text changes to say: pass `minimal_context=false` for a conversational reminder, `true` for a polling or digest job, and an omitted value takes the person's default. Both `minimal_context` descriptions in the tool list (`cron_add` and `cron_update` in `mcp_cron_tool_list.py`) say the same. The `cron_add` result names the value the job was created with and its source, so the agent can see what it got.

### 4. Proposed LTS amendment: a governance default for Minimal context

This section is proposed. It amends `rfc-crew-lts-edition` and holds only once that RFC's owner agrees.

**Proposed D14. The organisation can set the starting value of Minimal context.** In an LTS build, Crew reads the setting `crew_cron_default_minimal_context` (name pending, Open question 3) from the configuration entries of the governance reply (`body.configuration`), beside D7's `crew_sandbox_min_level`. Its value is a JSON boolean.

| Case | Effect |
|---|---|
| Reply declares `manifest.hash` | The entry is shape-checked and covered by the same hash as the policy and the other entries, as D7's are today (`parse_envelope` and `body_digest` in `src/kiro_crew/platform/enterprise_policy_fetch.py`) |
| No hash | The entry is read leniently (`_lenient_configuration`) |
| Value is not `true` or `false` | Sets no default and logs a warning, as D7 does for an unknown level. An entry Crew cannot read never refuses the reply |
| Entry absent | No organisation default; the person's setting applies |
| `UNCHANGED` reply, refresh, cache-only child | Keeps it: it is part of the translated document that is cached with the policy |

How it lands:

- The translator writes it into the Crew policy document as a new top-level key, `defaults: {cron_minimal_context: <bool>}`, the way `translate` writes D7's floor today (`translate` in `src/kiro_crew/platform/enterprise_policy_translate.py`).
- `defaults` joins the structural keys the policy parser accepts (`_STRUCTURAL_KEYS` in `src/kiro_crew/platform/governance_parts/parsing.py`). An unknown child under it fails closed, like every other fixed-child namespace, because the policy document is the tamper-evidence surface.
- `GovernanceCeiling` (`src/kiro_crew/platform/governance_parts/carriers.py`) carries it, beside the existing non-control values `updates`, `yolo_max_seconds` and `mcp_registry_local_control_plane`. One reader returns it and answers "no default" on any error, the same fail-open shape as `active_update_pins`: a governance glitch must not change how a job starts.
- Across policy tiers it folds like `updates.channel`: the authority's value, else the subordinate's (`_intersect_ceilings` in `src/kiro_crew/platform/governance_parts/tiers.py`).
- `TRANSLATION_REVISION` in `enterprise_policy_fetch.py` moves from `t6` to `t7`, so a document cached by an older translation is fetched again instead of being kept on `UNCHANGED`.

**It is not a control.** It does not pass through `_query_level`, is not one of the appendix's 66 controls and is not in the baseline policy. D3's rule that local settings may only narrow a control does not apply to it, because turning Minimal context off for a job widens no access: it only decides how much saved context the job's own agent reads. `capabilities.cron` stays an open control, unchanged.

**The `oss` edition** reads no governance reply, so it has no source for the entry: the fetcher refuses in an `oss` build (`_fetch_enterprise` in `src/kiro_crew/platform/policy_distribution_source.py`). Whether a hand-written or distributed `oss` policy document may carry `defaults.cron_minimal_context` is Open question 4.

On acceptance, Phase 3's pull request adds one line to the LTS RFC's decision log pointing at this section; the decision text stays here.

### 5. Precedence

For any create path, highest first:

1. A value given for the job (form toggle, `minimal_context` argument, `--minimal-context` / `--no-minimal-context`, request body).
2. The default: the person's setting and the organisation's entry, combined as Open question 1 decides.
3. `false`.

## Migration plan

Each phase is one pull request, independently shippable and independently abandonable.

1. **User setting and resolver (backend).** The `cron` config section, the resolver, the create paths in section 2, the CLI flag default, the `GET /api/crons` fields, the agent instructions and tool descriptions, and the resolved value in the `cron_add` result. Exit: with the setting unset, a job created by each path is stored with `minimal_context` exactly as on `ba935b4498`; with it `true`, `cron_add` without the argument, `POST /api/crons` without the field and `kirocrew cron add` without the flag store `true` for an agent job and `false` for a script or command job; `--no-minimal-context`, `minimal_context=false` and a body `false` store `false`; `cron_update` and `PATCH` with no value leave the job unchanged; app jobs and Bot check-ins are unchanged; a stored job without the key loads as `false` whatever the setting.
2. **Dashboard.** The Settings toggle, the job form's starting value and source note, the unsaved-changes check against the starting value, and the advisor's `'full-context'` value. Exit: the create form's toggle matches `default_minimal_context`; opening and closing an untouched create form reports no unsaved changes with the setting on; the edit form shows the job's stored value whatever the setting; `adviseCronMode("summarise my memory notes", true)` returns `'full-context'`; screenshots of the form with the default off, on from the setting, and on from the organisation.
3. **LTS entry.** Blocked on Open questions 1 and 3 and on the LTS RFC owner's agreement. The translator, the `defaults` key, the ceiling field and reader, the tier fold, `t7`, the resolver's `org` source, the Settings toggle's state for an organisation value, and the decision-log pointer. Exit: a hashed reply with the entry `true` makes a new agent job start on, and the same reply with a changed entry value fails the hash; an unhashed reply with `"yes"` sets no default, logs one warning and does not refuse the reply; an `UNCHANGED` reply keeps the value; a `t6` etag is not sent as a cached version; a job created with an explicit `false` stays `false` under an organisation `true`; no governance query or control changes.

## Backward compatibility

Compatible. With neither default set, every create path stores what it stores today. Phase 1's only change to an accepted input is that `kirocrew cron add` without the flag reads the setting instead of `False`, which matters only once someone sets it. No stored job, no store or load default and no edit path changes. An LTS build that receives no entry behaves as today. Older dashboards that omit `minimal_context` on create get the resolver, which is the default the person chose.

## Security considerations

- The setting and the entry only choose how much saved context a job's own agent reads. Turning Minimal context on or off grants no tool, file, network, approval or sandbox access, so neither can widen what a job may do. Approval modes, the sandbox, the local floor and every governance control apply to a minimal wake exactly as to a full one.
- Minimal context drops prompt-level guidance (lessons and steering), not enforcement. A rule a person keeps only as a lesson ("never push to main") is not in a minimal wake's prompt; the controls that enforce such rules are unaffected. The form's source note and the advisor's `'full-context'` hint exist so a person sees when a job starts without that guidance.
- The entry cannot stop Crew from starting: a value Crew cannot read sets no default and logs a warning. When the reply declares a hash, the entry is covered by it like D7's, and the policy source stays locked (D2).
- The reader fails open to "no organisation default", so a broken policy read never changes how a job starts.

## Alternatives considered

- **Make Minimal context the default for everyone.** Rejected: conversational reminders depend on full context, the change would alter every person's new jobs at once, and the mode advisor already suggests Minimal context where it fits.
- **A governance control that forces Minimal context on.** Rejected: some jobs need memory, and a forced control would be a new D3 control with a real cost to people and no access to protect.
- **Make the mode advisor tick the box itself.** Rejected: a regex guess would silently change what a job reads; the advisor stays a hint.
- **A per-agent default (`agents.<name>.…`).** Not proposed: more surface than the issue asks for; it can follow if one global default proves too coarse.

## Open questions

1. **Organisation value versus a person's setting (needs a maintainer ruling).** Options:
   - A. The person's setting wins when they have set it; the organisation's value applies otherwise. Needs a three-state setting (unset, `true`, `false`) so "unset" can be told from `false`.
   - B. The organisation's value wins when the reply carries one; the person's setting applies otherwise. The Settings toggle shows the organisation's value as set by the organisation, and every job's own toggle stays editable.
   - C. On when either says on.

   Recommendation: B. One source is visible at a time, it matches how the tiers fold `updates.channel` (the authority's, else the subordinate's), and the per-job toggle keeps the only freedom the issue promises. Its cost: under an organisation `true`, a person cannot make their own default off and unticks per job instead. The entry's `composeBehavior` field (listed in `_CONFIG_FIELDS` in `enterprise_policy_fetch.py` for the shape check and the digest, and not otherwise read by Crew today) is not used to decide this.
2. **App jobs.** Recommendation: unaffected. `ctx.cron.add_job` has no `minimal_context` parameter, so an app cannot opt out, and an app's prompts were written against full context.
3. **Entry name and publication.** `crew_cron_default_minimal_context` follows D7's naming. The governance service owns the configuration schema, so the name, and whether the console can publish it, need its agreement before Phase 3.
4. **`oss` policy documents.** The `defaults` key is parsed wherever a Crew policy document is installed. Recommendation: honour it there too, since it sets a default only and the parser is shared; the alternative is to refuse it outside an LTS build.
