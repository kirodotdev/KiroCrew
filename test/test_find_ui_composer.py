"""``find_ui`` goldens for the composer area (the message box and its shelf).

Reads the committed (packaged) index.
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


def _by_id(d: dict[str, Any]) -> dict[str, dict[str, Any]]:
    assert d["status"] == "ok", d
    return {r["id"]: r for r in d["results"]}


def _path(placement: dict[str, Any]) -> list[str]:
    return [seg["label"] for seg in placement["path"]]


#: The reveal step every composer control carries: a collapsed message box
#: unmounts them all (and the shelf), and the collapse survives a reload.
EXPAND = "shown_by:composer_collapsed"


def _reqs(placement: dict[str, Any]) -> list[str]:
    return [
        f"shown_by:{r['when']}" if r["kind"] == "shown_by" else r.get("id") or r.get("value")
        for r in placement["requires"]
    ]


@pytest.mark.parametrize(
    ("query", "lang", "target"),
    [
        ("upload an image to the chat", "en", "composer.add-menu.upload"),
        ("take a screenshot", "en", "composer.add-menu.screenshot"),
        ("draw a sketch", "en", "composer.add-menu.sketch"),
        ("use a skill", "en", "composer.add-menu.skill"),
        ("slash commands", "en", "composer.add-menu.slash"),
        ("reference a file", "en", "composer.add-menu.reference-file"),
        ("how much context is left", "en", "composer.context-usage"),
        ("stop the answer", "en", "composer.stop"),
        ("send my message", "en", "composer.send"),
        ("上传图片", "zh-CN", "composer.add-menu.upload"),
        ("截图", "zh-CN", "composer.add-menu.screenshot"),
        ("画草图", "zh-CN", "composer.add-menu.sketch"),
        ("使用技能", "zh-CN", "composer.add-menu.skill"),
        ("斜杠命令", "zh-CN", "composer.add-menu.slash"),
        ("上下文还剩多少", "zh-CN", "composer.context-usage"),
        ("停止回答", "zh-CN", "composer.stop"),
        ("发送消息", "zh-CN", "composer.send"),
    ],
)
def test_composer_newcomer_questions_find_their_control(query: str, lang: str, target: str) -> None:
    d = ui_index.find_ui(query, lang)
    assert d["status"] == "ok", d
    assert d["results"][0]["id"] == target, [r["id"] for r in d["results"]]


@pytest.mark.parametrize(("query", "lang"), [("how do I attach a file", "en"), ("附件", "zh-CN")])
def test_attaching_names_the_desktop_menu_and_the_phone_control(query: str, lang: str) -> None:
    d = ui_index.find_ui(query, lang)
    assert d["status"] == "ok", d
    assert {r["id"] for r in d["results"][:2]} == {"composer.add-menu", "composer.attach-files"}
    by_id = _by_id(d)
    (desk,) = by_id["composer.add-menu"]["placements"]
    phone, touch = by_id["composer.attach-files"]["placements"]
    assert _reqs(desk) == ["desktop", "mouse_input", "session_open", EXPAND]
    assert _reqs(phone) == ["mobile", "session_open", EXPAND]
    # A desktop-width touch device gets the direct picker, not the "+" menu.
    assert _reqs(touch) == ["desktop", "touch_input", "session_open", EXPAND]


def test_menu_items_walk_through_the_add_menu_in_both_languages() -> None:
    en = _by_id(ui_index.find_ui("take a screenshot", "en"))["composer.add-menu.screenshot"]
    (p,) = en["placements"]
    assert _path(p) == ["Sessions", "Add files & options", "Screenshot"]
    assert [s["role"] for s in p["path"]] == ["navigation", "navigation", "target"]
    assert p["route"] == "/chat" and p["entry"] == "menu"
    assert _reqs(p) == [
        "desktop",
        "mouse_input",
        "session_open",
        EXPAND,
        "screen_capture_available",
    ]
    zh = _by_id(ui_index.find_ui("画草图", "zh-CN"))["composer.add-menu.sketch"]
    assert _path(zh["placements"][0]) == ["会话", "添加文件与选项", "草图"]


@pytest.mark.parametrize(
    ("query", "lang", "target", "path", "reqs"),
    [
        (
            "send my message",
            "en",
            "composer.send",
            ["Sessions", "Send"],
            ["session_open", EXPAND, "message_typed", "no_response_running"],
        ),
        (
            "stop the answer",
            "en",
            "composer.stop",
            ["Sessions", "Stop generation"],
            ["session_open", EXPAND, "response_running", "message_box_empty"],
        ),
        (
            "how much context is left",
            "en",
            "composer.context-usage",
            ["Sessions", "Context usage"],
            ["session_open", EXPAND, "context_usage_reported"],
        ),
        (
            "upload an image to the chat",
            "en",
            "composer.add-menu.upload",
            ["Sessions", "Add files & options", "Upload file"],
            ["desktop", "mouse_input", "session_open", EXPAND],
        ),
        (
            "slash commands",
            "en",
            "composer.add-menu.slash",
            ["Sessions", "Add files & options", "Command"],
            ["desktop", "mouse_input", "session_open", EXPAND],
        ),
        (
            "use a skill",
            "en",
            "composer.add-menu.skill",
            ["Sessions", "Add files & options", "Skill"],
            ["desktop", "mouse_input", "session_open", EXPAND],
        ),
        (
            "reference a file",
            "en",
            "composer.add-menu.reference-file",
            ["Sessions", "Add files & options", "File"],
            ["desktop", "mouse_input", "session_open", EXPAND],
        ),
        (
            "发送消息",
            "zh-CN",
            "composer.send",
            ["会话", "发送"],
            ["session_open", EXPAND, "message_typed", "no_response_running"],
        ),
        (
            "停止回答",
            "zh-CN",
            "composer.stop",
            ["会话", "停止生成"],
            ["session_open", EXPAND, "response_running", "message_box_empty"],
        ),
        (
            "上下文还剩多少",
            "zh-CN",
            "composer.context-usage",
            ["会话", "上下文用量"],
            ["session_open", EXPAND, "context_usage_reported"],
        ),
    ],
)
def test_each_composer_location_has_its_exact_path_and_requirements(
    query: str, lang: str, target: str, path: list[str], reqs: list[str]
) -> None:
    (p,) = _by_id(ui_index.find_ui(query, lang))[target]["placements"]
    assert _path(p) == path
    assert _reqs(p) == reqs


def test_uploading_a_file_still_answers_artifacts_first() -> None:
    # The composer's Upload file row shares the words, but the artifacts import
    # stays the top answer (pinned in test_find_ui.py); the composer row is an
    # alternative, never a replacement.
    for q, lang in (("how do I upload a file", "en"), ("上传文件", "zh-CN")):
        ids = [r["id"] for r in ui_index.find_ui(q, lang)["results"]]
        assert ids[0] == "artifacts.import" and "composer.add-menu.upload" in ids, ids


@pytest.mark.parametrize(
    ("query", "lang"),
    [
        ("send a letter", "en"),
        ("take a break", "en"),
        ("where are my keys", "en"),
        ("chat about dinner", "en"),
        ("寄一封信", "zh-CN"),
        ("休息一下", "zh-CN"),
    ],
)
def test_unrelated_questions_are_no_match(query: str, lang: str) -> None:
    assert ui_index.find_ui(query, lang)["status"] == "no_match", query
