# Specialised traps (search when your test touches X)

Moved verbatim from the writing-tests `SKILL.md` core (Checklist).

- [ ] Nothing outlives the run: no temp residue, no write to `~/.kiro` or the real data
      home, no cron job, no service change, no file in the checkout
- [ ] Every `mkdtemp` has `addCleanup` on the next line (or uses `tmp_path`)
- [ ] Every child that may create a file gets `cwd=` under `tmp_path`, and every
      assertion is scoped to where that child actually ran
- [ ] Anything whose default directory is `Path.cwd()` (`TaskRunner(work_dir=...)`) is
      constructed with an explicit `tmp_path`; a gitignored name at the repo root is the
      one leak the residue guard cannot see
- [ ] A background worker gets every environment-derived input (paths AND config) from the
      dispatching thread, and registers itself so the rootdir conftest teardown can join it
- [ ] Every thread, task, child process, socket and connection it starts is stopped in a
      `finally` or an `addCleanup`
- [ ] Globals mutated through `monkeypatch`, never raw assignment — including `os.environ`
      keys a REAL production startup path is known to write (`KIROCREW_TELEMETRY`,
      `PATH`), restored in the shared helper that drives it
- [ ] A variable the code under test WRITES and that is absent before the test goes
      through `forget_env_at_teardown(monkeypatch, name)` — `delenv(raising=False)` on an
      absent key records no undo, and a `delenv` AFTER the write puts the written value back
- [ ] No `monkeypatch.undo()` in a test body: it reverts every fixture's records too. A
      patch you need to drop early lives in `with pytest.MonkeyPatch.context() as patched:`
- [ ] A fake of a host condition (default home, platform, privilege) lives on the tests
      that need it, not a shared fixture; adding a production check on that condition, or
      removing the fake, comes with a `git grep` for every test that reaches the check
- [ ] No module-level `skipif` probe that reads `KiroCrewConfig` / `config_dir()` /
      `Path.home()` — it runs before any pin and observes the operator's real config
- [ ] A collection-time probe that can construct a process singleton (`sel()`, a config
      loader) RETIRES it before its temp home is removed — the session floor only resets
      singletons at the first test's setup, and a live one re-creates the deleted path
- [ ] A constructor-less `AcpSessionHandle` comes from `bare_session_handle()`
      (`test/session_handle_helpers.py`), never `AcpSessionHandle.__new__` — a bare
      handle built by hand breaks on merge when a handle method starts reading a new
      runtime attribute (`test_bare_session_handle_guard.py` enforces it)
- [ ] Every `MagicMock` attribute the code under test converts or compares (`int()`,
      `len()`, `bool()`, `<`) is set explicitly in the mock helper — `int(MagicMock())`
      is 1, and a drain loop reads that as "one still pending" until its deadline. A
      test whose duration equals a production timeout has hit this
- [ ] A complexity guard (ReDoS, "stays linear") is sized so the regression it exists to
      catch FAILS it inside `--timeout` rather than hangs the worker: RAMP the pump one
      unit at a time on thread CPU (`assert_rejected_without_backtracking`), never a single
      huge input on a wall clock — no fixed "small" size is safe against every growth rate
- [ ] A test of a refused location names one the guard refuses on THIS host: `/usr` is
      `C:\usr` on Windows — accepted by the resolver, and then created on the system drive
- [ ] Fixture paths are absolute on EVERY host: `host_abs("usr", "bin")`, never a `/usr/bin`
      literal (`ntpath.isabs` rejects a driveless path from Python 3.13); a path that belongs
      to a simulated platform is judged with that platform's module (`posixpath`)
- [ ] Nothing assumes the ancestry of `tmp_path` is bare (no `.venv`, no project marker
      above it), that `127.0.0.1:1` refuses connections, that `python3` is on PATH (spawn
      `sys.executable`), or that `git`/`gh` sit in a trusted system directory
- [ ] A fabricated child environment does not substitute `HOME` while inheriting `PATH`
      (or vice versa): with a shim-led `PATH` the bare `python3`/`node` is a
      version manager that finds no tool state under the new `HOME` and spins forever
- [ ] Every child that could outlive the test is reaped by process GROUP
      (`start_new_session=True` + `os.killpg`; `taskkill /T /F` on Windows) from a
      `try/finally` around the whole post-spawn body, not only the timeout branch
- [ ] A `skipif` on an external tool gates on the VERSION floor the code needs, not just
      `shutil.which(tool) is not None`
- [ ] A gate that walks the REPO ROOT prunes `.worktrees/` (another branch's checkout),
      or asks `git ls-files` instead of walking
- [ ] A fixture that stubs away the only code path releasing a permit, lock or in-flight
      claim gives it back itself — restored to what the test INHERITED, not to a
      pristine value
- [ ] A source ratchet strips docstrings with `ast`, not by subtracting `__doc__` from
      `inspect.getsource` (3.13 dedents docstrings)
- [ ] A ratchet or count test survives two changes merging untested: its record is one
      line per file, site or change, never one shared total that two changes would
      both rewrite to the same number; a budget grows by per-change rows holding the
      change's own delta, not the amount over (testing-conventions § Merge-safe
      ratchets and counts; the flake-pattern ratchet's K21 refuses a new count pinned
      `==` a literal or a `_BASELINE`/`_TOTAL`-style constant in a tree-scanning test)
- [ ] No change detector: a test never hand-copies a registry's size or members, a
      workflow's steps or a module's imports and compares the copy; it derives the
      set from source, asserts each member's property and that the set is non-empty
      (testing-conventions § No change-detector tests; K22 / J7 refuse a new pin)
- [ ] A test whose contract IS a real symlink is listed in `test/requires-real-symlinks.txt`;
      one that needs a directory that resolves elsewhere uses `make_dir_link`
- [ ] No `AsyncMock` standing in for a synchronous method; every `cancel()` awaited. A
      bare `AsyncMock()` provider makes EVERY accessor an awaitable, and the sync ones
      (`context_window_tokens()`, `mcp_session_report()`) become `never awaited` warnings
      attributed to a LATER test — build the double from a factory that pins them as
      `MagicMock`, and a stand-in for a spawner or `wait_for` must `close()` the coroutine
      it swallows
- [ ] No module-level asyncio primitive (`Lock`/`Event`/`Future`/in-flight dict) reachable
      from the code under test without a per-test reset
- [ ] Nothing can block forever: every await the test itself must unblock is wrapped in a
      bounded `wait_for`; no `sleep(0.05)` standing in for "let the other task register"
- [ ] A test of a coalescing / single-flight path holds the shared operation open (a
      gated stub) until every reader has registered, then releases it — an instant stub
      lets the leader finish before the others arrive, and a done future never yields
- [ ] A module that `rglob`+`ast.parse`s `src/` once per module also carries
      `pytestmark = pytest.mark.xdist_group(name="tree_scan_<module>")`, or every xdist
      worker it touches re-runs the scan
- [ ] A scan of the WHOLE repo goes through `source_corpus.repo_files()` /
      `repo_files_named(...)`, never `rglob`/`os.walk` from the root — a walk descends
      gitignored trees and any nested worktree, so the gate reports that copy as the
      offender, or (with an `any(...)` assertion) keeps passing on it; the gate keeps its
      own scope filter, because `_vendor` is tracked
- [ ] A fixture stamped from a module-level `NOW` is only compared by production code
      whose clock is pinned to that same `NOW` on the module-under-test's own `time`
      binding (D2) -- never two clocks
- [ ] After `await handler(...)`, an assertion on something a worker thread emits via
      `call_soon_threadsafe` waits on that signal, not on the handler returning
- [ ] No upper bound on a rate, a sample count, or a measured duration beyond what D7
      allows
- [ ] Source files read via `_REPO_ROOT = Path(__file__).resolve().parents[N]`, never a
      relative `Path("src/...")` — xdist workers may change CWD
- [ ] Passes at `-n0` **and** under `-n auto`, and passes when run alone
- [ ] No large allocation at module scope — especially not inside `parametrize`, where
      every worker pays it at collection and holds it for the session
- [ ] Cross-platform: `platform_compat` for process/signal/lock calls, no assumption
      about path separators, case sensitivity, `/tmp`, or timer granularity
- [ ] `@pytest.mark.asyncio` only on `async def` tests — never a module-level `pytestmark`
      over a file that also holds sync tests
- [ ] Every aiohttp `app[...]` write happens before the `TestClient`/`TestServer` starts; an
      override of something a production `on_startup` hook creates is itself a later
      `on_startup` hook; an already-set-up runner's app is never wrapped in a second
      `TestServer` (serve `runner.server` through a `ServerRunner`)
- [ ] A fixture venv is built with `symlinks=not IS_WINDOWS`, and every probe of the
      RUNNING interpreter's packaging (`find_spec("pip")`) is pinned — a uv venv has no
      `pip`, and a copied python-build-standalone binary does not start
- [ ] A fake `subprocess.run` routes on whole argv tokens, never on a substring of the
      joined argv — a `TMPDIR` path element can contain any word, including the test's id
- [ ] A directory chmodded to a non-writable mode under `tmp_path` gets owner rwx back in a
      finalizer; a nested pytest the test spawns gets `--basetemp` under the outer
      `tmp_path` (the default is the SHARED per-user tree every other run prunes)
- [ ] A module- or session-scoped fixture that patches env does it through
      `pytest.MonkeyPatch.context()` scoped to what actually needs it, never a raw
      `os.environ[...]` write and never a `MonkeyPatch` held for the whole module when the
      value it builds is already materialised; an env value each test's subprocess must
      see (a git identity) is set per test through the function-scoped `monkeypatch`,
      since a session-lifetime patch still reaches every later suite on the worker
- [ ] A default that is not HOME-derived (`workspace_root()` → `/Volumes/workplace` on
      macOS) is relocated by patching the resolver, and the result is asserted to be under
      `tmp_path`
- [ ] A resolver that finds a per-user tool (`gh`, `mise`, `say`) is pinned at the seam
      production reads, never left to find the host's binary; an address probe that
      `connect`s a datagram socket off-loopback is stubbed with an inert socket subclass
- [ ] A stub for a runner that owns deferred cleanup (`cleanup_paths`) reaps them itself;
      a file with a `.lock` sidecar lives under `tmp_path`; a spawned real binary gets
      `TMPDIR`/`TMP`/`TEMP` inside the test's temp tree in its `env`
- [ ] A thread count that rises is only a leak if the new threads are not a named bounded
      pool (`mc-*`, `sel-writer`) — print thread names in the probe first; a
      `Thread(target=lambda: ...)` that is expected to raise captures the exception and
      asserts on it instead of leaving a `PytestUnhandledThreadExceptionWarning`
- [ ] A tree ratchet streams parsed trees and caches RESULTS, never a list of ASTs shared
      between scanners; a growth/linearity ratchet measures the algorithm's own work
      (bytes produced, items visited), not wall-clock and not interpreter call counts
      (`str.join` is one C call at any length)
- [ ] Nothing assumes `tmp_path` is OUTSIDE a git repository: a fixture whose verdict comes
      from an upward walk (nearest `.git`, `install.sh` + `setup.cfg`, project markers from
      the cwd, git discovery) plants the boundary it reads — a developer's `TMPDIR` may sit
      inside the checkout, and in a linked worktree the `.git` such a walk finds is a FILE
- [ ] A nested `python -m pytest` on files under `tmp_path` passes its own `-c <ini>`,
      `--rootdir` and `--color=no`: otherwise it adopts the repository's ini, and the
      repo `addopts` reshape the very output the outer test greps
- [ ] A stub for an `os.*` function reached through a module alias
      (`monkeypatch.setattr("<mod>.os.unlink", ...)`) forwards every non-owned call to the
      saved real function with `*args, **kwargs` INTACT — dropping `dir_fd` re-aims pytest's
      own `rmtree` at the cwd — and asserts a bare relative name never reaches the stub
- [ ] A test that compiles or imports a throwaway source under `tmp_path` keeps its bytecode
      there (`sys.pycache_prefix` under `tmp_path`, or the suite's `no_bytecode()` helper),
      because the suite-wide mirror is keyed on the absolute source path and a per-run path
      never gets read again
- [ ] A fixture needing a SHORT path (`AF_UNIX` `sun_path`, a redaction-sensitive path) calls
      `tmpdir_helpers.short_tmp_base()` — never a literal `/tmp`, and never
      `tempfile.gettempdir()`, which is too long under a pinned `TMPDIR`
- [ ] No raw `os.kill(pid, 0)` in test code — it TERMINATES the target on Windows; route
      liveness through `platform_compat.pid_exists`/`pid_liveness`, and any teardown signal
      to a pid published by a child captures the target's identity at spawn and revalidates
      before signalling
- [ ] Every fetch seam a code path can take is routed, not just the one the happy path uses
      (a JSON + text stub still lets a BLOB fetch reach the network)
- [ ] A skip condition is decidable the same way on every run of one host: no ctime tick, no
      "did a real tool answer in time", no "did an earlier test in this worker arm a hook" —
      build the condition, resolve it from disk, or measure in a fresh interpreter
- [ ] A handle the object under test opens for the process lifetime (a store's SQLite
      connection + writer thread, a lazily-opened index) is closed by the fixture that built
      it; when production has no close path, that gap is the finding
- [ ] No exception INSTANCE in a `parametrize` list (`pytest.param(OSError(...))`): the
      instance lives for the module, `raise err` hangs a `__traceback__` on it, and the
      frames on that traceback keep every local alive — a handle, a lease, a socket — for
      the rest of the worker. Parametrize the errno and build the exception inside the test
- [ ] A test that asserts process-global "nothing retained" state (`lease._held`, a
      registry, a pool) is only as good as the files that share the worker: the file that
      exercises the failure path pins the same table empty in its own teardown, so the
      retention is reported where it was created rather than forty files later
- [ ] "Let it finish" is never a fixed `sleep`: wait on the state you are about to assert
      (poll it off-loop under a bounded deadline, or await its event) — two 200 ms sleeps
      that were enough at `-n0` read `starting` for every row on a loaded Windows worker
- [ ] An unclosed `sqlite3.Connection` is a self-cycle on 3.11+ and refcounting never frees
      its `db`/`-wal`/`-shm`. Close every inline `VectorMemoryStore` / `KnowledgeStore` /
      `SkillsLoader` / `SubagentManager` as the SQLite teardown item below says, e.g. through
      `test/conftest.py`'s `opened` fixture. A `KnowledgeStore`'s `close()` is fine on the
      owning thread; use `_close_all_for_tests()` when another thread held a connection
- [ ] An exact bound on monotonic float arithmetic (`(now + cap) - now <= cap`) can miss by
      one ulp on some platforms; compare with a named slack (`_FLOAT_SLACK = 1e-6`, as in
      `test/test_knowledge.py`) or `pytest.approx`, never a bare `<=`
- [ ] A fixture that plants a path under `tmp_path` and asserts a production "not under
      `$HOME`" refusal does NOT fire pins `Path.home` (the seam the product reads) to a
      sibling that is not an ancestor of the fixture — a developer's `TMPDIR` may sit inside
      home — and a mirror test plants INSIDE the patched home to prove the refusal still fires
- [ ] A helper that asserts a WARNING count filters `caplog.records` by the logger the test
      enabled (`rec.name == <logger>`), never the unfiltered root capture: an executor thread
      another test's `SessionManager` armed logs on its own logger mid-test; and a test that
      builds a real `SessionManager` relies on conftest's `_no_boot_sandbox_sweep` pin rather
      than patching the sweep itself unless the sweep is its subject
- [ ] A teardown that signals a pid the body has already proven dead (a reaped root, a
      grandchild init collected) signals only while `process_start_time` still matches the
      identity recorded at spawn, and refuses an unpinned pid; a "nonexistent pid" probe uses a
      number above `/proc/sys/kernel/pid_max`, not a convention
- [ ] A resolver the product caches for the process (`functools.lru_cache`d `ssh -V`,
      `code_fingerprint()`) is pinned at MODULE scope with an explicit opt-out fixture — pinning
      only the flagged tests moves the real spawn to the next reader; a class-local copy of a
      conftest fixture re-implements ALL of its pins or requests the original instead
- [ ] A product change that ADDS a module-level cache, memo or singleton on a path other
      tests drive (`read_audit._loaded`) resets it in `test/conftest_resets.py`, not only in its
      own test file; a test that counts calls on a cached path pins that cache to the state its
      count assumes (`monkeypatch.setattr(read_audit, "_loaded", None)`)
- [ ] A production `git -C <scratch>` spawn the test cannot pass `cwd=` to runs under a
      fixture `monkeypatch.chdir(tmp_path)`, so nothing inherits the checkout as process cwd;
      the product keeps `cwd=None` there on purpose (a missing clone must stay git's rc=128,
      not a `FileNotFoundError` before git runs)
- [ ] A backend a daemon reaps on disconnect goes through the pool's TRACKED reap
      (`pool.spawn_shutdown`), never an inline `await shutdown()` in a handler's `finally`:
      teardown cancels the handler after the backend has left the map, and the child then
      belongs to nobody
- [ ] A child whose environment or argv the test must READ BACK through the kernel
      (`KERN_PROCARGS2`, `/proc/<pid>/environ`) is a non-platform binary -- the test's own
      `sys.executable` -- never `sleep` or `env`: macOS 26 answers an argv-only record for
      Apple platform binaries even to a same-uid reader, so the oracle reads `None` and a
      "refused" assertion passes for the wrong reason
- [ ] The bounds on nested `python -m pytest` runs fit INSIDE the per-test `--timeout`: N
      sequential children each capped at 60 s inside a 120 s test can only fail the test
      ahead of pytest-timeout, never protect it; independent children run concurrently
      (wall = the slowest) and each gets the outer budget less a margin
- [ ] A handle the product REWRITES asynchronously (a claim's `.task` that the run swaps for
      its inner task) is never read off shared state after an awaited response -- take it
      from the seam that hands it over (`attach_run_task`), or the assertion names whichever
      task won the race
- [ ] A fixture never answers a production path with a FIXED absolute host location to keep
      a golden host-free: the product WRITES into the window it is handed (`record_owner`
      unlinks `.owner`, the gate probe `shutil.rmtree`s it), and a path that "never exists"
      is one `mkdir` on some host away from a real deletion -- answer a real directory under
      `tmp_path` and stub the golden's env contribution instead
- [ ] A test whose red survives neither running the file ALONE nor running it WITHOUT
      `-p probe_plugin` is the probe's finding, not the suite's: a per-test observer that
      reaches `os.path` through the module a spied test patches records itself as the code
      under test
- [ ] A child that polices its OWN peak memory reads `/proc/self/status` `VmHWM` on Linux,
      never `getrusage(RUSAGE_SELF).ru_maxrss`: `execve` seeds `ru_maxrss` with the parent's
      high-water mark, so a child of a 2 GiB xdist worker (or gateway) reads over its ceiling
      before it has parsed a byte -- green at `-n0`, red under the suite; the test that pins
      it plants a BLOATED PARENT and asserts the child still finishes
- [ ] A test that plants a fake executable under `tmp_path` and expects a guard LATER than
      the working-tree fence to fire pins the fence root (`_WORKTREE_ROOT`) to a `tmp_path`
      sibling that is not the fake's ancestor -- under a repo-internal `TMPDIR` the fence
      wins first, and a patched `which()` that answers the fake for `git` too can hide it on CI
- [ ] A module that boots the REAL `start_dashboard` pins
      `hooks_integration._lifecycle_dispatcher` / `_route_registry` to `None` BEFORE the boot
      (so `monkeypatch` restores them); a module whose assertions assume "never booted" says
      so with an autouse pin. Attribute a flaky 409 by its BODY (`hooks disable failed: ...
      MagicMock ... await`), not by the endpoint that answered
- [ ] A PTY test that asserts a child receives a signal gates the send on
      `os.tcgetpgrp` on the PTY's own descriptor leaving the shell's group -- the line-discipline ECHO of the
      command is not proof the shell has read it (`VINTR` flushes unread input and the test
      passes with no child ever born) -- and reaps the session group in a `finally`; a spawn
      that hands a shell a terminal resets inherited `SIG_IGN` to `SIG_DFL` first, because a
      backgrounded launcher's ignored SIGINT survives `exec` into every descendant
- [ ] A module-scoped autouse fixture never wraps the module in
      `mock.patch.dict(os.environ, {...})`: the keys are live between tests for every module
      the worker runs meanwhile, while the function-scoped conftest floor already overrides the
      home during each body -- set the feature flag per test with `monkeypatch.setenv`
- [ ] A memoised per-file read on a tree scan (`lru_cache` on `_read_text`, a `tuple(...)`
      corpus helper) keeps every file resident for the process; the seam streams, and the test
      consumes the iterator (count, path set, `zip(strict=True)`) instead of materialising it
- [ ] A test's own `kiro-cli --version` is never the HOST's: every agent-spec write reaches
      `installed_kiro_cli_version()`, cached per process, so the module pins it to
      `SPEC_PERMISSIONS_MIN_VERSION` (house fixture) or the host's install decides the
      contract under test
- [ ] A coreutil a real-bash harness must neutralise (`sleep` in a retry backoff) is a shell
      FUNCTION defined at the top of the driver script, never an executable planted earlier on
      `PATH`: Git for Windows' `bin\bash.exe` launcher prepends `/usr/bin` to whatever `PATH`
      it is handed, so the shim never wins there and every failing case sleeps the whole
      budget; record each call and assert the SCHEDULE, not the clock
- [ ] A Windows Job `ActiveProcessLimit` sized as "this child and nothing else" is
      `1 + platform_compat.python_launcher_hops()`: a venv's `Scripts\python.exe` is a
      redirector that spawns the real interpreter as its child, so a ceiling of exactly one
      refuses the spawn it was meant to bound (exit 101, `Unable to create process using`)
- [ ] A test that asserts a key PASSES THROUGH a scrub (`HOME`, `PATH`) plants that key in the
      parent first — CI runners export `HOME` on Windows, a server session does not, and the
      assertion otherwise measures the host
- [ ] A refusal raised by an object that holds a process-global resource (a crew-log handle
      and its write lease) is asserted through a CLASS-based context manager whose `__exit__`
      copies the exception's fields into a plain record and returns -- never
      `pytest.raises(...) as exc` (the `ExceptionInfo` and the test frame form a cycle that
      keeps `append`'s `self` alive until the cyclic collector runs) and never a
      `@contextlib.contextmanager` (throwing into a generator adds the same cycle through the
      generator's frame); the file's autouse teardown pins the table (`lease._held`) empty
      without `gc.collect()`
- [ ] A stand-in handed to a table that judges liveness by probe (`RUNTIME_TENANCY._alive`
      reads `is_alive` / `is_process_alive`) either answers the probe or its test releases the
      claim itself -- a fake with neither is alive for the rest of the worker -- and a stubbed
      `_dispose_*` still owes every release the real one performs
- [ ] A test that hands the reaper a reset that never returns (`_hanging_reset`) pins
      `_RESET_TIMEOUT` the way its sibling classes do; a passing test whose wall time lands on a
      product constant (30.0 s, 60.0 s) with the CPU idle is spending that constant, not
      measuring it -- `classify.py`'s TIMEOUT-SHAPED rows name them
- [ ] `monkeypatch.delenv(key, raising=False)` on a key that is ABSENT records no undo: when the
      product then `os.environ.setdefault`s it, `setenv` the key first so the fixture's undo
      removes whatever the product leaves
- [ ] A harness that runs a real shell script SUPPLIES every tool the script requires that
      the host may lack (`jq` beside the `gh` and `curl` it already stubs) as a shell
      function in its `BASH_ENV` file, implementing exactly the invocations the script
      makes and failing loudly on any other -- never a `pytest.skip` on the missing tool,
      which is a loosened ratchet (`a-ratchet-may-only-tighten`); probe the host through
      the bash the script will run under, not `shutil.which`, and prefer the real binary
      where that bash has one
- [ ] A leaked child whose spawning thread is a NAMED pool reaper (`mc-*-reaper`) is the
      shared `executors` pool warming, not the test's child: the fix is at the pool (a
      `shutdown` that joins its reaper BEFORE the kill loop, so a tick past its stop check
      cannot refill the slot it just emptied) and at session end
      (`shutdown_maintenance_executor()` in a `test/conftest.py` fixture), never a reap in
      the first test that happened to trigger it
- [ ] Two writers creating one sidecar on a fresh directory open it `O_CREAT | O_EXCL` first
      and reopen without `O_CREAT` on `FileExistsError`: a nonexclusive `O_CREAT` can lose
      the create race on Darwin with a bare `ENOENT` for the leaf, and a `prune` or flush
      that swallows it silently skips its work (`_open_lock_sidecar`)
- [ ] A test double's pid (`4242`) is a shared name across hundreds of files, so any
      process-wide table keyed by pid (`runtime_ownership`'s leases and tenancy) is reset on
      BOTH sides of every test by a `test/conftest.py` autouse fixture; a kill-gate assertion
      that reads `refused` where it expects the failed-kill wording is a neighbour's lease,
      not the gate
- [ ] A race detector never parks on a `threading.Barrier(..., timeout=)` a correct
      interleaving cannot reach: the correct code then pays the whole timeout every run and
      pass is told from fail by elapsed time. Park on an `Event`, signal the arrival you are
      waiting for through the seam under test, and assert the event count while parked
- [ ] `monkeypatch.chdir` does not put a `cwd` on the spawn: the descriptor still says
      `cwd=None`, indistinguishable to a per-spawn audit from a spawn in the checkout. Pin the
      helper's seam instead -- `runner=partial(subprocess.run, cwd=...)`, or the script
      module's `subprocess` binding replaced with a namespace whose `run` carries `cwd`
- [ ] An object whose constructor opens SQLite (`SubagentManager` -> `tasks.db`,
      `KnowledgeStore` per thread, `SkillsLoader` -> the skill index) is closed through its
      production close path at teardown -- `test/conftest.py`'s `close_subagent_managers` /
      `close_skills_loaders` opt-in fixtures, `opened(...)`, or `_close_all_for_tests()` when
      ANOTHER thread held a connection (`close()` is per-thread) -- never left to the cyclic
      collector, whose timing is what makes the descriptor count flap between runs
