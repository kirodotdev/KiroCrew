"""KAS: a new session says which MCP servers start without part of their config.

KAS takes the agent spec over the ACP wire, so the projection withholds every
``env`` value it cannot prove is not a credential, and every ``headers`` block
(:func:`kiro_crew.acp.kas_agents._withhold_credential_fields`). The server is still
declared and still starts, so a credentialed server left unwrapped by the gateway
started with none of its settings and failed with no visible cause: the only trace
was an INFO log line. These tests pin the projection's record of what it withheld
and the one-line notice a new chat shows for a server the user can fix -- never a
value, a key name or a command line.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from kas_agents_helpers import _spec

from kiro_crew import agent as agent_mod
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.kas_agents import (
    NOT_RUN_INSECURE_URL,
    WithheldOut,
    build_kas_custom_agents,
    to_client_custom_agent,
)
from kiro_crew.acp.kas_withheld_notice import withheld_credentials_notice
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import ACP_BACKEND_KAS
from kiro_crew.config import paths as paths_mod
from kiro_crew.dashboard import chat_runner
from kiro_crew.mcp_gateway import session_servers as session_servers_mod
from kiro_crew.providers.acp import AcpProvider

#: Values that must never reach the notice. Long enough to be unique.
_REFERENCE_VALUE = "${GITLAB_PERSONAL_ACCESS_TOKEN}"
_URL_VALUE = "https://gitlab.internal.example/api/v4"
_REGEX_VALUE = "^discover_tools$"

#: The reported server: one ``${VAR}`` reference and literal settings, of which
#: only the boolean crosses the wire.
_GITLAB = {
    "command": "/usr/local/bin/mcp-gitlab",
    "env": {
        "GITLAB_PERSONAL_ACCESS_TOKEN": _REFERENCE_VALUE,
        "GITLAB_API_URL": _URL_VALUE,
        "GITLAB_DENIED_TOOLS_REGEX": _REGEX_VALUE,
        "GITLAB_READ_ONLY": "true",
    },
}

#: Set in the gateway's own environment and declared by no server.
_UNDECLARED = "KIROCREW_TEST_GATEWAY_ONLY_SETTING"
_UNDECLARED_VALUE = "gateway-only-value-4d1f"

_GITLAB_NOTICE = {"gitlab-mcp": (("GITLAB_API_URL",), ("env",), True)}


def _servers(**servers) -> dict:
    return _spec(mcpServers={"kirocrew-core": {"command": "x"}, **servers})


def _collect(spec: dict, stubbed: frozenset[str] = frozenset()) -> WithheldOut:
    withheld: WithheldOut = {}
    to_client_custom_agent("a", spec, "p", stub_server_names=stubbed, withheld_out=withheld)
    return withheld


class TestTheProjectionRecordsWhatItWithheld:
    def test_the_declared_keys_it_withheld_are_recorded(self):
        """The reported case: the server's own withheld env keys, by name."""
        assert _collect(_servers(**{"gitlab-mcp": _GITLAB})) == {
            "gitlab-mcp": (
                ("GITLAB_API_URL", "GITLAB_DENIED_TOOLS_REGEX", "GITLAB_PERSONAL_ACCESS_TOKEN"),
                ("env",),
                True,
            )
        }

    def test_a_relayed_key_and_an_undeclared_variable_are_not_recorded(self, monkeypatch):
        """Only keys the server declares and the wire lost: the boolean that crossed
        is not named, and neither is a variable only the gateway's environment holds."""
        monkeypatch.setenv(_UNDECLARED, _UNDECLARED_VALUE)
        keys, _fields, _stdio = _collect(_servers(**{"gitlab-mcp": _GITLAB}))["gitlab-mcp"]
        assert "GITLAB_READ_ONLY" not in keys
        assert _UNDECLARED not in keys

    def test_a_server_the_local_launcher_starts_with_its_env_is_not_recorded(
        self, tmp_path, monkeypatch
    ):
        """With a session key the projection hands a stdio server's withheld env to
        the local launcher, which starts it with those values on this machine, so the
        notice must not claim the server starts without them."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        withheld: WithheldOut = {}
        to_client_custom_agent(
            "a",
            _servers(**{"gitlab-mcp": _GITLAB}),
            "p",
            stub_server_names=frozenset(),
            session_key="session-1",
            withheld_out=withheld,
        )
        assert withheld == {}

    def test_headers_are_recorded(self):
        remote = {"type": "http", "url": "https://mcp.example/v1", "headers": {"X": "y"}}
        assert _collect(_servers(remote=remote)) == {"remote": ((), ("headers",), False)}

    def _collect_in_session(self, spec: dict) -> WithheldOut:
        withheld: WithheldOut = {}
        to_client_custom_agent(
            "a",
            spec,
            "p",
            stub_server_names=frozenset(),
            session_key="session-1",
            withheld_out=withheld,
        )
        return withheld

    def test_a_remote_server_the_local_bridge_carries_is_not_recorded(self, tmp_path, monkeypatch):
        """In a session the projection hands a streamable-HTTP server's headers to
        the local bridge, which adds them on this machine, so the notice is recorded
        after that step and must not claim the server starts without them."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        remote = {"type": "http", "url": "https://mcp.example/v1", "headers": {"X": "y"}}
        assert self._collect_in_session(_servers(remote=remote)) == {}

    def test_an_sse_server_the_bridge_cannot_carry_is_still_recorded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        remote = {"type": "sse", "url": "https://mcp.example/sse", "headers": {"X": "y"}}
        assert self._collect_in_session(_servers(remote=remote)) == {
            "remote": ((), ("headers",), False)
        }

    def test_a_server_that_lost_nothing_is_not_recorded(self):
        flags = {"command": "flags-mcp", "env": {"FEATURE_X_ENABLED": "true"}}
        assert _collect(_servers(flags=flags, bare={"command": "bare-mcp"})) == {}

    def test_crews_own_and_stubbed_servers_are_not_recorded(self):
        """A managed server's extra keys are a hand edit, and a stubbed server gets
        its env through the gateway, so neither is the user's missing setting."""
        spec = _servers(**{"gitlab-mcp": _GITLAB})
        spec["mcpServers"]["kirocrew-core"]["env"] = {"STRAY": "value"}
        assert _collect(spec, stubbed=frozenset({"gitlab-mcp"})) == {}

    def test_the_wire_is_unchanged(self, monkeypatch):
        """Collecting changes nothing that is sent: the projection with a collector
        equals the one without, and neither carries the undeclared variable."""
        monkeypatch.setenv(_UNDECLARED, _UNDECLARED_VALUE)
        spec = _servers(**{"gitlab-mcp": _GITLAB})
        plain = to_client_custom_agent("a", spec, "p")
        collected = to_client_custom_agent("a", spec, "p", withheld_out={})
        assert collected == plain
        assert plain["mcpServers"]["gitlab-mcp"]["env"] == {"GITLAB_READ_ONLY": "true"}
        assert _UNDECLARED not in json.dumps(plain)

    def test_build_kas_custom_agents_passes_the_collector_through(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        sse = {"type": "sse", "url": "https://mcp.example/sse", "headers": {"X": "y"}}
        withheld: WithheldOut = {}
        build_kas_custom_agents(tmp_path, "a", _servers(sse=sse), withheld_out=withheld)
        assert set(withheld) == {"sse"}


class TestTheNotice:
    def test_it_is_one_line_naming_the_server_and_the_ui_place(self):
        notice = withheld_credentials_notice(_GITLAB_NOTICE)
        assert notice == (
            "ℹ gitlab-mcp may be missing some of its settings in this chat. To fix it, "
            "turn on Share for it in MCP Servers → Sharing."
        )

    @pytest.mark.parametrize(
        "key", ["AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN"]
    )
    def test_a_server_share_would_not_fix_is_left_to_the_log(self, key):
        """The gateway leaves a shared server unwrapped while its env holds a key it
        does not forward, so Share would change nothing and the chat says nothing,
        exactly when the gateway's own count says so."""
        from kiro_crew.mcp_gateway.rewriter_env import _withheld_env_count

        assert _withheld_env_count({key: "x"}, True) == 1
        assert withheld_credentials_notice({"vendor": ((key,), ("env",), True)}) == ""

    @pytest.mark.parametrize("key", ["GITLAB_API_URL", "GITHUB_TOKEN"])
    def test_env_share_forwards_gets_share_alone(self, key):
        """The gateway forwards these to a shared server, so Share is enough."""
        from kiro_crew.mcp_gateway.rewriter_env import _withheld_env_count

        assert _withheld_env_count({key: "x"}, True) == 0
        notice = withheld_credentials_notice({"vendor": ((key,), ("env",), True)})
        assert notice.startswith("ℹ vendor may be missing")

    def test_forwarding_off_shows_nothing(self, monkeypatch):
        """With ``forward_declared_env`` off the gateway forwards no declared env,
        so Share alone never carries a key; the notice follows that setting."""
        from kiro_crew.mcp_gateway import rewriter

        monkeypatch.setattr(rewriter, "forward_declared_env_enabled", lambda: False)
        assert withheld_credentials_notice(_GITLAB_NOTICE) == ""

    def test_it_names_no_key_and_no_command_line(self):
        """The user has no terminal and may not know config terms, so the notice
        carries no key, no ``env``, no ``secret://`` recipe and no CLI command."""
        notice = withheld_credentials_notice(_GITLAB_NOTICE)
        assert "GITLAB_API_URL" not in notice
        for text in ("secret://", "kirocrew ", "config set", "acp_backend", "env", "\n"):
            assert text not in notice

    def test_a_remote_server_is_left_to_the_log(self):
        """Nothing on Crew's side supplies an ``sse`` server's headers, so the chat
        says nothing; the projection's log line still names it."""
        remote = {"type": "sse", "url": "https://mcp.example/sse", "headers": {"X": "y"}}
        assert withheld_credentials_notice(_collect(_servers(remote=remote))) == ""

    def test_a_server_crew_or_the_edition_ships_is_never_named(self, monkeypatch):
        """An edition-shipped stdio server is not the user's to configure."""
        monkeypatch.setattr(
            agent_mod, "_extra_mcp_servers", lambda: {"edition-tools": {"command": "e"}}
        )
        withheld: WithheldOut = {
            "edition-tools": (("X",), ("env",), True),
            "kirocrew-core": (("X",), ("env",), True),
        }
        assert withheld_credentials_notice(withheld) == ""
        withheld.update(_GITLAB_NOTICE)
        notice = withheld_credentials_notice(withheld)
        assert "gitlab-mcp" in notice
        assert "edition-tools" not in notice and "kirocrew-core" not in notice

    def test_a_not_run_server_keeps_its_line_beside_the_share_line(self):
        """A server KAS will not run keeps its own line, and a server Share would
        fix still gets the one-line pointer to MCP Management under it."""
        withheld: WithheldOut = {
            "plain": ((), (NOT_RUN_INSECURE_URL,), False),
            **_GITLAB_NOTICE,
        }
        not_run, share = withheld_credentials_notice(withheld).split("\n\n")
        assert "• plain" in not_run and "https://" in not_run
        assert "gitlab-mcp" not in not_run
        assert share == withheld_credentials_notice(_GITLAB_NOTICE)
        assert "plain" not in share

    def test_nothing_withheld_is_no_notice(self):
        assert withheld_credentials_notice({}) == ""

    def test_no_value_and_no_undeclared_variable_reaches_it(self, monkeypatch):
        monkeypatch.setenv(_UNDECLARED, _UNDECLARED_VALUE)
        notice = withheld_credentials_notice(_collect(_servers(**{"gitlab-mcp": _GITLAB})))
        assert "gitlab-mcp" in notice
        for value in (_REFERENCE_VALUE, _URL_VALUE, _REGEX_VALUE, _UNDECLARED, _UNDECLARED_VALUE):
            assert value not in notice

    def test_a_credential_shaped_server_name_is_counted_not_printed(self):
        name = "ghp_" + "b" * 36
        notice = withheld_credentials_notice(
            {"vendor": (("X",), ("env",), True), name: (("X",), ("env",), True)}
        )
        assert "b" * 36 not in notice
        assert "vendor, 1 more may be missing" in notice

    def test_a_malformed_env_is_left_to_the_log(self):
        """No key names to judge Share by, so the chat promises nothing."""
        spec = _servers(vendor={"command": "vendor-mcp", "env": ["not", "a", "map"]})
        assert withheld_credentials_notice(_collect(spec)) == ""


@pytest.fixture
def kas_spec_on_disk(monkeypatch, tmp_path):
    """The KAS projection's disk reads, over a real spec that declares the server."""
    monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(
        session_servers_mod, "injection_server_names", lambda overlay, agent: frozenset()
    )
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: tmp_path)
    spec = {"name": "a", "prompt": "p", "tools": [], "mcpServers": {"gitlab-mcp": _GITLAB}}
    (tmp_path / "a.json").write_text(json.dumps(spec), encoding="utf-8")


@pytest.mark.asyncio
async def test_the_session_payload_carries_the_notice(kas_spec_on_disk, tmp_path, monkeypatch):
    """The KAS harness builds the notice from the same projection it sends. The
    stdio server's env reaches it through the launcher even with no session key,
    and the ``sse`` server, which the bridge cannot carry, is left to the log, so
    neither is named."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    sse = {"type": "sse", "url": "https://mcp.example/sse", "headers": {"X": _URL_VALUE}}
    spec = {
        "name": "a",
        "prompt": "p",
        "tools": [],
        "mcpServers": {"gitlab-mcp": _GITLAB, "sse": sse},
    }
    (tmp_path / "a.json").write_text(json.dumps(spec), encoding="utf-8")
    extras = await harness_for(ACP_BACKEND_KAS).session_extras("a", work_dir=str(tmp_path))
    assert "gitlab-mcp" in extras.custom_agents[0]["mcpServers"]
    assert extras.withheld_credentials_notice == ""


@pytest.mark.asyncio
async def test_a_keyed_session_names_no_stdio_server(kas_spec_on_disk, tmp_path, monkeypatch):
    """The ordinary chat path: with a session key the local launcher carries every
    withheld stdio value, so the notice the same build returns is empty."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    extras = await harness_for(ACP_BACKEND_KAS).session_extras(
        "a", work_dir=str(tmp_path), session_key="chat-1"
    )
    assert extras.withheld_credentials_notice == ""
    entry = extras.custom_agents[0]["mcpServers"]["gitlab-mcp"]
    assert "kiro_crew.kas_env_shim" in entry["args"]


@pytest.mark.asyncio
async def test_a_session_that_withholds_nothing_has_no_notice(kas_spec_on_disk, tmp_path):
    spec = {"name": "a", "prompt": "p", "tools": [], "mcpServers": {"bare": {"command": "b"}}}
    (tmp_path / "a.json").write_text(json.dumps(spec), encoding="utf-8")
    extras = await harness_for(ACP_BACKEND_KAS).session_extras("a", work_dir=str(tmp_path))
    assert extras.withheld_credentials_notice == ""


@pytest.mark.asyncio
async def test_mcp_json_servers_the_spec_does_not_list_add_no_notice(
    kas_spec_on_disk, tmp_path, monkeypatch
):
    """The session notice names no mcp.json server, started or excluded: a
    notice about agent-file settings is not one users can act on."""
    monkeypatch.delenv("ASBX_KIRO_MANDATORY_MCPS", raising=False)
    mcp_json = tmp_path / "settings" / "mcp.json"
    mcp_json.parent.mkdir()
    servers = {"extra": {"command": "e"}, "off": {"command": "o", "disabled": True}, "bare": {}}
    mcp_json.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", mcp_json)
    spec = {"name": "a", "prompt": "p", "tools": ["*"], "mcpServers": {"bare": {"command": "b"}}}
    (tmp_path / "a.json").write_text(json.dumps(spec), encoding="utf-8")
    extras = await harness_for(ACP_BACKEND_KAS).session_extras("a", work_dir=str(tmp_path))
    assert extras.withheld_credentials_notice == ""


def test_the_notice_reaches_the_provider():
    notice = withheld_credentials_notice(_GITLAB_NOTICE)
    session = AcpSessionProvider.__new__(AcpSessionProvider)
    session._handle = SimpleNamespace(kas_withheld_credentials_notice=notice)
    assert session.kas_withheld_credentials_notice == notice
    read = AcpProvider.withheld_credentials_notice.fget
    assert read(SimpleNamespace(_client=session)) == notice
    assert read(SimpleNamespace(_client=SimpleNamespace())) == ""


def _surface(monkeypatch, **client):
    rows = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *args, **kw: rows.append(args))
    chat_runner._notice_steer_upgrade(object(), object(), SimpleNamespace(**client))
    return rows


def test_a_new_session_shows_one_notice_row(monkeypatch):
    notice = withheld_credentials_notice(_GITLAB_NOTICE)
    rows = _surface(monkeypatch, withheld_credentials_notice=notice)
    assert len(rows) == 1
    assert rows[0][2] == "notice"
    assert "gitlab-mcp" in rows[0][3]


@pytest.mark.parametrize("notice", ["", None, object()])
def test_no_row_when_nothing_was_withheld(monkeypatch, notice):
    assert _surface(monkeypatch, withheld_credentials_notice=notice) == []
