"""A cancelled app build or lifecycle script must not orphan its child.

The app build (``npm``/``pip``) and the enable/disable/uninstall lifecycle
scripts each spawn their child in its own session and kill the tree only when
their wait times out. A cancel (the gateway shuts down or restarts during a
first-boot default install, or the install task is torn down) ends that wait, so
without a cancel arm the child keeps running with no deadline left: third-party
app code, outside any request. The install script's own cancel arm is covered by
``test_apps_registry_timeout_reaping.py``.

Each test starts the call as a task, waits until the child is being awaited,
cancels it, and asserts the child was handed to ``platform_compat.kill_and_reap``
before the cancel propagated. The helper's own shielding (a second cancel cannot
skip the kill) is covered by ``test_platform_compat_kill_reap.py``.

This file lives in ``test/`` (not ``tests/``) so the ``setup.cfg``
``testpaths = test transfer`` gate, and therefore CI, collects it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from apps_registry_coverage_helpers import _fake_sandbox
from apps_registry_helpers import _catalog_absent  # noqa: F401 - fixture
from apps_registry_helpers import _explicit_registry_execution_admission  # noqa: F401 - fixture
from apps_registry_helpers import _install_script_runs_on_this_host  # noqa: F401 - fixture
from apps_registry_helpers import unsandboxed_spawn  # noqa: F401 - fixture

from kiro_crew import platform_compat
from kiro_crew.apps import lifecycle_scripts, registry
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import await_until


class _RunsUntilKilled:
    """A child whose output never ends until something kills it.

    ``communicate()``, ``wait()`` and the ``stdout`` line iterator all block on
    the same event, so whichever one the call under test awaits, it is still
    waiting when the test cancels it.
    """

    def __init__(self) -> None:
        self.pid = UNALLOCATABLE_PID
        self.returncode: int | None = None
        self.started = asyncio.Event()
        self._killed = asyncio.Event()
        self.stdout = self

    def _die(self) -> None:
        self.returncode = -9
        self._killed.set()

    async def _block(self) -> None:
        self.started.set()
        await self._killed.wait()

    async def communicate(self) -> tuple[bytes, bytes]:
        await self._block()
        return b"", b""

    async def wait(self) -> int:
        await self._block()
        return -9

    def kill(self) -> None:
        self._die()

    def __aiter__(self) -> _RunsUntilKilled:
        return self

    async def __anext__(self) -> bytes:
        await self._block()
        raise StopAsyncIteration


@pytest.fixture
def reaped(monkeypatch) -> list[Any]:
    """Record every child handed to ``kill_and_reap``, and kill it."""
    seen: list[Any] = []

    async def _kill_and_reap(proc, *, timeout=None):  # noqa: ANN001
        seen.append(proc)
        proc._die()

    async def _no_tree_kill(pid, sig):  # noqa: ANN001
        return True

    monkeypatch.setattr(platform_compat, "kill_and_reap", _kill_and_reap)
    monkeypatch.setattr(platform_compat, "kill_process_tree_async", _no_tree_kill)
    return seen


async def _cancel_once_running(task: asyncio.Task, proc: _RunsUntilKilled) -> None:
    await await_until(proc.started, what="the child being awaited")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_until(task, what="the cancelled call finishing")


@pytest.mark.asyncio
async def test_a_cancelled_lifecycle_script_kills_its_child(monkeypatch, tmp_path, reaped):
    (tmp_path / "demo-app").mkdir()
    proc = _RunsUntilKilled()

    async def _spawn(*_argv, **_kwargs):  # noqa: ANN001
        return proc

    monkeypatch.setattr(lifecycle_scripts, "apps_dir", lambda: tmp_path)
    monkeypatch.setattr(lifecycle_scripts, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle_scripts, "wrap_argv", lambda argv, **k: (argv, None))
    monkeypatch.setattr(lifecycle_scripts, "cgroup_scope_argv", lambda argv: argv)
    # The script path is the POSIX `/bin/bash` spawn; native Windows refuses it
    # before spawning, so pin IS_POSIX on every host.
    monkeypatch.setattr(lifecycle_scripts.platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", _spawn)

    task = asyncio.create_task(
        lifecycle_scripts.run_lifecycle_script("demo-app", "sleep 99", timeout=60)
    )
    await _cancel_once_running(task, proc)
    assert reaped == [proc], "a cancelled lifecycle script left its child running"


@pytest.mark.asyncio
async def test_a_cancelled_app_build_kills_its_child(monkeypatch, tmp_path, reaped):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(registry.shutil, "which", lambda name: "/usr/bin/npm")
    proc = _RunsUntilKilled()
    _fake_sandbox(monkeypatch, [proc])

    task = asyncio.create_task(
        registry._run_app_build(
            tmp_path, "demo", [], manifest=AppManifest.from_dict({}), self_managed=False
        )
    )
    await _cancel_once_running(task, proc)
    assert reaped == [proc], "a cancelled app build left its child running"
