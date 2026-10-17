"""The coverage omit globs must swallow phantom fixture paths, not real source.

Several tests in ``test_agent_home_isolation.py`` monkeypatch
``kiro_crew.agent.__file__`` to a fabricated checkout under pytest's tmp dir.
Nothing is ever written there, but the phantom path can land in the coverage
data, and then ``coverage xml`` / ``coverage report`` in the Coverage Combine
job aborts with ``No source for code: .../agent.py``. That step runs under
``bash -e``, so it fails the whole gate, Coverage Gate fails closed, and PR
Readiness is blocked on every open PR.

The omit globs in ``setup.cfg`` are the only thing standing between that
failure and a red CI, and they are easy to get subtly wrong in both
directions: too narrow and a new fixture directory name slips through, too
broad and real source is silently excluded so the coverage gate passes while
measuring nothing. Pin both directions here, against coverage.py's own
matcher rather than a hand-rolled fnmatch.
"""

from configparser import ConfigParser
from pathlib import Path

import pytest

SETUP_CFG = Path(__file__).resolve().parents[1] / "setup.cfg"

# Phantom paths: what the isolation fixtures fabricate. Both directory names
# really occur -- kirocrew-wt-example/ via _make_linked_worktree() and a plain
# KiroCrew/ clone dir in test_does_not_decline_from_an_ordinary_clone.
PHANTOM_PATHS = [
    pytest.param(
        "/tmp/pytest-of-runner/pytest-0/popen-gw0/"
        "test_does_not_decline_from_an_0/KiroCrew/src/kiro_crew/agent.py",
        id="ci-ordinary-clone",
    ),
    pytest.param(
        "/tmp/pytest-of-runner/pytest-0/popen-gw0/"
        "test_declines_0/kirocrew-wt-example/src/kiro_crew/agent.py",
        id="ci-linked-worktree",
    ),
    pytest.param(
        "/private/var/folders/ab/xyz/T/pytest-of-dev/pytest-3/"
        "test_does_not_decline_from_an_0/KiroCrew/src/kiro_crew/agent.py",
        id="macos-local-tmp",
    ),
]

# Real source that must stay measured. The CI path is the trap: the repo is
# checked out at /home/runner/work/KiroCrew/KiroCrew, so a "*/KiroCrew/*" glob
# would omit the entire package and turn the coverage gate green on nothing.
REAL_SOURCE_PATHS = [
    pytest.param(
        "/home/runner/work/KiroCrew/KiroCrew/src/kiro_crew/agent.py",
        id="ci-checkout",
    ),
    pytest.param("/home/dev/src/KiroCrew/src/kiro_crew/agent.py", id="local-checkout"),
]

OMIT_SECTIONS = ["coverage:run", "coverage:report"]


def _omit_patterns(section: str) -> list[str]:
    parser = ConfigParser()
    assert SETUP_CFG.is_file(), f"expected the coverage config at {SETUP_CFG}"
    parser.read(SETUP_CFG)
    raw = parser.get(section, "omit")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _matcher(section: str):
    globs = pytest.importorskip("coverage.files")
    return globs.GlobMatcher(_omit_patterns(section), "omit")


def test_run_and_report_omit_lists_stay_in_sync() -> None:
    """``[coverage:run] omit`` governs collection only; report needs its own.

    Coverage Combine reads shards produced by the test job, so a pattern that
    exists in one list and not the other still lets the phantom path abort the
    report.
    """
    run_omit, report_omit = (_omit_patterns(s) for s in OMIT_SECTIONS)

    assert run_omit == report_omit


@pytest.mark.parametrize("section", OMIT_SECTIONS)
@pytest.mark.parametrize("phantom", PHANTOM_PATHS)
def test_phantom_fixture_paths_are_omitted(section: str, phantom: str) -> None:
    assert _matcher(section).match(phantom), (
        f"{phantom} would reach `coverage report` and abort the Coverage "
        f"Combine job; [{section}] omit does not cover it"
    )


@pytest.mark.parametrize("section", OMIT_SECTIONS)
@pytest.mark.parametrize("real", REAL_SOURCE_PATHS)
def test_real_source_is_never_omitted(section: str, real: str) -> None:
    assert not _matcher(section).match(real), (
        f"[{section}] omit excludes real source at {real}; the coverage gate "
        f"would pass while measuring nothing"
    )
