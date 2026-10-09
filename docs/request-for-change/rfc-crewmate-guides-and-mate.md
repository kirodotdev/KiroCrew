---
title: Crewmate guides, change cards and Mate, a first crewmate
status: accepted
author: buluoray
created: 2026-10-08
last-audited: 2026-10-08
audited-at: 4f9c5697b9
doc-pr: 18326
implementation-prs: [18301, 18317]
tracking-issues: [18300, 18315]
supersedes: []
superseded-by: []
---

# RFC: Crewmate guides, change cards and Mate, a first crewmate

- Status: accepted — the product owner's decision of 2026-10-08 after the team
  review; maintainer acceptance is being requested on the doc PR.
- Author: buluoray
- Amends sections of [rfc-crewmates-launch.md](rfc-crewmates-launch.md),
  [rfc-crewmate-greeting.md](rfc-crewmate-greeting.md) and
  [rfc-crewmates-chatted-list.md](rfc-crewmates-chatted-list.md) (see "What this
  amends"). It replaces no whole document, so `supersedes` is empty.
- Implementation: [#18301](https://github.com/kirodotdev/KiroCrew/pull/18301)
  (Part 1) and [#18317](https://github.com/kirodotdev/KiroCrew/pull/18317)
  (Part 2), stacked. Tracking: [#18300](https://github.com/kirodotdev/KiroCrew/issues/18300),
  [#18315](https://github.com/kirodotdev/KiroCrew/issues/18315).
- Claims about today's code were checked at `4f9c5697b9` (main).

## Summary

Any crewmate can answer "where is X?" and "how do I do Y?" about Kiro Crew by
pointing at the real control on the user's screen, and can prepare a change as
a card that applies only when the user presses it. Both come from one MCP
server, `kirocrew-guide`, that only reads or offers.

On top of that, Kiro Crew creates one ordinary crewmate, Mate, so a person who
opens the Crewmates page meets a crewmate that speaks first, explains what
crewmates are, and asks to be named.

## Problem

A crewmate asked "where do I turn on the terminal?" can only describe the
dashboard from memory. It cannot see where a control lives on this build, cannot
show it on the live page, and cannot prepare a change for the user to confirm.
The user finds every page and switch alone.

A person who opens the Crewmates page for the first time sees "No crewmates
yet", or a four-step flow that asks them to invent and configure a crewmate
before they know what one is for. Nothing on the page speaks first.

## Decision

### Part 1: platform guides and change cards

**One server.** A managed MCP server, `kirocrew-guide`, carries `find_ui`,
`search_docs`, `find_setting`, `get_member_capabilities`, `diagnose_settings`,
the `guide_*` tools, `list_change_kinds`, `propose_change` and
`get_change_status`.

**Who mounts it.** The `kirocrew` template, every crewmate built from it, and
worker and dashboard sessions mount it by default. Conductors and background
templates do not. Its tools are granted by exact name through the
`allowedTools` ceiling. A governance ceiling wins over the grant. A tool added
to the server later is not pre-approved.

**Why its own server.** It is the unit of authorization and governance, and it
answers only dashboard turns. Folding it into `kirocrew-core` would grant it
wherever core is mounted and would mix a dashboard-only surface into a server
every session carries.

**It only reads or offers.** A guide highlights the real control. It resolves
the target through a registry of React refs on the live page, and runs reveal
steps first when the control sits behind a collapsed sidebar, a closed menu or
a phone layout. Every click is the user's.

A change card applies only when the user presses it. A card that widens what
an agent may do without asking (approval mode `auto`, sandbox off, a
crewmate's `autoApprove`) needs an acknowledgement tick plus the press. A
removal carries a caution line and the user's own confirm.

Trust-root areas are answered in words only, with no guide step and no change
kind: the security policy, computer use, secrets, and the approval-mode picker,
which is the agent's own ceiling.

**Only the user's own turn.** Only a running dashboard turn that the user sent
may start a guide or propose a card. A turn started by a loop, cron, app
injection, `session_send` or a subagent completion gets 403 `not_user_turn`.
Channel turns are refused. Those non-user dashboard turns can still use the
read tools.

**Where the rules live.** The safety rules live in each tool's description and
in the server's `next` hints, not in an agent prompt. They hold for every
crewmate that mounts the server and survive compaction. There is no special
assistant.

### Part 2: Mate, a first crewmate

**Created once.** On startup Kiro Crew creates one ordinary crewmate, Mate (key
`mate`), once, on fresh and existing installs alike. It is built from the
ordinary `kirocrew` template and has its own private memory. The user can
rename it and delete it. A deleted Mate is never re-created. Mate replaces the
"No crewmates yet" first state.

**When it opens.** The Crewmates page stays behind its Feature Preview. With the
preview on, a bare Crewmates visit opens Mate while the user has never exchanged
a message with it, ahead of every other landing rule. After the first exchange,
landing follows rfc-crewmates-chatted-list. For those users this replaces the
Meet CrewMates auto-open; the two never both fire. With the preview off nothing
is user-visible: Mate exists in the background, and a typed `/members` follows
main's ordinary landing.

**The first welcome.** Mate's first turn is a hidden kickoff. The model writes
the welcome from principles, not from a fixed text. It says who it is, what a
crewmate is compared with a session, and that it can help set up Kiro Crew, fix
problems in it, or show the user around. It asks what the user wants to call
it, never the user's own name. This changes rfc-crewmate-greeting §6 for Mate's
first welcome only. Other crewmates keep one greeting mechanism per create path.

**Renaming itself.** When the user answers, Mate renames itself through
`rename_self` on `kirocrew-guide`. The tool renames only the caller's own
crewmate thread, answers only a dashboard turn the user sent, validates the
name, and never renames the default crew. The chat header and the roster
update live.

**`default` stays the fallback.** `default` remains the shared fallback crew,
not Mate. Every chat with no crew picked folds into it, it uses Global memory,
and it cannot be deleted. The Crewmates roster lists it only once its thread
holds a message. The search still reaches it.

## What this amends

- [rfc-crewmates-launch.md](rfc-crewmates-launch.md) § 4 "02 Empty state and New
  crewmate": the "No crewmates yet" hero is no longer the first state; Mate is.
- [rfc-crewmates-launch.md](rfc-crewmates-launch.md) § 4 "08 Meet CrewMates
  onboarding": for a user who has Mate, the Crewmates-page auto-open is
  replaced by opening Mate. The on-demand entry and the explicit create path
  stay.
- [rfc-crewmate-greeting.md](rfc-crewmate-greeting.md) § 6 "Alternatives
  considered", the rejected model-written welcome turn: Mate's first welcome
  is one model turn. The warm and cold cards and their rules are unchanged for
  every crewmate.
- [rfc-crewmates-chatted-list.md](rfc-crewmates-chatted-list.md) § 3
  "Decision", landing order: a never-chatted Mate is opened first. The § 5
  older-gateway rule, which lists the default crew for a row without
  `last_chat_ts`, stays as written.

## Exists today

Checked at `4f9c5697b9`:

- No guide server exists. `_MANAGED_MCP_SERVERS` in `src/kiro_crew/agent.py`
  holds `kirocrew-cron`, `kirocrew-core`, `kirocrew-computer`,
  `kirocrew-dashboard`, `kirocrew-work`, `kirocrew-debug` and `kirocrew-panel`.
  `kirocrew-guide`, `mcp_guide`, `not_user_turn`, `_turn_user_sent`,
  `rename_self` and `rename_member_display` have no hits under `src/` or
  `website/src/`.
- The ceiling the grants go through exists: `build_agent_config` ends with
  `auto_approve._apply_allowed_tools_ceiling`, and per-template grant tuples
  such as `_CONDUCTOR_CORE_GRANTS` and `_MEMBER_DASHBOARD_GRANTS` live in
  `agent.py`.
- No first crewmate exists. There is no `first_crewmate` module and no `mate`
  key. The empty state renders `pages.membersPage.empty_title` ("No crewmates
  yet") in `MembersPage.tsx`, beside the on-demand `meet_crewmates` entry.
- `useMeetCrewmatesGate` in `website/src/hooks/useMeetCrewmatesGate.ts` opens
  the flow once, at the end of the first-run tour or on the first Crewmates
  visit, until `dashboard.crewmates_onboarded` is set.
- The greeting is one hook, `useMateGreeting` in
  `website/src/pages/members/mateGreeting.ts`, with `COLD_AFTER_MS` at six
  hours. It makes no model call.
- `listedByDefault` in `website/src/pages/members/rosterFilter.ts` lists a row
  with `last_chat_ts > 0` or a star, and keeps the older-gateway rule, which
  always lists the default crew, for a row without `last_chat_ts`.
  `crew_recency.record_user_chat` writes that record.
- The default crew cannot be deleted: `api_kirocrew_agent_delete` in
  `dashboard/agent_admin/crew_removal.py` refuses `cfg.default_agent`, and the
  CLI delete refuses it too. `validate_member_name` in `members.py` is the
  name gate.
- The Crewmates page sits behind `PREVIEW_CREW` in
  `website/src/utils/previewFlags.ts`.

## Alternatives considered

- **A special built-in assistant** with its own system prompt and Global
  memory. Rejected in the team review: a long-lived crewmate should stay
  ordinary, its rules must survive compaction, and a prompt does not. Putting
  the rules in tool text holds them for every crewmate.
- **Turning `default` into Mate.** Rejected: `default` receives every chat with
  no crew picked and uses Global memory. A crewmate the user names and shapes
  needs its own memory and must be deletable.
- **Putting the guide tools in `kirocrew-core`.** Rejected: core is mounted far
  more widely than the guide is allowed to reach, and the server is the unit
  a governance ceiling grants or withholds.
- **Letting the model drive the page or apply changes.** Rejected: every click
  and every write stays the user's.

## Rollout

Two stacked pull requests, Part 1 first:

1. [#18301](https://github.com/kirodotdev/KiroCrew/pull/18301): the
   `kirocrew-guide` server, its default mount and grants, the guide layer, the
   change cards and the user-turn admission.
2. [#18317](https://github.com/kirodotdev/KiroCrew/pull/18317), on top of #18301:
   Mate, its hidden first welcome, `rename_self`, the landing rule and the
   default-crew roster rule.

Part 2 depends on Part 1 for `rename_self` and for the guide tools Mate offers.
Part 1 ships on its own.
