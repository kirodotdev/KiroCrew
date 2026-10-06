---
title: CrewMates launch — one vocabulary and nine screens for AI teammates
status: accepted
author: CrysisDeu
created: 2026-09-22
last-audited: 2026-10-06
audited-at: 9348a25a34
revision: 2026-10-01 — §11 roster-as-floating-card amendment
doc-pr:
implementation-prs: [12797, 12798, 12805, 12806, 12924, 14897, 14914, 14925, 15103, 15280, 16317]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: CrewMates launch — one vocabulary and nine screens for AI teammates

- Status: accepted — the product owner's decision record from the CrewMates
  launch review of 2026-09-22, merged ahead of most of the implementation
  because the First Principles lane reads a product-shape decision off the base
  branch. Every "exists today" claim below was checked at `87553ba866` (main,
  2026-09-22); citations name symbols, not line numbers.
- Author: CrysisDeu
- Related: [rfc-conductor-work-ledger.md](rfc-conductor-work-ledger.md) (the
  work ledger the Work log tab reads), [rfc-append-only-ledger.md](rfc-append-only-ledger.md)
  (the per-crewmate log the Work log tab will read once it lands),
  [rfc-orchestrator-chat-sessions.md](rfc-orchestrator-chat-sessions.md) (the
  retired Crew Mode this page grew out of).

## 1. Summary

Kiro Crew has three things a person can shape and talk to, and until now the
product called them by overlapping names: an *agent* was sometimes the thing
you chat with, sometimes the template it was made from, and sometimes the
persistent teammate with its own memory. The launch review fixed one
vocabulary and nine screens.

A **crewmate** is your AI teammate. You give it a job; it keeps working while
you are away. It has a name, an avatar, its own memory, its own chat and its
own notes. A **custom agent** is what a crewmate is built from. The page where
you shape both is **Customize**. A crewmate's chat shows only what it says to
you; everything else it does lands in its own panel (Notes, Work log,
Dashboard). Crewmates can be grouped into **teams**, and a team has a view of
its own. This document records those decisions so the pull requests that
implement them can cite a decision rather than propose one.

## 2. Why a decision record

Each of the screens below replaces or removes something a person can see
today: the "Agent Capabilities" page title, the "Agents" and "Agent templates"
tabs, the per-crewmate "Crew summary" tab, the controls that sat above the
crewmate roster, the flat chat rendering. Under
[README.md § Status vocabulary](README.md#status-vocabulary), a change to a
user-facing default or the removal of a user-facing capability must trace to a
document here whose status is `accepted` or later, read off the base commit.
The first two launch PRs were blocked on exactly that: prose inside the change
is a proposal, not a decision.

The decision was taken in a product owner review on 2026-09-22 with the mocks
for every screen in front of the reviewers. This document is the durable copy
of what was decided. It does not re-argue the mocks; it says what they settled.

## 3. Vocabulary

The words below are the product's words. They apply to every user-visible
string, in every locale, on every surface (dashboard, docs, CLI help,
notifications).

| Say | Meaning | Never say |
|---|---|---|
| **Crewmate** | Your AI teammate. Has a name, an avatar, its own memory, its own chat, its own notes. Keeps working while you are away. | bot, member, crew member, agent (as the teammate) |
| **Custom agent** | A Kiro custom agent: prompt, tools, skills, model. What a crewmate is built from. Has no avatar and no memory of its own. | agent template, template |
| **Built from** | The field on a crewmate that names its custom agent. | Runs on, Agent template |
| **Chat** | The main conversation with a crewmate. | DM, session (in user copy) |
| **Thread** | A reply thread opened on one message inside a chat. | — |
| **Team** | A named group of crewmates with its own team view. | crew (as the group), squad |
| **Customize** | The page where you shape skills, connections, steering, hooks, custom agents and crewmates. | Agent Capabilities |

**"Agent" is no longer a top-level concept in the product.** The word
conflated custom agents, templates and crewmates. It survives only inside
"custom agent", and in code identifiers, routes and config keys, which this
document does not rename.

Positioning line, used wherever the product introduces crewmates for the first
time: **"Your AI teammates. Give them a job; they keep working while you are
away."** The singular form on a single crewmate's empty state is "Your AI
teammate. Give it a job; it keeps working while you are away."

## 4. Screens and decisions

Each screen below is a decision. "Today" names what is on main at the audited
commit; "Decided" is the shape the product owner accepted.

### 01 Customize page

Today: the sidebar and page title read "Agent Capabilities"
(`pages.kiroCrewAgentsPage.agent_capabilities` in `website/src/i18n/locales/en.json`),
with rail tabs "Agents" and "Agent templates".

Decided:

- The page is titled **Customize**. It is where a person manages skills,
  connections, steering, hooks, custom agents and crewmates.
- Rail tab **Crewmates**, subtitle "Your AI teammates".
- Rail tab **Custom agents**, subtitle "What a crewmate is built from. Pick one
  when you add a crewmate."
- The Crewmates tab shows the roster first. Nothing that a new person must read
  past sits between the tab and the roster.
- Four things leave the Crewmates tab and the Customize header: the
  private-memory notice, the **New sessions use** default-agent picker, the
  **Apply & Restart** button, and the custom agents glossary. The default
  agent is still set from Settings; Apply & Restart stays on the pages whose
  changes need a restart.
- Everywhere a crewmate's custom agent is named as a field — the editor, the
  roster column, the Settings table — the label is **Built from**.

### 02 Empty state and New crewmate

Decided:

- With no crewmates, the Crewmates tab is a hero: the ghost avatar, "No
  crewmates yet", the one-line positioning sentence in its singular form, and
  one button, **New crewmate**.
- **New crewmate** opens a dialog with four things: Name; Built from (the
  default agent or a custom agent); What it looks after (optional); Advanced,
  folded.
- After create, the new crewmate's chat opens and shows its first greeting.
  There is no separate confirmation.
- With crewmates present and none named in the URL, the Crewmates tab lands
  on the most-recently-used crewmate's chat: the one this browser last opened
  when it remembers one, else the one with the latest activity. There is no
  "Pick a crewmate" sentence and no "joined your crew" banner; below the
  medium breakpoint the tab still lands on the roster.

Why the landing rule: the tab is a place to talk to a teammate, not a menu of
them. A returning person almost always wants the conversation they left, and a
sentence asking them to pick is a step that answers nothing. Opening the last
chat is what a messaging app does; the roster stays one column away for the
rare switch. This replaces the earlier "the user picks" default (#11763), which
predated crewmates having conversations worth returning to. The auto-open is
an ordering of what already exists — the remembered crewmate first, activity
second — so a wrong guess costs one click and loses nothing.

### 03 Pruning the crewmates an earlier release generated

Why: releases before #12224 called the agent sync on every chat mount, and that
sync made a crewmate out of every agent spec it discovered — the person's own
under `~/.kiro/agents` and every package-installed one — as a `config.agents`
row with no `member_id`, on the shared memory store, stamped with the spec's
discovery source (`builtin`, `package`, or `aim`, the package source's older
name). An existing install can therefore carry one crewmate per synced agent,
most of them never opened. The launch review first decided a user-facing
opt-in step here; the product owner replaced it on 2026-09-23 with the decision
below, and it was revised on 2026-09-28 to cover package-installed agents and
to say what "chatted with" means.

Decided:

- No user-facing step. A one-time migration runs at gateway startup: a
  crewmate that an earlier sync generated (empty `member_id`, the shared
  `default` store, bound to an installed spec of the person's own or of a
  package — never one of the runtime's own agents (a Kiro Crew-owned spec, or
  a row stamped `kirocrew`), a private copy, or a spec that is no longer
  installed) and that was never chatted with is removed from
  the roster. Only the config row goes; the agent file and any transcript
  stay.
- "Chatted with" means the crewmate's own thread on the Crewmates page holds a
  turn, in its live transcript or an archived segment. A session elsewhere that
  ran the agent (a sub-agent, a cron job, an app slot, a plain chat), an
  activity record, or a thread that was opened and never written to does not
  count.
- A crewmate the person created (a `member_id` is set) is never removed,
  whether or not it was ever chatted with.
- A generated crewmate the person did chat with stays exactly as it is, on its
  V1 binding. A memory binding is identity and is chosen only at creation; the
  migration does not provision or rebind.
- A crewmate whose thread cannot be read is kept and named in the marker; the
  rest of the pass still runs. A marker records a completed pass so it runs
  once; the revised rule writes a new marker name, so an install the first
  pass left untouched gets one more run.
- A person with custom agents and no crewmate is served by screen 02's empty
  state and **New crewmate**; there is no offer to add agents in bulk.

Reasoning: the generated rows are leftover state, not a choice the person
made, so removing the unused ones needs no confirmation; a binding is identity,
so the kept ones are not rewritten; there is no screen to maintain, translate
or review; and the removal drops nothing the person made — every agent file
stays, and one click re-enrols any of them. A package-installed agent is
enrolled by the same sync as a person's own, so its generated row is the
same leftover state; the runtime's own agents and uninstalled specs are left
out because nothing re-enrols them in one click. A row the sync wrote for a
skill-view alias (`kirocrew-skill-view-*`) is not an uninstalled spec: the
runtime writes that file to project another agent's skills, nobody installs
or picks it, and it is removed under the same rule as any generated row. Only a turn on the crewmate's
own page counts as chatting, because that thread is the crewmate: another
session running the same agent file is a template use, and it survives the
removal untouched. One unreadable thread keeps only its own crewmate, so a
single bad file cannot hold the whole roster back on every boot. A description
edit does not protect a never-chatted generated row: the description is the
spec's copy, and the crewmate it describes is one click away. Any other edit
the person made -- a model, a picture, triggers, a colour, a star, a workspace,
a team, a local override in `config.local.json` -- takes the row out of the
rule, as do the member-aware edits that stamp `member_id` or allocate a store:
each is the person's own act on that row, so the row is theirs.

### 04 Crewmate detail page

Decided: the existing detail page is kept as is and becomes the one place a
crewmate is configured. The separate "Edit crewmate" modal is dropped from the
plan. Manager metaphor: the detail page is the HR file.

> **Amended 2026-09-30 (see § 10, CREW-18688) — ACCEPTED.** § 10 revises "the
> modal is dropped": a bot is edited in place on the Crewmates page via a modal
> that mounts the SAME editor the detail page uses (one editor, two front doors),
> not a second, divergent editor. § 10 now supersedes this screen-04 decision.

### 05 Crewmate chat shows only what it says to you

Today: a crewmate's chat renders every turn the runtime produces — auto-nudge
rows, tool folds, cron and sub-agent envelopes.

Decided:

- A crewmate's chat shows **only what the crewmate says to you**: findings
  that need you, questions with options, hand-offs. Status and progress go to
  the crewmate's panel (screen 06), never into the chat.
- Structure: avatar, name and time on the first message of a turn; each
  message in its own bubble; consecutive bubbles from one turn share the
  avatar.
- Corner rule for a run of bubbles from one turn: the first bubble has a small
  bottom-left corner; middle bubbles have small top-left and bottom-left
  corners; the last has a small top-left corner. Right corners are always
  full.

### 06 The crewmate panel: Notes, Work log, Dashboard

> Amended 2026-09-30: the order is now Dashboard, Work log, Notes and the panel
> opens on Dashboard. See the amendment at the end of this document.

Today: the right panel on the Crewmates page opens on a single **Crew
summary** tab (`CREW_SUMMARY_TAB_ID` in
`website/src/pages/members/MembersPage.tsx`) that mixes what the crewmate is
doing with how it is set up: template, model, workspace, memory binding, wake
sources and an in-panel "New schedule" dialog.

Decided:

- The per-crewmate panel has exactly three tabs, in this order, opening on the
  first: **Notes** (what it learned — its own markdown notes), **Work log**
  (what it did — a timeline), **Dashboard** (how things stand — the page it
  publishes itself).
- The **Crew summary tab is removed.** Its settings content — Built from,
  wake sources, memory, cloud — lives on the detail page (screen 04). The
  in-panel schedule-create dialog goes with it; schedules are created from the
  Schedule page or the detail page. The team-level view is screen 09.
- Manager metaphor: Notes is the team wiki, Work log is the weekly report,
  Dashboard is the project board.

Amendment (2026-09-29, CREW-18721):

- The per-crewmate panel gains a **fourth tab, Schedules**, after Dashboard. It
  lists only that crewmate's OWN schedules — the ones whose `member_id` is this
  crewmate — and its chip carries a live/total count badge, omitted entirely when
  the crewmate has none or the read fails.
- A schedule belonging to no crewmate stays on `/schedule` and appears on no
  crewmate's tab. `/schedule` remains the full cross-crewmate view, unchanged.
- **Create is available wherever a crewmate's schedules are shown**, so the tab
  carries the create form. This narrows the decision above, which sent creation to
  the Schedule page or the detail page: it applies to the removed Crew summary
  tab's dialog, not to this one. The form is draft-safe — every gesture that would
  unmount it asks first (`onBeforeLeave`), a create in flight refuses, and a window
  resize that re-docks the panel keeps the form mounted rather than discarding it.
- Unchanged by this amendment: the removal of the Crew summary tab, and the three
  original tabs, their order and the tab the panel opens on.
- Grounds: CREW-18721, a later product requirement that a crewmate's own schedules
  are visible on that crewmate and not only on `/schedule`. It supersedes this
  screen's read surface; the section 7 acceptance clause "offers exactly Notes,
  Work log, Dashboard" reads as those three plus Schedules from this date.

### 07 Reply threads (P1)

Decided:

- Any bubble — the person's or the crewmate's — can have a thread opened on
  it. A footer under the bubble shows the participants' faces, "N replies" and
  "Last reply <when>". The hover action is **Reply in thread**.
- An open thread lives in the right side panel: the quoted parent bubble,
  replies as small bubbles under the same corner rule, and a "Reply…"
  composer. The main chat stays visible.
- P1: the launch may ship without threads; when they ship, this is their
  shape.

### 08 Meet CrewMates onboarding

Decided: a four-step flow in the product's existing split-screen first-run
shell, not a single card.

1. **Meet CrewMates** — the positioning line and three example crewmates.
   Actions: Not now / Next.
2. **Name your first crewmate** — a name field with suggestions, and Built
   from. Actions: Back / Next.
3. **Give <name> a job** — what it looks after, when, and where it reports
   (this chat, or a Slack DM). Actions: Back / Create <name>.
4. **<Name> is ready** — one line saying when it starts. Action: Open
   <name>'s chat.

Amended at implementation (PR #12927): on step 3, "Slack DM" is a status row,
not a switch. The runtime offers no per-job Slack choice -- a connected Slack
always receives a scheduled run through the owner-DM leg, a disconnected one
cannot -- so the row reads "Active" or "Off" with the reason under it, and the
only delivery choice the flow offers is "Its own chat" (`hide_in_chat`). Step 2
calls the base agent "Starting setup" ("Standard (built in)" for the default),
and the crewmate name is held to the roster's agent-name grammar before Next.

Amended 2026-09-29 (PR #15085): the crewmate name is free-form. Step 2
only refuses a blank name; the server's `validate_member_name` is the gate,
and a name it refuses returns the user to step 2 with the reason under the
field.

Amended 2026-09-28 by the product owner (PR #14914): when the flow shows.
Having seen it -- finished or dismissed, recorded as
`dashboard.crewmates_onboarded` -- is the ONLY condition on showing it.
Existing crewmates and custom agents do not suppress it; this supersedes the
exclusion the first implementation applied, which kept the flow from any
workspace with custom agents because their earlier-sync crewmates were
screen 03's job. It opens once, at the first of: the end of the first-run tour
for a new user, or the first visit to the Crewmates page for everyone else,
including a workspace that finished first run before the flow shipped. The
Crewmates page keeps its on-demand **Meet CrewMates** entry. Reasoning: the
flow introduces the concept and creates one crewmate the person names; neither
depends on what the workspace already holds, and a person who opens the
Crewmates page is looking for exactly this.

Already shipped and shown in the review as evidence, not re-decided here:
crewmates are out of the ordinary session list; the ghost icon marks a
crewmate's session; each crewmate has one memory of its own; the sidebar
"New" menu.

### 09 Teams and the team view

Decided:

- The metaphor is manager and reports. Per-crewmate things live in that
  crewmate's panel (screen 06). The team-level view is the manager's desk: the
  team at a glance plus an inbox of everything waiting on you.
- **Team** is a new concept. A team is a name plus members. In this release a
  crewmate is in at most one team; crewmates without a team sit in a trailing
  "No team" group.
- The roster is grouped by team: a header row per team (icon, name, count,
  chevron) followed by its crewmate rows. Selecting a header opens the team
  view in the main pane; selecting a crewmate opens its chat as before.
- **New team** sits next to **New crewmate** in the roster's "+" menu. The New
  team dialog takes a name and the crewmates to include.
- The team view has three blocks, scoped to the team: a **status strip** (each
  crewmate's state — running, waiting on you, idle, paused — with today and
  this-week counts); **Needs you** (every question a team member asked, as its
  bubble with its option chips and an "Open chat" action; empty state "Nothing
  waiting on you."); **This week** (the team's work log, one line per item).
- Rejected: a whole-crew view as the landing when no crewmate is selected, and
  a pinned "Your crew" roster entry.

## 5. Out of scope

These were discussed in the review and are deliberately **not** decided by
this document:

- The backend change that gives a new crewmate its standing prompt and tools
  at creation time. It has no user-facing shape and is tracked separately.
- Whether a crewmate's built-in capabilities are best modelled as skills or as
  sessions. Open; needs its own call.
- A performance or retrospective view of a team. Not in this release.
- Whether a crewmate can belong to more than one team. This release assumes
  one; the data model should not make more than one impossible.
- Code identifiers, routes, config keys and file names that still say
  `member`, `agent` or `template`. The vocabulary in § 3 governs what people
  read, not what code is called.

## 6. Backward compatibility

- Renames are copy changes. Every route, i18n key name, config key and API
  stays; only rendered values change.
- Removing the Crew summary tab: a stored panel focus naming it falls back to
  the first tab, Notes, by the existing unknown-focus rule.
- The startup prune is gated by a marker file in the config directory, written
  once a pass completes; it removes only never-chatted rows with no `member_id`
  on the shared store bound to an installed spec of the person's own or of a
  package, or to a skill-view alias (`kirocrew-skill-view-*`), and touches no
  agent file or transcript. Other spec-less rows are untouched.
- Threads and teams add new storage beside the existing transcript and roster;
  nothing existing changes shape.

## 7. Acceptance

The launch is complete when, on main:

- No user-visible string on the Customize page, the Crewmates roster, the
  crewmate panel or the first-run flow says "Agent Capabilities", "Agent
  template", "crew member" or "Runs on".
- The crewmate panel opens on Notes and offers exactly Notes, Work log,
  Dashboard — plus Schedules, per the section 06 amendment of 2026-09-29
  (CREW-18721). (Amended again 2026-09-30: the panel opens on Dashboard and the
  order is Dashboard, Work log, Notes, Schedules. See the amendment at the end of
  this document.)
- A new install reaches a crewmate's first greeting through either the
  four-step flow or New crewmate without seeing a settings form.
- An install carrying crewmates an earlier sync generated loses, on its first
  start and once, exactly the rows § 03 names: never chatted with on the
  Crewmates page, no `member_id`, the shared store, bound to an installed spec
  of the person's own or of a package, or bound to a skill-view alias
  (`kirocrew-skill-view-*`). Chatted rows, created crewmates, runtime-owned
  rows, other spec-less rows and every agent file are untouched.
- A crewmate's chat contains no auto-nudge, cron or sub-agent envelope rows.

## 8. PRs implementing this

Open at the time of writing:

- [#12797](https://github.com/kirodotdev/KiroCrew/pull/12797) — reply threads,
  API half (screen 07).
- [#12798](https://github.com/kirodotdev/KiroCrew/pull/12798) — one-time
  startup prune of sync-generated crewmates (screen 03).
- [#14897](https://github.com/kirodotdev/KiroCrew/pull/14897) — the revised
  § 03 rule: package-sourced rows and the Crewmates-page definition of
  "chatted with".
- [#15103](https://github.com/kirodotdev/KiroCrew/pull/15103) — the prune
  also removes never-chatted rows bound to a skill-view alias (screen 03).
- [#12805](https://github.com/kirodotdev/KiroCrew/pull/12805) — crewmate
  panel: Notes, Work log, Dashboard (screen 06).
- [#12806](https://github.com/kirodotdev/KiroCrew/pull/12806) — Customize
  page and crewmate vocabulary (screens 01 and 04, § 3).

Branches not yet open as PRs; each will add its number to
`implementation-prs` when it opens:

- [#12924](https://github.com/kirodotdev/KiroCrew/pull/12924) — empty state,
  New crewmate and the returning-visit landing (screen 02).
- `feat/crewmate-chat-bubbles` — chat shows only what it says to you, grouped
  bubbles (screen 05).
- `feat/reply-threads-ui` — thread footer and side-panel thread (screen 07,
  frontend half).
- Teams: roster grouping, team view, New team (screen 09).
- Onboarding: the four-step Meet CrewMates flow (screen 08).

## 9. Provenance

Product owner review, 2026-09-22, with rendered mocks of all nine screens.
This document is the public record of that review; it carries no link to the
review's internal notes.

## 10. Amendment — edit a crewmate in place (CREW-18688)

Status: ACCEPTED 2026-09-30 (product owner). This section supersedes screen 04's
"the separate 'Edit crewmate' modal is dropped": a crewmate is edited in place
on the Crewmates page via the modal below.

The problem this fixes: to edit a bot from the Crewmates page today, the user is
navigated away to the crew manager (`/capabilities?tab=crews&crew=<name>`). That
drops them out of the crewmate thread they were reading, onto a different
top-level page. Leaving the member page to make a quick edit is the specific
experience the team decided to remove.

Decision: a crewmate is edited in place on the Crewmates page. The thread-header
identity pill opens the crew editor as a modal over the page. No navigation, no
page change. The user stays on the member they were looking at.

This revises screen 04. Screen 04's concern was a SECOND, divergent editor — two
editors that drift out of sync. This decision keeps ONE editor: the modal mounts
the same editor the detail page uses (`CrewEditorDialog` + `useCrewEditor`), so
there is one write path reached from two entry points, and nothing to drift. The
detail page stays the full "HR file" surface; the modal is the in-context edit
for "edit the bot I am looking at".

Implementation: the shared editor (hook + dialog) and the Crewmates-page wiring
ship first. Folding the crew manager (`KiroCrewAgentsPage`) onto the same shared
component, so "one editor" is literally true in code, is the committed follow-up.

Status: the follow-up is open. `KiroCrewAgentsPage` shares only the section
list (`useCrewEditorSections`) and keeps its own edit-sheet state, so a change to
the editor must be mirrored there until the fold-in lands.

Provenance: accepted by the product owner on 2026-09-30 (CREW-18688), recorded
here ahead of the implementation PR (#15275) so the decision lands on main
before the code that relies on it.

## 11. Amendment — the roster is a floating card, not a full-height column

Status: ACCEPTED 2026-10-01 (product owner). This section supersedes screen
01's "the Crewmates tab shows the roster first … one column away" layout and
screen 05's centred thread-header identity pill, **for the roster list only**.
The roster's contents, grouping, filters and the identity pill's edit behaviour
(§10) are unchanged; what changes is where the roster lives and how it is
summoned.

The problem this fixes: today the roster is a permanent full-height side column
beside every crewmate chat (`<aside>` with `w-[var(--roster-w)]`, a draggable
`ResizeHandle` on its right edge, in `MembersPage.tsx`). For a person with one
crewmate it is a column of one, taking fixed horizontal room from the only
conversation they have; for a person with several it is still always open,
whether or not they are switching. The chat is the place a person spends their
time, and the roster is a switch they reach for occasionally — the layout gives
them the opposite weights.

Decision:

- **The roster is a floating card, not a full-height column.** When shown it
  overlays the chat (it does not reserve a column of its own), anchored at the
  top-left under the crewmate avatar. Its height fits its contents — as tall as
  the number of crewmates, not the viewport — so a small crew gets a small card.
- **The crewmate avatar moves from the centre of the thread header to the
  top-left corner.** It stops being a centred identity pill and becomes the
  page's top-left anchor; its edit-on-click behaviour from §10 is unchanged.
- **A toggle sits directly under the avatar.** Functionally a hamburger: it
  shows and hides the floating roster card, which opens directly beneath it.
  The toggle is visible only while the card is **closed** — once the card is
  open it carries its own close affordance, so the hamburger steps aside (the
  same split the side-panel opener already makes: `panelChrome`'s `showOpener`
  hides the opener while the docked panel is open). The exact glyph and
  placement are provisional — "for now" — and may be refined as the surface is
  built; the toggle's *job* (show/hide the card) is the decided part.
- **The toggle and the card appear only when more than one crewmate is
  defined.** With exactly one crewmate there is nothing to switch to: no
  toggle, no card, and the chat takes the full width. The count that gates this
  is the roster the page already reads, not team membership.
- **Open/closed is persisted** across members and reloads, in the dashboard's
  existing `mc-` localStorage family (the same idiom as the roster filters and
  the side-panel-open flag — e.g. a new `mc-members-roster-open` key), per
  gateway origin. It is a property of the page's furniture, not of any one
  crewmate.

This replaces the roster UI entirely: there is no mode in which the full-height
column and the floating card both exist. The full-height `<aside>` and its
width variable are removed, not toggled.

Default when first shown (no persisted value, more than one crewmate): the card
starts **collapsed**. The point of the change is to give the chat the room; a
person who wants the roster open presses the toggle once and the choice sticks.

Why this shape:

- A floating card over the chat, rather than a resized column, keeps the chat
  at full width whenever the roster is not actively being used, which is most
  of the time.
- Height-fitting to the crew removes the empty expanse a short crew saw in a
  full-height column, and keeps the card from reading as a second permanent
  panel.
- Gating on `> 1` crewmate means the single-crewmate case — the first thing a
  new person has — carries no chrome for a choice they cannot make.

Backward compatibility: this is a layout change to one page. No route, API,
config key or i18n key name changes; the roster rows, grouping, filters and the
identity-pill editor are the same components in a new container. The one removed
affordance is the roster column's width `ResizeHandle` (see Open question
below). The new localStorage key is additive; its absence reads as the default
(collapsed) above, so an existing install loses no state.

Open questions:

1. **The width resize handle — RESOLVED 2026-10-01: removed.** A height-fit
   floating card has no column width to drag, so the `ResizeHandle` on the
   roster's right edge is removed outright. The card is not width-resizable or
   draggable.
2. **Dismiss-on-outside-click vs. explicit toggle only — RESOLVED 2026-10-01:
   toggle only.** The card closes only when the toggle is pressed again; a click
   into the chat behind it does not hide it. This matches the persisted-state
   model — hiding the switcher is a deliberate act, not a side effect of
   returning to the conversation.
3. **Narrow viewport (below `md`) — RESOLVED 2026-10-01: keep today's layout,
   but the gate applies on both.** The floating-card model is a wide-viewport
   concern; below the medium breakpoint the page stays single-pane and the
   roster keeps its existing full-screen behaviour — the floating card and the
   avatar/hamburger relocation do NOT apply there. What DOES carry across both
   layouts is the show-at-all gate: the roster affordance (the toggle + card on
   wide; the roster route/entry on narrow) appears only when more than one
   crewmate is defined — i.e. the built-in default plus at least one custom
   crewmate. With exactly one crewmate there is nothing to switch to on either
   viewport.

Implementation: frontend-only, in `website/src/pages/members/MembersPage.tsx`
and the shared list-shell/thread-header pieces it composes. The parity tests
that pin the roster to the shared list-shell classes
(`src/test/listShellParity.test.ts`, `src/test/kiroLightShellHooks.test.ts`)
and the resize-handle parity test (`src/test/ResizeHandle.parity.test.tsx`) are
the ones most likely to move with this change and must be updated in the same
commit.

Provenance: accepted by the product owner on 2026-10-01, recorded here ahead of
the implementation PR so the product-shape decision — a floating card replacing
a full-height panel, gated on crew size — lands on main before the code that
relies on it, per the First Principles lane's base-branch read.

## 12. Amendment — the panel lands on Dashboard, not Notes (CREW-18720)

Screen 06 fixed both the strip order and the landing tab in one sentence:
"exactly three tabs, in this order, opening on the first: **Notes**, **Work
log**, **Dashboard**", and § 7 made "the crewmate panel opens on Notes" an
acceptance criterion. Shipped, that is the wrong landing, and the product owner
asked for it changed on 2026-09-30.

The reason is what Notes actually holds. It renders the crewmate's own briefing
file, which the crewmate writes for itself as it works: absolute paths, branch
names, rulings it owes, half-sentences in whatever language it was thinking in.
That is working memory, not a page addressed to a person. Opening a crewmate put
it first — so the first thing a person saw was the one tab of the three that was
never written for them, and the only one they cannot change from the dashboard.

Amended:

- The three tabs stay exactly three and keep their bodies. Their order becomes
  **Dashboard**, **Work log**, **Notes**, and the panel opens on the first as
  before. The order is now how much of each tab is addressed to the reader:
  Dashboard is what the crewmate publishes for them, Work log is what it did,
  Notes is its own memory. Notes is one click away, never removed.
- The order and the landing remain ONE fact, not two settings. The default is
  `leadingIds[0]` in `usePanelTabs`, so a strip with no stored focus opens on
  whichever tab is first; nothing was added to let them disagree.
- A stored focus still wins, unchanged. Someone who was reading a crewmate's
  notes and comes back finds Notes still focused.
- The § 6 fallback sentence reads the same way with the new order: a stored
  focus naming the removed Crew summary tab falls back to the first tab, which
  is now Dashboard.
- Notes gains one line above the body naming whose notes these are and saying
  the tab does not change them. The tab already had no editor, and this is why
  (see the Notes section of `docs/system-specs/modules/crew-mode.md`); the line
  says so instead of leaving a reader to discover it by looking for the control.

§ 7's acceptance criterion is superseded by this paragraph: the crewmate panel
opens on Dashboard and offers exactly Dashboard, Work log, Notes.

## 13. Amendment — the panel becomes a Profile card plus Dashboard (#16317)

Status: shipped in [#16317](https://github.com/kirodotdev/KiroCrew/pull/16317)
by this RFC's author. This section supersedes screen 06's tab set, § 11's
roster column and § 12's order; § 06, § 7, § 11 and § 12 stay as the record of
the earlier decisions.

- The side panel leads with **Dashboard** only (`CREW_PANEL_TAB_IDS`), plus the
  standing Files entry. Dashboard is the crewmate's report.
- The thread-header identity pill opens a **Profile card** with four tabs:
  Profile, Schedules, Sessions, Goals.
- **Notes** is a pushed page inside Profile, not a panel tab.
- The **Work log** UI is removed.
- While a crewmate conversation is open on desktop, the standing roster is
  hidden and a switcher in the thread header replaces it.

The owning spec for this layout is
[crew-mode.md](../system-specs/modules/crew-mode.md). § 7's panel criterion is
superseded by this section: the panel opens on Dashboard, and Notes, Schedules
and the rest live in the Profile card.
