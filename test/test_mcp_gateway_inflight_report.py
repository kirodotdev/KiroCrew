"""The gateway half of the in-flight report (``mcp_caller.INFLIGHT_NOTIFICATION``).

A pooled backend parks its long calls as deferred tools and reports the ids it
is still making progress on. The gateway asks for that report in ``initialize``,
consumes it off the backend's stdout (never routing it to a stub), ages a
reported request from its last report, and holds a reporting backend to the
short ``PROGRESS_WEDGE_CEILING_SECS`` instead of the 3-hour ceiling -- which is
what lets a genuinely hung call recycle in minutes.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from test_mcp_gateway_wedge_ping_gate import _make_backend

from kiro_crew.mcp_caller import INFLIGHT_CAPABILITY_KEY, INFLIGHT_NOTIFICATION
from kiro_crew.mcp_gateway import backend as backend_mod
from kiro_crew.mcp_gateway import hazards
from kiro_crew.mcp_gateway.backend import (
    CANCELLED_REQUEST_TOMBSTONE_SECS,
    HARD_WEDGE_CEILING_SECS,
    HEARTBEAT_TIMEOUT_SECS,
    PROGRESS_WEDGE_CEILING_SECS,
    _inject_inflight_capability,
    _PendingRequest,
    _reports_inflight,
)


def _pending(age_secs: float, now: float) -> _PendingRequest:
    return _PendingRequest(
        stub_uuid="stub-A",
        original_id=1,
        method="tools/call",
        t_start_ms=(now - age_secs) * 1000.0,
    )


class TestCapabilityHandshake:
    def test_forwarded_initialize_asks_for_the_report(self):
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"capabilities": {"roots": {}}, "clientInfo": {"name": "kiro-cli"}},
        }
        out = _inject_inflight_capability(msg)
        assert out["params"]["capabilities"]["experimental"] == {INFLIGHT_CAPABILITY_KEY: {}}
        assert out["params"]["capabilities"]["roots"] == {}
        # Copy discipline: the stub's frame is never aliased.
        assert "experimental" not in msg["params"]["capabilities"]

    def test_existing_experimental_entries_are_kept(self):
        msg = {"params": {"capabilities": {"experimental": {"other": {"x": 1}}}}}
        out = _inject_inflight_capability(msg)
        assert out["params"]["capabilities"]["experimental"] == {
            "other": {"x": 1},
            INFLIGHT_CAPABILITY_KEY: {},
        }

    def test_non_initialize_shapes_pass_through(self):
        assert _inject_inflight_capability({"params": "nope"}) == {"params": "nope"}
        assert _inject_inflight_capability({}) == {}

    def test_reports_inflight_reads_the_advertisement(self):
        assert _reports_inflight({"capabilities": {"experimental": {INFLIGHT_CAPABILITY_KEY: {}}}})
        assert not _reports_inflight({"capabilities": {"experimental": {}}})
        assert not _reports_inflight({"capabilities": {}})
        assert not _reports_inflight({})


class TestReportConsumption:
    @pytest.mark.asyncio
    async def test_report_refreshes_progress_and_is_not_routed_or_recorded(self):
        now = time.monotonic()
        backend = _make_backend(
            pending={"gw-9999-1": _pending(600, now), "gw-9999-2": _pending(600, now)}
        )
        backend._record_hazard = MagicMock()  # type: ignore[method-assign]
        line = (
            '{"jsonrpc": "2.0", "method": "%s", "params": {"requestIds": ["gw-9999-1", "gw-unknown"]}}\n'
            % INFLIGHT_NOTIFICATION
        ).encode()
        await backend._route_backend_line(line)
        refreshed = backend._pending_requests["gw-9999-1"].t_progress_ms
        assert refreshed >= now * 1000.0
        assert backend._pending_requests["gw-9999-2"].t_progress_ms == 0.0
        backend._record_hazard.assert_not_called()
        assert backend._test_inbox.empty(), "a gateway-internal frame reached a stub"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_malformed_report_refreshes_nothing(self):
        now = time.monotonic()
        backend = _make_backend(pending={"gw-9999-1": _pending(600, now)})
        for params in ('{"requestIds": "gw-9999-1"}', "{}", "[]", "null"):
            line = (
                '{"jsonrpc": "2.0", "method": "%s", "params": %s}\n'
                % (INFLIGHT_NOTIFICATION, params)
            ).encode()
            await backend._route_backend_line(line)
        assert backend._pending_requests["gw-9999-1"].t_progress_ms == 0.0


class TestProgressAwareWedgeDetection:
    @pytest.mark.asyncio
    async def test_a_reported_request_is_aged_from_its_report(self):
        """Started an hour ago, reported 10s ago: not slow, not wedged."""
        now = time.monotonic()
        pending = _pending(3600, now)
        pending.t_progress_ms = (now - 10) * 1000.0
        backend = _make_backend(pending={"gw-9999-1": pending}, last_ping_response_mono=now - 30)
        backend.reports_inflight = True
        assert await backend._heartbeat_once(now=now) == "alive"
        assert "gw-9999-1" not in backend._warned_slow_ids

    @pytest.mark.asyncio
    async def test_reporting_backend_recycles_at_the_short_ceiling(self):
        """On a reporting backend an unrefreshed age past PROGRESS_WEDGE_CEILING_SECS
        is a call that stopped progressing: recycled even with fresh pings."""
        now = time.monotonic()
        backend = _make_backend(
            pending={"gw-9999-1": _pending(PROGRESS_WEDGE_CEILING_SECS + 1, now)},
            last_ping_response_mono=now - 5,
        )
        backend.reports_inflight = True
        assert await backend._heartbeat_once(now=now) == "wedged"
        assert backend._dead_reason is not None
        assert f"{PROGRESS_WEDGE_CEILING_SECS:.0f}s hard ceiling" in backend._dead_reason

    @pytest.mark.asyncio
    async def test_legacy_backend_keeps_the_long_ceiling(self):
        """A backend that never advertised the report (an older install, the
        Windows loop) cannot vouch for its calls, so the 3h ceiling stays."""
        now = time.monotonic()
        backend = _make_backend(
            pending={"gw-9999-1": _pending(PROGRESS_WEDGE_CEILING_SECS + 1, now)},
            last_ping_response_mono=now - 5,
        )
        assert backend.reports_inflight is False
        assert await backend._heartbeat_once(now=now) == "alive"
        assert "gw-9999-1" in backend._warned_slow_ids

    @pytest.mark.asyncio
    async def test_a_stale_report_does_not_outlive_the_short_ceiling(self):
        """Reported once, long ago: the refreshed age still crosses the ceiling."""
        now = time.monotonic()
        pending = _pending(7200, now)
        pending.t_progress_ms = (now - PROGRESS_WEDGE_CEILING_SECS - 1) * 1000.0
        backend = _make_backend(pending={"gw-9999-1": pending}, last_ping_response_mono=now - 5)
        backend.reports_inflight = True
        assert await backend._heartbeat_once(now=now) == "wedged"

    @pytest.mark.asyncio
    async def test_an_unreported_request_is_aged_from_its_start_on_a_small_clock(self):
        """``time.monotonic()`` can read small (Windows counts from boot), so a
        synthetic or early start stamp can be BELOW the unset 0.0 report stamp.
        An unreported request is aged from its start regardless, so the
        ceiling still fires."""
        now = 200.0  # a clock that has run for 200s
        pending = _pending(HARD_WEDGE_CEILING_SECS + 100, now)  # negative t_start_ms
        assert pending.t_start_ms < 0 and pending.t_progress_ms == 0.0
        backend = _make_backend(pending={"gw-9999-1": pending}, last_ping_response_mono=now - 5)
        assert await backend._heartbeat_once(now=now) == "wedged"

    @pytest.mark.parametrize(
        "running_for, verdict",
        [(170.0, "alive"), (PROGRESS_WEDGE_CEILING_SECS + 1, "wedged")],
        ids=["running-within-its-bound", "running-past-the-ceiling"],
    )
    @pytest.mark.asyncio
    async def test_calls_queued_behind_the_worker_do_not_age_the_backend_past_the_ceiling(
        self, running_for, verdict
    ):
        """Five slow synchronous calls forwarded together, all older than the
        short ceiling. The backend names the four still QUEUED behind its worker
        in every report; the one RUNNING was last named while it was queued, so
        its age is how long it has run. Healthy, that is within its own bound and
        the backend is kept; a call running past the ceiling recycles it."""
        now = time.monotonic()
        ids = [f"gw-9999-{n}" for n in range(1, 6)]
        pending = {fid: _pending(PROGRESS_WEDGE_CEILING_SECS + 100, now) for fid in ids}
        pending[ids[0]].t_progress_ms = (now - running_for) * 1000.0
        backend = _make_backend(pending=pending, last_ping_response_mono=now - 5)
        backend.reports_inflight = True
        report = '{"jsonrpc": "2.0", "method": "%s", "params": {"requestIds": %s}}\n' % (
            INFLIGHT_NOTIFICATION,
            json.dumps(ids[1:]),
        )
        await backend._route_backend_line(report.encode())
        assert await backend._heartbeat_once(now=time.monotonic()) == verdict

    def test_ceiling_relationships(self):
        """Above the slow-request threshold (it is a backstop, not the detector)
        and well under the legacy ceiling."""
        assert HEARTBEAT_TIMEOUT_SECS < PROGRESS_WEDGE_CEILING_SECS < HARD_WEDGE_CEILING_SECS

    def test_one_spawn_submission_finishes_inside_both_progress_bounds(self):
        """spawn_sub_agents and spawn_run submit at most one /api/spawn POST
        per step; a refused member may also need one bounded lost reconcile."""
        import ast
        import inspect

        from kiro_crew import mcp_core
        from kiro_crew.mcp_shared import DEFERRED_STEP_STUCK_SECS

        # The host-isolation floor replaces _post with a network-free double.
        source = ast.parse(inspect.getsource(mcp_core))
        post = next(
            node
            for node in source.body
            if isinstance(node, ast.FunctionDef) and node.name == "_post"
        )
        defaults = dict(zip((arg.arg for arg in post.args.kwonlyargs), post.args.kw_defaults))
        post_timeout = ast.literal_eval(defaults["timeout"])
        assert post_timeout < DEFERRED_STEP_STUCK_SECS < PROGRESS_WEDGE_CEILING_SECS

    def test_ceiling_outlives_every_synchronous_tool_bound(self):
        """A kirocrew-core tool that still runs on the worker bounds every call
        it makes; the short ceiling must sit above the LONGEST of those with the
        slow-request margin, or a reporting backend is recycled under a healthy
        call. Read from the owning constants, not restated: raising one of them
        past the ceiling is exactly what should fail here."""
        from kiro_crew.mcp_tools import apps, browser, knowledge
        from kiro_crew.validation import ASK_WAIT_SLICE_SECS

        synchronous_bounds = {
            "pod_up": apps._POD_UP_TIMEOUT_S,
            "pod_down": apps._POD_DOWN_TIMEOUT_S,
            "pod read": apps._POD_READ_TIMEOUT_S,
            "browser command": browser.COMMAND_TIMEOUT_SECS,
            "knowledge_add_document": knowledge._ADD_DOCUMENT_TIMEOUT_SECS,
            "ask slice (one deferred step)": ASK_WAIT_SLICE_SECS + 15,
        }
        longest = max(synchronous_bounds, key=synchronous_bounds.get)  # type: ignore[arg-type]
        assert (
            PROGRESS_WEDGE_CEILING_SECS >= synchronous_bounds[longest] + HEARTBEAT_TIMEOUT_SECS
        ), (
            f"{longest} runs for {synchronous_bounds[longest]}s synchronously; the ceiling "
            f"must leave it the slow-request margin"
        )


class TestCancelledRequestLeavesThePendingTable:
    """The backend answers a cancelled call with no response, so a cancel that
    kept the slot would let it age past the short ceiling and recycle the
    shared backend under every co-tenant."""

    @pytest.mark.asyncio
    async def test_forwarded_cancel_drops_the_slot_and_does_not_age_the_backend(self):
        now = time.monotonic()
        backend = _make_backend(pending={"gw-9999-1": _pending(10, now)})
        backend.reports_inflight = True
        await backend.forward_from_stub(
            "stub-A",
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
        )
        assert "gw-9999-1" not in backend._pending_requests
        sent = json.loads(backend.stdin.write.call_args[0][0])
        assert sent["params"]["requestId"] == "gw-9999-1"
        later = now + PROGRESS_WEDGE_CEILING_SECS + 1
        backend._last_ping_response_mono = later
        assert await backend._heartbeat_once(now=later) == "alive"

    @pytest.mark.asyncio
    async def test_a_late_response_for_the_cancelled_call_is_dropped(self):
        now = time.monotonic()
        backend = _make_backend(pending={"gw-9999-1": _pending(10, now)})
        await backend.forward_from_stub(
            "stub-A",
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
        )
        await backend._route_backend_line(b'{"jsonrpc": "2.0", "id": "gw-9999-1", "result": {}}\n')
        assert backend._test_inbox.empty(), "a cancelled call's late response reached the stub"  # type: ignore[attr-defined]
        assert backend.is_alive

    @pytest.mark.asyncio
    async def test_another_stubs_cancel_leaves_the_slot(self):
        now = time.monotonic()
        backend = _make_backend(pending={"gw-9999-1": _pending(10, now)})
        await backend.forward_from_stub(
            "stub-B",
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
        )
        assert "gw-9999-1" in backend._pending_requests


def _cancelled_backend(progress_token: object = "tok-A") -> Any:
    """A backend whose stub-A call ``gw-9999-1`` (carrying ``progress_token``)
    is about to be cancelled, with a mocked hazard recorder."""
    pending = _pending(10, time.monotonic())
    pending.progress_token = progress_token
    backend = _make_backend(pending={"gw-9999-1": pending})
    backend._record_hazard = MagicMock()
    return backend


_CANCEL_FRAME = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
_LATE_PROGRESS = b'{"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": "tok-A", "progress": 5}}\n'
_LATE_LOG = (
    b'{"jsonrpc": "2.0", "method": "notifications/message",'
    b' "params": {"level": "info", "data": "x", "_meta": {"relatedRequestId": "gw-9999-1"}}}\n'
)


class TestCancelledCallTombstone:
    """A server may still send progress or a log for a call its stub cancelled
    (frames already on the wire, or a server that ignores the cancel as MCP
    allows). Once the slot is gone that frame has no owner; it is dropped
    quietly for ``CANCELLED_REQUEST_TOMBSTONE_SECS`` instead of withdrawing a
    correct server's pooling recommendation."""

    @pytest.mark.asyncio
    async def test_late_progress_for_a_cancelled_call_is_dropped_without_a_hazard(self):
        backend = _cancelled_backend()
        await backend.forward_from_stub("stub-A", _CANCEL_FRAME)
        assert "gw-9999-1" not in backend._pending_requests
        await backend._route_backend_line(_LATE_PROGRESS)
        assert backend._test_inbox.empty(), "a cancelled call's progress reached the stub"
        backend._record_hazard.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_large_stub_id_retains_only_a_fixed_size_key(self, monkeypatch, manual_clock):
        """A stub names itself at registration, so its id is transport-sized:
        the tombstone keeps a fixed-size fingerprint of it, and attribution
        still treats the cancelled owner as a holder of the shared token."""
        manual_clock.install(monkeypatch, backend_mod)
        big_stub = "s" * (1024 * 1024)
        pending = _pending(10, time.monotonic())
        pending.stub_uuid = big_stub
        pending.progress_token = "tok-A"
        backend = _make_backend(pending={"gw-9999-1": pending})
        backend._record_hazard = MagicMock()
        inbox_b: asyncio.Queue[bytes] = asyncio.Queue()
        backend._stub_inboxes["stub-B"] = inbox_b
        live = _pending(1, time.monotonic())
        live.stub_uuid = "stub-B"
        live.progress_token = "tok-A"
        backend._pending_requests["gw-9999-2"] = live
        await backend.forward_from_stub(big_stub, _CANCEL_FRAME)
        tombstone = backend._cancelled_tombstones["gw-9999-1"]
        assert tombstone[1] != big_stub
        assert len(tombstone[1]) == 32 and all(c in "0123456789abcdef" for c in tombstone[1])
        assert len(repr(tombstone)) < 200
        # The cancelled owner still counts as a holder, so B does not inherit A's frame.
        await backend._route_backend_line(_LATE_PROGRESS)
        assert inbox_b.empty()
        backend._record_hazard.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_large_cancelled_token_retains_only_a_fixed_size_key(
        self, monkeypatch, manual_clock
    ):
        manual_clock.install(monkeypatch, backend_mod)
        token = "x" * (1024 * 1024)
        backend = _cancelled_backend(progress_token=token)
        await backend.forward_from_stub("stub-A", _CANCEL_FRAME)
        assert "gw-9999-1" not in backend._pending_requests
        tombstone = backend._cancelled_tombstones["gw-9999-1"]
        key = tombstone[0]
        assert isinstance(key, tuple) and len(key) == 2
        assert key[0] == "str"
        assert len(key[1]) == 32
        assert all(c in "0123456789abcdef" for c in key[1])
        assert len(repr(tombstone)) < 200
        late_progress = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {"progressToken": token, "progress": 5},
            }
        ).encode()
        assert backend._is_cancelled_call_notification(json.loads(late_progress))
        await backend._route_backend_line(late_progress)
        assert backend._test_inbox.empty()
        backend._record_hazard.assert_not_called()

    @pytest.mark.parametrize("cancelled_token, live_token", [(1, "1"), ("1", 1)])
    @pytest.mark.asyncio
    async def test_integer_and_string_tokens_remain_distinct_after_cancellation(
        self, monkeypatch, manual_clock, cancelled_token, live_token
    ):
        manual_clock.install(monkeypatch, backend_mod)
        backend = _cancelled_backend(progress_token=cancelled_token)
        await backend.forward_from_stub("stub-A", _CANCEL_FRAME)
        inbox_b: asyncio.Queue[bytes] = asyncio.Queue()
        backend._stub_inboxes["stub-B"] = inbox_b
        live = _pending(1, manual_clock.monotonic())
        live.stub_uuid = "stub-B"
        live.progress_token = live_token
        backend._pending_requests["gw-9999-2"] = live
        progress = {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {"progressToken": live_token, "progress": 5},
        }
        assert not backend._is_cancelled_call_notification(progress)
        await backend._route_backend_line(json.dumps(progress).encode())
        delivered = json.loads(inbox_b.get_nowait())
        assert delivered["params"]["progressToken"] == live_token
        assert type(delivered["params"]["progressToken"]) is type(live_token)
        progress["params"]["progressToken"] = cancelled_token
        assert backend._is_cancelled_call_notification(progress)
        await backend._route_backend_line(json.dumps(progress).encode())
        assert inbox_b.empty()
        assert backend._test_inbox.empty()
        backend._record_hazard.assert_not_called()

    @pytest.mark.asyncio
    async def test_late_log_naming_a_cancelled_call_is_dropped_without_a_hazard(self):
        backend = _cancelled_backend(progress_token=None)
        await backend.forward_from_stub("stub-A", _CANCEL_FRAME)
        await backend._route_backend_line(_LATE_LOG)
        assert backend._test_inbox.empty(), "a cancelled call's log reached the stub"
        backend._record_hazard.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_tombstone_expires_and_the_hazard_returns(self, monkeypatch, manual_clock):
        # The clock is installed on the backend module's own ``time`` binding
        # (D2), so the event loop and pytest-timeout keep real time.
        manual_clock.install(monkeypatch, backend_mod)
        backend = _cancelled_backend()
        await backend.forward_from_stub("stub-A", _CANCEL_FRAME)
        manual_clock.advance(CANCELLED_REQUEST_TOMBSTONE_SECS + 1)
        await backend._route_backend_line(_LATE_PROGRESS)
        backend._record_hazard.assert_called_once_with(hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION)
        assert not backend._cancelled_tombstones

    @pytest.mark.parametrize("token", [1, "tok-A"])
    @pytest.mark.asyncio
    async def test_a_late_frame_for_a_cancelled_token_another_stub_also_holds_reaches_nobody(
        self, monkeypatch, manual_clock, token
    ):
        manual_clock.install(monkeypatch, backend_mod)
        backend = _cancelled_backend(progress_token=token)
        inbox_b: asyncio.Queue[bytes] = asyncio.Queue()
        backend._stub_inboxes["stub-B"] = inbox_b
        live = _pending(1, manual_clock.monotonic())
        live.stub_uuid = "stub-B"
        live.progress_token = token
        backend._pending_requests["gw-9999-2"] = live
        await backend.forward_from_stub("stub-A", _CANCEL_FRAME)
        assert "gw-9999-1" not in backend._pending_requests
        late_progress = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {"progressToken": token, "progress": 5},
            }
        ).encode()
        await backend._route_backend_line(late_progress)
        assert backend._test_inbox.empty()
        assert inbox_b.empty(), "a cancelled co-tenant's progress reached stub-B"
        backend._record_hazard.assert_not_called()
        manual_clock.advance(CANCELLED_REQUEST_TOMBSTONE_SECS - 1)
        await backend._route_backend_line(late_progress)
        assert backend._test_inbox.empty()
        assert inbox_b.empty()
        backend._record_hazard.assert_not_called()
        manual_clock.advance(1)
        await backend._route_backend_line(late_progress)
        delivered = json.loads(inbox_b.get_nowait())
        assert delivered["params"]["progressToken"] == token
        assert inbox_b.empty()
        assert backend._test_inbox.empty()
        assert not backend._cancelled_tombstones
        backend._record_hazard.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_tombstone_for_a_different_token_leaves_live_progress_attributable(self):
        backend = _cancelled_backend(progress_token="tok-cancelled")
        await backend.forward_from_stub("stub-A", _CANCEL_FRAME)
        inbox_b: asyncio.Queue[bytes] = asyncio.Queue()
        backend._stub_inboxes["stub-B"] = inbox_b
        live = _pending(1, time.monotonic())
        live.stub_uuid = "stub-B"
        live.progress_token = "tok-A"
        backend._pending_requests["gw-9999-2"] = live
        await backend._route_backend_line(_LATE_PROGRESS)
        delivered = json.loads(inbox_b.get_nowait())
        assert delivered["params"]["progressToken"] == "tok-A"
        assert backend._test_inbox.empty()
        backend._record_hazard.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_same_stub_reusing_its_cancelled_token_still_owns_progress(self):
        backend = _cancelled_backend()
        await backend.forward_from_stub("stub-A", _CANCEL_FRAME)
        live = _pending(1, time.monotonic())
        live.progress_token = "tok-A"
        backend._pending_requests["gw-9999-2"] = live
        await backend._route_backend_line(_LATE_PROGRESS)
        delivered = json.loads(backend._test_inbox.get_nowait())
        assert delivered["params"]["progressToken"] == "tok-A"
        backend._record_hazard.assert_not_called()

    @pytest.mark.asyncio
    async def test_two_live_stubs_sharing_a_token_still_record_a_hazard(self):
        backend = _cancelled_backend()
        inbox_b: asyncio.Queue[bytes] = asyncio.Queue()
        backend._stub_inboxes["stub-B"] = inbox_b
        live = _pending(1, time.monotonic())
        live.stub_uuid = "stub-B"
        live.progress_token = "tok-A"
        backend._pending_requests["gw-9999-2"] = live
        assert not backend._cancelled_tombstones
        await backend._route_backend_line(_LATE_PROGRESS)
        assert backend._test_inbox.empty()
        assert inbox_b.empty()
        backend._record_hazard.assert_called_once_with(hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION)

    def test_the_tombstone_table_is_bounded(self, monkeypatch):
        monkeypatch.setattr(backend_mod, "_CANCELLED_TOMBSTONES_MAX", 3)
        backend = _make_backend()
        for n in range(5):
            backend._remember_cancelled(f"gw-9999-{n}", None, "stub-A")
        assert list(backend._cancelled_tombstones) == ["gw-9999-2", "gw-9999-3", "gw-9999-4"]


@pytest.mark.asyncio
async def test_unknown_notification_still_records_a_hazard():
    """The inflight arm is a narrow carve-out: any other unattributable
    request-scoped notification is still dropped and recorded."""
    backend = _make_backend()
    backend._record_hazard = MagicMock()  # type: ignore[method-assign]
    await backend._route_backend_line(
        b'{"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": "zz"}}\n'
    )
    backend._record_hazard.assert_called_once_with(hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION)
    await asyncio.sleep(0)
