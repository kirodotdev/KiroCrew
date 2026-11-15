---
title: Turn a scheduled job into a Bot — a from-cron card that carries instruction, schedule and last result
status: draft
author: garohop, with kirocrew
created: 2026-11-15
last-audited: 2026-11-15
audited-at: 686d00fc11
doc-pr: null
implementation-prs: []
tracking-issues: [37666]
supersedes: []
superseded-by: []
---

# RFC: Turn a scheduled job into a Bot — a from-cron card that carries instruction, schedule and last result

- Status: `draft`. Acceptance is requested from a maintainer; the status flips to `accepted` when one records it on this line with the date. Nothing is on main. This document lands first because the change adds a Schedule page action and changes where a job's runs report, and the First Principles lane reads that decision off the base branch.
- Tracking issue: [#37666](https://github.com/kirodotdev/KiroCrew/issues/37666), triaged `tier:T3`, `parked`, with the note that it "changes who a job belongs to, where its runs report and what the Create Bot card can take over".

This is the sibling of `rfc-chat-to-bot` (proposed in its own pull request for [#37663](https://github.com/kirodotdev/KiroCrew/issues/37663)) for a different source object, and it builds on the same accepted document, [rfc-crewmate-share.md](rfc-crewmate-share.md), whose `crewmate.import` card already creates a Bot and a paused cron bound to it from a bundle. That document is not edited here; §5.2 (one more carried part) and §5.4 (one more way a card of that shape is raised) are amended in this document. The `brief.md` carried part is defined once below (§3) in the same words as the sibling, so either document stands on its own.

## Problem

A scheduled job is usually the first form a standing job takes: a `message`, a schedule, a chat it delivers into (`session_key`), and with `persistent_session` a `last_result` that carries from run to run. When the job outgrows that and needs memory, a goal with check-ins, a thread to talk to, or Abilities, the product's object is a Bot. The parts that exist each stop short:

- The **Create Bot** card can create a *new* schedule bound to the Bot (`member_id`), from a 200-character goal. It cannot take over a job that exists.
- `kirocrew cron add --agent <bot>` binds a *new* job to the member (`split_cron_agent_member`, `src/kiro_crew/cli_commands_cron.py`, sets `member_id`). `kirocrew cron update <id> --agent <bot>` writes `agent_id` only (`cli_commands_cron.py`): `member_id` stays empty and `session_key`, which is both the job's owner and where its results land, stays on the old chat, so runs keep reporting there and the Bot cannot list or change the job from its own chat (#19295).
- `kirocrew cron adopt <id> --session-of <key>` moves owner and delivery together (delivery only when the key is a `dashboard:` session; for another namespace it prints "Ownership transferred; delivery did not."), but it is CLI-only and operator-only by design: `session_key` is not writable through MCP `cron_update` or the dashboard `PATCH`, and the command is covered by the `self-protection-cron-adopt` denied-command rule (`src/kiro_crew/security/denied_rules_catalog.py`; #10067 asks for the API this would need). It targets a session key, not a Bot.
- A stateless run cannot call `memory_recall` or `learn_add` (#36392), so a job never learns its way into being a Bot.

On one gateway on 2026-11-15: 24 jobs, two of them bound to a Bot because they were created with one; the rest belong to a chat or to nobody.

## Current behavior

Verified at `686d00fc11`:

1. A `CronJob` (`src/kiro_crew/cron_service/model.py`) carries `message`, `schedule`, `timezone`, `agent_id`, `member_id`, `memory_store`, `session_key` ("session that created this job"), `persistent_session`, `last_result` / `last_result_ts`, `enabled`, `user_paused`, failure state, `created_by`, `managed_by`, `skip_dates`, and for a script job its script path. A job bound to a member reports into that member's thread and runs on its worker (`src/kiro_crew/docs/crew-members.md`, "A scheduled turn runs on its worker"; observed as owner `dashboard:member-<slug>...` on member-created jobs).
2. `crewmate.import` step 4 (share RFC §5.4) creates a bundle's crons bound to the new member in two places: a `message` job through the plan's own cron step (`_import_cron_step`, `src/kiro_crew/change_card_catalog_plans.py`, a `POST /api/crons` with `enabled: false`), and a `script` job through the import-files route (`src/kiro_crew/dashboard/handlers/crewmate_import_files.py`), which copies the script into `<config dir>/crons/imported/<member id>/` under `O_EXCL` and creates the job with `add_job_async(..., enabled=False)`; and anything outside the bundle's cron shape `{name, message, cron_expr, timezone, script}` (`_CRON_KEYS`, `src/kiro_crew/crewmate_bundle.py`) refused, which is how a `command` job is refused. Its create step sends `approval_mode: "normal"` (`change_card_catalog_plans.py`).
3. The Schedule page's job row has an actions menu (`website/src/components/CronRowActions.tsx`: run or cancel the run, pause/resume, view last result or go to chat, the strict-schedule toggle, move to folder); a Bot's Schedules tab lists the jobs bound to it.
4. No route moves an existing job onto a member.

## Decision

A person can turn a scheduled job into a Bot in one press from the Schedule page. The Bot opens holding the job's instruction as its goal, the job's schedule as its own, and a brief made from the job's last result; the source job is paused, not deleted, in the same press.

### 1. A `crewmate.from_cron` card

A new change-card kind, `crewmate.from_cron`, shaped like `crewmate.import` and applied through the same plan builder:

| Param | Source | On the card |
|---|---|---|
| `cron_id` | the job the action was pressed on | shown as **From schedule** with the job's name; not editable |
| `name` | the job's name | editable; collisions resolved as import does |
| `goal` | the job's `message`, cut to the goal cap | editable |
| `schedule` | the job's `cron_expr` and `timezone` (an `every` or one-shot `at` job is refused: the card paths carry only a cron expression (`_cron_body` forwards `cron`; `crewmate.create` requires `cron_expr`), although `POST /api/crons` itself accepts `every` with a `member_id`) | editable |
| `brief` | the job's `last_result`, its `last_result_ts`, its last status and error | shown in full, editable |
| `pause_source` | `true` | a checkbox, on by default: pause the source job when the card applies |

The apply is import's plan for a one-cron bundle, with the departures named below, in this order: `POST /api/agents` (`approval_mode: "normal"` as import does, plus `first_greeting`, which import does not send; provenance `from_cron: {cron_id, name, at}` rides in the create body as import's `imported` does), goal, then the files step (which writes the brief as `handoff.md`, and for a `script` job the script and its paused cron), then for a `message` job the cron through `POST /api/crons` bound to the member, then the source job paused when `pause_source` is on. A `command` job never reaches a card: the row action is disabled on it with the reason, because import's cron shape has no `command` and this document keeps that.

Pausing the source needs one route change. The Schedule page pauses through `POST /api/crons/{id}/enable` (`api_cron_enable`, which sets `enabled`, `user_paused` and `user_pause_requested` together), while `PATCH /api/crons/{id}` has no pause field and answers `no fields to update`. A card step runs only on a route in `HOOKED_ROUTES` (`change_card_catalog.py`; `change_card_middleware` admits and records steps there), and the enable route is not one. Phase 1 adds `("POST", "/api/crons/{job_id}/enable")` to `HOOKED_ROUTES` and leaves it out of `MEMORY_ROUTES`, so a card may pause through it while a person's own pause or resume on the Schedule page writes no memory line, as today. The card records the source's prior `enabled`, `user_paused`, `auto_paused` and `consecutive_failures` before the step; a source already paused makes the step a no-op and the card says "already paused".

Where the brief and schedule come from, and when the bundle is built: the person may edit `name`, `goal`, `schedule` and `brief` on the card, so none of them can be frozen into a bundle at raise time (import avoids this because its only editable field is `name`, and `check_editable` refuses a change to `bundle_sha256`). And a card's plan is fixed at its last preview: each apply step is one browser request whose body must equal the stored step (`match_step`, fills aside), so a bundle first built at the press has no digest a previewed plan could carry. Phase 1 therefore gives the files route a **second body shape for this kind**, `{member, card}`, naming the card instead of a digest: inside that one request the route reads the card's final params and the live job, builds the **synthetic bundle** as the reader requires (`kirobot.json` carrying `name` and `description`, which `_MANIFEST_KEYS` allows; an empty `agent.json`, since `crewmate_import_params` allows only `mcpServers` and `skills` there; `crons.json` with the edited schedule; the script file where there is one; and `brief.md`), stages it as an upload is staged (`crewmate_stage`, `0600` under `run/`, named by its sha256, after the same wait on the start-up sweep the upload path takes), runs the same checks and writes, and discards the copy before answering. That release is new code (`_release_staged_copy` returns early for every kind but import); no copy outlives the request, a copy orphaned by a crash is reclaimed by the import-only sweep as any other unlisted copy is, and the card shows the script by its path rather than through the import-only files view. The brief is a card param, so its bound is `_PARAMS_MAX_BYTES` (32 KiB) with the card's record cap (`CARD_RECORD_MAX`, 64 KiB) counting its display row as well, each non-ASCII character as six bytes; the route cuts `last_result` to fit and says so on the card.

Departures from import, each named so the sign-off covers them: (1) `first_greeting` on the create; (2) a `message` job's copy starts **enabled** when the source was enabled (import pauses, because a file is untrusted; here the source is the owner's own running job and the point is that it keeps running, from the Bot); (3) a `script` job's copy starts **paused**, as import's does, because the files route creates script crons paused and its undo refuses `changed_since_apply` when the job is enabled, and this document does not change that route's contract (the card says the copy must be un-paused from the Bot's Schedules tab, and open question 3 asks whether that should change); (4) the source job is paused, not copied-and-left, through a newly hooked route; (5) the bundle is built by the files route in the apply request from the edited card and the owner's own job, not uploaded at the raise, under a second body shape for this kind that names the card instead of a digest. Undo removes the Bot and its job through import's undo (for a `script` job, with the copy still paused, so that undo's guard holds) and puts the source back to the state the card recorded: a source that was running is re-enabled through the same route; one that was already paused, or auto-paused after failures, stays so, because `{"enabled": true}` would also clear `auto_paused` and reset `consecutive_failures`.

### 2. Who raises it

Only the person, from the Schedule page: a **Turn into Bot...** row action. (Not on a Bot's Schedules tab: that tab lists only jobs that already wake this Bot, and "Nothing here edits"; turning one of those into a second Bot is the attach case deferred below.) The browser posts to an owner-only route, `POST /api/crons/{id}/to-bot`, which reads the job, builds the params above, re-checks them as import re-checks a file, and raises the card on the reserved owner slot (`owner:crewmate-import`'s pattern), so a card left pending reopens from the same action. The kind is refused from every agent caller and left out of `list_kinds`, as `crewmate.import` is. An agent that wants this for a job it owns says so in words; the person presses.

### 3. The brief: one more carried part

Amends share RFC §5.2: a bundle may carry one `brief.md`, text only, at most `CARRIED_FILE_MAX_BYTES`. The import-files route writes it as `handoff.md` at the root of the Bot's workspace (not under `.kiro/skills/`), with `O_EXCL`; a file already there refuses the step, and the step's undo deletes exactly that file. It is content, not code: the worker reads it as any workspace file, and nothing loads it as instructions. It reaches the route inside the synthetic bundle the gateway builds at the press (§1) from the brief as the person last edited it on the card. A change card's store **refuses** rather than redacts any param text an output redactor would change (`check_param_text`, `dashboard/change_cards_records.py`, `invalid_text`), so the `to-bot` route runs the same test the store does (`needs_redaction`, which applies the context patterns, not bare `redact_credentials()`) over every carried string, name, goal and brief, and redacts before raising the card; a job whose `last_result` holds a credential-shaped token or an exfiltration-shaped link gets a redacted brief, not a refused card. The sibling document for #37663 defines the same part in the same words; whichever lands first carries it, and the other then cites it.

### 4. What the new job keeps and loses

Kept: name, message, cron and timezone, script (as a copy in the member's folder), and the last result, as the brief. Lost: the job id, its run history, failure counters and dedup state, which stay on the paused source; and every field outside the cron shape both copy paths carry (`_CRON_KEYS` is `{name, message, cron_expr, timezone, script}`, and `_cron_body` adds only `agent` and `enabled`): channel and thread, model, approval mode, `timeout_secs` and the script or command `timeout`, `persistent_session`, `minimal_context`, `new_chat_per_run`, `silent`, `hide_in_chat`, strict schedule, folder and chat folder, project path, `skip_dates`, `env`, the auto-pause and alert thresholds, and a script job's secret grants, which all take the defaults a new job gets. The card's **Not carried** row lists them. This is the price of reusing the import path instead of writing `session_key` and `member_id` on the live job; the in-place move is not decided here (below).

### 5. The link back

The source job's owning chat, where there is one, receives one system row: "schedule <name> moved to Bot <name>", linking the Bot's thread. The source job's row on the Schedule page shows "paused: moved to Bot <name>" from the provenance stamp, so a person reading the page later knows why it stopped. The Bot's Profile shows **Made from schedule <name>**.

## Not decided here

- **Moving the job in place** (keep id and history; set `member_id`, `agent_id`, `session_key`). That is the write #10067 wants behind one authorized API; when it lands, a from-cron card can apply through it instead of copying, and this document would be amended, not replaced.
- **Attaching a job to an existing Bot** (`cron.move_to_bot`). Same write as above, no Bot created. Out of scope until #10067.
- Any change to `cron adopt`, its denied-command rule, or who may write `session_key` without a person's press.
- A job's chat history: a `persistent_session` job's `cron:{id}` session is not forked or copied, for the same memory-boundary reason the sibling document gives for a chat's transcript.

## Migration plan

- Phase 0: this document.
- Phase 1: the kind, its preview and plan (reusing import's steps with the five named departures), the `to-bot` route, the files route's second body shape for this kind with its in-request build, stage, write and release, the `/enable` route joining `HOOKED_ROUTES` outside `MEMORY_ROUTES`, the recorded prior state the undo restores, the row action on the Schedule page, the provenance stamp and the two link-back rows. One pull request; the cron and Bots docs pages each gain a paragraph in the same diff.
- Phase 2, after #10067: apply through the in-place move, keep id and history, and retire the copy-and-pause path by amendment.
- Nothing is removed or migrated; existing jobs and Bots are untouched.

## Backward compatibility

Additive. `crewmate.create` and `crewmate.import` keep their params and plans. The source job is left on the Schedule page, paused, with its history, and un-pausing it is one click; a person who changes their mind loses nothing but the un-pause (and, for a `script` job, the copy's own un-pause, which the card names).

## Security considerations

- Owner-only route and press; refused from every agent caller. The card moves management and delivery of a job the person already owns onto a Bot the person is creating in the same press, which is the authorization `cron adopt` reserves for the operator's shell today, expressed as a card.
- The params come from the owner's own job record and are re-checked as import re-checks a file: the synthetic bundle goes through the same reader and the same §5.2 checks as an upload, and the card store's `check_param_text` refuses anything a redactor would change, which the route pre-empts by redacting the brief itself (§3).
- `POST /api/crons/{id}/to-bot` sits under the `/api/crons` prefix, which is mixed internal/owner; it is owner-only the way `/api/crons/{job_id}/secrets` is, by a handler-side `require_owner_dashboard_request` refusal, not by the prefix.
- A script job's script is copied into the member's own `crons/imported/<member_id>/` folder, never run from or written into the shared `crons/` folder, exactly as import does and for the same `sys.path` reason. A `command` job is refused, as import refuses it.
- The new Bot's reach is `crewmate.import`'s, not `crewmate.create`'s: `approval_mode: "normal"` is pinned in the create write, as import does (`crewmate.create` sends no mode, and an unset mode opens the thread in `MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE`, which is `trust`). Nothing added under Abilities. The card shows the template's inherited auto-approvals as a widening row with `widen` risk, as both existing kinds do (import's wording, "Your Bots use these without asking"). The job's runs now ask the Bot's approvals, not the old chat's.

## Alternatives considered

- **In-place move now.** Keeps history, but needs the `session_key`/`member_id` write #10067 has not given an authorized API, and would put that write behind a card before the API's own reason codes exist. Deferred to Phase 2.
- **Delete the source job on apply.** One-way; a person who wants the history back has nothing. Pausing costs one row on the page.
- **Fold into the sibling chat-to-Bot document.** Same card shape and brief part, but a different surface (Schedule page, not a chat), no agent turn, a different carry-over and a cron created in the same press. Separate documents, separate acceptance.
- **Manual path (today).** Card, then `cron adopt` typed by the person because the agent is denied it, then `cron update --agent`, then a brief by hand. (Deleting the job and re-adding it with `cron add --agent <bot>` binds the member in one write, but loses the id and history, as the alternative above does.)

## Consequences

- One row action on the Schedule page, two owner routes (the `to-bot` raise and the files route's second body shape), one hooked route added, one card kind, one provenance stamp; the brief part is shared with the sibling chat-to-Bot document.
- Until Phase 2, a converted job is a new job: dashboards or notes that reference the old id keep pointing at the paused source.

## Sign-off

Requested from a maintainer on: (a) copy-and-pause now, in-place move after #10067; (b) a `message` job's copy starting enabled when the source was, and a `script` job's copy starting paused; (c) owner-only raising with no agent path, through a gateway-built staged bundle; (d) refusing `every` and `at` jobs rather than converting them.

## Open questions

1. Whether a job whose owning chat is a channel session (Slack, Telegram) may be converted, since its delivery was never a dashboard slot and the link-back row has nowhere to go. Proposed: allowed, with no link-back row.
2. Whether any of the fields the copy loses (§4) should ride along. `skip_dates` is the most useful, but it cannot ride the script path without adding it to `_CRON_KEYS`, which widens the bundle format for every import. Proposed: none in Phase 1; the Not carried row names them, and Phase 2's in-place move keeps them all.
3. Whether a `script` job's copy should start enabled like a `message` job's. That needs the import-files route to take an enabled state and its undo guard to compare against the state it recorded rather than `enabled`; this document leaves the route's contract alone and the copy paused.
