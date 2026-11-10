---
title: Shared Slack access - the owner opts in to let named Slack users drive the Crew as the owner
status: in-progress
author: kyleseaman
created: 2026-11-05
last-audited: 2026-11-05
audited-at: 3adab5e0c4
doc-pr: null
implementation-prs: []
tracking-issues: [22759, 30678]
supersedes: []
superseded-by: []
---

# RFC: Shared Slack access

## Decision

On 2026-11-05 the repository owner (Kyle Seaman) decided that the Slack bot may be
shared with named Slack users, as a plain owner opt-in with no Shared Crew
dependency:

- `slack.shared_access.enabled` (default `false`) and `slack.shared_access.user_ids`
  (default `[]`). Off means Slack answers the owner alone, exactly as before.
- A shared user acts **as the owner**: the owner's files, Git and cloud credentials
  and connected tools, the owner's other conversations and saved memory, and what
  Kiro remembers. The settings page says so before the switch can be turned on,
  keeps saying it while it is on, and turning it on needs an explicit confirm.
- `slack.allowed_users`, `!allowlist` and `/kirocrew @user` stay inert. Turning the
  switch on never admits a stale allowlist entry.

## Why not Shared Crew

A Shared Crew collaborator was the first design. Shared Crew's containment is
resource limits only: every runtime runs as the gateway's OS account (see
[shared-crew](../system-specs/modules/shared-crew.md) §4 Containment,
`adversarial_escape_closed: false`), and per-person execution is not built
([rfc-per-person-execution](rfc-per-person-execution.md)). Routing a Slack
collaborator through it would promise a separation that does not exist. The owner
chose an honest, explicit grant instead. When per-person execution lands, a
shared user can be bound to their own account and this grant narrowed.

## Who counts

An ID counts only once Slack confirms it with `users.info`: same workspace as the
bot (`auth.test`), a person (not a bot or app user), not deactivated, not the owner.
The dashboard refuses an ID that fails. An ID added with `kirocrew config` is
checked by the gateway before its first event counts. A confirmation is kept 10
minutes, a refusal 1 minute. The list and the switch are read on every event, so a
removal or switch-off stops the person's next message. A message already admitted
still meets every owner-only refusal below: the gateway keeps treating anyone it
admitted as a shared user that way until it restarts.

## The owner-only line (a judgment call)

Shared users drive work: DMs, mentions, threads, `!stop`, `!title`, `!compact`,
question cards, answers to the tool prompts **their own** turns post, and
the review-mode draft of their own turn. These stay the owner's:

| Control | Why |
|---|---|
| The switch and the list (dashboard, loopback, never an app token or internal caller) | Only the owner grants access |
| Trust, YOLO, approval-mode changes; answering another person's prompt | Each widens what runs without the owner |
| `!dashboard` and other dashboard links | A link is full dashboard access |
| Every other `!` command (`!channel`, `!agent`, `!ta`, ...), `/kirocrew` | Channel activation, agent overrides and settings |
| A message starting with `/`; `!temporary` / `!incognito` | The agent harness's own commands (`/agent`, `/model`, `/tools trust-all`) and keeping a turn out of the owner's record |
| The Home tab, the `sessions` list, and the `spawn list` and `status` keywords | They list every session or subagent, or count the host's |
| `run` and natural-language cron keywords | Scheduling and task runs |
| Code channels, including answering a card in one | Kept owner-only for v1 |
| Every other button | Settings, sessions, resume, links and the allowlist |

The agent itself is not narrowed: a tool it runs in a shared user's turn is
governed by the same approvals the owner's turns are, and the shared user answers
their own turn's prompts. So the Crew tools the default agent runs without asking
reach the owner's data for them too:

| Reachable through the agent | Effect |
|---|---|
| `list_sessions`, `search_chat_history`, `get_chat_session` (`all_workspaces`) | Read the owner's other conversations |
| `memory_recall`, `learn_add` / `learn_remove`, memory writes | Read saved memory; a lesson they add reaches the owner's later sessions |
| `artifact_*`, `workflow_run`, `spawn_run`, cron tools | Read and change artifacts, run work, manage schedules |
| Shell and file tools, with the sandbox off | Edit `config.json`, this list included (the sandbox seals it read-only) |

`is_allowed_user` stays the owner check it is on main. A shared user is admitted
only where a site asks `is_shared_user` on purpose: message routing, their own tool
prompts and review drafts, question cards, Stop, `!title` / `!compact`, the queued
re-check and linked threads. Every other reader of `is_allowed_user` refuses them by
default, including the Hooks page scripts (the operator's own commands, run for the
operator's DMs only), the agent's `send_message` to a Slack user, the Slack profile
read and the inert `!allowlist` prompt.

The warning says this in plain words (decided 2026-11-05, option A). Refusing
these tools when a turn's requester is a shared user is the follow-up
[#30678](https://github.com/kirodotdev/KiroCrew/issues/30678): it needs the
requester carried into the tool gate. The owner can widen or narrow the table
above later; it is recorded here so the First Principles lane has the decision
to read.

## Audit

Every admission and refusal is a security event log row with the Slack user ID and
no message text (`slack.message` with `resources=shared_access` on an admission;
the existing denial rows otherwise). Switch and list writes are
`slack.shared_access.switch|add|remove`.
