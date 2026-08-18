"""Contract: CI runs every test file, and coverage measures every test file.

This used to enforce the opposite direction. ``BACKEND_DESELECTS`` in
``.github/workflows/ci.yml`` deselected eleven test files from EVERY backend pytest
invocation, because the GitHub Actions runners deny ``unshare(CLONE_NEWNS)`` and the
suites drive a real ``git``/``gh``/``pytest`` through ``sandboxed_spawn_argv``. Files CI
never executes must not be charged to the coverage denominator -- an unreachable file is
not an uncovered file -- so ``setup.cfg`` omitted the same eleven from both
``[coverage:run]`` and ``[coverage:report]``, and this test kept the three lists in
agreement.

The deselects are gone: every sandbox-dependent test in those files carries its own
``skipif(not userns_available())``, so it skips on a runner with a named reason and runs
for real on a capable host. All eleven files are collected on every platform now.

The exemption did not disappear, it narrowed -- and its reason changed. A test that SKIPS
never executes its body either, so a file whose sandbox-dependent tests skip on a runner
still charges the denominator for lines the measuring host cannot reach:
``test_ledger_sync_git.py`` measured 16% (65/405) on CI for exactly that reason, and the
per-file floor caught it. So the six files carrying such a guard stay omitted, and the
five without one are measured normally.

That gives a rule worth enforcing in both directions:

* an omitted test file must carry a capability guard CI cannot satisfy -- so a stale entry
  cannot outlive its reason, which is how the retired eleven-file list would have rotted;
* a ``--deselect`` that comes back must arrive WITH its coverage omit, because a deselected
  file is unreachable for a second reason. A deselect is otherwise easy to add and
  invisible afterwards: absent from the run output, taking the file's other tests with it,
  never going red when its reason expires.

The two lists must still agree, and no pattern may swallow product code.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SETUP_CFG = REPO_ROOT / "setup.cfg"

#: Patterns that are legitimately omitted and are not test files at all. Both are pytest
#: temporary-directory escapes: nothing real is ever measured from one, and a shard that
#: recorded such a phantom path would otherwise trip ``coverage xml`` with "No source for
#: code".
_FIXTURE_ESCAPES = {"*/pytest-of-*/*", "*/kirocrew-wt-example/*"}

#: The predicate a test file must carry to earn an omit: the guard that makes its tests
#: skip where the capability is absent. Matched as source text so the check needs no
#: import of the suite under test.
_CAPABILITY_GUARD = "userns_available"


def _ci_deselected_paths() -> list[tuple[str, str]]:
    """``(workflow name, test path)`` for every ``--deselect`` in EVERY workflow.

    Every workflow, not just ``ci.yml``: two lived in ``test-durations.yml`` for the same
    two sandbox suites, where the sharded matrix could not see them and neither could a
    reader of the shard job. That one did quieter damage than a plain exclusion -- the
    durations file it produces then carried no entry for those files, so ``pytest-split``
    gave them the average and balanced the shards on a guess.

    Also scanned per line rather than per env block, so a deselect inlined into a run
    step or parked in a new matrix job cannot slip past.

    Read as literal text rather than through a YAML loader so the test needs no PyYAML
    and sees exactly what a maintainer edits.
    """
    found: list[tuple[str, str]] = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        # Skip comment lines: several workflows document the retired mechanism in prose,
        # and a comment describing a deselect is not one.
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        found += [(workflow.name, path) for path in re.findall(r"--deselect=(\S+)", code)]
    return found


def _cfg_omit(section: str) -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(SETUP_CFG, encoding="utf-8")
    raw = parser.get(section, "omit")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _matches(pattern: str, path: str) -> bool:
    """True if a coverage omit glob would exclude ``path``.

    coverage.py matches omit patterns against the measured filename, which is absolute
    at runtime, so the committed patterns are written with a leading ``*/``. Compare on
    suffix semantics: translate the glob and allow it to match the tail of the
    repo-relative path.
    """
    body = pattern[2:] if pattern.startswith("*/") else pattern
    regex = "".join(".*" if part == "*" else re.escape(part) for part in re.split(r"(\*)", body))
    return re.search(rf"(^|/){regex}$", path) is not None


def test_no_workflow_deselects_a_test_file() -> None:
    """The eleven suites run again; keep it that way, in every workflow.

    A capability a runner lacks belongs on the test that needs it, as a skip with a
    named reason, not on the file as a deselect. `userns_available()` is the predicate
    those suites already use.
    """
    deselected = [f"{workflow}: {path}" for workflow, path in _ci_deselected_paths()]
    assert not deselected, (
        f"a workflow deselects {deselected}. A whole-file deselect hides the file's other "
        "tests and expires silently -- guard the specific test with "
        "skipif(not userns_available()) instead. If a deselect is genuinely right, add "
        "a matching */... pattern to BOTH omit lists in setup.cfg so coverage does not "
        "charge an unreachable file to the denominator."
    )


@pytest.mark.parametrize("section", ["coverage:run", "coverage:report"])
def test_any_deselected_test_stays_out_of_coverage(section: str) -> None:
    """The other half of the ratchet, live the moment a deselect returns.

    Vacuous while nothing is deselected, which is the intended steady state -- it exists
    so that re-adding one cannot silently leave coverage measuring a file the suite
    cannot run.
    """
    omit = _cfg_omit(section)
    missing = [
        path
        for _workflow, path in _ci_deselected_paths()
        if not any(_matches(pat, path) for pat in omit)
    ]
    assert not missing, (
        f"[{section}] omit does not cover these CI-deselected test files: {missing}. "
        "They are never executed, so measuring them understates the real line-rate. "
        "Add a matching */... pattern to setup.cfg."
    )


def test_run_and_report_omit_lists_agree() -> None:
    """Collection-time and report-time omit must stay identical.

    They govern different processes (the test job vs Coverage Combine); if only one
    carries an entry, the gate silently measures a different denominator than the shards
    were collected under.
    """
    assert sorted(_cfg_omit("coverage:run")) == sorted(_cfg_omit("coverage:report"))


def _repo_files_matching(pattern: str) -> list[Path]:
    """Every tracked test file the omit *pattern* would exclude."""
    tail = pattern[2:] if pattern.startswith("*/") else pattern
    return [
        path
        for path in REPO_ROOT.rglob(tail.rsplit("/", 1)[-1])
        if _matches(pattern, path.relative_to(REPO_ROOT).as_posix())
    ]


def test_every_omitted_test_file_carries_a_capability_guard() -> None:
    """An omitted test file must be unreachable for a REASON that is in the file.

    Without this, an entry outlives its reason silently -- which is exactly how the
    retired eleven-file list rotted: it was justified by a `--deselect` and a "~6 minutes
    against a real git" cost, both of which had been gone for some time while the omit
    stayed. A deselected file is exempt for a second, separate reason and is allowed here
    too, but the deselect itself has to survive `test_ci_deselects_no_test_file`.
    """
    deselected = [path for _workflow, path in _ci_deselected_paths()]
    unjustified: list[str] = []
    for pattern in _cfg_omit("coverage:run"):
        if pattern in _FIXTURE_ESCAPES:
            continue
        if any(_matches(pattern, p) for p in deselected):
            continue
        matched = _repo_files_matching(pattern)
        if not matched:
            unjustified.append(f"{pattern} (matches no file in the repo)")
        elif not any(
            _CAPABILITY_GUARD in path.read_text(encoding="utf-8") for path in matched
        ):
            unjustified.append(f"{pattern} (no {_CAPABILITY_GUARD} guard)")
    assert not unjustified, (
        f"these omit patterns exempt a test file with nothing to justify it: {unjustified}. "
        "Coverage measures what the measuring host can run: a file whose tests execute "
        "there must be measured. Drop the pattern, or make the reason visible in the file "
        f"as a {_CAPABILITY_GUARD} guard."
    )


def test_omit_patterns_do_not_swallow_product_code() -> None:
    """Fail-safe: no pattern may match a non-test source file.

    A pattern like ``*/kiro_crew/*`` would omit all real source and turn the coverage
    gate green by measuring almost nothing. Assert every committed pattern is either a
    pytest-tmp fixture escape or targets a test file.
    """
    for pattern in _cfg_omit("coverage:run"):
        if pattern in _FIXTURE_ESCAPES:
            continue
        tail = pattern.rsplit("/", 1)[-1]
        assert tail.startswith("test_") and tail.endswith(".py"), (
            f"omit pattern {pattern!r} is neither a pytest-tmp escape nor a specific "
            "test file -- a broad pattern here can silently stop measuring product code."
        )
