---
name: writing-tests
description: "How to write a Kiro Crew backend test that has NO side effects and does not flake. Use when adding, editing, reviewing, or debugging a pytest test in the Kiro Crew source repo: which conftest is under your file, what leaks (temp dirs, the real data home, ~/.kiro, cron, threads, child processes), how to tell which of the five flake classes you have and where each one's fix is written, and the cross-platform traps on macOS/Linux/Windows and arm64. Also covers diagnosing a residue failure and keeping the parallel suite fast."
triggers: write a test, add a test, fix a flaky test, test is flaky, test side effect, temp dir residue, tmp residue, kirocrew test, pytest kirocrew, test isolation, conftest, xdist, test leaked
repo_scope: src/kiro_crew
---

# Writing a Kiro Crew test that does not leak and does not flake

> **Scope guard: this skill applies ONLY to the Kiro Crew source repository** (or a
> worktree of it). Its rules are conventions of this repo's suite. In any other
> project, ignore it.
>
> The canonical, longer reference is
> [`docs/system-specs/common/testing-conventions.md`](../../../../../docs/system-specs/common/testing-conventions.md).
> If this skill and that document disagree, the document wins. What this skill adds is
> the decision order — what to check first, and what the failure looks like when you
> get it wrong.

## The two properties, and why they are one problem

A test must be **hermetic** (no effect that outlives it, anywhere but its own tmp dir)
and **deterministic** (same verdict every run, on every platform, in any order). They
are the same problem because the suite runs `-n auto --dist loadgroup`: a side effect
is not just untidy, it is *the input to another test on the same worker*, and it
surfaces as a flake in a file you never touched.

## Rule 0 — Know which conftest is under your file BEFORE you isolate anything

`setup.cfg` declares three testpaths and they do **not** get the same fixtures. The
table is in [testing-conventions.md](../../../../../docs/system-specs/common/testing-conventions.md)
§ Which conftest you are standing on — read it rather than a second copy here, which
would drift. The short version: only `test/` gets `test/conftest.py`; `transfer/` and
`src/kiro_crew/apps/builtins/*/tests/` get the rootdir `conftest.py` plus that app's
own `tests/conftest.py` where one exists.

The rootdir `conftest.py` is the **host-mutation floor** — the guards that protect the
developer's machine, so they hold everywhere. Everything else is in `test/conftest.py`.

Getting this wrong is the most expensive mistake in this suite, because it fails
**silently and asymmetrically**: an in-package test that assumes `test/conftest.py`'s
fixtures passes on CI (where the operator's home is empty) and damages a real install
locally. When you add isolation, ask: *could a test in ANY testpath damage the host
without this?* If yes it belongs at the rootdir; if no, put it in `test/conftest.py`,
where the in-package suites pay nothing for it.

## Rule 1 — The side-effect floor: what actually leaks

Work down this list. Each item is a real leak that has happened here.

### 1a. Temp directories — register the destruction in the SAME scope

Prefer `tmp_path`. If you need `mkdtemp()`, register cleanup with `addCleanup` on the
**next line** — never an `rmtree` in `tearDown`, because `unittest` skips `tearDown`
entirely when `setUp` raises, so that shape leaks on exactly the failing runs nobody
watches. The before/after example is in testing-conventions.md § Rules.

`ignore_errors=True` also **hides** a cleanup that could not finish, so it is not proof
of anything. The floor helps, but it is not your discipline: the rootdir conftest redirects
`tempfile`'s base per run, removes it, and **reports** residue — as a warning today, fatal
under `KIROCREW_TMP_RESIDUE_STRICT=1`. So a leak of yours will not necessarily turn CI red;
clean up anyway. (Staged rollout and its owner: testing-conventions.md § Rules.)

Why it earns a guard: `/tmp` is often a tmpfs with a fixed **inode** budget
(1,048,576 on the hosts this was measured on) and it returns `ENOSPC` to every other
process on the machine while **90% of the bytes are free**. It is not a tidiness issue,
it is a "your shell stops working" issue.

### 1b. The operator's data home, and `~/.kiro`, which is a DIFFERENT axis

`KIROCREW_HOME` is pinned per test at the rootdir, which is what makes `config_dir()`
safe — and it needs to be, because resolving it is **not a read**: it creates the home
and its marker, and can run the `~/.kirocrew` → `~/.kiro/crew` migration.

Two shapes escape the env var:

- **Import-time from `config_dir()`** (`subagent_persistence._SUBAGENTS_DIR`): the var
  is read after the module captured the path. Each has its own autouse pin.
- **Import-time from `Path.home()`**: `~/.kiro` is *kiro-cli's* home, machine-wide and
  shared with the real installed agent — `~/.kiro/settings/mcp.json` is the live
  agent's MCP server list. `_isolate_shared_kiro_paths` redirects these from a table,
  and `test/test_host_isolation_floor.py` **fails when `src/` grows a new one**. That
  ratchet covers IMPORT-TIME bindings only: a lazy resolver such as
  `config.paths.kiro_home()` still names the real `~/.kiro`, so isolate that yourself.

  Two entries are excluded because they must *never* be redirected —
  `security._EXTRACT_INTO_TRUST_ROOT_RE` and `kiro_usage_api._CLI_SQLITE_DBS` are
  security anchors whose whole point is naming the real home. **Stub the reader, never
  move the anchor.** Redirecting a matcher so a test can pass makes it assert against a
  pattern that no longer matches the thing it protects.

### 1c. A child process inherits pytest's CWD, which is the repo root

This is the leak a reviewer cannot see: no line says `open(..., "w")`, the write
happens in a grandchild, and the test can assert against `tmp_path` and pass while the
artifact sits in the checkout. An empty file produced this way has been committed and
shipped from this repo.

- Pass `cwd=<a directory under tmp_path>` to any child that MAY create a file, and to
  every helper that spawns one.
- Scope the assertion to where that child's CWD actually **is**. An assertion over
  `tmp_path` proves nothing about a child that ran somewhere else — and a security test
  whose payload escapes its own assertion is worse than no test, because it reports a
  guarantee it never checked.
- A read-only query is exempt, and sometimes must be: `git check-ignore` against the
  checkout is asking a question *about the checkout*.

The rootdir conftest fails the run on new non-ignored entries at the repository root,
which is how this announces itself.

### 1d. Background lifecycle — the one that beats every filesystem cleanup

**A singleton with a daemon thread cannot be cleaned up by tidying files.** The worked
example is `sel.py`: `SecurityEventLog` is a process singleton, its writer is a daemon
thread, and `_init_locked` binds the directory **once** from whatever `_default_dir()`
resolved then. So the first test to call `sel()` fixes it for the whole worker, the
thread keeps writing after that test ends, and `_flush_batch` opens with
`mkdir(parents=True, exist_ok=True)` — **re-creating the directory after tearDown
deleted it**. MEASURED: exactly one stray directory per run of the ops-mission-control
suite. Full telling, with the stack it came from:
[testing-conventions.md](../../../../../docs/system-specs/common/testing-conventions.md)
§ Rules.

The fix was not better cleanup. It was giving the singleton a **session-scoped**
directory belonging to no individual test. When you touch a subsystem with a background
worker, ask: *which directory did its thread capture, and does anything delete that
directory underneath it?*

Related traps in the same family:

- **A `MagicMock` config reads TRUTHY.** Patching `KiroCrewConfig.load` with a bare
  `MagicMock` makes `cfg.telemetry.enabled` truthy, which starts a real recorder and a
  reader thread, and resolves `Path(cfg.local_dir)` to a *relative* path that writes
  into the repo. That is why `KIROCREW_TELEMETRY=0` is forced for the whole suite. Any
  subsystem gated on a truthy attribute read off a config object has this shape.
- **A sleeper child that outlives the test.** The suite spawns
  `python -c "import time; time.sleep(30)"` to simulate a hung process. Put the
  kill/wait in a `finally` or an `addCleanup`, never only on the happy path.
- **A fixed port.** Bind port `0`. A fixed number collides across xdist workers *and*
  with the operator's running gateway.

### 1e. Never leave the process working directory somewhere else

The CWD is per-PROCESS, so under xdist one test's `os.chdir` becomes every later test's
starting directory on that worker. Use `monkeypatch.chdir`, which reverts itself.

This is the leak with the widest blast radius measured here. Because a passing test's
`tmp_path` is removed at its own teardown, a test that chdirs into `tmp_path` and does
not come back leaves the worker in a **deleted** directory, and `Path.cwd()` then raises
`FileNotFoundError` for every later test that reaches it — including from inside
production code. The measured instance is in
[testing-conventions.md](../../../../../docs/system-specs/common/testing-conventions.md)
§ Rules; the shape to recognise is that it reads as "the suite is flaky" — many files,
each passing in isolation. The rootdir conftest restores the CWD before any fixture
teardown, but write the test so it would not need to.

### 1f. Never register a real cron job, and never touch a real service

The rootdir conftest traps the stdlib spawn funnels and refuses a
`systemctl`/`launchctl` invocation carrying a **mutating verb**. Read-only queries
(`show`, `cat`, `is-active`) are allowed and need no stub. A test reaching the make-live
cutover path must stub **both** `_run_cmd` and `_dropin_path`.

## Rule 2 — Determinism: five classes, one correct fix each

Never "fix" a flake with a rerun, a longer `sleep`, a weakened assertion, or a skip.
Full detail and examples: testing-conventions § Determinism.

Each class has ONE correct fix, and they are written out with examples in
[testing-conventions.md](../../../../../docs/system-specs/common/testing-conventions.md)
§ Determinism. What this skill adds is the routing — the symptom you actually have in front
of you, and which class it belongs to:

| The tell you are looking at | The class it is |
|---|---|
| `os.urandom` / `random` / `uuid4` / `now()` feeding an assertion the RNG or clock does not guarantee | nondeterministic input |
| an assertion on a rate, a sample count, or an elapsed duration the host controls | wall-clock race |
| `RuntimeWarning: coroutine ... never awaited`, blamed on an innocent later test | leaked async object |
| passes alone, fails in the suite | order dependence / shared state |
| a timing test that splits by **Python version** rather than by machine load | absolute time budget on an instrumented run |

Read the matching section before you change anything: four of the five have a fix that looks
like the obvious one and is not.

A sixth, adjacent trap: **a patch target that misses.** Patch the namespace whose
globals the code under test actually reads. It fails in both directions — patching a
package re-export when the caller reads its own defining module, or patching
`pkg.mod.fn` when the caller did `from pkg.mod import fn` and holds its own binding.
Either way the real function runs, the assertion passes for the wrong reason, and the
test pays real time. **Treat an unexpectedly slow "mocked" test as evidence the mock
missed.**

## Rule 3 — Cross-platform: macOS, Linux (x86_64 + arm64), Windows

- **Route POSIX calls through `platform_compat`.** See AGENTS.md's shim table. Most
  important: `os.kill(pid, 0)` **TERMINATES** the target on Windows — it is not a
  liveness probe. Use `platform_compat.pid_exists`.
- **Path length is a real constraint.** Windows caps a path at 260 characters unless
  long paths are enabled, and a macOS `AF_UNIX` `sun_path` is capped at ~104 bytes —
  which a macOS `basetemp` alone already exceeds. That is why
  `test/tmpdir_helpers.short_tmp_base()` exists, and why anything that prefixes every
  temp path in the suite has to be measured, not assumed.
- **Case-insensitive filesystems.** macOS and Windows are case-insensitive by default,
  so a test asserting that two paths differing only in case are distinct is broken
  there.
- **Windows timer granularity** rounds `sleep`/`Event.wait` up to ~15.6ms and has
  coarser file mtime resolution — so "two writes have different timestamps" is a flake
  there.
- **Probe, do not guess the platform.** `test/conftest.py::_can_create_symlink` is the
  model: creating a symlink needs `SeCreateSymbolicLinkPrivilege`, which CI runners
  hold and an ordinary shell does not, so a blanket `skipif(IS_WINDOWS)` would drop the
  assertion exactly where it needs to run. Use the `make_escaping_link` /
  `make_dir_link` helpers, which fall back to a junction.
- **arm64 vs x86_64** rarely matters for test logic, but check it when you touch memory
  or page arithmetic (`SC_PAGE_SIZE` is 16K on some arm64 configurations) or a
  dependency with per-architecture wheels.
- Windows gaps are tracked as burn-down lists, not scattered skips:
  `test/windows-collect-ignore.txt` and `test/windows-expected-failures.txt`. Anything
  NOT listed still fails the job. Delete a line when you fix its test.

## Rule 4 — Diagnosing a residue failure

The residue report runs in a session-fixture teardown, so it is attributed to the **last
test the worker ran**, which is almost never the culprit. The guard carries its own
bisector — use it instead of guessing:

```bash
KIROCREW_TMP_PER_TEST=1 pytest src/kiro_crew/apps/builtins/<app>/tests -n0 -q
# AssertionError: 1 temporary entry outlived this run under /tmp/kc-pytest-you-951504:
#     test_provider_listing_never_contains_a_token/tmpw2kvty2z
```

Each residue name becomes `<test id>/<leaked name>`. If the leak survives a `tearDown`
that visibly removes it, suspect Rule 1d: something **re-created** the path after
cleanup. Confirm by wrapping `os.mkdir` in a throwaway `-p` plugin and printing a stack
for paths under the run's temp root — that is how the `sel-writer` thread was found.

For a repository-root residue failure, the cause is almost always Rule 1c: a subprocess
spawned without `cwd=`.

## Rule 5 — Keep the parallel suite fast

At ~26.5k tests, **per-test setup cost dominates any single slow test** — an autouse
fixture is paid ~26,500 times. Profile, never guess; compare candidates back to back on
the same host (`git stash`, run, pop, run), because a loaded host makes an absolute
number meaningless.

The recurring wins, in order of leverage:

1. **An autouse fixture that costs more than it protects** — one requesting a
   `tmp_path` it never uses; a repeated `tmp_path_factory.mktemp` (it scans the whole
   basetemp to pick its suffix, so it slows as siblings accumulate); an unconditional
   `mkdir` where the consumer only needs a *path*. Measure the whole chain against a
   file of trivial `assert True` tests.
2. **A production timeout or poll the test never asserts on** — `monkeypatch` the
   interval to `0`; the branch still executes, only the waiting goes.
3. **An expensive immutable thing built per test** — a real `git` repo costs ~1–1.6s in
   subprocesses. Build it once `scope="session"` and `copytree` it per test; copy *from*
   the template rather than yielding it, so nothing one test does can reach another's.

**After any speedup, mutate the production code the test covers and confirm the test
still FAILS.** A test made faster by checking less is a regression. Restore from a copy
of the file you mutated, not from git — `git checkout --` discards unrelated uncommitted
work — and sequence with `;`, not `&&`, or the restore only runs when the mutation
did *not* work.

## Checklist before you push a test

- [ ] Nothing outlives the run: no temp residue, no write to `~/.kiro` or the real data
      home, no cron job, no service change, no file in the checkout
- [ ] Every `mkdtemp` has `addCleanup` on the next line (or uses `tmp_path`)
- [ ] Every child that may create a file gets `cwd=` under `tmp_path`, and every
      assertion is scoped to where that child actually ran
- [ ] Every thread, task, child process, socket and connection it starts is stopped in a
      `finally` or an `addCleanup`
- [ ] Globals mutated through `monkeypatch`, never raw assignment
- [ ] No `AsyncMock` standing in for a synchronous method; every `cancel()` awaited
- [ ] No assertion on a rate, a sample count, or an absolute duration
- [ ] Source files read via `_REPO_ROOT = Path(__file__).resolve().parents[N]`, never a
      relative `Path("src/...")` — xdist workers may change CWD
- [ ] Passes at `-n0` **and** under `-n auto`, and passes when run alone
- [ ] Cross-platform: `platform_compat` for process/signal/lock calls, no assumption
      about path separators, case sensitivity, `/tmp`, or timer granularity
