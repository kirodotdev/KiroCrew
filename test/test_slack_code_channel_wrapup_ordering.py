"""An older wrap-up's late write never replaces a newer wrap-up's state."""

from __future__ import annotations

import asyncio
from typing import Any

from slack_code_channel_surface_helpers import env_fixture  # noqa: F401 - fixture by name
from slack_code_channel_surface_helpers import CH, _bind, _calls, _click, _wrap  # noqa: F401
from slack_code_channels_helpers import OWNER

from kiro_crew.slack.code_channels import wrapup
from kiro_crew.slack.code_channels.surface_store import store as surface_store
from kiro_crew.testing.wait import async_wait_until, await_until


class _WatchedLock(asyncio.Lock):
    """The channel's lock, noting whether a later acquire had to wait for it."""

    contended = False

    async def acquire(self) -> bool:
        if self.locked():
            self.contended = True
        return await super().acquire()


def _pointers(env: Any) -> list[str]:
    return [
        c["code_channel"]["summary_message"]["message_ts"]
        for c in _calls(env, "agents.conversations.setProperties")
        if "summary_message" in (c.get("code_channel") or {})
    ]


def test_an_older_wrap_ups_late_pointer_never_replaces_the_newer_one(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    real_call = env.api.api_call
    held, release = asyncio.Event(), asyncio.Event()

    async def api_call(method: str, **kwargs: Any) -> Any:
        body = kwargs.get("json") or kwargs
        pointer = "summary_message" in str(body)
        if method == "agents.conversations.setProperties" and pointer and not held.is_set():
            held.set()  # the older wrap-up's pointer write, held in flight
            await release.wait()
        return await real_call(method, **kwargs)

    env.api.api_call = api_call

    async def scenario() -> None:
        lock = _WatchedLock()
        monkeypatch.setitem(wrapup._locks, CH, lock)  # dropped again at teardown
        older = asyncio.ensure_future(wrapup.post_wrap_up(binding, "older", caller=OWNER))
        await await_until(held, what="the older wrap-up's pointer write")
        newer = asyncio.ensure_future(wrapup.post_wrap_up(binding, "newer", caller=OWNER))
        # Either the newer one ran to its end, or it waits for the older one.
        await async_wait_until(lambda: newer.done() or lock.contended)
        release.set()
        await await_until(asyncio.gather(older, newer), what="both wrap-ups")

    asyncio.run(scenario())
    newest = surface_store().get(CH).summary_ts
    assert len(env.slack.blocks_posts) == 2
    assert newest == f"300.{len(env.slack.blocks_posts):06d}"  # the newer post's ts
    assert _pointers(env)[-1] == newest


def test_a_stale_cancel_never_disarms_a_newer_wrap_up(env: Any, monkeypatch: Any) -> None:
    _bind(env)
    real_update = wrapup.update_async
    held, release = asyncio.Event(), asyncio.Event()

    async def update_async(channel: str, **changes: Any) -> Any:
        if changes.get("wrapup_pending_since") == 0.0 and not held.is_set():
            held.set()  # the older request's clear, held in flight
            await release.wait()
        return await real_update(channel, **changes)

    monkeypatch.setattr(wrapup, "update_async", update_async)

    async def scenario() -> str:
        lock = _WatchedLock()
        monkeypatch.setitem(wrapup._locks, CH, lock)  # dropped again at teardown
        older = await wrapup.request_wrap_up(CH, OWNER)
        cancel = asyncio.ensure_future(wrapup.cancel_wrap_up(CH, older))
        await await_until(held, what="the older request's clear")
        newer = asyncio.ensure_future(wrapup.request_wrap_up(CH, OWNER))
        await async_wait_until(lambda: newer.done() or lock.contended)
        release.set()
        await await_until(cancel, what="the cancel")
        return await await_until(newer, what="the newer request")

    newer_tag = asyncio.run(scenario())
    assert wrapup._requests.get(CH) == newer_tag
    assert surface_store().get(CH).wrapup_pending_since > 0  # the newer request stays armed


def test_no_pointer_is_sent_once_the_stored_summary_is_another(env: Any, monkeypatch: Any) -> None:
    binding = _bind(env)
    real_mark = wrapup._mark_wrapped

    def mark_wrapped(channel: str, stamp: float) -> None:
        real_mark(channel, stamp)
        surface_store().update(channel, summary_ts="999.000001")  # someone else's, meanwhile

    monkeypatch.setattr(wrapup, "_mark_wrapped", mark_wrapped)
    assert asyncio.run(wrapup.post_wrap_up(binding, "done", caller=OWNER))
    assert _pointers(env) == []


def test_an_archive_clears_a_pending_wrap_up_under_the_lock_it_holds(env: Any) -> None:
    _bind(env)
    _, ts = _wrap(env)
    asyncio.run(wrapup.request_wrap_up(CH, OWNER))

    async def click() -> bool:
        task = asyncio.ensure_future(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
        return bool(await await_until(task, what="the archive click"))

    assert asyncio.run(click())
    assert env.store.get_binding(CH).archived_at > 0
    assert CH not in wrapup._requests
    assert not surface_store().get(CH).wrapup_pending_since
