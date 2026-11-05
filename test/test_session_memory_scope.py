"""The caller-scoped memory view behind ``resource_status``.

What these pin: an agent learns its OWN footprint as counts and sizes, and never
a session title, key, agent name, task text, run id or pid, nor any figure that
belongs to another session's scope.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import session_memory_scope as scope_mod
from kiro_crew.dashboard.handlers._shared import MemberScope
from kiro_crew.dashboard.handlers.session_memory_scope import (
    api_spawn_session_memory,
    caller_memory_view,
)
from kiro_crew.mcp_tools.spawn_parts import session_memory as tool_mod
from kiro_crew.session_allocation_parts.companions import subagent_runtime_row_key

ME = "dashboard:chat-mine"
OTHER = "dashboard:chat-theirs"

#: Strings that must never leave the route: titles, keys, agents, task text, pids.
SECRETS = (
    "Payroll migration plan",
    "Private chat title",
    "chat-theirs",
    "secret-agent",
    "summarize the board minutes",
    "run-theirs",
    "99991",
    "99992",
)


def _payload() -> dict[str, Any]:
    """A Sessions-panel payload holding the caller, a stranger and their runs."""
    return {
        "sessions": [
            {
                "key": ME,
                "title": "Payroll migration plan",
                "agent": "secret-agent",
                "pid": 99991,
                "rss_mb": 412.5,
                "procs": 7,
                "mcp": 3,
                "sharers": 1,
                "cpu_cores": 0.4,
            },
            {
                "key": OTHER,
                "title": "Private chat title",
                "agent": "secret-agent",
                "pid": 99992,
                "rss_mb": 5000.0,
                "procs": 40,
                "mcp": 12,
                "sharers": 1,
            },
            {"key": subagent_runtime_row_key(ME), "pid": 99993, "rss_mb": 300.0, "procs": 4},
            {"key": subagent_runtime_row_key(OTHER), "pid": 99994, "rss_mb": 7777.0},
        ],
        "tasks": [
            {
                "id": "run-mine-1",
                "task": "summarize the board minutes",
                "parent": ME,
                "rss_mb": 100.0,
                "procs": 2,
                "mcp": 1,
                "sampled": True,
                "pid": 99991,
            },
            {"id": "run-mine-2", "parent": ME, "rss_mb": 0.0, "sampled": False},
            {
                "id": "run-theirs",
                "parent": OTHER,
                "rss_mb": 9999.0,
                "procs": 50,
                "mcp": 9,
                "sampled": True,
            },
            {"id": "run-child", "parent": "subagent:run-mine-1", "rss_mb": 55.0, "sampled": True},
        ],
        "totals": {"rss_mb": 23000.0, "runtimes": 6, "rss_is_upper_bound": True},
        "history": [{"t": 1.0, "mb": 23000.0}],
    }


def _assert_no_secret(view: object) -> None:
    text = json.dumps(view)
    for secret in SECRETS:
        assert secret not in text, secret
    # No figure from the stranger's scope, and no host total.
    for foreign in ("5000", "7777", "9999", "23000"):
        assert foreign not in text, foreign


class TestCallerMemoryView:
    def test_a_session_sees_its_own_runtime_and_runs_and_nothing_else(self) -> None:
        view = caller_memory_view(_payload(), ME)
        assert view == {
            "self": {"rss_mb": 412.5, "procs": 7, "mcp": 3, "shared": False},
            "subagent_runtime": {"rss_mb": 300.0, "procs": 4, "mcp": None, "shared": False},
            "runs": {
                "count": 2,
                "unsampled": 1,
                "shared": 0,
                "rss_mb": 100.0,
                "procs": 2,
                "mcp": 1,
            },
            "rss_is_upper_bound": True,
        }
        _assert_no_secret(view)

    def test_no_name_field_survives_whatever_the_row_carries(self) -> None:
        view = caller_memory_view(_payload(), ME)
        allowed = {"rss_mb", "procs", "mcp", "shared"}
        assert set(view["self"]) == allowed  # type: ignore[arg-type]
        assert set(view["subagent_runtime"]) == allowed  # type: ignore[arg-type]
        assert set(view["runs"]) == {"count", "unsampled", "shared", "rss_mb", "procs", "mcp"}  # type: ignore[arg-type]

    def test_a_subagent_sees_its_own_run_and_its_children_only(self) -> None:
        view = caller_memory_view(_payload(), "subagent:run-mine-1")
        assert view["self"] == {"rss_mb": 100.0, "procs": 2, "mcp": 1, "shared": False}
        assert view["subagent_runtime"] is None
        assert view["runs"] == {
            "count": 1,
            "unsampled": 0,
            "shared": 0,
            "rss_mb": 55.0,
            # The child row sent a size but no counts: unmeasured, not 0.
            "procs": None,
            "mcp": None,
        }
        _assert_no_secret(view)
        # Its parent's own row is not its scope.
        assert "412" not in json.dumps(view)

    def test_an_unmeasured_own_run_reads_unmeasured_not_zero(self) -> None:
        view = caller_memory_view(_payload(), "subagent:run-mine-2")
        assert view["self"] == {"rss_mb": None, "procs": None, "mcp": None, "shared": False}
        text = "\n".join(tool_mod.render_session_memory(view))
        assert "This session: unmeasured" in text

    def test_a_continued_run_is_found_by_its_session_key(self) -> None:
        payload = _payload()
        payload["tasks"][0]["id"] = "run-continued"
        payload["tasks"][0]["session_key"] = "subagent:run-mine-1"
        view = caller_memory_view(payload, "subagent:run-mine-1")
        assert view["self"] == {"rss_mb": 100.0, "procs": 2, "mcp": 1, "shared": False}
        assert view["runs"]["count"] == 1  # type: ignore[index]

    def test_a_shared_runtime_gives_only_the_size_share(self) -> None:
        # Co-tenants' processes, MCP servers and their number are not the caller's.
        payload = _payload()
        payload["sessions"][0]["sharers"] = 3
        payload["tasks"][0]["shared"] = True
        view = caller_memory_view(payload, ME)
        assert view["self"] == {"rss_mb": 412.5, "procs": None, "mcp": None, "shared": True}
        assert view["runs"] == {
            "count": 2,
            "unsampled": 1,
            "shared": 1,
            "rss_mb": 100.0,
            # The only measured run is shared, so no run sent a count.
            "procs": None,
            "mcp": None,
        }
        assert "3" not in json.dumps(view["self"])
        text = "\n".join(tool_mod.render_session_memory(view))
        assert "This session: 412 MB (your share of a shared runtime)" in text
        assert "1 on a shared runtime: size only" in text

    def test_runs_none_of_which_was_measured_read_unmeasured_not_zero(self) -> None:
        payload = _payload()
        payload["tasks"][0]["sampled"] = False
        view = caller_memory_view(payload, ME)
        assert view["runs"] == {
            "count": 2,
            "unsampled": 2,
            "shared": 0,
            "rss_mb": None,
            "procs": None,
            "mcp": None,
        }
        text = "\n".join(tool_mod.render_session_memory(view))
        assert (
            "Runs you started: 2 live, unmeasured, ? processes, ? MCP servers "
            "(2 not measured yet)"
        ) in text
        runs_line = next(line for line in text.splitlines() if "Runs you started" in line)
        assert " 0 MB" not in runs_line
        assert " 0 processes" not in runs_line

    def test_runs_measured_only_on_shared_runtimes_send_no_counts(self) -> None:
        payload = _payload()
        payload["tasks"][0]["shared"] = True
        payload["tasks"][1].update({"sampled": True, "shared": True, "rss_mb": 20.0})
        view = caller_memory_view(payload, ME)
        assert view["runs"] == {
            "count": 2,
            "unsampled": 0,
            "shared": 2,
            "rss_mb": 120.0,
            "procs": None,
            "mcp": None,
        }
        text = "\n".join(tool_mod.render_session_memory(view))
        assert (
            "Runs you started: 2 live, 120 MB, ? processes, ? MCP servers "
            "(2 on a shared runtime: size only)"
        ) in text

    def test_a_caller_with_no_live_runtime_gets_no_borrowed_row(self) -> None:
        view = caller_memory_view(_payload(), "dashboard:chat-idle")
        assert view["self"] is None
        assert view["subagent_runtime"] is None
        assert view["runs"]["count"] == 0  # type: ignore[index]
        _assert_no_secret(view)

    def test_a_key_prefix_is_not_a_match(self) -> None:
        # ``dashboard:chat-mine`` must not read ``dashboard:chat-mine2``'s row.
        payload = _payload()
        payload["sessions"][0]["key"] = ME + "2"
        assert caller_memory_view(payload, ME)["self"] is None

    def test_non_numbers_are_dropped_not_echoed(self) -> None:
        payload = _payload()
        payload["sessions"][0]["rss_mb"] = "Payroll migration plan"
        payload["sessions"][0]["procs"] = True
        view = caller_memory_view(payload, ME)
        assert view["self"]["rss_mb"] is None  # type: ignore[index]
        assert view["self"]["procs"] is None  # type: ignore[index]
        _assert_no_secret(view)

    def test_an_empty_caller_is_refused(self) -> None:
        with pytest.raises(ValueError):
            caller_memory_view(_payload(), "")


def _request(*, internal: bool, scope: MemberScope | None = None) -> web.Request:
    app = web.Application()
    app["state"] = SimpleNamespace(sessions=object(), subagents=object())
    request = make_mocked_request(
        "GET", "/api/spawn/session-memory", headers={"X-Session-Key": ME}, app=app
    )
    request["internal_auth"] = internal
    return request


def _call(request: web.Request, scope: MemberScope, sample: AsyncMock) -> web.Response:
    with patch.object(scope_mod, "member_request_scope", AsyncMock(return_value=scope)):
        with patch.object(scope_mod._scope_sampler, "sample", sample):
            return asyncio.run(api_spawn_session_memory(request))


class TestRoute:
    def test_the_dashboard_owner_is_pointed_at_the_panel_route(self) -> None:
        sample = AsyncMock(return_value=_payload())
        resp = _call(_request(internal=False), MemberScope(None, False, None), sample)
        assert resp.status == 403
        sample.assert_not_called()

    @pytest.mark.parametrize(
        "scope",
        [MemberScope(ME, False, None), MemberScope(None, True, None), MemberScope("", True, "")],
    )
    def test_an_unverified_or_anonymous_caller_gets_nothing(self, scope: MemberScope) -> None:
        sample = AsyncMock(return_value=_payload())
        resp = _call(_request(internal=True), scope, sample)
        assert resp.status == 409
        sample.assert_not_called()
        _assert_no_secret(json.loads(resp.text))

    def test_a_verified_caller_gets_its_own_scope_without_titles(self) -> None:
        sample = AsyncMock(return_value=_payload())
        resp = _call(_request(internal=True), MemberScope(ME, True, ""), sample)
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["self"]["rss_mb"] == 412.5
        _assert_no_secret(body)
        # Titles are never even resolved: no slot lookup is handed to the sampler.
        assert "get_slot" not in sample.call_args.kwargs
        assert len(sample.call_args.args) == 2

    def test_the_attested_session_is_the_scope_not_the_header(self) -> None:
        # The header claims ME; the attested identity is the stranger. The
        # stranger's scope is served, never ME's.
        sample = AsyncMock(return_value=_payload())
        resp = _call(_request(internal=True), MemberScope(OTHER, True, ""), sample)
        body = json.loads(resp.text)
        assert body["self"]["rss_mb"] == 5000.0
        assert "412" not in resp.text

    def test_the_route_is_registered_before_the_run_id_route(self) -> None:
        from kiro_crew.dashboard.server import _register_mcp_routes

        app = web.Application()
        _register_mcp_routes(app)
        paths = [r.resource.canonical for r in app.router.routes() if r.method == "GET"]
        assert paths.index("/api/spawn/session-memory") < paths.index("/api/spawn/{agent_id}")
        handler = next(
            r.handler
            for r in app.router.routes()
            if r.resource.canonical == "/api/spawn/session-memory"
        )
        assert handler.__name__ == "api_spawn_session_memory"


class TestSampleReuse:
    def test_concurrent_callers_share_one_sample(self) -> None:
        started = 0

        async def sample(*_a: object, **_k: object) -> dict[str, Any]:
            nonlocal started
            started += 1
            await asyncio.sleep(0)
            return _payload()

        state = SimpleNamespace(sessions=object(), subagents=object())

        async def run() -> list[dict[str, object]]:
            return list(await asyncio.gather(*(scope_mod._host_sample(state) for _ in range(5))))

        with patch.object(scope_mod._scope_sampler, "sample", sample):
            results = asyncio.run(run())
        assert started == 1
        assert all(r is results[0] for r in results)

    def test_a_stale_sample_is_taken_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = iter([100.0, 100.0, 106.0, 106.0])
        monkeypatch.setattr(scope_mod, "_clock", lambda: next(clock))
        first, fresh = _payload(), _payload()
        sample = AsyncMock(side_effect=[first, fresh])
        state = SimpleNamespace(sessions=object(), subagents=object())
        with patch.object(scope_mod._scope_sampler, "sample", sample):
            assert asyncio.run(scope_mod._host_sample(state)) is first
            assert asyncio.run(scope_mod._host_sample(state)) is fresh
        assert sample.await_count == 2


class TestToolBlock:
    def test_it_renders_counts_and_sizes(self) -> None:
        lines = tool_mod.render_session_memory(caller_memory_view(_payload(), ME))
        text = "\n".join(lines)
        assert "This session: 412 MB, 7 processes, 3 MCP servers" in text
        assert "Sub-agent runtime: 300 MB, 4 processes, ? MCP servers" in text
        assert "Runs you started: 2 live, 100 MB, 2 processes, 1 MCP servers" in text
        assert "(1 not measured yet)" in text
        for secret in SECRETS:
            assert secret not in text

    def test_it_sends_the_strictly_verified_key(self) -> None:
        get = patch.object(
            tool_mod.mcp_core, "_get", return_value=caller_memory_view(_payload(), ME)
        )
        strict = patch.object(
            tool_mod.mcp_core, "require_strict_session_key", return_value=(ME, "")
        )
        with strict, get as fake_get:
            lines = tool_mod.session_memory_lines()
        assert fake_get.call_args.args[0] == "/api/spawn/session-memory"
        assert fake_get.call_args.kwargs["session_key"] == ME
        assert any("This session: 412 MB" in line for line in lines)

    def test_an_unverified_caller_reads_nothing(self) -> None:
        strict = patch.object(
            tool_mod.mcp_core, "require_strict_session_key", return_value=("", "no identity")
        )
        with strict, patch.object(tool_mod.mcp_core, "_get") as fake_get:
            lines = tool_mod.session_memory_lines()
        fake_get.assert_not_called()
        assert lines == [
            "\nYour session's memory: unavailable (this caller's session could not be verified)"
        ]

    @pytest.mark.parametrize("answer", [{"error": "nope"}, None, "text"])
    def test_a_refusal_or_failure_degrades_to_one_line(self, answer: object) -> None:
        strict = patch.object(
            tool_mod.mcp_core, "require_strict_session_key", return_value=(ME, "")
        )
        with strict, patch.object(tool_mod.mcp_core, "_get", return_value=answer):
            assert tool_mod.session_memory_lines() == [
                "\nYour session's memory: unavailable (the gateway did not answer)"
            ]

    def test_a_transport_exception_does_not_break_resource_status(self) -> None:
        strict = patch.object(
            tool_mod.mcp_core, "require_strict_session_key", return_value=(ME, "")
        )
        boom = patch.object(tool_mod.mcp_core, "_get", side_effect=OSError("down"))
        with strict, boom:
            assert len(tool_mod.session_memory_lines()) == 1
