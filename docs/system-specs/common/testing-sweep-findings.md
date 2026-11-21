# Testing sweep findings

The records of the repeated five-run measurements of the full test suite: one section
per pass, with the host, the worker count, the side effects and flakes the per-test
probe caught, and how each class was closed. They are evidence for the rules in
[testing-conventions.md](testing-conventions.md), which is the contract a test follows;
read that first. Names and counts here are as each pass recorded them, so a helper a
later change renamed or removed can appear under its old name.

The probe and the classes it measures are in
[Side effects](testing-conventions.md#side-effects-what-a-full-run-does-to-the-host-and-how-to-see-it).

## What a second five-run pass found (Windows host, ~88k tests per run)

The [measurement](testing-conventions.md#the-measurement) was repeated on `main` two days later, on a Windows developer
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
- **What a closed loop leaves behind runs after the pins are gone.** pytest-asyncio 0.21
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
  and run the loop until they finish, repeating while a cancelled task's `finally`
  starts a new one (a dashboard turn's queue cycle dispatches the next queued turn,
  which a single pass over a snapshot never sees, and asyncio then logs `Task was
  destroyed but it is pending!` into a later test's `caplog`), all passes under one
  deadline, then `shutdown_default_executor` — what
  `asyncio.run` does at shutdown — bounded, and before any fixture teardown so the pins
  still hold. A test that leaves an unstarted turn behind now shows up as a
  `coroutine ... was never awaited` warning at that point instead of as residue.
- **A default that bypasses the data-home pin BY DESIGN.** `PodConfig.load()` derives
  `pods_dir` from `_default_home()` — the operator's real `~/.kiro/crew/pods` — precisely so
  a pod running with its own isolated `KIROCREW_HOME` cannot redirect the host's pod
  registry, and `pod_root` from `Path.home()/.kirocrew-pods`. A fixture that was simply
  `PodConfig.load()` therefore recorded `viability-*.refused` notes into the real pod plane.
  The floor now pins `KIROCREW_POD_ROOT` and `KIROCREW_POD_ENV_DIR` per test
  (`TestThePodPlaneIsPinnedForEveryTestpath`); the modules that import `pod_helpers.py`'s
  autouse `_isolate_pod_host_state` (`test_pod.py` among them) clear them deliberately because
  the pod tests' subject includes the home-derived defaults, with `HOME` redirected first.
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
  workers, is more than ten seconds on Windows. Multiplying the ceiling (`WAIT_S * 3`)
  on the two tests that had failed did not fix the file: once the Windows backend shards
  ran six xdist workers, the one-pass export and head-branch tests failed the same way
  (`watcher never reached {'exhausted'}`, last status `nudging`, `pass 1/1`) on seven
  pull requests in a day. The file's poll helpers now call `wait_until` with no
  `timeout`, so every wait gets the lost-run bound (half the test's timeout). A
  file-wide short ceiling shared by poll helpers is D1's "bets on the runner's speed"
  for EVERY caller: raise it to the lost-run bound once, in the helper, rather than
  per test after each one fails.
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
  sides; the three ledger guards go through it. Ninety repeated runs pass. The same
  class came back as an EQUALITY guard: `test_dashboard_save_tool_older_rule`'s two
  concurrent first saves got a 503 because `atomic_write._refuse_linked_parent`
  compared a prefixed resolve of the parent with a plain one of the same directory
  (#37114, fixed in #37211). The rule is the platform-compat row "Compare paths for
  identity": every identity comparison of a resolved path folds both sides through
  `strip_extended_length_prefix`.

## What the fourth five-run pass found (Linux, 106k tests per run)

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
  (`test_security_conductor_scripts_verify_finding.runnable_python`). It cannot be fixed by
  spelling `sys.executable` in the proof
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
  PATH led with Node 18 — while the repo declares `.nvmrc` = 24 and
  `engines.node >= 22.12.0`. This is the rule already stated for config ("a `skipif` helper
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
  Eleven `test_slack_gateway_update_*.py::TestAutoApplyUpdate*` tests measured **30.02–30.16s
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
the retired browser-CLI installer's `test_an_interrupted_rebootstrap_restores_the_previous_node`
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

## What a fifth five-run pass found (macOS, uv venv, ~106k tests per run)

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
  (`test_apps_backend_coverage_dispatch._create_real_venv`).
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

## What a sixth five-run pass found (Windows host, ten workers, ~98k tests per run)

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
  only when the first overran (a GC pause cannot hit two in a row). Each long pump
  must also cost at most `REDOS_SCALING_RATIO` (25x) the one before it, above
  `REDOS_SCALING_FLOOR_SECONDS` (0.25 s): a quadratic rescan built from cheap C
  steps measured 1.8 s at 20 000, under the 2 s absolute budget, and only the ratio
  refuses it. The 200 and 2 000 pumps take three readings, since each one's
  cheapest is the next comparison's baseline; at any long pump, two readings over
  the line that pump is held to end it, so a regression is paid at most twice per
  size. The property
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

## What a seventh five-run pass found (Linux, 16 workers, 122,426 tests per run)

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
- **Stubbing the spawn is not enough when a PATH probe gates it.** A test that fakes
  `_run_gh` but not the adapter's `shutil.which("gh")` gate passes on a runner that ships
  `gh` and fails on one that does not: the gate answers "unconfigured" first, the fake
  never runs, and an `assertNotIsInstance` beside it passes for the trivial reason. Pin the
  probe too (`_gh_available` in the ops-mission-control `github_issues` adapter), and run a
  host-tool test once with the tool off `PATH` before calling it host-independent.
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
## What an eighth five-run pass found (macOS, eight workers, ~121k tests per run)

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

### The findings that were the instrument

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
  connection.** `src/kiro_crew/security/argv_floor_hosts.py`'s own-address enumeration connects a
  `SOCK_DGRAM` socket to `198.51.100.1:53` and `2001:db8::1:53` — TEST-NET-2 and
  documentation addresses, contacted by nobody — to learn the primary outbound address per
  family, and says so in a comment three lines above. The `socket.connect` audit hook never
  read the socket's `type`, so that recorded as an outbound connection to a non-loopback
  peer. One of the two `network` findings was this. The same pass also shows why the class
  needs thread attribution: one of those three events arrived off-thread, from the
  asynchronous name-resolution worker, and was charged to whichever test was running.

### The limit disclosed rather than closed

- **The two side-effect witnesses do not cover the same ground, and a reader who assumes
  they do reads one's silence as the other's corroboration.** The host snapshot walks the
  data home, `~/.kiro/agents` and the temp roots ONLY. A write to `~/.cache` is invisible to
  it, so the probe's filter is the single witness for that entire class, and `residue: 0`
  neither confirms nor denies it. The report now states that in its own header rather than
  leaving it to be inferred. Widening the snapshot is a separate change.

### The one defect that was the suite

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

### What the always-red set actually was

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

### The one real network finding

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

### What not to re-derive

Two classes looked large and were dispatched to nobody, because their thresholds and their
attribution have to be fixed before a member means anything. Of 204 tests in the
descriptor-leak class, 140 had a maximum delta of EXACTLY the threshold of 5 — a bounded
five-descriptor pool, not a leak — and the rest ran 6 to 21. In the thread-leak class 4 of
14 tests had a NEGATIVE delta in some round (−9, −10): a previous test's threads were reaped
during this one, so the counter crosses test boundaries and a delta is not that test's doing.
The `spawn_no_cwd` class (531 tests) is real and advisory only.

## What a ninth five-run pass found (Windows host, eight workers, 123,179 tests per run)

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
  the writer's `_run_job` returned the caught exception to `_write_batch`, which bound it to a
  local while deciding whether to retry; the traceback's `_run_job` frame reaches
  `_write_batch`'s frame through `f_back`, and that frame holds the exception -- a cycle
  through the handle, so the lease (a `weakref.finalize` on the handle) was released by the
  cyclic collector at some later pass instead of at the drop, in production as well as
  here. Reproduced deterministically at `-n0` by running the two files in order; traced with
  `gc.get_referrers` from the handle up to both roots. Fixes: the errno is parametrized and
  the exception built inside the test; `_run_job` returns the failure's TYPE (`_is_refusal`
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
  shard it read a lease from `test_crew_log_core_segments.py`'s chmod-refusal test, an earlier file
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
- **"Let it finish" as two 200 ms sleeps.** `test_overload_integration_glue_store_off_loop.py`'s
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
## What a tenth five-run pass found (Linux, 16 workers, 126,905 tests per run)

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

### The instrument

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

### The four reds

- **A test that plants a fixture under `tmp_path` and asserts a "not under `$HOME`"
  refusal does NOT fire is asserting where the host's `TMPDIR` lives.**
  `test_dev_fleet_app_trusted_helpers.py::test_trusted_bin_pins_the_resolved_target_not_the_symlink` built
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
  in the worker.** `test_taskq_runner_adapter_durability.py`'s `_one_warning` demanded exactly one
  record while the test had scoped `at_level` to `kiro_crew.taskq.adapters.runner`; the
  second record was `kiro_crew.sandbox`'s mount-source sweep, finishing on the shared
  `mc-maint` executor seconds after the `SessionManager` test that armed it had torn down
  (`close_all()` cancels the asyncio task but cannot stop an executor thread mid-`/proc`
  scan). Two fixes, in two places: the helper filters `rec.name == _RUNNER_LOGGER`, and
  `test/conftest.py`'s autouse `_no_boot_sandbox_sweep` pins
  `kiro_crew.session.cleanup_stale_sandbox_profiles` to a no-op for every test under
  `test/` -- the app test trees under `src/kiro_crew/apps/builtins` do not load
  `test/conftest.py` and so get no such pin (the table in § Which conftest you are
  standing on says which floors each testpath gets); a test of the sweep itself patches
  the same name inside its body and so still wins. Measured
  over `test_session.py` alone: 138 real sweeps on `mc-maint_*` threads without the pin, 0
  with it.

### The classes that were quiet but real

- **`spawn_no_cwd`, ranked by what was reached.** Real `gh` (a live token), `ssh` (the
  host's agent socket and config), `aws configure`, `systemctl is-active` and `mise` were
  reached from unit tests every round, and ~125 spawns per round ran with the CHECKOUT as
  process cwd. The causes were resolver seams the tests had not pinned, in four shapes:
  a class-local copy of the `_public_dns` fixture that re-implemented the DNS pin and
  DROPPED the sibling `_gh_prefers_ssh` pin (so `gh config get` ran for real); a
  `functools.lru_cache`d `ssh -V` probe behind `_build_launcher_script` that whichever
  launcher-building test ran first paid (six sandbox files, one autouse pin each, the
  house pattern from `sandbox_argv_helpers.py`); the doctor pinning only `shutil.which` while
  its sections resolved through `platform_compat.trusted_aws_bin` / `trusted_system_bin` /
  `trusted_git_bin`; and `code_fingerprint()`'s lru_cache deciding WHICH test pays a real
  `git` on the developer's checkout, so fixing the flagged tests only moved the spawn to
  the next reader -- the pin has to be module-scoped with an explicit opt-out fixture. For
  the `git -C <scratch>` production spawns a test cannot pass `cwd` to,
  `monkeypatch.chdir(tmp_path)` in the fixture closes the process-cwd hazard; adding
  `cwd=` to the product would turn git's rc=128 for a missing clone into a Python
  `FileNotFoundError` before git runs, which every caller reads off `returncode`, so that
  descriptor is deliberately left. (The one production change this pass made, to the
  since-deleted browser CLI installer's staged node launcher, went with that installer.)
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
  `test/conftest.py` flips once per session (so only the `test/` testpath has it: the app
  test trees under `src/kiro_crew/apps/builtins` do not load `test/conftest.py` and do not
  activate the flip themselves), under which a connection opens
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
  every inline construction through `test/conftest.py`'s `opened` register-and-close
  fixture (one definition, not one per module: a `KnowledgeStore` is closed through its
  test-only every-thread seam, everything else through `close()`), every `SkillsLoader` a
  test builds is closed by the rootdir conftest's autouse
  `_close_skills_loaders_the_test_opened` (§ Close the SQLite handles and skills loaders a
  test opens), and managers get `manager.close()` before `sessions.close_all()`.
  The one descriptor the probe still sees
  is pytest-asyncio 0.21.2's replacement loop: +3, one at a time, reclaimed at the next
  test's setup -- a float, not a leak, and never the thing to chase.
- **A cancelled inline reap is a backend nobody owns.** `mcp_gateway/daemon/connection.py`'s stub
  disconnect teardown awaited `orphan.shutdown()` inline; daemon teardown cancels those
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
  identity-pinned at the test's own children and left alone.
  `test_pid_lifecycle_provider_tree.py`'s `_reap` was the exception: a `finally` that
  SIGKILLed the RAW root and grandchild numbers
  after production had reaped the root and init had collected the grandchild, so on every
  passing run 19 of its 33 kills went to a number with no `/proc` entry -- whoever holds
  that number next. `_reap` now records `process_start_time` at spawn and signals only
  while it still matches, refusing an unpinned pid outright (0 freed-number kills after);
  and `test_kill_pid_nonexistent_is_safe` asserts its probe pid exceeds
  `/proc/sys/kernel/pid_max` so the kernel's range check, not convention, is what makes
  the number unreachable.
- **`network`: rank the address before you file the finding.** The UDP `connect` to
  `198.51.100.1:53` / `2001:db8::1:53` in `argv_floor_hosts._own_interface_addresses` is the
  packet-less local-IP trick over TEST-NET, a hardening item: `test_security_denied_commands.py` now routes
  it through an inert `_InertDatagramSocket` at the module's `socket` binding (datagram
  construction only; every other socket kind passes through) that records the peer and
  answers a documentation address from `getsockname`, and asserts the peers it WOULD have
  used -- rewriting the production peer to `8.8.8.8` fails it by name. The AF_UNIX connect
  to `./dashboard/app.sock` was the TEST: it dodged the `sun_path` cap with
  `monkeypatch.chdir` and a relative root of `.`, which no production caller ever spells;
  `test/conftest.py`'s `short_sock_dir` passed as an ABSOLUTE root is the fix.
- **`thread_leak` was named pools, again, and is left alone.** Every surviving thread in
  the class was `mc-embed_*`, `mc-recall_*`, `mc-pathres_*`, `mc-maint_*`,
  `mc-discovery_*`, `mc-subproc_*` or `skill-catalog-refresh` -- the last of which now
  exits when its `SkillsLoader` is closed. The `mc-maint_*` rows were the same boot
  sandbox sweep the caplog red exposed; the conftest pin removes the work, not the pool.

### What not to re-derive

`production git -C <path>` helpers (`auto_improvement`'s backend and spine,
`code_fingerprint`, `cli_doctor`) pass `cwd=None` on purpose (see `spawn_no_cwd` above),
and `green_age.run()` is a CLI whose contract is "git in the invoking cwd", so its ~150
descriptors per round will stay flagged until the probe records the PROCESS cwd alongside
the `cwd` kwarg -- with the fixture's `chdir` under `tmp_path`, none of them ran in the
checkout. `push_guard.py`'s two direct `git patch-id --stable` calls bypassed that module's
`_GIT_CMD` injection seam (its docstring's "every command goes through `run()`" was false
for them); the twelfth pass routed them through `run()`. `history_search`'s lazily opened `SessionSearchIndex`
has no close path, so `search_chat_history` leaks one handle per call until GC (+3, below
threshold). The dashboard files' 731 `aiohttp` warnings are class 15 (`app[...]` writes
after start, ~24 sites), not this pass.

## What an eleventh five-run pass found (macOS 26, eight workers, 138,593 tests per run)

Five rounds of backend, vitest and electron on a test-only branch off one commit, on an
18-core Mac with a LIVE gateway running out of the same data home: 138,593 / 39,693 /
2,199 tests per round, identical every round; vitest and electron green five times. The
backend was red in every round -- 12, 12, 11, 13, 13 -- and the red set was not stable:
one file (`test_trusted_apps_api.py`) held seven to nine reds per round with a different
subset each time, one test failed 5/5 and passed alone, and four others failed once or
twice each. Zero files added to the checkout by the suite in any round (the run root
itself was the only checkout residue, and it was ours).

The generalisable lesson: **the instrument fails in the direction of a finding, so a
class two orders of magnitude larger than anything else is the instrument.** Two probe
defects manufactured the two largest classes of the pass, and a third made one test's
verdict depend on whether the probe was loaded. Each was proved by the same move before
any fixer touched a test: run the file ALONE, and run it WITHOUT `-p probe_plugin`; a red
that survives neither is the probe's.

### The instrument

- **A relative operand is not relative to the cwd.** `shutil.rmtree` (3.12+) removes by
  descriptor -- `os.unlink(entry.name, dir_fd=fd)` -- so pytest's own `tmp_path` teardown
  under `tmp_path_retention_policy = failed` raised a bare `mspaint.exe` per file, and the
  probe realpath'd it against the worker cwd, the checkout: 49,408 tests / 1,911 files
  in `checkout_write` after round 1. Anchoring to the descriptor (`F_GETPATH`) removed
  those; the `os.open(name, flags, dir_fd=fd)` shape the nofollow writers (`pinned_fs`,
  the sage store, the packaging bundle) use then still left 1,970 tests / 9,529 events
  there -- and `os.open`'s audit event carries NO dir_fd, so those cannot be anchored at
  all. They are now recorded as `unanchored` and filed under `unattributable_write`,
  never under a location. The checkout diff (`git status` after each round) is the
  authority for whether the tree was written, and it said no, every round.
- **The probe was observed by the test it was observing.**
  `test_dashboard_files_onloop_fs.py::test_validation_runs_off_the_event_loop` wraps
  `os.path.realpath` to record which THREAD touches the request path; the probe's audit
  hook runs inside the test's own call, its classification called `realpath` on the
  rename operand from `asyncio_0`, and the spy counted it -- red 4/5, green alone, green
  without the probe. The probe now binds every `os.path` function it uses at import
  (which `-p` makes earlier than any conftest). A per-test observer must not be reachable
  through the module the suite patches.
- **The frontend logs read as INCOMPLETE while green.** vitest colours each summary
  column and prints `1 expected fail` as a two-word column; node's default reporter
  prints `ℹ pass N`, not `# pass N`. All eight frontend logs of a green sweep parsed as
  "no summary line". Strip colour before matching, take column labels verbatim, accept
  both node glyphs.
- **Live-gateway noise, again.** Every host-snapshot diff was DIRTY on `artifacts/`,
  `metrics/*.jsonl` and `pw/<8hex>/` (the running gateway's own writes, including the
  artifact this very report was rendered into); both snapshots hit the 20,000-entry cap.
  The per-test probe's attributed writes were the evidence for that class, as in the
  tenth pass, and they named ONE host path (below).

### The reds

- **An Apple platform binary hides its environment on macOS 26.**
  `test_darwin_spawn_marker.py::TestDarwinEnvironIsReadableSameUid` spawned `sleep 30`
  and asked `KERN_PROCARGS2` for its environment; the kernel answers an argv-only record
  for `/bin/sleep` even to a same-uid reader (`ps -E` shows nothing either), so the
  marker was never found (5/5, alone too) and the sibling "without the marker is refused"
  test was vacuous -- `None` collapses to the `False` it asserted. The child is now the
  test's own interpreter, a non-platform binary like every launcher the oracle exists
  for; `darwin_process_environ`'s docstring names the case. A host-dependent verdict is
  not made portable by a bigger buffer.
- **A kept emitter handle is a held lease, process-wide.**
  `test_eventlog_hooks.py::test_the_lease_module_can_report_a_holder_at_all` asserted
  `lease._held` empty after its own release and found leases from
  `test_crew_log_session_tree_projection.py`, hundreds of tests earlier on the same
  worker: that file writes units through `emit.on_session_opened`, and the emitter KEEPS
  the handle (correct for a live session), which holds the write lease until the handle
  is dropped. The fix is on the creators' side only, per the checklist item on
  process-global "nothing retained" assertions: the `_isolated_home` fixtures of both
  suites that write real units through the emitter -- `test_crew_log_session_tree_projection.py`
  and its sibling `test_crew_log_session_tree_adopt.py` -- call `emit.reset_caches()` in
  teardown, so the retention is reported where it was created. The eventlog
  test keeps its strict `assert not lease._held`: relaxing it to a per-test baseline
  would have accepted exactly the inherited leak it exists to catch, which is the
  `a-ratchet-may-only-tighten` rule in `AUTOSDE.yaml`. Reproduced red as a two-file run
  with the creator fix removed; 94 green with it.
- **Five nested pytests in series under a bound smaller than their sum.**
  `test_ci_ipv6_routing.py::test_real_collected_nodes_are_the_disjoint_fleet_and_hosted_union`
  spawned five `--collect-only` interpreters one after another, each capped at 60 s,
  inside the suite's 120 s per-test timeout. Idle, one collect is 1.8 s; under eight
  workers it read 8-13 s, and twice in five rounds the FIRST passed 60 s and failed the
  test ahead of pytest-timeout -- a cap that can only ever lose, never protect. The five
  are independent interpreters, so they now run concurrently (wall = the slowest, not the
  sum). Each keeps `_run`'s 60 s cap: that cap is the ratchet on how slow one collection
  may get, and `a-ratchet-may-only-tighten` forbids raising it to fit -- concurrency fixes
  the budget, a looser cap would only hide the stall. Every nested run gets `--basetemp`
  under `tmp_path` (the rule from the fifth pass). Why one collect passed 60 s on this host
  is NOT explained by this pass; the design defect was real regardless. A later pass found
  most of the child's work: given three file arguments, pytest still walks every sibling in
  `test/` (about 4,600 entries) through `pytest_ignore_collect` and `pytest_collect_file`.
  Each child now passes `--ignore-glob=*`, which pytest never applies to a named argument,
  so it reads only the three files and collects the same nodes in about 60% of the time
  (Linux, six children in series: 26.3 s -> 15.7 s). The 60 s cap stays.
  `test_a_nested_collection_reads_only_the_named_files` pins that only the named files
  reach `pytest_collect_file`. A Windows runner is slower at the per-entry `stat` and
  `relative_to` work this removes, so the saving is likely larger there; this pass did not
  measure it on Windows. What kept the Windows children at 44-64 s against about 4 s on
  Linux was `test/conftest.py`'s Windows `collect_ignore` list (91 names): pytest's own
  `pytest_ignore_collect` rebuilds a conftest ignore list into absolute paths for every
  entry it is offered, before it reads `--ignore-glob`, and it walks `test/` (~5,400
  entries) once per file argument, so each three-file child built ~1.47 million paths, one
  `GetFullPathNameW` call each. A Linux child given the same list goes from 4 s to 17 s.
  The list is now a frozenset behind the conftest's own `pytest_ignore_collect`
  (`platform_collect_ignore_helpers.py`), which builds none, and
  `test_platform_collect_ignore.py` pins that on every platform. The 60 s cap stays.
- **Reading a claim's task after the POST names whichever task won the race.**
  `test_cron_manual_run_claim_window.py::test_the_manual_wrappers_backstop_leaves_a_replacement_claim_alone`
  took `first = svc._claims[job.id].task` after the route answered; when the first run's
  refresh is not the parked call, `_run_claimed_manual` may already have swapped the
  claim's task for its inner `_run_job_isolated` task (result `None`), so
  `first.result() is True` held or failed by scheduling (2/5). The wrapper is now taken
  from the seam the route hands it through (`attach_run_task`), 15/15 alone.
- **`test_trusted_apps_api.py` -- 409 `teardown_incomplete` from a dispatcher another
  file left behind.** Seven to nine of its revoke tests answered 409 every round, a
  different subset each round, and every one passes alone. Replaying one worker's 1,874
  files in order at `-n0` with a debug hook on the handler named the cause:
  `hooks_integration._lifecycle_dispatcher` is a process-wide slot that
  `init_hooks_system` assigns and nothing in production ever clears (a gateway sets it
  once at boot), so it carried the LAST dashboard-app-building test's
  `LifecycleDispatcher` -- with a `MagicMock` as its `cron_service` -- into the revoke
  teardown, whose cron cleanup awaited the mock and reported `hooks disable failed:
  object MagicMock can't be used in 'await' expression`. Any of ~170 files that build
  the real app can be the one that lands before the victim (on the replayed worker the
  first was `test_dashboard_route_table.py::test_route_table_ordering_invariants`, a
  route-table test with no interest in crons at all), so the fix is a rootdir
  conftest floor that snapshots and restores both hooks-integration globals around every
  test, in the same shape as the log-record-factory and queue-listener floors; the
  victim file is untouched. Which subset went red each round was decided by which
  worker the poisoner and the victim shared, which is why a five-run pass saw four
  different red sets and a single run would have called each of them deterministic.

### The classes that were quiet but real

- **`spawn_no_cwd`, ranked by what was reached.** Four unit tests reached real host
  programs every round: `code_review_sage`'s `test_review_driver.py` ran the host's `gh`
  (a live token) for the draft read-back on `post=True` -- a GitHub API request for the
  made-up `o/r` repository from a unit test, 4 per round; `test_acp_client.py` spawned the
  installed `kiro-cli --version` 32 times per round through `_write_derived_permissions`'s
  version gate; three sandbox launcher suites paid the `lru_cache`d `ssh -V` after another
  file cleared the cache (32 per round; the checklist item on cached resolvers, now
  applied to the three files the seventh pass missed); and `test_pod_api.py` asked this
  host's `launchctl` about a pod named "demo" and `ps` about pid 4242 before the test
  pinned them. Each is pinned at the seam the product reads (`discovery._run_gh`,
  `kiro_cli.installed_kiro_cli_version`, `sandbox._ssh_supports_accept_new`,
  `runtime.main_pid` / `process_start_time`), and the probe confirms zero spawns of each
  afterwards. The `git` spawns with `cwd=None` are the CLI scripts' contract (see the tenth
  pass) and the fixtures `chdir` under `tmp_path`.
- **One real host path in a golden.** `test_acp_launch_goldens.py`'s DeepSeek capture
  stubbed `allocate_scratch` to the fixed `/opt/scratch/dsh-session` so the golden names no
  host path, and the spawn then `shutil.rmtree`d that window -- a real removal aimed at the
  host, harmless only while nothing lives there. This pass answered that one rmtree from the
  capture; the twelfth pass (below) saw the same class from the other side and replaced the
  fixed path with a real per-label directory under `tmp_path`, which is what ships. The
  golden is unchanged either way.
- **`kill` (210 tests) is own-tree signalling.** Re-measured with `own_child` recorded:
  every non-zero signal in the top five files went to a child the test spawned or to a
  grandchild of one (the provider-tree reap tests), never to a foreign pid. `thread_leak`
  (17 tests) is entirely named bounded pools (`mc-embed`, `mc-recall`, `mc-subproc`,
  `mc-pathres`, `mc-mcpprobe`, `mc-discovery`) warming on first use.

### What not to re-derive

`test_plugin_import_bounds.py::test_the_frontier_is_bounded_when_no_directory_carries_a_marker`
took 101 s once (round 1) and 30-41 s after: a nested-pytest test on a loaded host, the
same shape as the ipv6 one, left as is.
`test_instances_proxy.py::TestProxyRequest::test_failed_exchange_sends_no_link_and_remints`
failed once in round 1 with `ProxyRequestError: instance is not connected` immediately
after `connect()`; one occurrence under the round-1 probe is not attributable and is
recorded here, not fixed. `test_dashboard_files_onloop_fs.py` is green: its red was the
probe's (above). The `under_measured` residue (33 tests) is the per-test event cap on
genuinely chatty concurrency tests, not a gap in the suite.

## What a twelfth five-run pass found (Linux, 16 workers, 139,019 tests per run)

Five rounds of backend, vitest and electron on a test-only branch off one commit, on the
same 32-core host as the tenth pass, with a LIVE gateway out of the same data home and a
FRESH venv -- run the same day as the eleventh pass (macOS), on the other platform, from
the same base: 139,019 / 39,695 / 2,204 tests per round, identical every round. The backend
was red in every round with an unstable red set -- 12 tests failed 5/5, ten in one file
flipped between pass and fail in every combination, one passed exactly once -- and vitest,
green four times, went red once in round 5. Five production defects came out of it, two of
them user-visible: a PDF extractor that refused every PDF under a large gateway, and a
terminal whose Ctrl-C reached nothing when the gateway had been started as a background
job. The largest class in the report was, once more, the instrument.

The generalisable lesson this time: **inheritance across `exec` is a host property tests
never see at `-n0`.** A child's `ru_maxrss` starts at its parent's high-water mark; a
signal a launcher ignored stays ignored in every descendant; a process global one module
bound stays bound for every module after it on the worker. Each of the three surfaced as a
red in a test that was correct about its own subject, and each fix is at the seam that
inherits, not in the test that noticed.

### The instrument

- **The conftest's own cache tree is not a host write.** Run 1 filed 210 tests in 77 files
  under `host_write`; 209 were `.pyc` files landing in the `sys.pycache_prefix` mirror the
  rootdir conftest sets (a fresh venv compiles everything once) and the redirected
  hypothesis example database, both under `~/.cache/kirocrew` by that conftest's documented
  choice. The probe now sanctions the interpreter's bytecode prefix by construction and
  reads a per-sweep `allow-roots.txt` for roots a harness owns, and the analyser re-cuts
  runs recorded before the root was learned (`--allow-root`). After the re-cut the class
  held one test, and it was real.
- **A session-scoped fence lands on the worker's first test.** `GIT_CEILING_DIRECTORIES`,
  set once per worker by the seventh pass's conftest floor, read as an `env_leak` on 44
  innocent tests across five runs -- the same shape as the `XDG_RUNTIME_DIR` pop the
  analyser already excludes, and now excluded with it. The one real leak underneath was a
  module-scoped `mock.patch.dict(os.environ, {...})` in `test_issue_radar_crew_runtime.py`
  (159 of 160 tests, `KIROCREW_CREW_LOG` on and a foreign `KIROCREW_HOME` between tests);
  a function-scoped `monkeypatch.setenv` on the flag, and the conftest's own per-test home
  pin for the home, took it to 0 of 160 under a per-test env probe.
- **The driver ignored SIGINT for the whole suite.** `cmd &` in a non-interactive bash
  starts `cmd` with SIGINT and SIGQUIT ignored; `SIG_IGN` survives `exec`; so every xdist
  worker, every PTY shell a terminal test opened and every `sleep` under it ran with
  Ctrl-C disabled, and the one test that asserts Ctrl-C delivery failed 4/5 with the
  child's `SigIgn` bit set while the shell's was clean. The sweep's own `setsid` wrapper now
  resets inherited `SIG_IGN` to `SIG_DFL` before it execs the run. It was also a production
  finding (below): a gateway launched the same way ships the same terminals.
- **`under_measured` was volume, not findings.** Thirty-nine tests dropped events past the
  40-event cap; re-run alone with the cap raised, every one was the run-1 bytecode noise
  above or the test's own `cwd`-carrying `git`/`openssl` spawns (1,384 per test in one
  file). Zero host writes, zero checkout writes, zero connects in the complete streams.

### The reds

- **A child's `ru_maxrss` is its parent's, until the child grows past it.** All seven
  PDF reds (`test_pdf_extract.py`, `test_knowledge.py`, `test_file_grep.py`) were
  `PDF extraction failed: memory` with detail `rss`: the extractor child's own peak-RSS
  watchdog fired on its first 20 ms sample. On Linux `execve` folds the pre-exec image's
  high-water RSS into the process's `ru_maxrss` (`fs/exec.c`, `exec_mmap` ->
  `setmax_mm_hiwater_rss`), so a child forked from a 1.5-2.2 GiB xdist worker read more
  than the 1 GiB `--max-rss` before parsing a byte -- measured on this host as a 1,312 MiB
  child `ru_maxrss` against an 11 MB `VmHWM`. At `-n0` the runner is small and all 44
  tests pass; a live gateway over 1 GiB cannot extract any PDF. The fix is production:
  `pdf_extract_child.peak_rss_bytes()` reads the child's OWN `VmHWM` from
  `/proc/self/status` on Linux (unreadable -> `None`, watchdog off, never the inherited
  number); `ru_maxrss` stays the macOS source, where it is per-process. Pinned by a fake
  status text through the `_LINUX_STATUS_PATH` seam and by a test whose PARENT touches
  256 MiB, asserts its own `ru_maxrss` exceeds the 128 MiB ceiling it hands the real
  child, and still gets page 1 back. The seven tests are unchanged.
  `platform_compat.proc_peak_rss_bytes()` -- the dashboard's `proc_mem_peak_mb` and the
  `process.memory.peak_rss_bytes` gauge -- read the same inherited number, so a gateway
  started from a large parent published a peak that was wrong from its first sample and
  never moved; it reads its own `VmHWM` now too, clamped monotonic (the kernel folds live
  RSS into `hiwater_rss` lazily, and a raw read dips a few hundred KiB after an `munmap`),
  pinned by a bloated-parent -> child spawn that asserts the inheritance is present in the
  raw `ru_maxrss` and absent in the reader.
- **A test that plants a fake executable under `tmp_path` and expects a LATER guard to
  fire is asserting that `tmp_path` lies outside every checkout.** The five
  `test_metachar_arg_refused_for_cmd_launcher[...]` reds failed one check early, at
  `preflight.run()`'s working-tree fence, because this sweep's `TMPDIR` sits under the
  worktree. CI passed by accident: `which()` was patched to answer the `.cmd` for `git`
  too, so the fence's `git rev-parse` exec'd a text file and the resolver swallowed the
  `OSError`. The fix pins the fence root at the seam `run()` reads (`_WORKTREE_ROOT`) to a
  `tmp_path` sibling that is not the fake's ancestor -- the seventh pass's class, one more
  spelling -- and four neighbours that paid a real host `git rev-parse` for a root they do
  not test got the same pin (6 -> 2 `git` spawns per run; the two left are the tests OF
  the probe).
- **The same stale dispatcher, on Linux.** Ten tests in `test_trusted_apps_api.py` flipped
  between runs, every failure a 409 `teardown_incomplete` whose body named `hooks disable
  failed: object MagicMock can't be used in 'await' expression` -- the eleventh pass's
  finding, measured here as a different subset of ten each round under sixteen workers.
  Its rootdir restore floor is the fix; this pass adds the leaker's own contract on top:
  the startup-coverage module's `_start_dashboard` pins
  `hooks_integration._lifecycle_dispatcher` / `_route_registry` to `None` BEFORE it boots
  the real server, so `monkeypatch` puts back what the test found. Attribute by the
  failure body, not by the endpoint that answered.
- **A launcher's `SIG_IGN` reaches the user's terminal.** `test_ws_ctrl_c_delivers_sigint`
  passed once and failed four times. The failing evidence showed the PTY delivering `^C`
  to the right foreground group and `sleep` ignoring it (`SigIgn` bit 1) while bash was
  clean: bash keeps a signal that was ignored on entry ignored in every command it runs,
  and the shell inherited it from the worker, which inherited it from the driver's `&`.
  The passing run was vacuous: the test sent `\x03` on the line-discipline ECHO of
  `sleep 120`, before bash had read the line, so `VINTR` flushed the unread input and the
  marker echo ran in 48 ms with no `sleep` ever born. The production fix is in the exec
  shim: on the `--ctty-fd` path, after `login_tty` and before `execv`, every disposition
  that is `SIG_IGN` is reset to `SIG_DFL` (what `login` and `sshd` hand a shell), and the
  set is re-ignored if `exec` does not happen so the in-process shim tests leave the worker
  untouched. The test now gates the first Ctrl-C on `tcgetpgrp` of the PTY's own descriptor leaving the
  shell's group, asserts the shell gets the foreground back afterwards, and reaps the
  session in a `finally` -- the `leaked_child`, `thread_leak` and `slow` rows on this
  nodeid were all its 35 s failure path. 6/6 green under a launcher that ignores SIGINT.
- **The clock is a dependency, and `not.toContain` is a substring.** The one vitest red
  (`CrewWebviewContainment.test.tsx`, round 5) rendered relative ages from the real
  `Date.now()` against a fixture instant fixed in September; the "shown version" label
  crossed from `22d ago` to `23d ago` during the sweep, and `not.toContain('3d ago')` is
  satisfied by `23d ago`. It went red on `main` the same day and was fixed on its own
  (#14398 asserts the whole interpolated sentence; #17237 pins `Date` with
  `vi.useFakeTimers({ toFake: ["Date"] })`), so this pass carries no change for it. A
  test that compares a real-clock age to a sub-second or exact-unit output pins the
  clock the same way: unpinned, `timeAgo.test.ts` read `1s ago` where it expected `now`.

### The classes that were quiet but real

- **`spawn_no_cwd`: three binaries, three seams.** Real `kiro-cli --version` ran from
  unit tests in eleven files, exactly once per worker per run (80 over five runs): every
  agent-spec write ends in `installed_kiro_cli_version()`, cached process-wide, so the
  HOST's install decided whether `permissions` was written and which test paid the spawn.
  Each module now carries the house pin to `SPEC_PERMISSIONS_MIN_VERSION`. Real `ssh -V`
  ran from nine sandbox files -- the `functools.lru_cache`d accept-new probe behind
  `_build_launcher_script` the tenth pass pinned in six others; with the eleventh pass's
  three and this pass's seven, the same one-line autouse fixture now sits in every
  module that builds the launcher (a per-`Popen` recorder, not a
  passing suite, is what showed 0). `push_guard.py`'s two `git patch-id --stable` calls
  now go through `run()`/`_GIT_CMD`, with a test that fails before the change. A test in
  `test_browser_cli_view.py` hand-rolled three of `_stub_port_owner`'s pins and dropped
  `process_descendant_identities`: 242 real `ps` snapshots against a pid it did not own,
  and 27 s -> 7 s for the file once it used the helper. The doctor's `warm_backend()`
  re-probed userns on every `_doctor()` call past a warm cache (44 spawns -> 1);
  `code_fingerprint`'s cache paid a real `git` in two more modules (pinned as the daemon
  lifecycle module already was). `green_age.run()`'s 152 `cwd=None` spawns per round were
  recorded WITH the process cwd this time: every one ran under the scratch root, so the
  deliberate descriptor stays and the docstring now says why.
- **`fd_leak`: the tenth pass's seams, applied.** Thirteen tests at +5..+10 in eight
  files, all unclosed SQLite handles or a dashboard boot's process handles: stores routed
  through `test/conftest.py`'s `opened` fixture (a `KnowledgeStore` per-thread `close()` never
  reaches the connection a `to_thread` worker opened; the test-only every-thread close
  does), `close_skills_loaders` requested from module autouse fixtures where
  `ContextBuilder`s or an `env` fixture build a `SkillsLoader`, `SubagentManager`s tracked
  and closed, and a boot that RAISES before returning its state now has that state
  recorded through the constructor so `_release_process_handles` still runs. Per-test fd
  probe: every flagged test at +0; the only +3 rows left are pytest-asyncio's replacement
  loop.
- **`heavy_rss`: a memoised read seam.** `run_scoped_tests._read_text` was an unbounded
  `lru_cache`: the local gate's reference scan retained the text of 3,172 test files
  (+189 MiB) for the life of the process, for a cache that saved a 0.4 s re-read. It
  streams now, pinned by a shape test. `test_source_corpus.py` materialised the corpus
  twice per test (`tuple(...)` plus `set(...)`); the tests consume the iterators (count,
  path set, `zip(strict=True)`), +264 -> <20 MiB. On the way, `repo_files()` was letting
  `git ls-files --others` WALK the run's own scratch under the checkout (126k files, 20 s
  per first-asking worker) and filtering afterwards; the same scratch roots and pytest's
  `pytest-of-*/` marker are now `--exclude` patterns git prunes, 27 s -> 0.5 s, and about
  ten `slow`-class consumers of it clear without a change of their own.
- **`host_write`: one, and it was a stub.** The DeepSeek arm of the launch-golden capture
  answered `agent_scratch.allocate_scratch` with a FIXED absolute path,
  `/opt/scratch/dsh-session`, and that arm WRITES into the window it is handed
  (`record_owner` unlinks `.owner`, the gate probe `rmtree`s it) from executor threads --
  unattributable by frame, host by path. The stub answers a real per-label directory under
  `tmp_path` now, pinned by a test that wraps the writers and asserts every path
  `is_relative_to(tmp_path)`. A "synthetic-looking" literal is still a path.
- **`thread_leak` was named pools, again.** `mc-mcpprobe_*`, `mc-subproc_*`,
  `mc-embed_*`, `mc-recall_*`, `mc-pathres_*`, `mc-discovery_*`,
  `skill-catalog-refresh`: bounded pools warming on first use, left alone. The unnamed
  `Thread-N (_do_shutdown)` / `asyncio-waitpid` rows were the terminal test's failure path.
- **`kill`: 707 signals, zero non-zero signals at a foreign process.** An ownership
  probe (registering every `Popen`/`fork`/`posix_spawn` pid, auditing `os.kill`/`killpg`,
  classifying each target by registered pid, `/proc` lineage, own process group, own
  thread, or an inherited marker variable) over the 68 flagged files, at `-n0`, twice:
  395 signal-0 liveness probes, 190 SIGKILL, 120 SIGTERM, 2 SIGINT. Every SIGTERM and
  SIGKILL went to a registered child, a descendant by lineage or group, or a number the
  body had already proven dead (two: the fabricated `2000000000` and production's
  `_sync_kill_provider` at an already-reaped grandchild, ESRCH). The six "external"
  targets left were all signal 0 through `platform_compat.pid_exists` / `pid_liveness` /
  `pgroup_exists` at fabricated pids (`1`, `123`, `1001`) or at the test's own `setsid`
  grandchild after init had adopted it -- class 9's sanctioned shape. Exactly two
  signal-0 probes came from anywhere else, and they were the real defects: a naked
  `os.kill(pid, 0)` poll in `test_cli_manifest_signature.py` (routed through
  `pid_exists`), and the twin driver-reap tests in `test_codex_session_mcp.py` /
  `test_opencode_session_mcp.py`, which polled a grandchild pid read from the driver's
  stdout with `os.kill(pid, 0)` and SIGKILLed the RAW number on the failure path after the
  driver's group had been killed -- class 27: the driver now prints
  `get_process_start_id(pid)` at spawn, the poll treats an identity mismatch as gone, and
  the failure-path kill is `kill_pid_pinned`. Post-fix census: signal 0 outside
  `platform_compat`, 2 -> 0.

### What not to re-derive

The `unattributable` class (49,815 tests, every second file) is pytest's `tmp_path`
retention sweep and the repo's pinned-descriptor writers, as the tenth pass established;
the analyser excludes it from the partition. `green_age.run()` and the auto-improvement
`git -C` helpers keep `cwd=None` (see the tenth pass), now with the process cwd recorded
per spawn to prove it harmless. The exec shim still leaves `SIGPIPE`/`SIGXFSZ` ignored for
NON-terminal spawns (a Python shim re-ignores them after the fork child's
`restore_signals`), so `yes | head` in a spawned `bash -c` gets `EPIPE` -- a latent defect
with its own test to write, deliberately not changed in this pass because it alters tool
spawn semantics. `session_pid._sync_kill_provider` SIGKILLs a snapshot pid through the unpinned `kill_pid`
(the census saw it hit an already-reaped grandchild, ESRCH) -- a production class-27
candidate for its own change, with a test that plants a reissued number. The dashboard
files' `aiohttp` warnings remain class 15.

## What a thirteenth five-run pass found (Windows host, eight workers, 126,965 tests per run)

Native Windows (Server 2025, 16 cores), five rounds of the backend suite under the sweep
skill's per-test probe on a test-only worktree off one commit, `-n 8 --timeout 120`, the
results directory outside the checkout: 121,041 to 121,046 passed, 23 to 28 failed, 0 errors and 5,742 skipped per round, 59 to 80 minutes each (the 80 was the round during which eight extra workers reproduced a finding on the same host), minimum available memory 27.1 GiB. Twenty of the reds were the same twenty every round; the rest were four `pytest-timeout` worker kills a round and, once, two load-induced budget misses. Every round was comparable end to end;
no round was lost. Two tests were red in all five rounds and are the host, not the suite
(`test_crew_image_publish_contract.py`'s shared-fixture shell test and
`test_windows_fleet_setup.py`'s `[pwsh]` case fail BY DESIGN on a host whose `PATH` carries
no `bash` and no `pwsh`). Everything else red was reproduced on a clean second worktree at
the same sha before it was touched, and sorted into four mechanisms -- one of them a
production defect that no Windows developer running from a venv could have missed, one
already fixed by the concurrent macOS pass, and none of them visible to CI, which runs
every one of these files green.

The generalisable lesson this time: **a test that passes on CI and fails on a developer's
Windows box is asserting something about the CI runner.** Three of the four were exactly
that -- a `HOME` the runner exports and a server session does not, a `python.exe` that is
an interpreter on the runner and a redirector in every venv, a `sleep` shim the runner's
Git Bash launcher happens to outrank too but only costs time there. Read the failing
assertion for the host fact it depends on before reading the test for a bug.

- **A `PATH` shim for a coreutil is not an override under Git for Windows.**
  `test_withheld_verdict_retention.py` and `test_review_slot_read_error.py` drive the
  review lanes' real bash with a scripted `gh` and a `sleep` that exits at once, both
  planted in a directory prepended to `PATH`. Git for Windows' `bin\bash.exe` is a
  launcher that prepends `/mingw64/bin:/usr/bin` to whatever `PATH` it is handed, so `gh`
  (not under `/usr/bin`) resolved to the stub and `sleep` resolved to `/usr/bin/sleep`:
  every failing-`gh` case slept the lane's whole retry budget, `5+10+15+20+25` s. Measured
  at 76-77 s per test, 3,470 s of worker time in one file per round, and four
  `pytest-timeout` worker kills per round (the `thread` method exits the worker, which
  xdist reports as `worker 'gwN' crashed`). On CI the same tests pass at the same cost,
  45 s inside the shard's `--timeout=120`. The override is now a shell FUNCTION defined at
  the top of the driver script (`sleep() { printf '%s\n' "$1" >> sleep-calls.txt; }`):
  functions are resolved before any `PATH` lookup on every platform, and each call is
  recorded. The pin, `test_the_retry_backoff_is_intercepted_not_slept`, runs the
  measured instance and asserts the recorded schedule is `5 10 15 20 25` -- an event
  record, not a stopwatch; with the override removed it fails on "no sleep reached the
  override" after 151 s. Both files: 444 tests in 54 s at `-n 4`.
  `test_ai_review_workflows_comment_writes.py` carries the same shim at five sites and already skips
  them on Windows outright; un-skipping them through the same function is a follow-up
  that needs a host with `jq`.
- **A process ceiling of one refuses the venv redirector's own spawn.**
  `pdf_extract._windows_ceiling` attached a Job object with `ActiveProcessLimit=1` to
  the suspended extractor child, "this child spawns nothing". Under a venv on Windows,
  `sys.executable` is `Scripts\python.exe`, the venv REDIRECTOR: it reads `pyvenv.cfg`,
  `CreateProcess`-es the base interpreter as its own child and stays alive as its
  parent. With the limit at one that `CreateProcess` fails with `ERROR_NOT_ENOUGH_QUOTA`,
  the redirector prints `Unable to create process using '"C:\Python312\python.exe" -s -P
  -m kiro_crew.pdf_extract_child ...'` and exits 101, and the reader files it as
  `protocol`. So PDF extraction never worked from a venv-hosted gateway on Windows; the
  five red tests (`test_file_grep.py::TestDocumentPass`, `test_knowledge.py::
  TestFileReaderPdf`) were deterministic, and CI's `setup-python` interpreter is not a
  redirector, which is why they are green there. Probe: the same document under a limit
  of 1 fails `protocol`, under 2 extracts `Hello PDF regression`. The fix is
  `platform_compat.python_launcher_hops()` -- `1` when `sys.executable` and
  `sys._base_executable` name different files on Windows, `0` otherwise -- and the
  ceiling is `1 + hops`. Pins: `TestWindowsCeiling` asserts `max_procs == 2` when the hop
  is `1` (and keeps the `== 1` pin with the hop pinned to `0`); `TestPythonLauncherHops`
  derives the number on both platforms with pinned `sys` executables; and a native test
  spawns `sys.executable -c` under a ceiling of `1 + hops` and asserts it ran, then --
  on a venv host, where it can be shown -- under `1` and asserts exit 101 with the
  redirector's message. Over-counting would still bound a fork bomb; under-counting is
  the defect. The review lane then named the other half of the same shape: on the
  deadline path `_kill` terminated the pid alone, which under a redirector is the
  parent of the interpreter parsing the document, and the Job carries no
  `KILL_ON_JOB_CLOSE` by design -- so the interpreter's end rested on the redirector's
  own kill-on-close job, a CPython launcher detail (measured here: with the tree kill
  neutralised the interpreter still died with the redirector). `_kill` now kills the
  tree on Windows first, while the redirector is alive to name its child, so the
  guarantee is the gateway's; pinned on the faked path (`kill_process_tree` receives
  the applied pid) and natively on a venv host (the interpreter's pid is gone after
  the timeout).
- **A pass-through assertion needs the key in the parent.** `test_r8_s33_subprocess_env.py`
  asserts the scrubbed child env still carries `HOME`; the scrub passes it through when
  the parent has it, and this host's session has no `HOME` (Windows spells it
  `USERPROFILE`; the CI runners export both). Five tests red every round, measuring the
  host. An autouse fixture now plants `HOME` under `tmp_path` so the assertion measures
  the allowlist.
- **A process global a server-building test publishes outlives the test.**
  `apps.hooks_integration.init_hooks_system()` sets `_lifecycle_dispatcher` and
  `_route_registry` and nothing clears them. `test_dashboard_server_startup_coverage.py`
  builds the server with a `MagicMock` cron service; every later test on that worker then
  runs `teardown_app_runtime`'s cron cleanup against the mock, whose `remove_all_async`
  is not awaitable, and the teardown reports `hooks disable failed` -- so
  `test_trusted_apps_api.py`'s ten revoke tests answered 409 `teardown_incomplete` in
  every round, spread across whichever workers had run the polluter first, and 113/113
  green in any selection that had not (the neighbour set at `-n 8`, the whole
  `test_[s-z]*` tail at `-n 8`). Found by reading the revoke handler's three 409 branches
  and installing each candidate's leftover through a plugin until the body matched;
  reproduced deterministically at `-n0` by running the two files in order (10 red on the
  base). The eleventh pass, on macOS, met the same leak from the other side and carries
  the fix in [#14388](https://github.com/kirodotdev/KiroCrew/pull/14388): a rootdir
  conftest floor that hands the next test the two globals it inherited, pinned in
  `test_host_isolation_floor_restores.py`. This pass adds only the witness the diagnosis needed:
  the revoke tests' status assertions now carry the response body, so the next 409 names
  its branch in the failure instead of `assert 409 == 200`.
What was flagged and read before being left alone. `slow`/`heavy_cpu` was dominated by the
first mechanism above; the rest were the suite's own budgets (`test_2000_submissions_...`
85-110 s under its `timeout(900)` marker, the two `test_members_dm_thread_activity_and_briefing` thousand-event
reads at ~99 s). `env_leak` was again the session-scoped temp-root fixture's arm and undo
read against a worker's first and last test. `host_write` was the bytecode mirror and the
hypothesis database, `test_computer_use_launch.py`'s deliberate real-install-directory
probes and the data-home floor -- nothing touched the live data home or the checkout.
`under_measured` was the probe's own budget on tests that spawn real children.
`test_black_fleet_budget.py`'s two real-spawn cases hit their 45 s budget once, in the
round during which eight extra workers were reproducing the fourth mechanism on the same
host (6-11 s in every other round): the operator's load, not the suite's.

## What a fourteenth pass found (Windows host, eight workers, three rounds of 148,466 tests)

Native Windows (Server 2025, 16 cores), the backend suite under the sweep skill's per-test
probe on a test-only worktree off one commit, `-n 8 --timeout 120`, the results directory
outside the checkout and outside the session scratch: three comparable rounds of 148,466
tests each (141,113 to 141,114 passed, 5 to 6 failed, 3 errors, 7,182 skipped; 41 to 43
minutes; residue 0; minimum available memory 19.4 GiB). Rounds four and five were LOST, not
red: the gateway hosting the operator's session restarted at 67 percent of round four and
the pytest tree died with it, although the driver had been launched through WMI outside
that session's Job object -- an unknown outcome under the skill's rule, discarded, and the
flake and repeatability classes below are judged over three rounds rather than five. The
two permanent host reds (no `bash`, no `pwsh` on `PATH`) were present in every round, as in
every Windows pass. Everything else red was reproduced at `-n0` as a polluter-plus-victim
pair before it was touched and sorted into five mechanisms, all of them in tests, none of
them visible to CI -- plus one cost that IS paid on CI: 210 s of deliberate waiting per
round in six tests that pass. The first cut of one fix (a `pytest.skip`) was itself a
review finding; the version below is what the lane accepted.

- **`pytest.raises(...) as exc` keeps the subject alive through the test's own frame.**
  `test_crew_log_edge_exhaustion.py`'s teardown pin (`lease._held` empty) read a lease from
  `test_crew_log_core.py::test_an_entry_over_the_size_ceiling_is_refused_whole` in all three
  rounds, the first three tests on that worker erroring until the cyclic collector got round
  to it. The `as exc` binds an `ExceptionInfo` in the test frame; it holds the exception,
  whose traceback holds the test frame (a cycle) and `CrewLog.append`'s frame, whose `self`
  is the handle, whose `weakref.finalize` is the lease release. Twenty-five tests in that
  file had the shape -- every refusal asserted after a successful append -- and each held
  its lease until GC. Traced with `gc.get_referrers` from the handle. The first cut, a
  `@contextlib.contextmanager` that caught the exception in an `except ... as exc` clause
  (which Python unbinds), still left 26 tests holding: an exception THROWN into a generator
  grows its traceback by the generator's frame, whose `f_back` is `__exit__`'s frame, whose
  `value` is the exception -- the same cycle one layer down. The fix is a CLASS whose
  `__exit__` copies `code`/`field`/`message`/`written` into a plain record and returns; its
  frame is the only thing that ever named the exception. The pin moved to the creator:
  the autouse fixture in `crew_log_core_helpers.py`, which `test_crew_log_core.py` and its
  parts bind, asserts `lease._held` empty after every test,
  WITHOUT `gc.collect()` (release rides the refcount; a lease held there is a retention).
  39 errors under the old helper, none under the new. The old `_raises(code)` also never
  compared its argument, and one site had said `bad_src` for a refusal the product spells
  `event_type_not_owned` (a crew type in a session log); the helper compares now, and the
  site says what it means.
- **A stubbed releaser leaves a process-global tenancy alive for the worker.**
  `test_cron_reaper_failed_kill.py::test_a_refused_pid_is_audited_as_a_failed_kill_not_reaped` read
  `kill failed: runtime still leased by another tenant` for its fabricated pid 4242 in one
  round of three -- the round xdist placed it after
  `test_connections_mint_spec_files.py::test_the_mint_pid_is_protected_while_readiness_is_still_stalled`
  on one worker. That test replaced `_dispose_mint`, the only path that releases the
  `RUNTIME_TENANCY` claim the pid shield takes, with a no-op; and its client stand-in
  answers neither `is_alive` nor `is_process_alive`, which the table reads as alive for
  ever. Found by diffing the files that preceded the red victim on its worker against the
  files that preceded the green ones, then reproducing polluter plus victim at `-n0` (two
  tests, one red). Fix: the fake dispose releases what the flow claimed
  (`release_runtime_tenancy(holdings.pop("tenancy"))`), and the test pins
  `RUNTIME_TENANCY.claims_on_pid(4242) == 0`; with the release removed the pin reddens. The
  rule is the fourth pass's -- stub the only thing that releases a resource and the fixture
  owes the release -- met on a liveness-judged table, where a stand-in with no probe cannot
  expire on its own.
- **A tool the script under test requires, supplied by the harness rather than skipped.**
  `.github/scripts/pr-body-snapshot.sh` fails closed without `jq`, and three
  `TestUxReviewReadsTheScreenshotsBlindFirst*` evidence-step cases in
  `test_ai_review_workflows_ux*.py` were red every round on a host whose Git Bash has none.
  The first cut gated them (`pytest.skip` when the bash resolves no `jq`), and the GPT
  lane blocked it under `a-ratchet-may-only-tighten`: a newly added `skip` in a file the
  diff otherwise only extends is a loosened ratchet, whatever the spec's capability-gate
  rule says about a test that ASSERTS a host fact. (The rule's capability exception does
  not reach this case: the tests already existed, and `jq` is a tool the harness can
  supply.) The remedy is the one the harness
  already uses for `gh` and `curl`: a shell FUNCTION in the `BASH_ENV` stub file. `jq()`
  hands its arguments to a twelve-line Python stand-in that implements the three
  invocations the step and the gh stub make (`-Rs .`, `-r '.title'`, `-r '.body // ""'`),
  byte-exact through `sys.stdin.buffer` / `sys.stdout.buffer` (a text-mode stdout on
  Windows would put `\r\n` into the title file and change every digest computed over
  it), and exits 2 on any other filter, so a step that grew a fourth jq call is caught.
  Defined only where `command -v jq` fails in the bash the step runs under
  (`_bash_has_jq`, probed through that bash rather than `shutil.which`, since Git for
  Windows' launcher prepends its own `/usr/bin`); a host with the real binary runs the
  real binary. 21 cases pass on the jq-less host that reddened three and skipped none
  of them before. The thirteenth pass's PATH sleep-shim follow-up is a separate item
  this does not close.
- **A hung reset the test does not bound waits the shipped `_RESET_TIMEOUT`.** Five
  `TestEveryCandidateUnderTheKeyIsKilledOnItsOwnHandle` cases in
  `test_subagent_force_stop_audit.py` hand the reaper `_hanging_reset` (a reset that sleeps
  999 s) without the `patch("kiro_crew.subagent._RESET_TIMEOUT", 0.05)` every sibling class
  in the file carries: 30.04 s each, 150 s of worker time per round, on CI exactly as here.
  `test_subagent_reap_race.py::test_cancel_all_keeps_the_tombstone_when_delivery_already_happened`
  spent 60 s the same way: its docstring described a report "cancelled in the wait that
  FOLLOWS a successful delivery", but the report now waits for its teardown gate BEFORE it
  publishes (`_RESET_TIMEOUT + _TEARDOWN_REPORT_GRACE`), so the test reached delivery only
  when that grace ran out, and its assertion -- a delivered report is not re-admitted --
  never depended on the wait's length. Both constants are pinned small and the docstring
  says what is measured. All six were named by `classify.py`'s TIMEOUT-SHAPED list: a wall
  time landing exactly on a product constant with the CPU idle.
- **`delenv` of an absent key records nothing to undo.**
  `test_macos_x86_64_cpu_guard.py::test_load_llama_proceeds_past_guard_when_macos_x86_64_capable`
  called `monkeypatch.delenv("LLAMA_CPP_LIB_PATH", raising=False)` and then let the loader
  `os.environ.setdefault` the key to a fake `tmp_path` libs directory; `delenv` on a key
  that is not there registers no undo, so the path outlived the test on every worker that
  ran it (`env_leak`, three of three). Invisible from a shell whose gateway already exports
  the key -- the gateway's own loader set it, which is why the probe read clean under the
  operator's shell and red under the sweep's -- and reproduced by unsetting it first. Fix:
  `setenv` the key before the `delenv`, so the undo entry removes whatever the loader leaves.

What was flagged and read before being left alone. `host_write` was 923 of 948 events in
the bytecode mirror and the hypothesis database; the rest were
`test_computer_use_launch.py`'s deliberate real-install-directory probes, the
`kc-pytest-*-home` data-home floor under the system temp, and `\\?\`-prefixed
`basetemp` paths the probe's classifier does not fold back onto the run root
(`test_crew_teams.py`, `test_work_ledger.py`: an instrument gap, not a test). `leaked_child`
was 55 of 57 rows the two children of the process-wide `path_resolve_executor` pool,
created on first use and shut down at exit, attributed to whichever test first resolved a
sensitive path on each worker; the other two were `test_subprocess_pool.py`'s deliberately
wedged children under `shutdown(wait=False)`, which the reaper collects by design.
`thread_leak` was named pools only (`mc-embed`, `mc-recall`, `mc-mcpprobe`, `mc-subproc`,
`mc-pathres-reaper`). `env_leak` was otherwise 22 x `GIT_CEILING_DIRECTORIES`, the
session-scoped conftest pin read at each worker's first test. `under_measured` was the
probe's own event budget on tests that spawn real children. Nothing touched the live data
home or the checkout.

## What a fifteenth five-run pass found (macOS host, five workers, 152,063 tests per run)

macOS (Apple silicon, 48 GiB), five rounds of the backend suite under the sweep skill's
per-test probe on a test-only worktree off one commit, `-n 5` niced with a 4 GiB memory
watchdog floor, the results directory inside the worktree but outside every test root;
plus the vitest and electron suites once per round. Backend: 151,100 to 151,104 passed,
3 to 7 failed, 0 errors, 946 skipped, 29 to 40 minutes per round (the 40 was round one,
under the unpatched probe); vitest 41,106 passed and electron 2,298 passed every round;
minimum available memory 14.6 GiB; the watchdog never fired. Two tests were red in all
five rounds and are the host, not the suite (`test_kiro_cli_pin.py`'s two shadowed-`PATH`
cases: the desktop app exports its bundled `kiro-cli` directory to every child, the test
process was started from a dashboard session and inherited it, and `known_kiro_cli_dirs`
ranks that directory FIRST -- the product behaving correctly; the fake host now drops the
variable too). Everything else red was a race that went green in at least one round.

The generalisable lesson this time: **a process-lifetime pool created lazily during a
test is not that test's leak, and a probe that says otherwise is measuring scheduling.**
Seventy-three of the eighty-five `leaked_child` records were the shared
`executors.path_resolve_executor()` pool's two children, charged to whichever test first
resolved a path after the previous teardown; every `env_leak` was the session temp-root
fence landing on a worker's first and last test; every `thread_leak` but one was a named
`mc-*` pool warming once. Read the record for WHO spawned the thing before reading the
test for a bug -- and when the owner turns out to be a pool, fix the pool.

- **A shutdown that races its own reaper refills the slot it just emptied.**
  `SubprocessPoolExecutor.shutdown` set the stop flag and killed every child, while the
  reaper thread -- the ONLY thing that spawns -- was mid-tick past its second stop check
  and spawned into the slot a moment later. Measured as a `sleep(3600)` test child alive
  at teardown in four rounds of four (`test_subprocess_pool.py::TestCallerBudget`), as 18
  of 80 shut-down-at-once pools keeping a live child in a stress probe, and as the
  `mc-pathres-reaper` children behind most of cluster E. `shutdown` now joins the reaper
  (bounded, `_SHUTDOWN_REAPER_JOIN_SECS`) BEFORE the kill loop, skipping the join only
  when the caller is the reaper itself; the same probe reads 0 of 80 after. A
  `test/conftest.py` session fixture (the `test/` suite, not the built-in app tests) now
  also calls `shutdown_maintenance_executor()` at session end, so the
  shared pools are reaped where the leak reporting can see it rather than at `atexit`.
- **A nonexclusive `O_CREAT` open can lose the create race on Darwin.** Two writers on a
  fresh log directory -- the SEL background writer's first flush and a `prune` -- both
  opened the chain-lock sidecar with `O_CREAT`; one came back `ENOENT` for a bare
  `security_events.lock` and the prune was skipped, so `test_sel.py`'s concurrent chain
  read 23 of 24 (rounds 1-3) and `test_sel_prune_streaming.py` lost appends (rounds 1, 4,
  5). Same shape as the decision log's create. `_open_lock_sidecar` now creates
  EXCLUSIVELY first and reopens without `O_CREAT` when a sibling won; a leaf that vanishes
  between the two calls is a genuine `ENOENT`, left to the caller.
- **A kill gate that answers for a neighbour's lease.** `runtime_ownership` keeps one
  process-wide lease table and the kill gate refuses any pid found in it; test doubles
  reuse a handful of pids (`4242` appears in about two hundred files), so a lease left by
  an earlier test on the same worker made `test_cron_reaper.py` and
  `test_subagent_force_stop_audit.py` read `outcome == "refused"` where they assert the
  failed-kill wording -- three reds in one round, green in four. An autouse conftest reset
  now clears both tables on both sides of every test.
- **A wall-clock barrier as a race detector.** `spec_builder/tests/test_routes_delete.py`'s
  tombstone race parked both writers on a `threading.Barrier(3, timeout=10)`, but once the
  first holds `_INDEX_LOCK` the second can never reach the barrier, so the CORRECT code
  paid the full 10 s `BrokenBarrierError` every run and told pass from fail by elapsed
  time. It now parks the first reader on an `Event`, signals the second writer's arrival
  at the lock through the facade, and asserts exactly one read happened while parked --
  0.3 s, and both planted races still fail.
- **`Popen.kill()` is a shutdown, not a reap.** 7,765 `os.kill` events in runs 2-5 were
  walked by caller: 5,629 were signal 0 through `platform_compat`'s liveness helpers,
  2,136 were `Popen`/`asyncio.Process` kills or start-id-pinned helpers at a child the
  test or production spawned inside the test, and 0 were a signal at a pid the test did
  not create. The census (not a fix) is the deliverable; the probe still cannot tie a kill
  to a spawn because spawn events carry no pid.
- **A `monkeypatch.chdir` leaves the descriptor `cwd=None`.** 155 real `git` spawns from
  two files (`auto_improvement/tests/test_suite_scope.py`, `test_prepare_pr_green_age.py`)
  ran with the worker's cwd changed under them, indistinguishable to a per-spawn audit from
  a spawn in the checkout. Both production helpers keep `cwd=None` by contract and already
  expose the seam, so the tests now pin it: `runner=partial(subprocess.run, cwd=...)`, or
  the script module's `subprocess` binding replaced with a namespace whose `run` carries
  `cwd`. One spawn remains suite-wide, the rootdir conftest's one-time sandbox probe; it
  now runs in the profile's own temp dir.
- **An object whose constructor opened SQLite, dropped without a close.** `SubagentManager`
  (`tasks.db`), `KnowledgeStore` touched from a worker thread (per-thread connections, so
  `close()` releases only the caller's), `SkillsLoader` built inline: +3 to +9 descriptors
  per test, GC-timed, in eleven files. Every one already had a production close path;
  `close_subagent_managers` joins `close_skills_loaders` as an opt-in `test/conftest.py`
  fixture and replaces ten identical module-level copies, `_close_all_for_tests()` is used
  where another thread held a connection, and `close_skills_loaders` now joins the closed
  loader's `skill-catalog-refresh` worker so a thread probe sees it gone.

What was flagged and read before being left alone. `under_measured` and 54,197
`checkout_write` records in round one were the probe (bare filenames pytest's
`rmtree`-by-descriptor hands the audit hook, glued to the repo root; fixed before round
two). `host_write` was the hypothesis example database the conftest redirects to
`~/.cache`. `kill` is the census above. `thread_leak` outside the one `SkillsLoader` case
was `mc-embed`, `mc-recall`, `mc-maint`, `mc-mcpprobe`, `mc-subproc` and `sel-writer`
warming to their caps. `slow` was the suite's own budgets, plus the five
`test_subagent_force_stop_audit.py` cases at 30 s each -- a missing `_RESET_TIMEOUT` pin,
which the concurrent fourteenth pass (native Windows, three rounds) found and fixes in
[#15061](https://github.com/kirodotdev/KiroCrew/pull/15061), together with the
`test_connections_mint_spec_files.py` tenancy stub behind the same refused-kill wording the conftest
reset above floors.
