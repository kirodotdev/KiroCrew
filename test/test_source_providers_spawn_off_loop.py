"""The Windows provider-CLI spawn runs off the gateway event loop.

On native Windows the PR-chip repo-visibility refresh runs a one-shot provider
CLI (``gh repo view`` / ``glab api``) through ``runner._run_provider``. The async
spawn there builds the ``Popen`` -- and so runs ``CreateProcess`` -- SYNCHRONOUSLY
on the Proactor loop thread, inside the subprocess transport's constructor before
its first ``await``. When process creation on the host is slow, the gateway event
loop is held for seconds and the loop-stall watchdog kills the gateway.

A provider read is one-shot (spawn, read both pipes, wait for exit, nothing
streamed), so on Windows it runs off the loop on a worker thread via
``popen_limited`` + a bounded drain. POSIX keeps the async path: a fork is cheap
there and the loop was never held.

Off Windows, the Windows path is driven with the platform seam patched.
``time.sleep`` stands in for the synchronous ``CreateProcess``: an ``await`` there
would let the caller's loop advance even with the defect present.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kiro_crew import platform_compat  # noqa: E402
from kiro_crew.dashboard.source_providers import runner  # noqa: E402
from kiro_crew.testing.ids import unallocatable_pids  # noqa: E402
from kiro_crew.testing.wait import async_wait_until  # noqa: E402

_BLOCK_SECS = 0.4

# Fake PIDs no OS can allocate (above 2**32); never probed with os.kill.
_PID_PLAIN, _PID_FLOOD, _PID_BLOCK = unallocatable_pids(3)


async def _noop_sandbox_wrap(argv, *, mode="standard", env=None, _prepare=None):
    """Identity sandbox wrap: hand the argv straight back with its env, no shim."""
    return list(argv), dict(env or {}), None


def _parse_ok(returncode: int, stdout: bytes, stderr: bytes) -> Any:
    return {"ok": returncode == 0, "out": stdout}


def _install_common_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the provider plumbing so the test reaches the spawn and nothing else."""
    monkeypatch.setattr(
        runner, "_resolve_provider_executable", lambda executable: rf"C:\fixed\{executable}.exe"
    )
    monkeypatch.setattr(runner, "sandboxed_spawn_argv_async", _noop_sandbox_wrap)
    monkeypatch.setattr(runner, "_audit_provider_cli", lambda *a, **k: None)


class _OnLoopSpawn:
    """A fake create_subprocess_limited: records its loop, blocks, returns a proc."""

    def __init__(self, *, block: bool) -> None:
        self.block = block
        self.calls = 0
        self.loop_ids: list[int] = []
        self.thread_names: list[str] = []

    async def __call__(self, *_args: str, **_kwargs: Any) -> Any:
        self.calls += 1
        self.loop_ids.append(id(asyncio.get_running_loop()))
        self.thread_names.append(threading.current_thread().name)
        if self.block:
            time.sleep(_BLOCK_SECS)
        return _FakeAsyncProc()


class _EmptyAsyncReader:
    async def read(self, _n: int) -> bytes:
        return b""


class _FakeAsyncProc:
    returncode = 0
    stdout = _EmptyAsyncReader()
    stderr = _EmptyAsyncReader()

    async def wait(self) -> int:
        return 0


class _EmptySyncReader:
    def read(self, _n: int) -> bytes:
        return b""


class _FakePopen:
    """A fake popen_limited result: empty pipes, immediate exit."""

    def __init__(self) -> None:
        self.pid = _PID_PLAIN
        self.returncode = 0
        self.stdout = _EmptySyncReader()
        self.stderr = _EmptySyncReader()

    def wait(self, timeout=None) -> int:
        return 0


def _fake_popen_limited(record: dict[str, Any], *, block: bool):
    """A fake popen_limited recording WHERE it ran (worker thread, no loop)."""

    def _popen(argv, *, profile=None, stdout=None, stderr=None, env=None, **kwargs):
        record["calls"] += 1
        record["thread_names"].append(threading.current_thread().name)
        try:
            record["loop_ids"].append(id(asyncio.get_running_loop()))
        except RuntimeError:
            record["loop_ids"].append(None)  # no loop on the worker thread -- the point
        if block:
            time.sleep(_BLOCK_SECS)
        return _FakePopen()

    return _popen


class TestWindowsProviderSpawnLeavesTheGatewayLoop:
    @pytest.mark.asyncio
    async def test_caller_loop_keeps_ticking_while_the_spawn_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gateway loop stays live while a slow Windows spawn blocks.

        Deterministic, not timing-based: the fake spawn blocks on an event, and a
        concurrent heartbeat RELEASES it on its first tick. If the spawn ran on the
        loop thread (the defect), the heartbeat could never run to release it and
        this would hang until the suite timeout; off the loop, the heartbeat runs
        during the blocked spawn and the call completes.
        """
        _install_common_seams(monkeypatch)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)

        spawn_started = threading.Event()
        may_finish = threading.Event()
        heartbeat_ran_during_spawn = False

        def _blocking_popen(*_a, **_k):
            spawn_started.set()
            assert may_finish.wait(timeout=10), "the heartbeat never released the spawn"
            return _FakePopen()

        monkeypatch.setattr(runner, "popen_limited", _blocking_popen)

        async def _heartbeat() -> None:
            nonlocal heartbeat_ran_during_spawn
            # Wait until the worker is inside the blocking spawn, then prove the
            # loop advanced here (it could not if the spawn held this thread) and
            # release the worker.
            await asyncio.to_thread(spawn_started.wait, 10)
            heartbeat_ran_during_spawn = True
            may_finish.set()

        beat = asyncio.create_task(_heartbeat())
        try:
            result = await runner._run_provider("gh", parse=_parse_ok)
            assert result["ok"] is True
        finally:
            await asyncio.gather(beat, return_exceptions=True)

        assert heartbeat_ran_during_spawn, (
            "the heartbeat never advanced while the spawn blocked -- the loop was "
            "held by CreateProcess on the loop thread"
        )

    @pytest.mark.asyncio
    async def test_windows_spawn_runs_off_the_loop_via_popen_limited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Windows one-shot spawn runs through popen_limited off the loop."""
        _install_common_seams(monkeypatch)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        record: dict[str, Any] = {"calls": 0, "loop_ids": [], "thread_names": []}
        monkeypatch.setattr(runner, "popen_limited", _fake_popen_limited(record, block=False))
        on_loop = _OnLoopSpawn(block=False)
        monkeypatch.setattr(runner, "create_subprocess_limited", on_loop)

        caller_thread = threading.current_thread().name

        result = await runner._run_provider("gh", parse=_parse_ok)
        assert result["ok"] is True
        assert record["calls"] == 1, "popen_limited was not the spawn path on Windows"
        assert on_loop.calls == 0, "the async on-loop spawn was still used on Windows"
        assert record["loop_ids"][0] is None, "the spawn ran on an event loop, not a worker thread"
        assert record["thread_names"][0] != caller_thread


class TestBoundsAndCleanupArePreserved:
    """The off-loop path keeps the memory bound and the process-tree reap: the byte
    cap is enforced WHILE draining (not after a full buffer), and overflow or
    cancellation kills and reaps the child tree."""

    @staticmethod
    def _patch_windows(monkeypatch: pytest.MonkeyPatch) -> None:
        _install_common_seams(monkeypatch)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)

    @pytest.mark.asyncio
    async def test_oversized_response_is_rejected_without_buffering_the_whole_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_windows(monkeypatch)
        read_total = {"n": 0}
        reaped: list[int] = []

        class _FloodReader:
            def read(self, n: int) -> bytes:
                read_total["n"] += n
                return b"x" * n

        class _FloodPopen:
            def __init__(self) -> None:
                self.pid = _PID_FLOOD
                self.returncode = None  # still running when overflow trips

            stdout = None
            stderr = None

            def wait(self, timeout=None) -> int:
                self.returncode = 0
                return 0

        flood = _FloodPopen()
        flood.stdout = _FloodReader()
        flood.stderr = _EmptySyncReader()
        monkeypatch.setattr(runner, "popen_limited", lambda *a, **k: flood)
        monkeypatch.setattr(
            platform_compat,
            "terminate_and_reap_sync",
            lambda proc, **k: reaped.append(proc.pid),
        )

        with pytest.raises(runner.SourceProviderError, match="too large"):
            await runner._run_provider("gh", parse=_parse_ok, max_output_bytes=4096)

        # The cap tripped during draining, not after an unbounded buffer: only a
        # little over the 4 KiB cap was ever read.
        assert read_total["n"] <= 4096 + 64 * 1024
        assert reaped == [_PID_FLOOD], "the process tree was not reaped on overflow"

    @pytest.mark.asyncio
    async def test_cancellation_reaps_the_child(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_windows(monkeypatch)
        reaped: list[int] = []
        entered = threading.Event()
        release = threading.Event()

        class _BlockingReader:
            def read(self, _n: int) -> bytes:
                entered.set()
                release.wait(timeout=10)
                return b""

        class _BlockingPopen:
            def __init__(self) -> None:
                self.pid = _PID_BLOCK
                self.returncode = None

            stdout = None
            stderr = None

            def wait(self, timeout=None) -> int:
                release.wait(timeout=10)
                self.returncode = 0
                return 0

        blocking = _BlockingPopen()
        blocking.stdout = _BlockingReader()
        blocking.stderr = _EmptySyncReader()
        monkeypatch.setattr(runner, "popen_limited", lambda *a, **k: blocking)

        def _record_reap(proc, **k) -> None:
            reaped.append(proc.pid)
            release.set()  # let the stuck worker unwind after the reap

        monkeypatch.setattr(platform_compat, "terminate_and_reap_sync", _record_reap)

        task = asyncio.create_task(runner._run_provider("gh", parse=_parse_ok))
        await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert reaped == [_PID_BLOCK], "cancellation did not reap the child tree"

    @pytest.mark.asyncio
    async def test_cancellation_during_the_spawn_still_reaps(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cancel landing WHILE CreateProcess is in flight must not orphan the child.

        The spawn stand-in returns only once the worker's cancel flag is set, so
        the cancel has landed mid-spawn by construction. ``task.cancel()`` only
        schedules the cancel: the flag is set when the awaiting coroutine runs its
        ``except CancelledError`` on a later loop turn, and a worker thread released
        on a clock can run ahead of that turn and drive the empty-piped child to a
        normal exit with nothing left to reap.
        """
        self._patch_windows(monkeypatch)
        reaped: list[int] = []
        in_spawn = threading.Event()
        handle_box: dict[str, Any] = {}
        real_drain = runner._oneshot_drain_limited

        def _capture_handle(argv, env, max_output_bytes, handle):
            # Runs on the worker thread before the spawn: expose the cancel flag
            # the spawn stand-in below waits on.
            handle_box["handle"] = handle
            return real_drain(argv, env, max_output_bytes, handle)

        class _SlowSpawnPopen:
            pid = _PID_BLOCK
            returncode = None
            stdout = _EmptySyncReader()
            stderr = _EmptySyncReader()

            def wait(self, timeout=None) -> int:
                return 0

        def _slow_popen(*_a, **_k):
            # Stand in for a multi-second CreateProcess: return only once the
            # cancel flag is set, so the cancel lands before the proc is published
            # to the handle. The ceiling turns a lost cancel into a named failure.
            in_spawn.set()
            handle_box["handle"]["cancel"].wait(timeout=10)
            return _SlowSpawnPopen()

        monkeypatch.setattr(runner, "_oneshot_drain_limited", _capture_handle)
        monkeypatch.setattr(runner, "popen_limited", _slow_popen)
        monkeypatch.setattr(
            platform_compat, "terminate_and_reap_sync", lambda proc, **k: reaped.append(proc.pid)
        )

        task = asyncio.create_task(runner._run_provider("gh", parse=_parse_ok))
        await asyncio.to_thread(in_spawn.wait, 10)
        task.cancel()  # cancel while the spawn is still in flight
        with pytest.raises(asyncio.CancelledError):
            await task
        assert reaped == [_PID_BLOCK], "a cancel during the spawn orphaned the child"


class TestPosixProviderPathUnchanged:
    @pytest.mark.asyncio
    async def test_posix_spawns_on_the_async_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """POSIX keeps the async create_subprocess_limited path, no worker-thread hop."""
        if sys.platform == "win32":
            pytest.skip("POSIX-semantics test: the real os module lacks POSIX-only calls here")
        _install_common_seams(monkeypatch)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        on_loop = _OnLoopSpawn(block=False)
        monkeypatch.setattr(runner, "create_subprocess_limited", on_loop)

        def _forbidden(*_a, **_k):
            raise AssertionError("POSIX must not route the provider spawn through popen_limited")

        monkeypatch.setattr(runner, "popen_limited", _forbidden)

        result = await runner._run_provider("gh", parse=_parse_ok)
        assert result["ok"] is True
        assert on_loop.calls == 1, "POSIX did not use the async spawn path"
        assert on_loop.loop_ids[0] == id(asyncio.get_running_loop())


@pytest.mark.skipif(sys.platform != "win32", reason="exercises the real Windows off-loop spawn")
class TestWindowsRealChild:
    """Unstubbed coverage of the Windows off-loop path against a real child.

    Runs only on the Windows CI job. It drives ``_oneshot_drain_limited`` with a
    real ``sys.executable -c`` child -- the actual ``popen_limited`` + worker-
    thread drain -- rather than a fake, covering normal exit, output overflow and
    cancellation, and asserts the child is reaped on the last two.
    """

    @staticmethod
    def _argv(code: str) -> list[str]:
        return [sys.executable, "-c", code]

    @pytest.mark.asyncio
    async def test_normal_exit_returns_output(self) -> None:
        handle: dict = {"cancel": threading.Event()}
        rc, out, err = await asyncio.to_thread(
            runner._oneshot_drain_limited,
            self._argv("import sys; sys.stdout.write('ok'); sys.exit(0)"),
            dict(os.environ),
            1 << 20,
            handle,
        )
        assert rc == 0
        assert out == b"ok"

    @pytest.mark.asyncio
    async def test_overflow_is_rejected_and_child_reaped(self) -> None:
        handle: dict = {"cancel": threading.Event()}
        with pytest.raises(runner._ProviderOutputTooLarge):
            await asyncio.to_thread(
                runner._oneshot_drain_limited,
                self._argv("import sys\nwhile True: sys.stdout.write('x' * 65536)"),
                dict(os.environ),
                4096,
                handle,
            )
        proc = handle.get("proc")
        assert proc is not None and proc.poll() is not None, "overflowing child was not reaped"

    @pytest.mark.asyncio
    async def test_cancel_reaps_the_real_child(self) -> None:
        handle: dict = {"cancel": threading.Event()}
        # A child that sleeps well past the test; cancellation must reap it.
        worker = asyncio.ensure_future(
            asyncio.to_thread(
                runner._oneshot_drain_limited,
                self._argv("import time; time.sleep(60)"),
                dict(os.environ),
                1 << 20,
                handle,
            )
        )
        try:
            # Wait until the child exists, then assert; a failed wait must still
            # tear the worker and its child down (the finally below).
            proc = await async_wait_until(
                lambda: handle.get("proc"), describe=lambda: "child never started"
            )
            handle["cancel"].set()
            with pytest.raises(asyncio.CancelledError):
                await worker
            assert proc.poll() is not None, "cancelled child was not reaped"
        finally:
            handle["cancel"].set()
            worker.cancel()
            with contextlib.suppress(BaseException):
                await worker
