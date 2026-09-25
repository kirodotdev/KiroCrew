# Dynamic Sub-Agent Max Count

Kiro Crew sizes the concurrent sub-agent cap **automatically** by default
(`agent.max_subagents = 0`): at gateway startup it computes a sensible cap from
the host's actual memory, plus a per-agent memory cost Kiro Crew *learns* from
past runs. A fixed number is wrong in both directions — it wastes capacity on a
large host and over-commits a tiny one — so auto is the default; set an
integer >= 3 to pin an explicit cap.

## Enabling It

Auto-sizing is the default. To pin an explicit cap instead:

```
kirocrew config set agent.max_subagents 8
```

- `agent.max_subagents = 0` — **auto** (default): compute the cap at startup.
- `agent.max_subagents >= 3` — explicit ceiling; adaptive control may run below it.

`max_subagents` accepts **0 (auto) or an integer >= 3**. A pin of 1 or 2 would
silently disable auto-sizing *and* run below today's default of 3, so it is
normalized UP to 3 (config loader, with a `config_bounds_clamped` SEL event) and
rejected by the dashboard API. `resolve_max_subagents` also floors any explicit
value at 3 as a runtime backstop. `0` is the only way to request the host-safe
auto cap.

The cap is re-resolved whenever `agent.max_subagents` changes in `config.json`:
the running gateway picks the new value up within a couple of seconds, so a
change from the dashboard, the CLI or an editor never needs a restart. The
host-safe auto cap (`0`) is measured when the value is resolved -- at boot and
again on each such change -- not on a timer, so after the host's resources
change it is re-measured by the next subagent-setting edit or a restart.

For long-running work, new provider/tool stream activity can earn one additional
slot after a clear observation window, without waiting for the task to finish.
This probe requires queued work, free memory above the pressure line and no
provider throttle. An unchanged activity timestamp, a queued/stalled/parked run
or an unreadable memory probe cannot earn it. Successful completions still earn faster startup
doubling; after pressure, growth remains bounded to one slot per clean window.
The configured ceiling is never a command to start unnecessary workers.

The configured ceiling is the growth bound. The adaptive controller climbs
toward it on live pressure signals (free memory against the pressure line, loop
lag, timeouts) and never against a number predicted from past peaks: many
sessions may ask for many workers, the controller admits them up to the ceiling
you chose, and what the host cannot absorb yet queues -- at the per-spawn memory
gate below and in the controller's own back-off -- rather than being refused
for a guessed cap. An explicit ceiling such as 64 is not clamped by the
auto-sizing-only `subagent_auto_max`.

## How the Cap Is Computed

```
buf      = 1 - subagent_mem_buffer_pct / 100
mem_term = floor( (avail_gb * buf - pool_size * mem_cost) / mem_cost )
cap      = clamp( mem_term, 3, hard_cap )
```

- **Memory term** — how many agents fit in available RAM after reserving a
  buffer for the OS and other processes, and after holding back one worker's
  cost per warm-pool slot. `avail_gb` comes from `_available_memory_gb()`,
  which on Linux is `min(MemAvailable, cgroup headroom)` so a memory-capped
  container is respected.
- **No CPU term** — deliberately. Over-committing memory ends in the OOM
  killer, an unrecoverable hard failure, so it is sized up front. Over-committing
  CPU only slows work down, and the adaptive controller already backs off on the
  pressure that slowness produces. A static CPU term stacked on that loop did
  the opposite of what it promised: agents are mostly I/O-bound, but the term
  was priced from each agent's one-minute *peak*, so a single build-heavy run
  (20 cores for a minute) priced every slot at that burst and pinned a 32-core
  host with 96 GB free at 4.
- **Floor of 3** — the auto-sized cap never drops below the legacy default
  (`_LEGACY_DEFAULT_MAX`), so enabling auto can't regress a small host. This is
  a hard floor: `compute_max_subagents` clamps to `[3, hard_cap]`, and the
  config loader clamps `subagent_auto_max` itself UP to 3 (with a warning) if a
  file sets it lower. The per-spawn memory gate (`agent.spawn_min_memory_gb`)
  still refuses individual spawns under real memory pressure.
- **`hard_cap`** — an absolute ceiling (see "Why a hard cap" below).

## Learned Per-Agent Cost

Kiro Crew doesn't hard-code how much an agent costs — it measures it:

- While an agent runs, the reaper loop periodically samples its process-tree
  RSS (memory) and CPU, keeping the **high-water** mark for that run (a single
  reading at exit would miss a mid-run peak that has already declined).
- At exit, one sample `{agent, mem_gb, cpu_cores, ts}` is appended to
  `~/.kiro/crew/subagents/cost_samples.jsonl`. The CPU figure is telemetry
  only; sizing reads `mem_gb`.
- At the next startup, Kiro Crew takes the **p90 of the last N memory samples
  per agent name** (robust to the occasional outlier run), then the worst case
  across agent types, as the divisor.

The longer the gateway runs, the more accurate the learned cost becomes. The
sample log is bounded to the last N records per agent (FIFO compaction at
startup and periodically at runtime), so it never grows without limit. Before
enough samples accumulate, a conservative fallback is used
(`agent.subagent_cost_gb`).

### Session-shared sub-agents (AcpRuntime)

With `agent.session_sharing = True` (the default for the kiro-cli backend), an
eligible sub-agent does **not** spawn its own process — it runs as an extra
session inside the parent's shared **AcpRuntime** (one process hosts
everything). Its true incremental cost is small and roughly constant, not the
whole process.

Because every sharing sub-agent reports the **same** runtime PID, naive per-PID
sampling would charge the entire shared process to *each* of them and inflate
the learned cost — pinning the cap to the floor of 3, the opposite of what we
want now that shared sub-agents are cheap. So the sampler special-cases them:

- **Shared** sub-agents attribute the runtime's measured RSS/CPU **divided by
  the number of concurrently-live shared sessions** on that PID — an empirical
  per-session *average share*, not a guessed constant. As concurrency rises the
  per-agent share falls, so the learned cost tracks reality.
- **Dedicated** (per-process) spawns keep the per-PID subtree sampling above.
  A spawn takes that path when it sets `model`, `reasoning_effort`,
  `allowed_tools`, `bare`, or `keep: true`; when `agent.session_sharing` is
  off; when there is no parent session; or when the parent is not
  ACP/kiro-backed (e.g. a Claude-Code parent).

The practical effect: for the common session-shared case the memory term no
longer binds, so the cap rises to the **provider-concurrency ceiling**
(`agent.subagent_auto_max`) rather than host RAM — which is the real constraint
when N sessions share one process calling one upstream account.

## Why a Hard Cap

The formula sizes for **local** resources, but every sub-agent calls the same
upstream LLM provider under one account. The provider's concurrency / rate
limit is frequently the *real* bottleneck — a host that fits 48 agents in RAM
may only get useful throughput from a handful before requests start queueing.

`agent.subagent_auto_max` (default **32**) is an honest ceiling for that
unmodeled limit. On a big host the hard cap binds; on a small host memory
binds below it. If you've confirmed your provider serves more concurrency,
raise it. Kiro Crew does **not** yet measure provider saturation — that's a
deliberate v1 simplification we may revisit.

## Configuration

| Key | Default | Effect |
|-----|---------|--------|
| `agent.max_subagents` | `0` | `0` = auto-size (default); `>0` = explicit cap |
| `agent.subagent_mem_buffer_pct` | `20` | % of memory reserved for the OS and other processes |
| `agent.subagent_cost_gb` | `0.5` | First-boot memory-cost fallback (GB/agent) until learned |
| `agent.subagent_cpu_cost_cores` | `1.0` | **Deprecated, inert.** CPU no longer sizes the cap; kept so an existing config is not rewritten |
| `agent.subagent_auto_max` | `32` | Absolute ceiling on the computed cap (provider-concurrency stand-in) |
| `agent.spawn_min_memory_gb` | `4.0` | Per-spawn admission gate (separate runtime guard, refuses a spawn when free memory is low) |
| `agent.subagent_spawn_stagger_secs` | `0.25` | Delay between successive spawns (initial fill and queued drain), so a high cap never bursts on cold start |
| `session.pool_size` | `0` | Warm-pool size; reserved in the memory term when > 0 |

The cap interacts with `spawn_min_memory_gb` but does not replace it: the cap is
a bound on the RUNNING population, while `spawn_min_memory_gb` is a real-time
per-spawn memory floor. They are independent guards.

Three things bound a fan-out, and they bound different quantities. The cap
bounds how many agents RUN at once. `subagent_spawn_stagger_secs` bounds the
RATE at which starts are admitted -- one per interval -- and says nothing about
how many are still starting. `SubagentManager._startup_cap` bounds how many
admitted agents are IN STARTUP at once: past `_run_inner`'s first statement
(`_exec_started` set) but with no runtime PID, no first provider stream and no
turn -- the same shape the startup watchdog reaps on. A durable-store
reservation not yet registered as an agent is counted in its place, since its
re-entry skips the admission gate. An agent PARKED at the spawn-approval prompt
is deliberately NOT counted: it is starting nothing, and counting it would let a
handful of unanswered prompts hold every other spawn on the host, auto-approved
ones from unrelated parents included. What has to be bounded is its RELEASE,
because a bulk trust / yolo grant resolves every pending prompt in one pass: a
released start re-enters through the pump (`_admit_released_start`, a resident
`_startup_release` entry in the existing queue) and is metered into startup by
the same stagger and in-startup checks a fresh spawn passes, one per pass,
ahead of the capacity check (it already holds its slot) and of the fresh
spawns behind it (it was admitted first). While it waits it is registered,
holds its running slot and shows in its parent's queue depth; it joins
`_startup_population` only when the pump releases it. Without the third bound, one start is admitted
every interval however long each start takes; when each start is slow (a
dedicated process per `model` / `reasoning_effort` override, a queue at the
session-start gate, a throttled provider handshake) dozens sit in startup
together, all contending for the same gate and all running down the same
startup deadline. Measured on a 623-item fan-out: waves of 24-45
items lost ~2%, waves of 50-60 lost 2-16%, and a wave of 120 lost ~50% -- every
loss a healthy start reaped as `Failed to start within 120s`, and every retry of
one deepening the crowd that caused it. The bound holds further spawns in the
EXISTING queue (`_should_stagger_queue_impl` gains a third clause; the drain
pump holds its pick under the same test) and the queue wakes on the edges that
free a startup slot: a runtime PID or a first stream (`_note_startup_progress`)
and a terminal, including the watchdog's reap of a wedged start (the
slot-release drain), so a wedged population cannot hold the queue past the
reap.

The bound is tied to the session-start gate, not to the running cap:
`2 × session_start_concurrency` (`_STARTUP_CAP_GATE_ROUNDS` rounds of the
gate's width), clamped to `[1, cap]`, because the gate is the one resource
every start in startup contends for: `session/new` runs under `G` permits, so
at most `G` starts make progress at any moment, and every other admitted start
is a spawned process (dedicated path) or a claimed slot holding nothing but a
place in the gate's queue. Time in that queue is not charged to the startup
deadline (next paragraph), so the queue's length is not what reaps a healthy
start; what the bound decides is how much of the running cap may sit in
startup contending for `G` permits at once. `2G` is the smallest value that
never idles the gate -- one round holding permits and one round already
admitted to take them the moment they free -- and admitting more buys no
starts, since the gate serves `G` per round however many are queued: it only
lengthens the queue and grows the population of admitted-but-idle starts. A
cap-derived term -- `max(2 × G, ceil(cap / 4))`, say -- would do exactly that:
at a cap of 64 it admits 16 into startup against a 2-permit gate, seven rounds
queued for two permits; that is why the bound is tied to the gate and never to
the cap. At the default gate width of 2 the bound is `4` at any cap of 4 or
more (cap 8, 40 and 64 alike), `cap` below that, and `1` at a cap of `0` (the
running cap, not this bound, pauses admission there). Admission throughput is
unchanged by the bound: the gate serves `G` starts per round regardless of how
many are queued behind it.

There is no config key for this bound, on purpose. `2G` is both the floor and
the ceiling of the useful range -- below it the gate idles, above it only a
longer queue of idle admitted starts accrues -- so a knob could only move the
value somewhere worse, and the operator's real lever already exists:
`agent.session_start_concurrency` sizes the gate, and the bound tracks it.

Time spent WAITING FOR A PERMIT is not charged to the startup deadline, on
either start path. `runtime.create_session` runs under the ACP
`SessionStartGate` (`agent.session_start_concurrency`, default 2) and fires two
callbacks around the wait: `on_gate_queued` immediately before the wait for a
permit begins, and `on_gate_acquired` at gate exit with the queue wait. The
manager's `_gate_wait_mark` stamps `_gate_wait_started` on the first, and while
that stamp is set the startup watchdog reads the start clock as frozen at that
moment; `_gate_exit_reset` clears the stamp and restarts the clock on the
second. So the deadline measures time spent STARTING -- before the gate (a
process spawn on the dedicated path) and with a permit held (`session/new`) --
and never time queued behind other starts, however long the queue. The wait is
finite without a deadline of its own: every permit holder is on a running clock
from acquisition and is reaped at the base deadline if its `session/new` has
not returned, the request has its own budget (`agent.session_start_timeout_secs`),
and the gate keeps a headroom of permits no late-start collector may hold. Both
start paths install the same pair: the session-shared one hands them to the
parent runtime's `create_session` directly, and the dedicated-process one
(`model` / `reasoning_effort` spawns) threads them through `get_or_create` ->
provider factory -> `AcpProvider` to its own process's `create_session`.

The startup watchdog's deadline itself stays fixed (`120s`,
`SubagentManager(startup_timeout=...)`) however many agents are in startup. It
is deliberately not pressure-aware -- no term per other agent in startup --
for two reasons. With queue time uncharged and the in-startup population
bounded there is no evidence that a healthy start misses the base deadline, so
a term would have nothing to correct. And a term sampled at sweep time against
`now - _exec_started`, which spans the whole crowded period, would not be
monotonic: it would shrink as the crowd drained and could reap at one sweep an
agent the sweep before had left inside its window.
When the memory floor is enabled, admission also reserves memory for the next
start, for claimed starts awaiting registration, and for live dedicated workers.
A start that has not settled yet -- fewer than two reaper sweeps have measured
it -- is priced at the learned p90 from `cost_samples.jsonl` for the agent being
spawned (the named agent, or the template an agent-less spawn inherits -- the
same key its own samples are recorded under; only that agent's own history counts,
only from runs that ran as their own process, and only samples younger than 30
days, since a session-shared run's figure is a per-session share and a price
learned under a removed workload must be able to expire), never less than
`subagent_cost_gb`, less whatever RSS it already holds;
so the reserve prices it at what runs on this host have actually cost rather than
at the first-boot fallback (the reaper refreshes those figures off the event
loop, so they can lag a new sample by up to one sweep). A settled worker owes only
the gap between the larger of `subagent_cost_gb` and its own peak and what it
holds now, so observed memory is never counted twice and a learned cost above
what a particular worker needed does not hold memory it will never use. Parents waiting without a slot retain their reservation; confirmed
shared sessions do not add a dedicated-process cost. This lets short spawn
intervals fill available capacity without spending the same headroom repeatedly
while processes warm up. It cannot predict allocations beyond the estimated cost.
A deferral for low memory states the per-start price it used; if a learned cost
no longer reflects this host, delete `subagents/cost_samples.jsonl` under the
data home (or lower `spawn_min_memory_gb`) and it re-learns from the next runs.

## Notes

- Stdlib only — no new dependencies. Memory/CPU are read per platform:
  Linux reads `/proc/meminfo`, `/proc/<pid>/stat`, and cgroup limits; macOS
  reads *available* memory in-process via the Mach `host_statistics64` syscall
  through `ctypes`/`libSystem` (free + inactive + speculative + purgeable
  pages × page size) — no subprocess, so it is safe on the gateway event loop
  and passes the spawn-audit guard; Windows reads available memory via
  `GlobalMemoryStatusEx` (through `platform_compat.host_available_mib`) and has
  no cgroup clamp.
- On a platform with no probe yet, or with no usable memory bound, the memory reader
  fails open and the cap falls back to the floor of 3 (`_LEGACY_DEFAULT_MAX`),
  not to the configured value.
  The per-spawn memory guard uses the native reader on macOS and Windows, and
  also respects Linux cgroup headroom even if the host memory read fails.
- Linux cgroup headroom uses the process's memory-controller membership and
  mount mapping, including nested systemd/container groups. The tightest
  headroom at the group or a visible ancestor binds, accounting for siblings
  in each parent's usage. A finite limit with unreadable or invalid usage
  contributes zero headroom because spare capacity cannot be established;
  measured zero usage retains the full limit. Missing or unlimited limits
  leave the host-memory fallback intact. Ancestors hidden above the cgroup
  mount cannot be measured.
- Design rationale and worked examples:
  [`docs/system-specs/modules/subagent.md`](https://github.com/kirodotdev/KiroCrew/blob/main/docs/system-specs/modules/subagent.md).
