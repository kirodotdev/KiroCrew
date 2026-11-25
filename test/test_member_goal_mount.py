"""A Bot's own chat on a user agent gains the goal tools on every member backend.

The session-level sibling of ``test_kas_member_goal_tools.py``: the claude-style
client append and the codex-style runtime mount add ``kirocrew-mate`` only to a
Bot's own DM session whose agent lacks it, never on KAS (its projection declares
it) and never when the server or one of its tools is switched off.
"""

from __future__ import annotations

import asyncio

import pytest
import test_member_mount_deepseek as _deepseek

import kiro_crew.validation  # noqa: F401 - break the legacy import cycle first
from kiro_crew.acp import member_goal_mount as mount
from kiro_crew.acp.client import AcpClient, ClaudeAcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX, ACP_BACKEND_KAS
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT
from kiro_crew.members import MEMBER_MATE_SERVER

MEMBER_KEY = "dashboard_member-ar-tpm-bot"
#: The deepseek tests' spec-on-disk fixture, shared rather than copied.
agents_dir = _deepseek.agents_dir
USER_SPEC = {"tools": ["@asana"], "mcpServers": {"asana": {"command": "asana-mcp"}}}
MATE_SPEC = {"tools": ["@kirocrew-mate"], "mcpServers": {"kirocrew-mate": {"command": "k"}}}
#: Each way a spec withholds the server whole (``default_crew_servers.opted_out``).
EXCLUDING_SPECS = [
    {"tools": ["*"], "excludedTools": ["@kirocrew-mate"]},
    {"tools": ["*"], "excludedTools": ["@kirocrew-mate/*"]},
    {"tools": ["*"], "managedToolPolicy": {"excludeServers": ["kirocrew-mate"]}},
]
EXCLUDING_IDS = ["excludedTools", "excludedTools-glob", "excludeServers"]


def _base() -> list[dict]:
    return [{"name": "kirocrew-core", "command": "x", "args": [], "env": [], "type": "stdio"}]


class _ClientStub:
    backend = ACP_BACKEND_CLAUDE
    _agent = "ar-tpm"
    _session_key = MEMBER_KEY
    _claude_settings_authored = True
    _claude_settings_shared = False
    _permission_surface_share_validated = False
    _stub_session_token = "e" * 64
    _withhold_is_the_only_deny_channel = AcpClient._withhold_is_the_only_deny_channel
    _member_mount_withheld = AcpClient._member_mount_withheld

    @property
    def _permission_surface_governed(self):
        return ClaudeAcpClient._permission_surface_governed.fget(self)


def _names(servers: list[dict]) -> list[str]:
    return [e["name"] for e in servers]


class TestClientAppend:
    def test_a_bots_own_chat_on_a_user_agent_gains_the_server(self):
        out = mount.append_member_goal_server(_ClientStub(), _base(), USER_SPEC)
        assert _names(out)[-1] == MEMBER_MATE_SERVER
        assert {"name": "KIROCREW_SESSION_KEY", "value": MEMBER_KEY} in out[-1]["env"]

    @pytest.mark.parametrize(
        ("attr", "value", "spec"),
        [
            ("_session_key", "dashboard_chat-1", USER_SPEC),
            ("_agent", MATE_CONDUCTOR_AGENT, USER_SPEC),
            ("backend", ACP_BACKEND_KAS, USER_SPEC),
            ("_agent", "ar-tpm", MATE_SPEC),
        ],
        ids=["plain-chat", "conductor", "kas-projects-it", "spec-has-it"],
    )
    def test_every_other_session_is_untouched(self, attr, value, spec):
        stub = _ClientStub()
        setattr(stub, attr, value)
        assert mount.append_member_goal_server(stub, _base(), spec) == _base()

    def test_a_disabled_server_is_never_mounted(self):
        out = mount.append_member_goal_server(
            _ClientStub(), _base(), USER_SPEC, disabled={MEMBER_MATE_SERVER}
        )
        assert out == _base()

    @pytest.mark.parametrize("spec", EXCLUDING_SPECS, ids=EXCLUDING_IDS)
    def test_a_spec_that_excludes_the_server_is_never_mounted(self, spec):
        assert mount.append_member_goal_server(_ClientStub(), _base(), spec) == _base()


reads: list[tuple] = []


class TestRuntimeMount:
    @staticmethod
    def _run(monkeypatch, backend, *, disabled=False, narrowed=frozenset(), spec=USER_SPEC):
        from kiro_crew import member_mate_tools
        from kiro_crew.acp import runtime as runtime_mod
        from kiro_crew.acp import session_mcp

        reads.clear()
        rt = object.__new__(runtime_mod.AcpRuntime)
        rt._acp_backend = backend
        monkeypatch.setattr(member_mate_tools, "_read_template", lambda *a: reads.append(a) or spec)
        monkeypatch.setattr(session_mcp, "session_mcp_server_is_disabled", lambda *a, **k: disabled)
        monkeypatch.setattr(session_mcp, "session_mcp_disabled_tools", lambda *a, **k: narrowed)
        return asyncio.run(
            mount.mount_member_goal_tools(
                rt,
                _base(),
                member_session_key=MEMBER_KEY,
                agent_name="ar-tpm",
                session_work_dir="/work",
                stub_token="t" * 64,
            )
        )

    def test_it_reads_the_spec_this_session_consumes(self, monkeypatch):
        """The session's own directory, not the default workspace's."""
        self._run(monkeypatch, ACP_BACKEND_CODEX)
        assert reads == [("ar-tpm", "", "/work")]

    def test_codex_mounts_it(self, monkeypatch):
        assert _names(self._run(monkeypatch, ACP_BACKEND_CODEX))[-1] == MEMBER_MATE_SERVER

    def test_kas_leaves_it_to_the_projection(self, monkeypatch):
        assert self._run(monkeypatch, ACP_BACKEND_KAS) == _base()

    def test_a_switched_off_server_or_tool_withholds_it(self, monkeypatch):
        assert self._run(monkeypatch, ACP_BACKEND_CODEX, disabled=True) == _base()
        narrowed = frozenset({(MEMBER_MATE_SERVER, "set_goal")})
        assert self._run(monkeypatch, ACP_BACKEND_CODEX, narrowed=narrowed) == _base()

    @pytest.mark.parametrize("spec", EXCLUDING_SPECS, ids=EXCLUDING_IDS)
    def test_a_spec_that_excludes_the_server_withholds_it(self, monkeypatch, spec):
        assert self._run(monkeypatch, ACP_BACKEND_CODEX, spec=spec) == _base()

    def test_a_spec_that_has_it_is_untouched(self, monkeypatch):
        assert self._run(monkeypatch, ACP_BACKEND_CODEX, spec=MATE_SPEC) == _base()


class TestTheResolverWiresIt:
    """The real deepseek and pi resolvers, so dropping the append from either
    composition path in ``client_mcp_array`` turns this red."""

    @pytest.mark.parametrize("backend", ["deepseek", "pi"])
    def test_a_user_agent_bot_chat_carries_the_goal_tools(self, agents_dir, tmp_path, backend):
        import json

        _client = _deepseek._client

        (tmp_path / "agents" / "ar-tpm.json").write_text(
            json.dumps({"name": "ar-tpm", "tools": ["read"], "mcpServers": {}}),
            encoding="utf-8",
        )
        client = _client(tmp_path, MEMBER_KEY, backend)
        client._agent = "ar-tpm"
        assert MEMBER_MATE_SERVER in _names(client._resolve_session_mcp_servers())
        client._agent = "kirocrew"
        assert MEMBER_MATE_SERVER not in _names(client._resolve_session_mcp_servers())
