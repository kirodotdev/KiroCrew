---
title: Shared Slack access - the owner opts in to let named Slack users drive the Crew as the owner
status: partial
author: kyleseaman
created: 2026-11-05
last-audited: 2026-11-20
audited-at: 4158830b79
doc-pr: 30760
implementation-prs: [30760]
tracking-issues: [22759, 30678]
supersedes: []
superseded-by: []
---

# RFC: Shared Slack access

Status: partial. The 2026-11-05 decision is implemented in [#30760](https://github.com/kirodotdev/KiroCrew/pull/30760) (`slack.shared_access`, built in `config/section_builders.py`). The 2026-11-20 amendment, "A shared user's own code channel" below, is not built: on main `4158830b79` `_route_message` in `slack/events.py` skips `code_channel_dispatch` for a shared sender, who is refused in every code channel. Its implementation is open in [#40181](https://github.com/kirodotdev/KiroCrew/pull/40181).

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
chose an honest, explicit grant instead. When per-person execution lands, this
grant can end, as described under R10 below.

## Relation to rfc-per-person-execution R10

[rfc-per-person-execution](rfc-per-person-execution.md) R10 says work for an
unlinked channel user never runs as the owner. Shared Slack access is a
deliberate, owner-made exception to R10 for one case only: a person the owner
names in `slack.shared_access.user_ids`, after Slack confirms them, with the
switch on. Their turns run as the owner, including a turn already running when
the person is removed or the switch goes off (only their next message is
stopped). Turning the switch on from the settings page shows the warning and
needs a confirm; a hand edit of `config.json` can also set it.

R10 still governs everyone else. Anyone not on the list is refused exactly as
before, and the switch is off by default. When per-person execution is built,
this exception can end: a listed person would run as their own linked account,
or, while unlinked, as the guest account or not at all (R10).

Until then, refusing the owner-data tools in a shared user's turn is the
follow-up [#30678](https://github.com/kirodotdev/KiroCrew/issues/30678).

## Who counts

An ID counts only once Slack confirms it with `users.info`: same workspace as the
bot (`auth.test`), a person (not a bot or app user), not deactivated, not the owner.
The dashboard refuses an ID that fails. An ID added with `kirocrew config` is
checked by the gateway before its first event counts. A confirmation is kept 10
minutes, a refusal 1 minute. The list and the switch are read on every event, so a
removal or switch-off stops the person's next message. A message already admitted
still meets every owner-only refusal below: the gateway keeps treating anyone it
admitted as a shared user that way until it restarts. That memory is in the process
only: after a restart, a removed person is refused because the list no longer names
them.

## The owner-only line (a judgment call)

Shared users drive work: DMs, mentions, threads, `!stop`, `!title`, `!compact`,
question cards, answers to the tool prompts **their own** turns post,
the review-mode draft of their own turn, and code channels **they opened**
(amended 2026-11-20, "A shared user's own code channel" below). These stay the
owner's:

| Control | Why |
|---|---|
| The switch and the list (dashboard, loopback, never an app token or internal caller) | Only the owner grants access |
| Trust, YOLO, approval-mode changes; answering another person's prompt, except Approve / Reject in a code channel the shared user opened (below) | Each widens what runs without the owner |
| `!dashboard` and other dashboard links | A link is full dashboard access |
| Every other `!` command (`!channel`, `!agent`, `!ta`, ...), `/kirocrew` | Channel activation, agent overrides and settings |
| A message starting with `/`; `!temporary` / `!incognito` | The agent harness's own commands (`/agent`, `/model`, `/tools trust-all`) and keeping a turn out of the owner's record |
| The Home tab, the `sessions` list, and the `spawn list` and `status` keywords | They list every session or subagent, or count the host's |
| `run` and natural-language cron keywords | Scheduling and task runs |
| Code channels the owner opened, including answering a card in one; in a code channel a shared user opened, Trust this channel, the approval-mode and agent pickers, the helper-agent Turn-on, the channel's slash commands and Archive | A code channel is a persistent session in the owner's repositories, and these controls change what it runs without the owner (amended 2026-11-20, below) |
| Every other button | Settings, sessions, resume, links and the allowlist |

The agent itself is not narrowed: a tool it runs in a shared user's turn is
governed by the same approvals the owner's turns are, and the shared user answers
their own turn's prompts. So the Crew tools the default agent runs without asking
reach the owner's data for them too:

| Reachable through the agent | Effect |
|---|---|
| `list_sessions`, `search_chat_history`, `get_chat_session` (`all_workspaces`) | Read the owner's other conversations |
| `memory_recall`, `learn_add` / `learn_remove`, memory writes | Read saved memory; a lesson they add reaches the owner's later sessions |
| `artifact_*`, `workflow_run`, `spawn_run`, `task_run`, cron tools | Read and change artifacts, run work, manage schedules; a schedule set up in their turn can keep running as the owner after they are removed |
| Shell and file tools, with the sandbox off | Edit `config.json`, this list included (the sandbox seals it read-only) |

`is_allowed_user` stays the owner check it is on main. A shared user is admitted
only where a site asks `is_shared_user` on purpose: message routing, their own tool
prompts and review drafts, question cards, Stop, `!title` / `!compact`, the queued
re-check, linked threads and, from the 2026-11-20 amendment, the code-channel
checks for a channel they opened. Every other reader of `is_allowed_user` refuses them by
default, including the Hooks page scripts (the operator's own commands, run for the
operator's DMs only), the agent's `send_message` to a Slack user, the Slack profile
read and the inert `!allowlist` prompt.

The warning says this in plain words (decided 2026-11-05, option A). Refusing
these tools when a turn's requester is a shared user is the follow-up
[#30678](https://github.com/kirodotdev/KiroCrew/issues/30678): it needs the
requester carried into the tool gate. The owner can widen or narrow the table
above later.

## A shared user's own code channel (amended 2026-11-20)

**Decision.** On 2026-11-20 the repository owner, who wrote this RFC, decided
that a shared user may open code channels of their own and work in them, as part
of adding people through shared access. The 2026-11-05 table kept code channels
owner-only "for v1"; this amendment replaces that row. It takes effect when this
amendment merges; the code is [#40181](https://github.com/kirodotdev/KiroCrew/pull/40181).
There is no new switch: with `slack.shared_access` on, it applies to every
confirmed shared user.

**Who may open one.** A confirmed shared user ("Who counts"), by the same paths
as the owner: a message that mentions the bot, or an unmentioned reply in a
channel thread the bot already answers. An ordinary DM that does not mention the
bot is answered in place, as the owner's is. Their request goes through the same
classifier, the same policy refusals and the same idempotent open as the owner's
([rfc-slack-code-channels](rfc-slack-code-channels.md) §3, Routing). The channel
records them as its opener (`opener` on the channel's route and binding). A code
channel Slack's own UI opens records no opener, so it is the owner's.

**Who is admitted.** A code channel a shared user opened admits two people: the
owner and its opener. Being a member of the channel admits nobody. In that channel
the opener has what the owner's thread rules already give a shared user, applied to
the channel: their messages, question cards, Slack's Stop (and the context bar's
Stop, which runs the same check) and `!stop`, and Approve / Reject on the tool
prompts the channel's session posts there and on the cards of the helper agents it
started. The channel is one session, so the opener answers every prompt that
session posts there, including one from a turn the owner started in the opener's
channel. Every other shared user is refused there, and a shared user is refused in
every code channel the owner opened, as before.

**What stays the owner's**, in the opener's channel too: Trust on any card and
**Trust this channel**, the approval-mode and agent pickers (and
`/kirocrew approval`), the helper-agent Turn-on, the `!` commands the table above
keeps the owner's, a message starting with `/` (the channel's slash commands and
the agent harness's own), `!temporary` / `!incognito`, and Archive. Each of these changes what the channel
runs without the owner, or keeps work out of the owner's record. The context bar's
Create PR, the Workers tab's per-helper Stop, the restart
notice's Stop and Resume, and Plan-canvas comments reaching the agent also stay
the owner's: this amendment does not widen them.

**Helper agents.** The opener's turns start helper agents only when the owner's
own **Let code channels start helper agents** switch
(`slack.code_channel_dispatch`) is on. The opener cannot turn it on, and the
owner-only, workspace and approval-mode limits on helper agents do not change.

**Checked at every act.** Admission is asked of the person at each message,
button press and card answer (`is_shared_user`), so removing someone from the list
or turning the switch off cuts them off at their next act, in their own channel
too. A helper agent's card asks again just before the answer is applied. Admission
is also tied to the channel the act happens in: a thread elsewhere that is linked,
or relinked, to the channel's session stays refused, and so does a helper agent's
own session. A relinked session never carries the opener's admission to another
channel.

**Fails closed.** A route or binding written before the `opener` field existed,
or with an empty one, reads as the owner's channel. An older gateway
ignores the field, so it keeps refusing shared users in every code channel.

**Refusals people can see.** A shared user a code channel refuses is told why, in
an ephemeral message only they see: on every press of a button on one of the bot's
messages, and for messages (in the channel or in a thread linked to its session)
at most once per person per channel every 10 minutes, because members post many
messages that are not meant for the bot. The notice says that only the owner or
the person who asked for the channel can work in it. Slack's own Stop control is
not one of the bot's buttons: a refused press of it is audited and stops nothing,
with no notice. In a code channel Slack's own UI opened, a shared user is told this
owner-only rule instead of the allowlist text, since the allowlist does nothing
for them. A shared user's `/` command in their own channel is refused privately
and never reaches the session. Each refusal still writes its audit row.

**Security trade-off.** A shared user already acts as the owner in threads: the
owner's shell and files, Git, `gh`, cloud credentials, connected tools, runs and
schedules (the tables above). A code channel of their own therefore adds less power
than it seems. What it does add:

- a Slack channel created in the workspace under the bot's name, with the privacy
  of the conversation it was opened from;
- a persistent session and a git worktree on the owner's repositories;
- helper agents started on the owner's machine for the opener's request, only
  while the owner's helper-agent switch is on;
- the Code tab, which shows the owner's repository diff to every member of the
  channel;
- one classifier model call for each mention, or reply in a thread the bot
  answers, from that person.

The owner accepts these for people they listed. Everything that widens what runs
without the owner (Trust, the approval mode, the agent, helper-agent Turn-on)
stays the owner's, and so do the owner's own code channels.

**Existing installs and the Settings warning.** No existing operators depend on
the old owner-only warning. The Settings warning's owner-only line
(`slackSharedAccess.owner_only`: "Only you can change this list or switch, approve
tool prompts for a whole session, or use Slack Code channels.") must change in the
same change that admits the opener, so the page never shows the old boundary: it
says that people on the list can open and work in code channels of their own, and
that the owner's code channels and their settings stay the owner's. This is a
requirement of the implementation
([#40181](https://github.com/kirodotdev/KiroCrew/pull/40181)), not of this
amendment.

**Alternatives considered.**

- *A separate opt-in switch* (for example `slack.shared_access.code_channels`,
  default off). Rejected: turning shared access on is already the owner's explicit
  opt-in, behind the warning and a confirm, and the person can already do the same
  work in a thread. A second switch adds a setting, a Settings row, its copy in
  every language and the config contract for the same grant.
- *Keep code channels owner-only and only make the refusals visible.* Rejected:
  shared access is how the owner adds people, and code work for them would stay in
  threads.
- *Admit every shared user in every code channel.* Rejected: any shared user could
  drive the owner's own code channels, answer their cards and steer their helper
  agents.

## Audit

Every admission and refusal is a security event log row with the Slack user ID and
no message text (`slack.message` with `resources=shared_access` on an admission;
the existing denial rows otherwise). Switch and list writes are
`slack.shared_access.switch|add|remove`.

## Not a goal

- Removing the inert `slack.allowed_users`, `!allowlist` and `/kirocrew @user`
  surface, so that only one list of Slack users remains. That is a separate
  take-away change, with its own reader sweep.
