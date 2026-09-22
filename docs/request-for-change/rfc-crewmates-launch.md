---
title: CrewMates launch — one vocabulary and nine screens for AI teammates
status: accepted
author: CrysisDeu
created: 2026-09-22
last-audited: 2026-09-22
audited-at: 87553ba866
doc-pr:
implementation-prs: [12797, 12798, 12805, 12806]
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

### 03 One-time opt-in for existing custom agents

Why: earlier releases created a teammate from every agent template
automatically, so an existing install can carry many. The launch lets the
person choose which of their custom agents become crewmates.

Decided:

- The step shows once, over the Crewmates page, only when the person has
  custom agents and no crewmates. It lists the existing custom agents,
  pre-checked, with two actions: **Add N crewmates** and **Not now**. Either
  action records the step as done; it never shows again.
- Nothing changes for existing chats.
- After the add, the roster shows the new crewmates and **the most recently
  used crewmate's chat opens by default**. There is no "joined your crew"
  banner and no "Pick a crewmate" landing sentence once crewmates exist.

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
- The opt-in step is gated by one new config key with a `false` default; an
  install that already has crewmates never sees it.
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
- An install with custom agents and no crewmates is offered the opt-in once.
- A crewmate's chat contains no auto-nudge, cron or sub-agent envelope rows.

## 8. PRs implementing this

Open at the time of writing:

- [#12797](https://github.com/kirodotdev/KiroCrew/pull/12797) — reply threads,
  API half (screen 07).
- [#12798](https://github.com/kirodotdev/KiroCrew/pull/12798) — one-time
  opt-in for existing custom agents (screen 03).
- [#12805](https://github.com/kirodotdev/KiroCrew/pull/12805) — crewmate
  panel: Notes, Work log, Dashboard (screen 06).
- [#12806](https://github.com/kirodotdev/KiroCrew/pull/12806) — Customize
  page and crewmate vocabulary (screens 01 and 04, § 3).

Branches not yet open as PRs; each will add its number to
`implementation-prs` when it opens:

- `feat/crewmates-page-empty-create` — empty state and New crewmate (screen 02).
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
