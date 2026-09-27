"""cli.json overlay tests: the pre-spawn projection and its fence.

Default projections share their read window while level writes exclude it, and a
pending explicit Default makes startup remove the target model's entry once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from effort_helpers import _observe_directory_wait, _read_cli_overlay
from test_effort import _await_test

from conftest import requires_symlinks
from kiro_crew import platform_compat
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.config import paths as config_paths
from kiro_crew.effort import effort_settings_key
from kiro_crew.providers import acp as acp_provider
from kiro_crew.providers import acp_cli_overlay
from kiro_crew.providers.acp import _KIROCREW_EFFORT_OWNED_KEY, _write_cli_overlay
from kiro_crew.testing.wait import async_wait_until

pytestmark = pytest.mark.usefixtures("no_zero_tool_check")


class TestDefaultSpawnOverlayFence:
    """Default projections share their read window while level writes exclude it."""

    _MODEL = "claude-opus-4.7"
    # A call the fence lets through still does real file I/O in a worker thread, which
    # a loaded Windows runner has taken past 0.2 s. A call the fence wrongly holds waits
    # out a 30 s or 32 s ceiling, so this deadline still tells the two apart.
    _UNBLOCKED_DEADLINE_SECS = 10.0

    def _provider(self, work_dir, *, model=None, **kwargs):
        provider = acp_provider.AcpProvider(
            acp_backend=ACP_BACKEND_KIRO,
            work_dir=work_dir,
            model=model or self._MODEL,
            **kwargs,
        )
        # Each test stubs the runtime start, so the provider keeps the AcpClient it
        # was built with, and a level's post-spawn /effort push would start that
        # client for real and leave its skill projection to the garbage collector.
        provider._client.send_command = AsyncMock(return_value="")
        return provider

    @staticmethod
    async def _until(condition, timeout: float) -> None:
        """Wait for fence state another task reaches only after its key resolves.

        A holder resolves its key in a worker thread before it reaches the fence,
        so one loop turn does not show that another task is waiting yet.
        """
        await async_wait_until(condition, timeout=timeout)

    @staticmethod
    def _observe_write_fence_acquire(monkeypatch) -> tuple[asyncio.Event, asyncio.Event]:
        write_waiting = asyncio.Event()
        write_acquired = asyncio.Event()
        original_acquire = acp_provider._WorkspaceEffortOverlayFence.acquire

        async def observed_acquire(fence, mode, timeout):
            if mode == "write":
                write_waiting.set()
            acquired = await original_acquire(fence, mode, timeout)
            if mode == "write" and acquired:
                write_acquired.set()
            return acquired

        monkeypatch.setattr(acp_provider._WorkspaceEffortOverlayFence, "acquire", observed_acquire)
        return write_waiting, write_acquired

    @pytest.mark.asyncio
    async def test_default_spawn_keeps_a_live_pick_out_of_its_runtime_start(
        self, tmp_path, monkeypatch
    ):
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        default = self._provider(tmp_path)
        sibling = self._provider(tmp_path)
        sibling._client.send_command = AsyncMock(return_value="")
        write_waiting, write_acquired = self._observe_write_fence_acquire(monkeypatch)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_runtime_start():
            entered.set()
            await _await_test(release.wait(), "release")

        monkeypatch.setattr(default, "_start_kiro_runtime", hold_runtime_start)
        start_task = asyncio.create_task(default.start())
        await _await_test(entered.wait(), "entered")
        pick_task = asyncio.create_task(sibling.change_effort("max"))
        await _await_test(write_waiting.wait(), "pick reaching the write fence")
        assert not write_acquired.is_set(), "the write acquired through an open window"

        assert _read_cli_overlay(tmp_path) == {}
        assert not pick_task.done()

        release.set()
        await _await_test(start_task, "start_task")
        assert await _await_test(pick_task, "pick_task") is True
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}

    @pytest.mark.asyncio
    async def test_second_default_spawn_enters_while_first_default_window_is_open(
        self, tmp_path, monkeypatch, caplog
    ):
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        first = self._provider(tmp_path)
        second = self._provider(tmp_path)
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def first_runtime_start():
            first_entered.set()
            await _await_test(release_first.wait(), "release_first")

        async def second_runtime_start():
            second_entered.set()

        monkeypatch.setattr(first, "_start_kiro_runtime", first_runtime_start)
        monkeypatch.setattr(second, "_start_kiro_runtime", second_runtime_start)
        first_task = asyncio.create_task(first.start())
        await _await_test(first_entered.wait(), "first_entered")

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            second_task = asyncio.create_task(second.start())
            await asyncio.wait_for(second_entered.wait(), self._UNBLOCKED_DEADLINE_SECS)

        assert not any("could not be checked or cleared" in r.getMessage() for r in caplog.records)
        release_first.set()
        await _await_test(first_task, "first_task")
        await _await_test(second_task, "second_task")

    @pytest.mark.asyncio
    async def test_a_default_window_arriving_after_a_queued_write_waits_for_it(
        self, tmp_path, monkeypatch
    ):
        # Fairness: a Default start that arrives while a write is queued waits for
        # that write. Back-to-back Default starts otherwise overtake the pick until
        # its ceiling expires.
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        first = self._provider(tmp_path)
        second = self._provider(tmp_path)
        writer = self._provider(tmp_path)
        writer._client.send_command = AsyncMock(return_value="")
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release_first = asyncio.Event()
        write_landed_when_second_entered: list[bool] = []

        async def first_runtime_start():
            first_entered.set()
            await _await_test(release_first.wait(), "release_first")

        async def second_runtime_start():
            write_landed_when_second_entered.append(write_acquired.is_set())
            second_entered.set()

        monkeypatch.setattr(first, "_start_kiro_runtime", first_runtime_start)
        monkeypatch.setattr(second, "_start_kiro_runtime", second_runtime_start)
        first_task = asyncio.create_task(first.start())
        await _await_test(first_entered.wait(), "first_entered")
        write_waiting, write_acquired = self._observe_write_fence_acquire(monkeypatch)
        writer_task = asyncio.create_task(writer.change_effort("max"))
        await asyncio.wait_for(write_waiting.wait(), self._UNBLOCKED_DEADLINE_SECS)
        directory_wait = _observe_directory_wait(monkeypatch)
        second_task = asyncio.create_task(second.start())
        fence = acp_provider._workspace_effort_overlay_fence(
            os.path.realpath(tmp_path / ".kiro" / "settings")
        )
        await _await_test(directory_wait.wait(), "the second start waiting for the directory")
        assert fence._windows == 1
        assert not second_entered.is_set(), "the second window overtook the queued write"
        assert not writer_task.done()

        release_first.set()
        await _await_test(first_task, "first_task")
        assert await _await_test(writer_task, "writer_task") is True
        await asyncio.wait_for(second_entered.wait(), self._UNBLOCKED_DEADLINE_SECS)
        await _await_test(second_task, "second_task")
        assert write_landed_when_second_entered == [True], "the second start overtook the write"

    @pytest.mark.asyncio
    async def test_default_window_is_keyed_by_work_directory(self, tmp_path, monkeypatch):
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        _write_cli_overlay(first_dir, self._MODEL, "max")
        first = self._provider(first_dir)
        other = self._provider(second_dir)
        other._client.send_command = AsyncMock(return_value="")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_runtime_start():
            entered.set()
            await _await_test(release.wait(), "release")

        monkeypatch.setattr(first, "_start_kiro_runtime", hold_runtime_start)
        start_task = asyncio.create_task(first.start())
        await _await_test(entered.wait(), "entered")
        assert (
            await asyncio.wait_for(other.change_effort("max"), self._UNBLOCKED_DEADLINE_SECS)
            is True
        )
        release.set()
        await _await_test(start_task, "start_task")

    @pytest.mark.asyncio
    async def test_auto_spawn_keeps_a_different_models_pick_out_of_runtime_start(
        self, tmp_path, monkeypatch
    ):
        other_model = "claude-sonnet-4.6"
        _write_cli_overlay(tmp_path, other_model, "max")
        default = self._provider(tmp_path, model="auto")
        sibling = self._provider(tmp_path, model=other_model)
        sibling._client.send_command = AsyncMock(return_value="")
        write_waiting, write_acquired = self._observe_write_fence_acquire(monkeypatch)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_runtime_start():
            entered.set()
            await _await_test(release.wait(), "release")

        monkeypatch.setattr(default, "_start_kiro_runtime", hold_runtime_start)
        start_task = asyncio.create_task(default.start())
        await _await_test(entered.wait(), "entered")
        pick_task = asyncio.create_task(sibling.change_effort("max"))
        await _await_test(write_waiting.wait(), "pick reaching the write fence")
        assert not write_acquired.is_set(), "the write acquired through an open window"
        assert _read_cli_overlay(tmp_path) == {}
        assert not pick_task.done()
        release.set()
        await _await_test(start_task, "start_task")
        assert await _await_test(pick_task, "pick_task") is True

    @pytest.mark.asyncio
    async def test_explicit_default_with_workspace_default_waits_for_default_window(
        self, tmp_path, monkeypatch
    ):
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        default = self._provider(tmp_path)
        explicit = self._provider(tmp_path, effort_defaults={self._MODEL: "low"})
        explicit.arm_explicit_effort_default(nullcontext)
        write_waiting, write_acquired = self._observe_write_fence_acquire(monkeypatch)
        entered = asyncio.Event()
        release = asyncio.Event()
        explicit_started = asyncio.Event()

        async def hold_runtime_start():
            entered.set()
            await _await_test(release.wait(), "release")

        async def explicit_runtime_start():
            explicit_started.set()

        monkeypatch.setattr(default, "_start_kiro_runtime", hold_runtime_start)
        monkeypatch.setattr(explicit, "_start_kiro_runtime", explicit_runtime_start)
        default_task = asyncio.create_task(default.start())
        await _await_test(entered.wait(), "entered")
        explicit_task = asyncio.create_task(explicit.start())
        await _await_test(write_waiting.wait(), "explicit default reaching the write fence")
        assert not write_acquired.is_set(), "the write acquired through an open window"
        assert not explicit_started.is_set()
        assert _read_cli_overlay(tmp_path) == {}
        release.set()
        await _await_test(default_task, "default_task")
        await _await_test(explicit_task, "explicit_task")
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "low"}

    @pytest.mark.asyncio
    async def test_level_projection_waits_for_default_window_then_reasserts_live_level(
        self, tmp_path, monkeypatch
    ):
        default = self._provider(tmp_path)
        level = self._provider(tmp_path, effort_per_model={self._MODEL: "max"})
        level._client.send_command = AsyncMock(return_value="")
        write_waiting, write_acquired = self._observe_write_fence_acquire(monkeypatch)
        entered = asyncio.Event()
        release = asyncio.Event()
        level_started = asyncio.Event()

        async def hold_runtime_start():
            entered.set()
            await _await_test(release.wait(), "release")

        async def level_runtime_start():
            level_started.set()

        monkeypatch.setattr(default, "_start_kiro_runtime", hold_runtime_start)
        monkeypatch.setattr(level, "_start_kiro_runtime", level_runtime_start)
        default_task = asyncio.create_task(default.start())
        await _await_test(entered.wait(), "entered")
        level_task = asyncio.create_task(level.start())
        await _await_test(write_waiting.wait(), "level projection reaching the write fence")
        assert not write_acquired.is_set(), "the write acquired through an open window"
        assert not level_started.is_set()
        assert _read_cli_overlay(tmp_path) == {}
        release.set()
        await _await_test(default_task, "default_task")
        await _await_test(level_task, "level_task")
        level._client.send_command.assert_awaited_once_with("/effort", args={"level": "max"})

    @pytest.mark.asyncio
    async def test_level_spawn_releases_write_fence_before_runtime_start(
        self, tmp_path, monkeypatch
    ):
        level = self._provider(tmp_path, effort_per_model={self._MODEL: "max"})
        sibling = self._provider(tmp_path)
        level._client.send_command = AsyncMock(return_value="")
        sibling._client.send_command = AsyncMock(return_value="")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_runtime_start():
            entered.set()
            await _await_test(release.wait(), "release")

        monkeypatch.setattr(level, "_start_kiro_runtime", hold_runtime_start)
        level_task = asyncio.create_task(level.start())
        await _await_test(entered.wait(), "entered")
        assert (
            await asyncio.wait_for(sibling.change_effort("high"), self._UNBLOCKED_DEADLINE_SECS)
            is True
        )
        release.set()
        await _await_test(level_task, "level_task")

    @pytest.mark.asyncio
    async def test_expired_window_fails_before_tool_search_or_runtime(self, tmp_path, monkeypatch):
        holder = self._provider(tmp_path, effort_per_model={self._MODEL: "max"})
        waiter = self._provider(tmp_path)
        projection_started = threading.Event()
        release_projection = threading.Event()
        runtime_started = asyncio.Event()
        tool_search_calls = []

        def hold_level_projection():
            projection_started.set()
            assert release_projection.wait(timeout=5)
            return True

        def apply_tool_search_overlay():
            tool_search_calls.append(True)
            return True

        async def waiting_runtime_start():
            runtime_started.set()

        monkeypatch.setattr(holder, "_apply_effort_overlay", hold_level_projection)
        monkeypatch.setattr(holder, "_start_kiro_runtime", AsyncMock())
        monkeypatch.setattr(waiter, "_apply_tool_search_overlay", apply_tool_search_overlay)
        monkeypatch.setattr(waiter, "_start_kiro_runtime", waiting_runtime_start)
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_TIMEOUT_SECS", 0.01)
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS", 0.02)
        monkeypatch.setattr(
            acp_provider,
            "_WORKSPACE_EFFORT_OVERLAY_FENCE_WINDOW_TIMEOUT_SECS",
            0.03,
            raising=False,
        )
        holder_task = asyncio.create_task(holder.start())
        await async_wait_until(
            projection_started.is_set, describe=lambda: "projection_started was never set"
        )

        with pytest.raises(RuntimeError, match="Default effort overlay fence wait timed out"):
            await waiter.start()

        assert tool_search_calls == []
        assert not runtime_started.is_set()
        release_projection.set()
        await _await_test(holder_task, "holder_task")

    @pytest.mark.asyncio
    async def test_cancelled_window_releases_after_its_overlay_worker_settles(
        self, tmp_path, monkeypatch
    ):
        provider = self._provider(tmp_path)
        sibling = self._provider(tmp_path)
        sibling._client.send_command = AsyncMock(return_value="")
        worker_started = threading.Event()
        release_worker = threading.Event()

        def blocked_overlay():
            worker_started.set()
            assert release_worker.wait(timeout=5)
            return True

        monkeypatch.setattr(provider, "_apply_effort_overlay", blocked_overlay)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
        start_task = asyncio.create_task(provider.start())
        await async_wait_until(
            worker_started.is_set, describe=lambda: "worker_started was never set"
        )
        start_task.cancel()
        await asyncio.sleep(0)
        pick_task = asyncio.create_task(sibling.change_effort("max"))
        await asyncio.sleep(0)
        assert not pick_task.done()
        release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await _await_test(start_task, "start_task")
        assert await _await_test(pick_task, "pick_task") is True

    @pytest.mark.asyncio
    async def test_cancelled_waiting_start_does_not_block_next_write(self, tmp_path, monkeypatch):
        holder = self._provider(tmp_path, effort_per_model={self._MODEL: "max"})
        waiter = self._provider(tmp_path)
        writer = self._provider(tmp_path)
        writer._client.send_command = AsyncMock(return_value="")
        projection_started = threading.Event()
        release_projection = threading.Event()

        def hold_level_projection():
            projection_started.set()
            assert release_projection.wait(timeout=5)
            return True

        monkeypatch.setattr(holder, "_apply_effort_overlay", hold_level_projection)
        monkeypatch.setattr(holder, "_start_kiro_runtime", AsyncMock())
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_TIMEOUT_SECS", 0.01)
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS", 0.02)
        monkeypatch.setattr(
            acp_provider,
            "_WORKSPACE_EFFORT_OVERLAY_FENCE_WINDOW_TIMEOUT_SECS",
            self._UNBLOCKED_DEADLINE_SECS,
            raising=False,
        )
        holder_task = asyncio.create_task(holder.start())
        await async_wait_until(
            projection_started.is_set, describe=lambda: "projection_started was never set"
        )
        fence = acp_provider._workspace_effort_overlay_fence(
            os.path.realpath(tmp_path / ".kiro" / "settings")
        )
        directory_wait = _observe_directory_wait(monkeypatch)
        waiter_task = asyncio.create_task(waiter.start())
        await _await_test(directory_wait.wait(), "the waiter waiting for the directory")
        assert fence._pending_windows == 0, "a waiting start registered on the fence"
        waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await _await_test(waiter_task, "waiter_task")

        release_projection.set()
        await _await_test(holder_task, "holder_task")
        assert (
            await asyncio.wait_for(writer.change_effort("high"), self._UNBLOCKED_DEADLINE_SECS)
            is True
        )

    @pytest.mark.asyncio
    async def test_pick_fence_expiry_raises_without_writing(self, tmp_path, monkeypatch):
        _write_cli_overlay(tmp_path, self._MODEL, "max")
        holder = self._provider(tmp_path)
        picker = self._provider(tmp_path)
        picker._client.send_command = AsyncMock(return_value="")
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def hold_runtime_start():
            holder_entered.set()
            await _await_test(release_holder.wait(), "release_holder")

        monkeypatch.setattr(holder, "_start_kiro_runtime", hold_runtime_start)
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS", 0.01)
        holder_task = asyncio.create_task(holder.start())
        await _await_test(holder_entered.wait(), "holder_entered")

        with pytest.raises(
            RuntimeError, match="could not persist effort.*workspace overlay is locked"
        ):
            await picker.change_effort("max")
        assert _read_cli_overlay(tmp_path) == {}
        assert picker._effort_per_model == {}
        release_holder.set()
        await _await_test(holder_task, "holder_task")

    @pytest.mark.asyncio
    async def test_workspace_default_clear_fence_expiry_returns_none_without_writing(
        self, tmp_path, monkeypatch
    ):
        _write_cli_overlay(tmp_path, self._MODEL, "high")
        holder = self._provider(tmp_path)
        clearer = self._provider(
            tmp_path,
            effort_per_model={self._MODEL: "high"},
            effort_defaults={self._MODEL: "low"},
        )
        clearer._client.send_command = AsyncMock(return_value="")
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def hold_runtime_start():
            holder_entered.set()
            await _await_test(release_holder.wait(), "release_holder")

        monkeypatch.setattr(holder, "_start_kiro_runtime", hold_runtime_start)
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS", 0.01)
        holder_task = asyncio.create_task(holder.start())
        await _await_test(holder_entered.wait(), "holder_entered")

        assert await clearer.clear_effort(owned_only=False) is None
        assert _read_cli_overlay(tmp_path) == {}
        assert clearer._effort_per_model == {self._MODEL: "high"}
        clearer._client.send_command.assert_not_awaited()
        release_holder.set()
        await _await_test(holder_task, "holder_task")

    @pytest.mark.asyncio
    async def test_builtin_default_clear_fence_expiry_returns_none_without_writing(
        self, tmp_path, monkeypatch
    ):
        _write_cli_overlay(tmp_path, self._MODEL, "high")
        holder = self._provider(tmp_path)
        clearer = self._provider(tmp_path, effort_per_model={self._MODEL: "high"})
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()

        async def hold_runtime_start():
            holder_entered.set()
            await _await_test(release_holder.wait(), "release_holder")

        monkeypatch.setattr(holder, "_start_kiro_runtime", hold_runtime_start)
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS", 0.01)
        holder_task = asyncio.create_task(holder.start())
        await _await_test(holder_entered.wait(), "holder_entered")

        assert await clearer.clear_effort(owned_only=False) is None
        assert _read_cli_overlay(tmp_path) == {}
        assert clearer._effort_per_model == {self._MODEL: "high"}
        release_holder.set()
        await _await_test(holder_task, "holder_task")

    @pytest.mark.asyncio
    async def test_live_workspace_default_clear_waits_for_a_default_window(
        self, tmp_path, monkeypatch
    ):
        _write_cli_overlay(tmp_path, self._MODEL, "high")
        holder = self._provider(tmp_path)
        clearer = self._provider(
            tmp_path,
            effort_per_model={self._MODEL: "high"},
            effort_defaults={self._MODEL: "low"},
        )
        clearer._client.send_command = AsyncMock(return_value="")
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()
        clear_write_started = threading.Event()
        original_apply = clearer._apply_effort_overlay
        write_waiting, write_acquired = self._observe_write_fence_acquire(monkeypatch)

        async def hold_runtime_start():
            holder_entered.set()
            await _await_test(release_holder.wait(), "release_holder")

        def observed_clear_write(**kwargs):
            clear_write_started.set()
            return original_apply(**kwargs)

        monkeypatch.setattr(holder, "_start_kiro_runtime", hold_runtime_start)
        monkeypatch.setattr(clearer, "_apply_effort_overlay", observed_clear_write)
        holder_task = asyncio.create_task(holder.start())
        await _await_test(holder_entered.wait(), "holder_entered")
        clear_task = asyncio.create_task(clearer.clear_effort(owned_only=False))
        await _await_test(write_waiting.wait(), "clear reaching the write fence")
        assert not write_acquired.is_set(), "the write acquired through an open window"
        assert not clear_write_started.is_set()
        assert _read_cli_overlay(tmp_path) == {}

        release_holder.set()
        await _await_test(holder_task, "holder_task")
        assert await _await_test(clear_task, "clear_task") is True
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "low"}

    @pytest.mark.asyncio
    async def test_sequential_writes_release_the_fence_and_default_start_enters(
        self, tmp_path, monkeypatch
    ):
        first = self._provider(tmp_path)
        second = self._provider(tmp_path)
        default = self._provider(tmp_path)
        first._client.send_command = AsyncMock(return_value="")
        second._client.send_command = AsyncMock(return_value="")
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS", 0.01)
        monkeypatch.setattr(default, "_start_kiro_runtime", AsyncMock())

        assert await first.change_effort("max") is True
        assert await second.change_effort("high") is True
        await asyncio.wait_for(default.start(), self._UNBLOCKED_DEADLINE_SECS)
        assert _read_cli_overlay(tmp_path) == {}

    @pytest.mark.asyncio
    async def test_symlinked_alias_shares_one_fence(self, tmp_path):
        real_work_dir = tmp_path / "real"
        alias_work_dir = tmp_path / "alias"
        real_work_dir.mkdir()
        try:
            platform_compat.symlink_or_junction(real_work_dir, alias_work_dir)
        except OSError as exc:
            pytest.fail(f"platform cannot create a directory symlink: {exc}")
        real_provider = self._provider(real_work_dir)
        alias_provider = self._provider(alias_work_dir)

        async with acp_provider._hold_workspace_effort_overlay_fence(
            real_provider._client._work_dir, "write", self._UNBLOCKED_DEADLINE_SECS
        ) as acquired:
            assert acquired is True
            async with acp_provider._hold_workspace_effort_overlay_fence(
                alias_provider._client._work_dir, "write", self._UNBLOCKED_DEADLINE_SECS
            ) as alias_acquired:
                assert alias_acquired is False

    @pytest.mark.asyncio
    async def test_work_dirs_sharing_one_settings_folder_share_one_fence(self, tmp_path):
        outer_work_dir = tmp_path / "outer"
        inner_work_dir = outer_work_dir / "sub"
        (inner_work_dir / ".kiro" / "settings").mkdir(parents=True)
        try:
            platform_compat.symlink_or_junction(inner_work_dir / ".kiro", outer_work_dir / ".kiro")
        except OSError as exc:
            pytest.fail(f"platform cannot create a directory symlink: {exc}")
        outer_provider = self._provider(outer_work_dir)
        inner_provider = self._provider(inner_work_dir)

        async with acp_provider._hold_workspace_effort_overlay_fence(
            outer_provider._client._work_dir, "write", self._UNBLOCKED_DEADLINE_SECS
        ) as acquired:
            assert acquired is True
            async with acp_provider._hold_workspace_effort_overlay_fence(
                inner_provider._client._work_dir, "write", self._UNBLOCKED_DEADLINE_SECS
            ) as inner_acquired:
                assert inner_acquired is False

    @pytest.mark.asyncio
    async def test_default_window_through_symlink_blocks_real_work_directory_write(self, tmp_path):
        real_work_dir = tmp_path / "real"
        alias_work_dir = tmp_path / "alias"
        real_work_dir.mkdir()
        try:
            platform_compat.symlink_or_junction(real_work_dir, alias_work_dir)
        except OSError as exc:
            pytest.fail(f"platform cannot create a directory symlink: {exc}")

        async with acp_provider._hold_workspace_effort_overlay_fence(alias_work_dir, "window", 0.1):
            async with acp_provider._hold_workspace_effort_overlay_fence(
                real_work_dir, "write", 0.01
            ) as acquired:
                assert acquired is False

    @pytest.mark.asyncio
    async def test_fence_key_is_resolved_before_the_registry_lookup(self, tmp_path, monkeypatch):
        original_to_thread = asyncio.to_thread

        async def yielding_to_thread(function, *args, **kwargs):
            if function is acp_provider.workspace_cli_settings_fence_key:
                await asyncio.sleep(0)
            return await original_to_thread(function, *args, **kwargs)

        monkeypatch.setattr(acp_provider.asyncio, "to_thread", yielding_to_thread)
        original_acquire = acp_provider._WorkspaceEffortOverlayFence.acquire

        async def nonwaiting_write_acquire(fence, mode, timeout):
            if mode == "write" and fence._write_held:
                return False
            return await original_acquire(fence, mode, timeout)

        monkeypatch.setattr(
            acp_provider._WorkspaceEffortOverlayFence, "acquire", nonwaiting_write_acquire
        )
        release = asyncio.Event()
        outcomes_ready = asyncio.Event()
        outcomes: list[bool] = []

        async def hold_until_released():
            async with acp_provider._hold_workspace_effort_overlay_fence(
                tmp_path, "write", self._UNBLOCKED_DEADLINE_SECS
            ) as acquired:
                outcomes.append(acquired)
                if len(outcomes) == 2:
                    outcomes_ready.set()
                if acquired:
                    await _await_test(release.wait(), "release")

        first = asyncio.create_task(hold_until_released())
        second = asyncio.create_task(hold_until_released())
        await asyncio.wait_for(outcomes_ready.wait(), self._UNBLOCKED_DEADLINE_SECS)
        release.set()
        await _await_test(asyncio.gather(first, second), "task cleanup")

        assert sorted(outcomes) == [False, True]

    @pytest.mark.asyncio
    async def test_fence_resolution_failure_fails_the_start_it_cannot_key(
        self, tmp_path, monkeypatch
    ):
        provider = self._provider(tmp_path)
        original_realpath = os.path.realpath

        def failing_realpath(_path):
            raise OSError("unavailable mount")

        monkeypatch.setattr(acp_provider.os.path, "realpath", failing_realpath)
        with pytest.raises(OSError, match="unavailable mount"):
            await provider.start()

        monkeypatch.setattr(acp_provider.os.path, "realpath", original_realpath)
        async with acp_provider._hold_workspace_effort_overlay_fence(
            tmp_path, "write", self._UNBLOCKED_DEADLINE_SECS
        ) as acquired:
            assert acquired is True

    @pytest.mark.asyncio
    async def test_fence_resolution_refusal_uses_the_named_settings_fallback(
        self, tmp_path, monkeypatch
    ):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        platform_compat.symlink_or_junction(outside, work_dir / ".kiro")
        provider = self._provider(work_dir)
        monkeypatch.setattr(provider, "_apply_effort_overlay", lambda: True)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
        # The managed-agent shadow check reads <cwd>/.kiro/agents through the link on
        # purpose: kiro-cli loads that directory, so the check has to see what it would
        # load. This test is about the settings folder and the fence, so it stubs the
        # check out rather than recording its reads.
        monkeypatch.setattr(
            "kiro_crew.acp.managed_agent_shadow.managed_agent_shadow_refusal",
            lambda agent, work_dir: None,
        )

        work_dir_real = os.path.realpath(work_dir)
        outside_root = os.path.normcase(os.path.abspath(outside))
        calls = []

        def record(name, original):
            def wrapped(path, *args, **kwargs):
                try:
                    text = os.fsdecode(os.fspath(path))
                except TypeError:
                    text = repr(path)
                calls.append((name, text))
                return original(path, *args, **kwargs)

            return wrapped

        with monkeypatch.context() as probes:
            probes.setattr(os, "lstat", record("lstat", os.lstat))
            probes.setattr(os, "stat", record("stat", os.stat))
            probes.setattr(os, "open", record("open", os.open))
            probes.setattr(os, "readlink", record("readlink", os.readlink))
            probes.setattr(os, "scandir", record("scandir", os.scandir))
            probes.setattr(os, "listdir", record("listdir", os.listdir))
            probes.setattr(os.path, "realpath", record("realpath", os.path.realpath))

            assert acp_provider.workspace_cli_settings_fence_key(work_dir) == os.path.join(
                work_dir_real, ".kiro", "settings"
            )
            await provider.start()

        traversed = []
        for operation, raw in calls:
            if not os.path.isabs(raw):
                continue
            candidate = os.path.normcase(os.path.abspath(raw))
            if candidate == outside_root or candidate.startswith(outside_root + os.path.sep):
                traversed.append((operation, raw))
        assert traversed == []
        provider._start_kiro_runtime.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancelled_fence_resolution_acquires_nothing(self, tmp_path, monkeypatch):
        provider = self._provider(tmp_path)
        original_fence_key = acp_provider.workspace_cli_settings_fence_key
        resolution_started = threading.Event()
        release_resolution = threading.Event()

        def blocked_fence_key(path):
            resolution_started.set()
            assert release_resolution.wait(self._UNBLOCKED_DEADLINE_SECS)
            return original_fence_key(path)

        monkeypatch.setattr(acp_provider, "workspace_cli_settings_fence_key", blocked_fence_key)
        start_task = asyncio.create_task(provider.start())
        await async_wait_until(
            resolution_started.is_set,
            timeout=self._UNBLOCKED_DEADLINE_SECS,
            describe=lambda: "resolution_started was never set",
        )
        start_task.cancel()
        release_resolution.set()
        with pytest.raises(asyncio.CancelledError):
            await _await_test(start_task, "start_task")

        monkeypatch.setattr(acp_provider, "workspace_cli_settings_fence_key", original_fence_key)
        async with acp_provider._hold_workspace_effort_overlay_fence(
            tmp_path, "write", self._UNBLOCKED_DEADLINE_SECS
        ) as acquired:
            assert acquired is True

    @pytest.mark.asyncio
    async def test_default_spawn_waits_for_level_projection_then_reads_a_cleared_overlay(
        self, tmp_path, monkeypatch
    ):
        level = self._provider(tmp_path, effort_per_model={self._MODEL: "max"})
        default = self._provider(tmp_path)
        worker_started = threading.Event()
        release_worker = threading.Event()
        default_entered = asyncio.Event()
        snapshot_at_runtime = {}

        def blocked_level_projection():
            worker_started.set()
            assert release_worker.wait(timeout=5)
            return _write_cli_overlay(tmp_path, self._MODEL, "max")

        async def snapshot_runtime_start():
            snapshot_at_runtime.update(_read_cli_overlay(tmp_path))
            default_entered.set()

        monkeypatch.setattr(level, "_apply_effort_overlay", blocked_level_projection)
        monkeypatch.setattr(level, "_start_kiro_runtime", AsyncMock())
        monkeypatch.setattr(default, "_start_kiro_runtime", snapshot_runtime_start)
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_TIMEOUT_SECS", 0.01)
        monkeypatch.setattr(acp_provider, "CLI_SETTINGS_LOCK_ACTION_TIMEOUT_SECS", 0.02)
        monkeypatch.setattr(
            acp_provider,
            "_WORKSPACE_EFFORT_OVERLAY_FENCE_WINDOW_TIMEOUT_SECS",
            0.5,
            raising=False,
        )
        level_task = asyncio.create_task(level.start())
        await async_wait_until(
            worker_started.is_set, describe=lambda: "worker_started was never set"
        )
        fence = acp_provider._workspace_effort_overlay_fence(
            os.path.realpath(tmp_path / ".kiro" / "settings")
        )
        directory_wait = _observe_directory_wait(monkeypatch)
        default_task = asyncio.create_task(default.start())
        await _await_test(directory_wait.wait(), "the Default start waiting for the directory")
        assert fence._write_held
        assert not default_entered.is_set()
        release_worker.set()
        await _await_test(level_task, "level_task")
        await _await_test(default_task, "default_task")
        assert snapshot_at_runtime == {}

    @pytest.mark.asyncio
    async def test_pending_window_blocks_a_write_after_the_held_write_releases(self, tmp_path):
        fence = acp_provider._workspace_effort_overlay_fence(
            os.path.realpath(tmp_path / ".kiro" / "settings")
        )
        assert await fence.acquire("write", 0.1) is True
        window_task = asyncio.create_task(fence.acquire("window", self._UNBLOCKED_DEADLINE_SECS))
        await asyncio.sleep(0)
        assert fence._pending_windows == 1

        assert await fence.acquire("write", 0.01) is False

        fence.release("write")
        assert await _await_test(window_task, "window_task") is True
        fence.release("window")

    @pytest.mark.asyncio
    async def test_a_write_arriving_while_a_default_start_waits_lands_before_it(
        self, tmp_path, monkeypatch
    ):
        # A start that is only waiting for its directory holds nothing off, so a
        # write arriving then goes first. Once the start holds its window, a later
        # write waits for its runtime start.
        holder = self._provider(tmp_path, effort_per_model={self._MODEL: "max"})
        default = self._provider(tmp_path)
        writer = self._provider(tmp_path)
        writer._client.send_command = AsyncMock(return_value="")
        late_writer = self._provider(tmp_path)
        late_writer._client.send_command = AsyncMock(return_value="")
        projection_started = threading.Event()
        release_projection = threading.Event()
        default_started = asyncio.Event()
        release_default = asyncio.Event()
        write_landed_when_default_started: list[bool] = []

        def hold_level_projection():
            projection_started.set()
            assert release_projection.wait(timeout=5)
            return True

        async def hold_default_runtime_start():
            write_landed_when_default_started.append(write_acquired.is_set())
            default_started.set()
            await _await_test(release_default.wait(), "release_default")

        monkeypatch.setattr(holder, "_apply_effort_overlay", hold_level_projection)
        monkeypatch.setattr(holder, "_start_kiro_runtime", AsyncMock())
        monkeypatch.setattr(default, "_start_kiro_runtime", hold_default_runtime_start)
        holder_task = asyncio.create_task(holder.start())
        await async_wait_until(
            projection_started.is_set, describe=lambda: "projection_started was never set"
        )
        write_waiting, write_acquired = self._observe_write_fence_acquire(monkeypatch)
        directory_wait = _observe_directory_wait(monkeypatch)
        default_task = asyncio.create_task(default.start())
        await _await_test(directory_wait.wait(), "the Default start waiting for the directory")
        writer_task = asyncio.create_task(writer.change_effort("high"))
        await _await_test(write_waiting.wait(), "the write reaching the fence")
        release_projection.set()
        await _await_test(holder_task, "holder_task")
        await _await_test(default_started.wait(), "default_started")
        assert write_landed_when_default_started == [True], "the waiting start held off the write"
        assert await _await_test(writer_task, "writer_task") is True

        late_waiting, late_acquired = self._observe_write_fence_acquire(monkeypatch)
        late_task = asyncio.create_task(late_writer.change_effort("high"))
        await _await_test(late_waiting.wait(), "the late write reaching the fence")
        assert not late_acquired.is_set(), "the late write acquired through a held window"
        release_default.set()
        await _await_test(default_task, "default_task")
        assert await _await_test(late_task, "late_task") is True

    @pytest.mark.asyncio
    async def test_second_cancellation_while_waiting_for_overlay_worker_releases_window(
        self, tmp_path, monkeypatch
    ):
        provider = self._provider(tmp_path)
        sibling = self._provider(tmp_path)
        sibling._client.send_command = AsyncMock(return_value="")
        worker_started = threading.Event()
        release_worker = threading.Event()

        def blocked_overlay():
            worker_started.set()
            assert release_worker.wait(timeout=5)
            return True

        monkeypatch.setattr(provider, "_apply_effort_overlay", blocked_overlay)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
        start_task = asyncio.create_task(provider.start())
        await async_wait_until(
            worker_started.is_set, describe=lambda: "worker_started was never set"
        )
        start_task.cancel()
        await asyncio.sleep(0)
        start_task.cancel()
        release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await _await_test(start_task, "start_task")

        assert (
            await asyncio.wait_for(sibling.change_effort("max"), self._UNBLOCKED_DEADLINE_SECS)
            is True
        )


class TestExplicitDefaultColdStart:
    """A pending explicit Default makes startup remove the target model's entry once.

    The pending projection does to ``cli.json`` what ``clear_effort(owned_only=False)``
    does on a live provider with the same backend, model and resolved level, and
    the provider reports it applied only when that projection succeeded.
    """

    _MODEL = "claude-opus-4.7"

    def _provider(self, work_dir, *, model=None, **kwargs):
        return acp_provider.AcpProvider(
            acp_backend=kwargs.pop("acp_backend", ACP_BACKEND_KIRO),
            work_dir=work_dir,
            model=model or self._MODEL,
            **kwargs,
        )

    @staticmethod
    def _write(work_dir, data):
        settings = work_dir / ".kiro" / "settings"
        settings.mkdir(parents=True, exist_ok=True)
        cli_json = settings / "cli.json"
        cli_json.write_text(json.dumps(data), encoding="utf-8")
        return cli_json

    def _operator_entry(self, work_dir, level, model=None):
        model = model or self._MODEL
        entry = {effort_settings_key(model): {"effort": level}}
        return self._write(work_dir, {"chat.modelDefaults": {model: entry}})

    @staticmethod
    def _seed_sessions_dir(tmp_path, monkeypatch, session_ids):
        """Redirect the kiro-cli transcript directory and seed a pair per session id."""
        sessions = tmp_path / "kiro-sessions"
        sessions.mkdir()
        monkeypatch.setattr(config_paths, "_sessions_dir_override", lambda: sessions)
        for sid in session_ids:
            for suffix in (".json", ".jsonl"):
                (sessions / f"{sid}{suffix}").write_text("{}", encoding="utf-8")
        return sessions

    @staticmethod
    def _session_files_present(sessions, sid):
        return [(sessions / f"{sid}{suffix}").exists() for suffix in (".json", ".jsonl")]

    @staticmethod
    def _live_client(work_dir, model, session_id, *, push_error=None, backend=ACP_BACKEND_KIRO):
        alive = [True]

        async def kill(**_kwargs):
            alive[0] = False

        handle = SimpleNamespace(
            session_id=session_id,
            model="auto",
            served_model=model,
            memory_mode="persistent",
            keep_transcript=False,
            _cancel_hook_tasks=MagicMock(),
            destroy=AsyncMock(),
            send_command=AsyncMock(
                side_effect=push_error,
                return_value="" if push_error is None else None,
            ),
        )
        runtime = SimpleNamespace(
            acp_backend=backend,
            _work_dir=work_dir,
            is_alive=lambda: alive[0],
            kill=AsyncMock(side_effect=kill),
            work_scratch_dir=None,
        )
        return AcpSessionProvider(handle, runtime, owns_runtime=True), handle, runtime

    def _stub_client_start(self, provider, monkeypatch, model, snapshots):
        """Stub the AcpClient start path, the one a harness off the shared runtime takes."""
        client = provider._client

        async def ensure_ready():
            snapshots.append(_read_cli_overlay(client._work_dir))
            client._resolved_model_id = model

        monkeypatch.setattr(client, "ensure_ready", ensure_ready)
        monkeypatch.setattr(client, "confirm_member_projection", AsyncMock())

    def _stub_runtime_starts(
        self,
        provider,
        monkeypatch,
        models,
        snapshots,
        clients,
        resumed_flags=None,
        replay_flags=None,
        replay_flag_snapshots=None,
        backend=ACP_BACKEND_KIRO,
        teardown_error=None,
        runtime_stays_alive=False,
    ):
        async def start_runtime():
            snapshots.append(_read_cli_overlay(provider._client._work_dir))
            index = len(snapshots) - 1
            if replay_flag_snapshots is not None:
                replay_flag_snapshots.append(
                    (
                        provider._history_replay_needed,
                        provider._defer_replay_sid_promotion,
                    )
                )
            if replay_flags is not None and replay_flags[index] is not None:
                (
                    provider._history_replay_needed,
                    provider._defer_replay_sid_promotion,
                ) = replay_flags[index]
            if index:
                assert clients[-1][2].is_alive() is False
            client, handle, runtime = self._live_client(
                provider._client._work_dir,
                models[index],
                f"session-{index + 1}",
                backend=backend,
            )
            if index == 0 and teardown_error is not None:
                client.shutdown = AsyncMock(side_effect=teardown_error)
            if index == 0 and runtime_stays_alive:
                runtime.kill = AsyncMock()
            if resumed_flags is not None:
                client.resumed = resumed_flags[index]
            clients.append((client, handle, runtime, provider._client._resume_session_id))
            provider._client = client

        monkeypatch.setattr(provider, "_start_kiro_runtime", start_runtime)
        monkeypatch.setattr(provider, "_note_zero_tool_servers", AsyncMock())

    @pytest.mark.asyncio
    async def test_unpinned_start_restarts_after_clearing_only_the_served_model(
        self, tmp_path, monkeypatch
    ):
        other_model = "claude-sonnet-4.6"
        self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    self._MODEL: {"output_config": {"effort": "max"}},
                    other_model: {"output_config": {"effort": "high"}},
                }
            },
        )
        provider = self._provider(tmp_path, model="auto")
        sessions = self._seed_sessions_dir(tmp_path, monkeypatch, ["session-1", "session-2"])
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider, monkeypatch, [self._MODEL, self._MODEL], snapshots, clients
        )
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert snapshots == [
            {self._MODEL: "max", other_model: "high"},
            {other_model: "high"},
        ]
        assert _read_cli_overlay(tmp_path) == {other_model: "high"}
        assert len(clients) == 2
        first_client, first_handle, first_runtime, _ = clients[0]
        assert provider._client is clients[1][0]
        first_runtime.kill.assert_awaited_once_with(expected=True, reason="provider shutdown")
        first_handle.destroy.assert_not_awaited()
        assert clients[1][3] == ""
        assert self._session_files_present(sessions, "session-1") == [False, False]
        assert self._session_files_present(sessions, "session-2") == [True, True]
        assert provider.explicit_effort_default_applied is True
        assert provider._explicit_effort_default_pending is False

    @pytest.mark.asyncio
    async def test_unpinned_restart_keeps_the_first_session_that_is_the_resume_target(
        self, tmp_path, monkeypatch
    ):
        """A first session the chat can still resume is never deleted by the restart."""
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        provider._client._resume_session_id = "session-1"
        sessions = self._seed_sessions_dir(tmp_path, monkeypatch, ["session-1", "session-2"])
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider, monkeypatch, [self._MODEL, self._MODEL], snapshots, clients
        )
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert len(clients) == 2
        assert clients[1][3] == "session-1"
        assert self._session_files_present(sessions, "session-1") == [True, True]
        assert self._session_files_present(sessions, "session-2") == [True, True]
        assert provider.explicit_effort_default_applied is True

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KAS, "claude"], ids=["kas", "claude"])
    @pytest.mark.asyncio
    async def test_unpinned_default_leaves_the_overlay_alone_off_the_reader_set(
        self, tmp_path, monkeypatch, backend
    ):
        # A harness that never opens cli.json must not snapshot or rewrite the
        # entry a kiro-cli chat in the same work dir spawns on, and must not pay
        # a restart for a file its engine does not read. Its Default is spent at
        # the start, as a pinned start spends it.
        cli_json = self._operator_entry(tmp_path, "max")
        before = cli_json.read_bytes()
        provider = self._provider(tmp_path, model="auto", acp_backend=backend)
        snapshots = []
        clients = []
        if backend == ACP_BACKEND_KAS:
            # Two models are staged so a start that wrongly restarts reaches its
            # own assertions rather than running the stub out of answers.
            self._stub_runtime_starts(
                provider,
                monkeypatch,
                [self._MODEL, self._MODEL],
                snapshots,
                clients,
                backend=backend,
            )
        else:
            self._stub_client_start(provider, monkeypatch, self._MODEL, snapshots)
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert provider.served_model == self._MODEL
        assert len(snapshots) == 1
        assert cli_json.read_bytes() == before
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}
        assert provider.explicit_effort_default_applied is True
        assert provider._explicit_effort_default_pending is False

    @pytest.mark.asyncio
    async def test_unpinned_restart_repeats_the_first_start_resume_decision(
        self, tmp_path, monkeypatch
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        provider._client._resume_session_id = "persisted-session"
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider, monkeypatch, [self._MODEL, self._MODEL], snapshots, clients
        )
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert len(clients) == 2
        assert [client[3] for client in clients] == [
            "persisted-session",
            "persisted-session",
        ]
        assert all(client[0].resumed is False for client in clients)
        assert provider.explicit_effort_default_applied is True
        assert provider._explicit_effort_default_pending is False

    @pytest.mark.asyncio
    async def test_restart_uses_only_the_replacement_native_load_replay_flags(
        self, tmp_path, monkeypatch, caplog
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        replay_flag_snapshots = []
        self._stub_runtime_starts(
            provider,
            monkeypatch,
            [self._MODEL, self._MODEL],
            snapshots,
            clients,
            resumed_flags=[False, True],
            replay_flags=[(True, True), None],
            replay_flag_snapshots=replay_flag_snapshots,
        )
        provider.arm_explicit_effort_default(nullcontext)

        with caplog.at_level(logging.INFO, logger=acp_provider.__name__):
            await provider.start()

        assert replay_flag_snapshots == [(False, False), (False, False)]
        assert provider._history_replay_needed is False
        assert provider._defer_replay_sid_promotion is False
        assert (
            sum(
                "restored with session/load keeps the level" in record.getMessage()
                for record in caplog.records
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_restart_reapplies_tool_search_replay_flags_from_replacement_start(
        self, tmp_path, monkeypatch
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        replay_flag_snapshots = []
        self._stub_runtime_starts(
            provider,
            monkeypatch,
            [self._MODEL, self._MODEL],
            snapshots,
            clients,
            replay_flags=[(True, True), (True, True)],
            replay_flag_snapshots=replay_flag_snapshots,
        )
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert replay_flag_snapshots == [(False, False), (False, False)]
        assert provider._history_replay_needed is True
        assert provider._defer_replay_sid_promotion is True

    @pytest.mark.asyncio
    async def test_restart_preserves_start_entry_replay_state_only(self, tmp_path, monkeypatch):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        provider._history_replay_needed = True
        snapshots = []
        clients = []
        replay_flag_snapshots = []
        self._stub_runtime_starts(
            provider,
            monkeypatch,
            [self._MODEL, self._MODEL],
            snapshots,
            clients,
            replay_flags=[(True, True), None],
            replay_flag_snapshots=replay_flag_snapshots,
        )
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert replay_flag_snapshots == [(True, False), (True, False)]
        assert provider._history_replay_needed is True
        assert provider._defer_replay_sid_promotion is False

    @pytest.mark.asyncio
    async def test_unpinned_native_loaded_session_is_not_restarted(
        self, tmp_path, monkeypatch, caplog
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider,
            monkeypatch,
            [self._MODEL, self._MODEL],
            snapshots,
            clients,
            resumed_flags=[True, False],
        )
        provider.arm_explicit_effort_default(nullcontext)

        with caplog.at_level(logging.INFO, logger=acp_provider.__name__):
            await provider.start()

        assert snapshots == [{self._MODEL: "max"}]
        assert _read_cli_overlay(tmp_path) == {}
        assert len(clients) == 1
        assert provider._client is clients[0][0]
        assert clients[0][2].is_alive() is True
        clients[0][2].kill.assert_not_awaited()
        assert provider.explicit_effort_default_applied is True
        assert provider._explicit_effort_default_pending is False
        assert (
            sum(
                "restored with session/load keeps the level" in record.getMessage()
                for record in caplog.records
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_unpinned_start_with_no_served_model_entry_changes_nothing(
        self, tmp_path, monkeypatch
    ):
        other_model = "claude-sonnet-4.6"
        self._operator_entry(tmp_path, "high", model=other_model)
        before = (tmp_path / ".kiro" / "settings" / "cli.json").read_bytes()
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        self._stub_runtime_starts(provider, monkeypatch, [self._MODEL], snapshots, clients)
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert len(clients) == 1
        assert snapshots == [{other_model: "high"}]
        assert (tmp_path / ".kiro" / "settings" / "cli.json").read_bytes() == before
        assert provider.explicit_effort_default_applied is True

    @pytest.mark.parametrize(
        ("model", "inactive_key"),
        [
            ("claude-opus-4.7", "reasoning"),
            ("gpt-5.6-sol", "output_config"),
        ],
        ids=["claude-reasoning", "gpt-output-config"],
    )
    @pytest.mark.asyncio
    async def test_unpinned_start_ignores_effort_under_inactive_family_key(
        self, tmp_path, monkeypatch, model, inactive_key
    ):
        cli_json = self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    model: {inactive_key: {"effort": "max"}},
                }
            },
        )
        before = cli_json.read_bytes()
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        self._stub_runtime_starts(provider, monkeypatch, [model, model], snapshots, clients)
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert len(clients) == 1
        assert cli_json.read_bytes() == before
        assert provider.explicit_effort_default_applied is True

    def test_overlay_snapshot_counts_only_each_models_active_effort_key(self, tmp_path):
        claude_model = "claude-opus-4.7"
        gpt_model = "gpt-5.6-sol"
        self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    claude_model: {"output_config": {"effort": "max"}},
                    gpt_model: {"output_config": {"effort": "high"}},
                }
            },
        )

        assert acp_cli_overlay._cli_overlay_effort_models(tmp_path) == {claude_model}

    @pytest.mark.asyncio
    async def test_unpinned_start_does_not_remove_peer_entry_written_after_spawn(
        self, tmp_path, monkeypatch
    ):
        other_model = "claude-sonnet-4.6"
        self._operator_entry(tmp_path, "high", model=other_model)
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []

        async def start_runtime():
            snapshots.append(_read_cli_overlay(provider._client._work_dir))
            self._write(
                tmp_path,
                {
                    "chat.modelDefaults": {
                        self._MODEL: {"output_config": {"effort": "max"}},
                        other_model: {"output_config": {"effort": "high"}},
                    }
                },
            )
            client, handle, runtime = self._live_client(
                provider._client._work_dir, self._MODEL, "session-1"
            )
            clients.append((client, handle, runtime))
            provider._client = client

        monkeypatch.setattr(provider, "_start_kiro_runtime", start_runtime)
        monkeypatch.setattr(provider, "_note_zero_tool_servers", AsyncMock())
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert snapshots == [{other_model: "high"}]
        assert len(clients) == 1
        assert _read_cli_overlay(tmp_path) == {
            self._MODEL: "max",
            other_model: "high",
        }
        assert provider.explicit_effort_default_applied is True

    @pytest.mark.asyncio
    async def test_unpinned_start_with_non_effort_model_keeps_its_entry(
        self, tmp_path, monkeypatch
    ):
        model = "claude-haiku-4.5"
        cli_json = self._operator_entry(tmp_path, "max", model=model)
        before = cli_json.read_bytes()
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        self._stub_runtime_starts(provider, monkeypatch, [model], snapshots, clients)
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert len(clients) == 1
        assert snapshots == [{model: "max"}]
        assert cli_json.read_bytes() == before
        assert provider.explicit_effort_default_applied is True

    @pytest.mark.asyncio
    async def test_unpinned_start_keeps_default_pending_when_served_model_is_unknown(
        self, tmp_path, monkeypatch, caplog
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        self._stub_runtime_starts(provider, monkeypatch, [""], snapshots, clients)
        provider.arm_explicit_effort_default(nullcontext)

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            await provider.start()

        assert len(clients) == 1
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}
        assert provider.explicit_effort_default_applied is False
        assert provider._explicit_effort_default_pending is True
        assert sum("served model is unknown" in r.getMessage() for r in caplog.records) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("push_fails", [False, True], ids=["push-succeeds", "push-fails"])
    async def test_unpinned_start_projects_workspace_default_and_requires_live_push(
        self, tmp_path, monkeypatch, push_fails
    ):
        other_model = "claude-sonnet-4.6"
        self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    self._MODEL: {"output_config": {"effort": "max"}},
                    other_model: {"output_config": {"effort": "high"}},
                }
            },
        )
        provider = self._provider(tmp_path, model="auto", effort_defaults={self._MODEL: "low"})
        snapshots = []
        clients = []

        async def start_runtime():
            snapshots.append(_read_cli_overlay(provider._client._work_dir))
            error = RuntimeError("push refused") if push_fails else None
            client, handle, runtime = self._live_client(
                provider._client._work_dir,
                self._MODEL,
                "session-1",
                push_error=error,
            )
            clients.append((client, handle, runtime))
            provider._client = client

        monkeypatch.setattr(provider, "_start_kiro_runtime", start_runtime)
        monkeypatch.setattr(provider, "_note_zero_tool_servers", AsyncMock())
        provider.arm_explicit_effort_default(nullcontext)

        await provider.start()

        assert snapshots == [{self._MODEL: "max", other_model: "high"}]
        assert _read_cli_overlay(tmp_path) == {
            self._MODEL: "low",
            other_model: "high",
        }
        clients[0][1].send_command.assert_awaited_once_with("/effort", {"level": "low"})
        assert provider.explicit_effort_default_applied is not push_fails
        assert provider._explicit_effort_default_pending is push_fails

    @pytest.mark.asyncio
    async def test_unpinned_start_keeps_default_pending_when_rewrite_is_refused(
        self, tmp_path, monkeypatch, caplog
    ):
        other_model = "claude-sonnet-4.6"
        self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    self._MODEL: {"output_config": {"effort": "max"}},
                    other_model: {"output_config": {"effort": "high"}},
                }
            },
        )
        provider = self._provider(tmp_path, model="auto", effort_defaults={self._MODEL: "low"})
        snapshots = []
        clients = []
        self._stub_runtime_starts(provider, monkeypatch, [self._MODEL], snapshots, clients)
        monkeypatch.setattr(
            acp_provider,
            "_write_cli_overlay",
            MagicMock(side_effect=OSError("version guard refused the write")),
        )
        provider.arm_explicit_effort_default(nullcontext)

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            await provider.start()

        assert len(clients) == 1
        assert _read_cli_overlay(tmp_path) == {
            self._MODEL: "max",
            other_model: "high",
        }
        assert provider.explicit_effort_default_applied is False
        assert provider._explicit_effort_default_pending is True
        assert sum("overlay rewrite failed" in r.getMessage() for r in caplog.records) == 1

    @pytest.mark.asyncio
    async def test_unpinned_restart_stops_after_second_model_has_its_own_entry(
        self, tmp_path, monkeypatch, caplog
    ):
        second_model = "claude-sonnet-4.6"
        self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    self._MODEL: {"output_config": {"effort": "max"}},
                    second_model: {"output_config": {"effort": "high"}},
                }
            },
        )
        provider = self._provider(tmp_path, model="auto")
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider, monkeypatch, [self._MODEL, second_model], snapshots, clients
        )
        provider.arm_explicit_effort_default(nullcontext)

        with caplog.at_level(logging.WARNING, logger=acp_provider.__name__):
            with pytest.raises(
                RuntimeError,
                match="replacement runtime still reads its served model's effort entry",
            ):
                await provider.start()

        assert snapshots == [
            {self._MODEL: "max", second_model: "high"},
            {second_model: "high"},
        ]
        assert len(clients) == 2
        assert provider._client is clients[1][0]
        assert _read_cli_overlay(tmp_path) == {second_model: "high"}
        assert provider.explicit_effort_default_applied is False
        assert provider._explicit_effort_default_pending is True
        assert sum("no third runtime" in r.getMessage() for r in caplog.records) == 1

    @pytest.mark.asyncio
    async def test_unpinned_restart_fails_when_first_client_shutdown_raises(
        self, tmp_path, monkeypatch
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        sessions = self._seed_sessions_dir(tmp_path, monkeypatch, ["session-1"])
        snapshots = []
        clients = []
        shutdown_error = RuntimeError("shutdown failed")
        self._stub_runtime_starts(
            provider,
            monkeypatch,
            [self._MODEL, self._MODEL],
            snapshots,
            clients,
            teardown_error=shutdown_error,
        )
        provider.arm_explicit_effort_default(nullcontext)

        with pytest.raises(RuntimeError, match="shutting down the first runtime failed") as exc:
            await provider.start()

        assert exc.value.__cause__ is shutdown_error
        assert snapshots == [{self._MODEL: "max"}]
        assert provider._client is clients[0][0]
        assert provider.explicit_effort_default_applied is False
        assert provider._explicit_effort_default_pending is True
        assert self._session_files_present(sessions, "session-1") == [True, True]

    @pytest.mark.asyncio
    async def test_unpinned_restart_fails_when_first_runtime_remains_alive(
        self, tmp_path, monkeypatch
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        sessions = self._seed_sessions_dir(tmp_path, monkeypatch, ["session-1"])
        snapshots = []
        clients = []
        self._stub_runtime_starts(
            provider,
            monkeypatch,
            [self._MODEL, self._MODEL],
            snapshots,
            clients,
            runtime_stays_alive=True,
        )
        provider.arm_explicit_effort_default(nullcontext)

        with pytest.raises(RuntimeError, match="the first runtime did not stop"):
            await provider.start()

        assert snapshots == [{self._MODEL: "max"}]
        assert provider._client is clients[0][0]
        assert provider.explicit_effort_default_applied is False
        assert provider._explicit_effort_default_pending is True
        assert self._session_files_present(sessions, "session-1") == [True, True]

    @pytest.mark.asyncio
    async def test_unpinned_restart_failure_raises_without_restoring_dead_client(
        self, tmp_path, monkeypatch
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, model="auto")
        placeholder_client = provider._client
        snapshots = []
        clients = []

        async def start_runtime():
            snapshots.append(_read_cli_overlay(provider._client._work_dir))
            if len(snapshots) == 2:
                raise RuntimeError("replacement start failed")
            client, handle, runtime = self._live_client(
                provider._client._work_dir, self._MODEL, "session-1"
            )
            clients.append((client, handle, runtime))
            provider._client = client

        monkeypatch.setattr(provider, "_start_kiro_runtime", start_runtime)
        monkeypatch.setattr(provider, "_note_zero_tool_servers", AsyncMock())
        provider.arm_explicit_effort_default(nullcontext)

        with pytest.raises(RuntimeError, match="replacement start failed"):
            await provider.start()

        assert snapshots == [{self._MODEL: "max"}, {}]
        assert provider._client is placeholder_client
        assert clients[0][2].is_alive() is False
        assert provider.explicit_effort_default_applied is False
        assert provider._explicit_effort_default_pending is True

    @pytest.mark.asyncio
    async def test_start_removes_an_operator_entry_once_when_explicit_default_is_pending(
        self, tmp_path, monkeypatch
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path)
        provider.arm_explicit_effort_default(nullcontext)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())

        await provider.start()

        assert _read_cli_overlay(tmp_path) == {}
        assert provider.explicit_effort_default_applied is True

        # A later spawn of the same provider (a resume, a model swap) projects
        # with the ownership guard again.
        self._operator_entry(tmp_path, "high")
        await provider.start()
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "high"}

    @pytest.mark.asyncio
    async def test_start_keeps_an_operator_entry_without_explicit_default(
        self, tmp_path, monkeypatch
    ):
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())

        await provider.start()

        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}
        assert provider.explicit_effort_default_applied is False

    @pytest.mark.asyncio
    @requires_symlinks
    async def test_a_refused_file_leaves_the_default_pending_for_the_next_start(
        self, tmp_path, monkeypatch
    ):
        # A linked cli.json is never rewritten, so the projection fails and the
        # intent is not spent. The next start that can rewrite the file applies it.
        target = tmp_path / "outside.json"
        outside = {"chat.modelDefaults": {self._MODEL: {"output_config": {"effort": "max"}}}}
        target.write_text(json.dumps(outside), encoding="utf-8")
        settings = tmp_path / ".kiro" / "settings"
        settings.mkdir(parents=True)
        cli_json = settings / "cli.json"
        try:
            cli_json.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"symlink is unavailable: {exc}")
        provider = self._provider(tmp_path)
        provider.arm_explicit_effort_default(nullcontext)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())

        await provider.start()

        assert provider.explicit_effort_default_applied is False
        assert json.loads(target.read_text(encoding="utf-8")) == outside

        cli_json.unlink()
        self._operator_entry(tmp_path, "max")
        await provider.start()

        assert provider.explicit_effort_default_applied is True
        assert _read_cli_overlay(tmp_path) == {}

    @pytest.mark.asyncio
    async def test_the_rewrite_runs_inside_its_fence_and_the_spawn_after_it(
        self, tmp_path, monkeypatch
    ):
        # The fence refuses warm-runtime claims while it is up and dates the
        # rewrite as it comes down: the write must be inside it, and the rest of
        # the start, which can take seconds, outside it.
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path)
        events: list[str] = []

        @contextmanager
        def fence():
            events.append("fence up")
            try:
                yield
            finally:
                events.append("fence down")

        real_atomic_write = acp_provider.atomic_write

        def record_write(path, contents, **kwargs):
            events.append("write")
            real_atomic_write(path, contents, **kwargs)

        monkeypatch.setattr(acp_provider, "atomic_write", record_write)
        monkeypatch.setattr(
            provider, "_start_kiro_runtime", AsyncMock(side_effect=lambda: events.append("spawn"))
        )
        provider.arm_explicit_effort_default(fence)

        await provider.start()

        assert events == ["fence up", "write", "fence down", "spawn"]
        assert _read_cli_overlay(tmp_path) == {}

    @pytest.mark.asyncio
    async def test_a_start_cancelled_during_the_rewrite_still_knows_it_ran(
        self, tmp_path, monkeypatch
    ):
        # A cold start cancelled here arms the session's flag again unless the
        # provider reports the replacement ran, and a flag armed after the
        # removal removes the next level anyone projects for this model.
        self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path)
        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
        worker_started = threading.Event()
        release_worker = threading.Event()
        worker_finished = threading.Event()
        fence_down_after_the_worker: list[bool] = []
        real_apply = provider._apply_effort_overlay

        def blocked_apply(**kwargs):
            worker_started.set()
            try:
                assert release_worker.wait(timeout=5)
                return real_apply(**kwargs)
            finally:
                worker_finished.set()

        @contextmanager
        def fence():
            try:
                yield
            finally:
                fence_down_after_the_worker.append(worker_finished.is_set())

        monkeypatch.setattr(provider, "_apply_effort_overlay", blocked_apply)
        provider.arm_explicit_effort_default(fence)
        start_task = asyncio.create_task(provider.start())
        try:
            await async_wait_until(
                worker_started.is_set, describe=lambda: "worker_started was never set"
            )
            start_task.cancel()
            await asyncio.sleep(0)
            assert not start_task.done()
            assert fence_down_after_the_worker == []
        finally:
            release_worker.set()

        with pytest.raises(asyncio.CancelledError):
            await _await_test(start_task, "start_task")
        assert fence_down_after_the_worker == [True]
        assert _read_cli_overlay(tmp_path) == {}
        assert provider.explicit_effort_default_applied is True
        provider._start_kiro_runtime.assert_not_awaited()

    def test_no_level_removes_both_family_entries_only_for_explicit_default(self, tmp_path):
        cli_json = self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    self._MODEL: {
                        "output_config": {"effort": "max"},
                        "reasoning": {"effort": "high"},
                    }
                },
                "other": 1,
            },
        )
        provider = self._provider(tmp_path)

        assert provider._apply_effort_overlay() is True
        guarded = json.loads(cli_json.read_text(encoding="utf-8"))
        assert guarded["chat.modelDefaults"][self._MODEL]["output_config"] == {"effort": "max"}
        assert guarded["chat.modelDefaults"][self._MODEL]["reasoning"] == {"effort": "high"}

        assert provider._apply_effort_overlay(replace_unowned=True) is True
        data = json.loads(cli_json.read_text(encoding="utf-8"))
        entry = data.get("chat.modelDefaults", {}).get(self._MODEL, {})
        assert "effort" not in entry.get("output_config", {})
        assert "effort" not in entry.get("reasoning", {})
        assert data["other"] == 1

    def test_a_resolved_level_replaces_an_operator_entry(self, tmp_path):
        cli_json = self._operator_entry(tmp_path, "max")
        provider = self._provider(tmp_path, effort_per_model={self._MODEL: "high"})

        assert provider._apply_effort_overlay(replace_unowned=True) is True

        assert _read_cli_overlay(tmp_path) == {self._MODEL: "high"}
        data = json.loads(cli_json.read_text(encoding="utf-8"))
        assert data[_KIROCREW_EFFORT_OWNED_KEY] == {self._MODEL: "high"}

    def test_the_auto_model_keeps_the_ownership_guard(self, tmp_path):
        # Under auto no concrete model is known before kiro-cli reads the file,
        # so, as for a live auto session, only entries Kiro Crew recorded go.
        owned_model = "claude-sonnet-4.6"
        stamp = 1_700_000_000
        self._write(
            tmp_path,
            {
                "chat.modelDefaults": {
                    owned_model: {"output_config": {"effort": "low"}},
                    self._MODEL: {"output_config": {"effort": "max"}},
                },
                _KIROCREW_EFFORT_OWNED_KEY: {owned_model: "low"},
                "kirocrew.effortOwnedStamp": stamp,
            },
        )
        os.utime(tmp_path / ".kiro" / "settings" / "cli.json", (stamp, stamp))
        provider = self._provider(tmp_path, model="auto")

        assert provider._apply_effort_overlay(replace_unowned=True) is True
        assert _read_cli_overlay(tmp_path) == {self._MODEL: "max"}

    @pytest.mark.parametrize(
        ("model", "backend"),
        [("claude-haiku-4.5", ACP_BACKEND_KIRO), ("claude-opus-4.7", "claude")],
        ids=["model-without-effort", "backend-that-reads-no-overlay"],
    )
    def test_nothing_to_remove_counts_as_applied(self, tmp_path, model, backend):
        cli_json = self._operator_entry(tmp_path, "max", model=model)
        before = cli_json.read_bytes()
        provider = self._provider(tmp_path, model=model, acp_backend=backend)

        assert provider._apply_effort_overlay(replace_unowned=True) is True
        assert cli_json.read_bytes() == before
