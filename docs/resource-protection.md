# Resource Protection Mechanisms

KiroCrew runs long-lived LLM sessions that spawn OS processes (kiro-cli, MCP servers) across
multiple workflows — chat subagents, cron jobs, task runner steps, and background sessions.
Each workflow has different failure modes (event-loop saturation, orphaned tasks, hung processes,
context overflow), so protection is layered: primary timeouts catch the common case, independent
watchdogs catch what timeouts miss, and startup/periodic sweeps clean up anything that survived
a gateway crash. This defense-in-depth approach ensures no single mechanism is a single point
of failure.

## Mechanism Table

| Mechanism | Module | Scope | Timeout / Threshold | Independent Watchdog? | What Happens When It Fires |
|-----------|--------|-------|--------------------|-----------------------|---------------------------|
| `asyncio.wait_for` on `_run_inner` | `subagent.py` | Subagent tasks | 30 min (`_TIMEOUT_SECS`) | No (see reaper below) | Raises `TimeoutError`, marks subagent failed, resets session |
| Periodic reaper loop | `subagent.py` | Subagent tasks | 60s sweep (`_REAPER_INTERVAL`), kills at 30 min | Yes — runs independently of spawning session | `_force_reap`: reset → SIGKILL fallback → mark done → SEL audit → announce |
| Reset timeout in `_run` finally | `subagent.py` | Subagent cleanup | 30s (`_RESET_TIMEOUT`) | No | SIGKILL fallback + SEL audit if `reset()` hangs |
| Turn limit | `subagent.py` | Subagent tool calls | 100 turns (`_TURN_LIMIT`, configurable) | No | Stops execution, returns partial output |
| `asyncio.wait_for` on `_execute` | `cron.py` | Cron jobs | 30 min (`_JOB_TIMEOUT_SECS`) | No | Raises `TimeoutError`, logs error, marks job failed |
| Periodic reaper loop | `cron.py` | Cron jobs | 60s sweep (`_REAPER_INTERVAL`), kills at 30 min | Yes — runs independently of job execution | `_force_reap`: reset → SIGKILL fallback → mark failed → SEL audit |
| Task runner watchdog | `taskrunner.py` | Task runner steps | 60 min warn / 2 hr kill (`STALL_TIMEOUT` / `STALL_CANCEL_TIMEOUT`) | Yes — 30s heartbeat loop (`_HEARTBEAT_INTERVAL`) | Notifies on stall, resets stuck session after 2 hr |
| Global task timeout | `taskrunner.py` | Entire task run | User-configurable (`--timeout`) | Checked in watchdog loop | Stops task run, marks failed |
| ACP process death detection | `acp/client.py` | All sessions | 5 consecutive empty reads (`_MAX_CONSECUTIVE_EMPTY`) | No | Raises `AcpProcessDied`, triggers session recovery |
| ACP init timeout | `acp/client.py` | Session creation | 4 min (`_INIT_TIMEOUT`) | No | Raises `AcpTimeoutError`, retries once |
| ACP prompt timeout | `acp/client.py` | Per-prompt | 2 hr (`_DEFAULT_PROMPT_TIMEOUT`) | No | Raises `AcpTimeoutError` |
| ACP read timeout | `acp/client.py` | Per-readline | 20s (`_READ_TIMEOUT`) | No | Allows `CancelledError` delivery at each yield point |
| Process group kill | `acp/client.py` | Process cleanup | Immediate | No | `killpg(SIGTERM)` → `killpg(SIGKILL)` → `_kill_escaped_children` for different-PGID descendants |
| Per-process resource limits | `security.py` (`apply_resource_limits`) via `sandbox.py` (`resource_limit_preexec`) | Every agent-influenced spawn (root agent, ACP subagents/runtime, MCP servers, app backends, cron scripts, git, hooks, voice) | Kernel-enforced `RLIMIT_NOFILE=1024` default-on; `RLIMIT_NPROC`/`RLIMIT_CPU`/`RLIMIT_AS` opt-in (default off) | Yes — kernel enforces at fork/alloc/open time, no sweep needed | Kernel refuses `open()` past the FD cap (EMFILE); on opt-in NPROC/CPU/AS, EAGAIN / SIGXCPU / ENOMEM |
| cgroup v2 scope (fork bomb + memory) | `sandbox.py` (`cgroup_scope_argv`) | Every agent-influenced spawn tree (root agent + all its MCP servers/subagents as one scope; each cron/app-backend/hook/git/tool spawn its own) | `pids.max=1024` (`TasksMax`) + `memory.max=65% of host RAM` (`MemoryMax`, `MemorySwapMax=0`) per transient `systemd --user --scope` under `kirocrew-agents.slice`, default-on where cgroup v2 delegation exists | Yes — kernel enforces at fork()/alloc time; OOM-kills the scope on memory breach, `fork()` fails EAGAIN past `pids.max` | Fork bomb bounded to `pids.max`; memory balloon OOM-killed at `memory.max`. Unavailable (no delegation/macOS) → no-op + one loud SECURITY warning; RLIMIT_NOFILE still applies |
| Bounded restart shutdown | `dashboard/handlers.py` | Dashboard ⚡ Apply & Restart | 5s (`_SHUTDOWN_TIMEOUT_SECS`) | No | `asyncio.wait_for` on `provider.shutdown()`; `_sync_kill_provider` fallback on timeout |
| Subagent injection outer cap | `subagent.py _run()` | Per-subagent completion | 1200s (`_ON_DONE_TIMEOUT`) | No | Semaphore wait + injection combined; on timeout kills stuck kiro-cli via `sessions.reset()` and queues failure event for parent to drain |
| Subagent injection inner cap | `gateway.py` | Per `stream_and_collect` | 300s (`INJECTION_TIMEOUT`) | No | `_inject_with_retry` up to 2 retries (3 attempts) with backoff; bounded by outer 1200s cap |
| Prompt-busy recovery | `llm_helpers.py` | Per `stream_and_collect` | 2 retries + backoff | No | Cancels orphaned prompt; kills provider on exhaustion |
| Message queue | `session.py` + `events.py` | Per Slack thread | Unbounded FIFO | No | Queues when busy; `message_deleted` cancels; `!stop` clears |
| Orphaned dashboard reaping | `session.py` | Dashboard sessions | Immediate | Yes | `set_active_dashboard_slots()` reaps sessions whose slot is gone |
| Stale PID cleanup | `session.py` | `session_pid_*.txt` | Startup | No | Removes PID files for dead processes |
| Empty dir cleanup | `session.py` | `sessions/` subdirs | Startup | No | Removes empty dirs from timed-out subagents |
| `cleanup_orphaned_sessions` | `session.py` | All kiro-cli PIDs | Startup + shutdown only | No | Reads `kiro_pids.txt`, validates via `/proc`, sends SIGKILL, clears file. Also calls `_cleanup_orphaned_mcp_servers()` internally at startup |
| `_cleanup_orphaned_mcp_servers` | `session.py` | MCP child PIDs | Every ~5 min (periodic sweep) | Yes — runs in `_cleanup_loop` | Scans for orphaned MCP processes, sends SIGKILL |
| Idle session expiry | `session.py` | All sessions | 60 min idle (configurable via `session.timeout_secs`) | Yes — runs in `_cleanup_loop` (~5 min interval) | Calls `provider.shutdown()`, removes session |
| Circuit breaker | `session.py` | Per-session | 5 consecutive failures (`_CIRCUIT_BREAKER_THRESHOLD`) | No | Auto-resets session (kills process, creates fresh) |
| Context compaction | `session.py` | Chat sessions | Configurable (`session.autocompact_pct`, default 90%) | No | Sends `/compact` to kiro-cli to free context window |
| Background session recycle | `session.py` | Background sessions (cron, subagent) | 70% context usage (`_BG_RECYCLE_PCT`) | No | Recycles session before context overflow |
| Watchdog process liveness | `taskrunner.py` | Task runner steps | 2 consecutive dead checks (`_DEAD_THRESHOLD`) at 30s intervals | Yes — part of watchdog loop | Resets session to trigger crash recovery |
| Config bound clamp | `config/loader.py` | Subagent count / turns / pool size at load time | `subagent_auto_max` 1..64, `max_subagents` 0..64, `subagent_max_turns` 1..200, `pool_size` 0..10 (`_SECURITY_BOUNDED_FIELDS`) | No | `_clamp_security_bounds` clamps out-of-range ints, logs WARNING, emits SEL `config_bounds_clamped` (`outcome=clamped`) |

## Per-Workflow Coverage Matrix

|  | Primary Timeout | Watchdog / Reaper | Process Cleanup | Context Management |
|--|----------------|-------------------|-----------------|-------------------|
| **Chat subagents** | ✅ `wait_for` 30 min | ✅ Reaper (60s sweep) | ✅ `reset()` + SIGKILL fallback | ✅ `_BG_RECYCLE_PCT` 70% recycle |
| **Cron jobs** | ✅ `wait_for` 30 min | ✅ Reaper (60s sweep) | ✅ `reset()` + SIGKILL fallback | ✅ `_BG_RECYCLE_PCT` 70% recycle |
| **Task runner** | ✅ Global timeout + stall detection | ✅ Watchdog (30s heartbeat) | ✅ `_cleanup_run_sessions` + `asyncio.shield` | ✅ Compaction at 90% |
| **Background sessions** (shared: cron, heartbeat, lessons) | ⚠️ Idle expiry only (60 min) | ✅ Periodic sweep (~5 min) | ✅ `cleanup_orphaned_sessions` at startup | ✅ `_BG_RECYCLE_PCT` 70% recycle |

## Known Gaps

1. **Subagent timeout is not configurable.** `_TIMEOUT_SECS` (30 min) is hardcoded. Some
   legitimate tasks (large code generation, complex multi-tool workflows) may need longer.
   Tracked: configurable subagent timeout

2. **`cleanup_orphaned_sessions` only runs at startup/shutdown.** If a session's process dies
   mid-run without triggering `AcpProcessDied` (e.g. OOM kill), the PID stays in
   `kiro_pids.txt` until the next gateway restart. The periodic `_cleanup_orphaned_mcp_servers`
   sweep catches MCP children but not the root kiro-cli process.

3. **Per-process resource limits — implemented and wired (Talos bdf0d7e5 / V2285983353).**
   `security.py:apply_resource_limits(config)` returns a `preexec_fn` that applies POSIX
   `setrlimit` caps in the child (post-fork, pre-exec), and `sandbox.py:resource_limit_preexec()`
   is the cached accessor every agent-influenced spawn passes as `preexec_fn=` — the root agent
   and ACP subagents/runtime (`acp/client.py`, `acp/runtime.py`), MCP server probes
   (`mcp_discovery.py`), app backends and their dependency installs (`apps/backend.py`), the app
   registry's git clone/build spawns (`apps/registry.py`, `apps/routes.py`), builtin app
   subprocesses (deploy_web, file_explorer), cron scripts/commands
   (`cron_script.py`), the task runner's test spawn (`task_executor.py`), agent-selected git
   (`git_coord.py`), shell hooks (`hooks.py`), the knowledge worker pool
   (`knowledge/llm_pool.py`), and voice synthesis (`voice_reply.py`).
   `test/test_spawn_audit.py` enforces that every sandbox-routed spawn also applies the ceiling,
   so the helper cannot regress to dead code.

   **Defaults — only the one safe-by-default limit is on; the hazardous knobs are opt-in:**
   - `RLIMIT_NOFILE = 1024` (default-on) — max open file descriptors. It is **per-process**,
     generous enough that no legitimate tool trips it, yet finite so a descriptor leak (which
     climbs unbounded) is arrested. This is the only limit safe as a blanket default.
   - `RLIMIT_NPROC = 0` (disabled by default). CAVEAT: `RLIMIT_NPROC` is enforced **per
     real-UID** against the count of ALL the user's existing processes *and threads* — not the
     spawn's own subtree. A busy login/desktop UID routinely holds **thousands** of threads
     (measured ~3600 on one dev host), so any fixed cap tight enough to bound a fork bomb is
     already below the host's baseline and would make **every** spawn fail to fork (EAGAIN) —
     strictly worse than the DoS gap. Safe to enable only when the gateway runs as its own
     dedicated UID. cgroup v2 `pids.max` (per-cgroup, not per-UID) is the correct fork-bomb
     ceiling — see the remaining gap below. Darwin nuance: the kernel silently clamps a
     non-root `RLIMIT_NPROC` to `kern.maxprocperuid`, which can sit below the inherited hard
     cap (`kern.maxproc`); the clamp is strictly tighter so enforcement is unaffected, and
     `test_config_overrides_applied` folds the sysctl into its expectation on macOS.
   - `RLIMIT_CPU = 0` (disabled by default). CPU-seconds accrue over a process's **whole
     lifetime**; the root agent runs up to a 30-min turn and a busy tool-heavy session can
     legitimately burn hundreds of CPU-seconds, so a non-zero global cap would `SIGXCPU`-kill
     healthy sessions. Opt in per-deployment only when the spawn population is exclusively
     short-lived.
   - `RLIMIT_AS = 0` (disabled by default). `RLIMIT_AS` caps **virtual** address space, not
     resident memory, and Node/V8 (kiro-cli, claude-agent-acp, every npm MCP server) reserves
     huge virtual mappings far exceeding real use — measured ~2 GB VSZ for 4 idle worker
     threads, ~3.4 GB for 8 — so even a "generous" 4 GB cap `SIGKILL`s normal MCP-heavy
     sessions with spurious ENOMEM. cgroup v2 `memory.max` is the correct RSS ceiling and is
     tracked separately (below); `RLIMIT_AS` is left as an opt-in escape hatch for non-Node
     fleets.

   **Config:** operators override defaults via a `resource_limits` object in the config JSON —
   keys `max_processes`, `max_open_files`, `max_cpu_seconds`, `max_memory_mb` (each a positive
   int to set, `0` to leave inherited). A requested limit is always clamped **down** to the
   inherited hard limit — the helper only tightens, never raises. On non-POSIX platforms
   (`resource` unavailable) it is a no-op; on platforms lacking a specific rlimit (e.g. macOS
   has no `RLIMIT_NPROC`) that limit degrades gracefully.

   **cgroup v2 scope — the default-on fork-bomb + memory-DoS ceiling.** Because RLIMIT is the
   wrong tool for those two threats (`RLIMIT_NPROC` is per-UID, `RLIMIT_AS` caps virtual not
   resident memory), the actual default-on defense is a **cgroup v2 scope** applied by
   `sandbox.py:cgroup_scope_argv()`. Every agent-influenced spawn is wrapped in a transient
   `systemd-run --user --scope` (nested under `kirocrew-agents.slice`) with:
   - `TasksMax` = `pids.max` (default **1024**, from `max_processes`) — the **fork-bomb**
     ceiling. Per-cgroup, so it bounds the agent + all its MCP-server/tool descendants as one
     unit without the per-UID footgun; `fork()` fails `EAGAIN` past it.
   - `MemoryMax` + `MemorySwapMax=0` = `memory.max` (default **65% of physical RAM** — e.g.
     ~10.6 GB on a 16 GB box, ~21.3 GB on 32 GB; overridable via `max_memory_mb`, and an
     8192 MB fallback when host RAM can't be read) — the **memory-balloon** ceiling. It scales
     with the machine and is a **per-scope** cap (each spawn tree gets its own scope), so it
     bounds a single runaway tree while leaving headroom for the OS + gateway — it is not an
     aggregate guarantee across many concurrent scopes. This is a true RSS cap (not virtual),
     so it does not trip on Node/V8's large virtual mappings; the kernel OOM-kills the scope on
     breach.

   The kernel enforces both at `fork()`/allocation time — no reaper race. `--scope` execs into
   the target (it does not fork a wrapper), so the gateway's PID tracking / `killpg` /
   descendant scan are unaffected. It composes *outside* the OS-level sandbox: a child is
   filesystem-isolated (namespace/seatbelt) **and** cgroup-bounded. `test/test_spawn_audit.py`
   asserts every sandbox-routed spawn also applies the scope (tripwire against regression).

   **Availability + fallback:** requires Linux with cgroup v2 delegation (the `pids` + `memory`
   controllers delegated to the user slice) and a systemd user session. Where that is
   unavailable (older Linux without delegation, no user session, macOS), `cgroup_scope_argv`
   returns the argv unchanged and logs a **one-time loud SECURITY warning** — `RLIMIT_NOFILE`
   still applies, but the fork-bomb / memory ceilings are NOT enforced there. Operators on such
   hosts should run the gateway under an externally-configured cgroup/container limit.

   **Remaining gap:** these are per-*scope* ceilings, not a single per-*session* aggregate
   across a session's multiple spawn trees; and enforcement depends on cgroup v2 delegation
   being present. The load-time config clamp (see the Mechanism Table) only bounds process
   *counts* (subagent count, turn budget, pool size). Tracked as Shepherd finding 444f0e03.

## Interaction Notes

- **Reaper `reaped` flag prevents double cleanup.** When the reaper force-kills a subagent,
  it sets `info.reaped = True`. The `_run()` method's `CancelledError` handler and `finally`
  block check this flag and skip their own cleanup (release, reset, decrement, announce) to
  avoid double side-effects. The cron reaper uses the same pattern: `_reaped_jobs` set
  prevents `_run_job_isolated` from merging stale results after the reaper has already
  updated job state.

- **`asyncio.shield` in task runner protects cleanup from cancellation.** When a task run is
  cancelled, `_cleanup_run_sessions` is wrapped in `asyncio.shield()` so session resets
  complete even if the parent task is cancelled. This prevents orphaned processes.

- **Circuit breaker and context compaction are complementary.** The circuit breaker handles
  repeated failures (likely a broken session), while compaction handles context window
  exhaustion (a healthy session that's been running too long). Both trigger session reset
  but for different reasons.

- **Idle expiry and `_cleanup_orphaned_mcp_servers` run on the same loop.** The
  `_cleanup_loop` in `session.py` runs every ~5 min (timeout/6, min 60s) and performs both
  idle session expiry and orphaned MCP server cleanup in the same iteration.

- **ACP read timeout enables cooperative cancellation.** The 20s `_READ_TIMEOUT` on each
  `readline()` in the prompt loop ensures `CancelledError` can be delivered at every yield
  point, which is what makes the reaper's `task.cancel()` effective.

- **The periodic sweep's active set unions live shared-runtime PIDs.** Every `AcpRuntime`
  records its PID at spawn, so the orphan sweep would SIGKILL any tracked PID missing from
  the active set (surfacing as `process exited (rc=-9)` mid-chat). To protect runtimes that
  live outside `self._sessions` — companion subagent runtimes and the background
  `kirocrew-lite` runtime — the sweep unions `SessionManager._companion_runtime_pids()`
  (`session.py`) into the active set in both the candidate-collection and the phase-2 re-check
  passes, so live shared runtimes are never swept.

- **Direct worker-pool ACP sessions are shielded from the sweep by the pool engine.** Pool
  workers are long-lived agent sessions the sweep cannot see via `_collect_active_pids`.
  The shared `WorkerPool` engine (`acp/worker_pool.py`) registers each worker's PID via
  `register_protected_pid` / `unregister_protected_pid` (from `session_pid.py`) as part of
  the worker lifecycle, so every pool built on it — the knowledge `LLMPool` worker
  (`AcpWorker`, `knowledge/llm_pool.py`) and the code-review-sage `ReviewPool` worker
  (`AcpReviewWorker`, `sage_lib/review_pool.py`) — is protected by construction, and the
  periodic orphan sweep won't SIGKILL a busy worker mid-task.

- **Browser-triggerable read-only FS scans run on an isolated pool.** Dashboard list
  endpoints (`GET /api/skills`, `/api/agents/installed`, `/api/prompts`) do `os.walk`-style
  filesystem discovery on the dedicated `discovery_executor` pool (`executors.py`), kept
  separate from the reaper-critical `maintenance_executor` so a burst of concurrent
  user-triggered scans can never starve the orphan sweeps.
