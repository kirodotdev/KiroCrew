# Testing Conventions

## Framework

- `pytest` with `pytest-asyncio` for async tests
- Coverage via `pytest-cov`

## File Layout

```
test/
├── test_acp_types.py     # ACP type dataclasses
├── test_acp_client.py    # ACP client (mocked subprocess)
├── test_config.py        # Config loader
└── test_cli.py           # CLI commands
```

## Patterns

### Grouping
Group related tests in classes:
```python
class TestAcpClientInit:
    def test_defaults(self): ...
    def test_custom_work_dir(self, tmp_path): ...
```

### Async tests
```python
@pytest.mark.asyncio
async def test_read_message(self, tmp_path):
    ...
```

Never poll a synchronous store read from an async test. A plain `sdk.get(...)` /
`store.read(...)` inside an `async def` test runs ON the event loop, where
`read_bytes_with_retry` deliberately re-raises the Windows sharing-violation
`PermissionError` instead of sleeping the loop for its retry budget — so a poll
that races a concurrent `atomic_write` `os.replace` is a Windows-only flake that
POSIX shards can never reproduce (#7703). Offload every such read the way the
production routes do (`job_routes.py`):

```python
# WRONG: reads on the loop; retry budget is one attempt, and time.sleep stalls the loop
run = sdk.get(run_id); time.sleep(0.02)
# RIGHT: the retry applies off-loop, and the loop keeps running
run = await asyncio.to_thread(sdk.get, run_id); await asyncio.sleep(0.02)
```

### Mocking kiro-cli
Never spawn real `kiro-cli` in tests. Mock the subprocess:
```python
mock_process = MagicMock()
mock_stdout = AsyncMock()
mock_stdout.readline = AsyncMock(return_value=line.encode())
mock_process.stdout = mock_stdout
mock_process.returncode = None
client._process = mock_process
```

### Config overrides
Use `monkeypatch` to override config paths:
```python
def test_load_from_file(self, tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
```

### Filesystem tests
Use `tmp_path` fixture:
```python
def test_custom_work_dir(self, tmp_path):
    client = AcpClient(work_dir=tmp_path)
```

### Links: use the conftest helpers, do not skip on Windows

Creating a symlink on Windows needs `SeCreateSymbolicLinkPrivilege`; an unelevated
developer shell lacks it and `os.symlink` raises `OSError [WinError 1314]`. A
**directory junction** needs no privilege and is followed by the same reparse
machinery — `rglob`, `Path.resolve` and `GetFinalPathNameByHandleW` all traverse
it identically — so a junction exercises the behaviour under test on the platform
where these path semantics differ most. Two helpers in `test/conftest.py`:

| Need | Helper |
|------|--------|
| A path that reaches OUT of a sandbox root through a link | `make_escaping_link(inside, outside)` |
| A directory link at a chosen location (`ui/` -> the dev source tree) | `make_dir_link(link, target)` |

Prefer either over a bare `Path.symlink_to` plus a `skipif(sys.platform == "win32")`:
an unconditional skip drops the whole assertion on Windows. Reach for a skip only
where the *link kind itself* is the subject (a file symlink's `lstat` mode bits,
say), and then still pair it with a Windows counterpart.

### Patch the defining module, not a re-export

`monkeypatch.setattr`/`patch` rebind a NAME in one module namespace. Code
reads its globals from its **defining** module, so patching a package
re-export (e.g. `kiro_crew.dashboard.handlers.X`, imported there from
`handlers/sessions.py`) is a **silent no-op** — the test still passes but
exercises the production value. Symptom: a test that "shortens" a timeout yet
still takes the full production duration.

```python
# WRONG — handlers/__init__.py only re-exports the constant; sessions.py
# still reads its own module global (test silently waits the real 10s):
monkeypatch.setattr("kiro_crew.dashboard.handlers._SHUTDOWN_TIMEOUT_SECS", 0.05)

# RIGHT — patch where the constant is defined and read:
monkeypatch.setattr("kiro_crew.dashboard.handlers.sessions._SHUTDOWN_TIMEOUT_SECS", 0.05)
```

### Loop-wiring tests stub every dispatched operation

A test that drives a periodic/maintenance loop (e.g. `SessionManager.
_cleanup_loop`) pins the loop's *wiring* — which operations run, with what
args, and when. Stub **all** of them: any sweep left unstubbed runs for real
against the dev machine (process-table scans, `~/.kiro/crew` PID files), which
violates the isolation rules below and costs seconds per test (an unstubbed
`find_orphan_mcp_candidates` alone added ~9s to every `TestCleanupLoop`
test). The sweep's own behavior belongs in its own module's tests.

## Which conftest you are standing on

There are **two** testpaths (`setup.cfg`'s `testpaths = test
src/kiro_crew/apps/builtins`) and they do **not** get the same fixtures. Know which
floor is under your file before you decide what to isolate yourself:

| Your test lives in | It inherits |
|---|---|
| `test/` | the rootdir `conftest.py` **and** `test/conftest.py` |
| `src/kiro_crew/apps/builtins/*/tests/` | the rootdir `conftest.py`, plus that app's own `tests/conftest.py` where one exists (`auto_improvement`, `code_review_sage`, `spec_builder` have one; the other five apps do not) |

The **rootdir `conftest.py` is the host-mutation floor**: everything in it protects the
developer's machine rather than the correctness of one suite, so it holds for all
testpaths. It pins `$XDG_CONFIG_HOME` and the launchd paths, traps the spawn
funnels against service mutation, pins `KIROCREW_HOME` and the import-time `~/.kiro`
bindings, scrubs the inherited shell-preload and exported-function variables
`name_grant` refuses on (`BASH_ENV`/`ENV`/`SHELLOPTS`/`BASHOPTS`, `BASH_FUNC_*` keys,
and the legacy `() {` value spelling — a RHEL-family host inherits `BASH_FUNC_which%%`
from `which2.sh`, and the refusal outranks every narrower code), redirects
`tempfile`'s base, and fails the run on residue in the
checkout.

It also pins the other real host paths a test must not reach: the subagent registry (a
running gateway sweeps stray entries there as orphans), the 610MB embedding-model
download, and the agent-state sidecar.

Five members are there for a different reason — a **process-global** that any testpath
can poison for every test after it, which is the same failure shape as host mutation
one scope down:

* `pytest_runtest_setup` warms `sandbox._backend` when it is cold. A cold cache reached
  from a running event loop deliberately refuses to probe (the probe forks and waits)
  and answers "none", so the first async test to spawn through `wrap_argv` gets a hard
  refusal on a host whose sandbox works. Warming at setup rather than once per session
  is what makes it order-independent: the six `test_sandbox_*.py` files legitimately
  reset that cache in their own teardown.
* `_no_leaked_telemetry_exporter` fails the test that leaves an OTel exporter thread
  running. See the Rules entry — that thread makes the sandbox probe's fork child
  multithreaded, which the kernel answers with an EINVAL the probe used to cache as
  "this host has no sandbox backend".
* `_restore_log_record_factory` puts `logging`'s record factory back. There is one such
  slot per process, and `log_redaction`'s wrapper ALWAYS renders and clears
  `exc_info` (frame locals are unscannable), and clears `args` on any record that
  is not a clean tuple of exact scalars, so leaving it installed reds whatever
  unrelated test later asserts on either field. `cli._setup_cli_logging` installs it for a
  long-lived command, so grepping `cli.main()` finds only some of the tests that reach
  it — most call that helper directly, and they are in `test_cli_logging.py`, whose own
  `_pristine_logging` fixture restores handlers and levels but not the factory, which is
  why that file looks like it already handles this. Restored rather than blamed, for the
  same reason the CWD restore is: production installs it once per process and never
  undoes it, so a test driving that code cannot avoid it.
  `log_redaction.uninstall_log_redaction()` exists for a test that wants to assert on
  the uninstalled state itself.
* `_restore_logger_levels` puts every logger's level and `disabled` flag back. A level is
  process-global AND hierarchical, so an explicit one left on `kiro_crew` decides what
  every `kiro_crew.*` logger in the worker may emit and it outranks the root level
  `caplog.at_level()` sets — the victim's `caplog.text` comes back **empty**, not wrong,
  which reads as "the code stopped logging" rather than as pollution.
  `cli._setup_cli_logging` pins `kiro_crew` at WARNING, and test modules across the suite
  run it for real by driving `cli.main()` in process. Restored rather than blamed, for the
  same reason the CWD restore is. **Handlers are deliberately not restored**: one is
  routinely paired with a module-global recording it as installed
  (`dashboard.handlers.updates._log_ring_handler_installed`), and a floor can detach the
  handler but cannot know to clear the flag, which leaves the singleton reporting
  installed with nothing attached. The root logger's handler list is doubly excluded —
  pytest's own `catching_logs` adds one per test phase and removes it at the phase
  boundary, so writing back a setup-phase snapshot during teardown would drop the handler
  the teardown phase is capturing through.
* `_restore_autonudge_singleton` puts `autonudge._INSTANCE` back to whatever the test
  inherited. It lives in `test/conftest.py` rather than the rootdir floor, because only
  the `test/` suites drive the service; it is listed here because its failure shape is
  the process-global one this section is about.
  `AutoNudgeService.start()` publishes itself there and `stop()` clears it, so
  a test that starts the service — or drives a dashboard handler that does — leaves a live
  instance holding timer TASKS created on that test's event loop. Every later test in the
  same worker then reaches those tasks through the singleton on a loop that has since
  closed, which is how `test_dashboard_chat.py`'s `TestCloseBroadcastDurability` came to
  answer 500 from a leak in an unrelated file. Restored rather than blamed, for the same
  reason the CWD restore is: production really does publish this singleton. The teardown
  retires the leaked instance's timers through `_cancel_timer`, which is the one place
  that knows a task on a closed loop must be DROPPED rather than cancelled — `Task.cancel`
  schedules through `loop.call_soon` and raises `RuntimeError: Event loop is closed`.

It registers the xdist worker budget too — the policy is in the repo-root
`xdist_budget.py`, a plain module rather than a second conftest, because the module
name `conftest` is ambiguous: `test/` precedes the repository root on `sys.path`, so
`import conftest` from a test in `test/` can never reach the rootdir file. A distinct
name is reachable from both and resolves to one module object, which matters because
the held slot descriptors are module state.

`test/conftest.py` holds the rest: suite-specific isolation (Slack thread state, the
model-window cache, the platform context, …) and the Windows collect-ignore list.

When you add isolation, put it in the rootdir conftest **only** if a test in any
testpath could damage the host, poison a process global for every later test, or
consume enough of a shared *resource* — memory, cores, disk — to take the machine down
with it. Otherwise it belongs in `test/conftest.py`, where it costs the in-package
suites nothing. The first two of those entries started life in `test/conftest.py` and
were silently absent from the in-package tests, which is how each was found.

Resource consumption belongs on that list for the same reason damage does: a guard
that only covers `test/` is invisibly absent from the other two testpaths, and the
failure it was written to prevent — a swapped, unresponsive machine — does not care
which testpath asked for the workers.

## Rules

- Tests MUST NOT spawn real kiro-cli processes
- Tests MUST NOT depend on `~/.kiro/crew/` existing
- Tests MUST NOT write into the operator's real data dir. `KIROCREW_HOME` is pinned
  per test by the rootdir conftest, which is what makes `config_dir()` safe — and it
  needs to be, because resolving it is **not a read**: it creates the home and its
  marker on first use, and can run the one-time `~/.kirocrew` → `~/.kiro/crew`
  migration as a side effect.

  Two kinds of path escape that env var, and both need their own pin:

  1. **Bound at import time from `config_dir()`** — e.g.
     `subagent_persistence._SUBAGENTS_DIR`, set to `config_dir() / "subagents"` on
     first import. The env var is read *after* the module captured the path, so
     `conftest.py` pins each such global with a dedicated autouse fixture
     (`_isolate_subagents_dir`, …). Paths that instead call `config_dir()` lazily on
     each use (e.g. `agent_state`) already honor `KIROCREW_HOME`. A test that spawns
     subagents without isolating the import-time global leaks stub folders into
     `~/.kiro/crew/subagents/`, which a running gateway then sweeps as orphans on its
     next restart.
  2. **Bound at import time from `Path.home()`** — `~/.kiro` is *kiro-cli's* home,
     machine-wide and shared with the real installed agent, so it is a separate
     isolation axis from the data home entirely. `~/.kiro/settings/mcp.json` is the
     live agent's MCP server list. The rootdir conftest's `_isolate_shared_kiro_paths`
     redirects these from a table, and
     `test/test_host_isolation_floor.py::TestTheSharedKiroPathRatchet` fails when
     `src/kiro_crew` grows a module-level `Path.home()` binding that is neither in the
     table nor explicitly excluded with a reason. The guarantee is exactly that:
     **import-time bindings**.

     The LAZY half is **yours to isolate**, and the floor deliberately does not do it
     for you. `config.paths.kiro_home()` resolves on every call, so `kiro_agents_dir()`
     and `kiro_sessions_dir()` name the operator's real, machine-wide kiro-cli home.
     There are two levers and they are not interchangeable: `KIRO_HOME` (the documented
     production override, which also moves kiro-cli's session storage) outranks
     `Path.home()`, so pinning it at the floor would defeat the ~35 tests that isolate
     this resolver with `patch("pathlib.Path.home", return_value=tmp_path)` — they would
     read an empty directory instead of the tree they had just built. Use whichever the
     code path under test actually needs, per test.

     Getting this wrong is not loud. `test_kas_spawn.py` projected the developer's
     *installed* agent specs, so its verdict depended on which agents were present and
     whether their `file://` prompt files still resolved; it failed with an
     `AcpRuntimeError` naming a prompt file in an unrelated worktree. It is a write path
     too — `ensure_agent_materialized` targets that directory, and only its
     ephemeral-instance refusal ("This instance will use the existing specs instead")
     keeps tests out of the operator's live `~/.kiro/agents/`.

     The floor pins neither `Path.home()` nor `$HOME` either, so a path built from
     either without going through a resolver is also yours.

     Two exclusions are excluded for **opposite** reasons, and the distinction
     matters: the launchd paths are excluded because another fixture already
     redirects them, while the file browser's allow-list root
     `file_explorer/server._HOME` must **never** be redirected — it is a
     security anchor whose whole point is naming the real home. **Stub the reader,
     never move the anchor.** Redirecting a matcher so a test can pass makes it assert
     against a pattern that no longer matches the thing it protects.

- **Never leave the process working directory somewhere else.** The CWD is
  per-PROCESS, so under xdist one test's `os.chdir` becomes every later test's starting
  directory on that worker. Use `monkeypatch.chdir`, which reverts on its own; the
  rootdir conftest's `pytest_runtest_teardown` puts it back either way.

  This was survivable only while the directory outlived the run. With
  `tmp_path_retention_policy = failed` pytest removes a passing test's `tmp_path` at
  that test's teardown, so a test that chdirs into `tmp_path` and does not come back
  leaves the worker sitting in a **deleted** directory — and then `Path.cwd()` raises
  `FileNotFoundError` in every later test that reaches it, including from inside
  production code (`taskrunner.TaskRunner.__init__` does `work_dir or Path.cwd()`).
  MEASURED: that one leak produced the large majority of a 124-failure run, spread
  across ~10 files that every one of which passes in isolation — which is exactly why
  it reads as "the suite is flaky" instead of as one test missing one line.

- **A child process inherits pytest's CWD, which is the repo root.** A spawn that may
  create a file therefore writes into the checkout unless it is given
  `cwd=` under `tmp_path`. Scope the assertion to where the child actually ran, not to
  where you hoped it wrote: an assertion against `tmp_path` passes vacuously while the
  file lands in the repo, and neither the test nor the residue check attributes it to
  this test.

- **A singleton with a background thread beats every filesystem cleanup.** `sel.py` is
  the worked example: `SecurityEventLog` is a process singleton whose writer is a
  *daemon thread*, and `_init_locked` binds its directory **once**, from whatever
  `_default_dir()` resolved at that moment. So whichever test calls `sel()` first fixes
  the directory for the whole worker, the thread keeps writing there after that test
  ends, and `_flush_batch` opens with `mkdir(parents=True, exist_ok=True)` — which
  **re-creates the directory after the test's own tearDown removed it**. MEASURED: that
  is what left one stray `mkdtemp` directory behind on every run of the
  ops-mission-control suite, and the stack came from `sel-writer`, not from any test.

  The fix is not tidier cleanup — no cleanup can win against a thread that rebuilds
  the path. It is to give the singleton a **session-scoped** directory that belongs to
  no individual test (`_isolate_sel_default_dir`, in the rootdir conftest). When you
  add a subsystem with a background worker, ask which directory its thread captured
  and whether anything deletes that directory underneath it.

  One shared directory also means one shared **chain lock**, and that is the wrong
  tier for a test whose assertion depends on a fail-closed critical SEL write
  *winning* that lock — on the event-loop thread the acquire is a single non-blocking
  attempt, so any sibling's writer holding the lock at the wrong moment refuses the
  audit and fails the test with no code defect anywhere (issue #7029, the issue-radar
  trust flake). Such tests request `sel_private_root` (rootdir conftest): it rebinds
  the singleton to a per-test, per-xdist-worker directory built `sync=True` — no
  background writer at all — so no concurrent writer exists to contend with.

  The **lazy-resolving worker** is the second shape, and it writes into the operator's
  REAL home rather than a stray temp dir. `install_receipt.dispatch()` handed the
  receipt write to a daemon thread that called `beacon.config_dir()` *on that thread*.
  `config_dir()` honours `KIROCREW_HOME` at call time; the test's pin was gone by the
  time the thread ran, so `~/.kiro/crew/app_receipt_secret` appeared on the developer's
  machine (MEASURED, 1 of 5 runs — the per-test probe showed the
  `kirocrew-install-receipt` thread alive after teardown in 3 of 5). Two rules follow:
  **resolve every environment-derived input on the dispatching thread and pass it
  in** — the data home AND the config fields, since `KiroCrewConfig.load()` honours
  `KIROCREW_HOME` exactly as `config_dir()` does — so the worker reads nothing from
  the environment; and **make the worker joinable and join it structurally**: the
  rootdir `pytest_runtest_teardown` hook calls `wait_for_pending_receipt_writes()`
  before any fixture (including `monkeypatch`) is torn down, so no test has to
  remember it. A thread you cannot join is a thread you cannot isolate.

- **A cwd-relative default in a constructor is a write into the checkout.**
  `TaskRunner(work_dir=None)` falls back to `Path.cwd()`, which under pytest is the repo
  root, and its first save wrote `runs.json` there. The rootdir conftest's repository
  residue guard did not flag it because the name happens to be gitignored — so a
  gitignored artefact is exactly the one that leaks silently. Always pass
  `work_dir=tmp_path` (or the equivalent) to anything whose default is the process CWD,
  and when you add such a default to production code, add the test that constructs it
  with an explicit directory.

- **Production code that edits `os.environ` leaks through a test that exercises the real
  path.** `dashboard/server.py` startup does `os.environ.update(cli_env_overrides())` on
  purpose (descendant `playwright-cli` processes need it), and `load_credentials()`
  `setdefault`s every `.env` key. A test that drives that real startup — via a shared
  helper like `_start_dashboard` — inherits the mutation for every later test on the
  worker. The per-test probe in the 5x run caught `PLAYWRIGHT_MCP_OUTPUT_DIR`,
  `KIROCREW_TELEMETRY`, `PATH`, and a test's own `TEST_CRON_VAR` surviving teardown
  across ~50 tests. Snapshot the keys the production path is known to touch with
  `monkeypatch.setenv`/`delenv` **in the shared helper**, so every consumer is restored;
  never `os.environ[...] =` in a test body.

- **A path that "cannot be created" has to be made uncreatable, not spelled that way.**
  `test_mcp_gateway_oversize` pointed `KIROCREW_HOME` at
  `/nonexistent/path/that/cannot/be/created` to prove the spill degrades when the
  sidecar dir cannot be made. On Windows a leading slash is drive-relative, the path
  resolved to a writable `C:\nonexistent\...`, the spill *succeeded*, and a 300 KiB
  sidecar sat at the drive root for weeks — where it turned `install_app("/nonexistent/path")`
  in `test_app_manager` into a real directory and a second, unrelated red. Put the
  blocker under `tmp_path` as a regular **file** and use a path beneath it
  (`tmp_path / "blocker" / "home"`): `mkdir(parents=True)` fails on every platform, and
  the test can assert afterwards that nothing beneath the file exists.

- **A test that computes a budget pins every reading the caller could have inherited.**
  Kiro Crew seeds `PYTEST_XDIST_AUTO_NUM_WORKERS` at every agent spawn boundary
  (`resource_status.inject_xdist_auto_cap`), so a pytest run started from an agent shell
  carries the spawner's cap. Seven budget tests asserting "a 10-core host gets 10"
  read 7 there and were red for a reason that had nothing to do with the host. Whatever
  `resolve_workers()` consults from the environment — the max-workers knob AND the xdist
  cap — is `monkeypatch.delenv`'d in the file's autouse fixture; the tests that are
  *about* a ceiling set it themselves.

- **When you stub a lifecycle method, SPY and delegate — never replace.** A stub that
  only records the call leaves whatever that method was supposed to stop still running.
  The worked example cost 19 failures in files that contain no metrics code at all:
  three tests in `test/metrics/test_provider.py` needed to observe *that* the provider's
  `shutdown` was called and on which thread, so they replaced it with a recorder. The
  real `shutdown` is what stops OpenTelemetry's `PeriodicExportingMetricReader`, so its
  exporter thread stayed alive for the life of the xdist worker — and it cannot be
  cleaned up by dropping references, because the thread's target is a bound method of
  the reader it keeps alive.

  What that one thread then broke is the part worth remembering, because nothing about
  it is local: the OTel SDK registers an `os.register_at_fork(after_in_child=…)` hook
  that **restarts** the exporter thread in every fork child. The sandbox's userns probe
  forks, and `unshare(CLONE_NEWUSER)` implies `CLONE_THREAD`, which the kernel refuses
  with **EINVAL unless the caller is single-threaded**. EINVAL is indistinguishable from
  a kernel built without `CONFIG_USER_NS`, which is permanent, so the worker cached
  "this host has no sandbox backend" and every later sandboxed spawn on it failed
  closed. Diagnosis went: 19 `SandboxUnavailableError`s in two app suites → each file
  passes alone → the probe child had 2 threads, every time.

  Two guards came out of it. The rootdir conftest fails the test that leaves an
  exporter thread running (`_no_leaked_telemetry_exporter`, reported once per worker so
  one defect cannot red the shard), and the probe reports a multithreaded child as its
  own transient condition instead of letting an ambiguous EINVAL be cached as a verdict
  about the host. Neither replaces the rule: **anything you start, something must
  stop — and a stub is not a stop.**

  A second shape of the same hook survives even a clean shutdown: CPython cannot
  unregister an at-fork hook, so after a proper `shutdown()` the hook still runs in
  every fork child and restarts a ticker thread that exits almost immediately. Each
  fork then races that short-lived thread independently — one fork child can count 1
  thread while the next counts 2. The consequence for tests: **a single-threaded
  pre-check fork proves nothing about the fork that produces the verdict.** A guard
  for the multithreaded collapse must read the collapse off the verdict itself (the
  probe's reason names it; `sandbox._probe_reason_is_multithreaded_collapse`), not
  predict it from a separate probe.

- **A handler that answers before its work finishes must be awaited, not slept on.**
  `api_chat_slot_slack_link` returns 200 as soon as the link is persisted and hands the
  Slack backfill to `asyncio.create_task`, tracked in `state._background_tasks`. Six
  tests asserted on what that task did without awaiting it, which passes or fails purely
  on how the loop was scheduled: on a loaded CI shard it surfaced as
  `'NoneType' object has no attribute 'args'` on a **different test each run** (#4130),
  which reads as a flaky suite rather than as a missing `await`. Use
  `chat_test_helpers.drain_background_tasks(state)`, which awaits to a fixed point and
  re-raises; exiting the `TestClient` block is not a synchronisation point.
- Tests MUST NOT reconfigure or restart a real host service. This is enforced,
  not just asked for: the **rootdir** `conftest.py` (distinct from
  `test/conftest.py`, which only applies to `test/` — `testpaths` also collects
  `transfer` and `src/kiro_crew/apps/builtins`) pins `$XDG_CONFIG_HOME` to a tmp
  dir so `dev_fleet._dropin_path()` cannot name the operator's real
  `~/.config/systemd/user/kirocrew-gateway.service.d/`, and traps every stdlib
  spawn funnel (`subprocess.Popen.__init__`,
  `BaseEventLoop.subprocess_exec`/`subprocess_shell`, `os.execve`) to
  refuse a `systemctl`/`launchctl` invocation carrying a **mutating verb**
  (`restart`, `daemon-reload`, `stop`, `enable`, `load`, `bootout`, …). Read-only
  queries (`systemctl show`, `cat`, `is-active`) are allowed and need no stub,
  and `systemd-run` is deliberately NOT guarded because `sandbox` wraps nearly
  every subprocess in `systemd-run --scope` for cgroup limits — the guard keys on
  the verb, so it still catches `systemd-run … -- systemctl restart …` on the
  inner token. A test that reaches the make-live cutover path must stub BOTH
  `_run_cmd` and `_dropin_path`. Issue #1722: a test asserting that a staged
  cutover could be *cancelled* rewrote the developer's real unit to point into
  its own pytest temp dir, and systemd then looped on `203/EXEC` for 25 minutes
  after that dir was deleted. `test/test_host_service_guard.py` ratchets the
  guarded set against the service tools `src/` actually names, so a new
  host-mutating call site cannot land outside the floor.
- **Register the destruction of anything you create, in the same scope.** Prefer
  pytest's `tmp_path`. If you must call `tempfile.mkdtemp()`, pair it with
  `self.addCleanup(shutil.rmtree, path, ignore_errors=True)` **on the next line** —
  not with an `rmtree` in `tearDown`, which is the shape that leaks:

  ```python
  # WRONG — unittest does NOT run tearDown when setUp raises, so this leaks on
  # every setUp failure, and it is the failing run nobody watches that leaves it
  def setUp(self):
      self.tmp = Path(tempfile.mkdtemp())
      self.client = build_client()          # raises -> tearDown never runs
  def tearDown(self):
      shutil.rmtree(self.tmp, ignore_errors=True)

  # RIGHT — registered immediately, runs even if the rest of setUp blows up
  def setUp(self):
      self.tmp = Path(tempfile.mkdtemp())
      self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
      self.client = build_client()
  ```

  The rootdir conftest contains the *class* as well: `tempfile`'s base is redirected
  per run to `<platform temp>/kc-pytest-<user>-<pid>`, which the run removes at the end,
  so an unregistered directory no longer accumulates in the shared temp root forever.
  Residue there is still **reported** — relocation is not absolution.

  On **macOS** `<platform temp>` is forced to `/tmp` (`_SHORT_TMP_BASE`), which is what
  Linux and CI already resolve to. launchd's per-user temp dir is
  `/var/folders/<2>/<30 random>/T`: long enough that an AF_UNIX socket under a pytest temp
  dir exceeds Darwin's 104-byte `sun_path` and cannot bind at all, and random enough that
  the path clears the credential redactor's entropy floor — `/` is inside its
  `[A-Za-z0-9+/]{40,}` run, so a temp path is one contiguous match and comes back
  `[REDACTED: credential]`. Both are properties of the host prefix rather than of the code
  under test, and both used to fail ~13 tests locally while CI stayed green.

  A run only ever deletes the root it created itself — there is deliberately no sweep of
  other runs' roots, because every signal for "that directory is abandoned" is unsound from
  inside a test process: the name can be pre-created by another local account, and a pid
  means nothing across PID namespaces (two containers sharing a bind-mounted temp directory
  can each hold the same one). So **a run killed before its teardown leaves one directory
  for the platform to reclaim** — `systemd-tmpfiles` on a timer, macOS's periodic cleanup, a
  tmpfs cleared on reboot. That reliance is deliberate and is worth knowing if you own a
  long-lived CI host: it is bounded at one directory per killed run.

  Reported, not yet fatal, and that split is a staged rollout rather than a soft opinion.
  Two classes under that root are deliberately **not** residue and are excluded by name:
  the computer-use screenshot spool, which production keeps as a persistent ring buffer,
  and the scratch that Chromium and the Playwright driver create because a child inherits
  the redirected `TMPDIR`. What remains is a handful of single `mkstemp` **files**, some
  of them written by production code a test merely reached — one inode each, not the
  `mkdtemp` directories the rule is about. Failing the suite on that set today would
  block every unrelated change while it is attributed, and a guard that blocks unrelated
  work is a guard somebody deletes. Set `KIROCREW_TMP_RESIDUE_STRICT=1` to make it fatal,
  which is how the remaining set gets burned down and how the line gets held afterwards —
  the same shape as `windows-expected-failures.txt`.

  Why it is worth a guard rather than a convention: `/tmp` is commonly a tmpfs with a
  fixed **inode** budget (1,048,576 on the hosts this was measured on), and it returns
  `ENOSPC` to every other process on the machine while **90% of the bytes are still
  free**. MEASURED on one such host: retained pytest basetemps alone held 249,550
  inodes, a quarter of the whole budget — which is why `setup.cfg` now sets
  `tmp_path_retention_policy = failed`, keeping a `tmp_path` only for the tests whose
  directory anyone actually opens.

  **Finding the culprit.** The residue report runs in a session-fixture teardown, so it
  is attributed to the last test the worker ran, which is almost never the guilty one.
  Re-run the suspect subset with `KIROCREW_TMP_PER_TEST=1` and each residue name
  becomes the id of the test that leaked it:

  ```bash
  KIROCREW_TMP_PER_TEST=1 pytest src/kiro_crew/apps/builtins/<app>/tests -n0 -q
  # AssertionError: 1 temporary entry outlived this run under /tmp/kc-pytest-you-951504:
  #     test_provider_listing_never_contains_a_token/tmpw2kvty2z
  ```

  That mode is off by default because a directory per test is exactly the per-test cost
  the fixture audit below exists to avoid.

- **A missing host capability is guarded on the TEST, never deselected on the file.**
  `--deselect` is the wrong tool three ways: it is invisible in the run output, it takes
  the file's other tests with it, and nothing goes red when its reason expires. A
  `skipif` names the capability, on the test that needs it, in the report.

  Measured: eleven files were deselected from every CI backend invocation because a GH
  runner denies `unshare(CLONE_NEWNS)`, which kept **608 tests** — the ops autonomy gate
  among them — off every pull request. The sandbox-dependent tests inside them already
  carried `skipif(not userns_available())`, so 85 would have skipped and **523 would have
  run**, on Linux, Windows and macOS alike. The reason had also expired: the "~6 minutes
  against a real git" that justified keeping them out was the launcher's hardlink scan
  arming on every spawn, and all eleven now run in 38s.

  Two mechanisms replace it, both of which say what they exclude:
  `test/windows-expected-failures.txt` for a per-node-id Windows gap, and
  `skipif(not userns_available())` for the sandbox. `test_coverage_omit_contract.py`
  ratchets the rest: a returning `--deselect` fails it unless the coverage omit comes
  with it, because a file CI cannot run must not be charged to the denominator either.

  Entries in `windows-expected-failures.txt` are **plain node ids** — file, class,
  function, no `[params]` and no `@group` suffix. The rootdir `conftest.py` matcher
  reduces both the list and each collected item to that base form before comparing
  (`_base_nodeid`), which is load-bearing: under the default `--dist loadgroup`, xdist
  rewrites a grouped test's nodeid to `<nodeid>@<group>`, so a matcher that only split
  on `[` matched a *different* string for grouped vs ungrouped tests and for `-n0` vs
  `loadgroup` runs. Never add the `@group` suffix to an entry — it makes the line match
  in one invocation and silently miss in another.
- Tests SHOULD be fast (< 1s each)
- Async tests MUST use `@pytest.mark.asyncio` — and ONLY async tests. The mark on a
  plain `def` is accepted silently by pytest-asyncio strict mode and the test then
  asserts nothing it was meant to `await`; the warning it emits is easy to miss among
  thousands. A module-level `pytestmark = pytest.mark.asyncio` makes every sync test in
  the file wrong.

## Side effects: what a full run does to the host, and how to see it

Everything in the Rules above was learned one incident at a time. This section is the
systematic version: how to MEASURE what the suite does to the machine it runs on, and
the classes that measurement found on a clean `main` when it was first done (five full
backend runs, ~89.5k tests each, on one 32-core host).

### The measurement

Attribution by timestamp does not work: under `-n auto` thirty-odd tests are in flight
whenever a file changes, and the live gateway on a developer box writes the same
directories the suite must not. What works is an **in-process audit hook**, which names
the exact test and the exact stack for every write outside the sanctioned roots:

```python
# audit_home.py -- run: python audit_home.py -q -n0 test/test_foo.py
import os, sys, traceback
REAL = (os.path.expanduser("~/.kiro"), os.path.expanduser("~/workplace"), "/workplace")
def hook(event, args):
    if event not in ("os.mkdir", "open", "os.rename", "os.remove", "os.symlink"):
        return
    path = os.fspath(args[0]) if args and not isinstance(args[0], int) else ""
    if event == "open" and not any(m in (args[1] or "") for m in "wax+"):
        return
    if path.startswith(REAL) and "/scratch/" not in path:
        sys.stderr.write(f"### {event} {path}\n" + "".join(traceback.format_stack(limit=18)[:-1]))
sys.addaudithook(hook)
import pytest
sys.exit(pytest.main(sys.argv[1:]))
```

The same hook can watch `subprocess.Popen` (a spawn without `cwd=`), `socket.connect`
(any non-loopback address is a real network dependency), `os.kill` (a pid that is not a
child of the worker), and `socket.bind`. Run it as a pytest plugin under `-n auto` to
survey the whole suite, then re-run each suspect file under `-n0` to get a clean stack.
Two things the survey CANNOT tell you, both of which misled the first pass:

- **Peak RSS sampled from `/proc/self/statm` is in pages.** A test that fakes
  `os.sysconf` globally (`lambda _name: 65536`) also changes the page size the sampler
  multiplies by, so the worker "peaked at +20 GB" while its real maximum RSS was 114 MB.
  Fake `os.sysconf` for the ONE name under test and delegate the rest to the real
  function; measure memory with `resource.getrusage(...).ru_maxrss` in a `-n0` run
  before believing any per-test number taken under xdist.
- **A hit on a real path in the survey is not attributable to the test it landed on.**
  The breadcrumb pump below wrote under 106 different files' tests because the thread
  ran whenever it got scheduled. If a file is clean under `-n0`, look for a background
  worker, not at the file.

### The classes it found, and the one correct fix for each

- **A background worker resolves its path when it RUNS.** `safety_override`'s breadcrumb
  publisher hands a job to a long-lived daemon thread; the job called `config_dir()`
  inside the worker, so it ran after the enqueuing test's `KIROCREW_HOME` monkeypatch was
  torn down and DELETED the operator's real `~/.kiro/crew/safety_override_last_grant.json`
  on every full run. Fix: resolve every path on the calling thread and pass it into the
  job (`_sync_breadcrumb` now closes over `_breadcrumb_path()`), and give the worker a
  drain: production's `flush_breadcrumb_writes`, wrapped by `test/conftest.py`'s
  `drain_breadcrumb_writes()` (which raises on a starved worker) and called at teardown
  while the pin is still in force. When you add a queue-fed worker, ask what it resolves
  lazily; the answer must be "nothing".
- **Import must not mutate the host.** `model_registry` and `acp.seed_provenance` called
  `config_dir()` at import to load a cache sidecar, and `config_dir()` is
  resolve-AND-maintain: it `mkdir`s the home and refreshes the recovery breadcrumb. Every
  test collector, and every read-only tool, therefore created `~/.kiro/crew`. Fix:
  `config.paths.peek_data_home()` resolves the same home without creating it; readers use
  it, writers keep `config_dir()`. `test_model_registry.py::TestImportDoesNotCreateTheDataHome`
  imports the package in a fresh interpreter with an empty `$HOME` and asserts nothing
  appeared.
- **A second default that the data-home pin does not cover.** `workspace_root()` falls
  back to `~/workplace/kirocrew-workspace`, not to anything under `KIROCREW_HOME`, so 19
  files created the operator's real workspace and three wrote real `cli.json`/outbox files
  into it through `create_provider_factory(session_key=...)`. `kiro_sessions_dir()` is the
  same shape for kiro-cli's transcript store: session teardown deleted `sA.json` from the
  operator's real `~/.kiro/sessions/cli`. Both are now pinned per test by the rootdir
  conftest (`KIROCREW_WORKSPACE`, `_sessions_dir_override`), and
  `test_host_isolation_floor.py` ratchets them. When you add a resolver with its own
  default, add it to the floor and the ratchet in the same change.
- **The checkout's own git dir.** A watcher that ran `git -C ""` (an empty clone path)
  operated on whatever repository contained the process CWD — the real checkout's
  `.git/info`. An empty path is not "no repository"; refuse it (`pr_watchers` now returns
  early when no clone is configured).
- **Fixed `/tmp/<name>` paths race across files.** `test_review_pool.py` wrote `/tmp/x`
  and `test_deploy_round3_fixes.py` `rmtree`'d it; under xdist whichever ran second
  decided the other's outcome. There is no fixed name that is safe under `-n auto`; use
  `tmp_path`, and monkeypatch the constant at its defining module when production owns
  the name.
- **Writes into `src/`.** Skill registration created symlinks inside
  `src/kiro_crew/apps/builtins/*/skills/`, a task runner wrote `runs.json` relative to
  CWD, a child interpreter left `__pycache__` in the tree, and hypothesis kept its
  example database at the repo root. `pytest_configure` now points hypothesis at the
  per-user cache dir (`~/.cache/kirocrew/hypothesis`), where its shrunk counterexamples
  still persist across runs; the rest are the CWD rule above, applied.
- **Real network.** Five system-handler test files reached `8.8.8.8:80` (a local-IP probe
  in `handlers_system`), the Webex client fetched `webexapis.com`, and the Slack config
  save handlers validated a pasted secret against Webex and Azure AD. Each passes on a
  connected host and fails on a firewalled runner. Stub at the seam the code reads
  (`handlers_system.socket.socket`, `fetch_message`, `_validate_webex_token`).
- **A module-global set of asyncio tasks outlives its loop.** `source_providers`
  tracked visibility-refresh tasks in a module-global set whose done-callback never fires
  for a task whose loop was torn down under it; the next test, on a fresh loop, gathered
  the set and got `Future belongs to a different loop`. Production now prunes entries
  bound to a loop that is not the running one before adding; the test module clears the
  set per test. Any module-global collection of futures/tasks needs both halves.
- **Ratchets that re-parse the tree per test.** Fourteen files `rglob`+`ast.parse`d all
  ~1,300 modules under `src/` once per TEST (15–30 s each, ~10 CPU-minutes per run).
  Cache the derived facts once per module: `test/source_corpus.py` for scans its filters
  fit, or an `lru_cache` keyed on the tree root (so a test that points the scan at a fake
  tree under `tmp_path` gets its own entry). Bound the retention — the corpus helper
  exposes `_clear_caches()` for a module-scoped teardown, because ~160 MB of parsed source
  held for the life of the worker is paid by every later test on it. And mark the module
  as one `xdist_group` (see "Keeping the suite fast"): a per-module cache that xdist
  spreads over five workers is warmed five times.

The second full-run audit (five backend + five frontend runs against a clean `main`)
found these further classes. Each one passed on the host that wrote it.

- **A thread count that rises is not yet a leak.** The per-test census flagged `+3` to
  `+8` threads on dozens of tests. Classified, every one was either a process-wide
  singleton pool warming up for the first time on that worker (`mc-gov`, `mc-embed`,
  `mc-subproc`, `sel-writer` — bounded, by design, and shared by every later test) or a
  loop's default executor thread still winding down after `loop.close()`'s
  `shutdown(wait=False)`. Neither grows without bound, and the worker end state is a
  dozen threads. Stopping a singleton per test to make the number go down is the
  round-one anti-pattern in reverse: it costs every later test a cold pool. Report a
  thread leak only when the SAME test, repeated, keeps adding threads.
- **The systemd user manager is host state.** `sandbox.cgroup_scope_argv` wraps a spawn
  in `systemd-run --user --scope --slice=kirocrew-agents-<token>.slice`, and the token is a
  hash of the data home. The floor pins a fresh `KIROCREW_HOME` per test, so every test
  (and every `kirocrew` CLI child a test spawned, which probes for itself) that reached the
  wrapper created a NEW transient slice — and systemd never garbage-collects a slice. Five
  runs left 4,000+ `kirocrew-agents-*.slice` units loaded in the operator's user manager,
  and a late reconcile thread ran a real `systemctl --user set-property` on the operator's
  agents slice after its test's monkeypatch was undone. Fix: the rootdir conftest runs the
  whole session with no systemd user session (`XDG_RUNTIME_DIR` and
  `DBUS_SESSION_BUS_ADDRESS` removed — the probe's own documented gate, inherited by
  children, and CI parity). The one test that needs real enforcement opts in with the
  `real_user_session` fixture, which stops the slice it created on teardown. A test that
  wants to talk to the real user manager has to name it.
- **`Path.home()` is not pinned, and a resolver that reads it reads the operator.** The pod
  boot test staged the operator's real `~/.local/share/kiro-cli` sign-in store into the pod
  home and then resolved and SPAWNED the real kiro-cli from `known_kiro_cli_dirs(Path.home())`
  — it passed alone and returned `EX_CONFIG` in every full run, because an earlier test on
  the worker had poisoned the sandbox probe. A dashboard-server fixture `mkdir`+`chmod`ed the
  real `~/.kiro/crew-auth-staging` through `KiroPrerequisiteService(home=Path.home())`. The
  data-home pin cannot reach either: they hang off HOME, not `KIROCREW_HOME`. A test whose
  code path resolves anything from the operator's HOME pins `pathlib.Path.home` (the
  classmethod) and `HOME` to a fake host home under `tmp_path`; a HOME-relative constant that
  production must keep (the sandbox hides `~/.kiro/crew-auth-staging` by that spelling) is
  rebound per test through the rootdir conftest's `_SHARED_KIRO_PATHS` table instead, which
  works even for a RELATIVE constant because `pathlib` drops the left operand when the right
  one is absolute.
- **A resolver that is unpinned ON PURPOSE.** `PodConfig.load()` roots `pods_dir` at the
  DEFAULT data home so a pod process finds the host's plane rather than its own
  `KIROCREW_HOME`; the pin therefore cannot reach it. Two refusal tests wrote
  `<name>.refused` into the operator's real `~/.kiro/crew/pods` — and were red on the
  macOS runner, where that directory does not exist and a best-effort note silently
  vanishes. A deliberately host-rooted resolver needs its own lever in every test that
  reaches it (`KIROCREW_POD_ROOT`, `KIROCREW_POD_ENV_DIR` under `tmp_path`).
- **A constant derived from a patchable one at import.** `design_tweak`'s `CONFIG_FILE` was
  computed from `DATA_DIR` at import; tests (and every pin) repoint `DATA_DIR`, so the
  registry write still landed in the operator's real app config and left it pointing at a
  pytest tmp path. Derive it at access time (a module `__getattr__`, or a function), and add
  a test that the written file sits under the pinned dir.
- **`monkeypatch.undo()` unwinds the FIXTURE's pins too.** A test that called `undo()` on
  the same `monkeypatch` its fixture had used to pin `KIROCREW_HOME` unpinned the data home
  mid-test, and its trailing `empty_trash()` wrote the real `~/.kiro/crew/trash` lock. Scope
  an ad hoc patch with `pytest.MonkeyPatch.context()`; never `undo()` a shared instance.
- **A fire-and-forget task drains after the pin.** `_start_channel_transports()` detaches
  `_replay_spooled_inbound()` on purpose; the test never awaited it, so it resolved
  `data_home()` after teardown and created the real `~/.kiro/crew/inbound-spool`. The
  drain `_shutdown()` already performs (cancel + `wait_for`) belongs in the test's teardown
  too — the same rule as the daemon-thread breadcrumb above, one layer up.
- **A read-only directory under `tmp_path` outlives the run.** A test `chmod`ed a
  directory to `0o555` and never restored it; pytest's `rm_rf` cannot unlink inside it,
  renames the tree to `/tmp/pytest-of-<user>/garbage-<uuid>/` and leaves it there forever,
  one per run. Restore the mode in a finalizer (`request.addfinalizer`).
- **A process-wide descriptor census is not a leak check.** Three tests compared
  `len(os.listdir("/proc/self/fd"))` before and after, or re-probed a closed fd with
  `os.fstat`; the xdist worker has ten-plus live threads (executors, the SEL writer) that
  open and close descriptors of their own, and a freed fd NUMBER is reissued to any of
  them. Assert the code's own open/close pairing: spy (wrap, never stub) the primitive the
  code uses (`os.open`, `tempfile.mkstemp`, `open_write_nofollow`, `os.close`) and assert
  every descriptor it opened was closed — or, for ordering, that the pinned fd's FIRST
  `os.close` happened before `rmtree` was entered. `test/test_bench_download_fd.py`,
  `test/test_session_image_repair.py` and `test/test_meetings_audio_import.py` are the shapes.
- **A module-global task set gathered across loops, second half.** Round one pruned tasks
  whose loop was CLOSED; a task from another still-live loop slipped through and
  `asyncio.gather` raised `attached to a different loop` once in five runs. Drain only what
  the running loop can await (`task.get_loop() is asyncio.get_running_loop()`); the test
  module carries the filter, since production never drains the set.
- **A test about a cold cache must make it cold.** `TestColdCacheModelFallback` asserted
  three fallback pushes, but `model_registry._ADVERTISED_MODELS` is a module global another
  test on the worker had warmed, so the id folded to the served spelling and one push went
  out. Pin the premise (`monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})`).
- **A probe that depends on the venv's own packaging.** `_pip_install_channel_available()`
  reads `importlib.util.find_spec("pip")`; a uv-created venv ships no `pip` module, so two
  tests that meant to exercise the PEP 668 branch failed on every uv host. Pin every probe
  the function reads, not just the one the test is about.
- **A test-only import that CREATES the data home.** `test/conftest.py` imports
  `slack.handler`, which built `_PHASE_EMOJIS` by calling `KiroCrewConfig.load()` at import —
  and loading resolves `config_dir()`, which `mkdir`s `~/.kiro/crew`. Import-time reads of
  the data home peek first (`peek_data_home()`) and load only when the file already exists.
- **Bytecode written into the checkout by import-by-path.** Loading a script with
  `spec_from_file_location` + `exec_module` writes `__pycache__` beside it —
  `packaging/signing/`, `.github/scripts/`. Wrap the `exec_module` in a scoped
  `sys.dont_write_bytecode = True`.
- **Unbounded `lru_cache`s in a script under test.** `scripts/leaf_test_scope.py` caches the
  text of every `.py` it scans, exactly right for one CLI run and wrong for a long-lived
  worker; the test module clears them at module teardown.
- **Electron: an unref'd backstop timer, and a lazy binary download.** `stopGatewayGracefully`
  bounded a never-settling tree kill with a `setTimeout(...).unref()`; an unref'd timer
  cannot keep the loop alive, so when nothing else was pending the loop drained before the
  backstop fired — 26 `node:test` cases cancelled on Node 22 (the declared floor), passing
  on the Node 24 CI runner by accident. A backstop the caller awaits must hold the loop.
  And `require("electron")` in a plain Node process runs `electron/index.js`, which
  DOWNLOADS the binary into `node_modules` when `dist/` is absent (electron 43 has no
  postinstall), so four test files raced the network on a fresh checkout. The `test` script
  now preloads `website/electron/test/_preload.cjs`, which sets `ELECTRON_OVERRIDE_DIST_PATH`
  before any source loads. Details: [website/docs/testing.md](../../../website/docs/testing.md).
- **The basetemp can sit INSIDE a guarded root.** A Kiro Crew agent session sets `TMPDIR`
  to its scratch dir under `~/.kiro/crew/scratch/`, so pytest's basetemp — and every
  correctly pinned home — resolves inside the real `~/.kiro` while touching nothing of the
  operator's. The sessions-dir fence and `test_host_isolation_floor.py`'s guard therefore
  treat this run's own basetemp and `tempfile` root as test-owned (`_test_owned_roots`)
  and still catch a pin that escapes to the real tree. Sixteen tests were red in every
  agent-driven run before this.

The third full-run audit ran five backend and five frontend runs on a **macOS** host,
from inside a Kiro Crew agent session, with the audit hook attributing every write,
spawn, connect and kill to a test and a per-test census of duration, RSS, threads and
descriptors. Both changes of venue mattered: the two earlier audits ran on Linux, and a
suite that is clean there had 227 deterministic failures and four 120-second hangs on a
Mac, plus host writes the Linux runs could not see. Zero backend flakes in 5 × 92k
tests; the classes below are what the rest was made of.

- **`monkeypatch.undo()` in a test body unwound the floor, and the order of the two
  stacks was luck.** The same class the second pass closed with `_floor_monkeypatch`
  (below); this pass caught `detect()` creating the real `~/.kiro/crew` through it and
  added the missing half. Two independent `MonkeyPatch` stacks only nest correctly when
  the floor's is set up BEFORE the test's `monkeypatch` and torn down AFTER it, and
  autouse ordering across three conftests does not promise that. The rootdir conftest
  therefore re-declares the `monkeypatch` fixture with `_floor_monkeypatch` as a
  dependency, so the order is a dependency edge, not a convention.
  `test_host_isolation_floor.py::TestTheDataHomeIsPinnedForEveryTestpath` pins both
  halves: `undo()` in a test body leaves every pin in force, and the fixture in force is
  the rootdir override. Never `undo()` a shared instance to lift one patch; use
  `pytest.MonkeyPatch.context()`.
- **A pin that lives in `test/conftest.py` is not a floor.** `KIROCREW_PROFILE=standalone`
  was pinned there, so the ~108 modules under `src/kiro_crew/apps/builtins/*/tests/`
  never saw it. A Kiro Crew agent session exports the enterprise `KIROCREW_PROFILE` to every
  child it spawns; with no companion installed that profile FAILS CLOSED, and 150+
  builtin-app tests were red (every governance-gated route 500, every gated
  notification dropped) while `test/` was green. The pin is a rootdir autouse fixture
  now (`_reset_platform_context`). The rule generalises: anything an operator's shell
  can export that changes what production resolves is pinned at the ROOTDIR, and
  `test_host_isolation_floor.py` asserts it for every testpath.
- **A metric emitted at import builds the recorder from the operator's config.**
  `ToolHookResult.allow()` as a module-level DEFAULT ARGUMENT ran during collection,
  before any pin existed; the recorder's first build read the real `config.json`
  (`telemetry.enabled: true`) and started a `PeriodicExportingMetricReader` bound to
  the real `~/.kiro/crew/metrics`, which exported every minute for the life of each
  xdist worker (four worker pids' shards in the operator's metrics dir per run). The
  per-test exporter-leak guard cannot see it (the thread predates every test) and the
  per-test env pin cannot reach it (already built). Two fixes: `pytest_configure` pins
  `KIROCREW_TELEMETRY=0` for the whole PROCESS, so even an emitter nobody has named
  yet builds a no-op recorder; and `pytest_make_collect_report` records every module
  whose collection flipped `metrics.provider._ever_built` into
  `IMPORT_TIME_METRIC_EMITTERS`, asserted empty by
  `TestNoMetricIsEmittedAtImport`. Build such values inside the test or fixture.
- **A maintenance-pool job resolved its path when it RAN.** `cleanup_stale_sandbox_profiles`
  ran on the `mc-maint` executor and called `config_dir()` there; the test that queued
  it had torn down its pin by the time the thread was scheduled, so the sweep `mkdir`ed
  the operator's real `~/.kiro/crew` 60+ times per run and aimed its retired-snapshot
  `rmtree` and legacy-residue marker at the same tree. Round one's breadcrumb rule,
  one layer up: the caller resolves the home on ITS thread and hands it in
  (`SessionManager._cleanup_deps` → `cleanup_stale_sandbox_profiles(data_home=...)`).
  When a job goes onto a pool, ask what it resolves lazily; the answer must be "nothing".
  The `mc-maint` pool is the gateway's own, so `_join_test_loop_executor` (which joins
  the TEST LOOP's default executor at teardown) cannot reach it; the caller-resolves rule
  is the fix there. The loop's executor is covered: a cache write-through in
  `test_source_providers.py` scheduled a detached repo-visibility refresh whose
  `to_thread` resolved the provider CLI through `workspace_root()`; the join drains it,
  and the module also stubs the scheduler (a recorder, so the calls stay observable),
  because no test there is about visibility and a side task that never starts has
  nothing to drain.
- **Dropping the override to test the DEFAULT home resolves, and creates, the real one.**
  Five tests `delenv("KIROCREW_HOME")` (or `patch.dict(os.environ, {}, clear=True)`, or
  ran a module-scoped fixture BEFORE the function-scoped floor) and let `config_dir()`
  fall through to `~/.kiro/crew` plus the recovery breadcrumb beside it. A test of the
  default path relocates the default too: `monkeypatch.setattr(paths,
  "_resolve_default_home", lambda: tmp_path / "d")`, a fake `HOME` + `Path.home`, or
  keep `KIROCREW_HOME` in the cleared environment. Ratcheted:
  `pytest_runtest_teardown` reads `paths._resolved_home` BEFORE any fixture unwinds
  (a test that patched the global itself would otherwise restore the evidence first)
  and fails the test AFTER the floor has torn down, when it holds the operator's real
  home (`_refuse_a_resolved_real_default_home`). Relocating only the RESOLVER is not
  enough: `config_dir()`'s default path also writes `~/.kirocrew.breadcrumb` beside the
  home, through `Path.home()`, so a tmp stand-in for `_resolve_default_home` alone
  rewrote the operator's real breadcrumb to point at a pytest directory (six writes per
  run, repaired only because the live gateway wrote it back). The floor wraps
  `_write_recovery_breadcrumb` (`_breadcrumb_guard`): with `Path.home()` still the real
  home it fails the test, with a faked home it delegates. So fake the host home (`HOME`
  and `pathlib.Path.home`) and let every default derive from it. A module-, class- or
  session-scoped fixture (`setUpClass` included) runs OUTSIDE the function-scoped floor
  and pins what it resolves itself.
- **Bytecode written into the checkout, closed as a class.** Fifteen `__pycache__/`
  trees per run (`scripts/`, `packaging/signing/`, every skill's `scripts/`), each from
  an import-by-path that round two had closed one site at a time with a scoped
  `sys.dont_write_bytecode`. `pytest_configure` now sets `sys.pycache_prefix` (and
  `PYTHONPYCACHEPREFIX` for children) to `~/.cache/kirocrew/pycache`: every import's
  bytecode lands in a mirror tree under the cache root, still persistent across runs.
  A test that wants a module's stale bytecode gone locates it with
  `importlib.util.cache_from_source`, not by assuming a sibling `__pycache__/`.
- **A PTY close that deadlocks on macOS: a hang is a lost run.** `_kill_session` closed
  the PTY's controller descriptor first, to unblock the reader's `os.read()`. True on Linux
  (the read returns EIO), false on macOS/BSD, where `close()` WAITS for the outstanding
  read. With an interactive bash holding the terminal end, four terminal tests hit the 120 s
  timeout on every run, each parking a pool thread forever. The process tree is now
  hung up (SIGHUP: the signal a vanished terminal delivers, and the one an interactive
  shell does not ignore) and terminated BEFORE the controller end is closed; the tests run in
  under a second. A teardown that "unblocks" something by closing a descriptor has to be
  true on every kernel the suite runs on.
- **Linux-shaped tests on a Mac.** Ten distinct shapes, one rule: a test that asserts a
  platform behaviour pins the platform it means, or gates on the SAME predicate
  production gates on, never on `os.name == "nt"` alone. The frame recorder is
  Linux-only (`_require_acl_inspectable`), so its 75 logic tests pin the gate open
  (`IS_LINUX=True`, an `os.listxattr` that reports no ACLs) and only the two tests OF
  the gate flip it; the unnamed-inode (`O_TMPFILE`) prompt tests skip on the production
  capability flag `_UNNAMED_CREATE_SUPPORTED`; `/dev/fd/N` is a symlink Linux `realpath`
  follows and a devfs node macOS leaves alone, so a consumer path is compared by
  `open`+`fstat` identity; `unlink()` on a directory is `EISDIR` on Linux and `EPERM` on
  macOS, so `_discard_untracked_files` recognises both; a simulated `O_BINARY` bit is
  derived from the live `os.O_*` constants (`1 << 20` IS `O_DIRECTORY` on macOS, and
  every open became a directory open); a `--copies` venv cannot relocate a
  non-framework shared-lib CPython, so that test skips on `Py_ENABLE_SHARED` without
  `PYTHONFRAMEWORK`; the darwin-only workspace binding adds a `pass_fds` entry, so the
  test about the snapshot descriptor pins that seam to the no-descriptor shape;
  `sys.platform` patched to `"darwin"` on a real Mac lets `get_process_start_id`
  answer for the pid the test hoped was absent, so the start id is pinned; a
  `Path.mkdir` stub (record, do nothing) left the pinned data home uncreated for the
  macOS seatbelt priming that `lstat`s it, so it is a SPY that delegates; and a
  workflow's `sed -e '1{...}' -e '1,8{...}'` relied on GNU opening a numeric range on
  a later line (BSD only opens it on the exact one), so the two deletes share one
  `1,8{}` block.
- **Real network in a platform-specific test file.** `test_handlers_system_macos_paths.py`
  reached `8.8.8.8:80` through `_local_ip()`; the Linux siblings had stubbed it in
  round one, this file was never run there. Stub at the seam the code reads.
- **A probe that depends on the venv's packaging, again.** `transcribe_unsupported`
  folds in `_pip_install_channel_available()`, False in every uv-created venv; one
  test had not pinned it. Same rule as round two: pin every probe the function reads.
- **Frontend: a fetch chain `act` does not await, and a latch that lags its source.**
  `AutoNudgePopover`'s watch list is `fetch` → `json()` → `setState`, three promise hops
  after `await act(render)`, so the positive assertion flaked (1 in 5 runs) and every
  "not listed" assertion in the block was vacuous (absent BEFORE the fetch resolves
  whether or not the filter works). Render, wait for the mocked fetch to have been
  CALLED, drain the chain, then assert (`renderPopoverSettled`). And `App`'s startup
  video gate read a `startupInterruptionSeen` latch that an effect sets AFTER the commit
  showing the changelog, while `changelogDecided` is set one microtask later on the
  same fetch chain: when that microtask landed between the commit and its passive
  effects, the gate opened beside the changelog (2 in 5 runs). The gate now reads the
  live conditions in the same commit as well as the latch, the `onboardingOwed` shape.

Two ways to see all of the above on your own machine: run a touched file under
`trace_home.py`-style tracing (an `sys.addaudithook` that prints the stack of every
write under the real home — the recipe is in "The measurement" above), and compare
`systemctl --user list-units --all | grep -c kirocrew-agents` before and after a run.
On macOS the manager to compare is `launchctl list`, and the metrics dir to watch is
`ls -la ~/.kiro/crew/metrics`, where a shard named after a pytest worker's pid is the
import-time-emission class above. A thread that exists at the FIRST test's setup with
an exporter bound to the real home (`_prev` in an audit plugin's
`pytest_runtest_protocol`) was started at collection, and the plugin above names the
module in `IMPORT_TIME_METRIC_EMITTERS`.

### Coverage that only looks like coverage

- `AsyncMock()` for an object with SYNC methods: every sync call site then gets a
  coroutine it never awaits, and the test passes while `RuntimeWarning: coroutine ...
  was never awaited` is attributed to whichever later test triggers GC. Build the mock
  with `spec=` (`AsyncMock(spec=SessionManager)`) so sync attributes come back as
  `MagicMock`, or set them explicitly.
- A `skipif` whose predicate depends on load — a capability probe with a wall-clock
  timeout skipped two `test_worktree_create.py` tests in two of five runs. A skip that
  flips is coverage that silently comes and goes; compute the verdict once per session
  without a timeout.
- A resolver with a memo that an earlier test on the same worker warmed
  (`browser_cli.cli_path()` returned the developer's mise shim after `HOME` and `PATH`
  were pinned). Pin every input the resolver reads AND reset its cache in the test.

### What a second five-run pass found (Windows host, ~88k tests per run)

The measurement above was repeated on `main` two days later, on a Windows developer
machine, with a per-test probe (RSS, threads, environment, CWD) and a before/after
snapshot of the operator's home. Three files appeared in the real `~/.kiro/crew` on every
run, and each named a class the floor did not yet close.

- **`monkeypatch.undo()` takes the floor down with it.** `undo()` reverts EVERY record on
  the instance it is called on, and the rootdir floor used to patch through the same
  function-scoped `monkeypatch` a test receives. About ninety tests call `undo()` mid-way
  to drop one of their own patches before a final assertion; every one of them also
  unpinned `KIROCREW_HOME` for the rest of the test. `test_session_storage` then ran
  `empty_trash()` against the operator's REAL trash and left `trash/session-storage.lock`
  behind. Fix, structural: the floor fixtures patch through their own `_floor_monkeypatch`
  instance, undone at their own teardown, so a test's `undo()` reverts only the test's
  records (`TestTheFloorSurvivesATestsOwnUndo` ratchets it). Fix, local: a patch you need
  to drop before the test ends belongs in `with pytest.MonkeyPatch.context() as patched:`,
  never behind `monkeypatch.undo()` — the shared instance also carries every fixture the
  test requested (`stores` sets `KIROCREW_HOME` through it), and those are gone too.
- **A detached boot task resolves the data home after the test has returned.**
  `_start_channel_transports` schedules `_replay_spooled_inbound` with
  `asyncio.create_task` and never awaits it; the task runs `inbound_spool.peek_next`
  through `asyncio.to_thread`, and `spool_path()` inside it read `data_home()` on a worker
  thread that was still running when the starting test's pins had been undone —
  `~/.kiro/crew/inbound-spool/refused.jsonl.lock`, attributed to whichever test came next.
  Same shape as the breadcrumb pump above, one layer up: a detached task is a background
  worker. Fixed in production, at the calling side: the scheduler resolves
  `spool_path()` on the loop as it creates the task and hands the path in, so the
  worker thread reads a location fixed at boot rather than whatever the environment
  names when it happens to run (`TestInboundReplayResolvesItsSpoolWhenScheduled` pins
  it). When you add a detached task, resolve every environment-derived input where the
  task is scheduled, or give the tests a handle to await.
- **What a closed loop leaves behind runs after the pins are gone.** pytest-asyncio 0.20
  ends a test's loop with a bare `loop.close()`. Two things survive that: a task the
  code under test detached and the test never awaited — destroyed with the loop, its
  coroutine gets `GeneratorExit` at garbage collection, so its `finally` blocks run
  *then*; `_run_chat`'s queue-cycle `finally` reaches a synchronous
  `KiroCrewConfig.load()` — and a default-executor job (`asyncio.to_thread`,
  `run_in_executor(None, ...)`), which `close()` abandons without waiting. Either one
  resolving `config_dir()` after `KIROCREW_HOME` is unpinned creates the operator's
  `~/.kiro/crew` and refreshes the breadcrumb: a fresh fake `HOME` grew both after four
  subagent `on_done` tests, none of which failed. Structural fix in the floor's
  `tryfirst` teardown hook, beside the receipt-worker join: cancel every pending task
  and run the loop until they finish, then `shutdown_default_executor` — what
  `asyncio.run` does at shutdown — bounded, and before any fixture teardown so the pins
  still hold. A test that leaves an unstarted turn behind now shows up as a
  `coroutine ... was never awaited` warning at that point instead of as residue.
- **A default that bypasses the data-home pin BY DESIGN.** `PodConfig.load()` derives
  `pods_dir` from `_default_home()` — the operator's real `~/.kiro/crew/pods` — precisely so
  a pod running with its own isolated `KIROCREW_HOME` cannot redirect the host's pod
  registry, and `pod_root` from `Path.home()/.kirocrew-pods`. A fixture that was simply
  `PodConfig.load()` therefore recorded `viability-*.refused` notes into the real pod plane.
  The floor now pins `KIROCREW_POD_ROOT` and `KIROCREW_POD_ENV_DIR` per test
  (`TestThePodPlaneIsPinnedForEveryTestpath`); `test_pod.py` clears them deliberately
  because its subject includes the home-derived defaults, with `HOME` redirected first.
- **A collection-time probe reads the operator's config.** `test_app_backend.py`'s
  `_sandbox_can_spawn()` runs at import, before any per-test pin, and called
  `wrap_argv()` — which loads `KiroCrewConfig` from the REAL `~/.kiro/crew/config.json`.
  A developer box that carries `sandbox_allow_unsandboxed_exec=true` (redundant on Windows
  since the platform default already permits it, but common on a backend-less Linux box) made the probe say "can spawn", and the three tests it gates then ran
  under the fixture's default config and failed closed, while CI skipped them. A
  `skipif` helper must observe what the tests will observe: run it under an empty
  `KIROCREW_HOME`. The pattern to grep for is a module-level `def _can_*()` (or
  `_has_*`, `_probe_*`) used by a `skipif` whose body touches `KiroCrewConfig`,
  `config_dir()`, `data_home()` or `Path.home()`.
- **`monkeypatch.delenv` records nothing for an absent variable, and an after-the-fact
  `delenv` records the leaked value.** Both spellings were found around variables the code
  under test WRITES: `_export_bound_port` publishing `KIROCREW_BOUND_PORT`, `cli.main`
  pinning `KIROCREW_PROJECT_DIR`, the Webex save handler exporting the token, a cron
  preview applying `--env`, `load_credentials` propagating `OWNER_ID`. `delenv(name,
  raising=False)` BEFORE the write does not restore (pytest only records an undo for a key
  that existed); `delenv(name)` AFTER the write records the written token as the value to
  put back, so teardown re-instates it. Use `test/conftest.py`'s
  `forget_env_at_teardown(monkeypatch, *names)`, which records the pre-test state as the
  undo; the floor does the same for its four cleared names.
- **Production mutates `PATH` for the life of the worker.** The doctor's media section
  calls `transcribe.ensure_ffmpeg_in_path()`, which prepends a host-specific directory to
  `os.environ["PATH"]`; the first doctor test on a worker changed `PATH` for every later
  test. `TestDoctor` records `PATH` through monkeypatch (`monkeypatch.setenv("PATH",
  os.environ["PATH"])`) so it is restored whatever the doctor did.

Beyond the residue, five runs turned up exactly three tests that flipped between runs
with nothing in the host or the TEMP placement to blame, and the pull request's own CI
added a fourth; each was a real defect:

- **`os.replace` on Windows loses to a reader holding the destination.** Three tests in
  three files failed once each with `PermissionError: [WinError 5]` from the same line in
  `history_projection.py`, where the projection swapped a freshly written temp file over
  the live one. A scanner (the indexer's own reader, or the antivirus) that has the
  destination open for a few milliseconds is enough. Production fix: the swap goes
  through `atomic_write.replace_with_retry`, which already existed for exactly this
  and retries `WinError 5`/`32` briefly, off-loop only. The tests were right to fail.
- **A single-flight test that did not establish the concurrency it asserted.**
  `test_skills_catalog_cache` gathers eight readers of one catalog and asserts one
  assembly. The counting stub returned instantly, so the leader's executor job was done
  before the loop reached the `await` — Python 3.13 sets the wrapped future's state
  synchronously when the pool thread has finished — and awaiting a done future does not
  yield. The leader completed with a waiter count of one, offered nothing, and the second
  reader assembled again: `2 == 1`, once in five runs. The stub is now gated on a
  `threading.Event` released only after all eight readers are registered. The production
  coalescing was never wrong; "readers that arrive while a scan is in flight share it"
  is only testable while a scan is in flight.
- **A wall-clock ceiling sized for one pass, spent on three.** `test_pr_watchers`'
  three-pass clone test waited `WAIT_S` (10 s) for `exhausted`, and the failing snapshot
  showed `pass 3/3` complete with only the final status flip outstanding: a real clone
  plus three passes of several git subprocesses each, on a host shared with five other
  workers, is more than ten seconds on Windows. It now waits a named `WAIT_S * 3` with
  the reason next to it — class 5 above, not a stuck watcher.
- **Two `resolve()` calls that disagree by a prefix.** The CI Windows shard failed
  `test_work_ledger`'s four-threads-bind-one-worker test with one thread reporting
  `path traversal blocked for worker key` — for a key with no traversal in it. The
  guard resolved the child and the base in two separate calls; on Windows,
  `Path.resolve()` on a FILE another thread is replacing at that instant comes back as
  `\\?\C:\...` (`ntpath.realpath` drops the extended-length prefix only after a
  re-check that fails when the file has just been swapped), the directory resolves to
  `C:\...`, and `is_relative_to` reads the prefix as an escape. Reproduced locally in
  about four runs of ten by pointing the temp root at its 8.3 short name, the shape of
  the runner's `C:\Users\RUNNER~1`. Production fix: `session_ledger.resolved_within`
  resolves the base once, builds the child from it, and strips the prefix from both
  sides; the three ledger guards go through it. Ninety repeated runs pass.

### What the host lends the suite, and must not

The same pass found ~140 tests that pass on the CI runners and fail on an ordinary
developer machine — not flakes, but assertions about the HOST dressed up as assertions
about the code. Each is a hermeticity gap, and each has one fix:

- **A POSIX literal is not an absolute path on Windows from Python 3.13.**
  `ntpath.isabs("/opt/shims")` is True on 3.12 and False on 3.13 (a path without a drive is
  relative to the current drive), and production filters and validates paths with
  `os.path.isabs` — spec `PATH` entries, trusted binaries, upload roots, socket paths. A
  fixture spelled `"/usr/bin"` therefore exercised the REJECTION branch on 3.13. Spell
  fixture paths with `test/conftest.py`'s `host_abs("usr", "bin")`; judge a path that
  belongs to a SIMULATED platform with that platform's module (`posixpath.isabs` when the
  test set `sys.platform = "darwin"`). CI runs 3.12 only, so nothing there will catch it.
- **Python 3.13 dedents docstrings.** `__doc__` no longer occurs verbatim in
  `inspect.getsource()`, so a source ratchet that subtracted `func.__doc__` from the source
  left the docstring in place and flagged its own prose. Strip a docstring structurally
  (`ast.parse` → drop the first statement → `ast.unparse`), never by text replacement.
- **Trusted-directory resolvers versus per-user installs.** `platform_compat.trusted_git_bin`
  and the `gh` resolver deliberately refuse binaries outside fixed system directories; a
  developer's Git for Windows lives under `%LOCALAPPDATA%\Programs\Git`, so every real-repo
  assertion in `test_governance_updates` answered "unreadable git config" and the
  auto-update tests passed vacuously on the refusal branch. When the subject is what the
  seam does with the tool's ANSWERS, pin the resolver (to the fixture's own `git`, or to a
  fake absolute path when the spawn is faked); the resolver's own tests patch it explicitly.
- **`tmp_path` has ancestors.** A walk that runs to the filesystem root — the kirocrew
  launcher resolver's `.venv` search, `artifact_source`'s project-marker walk — finds what
  sits above the temp root: with `TMPDIR` inside a checkout that is a real
  `.venv/Scripts/kirocrew.exe`, and under `~/.kiro/crew/workspace` a `.kiro` marker, so
  "a plain directory" classified as a project and "no launcher anywhere" found one. Confine
  the walk to `tmp_path` at the validator (`launchers_confined_to_tmp`,
  `cap_project_root_walk`) rather than assuming the host's temp root is bare.
- **"A port nothing listens on" is a property of the host.** Endpoint agents on managed
  machines intercept loopback connects and answer every port with HTTP 200 (a SOAP envelope
  from `127.0.0.1:1`), so a test that provoked `transfer_unreachable` by POSTing to port 1
  got a delivered bundle instead. Model the connect failure at the client seam.
- **Real symlinks and long paths are capabilities, not platforms.** An unelevated Windows
  shell cannot create a symlink (WinError 1314); a stock one refuses a path past 260
  characters. Tests whose contract IS the link go in `test/requires-real-symlinks.txt`
  (89 added this pass — the conftest skips them only when the probe fails); tests that
  need a directory that resolves elsewhere use `make_dir_link` (a junction) and keep their
  Windows coverage; a test that needs a 240-character leaf probes the path first and skips
  on the host that cannot hold it.
- **`"python3"` is not on PATH on Windows.** Spawn the interpreter as `sys.executable`; a
  literal name fails with cmd's 9009 and every verdict downstream reads as a plain failure.
- **The interpreter decides where recursion gives way.** A test that pinned "decode
  succeeds but encode fails" for a 2,000-deep JSON body met an interpreter that did both;
  assert the invariant across all three outcomes, and walk a deep structure iteratively
  in the assertion itself.

## Running the suite: the defaults, and how to narrow safely

The checkpoint run is the whole suite with the configured defaults:

```bash
python -m pytest
```

`setup.cfg`'s `[tool:pytest] addopts` supplies `--verbose`,
`--ignore=build/private`, `-n auto`, `--dist loadgroup`, `--max-worker-restart=2`,
`--timeout=120`, `--durations=5` and `--color=yes`. Coverage is deliberately NOT in
`addopts`: measured on a 1,231-test subset it cost +21% wall time on every local and
agent run, while CI asks for it explicitly. So you no longer need an override just to
avoid coverage. (Coverage's cost is overwhelmingly TIME, not memory: re-measured
across three slices it added +33% to +160% wall clock but only +1.6% to +8.1% peak
worker RSS.)

### Running on a machine with little RAM

**A worker costs between 0.8 and 2.2 GiB depending on how many there are, and
`-n auto` would ask for one per core.** Almost all of the fixed part is *collection*:
every xdist worker independently collects every testpath — nearly 57,000 items —
which costs ~750 MiB of peak RSS before it runs a single test, 99% of it private, so
there is no page sharing to exploit. From there a worker grows another ~25 MiB per
1,000 tests it runs, and that growth does not saturate.

**Those two facts together mean per-worker cost rises as parallelism falls**, because
fewer workers each run more tests. Projected peak is `750 + (57,000 / N) × 0.0255` MiB:

| workers | tests each | projected peak |
|---|---|---|
| 32 | 1,780 | ~790 MiB |
| 8 | 7,100 | ~930 MiB |
| 2 | 28,500 | ~1.5 GiB |
| 1 | 56,900 | ~2.2 GiB |

That is why the reservation is 2 GiB per worker and why a measurement taken on a wide
run makes it look twice as generous as it is: a real `-n 8` worker peaks at
0.9–1.2 GiB, but sizing the divisor on that number would grant 6 workers on an 8 GiB
laptop, whose ~9,500 tests each would then want ~6 GiB between them. **Do not lower
the divisor on the strength of a high-parallelism measurement.**

Where that ~750 MiB goes, measured by ablation on one worker (a `--collect-only -n0`
run reproduces a real worker's peak to within about a megabyte, which is the cheap way
to re-measure it — 66 seconds instead of a five-minute `-n 32` run):

- **~77 MiB is spent before collection starts** — interpreter, pytest, its
  auto-loaded plugins, and the two conftests. The rootdir conftest alone is ~35 MiB;
  `test/conftest.py` adds the rest, mostly `hypothesis` and `kiro_crew.slack`.
- **~320 MiB imports the ~1,540 test modules** and, through them, most of
  `kiro_crew`. The package's ~960 modules cost ~145 MiB to import on their own, so
  the product is a sixth of the floor, not a rounding error — `import kiro_crew`
  alone is 2 MiB and is the wrong number to plan around.
- **~350 MiB is pytest's item tree**, ~6 KiB per item. Roughly half of that is the
  fixture closure, and the autouse guards in the two conftests are what fill it: they
  apply to every item, so each one costs ~106 bytes per item it reaches, and holding
  the closure to a single name per conftest level would drop the floor by 161 MiB.
  That is an accounting of the cost, not a licence to delete a guard — this is the
  host-mutation floor, so the only version of that saving is merging guards behind
  fewer fixture *names* while every guard still runs.

Every layer is live: the item tree, the closures and the rewritten modules are
retained for the whole session by design, so none of the floor is reclaimable.

So the full suite genuinely needs multiple gigabytes, and on an 8–16 GiB laptop with
a browser open it does not fit. The budget in the rootdir conftest works this out for
you and clamps `-n auto`, printing one line saying so:

```
xdist worker budget: 1 of 10 workers (3.0 GiB free, 16 GiB installed). Each worker
needs about 2 GiB, mostly to collect the suite. A run this narrow is slow, not
stuck -- free some memory, run a subset (pytest test/test_thing.py), or pass an
explicit -n <N> to bypass this budget.
```

It bounds the worker count by **two** memory readings, and the split is deliberate:

- **Total RAM and the cgroup ceiling** are constants of the machine, so they shape
  the shared *slot range* (see below) — two concurrent runs share one budget rather
  than each claiming it.
- **What is free right now** (`platform_compat.host_available_mib()`, which answers
  on Linux, macOS and Windows) throttles only *this* run. It is the reading that
  notices the 10 GiB your browser is holding, and it is why the budget protects a
  loaded laptop rather than only a small one.

Either reading returning 0 means *unknown*, and an unknown reading is **skipped**,
not treated as zero memory — a platform we cannot read keeps its parallelism instead
of silently dropping to one worker.

Concurrent runs coordinate through advisory locks under
`~/.cache/kirocrew/test-slots/<hostname>`, one file per worker a run intends to
spawn, held for the process's lifetime. The kernel releases them when the process
exits, so an orphaned or killed run frees its share with no cleanup logic. A run
arriving at a fully-locked machine drops to one worker: slow, never stalled.

The knobs, tightest-wins:

| Knob | Effect |
|---|---|
| `-n <N>` on the command line | Bypasses the budget entirely. xdist only calls it for `auto`/`logical`. |
| `--maxprocesses=<N>` | Clamps *after* the budget, so it can only tighten. |
| `KIROCREW_MAX_TEST_WORKERS` | Per-run ceiling, default 32. |
| `PYTEST_XDIST_AUTO_NUM_WORKERS` | xdist's own ceiling. Honoured here, because this hook replaces xdist's default implementation. Kiro Crew seeds it with a memory-aware cap at every agent spawn boundary. |
| `KIROCREW_TEST_SLOT_DIR` | Where the slot locks live. Point it at a throwaway dir to measure without contending with another run. |

If the suite is slow on your machine, the answer is usually not a bigger `-n`: run
the slice you are working on. A full-suite checkpoint is what CI is for.

**Narrow by FILE, not by `--splits`.** `--splits/--group` — pytest-split, which CI
uses to spread the suite across runners — deselects *after* the session has collected
everything, so a 1-of-4 shard still pays the whole floor in every worker while running
a quarter of the tests. Measured: 14,237 of 56,946 items selected, 744 MiB peak, which
is the unsharded floor. It buys wall time across runners, never memory on one machine.

What the floor actually tracks is the FILES a process is given. Measured on one
worker: 1,540 files → ~745 MiB, 770 → 477, 385 → 332, 193 → 226–252. So at equal
parallelism the aggregate is what changes, and summing the peaks of every process
says so: eight xdist workers each collecting all 1,540 files come to 5,945 MiB, while
eight single-worker processes given 193 files each — the same 56,946 items collected
once between them, and the same eight-way execution — come to 1,896 MiB, a 68% cut on
the machine as a whole. Two things make that a real runner rather than a one-liner,
and both fail silently if skipped: naming files on the command line bypasses
`collect_ignore`, so the runner must apply `test/windows-collect-ignore.txt` itself
the way `scripts/ci-surface-tests.py` does, and files sharing an `xdist_group`
(`subprocess_spawn`, `mcp_gateway`, `serial`) must land in the same process or they
lose the serialization the mark exists to provide.

### Where the temp root points, and what else is running

Two things about the HOST decided the outcome of a full run before any test did:

- **`TMPDIR`/`TEMP` must not sit under `~/.kiro` or inside a checkout.** Every temp root
  in the suite derives from it, so `tmp_path` inherits its ANCESTRY: under
  `~/.kiro/crew/workspace` the isolation floor's own self-tests fail (the pinned home is
  "a real home path"), the file-explorer and design-tweak suites classify every fixture
  as sensitive or as inside a project, and a walk that runs to the filesystem root finds
  the checkout's `.venv`. About a hundred false reds, none of them defects. An agent
  shell here pre-seeds exactly that (`TEMP` under `~/.kiro/crew/scratch`); export a
  short neutral root (`C:\kc-tmp`, `/tmp/kc`) before a full run.
- **Do not co-schedule the backend suite with `vitest run --coverage` on one machine.**
  Eight xdist workers at ~1.8 GiB each plus twelve coverage forks exhausted a 32 GiB
  host with 10 GiB of page file: the workers died with `RuntimeError: can't start new
  thread` inside pytest-timeout (an INTERNALERROR that ends the whole run, not a red
  test) and vitest lost files to `Worker forks emitted error`, four runs out of four,
  with every worker otherwise healthy (≤19 threads, no RSS growth). Run the two suites
  back to back; the pytest-only run finished in 64 minutes at `-n 6`.

### A multi-test `--override-ini` MUST re-state the xdist flags

`--override-ini="addopts=..."` REPLACES the whole list. Anything you leave out is
silently gone, and two of the defaults are load-bearing:

- **`--dist loadgroup`** is what honors `@pytest.mark.xdist_group`. Under
  `loadgroup` the scheduling unit is a test's own nodeid unless it carries the mark,
  in which case the group collapses to a shared scope and those tests land on ONE
  worker. Drop the flag and the concurrency-sensitive tests that depend on
  serialization are scattered across workers, which produces flaky races rather than
  a clean failure. Nothing warns you.
- **`--max-worker-restart=2`** turns worker loss into a fast loud failure. Without a
  cap, xdist silently clones replacements up to `numprocesses * 4`: a 10-worker run
  quietly restarts 40 times, and on a host that has started swapping that is roughly
  20 minutes of zero progress and an empty log. Two replacements absorb a genuine
  one-off crash; past that the run is not going to finish.

When worker replacement itself ends in an xdist INTERNALERROR (exit 3, no
`short test summary info` at all -- the scheduler can die with a `KeyError` on a
replaced node), `test/conftest.py`'s `pytest_internalerror` hook prints an
`xdist run ABANDONED` banner to stderr replaying the crashed workers and the
tests they were running, so the red stays diagnosable. The run still exits
non-zero; the banner only preserves the report the crash would otherwise erase.

So any override that still runs MANY tests must carry
`-n auto --dist loadgroup --max-worker-restart=2`:

```bash
python -m pytest --testmon \
  --override-ini="addopts=-v --ignore=build/private -n auto --dist loadgroup --max-worker-restart=2 --durations=5 --color=yes" \
  -q 2>&1 | tail -25
```

### Selective execution with testmon

`pytest-testmon` tracks which source files each test touches and runs only the
tests affected by your changes. It is declared in `setup.cfg`'s `dev` extra (what
`make build` installs), not in `pyproject.toml`'s `dependency-groups` dev that CI
uses, so a CI-shaped environment will not have it.

```bash
# Only tests affected by the current changes.
python -m pytest --testmon --override-ini="addopts=..." -q

# Only the tests that failed last run.
python -m pytest --lf --override-ini="addopts=..." -q
```

The first `--testmon` run builds the dependency database, so it costs a full pass;
the wins come after.

### One file or one test: use `-n0`

Per-worker startup dominates a small selection, so parallelism makes a narrow run
SLOWER. One measured test took 36.9s under `-n 2` and about 1.4s under `-n0`.

```bash
python -m pytest test/test_dashboard_chat.py -n0 -q
python -m pytest -k "flush_segment" -n0 -q
python -m pytest -n0 -k test_name --pdb        # -n0 is also what makes --pdb usable
```

`-n0` on the command line overrides the `addopts` `-n auto` without replacing the
rest of the list, which is why a single-file run needs no `--override-ini` at all.

### Which to use when

| Scenario | Command |
|---|---|
| Iterating on one task | `pytest --testmon` with the full override above |
| Debugging a specific failure | `pytest --lf` with the override, or `-k "test_name" -n0` |
| One file | `pytest test/test_foo.py -n0 -q` |
| Small-RAM laptop | Run a subset. For a full run, let the budget clamp `-n auto` and expect it to be slow; do not raise it. |
| Checkpoint before committing | `scripts/check_black_formatting.py && scripts/check_subprocess_encoding.py && isort && flake8 && mypy && python -m pytest` |

## Determinism: the six flake classes

A test that fails on CI but not locally is almost always one of these. Each has one
correct fix; reruns and `sleep` increases are not among them.

### 1. Nondeterministic input

Feeding `os.urandom` / `random` / `uuid4` into an assertion that depends on a property
the RNG does not guarantee. A random opaque id is fine; a random *payload* asserted to
NOT match a pattern is a coin flip.

Fix: seed it. `random.Random(_SEED).randbytes(n)` keeps the payload high-entropy,
which is usually the property under test, while fixing the outcome. Verify the chosen
seed against the real predicate, and say in a comment that you did.

**The host is an input too, and a PID is the one that catches people.** `999999` is
not an impossible PID: Linux `pid_max` is 4194304, so on a long-running host it names
an ordinary live process. Two tests asserted its absence — one as "a dead gateway
whose entry must be pruned", one as "a value only a planted `ps` shim could have
produced" — and both went red on a host whose counter had passed it, the second while
accusing the shim of running when it had not. Fix by kind: for a PID the code *probes*,
pin the probe (`patch(..., "pid_exists", side_effect=lambda p: p != 999999)`); for a
PID that must never appear in real output, use a number no OS can allocate
(`99999999999`) rather than one that merely looks unused.

```python
# WRONG: ~1% of runs match a credential prefix and the exemption assert fails
body = os.urandom(20_000)
# RIGHT: same entropy, same code path, one outcome
body = random.Random(20260803).randbytes(20_000)
```

**Host MEMORY is the other one, and it fails with a misleading exception.**
`SubagentManager.spawn` refuses — returning before it registers anything in
`_tasks` — while the machine looks short of memory, and it does so twice: an
absolute floor (`check_memory_available` against `agent.spawn_min_memory_gb`) and
the posture tier (`cached_admission_check`, refusing while the cgroup-clamped
reading is CRITICAL). What makes it expensive to diagnose is that a refusal IS a
`SubagentInfo` — a done one carrying `error` — so `assert info is not None` still
passes and the test dies on the NEXT line, at `await mgr._tasks[info.id]`, with a
bare `KeyError` naming an id nothing else mentions. Measured on a CI runner with
~0.5 GB free.

Fix: pin the reading with `healthy_host_memory` (`test/conftest.py`), which any
file driving `spawn` opts into at module scope:

```python
pytestmark = pytest.mark.usefixtures("healthy_host_memory")
```

It pins only the HOST reading — a caller that names its own `path` is feeding the
`/proc/meminfo` parser a fixture file rather than asking about this machine, so
those tests still run the real function and a parser regression still goes red. A
test that is actually ABOUT either guard patches it in its own body, which lands on
top of the fixture and reverts to it.

Opt-in rather than autouse, because the pin is not free of consequence: the tests
that drive the probe with no `path` and stub `safe_read_file` underneath it —
`test_subagent_coverage.py::TestCheckMemoryAvailable` — never reach their own stub
once the reading is pinned. `test_subagent_spawn_host_pin.py` is what keeps opt-in
from decaying into "whoever remembered": a module that names `SubagentManager` and
calls `.spawn(` must be pinned or excluded with a reason, so the next spawning test
file cannot land unpinned.

### 2. Wall-clock races

Asserting a *rate* or a *count* that the host controls. Windows rounds `time.sleep` /
`Event.wait` up to ~15.6ms and a loaded runner starves threads, so "burn 0.25s at a 2ms
interval, expect ~125 samples" observed **one** sample in CI.

Fix: poll for the condition with a generous deadline, and keep the assertion. Never
extend a fixed sleep, which trades flakiness for wall-clock and still races.

```python
# WRONG: assumes the scheduler cooperates
do_work_for(0.25); assert observed()
# RIGHT: returns as soon as it is true, fails loudly if it never is
give_up_at = time.monotonic() + 30.0
while not observed():
    assert time.monotonic() < give_up_at, "never happened"
    do_work_for(0.05)
```

Where a test wants a timeout to *expire*, set it to `0` rather than a small value: the
same branch is reached with no clock dependency at all.

The commonest shape here is not a rate but **an unawaited task**: a handler that
answers before its work finishes leaves the assertion racing the loop. There is a
synchronisation point, so use it — `drain_background_tasks(state)` — and see the Rules
entry for what it looks like when you do not (a different test failing each run).

Two more shapes, both MEASURED in a 5x full-suite run on Windows:

- **A completion signalled from another thread.** `await handler(...)` returning does
  not mean everything the handler *scheduled* has run. `_sse_from_thread` hands the
  terminal `complete`/`failed` event to the loop with `call_soon_threadsafe` from a
  worker thread, so `assert sse.types() == ["complete"]` on the very next line saw `[]`
  in 1 of 5 runs (`test_auto_research_handlers_coverage`). Wait on the signal the test
  asserts on (`await _await_until(lambda: "complete" in sse.types())`), not on the call
  that eventually causes it.
- **Two clocks: fixtures on one, production on the other.** `NOW = time.time()` at
  module level is read when pytest *imports* the file; under `-n auto` the tests run
  minutes later, and production compares the fixture's `modified=NOW` against its own
  live `time.time()` recency cutoff. All ten `TestReconcile*` tests in
  `test_channel_slots` failed together in one run because every session had aged past
  the cutoff on the way from collection to execution. The defect is the *pair*, not the
  constant: either both sides read one clock, or neither reads a frozen one. The in-tree
  fix (`frozen_clock`) pins `time.time` to the module's `NOW` for every test that calls
  the real pass, which makes eligibility pure arithmetic and also stops an `== 0`
  assertion passing vacuously because a stamp aged out. Reading the clock inside the
  test instead is the weaker fix — it shrinks the gap to microseconds without closing it.

**Guess-the-latency sleeps are this class too.** `asyncio.sleep(0.05)` "to let the
first prompt register" is a bet that two awaits and a `to_thread` hop finish inside
50ms; on a loaded runner they did not, the guard the test exists to exercise was never
armed, and the test blocked on a turn nothing would ever complete — see
[class 6](#6-a-hang-is-a-lost-run-not-a-failed-test). Wait on the observable state
(`_await_routed`, an `Event`, the queue entry) and put a bounded `wait_for` around the
call whose *refusal* is under test, so a missed refusal fails at that line by name.

### 3. Leaked async objects

An `AsyncMock` standing in for a **synchronous** method (`StreamWriter.write`,
`stdin.close`) returns a coroutine nobody awaits. A `cancel()` that is never awaited
leaves a live task at loop teardown. Both surface as `RuntimeWarning: coroutine ... was
never awaited` / `coroutine ignored GeneratorExit`, attributed to whichever *later* test
happened to trigger the GC, so the reported test is rarely the guilty one.

Fix: `MagicMock()` for sync methods; `await` the task after `cancel()`, absorbing
`CancelledError`.

The other member of this class is a **module-level asyncio primitive** in production
code — a `Lock`, `Event`, `Future`, or an in-flight `dict` of tasks created at import.
`pytest-asyncio` gives every test a fresh loop, the primitive stays bound to the loop
that first touched it, and the next test to reach it fails with `The future belongs to a
different loop` or `Task ... got Future attached to a different loop` — in whichever
test happens to run second, so four different `test_public_repo_chip_status` tests took
turns failing across five runs. Either create the primitive lazily inside the running
loop, or give the test file a fixture that resets the module state before each test.

### 4. Order dependence and shared state

Under `-n auto --dist loadgroup` the scheduling unit is a test's **own nodeid** unless it
carries an `xdist_group` mark: `LoadGroupScheduling._split_scope` returns the nodeid
verbatim and only collapses to a shared scope for tests marked `@<group>`. So ordinary
tests are distributed freely and independently: which worker any given test lands on, and
which tests precede it there, changes run to run. That is exactly why cross-test pollution
surfaces as flakiness rather than as a reproducible ordering bug, and why an `xdist_group`
mark is the tool for a test that genuinely cannot share a worker.

Mutate process globals through `monkeypatch`, which reverts on teardown even when the
test fails. Raw assignment does not.

**Sharding does not just scatter this class, it hides it — so a full-suite run is the wrong
place to be finding it.** `ci.yml` slices the suite into duration-balanced `pytest-split`
groups, and a leaker only damages tests that land in the *same process*, so a leak whose
victim sits in another shard is not observable in PR CI at all. The release job runs the
suite whole and is therefore the first place it appears — as failures in files that have
nothing to do with the cause, at a point where the diff that introduced it is long merged.
Running the full suite more often narrows that window; it does not close it, because which
tests share a worker still varies run to run.

What closes it is a floor fixture per process-global chokepoint: snapshot at setup, compare
at teardown, restore to **what the test inherited** (not to a pristine value, so a leak from
an earlier test is not re-reported against every test after it). So when you introduce a new
process-global, ship its floor entry with it rather than relying on a full-suite run to
notice. Whether that entry also *fails* the test depends on whether reaching the global is a
defect: `_no_leaked_telemetry_exporter` fails, because nothing legitimately leaves an
exporter running; the CWD restore and `_restore_log_record_factory` restore silently, because
production really does `chdir` and really does install a record factory, and a test driving
that code cannot avoid inheriting it. Restore either way — the damage is to other tests, and
stopping it propagating is the part that is never optional.

### 5. Absolute time budgets on instrumented runs

Asserting a *duration* when the property under test is algorithmic **complexity**. Coverage
instrumentation multiplies the cost of every executed line — so the same un-regressed code
measured ~1.7s of CPU bare and >5s under coverage, and a shard that runs `--no-cov` passes
while an instrumented one fails **at the identical commit**. The tell is a timing test whose
verdict depends on whether coverage was enabled rather than on machine load.

`time.process_time` fixes only the other half: it removes co-tenant scheduling noise, but CPU time
still includes the instrumentation, so an absolute ceiling stays version-dependent.

Fix: assert the **shape**, not the magnitude — and prefer asserting it *deterministically*.
When the code under test has an instrumentation surface (a routing decision, a memoized
matcher, a countable set of engine invocations), assert on that: pin that the linear path
is the one taken, wrap the primitives, and require the invocation trace to be IDENTICAL
when the input doubles. That fails only on the property, never on the runner. A *timed*
doubling ratio is version-independent (a constant multiplier cancels) but still
runner-dependent: even on `thread_time`, frequency scaling and co-tenant cache contention
on a shared runner inflated a measured 3.0-bounded ratio to 3.2x with the property intact.
Reserve a measured ratio for code with no observable structure, and make its bound
generous — a real complexity regression is orders of magnitude, so a wide bound still
catches it. Raising an absolute budget instead banks the overhead as headroom and hides
the next real regression.

### Interleavings: name the point, do not sleep toward it

Some races are not reachable from outside the process. Which of two coroutines lands inside
the other's critical section is decided by who holds the event loop between two awaits, and
an HTTP client can only issue both requests and hope — so `await asyncio.sleep(0.05); assert
nothing_happened_yet()` is the shape these tests keep taking, and it is a wall-clock race
(class 2) dressed as a concurrency test.

Where a race matters enough to pin, the answer is a **test-only interception seam**: a
module-level `Callable[[str], Awaitable[None]] | None`, default `None`, awaited at named
points inside the paths that race. `chat_handlers._test_interleave` is the worked example,
with four points across the session-teardown paths (`reload:pre_reset`,
`switch:post_commit`, `reset:pre_pop`, `reset:post_pop`). A test suspends one racer at a
point by name and drives the other from there, so the interleaving is a property of the test
and identical on every host.

The rules such a seam follows, each of which it stops being safe without:

- **`None` by default, and settable only from tests.** No env var, no config key: a knob that
  suspends a teardown mid-pop is a way to wedge a live session, and nothing outside the suite
  wants one. Production pays one global read and an identity comparison per point, and
  creates no coroutine.
- **Points earn their names.** Each marks a boundary the race actually crosses, with a
  comment at the call site saying what suspending there intercepts. A point reachable only
  where a test could already observe the state is one more thing to keep correct for nothing.
- **Placed on the shared chokepoint, not per caller.** One point on the helper every switch
  handler resets through covers the family; a point per handler is how one of them ends up
  without one.
- **Assigned with `monkeypatch`**, which reverts even when the test fails, and floored by an
  autouse fixture that fails a test which INHERITED a set hook. Check on the way in, not at
  teardown: `monkeypatch` is built early as a dependency of an earlier autouse fixture, so its
  undo runs *after* a teardown-side check, which then cannot tell a pending undo from a real
  leak and reddens every legitimate test. Entry-side, the only thing that can still be set is
  a raw assignment — exactly the leak worth catching.

Two shapes recur when writing against a seam:

- **Bounded yields, not sleeps, to let the other racer run.** Yield the loop until a monotone
  marker holds (`task.done()`), capped by a turn count. Turns are not milliseconds: how many
  a given interleaving needs is a property of the code, so the cap only bounds a coroutine
  that can never progress and never decides the outcome for one that can.
- **Report, do not assert, inside the helper.** Returning a bool keeps a test readable in the
  world where a future fix makes the other racer BLOCK: it fails on its own named assertion
  instead of hanging until `--timeout` kills it with nothing to read.

A test that pins today's WRONG outcome says so at the assertion, names the issue, and states
which assertion the fix flips — otherwise the next reader repairs the test instead of the
defect.

```python
# WRONG: passes bare, fails under --cov, and the margin shrinks as the catalog grows
assert self._elapsed(build(8000)) < 5.0
# WRONG on shared runners: a timed doubling ratio — even thread-CPU — false-reds under
# frequency scaling / co-tenant contention (measured 3.2x against a 3.0 bound)
# RIGHT: doubling the input must not change WHAT the engine executes; only each single
# linear scan gets longer (see test_mid_dotstar_chain_spam_stays_linear)
assert traced(build(4000)) == traced(build(2000))
```

Keep a *small*-`n` absolute assertion alongside it so a uniform slowdown is still caught, and
verify the threshold against a mutated implementation rather than reasoning about it.

**First check that the time is even the algorithm's.** `test_chained_cd_expansions` asserted
`elapsed < 30s` around the bash gate and took 144s under load — but with the gate's
filesystem probes (`is_sensitive_path`, `_dir_holds_sensitive_leaf`, `_resolved_forms_bounded`)
stubbed, the same input ran in 50ms. The budget was measuring ~2,700 `stat` calls, not the
bounded-working-set property it named. Stub the I/O, **count the probes**, and assert the
count grows linearly with the input; that is the property, and it costs nothing.

### 6. A hang is a lost run, not a failed test

pytest-timeout has no `SIGALRM` on Windows, so a test that blocks past `--timeout` is not
failed in place: the whole xdist worker is killed (`node down: Not properly terminated`),
and with `--max-worker-restart=0` — which the Windows job needs, see `ci.yml` — the run
**aborts** with every test that worker had not reached still uncollected. MEASURED: one
test that could wait forever (`test_acp_runtime::test_concurrent_prompt_on_same_handle_rejected`,
a second `prompt()` awaiting a completion the test never feeds, `timeout=None` resolving to
the multi-hour dashboard ceiling) ended 2 of 5 full runs at ~3,000 of 62,000 tests. The
report showed 6 failures; the other 59,000 results simply did not exist.

So a test that awaits anything it must itself cause to happen carries a **bounded** wait
that fails **by name**:

```python
# WRONG: if the guard is broken this never returns, and the worker dies with it
with pytest.raises(AcpRuntimeError):
    await handle.prompt("again").__anext__()
# RIGHT: a missed refusal is a TimeoutError at THIS line, attributed to THIS test
with pytest.raises(AcpRuntimeError):
    await asyncio.wait_for(handle.prompt("again", timeout=1.0).__anext__(), 5.0)
```

The same applies to `Event.wait()`, `Queue.get()`, `Condition.wait()`, and a
`subprocess.communicate()` with no timeout. The ceiling is not a race to tune (it only
matters when the property is broken); make it generous and keep it well under
`--timeout`, so the failure is a named assertion and not a dead worker.

## Keeping the suite fast

The suite is ~89.5k tests. At that count a per-test cost is multiplied by 89,500, so
setup overhead, not any single slow test, is what dominates. Profile before optimizing:

```bash
# Per-test durations for the whole suite (writes a JSON map)
pytest -q -n auto --dist loadgroup --no-cov --store-durations --durations-path=/tmp/d.json
# One file, serially, with its own worst offenders
pytest test/test_foo.py -n0 -q --no-cov --durations=10
```

Note that `--store-durations` numbers taken under `-n auto` include worker contention
and overstate individual tests. Compare candidates **back to back** on the same machine
(`git stash` / run / `git stash pop` / run); a number from an idle machine measured an
hour earlier is not a baseline.

### The three highest-leverage patterns

1. **Audit what the autouse fixtures cost, before anything else.** Every one of them is
   paid ~89.5k times, so a few milliseconds there outweighs any single slow test. Two
   things to look for: a fixture requesting a fixture it never uses (one unused
   `tmp_path` allocated a directory for every test in the suite), and repeated
   `tmp_path_factory.mktemp` calls, which pick a numbered suffix by scanning the whole
   basetemp, so it gets slower as siblings accumulate. Allocate one session-scoped
   parent and `mkdir` under it instead. Measure the whole chain against a file of
   trivial `assert True` tests, which isolates setup cost from any real work:

   ```bash
   # 600 trivial tests, with the real conftest vs without it
   python -c "
   for i in range(600): print(f'def test_t{i}(): assert True')" > /tmp/probe/test_p.py
   cp test/conftest.py /tmp/probe/ && cd /tmp/probe && pytest test_p.py -n0 -q --no-cov
   ```

   That probe read 6.35s here before these fixes and 0.82s after: **9.2ms per test**,
   which is where most of the suite-wide win came from.
2. **Function-scoped construction of an immutable, expensive thing.** Real `git`
   repos are the worst offender here: seeding one costs ~1–1.6s in subprocesses, paid
   per test. Build it **once** in a `scope="session"` fixture and `shutil.copytree` it
   per test. This is safe only if the template is never handed to a test: copy from
   it rather than yielding it, so nothing one test does can reach another's. Re-point any
   absolute path the tool recorded (e.g. `git remote set-url`) in the copy. On Windows
   the copy also needs a `git reset --hard HEAD`: the copied files get fresh inode and
   ctime values, git's index stat cache no longer matches, and the copy reads as having
   "unstaged changes" -- `git rebase` refuses outright (MEASURED in `test_push_guard`
   when its repo pair moved to a session template). Nothing in a template is
   uncommitted, so the reset changes no content; it only re-stats the index.
3. **A production timeout or poll the test never asserts on.** Fake fixtures are often
   small enough to trip a real retry heuristic, then pay its full budget every test.
   `monkeypatch` the interval to `0`: the branch still executes, only the waiting
   goes. Confirm first that no test asserts on the interval itself.

Measured on this suite, each file run serially with `-n0 --no-cov` back to back on one
host (state the regime whenever you quote a number, because these do not compare across
regimes): `test_computer_use_snapshot_macos.py` 142.0s to 1.5s (pattern 3),
`test_md_notebook.py` 54.2s to 27.1s and `test_worktree_create.py` 20.7s to 15.8s
(pattern 2). Applying all three across ~16 files took the full suite from 281s to 116s
wall, and most of that came from the *shared* fixes, which is why the conftest audit is
item 1.

A fourth, adjacent pattern: **a patch target that misses.** Both this and § Patch the
defining module, not a re-export are the same one rule, *patch the namespace whose
globals the code under test actually reads*, and they are the two directions it fails
in. There, the caller reads its own defining module and the test patched a package
re-export. Here it is the reverse: the caller did `from pkg.mod import fn`, so it holds
its **own** binding, and patching `pkg.mod.fn` leaves that binding untouched. Either way
the REAL function runs, the assertion passes for the wrong reason, and the test pays real
time. One such target cost 6.1s and left a live transcriber running. Ask which module's
globals the call resolves through, and treat an unexpectedly slow "mocked" test as
evidence the mock missed.

Two more, from a 5x full-suite run whose per-test probe recorded wall time and RSS:

- **Setup that goes through a persisting helper.** `CrewStore.add_topic()` saves on
  every call (three file writes plus a prune scan), so a cap test that built
  `_TOPIC_IDLE_CAP + 25` filler topics through it paid an O(n²) disk cost for state it
  only asserted on after the *final* `save()`. Build fixture records directly (a helper
  with the same record shape) and save once; 47s became 0.2s.
- **A ratchet that re-walks the tree per test.** Several ratchet files parse every
  module under `src/` inside each test method — six methods in one class meant six
  walks, ~45s each under load, and the top ten such tests were 15 minutes of the run.
  Walk once per file: a module-scoped fixture or an `lru_cache`d loader that skips
  `node_modules`, `.venv`, `dist`, and `__pycache__`, and hand every test the same
  parsed set. The assertions do not change, so the planted-violation check below is
  how you prove nothing got weaker.
- **A module-cached walk that is still paid once per worker.** Caching per module
  is not the whole fix under xdist: `--dist loadgroup` hands an unmarked module's
  tests to whichever workers are free, and each worker warms its own copy of the
  cache. Five full runs measured `test_spawn_audit.py` at 5 workers × 40–75 s and
  `test_lazy_data_home_paths.py` at up to 3 workers × 27–159 s — eighteen such
  modules re-did their one scan ~4 times each, about 22 CPU-minutes per run that no
  test needed. The fix is one line at module scope,
  `pytestmark = pytest.mark.xdist_group(name="tree_scan_<module>")`, one group PER
  FILE: the module's tests then land on one worker and the cache is computed once
  per run, while different ratchet files still scan in parallel. Do not put every
  ratchet in one shared group — that serializes several minutes of scanning onto a
  single worker while the others sit idle at the tail.

Neither of these shows up as a *failure*, which is why they survive: the suite is
green, just three times slower than it needs to be, and every timing-sensitive test
in the same shard inherits the load.

### Verify an optimization did not weaken the test

**Prefer the command.** `prove.py` in the `prepare-pr` skill does this for a whole
change and cannot cost you work: it reverts the change's production hunks inside a
throwaway git worktree, so your tree is never mutated and nothing needs restoring,
and it refuses to run while a file under proof carries uncommitted edits.

```bash
python3 src/kiro_crew/builtin_skills/kirocrew-dev/prepare-pr/scripts/prove.py
# 0 PROVEN · 20 NOT_PROVEN · 21 INCONCLUSIVE · 10 nothing to prove · 30 baseline red
# add --per-hunk to name the hunks no test catches
```

The hand-typed form below remains correct for a single line you want to probe
in isolation, and its two footguns are why the command exists.


A fix that makes a test faster by making it check less is a regression. Mutate the
production code the test covers and confirm the test still **fails**:

Restore from a **copy of the file you mutated**, not from git. `git checkout --` resets
the path to HEAD, which silently discards any unrelated uncommitted work in that file and
cannot be undone. And sequence it with `;`, not `&&`: with `&&` the restore runs only when
pytest exits 0, i.e. only in the case where the mutation did *not* do its job, leaving a
correctly-failing mutation in your tree.

```bash
f=src/kiro_crew/foo.py
cp "$f" "$f.premutation"                 # back up whatever is there now
# ...edit $f to invert the branch the test covers...
pytest test/test_foo.py -n0 -q           # expect RED; if it passes, the test is weak
mv "$f.premutation" "$f"                 # exact pre-mutation bytes, unrelated edits kept
git diff --stat "$f"                     # should show only what you had before
```

### Shard balance

`ci.yml` splits the backend suite into 4 `pytest-split` groups. Splitting is balanced by
recorded runtime **only when a `.test_durations` file is committed**; without one
pytest-split falls back to an even split by test *count*. No such file is committed here:
`test-durations.yml` would generate one weekly but has failed on a transient `git push`
502 both times it ran, so it has never landed.

**Measure a shard by running it, not by summing durations.** Each shard runs its own
tests at `-n 4`, so per-test times from a `--store-durations` run include worker
contention and do not add up to a shard's wall clock. Summing them predicted a 3× spread
here. Running the four shards the way CI does,

```bash
pytest -q -n 4 --no-cov --splits 4 --group <N>
```

measures **54.8 / 59.9 / 81.1 / 62.4s**, a 1.5× spread. Count-based splitting is
already close enough that committing `.test_durations` would save on the order of
seconds, so it is not the lever it looks like. The lever is the outliers: a single file
paying a 2s production poll 119 times moves a shard far more than the split ever does,
and it was the two files carrying that kind of cost that sat on the shards which failed
most.

## Exploratory Testing via Manual Command Execution

For integration issues involving external processes (kiro-cli, MCP servers, build
tools), use the **observe → diagnose → fix → verify** pattern:

### When to Use

- Debugging protocol-level issues (ACP JSON-RPC, MCP handshake)
- Investigating timing/ordering problems (async init, notification delivery)
- Verifying build pipeline behavior (setuptools, npm, pip)
- Any issue where mocked unit tests can't reproduce the real behavior

### Method

1. **Write a minimal script** that reproduces the exact subprocess interaction:
   - Spawn the real process (`kiro-cli acp`, `aim mcp install`, etc.)
   - Send inputs step by step
   - Log every output with timestamps
   - Use large stdout buffers (`limit=10*1024*1024`) to avoid truncation

2. **Observe raw behavior** — don't assume, capture everything:
   - Log all JSON-RPC messages (method, id, params keys)
   - Record timing (when does each message arrive relative to start?)
   - Note message classification (notification vs response vs request)

3. **Identify root cause** from observations, not from reading code alone

4. **Apply minimal fix** targeting the observed root cause

5. **Re-run the same script** to verify the fix works end-to-end

### Example: ACP Protocol Testing

```python
"""Test ACP handshake and MCP server loading."""
import asyncio, json, time

async def main():
    kiro = await asyncio.create_subprocess_exec(
        "kiro-cli", "acp", "--agent", "kirocrew",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=10 * 1024 * 1024,
    )
    req_id = 0
    buffered = []

    async def send(method, params):
        nonlocal req_id; req_id += 1
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        kiro.stdin.write((json.dumps(msg) + "\n").encode())
        await kiro.stdin.drain()
        return req_id

    async def wait_response(rid, timeout=120):
        """Wait for response, buffer notifications."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = await asyncio.wait_for(kiro.stdout.readline(), timeout=3)
                if not line.strip(): continue
                msg = json.loads(line)
                if msg.get("method") and msg.get("id") is None:
                    buffered.append(msg)  # notification
                    continue
                if msg.get("id") == rid:
                    return msg.get("result", {})
            except (asyncio.TimeoutError, json.JSONDecodeError):
                continue
        return {}

    # Step through protocol, log everything
    t0 = time.time()
    await wait_response(await send("initialize", {
        "protocolVersion": "2024-11-05",
        "clientInfo": {"name": "kirocrew", "version": "0.1.0"},
    }))
    await wait_response(await send("session/new", {"cwd": "/tmp", "mcpServers": []}))

    # Check what was buffered during handshake
    for msg in buffered:
        method = msg.get("method", "")
        name = msg.get("params", {}).get("serverName", "")
        print(f"  [{time.time()-t0:.1f}s] {method} name={name}")

    kiro.kill()

asyncio.run(main())
```

### Example: Build Pipeline Testing

```bash
# Reproduce: run build N times, check for flaky failures
pip install -e . && pip install -e . && pip install -e .

# Diagnose: find stale cached files
find build/ -name "SOURCES.txt" -exec grep "basePickBy" {} +

# Verify fix: same sequence must pass consistently
rm -rf build/ && pip install -e . && pip install -e . && pip install -e .
```

### Key Principles

- **Observe before fixing** — capture raw data, don't guess
- **Reproduce reliably** — if you can't trigger it on demand, you can't verify the fix
- **Test the exact flow** — simulate what the real code does (same process, same protocol, same ordering)
- **Verify N times** — flaky issues need multiple runs to confirm (3+ consecutive passes)
- **Keep test scripts** — save in `/tmp/test_*.py` during debugging, discard after fix is verified
