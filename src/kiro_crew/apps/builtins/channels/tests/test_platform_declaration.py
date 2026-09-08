"""Platform-declaration tests for the ``channels`` builtin app.

``channels`` is a pure manifest + frontend app: its directory holds only
``app.json``, and the implementation lives in the cross-platform core
(``kiro_crew/dashboard/handlers_channel.py`` and ``kiro_crew/channel.py``,
neither of which imports ``subprocess``, ``signal``, ``fcntl``, ``pwd`` or
``resource``). So the app is functionally complete on native Windows the moment
the manifest stops implying otherwise — and the manifest was implying otherwise,
because an absent ``platform`` block falls back to ``PlatformConfig``'s default
``["macos", "linux"]``.

Everything is resolved relative to this file, so the suite is machine
independent (no hardcoded absolute paths).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.discovery import discover_builtin_apps
from kiro_crew.apps.manifest import AppManifest, PlatformConfig

# .../channels/tests/test_platform_declaration.py
#   parents[0] = tests
#   parents[1] = channels   (the app root)
APP_ROOT = Path(__file__).resolve().parents[1]
APP_JSON = APP_ROOT / "app.json"

APP_NAME = "channels"
DECLARED_OS = ["macos", "linux", "windows"]


@pytest.fixture(scope="module")
def raw_manifest() -> dict:
    return json.loads(APP_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> AppManifest:
    return AppManifest.from_json_file(APP_JSON)


# ---------------------------------------------------------------------------
# 1. The declaration itself
# ---------------------------------------------------------------------------


def test_typed_manifest_carries_the_same_platform_list(manifest: AppManifest):
    # The raw JSON and the parsed manifest must not disagree: the gates read the
    # typed value, while humans read the file.
    assert manifest.platform.os == DECLARED_OS


# ---------------------------------------------------------------------------
# 2. The declaration is load-bearing, and it reaches the surface
# ---------------------------------------------------------------------------


def test_the_implicit_default_would_have_excluded_windows():
    """Documents why the block is needed at all, so test 1 is meaningful.

    Without it, ``PlatformConfig()`` answers False for win32 — the app would read
    as macOS/Linux-only even though nothing in it is POSIX-specific.
    """
    default_cfg = PlatformConfig()
    assert default_cfg.os == ["macos", "linux"]
    assert not default_cfg.supports_platform("win32")


def test_discovery_serializes_the_platform_declaration():
    """``PlatformConfig.to_dict()`` emits ``os`` ONLY when it differs from the
    default, so a regression back to ``["macos", "linux"]`` does not merely
    change the value — it removes the key, and the App Store detail page then
    renders no platform row at all.
    """
    entry = next((a for a in discover_builtin_apps() if a.get("name") == APP_NAME), None)
    assert entry is not None, f"{APP_NAME!r} not discovered"
    assert entry["platform"]["os"] == DECLARED_OS


# ---------------------------------------------------------------------------
# 3. The properties that make "full on Windows" true
# ---------------------------------------------------------------------------


def test_manifest_validates_with_no_errors(manifest: AppManifest):
    # Guards the hand-edited JSON: a malformed manifest is dropped by discovery
    # rather than reported, so the app would just vanish from the App Store.
    assert manifest.validate(app_root=APP_ROOT) == []


def test_app_declares_no_backend_and_no_dependencies(raw_manifest: dict):
    """The Windows claim is "functionally complete", not merely "installable" —
    and that holds only while the app spawns no external process.

    Kiro Crew has no native OS sandbox backend on Windows, so a backend that
    shelled out would fail closed there. If someone adds one, the manifest's
    ``windows`` promise becomes a lie and this test is the place that says so.

    Asserted against the raw JSON on purpose: the typed fields are dataclasses
    with eager defaults, so ``manifest.backend`` and ``manifest.dependencies``
    are truthy objects whether or not the manifest declared them.
    """
    for absent in ("backend", "dependencies", "hooks", "setup", "mcpServers"):
        assert absent not in raw_manifest, (
            f"{absent!r} appeared in the manifest — re-check the 'full on Windows' "
            "claim before shipping it, since Windows has no native sandbox backend"
        )
    assert not raw_manifest.get("platform", {}).get("requiresDesktopApp", False)


def test_platform_block_did_not_disturb_the_opt_in_flags(raw_manifest: dict):
    # The new block was inserted between `hidden` and `permissions`; this pins
    # that the surrounding contract survived the edit. The app must stay hidden
    # and off by default — it is enabled with `kirocrew app enable channels`.
    assert raw_manifest["defaultEnabled"] is False
    assert raw_manifest["hidden"] is True
    assert raw_manifest["permissions"]["api"] == ["/api/channels"]
    assert raw_manifest["ui"]["pages"][0]["route"] == "/channels"
