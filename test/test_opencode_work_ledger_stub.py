"""opencode admits the gateway's work-ledger broker stub, and only that stub.

The work ledger is one of Crew's identity-bound servers, so the opencode projection
withholds it. The gateway's broker stub for it is the one form that works on this
transport: the rewriter authored its launch and the stub token names this session.
These tests pin both sides. The exact stub shape bound to this session's token is
mounted; every look-alike, an unbound or foreign stub, and every other
identity-bound server stays withheld.

Each withheld case also projects the genuine stub, so a projection that admits
nothing fails these tests as surely as one that admits too much.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import mcp_work
from kiro_crew.acp import session_mcp
from kiro_crew.mcp_gateway import STUB_MODULE
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.mcp_gateway.hashing import STUB_FLAGS_FLAG, encode_target_args
from kiro_crew.mcp_gateway.session_servers import attach_stub_session_token
from kiro_crew.providers.mirrors.goose import goose_projection
from kiro_crew.providers.mirrors.identity import identity_bound_crew_servers
from kiro_crew.providers.mirrors.opencode import opencode_projection, opencode_tool_id

_TOKEN = "session-token-a"
_WORK = mcp_work.SERVER_NAME


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """Point the agent-spec resolver at a temp agents directory; these tests supply the spec."""
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", lambda name, **kwargs: None)
    monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    return d


def _write_spec(agents_dir: Path, *, servers: dict, tools: list) -> None:
    spec = {"name": "kirocrew", "mcpServers": servers, "tools": tools}
    (agents_dir / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")


def _stub_args(server: str, *, envelope: bool = False) -> list[str]:
    flags = ["--server", server, "--agent", "kirocrew", "--socket", "/run/gw.sock"]
    if envelope:
        return ["-m", STUB_MODULE, f"{STUB_FLAGS_FLAG}={encode_target_args(flags)}"]
    return ["-m", STUB_MODULE, *flags]


def _stub(server: str = _WORK, *, token: str = _TOKEN, envelope: bool = False) -> dict:
    """A broker stub as the client hands it over: shaped, then given the token."""
    shaped = {
        "name": server,
        "command": "/usr/bin/python3",
        "args": _stub_args(server, envelope=envelope),
        "env": [],
    }
    return attach_stub_session_token([shaped], token)[0]


def _grant(agents_dir, *names: str) -> None:
    servers = {n: {"command": "/opt/kirocrew", "args": ["mcp-work"]} for n in names}
    _write_spec(agents_dir, servers=servers, tools=[f"@{n}" for n in names])


def _project(stubs: list[dict], *, session_token: str = _TOKEN, project=opencode_projection, **kw):
    names = tuple(sorted({s["name"] for s in stubs}))
    return project(
        "kirocrew",
        session_key="worker:one",
        session_token=session_token,
        stub_server_names=names,
        stub_elements=stubs,
        **kw,
    )


def _mounted(stubs: list[dict], **kw) -> dict[str, dict]:
    return {e["name"]: e for e in _project(stubs, **kw).params["mcpServers"]}


@pytest.mark.parametrize("envelope", [False, True])
def test_session_bound_work_ledger_stub_is_mounted_as_given(agents_dir, envelope):
    _grant(agents_dir, _WORK)
    stub = _stub(envelope=envelope)
    mounted = _mounted([stub])
    assert mounted.get(_WORK) == stub
    # No Crew identity is added: the stub token is the only credential it carries.
    assert [p["name"] for p in mounted[_WORK]["env"]] == [STUB_SESSION_TOKEN_ENV]


def test_a_stdio_tagged_stub_is_mounted_with_the_tag_stripped(agents_dir):
    _grant(agents_dir, _WORK)
    stub = {**_stub(), "type": "stdio"}
    element = _mounted([stub])[_WORK]
    assert "type" not in element
    assert element["args"] == stub["args"]


def test_a_spec_described_work_ledger_entry_stays_withheld(agents_dir):
    _grant(agents_dir, _WORK)
    assert _WORK not in _mounted([])


def _with_env(env: list) -> dict:
    stub = _stub()
    stub["env"] = env
    return stub


def _with(**over) -> dict:
    stub = _stub()
    stub.update(over)
    return stub


_TOKEN_PAIR = {"name": STUB_SESSION_TOKEN_ENV, "value": _TOKEN}

_LOOK_ALIKES = {
    "unbound, no token": _with_env([]),
    "token for another session": _stub(token="session-token-b"),
    "duplicate token pairs": _with_env([dict(_TOKEN_PAIR), dict(_TOKEN_PAIR)]),
    "conflicting token pairs": _with_env(
        [dict(_TOKEN_PAIR), {"name": STUB_SESSION_TOKEN_ENV, "value": "session-token-b"}]
    ),
    "extra env pair": _with_env(
        [dict(_TOKEN_PAIR), {"name": "KIROCREW_SESSION_KEY", "value": "x"}]
    ),
    "env as a mapping": _with(env={STUB_SESSION_TOKEN_ENV: _TOKEN}),
    "pair with extra keys": _with_env([{**_TOKEN_PAIR, "extra": "1"}]),
    "not the stub module": _with(args=["-m", "kiro_crew.mcp_work", "--server", _WORK]),
    "spec launch, no stub": _with(command="/opt/kirocrew", args=["mcp-work"]),
    "registers another server": _with(args=_stub_args("kirocrew-dashboard")),
    "registers two servers": _with(args=[*_stub_args(_WORK), "--server", "kirocrew-mate"]),
    "unreadable envelope": _with(args=["-m", STUB_MODULE, f"{STUB_FLAGS_FLAG}=@@not-base64@@"]),
    "non-string arg": _with(args=["-m", STUB_MODULE, "--server", _WORK, 7]),
    "http transport": _with(type="http", url="http://127.0.0.1:1"),
    "no command": _with(command=""),
}


@pytest.mark.parametrize("stub", list(_LOOK_ALIKES.values()), ids=list(_LOOK_ALIKES))
def test_work_ledger_look_alike_is_withheld(agents_dir, stub):
    _grant(agents_dir, _WORK)
    assert _WORK in _mounted([_stub()])
    assert _WORK not in _mounted([stub])


def test_a_session_with_no_token_admits_no_work_ledger_stub(agents_dir):
    _grant(agents_dir, _WORK)
    assert _WORK in _mounted([_stub()])
    assert _WORK not in _mounted([_stub(token="")], session_token="")
    assert _WORK not in _mounted([_stub()], session_token="")


@pytest.mark.parametrize(
    "entry,tools",
    [
        ({"command": "/opt/kirocrew", "disabled": True}, [f"@{_WORK}"]),
        ({"command": "/opt/kirocrew"}, ["@kirocrew-core"]),
    ],
    ids=["server disabled", "not granted"],
)
def test_work_ledger_stub_keeps_the_spec_restrictions(agents_dir, entry, tools):
    _grant(agents_dir, _WORK)
    assert _WORK in _mounted([_stub()])
    _write_spec(agents_dir, servers={_WORK: entry}, tools=tools)
    assert _WORK not in _mounted([_stub()])


def test_a_switched_off_tool_is_denied_on_the_stub_or_the_stub_is_withheld(agents_dir):
    """opencode keeps a narrowed server mounted only while its deny rule is in force."""
    entry = {"command": "/opt/kirocrew", "disabledTools": ["work_report"]}
    _write_spec(agents_dir, servers={_WORK: entry}, tools=[f"@{_WORK}"])
    rule = opencode_tool_id(_WORK, "work_report")
    projection = _project([_stub()])
    assert _WORK in {e["name"] for e in projection.params["mcpServers"]}
    assert rule in projection.harness_deny_rules
    # The rule is not in force on this session: the stub is withheld whole.
    assert _WORK not in _mounted([_stub()], denies_in_force=())


def test_every_other_identity_bound_stub_stays_withheld(agents_dir):
    others = sorted(identity_bound_crew_servers() - {_WORK})
    assert others
    _grant(agents_dir, _WORK, *others)
    stubs = [_stub(_WORK)]
    for name in others:
        stubs.append(_stub(name))
        # The work-ledger shape under another name is no admission either.
        renamed = _stub(_WORK)
        renamed["name"] = name
        stubs.append(renamed)
    mounted = _mounted(stubs)
    assert _WORK in mounted
    assert not set(others) & set(mounted)


def test_the_admission_is_opencode_only(agents_dir):
    """The placement function is shared; goose does not opt in, so it still withholds."""
    _grant(agents_dir, _WORK)
    assert _WORK in _mounted([_stub()])
    assert _WORK not in _mounted([_stub()], project=goose_projection)
