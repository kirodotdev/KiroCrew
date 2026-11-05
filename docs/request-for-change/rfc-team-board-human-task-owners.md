---
title: Team board with human task owners, a shared Team Bot, and consented Bot-to-Bot requests -- built as an app first
status: draft
author: JiaDe-Wu
created: 2026-11-06
last-audited: 2026-11-06
audited-at: 9c3c6b58e9
doc-pr: null
implementation-prs: []
tracking-issues: [30572]
supersedes: []
superseded-by: []
---

# RFC: Team board with human task owners, a shared Team Bot, and consented Bot-to-Bot requests

Status: draft. Nothing built; this document changes no code. Every code reference
was read at `9c3c6b58e9`. It is the design behind
[#30572](https://github.com/kirodotdev/KiroCrew/issues/30572) and asks for
direction, not for acceptance of every phase.

## Summary

A team that runs Bots on a shared Crew has no shared view. Each person directs
their own Bots; nothing holds who owns which task, the team goals, what the last
meeting decided, or how one person's Bot can ask another's a question without a
human relaying it.

This RFC proposes that the whole of it ships first as one app, **Team Board**,
using only what the platform already offers, and that the core changes it needs
are listed, ordered and cut to PR size, each one useful on its own:

1. **Phase 0, no core change.** The app stores tasks with a human owner, shows
   the board, exposes its tools to Bots as an MCP server, and carries a Team Bot
   and role Bots as bundles the owner imports.
2. **Phase 1, the gaps the app cannot close.** Let an app propose Bots; let an app
   handler learn the acting person; let a change card name its required approver.
3. **Phase 2, the policy.** A governance control for how open Bot-to-Bot requests
   are, and the audit record.
4. **Phase 3, across Crews.** Each person's Bots on their own Crew, once remote
   dispatch lands.

Bot-to-Bot requests go through the app's own request and answer records, not
through `session_send`, so the withholding of `session_send` is left as it is.

## Motivation

### Current state (measured at `9c3c6b58e9`)

- **There are three boards and none has a task owner.**
  - The conductor drawer board holds `task`, `state`, `step` and an optional `pr`
    (`src/kiro_crew/conductor_board_contract.py`, `ConductorBoardTask`).
  - The pipeline board's contract states that a conductor typing owner names into
    the cell that must say what a person should do is the defect the contract
    exists to stop (`src/kiro_crew/pipeline_board_contract.py`, module docstring`). An owner on that
    board is an explicit anti-goal.
  - The work ledger's `WorkBoardItem` carries a worker session key and no person
    (`src/kiro_crew/work_vocab.py`, `WorkBoardItem`).
- **The Crew board is owner-only.** `GET /api/crew-board` calls
  `require_owner_dashboard_request` first
  (`src/kiro_crew/dashboard/handlers/work_ledger_board.py`, the `/api/crew-board` read handler`).
- **A person has an identity, but no role.** `Principal` is the server-issued
  identity (`src/kiro_crew/shared_crew/contracts.py`, `Principal`) and `derive_principal`
  resolves it per request (`src/kiro_crew/shared_crew/identity.py`, `derive_principal`). Roles
  are design only: [rfc-per-person-execution.md](rfc-per-person-execution.md) is
  accepted and says roles are off until the operator creates `people_policy.json`
  (section 7), but `people_policy` has no hit anywhere in `src/` or `website/src/`.
- **Only the owner can confirm a change card.** Confirmation is refused with
  `owner_only` for anyone but the dashboard owner
  (`src/kiro_crew/dashboard/handlers/change_cards.py`, the `owner_only` refusal`), and a card has no
  field naming a required approver.
- **An app cannot install a Bot.** `ctx.crewmate` is held off by
  `PREVIEW_ENABLED = False` (`src/kiro_crew/apps/crewmate_sdk.py`, `PREVIEW_ENABLED`; the field
  is `src/kiro_crew/apps/context.py`, the `crewmate` field`) and `permissions.proposeCrewmate` is
  documented as a preview, switched off (the `permissions.proposeCrewmate` row of `docs/app-kit/manifest-reference.md`).
  The owner path works: importing a `.kirobot` file raises a `crewmate.import`
  card that the owner applies.
- **The meeting data exists, and Bots cannot read it.** The `meetings` app keeps
  transcripts, `tasks.json` action items and notes under its own data directory
  (`src/kiro_crew/apps/builtins/meetings/backend/store.py`, storage layout in the module docstring`) and serves the
  action items at `GET /api/apps/meetings/meetings/{id}/tasks`
  (`src/kiro_crew/apps/builtins/meetings/backend/routes/__init__.py`, the tasks route`). Another
  app reaches them only through an app token with a matching `permissions.api`
  prefix.
- **`session_send` is withheld on purpose.** It runs text as another session's
  user turn, and "nothing bounds WHAT is sent"
  (`src/kiro_crew/agent_parts/spec_grants.py`, the `session_send` entry`). Reach is bounded by the creator
  fence (`src/kiro_crew/dashboard/session_control_parts/caller_access.py`, `_outside_reach`,
  `src/kiro_crew/dashboard/session_control_parts/target_authorization.py`, `authorize_target`),
  which has no peer exemption. The 2026-09-13 decision declined an inbox
  subsystem and pointed at a narrow member-to-member allow
  ([the decision](../decisions/2026-09-13-crew-members-message-through-session-send-not-an-inbox.md));
  the PR it names, #10690, was closed unmerged.
- **A Team groups one person's roster.** `Team` is a name and an ordered list of
  crew names, written only by the owner's routes and CLI
  (`src/kiro_crew/crew_teams.py`, `Team`), and no agent-facing reader exists.

### Problems

1. Task ownership by a person is not representable on any board.
2. Nothing assembles the team picture, and the one input that would feed it
   (reviewed meeting action items) has no consumer.
3. One person's Bot cannot ask another's a question except through a human, and
   loosening `session_send` for everyone is the wrong remedy.
4. The permission model for cross-owner requests, and for how open a team wants to
   be, has nowhere to live.

## Goals

1. Every task on the team board has exactly one human owner, who is the only one
   (or hands it to someone) who changes it.
2. A Team Bot, shared by the Crew, keeps the board current, proposes tasks from
   reviewed meeting action items for a person to confirm, and drafts review
   documents. It asks other Bots; it does not read private Bot memory.
3. A Bot can send a question to another person's Bot and receive an answer, as
   data, with the owner of the receiving Bot in control: a per-Bot switch, off by
   default, that takes effect at once.
4. A team can choose to be more open than the default, and an organisation can
   cap that choice.
5. With nothing configured, Crew behaves exactly as it does today.

## Non-goals

- Relaxing the `session_send` withholding, or adding a member inbox subsystem.
- A hop counter or per-pair rate limit for Bot-to-Bot requests (declined in the
  2026-09-13 decision, "trust the agents").
- An owner field on the conductor or pipeline drawer boards.
- Reading another person's private Bot memory.
- Building any of Phase 1 to 3 before a maintainer has reacted to the approach.

## Design

### Principles

1. **A task has one human owner.** The owner answers for it and holds the domain
   knowledge, through their own Bots. What is shared is the Bot that tracks how
   tasks relate.
2. **An app first** ([TENETS.md](../../TENETS.md) tenet 8). Core changes are
   admitted only where the app cannot do the job, and each is its own PR.
3. **Requests are data.** A question and its answer travel as records the receiving
   Bot reads through a tool. Neither is run as a user turn in anyone's session.
4. **The owner of a Bot decides what it accepts.** The switch is on the Bot, not on
   the team.

### The Team Board app (Phase 0)

- **Data.** Tasks live in the app's own storage (`permissions.storage`), each with
  an id, title, a `principal id` for the owner, a status, links, and timestamps.
  The board is an app page (`ui.pages`). It is not one of the drawer boards.
- **Tools for Bots.** The app ships an MCP server through its manifest
  `mcpServers`. The platform registers an app's servers into the default agent spec
  under an `<app>:<server>` name (`_register_mcp_servers` in
  `src/kiro_crew/apps/bridges_mcp.py`), and the worker spec is derived from that
  spec (`rederive_worker_agent` in
  `src/kiro_crew/agent_materialization/worker_agent.py`, whose docstring names app
  MCP registration as a writer of the default spec). So the tools reach a Bot's
  **worker**, which does the work; a Bot's own chat is a dispatch-only conductor
  ([rfc-mate-owned-agent-config.md](rfc-mate-owned-agent-config.md)) and does not
  call them itself. Tools: list and read tasks, propose a change,
  `ask_bot(target, question)`, `pending_requests()`, `answer(request_id, text)`.
- **Requests.** `ask_bot` writes a request record addressed to the target Bot.
  The target Bot reads it with `pending_requests` and answers, within the target's
  switch. The answer is stored and returned to the asker as data. Every request
  and answer is recorded through `ctx.audit`.
- **Switches.** The per-Bot "accept requests from other Bots" switch is off by
  default and is set by the Bot's owner on the app's page. A team-level default
  can be set to open; the per-Bot switch can always turn a Bot off, immediately,
  and an open request that has not been answered is then dropped.
- **Meetings.** The app's backend reads `/api/apps/meetings/*` under a declared
  `permissions.api` prefix and proposes tasks from the reviewed action items. A
  person confirms each one on the board before it becomes a task.
- **Team Bot and role Bots.** Shipped as `.kirobot` bundles. The owner imports each
  one through the existing `crewmate.import` card, and so approves each.
- **Distribution.** Published in a team's own registry (`registries` in config), so
  another team can install the same app and change the roles, rules and Bots.

### Phase 1: what the app cannot do

| Gap | Today | Change |
|---|---|---|
| Propose Bots | `ctx.crewmate` is off | Switch it on (#21718), so the app proposes the Team Bot and role Bots as cards |
| Know who is acting | The signed principal claim (`backend.signedPrincipal`, `X-KiroCrew-Principal`) already carries an authenticated identity to an app backend, but its kinds are `agent-tool`, `app-token`, `owner-session` and `none`, and "an authenticated non-owner" is `none` with an empty `ownerId`. So on a team every person but the owner is indistinguishable | Give the existing claim a kind that names a person, resolved from `derive_principal`, so a request carries who is acting rather than only whether they are the owner. No new header and no accessor the app could be lied to through |
| Name an approver | A card is confirmed by the owner only | Allow a card to name a required approver, so a task owner confirms a change to their task |
| Read another app's reviewed output | Only by `permissions.api` prefix | A consented read of the `meetings` output, if the prefix route proves too coarse |

Each is one PR and stands without the others. The second is the one the owner
field depends on, because without it the app cannot tell which person set an owner.

### Phase 2: the policy

A governance control for how open Bot-to-Bot requests are. A control is a row in
`SCOPE_CATALOG` (`src/kiro_crew/platform/governance_parts/catalog.py`, `SCOPE_CATALOG`), a row in
`BASELINE_CONTROLS` (`src/kiro_crew/platform/governance_parts/baseline.py`, `BASELINE_CONTROLS`) and a
gate at the call site, as `capabilities.memory_writes` is gated
(`src/kiro_crew/mcp_core_parts/governance.py`, `_vet_memory_writes_governance`). An absent scope is permitted in
the oss edition, so adding the control changes nothing until a policy names it. The
organisation's ceiling can then cap how open a team chooses to be.

### Phase 3: across Crews

Each person's Bots on their own Crew, reached through the hub. It depends on
[rfc-conductor-remote-dispatch.md](rfc-conductor-remote-dispatch.md), which is a
draft, and is not started until that lands.

## Migration plan

| Phase | Scope | Exit criteria |
|---|---|---|
| 0 | `team-board` app, its MCP server, Team Bot and role Bot bundles, published in a team registry | A task cannot be saved without an owner; an `ask_bot` to a Bot whose switch is off returns a refusal and writes no request; turning the switch off drops an unanswered request; every request and answer has an audit row; a meetings action item becomes a task only after a person confirms it |
| 1a | Enable `ctx.crewmate` | An app with `proposeCrewmate` raises a card; the owner applies it; an app without the permission cannot |
| 1b | Acting person to app handlers | The id equals the `derive_principal` result for the same request, and a forged header or body field is ignored |
| 1c | Required approver on a change card | A card naming person A is refused for person B and confirmed by A; a card naming nobody behaves as today |
| 2 | Governance control for Bot-to-Bot openness | With no policy, behaviour is unchanged; a ceiling that forbids requests blocks `ask_bot` even for an open team |
| 3 | Cross-Crew | Blocked on remote dispatch |

Phase 0 is blocked on nothing. Phases 1b and 1c are blocked on a maintainer's view
of Open questions 2 and 3.

## Backward compatibility

Compatible. Phase 0 adds an app and changes no core behaviour. Phases 1 and 2 are
additive: a missing permission, a card with no named approver and an unnamed
governance scope all behave as they do today. Removes nothing.

## Security considerations

- **Prompt injection.** A Bot that reads untrusted content may be induced to send
  a request. Requests arrive as data through a tool and are never run as a turn,
  which is the property `session_send` lacks. The receiving Bot still decides what
  to answer, so an answer is a disclosure channel; that is why the switch is off by
  default and why every exchange is audited.
- **Open teams.** A team that sets Bot-to-Bot requests fully open accepts that a
  compromised Bot can ask any other. The per-Bot switch only stops later requests.
  Whether that, plus the audit record, is an adequate floor is Open question 3.
- **Identity.** The owner of a task, and the sender of a request, are taken from
  the server-derived principal, never from a body, header, tool argument or agent
  text ([rfc-per-person-execution.md](rfc-per-person-execution.md), requirement R2).
  The gateway already strips any `X-KiroCrew-Principal` a caller sends, so extending
  that claim keeps the same property; an app-supplied owner id would not.
- **Spend.** `permissions.spawn` has no per-app rate limit
  (`docs/app-kit/manifest-reference.md`), so the app budgets any Bot work it
  triggers.
- **Registries.** A team registry is cloned credential-free unless the operator
  grants it trust.

## Alternatives considered

1. **Loosen `session_send` for all sessions** (#23807). It widens the
   prompt-injection surface for every user for the sake of one use case.
2. **A member inbox subsystem.** Declined on 2026-09-13 after the design grew to
   thousands of lines; not revived here. The app's request records are the smallest
   thing that gives a request an owner, a state and an audit row.
3. **An owner field on the existing drawer boards.** The pipeline board's contract
   forbids it, and those boards are agent-written panels, not a record.
4. **A Team Bot in core.** Tenet 8 says a surface that cannot be built as an app is
   a platform defect to fix, not a reason to build it in core.
5. **Wait for `people_policy.json` and run-as before anything.** Phase 0 does not
   depend on roles; it stores a principal id the platform already issues.

## Open questions

1. Is an app the right shape for the Team Bot, with core changes limited to Phase 1?
2. Should the acting person reach an app through a new kind on the existing signed
   principal claim (`X-KiroCrew-Principal`, today `owner-session` or `none`), or is
   there a reason the claim should stay owner-only? The claim already binds to one
   method, target and body and is HMAC'd with the app secret, so it looks like the
   right carrier rather than a new surface.
3. For a fully open team, are the per-Bot switch and the audit record a sufficient
   safety floor, given why `session_send` is withheld?
4. May a change card name a required approver, or should task-owner confirmation
   live inside the app?
5. An app's MCP tools reach a Bot's worker, not its conductor chat. Is that the
   right place for `pending_requests` and `answer`, given that a Bot only reads
   its requests when a worker runs? The pilot should measure how long a request
   waits for an idle Bot and whether a cron-driven check is needed.
6. A shared Crew host with per-person execution, or one Crew per person joined
   through the hub, as the first target?
