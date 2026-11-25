"""The library preview stays linear on a long run of blank lines.

The list-marker and quote patterns of the markdown cleanup started a line with
``\\s*``, which also matches a newline. At every blank line of a run the pattern
then re-scanned the rest of the run before it failed, so one artifact holding tens
of thousands of blank lines stalled ``GET /api/artifacts?snippet=1`` for seconds.
The leading class is now horizontal whitespace, so a line start never reaches into
the next line. The test pins that mechanism instead of a timing: a blank line before
a list item or a quote keeps its line break.
"""

from __future__ import annotations

import pytest
from artifacts_handlers_helpers import (  # noqa: F401 -- fixtures are requested by name
    _json_body,
    _request,
    isolated_store_fixture,
    patch_restricted_fixture,
)

from kiro_crew.dashboard.handlers.artifacts import api_artifacts_list
from kiro_crew.dashboard.handlers.artifacts_parts import library_reads as lr

_BLANK_LINES = 40000


def test_a_line_start_does_not_consume_the_blank_lines_before_it():
    cleaned = lr._clean_markdown("a\n\n\n- item\n\n> quote\n")
    assert cleaned == "a\n\n\nitem\n\nquote\n"


def test_indented_markers_and_quotes_are_still_stripped():
    assert lr._clean_markdown("   - one\n\t* two\n  12. three\n  > four\n") == (
        "one\ntwo\nthree\nfour\n"
    )


def test_a_marker_in_the_middle_of_a_line_is_kept():
    assert lr._clean_markdown("a - b > c\n") == "a - b > c\n"


def test_a_long_blank_run_still_reads_as_one_line_of_prose():
    text = "# notes\n" + "\n" * _BLANK_LINES + "- end of notes\n"
    assert lr._strip_content(text) == "notes end of notes"


@pytest.mark.asyncio
async def test_the_list_snippet_over_a_long_blank_run(isolated_store, patch_restricted) -> None:
    body = "# notes\n" + "\n" * _BLANK_LINES + "- end of notes\n"
    isolated_store.create(name="notes", content=body)
    resp = await api_artifacts_list(_request(query={"snippet": "1"}))
    assert _json_body(resp)["artifacts"][0]["snippet"] == "notes end of notes"
