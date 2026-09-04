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
  inherited. `AutoNudgeService.start()` publishes itself there and `stop()` clears it, so
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
     redirects them, while `security._EXTRACT_INTO_TRUST_ROOT_RE` and
     `kiro_usage_api._CLI_SQLITE_DBS` must **never** be redirected — they are
     security anchors whose whole point is naming the real home. **Stub the reader,
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
- Async tests MUST use `@pytest.mark.asyncio`

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

## Determinism: the five flake classes

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

### 3. Leaked async objects

An `AsyncMock` standing in for a **synchronous** method (`StreamWriter.write`,
`stdin.close`) returns a coroutine nobody awaits. A `cancel()` that is never awaited
leaves a live task at loop teardown. Both surface as `RuntimeWarning: coroutine ... was
never awaited` / `coroutine ignored GeneratorExit`, attributed to whichever *later* test
happened to trigger the GC, so the reported test is rarely the guilty one.

Fix: `MagicMock()` for sync methods; `await` the task after `cancel()`, absorbing
`CancelledError`.

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

Asserting a *duration* when the property under test is algorithmic **complexity**. CI enables
coverage on one Python version only (`--cov` on 3.12, `--no-cov` on 3.10), and instrumentation
multiplies the cost of every executed line — so the same un-regressed code measured ~1.7s of CPU
bare and >5s under coverage, and one shard failed on 3.12 while passing on 3.10 **at the identical
commit**. The tell is a timing test that splits by Python version rather than by machine load.

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

## Keeping the suite fast

The suite is ~56.5k tests. At that count a per-test cost is multiplied by 56,500, so
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
   paid ~56.5k times, so a few milliseconds there outweighs any single slow test. Two
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
   absolute path the tool recorded (e.g. `git remote set-url`) in the copy.
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
