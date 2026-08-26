# Session Work Ledger

Status: implemented (this PR)
Owners: gateway core (`session_ledger.py`), MCP tools (`mcp_tools/ledger.py`)

## 1. Problem

Long-horizon loops — `monitor_start` babysit loops, goal loops, any session that
wakes dozens of times — carry their working state in the context window. Every
cycle appends a full turn to the same session, so "what am I doing, what have I
tried, where are my artifacts" survives only as prior transcript turns. When the
context fills, compaction summarizes generically and the agent loses exactly the
state it needs to keep going: approaches already rejected, the branch it was on,
the reason a phase was entered.

Compaction itself is out of scope: it is performed by the agent harness (the
ACP/kiro-cli layer), not by this codebase, so the fix cannot be "compact
better". The fix is to stop using the context window as the authoritative store
for loop state.

The pattern already exists in this repo — three times, hand-rolled per app:

- Issue Radar's crew ledger (`apps/builtins/issue_radar/backend/crew_store.py`):
  work items with `phase` / `next` / `tried[]`, an append-only content-addressed
  event log, and the rule that a phase never moves without a logged event.
- Ops Mission Control's knowledge ledger
  (`apps/builtins/ops_mission_control/backend/ledger.py`): append-only JSONL,
  content-addressed ids, locked read-modify-write.
- The heartbeat/cron surfaces re-derive state per cycle from files.

Each app that needs durable work state re-invents the primitive. Sessions that
are not one of those apps get nothing.

## 2. Solution overview

A generic, per-session **work ledger** on disk:

- One directory per session under the data home, created lazily on first write,
  deleted when the session's history is permanently deleted.
- A mutable **state record** (goal, phase, next intent, tried/rejected
  approaches, artifact pointers) plus an append-only **event log**.
- Two core MCP tools, `session_ledger_read` and `session_ledger_record`, with
  session-resolved identity: a session can only ever touch its own ledger.
- Auto-nudge integration: when a monitor loop fires on a session that has a
  ledger, the nudge body carries a compact snapshot of the state record, so each
  cycle starts from the ledger instead of from transcript memory.

The context window becomes a cache; the ledger is the authority. A loop cycle
needs the snapshot plus its check instructions — its cost no longer grows with
the number of cycles that came before it.

## 3. On-disk layout and lifecycle

```
<data_home>/ledger/<store-name>/
    slot_key        # breadcrumb: the exact ledger key this dir belongs to
    state.json      # the whole record, replaced atomically on every write
    .lock           # cross-process mutex inode (never replaced by writes)
```

- **Identity is the exact key.** The ledger key is the session key with only
  the dashboard prefixes stripped (one dashboard session is legitimately
  spelled both `dashboard_chat-X` and `chat-X`, and both must reach one
  ledger); nothing else is rewritten. `<store-name>` is a readable charset
  fold of that key plus a sha256 prefix over the FULL key — the fold shapes
  only legibility, the digest carries identity, so two distinct
  colon-structured channel keys can never share a directory (a lossy charset
  fold as the identity would let one session read and overwrite another's
  state). Path safety mirrors `subagent_persistence._agent_dir`: hostile keys
  refused, resolved path required to stay inside the ledger root.
- **Not `/tmp`, not the scratch dir.** Scratch (`KIROCREW_SCRATCH`) is keyed to
  process liveness and swept hourly once the owner process group dies; a ledger
  must survive gateway restarts for as long as its session exists.
- **Tab close keeps the ledger.** Closing a dashboard tab
  (`api_chat_slot_delete`) deliberately preserves resumable session state, and
  the ledger is part of that state.
- **History deletion reaps the ledger.** Both permanent-delete endpoints
  (`DELETE /api/sessions/{key}` and the bulk `DELETE /api/sessions`) funnel
  through `_remove_slot_for_history_key`; the ledger purge runs at the END of
  that funnel — after the slot's turn is cancelled and its session destroyed —
  so an in-flight write from the dying turn cannot land after the purge. A
  write racing in from another process can at worst recreate an orphan
  directory that the next delete sweeps; ledger content is disposable
  intermediate state, so that residue is accepted rather than buying a
  tombstone protocol for data nothing reconstructs from.

## 4. Data model

`state.json` is ONE document — the mutable state record carrying a bounded
event tail — replaced atomically (`atomic_write`: temp file + rename) on every
write. Schema-versioned; unknown fields preserved, defaults coerced forward on
read; a malformed or oversized file is treated as absent, never fatal.

| Field | Type | Meaning |
|---|---|---|
| `schema` | int | record version, currently 1 |
| `goal` | str | the binding objective of the workstream |
| `phase` | str | current phase; free-form but see write discipline |
| `next` | str | the resumable intent — a concrete next step, not a status word |
| `tried` | list | `{approach, rejected_because, at}` — appended, never rewritten |
| `artifacts` | dict | string-to-string pointers: worktree, branch, pr, paths |
| `events` | list | bounded tail of `{ts, kind, text}` progress lines, oldest aged out |
| `created_at` / `last_progress_at` / `finished_at` | str | ISO timestamps |

### Write discipline (carried over from the crew ledger)

- A `phase` change **requires** an event (`event` + a recognized `event_kind`)
  in the same call. Because state and event land in the same atomic write,
  the invariant is crash-atomic by construction: no failure between "phase
  moved" and "event logged" can exist.
- Every field is clamped at write time and the event tail is bounded, so the
  document cannot grow without limit and every read is O(record), never
  O(history).
- `last_progress_at` advances on every accepted record call; `finished_at` is
  set when `phase` enters a terminal value (`done`, `abandoned`).
- Fields omitted from a record call keep their stored values: partial updates
  are the norm and never a way to erase state.
- Writes hold a per-ledger cross-process file lock with a **bounded** acquire:
  a wedged holder costs one refused write (surfaced as a retryable error),
  never a worker thread parked forever. Reads are lock-free — the atomic
  replace means a reader sees the old or the new document, never a torn one,
  and state + events always come from one transaction.

## 5. MCP tools

Registered as a core domain module (`mcp_tools/ledger.py`, listed in
`DOMAIN_MODULES`), following the `learn.py` template: `schemas()` + `HANDLERS`,
handlers reach the gateway over the loopback HTTP API with the session-resolved
identity header. There is no slot-key argument — the backend resolves the
calling session and refuses requests that carry no session identity, exactly
like the Issue Radar crew routes (raw HTTP gets 403).

- `session_ledger_read` — no arguments. Returns the state record plus the tail
  of the event log. The tool description tells the agent this is its own
  session's durable work state.
- `session_ledger_record` — `goal?`, `phase?`, `next?`, `tried_approach?` +
  `tried_rejected_because?`, `artifacts?` (string map, merged), `event?` +
  `event_kind?`. Enforces the phase/event rule server-side.

Subagent, cron, and channel sessions may call the tools; each writes the ledger
of its own session key. The primitive is deliberately session-scoped — there is
no cross-session read, which keeps the authorization story trivial.

## 6. Auto-nudge snapshot injection

`compose_nudge_body` (`dashboard/handlers/autonudge.py`) is the single
composer used by all three fire callbacks (dashboard slot, Slack thread,
Discord DM). When the loop's session has a ledger with a non-empty,
non-terminal state record, the rendered body is prefixed with a bounded
`[work ledger]` block — goal, phase, next, the last few tried entries,
artifacts — capped in size so a runaway ledger cannot flood the turn. The
ledger read runs in a worker thread: a slow or wedged filesystem costs one
loop's snapshot, never the gateway event loop.

No MCP schema or directive plumbing changes: the snapshot is derived
server-side at fire time from the session key the loop already carries. Loops
on sessions without a ledger render exactly as before. Cron and heartbeat
composers are intentionally untouched — heartbeat is stateless-per-cycle by
design, and cron sessions can call `session_ledger_read` themselves.

## 7. Non-goals

- **Compaction.** Owned by the agent harness (ACP/kiro-cli); this feature
  reduces what compaction can lose, it does not change how compaction works.
- **Turn-internal durability / operation log.** Journaling tool calls with
  stable operation ids so an interrupted turn can resume without re-executing
  side effects is a separate, finer-grained track. This ledger records state
  *between* wakes, not execution *within* a turn.
- **Multi-writer arbitration / fenced leases.** One gateway process owns a
  session's turns; concurrent cross-process writes to the same ledger are
  serialized by the file lock. There is no takeover semantic to protect, so
  version-fenced execution tokens would be complexity without a customer.
- **UI.** No dashboard surface in this iteration; the ledger is agent-facing.

## 8. Failure modes

- Ledger read/parse failure, or a state file past the size ceiling → treated
  as absent; tools report empty state; nudge injection skips the block. Never
  blocks the loop or the turn.
- Lock contention → the acquire is a bounded poll; on expiry the record call
  fails closed with a retryable error. Nudge-time reads are lock-free and
  never wait on a writer.
- Session deleted while a loop still points at it → purge wins; subsequent
  reads see an empty ledger. A cross-process write racing the purge can
  recreate an orphan directory, which the next delete sweeps — accepted for
  disposable state (§3).
