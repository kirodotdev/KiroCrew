"""The registry-app clone removal on uninstall stays off the loop, under the lock,
and inside one checkout under app-sources."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dashboard_owner_helpers import owner_claims

import kiro_crew.apps.routes as routes_mod
from kiro_crew import platform_compat
from kiro_crew.apps import registry
from kiro_crew.apps.manager import app_lifecycle_lock

_FAKE_APP = {
    "name": "demo",
    "manifest": {},
    "resources": "gateway",
    "lifecycle": "normal",
    "enabled": False,
    "source": "registry:demo",
}
_NO_DEPS = {"removable": [], "shared": [], "userInstalled": []}


@pytest.mark.asyncio
async def test_registry_workspace_removal_runs_off_the_loop_thread(tmp_path: Path) -> None:
    """The clone removal runs on a worker thread, still holding the lifecycle lock."""
    ws_dir = tmp_path / "app-sources" / "demo"
    ws_dir.mkdir(parents=True)
    (ws_dir / "file.txt").write_text("x", encoding="utf-8")

    threads: list[threading.Thread] = []
    removed: list[Path] = []
    seen_kwargs: list[dict[str, Any]] = []
    lock_held: list[bool] = []
    reg_lock = app_lifecycle_lock("demo")

    def _recording_rmtree(target: Any, **kwargs: Any) -> None:
        threads.append(threading.current_thread())
        seen_kwargs.append(kwargs)
        removed.append(Path(target))
        # Read from the worker: this is the window a queued install would resume in.
        lock_held.append(reg_lock.locked())

    request = MagicMock()
    request.match_info = {"name": "demo"}
    state = MagicMock()
    state.crons = None  # no cron service, so that step is skipped
    state.sessions = None  # no session map, so the pointer step is skipped
    state.owner_id = ""
    request.app = {"state": state}
    owner_claims(request)
    request.json = AsyncMock(return_value={})
    result = MagicMock(ok=True, to_dict=lambda: {"ok": True})

    # The double goes on ``platform_compat.rmtree_force``, the helper the handler calls; the
    # global ``shutil`` stays untouched so pytest's own ``tmp_path`` teardown is not affected.
    with (
        patch.object(routes_mod.platform_compat, "rmtree_force", _recording_rmtree),
        patch.object(routes_mod, "get_app", return_value=_FAKE_APP),
        patch.object(routes_mod, "is_registry_source", return_value=True),
        patch.object(routes_mod, "registry_name_from_source", return_value="demo"),
        patch("kiro_crew.apps.registry.app_source_dir", return_value=ws_dir),
        patch.object(routes_mod, "uninstall_app", return_value=result),
        patch.object(routes_mod, "stop_app_backend", return_value=None),
        patch.object(routes_mod, "deregister_app", return_value=None),
        patch("kiro_crew.apps.teardown.on_app_disable", new_callable=AsyncMock),
        patch.object(routes_mod, "sel", return_value=MagicMock()),
        patch.object(routes_mod, "classify_and_clean_for_uninstall", return_value=_NO_DEPS),
        patch.object(routes_mod, "clean_dependencies", new_callable=AsyncMock, return_value=[]),
    ):
        resp = await routes_mod.handle_uninstall_app(request)

    assert resp.status < 500, f"uninstall returned {resp.status}"
    assert removed == [ws_dir], f"workspace not removed: {removed}"
    assert seen_kwargs == [{}], seen_kwargs
    assert len(threads) == 1, threads
    assert (
        threads[0] is not threading.current_thread()
    ), "the registry workspace rmtree ran on the event loop thread"
    assert lock_held == [True], (
        "the lifecycle lock was not held during the removal, so the removal sits "
        "outside it and a queued install can interleave with the tree walk"
    )
    assert not reg_lock.locked(), "the lifecycle lock was not released"


async def _uninstall_demo_recorded_with(source: str) -> tuple[Any, MagicMock]:
    """Uninstall ``demo`` whose installed record carries *source*.

    Every step before the clone removal is stubbed; the removal itself, the
    source parsing and ``app_source_dir`` run for real. Returns the response and
    the ``uninstall_app`` double.
    """
    request = MagicMock()
    request.match_info = {"name": "demo"}
    state = MagicMock()
    state.crons = None
    state.sessions = None
    state.owner_id = ""
    request.app = {"state": state}
    owner_claims(request)
    request.json = AsyncMock(return_value={})
    result = MagicMock(ok=True, to_dict=lambda: {"ok": True})
    with (
        patch.object(routes_mod, "get_app", return_value={**_FAKE_APP, "source": source}),
        patch.object(routes_mod, "uninstall_app", return_value=result) as uninstall,
        patch.object(routes_mod, "stop_app_backend", return_value=None),
        patch.object(routes_mod, "deregister_app", return_value=None),
        patch("kiro_crew.apps.teardown.on_app_disable", new_callable=AsyncMock),
        patch.object(routes_mod, "sel", return_value=MagicMock()),
        patch.object(routes_mod, "classify_and_clean_for_uninstall", return_value=_NO_DEPS),
        patch.object(routes_mod, "clean_dependencies", new_callable=AsyncMock, return_value=[]),
    ):
        try:
            resp = await routes_mod.handle_uninstall_app(request)
        except ValueError as exc:
            pytest.fail(f"the uninstall raised instead of skipping the clone removal: {exc}")
    return resp, uninstall


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_name", "survivor"),
    [
        pytest.param("../outside", "outside", id="out-of-app-sources"),
        pytest.param(".", "app-sources/other-app", id="the-app-sources-root"),
    ],
)
async def test_a_recorded_name_that_is_not_one_checkout_deletes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stored_name: str,
    survivor: str,
) -> None:
    """``installed.json`` is app-writable, so its ``registry:<name>`` is not trusted
    to name one checkout: a name that leaves app-sources, or names the root and so
    every app's checkout, skips the clone removal with a warning, and the app is
    still uninstalled."""
    sources = tmp_path / "app-sources"
    sources.mkdir()
    keep = tmp_path / survivor / "keep.txt"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(registry, "_app_sources_dir", lambda: sources)
    caplog.set_level(logging.WARNING, logger=routes_mod.logger.name)

    resp, uninstall = await _uninstall_demo_recorded_with(f"registry:{stored_name}")

    assert resp.status == 200, f"uninstall returned {resp.status}"
    assert uninstall.call_count == 1 and uninstall.call_args.args[0] == "demo"
    assert keep.is_file(), "a path outside one checkout was removed"
    assert any(
        rec.name == routes_mod.logger.name
        and rec.levelno == logging.WARNING
        and repr(stored_name) in rec.getMessage()
        for rec in caplog.records
    ), "the skipped removal was not logged"


@pytest.mark.asyncio
async def test_a_link_at_the_slot_is_not_walked_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link sitting at a valid name's slot passes the name check, and the
    removal still does not reach through it: ``rmtree`` refuses a link (or a
    Windows junction) at its root, so the directory it points at survives."""
    sources = tmp_path / "app-sources"
    sources.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    platform_compat.symlink_or_junction(outside, sources / "demo")
    monkeypatch.setattr(registry, "_app_sources_dir", lambda: sources)

    resp, uninstall = await _uninstall_demo_recorded_with("registry:demo")

    assert resp.status == 200, f"uninstall returned {resp.status}"
    assert uninstall.call_count == 1
    assert (outside / "keep.txt").is_file(), "the removal reached through the link"
