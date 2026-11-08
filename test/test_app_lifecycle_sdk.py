"""``ctx.lifecycle``: an app's own switch, and admission under its own lifecycle lock.

The install records are a dict behind ``apps.manager.is_app_enabled``, patched through
the facade, so every test can say exactly which app's record was read. The lifecycle
lock is the gateway's real ``apps.manager.app_lifecycle_lock``, so a test that holds it
stands for a disable in progress.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import threading
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps import lifecycle_sdk, manager
from kiro_crew.apps.context import build_app_context
from kiro_crew.apps.lifecycle_sdk import (
    DEFAULT_ADMIT_TIMEOUT_SEC,
    MAX_ADMIT_TIMEOUT_SEC,
    Admission,
    AppLifecycleSDK,
)
from kiro_crew.testing.wait import await_until, finished, until_parked

APP = "lifecycle-probe"
OTHER = "lifecycle-other"
#: Bounds the worker thread a hung check parks, so it ends even if the test fails
#: before releasing it.
_THREAD_PARK_SEC = 10.0


class _Records(dict[str, bool]):
    """``app name -> enabled``; a name with no entry has no install record."""

    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch) -> _Records:
    state = _Records()

    def _is_app_enabled(name: str) -> bool:
        state.reads.append(name)
        return state.get(name, False)

    monkeypatch.setattr(manager, "is_app_enabled", _is_app_enabled)
    return state


def test_every_built_context_carries_the_seam_for_its_own_app(tmp_path: Path) -> None:
    ctx = build_app_context(APP, tmp_path)
    assert isinstance(ctx.lifecycle, AppLifecycleSDK)
    assert ctx.lifecycle.app_name == APP


def test_is_enabled_reads_only_the_contexts_own_record(records: _Records) -> None:
    records[OTHER] = True
    assert AppLifecycleSDK(APP).is_enabled() is False
    records[APP] = True
    assert AppLifecycleSDK(APP).is_enabled() is True
    assert records.reads == [APP, APP]


def test_no_method_can_name_another_app() -> None:
    """The negative half of the seam: nothing an app passes picks the app it acts for."""
    public = {
        name: member
        for name, member in inspect.getmembers(AppLifecycleSDK)
        if not name.startswith("_") and callable(member)
    }
    assert set(public) == {"is_enabled", "admit"}
    params = {
        name: set(inspect.signature(member).parameters) - {"self"}
        for name, member in public.items()
    }
    assert params == {"is_enabled": set(), "admit": {"check", "timeout"}}
    assert isinstance(inspect.getattr_static(AppLifecycleSDK, "app_name"), property)


@pytest.mark.asyncio
async def test_a_disabled_app_is_not_admitted_and_its_check_never_runs(
    records: _Records,
) -> None:
    records[OTHER] = True
    ran: list[bool] = []
    result = await AppLifecycleSDK(APP).admit(lambda: ran.append(True))
    assert result == Admission(admitted=False)
    assert ran == []
    assert records.reads == [APP]


@pytest.mark.asyncio
async def test_an_enabled_app_runs_its_check_off_the_loop_under_its_own_lock(
    records: _Records,
) -> None:
    records[APP] = True
    loop_thread = threading.get_ident()
    seen: dict[str, object] = {}

    def _check() -> int:
        seen["thread"] = threading.get_ident()
        # Off any loop, ``locked`` reports whether any loop holds the lock.
        seen["own_locked"] = manager.app_lifecycle_lock(APP).locked()
        seen["other_locked"] = manager.app_lifecycle_lock(OTHER).locked()
        return 7

    result = await AppLifecycleSDK(APP).admit(_check)
    assert result == Admission(admitted=True, value=7)
    assert seen == {"thread": seen["thread"], "own_locked": True, "other_locked": False}
    assert seen["thread"] != loop_thread
    assert not manager.app_lifecycle_lock(APP).locked()


@pytest.mark.asyncio
async def test_admission_waits_for_a_disable_that_holds_the_lock(
    records: _Records,
) -> None:
    records[APP] = True
    holding = asyncio.Event()
    release = asyncio.Event()

    async def _disable() -> None:
        async with manager.app_lifecycle_lock(APP):
            holding.set()
            await await_until(release, what="the test releasing the disable")
            records[APP] = False

    disable = asyncio.create_task(_disable())
    await await_until(holding, what="the disable taking the lock")
    admit = asyncio.create_task(AppLifecycleSDK(APP).admit(lambda: "started"))
    # The admission parks on the lock the disable holds; it must still be waiting there.
    await until_parked(manager.app_lifecycle_lock(APP))
    assert not admit.done()
    release.set()
    await await_until(disable, what="the disable finishing")
    assert await await_until(admit, what="the admission answering") == Admission(admitted=False)


class _DeadlineOnceEntered:
    """``asyncio`` as ``lifecycle_sdk`` sees it, whose ``wait_for`` deadline passes
    only once the check has signalled entry, so a slow worker start cannot pass it
    first. The cancel and the ``TimeoutError`` are what a real deadline delivers."""

    def __init__(self, entered: asyncio.Event) -> None:
        self._entered = entered
        self.timeouts: list[float] = []

    def __getattr__(self, name: str) -> object:
        return getattr(asyncio, name)

    async def wait_for(self, aw: Any, timeout: float) -> Any:
        self.timeouts.append(timeout)
        task = asyncio.ensure_future(aw)
        await await_until(self._entered, what="the check signalling entry")
        if task.done():
            return task.result()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise asyncio.TimeoutError


@pytest.mark.asyncio
async def test_a_check_that_hangs_cannot_keep_the_apps_disable_waiting(
    records: _Records, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound: past the deadline the lock is free again, whatever the check does."""
    records[APP] = True
    unblock = threading.Event()
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    deadline = _DeadlineOnceEntered(entered)
    monkeypatch.setattr(lifecycle_sdk, "asyncio", deadline)

    def _hang() -> str:
        loop.call_soon_threadsafe(entered.set)
        unblock.wait(_THREAD_PARK_SEC)
        return "late"

    try:
        result = await AppLifecycleSDK(APP).admit(_hang)
        assert result == Admission(admitted=False, timed_out=True)
        assert entered.is_set()
        assert deadline.timeouts == [DEFAULT_ADMIT_TIMEOUT_SEC]
        lock = manager.app_lifecycle_lock(APP)
        assert not lock.locked()
        await finished(lock.acquire(), "acquiring the freed lock")
        lock.release()
    finally:
        unblock.set()


@pytest.mark.asyncio
async def test_a_call_from_inside_the_apps_own_disable_times_out_instead_of_deadlocking(
    records: _Records,
) -> None:
    """``on_shutdown`` runs under the lock, which is not re-entrant."""
    records[APP] = True
    async with manager.app_lifecycle_lock(APP):
        result = await AppLifecycleSDK(APP).admit(
            timeout=0.05
        )  # flake-ok: the deadline is the subject
    assert result == Admission(admitted=False, timed_out=True)


@pytest.mark.asyncio
async def test_a_check_that_raises_propagates_and_releases_the_lock(
    records: _Records,
) -> None:
    records[APP] = True

    def _boom() -> None:
        raise ValueError("check failed")

    with pytest.raises(ValueError, match="check failed"):
        await AppLifecycleSDK(APP).admit(_boom)
    assert not manager.app_lifecycle_lock(APP).locked()


@pytest.mark.parametrize(
    ("asked", "used"),
    [
        (5, 5.0),
        (0.5, 0.5),
        (MAX_ADMIT_TIMEOUT_SEC * 100, MAX_ADMIT_TIMEOUT_SEC),
        (float("inf"), MAX_ADMIT_TIMEOUT_SEC),
        (0, DEFAULT_ADMIT_TIMEOUT_SEC),
        (-1, DEFAULT_ADMIT_TIMEOUT_SEC),
        (float("nan"), DEFAULT_ADMIT_TIMEOUT_SEC),
        (True, DEFAULT_ADMIT_TIMEOUT_SEC),
        ("30", DEFAULT_ADMIT_TIMEOUT_SEC),
        (None, DEFAULT_ADMIT_TIMEOUT_SEC),
    ],
)
def test_an_app_cannot_ask_for_a_longer_hold_than_the_cap(asked: object, used: float) -> None:
    assert lifecycle_sdk._bounded_timeout(asked) == used  # type: ignore[arg-type]
