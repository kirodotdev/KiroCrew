"""Coverage for the guard and failure paths of
:mod:`kiro_crew.dashboard.chat_regenerate`.

``test_dashboard_chat.py::TestRegenerateAndVariants`` covers the happy paths of
regenerate and variant switching. Untested there: ``edit-resend`` in its
entirety (it is not even wired into the shared test app), every 400/404/409
guard on all three endpoints, the readiness latch that must fire BEFORE the
destructive truncation, the persist-failure paths, and the two done-callbacks.

The app here registers the three handlers directly so ``edit-resend`` is
reachable; ``_run_chat`` is always patched, so no backend session is started.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_regenerate
from kiro_crew.dashboard.chat_regenerate import (
    api_chat_slot_edit_resend,
    api_chat_slot_regenerate,
    api_chat_slot_switch_variant,
)

# Ceiling for the cross-thread gates below. Generous rather than tight: it is a
# deadlock backstop, never a synchronisation point, so a slow shared runner must
# not trip it -- every test that uses it also releases its gate in a ``finally``.
_GATE_TIMEOUT_SECS = 30


def _make_regen_app(state) -> web.Application:
    """App exposing all three chat_regenerate routes, including edit-resend."""
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/regenerate", api_chat_slot_regenerate)
    app.router.add_post("/api/chat/slots/{slot}/switch-variant", api_chat_slot_switch_variant)
    app.router.add_post("/api/chat/slots/{slot}/edit-resend", api_chat_slot_edit_resend)
    return app


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    # edit-resend is now a real conversation boundary: it discards the native
    # ACP conversation and flushes the cleared resume sid BEFORE persisting the
    # truncated history. Configure the sessions double so the happy paths reach
    # commit -- discard succeeds (returns True), the flush is a no-op, and the
    # orphan-session lookup returns "" so no cleanup is attempted.
    st.sessions.discard_conversation = AsyncMock(return_value=True)
    st.sessions.aflush = AsyncMock()
    st.sessions._session_map.get = MagicMock(return_value="")
    return st


def _client(state):
    return TestClient(TestServer(_make_regen_app(state)))


async def _busy(slot) -> None:
    """Pin the slot as running with a task that outlives the request."""

    async def _sleep() -> None:
        await asyncio.sleep(10)

    slot.task = asyncio.create_task(_sleep())


# ── regenerate ──


@pytest.mark.asyncio
async def test_regenerate_unknown_slot_is_404(state) -> None:
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/nope/regenerate")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_regenerate_requires_a_preceding_user_message(state) -> None:
    """An assistant-first transcript has nothing to re-send."""
    slot = state.get_or_create_slot("s1")
    slot.append("assistant", "unprompted greeting")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/regenerate")
        assert resp.status == 400
        assert (await resp.json())["error"] == "no preceding user message"
    assert [m["role"] for m in slot.messages] == ["assistant"]  # untouched


@pytest.mark.asyncio
async def test_regenerate_rejects_an_empty_user_message(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "")
    slot.append("assistant", "reply to nothing")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/regenerate")
        assert resp.status == 400
        assert (await resp.json())["error"] == "empty user message"


@pytest.mark.asyncio
async def test_readiness_latch_blocks_before_the_truncation(state) -> None:
    """Regenerate persists the truncation, so an unverified backend must be
    rejected BEFORE history is mutated -- a failed turn cannot undo it."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "hello v1")
    blocked = web.json_response({"error": "kiro not verified"}, status=503)

    with patch(
        "kiro_crew.dashboard.chat_regenerate.reject_if_kiro_unverified",
        new=AsyncMock(return_value=blocked),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")

    assert resp.status == 503
    assert [m["role"] for m in slot.messages] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_regenerate_survives_a_history_write_failure(state, caplog) -> None:
    """A failed rewrite must not fail the request, and must leave the
    rewrite flag set so the flush loop still archives the dropped tail."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "hello v1")
    slot.drain()

    with (
        patch(
            "kiro_crew.dashboard.chat_regenerate._save_slot_to_history",
            side_effect=OSError("disk full"),
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
    ):
        with caplog.at_level("WARNING"):
            async with _client(state) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.sleep(0)

    assert "failed to rewrite session history" in caplog.text
    assert slot._pending_rewrite is True


@pytest.mark.asyncio
async def test_unconsumed_variants_are_discarded_with_a_warning(state, caplog) -> None:
    """If the flush never picks the stash up, the done-callback clears it rather
    than leaking it into the next turn."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "hello v1")
    slot.drain()

    with patch(
        "kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()
    ):  # returns without consuming _pending_variants
        with caplog.at_level("WARNING"):
            async with _client(state) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.sleep(0)
                await asyncio.sleep(0)

    assert slot._pending_variants == []
    assert "pending variants not consumed by flush" in caplog.text


@pytest.mark.asyncio
async def test_regenerate_rejected_while_a_turn_is_in_flight(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "hello")
    await _busy(slot)
    try:
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
        assert resp.status == 409
    finally:
        slot.task.cancel()


# ── switch-variant ──


@pytest.mark.asyncio
async def test_switch_variant_unknown_slot_is_404(state) -> None:
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/nope/switch-variant", json={"index": 0})
    assert resp.status == 404


@pytest.mark.asyncio
async def test_switch_variant_rejects_a_non_json_body(state) -> None:
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/switch-variant",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "invalid JSON"


@pytest.mark.asyncio
async def test_switch_variant_rejects_a_non_object_body(state) -> None:
    """A JSON array has no .get(), so an unguarded handler would 500."""
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/switch-variant", json=[0])
    assert resp.status == 400


@pytest.mark.asyncio
async def test_switch_variant_rejects_a_non_integer_index(state) -> None:
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        for body in ({"index": "second"}, {}):
            resp = await client.post("/api/chat/slots/s1/switch-variant", json=body)
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid index"


@pytest.mark.asyncio
async def test_switch_variant_needs_an_assistant_row_with_variants(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "only one answer")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
        assert resp.status == 400
        assert (await resp.json())["error"] == "no variants"


@pytest.mark.asyncio
async def test_switch_variant_rejects_a_corrupt_variant_entry(state) -> None:
    """A restored transcript can hold a non-dict entry; picking it would 500."""
    slot = state.get_or_create_slot("s1")
    slot.append("assistant", "v1")
    slot.messages[-1]["variants"] = ["a bare string, not an entry"]
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
        assert resp.status == 400
        assert (await resp.json())["error"] == "corrupt variant entry"


@pytest.mark.asyncio
async def test_switch_variant_rejected_while_a_turn_is_in_flight(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("assistant", "v1")
    slot.messages[-1]["variants"] = [{"content": "v1"}]
    await _busy(slot)
    try:
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
        assert resp.status == 409
    finally:
        slot.task.cancel()


@pytest.mark.asyncio
async def test_switch_variant_broadcasts_redacted_content(state) -> None:
    """The broadcast leaves the process, so the chosen variant is redacted."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "what is the key?")
    slot.append("assistant", "v2")
    slot.messages[-1]["variants"] = [
        {"content": "the key is AKIAIOSFODNN7EXAMPLE", "ts": "t1"},
        {"content": "v2", "ts": "t2"},
    ]
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
        assert resp.status == 200
        assert (await resp.json())["index"] == 0

    msg_type, payload = state.broadcast_ws.call_args.args
    assert msg_type == "chat_variant_switch"
    assert payload["index"] == 0
    assert "AKIAIOSFODNN7EXAMPLE" not in payload["content"]
    # The stored row keeps the real content; only the wire copy is redacted.
    assert slot.messages[-1]["content"] == "the key is AKIAIOSFODNN7EXAMPLE"
    assert slot.messages[-1]["ts"] == "t1"


@pytest.mark.asyncio
async def test_switch_variant_survives_a_persist_failure(state, caplog) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("assistant", "v2")
    slot.messages[-1]["variants"] = [{"content": "v1", "ts": "t1"}, {"content": "v2"}]

    with patch(
        "kiro_crew.dashboard.chat_regenerate._save_slot_to_history",
        side_effect=OSError("disk full"),
    ):
        with caplog.at_level("WARNING"):
            async with _client(state) as client:
                resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})

    assert resp.status == 200
    assert "switch-variant: failed to persist" in caplog.text
    assert slot.messages[-1]["content"] == "v1"


# ── edit-resend ──


@pytest.mark.asyncio
async def test_edit_resend_by_ts_truncates_and_resends(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "deploy alpha", ts="t1")
    slot.append("assistant", "deployed alpha", ts="t2")
    slot.append("user", "deploy beta", ts="t3")
    slot.append("assistant", "deployed beta", ts="t4")
    slot.drain()
    run = AsyncMock()

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"ts": "t3", "content": "  deploy gamma  "},
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    assert [m["content"] for m in slot.messages] == [
        "deploy alpha",
        "deployed alpha",
        "deploy gamma",
    ]
    assert run.await_args.args[2] == "deploy gamma"
    assert state.push_slots_update.called


@pytest.mark.asyncio
async def test_edit_resend_by_index_truncates_from_that_row(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    assert [m["content"] for m in slot.messages] == ["edited"]


@pytest.mark.asyncio
async def test_edit_resend_redacts_the_edited_content(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "use AKIAIOSFODNN7EXAMPLE please"},
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    assert "AKIAIOSFODNN7EXAMPLE" not in slot.messages[-1]["content"]
    assert "AKIAIOSFODNN7EXAMPLE" not in run.await_args.args[2]


@pytest.mark.asyncio
async def test_edit_resend_unknown_slot_is_404(state) -> None:
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/nope/edit-resend", json={"index": 0, "content": "x"}
        )
    assert resp.status == 404


@pytest.mark.asyncio
async def test_edit_resend_rejects_a_non_json_body(state) -> None:
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "invalid JSON"


@pytest.mark.asyncio
async def test_edit_resend_rejects_a_non_object_body(state) -> None:
    """A valid-JSON array has no .get() -- without the guard this is a 500."""
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/edit-resend", json=["x"])
    assert resp.status == 400


@pytest.mark.asyncio
async def test_edit_resend_requires_non_blank_content(state) -> None:
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        for body in ({"index": 0, "content": "   "}, {"index": 0}):
            resp = await client.post("/api/chat/slots/s1/edit-resend", json=body)
            assert resp.status == 400
            assert (await resp.json())["error"] == "content is required"


@pytest.mark.asyncio
async def test_edit_resend_rejects_a_non_string_content(state) -> None:
    """A PRESENT non-string ``content`` has no ``.strip()``.

    Without the type check this is an ``AttributeError`` -> 500 on a body a
    caller can trivially send, so the failure is unreadable rather than a 400
    naming the field. ``None`` stays out of it: an empty composer sends that and
    must keep answering ``content_required``.
    """
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        for bad in (123, True, {"text": "x"}, ["x"]):
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": bad}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_content"
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": None}
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "content_required"


@pytest.mark.asyncio
async def test_edit_resend_rejects_an_oversize_content(state) -> None:
    """The cap matches the sibling ``rewind``/``fork`` boundaries.

    One edit of the same message must not be accepted by one endpoint and
    refused by another, and the refusal must land BEFORE the destructive
    boundary rather than after the native conversation is already discarded.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()
    state.sessions.discard_conversation = AsyncMock(return_value=True)
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend",
            json={"index": 0, "content": "x" * (chat_regenerate._MAX_EDIT_CONTENT_CHARS + 1)},
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "content_too_long"
    # The refusal is pre-boundary: nothing was discarded and the window stands.
    state.sessions.discard_conversation.assert_not_awaited()
    assert [m["content"] for m in slot.messages] == ["first"]


@pytest.mark.asyncio
async def test_edit_resend_unknown_ts_is_rejected(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first", ts="t1")
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend", json={"ts": "t9", "content": "edited"}
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "user message not found for ts"
    assert len(slot.messages) == 1


@pytest.mark.asyncio
async def test_edit_resend_index_must_point_at_a_user_row(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend", json={"index": 1, "content": "edited"}
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "index is not a user message"


@pytest.mark.asyncio
async def test_edit_resend_needs_an_index_or_a_ts(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    async with _client(state) as client:
        for body in ({"content": "edited"}, {"index": 99, "content": "edited"}):
            resp = await client.post("/api/chat/slots/s1/edit-resend", json=body)
            assert resp.status == 400
            assert (await resp.json())["error"] == "index or ts required"


@pytest.mark.asyncio
async def test_edit_resend_rejected_while_a_turn_is_in_flight(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    await _busy(slot)
    try:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
        assert resp.status == 409
        assert [m["content"] for m in slot.messages] == ["first"]
    finally:
        slot.task.cancel()


@pytest.mark.asyncio
async def test_edit_resend_readiness_latch_blocks_before_the_truncation(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    blocked = web.json_response({"error": "kiro not verified"}, status=503)

    with patch(
        "kiro_crew.dashboard.chat_regenerate.reject_if_kiro_unverified",
        new=AsyncMock(return_value=blocked),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )

    assert resp.status == 503
    assert [m["content"] for m in slot.messages] == ["first"]


@pytest.mark.asyncio
async def test_edit_resend_rejects_when_the_history_save_raises(state, caplog) -> None:
    """A failed rewrite is now a retryable 503 (was log-and-continue 200): the
    live slot is untouched and no replacement turn is dispatched from state that
    was never persisted."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    run = AsyncMock()

    with (
        patch(
            "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
            side_effect=OSError("disk full"),
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        with caplog.at_level("WARNING"):
            async with _client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/edit-resend",
                    json={"index": 0, "content": "edited"},
                )
                assert resp.status == 503
                assert (await resp.json())["code"] == "edit_resend_save_failed"
                await asyncio.sleep(0)

    assert "edit-resend: failed to persist" in caplog.text
    assert slot.messages == original_messages
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_rejects_when_the_save_is_refused(state) -> None:
    """A save refused by its own guards (returns False) must 503, not dispatch:
    the session was deleted or the slot rebound while the write awaited its
    lock, so nothing was persisted."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    run = AsyncMock()

    with (
        patch(
            "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
            MagicMock(return_value=False),
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "edit_resend_save_failed"
            await asyncio.sleep(0)

    assert slot.messages == original_messages
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_rejects_when_the_native_boundary_cannot_be_discarded(state) -> None:
    """A failed discard leaves the original branch in place with a retryable
    503 -- no history rewrite, no replacement turn."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    state.sessions.discard_conversation = AsyncMock(side_effect=OSError("map write failed"))
    run = AsyncMock()

    with (
        patch("kiro_crew.dashboard.chat_persistence._save_slot_to_history") as save,
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "edit_resend_prepare_failed"
            await asyncio.sleep(0)

    assert slot.messages == original_messages
    save.assert_not_called()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_rejects_when_the_sid_flush_fails(state) -> None:
    """The cleared resume sid must be durable before the commit: a flush failure
    takes the same 503 prepare-failed path as a failed discard."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    state.sessions.aflush = AsyncMock(side_effect=OSError("map write failed"))
    run = AsyncMock()

    with (
        patch("kiro_crew.dashboard.chat_persistence._save_slot_to_history") as save,
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "edit_resend_prepare_failed"
            await asyncio.sleep(0)

    assert slot.messages == original_messages
    state.sessions.discard_conversation.assert_awaited_once_with("dashboard:s1", skip_if_busy=True)
    state.sessions.aflush.assert_awaited_once()
    save.assert_not_called()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_refuses_a_busy_session_with_409(state) -> None:
    """A busy native session (discard returns False, an inbound channel reply in
    flight) must 409 with the slot untouched and no flush/save/dispatch."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    state.sessions.discard_conversation = AsyncMock(return_value=False)
    run = AsyncMock()

    with (
        patch("kiro_crew.dashboard.chat_persistence._save_slot_to_history") as save,
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "edit_resend_session_busy"
            await asyncio.sleep(0)

    assert slot.messages == original_messages
    state.sessions.discard_conversation.assert_awaited_once_with("dashboard:s1", skip_if_busy=True)
    state.sessions.aflush.assert_not_awaited()
    save.assert_not_called()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_discards_the_native_conversation_before_persisting(state) -> None:
    """The happy path clears the native conversation (once, skip_if_busy) BEFORE
    the history save, and dispatches the edited turn only after both boundaries
    commit."""
    order: list[str] = []
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    async def _discard(key, **kwargs):
        order.append(f"discard:{key}:{kwargs.get('skip_if_busy')}")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

    def _save(*_args, **_kwargs):
        order.append("save")
        return True

    run = AsyncMock()
    with (
        patch("kiro_crew.dashboard.chat_persistence._save_slot_to_history", side_effect=_save),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    # Discard the native conversation (exactly once) before persistence.
    state.sessions.discard_conversation.assert_awaited_once_with("dashboard:s1", skip_if_busy=True)
    assert order == ["discard:dashboard:s1:True", "save"]
    # The live slot only adopts the edit after the boundaries commit.
    assert [m["content"] for m in slot.messages] == ["edited"]
    # The edited turn is dispatched after the commit.
    run.assert_awaited_once()
    assert run.await_args.args[2] == "edited"


@pytest.mark.asyncio
async def test_edit_resend_cancelled_mid_save_keeps_live_and_disk_in_sync(state) -> None:
    """A client disconnect during the save must not desync disk from the live
    slot. The worker thread finishes the destructive rewrite regardless of the
    handler's fate; on cancellation the handler waits for the worker's outcome,
    commits the live slot to match the persisted window, and still dispatches
    the edited prompt. Mirrors
    test_dashboard_chat_rewind::test_rewind_cancelled_mid_save_still_commits_the_landed_rewrite.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    save_started = threading.Event()
    release = threading.Event()
    saved_windows: list[list[str]] = []

    def _gated_save(_state, _slot, msgs_snapshot, **_kwargs):
        # Record the window the worker thread persisted to "disk", then block
        # so the test can cancel the handler while the save is in flight.
        saved_windows.append([m["content"] for m in msgs_snapshot])
        save_started.set()
        # BOUNDED, and the release below is in a ``finally``. This blocks a
        # thread in the DEFAULT executor, which the interpreter joins at exit --
        # so a gate that is never released does not fail this test, it hangs
        # interpreter shutdown for the whole worker. Neither half is redundant:
        # the ``finally`` covers a failure inside the ``with`` block, and the
        # timeout covers a failure that prevents the ``finally`` from running at
        # all (a hard kill of the awaiting task).
        release.wait(timeout=_GATE_TIMEOUT_SECS)
        return True

    run = AsyncMock()
    try:
        with (
            patch(
                "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
                side_effect=_gated_save,
            ),
            patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
        ):
            app = _make_regen_app(state)
            fake_request = make_mocked_request(
                "POST", "/api/chat/slots/s1/edit-resend", match_info={"slot": "s1"}, app=app
            )
            fake_request["app"] = ""

            async def _json():
                return {"index": 0, "content": "edited"}

            fake_request.json = _json  # type: ignore[method-assign]
            handler_task = asyncio.create_task(api_chat_slot_edit_resend(fake_request))
            # The INNER wait is bounded too, and that is the load-bearing half:
            # cancelling ``wait_for`` abandons the future but cannot interrupt the
            # worker thread already sitting in ``Event.wait()``, so an unbounded
            # inner wait strands a default-executor thread that interpreter
            # shutdown then joins on. The outer 2s stays tight so a save that
            # never starts still fails this test fast.
            await asyncio.wait_for(
                asyncio.to_thread(save_started.wait, _GATE_TIMEOUT_SECS), timeout=2
            )
            handler_task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await handler_task

            # The rewrite landed on "disk" with the truncated+edited window; the
            # live slot must have adopted the SAME window rather than keeping the
            # full original one (which the next flush would push back over disk).
            assert saved_windows == [["edited"]]
            assert [m["content"] for m in slot.messages] == ["edited"]
            # The edited prompt is still dispatched.
            for _ in range(50):
                if run.await_count:
                    break
                await asyncio.sleep(0.02)
            run.assert_awaited_once()
            assert run.await_args.args[2] == "edited"
    finally:
        # Unconditional: a failure anywhere above must still let the gated
        # worker thread exit, or it outlives this test and blocks the
        # interpreter's executor join at shutdown.
        release.set()


@pytest.mark.asyncio
async def test_edit_resend_excludes_the_periodic_flush_during_the_rewrite(state) -> None:
    """The periodic dirty-slot flush must not race the rewrite.

    The live slot keeps the FULL window until the commit, so a flush tick can
    snapshot that stale window, block behind this rewrite on the per-session
    history lock, and then write the snapshot back on top -- restoring every
    message the rewrite just discarded. Routing the save through
    ``save_slot_off_loop`` with an ``expected_history_key`` raises
    ``_metadata_persist_inflight``, which is the flag ``flush_slot_now`` already
    honours to skip a slot with a guarded write pending.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    observed: list[int] = []
    flush_ran: list[bool] = []

    def _save_observing_inflight(_state, saved_slot, *_args, **_kwargs):
        # Runs on the worker thread WHILE the guard should be held.
        observed.append(getattr(saved_slot, "_metadata_persist_inflight", 0))
        # A real flush tick landing here must decline to write this slot. Its
        # only observable "I declined" is leaving the dirty bit set, since a
        # completed flush clears it.
        state.flush_slot_now(saved_slot)
        flush_ran.append(bool(saved_slot._dirty))
        return True

    with (
        patch(
            "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
            side_effect=_save_observing_inflight,
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    # The guard was held for the duration of the rewrite ...
    assert observed and all(count > 0 for count in observed)
    # ... and the concurrent flush declined rather than writing the stale window
    # (it returned without clearing the dirty bit).
    assert flush_ran == [True]
    # Released afterwards, or the slot would never flush again.
    assert slot._metadata_persist_inflight == 0


@pytest.mark.asyncio
async def test_edit_resend_repeated_cancellation_still_commits_the_landed_rewrite(state) -> None:
    """A SECOND cancellation must not abandon the rewrite.

    A gateway shutdown can cancel a handler already unwinding from a client
    disconnect, and ``CancelledError`` is a ``BaseException`` -- so an
    ``except Exception`` around the drain cannot absorb it and a bare
    ``await save_task`` walks away from a rewrite the worker thread finishes
    anyway: disk truncated, live slot still holding the discarded suffix, and the
    next flush pushing that stale window back over the truncated file.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    save_started = threading.Event()
    release = threading.Event()

    def _gated_save(_state, _slot, msgs_snapshot, **_kwargs):
        save_started.set()
        release.wait(timeout=_GATE_TIMEOUT_SECS)
        return True

    run = AsyncMock()
    try:
        with (
            patch(
                "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
                side_effect=_gated_save,
            ),
            patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
        ):
            app = _make_regen_app(state)
            fake_request = make_mocked_request(
                "POST", "/api/chat/slots/s1/edit-resend", match_info={"slot": "s1"}, app=app
            )
            fake_request["app"] = ""

            async def _json():
                return {"index": 0, "content": "edited"}

            fake_request.json = _json  # type: ignore[method-assign]
            handler_task = asyncio.create_task(api_chat_slot_edit_resend(fake_request))
            await asyncio.wait_for(
                asyncio.to_thread(save_started.wait, _GATE_TIMEOUT_SECS), timeout=2
            )
            # First cancel: the client disconnected. The handler is now inside the
            # drain, waiting on the still-blocked worker.
            handler_task.cancel()
            await asyncio.sleep(0)
            # Second cancel: the gateway is shutting down. This is the one a bare
            # await loses.
            handler_task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await handler_task

            # Disk was rewritten, so the live slot MUST match it rather than
            # keeping the full original window.
            assert [m["content"] for m in slot.messages] == ["edited"]
            for _ in range(50):
                if run.await_count:
                    break
                await asyncio.sleep(0.02)
            run.assert_awaited_once()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_edit_resend_logs_a_failing_background_turn(state, caplog) -> None:
    """The task is fire-and-forget, so its exception must be surfaced by the
    done-callback or it is swallowed entirely."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()

    with patch(
        "kiro_crew.dashboard.chat_regenerate._run_chat",
        new=AsyncMock(side_effect=RuntimeError("backend exploded")),
    ):
        with caplog.at_level("ERROR"):
            async with _client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/edit-resend",
                    json={"index": 0, "content": "edited"},
                )
                assert resp.status == 200
                await asyncio.sleep(0)
                await asyncio.sleep(0)

    assert "edit-resend _run_chat failed" in caplog.text


# ── edit-resend: the prospective copy must not touch the live slot ──
# ``copy.copy`` is shallow, so reassigning ``messages`` alone leaves every other
# mutable attribute aliased to the live slot's object -- and ``append`` writes
# through four of them. These pin that a REFUSED edit leaves all four alone, and
# that the commit is what adopts them.


def _arm_pending_question(slot) -> tuple[str, list]:
    """Register one non-blocking question card and a retirement spy on *slot*."""
    announced: list = []
    slot._question_pending = {"q1": {"blocking": False, "prompt": "which host?"}}
    slot._on_question_retired = lambda key, ids: announced.append((key, list(ids)))
    return "q1", announced


@pytest.mark.asyncio
async def test_edit_resend_refusal_leaves_the_live_pending_and_cards_alone(state) -> None:
    """A refused edit must publish nothing to the live slot.

    The prospective ``append`` runs BEFORE all four rejection points, so an
    un-severed shallow copy pushes the edited row into the live ``_pending``
    queue (the open stream reader's next drain renders it), wakes ``event``, and
    announces the live question cards as retired -- leaving a phantom row on
    screen and a card-less "needs input" behind for an edit the server refused.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    slot.event.clear()
    question_id, announced = _arm_pending_question(slot)
    state.sessions.discard_conversation = AsyncMock(side_effect=OSError("map write failed"))

    with (
        patch("kiro_crew.dashboard.chat_persistence._save_slot_to_history"),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 503
            await asyncio.sleep(0)

    assert slot._pending == []
    assert not slot.event.is_set()
    assert announced == []
    assert question_id in slot._question_pending


@pytest.mark.asyncio
async def test_edit_resend_commit_adopts_the_prepared_pending_and_retires_cards(state) -> None:
    """The commit is the ONE place the prepared state becomes live.

    Severing the copy must not lose the work: on success the edited row still
    reaches the live pending queue, ``event`` is still woken for the stream
    reader, and the question retirement the prospective append computed is
    announced HERE, through the live callback the copy was denied.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    slot.event.clear()
    question_id, announced = _arm_pending_question(slot)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    assert [m["content"] for m in slot._pending] == ["edited"]
    assert slot.event.is_set()
    assert announced == [("s1", [question_id])]
    assert question_id not in slot._question_pending


@pytest.mark.asyncio
async def test_edit_resend_commit_advances_the_lifetime_message_counter(state) -> None:
    """``total_messages`` is a lifetime counter, and the prospective ``append``
    bumps only the COPY's int.

    Left stale, the edited row is invisible to every reader of it:
    ``_get_active_workspace`` picks the max-counter slot to decide which
    workspace's lessons to load, and the Slack mirror compares the counter
    against its own start value to decide whether anything happened. A refused
    edit must not advance it either.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    before = slot.total_messages

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    # One new row landed, so the lifetime counter advanced by exactly one --
    # truncating the window deliberately does not roll it back.
    assert slot.total_messages == before + 1

    # A refused edit leaves it alone.
    state.sessions.discard_conversation = AsyncMock(side_effect=OSError("map write failed"))
    with (
        patch("kiro_crew.dashboard.chat_persistence._save_slot_to_history"),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "again"}
            )
            assert resp.status == 503
            await asyncio.sleep(0)

    assert slot.total_messages == before + 1


# ── edit-resend: app isolation ──
# This endpoint discards the slot's NATIVE ACP conversation, so an app token
# reaching a slot it does not own destroys a resume identity it has no claim on.


def _app_request(state, slot_name: str, app_token: str, body: dict):
    """A mocked edit-resend request carrying *app_token* as its app identity."""
    request = make_mocked_request(
        "POST",
        f"/api/chat/slots/{slot_name}/edit-resend",
        match_info={"slot": slot_name},
        app=_make_regen_app(state),
    )
    request["app"] = app_token

    async def _json():
        return body

    request.json = _json  # type: ignore[method-assign]
    return request


@pytest.mark.asyncio
async def test_edit_resend_denies_an_app_that_does_not_own_the_slot(state) -> None:
    """404 rather than 403: a non-owning token must not be able to use the status
    code to probe which slots exist. Nothing is discarded and nothing moves."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        resp = await api_chat_slot_edit_resend(
            _app_request(state, "s1", "other-app", {"index": 0, "content": "edited"})
        )

    assert resp.status == 404
    assert resp.text is not None and "slot_not_found" in resp.text
    assert slot.messages == original_messages
    state.sessions.discard_conversation.assert_not_awaited()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_denies_an_app_reaching_a_channel_linked_session(state) -> None:
    """Owning the slot is not owning the session it is linked to.

    ``effective_session_key`` resolves a channel-linked slot onto the channel's
    own conversation, so an app edit-resend would discard the native identity of
    a session the app does not own. Same 404 shape (anti-enumeration).
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    slot._app = "some-app"
    slot.linked_session_key = "slack:1234567890.123"
    original_messages = list(slot.messages)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        resp = await api_chat_slot_edit_resend(
            _app_request(state, "s1", "some-app", {"index": 0, "content": "edited"})
        )

    assert resp.status == 404
    assert slot.messages == original_messages
    state.sessions.discard_conversation.assert_not_awaited()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_reauthorizes_the_slot_after_the_body_read(state) -> None:
    """Reading the body is an await, and a slot can be replaced across it.

    A delete-and-recreate under the same name is a DIFFERENT conversation that
    would pass any name-based re-check, so the guard requires the same slot
    OBJECT. Not app-only: a dashboard caller must not land a destructive edit on
    a replaced slot either.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    request = make_mocked_request(
        "POST",
        "/api/chat/slots/s1/edit-resend",
        match_info={"slot": "s1"},
        app=_make_regen_app(state),
    )
    request["app"] = ""

    async def _json():
        # The replacement lands while the body is being read.
        state._slots["s1"] = state.get_or_create_slot("s2")
        return {"index": 0, "content": "edited"}

    request.json = _json  # type: ignore[method-assign]

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        resp = await api_chat_slot_edit_resend(request)

    assert resp.status == 404
    state.sessions.discard_conversation.assert_not_awaited()
    run.assert_not_awaited()


# ── edit-resend: a busy SESSION is not the same question as a busy slot ──
# ``discard_conversation`` is a full teardown: it drops the native conversation
# AND releases the shared sub-agent runtime. ``slot.running`` tracks only this
# slot's own task, so it answers False in both states below.


@pytest.mark.asyncio
async def test_edit_resend_refuses_while_a_plan_is_mid_stage(state) -> None:
    """An autopilot plan reads ``running`` False BETWEEN stages while still
    mid-plan, so ``running`` alone would discard the conversation the plan is
    writing into and truncate the history it is producing. Same 409 code the
    sibling reset-conversation teardown returns."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    slot._in_stage_execution = True
    original_messages = list(slot.messages)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "slot_orchestrating"

    assert slot.messages == original_messages
    state.sessions.discard_conversation.assert_not_awaited()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_refuses_while_subagents_are_attached(state) -> None:
    """The discard releases the shared runtime the parent's children run on, and
    the parent turn ends FIRST -- so ``running`` is False while they keep going.
    Without this guard an edit destroys work it has no part in."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    subs = MagicMock()
    subs.running_agents_for = MagicMock(return_value=["child-1"])
    subs._queued_depth = MagicMock(return_value=0)
    state.subagents = subs

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "slot_subagents_running"

    # Probed on the session the discard would have torn down, not on the slot name.
    subs.running_agents_for.assert_called_with("dashboard:s1")
    assert slot.messages == original_messages
    state.sessions.discard_conversation.assert_not_awaited()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_refuses_when_the_subagent_probe_fails(state) -> None:
    """An unreadable probe is UNKNOWN children, not zero children. The shared
    predicate fails closed on a None running-probe; pinning it here keeps this
    endpoint from being the one that reads a failure as "safe to tear down"."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    subs = MagicMock()
    subs.running_agents_for = MagicMock(return_value=None)
    state.subagents = subs

    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "slot_subagents_running"

    state.sessions.discard_conversation.assert_not_awaited()


# ── edit-resend: the slot is reserved across the durable boundaries ──


@pytest.mark.asyncio
async def test_edit_resend_reserves_the_slot_so_a_concurrent_send_queues(state) -> None:
    """``slot.running`` must read True while the boundaries are pending.

    ``running`` derives from ``slot.task`` and the send path is not serialized on
    ``slot._lock``, so without the reservation a send arriving during the three
    awaited boundaries sees an idle slot, appends its row and dispatches a
    competing turn -- which the commit would then erase. Reserved, that send
    takes the queue path instead, and the commit leaves the entry alone.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    observed: dict = {}

    async def _discard(key, **kwargs):
        observed["running"] = slot.running
        observed["arrived_id"] = slot.queue_append("sent during the edit")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    assert observed["running"] is True
    assert observed["arrived_id"] in [entry["id"] for entry in slot._queue]


@pytest.mark.asyncio
async def test_edit_resend_abort_hands_a_diverted_send_to_the_queue_drain(state) -> None:
    """A send diverted by the reservation must never be stranded.

    On abort no turn ran, so the entry the reservation pushed to the queue has
    no drain trigger of its own; the reserved task hands it to the canonical
    successor dispatch, which re-validates holds before starting anything.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    async def _discard(key, **kwargs):
        slot.queue_append("sent during the edit")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)
    state.sessions.aflush = AsyncMock(side_effect=OSError("map write failed"))
    drain = AsyncMock(return_value=True)

    with (
        patch("kiro_crew.dashboard.chat_regenerate._start_next_queued_turn", new=drain),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run,
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 503
            for _ in range(50):
                if drain.await_count:
                    break
                await asyncio.sleep(0.02)

    drain.assert_awaited_once()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_abort_leaves_a_pre_existing_queue_entry_waiting(state) -> None:
    """An entry queued BEFORE the reservation keeps its own trigger.

    Only a send DIVERTED by this reservation lost its drain, so an abort must not
    dispatch on behalf of work that was already waiting -- that would start a
    turn the user never unblocked.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    slot.queue_append("queued long before the edit")
    state.sessions.aflush = AsyncMock(side_effect=OSError("map write failed"))
    drain = AsyncMock(return_value=True)

    with (
        patch("kiro_crew.dashboard.chat_regenerate._start_next_queued_turn", new=drain),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run,
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 503
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    drain.assert_not_awaited()
    run.assert_not_awaited()
    assert len(slot._queue) == 1


# ── edit-resend: rows that arrive during the boundary belong to the new timeline ──


@pytest.mark.asyncio
async def test_edit_resend_commit_keeps_a_row_injected_during_the_boundary(state) -> None:
    """A workflow/cron completion landing mid-boundary must survive the commit.

    Those injectors append WITHOUT taking ``slot._lock`` -- ``workflow_inject``
    calls ``append_and_surface`` straight on the event loop -- so a wholesale
    ``slot.messages = prospective_slot.messages`` silently drops the injected
    row. The rewrite save cannot put it back either: a rewrite deliberately
    skips the cross-process-append scan, so carrying it in the live window is
    what keeps it. It reaches disk on the next ordinary flush, which is why the
    boundary itself writes exactly ONCE.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    saved_windows: list[list[str]] = []

    async def _discard(key, **kwargs):
        # Stands in for inject_workflow_result: an append on the live slot while
        # this handler holds slot._lock.
        slot.append("assistant", "workflow finished", "msg msg-a")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

    def _save(_state, saved_slot, msgs_snapshot=None, **_kwargs):
        window = msgs_snapshot if msgs_snapshot is not None else saved_slot.messages
        saved_windows.append([m["content"] for m in window])
        return True

    with (
        patch("kiro_crew.dashboard.chat_persistence._save_slot_to_history", side_effect=_save),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    # The truncation still happened, the edit is live, and the injected row was
    # carried rather than replaced away.
    assert [m["content"] for m in slot.messages] == ["edited", "workflow finished"]
    assert "workflow finished" in [m["content"] for m in slot._pending]
    # Order is not accidental, and the claim is "never EARLIER" rather than
    # "strictly later". ``monotonic_transcript_ts`` only ever moves a row
    # forward, and it floors on the window tail the appender saw: the edited row
    # was floored on the (empty) truncated prefix, the arrived row on the
    # original tail. On a coarse clock -- Windows advances the system clock in
    # ~15.6 ms steps, which is exactly why that helper exists -- both reads of
    # ``now`` return the same instant and the two rows legitimately carry an
    # IDENTICAL ts. Asserting ``<`` passed on Linux and failed on Windows for
    # that reason. What must hold is that the merge never stamps the arrived row
    # BEFORE the edited one, which would reorder the transcript; list order is
    # what separates a tie, and the content assertion above pins that.
    assert slot.messages[0]["ts"] <= slot.messages[1]["ts"]
    # The boundary writes ONCE, and the carried row is left to the ordinary flush.
    # A second guarded save for it would have to be awaited after the commit,
    # where ``dispatch_commit`` is already True and only ``dispatch_ready.set()``
    # remains -- so a rebind landing on that await would release this handler's
    # prompt against another conversation. ``_dirty`` is what makes the merged
    # window durable instead.
    assert saved_windows == [["edited"]]
    assert slot._dirty is True


@pytest.mark.asyncio
async def test_edit_resend_commit_does_not_resurrect_a_card_retired_meanwhile(state) -> None:
    """The prospective question map is PRE-await, so adopting it wholesale would
    restore a card an arrived row already retired -- re-rendering a card whose
    answer channel is gone. The commit intersects instead: retired by either
    side stays retired."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    slot._question_pending = {
        "q1": {"blocking": False, "prompt": "which host?"},
        "q2": {"blocking": False, "prompt": "which region?"},
    }
    announced: list = []
    slot._on_question_retired = lambda key, ids: announced.append((key, sorted(ids)))

    async def _discard(key, **kwargs):
        # An arrived user-role row retires every live non-blocking card.
        slot.append("user", "asked elsewhere")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    assert slot._question_pending == {}
    # The arrived append already announced the retirement; the commit must not
    # announce the same ids a second time.
    assert announced == [("s1", ["q1", "q2"])]


# ── edit-resend: the commit re-checks the transcript it was authorized against ──


@pytest.mark.asyncio
async def test_edit_resend_refuses_the_commit_when_the_slot_is_rebound(state) -> None:
    """A slot rebound to another transcript mid-save must not be replaced.

    A cron injection can re-link the slot -- hydrating it with another
    conversation's state -- while the history write is in flight. The save's own
    ``expected_history_key`` guard cannot see it, because the snapshot froze the
    old routing; this loop-side re-check is the only fence that can, and without
    it the commit silently overwrites the injected conversation.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)

    async def _rebinding_discard(key, **kwargs):
        # The slot moves to another transcript while the edit persists. The
        # save is stubbed to accept the write, so ONLY the commit-side re-check
        # can refuse -- which is exactly what this pins.
        slot.linked_session_key = "slack:9876543210.999"
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_rebinding_discard)
    run = AsyncMock()

    with (
        patch(
            "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
            MagicMock(return_value=True),
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "edit_resend_slot_rebound"
            await asyncio.sleep(0)

    assert slot.messages == original_messages
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_refuses_the_commit_when_the_slot_is_replaced(state) -> None:
    """A close-and-recreate under the same name is a DIFFERENT conversation.

    The transcript key is unchanged by such a swap, so the rebind fence cannot
    see it; identity has to be the slot OBJECT -- the same discipline
    ``_reauthorize_after_await`` applies across the body-read await.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)

    async def _replacing_discard(key, **kwargs):
        # Same name, different object -- as a close-and-recreate produces.
        state._slots["s1"] = state.get_or_create_slot("s2")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_replacing_discard)
    run = AsyncMock()

    with (
        patch(
            "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
            MagicMock(return_value=True),
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "edit_resend_slot_rebound"
            await asyncio.sleep(0)

    assert slot.messages == original_messages
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_refuses_the_commit_when_the_reservation_is_displaced(state) -> None:
    """If something else took ``slot.task``, committing would run this handler's
    turn ALONGSIDE whatever now owns the slot -- two concurrent turns writing one
    window. The reservation must still be the slot's task at commit time."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    usurpers: list = []

    async def _displacing_discard(key, **kwargs):
        async def _other_turn() -> None:
            await asyncio.sleep(10)

        # Another dispatcher claims the slot while the boundary is pending.
        usurpers.append(asyncio.create_task(_other_turn()))
        slot.task = usurpers[-1]
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_displacing_discard)
    run = AsyncMock()

    try:
        with (
            patch(
                "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
                MagicMock(return_value=True),
            ),
            patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
        ):
            async with _client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
                )
                assert resp.status == 503
                assert (await resp.json())["code"] == "edit_resend_slot_rebound"
                await asyncio.sleep(0)

        assert slot.messages == original_messages
        run.assert_not_awaited()
    finally:
        for pending in usurpers:
            pending.cancel()


# ── machine-readable refusal codes ──
# The tests above pin each refusal's human sentence. These pin the `code`
# beside it, which is the half a caller can branch on: "slot is running" is a
# developer sentence that a client must not string-match to tell a BUSY slot
# (retry once the turn ends) from a MISSING one (stop and refresh).


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("regenerate", None),
        ("switch-variant", {"index": 0}),
        ("edit-resend", {"index": 0, "content": "edited"}),
    ],
)
@pytest.mark.asyncio
async def test_every_endpoint_refuses_a_busy_slot_with_slot_running(state, path, body) -> None:
    """All three endpoints share one busy-slot refusal, so they must share one
    code -- a client that special-cases the retryable case cannot be asked to
    learn a different spelling per endpoint."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "hello v1")
    await _busy(slot)
    try:
        async with _client(state) as client:
            resp = await client.post(f"/api/chat/slots/s1/{path}", json=body)
            assert resp.status == 409
            payload = await resp.json()
            assert payload["code"] == "slot_running"
            # The human sentence is unchanged: the code is additive, so an
            # existing client that renders `error` keeps working.
            assert payload["error"] == "slot is running"
    finally:
        slot.task.cancel()


@pytest.mark.parametrize(
    ("path", "body", "status", "code"),
    [
        ("regenerate", None, 404, "slot_not_found"),
        ("switch-variant", {"index": 0}, 404, "slot_not_found"),
        ("edit-resend", {"index": 0, "content": "x"}, 404, "slot_not_found"),
    ],
)
@pytest.mark.asyncio
async def test_unknown_slot_refusals_carry_slot_not_found(state, path, body, status, code) -> None:
    async with _client(state) as client:
        resp = await client.post(f"/api/chat/slots/nope/{path}", json=body)
        assert resp.status == status
        assert (await resp.json())["code"] == code


@pytest.mark.asyncio
async def test_no_variants_refusal_carries_its_own_code(state) -> None:
    """Distinct from a busy slot: nothing to switch to is permanent for this
    row, so a client must not offer a retry."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "only reply")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
        assert resp.status == 400
        payload = await resp.json()
        assert payload["code"] == "no_variants"
        assert payload["error"] == "no variants"
