"""The gateway's Unix loop starts a child process off the event loop thread.

``subprocess.Popen`` forks the gateway and returns only after the child exec'd. On
a gateway with a large heap and many threads that fork alone held the loop long
enough for the loop-stall watchdog to restart the gateway; the App Store refresh
starts one ``git clone`` per catalog row, so it was the usual trigger.

These tests hold ``Popen`` closed until the test releases it, so a fork of any
length is simulated without a sleep: on the gateway's loop the loop keeps turning
while ``Popen`` is held, through the spawn helper the App Store's clone uses. On
a loop that forks on its own thread, ``Popen`` is held on that thread, nothing can
release it, and it fails by name at the hold bound.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import threading
import warnings
from collections.abc import Iterator
from typing import Any

import pytest

from kiro_crew import platform_compat_offloop_spawn as offloop
from kiro_crew import sandbox_rlimit
from kiro_crew.platform_compat_native import IS_WINDOWS
from kiro_crew.testing.wait import await_until, finished

pytestmark = [
    pytest.mark.skipif(IS_WINDOWS, reason="drives the gateway's Unix loop"),
    # Each test starts its own interpreter child; one worker keeps them serial.
    pytest.mark.xdist_group(name="spawn_off_loop"),
]

#: A lost-run bound only: the tests release ``Popen`` themselves, and a ``Popen``
#: still held at this bound means the loop never got to release it.
_HOLD_BOUND_SECS = 10.0

#: Loop turns taken while ``Popen`` is held. Any number above zero proves the loop
#: was free; a few dozen makes the claim visible in a failure.
_TICKS = 50


class _HeldPopen:
    """Replaces ``subprocess.Popen`` with one that waits for the test to release it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered: asyncio.Event | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.release = threading.Event()
        self.threads: list[int] = []
        self.children: list[subprocess.Popen[bytes]] = []
        held = self
        real_popen = subprocess.Popen

        class _Held(real_popen):  # type: ignore[misc, valid-type]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                held.threads.append(threading.get_ident())
                if held.loop is not None and held.entered is not None:
                    held.loop.call_soon_threadsafe(held.entered.set)
                if not held.release.wait(_HOLD_BOUND_SECS):
                    raise AssertionError("Popen was never released: the loop was held")
                super().__init__(*args, **kwargs)
                held.children.append(self)

        monkeypatch.setattr(subprocess, "Popen", _Held)

    def bind(self) -> asyncio.Event:
        self.loop = asyncio.get_running_loop()
        self.entered = asyncio.Event()
        return self.entered


def _kill_leftover_children(held: _HeldPopen) -> None:
    """Kill and reap any child a failed test left running."""
    for child in held.children:
        if child.returncode is None:
            child.kill()
            child.wait()


@contextlib.contextmanager
def _sigchld_child_watcher() -> Iterator[None]:
    """The SIGCHLD watcher the macOS gateway installs, on interpreters that have it."""
    if not hasattr(asyncio, "SafeChildWatcher"):
        yield  # CPython 3.14+: the loop reaps its own children.
        return
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        policy = asyncio.get_event_loop_policy()
        previous = policy._watcher  # type: ignore[attr-defined]
        watcher = asyncio.SafeChildWatcher()
        asyncio.set_child_watcher(watcher)
    try:
        yield
    finally:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            watcher.close()
            asyncio.set_child_watcher(previous)


def _run_on_gateway_loop(body: Any) -> Any:
    with asyncio.Runner(loop_factory=offloop.gateway_loop_factory()) as runner:
        return runner.run(body())


async def _ticks_while_pending(task: asyncio.Future[Any]) -> int:
    ticks = 0
    for _ in range(_TICKS):
        await asyncio.sleep(0)
        if task.done():
            break
        ticks += 1
    return ticks


@pytest.mark.parametrize("watcher", ["default", "sigchld"])
def test_the_gateway_loop_keeps_running_while_a_child_forks(
    monkeypatch: pytest.MonkeyPatch, watcher: str
) -> None:
    held = _HeldPopen(monkeypatch)

    async def body() -> tuple[int, int | None, bytes, bytes, int]:
        loop_thread = threading.get_ident()
        entered = held.bind()

        async def spawn() -> tuple[int | None, bytes, bytes]:
            # The spawn helper the App Store's manifest clone goes through.
            proc = await sandbox_rlimit.create_subprocess_limited(
                sys.executable,
                "-c",
                "import sys; sys.stdout.write(sys.stdin.read().upper())",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate(b"manifest")
            return proc.returncode, out, err

        work = asyncio.ensure_future(spawn())
        await await_until(entered, what="Popen entered")
        ticks = await _ticks_while_pending(work)
        held.release.set()
        returncode, out, err = await finished(work, "the spawn")
        return ticks, returncode, out, err, loop_thread

    try:
        with _sigchld_child_watcher() if watcher == "sigchld" else contextlib.nullcontext():
            ticks, returncode, out, err, loop_thread = _run_on_gateway_loop(body)
    finally:
        _kill_leftover_children(held)

    assert ticks == _TICKS, "the loop stopped turning while the child was forking"
    assert (returncode, out) == (0, b"MANIFEST"), err
    assert held.threads and loop_thread not in held.threads


def test_a_child_built_off_the_loop_reports_its_exit_and_can_be_killed() -> None:
    async def body() -> tuple[int, int]:
        sleeper = await sandbox_rlimit.create_subprocess_limited(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            assert sleeper.returncode is None
            sleeper.kill()
            killed = await finished(sleeper.wait(), "the killed child's exit")
        finally:
            # A failed assertion or wait above must not leave the sleeper running.
            if sleeper.returncode is None:
                sleeper.kill()
                await sleeper.wait()
        quick = await sandbox_rlimit.create_subprocess_limited(
            sys.executable, "-c", "raise SystemExit(3)"
        )
        return killed, await finished(quick.wait(), "the quick child's exit")

    with _sigchld_child_watcher():
        killed, quick = _run_on_gateway_loop(body)
    assert killed != 0
    assert quick == 3


def test_a_spawn_cancelled_mid_fork_reaps_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = _HeldPopen(monkeypatch)

    async def body() -> None:
        entered = held.bind()
        task = asyncio.ensure_future(
            sandbox_rlimit.create_subprocess_limited(
                sys.executable, "-c", "import time; time.sleep(60)"
            )
        )
        await await_until(entered, what="Popen entered")
        task.cancel()
        held.release.set()
        with pytest.raises(asyncio.CancelledError):
            await finished(task, "the cancelled spawn")

    try:
        _run_on_gateway_loop(body)
        # The fork finished after the cancel; that child was killed and reaped,
        # not left running with no owner.
        assert len(held.children) == 1
        assert held.children[0].returncode is not None
        assert held.children[0].returncode != 0
    finally:
        _kill_leftover_children(held)


def test_a_loop_with_no_usable_watcher_spawns_the_stock_way_and_says_so_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    threads: list[int] = []
    real_popen = subprocess.Popen

    class _Recorded(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            threads.append(threading.get_ident())
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _Recorded)
    monkeypatch.setattr(offloop, "_posix_child_watcher", lambda _loop: None)
    monkeypatch.setattr(offloop, "_stock_spawn_fallback_logged", False)

    async def body() -> tuple[int, list[int]]:
        codes = []
        for code in (4, 5):
            proc = await sandbox_rlimit.create_subprocess_limited(
                sys.executable, "-c", f"raise SystemExit({code})"
            )
            codes.append(await finished(proc.wait(), "the child's exit"))
        return threading.get_ident(), codes

    with caplog.at_level("WARNING", logger=offloop.logger.name):
        loop_thread, codes = _run_on_gateway_loop(body)

    assert codes == [4, 5]
    # The stock path: Popen ran on the loop thread itself.
    assert threads == [loop_thread, loop_thread]
    fallback_lines = [r for r in caplog.records if "off-loop spawn unavailable" in r.message]
    assert len(fallback_lines) == 1


@pytest.mark.skipif(
    not hasattr(asyncio, "FastChildWatcher"), reason="CPython 3.14+ has no child-watcher API"
)
@pytest.mark.parametrize("kind", ["FastChildWatcher", "SafeChildWatcher"])
def test_a_watcher_the_off_loop_path_cannot_trust_sends_spawns_the_stock_way(kind: str) -> None:
    # FastChildWatcher drops the exit of a pid registered after it exits; a
    # SafeChildWatcher never attached to a loop is not active. Either way the
    # stock spawn runs and keeps its own behaviour.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        policy = asyncio.get_event_loop_policy()
        previous = policy._watcher  # type: ignore[attr-defined]
        watcher = getattr(asyncio, kind)()
        policy._watcher = watcher  # type: ignore[attr-defined]
    loop = offloop.OffLoopSpawnUnixSelectorEventLoop()
    if kind == "FastChildWatcher":
        watcher.attach_loop(loop)  # active, so only its kind can refuse it

    async def check() -> tuple[bool, object]:
        return watcher.is_active(), offloop._posix_child_watcher(loop)

    try:
        active, chosen = loop.run_until_complete(check())
        assert active is (kind == "FastChildWatcher")
        assert chosen is None
    finally:
        loop.close()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            watcher.close()
            policy._watcher = previous  # type: ignore[attr-defined]
