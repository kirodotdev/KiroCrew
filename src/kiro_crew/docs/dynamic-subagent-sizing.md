# Dynamic Sub-Agent Max Count

Free memory, not a count, bounds how many sub-agents run at once. Each start is
admitted only if the host still has `agent.spawn_min_memory_gb` (2 GB) free
after it, counting what the starts already admitted will grow to (the *memory
floor*, below). The count cap is a high ceiling on top of that:
`agent.max_subagents = 0` (the default) means `agent.subagent_auto_max`, 32. On
a 16-32 GB host, memory runs out long before 32 sub-agents start, so the floor
is what you see. The ceiling stands in for limits the floor cannot see: your
LLM provider's concurrency, and the host's file-descriptor and process limits.

The count used to be sized from host memory as well, and an adaptive cap started
at 4 and halved whenever the gateway's event loop lagged or memory ran low.
Those were count limits sitting under the memory floor. With two or more chats
fanning out at once, one chat's wave held the slots the other chat's sub-agents
waited for while memory was still free. Now nothing but memory (and work that
keeps failing, below) holds a start back, and when memory runs short it is
shared between chats (*Several chats near the floor*, below).

## Enabling It

The auto ceiling is the default. To pin an explicit cap instead:

```
kirocrew config set agent.max_subagents 8
```

- `agent.max_subagents = 0` — **auto** (default): the ceiling is
  `agent.subagent_auto_max`.
- `agent.max_subagents >= 3` — explicit ceiling; adaptive control may run below it.

`max_subagents` accepts **0 (auto) or an integer >= 3**. A pin of 1 or 2 is
normalized UP to 3 (config loader, with a `config_bounds_clamped` SEL event) and
rejected by the dashboard API. `resolve_max_subagents` also floors any explicit
value at 3 as a runtime backstop.

The cap is re-resolved whenever `agent.max_subagents`,
`agent.subagent_auto_max` or `agent.spawn_min_memory_gb` changes in
`config.json`, and, while the spawn floor is off, whenever one of the memory
terms the cap is then sized from (`agent.subagent_mem_buffer_pct`,
`agent.subagent_cost_gb`, `session.pool_size`) changes: the running gateway
picks the new value up within a couple of seconds, so a change from the
dashboard, the CLI or an editor never needs a restart.

The adaptive controller ([adaptive-concurrency](https://github.com/kirodotdev/KiroCrew/blob/main/docs/system-specs/modules/adaptive-concurrency.md))
starts the running cap AT the ceiling and lowers it only when admitted work keeps
failing: attributable start timeouts, slow or failing MCP servers, file
descriptors or processes near their limit, or a low completion rate, two of them
agreeing in one sample. It never lowers it for event-loop lag or low memory. A
provider's rate limit (429) is handled per provider and never lowers the cap
either. After a cut, the cap climbs back one step per clean window, and fresh
stream progress from a long-running task can earn one extra slot without waiting
for the task to finish. The configured ceiling is never a command to start
unnecessary workers.

## How the Cap Is Computed

```
cap = agent.max_subagents            if it is > 0 (floored at 3)
cap = max(3, agent.subagent_auto_max) otherwise
cap = memory-sized (see below)       when agent.spawn_min_memory_gb <= 0
cap = 3                              when host memory cannot be read at all
```

- **No memory term, no CPU term.** Memory is judged start by start by the floor,
  which prices each start at what it will settle at; sizing the count from the
  same memory as well only stalled work while memory was still free. CPU
  over-commit only slows work down, and slow, failing work is exactly what the
  adaptive controller backs off on.
- **The unreadable-memory fallback.** On a platform with no memory probe, or
  when the read fails, the floor cannot bound starts (it admits rather than
  refusing everything forever), so the count is the only guard left and the cap
  falls back to the legacy 3.
- **The disabled-floor fallback.** `agent.spawn_min_memory_gb` at 0 or below
  turns the floor off, so it admits every start just as an unreadable host does.
  The count is then the only bound, and it is sized from host memory:
  `floor((avail_gb * buf - pool_size * typical - heavy_peak) / typical)`, with
  `buf = 1 - subagent_mem_buffer_pct / 100`, clamped to
  `[3, agent.subagent_auto_max]` -- the same figure the TaskRunner's auto value
  uses. `typical` and `heavy_peak` come from the learned cost store (below);
  before any are learned, `typical` is `agent.subagent_cost_gb` and nothing is
  reserved.
- **`subagent_auto_max`** — the ceiling itself (see "Why a hard cap" below).

The figure the model is told -- the `{{MAX_SUBAGENTS}}` prompt token and the
spawn tool's description -- is this cap (or the controller's cap in force), and
the spawn tool adds that each start also waits until host memory can hold it, so
a wide batch may start in waves.

## Learned Per-Agent Cost

Kiro Crew doesn't hard-code how much an agent costs — it measures it:

- While an agent runs, the reaper loop periodically samples its process-tree
  RSS (memory) and CPU, keeping the **high-water** mark for that run (a single
  reading at exit would miss a mid-run peak that has already declined), and,
  once it is up and idle, a **settled** reading of what the runtime holds before
  its work grows the tree.
- At exit, one sample `{agent, mem_gb, cpu_cores, ts}` (plus `settled_gb` when
  it was captured) is appended to `~/.kiro/crew/subagents/cost_samples.jsonl`.
  The CPU figure is telemetry only.
- The memory floor prices a dedicated start of an agent at its learned settled
  figure once three runs have measured it (below). The TaskRunner's auto
  parallel-step cap (`taskrunner.max_parallel_steps = 0`) is still sized from
  the whole-run figures, because no per-start floor prices a TaskRunner step;
  that is the one memory-sized count left (plus the disabled-floor fallback
  above). It prices every slot at a **typical** run -- the p50 of each agent's
  last N samples, then the median across agent names, each agent counted once
  -- so one build-heavy agent does not price every slot, and it **reserves the
  heaviest agent's p90 once** to cover that agent's growth after it starts.
  One reserve means several concurrent heavy runs are an accepted risk.

The sample log is bounded to the last N records per agent (FIFO compaction at
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
the learned cost. So the sampler special-cases them:

- **Shared** sub-agents attribute the runtime's measured RSS/CPU **divided by
  the number of concurrently-live shared sessions** on that PID — an empirical
  per-session *average share*, not a guessed constant.
- **Dedicated** (per-process) spawns keep the per-PID subtree sampling above.
  A spawn takes that path when it sets `model`, `reasoning_effort`,
  `allowed_tools`, `bare`, or `keep: true`; when `agent.role_models["subagent"]`
  or `agent.role_efforts["subagent"]` is pinned; when it runs as a crew member;
  when `agent.session_sharing` is off; when there is no parent session; or when
  the parent is not ACP/kiro-backed (e.g. a Claude-Code parent).

The practical effect: the memory floor prices a shared start at a fraction of a
dedicated one, so many more shared sub-agents fit before the floor holds the
next start.

## Why a Hard Cap

Every sub-agent calls the same upstream LLM provider under one account. The
provider's concurrency / rate limit is frequently the *real* bottleneck — a host
with room for 48 agents in RAM may only get useful throughput from a handful
before requests start queueing. Many sub-agents also mean many open files and
processes.

`agent.subagent_auto_max` (default **32**) is an honest ceiling for those
unmodeled limits. On most hosts memory binds below it. If you've confirmed your
provider serves more concurrency and the host has the memory, raise it. Kiro
Crew does **not** yet measure provider saturation beyond backing off on 429s per
provider — a deliberate simplification we may revisit.

The MCP gateway's backend spawn gate (`mcp_gateway.spawn_concurrency_max`, how
many MCP server processes fork and initialize at once) is raised to this ceiling
when it is lower, so a fan-out the cap admits is not queued behind eight
backend initializations. It still starts at `spawn_concurrency_initial` and
grows only while backends keep initializing cleanly.

## Configuration

| Key | Default | Effect |
|-----|---------|--------|
| `agent.max_subagents` | `0` | `0` = auto: the `subagent_auto_max` ceiling (default); `>0` = explicit cap |
| `agent.subagent_mem_buffer_pct` | `20` | % of memory reserved for the OS and other processes when sizing the TaskRunner's auto parallel-step cap; it sizes the sub-agent auto cap only while `spawn_min_memory_gb <= 0` disables the floor |
| `agent.subagent_cost_gb` | `0.5` | Minimum price of a warming dedicated start in the admission gate (the measured or learned projection applies when higher); also the TaskRunner figure's fallback (GB/agent) until a learned cost exists, and the sub-agent auto cap's while `spawn_min_memory_gb <= 0` |
| `agent.subagent_cpu_cost_cores` | `1.0` | **Deprecated, inert.** CPU no longer sizes anything; kept so an existing config is not rewritten |
| `agent.subagent_auto_max` | `32` | The sub-agent count ceiling when `max_subagents=0` (provider-concurrency and fd/PID stand-in); memory binds below it on most hosts. With the floor disabled it caps the memory-sized figure instead |
| `agent.spawn_min_memory_gb` | `2.0` | Free memory (GB) that must remain after admitting a start; a spawn that does not fit waits in the queue, never refused (one in temporary or incognito memory mode, or with `agent.task_queue_enabled` off, is lost if the gateway restarts), for at most `agent.subagent_queue_max_wait_secs` before it ends as `never started: waiting for memory`. `0` disables the gate, and the sub-agent auto cap is then sized from memory like the TaskRunner figure |
| `agent.subagent_spawn_stagger_secs` | `0.25` | Delay between successive spawns (initial fill and queued drain), so a high cap never bursts on cold start |
| `session.pool_size` | `0` | Warm-pool size; reserved in the TaskRunner figure's memory term when > 0 (and in the sub-agent auto cap's while the floor is disabled) |

The cap and `spawn_min_memory_gb` bound different things: the cap is a high
bound on the RUNNING population, while `spawn_min_memory_gb` is the real-time
floor on what each admitted start must leave free -- and it is the floor that
binds first on most hosts.

Three things bound a fan-out, and they bound different quantities. The cap
bounds how many agents RUN at once. `subagent_spawn_stagger_secs` bounds the
RATE at which starts are admitted -- one per interval -- and says nothing about
how many are still starting. `SubagentManager._startup_cap` bounds how many
admitted agents are IN STARTUP at once: past `_run_inner`'s first statement
(`_exec_started` set) but with no runtime PID, no answer on its own session
yet and no turn -- the same shape the startup watchdog reaps on. A durable-store
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
free a startup slot: a runtime PID or the first answer on the run's own session (`_note_startup_progress`)
and a terminal, including the watchdog's reap of a wedged start (the
slot-release drain), so a wedged population cannot hold the queue past the
reap.

The bound is tied to the session-start gate, not to the running cap:
`2 × session_start_concurrency` (`_STARTUP_CAP_GATE_ROUNDS` rounds of the
gate's width), clamped to `[1, cap]` (with the default `"auto"` width, the
floor of 2 until the gateway has sized it from the host just after boot),
because the gate is the one resource
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
either start path. A start can queue at three places: the session manager's
cold-start semaphore, the gateway's spawn admission, and the ACP
`SessionStartGate` (`agent.session_start_concurrency`, default `auto`: 2..16
sized from the host) around `session/new`.
Each fires two callbacks around its wait: `on_gate_queued` immediately before
the wait begins, and `on_gate_acquired` when the permit is granted, with the
wait and the queue's name. The manager's `_gate_wait_mark` stamps
`_gate_wait_started` on the first, and while that stamp is set the startup
watchdog reads the start clock as paused at that moment; `_gate_exit_reset`
clears the stamp and adds the wait to `_start_queue_wait_ms` on the second. So
the deadline measures time spent STARTING since execution began, minus the time
queued, never time queued behind other starts, however long the queues. The
dedicated-process path (`model` / `reasoning_effort` spawns) pauses at all three
queues, threading the pair through `get_or_create` -> provider factory ->
`AcpProvider` to its own process's spawn and `create_session`; the
session-shared path pauses at the gate, handing the pair to the parent
runtime's `create_session` directly, and, when it needs the parent's companion
runtime, at that runtime's per-parent lock and its spawn's admission (the
spawn's own work stays on the clock). The paused total is bounded: a start that
has spent more than `_START_QUEUE_MAX_SECS` queued without starting is ended as
"Never started: start queues saturated", because a queue's holders are not all
subagents a watchdog would reap.

The startup watchdog's deadline does not change with how many agents are in
startup. Its size comes from the start budgets: two handshake rounds (a
retried start runs two) of the spawn's `initialize` budget (90s) plus
`agent.session_start_timeout_secs`, plus the late-start collector's wait
(`agent.start_collect_timeout_secs` plus 5s) plus a 30s margin, never below
120s (695s at the defaults), read from the live config and fixed
per start clock (`SubagentManager._startup_deadline`;
`SubagentManager(startup_timeout=...)` pins it). It
is deliberately not pressure-aware -- no term per other agent in startup --
for two reasons. With queue time uncharged and the in-startup population
bounded there is no evidence that a healthy start misses the base deadline, so
a term would have nothing to correct. And a term sampled at sweep time against
`now - _exec_started - _start_queue_wait_ms`, which spans the whole crowded
period, would not be
monotonic: it would shrink as the crowd drained and could reap at one sweep an
agent the sweep before had left inside its window.
When the memory floor is enabled, admission also reserves memory for the next
start, for claimed starts awaiting registration, and for workers the reaper has
not measured twice yet, each in full at the price it was admitted at (a row no
sweep can measure, on macOS or Windows, settles two sweep intervals after its
session first answered); a settled worker owes nothing, because its memory is
already in the free-memory reading. A dedicated start is priced at what such a runtime
settles at: the learned settled RSS of its agent once three runs have measured
it (capped at 2 GB), else the measured default of about 1 GB, and never less than
`subagent_cost_gb`. A start that will share its parent's runtime skips the
process but still starts the agent's MCP servers, so it is priced at that less
about 0.35 GB. Parents waiting without a slot retain their reservation; a shared session
owes its price until it settles too, because its MCP servers start after it
binds. If the shared runtime turns out to be unavailable,
the start is re-priced as dedicated and the floor re-checked before the fallback
process starts. At defaults, a shared start needs about 2.65 GB free, the first
dedicated start 3.0 GB, a second while the first still warms 4.0 GB.

A nested sub-agent that will share its parent's runtime is the exception. Its
parent already holds its memory (a parent waiting on its children keeps it), so
waiting on memory for that child could only end at the 30-minute limit. Such a
child is admitted with nothing owed for itself: at 2.0 GB free it starts, which
can take the host below the floor by its shared price. It still counts at that
price while it warms, so a second one needs 2.65 GB.

### Several chats near the floor

A start that waits for memory is re-checked as soon as memory can have moved:
when one of the gateway's sub-agents finishes and, only while something waits,
on a short sampler that reads free memory every few seconds, so memory another
program frees, or a warming sub-agent that has settled, is noticed too. Each such check reads
free memory once and starts only what now fits; a start that still does not fit
is left waiting with nothing written. The fixed 30-second re-check remains as a
backstop (after a restart, for example).

Memory is shared between chats. While one chat's sub-agents wait for memory, a
chat that already runs its share of dedicated sub-agents does not get the next
start that memory allows; the waiting chat does. Shares follow
`agent.lane_weights` (each chat weighs 1 unless listed), and a chat's nested
sub-agents are never held back by it, so a running tree can always finish. This
is not a count cap: with one chat alone, or with memory to spare, nothing
changes.

A start is never priced at a whole-run peak or a whole-tree p90: those measure
the whole process subtree, including the test suites and builds a run launched,
not what a runtime holds once it is up.

On macOS the floor also reads the kernel's memory-pressure level
(`kern.memorystatus_vm_pressure_level`). While it is WARN or CRITICAL and a
dedicated subagent of this gateway is running, a start waits even when the
reclaimable figure clears the floor, for at most 30 minutes, after which it ends
without starting; with none running
it is judged on the figure alone. The level lags (it can read NORMAL on a Mac
that is already swapping), so it backs the figure up rather than replacing it.
It never changes the count cap.

What the floor guarantees, stated plainly: admission never takes the host below
the floor, except that a nested sub-agent sharing its parent's runtime can take it
below by that one start's shared price (above). It does not shed running work: a settled sub-agent that later runs a
build or a test suite, or another application, can still push free memory below
it, and then new starts wait. On Windows the floor reads available physical
memory, not the commit charge, so a host near its commit limit can admit a
start that later fails to commit (an accepted gap, tracked as
[#16983](https://github.com/kirodotdev/KiroCrew/issues/16983)).

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
  not to the configured ceiling: the floor cannot bound starts it cannot measure.
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
- Usage there means the working set: `memory.current` (v1:
  `memory.usage_in_bytes`) minus the group's inactive page cache
  (`inactive_file`, v1 `total_inactive_file`), so reclaimable cache does not
  read as used. The agents slice (`kirocrew-agents.slice`) is a second clamp:
  its headroom is the smaller of `memory.high` and `memory.max`, minus the
  slice's own working set.
- Design rationale and worked examples:
  [`docs/system-specs/modules/subagent.md`](https://github.com/kirodotdev/KiroCrew/blob/main/docs/system-specs/modules/subagent.md).
