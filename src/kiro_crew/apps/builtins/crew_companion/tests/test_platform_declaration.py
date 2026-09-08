"""Platform-declaration contract tests for the Crew Companion builtin app.

Why Windows belongs in ``platform.os``
-------------------------------------
Crew Companion declares ``backend.hooks`` and ``backend.routes`` but no
``backend.entryPoint``, so the gateway never spawns a backend child process for
it -- the hooks and route handlers run in-process. Its own code is stdlib
filesystem and JSON work that is already platform-neutral (``platform_compat``
shims for POSIX modes and links, ``os.replace`` for atomic swaps, explicit
``encoding="utf-8"`` on every text read/write), so nothing in its path is
POSIX-only.

Omitting the block is not a neutral choice: the manifest then falls back to the
implicit ``["macos", "linux"]`` default, which silently gates Windows users out
of an app that runs there.

``requiresDesktopApp`` is a DIFFERENT axis and stays ``True``: it says the
companion needs the Electron desktop shell rather than a browser tab, which is
true on every OS. ``platform.os`` says which operating systems that shell may
run on. ``tests/test_manifest.py`` pins the desktop-app flag; this file pins the
OS list, so neither can be widened by accident while the other is edited.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.apps.manifest import PlatformConfig

# repo_root/src/kiro_crew/apps/builtins/crew_companion/tests/<this file>
_APP_JSON = Path(__file__).resolve().parents[1] / "app.json"


def _raw() -> dict:
    return json.loads(_APP_JSON.read_text(encoding="utf-8"))


def test_manifest_declares_every_platform_the_app_runs_on() -> None:
    """The app runs wherever the desktop shell does, so all three are declared.

    Pinned because both narrower answers misinform: dropping ``windows`` reads
    as "does not run on Windows", and omitting ``platform.os`` entirely inherits
    the implicit macOS+Linux default with the same effect.
    """
    assert _raw()["platform"]["os"] == ["macos", "linux", "windows"]


def test_declared_platforms_all_resolve_to_a_real_sys_platform() -> None:
    """Every declared name must map to a ``sys.platform`` value.

    An unmapped name is accepted into the list and then never matches, so a
    declaration can claim a platform the gate actually rejects.
    """
    cfg = PlatformConfig(os=_raw()["platform"]["os"])
    for sys_platform in ("darwin", "linux", "win32"):
        assert cfg.supports_platform(sys_platform), sys_platform


def test_desktop_app_requirement_is_independent_of_the_os_list() -> None:
    """Adding Windows must not relax the Electron-shell requirement.

    The two keys answer different questions -- which shell, and which OS -- and
    a future edit that conflates them would either strand Windows users or let
    the companion be opened in a plain browser tab it was never built for.
    """
    platform = _raw()["platform"]
    assert platform["requiresDesktopApp"] is True
    assert "windows" in platform["os"]
