"""What find_ui tells the agent to do next for a few question shapes.

A named app is the item to act on, not words to search for; a result with no
guide is answered in words without promising one; a preview feature is
answered in words; a miss on the control's own words is said plainly.
"""

from __future__ import annotations

import pytest

from kiro_crew import mcp_guide, ui_index


@pytest.mark.parametrize(
    "query,lang",
    [
        ("uninstall Command Bar", "en"),
        ("remove the Command Bar app", "en"),
        ("卸载 Command Bar", "zh-CN"),
        ("删除 Task Runner 应用", "zh-CN"),
    ],
)
def test_uninstalling_a_named_app_reaches_the_uninstall_guide_with_that_app_as_pick(
    query: str, lang: str
) -> None:
    d = ui_index.find_ui(query, lang)
    assert d["status"] == "ok" and not d["ambiguous"], d
    top = d["results"][0]
    # "Command Bar" is also what the top bar's search box is called: the
    # question names the app, so the search must not land on the search box.
    assert top["id"] == "apps.library.tile-uninstall"
    assert top["guide_ref"]["action_id"] == "ui.show"
    named = d["named_item"]
    assert named in query
    nxt = mcp_guide._find_ui_next(d)
    assert nxt and f'"pick": "{named}"' in nxt


def test_an_app_name_without_an_app_action_is_searched_as_words() -> None:
    d = ui_index.find_ui("open Command Bar", "en")
    assert "named_item" not in d
    assert d["results"][0]["id"] == "shell.search"


def test_a_result_without_any_guide_is_answered_in_words_and_promises_none() -> None:
    # The approval-mode picker is the agent's own ceiling: no guide is handed
    # out, and the agent must not say it can show it.
    d = ui_index.find_ui("approval mode", "en")
    assert d["results"][0]["id"] == "composer.approval-mode"
    assert "guide_ref" not in d["results"][0] and "find_ref" not in d["results"][0]
    nxt = mcp_guide._find_ui_next(d)
    assert nxt == mcp_guide._NO_GUIDE_NOTE
    assert "Never say you can show" in nxt


def test_a_preview_feature_is_answered_in_words_without_offering_to_show_it() -> None:
    for query, lang in (("webhooks", "en"), ("新建 webhook", "zh-CN")):
        nxt = mcp_guide._find_ui_next(ui_index.find_ui(query, lang))
        assert nxt == mcp_guide._PREVIEW_NOTE, query
        assert "Never say you can show" in nxt and "preview" in nxt


def test_a_miss_is_said_plainly_once_the_retry_misses_too() -> None:
    d = ui_index.find_ui("export a skill", "en")
    assert d["status"] == "no_match"
    nxt = mcp_guide._find_ui_next(d)
    assert nxt == mcp_guide._FIND_UI_RETRY_NOTE
    assert "say plainly that the dashboard has no such control" in nxt


def test_a_removal_is_described_only_from_the_pages_own_words() -> None:
    d = ui_index.find_ui("delete schedule", "en")
    nxt = mcp_guide._find_ui_next(d)
    assert nxt and "caution_text" in nxt and "cannot be undone" not in nxt
