"""Windows-portability regression tests for the ``design-critique`` builtin.

Two call sites decide whether this app works on Windows at all, and each fails
*silently* rather than loudly when it is POSIX-only:

* ``backend.routes._run`` spawned its capture child with a hardcoded
  ``start_new_session=True``. Windows ignores that flag outright, so the node →
  Chromium tree was never put in its own process group and ``kill_and_reap``'s
  ``taskkill /T`` had nothing to reap — a timed-out or cancelled scan leaked a
  headless Chromium per request.
* ``skills/design-critique/scripts/render.mjs`` looked for a browser in three
  POSIX paths only, so the Playwright fallback could never fire on Windows even
  with Chrome or Edge installed.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.design_critique.backend import routes

# .../design_critique/tests/test_windows_portability.py -> parents[1] is the app root.
APP_ROOT = Path(__file__).resolve().parents[1]
RENDER_MJS = APP_ROOT / "skills" / "design-critique" / "scripts" / "render.mjs"


# ---------------------------------------------------------------------------
# routes._run: process-group flags must be chosen per platform
# ---------------------------------------------------------------------------


def _fed_reader(payload: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


class _FakeProc:
    returncode = 0

    def __init__(self) -> None:
        self.stdout = _fed_reader(b"out")
        self.stderr = _fed_reader(b"err")

    async def wait(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_run_spawns_with_platform_correct_process_group_flags(monkeypatch) -> None:
    """The spawn must ask for its own process group in the form the host honours."""
    captured: dict[str, Any] = {}

    async def fake_spawn_argv_async(cmd, *, mode, env, _prepare):
        return list(cmd), {"PATH": ""}, None  # cleanup=None: nothing to unlink

    async def fake_create_subprocess_limited(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(routes.sandbox, "sandboxed_spawn_argv_async", fake_spawn_argv_async)
    monkeypatch.setattr(routes.sandbox, "create_subprocess_limited", fake_create_subprocess_limited)

    rc, out, err = await routes._run(["node", "--version"], 5)

    assert (rc, out, err) == (0, "out", "err")
    kwargs = captured["kwargs"]
    # POSIX gets setsid; Windows gets CREATE_NEW_PROCESS_GROUP instead, because it
    # ignores start_new_session and rejects nothing here silently.
    assert kwargs["start_new_session"] is platform_compat.IS_POSIX
    assert kwargs["creationflags"] == platform_compat.CREATE_NEW_PROCESS_GROUP


def test_run_does_not_hardcode_start_new_session() -> None:
    """Guards the regression directly: the literal is what broke Windows."""
    src = inspect.getsource(routes._run)
    assert "start_new_session=True" not in src
    assert "start_new_session=platform_compat.IS_POSIX" in src
    assert "creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP" in src


def test_process_group_constants_are_safe_on_both_platforms() -> None:
    """Why passing both flags unconditionally is correct.

    ``creationflags`` must stay 0 off Windows — a non-zero value is rejected
    outright there — and must be non-zero on Windows or the flag is a no-op.
    """
    assert platform_compat.IS_POSIX == (not platform_compat.IS_WINDOWS)
    if platform_compat.IS_WINDOWS:
        assert platform_compat.CREATE_NEW_PROCESS_GROUP != 0
    else:
        assert platform_compat.CREATE_NEW_PROCESS_GROUP == 0


# ---------------------------------------------------------------------------
# render.mjs: browser discovery must have a Windows branch
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def render_src() -> str:
    return RENDER_MJS.read_text(encoding="utf-8")


def test_render_script_looks_for_a_browser_on_windows(render_src: str) -> None:
    assert "process.platform === 'win32'" in render_src
    # Installs live under a per-user root or either Program Files root, and Edge
    # counts because it is Chromium and is present on a stock Windows machine.
    for token in ("LOCALAPPDATA", "PROGRAMFILES", "chrome.exe", "msedge.exe"):
        assert token in render_src, f"Windows browser discovery lost {token!r}"


def test_render_script_keeps_the_posix_candidates(render_src: str) -> None:
    # The Windows branch must not have displaced macOS/Linux discovery.
    assert "/Applications/Google Chrome.app" in render_src
    assert "/usr/bin/google-chrome" in render_src


def test_render_script_imports_the_path_helper_it_uses(render_src: str) -> None:
    """Windows candidates are built with path.join — an unimported join is a
    ReferenceError at the exact moment the Playwright fallback is needed, i.e.
    only on the machine that has no Playwright."""
    path_import = next((line for line in render_src.splitlines() if "from 'node:path'" in line), "")
    assert "join" in path_import, f"node:path import does not bring in join: {path_import!r}"
