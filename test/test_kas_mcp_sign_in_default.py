"""KAS keeps remote MCP sign-ins by default, except on a governed host.

A regular install keeps each remote MCP server's OAuth sign-in in Crew's grant
vault unless the operator sets ``agent.kas_persist_mcp_sign_in`` false, so a later
KAS process (a new chat, a subagent, a cron) reuses it instead of asking again. A
governed install -- one with a security policy installed, or one declaring
``agent.mcp_registry_mode`` -- keeps sign-ins only when
the admin's security policy sets ``mcp_persist_sign_in``: ``config.json``
alone never widens it, and a false there still turns it off. The session tracker
also names a completed sign-in and a sign-in the engine drops in the gateway log,
with nothing but the server name and the session id.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from kiro_crew.acp import kas_secret_storage as kss
from kiro_crew.acp import runtime_spawn_launch
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_KAS_MCP_STATUS, JsonRpcMessage
from kiro_crew.platform import context as platform_context
from kiro_crew.platform.governance import PlatformCompositionError
from kiro_crew.platform.governance import _intersect_ceilings as intersect_ceilings
from kiro_crew.platform.governance import parse_policy, parse_profile

_FIELD = "mcp_persist_sign_in"


def _doc(**extra: object) -> dict:
    return {"version": 1, "boot": {}, **extra}


# ── the policy field ────────────────────────────────────────────────────


class TestPolicyField:
    def test_absent_is_off(self):
        assert parse_policy(_doc()).mcp_persist_sign_in is False

    @pytest.mark.parametrize("value", [True, False])
    def test_a_json_boolean_is_taken_as_written(self, value):
        assert parse_policy(_doc(**{_FIELD: value})).mcp_persist_sign_in is value

    @pytest.mark.parametrize("bad", ["true", 1, 0, [], {}])
    def test_any_other_value_fails_the_load(self, bad):
        with pytest.raises(PlatformCompositionError, match=_FIELD):
            parse_policy(_doc(**{_FIELD: bad}))

    def test_a_profile_cannot_carry_it(self):
        with pytest.raises(PlatformCompositionError, match=_FIELD):
            parse_profile({"name": "p", _FIELD: True})

    def test_a_lower_tier_cannot_supply_it(self):
        off = parse_policy(_doc())
        on = parse_policy(_doc(**{_FIELD: True}))
        assert intersect_ceilings(off, on).mcp_persist_sign_in is False
        assert intersect_ceilings(on, off).mcp_persist_sign_in is True


# ── the spawn decision ──────────────────────────────────────────────────


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A config dir and an installed ceiling the decision reads, both per test."""
    from kiro_crew.config import loader as L

    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    state = SimpleNamespace(governance=None)
    monkeypatch.setattr(platform_context, "current_context", lambda: state)

    def configure(agent: dict | None = None, policy: dict | None = None) -> None:
        if agent is not None:
            (tmp_path / "config.json").write_text(json.dumps({"agent": agent}))
        state.governance = parse_policy(_doc(**policy)) if policy is not None else None

    return configure


def test_a_regular_install_keeps_sign_ins_by_default(host):
    host()
    assert runtime_spawn_launch._mcp_sign_in_storage_decision() is True


def test_an_explicit_false_turns_a_regular_install_off(host):
    host(agent={"kas_persist_mcp_sign_in": False})
    assert runtime_spawn_launch._mcp_sign_in_storage_decision() is False


def test_a_governed_host_stays_in_memory_by_default_and_says_why(host, caplog):
    host(agent={"mcp_registry_mode": True})
    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        assert runtime_spawn_launch._mcp_sign_in_storage_decision() is False
    assert _FIELD in caplog.text


def test_a_config_true_alone_does_not_widen_a_governed_host(host):
    host(agent={"mcp_registry_mode": True, "kas_persist_mcp_sign_in": True})
    assert runtime_spawn_launch._mcp_sign_in_storage_decision() is False


def test_the_admin_policy_opts_a_governed_host_in(host):
    host(agent={"mcp_registry_mode": True}, policy={_FIELD: True})
    assert runtime_spawn_launch._mcp_sign_in_storage_decision() is True


def test_an_explicit_false_wins_over_the_admin_opt_in(host):
    host(
        agent={"mcp_registry_mode": True, "kas_persist_mcp_sign_in": False},
        policy={_FIELD: True},
    )
    assert runtime_spawn_launch._mcp_sign_in_storage_decision() is False


def test_a_host_with_a_security_policy_is_governed_without_registry_mode(host, caplog):
    host(policy={})
    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.runtime"):
        assert runtime_spawn_launch._mcp_sign_in_storage_decision() is False
    assert _FIELD in caplog.text


def test_the_admin_policy_opts_a_policy_governed_host_in(host):
    host(policy={_FIELD: True})
    assert runtime_spawn_launch._mcp_sign_in_storage_decision() is True


def test_an_explicit_false_wins_on_a_policy_governed_host(host):
    host(agent={"kas_persist_mcp_sign_in": False}, policy={_FIELD: True})
    assert runtime_spawn_launch._mcp_sign_in_storage_decision() is False


@pytest.mark.parametrize(
    "text",
    ['{"agent": {"mcp_registry_mode": true', "[]"],
    ids=["unparseable", "not-an-object"],
)
def test_a_degraded_config_read_keeps_sign_ins_in_memory(host, tmp_path, text):
    # A degraded read loads as field defaults ("keep", "not governed"), which a
    # governed host's own file may contradict, so it decides nothing.
    host()
    (tmp_path / "config.json").write_text(text)
    assert kss.sign_in_settings() is None
    assert runtime_spawn_launch._mcp_sign_in_storage_decision() is False


def test_a_config_read_that_raises_keeps_sign_ins_in_memory(host, monkeypatch):
    from kiro_crew.config.loader import KiroCrewConfig

    host()

    def broken(*_a, **_k):
        raise OSError("config plane down")

    monkeypatch.setattr(KiroCrewConfig, "load", broken)
    assert runtime_spawn_launch._mcp_sign_in_storage_decision() is False


def test_an_unreadable_policy_keeps_a_governed_host_in_memory(host, monkeypatch):
    # No registry mode either: a ceiling that cannot be read may still be one.
    host()

    def broken():
        raise RuntimeError("no context")

    monkeypatch.setattr(platform_context, "current_context", broken)
    assert kss.policy_permit() == kss.PolicyPermit(installed=True, permits=False)
    assert runtime_spawn_launch._mcp_sign_in_storage_decision() is False


@pytest.mark.asyncio
async def test_a_regular_kas_handshake_declares_secret_storage_by_default(host):
    host()
    runtime = AcpRuntime(acp_backend=ACP_BACKEND_KAS)
    runtime._tool_search = None
    caps = await runtime_spawn_launch._handshake_client_capabilities(runtime)
    assert caps["_meta"]["kiro"].get("secretStorage") is True
    assert runtime._mcp_secret_server is not None


@pytest.mark.asyncio
async def test_a_governed_kas_handshake_declares_nothing_without_the_policy(host):
    host(agent={"mcp_registry_mode": True})
    runtime = AcpRuntime(acp_backend=ACP_BACKEND_KAS)
    runtime._tool_search = None
    caps = await runtime_spawn_launch._handshake_client_capabilities(runtime)
    assert "secretStorage" not in caps["_meta"]["kiro"]
    assert runtime._mcp_secret_server is None


# ── the sign-in log lines ───────────────────────────────────────────────

_SECRET_URL = "https://auth.example.test/authorize?client_id=c&code_challenge=SECRETCHALLENGE"


def _handle() -> AcpSessionHandle:
    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._session_id = "s1"
    handle._runtime = SimpleNamespace(begin_mcp_sign_in=lambda *_a: False)
    handle._mcp_sign_in_needed = set()
    handle._mcp_sign_in_wanted = set()
    handle._mcp_sign_in_ask = ""
    handle._mcp_sign_in_asked = set()
    handle._mcp_sign_in_completed = set()
    handle._mcp_sign_in_dropped = 0
    handle._mcp_connected_seen = set()
    handle._mcp_reattempt_waiting = set()
    handle._mcp_reattempt_counts = {}
    handle._oauth_emitted_servers = set()
    return handle


def _status(**server: object) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS,
        params={"sessionId": "s1", "servers": [{"name": "atlassian", **server}]},
    )


def _unauthorized() -> JsonRpcMessage:
    return _status(
        status="failed",
        failedAuthorization=True,
        authorizationUrl=_SECRET_URL,
        errorMessage="401 Bearer SECRETTOKEN rejected",
    )


def test_a_completed_sign_in_is_logged_at_info(caplog):
    handle = _handle()
    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.session_handle"):
        handle._note_mcp_sign_in_status(_unauthorized(), offer=False)
        handle._note_mcp_sign_in_status(_status(status="connected"), offer=False)
    lines = [r for r in caplog.records if "completed" in r.getMessage()]
    assert [r.levelno for r in lines] == [logging.INFO]
    assert "atlassian" in lines[0].getMessage()
    assert "SECRET" not in caplog.text


def test_an_ordinary_connected_server_logs_no_completion(caplog):
    handle = _handle()
    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.session_handle"):
        handle._note_mcp_sign_in_status(_status(status="connected"), offer=False)
    assert "completed" not in caplog.text


def test_a_sign_in_the_engine_drops_is_logged_once_as_a_warning(caplog):
    handle = _handle()
    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.session_handle"):
        handle._note_mcp_sign_in_status(_status(status="connected"), offer=False)
        handle._note_mcp_sign_in_status(_unauthorized(), offer=False)
        handle._note_mcp_sign_in_status(_unauthorized(), offer=False)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "atlassian" in warnings[0].getMessage()
    assert "SECRET" not in caplog.text
    assert "auth.example.test" not in caplog.text


def test_a_server_that_never_connected_logs_no_drop(caplog):
    handle = _handle()
    with caplog.at_level(logging.INFO, logger="kiro_crew.acp.session_handle"):
        handle._note_mcp_sign_in_status(_unauthorized(), offer=False)
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
