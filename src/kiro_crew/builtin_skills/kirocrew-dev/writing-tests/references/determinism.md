# Rule 2 — Determinism: seven classes, one correct fix each

Moved verbatim from the writing-tests `SKILL.md` core.

Never "fix" a flake with a rerun, a longer `sleep`, a weakened assertion, or a skip.
Full detail and examples: testing-conventions § Determinism. Its short form, thirteen
MUST rules, is the
[Determinism contract](../../../../../../docs/system-specs/common/testing-conventions.md#determinism-contract-read-this-first)
at the top of that document, and the checklist below opens with the same thirteen.

The seven classes, the tell that identifies each, and the ONE correct fix for each are in
[testing-conventions.md](../../../../../../docs/system-specs/common/testing-conventions.md)
§ Determinism. Read the section matching your symptom before changing anything: most of them
have a fix that looks like the obvious one and is not.

What this skill adds is when to go looking — a test that passes alone and fails in the suite, or
one that splits by Python version rather than by machine load, is one of those seven and not a
mystery. Do not reach for a rerun, a longer `sleep`, a weakened assertion, or a skip.

The seventh class is the one a sleep hides best: **two stamps written back to back can be
EQUAL.** On Windows through Python 3.12 `time.time()` steps ~15.6 ms, like
`time.monotonic()`, and some filesystems store mtimes in whole seconds, so a `sleep`
between two writes makes a tie less likely, never impossible. Set the stamps (`os.utime(ns=...)`, a stepped
clock, an explicit `created_at`), assert strictly, and pin a time-sorted output's
tie-break with an equal-stamp pair.

The fifth class looks fixable by tuning and is not: **a performance assert in seconds
reads the runner.** `thread_time` hides scheduling but not coverage's tracer, which
multiplies the CPU of every traced line about fivefold on the instrumented Linux
shards, so a 0.5 s budget the code meets bare in 0.07 s fails there on every rerun.
Taking the minimum of N readings or pausing the tracer only moves the line. Count the
work instead and bound its GROWTH between equally spaced input sizes:
`_interpreter_work` and `_linear_work` in `test/denied_commands_security_helpers.py`
are the model (testing-conventions § Determinism 5).

Prove a test rather than trusting a green run: 20 repeats at `-n0`, 10 at `-n 4` beside
its neighbours, and a shuffled order. A FIX to a flaky test also shows the forced
condition (a coarse clock, a late executor, a delay at the named seam) red on the parent
commit and green on the fix (testing-conventions § Proving a determinism fix).

Run that proof on a real machine of the OS the test fails on, the parent first, and put
it in the PR's `## Real-machine verification` (testing-conventions § Real-machine
verification).

The sixth class has a tell of its own: **the run ends early, not red.** On Windows a test that
blocks past `--timeout` is not failed, its xdist worker is killed, and with
`--max-worker-restart=0` the run aborts with every uncollected result missing. If a full run
reports a few thousand tests instead of 100k+, look for `worker ... crashed while running` in the
log: the named test is one that can wait forever. Wait on the observable state, not a guessed
`sleep`, and bound the await whose refusal is under test with `asyncio.wait_for` so a missed
refusal fails by name at that line.

On Linux the same class shows up as **a whole shard stalling at one moment**: four tests that
each start a real `node` land on four workers and cold-start four ~100 MB binaries in the same
second, on a runner already near its memory ceiling, and every test in flight times out. Start
one module-scoped child for all the cases, put the module in its own `xdist_group`, and give
the child a ceiling of half the test's own timeout, never a literal equal to `--timeout`
(testing-conventions § Determinism 6, "A real interpreter child is started once per module").

An adjacent trap: **a patch target that misses.** Patch the namespace whose
globals the code under test actually reads. It fails in both directions — patching a
package re-export when the caller reads its own defining module, or patching
`pkg.mod.fn` when the caller did `from pkg.mod import fn` and holds its own binding.
Either way the real function runs, the assertion passes for the wrong reason, and the
test pays real time. **Treat an unexpectedly slow "mocked" test as evidence the mock
missed.** The opposite trap is a patch that is too WIDE: a side effect on a module global
also fires for every background worker that calls it (the `eventlog-io` legacy fold calls
`members.read_dm_binding`). Patch the function awaited in the window you mean, and pin
the order of the calls the test relies on.

Another: **the host is an input, and a "surely-unused" number is not a constant.**
`999999` reads as an impossible PID and is not — `pid_max` is 4194304, so on a
long-running host it names a live process. That broke two tests in opposite ways: one
stopped pruning an entry whose owner "must be dead", and one accused a planted `ps` shim
of executing when it had not. If the code *probes* the PID, pin the probe; if the number
must never appear in real output, pick one no OS can allocate (`99999999999`).

Host files and installs are inputs the same way. A whole-report doctor test pins the
`tz data` probe (`cli_helpers._doctor_reads_current_tz_data`); a test that reaches the
spec-permissions gate pins `installed_kiro_cli_version`, because `pin_kiro_cli` finds a
real kiro-cli in `~/.local/bin` without `PATH` (the floor hides the operator's own copy
there, `_hide_the_operators_kiro_cli`; plant one through `KIROCREW_KIRO_BIN`); a test
that asserts on the stores under `HOME` gives itself a fresh `HOME`, because a real store can be a symlink out of it; and a
tool a child will run is probed on the child's `PATH`, not pytest's.

The host's free MEMORY is an input too, and the most-used probe of it is
`SubagentManager.spawn`, which queues — registering nothing in `_tasks` — on a
pressured machine. A queued spawn is still a `SubagentInfo`, so the test dies a line
later on a bare `KeyError`, not on the assert that would have named the cause. Any
file driving `spawn` takes `pytestmark = pytest.mark.usefixtures("healthy_host_memory")`,
and `test_subagent_spawn_host_pin.py` fails when a new one does not. The guard it
pins, and why it stays transparent for the parser's own tests, are in
testing-conventions § Determinism 1. Both the fixture and its ratchet are
`test/`-only — `healthy_host_memory` lives in `test/conftest_opt_in.py`, bound by
`test/conftest.py`, and `test_subagent_spawn_host_pin.py` scans `test/*.py`
non-recursively. An in-package app suite cannot request the fixture and is not
swept, so a test there that drives `SubagentManager.spawn` must pin the floor reading itself.

One more, for tests of a **single-flight or coalescing** path ("N concurrent readers
share ONE scan"): the property only holds for readers that arrive WHILE the shared
operation is in flight, so the test has to keep it in flight until they have. An
instant stub does not: `asyncio.gather` starts the leader first, its executor job
finishes on the pool thread before the loop reaches the `await`, and on Python 3.13
the wrapped future is already done — awaiting a done future does not yield. The
leader then completes with a waiter count of one and the next reader assembles again,
once in five loaded runs. Gate the stub on a `threading.Event`, poll until every
reader is registered, then release it. Whatever the count of assemblies asserts, the
test must first establish the concurrency it is asserting about.

The last one is for the author of a **product** change, not a test: **a new
process-wide cache makes OTHER people's tests order-dependent.** When your change adds a
module-level cache, memo or singleton on a path existing tests drive, any of them that
counts a side effect on that path (config loads, spawns, writes) now passes warm and
fails cold. #37659's `secrets.read_audit._loaded` did exactly that to
`test_status_loads_config_off_the_event_loop`, which then failed whenever it ran first on
a worker. Ship its floor entry in `test/conftest_resets.py` with the change, not only a reset in
your own test file, and find the new state with the `git diff ... | grep` line in
[testing-conventions.md](../../../../../../docs/system-specs/common/testing-conventions.md)
§ 4 Order dependence and shared state ("That rule did not stop #37659").
A switch or allowlist snapshot (`= True`, `= frozenset()`) that a handler test publishes
through the real write path is the same leak and needs the same floor entry; that section
has the second grep line for those spellings (`_cold_source_link_settings`).
