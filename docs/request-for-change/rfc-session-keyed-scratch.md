---
title: Session-keyed scratch, a $KIROCREW_SCRATCH that lasts as long as the chat
status: draft
author: Pearcekieser
created: 2026-10-28
last-audited: 2026-10-28
audited-at: c6789799df
doc-pr: null
implementation-prs: []
tracking-issues: [15979]
supersedes: []
superseded-by: []
---

# RFC: Session-keyed scratch, a $KIROCREW_SCRATCH that lasts as long as the chat

The system prompt tells every agent to keep clones, probe scripts, build logs
and PR drafts in `$KIROCREW_SCRATCH`. The directory behind that name lives as
long as the processes that mounted it. A dashboard chat lives longer than its
processes: a gateway restart, a resume after a backend error, or an idle reap
followed by a revive all give the chat a new process, and the new process gets
a new, empty directory. The hourly sweep deletes the old one an hour later.

This RFC proposes that the scratch tree belongs to the session key instead.
A new process for a known session rejoins the session's tree. The sweep stops
asking whether the owner processes are alive while the session is open, and
starts its clock when the session is closed. `$KIROCREW_SCRATCH` then names the
same directory for the whole life of the chat, which is the durable
per-session directory agents have been missing.

[#15979](https://github.com/kirodotdev/KiroCrew/issues/15979) reports the
resume case and [#15980](https://github.com/kirodotdev/KiroCrew/pull/15980)
builds a rejoin for it. Triage parked both because the change alters the
documented scratch lifecycle and asked for a one-pager first. This is that
document. Measured against main at `c6789799df`. Nothing here is built.

## Summary

| | Today | Proposed |
|---|---|---|
| What a tree belongs to | the processes that mount it (`.owner` pids) | a session key, recorded host-side |
| New process for the same chat after a gateway restart or resume | new empty tree | the chat's existing tree, same path |
| Reclaim starts | 1 h after every owner process group is dead and the tree is idle | when the session is closed, plus a retention window |
| Detached (`setsid`) job writing into scratch | holds the tree only while it keeps writing; an hour after its last write the tree goes | held while the session is open |
| Sessions with no close event (crons, incognito, `_bg`, warm spares, channel threads with no dashboard slot) | process rule | process rule, unchanged |
| Where a reclaimed tree goes | quarantine if it holds git, else deleted | the same, unchanged |

## Motivation

### Current state

- `allocate_scratch` makes `<data home>/scratch/<label>-<token8>` per spawned
  process and `record_owner` writes the child's pid to `.owner`
  (`src/kiro_crew/agent_scratch.py`).
- A process spawned on behalf of an existing session tree gets that tree as a
  second sandbox window, and `KIROCREW_SCRATCH` names it
  (`shared_scratch_window` and `scratch_env` in `agent_scratch.py`).
  That covers a dedicated subagent, a companion runtime, a recycled runtime's
  successor and a respawn on the same provider object
  (`_start_kiro_runtime_impl` in `providers/acp_runtime_start.py` reads
  `work_scratch_dir` off the previous client and later off the live runtime).
- The tree is NOT carried when a new provider is built for the same session
  key. A resume (`resume_sid` read from `_session_map` in `_get_or_create_impl`,
  `session_allocation_parts/get_or_create.py`) builds a new provider, and
  nothing tells it which tree the key had. The record that would say so does
  not exist on main: no index from session key to tree is persisted anywhere.
- The only reclaimer is the gateway's hourly maintenance loop
  (`_run_agent_scratch_sweep` in `slack/gateway_runtime/process_entry.py`,
  which calls `sweep_dead_scratch`). It deletes a tree when every pid in
  `.owner` names a dead process group and nothing in the tree changed for
  `_UNOWNED_GRACE_SECONDS` (3600 s). A tree holding a git repository is moved
  into `.quarantine` for 14 days instead (`QUARANTINE_TTL_SECONDS`).
- The subagent spec states the current contract on purpose: the tree is
  reclaimed once the gateway and its runtimes are gone, "and a gateway restart
  starts a fresh one" (`docs/system-specs/modules/subagent.md`, the shared
  scratch window section).
- The prompt tells agents the same thing from the other side: scratch is
  "reclaimed automatically once the session's processes are gone", and "state
  that must outlive your session's processes belongs in NEITHER" (the
  `$KIROCREW_SCRATCH` rule in `src/kiro_crew/config/prompt.md`). It names no
  place that does qualify.
- The durable `work/` root exists for state that must outlive a process, but
  it is masked from every sandboxed process and refused to the agent's file
  tools by design (`work_root.py` module docstring;
  `security/paths_lists.py`, the `"work"` entry).

### The problem

One operator's host recorded 22 incidents between 2026-09-01 and 2026-10-25
where a scratch tree was reclaimed or replaced while its chat still needed it.
They are listed in the acceptance table below. Every one has the same shape:
the chat continued, the process that owned the tree did not.

The fixes that landed in that window each narrowed one path:
[#12782](https://github.com/kirodotdev/KiroCrew/pull/12782) shared the tree with
subagents and recycled runtimes,
[#19897](https://github.com/kirodotdev/KiroCrew/pull/19897) quarantines trees
that hold git, and [#13290](https://github.com/kirodotdev/KiroCrew/pull/13290)
added `work/`, which agents cannot reach. None of them changes what a
restarted or resumed chat sees, and incidents of that kind continue through
2026-10-25. Quarantine saves clones but not logs, issue and PR bodies,
screenshots or a detached job's output, which is most of what was lost.

The workaround agents adopted is to write somewhere the sweep does not look:
a home-directory path or `/tmp`. #13290's own commit message names this ("with
nowhere sanctioned to go, such a job reaches for the system temp dir"). Those
paths are never cleaned, and on the same host `/tmp` ran out of inodes on
2026-10-28.

## Goals

1. A new process for a session that already has a tree mounts that tree, at
   the same path, whatever replaced the old process: gateway restart, resume
   after a backend error, a new process after the runtime idle reaper ended
   the old one while the tab stayed open, a turn interrupted and restarted.
   (The `slot_retention` auto-close is different: it archives the tab, and
   Goal 3 covers it.)
2. While a session is open, its tree is never reclaimed, whether or not any
   owner process is alive.
3. Reclaim starts when the session closes and runs after a retention window,
   so a chat revived from History inside the window finds its files.
4. Agents keep using `$KIROCREW_SCRATCH` as they do now. It becomes the
   durable per-session directory; no new variable and no new root.
5. A session can mount only its own tree. The record that ties a key to a
   tree cannot be written by any agent.

## Non-goals

- Handing files to another session, a cron, the operator or a pod. That stays
  the job of the outbox, `workspace/briefs` and `work/`.
- Keeping processes alive. A detached job killed by a process-group teardown
  stays killed; this RFC only keeps the directory it was writing to.
- Changing what quarantine keeps
  ([rfc-quarantine-unsaved-work-only.md](rfc-quarantine-unsaved-work-only.md)).
- Moving pytest test homes out of `/tmp`, or giving agents a sanctioned way to
  delete what they made. Both are separate changes.
- Making the scratch root configurable
  ([#11708](https://github.com/kirodotdev/KiroCrew/issues/11708)).

## Design

### The rule

A tree that a session key claims is kept while that session is open, and for
`SESSION_RETENTION_SECONDS` after it closes. A tree no session claims follows
today's process rule. Everything downstream of "this tree may be reclaimed"
(quarantine, the regenerable strip, expiry) is unchanged.

### The session index

The gateway keeps one host-side file, `<scratch root>/.sessions.json`, that
maps a session key to a tree basename plus a state:

```json
{"version": 1,
 "rows": {"dashboard:chat-12-1793000000": {"tree": "runtime-811554e9",
                                           "state": "open",
                                           "closed_at": null}}}
```

- Only gateway code writes it, off the event loop, as a temp file renamed
  into place, under an index lock of its own. Apart from the sweep's claim
  step (see Reclaim), it does not use `_SWEEP_LOCK`,
  which `agent_scratch.py` documents as held only across lstat, utime and the
  rmtree and never across marker I/O: index writes at session start, close,
  revive or delete must not wait behind the sweep deleting a large tree.
- Every row is keyed by one spelling: the provider's session key, the same
  string `get_or_create` resolves. Close, revive, delete and start each map
  the name they hold (slot name, history key, `effective_session_key`) to that
  key before they touch a row, so a revive published under a different slot
  name still finds its row.
- It lives in the scratch root, which the sandbox masks as a whole, so no
  spawned process can reach it. It is also added to the crew-home sensitive
  leaves in `security/paths_lists.py`, because the `scratch` leaf is
  deliberately reachable by the agent's file tools and the index must not be.
  #15980 makes the same two choices for its `.trees.json`.
- It is not kept in `session_map.json`. `security.md` documents that file as
  agent-writable, so a row there could be repointed at another session's tree.
- A row names a basename only, validated on every read as a plain directory
  directly under the scratch root, never a link, the same checks
  `shared_scratch_window` already makes.
- Reads are bounded and refuse links, like the `.owner` marker. A missing,
  unreadable or garbled index means "no session claims any tree". The sweep
  then falls back to today's process rule, which is the current behaviour and
  never deletes a tree with a live owner.
- Rows whose tree is gone are pruned on write.

### Which keys get a row

A row is written when a session's first process starts on a key that has a
close event, which on main means a dashboard slot. A channel conversation
with no slot (a Slack or Discord thread on its own) has no close event, so under the rule above its tree would never be
reclaimed; it stays on the process rule until such an event exists. Keys that
`get_or_create` already classifies as stateless (`background_key`,
`heartbeat_key`, `stateless_keys`, `stateless_prefixes`), incognito and
temporary sessions, script crons, and warm-pool spares that no session has
taken get no row and keep the process rule. A dedicated subagent or companion
runtime already joins its parent's tree through the existing window and adds
its pid to `.owner`; it gets no row of its own, because its tree is the
parent's.

### Rejoin

Wherever a provider is built for a key that has an `open` row, the gateway
passes the row's tree as `shared_scratch`. The existing window logic validates
it, refreshes its mtime and falls back to the process's own directory if the
tree is gone. That is one call site in `get_or_create` for the resume and
revive paths, which #15980 adds, plus the restart path on the provider
object, which already exists. A rejoin that falls back writes the new tree
into the row, so the next process joins the directory this one actually has.

A resume already skips the warm pool (`pool_decision = "bypass_resume"` in
`_get_or_create_impl`) and every root provider builds its own `AcpRuntime` in
`_start_kiro_runtime_impl`, so passing the tree adds no new cold start.

### Reclaim

The hourly sweep reads the index once per pass, then, for each tree:

1. Claimed by an `open` row: keep. No pid check, no idle check.
2. Claimed by a `closed` row with `closed_at` less than
   `SESSION_RETENTION_SECONDS` ago: keep.
3. Claimed by a `closed` row past the window, or by a `deleted` row: the
   tree is reclaimed in two steps, so a revive racing the sweep never loses
   it.
   - The full idle walk (`_tree_newest_mtime`) runs first, with no lock
     held, as the sweep does today.
   - The claim step runs under the index lock, then `_SWEEP_LOCK`, in that
     order. Nothing takes them in the other order. It re-reads the row from
     disk. If the row is no longer an expired `closed` or a `deleted` row,
     because a revive set it back to `open`, the tree is kept and the pass
     moves on. Otherwise it re-checks only the tree root's mtime, as the
     sweep's final look does today, renames the tree into `.reclaiming`, a
     holding directory under the scratch root that is distinct from
     `.quarantine`, and drops the row. Both locks are released. The claim
     step is the one index write made while `_SWEEP_LOCK` is held, and it
     does no walk or delete there.
   - The reclaim step then takes every entry in `.reclaiming` through
     today's path (quarantine if it holds git, else delete), skipping the pid
     check. It runs outside both locks, so a large delete never blocks a
     session start, close or revive. It drains the whole holding directory
     on every pass, so a tree left there by a crash between the two steps, a
     failed quarantine or a partial delete is retried on the next pass rather
     than leaked. The sweep's scan of the root skips `.reclaiming` the way it
     skips `.quarantine`.

   A revive takes the index lock to set its row back to `open`. If the claim
   step already dropped the row, the revive finds no row and starts a new
   tree, as any session whose tree was reclaimed does. A revive therefore
   either keeps its tree or comes after the claim step, never in between.
4. Not claimed: today's rule, unchanged.

The row's state changes at three existing points:

- Archiving a slot sets `closed` and stamps `closed_at`. On main two paths
  archive: `close_slot` in `dashboard/chat_api/slot_lifecycle.py` (the tab's
  close button, `session_close`, and the automatic close in
  `dashboard/slot_retention.py`), and `api_chat_slots_cleanup` in the same
  file, which archives in its own loop without calling `close_slot`. The state
  change goes in one archive step both paths call, so a later archive path
  cannot skip it.
- A revive from History sets `open` again and clears `closed_at`.
- History deletion (`dashboard/handlers/sessions_parts/delete_claim.py`) sets
  `deleted`.

`reset_conversation` and `/clear` keep the row. The tab and the session key
are the same, and the reset tool's own description tells the agent to write
anything it still needs to a file before the reset. Deleting that file at the
reset would contradict it. #15980 starts a fresh tree on a reset; this is the
one point where this design differs from it.

A forked session (`session_fork`) gets its own tree. Copying a tree is out of
scope, and sharing one would let two open sessions write the same files.

### Per-process temp stays per process

Today a root's first process uses one directory for both `TMPDIR` and
`$KIROCREW_SCRATCH`. Under this design that directory would keep every pytest
basetemp and `mktemp` file the first process ever made, for the life of the
chat. The root's first process therefore gets two directories: its own
process directory for `TMPDIR`, the kiro-cli log and temp files, swept by the
process rule as now; and the session tree for `$KIROCREW_SCRATCH`. Later
processes already get exactly this split. The session tree carries an
`.owner` marker like any other tree, and every process that mounts it adds
its pid through the existing `adopt_owner` path. Without the index the sweep
then judges the session tree by today's process rule. An unowned tree would
be kept forever instead, because the sweep never deletes a tree whose marker
is missing.

### Prompt and spec text

When the design ships, the `$KIROCREW_SCRATCH` rule in `config/prompt.md` changes from
"reclaimed automatically once the session's processes are gone" to say that
`$KIROCREW_SCRATCH` lasts as long as this chat and is reclaimed
`SESSION_RETENTION_SECONDS` after the chat closes. The subagent spec sentence
"a gateway restart starts a fresh one" changes in the same PR.

## Acceptance cases

The incidents come from one host's session transcripts, memory store and
briefs (2026-09-01 to 2026-10-25). Session keys and internal project names are
left out. "Cause" is what the session recorded at the time.

| # | Date | Lost or broken | Cause recorded | Case |
|---|---|---|---|---|
| 1 | 09-01 | One-shot installer driver | Scratch "rotated at midnight" | A6 |
| 2 | 09-08 | Review-comment script | Interrupted turn, new scratch | A2 |
| 3 | 09-10 | Test-run definitions and launch state; runs relaunched | Runtime changed, old tree reclaimed | A1 |
| 4 | 09-22 | Detached deploy process died; its log unreadable | 180-minute tool-call ceiling, tree reclaimed | A4 |
| 5 | 09-22 | PR body draft | Runtime reclaim | A1 |
| 6 | 09-22 | Review description stored as a scratch path; file gone | Reclaim after the fix subagent exited | A5 |
| 7 | 09-22 | Subagents could not read briefs; old review checkout gone | Sandbox mask plus reclaim | A5 |
| 8 | 09-22 | Review workspace, pulled again elsewhere | Reclaim | A3 |
| 9 | ~09-24 | Survey clone; second clone made in `/tmp` | Tree reclaimed while the survey ran | A4 |
| 10 | 09-30 | Scratch copy of a PR comment | Reclaim | A3 |
| 11 | 10-07 | Pre-deploy backup | "Deleted when the session ends" | A3 |
| 12 | 10-09 | `tsc` and `vitest` logs | Reclaim between wakes | A3 |
| 13 | 10-09 | Detached frontend shard logs, issue body | Wiped at turn end | A7 |
| 14 | 10-12 | Earlier process's test recording | Reclaim | A3 |
| 15 | 10-13 | Test log, issue and PR bodies | Runtime replaced at a restart | A1 |
| 16 | 10-13 | Issue body | Same restart | A1 |
| 17 | 10-14 | Clone and notes | Interrupted turn, "scratch dir was reset" | A2 |
| 18 | 10-18 | Detached gate runner killed before writing results | Resume with new scratch, old deleted | A2, A4 |
| 19 | 10-18 | Gate logs and shards, two sessions | Resume | A2 |
| 20 | 10-23 | Gate results; 61 gates rerun | Resume from sleep | A2 |
| 21 | 10-25 | Resolver chain halted mid-rebase | Scratch path changed between tool calls | A2 |
| 22 | 10-25 | CI shard log unreadable | Scratch changed after resume | A2 |

Each case is a test someone can run, on a pod or in the suite. Each one writes
a file to `$KIROCREW_SCRATCH` in a dashboard session, records the path, causes
the event, then checks on the next turn that `$KIROCREW_SCRATCH` prints the
same path and the file is there.

- A1 Gateway restart. Stop and start the gateway between two turns. Covers
  rows 3, 5, 15 and 16.
- A2 New provider on the same key. Force a transient backend reset, and,
  separately, a resume after an interrupted turn. Includes a reset in the
  middle of a turn: two tool calls on either side of it see the same path.
  Covers rows 2, 17 to 22.
- A3 No process for over an hour while the session is open. Let the runtime
  idle reaper end the session's process with the tab still open, wait past one sweep plus `_UNOWNED_GRACE_SECONDS`, send a
  message. Covers rows 8, 10 to 12 and 14.
- A4 Detached job. Start a `setsid` job that writes into scratch, end every
  owner process group, let the sweep run. The tree and the job's output are
  still there. Covers rows 4, 9 and 18.
- A5 Subagent after the parent's process changed. Stage a brief, restart the
  parent's process (A1 or A2), spawn a dedicated subagent, read the brief from
  it. Then let the subagent exit and read back a file it wrote. Covers rows 6
  and 7.
- A6 Runtime recycle. Recycle the runtime for age. Already covered by #12782;
  kept as a regression test because row 1 predates it.
- A7 Turn end. Row 13's cause is not explained by any reclaim path on main,
  which never runs at turn end. Phase 0 reproduces it or records that it was a
  different event; the case is a regression test either way.
- A8 Close and revive. Close the tab, revive it inside the window: same path,
  file present. Repeat with the revive published under a different slot name.
  Repeat with the session archived by the bulk cleanup route and by the
  `slot_retention` auto-close: each sets the row to `closed`. Close, advance
  the clock past the window, sweep: a tree without git is deleted, a tree
  with git is quarantined. A revive that lands after the sweep's first read
  of the index but before the claim step keeps the row and the tree; a test
  holds the sweep between the two steps to force that order.
- A9 History deletion. Delete the closed session from History; the next sweep
  hands the tree to the reclaim path regardless of the window.
- A10 Reset. `reset_conversation` on the slot keeps the path and the file.
- A11 Isolation. From inside a sandboxed session, the file tools and the shell
  cannot read or write `.sessions.json`. A row hand-written to name another
  tree, or a link, or a path outside the root, is refused and the session
  starts on its own tree.
- A12 Unkeyed sessions. A script cron, an incognito chat, the `_bg` session
  and a channel thread with no dashboard slot are reclaimed by the process
  rule exactly as today. With the index file removed, a session tree is
  reclaimed by the process rule too, and is not kept forever.

## Migration plan

Each phase is one PR and can stop after any phase.

**Phase 0, probe (no code change).** Run A1, A2, A3 and A7 on a pod at main
and record which fail and why in this document. Measure the scratch root's
size and tree count on a long-running host, which sizes the retention window.
Exit: each of A1, A2, A3 and A7 has a recorded verdict and the reason for it.

**Phase 1, rejoin.** The index, written at session start and read on every new
provider for a keyed session, plus the sensitive-leaf entry. This is the scope
#15980 already builds, except that a reset keeps the row. Exit: A1, A2, A5
and A11 pass; A12 is unchanged.

**Phase 2, reclaim by session state.** Close, revive and delete update the
row; the sweep applies the rule above; the root's first process gets a
separate `TMPDIR` directory. The prompt and subagent-spec text change here.
Exit: A3, A4, A8, A9 and A10 pass; A12 is unchanged; a process's temp files
are still reclaimed one hour after it dies.

**Phase 3, guidance.** Point the skills and docs that tell agents to keep
durable work outside scratch back at `$KIROCREW_SCRATCH`, and say where the
retention window is set. Exit: no skill shipped in this repository tells an
agent to put per-session work under `/tmp` or a home-directory path.

## Backward compatibility

- No index on disk means today's behaviour, so a downgrade or an unreadable
  index loses nothing that today's sweep would keep.
- Trees allocated before Phase 1 have no row and age out under the process
  rule.
- The visible change is that `$KIROCREW_SCRATCH` stays the same across a
  restart or resume, and that trees for open sessions stay on disk longer.

## Security considerations

- The index is the only new input that decides which directory a sandbox
  mounts. Only the gateway writes it, it sits under the sandbox mask, it is a
  sensitive leaf for the file tools, and every row is re-validated as a plain
  directory under the root before it is mounted.
- One tree is mounted only for one session key and the processes that already
  join it through the existing window (its subagents and companions). A fork
  gets a new tree.
- Session-keyed retention means more data stays on disk for longer. It stays
  under the data home at mode 0700, as today.

## Disk use

An open chat now keeps its tree for its whole life. A chat that is pinned and
never closed keeps it indefinitely. The existing regenerable strip
(`node_modules`, virtual environments, caches, pytest basetemps) runs only
when a git-holding tree moves into quarantine, which under this design is
after the retention window. Until then those directories stay in full. Phase 0's measurement decides whether open sessions also
need an idle bound (open question 2). Disk reporting for the scratch root
belongs with `kirocrew doctor`, not here.

## Alternatives considered

- **A longer grace window for dead-owner trees.** Simple, but the new process
  still gets a new path, so A1, A2 and A5 still fail; it only delays the
  deletion.
- **Store the tree in `session_map.json`.** Rejected: agent-writable, so one
  session could point its next resume at another session's tree.
- **Give agents a window into `work/`.** Rejected by `work_root`'s own design:
  its keys are guessable by construction, and no environment variable may name
  it.
- **Mount the session directory at a fixed path inside the sandbox.** Works
  with Linux bind mounts only; the macOS and Windows sandboxes have no
  equivalent, and the per-tree window already gives a stable path.
- **Keep telling agents to write durable work elsewhere.** This is what the
  incidents produced. Those paths are host-specific, never cleaned, and one of
  them is `/tmp`.

## Open questions

1. `SESSION_RETENTION_SECONDS`. Proposed: 7 days, the same idle window as
   `work_root.IDLE_GRACE_SECONDS`. A chat revived from History after the
   window starts a new tree.
2. Should an open session whose tree has had no write and no live owner for
   N days be treated as closed? Proposed: no bound in Phase 2, revisit with
   Phase 0's numbers.
3. A reset keeps the tree (above). Confirm, since #15980 chose the opposite.
4. Does a revive after an idle reap always go through the `resume_sid` path,
   or can it reach a fresh provider without one? Phase 0 answers it from A3.
