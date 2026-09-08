"""Platform + manifest-contract tests for the ``agent-worlds`` builtin app.

Agent Worlds is a MANIFEST-ONLY builtin: this directory's app.json is the whole
app, and its UI is a lazy-loaded page in the host bundle
(``website/src/pages/WorldsPage.tsx``, routed from
``website/src/apps/builtinRegistry.ts``). So the tests here validate the *static*
contract — which platforms the manifest claims, and the no-backend shape that
makes the Windows claim true — not runtime behaviour.

Everything resolves relative to this file, so the suite is machine independent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kiro_crew.apps.discovery import discover_builtin_apps
from kiro_crew.apps.manifest import AppManifest, PlatformConfig

# .../agent_worlds/tests/test_manifest.py -> parents[1] is the app root.
APP_ROOT = Path(__file__).resolve().parents[1]
APP_JSON = APP_ROOT / "app.json"

APP_NAME = "agent-worlds"
DECLARED_OS = ["macos", "linux", "windows"]


@pytest.fixture(scope="module")
def raw_manifest() -> dict:
    return json.loads(APP_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> AppManifest:
    return AppManifest.from_json_file(APP_JSON)


# --- manifest platform declaration ---


def test_manifest_declares_every_platform_the_app_runs_on(raw_manifest: dict):
    """``platform.os`` summarises the whole app, and the whole app is a manifest
    plus a page in the host bundle — no backend, no subprocess, no filesystem
    path — so it runs wherever the gateway runs.

    Pinned because omitting the block is not neutral: ``PlatformConfig.os``
    defaults to ``["macos", "linux"]``, which silently drops Windows and hides
    the app from the App Store on win32. That default was the ONLY thing
    blocking native Windows here.
    """
    assert raw_manifest["platform"]["os"] == DECLARED_OS


def test_declared_platforms_all_resolve_to_a_real_sys_platform(raw_manifest: dict):
    """Every declared name must map to a sys.platform value.

    An unmapped name is silently accepted into the list and then never matches,
    so a declaration can claim a platform the gate rejects.
    """
    cfg = PlatformConfig(os=raw_manifest["platform"]["os"])
    for sys_platform in ("darwin", "linux", "win32"):
        assert cfg.supports_platform(sys_platform), sys_platform


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


def test_pop_out_does_not_require_the_desktop_shell(manifest: AppManifest):
    """``requiresDesktopApp`` is a different axis from ``os`` and must stay
    false: the second-screen pop-out is ``window.open`` plus a cross-window
    scene broadcast, which behaves the same in a browser tab as in Electron.
    Setting it would withhold the enable action from every browser session.
    """
    assert manifest.platform.requiresDesktopApp is False


# --- the no-backend shape the Windows claim rests on ---


def test_declares_no_backend_so_windows_needs_no_sandbox(raw_manifest: dict):
    """Windows has no native OS sandbox backend, so an app that spawns an
    external process fail-closes there until an operator opts in.

    This app claims full Windows support precisely because it spawns nothing:
    no ``backend`` entry point, no ``dependencies`` on external commands.
    If either key is ever added, the ``windows`` declaration above stops being
    free and has to be re-argued against the sandbox story — that is what this
    test is here to force.
    """
    assert "backend" not in raw_manifest
    assert "dependencies" not in raw_manifest


def test_app_ships_no_executable_code(raw_manifest: dict):
    """Corollary of the test above, checked against disk rather than the
    manifest: the only Python in this builtin is the package marker and this
    test package. Any new module here could introduce a POSIX-only path,
    which is the class of bug the platform declaration would then be hiding.
    """
    assert raw_manifest["name"] == APP_NAME
    offenders = [
        str(p.relative_to(APP_ROOT))
        for p in APP_ROOT.rglob("*.py")
        if "tests" not in p.relative_to(APP_ROOT).parts and p.name != "__init__.py"
    ]
    assert not offenders, (
        "agent-worlds is manifest-only; new code here must be audited for "
        f"platform-specific behaviour before app.json keeps claiming Windows: {offenders}"
    )


def test_manifest_validates_with_no_errors(manifest: AppManifest):
    """A malformed ``platform`` block would make validation fail, and discovery
    drops any app whose manifest fails to validate — turning a platform widening
    into the app disappearing everywhere."""
    assert manifest.validate(app_root=APP_ROOT) == []


def test_discovery_still_includes_agent_worlds():
    apps = discover_builtin_apps()
    names = [a.get("name") for a in apps]
    assert APP_NAME in names, f"{APP_NAME!r} not discovered. Discovered: {names}"
