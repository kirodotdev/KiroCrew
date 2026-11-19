---
title: A cron job created from a chat can run as a turn in that chat instead of in its own session
status: draft
author: kidbrax, with a kirocrew agent session
created: 2026-11-19
last-audited: 2026-11-19
audited-at: 5c7dcb8111
doc-pr: null
implementation-prs: []
tracking-issues: [35874]
supersedes: []
superseded-by: []
---

# RFC: `deliver_to` for cron jobs: run in the originating chat, or in the job's own session

Status: draft. Nothing is built and this document changes no code. It answers
[#35874](https://github.com/kirodotdev/KiroCrew/issues/35874), which a
maintainer triaged as `tier:T3` and `parked`, and pointed at this process.
Acceptance is requested from a maintainer, who records it on this Status line.
Every code reference below was read at `5c7dcb8111`.

Related, and not changed by this RFC:

- [#22701](https://github.com/kirodotdev/KiroCrew/issues/22701): each run in a
  fresh tab. That is `new_chat_per_run`, which stays and is the opposite
  preference this RFC keeps working.
- [#29168](https://github.com/kirodotdev/KiroCrew/issues/29168): a cron
  sub-agent's completion starts a context-less session when the parent is gone.
  This RFC's fallback (§4.4) is the same shape and does not fix that case.

## 1. Summary

A cron job gets one new field, `deliver_to`, with two values:

- `own_session` (today's behavior, and the default): a run executes in the
  job's `cron:{id}` session and shows in its `cron-{id}` tab.
- `origin`: a run is delivered as a turn in the chat session that created the
  job, so the agent answers with that thread's whole history in front of it.

If the origin chat is gone, the run falls back to `own_session` and its first
line names the thread it came from.

## 2. Problem

A reminder or check created in a chat fires later in a new session that holds
none of the thread's context. By then the person has forgotten what it was
about, and so has the agent answering. They have to find the original thread
and rebuild the details by hand.

`send_message(session="origin")` covers part of this, but only when the job's
prompt remembers to call it. The run itself still happens in a session with no
history from the thread, so the agent that decides what to say does not know
what the thread was doing.

## 3. What is on main

- A job already records its originating chat. `CronJob.session_key`
  (`src/kiro_crew/cron_service/model.py`) is "session that created this job",
  and it is the one delivery target every consumer reads.
  `adopt_job` (`cron_service/service/job_ownership.py`) sets it, and it is
  released to `""` for jobs created outside a chat.
- Delivery into a chat session already exists and already handles a busy
  session. `api_send_message` (`dashboard/handlers/messaging.py`) with
  `session="origin"` resolves the slot, rehydrates it from disk when it is not
  loaded, and calls `session_deliver.deliver(state, slot, wrapped,
  session_deliver.cron(label), evict_oldest=True)`. An idle session starts a
  turn; a running one gets a queued card (`queue_cron_notification` in
  `dashboard/cron_inject.py`), so a run never interrupts a turn in progress.
  Its outcomes are `QUEUE_FULL` and `NOT_DELIVERED`, and the caller falls back.
- A script cron's result already goes to the origin session the same way. Only
  an agent job's own run (`_cron_capture` in
  `slack/gateway_runtime/cron_fire.py`) always opens `cron:{id}`.
- Fields that shape the job's own session: `persistent_session`,
  `new_chat_per_run`, `hide_in_chat`, `minimal_context`, `on_tab_close`,
  `chat_folder_id`, `agent_id`, `model`, `memory_store`, `approval_mode`, and
  the project directory. The store already refuses incompatible pairs, for
  example `new_chat_per_run` with `persistent_session`.

## 4. Proposal

### 4.1 The field

`CronJob.deliver_to: str = "own_session"`, one of `own_session` or `origin`.
It is settable from `cron_add` and `cron_update` (MCP), the CLI, and the
dashboard Schedule page. A record written before the field existed reads as
`own_session`, so no migration is needed.

`origin` is accepted only when `session_key` names a dashboard chat session
and the job is an agent job (a script job has no turn to place). Otherwise the
store refuses it with a message that says why, as it does for the other
incompatible pairs.

### 4.2 What a fire does in `origin` mode

At fire time, before any LLM client is acquired:

1. Resolve the origin slot the way `api_send_message` does: `get_slot`, then
   rehydrate from disk when not loaded.
2. Wrap the job's prompt as the existing cron notification
   (`CRON_NOTIFY_PREFIX`) and call `session_deliver.deliver` with
   `session_deliver.cron(label)`. Idle: a turn starts in the origin session.
   Running: the prompt queues behind the current turn and a card shows it.
3. Record the run as delivered. The run's outcome is the delivery, not the
   turn's result: the turn is the origin session's own and is read there.

No `cron:{id}` slot, session or tab is created for the run, and no separate
LLM client is acquired. The turn runs on the origin session's agent, model and
working directory.

### 4.3 Fields that do not apply

In `origin` mode the run is a turn of someone else's session, so the job's own
session settings have nothing to act on. The store refuses `origin` together
with `persistent_session=False`, `new_chat_per_run`, `on_tab_close=fresh`,
`chat_folder_id`, `minimal_context`, a job `agent_id`, `model`, `memory_store`
or project directory that differs from the origin session's, and
`approval_mode` set on the job. `hide_in_chat` is accepted and ignored.
Refusing beats ignoring silently: a job that asked for `auto` approval must
not quietly run under the thread's approval mode.

### 4.4 Fallback

When the origin slot cannot take the run (closed, deleted, never persisted:
`NOT_DELIVERED`, or `QUEUE_FULL` with nothing to evict), the run executes as it
does today in `own_session`, and the first line of its prompt names the thread
it came from by title and session key. The job keeps `deliver_to=origin`, so a
later fire tries the thread again.

### 4.5 Overlap

A job whose previous delivery is still queued behind a running turn does not
queue a second copy. The newer fire replaces the queued one, so a thread that
is busy for an hour does not wake to twelve identical pollers.

## 5. Default and who chooses

`own_session` is the default and this RFC does not propose changing it, so
nothing changes for existing jobs and noisy pollers stay isolated: every poll
that lands in a thread adds to that thread's context.

The choice is the person's, per job. It is set when the job is created or
changed later, from `cron_add` / `cron_update` (an agent asks the person which
they want when the job's purpose is a reminder about the thread), the CLI, and
the Schedule page. There is no global setting that flips it: a job's mode is
visible on the job, and one key in the store is enough.

## 6. Risks

- **Context growth.** Each run adds to the origin session's history. A frequent
  job in `origin` mode fills the thread. Mitigation: `own_session` stays the
  default, and the cron-creation docs say which jobs belong where
  (`cron-cost-optimize` skill included).
- **Approval mode.** The turn runs under the origin session's approval mode,
  and unattended it may wait on an approval card nobody is watching.
  Mitigation: §4.3 refuses a job `approval_mode` rather than overriding the
  thread's; the run card says it is waiting on approval like any queued turn.
- **Authority.** A job written in one turn executes later with the thread's
  tools and grants. The same person owns both, and `session_deliver` is the
  path `send_message(session="origin")` already uses, so this adds no new
  grant. A job in `origin` mode inherits whatever the thread is allowed at
  fire time, not at creation time.
- **Failure accounting.** Auto-pause counts consecutive failed runs. In
  `origin` mode a run is delivered or not, so only a failed delivery counts;
  a turn that errors in the thread does not.

## 7. Alternatives

- **Keep today's session and inject a summary of the thread.** Smaller, but the
  agent still lacks the history and the summary is another thing to go stale.
- **Make `origin` the default.** Matches the issue's ideal, but changes every
  existing chat-created cron and fills threads with pollers. Left out on
  purpose; the person picks per job.
- **Only document `send_message(session="origin")`.** Leaves the run itself
  context-less, which is the reported problem.

## 8. Plan

1. Field, store validation, MCP/CLI/dashboard setters, and the refusals in §4.3.
2. The `origin` branch of the fire path (§4.2, §4.4, §4.5), reusing
   `session_deliver`.
3. Schedule page control and the cron skill/doc text.

Tests: store validation for each refused pair; fire into an idle origin slot
(a turn starts, no `cron:{id}` slot); fire into a running slot (queued card, a
second fire replaces the first); origin slot gone (falls back, first line names
the thread); a record without the field reads as `own_session`.

## 9. Not decided here

- Whether to key the fallback on `QUEUE_FULL` or retry once first.
- Whether `cron_update` may change `deliver_to` on a job whose origin is gone.
