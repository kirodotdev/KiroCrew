# Dynamic Sub-Agent Max Count

Kiro Crew sizes the concurrent sub-agent cap **automatically** by default
(`agent.max_subagents = 0`): at gateway startup it computes a sensible cap from
the host's actual memory and CPU, plus a per-agent cost Kiro Crew *learns* from
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
This probe requires queued work, measured host headroom and no provider throttle.
An unchanged activity timestamp, a queued/stalled/parked run or an unreadable
host probe cannot earn it. Successful completions still earn faster startup
doubling; after pressure, growth remains bounded to one slot per clean window.
The configured ceiling is never a command to start unnecessary workers.

The configured ceiling and live growth bound are separate. The controller
refreshes host headroom while running: memory headroom buys additional slots,
so existing resident workers are added back to that memory term; CPU capacity
is already a total. An explicit ceiling such as 64 is not clamped by the
auto-sizing-only `subagent_auto_max`.

## How the Cap Is Computed

```
buf      = 1 - subagent_mem_buffer_pct / 100
mem_term = floor( (avail_gb * buf - pool_size * mem_cost) / mem_cost )
cpu_term = floor( (cpu_count * buf) / cpu_cost )
cap      = clamp( min(mem_term, cpu_term), 3, hard_cap )
```

- **Memory term** — how many agents fit in available RAM after reserving a
  buffer for the OS and other processes, and after holding back one worker's
  cost per warm-pool slot. `avail_gb` comes from `_available_memory_gb()`,
  which on Linux is `min(MemAvailable, cgroup headroom)` so a memory-capped
  container is respected.
- **CPU term** — how many fit in the core budget, using a measured per-agent
  CPU cost (agents are mostly I/O-bound, so this is generous).
- **`min(...)`** — the tighter of memory/CPU wins.
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
  `~/.kiro/crew/subagents/cost_samples.jsonl`.
- At the next startup, Kiro Crew takes the **p90 of the last N samples per
  agent name** (robust to the occasional outlier run), then the worst case
  across agent types, as the divisor.

The longer the gateway runs, the more accurate the learned cost becomes. The
sample log is bounded to the last N records per agent (FIFO compaction at
startup and periodically at runtime), so it never grows without limit. Before
enough samples accumulate, a conservative fallback is used
(`agent.subagent_cost_gb`, `agent.subagent_cpu_cost_cores`).

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
unmodeled limit. On a big host the hard cap binds; on a small host memory or
CPU binds below it. If you've confirmed your provider serves more concurrency,
raise it. Kiro Crew does **not** yet measure provider saturation — that's a
deliberate v1 simplification we may revisit.

## Configuration

| Key | Default | Effect |
|-----|---------|--------|
| `agent.max_subagents` | `0` | `0` = auto-size (default); `>0` = explicit cap |
| `agent.subagent_mem_buffer_pct` | `20` | % of memory/CPU reserved for the OS and other processes |
| `agent.subagent_cost_gb` | `0.5` | First-boot memory-cost fallback (GB/agent) until learned |
| `agent.subagent_cpu_cost_cores` | `1.0` | First-boot CPU-cost fallback (cores/agent) until learned |
| `agent.subagent_auto_max` | `32` | Absolute ceiling on the computed cap (provider-concurrency stand-in) |
| `agent.spawn_min_memory_gb` | `4.0` | Per-spawn admission gate (separate runtime guard, refuses a spawn when free memory is low) |
| `agent.subagent_spawn_stagger_secs` | `0.25` | Delay between successive spawns (initial fill and queued drain), so a high cap never bursts on cold start |
| `session.pool_size` | `0` | Warm-pool size; reserved in the memory term when > 0 |

The cap interacts with `spawn_min_memory_gb` but does not replace it: the cap is
a startup count limit, while `spawn_min_memory_gb` is a real-time per-spawn
memory floor. They are independent guards.
When the memory floor is enabled, admission also reserves memory for the next
start and for live dedicated workers whose RSS has not yet reached the larger of
`subagent_cost_gb` and the live dedicated peak RSS. Claimed starts awaiting
registration and parents waiting without a slot retain this reservation;
confirmed shared sessions do not add a dedicated-process cost. Observed RSS
replaces reserved memory, so it is not counted twice. This lets short spawn
intervals fill available capacity without spending the same headroom repeatedly
while processes warm up. It cannot predict allocations beyond the estimated cost.

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
