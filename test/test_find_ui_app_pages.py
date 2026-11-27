"""``find_ui`` answers with the pages of the apps that ship in the package.

The generator (``website/scripts/gen-ui-index.mjs``) turns every packaged
``app.json``'s ``ui.pages[]`` into an ``apppage.*`` location at build time;
find_ui reads only the committed index. These tests read that committed index
and the packaged manifests, so they run in every backend lane, with no node.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import ui_index
from kiro_crew.ui_index_load import _parse

REPO = Path(__file__).resolve().parents[1]
BUILTINS = REPO / "src" / "kiro_crew" / "apps" / "builtins"
APP_PAGE_PREFIX = "apppage."


@pytest.fixture(autouse=True, scope="module")
def _fresh_cache() -> None:
    ui_index._cache.update(key=None, index=None)


@pytest.fixture(autouse=True)
def _no_auto_tier(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The committed tiers only, whatever dashboard build this checkout has.
    _floor_monkeypatch.setattr(ui_index, "AUTO_INDEX_PATH", tmp_path / "no-auto-tier.json")


def _manifests() -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(BUILTINS.glob("*/app.json"))]


def _committed() -> dict[str, Any]:
    return json.loads(ui_index.INDEX_PATH.read_text(encoding="utf-8"))


def test_aws_control_answers_with_its_app_page() -> None:
    d = ui_index.find_ui("AWS Control", "en")
    assert d["status"] == "ok", d
    top = d["results"][0]
    assert top["id"] == "apppage.aws-control.aws-control"
    assert top["kind"] == "page"
    assert top["label"] == "AWS Control"
    assert top["app"] == "AWS Control"
    (placement,) = top["placements"]
    assert placement["route"] == "/aws-control"
    assert placement["entry"] == "rail"
    # find_ui never reads live state: a page of an app that is off or not
    # installed is answered with the prerequisite that says so.
    assert {"kind": "condition", "id": "app_enabled"} == {
        k: placement["requires"][0][k] for k in ("kind", "id")
    }


def test_app_pages_answer_in_the_label_the_rail_shows() -> None:
    # The rail renders a built-in app's page label from its catalog key
    # (APP_MANIFEST_KEY), so the index carries that key, not the English text,
    # and a Chinese question finds the page by its Chinese label.
    index = _committed()
    pages = [loc for loc in index["locations"] if loc["id"].startswith(APP_PAGE_PREFIX)]
    assert pages
    assert [loc["id"] for loc in pages if loc["label_key"].startswith("literal:")] == []
    aws = next(loc for loc in pages if loc["id"] == "apppage.aws-control.aws-control")
    zh_label = index["labels"]["zh-CN"][aws["label_key"]]
    d = ui_index.find_ui(zh_label, "zh-CN")
    assert d["status"] == "ok", d
    assert d["results"][0]["id"] == aws["id"]
    assert d["results"][0]["label"] == zh_label


def test_the_app_name_finds_a_page_whose_label_differs() -> None:
    # Ops Mission Control's page is labelled "Mission Control" on the rail.
    by_label = {
        p["label"]: m["displayName"]
        for m in _manifests()
        for p in (m.get("ui") or {}).get("pages", [])
        if p["label"] != m["displayName"]
    }
    assert by_label, "no packaged app page has a label other than its app's name"
    label, app = sorted(by_label.items())[0]
    d = ui_index.find_ui(app, "en")
    assert d["status"] == "ok", d
    assert d["results"][0]["app"] == app
    assert d["results"][0]["label"] == label


def test_committed_index_matches_the_packaged_manifests() -> None:
    """A manifest edit that changes an indexed page must regenerate the index."""
    index = _committed()
    app_pages = {
        loc["id"]: loc for loc in index["locations"] if loc["id"].startswith(APP_PAGE_PREFIX)
    }
    core_routes = {
        p["route"]
        for loc in index["locations"]
        if not loc["id"].startswith(APP_PAGE_PREFIX)
        for p in loc["placements"]
    }
    got = {
        (
            loc["placements"][0]["route"],
            index["labels"]["en"][loc["label_key"]],
            loc["app"],
        )
        for loc in app_pages.values()
    }
    want = set()
    for m in _manifests():
        for page in (m.get("ui") or {}).get("pages", []):
            if page["route"] in core_routes:
                continue
            want.add(
                (
                    page["route"],
                    page["label"].strip(),
                    m["displayName"],
                )
            )
    assert got == want, "run `npm run gen:ui` in website/ and commit the result"
    for loc in app_pages.values():
        (placement,) = loc["placements"]
        assert placement["requires"] == [{"kind": "condition", "id": "app_enabled"}]
        assert placement["entry_kind"] == "rail"


def test_app_pages_are_in_the_apps_area() -> None:
    idx = ui_index.load_index()
    members = ui_index.area_members(idx, "apps")
    assert "apppage.aws-control.aws-control" in members


def test_loader_refuses_a_malformed_app_name() -> None:
    raw = copy.deepcopy(_committed())
    loc = next(x for x in raw["locations"] if x["id"].startswith(APP_PAGE_PREFIX))
    loc["app"] = 5
    with pytest.raises(ui_index.IndexUnavailable):
        _parse(raw)


#: The code that turns manifests into locations and answers with them.
_CORE_FILES = (
    "website/scripts/gen-ui-index.mjs",
    "website/scripts/lib/ui-index.mjs",
    "website/scripts/lib/ui-index-apps.mjs",
    "src/kiro_crew/ui_index.py",
    "src/kiro_crew/ui_index_load.py",
    "src/kiro_crew/ui_index_records.py",
)


def test_core_names_no_specific_app() -> None:
    names = sorted(
        {n for m in _manifests() for n in (m["name"], m["displayName"]) if isinstance(n, str)}
    )
    assert names
    for rel in _CORE_FILES:
        text = (REPO / rel).read_text(encoding="utf-8")
        for name in names:
            # A quoted literal is how code would special-case an app.
            hit = re.search(r"[\"'`]" + re.escape(name) + r"[\"'`]", text)
            assert hit is None, f"{rel} names the app {name!r}: read it from the manifest instead"
