"""The registry-app clone removal on uninstall stays off the loop, under the lock."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.apps.routes as routes_mod
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
    request.app = {"state": state}
    request.json = AsyncMock(return_value={})
    result = MagicMock(ok=True, to_dict=lambda: {"ok": True})

    # The double goes on the module attribute, never the global ``shutil``: a global
    # patch is inherited by pytest's own ``tmp_path`` teardown.
    with (
        patch.object(routes_mod, "shutil", SimpleNamespace(rmtree=_recording_rmtree)),
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
    assert seen_kwargs == [{"ignore_errors": True}], seen_kwargs
    assert len(threads) == 1, threads
    assert (
        threads[0] is not threading.current_thread()
    ), "the registry workspace rmtree ran on the event loop thread"
    assert lock_held == [True], (
        "the lifecycle lock was not held during the removal, so the removal sits "
        "outside it and a queued install can interleave with the tree walk"
    )
    assert not reg_lock.locked(), "the lifecycle lock was not released"
