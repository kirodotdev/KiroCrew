"""A session confined beyond the shared backends' standard tier runs its MCP servers per chat.

A shared backend runs in the global ``standard`` sandbox, so a strict (or ``cc``)
session, or an enforced harness under Kiro Crew's credential mask, is marked through
its environment and each of its stubs runs the server locally, refusing an
unresolved ``secret://`` value instead of launching with it.
"""

from __future__ import annotations

import pytest

from kiro_crew.mcp_gateway import session_confinement


@pytest.fixture
def no_floor(monkeypatch):
    """No governed ``sandbox.min_level`` floor, and a host where the mask applies."""
    import kiro_crew.sandbox as sandbox_facade
    import kiro_crew.sandbox_spawn as spawn

    monkeypatch.setattr(sandbox_facade, "_governance_sandbox_floor", lambda: None)
    monkeypatch.setattr(spawn, "credential_mask_applies", lambda mode: mode != "off")


@pytest.mark.parametrize(
    "backend, tier, shared",
    [
        ("kas", "auto", True),
        ("kas", "standard", True),
        ("kas", "off", True),
        ("", "auto", True),
        ("kas", "strict", False),
        ("kas", "cc", False),
        # An enforced harness runs under the credential mask, which a shared
        # standard-tier backend does not carry.
        ("codex", "auto", False),
        # With the sandbox off nothing is masked, so nothing is fenced more.
        ("codex", "off", True),
    ],
)
def test_who_uses_shared_backends(no_floor, backend, tier, shared):
    assert session_confinement.uses_shared_backends(backend, tier) is shared


def test_a_governed_strict_floor_keeps_an_auto_session_per_chat(monkeypatch):
    import kiro_crew.sandbox as sandbox_facade

    monkeypatch.setattr(sandbox_facade, "_governance_sandbox_floor", lambda: "strict")

    assert session_confinement.uses_shared_backends("kas", "auto") is False


def _factory_kwargs(monkeypatch, sandbox: str, backend: str = "") -> dict:
    from kiro_crew.agent_sdk.drivers import acp
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.agent.sandbox = sandbox
    cfg.agent.acp_backend = backend
    cfg.mcp_gateway.stub_servers = ["weather"]
    cfg.mcp_gateway.overlay_dir = "/run/overlay"
    cfg.mcp_gateway.socket_path = "/run/gw.sock"
    monkeypatch.setattr(acp, "build_acp_provider", lambda **kwargs: kwargs)
    return cfg.create_provider_factory()(session_key="dashboard:confinement")


def test_the_factory_leaves_a_standard_session_unmarked(no_floor, monkeypatch):
    kwargs = _factory_kwargs(monkeypatch, "auto")

    assert session_confinement.PER_CHAT_ENV not in (kwargs["extra_env"] or {})
    assert kwargs["mcp_gateway_overlay"] == "/run/overlay"


def test_the_factory_marks_a_strict_session_per_chat(no_floor, monkeypatch):
    kwargs = _factory_kwargs(monkeypatch, "strict")

    assert kwargs["sandbox_mode"] == "strict"
    assert kwargs["extra_env"][session_confinement.PER_CHAT_ENV] == "1"
    # The overlay stays: its stubs are what refuse an unresolved secret:// value.
    assert kwargs["mcp_gateway_overlay"] == "/run/overlay"


def _stub_argv(tmp_path, declared: dict) -> list[str]:
    import json

    env_file = tmp_path / "env.json"
    env_file.write_text(json.dumps(declared), encoding="utf-8")
    return [
        "--server",
        "atl",
        "--agent",
        "agent-a",
        "--target-command",
        "true",
        "--work-dir",
        str(tmp_path),
        "--env-file",
        str(env_file),
    ]


@pytest.fixture
def per_chat_stub(monkeypatch):
    """A stub in a marked session, with every gateway contact and the exec recorded."""
    from kiro_crew.mcp_gateway import stub as stub_mod

    def _no_gateway(*_a, **_kw):
        raise AssertionError("a per-chat session's stub contacted the gateway")

    execs: list[list[str]] = []

    def _exec(path, argv, env):
        execs.append(list(argv))
        raise SystemExit(0)

    monkeypatch.setenv(session_confinement.PER_CHAT_ENV, "1")
    monkeypatch.setattr(stub_mod, "build_register_payload", _no_gateway)
    monkeypatch.setattr(stub_mod, "handshake", _no_gateway)
    monkeypatch.setattr(stub_mod.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(stub_mod.os, "execvpe", _exec)
    return stub_mod, execs


def test_a_marked_session_runs_the_server_locally_and_never_attaches(per_chat_stub, tmp_path):
    import asyncio

    stub_mod, execs = per_chat_stub

    with pytest.raises(SystemExit):
        asyncio.run(stub_mod._amain(_stub_argv(tmp_path, {"CITY": "Paris"})))

    assert execs == [["true"]]


def test_a_marked_session_never_launches_with_a_literal_secret_reference(per_chat_stub, tmp_path):
    import asyncio

    stub_mod, execs = per_chat_stub

    with pytest.raises(SystemExit) as exc:
        asyncio.run(
            stub_mod._amain(_stub_argv(tmp_path, {"API_TOKEN": "secret://ATLASSIAN_TOKEN"}))
        )

    assert exc.value.code == stub_mod._EXIT_UNRESOLVED_SECRET
    assert execs == []


def _stub_element(token: str, *, per_chat: bool) -> dict:
    from kiro_crew.mcp_gateway import STUB_MODULE
    from kiro_crew.mcp_gateway.session_servers import attach_stub_session_token

    element = {
        "name": "kirocrew-core",
        "command": "/usr/bin/python3",
        "args": ["-m", STUB_MODULE, "--server", "kirocrew-core"],
        "env": [],
    }
    [shaped] = attach_stub_session_token([element], token, per_chat=per_chat)
    return shaped


def test_a_confined_sessions_stub_element_carries_the_mark_itself():
    """The element's own env, which a harness that clears its children's env still passes."""
    env = _stub_element("tok", per_chat=True)["env"]

    assert {"name": session_confinement.PER_CHAT_ENV, "value": "1"} in env
    assert any(e["value"] == "tok" for e in env)
    assert session_confinement.PER_CHAT_ENV not in str(_stub_element("tok", per_chat=False))


def test_codex_admits_the_marked_stub_element():
    from kiro_crew.providers.mirrors import codex

    marked = _stub_element("tok", per_chat=True)

    assert codex._is_stub_for(marked, "kirocrew-core", "tok")
    # Anything else beside the token still fails the shape check.
    odd = {**marked, "env": [*marked["env"][:1], {"name": "OTHER", "value": "1"}]}
    assert not codex._is_stub_for(odd, "kirocrew-core", "tok")


def _env_from_element(monkeypatch, element: dict) -> None:
    """The child env codex builds: ``env_clear()`` plus the element's own pairs."""
    import os

    for key in list(os.environ):
        if key == session_confinement.PER_CHAT_ENV:
            monkeypatch.delenv(key)
    for pair in element["env"]:
        monkeypatch.setenv(pair["name"], pair["value"])


def test_under_codex_a_strict_session_never_attaches_nor_launches_a_secret_reference(
    per_chat_stub, monkeypatch, tmp_path
):
    import asyncio

    stub_mod, execs = per_chat_stub
    _env_from_element(monkeypatch, _stub_element("tok", per_chat=True))

    with pytest.raises(SystemExit):
        asyncio.run(stub_mod._amain(_stub_argv(tmp_path, {"CITY": "Paris"})))
    assert execs == [["true"]]

    with pytest.raises(SystemExit) as exc:
        asyncio.run(
            stub_mod._amain(_stub_argv(tmp_path, {"API_TOKEN": "secret://ATLASSIAN_TOKEN"}))
        )
    assert exc.value.code == stub_mod._EXIT_UNRESOLVED_SECRET
    assert execs == [["true"]]


def test_the_factory_marks_a_codex_session_at_auto(no_floor, monkeypatch):
    from kiro_crew.agent_sdk import backends

    monkeypatch.setattr(backends, "selectable_backends", lambda: frozenset({"", "kas", "codex"}))
    kwargs = _factory_kwargs(monkeypatch, "auto", backend="codex")

    assert kwargs["acp_backend"] == "codex"
    assert kwargs["extra_env"][session_confinement.PER_CHAT_ENV] == "1"


def test_the_mount_check_leaves_per_chat_stubs_out():
    from kiro_crew.acp.mcp_mount_check import broker_stub_roster

    assert broker_stub_roster([_stub_element("tok", per_chat=True)]).servers == {}
    assert broker_stub_roster([_stub_element("tok", per_chat=False)]).servers != {}
