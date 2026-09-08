"""Platform-declaration tests for the ``design-critique`` builtin app.

``platform.os`` is the gate the App Store and the install path consult, so a
missing or narrow block is what makes the app invisible on a platform it in
fact runs on. Pinned here because both wrong answers are silent: omitting the
block falls back to the implicit ``["macos", "linux"]`` default (drops
Windows), and a name that maps to no ``sys.platform`` value is accepted into
the list and then never matches.

Everything is resolved relative to this file, so the suite is machine
independent (no hardcoded absolute paths).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.apps.manifest import PlatformConfig

# .../design_critique/tests/test_platform.py -> parents[1] is the app root.
APP_ROOT = Path(__file__).resolve().parents[1]
APP_JSON = APP_ROOT / "app.json"

EXPECTED_OS = ["macos", "linux", "windows"]


@pytest.fixture(scope="module")
def raw_manifest() -> dict:
    return json.loads(APP_JSON.read_text(encoding="utf-8"))


def test_manifest_declares_every_platform_the_app_runs_on(raw_manifest: dict):
    """The app is a FastAPI route module plus Node capture scripts — both are
    portable, so the declaration must name all three desktop platforms.

    Pinned because both narrower answers misinform: a shorter list reads as
    "does not run there", and omitting the block entirely falls back to the
    implicit ``["macos", "linux"]`` default, which silently drops Windows.
    """
    assert raw_manifest["platform"]["os"] == EXPECTED_OS


def test_declared_platforms_all_resolve_to_a_real_sys_platform(raw_manifest: dict):
    """Every declared name must map to a ``sys.platform`` value.

    An unmapped name is silently accepted into the list and then never matches,
    so a declaration can claim a platform the gate rejects.
    """
    cfg = PlatformConfig(os=raw_manifest["platform"]["os"])
    for sys_platform in ("darwin", "linux", "win32"):
        assert cfg.supports_platform(sys_platform), sys_platform
