"""Guards that builtin app icon/hero art referenced as ``/app-assets/...`` exists.

Builtin manifests point at brand SVGs via absolute ``/app-assets/<app>/<file>``
paths (top-level ``iconUrl`` / ``heroImage`` / ``heroImageDark``). Those files
live in ``website/public/app-assets/`` and are served by the gateway's
``/app-assets`` static mount. A manifest that references a non-existent file
renders the ``<img onError>`` placeholder instead of the intended art — the exact
failure mode this suite prevents (e.g. Agent Worlds shipping a lucide glyph with
its ``icon.svg`` left unreferenced, or an iconUrl pointing at a missing file).
"""
from __future__ import annotations

from pathlib import Path

import kiro_crew.apps.manager as mgr
from kiro_crew.apps.discovery import discover_builtin_apps

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APP_ASSETS_DIR = _REPO_ROOT / "website" / "public" / "app-assets"
#: Every top-level manifest field that can name an ``/app-assets/`` file. The
#: two detail banners were missing, so ten builtins referenced art this guard
#: never checked — a typo in a ``heroImageDetail`` path would have shipped the
#: broken-image placeholder on the app's own detail page with CI green.
_ASSET_FIELDS = (
    "iconUrl",
    "heroImage",
    "heroImageDark",
    "heroImageDetail",
    "heroImageDetailDark",
)


def _asset_refs(app: dict) -> list[tuple[str, str]]:
    """Return (field, path) for each /app-assets/ reference on an app dict."""
    refs: list[tuple[str, str]] = []
    for field in _ASSET_FIELDS:
        val = app.get(field)
        if isinstance(val, str) and val.startswith("/app-assets/"):
            refs.append((field, val))
    return refs


def _resolve(app_assets_path: str) -> Path:
    """Map a served ``/app-assets/<rel>`` URL to its public/ source file."""
    rel = app_assets_path[len("/app-assets/") :]
    return _APP_ASSETS_DIR / rel


def _all_builtin_apps() -> list[dict]:
    return [*mgr._BUILTIN_APPS, *discover_builtin_apps()]


def test_all_builtin_app_assets_exist() -> None:
    """Every ``/app-assets/...`` icon/hero referenced by a builtin exists on disk."""
    missing: list[str] = []
    for app in _all_builtin_apps():
        for field, path in _asset_refs(app):
            if not _resolve(path).is_file():
                missing.append(f"{app.get('name')}.{field} -> {path}")
    assert not missing, "builtin app-asset references with no file:\n" + "\n".join(missing)


def test_agent_worlds_icon_wired_to_svg() -> None:
    """Agent Worlds surfaces its colorful icon via top-level iconUrl.

    Regression: the shipped ``worlds/icon.svg`` was present but unreferenced, so
    the app fell back to the lucide Gamepad2 glyph in both the store and nav.
    """
    worlds = next((a for a in _all_builtin_apps() if a["name"] == "agent-worlds"), None)
    assert worlds is not None, "agent-worlds builtin missing from every registration path"
    assert worlds.get("iconUrl") == "/app-assets/worlds/icon.svg"
    assert _resolve(worlds["iconUrl"]).is_file()
