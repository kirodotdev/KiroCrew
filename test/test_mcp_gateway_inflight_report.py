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
    HARD_WEDGE_CEILING_SECS,
    HEARTBEAT_TIMEOUT_SECS,
    PING_STALE_SECS,
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
        it makes; the short ceiling must sit above each of those bounds with the
        slow-request margin, or a reporting backend is recycled under a healthy
        call. The bounds are scanned from source, not listed by hand: every
        numeric ``timeout=`` passed to ``_post``/``_get`` and every
        ``*_TIMEOUT_SECS``/``*_TIMEOUT_S`` constant under ``mcp_tools/`` and in
        ``mcp_core.py``. The scan does not see a tool that blocks without a
        named bound (a bare sleep or an unbounded wait)."""
        import ast
        from pathlib import Path

        from kiro_crew.mcp_shared import DEFERRED_STEP_STUCK_SECS

        bounds = _synchronous_tool_bounds(ast, Path(backend_mod.__file__).parents[1])
        assert bounds, "the scan found no bounds; it is no longer reading the tool sources"
        too_long = {
            where: value
            for where, value in bounds.items()
            if value + HEARTBEAT_TIMEOUT_SECS > PROGRESS_WEDGE_CEILING_SECS
        }
        assert not too_long, (
            f"synchronous bounds leave no slow-request margin under the "
            f"{PROGRESS_WEDGE_CEILING_SECS:.0f}s ceiling: {too_long}"
        )
        assert DEFERRED_STEP_STUCK_SECS < PROGRESS_WEDGE_CEILING_SECS < HARD_WEDGE_CEILING_SECS


def _synchronous_tool_bounds(ast: Any, package: Any) -> dict[str, float]:
    """``{"file:line name": seconds}`` for each timeout bound named in the
    kirocrew-core tool sources (see the test above). Names resolve against
    each module's own top-level numeric assignments, then ``mcp_core``'s."""
    files = (
        sorted((package / "mcp_tools").rglob("*.py"))
        + sorted((package / "mcp_core_parts").glob("*.py"))
        + [package / "mcp_core.py"]
    )
    trees = {path: ast.parse(path.read_text(encoding="utf-8")) for path in files}

    def constants(tree: Any) -> dict[str, float]:
        found: dict[str, float] = {}
        for node in tree.body:
            target = value = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            if isinstance(target, ast.Name):
                try:
                    literal = ast.literal_eval(value)
                except ValueError:
                    continue
                if isinstance(literal, (int, float)) and not isinstance(literal, bool):
                    found[target.id] = float(literal)
        return found

    core = constants(trees[package / "mcp_core.py"])
    bounds: dict[str, float] = {}
    for path, tree in trees.items():
        local = {**core, **constants(tree)}
        rel = path.relative_to(package)
        for name, value in local.items():
            if name.endswith(("_TIMEOUT_SECS", "_TIMEOUT_S")) and name in constants(tree):
                bounds[f"{rel} {name}"] = value
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in ("_post", "_get"):
                for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
                    if arg.arg == "timeout" and default is not None:
                        bounds[f"{rel}:{node.lineno} {node.name} default"] = float(
                            ast.literal_eval(default)
                        )
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if callee not in ("_post", "_get"):
                continue
            for kw in node.keywords:
                if kw.arg != "timeout":
                    continue
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, (int, float)):
                    bounds[f"{rel}:{node.lineno} timeout"] = float(kw.value.value)
                elif isinstance(kw.value, ast.Name) and kw.value.id in local:
                    bounds[f"{rel}:{node.lineno} {kw.value.id}"] = local[kw.value.id]
    return bounds


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


class TestCancelledCallMarking:
    """A forwarded cancel marks the pending entry instead of dropping it. The
    backend answers a cancelled call with no response, so the wedge detector
    skips a marked entry; attribution and the last-stub recycle still see it."""

    @pytest.mark.asyncio
    async def test_forwarded_cancel_marks_the_entry_and_keeps_recycle_ownership(self):
        backend = _cancelled_backend()
        await backend.forward_from_stub("stub-A", _CANCEL_FRAME)
        assert backend._pending_requests["gw-9999-1"].cancelled
        sent = json.loads(backend.stdin.write.call_args[0][0])
        assert sent["params"]["requestId"] == "gw-9999-1"
        assert backend_mod.has_recyclable_in_flight(backend._pending_requests, "stub-A")
        assert not backend_mod.has_recyclable_in_flight(backend._pending_requests, "stub-B")

    @pytest.mark.asyncio
    async def test_another_stubs_cancel_does_not_mark_the_entry(self):
        backend = _cancelled_backend()
        await backend.forward_from_stub("stub-B", _CANCEL_FRAME)
        assert not backend._pending_requests["gw-9999-1"].cancelled

    @pytest.mark.parametrize("cancelled, verdict", [(True, "alive"), (False, "wedged")])
    @pytest.mark.asyncio
    async def test_a_cancelled_entry_does_not_reach_the_ceiling(self, cancelled, verdict):
        """Pings fresh: a cancelled call that outlives the ceiling is not a
        wedge, because the backend never answers it."""
        now = time.monotonic()
        pending = _pending(PROGRESS_WEDGE_CEILING_SECS + 1, now)
        pending.cancelled = cancelled
        backend = _make_backend(pending={"gw-9999-1": pending}, last_ping_response_mono=now - 5)
        backend.reports_inflight = True
        assert await backend._heartbeat_once(now=now) == verdict

    @pytest.mark.parametrize("reports_inflight", [True, False])
    @pytest.mark.asyncio
    async def test_a_cancelled_entry_still_counts_when_pings_have_stopped(self, reports_inflight):
        """Pings stale: a hung backend whose only call was cancelled is still
        recycled by the two-condition rule, as it is on any backend that does
        not report in-flight calls."""
        now = time.monotonic()
        pending = _pending(HEARTBEAT_TIMEOUT_SECS + 1, now)
        pending.cancelled = True
        backend = _make_backend(
            pending={"gw-9999-1": pending},
            last_ping_response_mono=now - PING_STALE_SECS - 1,
        )
        backend.reports_inflight = reports_inflight
        assert await backend._heartbeat_once(now=now) == "wedged"
        assert "ping stale" in backend._dead_reason

    @pytest.mark.asyncio
    async def test_late_progress_for_a_sole_holder_reaches_its_stub(self):
        backend = _cancelled_backend()
        await backend.forward_from_stub("stub-A", _CANCEL_FRAME)
        await backend._route_backend_line(_LATE_PROGRESS)
        delivered = json.loads(backend._test_inbox.get_nowait())
        assert delivered["params"]["progressToken"] == "tok-A"
        backend._record_hazard.assert_not_called()

    @pytest.mark.asyncio
    async def test_late_progress_for_a_shared_token_reaches_nobody(self):
        backend = _cancelled_backend()
        inbox_b: asyncio.Queue[bytes] = asyncio.Queue()
        backend._stub_inboxes["stub-B"] = inbox_b
        live = _pending(1, time.monotonic())
        live.stub_uuid = "stub-B"
        live.progress_token = "tok-A"
        backend._pending_requests["gw-9999-2"] = live
        await backend.forward_from_stub("stub-A", _CANCEL_FRAME)
        await backend._route_backend_line(_LATE_PROGRESS)
        assert backend._test_inbox.empty()
        assert inbox_b.empty(), "a cancelled co-tenant's progress reached stub-B"
        backend._record_hazard.assert_called_once_with(hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION)

    @pytest.mark.asyncio
    async def test_a_late_response_for_the_cancelled_call_settles_the_entry(self):
        backend = _cancelled_backend()
        await backend.forward_from_stub("stub-A", _CANCEL_FRAME)
        await backend._route_backend_line(b'{"jsonrpc": "2.0", "id": "gw-9999-1", "result": {}}\n')
        assert "gw-9999-1" not in backend._pending_requests
        assert not backend_mod.has_recyclable_in_flight(backend._pending_requests, "stub-A")
        assert backend.is_alive

    @pytest.mark.parametrize("method", ["resources/subscribe", "resources/unsubscribe"])
    @pytest.mark.asyncio
    async def test_a_cancelled_subscription_change_is_not_marked(self, method):
        """Its verdict still settles the shared lease tables."""
        backend = _cancelled_backend(progress_token=None)
        entry = backend._pending_requests["gw-9999-1"]
        entry.method = method
        entry.resource_uri = "file:///x"
        await backend.forward_from_stub("stub-A", _CANCEL_FRAME)
        assert backend._pending_requests["gw-9999-1"] is entry
        assert not entry.cancelled


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
