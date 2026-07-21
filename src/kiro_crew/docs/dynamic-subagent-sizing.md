# Dynamic Sub-Agent Max Count

KiroCrew sizes the concurrent sub-agent cap **automatically** by default
(`agent.max_subagents = 0`): at gateway startup it computes a sensible cap from
the host's actual memory and CPU, plus a per-agent cost KiroCrew *learns* from
past runs. A fixed number is wrong in both directions — it wastes capacity on a
large host and over-commits a tiny one — so auto is the default; set a positive
integer to pin an explicit cap.

## Enabling It

Auto-sizing is the default. To pin an explicit cap instead:

```
kirocrew config set agent.max_subagents 8
```

- `agent.max_subagents = 0` — **auto** (default): compute the cap at startup.
- `agent.max_subagents > 0` — explicit fixed cap.

The cap is computed once per gateway start. Restart to recompute (e.g. after the
host's resources change).

## How the Cap Is Computed

```
mem_term = floor( (effective_available_GB * (1 - buffer%) - pool_reserve) / mem_cost )
cpu_term = floor( (cpu_count * (1 - buffer%)) / cpu_cost )
cap      = clamp( min(mem_term, cpu_term), 3, hard_cap )
```

- **Memory term** — how many agents fit in available RAM after reserving a
  buffer for the OS and other processes. `effective_available` is
  `min(MemAvailable, cgroup headroom)` so a memory-capped container is respected.
- **CPU term** — how many fit in the core budget, using a measured per-agent
  CPU cost (agents are mostly I/O-bound, so this is generous).
- **`min(...)`** — the tighter of memory/CPU wins.
- **Floor of 3** — never drops below the legacy default, so enabling auto can't
  regress a small host. The per-spawn memory gate (`agent.spawn_min_memory_gb`)
  still refuses individual spawns under real memory pressure.
- **`hard_cap`** — an absolute ceiling (see "Why a hard cap" below).

## Learned Per-Agent Cost

KiroCrew doesn't hard-code how much an agent costs — it measures it:

- While an agent runs, the reaper loop periodically samples its process-tree
  RSS (memory) and CPU, keeping the **high-water** mark for that run (a single
  reading at exit would miss a mid-run peak that has already declined).
- At exit, one sample `{agent, mem_gb, cpu_cores, ts}` is appended to
  `~/.kirocrew/subagents/cost_samples.jsonl`.
- At the next startup, KiroCrew takes the **p90 of the last N samples per
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
- **Dedicated** (per-process) spawns — any spawn with model/allowed-tools/bare
  overrides, or with session sharing off — keep the per-PID subtree sampling
  above.

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
raise it. KiroCrew does **not** yet measure provider saturation — that's a
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
| `session.pool_size` | `0` | Warm-pool size; reserved in the memory term when > 0 |

The cap interacts with `spawn_min_memory_gb` but does not replace it: the cap is
a startup count limit, while `spawn_min_memory_gb` is a real-time per-spawn
memory floor. They are independent guards.

## Notes

- Stdlib only — no new dependencies. Memory/CPU are read per platform:
  Linux reads `/proc/meminfo`, `/proc/<pid>/stat`, and cgroup limits; macOS
  reads *available* memory in-process via the Mach `host_statistics64` syscall
  through `ctypes`/`libSystem` (free + inactive + speculative + purgeable
  pages × page size) — no subprocess, so it is safe on the gateway event loop
  and passes the spawn-audit guard.
- On platforms with no probe yet (e.g. Windows) the readers fail open and the
  cap falls back to the configured value.
  NOTE: the per-spawn `spawn_min_memory_gb` admission gate still reads
  `/proc/meminfo` and therefore remains inert (fails open) on non-Linux hosts —
  auto-sizing and the runtime gate are independent guards.
- Design rationale and worked examples:
  `~/.kirocrew/workspace/dynamic-subagent-sizing.md`.
