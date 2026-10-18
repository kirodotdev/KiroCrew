"""The git update route's install sees its caller's cancel before pip starts.

The gateway auto-apply and the dashboard ``POST /api/update`` run the reinstall
on an executor thread, which a cancelled await cannot stop. The install's cancel
event closes the part of that race that is safe to close: once the caller has
stopped (a gateway shutdown cancels it), an install still in its probes never
starts pip. A pip already running is left to finish, because killing it mid
install is what leaves the venv half rewritten.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import dep_sync
from kiro_crew.testing.wait import await_until


def _guards_open():
    """Open the ownership/lock/floor guards: they have their own tests."""
    return (
        patch.object(dep_sync, "installed_package_origin", return_value="<stub>"),
        patch.object(dep_sync, "venv_not_mapped_to", return_value=None),
        patch.object(dep_sync, "locked_console_scripts", return_value=[]),
        patch.object(dep_sync, "requires_python", return_value=None),
    )


def _no_pip(*_a, **_k):
    raise AssertionError("pip must not start after the cancel")


@pytest.mark.parametrize("locked", [False, True], ids=["reinstall", "dependency-sync"])
def test_a_set_cancel_starts_no_pip_and_reports_nothing_changed(tmp_path, locked):
    stop = threading.Event()
    stop.set()
    messages: list[tuple[str, bool]] = []
    g = _guards_open()
    with (
        g[0],
        g[1],
        g[2],
        g[3],
        patch.object(
            dep_sync, "locked_console_scripts", return_value=["kirocrew"] if locked else []
        ),
        patch.object(dep_sync, "sync", side_effect=_no_pip),
        patch.object(dep_sync.subprocess, "run", side_effect=_no_pip),
    ):
        rc = dep_sync.sync_or_reinstall(
            tmp_path, Path("/venv/bin/python"), lambda m, e: messages.append((m, e)), cancel=stop
        )

    # REFUSED: the callers then skip the repair install meant for a run that ran.
    assert rc == dep_sync.REFUSED
    assert any("cancelled before pip started" in m for m, _ in messages)


@pytest.mark.asyncio
async def test_cancelling_the_supervised_install_during_its_probes_starts_no_pip(tmp_path):
    """A shutdown cancel that lands while the probes run must reach the thread."""
    loop = asyncio.get_running_loop()
    in_probe = asyncio.Event()
    finished: asyncio.Future[int] = loop.create_future()
    release = threading.Event()
    pip_started = []
    real = dep_sync.sync_or_reinstall

    def slow_origin(_py):
        loop.call_soon_threadsafe(in_probe.set)
        # Released by the test's `finally`; the bound only keeps a test that
        # failed before reaching it from pinning the executor thread for good.
        if not release.wait(timeout=120):
            raise AssertionError("the probe was never released")
        return "<stub>"

    def record_pip(argv, **_k):
        pip_started.append(argv)
        raise AssertionError("pip started after the cancel")

    def observed(*a, **k):
        try:
            rc = real(*a, **k)
        except BaseException as exc:  # surfaced through the future below
            loop.call_soon_threadsafe(finished.set_exception, exc)
            raise
        loop.call_soon_threadsafe(finished.set_result, rc)
        return rc

    with (
        patch.object(dep_sync, "sync_or_reinstall", side_effect=observed),
        patch.object(dep_sync, "installed_package_origin", side_effect=slow_origin),
        patch.object(dep_sync, "venv_not_mapped_to", return_value=None),
        patch.object(dep_sync, "locked_console_scripts", return_value=[]),
        patch.object(dep_sync, "requires_python", return_value=None),
        patch.object(dep_sync.subprocess, "run", side_effect=record_pip),
    ):
        task = asyncio.create_task(
            dep_sync.sync_or_reinstall_supervised(
                None, tmp_path, Path("/venv/bin/python"), lambda m, e: None, timeout=60
            )
        )
        try:
            await await_until(in_probe, what="the install reaching its ownership probe")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            # The caller is gone; let the thread finish its probe and settle.
            release.set()
        rc = await await_until(finished, what="the executor install returning")

    assert rc == dep_sync.REFUSED
    assert pip_started == []


@pytest.mark.asyncio
async def test_the_supervised_install_returns_the_install_result(tmp_path):
    with patch.object(dep_sync, "sync_or_reinstall", return_value=0) as install:
        rc = await dep_sync.sync_or_reinstall_supervised(
            None, tmp_path, Path("/venv/bin/python"), lambda m, e: None, timeout=42
        )
    assert rc == 0
    assert install.call_args.kwargs["timeout"] == 42
    assert isinstance(install.call_args.kwargs["cancel"], threading.Event)
