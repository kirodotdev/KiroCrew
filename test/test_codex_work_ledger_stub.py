"""Codex admits the gateway's work-ledger broker stub, and only that stub.

The work ledger is one of Crew's identity-bound servers, so the codex projection
withholds it. The gateway's broker stub for it is the one form that works on this
transport: the rewriter authored its launch and the stub token names this session.
These tests pin both sides. The exact stub shape bound to this session's token is
mounted; every look-alike, an unbound or foreign stub, and every other
identity-bound server stays withheld.

Each withheld case also projects the genuine stub, so a projection that admits
nothing fails these tests as surely as one that admits too much.
"""

from __future__ import annotations

import codex_session_mcp_helpers
import pytest
from codex_session_mcp_helpers import _by_name, _write_spec

from kiro_crew import mcp_work
from kiro_crew.mcp_gateway import STUB_MODULE
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.mcp_gateway.hashing import STUB_FLAGS_FLAG, encode_target_args
from kiro_crew.mcp_gateway.session_servers import attach_stub_session_token
from kiro_crew.members import MEMBER_DISPATCH_SERVER
from kiro_crew.providers.mirrors.codex import codex_projection
from kiro_crew.providers.mirrors.identity import identity_bound_crew_servers

agents_dir = codex_session_mcp_helpers.agents_dir

_TOKEN = "session-token-a"
_WORK = mcp_work.SERVER_NAME


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


def _grant(agents_dir, *names: str, entry: dict | None = None) -> None:
    servers = {n: dict(entry or {"command": "/opt/kirocrew", "args": ["mcp-work"]}) for n in names}
    _write_spec(agents_dir, servers=servers, tools=[f"@{n}" for n in names])


def _mounted(stubs: list[dict], *, session_token: str = _TOKEN) -> dict[str, dict]:
    names = tuple(sorted({s["name"] for s in stubs}))
    projection = codex_projection(
        "kirocrew",
        session_key="worker:one",
        session_token=session_token,
        stub_server_names=names,
        stub_elements=stubs,
    )
    return _by_name(projection.params["mcpServers"])


@pytest.mark.parametrize("envelope", [False, True])
def test_session_bound_work_ledger_stub_is_mounted_as_given(agents_dir, envelope):
    _grant(agents_dir, _WORK)
    stub = _stub(envelope=envelope)
    mounted = _mounted([stub])
    assert mounted.get(_WORK) == stub
    # No Crew identity is added: the stub token is the only credential it carries.
    assert [p["name"] for p in mounted[_WORK]["env"]] == [STUB_SESSION_TOKEN_ENV]


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
        ({"command": "/opt/kirocrew", "disabledTools": ["work_report"]}, [f"@{_WORK}"]),
        ({"command": "/opt/kirocrew", "disabled": True}, [f"@{_WORK}"]),
        ({"command": "/opt/kirocrew"}, ["@kirocrew-core"]),
    ],
    ids=["tool switched off", "server disabled", "not granted"],
)
def test_work_ledger_stub_keeps_the_spec_restrictions(agents_dir, entry, tools):
    _grant(agents_dir, _WORK)
    assert _WORK in _mounted([_stub()])
    _write_spec(agents_dir, servers={_WORK: entry}, tools=tools)
    assert _WORK not in _mounted([_stub()])


def test_every_other_identity_bound_stub_stays_withheld(agents_dir):
    others = sorted(identity_bound_crew_servers() - {_WORK, MEMBER_DISPATCH_SERVER})
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
