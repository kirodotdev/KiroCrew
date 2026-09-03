# Investigation: issue #7597 — `fleet_probe.py` at 14.8% failing the per-file coverage floor

## Summary

The disposition is **needs-investigation / likely duplicate of the CI coverage-arm
truncation class (#7516 / PR #7517)** — *not* an undertested file.

The remedy the gate prints ("add tests, do not extend the baseline") is the correct
remedy for a genuinely bare file, but it does not apply here: `fleet_probe.py`
already ships a dedicated ~20-method test class that arrived in the same commit that
added the script, and a sibling script loaded through the exact same mechanism in the
same test file is unbaselined and passes the gate. A 34/229 (14.8%) reading is the
fingerprint of the covering tests not landing in the coverage data that got combined
— the signature of dropped coverage data — rather than of a file with no tests.

**Do not** add `fleet_probe.py` to `.github/coverage-baselines/backend.txt`, and
**do not** write new tests for it. Both would be redundant work that records a false
rate for a file that is in fact covered.

> Empirical note: per-file coverage could not be measured in this sandbox. See
> [Measurement was blocked](#measurement-was-blocked) — the conclusion below is
> reached by static analysis, which is conclusive here because of the control case
> and the self-documented CI truncation mechanism.

## Evidence

### Evidence 1 — the tests the gate asks for already exist

`test/test_pipeline_conductor_agent.py` carries a dedicated `TestFleetProbe` class:

- `class TestFleetProbe:` — `test/test_pipeline_conductor_agent.py:179`
- `_mod()` loads the real script via
  `load_skill_script("fleet_probe", SKILL_DIR / "scripts" / "fleet_probe.py")` —
  `test/test_pipeline_conductor_agent.py:181`
- `SKILL_DIR` is defined at `test/test_pipeline_conductor_agent.py:26` and resolves to
  `src/kiro_crew/builtin_skills/pipeline-conductor`.

The class holds **20** `test_*` methods (between lines 179 and 453), most of which
drive the script end to end through `mod.main([...])`. (The issue thread's
auto-pipeline comment put this at "roughly 25"; the precise count is **20** — an
`awk` count of `def test_` between the `TestFleetProbe` and `TestCreditSpend` class
boundaries returns exactly 20. The "roughly 25" figure and this "20" describe the
same test class; this note records the concrete number so the reply is not read as
silently contradicting the auto-pipeline comment.) They exercise exactly the
surfaces the issue body nominates as "the cheap coverage":

- **Argument / config parsing (exit-2 paths):**
  - `test_malformed_config_exits_2`
  - `test_typed_misconfiguration_is_malformed_config`
  - `test_nonfinite_numeric_config_is_malformed`
- **Output shaping:**
  - `test_fired_lines_carry_metadata_never_transcript_text`
  - `test_protocol_tag_fires_and_working_stays_quiet`
- **Failure / containment paths:**
  - `test_missing_transcript_reports_gone`
  - `test_corrupt_handled_map_degrades_to_empty`
  - `test_symlink_out_of_the_store_reads_gone`
  - `test_traversal_session_keys_are_malformed_config`

`git log -- src/kiro_crew/builtin_skills/pipeline-conductor/scripts/fleet_probe.py`
shows a single commit, `c0dca4a8b`, and `TestFleetProbe` arrived in that same commit.
So the body's premise "No commit since has added tests for it" does not hold — the
tests landed with the script.

### Evidence 2 — the load mechanism does not defeat coverage attribution

The concern that a script loaded by `load_skill_script` might be invisible to
coverage is ruled out by both the mechanism and a direct control case.

Mechanism: `test/skill_script_helpers.py::load_skill_script` uses
`importlib.util.spec_from_file_location` and deliberately does **not** register the
module in `sys.modules` (documented in its docstring). Coverage.py attributes
execution **by file path**, and `setup.cfg` maps recorded paths onto the source tree:

```
[coverage:paths]        # setup.cfg:366
source =                # setup.cfg:367
    src/
    build/lib/*/site-packages/
```

Because attribution is by path and the script lives under `src/`, executing its lines
through the importlib loader is measured normally. Not registering in `sys.modules`
does not change which *file* the executed lines belong to.

Control case (this is the decisive point): `credit_spend.py` lives in the **same**
`src/kiro_crew/builtin_skills/pipeline-conductor/scripts/` directory, is loaded by the
**same** `load_skill_script` in the **same** test file:

- `class TestCreditSpend:` — `test/test_pipeline_conductor_agent.py:453`
- `_mod()` → `load_skill_script("credit_spend", SKILL_DIR / "scripts" / "credit_spend.py")`

`credit_spend.py` is **not** present in `.github/coverage-baselines/backend.txt`
(`grep -n credit_spend .github/coverage-baselines/backend.txt` → no match) and it
passes the gate. More broadly, ~14 skill scripts across the repo are loaded through
this same helper and none of them are baselined. If the loader defeated attribution,
every one of those files would sit near 0% and fail the floor — they do not.

Therefore a near-zero reading for `fleet_probe.py` *specifically* is an anomaly, not a
property of the load path.

### Evidence 3 — not reproducible; the 14.8% was one run, not a standing state

- `git log` for the script shows only `c0dca4a8b`; no test or baseline change since.
- `fleet_probe` remains absent from `.github/coverage-baselines/backend.txt`
  (`grep -n fleet_probe .github/coverage-baselines/backend.txt` → no match, exit 1).
- Overall backend coverage was reported healthy at **91.68%**, so this is a
  single-file blip against an otherwise-passing suite, not a broad regression.
- The auto-pipeline sampling recorded in the issue thread found **7 of 7**
  rebased-ahead open PRs (#7585, #7157, #6825, #7476, #7473, #4634, #7451) showing
  `Coverage Gate: success`, completed the same morning; #7585's pass landed one second
  before the issue was filed, with nothing changed in between.

Nothing in the tree was fixed between the failing observation and the passing samples.
A breach that is present on one run and absent on the next, with no intervening change
to the file, its tests, or the baseline, is the definition of a **non-deterministic
CI artifact**, not a code-coverage deficiency.

## The 34/229 signature

`fleet_probe.py` is 453 lines and defines its module top level with real executable
statements — imports plus module constants and a stack of `def` lines:

- `PROTO` — `fleet_probe.py:88`
- `DEFAULT_ERR_RES` — `fleet_probe.py:91`
- `DEFAULT_BANNED_RES` — `fleet_probe.py:102`
- `IDLE_TAG` — `fleet_probe.py:107`
- `_FIRING` — `fleet_probe.py:108`
- `_KEY_RE` — `fleet_probe.py:113`

34 statements out of 229 is very close to what executes from the **module top level
alone** at import time: the `import` statements, the module-level constant
assignments, and the `def`/`class` header lines (which run at import, defining the
functions), with every function *body* left unattributed. That is precisely the
pattern you get when a module is imported but the tests that call into its functions
never contributed to the combined coverage data — i.e. the covering tests ran on a
worker whose data was lost — not the pattern of a module that genuinely has no tests.

A file with truly no tests would still show its import-time lines; a file whose tests
all ran but whose data was dropped shows *exactly* its import-time lines and nothing
more. The 14.8% reading matches the second case, and Evidence 1 confirms the tests
exist and drive the function bodies.

## CI truncation mechanism

The truncation path is visible in `.github/workflows/ci.yml`:

- Backend coverage runs **only on the 3.12 shards** — the pytest invocation passes
  `--cov=kiro_crew --cov=sage_lib` on 3.12 and `--no-cov` otherwise
  (`ci.yml:692`); the 3.10 arm is trace-free by design (`ci.yml:655-657`).
- The suite is **sharded 4 ways** via pytest-split (`group: [1, 2, 3, 4]`,
  `ci.yml:595` in the `backend-test` matrix). Each 3.12 shard folds the parallel data
  files its xdist workers wrote into a single `.coverage` in the "Stage shard coverage
  data" step (`ci.yml:699-707`), renames it to `.coverage.<group>` (`ci.yml:707`), and
  the "Upload shard coverage" step (`ci.yml:709-716`) publishes it as artifact
  `coverage-shard-<group>`.
- That staging step is where data can go missing **with nothing going red**. It runs
  `coverage combine 2>/dev/null || true` (`ci.yml:705`), which discards both the stderr
  and the exit status of the merge, and the guard that follows asserts only that the
  file **exists** — `test -f .coverage || { echo "::error::..."; exit 1; }`
  (`ci.yml:706`). Nothing asserts that the merge consumed every worker's data file, so
  the guard separates "some data" from "no data" and never "all data" from "some data".
  A shard whose combine dropped one worker therefore stages a well-formed but
  **incomplete** `.coverage.<group>` and stays green.
- The **`coverage-combine`** job (`ci.yml:973`) downloads `pattern: coverage-shard-*`
  with `merge-multiple: true` (`ci.yml:1004-1005`) and then runs `coverage combine`
  / `coverage xml` / `coverage report` (`ci.yml:1012-1014`). It has **no
  all-shards-present assertion**, and no per-shard size or content expectation either,
  so it cannot notice a shard that is present but thin.

### Why the shard wall is not the mechanism here

`ci.yml:581-583` documents an adjacent failure, and it is tempting to reach for it:

> "The slowest 3.12 shard runs ~29 minutes, so a 30-minute cap kills the job at the
> coverage step with every test green and Coverage Gate then reds on the missing
> shard artifact."

(The cap is now 40 at `ci.yml:584`, per #7527 / #7552.) That path is real, but it
**cannot produce a per-file line-rate at all**, so it is not what happened here:

- A shard killed at the wall makes the `backend-test` matrix job **fail**.
  `fail-fast: false` (`ci.yml:588`) keeps its siblings running but does not change the
  matrix job's own result.
- `coverage-combine` declares `needs: [backend-test, changes]` (`ci.yml:975`) and an
  `if:` that does **not** use `always()` (`ci.yml:979`), so a failed `backend-test`
  leaves it **skipped** — no `coverage combine`, no `coverage.xml`.
- The per-file backend check is gated on `if: needs.coverage-combine.result ==
  'success'` (`ci.yml:1255`), and Coverage Gate fails closed on any other combine
  result (`ci.yml:1173-1175`).

A wall death therefore reds Coverage Gate at its "Require upstream coverage jobs to
have succeeded" step — `backend-test=failure` and `coverage-combine=skipped`, the job
itself reached only because it carries `if: always()` (`ci.yml:1117`) — and never
reports a number for any file. That the gate named `fleet_probe.py` at 14.8%, and that
a healthy 91.68% overall was measured on the same run, both require
`coverage-combine` to have **succeeded** — which means no shard died. The loss has to
be inside a shard that passed.

Chain it together: a 3.12 shard runs the `TestFleetProbe` methods green, its staging
step's `coverage combine` loses the worker data file those tests wrote, and
`|| true` plus the existence-only guard let the shard pass and upload a partial
`.coverage.<group>`. `coverage-combine` merges all four present artifacts and succeeds,
and the resulting `coverage.xml` shows `fleet_probe.py` with only its import-time lines
— 34/229. Coverage Gate then reds on that file even though its tests all ran green.
This is the **#7516 / PR #7517** coverage-arm truncation class.

Under this mechanism:
- **Adding tests would not help** — the tests already exist and already run; the
  problem is their data being dropped, not their absence.
- **Baselining would be actively wrong** — it would record a false 14.8% rate for a
  file that is in fact covered, permanently masking real regressions in it.

## Second suggestion — should Coverage Gate also run on pushes to `main`?

The issue frames the gate as "effectively PR-only". That framing is imprecise and
worth correcting before it becomes a fix aimed at the wrong line.

- By **trigger**, the workflow is *not* PR-only. `ci.yml:3-7` fires on both:

  ```yaml
  on:
    push:
      branches: [main]      # ci.yml:4-5
    pull_request:
      branches: [main]      # ci.yml:6-7
  ```

- The real gap is the **concurrency block** at `ci.yml:12-20`. `cancel-in-progress`
  is `${{ github.event_name == 'pull_request' }}` (`ci.yml:20`) — false on `main`, so
  an in-flight main run is allowed to finish. But the concurrency *group* still
  permits only one **pending** run, and GitHub evicts that pending run when a newer
  one queues (a documented GitHub behavior, noted in the comment itself). With a
  ~19-minute run and a ~1.4-minute median merge gap, `main` gets **periodic** coverage
  verdicts, not one per commit.

That periodic-verdict gap is the mechanism by which a below-floor file could merge in
a window with no verdict and only surface later on a rebasing PR. It is a real
CI-policy concern, but it is:

1. **Distinct from this diagnosis.** For #7597 specifically the file is covered; the
   14.8% is a truncation artifact, so tightening main verdicts would not have caught
   anything here (there was nothing to catch).
2. **A policy change, not a bug fix** — it belongs in its own dedicated issue (as the
   auto-pipeline routing also concluded), and the fix would target the *concurrency*
   semantics, not the `on:` triggers, which already include push to main.

## Measurement was blocked

Empirical per-file coverage measurement was **not possible in this sandbox**. The
network mode is `INTEGRATIONS_ONLY` (no external network, no PyPI), and both
`coverage` and `pytest_cov` are absent and cannot be installed:

```
$ python3 -c "import coverage"     -> ModuleNotFoundError: No module named 'coverage'
$ python3 -c "import pytest_cov"   -> ModuleNotFoundError: No module named 'pytest_cov'
```

The normal empirical check would be
`pytest --cov=kiro_crew test/test_pipeline_conductor_agent.py` followed by
`coverage xml` and inspecting the `fleet_probe.py` line-rate — which requires exactly
the packages that are unavailable here.

The disposition above is therefore reached by **static analysis**, which is
conclusive in this case: the control case (`credit_spend.py`, same directory, same
loader, same test file, unbaselined and passing) proves the load path does not defeat
attribution, and the swallowed-exit-status staging step plus its existence-only guard
(`ci.yml:705-706`) supply a concrete, already-filed mechanism (#7516 / #7517) for a
truncated reading that leaves every job green.

## Recommendation

1. **Route #7597 to `needs-investigation` as a likely duplicate of the CI
   coverage-arm truncation class (#7516 / PR #7517).** Do **not** add tests for
   `fleet_probe.py` (they already exist — `TestFleetProbe`,
   `test/test_pipeline_conductor_agent.py:179`), and do **not** extend
   `.github/coverage-baselines/backend.txt` (that would record a false rate for a
   covered file). The correct fix is in the **per-shard staging step**, not in
   `coverage-combine`: at `ci.yml:705`, distinguish the benign no-op exit from a real
   merge failure instead of discarding both (the step is documented as a no-op when
   pytest-cov already combined the shard, and in that case `coverage combine` exits
   non-zero with "No data to combine" — so the `|| true` cannot simply be dropped),
   and assert the merge consumed the number of worker data files the run produced,
   so a shard that loses a worker reds itself with a clear cause instead of uploading
   a plausible-looking partial artifact. An all-shards-present assertion in
   `coverage-combine` is worth having for its own sake but would **not** have caught
   this: all four shard artifacts were present, and the one that mattered was thin
   rather than absent.

2. **Track the sparse-main-coverage-verdict concern as its own CI-policy issue.** The
   workflow already triggers on push to `main` (`ci.yml:3-7`); the true gap is the
   concurrency-driven periodic verdicts on `main` (`ci.yml:12-20`). This is a policy
   change distinct from this diagnosis and worth a dedicated issue.

### Evidence index (file:line)

| Claim | Location |
| --- | --- |
| `TestFleetProbe` class | `test/test_pipeline_conductor_agent.py:179` |
| `_mod()` loads real script | `test/test_pipeline_conductor_agent.py:181` |
| `SKILL_DIR` definition | `test/test_pipeline_conductor_agent.py:26` |
| Control case `TestCreditSpend` | `test/test_pipeline_conductor_agent.py:453` |
| Loader: importlib, not in `sys.modules` | `test/skill_script_helpers.py` (`load_skill_script`) |
| Coverage attributes by path | `setup.cfg:366-367` (`[coverage:paths] source = src/`) |
| `fleet_probe` absent from baseline | `.github/coverage-baselines/backend.txt` (no match) |
| `credit_spend` absent from baseline | `.github/coverage-baselines/backend.txt` (no match) |
| Module top-level constants | `fleet_probe.py:88,91,102,107,108,113` |
| CI `on:` push + pull_request | `.github/workflows/ci.yml:3-7` |
| CI concurrency (periodic main verdicts) | `.github/workflows/ci.yml:12-20` |
| Shard-wall self-doc comment (ruled out as the mechanism) | `.github/workflows/ci.yml:581-583` |
| `fail-fast: false` (a dead shard still fails the matrix job) | `.github/workflows/ci.yml:588` |
| Coverage runs on 3.12 only | `.github/workflows/ci.yml:655-657,692` |
| Shard split `group: [1, 2, 3, 4]` | `.github/workflows/ci.yml:595` |
| Staging swallows the merge's exit status (`coverage combine 2>/dev/null \|\| true`) | `.github/workflows/ci.yml:705` |
| Per-shard empty-coverage guard (`test -f .coverage \|\| exit 1`) | `.github/workflows/ci.yml:706` |
| Stage shard coverage (guard + `mv .coverage`) | `.github/workflows/ci.yml:699-707` |
| Shard uploads `.coverage.<group>` | `.github/workflows/ci.yml:709-716` |
| `coverage-combine` job | `.github/workflows/ci.yml:973` |
| Combine `needs: [backend-test, changes]`, `if:` without `always()` | `.github/workflows/ci.yml:975,979` |
| Combine downloads `coverage-shard-*`, `merge-multiple`, no all-present assertion | `.github/workflows/ci.yml:1004-1014` |
| Coverage Gate runs regardless (`if: always()`) | `.github/workflows/ci.yml:1117` |
| Coverage Gate fails closed on a non-`success` combine | `.github/workflows/ci.yml:1173-1175` |
| Per-file backend check gated on combine `success` | `.github/workflows/ci.yml:1255` |
