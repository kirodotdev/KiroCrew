"""The pipeline-conductor scripts bound ``--repo`` at GitHub's own widths.

``claim_preflight.py`` and ``coverage_filter.py`` validate an ``owner/repo`` pair
before handing it to ``gh``. Each spells GitHub's limits itself because the
scripts run as bare files without ``kiro_crew`` on the import path, so these
tests also pin the spellings to the constants ``kiro_crew.github_runner``
publishes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

from kiro_crew import github_runner

SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "pipeline-conductor"
    / "scripts"
)


@pytest.fixture(params=["claim_preflight", "coverage_filter"])
def script(request):
    return load_skill_script(f"repo_bounds_{request.param}", SCRIPTS / f"{request.param}.py")


def test_limits_match_github_runner(script):
    assert script._GITHUB_MAX_OWNER_CHARS == github_runner.GITHUB_MAX_OWNER_CHARS
    assert script._GITHUB_MAX_REPO_CHARS == github_runner.GITHUB_MAX_REPO_CHARS


def test_owner_and_repo_at_the_limit_match(script):
    owner = "o" * script._GITHUB_MAX_OWNER_CHARS
    repo = "r" * script._GITHUB_MAX_REPO_CHARS
    assert script._REPO_RE.match(f"{owner}/{repo}") is not None


def test_owner_one_past_the_limit_is_refused(script):
    owner = "o" * (script._GITHUB_MAX_OWNER_CHARS + 1)
    assert script._REPO_RE.match(f"{owner}/repo") is None


def test_repo_one_past_the_limit_is_refused(script):
    repo = "r" * (script._GITHUB_MAX_REPO_CHARS + 1)
    assert script._REPO_RE.match(f"owner/{repo}") is None


def test_a_trailing_newline_cannot_escape_the_bound(script):
    assert script._REPO_RE.match("owner/repo\n") is None


def test_the_usual_slug_still_matches(script):
    assert script._REPO_RE.match("kirodotdev/KiroCrew") is not None
