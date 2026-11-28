"""A Bot chat cold-starting while its app is disabled must not mount the app.

A disable bumps no revocation generation: it holds ``app_stop_fence`` across its
teardown and the disabled-state write. Each test parks the cold start between the
delivery filter (``check_and_filter_projection``) and ``record_mounted`` and lets
the disable happen there. Guarantees and the one-line change that reddens each:

* the filter refuses while a stop span is open -- drop the
  ``_FENCE_HOLD_COUNTS`` clause from its final re-check;
* the record re-reads the app's state under the delivery lock -- drop the
  ``_still_reaches`` clause from ``late_revoked_hold``;
* no state read runs under ``_DELIVERY_LOCK`` -- call ``_still_reaches`` inside it;
* a stop span during the off-lock read voids -- drop the ``_STOP_EPOCHS`` stamp.
"""

from __future__ import annotations

import pytest
from test_bot_chat_app_tools import APP, BASE, KEY, _fresh_record, world  # noqa: F401

from kiro_crew import bot_chat_app_tools as tools

ALIAS = f"{APP}:team"


def _filtered() -> dict:
    out = tools.conductor_session_spec(dict(BASE), KEY)
    return tools.check_and_filter_projection(KEY, dict(out["mcpServers"]))


@pytest.mark.asyncio
async def test_the_filter_refuses_an_app_whose_stop_span_is_open(world):  # noqa: F811
    async with tools.app_stop_fence(APP):
        handed = _filtered()
        world["scopes"] = set()  # the disable lands while the span is open
    assert ALIAS not in handed
    voided = tools.record_mounted(KEY, tools.mounted_names(handed))
    assert ALIAS not in tools._MOUNTED.get("atlas", {})
    assert not voided  # nothing to strip: it was never handed


@pytest.mark.asyncio
async def test_a_disable_finishing_between_filter_and_record_is_not_recorded(world):  # noqa: F811
    handed = _filtered()  # the gate passes: the app is still enabled
    assert ALIAS in handed
    async with tools.app_stop_fence(APP):  # the whole disable runs here
        world["scopes"] = set()
    voided = tools.record_mounted(KEY, tools.mounted_names(handed))
    assert APP in voided  # the caller strips it before session/new
    assert ALIAS not in tools._MOUNTED.get("atlas", {})


def test_no_state_read_runs_under_the_delivery_lock(world, monkeypatch):  # noqa: F811
    """app_stop_fence takes ``_DELIVERY_LOCK`` on the event loop, so a disk read
    (Windows retries with sleeps) under it would stall the gateway. Reddens when
    ``record_mounted`` reads the app state inside the lock."""
    from kiro_crew.apps import bot_grants

    under_lock: list[str] = []
    real = bot_grants.app_granted_bots

    def _granted(app):
        if tools._DELIVERY_LOCK.locked():
            under_lock.append(app)
        return real(app)

    monkeypatch.setattr(bot_grants, "app_granted_bots", _granted)
    handed = _filtered()
    voided = tools.record_mounted(KEY, tools.mounted_names(handed))
    assert under_lock == []
    assert not voided and ALIAS in tools._MOUNTED["atlas"]


@pytest.mark.asyncio
async def test_a_stop_span_during_the_off_lock_read_voids_the_record(
    world, monkeypatch  # noqa: F811
):
    """The read off the lock saw the app still ticked, but a whole disable ran while
    it read: the span's stamp moved, so the record voids. Reddens without the
    ``_STOP_EPOCHS`` comparison in ``_stop_raced``."""
    import asyncio

    from kiro_crew.apps import bot_grants

    real = bot_grants.app_granted_bots
    loop = asyncio.get_running_loop()

    def _granted(app):
        got = real(app)  # read before the disable's state write

        async def _disable():
            async with tools.app_stop_fence(app):
                world["scopes"] = set()

        asyncio.run_coroutine_threadsafe(_disable(), loop).result(timeout=30)
        return got

    handed = _filtered()
    monkeypatch.setattr(bot_grants, "app_granted_bots", _granted)
    voided = await asyncio.to_thread(tools.record_mounted, KEY, tools.mounted_names(handed))
    assert APP in voided
    assert ALIAS not in tools._MOUNTED.get("atlas", {})


@pytest.mark.asyncio
async def test_a_stop_fence_entered_while_a_cold_start_reads_app_state_does_not_block_the_loop(
    world, monkeypatch  # noqa: F811
):
    """A cold start's ``record_mounted`` is parked inside ``_app_reaches`` (its disk
    read) when a disable enters ``app_stop_fence`` from the event loop. The fence
    must be entered, and the loop keep running, while the read is still held.
    Reddens when ``record_mounted`` reads the app state under ``_DELIVERY_LOCK``
    (the fence then waits on the lock, the loop stalls, and the read is released
    only by its own bounded wait)."""
    import asyncio
    import threading

    from kiro_crew.testing.wait import await_until

    handed = _filtered()
    loop = asyncio.get_running_loop()
    reading, release = asyncio.Event(), threading.Event()
    seen: dict[str, bool] = {}
    real = tools._app_reaches

    def _held_read(app, slug):
        loop.call_soon_threadsafe(reading.set)
        seen["released"] = release.wait(timeout=10)  # bounded: a stalled loop ends it
        return real(app, slug)

    monkeypatch.setattr(tools, "_app_reaches", _held_read)
    cold_start = asyncio.ensure_future(
        asyncio.to_thread(tools.record_mounted, KEY, tools.mounted_names(handed))
    )
    await await_until(reading, what="the cold start parked inside its app-state read")
    async with tools.app_stop_fence(APP):
        await asyncio.sleep(0)  # the loop still ticks
        release.set()  # only now may the parked read finish
        voided = await await_until(cold_start, what="the cold start's record")
    assert seen["released"] is True  # the fence was entered while the read was held
    assert APP in voided  # the span open at the record voids the mount
