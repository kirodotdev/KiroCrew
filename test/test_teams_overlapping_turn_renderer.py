"""A Teams message admitted while an earlier turn on its key runs keeps that turn's renderer.

Two activities on one conversation are handled concurrently, and both can pass the
busy check before either holds the session. The renderer registry is what an
approval click or an ``[OPTIONS:]`` chip resolves against, so the turn that holds
the session must own the slot until the next turn takes the session from it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from test_teams_commands_dispatch import _EMAIL, _Client, _dispatcher, _inbound

from kiro_crew.testing.wait import await_until


@pytest.mark.asyncio
async def test_a_waiting_turn_does_not_take_the_running_turns_renderer(monkeypatch) -> None:
    renderers: dict[str, object] = {}
    a_running = asyncio.Event()
    b_waiting = asyncio.Event()
    a_done = asyncio.Event()
    release_a = asyncio.Event()
    seen_by_a: list[object] = []

    async def _fake_drive(turn, **kw):
        provider = SimpleNamespace(work_scratch_dir="")
        if turn.user_text == "first":
            # A holds the session: the pipeline binds its provider, then the turn runs.
            turn.bind_provider(provider)
            renderers["a"] = turn.renderer
            a_running.set()
            await await_until(release_a, what="A released")
            seen_by_a.append(d._active_renderers.get(turn.session_key))
            a_done.set()
        else:
            # B was admitted but waits for the session A holds.
            renderers["b"] = turn.renderer
            b_waiting.set()
            await await_until(a_done, what="A finished")
            turn.bind_provider(provider)

    async def _permitted(_c, **_kw):
        return True

    monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", _fake_drive)
    monkeypatch.setattr("kiro_crew.teams.transport_dispatch.inbound_permitted", _permitted)
    d = _dispatcher(_Client())

    first = asyncio.ensure_future(d.handle_message(_inbound("first")))
    await await_until(a_running, what="A running")
    second = asyncio.ensure_future(d.handle_message(_inbound("second")))
    await await_until(b_waiting, what="B waiting")

    key = d._session_key(_EMAIL)
    assert d._active_renderers.get(key) is renderers["a"]
    release_a.set()
    await await_until(asyncio.gather(first, second), what="both turns")
    # A's approval clicks resolved against A's own renderer for the whole turn.
    assert seen_by_a == [renderers["a"]]


@pytest.mark.asyncio
async def test_a_hook_reply_with_chips_takes_an_idle_slot(monkeypatch) -> None:
    """A reply an ``on_message`` hook answered never binds a provider, so its chips
    must still replace an earlier reply's once no turn holds the session."""

    async def _fake_drive(turn, **kw):
        await turn.renderer.on_text_chunk("pick one\n[OPTIONS: yes | no]")
        await turn.renderer.on_done()

    async def _permitted(_c, **_kw):
        return True

    monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", _fake_drive)
    monkeypatch.setattr("kiro_crew.teams.transport_dispatch.inbound_permitted", _permitted)
    d = _dispatcher(_Client())
    key = d._session_key(_EMAIL)
    older = SimpleNamespace(has_pending_choices=True)
    d._active_renderers[key] = older

    await d.handle_message(_inbound("again"))

    assert d._active_renderers[key] is not older
    assert d._active_renderers[key].has_pending_choices
