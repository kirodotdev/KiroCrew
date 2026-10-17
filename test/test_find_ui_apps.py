"""``find_ui`` goldens for the ``apps`` area: Discover, Library and an app's detail page.

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


def _top(d: dict[str, Any]) -> dict[str, Any]:
    assert d["status"] == "ok", d
    return d["results"][0]


def _path(placement: dict[str, Any]) -> list[str]:
    return [seg["label"] for seg in placement["path"]]


def _conditions(placement: dict[str, Any]) -> list[str]:
    return [r["id"] for r in placement["requires"] if r["kind"] == "condition"]


#: id -> (English path, zh-CN path, route, condition ids). Library actions are
#: reached through the card's ⋯ menu -> Details: clicking a Library card
#: launches an app that has a page (LaunchpadTile `openable ? onOpen : onDetail`).
_EXPECTED: dict[str, tuple[list[str], list[str], str, list[str]]] = {
    "apps.refresh-store": (["Discover", "Refresh the store"], ["发现", "刷新商店"], "/apps", []),
    "apps.detail.install": (
        ["Discover", "Install"],
        ["发现", "安装"],
        "/apps",
        ["app_details_open", "open_app_not_installed"],
    ),
    "apps.library.tile-details": (
        ["Library", "Details"],
        ["库", "详情"],
        "/apps/library",
        ["app_tile_menu_open"],
    ),
    "apps.library.tile-disable": (
        ["Library", "Disable"],
        ["库", "禁用"],
        "/apps/library",
        ["app_tile_menu_open"],
    ),
    "apps.detail.disable": (
        ["Library", "Details", "Disable"],
        ["库", "详情", "禁用"],
        "/apps/library",
        ["app_tile_menu_open", "open_app_enabled"],
    ),
    "apps.detail.sync": (
        ["Library", "Details", "Sync"],
        ["库", "详情", "同步"],
        "/apps/library",
        ["app_tile_menu_open", "open_app_syncable"],
    ),
    "apps.detail.update": (
        ["Library", "Details", "Update"],
        ["库", "详情", "更新"],
        "/apps/library",
        ["app_tile_menu_open", "open_app_update_available"],
    ),
    "apps.detail.uninstall": (
        ["Library", "Details", "Uninstall"],
        ["库", "详情", "卸载"],
        "/apps/library",
        ["app_tile_menu_open", "open_app_removable"],
    ),
}

#: What the first condition tells a beginner, per list: Discover opens details
#: from the card, Library only from the card's ⋯ menu.
_DOORWAY = {
    "app_details_open": "an app's detail page is open (in Discover, click the app's card)",
    "app_tile_menu_open": (
        "the app card's ⋯ (More actions) menu is open (in Library, point at the app's card"
        " to show ⋯, then click it; clicking the card itself opens the app instead)"
    ),
}

_GOLDENS: list[tuple[str, str, str]] = [
    ("install an app", "en", "apps.detail.install"),
    ("how do I install an app?", "en", "apps.detail.install"),
    ("uninstall an app", "en", "apps.library.tile-uninstall"),
    ("remove an app", "en", "apps.library.tile-uninstall"),
    ("turn off an app", "en", "apps.library.tile-disable"),
    ("refresh the app store", "en", "apps.refresh-store"),
    ("sync an app from source", "en", "apps.detail.sync"),
    ("How do I update an app?", "en", "apps.detail.update"),
    ("update an app", "en", "apps.detail.update"),
    ("怎么更新应用", "zh-CN", "apps.detail.update"),
    ("更新应用", "zh-CN", "apps.detail.update"),
    ("app details", "en", "apps.library.tile-details"),
    ("安装应用", "zh-CN", "apps.detail.install"),
    ("卸载应用", "zh-CN", "apps.library.tile-uninstall"),
    ("删除应用", "zh-CN", "apps.library.tile-uninstall"),
    ("停用应用", "zh-CN", "apps.library.tile-disable"),
    ("刷新应用商店", "zh-CN", "apps.refresh-store"),
    ("同步应用", "zh-CN", "apps.detail.sync"),
    ("怎么卸载应用", "zh-CN", "apps.library.tile-uninstall"),
]


@pytest.mark.parametrize(("query", "lang", "expected"), _GOLDENS)
def test_novice_phrasing_finds_the_apps_control(query: str, lang: str, expected: str) -> None:
    d = ui_index.find_ui(query, lang)
    top = _top(d)
    assert top["id"] == expected, d
    assert not d.get("ambiguous"), d


@pytest.mark.parametrize("loc_id", sorted(_EXPECTED))
def test_each_apps_location_has_its_exact_path_route_and_requirements(loc_id: str) -> None:
    en_path, zh_path, route, conds = _EXPECTED[loc_id]
    # Search by the id's own English term so the location is the top hit.
    query = {
        "apps.refresh-store": "refresh the app store",
        "apps.detail.install": "install an app",
        "apps.library.tile-disable": "turn off an app",
        "apps.detail.disable": "disable from the app details page",
        "apps.detail.sync": "sync an app from source",
        "apps.detail.update": "update an app",
        "apps.library.tile-details": "app details",
        "apps.detail.uninstall": "uninstall from the app details page",
    }[loc_id]
    en = _top(ui_index.find_ui(query, "en"))
    assert en["id"] == loc_id
    (placement,) = en["placements"]
    assert _path(placement) == en_path, placement
    assert placement["route"] == route
    assert _conditions(placement) == conds
    assert [r for r in placement["requires"] if r["kind"] != "condition"] == []
    if conds:
        detail = next(r for r in placement["requires"] if r["kind"] == "condition")
        assert detail["description"] == _DOORWAY[conds[0]]
    assert en["availability"] == "not_observed"

    zh_query = {
        "apps.refresh-store": "刷新应用商店",
        "apps.detail.install": "安装应用",
        "apps.library.tile-disable": "停用应用",
        "apps.detail.disable": "在应用详情页停用",
        "apps.detail.sync": "同步应用",
        "apps.detail.update": "更新应用",
        "apps.library.tile-details": "应用详情",
        "apps.detail.uninstall": "在应用详情页卸载",
    }[loc_id]
    zh = _top(ui_index.find_ui(zh_query, "zh-CN"))
    assert zh["id"] == loc_id
    assert _path(zh["placements"][0]) == zh_path


def test_where_is_the_app_store_lands_on_discover() -> None:
    # No page-level term on `page.apps` yet (that is SEARCH_TERMS in
    # descriptors.ts, outside this batch), so the store question reaches the
    # Discover page through its Refresh control's term. Pin the route only.
    top = _top(ui_index.find_ui("where is the app store", "en"))
    assert top["placements"][0]["route"] == "/apps"
    assert _path(top["placements"][0])[0] == "Discover"


@pytest.mark.parametrize(
    ("query", "lang"),
    [
        ("uninstall windows", "en"),
        ("app for taxes", "en"),
        ("where are my keys", "en"),
        ("chat about dinner", "en"),
        ("卸载 Windows", "zh-CN"),
    ],
)
def test_unrelated_questions_stay_no_match(query: str, lang: str) -> None:
    d = ui_index.find_ui(query, lang)
    assert d["status"] == "no_match", d
