"""The connect snapshot's hold flags are the ones the shared pass's rows were taken under.

``_serve_ws`` waits for a trailing shared slot-list pass. A channel reconcile that
publishes its sessions and finishes the rebuild between the pass taking the rows and
the connect resuming must not turn those incomplete rows into a snapshot labelled
complete: the client drains every session the list omits.
"""

from __future__ import annotations

import asyncio
import json
import math

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import slot_list_pass
from kiro_crew.dashboard import ws as ws_mod


@pytest.mark.asyncio
async def test_a_rebuild_finished_after_the_pass_took_its_rows_keeps_the_snapshot_partial(
    tmp_path, monkeypatch
) -> None:
    state = _make_state(tmp_path)
    state.begin_slot_list_rebuild()
    loop = asyncio.get_running_loop()
    # A pass just ended for every audience, so the connect joins the trailing pass.
    for owner in (False, True):
        passes = slot_list_pass._AudiencePasses()
        passes.taken_at = math.inf
        slot_list_pass._PASSES.setdefault(state, {})[(loop, owner, True)] = passes

    def _channels_published_and_rebuild_finished() -> None:
        state.replace_channel_slot_holds([], hold_channels=False)
        state.finish_slot_list_rebuild()

    def _rows_taken_under_a_rebuild(**_kw):
        # Queued before the pass settles its callers, so it runs before they resume.
        loop.call_soon(_channels_published_and_rebuild_finished)
        return [{"key": "restored-before-the-channels"}]

    monkeypatch.setattr(state, "serialize_slots", _rows_taken_under_a_rebuild)

    @web.middleware
    async def _as_the_dashboard_user(request, handler):
        request["app"] = ""
        request["is_dashboard_user"] = True
        return await handler(request)

    app = web.Application(middlewares=[_as_the_dashboard_user])
    app["state"] = state
    app["allowed_origins"] = set()
    app.router.add_get("/api/ws", ws_mod.api_ws)
    async with TestClient(TestServer(app)) as client:
        origin = str(client.make_url("/")).rstrip("/")
        app["allowed_origins"].add(origin)
        client.session.headers["Origin"] = origin
        sock = await client.ws_connect("/api/ws")
        frame = json.loads((await sock.receive(timeout=30)).data)  # flake-ok: caps a hang
        await sock.close()

    assert state.slot_list_rebuilding is False, "the race did not happen"
    assert frame["type"] == "slots"
    assert frame["slotsPartial"] is True, "incomplete rows were labelled complete"
    assert frame.get("slotsHoldChannels") is True


@pytest.mark.asyncio
async def test_the_pass_carries_the_holds_it_was_taken_under(tmp_path) -> None:
    state = _make_state(tmp_path)
    state.begin_slot_list_rebuild()
    state.replace_channel_slot_holds(["channel-1"], hold_channels=True)
    expected = (tuple(state.slot_list_held_keys()), True)

    taken = await slot_list_pass.shared_slot_pass(state, dashboard_user=True)
    state.replace_channel_slot_holds([], hold_channels=False)
    state.finish_slot_list_rebuild()

    assert taken.partial is True
    assert (taken.held_keys, taken.hold_channels) == expected
    assert "channel-1" in taken.held_keys
