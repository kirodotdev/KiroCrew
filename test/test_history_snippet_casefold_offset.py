"""A history snippet is cut around the match even when folding lengthens the text.

``tiered_first_hit`` finds a needle in ``text.casefold()``, and both snippet
builders cut their window from the original text at the position it returns.
``ß`` folds to ``ss`` (``İ`` and ``ﬃ`` lengthen too), so the position it returns
must index the original text, or every such character before the match moves
the window one place further along until it misses the match.
"""

from __future__ import annotations

from kiro_crew import history_search, mcp_core
from kiro_crew.history import ConversationLog


def test_the_position_indexes_the_original_text():
    text = "ß" * 200 + " needle"
    found = history_search.tiered_first_hit([text], [["needle"]])
    assert found is not None
    got, pos, needle = found
    assert got[pos : pos + len(needle)] == needle


def test_a_match_that_starts_inside_a_fold_points_at_that_character():
    text = "a" + "ß" * 50 + "x"
    found = history_search.tiered_first_hit([text], [["ssx"]])
    assert found is not None
    _, pos, _ = found
    assert text[pos:] == "ßx"


def test_text_that_folds_to_its_own_length_keeps_its_positions():
    text = "Needle and HAYSTACK"
    assert history_search.tiered_first_hit([text], [["haystack"]]) == (text, 11, "haystack")


def test_the_agent_snippet_still_shows_the_match():
    text = "Straße " * 150 + "needle here"
    snippet = mcp_core._extract_history_snippet([{"role": "user", "content": text}], "needle")
    assert "<<<needle>>>" in snippet


def test_the_dashboard_snippet_still_shows_the_match(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.append("k", "user", "Straße " * 150 + "needle here")
    assert "needle" in log._content_snippet("k", "needle")
