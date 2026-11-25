"""``ui.loading``: the layout (a closed set of placeholder shapes) and the logo
path, each validated on parse.

The dashboard reads the manifest ``GET /api/apps`` returns, which is
``AppManifest.to_dict()`` of the parsed file, so this parser is the gate that
decides what value ever reaches ``AppLoadingFallback.tsx``. Anything that is not
exactly a known name is dropped (the dashboard then draws the default "page"),
and so is a logo path the app UI route answers 400 for (no image).
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.manifest_sections import UIConfig


def _manifest(ui: dict[str, Any]) -> dict[str, Any]:
    return {"name": "ledger-lens", "displayName": "Ledger Lens", "version": "1.0.0", "ui": ui}


@pytest.mark.parametrize("layout", ["page", "rail", "centered", "split"])
def test_each_known_layout_round_trips(layout: str) -> None:
    ui = UIConfig.from_dict({"entry": "index.mjs", "loading": {"layout": layout}})
    assert ui.loading_layout == layout
    assert ui.to_dict()["loading"] == {"layout": layout}

    m = AppManifest.from_dict(_manifest({"entry": "index.mjs", "loading": {"layout": layout}}))
    assert m.to_dict()["ui"]["loading"] == {"layout": layout}


def test_layout_set_matches_the_dashboard() -> None:
    assert UIConfig.LOADING_LAYOUTS == {"page", "rail", "centered", "split"}


@pytest.mark.parametrize(
    "loading",
    [
        None,  # "loading": null
        "rail",  # non-object
        ["rail"],
        42,
        True,
        {},  # object with no layout
        {"layout": None},
        {"layout": 1},  # non-string layout
        {"layout": ["rail"]},
        {"layout": {"name": "rail"}},
        {"layout": "Rail"},  # wrong case
        {"layout": "RAIL"},
        {"layout": " rail"},  # whitespace
        {"layout": "rail "},
        {"layout": ""},
        {"layout": "grid"},  # unknown name
        {"layout": "<div>"},
        {"html": "<div class='x'></div>"},  # extra keys are never carried
    ],
)
def test_malformed_values_are_dropped(loading: Any) -> None:
    ui = UIConfig.from_dict({"entry": "index.mjs", "loading": loading})
    assert ui.loading_layout == ""
    assert "loading" not in ui.to_dict()


def test_a_str_subclass_that_forges_equality_is_refused() -> None:
    class Sly(str):
        def __eq__(self, other: object) -> bool:
            return True

        def __hash__(self) -> int:
            return hash("rail")

    ui = UIConfig.from_dict({"entry": "index.mjs", "loading": {"layout": Sly("anything")}})
    assert ui.loading_layout == ""


def test_only_layout_is_carried_from_a_valid_block() -> None:
    ui = UIConfig.from_dict(
        {"entry": "index.mjs", "loading": {"layout": "split", "css": "body{display:none}"}}
    )
    assert ui.to_dict()["loading"] == {"layout": "split"}


def test_missing_loading_leaves_the_ui_dict_unchanged() -> None:
    """The invariant for every existing app: no key appears that was not declared,
    so the persisted app.json and the signed payload are byte-identical."""
    raw = {"entry": "index.mjs", "pages": [{"route": "/apps/x", "label": "X"}]}
    ui = UIConfig.from_dict(raw)
    assert ui.loading_layout == ""
    assert "loading" not in ui.to_dict()
    assert UIConfig.from_dict(ui.to_dict()).to_dict() == ui.to_dict()


def test_a_declared_layout_survives_a_reparse() -> None:
    first = AppManifest.from_dict(_manifest({"entry": "index.mjs", "loading": {"layout": "rail"}}))
    again = AppManifest.from_dict(first.to_dict())
    assert again.ui.loading_layout == "rail"


@pytest.mark.parametrize(
    ("loading", "expected"),
    [
        ({"logo": "img/logo.png"}, {"logo": "img/logo.png"}),  # no layout needed
        ({"logo": "./logo.svg", "layout": "rail"}, {"layout": "rail", "logo": "./logo.svg"}),
    ],
)
def test_a_declared_logo_round_trips(loading: dict[str, str], expected: dict[str, str]) -> None:
    m = AppManifest.from_dict(_manifest({"entry": "index.mjs", "loading": loading}))
    assert m.to_dict()["ui"]["loading"] == expected
    assert AppManifest.from_dict(m.to_dict()).ui.loading_logo == loading["logo"]


@pytest.mark.parametrize(
    "logo",
    [
        "../secret.png",  # every spelling the app UI route answers 400 for
        "img/../../secret.png",
        "/etc/logo.png",
        "img\\logo.png",
        "https://tracker.example/logo.png",
        "//tracker.example/logo.png",
        "data:image/png;base64,AAAA",
        "C:/logo.png",
        "",
        None,
        7,
        ["logo.png"],
    ],
)
def test_a_logo_path_the_ui_route_answers_400_for_is_dropped(logo: Any) -> None:
    ui = UIConfig.from_dict({"entry": "index.mjs", "loading": {"layout": "rail", "logo": logo}})
    assert ui.loading_logo == ""
    assert ui.to_dict()["loading"] == {"layout": "rail"}
