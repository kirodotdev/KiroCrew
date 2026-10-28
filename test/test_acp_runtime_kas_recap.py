"""The KAS session recap on the runtime path.

The reader parks the recap KAS replays during ``session/load`` in a bounded
per-session slot outside the init-frame buffer, the session handle pops it once
for the dashboard's resume prefetch, and the pre-turn drain drops the recap KAS
sends after every turn without counting it as a leftover frame.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import _feed, _make_runtime, _register, _start_reader, _stop_reader

from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.runtime import _INIT_NOTIFICATION_BUFFER_LIMIT, AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    EVENT_TEXT_CHUNK,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_UPDATE,
    JsonRpcMessage,
)
from kiro_crew.testing.wait import async_wait_until
from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN


async def _await_count(seq, n: int, what: str) -> None:
    """Wait until the reader task has recorded *n* entries in *seq*.

    The reader runs as a task of its own, so the test waits on the count it
    records rather than on a sleep.
    """
    await async_wait_until(
        lambda: len(seq) >= n,
        describe=lambda: f"only {len(seq)} {what} recorded, expected {n}",
    )


def _kas_recap_wire(session_id: str, text: object) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": METHOD_SESSION_UPDATE,
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "session_info_update",
                "_meta": {"kiro": {"kind": "recap", "text": text}},
            },
        },
    }


async def _run_recap_turn(handle: AcpSessionHandle) -> list:
    async def _send(_method, _params):
        handle._queue.put_nowait(
            JsonRpcMessage.from_dict(
                {
                    "method": METHOD_SESSION_UPDATE,
                    "params": {
                        "sessionId": handle.session_id,
                        "update": {"sessionUpdate": "agent_message_chunk", "text": "turn output"},
                    },
                }
            )
        )
        handle._queue.put_nowait(JsonRpcMessage(id=9, result={"stopReason": "end_turn"}))
        return 9

    handle._runtime.send_request = AsyncMock(side_effect=_send)
    return [event async for event in handle.prompt("hi", timeout=1.0)]


@pytest.mark.asyncio
async def test_pre_turn_drain_drops_a_post_turn_recap_without_counting_it(caplog):
    rt, _, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KAS
    rt._kas_load_recaps["sR"] = "parked load recap"
    claim = MagicMock(wraps=rt.take_kas_load_recap)
    rt.take_kas_load_recap = claim
    queues = _register(rt, "sR")
    handle = AcpSessionHandle("sR", queues["sR"], rt)

    queues["sR"].put_nowait(
        JsonRpcMessage.from_dict(_kas_recap_wire("sR", "Goal: ship. Next: fix the red test."))
    )
    queues["sR"].put_nowait(
        JsonRpcMessage.from_dict(
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {"sessionId": "sR", "update": {"sessionUpdate": "plan"}},
            }
        )
    )

    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.session_handle"):
        events = await _run_recap_turn(handle)

    # A turn never reads the load slot: the parked recap is left for a later
    # take_session_recap (the resume prefetch), not shown at the turn's start.
    claim.assert_not_called()
    assert rt._kas_load_recaps == {"sR": "parked load recap"}
    assert handle.take_session_recap() == "parked load recap"
    # The turn's output comes first and no event carries either recap: not the
    # parked load recap, and not the post-turn recap KAS sent after the
    # previous turn.
    assert events[0].kind == EVENT_TEXT_CHUNK
    assert events[0].text == "turn output"
    assert "session_recap" not in [event.kind for event in events]
    assert all("recap" not in (event.text or "") for event in events)
    assert all("Goal: ship" not in (event.text or "") for event in events)
    # Only the plan frame is a leftover: the recap was expected, not discarded.
    leftovers = [r.getMessage() for r in caplog.records if "leftover frame" in r.getMessage()]
    assert len(leftovers) == 1 and "discarded 1 leftover frame" in leftovers[0]


@pytest.mark.asyncio
async def test_take_session_recap_pops_the_parked_load_recap_once():
    rt, _, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KAS
    rt._kas_load_recaps["sR"] = "Goal: resume. Next: run tests."
    claim = MagicMock(wraps=rt.take_kas_load_recap)
    rt.take_kas_load_recap = claim
    queues = _register(rt, "sR")
    handle = AcpSessionHandle("sR", queues["sR"], rt)

    # The first take is the only read that returns text; the slot is then
    # empty, so a second prefetch (or a resumed handle) gets nothing.
    assert handle.take_session_recap() == "Goal: resume. Next: run tests."
    assert handle.take_session_recap() is None
    assert [c.args for c in claim.call_args_list] == [("sR",), ("sR",)]
    assert rt._kas_load_recaps == {}

    # A later turn neither reads the slot again nor replays the taken recap.
    events = await _run_recap_turn(handle)
    assert claim.call_count == 2
    assert events[0].kind == EVENT_TEXT_CHUNK
    assert events[0].text == "turn output"
    assert all("Goal: resume" not in (event.text or "") for event in events)


def test_take_session_recap_reads_nothing_on_kiro():
    rt, _, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KIRO
    rt._kas_load_recaps["sR"] = "KAS-shaped recap"
    queues = _register(rt, "sR")
    handle = AcpSessionHandle("sR", queues["sR"], rt)
    claim = MagicMock(wraps=rt.take_kas_load_recap)
    rt.take_kas_load_recap = claim

    assert handle.take_session_recap() is None
    claim.assert_not_called()
    assert rt._kas_load_recaps == {"sR": "KAS-shaped recap"}


@pytest.mark.asyncio
async def test_load_window_recap_is_parked_outside_init_and_handle_queues():
    rt, reader, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KAS
    rt._session_inits_in_flight = 1
    task = await _start_reader(rt)
    try:
        _feed(reader, _kas_recap_wire("sid-77", "   "))
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {"sessionId": "sid-77", "update": {"sessionUpdate": "plan"}},
            },
        )
        await _await_count(rt._dropped_frames, 1, "dropped frames")
        assert rt._kas_load_recaps == {}
        assert not rt._pending_init_notifications

        _feed(reader, _kas_recap_wire("sid-77", "Goal: resume. Next: run tests."))
        await _await_count(rt._kas_load_recaps, 1, "parked recaps")
        queues = _register(rt, "sid-77")
        handle = AcpSessionHandle("sid-77", queues["sid-77"], rt)

        assert handle._queue.empty()
        assert not rt._pending_init_notifications
        assert rt.take_kas_load_recap("sid-77") == "Goal: resume. Next: run tests."
        assert rt.take_kas_load_recap("sid-77") is None
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_load_window_recap_claim_is_kas_only(caplog):
    rt, reader, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KIRO
    rt._session_inits_in_flight = 1

    # The reader parks the recap discriminant on KAS only: on kiro the same
    # frame takes the flood-drop path and nothing is parked or staged.
    task = await _start_reader(rt)
    try:
        _feed(reader, _kas_recap_wire("sid-79", "KAS-shaped recap"))
        await _await_count(rt._dropped_frames, 1, "dropped frames")
        assert rt._kas_load_recaps == {}
        assert not rt._pending_init_notifications
    finally:
        await _stop_reader(task)

    # The pre-turn drain's recap exemption is KAS-only too: on kiro a
    # recap-shaped frame waiting in the handle queue is an ordinary leftover
    # and is counted in the warning, where the KAS drain drops it uncounted
    # (test_pre_turn_drain_drops_a_post_turn_recap_without_counting_it).
    queues = _register(rt, "sid-79")
    handle = AcpSessionHandle("sid-79", queues["sid-79"], rt)
    queues["sid-79"].put_nowait(
        JsonRpcMessage.from_dict(_kas_recap_wire("sid-79", "post-turn recap shape"))
    )

    with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.session_handle"):
        events = await _run_recap_turn(handle)

    leftovers = [r.getMessage() for r in caplog.records if "leftover frame" in r.getMessage()]
    assert len(leftovers) == 1 and "discarded 1 leftover frame" in leftovers[0]
    assert events[0].kind == EVENT_TEXT_CHUNK
    assert all("recap" not in (event.text or "") for event in events)


@pytest.mark.asyncio
async def test_reader_loop_bounds_and_redacts_load_window_recap_before_retention():
    from kiro_crew.acp.session_handle import (
        _RECAP_MAX_CHARS,
        _RECAP_REDACT_INPUT_MAX_CHARS,
        _normalize_kas_recap_text,
    )

    rt, reader, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KAS
    rt._session_inits_in_flight = 1

    secret = "AKIAIOSFODNN7EXAMPLE"
    prefix = "x" * (_RECAP_MAX_CHARS - len(secret) // 2)
    accepted = (prefix + secret + ("y" * _RECAP_REDACT_INPUT_MAX_CHARS))[
        :_RECAP_REDACT_INPUT_MAX_CHARS
    ]
    raw_update = {
        "sessionUpdate": "session_info_update",
        "ignoredUpdateText": "drop this update field",
        "_meta": {
            "kiro": {
                "kind": "recap",
                "text": accepted,
                "ignoredKiroText": "drop this kiro field",
            }
        },
    }
    expected = _normalize_kas_recap_text(raw_update)
    assert expected

    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sid-78",
                    "ignoredParamsText": "drop this params field",
                    "update": raw_update,
                },
            },
        )
        await _await_count(rt._kas_load_recaps, 1, "parked recaps")

        assert rt._kas_load_recaps == {"sid-78": expected}
        assert len(rt._kas_load_recaps["sid-78"]) <= _RECAP_MAX_CHARS
        for fragment in ("AKIA", "IOSF", "ODNN", "7EXA", "MPLE"):
            assert fragment not in rt._kas_load_recaps["sid-78"]
        assert not rt._pending_init_notifications
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_reader_drops_overlong_and_non_string_load_window_recaps():
    from kiro_crew.acp.session_handle import _RECAP_REDACT_INPUT_MAX_CHARS

    rt, reader, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KAS
    rt._session_inits_in_flight = 1
    secret = "AKIAIOSFODNN7EXAMPLE"
    prefix = "x" * (_RECAP_REDACT_INPUT_MAX_CHARS - len(secret) // 2)
    rejected = (
        "x" * (_RECAP_REDACT_INPUT_MAX_CHARS + 1),
        prefix + secret + "y",
        {"unexpected": "shape"},
    )

    task = await _start_reader(rt)
    try:
        for index, text in enumerate(rejected):
            _feed(reader, _kas_recap_wire(f"sid-rejected-{index}", text))
        _feed(
            reader,
            {
                "method": METHOD_SESSION_UPDATE,
                "params": {
                    "sessionId": "sid-barrier",
                    "update": {"sessionUpdate": "plan"},
                },
            },
        )
        await _await_count(rt._dropped_frames, 1, "barrier frame")

        assert rt._kas_load_recaps == {}
        assert not rt._pending_init_notifications
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_reader_parks_recap_only_within_the_session_id_bound():
    rt, reader, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KAS
    rt._session_inits_in_flight = 1
    at_bound = "s" * MAX_ACP_SESSION_ID_LEN
    over_bound = "s" * (MAX_ACP_SESSION_ID_LEN + 1)

    task = await _start_reader(rt)
    try:
        _feed(reader, _kas_recap_wire(over_bound, "must drop"))
        _feed(reader, _kas_recap_wire(at_bound, "must park"))
        await _await_count(rt._kas_load_recaps, 1, "bounded session-id recap")

        assert rt._kas_load_recaps == {at_bound: "must park"}
        assert over_bound not in rt._kas_load_recaps
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_kas_load_recap_slot_evicts_oldest_and_refreshes_repeated_id():
    rt, reader, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KAS
    rt._session_inits_in_flight = 1
    update_key = runtime_mod._drop_key_part(METHOD_SESSION_UPDATE)
    first_evicted = (runtime_mod._drop_key_part("sid-1"), update_key)
    second_evicted = (runtime_mod._drop_key_part("sid-2"), update_key)
    task = await _start_reader(rt)
    try:
        for index in range(_INIT_NOTIFICATION_BUFFER_LIMIT):
            _feed(reader, _kas_recap_wire(f"sid-{index}", f"recap-{index}"))
        await _await_count(
            rt._kas_load_recaps,
            _INIT_NOTIFICATION_BUFFER_LIMIT,
            "parked recaps",
        )

        _feed(reader, _kas_recap_wire("sid-0", "replacement"))
        _feed(reader, _kas_recap_wire("sid-overflow", "overflow"))

        await async_wait_until(
            lambda: "sid-overflow" in rt._kas_load_recaps,
            describe=lambda: list(rt._kas_load_recaps)[-3:],
        )

        assert len(rt._kas_load_recaps) == _INIT_NOTIFICATION_BUFFER_LIMIT
        assert "sid-1" not in rt._kas_load_recaps
        assert rt._kas_load_recaps["sid-0"] == "replacement"
        assert list(rt._kas_load_recaps)[-2:] == ["sid-0", "sid-overflow"]
        assert rt._dropped_frames == {first_evicted: 1}

        _feed(reader, _kas_recap_wire("sid-overflow-2", "overflow-2"))
        await async_wait_until(
            lambda: "sid-overflow-2" in rt._kas_load_recaps,
            describe=lambda: list(rt._kas_load_recaps)[-3:],
        )
        assert rt._dropped_frames == {first_evicted: 1, second_evicted: 1}
    finally:
        await _stop_reader(task)


def test_reader_loop_staging_branch_covers_recap_updates():
    """The staging condition itself: with inits in flight on the KAS backend,
    a recap session/update is shaped for the staging branch (the shared
    kas_wire predicate), so it cannot take the flood-drop path."""
    from kiro_crew.acp import kas_wire

    params = {
        "sessionId": "s",
        "update": {
            "sessionUpdate": "session_info_update",
            "_meta": {"kiro": {"kind": "recap", "text": "x"}},
        },
    }
    assert kas_wire.is_recap_update(params)
    assert not kas_wire.is_recap_update({"update": {"sessionUpdate": "plan"}})
    assert not kas_wire.is_recap_update(
        {
            "update": {
                "sessionUpdate": "session_info_update",
                "_meta": {"kiro": {"kind": "context_usage"}},
            }
        }
    )
    assert not kas_wire.is_recap_update("not a dict")


@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
@pytest.mark.asyncio
async def test_load_session_queues_every_buffered_init_frame(monkeypatch, backend):
    rt, _, _ = _make_runtime()
    rt._acp_backend = backend
    rt._can_load_session = True
    frames = [
        JsonRpcMessage.from_dict(_kas_recap_wire("sid-load", "recap-shaped frame")),
        JsonRpcMessage(
            method=METHOD_SESSION_UPDATE,
            params={"sessionId": "sid-load", "update": {"sessionUpdate": "plan"}},
        ),
    ]

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_LOAD:
            rt._pending_init_notifications.extend(frames)
            return {"modes": {"currentModeId": "kirocrew"}, "models": []}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    with (
        patch.object(AcpSessionHandle, "drain_init", AsyncMock()),
        patch.object(AcpSessionHandle, "wait_mcp_ready", AsyncMock()),
    ):
        handle = await rt.load_session(
            "/home/u/.kiro/sessions/cli/sid-load.json",
            "sid-load",
            cwd="/w",
            agent="kirocrew",
        )

    queued = []
    while not handle._queue.empty():
        queued.append(handle._queue.get_nowait())
    assert queued == frames
    assert all(actual is expected for actual, expected in zip(queued, frames))
