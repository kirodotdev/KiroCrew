"""KAS: a new session says which MCP servers start without part of their config.

KAS takes the agent spec over the ACP wire, so the projection withholds every
``env`` value it cannot prove is not a credential, and every ``headers`` block
(:func:`kiro_crew.acp.kas_agents._withhold_credential_fields`). The server is still
declared and still starts, so a credentialed server left unwrapped by the gateway
started with none of its settings and failed with no visible cause: the only trace
was an INFO log line. These tests pin the notice that now names each such server
and the key names it lost -- never a value, and never a variable the server does
not declare -- from the projection to the row a new chat shows.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from kas_agents_helpers import _spec

from kiro_crew import agent as agent_mod
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.kas_agents import WithheldOut, build_kas_custom_agents, to_client_custom_agent
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

_GITLAB_NOTICE = {"gitlab-mcp": (("GITLAB_API_URL",), ("env",))}


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
            )
        }

    def test_a_relayed_key_and_an_undeclared_variable_are_not_recorded(self, monkeypatch):
        """Only keys the server declares and the wire lost: the boolean that crossed
        is not named, and neither is a variable only the gateway's environment holds."""
        monkeypatch.setenv(_UNDECLARED, _UNDECLARED_VALUE)
        keys, _fields = _collect(_servers(**{"gitlab-mcp": _GITLAB}))["gitlab-mcp"]
        assert "GITLAB_READ_ONLY" not in keys
        assert _UNDECLARED not in keys

    def test_headers_are_recorded(self):
        remote = {"type": "http", "url": "https://mcp.example/v1", "headers": {"X": "y"}}
        assert _collect(_servers(remote=remote)) == {"remote": ((), ("headers",))}

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

    def test_build_kas_custom_agents_passes_the_collector_through(self, tmp_path):
        withheld: WithheldOut = {}
        build_kas_custom_agents(
            tmp_path, "a", _servers(**{"gitlab-mcp": _GITLAB}), withheld_out=withheld
        )
        assert set(withheld) == {"gitlab-mcp"}


class TestTheNotice:
    def test_it_names_the_server_and_keys_and_the_way_out(self):
        notice = withheld_credentials_notice(
            {
                "gitlab-mcp": (("GITLAB_API_URL", "GITLAB_DENIED_TOOLS_REGEX"), ("env",)),
                "remote": ((), ("headers",)),
            }
        )
        assert "gitlab-mcp: env GITLAB_API_URL, GITLAB_DENIED_TOOLS_REGEX" in notice
        assert "remote: headers" in notice
        assert "secret://" in notice and "MCP Management" in notice
        # A stored ``kiro`` backend loads as KAS once kiro-cli serves its v3 engine
        # (``agent_sdk.backends.kiro_cli_pick_backend``), so it is no way out.
        assert "acp_backend" not in notice

    def test_nothing_withheld_is_no_notice(self):
        assert withheld_credentials_notice({}) == ""

    def test_no_value_and_no_undeclared_variable_reaches_it(self, monkeypatch):
        monkeypatch.setenv(_UNDECLARED, _UNDECLARED_VALUE)
        notice = withheld_credentials_notice(_collect(_servers(**{"gitlab-mcp": _GITLAB})))
        assert "gitlab-mcp" in notice
        for value in (_REFERENCE_VALUE, _URL_VALUE, _REGEX_VALUE, _UNDECLARED, _UNDECLARED_VALUE):
            assert value not in notice

    def test_a_credential_shaped_name_is_counted_not_printed(self):
        key = "ghp_" + "a" * 36
        notice = withheld_credentials_notice(
            {"vendor": ((key,), ("env",)), "ghp_" + "b" * 36: (("X",), ("env",))}
        )
        assert key not in notice and "b" * 36 not in notice
        assert "vendor: env 1 unnamed" in notice
        assert "(unnamed): env X" in notice

    def test_a_malformed_env_is_named_whole(self):
        spec = _servers(vendor={"command": "vendor-mcp", "env": ["not", "a", "map"]})
        assert "vendor: its env" in withheld_credentials_notice(_collect(spec))

    def test_an_env_key_named_headers_is_an_env_key(self):
        spec = _servers(vendor={"command": "vendor-mcp", "env": {"headers": "x-y"}})
        assert "vendor: env headers" in withheld_credentials_notice(_collect(spec))


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
async def test_the_session_payload_carries_the_notice(kas_spec_on_disk, tmp_path):
    """The KAS harness builds the notice from the same projection it sends."""
    extras = await harness_for(ACP_BACKEND_KAS).session_extras("a", work_dir=str(tmp_path))
    assert "gitlab-mcp" in extras.custom_agents[0]["mcpServers"]
    assert "gitlab-mcp: env GITLAB_API_URL" in extras.withheld_credentials_notice
    assert _URL_VALUE not in extras.withheld_credentials_notice


@pytest.mark.asyncio
async def test_a_session_that_withholds_nothing_has_no_notice(kas_spec_on_disk, tmp_path):
    spec = {"name": "a", "prompt": "p", "tools": [], "mcpServers": {"bare": {"command": "b"}}}
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
