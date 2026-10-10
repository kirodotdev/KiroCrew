# session-ledger

## What a reader learns

What one long-running session is carrying: its goal, the phase it is in, the
next step as an intent, the approaches it tried and rejected, and what it
produced. This is the record a cold resume reads, so the page is **what the work
would be resumed from**.

## Types its blocks bind

One fold, `ledger`, plus a headline you write.

| field | type | source |
|---|---|---|
| headline | `text` | `{"agentic": true}` |
| goal | `text` | `ledger.goal` |
| phase | `enum`, `choices: ["planning","implementing","verifying","reviewing","done"]` | `ledger.phase` |
| next step | `text` | `ledger.next` |
| rejected approaches | `number` | count of `ledger.tried` |
| the newest rejection | `text` | and why |
| artifacts | `number` | count of `ledger.artifacts` keys |
| opened | `timestamp` | `ledger.created_at` |
| last progress | `timestamp` | `ledger.last_progress_at` |

The headline is the one agentic field: no fold records a human-facing read of
where the work stands, which is exactly the bar for writing one.

## Block layout

| block | type | holds |
|---|---|---|
| headline | - | your read, marked as yours |
| phase | `pills` | the five phases, current one lit |
| next step | `note` | the intent, as the title |
| what was ruled out | `stat` | count, caption "approaches rejected" |
| when | `timeline` | opened, last progress |

## When it fits

One session, long-lived, and the reader's question is "where would this pick up
again" - a handover, a resume after a break, a review of a stalled lane. It is
about a session, not a crew, so it answers nothing about a fleet.

## For a different subject

"Phase" is the part to rewrite in the subject's own stages: sourcing /
screening / onsite / offer for a hiring loop; scoped / booked / packed /
travelling for a trip. **Keep "tried and rejected"** - a page that only says
what is being done invites a reader to re-propose what was already ruled out,
which is the specific waste this view prevents.
