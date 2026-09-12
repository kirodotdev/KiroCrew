"""Manifest invariants, mirroring design_critique's template.

Paths are anchored off ``__file__`` rather than hardcoded, so the suite runs in
any worktree.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from kiro_crew.apps.builtins import BUILTIN_NAMES
from kiro_crew.apps.discovery import discover_builtin_apps
from kiro_crew.apps.manifest import AppManifest

APP_NAME = "agentcore-observatory"
APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parents[4]
WEBSITE_PUBLIC = REPO_ROOT / "website" / "public"

#: Mirrored from builtinRegistry.ts's _BUILTIN_ROUTE_RE: a builtin page route is
#: ONE segment. A nested route resolves to no component and renders blank.
_ROUTE_RE = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._~-]*$")


def _manifest() -> AppManifest:
    return AppManifest.from_json_file(APP_ROOT / "app.json")


def test_manifest_validates() -> None:
    assert (APP_ROOT / "app.json").is_file()
    assert _manifest().validate(app_root=APP_ROOT) == []


def test_identity_is_stable() -> None:
    manifest = _manifest()
    assert manifest.name == APP_NAME
    assert manifest.version
    assert manifest.displayName


def test_module_is_registered_in_builtin_names() -> None:
    """The guard for the one edit that is easy to forget.

    Route registration loops BUILTIN_NAMES; discovery does not. Missing this
    entry leaves the app visible in the App Store with no backend behind it.
    """
    assert APP_ROOT.name in BUILTIN_NAMES


def _discovered() -> dict[str, dict]:
    """Discovery returns a LIST of metadata dicts; key it by name for lookup."""
    return {app["name"]: app for app in discover_builtin_apps()}


def test_discovery_finds_the_app() -> None:
    """Also catches a manifest that fails validation — discovery drops those silently."""
    assert APP_NAME in _discovered()


def test_ships_default_disabled() -> None:
    """Asserted twice: in the raw manifest and in what discovery returns."""
    raw = json.loads((APP_ROOT / "app.json").read_text(encoding="utf-8"))
    assert raw["defaultEnabled"] is False
    assert _discovered()[APP_NAME].get("defaultEnabled") is False


def test_exactly_one_single_segment_page_route() -> None:
    pages = _manifest().ui.pages
    assert len(pages) == 1
    assert _ROUTE_RE.match(pages[0].route), pages[0].route
    assert pages[0].label


def test_route_guard_rejects_the_shapes_it_must() -> None:
    """A negative test, so the regex above cannot rot into something permissive."""
    for bad in ("/apps/agentcore-observatory", "/", "agentcore-observatory", "/a/b"):
        assert not _ROUTE_RE.match(bad), bad


def test_declares_no_agents() -> None:
    """Builtin agent registration does not exist; a declared agent registers zero."""
    assert _manifest().agents == []


def test_declared_assets_exist_on_disk() -> None:
    raw = json.loads((APP_ROOT / "app.json").read_text(encoding="utf-8"))
    for field in ("iconUrl", "heroImage", "heroImageDark"):
        url = raw.get(field)
        if not url:
            continue
        assert url.startswith(f"/app-assets/{APP_NAME}/"), f"{field}: {url}"
        assert (WEBSITE_PUBLIC / url.lstrip("/")).is_file(), f"{field}: {url}"


def test_registers_routes_from_the_package() -> None:
    """dashboard/routes/system.py checks hasattr on the PACKAGE, not on backend.routes."""
    import kiro_crew.apps.builtins.agentcore_observatory as pkg

    assert callable(getattr(pkg, "register_routes", None))


def test_no_hardcoded_home_paths() -> None:
    """No absolute developer path in shipped source.

    ``__pycache__`` is skipped because compiled bytecode embeds the build
    machine's absolute paths, and this file is skipped because it necessarily
    contains the sentinel it searches for.
    """
    sentinel = "/" + "Users/"
    for path in APP_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts or path == Path(__file__).resolve():
            continue
        assert sentinel not in path.read_text(encoding="utf-8"), path
