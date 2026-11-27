"""A crewmate's worker on a mirrored host gets its broker stubs by its own session.

codex mounts Crew's own identity-bound servers only as broker stubs bound to the
session's token, so a worker whose stub lookup answered nothing had no
``kirocrew-work`` at all. Both mirrored session paths (``AcpRuntime`` and
``AcpClient``) resolve the worker's stubs through its session key, and hold them
to the limits its merged spec reads at the projection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from types import SimpleNamespace

import codex_session_mcp_helpers
import pytest

from kiro_crew import mate_config, mate_worker
from kiro_crew.acp import client as acp_client
from kiro_crew.acp import runtime as acp_runtime
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_mcp_crewmate import crewmate_worker_deny_rules
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX
from kiro_crew.config import paths
from kiro_crew.mate_worker import MATE_WORKER_AGENT
from kiro_crew.mcp_gateway import session_servers
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.mcp_gateway.hashing import expand_stub_flags
from kiro_crew.mcp_gateway.rewriter import rewrite_agents

agents_dir = codex_session_mcp_helpers.agents_dir
WORK = "kirocrew-work"
PROBE = "probe"
KEY = "worker:own"
WORK_ENTRY = {"command": "/opt/kirocrew", "args": ["mcp-work"]}
PROBE_ENTRY = {"command": sys.executable, "args": ["-V"]}


def _install(
    agents_dir, tmp_path, monkeypatch, *, servers=None, tools=None, bot=None, stub_servers=None
):
    """The stock worker spec, the Bot ``pub``'s file when *bot* is given, and their overlays.

    Session ``KEY`` is the Bot ``bot``'s worker; ``chat:unrelated`` is another Bot's chat.
    """
    mates = tmp_path / "mates"
    mates.mkdir()
    base = {
        "name": MATE_WORKER_AGENT,
        "prompt": "Worker",
        "tools": tools if tools is not None else [f"@{WORK}"],
        "mcpServers": servers if servers is not None else {WORK: dict(WORK_ENTRY)},
    }
    (mates / f"{MATE_WORKER_AGENT}.json").write_text(json.dumps(base), encoding="utf-8")
    if bot is not None:
        (mates / "pub.json").write_text(json.dumps({"name": "pub", **bot}), encoding="utf-8")
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(
        mate_worker,
        "mate_file_for_member",
        lambda member, config=None: "pub" if bot is not None and member == "bot" else None,
    )
    monkeypatch.setattr(mate_config, "crewmate_file_on_record", lambda mate_id: mate_id == "pub")
    monkeypatch.setattr(mate_worker, "globally_disabled_refs", lambda names: [])
    records = {
        KEY: SimpleNamespace(
            selection_kind="template", template_id=MATE_WORKER_AGENT, member_id="bot"
        ),
        "chat:unrelated": SimpleNamespace(
            selection_kind="member", template_id="other", member_id="other"
        ),
    }
    monkeypatch.setattr("kiro_crew.execution_context.read_session_execution", records.get)
    overlay = tmp_path / "overlay"
    rewrite_agents(
        source_dir=agents_dir,
        overlay_dir=overlay,
        socket_path=tmp_path / "gateway.sock",
        work_dir=tmp_path,
        stub_servers=frozenset(stub_servers or {WORK}),
        mate_source_dir=mates,
    )
    return overlay


@pytest.fixture
def worker(agents_dir, tmp_path, monkeypatch):
    """The stock worker spec mounting ``kirocrew-work``, its overlay, and one worker session."""
    return _install(agents_dir, tmp_path, monkeypatch)


def _work_stub(servers: list[dict], name: str = WORK, agent: str = MATE_WORKER_AGENT) -> dict:
    """The one *name* element, which must be a broker stub carrying ``--agent`` *agent*."""
    found = [e for e in servers if e.get("name") == name]
    assert len(found) == 1, servers
    flags = expand_stub_flags(found[0]["args"])
    assert flags[flags.index("--agent") + 1] == agent
    return found[0]


def _token(entry: dict) -> str | None:
    return {p["name"]: p["value"] for p in entry["env"]}.get(STUB_SESSION_TOKEN_ENV)


def _runtime_servers(overlay, tmp_path, key: str):
    runtime = AcpRuntime(work_dir=str(tmp_path), acp_backend=ACP_BACKEND_CODEX)
    runtime._mcp_gateway_overlay = str(overlay)
    return asyncio.run(
        runtime._mirrored_session_mcp(
            MATE_WORKER_AGENT, work_dir=tmp_path, session_key=key, channel_id="c-1"
        )
    )


def test_a_codex_runtime_worker_mounts_its_work_stub(worker, tmp_path):
    projected = _runtime_servers(worker, tmp_path, KEY)
    assert _token(_work_stub(projected.servers)) == projected.stub_token


@pytest.mark.parametrize("key", ["", "chat:unrelated"])
def test_a_session_that_is_not_the_worker_gets_no_worker_stub(worker, tmp_path, key):
    servers = _runtime_servers(worker, tmp_path, key).servers
    assert WORK not in {e.get("name") for e in servers}


def test_a_codex_client_worker_mounts_its_work_stub(worker, tmp_path):
    client = AcpClient(work_dir=tmp_path, agent=MATE_WORKER_AGENT, acp_backend=ACP_BACKEND_CODEX)
    client._session_key = KEY
    client._mcp_gateway_overlay = str(worker)
    try:
        entry = _work_stub(client._resolve_session_mcp_servers())
        assert _token(entry) == client._stub_session_token
    finally:
        client._reset_state()


def test_a_claude_client_worker_mounts_its_work_stub(worker, tmp_path):
    client = AcpClient(work_dir=tmp_path, agent=MATE_WORKER_AGENT, acp_backend=ACP_BACKEND_CLAUDE)
    client._session_key = KEY
    client._mcp_gateway_overlay = str(worker)
    try:
        client._write_claude_local_settings()
        assert client._permission_surface_governed
        entry = _work_stub(client._resolve_session_mcp_servers())
        assert _token(entry) == client._stub_session_token
    finally:
        try:
            asyncio.run(client._discard_claude_settings_seed())
        finally:
            client._reset_state()


def test_a_codex_worker_gets_its_bots_added_server_as_a_gateway_stub(
    agents_dir, tmp_path, monkeypatch
):
    """A server the Bot's file adds starts in the gateway, which resolves its ``secret://``."""
    wx = {**PROBE_ENTRY, "env": {"WX_APPSECRET": "secret://WX_APPSECRET"}}
    overlay = _install(
        agents_dir,
        tmp_path,
        monkeypatch,
        bot={"tools": ["@wx"], "mcpServers": {"wx": wx}},
        stub_servers={WORK, "wx"},
    )
    projected = _runtime_servers(overlay, tmp_path, KEY)
    entry = _work_stub(projected.servers, "wx", "crewmate:pub")
    assert _token(entry) == projected.stub_token
    assert "secret://" not in json.dumps(projected.servers)
    for key in ("", "chat:unrelated"):
        assert "wx" not in {e.get("name") for e in _runtime_servers(overlay, tmp_path, key).servers}


def _claude_runtime_servers(overlay, tmp_path) -> list[dict]:
    runtime = AcpRuntime(work_dir=str(tmp_path), acp_backend=ACP_BACKEND_CLAUDE)
    runtime._mcp_gateway_overlay = str(overlay)
    projected = asyncio.run(
        runtime._mirrored_session_mcp(
            MATE_WORKER_AGENT,
            work_dir=tmp_path,
            session_key=KEY,
            channel_id="c-1",
            permission_surface_owned=True,
        )
    )
    return projected.servers


@contextlib.contextmanager
def _claude_client(overlay, tmp_path):
    client = AcpClient(work_dir=tmp_path, agent=MATE_WORKER_AGENT, acp_backend=ACP_BACKEND_CLAUDE)
    client._session_key = KEY
    client._mcp_gateway_overlay = str(overlay)
    try:
        client._write_claude_local_settings()
        assert client._permission_surface_governed
        yield client
    finally:
        try:
            asyncio.run(client._discard_claude_settings_seed())
        finally:
            client._reset_state()


def _claude_worker(path, overlay, tmp_path, arm=None) -> tuple[list[dict], list[str], list[str]]:
    """A Claude worker session's array, and the deny and allow rules its seed carries.

    *arm* runs right before the array is resolved, after the client's seed is
    written. The runtime's seed is written per session from the same rules
    (``crewmate_worker_deny_rules``) and carries no allow rule of its own.
    """
    if path == "runtime":
        if arm is not None:
            arm()
        deny = crewmate_worker_deny_rules(
            MATE_WORKER_AGENT, work_dir=None, session_key=KEY, backend=ACP_BACKEND_CLAUDE
        )
        return _claude_runtime_servers(overlay, tmp_path), list(deny or []), []
    with _claude_client(overlay, tmp_path) as client:
        seed = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        if arm is not None:
            arm()
        servers = client._resolve_session_mcp_servers()
    permissions = seed.get("permissions") or {}
    return servers, list(permissions.get("deny") or []), list(permissions.get("allow") or [])


def _withhold(spec_path, change: str) -> None:
    """Drop, mute or retype ``kirocrew-work`` in the stock worker spec."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if change == "removed":
        del spec["mcpServers"][WORK]
    else:
        spec["mcpServers"][WORK].update(
            {"disabled": True} if change == "muted" else {"type": "registry"}
        )
    spec_path.write_text(json.dumps(spec), encoding="utf-8")


@pytest.mark.parametrize("path", ["runtime", "client"])
@pytest.mark.parametrize("when", ["before_the_lookup", "after_the_lookup"])
@pytest.mark.parametrize("change", ["removed", "muted", "registry"])
def test_a_claude_worker_mounts_no_stub_for_a_server_its_spec_withholds(
    worker, tmp_path, monkeypatch, path, when, change
):
    """Neither the stub nor the entry it replaces, even when the change lands mid-start."""
    spec_path = tmp_path / "mates" / f"{MATE_WORKER_AGENT}.json"
    if when == "before_the_lookup":
        _withhold(spec_path, change)
        servers, _deny, _allow = _claude_worker(path, worker, tmp_path)
    else:
        real = session_servers.pooled_session_servers
        changed: list[bool] = []

        def lookup_then_change(*args, **kwargs):
            found = real(*args, **kwargs)
            if not changed:
                assert WORK in {e.get("name") for e in found}
                _withhold(spec_path, change)
                changed.append(True)
            return found

        def arm():
            monkeypatch.setattr(acp_runtime, "pooled_session_servers", lookup_then_change)
            monkeypatch.setattr(acp_client, "pooled_session_servers", lookup_then_change)

        servers, _deny, _allow = _claude_worker(path, worker, tmp_path, arm)
        assert changed
    assert WORK not in {e.get("name") for e in servers}


@pytest.mark.parametrize("path", ["runtime", "client"])
def test_a_claude_workers_switched_off_tools_are_denied_on_its_stub(
    agents_dir, tmp_path, monkeypatch, path
):
    """The merged ``disabledTools`` and ``@server/tool`` excludes name the stub's tools."""
    overlay = _install(
        agents_dir,
        tmp_path,
        monkeypatch,
        servers={WORK: dict(WORK_ENTRY), PROBE: {**PROBE_ENTRY, "disabledTools": ["off"]}},
        tools=[f"@{WORK}", f"@{PROBE}"],
        bot={"excludedTools": [f"@{PROBE}/hidden"]},
        stub_servers={WORK, PROBE},
    )
    servers, deny, _allow = _claude_worker(path, overlay, tmp_path)
    _work_stub(servers, PROBE)
    assert {f"mcp__{PROBE}__off", f"mcp__{PROBE}__hidden"} <= set(deny)


@pytest.mark.parametrize("path", ["runtime", "client"])
def test_a_claude_workers_stub_is_never_auto_approved(agents_dir, tmp_path, monkeypatch, path):
    overlay = _install(
        agents_dir, tmp_path, monkeypatch, servers={WORK: {**WORK_ENTRY, "autoApprove": ["*"]}}
    )
    servers, _deny, allow = _claude_worker(path, overlay, tmp_path)
    assert "autoApprove" not in _work_stub(servers)
    assert not [rule for rule in allow if rule.startswith(f"mcp__{WORK}")]


@pytest.mark.parametrize("path", ["runtime", "client"])
def test_a_claude_worker_mounts_no_stub_its_tools_do_not_reference(
    agents_dir, tmp_path, monkeypatch, path
):
    overlay = _install(
        agents_dir,
        tmp_path,
        monkeypatch,
        servers={WORK: dict(WORK_ENTRY), PROBE: dict(PROBE_ENTRY)},
        tools=[f"@{WORK}", f"@{PROBE}"],
        stub_servers={WORK, PROBE},
    )
    # The pass wraps only a server ``tools`` names, so the stub exists because the
    # last pass saw ``@probe``; ``tools`` drops it after that pass, before the session.
    spec_path = tmp_path / "mates" / f"{MATE_WORKER_AGENT}.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["tools"] = [f"@{WORK}"]
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    looked_up = session_servers.pooled_session_servers(
        overlay, MATE_WORKER_AGENT, worker_session_key=KEY
    )
    assert PROBE in {e.get("name") for e in looked_up}
    servers, _deny, _allow = _claude_worker(path, overlay, tmp_path)
    _work_stub(servers)
    assert PROBE not in {e.get("name") for e in servers}
