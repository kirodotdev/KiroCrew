---
name: writing-tests
description: "Kiro Crew repo only: how to write a backend pytest test with NO side effects that does not flake. Use when adding, editing, reviewing or debugging a test there: which conftest applies, what leaks (temp dirs, data home, ~/.kiro, cron, threads), the seven flake classes, cross-platform traps."
triggers: write a test, add a test, fix a flaky test, test is flaky, test side effect, temp dir residue, tmp residue, kirocrew test, pytest kirocrew, test isolation, conftest, xdist, test leaked
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

## Where this skill stops, and which sibling takes over

This skill owns **authoring** a test: isolation, determinism, cross-platform
behaviour, diagnosing a residue failure, and suite speed. Three siblings own the
neighbouring steps, and none of them restates what is here:

| You are doing | Skill |
|---|---|
| Writing, fixing, or speeding up a test | **this skill** |
| Running the build gate in a worktree, and deciding whether a red is yours | **kirocrew-worktree-dev** |
| Driving a branch to a review-ready PR and through CI | **kirocrew-prepare-pr** |
| Polling that PR until it is green | **babysit** |

The line that matters most in practice: **kirocrew-worktree-dev** tells you whether a
failure is yours (re-run it on `origin/main`, mine CI for what is genuinely flaky);
once it is yours, the fix is here. Do not fix a flake from a summary of the
determinism classes — pick the class from the symptom, in Rule 2.

## The two properties, and why they are one problem

A test must be **hermetic** (no effect that outlives it, anywhere but its own tmp dir)
and **deterministic** (same verdict every run, on every platform, in any order). They
are the same problem because the suite runs `-n auto --dist loadgroup`: a side effect
is not just untidy, it is *the input to another test on the same worker*, and it
surfaces as a flake in a file you never touched.

## Rule 0 — Know which conftest is under your file BEFORE you isolate anything

`setup.cfg` declares two testpaths and they do **not** get the same fixtures. The
table is in [testing-conventions.md](../../../../../docs/system-specs/common/testing-conventions.md)
§ Which conftest you are standing on — read it rather than a second copy here, which
would drift. The short version: only `test/` gets `test/conftest.py`;
`src/kiro_crew/apps/builtins/**/tests/` (and the one `**/*_tests/` suite) gets the
rootdir `conftest.py` plus that suite's own `conftest.py` where one exists.

The rootdir `conftest.py` is the **host floor** — the guards that protect the
developer's machine, so they hold everywhere. That covers damage (temp dirs, the data
home, services) and *resource exhaustion*: the xdist worker budget is registered there,
from the repo-root `xdist_budget.py`, because a run that spawns one worker per core on a
small laptop takes the machine down and does not care which testpath asked. Everything
else is in `test/conftest.py`.

Getting this wrong is the most expensive mistake in this suite, because it fails
**silently and asymmetrically**: an in-package test that assumes `test/conftest.py`'s
fixtures passes on CI (where the operator's home is empty) and damages a real install
locally. When you add isolation, ask: *could a test in ANY testpath damage the host —
or consume enough memory, cores or disk to take it down — without this?* If yes it
belongs at the rootdir; if no, put it in `test/conftest.py`, where the in-package suites
pay nothing for it.

## Rule 1 — The side-effect floor: what actually leaks

Work down this list. Each item is a real leak that has happened here.

### 1a. Temp directories — register the destruction in the SAME scope

Prefer `tmp_path`. If you need `mkdtemp()`, register cleanup with `addCleanup` on the
**next line** — never an `rmtree` in `tearDown`, because `unittest` skips `tearDown`
entirely when `setUp` raises, so that shape leaks on exactly the failing runs nobody
watches. The before/after example is in testing-conventions.md § Rules.

`ignore_errors=True` also **hides** a cleanup that could not finish, so it is not proof
of anything. The floor helps, but it is not your discipline: the rootdir conftest redirects
`tempfile`'s base per run, removes it, and **reports** residue. CI's `Backend Tests*` jobs
set `KIROCREW_TMP_RESIDUE_STRICT=1`, so a leak of yours turns that shard red; a local run
only warns unless you set it. Need a data home? Use the conftest's per-test `KIROCREW_HOME`
pin, not a private temp home: a background worker can re-create a home you removed.
The same goes for a store root that its own reads `mkdir` (the Issue Radar store does):
in a `unittest` class, set it from `tmp_path` in an autouse fixture method.
(Details: testing-conventions.md § Rules.)

Running the suite from a Kiro Crew agent needs no setup. The agent's shell inherits the
live `KIROCREW_HOME`, and the rootdir conftest sees `KIROCREW_SCRATCH` beside it and gives
the run its own home under `$TMPDIR` (the agent's scratch, reclaimed by the gateway)
instead of refusing. Do not make a home under `/tmp/<name>`, and do not fake `HOME`: a
fake `HOME` makes uv, npm and pip rebuild their caches under it, 10k+ inodes per run, and
nothing an agent may run can delete it afterwards. If a harness must fake `HOME`, keep
the caches shared: `XDG_CACHE_HOME=<real home>/.cache UV_CACHE_DIR=<real home>/.cache/uv
npm_config_cache=<real home>/.npm` on the commands the harness itself runs. The conftest
keeps only its own bytecode and hypothesis caches on the real home.

Why it earns a guard: `/tmp` is often a tmpfs with a fixed **inode** budget
(1,048,576 on the hosts this was measured on) and it returns `ENOSPC` to every other
process on the machine while **90% of the bytes are free**. It is not a tidiness issue,
it is a "your shell stops working" issue.

### 1b. The operator's data home, and `~/.kiro`, which is a DIFFERENT axis

`KIROCREW_HOME` is pinned per test at the rootdir; `~/.kiro` is a DIFFERENT axis
that pin does not cover. Read `references/data-home.md` before a test reads or
writes either, or spawns a process that might.

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

Two more things a spawning test owes, both learned from processes that outlived the run
by **six days**, spinning at 1464% CPU between them:

- **`HOME` and `PATH` are a PAIR.** If you hand a child a fabricated environment,
  substituting one while inheriting the other is the defect. An inherited `PATH` on a
  developer host routinely leads with a version-manager shim directory (mise, asdf,
  pyenv, volta, nodenv), and a shim resolves its tool set from `HOME` — so with a
  substituted `HOME` the bare name `python3` or `node` reaches the MANAGER, which finds
  no tool state and never execs anything. It spins, forever. Pin the real interpreter's
  own directory first on `PATH`, or resolve the tool to its real executable before
  building the env. Do not drop the `HOME` substitution instead: it is usually a
  blast-radius bound somebody chose on purpose. The shim also reads
  `$XDG_CONFIG_HOME` and the version files above the CWD, and the floor redirects
  `$XDG_CONFIG_HOME` for EVERY test, so a `tmp_path` CWD alone breaks it. `node` is
  handled once for the whole run (`_put_the_real_node_first_on_path` puts the directory
  `process.execPath` names first on `PATH`); another shimmed tool needs the same
  treatment: ask the tool where it lives, under the operator's environment, first.
- **Reap the process GROUP, on every exit path.** A child is routinely a wrapper that
  forks, so `Popen.kill()` reaps the wrapper and leaves the real work running — and a
  bounded `wait()` ending in a bare `pass` then reports success. Use
  `start_new_session=True` + `os.killpg` on POSIX and `CREATE_NEW_PROCESS_GROUP` +
  `taskkill /T /F` on Windows; `test/installer_test_helpers.run_bounded` is the
  reference. A `try/finally` around the whole post-spawn body, not just the timeout
  branch, is what makes "every exit path" true.

Two host traits a spawning test must not inherit. A suite started under `nohup` has
SIGHUP ignored, and an ignore survives `exec`: spawn a child you will interrupt inside
`spawn_test_helpers.unblocked_signals()`. And `sleep` can be a coreutils shebang script
whose kernel-visible argv is the interpreter's: a test that reads a child's argv or name
back spawns `plain_sleeper_argv()` instead.

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

**The same shape, one level up: a stub that replaces a `shutdown`/`close`/`stop` is
not a stop.** Three tests needed to observe *that* the metrics provider's `shutdown`
was called, so they replaced it with a recorder — and the real `shutdown` is what stops
OpenTelemetry's exporter thread. That thread then survived for the life of the worker,
and because the OTel SDK reinstalls it in every fork child via `os.register_at_fork`,
the sandbox's userns probe forked a MULTITHREADED child. `unshare(CLONE_NEWUSER)`
implies `CLONE_THREAD`, which the kernel refuses with EINVAL unless the caller is
single-threaded, and EINVAL is indistinguishable from "no `CONFIG_USER_NS`" — so the
worker cached "this host has no sandbox backend" and every later sandboxed spawn failed
closed. 19 red tests in two app suites, each passing alone, none of them a metrics test.
**Spy and delegate; never replace a lifecycle method.** The rootdir conftest now fails
the test that leaves an exporter thread running.

Two general lessons worth carrying out of that one: a thread whose target is a bound
method keeps its own object alive, so dropping references never collects it; and
anything registered with `os.register_at_fork` runs inside `os.fork()`, before it
returns, so a fork child is not reliably single-threaded.

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

## Rule 2 — Determinism: seven classes, one correct fix each

Never "fix" a flake with a rerun, a longer `sleep`, a weakened assertion or a
skip. Name its class first: read `references/determinism.md` for the seven
classes and the one correct fix each, before you write or fix a test that
waits, races, orders, times or shares state. *Determinism top 13* below is
the checklist.

## Rule 3 — Cross-platform: macOS, Linux (x86_64 + arm64), Windows

Every test runs on macOS, Linux (x86_64 + arm64) and Windows. Read
`references/cross-platform.md` before a test touches paths, processes, signals,
file modes, line endings or POSIX-only calls.

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

At 100k+ tests, per-test setup cost dominates. Read
`references/suite-speed.md` before you add a fixture, a conftest hook or a
slow setup step.

## Rule 6 — MEMORY is the other budget, and collection is most of it

A worker costs about 2 GiB, and collection is most of it. Read
`references/memory.md` before you add a module-level import, a large fixture
or anything collection loads.

## Checklist before you push a test

### Determinism top 13

One box per rule of the [Determinism contract](../../../../../docs/system-specs/common/testing-conventions.md#determinism-contract-read-this-first),
naming the shape; the contract line holds the detail and its numbers. The helpers are in
`kiro_crew.testing` (`clock`, `wait`, `ids`). Every other item below is a specialised trap.

- [ ] D1: wait on the asserted state with a raising, bounded poll (`wait_until`,
      `async_wait_until`, `until_parked`), never a sleep
      ([class 2](../../../../../docs/system-specs/common/testing-conventions.md#2-wall-clock-races))
- [ ] D2: one clock, installed on the module-under-test's own binding
      (`ManualClock.install`, the `manual_clock` fixture)
      ([class 2](../../../../../docs/system-specs/common/testing-conventions.md#2-wall-clock-races))
- [ ] D3: set the timestamps (`ManualClock(tick=...)`, `os.utime(ns=...)`, `seq_ids`),
      assert order strictly, pin the tie-break
      ([class 7](../../../../../docs/system-specs/common/testing-conventions.md#7-data-order-and-timestamp-ties))
- [ ] D4: no asserted order the code does not define
      ([class 7](../../../../../docs/system-specs/common/testing-conventions.md#7-data-order-and-timestamp-ties))
- [ ] D5: the product's zone and a frozen instant, never the host's (`local_tz`)
      ([class 1](../../../../../docs/system-specs/common/testing-conventions.md#1-nondeterministic-input))
- [ ] D6: seeded RNGs and unallocatable fake PIDs (`seeded_rng`, `unallocatable_pids`)
      ([class 1](../../../../../docs/system-specs/common/testing-conventions.md#1-nondeterministic-input))
- [ ] D7: no upper bound on a measured duration beyond what the contract allows
      ([class 5](../../../../../docs/system-specs/common/testing-conventions.md#5-absolute-time-budgets-on-instrumented-runs))
- [ ] D8: every self-unblocked await, join or communicate bounded, failing by name; a real
      `node`/interpreter child starts once per module, its deadline half the test's timeout
      ([class 6](../../../../../docs/system-specs/common/testing-conventions.md#6-a-hang-is-a-lost-run-not-a-failed-test))
- [ ] D9: listeners bind port 0 and report the port ([Rules](../../../../../docs/system-specs/common/testing-conventions.md#rules))
- [ ] D10: loopback only, the network stubbed at the product's seam
      ([side effects](../../../../../docs/system-specs/common/testing-conventions.md#side-effects-what-a-full-run-does-to-the-host-and-how-to-see-it))
- [ ] D11: globals through `monkeypatch`, no in-process reload, evictions restored
      ([class 4](../../../../../docs/system-specs/common/testing-conventions.md#4-order-dependence-and-shared-state))
- [ ] D12: proven by repeats, a shuffled order and, for a fix, the forced condition
      ([proving a fix](../../../../../docs/system-specs/common/testing-conventions.md#proving-a-determinism-fix))
- [ ] D13: no dependence on the host's `PATH`; pin the resolver or set `PATH` to `tmp_path` and plant the tool
      ([what the host lends](../../../../../docs/system-specs/common/testing-conventions.md#what-the-host-lends-the-suite-and-must-not))

### Specialised traps (search when your test touches X)

A checklist of 98 traps lives in `references/specialised-traps.md`. Search it
for what your test touches (a subprocess, a socket, the clock, `tmp_path`, a
mock, a config read, ...) and check every matching item before you push.
