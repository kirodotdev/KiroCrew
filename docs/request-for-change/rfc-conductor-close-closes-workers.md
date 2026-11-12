---
title: Closing a conductor closes its workers
status: accepted
author: iamwhatever, with kirocrew
created: 2026-11-06
last-audited: 2026-11-06
audited-at: 4b5eee0c18
doc-pr: null
implementation-prs: [30877]
tracking-issues: [30800]
supersedes: []
superseded-by: []
---

# RFC: Closing a conductor closes its workers

- Status: accepted on the decision of the maintainer iamwhatever, 2026-11-06, who picked option A below. Nothing is built on main yet. This document lands first because the change moves what the person's close does by default, and the First Principles lane reads that decision off the base branch.
- Tracking issue: [#30800](https://github.com/kirodotdev/KiroCrew/issues/30800)
- Implementation: [#30877](https://github.com/kirodotdev/KiroCrew/pull/30877)

## Problem

When the person closes a conductor from the session list, the worker sessions it opened stay open. They are stuck: a message typed into one is refused with `this Bot's conductor is closed; reopen it to keep working` (`crew_root_closed`), and with `agent.session_manager_chain` off (the default) no session manages them. The person has to find and close each worker by hand.

## Current behavior

Verified at `4b5eee0c18`:

1. `DELETE /api/chat/slots/{slot}` (`api_chat_slot_delete` in `dashboard/chat_api/slot_lifecycle.py`) closes that one slot through `close_slot`.
2. `close_slot` then runs `hand_back_on_close` (`session_control_parts/session_handoff.py`). With the chain switch off it moves nothing; with it on it hands the closed session's workers to its own manager, or back to their creator.
3. A send typed into a worker whose Ultra Mode root is closed is refused with `crew_root_closed` (`chat_api/chat_send.py`) until the person reopens the root.

## Options

| Option | What close does | Cost |
|---|---|---|
| A. Close all | the conductor's workers close with it | running work stops |
| B. Ask first | the close dialog offers "close N workers too" | new dialog and choice |
| C. Close idle only | idle workers close, running ones stay | stuck running workers remain |

## Decision

Option A, narrowed to conductors:

| Closed by | Session closed | Its workers |
|---|---|---|
| the person | an Ultra Mode root or a Bot thread | close, down the whole line |
| the person | any other session | stay open |
| an agent, an app token or the idle sweep | any session | today's rule, unchanged |

- The workers are the open sessions that still name the conductor as their manager (`session_managers.holder`) after the conductor's own close and hand-back. A worker the hand-back moved up the line or back to its creator stays open.
- Each worker closes through the same `close_slot` path as the tab ✕, so it is archived to history and can be reopened.
- The DELETE response adds `closed_workers` and `failed_workers` only when there were workers; a worker that fails to close stays open and is named.

## Not decided here

- A confirm dialog or a "keep workers" choice on close (option B).
- Any change to agent-driven `session_close` or to `agent.session_manager_chain`.
