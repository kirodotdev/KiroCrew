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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_regenerate import (
    api_chat_slot_edit_resend,
    api_chat_slot_regenerate,
    api_chat_slot_switch_variant,
)


def _make_regen_app(state) -> web.Application:
    """App exposing all three chat_regenerate routes, including edit-resend."""
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/regenerate", api_chat_slot_regenerate)
    app.router.add_post(
        "/api/chat/slots/{slot}/switch-variant", api_chat_slot_switch_variant
    )
    app.router.add_post("/api/chat/slots/{slot}/edit-resend", api_chat_slot_edit_resend)
    return app


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
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

    with patch(
        "kiro_crew.dashboard.chat_regenerate._save_slot_to_history",
        side_effect=OSError("disk full"),
    ), patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
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
            resp = await client.post(
                "/api/chat/slots/s1/switch-variant", json={"index": 0}
            )
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
                resp = await client.post(
                    "/api/chat/slots/s1/switch-variant", json={"index": 0}
                )

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
async def test_edit_resend_survives_a_persist_failure(state, caplog) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()

    with patch(
        "kiro_crew.dashboard.chat_regenerate._save_slot_to_history",
        side_effect=OSError("disk full"),
    ), patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        with caplog.at_level("WARNING"):
            async with _client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/edit-resend",
                    json={"index": 0, "content": "edited"},
                )
                assert resp.status == 200
                await asyncio.sleep(0)

    assert "edit-resend: failed to persist" in caplog.text


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
