---
title: Slack channel roles — read-only teammates, per-channel maintainers, owner
approvals
status: draft
author: mvn-bachhuynh-dn
created: 2026-10-23
last-audited: 2026-10-24
audited-at: 9ae6bc7124
revision: 2
doc-pr: 23362
implementation-prs: []
tracking-issues: [22759]
supersedes: []
superseded-by: []
---

# RFC: Slack channel roles — read-only teammates, per-channel maintainers, owner
approvals

- Status: draft. Nothing built. Acceptance requested from a maintainer; the
  status flips to `accepted` when one records it here. This document lands first
  because it reverses a deliberate default (Slack is owner-only), which the First
  Principles lane reads as a product-shape decision off the base branch.
- Author: mvn-bachhuynh-dn
- Created: 2026-10-23
- Tracking issue: [#22759](https://github.com/kirodotdev/KiroCrew/issues/22759)
- Measured at: `9ae6bc7124` (revision 2; first written at `7796e2af52`). Paths
  below are relative to `src/kiro_crew/`.
- Revision 2 (review of #23362): a role profile now intersects with the
  app/task/surface profile instead of replacing it, and role data lives only in a
  dedicated read-only leaf, never in `config.json`.

## Summary

Let a single-owner gateway admit a few named Slack users with narrow,
per-channel rights, enforced at Crew's own gate. A **teammate** may talk to the
bot and have it read the channel's workspace, nothing else. A **channel
maintainer** may additionally have the agent write inside that one channel's
workspace and run commands the owner approved, with the agent process confined
to that workspace by the OS. Anything outside a role is asked of the owner in
the Slack thread. The owner keeps every right they have today, and with no
roles configured nothing changes.

The roles are expressed as governance profiles selected by **who sent the turn
and in which channel**, applied **on top of** the profile the turn already gets,
so a role can only narrow: the effective permission is
`POLICY ∩ PROFILE ∩ ROLE`, and adding a role is data, not evaluator code.

## Motivation

### Current state

- Slack is owner-only by construction. `slack/handler_runtime/access.py`
  `is_allowed_user()` ("Multi-user access is disabled for security") returns
  `is_owner(user_id)` and never reads `_allowed_users`;
  the gateway's orchestrator init (`slack/gateway_runtime/orchestrator_init.py`)
  prunes `slack.allowed_users` to the owner at boot; `is_open_channel()` always
  returns False. The `allowed_users` and `open_channels` field metadata in
  `config/sections_slack.py` documents both keys as ignored.
- A non-owner's message is refused at the events layer:
  `_route_message` in `slack/events.py` gates on `is_allowed_user`, logs and
  audits a deny, and posts an ephemeral "not authorized". It is never answered.
- The tool gate does not know who asked. `hooks.on_tool_call` takes
  the session, agent and tool, not the requester; `slack/renderer.py`
  `SlackApprovalDecider` is keyed by session and request only. The approval
  mode is global (`_resolve_approval_mode` in `slack/events_queue.py`).
- Governance cannot select a profile per user or per channel.
  `Bind.type` (`platform/governance_parts/carriers.py`) allows
  `surface | app | task`, and `platform/governance_profiles.py`
  `resolve_active_scope(session_key, agent, app, task)` picks app, then task,
  then the surface inferred from the session key. Every Slack session shares the
  `slack` surface profile or its agent's task profile.
- A channel already maps to a project: `slack/channel_crew.py`
  `resolve_channel_crew` turns `slack.channels.<id>.agent` into the crew's kiro
  agent and workspace cwd, on both chat paths (`slack/transport_dispatch.py`,
  `slack/handler_runtime/turn.py`). That cwd is a starting directory,
  not a boundary.
- The sandbox masks a deny-list (the tiers in the `sandbox.py` module docstring);
  `sandbox_wrap.py` `wrap_argv` has no "only this directory is writable" mode,
  and `agent.sandbox`
  is global (`agent.sandbox` in `config/sections_agent.py`).
- "Member" already means a crew persona with its own memory and workspace
  (the `members.py` and `member_workspace.py` module docstrings), not a Slack
  user. This document
  therefore says **teammate** for a human admitted on Slack.

### Problem

A team that runs one Kiro Crew per project, one Slack channel per project, has
two settings: only the owner, or (by patching the gate) any allow-listed user
with the owner's power. In practice a teammate needs to ask the bot about the
project without being able to change it; the person responsible for one project
needs the bot to edit and build that project without the owner approving every
call; and neither may reach another channel's workspace, the owner's secrets or
owner-only controls.

### Evidence from running it

The author has run this model as local patches on a 0.9.0 gateway with real
teammates (the read-only allow-list for several weeks; maintainers, open
channels and confinement for a few days). The patches are public for reference,
not as a proposed diff:
<https://github.com/mvn-bachhuynh-dn/slack-channels-onboard/tree/main/gateway-patches>.
Each finding below is a requirement on any upstream design:

1. Owner-only DMs must be enforced in the events layer, before the
   native/transport split, or a teammate's DM reaches one of the two paths.
2. A read-only gate on the legacy handler path alone leaves the transport path,
   which messages actually take by default, open.
3. A teammate can act with owner authority without any tool: approval and
   OPTIONS buttons, a linked dashboard slot (`maybe_route_linked_thread` in
   `slack/handler_runtime/inbound.py` runs a routed turn as a dashboard turn
   with the owner's approvals), and
   `!`/slash commands.
4. Read scoping must hold for relative paths and pattern-only searches when the
   session cwd is not the workspace, and for a harness that self-approves reads
   (the Claude backend allows Read/Grep/Glob in its cwd without asking the gate).
5. Command matching must see through wrappers (`rtk <cmd>`, `env`, `timeout`),
   and a refused request must not trigger a storm of approval cards.

## Goals

1. With no roles configured, behaviour is byte-identical to today: owner-only.
2. Teammates are named Slack users, verified against Slack (`users.info`: not a
   bot, not deactivated, not a guest, same workspace or org), stored in a file
   the agent can read but never write.
3. A teammate's turn runs under a teammate profile: read-only, scoped to the
   channel's workspace, no secrets, no spawn, cron, send, memory writes, DMs,
   buttons, linked slots or commands.
4. A maintainer's turn in their channel runs under a maintainer profile: write
   and owner-approved commands inside that channel's workspace only. In any other
   channel a maintainer is a teammate.
5. A request outside the requester's role becomes an owner approval in the
   Slack thread, naming the requester; "always" stores a rule the owner can see
   and remove.
6. A maintainer channel's agent process is confined by the OS to its workspace
   before the maintainer fast path is allowed; without confinement the owner
   approves as today.
7. Every decision is SEL-audited with the requester's id.

## Non-goals

- Hosted multi-tenant deployments, SSO, or an identity provider (#12673's
  scope). Identity here is the Slack user id the event carries.
- Per-teammate memory isolation beyond what a channel's crew record already
  gives (open question 2).
- Roles on Discord, Telegram or other channels. The model should not preclude
  them; this document only specifies Slack.
- Changing what the owner can do, or the approval UX for the owner's own turns.
- Governing computer use (it stays an operator opt-in, per AGENTS.md).

## Design

### Identity and roles

- **Owner**: unchanged (`CRED_OWNER_ID`, read in
`slack/gateway_runtime/orchestrator_init.py`).
- **Teammate**: a Slack user id listed in the role file below. Admission checks
  `users.info` at write time and caches the verdict; a teammate who becomes
  deactivated or a guest is refused at the next turn.
- **Open channel**: a per-channel `open: true` in the role file admits every
  human member of that channel as a teammate *in that channel only* (guests and
  Slack Connect users from another workspace refused).
- **Maintainer**: per channel, `maintainers: [U…]` in the role file; must be a
  teammate first.

**Where role data lives.** One dedicated file under the data home (working
name `slack_roles.json`), never `config.json`. It is fenced on every write path
an agent has:

- the sandbox: the leaf joins `_CREW_READONLY_LEAVES` (`sandbox_leaves.py`), so
  in-sandbox code can read it and a write is a kernel denial in every sandbox
  mode, and the union pin in `test_sandbox_governance_mask.py` covers it;
- the tool gate: `is_sensitive_write_path` (`security/paths.py`) refuses the
  file-edit tool on it, for the case the sandbox does not cover;
- `agent.sandbox: off`: roles are not admitted at all. With no sandbox a spawned
  shell's `open(..., "w")` reaches any file, so the gateway refuses to load a
  non-empty role file in that mode and logs why; Slack stays owner-only.

Its only writers are owner-only gateway surfaces (a dashboard route, a Slack
interaction, the operator's own `kirocrew` CLI), all outside the sandbox, each
audited. The existing `slack.allowed_users` and `slack.open_channels` keys stay
ignored, as they are today.

### Turn principal

The events layer already knows the sender (`_route_message` in `slack/events.py`).
It resolves a
**turn principal** `(user_id, role, channel_id)` once and carries it with the
turn to every place that authorizes: the tool gate, button handlers, linked-slot
routing and command dispatch. `hooks.on_tool_call` gains an optional
`principal` argument; absent means "owner, as today", so every non-Slack caller
is unchanged.

### Roles as governance profiles

Add one bind type, `principal`, to `Bind.type`
(`platform/governance_parts/carriers.py`), with selectors
`role: teammate | maintainer` and an optional `channel`.

The role profile **does not replace** the profile the turn already gets.
`resolve_active_scope` (`platform/governance_profiles.py`) keeps choosing the
app, task or surface profile exactly as today; for a non-owner principal the gate
then also resolves the role profile, and an item is permitted only if both
permit it. The evaluator (`resolve` in `platform/governance_parts/evaluator.py`)
is reused unchanged, called once per profile, and the decision is the tighter
of the two:

    effective = POLICY ∩ active profile (app | task | surface) ∩ role profile

So a teammate's or maintainer's turn can never be granted something the owner's
own turn in the same app, task or surface context would be refused. The role
profile is a narrowing on top, never a substitute. Shipped default profiles:

- `teammate`: `filesystem.write` none; `folders.read` = the channel workspace;
  `tools` read-only kinds; `capabilities.spawn`, `cron`, `messaging`,
  `memory_writes` denied; `commands` none.
- `maintainer`: `teammate` plus `folders.write` = the channel workspace and
  `commands` = the channel's approved set.

The workspace is the one `resolve_channel_crew` already resolves, so a channel
with no crew binding has no workspace and a maintainer there is a teammate.

### Owner-only surfaces

Independently of profiles, the principal is checked where today's gate is
`is_allowed_user` and its callers: DMs, approval and OPTIONS buttons (the
interaction dispatch in `slack/interactions.py`), linked-slot routing
(`maybe_route_linked_thread`), `!` and slash commands, the home
tab, `!stop` for another person's turn. These stay owner-only; a teammate gets
the existing "not authorized" reply.

### Owner approvals in the thread

When a teammate's or maintainer's tool call falls outside their profile, the
gate does not deny outright: it posts an approval card to the owner in the same
thread, naming the requester and the exact call, with *Allow once*, *Allow for
this turn*, *Always allow* and *Deny*. Only the owner can click (the
`is_allowed_user` check before `SlackApprovalDecider.resolve_global`). *Always
allow* writes a rule into the channel's
role data (commands or programs), shown and removable wherever roles are edited.
*Deny*, or a card left to expire, ends asking for the rest of that turn so the
thread does not fill with cards. Some requests are never approvable
(shells, interpreters, package installs, network tools); the list is data.

### Confinement

The maintainer fast path (auto-approving in-scope writes and commands) is
allowed only when the session's agent process is verifiably confined to the
channel workspace: read-only root, workspace and a scratch dir writable, the
data home and other workspaces absent, container and session-bus sockets
masked. Today's sandbox is a deny-list mask (`sandbox.py`), so this needs a new
allow-list tier selectable per session (open question 1). Without it, a
maintainer's in-scope requests still go to the owner, which is safe and only
slower.

### Harness differences

A harness that approves some calls itself (the Claude backend's in-cwd reads)
must be told to ask, or must be confined so that what it can reach is already
in scope. Each harness declares whether Crew sees every call; a role is only
offered on a harness that does, or that runs confined.

## Migration plan

Each phase is shippable and abandonable on its own. "Owner-only unchanged" is
an exit criterion of every phase.

1. **Principal plumbing.** Resolve the turn principal in the events layer and
   carry it to the tool gate, buttons, linked slots and commands; SEL records
   it. No behaviour change.
   Exit: every SEL entry for a Slack turn carries the sender; with roles unset,
   the full Slack test suite is unchanged.
2. **Teammates, read-only.** The read-only role file with `users.info`
   admission; `principal` bind type; shipped `teammate` profile; owner-only
   surfaces enforced on both paths; roles refused when `agent.sandbox` is `off`.
   Exit: for every scope in `SCOPE_CATALOG`, a test proves a teammate turn is
   never permitted an item the owner's turn in the same app/task/surface context
   is refused (the role intersects, never replaces); tests prove a teammate can
   read the channel workspace and nothing outside it (relative paths, searches
   without a path, a cwd that is not the workspace), cannot write, run a shell,
   spawn, cron, DM, click a button, route
   into a linked slot or run a command; a teammate DM is dropped before the
   native/transport split.
   A write to the role file from inside the sandbox fails in every sandbox mode,
   the file-edit tool refuses it, and a non-empty role file with
   `agent.sandbox: off` leaves Slack owner-only.

3. **Open channels.** The per-channel `open` flag in the role file, with the
   guest and Slack Connect exclusions.
   Exit: a non-listed human member gets teammate rights in that channel only;
   a guest, a bot, and a user from another workspace are refused.
4. **Owner approvals in the thread.** Out-of-profile calls become owner cards;
   *Always allow* stores a rule; refusal ends asking for the turn.
   Exit: wrapped commands (`rtk`, `env`, `timeout`) are matched by what they run;
   a never-approvable request posts no card; one refusal produces no further
   cards in that turn.
5. **Maintainers.** Per-channel maintainers and the `maintainer` profile, with
   writes and approved commands still owner-approved (no fast path).
   Exit: a maintainer can have the agent edit files in their channel's workspace
   after owner approval and is a teammate in every other channel.
6. **Confinement and the fast path.** Allow-list confinement tier; the fast path
   only for a verified-confined session. Blocked on open question 1.
   Exit: from a confined maintainer session, the data home, another workspace,
   `~/.ssh`, the container socket and `sudo` are unreachable, and in-scope writes
   need no approval.

## Backward compatibility

Compatible: with no role file (or an empty one), every code path behaves as
today; the new `principal` argument defaults to the owner. Roles live in a new
file that only an owner action creates, so an existing `config.json` that still
lists non-owner ids in `slack.allowed_users` from before the owner-only change
admits nobody: those keys stay ignored and need no migration.

## Security considerations

- **Identity source.** The Slack event's `user` is the only identity. An event's
  `team` is the author's workspace, never the channel's (the same rule
  `RealSlackClient.ensure_channel_team` follows).
- **The agent must not raise its own ceiling.** Role data is a read-only leaf
  (kernel-denied write in every sandbox mode, refused by the file-edit tool), and
  roles are not admitted with the sandbox off. A role only intersects with the
  profile the turn already has, so even a wrong role entry cannot grant more
  than the owner's own turn in that context.
- **Prompt injection by a teammate.** A teammate can steer the agent with text.
  The profile, not the prompt, bounds what the agent can do on their turn; that
  is why reads are scoped to the channel workspace and secrets are masked.
- **Memory.** A channel's crew record decides the memory store. A teammate's
  turn can read what the agent remembers in that store; open question 2.
- **Shared sessions.** A thread is one session. If the owner and a teammate both
  write in a thread, each turn is authorized by its own sender, and a linked
  dashboard slot stays owner-only.
- **Audit.** Every admit, deny, approval and stored rule is an SEL event naming
  the requester and, for approvals, the owner.

## Alternatives considered

- **Honour the allow-list only.** Every listed user gets the owner's power.
  Rejected: this is the risk the owner-only change removed.
- **One gateway per person.** No shared channel context or project memory, and
  N copies of credentials. Rejected for small teams.
- **Rely on each harness's own permission prompts.** Harnesses differ (the
  Claude backend self-approves reads in its cwd), and their prompts know nothing
  about Slack users. Rejected as the enforcement point; kept as defence in depth.
- **Model teammates as crew members.** A crew member is an agent persona with
  its own memory, not a human principal; overloading it would mix two concepts.

## Open questions

1. Confinement: a new allow-list sandbox tier in `sandbox.py`/`sandbox_wrap.py`,
   or a separate wrapper (bwrap on Linux, Seatbelt profile on macOS) applied only
   to maintainer sessions. What would the sandbox owners accept?
2. Memory: should a teammate's turn use a channel-scoped memory store, or is the
   channel's crew record (often a private store per project) enough?
3. Should the owner-approval cards (phase 4) also serve the owner's own turns,
   replacing the current per-tool approval card, or stay a separate flow?
