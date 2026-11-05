"""Workflow library search: short words must not prefix-match every query word."""

from __future__ import annotations

from kiro_crew.workflows.library import WorkflowDefinitionLibrary

# A script whose only short tokens are one- and two-letter identifiers. None of
# its words share a meaningful prefix with the query used below.
SHORT_TOKEN_SOURCE = (
    'META = {"name": "unrelated", "description": "Unrelated"}\n'
    "async def workflow(ctx):\n"
    "    r = await ctx.agent('x')\n"
    "    re = r\n"
    "    return re\n"
)

RELEASE_SOURCE = (
    'META = {"name": "release-notes", "description": "Summarize changes"}\n'
    "async def workflow(ctx):\n"
    "    return await ctx.agent('summarize the release')\n"
)


def test_a_one_letter_script_word_does_not_match_an_unrelated_intent(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    library.create(source=SHORT_TOKEN_SOURCE, name="Inventory Count", description="Tally stock")

    assert library.search("rotate replica credentials") == []


def test_a_short_query_word_does_not_prefix_match_longer_saved_words(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    library.create(source=RELEASE_SOURCE, name="Release Notes", description="Summarize changes")

    assert library.search("re") == []


def test_short_words_still_match_exactly(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    created = library.create(source=SHORT_TOKEN_SOURCE, name="Inventory Count", description="")

    assert [item["id"] for item in library.search("r")] == [created["id"]]


def test_prefix_matches_of_real_words_still_rank_first(tmp_path) -> None:
    library = WorkflowDefinitionLibrary(tmp_path)
    release = library.create(
        source=RELEASE_SOURCE, name="Release Notes", description="Summarize changes"
    )
    library.create(source=SHORT_TOKEN_SOURCE, name="Inventory Count", description="Tally stock")

    matches = library.search("releases summarize")

    assert [item["id"] for item in matches] == [release["id"]]
