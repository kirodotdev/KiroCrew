# Resource Protection Mechanisms

Kiro Crew runs long-lived LLM sessions that spawn OS processes (kiro-cli, MCP servers) across
several workflows: chat subagents, cron jobs, task runner steps, and background sessions.
Each workflow has different failure modes (event-loop saturation, orphaned tasks, hung
processes, context overflow), so protection is layered. Primary timeouts catch the common
case, independent watchdogs catch what timeouts miss, and startup/periodic sweeps clean up
anything that survived a gateway crash. No single mechanism is a single point of failure.

## Mechanism table

| Mechanism | Module | Scope | Timeout / threshold | Independent watchdog? | What happens when it fires |
|-----------|--------|-------|--------------------|-----------------------|---------------------------|
| `asyncio.wait_for` on `_run_inner` | `subagent.py` | Subagent tasks | 30 min (`_TIMEOUT_SECS`) | No (see reaper below) | Raises `TimeoutError`, marks subagent failed, resets session |
| Periodic reaper loop | `subagent.py` | Subagent tasks | 60s sweep (`_REAPER_INTERVAL`), kills at 30 min | Yes, runs independently of the spawning session | `_force_reap`: reset, SIGKILL fallback, mark done, SEL audit, announce |
| Startup watchdog | `subagent.py` | Pre-first-turn subagents | 120s with no runtime (`_STARTUP_TIMEOUT_SECS`) | Yes | Reaps a subagent that never got a runtime |
| Reset timeout in `_run` finally | `subagent.py` | Subagent cleanup | 30s (`_RESET_TIMEOUT`) | No | SIGKILL fallback plus SEL audit if `reset()` hangs |
| Turn limit | `subagent.py` | Subagent tool calls | 100 turns (`_TURN_LIMIT`, configurable) | No | Stops execution, returns partial output |
| Stall surfacing | `subagent.py` | Running subagents | 120s with no stream activity (`_STALL_IDLE_SECS`) | Yes | Surfaces the subagent as "stalled" in the UI |
| `asyncio.wait_for` on `_execute` | `cron.py` | Cron jobs | 30 min (`_JOB_TIMEOUT_SECS`) | No | Raises `TimeoutError`, logs error, marks job failed |
| Periodic reaper loop | `cron.py` | Cron jobs | 60s sweep (`_REAPER_INTERVAL`), kills at 30 min; reset bounded by `_REAPER_RESET_TIMEOUT` (30s) | Yes, runs independently of job execution | `_force_reap`: reset, SIGKILL fallback, mark failed, SEL audit |
| Task runner watchdog | `taskrunner.py` | Task runner steps | 60 min warn / 2 hr kill (`STALL_TIMEOUT` / `STALL_CANCEL_TIMEOUT` in `task_models.py`) | Yes, 30s heartbeat loop (`_HEARTBEAT_INTERVAL`) | Notifies on stall, resets the stuck session after 2 hr |
| Global task timeout | `taskrunner.py` | Entire task run | User-configurable (`--timeout`) | Checked in the watchdog loop | Stops the task run, marks failed |
| ACP process death detection | `acp/client.py` | All sessions | 5 consecutive empty reads (`_MAX_CONSECUTIVE_EMPTY`) | No | Raises `AcpProcessDied`, triggers session recovery |
| ACP init timeout | `acp/client.py` | Session creation | 4 min (`_INIT_TIMEOUT`; MCP servers can be slow to initialize) | No | Raises `AcpTimeoutError`, retries once |
| ACP prompt timeout | `acp/client.py` | Per prompt | 2 hr (`_DEFAULT_PROMPT_TIMEOUT`) | No | Raises `AcpTimeoutError` |
| ACP read timeout | `acp/client.py` | Per readline | 20s (`_READ_TIMEOUT`) | No | Allows `CancelledError` delivery at each yield point |
| Cooperative-cancel grace | `acp/client.py` | Per cancel | `max(_CANCEL_GRACE_SECS, caller budget)`, floor 10s | No | Read loop abandons the turn as unresponsive once the grace elapses |
| Process group kill | `acp/client.py` | Process cleanup | Immediate | No | `killpg(SIGTERM)`, `killpg(SIGKILL)`, then `_kill_escaped_children` for descendants that changed PGID |
| Per-process resource limits | `security.py` (`apply_resource_limits`), delivered **after `exec`** by `_spawn_exec_shim.py` via `sandbox.py` (`create_subprocess_limited` / `spawn_shim_argv`) | Every agent-influenced spawn (see the profile list below) | Kernel-enforced `RLIMIT_NOFILE=1024` default-on; `RLIMIT_NPROC` / `RLIMIT_CPU` / `RLIMIT_AS` opt-in (default off) | Yes, the kernel enforces at fork/alloc/open time, no sweep needed | Kernel refuses `open()` past the FD cap (EMFILE); on opt-in NPROC/CPU/AS: EAGAIN, SIGXCPU, ENOMEM |
| cgroup v2 scope (fork bomb + memory) | `sandbox.py` (`cgroup_scope_argv`) | Every agent-influenced spawn tree (root agent plus all its MCP servers and subagents as one scope; each cron, app-backend, hook, git or tool spawn gets its own) | `pids.max=8192` (`TasksMax`) plus `memory.max=65% of host RAM` (`MemoryMax`, `MemorySwapMax=0`) per transient `systemd-run --user --scope` under `kirocrew-agents.slice`, default-on where cgroup v2 delegation exists | Yes, the kernel enforces at `fork()`/alloc time; OOM-kills the scope on a memory breach, `fork()` fails EAGAIN past `pids.max` | Fork bomb bounded to `pids.max`; memory balloon OOM-killed at `memory.max`. Unavailable (no delegation, macOS): no-op plus one loud SECURITY warning, `RLIMIT_NOFILE` still applies |
| Bounded restart shutdown | `dashboard/handlers/sessions.py` | Dashboard Apply & Restart | 10s (`_SHUTDOWN_TIMEOUT_SECS`) | No | `asyncio.wait_for` on `provider.shutdown()`; `_sync_kill_provider` fallback on timeout |
| Subagent injection outer cap | `subagent.py` `_run()` | Per-subagent completion | 1200s (`_ON_DONE_TIMEOUT`) | No | Covers semaphore wait plus injection; on timeout kills the stuck kiro-cli via `sessions.reset()` and queues a failure event for the parent to drain |
| Subagent injection inner cap | `slack/gateway.py` | Per `stream_and_collect` | 900s (`INJECTION_TIMEOUT`, from `_DEFAULT_INJECTION_TIMEOUT`; override with `KIROCREW_INJECTION_TIMEOUT`, clamped down to `_ON_DONE_TIMEOUT`) | No | `_inject_with_retry` up to 3 attempts with backoff, bounded by the outer 1200s cap |
| Prompt-busy recovery | `llm_helpers.py` | Per `stream_and_collect` | 2 retries plus backoff | No | Cancels the orphaned prompt; kills the provider on exhaustion |
| Message queue | `session.py` plus `events.py` | Per Slack thread | Unbounded FIFO | No | Queues when busy; `message_deleted` cancels; `!stop` clears |
| Orphaned dashboard reaping | `session.py` | Dashboard sessions | Immediate | Yes | `set_active_dashboard_slots()` reaps sessions whose slot is gone |
| Empty dir cleanup | `session.py` | `sessions/` subdirs | Startup | No | Removes empty dirs left by timed-out subagents |
| `cleanup_orphaned_sessions` | `session_pid.py` | All kiro-cli PIDs | Startup and shutdown only | No | Reads `kiro_pids.txt`, validates liveness, sends SIGKILL, clears the file. Also removes stale `session_pid_*.txt` files for dead processes, and calls `_cleanup_orphaned_mcp_servers()` internally |
| `_cleanup_orphaned_mcp_servers` | `session_pid.py` | MCP child PIDs | Every ~5 min (periodic sweep) | Yes, runs in `_cleanup_loop` | Scans for orphaned MCP processes, sends SIGKILL |
| Idle session expiry | `session.py` | All sessions | `session.timeout_secs`, clamped up to a 60s minimum; `0` disables the sweep but keeps process hygiene | Yes, runs in `_cleanup_loop` (~5 min interval) | Calls `provider.shutdown()`, removes the session |
| Circuit breaker | `session.py` | Per session | 5 consecutive failures (`_CIRCUIT_BREAKER_THRESHOLD`) | No | Auto-resets the session (kills the process, creates a fresh one) |
| Context compaction | `session.py` | Chat sessions | `session.autocompact_pct` | No | Sends `/compact` to kiro-cli to free context window |
| Background session recycle | `session.py` | Background sessions (cron, subagent) | 70% context usage (`_BG_RECYCLE_PCT`) | No | Recycles the session before context overflow |
| Watchdog process liveness | `taskrunner.py` | Task runner steps | 2 consecutive dead checks (`_DEAD_THRESHOLD`) at 30s intervals | Yes, part of the watchdog loop | Resets the session to trigger crash recovery |
| Config bound clamp | `config/loader.py` | Subagent count, turns, timeouts and pool size at load time | `subagent_auto_max` and `max_subagents` to 64 (`SUBAGENT_AUTO_MAX_CEILING`), `subagent_max_turns` 1..200, `chat_turn_timeout_secs` 300..7200, `tool_approval_timeout_secs` 30..7200 and cross-field to 60s under the turn ceiling (`APPROVAL_TURN_MARGIN_SECS`), `loop_stall_exit_after_secs` 10..300, `pool_size` 0..10 (`_SECURITY_BOUNDED_FIELDS`) | No | `_clamp_security_bounds` clamps out-of-range ints, logs a WARNING, emits SEL `config_bounds_clamped` (`outcome=clamped`) |

## Per-workflow coverage matrix

|  | Primary timeout | Watchdog / reaper | Process cleanup | Context management |
|--|----------------|-------------------|-----------------|-------------------|
| **Chat subagents** | `wait_for` 30 min | Reaper (60s sweep) | `reset()` plus SIGKILL fallback | `_BG_RECYCLE_PCT` 70% recycle |
| **Cron jobs** | `wait_for` 30 min | Reaper (60s sweep) | `reset()` plus SIGKILL fallback | `_BG_RECYCLE_PCT` 70% recycle |
| **Task runner** | Global timeout plus stall detection | Watchdog (30s heartbeat) | `_cleanup_run_sessions` plus `asyncio.shield` | Compaction at `autocompact_pct` |
| **Background sessions** (shared: cron, heartbeat, lessons) | Idle expiry only | Periodic sweep (~5 min) | `cleanup_orphaned_sessions` at startup | `_BG_RECYCLE_PCT` 70% recycle |

Background sessions are the thin row: they have no per-turn primary timeout, only
idle expiry, so a wedged background session survives until the idle sweep or a
context recycle catches it.

## Per-process resource limits

`security.apply_resource_limits(config)` resolves the POSIX `setrlimit` caps, and
`sandbox._rlimit_spec()` renders them as the `RLIMIT_NAME:value` policy string that
`_spawn_exec_shim.py` applies **after `exec`**, in the single-threaded child.
`sandbox.create_subprocess_limited()` is the accessor every agent-influenced ASYNC spawn
uses: it prepends the shim via `spawn_shim_argv()` and passes `preexec_fn=None`.

Four profiles:

| Profile | Used by | Effect |
|---------|---------|--------|
| `tool` (default) | Every ordinary agent-influenced spawn | The full rlimit ceiling plus `oom_score_adj=1000` |
| `session_host` | The trusted ACP session-host spawns (`acp/client.py`, `acp/runtime.py`) | RAISES NOFILE to the inherited hard limit and does nothing else. A session host multiplexes many MCP pipe pairs, and the 1024 cap caused EMFILE crashes. No OOM bias: a trusted session host must not be the preferred kill target |
| `build` | The dev-fleet build spawns (`apps/builtins/dev_fleet/server.py`) | Vite and npm need thousands of descriptors; keeps the OOM bias |
| `none` | The user's own interactive terminal | No rlimits, no OOM bias, so the shim has nothing to deliver |

Async, shim-routed spawns cover MCP server probes (`mcp_discovery.py`), the app
registry's clone and build spawns (`apps/registry.py`, `apps/routes.py`), the task
runner's test spawn (`task_executor.py`), agent-selected git (`git_coord.py`), shell
hooks (`hooks.py`), the knowledge worker pool (`knowledge/llm_pool.py`), voice
synthesis (`voice_reply.py`), the source-provider CLI spawns
(`dashboard/handlers/source_providers.py`), and the builtin app subprocesses under
`apps/builtins/`. Synchronous `subprocess.run` / `Popen` spawns, notably cron scripts
(`cron_script.py`) and app-backend dependency installs (`apps/backend.py`), still pass
`resource_limit_preexec()` as `preexec_fn=`: they wedge a worker thread rather than the
event loop, so the hazard below does not apply to them with the same force. Migrating
them is tracked follow-up work.

`test/test_spawn_audit.py` enforces that every sandbox-routed spawn also applies the
ceiling, so the helper cannot regress into dead code.

### Why after `exec` and not in a `preexec_fn`

`preexec_fn` forces CPython off `posix_spawn`/`vfork` onto a plain `fork()` of the
multi-GB, roughly-118-thread gateway, and runs Python bytecode in the child before
`exec`. A lock another thread held at fork time cannot be released there, so the child can
wedge before ever reaching `exec`, and a wedged child takes more than itself down:

- `subprocess.Popen._execute_child` blocks in an unbounded `os.read(errpipe_read, ...)`
  waiting for the child to exec or die. For `asyncio.create_subprocess_exec` that read runs
  on the event loop thread with no `await` point, so no `asyncio.wait_for` can interrupt it
  and the whole gateway stops.
- `_posixsubprocess`'s `child_exec()` runs `_close_open_fds()` *after* `preexec_fn`, so the
  wedged child still holds a duplicate of every inherited fd, `gateway.lock` and the
  dashboard's listening socket included, which then outlive the gateway.

This is observed behavior, not theory: a child deadlocked in a futex, never exec'd, and
pinned the fds it inherited. Limits set post-`exec` are inherited by the exec'd image and
all its descendants, so coverage is unchanged; only the delivery point moved.
`test/test_spawn_preexec_guard.py` is the AST tripwire that keeps a new async call site
from reintroducing the fork.

**Two documented exceptions**, both allowlisted in the tripwire:

- `sandbox.create_subprocess_limited`'s own fallback, for a host with no usable shim
  (non-POSIX, or a truncated install). Dropping the caps silently would be worse.
- `dashboard/handlers/terminal.py`'s interactive shell. It carries the `none` profile, so
  the shim would have nothing to deliver while costing an interpreter startup on every
  terminal open (measurably doubling the terminal test file's wall time). Its `preexec_fn`
  is a single pre-resolved `ioctl` with no allocation and no lock acquisition, which is the
  only shape where a fork-child callable is defensible. The fork remains; the risk is
  accepted and stated at the call site.

### Defaults: one safe blanket limit, three opt-in knobs

- **`RLIMIT_NOFILE = 1024` (default-on)**, max open file descriptors. It is
  **per-process**, generous enough that no legitimate tool trips it, yet finite, so a
  descriptor leak (which climbs unbounded) is arrested. This is the only limit safe as a
  blanket default.
- **`RLIMIT_NPROC = 0` (disabled).** It is enforced **per real UID** against the count of
  ALL the user's existing processes *and threads*, not the spawn's own subtree. A busy
  login or desktop UID routinely holds thousands of threads (roughly 3600 measured on one
  dev host), so any fixed cap tight enough to bound a fork bomb already sits below the
  host's baseline and would make **every** spawn fail to fork (EAGAIN), strictly worse than
  the DoS gap. Safe to enable only when the gateway runs as its own dedicated UID. cgroup
  v2 `pids.max` (per-cgroup, not per-UID) is the correct fork-bomb ceiling. Darwin nuance:
  the kernel silently clamps a non-root `RLIMIT_NPROC` to `kern.maxprocperuid`, which can
  sit below the inherited hard cap (`kern.maxproc`); the clamp is strictly tighter, so
  enforcement is unaffected, and `test_config_overrides_applied` folds the sysctl into its
  expectation on macOS.
- **`RLIMIT_CPU = 0` (disabled).** CPU-seconds accrue over a process's **whole lifetime**,
  and the root agent runs up to a 30-minute turn while a busy tool-heavy session can
  legitimately burn hundreds of CPU-seconds, so a non-zero global cap would `SIGXCPU`-kill
  healthy sessions. Opt in per deployment only when the spawn population is exclusively
  short-lived.
- **`RLIMIT_AS = 0` (disabled).** It caps **virtual** address space, not resident memory,
  and Node/V8 (kiro-cli, every npm MCP server) reserves huge virtual mappings far exceeding
  real use (roughly 2 GB VSZ measured for 4 idle worker threads, 3.4 GB for 8), so even a
  generous 4 GB cap `SIGKILL`s normal MCP-heavy sessions with spurious ENOMEM. cgroup v2
  `memory.max` is the correct RSS ceiling; `RLIMIT_AS` is left as an opt-in escape hatch for
  non-Node fleets.

**Config.** Operators override the defaults with a `resource_limits` object in the config
JSON: `max_processes`, `max_open_files`, `max_cpu_seconds`, `max_memory_mb`, each a
positive int to set and `0` to leave inherited. A requested limit is always clamped **down**
to the inherited hard limit, so the helper only tightens, never raises. On non-POSIX
platforms (no `resource` module) it is a no-op; on a platform lacking a specific rlimit
(macOS has no `RLIMIT_NPROC`) that limit degrades gracefully.

## The cgroup v2 scope

Because RLIMIT is the wrong tool for the fork-bomb and memory-DoS threats (`RLIMIT_NPROC`
is per-UID, `RLIMIT_AS` caps virtual rather than resident memory), the actual default-on
defense for both is a **cgroup v2 scope** applied by `sandbox.cgroup_scope_argv()`. Every
agent-influenced spawn is wrapped in a transient `systemd-run --user --scope` nested under
`kirocrew-agents.slice`, with:

- **`TasksMax`** = `pids.max`, default **8192** from `_CGROUP_DEFAULT_MAX_PROCESSES`
  (override via `resource_limits.max_processes`), the **fork-bomb** ceiling. `pids.max`
  counts tasks (threads), not processes. 1024 starved legitimate JVM build trees (Gradle
  plus parallel test workers need thousands of threads, failing as `pthread_create` EAGAIN
  while the host is idle); 8192 still bounds fork bombs, which spawn tens of thousands of
  tasks near-instantly. It is per-cgroup, so it bounds the agent plus all its MCP-server and
  tool descendants as one unit without the per-UID footgun. `fork()` fails `EAGAIN` past it.
- **`MemoryMax`** plus **`MemorySwapMax=0`** = `memory.max`, default **65% of physical RAM**
  (`_CGROUP_MEMORY_FRACTION`, roughly 10.6 GB on a 16 GB box and 21.3 GB on 32 GB;
  overridable via `max_memory_mb`, with an 8192 MB fallback from
  `_CGROUP_FALLBACK_MAX_MEMORY_MB` when host RAM cannot be read), the **memory-balloon**
  ceiling. It scales with the machine, where a flat 8 GB cap was both too tight on big boxes
  and too loose on small ones. There is deliberately **no floor**: a floor could push a tiny
  box above 65%, and 65% is the ceiling on our take. It is a **per-scope** cap (each spawn
  tree gets its own scope), so it bounds a single runaway tree while leaving headroom for the
  OS and the gateway. It is not an aggregate guarantee across many concurrent scopes. It is a
  true RSS cap, not virtual, so it does not trip on Node/V8's large virtual mappings; the
  kernel OOM-kills the scope on breach.
- **`CPUWeight`**, default **50** from `_CGROUP_DEFAULT_CPU_WEIGHT` (systemd's own default is
  100; override via `resource_limits.cpu_weight`), the **CPU fair-share** control, emitted
  only when the `cpu` controller is delegated. It is a proportional share, never a hard
  throttle: agent scopes use 100% of an idle host but yield to interactive work under CPU
  contention. A hard cap, `CPUQuota`, is available **opt-in only** via
  `resource_limits.max_cpu_percent` (`200` = 2 cores) and is off by default because hard
  quotas slow legitimate builds.

The spawn shim additionally writes `oom_score_adj=1000` on the child it execs (inherited by
its descendants), biasing the kernel OOM killer toward tool subprocesses so a
memory-ballooning command is killed *before* `memory.max` takes out the entire agent scope.
It is requested explicitly (`--oom-bias`) by the `tool` and `build` profiles only
(`_PROFILE_OOM_BIAS`).

The kernel enforces both ceilings at `fork()` and allocation time, so there is no reaper
race. `--scope` execs into the target rather than forking a wrapper, so the gateway's PID
tracking, `killpg` and descendant scan are unaffected. It composes *outside* the OS-level
sandbox: a child is filesystem-isolated (namespace or seatbelt) **and** cgroup-bounded.
`test/test_spawn_audit.py` asserts every sandbox-routed spawn also applies the scope.

### Availability and fallback

The scope requires Linux with cgroup v2 delegation (the `pids` and `memory` controllers
delegated to the user slice) plus a systemd user session. Where that is unavailable (older
Linux without delegation, no user session, macOS), `cgroup_scope_argv` returns the argv
unchanged and logs a **one-time loud SECURITY warning**. `RLIMIT_NOFILE` still applies, but
the fork-bomb and memory ceilings are NOT enforced there. Operators on such hosts should run
the gateway under an externally-configured cgroup or container limit.

### Bus locators are part of the wrapper contract, and only the wrapper's

`systemd-run --user` reaches the user session bus via `XDG_RUNTIME_DIR` and
`DBUS_SESSION_BUS_ADDRESS`, so those must be present in the environment the spawn is created
with, not merely the gateway's. That environment is credential-scrubbed, and some callers
(`dashboard/handlers/source_providers.py` builds it from a strict allowlist rather than
inheriting `os.environ`), so `sandboxed_spawn_argv` restores the two keys via
`cgroup_scope_bus_env()` after the scrub, gated on the same availability probe that decides
whether to wrap at all. Omitting them does not degrade to an unbounded spawn, it fails the
spawn outright: `systemd-run` exits 1 with `Failed to connect to bus: No medium found`
before exec'ing the wrapped command.

They must not survive into the sandboxed child, however. A live user-bus address inside the
sandbox can be used to ask the user systemd manager to start a unit that runs *outside* the
namespace. So the forward is paired with an `env -u XDG_RUNTIME_DIR -u
DBUS_SESSION_BUS_ADDRESS` shim placed inside the scope, immediately after `--`, which drops
exactly the keys this layer added; a value the caller supplied itself is left alone. `env`
`exec`s in place, so PID tracking, `killpg` and descendant scans are unaffected. It is
resolved from an absolute path, never a caller-influenced `PATH`, and when no `env` binary
exists the layer **fails closed**: the locators are not forwarded at all, so the wrapper
fails loudly rather than handing the child a reachable bus.

## Known gaps

1. **The subagent timeout is not configurable.** `_TIMEOUT_SECS` (30 min) is hardcoded, and
   some legitimate tasks (large code generation, complex multi-tool workflows) need longer.

2. **`cleanup_orphaned_sessions` only runs at startup and shutdown.** If a session's process
   dies mid-run without triggering `AcpProcessDied` (an OOM kill, for instance), the PID
   stays in `kiro_pids.txt` until the next gateway restart. The periodic
   `_cleanup_orphaned_mcp_servers` sweep catches MCP children but not the root kiro-cli
   process.

3. **The cgroup ceilings are per-*scope*, not a per-*session* aggregate** across a session's
   several spawn trees, and enforcement depends on cgroup v2 delegation being present. The
   load-time config clamp bounds process *counts* (subagent count, turn budget, pool size),
   not memory or CPU.

## Interaction notes

- **The reaper's `reaped` flag prevents double cleanup.** When the reaper force-kills a
  subagent it sets `info.reaped = True`. `_run()`'s `CancelledError` handler and `finally`
  block check the flag and skip their own cleanup (release, reset, decrement, announce) to
  avoid double side effects. The cron reaper uses the same pattern: the `_reaped_jobs` set
  prevents `_run_job_isolated` from merging a stale result after the reaper has already
  updated job state.

- **`asyncio.shield` in the task runner protects cleanup from cancellation.** When a task run
  is cancelled, `_cleanup_run_sessions` is wrapped in `asyncio.shield()` so session resets
  complete even if the parent task is cancelled, which is what prevents orphaned processes.

- **The circuit breaker and context compaction are complementary.** The circuit breaker
  handles repeated failures (a broken session), while compaction handles context-window
  exhaustion (a healthy session that has been running a long time). Both trigger a session
  reset, for different reasons.

- **Idle expiry and `_cleanup_orphaned_mcp_servers` run on the same loop.** `_cleanup_loop`
  in `session.py` runs every ~5 min (timeout/6, minimum 60s) and performs both idle session
  expiry and orphaned MCP server cleanup in the same iteration.

- **The ACP read timeout is what enables cooperative cancellation.** The 20s `_READ_TIMEOUT`
  on each `readline()` in the prompt loop ensures `CancelledError` can be delivered at every
  yield point, which is what makes the reaper's `task.cancel()` effective.

- **The periodic sweep's active set unions live shared-runtime PIDs.** Every `AcpRuntime`
  records its PID at spawn, so the orphan sweep would SIGKILL any tracked PID missing from
  the active set (surfacing as `process exited (rc=-9)` mid-chat). Two runtime kinds live
  outside `self._sessions` and are invisible to `_collect_active_pids`: companion subagent
  runtimes (`_subagent_runtimes`, alive for the parent's whole lifetime) and the background
  `kirocrew-lite` runtime (`_bg_runtime`). The sweep unions
  `SessionManager._companion_runtime_pids()` into the active set in both the
  candidate-collection and the phase-2 re-check passes, so live shared runtimes are never
  swept. Only alive runtimes contribute, because a dead entry SHOULD be reaped.

- **Long-lived pool sessions are shielded from the sweep by an explicit PID registration.**
  Pool workers are long-lived agent sessions the sweep cannot see via
  `_collect_active_pids`, so without a shield it would SIGKILL a *busy* worker mid-task.
  Three shields, one mechanism (`register_protected_pid` / `unregister_protected_pid` in
  `session_pid.py`): the shared `WorkerPool` engine (`acp/worker_pool.py`) registers each
  worker's PID as part of the worker lifecycle and re-syncs it on every `reset()` (which
  respawns under a new PID), so any pool built on it (`workflows/agent_pool.py`) is
  protected by construction; the knowledge `LLMPool` worker (`AcpWorker`,
  `knowledge/llm_pool.py`) registers inline because it does not ride that engine; and
  `AcpRuntime` (`acp/runtime.py`) registers at spawn, which covers the code-review-sage
  `ReviewPool` (`apps/builtins/code_review_sage/sage_lib/review_pool.py`), whose
  `_BatchRuntimeHolder` multiplexes every concurrent review onto ONE batch-scoped
  `AcpRuntime` rather than a pool of subprocesses.

- **Browser-triggerable read-only FS scans run on an isolated pool.** Dashboard list
  endpoints (`GET /api/skills`, `/api/agents/installed`, `/api/prompts`, plus the themes,
  steering and prompt readers) do `os.walk`-style filesystem discovery on the dedicated
  `discovery_executor` pool (`executors.py`), kept separate from the reaper-critical
  `maintenance_executor` so a burst of concurrent user-triggered scans can never starve the
  orphan sweeps.
