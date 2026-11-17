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


_PATTERNS = ["kirocrew-work/*", "**", "*", "a**b*c", "kirocrew-*/session_?end", "x/y", "**/z", ""]
_RESOURCES = [
    "kirocrew-work/work_report",
    "kirocrew-dashboard/session_send",
    "ab/bc",
    "x/y",
    "a/b/z",
    "",
]


def test_the_memoized_answer_is_the_matchers_own_for_every_pair():
    """The cache sits on a pure function of two strings: whatever is cached is what
    the matcher answers, in either call order, so a hit can never change a result."""
    guard = importlib.import_module("kiro_crew.acp.batch_guard")
    guard._read_glob_matches.cache_clear()
    for _ in range(2):  # the second pass is answered from the cache
        for pattern in _PATTERNS:
            for resource in _RESOURCES:
                expected = guard._glob_match_steps(pattern, resource)[0]
                assert guard._glob_matches(pattern, resource, unread=not expected) is expected
    info = guard._read_glob_matches.cache_info()
    assert info.hits == info.misses == len(_PATTERNS) * len(_RESOURCES)
    assert info.maxsize is not None, "the cache is bounded"


def test_an_unread_pattern_still_answers_the_callers_side_and_is_not_cached():
    guard = importlib.import_module("kiro_crew.acp.batch_guard")
    guard._read_glob_matches.cache_clear()
    assert guard._glob_matches("kirocrew-[w]ork/*", "kirocrew-work/x", unread=True) is True
    assert guard._glob_matches("kirocrew-[w]ork/*", "kirocrew-work/x", unread=False) is False
    assert guard._read_glob_matches.cache_info().currsize == 0


def test_the_cached_matcher_reads_no_module_state():
    """Purity, pinned on the code itself: the memoized function and the matcher it
    calls name no module-level object but the matcher, so no setting, file, policy
    or clock can reach an answer the cache keeps."""
    guard = importlib.import_module("kiro_crew.acp.batch_guard")

    def names(code):
        out = set(code.co_names)
        for const in code.co_consts:
            if hasattr(const, "co_names"):
                out |= names(const)
        return out

    used = names(guard._read_glob_matches.__wrapped__.__code__) | names(
        guard._glob_match_steps.__code__
    )
    assert used & set(vars(guard)) == {"_glob_match_steps"}


def test_an_oversized_name_is_matched_but_never_kept():
    """A tool name is a server's own text: one longer than the memo's bound is
    matched as before and not retained by the cache."""
    guard = importlib.import_module("kiro_crew.acp.batch_guard")
    guard._read_glob_matches.cache_clear()
    long_name = "s/" + "t" * (guard._MEMO_MAX_CHARS + 1)
    assert guard._glob_matches("s/*", long_name, unread=False) is True
    assert (
        guard._glob_matches("s/" + "*" * (guard._MEMO_MAX_CHARS + 1), "s/t", unread=False) is True
    )
    assert guard._read_glob_matches.cache_info().currsize == 0
