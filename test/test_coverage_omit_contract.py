"""Contract: every test file CI refuses to run is excluded from coverage.

``BACKEND_DESELECTS`` in ``.github/workflows/ci.yml`` deselects a fixed set of
test files on EVERY backend pytest invocation, because the GitHub Actions
runners lack what those tests need (Linux-namespace sandbox, a real ``git``/
``gh``, POSIX rmtree/path assumptions). Those files are therefore never
executed in CI.

Coverage must not charge their statements to the denominator: an unreachable
file is not an uncovered file, and counting it silently understates the real
line-rate (it hid ~3.9k permanently-unreachable statements, ~1.5pp). So
``setup.cfg`` omits them from BOTH ``[coverage:run]`` (collection time, in the
test job) and ``[coverage:report]`` (report time, in the Coverage Combine job,
a separate process that re-reads shard data).

Three lists therefore have to agree, and they live in two different files. The
CI deselect list has already drifted once against another copy of itself, so
this test enforces the invariant instead of trusting a comment to be read.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SETUP_CFG = REPO_ROOT / "setup.cfg"


def _ci_deselected_paths() -> list[str]:
    """Repo-relative test paths from ci.yml's BACKEND_DESELECTS block.

    Parsed with a regex rather than a YAML loader so the test does not depend on
    PyYAML being installed, and so it reads the literal text a maintainer edits.
    """
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"^  BACKEND_DESELECTS: >-\n((?:    .*\n)+)", text, re.MULTILINE)
    assert match, "BACKEND_DESELECTS block not found in ci.yml -- did its shape change?"
    paths = re.findall(r"--deselect=(\S+)", match.group(1))
    assert paths, "BACKEND_DESELECTS matched but contained no --deselect= entries"
    return paths


def _cfg_omit(section: str) -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(SETUP_CFG, encoding="utf-8")
    raw = parser.get(section, "omit")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _matches(pattern: str, path: str) -> bool:
    """True if a coverage omit glob would exclude ``path``.

    coverage.py matches omit patterns against the measured filename, which is
    absolute at runtime, so the committed patterns are written with a leading
    ``*/``. Compare on suffix semantics: translate the glob and allow it to
    match the tail of the repo-relative path.
    """
    body = pattern[2:] if pattern.startswith("*/") else pattern
    regex = "".join(".*" if part == "*" else re.escape(part) for part in re.split(r"(\*)", body))
    return re.search(rf"(^|/){regex}$", path) is not None


def test_ci_deselect_block_is_parseable() -> None:
    """Guard the regex itself: a reshaped ci.yml must fail loudly, not silently."""
    paths = _ci_deselected_paths()
    assert len(paths) >= 11, f"expected the full deselect list, parsed only {len(paths)}"
    assert all(p.endswith(".py") for p in paths), paths


@pytest.mark.parametrize("section", ["coverage:run", "coverage:report"])
def test_every_deselected_test_is_omitted_from_coverage(section: str) -> None:
    """A file CI never executes must not be measured as coverable code."""
    omit = _cfg_omit(section)
    missing = [p for p in _ci_deselected_paths() if not any(_matches(pat, p) for pat in omit)]
    assert not missing, (
        f"[{section}] omit does not cover these CI-deselected test files: {missing}. "
        "They are never executed, so measuring them understates the real line-rate. "
        "Add a matching */... pattern to setup.cfg."
    )


def test_run_and_report_omit_lists_agree() -> None:
    """Collection-time and report-time omit must stay identical.

    They govern different processes (the test job vs Coverage Combine); if only
    one carries an entry, the gate silently measures a different denominator
    than the shards were collected under.
    """
    assert sorted(_cfg_omit("coverage:run")) == sorted(_cfg_omit("coverage:report"))


def test_omit_patterns_do_not_swallow_product_code() -> None:
    """Fail-safe: no pattern may match a non-test source file.

    A pattern like ``*/kiro_crew/*`` would omit all real source and turn the
    coverage gate green by measuring almost nothing. Assert every committed
    pattern is either a pytest-tmp fixture escape or targets a test file.
    """
    fixture_escapes = {"*/pytest-of-*/*", "*/kirocrew-wt-example/*"}
    for pattern in _cfg_omit("coverage:run"):
        if pattern in fixture_escapes:
            continue
        tail = pattern.rsplit("/", 1)[-1]
        assert tail.startswith("test_") and tail.endswith(".py"), (
            f"omit pattern {pattern!r} is neither a pytest-tmp escape nor a specific "
            "test file -- a broad pattern here can silently stop measuring product code."
        )
