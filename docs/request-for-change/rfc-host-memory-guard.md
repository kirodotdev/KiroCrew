---
title: Host memory guard — the gateway stops the heaviest non-gateway tree at critical memory, and the conductor keeps the host healthy
status: implemented
author: zejiangg (drafted with an agent session)
created: 2026-11-18
last-audited: 2026-11-19
audited-at: fc89562d69
doc-pr: 39188
implementation-prs: [39270, 39289, 39755]
tracking-issues: [22681]
supersedes: []
superseded-by: []
---

# RFC: Host memory guard

Status: implemented. #39188 was approved and merged 2026-11-18 20:57Z; the
repository owner approved the design as proposed in the conductor chat on
2026-11-18. PR A (fast layer, `pressure_reclaim` in `session_memory_guard.py`,
`apply_guard_report` in `work_ledger_writes.py`) shipped in #39270 with the
follow-up #39755; PR B (per-worker view, posture-change wake, host-health rule)
shipped in #39289. Every code
reference below was read at `6bd6d5b3ce` (origin/main, 2026-11-18T18:04Z).
Code paths without a leading directory are under `src/kiro_crew/`. Code is cited
by symbol and file, as `scripts/docs_lint.py` requires; each was checked by grep
at that commit.

This document goes RFC-first, as its own pull request, because it changes a
recorded decision: the 2026-11-14 narrowing of
[`rfc-work-ledger-resource-pressure.md`](rfc-work-ledger-resource-pressure.md),
which [#37164](https://github.com/kirodotdev/KiroCrew/pull/37164) (`05aab93d27`,
merged 2026-11-15) implemented. It also extends what Crew may kill by default,
and the First Principles lane reads that decision from the base branch.

## 0. The owner's decision

The repository owner, 2026-11-18, in the conductor chat that dispatched this
document:

> When host memory is critical, Crew must free memory by stopping internal
> non-gateway process trees, heavy tests first, never the gateway; each stop
> leaves a log line and a message in the owning chat, and the conductor that
> dispatched that chat must be told.

The owner widened it the same day (~18:40Z): the conductor must actively manage
memory **and disk**, not only read `resource_status` to size a dispatch wave. It
reads resources on every patrol turn, sees memory and disk per child chat it
dispatched, and is woken when the host posture changes. The owner then narrowed
the conductor half (~18:45Z): resource management is not only for coding work
(a conductor may run research, documents, data, browser or eval work), so the
conductor guidance is **high-level and task-agnostic**: a goal, the facts it
gets, a few hard bounds, and the method left to the model. Concrete actions
appear only as labelled examples.

The owner is asked to restate this decision on the pull request, so it is on
record outside a private chat.

### What this changes in the 2026-11-14 ruling

The 2026-11-14 ruling ("开精简 PR，只留 critical 拒绝", quoted in full in
`rfc-work-ledger-resource-pressure.md`) kept only the critical `create`
refusal: `_refuse_if_host_critical` (`src/kiro_crew/work_ledger_writes.py`,
called from `_create_item`), code `host_memory_critical`
(`src/kiro_crew/work_ledger_records.py`). #37164 removed the
`host_pressure` record, the notifier's `pressure_sink` and level-change wake,
the pressure wake in `conductor_wake` and `WorkLedgerProbe`, and the conductor's
`host_pressure` prompt rule.

This RFC changes exactly two things in that ruling, one per layer:

| Layer | Change | Not changed |
|---|---|---|
| Fast (gateway guard) | **One event on the bound work item** when the guard stops one of that item's worker's trees (§4.4). | No `host_pressure` record. No new wake channel: the event is a `report`, which already wakes the conductor. |
| Slow (conductor) | **A conductor wake when the host posture changes** (memory or disk: ample, tight, critical), carrying the reading (§5.2). A short task-agnostic conductor rule (§5.4), and a per-child view in `resource_status` (§5.3). | No `host_pressure` record file. The `create` refusal stays as it is. The conductor is not told to pause by rule; pausing is one example among others. |

The slow layer reverses more of #37164 than the fast layer does: it brings back
a posture-change wake, which #37164 deleted. That is the owner's 2026-11-18
decision, quoted above.

## 1. Summary

Two layers, each acting at its own speed.

- **Fast layer, in the gateway.** When `ResourcePressureNotifier` reads
  `critical`, the cleanup watchdog runs a *pressure reclaim pass*: it picks the
  largest test-runner sub-tree under any Crew session host (else the largest
  other tool sub-tree above 512 MB), sends it SIGTERM, then SIGKILL after 5 s,
  one tree per pass, and re-reads memory before the next. It never touches the
  gateway, a session host process, an MCP server, or anything outside Crew's
  session trees. Each stop writes one log line, one notice in the owning chat,
  and, when that chat is a bound worker, one `report` event on its work item,
  which wakes the conductor. Linux only in the first PR; macOS and Windows log
  and notify but do not stop.
- **Slow layer, in the conductor.** The conductor reads `resource_status` at
  the start of every patrol turn. `resource_status` gains a block with each of
  its own workers' memory and the free space of the volumes they use. The
  gateway wakes the conductor once per posture change. A few lines in the
  conductor prompt state the goal (keep the host healthy so the gateway and
  every worker survive), the facts, and the hard bounds; the model picks the
  method.
- [#22683](https://github.com/kirodotdev/KiroCrew/pull/22683)
  (`oom_score_adj` 1000 on session hosts with no cgroup scope) stays as the
  kernel's last-resort layer below both.

## 2. Motivation

### 2.1 What ended the gateway on the dev host

Read from `~/.kiro/crew/gateway.log*`, `~/.kiro/crew/logs/gateway-boots/`,
`~/.kiro/crew/logs/crash-dumps/`, `last -x` and `journalctl --list-boots` on the
owner's host (alinux, kernel 5.10, 61 GB RAM, **no swap**, **cgroup v1**, PSI
present at `/proc/pressure/memory`). The kernel log is not readable as the
user, so no OOM-kill line could be checked.

| Gateway PID | Started | End |
|---|---|---|
| 9276 | loop-stall dump 2026-11-14T17:35:10Z, no `.clean` | journal silent after 19:25:32; host reboot 22:35 |
| 24035 | 2026-11-15 02:01:10, loop-stall dump 02:01:18Z, no `.clean` | journal silent after 02:01:30; host back 02:36:28 |
| 30571, 31864, 4995 | — | graceful shutdowns |

Both abrupt ends are **whole-host deaths** during heavy test runs, not the
kernel OOM killer picking the gateway: a swapless host with no cgroup ceiling
stops logging for ~35 minutes, then reboots. That is page-cache thrash, the case
`_doctor_memory_pressure` warns about
(`src/kiro_crew/doctor_checks/resources.py`). It is not proven without the
kernel log, but nothing points at a gateway-only kill. An `oom_score_adj` bias
only helps once the kernel OOM killer fires, and on this host it fired late or
never.

### 2.2 What main has, and why none of it stops a running test

| Guard | Where | What it does | Stops a running test tree? | Tells chat / conductor? |
|---|---|---|---|---|
| Spawn memory floor | `_spawn_memory_floor_and_cost`, `src/kiro_crew/subagent_parts/start_pricing.py` | refuses or queues new sub-agent starts | No | caller only |
| Work-ledger create refusal | `_refuse_if_host_critical`, `work_ledger_writes.py` | 409 `host_memory_critical` on new items | No | conductor sees the 409 |
| `ResourcePressureNotifier` | `src/kiro_crew/notifications/resource_pressure.py`; `observe`; `SAMPLE_INTERVAL_SECS = 30.0`; channel `system.resources` | tight / critical / recovery toasts | No | a human toast only |
| Cleanup watchdog RSS ceiling | `_rss_threshold_check`, `src/kiro_crew/session_cleanup.py`; default `DEFAULT_WATCHDOG_RSS_MAX_MB = 4096` (`config/sections_defaults.py`), key `session.watchdog_rss_max_mb` (`config/sections_session.py`) | recycles an **idle** session over a fixed per-tree ceiling (`reset(..., skip_if_busy=True)`) | No: a session running pytest is busy and is only reported (`report_busy_sessions`, `session_cleanup.py` → `session_cleanup_leftovers.py`) | chat notice (`on_leftover_memory`, `dashboard/state_parts/session_events.py`); conductor no |
| Mid-turn leftover end | `session.watchdog_end_leftovers_mid_turn` (`config/sections_session.py`), off by default; Linux only (`can_end_leftovers`) | ends processes that left the runtime's tree, when the session is over its ceiling | Only detached leftovers, only when opted in | chat notice |
| Tool OOM bias | `_bias_child_oom_score`, `src/kiro_crew/security/helpers.py` (called from the preexec `_set_limits`); `--oom-bias` added at `sandbox_rlimit.py`; `_bias_oom_score` in `_spawn_exec_shim.py` and `_process_group_supervisor.py` | `oom_score_adj = 1000` on tool children | Only if the kernel OOM killer fires | No |
| cgroup scope / agents slice | `acp/launch.py`; `sandbox_cgroup.py` (`_CGROUP_MEMORY_FRACTION`: `memory.max` = 65% RAM per scope; `_CGROUP_DEFAULT_SCOPE_MEMORY_HIGH_PCT`: `memory.high` 90%) | throttles, then the kernel OOM-kills inside one scope | Yes, by the kernel, inside the scope | slice OOM is read back into the notifier; **unavailable on cgroup v1** |

The gaps: nothing is pressure-driven and acts on a busy session; the notifier
samples every 30 s, too slow on a thrashing host; nothing tells the owning chat
*and* the conductor that work was stopped; and the conductor sees only its own
session's memory.

## 3. Goals and non-goals

Goals:

1. At critical host memory, free memory by stopping Crew's own heaviest
   non-gateway tree, tests first, before a swapless host thrashes.
2. Never stop the gateway, a session host, an MCP server, or any process
   outside Crew's session trees.
3. Every stop is visible: a log line, a chat notice in the owning chat, and an
   event on the bound work item that wakes the conductor.
4. The conductor sees the host's memory and disk and its own workers' share,
   on every patrol turn and on a posture change, and keeps the host healthy by
   whatever method fits the work.

Non-goals are in §9.

## 4. Fast layer: the gateway guard

### 4.1 Trigger

**Decision: reuse `ResourcePressureNotifier`'s posture. No new sampler or
daemon.** The notifier already runs on the event-loop heartbeat
(`dashboard/server_runtime/heartbeat.py`, every 5 s), already runs its probe
off the loop (`asyncio.to_thread` with a timeout below the heartbeat), and
already classifies `critical` as `MemAvailable` (or the cgroup-clamped reading)
at or below `agent.resource_critical_gb`, default 2 GB
(`_DEFAULT_CRITICAL_GB` and `_classify` in `resource_status.py`).

Three changes make it act before a swapless host thrashes:

1. **Sample faster while tight or critical.** The 30 s gate
   (`SAMPLE_INTERVAL_SECS`) drops to the heartbeat's 5 s while the last posture
   was `tight` or `critical`, and returns to 30 s on `ample`. Reason: the probe
   is a few `/proc` and `/sys` reads; at 30 s a host can go from tight to
   livelock between two samples. 5 s is the heartbeat's own floor, so this adds
   no timer.
2. **Read PSI as a second trigger on Linux.** While the posture is `tight`, a
   `/proc/pressure/memory` `full avg10` of 10 or more counts as critical for the
   guard only (not for toasts, the spawn gate or the 409). Reason: on a swapless
   host, reclaim stalls (the start of thrash) can begin while `MemAvailable`
   still reads above 2 GB, because `MemAvailable` counts page cache the kernel
   is already fighting to keep. Requiring `tight` as well keeps an IO-only PSI
   spike on an ample host from stopping anything. Main has no PSI reader today
   (`git grep 'pressure/memory'` finds nothing in `src/`); the reader is about
   15 lines in `platform_compat_metrics.py` beside `_linux_available_mib`, returns `None` when the file is missing (older kernels, macOS,
   Windows), and fails open.
3. **Act through a callback, off the event loop.** `observe` calls one
   in-process callback with the sample when the guard trigger holds. The
   callback schedules a single-flight `pressure_reclaim()` on the cleanup
   watchdog; it does not wait for the watchdog's own tick, which can be up to
   300 s (`MAX_TICK_INTERVAL_SECS`, `session_cleanup.py`). Every `/proc`
   walk and every signal runs on the maintenance executor, as the leftover
   path already does (`session_cleanup_leftovers.py`, `_end`). This callback is
   not the `pressure_sink` #37164 removed: it writes no record and wakes nobody.

The off switch is the existing one: `agent.resource_critical_gb = 0` disables
the critical posture at the source (`_classify`). The guard reads the same
threshold, so with it at `0` the guard is off on **both** triggers: the
critical posture never occurs, and the PSI trigger in point 2 is skipped too
(it only stands in for critical, so it is armed only while
`critical_gb > 0`). No new config key.

### 4.2 Victim choice

**Population.** Every live session runtime pid in the session manager's map,
the same map `_rss_threshold_check` reads, **without** its skip of persistent
and channel sessions: that skip protects a session from being *recycled*, and
this pass never recycles a session. For each runtime: its descendants
(`_iter_descendant_pids`, `acp/runtime_process_tree.py`) plus its detached
leftovers (`find_session_leftovers`, `session_pid_rss.py`), which keep the
runtime's session id.

**Never a victim**, checked on every pass:

- the gateway's own pid and anything that is not a descendant or leftover of a
  session runtime (processes in the user's own terminals are outside every
  session tree);
- every session runtime pid itself, the warm-pool spares (`_pool_pids`), the
  companion sub-agent runtimes (`_companion_runtime_pids`) and in-flight
  launches (`_in_flight_pids`), passed as `protected_pids`;
- any process whose basename is a managed agent runtime
  (`_MANAGED_AGENT_RUNTIME_BASENAMES`, `session_pid_harness.py`), so a
  nested harness is never mistaken for a tool;
- any sub-tree whose root matches the MCP predicates the orphan sweep already
  uses (`_is_orphan_mcp` and
  `_is_marked_mcp_launcher` in `session_pid_orphan_scan.py`).

**Tier 1, test runners.** A sub-tree whose root is the topmost process matching
`_work_sweep_cmdline_is_test_runner` (`session_pid_orphan_scan.py`: argv0
`pytest`, `-m pytest`, or the xdist bootstrap). Topmost means its parent does
not match, so a pytest run and its xdist workers are one tree, by ancestry, not
by name. Reason for reuse: the classifier is structural (never a substring), is
already trusted to pick kill targets in the orphan sweep, and covers the
workload that killed the host. Node runners (`vitest`, `jest`, `mocha`) are not
in it; they fall to tier 2 by size. Adding them to tier 1 is later tuning.

**Tier 2, other tool trees.** When no tier-1 tree exists, a sub-tree rooted at a
shell child of a session host (basename `sh`, `bash`, `zsh`, `dash`, `fish`,
`cmd.exe`, `powershell`, `pwsh`): the agent's shell tool. Reason: MCP servers
are exec'd directly (`npx`, `uvx`, `node`, `python`), never through a shell, so
requiring a shell root keeps a user-configured MCP server that matches no
marker out of reach.

**Ranking.** Within a tier, by tree RSS
(`_rss_tree_mb_for_pids`, `runtime_process_tree.py`). A tree under
**512 MB** is never picked in either tier: stopping it frees too little to be
worth the lost work. `shortcut: one fixed floor; make it a fraction of RAM if
small hosts show it never fires.`

**PID-reuse safety.** The candidate tree is captured with each pid's start id
(`_capture_child_records`, `runtime_process_tree.py`). Signals go through
`terminate_pids_pinned` (`platform_compat_kill.py`), which pins each target
with a pidfd and re-identifies it before signalling; a target that cannot be
pinned is never signalled by pid.

### 4.3 Stop

- **SIGTERM to the whole sub-tree, leaf first, then SIGKILL after a 5 s grace**,
  through `terminate_pids_pinned(targets, protected_pids=..., grace_secs=5.0)`.
  Reason for 5 s: pytest and node runners exit on SIGTERM within a second; a
  longer grace keeps memory held while the host is critical.
- **One tree per pass.** After a stop, the next pass runs on the next
  notifier sample (≤ 5 s) and only if the trigger still holds. Reason: one large
  test tree is usually the whole excess; stopping several at once kills work
  that did not need to die.
- **Per-session cool-down of 60 s.** After a stop in session S, S's trees are
  skipped while another session has a candidate. If S is the only session with
  a candidate, it is picked anyway: the host must not die to honour a
  cool-down.
- The session itself is never reset: its agent sees its command fail and
  carries on with the chat notice in front of it.

### 4.4 Tell

Each stop does three things, in the same pass. They ship in the same PR as the
kill, because a stop without them is the silent failure the owner ruled out.

1. **Log line**, `logger.warning`, one line:
   `Memory guard: stopped tier=test session=<key> root_pid=<pid> start=<start id> cmd=<basename> tree_rss_mb=<n> procs=<n> available_gb=<x> psi_full_avg10=<x|-> trigger=<critical|psi> signal=<TERM|KILL> freed=<yes|no>`.
2. **Chat notice** in the owning chat, through the existing
   `on_leftover_memory`-style callback (a sibling `on_memory_guard_stop`), worded
   in the chat's language through the usual i18n keys:
   "Host memory was critically low (1.4 GB free), so Crew stopped `pytest`
   (3.1 GB, 9 processes) that this chat started. The session keeps running.
   Retry with fewer parallel workers, or wait until memory frees."
   A chat that lives on a channel with no dashboard tab gets the same text in
   its channel conversation, through the governed legs the auto-compact notice uses.
3. **Work-ledger event** when the owning session is a bound worker
   (`read_binding`, `work_ledger_writes.py`): one `report` event on its
   item, written by the gateway through a new `apply_guard_report` beside
   `apply_worker_report` (`work_ledger_writes.py`), under the same
   `item_lock`.
   - `status: question`, `reason` unset. Reason for `question`: deciding how to
     re-plan (fewer workers, one heavy step at a time) is the conductor's
     decision, and `question` always wakes it, while a `blocked` right after a
     `blocked` is suppressed as a repeat (`repeats_status`,
     `ledger_wake.py`). `reason` stays unset because no person must act.
   - `summary`: `memory guard: stopped pytest (3.1 GB) at 1.4 GB free; the
     worker keeps running`.
   - It carries the item's current `artifacts`, `pr` and `discussion` forward
     unchanged, so the worker's pointers are not wiped (a plain
     `apply_worker_report` would replace them).
   - The worker's next own report replaces it, as any report does.
   - Nothing else is written: no `host_pressure` record, no new event kind, no
     new wake path. The conductor wakes through the existing report wake
     (`conductor_wake.py`, `_report_stamp` moves).

A stop in a session with no binding (a person's own chat) writes the log line
and the chat notice only.

### 4.5 Relation to #22683

[#22683](https://github.com/kirodotdev/KiroCrew/pull/22683) ("fix(sandbox):
OOM-bias session hosts that no cgroup scope bounds", read live 2026-11-18:
`OPEN`, `MERGEABLE`, head `483d4b683f`, landed by another chat) sets
`oom_score_adj = 1000` on session hosts with no cgroup scope. It stays as the
**kernel last-resort layer**. The guard acts earlier, at 2 GB free or at PSI
stall, while the kernel OOM killer acts only at exhaustion. With both, the
order on a host with no cgroup is: guard stops a test tree → if memory still
runs out, the kernel kills a biased session host or tool → the gateway (score
0) is last. Neither PR depends on the other.

### 4.6 Per OS

| Host | Today | After |
|---|---|---|
| Linux, cgroup v2 with delegation | the kernel OOM-kills inside one agent scope at 65% RAM; several scopes can still sum past RAM | same scope limit; the guard also runs at host level, and the log line names which layer acted |
| Linux, no cgroup (v1 or no delegation) | nothing acts until the kernel OOM killer, which may never fire before thrash | the guard stops the heaviest test tree at critical / PSI stall; #22683 stays below it |
| macOS | toasts only; no `/proc`, no `oom_score_adj` | the guard picks a victim the same way (RSS via libproc, `_get_rss_mb` in `runtime_process_tree.py`) and writes the log line and chat notice saying it could not stop the tree on this host; **no stop, no ledger event**. Reason: `terminate_pids_pinned` signals nothing off Linux, and a pid-only kill there has a reuse window |
| Windows | toasts only | same as macOS: log line and chat notice, no stop |

Linux-first is deliberate: it is the host that died. macOS and Windows stops
need a pinned tree kill there (`kill_process_tree_pinned` exists for Windows,
`platform_compat_kill.py`, with hard termination only) and are a later PR.

## 5. Slow layer: the conductor

### 5.1 When it reads resources

**Every patrol turn, plus a wake on posture change.** The conductor's rule says
to call `resource_status` at the start of every patrol turn that does work, not
only before a dispatch wave. A quiet patrol cycle under `watch="work-ledger"`
costs no turn today and still costs none: the rule applies only to a turn that
runs.

### 5.2 The posture-change wake

**Decision: the notifier, on a change of the combined posture, pulls each
watched conductor's patrol forward; the patrol tick emits one wake with the
reading.** The combined posture is the worse of memory (`ample` / `tight` /
`critical`, as today) and disk (§5.3: `tight` when any watched volume is low by
the existing `TempHeadroom.low` rule, `resource_status.py` `TEMP_FREE_PCT_WARN
= 10.0`, `TEMP_FREE_INODES_FLOOR = 1000`; `critical` under 3% space or 200
inodes).

- Delivery reuses `conductor_wake._fire` (`conductor_wake.py`) over
  `_watched_boards`, the path a worker report already uses; the tick's
  `WorkLedgerProbe` (`probes/work_ledger.py`) reads the **live** posture,
  not a record file, and emits one observation keyed `posture:<level>` per
  conductor, deduped against the last level it emitted for that conductor.
- The wake text names the reading: `[work-ledger wake] host posture is tight
  (memory 3.1 GB free; /tmp 6% free)`.
- Only boards with an active work-ledger patrol loop are watched
  (`_watched_boards`), so a conductor that armed no patrol is not woken.
- This is the wake #37164 removed, rebuilt without the `host_pressure` record:
  #36481 needed the record because its probe read a cross-process file; the
  probe runs in the gateway, so it reads the posture directly.

### 5.3 What it sees: a per-child view in `resource_status`

Today `resource_status` shows the host and the caller's own session: its
runtime, its sub-agent runtime and its runs, through `GET
/api/spawn/session-memory` (`api_spawn_session_memory` and
`caller_memory_view` in `dashboard/handlers/session_memory_scope.py`). A conductor's workers are separate chats, so it
sees none of them.

**Decision: that route, for a conductor caller, adds one `workers` list; the
tool renders it as a "Your workers" block.** One row per open item on the
caller's **own** board whose worker is bound (`read_binding`):

| Field | Meaning |
|---|---|
| `item_id` | the conductor's own item id (it already knows it) |
| `rss_mb`, `procs` | the worker session's tree, as the route measures its own row |
| `test_mb` | the largest tier-1 tree in it (§4.2), or 0 |
| `turn` | `busy` or `idle` |
| `volume_free_pct` | free space of the volume holding the worker's working directory |

Scope rule: only sessions bound to the caller's own items, which are its own
descendants. The route keeps its contract: counts, sizes and flags only; no
session key, title, path or pid of any session leaves it, and a session bound
to someone else's board is never listed. Host disk stays per volume: `/tmp`,
`$TMPDIR` (`temp_roots`, `resource_status.py`) and each worker's working
volume. **No per-child `du`**: walking build trees on every patrol would load
the same disk that is short. A conductor that needs a worker's own usage asks
the worker, which can measure its own tree.

### 5.4 What it does, and where the rule lives

The rule is a few lines of principle, task-agnostic, replacing the current
`resource_wave` rule (`agent_parts/spec_prompts.py`) and its mirror in the
goal-conductor skill (the "Capacity decides the wave" paragraph in
`src/kiro_crew/builtin_skills/goal-conductor/SKILL.md`). Proposed
text:

> **Keep the host healthy.** Your goal is that the gateway and every worker
> survive. Read `resource_status` at the start of every patrol turn; it shows
> host memory and disk and each of your workers' share, and you are woken when
> the posture changes. On `tight` or `critical`, act on what your workers are
> doing, by whatever method fits the work. Hard bounds: never stop or slow the
> gateway; never delete or overwrite anything outside your own workers' output
> without asking the owner; tell the affected worker, and say in your reply
> what you did. *Examples only, not a rulebook:* dispatch less or none, ask the
> heaviest worker to serialize or shrink a heavy step, close an idle finished
> worker chat (`session_close` archives), ask a worker to remove its own build
> output once its work is pushed.

What stays out of the rule, on purpose: per-task recipes (test worker counts,
browser tabs, dataset sizes), a fixed pause rule, and any number of the
conductor's own. The existing wave sizing by posture remains true but becomes
one example.

## 6. Implementation plan (round 2)

| PR | Layer | Contents | Exit criteria |
|---|---|---|---|
| A | fast | PSI reader; notifier fast sampling while tight/critical and the guard callback; `pressure_reclaim()` on the cleanup watchdog (victim choice, pinned stop, cool-down); log line; chat notice; `apply_guard_report`; macOS/Windows notice-only arm; i18n keys; this RFC's status → `in-progress` | tests in §8 pass; on a Linux host with no cgroup, a synthetic 3 GB pytest tree is stopped within 10 s of `critical`, its chat shows the notice, its bound item shows the `question` report, and its conductor wakes |
| B | slow | `workers` list on `/api/spawn/session-memory` and its rendering; disk posture in the notifier; posture-change wake through `_fire` and `WorkLedgerProbe`; the §5.4 rule in `spec_prompts.py`, `crewmate_prompts.json` and goal-conductor `SKILL.md` with the parity pin; this RFC's status → `implemented` when both land | tests in §8 pass; a conductor with two workers sees both rows; one posture change produces one wake per watched conductor and a repeat produces none |

Kill and notify ship together in PR A. PR B does not depend on PR A and may
land first. Both extend existing code (cleanup watchdog, notifier, session-memory
route, work-ledger probe); neither adds a daemon, a sampler, a record file or a
config key.

## 7. Backward compatibility and security

- No config key, persisted format or public API changes. The only new
  `report` writer is the gateway, through one function, on items it resolves
  from a worker's own binding.
- Kill authority widens: Crew may now stop a **running** tool tree in a busy
  session without the user opting in. It is bounded to descendants and
  leftovers of Crew session hosts, excludes session hosts, MCP servers and
  harnesses, signals only pinned identities, and is off when
  `agent.resource_critical_gb = 0`.
- The per-child view reveals nothing outside the caller's own board: sizes and
  flags keyed by its own item ids.
- `session.watchdog_end_leftovers_mid_turn` keeps its meaning: it governs the
  RSS-ceiling path, not the pressure path.

## 8. Tests the implementation must have

PR A:

1. Victim choice picks the largest tier-1 tree over a larger tier-2 tree, and
   the topmost pytest with its xdist workers as one tree.
2. A session host pid, a pool spare, a companion runtime, an MCP-marked root,
   a managed-harness basename and a non-session process are never picked, even
   when largest.
3. A tier-2 root that is not a shell is never picked; a tree under 512 MB is
   never picked.
4. A recycled pid (start id changed) is not signalled.
5. SIGTERM, then SIGKILL after the grace for a survivor; one tree per pass; the
   next pass re-reads memory and stops nothing once the trigger clears.
6. Cool-down skips a just-stopped session while another has a candidate, and
   picks it when it is the only one.
7. Trigger: `critical` fires; `tight` with PSI `full avg10 ≥ 10` fires; PSI
   alone on `ample` does not; a missing PSI file fails open;
   `resource_critical_gb = 0` disables the guard, including on a `tight`
   reading with PSI `full avg10 ≥ 10`.
8. Sampling drops to 5 s on tight/critical and returns to 30 s on ample.
9. The kill path does not run on the event loop (executor called; loop not
   blocked).
10. Each stop writes the log line with every field, one chat notice, and on a
    bound worker exactly one `report` event with `status: question`, no
    reason, and the item's prior `artifacts`, `pr` and `discussion` unchanged;
    an unbound session gets no ledger write; the conductor's report wake fires.
11. One test per platform arm: Linux stops; macOS and Windows write the log and
    notice and stop nothing and write no ledger event.

PR B:

12. The `workers` list holds only items on the caller's own board; a session
    bound to another board is absent; no key, title, path or pid is served.
13. One wake per conductor per posture change (memory and disk), none on a
    repeat, none for a board with no active work-ledger patrol.
14. Parity pin: the §5.4 rule text is identical in `spec_prompts.py`,
    `crewmate_prompts.json` and the goal-conductor skill.

## 9. Out of scope

- CPU or load average, CI queue depth, network.
- Creating cgroups on v1 hosts, installing `systemd-oomd` or `earlyoom`
  (doctor already advises).
- Stopping on macOS and Windows (a later PR, §4.6).
- Node test runners in tier 1 (later tuning, §4.2).
- Per-child disk usage by walking trees (§5.3).
- Any change to the idle RSS recycle, the spawn floor, or the `create` 409.
- A `host_pressure` record or any ledger record of host state.
- Automatic deletion of anything by the gateway: disk cleanup is the
  conductor's, through its own workers, inside the §5.4 bounds.

## 10. Alternatives considered

| Alternative | Why not |
|---|---|
| Rely on #22683 alone | it acts only when the kernel OOM killer fires; the observed failure is thrash where it fires late or never |
| A separate memory daemon or sampler | the notifier already samples off the loop on the heartbeat; a second sampler would disagree with it |
| Reset the whole session (the RSS-ceiling path) | loses the chat's in-flight turn and every other process in it; a sub-tree stop frees the same memory |
| Kill by largest process, any kind | can pick an MCP server, a harness or a dev server the user watches |
| Write the stop as a new event kind | needs a new wake path; the owner's bound is no new wake channel |
| `status: blocked` for the stop event | a `blocked` after a `blocked` is suppressed as a repeat, so the conductor could miss it |
| A per-task conductor rulebook (test workers, tabs, batch sizes) | the owner ruled the conductor guidance task-agnostic (2026-11-18 ~18:45Z) |

## 11. Open questions

None blocking. The thresholds (5 s grace, 512 MB floor, PSI 10, disk 3% /
200 inodes, 60 s cool-down) are first values to tune from the log line's data.
