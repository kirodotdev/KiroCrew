"""``find_ui`` goldens for the Crewmates area (``website/src/uiLocations/areas/members.ts``).

Reads the committed (packaged) index.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiro_crew import ui_index


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ui_index._cache.update(key=None, index=None)
    # The committed tiers only, whatever dashboard build this checkout has:
    # the build-time auto tier is pinned in test_find_ui_auto.py.
    monkeypatch.setattr(ui_index, "AUTO_INDEX_PATH", tmp_path / "no-auto-tier.json")


def _path(placement: dict[str, Any]) -> list[str]:
    return [seg["label"] for seg in placement["path"]]


def _hit(query: str, lang: str, target: str) -> dict[str, Any]:
    d = ui_index.find_ui(query, lang)
    assert d["status"] == "ok", d
    return next(r for r in d["results"] if r["id"] == target)


def _conditions(placement: dict[str, Any]) -> list[str]:
    return [r["id"] for r in placement["requires"] if r["kind"] == "condition"]


def _has_preview_flag(placement: dict[str, Any]) -> bool:
    # A Crewmates preview prerequisite, which shipped Crewmates do not carry.
    return any(
        r["kind"] == "preview_flag" and r.get("setting_id") == "developer.crewmates"
        for r in placement["requires"]
    )


# (query, lang, id, path in the query's language, conditions)
_GOLDENS = [
    # Crewmates page: the empty roster's hero.
    (
        "make a new crewmate",
        "en",
        "members.new",
        ["Crewmates", "New crewmate"],
        ["no_crewmates"],
    ),
    ("新建队友", "zh-CN", "members.new", ["队友", "新建队友"], ["no_crewmates"]),
    (
        "advanced crewmate setup",
        "en",
        "members.new-advanced",
        ["Crewmates", "Advanced"],
        ["no_crewmates"],
    ),
    ("高级队友设置", "zh-CN", "members.new-advanced", ["队友", "高级"], ["no_crewmates"]),
    # Crewmates page: the header "Add…" menu, once a crewmate exists.
    (
        "advanced crewmate setup",
        "en",
        "members.add-menu",
        ["Crewmates", "Add…"],
        ["has_crewmates"],
    ),
    (
        "高级队友设置",
        "zh-CN",
        "members.add-menu",
        ["队友", "添加…"],
        ["has_crewmates"],
    ),
    (
        "make a team",
        "en",
        "members.add-menu.new-team",
        ["Crewmates", "Add…", "New team"],
        ["has_crewmates"],
    ),
    (
        "新建团队",
        "zh-CN",
        "members.add-menu.new-team",
        ["队友", "添加…", "新建团队"],
        ["has_crewmates"],
    ),
    ("添加队友", "zh-CN", "members.add-menu", ["队友", "添加…"], ["has_crewmates"]),
    # Crewmates page: the open crewmate.
    (
        "edit my crewmate",
        "en",
        "members.edit",
        ["Crewmates", "Profile"],
        ["crewmate_selected"],
    ),
    ("编辑队友", "zh-CN", "members.edit", ["队友", "资料卡"], ["crewmate_selected"]),
    (
        "crewmate details",
        "en",
        "members.details",
        ["Crewmates", "Dashboard & files"],
        ["crewmate_selected", "crewmate_panel_not_docked"],
    ),
    (
        "队友详情",
        "zh-CN",
        "members.details",
        ["队友", "仪表板和文件"],
        ["crewmate_selected", "crewmate_panel_not_docked"],
    ),
    # Customize > Crewmates.
    ("add a new agent", "en", "agents.add", ["Customize", "Crewmates", "New crewmate"], []),
    ("创建代理", "zh-CN", "agents.add", ["自定义", "队友", "新建队友"], []),
    (
        "create a crewmate",
        "en",
        "agents.create-first",
        ["Customize", "Crewmates", "Create your first crewmate"],
        ["no_crewmates"],
    ),
    (
        "创建队友",
        "zh-CN",
        "agents.create-first",
        ["自定义", "队友", "创建你的第一位队友"],
        ["no_crewmates"],
    ),
    (
        "change my crewmate's avatar",
        "en",
        "agents.edit-avatar",
        ["Customize", "Crewmates", "Edit avatar"],
        ["crewmate_editor_open"],
    ),
    (
        "修改头像",
        "zh-CN",
        "agents.edit-avatar",
        ["自定义", "队友", "编辑头像"],
        ["crewmate_editor_open"],
    ),
    (
        "delete an agent",
        "en",
        "agents.delete",
        ["Customize", "Crewmates", "Delete crewmate"],
        ["crewmate_editor_open", "crewmate_danger_zone_open"],
    ),
    (
        "删除代理",
        "zh-CN",
        "agents.delete",
        ["自定义", "队友", "删除队友"],
        ["crewmate_editor_open", "crewmate_danger_zone_open"],
    ),
]


@pytest.mark.parametrize(("query", "lang", "target", "path", "conds"), _GOLDENS)
def test_crewmate_controls_are_found_with_their_path_and_state(
    query: str, lang: str, target: str, path: list[str], conds: list[str]
) -> None:
    hit = _hit(query, lang, target)
    (placement,) = hit["placements"]
    assert _path(placement) == path
    assert _conditions(placement) == conds
    # Crewmates has shipped, so no control waits behind a preview flag.
    assert not _has_preview_flag(placement)


def test_creating_a_crewmate_names_every_host_and_the_state_that_draws_it() -> None:
    d = ui_index.find_ui("create a crewmate", "en")
    assert d["status"] == "ok"
    by_id = {r["id"]: r for r in d["results"]}
    assert {"members.new", "agents.add", "agents.create-first"} <= set(by_id)
    assert _conditions(by_id["members.new"]["placements"][0]) == ["no_crewmates"]
    assert _conditions(by_id["agents.add"]["placements"][0]) == []
    assert by_id["members.new"]["placements"][0]["route"] == "/members"
    assert by_id["agents.add"]["placements"][0]["route"] == "/capabilities?tab=crews"
    zh = {r["id"] for r in ui_index.find_ui("创建队友", "zh-CN")["results"]}
    assert {"members.new", "agents.add", "agents.create-first"} <= zh


def test_menu_items_hang_under_the_add_menu() -> None:
    for target in ("members.add-menu.new-team",):
        (p,) = ui_index.find_ui(target, "en")["results"][0]["placements"]
        assert [s["id"] for s in p["path"]] == ["page.members", "members.add-menu", target]
        assert p["path"][-1]["role"] == "target"


@pytest.mark.parametrize(
    ("query", "lang"),
    [
        ("make new friends", "en"),
        ("edit a photo", "en"),
        ("where are my keys", "en"),
        ("chat about dinner", "en"),
        ("交新朋友", "zh-CN"),
        ("编辑照片", "zh-CN"),
    ],
)
def test_unrelated_questions_are_no_match(query: str, lang: str) -> None:
    assert ui_index.find_ui(query, lang)["status"] == "no_match"
