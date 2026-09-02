"""Task Runner ("projects") ships as a file-based builtin manifest.

Task Runner used to be a hardcoded ``_BUILTIN_APPS`` entry while every other
builtin owned an ``apps/builtins/<name>/app.json``. These tests pin the move:
the manifest must exist, be discovered, keep the identity/App-Store presentation
the hardcoded entry had, and no longer be duplicated in the Python list.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.apps.manager as mgr
from kiro_crew.apps.discovery import _get_builtins_dir, discover_builtin_apps
from kiro_crew.apps.manager import _validate_builtin_app

APP_NAME = "projects"


@pytest.fixture(scope="module")
def manifest_path() -> Path:
    return _get_builtins_dir() / APP_NAME / "app.json"


@pytest.fixture(scope="module")
def discovered() -> dict:
    for app in discover_builtin_apps():
        if app["name"] == APP_NAME:
            return app
    pytest.fail(
        "projects was not discovered from apps/builtins/ — the manifest is "
        "missing, invalid, or the directory lacks app.json"
    )


def test_manifest_file_exists(manifest_path: Path) -> None:
    assert manifest_path.is_file(), f"expected a manifest at {manifest_path}"


def test_manifest_is_valid_json(manifest_path: Path) -> None:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["name"] == APP_NAME


def test_not_duplicated_in_hardcoded_list() -> None:
    """The hardcoded entry must be gone, or it would shadow the manifest.

    register_builtin_apps() gives _BUILTIN_APPS precedence on name collision, so
    a leftover entry would keep winning and every manifest edit would be a no-op.
    """
    names = {a["name"] for a in mgr._BUILTIN_APPS}
    assert APP_NAME not in names


def test_discovered_entry_passes_builtin_validation(discovered: dict) -> None:
    assert _validate_builtin_app(discovered) == []


def test_keeps_its_identity_and_sidebar_entry(discovered: dict) -> None:
    """Identity and nav placement are what users see — pin them exactly.

    The route in particular is load-bearing twice over: it keys the sidebar entry
    AND the frontend's BUILTIN_COMPONENT_REGISTRY lookup, so a change here
    silently drops the page.
    """
    assert discovered["displayName"] == "Task Runner"
    assert discovered["version"] == "1.0.0"
    assert discovered["author"] == "kirocrew"
    assert discovered["ui"]["pages"] == [
        {"route": "/projects", "label": "Task Runner", "icon": "ClipboardCheck"}
    ]


def test_stays_enabled_on_a_fresh_install(discovered: dict) -> None:
    """Task Runner is a core surface, not an opt-in add-on.

    Moving the manifest between registration paths must not change fresh-install
    visibility, so assert the exemption from BOTH sides: the manifest says True
    and the shared allowlist agrees.
    """
    assert discovered["defaultEnabled"] is True
    assert APP_NAME in mgr._DEFAULT_ON_BUILTINS


def test_declares_the_api_surface_it_owns(discovered: dict) -> None:
    """Every other builtin declares its permissions; projects now does too."""
    assert discovered["permissions"]["api"] == ["/api/taskrunner"]


def test_keeps_its_app_store_presentation(discovered: dict) -> None:
    """The App Store card needs art and highlights — assert they are PRESENT.

    Deliberately not pinning the exact tag list, highlight count, or asset
    filenames: that would re-couple routine copy/art edits to a Python test edit,
    which is the friction moving the manifest out of Python was meant to remove.
    What matters is that the fields exist and point into this app's own asset
    namespace (a copy/paste from another app's manifest would render the wrong
    art), so that is what is checked.
    """
    assert discovered["iconUrl"].startswith("/app-assets/projects/")
    for key in ("heroImage", "heroImageDark", "heroImageDetail", "heroImageDetailDark"):
        assert discovered[key].startswith("/app-assets/projects/"), key
    assert discovered["highlights"], "App Store detail page needs at least one highlight"
    assert discovered["tags"], "discovery/filtering needs at least one tag"


def test_hero_and_icon_assets_exist_on_disk(discovered: dict) -> None:
    """A manifest pointing at missing art renders a broken App Store card.

    Assets are served from the built frontend's dist/app-assets, but the source
    of truth is website/public/app-assets — check there so the test does not
    require a build.
    """
    repo_root = Path(__file__).resolve().parents[1]
    public = repo_root / "website" / "public" / "app-assets"
    if not public.is_dir():
        pytest.skip("website/public/app-assets not present (packaged install)")
    refs = [discovered["iconUrl"]] + [
        discovered[k]
        for k in ("heroImage", "heroImageDark", "heroImageDetail", "heroImageDetailDark")
    ]
    for ref in refs:
        rel = ref.removeprefix("/app-assets/")
        assert (public / rel).is_file(), f"missing asset for {ref}"
