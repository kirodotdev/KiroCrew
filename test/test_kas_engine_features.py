"""What the KAS projection sends for a remote server's ``oauth`` block (always,
whatever the engine announces), and for ``mcpRegistryPointers`` (only when the
engine announces it in ``initialize``).

The initialize answers here are stubs shaped like the engine's; no engine runs.
"""

from __future__ import annotations

import logging

import pytest
from kas_agents_helpers import _spec

from kiro_crew.acp import kas_agents
from kiro_crew.acp import kas_engine_features as features
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.kas_agents import to_client_custom_agent

_SECRET = "placeholder-client-secret-value"
_CLIENT_ID = "placeholder-client-id"
# A managed server with no spec_gate: a gated one is withheld from the projection
# while its gate is closed, as it is on Linux and with Computer Use off.
_MANAGED = "kirocrew-core"


def _initialize(**kiro: object) -> dict:
    """A stub ``initialize`` result carrying *kiro* under ``agentCapabilities._meta``."""
    return {
        "protocolVersion": 1,
        "agentCapabilities": {"loadSession": True, "_meta": {"kiro": kiro}},
    }


def _caps(**kiro: object) -> dict:
    return _initialize(**kiro)["agentCapabilities"]


OAUTH = features.advertised(_caps(mcpServerOAuth=True))
POINTERS = features.advertised(_caps(mcpRegistryPointers=True))


@pytest.fixture(autouse=True)
def _not_registry_governed(_floor_monkeypatch):
    _floor_monkeypatch.setattr(kas_agents, "_registry_governed", lambda: False)


def _remote(**oauth_extra: object) -> dict:
    oauth = {
        "clientId": _CLIENT_ID,
        "redirectUri": "http://localhost:8080/callback",
        "oauthScopes": ["channels:history"],
    }
    oauth.update(oauth_extra)
    return _spec(
        mcpServers={
            "remote": {"type": "http", "url": "https://mcp.example.com/mcp", "oauth": oauth}
        }
    )


class TestAdvertised:
    @pytest.mark.parametrize(
        "caps",
        [
            None,
            {},
            {"_meta": None},
            {"_meta": {"kiro": None}},
            _caps(mcpServerOAuth="true", mcpRegistryPointers=1),
            _caps(somethingElse=True),
        ],
    )
    def test_only_a_literal_true_counts(self, caps):
        assert features.advertised(caps) == frozenset()

    def test_both_flags_are_read(self):
        caps = _caps(mcpServerOAuth=True, mcpRegistryPointers=True)
        assert features.advertised(caps) == {
            features.MCP_SERVER_OAUTH,
            features.MCP_REGISTRY_POINTERS,
        }

    def test_a_handle_is_read_through_its_initialize_result(self):
        class Handle:
            initialize_result = _initialize(mcpServerOAuth=True)

        assert features.advertised_by(Handle()) == OAUTH
        assert features.advertised_by(object()) == frozenset()

    def test_a_real_provider_is_read_through_its_client(self):
        """The dashboard hands the notice an ``AcpProvider``, whose client holds the answer."""
        from types import SimpleNamespace

        from kiro_crew.providers.acp import AcpProvider

        provider = AcpProvider.__new__(AcpProvider)  # no spawn
        provider._client = SimpleNamespace(initialize_result=_initialize(mcpServerOAuth=True))
        assert features.advertised_by(provider) == OAUTH
        provider._client = SimpleNamespace(initialize_result=_initialize())
        assert features.advertised_by(provider) == frozenset()


class TestOAuth:
    """Sent on every engine: 0.66.104 reads it, older ones drop it and still start
    the server and the agent (measured on 0.66.33 and 0.66.91)."""

    @pytest.mark.parametrize("flags", [frozenset(), OAUTH, POINTERS])
    def test_the_accepted_keys_are_sent_whatever_the_engine_announces(self, flags):
        out = to_client_custom_agent("a", _remote(), "p", engine_features=flags)
        entry = out["mcpServers"]["remote"]
        assert entry["oauth"] == {
            "clientId": _CLIENT_ID,
            "redirectUri": "http://localhost:8080/callback",
        }
        assert entry["oauthScopes"] == ["channels:history"]

    def test_client_metadata_url_and_unknown_keys_are_not_sent(self):
        """A metadata url without a client id would re-key the engine's grant."""
        out = to_client_custom_agent(
            "a", _remote(clientMetadataUrl="https://app.example/client.json", extra="x"), "p"
        )
        oauth = out["mcpServers"]["remote"]["oauth"]
        assert "clientMetadataUrl" not in oauth
        assert "extra" not in oauth

    def test_a_metadata_url_only_block_is_not_sent(self):
        spec = _spec(
            mcpServers={
                "s": {
                    "url": "https://mcp.example.com",
                    "oauth": {"clientMetadataUrl": "https://app.example/client.json"},
                }
            }
        )
        out = to_client_custom_agent("a", spec, "p")
        assert "oauth" not in out["mcpServers"]["s"]

    def test_a_relay_session_names_what_the_projection_does_not_send(self):
        """The notice and the projection read one rule: a server is named exactly
        when its projected entry carries no ``oauth`` and no nested scopes."""
        spec = _spec(
            mcpServers={
                "metadata-only": {
                    "url": "https://mcp.example.com",
                    "oauth": {"clientMetadataUrl": "https://app.example/client.json"},
                },
                "confidential": {
                    "url": "https://mcp2.example.com",
                    "oauth": {"clientId": _CLIENT_ID, "clientSecret": _SECRET},
                },
                "public": {"url": "https://mcp3.example.com", "oauth": {"clientId": _CLIENT_ID}},
            }
        )
        out = to_client_custom_agent("a", spec, "p")["mcpServers"]
        assert "oauth" not in out["metadata-only"]
        assert "oauth" not in out["confidential"]
        assert out["public"]["oauth"] == {"clientId": _CLIENT_ID}
        lost = kas_agents.spec_keys_without_carrier(
            spec, discards_mcp_oauth=False, withholds_oauth_secret=True
        )
        assert lost == ["mcpServers.confidential.oauth", "mcpServers.metadata-only.oauth"]

    @pytest.mark.parametrize("flags", [frozenset(), OAUTH])
    def test_a_confidential_client_is_withheld_whole_and_its_secret_never_logged(
        self, caplog, flags
    ):
        with caplog.at_level(logging.DEBUG):
            out = to_client_custom_agent(
                "a", _remote(clientSecret=_SECRET), "p", engine_features=flags
            )
        entry = out["mcpServers"]["remote"]
        assert "oauth" not in entry
        assert _SECRET not in repr(out)
        assert _SECRET not in caplog.text and _CLIENT_ID not in caplog.text
        assert "names a clientSecret" in caplog.text

    def test_a_secret_only_block_is_not_sent(self):
        spec = _spec(
            mcpServers={"s": {"url": "https://x.example", "oauth": {"clientSecret": _SECRET}}}
        )
        out = to_client_custom_agent("a", spec, "p")
        assert "oauth" not in out["mcpServers"]["s"]
        assert _SECRET not in repr(out)


class TestEngineDiscardsOAuth:
    @pytest.mark.parametrize("version", ["0.66.33", "0.66.91", "0.66.103", "0.66.103-next.9"])
    def test_a_known_older_engine_drops_it(self, version):
        assert features.engine_discards_oauth(version) is True

    @pytest.mark.parametrize("version", ["0.66.104", "0.66.104-next.4", "0.67.0", "", None, "x"])
    def test_a_newer_or_unknown_engine_is_not_claimed_to_drop_it(self, version):
        assert features.engine_discards_oauth(version) is False


class TestRegistryPointers:
    @staticmethod
    def _governed(monkeypatch) -> None:
        monkeypatch.setattr(kas_agents, "_registry_governed", lambda: True)

    @staticmethod
    def _marked() -> dict:
        return _spec(
            mcpServers={
                "governed": {"command": "x", "type": "registry", "env": {"K": "v"}},
                "plain": {"command": "y"},
            }
        )

    def test_without_the_flag_a_marked_entry_is_withheld_as_before(self):
        out = to_client_custom_agent("a", self._marked(), "p")
        assert out["mcpServers"] == {"plain": {"command": "y"}}

    def test_with_the_flag_a_marked_entry_is_sent_as_a_pointer_with_its_command(self):
        out = to_client_custom_agent("a", self._marked(), "p", engine_features=POINTERS)
        assert out["mcpServers"]["governed"] == {"type": "registry", "command": "x"}
        assert out["mcpServers"]["plain"] == {"command": "y"}

    def test_the_pointer_keeps_command_and_args_and_never_env(self):
        spec = _spec(
            mcpServers={
                "governed": {
                    "type": "registry",
                    "command": "srv",
                    "args": ["--port", "1"],
                    "env": {"K": "v"},
                    "headers": {"H": "v"},
                }
            }
        )
        out = to_client_custom_agent("a", spec, "p", engine_features=POINTERS)
        assert out["mcpServers"]["governed"] == {
            "type": "registry",
            "command": "srv",
            "args": ["--port", "1"],
        }

    def test_registry_mode_still_withholds_an_unmarked_third_party_entry(self, monkeypatch):
        self._governed(monkeypatch)
        out = to_client_custom_agent("a", self._marked(), "p", engine_features=POINTERS)
        assert out["mcpServers"] == {"governed": {"type": "registry", "command": "x"}}


class TestCrewsOwnServers:
    """Crew's own servers are never pointers: a pointer has no slot for the session
    token the session-level hoist stamps, so they go out exactly as without the flag."""

    @staticmethod
    def _spec() -> dict:
        return _spec(mcpServers={_MANAGED: {"command": "kirocrew", "args": ["mcp"]}})

    @pytest.mark.parametrize("governed", [False, True])
    def test_they_are_never_marked_and_stay_in_the_hoisted_session_array(
        self, monkeypatch, governed
    ):
        monkeypatch.setattr(kas_agents, "_registry_governed", lambda: governed)
        out = to_client_custom_agent("a", self._spec(), "p", engine_features=POINTERS)
        assert (
            out["mcpServers"][_MANAGED]
            == to_client_custom_agent("a", self._spec(), "p")["mcpServers"][_MANAGED]
        )
        assert "type" not in out["mcpServers"][_MANAGED]
        agents, array = kas_agents.hoist_servers_for_session([out], "a", [], session_token="t")
        assert _MANAGED in [e["name"] for e in array]
        assert _MANAGED not in agents[0].get("mcpServers", {})

    def test_registry_mode_keeps_the_warning_and_the_notice(self, monkeypatch, caplog):
        monkeypatch.setattr(kas_agents, "_registry_governed", lambda: True)
        withheld: dict = {}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent(
                "a", self._spec(), "p", engine_features=POINTERS, withheld_out=withheld
            )
        assert "no slot" in caplog.text
        assert _MANAGED in withheld

    def test_the_session_array_is_left_alone(self):
        array = [{"name": _MANAGED, "command": "kirocrew", "args": ["mcp"], "env": []}]
        caps = _caps(mcpRegistryPointers=True)
        assert KasHarness().session_mcp_servers(array, agent_capabilities=caps) is array


class TestRuntimeSeam:
    """The runtime hands the harness exactly what its own handshake announced."""

    @staticmethod
    async def _seen(caps: dict) -> frozenset[str]:
        from types import SimpleNamespace

        from kiro_crew.acp import runtime_session_setup
        from kiro_crew.acp.harness import SessionExtras

        seen: dict = {}

        async def session_extras(agent, **kw):
            seen.update(kw)
            return SessionExtras()

        runtime = SimpleNamespace(
            _agent_capabilities=caps,
            _harness=SimpleNamespace(session_extras=session_extras),
            _mcp_gateway_overlay=None,
            _work_dir=None,
            _refuse_if_loader_unreachable=lambda *_a: None,
        )
        await runtime_session_setup._kas_custom_agents(runtime, "a")
        return seen["engine_features"]

    @pytest.mark.asyncio
    async def test_the_handshake_flags_reach_session_extras(self):
        assert await self._seen(_caps(mcpServerOAuth=True)) == OAUTH

    @pytest.mark.asyncio
    async def test_an_engine_that_announces_nothing_hands_nothing(self):
        assert await self._seen({}) == frozenset()


class TestOAuthNotice:
    """The "sign-in will not work here" notice shows only where Crew KNOWS the
    engine drops the block: its own bundle below 0.66.104. The relay names no
    engine version, so it shows nothing there."""

    @staticmethod
    def _discards(monkeypatch, bundle_engine_version: str) -> bool:
        import asyncio
        from types import SimpleNamespace

        from kiro_crew.acp.types import ACP_BACKEND_KAS
        from kiro_crew.agent_sdk import spec_hooks
        from kiro_crew.agent_sdk.capabilities import capabilities_for
        from kiro_crew.dashboard import chat_runner

        seen: dict = {}

        def lost(_spec, **kw):
            seen.update(kw)
            return []

        monkeypatch.setattr(spec_hooks, "_agent_spec_with_origin", lambda *_a, **_k: ({}, "user"))
        monkeypatch.setattr(kas_agents, "spec_keys_without_carrier", lost)
        # Shaped like the provider a dashboard turn hands in: the session's facts
        # are on its ``client``, not on the provider itself.
        client = SimpleNamespace(
            capabilities=capabilities_for(ACP_BACKEND_KAS),
            cwd="",
            client=SimpleNamespace(
                initialize_result=_initialize(),
                kas_bundle_engine_version=bundle_engine_version,
            ),
        )
        asyncio.run(
            chat_runner._prepare_spec_hooks(
                SimpleNamespace(), SimpleNamespace(), client, "a", is_new=False
            )
        )
        assert seen["withholds_oauth_secret"] is True
        return seen["discards_mcp_oauth"]

    def test_an_older_bundled_engine_gets_the_notice(self, monkeypatch):
        assert self._discards(monkeypatch, "0.66.33") is True

    def test_the_relay_gets_no_notice(self, monkeypatch):
        assert self._discards(monkeypatch, "") is False

    def test_a_bundled_engine_that_keeps_oauth_gets_no_notice(self, monkeypatch):
        assert self._discards(monkeypatch, "0.66.104") is False


class TestBundleEngineVersionReachesTheHandle:
    """The harness records its bundle's version on each session handle."""

    def test_the_bundle_version_is_recorded_and_the_relay_leaves_it_empty(self):
        from types import SimpleNamespace

        harness = KasHarness()
        handle = SimpleNamespace()
        harness.bundle_engine_version = "0.66.33"
        harness.record_session_projection(handle, [], "")
        assert handle.kas_bundle_engine_version == "0.66.33"
        harness.bundle_engine_version = ""
        harness.record_session_projection(handle, [], "")
        assert handle.kas_bundle_engine_version == ""
