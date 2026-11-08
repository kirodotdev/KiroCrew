"""End to end: the ``work`` board as ``slot_fold`` deltas, and every older tab unchanged.

The real path: an aiohttp server running the real ``/api/ws`` handler and the real
work-board REST read, a real ``DashboardState`` and hub, the real crew-log exporter on
the real bus, real session logs on disk and the real eager folder.

Three tabs are open on one board:

- NEW: declares ``fold_delta.1`` and ``fold_delta.2``. Its hello names ``delta``, and
  it gets ``slot_fold`` frames for ``work``. Starting from the REST snapshot's
  ``published_revision`` and applying each delta, it ends equal to the value the other
  tabs were sent.
- WATCH-ONLY: declares ``fold_delta.1`` alone, as the bundle that shipped the watch
  did. Its hello names no ``delta`` and it gets whole-value ``slot_projection`` frames.
- LEGACY: declares neither, and gets whole values for every board, as before.

Then the gateway "restarts" its published bases (a new process holds none): the next
frame to the new tab is ``snapshot_needed``, and a fresh REST read re-bases it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app
from crew_log_fold_eager_helpers import _slot_log, _work_entry
from test_fold_watch_e2e import (  # noqa: F401 -- fixtures register by import
    _FENCE,
    _frames_until_fence,
    _isolated_home,
    e2e_state,
)

from kiro_crew.crew_log import eager, fold_delta, session_tree_projection
from kiro_crew.dashboard import token_auth
from kiro_crew.dashboard.handlers.crew_log import (
    DELTA_FRAME,
    SLOT_FRAME,
    CrewLogPublisher,
    api_session_crew_log_projection,
)
from kiro_crew.dashboard.websocket_hub import (
    FOLD_APPLY_CAPABILITY,
    FOLD_DELTA_CAPABILITY,
    FOLD_HELLO_FRAME,
)
from kiro_crew.testing.wait import async_wait_until, finished

BOARD = "board-a"
UNIT = "s-d-a"


async def _report(handle, item: str) -> None:
    """One ``work/recorded`` entry on the board's own unit, folded and published."""
    seq = _work_entry(handle, item, slot=BOARD)
    eager.note_commit(UNIT, "work/recorded", seq, board=BOARD)
    assert await asyncio.to_thread(eager.drain, 10.0)


def _of(frames: list[dict], kind: str) -> list[dict]:
    return [f["data"] for f in frames if f.get("type") == kind and f["data"].get("fold") == "work"]


async def _hello(tab) -> dict:
    assert (await finished(tab.receive_json(), "snapshot"))["type"] == "slots"
    while True:
        frame = await finished(tab.receive_json(), "hello")
        if frame["type"] == FOLD_HELLO_FRAME:
            return frame["data"]


@pytest.mark.asyncio
async def test_a_delta_tab_rebuilds_the_board_and_older_tabs_get_whole_values(
    e2e_state, monkeypatch  # noqa: F811 -- the fixture imported above
):
    from kiro_crew.dashboard.ws import api_ws

    monkeypatch.setattr(
        session_tree_projection,
        "projection",
        lambda: SimpleNamespace(seeded_for_current_store=True),
    )
    eager.reset_published_for_tests()
    app = _make_app(e2e_state)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    app["allowed_origins"] = set()
    app.router.add_get("/api/ws", api_ws)
    app.router.add_get(
        "/api/sessions/{id}/crew-log/projection/{name}", api_session_crew_log_projection
    )
    async with TestClient(TestServer(app)) as client:
        publisher = CrewLogPublisher(e2e_state)
        publisher.bind(asyncio.get_running_loop(), e2e_state)
        publisher.subscribe_bus()
        try:
            origin = str(client.make_url("/")).rstrip("/")
            app["allowed_origins"].add(origin)
            client.session.headers["Origin"] = origin
            token = token_auth.generate_token("local-app")

            async def snapshot() -> dict:
                # Addressed by the board unit, whose header names the board.
                resp = await client.get(
                    f"/api/sessions/{UNIT}/crew-log/projection/work", params={"token": token}
                )
                assert resp.status == 200, await resp.text()
                return await resp.json()

            new = await client.ws_connect(
                "/api/ws",
                params={
                    "token": token,
                    "caps": f"slot_patch,{FOLD_DELTA_CAPABILITY},{FOLD_APPLY_CAPABILITY}",
                },
            )
            watch_only = await client.ws_connect(
                "/api/ws", params={"token": token, "caps": f"slot_patch,{FOLD_DELTA_CAPABILITY}"}
            )
            assert await _hello(new) == {"v": 1, "folds": ["work"], "delta": ["work"]}
            assert await _hello(watch_only) == {"v": 1, "folds": ["work"]}
            for tab in (new, watch_only):
                await tab.send_json({"type": "fold_watch", "slots": [BOARD]})
            legacy = await client.ws_connect(
                "/api/ws", params={"token": token, "caps": "slot_patch"}
            )
            assert (await finished(legacy.receive_json(), "snapshot"))["type"] == "slots"
            await async_wait_until(
                lambda: publisher.slot_subscription_cells() == {("*", "*")},
                describe=publisher.slot_subscription_cells,
            )

            # First publish of the board in this process: no base, so snapshot_needed.
            handle = _slot_log(UNIT, BOARD)
            await _report(handle, "it-1")
            e2e_state.push_refresh(_FENCE)
            first = _of(await _frames_until_fence(new), DELTA_FRAME)
            assert [f["kind"] for f in first] == ["snapshot_needed"]
            assert "ops" not in first[0]
            await _frames_until_fence(watch_only)
            await _frames_until_fence(legacy)

            # The tab pulls its snapshot, labelled with the base the next delta uses.
            snap = await snapshot()
            assert snap["published_revision"] == first[0]["revision"]
            held, base = snap["value"], snap["published_revision"]

            for item in ("it-2", "it-3"):
                await _report(handle, item)
            e2e_state.push_refresh(_FENCE)
            new_frames = await _frames_until_fence(new)
            whole_watch = _of(await _frames_until_fence(watch_only), SLOT_FRAME)
            whole_legacy = _of(await _frames_until_fence(legacy), SLOT_FRAME)
            assert _of(new_frames, SLOT_FRAME) == [], "the new tab gets no whole work value"
            deltas = _of(new_frames, DELTA_FRAME)
            assert [f["kind"] for f in deltas] == ["delta", "delta"]
            for frame in deltas:
                assert frame["v"] == 1 and frame["base_revision"] == base
                held = fold_delta.apply_work(held, frame["ops"])
                assert held is not None
                base = frame["revision"]
            assert whole_watch == whole_legacy and len(whole_watch) == 2
            assert held == whole_legacy[-1]["value"], "deltas rebuilt the board exactly"
            assert [f["revision"] for f in whole_legacy] == [f["revision"] for f in deltas]
            assert all(f.get("kind") is None for f in whole_legacy), "old frame shape"

            # A restarted gateway holds no base: the next frame asks for a snapshot, and
            # a fresh read re-labels the board.
            eager.reset_published_for_tests()
            await _report(handle, "it-4")
            e2e_state.push_refresh(_FENCE)
            after = _of(await _frames_until_fence(new), DELTA_FRAME)
            assert [f["kind"] for f in after] == ["snapshot_needed"]
            again = await snapshot()
            assert again["published_revision"] == after[0]["revision"]
            assert [i["item_id"] for i in again["value"]["items"]] == [
                i["item_id"] for i in held["items"]
            ] + ["it-4"]
            for tab in (new, watch_only, legacy):
                await tab.close()
        finally:
            publisher.unsubscribe_bus()
