"""A started MCP server that gives the session no tools is reported, not silent.

On kiro-cli, two MCP servers that publish the same tool name collide: one
server's tools reach the model and the other's whole set is dropped. Both still
report ``running``, so the only trace is a server with no tool in ``/tools``.

The ``/mcp`` and ``/tools`` shapes below are trimmed from a live kiro-cli 2.28.0
capture: two stdio servers that each publish one tool named ``echo``, under an
agent spec with ``"tools": ["*"]``. ``dup-beta`` is the server whose tool was
dropped.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from kiro_crew.acp.mcp_session_report import McpSessionReport, servers_exposing_no_tools
from kiro_crew.providers import acp as acp_provider


def _mcp(*servers: tuple[str, str, int]) -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "servers": [
                {"name": n, "status": st, "toolCount": c, "authenticating": False}
                for n, st, c in servers
            ]
        },
    }


def _tools(*rows: tuple[str, str]) -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "tools": [
                {"name": n, "source": src, "description": "", "status": "requires-approval"}
                for n, src in rows
            ]
        },
    }


CLASH_MCP = _mcp(("dup-beta", "running", 1), ("dup-alpha", "running", 1))
CLASH_TOOLS = _tools(("read", "built-in"), ("echo", "mcp:dup-alpha"))
SPEC_ALL = {"tools": ["*"], "mcpServers": {"dup-alpha": {}, "dup-beta": {}}}

# The tool_search loader is in /tools exactly when the session is deferring its
# MCP tools; its presence is how the detector tells deferral from a clash.
LOADER_ROW = ("tool_search", "native")


def _defer_tools(*rows: tuple[str, str]) -> dict[str, Any]:
    return _tools(LOADER_ROW, *rows)


class TestServersExposingNoTools:
    def test_the_server_that_lost_the_clash_is_named(self):
        assert servers_exposing_no_tools(CLASH_MCP, CLASH_TOOLS, SPEC_ALL) == ("dup-beta",)

    def test_both_servers_exposing_tools_is_clean(self):
        tools = _tools(("echo", "mcp:dup-alpha"), ("ping", "mcp:dup-beta"))
        assert servers_exposing_no_tools(CLASH_MCP, tools, SPEC_ALL) == ()

    def test_a_server_the_spec_does_not_grant_is_not_reported(self):
        # Captured too: with "tools": ["@dup-alpha", "fs_read"] dup-beta still
        # runs, and /tools rightly lists none of its tools.
        spec = {"tools": ["@dup-alpha", "fs_read"]}
        assert servers_exposing_no_tools(CLASH_MCP, CLASH_TOOLS, spec) == ()

    def test_a_server_granted_by_ref_is_reported(self):
        spec = {"tools": ["@dup-alpha", "@dup-beta/echo"]}
        assert servers_exposing_no_tools(CLASH_MCP, CLASH_TOOLS, spec) == ("dup-beta",)

    @pytest.mark.parametrize("excluded", [["@dup-beta"], ["*"]])
    def test_an_excluded_server_is_not_reported(self, excluded):
        spec = {**SPEC_ALL, "excludedTools": excluded}
        assert servers_exposing_no_tools(CLASH_MCP, CLASH_TOOLS, spec) == ()

    def test_a_server_with_disabled_tools_is_not_reported(self):
        spec = {"tools": ["*"], "mcpServers": {"dup-beta": {"disabledTools": ["echo"]}}}
        assert servers_exposing_no_tools(CLASH_MCP, CLASH_TOOLS, spec) == ()

    def test_a_server_still_loading_is_not_reported(self):
        mcp = _mcp(("dup-beta", "loading", 0), ("dup-alpha", "running", 1))
        assert servers_exposing_no_tools(mcp, CLASH_TOOLS, SPEC_ALL) == ()

    def test_a_tool_search_deferred_server_is_not_reported(self):
        # Default install: the tool_search loader is in /tools, so a running
        # server absent from /tools (positive toolCount) is deferred, not a
        # clash victim, and must not be reported.
        mcp = _mcp(("deferred", "running", 9), ("other", "running", 4))
        tools = _defer_tools(("read", "built-in"))
        assert servers_exposing_no_tools(mcp, tools, SPEC_ALL) == ()

    def test_under_deferral_only_a_zero_toolcount_server_is_reported(self):
        # One server genuinely advertised nothing (toolCount 0); the other is
        # merely deferred (toolCount 7). Only the empty one is evidence.
        mcp = _mcp(("empty", "running", 0), ("deferred", "running", 7))
        tools = _defer_tools(("read", "built-in"))
        assert servers_exposing_no_tools(mcp, tools, SPEC_ALL) == ("empty",)

    @pytest.mark.parametrize(
        "row",
        [
            ("mystery", "running", -1),
            ("mystery", "running", "nope"),
        ],
    )
    def test_under_deferral_an_unreadable_toolcount_says_nothing(self, row):
        name, status, count = row
        mcp = {
            "success": True,
            "data": {
                "servers": [
                    {"name": name, "status": status, "toolCount": count},
                    {"name": "other", "status": "running", "toolCount": 2},
                ]
            },
        }
        tools = _defer_tools(("read", "built-in"))
        spec = {"tools": ["*"], "mcpServers": {name: {}, "other": {}}}
        assert servers_exposing_no_tools(mcp, tools, spec) == ()

    def test_a_missing_toolcount_under_deferral_says_nothing(self):
        mcp = {
            "success": True,
            "data": {
                "servers": [
                    {"name": "nocount", "status": "running"},
                    {"name": "other", "status": "running", "toolCount": 2},
                ]
            },
        }
        tools = _defer_tools(("read", "built-in"))
        spec = {"tools": ["*"], "mcpServers": {"nocount": {}, "other": {}}}
        assert servers_exposing_no_tools(mcp, tools, spec) == ()

    def test_the_clash_is_still_reported_with_tool_search_off(self):
        # No loader in /tools => nothing is deferred, so absence from /tools is
        # the clash signal and a positive toolCount does not suppress it.
        assert servers_exposing_no_tools(CLASH_MCP, CLASH_TOOLS, SPEC_ALL) == ("dup-beta",)

    def test_a_deferred_clash_shape_without_the_loader_is_reported(self):
        # Same server shapes as the deferral test, but no loader row in /tools:
        # the detector reads it as a clash and reports both unexposed granted
        # servers, proving the loader is the signal that suppresses the warning.
        mcp = _mcp(("deferred", "running", 9), ("other", "running", 4))
        tools = _tools(("read", "built-in"))
        assert servers_exposing_no_tools(mcp, tools, SPEC_ALL) == ("deferred", "other")

    def test_an_mcp_tool_named_tool_search_does_not_count_as_the_loader(self):
        # A server that happens to publish a tool named tool_search is tagged
        # mcp:<server>, not a built-in loader, so it does not turn deferral on.
        mcp = _mcp(("srv", "running", 1), ("other", "running", 2))
        tools = _tools(("tool_search", "mcp:other"))
        assert servers_exposing_no_tools(mcp, tools, SPEC_ALL) == ("srv",)

    @pytest.mark.parametrize(
        "mcp, tools, spec",
        [
            ({}, CLASH_TOOLS, SPEC_ALL),
            (CLASH_MCP, {}, SPEC_ALL),
            (CLASH_MCP, CLASH_TOOLS, None),
            (CLASH_MCP, CLASH_TOOLS, {"mcpServers": {}}),
        ],
    )
    def test_unreadable_evidence_says_nothing(self, mcp, tools, spec):
        assert servers_exposing_no_tools(mcp, tools, spec) == ()


class TestReportBucket:
    def test_recorded_servers_reach_payload_and_summary(self):
        r = McpSessionReport()
        r.begin_session([])
        assert r.record_no_tools(["dup-beta"]) is True
        payload = r.payload()
        assert payload is not None and payload["no_tools"] == ["dup-beta"]
        assert "gave no tools" in r.problem_summary()
        assert "dup-beta" in r.problem_summary(include_reasons=False)

    def test_a_new_session_clears_it(self):
        r = McpSessionReport()
        r.record_no_tools(["dup-beta"])
        r.begin_session([])
        assert r.payload()["no_tools"] == []
        assert r.problem_summary() == ""

    def test_recording_the_same_answer_is_no_change(self):
        r = McpSessionReport()
        r.record_no_tools(["dup-beta"])
        assert r.record_no_tools(["dup-beta"]) is False

    def test_names_past_the_cap_are_counted_and_said(self):
        # 66 running servers clash with one: 65 lose. The bucket keeps 64 and
        # says the 65th exists instead of dropping it without a word.
        mcp = _mcp(*((f"s{i:02d}", "running", 1) for i in range(66)))
        tools = _tools(("echo", "mcp:s00"))
        names = servers_exposing_no_tools(mcp, tools, {"tools": ["*"]})
        assert len(names) == 65
        r = McpSessionReport()
        r.begin_session([])
        r.record_no_tools(names)
        payload = r.payload()
        assert len(payload["no_tools"]) == 64 and payload["no_tools_omitted"] == 1
        assert "(+1 not listed)" in r.problem_summary()
        r.begin_session([])
        assert r.payload()["no_tools_omitted"] == 0


class _FakeClient:
    """Stands in for the started session: answers native commands from a table."""

    _agent = "zt"

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[str] = []
        self.report = McpSessionReport()
        self.report.begin_session([])

    async def command_result(self, command: str) -> dict[str, Any]:
        self.calls.append(command)
        answer = self.answers[command]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def mcp_session_report(self) -> McpSessionReport:
        return self.report


def _provider(client: _FakeClient) -> Any:
    provider = object.__new__(acp_provider.AcpProvider)
    provider._client = client
    return provider


@pytest.fixture
def spec_all(monkeypatch):
    monkeypatch.setattr(acp_provider, "agent_spec_snapshot", lambda agent, work_dir=None: SPEC_ALL)
    monkeypatch.setattr(acp_provider.AcpProvider, "cwd", property(lambda self: ""))


class TestStartWarning:
    def test_a_clash_is_recorded_and_logged(self, spec_all, caplog):
        client = _FakeClient({"/mcp": CLASH_MCP, "/tools": CLASH_TOOLS})
        with caplog.at_level(logging.WARNING, logger=acp_provider.logger.name):
            asyncio.run(_provider(client)._note_zero_tool_servers())
        assert client.report.payload()["no_tools"] == ["dup-beta"]
        assert any("dup-beta" in rec.getMessage() for rec in caplog.records)

    def test_one_running_server_skips_the_tools_read(self, spec_all, caplog):
        client = _FakeClient({"/mcp": _mcp(("dup-alpha", "running", 1))})
        with caplog.at_level(logging.WARNING, logger=acp_provider.logger.name):
            asyncio.run(_provider(client)._note_zero_tool_servers())
        assert client.calls == ["/mcp"]
        assert client.report.no_tools == ()
        assert not caplog.records

    def test_a_failing_command_never_fails_the_start(self, spec_all):
        client = _FakeClient({"/mcp": RuntimeError("backend gone")})
        asyncio.run(_provider(client)._note_zero_tool_servers())
        assert client.report.no_tools == ()

    def test_under_deferral_a_deferred_clash_shape_is_not_recorded(self, spec_all, caplog):
        # The default-install path: two running servers, one absent from /tools
        # but advertising tools (toolCount 1), and the tool_search loader in
        # /tools. It is deferred, not a clash victim, so nothing is recorded.
        tools = _defer_tools(("echo", "mcp:dup-alpha"))
        client = _FakeClient({"/mcp": CLASH_MCP, "/tools": tools})
        with caplog.at_level(logging.WARNING, logger=acp_provider.logger.name):
            asyncio.run(_provider(client)._note_zero_tool_servers())
        assert client.report.no_tools == ()
        assert not caplog.records

    def test_under_deferral_a_zero_toolcount_server_is_recorded(self, spec_all):
        mcp = _mcp(("empty", "running", 0), ("dup-alpha", "running", 1))
        tools = _defer_tools(("echo", "mcp:dup-alpha"))
        client = _FakeClient({"/mcp": mcp, "/tools": tools})
        asyncio.run(_provider(client)._note_zero_tool_servers())
        assert client.report.payload()["no_tools"] == ["empty"]
