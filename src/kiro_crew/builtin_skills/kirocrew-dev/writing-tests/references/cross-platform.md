# Rule 3 — Cross-platform: macOS, Linux (x86_64 + arm64), Windows

Moved verbatim from the writing-tests `SKILL.md` core.

- **Route POSIX calls through `platform_compat`.** See docs/system-specs/common/platform-compat.md. Most
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
- **Windows clock granularity.** Through Python 3.12 `time.time()` and
  `time.monotonic()` step ~15.6 ms, and so do `Event.wait`, lock and queue timeouts;
  only `time.sleep` has been high-resolution since 3.11 — so a short sleep can put two
  writes on one stamp, and "two writes have different timestamps" is a flake there
  (class 7).
- **Probe, do not guess the platform.** `test/conftest.py::_can_create_symlink` is the
  model: creating a symlink needs `SeCreateSymbolicLinkPrivilege`, which CI runners
  hold and an ordinary shell does not, so a blanket `skipif(IS_WINDOWS)` would drop the
  assertion exactly where it needs to run. Use the `make_escaping_link` /
  `make_dir_link` helpers, which fall back to a junction.
- **Probe with the program under test, not a stand-in.** When a live test picks its
  expected branch from a host probe, the probe makes the same check the program makes,
  with the binary it runs (codex's own bubblewrap preflight, not Crew's
  `userns_available()`). AppArmor judges each binary by its own profile, so one
  process's refusal does not predict another's: on the `ubuntu-26.04` runner Crew's
  probe was refused while codex's sandbox ran, and the codex inner-sandbox tests were
  red on every PR that runner served. A second probe answers only where the program's
  own check gives no verdict, never as a veto ahead of it (testing-conventions § A host
  probe that asks a different program than the one under test).
- **arm64 vs x86_64** rarely matters for test logic, but check it when you touch memory
  or page arithmetic (`SC_PAGE_SIZE` is 16K on some arm64 configurations) or a
  dependency with per-architecture wheels.
- Windows gaps are tracked as burn-down lists, not scattered skips:
  `test/windows-collect-ignore.txt` and `test/windows-expected-failures.txt`. Anything
  NOT listed still fails the job. Delete a line when you fix its test.
- **Never `--deselect` a whole file for a missing host capability.** Guard the test that
  needs it — `skipif(not userns_available())` for the OS sandbox — so the report names the
  capability. A deselect is invisible in the output, takes the file's other tests with it,
  and never goes red when its reason expires: eleven such files kept 608 tests off every
  PR, of which 523 would have run on all three platforms, long after the cost that
  justified the exclusion had been fixed.
