"""Per-tool ``@kirocrew-core/<tool>`` refs as an include list, behind an opt-in.

A spec that sets ``managedToolPolicy.honourToolRefs: true`` and grants
kirocrew-core only tool by tool gets an ``include`` in its tool-policy answer;
kirocrew-core then lists only those tools and refuses any other call. Every
other spec -- the opt-in absent or ``false``, a bare ``@kirocrew-core``, ``*``,
a glob tool part -- gets the answer and the tool list it gets without the key.

Three layers, each tested on its own: the ``tools`` reader
(:func:`kiro_crew.agent_sdk.mcp_refs.per_tool_grants`), the gateway's policy
read, and the managed server's ``tools/list`` / ``tools/call`` handling.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import kiro_crew.mcp_shared as mcp_shared
from kiro_crew.agent_sdk.mcp_refs import per_tool_grants
from kiro_crew.dashboard.handlers import sessions as sessions_mod
from kiro_crew.testing.wait import wait_until

AGENT = "narrow-agent"
CORE = "kirocrew-core"


# --------------------------------------------------------------------------- #
# The ``tools`` reader
# --------------------------------------------------------------------------- #


class TestPerToolGrants:
    def test_per_tool_refs_only_name_the_tools(self) -> None:
        tools = [f"@{CORE}/skill_search", f"@{CORE}/spawn_run", "fs_read", "@other/x"]
        assert per_tool_grants(tools, CORE) == frozenset({"skill_search", "spawn_run"})

    @pytest.mark.parametrize(
        "tools",
        [
            [f"@{CORE}"],
            [f"@{CORE}/skill_search", f"@{CORE}"],
            ["*", f"@{CORE}/skill_search"],
            [f"@{CORE}/*"],
            [f"@{CORE}/skill_search", f"@{CORE}/artifact_*"],
            [f"@{CORE}/"],
        ],
        ids=["bare", "bare-and-tool", "star", "server-glob", "tool-glob", "empty-tool"],
    )
    def test_a_whole_server_grant_names_no_include(self, tools: list[str]) -> None:
        assert per_tool_grants(tools, CORE) is None

    @pytest.mark.parametrize(
        "tools",
        [[], ["fs_read"], ["@kirocrew-cron/cron_add"], [f"@{CORE}-extra/x"], None, "nope"],
        ids=["empty", "builtin-only", "other-server", "prefix-sibling", "none", "not-a-list"],
    )
    def test_no_ref_to_the_server_names_no_include(self, tools: Any) -> None:
        assert per_tool_grants(tools, CORE) is None


# --------------------------------------------------------------------------- #
# The gateway's policy read
# --------------------------------------------------------------------------- #


def _policy_for(tmp_path: Path, spec: dict[str, Any]) -> dict[str, Any] | None:
    (tmp_path / f"{AGENT}.json").write_text(json.dumps({"name": AGENT, **spec}), encoding="utf-8")
    return sessions_mod._read_managed_tool_policy_uncached(tmp_path, AGENT)


class TestGatewayPolicyAnswer:
    def test_opted_in_per_tool_refs_answer_an_include(self, tmp_path: Path) -> None:
        policy = _policy_for(
            tmp_path,
            {
                "tools": [f"@{CORE}/spawn_run", f"@{CORE}/skill_search", "fs_read"],
                "managedToolPolicy": {"honourToolRefs": True, "exclude": ["cron_add"]},
            },
        )
        assert policy == {
            "honourToolRefs": True,
            "exclude": ["cron_add"],
            "include": {CORE: ["skill_search", "spawn_run"]},
        }

    @pytest.mark.parametrize(
        "managed",
        [None, {}, {"exclude": ["cron_add"]}, {"honourToolRefs": False}],
        ids=["no-policy", "empty-policy", "exclude-only", "opt-in-false"],
    )
    def test_an_unchanged_spec_answers_the_policy_as_written(
        self, tmp_path: Path, managed: dict[str, Any] | None
    ) -> None:
        spec: dict[str, Any] = {"tools": [f"@{CORE}/skill_search"]}
        if managed is not None:
            spec["managedToolPolicy"] = managed
        assert _policy_for(tmp_path, spec) == (managed or {})

    @pytest.mark.parametrize(
        "tools",
        [[f"@{CORE}"], ["*"], [f"@{CORE}/*"], [f"@{CORE}/skill_search", f"@{CORE}"]],
        ids=["bare", "star", "glob", "bare-and-tool"],
    )
    def test_an_opted_in_whole_server_grant_answers_no_include(
        self, tmp_path: Path, tools: list[str]
    ) -> None:
        policy = _policy_for(
            tmp_path, {"tools": tools, "managedToolPolicy": {"honourToolRefs": True}}
        )
        assert policy == {"honourToolRefs": True}

    def test_an_include_written_in_the_spec_is_not_answered(self, tmp_path: Path) -> None:
        policy = _policy_for(
            tmp_path,
            {
                "tools": [f"@{CORE}"],
                "managedToolPolicy": {"exclude": ["x"], "include": {CORE: ["skill_search"]}},
            },
        )
        assert policy == {"exclude": ["x"]}

    @pytest.mark.parametrize("value", ["yes", 1, None, ["true"]])
    def test_a_non_bool_opt_in_is_unreadable(self, tmp_path: Path, value: Any) -> None:
        with pytest.raises(sessions_mod.ManagedToolPolicyUnreadable, match="honourToolRefs"):
            _policy_for(
                tmp_path,
                {
                    "tools": [f"@{CORE}/skill_search"],
                    "managedToolPolicy": {"honourToolRefs": value},
                },
            )


# --------------------------------------------------------------------------- #
# The managed server's reading of the answer
# --------------------------------------------------------------------------- #


def _reset_policy_state() -> None:
    mcp_shared._excluded_tools_by_session.clear()
    mcp_shared._included_tools_by_session.clear()
    mcp_shared._last_failure_time = 0.0
    mcp_shared._last_startup_race_time = 0.0
    mcp_shared._failure_count = 0


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch):
    """Answer the policy endpoint with the body the test sets."""
    _reset_policy_state()
    box: dict[str, bytes] = {"body": b"{}"}

    def answer(_req: Any, timeout: float = 0) -> io.BytesIO:
        return io.BytesIO(box["body"])

    monkeypatch.setattr(mcp_shared, "loopback_urlopen", answer)
    monkeypatch.setattr(mcp_shared, "read_local_secret", lambda _port, **_kw: "internal")
    monkeypatch.setattr(mcp_shared, "resolve_client_port_src", lambda port: (5476, "config"))
    monkeypatch.setattr(mcp_shared, "sel", lambda: MagicMock())
    yield box
    _reset_policy_state()


class TestPolicyResolution:
    def test_an_include_is_read_per_server_and_served_from_the_cache(self, gateway) -> None:
        gateway["body"] = json.dumps({"exclude": [], "include": {CORE: ["skill_search"]}}).encode()
        first = mcp_shared._resolve_tool_policy("dashboard:alice")
        assert first.unresolved == ""
        assert first.include_for(CORE) == frozenset({"skill_search"})
        assert first.include_for("kirocrew-cron") is None
        gateway["body"] = b"{}"
        cached = mcp_shared._resolve_tool_policy("dashboard:alice")
        assert cached.include_for(CORE) == frozenset({"skill_search"})

    def test_an_answer_without_include_narrows_nothing(self, gateway) -> None:
        gateway["body"] = b'{"exclude":["blocked"]}'
        policy = mcp_shared._resolve_tool_policy("dashboard:bob")
        assert policy.excluded == {"blocked"}
        assert policy.include_for(CORE) is None
        assert mcp_shared._included_tools_by_session == {}

    @pytest.mark.parametrize(
        "include",
        [["skill_search"], {CORE: "skill_search"}, {CORE: [1]}, {"": ["a"]}, "x"],
        ids=["list", "string-tools", "non-string-tool", "empty-server", "string"],
    )
    def test_a_malformed_include_refuses_rather_than_narrowing_part(
        self, gateway, include: Any
    ) -> None:
        gateway["body"] = json.dumps({"include": include}).encode()
        policy = mcp_shared._resolve_tool_policy("dashboard:carol")
        assert policy.unresolved == "policy_unreadable"
        assert policy.unresolved in mcp_shared._UNRESOLVED_REFUSES_CALL
        assert "dashboard:carol" not in mcp_shared._excluded_tools_by_session


class _Loop:
    """``run_mcp_stdio_loop`` on a pipe, under a fixed policy and server name."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        server_name: str,
        policy: mcp_shared.ToolPolicy,
        ran: list[str],
    ) -> None:
        self.responses: list[tuple[Any, Any, Any]] = []
        rfd, self._wfd = os.pipe()
        self._stdin = io.TextIOWrapper(io.open(rfd, "rb"))
        monkeypatch.setattr(sys, "stdin", self._stdin)
        monkeypatch.setattr(mcp_shared, "respond", self._record)
        monkeypatch.setattr(mcp_shared, "_resolve_tool_policy", lambda *a, **k: policy)
        # ``call_tool`` below stands in for a managed server's own handler.
        monkeypatch.setattr(mcp_shared, "serves_managed_tools", lambda _fn: True)
        self.sel_mock = MagicMock()
        monkeypatch.setattr(mcp_shared, "sel", lambda: self.sel_mock)

        def call_tool(name: str, _args: dict[str, Any]) -> str:
            ran.append(name)
            return "ok"

        self._thread = threading.Thread(
            target=mcp_shared.run_mcp_stdio_loop,
            args=(
                server_name,
                "0.0.0",
                lambda: [{"name": "skill_search"}, {"name": "spawn_run"}],
                call_tool,
            ),
            daemon=True,
        )
        self._thread.start()

    def _record(self, req_id: Any, result: Any, error: Any = None) -> None:
        self.responses.append((req_id, result, error))

    def send(self, msg: dict[str, Any]) -> None:
        os.write(self._wfd, (json.dumps(msg) + "\n").encode("utf-8"))

    def reply(self, req_id: int) -> Any:
        found = wait_until(
            lambda: next((r for r in self.responses if r[0] == req_id), None),
            describe=lambda: self.responses,
        )
        return found[1]

    def listed(self, req_id: int) -> list[str]:
        self.send({"jsonrpc": "2.0", "id": req_id, "method": "tools/list"})
        return [t["name"] for t in self.reply(req_id)["tools"]]

    def call(self, req_id: int, tool: str) -> str:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": {}},
            }
        )
        return json.dumps(self.reply(req_id))

    def close(self) -> None:
        os.close(self._wfd)
        self._thread.join(timeout=30.0)


def _narrowing(server: str) -> mcp_shared.ToolPolicy:
    return mcp_shared.ToolPolicy(
        frozenset(),
        "",
        included=types.MappingProxyType({server: frozenset({"skill_search"})}),
    )


class TestManagedServerHonoursInclude:
    def test_the_named_server_lists_and_runs_only_the_included_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ran: list[str] = []
        loop = _Loop(monkeypatch, CORE, _narrowing(CORE), ran)
        try:
            assert loop.listed(1) == ["skill_search"]
            refused = loop.call(2, "spawn_run")
            assert "not available for this agent" in refused
            assert ran == []
            outcomes = [
                c.kwargs.get("outcome") for c in loop.sel_mock.log_tool_invocation.call_args_list
            ]
            assert "rejected_not_included" in outcomes
            assert "ok" in loop.call(3, "skill_search")
            assert ran == ["skill_search"]
        finally:
            loop.close()

    def test_an_include_for_another_server_narrows_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ran: list[str] = []
        loop = _Loop(monkeypatch, "kirocrew-cron", _narrowing(CORE), ran)
        try:
            assert loop.listed(1) == ["skill_search", "spawn_run"]
            assert "ok" in loop.call(2, "spawn_run")
            assert ran == ["spawn_run"]
        finally:
            loop.close()

    def test_a_policy_with_no_include_lists_and_runs_everything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ran: list[str] = []
        loop = _Loop(monkeypatch, CORE, mcp_shared.ToolPolicy(frozenset(), ""), ran)
        try:
            assert loop.listed(1) == ["skill_search", "spawn_run"]
            assert "ok" in loop.call(2, "spawn_run")
            assert ran == ["spawn_run"]
        finally:
            loop.close()
