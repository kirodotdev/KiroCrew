"""A crewmate's worker on pi gets the gateway stubs for the servers its Bot adds.

pi has no mirror, so Crew's own resolver (``AcpClient._mirrorless_member_mounts``)
mounts the worker's spec servers, and the pooled stubs ride beside them. The
worker's stubs are looked up by its own session key, so a server the Bot adds
starts in the gateway, which resolves its ``secret://`` values, instead of
reaching pi from the spec as written.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import codex_session_mcp_helpers
import pytest

from kiro_crew import mate_config, mate_worker
from kiro_crew.acp import client as acp_client
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp_backends import ACP_BACKEND_PI
from kiro_crew.config import paths
from kiro_crew.mate_worker import MATE_WORKER_AGENT
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.mcp_gateway.hashing import expand_stub_flags
from kiro_crew.mcp_gateway.rewriter import rewrite_agents
from kiro_crew.members_parts import member_sessions

agents_dir = codex_session_mcp_helpers.agents_dir
KEY = "worker:own"
WX = {"command": sys.executable, "args": ["-V"], "env": {"WX_APPSECRET": "secret://WX_APPSECRET"}}


CORE = "kirocrew-core"
_LISTED = object()


def _install(agents_dir, tmp_path, monkeypatch, *, tools=_LISTED, core=False):
    """The stock worker base, the Bot ``pub``'s file adding ``wx``, and their overlays.

    *tools* is the Bot file's ``tools`` (``["@wx"]`` by default, ``None`` for no key).
    *core* puts ``kirocrew-core`` in the worker base and routes it through the gateway.
    Session ``KEY`` is the Bot ``bot``'s worker; ``chat:unrelated`` is another Bot's chat.
    """
    mates = tmp_path / "mates"
    mates.mkdir()
    base = {
        "name": MATE_WORKER_AGENT,
        "prompt": "Worker",
        "tools": [f"@{CORE}"] if core else [],
        "mcpServers": {CORE: dict(codex_session_mcp_helpers._CORE)} if core else {},
    }
    (mates / f"{MATE_WORKER_AGENT}.json").write_text(json.dumps(base), encoding="utf-8")
    pub: dict = {"name": "pub", "mcpServers": {"wx": dict(WX)}}
    if tools is _LISTED:
        pub["tools"] = ["@wx"]
    elif tools is not None:
        pub["tools"] = tools
    (mates / "pub.json").write_text(json.dumps(pub), encoding="utf-8")
    monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(
        mate_worker,
        "mate_file_for_member",
        lambda member, config=None: "pub" if member == "bot" else None,
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
    monkeypatch.setattr(member_sessions, "_CREWMATE_WORKER_SLOTS", {KEY})
    overlay = tmp_path / "overlay"
    rewrite_agents(
        source_dir=agents_dir,
        overlay_dir=overlay,
        socket_path=tmp_path / "gateway.sock",
        work_dir=tmp_path,
        stub_servers=frozenset({"wx", CORE} if core else {"wx"}),
        mate_source_dir=mates,
    )
    return SimpleNamespace(overlay=overlay, pub=mates / "pub.json")


@pytest.fixture
def bot(agents_dir, tmp_path, monkeypatch):
    return _install(agents_dir, tmp_path, monkeypatch)


def _pi_array(overlay, tmp_path, key: str, member_context=None) -> tuple[list[dict], str]:
    """What the pi handoff carries for *key*: the member mounts, then the pooled stubs.

    *member_context* defaults to what allocation sets: the worker's execution record
    names its crewmate, so a keyed session is a member.
    """
    client = AcpClient(work_dir=tmp_path, agent=MATE_WORKER_AGENT, acp_backend=ACP_BACKEND_PI)
    client._session_key = key
    client._mcp_gateway_overlay = str(overlay)
    client.member_context = bool(key) if member_context is None else member_context
    try:
        client._session_mcp_cache = client._resolve_session_mcp_servers()
        return [*client._session_mcp_servers(), *client._pooled_mcp_servers()], (
            client._stub_session_token
        )
    finally:
        client._reset_state()


def test_a_pi_worker_gets_its_bots_added_server_as_a_gateway_stub(bot, tmp_path):
    servers, token = _pi_array(bot.overlay, tmp_path, KEY)
    found = [e for e in servers if e.get("name") == "wx"]
    assert len(found) == 1, servers
    flags = expand_stub_flags(found[0]["args"])
    assert flags[flags.index("--agent") + 1] == "crewmate:pub"
    assert {p["name"]: p["value"] for p in found[0]["env"]}[STUB_SESSION_TOKEN_ENV] == token
    assert "secret://" not in json.dumps(servers)


@pytest.mark.parametrize("key", ["", "chat:unrelated"])
def test_a_pi_session_that_is_not_the_worker_gets_no_worker_stub(bot, tmp_path, key):
    servers, _token = _pi_array(bot.overlay, tmp_path, key)
    assert "wx" not in {e.get("name") for e in servers}


@pytest.mark.parametrize("change", ["removed", "muted"])
def test_a_pi_worker_mounts_no_stub_for_a_server_withheld_after_the_lookup(
    bot, tmp_path, monkeypatch, change
):
    """Neither the stub nor the entry it replaces, when the Bot's file changes mid-start."""
    real = acp_client.pooled_session_servers
    changed: list[bool] = []

    def lookup_then_change(*args, **kwargs):
        found = real(*args, **kwargs)
        if not changed and "wx" in {e.get("name") for e in found}:
            pub = json.loads(bot.pub.read_text(encoding="utf-8"))
            if change == "removed":
                del pub["mcpServers"]["wx"]
            else:
                pub["mcpServers"]["wx"]["disabled"] = True
            bot.pub.write_text(json.dumps(pub), encoding="utf-8")
            changed.append(True)
        return found

    monkeypatch.setattr(acp_client, "pooled_session_servers", lookup_then_change)
    servers, _token = _pi_array(bot.overlay, tmp_path, KEY)
    assert changed
    assert "wx" not in {e.get("name") for e in servers}


@pytest.mark.parametrize(
    ("tools", "expected"),
    # Pinned: the name set an ordinary pi start mounts on this fixture with
    # ``member_context`` off, ``wx`` when ``tools`` names it and nothing when ``tools``
    # is absent or empty. The fix changes only the form of ``wx``, never the names.
    [(_LISTED, ["wx"]), (None, []), ([], [])],
    ids=["tools-listed", "no-tools-key", "tools-empty"],
)
def test_a_pi_workers_server_names_with_member_context_off_match_main(
    agents_dir, tmp_path, monkeypatch, tools, expected
):
    """A worker is held to its spec's ``tools`` whatever ``member_context`` says, so an
    unreferenced Bot server gets no stub either."""
    overlay = _install(agents_dir, tmp_path, monkeypatch, tools=tools).overlay
    servers, _token = _pi_array(overlay, tmp_path, KEY, member_context=False)
    assert sorted(e.get("name") for e in servers) == expected
    assert "secret://" not in json.dumps(servers)


def test_a_pi_workers_base_server_reaches_it_once_as_a_stub(agents_dir, tmp_path, monkeypatch):
    overlay = _install(agents_dir, tmp_path, monkeypatch, core=True).overlay
    servers, token = _pi_array(overlay, tmp_path, KEY)
    found = [e for e in servers if e.get("name") == CORE]
    assert len(found) == 1, servers
    flags = expand_stub_flags(found[0]["args"])
    assert flags[flags.index("--agent") + 1] == MATE_WORKER_AGENT
    assert {p["name"]: p["value"] for p in found[0]["env"]}[STUB_SESSION_TOKEN_ENV] == token


@pytest.mark.parametrize(
    "tools", [_LISTED, None, []], ids=["tools-listed", "no-tools-key", "tools-empty"]
)
def test_a_worker_record_whose_slot_this_process_has_not_noted_takes_mains_path(
    agents_dir, tmp_path, monkeypatch, tools
):
    """The record says worker but the in-process slot set does not: no keyed stub.

    Pinned: such a session mounts no server on this fixture with ``member_context``
    off, whatever the Bot's ``tools`` says, as an ordinary pi start does.
    """
    overlay = _install(agents_dir, tmp_path, monkeypatch, tools=tools).overlay
    monkeypatch.setattr(member_sessions, "_CREWMATE_WORKER_SLOTS", set())
    servers, _token = _pi_array(overlay, tmp_path, KEY, member_context=False)
    assert sorted(e.get("name") for e in servers) == []
