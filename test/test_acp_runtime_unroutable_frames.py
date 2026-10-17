"""Frames the ``AcpRuntime`` reader cannot route to a registered session.

The throttled drop accounting for unknown-session and malformed frames, and the
runtime's own answer to a permission request from a session it never registered,
including the routing it grants to native child sessions the backend announced.
"""

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import (
    _drain,
    _drain_audits,
    _feed,
    _last_written_frame,
    _make_runtime,
    _next,
    _register,
    _start_reader,
    _stop_reader,
)

from kiro_crew.acp.types import METHOD_KIRO_SESSION_UPDATE, METHOD_SESSION_UPDATE

# ── Unroutable-frame drop accounting (log-flood containment) ──
#
# The reader drops any frame it cannot route. Logging that per frame turned a
# multiplexed backend's post-teardown / transcript-replay stream into ~60
# lines/second sustained for hours, taking 33–59% of every 2MB gateway.log
# rotation and rolling incident evidence out of the retained window. These tests
# lock in the replacement: one throttled summary per (sessionId, method) carrying
# the count, with the DROP behaviour itself unchanged.


def _drop_records(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "unroutable frame(s)" in r.getMessage()]


@pytest.mark.asyncio
async def test_unknown_session_drops_aggregate_into_one_counted_record(caplog):
    """N drops of the same (sid, method) inside one window → ONE record, count N."""
    import logging

    import kiro_crew.acp.runtime as runtime_mod

    rt, reader, _ = _make_runtime()
    task = await _start_reader(rt)
    try:
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
            for _ in range(5):
                _feed(reader, {"method": "session/update", "params": {"sessionId": "ghost"}})
            await _drain(reader)
            # Still inside the first window: aggregated, nothing emitted yet —
            # this is the assertion that fails on the per-frame implementation.
            assert _drop_records(caplog) == []
            assert rt._dropped_frames == {("ghost", "session/update"): 5}

            # Age the window out, then one more drop triggers the flush.
            rt._dropped_frames_flushed_at -= runtime_mod._DROP_SUMMARY_INTERVAL_SECS + 1.0
            _feed(reader, {"method": "session/update", "params": {"sessionId": "ghost"}})
            await _drain(reader)

        records = _drop_records(caplog)
        assert len(records) == 1, records
        assert (
            "Dropped 6 unroutable frame(s) for session 'ghost' (method='session/update')"
            in records[0]
        )
        # The point of the change: SIX dropped frames produce ONE log record,
        # not six. Counts every record naming the session, whatever its wording.
        assert len([r for r in caplog.records if "ghost" in r.getMessage()]) == 1
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_two_unknown_sessions_are_counted_separately(caplog):
    """A global tally would hide that two distinct session UUIDs are flooding."""
    import logging

    rt, reader, _ = _make_runtime()
    task = await _start_reader(rt)
    try:
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
            for _ in range(3):
                _feed(reader, {"method": "session/update", "params": {"sessionId": "sid-aaa"}})
            for _ in range(2):
                _feed(reader, {"method": "session/update", "params": {"sessionId": "sid-bbb"}})
            await _drain(reader)
            # Residual flush on reader exit reports both keys.
            await _stop_reader(task)

        records = _drop_records(caplog)
        assert len(records) == 2, records
        joined = "\n".join(records)
        assert "Dropped 3 unroutable frame(s) for session 'sid-aaa'" in joined
        assert "Dropped 2 unroutable frame(s) for session 'sid-bbb'" in joined
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_counted_drop_is_still_dropped_not_delivered():
    """Logging change only: an unroutable frame reaches no queue, as before."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"method": "session/update", "params": {"sessionId": "ghost"}})
        await _drain(reader)
        # Not routed to the co-tenant, not broadcast — just counted.
        assert q["sA"].empty()
        assert rt._dropped_frames == {("ghost", "session/update"): 1}
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_no_session_broadcast_drops_are_counted(caplog):
    """With zero registered sessions every global frame drops — same shape."""
    import logging

    import kiro_crew.acp.runtime as runtime_mod

    rt, reader, _ = _make_runtime()
    task = await _start_reader(rt)
    try:
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
            for _ in range(4):
                _feed(reader, {"method": "mcp/status", "params": {}})
            await _drain(reader)
            assert rt._dropped_frames == {(runtime_mod._DROP_NO_SESSION, "mcp/status"): 4}
            await _stop_reader(task)

        records = _drop_records(caplog)
        assert len(records) == 1, records
        assert "Dropped 4 unroutable frame(s)" in records[0]
        assert "(method='mcp/status')" in records[0]
    finally:
        await _stop_reader(task)


def test_drop_counter_state_does_not_leak_between_intervals(caplog):
    """A flushed window starts empty — the next record counts only new drops."""
    import logging

    rt, _reader, _ = _make_runtime()

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
        rt._note_dropped_frame("sid-x", "session/update")
        rt._note_dropped_frame("sid-x", "session/update")
        rt._flush_dropped_frames()
        assert rt._dropped_frames == {}

        rt._note_dropped_frame("sid-x", "session/update")
        rt._flush_dropped_frames()
        assert rt._dropped_frames == {}

    records = _drop_records(caplog)
    assert len(records) == 2, records
    assert "Dropped 2 unroutable frame(s) for session 'sid-x'" in records[0]
    # Not 3 — the first window's count did not carry over.
    assert "Dropped 1 unroutable frame(s) for session 'sid-x'" in records[1]


def test_drop_counter_map_is_bounded(caplog):
    """A wide fan-out of distinct keys flushes early instead of growing."""
    import logging

    import kiro_crew.acp.runtime as runtime_mod

    rt, _reader, _ = _make_runtime()
    cap = runtime_mod._DROP_SUMMARY_MAX_KEYS

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
        for i in range(cap * 3):
            rt._note_dropped_frame(f"sid-{i}", "session/update")
            assert len(rt._dropped_frames) <= cap

    # Overflow forced flushes rather than an unbounded map.
    assert len(_drop_records(caplog)) >= cap


def test_drop_counter_truncates_backend_controlled_key_text():
    """A pathological sessionId/method cannot be retained at full length."""
    import kiro_crew.acp.runtime as runtime_mod

    rt, _reader, _ = _make_runtime()
    limit = runtime_mod._DROP_SUMMARY_KEY_MAX_CHARS

    rt._note_dropped_frame("s" * (limit * 10), "m" * (limit * 10))

    (session_id, method), count = next(iter(rt._dropped_frames.items()))
    assert count == 1
    assert len(session_id) == limit
    assert len(method) == limit


def test_drop_key_is_redacted_before_the_retention_cap(caplog):
    """A credential straddling the 80-char cut leaves no fragment in key or log.

    Redact-before-bound on the retained key: a slice taken FIRST would sever the
    token at the cap into a head no credential pattern matches, and the flush
    would then log that head verbatim.
    """
    import logging

    import kiro_crew.acp.runtime as runtime_mod

    rt, _reader, _ = _make_runtime()
    limit = runtime_mod._DROP_SUMMARY_KEY_MAX_CHARS
    token = "AKIA" + "STRADDLE0123456A"
    hostile = "s" * (limit - 9) + token + " tail"
    assert limit - 9 < limit < limit - 9 + len(token), "premise: the cap cuts the token"

    rt._note_dropped_frame(hostile, hostile)
    (session_id, method), count = next(iter(rt._dropped_frames.items()))
    assert count == 1
    for part in (session_id, method):
        assert "AKIA" not in part and len(part) <= limit

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
        rt._flush_dropped_frames()
    records = _drop_records(caplog)
    assert records, "the flush must still report the drop"
    assert all("AKIA" not in r for r in records)


def test_drop_key_over_the_redact_input_cap_keeps_only_its_length():
    """A multi-KB key half never reaches the redactor and retains no content."""
    import kiro_crew.acp._dispatch as acp_dispatch
    import kiro_crew.acp.runtime as runtime_mod

    rt, _reader, _ = _make_runtime()
    huge = "ghp_" + "A" * (acp_dispatch._REQUEST_ID_REDACT_INPUT_CAP + 40)

    rt._note_dropped_frame(huge, "session/update")

    (session_id, method), count = next(iter(rt._dropped_frames.items()))
    assert count == 1 and method == "session/update"
    assert session_id.startswith("<id too long: ") and "ghp_" not in session_id
    assert len(session_id) <= runtime_mod._DROP_SUMMARY_KEY_MAX_CHARS


def test_drop_counter_handles_missing_method():
    """A frame with no `method` is still counted, under a placeholder key."""
    rt, _reader, _ = _make_runtime()

    rt._note_dropped_frame("sid-x", None)

    assert rt._dropped_frames == {("sid-x", "?"): 1}


# The two key halves come straight from backend JSON, which is untrusted and
# type-unchecked (JsonRpcMessage.from_dict copies `method` / `params` verbatim).
# A wrong-typed value can raise TypeError inside _reader_loop — the SINGLE
# owner of this process's stdout — killing every multiplexed session over one
# malformed frame. These lock in that the frame is counted and the demux lives.


@pytest.mark.asyncio
async def test_numeric_method_is_counted_and_reader_survives():
    """`{"method": 123}` must not kill the shared reader (all sessions with it)."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        _feed(reader, {"method": 123, "params": {"sessionId": "ghost"}})
        await _drain(reader)

        # Counted under the placeholder, not crashed.
        assert rt._dropped_frames == {("ghost", "?"): 1}
        # The property the finding is about: the demux is still alive...
        assert rt._dead is False
        assert rt.is_alive() is True
        assert not task.done()
        # ...and still routing for every co-tenant session.
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_non_string_session_id_is_counted_and_reader_survives():
    """Same hazard on the sessionId half: `params.sessionId` is Any, not str."""
    rt, reader, _ = _make_runtime()
    q = _register(rt, "sA")
    task = await _start_reader(rt)
    try:
        # Truthy, unregistered, and not a str → reaches the drop counter.
        _feed(reader, {"method": "session/update", "params": {"sessionId": 12345}})
        await _drain(reader)

        assert rt._dropped_frames == {("?", "session/update"): 1}
        assert rt._dead is False
        assert rt.is_alive() is True
        assert not task.done()
        _feed(reader, {"method": "session/update", "params": {"sessionId": "sA"}})
        msg = await _next(q["sA"])
        assert msg.params["sessionId"] == "sA"
    finally:
        await _stop_reader(task)


def test_drop_counter_placeholder_appears_in_flushed_summary(caplog):
    """A coerced key half is reported as the placeholder, wording unchanged."""
    import logging

    rt, _reader, _ = _make_runtime()

    with caplog.at_level(logging.DEBUG, logger="kiro_crew.acp.runtime"):
        rt._note_dropped_frame(12345, 123)
        rt._flush_dropped_frames()

    records = _drop_records(caplog)
    assert len(records) == 1, records
    assert "Dropped 1 unroutable frame(s) for session '?' (method='?')" in records[0]


# ── Unroutable permission requests (backend-internal subagents) ──────────────
#
# A `session/request_permission` REQUEST for a sessionId this client never
# registered comes from a backend-internal subagent (e.g. kiro-cli's own
# `subagent` tool). Dropping it strands the backend's response oneshot and
# wedges the child's whole tool batch until process teardown. These tests pin
# the behaviour: the runtime answers the request itself, with the request's
# own reject option, and never counts it as a dropped frame.


@pytest.mark.asyncio
async def test_unroutable_permission_request_is_auto_rejected(caplog):
    """Unknown-session permission REQUEST → answered with its reject option."""
    import logging

    rt, reader, proc = _make_runtime()
    task = await _start_reader(rt)
    try:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.runtime"):
            _feed(
                reader,
                {
                    "jsonrpc": "2.0",
                    "id": 77,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": "ghost-child",
                        "toolCall": {"toolCallId": "tc-1", "title": "glob /home"},
                        "options": [
                            {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                            {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                        ],
                    },
                },
            )
            await _drain(reader)

        frame = _last_written_frame(proc)
        assert frame["id"] == 77
        assert frame["result"] == {"outcome": {"outcome": "selected", "optionId": "reject_once"}}
        # Answered, not dropped: the drop counter must stay empty so the
        # summary log cannot misattribute an answered request as a drop.
        assert rt._dropped_frames == {}
        warnings = [r.getMessage() for r in caplog.records if "ghost-child" in r.getMessage()]
        assert warnings and "auto-rejected" in warnings[0]
        assert "glob /home" in warnings[0]
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unroutable_permission_never_picks_an_allow_option():
    """A payload with ONLY allow options must answer `cancelled`, never allow."""
    rt, reader, proc = _make_runtime()
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 78,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "ghost-child",
                    "options": [
                        {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "allow_always", "name": "Always", "kind": "allow_always"},
                    ],
                },
            },
        )
        await _drain(reader)

        frame = _last_written_frame(proc)
        assert frame["id"] == 78
        assert frame["result"] == {"outcome": {"outcome": "cancelled"}}
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unroutable_permission_legacy_options_without_kind():
    """Legacy kiro options omit `kind`; only a well-known reject id matches."""
    rt, reader, proc = _make_runtime()
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 79,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "ghost-child",
                    "options": [
                        {"optionId": "allow_once", "name": "Allow"},
                        {"optionId": "reject_once", "name": "Reject"},
                    ],
                },
            },
        )
        await _drain(reader)

        frame = _last_written_frame(proc)
        assert frame["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_registered_session_permission_still_routes_to_queue():
    """The fix must not intercept permission requests for registered sessions."""
    rt, reader, proc = _make_runtime()
    queues = _register(rt, "known-session")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 80,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "known-session",
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            },
        )
        await _drain(reader)

        routed = queues["known-session"].get_nowait()
        assert routed.id == 80
        # The runtime did not answer on the session's behalf.
        proc.stdin.write.assert_not_called()
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unroutable_non_permission_request_still_drops():
    """Only permission requests get the auto-answer; other unknown-session
    frames keep the counted-drop behavior."""
    rt, reader, proc = _make_runtime()
    task = await _start_reader(rt)
    try:
        _feed(reader, {"method": "session/update", "params": {"sessionId": "ghost"}})
        await _drain(reader)
        assert rt._dropped_frames == {("ghost", "session/update"): 1}
        proc.stdin.write.assert_not_called()
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_subagent_list_update_snapshots_child_ids():
    """A broadcast list_update replaces the known-child set (full list each time)."""
    rt, reader, _ = _make_runtime()
    _register(rt, "parent-session")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {
                    "subagents": [
                        {"sessionId": "child-a", "sessionName": "correctness"},
                        {"sessionId": "child-b", "sessionName": "security"},
                    ],
                    "pendingStages": [],
                },
            },
        )
        await _drain(reader)
        assert rt._subagent_sessions == {"child-a", "child-b"}

        # Next update omits child-a (terminated) — the set is replaced, not grown.
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-b"}]},
            },
        )
        await _drain(reader)
        assert rt._subagent_sessions == {"child-b"}
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_known_subagent_permission_routes_to_slot_queue():
    """A child the backend announced gets the SAME policy pipeline as the main
    agent: its permission request lands on the slot's session queue instead of
    being auto-rejected."""
    rt, reader, proc = _make_runtime()
    queues = _register(rt, "parent-session")
    # An explicitly marked active turn — requests are only routed while the
    # owner's prompt dispatch loop is consuming the queue. (_routed_requests
    # is NOT a proxy for this: it also holds set_mode/steer/config ids.)
    rt.mark_turn_active("parent-session", True)
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 90,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "child-a",
                    "toolCall": {"toolCallId": "tc-9", "title": "glob /home"},
                    "options": [
                        {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                    ],
                },
            },
        )
        await _drain(reader)

        # list_update broadcast + the routed permission request both arrive.
        frames = []
        while not queues["parent-session"].empty():
            frames.append(queues["parent-session"].get_nowait())
        assert any(f.id == 90 for f in frames), frames
        # The runtime did NOT answer it — the slot's consumer owns the decision.
        proc.stdin.write.assert_not_called()
        assert rt._dropped_frames == {}
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unannounced_session_permission_still_auto_rejected():
    """A sessionId the backend never announced cannot ride the routing path —
    it keeps the fail-closed auto-reject."""
    rt, reader, proc = _make_runtime()
    _register(rt, "parent-session")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 91,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "never-announced",
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            },
        )
        await _drain(reader)

        frame = _last_written_frame(proc)
        assert frame["id"] == 91
        assert frame["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_ambiguous_multi_session_runtime_falls_back_to_reject():
    """With several registered sessions the child→consumer mapping is ambiguous
    — the frame names no owner — so the runtime fails closed instead of handing
    the approval to an arbitrary sibling's policy."""
    rt, reader, proc = _make_runtime()
    queues = _register(rt, "session-one", "session-two")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 92,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "child-a",
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            },
        )
        await _drain(reader)

        frame = _last_written_frame(proc)
        assert frame["id"] == 92
        assert frame["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}
        # Neither sibling consumer received the request frame.
        for q in queues.values():
            while not q.empty():
                assert q.get_nowait().id != 92
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_announced_child_session_update_routes_for_cache_population():
    """A child's session/update (tool_call) frame is routed to the slot queue
    so the consumer's caches capture the real command bytes for a later
    permission request — the payload full mode-parity depends on."""
    rt, reader, proc = _make_runtime()
    queues = _register(rt, "parent-session")
    # Updates route only while the owner has an in-flight prompt: between
    # turns the dispatch loop is not consuming and the next turn's start
    # clears the caches anyway, so the runtime counts them as drops.
    rt.mark_turn_active("parent-session", True)
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        _feed(
            reader,
            {
                "method": "session/update",
                "params": {
                    "sessionId": "child-a",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-child-1",
                        "title": "Running: sha256sum README.md",
                        "kind": "execute",
                        "rawInput": {"command": "sha256sum README.md"},
                    },
                },
            },
        )
        await _drain(reader)

        frames = []
        while not queues["parent-session"].empty():
            frames.append(queues["parent-session"].get_nowait())
        routed = [f for f in frames if f.method == "session/update"]
        assert routed, "child session/update must reach the slot queue"
        assert (routed[0].params or {}).get("sessionId") == "child-a"
        # Not answered by the runtime, not counted as a drop.
        proc.stdin.write.assert_not_called()
        assert rt._dropped_frames == {}
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unannounced_child_session_update_still_drops():
    """Updates for sessions the backend never announced keep the counted-drop
    path — routing is gated on the announce, same as permission requests."""
    rt, reader, proc = _make_runtime()
    q = _register(rt, "parent-session")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "session/update",
                "params": {
                    "sessionId": "never-announced",
                    "update": {"sessionUpdate": "tool_call"},
                },
            },
        )
        await _drain(reader)
        assert rt._dropped_frames == {("never-announced", "session/update"): 1}
        assert q["parent-session"].empty()
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_announced_child_kiro_session_update_routes_for_cache_population():
    """kiro-cli 2.21.x emits child updates under the extension method
    `_kiro.dev/session/update`. The announced-child single-owner branch must
    admit it under the exact same guard as plain `session/update`, or the
    consumer's caches never see the child's command bytes and identity, the
    permission gate reads the child as UNVERIFIED, and every auto-approve
    path is skipped."""
    rt, reader, proc = _make_runtime()
    queues = _register(rt, "parent-session")
    # Same in-flight-prompt gate as the plain spelling: between turns an
    # update is a counted drop (see the between-turns test below).
    rt.mark_turn_active("parent-session", True)
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        _feed(
            reader,
            {
                "method": "_kiro.dev/session/update",
                "params": {
                    "sessionId": "child-a",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-child-1",
                        "title": "@example-server/get-item",
                        "kind": "other",
                        "rawInput": {"itemId": "item-0001"},
                        "_meta": {
                            "kiro": {
                                "mcpServerName": "example-server",
                                "toolName": "get-item",
                            }
                        },
                    },
                },
            },
        )
        await _drain(reader)

        frames = []
        while not queues["parent-session"].empty():
            frames.append(queues["parent-session"].get_nowait())
        routed = [f for f in frames if f.method == METHOD_KIRO_SESSION_UPDATE]
        assert routed, "child _kiro.dev/session/update must reach the slot queue"
        assert (routed[0].params or {}).get("sessionId") == "child-a"
        # Not answered by the runtime, not counted as a drop.
        proc.stdin.write.assert_not_called()
        assert rt._dropped_frames == {}
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unannounced_child_kiro_session_update_still_drops():
    """The extension spelling is gated on the announce exactly like plain
    session/update: a session the backend never announced keeps the
    counted-drop path."""
    rt, reader, proc = _make_runtime()
    q = _register(rt, "parent-session")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/session/update",
                "params": {
                    "sessionId": "never-announced",
                    "update": {"sessionUpdate": "tool_call"},
                },
            },
        )
        await _drain(reader)
        assert rt._dropped_frames == {("never-announced", "_kiro.dev/session/update"): 1}
        assert q["parent-session"].empty()
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_between_turns_child_update_is_dropped_not_queued():
    """A child update (either spelling) arriving while the owner has NO
    in-flight prompt is a counted drop, never queued: the next turn's start
    clears the caches and discards stale non-permission frames, so queueing
    would only grow an unbounded queue in gateway memory while the slot
    idles."""
    rt, reader, proc = _make_runtime()
    queues = _register(rt, "parent-session")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        for method in ("session/update", "_kiro.dev/session/update"):
            _feed(
                reader,
                {
                    "method": method,
                    "params": {
                        "sessionId": "child-a",
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "tc-child-1",
                            "rawInput": {"itemId": "item-0001"},
                        },
                    },
                },
            )
        await _drain(reader)
        assert rt._dropped_frames == {
            ("child-a", "session/update"): 1,
            ("child-a", "_kiro.dev/session/update"): 1,
        }
        while not queues["parent-session"].empty():
            frame = queues["parent-session"].get_nowait()
            assert frame.method not in (METHOD_SESSION_UPDATE, METHOD_KIRO_SESSION_UPDATE)
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_multi_session_child_kiro_session_update_stays_dropped():
    """With several registered sessions the frame names no owner — the
    fail-closed multi-session path is unchanged for the extension spelling:
    the update is a counted drop, never guessed onto a queue."""
    rt, reader, proc = _make_runtime()
    queues = _register(rt, "session-1", "session-2")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        _feed(
            reader,
            {
                "method": "_kiro.dev/session/update",
                "params": {
                    "sessionId": "child-a",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-child-1",
                        "rawInput": {"itemId": "item-0001"},
                    },
                },
            },
        )
        await _drain(reader)
        assert rt._dropped_frames == {("child-a", "_kiro.dev/session/update"): 1}
        # The roster announce itself broadcasts to every queue; the child's
        # update must reach NONE of them.
        for sid in ("session-1", "session-2"):
            while not queues[sid].empty():
                frame = queues[sid].get_nowait()
                assert frame.method != METHOD_KIRO_SESSION_UPDATE
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_session_swap_on_warm_runtime_does_not_inherit_child_routing():
    """A new session registered after the announcing owner departs must NOT
    receive the stale child's permission requests — they fail closed."""
    rt, reader, proc = _make_runtime()
    _register(rt, "owner-session")
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "method": "_kiro.dev/subagent/list_update",
                "params": {"subagents": [{"sessionId": "child-a"}]},
            },
        )
        await _drain(reader)
        assert rt._subagent_owner == "owner-session"

        # Owner departs; a different session takes the warm runtime.
        rt.unregister_session("owner-session")
        assert rt._subagent_owner is None and rt._subagent_sessions == set()
        q2 = _register(rt, "successor-session")

        _feed(
            reader,
            {
                "jsonrpc": "2.0",
                "id": 95,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "child-a",
                    "options": [
                        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}
                    ],
                },
            },
        )
        await _drain(reader)

        frame = _last_written_frame(proc)
        assert frame["id"] == 95
        assert frame["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}
        assert q2["successor-session"].empty()
    finally:
        await _drain_audits(rt)
        await _stop_reader(task)
