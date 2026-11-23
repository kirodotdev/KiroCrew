# Rule 5 — Keep the parallel suite fast

Moved verbatim from the writing-tests `SKILL.md` core.

At 100k+ tests, **per-test setup cost dominates any single slow test** — an autouse
fixture is paid once per test, over 100,000 times. Profile, never guess; compare candidates back to back on
the same host (run the change, then the base in `git worktree add --detach <dir> <base>`;
never `git stash`: every worktree shares one stash list), because a loaded host makes an
absolute number meaningless.

The recurring wins, in order of leverage:

1. **An autouse fixture that costs more than it protects** — one requesting a
   `tmp_path` it never uses; a repeated `tmp_path_factory.mktemp` (it scans the whole
   basetemp to pick its suffix, so it slows as siblings accumulate); an unconditional
   `mkdir` where the consumer only needs a *path*. Measure the whole chain against a
   file of trivial `assert True` tests.
2. **A production timeout or poll the test never asserts on** — `monkeypatch` the
   interval to `0`; the branch still executes, only the waiting goes.
3. **An expensive immutable thing built per test** — a real `git` repo costs ~1–1.6s in
   subprocesses. Build it once and copy it per test with `copy_repo` from
   `test/git_repo_templates.py`, which needs no git after the copy; copy *from* the
   template rather than yielding it, so nothing one test does can reach another's.
4. **A module-cached tree scan paid once per WORKER** — an `lru_cache`d `rglob` +
   `ast.parse` of `src/` (~30 s) is computed again on every xdist worker the module's
   tests land on; five full runs measured eighteen ratchet modules each re-scanning on
   3–5 workers, ~22 CPU-minutes per run. Add
   `pytestmark = pytest.mark.xdist_group(name="tree_scan_<module>")` — one group per
   file, never one shared group — and the scan runs once per run. The group also
   carries the tree-scan timeout: `test/conftest.py` gives every test in a `tree_scan`
   group with no timeout of its own 300 s instead of the suite's 120 s.

**After any speedup, mutate the production code the test covers and confirm the test
still FAILS.** A test made faster by checking less is a regression. Restore from a copy
of the file you mutated, not from git — `git checkout --` discards unrelated uncommitted
work — and sequence with `;`, not `&&`, or the restore only runs when the mutation
did *not* work.
