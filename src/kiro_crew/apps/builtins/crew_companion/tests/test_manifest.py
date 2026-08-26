"""Manifest contract tests for the Crew Companion builtin app.

Why this manifest still mirrors the user-installed one
-----------------------------------------------------
Apps live at ``apps/<name>/`` keyed on NAME ALONE, so this builtin shares a
directory with the externally distributed Crew Companion app. Registration no
longer touches such a directory -- ``register_builtin_apps()`` stands down when
``installed.json`` shows a user-owned install, because overwriting it would set
``lifecycle="locked"`` and destroy the ``origin`` record, leaving no way to tell
a user install from a builtin afterwards (see ``_builtin_owns_install`` and
``TestBuiltinDoesNotClobberUserInstall``).

The guard is the protection; keeping these fields is belt-and-braces, so the two
manifests stay behaviourally interchangeable for anyone who ends up with either:

  * ``openCommand`` — how the dashboard opens the installed .app
  * ``setup.onEnable`` — reopen the .app when the app is re-enabled
  * ``platform`` — macOS-only + client install (postInstall opens the .app)

The mcpServers URL is normalised to the loopback literal ``127.0.0.1`` (the
gateway proxy refuses ``localhost``/non-IP hosts), and the top-level asset
fields point at brand SVGs under ``/app-assets/crew-companion/`` served by the
gateway static mount rather than an app-relative ``ui.entry`` bundle.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from kiro_crew.apps.builtins.crew_companion import hooks as hooks_mod
from kiro_crew.apps.builtins.crew_companion.backend import routes as routes_mod
from kiro_crew.apps.discovery import discover_builtin_apps
from kiro_crew.apps.manifest import AppManifest

_BUILTIN_DIR = Path(__file__).resolve().parents[1]
_APP_JSON = _BUILTIN_DIR / "app.json"
# repo_root/src/kiro_crew/apps/builtins/crew_companion/tests/test_manifest.py
_REPO_ROOT = Path(__file__).resolve().parents[6]
_APP_ASSETS_DIR = _REPO_ROOT / "website" / "public" / "app-assets"

_ROUTE_RE = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._~-]*$")
_ASSET_FIELDS = ("iconUrl", "heroImage", "heroImageDark")
_ASSET_PREFIX = "/app-assets/crew-companion/"


def _raw() -> dict:
    return json.loads(_APP_JSON.read_text(encoding="utf-8"))


def _manifest() -> AppManifest:
    return AppManifest.from_json_file(_APP_JSON)


def test_app_json_exists() -> None:
    assert _APP_JSON.is_file(), f"missing manifest: {_APP_JSON}"


def test_manifest_validates_with_no_errors() -> None:
    errors = _manifest().validate(app_root=_BUILTIN_DIR)
    assert errors == [], f"manifest validation errors: {errors}"


def test_identity() -> None:
    m = _manifest()
    assert m.name == "crew-companion"
    assert m.version == "1.0.0"
    assert m.displayName == "Crew Companion"
    assert m.author == "kirocrew"


def test_discovered_as_builtin() -> None:
    names = {a["name"] for a in discover_builtin_apps()}
    assert "crew-companion" in names


def test_default_enabled_is_boolean_false() -> None:
    val = _raw().get("defaultEnabled")
    assert val is False
    assert isinstance(val, bool)


def test_exactly_one_ui_page() -> None:
    pages = _raw().get("ui", {}).get("pages", [])
    assert len(pages) == 1


def test_route_is_single_top_level_segment() -> None:
    # A nested route (more than one path segment) silently redirects to /chat,
    # so the page must live at a single top-level segment.
    route = _raw()["ui"]["pages"][0]["route"]
    assert route == "/crew-companion"
    assert _ROUTE_RE.match(route), f"route {route!r} is not a single segment"


def test_permissions_api_scopes_own_backend() -> None:
    """The backend is now IN-PROCESS, so its routes live under the gateway's own
    ``/api/apps/<name>/`` prefix rather than behind the reverse proxy at
    ``/apps/<name>/api``. This assertion changed with the architecture: the old
    path does not exist any more, and asserting it would pin a shape that
    required a second process.
    """
    api = _raw().get("permissions", {}).get("api", [])
    assert "/api/apps/crew-companion" in api
    assert "/api/apps/crew-companion/*" in api


def test_asset_urls_under_app_assets_and_files_exist() -> None:
    raw = _raw()
    missing_field = [f for f in _ASSET_FIELDS if f not in raw]
    assert not missing_field, f"manifest missing asset fields: {missing_field}"

    missing_file: list[str] = []
    for field in _ASSET_FIELDS:
        url = raw[field]
        assert isinstance(url, str) and url.startswith(_ASSET_PREFIX), (
            f"{field}={url!r} must be under {_ASSET_PREFIX}"
        )
        rel = url[len("/app-assets/"):]
        if not (_APP_ASSETS_DIR / rel).is_file():
            missing_file.append(f"{field} -> {url}")
    # NOTE: a sibling agent draws these SVGs into
    # website/public/app-assets/crew-companion/. They are present as of writing
    # (icon.svg, hero-light.svg, hero-dark.svg); this assertion fails loudly if
    # a rename/removal drops one.
    assert not missing_file, "declared asset(s) with no file on disk:\n" + "\n".join(
        missing_file
    )


def test_no_absolute_user_path_in_builtin_dir() -> None:
    # Guards the SHIPPED builtin files (manifest + package code) against a
    # hardcoded absolute home path. The tests/ dir is excluded: it is not part
    # of the app surface and legitimately contains the "/Users/" literal it
    # scans for.
    offenders: list[str] = []
    for path in _BUILTIN_DIR.rglob("*"):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "/Users/" in text:
            offenders.append(str(path.relative_to(_BUILTIN_DIR)))
    assert not offenders, f"absolute /Users/ path(s) found in: {offenders}"


def test_declares_no_separate_process_backend() -> None:
    """The inverse of the assertion this replaced, and the point of the migration.

    This manifest used to declare ``mcpServers.crew-companion.url =
    http://127.0.0.1:7778/mcp`` — a SEPARATE macOS app the gateway proxied to.
    That single field is what made the whole class of defects reachable: the
    ``.app_secret`` the proxy signs with, the malformed-port crash that could
    stop gateway startup, and the hole where a never-enabled app still had an
    authenticated route to its backend.

    There is no second process now, so there must be no loopback backend URL to
    resolve. Asserting the absence is what stops someone reintroducing it.
    """
    servers = _raw().get("mcpServers", {})
    for name, cfg in servers.items():
        url = cfg.get("url", "")
        assert not url, (
            f"mcpServers[{name!r}] declares url={url!r}; the backend runs "
            "in-process, so a loopback URL means a second process crept back in"
        )


def test_does_not_launch_anything_on_enable() -> None:
    """Enabling must not run a command that can fail.

    ``setup.onEnable`` used to be ``open "$HOME/Applications/Crew Companion.app"``,
    and ``handle_app_api_proxy`` rolls an enable BACK when that script fails. On
    every machine without that app already present — which is every machine but
    the author's — the tile therefore could not be switched on at all. Nothing
    here may reintroduce a launch step: the window follows the enabled state
    instead, so there is nothing to fail and nothing to roll back.
    """
    raw = _raw()
    assert "onEnable" not in raw.get("setup", {}), (
        "setup.onEnable is what made this app impossible to enable"
    )
    assert "openCommand" not in raw, (
        "openCommand points at a separate app that is no longer shipped"
    )
    platform = raw.get("platform", {})
    assert "clientInstall" not in platform, (
        "clientInstall describes installing a separate app on the user's machine"
    )
    assert platform.get("installMode") != "client", (
        "installMode 'client' means 'the user installs this themselves'"
    )


def test_declares_an_in_process_backend_that_imports() -> None:
    """The hooks and routes the gateway will call must exist and be importable.

    A typo in either dotted path is otherwise a runtime failure at enable time,
    on a code path that only runs on a machine where someone enabled the app.
    """
    backend = _raw().get("backend", {})
    assert backend.get("routes") == "backend.routes:register_routes"
    hooks = backend.get("hooks", {})
    assert hooks.get("on_startup") == "hooks:on_startup"
    assert hooks.get("on_shutdown") == "hooks:on_shutdown"

    assert callable(hooks_mod.on_startup)
    assert callable(hooks_mod.on_shutdown)
    assert callable(routes_mod.register_routes)


def test_requires_the_desktop_app_declaratively() -> None:
    """A flag the shell reads, not a shell command it runs.

    The companion needs Kiro Crew's desktop app to have somewhere to draw its
    window, and this is how that is stated — so the dashboard can gate the tile
    instead of the enable failing on a machine with no shell.
    """
    assert _raw().get("platform", {}).get("requiresDesktopApp") is True
