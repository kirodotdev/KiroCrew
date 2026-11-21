---
title: Crewmate abilities at create — say what a new Bot's worker can reach before its first run fails
status: draft
author: garohop
created: 2026-11-15
last-audited: 2026-11-15
audited-at: 45c0cddb31
doc-pr: 37534
implementation-prs: []
tracking-issues: [37508]
supersedes: []
superseded-by: []
---

# RFC: Crewmate abilities at create — say what a new Bot's worker can reach before its first run fails

- Status: draft. Proposes three independent changes; nothing is built. Filed from
  [#37508](https://github.com/kirodotdev/KiroCrew/issues/37508), which triage
  parked as `tier:T3` pending a design sign-off. Claims about today's code were
  checked at `45c0cddb31` (main, 2026-11-14). Citations name symbols, not lines, as the docs lint asks.
- Author: garohop
- Amends, in this document so the accepted ones are not edited:
  [rfc-crewmate-goals.md](rfc-crewmate-goals.md) ("New crewmate card", the
  Advanced fields) and the `[Goal set]` kickoff
  ([rfc-crewmate-heartbeat.md](rfc-crewmate-heartbeat.md), 2026-10-29 amendment:
  `goal_setup.kickoff_prompt`); extends
  [rfc-crewmate-profile-settings.md](rfc-crewmate-profile-settings.md) §3 by one
  row. Sits inside [rfc-mate-owned-agent-config.md](rfc-mate-owned-agent-config.md)
  §5.1/§5.4 and the 2026-11-04 decision
  ([conductors stay simple](../decisions/2026-11-04-conductors-stay-simple-and-new-abilities-go-to-workers.md)):
  a mate copies a custom agent's abilities **once, as its starting point**, and
  grows through its own Abilities afterwards. Nothing here adds anything to a
  conductor.

## 1. Summary

A new Bot's worker starts as the source-defined worker base plus the edition's
additions, and nothing in the create path says so. The user learns the Bot
cannot reach Slack, Chorus or a data source only after a worker run has already
failed. The gateway knows the answer at create time
(`mate_capability_catalog.dispatch_summary`) and already has a reviewed
create-then-add-abilities flow (`EnrollAsBotDialog`). This RFC surfaces that
knowledge at the three moments the user is already looking: the create dialog,
the `[Goal set]` kickoff, and the Profile card. Every default stays as it is:
nothing is added to a Bot without a press on an Add or a card, and the Dynamic
capabilities preview stays off.

## 2. Motivation

### Current state (at `45c0cddb31`)

- **Create.** The header comment of `website/src/pages/members/NewCrewmateDialog.tsx`: "Every
  crewmate starts as Kiro Crew's own source-defined worker; abilities are added
  afterwards in its editor (Add abilities), so there is no template to pick
  here." Its Advanced region holds Goal, Workspace, Model, Triggers and Session
  colour (`WorkspaceField`, `ModelField`, `TriggersField`, `SessionColorField`);
  no field, hint or sentence mentions abilities. The only ability text on the
  card is the Goal hint (`pages.membersPage.create_job_hint`).
- **First turn.** `welcome.offer_tools` (`src/kiro_crew/crewmate_prompts.json`)
  tours exactly three `ui.show` locations, `members.profile-tab`,
  `members.schedule-tab`, `members.goals-tab`, and says "Put no other location
  in that guide." `src/kiro_crew/docs/ui-index.generated.json` has `members.edit` and
  `members.profile` but no location for the editor's Abilities section.
- **Goal set.** `goal_setup.kickoff_prompt` builds the hidden `[Goal set]` turn
  from `goal_setup.kickoff_head` through `goal_setup.one_thing`.
  Step 2 (`goal_setup.kickoff_step2`) hands research to a worker with
  `session_create` right away. No line names what that worker can reach.
- **The data exists, and is withheld.**
  `mate_capability_catalog.dispatch_summary(spec, catalog, report)` renders "Your worker's MCP servers:
  …" and "This gateway also has, and your worker lacks: …" (`dispatch.worker_has`,
  `dispatch.gateway_has`). `session_control._add_worker_capabilities` attaches
  it to a conductor's `session_create` / `session_send` result as
  `worker_capabilities`, but `crewmate_dispatch_capabilities._note` returns
  `""` whenever `crewmate_dynamic_capabilities.enabled_for(member)` is false,
  and that flag is "off by default" (the module docstring of
  `crewmate_dynamic_capabilities.py`).
  So for every Bot created from the standard dialog the line is computed for
  nobody.
- **Failure path today.** The worker opens its report with
  `mate_worker.MISSING_CAPABILITY_PREFIX`; `mate_thread_wakes.report_prompt`
  tells the conductor; with the preview off the conductor's first
  `request_capabilities` raises the Preview opt-in card instead of the
  capabilities card (`api_member_self_propose`'s `opt_in` branch and
  `_raise_opt_in` in `handlers/member_self.py`); the user
  presses **Turn on**, then presses the real card, then the work is re-sent
  (`goal_setup.capabilities_added`). One wasted run and two cards for a fact
  known at create.
- **Profile card.** The `crew-profile-dynamic-capabilities` row of
  `pages/members/CrewProfileSettings.tsx` shows "Dynamic capabilities · Let
  {name} ask you for tools" (`pages.membersPage.profile_dynamic_capabilities_switch`),
  whose hint ends "Off: only you add them, in {name}'s advanced settings." The
  list it points at is two clicks away: pencil (`members.edit`) → General →
  **View abilities** (the `next-open-advanced` button in
  `CrewEditorNextDialog.tsx`, `crewEditorNext.open_capabilities`), and the
  Profile tab never says the list
  exists.
- **A precedent already on main.** `pages/overview/EnrollAsBotDialog.tsx`:
  "Enroll as bot" on a custom agent creates a Bot on the built-in worker, then
  mounts `WorkerKitPane` with that agent as `initialTemplate`, so its abilities
  are previewed and written only on confirm, with a footer of **Finish without
  abilities** / **Done** (`docs/system-specs/modules/crew-mode.md`, "Custom
  agents tab").
  That is the two-step the Bots page's own "+" lacks.

### Problems

1. The one decision that shapes what a Bot can do is made silently, at create,
   by omission.
2. The recovery costs a failed run and two cards, once per Bot per missing
   system, and the second card is a *preview opt-in* the user did not ask about.
3. The hand-off summary is gated on a preview flag although it grants nothing;
   it is text.
4. Abilities are undiscoverable from the surface the user actually visits (the
   Profile tab).

On the author's install: three Bots beside a chat agent with 30 MCP servers;
one Bot has 18 added, two have none. Both were created from the standard dialog.

## 3. Goals

- G1. Before a Bot's first worker run, the user has seen, in words, what its
  worker can reach and that more can be added.
- G2. A create can carry an ability set the product already knows how to add
  (a custom agent's), through the reviewed add, with nothing written before a
  press on Add.
- G3. The `[Goal set]` kickoff names the worker's reach before it dispatches,
  whether or not the Dynamic capabilities preview is on.
- G4. The Abilities list is one click from the Profile tab.

## 4. Non-goals

- Turning the Dynamic capabilities preview on by default, or copying any
  agent's abilities to a Bot without a press. Both widen what runs without
  asking; they are a separate product decision.
- Adding anything to the conductor (decision 2026-11-04).
- Changing the welcome tour (`welcome.offer_tools`) or adding a UI-index
  location for the Abilities section; §5.3 gives the discoverability without it.
- A worker-base read for a Bot that does not exist yet (see Open question 2).
- The adjacent failure where a worker server exists but cannot start
  ([#32778](https://github.com/kirodotdev/KiroCrew/issues/32778)), and copying a
  whole Bot ([#36866](https://github.com/kirodotdev/KiroCrew/issues/36866)).

## 5. Design

### 5.1 Create dialog: say the starting point, offer the reviewed add

Two additions to `NewCrewmateDialog.tsx`, both inert unless used.

**a. One sentence under Create**, in the card's muted hint register:
"Starts with Kiro Crew's basics. Add more under Abilities." No count: at
`45c0cddb31` the only reads of a worker's reach take a member name
(`GET /api/agents/{name}/capabilities`, `/worker-kit`), and the Bot does not
exist yet. The sentence is static text in `pages.membersPage`.

**b. Advanced → "Copy abilities from"**, a `SimpleSelect` over the installed
custom agents (the same list the editor hands `WorkerKitPane` as
`kiroAgentOptions`, read from `GET /api/agents/installed` by
`useCrewEditor`'s `agents-installed` query), default "None". The create request is
unchanged: `kiro_agent` stays `DEFAULT_CREWMATE_TEMPLATE`, since a create may
only start from the
built-in worker (`mate_template_unsupported`). When a pick was made and the
create succeeds, the dialog does what `EnrollAsBotDialog` does after its create:
its body becomes `WorkerKitPane` with `templates={[pick]}` and
`initialTemplate={pick}`, the footer offers **Finish without abilities** until
the add lands and **Done** after, and the dialog cannot close while a create or
an add is in flight. The chat opens (`first_greeting`) on Done or
Finish, as it does today on create. With no pick, nothing changes: create
closes the dialog and opens the chat exactly as now.

Reuses: `WorkerKitPane` (`templates`, `initialTemplate`, `onApplied`), its
`POST .../worker-kit/preview` and `PUT .../worker-kit` (`api_member_worker_kit`,
owner-only, revision-checked, purely additive), and the Enroll strings
(`enroll_skip`, `enroll_done`) or copies of them under `pages.membersPage`. No
server change.

### 5.2 `[Goal set]` kickoff: name the reach, flag or no flag

In `goal_setup.kickoff_prompt`, after
`goal_setup.kickoff_head` and before step 2, one new line and one new step:

- **Reach line**, the `dispatch_summary` text for this member
  (`mate_capability_catalog.worker_spec(member)` and the member's catalog, as
  `crewmate_dispatch_capabilities._note` reads them, minus the `report`
  argument since no worker session exists yet). Computed in
  `goal_setup._goal_prompt`'s `build`, off the loop beside
  `dashboard_line`, and passed to `kickoff_prompt` as a new `reach: str = ""`
  argument, so `kickoff_prompt` stays a pure function of its inputs. Added
  **without** the `enabled_for` gate: the summary is server names, it invites
  nothing by itself, and the tools it mentions (`request_capabilities`) are
  mounted either way (`crewmate_dynamic_capabilities.py`, module docstring).
  Rendered through
  `dispatch.worker_has` / `dispatch.gateway_has` as today, so the wording stays
  in one place. A catalog read that fails leaves the line out and logs, as
  `own_worker_note` does; the kickoff must never fail on it.
- **New step 1b** (`goal_setup.kickoff_reach`, a new prompt key with an
  `_about` entry): "Before you hand this off, say in one sentence which systems
  goal {n} will likely need that your worker cannot reach, from the list above,
  and ask whether to request them. If it needs none, say so in a few words and
  go on." Then step 2 as today.

What happens on the answer is already designed: preview on,
`request_capabilities` raises the capabilities card; preview off, its first
call raises the Preview opt-in card, **Turn on** raises the held request
(`goal_setup.tool_access_on`), **Not now** leaves it off and every later call
is refused with a plain line (`crewmate_dynamic_capabilities.py`, module
docstring). The kickoff is a hidden turn (`handlers.guide.REQ_OWN_GOAL_TURN`),
and a capabilities request from such a turn is admitted only when its draft
names servers this gateway already runs (`api_member_self_propose`'s
`not_user_turn` check and `_gateway_servers_only`), which is exactly the
`gateway_has` list, so
no new admission rule is needed.

`crewmate_dispatch_capabilities._note` is **not** changed: the hand-off result
line keeps its gate, since this RFC does not relitigate the preview. Open
question 1 asks whether it should.

### 5.3 Profile card: an Abilities row

In `CrewProfileSettings.tsx`, directly above the Dynamic capabilities
`RowGroup`, one `RowStatic` with the `Puzzle` icon: label **Abilities**
(`crewEditorNext.section_capabilities`), sub-line
"{n} connections" from the member's capability rows (`crewCapabilitiesKey`,
`GET /api/agents/{name}/capabilities`, counting `mcpServers` rows; "—" while the
read is pending, and the shared `ErrorNotice` when it failed, as the other rows
do), and a **View abilities** button (`crewEditorNext.open_capabilities`) that
opens the editor on section `builtFrom`, the same navigation General's
`next-open-advanced` button performs. The Dynamic
capabilities hint's last sentence changes from "in {name}'s advanced settings"
to "under Abilities above", so the two rows read as one story.

### 5.4 Strings

New: `pages.membersPage.create_starts_with`, `create_copy_abilities`,
`create_copy_abilities_none`, `create_copy_abilities_hint`,
`profile_abilities_count`; prompt key `goal_setup.kickoff_reach`. Changed:
`profile_dynamic_capabilities_hint` (last sentence). Each with its
`en.context.json` entry; `en-XA` and the locale catalogs regenerate as the
i18n parity tests require.

## 6. Migration plan

Three PRs, independently shippable and abandonable. Order is by value per
line changed.

**Phase 1 — §5.3 Profile row** (frontend only).
Exit: on a Bot's Profile tab an "Abilities" row shows the server count and a
"View abilities" button; pressing it opens the editor on `builtFrom`
(`next-open-advanced`'s target); a capabilities read that fails shows the
`ErrorNotice` and leaves the Dynamic capabilities row usable; the Dynamic
capabilities hint names "Abilities above". `rfc-crewmate-profile-settings.md`
§3 gets the row listed in the same PR.

**Phase 2 — §5.2 kickoff reach line** (Python + `crewmate_prompts.json`).
Exit: `kickoff_prompt` output for a member whose preview is off contains the
`dispatch.worker_has` text and, when the gateway has servers the worker lacks,
the `dispatch.gateway_has` text and the `kickoff_reach` step; for a member with
no lacking servers the `gateway_has` line is absent; the prompt-key audit test
passes with the new `_about` entry; `_note`'s gate is untouched (its existing
tests still pass unchanged).

**Phase 3 — §5.1 create dialog** (frontend only).
Exit: the create card shows the starting-point sentence; with no Advanced pick,
`POST /api/agents` body and the post-create navigation are byte-identical to
today (existing dialog tests pass unchanged); with a pick, after create the
dialog shows `WorkerKitPane` for the new Bot with that template preselected,
`Finish without abilities` closes without a `PUT`, `Add` performs the preview
then `PUT`, the dialog refuses to close while either is in flight, and the chat
opens afterwards.

## 7. Backward compatibility

No config, file or API shape changes. Existing Bots gain only the Profile row
and, on their next `[Goal set]`, the reach line. A Bot created with no pick is
created exactly as today. The Enroll dialog is untouched.

## 8. Security considerations

- Nothing is added to a Bot's file without the user pressing Add (§5.1) or a
  card (§5.2); both paths are the existing owner-only, revision-checked writes.
- The reach line exposes server **names** only; `dispatch_summary` already cuts
  names to a plain character set and a bounded count (its docstring), and
  never a transport.
- The hidden-turn admission rule for capability cards is unchanged
  (`_gateway_servers_only`): a registry install or a skill still needs a turn
  the user sent.
- The Dynamic capabilities default is unchanged; the opt-in card remains the
  only way in.

## 9. Alternatives considered

- **Preview on by default, or copy the default chat agent's abilities to every
  new Bot.** Both widen auto-approvals silently. Out of scope by §4.
- **A fourth welcome-tour stop at Abilities.** Needs a new UI-index location and
  touches the generator (`ui_index.py`); §5.3 gives the same discoverability
  from the tab the tour already visits.
- **Drop the create-dialog picker and rely on Enroll.** Enroll lives under
  Customize → Custom agents, the page for agent templates; a user creating a
  Bot from the Bots page's "+" never passes it. §5.1 brings the same step to
  that door.
- **Gate the reach line on the preview like `_note`.** That is today's
  behaviour, and the reason the first run fails.
- **Do nothing.** The path recovers; it costs a failed run and two cards per
  missing system, once per Bot.

## 10. Open questions

1. Should `crewmate_dispatch_capabilities._note` lose its `enabled_for` gate
   too, so a conductor sees the reach on every hand-off result, not only at
   `[Goal set]`? Same argument (text grants nothing); wider blast radius (every
   `session_create` / `session_send` result). Not proposed here; a maintainer
   call.
2. Is a count worth a new member-less read (`GET /api/capability/worker-base`:
   the base's server names after the edition's additions) so §5.1a can say
   "and {n} connections"? Phase 3 ships the static sentence without it.
3. §5.1b wording: "Copy abilities from" vs the Enroll page's framing, which
   avoids the word "copy" because "Bots with their own copy" means something
   else there (`crew-mode.md`, "Custom agents tab"). "Start with the abilities of" is the
   candidate.
4. Which of the three phases, if any, the maintainers want. They are
   independent; §6 orders them by value per line.
