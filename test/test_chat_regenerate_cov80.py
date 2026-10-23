"""Coverage for the guard and failure paths of
:mod:`kiro_crew.dashboard.chat_regenerate`.

``test_dashboard_chat_regenerate.py::TestRegenerateAndVariants`` covers the happy paths of
regenerate and variant switching. Untested there: ``edit-resend`` in its
entirety (it is not even wired into the shared test app), every 400/404/409
guard on all three endpoints, the readiness latch that must fire BEFORE the
destructive truncation, the persist-failure paths, and the two done-callbacks.

The app here registers the three handlers directly so ``edit-resend`` is
reachable; ``_run_chat`` is always patched, so no backend session is started.

Edit-resend's boundaries -- the prospective copy, app isolation, attached sub-agents, the
slot reservation, rows that arrive during the boundary and the commit's re-checks -- are in
``test_chat_regenerate_cov80_edit_resend_boundary.py``; the app and fixtures both modules
share are in ``chat_regenerate_cov80_helpers.py``.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from chat_regenerate_cov80_helpers import state_fixture  # noqa: F401
from chat_regenerate_cov80_helpers import _client, _make_regen_app

from kiro_crew.dashboard import chat_regenerate
from kiro_crew.dashboard.chat_regenerate import (
    api_chat_slot_edit_resend,
)
from kiro_crew.testing.wait import async_wait_until, finished

# Ceiling for the cross-thread gates below. Generous rather than tight: it is a
# deadlock backstop, never a synchronisation point, so a slow shared runner must
# not trip it -- every test that uses it also releases its gate in a ``finally``.
_GATE_TIMEOUT_SECS = 30


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
async def test_regenerate_skips_a_trailing_system_notice(state) -> None:
    """A trailing compaction/session-reload notice is a status row, not the
    reply being regenerated: the variant capture must take the real reply.
    Capturing the notice instead drops the reply from variant history with
    no recovery path."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "the real reply")
    slot.append("assistant", "auto compacted", meta={"kind": "compaction"})
    slot.drain()

    captured: list[list[dict]] = []

    async def _capture(*args, **kwargs) -> None:
        # Runs before the done-callback discards unconsumed variants.
        captured.append(list(slot._pending_variants))

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_capture):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    # The variant stash holds the reply, never the notice.
    assert captured, "background turn never started"
    contents = [v.get("content") for v in captured[0]]
    assert "the real reply" in contents
    assert "auto compacted" not in contents
    # Truncation still lands after the user row, dropping reply AND notice.
    assert [m["role"] for m in slot.messages] == ["user"]


@pytest.mark.asyncio
async def test_regenerate_never_crosses_a_newer_user_turn(state) -> None:
    """The notice skip must stop at a real user row: a /compact command is a
    user row followed by its notice, and skipping past it would regenerate the
    PRIOR turn -- irreversibly deleting the newer user turn. With no reply in
    the newest turn there is nothing to regenerate: refuse, mutate nothing."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "Q")
    slot.append("assistant", "A")
    slot.append("user", "/compact")
    slot.append("assistant", "auto compacted", meta={"kind": "compaction"})
    slot.drain()

    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/regenerate")
        assert resp.status == 400
        assert (await resp.json())["code"] == "no_assistant_message"

    # Nothing was truncated or persisted.
    assert [m["role"] for m in slot.messages] == ["user", "assistant", "user", "assistant"]


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
            "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
            new=AsyncMock(side_effect=OSError("disk full")),
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
@pytest.mark.parametrize("raw", ["1e999", "-1e999"])
async def test_switch_variant_rejects_an_infinite_index(state, raw) -> None:
    """JSON reads ``1e999`` as an infinite float, which ``int`` cannot convert."""
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/switch-variant",
            data='{"index": %s}' % raw,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert await resp.json() == {"error": "invalid index", "code": "index_invalid"}


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
        "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
        new=AsyncMock(side_effect=OSError("disk full")),
    ):
        with caplog.at_level("WARNING"):
            async with _client(state) as client:
                resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})

    assert resp.status == 200
    assert "switch-variant: failed to persist" in caplog.text
    assert slot.messages[-1]["content"] == "v1"


# ── recreate-race slot-identity guard ──
#
# Both truncating saves run under slot._lock but dispatch the write to a worker
# thread; the event loop is free across that one await, and a same-name
# close-and-recreate is NOT serialized against this lock (the cleanup pops
# state._slots[name] and get_or_create_slot re-inserts, neither taking the
# original lock). The rewrite resolves its target file from the slot object it
# was handed, so an unguarded write lands the truncation on the replacement's
# transcript. The fix carries the same PAIR edit-resend carries:
#   * expected_history_key = slot_history_key(slot), captured before the await,
#     so the save refuses when the routing resolves to a different key -- a
#     RENAMED replacement;
#   * a state._slots[name] object-identity check immediately before the write,
#     so a SAME-NAME recreate (which keeps the key identical, waving the routing
#     check through) skips the write instead.
# These tests assert the pin reaches the write, and that a same-name swap before
# the write suppresses it. The disk-side routing refusal itself is covered by
# _save_slot_to_history's own expected_history_key tests.


@pytest.mark.asyncio
async def test_regenerate_pins_the_truncating_write_to_its_transcript(state) -> None:
    slot = state.get_or_create_slot("s1", linked_session_key="orig:key")
    slot.append("user", "hi")
    slot.append("assistant", "keep-me")
    slot.drain()

    saved = AsyncMock(return_value=True)
    with (
        patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=saved),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            await asyncio.sleep(0)

    # The write carried the slot's own transcript and map key as its
    # authorization pins.
    assert saved.await_count == 1
    assert saved.await_args.kwargs["expected_history_key"] == "orig:key"
    assert saved.await_args.kwargs["expected_slot_name"] == "s1"


@pytest.mark.asyncio
async def test_regenerate_skips_the_write_when_the_slot_is_recreated(state) -> None:
    """A same-name recreate that lands INSIDE the executor wait -- after the
    caller dispatches the write, while the worker thread holds the lock --
    keeps the history key identical, so only an object-identity recheck at the
    locked commit boundary catches it. The truncating write must not commit
    onto the replacement's transcript."""
    slot = state.get_or_create_slot("s1", linked_session_key="orig:key")
    slot.append("user", "hi")
    slot.append("assistant", "keep-me-1")
    slot.append("user", "again")
    slot.append("assistant", "keep-me-2")
    slot.drain()
    # A replacement bound to the SAME transcript key -- what a recreate that
    # resumes the same session produces, so the routing check alone waves it
    # through.
    replacement = state.get_or_create_slot("s1b", linked_session_key="orig:key")

    real_status = state.conversation_log.get_metadata_status

    def _swap_inside_the_locked_write(key):
        # get_metadata_status runs inside the save's _locked region, before the
        # identity recheck -- model the recreate landing in that window.
        if state._slots.get("s1") is slot:
            state._slots["s1"] = replacement
        return real_status(key)

    with (
        patch.object(
            state.conversation_log,
            "get_metadata_status",
            side_effect=_swap_inside_the_locked_write,
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run,
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 409
            assert (await resp.json())["code"] == "regenerate_save_refused"
            await asyncio.sleep(0)

    # The truncation never reached disk for the transcript the replacement holds.
    assert state.conversation_log.get_metadata("orig:key") == {}
    # And the turn was not dispatched onto the removed slot.
    assert run.await_count == 0


@pytest.mark.asyncio
async def test_switch_variant_pins_the_persist_to_its_transcript(state) -> None:
    slot = state.get_or_create_slot("s1", linked_session_key="orig:key")
    slot.append("assistant", "v2")
    slot.messages[-1]["variants"] = [{"content": "v1", "ts": "t1"}, {"content": "v2", "ts": "t2"}]
    slot.drain()

    saved = AsyncMock(return_value=True)
    with patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=saved):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 200

    assert saved.await_count == 1
    assert saved.await_args.kwargs["expected_history_key"] == "orig:key"
    assert saved.await_args.kwargs["expected_slot_name"] == "s1"


@pytest.mark.asyncio
async def test_switch_variant_skips_the_write_when_the_slot_is_recreated(state) -> None:
    """Switch-variant carries the same object-identity recheck at the save's
    locked commit boundary: a same-name recreate landing inside the executor
    wait suppresses the persist onto the replacement's transcript."""
    slot = state.get_or_create_slot("s1", linked_session_key="orig:key")
    slot.append("assistant", "v2")
    slot.messages[-1]["variants"] = [{"content": "v1", "ts": "t1"}, {"content": "v2", "ts": "t2"}]
    slot.drain()
    replacement = state.get_or_create_slot("s1b", linked_session_key="orig:key")

    real_status = state.conversation_log.get_metadata_status

    def _swap_inside_the_locked_write(key):
        if state._slots.get("s1") is slot:
            state._slots["s1"] = replacement
        return real_status(key)

    with patch.object(
        state.conversation_log,
        "get_metadata_status",
        side_effect=_swap_inside_the_locked_write,
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 409
            assert (await resp.json())["code"] == "switch_variant_save_refused"

    assert state.conversation_log.get_metadata("orig:key") == {}
    # A refused persist must not announce a switch no transcript holds.
    assert not any(
        call.args and call.args[0] == "chat_variant_switch"
        for call in state.broadcast_ws.call_args_list
    )


# ── edit-resend ──


@pytest.mark.asyncio
async def test_edit_resend_pins_the_truncating_write_to_its_transcript(state) -> None:
    """Edit-resend carries BOTH axes into the write, like its two siblings.

    Its loop-side checks run before the save is dispatched, and the event loop
    is free from there until the worker commits, so neither of them decides the
    commit. ``expected_history_key`` alone leaves the same-name case open: a
    recreate resuming the same transcript keeps the key identical.
    """
    slot = state.get_or_create_slot("s1", linked_session_key="orig:key")
    slot.append("user", "deploy alpha", ts="t1")
    slot.append("assistant", "deployed alpha", ts="t2")
    slot.drain()

    saved = AsyncMock(return_value=True)
    with (
        patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=saved),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    assert saved.await_count == 1
    assert saved.await_args.kwargs["expected_history_key"] == "orig:key"
    assert saved.await_args.kwargs["expected_slot_name"] == "s1"


@pytest.mark.asyncio
async def test_edit_resend_skips_the_write_when_the_slot_is_recreated(state) -> None:
    """A same-name recreate landing inside the locked write suppresses the rewrite.

    The replacement is bound to the SAME transcript key, which is what a recreate
    resuming the same session produces, so the routing pin waves it through and
    only the object-identity recheck at the commit boundary refuses. The
    truncated window must not reach the transcript the replacement now holds, and
    the edited prompt must not be dispatched onto the slot being torn down.
    """
    slot = state.get_or_create_slot("s1", linked_session_key="orig:key")
    slot.append("user", "keep-me-1", ts="t1")
    slot.append("assistant", "keep-me-2", ts="t2")
    slot.append("user", "keep-me-3", ts="t3")
    slot.append("assistant", "keep-me-4", ts="t4")
    slot.drain()
    replacement = state.get_or_create_slot("s1b", linked_session_key="orig:key")

    real_status = state.conversation_log.get_metadata_status

    def _swap_inside_the_locked_write(key):
        # Runs inside the save's ``_locked`` region, before the identity
        # recheck -- the window a recreate lands in.
        if state._slots.get("s1") is slot:
            state._slots["s1"] = replacement
        return real_status(key)

    with (
        patch.object(
            state.conversation_log,
            "get_metadata_status",
            side_effect=_swap_inside_the_locked_write,
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run,
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "edit_resend_save_failed"
            await asyncio.sleep(0)

    assert state.conversation_log.get_metadata("orig:key") == {}
    assert run.await_count == 0
    # Nothing was mutated on the live slot either: the refusal happens before
    # the commit, so the original window is intact for a retry.
    assert [m["content"] for m in slot.messages] == [
        "keep-me-1",
        "keep-me-2",
        "keep-me-3",
        "keep-me-4",
    ]


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
async def test_edit_resend_delivers_the_owners_edit_as_typed(state) -> None:
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

    # No request app, so the edit is the session owner's own words: it is
    # delivered as typed into both the persisted row and the turn input, the
    # same rule an idle send and a steer follow. An app-driven edit still
    # redacts (test_queued_user_text_display covers that boundary).
    assert slot.messages[-1]["content"] == "use AKIAIOSFODNN7EXAMPLE please"
    assert run.await_args.args[2] == "use AKIAIOSFODNN7EXAMPLE please"


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
            await finished(
                asyncio.to_thread(save_started.wait, _GATE_TIMEOUT_SECS), "save_started", timeout=2
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
            await async_wait_until(lambda: bool(run.await_count))
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
            await finished(
                asyncio.to_thread(save_started.wait, _GATE_TIMEOUT_SECS), "save_started", timeout=2
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
            await async_wait_until(lambda: bool(run.await_count))
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
