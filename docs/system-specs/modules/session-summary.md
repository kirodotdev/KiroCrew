# Session Summary

## Overview

The session summary is an intent-level description of a chat session, generated
after a turn completes and rendered in the chat right panel. Its purpose is
narrow: make **re-entering** a session cheap. A person who kicked off work, got
pulled away, and came back needs three things — why they asked for this, what
happened, and what to do next — without re-reading the transcript.

The organizing unit is an **intent**: a goal the person pursued, which can span
many turns. It is not a per-turn log and not a diff. A session where the person
pivoted produces several intents.

Off by default. Generating a summary spends tokens on a turn the user did not ask
to pay for, so the whole subsystem is inert until `session_summary.enabled`.

| Concern | Module |
|---|---|
| Config section and its clamps | `config/loader.py` (`SessionSummaryConfig`) |
| Transcript extraction, payload shaping | `session_summary.py` |
| Turn-end generation, the prompt | `dashboard/chat_summary.py` |
| Sidecar cache | `history.py` (`ConversationLog`) |
| Read endpoint | `dashboard/chat_handlers.py` (`api_chat_slot_summary`) |

## Storage: a sidecar, never the transcript

Summaries live in `~/.kiro/crew/sessions/.intents/<safe_key>.json`, keyed by the
session's **transcript** key, with the payload shape below plus a `sig` field.

```json
{"sig": 1760000000.5, "generated_at": 1760000123.1, "user_turns": 7,
 "last_activity": "2026-08-10T10:00:00+00:00",
 "intents": [ … ], "constraints": [ … ]}
```

Three properties of this arrangement are load-bearing:

**The transcript is never rewritten.** A summary write touches only the sidecar.
The session JSONL's mtime is the cache-validity signature for *every* derived
artifact and the sort key for `list_sessions`, so a write that advanced it would
invalidate unrelated caches and reorder the user's session list. Housekeeping
rewrites (title edits, consolidation, rotation) deliberately restore the previous
mtime for the same reason — see `history.md`. `test_session_summary_storage.py`
pins that writing and reading a summary leaves the transcript's bytes and mtime
untouched.

**`sig` is the session file's mtime.** Any real append advances it and invalidates
the cache; a metadata-only rewrite does not. This makes staleness exact and free
to check — one `stat`, no content comparison.

**It is a different file from the one-line summary.** `.summaries/<key>.json`
holds the on-demand one-line description shown in the sessions list, written by
`set_cached_summary`, which overwrites the whole file. The two artifacts have
independent writers and independent triggers, so sharing a file would have them
clobbering each other — exactly the read-modify-write race the sidecar design
exists to avoid.

Both sidecars are reaped by `delete_session`, which is contractually a permanent
removal: a deleted session must leave no orphaned model-generated text on disk.

**The write is guarded against resurrecting a delete.** Generation holds no lock
while the model call is in flight (it can take tens of seconds), so a permanent
`delete_session` can complete in that window — removing the transcript and both
sidecars. `set_cached_intent_summary` therefore takes `_locked(key)` and writes
only if the transcript still exists with the exact signature the generation
started from; otherwise it refuses (returns `False`) and the generator discards
the payload without pushing a WS update or advancing its turn mark. The same
check drops a summary a mid-generation append has already made stale, rather
than storing it as the latest word.

### Keyed on the transcript key, not the slot key

The cache is keyed by `slot_history_key(slot)`. That differs from `slot.key` for a
channel-born slot the dashboard could not bind, and keying on the slot key would
stat a file no read path addresses — the summary would be written to a phantom
transcript and the panel would never find it. Generator and endpoint resolve the
same key for this reason.

## Payload shape

Each intent carries:

| Field | Meaning |
|---|---|
| `title` | The goal, short, in the person's terms |
| `ranges` | **List** of `[first_user_turn, last_user_turn]` pairs |
| `status` | `active` \| `completed` \| `abandoned` — about the work |
| `verified` | `true` \| `false` \| `null` — about the result, independent of status |
| `state` | Derived single word the panel renders |
| `last_touched_turn` | Highest turn in `ranges`; the panel's sort key |
| `origin_turn` | The turn that triggered this intent, or `null` |
| `initial_intent` | Why the work started |
| `progress` | A runbook of what is true now, not a history |
| `next_steps` | `{what, why, expect}` — the summarizer's inferences |

**`ranges` is a list, and ranges may overlap.** An intent can go dormant and
resume days later, and one intent can sit inside another's span (a question asked
mid-intent whose answer becomes part of the work). A single interval cannot
express either, and forcing them apart would misdescribe the session.

**Status has two axes because they disagree in the cases that matter.** Work whose
discussion ended while the goal was never reached — diagnosed but never fixed,
merged but never run — is exactly what a person forgets. `status: completed` with
`verified: false` says that; one field cannot. `derive_state()` collapses the two
into the single word the panel shows, so both surfaces agree by construction:

| status | verified | state |
|---|---|---|
| `abandoned` | any | `dropped` |
| `completed` | `false` | `needs-you` |
| `completed` | `true` / `null` | `done` |
| `active` | any | `in-progress` |

`constraints` is session-level, not per-intent: recurring operational facts about
how this project has to be run (a required build flag, a step that must follow a
change, a name the user corrected). Capped by `max_constraints`, default 5 — a
long list stops being read. Durable cross-session preferences belong in
**lessons**, not here; this field is scoped to the session's project.

## Extraction: what the model actually reads

`extract_turns()` reduces raw transcript records to a bounded input:

- **`role` and `content` only.** Every other field is ignored, notably the `meta`
  blob on assistant rows — in one real 15.6 MB session, message content totalled
  602 KB and `meta` was the other 14.65 MB. Ignoring it rather than excerpting it
  is the single largest saving available.
- **Tool and error rows are dropped.** The assistant has already distilled them.
- **User messages whole, assistant messages excerpted** head-and-tail at
  `assistant_excerpt_chars` (default 400). Intent lives in the user's messages,
  which are small; progress lives in the assistant's, which are not.

Measured against three real sessions, this reads roughly 1% of a transcript's
bytes.

### Mechanically detectable traps live here

Two transcript shapes reliably produce a wrong summary and are both detectable
without judgement, so they are labelled in extraction rather than left to the
prompt:

- **Automation posting under `role: "user"`** — cron notifications, subagent
  completion events, auto-nudge cycles, tool refusals, restored webhook context.
  These are flagged `injected`, excluded from the user-turn count, and rendered
  as `[automation, not the user]`. Counting one invents a goal the person never
  had.
- **A verbatim resend** of the previous user message is marked `repeat_of`. A
  resend looks identical to insistence, and reading it as insistence produces
  "the user asked repeatedly, so it was ignored" for a request that already
  succeeded.

An oversized user paste (a stack trace, a log dump) is capped so one paste cannot
crowd out the session; intent survives truncation.

## Generation

Dispatched from `_finish_queue_cycle` in `dashboard/chat_runner.py` — the post-turn
hub, alongside auto-titling — as a background task. Not from `hooks.py`: the
`Stop` hook event is script/config-driven and fires only for dashboard turns, and
shipped behavior does not belong on a user-extension seam.

`_should_summarize()` returns a **skip reason string** rather than a boolean, so a
declined pass is auditable in logs; a feature that silently declines to run is
indistinguishable from one that is broken.

| Reason | Cause |
|---|---|
| `disabled` | The flag is off — the common case, and it costs nothing |
| `in_flight` | A pass for this slot is already running |
| `memory_mode` | Incognito or temporary: no derived artifact from this conversation (mirrors `history.INCOGNITO_MEMORY_MODES` — a temporary transcript is discarded, so a persisted summary would outlive it) |
| `stop_reason:<r>` | The turn did not cleanly end |
| `too_few_turns` | Below `min_user_turns` |
| `cadence` | Fewer than `regenerate_after_turns` since the last pass |

**Only a clean `end_turn` qualifies — and the marker is fail-closed.** Timeout,
cancel-unacked, tool-stall and stale-recover all describe a turn that was cut
short; summarizing one would present interrupted work as concluded. Because
`_finish_queue_cycle` does not receive the stop reason, it is recorded on the slot
at `EVENT_COMPLETE` (`slot._last_stop_reason`) — and **cleared at turn start**, so
a turn that dies before `EVENT_COMPLETE` (ACP crash, auth expiry, transport drop)
leaves an empty marker rather than the previous turn's stale `end_turn`. The gate
requires the marker to equal `end_turn` exactly; empty means "this turn never
finished" and skips as `stop_reason:missing`.

**Input is the full on-disk transcript, not `slot.messages`.** A restored slot
keeps only the most recent 500 messages in memory (`chat_persistence` caps the
restore), so summarizing the in-memory tail of a long session would regenerate
from a truncated view and overwrite the sidecar — earlier intents would silently
vanish from the panel. The generator reads `read_messages_chained()` off the event
loop; the cheap slot-level gates (disabled, unclean stop) run first so the common
skip cases cost no disk IO, and `extract_turns` still bounds what the model reads.

**An unchanged transcript costs nothing.** Before any model call the pass checks
the sidecar; a valid signature means the stored summary is already exactly right.
This is what makes the summary stable rather than a live feed — a property of the
cache, not something the UI has to enforce.

The model comes from `AgentConfig.resolve_model("background")`; the role never
inherits `agent.model`, so unattended summarization cannot ride the interactive
flagship. Every failure path is swallowed and logged: a lost summary must never
surface as a failed turn, and the previous cached summary stays valid.

On success, `push_session_summary()` broadcasts `{"_type": "session_summary"}` so
the panel invalidates immediately rather than polling.

### The prompt's trap list is tested

The judgement traps cannot be detected in code, so the prompt names each one, and
`test_session_summary_generate.py` asserts the prompt still mentions them. A later
trim of the prompt fails a test instead of quietly degrading every summary:
retractions supersede the claim they retract; option lines are offers that were
mostly declined, not requests; merged is not verified; a feasibility question is
not a work order; facts go stale inside one session; a compaction is not a topic
boundary; the session title describes only its beginning.

The prompt also ranks the boundary signals: commits and merges punctuate a session
more reliably than any phrasing, timestamps separate a daily routine from a failed
retry, and a user correction is the highest-value signal per character in the file.

## Endpoint

```
GET /api/chat/slots/{slot}/summary
```

Read-only, and deliberately so: it never triggers generation. A panel that
generated on open would spend tokens on every look and reward refreshing — the
behavior the feature exists to remove.

| Response | Meaning |
|---|---|
| `200` | `{enabled, stale, intents, constraints, generated_at, user_turns, last_activity}` |
| `200` with `enabled: false` | Feature off; the panel explains itself rather than erroring |
| `404` `{"code": "slot_not_found"}` | Unknown slot, or a slot the calling app does not own |

`stale: true` means a summary exists but the transcript has moved on. The stored
payload is still returned: an empty panel reads as "this is broken" while a stale
one reads as "not regenerated yet", which is the truth. `read_intent_summary()` is
the non-strict accessor for this; `get_cached_intent_summary()` is the strict one
the generator uses.

The flag gates the READ as well as the write. Turning the feature off has to stop
serving summaries, not merely stop producing them — otherwise a sidecar written
during an earlier opt-in keeps being returned after opt-out, which is the opposite
of what the switch promises.

Reads are subject to App Kit §5.2 ownership isolation (mirroring
`api_chat_slot_delete`): a summary is derived conversation content, so a slot
merely existing must not make it readable. An app token may read only slots that
app created, never an unscoped slot; a dashboard user (explicit empty
`request["app"]`) is unaffected. A denied read answers `404`, not `403`, so a
foreign slot is indistinguishable from a missing one (anti-enumeration, CWE-204);
the true reason is recorded via SEL.

## Redaction

The payload is model output derived from transcript text, so anything the user
pasted into the conversation — a token, a key, a beacon URL — can be reproduced
inside it. `normalize_payload()` therefore runs the whole payload through the
standard `redact_credentials` + `redact_exfiltration_urls` chain before it is
returned, recursively: the payload nests (intents → next_steps → strings) and a
top-level-only pass would leave every field the panel actually renders unredacted.

Redaction sits in normalization rather than at the write site so that every path
into the sidecar is covered by construction, and at write time rather than render
time because the sidecar is durable — a secret written once would be served on
every subsequent open.

## Configuration

`session_summary` in `config.json`. All knobs exist so cost can be reduced without
losing the feature.

| Key | Default | Purpose |
|---|---|---|
| `enabled` | `false` | Top-level switch; the subsystem is inert while off |
| `min_user_turns` | `2` | A one-exchange session has no intent structure |
| `regenerate_after_turns` | `1` | Turns between rebuilds; raise to trade freshness for tokens |
| `max_intents` | `8` | Oldest-touched are trimmed; the panel collapses them anyway |
| `max_constraints` | `5` | Project notes; `0` suppresses the section |
| `assistant_excerpt_chars` | `400` | Head/tail kept per assistant message |

Out-of-range values are clamped with a warning rather than raising, and a
malformed section degrades to defaults, so a hand-edited `config.json` cannot
prevent the gateway from starting.

## Scope in this release

Dashboard sessions only. The generator hangs off the dashboard turn-end hub, so
Slack, cron, webhook and task-runner turns do not produce summaries. Reaching
them means threading the flag through the same four call sites
`skills.auto_create_from_sessions` uses (`cli.py`, `cli_server.py`,
`dashboard/server.py`, `slack/gateway.py`). This is a deliberate cut, not an
oversight.

No governance capability scope. Nothing enforces one, and a scope is a data-only
addition later; if one is ever added it must be registered inline in
`SCOPE_CATALOG`, never lazily from a feature package, because
`load_security_policy()` runs at boot before feature imports.

## Tests

| File | Covers |
|---|---|
| `test_session_summary_config.py` | Defaults, clamping, disk parsing, malformed sections |
| `test_session_summary_extract.py` | Role filtering, `meta` ignored, excerpting, the two mechanical traps |
| `test_session_summary_storage.py` | Sidecar round-trip, invalidation, transcript-untouched, delete reaping |
| `test_session_summary_generate.py` | Gating, caching, failure containment, prompt trap coverage |
| `test_session_summary_api.py` | Status codes, error `code`, stale flag, never-generates |
