"""Bounds on what a ``spawn_sub_agents`` caller can hand the inline-collection seams.

Split from ``test_spawn_sub_agents_inline_collection`` under the file line cap:
every caller wait is finite and non-negative, and ``mark-collected`` acts only
on the caller's own parent, named by a string.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import test_spawn_sub_agents_inline_collection as _inline_tests
from test_handlers_messaging_coverage import _payload
from test_handlers_messaging_coverage import _Req as _CoverageReq
from test_handlers_messaging_coverage import _state
from test_spawn_sub_agents_inline_collection import (
    _UNUSABLE_WAITS,
    CRON_PARENT,
    _gateway,
    _post,
    _Req,
    _TimerLoop,
)

import kiro_crew.dashboard.handlers.messaging as handlers
import kiro_crew.subagent_inline_collection as sic
from kiro_crew.subagent_inline_collection import InlineCollections


class TestEveryCallerWaitIsFiniteAndNonNegative:
    """A NaN ``max_wait`` once gave a deadline that never expired and an expiry
    timer that fired at once and re-armed for ever. Each entry point refuses
    every non-finite or negative wait before anything is recorded."""

    @pytest.mark.parametrize(
        "value",
        [*_UNUSABLE_WAITS, "nan", "600", True, None, "soon", [], 10**400],
    )
    def test_the_check_refuses_every_unusable_wait(self, value: Any) -> None:
        assert sic.finite_nonnegative_number(value) is None

    @pytest.mark.parametrize("value, seconds", [(0, 0.0), (600, 600.0), (60.5, 60.5)])
    def test_the_check_keeps_every_usable_wait(self, value: Any, seconds: float) -> None:
        assert sic.finite_nonnegative_number(value) == seconds

    @pytest.mark.parametrize("value", _UNUSABLE_WAITS)
    def test_reserve_records_nothing_and_arms_no_timer(self, value: float) -> None:
        now = [0.0]
        reg = InlineCollections(clock=lambda: now[0])
        loop = _TimerLoop(now)
        with patch.object(sic.asyncio, "get_running_loop", return_value=loop):
            assert reg.reserve(CRON_PARENT, "a1", value, call="c1") is False
        assert reg._records.get(CRON_PARENT) is None
        assert loop.timers == []

    @pytest.mark.parametrize(
        "raw",
        [
            "NaN",
            "Infinity",
            "-Infinity",
            "-5",
            '"nan"',
            '"600"',
            '"60.5"',
            "true",
            "false",
            "null",
            '""',
            "[]",
            "{}",
        ],
        ids=[
            "nan",
            "+inf",
            "-inf",
            "negative",
            "nan-string",
            "numeric-string",
            "decimal-string",
            "true",
            "false",
            "null",
            "empty-string",
            "list",
            "object",
        ],
    )
    def test_api_spawn_refuses_the_wait_from_the_real_json_parse(self, raw: str) -> None:
        # The same parser the request uses: ``NaN`` here is a JSON literal.
        body = json.loads(
            '{"task": "x", "parent_session": "%s", "inline_collect": true,'
            ' "inline_call": "c1", "max_wait": %s}' % (CRON_PARENT, raw)
        )
        registry = InlineCollections()
        # Reached through the module: a ``Test*`` name imported here would be
        # collected and run a second time in this file.
        mgr = _inline_tests.TestApiSpawnReservesBeforeTheRunExists()._mgr(registry)
        with patch.object(
            handlers, "_spawn_request_memory_mode", AsyncMock(return_value="persistent")
        ):
            resp = asyncio.run(handlers.api_spawn(_CoverageReq(_state(subagents=mgr), body)))
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_max_wait"
        mgr.spawn.assert_not_called()
        assert registry._records == {}

    @pytest.mark.parametrize("value", ["nan", "inf", "-inf", "-5"])
    def test_the_tools_clamped_wait_is_always_one_the_route_accepts(self, value: str) -> None:
        """The env clamp keeps every setting inside ``[60, 7200]``, so the
        ``max_wait`` the tool sends is never one ``/api/spawn`` refuses."""
        import math

        from kiro_crew.mcp_tools import spawn as spawn_mod

        with (
            patch("kiro_crew.mcp_core.sel"),
            patch.dict(
                "os.environ",
                {"KIROCREW_SESSION_KEY": CRON_PARENT, "KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT": value},
            ),
        ):
            step = spawn_mod.spawn_sub_agents("spawn_sub_agents", {"agents": [{"prompt": "x"}]})
        assert math.isfinite(step.max_wait) and 60.0 <= step.max_wait <= 7200.0

    def test_a_non_finite_deadline_never_spins_the_expiry_timer(self) -> None:
        """Defence in depth: a deadline that slipped past reserve arms no timer
        sooner than a whole sweep, and its re-arm is a whole sweep too, so it
        can never fire and re-arm at once. The loop's ``call_later`` is
        recorded, never run on the wall clock."""
        now = [0.0]
        reg = InlineCollections(clock=lambda: now[0])
        reg._records[CRON_PARENT] = {
            "a1": sic._Record("a1", CRON_PARENT, sic.COLLECTING, float("nan"))
        }
        loop = _TimerLoop(now)
        sweep = sic._EXPIRY_SWEEP_SECS + 1.0
        with patch.object(sic.asyncio, "get_running_loop", return_value=loop):
            reg._schedule_expiry(CRON_PARENT, float("nan"))
            armed = [due - now[0] for due, *_rest in loop.timers]
            assert armed == [sweep]
            for _ in range(3):  # each firing re-arms the NaN deadline one sweep out
                assert loop.run_next()
                assert [due - now[0] for due, *_rest in loop.timers] == [sweep]


class TestMarkCollectedIsScopedToTheCallersOwnParent:
    """mark-collected settles and releases whole calls, so the scope check that
    bounds it is pinned: an internal caller may only act on the parent its own
    session is. Replacing the claim with ``claimed_session=None`` must turn
    this red."""

    OWNER = "cron:B"
    OTHER = "cron:A"

    @staticmethod
    def _scope(request: Any) -> Any:
        from kiro_crew.dashboard.handlers._shared import MemberScope

        session = request.headers.get("X-Session-Key", "")
        return MemberScope(session or None, True, None)

    async def _post(self, header: str, parent: str) -> tuple[Any, InlineCollections]:
        import kiro_crew.dashboard.handlers._shared as shared

        registry = InlineCollections()
        assert registry.reserve(self.OWNER, "b1", 600, call="cb")
        orch, _on_done, mgr = _gateway(registry)
        state = orch.dashboard_state
        state.subagents = mgr
        body = {"ids": ["b1"], "parent_session": parent, "call": "cb"}
        req = _Req(state, body)
        req._extra["internal_auth"] = True
        if header:
            req.headers["X-Session-Key"] = header
        with (
            patch.object(shared, "member_request_scope", AsyncMock(side_effect=self._scope)),
            patch.object(shared, "_audit_private_memory_denial", AsyncMock()),
        ):
            resp = await handlers.api_spawn_mark_collected(req)
        return resp, registry

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("header", "parent"),
        [(OTHER, OWNER), (OTHER, ""), ("", OWNER)],
        ids=["other-session-names-owner", "other-session-no-parent", "no-session-names-owner"],
    )
    async def test_another_parents_record_is_refused_and_untouched(
        self, header: str, parent: str
    ) -> None:
        resp, registry = await self._post(header, parent)
        assert resp.status == 409
        assert json.loads(resp.body)["code"] == "member_identity_unavailable"
        rec = registry._records[self.OWNER]["b1"]
        assert rec.state == "collecting" and not rec.held

    @pytest.mark.asyncio
    async def test_the_owning_session_is_admitted(self) -> None:
        resp, _registry = await self._post(self.OWNER, self.OWNER)
        assert resp.status == 200


class TestMarkCollectedRefusesANonStringParent:
    """As ``api_spawn`` does: ``{"a": 1}`` or ``5`` is a 400, never coerced
    with ``str()`` into a key, and no record is touched."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("parent", [{"a": 1}, 5, None, ["cron:B"], True])
    async def test_a_non_string_parent_session_is_400(self, parent: Any) -> None:
        registry = InlineCollections()
        assert registry.reserve(CRON_PARENT, "a1", 600, call="c1")
        orch, _on_done, mgr = _gateway(registry)
        body = {"ids": ["a1"], "parent_session": parent, "call": "c1"}
        reply = await _post(orch, mgr, body)
        assert reply["code"] == "invalid_parent_session"
        assert registry._records[CRON_PARENT]["a1"].state == "collecting"
