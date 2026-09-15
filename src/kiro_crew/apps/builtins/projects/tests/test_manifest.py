"""Platform + manifest-contract tests for the ``projects`` (Task Runner) builtin.

Task Runner is a MANIFEST-ONLY builtin: this directory's app.json is the whole
app. Its page is a lazy-loaded route in the host bundle
(``website/src/pages/ProjectsPage.tsx``) and its REST surface
(``/api/taskrunner``) is registered unconditionally by the host
(``kiro_crew.dashboard.handlers.taskrunner``), so there is no ``register_routes``
here to test. What these tests pin is the *static* contract: which platforms the
manifest claims, the no-backend shape of the package itself, and the honesty of
the Windows copy — because the runtime half a run depends on (one ``AcpRuntime``
kiro-cli process per run, plus ``git`` for git-worktree runs) is host-owned and
sandbox-gated on Windows.

Everything resolves relative to this file, so the suite is machine independent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kiro_crew.apps.discovery import discover_builtin_apps
from kiro_crew.apps.manifest import AppManifest

# .../projects/tests/test_manifest.py -> parents[1] is the app root.
APP_ROOT = Path(__file__).resolve().parents[1]
APP_JSON = APP_ROOT / "app.json"

APP_NAME = "projects"
DECLARED_OS = ["macos", "linux", "windows"]


@pytest.fixture(scope="module")
def raw_manifest() -> dict:
    return json.loads(APP_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> AppManifest:
    return AppManifest.from_json_file(APP_JSON)


# --- manifest platform declaration ---


def test_typed_manifest_carries_the_widened_os_list(manifest: AppManifest):
    """The widened list must survive parsing AND re-serialization.

    ``PlatformConfig.to_dict()`` omits ``os`` when it still equals the implicit
    ``["macos", "linux"]`` default, so a manifest that lost the ``windows``
    entry would round-trip through the App Store payload as a block with no
    ``os`` key at all — indistinguishable from never having declared one.
    """
    assert manifest.platform.os == DECLARED_OS
    assert manifest.platform.to_dict()["os"] == DECLARED_OS


@pytest.mark.skipif(
    sys.platform not in ("darwin", "linux", "win32"),
    reason="only the three platforms the manifest names are asserted here",
)
def test_app_is_supported_on_the_host_running_this_suite(manifest: AppManifest):
    """The gate that hides an app is evaluated against the live ``sys.platform``,
    so on a Windows CI runner this is the assertion that would have failed
    before the manifest was widened."""
    assert manifest.platform.supports_platform(sys.platform)


def test_task_runner_does_not_require_the_desktop_shell(manifest: AppManifest):
    """``requiresDesktopApp`` is a different axis from ``os`` and must stay
    false: the run page is ordinary dashboard UI over ``/api/taskrunner``, with
    nothing Electron-only in it. Setting it would withhold the enable action
    from every browser session, including on the platforms already supported.
    """
    assert manifest.platform.requiresDesktopApp is False


# --- the no-backend shape the Windows claim rests on ---


def test_declares_no_backend_so_the_app_itself_launches_nothing(raw_manifest: dict):
    """Windows rejects an app-owned ``exec`` launcher it cannot seal, so an app
    that ships one cannot honestly claim Windows.

    This one ships none: no ``backend`` entry point and no ``dependencies`` on
    external commands. The kiro-cli and git processes a run uses belong to the
    host's agent/git layers, which are shared with features that are not apps.
    If either key is ever added here, the ``windows`` declaration above stops
    being free and has to be re-argued — that is what this test forces.
    """
    assert "backend" not in raw_manifest
    assert "dependencies" not in raw_manifest
    assert "hooks" not in raw_manifest


def test_app_ships_no_executable_code(raw_manifest: dict):
    """Corollary of the test above, checked against disk rather than the
    manifest: the only Python in this builtin is the package marker and this
    test package. Any new module here could introduce a POSIX-only path, which
    is the class of bug the platform declaration would then be hiding.
    """
    assert raw_manifest["name"] == APP_NAME
    offenders = [
        str(p.relative_to(APP_ROOT))
        for p in APP_ROOT.rglob("*.py")
        if "tests" not in p.relative_to(APP_ROOT).parts and p.name != "__init__.py"
    ]
    assert not offenders, (
        "projects is manifest-only; new code here must be audited for "
        f"platform-specific behaviour before app.json keeps claiming Windows: {offenders}"
    )


def test_claimed_api_surface_is_the_host_registered_prefix(raw_manifest: dict):
    """The widening must not be read as opening new surface.

    ``/api/taskrunner`` is registered by the host regardless of app platform,
    so those routes already answered on win32 while the page was hidden. Pinned
    so a future edit cannot quietly attach an app-owned route family to this
    manifest under cover of the platform change.
    """
    assert raw_manifest["permissions"]["api"] == ["/api/taskrunner"]


def test_manifest_validates_with_no_errors(manifest: AppManifest):
    """A malformed ``platform`` block would make validation fail, and discovery
    drops any app whose manifest fails to validate — turning a platform widening
    into the app disappearing everywhere."""
    assert manifest.validate(app_root=APP_ROOT) == []


def test_discovery_still_includes_projects():
    apps = discover_builtin_apps()
    names = [a.get("name") for a in apps]
    assert APP_NAME in names, f"{APP_NAME!r} not discovered. Discovered: {names}"
