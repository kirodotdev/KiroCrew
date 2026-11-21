# Testing Conventions

## Framework

- `pytest` with `pytest-asyncio` for async tests
- Coverage via `pytest-cov`

## File Layout

Backend tests live under the roots `setup.cfg` pins in `testpaths`
(`test` plus `src/kiro_crew/apps/builtins`): pytest recurses into
`test`, so nested `test/<pkg>/test_*.py` files are collected too (for
example `test/metrics/`, `test/e2e/`, `test/integration/`), alongside
helper subdirectories that hold shared fixtures rather than tests (for
example `test/workflows/`). What distinguishes a top-level
`test/test_<module>.py` is not whether nested files are collected but
the `scripts/leaf_test_scope.py` CI fast path: only `test/test_*.py`
files count as *leaves* there (`_LEAF_NAME` in `scripts/leaf_test_scope.py`),
which limits that fast path without stopping collection
of nested files. Placement is enforced by the `testpaths-coverage` gate
(`scripts/check_testpaths_coverage.py`). A file
outside those roots is never collected, so it stays green by omission.

## Determinism contract (read this first)

Every backend test MUST keep these thirteen rules. Each line names the shape to use and
links the class that explains it; the rest of this document is the evidence behind
them. The shared shapes live in `kiro_crew.testing` (`clock`, `wait`, `ids`), which
ships in the wheel, so the in-package app suites use them too; the rootdir
`conftest.py` adds the `manual_clock`, `seeded_rng` and `local_tz` fixtures.
`AUTOSDE.yaml`'s `tests-are-deterministic` rule holds review to them, and the SAST
job's `semgrep/test-determinism.yaml` refuses one of the commonest shapes on a line a
change adds or rewrites (a fixed sleep in a test body outside a poll loop, a bare
`monkeypatch.undo()`, an in-process reload, a rebound stdlib clock, a naive local-time
read, an unseeded draw, a new autouse fixture on the shared `monkeypatch`, a
promise-sleep, `waitForTimeout`); a deliberate one says why with
`# nosemgrep: <rule id>`. The frontend's rules are in
[website/docs/testing.md](../../../website/docs/testing.md#determinism-establish-the-state-you-assert-on).

- **D1. Wait on a signal, never a duration.** Poll the state you are about to assert
  under a bounded deadline that RAISES quoting the last state it read: `wait_until` /
  `async_wait_until` in `kiro_crew.testing.wait`, `until_parked` for a task parked on a
  lock, `drained_progress` for a batch whose speed the host owns; import it at module
  level. That deadline is a lost-run bound (D7), half the test's timeout by default. An
  absence assertion waits on a positive signal first (the waiter registered, the
  product's own tick counter reached N), or it passes for the trivial reason.
  Wait on signals, not clocks: await the event, future or coroutine the code emits
  (`await_until`; `LoopHop` for a hook that must run a coroutine on the loop from an
  executor thread), and a ceiling of 30 s or more only names a hang -- a
  `wait_for(..., timeout=2)`, a `.result(timeout=5)` or a poll of N short sleeps bets
  on the runner's speed, and `test/test_wait_discipline_ratchet.py` holds each test
  file to the number of those it already has: on a pull request each touched file is
  compared to its count at the PR's merge-base, and also to its committed baseline
  entry, so a PR is not blamed for drift it did not cause but a rename without a
  baseline-entry update is still caught.  A new file the PR splits off may declare
  its count by adding an entry to ``test/wait_discipline_baseline.txt``; without
  that entry its sites are a violation.  Merge-order drift in untouched files is
  still caught by the committed baseline on main and in the merge queue.  A renamed file
  keeps its line under its new path: `python test/test_wait_discipline_ratchet.py --move OLD NEW`.
  [Class 2](#2-wall-clock-races).
- **D2. One clock, injected or frozen at the seam the code reads.** Replace the module's
  own `time` / `datetime` name with a `ManualClock` (`kiro_crew.testing.clock`, or the
  `manual_clock` fixture): `clock.install(monkeypatch, module)`. Never rebind a stdlib
  clock or `time.sleep` for the whole worker. [Class 2](#2-wall-clock-races),
  [class 4](#4-order-dependence-and-shared-state).
- **D3. Never sleep to make two timestamps differ: set them** (`os.utime(path, ns=...)`
  as `write_config` in `test/_hot_reload_helpers.py` does, a stepped clock such as
  `ManualClock(tick=...)`, an explicit `created_at`, ids from `seq_ids` whose string
  order is their creation order). Assert order strictly, and pin a time-sorted output's
  tie-break with an equal-stamp pair (a frozen `ManualClock`, or `colliding_clock` in
  `test/windows_sim.py`).
  [Class 7](#7-data-order-and-timestamp-ties).
- **D4. Never assert an order you did not define.** A `set`, a dict built from one,
  `listdir` / `iterdir` / `glob`, `as_completed` iteration and side effects appended in
  completion order are compared as a `set` or `Counter`, or after sorting by a total key.
  (`gather`'s return list is in argument order and is fine.)
  [Class 7](#7-data-order-and-timestamp-ties).
- **D5. No dependence on the host's zone or locale.** Read the zone the product stamps,
  with the instant frozen; a test about local time pins its zone and runs under `UTC`,
  `Pacific/Kiritimati` (+14:00) and `America/St_Johns` (-03:30). Inject the zone where
  the product allows it; otherwise use the `local_tz` fixture, which sets `TZ` and calls
  `time.tzset()` under a POSIX-only skip and restores both at teardown (class 1 has the
  shape), never a bare `monkeypatch.undo()`. A test that reads a repository file as
  text names the codec, `read_text(encoding="utf-8")` or `open(path, encoding="utf-8")`:
  a bare call decodes with the host's code page, which raises at the first non-ASCII
  character on a cp950 Windows host and silently mis-decodes it on a cp1252 one.
  [Class 1](#1-nondeterministic-input).
- **D6. Seed every RNG that feeds an assertion** (`random.Random(SEED)`, or the
  `seeded_rng` fixture, seeded from the node id and printing the seed when the test
  fails), and give a fake PID a number no OS can allocate (`UNALLOCATABLE_PID` /
  `unallocatable_pids()` in `kiro_crew.testing.ids`). A substring absence check over
  a payload that carries random ids first replaces each id, matched by its exact
  shape, with a fixed stand-in: a short fragment turns up inside a random id by chance.
  [Class 1](#1-nondeterministic-input).
- **D7. No upper bound on a measured duration, rate or sample count, except a lost-run
  bound or a lower bound.** Assert the work, the schedule or the order. A wait's
  [lost-run bound](#6-a-hang-is-a-lost-run-not-a-failed-test) may stay, and so may a
  lower bound that proves a deadline was honoured (with a margin of at least one clock
  tick) or a bound relative to a span the test measured itself. An existing complexity
  guard keeps its thread-CPU budget at the original constant, or replaces it with a
  count of work proven to fail on what the budget caught (class 5); a new one counts
  work instead. Reshaping a kept budget's reading (the minimum of N readings, the
  tracer paused around it) is not a fix: it still asserts seconds, and the K8 ratchet
  counts `thread_time` and `process_time` deltas like any other clock.
  K8 (`test/flake_pattern_k8.py`) counts every upper bound on a span read off the
  real clock, an event loop's `time()` included: a literal or non-literal limit
  (`elapsed < budget`, `time.monotonic() - started < _CEILING`,
  `elapsed < x * 1.6`), the reversed form (`BUDGET > spent`,
  `self.assertGreater(5.0, elapsed)`, and `stamp > time.monotonic() - 5`, which bounds
  an age), and unittest's `assertLess` / `assertLessEqual` with the span first. It
  follows a stamp through aliases (`before = started` after
  `started = time.monotonic()`), and a span only a helper returns
  (`elapsed = run_timed()`) goes by its name. A clock alias or import
  (`clock = time`, `from time import monotonic as now`) reaches only its own function
  and the functions inside it, or the whole file at module level. A loop is `loop`,
  `self._loop`, pytest-asyncio's `event_loop`, a getter's result or a name bound from
  one, not any name ending in `_loop`. It exempts a span off a clock the test drives
  (`clock.monotonic() - t0` where `clock` is a fixture, `vclock.time() - 5`), time
  remaining (`deadline - time.monotonic() <= cap`), ordering two stamps
  (`rt.at > t0`, or `stamp > cutoff` after `cutoff = time.monotonic() - 5.0`, a
  cutoff taken before the work), a lower bound, a floor or ratio between two spans
  (`elapsed >= min_duration`, `elapsed < first_elapsed / 4`), a module constant, and a
  delta-named attribute such as `plan.duration_ms` against a non-literal limit. A
  deliberate lost-run assertion on a measured span carries `# flake-ok: <reason>`.
  [Class 5](#5-absolute-time-budgets-on-instrumented-runs).
- **D8. Every await, join or `communicate` the test itself must unblock is bounded and
  fails by name** (`asyncio.wait_for`; `join(timeout)` followed by an `is_alive()`
  assertion; `communicate(timeout=...)`). A real `node` or interpreter child starts once
  per module, not once per test, and its deadline is half the test's own timeout.
  [Class 6](#6-a-hang-is-a-lost-run-not-a-failed-test).
- **D9. A listener binds port 0 and reports the port it got** (aiohttp's `TestServer`
  does; `TCPSite(runner, "127.0.0.1", 0)` then `runner.addresses`, as in
  `test/test_dashboard_slowloris.py`). Bind, close and hand the number on only where the
  subject needs a number before it can listen, and then handle `EADDRINUSE`.
  [Rules](#rules).
- **D10. Loopback only.** Stub the network at the seam the product calls; no test
  depends on a host other than `127.0.0.1` / `::1`. The rootdir network floor reports a
  test that reaches past loopback; one that must is marked `@pytest.mark.real_network`.
  [Side effects](#side-effects-what-a-full-run-does-to-the-host-and-how-to-see-it).
- **D11. Process globals change only through `monkeypatch`**, and an autouse fixture's
  only through `_floor_monkeypatch`. Never `importlib.reload` a shared module in-process:
  load a private copy with `importlib.util.spec_from_file_location` under a unique name,
  registered under that name with `monkeypatch.setitem(sys.modules, name, mod)` before
  `exec_module` (a `@dataclass` module needs it), never under the real name.
  A `sys.modules` eviction restores the entry AND its parent package's attribute in the
  same scope (the rootdir package-attribute floor puts back a `kiro_crew` one a test
  left changed, and names the test), and a new process-global ships its floor entry
  (`test/test_process_cache_floor_ratchet.py` fails a new module-level cache in
  `src/kiro_crew` that no floor resets).
  [Class 4](#4-order-dependence-and-shared-state).
- **D12. Prove it.** Each new or changed test passes 20 times at `-n0`, 10 times at
  `-n 4` beside its neighbours, and in a shuffled order. A FIX to a flaky test also
  shows the forced condition (a coarse clock, a late executor, a delay at the named
  seam) red on the parent commit and green on the fix. A timing result from an
  overloaded host is not evidence; measure under CI-equivalent load.
  [Proving a determinism fix](#proving-a-determinism-fix).
- **D13. No dependence on what the host has on `PATH`.** When the code under test
  resolves a command with `shutil.which` (an MCP server's `command`, `gh`, `git`,
  `node`), the test chooses the answer. A command name that is only fixture DATA counts
  too: `"command": "http"` in a spec is a lookup on the runner's `PATH`, and a Windows
  runner's `PATH` holds very different programs from a Linux one. Pin the product's own
  resolver (`trusted_system_bin`; `_pin_rg_on_path` in `test/test_file_grep.py` pins the
  grep endpoint's), or set the search path with `monkeypatch.setenv("PATH",
  str(tmp_path))` and plant the tool there; a test that needs a real interpreter names
  `sys.executable`. The suite has no autouse `PATH` floor (real `git`, `bash` and `node`
  tests need the host's), so no shared fixture covers this. Run a host-tool test once
  with the tool off `PATH`. #35547 fixed a rewriter spec whose verdict turned on an
  `http` program being on `PATH`.
  [What the host lends the suite](#what-the-host-lends-the-suite-and-must-not).

### Production deadlines are not test boundaries

A test passes no value that is not literal ``0``/``None`` or a ``kiro_crew.testing``
import as a duration-named argument.

- **``0`` for the timeout path.** Literal ``0`` (or ``None``) is the "no deadline /
  already expired" value.
- **A ``kiro_crew.testing`` name for a generous safety net.** Use
  ``from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS`` (60 s) for the
  happy-path pin. Any name imported from ``kiro_crew.testing.*`` is allowed.
- **A fixture-pinned seam for a production constant.** ``_floor_monkeypatch.setattr``
  in an autouse fixture keeps the constant out of every test body.
- **An injected clock** for a module that owns a ``time`` import.
- **Lift the deadline the assertion rides on, even when the test never names it.**
  A best-effort write or a bounded cold wait that drops or serves a partial answer
  when it expires decides the outcome on a slow runner, while the test is green
  everywhere else: ``platform_log_append._APPEND_TIMEOUT_SECONDS`` under the
  ``model.route`` strip, ``skills._COLD_CATALOG_WAIT_SECS`` under a cold
  ``get_context``. Set it to ``FALLBACK_TIMEOUT_SECS`` in the test or a shared fixture
  (``test_decisions_strip_rides_message.generous_decision_log_append``). K19 sees only
  a wrong override, never a missing one, so trace the asserted value back through
  every budget on its path.

**WRONG** (non-zero literal or local constant — K19 flags both):

```python
_floor_monkeypatch.setattr(gate, "_LOG_BUDGET_SECS", 20.0)
subprocess.run(cmd, timeout=60)       # use FALLBACK_TIMEOUT_SECS
_T = 30
subprocess.run(cmd, timeout=_T)       # local constant counts
```

**RIGHT** (``kiro_crew.testing`` import; literal ``0`` for the expired path):

```python
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

@pytest.fixture(autouse=True)
def _raise_deadlines(_floor_monkeypatch):
    _floor_monkeypatch.setattr(gate, "_LOG_BUDGET_SECS", FALLBACK_TIMEOUT_SECS)

def test_timeout_path(_floor_monkeypatch):
    _floor_monkeypatch.setattr(skills_select, "MAX_WAIT_SECS", 0)
    assert run_something() is None
```

K19 has two arms. The **setattr arm** flags any non-zero non-``kiro_crew.testing`` value
as the third argument of ``monkeypatch.setattr`` / ``_floor_monkeypatch.setattr`` on an
attribute whose name ends in a time-unit suffix (``_s``, ``_secs``, ``_seconds``,
``_ms``, etc.) or starts with ``timeout``. This catches the five motivating flakes
(`_LOG_BUDGET_SECS`, `_PRUNE_MAX_SECONDS_PER_RUN`, etc.).

The **keyword arm** flags the same value rule when the keyword argument name contains one
of these *wait-bound words* as a whole ``_``-separated component:

- ``timeout`` — e.g. ``timeout=``, ``read_timeout=``, ``timeout_ms=``
- ``deadline`` — e.g. ``deadline=``, ``soft_deadline_secs=``
- ``ttl`` — e.g. ``ttl=``, ``ttl_secs=``
- ``grace`` — e.g. ``grace=``, ``grace_period_s=``
- ``budget`` *only when followed by a time-unit suffix* — e.g. ``budget_secs=``,
  ``log_budget_ms=`` (not ``token_budget=`` or ``budget_key=``)

K9 callees (``wait_for``/``wait``/``result``/``join``) are excluded — those are
bounded-wait primitives. Lambdas and module-level function names are also excluded.
The ratchet is diff-scoped: the existing tree is not forced to burn down.
A line that must keep one of these shapes on purpose carries `# flake-ok: <reason>`
(`// flake-ok: <reason>` in TypeScript) with a concrete reason, or `# nosemgrep: <rule
id>` with the reason where a `semgrep/test-determinism.yaml` rule refuses it. Review
accepts the marker, and the reason is the record of why the rule does not apply there.

`test/test_flake_pattern_ratchet.py` counts, file by file, the shapes
`semgrep/test-determinism.yaml`'s patterns miss, so no site needs both markers: patches of `asyncio.sleep` or of the
`datetime` classes (named directly or through another module's binding; the `time`
module through any binding is the semgrep rule's), a `sys.modules` eviction with
no restore in the same function, raw environment writes, duration bounds, literal waits
under two seconds (the too-tight lost-run bound of class 6; a negative such as
`assert not done.wait(0.3)` is not one), literal listener ports, fixed crew-log flush
ceilings, crew-log removals outside `eager.paused()`, Event waits a test parks on a thread-pool executor, direct writes of asyncio Tasks or TimerHandles to a ``kiro_crew`` module attribute from a test function (K16), ``pytest.mark.xfail(strict=False)`` overrides (K18), a non-zero numeric literal used as a duration-named keyword argument or as a ``setattr`` value on a duration-named attribute — the production-deadline-as-test-boundary shape (K19), an `update_metadata` or `update_metadata_if` call in a coroutine's own frame (K20, [Async tests](#async-tests)), a shared total pinned exactly in a module that walks the product tree: a count asserted `==` an integer literal of 10 or more in a function that reads the walk, or a count compared `==` / `!=` with a module-level ALL-CAPS constant whose name holds `BASELINE`, `COUNT`, `TOTAL`, `EXPECTED` or `ROWS` (K21, [Merge-safe ratchets and counts](#merge-safe-ratchets-and-counts)), a product registry's size or members pinned to literals (K22 and its frontend twin J7, [No change-detector tests](#no-change-detector-tests)), and the frontend shapes in `test/flake_pattern_js_detectors.py`. In a file
the pinned semgrep reads no line of, it counts the `time` patches the semgrep rule would
refuse as well: a file over 1,000,000 bytes, or one holding both a lambda with a bare `*`
(`lambda a, *, b: ...`) and a parenthesized `with (a as b, ...):`, which semgrep 1.78
cannot parse. It fails a
change that raises any file's count above that file's count at the change's merge-base:
`FLAKE_RATCHET_BASE` when set, the first parent of a CI merge commit, else the newer
merge-base with `upstream/main` or `origin/main` (fetch the remote your branch was cut
from: `upstream` for a fork). The working tree is judged, untracked files included. A
new file starts at zero, a deleted file passes, a rename git detects keeps its count,
and a rise is forgiven only for a site that moved (the same class in an identical
enclosing function, or the same line in TypeScript, that left another file in the same
change; a function edited in place, or a site a `flake-ok` still excuses, has not
left). When no merge-base is readable, the test
warns `compared nothing` and passes rather than judge a change by sites other merged
changes added. CI's Backend Tests checkout is depth 2, so the merge commit's first
parent is readable there and the shards gate the comparison after their 20-40 min run;
the early, fail-closed verdict is `fast-gate.yml`'s `flake-pattern-ratchet` job, which
checks out depth 2 on every pull request and merge group so the resolver reads that
first parent, answers in about a minute, and fails when the ratchet still
reports `compared nothing`. Run `python -m pytest -n0 test/test_flake_pattern_ratchet.py`
before pushing to see the same verdict first. It does not
count an order assertion (D4) or a network call (D10): those rest on review. For D10
the same job also runs `scripts/net_strict_changed.py`, which sets `KIROCREW_NET_STRICT=1`
for the test files the change adds or changes and names any that look up a non-local
host or connect past loopback in the job summary; it is warn-only for now, and a file
that already does so is held to the rule only once someone edits it. A
`flake-ok` line whose reason has ten characters or more, words and not just punctuation
or digits, is not counted. `python test/test_flake_pattern_ratchet.py` prints each
file's current counts as the burn-down view; no committed file holds them.

## Patterns

### Grouping
Group related tests in classes:
```python
class TestAcpClientInit:
    def test_defaults(self): ...
    def test_custom_work_dir(self, tmp_path): ...
```

### Async tests

Session-switch lock registries are reset per test. Reused fixture session keys
must not retain a contended lock tied to another test's event loop.
The test floor clears live session and run execution records between tests so a
reused temporary home cannot inherit another test's routing or retention mode.
Dispatch doubles provide a concrete `get_agent_selection()` tuple, including
`("template", "")` for the default template. An unconstrained mock is not a
valid member or template identity. Runtime fixtures that bypass the constructor
with `__new__` explicitly set `_remote_workspace = None` for a local session,
so activation exercises its real local path rather than an incomplete object.
Session context doubles implement the async
`memory_mode_for_session()` accessor and return a concrete retention mode.
Session-start collector tests declare their
MCP roster explicitly rather than inheriting the installed agent's tools.

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

The write side has the same rule: `replace_with_retry` is one attempt on the loop
thread, so a synchronous helper that ends in a session-file rename
(`ConversationLog.update_metadata`, `bind_session_execution`) called from an
`async def` test loses to any handle a scanner holds on the destination for a few
milliseconds (`[WinError 5]` on `sessions/<key>.jsonl`). `member_memory_helpers`'
`env.bind_session` refuses to run on the loop for this reason; a coroutine awaits
`env.bind_session_async`, which runs the same writes on a worker thread. The
flake-pattern ratchet counts an `update_metadata` or `update_metadata_if` call made in
a coroutine's own frame (K20; a sync helper that calls it is not counted), so a new one
fails a change; hand the method to
`await asyncio.to_thread(log.update_metadata, ...)` instead.

A test that drives code to `RecursionError` on purpose must hold the cyclic
collector off for the walk. Its innermost frames have no headroom, and a gen0
sweep lands wherever the allocation counter says -- sometimes there. Whatever
cyclic garbage the worker is carrying then runs its finalizers at that depth; a
pending Task leaked by an EARLIER test reports itself through `logger.error` on
`__del__`, the report overflows, the interpreter hands the escaped exception to
`sys.unraisablehook`, and pytest's hook overflows too -- surfacing as
`RuntimeError: Failed to process unraisable exception` against the recursing test,
on any platform (`test_mcp_preflight`, 3 heads, Linux and Windows). Reproduce by
planting one such cycle per recursion level under `gc.set_threshold(1, 1, 1)`:
every run. Fix: `gc.collect()` once at depth zero so inherited garbage pays its
finalizers where there is stack, `gc.disable()` around the walk, re-enable and
collect in `finally` (`no_cyclic_gc_at_the_recursion_limit`). Reference counting
still frees the walk's own objects; only cycles wait for teardown.

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

### Liveness tests with fabricated PIDs

A fabricated PID can identify a real process on the test host. Give a real
`LivenessOracle` an explicit process backend or a fixture-owned proc tree rather
than letting it select the host backend. Keep that source isolated across
`fresh()` and exercise the real cross-tick state transitions. A collision case
must still detect the fabricated child's exit without reading the host table.
Windows pod handle-stop fixtures must also own the separate numeric `pid_exists`
probe: after a simulated handle exits, a real host process with the same PID
must not change the verdict. Cover both a gone PID and a recycled live PID;
the latter must still refuse state deletion after exact-handle draining.
Keep one numeric-liveness stub for each scenario: a later duplicate patch
must not replace the recycled-PID case with the ordinary exited-handle case.

Tests of executable ownership pin only the ancestors above their temporary tree;
fixture files retain their real ownership and permission bits. Host kernel headers
must be matched to their architecture before validating syscall numbers. Nested
pytest processes clear inherited `PYTEST_ADDOPTS`, and Unix-socket fixtures use
`short_tmp_base()` so a deep `TMPDIR` cannot exceed the socket path limit. A real
cgroup enforcement test skips an unreachable user bus, not other scope failures.
Duration-accounting tests use injected clocks and report durations for exact
arithmetic; subprocess integration tests verify reporting and cleanup without a
wall-clock ceiling tied to runner speed.

Cancellation-during-persistence tests must wait for a worker-entered handshake
before cancelling, not infer entry from a short sleep. Keep the worker's wait
bounded, release it in `finally`, and await the cancelled task's write drain;
assertions must still prove the lock stays held and the real write completes.

SQLite contention tests open the writer's own connection before taking the holder's
transaction, observe its actual `BEGIN IMMEDIATE`, and require completion to remain
pending while the lock is held. Release the holder before awaiting completion and
assert the persisted row: `sync` can swallow a failed write. Do not compare a real
clock delta with the requested sleep duration. Bound every handshake and join, release
the holder in `finally`, and close each thread's connection on that thread.

A delayed executable fixture must be absent or ready to execute at its public path.
Write and chmod a private sibling first, then atomically rename it into place. Writing
the public path before chmod exposes a non-executable file to the real child; EACCES
must remain terminal, not become another retry case. Assert initial absence and the
child's successful exit, and cancel/join the publisher and reap the child on failure.

The same handshake applies when the cancel comes from a PRODUCTION deadline rather
than the test. `run_with_recall_deadline` arms its timer the moment it is awaited;
`run_in_embed_pool` hands the job to a thread the OS still has to schedule. A test
that shrinks `RECALL_TIMEOUT_SECS` to 100 ms and asserts on the RUNNING worker is
therefore racing the scheduler: on a loaded runner the timer won, `future.cancel()`
succeeded on an unclaimed job, and `entered.wait` read `False`
(`test_memv2_audit_c_runtime`, two tests, Linux and Windows). Reproduced on an idle
host by pricing the worker's pickup at 150 ms: every run. Fix the ORDER, not the
constant: give the test a recall pool whose `submit` returns only once the worker has
marked the future running (`entered_recall_pool`), so entry precedes the first point
the timer can fire; the 100 ms then only decides how soon the deadline arrives, which
nothing races. Likewise a budget the subject carries itself (`_EMBED_WAIT_SECS` on
the owner of a coalesced embed) is expired by the test through `work.cancelled.set()`
once the native call is provably in flight -- `expired()` honours it -- never by
sleeping past a shortened deadline that also has to outlast a thread start.

Both of those are one rule: **an assertion about an interleaving must not be an assertion
about a window.** "Long enough for the other side to get there" is a claim about how much
progress a loaded runner makes in that many milliseconds, so when it makes less the test
reports the property as violated when the truth is that the setup was never reached.
Enlarging the constant is the same defect in a longer sleeve.
`test_config_purged_on_phase1_cancellation` is the worked example: it cancelled a
two-phase MCP apply 50 ms after starting it, but the prologue before the
guaranteed-cleanup `try` -- a bounded body read plus a read-only preflight on a worker
thread -- can outlast that window, and a cancellation landing there owes no sweep, which
the assertion reads as a missing purge. Priced at 300 ms, it fails every run. The fake
confirmed uninstall now sets an `asyncio.Event` as it returns and the test awaits that,
so the ordering is a fact and the constant is gone.

Three forms satisfy the rule, and the tree has a worked example of each.

- **Hold the state until it is observed, then release it.**
  `test/test_runloop_integration.py::test_throttle_parks_two_runs_on_one_scope_and_wakes_by_capacity`
  parks on `_HELD_PARK_SECS` (600 s, longer than every barrier below it) and releases
  through `coordinator.recovered`, so the parked state is one the test reads rather than
  one it must catch.
- **Observe at the seam, so the state cannot become visible unobserved.**
  `ops_mission_control/tests/test_policy_store.py::TestConcurrentWritesCannotRestoreAStaleCeiling::test_the_two_halves_of_the_ceiling_commit_together`
  wraps the publish primitive and asserts every state `set_ceiling` publishes is
  coherent. Because `atomic_write` publishes by rename, a published state is the only
  thing an unsynchronised reader can ever see, so this proves the property for every
  schedule instead of sampling schedules -- and needs no concurrency at all.
- **Wait for a handshake the production path raises, then act.** The two cancellation
  cases above.

Two rules travel with all three. **Make the precondition a separate, deterministic
assertion from the property**: bound the handshake wait as a hang guard and fail it in
the precondition's own words ("the interleaving under test did not occur"), so an unmet
setup is never read as a broken invariant. **An assertion must be unable to fail for the
wrong reason**: scope it to the subject under test -- this logger, this file, this object
-- so an unrelated event in the same process cannot red it.

Do not factor these interleaving HOLDS into a shared helper. The seam differs in kind
every time (a SQLite write lock, an asyncio task reaching an attributable state, a
multi-phase uninstall's phase 1, a dependency coordinator's park, a policy-file publish),
anything spanning them is a thin wrapper over `threading.Event` / `asyncio.Event` plus
`try`/`finally` behind an indirection every reader must learn, and the seam form holds
nothing at all, so a hold helper would not even cover it. A condition POLL is a different
thing: wait on the state through `kiro_crew.testing.wait` (`wait_until`,
`async_wait_until`, `until_parked`), which raises with the state it last read
([D1](#determinism-contract-read-this-first)), never another silent poll.

A mock subprocess handed to a real kill path must not carry a pid a live process
can own. The kill helpers' only handle on their target is the integer `pid`:
they resolve it against the runner's real process table and signal whatever owns
that number. `kill_process_tree`'s self-group refusal is not a defence -- it
declines only the GROUP signal and then sends a pid-scoped SIGKILL, which reaches
a same-group process just as hard. Under `pytest-xdist` the runner's own group is
full of sibling workers, so the casualty is a worker: its channel closes
mid-batch and the shard reports whichever test it had been sent, with no
assertion and no traceback. Running the file alone hides it -- with a sparse
process table the lookup raises and the suppressed exception swallows the whole
path, so the crash needs the full shard. The surface is every kill helper on
`platform_compat`, not only the tree kill: `kill_pid`, `kill_pid_pinned`,
`kill_pid_async`, `kill_process_group`, `kill_process_tree`,
`kill_process_tree_pinned`, `kill_process_tree_async`, `kill_popen_tree`,
`kill_and_reap`, `terminate_and_reap`, `terminate_and_reap_sync`,
`run_killing_tree_on_timeout` and `stdlib_run_killing_tree_on_timeout`. Pick
one of the two spellings
already in the tree rather than inventing a third: give the mock a pid above
every supported platform's `pid_max`
(`src/kiro_crew/testing/ids.py::UNALLOCATABLE_PID`, or `unallocatable_pids(n)` for
several; `test/test_update_provider.py::_UNALLOCATABLE_PID` is the same value), or
neutralise the killer
and assert the ordering instead
(`test/test_platform_compat_kill_reap.py::TestKillAndReap`). The first spelling IS gated:
`semgrep/mock-pid-allocatable.yaml` (`kirocrew.mock-pid-allocatable`), which
`code-review.yml`'s SAST job runs diff-scoped and blocking, refuses a literal below
`2**32` assigned to `.pid` or passed as `pid=` to a `Mock` / `SimpleNamespace`
constructor under `test/` and `src/kiro_crew/apps/builtins/**/tests/`. It is
unconditional on purpose. Whether a given double is covered by the second spelling stays
a convention, because one file does not carry the answer: the neutralising patch may sit
in a class fixture or a conftest, and the killer is as often replaced one level down
(`os.kill`, `os.killpg`) or behind a module's own private helper, so no per-file rule can
tell a covered mock from an exposed one.

### Config overrides

Use `monkeypatch` to override config paths:
```python
def test_load_from_file(self, tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
```

Chat-runner fixtures that resolve agent bindings must use a concrete
`KiroCrewConfig`. A bare `MagicMock` can claim to contain agents while yielding
no entries, so binding resolution fails before the behavior under test runs.
When stubbing the resolver itself, return `ResolvedBindings` with the intended
member/template selection. A partial namespace can raise on a missing field
before the dispatch guard under test is reached.
Subagent session doubles must return a concrete string from `get_agent`,
including `""` for the default template. Execution identity publication rejects
an unconfigured mock before allocating the provider.
Direct `_run_inner` fixtures create a run record whose execution context matches
`SubagentInfo.execution_context` before dispatch. Cancellation tests for model
provenance wait for the `requested_model` write after execution identity has been
published, so they interrupt the write they intend to exercise.

The backend test floor gives each test an empty advertised-model cache.
Capturing a session response updates this process-global cache, so a later
model-selection test must not inherit another test's wire spellings. Tests that
need advertised models seed the cache within their own fixture or body.

### Filesystem tests

The subagent registry fixture nests `subagents/` beneath a per-test home.
Session execution records also belong to that test home; isolating only the
registry leaf lets repeated session keys leak routing state between tests.

Member execution fixtures must provision their own V2 memory before resolving
bindings. Use `provision_member_memory` inside the isolated test home; do not
bypass ownership checks to exercise an unrelated model or scheduling assertion.
Mocked conversation logs must return a concrete metadata dictionary and its
readability status. Inject member persistence failures at `persist_member_config`
or its config writer, so rollback tests reach the current publication path.

Use `tmp_path` fixture:
```python
def test_custom_work_dir(self, tmp_path):
    client = AcpClient(work_dir=tmp_path)
```

Assert path containment against the fixture's resolved root, not a substring
such as `.kiro/crew` that may also occur in `tmp_path`'s ancestors. Parameterize
path-repair tests with a same-named ancestor directory so this stays independent
of the runner's temporary directory.

**A shared append is not atomic off POSIX, so N processes must not observe through
one file.** `open(path, "a")` is race-free on POSIX because `O_APPEND` makes the
seek-to-end and the write one kernel step; the Windows CRT emulates append with a
separate seek and write, so two processes that reach the end offset together write
over each other and one line is simply GONE. A harness that counts lines to observe
"how many backends launched" or "how many handshakes completed" then reports a number
short of the truth, and the test reads it as the behaviour being broken —
`test_mcp_gateway_pool_integ` counted 11 of 12 windows on Windows while all 12 stubs
had in fact been answered. The clustered writes are the ones that collide, and a
coarse clock creates them: several processes sleeping the same delay wake on the same
15.6 ms tick. Fix by removing the shared file, not by locking it — one file per
writer (`fake_pool_mcp_server._record` writes `<log>.d/<pid>.txt`) and a reader that
concatenates them, which needs no cross-platform locking primitive and keeps the
observation closed-box.

**Ship that reader beside the writer and have every consumer import it**
(`fake_pool_mcp_server.recorded`). The layout is the harness's contract, not one
test's private detail, and a consumer that opens the log path itself reads an empty
history — which is indistinguishable from "the subject recorded nothing", so it stays
silent until some assertion happens to expect a non-empty one.
`test_mcp_gateway_pool_integ.test_every_consumer_of_the_fake_reads_it_through_recorded`
pins the import for every module that spawns the fake, because co-location alone does
not stop a second consumer from hand-rolling the read.

**Delete a file a background thread may have open the way the product deletes it.**
Windows refuses to unlink a file while any handle holds it (`[WinError 32]`), and on
every platform a fold that listed a unit's segments before a bare `shutil.rmtree` of its
folder recreates the unit's `.lock` inside it (`[Errno 39] Directory not empty`). A
test that removes crew-log segments or a unit folder with a bare `path.unlink()` or
`shutil.rmtree` races the eager folder,
which reads the unit on its own thread after every entry. Retention removes them inside
`crew_log.eager.paused()`, which holds the folder between batches; a test does the same
after `eager.drain()`, and asserts the hold was granted rather than proceeding without it
(`test_issue_radar_crew_store_fold_bounds.py::test_a_unit_recreated_under_its_id_folds_cold_however_far_its_seq_climbed`).
That hold is only waited for off the event-loop thread: an `async def` test that calls
`store.sweep_expired` or `store.remove_unit` directly proceeds without it, so the fold
woken by the entry the test just flushed keeps the segment open and Windows reports
the unit not removed (`assert (0, 1) == (1, 0)`). Call them through `off_loop(...)`,
as the product's executor-thread caller does; `test_crew_log_off_loop_pin.py` fails
the direct call.

**Hold the eager folder before patching a store read it also makes.** The folder scans
the session slot index on its own thread, and a scan that read the headers before a
test patched `_read_header_line` can assign `_slot_index` after the test's own read,
replacing the map the test is about to check with one that proves every unit. A test
that patches a store read and then inspects the index does it inside
`_eager_folder_held()` (drain, then `eager.paused()`), the way the two unprovable-header
tests in `test_issue_radar_crew_store_write_refusal.py` do.

**Probe a crew log's write lease only after the eager folder settles.** The folder
writes a fold's savepoint under this process's share of the unit's lease, on its own
thread, and a `session/closed` always triggers one (the seal). `assert_drained()`
waits for the WRITER only, so a test that probes the kernel lock from a second
descriptor right after it can land inside that write and read "this process still
owns the log" although the emitter let go. A slow runner widens the window: macOS
reported `a torn-down session with no live turn kept the log` about once a day.
Probe through `_owned_elsewhere` / `_can_take_ownership` in
`test/crew_log_emit_helpers.py`, which call `eager.drain()` first -- the barrier the
history-delete funnel waits on before it claims the lease sole -- or call it yourself
before any other lock probe.

**Hold the crew-log writer for a burst the test counts.** `CrewLogWriter` claims a
unit's whole bucket after its batching pause (`WriterLimits.batch_deadline_seconds`,
20 ms) and the claim takes those entries OUT of the pending count, so blocking
`CrewLog.append` holds the DISK, not the buffer: a burst that submits slower than the
pause -- a loaded Windows xdist worker does -- is split across two claims, and a test
that floods past `max_pending_count` sees fewer rejections than it counted on
(`overflow_writes() == 2`, not 6). Give that writer a `batch_deadline_seconds` the
burst cannot outlast, assert the exact buffered and overflow counts, and end the burst
with `emit.drain_for_shutdown`, which wakes the writer and skips the pause (so does a
waiting `flush`, so a flush in the middle of the burst ends the hold early too). The blocked append's gate must be opened in `finally`: the writer
thread parked in it holds the log handle and with it the unit's write lease, which the
file's autouse teardown reports as a lease that outlived its test and the next test
on the worker inherits as an error (`test_crew_log_edge_exhaustion.py`).

**A fixture that changes an input a worker thread reads publishes each value as a new
file.** `os.replace` onto a file another thread holds open fails on Windows with
`[WinError 5]`, and an `open` that races the rename fails too, which a lenient reader
such as `check_memory_available` takes as "no reading" and admits on. Write the next
value to a fresh path and rebind the attribute the reader resolves at call time, so a
reader that took the old path finishes the old file
(`test_spawn_floor_defaults.py::_Host.set`).

### Host tool dialects

A test double for a platform-specific CLI must not depend on another host's
flags. The BSD `stat -f %Lp` fixture reads real permission bits with Python's
`os.stat` and `stat.S_IMODE`, rather than delegating to GNU `stat -c %a`.
Reject unsupported arguments and verify actual modes; never substitute a fixed
successful response for the permission check. Quote paths and bound subprocesses.

A test that runs a GNU-only shell form through bash (`date -d`, `grep -P`, `sed -i`
with no attached suffix, `stat -c`, a bare `sha256sum -c` on stdin, `find -printf`,
`base64 -w`) probes the host first and puts a shim or a skip in front of it, the way
`_has_gnu_date`, `_pcre_grep_path` and `_host_has_gnu_sha256sum` do; the macOS
shards run it on BSD tools. This covers a workflow step the test extracts, since
Actions runs that step on Linux but the test does not. A shell script outside
`.github/` uses the portable form. `test/test_gnu_only_shell_usage.py` enforces both
and holds the reviewed exceptions.

### Shell stand-ins that trap a group signal

A shell stand-in that must run a TERM/INT trap when the test signals its
process GROUP keeps its long step out of that group. macOS `/bin/sh` is bash
3.2: when the step it `wait`s on dies of the same group SIGTERM, bash can
return from `wait` with the step's 143 and exit without ever running the trap.
That failed about 1 run in 150 on a loaded Mac and looked like "the rollback
never happened". Run the step in a group of its own, the way cli.sh's
`_run_step` does on macOS, let the trap stop it, and record its pid so teardown
can reap that group too:

```sh
# WRONG: the group SIGTERM also kills the waited step; bash 3.2 may skip the trap
trap restore TERM
sleep 30 & step=$!
wait "$step"

# RIGHT: only the shell gets the group signal; the trap stops the step
trap restore TERM
set -m 2>/dev/null || true
sleep 30 & step=$!
set +m 2>/dev/null || true
echo "$step" > "$D/steppid"   # teardown reaps this group too
wait "$step"
```

The trap is also given the suite's safety net as its grace, never a product
deadline such as `UPDATE_INSTALLER_TERM_GRACE_SECS` (4 s): a loaded runner can
stall the rollback for longer ([Production deadlines are not test
boundaries](#production-deadlines-are-not-test-boundaries)). A product helper
whose grace default is bound at `def` time cannot be raised by a fixture; read
the module constant at call time so its binding is the one seam.
`test/test_update_installer_graceful_stop.py` is the example.

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

`make_dir_link` lives in `kiro_crew.testing.links`; the conftest re-exports it, and
app-embedded tests under `src/kiro_crew/apps/builtins` import it from there.

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
# WRONG — handlers/__init__.py only re-exports the constant; the sessions
# family still reads its own module global (test silently waits the real 10s):
monkeypatch.setattr("kiro_crew.dashboard.handlers._SHUTDOWN_TIMEOUT_SECS", 0.05)

# RIGHT — patch the sessions facade, whose mirror writes the owner that reads it:
monkeypatch.setattr("kiro_crew.dashboard.handlers.sessions._SHUTDOWN_TIMEOUT_SECS", 0.05)
```

One exception: a **forwarding facade** sends a write or delete of a re-exported
name to the module that owns it, so a test patches through the facade and reaches
the owner. `kiro_crew.agent`, `acp.client`, `acp.runtime`, `apps.backend`,
`apps.registry`, `artifacts`, `cli_doctor`, `onboarding_import` and the AWS Control
`backup` / `packaging` modules are facades of this kind (see `_ReExportModule` in
`agent.py`). Never `mock.patch(..., create=True)` a forwarded name: on restore it
skips the set and leaves the owner without the name, which
`test/test_agent_refactor_create_guard.py` catches. A module-valued name (a shared
module the facade holds) cannot be rebound at all; patch its attributes instead.

A **mirrored facade** is the variant for a split whose owners import each other's
names: `kiro_crew._facade_mirror` indexes, once, every owner that binds a name, and a
write or delete through the facade lands in all of them (and in the facade when it
binds the name too), so a patch of `facade.name` reaches every reader the unsplit
module had, module-valued names included; a name the patch creates, such as a builtin
shadowed with `raising=False`, lands on every owner that lacks it as well. Patch a name
that two modules of the family bind through the facade, never on one owner: the
facade's undo would restore every holder to that owner's patched value. A name one
owner binds alone may be patched in that owner. Never write the facade's namespace dict
(`vars(facade)[...]`, `facade.__dict__[...]`, `mock.patch.dict` on either): that skips
the mirror, so no owner sees the write. `test/test_facade_mirror_guards.py` refuses all
three shapes for every facade that serves a mirror:
`test_no_test_patches_a_mirrored_facade_name_with_create_true`,
`test_no_test_patches_a_shared_name_on_an_owner_directly` and
`test_no_test_writes_a_mirrored_facade_namespace_directly`.

A module first imported while a patch is active binds the patched object by name
(`from x import f` copies the object), so the patch outlives itself in that
module. For `kiro_agents_dir` and `kiro_sessions_dir`, redirect through
`config.paths._agents_dir_override` / `_sessions_dir_override`, which every
binding reads inside the function body.

The opposite mistake is a patch WIDER than the caller under test. A patch on a
module global is seen by every thread in the process, so a stub with a side effect
also fires for a background worker that calls the same function. MEASURED: a resume
test simulated a concurrent winner as a side effect on `members.read_dm_binding`, and
the member event log's legacy fold calls that function on its `eventlog-io` worker,
queued by the winner's own row. When the worker reached it before the request
returned (a loaded runner), the "winner" ran twice and the test read
`history duplicated: 2 copies`, with two different message ids. The same test was
green for the wrong reason: its side effect fired at the handler's FIRST binding
read, so deleting the late re-check it was named for left it passing.

So patch the function the handler awaits in the window you mean, and pin the ORDER
of the awaits and checks the test relies on (`TestResumeGuards` records a timeline
and asserts it exactly): a count or a call position stays green when a refactor adds
a call or drops the one you meant. A side effect that stands in for a concurrent
request runs on the loop: from the worker thread `asyncio.to_thread` gave the patched
function, `asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=...)`. Only
from a worker: on the loop thread that `.result()` blocks the loop it is waiting on,
so assert the thread first.

Unit tests that exercise a caller's handling of a subprocess result stub its imported
launch helper. For example, `cloud.aws.run_aws` tests stub `cloud.aws.popen_limited`;
patching stdlib `Popen` underneath it still runs executable resolution and can fail
before reaching the stub on a host without the AWS CLI. Keep the caller's action
guards and result/interrupt assertions real; launcher enforcement belongs in the
launcher's own tests.

Config binding tests unrelated to memory provision real private stores for named
members through `provision_member_memory`. Only the reserved `default` assistant
can use V1. Workspace fallback and alias resolution assertions must not depend on
an invalid member-to-global binding or disable private-file validation.

### A faked host condition changes with the check that reads it

A test often fakes a host condition the suite cannot have for real: the default
data home (`beacon.is_default_home`), the platform, a privilege, a governance pin.
The fake and the production check that reads it are one contract, split across
two files. Change them in the SAME pull request:

- **Adding a check** on a host condition (`if not is_default_home(): return False`)
  means searching `test/` for every test that reaches that code, and giving the
  ones that assert the "yes" branch the fake the check now needs.
- **Removing a fake** (from a shared fixture such as `home`) means searching for
  every assertion that still depends on it, not only the call the fake was added
  for: `git grep -n "<function the fake answered>" -- src test`, then read each
  production caller and the tests that reach it.

MEASURED: one pull request removed a shared `home` fixture's
`is_default_home() -> True` fake, because the function it was written for stopped
reading the home. A second pull request, written in parallel, made another check
return `False` off the default home. Each was green on its own base; once both
merged, that check's tests were red on every run of `main` (`assert False is True`)
and blocked every open pull request until the fake came back, scoped to their class.
Repeat runs cannot catch this shape, because it fails every time: the guard is the
search, done by whichever change lands second, plus a rebase onto current `main`
before the merge.

Scope the fake to the tests that need it (a class-level autouse fixture), not the
shared fixture, and keep one test that drops it and proves the "no" branch
(a `home` case in the same class). A fake in a shared fixture outlives the check it
was written for, and nothing reports that it now hides a branch.

### Transport readiness before event assertions

Before triggering a broadcast, a WebSocket test waits for each connection's
initial `slots` frame. `ws_connect()` completes the HTTP upgrade, while `api_ws`
can still be awaiting allowlist and app-scope loading before `register_ws()`, and
a registered socket can still be waiting for its slot list, its frames held until
the snapshot is queued (`ws_snapshot_hold.HELD_FRAMES_KEY` is `None` once it is).
For SSE, the initial `dashboard` frame proves registration. A delivery fence
orders events within registered queues; it cannot establish that a connection
joined before an earlier event. Use bounded frame receives to establish readiness.

### Loop-wiring tests stub every dispatched operation

A test that drives a periodic/maintenance loop (e.g. `SessionManager.
_cleanup_loop`) pins the loop's *wiring* — which operations run, with what
args, and when. Stub **all** of them: any sweep left unstubbed runs for real
against the dev machine (process-table scans, `~/.kiro/crew` PID files), which
violates the isolation rules below and costs seconds per test (an unstubbed
`find_orphan_mcp_candidates` alone added ~9s to every `TestCleanupLoop`
test). The sweep's own behavior belongs in its own module's tests.

### Golden payload tests: the mechanism that makes "unchanged" checkable

Some subsystems assemble one large output from many contributors. The first-turn
context payload is the case that has one —
[`test/test_memory_v1_golden.py`](../../../test/test_memory_v1_golden.py) pins the v1
default memory path. Every contributor there already has behavioural tests, and none
of them can see the property that matters when the subsystem is refactored: that the
**whole assembled payload** is the same. A block can be added, reordered, doubled, or
grown past its budget with every per-property test still green, which leaves "the
default path is unchanged" an assertion nobody can check.

A golden payload test closes that by pinning, in one place, what the assembled output
IS: the SET of blocks, their ORDER, each one's character extent and the total, the
recall order of rows inside each block, and the files the build touches. **It is
re-run UNEDITED after every later change to the subsystem it covers. Needing to edit
it is the definition of a regression** — the edit is the diff a reviewer reads, and
its size is the change's real blast radius.

Rules that decide whether one is worth having:

- **The golden values live in the test file as explicit expected structures**, never
  in a committed snapshot artifact. A `.txt` golden invites a blind `--update` that
  re-baselines the regression instead of reporting it.
- **Derive every budget from the production constant** (`context._resolve_caps`, the
  module `_*_CAP` values), never a restated literal — a restated cap goes stale
  silently and the test then pins a number the code no longer reads. Pair the extents
  with an overflow case that lands exactly on the cap, so the extents stay tied to the
  constant rather than to the size of the seed.
- **Split what the golden covers from what it must not.** Content that changes for
  reasons the golden does not cover — the shipped agent prompt, the real skill catalog
  — gets a deterministic stand-in, and its presence on the real default path is
  asserted separately. Otherwise every prompt reword edits the golden and the edit
  stops meaning anything.
- **Normalize only machine-specific absolute paths**, by exact-string substitution.
  Extents are meaningless while a tmp dir's length is inside them, and a fuzzier
  normalization would hide a content change.
- **Pin the clock, and pin ages rather than timestamps** wherever production scores
  against `now`. An absolute `created_at` behind an `exp(-rate * days_old)` decay term
  drifts the ranking every day the suite runs.
- **Make each ranked seed discriminating.** Write the most-relevant row FIRST, so the
  ranked order is the reverse of the insertion order; a seed whose ranked order equals
  its write order proves nothing about ranking.
- **Mark the module `xdist_group`** when the subsystem holds module globals
  (`context._memory_stores` / `_lesson_stores` behind `_stores_lock`), and reset those
  globals through `monkeypatch`, never raw assignment. The floor
  (`conftest_resets._isolate_context_store_caches`) hands every test its own copy of
  both caches and puts the original back after it, because `ContextBuilder.__init__`
  makes its `MemoryStore` the process default: without it, a test that builds one (the
  eval runner, over a temporary directory it deletes) aims every later test's
  default-store writes at its workspace, and on Windows a change-card history write
  re-created the deleted `kirocrew_eval_*` directory and failed the temp-residue floor.

### Refusal tests: assert the refusal, name the rule only at its own bound

A command, path or payload is often refused by more than one check: a gate-wide
cap, then a floor, then a rule. Which one answers first is an ordering detail that
another change may move. On 2026-11-16 #37820 added a test that sent a 128-level
`$( )` nest and asserted the ssh floor's rule refused it, and #37875 added a
32-level substitution cap to the gate. Each was green on its base; merged, the cap
refused the nest first and the rule-name assertion went red on every PR (fixed in
#37964). The command was still refused. Only the name was wrong.

- **A test whose point is "this is refused" asserts the refusal and nothing more**:
  the verdict is a refusal, and the call returned one rather than raising. It does
  not name the rule. A new, earlier check then keeps it green, and a change that
  lets the input through still turns it red.
- **Name the rule only in a test of that rule's own boundary**, and put the input
  inside every OTHER limit it passes on the way. Assert that precondition in the test
  (`depth <= _MAX_SUBSTITUTION_DEPTH`), so a later, tighter cap fails as a broken
  precondition with its own message instead of as a wrong rule name.
- **Probe both sides of the bound** (refused at it, allowed one below it), so the
  test pins the bound and not just "something refused".

```python
# WRONG: the point is "a deep nest is refused", but the test names the floor's rule.
# A gate cap added later refuses it first, and the test goes red although nothing
# got through.
def test_the_gate_refuses_a_nest_past_the_bound_without_an_exception():
    assert _denied_by(_nested_double_quoted(128, "true; echo ssh")) == "sandbox-escape-ssh-self"

# RIGHT: "refused, and no exception" for the deep nest. The rule is named only at
# the rule's own bound, with the input proved to be inside the gate's cap.
def test_a_deep_nest_is_refused_without_an_exception():
    assert security.is_denied(_nested_double_quoted(128, "true; echo ssh")) is not None

def test_the_ssh_floor_refuses_at_its_reading_bound():
    depth = ssh._MAX_SSH_READING_DEPTH
    assert depth <= deny_gate._MAX_SUBSTITUTION_DEPTH, "the gate cap would answer first"
    assert _denied_by(_nested_double_quoted(depth, "true; echo ssh")) == "sandbox-escape-ssh-self"
    assert _denied_by(_nested_double_quoted(depth - 1, "true; echo ssh")) is None
```

`is_denied` returns a verdict or `None`, so the plain call is the "no exception"
check: pytest fails the test on anything it raises. Do not wrap it in a
`try`/`except` that turns an exception into a pass. Review flags this through
`tests-are-deterministic` item 16 in `AUTOSDE.yaml`.

### No change-detector tests

A change detector copies by hand a list or a count that the source already holds (a
registry, the steps of a workflow, a module's imports or exports, the rows a page
draws) and compares the copy with the source. It finds no bug: the only thing that
turns it red is a legal addition. Every new entry must edit the test, and two changes
that each add one either both rewrite one number (git merges them, `main` goes red)
or both add a line at the same spot in one list (the second change conflicts).
Writing the copy one entry per line does not fix this; it only moves the cost from
a red `main` to a conflict. In a 30-day scan of `main`, the lazy-import table in
`test/test_dashboard_server_composition_contract.py` took 76 commits that changed
nothing but its literals, and the base-ref list in
`website/src/test/renderVerdict.test.ts` was edited by every new diff-scoped gate.

Bad, a copy of the workflow:

```ts
const names = wirings.map((line) => line.trim().split(':')[0]).sort()
expect(names).toEqual(['BRAND_BASE_REF', 'CHANGELOG_BASE_REF', /* ... one per gate */])
```

Good, the property each item must hold, over a set read from the source:

```ts
const steps = gateSteps(['ci.yml', 'fast-gate.yml'])   // every step that runs the resolver
expect(steps.length).toBeGreaterThan(0)                // a broken parse cannot pass empty
for (const step of steps) {
  expect(step.env[step.baseRefName]).toContain('github.event.pull_request.base.sha')
  expect(step.env[step.baseRefName]).toContain('github.event.before')
}
```

- **Derive the set from where it lives.** Parse the workflow, import the registry,
  walk the module's AST, read the rendered DOM. Never type the members.
- **Assert what each member must satisfy.** The bug the old copy was meant to catch
  is a member that breaks a rule (a gate with no base ref, a row with a hint but no
  InfoTip, a lazy import moved to module level). Name that rule and check it per
  member. A relation between two sets that both come from source
  (`len(handlers) == len(schemas)`, `set(HANDLERS) == {d["name"] for d in schemas()}`)
  is a property too.
- **Assert the derived set is not empty**, per source when there are several, so a
  derivation that silently finds nothing fails instead of passing.
- **Prove both directions in the PR.** Make the bug in the product file and show the
  test red; add a legal new member without touching the test and show it green.

What stays, and why:

| Kind | Example | Why it stays |
|---|---|---|
| Ratchet | `test/wait_discipline_baseline.txt`, `_ALLOWED_DROPS`, per-site bypass baselines | It may only shrink; a new bad site is what it refuses ([Merge-safe ratchets and counts](#merge-safe-ratchets-and-counts)) |
| Review list | the [whole-tree registration pins](#a-whole-tree-registration-pin-not-selected-by-diff-relevance) for link screens and port verdicts | A new dangerous site is meant to make a person look |
| Contract | wire protocol values, a telemetry schema, persisted settings values, a platform fact | Adding or dropping one silently is a defect. Say which contract in a one-line comment |
| Frozen snapshot | the names a facade served when its file was split | Nobody edits it again |

Enforced by the flake-pattern ratchet: K22 counts, in a Python test, an `assert` or
`assertEqual` that pins a public ALL-CAPS name imported from `kiro_crew` (or read
off a `kiro_crew` module) to `len(...) ==` an integer of 2 or more, or to a literal
list, set or tuple of 3 or more strings or numbers. J7 counts, in a frontend test,
`expect(REG).toHaveLength(n)`, `expect(REG.length).toBe(n)` or
`expect([...REG].sort()).toEqual(['a', ...` for an ALL-CAPS `REG` imported from a
product module, with `n` of 2 or more. Both count per file against the merge-base,
so existing pins are not forced to burn down. A contract pin carries
`# flake-ok: <the contract>` (`// flake-ok:` in TypeScript). Review catches what
they cannot see, such as a list read from a workflow, an AST or the DOM (AutoSDE
`a-ratchet-may-only-tighten`, CHANGE DETECTORS).

### Merge-safe ratchets and counts

Each pull request's CI runs on its own base, and the repository merges without
testing the combined tree (there is no merge queue). So a ratchet, count or budget
test must stay green when two changes that are each green on the same base both
land. Git is the only check between them: two edits to the same line, or to lines
that touch, conflict and the second author rebases onto the first; two edits that
make one line the same new text merge cleanly. A test whose record is one shared
number is therefore broken by design: two changes that each add one site both
write `9` over `8`, git merges that silently, and `main` holds ten sites. Most of
the ratchet reds on `main` in 2026-11 had this shape: the lazy-import total in
`test_dashboard_server_composition_contract.py`, the owner-marked count in
`test_pid_reader_ratchet.py`, and security line-budget rows written as the amount
over budget.

- **First ask whether the test needs a record at all.** A record belongs to a ratchet
  (sites that may only shrink), a review list or a contract. A scan compared with a
  hand list of everything that exists today is a change detector: replace both the
  total and the list with a property over the scanned set
  ([No change-detector tests](#no-change-detector-tests)). Turning such a total into
  one line per item is not a fix.
- **Never pin an aggregate the record already decides.** When a ratchet compares the
  scan with a table, mapping or set, a hand-kept total beside it adds no check and is
  the one line both changes edit identically. Drop it. A number the message must
  print is computed from the table.
- **Key a ratchet's record by what changes.** One entry per file, per site or per change: a
  dict entry on its own line, a line in a baseline file
  (`test/wait_discipline_baseline.txt`), or one row file per change
  (`test/security_line_budget/<change>.txt`). Two changes to different files then
  edit different lines and both land correct; two changes to the same key edit the
  same line and conflict, which is the check working. A single module constant such
  as `_BASELINE_SITES = 143` is the shape this rule refuses.
- **Exact per key, not exact on the sum.** Equality on keyed entries is merge-safe and
  leaves no slack: removing a site lowers its own line. A ceiling (`<=`) on a shared
  total is merge-safe only while nobody may grow into its headroom without a row, so
  a budget grows by rows that carry the change's OWN delta (lines added minus lines
  removed, measured from the diff), never by "the amount over": that is growth minus
  whatever headroom the base had, and two such rows under-count once both merge.
- **Name the residual.** Two changes that add the same number of sites under the
  same key still make the same edit. When that happens in practice, narrow the key
  (file to function).
- **A shared corpus is a shared number.** An assertion over text every change may
  edit, such as the ranking of a search over the whole shipped docs tree or
  `"at most 6" not in text` over a whole prompt, flips when another change adds a
  page or a sentence. Build the corpus the test ranks inside the test, and match a
  whole word or an exact line the test owns, never a substring of shared prose.
- **The per-file line cap is a shared ceiling too.** Two changes that each add a
  few lines to a file just under 2,000 both pass, and `main` crosses the cap
  ([code-style § No file over 2,000 lines](code-style.md#no-file-over-2000-lines)).
  Split a file at the 1,600-line working target, not at the cap.
- **A test skipped for a missing optional extra checks nothing on a change that
  runs without the extra.** A change that makes CI install an extra runs the
  tests it unskips against current `main` in that same change.
- **Some cross-file checks must stay exact, and their red after a merge is a real
  defect.** A facade's `TYPE_CHECKING` manifest compared with its owners' imports
  (`test_updates_handlers_facade.py`), or a doc that must be reachable from its
  directory's index (`scripts/docs_lint.py`): when two changes leave the merged tree
  inconsistent, the tree is wrong. Fix the tree; do not loosen the check.
- **Prove it with a two-branch merge.** In a scratch clone, cut two branches from one
  base, make one change of the counted class on each (different files) and update the
  record exactly as the failure message says, then merge both and run the test. It is
  red before the fix and green after, and the PR body says so.
- **Enforced by the flake-pattern ratchet (K21).** `test/test_flake_pattern_ratchet.py`
  refuses a change that adds a shared-total pin to a test module that walks the
  product tree (`source_corpus`, `rglob` / `glob`, `os.walk`, `git ls-files`,
  `pkgutil.iter_modules` / `walk_packages`, or a source reader imported from a
  `*_helpers` module such as `_owner_sources`, in a module that names `src`, `_SRC`,
  `repo_root` or a package's `__path__`): a count (`len`, `sum`, `.count()`, a name holding
  `count` or `total`) asserted `==` an integer literal of 10 or more in a function
  that reads the walk, or a count compared `==` / `!=` with a module-level ALL-CAPS
  constant whose name holds `BASELINE`, `COUNT`, `TOTAL`, `EXPECTED` or `ROWS`. It
  counts per file against the merge-base like its other shapes, so existing pins are
  not forced to burn down. It does not see a pin enforced as two one-sided
  `if total > X` / `if total < X` checks, or one in a module that reads no walk (a
  doc's count checked against a registry); review catches those (AutoSDE
  `a-ratchet-may-only-tighten`). A deliberate pin, such as a frozen snapshot no later
  change edits, carries `# flake-ok: <reason>`.

### Forwarding calls: a keyword added on one side, a re-call added on the other

The same no-merge-queue gap breaks product code, not only counts. A function that
re-calls itself or a sibling with its own parameters spelled out by hand has to be
edited twice when the callee grows a keyword. On 2026-11-16 one change added such a
re-call of `load_session` (#37955) and another added `kas_memory_mode` to
`load_session` (#36872). Each was green on its base, git merged them cleanly, and the
merged re-call dropped the new keyword (fixed in #38108). `create_session` had the
same drop for `member_context` (#38105).

```python
# WRONG: every keyword spelled by hand; a keyword added to load_session later is
# silently dropped on this path unless someone also edits this call.
async def load_session(self, session_file, resume_sid, *, cwd=None, channel_id="",
                       kas_memory_mode=None, focus_resume=False, harness_state=None):
    with self.keep_record_on_teardown(resume_sid):
        return await load_session(self, session_file, resume_sid, cwd=cwd,
                                  channel_id=channel_id, focus_resume=focus_resume,
                                  harness_state=harness_state)

# RIGHT: pass every keyword both sides accept (kas_memory_mode=kas_memory_mode),
# and pin it with a test that compares the call with the callee's signature
# (test_the_kas_keep_record_reentry_forwards_every_keyword).
```

- **Pass every parameter the caller and the callee share.** When the call leaves one
  out on purpose, say why in a comment at the call.
- **Pin a re-entry against the signature.** A test that builds the keyword set from
  `inspect.signature(callee)` and asserts the call forwards each one fails on the
  change that adds a keyword, whichever change lands second.
- **Enforced by `test/test_forwarding_calls_forward_every_keyword.py`.** It scans
  every non-test Python file (`test/forwarding_call_scan.py`). A call counts as
  forwarding when it passes at least three of the caller's parameters as `x=x` and
  binds at least half of the callee's parameters; it fails when the callee accepts a
  parameter the caller also accepts and the call does not pass it. A callee that only
  takes `**kwargs` and hands them on is followed to the function it calls. A
  deliberate drop is one row in `_ALLOWED_DROPS`, keyed `(file, caller, callee,
  parameter)` with its reason, so two changes that each add one edit different lines.
  A row that matches nothing fails, so the table cannot rot. A known bug whose fix is
  open goes in `_OPEN_BUGS`, which is not stale-checked, so the fix and the table can
  land in either order. It resolves callees by name and skips a name whose definitions
  disagree, so a call through a differently named alias is not seen. It costs 6 to
  8 s, because it parses single defs, not whole files (see the module docstring).

### Bare session handles: one factory, so a new runtime read lands in one place

A unit test of one `AcpSessionHandle` method often skips `__init__` and sets only the
fields that method reads. Such a handle breaks as soon as the method starts reading
something new. On 2026-11-17 #38228 made the KAS start barrier await
`settle_kas_model_select()`, which reads `self._runtime`, and stubbed it in the bare
handles that existed then. #38761, cut before #38228, added a new file with its own
bare handle. Each was green on its base. Merged, both of the new file's barrier tests
raised `AttributeError: 'AcpSessionHandle' object has no attribute '_runtime'` and
held 15 PRs red (fixed in #38961).

```python
# WRONG: the handle has exactly the fields this file knew about on its base. A
# handle method that reads a new runtime attribute breaks this file on merge.
handle = AcpSessionHandle.__new__(AcpSessionHandle)
handle._session_id = _SID
handle._mcp_report = McpSessionReport()

# RIGHT: the factory gives a real, never-spawning AcpRuntime and stubs the steps
# tests of one method do not exercise; the change that adds a read updates it once.
from session_handle_helpers import bare_session_handle

handle = bare_session_handle(_SID)
handle._mcp_report = McpSessionReport()
```

- **Build every constructor-less handle with `bare_session_handle()`**
  (`test/session_handle_helpers.py`). Its default `_runtime` is a real `AcpRuntime`,
  so every attribute a handle method reads exists with its real default. Pass
  `runtime=<double>` to steer it, or `runtime=None` for a handle with none.
- **A change that adds a handle step a bare test should not run stubs it in the
  factory**, not in each test file.
- **Enforced by `test/test_bare_session_handle_guard.py`.** It parses every file under
  `test/` and fails on `AcpSessionHandle.__new__(...)` or
  `object.__new__(AcpSessionHandle)` outside the factory. An exemption is one row in
  `_EXEMPT` with its reason; a row naming a missing file fails.
- **Measured:** on `f588907dcc` (both changes, no fix) the file failed 2 of 10. With
  only its `_handle()` switched to the factory, 10 of 10 passed, with or without the
  factory's `settle_kas_model_select` stub: the real runtime alone is enough. On a
  branch where #38761's file still used `__new__`, the guard failed and named it.

### Channel wire fakes: borrow the library's read side, do not model it

`kiro_crew.testing.fake_channel_wire` fakes a channel's `aiohttp` session one layer
below the client, so the real client, transport, dispatcher and renderer run against
canned vendor bytes. A fake at that seam has a standing hazard: a response object
written by hand can disagree with `aiohttp` in ways that raise no `AttributeError`,
so the suite verifies the client against *our model of `aiohttp`* rather than against
`aiohttp`, and stays green while production refuses the same bytes.

The rule that removes it: **the response read side is `aiohttp`'s own code.**
`_FakeResponseCM` binds `ClientResponse.json`, `.text`, `.get_encoding`,
`.raise_for_status` and `.ok` onto itself and inherits `HeadersMixin` for
`content_type` / `charset`, supplying only the attributes those methods read
(`_body`, `_headers`, `headers`, `status`, `reason`, `request_info`, `history`,
`_in_context`, `_resolve_charset`, `release`).
Content-type enforcement, the `+json` suffix match, the `content_type=None` bypass,
empty-body-reads-as-`None`, charset decoding and the `status < 400` rule are then
decided by the library, not by this repo. `test_fake_channel_wire.py` asserts that
identity directly: a local re-implementation of any of those methods fails the suite
regardless of what it returns.

Do not construct a real `ClientResponse` to get this. Its `__init__` is private and
churns across minor releases, which is the fragile surface; the read-side method
bodies are not.

Borrowing a method means supplying what it reads in the **type** it reads it in, not
merely under the right name. `_headers` is a `multidict.CIMultiDict`, the mapping a
real response carries, because `HeadersMixin` looks a header up by `aiohttp`'s own
spelling: under a plain `dict` a fixture writing `content-type` is a second, separate
field that the lookup walks past, so the default answers instead and `.json()`
accepts a body production refuses. That is the very divergence the borrow removes, so
`multidict` is a declared direct dependency rather than `aiohttp`'s transitive one.

#### Third-party HTTP test doubles: declined for this harness

`aioresponses` was evaluated as a replacement and **declined**. Recorded so the
question is not reopened without new facts:

- It does not run on the `aiohttp` this repo resolves to. `aioresponses` 0.7.9, the
  latest release, declares `aiohttp<4.0,>=3.8` but constructs `ClientResponse`
  directly with a `writer=` keyword; on `aiohttp` 3.14.x -- what `setup.cfg`'s
  `aiohttp>=3.9,<4` resolves to, with no lockfile and no CI ceiling -- that raises
  `TypeError: ClientResponse.__init__() missing 1 required keyword-only argument:
  'stream_writer'`. Adopting it costs either a first-party `response_class` shim,
  which restores the hand-written model it was meant to delete, or an
  `aiohttp<3.14` ceiling on a **runtime** dependency to serve a test-only concern.
- It reaches no further than the rule above. Both patch at the session boundary, so
  both inherit the same response semantics; the delegation does it without a
  dependency and without coupling to a private constructor.
- It cannot replace the harness outright. `FakeWireWebSocket` scripts frames for the
  WeCom streaming reply, and `aioresponses` is HTTP-only, so the best available
  outcome was a split harness rather than one deleted file.

Outbound **encoding** fidelity is out of reach for either option and remains an
accepted limit: `aiohttp` encodes a body inside `ClientRequest`, which is built below
the `client._session` seam this harness replaces, so a body recorded here is the
value the client passed, not the bytes a real request would carry. Assert on
`RecordedRequest.form` / `.json_body` with that in mind.

#### Path hardening in test utilities is not a precedent

`channel_fixtures.py` resolves fixture paths with `O_NOFOLLOW` containment and an
atomic replace. That is shipped and correct, but its inputs are test-authored strings
inside a single-user trust boundary, so it sets no expectation that test utilities
carry attacker-grade path containment. Spend that review effort on the governance and
keystone paths instead.

### Close the SQLite handles and skills loaders a test opens

Every object that opens SQLite in its constructor holds three descriptors (`db`,
`-wal`, `-shm`) until it is closed. On CPython 3.11+ an unclosed `sqlite3.Connection`
is a reference cycle, so dropping the object does not free them: they stay open until
the cyclic collector happens to run. Across one xdist worker's share of a shard they
add up.

The limit they hit depends on the host:

| Host | Default soft `ulimit -n` |
|---|---|
| macOS (a laptop, a CodeBuild Mac) | 256 |
| GitHub `macos-15-arm64` and `macos-26-arm64` runners | 10240 |
| GitHub Linux runners | 65536 |

So CI does not show these leaks. Measured on a 256 host: one worker ended a backend
shard with up to 335 leaked descriptors, and from the 256th on every test on that
worker failed with `EMFILE` or `sqlite3.OperationalError: unable to open database
file`, in files that had nothing to do with the leak. Raising the limit hides the
leak and is not the fix.

The fix depends on the owner:

| Owner | What opens SQLite | How it is closed |
|---|---|---|
| `SkillsLoader` (also built inside `ContextBuilder` and the dashboard's standalone loader) | `skill_search_index.sqlite3` | The rootdir conftest's autouse `_close_skills_loaders_the_test_opened`, for every test: it closes each loader built after it sets up, joins the catalog worker, and closes every search index built during the test, including one whose loader was already dropped. Nothing to request. |
| `SubagentManager` | `tasks.db` | Request `close_subagent_managers` (a module-level `pytestmark` usefixtures entry). |
| `VectorMemoryStore`, `KnowledgeStore`, a `SkillsLoader` from an earlier-ordered autouse fixture | their own databases | Wrap the object in `opened(...)`. |
| A new class that opens SQLite in `__init__` | its database | Give it a `close()`. Its tests close it through `opened(...)` or an opt-in fixture shaped like `close_subagent_managers`. |

A loader that a module- or session-scoped fixture shares across tests is set up before
the floor takes its snapshot, so it stays open. A loader the test body builds and
stores in a module global is closed at that test's teardown. A closed loader serves
only what it had already cached, and an empty list for any scope it had not built, so
a later test must not reuse it: such a cache already points at a deleted `tmp_path`,
and the test that reads it sets its own loader.

To find a leak, record `os.listdir("/dev/fd")` (`/proc/self/fd` on Linux) before and
after each test from a throwaway `-p` plugin. Charge every new descriptor to the test
that opened it, and list the ones still open when the worker ends. A run with
`ulimit -n 256` on macOS confirms that the leak is gone.

## Which conftest you are standing on

There are **two** testpaths (`setup.cfg`'s `testpaths = test
src/kiro_crew/apps/builtins`) and they do **not** get the same fixtures. Know which
floor is under your file before you decide what to isolate yourself:

| Your test lives in | It inherits |
|---|---|
| `test/` | the rootdir `conftest.py` **and** `test/conftest.py` |
| `src/kiro_crew/apps/builtins/**/tests/` (one level down for most apps, deeper for `aws_control/crew/packaging/tests/`) | the rootdir `conftest.py`, plus that app's own `tests/conftest.py` where one exists (currently `auto_improvement`, `code_review_sage`, and `spec_builder`) |
| `src/kiro_crew/apps/builtins/**/*_tests/` (today only `aws_control/crew/runtime/container_tests/`) | the rootdir `conftest.py`, plus its own `conftest.py`, which puts the docker build context on `sys.path` so the suite imports `container.*` the way the image does. Patterns written as `**/tests/**` skip it. |

The **rootdir `conftest.py` is the host-mutation floor**: everything in it protects the
developer's machine rather than the correctness of one suite, so it holds for all
testpaths. It pins `$XDG_CONFIG_HOME` and the launchd paths, traps the spawn
funnels against service mutation, pins `KIROCREW_HOME` and the import-time `~/.kiro`
bindings, scrubs the inherited shell-preload and exported-function variables
`name_grant` refuses on (`BASH_ENV`/`SHELLOPTS`/`BASHOPTS`, `BASH_FUNC_*` keys,
and the legacy `() {` value spelling — a RHEL-family host inherits `BASH_FUNC_which%%`
from `which2.sh`, and the refusal outranks every narrower code), redirects
`tempfile`'s base, and fails the run on residue in the
checkout.

It also pins the other real host paths a test must not reach: the subagent registry (a
running gateway sweeps stray entries there as orphans), the 610MB embedding-model
download, and the agent-state sidecar.

It also carries the [Determinism contract](#determinism-contract-read-this-first)'s
floors. Two of them act: `_kiro_crew_package_attr_floor` puts back every `kiro_crew.*`
module, parent-package attribute and root-package global a test replaced, evicted or
rebound (and reports the test; `KIROCREW_PKG_ATTR_STRICT=1` fails it instead; a module the
test imported meanwhile stays, bound to what it saw), and
`_reset_member_eventlog_singleton` drains the member event-log queue (class 4). The rest
only REPORT, in one `[determinism floors]` warning per worker at session end: a test that
changed the process time zone, left work running or queued on a shared
`kiro_crew.executors` pool, reached a host other than loopback (a test marked
`@pytest.mark.real_network` is exempt; `KIROCREW_NET_STRICT=1` refuses the call and fails
the test, except a datagram route probe, which sends nothing), or signalled the worker
itself, its xdist controller, a process group, or, on Linux, a sibling xdist worker or
another account's process. A network or kill finding is charged to the test running when
it happens, so a background thread an earlier test left behind is reported against
whichever test it outlives into. They report before they may fail anyone, so their counts
are read from a full CI run before a strict switch (set to `1`) is turned on. The
worker's last test is checked after that warning is written, so its findings are not in
it. The package-attribute floor also undoes a replacement made by a wider-scoped fixture
that a test created lazily (`request.getfixturevalue`) at that test's teardown. One more
floor acts only under a switch: with `KIROCREW_CREW_LOG_READ_BUDGET=raise` (the Linux
`Backend Tests` job) every test counts its crew-log reads as the gateway and fails at
teardown on an over-budget read at a product site not listed in
`test/conftest_floor_read_budget.py`, unless its module resets the budget in its own
fixture.

Eight members are there for a different reason — a **process-global** that any testpath
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
* `_no_leaked_embed_evictor` fails the test that loads an embedding model and leaves its
  idle evictor running. `LlamaCppEmbedder` starts the daemon `kc-embed-evict` thread when
  a model is published, and every tick re-reads the memory config through `config_dir()`,
  so a backend a test never `close()`d keeps resolving the data home from under every
  later test on the worker; one of those ticks landing in a test that had cleared
  `os.environ` is how the home guard came to blame `test_dashboard_origin.py` for a leak
  from `test_custom_embedding_model.py` (class 4). Attributed by difference and stopped
  through its owner, like the exporter guard.
* `pytest_runtest_teardown` cancels every debounced session-tree checkpoint write the
  test armed (`_cancel_session_tree_checkpoints`, through
  `session_tree_projection.reset_for_tests`), after the fold worker is retired and before
  the pins lift. The write sleeps a debounce on the gateway's `mc-maint` pool, which no
  loop join reaches, so without the cancel it fires inside a LATER test. Its paths are
  pinned when it is armed (the caller-resolves rule), so this is the owner paying for its
  own queued work rather than a repair of the resolution.
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
* `_close_skills_loaders_the_test_opened` closes every `SkillsLoader` the test built,
  through production's weak registry of live loaders, and joins each one's catalog
  worker. It then closes every `SkillSearchIndex` built during the test (the
  session-scoped `_record_skill_search_indexes` records them), which reaches the index
  of a loader the test dropped before teardown. A dropped loader keeps its SQLite descriptors until the cyclic collector runs,
  and on a macOS host with the default `ulimit -n 256` that runs a worker out of
  descriptors (§ Close the SQLite handles and skills loaders a test opens).
* `_restore_autonudge_singleton` puts `autonudge._INSTANCE` back to whatever the test
  inherited. It lives in `test/conftest_resets.py`, bound by `test/conftest.py`, rather
  than the rootdir floor, because only the `test/` suites drive the service; it is
  listed here because its failure shape is the process-global one this section is about.
  `AutoNudgeService.start()` publishes itself there and `stop()` clears it, so
  a test that starts the service — or drives a dashboard handler that does — leaves a live
  instance holding timer TASKS created on that test's event loop. Every later test in the
  same worker then reaches those tasks through the singleton on a loop that has since
  closed, which is how `test_dashboard_chat_session_authority.py`'s `TestCloseBroadcastDurability` came to
  answer 500 from a leak in an unrelated file. Restored rather than blamed, for the same
  reason the CWD restore is: production really does publish this singleton. The teardown
  retires the leaked instance's timers through `_cancel_timer`, which is the one place
  that knows a task on a closed loop must be DROPPED rather than cancelled — `Task.cancel`
  schedules through `loop.call_soon` and raises `RuntimeError: Event loop is closed`.
* `_forget_gatewayd_telemetry_override` drops the process-level telemetry provider
  `gatewayd._prepare_telemetry` installs (`metrics.provider.use_telemetry_provider`)
  and the gatewayd role mark. It lives in `test/conftest_resets.py`, like the
  autonudge restore. A test that runs `gatewayd.main()` in process leaves both set, and
  every later test on the worker then resolves OTLP egress through gatewayd's provider
  and ignores the one it installed with `set_context`. That is how
  `test_config_patch.py`'s
  `test_enable_is_refused_when_the_egress_posture_cannot_be_resolved` came to answer 200
  instead of 409 after `test_mcp_gatewayd_coverage.py`'s `TestMain` on the same worker.
  Reset at setup through the module's own `reset_for_testing()`, and only when the
  override is set.

It registers the xdist worker budget too — the policy is in the repo-root
`xdist_budget.py`, a plain module rather than a second conftest, because the module
name `conftest` is ambiguous: `test/` precedes the repository root on `sys.path`, so
`import conftest` from a test in `test/` can never reach the rootdir file. A distinct
name is reachable from both and resolves to one module object, which matters because
the held slot descriptors are module state.

`test/conftest.py` holds the rest: suite-specific isolation (Slack thread state, the
model-window cache, the platform context, …) and the Windows collect-ignore list.

Both conftests keep most of their definitions in plain modules beside the suite, never
collected: `test/conftest_floor_*.py` for the rootdir file and `test/conftest_*.py` for
`test/conftest.py`. Each conftest binds every fixture and hook back into its own
namespace, because pytest registers them from there. The rootdir file loads its parts
by file path (`_load_floor_part`), since `test/` is off `sys.path` for the in-package
suites. A patch on a conftest rebinds that conftest's global only, so a function that
reads a name a test patches there stays in the conftest beside it, and so does what a
test needs fresh each time it loads `conftest.py` by path (the audit core, the residue
guard). `test/test_conftest_parts_reach.py` resolves the patches the test trees make on
a conftest it can name (a by-path load, a plugin lookup, `import conftest`, a helper
parameter every caller fills with one) and fails on a reader in a part, apart from
listed readers the patching test never runs. It fails closed on a patch call it cannot
resolve in a file that reaches a conftest; a plugin written as a string is listed there
by hand. A source pin whose assertion covers every site in a conftest (absent,
counted, listed) reads it joined with its parts. Anything else a test reads on a
conftest is bound from its part. New isolation goes in the part whose docstring names
its job, and a new fixture or hook is also added to its conftest's import block, or
pytest never sees it. A per-test reset or pin for the `test/` suites is not a fixture of
its own: it is a function marked `@per_test_guard` (`test/conftest_guards.py`), written
like a fixture (setup, one `yield`, teardown) and taking only `request` and
`_floor_monkeypatch`. Five autouse fixtures there (`_b_guards` to `_o_guards`) run every
guard around every test, in name order, so the suite pays pytest's per-fixture work for
five names and not for one per guard. `seal_guards` at the end of `test/conftest.py`
raises at collection for a guard the import block does not bind.
`test/test_conftest_guards.py` pins that every guard runs, that one guard's failure
does not skip another's teardown, and that a fixture named like a guard fails the test
that defines it. A guard a test must override or request by name stays a fixture, and
its name is added to `SEGMENT_BOUNDS`. The local related-test selection matches the name `conftest.py`
only, so after editing a part also run the floor's own tests
(`test/test_host_isolation_floor*.py`, `test/test_host_service_guard.py`,
`test/test_testing_determinism_helpers.py`, `test/test_conftest_parts_reach.py`).

Host-side `PodConfig` paths do not derive from `KIROCREW_HOME`. Tests that publish
pod state must set `pod_root`, `pods_dir` and `artifacts_dir` under `tmp_path` on
their configuration object. Keep the real publisher and reader so the fixture
proves persistence without depending on an existing directory in the host home.

Provider-stub tests of member routing must retain real member provisioning and
canonical member/store validation in an isolated test home. Memory version adds
no platform-capability admission. Tests of ordinary host sandbox behavior must
exercise the real sandbox capability checks rather than substituting member-memory
policy.

When you add isolation, put it in the rootdir conftest **only** if a test in any
testpath could damage the host, poison a process global for every later test, or
consume enough of a shared *resource* — memory, cores, disk — to take the machine down
with it. Otherwise it belongs in `test/conftest.py`, where it costs the in-package
suites nothing. The first two of those entries started life in `test/conftest.py` and
were silently absent from the in-package tests, which is how each was found.

Resource consumption belongs on that list for the same reason damage does: a guard
that only covers `test/` is invisibly absent from the built-in-app testpath, and the
failure it was written to prevent — a swapped, unresponsive machine — does not care
which testpath asked for the workers.

## The integration layer (`test/integration/`)

Three layers, told apart by how much of the product is real:

| Layer | Where | What is real | What is fake | Runs |
|---|---|---|---|---|
| Unit | `test/`, `src/kiro_crew/apps/builtins/*/tests/` | one function or one handler on a bare `web.Application()` | everything else | every shard, every platform |
| Integration | `test/integration/` | the whole gateway, booted by `GatewayOrchestrator.run()` in the pytest process, on a `tmp_path` home; real config, stores, policy files, routes | the model (`kiro_crew.testing.fake_acp_backend`) | the `integration` job, Linux, behind `KIROCREW_INTEGRATION=1` |
| E2E | `test/test_e2e_smoke.py`, `test/e2e/`, `test/test_playwright_e2e.py` | a `kirocrew gateway` subprocess, and for the browser suite a real Chromium | the model | the `e2e*` jobs, behind `KIROCREW_E2E=1` |

The middle layer exists because the other two cannot see the seams between
boot steps. A handler test mocks the store the handler reads; the E2E harness
sees only what crosses the process boundary. Neither catches: a memory
binding that `doctor` accepts but workflow creation refuses; a chat that is on
disk before a restart and gone after it; a policy file the boot itself wrote
that the next request cannot parse; a second session starved because the
first holds the event loop. Those all live in one process, between modules,
and that is exactly what a test in `test/integration/` can hold in one hand.

### The fixtures

`integration_home` is a fresh `KIROCREW_HOME` under `tmp_path` with the same
environment the E2E harness sets (`KIRO_HOME` moved under it so the boot's
agent-spec rewrite cannot touch the operator's `~/.kiro/agents`;
`KIROCREW_KIRO_BIN` pointing at the fake backend). Because the boot runs in the
test process, it also keeps the developer's own home and credentials out:
`isolate_user_home` points `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA` and the
XDG data, cache and state roots at an empty `tmp_path` sibling, drops every
declared home-override variable, puts this interpreter's directory first on
`PATH`, and stubs the credit pill's kiro-cli token reader; `isolate_credentials`
drops channel, AWS and GitHub credentials and points the AWS CLI files at paths
that do not exist. Both docstrings in `test/integration/conftest.py` list exactly
what they cover, and `test_boot_smoke.py::test_a_boot_reads_nothing_from_the_outer_home`
fails on any read beneath a stand-in home. `gateway_boot` binds the
boot helper to that home; the boot itself is an `async with` block inside the
test -- this repo's convention for anything whose teardown must AWAIT on the
test's own loop (see "Async tests" above), not an `@pytest_asyncio.fixture`:

```python
@pytest.mark.asyncio
async def test_sessions_survive_a_restart(gateway_boot):
    async with gateway_boot() as gw:
        created = await gw.post_json("/api/sessions", {...})
        await gw.restart()                        # second boot, SAME home
        listed = await gw.get_json("/api/sessions")
        assert created["key"] in {s["key"] for s in listed}
```

`get`/`post`/`put`/`patch`/`delete` return the aiohttp response;
`get_json`/`post_json` assert the status and decode. `auth=True` (the default)
sends the dashboard session cookie the boot minted from its token -- not
`?token=`, which is a one-use link nonce the `mixed_internal` routes refuse
once any ordinary route has minted the cookie. `auth=False` proves the denied
side of a contract. For the internal routes a managed MCP server calls
(`/api/session-tool-policy`, the memory routes), `gw.mcp_headers(session_key)`
does the launcher's half of the session-token handshake in-process and returns
the `X-Internal-Secret` / `X-Session-Key` / `X-Session-Token` headers; send them
with `auth=False`. `gw.state` is the live `DashboardState`, `gw.app` the real
`web.Application`, `gw.home` the data home -- use them to assert on what a
request left behind, not to bypass the request.

`integration_home` grants the disposable home unsandboxed-exec consent in its
`config.local.json`, as the E2E suite does for its gateway: the agent binary is
the fake stub, and the CI container has no sandbox backend, so without it every
spawn is refused before the stub runs. Write further per-test config through the
same file by merging, never by replacing it.

`integration_home` also releases the rootdir conftest's agent-spec pin: that
pin sends the boot's spec WRITES to a per-test directory while request-time
READS follow `KIRO_HOME`, so under it the boot would write `kirocrew.json`
where no request reads it. Both sides resolve to `<home>/kiro/agents` here,
which is the private target the shared-home write guard exempts. A test that
edits the agents directory waits for the managed `kirocrew.json` first (the
spec rebuild runs after the dashboard is serving).

`gw.registered_routes()` is every `(METHOD, canonical path)` the live router
serves -- the same reading the coverage ratchet counts -- so a sweep over
"every route of a kind" (`test_route_sweep.py`: every parameter-less `GET`
is guarded and serves without a 5xx on a fresh home; every mutating or
parameterized route is guarded; every parameterized `GET` answers an unknown
id below 500) and the metric agree on what a route is. The ratchet the sweeps
carry is REQUESTED routes: it says every route has been asked for its guard
and its fresh-home answer, not that its behaviour is pinned -- that is what
the seam tests beside it are for. A sweep excludes by
EXACT path with a reason, never by pattern or prefix: the routes that answer
unauthenticated, the ones that hold a connection open, and the ones that
reach the network on a fresh home are each a small table in the test, and a
route that joins one of those classes is added there by hand.

Two operator seams reach the boot without patching anything inside it. A spec
that a SLOT or a SPAWN must resolve by name is written to `<home>/kiro/agents`
BEFORE the boot: the loader answers agent names from a snapshot it scans once
(`_scan_materialized_agents`), so a spec dropped in after the boot is not
dispatchable until the next registration (the loader documents this as accepted
staleness). Config a test needs the boot to read (`agent.spawn_min_memory_gb`,
say) goes in `<home>/config.local.json`, the override file the operator owns.

A chat turn is the real thing too:
`gw.post("/api/chat", {"message": text, "slot": slot_key}, timeout=TURN_SECS)`
returns the SSE response, and the test reads `resp.content` line by line
(`data: {...}` events, `data: [DONE]` last). To order two turns, use the fake
model's `[[SLOW_HOLD:<token>]]` marker (`fake_acp_backend.slow_hold_trigger`):
set `KIROCREW_FAKE_ACP_HOLD_DIR` to a directory under `tmp_path` before the
boot, and create `slow_hold_path(dir, token)` only once the event you order on
has been observed; the held turn streams one chunk and waits there. A cold
session start costs several seconds before the first chunk, so warm each slot
with one plain turn first, bound each awaited signal by a named lost-run
ceiling ([class 6](#6-a-hang-is-a-lost-run-not-a-failed-test)) under the
module's `pytest.mark.timeout`, so a wedged turn fails readably instead of
killing the worker, and assert on the ORDER of what the two streams saw, never on absolute
latency. `test/integration/test_streaming.py` is the worked example.

The directory is a package (`test/integration/__init__.py`) so its conftest
imports as `integration.conftest`. The unit files import `test/conftest.py` by
the bare name `conftest`; a second top-level `conftest` shadows it and 160
files fail to import.

### What the boot helper does that a test must not undo

`run()` ends in `_shutdown_and_exit` -> `os._exit`. `booted_gateway` starts
`run()` as a task, waits until the dashboard answers `/api/health` on the port
it bound, and on exit sets `shutdown_event` -- exactly what SIGTERM does -- so
`run()` walks its OWN exit path: the run-marker settle and clear,
`_shutdown()`, the orphaned-session cleanup, the crew-log and event-log
drains, the log-queue drain. The helper intercepts only `os._exit`
(`conftest.intercepted_os_exit`, held for the WHOLE boot -- a `run()` that
exits on its own mid-test must raise, not end pytest), which raises
`HarnessExit` with the exit code instead of ending the interpreter.
Nothing about shutdown is re-implemented in the harness, so a step added to
`_shutdown_and_exit` runs here the day it lands.
`test_boot_smoke.py::test_shutdown_and_exit_ends_in_os_exit` pins the shape
that makes the interception sufficient: `os._exit` is the last statement of
`_shutdown_and_exit` and the only hard exit `run()` reaches. A `run()` that
does not reach its exit within `SHUTDOWN_PATH_SECS` of the event is cancelled
and `_shutdown()` awaited directly, and the teardown reports it.
`GatewayOrchestrator` has no serve/stop seam an in-process caller could use
instead, so a change to the exit path that trips the pin needs the harness
updated in the same PR.
Do not set `shutdown_event` from a test, do not call `run()` yourself, and do
not `await` the helper's task.

A production gateway is one process for one home and never expects its
process-wide state to be undone; this layer boots many homes in one process,
so the helper undoes it, by four mechanisms the conftest docstring lists in
full: a **reset list** for module globals a boot derives from its home
(signing key, revoked-nonce store, crash-log path, `SafetyOverride`, live
config, autonudge, platform context, embedder and model-download manager); a
**snapshot** of the process settings a boot changes in place (signal handlers,
`os.environ`, the loop exception handler, `RLIMIT_NOFILE`) restored on every
exit; a **wait** for the memory-preparation worker thread to drop its
process-wide fence (`MemoryStartup`), because cancelling its awaiter does not
stop the thread and a second boot on the same home would otherwise be refused
with "Another gateway is still preparing memory" (a fence still held at the
deadline fails the test by name), and for the MCP probe pool, shut down and
joined after the reap below (the next boot builds a fresh one), because a
cancelled probe's thread runs on until its server has exited and that server
creates the boot's home as it starts, so one still starting would put
`tmp_path / "home"` back after the test removed it (an overrun fails the test
by name); and a **reap** of every
asyncio task the boot added that `_shutdown()` left running (production leaves
those to `os._exit`), failing the test by name if one ignores cancellation.
The lists are kept honest by
`test_boot_smoke.py::test_a_second_boot_touches_only_known_module_globals`:
it diffs every loaded `kiro_crew` module's globals across a second boot, and a
changed name that is neither restored by the harness nor listed with a reason
in `_KNOWN_SECOND_BOOT_CHANGES` fails there. When startup grows a home-derived
global, that test names it; put it on the reset list, or on the known list
with its reason, in the same PR.

A global that startup fills from a job it hands to an executor and never joins
(`loop.run_in_executor`, `asyncio.to_thread` it does not await) needs both: the
reset, and a teardown wait until no such job is queued or running. Reset alone
loses to a job that lands after it and writes the old home's value into the next
boot. The witness only sees the job when it lands late, so the PR that adds it
usually passes its own CI and later PRs go red. Make the late landing happen
before you trust the reset: a throwaway `-p` plugin that delays the job's first
run by a few seconds (see *Proving a determinism fix*) must turn the witness red
on the parent and leave it green on the fix.

```python
# WRONG: reset only; a refresh still queued on the default executor
# publishes this home's apps after the reset, into the next boot.
hidden_sessions._app_ids = frozenset()

# RIGHT: teardown waits for the queued and running refreshes, then resets
# under the refresh lock (test/integration/conftest.py).
await _drain_hidden_chat_refresh()
with hidden_sessions._refresh_lock:
    hidden_sessions._app_ids = frozenset()
```

The only fake is the model, but not every production step runs: the boot is
`GatewayOrchestrator.run()`, not `kirocrew gateway`, with `test_mode=True` and
`no_crons=True`. Skipped, and so left to the E2E layer: everything
`run_gateway()` does before constructing the orchestrator (platform boot,
slice limits, the agents-dir janitor, the agent scratch sweep, the kiro-cli
log cap, the telemetry beacon); under `test_mode`, the kiro-cli readiness
probe and the outbound policy-distribution refresher; under `no_crons`, cron
arming and reconciliation. The flags are fixed: a test that needs one of
those steps is an E2E test today, and the helper grows the flag when the
first such in-tree test does.

Every boot is a fresh boot (one per `async with`). That is deliberate: the
bugs this layer chases are state bugs, and a shared boot would let one test's
residue explain another's failure. Budget accordingly -- a boot is about two
seconds here, and a file should hold a few tests, not fifty.

### The metric is routes, not lines

Each request through the handle is attributed to the aiohttp route it
resolved to (`/api/sessions/abc` counts toward `GET /api/sessions/{key}`).
With `KIROCREW_INTEGRATION_HITS_DIR` set the conftest writes the hit set and
the registered-route list per process; `scripts/check_integration_route_coverage.py`
unions them and prints the share of registered routes the suite requested,
`--missing` grouped by path prefix so the next file to write is obvious. A
route served end to end proves the wiring; a line reached through a mock
proves the line exists. Line coverage of the layer is still worth reading
(`--cov=kiro_crew` works as usual), it is just not what the layer is gated on.
`--min` in the `integration` job is a ratchet: a little under what `main`
measures, never above.

### Writing one

- One file per route group or per bug family. Name the contract in the test
  name: `test_bad_spec_does_not_disable_memory_tools_for_other_agents`, not
  `test_policy`.
- Assert three things per route where they apply: the denied side without a
  token, the happy path's status and JSON shape, one validation `4xx`.
- A test that documents a bug we have not fixed is welcome -- mark it
  `xfail(strict=True, reason="GH #<n>")` so the fix flips it and the marker
  has to come off in the same PR.
- Seed the home through the product (a request, or the same store the
  product uses), not by hand-writing JSON the product never wrote.
- Run locally with `KIROCREW_INTEGRATION=1 python -m pytest test/integration/test_x.py -n0`.
  A multi-file run keeps `-n 2 --dist loadgroup --max-worker-restart=2`.

## Rules

- Remote Crew chaining cases using a real `HopPortGuard` obtain a kernel-selected
  loopback port with `bind(("127.0.0.1", 0))` and keep that socket bound until the
  guard's first hold of the port, which closes it just before binding: the fake
  forwarder binds nothing, so a port released at once is free through the whole
  connect and mint, and another worker can be handed it first. Fake-only cases
  retain inert guards and their fake port band. The port is only a hint: tests
  still exercise the production handoff, assert real ownership and cross-process
  bind refusal, and close real guards in `finally`.

- **Host-floor patches use `_floor_monkeypatch`, never the test's shared
  `monkeypatch`.** The rootdir fixtures keep path redirects, service guards,
  download/telemetry switches and policy/preload scrubs on a private undo stack.
  A test can override them with `monkeypatch`, and undoing that override restores
  the floor without removing it. The public `monkeypatch` fixture depends on
  `_floor_monkeypatch`, so its stack unwinds before the floor at teardown;
  reversing that order can reinstall a stale per-test path or delete a restored
  inherited environment value. Tests
  should use `monkeypatch.context()` for a temporary override instead of calling
  the shared fixture's `undo()`. The undo regression tests deliberately exercise
  that misuse with inert sentinels and path/guard identity assertions, without
  invoking an unprotected filesystem or process operation.

- Tests MUST NOT spawn real kiro-cli processes
- In-process calls to `cli.main()` clear the inherited sandbox-active and tier
  markers as part of CLI startup hardening, and its real console initializer
  publishes the UTF-8 process contract. The root isolation floor snapshots
  `KIROCREW_SANDBOX_ACTIVE`, `KIROCREW_SANDBOX_LEVEL`, `PYTHONUTF8` and
  `PYTHONIOENCODING`, then restores all four to their exact prior values
  (including absence and explicit emptiness) after the test's monkeypatches are
  undone. The CLI still performs both mutations during the call; tests must not
  disable either guard. The floor regression uses test-owned streams so Windows
  stream reconfiguration is observed without changing pytest's capture streams.
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
     **import-time bindings**. The floor patches only modules already in
     `sys.modules`, so a module first imported inside a test body (for example
     `dashboard.handlers.mcp`, reached lazily by `onboarding_apply._mcp_lock`) is
     not patched; such a test patches the binding itself.

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

- **Tests MUST NOT derive an expected value from the repo's declared version.**
  `kiro_crew.__version__` is an input the checkout controls, not a constant: a release
  branch declares `X.Y.Z-rc.N` by contract ([release](../../build/release.md)), a
  nightly tree carries `.dev<stamp>`, an insider wheel its own suffix. `main` declares
  a bare release, so an expectation COMPUTED from it is green there and red exactly
  where a release is decided: `f"{__version__}.12"` for a `BUILD_VERSION` stamp
  production honours only over a bare numeric base passed on `main` for months while
  reddening `release.yml`'s `release-candidate-tests` — the same-SHA gate every
  prerelease tag must clear before a promotion record can be assembled — and every
  local run and back-to-`main` PR off that branch with it. Pin a synthetic base
  instead, and when the test synthesizes the package under test, rewrite the literal
  there so the test owns that input outright (`test/test_build_version_override.py`'s
  `_PINNED_BASE`). Comparing the SAME live value on both sides is fine and is not this
  rule — "`--version` reports the string the package declares" IS the contract; it is
  computing a DIFFERENT string from the live one that assumes a shape no branch
  guarantees.

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

- **Never name one of the repository's own files by a bare relative path.** A
  structural test that reads the source it pins has to name a file, and
  `Path("src/kiro_crew/dashboard/ws.py")` names it against whatever directory the
  process started in. That holds only while pytest is launched from the root: from a
  subdirectory, an editor's runner, or any wrapper that changes directory first, the
  same literal resolves to nothing and the read raises `FileNotFoundError` before a
  single assertion runs — a failure that says nothing about the behaviour under test,
  which costs a CI round and teaches readers to re-run past it.

  Ask the **module under test** where it lives, which is the form that cannot go
  stale: a module that moves or is renamed fails at import, and the path cannot name a
  different file than the one the rest of the test exercises.

  ```python
  from kiro_crew.dashboard import ws
  source = Path(ws.__file__).read_text(encoding="utf-8")
  ```

  When no module owns the file — a workflow, a script, a spec — resolve the repository
  from the test file instead: `Path(__file__).resolve().parents[1] / <relative path>`.
  Independent of the working directory, though it still spells the layout by hand.

  `scripts/check_cwd_relative_repo_reads.py` enforces this over the collected test
  trees. A relative literal that is only COMPARED, or joined onto a resolved root, is
  not a violation — the allowlists elsewhere in this suite are exactly that shape —
  and a file that changes directory itself is skipped, because a relative path there
  names the tree the test built. One access can carry `# cwd-ok: <reason>`.

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

  The third instance of that shape is the **metrics consent-recheck worker**
  (`metrics/provider.py`): once per `_CONSENT_RECHECK_SECS`, `get_recorder()` -- on the
  path of `skills`, `session`, `context`, `hooks` and every metric helper -- hands a
  consent re-read to a daemon thread whose `KiroCrewConfig.load()` reaches
  `config_dir()` and `mkdir`s whatever `KIROCREW_HOME` names at that instant. The
  recorder is process-global and nothing resets it between tests, so the read lands a
  few milliseconds later under the NEXT test's pin and creates that test's data home
  from a thread it never started. The Windows fingerprint of that race is a
  `\\?\`-prefixed path: `os.path.realpath` keeps the prefix when the directory above a
  not-yet-existing path appears between its two lookups, which is how
  `test_pathres_loop_starvation.py::test_the_inline_anchors_are_the_bounded_anchors`
  saw `\\?\...\<n>-kirocrew-home\diag` in one build of the sensitive-target set and not
  the other (`diag` is the first anchor under the crew home; a recorder *creating*
  `diag` cannot produce that string -- an anchor that exists at both lookups resolves
  prefix-free). MEASURED by replaying one Windows shard's file list with a probe on
  every `os.mkdir` of a per-test home: this thread was the only off-main-thread
  creator of a home that was not the running test's own. The rootdir teardown hook
  joins it (`_join_telemetry_consent_worker`, bounded by `_EXECUTOR_JOIN_SECS`) beside
  the receipt-worker join, and fails the test that left one wedged past the bound.
  When you see a per-test directory appear with no test writing it, list the threads
  first: `threading.enumerate()` names the lazy resolver, the filesystem never will.

- **A cwd-relative default in a constructor is a write into the checkout.**
  `TaskRunner(work_dir=None)` falls back to `Path.cwd()`, which under pytest is the repo
  root, and its first save wrote `runs.json` there. The rootdir conftest's repository
  residue guard did not flag it because the name happens to be gitignored — so a
  gitignored artefact is exactly the one that leaks silently. Always pass
  `work_dir=tmp_path` (or the equivalent) to anything whose default is the process CWD,
  and when you add such a default to production code, add the test that constructs it
  with an explicit directory.

- **Production code that edits `os.environ` leaks through a test that exercises the real
  path.** `dashboard/server.py` startup exports the bound-port evidence into
  `os.environ` on purpose (descendant processes read it), and `load_credentials()`
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
  `src/kiro_crew/apps/builtins`) pins `$XDG_CONFIG_HOME` to a tmp
  dir so `dev_fleet._dropin_path()` cannot name the operator's real
  `~/.config/systemd/user/kirocrew.service.d/` (or `kirocrew-gateway.service.d/`
  on a host whose live unit has that name), and traps every stdlib
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
- **Register the destruction of anything you create, in the same scope.** Take a
  directory from pytest's `tmp_path` or `tmp_path_factory.mktemp()`; pytest owns it,
  removes it, and the temp-residue guard below does not scan it. In a `unittest`
  class, an autouse fixture method hands `tmp_path` to `setUp`. A `tempfile.mkdtemp()`
  or a held `tempfile.TemporaryDirectory()` lands in the shared temp dir instead, and
  `kirocrew.test-shared-tmp-dir` refuses a new one (see the residue guard below):

  ```python
  # WRONG — unittest does NOT run tearDown when setUp raises, so this leaks on
  # every setUp failure, and it is the failing run nobody watches that leaves it
  def setUp(self):
      self.tmp = Path(tempfile.mkdtemp())
      self.client = build_client()          # raises -> tearDown never runs
  def tearDown(self):
      shutil.rmtree(self.tmp, ignore_errors=True)

  # RIGHT — pytest made the directory before setUp ran and removes it either way
  @pytest.fixture(autouse=True)
  def _per_test_dirs(self, tmp_path):
      self.tmp = tmp_path / "root"
      self.tmp.mkdir()
  def setUp(self):
      self.client = build_client()

  # WRONG — held on self, the directory lives until someone calls cleanup();
  # a setUp failure, or a thread still writing after it, leaves it behind
  def setUp(self):
      self._td = tempfile.TemporaryDirectory()
      self.root = Path(self._td.name)

  # RIGHT — the same tmp_path fixture method; no handle to clean up
  @pytest.fixture(autouse=True)
  def _per_test_dirs(self, tmp_path):
      self.root = tmp_path
  ```

  `with tempfile.TemporaryDirectory() as d:` around synchronous code is still fine:
  the block removes it, and the rule does not refuse it.

  The rootdir conftest contains the *class* as well: `tempfile`'s base is redirected
  per run to `<platform temp>/kc-pytest-<user>-<pid>`, which the run removes at the end,
  so an unregistered directory no longer accumulates in the shared temp root forever.
  Residue there is still **reported** — relocation is not absolution.

  On **macOS** `<platform temp>` is forced to `/tmp` (`_SHORT_TMP_BASE`), which is what
  Linux and CI already resolve to. launchd's per-user temp dir is
  `/var/folders/<2>/<30 random>/T`: long enough that an AF_UNIX socket under a pytest temp
  dir exceeds Darwin's 104-byte `sun_path` and cannot bind at all. That is a property of the
  host prefix rather than of the code under test, so such a test fails locally while CI
  stays green.

  A run only ever deletes the root it created itself — there is deliberately no sweep of
  other runs' roots, because every signal for "that directory is abandoned" is unsound from
  inside a test process: the name can be pre-created by another local account, and a pid
  means nothing across PID namespaces (two containers sharing a bind-mounted temp directory
  can each hold the same one). So **a run killed before its teardown leaves one directory
  for the platform to reclaim** — `systemd-tmpfiles` on a timer, macOS's periodic cleanup, a
  tmpfs cleared on reboot. That reliance is deliberate and is worth knowing if you own a
  long-lived CI host: it is bounded at one directory per killed run.

  Fatal in CI's `Backend Tests*` jobs, a warning elsewhere. Those six jobs in `ci.yml`
  set `KIROCREW_TMP_RESIDUE_STRICT=1`, so a test that leaves residue fails the shard it
  runs in. A local run only warns, because a developer host has its own tools writing under
  the redirected `TMPDIR` that CI does not have; set the variable yourself to hold a
  local run to the CI rule. Two classes under that root are deliberately **not** residue
  and are excluded by name: the computer-use screenshot spool, which production keeps as
  a persistent ring buffer, and the scratch that Chromium and the Playwright driver
  create because a child inherits the redirected `TMPDIR`.

  A residue line names the test that created the entry when `tempfile` made it in that
  worker. The usual cause is not a missing cleanup but a late writer: a background
  thread that still holds a path under the removed directory and calls
  `mkdir(parents=True)`, which re-creates it. The crew-log eager fold worker is one: a
  fold still running when a test removes its private `KIROCREW_HOME` re-creates that
  home. Use the conftest's per-test `KIROCREW_HOME` pin instead of a private home,
  because the conftest stops that worker before the pin lifts. A store that `mkdir`s
  its root on READ is another: the Issue Radar store's `data_dir(root)` does, so any
  reader still running after a `unittest` cleanup re-creates a `TemporaryDirectory`
  root that cleanup just removed. In a `unittest` class, take that root from
  `tmp_path` through an autouse fixture method (`test_issue_radar_deps.py`'s
  `_per_test_dirs`), because pytest owns that directory and this guard does not scan it.

  `kirocrew.test-shared-tmp-dir` in `semgrep/test-determinism.yaml` refuses a NEW line
  in any test file (`test/**`, the builtin apps' `tests/` and `*_tests/`, `conftest.py`)
  that makes a directory there pytest does not own: `tempfile.mkdtemp(...)` in any
  spelling (`tempfile.mkdtemp`, an imported `mkdtemp`, a chain such as
  `np.tempfile.mkdtemp`), unless `dir=` is `tmp_path`, and a `tempfile.TemporaryDirectory(...)`
  that is held instead of used as a `with` header (assigned, stored on `self`, returned,
  passed on, `.name` read, or entered by hand). An `ExitStack.enter_context(...)` of one
  is allowed. Every such shape leaks the same ways: a `setUp` failure skips `tearDown`,
  a thread or child process outlives the cleanup, or a reader `mkdir`s the root again.
  The SAST job runs it diff-scoped, so the sites already in the tree are not
  findings until a change adds one; a site that needs the shared temp dir on purpose (a
  short AF_UNIX socket path under `short_tmp_base()`) says why with
  `# nosemgrep: kirocrew.test-shared-tmp-dir`. The `mkstemp`, `NamedTemporaryFile(delete=False)`
  and `gettempdir()` shapes are not refused: every current site of them cleans up.

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
  `a-ratchet-may-only-tighten` in `AUTOSDE.yaml` says when that guard on a new test is not
  a loosening.

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

  Entries in `windows-expected-failures.txt` are node ids **without the `@group`
  suffix**. `[params]` is optional and the matcher treats the two spellings
  differently: a line with no `[` is compared param-stripped (`_base_nodeid`) and so
  covers **every** parametrization, while a line **with** `[` is compared with its
  params intact (`_ungrouped_nodeid`) and covers **only that one**. Naming a single
  parametrization is what makes a strict xfail expressible for a test whose params do
  not all fail: when two parametrizations fail on Windows and one passes, a single
  base entry would either un-track the two or red the job forever on the one via
  XPASS.

  Stripping `@group` on both sides is load-bearing and unconditional: under the
  default `--dist loadgroup`, xdist rewrites a grouped test's nodeid to
  `<nodeid>@<group>`, so a matcher that only split on `[` matched a *different* string
  for grouped vs ungrouped tests and for `-n0` vs `loadgroup` runs. Never add the
  `@group` suffix to an entry — it makes the line match in one invocation and silently
  miss in another.

  **macOS uses the same list mechanism, not a second one.**
  `test/macos-expected-failures.txt` is applied by the same rootdir
  `_apply_tracked_gap_list` matcher, with the same node-id spelling and the same
  burn-down semantics: anything not on the list still fails the macOS shards — which
  since the lane moved to `platform-tests.yml` means it fails the NIGHTLY and holds the
  nightly publish, not a pull request, so a widened list is worth the same scrutiny with
  a day's delay before anyone notices. Prefer a
  precise `skipif(sys.platform == "darwin", reason=...)` on the test when the reason is
  a named capability difference; use the list when the gap is a real one to be fixed
  later, with a `# TODO` reason line above the entry. `test/macos-collect-ignore.txt`
  exists for the blunt case only — a file that cannot be *collected* on darwin.
- **When you fabricate a child environment, `HOME` and `PATH` are a PAIR.** Substituting
  one while inheriting the other is the defect, in either direction. An inherited `PATH`
  on a developer host routinely leads with a version-manager shim directory (mise, asdf,
  pyenv, volta, nodenv), and a shim resolves its tool set from `HOME` — so with a
  substituted `HOME` the bare name `python3` or `node` reaches the MANAGER, which finds
  no tool state and **spins forever instead of exec'ing an interpreter**. Nine such
  processes were measured reparented to init at 1464% CPU between them for six days;
  another site turned it into a permanently blocked xdist worker. Either pin the real
  interpreter's own directory first on `PATH`
  (`test_security_conductor_scripts_verify_finding.runnable_python` is the shape) or
  resolve the tool to its real executable before building the env. Do
  NOT drop the `HOME` substitution to fix it — that is usually a blast-radius bound
  somebody chose on purpose.

- **A child must use the tool the fixture discovered.** In
  `ai_review_workflows_helpers.py`, keep the temporary `gh`/`sleep` stub directory
  first, the directory of `shutil.which("jq")` next, and Unix defaults last.
  Putting defaults before the discovered directory can select an older system
  jq that misparses adjacent JSON pages. Keep the adversarial input and the
  missing-tool skip; check other commands used by consumers before promoting a
  directory that also contains sibling tools.

- **Bootstrap fixtures must match the child's libc target.** In
  the retired browser-CLI installer helpers, `_expected_node_base` used native
  `os.confstr("CS_GNU_LIBC_VERSION")` on Linux/x64 to select `glibc-217` below
  glibc 2.28. OS and architecture alone give the fake mirror the wrong archive,
  failing before checksum and cleanup assertions. Keep the real platform tool
  outputs and verification assertions.

- **Pin discovery inputs beyond the mocked call.** A test controlling
  `shutil.which` must also pin explicit candidate locations consulted first
  (`TestPySpy`). Preserve the candidate discovery assertions.

- **A reimported module is restored in both places.** Tests that reimport a module
  restore its `sys.modules` entry and its parent package's child attribute, including
  their original absence, and remove temporary module entries before restoring the
  saved objects. The pysqlite3 fallback tests left `kiro_crew.knowledge.store`
  pointing at the stdlib-backed temporary module after restoring `sys.modules`, so
  the next schema-behind test failed to catch the optional driver's exception. Verify
  by running the fallback and its consumer in order in one `-n0` process.

- **Keep source annotations outside generated configuration text.** An inline
  `# wokeignore:rule=master` belongs on the Python literal, not inside its SSH config
  value, where OpenSSH treats it as extra option arguments.

- **A spawn that can outlive the test gets a process-GROUP reap, not `kill()`.** A child
  is routinely a wrapper that forks, so killing the direct pid reaps the wrapper and
  leaves the real work running; a bounded `wait()` that ends in a bare `pass` then
  reports success. Start the child in its own session/group and reap the group:
  `start_new_session=True` + `os.killpg` on POSIX, `CREATE_NEW_PROCESS_GROUP` +
  `taskkill /T /F` on Windows. `test/installer_test_helpers.run_bounded` is the
  cross-platform reference and `verify_finding.reap` the stdlib-only one. Reap on EVERY
  exit path, not only the timeout.

- **A walker rooted at the REPO ROOT must prune `.worktrees/`.** It is gitignored and
  holds other branches' entire checkouts, so a corpus or ratchet gate that descends it
  audits code that is not on this branch and reports offenders nobody on this branch can
  fix. CI has no `.worktrees/`, so CI stays green and only the developer running the
  repo's own documented worktree workflow sees the red. Prefer `git ls-files`, which
  never had the problem; if you must walk the filesystem, prune `.worktrees` alongside
  `node_modules`, `.venv`, `build`, `dist` and `.git`.

- **A capability `skipif` must observe the tool's VERSION, not merely its presence.**
  `shutil.which("node") is not None` is not "node works here": `import.meta.dirname` is
  undefined before Node 20.11, so two tests ran and failed on a host whose `PATH` led
  with Node 18 while the repo declares `engines.node >= 22.12.0`. Gate on the floor the code
  under test actually needs, and probe it the way the tests will experience it.

- **Sandbox only the cases whose subject is the sandbox.** On macOS each sandboxed
  spawn costs about 0.3 s (`sandbox-exec` compiles the Seatbelt profile on every exec),
  and one git call is several spawns. A test of worktree or git logic runs plain git;
  only a case that tests the sandbox composition asks for it, the way
  `test_crewmate_worktree.py`'s `sandboxed_git` fixture does. The wrapper itself is
  tested in `test_worktree_create.py`.

- **Scan only the cases whose subject is the hook scan.** Every git call through
  `worktree._run_git` (so every `git_ops` read) first runs `git_config_hooks`' scan: three
  more gits (`config`, `rev-parse --show-toplevel`, `ls-files -s`), plus one per
  submodule. Windows process start is slow on a loaded runner, so a test that drives a
  service refresh after refresh pays four spawns per read. MEASURED (#39484):
  `test_slack_code_channel_bar_budget.py` tests took 58-107 s each on
  `Backend Tests (Windows) (8)`, and the six-refresh
  `test_the_bar_is_resent_only_when_its_items_change` took 91-95 s at four xdist workers
  and 145-174 s at six, so it ran past `--timeout=180` on unrelated PRs and on main. A
  test of logic ABOVE git, over repositories with no config hooks, stubs the scan at EVERY
  name that binds it, next to its `sandboxed_spawn_argv` stubs:
  `worktree.config_hook_disable_args_sandboxed` for `_run_git` (#39505) and
  `git_ops.config_hook_disable_args_sandboxed` for `git_ops.run_git_capped`, the Code-tab
  diff reader, which imports the scan itself. MEASURED (#39484): the code-channel
  workspace, board and surface suites with both stubs, four xdist workers, AWS c6i.2xlarge
  Windows, median of 5: `test_slack_code_channel_workspace.py` 114.6 s -> 42.1 s,
  `test_slack_code_channel_board_diff.py` 104.6 s -> 40.4 s. The scan and both callers'
  use of it are tested in `test_git_config_hooks.py`.

- **Count the git spawns of a parametrized case, and spend each one on a new answer.**
  A case multiplied by its parameters multiplies its process starts, and on a loaded
  Windows runner a git spawn is the cost. Run each case only from the directories that
  can answer differently, and do not re-ask an answer that is a pure function of one the
  case already asserted. MEASURED (#39930): the 51 cases of
  `test_prepare_pr_pre_push_gate.py::test_one_hostile_config_key_changes_nothing_from_a_subdirectory`
  took a median 525 worker-seconds of `Backend Tests (Windows) (4)` over 10 main runs.
  From two directories instead of three, without re-asking the gate selection, they
  start 19 gits instead of 34; four xdist workers, AWS c6i.2xlarge Windows, 5 rounds
  each: 86.2 -> 57.5 worker-seconds.

- **If you stub the only thing that releases a resource, the fixture owes the release.**
  A permit, an in-flight claim, a lock: when the green-path test replaces the runner
  whose `finally` gives it back, the resource is gone for the life of the worker. The
  victim is whichever later test asserts on capacity — it passes for the wrong reason,
  or waits for a permit that will never come and takes the worker with it. Restore to
  what the test INHERITED, not to a pristine value, so one leak is not re-reported
  against every test after it. In production code, treat everything between acquiring a
  resource and entering the `try` that releases it as a leak window.

- **A test that asserts a credential is REMOVED builds it with
  `kiro_crew.testing.synthetic_credentials`, never a literal.** Use `github_classic()`,
  `aws_secret()` or another helper there, not the AWS documentation example key
  `AKIAIOSFODNN7EXAMPLE` or any other `AKIA…` key ID. The structured redactor keeps AWS
  key IDs and documentation example keys visible as identifiers, so a test planting one
  goes red or green with detector policy, not with the code under test. Each PR is green
  on its own base, and both landing reds `main` on every platform: #36640 planted the
  example key and asserted it gone after #29423 had made the redactor keep it, and
  #37423 fixed the four test files by switching them to `github_classic()` (why: #37421).
  `test/test_redaction_literal_ratchet.py` refuses a new `<AWS key ID literal> not in`
  or `assertNotIn` site; its `_BASELINE` lists the older ones and only shrinks.
  Asserting an identifier SURVIVES is the case where an `AKIA…` literal belongs.

- Tests SHOULD be fast (< 1s each)
- Async tests MUST use `@pytest.mark.asyncio` — and ONLY async tests. The mark on a
  plain `def` is accepted silently by pytest-asyncio strict mode and the test then
  asserts nothing it was meant to `await`; the warning it emits is easy to miss among
  thousands. A module-level `pytestmark = pytest.mark.asyncio` makes every sync test in
  the file wrong.

## Side effects: what a full run does to the host, and how to see it

The audit passes in this section are evidence, not rules: the rules are the
[Determinism contract](#determinism-contract-read-this-first) and the Rules above.

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
  streams file text and caches only the tree's path list, and exposes `_clear_caches()`
  for a module-scoped teardown; it used to memoise ~160 MB of source text for the life of
  the worker, paid by every later test on it (see the [tenth pass](testing-sweep-findings.md#what-a-tenth-five-run-pass-found-linux-16-workers-126905-tests-per-run)). And mark the module
  as one `xdist_group` (see "Keeping the suite fast"): a per-module cache that xdist
  spreads over five workers is warmed five times.
- **A repo-WIDE corpus scan enumerates via git, never the filesystem.** `rglob` and
  `os.walk` from the repo root descend every gitignored tree and every checkout nested
  under it, so a worktree under `.claude/worktrees/` (the Claude Code harness creates
  them there), a local `.kirocrew-dev/` data home or a scratch clone puts a second copy
  of every shipped file in front of the gate. That is not only a false positive naming a
  path the author cannot edit: where the gate asserts `any(...)` over its matches — the
  coverage omit contract does — a stale copy keeps satisfying it after the real file lost
  the property, and the gate fails OPEN. Use `source_corpus.repo_files()` /
  `repo_files_named(...)`, which asks `git ls-files --cached --others --exclude-standard`
  (untracked-but-not-ignored included, so a new file is policed before it is `git add`ed)
  and keep the gate's own scope filter — `_vendor` is TRACKED, so git names it just as a
  walk would. Its no-git fallback is reachable ONLY where there is no `.git` (an sdist) or
  no git binary: a checkout whose `git` call merely FAILED — a leaked `GIT_DIR`,
  `safe.directory` — raises instead, because a fallback there answers wider than git and
  no count floor catches a surplus. Skipping one directory by name is not the fix: the set
  of nested trees is open, and `test_source_corpus.py` pins the rule instead — no test
  under `test/` or `scripts/` may root a recursive filesystem scan at the repo root.

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
  `slack.handler` (through `test/conftest_slack.py`), which built `_PHASE_EMOJIS` by calling `KiroCrewConfig.load()` at import —
  and loading resolves `config_dir()`, which `mkdir`s `~/.kiro/crew`. Import-time reads of
  the data home peek first (`peek_data_home()`) and load only when the file already exists.
- **Bytecode written into the checkout by import-by-path.** Loading a script with
  `spec_from_file_location` + `exec_module` writes `__pycache__` beside it —
  `packaging/signing/`, `.github/scripts/`. Wrap the `exec_module` in a scoped
  `sys.dont_write_bytecode = True`.
- **Unbounded `lru_cache`s in a script under test.** `scripts/leaf_test_scope.py` used to
  cache the TEXT of every `.py` it scans, exactly right for one CLI run and wrong for a
  long-lived worker (143 MB retained, +291 MiB on one test); it now streams each file and
  caches only the derived name index, and the test module still clears the caches at module
  teardown.
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
- **An agent's shell inherits the live `KIROCREW_HOME`.** When `KIROCREW_SCRATCH` is set
  beside it, `conftest_floor_home` treats the value as inherited rather than chosen and
  installs the import-time floor anyway, under `$TMPDIR` first and the system temp dirs
  after. The run's own scratch (`$KIROCREW_SCRATCH`, its `TMPDIR`, and the floor an xdist
  worker inherits through `KIROCREW_TEST_HOME_FLOOR`) is not live data for the guard when
  it sits strictly inside `<home>/scratch/`, so that floor passes
  `_refuse_a_real_data_home`. Another session's scratch tree, the scratch root and a live
  home exported by hand are still refused. Before this, the refusal sent agents to `HOME=/tmp/<name>`, and each fake
  `HOME` rebuilt the uv, npm and bytecode caches under it: those homes exhausted the
  `/tmp` tmpfs inode budget on a developer host. When `HOME` is faked anyway, the guard
  also checks the password-database home, and `conftest_floor_configure` keeps the
  bytecode and hypothesis caches under that real home's `.cache/kirocrew`, the path an
  unfaked run uses.

The third full-run audit ran five backend and five frontend runs on a **macOS** host,
from inside a Kiro Crew agent session, with the audit hook attributing every write,
spawn, connect and kill to a test and a per-test census of duration, RSS, threads and
descriptors. Both changes of venue mattered: the two earlier audits ran on Linux, and a
suite that is clean there had 227 deterministic failures and four 120-second hangs on a
Mac, plus host writes the Linux runs could not see. Zero backend flakes in 5 × 92k
tests; the classes below are what the rest was made of.

- **`monkeypatch.undo()` in a test body unwound the floor, and the order of the two
  stacks was luck.** The same class the [second pass](testing-sweep-findings.md#what-a-second-five-run-pass-found-windows-host-88k-tests-per-run)
  closed with `_floor_monkeypatch`; this pass caught `detect()` creating the real `~/.kiro/crew` through it and
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
  `IMPORT_TIME_METRIC_EMITTERS`, resets the recorder for the next module, and fails
  that module's collection report. Pytest/xdist therefore fails the job on every
  file shard, independently of where `TestNoMetricIsEmittedAtImport` runs. That
  test also asserts the record is empty. Build such values inside the test or fixture.
- **A test writes `config.json` through the config lock, never around it.** Any load
  of a document that still needs a migration writes it back as a locked
  read-modify-write, and a load runs on threads the test does not own: the telemetry
  consent recheck starts one from the first metric call after its window lapses,
  which in a long-lived worker is a database query in the test's own setup. A plain
  `write_text` or `atomic_write` lands between that migration's read and its write and
  is replaced by the migrated pre-write document, so the next load answers field
  defaults with no warning (`assert 7 == 3` on `memory.backup_keep` in
  `test_memory_store_dashboard.py`). Write a setting with
  `await asyncio.to_thread(update_config_locked, path, mutate=...)`, reading the
  document inside `mutate`, which is how the dashboard writes one.
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
  `test_source_providers.py` (and the topic modules split from it, which import its
  fixtures from `source_providers_helpers.py`) scheduled a detached repo-visibility
  refresh whose `to_thread` resolved the provider CLI through `workspace_root()`; the
  join drains it, and those modules also stub the scheduler (a recorder, so the calls
  stay observable), because no test there is about visibility and a side task that
  never starts has nothing to drain.
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
- **A real PTY test must not source the operator's profiles.** The ordinary terminal
  integration tests run through a Bash-named shim that execs the resolved Bash with
  `--noprofile --norc -i`. Keeping the shim's basename `bash` preserves the production
  readiness-marker path without letting `/etc/profile`, `~/.bash_profile`, or an automatic
  tmux attach redirect test input into a developer's live pane. Tests whose subject IS login
  profile behavior replace `HOME` with their own temporary profile; the fixture detects that
  explicit handoff and preserves the real `bash -l` path for those tests only.
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
  CALLED, drain the chain, then assert (`renderPopoverSettled`). A gate that reads a
  latch an effect sets AFTER the commit lags its source by one commit: read the live
  conditions in the same commit as well as the latch.

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

### The five-run pass records

Each repeated five-run measurement of the full suite, with the host, the worker count,
what it found and how each class was closed, is in
[testing-sweep-findings.md](testing-sweep-findings.md). The rules those passes produced
are the ones above and in [Determinism: the seven flake classes](#determinism-the-seven-flake-classes);
the records are evidence for them, not extra rules.

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
  `cap_project_root_walk`, and `cap_node_module_walk` for the ACP adapter's
  `node_modules` search in `acp.client._node_module_search_dirs`) rather than
  assuming the host's temp root is bare.
- **A built turn can carry host load.** `ContextBuilder.build_message` prepends a
  `[RESOURCES]` advisory when the host is under pressure, so a test that compares
  built turns requests the `ample_host_resources` fixture.
- **Fixtures and helpers the suite provides.** Owner-gated handler tests wrap the
  app with `dashboard_owner_helpers.as_owner` (its state has `owner_id = ""`; a
  `MagicMock` owner id never matches). MCP launches are approved by an autouse
  fixture; a module opts out with `ENFORCE_LAUNCH_APPROVAL = True`, and
  `test_gateway_spawn_and_env_tests_choose_launch_approval_fixture` fails a new
  gatewayd spawn/env test file that neither opts in nor is exempted.
  Line-oriented JSON readers are tested against `stray_line_helpers.STRAY_LINES`
  and `too_deep_to_decode()`. An in-process child `main()` that arms an `os._exit`
  watchdog (`pdf_extract_child.start_rss_watchdog`) kills the xdist worker, so stub
  the starter.
- **Socket state and the leak sentinels are checked after every test.** A hook in
  `test/conftest.py` restores `socket.socket`, `socket.socketpair`, `AF_INET`,
  `AF_INET6`, `asyncio.proactor_events.socket` and each `LEAK_SENTINELS` entry, and
  fails the test with `test left ... rebound (restored)`. It is a hook rather than an
  autouse fixture so it reads the state after `monkeypatch` has undone, which is why
  it lives in the root conftest.
- **A fixed system path is the host's, not the test's.** `service.linux` reads
  `/etc/kirocrew/kirocrew.env` to decide whether uninstall deletes it, so on a host where
  a real install left the untouched seed there, a test that pinned only `UNIT_PATH` saw
  `rm` and `rmdir` it never caused. `test/service_helpers.py`'s autouse `_no_host_env_file`
  (imported by `test_service.py` and its sibling suites) points `ENV_DIR` and
  `ENV_FILE_PATH` at an absent `tmp_path` directory for every test; pin every module-level
  `/etc` (or other absolute) path the code under test reads, not just the one the test is
  about.
- **Host data a check reads is the host's, not the test's.** The doctor's `tz data` row
  reads the zone files in use, and on a host whose data predates IANA tzdb 2026b it adds
  "timezone data out of date" to the issues, so 13 whole-report doctor tests exited 1
  there. A test about another row pins `_tz_probe_offset` and `_tz_data_from_system`
  (`cli_helpers._doctor_reads_current_tz_data`); the row's own tests are in
  `test_doctor_tz_data.py`.
- **A real kiro-cli in a known install directory is found without `PATH`.**
  `pin_kiro_cli` looks in the fixed install directories (`~/.local/bin` among them), so
  an unpinned writer gate spawned the host's `kiro-cli --version` inside the sandbox,
  whose ceiling seal created `<data home>/profiles` between a preview and its save, and
  the save failed `stale_revision`. The same real binary wrote `mcs-telemetry-l1-*.log`
  and `kiro-log` into `$TMPDIR` from whichever test probed it first in a worker, so every
  shard ended with a temp-residue red. The rootdir floor's `_hide_the_operators_kiro_cli`
  now drops the directories under the REAL account home from `known_kiro_cli_dirs` at
  every module binding (`_KIRO_CLI_DIR_BINDINGS`, ratcheted by
  `test_floor_operators_kiro_cli.py`), so a test that never chose a kiro-cli sees "not
  installed", as on CI. A test that wants one passes its own home or plants it through
  `KIROCREW_KIRO_BIN`. A test that reaches the spec-permissions gate still pins
  `installed_kiro_cli_version` (`agent_capabilities_helpers._accepting_kiro_cli`), which
  states the version it means.
- **Who owns a binary, and where a home's stores point, are the host's.** The grep
  endpoint refuses a ripgrep owned by another user (a dev image's `/usr/local/bin/rg`
  owned by uid 1001). A test whose subject is the rg ENGINE's answer pins
  `_grep_rg_executable` to the ripgrep on `PATH` (`_pin_rg_on_path`); the provenance
  check keeps its own tests. A store under
  the operator's home can be a symlink out of it (`~/.claude` on a dev box mounting a
  shared one), and a target that resolves outside the root is correctly not emitted, so
  a test that asserts every store under `HOME` gives the test a fresh `HOME`.
- **Probe a tool on the `PATH` the child gets.** The LFS test checked
  `shutil.which("git-lfs")` on pytest's own `PATH` and then ran git with `CLIENT_PATH`,
  where a git-lfs found only through a launcher directory does not exist; look it up with
  `shutil.which(name, path=<the child's PATH>)`.
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
- **A run started under `nohup` ignores SIGHUP in every worker.** An ignored signal
  stays ignored across `exec` (a handler does not), a POSIX shell cannot trap a signal it
  started with ignored, and `push_guard.py` keeps an inherited ignore on purpose, as
  `nohup` expects. So a test that hangs up its child waited out its timeout on a host where
  the suite ran under `nohup`, and passed on CI. Spawn a child the test will interrupt
  inside `spawn_test_helpers.unblocked_signals()`, which covers SIGINT, SIGTERM and SIGHUP.
- **A system tool can be a script.** coreutils-single (Amazon Linux 2023, Fedora minimal
  images) installs `sleep` as `#!/usr/bin/coreutils --coreutils-prog-shebang=sleep`, and
  for a script the kernel reports the interpreter's argv, so `/proc/<pid>/cmdline` read
  `/usr/bin/coreutils --coreutils-prog-shebang=sleep /usr/bin/sleep 300`. A test that
  reads a live child's argv or name back spawns `spawn_test_helpers.plain_sleeper_argv()`:
  `sleep` where it is a binary, this interpreter where it is a script.
- **A child interpreter must import the tree under test, not the installed one.**
  `setup.cfg`'s `pythonpath = src` reaches only pytest's own interpreter; a
  `sys.executable -c` child resolves `kiro_crew` from the venv's install. On CI that is
  the checkout being tested, so CI stays green; in a linked worktree that borrows another
  checkout's venv, every child imported THAT checkout, and a child calling a symbol the
  worktree added failed with `AttributeError`. The rootdir conftest's
  `_put_the_tree_under_test_first_on_child_paths` closes the class once: in
  `pytest_configure` it prepends the directory `kiro_crew` resolves from to
  `os.environ["PYTHONPATH"]`, keeps any inherited entries after it and never writes an
  empty component (an empty entry is the child's CWD on `sys.path`). A child that
  inherits the environment needs nothing of its own; a test that builds a child env from
  scratch, or strips `PYTHONPATH` on purpose, still decides for itself.
- **A version-manager shim picks its tool from the environment the test changed.**
  mise, asdf, nodenv and volta put a shim first on `PATH` that chooses the real binary
  from `HOME`, `$XDG_CONFIG_HOME` and the version files above the CWD. The floor
  redirects `$XDG_CONFIG_HOME` for every test, and many tests run `node` from a
  `tmp_path` CWD, so on a host whose node came from mise 71 tests in 8 files failed with
  `mise ERROR No version is set for shim: node`, and the ones that also substituted
  `HOME` waited out the 120 s timeout. CI's node is a real binary, so CI stayed green. The
  rootdir conftest's `_put_the_real_node_first_on_path` asks node for `process.execPath`
  once, in `pytest_configure`, under the operator's environment, and puts that directory
  first on `PATH`; `npm` and `npx` sit beside it. A test that drives another shimmed tool
  resolves it the same way before it changes `HOME`, `$XDG_CONFIG_HOME` or the CWD.
- **The interpreter decides where recursion gives way.** A test that pinned "decode
  succeeds but encode fails" for a 2,000-deep JSON body met an interpreter that did both;
  assert the invariant across all three outcomes, and walk a deep structure iteratively
  in the assertion itself.
- **A fixed loopback port is the host's, not the test's.** `SshTunnelManager.connect`
  probes the port it allocated, for real, and gives up with "local port N was taken
  while connecting" when it lost a race for it. `TestProxyRequest` handed the manager
  `base_port=53500` and left the probe real, so on a busy macOS runner the remint test
  read `False`: 53500 sits in the ephemeral range, and any outgoing connect on the box
  can be using it. A test about what the manager does once connected pins the probe
  with `_patch_port_probe`; a test about real port ownership binds port 0 and uses
  the port the kernel picked. `test_every_manager_connect_test_pins_the_port_probe`
  fails any test class that connects a manager, and any module-level helper that
  builds one for a file that connects it, unless the probe is pinned outside a single
  `test_` method or `_REAL_PORT_PROBE_CLASSES` names it with the reason the real probe
  is the subject.
- **`http.server` looks the host up between `bind()` and `listen()`.**
  `HTTPServer.server_bind` calls `socket.getfqdn(host)`, a system-resolver call that
  can stall on a macOS runner while the socket is bound but refuses every connect. A
  parent that waits on a deadline for a spawned server to answer then reads a live
  child that never comes up (`runtime stayed {'state': 'starting'}`). A server a test
  spawns and waits for overrides `server_bind` to call `socketserver.TCPServer.server_bind`
  and set `server_name` from the address it already has, as the stand-in in
  `test_decisions_local_runtime_real_spawn.py` and `plumb_cpu.py` do.

## Running the suite: the defaults, and how to narrow safely

The checkpoint run before a commit is the change-related set on both surfaces,
with a bounded worker count -- the full suite is CI's job:

```bash
python3 scripts/local-gate.py
```

A command that already passed on this exact tree is not run again. The gate keeps one
proof per command under `<git-dir>/local-gate-proofs/`, keyed by the tree (HEAD plus
every staged, unstaged and untracked edit; files `.gitignore` hides are outside it), the
argv, the working directory, a digest of the whole command environment and the tools
behind the argv (this interpreter and every package installed in it; for npm and npx also
the node they resolve to and the lockfile npm wrote for `node_modules`), and it reports
`reused, not run` for each command it skips. A command whose tools cannot be named always
runs. A proof lasts an hour, a failure is never kept, and a pass is kept only when the
tree did not move while the command ran. After changing something that no package list or
lockfile names (a built `dist/`, a hand edit under `node_modules`), run
`python3 scripts/local-gate.py --no-reuse`.

The whole suite with the configured defaults is a human's run, not a gate:

```bash
python -m pytest
```

The same suite as many small processes, in about a quarter of the memory (see "Running on
a machine with little RAM"):

```bash
python3 scripts/run_sharded_suite.py -- -m "not ipv6_required and not real_venv"
```

`setup.cfg`'s `[tool:pytest] addopts` supplies `--verbose`,
`--ignore=build/private`, `-p no:platformdirs` (refuses platformdirs' auto-loaded
`pytest11` plugin), `-n auto`, `--dist loadgroup`, `--max-worker-restart=2`,
`--timeout=120`, `--durations=5` and `--color=yes`. Coverage is deliberately NOT in
`addopts`: measured on a 1,231-test subset it cost +21% wall time on every local and
agent run, while CI asks for it explicitly. So a local run needs no override to
avoid coverage. (Coverage's cost is overwhelmingly TIME, not memory: re-measured
across three slices it added +33% to +160% wall clock but only +1.6% to +8.1% peak
worker RSS.)

### Opt-in CI progress records

The Linux (`backend-test`) and Windows (`backend-test-windows`) test jobs in `ci.yml`
load `scripts.ci_pytest_progress` explicitly with `-p` and
`--ci-progress-dir`; importing the plugin without that option registers no recorder
and creates no files. It leaves selection, scheduling, coverage and timeout limits
unchanged. One open JSONL stream per worker records collection start/end and selected
count, test start/end, and pytest's setup/call/teardown durations. File names include
worker, PID and a fresh UUID, so nested runs and repeated in-process runs cannot
replace one another. Each record is flushed, without per-event fsync or path probes.
Source declarations are parsed once per selected module for structural names;
parameter values and dynamic node names are never copied. A selected-collection
ordinal distinguishes cases; unsupported declarations use `dynamic`. No captured
output, exception text, locals or absolute paths are recorded.

The controller emits a bounded `CI_PROGRESS` summary at most once per 30 seconds
of incoming phase reports, with the last phase, last completed test and slowest
phase since the previous summary. Worker-ready/collected and session-end markers
are also logged. These are event-driven, not a heartbeat: a stuck collection or
worker can leave no new summary, and the last summary need not name the test active
at cancellation. JSONL preserves prior events on process termination, but a job
limit can skip artifact upload and a machine loss can lose the files entirely.
The Actions log then retains only the sampled summaries, not a complete trace.
Diagnostic file I/O failures disable that stream without changing the test verdict.
Linux child-process tests verify these mechanics, not native Windows performance.

### Running on a machine with little RAM

**The Linux/Windows budget reserves 5 GiB per worker; the Linux wide-run
measurements below were 1.8–2.8 GiB, and a host OOM later showed ~4.5 GiB workers. macOS reserves 16 GiB after measured
14.9–16.1 GiB workers.** Without the budget, `-n auto` would ask for one worker per
core. In the Linux measurement almost all of the fixed part was *collection*: every
xdist worker collected every testpath — 106,491 items — at ~1,499 MiB of peak RSS
before running a test, 99% of it private. From there a worker grew another ~60 MiB per
1,000 tests, and that growth did not saturate.

Both numbers were remeasured in the
[fourth five-run pass](testing-sweep-findings.md#what-the-fourth-five-run-pass-found-linux-106k-tests-per-run) and both had roughly DOUBLED
under the previous figures (~57,000 items / ~750 MiB / ~25 MiB per 1,000). **Re-derive
them whenever the suite grows by half again**, rather than trusting this table: the
cheap way is a `--collect-only -n0` run, which reproduces a worker's collection peak to
within about a megabyte.

**Those two facts together mean per-worker cost rises as parallelism falls**, because
fewer workers each run more tests. Projected peak is `1,499 + (106,491 / N) × 0.060` MiB:

| workers | tests each | projected peak | measured |
|---|---|---|---|
| 32 | 3,330 | ~1.7 GiB | — |
| 12 | 8,875 | ~2.0 GiB | 1.8 / **2.0** / 2.8 GiB (min/median/max, 60 worker-runs) |
| 8 | 13,300 | ~2.3 GiB | — |
| 2 | 53,250 | ~4.6 GiB | — |
| 1 | 106,491 | ~7.7 GiB | — |

The `-n 12` projection lands within 11 MiB of the measured median, which is what makes
the formula worth quoting at all. The rows below it are EXTRAPOLATIONS no measurement
covers, and they are the rows where the budget actually binds — treat them as a floor on
the answer, not the answer. The measured **max** matters as much as the median and is not
noise: the same worker slot peaked at 2,771 MiB in all five runs, because the
`tree_scan_*` xdist groups land together and one of them alone retains ~1.3 GiB of parsed
source.

That is why `xdist_budget.py` reserves 5 GiB per worker on Linux and Windows,
and 16 GiB on macOS. The 5 GiB value is ~1.1× the ~4.5 GiB anonymous RSS per worker that
the kernel OOM table showed when ~40 workers filled a 184 GiB host with no swap; the
`-n 12` peak above (2,771 MiB) undercounts workers that run long suites. On
Linux/Windows, sizing the divisor on a wide-run number would grant 4 workers on an
8 GiB laptop, whose ~26,600 tests each would then want ~12 GiB between them and swap
the machine. On macOS, the 16 GiB value prevents four workers measured at roughly
62 GiB total from being granted on a 36 GiB host. **Do not lower either divisor on the
strength of a high-parallelism or cross-platform measurement.**

Where the floor goes, measured by ablation on one worker (a `--collect-only -n0` run
reproduces a real worker's peak to within about a megabyte, which is the cheap way to
re-measure the TOTAL — it read 1,499 MiB in 195 seconds on the pass that last checked):

**The per-layer split below is the earlier ~747 MiB ablation and has NOT been
re-derived since the floor doubled.** Only the total has. Two of the three layers scale
with the item and module counts, so the shares are still the right places to look —
pytest's item tree at ~6 KiB per item alone projects to ~625 MiB at 106,491 items — but
do not quote a layer's absolute number as current. Re-ablate before optimizing one.

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

**Re-measured at 283,605 items, the fixture bookkeeping had become the biggest
layer.** A `--collect-only -n0` worker peaked at 5,295 MiB, and `tracemalloc` put
2,971 MiB of it in pytest's per-item fixture structures: each item resolves ~130
fixtures, and pytest kept a fresh `(FixtureDef,)` tuple per name per test function
(1,084 MiB), a `name2fixturedefs` dict per function (577 MiB), and a copy of that
dict in every item's `TopRequest` (884 MiB). Almost all of them are identical. The
rootdir conftest's `pytest_itemcollected` hook (`compact_collected_item`) now shares
the identical tuples and dicts and drops each item's request until pytest rebuilds
it at setup, which took the collection peak to 2,957 MiB. It runs per item so the
freed copies are reused while collection is still allocating; a hook at collection
finish would leave the peak where it was. `test/test_worker_memory_retention.py`
pins it, including that every item but the running one holds no request.

The hook bounds what each autouse fixture costs; it does not make one free. It also
shares identical `names_closure` lists (each item's `fixturenames`) between tests that
are not asyncio tests: pytest-asyncio inserts `event_loop` into an `asyncio` test's
list at setup, so those keep their own. Every autouse name still adds a setup and a
teardown to every test. Merge a new guard into an existing autouse fixture of the same
scope before adding a name. Measured on 2,000 no-op tests (`-n0 --no-cov`, three runs
each side on one host): running 87 of `test/conftest.py`'s guards behind five names
took setup plus teardown from 9.8-10.6 ms to 7.1-7.6 ms per test, with the guards'
call order unchanged.

**The per-item cost is budgeted.** The rootdir conftest records the process RSS after
configure and the VmHWM at collection finish, and
`test_worker_memory_retention.py::TestCollectionCostPerItem` fails when collection
spends more than 600 MiB plus 16 KiB per collected item (measured ~11 KiB per item on
the full Linux suite; ~19 KiB before the fixture bookkeeping was shared). It is a
per-item budget, so adding tests never trips it; it runs wherever a process collects at
least 5,000 items, which includes every CI file shard.

The shapes that cost the most, each with a wrong and a right example and its measured
saving, are in [Writing a test that is cheap on memory](#writing-a-test-that-is-cheap-on-memory).

Every layer that remains is live: the item tree, the closures and the rewritten
modules are retained for the whole session by design.

So the full suite genuinely needs multiple gigabytes. On an 8–16 GiB Linux or Windows
laptop with a browser open it may not fit; on macOS the 16 GiB reservation often clamps
a run to one worker even on larger hosts. The budget in the rootdir conftest works this
out and clamps `-n auto`, naming the active platform reservation. A Linux/Windows example:

```
xdist worker budget: 1 of 10 workers (5.0 GiB free, 16 GiB installed). Each worker
needs about 5 GiB, mostly to collect the suite. A run this narrow is slow, not
stuck -- free some memory, or run a subset (pytest test/test_thing.py).
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
| `-n <N>` on the command line | Skips the memory readings, but still claims N slots from the same host pool (rootdir `pytest_cmdline_main`). Fewer than N free: clamped to what it got, floor 1, with a warning. |
| `KIROCREW_TEST_SLOTS_BYPASS=1` | Lets an explicit `-n <N>` skip the host pool too. Only for a host you own outright. |
| `--maxprocesses=<N>` | Clamps *after* the budget, so it can only tighten. |
| `KIROCREW_MAX_TEST_WORKERS` | Per-run ceiling, default 32. |
| `PYTEST_XDIST_AUTO_NUM_WORKERS` | xdist's own ceiling. Honoured here, because this hook replaces xdist's default implementation. Kiro Crew seeds it with a memory-aware cap at every agent spawn boundary. |
| `KIROCREW_TEST_SLOT_DIR` | Where the slot locks live. Point it at a throwaway dir to measure without contending with another run. |

If the suite is slow on your machine, the answer is usually not a bigger `-n`: run
the slice you are working on. A full-suite checkpoint is what CI is for.

**Narrow by FILE, not by `--splits`.** `--splits/--group` — pytest-split — deselects
*after* the session has collected everything, so a 1-of-4
item shard still pays the whole floor in every worker while running a quarter of
the tests. Measured: 14,237 of 56,946 items selected, 744 MiB peak, which is the
unsharded floor. CI uses `scripts.ci_file_shards` on every platform to
assign whole files before import; each worker collects only its shard's files.
For local work, pass the specific files relevant to the change.

What the floor actually tracks is the FILES a process is given. Measured on one
worker: 1,540 files → ~745 MiB, 770 → 477, 385 → 332, 193 → 226–252. So at equal
parallelism the aggregate is what changes, and summing the peaks of every process
says so: eight xdist workers each collecting all 1,540 files come to 5,945 MiB, while
eight single-worker processes given 193 files each — the same 56,946 items collected
once between them, and the same eight-way execution — come to 1,896 MiB, a 68% cut on
the machine as a whole.

`scripts/run_sharded_suite.py` is that runner for the whole suite. It starts `--shards`
single-process pytest runs, at most `--jobs` at a time, each loading
`scripts.ci_file_shards` so it imports only its own files. Two things make it a runner
rather than a one-liner. Sharding at the collect report, not by naming files, keeps
pytest's own `collect_ignore` rules. And every file that names the same literal
`xdist_group` (`subprocess_spawn`, `mcp_gateway`, `serial`) is pinned to one part, so those
tests still run one at a time. A group name built at runtime (the `tree_scan_*` names in
`test_facade_shared_checks.py` and `test_bindings_shared_checks.py`) is not seen; those
groups only share a memoised scan, so a split costs memory, not correctness. Measured on one r7i.8xlarge (32 cores, 247 GiB), same tree, the
baseline `-m "not ipv6_required and not real_venv" --timeout=120 -p no:cacheprovider`
selection:

| command | peak used | wall time | failures |
|---|---|---|---|
| `pytest -n 32` | 120.1 GiB | 50 min | 1 (red on main) |
| `scripts/run_sharded_suite.py` (32 parts, 32 at a time) | 33.6 GiB | 18 min | the same 1 |
| `scripts/run_sharded_suite.py --jobs 8 --shards 32`, on 8 cores, before group pinning | 11.6 GiB | 36 min | the same 1, and 2 timing tests that passed when their parts ran again |

In the first two rows the same 284,758 tests ran with the same outcomes. Each part writes
`junit-<i>.xml` and `shard-<i>.log` under `--out`, and the exit status is 0 only when
every part passed.

### Writing a test that is cheap on memory

Every xdist worker imports every test module it collects and keeps it for the whole
run, so a byte a test module holds is paid once per worker. A test that pins its own
state after it ends is paid until something replaces it. Each rule below was measured
on one r7i.8xlarge with the same command before and after the fix, and the fix passed
the repository's boot smoke (`test/integration/test_boot_smoke.py`).

**1. Build a large payload inside the test, never at import.** A module-level or
`parametrize` value is built when the module is imported and kept alive by the module
or by the mark, in every worker.

```python
# Wrong: 30 MiB in every worker for the whole run
_HOSTILE = {"entry_too_large": [(_entry("big.bin"), bytes(range(256)) * (26 * 4096))]}

# Right: built by the test that reads it
_HOSTILE = {"entry_too_large": [(_entry("big.bin"), lambda: bytes(range(256)) * (26 * 4096))]}
```

Measured: five such modules held 80 MiB per worker (`--collect-only` of those modules,
286,804 -> 206,284 KiB). A corpus several tests share goes in a `scope="module"`
fixture, which pytest frees at the module's teardown.

**2. Do not keep a whole-tree read in a module-level memo.** pytest keeps every test
module for the session, so an `lru_cache` of every test file's text in a test module
lives for the rest of the worker.

```python
# Wrong: the test tree's text, held by the module for the whole run
@functools.lru_cache(maxsize=1)
def _family_test_texts():
    return tuple((p, p.read_text()) for p in _test_tree_files())

# Right: the same memo, in a module whose tests carry a tree_scan xdist group
pytestmark = pytest.mark.xdist_group(name="tree_scan_<module>")
```

`test/conftest.py` clears every module-level `lru_cache` of a `tree_scan_*` group's
modules when the worker leaves the group. Measured: 361 -> 51 MiB retained after
`test_facade_mirror_guards.py` (`-n0`, tracemalloc after `gc.collect()`). Outside a
`tree_scan` group, stream the read or clear the memo from a module-scoped fixture.

**3. Never leave a closure over a test's state in a module global.** A setter such as
`set_share_tree_reader(lambda key: ...)` keeps the state, and every `MagicMock` call it
recorded, until something overwrites the global.

```python
# Wrong: the 2,000-slot state and its mocks live until the next test builds a state
set_share_tree_reader(lambda key: _share_tree_root(self, key))

# Right (test side): put back what the test inherited
monkeypatch.setattr(sharing, "_tree_root_reader", sharing._tree_root_reader)
```

A global that production wires once at boot belongs in the floor's
`_BOOT_WIRED_GLOBALS`, which restores it after every test. Measured: 319 -> 18 MiB
retained after `test_slots_pass_work.py`, and 195 -> 0 MiB added by
`test_slots_diff_budget.py`.

**4. Do not add an autouse fixture for what an existing one can do.** Each autouse
name is resolved for every collected item (~280k) and set up and torn down for every
test.

```python
# Wrong: a new name every item pays for
@pytest.fixture(autouse=True)
def _reset_my_cache():
    yield
    my_module._cache.clear()

# Right: add the reset to an existing autouse fixture of the same scope. In the
# test/ suites that is a guard, which the `_*_guards` fixtures already run:
@per_test_guard
def _reset_my_cache():
    yield
    my_module._cache.clear()
```

Measured: before the rootdir conftest shared the per-item fixture bookkeeping, the
~130 fixtures each item resolves cost 2,971 MiB of a 5,295 MiB worker's collection
(5,295 -> 2,957 MiB once shared). Sharing the `names_closure` lists as well took a full
collection from 3,248 to 3,126 MiB. `TestCollectionCostPerItem` budgets what is left
per item.

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
A worker can also finish, without crashing, while still assigned work, after a
shutdown command or its channel closing: xdist then fails `assert not crashitem`
on an item that never started. The banner names that worker, that item and how
many selected items the scheduler still queued, and says to re-run the shard;
that worker's `session_end` progress record carries `channel_closed` to tell the
two causes apart (other workers' channels close at teardown, so only its own
record counts).

So any override that still runs MANY tests must carry
`-n auto --dist loadgroup --max-worker-restart=2`, and keep `-p no:platformdirs`
from `setup.cfg`'s `addopts`:

```bash
python -m pytest --testmon \
  --override-ini="addopts=-v --ignore=build/private -p no:platformdirs -n auto --dist loadgroup --max-worker-restart=2 --durations=5 --color=yes" \
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
| Checkpoint before committing | `scripts/check_black_formatting.py && scripts/check_subprocess_encoding.py && isort && flake8 && mypy && python3 scripts/local-gate.py` (related tests; the full suite is CI's) |

## Determinism: the seven flake classes

A test that fails on CI but not locally is almost always one of these. Each has one
correct fix; reruns and `sleep` increases are not among them. The
[contract](#determinism-contract-read-this-first) at the top of this document is the
short form of what follows.

### 1. Nondeterministic input

Feeding `os.urandom` / `random` / `uuid4` into an assertion that depends on a property
the RNG does not guarantee. A random opaque id is fine; a random *payload* asserted to
NOT match a pattern is a coin flip.

Fix: seed it. `random.Random(_SEED).randbytes(n)` keeps the payload high-entropy,
which is usually the property under test, while fixing the outcome. Verify the chosen
seed against the real predicate, and say in a comment that you did.

**A random opaque id is fine until a substring check reads it.** An absence check
such as `assert fragment not in json.dumps(rows)` also scans every random id in the
rows, so a short fragment turns up in one by chance. A reveal handle is `rv_` and 22
random url-safe characters: a four-character half of a credential is inside about one
handle in 880,000, and that is how
`test_chat_runner_redact_across_tool_boundary.py` reported a leaked key half that was
only the handle (`rv_OEV1o4bO0kJp8nQh4TRSFA` holds `Qh4T`). Fix: before the check,
replace every match of the id's exact shape with a fixed stand-in, as that file's
`_queued_text` does (`re.sub(r"rv_[A-Za-z0-9_-]{22}", "rv_<handle>", json.dumps(rows))`).
Everything outside those ids stays checked. Do not shorten the scan to `content` only,
and do not lengthen the fragment: the half is the scenario. Prove it by pinning the id
source to a value holding the fragment
(`monkeypatch.setattr(reveal_store, "new_handle", lambda: handle)`), and assert that
handle reaches the payload, so the test fails if the id stops being sent.

**The host is an input too, and a PID is the one that catches people.** `999999` is
not an impossible PID: Linux `pid_max` is 4194304, so on a long-running host it names
an ordinary live process. Two tests asserted its absence — one as "a dead gateway
whose entry must be pruned", one as "a value only a planted `ps` shim could have
produced" — and both went red on a host whose counter had passed it, the second while
accusing the shim of running when it had not. Fix by kind: for a PID the code *probes*,
pin the probe (`patch(..., "pid_exists", side_effect=lambda p: p != 999999)`); for a
PID that must never appear in real output, use a number no OS can allocate
(`99999999999`) rather than one that merely looks unused.

Synthetic process trees must also give the owner a synthetic PID. Mixing
`os.getpid()` with fixed child PIDs can overwrite the owner's namespace when a
container assigns the worker one of those child PIDs. Prefer an injected pid (or a
local proxy as the tested module's `os` binding); otherwise patch `os.getpid` only for
the span of the call, through `monkeypatch`.

```python
# WRONG: ~1% of runs match a credential prefix and the exemption assert fails
body = os.urandom(20_000)
# RIGHT: same entropy, same code path, one outcome
body = random.Random(20260803).randbytes(20_000)
```

**The host's zone, locale and hash seed are inputs too.** Nothing pins `TZ` or the
locale for the backend suite, so a naive `datetime.now()`, `date.today()` or `time.localtime()`
in an expectation reads the runner's zone, and a test that passes in UTC fails where
local midnight falls between two of its reads. Read the zone the product stamps, with
the instant frozen: a mirror of a product defined in local time (a ZIP entry's
`date_time`) reads `time.localtime(<fixed instant>)`, never the live clock. A test about
local time sets its zone itself and runs under `UTC`, `Pacific/Kiritimati` (+14:00, a
calendar day ahead of UTC for 14 hours of every day) and `America/St_Johns` (-03:30, a
half-hour offset). Inject the zone where the product takes one. Otherwise request the
rootdir `local_tz` fixture and call `local_tz("Pacific/Kiritimati")`: it sets `TZ`,
calls `time.tzset()`, and at teardown puts the variable back and calls `time.tzset()`
again; `time.tzset` exists only on POSIX, so the test skips elsewhere. Never restore it
with a bare `monkeypatch.undo()` in the test body. The per-interpreter `str` hash seed
decides set iteration order, which is [class 7](#7-data-order-and-timestamp-ties).

**A property test is random input by design.** Locally, Hypothesis's `default` profile
(`test/conftest.py`: `max_examples=20`) draws new examples on every run. Under `CI` the
suite loads the `ci` profile instead: the same settings derandomized, so each test
replays one fixed example sequence and a `@given` test cannot red a run whose diff never
touched it, with no example database and the `@reproduce_failure` blob printed.
Exploration happens locally (or under `HYPOTHESIS_PROFILE=thorough`). Reproduce a red
from the falsifying example or the blob, pin that counterexample with `@example`, and fix
the property; never answer it with a skip.

**The kernel's name for a hardlinked inode is an input too.** An inode with
`st_nlink > 1` has several names, and macOS `F_GETPATH` returns whichever one the name
cache holds, not the one the file was opened by: 1 read in 100 on an idle laptop named the
sibling link, and 34 of 2000 admitted hardlink reads were refused by the old name
comparison in `hooks._opened_file_matches_validated_path`. That was a product race, not
a test one: a test that reads a hardlinked file through a path-identity check must not
treat "the kernel named the other link" as a swap. The check now proves identity with
the pinned witness walk (`_hardlink_alias_matches`) for a multi-link inode only. To test
it, pin the kernel's answer (`monkeypatch.setattr(hooks, "_fd_real_path", ...)`) rather
than hoping the cache picks the sibling.

**Host MEMORY is the other one, and it fails with a misleading exception.**
`SubagentManager.spawn` queues — returning before it registers anything in
`_tasks` — while the machine looks short of memory: the absolute floor
(`check_memory_available` against `agent.spawn_min_memory_gb`). What makes it
expensive to diagnose is that a queued spawn IS a `SubagentInfo`, so
`assert info is not None` still passes and the test dies on the NEXT line, at
`await mgr._tasks[info.id]`, with a bare `KeyError` naming an id nothing else
mentions. Measured on a CI runner with
~0.5 GB free.

Fix: pin the reading with `healthy_host_memory` (`test/conftest.py`), which any
file driving `spawn` opts into at module scope:

```python
pytestmark = pytest.mark.usefixtures("healthy_host_memory")
```

It pins only the HOST reading — a caller that names its own `path` is feeding the
`/proc/meminfo` parser a fixture file rather than asking about this machine, so
those tests still run the real function and a parser regression still goes red. A
test that is actually ABOUT the guard patches it in its own body, which lands on
top of the fixture and reverts to it.

The pinned host is healthy, not infinite: it reads 8 GB free and still compares
the floor it is asked about against that figure, `(8.0 >= min_gb, 8.0)`, so a
test whose wave of unsettled starts outgrows an 8 GB host is queued here exactly
as it would be on one. Size such a test's concurrency to fit the pinned host (the
floor plus one dedicated start per unsettled row), or patch the reading in its
body; never widen the pin back to an unconditional admit. `overload_fakes`'s
`ManagerHarness` pins the same 8 GB host the same way, so the synchronous gate
(which reads the boolean) and the off-loop one (which compares the figure against
its own bar) give a harness test the same answer.

Opt-in rather than autouse, because the pin is not free of consequence: the tests
that drive the probe with no `path` and stub `safe_read_file` underneath it —
`test_subagent_coverage.py::TestCheckMemoryAvailable` — never reach their own stub
once the reading is pinned. `test_subagent_spawn_host_pin.py` is what keeps opt-in
from decaying into "whoever remembered": a module that names `SubagentManager` and
calls `.spawn(` must be pinned or excluded with a reason, so the next spawning test
file cannot land unpinned.

Being pinned is not sufficient, which is why that file carries a **second** ratchet:
a bare `monkeypatch.undo()` in a pinned module's test body also reverts the
fixture's pin, because pytest hands the test function and every fixture it
requests the SAME `monkeypatch` instance. Everything after that line reads the
runner's real free memory — the file is pinned, reads as pinned, and is not pinned
where it matters. Measured on a macos-15 nightly backend shard reading 2.58 GB
available, under the 4.5 GB floor then in force: `test_taskq_admission_integration.py`'s
post-pressure drain deferred the row a second time and failed as
`assert 'queued' == 'starting'` — nothing in the traceback named memory, and the
whole nightly publish chain skipped behind it. Scope the patches a test wants
reverted with `with monkeypatch.context() as scoped:` instead, so leaving the block
restores the fixture's readings rather than the host's.

The memory POSTURE has its own pin. `resource_status._read_available_gb` is the
live reading behind the posture that `admission_check` and `prewarm_allowance`
act on; while it reads critical the cron due-scan defers interval and one-shot
firings and the chat runner holds no eager pre-warm session, so a test that fires
a cron job or an eager spawn fails on a runner with 2 GB or less free as a bare
`KeyError` on `svc._claims[job_id]`, a wait that times out, or `get_or_create`
awaited 0 times. Such a module opts into `ample_host_free_memory`
(`test/conftest.py`) the same way:

```python
pytestmark = pytest.mark.usefixtures("ample_host_free_memory")
```

It pins the reading rather than `probe`, because `prewarm_allowance` reads it
without going through `probe`, and it pins nothing below it: a test about the
slice, cgroup or host readers must not opt in, and a test about the posture
patches the reader in its own body, on top of the fixture.

### 2. Wall-clock races

Asserting a *rate* or a *count* that the host controls. On Windows through Python 3.12
the clocks step ~15.6 ms, and so do `Event.wait`, lock and queue timeouts and the
proactor loop's waits; only `time.sleep` has been high-resolution since 3.11. A loaded
runner also starves threads, so "burn 0.25s at a 2ms interval, expect ~125 samples"
observed **one** sample in CI.

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

**A request budget the test spends in real seconds measures the runner.** A 3 s recall
deadline with a 1 s `time.sleep` "burn" left the judge's wait at the mercy of every
cold step before it (store, pool, embedding): on loaded Windows runners the remaining
wait came out negative or the route answered `504 memory_recall_timeout`. Replace the
module's own `time` name with a frozen clock the test advances, assert the computed
wait exactly, and give the real deadline a generous backstop (`ManualClock.install`;
`_PointClock` in `test_decisions_memory_recall_reachable.py` is the hand-rolled form). To
make a deadline pass, move it to the real present rather than sleeping past it.

Where a test wants a timeout to expire at a particular AWAIT -- the cancellation must
land inside the steer RPC, after authorization, not in the gate work in front of it --
neither `0` nor a small value is correct: both let real work ahead of the hang consume
the bound, and on a loaded Windows runner a 0.05 s allowance was spent before the hang
was reached, so the case read the pre-hand-over arm (`assert 0 == 1` on the missing
audit row). Make the allowance a large backstop and fire the bound's own
`asyncio.Timeout` from the hang as it parks, so the product's real `wait_for` path
runs and only its moment is chosen. `_bound_expires_at_the_hang` in
`test/test_session_broadcast.py` is the helper.

**A count taken across a wall-clock cache measures the runner too.** The path gate's
target cache expires after a 0.1s floor, and each expiry is a rebuild that resolves the
root anchors once more. `test_the_fence_resolves_its_anchors_per_directory_not_per_entry`
counted those resolutions for a 4-entry and a 400-entry walk; a slow Windows runner took
longer than the floor to list 400 entries, the cache expired mid-walk, and the count came
out `4 == 5`. When a test counts calls behind a TTL, hold the expiry open for the
measurement (patch the module's own TTL function) so the count is the walk's shape, not
its duration. Reproduce by advancing a fake clock per entry processed: the bigger walk
crosses the floor on any host.

**A pin whose precondition is "the loop woke before a timer's `when`" stands the loop
clock still.** Widening `loop._clock_resolution` makes asyncio pop a handle early only
when some wake lands before its `when`, and a starved Windows runner's 10 ms poll can
return after a 50 ms timer is already due, so the precondition assertion reds with the
defect never reached (`test_the_ramp_is_woken_when_the_pump_timer_fires_inside_the_clock_resolution`).
Patch `loop.time` to a constant inside `monkeypatch.context()` for the wait, so every
wake reads the arm instant and asyncio's own pop rule fires the handle early on every
host; restore it before teardown awaits a real timeout. Compare against the frozen
`now` with the production expression itself (`now < when <= now + delay`), since
`when - now` can round past `delay`.

**An append-only log with two writers has no "last row".** The decision gate stops
waiting for its own call row after a 50 ms write budget and lets the append finish in
the background, so on a slow Windows runner the gate's row for a turn commits after the
outcome row the runner awaited, and `_rows(tmp_path)[-1]` reads the wrong row
(`KeyError: 'model_chosen'`). Select the row by a field only it carries, assert how many
there are, and pin the reversed order with events: hold the early writer until the late
one has committed (`test_the_outcome_row_is_read_when_the_call_row_lands_after_it`).

**`wait_for` on a subagent run cancels it, and the run can swallow the cancel.** A run
cancelled before its first tool call takes the one-shot auto-continue branch: it returns
normally with neither `done` nor `error` set, so `asyncio.wait_for(manager._tasks[id])`
returns as if the run had finished and the next assertion reads state that was never
written (`call_args` is `None`). Wait with `asyncio.wait({task}, timeout=...)`, which never
cancels, then assert `info.done and not info._cancel_retry_used` with `_stop_reason(info)`.

Two snapshots from different kernel accounting sources are this class too. Compare them
with a bounded, measured slack, and keep allocation-growth observations in the failure
message because a long-lived allocator may serve a probe from resident memory. Set the
allowance above observed counter drift but far below unit/scale errors or a real
multi-megabyte inversion.

The commonest shape here is not a rate but **an unawaited task**: a handler that
answers before its work finishes leaves the assertion racing the loop. There is a
synchronisation point, so use it — `drain_background_tasks(state)` — and see the Rules
entry for what it looks like when you do not (a different test failing each run).

A route with a **wait budget** is the same shape from the other side: it awaits its
work for a fixed interval, then answers a placeholder (`refreshing: True`) while the
work continues. `/api/usage/kiro` gives its session scan 50ms, and a real scan on a
loaded Windows runner overruns it, so a test that asserts the settled payload on the
first response reads the zero placeholder (`assert 0 == 1`, a missing `error` key).
A test about the settled payload waits on the task the route left running and asks
again (`_get_settled_usage` in `test_usage.py`); a test about the placeholder sets the
budget to `0` and holds the work. Reproduce with the budget at `0`: every test that
races it fails on any host.

An unawaited executor job holding a `file_lock` is the sharpest version, because the
acquire on the event-loop thread is one attempt and never waits (the
`platform_compat.file_lock` docstring). `await svc.remove(a)` leaves its trust
revocation running on an executor thread, so a sync `svc.remove_sync(b)` on the next
line, still on the loop, fails closed (`test_autonudge_refactor_contract`, 7 CI runs
in one day). Prefer the async mutator, `await svc.remove(b)`. A test whose subject IS
the sync mutator calls it off the loop (`await asyncio.to_thread(...)`) only when
nothing else on the service is in flight: no armed timer, no observer, no pending
persist, since those all belong to the loop. The mirror image is a holder on a fixed
timer: it races the waiter's own start (`test_posix_lock_ceiling` saw a 4e-05s
"wait"). Release it when the waiter's own refused attempt on that lock file is seen,
and release it inside the coroutine, because `asyncio.run` joins its executor before
an outer `finally` runs and a waiter cannot finish while the lock is held.

A **crew log** has two holders the test does not own: the writer thread that lands
every append, and the eager folder that reads the unit on its own thread right after
each entry. Any crew-log call made straight from an `async def` -- a write
(`commit_work_progress`), an open or read (`open_session_log`, `iter_from`,
`session_ledger.read_state`) -- is refused whenever one of them holds the unit lock.
A refused write raises `CrewLedgerNotRecorded`, a refused read raises `OSError`, and a
best-effort reader quietly answers the empty record (`assert '' == 'fold me'`). It
reached unrelated PRs on Windows and Linux shards through sync helpers that cannot see
their caller is async (`_item`, `_work`, `_seed_item` in the issue-radar tests) and
through direct reads in `test_discord_mirroring.py`, `test_session_ledger.py` and
`test_work_ledger_projection.py`; 0 in 400 local runs, so only a held-lock probe
reproduces it. It came back through a read the pin did not list: `_folded()` in
`test_spawn_approval_crew_log.py` opened the log with `CrewLog.open` and read it with
`iter_from` from async tests, and failed three unrelated PRs on Windows and Linux the
day it merged. The pin's call list must name every crew-log entry point, not only the
ones a past failure used. Make the call the way product handlers do, off the loop:
`off_loop(fn, ...)` from `test/off_loop_helpers.py` inside a helper both kinds of test
call, or `await asyncio.to_thread(...)`. `test_crew_log_off_loop_pin.py` fails any
`async def` that makes such a call directly or through a same-module sync helper, and
`TestSeedingSurvivesABriefLogLock` proves the hop waits out a held lock while the
on-loop call is refused. Do not fix it by patching `_on_event_loop`: that hides the
on-loop hazard from the product code under test too.

A **negative control that calls the crew log on the loop on purpose** meets those same
two holders, left behind by its own setup write, before it reaches the holder it
planted: the on-loop write's preparing fold is refused with a bare `OSError` instead of
the `CrewLedgerNotRecorded` the control expects. Settle the setup first --
`emit.flush()` for the writer, then `eager.drain()` for the folder -- so the planted
holder is the only one the deliberate on-loop call can meet.
`TestSeedingSurvivesABriefLogLock.test_the_held_lock_is_real_contention` does this.

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
  assertion passing vacuously because a stamp aged out. It pins the stdlib `time.time`
  for the whole worker, though, so a new fixture of this kind freezes the
  module-under-test's own `time` binding instead (D2). Reading the clock inside the
  test instead is the weaker fix — it shrinks the gap to microseconds without closing it.
- **Same instant, two readers.** `datetime.now(timezone.utc).timestamp()` is rounded to
  the microsecond; `time.time()` is not. Read in that order, the earlier `datetime`
  value is larger in about 1 of 80 back-to-back pairs on Windows, by up to 0.24 us.
  `test_a_future_ts_is_clamped_to_the_present` bounded the fold's ceiling (`datetime`)
  by a later `time.time()` and failed by one float step. A bound on a value the product
  computed from the clock must be read through the product's own clock call.

More shapes this class hides, all Windows-only and all green on every Linux run:

- **A state written in two phases across a thread boundary.** Waiting on ONE half is
  not waiting on the state. A dependency park registers its waiter on the store's
  writer thread and yields the lane slot in the continuation the thread's wake
  schedules, so a barrier that stops at `len(coordinator.waiters(scope)) == 2` samples
  `_running_count` mid-park: microseconds wide where a cross-thread wake is a self-pipe
  write, tens of milliseconds where the loop has to return from an IOCP wait, and
  `assert 1 == 0` when it loses. Wait on the CONJUNCTION the assertions then read
  (`test_runloop_integration._await_parked`: waiters, slot count and row state
  together) with a generous ceiling, never on the first half to become true. That
  ceiling is a lost-run guard, so reaching it RAISES with the conjunction it last read:
  a barrier that returns anyway hands its caller a state nobody asked about, and the
  run then fails as whichever later assertion happens to touch it first — a park that
  never happened reported as `assert [] == ['provider:acp']` three lines on.
- **A silent bounded wait reports a THROUGHPUT shortfall as an ordering defect.**
  `test_subagent_scale_queue.TestDurableQueueScale::test_queue_survives_manager_loss_and_drains_fifo`
  drained 199 recovered queue rows under `while store.count(DONE) < 199 and
  time.monotonic() < deadline`, then asserted `started == ids[1:]`. On the Windows
  shard the deadline expired mid-drain, the loop exited silently, and the run failed
  as `AssertionError: Right contains 34 more items` — an ORDER assertion, on a list
  whose 165 entries were in perfect FIFO order. Reproduced on Linux by shrinking the
  deadline alone. The defect is the silent exit, not the constant: the completion of
  the drain is its own assertion, so the wait raises naming the shortfall (`drain
  unfinished after 0.1s: 33 of 199 rows started, 33 DONE, 64 still in the window,
  running_count=3`) and the order assertion runs only on a complete drain. Two rules
  this shape teaches. **Size the ceiling from a measurement and say which one:** 199
  rows is a per-row cost, not a race — 0.58-0.60 s idle on Linux and 0.86 s worst
  under eight-way local contention (~3-4 ms/row) against ~180 ms/row on the shard
  that failed, so the ceiling is the measured worst case x 175 (150 s) with the
  derivation in the comment, and only a wedged queue ever spends it. **Do not poll a
  sqlite count on the event loop:** each `store.count()` in the hot loop takes the
  store's connection ON the loop (`on_loop_db` warns for exactly this) and a read
  contended with the writer thread blocks the loop for the connection's whole busy
  timeout — the poll slows the drain it is measuring, so gate the DB read behind the
  in-memory half of the conjunction. The same silent shape sat in that file's shared
  `_settle(predicate)` helper across 19 call sites; with the ceiling forced to 0 s the
  raising version fails 14 tests naming what never settled while the silent version
  fails 11 and passes 3 VACUOUSLY — including one whose `assert secret not in body`
  is trivially true when no digest was ever built.
- **A timer asyncio runs BEFORE its own `when`.** `BaseEventLoop._run_once` runs every
  handle within `loop._clock_resolution` of now, and that resolution IS the `monotonic()`
  tick above: 15.625 ms on Windows against ~1 ns on Linux. So a callback there reads
  `loop.time() < handle.when()` for the very handle it was armed as, and code that
  re-arms a one-shot from inside its own callback while skipping the arm whenever some
  handle still looks future-dated arms nothing at all — once per rung on Windows, never
  on Linux. Emulating it locally takes ONE property: `_clock_resolution` set per LOOP
  INSTANCE, because `BaseEventLoop.__init__` writes its own from
  `time.get_clock_info('monotonic').resolution` and a class-level value is never read —
  an unpatched loop reads `1e-09` however coarse the module clock is made. Flooring
  `BaseEventLoop.time` to the same tick as well reproduces the shard's own SYMPTOM — the
  park barrier's 20 s gather timing out — in 3 of 24 whole-file runs with the defect in
  memory, where the pin named next fails on all 24; neither `time.time()` nor the
  module-level `time.monotonic()` has to move for either.
  `test_runloop_integration.test_the_ramp_is_woken_when_the_pump_timer_fires_inside_the_clock_resolution`
  pins the invariant from the resolution alone, with no fake clock. Such a pin also needs
  a poll SHORTER than the resolution, and that makes a sleep length load-bearing where
  this file otherwise says to wait on a signal: `_run_once` pops a handle early only
  while the loop is AWAKE inside `(when - resolution, when)`, so a poll longer than that
  window leaves the loop asleep until the timer is overdue, no early fire happens, and
  the pin goes green having exercised nothing. Set the resolution COARSER than the delay
  under test (4 ticks against a 0.05 s arm) so the window is the whole wait instead of
  its last tick — at Windows' own 15.625 ms the pop is a lottery on when the loop
  happens to wake, and 1 of 15 runs starved on one busy core never saw it, which is a
  flake rather than a defect. Then ASSERT the precondition instead of trusting whoever
  reads the test next to leave the poll alone — and assert the precondition the DEFECT
  needs, not merely that a spent future-dated handle was seen somewhere: the pass must
  have had a deadline to arm, and the margin must fall inside the delay that pass wanted
  (the dedup's own `now < when <= now + delay`). A spent handle read on a final
  `deadline is None` pass, or one further out than the pass would have armed, strands
  nothing, so counting it certifies a precondition the defect never needed and the pin
  is green again for the wrong reason.
- **`time.monotonic()` has a ~15.6 ms tick on Windows through 3.12** (GetTickCount64;
  QueryPerformanceCounter only from 3.13). Two reads inside one tick return the SAME
  float, so a duration synthesized as `t0 = monotonic() - 0.2` and measured against a
  second read is exactly 0.2 s round-tripped through a float subtraction — 199.999… at
  some machine uptimes, which a `>= 200` assertion reads as a failure while the code is
  correct. Bound such a sample instead of pinning it on the boundary: a floor an order
  of magnitude below (which still fails a seconds-for-milliseconds bug) and, as the
  ceiling, a span the test measures itself. `time.time()` steps the same ~15.6 ms there
  (GetSystemTimeAsFileTime; precise from 3.13), and asyncio's `loop.time()` IS
  `time.monotonic()`, so the gap between two timers quantizes too. Two wall-clock stamps
  taken back to back are a tie, not an order: see
  [class 7](#7-data-order-and-timestamp-ties).
- **A console child brings a console host.** A child started with `CREATE_NO_WINDOW`
  gets its own `conhost.exe`, which Toolhelp lists as that child's child, so a
  process-tree discovery from it can return a member the test never spawned. Whether
  that console host is still alive at the scan is host timing: gone on an idle
  machine, present on a loaded shard, which is how `assert {5436, 7788} == {5436}`
  reached unrelated PRs from a drain test. Assert the tree's own invariant (every
  member the drain retained reached a terminal scan), never an exact member set, and
  pin the populated case deterministically: a helper that calls `AttachConsole` on
  the child keeps its console host alive past the child's exit
  (`test_runtime_cleanup_windows.py::test_a_child_exiting_259_reads_as_exited_and_its_drain_finishes[True]`).
  Start that helper only once the child has said it is running: `AttachConsole` on a
  child still initialising fails with `ERROR_INVALID_HANDLE` (6), and process creation
  returns before the child's console connection exists, so a helper started straight
  after creation fails as `helper did not attach`.
- **A fixed drain ceiling over a batch of fsync-priced writes is a rate assertion.**
  Every test in `test_crew_log_edge_concurrency` hands the session log's single writer
  thread 30 to 160 appends, and `assert emit.flush(timeout=10.0)` across that batch
  bounds a write RATE rather than the emitter. One append is an `fsync` behind a
  cross-process lock:
  0.4 ms measured on a warm Linux host, over 100 ms on the Windows shard that failed, so
  one constant covers the work on one host and not on the other.
  `test_many_producers_one_session_all_entries_land` ran 5.9 s green on `main` and
  16.1 s red one head later on the SAME shard, where the 943 tests common to both runs
  came in 2.2x slower end to end — the runner, not the diff. Reproduced on Linux by
  pricing `CrewLog.append` at 100 ms and changing nothing else. The give-up condition has
  to be a writer that STOPPED rather than one that is slow: poll `flush` in windows and
  fail when a whole window lands nothing new, measuring the first window from BEFORE the
  first wait so a real wedge is still reported one window in, and cap the total at half
  the module's `--timeout` so a trickle fails as a readable assertion instead of a
  [class 6](#6-a-hang-is-a-lost-run-not-a-failed-test) lost run. Read progress as file
  SIZE, never as the buffer count: the writer takes a batch OUT of the buffer before it
  writes it, so an empty buffer is what a wedged writer and a finished one both show —
  the failing shard's own teardown warning read `0 append(s) buffered, batch in
  flight=True`. That barrier is `test/crew_log_drain.py::assert_drained`; its twin
  `settle()` runs in teardown BEFORE the `KIROCREW_HOME` pin lifts, because a writer
  job resolves the data home when it runs, so a batch still in flight lands in the
  next test's home (read there as a duplicate append). A test whose subject is the
  records rather than their durability takes the fsync out with `unsync_appends`.

- **A PRODUCTION write budget sitting on the assertion's path.** `tool_risk._record_outcome`
  bounds its off-loop append with `asyncio.wait_for(to_thread(_log.append, row), 0.05)` and
  returns `None` -- no badge -- when the budget expires; that is the product's contract, and
  it is deliberate. But a test that asserts `record is not None` and reads the row back is
  then ALSO asserting that a new-file `CreateFile` + lock + write beats 50 ms on the host.
  It does on a warm Linux host (0.4 ms measured); on the Windows shard the same five tests
  (`test_decisions_tool_risk`, `..._end_to_end`) came in `None` on 26 unrelated heads in two
  days, green on every rerun. Reproduced on Linux by pricing `_log.append` at 60 ms alone
  (8 of 40 fail, every run). The fix is in the FIXTURE, not the constant: lift the budget
  to a 20 s lost-run ceiling (`GENEROUS_LOG_BUDGET_SECS`), under the suite's `--timeout=120`, for every test whose subject
  is the record -- raised, never removed, so a wedged writer still fails by name -- and pin
  the budget's own branch with the value set to `0`, which expires before the append is
  even dispatched and so carries no clock. The e2e file's `_generous_append_deadline`
  fixture is the shape (a new autouse fixture patches through `_floor_monkeypatch`,
  D11): it lifts both budgets on that path (`platform_log_append._APPEND_TIMEOUT_SECONDS`
  and `gate._LOG_BUDGET_SECS`), because lifting one leaves the assertion racing the next.
  `test_decisions_tool_risk.py` lifts `tool_risk.LOG_BUDGET_SECS` to
  `GENEROUS_LOG_BUDGET_SECS` the same way.
- **`TerminateProcess` returns before the process has exited.** It starts the
  termination and the object is signalled later, so a test that reads a member as
  exited on the line after a drain signalled it asserts on scheduling.
  `test_windows_failed_owner_drain_recovers_after_owner_drop` did that after a refused
  pass and failed as `assert None is not None` on 16 unrelated heads in two days.
  Reproduced by delivering the real terminate 0.5 s late from a timer. Wait on the
  member's own exit signal with the file's bounded `_assert_exited`, never on the
  call that requested it.

**Guess-the-latency sleeps are this class too.** `asyncio.sleep(0.05)` "to let the
first prompt register" is a bet that two awaits and a `to_thread` hop finish inside
50ms; on a loaded runner they did not, the guard the test exists to exercise was never
armed, and the test blocked on a turn nothing would ever complete — see
[class 6](#6-a-hang-is-a-lost-run-not-a-failed-test). Wait on the observable state
(`_await_routed`, an `Event`, the queue entry) and put a bounded `wait_for` around the
call whose *refusal* is under test, so a missed refusal fails at that line by name.

**A turn budget is not a barrier.** `for _ in range(40): await asyncio.sleep(0)` after
feeding a frame reads as "let the handler settle", but it staples two different claims
together and only one is turn-shaped. Measured on the kiro-cli demux
(`test_native_subagent_boundary`): the roster snapshot a `subagent/list_update` produces
lands in the SAME event-loop step that empties the reader's buffer — `readuntil` deletes
the line and the handler runs to its next await without yielding, so ZERO extra turns are
ever needed — while the auto-reject the same reader spawns for an unroutable permission
request is a TASK, and how many turns it needs is wall clock, not scheduling. With a 1 s
answer path (an added thread hop, or a loaded host) the 40-turn budget returns before the
answer is written and `assert denials == [...]` fails on the barrier. Wait on the
runtime's own signals instead: the reader's buffer draining for anything the reader
publishes itself, then `rt._answer_tasks` draining for the answers those frames earned.
For state published on the way to a broadcast, the frame arriving on the owner's queue IS
the barrier — the demux snapshots before it broadcasts, so the frame proves the snapshot
ran.

**A negative assertion is only as strong as the barrier in front of it.**
`assert qa.empty() and qb.empty()` after a turn budget passes for the trivial reason if
the demux has not read the line yet. The same pin behind the buffer-drain signal reds on
the mutation that broadcasts an unknown session's frame; behind the budget it can pass
either way.

**Ask before you park when a refusal has to be armed under the waiter's own handle.**
`_rearm_resume` captures `info._resume_event` at ARM time and the re-armed retry drops
itself if the run's event is no longer that one, so a pin about "a re-arm outliving its
waiter" has to arrange for the refusal to be armed while the waiter's event is still
installed. Starting the bounded waiter FIRST makes the pump's refusal pass race the
waiter's own ceiling, and that pass hops the store's writer thread several times (wait
expiry, two window refills, the pick's lane resolve). Measured with a 0.35 s delay on
`TaskStore.run` — an fsync-bound writer thread on a loaded host — the waiter's 0.2 s
ceiling withdrew the queue entry before the pump picked it, the refusal never happened,
and the pin failed with "the refused grant armed 0 re-arm(s), not one". The order that
carries no clock is production's own dependency order: arm `_resume_event`, ask through
`request_resume` (what `taskq_wake_through` does), wait on the ARM signal, and only then
park with `request=False`. What the run parks on afterwards can be a short ceiling,
because by then every issuer of its wake is accounted for and nothing can set the event:
the give-up is a cost, not a race.

**A lost-run ceiling must sit under the module's own `pytest.mark.timeout`.** A 30 s
`wait_for` inside a file marked `timeout(30)` can never be reached as a readable failure —
pytest-timeout kills the worker first, which is
[class 6](#6-a-hang-is-a-lost-run-not-a-failed-test). Derive the ceiling from that mark
(at most 15 s under a 30 s mark, per the
[definition](#6-a-hang-is-a-lost-run-not-a-failed-test)) and say so where it is defined.

**A cleanup awaited through `asyncio.to_thread` can be withdrawn, not merely late.**
Cancelling the awaiting task cancels the executor job too if no worker has started it, so
a `finally: await asyncio.to_thread(remove, path)` skips the removal whenever the
cancellation wins the race for a worker. `aiohttp`'s `TestServer` cancels a handler when its
client disconnects, so a test that waits on the removal fails as "cleanup never ran" however
long it waits. Shield the cleanup, and close any handle on the file in that same job before
the delete, since Windows refuses to delete an open file. Reproduce by holding the job in a
default executor that queues it unstarted (`test_session_export._HeldExecutor`).

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

**An autouse fixture patches through `_floor_monkeypatch`, never the shared
`monkeypatch`.** pytest hands a test and every fixture it requests ONE `monkeypatch`
instance, so a bare `monkeypatch.undo()` in a test body also reverts every autouse
fixture that patched through it, and the rest of that test runs on the host's real value.
The rootdir conftest's `_floor_monkeypatch` is undone independently, so an autouse
fixture requests it instead; no autouse fixture in either conftest patches through the
shared instance. A `@per_test_guard` is handed `_floor_monkeypatch` the same way, and
the decorator refuses a guard that names any other fixture. `test/conftest.py`'s `_a_shared_monkeypatch_first` requests it only so
that it is created before the others there and undone after their teardowns, which read
the test's patches while they unwind.

**A test does not patch over an attribute a fixture holds through `with patch(...)`.**
`_a_shared_monkeypatch_first` builds the shared `monkeypatch` before every
function-scoped fixture a test file declares, so at teardown the fixture's patch unwinds
first and puts the original back, and the test's `monkeypatch.setattr` over the same
attribute is undone second and re-installs the fixture's stand-in for every later test on
the worker. A run of that file alone passes; the tests that fail come after it on the
worker, in other files. Scope the inner patch to the one call with its own
`with patch(...)`. The teardown tripwire in `test/conftest.py` puts back a
`LEAK_SENTINELS` entry (`asyncio.to_thread`, `time.monotonic`, `time.sleep`,
`asyncio.sleep`) that a test left rebound and fails that test by name; a module global
that later tests read at call time belongs in that table.

**Never rebind a stdlib clock or `time.sleep`.**
`monkeypatch.setattr(time, "time", ...)` or a string target such as
`"<mod>.time.monotonic"` (where `<mod>.time` is the stdlib module) replaces the function
for the whole worker: every thread and library that looks the name up at call time
reads the fake while the patch holds, and asyncio's `loop.time()` is one of them, so a
frozen `time.monotonic` freezes every event loop's timers. Replace the
module-under-test's own `time` binding with a stand-in that delegates every other name
to the real module: `ManualClock().install(monkeypatch, mod)` does that, and refuses a
stdlib or test-runner module. By hand,
`monkeypatch.setattr(mod, "time", SimpleNamespace(**{**vars(time), "monotonic": fake}))`.
(`_PointClock` can use a bare namespace because the point reads only `time.monotonic`.)
The same reach applies, more mildly, to a patch of `asyncio.create_subprocess_exec` or
`os.getpid` on the stdlib module: every caller in the worker sees it. Prefer an injected
value; otherwise patch only for the span of the call, through `monkeypatch`.

**A counting spy counts only the calls the code under test makes.**
`monkeypatch.setattr(mod.os, "scandir", counting)` reads like a patch of `mod`, but
`mod.os` IS the stdlib `os`, so the spy counts every scan of the path from every module and
thread in the worker. A count of "how often this code walked the folder" then also counts
another module's scan of the same folder, even one the code under test asked a shared
pool to run. `test_app_manager_installed_record.py::TestListingReusesTheWalk` counted the
apps folder's walks this way. When `detect_orphaned_builtins` had no cached result,
`list_apps` validated the built-in manifests, the path gate listed the home's credential
masks on the shared `mc-pathres` pool (`credential_masks._installed_manifests`, its own
`os.scandir(apps)`), and the spy saw two walks. Whether that cache was cold depended on
the tests before it on the worker. After `test_app_backend.py::TestBootSpawnLatency` it
failed 30 of 30 runs; with the CPU busy, `-n 8` and `-p randomly` it failed 14 of 30 (9 of
30 on an 8-core Linux host). Counting only the calls the walking function makes, both
passed 30 of 30. Two ways do that:

- Replace the module's own binding, the way `ManualClock.install` does for `time`:
  `monkeypatch.setattr(mod, "os", SimpleNamespace(**{**vars(os), "scandir": counting}))`.
  Not on an owner of a mirrored facade such as `kiro_crew.apps.manager`, where `os` is a
  shared name: `test_facade_mirror_guards.py` refuses a direct owner patch of one.
- Keep the stdlib patch but count a call only when its caller is the function under test:

```python
# Wrong: an unfiltered spy counts every module's and thread's scan of the folder.
monkeypatch.setattr(manager_records.os, "scandir", counting)

# Right: only the listing walk's own calls are counted.
real = os.scandir
listing_walk = manager_records._recorded_app_names.__code__

def counting(path=".", *args, **kwargs):
    if sys._getframe(1).f_code is listing_walk:
        walks.append(os.fspath(path))
    return real(path, *args, **kwargs)

monkeypatch.setattr(os, "scandir", counting)
```

**A reload or a `sys.modules` eviction leaves the parent package pointing somewhere
else.** A string target (`monkeypatch.setattr("kiro_crew.agent_state.config_dir", ...)`)
is resolved through the PARENT PACKAGE's attribute, not through `sys.modules`. Two shapes
break it, both reproduced:

- If the attribute is gone while `sys.modules` still holds the module, every later string
  patch on it raises `AttributeError` at setup. One Windows xdist worker reported 2,167
  setup errors in the rootdir floor's `_isolate_agent_state_sidecar`, which patches
  through such a target; the test that left the package that way was not identified.
- If a test imports the module for the first time inside `mock.patch.dict(sys.modules)`,
  leaving the block drops the `sys.modules` entry but keeps the parent attribute. The next
  string patch then lands on that stale object while the next real import builds a fresh
  one, so the patch silently misses.

`importlib.reload` is the same trap one step over. It rebinds the module's globals to new
objects, but every `from kiro_crew.learn import Lesson` taken earlier keeps the old class,
so an `isinstance` or identity check in a later test fails. `test_learn.py` reloads
`kiro_crew.learn` to prove its import is side-effect free, and
`test_onboarding_import_refactor_contract.py` is the victim; a second reload to "restore"
does not undo it. So never reload a shared module in-process: probe an import on a private
copy instead, loaded with `importlib.util.spec_from_file_location` under a unique name and
registered under that unique name with `monkeypatch.setitem(sys.modules, name, mod)`
before `spec.loader.exec_module(mod)`. Without the registration a module like
`kiro_crew.learn` (`from __future__ import annotations` plus `@dataclass`) raises
`AttributeError` inside `dataclasses`; never register the copy under the real name. A
test that must evict a `sys.modules` entry restores both the entry and the parent
attribute in the same scope (`monkeypatch.setitem(sys.modules, ...)` and
`monkeypatch.setattr(parent, ...)`).

**A class-level patch reaches every live instance in the worker, not just the test's.**
`monkeypatch.setattr(SomeClass, "method", fake)` rebinds the method for the whole
process, so any instance another test left running calls the fake too. The process-wide
`executors.path_resolve_executor()` pool is that instance after any security test: its
reaper ticks on its own thread and calls `_Child.ensure_spawned`. A fake that set the
TEST pool's shutdown flag therefore fired from the shared reaper first, the test's reaper
exited before its spawn, and nothing was killed (`assert [] == [<_Child ...>]`, 15 macOS
runs on 14 branches in three days). Poking the shared reaper's wake event in a loop
reproduced it 18 times in 30. Fix: the fake acts only for the instances under test and
hands every other one to the real method (`if child not in executor._children: return
real(child)`), as `TestShutdownRefillWindow` in `test_subprocess_pool.py` does.

**Count descriptors by what they point at, never by the process total.** A shared xdist
worker opens and closes logger, SQLite and pool handles on its own schedule, so a
whole-process count drifts by a few under load (`assert 87 == 90` on macOS) while the
code under test leaks nothing. Resolve each descriptor's target (`pinned_fs.fd_real_path`:
`/proc/self/fd` on Linux, `F_GETPATH` on macOS) and count only the ones naming the object
under test, as `_descriptors_pointing_at` in `test_chat_threads.py` does; skip where the
host can resolve neither. Prove it with a negative control: drop the `os.close` under
test and the count must move.

**A process-wide verdict cache decides whether the code under test runs at all.**
`cron_script._shell_is_posix_strict` caches its answer per shell for the life of the
process. Any earlier test on the same worker that ran the real probe leaves `/bin/sh`
decided, so the cancel test's probe never ran, the blip it patched in never fired, and
the cancel had nowhere to land (`assert [] == [True]`, two macOS runs out of two). A
test that patches a step behind such a cache rebinds the cache to a fresh dict with
`monkeypatch.setattr` for its own duration, as `_probe_blip` in
`test_sandbox_interpreter_enoent_retry.py` does. To prove the fix, pre-fill the cache
in a plugin: the test must fail on the old code and pass on the new.

**Sharding does not just scatter this class, it hides it — so a full-suite run is the wrong
place to be finding it.** `ci.yml` assigns whole files to Linux/Windows shards
before import (every platform assigns whole files), and a leaker only damages tests
that land in the *same process*, so a leak whose
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

**That rule did not stop #37659, so check it before push.** #37659 added
`secrets.read_audit._loaded`, the audit switch read from `config.json` and reused for
`CONFIG_TTL_SECS` (30 s) when there is no live config snapshot, consulted on every vault
read that returns a secret. It reset the global only in its own test file.
`test_wakatime_handlers.py::test_status_loads_config_off_the_event_loop` counts
`KiroCrewConfig.load` calls on the WakaTime status route: one when another test on the
worker had read the vault in the last 30 s, two when the cache was cold. On Linux it failed
alone 20 times in 20, in 3 of 5 `-n 4` runs of its file and in 4 of 9 shuffled runs, and it
reddened backend shard 1 on unrelated PRs. #37726 pinned the test cold and #37754 added the
floor entry. The rule above is one sentence, nothing checks it, and #37659's own CI was
green, as the sharding described above allows. So run this on a product change before you push, and run it as a reviewer
of one:

```bash
git diff origin/main -- 'src/kiro_crew/*.py' \
  | grep -nE '^\+(_[A-Za-z0-9_]+ *(:[^=]+)?= *(None|\{\}|\[\]|dict\(|set\(|OrderedDict\()|@(functools\.)?(lru_cache|cache)\b| +global )'
```

On #37659 it prints `_seen`, `_loaded` and `global _loaded`. Every hit at module scope on a
path other tests drive gets its entry in `test/conftest_resets.py` in the same PR. The line
only knows those initializer spellings; a global bound to anything else still needs a
reviewer's eye.

The reset goes in `test/conftest_resets.py` as an autouse fixture, re-bound in
`test/conftest.py`'s `from conftest_resets import (...)` block. When that file is at its
1,600-line target, the reset goes in a `test/conftest_resets_<topic>.py` sibling named for
the package it resets (`conftest_resets_acp.py`), re-bound by its own import block there.
A reset in the new
cache's own test file does not count: the next file on the worker is the one that
inherits it. `test/test_process_cache_floor_ratchet.py` refuses the common shape, but
only in a CI run whose checkout contains it, so a PR whose last green run is older than
the ratchet can still merge the leak. The AUTOSDE rule
`a-module-level-cache-ships-its-per-test-reset` asks for the same reset at review time,
for every shape (an `lru_cache`, a `global` flag, a name the ratchet's pattern misses).

```python
# WRONG: src/kiro_crew/subagent_persistence.py fills a module-level memo and no floor
# clears it, so a verdict one test left lets the next test's sweep skip a run.
_NOT_DUE_MEMO: dict[str, tuple[tuple[int, int, int], int | float, bool]] = {}

# RIGHT: the same PR adds the floor entry (test/conftest_resets.py).
@per_test_guard
def _reset_subagent_not_due_memo():
    def clear():
        module = sys.modules.get("kiro_crew.subagent_persistence")
        if module is not None:
            module._NOT_DUE_MEMO.clear()

    clear()
    try:
        yield
    finally:
        clear()
```

No reset is needed for a constant nothing writes after import, a registry filled only at
import time, or a memo of a pure function of its arguments. A cache-named empty
container of that kind still trips the ratchet, so its line says
`# process-cache-ok: <reason>`.

**A switch or allowlist snapshot that a handler test publishes is the same leak, and the
grep above does not see it.** `dashboard.source_providers.hosts` holds the session-card
chip switch as `_session_card_chips_snapshot: bool = True` and the host allowlists as
`frozenset()` snapshots. The real `/api/dashboard/config` PUT publishes the switch, so
`test_session_card_source_links_knob.py::TestConfigEndpoint::test_put_persists_off`, which
sends `session_card_source_links: false` and patches nothing, left the chips off for its
worker. Every later `to_dict` there reported `source_links_total` 0: MEASURED, that test
followed by `test_source_link_github_case.py` fails 4 of 21 at `-n0`, every run; on CI it
reddened backend shard 7 on main and on four unrelated PRs in one morning. A pin in the
polluting file's fixture stops that file; the floor entry `_cold_source_link_settings`
stops the next file too, by pinning every snapshot the publishers write to its cold value
for every test. Leave a monotonic generation counter unpinned, because caches key on it
and putting an older number back can match an entry built under other settings. Before
push, also grep for scalar and frozen initializers of a global that a write path
republishes:

```bash
git diff origin/main -- 'src/kiro_crew/*.py' \
  | grep -nE '^\+_[A-Za-z0-9_]+ *(:[^=]+)?= *(True|False|frozenset\(|[0-9.]+$)'
```

```python
# WRONG: the real PUT publishes False into the module global, and nothing puts it back.
async def test_put_persists_off(self, handler_app, cfg_file):
    await client.put("/api/dashboard/config", json={"session_card_source_links": False})

# RIGHT: the floor hands every test the cold snapshot (test/conftest_resets.py).
_floor_monkeypatch.setattr(hosts, "_session_card_chips_snapshot", True)
```

**Work QUEUED by a test is shared state too, and resetting the object it runs against is
not enough.** MEASURED: a member row's event is fire-and-forget on the single `eventlog-io`
worker (`eventlog_hooks.submit`), and the member log's path is resolved from
`KIROCREW_HOME` when the append RUNS, not when it was queued. In three failing Windows
shard-8 runs, the test just before the red on the same worker was a member resume that
ends just after appending a row. Its append ran during the next test, opened that
member's log under the next test's home, and the lock it held there refused that test's
own first `record_activity`, which answered `False`. Resetting the event-log singleton at
the boundary does not help: the closure resolves the service and the path as it runs.
(`set_service` does close the outgoing service and release its cached leases, so a
lease held by a replaced service is a separate cause.) On
Linux the overlap is usually too narrow to see, so the leak shows only where fsync is
slow.

The rootdir `conftest.py`'s `_reset_member_eventlog_singleton` (rootdir, so the
in-package app suites get it too) drains that queue at teardown, before the home pin is
undone (it depends on `_isolate_kirocrew_home`), and FAILS the test that filled a queue
which will not drain — so the cost lands on the test that queued the work rather than on
whichever test would have inherited it. It binds `drain_for_shutdown` at SETUP when the
module is already loaded, so a test of the shutdown path that replaces that function
with a wedged or recording stand-in cannot wedge the floor.

**A poller or a debounced writer an earlier test left behind resolves the home from
under a later one, and the guard blames the later one.** MEASURED on a Linux shard with a
throwaway tripwire on `config_dir()` keyed by the test that started the thread or queued
the job: in one 22k-test shard, six resolutions by `kc-embed-evict` threads born in
`test_custom_embedding_model.py` (a `LlamaCppEmbedder` whose fake model was published and
never `close()`d; its idle evictor re-reads the memory config on every tick) landing in
four unrelated files, and 36 by `mc-maint` jobs from `test_crew_log_emit.py` and
`test_crew_log_edge_fanout.py` (`SessionTreeProjection._debounced_write` built its
payload's `root` with a live `_current_root()` when it WOKE, a debounce after the test
that armed it had ended). Under the per-test home these resolve another test's home and
nothing notices. The visible failure needs a third party: a test that cleared
`os.environ` (`patch.dict("os.environ", {}, clear=True)`) for its own reasons has no
`KIROCREW_HOME` for its duration, so a tick inside that window falls through to the
operator's real `~/.kiro/crew`, and `_refuse_a_resolved_real_default_home` fails THAT
test -- `test_dashboard_origin.py`'s hanging-lookup test, whose 200 ms window was the
widest in its file, on nine PR branches in a week; the leakers passed every time.
Reproduced on demand by shrinking the evictor's poll ceiling and running one loader test
before the victim: red on the parent, green with the fixes. Three fixes, each needed:
the leakers close what they start (`close()` in the tests that build a backend; the
projection pins `root` beside `target` when it ARMS the write, so the worker resolves
nothing); the floor catches the next one by name (`_no_leaked_embed_evictor`, and the
teardown hook cancels the pending checkpoint writes); and the victim keeps the pin,
unsetting only the variables its subject reads (`SSH_CONNECTION`, `SSH_CLIENT`) rather
than clearing the environment -- the Rules entry on dropping the override already says
so, and the floor's pin cannot survive a `clear=True` any other way.

One thing that floor does not reach, residual rather than fixed: it covers `eventlog-io`
only — the dashboard's `notif-io` pool
(`_notification_io_executor`) resolves the notifications file the same way when its job
runs. The fix that removes the race rather than draining it is the one in
[the classes it found](#the-classes-it-found-and-the-one-correct-fix-for-each): resolve
every path when the work is queued. For the member log that means threading a home
through `crew_log/store_paths.py`, which derives it at each of the log, lease, segment and
checkpoint paths; production never moves its home, so only the suite pays. A new
fire-and-forget writer resolves at queue time AND joins a drain.

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
matcher, a countable unit of work), assert on that: wrap the primitives so the WORK they
do is counted -- characters read, entries visited -- and require that doubling the input
at most doubles the count (a small constant factor of passes aside). That fails only on
the property, never on the runner, and unlike a count of *calls* it also sees a
superlinear scan hidden inside one call. Keep any timing bound the guard already carried
at its original constant, read on thread CPU: a ratchet may only tighten, and the only
way such a bound leaves is a count proven to catch what it caught (below). A *timed*
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

**A kept budget still reads the tracer, so replace it once a count can do its job.** The
self-module scan guard (`test_denied_commands_security_cost.py`,
`test_the_scan_is_linear_not_quadratic`) kept a 0.5 s thread-CPU budget beside its call
counts, for cost added outside the counted functions. The scan costs about 0.07 s bare.
Coverage's tracer multiplies that about five times, so on the instrumented Linux shards
the intact scan read 0.51 to 0.92 s, run after run, on heads that never touched it.
Taking the smallest of several readings removes a one-off spike, not the multiplier,
which is on every reading. Removing the trace function for the readings takes the
multiplier off and leaves a clock. A budget may leave only when a count
takes over everything it caught: plant each regression the budget was there for and show
the count fails it. `_interpreter_work` in `test/denied_commands_security_helpers.py` is
that count where no narrower unit exists: the interpreter instructions the call executes
on this thread, read through `sys.monitoring`. It is the same number bare, under coverage
and on a loaded host, so it is bounded as a ratio between three equally spaced sizes
(`_linear_work`: the second step costs at most a twentieth more than the first), and
what one more token costs is held under a wide fixed ceiling, so a slowdown that is the
same for every token still fails. Planted
in the scan, the old per-token re-walk fails it, and so does a per-token walk that
inlines its comparisons and calls no counted function, which the 0.5 s budget passed. It
sees every step taken in Python, however the loop is spelled; it does not see work inside
one C call (a slice, `in` on a list, `str.join`, a regex search). For a regex, where all
the work is inside one call, assert what the engine is HANDED before the hostile input
runs: that a refused pattern compiled nothing, that the one exponential construct never
reaches a compiled pattern, that a bounded matcher passes at most its window, that no
character can start two alternatives of one repeated body (read off `re._parser`'s
parse of the pattern, as `test_the_quoting_regex_is_not_redos_prone` does). A
regression then fails at that assertion instead of never returning (class 6). A
complexity guard that runs both a Python walk and a regex search asserts the two
apart: what the engine is handed, then `_linear_work` on the call's instructions
(`test/denied_commands_security_helpers.py::_assert_scan_is_linear`).

**"Returns at once" is a claim about waits, so assert the wait.** Most short bounds
(`elapsed < 0.5`) guard no complexity at all: they say a call never blocks, polls or
sleeps. A count of instructions cannot see that (a `sleep` is one instruction), and a
clock cannot tell a wait from a slow host. Assert that the wait was never taken:

- spy the blocking primitive and refuse it on the spot: a lock whose `acquire` fails
  when asked to block (`test_record_frame_never_waits_on_the_byte_budget_lock`), a
  `time.sleep` / `concurrent.futures.wait` that raises
  (`test_a_drain_with_nothing_outstanding_still_returns_at_once`), a condition whose
  `wait` raises for any thread but the one allowed to wait
  (`test_a_nested_hold_by_the_same_task_does_not_wait_behind_a_waiting_window`);
- record what a handle is asked and allow only the non-blocking calls
  (`is_alive` and `terminate`, never `join`, in
  `test_request_stop_does_not_wait_for_the_child`);
- for a coroutine, settle it in bounded loop turns (`await asyncio.sleep(0)`), with no
  timer to run out (`test_cancelled_mid_steer_does_not_hang_on_a_stalled_reject`);
- freeze the clock a deadline is read from, so the one timer left is the one under test
  (`test_silence_from_a_queue_aware_daemon_times_out_before_the_budget`);
- for "refused before any per-character work", forbid the per-character steps and
  require the same instruction count at three input sizes
  (`test_command_length_ceiling_refuses_before_allocating_scan_state`).

Planted, the sleep, join, timed acquire or extra walk each fails these at once; the
0.5 s bounds they replaced passed six of eight such plants.

The 1.5-3 s bounds carry three more claims, each asserted as a value:

- "waits at most its budget": record the timeout the code hands the wait and assert
  that value, not the time it took
  (`test_off_loop_miss_gives_up_rather_than_waiting_unbounded` asserts the acquire is
  asked for `(True, 0.1)`);
- "refused without hanging": spy the system call and refuse the blocking form
  (`test_a_fifo_is_refused_without_hanging` refuses an `os.open` without
  `O_NONBLOCK` and asserts the FIFO is never read);
- "teardown does not run out its grace window": record the signals sent
  (`test_terminate_pgid_graceful_exit_returns_fast`: SIGTERM, liveness probes, no
  SIGKILL).

A spy that refuses or replaces a blocking primitive is installed for the thread under
test only (the test's thread or the event loop's), so a pool thread the product starts
still gets the real primitive. A spy that only records arguments and passes the call
through may see every thread.

Two traps in building those spies. A sleep is refused with a `sys.setprofile` hook
on the thread (or task) under test, never by patching `time.sleep` or
`asyncio.sleep`: the semgrep rule `kirocrew.test-stdlib-clock-rebound` refuses
rebinding `time.sleep`, the K3 ratchet refuses `asyncio.sleep`, and either patch
would reach every thread
(`test_timeout_reaps_the_scrape_before_the_adjacent_whoami`). A spy on `os.open`
must also be added to `os.supports_dir_fd`, or the pinned walk refuses the
`dir_fd` call before any file is opened and the test passes for the wrong reason
(`test_fifo_briefing_is_refused_without_blocking`).

For a regex — no instrumentation surface at all — use `test/conftest.py`'s
`assert_rejected_without_backtracking(reject, build_pump)`, and note what the mutation
showed: a shared character between two adjacent quantified classes in the marker grammars
is not polynomial but EXPONENTIAL (doubling per pumped character; 4.3 s at 24), so the
200 000-character pump the old guards used would never return under a regression and the
worker would die at `--timeout` (class 6) — and no single "small" size is safe either: a
harsher mutant grew ~8x per pumped block. The helper therefore RAMPS the pump one unit
at a time from 1 to 24, on thread CPU, failing at the first size that overruns its
budget (so catching any regression costs about one growth step), and only then tries
ascending long pumps for the polynomial class, each held both to an absolute budget and to
at most 25x the cost of the pump 10x shorter (above a 0.25 s floor, so a coarse clock tick
cannot read a linear cost as an unbounded ratio). The long pumps' absolute budget is
`REDOS_LARGE_BUDGET_SECONDS` unless the caller passes the keyword-only `budget_seconds`:
a guard converted from a wall-clock bound passes the constant it already carried, as
`test/test_markdown_link_parentheses.py` does, since a ratchet may only tighten. Size any
complexity guard so the regression it exists to catch FAILS it, not hangs it. When the
pattern itself is the guard, read its parse instead of pumping it:
`test/regex_structure_helpers.py` has `unbounded_repeat_bodies` (one unbounded repeat of a
fixed-width body, nothing unbounded nested in it, as
`test_a_long_non_matching_run_is_rejected_promptly` asserts) and `run_cost_unproven` (no
two run-consuming repeats separated only by items that match the run, as
`test_long_untrusted_paste_is_linear` asserts on every pattern the scrub hands the run).

**First check that the time is even the algorithm's.** A test that bounded `elapsed` around
the bash gate took 144s under load — but with the gate's filesystem probes stubbed, the same
input ran in 50ms. The budget was measuring ~2,700 `stat` calls, not the bounded-working-set
property it named. Stub the I/O, **count the probes**, and assert the count grows linearly
with the input; that is the property, and it costs nothing.

### 6. A hang is a lost run, not a failed test

pytest-timeout has no `SIGALRM` on Windows, so a test that blocks past `--timeout` is not
failed in place: the whole xdist worker is killed (`node down: Not properly terminated`),
and with `--max-worker-restart=0` — which the Windows job needs, see `ci.yml` — the run
**aborts** with every test that worker had not reached still uncollected. MEASURED: one
test that could wait forever (`test_acp_runtime_session_handle::test_concurrent_prompt_on_same_handle_rejected`,
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

**On Windows, `subprocess.run(..., capture_output=True, timeout=N)` is not a bound
either.** The stdlib kills only the child it started, then drains the pipes with no
timeout; when that child is a launcher (Git for Windows' `cmd\git.exe`, a `.cmd` or exe
shim), the real process still holds the pipes and the call blocks until it exits.
MEASURED (#37448): `git_config_hooks`' hook-name scan bound `subprocess.run` once at
import, so it never reached the tree kill, and an md_notebook sync in
`test_md_notebook_git_trust.py` hung past `--timeout` and took its xdist worker down.
Production code spawns a timed, piped child through `run_killing_tree_on_timeout`
(`stdlib_run_killing_tree_on_timeout` when it binds the stdlib's run on purpose), never
through a module-level alias of `subprocess.run`
([platform-compat](platform-compat.md)).

**A production budget a test passes only to bound a broken run is one of these
ceilings, and is sized like one.** `test_auto_improvement_members.py` handed each member
assignment `timeout_s=5` around ~0.3s of config loads, sqlite commits and fsyncs; with
four Windows CI workers in that file at once the same work took 6-8s, and the
assignment came back `TimeoutError: timeout after 5s` with nothing wrong. When no
assertion in the test is about the budget, route every call through one helper that
names the budget and the wait as lost-run guards (`_run_member`), and leave a tight
budget only to the tests that drive its expiry on purpose.

A wait for something a CHILD process will write also watches the child, and quotes
what it said. MEASURED in `test_crew_log_real_crash.py`: a marker poll with no
`child.poll()` in it spends the whole 60 s ceiling on a child that crashed at import,
then fails as "never reached failpoint" with the traceback that named the cause sitting
unread in a pipe. So fail as soon as the exit is seen -- re-reading the marker first, so
a child that announces and then exits still counts -- and quote the child's output. Send
that output to a FILE under `tmp_path`, not to a pipe nobody reads before the wait
returns: once a pipe buffer fills, the child blocks in its write and the wait times out
on a child that is merely stuck.

**A real interpreter child is started once per module, not once per test.** MEASURED in
`test_ssrf_guard_fail_closed.py`: four tests each started their own `node`, and
`--dist loadgroup` handed them to four workers, so four cold Node starts, each paging in a
~100 MB binary, began in the same second at 98% of Linux `Backend Tests (3.12, 7)`, in
runs that peaked at 15.0 of the runner's 15.7 GB. In six of six failed attempts
(`linux-pytest-progress-7`) the shard ran ~50 tests a second up to that second and almost
none after it: every test in flight, `node` or not, hit `Timeout >120.0s`, one `node` came
back killed (`-9`), and some runs lost the runner. Passing attempts finished the ~530 tests
left in under ten seconds. On a dev host under memory pressure the same four children took
up to 23 s each, against 0.03 s unloaded. So run every case through ONE
module-scoped child that prints a verdict per case, put the module in its own
`xdist_group` so it lands on one worker, and give the child a lost-run ceiling of half the
test's timeout, `kiro_crew.testing.wait.default_timeout()`. A literal equal to
`--timeout` (it was `120` against `--timeout=120`) is never reached: pytest-timeout fires
first, and the child's own deadline names nothing.

**What a lost-run ceiling is.** A bound on a condition wait (`wait_for`, `join`,
`communicate`, a poll deadline) that only ends a broken run, and fails the test loudly
when it expires, is fine as a literal well under the module's timeout: the `RIGHT`
snippets in classes 2 and 6 are that. When the work a passing run needs takes a real
share of the bound, the bound becomes a lost-run ceiling, and it has four properties. It
is a named `UPPER_CASE` constant. It is at least 10x the worst case measured, and a
comment beside it cites the measurement: per-test durations from the slowest shard's
`linux-pytest-progress-*` / `windows-pytest-progress-*` artifact or its `--durations`
output, or, when no CI measurement exists yet, 10x the worst of the repeats in
[Proving a determinism fix](#proving-a-determinism-fix). It is at most half the
module's `pytest.mark.timeout` (or the suite's `--timeout=120`), so it is reached as a
readable failure rather than a dead worker; when the measured work cannot fit, the file
declares its own larger `pytest.mark.timeout` with the derivation beside it, which sizes
the run rather than loosening a race. And its failure message quotes the elapsed time
and the last state the wait observed. It bounds a broken run; it never decides a passing
one, so it is never the thing a fix tunes.

### 7. Data order and timestamp ties

Two records written back to back can carry the SAME stamp, and two directory entries or
set members come back in an order nobody chose. A test that asserts such an order is
asserting something the code never defined.

**Clocks tick coarsely.** On Windows through CPython 3.12, `time.time()` and
`time.monotonic()` both advance in ~15.6 ms steps by default. `time.sleep` there no longer
rounds up to that tick (3.11 moved it to a high-resolution waitable timer), so
`sleep(0.004)` can return inside one tick and the two stamps tie. On most Linux kernels a
filesystem stamps `mtime` from the coarse clock, which advances once per jiffy (1-10 ms,
by `CONFIG_HZ`); HFS+ stores whole seconds and FAT two. A sleep between two writes makes a
tie less likely, not impossible.

**Set the stamps, and assert strictly.** Write the stamp the order depends on yourself:
`os.utime(path, ns=(t, t))`, a stepped clock on the module's own binding (D2), or an
explicit `created_at`. Then assert the ORDER as an exact list or a strict `<`, never
`>=`: a `>=` between two stamps also accepts the tie, so the order it claims to check
can come back either way and the test still passes. (A `<=` is right for an invariant
a tie satisfies, such as start <= finish.)

**A time-sorted output needs an equal-stamp pair.** If production sorts by a time key,
the fixture includes two records with the SAME stamp and pins which one comes first. If
production has no tie-break, the missing tie-break is the finding: report it as a defect
rather than hiding it behind a sleep that pins nothing.

**An order nobody defined is not an order.** `str` hashing is salted per interpreter, so
a `set` of strings, and a dict built from one, iterates in a different order in every
xdist worker and every run. `os.listdir`, `Path.iterdir` and `glob` return the
filesystem's order: hash order on ext4, name order on NTFS, creation order on neither.
Side effects that `asyncio.gather` or `as_completed` workers append land in completion
order; `gather`'s own return list is in argument order and is fine. Compare such output
as a `set` or `Counter`, or sort it by a total key first.

Three worked examples:

- `test/test_issue_radar_crew_fabric.py::FoldTest::test_items_are_newest_progress_first`
  separates two crew-log millisecond stamps with `_tick()`, a 4 ms sleep.
  `crew_store.fold_fabric` sorts on `last_progress_at` alone, so on a tie the order falls
  back to insertion: the test and the product share the same gap.
- `test/test_auto_research.py::TestCRUD::test_list_newest_first` sleeps 10 ms between
  two campaigns and asserts `camps[0]["created_at"] >= camps[1]["created_at"]`. On a tie
  it passes with the campaigns in either order, and `list_campaigns` orders by
  `created_at DESC` with no second key.
- `test/test_session_control_revive.py::test_a_revive_in_the_close_clock_reading_keeps_the_marker`
  has the structure to copy: it freezes the clock so the tie happens on every OS, pins
  the documented tie-break (a resume inside the close's clock reading keeps the `closed`
  marker), then steps the clock once and shows the other side. Copy the structure, not
  its spelling: it freezes the stdlib `time.time`, and a new test freezes the
  module-under-test's own clock binding instead (D2).

### Proving a determinism fix

A test is proven by running it many ways, and a FIX by making the failure condition
happen on purpose, not by a run of green results. Each new or changed test passes the
first two; a fix to a flaky test also passes the forced condition, and a fix to a test
that passed vacuously also the negative control:

- **Repeats.** 20 runs at `-n0`, then 10 at `-n 4 --dist loadgroup
  --max-worker-restart=2` with two sibling files as co-tenants:
  `for i in $(seq 20); do python -m pytest -n0 -p no:cacheprovider -q <node ids> || break; done`.
- **A shuffled order.** pytest runs node ids in the order they are given on the command
  line, so pass them shuffled with a seed you print, and keep that seed to replay a red.
  Read them from a file (`@file`, pytest 8.2 and later; the pin is 9.x) so ids with
  spaces or `[...]` and long lists survive bash and zsh alike, and use `-qq` because
  `setup.cfg` adds `--verbose`:

  ```bash
  python -m pytest --collect-only -qq -n0 <files> | grep '::' > ids.txt
  SEED=$RANDOM; echo "seed=$SEED"
  python -c "import random,sys; i=open('ids.txt').read().splitlines(); random.Random(int(sys.argv[1])).shuffle(i); open('shuffled.txt','w').write(''.join(x+'\n' for x in i))" "$SEED"
  [ -s shuffled.txt ] || { echo "no ids collected"; exit 1; }
  python -m pytest -n0 -p no:cacheprovider -q @shuffled.txt
  ```

  The guard matters: an empty `@file` is an empty argument list, and pytest then runs
  the whole default `testpaths` suite. Any loop that builds node ids aborts on an empty
  list the same way; the full suite never runs locally.
- **The forced condition.** A throwaway `-p` plugin, never committed, makes the race
  happen every run: a coarse clock (`time.time()` and `time.monotonic()` floored to the
  Windows tick above, with `time.sleep` left real, which is how a 3.12 Windows runner
  behaves), a late executor (every job starts after a delay), a busy thread holding the
  GIL, or a delay at the seam the diagnosis names. The parent commit must RED under it,
  and the fix must stay green. A parent that stays green means the diagnosis is wrong.
  For a timestamp tie the condition can be committed: `test/windows_sim.py`'s
  `colliding_clock` forces the tie through a module's own `datetime` binding.
- **A negative control** for a fix to a test that passed vacuously: revert the product
  guard the test pins, and the test must fail by name, not pass and not hang.

A timing result from an overloaded host is not evidence either way: a pass there can
hide the race, and a red can be the host's. Size a ceiling and judge a fix from runs at
CI-equivalent load (the suite's own bounded `-n`, no co-tenant saturating the cores), and
reproduce a race with the forced condition rather than with host load. Record the
commands and both outputs in the pull request.

### A Playwright spec that passed only on a retry: read what the page received

The E2E gate fails a run when any spec passes only on a retry (`MAX_FLAKY_SPECS` is 0).
In the 48 hours to 2026-11-16, four specs did that in 26 PR jobs. In every case the
value Playwright received was one the PAGE produced after the spec's action. None of them
was a spec that read too early. In three of the four, a longer timeout or one more wait in
the spec would have hidden a real defect. So before you touch the spec, read the
`Received` line and name who wrote it.

- **Received is the page you just left.** `settings.spec.ts` clicked About and kept seeing
  `/settings/display/view`. `embed-popout.spec.ts` saw `/embed/chat/...` and then
  `/embed/settings?sub=view` again. In both, an effect wrote a replace navigation from a
  render that still carried the old location. `BrowserRouter` applies a history change
  inside `startTransition`. An urgent update (here the sub-nav's ResizeObserver width) can
  commit first with the previous `useLocation()`, and the effect's `replace` then
  overwrites the entry the click or redirect made. This is a product race. Fix the write,
  not the spec ([PR #37885](https://github.com/kirodotdev/KiroCrew/pull/37885)):

  ```tsx
  // WRONG: re-runs when the width lands, and builds its target from a location that can
  // lag the history by one pending transition
  useEffect(() => {
    if (width !== null && twoPane && !selectedKey) select(items[0].key)
  }, [width, twoPane, selectedKey])

  // RIGHT: skip while a newer entry is pending. The check changes identity with
  // location.key, so the effect runs again once the router renders that entry.
  const renderedLocationIsCurrent = useRenderedLocationIsCurrent()
  useEffect(() => {
    if (width !== null && twoPane && !selectedKey && renderedLocationIsCurrent()) select(items[0].key)
  }, [width, twoPane, selectedKey, renderedLocationIsCurrent])
  ```

  Measured: 3 red runs (settings) and 2 (embed) in 48 hours before the fix; after it, 30
  of 30 each at `--retries=0` on Linux, idle and with 6 of 8 cores busy.

- **Received is the state from before an API write the spec made.** `session-tags-e2e.spec.ts`
  test 8 posted a drop over `request` as soon as the column rendered, and the slot never
  reached the Done column. The page's own `GET /api/chat/slots` was serialized before the
  drop and applied after the drop's push, so the old row came back and nothing pushed
  again. Wait for the page's read to finish before the write
  ([PR #37286](https://github.com/kirodotdev/KiroCrew/pull/37286)):

  ```ts
  // WRONG: the column renders before the page's slot read has returned
  await page.goto('/chat')
  await page.waitForSelector(`[data-testid="column-${col.id}"]`)
  await request.post(`/api/chat/slots/${slotKey}/drop`, { data: { column_id: col.id } })

  // RIGHT: arm the wait BEFORE goto, so a fast reply is not missed
  const slotListRead = page.waitForResponse(r =>
    r.request().method() === 'GET' && new URL(r.url()).pathname === '/api/chat/slots')
  await page.goto('/chat')
  await page.waitForSelector(`[data-testid="column-${col.id}"]`)
  await (await slotListRead).finished()
  await request.post(`/api/chat/slots/${slotKey}/drop`, { data: { column_id: col.id } })
  ```

  Measured: 19 red runs in 48 hours, all before the fix; after it, 30 of 30 at
  `--retries=0`, idle and under the same load. A user can hit the same reorder. If that ever matters, the fix is a
  version check on the read in the product, not a wait.

  The fix covered test 8 only. Test 22 in the same file drags a card with
  `DragEvent`s instead of posting the drop, and has the same race. It went red on
  2026-11-17 with the same `element(s) not found` on the Done column, while the
  server already listed the slot as Done
  ([issue #37561](https://github.com/kirodotdev/KiroCrew/issues/37561),
  [PR #38482](https://github.com/kirodotdev/KiroCrew/pull/38482)). Waiting for the
  card in the source column is no barrier: the socket's connect snapshot renders
  it while the read is still in flight. So when you fix this shape, fix every
  test with it in the same change. Search the spec file and its siblings for a
  `goto('/chat')` followed by a slot write (`/drop`, `/tags`, `/folder`, a
  `DragEvent('drop')`, a row-menu click) and then a page assertion on that
  slot. A test that reloads before it asserts, or asserts only over `request`,
  does not have the race.

  ```ts
  // WRONG: the row is on screen before the read returns, so the drag can still
  // land between the read's serialization and its reply
  await page.goto('/chat')
  await page.waitForSelector(`[data-testid="column-${colTodo.id}"] [data-slot-key="${slotKey}"]`)
  await page.evaluate(dispatchDrop, [slotKey, colDone.id])

  // RIGHT: the same barrier as test 8, armed before goto
  const slotListRead = page.waitForResponse(r =>
    r.request().method() === 'GET' && new URL(r.url()).pathname === '/api/chat/slots')
  await page.goto('/chat')
  await page.waitForSelector(`[data-testid="column-${colTodo.id}"] [data-slot-key="${slotKey}"]`)
  await (await slotListRead).finished()
  await page.evaluate(dispatchDrop, [slotKey, colDone.id])
  ```

  Measured on Linux (32 cores, all busy, `--retries=0`, the harness gateway):
  unforced, test 22 passed 30 of 30 before the fix and 30 of 30 after, so load
  alone does not reproduce it. With the page's first slot read held until the
  drop's push had moved the card (`page.route`, then `route.fulfill`), it failed
  20 of 20 before the fix, with the CI error, and passed 20 of 20 after.

- **Received is a real miss.** `chat-form-switch-anchor.spec.ts` found the reader's row
  33 to 54 px lower after a 1400 -> 382 px resize. Two virtualizer defects caused it. The
  anchor was dropped on the scroll event its own write raised
  ([PR #37931](https://github.com/kirodotdev/KiroCrew/pull/37931)). And under load a
  window recompute ran before the fire that paid a reflow, and unmounted the reader's row
  ([PR #37965](https://github.com/kirodotdev/KiroCrew/pull/37965)). Widening
  `PX_TOLERANCE`, the quiet window or a timeout would have shipped both. Measured with 6
  of 8 cores busy: 10 of 30 red after the first fix, 30 of 30 green after the second.

To check a fix, run the spec through the harness gateway (`test/test_playwright_e2e.py`) with
`--repeat-each=30 --retries=0` and expect 30 of 30. A spec that stays green idle can still
be red under load: the form-switch miss was 0 of 30 idle and 10 of 30 with most cores busy.
So reproduce the red first, with load if needed, and judge the fix against that red; a
green run under load proves nothing alone. Raising retries, `test.skip`, a
`waitForTimeout` and a looser assertion are not fixes.

### The gateway harness runs on all three platforms

`kiro_crew.testing.harness.spawn_feature_gateway` boots a real gateway subprocess
on an isolated throwaway `KIROCREW_HOME`. Two of its internals are platform
contracts rather than implementation taste, and both used to be POSIX-shaped:

- The `KIROCREW_READY:` wait reads the child's stdout through ONE daemon reader
  thread feeding a `queue.Queue`. It is not a selector, because
  `selectors.DefaultSelector()` is select()-based on Windows and accepts sockets
  only, so registering a subprocess pipe there raises. The queue's bounded `get`
  keeps what the selector poll bought: the `KIROCREW_HARNESS_READY_TIMEOUT`
  deadline (default 60s) is enforced even while the child is alive and silent,
  and a child that exits during the wait fails IMMEDIATELY with its stderr tail
  rather than waiting out the deadline.
- Teardown routes through `platform_compat.kill_process_tree` on Windows and
  `terminate_pgid` on POSIX. There is no `setsid` or `killpg` on Windows, and
  `taskkill /F` gives the child no shutdown budget there.

Because the harness spawns a real process per test, a module built on it runs
with `-n0` and an explicit `--timeout` above the widest readiness window: under
xdist a block would take the worker with it (flake class 6 above), and on Windows
that aborts the run. `test/e2e/test_gateway_boot_matrix.py` is the reference
shape; `docs/ci/e2e-gate.md` documents the job that runs it.

## A gate that reds on someone else's pull request

The flake classes above are about a test that disagrees with itself. This is the
other failure mode: a gate that is perfectly deterministic and still sends its bill
to the wrong person. Main goes red, and the contributor who pays is whoever opens
the next pull request — someone whose own diff contains nothing to fix, and who has
to spend an hour proving that before they can even read their own result.

On 2026-09-27 three of these fired at once and took **every open pull request** red
on three separate lanes. They are three different shapes of the same mistake, and
each has a cheap structural fix.

**Before you believe a red is yours, price the alternative.** Ask whether the same
check is failing on other people's heads: list the most recently updated open pull
requests and read the check-runs on their head SHAs. It costs no rerun, spends no
review quota, and it answers in seconds what reproducing locally can take an hour to
answer. A check failing on 8 of 10 unrelated heads is main's, not yours — and the
one head where it passes usually turns out to sit on a base from before the break,
which is itself the confirmation.

### A count ceiling with no diff-scoped companion

`expect(bad.length).toBeLessThanOrEqual(8)` tolerates inherited debt, which is
legitimate. What it cannot do is say WHOSE violation it is: a branch that adds one
rides under the ceiling, and the round that finally reds belongs to whichever branch
happens to run after the count crosses.

Pair every ceiling with a zero-tolerance assertion over the values the branch itself
wrote, diff-scoped against its base. The count keeps guarding the inheritance, the
diff-scoped half names the author, and nothing is stored, so two branches have no
ledger line to conflict on. `website/src/i18n/style/bnStyle.test.ts` carries both
halves now; [i18n-gates](../../ci/i18n-gates.md) states the rule.

### A whole-tree registration pin, not selected by diff relevance

Some pins scan the entire tree and require every call site of a guarded shape to be
declared in a list — `test/test_link_screen_hold_pin.py` for screen-then-operate
TOCTOU sites, `website/electron/test/port-owner-self-asserted-identity.test.js` for
port-verdict sites. They are the right design: an unclassifiable site fails rather
than passing, so a new one cannot join quietly.

The trap is that `scripts/local-gate.py` selects tests by diff relevance, and a pin
that scans everything is relevant to a diff that names none of its files. So adding
a guarded call site passes the local gate, merges, and reds every pull request
opened afterwards.

When your change adds a call of a guarded shape — a link screen, a tree walk, an
`unlink`/`rmdir`, a port verdict — run the tree-scan pins explicitly. Registering a
site is one line, and the pin's own message says which list it belongs in; what it
will not do is guess.

### A dashboard turn is tested through the turn harness, not its source

A test of what a dashboard chat turn does runs it: `test/turn_harness.py`'s
`run_turn` drives the production `_run_chat` through the production
`spawn_guarded_turn` on a virtual clock against a scripted provider and returns a
`TurnRecord` (frames, persisted rows, the slot's window, audits, crew-log entries,
provider and session calls, hand-offs, the stop reason) to assert on. Do not read
`dashboard/chat_runner.py` or `dashboard/chat_turn/` as text, AST or
`inspect.getsource`: such a pin breaks when the code moves and stays green when the
behaviour regresses. `test/test_source_pin_budget.py` counts every test module's
reads of those files against a per-module `BUDGET` that may only fall, so a new read
fails and a change that removes one lowers its row in the same commit.

### A host probe that asks a different program than the one under test

A live test that picks its expected branch from a host probe ("this host can build
the sandbox, so the step must run; otherwise it must fail closed") is only as
deterministic as the probe's agreement with the program under test. A probe that
asks a DIFFERENT program the same question -- Crew's own launcher probe standing in
for codex's bubblewrap -- agrees on some runner images and not on others, and the
test then reds on every pull request routed to the image where they part.

The probe MUST make the same check the program under test makes, with the binary it
runs: its own preflight, its own binary lookup (`PATH` first, then the bundled copy,
if that is the program's order). A second probe may answer only where the program's
own check gives no verdict. Never chain the two so the stand-in can veto the real
one: a short-circuit on the stand-in's "no" skips the check that would have said
"yes". The kernel and its LSMs judge per binary and per profile (AppArmor attaches
a profile to a path), so one process's refusal does not predict another's.

MEASURED: `test/test_codex_inner_sandbox_approval.py` returned "no sandbox" whenever
`userns_available()` said no, without running codex's own bubblewrap preflight. On
the `ubuntu-26.04` image of `kirocrew-linux-4-core`, Crew's Python probe was refused
`unshare(CLONE_NEWNS)` (EPERM) while codex's sandbox ran `git status` and the
network step (exit 0, `NET-200`). The test expected a `bwrap:` refusal, so two
contract tests were red on every pull request that runner served. Codex's preflight
now decides; Crew's probe answers only when no bubblewrap is found or the preflight
fails some other way.

### A test that ASSERTS a host capability instead of gating on it

`assert supervisor.can_reap()` inside a test body says "this host supports pidfd".
That is not an assertion about the code under test, it is a claim about the runner —
and on a heterogeneous fleet it is false on some machines, so the test fails where
the capability is absent rather than standing aside.

Gate it. Probe through the REAL path first, while it is still present, and skip when
the host itself says no; assert only the thing the test is actually for. The skip
stays honest because it cannot trigger on the subject failing: in the pidfd case,
`pidfd_open(2)` is one syscall number, so a host that refuses it to `os` refuses it
to the ctypes fallback too, and a host that allows it to `os` while the fallback
fails is a genuine regression that must still red. A file that already has such a
marker — `reaping = pytest.mark.skipif(not supervisor.can_reap(), ...)` — is telling
you the precondition exists and this test forgot to use it.

**How to tell a capability gap from a real break, from the CI summary alone:** diff
the counts between a passing and a failing run of the same shard. The failing
`Backend Tests (3.12, 2)` read `22089 passed, 1030 skipped, 1 failed` against a
passing `22094 passed, 1026 skipped` — five fewer passes, four more skips, one
failure. The four extra skips ARE the capability-gated tests standing aside, which
proves the runner lacked the capability and the one failure was the test that forgot
to stand aside with them. No log spelunking required.

### A test that says "every host" but was never run on Windows

`test/test_app_script_cron_sibling_import.py` claimed its recorder cases run "on every
host". The command-cron case does not: on Windows `run_command_sandboxed` refuses every
command cron before it spawns (there is no POSIX shell, by design), so the recorder
stayed empty and the case failed with `AssertionError: []` on every Windows shard of
every pull request. It was not a flake: it failed the same way every time, locally too.

Two rules close it:

* When the product refuses a feature on some host, a test of that feature gates on the
  product's OWN refusal probe -- here `cron_script._resolve_command_shell() is None` --
  not on `sys.platform`. The skip then tracks the product: a host that starts running
  command crons is held to the pin again. Run the probe under the SAME patches the test
  body uses: unpatched, this one spawns through the real `wrap_argv`, which raises on a
  POSIX host with no sandbox backend, so the probe reads that host as shell-less and
  the skip stands the pin down on exactly the host it was written for.
* A test whose docstring or name says "every host" / "all platforms" is run on Windows
  before it merges: locally (`-n0` on one file is seconds) or by reading the
  `Backend Tests (Windows)` shards of its own round. A recorder or spy that asserts it
  saw a call is the shape to watch for: on a host that refuses early it sees nothing.

### A frozen list of a surface other branches keep growing

`test/test_context_composition_contract_facade.py` pins the full signature of
`ContextBuilder.build_message` as a string. A refactor branch recorded the list; a
feature branch cut from an older base added one keyword. Each was green alone; on main
together they were red on every platform and every later pull request inherited it.
Rerunning cannot fix it, which is how to tell it from a flake: the same assertion fails
on every head built on that main.

A snapshot of a hot surface pins what callers depend on, not every byte. The signature
tests compare through `_frozen_view`, which drops a NEW keyword-only parameter that has
a default (no caller can feel it) and keeps everything else -- a removed, renamed,
reordered or retyped parameter, a new positional or required one, a changed return
annotation -- red. `test_only_a_compatible_addition_is_tolerated` proves each of those
still reds. Use the same shape for any new pin over a surface that ordinary feature
work grows; a byte-exact pin is right only for a surface nobody else edits.

### An absolute count over a tree that grows

A prefilter-cost check that says "this filter keeps fewer than 907 files" is a
ratchet set one file above main: the next branch that adds any file containing the
literal reds, and its own diff holds nothing to fix. What such a check is FOR is
"the filter still narrows the tree", which is a share, not a count. Bound
`kept / total`, with the bar well above today's share and well below one, and pair
it with a negative control that runs a literal found in nearly every file and
asserts the widest bar refuses it -- that is the regression the check exists to
catch. `TestFiltersStillNarrowTheTree` in `test/test_source_corpus.py` has both.

### A CI wait that runs out is infrastructure, not a red test

A job that waits on another system (a gate run, the API quota) must size its wait
to what that system really takes, and must say so when the wait runs out. A fixed
ceiling shorter than a normal queue reports a green commit red, and every author
then hunts for a test that never failed. Give a wait that is visibly moving (a run
`pending` or `queued`, a known rate-limit reset) the long budget, give a wait with no
news the short one, back off between failed reads, and start the error with
`CI infrastructure, not a test failure` so the reader re-runs instead of debugging.
Never turn the wait into a pass: the barrier still fails closed. The model is
`.github/scripts/await-fast-gate.sh` ([ci-and-reviews](../../ci/ci-and-reviews.md),
`await-fast-gate`); `test/test_await_fast_gate_poll.py` drives both budgets. A
transient download error inside a third-party action (`Unexpected HTTP response:
504` from `setup-python`) is already retried by that action; a red after its
retries is an upstream outage, so re-run the job once.

## Keeping the suite fast

The measured runs above exceeded 100k tests. At that scale, setup overhead rather
than any single slow test is what dominates. Profile before optimizing:

```bash
# Per-test durations for the whole suite (writes a JSON map)
pytest -q -n auto --dist loadgroup --no-cov --store-durations --durations-path=/tmp/d.json
# One file, serially, with its own worst offenders
pytest test/test_foo.py -n0 -q --no-cov --durations=10
```

Note that `--store-durations` numbers taken under `-n auto` include worker contention
and overstate individual tests. Compare candidates **back to back** on the same machine
(run the change, then the base in `git worktree add --detach <dir> <base>`; never
`git stash`: every worktree shares one stash list); a number from an idle machine
measured an hour earlier is not a baseline.

### The three highest-leverage patterns

1. **Audit what the autouse fixtures cost, before anything else.** Every one of them is
   paid ~106k times, so a few milliseconds there outweighs any single slow test. Two
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
   cp test/conftest.py test/conftest_*.py /tmp/probe/ && cd /tmp/probe && pytest test_p.py -n0 -q --no-cov
   ```

   That probe read 6.35s with the unused `tmp_path` and the repeated `mktemp` calls in
   place, and 0.82s with them fixed: **9.2ms per test**, which is where most of the
   suite-wide win came from.
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

- **Setup that goes through a persisting helper.** A store method that saves on every
  call (several file writes plus a prune scan) turns a cap test that builds `CAP + 25`
  filler records through it into an O(n²) disk cost for state it only asserts on after
  the *final* `save()`. Build fixture records directly (a helper with the same record
  shape) and save once; a run of 47s became 0.2s.
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
- **A whole-tree walk inside one test is one test's timeout budget.** The per-test
  timeout bounds a test, not a walk, and a census that parses every test file that
  could patch a facade (`test_sandbox_refactor_facade.py`: ~870 files, ~50 MB) is
  more than one test's budget under coverage on a shared runner: 27 s idle and bare,
  50 s idle under `--cov` (the pure-Python visitor doubles, `ast.parse` does not), past
  120 s on an eight-vCPU shard beside four workers. Two fixes, neither touching an
  assertion. Read bindings from statements only: `ast.walk` visits every node, but an
  import, a def, a class and an assignment are statements, and the one expression that
  binds a name is a `NamedExpr`, so a source with no `:=` is read from its statement
  bodies -- a sixth of the nodes -- in `ast.walk`'s own breadth-first order, which
  matters because a name bound twice keeps the binding visited last. Then deal the
  files into size-balanced parts and run one parametrized test per part, with one
  whole-census test after them that reads the parts' caches: each test's work is a
  bounded share of the tree, the sum is unchanged, and in file order the final test
  costs nothing. The per-part check asserts `found <= listed`; only the whole-census
  one asserts equality, because a stale entry is visible only to the whole. Prove it
  with the planted-violation check below, on a part and on the whole.
- **The walk is usually three walks, and a gate that reads statements can skip the
  expressions.** The spawn census in `test_spawn_audit.py` ran `ast.walk` three times
  per file (imports, defs, calls) and then keyed each call to its function by comparing
  line ranges against every `def`; it sat at 120 s on shard 6 beside the facade census,
  as did the two composition contracts' patch censuses, whose alias fixpoint re-walked
  every node per round and whose patch scan walked every function and then the module.
  One walk per file in `ast.walk`'s own order, carrying the innermost enclosing `def`
  down to each call (a decorator belongs to the scope around the `def`), reads the
  same census from a third of the visits; and a gate that reads only imports, defs or
  assignments reads statements, so `source_corpus.iter_statements` -- the shared form
  of the statement walk above -- yields every statement from a sixth of the nodes, in
  the same breadth-first order. Both censuses then take the parts shape above, with the
  stale-entry and known-rebind checks reading the whole from the parts' caches.
- **A collection-time scan is paid by whoever imports the module.** A ratchet that
  resolves its parametrize population at module scope (`SPLITTING_MODULES =
  _modules_that_split()`) runs that scan at collection on its own shard, and again in
  the BODY of any test elsewhere that imports the module to drive one of its checks:
  `test_the_splitter_offload_ratchet_reads_the_owners` was 25 s idle and 117 s on CI,
  all of it the sibling's import-time `rglob` + `ast.parse` of the package. Narrow the
  scan to the files that can match (`source_corpus.parsed_candidates(require_any=...)`
  with the splitter names the pattern has to spell) and it is a dozen files; the
  general rule is that a module-level walk must cost a few files, not the tree, because
  nothing bounds where it runs.
- **A whole-tree scan that outgrows the suite timeout.** A slow scan takes 30-50 s of
  CPU on a fast host and about three times that on a hosted runner, past the
  suite-wide `--timeout=120`. In order of preference:
  1. Narrow it. Read the tree through `test/source_corpus.py`'s `parsed_candidates`
     with the literals a hit must spell (`require_all` / `require_any`), so only the
     files that can match are parsed. A narrowed scan is not a whole-tree scan.
     Those literals narrow a scan of names (identifiers, attributes, keywords), never
     a scan of `str` values: an escape (`\x31`), a backslash-newline or adjacent
     literals (`'1' '4'`) spell a value the source text does not hold, and the corpus
     matches NFKC-folded text, which Python applies to names only. A value scan
     parses every file and takes step 2, as `test/test_memory.py`'s history decay
     gate does.
  2. Otherwise put the module in its own `tree_scan_<module>` group (above).
     `test/conftest.py` gives every test in a `tree_scan` group that has no
     `pytest.mark.timeout` the group default, `TREE_SCAN_TIMEOUT_SECS` (300 s, eight
     times 37.5 s). Nothing else is needed. A timeout mark that names no seconds
     (`timeout(method="thread")`) takes the closest explicit value at any level (a
     module's `timeout(600)`), or the default when there is none.
  3. If the scan takes more than 37.5 s of CPU alone on a fast host (`-n 0`, the
     first caller of any cache), give it an explicit `pytest.mark.timeout` of at
     least eight times its cost. Five times its cost timed out on a loaded host.

  `test/test_tree_scan_budget.py` pins the hook. The hook reads the closest
  `xdist_group`, so a `tree_scan` group on one test gives that test alone the
  default. Nothing finds scans or measures cost, so the eight-times rule in step 3 is
  the author's job. A slow scan with no `tree_scan` group still runs at 120 s and
  nothing flags it: narrow it (step 1), which is the fix for the cost itself, or
  group it (step 2).

Neither of these shows up as a *failure*, which is why they survive: the suite is
green, just three times slower than it needs to be, and every timing-sensitive test
in the same shard inherits the load.

### Verify an optimization did not weaken the test

**Prefer the command.** `prove.py` in the `kirocrew-prepare-pr` skill does this for a whole
change and cannot cost you work: it reverts the change's production hunks inside a
throwaway git worktree, so your tree is never mutated and nothing needs restoring,
and it refuses to run while a file under proof carries uncommitted edits.

```bash
python3 src/kiro_crew/builtin_skills/kirocrew-dev/kirocrew-prepare-pr/scripts/prove.py
# 0 PROVEN · 20 NOT_PROVEN · 21 INCONCLUSIVE · 10 nothing to prove · 11 frontend tests · 30 baseline red
# it also prints the `Proof:` line a fix PR pastes under `## Tests`
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

`ci.yml` assigns the backend suite to eight whole-file shards on Linux and Windows
using `scripts.ci_file_shards`. Ownership is SHA-256 of the root-relative POSIX
path, not a duration or test-count balance. Other shards skip the file before
import, while discovery patterns and platform ignores remain pytest's own.

The Linux full-suite run adds `--file-shard-pins scripts/ci_file_shard_pins.txt`:
every file with a measured weight gets a fixed shard, placed so the eight shards'
summed test seconds come out even, and a file with no weight keeps its hash
owner. A pin only changes which shard owns a file, so a new or renamed file is
still collected, on its hash shard. `scripts/ci_file_shard_pins.py --every-file`
regenerates the pin file from the `linux-pytest-progress-<N>` artifacts of
several green `main` runs; regenerate it when the Linux shards' `Run tests` times
drift apart again. Without `--every-file` the script pins the 800 heaviest files
and hashes the rest, and the hashed rest is uneven: planned from 11 green runs
and replayed on the one held out (12 runs, 2026-11-18), the heaviest Linux shard
carried a median 1.07x the mean test seconds with 800 pins and 1.02x with every
file planned. The Windows run adds its own pin file, `--file-shard-pins
scripts/ci_file_shard_pins_windows.txt`, generated with `--platform windows`
from the `windows-pytest-progress-<N>` artifacts. Windows keeps a separate file
because a file's share of the time differs there: the Linux pins cut the slowest
Windows shard by a median 12% of its summed test seconds over six held-out green
runs, the Windows pins by 20%. The Windows file stays at the 800 heaviest
(1.06x held out, against 1.02x with every file) because the Windows leaf scope
in `ci.yml` reads that file as its list of heavy files (`WINDOWS_LEAF_MAX_PINNED`);
listing every file there would send any leaf diff of five or more files to the
full suite. Regenerate the Windows file when its shards drift apart: on 10 green
`main` runs of 2026-11-19 and 2026-11-20, the file generated on 2026-11-19 left the
heaviest Windows shard at a median 1.18x the mean test seconds, and a file planned
from 9 of those runs and replayed on the one held out at 1.06x (#39955); no file
outside the 800 took more than 30 s in any of them. macOS, release and the
reduced and leaf scopes keep pure hash ownership.

Before the Linux and Windows full-suite test step, `scripts/ci_rewrite_prewarm.py`
writes pytest's assertion-rewrite pyc for the shard's test files (same hash and
pin file) and every `conftest.py` loaded during collection, in parallel and without importing them, under
the `sys.pycache_prefix` the rootdir conftest sets. It changes where a worker's
collection time goes, not which files the shard collects, and never fails the
job. `test/test_ci_rewrite_prewarm.py` owns the proof that pytest reads those
pycs; a change to the cache root in `test/conftest_floor_configure.py` must keep
`ci_rewrite_prewarm.pycache_prefix` equal to it.

macOS assigns whole files like the other platforms, in eight shards. The shard
count is the knob to reach for when the suite grows, not the 60-minute cap,
because that cap is a spend guard as much as a hang guard on a lane billed at ten
times Linux: a higher cap bounds a runaway more loosely, while a higher count is
what divides the work. The cap sits at 60 rather than at the shards' usual 20-30
minutes because a macOS arm64 runner that is slow or short of capacity can push a
shard past 40 minutes with no failing test. The macOS shards run an explicit `-n 2` (`platform-tests.yml`), which
bypasses the xdist memory budget behind `-n auto`. `test-durations.yml` still
invokes pytest-split to RECORD `.test_durations`, and three tests load its plugin
directly, but no sharding lane reads that file, so the recording balances
nothing.

**Measure a shard by running it, not by summing durations.** Per-test times from
`--store-durations` include worker contention and do not add up to shard wall time.
A bounded local reproduction of file shard `<N>` is:

```bash
python -m pytest -q -n 2 --dist loadgroup --no-cov \
  -p scripts.ci_file_shards --file-shards 8 --file-shard <N>
```

Keep CI's selectors, ignores and coverage settings when comparing actual CI runs.
A hash partition does not promise equal runtime: one slow file is indivisible,
and shared conftest/package imports still cost every worker. Use collection-phase
progress and completed shard timings; measurements from item-split runs do not
establish the balance of file shards. Fix measured test outliers rather than
assuming a duration file changes this partition.

## Real-machine verification

A pull request has passed only when a real machine shows three things: the bug is
real, the fix fixes it, and the machine is gone afterwards. Unit tests and a green
CI run are necessary, not sufficient: they run the code the author thought to test,
in the environment CI happens to have. Every PR that changes what runs (product code,
tests, test tooling, scripts) carries a `## Real-machine verification` section in its
body. A change that runs nothing (docs, comments, the PR template) writes
`N/A -- <what in the diff runs nothing>` there. A body without the section has not
passed review.

### The loop

1. **Get a machine** (below). Note its OS, version, architecture, instance type or
   model, CPU count and memory.
2. **Reproduce on the base first.** Check out `origin/main` (the PR's base sha) on the
   machine and trigger the bug with one command. Record the command and its output.
   A flaky test is reproduced the way *Proving a determinism fix* says: the forced
   condition red on the base, plus the repeats; give the command and the counts.
   **A bug you cannot reproduce is not a confirmed bug.** Keep diagnosing (CI history,
   logs, the forced condition) before you write a fix, and say in the PR what you could
   and could not reproduce. For a feature, the base run shows the missing behaviour.
3. **Open the PR.**
4. **While CI runs, verify the PR head** on the same machine with the same command. A
   flaky fix shows the forced condition green on the head and the same repeats.
   Re-verify when a later push changes what runs.
5. **Clean up** (below) and record the result.
6. **Write the section**:

   ```markdown
   ## Real-machine verification

   Machine: EC2 m7i.xlarge, Ubuntu 24.04 x86_64, 4 vCPU / 16 GiB (or: author's MacBook, macOS 15.1 arm64)
   Before (main <sha12>): `<command>` -> <the failing output, excerpted>
   After (head <sha12>): `<command>` -> <the passing output>
   Cleanup: instance terminated, security group deleted, no key pair, no volumes left (describe output empty)
   ```

### Which machine

| PR | Machine |
|---|---|
| A maintainer's PR | An EC2 instance in the maintainer's own AWS account, set up as below |
| An outside contributor's PR | A maintainer asks, on the PR, for a role in the contributor's AWS account to launch the verification machine; best is Linux, macOS and Windows, at least Linux |
| No machine available | The author reproduces on their own computer and pastes the commands and outputs, before and after, into the section |

Match the machine to the change: a Linux test fix runs on Linux, a Windows path fix
on Windows. Linux x86_64 is the floor for any change that runs on every OS. EC2 macOS
needs a Dedicated Host with a 24-hour minimum allocation, so a macOS-only change is
usually verified on the author's own Mac. Windows uses the
`/aws/service/ami-windows-latest/Windows_Server-2022-English-Full-Base` image, and its
self-terminating user data is PowerShell, not `/bin/sh`:
`--user-data '<powershell>shutdown /s /t 14400</powershell>'`.

### Setting up an EC2 verification machine

The machine is private, short-lived, tagged and capped. Every one of these holds:

- **No inbound access.** A security group of its own with **no ingress rules**; you
  connect through SSM Session Manager (`aws ssm start-session --target <id>`). If SSH is
  unavoidable, one ingress rule for port 22 from your own `/32`, never `0.0.0.0/0`,
  and a key pair made for this machine. Nothing the tests start is published: a
  server under test binds `127.0.0.1`, and you reach it through the SSM session or an
  SSH local port-forward.
- **Least privilege.** The instance profile allows SSM only
  (`AmazonSSMManagedInstanceCore`). The machine holds no other credentials.
- **Self-terminating.** `--instance-initiated-shutdown-behavior terminate`, and user
  data that shuts the machine down after 4 hours (`shutdown -h +240` on Linux, the
  PowerShell line above on Windows), so a forgotten machine dies within 4 hours.
- **Tagged**: `Purpose=kc-pr-verify`, `Pr=<n>`, `Owner=<you>`, `ExpiresAt=<ISO time>`,
  on the instance and its volumes, so cleanup finds everything by tag.
- **Capped spend.** Pick the smallest type that matches CI (4 vCPU / 16 GiB for a
  test run), use a larger one only for a full-suite run, and stop at the 4-hour cap.
  Record the hours and the on-demand cost in the section.
- **IMDSv2 only** (`HttpTokens=required`).

```bash
TAG=kc-pr-verify; PR=<n>; OWNER=<you>
END=$(python3 -c "import datetime as d; print((d.datetime.now(d.timezone.utc) + d.timedelta(hours=4)).strftime('%Y-%m-%dT%H:%MZ'))")
SG=$(aws ec2 create-security-group --group-name "$TAG-$PR" \
  --description "Kiro Crew PR $PR verification, no ingress" --vpc-id "$VPC" \
  --query GroupId --output text)
ID=$(aws ec2 run-instances --instance-type m7i.xlarge \
  --image-id resolve:ssm:/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --subnet-id "$SUBNET" --security-group-ids "$SG" \
  --iam-instance-profile Name="$SSM_ONLY_PROFILE" \
  --metadata-options HttpTokens=required \
  --instance-initiated-shutdown-behavior terminate \
  --user-data $'#!/bin/sh\nshutdown -h +240' \
  --tag-specifications \
    "ResourceType=instance,Tags=[{Key=Purpose,Value=$TAG},{Key=Pr,Value=$PR},{Key=Owner,Value=$OWNER},{Key=ExpiresAt,Value=$END}]" \
    "ResourceType=volume,Tags=[{Key=Purpose,Value=$TAG},{Key=Pr,Value=$PR},{Key=Owner,Value=$OWNER},{Key=ExpiresAt,Value=$END}]" \
  --query 'Instances[0].InstanceId' --output text)
```

Prefer a private subnet with a NAT gateway or SSM endpoints. A public IP is acceptable
only for outbound traffic, with the security group still admitting nothing.

### Cleaning up

```bash
aws ec2 terminate-instances --instance-ids "$ID"
aws ec2 wait instance-terminated --instance-ids "$ID"
aws ec2 delete-security-group --group-id "$SG"
# Both must print []:
aws ec2 describe-instances --filters Name=tag:Pr,Values="$PR" Name=tag:Purpose,Values="$TAG" \
  Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].InstanceId'
aws ec2 describe-volumes --filters Name=tag:Pr,Values="$PR" Name=tag:Purpose,Values="$TAG" \
  --query 'Volumes[].VolumeId'
```

Delete any key pair you made (`aws ec2 delete-key-pair`) and its local file. Paste the
two empty results into the section. Delete only resources you created for this PR.

### An outside contributor's account

- Ask for a **role**, never for access keys: an IAM role in the contributor's account
  whose trust policy names the maintainer's account and an `ExternalId`, limited to
  launching, tagging, describing and terminating EC2 instances, creating and deleting
  security groups, starting SSM sessions, and `iam:PassRole` on one SSM-only instance
  profile role the contributor creates, in one region, with a maximum session of 1 hour.
  The role ARN can be posted on the PR; it is not a secret.
- The account is used **only to verify this PR**, and the contributor pays for the
  machine, so keep it to the 4-hour cap and the smallest type. Every machine and
  security group made in it is deleted when verification ends, the PR says so, and the
  contributor then deletes the role.
- **Credentials never enter a file, commit, PR, comment, log or chat.** Role sessions
  stay in the shell's environment and expire. If a contributor posts a key anywhere,
  do not use it: tell them to deactivate and rotate it.
- When a contributor cannot provide an account, they verify on their own machine and
  paste the commands and outputs, before and after, into the section.

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
import kiro_crew

async def main():
    kiro = await asyncio.create_subprocess_exec(
        "kiro-cli", "acp", "--agent", "kirocrew",  # --agent must name an installed agent
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
    # Match production: acp.client sends PROTOCOL_VERSION, ACP_CLIENT_CAPABILITIES and
    # clientInfo (CLIENT_NAME and the package __version__).
    await wait_response(await send("initialize", {
        "protocolVersion": "2025-08-22",
        "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        },
        "clientInfo": {"name": "kirocrew", "version": kiro_crew.__version__},
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
find build/ -name "SOURCES.txt" -exec grep "<symbol-you-removed>" {} +

# Verify fix: same sequence must pass consistently
rm -rf build/ && pip install -e . && pip install -e . && pip install -e .
```

### Key Principles

- **Observe before fixing** — capture raw data, don't guess
- **Reproduce reliably** — if you can't trigger it on demand, you can't verify the fix
- **Test the exact flow** — simulate what the real code does (same process, same protocol, same ordering)
- **Verify N times** — flaky issues need multiple runs to confirm (3+ consecutive passes)
- **Keep test scripts** — save in `/tmp/test_*.py` during debugging, discard after fix is verified
