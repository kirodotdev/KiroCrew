"""Taking back a steer the running turn has not read yet (``dashboard/steer_retract.py``).

The take-back is asynchronous: ``POST /api/chat/slots/{slot}/steers/{mid}/retract``
arms it and writes kiro-cli's ``_session/steer/clear``, and what the turn reports
next decides the outcome. These tests drive each report directly:

* ``on_dropped`` stands for ``EVENT_STEER_DROPPED`` (kiro-cli's
  ``AgentExecutionUserMessageCleared``);
* ``_settle_consumed_steers`` for ``EVENT_STEER_CONSUMED``;
* ``_requeue_unconsumed_steers`` for the turn's teardown.

The steers themselves go through the real composer path (``POST /api/chat`` with
``steer``), so the row a take-back reads is the row the steer path persisted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import steer_requeue_helpers
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state
from steer_requeue_helpers import _running_slot

from kiro_crew.acp.types import wrap_user_message
from kiro_crew.dashboard import steer_retract
from kiro_crew.dashboard.chat_delivery import (
    STEER_RETRACTABLE_META,
    STEER_STATE_CONSUMED,
    STEER_STATE_REQUEUED,
    STEER_STATE_RETRACTED,
    STEER_STATE_WRITTEN,
)
from kiro_crew.testing.wait import async_wait_until, await_until

_patch_sel = steer_requeue_helpers._patch_sel


def _app(state, *, app_id: str = ""):
    """The chat test app plus the take-back route; *app_id* makes it an app's view."""
    from aiohttp import web

    from kiro_crew.dashboard.chat_handlers import api_chat_slot_steer_retract

    app = _make_app(state)
    app.router.add_post("/api/chat/slots/{slot}/steers/{mid}/retract", api_chat_slot_steer_retract)
    if app_id:

        @web.middleware
        async def as_app(request, handler):
            request["app"] = app_id
            return await handler(request)

        # Ahead of the test auth middleware, which keeps an identity already set.
        app.middlewares.insert(0, as_app)
    return app


def _retract_client(*, clear_answer: object = True) -> MagicMock:
    """A live client that takes steers and can take one back."""
    client = MagicMock()
    client.supports_steer = True
    client.supports_steer_retract = True
    client.steer = AsyncMock(return_value=True)
    client.clear_pending_steers = AsyncMock(return_value=clear_answer)
    return client


@pytest.fixture
def harness(tmp_path, monkeypatch, _patch_sel):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = _running_slot(state)
    slot._acp_client = _retract_client()
    return SimpleNamespace(state=state, slot=slot, app=_app(state))


def _steer_rows(slot) -> list[dict]:
    return [m for m in slot.messages if (m.get("meta") or {}).get("steer") is True]


def _mid_of(slot, text: str) -> str:
    row = next(m for m in _steer_rows(slot) if m["content"] == text)
    return row["meta"]["mid"]


def _frames(state, kind: str) -> list[dict]:
    return [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == kind]


async def _steer(http, text: str) -> None:
    resp = await http.post("/api/chat", json={"slot": "test", "message": text, "steer": True})
    assert resp.status == 200
    assert (await resp.json()).get("steered") is True


async def _retract(http, mid: str, action: str):
    return await http.post(f"/api/chat/slots/test/steers/{mid}/retract", json={"action": action})


class TestTheOffer:
    """Only a steer that can be taken back says so on its row."""

    @pytest.mark.asyncio
    async def test_a_composer_steer_on_a_capable_harness_is_offered(self, harness):
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "use the blue fleet")

        (row,) = _steer_rows(harness.slot)
        assert row["meta"]["steerState"] == STEER_STATE_WRITTEN
        assert row["meta"][STEER_RETRACTABLE_META] is True
        (push,) = _frames(harness.state, "steer_push")
        assert push[STEER_RETRACTABLE_META] is True

    @pytest.mark.asyncio
    async def test_a_harness_without_the_clear_is_not_offered(self, harness):
        harness.slot._acp_client.supports_steer_retract = False
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "use the blue fleet")

        (row,) = _steer_rows(harness.slot)
        assert STEER_RETRACTABLE_META not in row["meta"]
        (push,) = _frames(harness.state, "steer_push")
        assert STEER_RETRACTABLE_META not in push

    @pytest.mark.asyncio
    async def test_a_peer_steer_is_not_offered(self, harness):
        from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn

        outcome = await steer_into_running_turn(
            harness.state, harness.slot, "from a peer", user_origin=False, admission={}
        )

        assert outcome == STEER_STEERED
        (row,) = _steer_rows(harness.slot)
        assert STEER_RETRACTABLE_META not in row["meta"]


class TestTheRoute:
    """What the route refuses changes nothing; what it accepts writes the clear."""

    @pytest.mark.asyncio
    async def test_accepting_writes_the_clear_and_changes_nothing_yet(self, harness):
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "use the blue fleet")
            resp = await _retract(http, _mid_of(harness.slot, "use the blue fleet"), "unsend")

            assert resp.status == 202
            assert await resp.json() == {"ok": True, "pending": True}
        harness.slot._acp_client.clear_pending_steers.assert_awaited_once()
        assert harness.slot._pending_steers == ["use the blue fleet"]
        (row,) = _steer_rows(harness.slot)
        assert row["meta"]["steerState"] == STEER_STATE_WRITTEN

    @pytest.mark.asyncio
    async def test_an_unknown_action_is_refused(self, harness):
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "use the blue fleet")
            resp = await _retract(http, _mid_of(harness.slot, "use the blue fleet"), "delete")

            assert resp.status == 400
            assert (await resp.json())["code"] == "steer_retract_bad_action"
        harness.slot._acp_client.clear_pending_steers.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", [[], {}])
    async def test_a_non_string_action_is_refused(self, harness, action):
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "use the blue fleet")
            resp = await http.post(
                f"/api/chat/slots/test/steers/{_mid_of(harness.slot, 'use the blue fleet')}/retract",
                json={"action": action},
            )

            assert resp.status == 400
            assert (await resp.json())["code"] == "steer_retract_bad_action"
        harness.slot._acp_client.clear_pending_steers.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_row_that_is_no_pending_steer_is_refused(self, harness):
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "use the blue fleet")
            harness.slot._pending_steers.clear()
            resp = await _retract(http, _mid_of(harness.slot, "use the blue fleet"), "unsend")
            unknown = await _retract(http, "no-such-row", "unsend")

            assert resp.status == 409
            assert (await resp.json())["code"] == "steer_not_pending"
            assert unknown.status == 409
        harness.slot._acp_client.clear_pending_steers.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_steer_still_being_written_makes_it_busy(self, harness):
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "use the blue fleet")
            harness.slot._steer_rpc_in_flight.add("another steer")
            resp = await _retract(http, _mid_of(harness.slot, "use the blue fleet"), "unsend")

            assert resp.status == 409
            assert (await resp.json())["code"] == "steer_retract_busy"
        harness.slot._acp_client.clear_pending_steers.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_second_take_back_waits_for_the_first(self, harness):
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "first")
            await _steer(http, "second")
            first = await _retract(http, _mid_of(harness.slot, "first"), "unsend")
            second = await _retract(http, _mid_of(harness.slot, "second"), "unsend")

            assert first.status == 202
            assert second.status == 409
            assert (await second.json())["code"] == "steer_retract_busy"

    @pytest.mark.asyncio
    async def test_an_app_cannot_take_back_a_steer(self, harness, _patch_sel):
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "use the blue fleet")
        mid = _mid_of(harness.slot, "use the blue fleet")
        harness.slot._app = "some-app"
        async with TestClient(TestServer(_app(harness.state, app_id="some-app"))) as http:
            resp = await _retract(http, mid, "unsend")

            assert resp.status == 403
            assert (await resp.json())["code"] == "steer_retract_forbidden"
        harness.slot._acp_client.clear_pending_steers.assert_not_awaited()
        denied = [
            call.kwargs
            for call in _patch_sel.log_api_access.call_args_list
            if call.kwargs.get("operation") == "slot_steer_retract"
        ]
        assert len(denied) == 1, "the app's refused take-back leaves one audit row"
        assert denied[0]["caller"] == "some-app"
        assert denied[0]["outcome"] == "denied"
        assert denied[0]["source"] == "app_isolation"
        assert denied[0]["resources"] == "slot=test"

    @pytest.mark.asyncio
    async def test_a_refused_clear_withdraws_the_offer(self, harness):
        harness.slot._acp_client.clear_pending_steers = AsyncMock(return_value=False)
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "use the blue fleet")
            resp = await _retract(http, _mid_of(harness.slot, "use the blue fleet"), "unsend")

            assert resp.status == 409
            assert (await resp.json())["code"] == "steer_retract_unsupported"
        (row,) = _steer_rows(harness.slot)
        assert row["meta"]["steerState"] == STEER_STATE_WRITTEN
        assert row["meta"][STEER_RETRACTABLE_META] is False
        assert "steerRetractExpired" not in row["meta"], "a refusal is not an expiry"
        assert harness.slot._steer_retract is None
        assert harness.slot._steer_clear is None
        assert harness.slot._pending_steers == ["use the blue fleet"]

    @pytest.mark.asyncio
    async def test_a_refused_clear_after_turn_end_keeps_the_accepted_edit(self, harness):
        release = asyncio.Event()

        async def delayed_refusal() -> bool:
            await release.wait()
            return False

        harness.slot._acp_client.clear_pending_steers = AsyncMock(side_effect=delayed_refusal)
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "change this")
            mid = _mid_of(harness.slot, "change this")
            retract = asyncio.create_task(_retract(http, mid, "edit"))
            await async_wait_until(lambda: harness.slot._steer_retract is not None)

            steer_retract.on_turn_ending(harness.state, harness.slot)
            release.set()
            response = await await_until(retract, what="the refused clear answering")

            assert response.status == 202
            assert await response.json() == {"ok": True, "pending": True}

        (row,) = _steer_rows(harness.slot)
        assert row["meta"]["steerState"] == STEER_STATE_RETRACTED
        assert row["meta"][steer_retract.RETRACT_ACTION_META] == "edit"
        assert row["content"] == ""
        assert harness.slot._steer_retract is None
        assert harness.slot._steer_clear is None
        updates = _frames(harness.state, "chat_message_update")
        assert len(updates) == 1, "the late refusal does not withdraw the settled row"
        assert updates[0]["meta"]["steerState"] == STEER_STATE_RETRACTED

    @pytest.mark.asyncio
    async def test_a_refused_clear_after_settle_releases_the_clear(self, harness):
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        release = asyncio.Event()
        clear_calls = 0

        async def delayed_refusal() -> bool:
            nonlocal clear_calls
            clear_calls += 1
            if clear_calls == 1:
                await release.wait()
                return False
            return True

        harness.slot._acp_client.clear_pending_steers = AsyncMock(side_effect=delayed_refusal)
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "change this")
            await _steer(http, "still pending")
            retract = asyncio.create_task(
                _retract(http, _mid_of(harness.slot, "change this"), "edit")
            )
            await async_wait_until(lambda: harness.slot._steer_retract is not None)

            _settle_consumed_steers(harness.slot, wrap_user_message("change this"), harness.state)
            release.set()
            response = await await_until(retract, what="the refused clear answering")

            assert response.status == 202
            assert await response.json() == {"ok": True, "pending": True}
            assert harness.slot._steer_clear is None

            next_response = await _retract(http, _mid_of(harness.slot, "still pending"), "unsend")
            assert next_response.status == 202

        rows = {row["content"]: row for row in _steer_rows(harness.slot)}
        assert rows["change this"]["meta"]["steerState"] == STEER_STATE_CONSUMED
        assert harness.slot._steer_retract is not None
        assert harness.slot._steer_retract.message == "still pending"


class TestDropped:
    """kiro-cli reported the drop: the steer is taken back and the rest resent."""

    @pytest.mark.asyncio
    async def test_unsend_marks_the_row_and_forgets_the_steer(self, harness):
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "use the blue fleet")
            mid = _mid_of(harness.slot, "use the blue fleet")
            await _retract(http, mid, "unsend")

        await steer_retract.on_dropped(harness.state, harness.slot)

        assert harness.slot._pending_steers == []
        (row,) = _steer_rows(harness.slot)
        assert row["meta"]["steerState"] == STEER_STATE_RETRACTED
        assert row["meta"][steer_retract.RETRACT_ACTION_META] == "unsend"
        assert row["meta"][STEER_RETRACTABLE_META] is False
        assert row["content"] == "", "the row keeps its place but not the text taken back"
        patch = _frames(harness.state, "chat_message_update")[-1]
        assert patch["mid"] == mid
        assert patch["meta"]["steerState"] == STEER_STATE_RETRACTED
        assert patch["content"] == ""
        assert harness.slot._queue == []
        assert harness.slot._steer_retract is None
        assert harness.slot._steer_clear is None

    @pytest.mark.asyncio
    async def test_the_other_steers_the_clear_dropped_are_sent_again_in_order(self, harness):
        client = harness.slot._acp_client
        async with TestClient(TestServer(harness.app)) as http:
            for text in ("first", "take me back", "last"):
                await _steer(http, text)
            await _retract(http, _mid_of(harness.slot, "take me back"), "unsend")
        client.steer.reset_mock()
        mids = {text: _mid_of(harness.slot, text) for text in ("first", "take me back", "last")}

        await steer_retract.on_dropped(harness.state, harness.slot)

        assert [c.args[0] for c in client.steer.await_args_list] == ["first", "last"]
        assert harness.slot._pending_steers == ["first", "last"]
        assert harness.slot._steer_rpc_in_flight == set()
        by_mid = {m["meta"]["mid"]: m["meta"]["steerState"] for m in _steer_rows(harness.slot)}
        states = {text: by_mid[mid] for text, mid in mids.items()}
        assert states == {
            "first": STEER_STATE_WRITTEN,
            "take me back": STEER_STATE_RETRACTED,
            "last": STEER_STATE_WRITTEN,
        }

    @pytest.mark.asyncio
    async def test_a_possibly_delivered_steer_is_not_taken_back_or_resent(self, harness):
        client = harness.slot._acp_client
        async with TestClient(TestServer(harness.app)) as http:
            for text in ("possibly read", "send again"):
                await _steer(http, text)
            await _retract(http, _mid_of(harness.slot, "possibly read"), "unsend")
        harness.slot._steer_possibly_delivered.add("possibly read")
        client.steer.reset_mock()

        await steer_retract.on_dropped(harness.state, harness.slot)

        assert [call.args[0] for call in client.steer.await_args_list] == ["send again"]
        assert harness.slot._pending_steers == ["possibly read", "send again"]
        assert harness.slot._steer_retract is None
        states = {row["content"]: row["meta"]["steerState"] for row in _steer_rows(harness.slot)}
        assert states["possibly read"] == STEER_STATE_WRITTEN
        assert states["send again"] == STEER_STATE_WRITTEN

    @pytest.mark.asyncio
    async def test_a_steer_sent_after_the_clear_is_not_resent(self, harness):
        client = harness.slot._acp_client
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "take me back")
            await _retract(http, _mid_of(harness.slot, "take me back"), "unsend")
            await _steer(http, "after the clear")
        client.steer.reset_mock()

        await steer_retract.on_dropped(harness.state, harness.slot)

        client.steer.assert_not_awaited()
        assert harness.slot._pending_steers == ["after the clear"]

    @pytest.mark.asyncio
    async def test_queue_instead_queues_the_text_with_its_attachments(self, harness):
        async with TestClient(TestServer(harness.app)) as http:
            resp = await http.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": "read [attached_file 1] /tmp/notes.md",
                    "steer": True,
                    "meta": {"files": ["/tmp/notes.md"]},
                },
            )
            assert resp.status == 200
            mid = _mid_of(harness.slot, "read [attached_file 1] /tmp/notes.md")
            await _retract(http, mid, "queue")

        await steer_retract.on_dropped(harness.state, harness.slot)

        (entry,) = harness.slot._queue
        assert entry["content"] == "read [attached_file 1] /tmp/notes.md"
        assert entry["meta"]["files"] == ["/tmp/notes.md"]
        (push,) = _frames(harness.state, "queue_push")
        assert push["queue_id"] == entry["id"]
        (row,) = _steer_rows(harness.slot)
        assert row["meta"]["steerState"] == STEER_STATE_RETRACTED
        assert row["meta"][steer_retract.RETRACT_ACTION_META] == "queue"

    @pytest.mark.asyncio
    async def test_queue_instead_keeps_the_request_time_admission(self, harness):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard import session_control as sc
        from kiro_crew.dashboard.chat_utils import slot_history_key

        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "run me next")
            await _retract(http, _mid_of(harness.slot, "run me next"), "queue")

        harness.state.sessions.set_mirror_link(
            slot_history_key(harness.slot), "C0NEWROOM", "1700000000.000200"
        )
        await steer_retract.on_dropped(harness.state, harness.slot)

        (entry,) = harness.slot._queue
        admission = entry["meta"][sc.QUEUED_CONTAINMENT_META_KEY]
        assert admission["mirrored"] is False
        chat_runner._drop_stale_admissions(harness.state, harness.slot)
        assert harness.slot._queue == []

    @pytest.mark.asyncio
    async def test_queue_instead_with_unchanged_containment_still_drains(self, harness):
        from kiro_crew.dashboard import chat_runner

        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "run me next")
            await _retract(http, _mid_of(harness.slot, "run me next"), "queue")

        await steer_retract.on_dropped(harness.state, harness.slot)

        chat_runner._drop_stale_admissions(harness.state, harness.slot)
        assert [entry["content"] for entry in harness.slot._queue] == ["run me next"]

    @pytest.mark.asyncio
    async def test_edit_leaves_the_text_to_the_composer(self, harness):
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "use the blue fleet")
            await _retract(http, _mid_of(harness.slot, "use the blue fleet"), "edit")

        await steer_retract.on_dropped(harness.state, harness.slot)

        assert harness.slot._queue == []
        (row,) = _steer_rows(harness.slot)
        assert row["meta"]["steerState"] == STEER_STATE_RETRACTED
        assert row["meta"][steer_retract.RETRACT_ACTION_META] == "edit"

    @pytest.mark.asyncio
    async def test_a_drop_no_clear_asked_for_settles_nothing(self, harness):
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "use the blue fleet")

        await steer_retract.on_dropped(harness.state, harness.slot)

        assert harness.slot._pending_steers == ["use the blue fleet"]
        (row,) = _steer_rows(harness.slot)
        assert row["meta"]["steerState"] == STEER_STATE_WRITTEN


class TestTheTurnReadItFirst:
    """The turn's consumption echo wins: nothing is taken back."""

    @pytest.mark.asyncio
    async def test_a_settle_ends_the_take_back(self, harness):
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "use the blue fleet")
            await _retract(http, _mid_of(harness.slot, "use the blue fleet"), "unsend")

        _settle_consumed_steers(
            harness.slot, "<user_message>\nuse the blue fleet\n</user_message>", harness.state
        )

        (row,) = _steer_rows(harness.slot)
        assert row["meta"]["steerState"] == STEER_STATE_CONSUMED
        assert harness.slot._steer_retract is None
        assert harness.slot._steer_clear is None, "nothing is left for the clear to drop"

    @pytest.mark.asyncio
    async def test_a_possibly_delivered_steer_ends_an_armed_take_back(self, harness):
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "possibly read")
            await _retract(http, _mid_of(harness.slot, "possibly read"), "unsend")
        harness.slot._steer_possibly_delivered.add("possibly read")

        steer_retract.on_settled(harness.slot)

        assert harness.slot._steer_retract is None
        assert harness.slot._pending_steers == ["possibly read"]

    @pytest.mark.asyncio
    async def test_a_drop_after_the_read_resends_only_what_is_still_pending(self, harness):
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        client = harness.slot._acp_client
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "take me back")
            await _steer(http, "still waiting")
            await _retract(http, _mid_of(harness.slot, "take me back"), "unsend")
        _settle_consumed_steers(
            harness.slot, "<user_message>\ntake me back\n</user_message>", harness.state
        )
        assert harness.slot._steer_clear is not None, "the clear can still drop the other steer"
        client.steer.reset_mock()

        await steer_retract.on_dropped(harness.state, harness.slot)

        assert [c.args[0] for c in client.steer.await_args_list] == ["still waiting"]
        states = {m["content"]: m["meta"]["steerState"] for m in _steer_rows(harness.slot)}
        assert states["take me back"] == STEER_STATE_CONSUMED

    @pytest.mark.asyncio
    async def test_a_consumed_steer_with_a_closing_tag_cannot_be_taken_back_or_resent(
        self, harness
    ):
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        consumed = "steer A\n</user_message>\nstill part of steer A"
        waiting = "steer B"
        client = harness.slot._acp_client
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, consumed)
            consumed_mid = _mid_of(harness.slot, consumed)
            _settle_consumed_steers(harness.slot, wrap_user_message(consumed), harness.state)
            await _steer(http, waiting)
            waiting_mid = _mid_of(harness.slot, waiting)

            consumed_response = await _retract(http, consumed_mid, "unsend")
            waiting_response = await _retract(http, waiting_mid, "unsend")

            assert consumed_response.status == 409
            assert (await consumed_response.json())["code"] == "steer_not_pending"
            assert waiting_response.status == 202

        client.steer.reset_mock()
        await steer_retract.on_dropped(harness.state, harness.slot)

        client.steer.assert_not_awaited()
        assert harness.slot._pending_steers == []
        states = {m["meta"]["mid"]: m["meta"]["steerState"] for m in _steer_rows(harness.slot)}
        assert states[consumed_mid] == STEER_STATE_CONSUMED
        assert states[waiting_mid] == STEER_STATE_RETRACTED


class TestAPartlyEchoedCollision:
    """An ambiguous echo makes every colliding steer unsafe to take back."""

    first = "append <user_message> to notes.txt"
    second = "append &lt;user_message&gt; to notes.txt"

    @pytest.mark.asyncio
    async def test_both_steers_are_possibly_delivered_and_refuse_take_back(self, harness):
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, self.first)
            await _steer(http, self.second)
            _settle_consumed_steers(harness.slot, wrap_user_message(self.first), harness.state)

            assert harness.slot._steer_possibly_delivered == {self.first, self.second}
            for text in (self.first, self.second):
                response = await _retract(http, _mid_of(harness.slot, text), "unsend")
                assert response.status == 409
                assert (await response.json())["code"] == "steer_not_pending"

    @pytest.mark.asyncio
    async def test_the_echo_ends_an_armed_take_back_and_a_clear_resends_neither(self, harness):
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        client = harness.slot._acp_client
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, self.first)
            await _steer(http, self.second)
            response = await _retract(http, _mid_of(harness.slot, self.first), "unsend")
            assert response.status == 202

        _settle_consumed_steers(harness.slot, wrap_user_message(self.first), harness.state)

        assert harness.slot._steer_retract is None
        assert harness.slot._pending_steers == [self.first, self.second]
        client.steer.reset_mock()
        await steer_retract.on_dropped(harness.state, harness.slot)

        client.steer.assert_not_awaited()
        assert harness.slot._pending_steers == [self.first, self.second]
        assert all(
            row["meta"]["steerState"] == STEER_STATE_WRITTEN for row in _steer_rows(harness.slot)
        )

    @pytest.mark.asyncio
    async def test_turn_end_requeues_both_with_the_possibly_delivered_note(self, harness):
        from kiro_crew.dashboard.chat_runner import (
            _requeue_unconsumed_steers,
            _settle_consumed_steers,
        )
        from kiro_crew.dashboard.chat_utils import STEER_POSSIBLY_DELIVERED_META

        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, self.first)
            await _steer(http, self.second)

        _settle_consumed_steers(harness.slot, wrap_user_message(self.first), harness.state)
        _requeue_unconsumed_steers(harness.state, harness.slot)

        assert [entry["content"] for entry in harness.slot._queue] == [self.first, self.second]
        assert all(
            entry["meta"][STEER_POSSIBLY_DELIVERED_META] is True for entry in harness.slot._queue
        )


class TestAHardStop:
    """A hard Stop releases a take-back without claiming the steer was dropped."""

    async def _stop(
        self,
        harness,
        *,
        settlement_started: asyncio.Event | None = None,
        settlement_release: asyncio.Event | None = None,
    ) -> None:
        from kiro_crew.dashboard.chat_handlers import stop_slot_turn

        if settlement_started is not None:
            assert settlement_release is not None

            async def settle_queued_delivery(_deliveries) -> None:
                settlement_started.set()
                await settlement_release.wait()

            harness.slot._queue.append({"content": "queued completion", "meta": {}})
            harness.slot.note_pending_subagent_delivery(
                "queued completion", [SimpleNamespace(agent_id="owed")]
            )
            # The hard Stop's slots push serializes every slot whenever its coalescing
            # window is open, which reads the manager's running and queued sub-agents.
            harness.state.subagents = SimpleNamespace(
                settle_queued_delivery=MagicMock(side_effect=settle_queued_delivery),
                running_agents_for=MagicMock(return_value=[]),
                published_queued_depths=MagicMock(return_value={}),
            )
        harness.state.sessions.stop_turn = AsyncMock(return_value="hard")
        harness.slot._stop_state = "soft_pending"

        result = await stop_slot_turn(harness.state, harness.slot, force=True)

        assert result == {"ok": True}
        assert harness.slot._stop_state == "killing"

    @pytest.mark.asyncio
    async def test_unsend_is_released_without_settling_its_row(self, harness):
        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "take me back")
            await _retract(http, _mid_of(harness.slot, "take me back"), "unsend")

        await self._stop(harness)

        (row,) = _steer_rows(harness.slot)
        assert row["meta"]["steerState"] == STEER_STATE_WRITTEN
        assert row["content"] == "take me back"
        assert _frames(harness.state, "chat_message_update") == []
        assert harness.slot._pending_steers == []
        assert harness.slot._queue == []
        assert harness.slot._steer_retract is None
        assert harness.slot._steer_clear is None

        _requeue_unconsumed_steers(harness.state, harness.slot)

        assert harness.slot._queue == []
        assert harness.slot._steer_retract is None
        assert harness.slot._steer_clear is None

    @pytest.mark.asyncio
    async def test_edit_is_released_without_settling_its_row(self, harness):
        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "change this")
            await _retract(http, _mid_of(harness.slot, "change this"), "edit")

        await self._stop(harness)

        (row,) = _steer_rows(harness.slot)
        assert row["meta"]["steerState"] == STEER_STATE_WRITTEN
        assert row["content"] == "change this"
        assert _frames(harness.state, "chat_message_update") == []
        assert harness.slot._pending_steers == []
        assert harness.slot._queue == []
        assert harness.slot._steer_retract is None
        assert harness.slot._steer_clear is None

        _requeue_unconsumed_steers(harness.state, harness.slot)

        assert harness.slot._queue == []
        assert harness.slot._steer_retract is None
        assert harness.slot._steer_clear is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["unsend", "edit"])
    async def test_a_late_consumed_report_never_retracts_the_row(self, harness, action):
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        text = f"{action} after the turn read this"
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, text)
            await _retract(http, _mid_of(harness.slot, text), action)

        await self._stop(harness)
        _settle_consumed_steers(harness.slot, wrap_user_message(text), harness.state)

        (row,) = _steer_rows(harness.slot)
        assert row["meta"]["steerState"] == STEER_STATE_WRITTEN
        assert row["content"] == text
        assert all(
            frame.get("content") != "" for frame in _frames(harness.state, "chat_message_update")
        )

    @pytest.mark.asyncio
    async def test_queue_instead_is_cleared_and_discarded_by_a_hard_stop(self, harness):
        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "run me next")
            await _retract(http, _mid_of(harness.slot, "run me next"), "queue")

        await self._stop(harness)

        (row,) = _steer_rows(harness.slot)
        assert row["meta"]["steerState"] == STEER_STATE_WRITTEN
        assert row["content"] == "run me next"
        assert harness.slot._pending_steers == []
        assert harness.slot._queue == []
        assert harness.slot._steer_retract is None
        assert harness.slot._steer_clear is None

        _requeue_unconsumed_steers(harness.state, harness.slot)

        assert harness.slot._queue == []
        assert row["meta"]["steerState"] == STEER_STATE_WRITTEN

    @pytest.mark.asyncio
    async def test_a_drop_during_discarded_delivery_settle_cannot_requeue(self, harness):
        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "run me next")
            await _retract(http, _mid_of(harness.slot, "run me next"), "queue")

        settlement_started = asyncio.Event()
        settlement_release = asyncio.Event()
        stop = asyncio.create_task(
            self._stop(
                harness,
                settlement_started=settlement_started,
                settlement_release=settlement_release,
            )
        )
        try:
            await async_wait_until(settlement_started.is_set)
            await steer_retract.on_dropped(harness.state, harness.slot)
        finally:
            settlement_release.set()
            await await_until(stop, what="the hard Stop settlement finishing")

        (row,) = _steer_rows(harness.slot)
        assert row["meta"]["steerState"] == STEER_STATE_WRITTEN
        assert row["content"] == "run me next"
        assert _frames(harness.state, "chat_message_update") == []
        assert harness.slot._pending_steers == []
        assert harness.slot._queue == []
        assert harness.slot._steer_retract is None
        assert harness.slot._steer_clear is None

        _requeue_unconsumed_steers(harness.state, harness.slot)

        assert harness.slot._queue == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["queue", "edit", "unsend"])
    async def test_a_take_back_during_discarded_delivery_settle_is_refused(self, harness, action):
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "hard Stop is discarding this")
            mid = _mid_of(harness.slot, "hard Stop is discarding this")
            settlement_started = asyncio.Event()
            settlement_release = asyncio.Event()
            stop = asyncio.create_task(
                self._stop(
                    harness,
                    settlement_started=settlement_started,
                    settlement_release=settlement_release,
                )
            )
            try:
                await async_wait_until(settlement_started.is_set)
                response = await _retract(http, mid, action)
                assert response.status == 409
                assert (await response.json())["code"] == "steer_not_pending"
                harness.slot._acp_client.clear_pending_steers.assert_not_awaited()
                assert harness.slot._steer_retract is None
                assert harness.slot._steer_clear is None
            finally:
                settlement_release.set()
                await await_until(stop, what="the hard Stop settlement finishing")

        assert harness.slot._pending_steers == []
        assert harness.slot._queue == []
        assert harness.slot._steer_retract is None
        assert harness.slot._steer_clear is None

    @pytest.mark.asyncio
    async def test_a_hard_stop_releases_a_steer_waiting_at_the_clear_gate(self, harness):
        clear_release = asyncio.Event()

        async def slow_clear() -> bool:
            await clear_release.wait()
            return True

        harness.slot._acp_client.clear_pending_steers = AsyncMock(side_effect=slow_clear)
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "take me back")
            retract = asyncio.create_task(
                _retract(http, _mid_of(harness.slot, "take me back"), "unsend")
            )
            await async_wait_until(lambda: harness.slot._steer_clear is not None)
            clear = harness.slot._steer_clear
            waiter = asyncio.create_task(steer_retract.wait_for_clear(harness.slot))
            assert not clear.answered.is_set()

            try:
                await self._stop(harness)
                assert clear.answered.is_set()
                await await_until(waiter, what="the steer held at the clear's gate being released")
            finally:
                clear_release.set()
                response = await await_until(retract, what="the take-back's request finishing")
                assert response.status == 202

        assert clear.answered.is_set()
        assert harness.slot._steer_retract is None
        assert harness.slot._steer_clear is None


class TestTheTurnEnded:
    """A steer still being taken back when the turn ends never reached it."""

    @pytest.mark.asyncio
    async def test_unsend_is_not_requeued(self, harness):
        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "take me back")
            await _steer(http, "keep me")
            await _retract(http, _mid_of(harness.slot, "take me back"), "unsend")
        mids = {text: _mid_of(harness.slot, text) for text in ("take me back", "keep me")}

        _requeue_unconsumed_steers(harness.state, harness.slot)

        assert [e["content"] for e in harness.slot._queue] == ["keep me"]
        by_mid = {m["meta"]["mid"]: m["meta"]["steerState"] for m in _steer_rows(harness.slot)}
        states = {text: by_mid[mid] for text, mid in mids.items()}
        assert states == {"take me back": STEER_STATE_RETRACTED, "keep me": STEER_STATE_REQUEUED}
        assert harness.slot._steer_retract is None
        assert harness.slot._steer_clear is None

    @pytest.mark.asyncio
    async def test_a_possibly_delivered_record_is_requeued_instead_of_taken_back(self, harness):
        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers
        from kiro_crew.dashboard.chat_utils import STEER_POSSIBLY_DELIVERED_META

        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "possibly read")
            await _retract(http, _mid_of(harness.slot, "possibly read"), "unsend")
        harness.slot._steer_possibly_delivered.add("possibly read")

        _requeue_unconsumed_steers(harness.state, harness.slot)

        assert [entry["content"] for entry in harness.slot._queue] == ["possibly read"]
        assert harness.slot._queue[0]["meta"][STEER_POSSIBLY_DELIVERED_META] is True
        (row,) = _steer_rows(harness.slot)
        assert row["meta"]["steerState"] == STEER_STATE_REQUEUED
        assert steer_retract.RETRACT_ACTION_META not in row["meta"]

    @pytest.mark.asyncio
    async def test_queue_instead_is_left_to_the_requeue(self, harness):
        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "run me next")
            await _retract(http, _mid_of(harness.slot, "run me next"), "queue")

        _requeue_unconsumed_steers(harness.state, harness.slot)

        assert [e["content"] for e in harness.slot._queue] == ["run me next"]
        (row,) = _steer_rows(harness.slot)
        assert row["meta"]["steerState"] == STEER_STATE_REQUEUED

    @pytest.mark.asyncio
    async def test_queue_instead_keeps_unchanged_mirrored_admission_at_turn_end(self, harness):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard import session_control as sc
        from kiro_crew.dashboard.chat_utils import slot_history_key

        harness.state.sessions.set_mirror_link(
            slot_history_key(harness.slot), "C0EXISTING", "1700000000.000100"
        )
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "run me next")
            await _retract(http, _mid_of(harness.slot, "run me next"), "queue")

        chat_runner._requeue_unconsumed_steers(harness.state, harness.slot)

        (entry,) = harness.slot._queue
        assert entry["_directive_user_origin"] is True
        assert entry["meta"][sc.QUEUED_CONTAINMENT_META_KEY]["mirrored"] is True
        chat_runner._drop_stale_admissions(harness.state, harness.slot)
        assert [item["content"] for item in harness.slot._queue] == ["run me next"]

    @pytest.mark.asyncio
    async def test_queue_instead_drops_a_mirror_added_after_request_at_turn_end(self, harness):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard import session_control as sc
        from kiro_crew.dashboard.chat_utils import slot_history_key

        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "run me next")
            await _retract(http, _mid_of(harness.slot, "run me next"), "queue")

        harness.state.sessions.set_mirror_link(
            slot_history_key(harness.slot), "C0NEWROOM", "1700000000.000200"
        )
        chat_runner._requeue_unconsumed_steers(harness.state, harness.slot)

        (entry,) = harness.slot._queue
        assert entry["_directive_user_origin"] is True
        assert entry["meta"][sc.QUEUED_CONTAINMENT_META_KEY]["mirrored"] is False
        chat_runner._drop_stale_admissions(harness.state, harness.slot)
        assert harness.slot._queue == []


class TestATakeBackLivesForOneTurn:
    """A take-back left behind by a turn that ended some other way blocks nothing."""

    @pytest.mark.asyncio
    async def test_a_record_from_an_earlier_turn_is_dropped(self, harness):
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "first turn's steer")
            await _retract(http, _mid_of(harness.slot, "first turn's steer"), "unsend")
            stale_clear = harness.slot._steer_clear
            # A new turn owns the slot and its exit never ran the teardown hook.
            harness.slot.task = MagicMock(done=MagicMock(return_value=False))
            await _steer(http, "second turn's steer")
            resp = await _retract(http, _mid_of(harness.slot, "second turn's steer"), "unsend")

            assert resp.status == 202
        assert stale_clear.answered.is_set(), "a steer waiting on the old clear is released"
        assert harness.slot._steer_retract.message == "second turn's steer"


class TestTheClearGate:
    """A steer sent while a clear is unanswered is written after the answer."""

    @pytest.mark.asyncio
    async def test_a_new_steer_waits_for_the_clear(self, harness, monkeypatch):
        client = harness.slot._acp_client
        release = asyncio.Event()
        at_gate = asyncio.Event()
        order: list[str] = []
        real_gate = steer_retract.wait_for_clear

        async def slow_clear() -> bool:
            order.append("clear sent")
            await release.wait()
            order.append("clear answered")
            return True

        async def record_steer(text: str) -> bool:
            order.append(f"steer {text}")
            return True

        async def gate(slot):
            # The positive signal the absence check below needs: the steer has
            # reached the gate, so it would have been written by now without it.
            if slot._steer_clear is not None:
                at_gate.set()
            await real_gate(slot)

        client.clear_pending_steers = AsyncMock(side_effect=slow_clear)
        monkeypatch.setattr(steer_retract, "wait_for_clear", gate)
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "take me back")
            client.steer = AsyncMock(side_effect=record_steer)
            retract = asyncio.ensure_future(
                _retract(http, _mid_of(harness.slot, "take me back"), "unsend")
            )
            steer = None
            try:
                await async_wait_until(lambda: "clear sent" in order)
                steer = asyncio.ensure_future(_steer(http, "after the clear"))
                await await_until(at_gate, what="the steer reaching the clear's gate")

                assert "steer after the clear" not in order
            finally:
                release.set()
                await await_until(
                    asyncio.gather(retract, *([steer] if steer is not None else [])),
                    what="the take-back and the held steer finishing",
                )

        assert order == ["clear sent", "clear answered", "steer after the clear"]

    async def _held_steer(self, harness, monkeypatch, text: str) -> asyncio.Future:
        """A steer sent while a clear is unanswered, suspended at the clear's gate."""
        from kiro_crew.dashboard.chat_delivery import steer_into_running_turn

        at_gate = asyncio.Event()
        real_gate = steer_retract.wait_for_clear

        async def gate(slot):
            at_gate.set()
            await real_gate(slot)

        monkeypatch.setattr(steer_retract, "wait_for_clear", gate)
        harness.slot._steer_clear = steer_retract.SteerClear(batch=(), turn=harness.slot.task)
        steer = asyncio.ensure_future(steer_into_running_turn(harness.state, harness.slot, text))
        await await_until(at_gate, what="the steer reaching the clear's gate")
        assert not harness.slot._steer_clear.answered.is_set()
        return steer

    def _assert_handed_back(self, harness, outcome: str) -> None:
        from kiro_crew.dashboard.chat_delivery import STEER_UNAVAILABLE

        assert outcome == STEER_UNAVAILABLE
        assert _steer_rows(harness.slot) == []
        assert harness.slot._pending_steers == []
        assert harness.slot._steer_delivery_ids == {}
        harness.slot._acp_client.steer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_steer_held_while_the_turn_ends_is_handed_back(self, harness, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        steer = await self._held_steer(harness, monkeypatch, "too late for this turn")

        harness.slot.task.done.return_value = True
        _requeue_unconsumed_steers(harness.state, harness.slot)

        outcome = await await_until(steer, what="the held steer resuming")
        self._assert_handed_back(harness, outcome)

    @pytest.mark.asyncio
    async def test_a_steer_held_through_a_hard_stop_is_handed_back(self, harness, monkeypatch):
        from kiro_crew.dashboard.chat_handlers import stop_slot_turn

        steer = await self._held_steer(harness, monkeypatch, "sent as the stop landed")

        harness.state.sessions.stop_turn = AsyncMock(return_value="hard")
        harness.slot._stop_state = "soft_pending"
        assert await stop_slot_turn(harness.state, harness.slot, force=True) == {"ok": True}

        outcome = await await_until(steer, what="the held steer resuming")
        self._assert_handed_back(harness, outcome)


@pytest.mark.asyncio
async def test_the_turn_hands_a_dropped_report_to_the_take_back(harness, monkeypatch):
    from kiro_crew.acp.types import EVENT_STEER_DROPPED, AcpEvent
    from kiro_crew.dashboard.chat_turn.steer_queue import _on_steer_dropped

    seen: list[tuple] = []

    async def record(state, slot):
        seen.append((state, slot))

    monkeypatch.setattr(steer_retract, "on_dropped", record)
    turn = SimpleNamespace(state=harness.state, slot=harness.slot)

    await _on_steer_dropped(turn, AcpEvent(kind=EVENT_STEER_DROPPED))
    await _on_steer_dropped(turn, AcpEvent(kind=EVENT_STEER_DROPPED, runtime_global=True))

    assert seen == [(harness.state, harness.slot)], "an ownerless copy settles nothing"


@pytest.mark.asyncio
async def test_the_event_loop_hands_a_drop_to_its_phase(monkeypatch):
    from turn_harness import TurnScript, run_turn

    from kiro_crew.acp.types import EVENT_STEER_DROPPED, EVENT_TEXT_CHUNK, AcpEvent

    handed: list[str] = []

    async def record(_state, slot):
        handed.append(slot.key)

    monkeypatch.setattr(steer_retract, "on_dropped", record)

    await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_STEER_DROPPED),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply"),
            ]
        )
    )

    assert len(handed) == 1, "the turn hands kiro-cli's drop report to the take-back once"


class TestTheSessionProvider:
    """The dashboard reaches kiro-cli's clear through ``AcpSessionProvider``."""

    @staticmethod
    def _provider(handle: MagicMock):
        from kiro_crew.acp.session_provider import AcpSessionProvider

        return AcpSessionProvider(handle, runtime=MagicMock())

    def test_it_offers_a_take_back_only_when_its_handle_does(self):
        handle = MagicMock()
        handle.supports_steer_retract = True
        assert self._provider(handle).supports_steer_retract is True

        handle.supports_steer_retract = False
        assert self._provider(handle).supports_steer_retract is False

    @pytest.mark.asyncio
    async def test_the_clear_goes_to_its_handle_and_the_answer_comes_back(self):
        handle = MagicMock()
        handle.clear_pending_steers = AsyncMock(return_value=True)

        assert await self._provider(handle).clear_pending_steers() is True
        handle.clear_pending_steers.assert_awaited_once_with()


def test_a_slot_starts_with_nothing_being_taken_back(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    slot = _make_state(tmp_path).get_or_create_slot("fresh")

    assert slot._steer_retract is None
    assert slot._steer_clear is None


@pytest.mark.asyncio
async def test_an_old_identical_written_row_cannot_retract_the_current_steer(harness):
    async with TestClient(TestServer(harness.app)) as http:
        await _steer(http, "use the blue fleet")
        old_mid = _mid_of(harness.slot, "use the blue fleet")
        # A hard-killed turn can leave its row written after pending delivery state is gone.
        harness.slot._pending_steers.clear()
        await _steer(http, "use the blue fleet")

        resp = await _retract(http, old_mid, "unsend")

        assert resp.status == 409
        assert (await resp.json())["code"] == "steer_not_pending"
    harness.slot._acp_client.clear_pending_steers.assert_not_awaited()
    assert harness.slot._pending_steers == ["use the blue fleet"]
    rows = _steer_rows(harness.slot)
    assert len(rows) == 2
    assert all(row["meta"]["steerState"] == STEER_STATE_WRITTEN for row in rows)


def _offer_of(slot, mid: str) -> object:
    row = next(m for m in slot.messages if (m.get("meta") or {}).get("mid") == mid)
    return row["meta"].get(STEER_RETRACTABLE_META)


def _scripted_turn_state(tmp_path, monkeypatch):
    """A state whose backend streams one finished reply, for driving ``_run_chat``."""
    from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    client = MagicMock()
    client.shutdown = AsyncMock()

    async def _stream(*_a, **_kw):
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="done")
        yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.record_failure = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    return state


class TestAnEndedTurnsOffer:
    """A steer its turn ended without settling stops offering a take-back at the next turn."""

    @pytest.mark.asyncio
    async def test_a_hard_killed_steer_loses_the_offer_when_the_next_turn_starts(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat_runner import _run_chat

        state = _scripted_turn_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("ended-offer")
        # What a hard Stop (or a restart mid-turn) leaves behind: the row of a
        # steer the turn never read, still written and offered, nothing pending.
        row = slot.append(
            "user",
            "use the blue fleet",
            "msg msg-u",
            meta={"steer": True, "steerState": STEER_STATE_WRITTEN, STEER_RETRACTABLE_META: True},
        )
        mid = row["meta"]["mid"]
        state.broadcast_ws.reset_mock()

        await _run_chat(state, slot, "the next turn")

        assert _offer_of(slot, mid) is False
        assert row["meta"].get("steerRetractExpired") is True
        assert row["meta"]["steerState"] == STEER_STATE_WRITTEN
        assert row["content"] == "use the blue fleet"
        patch = next(f for f in _frames(state, "chat_message_update") if f.get("mid") == mid)
        assert patch["meta"][STEER_RETRACTABLE_META] is False
        assert patch["meta"].get("steerRetractExpired") is True
        assert patch["meta"]["steerState"] == STEER_STATE_WRITTEN
        assert "content" not in patch

    @pytest.mark.asyncio
    async def test_a_steer_discarded_by_a_hard_stop_is_withdrawn_at_the_next_start(self, harness):
        from kiro_crew.dashboard.chat_handlers import stop_slot_turn

        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "use the blue fleet")
        mid = _mid_of(harness.slot, "use the blue fleet")
        harness.state.sessions.stop_turn = AsyncMock(return_value="hard")
        harness.slot._stop_state = "soft_pending"
        await stop_slot_turn(harness.state, harness.slot, force=True)
        assert _offer_of(harness.slot, mid) is True, "the hard Stop itself leaves the offer"

        steer_retract.withdraw_stale_offers(harness.state, harness.slot)

        (row,) = _steer_rows(harness.slot)
        assert row["meta"][STEER_RETRACTABLE_META] is False
        assert row["meta"].get("steerRetractExpired") is True
        assert row["meta"]["steerState"] == STEER_STATE_WRITTEN
        assert row["content"] == "use the blue fleet"
        patch = _frames(harness.state, "chat_message_update")[-1]
        assert patch["mid"] == mid
        assert patch["meta"][STEER_RETRACTABLE_META] is False
        assert patch["meta"].get("steerRetractExpired") is True

    @pytest.mark.asyncio
    async def test_a_steer_of_the_new_turn_keeps_its_offer(self, harness):
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "from the turn that was killed")
            harness.slot._pending_steers.clear()  # the hard Stop's discard
            steer_retract.withdraw_stale_offers(harness.state, harness.slot)
            await _steer(http, "from the new turn")
        new_mid = _mid_of(harness.slot, "from the new turn")

        assert (
            _offer_of(harness.slot, _mid_of(harness.slot, "from the turn that was killed")) is False
        )
        assert _offer_of(harness.slot, new_mid) is True
        async with TestClient(TestServer(harness.app)) as http:
            resp = await _retract(http, new_mid, "unsend")
        assert resp.status == 202

    @pytest.mark.asyncio
    async def test_nothing_is_withdrawn_while_a_steer_is_pending(self, harness):
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "still waiting")
        mid = _mid_of(harness.slot, "still waiting")
        harness.state.broadcast_ws.reset_mock()

        steer_retract.withdraw_stale_offers(harness.state, harness.slot)

        assert _offer_of(harness.slot, mid) is True
        assert _frames(harness.state, "chat_message_update") == []

    @pytest.mark.asyncio
    async def test_nothing_is_withdrawn_while_a_take_back_is_armed(self, harness):
        async with TestClient(TestServer(harness.app)) as http:
            await _steer(http, "take me back")
            mid = _mid_of(harness.slot, "take me back")
            await _retract(http, mid, "queue")
        harness.slot._pending_steers.clear()
        record, clear = harness.slot._steer_retract, harness.slot._steer_clear
        assert record is not None and clear is not None
        harness.state.broadcast_ws.reset_mock()

        # Either half of an armed take-back holds the sweep off on its own.
        for armed in ((record, None), (None, clear)):
            harness.slot._steer_retract, harness.slot._steer_clear = armed
            steer_retract.withdraw_stale_offers(harness.state, harness.slot)

        assert _offer_of(harness.slot, mid) is True
        assert _frames(harness.state, "chat_message_update") == []

    def test_other_rows_and_settled_steers_are_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("settled")
        offered = {STEER_RETRACTABLE_META: True}
        rows = [
            slot.append("user", "a plain message", meta=dict(offered)),
            slot.append(
                "assistant",
                "a reply",
                meta={"steer": True, "steerState": STEER_STATE_WRITTEN, **offered},
            ),
            slot.append(
                "user",
                "consumed",
                meta={"steer": True, "steerState": STEER_STATE_CONSUMED, **offered},
            ),
            slot.append(
                "user",
                "requeued",
                meta={"steer": True, "steerState": STEER_STATE_REQUEUED, **offered},
            ),
            slot.append(
                "user", "", meta={"steer": True, "steerState": STEER_STATE_RETRACTED, **offered}
            ),
            slot.append(
                "user", "never offered", meta={"steer": True, "steerState": STEER_STATE_WRITTEN}
            ),
        ]
        before = [(r["content"], dict(r["meta"])) for r in rows]
        state.broadcast_ws.reset_mock()

        steer_retract.withdraw_stale_offers(state, slot)

        assert [(r["content"], dict(r["meta"])) for r in rows] == before
        assert _frames(state, "chat_message_update") == []
