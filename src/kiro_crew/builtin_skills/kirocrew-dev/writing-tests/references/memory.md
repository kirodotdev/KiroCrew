# Rule 6 — MEMORY is the other budget, and collection is most of it

Moved verbatim from the writing-tests `SKILL.md` core.

A worker costs ~2.0 GiB (measured median at `-n 12`; worst observed 2.8 GiB), and
~1,499 MiB of that is paid before your test runs: every xdist worker independently
collects every item in both testpaths (106,491), and 99% of that footprint is private,
so more workers never amortize it. This is why `-n auto` is bounded by available memory
— on an 8–16 GiB laptop the full suite otherwise swaps the machine.

Both halves of that model doubled between audits (from ~57k items / ~750 MiB), so
**re-measure rather than trusting the numbers above** once the suite grows by half
again: `--collect-only -n0` reproduces a worker's collection peak. The reservation in
`xdist_budget.py` is sized on them, and an under-sized reservation is how a laptop
starts swapping. At 283,605 items the collection peak was 5,295 MiB, and 2,971 MiB of
that was pytest's per-item fixture bookkeeping; the rootdir conftest's
`compact_collected_item` shares it, for a 2,957 MiB peak
(`testing-conventions.md` § Running on a machine with little RAM).

The consequence for how you write a test (each with a wrong and a right example and
its measured saving in `testing-conventions.md` § Writing a test that is cheap on
memory):

- **A new autouse fixture is paid by every item.** Each one adds a slot to every test
  function's fixture closure and a setup and teardown to every test, ~280k times. Add
  the guard to an existing autouse fixture of the same scope instead of a new name. A
  per-test reset or pin in `test/conftest_resets.py` is a `@per_test_guard` function
  (`test/conftest_guards.py`), which five existing autouse fixtures already run.
- **A module-level memo of a whole-tree read lives for the worker.** An
  `lru_cache`d `(path, text)` corpus in a test module is never released: pytest keeps
  the module object for the session. Put the module in a `tree_scan_*` xdist group (the
  conftest clears its module memos when the group ends), stream the read, or clear the
  memo from a module-scoped fixture.
- **A closure over a test's state in a module global outlives the test.** A setter
  such as `set_share_tree_reader(lambda key: ...)` keeps the state, and every mock call
  it recorded, until something overwrites the global. Restore the global with
  `monkeypatch`, or have the floor restore it when production wires it once at boot.
- **Collection is budgeted per item**
  (`test_worker_memory_retention.py::TestCollectionCostPerItem`): a change that makes
  every collected item heavier fails there, not in a swapping worker.
- **An allocation at module scope is multiplied by every worker, and outlives the
  test.** A big literal inside `@pytest.mark.parametrize` is the worst shape: it is
  built while the module is IMPORTED and the mark keeps it alive on the function
  object for the whole session, so one test's payload is charged to all of them. Two
  such literals — a 64 MiB frame and a 40 MB string — cost 102 MiB per worker until
  they were moved into the test bodies behind a sentinel.
- **A transient peak counts too**, because a worker's high-water mark is what the
  budget must reserve. Building an image as a list of per-pixel tuples cost ~390 MiB
  for a 17 MB PNG; one `frombytes` over a bytes buffer was 10x smaller and
  byte-equivalent.
- **Derive a size from the production constant** rather than restating it. A literal
  one past a production size ceiling, restated beside that constant, hides both the
  coupling and the cost, and goes stale silently.
- Suspect a **module-scope literal** whenever a file's collection RSS is large; measure
  it with `pytest <file> --collect-only` and `/proc/self/status`'s `VmHWM`.
