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

Session-switch lock registries are reset per test. Reused fixture session keys
must not retain a contended lock tied to another test's event loop.
The test floor clears live session and run execution records between tests so a
reused temporary home cannot inherit another test's routing or retention mode.
Dispatch doubles provide a concrete `get_agent_selection()` tuple, including
`("template", "")` for the default template. An unconstrained mock is not a
valid member or template identity. Session context doubles implement the async
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

Do not factor these into a shared helper. The seam differs in kind every time (a SQLite
write lock, an asyncio task reaching an attributable state, a multi-phase uninstall's
phase 1, a dependency coordinator's park, a policy-file publish), anything spanning them
is a thin wrapper over `threading.Event` / `asyncio.Event` plus `try`/`finally` behind an
indirection every reader must learn, and the seam form holds nothing at all, so a hold
helper would not even cover it.

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
`kill_pid_async`, `kill_process_tree`, `kill_process_tree_pinned`,
`kill_process_tree_async`, `kill_process_group` and `kill_and_reap`. Pick one
of the two spellings already in the tree rather than inventing a third: give the
mock a pid above
every supported platform's `pid_max`
(`test/test_update_provider.py::_UNALLOCATABLE_PID`), or neutralise the killer
and assert the ordering instead
(`test/test_platform_compat.py::TestKillAndReap`). This is a convention rather
than a gate because one file does not carry the answer: the neutralising patch
may sit in a class fixture or a conftest, and the killer is as often replaced one
level down (`os.kill`, `os.killpg`) or behind a module's own private helper, so
no per-file rule can tell a covered mock from an exposed one.

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

### Host tool dialects

A test double for a platform-specific CLI must not depend on another host's
flags. The BSD `stat -f %Lp` fixture reads real permission bits with Python's
`os.stat` and `stat.S_IMODE`, rather than delegating to GNU `stat -c %a`.
Reject unsupported arguments and verify actual modes; never substitute a fixed
successful response for the permission check. Quote paths and bound subprocesses.

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

### Transport readiness before event assertions

Before triggering a broadcast, a WebSocket test waits for each connection's
initial `slots` frame. `ws_connect()` completes the HTTP upgrade, while `api_ws`
can still be awaiting allowlist and app-scope loading before `register_ws()`.
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
  globals through `monkeypatch`, never raw assignment.

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
(`_body`, `_headers`, `status`, `reason`, `request_info`, `history`, `release`).
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

## Which conftest you are standing on

There are **two** testpaths (`setup.cfg`'s `testpaths = test
src/kiro_crew/apps/builtins`) and they do **not** get the same fixtures. Know which
floor is under your file before you decide what to isolate yourself:

| Your test lives in | It inherits |
|---|---|
| `test/` | the rootdir `conftest.py` **and** `test/conftest.py` |
| `src/kiro_crew/apps/builtins/*/tests/` | the rootdir `conftest.py`, plus that app's own `tests/conftest.py` where one exists (currently `auto_improvement`, `code_review_sage`, and `spec_builder`) |

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
`KIROCREW_KIRO_BIN` pointing at the fake backend). `gateway_boot` binds the
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
`get_json`/`post_json` assert the status and decode. `auth=False` proves the
denied side of a contract. `gw.state` is the live `DashboardState`, `gw.app`
the real `web.Application`, `gw.home` the data home -- use them to assert on
what a request left behind, not to bypass the request.

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
and `_shutdown()` awaited directly, and the teardown reports it. The serve/stop
seam on `GatewayOrchestrator` (issue #13627) would let an in-process caller
skip the interception altogether; until it lands, a change to the exit path
that trips the pin is a change that needs the harness updated in the same PR.
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
deadline fails the test by name); and a **reap** of every
asyncio task the boot added that `_shutdown()` left running (production leaves
those to `os._exit`), failing the test by name if one ignores cancellation.
The lists are kept honest by
`test_boot_smoke.py::test_a_second_boot_touches_only_known_module_globals`:
it diffs every loaded `kiro_crew` module's globals across a second boot, and a
changed name that is neither restored by the harness nor listed with a reason
in `_KNOWN_SECOND_BOOT_CHANGES` fails there. When startup grows a home-derived
global, that test names it; put it on the reset list, or on the known list
with its reason, in the same PR.

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
  `src/kiro_crew/apps/builtins`) pins `$XDG_CONFIG_HOME` to a tmp
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

  **macOS uses the same list mechanism, not a second one.**
  `test/macos-expected-failures.txt` is applied by the same rootdir
  `_apply_tracked_gap_list` matcher, with the same plain-node-id spelling and the same
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
  interpreter's own directory first on `PATH` (`test_security_conductor_scripts.runnable_python`
  is the shape) or resolve the tool to its real executable before building the env. Do
  NOT drop the `HOME` substitution to fix it — that is usually a blast-radius bound
  somebody chose on purpose.

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
  with Node 18 while the repo declares `engines.node >= 22`. Gate on the floor the code
  under test actually needs, and probe it the way the tests will experience it.

- **If you stub the only thing that releases a resource, the fixture owes the release.**
  A permit, an in-flight claim, a lock: when the green-path test replaces the runner
  whose `finally` gives it back, the resource is gone for the life of the worker. The
  victim is whichever later test asserts on capacity — it passes for the wrong reason,
  or waits for a permit that will never come and takes the worker with it. Restore to
  what the test INHERITED, not to a pristine value, so one leak is not re-reported
  against every test after it. In production code, treat everything between acquiring a
  resource and entering the `try` that releases it as a leak window.

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
  streams file text and caches only the tree's path list, and exposes `_clear_caches()`
  for a module-scoped teardown; it used to memoise ~160 MB of source text for the life of
  the worker, paid by every later test on it (see the tenth pass). And mark the module
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
  `slack.handler`, which built `_PHASE_EMOJIS` by calling `KiroCrewConfig.load()` at import —
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
  `IMPORT_TIME_METRIC_EMITTERS`, resets the recorder for the next module, and fails
  that module's collection report. Pytest/xdist therefore fails the job on every
  file shard, independently of where `TestNoMetricIsEmittedAtImport` runs. That
  test also asserts the record is empty. Build such values inside the test or fixture.
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

### What the fourth five-run pass found (Linux, 106k tests per run)

Five full backend runs plus five frontend runs on a 32-core Linux host, from inside a
Kiro Crew agent session, with an in-process audit hook attributing every write, spawn,
connect and kill to a test and a per-test census of duration, RSS, threads and
descriptors. **Zero flaky tests across 5 × 106,199** — every failure was identical in
all five runs. That is the headline, and it changes where the value of a pass like this
comes from: the flakes are gone, so what is left is (a) tests that fail on a developer's
host and cannot fail on CI, (b) side effects that outlive the run, and (c) cost. All
three below.

The first finding was visible before a single test ran, and it is the shape worth
carrying forward:

- **A version-manager shim plus a repointed `HOME` is an immortal spinning process.**
  Nine `python3` processes were found reparented to init, spinning at **1464% CPU
  between them (14.6 cores) for six days**, left by four separate earlier runs of
  `test_security_conductor_scripts.py`. Each had a deleted
  `pytest-of-*/garbage-*/scratch-checkout` cwd, so nothing on the machine could name
  what it belonged to. Killing them moved the host's load average from 21 to 9.6 — i.e.
  the "slow machine" a developer blames the suite for was the suite's own leftovers.

  The mechanism is the interaction of two individually-correct decisions in
  `verify_finding.child_env`: PATH is inherited (a proof needs an interpreter) while
  HOME is repointed at a throwaway worktree (the blast-radius bound). On any host whose
  PATH leads with a shim directory — mise, asdf, pyenv, volta, nodenv — the bare name
  `python3` IS the manager, and a manager that cannot find its tool state under the
  substituted HOME never execs an interpreter at all. It spins. Reproduced in 20
  seconds:

  ```bash
  env -i PATH=~/.local/share/mise/shims:/usr/bin:/bin HOME=<empty dir> \
      python3 -c "raise SystemExit(3)"     # hangs; SIGKILLed at a 20s external timeout
  ```

  Two fixes, and both are needed. **The reap must take the process GROUP** — a proof is
  routinely a wrapper that forks, so `Popen.kill()` reaps the wrapper and leaves the
  spinning half; `run_poc` now starts the child in its own session and `reap` uses one
  `killpg` (POSIX) or `taskkill /T` (Windows), which is what
  `test/installer_test_helpers.run_bounded` already did one layer up. **And a test that
  lets a proof actually RUN pins the interpreter's own directory first on PATH**
  (`runnable_python`). It cannot be fixed by spelling `sys.executable` in the proof
  itself: the verifier refuses a proof whose argv names an absolute path outside the
  worktree, and that refusal is correct and stays.

  The same class was then found a second time, independently, at
  `test_symbols_manifest_contract.py`: its helpers build a 4-key child env with
  `dirname(which("node"))` on PATH and `HOME=tmp_path`, so on a node-from-a-manager
  host `bash` blocks forever inside `$(node -e ...)`. There it is worse than an orphan —
  pytest-timeout's SIGALRM fires *before* `subprocess.run`'s own deadline, `Popen.__exit__`
  then calls `wait()` on a bash that never returns, and **the xdist worker blocks
  forever: a lost run** (class 6), not eleven timing-out tests. **When a test fabricates
  a child environment, HOME and PATH are a PAIR.** Substituting one while inheriting the
  other is the defect, whichever way round.

- **A capability probe must observe the tool's VERSION, not just its presence.**
  `requires_shell_and_node` gated on `shutil.which("node") is None`. `scripts/emit-symbols-manifest.mjs`
  uses `import.meta.dirname`, **undefined before Node 20.11**, so `path.resolve(undefined, "..")`
  raises `ERR_INVALID_ARG_TYPE` and two tests failed in all five runs on a host whose
  PATH led with Node 18 — while the repo declares `.node-version` = 24 and
  `engines.node >= 22`. This is the rule already stated for config ("a `skipif` helper
  must observe what the tests will observe") extended to a version floor. Same shape as
  the per-user-install resolvers below: presence is not capability.

- **A repo-root walker must prune `.worktrees/`, which is another BRANCH'S checkout.**
  `.worktrees/` is gitignored and is where this repo's own documented worktree workflow
  puts sibling checkouts. Two filesystem walkers rooted at the repo root did not prune
  it, so they audited code that is not on this branch and reported offenders nobody on
  this branch can fix — one failure quoted **this very file's docstring, from another
  branch**. Three tests failed in all five runs. `_shell_scripts()` one function below
  the worst offender never had the problem because it asks `git ls-files` instead of
  walking, and says so. CI has no `.worktrees/`, so CI is green and only the developer
  sees it. Closed by generalising that helper rather than by extending a skip list:
  `source_corpus.repo_files_named` asks git, so every ignored tree is out of scope by
  construction and no future one needs naming. Prefer it; if you must walk the
  filesystem, prune `.worktrees` with `node_modules`, `.venv`, `build`, `dist` and
  `.git`.

- **A permit released only in a patched-away runner's `finally` is leaked forever.**
  `api_hooks_agent` acquires `_hook_semaphore` and claims a key in
  `_hook_inflight_sessions`; both are returned only by `_run_hook_agent`'s `finally`,
  which the green-path tests replace with a no-op. Every run of
  `test_webhooks_event_loop.py` therefore dropped the worker's permits 6 → 5 for good
  and stranded `hook:x`. The victim is whichever later test asserts on capacity: a 429
  `capacity_reached` test passes for the wrong reason, and `test_webhooks_api.py`'s
  gather-all-permits test **hangs to the 120s timeout and takes the worker with it**.
  When you stub the only thing that releases a resource, the fixture owes the release —
  restored to what the test INHERITED, not to a pristine value.

  Auditing that also turned up the production half: `_run_hook_agent` loaded its saved
  context *before* the `try` whose `finally` releases both, so a corrupt `hooks.json` or
  a cancellation during that await wedged the live gateway at 429/409 until restart.
  **Everything between acquiring a resource and the `try` that releases it is a leak
  window.**

- **A production timeout the test never asserts on is paid in full, ~11 times over.**
  Eleven `test_slack_gateway.py::TestAutoApplyUpdate*` tests measured **30.02–30.16s
  each, identically in all five runs**: `_auto_apply_update` awaits
  `_drain_update_callback_work(timeout=30.0)`, nothing in those tests makes the drain
  condition true, and it polls at 10ms to the deadline. That is **~330s of pure sleeping
  per run**, and the verdict after waiting is the same one the tests already assert. The
  fix is doc pattern 3 with one twist worth copying: the literal became a named class
  constant so the tests about the *sequence* can shorten it to 0, while the value itself
  stays pinned by the one test that is ABOUT it (which asserts `drain:30.0`). The whole
  file went from ~350s to 22s.

- **The memory model went stale under the suite, and the per-worker reservation with
  it.** Remeasured: the collection floor is **1,499 MiB for 106,491 items**, against the
  ~747 MiB / ~57,000 the budget's own comment justified `_GIB_PER_WORKER = 2` with. Per-test
  VmRSS sampling across 60 worker-runs at `-n 12` read **min 1,879 / median 2,042 / max
  2,771 MiB** — the median already at the 2,048 MiB reservation and the max 35% past it,
  *at the parallelism where the footprint is smallest*. The max is not noise: the same
  worker slot hit 2,771 MiB in all five runs, because the `tree_scan_*` groups land
  together and one alone retains ~1.3 GiB of parsed source. `_GIB_PER_WORKER` is
  therefore 3. **Re-derive both halves of that model whenever the suite grows by half
  again**; the cheap way is `--collect-only -n0`, which still reproduces a worker's
  collection peak.

Two instrument lessons, because both misled this pass before they were caught:

- **A duration measured with `time.monotonic()` is meaningless for a test that fakes the
  clock.** The census reported 185,075s for one test and 10,800s for four others against
  a 960s run — they advance a fake clock and the sampler read it. Cross-check any
  per-test timing against pytest's own `--durations`.
- **An env-delta census in a plain `pytest_runtest_teardown` hook reports the FLOOR's own
  undo as a leak.** Fixture finalizers had already run, so `KIROCREW_HOME` and the git
  identity pins read as "removed" on ~30 tests that leak nothing. The rootdir conftest's
  teardown hook is `tryfirst` for exactly this reason; a census hook must be too.

Two things that look like findings and are not, recorded so the next pass does not
re-litigate them: `socket.connect` to `198.51.100.1` / `2001:db8::1` is the RFC 5737 /
RFC 3849 local-IP-discovery trick that replaced round one's `8.8.8.8` — no packet leaves
the host; and `os.kill(pid, 0)` against pids the worker never spawned is
`platform_compat.pid_exists`, which is POSIX-only by construction and uses
`OpenProcess` on Windows.

**One finding is left OPEN on purpose, and the reason generalises.**
`test_playwright_cli_installer.py::test_an_interrupted_rebootstrap_restores_the_previous_node`
sleeps 2.5s over a 60 MB incompressible tarball hoping to catch `tar` mid-unpack, and it
never enters that window — so it costs ~1.5s of CPU and ~300 MB of temp I/O a run while
both its assertions are satisfied by a prefix nothing modified. It is a *vacuous* test,
i.e. flake class "coverage that only looks like coverage" wearing a cost problem's
clothes. A rewrite that names the promotion window with an `mv` stub was written, proven
non-vacuous by mutation (deleting the installer's restore-on-interrupt fails it by name),
and then **reverted**: it passes at `-n0` and fails under the full suite, where the
interrupt does not land in the window. Two lessons, both worth more than the fix would
have been:

- **A test that passes alone is not verified.** Only the full run distinguished these.
- **Making a vacuous test real can expose what the vacuity was hiding.** Reaching further
  into the installer than the original ever did also reached an install step whose `npm`
  that test had never needed to stub. When you fix a test that never reached its subject,
  re-check every stub the newly-reached path requires.

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

### What a fifth five-run pass found (macOS, uv venv, ~106k tests per run)

The measurement was repeated on a macOS host whose virtualenv was created by `uv` (so
it carries **no `pip` module**) and whose base interpreter is a relocatable
python-build-standalone build, from inside an agent session, with the audit hook and the
per-test census above plus a warnings census. Five backend runs of 105,536 tests each:
ten deterministic failures on every run, one flake in five, zero timeouts, zero worker
crashes; five vitest runs (33,722) and four Electron runs (1,901) with no failure at all
— the one red Electron run had no `website/electron/node_modules`, which `npm ci` in
`website/` does not install (`npm ci --ignore-scripts` in `website/electron/` does).
The suite was also emitting ~9,250 warnings per run, and mining them turned up four
classes the residue and failure signals never would have.

The ten failures were the venv, not the code:

- **A fixture venv built with `venv.create()` copies the binary, and a copied
  python-build-standalone binary does not start.** The API's default is
  `symlinks=False`; the copied `.venv/bin/python3` then resolves
  `@rpath/libpython3.12.dylib` relative to its own location and aborts under dyld. The
  interpreter resolver's usability probe fails closed and production falls back to
  `sys.executable`, so `resolve_app_python` and `venv_provided_command` both "prefer" the
  running interpreter. `python -m venv` — where real venvs come from — uses symlinks on
  POSIX. Build fixture venvs with `symlinks=not IS_WINDOWS`
  (`test_apps_backend_coverage._create_real_venv`).
- **A probe of the RUNNING interpreter's packaging.** `_run_app_build` decides whether
  to plan a `pip install` with `importlib.util.find_spec("pip")`, which is `None` in a uv
  venv, so every build-planning test captured an empty command list. Already a documented
  class (§ Coverage that only looks like coverage); it now has a fixture: pin the probe
  truthy with a `ModuleSpec("pip", loader=None)` for the planning branch and falsy for
  the skip branch, never leave it to the host.
- **A fake `subprocess.run` that routes by SUBSTRING of the joined argv is
  host-dependent.** The Linux service test's responder matched `"restart" in " ".join(argv)`;
  the unit-file write's argv carries a `mkstemp` path under `TMPDIR`, and in
  `KIROCREW_TMP_PER_TEST` mode that directory is named after the test's own nodeid —
  which contains "restart". The fake returned the restart failure at the write step and
  the branch under test was never reached (the `..._at_enable` sibling had the same
  exposure). Route on whole tokens, `needle in argv_list`; a path element can contain any
  word.
- **A test that drives a tool against the REAL checkout asserts one branch's wording.**
  `test_dry_run_against_the_real_repo_never_plans_a_full_suite` required "deferred to
  CI", which `local-gate.py` prints only for a dirty checkout; a clean one takes the
  no-diff branch. The invariant is "never `(full)`" and holds on both; assert that, and
  accept either branch's wording.

The one flake was two defects a shared directory connected:

- **A `0o555` directory under `tmp_path` with no finalizer, and a nested pytest on the
  default basetemp.** `test_removing_owner_write_still_diverges` chmods a directory of
  the installed copy and never restores it, so pytest's `rm_rf` cannot delete that
  `tmp_path` and renames it into the shared `/tmp/pytest-of-<user>/garbage-<uuid>/`.
  `test_xdist_escaped_failure_guard` spawns a nested pytest that used the DEFAULT
  basetemp — the same shared tree — whose startup prune tripped over that garbage and
  printed `(rm_rf) error removing ...`; the guard's `assert "error" not in out` failed,
  once in five runs, on a test in another file. Both halves are fixed: every fixture
  that chmods under `tmp_path` restores owner rwx top-down in a finalizer
  (`_restore_owner_rwx`), and a nested pytest ALWAYS gets `--basetemp` under the outer
  test's `tmp_path` so its output describes only its own run. Isolation is the fix; the
  assertion was not weakened.

Host writes and leaks the earlier passes did not have the hooks to see:

- **A platform default that is not HOME-derived cannot be relocated by faking HOME.**
  `workspace_root()` on macOS defaults to `/Volumes/workplace` — the operator's real
  workspace — and `_resolve_workspace_root()` mkdirs it. `delenv("KIROCREW_WORKSPACE")`
  to test the default therefore created the operator's directory on every run
  (invisible because it already existed). Patch the resolver's default in the namespace
  the caller reads (`loader._default_workspace_base`) and assert the result lands under
  `tmp_path`.
- **A module-scoped fixture holding a `MonkeyPatch` over env leaks across the FIRST
  test that requests it and the LAST test the module runs on each worker**, so the
  attributed test differs per run. `test_otlp_wire_e2e`'s `exported` fixture held
  `KIROCREW_HOME` and `KIROCREW_TELEMETRY` for the module's lifetime although nothing
  downstream read them. When the shared value is fully built by the fixture body, scope
  the patches to the build with `pytest.MonkeyPatch.context()` and return the value.
- **A session-scoped autouse fixture that writes `os.environ` directly leaks for the
  life of the worker.** The auto_improvement app conftest set `GIT_AUTHOR_*` /
  `GIT_COMMITTER_*` with `os.environ.setdefault` so its `git commit` subprocesses had an
  identity; every later suite on that worker inherited them. A session-lifetime
  `pytest.MonkeyPatch.context()` is not the fix either: it undoes the keys at session
  end, but every later suite on the worker still runs with them set. An env value a
  subprocess must see is set per test through the function-scoped `monkeypatch`
  (`monkeypatch.setenv`, only when the key is absent), so it is undone with the test.
- **A trusted-directory resolver that finds a real per-user tool turns a unit test
  into a host-toolchain test.** `test_md_notebook` reached `_find_gh` → the host's `gh`
  ~154 times per run (`gh auth token`: install- and login-dependent, and it hands the
  code under test a LIVE token); `test_acp_client` reached `mise` through
  `_resolve_claude_code_executable`. Pin the resolver at the seam production reads (its
  env override `MD_NOTEBOOK_GH_BIN`, or the resolving function), never the spawn.
- **A "packet-less" UDP `connect` is still an off-loopback network touch.** The first
  `is_denied("ssh ...")` on a worker ran the own-interface probe, which connects a
  datagram socket to a TEST-NET address to read the local interface; the routing table
  answers it, and it also starts the DNS worker thread. Stub it with a `socket.socket`
  subclass whose datagram `connect` is inert and whose `getsockname` reports loopback,
  scoped to the tests that seed the verdict cache, so the real enumeration still runs in
  its own test.
- **Stubbing a runner that owns deferred cleanup also stubs away the cleanup.**
  `worktree_ops` registers its `mkdtemp` snapshot and runner directories in
  `cleanup_paths` and hands them to `_start_run`, whose `finally` removes them; eight
  `test_dev_fleet_app` tests replaced `_start_run` with a bare `AsyncMock` and 15
  `kirocrew-sync-runner-*` / `kirocrew-npm-preflight-*` directories outlived every run.
  Production was correct. A stub for a runner that owns cleanup reaps `cleanup_paths`
  itself (`_start_run_stub`).
- **A file the code guards with a sidecar must live under `tmp_path`.** Config tests
  wrote through `NamedTemporaryFile(delete=False)` in the temp root and unlinked only the
  JSON; the loader's `<config>.lock` sidecar outlived the test. Seed the file at
  `tmp_path / "config.json"` and the sidecar dies with the directory.
- **A spawned external binary writes its own logs and telemetry into the inherited
  `TMPDIR`.** The real `kiro-cli` left `kiro-log/` and `toolbox-telemetry-emf*` per run.
  Pass `TMPDIR`/`TMP`/`TEMP` pointing inside the test's own temp tree in the child's
  `env`, the way the child's `cwd` already is.
- **A subsystem whose only close is process exit leaks one handle per start on the
  worker.** `start_dashboard` opens the loop-stall crash-dump descriptor through a
  wrapper whose `close()` is a deliberate no-op, and the knowledge store's SQLite
  connections are per-thread with no enumeration; every dashboard-starting test added a
  constant +8 descriptors. The harness teardown (`_release_process_handles`) now stops
  the watchdog, `os.close`s the dump fd and closes the store's calling-thread connection
  after `runner.cleanup()`; a worker-thread body that opens a thread-local connection
  closes it before returning. The same tests were ALSO wrapping an app `start_dashboard`
  had already set up in a second `TestServer`, which re-ran every `on_startup` hook on the
  frozen app and orphaned a second `ClientSession` and knowledge watcher — serve
  `runner.server` through a `ServerRunner` instead.
- **A rising thread count is a leak only when the new threads are not a named bounded
  pool.** Every repeatable thread delta this pass measured (`+15` on the multistore
  stress test, `+3` on the terminal probe cap, `+3` on headless memory) was
  `mc-embed_*`, `mc-recall_*`, `mc-discovery_*`, `mc-pathres_*`, `mc-maint_*` or
  `sel-writer` warming for the first time on that worker — the same delta on every run
  because every run is a fresh process warming in the same order. Print thread NAMES in
  the probe before treating a count as a leak.
- **The per-test residue bisector applied the allow-list to the wrong level.** In
  `KIROCREW_TMP_PER_TEST` mode the by-design entries (`kirocrew-computer-shots`, a nested
  basetemp) sit INSIDE the per-test base, and the filter matched only the base's name, so
  144 spool "leaks" per run were attributed to whichever computer-use test reached the
  feature. The filter applies to the leaf as well now.

What the warnings census found, none of it visible as a failure or a residue:

- **Writing to a frozen aiohttp app: 160 per run.** `client.app["state"] = ...` after
  `start_server()`, a production `on_startup` object overwritten after start, and a
  request-time `request.app.setdefault("_bg_tasks", set())` in production — all
  "Changing state of started or joined application is deprecated", which a later aiohttp
  raises. Configure all app state before the client starts; to override something a
  production hook creates, append your own `on_startup` hook AFTER the registration call
  (hooks run in order) that tears the real object down and installs the fake; and seed
  every registry the handlers reach at route registration, while the app is still
  mutable (`setup_knowledge_routes`, `auto_research.register_routes` now do).
- **Fifty synchronous tests carrying `@pytest.mark.asyncio`**, forty-four of them from a
  module-level `pytestmark` in nine files. Each emits a `PytestWarning` today and, the
  day it gains an await-shaped call, asserts nothing. Per-test marks on the async tests
  (a class-level mark only where every test in the class is async); never make a sync
  test async to satisfy the mark.
- **A bare `AsyncMock()` standing in for a provider makes EVERY attribute an
  awaitable: 224 `coroutine ... was never awaited` per run.** `_run_chat` and the run
  loop call synchronous accessors on the provider — `context_window_tokens()`,
  `mcp_session_report()`, `available_models()`, the inner client's
  `pop_pending_oauth_requests()` — and each call handed back a coroutine nobody awaited,
  reported at garbage collection against a LATER test in a file that never built the
  mock. Build the double from one factory that pins each sync accessor as `MagicMock`
  or a `lambda`; and a mock that replaces a spawner or `asyncio.wait_for` must
  `close()` the coroutine it is handed, or the real turn it swallowed is the warning.
  Locate the culprit with a `-p` plugin wrapping `AsyncMockMixin._execute_mock_call`,
  not with the file the warning is attributed to.
- **A `threading.Thread(target=lambda: ...)` whose target is stubbed to raise turns
  the test's own signal into a swallowed `PytestUnhandledThreadExceptionWarning`.** Two
  dogfood tests stubbed `build_profile` to raise `ValueError("stop")` to halt startup
  after the checkout, then never observed it. Capture the outcome in the thread body and
  assert it is exactly the planted exception; a thread exception is never silent.

And two the census of duration and RSS added:

- **A tree ratchet that RETAINS parsed trees to share them between scanners.**
  `test_platform_cpp_seam_coverage`'s module fixture kept every core module's AST in a
  list for the module's life: +481 MiB on the worker's high-water mark (1,227 MiB under
  `-n0`), the largest single step in the suite, plus slower GC for every later test.
  Stream: yield each tree, extract everything every consumer needs in ONE walk into
  small result maps, drop the tree, cache the results — +0 MiB, and 13.5 s → 2.9 s CPU.
  Two more ratchets (`test_cron_store_unreadable_boundaries`,
  `test_compaction_wait_budget`) re-parsed the tree per TEST with no `xdist_group`; they
  now use `source_corpus.candidate_sources` and carry their own group.
- **A linearity ratchet on a wall-clock ratio flips under load, and a call-event
  count cannot replace it.** `test_the_eval_join_stays_linear` compared timings at
  2,000 and 16,000 tokens; the small sample is milliseconds, so one preemption during
  the large one breached a ratio a regression would also breach — it had been widened
  once already and still flipped under twelve concurrent agents. Interpreter call
  counts read a quadratic `str.join` as linear (one C call whatever its length). The
  join's work IS the length of the string it builds, so the test now compares total
  payload characters: exactly 8.0x for the linear walk, 64x for the unbounded-join
  mutant, on every host. Measure the work the algorithm does, in units the algorithm
  defines.
- **A keepalive cadence racing a silence window is the same ratio, one layer down.**
  `test_queued_frames_extend_a_queue_aware_wait` had a scripted daemon send `queued`
  every 0.05 s under a 0.12 s silence window, both on ONE event loop. The window is an
  `asyncio.wait_for` timer, so it runs on `loop.time()`; a 150 ms stall of the loop
  thread between two frames -- a loaded Windows worker -- reads as silence and the wait
  returns `timeout` (four unrelated heads, green on rerun; reproduced on Linux by
  `time.sleep(0.15)` before each frame, every run). Widening the ratio only moves the
  stall that flips it. Move the clock instead: `monkeypatch.setattr(loop, "time", ...)`
  to a value the test owns, and charge the "silence" at the read -- a `_read_frame`
  wrapper that advances the clock 0.1 s before each frame lands -- while the daemon
  writes its frames back to back. The timer and the cadence are then measured on the
  same clock and nothing else moves it, so the test also asserts the wait outlived a
  fixed 0.12 s deadline (`clock - started == 0.7`), which a fixed-deadline mutant fails.
  Pass the same clock as `now=` so the total budget rides the same time. A frozen loop
  clock also freezes the test's own `wait_for(..., timeout=10)` net, so a read that
  never resolved would hang the worker instead of failing: pair the freeze with a
  REAL-clock watchdog -- a `threading.Timer` that, after ten seconds, jumps the loop
  clock past every armed deadline via `call_soon_threadsafe` -- and prove it once by
  running the test against a daemon that never sends and never closes (red at 10.00 s,
  not a hang). The cleanup must end on its own too: close the client writer, then
  CANCEL and await the daemon's handler task so its `finally` closes the server-side
  writer -- on POSIX `server.wait_closed()` waits for that connection, and a `script`
  that never returned would hold it forever -- and only then drain the server and
  cancel the watchdog. Arm the watchdog before the first await that can fail and
  cancel it in the same `finally`, so a failed bind cannot leave a Timer thread to
  fire on a closed loop. The freeze is NOT safe around `asyncio.sleep(x > 0)` or a
  connect with its own timer either -- check the path first.

### What a sixth five-run pass found (Windows host, ten workers, ~98k tests per run)

Repeated on `main` one day after the macOS pass above, on the Windows developer machine
of the second pass, with the same per-test probe plus a before/after snapshot of the TEMP
root and the operator's home. Four of the eight classes it found had landed upstream from
the macOS pass while this one was being written (the retained ASTs, the `workspace_root()`
default, the `GIT_*` session fixture and the two `delenv`-before-write env leaks — each
reproduced here independently, with the same fix); the four below were new. One test
flipped across the five runs, and the always-red set (173 tests, every one from
`workflow_memory` refusing files whose owner is the built-in `Administrators` group) was
the host, not the suite — see the section above.

- **A singleton built at COLLECTION outlives the tmp dir it was bound to.**
  `test_app_backend.py`'s `_sandbox_can_spawn()` probe runs `wrap_argv()` under an
  empty `KIROCREW_HOME` inside a `TemporaryDirectory`. On a host with no sandbox
  backend the call fail-closes and records a `denied` audit through `sel()` — the
  process singleton, which binds `_dir` once from whatever `_default_dir()` says at
  that moment: the throwaway home. The `_isolate_sel_default_dir` session floor resets
  the singleton at the FIRST TEST's setup, which is after collection, so the probe's
  instance stays live through the whole collection phase and any write on it
  `mkdir`s the deleted home back: one bare `tmp*` directory at the TEMP root per
  worker, holding `security_events.jsonl` and a `trust/` HMAC key, on every run.
  Fix, local: the probe OWNS the singleton — `_own_probe_sel` constructs it
  `sync=True` under the temp home BEFORE `wrap_argv()`, so the denial is written
  inline and no writer thread ever exists, and `_retire_probe_sel` clears the class
  slots before the directory goes. The first shape retired an async instance after
  the fact (flush, shutdown sentinel, `join(timeout=5)`); review pointed out that a
  join which times out leaves a daemon thread whose next `_flush_batch` re-creates the
  deleted home — the very leak, one race away. Prefer never starting the thread over
  stopping it. The shape to grep for is a module-level probe that can reach a process
  singleton (`sel()`, a config loader, a metrics exporter) — the floor cannot see it
  because no fixture has run yet, and a `with TemporaryDirectory()` around it proves
  nothing when the object it created holds the path.
- **`int(MagicMock())` is 1, and a drain loop reads it as "one still pending".**
  `_drain_update_callback_work` polls `int(sessions.inbound_callback_count)` until it
  reaches zero or its 30 s deadline. The shared `_mock_sessions()` never set that
  attribute, so the mock answered 1 forever, and twelve auto-apply-update tests each
  sat out the full 30 s — six minutes per run, in a file whose other 300 tests take
  fifteen seconds — then took the "restart deferred" branch instead of the restart
  they were named for, and still passed. The tell is a test whose duration is EXACTLY
  a production timeout. A `MagicMock` attribute the code under test converts (`int()`,
  `float()`, `len()`, `bool()`) or compares must be set explicitly in the helper that
  builds the mock; the fix here was one line, `s.inbound_callback_count = 0`.
- **A ReDoS regression in these grammars is EXPONENTIAL, so the guard's input size
  decides whether it fails or hangs.** `test_options_marker_closers`'
  `elapsed < 1.0` wall-clock bound on a 200 000-tab pump flipped once in five runs —
  0.15 s of CPU, descheduled behind nine sibling workers (class 5). Measured against a
  mutated closer class that shares `\t` with the trailing `[ \t]*`: 0.27 s at 20
  characters, 4.3 s at 24, doubling per character. Under that regression the
  200 000-character input never returns and the worker is killed at `--timeout` — a
  lost run (class 6), not a red test; the same shape sat in three sibling tests, and
  a fifth guard (`test_options_marker_label_closers`' opener-run ratio) failed in the
  change-related gate for a reason of its own: it DIVIDED two `time.monotonic()`
  readings, and on Windows that clock ticks every 15.6 ms, so a 20 000-opener scan
  that read 0.0 (floored to 1 ms) against one that landed on a single tick (16 ms)
  produced a 16x "ratio" with the property intact — a mutant whose interior admits
  the lookalike openers is exponential as well (3.2 s at 24), and one whose interior
  admits EVERY bracket grows ~8x per pumped block (3.8 s at 8), so no single "small"
  size is safe against every regression class.
  `conftest.assert_rejected_without_backtracking` replaces every wall-clock guard
  on the marker grammar — sixteen across nine test files, found by grepping for
  the PUMP shape (`"[OPTIONS:" ... * <n>`) rather than for any one assertion
  message, since two rounds of review each turned up siblings a message grep had
  missed: thread CPU, a one-unit RAMP from 1 to 24 whose first over-budget size
  fails the assertion — so the cost of catching a regression is bounded by one
  growth step times the budget, measured 6 s against both mutants — then ascending
  long pumps (200, 2 000, 20 000) for the polynomial class, taking a second reading
  only when the first overran (a GC pause cannot hit two in a row). The property
  itself is also asserted structurally where it can be: no
  closer, opener or wrapper character `isspace()` or is a label separator.
- **A refused "system directory" is platform-shaped, and the refusal path CREATES
  what it does not refuse.** `KIROCREW_HOME=/usr` resolves to `C:\usr` on Windows,
  which `_is_unsafe_home` (correctly) does not know, so `config_dir()` accepted the
  override and made the directory on the system drive every run; the test was a
  strict xfail there for the wrong reason. The test now names the location the guard
  refuses on the host it runs on (the drive root on Windows) and the xfail line is
  gone. A fixture value that spells a POSIX absolute path is a Windows relative one
  (`host_abs`, above), and a resolver test must check what its rejected input
  resolves TO before assuming rejection.

Two things this pass did NOT find are worth recording so the next one does not
re-derive them. A per-worker `test-floor-*` directory at the TEMP root was residue from
runs KILLED mid-session on the same host (zero of fifty-five floors leaked from a run that
finished), and the computer-use host catalog probing `Program Files` at import is the
product's own capability probe, transient and by design.

### What a seventh five-run pass found (Linux, 16 workers, 122,426 tests per run)

Five backend runs plus five frontend runs (36,093 vitest, 1,959 Electron) on a 32-core
Linux host, under a per-test audit-hook probe, with the run root pinned under the worktree.
Counts were identical across all five runs — 121,905 ± 2 passed, **74 failed in every
run**, zero pass/fail flakes in ~612k backend test executions — so every class below is a
property of the code or the host, never a race. Two properties of the measurement shaped
what it could see, and both are worth reusing:

- **`TMPDIR` pinned under the checkout is a probe, not an accident.** It is what exposed
  the largest class here (66 of the 74 always-red tests), and the fixes are real hardening
  rather than accommodation: a developer whose `TMPDIR` is `./tmp` hits the same wall.
  Every fix was verified BOTH ways — under the pinned root and under the default one.
- **A relative path in an audit event is not a cwd write.** The probe attributed
  `os.remove("b.txt")` from `shutil.rmtree`'s fd-walk, and every leaf name this repo's
  pinned-`dir_fd` writers open, to the process cwd — which for a pytest worker is the
  checkout. That single mistake produced the report's biggest "class" (43,527 tests) with
  **zero real members**, and it spent the per-test event budget so 1,324 tests came back
  under-measured, hiding real findings behind the noise. A probe must treat a path it
  cannot attribute as unattributable and say so.

- **A test must not assume `tmp_path` is outside a repository.** Any fixture whose verdict
  comes from an UPWARD walk — nearest `.git`, `install.sh` + `setup.cfg`, project markers
  from the cwd, git's own repository discovery, repo-relative keying — silently inherits
  the enclosing checkout when the temp root sits under one, and answers about the host
  instead of about the code. It is worse in a LINKED worktree, where the `.git` the walk
  finds is a FILE, so a marker probe declines before the arm under test ever runs. Fix:
  pin the boundary the walk reads inside the fixture — plant the nearest marker, give the
  directory a git-accepted `.git`, or use the walk's own seam. The floor now sets
  `GIT_CEILING_DIRECTORIES` to this run's temp roots so git discovery cannot climb out of
  `tmp_path` by default, and `--confcutdir` + an explicit `-c <ini>` do the same for a
  nested pytest session, whose rootdir would otherwise become the repository and whose
  `addopts` (`--color=yes`) would reshape the very output the outer test greps.
  NOT the fix: moving the temp root.
- **A stub for an `os.*` function reached through a module alias replaces the
  PROCESS-GLOBAL original.** `monkeypatch.setattr("<module>.os.unlink", ...)` patches the
  stdlib, and a stub whose fallthrough narrows the signature (`os.remove(path)`, dropping
  `dir_fd`) re-aims pytest's own fd-relative `rmtree` at the cwd — the only absolute write
  into the checkout the whole pass found, deleting real files there and leaving the tmp dir
  behind. Fix: intercept only the owned path and forward every other call to the saved real
  function with `*args, **kwargs` intact, and assert a bare relative name never reaches the
  stub. The same shape disables cleanup wholesale when the patched function is `os.close`.
- **Bytecode for a source under `tmp_path` accumulates forever in the per-user mirror.**
  `sys.pycache_prefix` is keyed on the source's absolute path, and a `tmp_path` module's
  path is new every run, so each run left one more dead tree: 14,816 orphaned `.pyc` files,
  5.3 GB, across 161 dead run roots on the host that found it. The suite compiles throwaway
  sources on purpose, so the answer is not to stop: the writer owns the retirement, and
  `pytest_sessionfinish` now prunes the mirror subtrees keyed on this run's temp roots. A
  test that asserts the SHIPPED `__pycache__`-beside-source layout clears
  `sys.pycache_prefix` for its own duration instead.
- **A fixture that needs a SHORT temp path must not reach for a literal `/tmp`.** An
  `AF_UNIX` `sun_path` caps the bind string at 108 bytes (104 on macOS) and a path asserted
  in message metadata must not trip `redact_credentials()`, so ~40 sites used
  `mkdtemp(dir="/tmp")` — anonymous directories that no run owns, on a host whose `/tmp` is
  reaped mid-session. `tempfile.gettempdir()` cannot serve: under a long `TMPDIR` the socket
  path is already 122 bytes. The floor now mints ONE run-owned short root under the platform
  temp root carrying the `kc-pytest-<user>-<pid>-` stem the residue guard recognises, and
  `tmpdir_helpers.short_tmp_base()` is the single seam that returns it.
- **`kill` is classified by CALLER and by who spawned the target, never by the signal.**
  A signal 0 from the repo's own `pid_exists`/`pid_liveness` helper is a liveness probe
  (1,149 of 1,185 recorded events came from ONE background sweep thread, attributed to
  whichever test was running), and a signal aimed at a process the test's own `Popen`/`fork`
  created is correct teardown. Two real defects hid in that volume: a raw
  `os.kill(pid, 0)` in TEST code — forbidden outright, since it TERMINATES the target on
  Windows — and a `finally` that SIGKILLed a raw grandchild pid the body had already proven
  dead, firing a stray signal at whatever now holds that number on every passing run. Fix
  the first by routing through `platform_compat`; the second by capturing the target's
  identity at spawn and revalidating before signalling.
- **A test that reaches a real host binary is two findings, and the missing `cwd=` is the
  smaller one.** Pin the production seam that RESOLVES the tool (`trusted_system_bin`,
  `_mise_which`, `_ssh_supports_accept_new`, `_gh_prefers_ssh`) to an argv-recording fake
  under `tmp_path` so the binary is never reached; keep only the spawns whose real behaviour
  IS the assertion (git's ignore and ancestry semantics, openssl's DER output), and give
  those — production helper included — a `cwd=` the test owns. `cwd=` alone leaves a live
  `gh` token in play.
- **A skip whose condition is a clock or a scheduler flips without any test failing.** Three
  tests skipped in some runs and passed in others: a ctime tick, whether a real `npm`
  answered in time, whether an earlier test had armed a fork hook in that worker. No
  assertion ever failed, so nothing was red. Fix: make the test BUILD the condition it needs
  — construct the observation through the production seam, resolve the capability from what
  is on disk, or run the measurement in a fresh interpreter — so the gate reaches the same
  verdict on every run of one host, and a timeout becomes a failure rather than a skip.
- **A whole-tree scan that memoizes file TEXT retains ~2 bytes per character for the
  process lifetime.** `scripts/leaf_test_scope.py`'s unbounded `lru_cache` held every `.py`
  under `src/`, `test/` and `scripts/` — 143 MB — to answer one question per file. Fix:
  stream the read and cache only the derived answer (a name index), which took the per-test
  delta from +291 MiB to +22 MiB and the module's wall clock from 32.6 s to 14.1 s. A
  ratchet changed this way must be re-proven by planting the violation it exists to catch.
- **A test whose network stub covers only some fetch seams still reaches the network.**
  A passing test fetched from `raw.githubusercontent.com` because it routed the JSON and
  text seams while the search → commit → tree → BLOB walk used the third. Rank before
  fixing: a routable address is genuine egress, while a UDP connect to RFC 5737 TEST-NET is
  the packet-less local-IP-discovery trick and a hardening item. Refuse the opener in an
  autouse fixture, route EVERY seam the walk can take, and assert the address the code would
  have used.
- **A process-lifetime handle the object under test opened is the harness's to close.**
  185 tests leaked descriptors in at least four of the five runs: a member vector DB, a
  lazily-opened search index, and `SubagentManager`'s durable task queue (a SQLite
  connection plus a writer thread) which had NO close path at all — a real production gap,
  now `SubagentManager.close()`. Fix at the seam the object exposes, attached to the fixture
  that built it; never `gc.collect()` to make the number drop.

Three classes the pass proved were NOT defects, so the next one does not re-litigate them.
`thread_delta` whose new threads are all NAMED pool workers plateauing at the pool's
`max_workers` is lazy bounded-pool warming. An `env` delta naming only `XDG_RUNTIME_DIR`
and `DBUS_SESSION_BUS_ADDRESS` is the session floor's own pop-and-restore, attributed to
whichever test ran first and last on the worker — moving a different file to the front moves
the finding with it, and converting that floor to function scope would reopen the hazard it
documents. And the hypothesis example database under the per-user cache root is deliberate
persistence so a shrunk counterexample replays; a harness that wants it inside its sandbox
pins `XDG_CACHE_HOME`, rather than each test opting out.
### What an eighth five-run pass found (macOS, eight workers, ~121k tests per run)

A Linux pass ran on another machine at the same time and claims the seventh slot
([#12425](https://github.com/kirodotdev/KiroCrew/pull/12425)); the two overlap on
three findings and are noted where they do.

Five rounds of backend, vitest and electron on a test-only branch off one commit:
121,271 / 36,093 / 1,959 tests per round, all five rounds identical, and a backend
failure set identical across all five (md5 `c911e103` of the sorted `FAILED` node
ids with the trailing reason text stripped — the reason wording can vary while the
set does not, so it is the ids that are hashed). **Zero** flaky tests, and zero
added files in the checkout in every round. Every large cluster the sweep reported
was its own instrumentation, so this section is mostly about the instrument rather
than the suite: nine defects in the measurement, one limit disclosed rather than
closed, and one real defect in the suite. Two of the four always-red tests and one
of the two `network` findings were the tools, not the tests.

The generalisable lesson: **a probe that observes the suite is itself code under test,
and its failure mode is a CLEAN report.** A measurement defect does not announce itself
the way a red test does — it removes signal, or manufactures a cluster large enough that
nobody reads its members. Every number below was checked against a second witness or a
negative control before it was believed, and that is what caught them.

#### The findings that were the instrument

- **A green summary line has a different SHAPE from a red one, so a parser derived from a
  failing run reads every passing run as a killed one.** vitest prints
  `Tests  36093 passed | 1 expected fail | 2 skipped (36096)` — no `failed` token at all,
  and `expected fail` is a TWO-WORD label that broke a `(\d+) ([a-z]+)` repetition, so the
  whole line matched nothing and all five green rounds were reported as runs that never
  finished (exit 3). electron runs `node --test` under its DEFAULT reporter
  (`<glyph> pass 1959` / `<glyph> fail 0`), never the TAP `not ok` the parser looked for;
  the glyph is U+2139, which Python's `\w` matches, so a `[^\w\s]` class for it rejected
  every real line. Then the electron FIX had the defect it was written to close:
  `if "pass" not in counts and "fail" not in counts` answered `(1959, 0)` — fully green —
  for a log truncated after the `pass` line. That reporter emits
  `tests / suites / pass / fail / cancelled / skipped / todo` as one ordered block, so
  every column must be present AND reconcile against `tests`. An unreadable summary is not
  a passing summary.
- **`os.path.realpath` on a RELATIVE path anchors it at the process cwd, and an xdist
  worker's cwd is the checkout root.** So every per-test `tmp_path` write of `config.json`,
  `home`, `memory.db`, `sessions` or `agents` was recorded as a write into the tree under
  test: 43,316 tests over 1,735 files — the largest class in the report by two orders of
  magnitude — against a snapshot that saw zero added files in the checkout in all five
  rounds. Two witnesses disagreeing that far is the tell, and the snapshot was the one
  telling the truth. A path that cannot be attributed to a root is now its own class and is
  never asserted to be a checkout write.
- **Absoluteness is a property of the PATH's own syntax, not of the host reading the
  report.** The guard asked whether the path starts with `/`, so every `C:\Users\...`
  record read as relative — and the sweep skill routes Windows through its probe entry
  point, so that platform's whole host/checkout signal landed in the unattributable class
  and the report read clean. A backslash is a legal POSIX filename character, so it counts
  as a separator only for a path that is Windows-shaped. The host that reads a report need
  not be the host that produced it.
- **A live gateway writes into its own data home for the whole run, and that is not
  residue.** Every "new file" in all five rounds was `artifacts/`, `metrics/`, `pw/` or a
  session `.sig` under the data home — in round 2 the 27 of them included the very report
  card the finding was delivered in. Anchored on real path COMPONENTS under the data home
  so a project's own `artifacts/` directory is never swallowed. Residue is 0 in every
  round.
- **Signal 0 delivers nothing, and reaping your own child is not a foreign signal.** 1,240
  recorded signals were zero — the existence probe, which cannot affect its target — and of
  the 214 tests the class named, 50 sent nothing else, leaving 164. Both are separated out
  and COUNTED, never silently dropped: a filtered class and an empty class must not look
  alike.
- **A repo-relative build-product filter cannot reach a cache the tooling keeps OUTSIDE the
  repo.** The suite points hypothesis' example database and `PYTHONPYCACHEPREFIX` at
  `~/.cache/kirocrew/...`, so every property test and every subprocess import wrote there,
  and the suite's own socket directories added the rest. Sanctioning those two took the
  class from 331 tests over 85 files to 63, and the 63 that remain are attempts that could
  not succeed — a write to `/proc/nonexistent`, a path a monkeypatched ingest returned — so
  read the paths before reading the count as damage.
- **A probe's stdlib lookups run INSIDE the window the test under measurement has
  monkeypatched.** `test/test_cli_setup_cov80.py`'s
  `TestRemoveRetiredConductorSkill::test_the_conductor_directory_is_never_re_resolved_by_name`
  patches `os.path.realpath` to fail on any path ending in `conductor` — globally, because
  patching it through a module's `os.path` attribute patches the one shared `os.path` — then
  removes a `conductor/` directory. The probe normalises every recorded path with
  `os.path.realpath`, so the PROBE tripped the assertion and the test was red in all five
  rounds. Negative control: the identical node on the identical tree passes without the
  probe loaded and fails with it, deterministically. An observer must bind every stdlib
  callable it uses at IMPORT time; reading one by attribute at call time makes the observer
  visible to whatever the test patched, and the test cannot tell it apart from the product.
  The concurrent Linux pass closed the other half in
  [#12425](https://github.com/kirodotdev/KiroCrew/pull/12425), keying the trap on which
  module asked so a bystander normalising a path it was handed is not the defect production
  re-resolving the name is. Both halves are worth having: the test stops accusing observers,
  and a well-behaved observer stops being visible to tests that patch a stdlib symbol.
- **A datagram `connect()` sends no packet, so it is a routing-table query and not a network
  connection.** `src/kiro_crew/security/argv_floor.py`'s own-address enumeration connects a
  `SOCK_DGRAM` socket to `198.51.100.1:53` and `2001:db8::1:53` — TEST-NET-2 and
  documentation addresses, contacted by nobody — to learn the primary outbound address per
  family, and says so in a comment three lines above. The `socket.connect` audit hook never
  read the socket's `type`, so that recorded as an outbound connection to a non-loopback
  peer. One of the two `network` findings was this. The same pass also shows why the class
  needs thread attribution: one of those three events arrived off-thread, from the
  asynchronous name-resolution worker, and was charged to whichever test was running.

#### The limit disclosed rather than closed

- **The two side-effect witnesses do not cover the same ground, and a reader who assumes
  they do reads one's silence as the other's corroboration.** The host snapshot walks the
  data home, `~/.kiro/agents` and the temp roots ONLY. A write to `~/.cache` is invisible to
  it, so the probe's filter is the single witness for that entire class, and `residue: 0`
  neither confirms nor denies it. The report now states that in its own header rather than
  leaving it to be inferred. Widening the snapshot is a separate change.

#### The one defect that was the suite

- **A fixture temp directory with no name cannot be told apart from a leak, and naming the
  rest by sample got it wrong twice.** About a dozen fixtures cannot use the pinned per-test
  root — an `AF_UNIX` path is capped at 104 bytes, and a path asserted in message metadata
  must not trip credential redaction — so they take the shared system temp dir. Six of them
  passed NO prefix and landed as `tmp<random>`, the stdlib default, which is exactly what an
  accidental bare `mkdtemp()` also produces: sanctioning that name would go blind to the
  leaks the class exists to catch. The other six each invented their own (`pw-`, `podapi-`,
  `kcsock-`, `kcs-`), and enumerating those from one run's records missed `kcs-` on the first
  attempt and a bare `tmp-` on the second. The rule that follows is the SUITE's, not the
  filter's: a tool that must recognise the suite's own directories needs the suite to SAY
  whose they are, because a rule built from a sample of them is wrong by construction the
  next time a fixture is added. Both halves of the answer are now in the tree:
  [#12399](https://github.com/kirodotdev/KiroCrew/pull/12399) gives the short-rooted
  fixtures one shared stem (`SHORT_TMP_PREFIX`) with two ratchets pinning every caller to
  it, and [#12425](https://github.com/kirodotdev/KiroCrew/pull/12425) mints a run-owned
  short root under the per-run stem the residue guard already recognises. They compose, and
  each answers a question the other cannot: the root says WHICH RUN made the directory and
  who removes it, the prefix says WHICH FIXTURE inside it — which still matters wherever the
  root is absent (outside a pytest run, or in a checkout whose conftest predates it), since
  both fall back to the shared system temp dir.

#### What the always-red set actually was

Four backend tests failed in all five rounds. The byte-identical failure set is what makes
each one attributable at all — no re-run can turn a host-shaped red green, so classify
before rerunning.

- `test/test_kiro_cli_pin.py`'s two `*_never_runs_a_path_shadowed_shim` tests pin `PATH` to a
  fake shim directory and pass a fake `home`, but the candidate list in
  `src/kiro_crew/kiro_cli.py` also carries a HARDCODED `/Applications` bundle path that no
  fixture argument reaches. On a developer machine with the real app installed the product
  CORRECTLY resolves a pinned absolute path there, and the test's "nothing was spawned"
  assertion fails. A resolver whose candidate set mixes injectable locations with a fixed
  system one cannot be fenced by the injectable ones alone — the fixed entry needs the same
  seam, or the test is a statement about the developer's `/Applications`.
- `test/test_overload_home_fence.py`'s
  `test_a_window_alone_still_routes_through_the_extra_paths_launcher` read `cleanup is None`.
  Not the platform, and the first diagnosis — that a macOS host has no namespace backend to
  write a launcher for, so the test wants a Linux-only guard — was **wrong**, and requiring
  Linux would have thrown away coverage the host genuinely has. A macOS kiro-cli spawn is
  handed to kiro-cli's OWN sandbox when the internal edition's settings ask for it, and Kiro
  Crew then writes no launcher at all; `is_kiro_cli=True` is what reaches that decision,
  which is why the sibling seatbelt case never saw it. With the delegation pinned off the
  launcher IS written on macOS and the window reaches it, so the test now runs there. A
  settings file the code reads is an input like any other: when a test's subject is the
  routing and not the operator's configuration, pin the decision rather than skipping the
  platform. The tell that the first diagnosis was wrong is cheap to get — force the suspected
  gate off in a five-line probe and see whether the assertion's own subject appears.
- the fourth was the probe's `os.path.realpath`, above.

#### The one real network finding

`test/test_skill_provider_github.py`'s `TestNetworkGuards::test_commit_resolution_asks_for_the_sha_media_type`
patches that module's JSON and text fetch helpers and NOT its bytes helper, so the search it
drives fetched real bytes from `raw.githubusercontent.com` in every round — inside a class
whose entire subject is that module's network boundary. Measured: as written, one TCP connect
to a real GitHub address; with the bytes helper also patched, zero connects, and the test's
own assertion still holds. A test that fences a module's network access must patch EVERY
fetch entry point the module exposes, found by grepping the module for its fetch helpers —
not the subset the assertion happens to read. The concurrent Linux pass reached the same
test independently and its fix lands in
[#12425](https://github.com/kirodotdev/KiroCrew/pull/12425), which also refuses the shared
opener for the whole module so an unrouted seam raises instead of requesting — the stronger
shape, because it does not depend on the next author grepping.

#### What not to re-derive

Two classes looked large and were dispatched to nobody, because their thresholds and their
attribution have to be fixed before a member means anything. Of 204 tests in the
descriptor-leak class, 140 had a maximum delta of EXACTLY the threshold of 5 — a bounded
five-descriptor pool, not a leak — and the rest ran 6 to 21. In the thread-leak class 4 of
14 tests had a NEGATIVE delta in some round (−9, −10): a previous test's threads were reaped
during this one, so the counter crosses test boundaries and a delta is not that test's doing.
The `spawn_no_cwd` class (531 tests) is real and advisory only.

### What a ninth five-run pass found (Windows host, eight workers, 123,179 tests per run)

Native Windows (Server 2025, 16 cores), five rounds of the backend suite under the sweep
skill's per-test probe on a test-only worktree off one commit, `-n 8 --timeout 120`, the
results directory outside the checkout: 117,529 to 117,532 passed, 2 to 5 failed, 0 errors and 5,479 skipped per round, 44 to 46 minutes each, minimum available memory 32.8 GiB. No worker was killed by
`pytest-timeout` in any round -- the previous Windows pass lost one round of five to the
probe's own `realpath` hot path, and the memoised probe is what made this one comparable
end to end. Residue was 0 in every round. Two tests were red in all five rounds and both
are the host, not the suite: `test_crew_image_publish_contract.py`'s shared-fixture shell
test and `test_windows_fleet_setup.py`'s `[pwsh]` case fail BY DESIGN on a host with no
Git Bash and no `pwsh`, which this one has not. Everything else that went red went red in
SOME rounds, which is the class this pass is about: three flakes, each reproduced on a
clean second worktree at the same sha before it was touched, each with a different
mechanism, and one of them a production defect wearing a test's clothes.

- **A caller-side write budget is a wall clock the test did not know it was on.**
  `test_decisions_tool_risk_end_to_end.py` drives the real `tool.risk` point and asserts
  the badge on the tool card. The point drops the badge ON PURPOSE when the outcome row's
  `to_thread` append does not return inside `LOG_BUDGET_SECS` (50 ms) -- a badge whose
  durable row was refused would carry a `turn_id` no verdict could be filed against. On
  this host a cold thread pool plus a first file open crosses 50 ms often: alone at `-n0`
  the file was red in three runs of four, and the point's own debug line named it (`outcome
  row outlived its write budget`); with the budget lifted, 5 of 5 green. The fix landed
  concurrently in [#12753](https://github.com/kirodotdev/KiroCrew/pull/12753), which lifts
  all three budgets on that path in the file's autouse fixture and pins the budget's own
  branch with the value set to 0 -- this pass only confirms it from a second host, and
  carries no change to that file.
- **A returned exception is a reference cycle through whatever its frames were holding.**
  `test_eventlog_hooks.py` pins that this process retains no crew-log write lease after
  `ensure`/`append`/`read`; in rounds 1, 3 and 4 it read a lease belonging to
  `test_crew_log_edge_exhaustion.py`'s temp home, from a different worker's earlier test.
  Two retention roots, both real. The test one: the disk-error tests parametrized OSError
  INSTANCES (`pytest.param(OSError(errno.ENOSPC, ...))`), so `raise err` hung a
  `__traceback__` on a module-lifetime object and every frame on it -- the writer's job,
  with the `CrewLog` handle as a local -- lived until the module did. The production one:
  `emit._run_job` returned the caught exception to `_write_batch`, which bound it to a
  local while deciding whether to retry; the traceback's `_run_job` frame reaches
  `_write_batch`'s frame through `f_back`, and that frame holds the exception -- a cycle
  through the handle, so the lease (a `weakref.finalize` on the handle) was released by the
  cyclic collector at some later pass instead of at the drop, in production as well as
  here. Reproduced deterministically at `-n0` by running the two files in order; traced with
  `gc.get_referrers` from the handle up to both roots. Fixes: the errno is parametrized and
  the exception built inside the test; `_run_job` returns the failure's TYPE (`_permanent`
  needs only that) and `_report` hands the record `str(exc)`; and the exhaustion file's
  teardown pins `lease._held` empty, so the retention is reported where it is created. The
  first cut stripped only `exc.__traceback__`, and the review lanes caught what that
  leaves: an error raised inside an `except` carries the first exception as `__context__`,
  whose traceback holds the same frames -- a chained-raise case now sits under the pin and
  fails against that cut. Mutations: returning the exception with only its traceback
  stripped trips the pin on the chained case; returning the type but logging the exception
  object trips it on every case (pytest's per-test record capture keeps the object, and
  with it the frames); both hunks together are green, and reverting the test hunk alone
  stays green, so the production change is the load-bearing one and the test change is
  hygiene plus the witness. The pin then paid for itself before the PR merged: on the macOS
  shard it read a lease from `test_crew_log_core.py`'s chmod-refusal test, an earlier file
  on the same worker -- `store._mkdir_private` warned with `exc_info=True` from inside
  `CrewLog.append`, the same class at a second site, POSIX-only because Windows never
  refuses the `chmod`. That warning carries its traceback as text now and the test pins
  that dropping the handle releases the lease at once. The review lane then counted the
  rest of the class in the package: it counted seven more `exc_info` sites whose frame
  holds a `CrewLog` handle, and a source-reading pin written for the follow-up found five
  the count had missed (a keyword-only `handle: CrewLog` parameter, a helper that takes
  the handle to check the unit is still there), and widening the pin to locals found a
  thirteenth (`read.recorded_class` binds `handle` from `open_session_log`). All of
  them -- two in the store's prefix readers, ten on the checkpoint module's savepoint
  paths, one in the reader -- route through one `store.log_exception_text` helper, which
  renders the traceback to text and skips the render when the level is off. The pin,
  `test_crew_log_exc_info_sites.py`, walks the source tree's AST: no `exc_info` call in
  a `CrewLog` method or in any function under `kiro_crew` that names a handle anywhere
  in its body (the invariant is process-wide, so the walk is too -- files naming no handle
  are skipped before the parse, which keeps it under a second), and the sites left in the
  package equal to a vetted list. A checklist item asks a reviewer to notice; the pin
  fails the first run that adds one.
- **"Let it finish" as two 200 ms sleeps.** `test_overload_integration_glue.py`'s
  `test_drain_refill_reads_the_store_off_loop` waited `20 x sleep(0.01)` twice for started
  runs to settle through the writer thread, then asserted a row reached a terminal state; in
  round 3, on a loaded worker, both rows still read `starting`. The pin the test exists for
  (`loop_thread_calls` unchanged under the strict guard) was never at risk -- the timing
  assertion around it was. It now polls the two rows' states OFF the loop (the guard is
  still armed) under a 10 s deadline and asserts on the state it waited for, which is the
  "Interleavings: name the point" rule above applied to a settle rather than a registration.

What was flagged and read before being left alone, so the next pass does not re-derive it.
`env_leak` named `GIT_CEILING_DIRECTORIES` on 25 tests over the five rounds, and every
one of them was the FIRST or the LAST test an xdist worker ran: the key is written by the
session-scoped temp-root fixture during the first test's setup and removed during the last
test's teardown, and the probe's census brackets each test from `logstart` to
`logfinish` -- so it reads the floor's own arm and undo as that test's leak (measured
directly: one file alone shows `added` on its first test and `removed` on its last).
`host_write` was the bytecode mirror and the hypothesis database (about 1,400 events
each), `test_computer_use_launch.py`'s deliberate real-install-directory probes, and the
data-home floor's `kc-pytest-*-home-*` directory -- all documented in the pass before this
one; nothing touched the live data home or the checkout in any round. `thread_leak` was
the bounded named pools (`mc-embed_*`, `mc-recall_*`, `mc-subproc_*`, `mc-pathres_*`),
with deltas that repeat exactly across rounds rather than grow. The one `timeout`-class
row, `test_2000_submissions_all_complete_window_never_exceeds_64`, ran 85 to 110 s under
its own `timeout(900)` marker. The two instrument corrections the earlier Windows pass
had to make (a basetemp inside the worktree read as `checkout_write`; the `nul` device
read as `host_write`) did not recur, because the results directory was placed outside
the checkout and the probe knows the null device.
### What a tenth five-run pass found (Linux, 16 workers, 126,905 tests per run)

Five rounds of backend, vitest and electron on a test-only branch off one commit, on a
32-core host with a LIVE gateway running out of the same data home: 126,905 / 36,811 /
1,999 tests per round, identical every round; vitest and electron green five times. The
backend was red in every round, and the red set was NOT stable: one test failed 5/5 and
three others failed exactly once each, in rounds 3, 4 and 5 -- which is the shape a
five-run pass exists to catch, since a single run would have shipped each of them as
green. Zero added files in the checkout in every round. Two production defects came out of
the flakes, two more out of the descriptor class, and the largest class in the report was
the instrument again.

The generalisable lesson this time: **the test that fails is rarely the test that is
wrong.** Three of the four reds were victims: a stale lease from a module 870 tests
earlier on the same worker, a WARNING from an executor thread another test's
`SessionManager` armed, a ratio that a neighbouring worker's scheduling moved. Attribute
by mechanism (which frame, which thread, which cache) before touching the assertion that
went red, or the fix lands in the wrong file and the leak stays.

#### The instrument

- **pytest's own `tmp_path` retention sweep is not a test's write.** Every fs event whose
  path was a bare relative name (`18e82dd5...`, `42`) came from `_pytest/tmpdir.py`'s
  `rm_rf` over the previous runs' `pytest-N` directories -- the fd-walk leaf names of
  `rmtree` -- and the probe filed 45,573 tests in 1,799 files under `unattributable_write`
  and 1,358 under `under_measured` because those events also ate the per-test budget. With
  default priority those two classes OWNED 1,625 files, including the ones that carried the
  real `spawn_no_cwd` and `fd_leak` findings, so the analyser gained an `--exclude-class`
  switch and the report is re-cut without them. A class every second file is in is a
  statement about the harness, not the suite.
- **A host that runs the product cannot use the data-home snapshot as a witness.** The
  live gateway wrote 300-700 files under `~/.kiro` per round (skill-projection leases,
  `crew/run`, review worktrees), so `residue` was DIRTY every round and said nothing about
  the tests; the per-test probe's attributed writes were the only usable evidence for that
  class. Pin `TMPDIR` under the run root so the `/tmp` churn of other processes is not
  yours either.

#### The four reds

- **A test that plants a fixture under `tmp_path` and asserts a "not under `$HOME`"
  refusal does NOT fire is asserting where the host's `TMPDIR` lives.**
  `test_dev_fleet_app.py::test_trusted_bin_pins_the_resolved_target_not_the_symlink` built
  a fake Homebrew cellar under `tmp_path` and expected `_trusted_bin` to accept it; on this
  host `TMPDIR` sits under `~/.kiro/crew/scratch`, so the production check that refuses
  anything under `Path.home()` correctly refused the fixture, 5/5, and the same test is
  green on CI and macOS only because their temp roots are outside home. The fix pins the
  home root at the seam the product reads (`monkeypatch.setattr(Path, "home", ...)`) to a
  sibling that is not an ancestor of the fixture, and the mirror test
  `test_trusted_bin_refuses_target_under_home` plants INSIDE the patched home so the
  refusal is proven rather than dodged.
- **A wall-clock complexity guard measures the scheduler, not the algorithm.**
  `test_argv_floor_inline_and_brace_scope.py`'s linearity check timed 3k against 12k
  braces and bounded the ratio at 8x; under sixteen workers it read 9.3x once with the
  algorithm intact. The class-5 fix (above, "Absolute time budgets on instrumented runs"):
  the test now installs delegating stand-ins on the two module globals the expansion loop
  calls (`_brace_pairs`, `_glob_to_regex`) that hand each primitive a counting `str`, and
  asserts the WORK -- 4x the braces may read at most 6x the characters. A planted
  per-`{` suffix rescan reads 16x and fails it at any size, on any host; the sibling
  `monotonic() < 1.0` guard in the same class became a structural bound on the emitted
  regex's alternation-group count. The two clocks that remain are the base's own bounds
  (12k braces under 30 s, the alternation run under 1 s) read on THREAD CPU at their
  original constants -- a strictly tighter reading than the wall clock they replace, and
  one a sibling worker's slice cannot inflate -- each read ONCE, on the cold call: a
  repeat would time `re`'s compile cache rather than the translator, and the cheaper of
  two readings would let a stall that reproduces intermittently pass.
- **A helper that counts WARNINGs over unfiltered `caplog.records` asserts on every thread
  in the worker.** `test_taskq_runner_adapter.py`'s `_one_warning` demanded exactly one
  record while the test had scoped `at_level` to `kiro_crew.taskq.adapters.runner`; the
  second record was `kiro_crew.sandbox`'s mount-source sweep, finishing on the shared
  `mc-maint` executor seconds after the `SessionManager` test that armed it had torn down
  (`close_all()` cancels the asyncio task but cannot stop an executor thread mid-`/proc`
  scan). Two fixes, in two places: the helper filters `rec.name == _RUNNER_LOGGER`, and the
  rootdir conftest's autouse `_no_boot_sandbox_sweep` pins
  `kiro_crew.session.cleanup_stale_sandbox_profiles` to a no-op for every test (a test of
  the sweep itself patches the same name inside its body and so still wins). Measured
  over `test_session.py` alone: 138 real sweeps on `mc-maint_*` threads without the pin, 0
  with it.

#### The classes that were quiet but real

- **`spawn_no_cwd`, ranked by what was reached.** Real `gh` (a live token), `ssh` (the
  host's agent socket and config), `aws configure`, `systemctl is-active` and `mise` were
  reached from unit tests every round, and ~125 spawns per round ran with the CHECKOUT as
  process cwd. The causes were resolver seams the tests had not pinned, in four shapes:
  a class-local copy of the `_public_dns` fixture that re-implemented the DNS pin and
  DROPPED the sibling `_gh_prefers_ssh` pin (so `gh config get` ran for real); a
  `functools.lru_cache`d `ssh -V` probe behind `_build_launcher_script` that whichever
  launcher-building test ran first paid (six sandbox files, one autouse pin each, the
  house pattern from `test_sandbox_argv.py`); the doctor pinning only `shutil.which` while
  its sections resolved through `platform_compat.trusted_aws_bin` / `trusted_system_bin` /
  `trusted_git_bin`; and `code_fingerprint()`'s lru_cache deciding WHICH test pays a real
  `git` on the developer's checkout, so fixing the flagged tests only moved the spawn to
  the next reader -- the pin has to be module-scoped with an explicit opt-out fixture. For
  the `git -C <scratch>` production spawns a test cannot pass `cwd` to,
  `monkeypatch.chdir(tmp_path)` in the fixture closes the process-cwd hazard; adding
  `cwd=` to the product would turn git's rc=128 for a missing clone into a Python
  `FileNotFoundError` before git runs, which every caller reads off `returncode`, so that
  descriptor is deliberately left. One production change: `browser_cli/install.py`'s
  staged node launcher now runs from the managed leaf its docstring already promised, and
  `test_browser_cli_install.py` pins the `cwd` it is handed.
- **`fd_leak` at +3 per unclosed SQLite handle, and why refcounting never reclaims it.**
  On CPython 3.11+ every `sqlite3.Connection` is a self-cycle through its `lru_cache`
  statement cache, so an unreferenced connection holds `db` + `-wal` + `-shm` until the
  cyclic collector runs -- 52 tests at +5..+9, stable across all five rounds. Three owners:
  `SkillsLoader` (its `SkillSearchIndex` and `skill-catalog-refresh` thread), `SubagentManager`
  (its durable task queue), and `KnowledgeStore`, whose per-thread connections had NO close
  path for another thread -- the fixture's `close()` released the loop thread's and left
  the three that `asyncio.to_thread` workers opened at dashboard start. The store gained
  `KnowledgeStore._close_all_for_tests()` -- test-private on purpose: production has no
  moment at which every thread is provably idle short of process exit, so it gets no
  consumer there -- built from a module flag (`_ALLOW_CROSS_THREAD_CLOSE_FOR_TESTS`) that
  the rootdir test conftest flips once per session, under which a connection opens
  `check_same_thread=False` through a factory that re-applies the thread-affinity guard in
  Python on every statement entry point the store uses (`cursor`, `execute*`, `commit`,
  `rollback`), leaving only `close()` cross-thread, and is registered for the teardown close, while
  production keeps SQLite's native guard, registers nothing and refuses the seam; plus a
  generation counter so a thread whose handle was closed elsewhere reopens lazily; `close()` keeps its per-thread contract, which
  `test_knowledge_cross_thread.py` pins, and
  `test_close_all_releases_every_threads_connection_and_reopens_lazily` pins the seam),
  and `VectorMemoryStore.init()` on an already-open store now closes the handle it replaces
  instead of orphaning it (`test_a_repeated_init_closes_the_handle_it_replaces`; the CLI's
  `_learn` re-inits the store it is handed, so every learn test paid this). Tests route
  every inline construction through the rootdir conftest's `opened` register-and-close
  fixture (one definition, not one per module: a `KnowledgeStore` is closed through its
  test-only every-thread seam, everything else through `close()`), modules that build
  `ContextBuilder`s request the conftest's opt-in `close_skills_loaders` from a one-line
  module autouse fixture, and managers get `manager.close()` before `sessions.close_all()`.
  The one descriptor the probe still sees
  is pytest-asyncio 0.20.3's replacement loop: +3, one at a time, reclaimed at the next
  test's setup -- a float, not a leak, and never the thing to chase.
- **A cancelled inline reap is a backend nobody owns.** `mcp_gateway/gatewayd.py`'s stub
  disconnect `finally` awaited `orphan.shutdown()` inline; daemon teardown cancels those
  connection handlers, and the cancel landed AFTER `release_exclusive` had dropped the
  backend from the exclusive map, so it was outside `shutdown_all` -- the child exited on
  its own, SIGKILL escalation never reached it, and its pipe transports were still
  finalizing when the test's loop closed (+16 descriptors on the storm test). The reap is
  now `pool.spawn_shutdown(orphan)`, a tracked task `shutdown_all` joins, so
  `process.wait()` -- which asyncio resolves only once every pipe is closed -- completes
  before the daemon returns.
- **`heavy_rss`: one emoji makes CPython store a file at 4 bytes per code point.**
  `test/source_corpus.py` memoised every file's text AND an NFKC copy; 1,262 of 1,702
  modules under `src/` contain a non-BMP character, so a 62 MB tree became +212 MiB per
  worker before a single parse, and every fast narrow gate that touched the corpus
  inherited that high-water mark (eight ratchets at 202-263 MiB, just over the 200 MiB
  threshold). The corpus now streams -- read, normalise, filter, parse, drop, one file live
  at a time -- and caches only the sorted path list; the public API is unchanged and
  `test_source_corpus.py` pins it. In-file maximum RSS delta across those modules is now 22
  MiB, and `_release_source_corpus_after_module` releases a megabyte of `Path` objects, not
  160 MB of text. Each of the eight ratchets was re-proven against a planted violation
  before the change was believed. (A behaviour-identical speed-up of
  `scripts/check_lockdown_before_publish.py` found on the way stays out of this pass: it fixes
  no red and no leak.) The `_GIB_PER_WORKER` reservation model measured earlier
  in this document ("one `tree_scan_*` group alone retains ~1.3 GiB") predates this change
  and is to be re-derived, not restated, on the next pass.
- **`kill` at a pid the body has already proven dead.** Fifteen of seventeen signals were
  identity-pinned at the test's own children and left alone. `test_pid_lifecycle.py`'s
  `_reap` was the exception: a `finally` that SIGKILLed the RAW root and grandchild numbers
  after production had reaped the root and init had collected the grandchild, so on every
  passing run 19 of its 33 kills went to a number with no `/proc` entry -- whoever holds
  that number next. `_reap` now records `process_start_time` at spawn and signals only
  while it still matches, refusing an unpinned pid outright (0 freed-number kills after);
  and `test_kill_pid_nonexistent_is_safe` asserts its probe pid exceeds
  `/proc/sys/kernel/pid_max` so the kernel's range check, not convention, is what makes
  the number unreachable.
- **`network`: rank the address before you file the finding.** The UDP `connect` to
  `198.51.100.1:53` / `2001:db8::1:53` in `argv_floor._own_interface_addresses` is the
  packet-less local-IP trick over TEST-NET, a hardening item: `test_security.py` now routes
  it through an inert `_InertDatagramSocket` at the module's `socket` binding (datagram
  construction only; every other socket kind passes through) that records the peer and
  answers a documentation address from `getsockname`, and asserts the peers it WOULD have
  used -- rewriting the production peer to `8.8.8.8` fails it by name. The AF_UNIX connect
  to `./dashboard/app.sock` was the TEST: it dodged the `sun_path` cap with
  `monkeypatch.chdir` and a relative root of `.`, which no production caller ever spells;
  the rootdir conftest's `short_sock_dir` passed as an ABSOLUTE root is the fix.
- **`thread_leak` was named pools, again, and is left alone.** Every surviving thread in
  the class was `mc-embed_*`, `mc-recall_*`, `mc-pathres_*`, `mc-maint_*`,
  `mc-discovery_*`, `mc-subproc_*` or `skill-catalog-refresh` -- the last of which now
  exits when its `SkillsLoader` is closed. The `mc-maint_*` rows were the same boot
  sandbox sweep the caplog red exposed; the conftest pin removes the work, not the pool.

#### What not to re-derive

`production git -C <path>` helpers (`auto_improvement`'s backend and spine,
`code_fingerprint`, `cli_doctor`) pass `cwd=None` on purpose (see `spawn_no_cwd` above),
and `green_age.run()` is a CLI whose contract is "git in the invoking cwd", so its ~150
descriptors per round will stay flagged until the probe records the PROCESS cwd alongside
the `cwd` kwarg -- with the fixture's `chdir` under `tmp_path`, none of them ran in the
checkout. `push_guard.py`'s two direct `git patch-id --stable` calls bypass that module's
`_GIT_CMD` injection seam (its docstring's "every command goes through `run()`" is false
for them) and want a separate fix. `history_search`'s lazily opened `SessionSearchIndex`
has no close path, so `search_chat_history` leaks one handle per call until GC (+3, below
threshold). The dashboard files' 731 `aiohttp` warnings are class 15 (`app[...]` writes
after start, ~24 sites), not this pass.

## Running the suite: the defaults, and how to narrow safely

The checkpoint run before a commit is the change-related set on both surfaces,
with a bounded worker count -- the full suite is CI's job:

```bash
python3 scripts/local-gate.py
```

The whole suite with the configured defaults is a human's run, not a gate:

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

### Opt-in Windows CI progress records

The Windows test job loads `scripts.ci_pytest_progress` explicitly with `-p` and
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

**The Linux/Windows budget reserves 3 GiB per worker; the Linux wide-run
measurements below were 1.8–2.8 GiB. macOS reserves 16 GiB after measured
14.9–16.1 GiB workers.** Without the budget, `-n auto` would ask for one worker per
core. In the Linux measurement almost all of the fixed part was *collection*: every
xdist worker collected every testpath — 106,491 items — at ~1,499 MiB of peak RSS
before running a test, 99% of it private. From there a worker grew another ~60 MiB per
1,000 tests, and that growth did not saturate.

Both numbers were remeasured in the fourth five-run pass and both had roughly DOUBLED
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

That is why `xdist_budget.py` reserves 3 GiB per worker on Linux and Windows,
and 16 GiB on macOS. The 3 GiB value is ~1.1× the measured Linux worst-case peak at
`-n 12`, and the worker count where the budget binds is far lower than that. On
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

Every layer is live: the item tree, the closures and the rewritten modules are
retained for the whole session by design, so none of the floor is reclaimable.

So the full suite genuinely needs multiple gigabytes. On an 8–16 GiB Linux or Windows
laptop with a browser open it may not fit; on macOS the 16 GiB reservation often clamps
a run to one worker even on larger hosts. The budget in the rootdir conftest works this
out and clamps `-n auto`, naming the active platform reservation. A Linux/Windows example:

```
xdist worker budget: 1 of 10 workers (3.0 GiB free, 16 GiB installed). Each worker
needs about 3 GiB, mostly to collect the suite. A run this narrow is slow, not
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

**Narrow by FILE, not by `--splits`.** `--splits/--group` — pytest-split, retained
for macOS — deselects *after* the session has collected everything, so a 1-of-4
item shard still pays the whole floor in every worker while running a quarter of
the tests. Measured: 14,237 of 56,946 items selected, 744 MiB peak, which is the
unsharded floor. Linux and Windows CI instead use `scripts.ci_file_shards` to
assign whole files before import; each worker collects only its shard's files.
For local work, pass the specific files relevant to the change.

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
| Checkpoint before committing | `scripts/check_black_formatting.py && scripts/check_subprocess_encoding.py && isort && flake8 && mypy && python3 scripts/local-gate.py` (related tests; the full suite is CI's) |

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

Synthetic process trees must also give the owner a synthetic PID. Mixing
`os.getpid()` with fixed child PIDs can overwrite the owner's namespace when a
container assigns the worker one of those child PIDs. Replace the tested module's
`os` binding with a local proxy; never change the shared stdlib `os.getpid`.

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

Being pinned is not sufficient, which is why that file carries a **second** ratchet:
a bare `monkeypatch.undo()` in a pinned module's test body also reverts the
fixture's two pins, because pytest hands the test function and every fixture it
requests the SAME `monkeypatch` instance. Everything after that line reads the
runner's real free memory — the file is pinned, reads as pinned, and is not pinned
where it matters. Measured on a macos-15 nightly backend shard reading 2.58 GB
available, under the 4.5 GB floor: `test_taskq_admission_integration.py`'s
post-pressure drain deferred the row a second time and failed as
`assert 'queued' == 'starting'` — nothing in the traceback named memory, and the
whole nightly publish chain skipped behind it. Scope the patches a test wants
reverted with `with monkeypatch.context() as scoped:` instead, so leaving the block
restores the fixture's readings rather than the host's.

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

Two snapshots from different kernel accounting sources are this class too. Compare them
with a bounded, measured slack, and keep allocation-growth observations in the failure
message because a long-lived allocator may serve a probe from resident memory. Set the
allowance above observed counter drift but far below unit/scale errors or a real
multi-megabyte inversion.

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
  `test_subagent_scale.TestDurableQueueScale::test_queue_survives_manager_loss_and_drains_fifo`
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
  ceiling, a span the test measures itself.
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
  flight=True`.

- **A PRODUCTION write budget sitting on the assertion's path.** `tool_risk._record_outcome`
  bounds its off-loop append with `asyncio.wait_for(to_thread(_log.append, row), 0.05)` and
  returns `None` -- no badge -- when the budget expires; that is the product's contract, and
  it is deliberate. But a test that asserts `record is not None` and reads the row back is
  then ALSO asserting that a new-file `CreateFile` + lock + write beats 50 ms on the host.
  It does on a warm Linux host (0.4 ms measured); on the Windows shard the same five tests
  (`test_decisions_tool_risk`, `..._end_to_end`) came in `None` on 26 unrelated heads in two
  days, green on every rerun. Reproduced on Linux by pricing `_log.append` at 60 ms alone
  (8 of 40 fail, every run). The fix is in the FIXTURE, not the constant: lift the budget
  to a lost-run ceiling under the module's `--timeout` (20 s) for every test whose subject
  is the record -- raised, never removed, so a wedged writer still fails by name -- and pin
  the budget's own branch with the value set to `0`, which expires before the append is
  even dispatched and so carries no clock. The e2e file's `_generous_append_deadline`
  fixture is the shape: it lifts all three budgets on that path (`_APPEND_TIMEOUT_SECONDS`,
  `gate._LOG_BUDGET_SECS`, `tool_risk.LOG_BUDGET_SECS`), because lifting one leaves the
  assertion racing the next.

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
(20 s under a 30 s mark) and say so where it is defined.

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
place to be finding it.** `ci.yml` assigns whole files to Linux/Windows shards
before import (macOS retains `pytest-split` groups), and a leaker only damages tests
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
at its original constant, read on thread CPU: a ratchet may only tighten. A *timed*
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

For a regex — no instrumentation surface at all — use `test/conftest.py`'s
`assert_rejected_without_backtracking(reject, build_pump)`, and note what the mutation
showed: a shared character between two adjacent quantified classes in the marker grammars
is not polynomial but EXPONENTIAL (doubling per pumped character; 4.3 s at 24), so the
200 000-character pump the old guards used would never return under a regression and the
worker would die at `--timeout` (class 6) — and no single "small" size is safe either: a
harsher mutant grew ~8x per pumped block. The helper therefore RAMPS the pump one unit
at a time from 1 to 24, on thread CPU, failing at the first size that overruns its
budget (so catching any regression costs about one growth step), and only then tries
ascending long pumps for the polynomial class. Size any complexity guard so the
regression it exists to catch FAILS it, not hangs it.

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
(`git stash` / run / `git stash pop` / run); a number from an idle machine measured an
hour earlier is not a baseline.

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

`ci.yml` assigns the backend suite to eight whole-file shards on Linux and Windows
using `scripts.ci_file_shards`. Ownership is SHA-256 of the root-relative POSIX
path, not a duration or test-count balance. Other shards skip the file before
import, while discovery patterns and platform ignores remain pytest's own.

macOS retains three `pytest-split` groups. That plugin uses recorded runtime only
when `.test_durations` is available, otherwise it falls back to test count.
`test-durations.yml` remains the optional duration-recording workflow; its output
does not affect Linux/Windows file ownership. Linux-recorded durations must not be
assumed to balance macOS.

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
