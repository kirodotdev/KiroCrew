"""pi's spec projection, spawn resolution, probe and deny-match.

``providers/mirrors/pi.py`` reuses claude's translation and adds pi's own rules.
Each rule here is a property of the ADAPTER, read off its source or its wire:

* names sanitize the adapter's way (``mcp-bridge.ts sanitizeName``), not codex's
  whitespace fold -- so the denied set carries registered spellings and the
  client matches permission titles by equality, never by parsing ``__``;
* no transport is filtered: the adapter accepts stdio/http/sse and skips an
  unknown shape with a log while ``session/new`` succeeds, so unlike codex there
  is no fatal shape;
* Crew's OWN control plane carries ``KIROCREW_SESSION_KEY`` on the element;
* a narrowed third-party server is withheld, and a narrowed control-plane tool
  is refused at the approval request (``AcpClient._deny_spec_disabled_tool``).

The live wire these shapes were read from is pinned by the frame-replay corpus
(``test/fixtures/acp_frames/pi/permission-live.jsonl``); these tests pin the
Crew side that consumes it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.acp import session_mcp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_PI
from kiro_crew.providers.mirrors.pi import (
    PiMirror,
    pi_elements,
    pi_name,
    pi_projection,
    pi_tool_title,
    pi_withheld_servers,
)

_CORE = {"command": "/opt/kirocrew", "args": ["mcp-core"]}
_CRON = {"command": "/opt/kirocrew", "args": ["mcp-cron"]}


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """Point the agent-spec resolver at a temp agents directory.

    Same seam as ``test_codex_session_mcp.py``: materialization would rebuild the
    managed default from bundled defaults, and these tests supply the spec.
    """
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    managed = {"kirocrew-core": dict(_CORE), "kirocrew-cron": dict(_CRON)}
    monkeypatch.setattr(
        session_mcp,
        "managed_mcp_spec_entry",
        lambda name: dict(managed[name]) if name in managed else None,
    )
    monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    return d


def _write_spec(agents_dir: Path, *, servers: dict, tools: list | None) -> None:
    spec: dict = {"name": "kirocrew", "mcpServers": servers}
    if tools is not None:
        spec["tools"] = tools
    (agents_dir / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")


def _by_name(elements: list[dict]) -> dict[str, dict]:
    return {e["name"]: e for e in elements}


def _env(element: dict) -> dict[str, str]:
    return {p["name"]: p["value"] for p in element.get("env") or []}


def _pi_mcp_tool_call(call_id: str, server: str, tool: str) -> JsonRpcMessage:
    """The ``tool_call`` frame pi-acp emits for a bridged call.

    Title is the registered ``mcp__<server>__<tool>`` spelling, kind is ``other``
    (bridged tools are not shell), and rawInput carries the MCP arguments -- no
    ``server``/``tool`` keys, no ``_meta``. Shapes verified live in
    ``test/fixtures/acp_frames/pi/permission-live.jsonl``.
    """
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "ses_pi_1",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "kind": "other",
                "title": pi_tool_title(server, tool),
                "status": "pending",
                "locations": [{"path": "/tmp"}],
                "rawInput": {"text": "hello"},
            },
        },
    )


def _pi_mcp_approval(request_id: int, call_id: str, server: str, tool: str) -> JsonRpcMessage:
    """The correlated ``session/request_permission``: the adapter's three options."""
    return JsonRpcMessage(
        id=request_id,
        method="session/request_permission",
        params={
            "sessionId": "ses_pi_1",
            "toolCall": {
                "toolCallId": call_id,
                "title": pi_tool_title(server, tool),
                "kind": "other",
                "status": "pending",
                "rawInput": {"text": "hello"},
            },
            "options": [
                {"optionId": "once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "always", "name": "Always allow", "kind": "allow_always"},
                {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
            ],
        },
    )


# ── names ───────────────────────────────────────────────────────────────────


class TestPiName:
    def test_sanitize_matches_the_adapter(self):
        # mcp-bridge.ts sanitizeName: [^a-zA-Z0-9_-] -> _, capped at 64.
        assert pi_name("my srv/tool!") == "my_srv_tool_"
        assert pi_name("kirocrew-core") == "kirocrew-core"
        assert pi_name("a" * 100) == "a" * 64
        assert pi_name("") == "unnamed"

    def test_title_is_the_registered_spelling(self):
        assert pi_tool_title("kirocrew-core", "spawn_run") == "mcp__kirocrew-core__spawn_run"
        assert pi_tool_title("my srv", "do x") == "mcp__my_srv__do_x"


# ── projection ──────────────────────────────────────────────────────────────


class TestPiProjection:
    def _client(self, tmp_path, agents_dir, *, disabled: list[str]) -> AcpClient:
        _write_spec(
            agents_dir,
            servers={
                "kirocrew-core": {
                    "command": "/opt/kirocrew",
                    "args": ["mcp-core"],
                    "disabledTools": disabled,
                }
            },
            tools=["@kirocrew-core"],
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_PI)
        client._session_mcp_cache = client._resolve_session_mcp_servers()
        return client

    def test_control_plane_mounts_with_session_identity(self, tmp_path, agents_dir):
        _write_spec(
            agents_dir,
            servers={"kirocrew-core": dict(_CORE)},
            tools=["@kirocrew-core"],
        )
        out = pi_projection("kirocrew", work_dir=None, session_key="K", channel_id="C")
        names = _by_name(out.params["mcpServers"])
        assert "kirocrew-core" in names
        env = _env(names["kirocrew-core"])
        assert env["KIROCREW_SESSION_KEY"] == "K"
        assert env["KIROCREW_CHANNEL_ID"] == "C"

    def test_a_narrowed_third_party_server_is_withheld(self, tmp_path, agents_dir):
        _write_spec(
            agents_dir,
            servers={
                "third": {
                    "command": "/opt/third",
                    "disabledTools": ["secret_tool"],
                }
            },
            tools=["@third"],
        )
        out = pi_projection("kirocrew", work_dir=None)
        assert "third" not in _by_name(out.params["mcpServers"])
        assert ("third", "secret_tool") in out.denied_tools

    def test_the_deny_set_comes_out_of_the_projection(self, tmp_path, agents_dir):
        client = self._client(tmp_path, agents_dir, disabled=["spawn_run"])
        assert ("kirocrew-core", "spawn_run") in client._spec_denied_tools
        # And the server itself is still mounted: the restriction narrows a tool,
        # it does not cost the session its control plane.
        assert "kirocrew-core" in _by_name(client._session_mcp_servers())

    def test_withheld_servers_derive_from_one_parse(self):
        assert pi_withheld_servers(frozenset({"third"})) >= {"third"}

    def test_mirror_serves_pi(self):
        from kiro_crew.providers.mirrors import mirror_for
        from kiro_crew.providers.mirrors.pi import PiMirror

        assert isinstance(mirror_for(ACP_BACKEND_PI), PiMirror)

    def test_elements_claim_sanitized_names_once(self):
        out = pi_elements(
            [
                {"name": "my srv", "command": "/x"},
                {"name": "my_srv", "command": "/y"},
            ]
        )
        assert [e["name"] for e in out] == ["my_srv"]


# ── deny at the approval request ────────────────────────────────────────────


class TestPiDenyAtApproval:
    def _client(self, tmp_path, agents_dir, *, servers: dict) -> AcpClient:
        _write_spec(agents_dir, servers=servers, tools=["@kirocrew-core", "@my_srv"])
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_PI)
        client._session_mcp_cache = client._resolve_session_mcp_servers()
        return client

    @staticmethod
    def _capture(client: AcpClient) -> list[tuple]:
        sent: list[tuple] = []

        async def _send(request_id, payload):
            sent.append((request_id, payload))

        client._send_response = _send  # type: ignore[method-assign]
        return sent

    @pytest.mark.asyncio
    async def test_a_switched_off_tool_is_refused_by_exact_title(
        self, tmp_path, agents_dir, monkeypatch
    ):
        client = self._client(
            tmp_path,
            agents_dir,
            servers={
                "kirocrew-core": {
                    "command": "/opt/kirocrew",
                    "args": ["mcp-core"],
                    "disabledTools": ["spawn_run"],
                }
            },
        )
        sent = self._capture(client)
        audited: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                audited.append(kw)

        import kiro_crew.sel as sel_mod

        monkeypatch.setattr(sel_mod, "sel", lambda: _Sel())

        assert client._extract_tool_event(
            _pi_mcp_tool_call("c1", "kirocrew-core", "spawn_run")
        )
        event = client._build_permission_event(
            _pi_mcp_approval(7, "c1", "kirocrew-core", "spawn_run")
        )
        assert await client._deny_spec_disabled_tool(event) is True
        # Answered with the adapter's OWN reject option id, never a bare cancel.
        assert sent == [(7, {"outcome": {"outcome": "selected", "optionId": "reject"}})]
        assert audited and audited[0]["outcome"] == "denied"
        assert audited[0]["tool_name"] == "mcp__kirocrew-core__spawn_run"
        assert audited[0]["metadata"]["reason"] == "spec_disabled_tool"

    @pytest.mark.asyncio
    async def test_a_tool_the_spec_left_on_goes_to_the_ordinary_gate(
        self, tmp_path, agents_dir
    ):
        client = self._client(
            tmp_path,
            agents_dir,
            servers={
                "kirocrew-core": {
                    "command": "/opt/kirocrew",
                    "args": ["mcp-core"],
                    "disabledTools": ["spawn_run"],
                }
            },
        )
        sent = self._capture(client)
        client._extract_tool_event(_pi_mcp_tool_call("c2", "kirocrew-core", "send_message"))
        event = client._build_permission_event(
            _pi_mcp_approval(8, "c2", "kirocrew-core", "send_message")
        )
        assert await client._deny_spec_disabled_tool(event) is False
        assert sent == []

    @pytest.mark.asyncio
    async def test_match_is_equality_not_a_parse(self, tmp_path, agents_dir):
        """Underscores survive the adapter's sanitize, so ``__`` cannot be split.

        The title ``mcp__my_srv__do_x`` must match the denied pair
        (``my_srv``, ``do_x``) and must NOT match (``my``, ``srv__do_x``) --
        only an exact equality over the registered spellings has that property.
        """
        _write_spec(
            agents_dir,
            servers={
                "my_srv": {
                    "command": "/opt/s",
                    "disabledTools": ["do_x"],
                }
            },
            tools=["@my_srv"],
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_PI)
        client._session_mcp_cache = client._resolve_session_mcp_servers()
        assert ("my_srv", "do_x") in client._spec_denied_tools
        sent = self._capture(client)

        client._extract_tool_event(_pi_mcp_tool_call("c3", "my_srv", "do_x"))
        event = client._build_permission_event(_pi_mcp_approval(9, "c3", "my_srv", "do_x"))
        assert await client._deny_spec_disabled_tool(event) is True
        assert sent == [(9, {"outcome": {"outcome": "selected", "optionId": "reject"}})]

        # Same title, different denied pair: no match, no refusal.
        client._spec_denied_tools = frozenset({("my", "srv__do_x")})
        assert await client._deny_spec_disabled_tool(event) is False

    @pytest.mark.asyncio
    async def test_a_non_pi_client_is_untouched(self, tmp_path, agents_dir):
        _write_spec(
            agents_dir,
            servers={"kirocrew-core": {"command": "/x", "disabledTools": ["spawn_run"]}},
            tools=["@kirocrew-core"],
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        client._session_mcp_cache = client._resolve_session_mcp_servers()
        assert client._spec_denied_tools == frozenset()
        client._extract_tool_event(_pi_mcp_tool_call("c4", "kirocrew-core", "spawn_run"))
        event = client._build_permission_event(_pi_mcp_approval(10, "c4", "kirocrew-core", "spawn_run"))
        assert await client._deny_spec_disabled_tool(event) is False


# ── resolver + probe ────────────────────────────────────────────────────────


class TestPiResolver:
    def test_override_file_wins(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        entry = tmp_path / "index.js"
        entry.write_text("// pi-acp", encoding="utf-8")
        monkeypatch.setenv("PI_ACP_BIN", str(entry))
        monkeypatch.setattr(client_mod, "_resolve_node_for_script", lambda _s: "/usr/bin/node")
        argv, _searched = client_mod._resolve_pi_acp_bin()
        assert argv == ["/usr/bin/node", str(entry.resolve())]

    def test_vendored_copy_needs_its_dependency_marker(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        root = tmp_path / "node_modules"
        entry = root / "@kirocrew" / "pi-acp" / "dist" / "index.js"
        entry.parent.mkdir(parents=True)
        entry.write_text("// pi-acp", encoding="utf-8")
        monkeypatch.delenv("PI_ACP_BIN", raising=False)
        monkeypatch.setattr(client_mod, "_vendored_acp_roots", lambda *a: [root])
        monkeypatch.setattr(client_mod, "_mise_which", lambda _b: None)
        monkeypatch.setattr(client_mod, "_mise_node_installs_dir", lambda: tmp_path / "no-mise")
        monkeypatch.setattr(client_mod, "augmented_path", lambda _p: "")
        # Entry without the hoisted dependency beside it is skipped, not crashed on.
        argv, _searched = client_mod._resolve_pi_acp_bin()
        assert argv is None
        (root / "@earendil-works" / "pi-coding-agent").mkdir(parents=True)
        monkeypatch.setattr(client_mod, "_resolve_node_for_script", lambda _s: "/usr/bin/node")
        argv, _searched = client_mod._resolve_pi_acp_bin()
        assert argv == ["/usr/bin/node", str(entry.resolve())]

    def test_absent_everywhere_is_none(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.delenv("PI_ACP_BIN", raising=False)
        monkeypatch.setattr(client_mod, "_vendored_acp_roots", lambda *a: [])
        monkeypatch.setattr(client_mod, "_mise_which", lambda _b: None)
        monkeypatch.setattr(client_mod, "_mise_node_installs_dir", lambda: tmp_path / "no-mise")
        monkeypatch.setattr(client_mod, "augmented_path", lambda _p: "")
        monkeypatch.setattr(client_mod.shutil, "which", lambda *a, **k: None)
        argv, _searched = client_mod._resolve_pi_acp_bin()
        assert argv is None


class TestPiProbe:
    def test_missing_names_the_component(self, monkeypatch):
        import test_agent_sdk_backend_install as install_test
        from kiro_crew.agent_sdk import backend_install as probe

        install_test._stub_resolvers(monkeypatch)
        probe.clear_probe_cache()
        state = probe.probe_backend(ACP_BACKEND_PI)
        assert state.installed == probe.MISSING
        assert state.missing_components == (probe.COMPONENT_PI_ACP_ADAPTER,)
        assert state.install_command == ""

    def test_present_adapter_is_installed(self, monkeypatch):
        import test_agent_sdk_backend_install as install_test
        from kiro_crew.agent_sdk import backend_install as probe

        install_test._stub_resolvers(
            monkeypatch, pi=(["node", "/opt/pi-acp/dist/index.js"], "/usr/bin")
        )
        probe.clear_probe_cache()
        state = probe.probe_backend(ACP_BACKEND_PI)
        assert state.installed == probe.INSTALLED
        assert state.missing_components == ()

    def test_driver_seams_agree(self, monkeypatch):
        import test_agent_sdk_backend_install as install_test
        from kiro_crew.agent_sdk.drivers import acp as driver

        install_test._stub_resolvers(
            monkeypatch, pi=(["node", "/opt/pi-acp/dist/index.js"], "/usr/bin")
        )
        assert driver.pi_adapter_resolves() is True
        assert driver.pi_adapter_install_command() == ""
