"""The gateway's unattended managed-venv apply builds BESIDE the live venv.

These tests drive the REAL ``GatewayOrchestrator._auto_apply_wheel_update``
through the REAL ``wheel_apply.run_wheel_apply`` into the REAL
``wheel_engine.apply_wheel_update`` against a managed layout under ``tmp_path``
(``crew-venv`` served through ``crew-venv-current``). Only the network fetches
and the venv build are stand-ins. The build-child kill itself is pinned against
real processes in ``test_wheel_engine.py``.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from wheel_update_test_helpers import wire_wheel_apply

from kiro_crew import platform_compat
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform import wheel_engine
from kiro_crew.platform.wheel_engine import ManagedVenvLayout
from kiro_crew.platform_compat import IS_POSIX
from kiro_crew.slack.gateway import GatewayOrchestrator

# The managed venv is a POSIX install shape (cli.sh), and the layout leans on an
# atomic rename over a symlink, which Windows refuses.
pytestmark = pytest.mark.skipif(not IS_POSIX, reason="the managed venv is POSIX-only")

_VERSION = "9.9.9"


def _served_snapshot(tree: Path) -> tuple[int, list[tuple[str, int]]]:
    """The served tree's identity: its inode and every entry's (path, inode)."""
    entries = sorted((str(path.relative_to(tree)), path.lstat().st_ino) for path in tree.rglob("*"))
    return tree.lstat().st_ino, entries


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ManagedVenvLayout:
    """A legacy managed venv served through the stable link, network stubbed."""
    legacy = tmp_path / "crew-venv"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "bin" / "kirocrew").write_text("#!/bin/sh\n", encoding="utf-8")
    (legacy / "pyvenv.cfg").write_text("", encoding="utf-8")
    (legacy / "lib" / "kiro_crew" / "static" / "dist").mkdir(parents=True)
    (legacy / "lib" / "kiro_crew" / "static" / "dist" / "index.html").write_text(
        "<html></html>", encoding="utf-8"
    )
    stable = tmp_path / "crew-venv-current"
    stable.symlink_to(legacy)
    managed = ManagedVenvLayout(legacy=legacy, stable_link=stable)
    staging = tmp_path / "staging"
    staging.mkdir()

    monkeypatch.setattr(wheel_engine, "managed_venv_layout", lambda: managed)
    monkeypatch.setattr(wheel_engine, "_staging_dir", lambda: staging)
    monkeypatch.setattr(
        wheel_engine,
        "fetch_verified_manifest",
        lambda **_kw: {"version": _VERSION, "sha256": "a" * 64, "python_requires": ">=3.10"},
    )

    def _download(payload: dict[str, str], dest: Path, **_kw: object) -> Path:
        wheel = dest / f"kirocrew-{_VERSION}-py3-none-any.whl"
        wheel.write_bytes(b"wheel")
        return wheel

    monkeypatch.setattr(wheel_engine, "download_verified_wheel", _download)
    monkeypatch.setattr(wheel_engine, "verify_shadow_venv", lambda *_a, **_kw: None)
    monkeypatch.setattr(wheel_engine, "repoint_launcher_symlink", lambda _layout: True)
    # The real engine, behind the real preflight wiring.
    wire_wheel_apply(monkeypatch, apply=wheel_engine.apply_wheel_update)
    monkeypatch.setattr(
        "kiro_crew.platform.wheel_apply.snapshot_memory_before_update", lambda: (1, "")
    )
    return managed


def _orchestrator(monkeypatch: pytest.MonkeyPatch) -> tuple[GatewayOrchestrator, AsyncMock]:
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U1"}):
        orch = GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    orch.dashboard_state = None
    orch.sessions = None
    restart = AsyncMock()
    monkeypatch.setattr(orch, "_restart_after_update", restart)
    return orch, restart


def _claim(shadow: Path) -> None:
    """What the real build does first: an owned, sentinel-marked directory."""
    shadow.mkdir(mode=0o700)
    (shadow / wheel_engine._SHADOW_SENTINEL).write_text("", encoding="utf-8")


@pytest.mark.asyncio
async def test_the_served_venv_is_never_moved_while_the_new_tree_builds(
    layout: ManagedVenvLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.platform.wheel_engine import respawn_executable

    orch, restart = _orchestrator(monkeypatch)
    before = _served_snapshot(layout.legacy)
    during: list[bool] = []

    def _build(wheel: Path, shadow: Path, stable_link: Path | None = None, **_kw: object) -> None:
        # Mid-apply: the served tree and the link to it are exactly as they were.
        during.append(
            _served_snapshot(layout.legacy) == before
            and layout.stable_link.resolve() == layout.legacy.resolve()
        )
        _claim(shadow)
        (shadow / "bin").mkdir()
        (shadow / "bin" / "kirocrew").write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(wheel_engine, "build_shadow_venv", _build)

    await orch._auto_apply_wheel_update("stable", _VERSION)

    assert during == [True], "the served venv moved while the new tree was building"
    assert _served_snapshot(layout.legacy) == before
    promoted = layout.versioned_tree(_VERSION)
    assert layout.stable_link.resolve() == promoted.resolve()
    assert promoted.parent == layout.legacy.parent, "the new tree is a sibling"
    restart.assert_awaited_once_with(respawn_executable)


@pytest.mark.asyncio
async def test_a_gateway_running_through_the_link_restarts_before_it_builds(
    layout: ManagedVenvLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Promoting under it would swap its own modules while a busy restart waits."""
    from kiro_crew.platform import wheel_apply
    from kiro_crew.platform.wheel_engine import respawn_executable

    orch, restart = _orchestrator(monkeypatch)
    monkeypatch.setattr(wheel_apply, "relaunch_before_apply", lambda: True)
    built: list[Path] = []
    monkeypatch.setattr(
        wheel_engine, "build_shadow_venv", lambda _wheel, shadow, *_a, **_kw: built.append(shadow)
    )

    await orch._auto_apply_wheel_update("stable", _VERSION, mandatory=True, mandatory_key="k")

    assert built == [], "nothing is built until the gateway is off the link"
    assert layout.stable_link.resolve() == layout.legacy.resolve()
    restart.assert_awaited_once_with(respawn_executable)
    assert orch._pending_update_mandatory is True, "a floor keeps its grace across retries"
    assert orch._pending_update_mandatory_key == "k"


@pytest.mark.asyncio
async def test_a_cancelled_apply_leaves_the_stable_link_on_the_old_tree(
    layout: ManagedVenvLayout, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    orch, restart = _orchestrator(monkeypatch)
    before = _served_snapshot(layout.legacy)
    building = threading.Event()

    def _build(
        wheel: Path, shadow: Path, stable_link: Path | None = None, *, ctx: object = None
    ) -> None:
        """A build that runs until its apply is cancelled, as a real child would."""
        _claim(shadow)
        cancelled = threading.Event()
        ctx.cancel.on_set(cancelled.set)  # type: ignore[attr-defined]
        building.set()
        assert cancelled.wait(30), "the apply was never cancelled"
        raise wheel_engine.WheelUpdateCancelled("cancelled")

    monkeypatch.setattr(wheel_engine, "build_shadow_venv", _build)

    task = asyncio.create_task(orch._auto_apply_wheel_update("stable", _VERSION))
    try:
        assert await asyncio.to_thread(building.wait, 30), "the build never started"
        task.cancel()  # what the gateway's shutdown does to the update coordinator
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=30)
    finally:
        platform_compat.cancel_wheel_applies_in_flight("shutdown")

    assert layout.stable_link.resolve() == layout.legacy.resolve()
    assert _served_snapshot(layout.legacy) == before
    restart.assert_not_awaited()
    # Only an unpromoted sibling is left, set aside for the next apply's sweep.
    leftovers = sorted(os.listdir(layout.legacy.parent))
    assert leftovers == sorted(
        [
            "crew-venv",
            "crew-venv-current",
            "crew-venv.update.lock",
            "staging",
            f".crew-venv-{_VERSION}.deleting-{os.getpid()}",
        ]
    ), leftovers
