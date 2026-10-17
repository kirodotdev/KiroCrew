"""``find_ui`` goldens for the ``sessions`` area: the Sessions sidebar's menus and rows.

Each location is reached by an English and a zh-CN newcomer phrasing, with its
exact path and requirements; unrelated phrasings stay ``no_match``. Reads the
committed (packaged) index.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiro_crew import ui_index


@pytest.fixture(autouse=True)
def _fresh_cache(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ui_index._cache.update(key=None, index=None)
    # The committed tiers only, whatever dashboard build this checkout has:
    # the build-time auto tier is pinned in test_find_ui_auto.py.
    _floor_monkeypatch.setattr(ui_index, "AUTO_INDEX_PATH", tmp_path / "no-auto-tier.json")


def _top(query: str, lang: str) -> dict[str, Any]:
    d = ui_index.find_ui(query, lang)
    assert d["status"] == "ok", (query, d)
    return d["results"][0]


def _path(placement: dict[str, Any]) -> list[str]:
    return [seg["label"] for seg in placement["path"]]


def _conditions(placement: dict[str, Any]) -> list[str]:
    return [r["id"] for r in placement["requires"] if r["kind"] == "condition"]


def _shown_by(placement: dict[str, Any]) -> tuple[str, str]:
    r = next(r for r in placement["requires"] if r["kind"] == "shown_by")
    return r["label"], r["when"]


def _viewport(placement: dict[str, Any]) -> str:
    return next(r["value"] for r in placement["requires"] if r["kind"] == "viewport")


_DESKTOP_STEP = ("Show sessions sidebar", "sessions_sidebar_collapsed")
_MOBILE_STEP = ("Toggle sessions", "sessions_drawer_closed")

# id -> (EN phrasing, zh-CN phrasing, EN path, zh-CN path, conditions, both viewports?)
_CASES: dict[str, tuple[str, str, list[str], list[str], list[str], bool]] = {
    "sessions.list-menu": (
        "session options",
        "会话菜单",
        ["Sessions", "More options"],
        ["会话", "更多选项"],
        [],
        True,
    ),
    "sessions.list-menu.dashboards": (
        "all session dashboards",
        "全部仪表板",
        ["Sessions", "More options", "All Dashboards"],
        ["会话", "更多选项", "全部仪表板"],
        [],
        True,
    ),
    "sessions.list-menu.view": (
        "board view",
        "看板视图",
        ["Sessions", "More options", "Switch to board view"],
        ["会话", "更多选项", "切换到看板视图"],
        [],
        True,
    ),
    "sessions.list-menu.add-lanes": (
        "add status columns",
        "添加状态列",
        ["Sessions", "More options", "Add automatic columns"],
        ["会话", "更多选项", "添加自动栏"],
        ["sessions_board_view", "board_missing_state_lanes"],
        True,
    ),
    "sessions.list-menu.clean-up": (
        "how do I clean up old chats",
        "清理会话",
        ["Sessions", "More options", "Clean up sessions"],
        ["会话", "更多选项", "清理会话"],
        [],
        True,
    ),
    "sessions.list-menu.switch-model": (
        "switch every session to one model",
        "所有会话换模型",
        ["Sessions", "More options", "Switch all to model…"],
        ["会话", "更多选项", "全部切换到模型…"],
        [],
        True,
    ),
    "sessions.list-menu.manage-tags": (
        "where are tags",
        "管理标签",
        ["Sessions", "More options", "Manage tags…"],
        ["会话", "更多选项", "管理标签…"],
        [],
        True,
    ),
    "sessions.create-menu": (
        "more create options",
        "更多创建选项",
        ["Sessions", "More create options"],
        ["会话", "更多创建选项"],
        [],
        True,
    ),
    "sessions.create-menu.new-folder": (
        "make a folder for my chats",
        "给会话建文件夹",
        ["Sessions", "More create options", "New folder"],
        ["会话", "更多创建选项", "新建文件夹"],
        [],
        True,
    ),
    "sessions.show-all-older": (
        "show all my old sessions",
        "显示全部历史会话",
        ["Sessions", "Show all older sessions"],
        ["会话", "显示所有较早的会话"],
        ["older_sessions_collapsed"],
        True,
    ),
    "sessions.row-duplicate": (
        "duplicate a chat",
        "复制会话",
        ["Sessions", "Fork chat"],
        ["会话", "分叉对话"],
        ["has_open_sessions", "pointer_on_session_row"],
        False,
    ),
    "sessions.row-close": (
        "close a session",
        "关闭会话",
        ["Sessions", "Close session"],
        ["会话", "关闭会话"],
        ["has_open_sessions", "pointer_on_session_row"],
        False,
    ),
}


@pytest.mark.parametrize("loc_id", sorted(_CASES))
@pytest.mark.parametrize("lang", ["en", "zh-CN"])
def test_newcomer_phrasing_finds_the_location_with_its_exact_path(loc_id: str, lang: str) -> None:
    en_q, zh_q, en_path, zh_path, conds, both = _CASES[loc_id]
    top = _top(en_q if lang == "en" else zh_q, lang)
    assert top["id"] == loc_id
    placements = top["placements"]
    assert len(placements) == (2 if both else 1)
    want_path = en_path if lang == "en" else zh_path
    for p in placements:
        assert _path(p) == want_path
        assert p["route"] == "/chat"
        assert _conditions(p) == conds
    desktop = placements[0]
    assert _viewport(desktop) == "desktop"
    assert _shown_by(desktop)[1] == _DESKTOP_STEP[1]
    if both:
        mobile = placements[1]
        assert _viewport(mobile) == "mobile"
        assert _shown_by(mobile)[1] == _MOBILE_STEP[1]


def test_the_steps_quote_the_sidebar_toggles_in_english() -> None:
    desktop, mobile = _top("delete many sessions", "en")["placements"]
    assert _shown_by(desktop) == _DESKTOP_STEP
    assert _shown_by(mobile) == _MOBILE_STEP


@pytest.mark.parametrize(
    ("query", "lang", "loc_id"),
    [
        ("delete many sessions", "en", "sessions.list-menu.clean-up"),
        ("批量删除会话", "zh-CN", "sessions.list-menu.clean-up"),
        ("close a session", "en", "sessions.row-close"),
        ("关闭会话", "zh-CN", "sessions.row-close"),
        ("显示全部历史会话", "zh-CN", "sessions.show-all-older"),
    ],
)
def test_more_planned_phrasings_rank_first(query: str, lang: str, loc_id: str) -> None:
    assert _top(query, lang)["id"] == loc_id


def test_ambiguous_planned_phrasings_still_list_the_sessions_control() -> None:
    # "all chats" is the bulk item's own phrase, so it ranks first and is not a
    # tie; the per-chat model chip (its term "change the model for this chat")
    # still follows, since "for all chats" also mentions the model of a chat.
    d = ui_index.find_ui("How do I change the model for all chats?", "en")
    ids = [r["id"] for r in d["results"]]
    assert ids[:2] == ["sessions.list-menu.switch-model", "chat.model-picker"]
    assert d["ambiguous"] is False
    # "New folder" is a label on several pages (Artifacts, Schedule, Sessions):
    # without context words it is a tie that lists all of them.
    d = ui_index.find_ui("新建文件夹", "zh-CN")
    ids = [r["id"] for r in d["results"]]
    assert "sessions.create-menu.new-folder" in ids
    if len(ids) > 1:
        assert d["ambiguous"] is True


def test_the_list_menu_view_item_is_one_location_for_both_views() -> None:
    assert _top("switch to list view", "en")["id"] == "sessions.list-menu.view"
    assert _top("切换到列表视图", "zh-CN")["id"] == "sessions.list-menu.view"


def test_existing_older_sessions_golden_still_wins_its_own_question() -> None:
    assert _top("where are my older sessions?", "en")["id"] == "chat.older-sessions"


@pytest.mark.parametrize(
    ("query", "lang"),
    [
        ("clean my room", "en"),
        ("close the window", "en"),
        ("where are my keys", "en"),
        ("chat about dinner", "en"),
    ],
)
def test_unrelated_phrasings_stay_no_match(query: str, lang: str) -> None:
    assert ui_index.find_ui(query, lang)["status"] == "no_match"
