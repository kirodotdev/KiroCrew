## Problem / Motivation

Older dashboards called `POST /api/agents/sync` on every chat mount, and that
sync made a crewmate out of every custom agent under `~/.kiro/agents`: a
`config.agents` row with no `member_id`, on the shared `default` memory store,
bound to the agent by name. An existing user therefore opens the Crewmates page
to one crewmate per custom agent, most of them never used and none with a
memory of its own.

## Why it matters

These are exactly the users the Crewmates launch is for. Without this, the launch
lands on a roster full of crewmates nobody asked for, and the ones the user does
chat with are shared-memory shells rather than real members.

## What changed (motivation → approach → change)

Implements the accepted RFC: `docs/request-for-change/rfc-crewmates-launch.md`
(screen 03, existing users) — as a one-time startup migration, no UI
(amendment [#13032](https://github.com/kirodotdev/KiroCrew/pull/13032)).

| Piece | What it does |
|---|---|
| `crewmate_prune_migration.py` (new) | Once, when the dashboard gateway starts (not the headless `--slack-only` entrypoint): finds the rows an older sync generated, removes the never-chatted ones, leaves the chatted ones untouched, keeps any it cannot judge, writes a marker. The whole pass runs under an exclusive cross-process lock (`<config dir>/crewmate_prune.lock`, `platform_compat.file_lock`, waited for without give-up: a contender that returned early would open its own barrier while the holder can still delete) and the marker is created `O_EXCL`, so two gateways on one data home cannot both prune |
| `dashboard/server.py` | `_kick_crewmate_prune` runs the pass as a tracked background task right after the listener binds (the `_kick_*` shape); nothing on the startup path awaits it, so a session-count-scaled scan never gates readiness. Slot restores need not wait: a removed row has no DM binding and no session naming it, so no slot can be rebuilt for it. `_register_crewmate_prune_gate`, installed next to the workflow gate BEFORE the bind, clears `crewmate_prune_settled` and adds one middleware that holds every request whose method is not `GET`/`HEAD`/`OPTIONS`, on any path (`/api/*` and `POST /v1/chat/completions` alike), plus every request under `/api/members` whatever its method (the roster read folds and retires a member's legacy activity file and appends reconciles to the member log — the places the pass reads), until the pass sets the event in `finally` (60 s, then 503 `prune_in_progress`; a 503 writes nothing). The non-HTTP writers (subagent pump, channel agent resume, cron) start only after `await_crewmate_prune_settled` returns — in `slack/gateway.py` `run()` past `KIROCREW_READY`, so readiness never waits — and it returns only once the pass has RETURNED: past its 60 s budget it sets `crewmate_prune_abandon` (the pass polls it before each candidate and inside the config lock before each delete, keeps the rest under `doubted`, writes the marker, returns) and keeps waiting. No writer ever runs beside a pass that can still delete; the pass always returns (non-blocking opens, bounded lock acquires). Fast path: one `is_set()` read |
| `dashboard/state.py` | `crewmate_prune_settled` event (set by default so tests and the CLI never wait); `crewmate_prune_abandon`, the stop signal the pass's worker polls |
| `channel_transcript_migration.py` | `migrate_channel_transcripts(remove=...)`: the startup merge of orphaned dashboard copies into their channel transcripts still lands before the session restores, but while the prune has not settled the copies are kept — an orphan's first line is the only record of the agent its dashboard surface ran as, the evidence the pass reads — and `_kick_deferred_transcript_removal` (a tracked task, off the readiness path) re-runs the migration with removal on once the pass has returned; the re-merge is byte-identical |

A **candidate** is a row that is exactly what the sync wrote: its name is its
`kiro_agent`; the spec is on disk, user-authored, not the runtime's own or a
private copy; its `description` equals the spec's; every other field is at its
default, no undeclared key (`_is_fresh_sync_shape`, on the raw row); a name
`config.local.json` touches is never one. **Chatted** means any of: a session's metadata names
it in `agent` or `execution_context`; its member activity log holds a session pointer for it
(`record_activity`; survives the agent switch that rewrites the metadata); or
its DM thread was opened. All three are read strictly here.
Never-chatted → deleted under the base config lock and, nested, the overlay's
and the spec's (overlay then spec, both held until the delete commits), only
while the row still has the sync shape, the overlay does not name it and the
spec still carries that description; a change is refused (no marker). Chatted → **untouched**, shared store, no `member_id`: a memory
binding is identity, chosen at creation (`memory-skills-hooks.md`). **Removal
needs a complete history; the pass always finishes.** Two kinds of session file
are kept apart (`_session_agents_named`): one that reads and parses is
evidence whatever it says — every agent its record names, or nobody (an older
build's first line, a record naming no agent, another agent) — and never voids
the pass; one
that cannot be read (open/read failure, gone since the listing, not UTF-8,
empty, over budget, not JSON) makes the history incomplete, and an incomplete history removes nothing:
every candidate is kept (`doubted`), the files listed (`unreadable_sessions`).
Per candidate, an activity log that cannot be loaded or whose segments are
present but empty (a torn write the store reads as absent), an unreadable
legacy activity file or DM binding each keep that crewmate. The marker still
lands, so one bad file never makes the prune re-run on
every boot; a kept row loses nothing. Agent-writable files are opened with
`open_file_no_reparse`, regular files only (a FIFO is "no record", never a
hang). Only `config.json` rows move. A completed pass
writes `<config dir>/crewmate_prune_migrated.json` and logs one INFO line,
`removed N unused auto-generated crewmates: <names>`, plus a WARNING for any
kept on doubt.

**What changed vs the RFC's screen 03.** The RFC offered a user with custom
agents but no crewmates to add them. That path is not this PR's job: #12224
ended the enrol-on-mount behaviour, and the Crewmates page's empty state with
**New crewmate** (#12924) is the way from a custom agent to a crewmate. An
existing user carries the opposite — the crewmates sync already made — so this
PR settles those once, at startup, with no UI (RFC amendment #13032, merged).
Two refinements past its wording: an edited generated row is kept; an
incomplete history keeps every candidate.

Specs: `crew-mode.md` (new section; the catalog's note on the sync's exclusions
updated), `docs/feature-map/README.md` (new row).

| Finding (GPT 5.6, span `eced8e08c12f`, `crewmate_prune_migration.py`) | Response |
|---|---|
| Spec lock ends before deletion commits (`:756`) | The spec lock is now entered on the same `ExitStack` as the overlay's, inside the base config lock, overlay then spec — the order `_write_bindings` and the template create keep — and both are released only after `update_config_locked` has renamed the base file into place. A spec edit that waits on the lock therefore lands after the row is gone, never between the description re-read and the delete. Two tests: the base file already lacks the row when the spec lock is released; the nesting order is overlay then spec. |
| Transcript scan races startup transcript deletion (`:508`) | Two changes. The pass no longer skips a file that is gone between the listing and the open: it is listed under `unreadable_sessions`, the history is incomplete, every candidate is kept (test added). And the one startup step that deletes a transcript — `migrate_channel_transcripts`, which merges an orphaned dashboard copy into its channel transcript and drops the copy's `agent` — now runs with `remove=False` while the pass has not settled: the merge still lands before the restores, the copy stays with its first line intact, and `_kick_deferred_transcript_removal` removes the copies once the pass has returned, off the readiness path (nothing on it waits). Tests: `remove=False` merges but keeps the copy and its first line, and a removing pass then converges; the deferred removal starts only after the event is set, with removal on. |
| Legacy agent switches erase the only usage evidence (`:441`) | Per-session evidence is now the union of every agent the metadata record names: `agent`, `execution_context.selection_name`, `execution_context.template_id` (an interrupted switch can leave them apart). Nothing below that line carries agent provenance: `ConversationLog.append` writes role, content, `ts`, `tools`, source and `meta.mid`; `api_chat_slot_agent` rewrites the metadata line in place and appends no row, so no switch marker exists and no earlier agent can be recovered from a transcript. A session that ran as a crewmate, switched, and had every field overwritten is found only through the member activity log. Residual, accepted by the maintainers' ruling: a session from before the activity log that did exactly that leaves no trace; its crewmate row is removed, its agent file and shared store stay, **New crewmate** re-enrols it. A pass is not voided by such sessions existing. |

## Backwards compatibility

Compatible for configuration and files: no key changes shape, no agent file is
touched, transcripts stay. One behaviour changes on purpose: a never-chatted
sync-generated crewmate disappears from the roster on the first start of this
build — its custom agent is still there and **New crewmate** re-enrols it in one
step. A chatted one keeps its name, thread and exact memory binding.

## Tests

CI note: Backend Tests (3.12, 1) and macOS Tests (2) reds on this branch are
`test_pdf_extract.py` (`TestChildCaps::test_main_reports_a_wrapped_memory_error_and_exits_failed`
on Linux; `TestBound::test_the_child_itself_exits_with_the_memory_report_under_the_profile`
plus a `TestChildCaps` worker crash on macOS), the xdist flake main also shows
(tracking issue #13030); not chased here. Backend Tests (3.12, 3) red is
`test_dashboard_server_startup_coverage.py::TestReserveDashboardPort::test_bind_once_matches_kernel_listener_posture`
(`OSError: [Errno 99]` binding on the runner), red on main too; not chased here.
Backend Tests (Windows) (2) red is
`test_taskrunner_coverage.py::TestLogTask::test_on_loop_offloads_the_write`
(an off-loop write not observed within 100 `sleep(0)` ticks on the Windows
runner); the task runner is not in this diff, not chased here.


- `test/test_crewmate_prune_migration.py` — seeds an old-style config (one
  chatted, one never-chatted, one hand-created with a `member_id`, one
  sync-shaped row that owns a non-empty memory store) and asserts exactly the
  never-chatted one on the shared store is removed, the chatted one keeps its
  exact binding (shared store, no `member_id`), the hand-created one and the one
  with memory are untouched, the marker names what happened, and a second boot
  is a no-op;
  a session that merely named the crewmate counts as chatted, and so does an
  activity record in the member event log or the legacy `activity.jsonl` /
  `.1` (for the exact name only — a colliding slug's other member does not);
  a session file that does not parse, is not UTF-8, is empty, has a first line
  over budget, cannot be opened or vanishes after the listing keeps every candidate and is named in the
  marker, while one that reads but names another agent, no agent, or is JSON
  that is not a record is evidence and removes as usual; a missing
  conversation log keeps every candidate, an unresolvable or
  containment-refused binding path, a malformed or non-UTF-8 binding file, an
  unparseable or over-budget activity file, a corrupt event log and a torn
  (zero-byte segment) event log each keep that crewmate only, a FIFO or link
  at a session or activity path is "no record" and does not hang, and a
  doubted pass is not re-run; a doubt on one candidate does not spare the
  next; a binding naming another crew is not this crewmate's; a row that gains
  a model — or an overlay leaf — between the judgement and the lock is refused
  with no marker; a legacy row missing keys is still removed; a row with a
  model, triggers, a star, an avatar, another workspace, a description that
  differs from the spec's, an undeclared key or a renamed key is never a
  candidate, nor is a name `config.local.json` touches; a row or spec
  description edit under the lock refuses the delete, and the overlay and spec
  locks outlive the base write, overlay then spec; the candidate filter (spec gone, package
  spec, runtime-owned, private copy) admits only the user's own; a pass told
  to stop keeps every unjudged row under `doubted`, still writes the marker,
  and a stop that lands inside the config lock leaves that row; the gate holds
  `GET /api/members` and the routes under it (not a path that merely shares
  the letters); the writer-side wait returns as soon as the pass settles, and
  past its budget sets the stop signal and still does not return until the
  pass has; the cross-process lock: a pass waits for a holder and then finds
  its marker, a holder that outlasts the wait is waited for (never given up
  on), a marker that appears while waiting ends the wait,
  the marker is created exclusively, a failure inside the pass is not read as
  contention, and the lock is released after a pass that returns or raises.
- `test/test_agent_spec_hardened_reads.py` — the strict-reader call-site
  ratchet gains the prune's under-lock spec re-read, labelled
  `crewmate_prune` / `dashboard` like the other dashboard-surface reads.

## Manual verification

N/A — a startup migration with no UI; the unit tests above run the real config
loader against an isolated home.

## Related Issues

no linked issue: launch-review item, no tracking issue filed.

## Checklist

- [x] At most two commits (one is the norm), with a Conventional Commits title (`feat|fix|docs|refactor|perf|test|chore|ci|build|revert: ...`)
- [x] Existing tests pass and new tests added for new functionality
- [x] Self-review completed; code follows project style guidelines
- [x] Documentation updated (if applicable)
- [x] No secrets, credentials, or internal references in the diff
