---
title: CrewMates launch — one vocabulary and nine screens for AI teammates
status: accepted
author: CrysisDeu
created: 2026-09-22
last-audited: 2026-09-22
audited-at: 87553ba866
doc-pr:
implementation-prs: [12797, 12798, 12805, 12806, 12924]
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
sync made a crewmate out of every custom agent under `~/.kiro/agents` — a
`config.agents` row with no `member_id`, on the shared memory store. An
existing install can therefore carry one crewmate per custom agent, most of
them never opened. The launch review first decided a user-facing opt-in step
here; the product owner replaced it on 2026-09-23 with the decision below.

Decided:

- No user-facing step. A one-time migration runs at gateway startup: a
  crewmate that an earlier sync generated (empty `member_id`, the shared
  `default` store, bound to a custom agent of the person's own — never the
  runtime's own, a package's or a private copy) and that was never chatted
  with is removed from the roster. Only the config row goes; the agent file
  under `~/.kiro/agents` and any transcript stay.
- A generated crewmate the person did chat with stays exactly as it is, on its
  V1 binding. A memory binding is identity and is chosen only at creation; the
  migration does not provision or rebind.
- If chat history cannot be read, nothing is removed and the pass runs again
  next boot. A marker records a completed pass so it runs once.
- A person with custom agents and no crewmate is served by screen 02's empty
  state and **New crewmate**; there is no offer to add agents in bulk.

Reasoning: the generated rows are leftover state, not a choice the person
made, so removing the unused ones needs no confirmation; a binding is identity,
so the kept ones are not rewritten; there is no screen to maintain, translate
or review; and the removal drops nothing the person made — every agent file
stays, and one click re-enrols any of them. A generated row the person edited
but never chatted with (a model, a picture, triggers) still matches the rule
and is removed with those edits; that is accepted: the row itself is the
sync's, not the person's, and the crewmate the edits describe is one click
away. Member-aware edits that stamp `member_id` or allocate a store take the
row out of the rule.

### 04 Crewmate detail page

Decided: the existing detail page is kept as is and becomes the one place a
crewmate is configured. The separate "Edit crewmate" modal is dropped from the
plan. Manager metaphor: the detail page is the HR file.

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
  on the shared store, and touches no agent file or transcript.
- Threads and teams add new storage beside the existing transcript and roster;
  nothing existing changes shape.

## 7. Acceptance

The launch is complete when, on main:

- No user-visible string on the Customize page, the Crewmates roster, the
  crewmate panel or the first-run flow says "Agent Capabilities", "Agent
  template", "crew member" or "Runs on".
- The crewmate panel opens on Notes and offers exactly Notes, Work log,
  Dashboard.
- A new install reaches a crewmate's first greeting through either the
  four-step flow or New crewmate without seeing a settings form.
- An install carrying crewmates an earlier sync generated loses, on its first
  start and once, exactly the rows § 03 names: never chatted with, no
  `member_id`, the shared store, bound to a custom agent of the person's own.
  Chatted rows, package-sourced rows and every agent file are untouched.
- A crewmate's chat contains no auto-nudge, cron or sub-agent envelope rows.

## 8. PRs implementing this

Open at the time of writing:

- [#12797](https://github.com/kirodotdev/KiroCrew/pull/12797) — reply threads,
  API half (screen 07).
- [#12798](https://github.com/kirodotdev/KiroCrew/pull/12798) — one-time
  startup prune of sync-generated crewmates (screen 03).
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
