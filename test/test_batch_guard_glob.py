"""The batch guard's KAS resource glob matcher reads globs in bounded time.

``**`` crosses path segments, ``*`` and ``?`` stay inside one, every other
character is literal. The guard runs this matcher while it projects every new
session's agent spec, so a pattern with a long run of ``*`` must not backtrack
exponentially: that would hold the GIL and freeze the gateway. The bound is
checked by counting the matcher's steps, not by timing it.
"""

from __future__ import annotations

import importlib

import pytest


def _glob_matches(pattern: str, resource: str) -> bool:
    guard = importlib.import_module("kiro_crew.acp.batch_guard")
    return guard._glob_matches(pattern, resource, unread=False)


@pytest.mark.parametrize(
    ("pattern", "resource", "expected"),
    [
        ("kirocrew-work/work_report", "kirocrew-work/work_report", True),
        ("kirocrew-work/work_report", "kirocrew-work/work_reports", False),
        ("kirocrew-work/*", "kirocrew-work/work_report", True),
        ("kirocrew-work/*", "kirocrew-work/a/b", False),
        ("*", "kirocrew-work/work_report", False),
        ("**", "kirocrew-work/work_report", True),
        ("**", "", True),
        ("*", "", True),
        ("***", "a/b", True),
        ("kirocrew-work/work_?eport", "kirocrew-work/work_report", True),
        ("kirocrew-work/work_?eport", "kirocrew-work/work_/eport", False),
        ("kirocrew-*/session_*", "kirocrew-dashboard/session_send", True),
        ("**/session_send", "kirocrew-dashboard/session_send", True),
        # A segment star must hand the slash back to an earlier ** to match.
        ("a**b*c", "ab/bc", True),
        ("a.b/c", "aXb/c", False),
    ],
)
def test_the_glob_reads_star_globstar_and_question_mark(pattern, resource, expected):
    assert _glob_matches(pattern, resource) is expected


def test_a_long_run_of_stars_is_matched_in_bounded_work():
    guard = importlib.import_module("kiro_crew.acp.batch_guard")
    pattern = "a" + "*" * 30 + "b"
    resource = "a" * 5000
    matched, steps = guard._glob_match_steps(pattern, resource)
    assert matched is False
    assert steps <= (len(pattern) + 1) * (len(resource) + 1)
    # The matcher is not a regex translation, which backtracks on star runs.
    assert not hasattr(guard, "re")
