"""Crew MCP tools on a spec-adapter session: delivery, identity, and directives.

kiro-cli advertises ``kirocrew-core`` through its agent spec and tags every
MCP tool call with ``_meta.kiro``. A spec adapter gets the same server only
on ``session/new``, and it names the call ``mcp__<server>__<tool>`` in
``title`` with no ``_meta``. Follow-up cards and workflow POSTs both depend
on those two facts being true at once — delivery without identity drops
``ask_question`` / ``suggest_followup`` silently, and identity without a
gateway pin makes ``workflow_run`` miss the loopback.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp import spec_servers
from kiro_crew.acp._dispatch import parse_session_update
from kiro_crew.acp.codex import Verdict
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    EVENT_TOOL_CALL,
)
from kiro_crew.mcp_tools import build_tool_list
from kiro_crew.session_directive import decode, match_tool


def _client(backend: str, work_dir):
    from kiro_crew.acp.client import AcpClient

    return AcpClient(work_dir=str(work_dir), acp_backend=backend)


def _tool_call_update(*, title: str, kind: str, meta: dict | None = None) -> dict:
    update: dict = {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc-1",
        "title": title,
        "kind": kind,
        "rawInput": {"questions": []},
    }
    if meta is not None:
        update["_meta"] = meta
    return update


class TestRoutedAdaptersReceiveCoreTools:
    def test_codex_withholds_core_until_session_config_is_acknowledged(self, tmp_path) -> None:
        """SESSION_CONFIG routing is not established before session/new."""
        assert _client(ACP_BACKEND_CODEX, tmp_path)._spec_session_mcp_servers() == []

    def test_claude_seeded_settings_deliver_core(self, tmp_path) -> None:
        from kiro_crew.acp import tool_gate

        tool_gate.enforce(ACP_BACKEND_CLAUDE, tmp_path, allow_ungated=False)
        names = {
            e["name"] for e in _client(ACP_BACKEND_CLAUDE, tmp_path)._spec_session_mcp_servers()
        }
        assert "kirocrew-core" in names

    def test_goose_withholds_core_until_permission_routing_is_acknowledged(self, tmp_path) -> None:
        assert _client(ACP_BACKEND_GOOSE, tmp_path)._spec_session_mcp_servers() == []

    def test_opencode_is_routed_and_receives_crew_servers(self, tmp_path) -> None:
        from kiro_crew.acp import tool_gate

        tool_gate.enforce(ACP_BACKEND_OPENCODE, tmp_path, allow_ungated=False)
        names = {
            e["name"] for e in _client(ACP_BACKEND_OPENCODE, tmp_path)._spec_session_mcp_servers()
        }
        assert "kirocrew-core" in names
        assert "kirocrew-cron" in names

    def test_pi_is_routed_and_receives_crew_servers(self, tmp_path) -> None:
        """We still deliver when ROUTED. The adapter may leave them inert."""
        names = {e["name"] for e in _client(ACP_BACKEND_PI, tmp_path)._spec_session_mcp_servers()}
        assert "kirocrew-core" in names
        assert "kirocrew-cron" in names

    def test_kiro_does_not_pay_this_seam(self, tmp_path) -> None:
        assert _client(ACP_BACKEND_KIRO, tmp_path)._spec_session_mcp_servers() == []

    def test_kas_does_not_pay_this_seam(self, tmp_path) -> None:
        """KAS speaks the kiro dialect and is served by AcpRuntime.

        This client seam is for spec adapters. Empty here is correct, not a
        hole in the adapter path — KAS session MCP is a runtime concern.
        """
        assert _client(ACP_BACKEND_KAS, tmp_path)._spec_session_mcp_servers() == []


class TestCoreAdvertisesWorkflowsAndFollowups:
    def test_tools_list_includes_the_prompt_facing_control_plane(self) -> None:
        names = {tool["name"] for tool in build_tool_list()}
        assert {
            "workflow_run",
            "workflow_status",
            "workflow_list",
            "ask_question",
            "suggest_followup",
        } <= names


class TestSpecAdapterToolIdentity:
    """Without this, chat_runner never registers a directive tool.

    It keys on ``event.mcp_server_name == kirocrew-core`` and
    ``match_tool(event.tool_name)``. Both were empty for every spec-adapter
    MCP call because identity lived only on ``_meta.kiro``.
    """

    def test_mcp_title_without_meta_is_core_ask_question(self) -> None:
        events = parse_session_update(
            _tool_call_update(title="mcp__kirocrew-core__ask_question", kind="other"),
            allow_spec_title_identity=True,
            spec_mcp_server_names={"kirocrew-core"},
        )
        assert events[0].kind == EVENT_TOOL_CALL
        assert events[0].mcp_server_name == "kirocrew-core"
        assert events[0].tool_name == "ask_question"
        assert match_tool(events[0].tool_name) == "ask_question"

    def test_mcp_title_without_meta_is_core_suggest_followup(self) -> None:
        events = parse_session_update(
            _tool_call_update(title="mcp__kirocrew-core__suggest_followup", kind="other"),
            allow_spec_title_identity=True,
            spec_mcp_server_names={"kirocrew-core"},
        )
        assert events[0].mcp_server_name == "kirocrew-core"
        assert match_tool(events[0].tool_name) == "suggest_followup"

    def test_mcp_title_without_meta_is_core_workflow_run(self) -> None:
        events = parse_session_update(
            _tool_call_update(title="mcp__kirocrew-core__workflow_run", kind="other"),
            allow_spec_title_identity=True,
            spec_mcp_server_names={"kirocrew-core"},
        )
        assert events[0].mcp_server_name == "kirocrew-core"
        assert events[0].tool_name == "workflow_run"

    def test_a_forged_mcp_title_on_a_shell_kind_is_not_an_identity(self) -> None:
        events = parse_session_update(
            _tool_call_update(title="mcp__kirocrew-core__ask_question", kind="execute"),
            allow_spec_title_identity=True,
            spec_mcp_server_names={"kirocrew-core"},
        )
        assert events[0].mcp_server_name == ""
        assert events[0].tool_name == ""
        assert match_tool(events[0].tool_name) == ""

    def test_a_title_without_the_mcp_prefix_is_not_an_identity(self) -> None:
        """kiro-cli titles are LLM prose. They must stay unparsed."""
        events = parse_session_update(
            _tool_call_update(title="Asking the user which approach", kind="other"),
            allow_spec_title_identity=True,
            spec_mcp_server_names={"kirocrew-core"},
        )
        assert events[0].mcp_server_name == ""
        assert events[0].tool_name == ""

    def test_meta_kiro_wins_over_a_conflicting_title(self) -> None:
        events = parse_session_update(
            _tool_call_update(
                title="mcp__evil__ask_question",
                kind="other",
                meta={"kiro": {"toolName": "ask_question", "mcpServerName": "kirocrew-core"}},
            ),
            allow_spec_title_identity=True,
            spec_mcp_server_names={"kirocrew-core"},
        )
        assert events[0].mcp_server_name == "kirocrew-core"
        assert events[0].tool_name == "ask_question"

    def test_missing_kind_does_not_parse_a_title(self) -> None:
        """Fail closed: no kind means we cannot tell a shell from an MCP call."""
        update = _tool_call_update(title="mcp__kirocrew-core__ask_question", kind="other")
        del update["kind"]
        events = parse_session_update(
            update,
            allow_spec_title_identity=True,
            spec_mcp_server_names={"kirocrew-core"},
        )
        assert events[0].mcp_server_name == ""
        assert events[0].tool_name == ""

    def test_title_identity_is_disabled_without_positive_spec_context(self) -> None:
        """The Kiro path must not promote its LLM-authored title to identity."""
        events = parse_session_update(
            _tool_call_update(title="mcp__kirocrew-core__ask_question", kind="other")
        )
        assert events[0].mcp_server_name == ""
        assert events[0].tool_name == ""

    def test_spec_client_enables_title_identity_from_its_backend(self, tmp_path) -> None:
        from kiro_crew.acp.types import JsonRpcMessage

        client = _client(ACP_BACKEND_CODEX, tmp_path)
        client._spec_mcp_server_names = frozenset({"kirocrew-core"})
        event = client._extract_tool_event(
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": _tool_call_update(
                        title="mcp__kirocrew-core__ask_question", kind="other"
                    )
                },
            )
        )
        assert event is not None
        assert event.mcp_server_name == "kirocrew-core"
        assert event.tool_name == "ask_question"

    def test_tool_name_with_double_underscore_uses_exact_session_roster(self) -> None:
        events = parse_session_update(
            _tool_call_update(title="mcp__github__repo__delete", kind="other"),
            allow_spec_title_identity=True,
            spec_mcp_server_names={"github"},
        )
        assert events[0].mcp_server_name == "github"
        assert events[0].tool_name == "repo__delete"

    def test_ambiguous_server_roster_fails_closed(self) -> None:
        events = parse_session_update(
            _tool_call_update(title="mcp__github__repo__delete", kind="other"),
            allow_spec_title_identity=True,
            spec_mcp_server_names={"github", "github__repo"},
        )
        assert events[0].mcp_server_name == ""
        assert events[0].tool_name == ""
        assert events[0].mcp_identity_ambiguous is True

    def test_unrostered_prefixed_title_fails_closed(self) -> None:
        events = parse_session_update(
            _tool_call_update(title="mcp__unrostered__delete", kind="other"),
            allow_spec_title_identity=True,
            spec_mcp_server_names={"github"},
        )
        assert events[0].mcp_server_name == ""
        assert events[0].tool_name == ""
        assert events[0].mcp_identity_ambiguous is True

    def test_ambiguous_server_roster_hard_denies_the_permission(self, tmp_path) -> None:
        """Blank identity must not turn a policy boundary into a human choice."""
        from kiro_crew.acp.types import JsonRpcMessage
        from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig

        client = _client(ACP_BACKEND_OPENCODE, tmp_path)
        client._spec_mcp_server_names = frozenset({"github", "github__repo"})
        client._extract_tool_event(
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": _tool_call_update(title="mcp__github__repo__delete", kind="other")
                },
            )
        )
        permission = client._build_permission_event(
            JsonRpcMessage(
                id=42,
                method="session/request_permission",
                params={
                    "toolCall": {"toolCallId": "tc-1", "title": "Delete repository"},
                    "options": [{"optionId": "allow_once", "name": "Allow once"}],
                },
            )
        )

        result = HookManager(HooksConfig()).on_tool_call(
            permission.title,
            tool_kind=permission.tool_kind,
            raw_params=permission.raw_tool_params,
            mcp_server_name=permission.mcp_server_name,
            mcp_tool_name=permission.tool_name,
            mcp_identity_ambiguous=permission.mcp_identity_ambiguous,
        )

        assert permission.mcp_identity_ambiguous is True
        assert result.action == TOOL_DENY
        assert "ambiguous MCP identity" in (result.reason or "")

    def test_permission_keeps_double_underscore_tool_for_governance(self, tmp_path) -> None:
        from kiro_crew.acp.types import JsonRpcMessage
        from kiro_crew.platform.governance import MODE_DENY, ScopedRuleset

        client = _client(ACP_BACKEND_OPENCODE, tmp_path)
        client._spec_mcp_server_names = frozenset({"github"})
        client._extract_tool_event(
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": _tool_call_update(title="mcp__github__repo__delete", kind="other")
                },
            )
        )
        permission = client._build_permission_event(
            JsonRpcMessage(
                id=42,
                method="session/request_permission",
                params={
                    "toolCall": {"toolCallId": "tc-1", "title": "Delete repository"},
                    "options": [{"optionId": "allow_once", "name": "Allow once"}],
                },
            )
        )

        ref = f"@{permission.mcp_server_name}/{permission.tool_name}"
        rules = ScopedRuleset(mode=MODE_DENY, deny=("@github/repo__delete",), matcher="mcp")
        assert ref == "@github/repo__delete"
        assert not rules.permits(ref).permitted


class TestSessionCallbackEnv:
    def test_pin_writes_session_and_port_onto_every_entry(self) -> None:
        entries = spec_servers.pin_session_callback_env(
            [{"name": "kirocrew-core", "command": "c", "args": [], "env": []}],
            session_key="dashboard:slot-1",
            channel_id="",
            bound_port="18789",
        )
        env = {pair["name"]: pair["value"] for pair in entries[0]["env"]}
        assert env["KIROCREW_SESSION_KEY"] == "dashboard:slot-1"
        assert env["KIROCREW_PORT"] == "18789"
        assert env["KIROCREW_BOUND_PORT"] == "18789"
        assert "KIROCREW_CHANNEL_ID" not in env

    def test_pin_overwrites_a_stale_port_and_keeps_other_keys(self) -> None:
        entries = spec_servers.pin_session_callback_env(
            [
                {
                    "name": "kirocrew-core",
                    "command": "c",
                    "args": [],
                    "env": [
                        {"name": "KIROCREW_HOME", "value": "/tmp/crew"},
                        {"name": "KIROCREW_PORT", "value": "1"},
                    ],
                }
            ],
            session_key="dashboard:s",
            bound_port="9",
        )
        env = {pair["name"]: pair["value"] for pair in entries[0]["env"]}
        assert env["KIROCREW_HOME"] == "/tmp/crew"
        assert env["KIROCREW_PORT"] == "9"

    @pytest.mark.asyncio
    async def test_routed_session_new_array_carries_the_pin(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "18789")
        monkeypatch.setattr(
            "kiro_crew.acp.tool_gate.resolve_verdict",
            lambda backend, work_dir: (Verdict.ROUTED, "delegates"),
        )
        client = _client(ACP_BACKEND_PI, tmp_path)
        client._session_key = "dashboard:slot-9"
        servers = await client._session_mcp_servers()
        core = next(entry for entry in servers if entry["name"] == "kirocrew-core")
        env = {pair["name"]: pair["value"] for pair in core["env"]}
        assert env["KIROCREW_SESSION_KEY"] == "dashboard:slot-9"
        assert env["KIROCREW_BOUND_PORT"] == "18789"
        assert env["KIROCREW_PORT"] == "18789"

    @pytest.mark.asyncio
    async def test_kiro_session_array_is_not_rewritten(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The kiro path must stay byte-identical: servers arrive via --agent."""
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "18789")
        client = _client(ACP_BACKEND_KIRO, tmp_path)
        client._session_key = "dashboard:slot-9"
        pooled = [{"name": "stub", "command": "c", "args": [], "env": []}]
        from unittest.mock import patch

        with patch.object(client, "_pooled_mcp_servers", return_value=pooled):
            assert await client._session_mcp_servers() == pooled


class TestDirectiveRoundTrip:
    def test_ask_question_directive_still_decodes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import kiro_crew.mcp_core as mcp_core
        from kiro_crew.mcp_core import _call_tool_inner

        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
        result = _call_tool_inner(
            "ask_question",
            {
                "questions": [
                    {
                        "question": "Which approach?",
                        "options": [{"label": "A"}, {"label": "B"}],
                    }
                ]
            },
        )
        args = decode(result, "ask_question")
        assert args is not None
        assert args["questions"][0]["question"] == "Which approach?"

    def test_suggest_followup_directive_still_decodes(self) -> None:
        from kiro_crew.mcp_core import _call_tool_inner

        result = _call_tool_inner(
            "suggest_followup",
            {
                "items": [
                    {
                        "title": "Run the workflow",
                        "description": "Author and start the next slice.",
                        "prompt": "Run a workflow that lists recent runs.",
                    }
                ]
            },
        )
        args = decode(result, "suggest_followup")
        assert args is not None
        assert args["items"][0]["title"] == "Run the workflow"
