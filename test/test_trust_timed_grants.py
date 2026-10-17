"""Timed command-scoped trust grants on the chat approval endpoint.

A timed grant is an ordinary per-chat command or command-family grant with a
deadline. It lives on the chat that made it, is dropped at the first decision
after its deadline, and an unreadable lifetime refuses the decision outright.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import platform_compat
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.trust_patterns import (
    MAX_TIMED_GRANT_SECONDS,
    GrantDeadline,
    base_trust_patterns,
    exact_trust_pattern,
    grant_deadline,
    live_trusted_patterns,
    parse_grant_ttl,
    record_trust_grant,
    timed_grant_deadline,
)


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


@web.middleware
async def _test_auth_middleware(request, handler):
    if "app" not in request:
        request["app"] = ""
    if "user" not in request:
        request["user"] = "local-app"
    return await handler(request)


def _make_app(state: DashboardState) -> web.Application:
    from kiro_crew.dashboard.chat import api_chat_slot_approve

    app = web.Application(middlewares=[_test_auth_middleware])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    return app


def _pending(state: DashboardState, key: str, request_id: str, command: str, base: str):
    """Register a live shell approval on a new slot and return ``(slot, future)``."""
    slot = _ChatSlot(key=key)
    state._slots[key] = slot
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    slot._approval_futures[request_id] = fut
    meta = {
        "request_id": request_id,
        "full_command": command,
        "trust_command_key": command,
        "base_command": base,
        "trust_command_grantable": "1",
        "trust_base_grantable": "1",
        "trust_grantable": "1",
    }
    slot.messages.append(
        {"role": "permission", "content": f"Running: {command}", "cls": json.dumps(meta)}
    )
    return slot, fut


class TestGrantHelpers:
    @pytest.mark.parametrize("value", [1, 3600, MAX_TIMED_GRANT_SECONDS])
    def test_parse_grant_ttl_accepts_whole_seconds_in_range(self, value):
        assert parse_grant_ttl(value) == value

    @pytest.mark.parametrize(
        "value", [0, -5, MAX_TIMED_GRANT_SECONDS + 1, True, False, 1.5, "3600", None, [60]]
    )
    def test_parse_grant_ttl_refuses_anything_else(self, value):
        assert parse_grant_ttl(value) is None

    def test_timed_grant_lives_only_in_the_timed_store(self):
        patterns: set[str] = set()
        timed: dict[str, GrantDeadline] = {}
        record_trust_grant(patterns, timed, ["ls"], GrantDeadline(100.0, 100.0))
        assert patterns == set() and timed == {"ls": (100.0, 100.0)}
        record_trust_grant(patterns, timed, ["ls"], GrantDeadline(50.0, 50.0))
        assert timed == {"ls": (50.0, 50.0)}

    def test_chat_long_grant_outlasts_and_clears_a_timed_one(self):
        patterns: set[str] = set()
        timed = {"ls": GrantDeadline(100.0, 100.0)}
        record_trust_grant(patterns, timed, ["ls"], None)
        assert patterns == {"ls"} and timed == {}

    def test_grant_deadline_counts_on_both_clocks(self):
        assert grant_deadline(3600, wall=1000.0, mono=5.0) == GrantDeadline(4600.0, 3605.0)

    def test_live_patterns_keep_chat_long_and_live_timed_grants(self):
        patterns = {"chat-long"}
        timed = {
            "live": GrantDeadline(10.0, 10.0),
            "due": GrantDeadline(5.0, 5.0),
            "past": GrantDeadline(1.0, 1.0),
        }
        live = live_trusted_patterns(patterns, timed, wall=5.0, mono=5.0)
        assert live == {"chat-long", "live"}
        assert timed == {"live": (10.0, 10.0)}
        assert patterns == {"chat-long"}

    def test_wall_clock_expiry_covers_a_suspended_host(self):
        """The monotonic clock stops in suspend; the wall-clock deadline still ends it."""
        timed = {"ls": GrantDeadline(wall=100.0, mono=100.0)}
        assert live_trusted_patterns(set(), timed, wall=101.0, mono=50.0) == set()
        assert timed == {}

    def test_monotonic_expiry_covers_a_clock_set_backwards(self):
        timed = {"ls": GrantDeadline(wall=100.0, mono=100.0)}
        assert live_trusted_patterns(set(), timed, wall=10.0, mono=100.0) == set()
        assert timed == {}

    def test_expiry_never_touches_a_pattern_another_writer_holds(self):
        """A worker lease or chat-long grant of the same pattern keeps its own lifetime."""
        patterns = {"ls"}
        timed = {"ls": GrantDeadline(1.0, 1.0)}
        assert live_trusted_patterns(patterns, timed, wall=5.0, mono=5.0) == {"ls"}
        assert patterns == {"ls"} and timed == {}

    def test_no_live_timed_grant_returns_the_chat_long_set_itself(self):
        patterns = {"ls"}
        assert live_trusted_patterns(patterns, {}, wall=0.0, mono=0.0) is patterns

    def test_a_writer_withdrawing_its_pattern_leaves_a_timed_grant_live(self):
        patterns = {"ls"}
        timed = {"ls": GrantDeadline(100.0, 100.0)}
        patterns.difference_update({"ls"})  # e.g. a worker lease ending
        assert live_trusted_patterns(patterns, timed, wall=5.0, mono=5.0) == {"ls"}

    def test_each_chat_has_its_own_deadline_store(self):
        first, second = _ChatSlot(key="a"), _ChatSlot(key="b")
        first._trusted_pattern_deadlines["ls"] = GrantDeadline(1.0, 1.0)
        assert second._trusted_pattern_deadlines == {}


class TestTimedGrantDeadline:
    """The deadline an approval carries is that of the timed grants it rests on."""

    def test_chat_long_patterns_alone_give_no_deadline(self):
        timed = {"ls*": GrantDeadline(100.0, 100.0)}
        assert timed_grant_deadline("ls -la", {"ls*"}, timed) is None

    def test_a_timed_grant_gives_its_own_deadline(self):
        timed = {"ls*": GrantDeadline(100.0, 90.0)}
        assert timed_grant_deadline("ls -la", set(), timed) == GrantDeadline(100.0, 90.0)

    def test_only_the_parts_chat_long_patterns_leave_carry_a_deadline(self):
        timed = {"ls*": GrantDeadline(100.0, 100.0), "cat*": GrantDeadline(5.0, 5.0)}
        assert timed_grant_deadline("ls && cat x", {"cat*"}, timed) == GrantDeadline(100.0, 100.0)

    def test_parts_on_different_timed_grants_end_at_the_earliest_on_each_clock(self):
        timed = {"ls*": GrantDeadline(100.0, 20.0), "cat*": GrantDeadline(50.0, 80.0)}
        assert timed_grant_deadline("ls && cat x", set(), timed) == GrantDeadline(50.0, 20.0)

    def test_a_part_two_timed_grants_cover_takes_the_one_ending_last(self):
        timed = {"ls*": GrantDeadline(100.0, 100.0), "ls -la": GrantDeadline(10.0, 10.0)}
        assert timed_grant_deadline("ls -la", set(), timed) == GrantDeadline(100.0, 100.0)

    def test_a_write_redirect_only_a_timed_grant_spells_carries_its_deadline(self):
        """``echo *`` matches the call but cannot cover its redirect; the timed grant does."""
        command = "echo hi > out.txt"
        timed = {exact_trust_pattern(command): GrantDeadline(100.0, 90.0)}
        assert timed_grant_deadline(command, base_trust_patterns("echo"), timed) == (
            GrantDeadline(100.0, 90.0)
        )

    def test_a_write_redirect_a_chat_long_grant_spells_gives_no_deadline(self):
        command = "echo hi > out.txt"
        timed = {exact_trust_pattern(command): GrantDeadline(100.0, 90.0)}
        chat_long = {exact_trust_pattern(command)}
        assert timed_grant_deadline(command, chat_long, timed) is None

    def test_a_discard_redirect_is_covered_by_a_chat_long_base_grant(self):
        timed = {"echo*": GrantDeadline(100.0, 90.0)}
        assert timed_grant_deadline("echo hi 2>/dev/null", base_trust_patterns("echo"), timed) is (
            None
        )

    def test_a_quoted_gt_is_covered_by_a_chat_long_base_grant(self):
        """The same reading as ``redirect_outside_grant``: a quoted ``>`` is text."""
        timed = {"echo*": GrantDeadline(100.0, 90.0)}
        with patch.object(platform_compat, "IS_WINDOWS", False):
            assert timed_grant_deadline("echo 'a > b'", base_trust_patterns("echo"), timed) is None
            assert timed_grant_deadline("echo 'a' > b", base_trust_patterns("echo"), timed) == (
                GrantDeadline(float("-inf"), float("-inf"))
            )

    def test_a_cut_inside_double_quotes_does_not_hide_a_redirect(self):
        """The segment after a cut at ``\\|`` inside ``"..."`` still writes ``out``."""
        command = "echo \"x\\|ls '\" > out '\\'"
        patterns = base_trust_patterns("echo") | base_trust_patterns("ls")
        timed = {"echo*": GrantDeadline(100.0, 90.0)}
        with patch.object(platform_compat, "IS_WINDOWS", False):
            assert timed_grant_deadline(command, patterns, timed) == (
                GrantDeadline(float("-inf"), float("-inf"))
            )

    @pytest.mark.parametrize("command", ["ls && rm x", "$(ls)"])
    def test_a_command_no_grant_covers_gets_a_deadline_already_passed(self, command):
        deadline = timed_grant_deadline(command, set(), {"ls*": GrantDeadline(100.0, 100.0)})
        assert deadline is not None
        assert deadline.ended(wall=-1e300, mono=-1e300)

    def test_ended_on_either_clock(self):
        deadline = GrantDeadline(10.0, 10.0)
        assert not deadline.ended(wall=9.0, mono=9.0)
        assert deadline.ended(wall=10.0, mono=0.0)
        assert deadline.ended(wall=0.0, mono=10.0)


class TestTimedGrantEndpoint:
    @pytest.mark.asyncio
    async def test_timed_command_grant_records_a_deadline(self, tmp_path):
        state = _make_state(tmp_path)
        slot, fut = _pending(state, "slot-1", "req-1", "ls /tmp", "ls")
        before_mono, before_wall = time.monotonic(), time.time()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/slot-1/approve",
                json={
                    "action": "trust_command",
                    "request_id": "req-1",
                    "pattern": "ls /tmp",
                    "ttl_seconds": 3600,
                },
            )
            assert resp.status == 200

        stored = exact_trust_pattern("ls /tmp")
        assert slot._trusted_patterns == set()
        deadline = slot._trusted_pattern_deadlines[stored]
        assert before_mono + 3600 <= deadline.mono <= time.monotonic() + 3600
        assert before_wall + 3600 <= deadline.wall <= time.time() + 3600
        assert fut.result() == "approved"

    @pytest.mark.asyncio
    async def test_timed_base_grant_times_every_base_pattern(self, tmp_path):
        state = _make_state(tmp_path)
        slot, fut = _pending(state, "slot-1", "req-2", "git status", "git")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/slot-1/approve",
                json={
                    "action": "trust_base",
                    "request_id": "req-2",
                    "pattern": "git *",
                    "ttl_seconds": 60,
                },
            )
            assert resp.status == 200

        assert slot._trusted_patterns == set()
        assert set(slot._trusted_pattern_deadlines) == base_trust_patterns("git")
        assert fut.result() == "approved"

    @pytest.mark.asyncio
    async def test_grant_without_a_lifetime_lasts_for_the_chat(self, tmp_path):
        state = _make_state(tmp_path)
        slot, fut = _pending(state, "slot-1", "req-3", "ls /tmp", "ls")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/slot-1/approve",
                json={"action": "trust_command", "request_id": "req-3", "pattern": "ls /tmp"},
            )
            assert resp.status == 200

        assert slot._trusted_patterns == {exact_trust_pattern("ls /tmp")}
        assert slot._trusted_pattern_deadlines == {}
        assert fut.result() == "approved"

    @pytest.mark.asyncio
    async def test_timed_grant_stays_on_the_chat_that_made_it(self, tmp_path):
        state = _make_state(tmp_path)
        granting, _ = _pending(state, "slot-1", "req-4", "ls /tmp", "ls")
        other, other_fut = _pending(state, "slot-2", "req-5", "ls /tmp", "ls")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/slot-1/approve",
                json={
                    "action": "trust_command",
                    "request_id": "req-4",
                    "pattern": "ls /tmp",
                    "ttl_seconds": 3600,
                },
            )
            assert resp.status == 200

        assert set(granting._trusted_pattern_deadlines) == {exact_trust_pattern("ls /tmp")}
        assert other._trusted_patterns == set()
        assert other._trusted_pattern_deadlines == {}
        assert not other_fut.done()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ttl", [0, MAX_TIMED_GRANT_SECONDS + 1, True, "3600", 1.5, None])
    async def test_unreadable_lifetime_refuses_without_any_grant(self, tmp_path, ttl):
        state = _make_state(tmp_path)
        slot, fut = _pending(state, "slot-1", "req-6", "ls /tmp", "ls")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/slot-1/approve",
                json={
                    "action": "trust_command",
                    "request_id": "req-6",
                    "pattern": "ls /tmp",
                    "ttl_seconds": ttl,
                },
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "ttl_invalid"

        assert slot._trusted_patterns == set()
        assert slot._trusted_pattern_deadlines == {}
        assert not fut.done()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["trust", "trust_reads", "approved", "yolo"])
    async def test_lifetime_on_any_other_tier_is_refused(self, tmp_path, action):
        state = _make_state(tmp_path)
        slot, fut = _pending(state, "slot-1", "req-7", "ls /tmp", "ls")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/slot-1/approve",
                json={"action": action, "request_id": "req-7", "ttl_seconds": 3600},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "ttl_unsupported"

        assert slot._trust is False
        assert slot._trust_reads is False
        assert slot._trusted_patterns == set()
        assert not fut.done()
        state.sessions.set_approval_policy.assert_not_called()
