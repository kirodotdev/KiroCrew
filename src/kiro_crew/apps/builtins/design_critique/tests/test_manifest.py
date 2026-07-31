"""Manifest + packaging-contract tests for the ``design-critique`` builtin app.

These tests validate the *static* contract of the builtin — its ``app.json``,
the resource paths it declares, its dashboard route, and its shipped assets —
NOT the app's runtime UI or behaviour. They are the CI backstop that catches a
manifest edit which would silently break install / discovery / navigation for
every user.

Everything is resolved relative to this file, so the suite is machine
independent (no hardcoded absolute paths).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kiro_crew.apps.discovery import discover_builtin_apps
from kiro_crew.apps.manifest import AppManifest

# ---------------------------------------------------------------------------
# Path anchors (all derived from this file's location — no absolute paths)
# ---------------------------------------------------------------------------

# .../design_critique/tests/test_manifest.py
#   parents[0] = tests
#   parents[1] = design_critique   (the app root)
#   parents[2] = builtins
#   parents[3] = apps
#   parents[4] = kiro_crew
#   parents[5] = src
#   parents[6] = repo root
APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[6]
APP_JSON = APP_ROOT / "app.json"

# Where the frontend serves /app-assets/** from.
APP_ASSETS_DIR = REPO_ROOT / "website" / "public" / "app-assets" / "design-critique"

APP_NAME = "design-critique"

# The dashboard's BuiltinAppRoute resolves a SINGLE path param; its guard regex
# (mirrored from the frontend `/^\/[A-Za-z0-9][A-Za-z0-9._~-]*$/`) rejects any
# route with a second '/'. A route like "/apps/design-critique" would register
# but then silently redirect to chat, shipping a dead nav entry.
ROUTE_GUARD_RE = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._~-]*$")

# The five automation scripts the skill ships and the docs it depends on.
_SKILL_REQUIRED_FILES = (
    "SKILL.md",
    "frameworks/main-checklist.md",
    "scripts/capture-build.mjs",
    "scripts/capture-site.mjs",
    "scripts/discover-routes.mjs",
    "scripts/ensure-playwright.mjs",
    "scripts/render.mjs",
)

# The asset URLs declared in app.json, keyed by manifest field name.
_ASSET_URL_FIELDS = (
    "iconUrl",
    "heroImage",
    "heroImageDark",
    "heroImageDetail",
    "heroImageDetailDark",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def raw_manifest() -> dict:
    """The parsed-but-untyped app.json dict (asset URLs live outside the
    typed AppManifest fields, so we also read the raw JSON)."""
    return json.loads(APP_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> AppManifest:
    return AppManifest.from_json_file(APP_JSON)


# ---------------------------------------------------------------------------
# 1. app.json parses and validates with no errors
# ---------------------------------------------------------------------------

def test_app_json_exists():
    assert APP_JSON.is_file(), f"missing app.json at {APP_JSON}"


def test_manifest_validates_with_no_errors(manifest: AppManifest):
    errors = manifest.validate(app_root=APP_ROOT)
    assert errors == [], f"AppManifest.validate returned errors: {errors}"


def test_manifest_identity(manifest: AppManifest):
    # Guards against a rename that would break discovery / the route below.
    assert manifest.name == APP_NAME
    assert manifest.version  # semver already enforced by validate()
    assert manifest.displayName


# ---------------------------------------------------------------------------
# 2. discover_builtin_apps() includes 'design-critique'
# ---------------------------------------------------------------------------

def test_discovery_includes_design_critique():
    apps = discover_builtin_apps()
    names = [a.get("name") for a in apps]
    assert APP_NAME in names, (
        f"{APP_NAME!r} not discovered — discovery drops apps whose manifest "
        f"fails validation. Discovered: {names}"
    )


# ---------------------------------------------------------------------------
# 3. defaultEnabled is False (opt-in, never on for a fresh install)
# ---------------------------------------------------------------------------

def test_default_enabled_is_false_in_manifest(manifest: AppManifest):
    # defaultEnabled is not a typed AppManifest field, so it is preserved in
    # `extra`. It MUST be the boolean False — a regression here would turn the
    # app on in every new user's sidebar.
    assert manifest.extra.get("defaultEnabled") is False, (
        "design-critique must be opt-in: defaultEnabled must be boolean false, "
        f"got {manifest.extra.get('defaultEnabled')!r}"
    )


def test_default_enabled_is_false_in_discovery():
    # The value the registration path actually sees must also be False.
    apps = discover_builtin_apps()
    entry = next((a for a in apps if a.get("name") == APP_NAME), None)
    assert entry is not None, f"{APP_NAME!r} not discovered"
    assert entry.get("defaultEnabled") is False, (
        f"discovered defaultEnabled must be boolean false, got "
        f"{entry.get('defaultEnabled')!r}"
    )


# ---------------------------------------------------------------------------
# 4. The ui.pages route is a SINGLE plain path segment (most valuable test)
# ---------------------------------------------------------------------------

def test_single_ui_page_declared(manifest: AppManifest):
    assert len(manifest.ui.pages) == 1, (
        f"expected exactly one ui.pages entry, got {len(manifest.ui.pages)}"
    )


def test_ui_route_is_single_path_segment(manifest: AppManifest):
    route = manifest.ui.pages[0].route
    assert ROUTE_GUARD_RE.match(route), (
        f"ui route {route!r} is not a single plain path segment. "
        f"BuiltinAppRoute's guard is {ROUTE_GUARD_RE.pattern!r}; a route with a "
        "second '/' (e.g. '/apps/design-critique') registers but silently "
        "redirects to chat, shipping a dead nav entry."
    )


def test_route_guard_regex_rejects_nested_paths():
    # Documents the guard's intent: a nested path must NOT be accepted, so the
    # positive test above is meaningful.
    assert ROUTE_GUARD_RE.match("/design-critique")
    assert not ROUTE_GUARD_RE.match("/apps/design-critique")
    assert not ROUTE_GUARD_RE.match("/")
    assert not ROUTE_GUARD_RE.match("design-critique")  # missing leading slash


# ---------------------------------------------------------------------------
# 5. Every declared resource path exists on disk
# ---------------------------------------------------------------------------

def test_declares_no_agent(manifest: AppManifest):
    # Deliberately empty. A builtin's declared `agents` are never registered:
    # discovery does not serialize the field, and bridges._register_agents resolves
    # an app root under the user data dir, where a builtin has only app.json. An
    # agent named here would look installed and then fail every /api/chat call, so
    # the critic persona travels in the prompt (website/.../prompts.ts CRITIC) and
    # runs on the core agent instead.
    #
    # If the platform gains real builtin-agent registration, delete this test and
    # ship agents/design-critic.json again.
    assert not manifest.agents, (
        "this builtin must not declare agents until builtin agent registration exists; "
        f"got {manifest.agents}"
    )


def test_declared_skill_paths_exist(manifest: AppManifest):
    assert manifest.skills, "manifest declares no skills"
    for rel in manifest.skills:
        assert (APP_ROOT / rel).is_dir(), f"declared skill dir missing: {rel}"


def test_skill_required_files_exist(manifest: AppManifest):
    # Within each declared skill dir, the SKILL.md, checklist, and the five
    # automation scripts must all be present.
    for rel in manifest.skills:
        skill_dir = APP_ROOT / rel
        for required in _SKILL_REQUIRED_FILES:
            assert (skill_dir / required).is_file(), (
                f"skill {rel!r} missing required file: {required}"
            )


# ---------------------------------------------------------------------------
# 6. Declared asset URLs point at files under website/public/app-assets/…
#    The App Store renders the icon and the four hero variants straight from
#    these URLs, so a typo in the manifest shows up as broken art rather than
#    an error — hence asserting the files exist on disk.
# ---------------------------------------------------------------------------

def _asset_url_to_path(url: str) -> Path:
    # URL form: "/app-assets/design-critique/icon.svg" served from website/public
    return REPO_ROOT / "website" / "public" / url.lstrip("/")


def test_declared_asset_urls_present(raw_manifest: dict):
    declared = {field: raw_manifest.get(field) for field in _ASSET_URL_FIELDS}

    missing_fields = [f for f, u in declared.items() if not u]
    assert not missing_fields, f"manifest missing asset URL fields: {missing_fields}"

    # Presence is asserted above, so every value is a non-empty string here.
    urls: dict[str, str] = {f: str(u) for f, u in declared.items()}

    # Every asset URL must be namespaced under the app's own asset dir.
    for field, url in urls.items():
        assert url.startswith("/app-assets/design-critique/"), (
            f"{field} URL {url!r} is not under /app-assets/design-critique/"
        )

    resolved = {field: _asset_url_to_path(url) for field, url in urls.items()}
    missing_files = {f: str(p) for f, p in resolved.items() if not p.is_file()}
    assert not missing_files, f"declared asset files missing on disk: {missing_files}"


# ---------------------------------------------------------------------------
# 7. No file in the builtin contains a hardcoded '/Users/' absolute path
# ---------------------------------------------------------------------------

def _iter_builtin_files() -> list[Path]:
    skip_dirs = {"tests", "__pycache__", "node_modules", ".git"}
    files: list[Path] = []
    for path in APP_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.relative_to(APP_ROOT).parts):
            continue
        files.append(path)
    return files


def test_no_hardcoded_user_paths():
    offenders: list[str] = []
    for path in _iter_builtin_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable file — nothing to scan
        if "/Users/" in text:
            offenders.append(str(path.relative_to(APP_ROOT)))
    assert not offenders, (
        "hardcoded '/Users/' absolute path found in builtin files (breaks on "
        f"other machines): {offenders}"
    )
