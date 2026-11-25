"""A confidential MCP client on a KAS engine that takes its secret through
``_kiro/mcp/oauth/setClientSecret`` (announced as ``mcpOAuthClientSecret``).

The request shape is kiro-agent's ``mcp/oauth/client-secret-requests.ts``
(``{sessionId?, serverName, clientSecret}``), checked live against 0.66.135. No
engine runs here: the initialize answers and the runtime are stubs.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from kas_agents_helpers import _spec

from kiro_crew.acp import kas_agents, kas_client_secret
from kiro_crew.acp import kas_engine_features as features
from kiro_crew.acp.harness import SessionExtras
from kiro_crew.acp.kas_agents import to_client_custom_agent
from kiro_crew.acp.kas_client_secret import (
    PreregClient,
    declared_clients,
    deliver_prereg_clients,
)

_VALUE = "placeholder-confidential-value"
_CLIENT_ID = "placeholder-client-id"
_URL = "https://mcp.example.com/mcp"
TAKES_FLAG = frozenset({features.MCP_OAUTH_CLIENT_SECRET})


@pytest.fixture(autouse=True)
def _not_registry_governed(_floor_monkeypatch):
    _floor_monkeypatch.setattr(kas_agents, "_registry_governed", lambda: False)


def _prereg_spec(**oauth: object) -> dict:
    block = {
        "clientId": _CLIENT_ID,
        "clientSecret": _VALUE,
        "redirectUri": "http://localhost:1/cb",
    }
    block.update(oauth)
    return _spec(
        mcpServers={
            "conf": {"url": _URL, "oauth": {k: v for k, v in block.items() if v is not None}}
        }
    )


class TestAdvertised:
    def test_the_flag_is_read_as_a_literal_true(self):
        caps = {"_meta": {"kiro": {"mcpOAuthClientSecret": True}}}
        assert features.advertised(caps) == TAKES_FLAG
        assert (
            features.advertised({"_meta": {"kiro": {"mcpOAuthClientSecret": "true"}}})
            == frozenset()
        )


class TestProjection:
    def test_an_engine_that_takes_it_gets_the_block_without_the_secret(self, caplog):
        collected: dict[str, str] = {}
        with caplog.at_level(logging.DEBUG):
            out = to_client_custom_agent(
                "a", _prereg_spec(), "p", engine_features=TAKES_FLAG, prereg_out=collected
            )
        assert out["mcpServers"]["conf"]["oauth"] == {
            "clientId": _CLIENT_ID,
            "redirectUri": "http://localhost:1/cb",
        }
        assert collected == {"conf": _VALUE}
        assert _VALUE not in repr(out)
        assert _VALUE not in caplog.text

    def test_without_the_flag_the_block_is_withheld_and_nothing_collected(self):
        collected: dict[str, str] = {}
        out = to_client_custom_agent("a", _prereg_spec(), "p", prereg_out=collected)
        assert "oauth" not in out["mcpServers"]["conf"]
        assert collected == {}

    def test_a_projection_that_collects_nothing_withholds_it(self):
        """A sub-agent target is projected with no collector: the request names a
        server of the session, so its block is not sent without a secret."""
        out = to_client_custom_agent("a", _prereg_spec(), "p", engine_features=TAKES_FLAG)
        assert "oauth" not in out["mcpServers"]["conf"]

    @pytest.mark.parametrize(
        "oauth", [{"clientId": None}, {"clientSecret": 12345}], ids=["no-client-id", "non-string"]
    )
    def test_a_secret_the_engine_cannot_key_is_withheld(self, oauth):
        collected: dict[str, str] = {}
        out = to_client_custom_agent(
            "a",
            _prereg_spec(**oauth),
            "p",
            engine_features=TAKES_FLAG,
            prereg_out=collected,
        )
        assert "oauth" not in out["mcpServers"]["conf"]
        assert collected == {}


class TestNotice:
    def test_a_block_the_engine_now_receives_is_not_named(self):
        spec = _spec(
            mcpServers={
                "conf": {"url": _URL, "oauth": {"clientId": _CLIENT_ID, "clientSecret": _VALUE}},
                "meta": {
                    "url": "https://m.example",
                    "oauth": {"clientMetadataUrl": "https://c.example/x"},
                },
            }
        )
        named = kas_agents.spec_keys_without_carrier(spec, withholds_oauth_secret=True)
        assert named == ["mcpServers.conf.oauth", "mcpServers.meta.oauth"]
        named = kas_agents.spec_keys_without_carrier(
            spec, withholds_oauth_secret=True, sends_oauth_secret=True
        )
        assert named == ["mcpServers.meta.oauth"]

    @pytest.mark.parametrize("announced", [True, False])
    def test_the_chat_turn_reads_the_engine_flag(self, monkeypatch, announced):
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
        kiro = {"mcpOAuthClientSecret": True} if announced else {}
        client = SimpleNamespace(
            capabilities=capabilities_for(ACP_BACKEND_KAS),
            cwd="",
            client=SimpleNamespace(
                initialize_result={"agentCapabilities": {"_meta": {"kiro": kiro}}},
                kas_bundle_engine_version="",
                kas_relay_engine_version="",
            ),
        )
        asyncio.run(
            chat_runner._prepare_spec_hooks(
                SimpleNamespace(), SimpleNamespace(), client, "a", is_new=False
            )
        )
        assert seen["sends_oauth_secret"] is announced


class TestDeclaredSecrets:
    def test_only_servers_the_final_entry_declares_with_a_client_id(self):
        batch = [
            {"id": "other", "mcpServers": {"gone": {"url": _URL, "oauth": {"clientId": "x"}}}},
            {
                "id": "a",
                "mcpServers": {
                    "conf": {"url": _URL, "oauth": {"clientId": _CLIENT_ID}},
                    "public": {"url": _URL},
                },
            },
        ]
        out = declared_clients(batch, "a", {"conf": _VALUE, "public": "s2", "gone": "s3"})
        assert out == {"conf": PreregClient(value=_VALUE, url=_URL, client_id=_CLIENT_ID)}

    def test_no_value_in_any_repr(self):
        target = PreregClient(value=_VALUE, url=_URL, client_id=_CLIENT_ID)
        assert _VALUE not in repr(target)
        assert _VALUE not in repr(SessionExtras(mcp_prereg_clients={"conf": target}))


class TestDeliveryOrder:
    """The request names a server of the session's ACTIVE agent, so it goes out
    after ``set_mode``: a re-open parked in the server-less start agent would
    otherwise name a server no active agent declares."""

    @pytest.mark.parametrize(
        "owner",
        [
            "kiro_crew.acp.runtime_session_create:_finish_create_session",
            "kiro_crew.acp.runtime_session_load:load_session",
        ],
    )
    def test_it_is_sent_after_the_mode_is_activated(self, owner):
        import importlib
        import inspect

        module, name = owner.split(":")
        source = inspect.getsource(getattr(importlib.import_module(module), name))
        activated = source.index("await self._activate_mode_bracketed(")
        assert activated < source.index("deliver_prereg_clients(self,")


class _Runtime:
    def __init__(self, fail: Exception | None = None) -> None:
        self._mcp_prereg_sent: dict[tuple[str, str], str] = {}
        self.sent: list[tuple[str, dict]] = []
        self._fail = fail

    async def _send_and_await(self, method, params, timeout=0.0, *, translate=True):
        assert translate is False
        self.sent.append((method, params))
        if self._fail is not None:
            raise self._fail
        return {"success": True}


class TestDeliver:
    _TARGETS = {"conf": PreregClient(value=_VALUE, url=_URL, client_id=_CLIENT_ID)}

    @pytest.mark.asyncio
    async def test_it_names_the_server_of_the_session_and_is_sent_once_per_process(self):
        runtime = _Runtime()
        await deliver_prereg_clients(runtime, "sess-1", self._TARGETS)
        await deliver_prereg_clients(runtime, "sess-2", self._TARGETS)
        assert runtime.sent == [
            (
                "_kiro/mcp/oauth/setClientSecret",
                {"sessionId": "sess-1", "serverName": "conf", "clientSecret": _VALUE},
            )
        ]
        assert _VALUE not in repr(runtime._mcp_prereg_sent)

    @pytest.mark.asyncio
    async def test_a_new_secret_for_the_same_client_is_sent(self):
        runtime = _Runtime()
        await deliver_prereg_clients(runtime, "s", self._TARGETS)
        rotated = {
            "conf": PreregClient(value="placeholder-rotated", url=_URL, client_id=_CLIENT_ID)
        }
        await deliver_prereg_clients(runtime, "s", rotated)
        assert [p["clientSecret"] for _m, p in runtime.sent] == [_VALUE, "placeholder-rotated"]

    @pytest.mark.asyncio
    async def test_a_secret_restored_after_a_change_is_sent_again(self):
        runtime = _Runtime()
        rotated = {
            "conf": PreregClient(value="placeholder-rotated", url=_URL, client_id=_CLIENT_ID)
        }
        for targets in (self._TARGETS, rotated, self._TARGETS):
            await deliver_prereg_clients(runtime, "s", targets)
        assert [p["clientSecret"] for _m, p in runtime.sent] == [
            _VALUE,
            "placeholder-rotated",
            _VALUE,
        ]

    @pytest.mark.asyncio
    async def test_a_refusal_is_logged_without_the_value_and_retried_next_time(self, caplog):
        from kiro_crew.acp.session_handle import AcpRuntimeError

        refusal = AcpRuntimeError(f"RPC error: echoed {_VALUE}")
        refusal.rpc_error = {"code": -32602, "message": f"echoed {_VALUE}"}
        runtime = _Runtime(fail=refusal)
        with caplog.at_level(logging.DEBUG, logger=kas_client_secret.__name__):
            await deliver_prereg_clients(runtime, "s", self._TARGETS)
        assert "refused its confidential OAuth client" in caplog.text and "-32602" in caplog.text
        assert _VALUE not in caplog.text
        assert runtime._mcp_prereg_sent == {}
