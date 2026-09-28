"""Store-card asset URLs join the registry entry's ``subdirectory``.

``_merge_manifest`` turns each repo-relative art path in ``app.json`` into a
``/api/apps/blob`` URL. The manifest itself is read from
``_contained_join(clone_dir, subdirectory)``, so for a monorepo entry every art
path is relative to that subdirectory, not the repo root -- and the blob route
resolves ``path`` against the repo root. Without the join the store asks for
``ui/icons/app.png`` where the file lives at ``apps/demo/ui/icons/app.png``, the
route answers 502, and the Discover card renders blank.

These tests pin the join at the store-card reader only. The manifest field keeps
its meaning (the installed-app reader still resolves it against the install
directory), and an entry without a ``subdirectory`` produces byte-identical URLs.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps import registry

REPO = "example/monorepo"
SUBDIR = "apps/demo"


def _merge(manifest: dict, subdirectory: str | None = SUBDIR) -> dict:
    entry: dict = {"name": "demo", "repo": REPO}
    if subdirectory is not None:
        entry["subdirectory"] = subdirectory
    return registry._merge_manifest(entry, manifest)


def _blob(path: str) -> str:
    return f"/api/apps/blob?repo={REPO}&path={path}"


def test_icon_url_joins_the_entry_subdirectory() -> None:
    """The reporter's case: ``iconPath: ui/icons/app.png`` under ``apps/demo``."""
    out = _merge({"iconPath": "ui/icons/app.png"})
    assert out["iconUrl"] == _blob("apps/demo/ui/icons/app.png")


def test_dark_icon_url_joins_the_entry_subdirectory() -> None:
    out = _merge({"iconPathDark": "ui/icons/app-dark.png"})
    assert out["iconUrlDark"] == _blob("apps/demo/ui/icons/app-dark.png")


def test_screenshots_join_the_entry_subdirectory() -> None:
    out = _merge(
        {
            "screenshots": ["ui/shots/one.png", "ui/shots/two.png"],
            "screenshotsDark": ["ui/shots/one-dark.png"],
        }
    )
    assert out["screenshots"] == [
        _blob("apps/demo/ui/shots/one.png"),
        _blob("apps/demo/ui/shots/two.png"),
    ]
    assert out["screenshotsDark"] == [_blob("apps/demo/ui/shots/one-dark.png")]


@pytest.mark.parametrize(
    "field",
    ["heroImage", "heroImageDark", "heroImageDetail", "heroImageDetailDark"],
)
def test_hero_images_join_the_entry_subdirectory(field: str) -> None:
    out = _merge({field: "ui/hero.svg"})
    assert out[field] == _blob("apps/demo/ui/hero.svg")


def test_entry_without_subdirectory_is_byte_identical() -> None:
    """The repo-root layout keeps exactly the URLs the store built before."""
    manifest = {
        "iconPath": "ui/icons/app.png",
        "iconPathDark": "ui/icons/app-dark.png",
        "screenshots": ["ui/shots/one.png"],
        "screenshotsDark": ["ui/shots/one-dark.png"],
        "heroImage": "ui/hero.svg",
        "heroImageDark": "ui/hero-dark.svg",
        "heroImageDetail": "ui/hero-detail.svg",
        "heroImageDetailDark": "ui/hero-detail-dark.svg",
    }
    expected = {
        "iconUrl": "/api/apps/blob?repo=example/monorepo&path=ui/icons/app.png",
        "iconUrlDark": "/api/apps/blob?repo=example/monorepo&path=ui/icons/app-dark.png",
        "screenshots": ["/api/apps/blob?repo=example/monorepo&path=ui/shots/one.png"],
        "screenshotsDark": ["/api/apps/blob?repo=example/monorepo&path=ui/shots/one-dark.png"],
        "heroImage": "/api/apps/blob?repo=example/monorepo&path=ui/hero.svg",
        "heroImageDark": "/api/apps/blob?repo=example/monorepo&path=ui/hero-dark.svg",
        "heroImageDetail": "/api/apps/blob?repo=example/monorepo&path=ui/hero-detail.svg",
        "heroImageDetailDark": "/api/apps/blob?repo=example/monorepo&path=ui/hero-detail-dark.svg",
    }
    for absent in (None, ""):
        out = _merge(manifest, subdirectory=absent)
        assert {k: out[k] for k in expected} == expected


@pytest.mark.parametrize("subdirectory", ["", ".", "./"])
def test_repo_root_subdirectory_spellings_leave_the_path_unchanged(subdirectory: str) -> None:
    assert registry._store_asset_path(subdirectory, "ui/icons/app.png") == "ui/icons/app.png"


def test_trailing_slash_on_the_subdirectory_joins_with_one_separator() -> None:
    assert (
        registry._store_asset_path("apps/demo/", "ui/icons/app.png") == "apps/demo/ui/icons/app.png"
    )


@pytest.mark.parametrize(
    "subdirectory",
    ["../outside", "apps/../../outside", "/abs/apps", "C:/apps", "apps\\demo"],
)
def test_an_escaping_subdirectory_is_not_joined(subdirectory: str) -> None:
    """Containment: the join never manufactures a path the blob route must reject.

    Such entries are dropped by the lexical gate before listing; the helper is
    the defense-in-depth layer and answers the bare path, exactly as before.
    """
    assert registry._store_asset_path(subdirectory, "ui/icons/app.png") == "ui/icons/app.png"


@pytest.mark.parametrize(
    "asset",
    ["/apps/demo/ui/icon.png", "https://example.invalid/icon.png"],
)
def test_an_absolute_asset_path_is_left_untouched(asset: str) -> None:
    assert registry._store_asset_path("apps/demo", asset) == asset


def test_empty_asset_path_stays_empty() -> None:
    assert registry._store_asset_path("apps/demo", "") == ""


def test_a_non_string_art_value_does_not_drop_the_card() -> None:
    """An untrusted manifest may declare ``"iconPath": 1`` or a list for a hero.

    The join must pass such a value through untouched rather than raise: a
    raise inside ``_merge_manifest`` falls the whole row back to the bare entry
    and loses every merged display field, while a bad value that only reaches
    the f-string costs that one field.
    """
    assert registry._store_asset_path("apps/demo", 1) == 1
    assert registry._store_asset_path("apps/demo", ["ui/hero.svg"]) == ["ui/hero.svg"]
    out = _merge({"displayName": "Demo", "iconPath": 1, "heroImage": ["ui/hero.svg"]})
    assert out["displayName"] == "Demo"
    assert out["iconUrl"] == _blob("1")
