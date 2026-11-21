"""MiMo Code admits the gateway's broker stubs for Crew's own servers, and only those stubs.

Crew's identity-bound servers (every managed server but the control plane) are
withheld from the MiMo Code array, because an element the agent spec describes
cannot carry this session's identity. The gateway's broker stub for one of them is
the form that works: the rewriter authored its launch, the stub token names this
session, and gatewayd hands the server its caller per call. These tests pin both
sides: the exact stub shape bound to this session's token is mounted for every such
server, and every look-alike, an unbound or foreign stub, a spec-described entry and
a server that reads identity from its own process stay withheld.

Each withheld case also projects the genuine stub, so a projection that admits
nothing fails these tests as surely as one that admits too much.
"""

from __future__ import annotations

import codex_session_mcp_helpers
import pytest
from codex_session_mcp_helpers import _by_name, _write_spec

from kiro_crew import mcp_discovery_managed, mcp_work
from kiro_crew.mcp_gateway import STUB_MODULE
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.mcp_gateway.hashing import STUB_FLAGS_FLAG, encode_target_args
from kiro_crew.mcp_gateway.session_servers import attach_stub_session_token
from kiro_crew.providers.mirrors.identity import identity_bound_crew_servers
from kiro_crew.providers.mirrors.mimocode import mimocode_projection, mimocode_tool_id

agents_dir = codex_session_mcp_helpers.agents_dir

_TOKEN = "session-token-a"
_WORK = mcp_work.SERVER_NAME
_GUIDE = "kirocrew-guide"
_DASHBOARD = "kirocrew-dashboard"

#: Every identity-bound Crew server. Unlike codex, the dashboard has no rule of its
#: own here: a member's session control is appended by the client after this array.
_SHAPE_CHECKED = sorted(identity_bound_crew_servers())


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
    servers = {n: dict(entry or {"command": "/opt/kirocrew", "args": ["mcp-x"]}) for n in names}
    _write_spec(agents_dir, servers=servers, tools=[f"@{n}" for n in names])


def _project(stubs: list[dict], *, session_token: str = _TOKEN, denies_in_force=None):
    return mimocode_projection(
        "kirocrew",
        session_key="worker:one",
        session_token=session_token,
        stub_server_names=tuple(sorted({s["name"] for s in stubs})),
        stub_elements=stubs,
        denies_in_force=denies_in_force,
    )


def _mounted(stubs: list[dict], **kw) -> dict[str, dict]:
    return _by_name(_project(stubs, **kw).params["mcpServers"])


def test_the_shape_checked_set_is_every_identity_bound_crew_server():
    assert {_WORK, _GUIDE, _DASHBOARD, "kirocrew-mate"} <= set(_SHAPE_CHECKED)
    assert "kirocrew-core" not in _SHAPE_CHECKED
    assert "kirocrew-cron" not in _SHAPE_CHECKED


@pytest.mark.parametrize("server", _SHAPE_CHECKED)
@pytest.mark.parametrize("envelope", [False, True])
def test_session_bound_crew_stub_is_mounted_as_given(agents_dir, server, envelope):
    _grant(agents_dir, server)
    stub = _stub(server, envelope=envelope)
    # The stub token is the only credential: no Crew identity env is added.
    assert _mounted([stub]).get(server) == stub


def test_a_tagged_stub_is_mounted_without_its_stdio_tag(agents_dir):
    """MiMo Code reads any element carrying ``type`` as remote, so the tag goes."""
    _grant(agents_dir, _GUIDE)
    stub = {**_stub(_GUIDE), "type": "stdio"}
    mounted = _mounted([stub])[_GUIDE]
    assert "type" not in mounted
    assert mounted == {k: v for k, v in stub.items() if k != "type"}


def test_every_crew_stub_mounts_together(agents_dir):
    _grant(agents_dir, *_SHAPE_CHECKED)
    mounted = _mounted([_stub(name) for name in _SHAPE_CHECKED])
    assert set(_SHAPE_CHECKED) <= set(mounted)


def _with_env(server: str, env: object) -> dict:
    stub = _stub(server)
    stub["env"] = env
    return stub


def _with(server: str, **over) -> dict:
    stub = _stub(server)
    stub.update(over)
    return stub


def _look_alikes(server: str) -> dict[str, dict]:
    pair = {"name": STUB_SESSION_TOKEN_ENV, "value": _TOKEN}
    other = _GUIDE if server != _GUIDE else _WORK
    return {
        "unbound, no token": _with_env(server, []),
        "token for another session": _stub(server, token="session-token-b"),
        "duplicate token pairs": _with_env(server, [dict(pair), dict(pair)]),
        "conflicting token pairs": _with_env(
            server,
            [dict(pair), {"name": STUB_SESSION_TOKEN_ENV, "value": "session-token-b"}],
        ),
        "extra env pair": _with_env(
            server, [dict(pair), {"name": "KIROCREW_SESSION_KEY", "value": "x"}]
        ),
        "env as a mapping": _with_env(server, {STUB_SESSION_TOKEN_ENV: _TOKEN}),
        "pair with extra keys": _with_env(server, [{**pair, "extra": "1"}]),
        "not the stub module": _with(server, args=["-m", "kiro_crew.mcp_x", "--server", server]),
        "spec launch, no stub": _with(server, command="/opt/kirocrew", args=["mcp-x"]),
        "registers another server": _with(server, args=_stub_args(other)),
        "registers two servers": _with(
            server, args=[*_stub_args(server), "--server", "kirocrew-panel"]
        ),
        "dangling --server": _with(server, args=["-m", STUB_MODULE, "--server"]),
        "unreadable envelope": _with(
            server, args=["-m", STUB_MODULE, f"{STUB_FLAGS_FLAG}=@@not-base64@@"]
        ),
        "non-string arg": _with(server, args=["-m", STUB_MODULE, "--server", server, 7]),
        "http transport": _with(server, type="http", url="http://127.0.0.1:1"),
        "no command": _with(server, command=""),
    }


_LOOK_ALIKE_CASES = [
    pytest.param(server, case, id=f"{server}-{case}")
    for server in (_WORK, _GUIDE)
    for case in _look_alikes(server)
]


@pytest.mark.parametrize("server,case", _LOOK_ALIKE_CASES)
def test_crew_stub_look_alike_is_withheld(agents_dir, server, case):
    _grant(agents_dir, server)
    assert server in _mounted([_stub(server)])
    assert server not in _mounted([_look_alikes(server)[case]])


@pytest.mark.parametrize("server", [_WORK, _GUIDE])
def test_a_session_with_no_token_admits_no_crew_stub(agents_dir, server):
    _grant(agents_dir, server)
    assert server in _mounted([_stub(server)])
    assert server not in _mounted([_stub(server, token="")], session_token="")
    assert server not in _mounted([_stub(server)], session_token="")
    # An empty token on both sides matches pair for pair, and is still no binding.
    empty = _with_env(server, [{"name": STUB_SESSION_TOKEN_ENV, "value": ""}])
    assert server not in _mounted([empty], session_token="")


@pytest.mark.parametrize("server", [_WORK, _GUIDE])
@pytest.mark.parametrize(
    "entry,granted",
    [({"command": "/opt/kirocrew", "disabled": True}, True), ({"command": "/opt/kirocrew"}, False)],
    ids=["server disabled", "not granted"],
)
def test_crew_stub_keeps_the_spec_grants(agents_dir, server, entry, granted):
    _grant(agents_dir, server)
    assert server in _mounted([_stub(server)])
    tools = [f"@{server}"] if granted else ["@kirocrew-core"]
    _write_spec(agents_dir, servers={server: entry}, tools=tools)
    assert server not in _mounted([_stub(server)])


@pytest.mark.parametrize("server", [_WORK, _GUIDE])
def test_a_switched_off_tool_keeps_the_stub_with_its_deny_rule(agents_dir, server):
    """The per-tool deny keys on the server name the stub shares, so the stub stays
    mounted while the rule is in force and is withheld when it is not."""
    entry = {"command": "/opt/kirocrew", "disabledTools": ["some_tool"]}
    _write_spec(agents_dir, servers={server: entry}, tools=[f"@{server}"])
    rule = mimocode_tool_id(server, "some_tool")
    projection = _project([_stub(server)])
    assert server in _by_name(projection.params["mcpServers"])
    assert rule in projection.harness_deny_rules
    assert server not in _mounted([_stub(server)], denies_in_force=frozenset())
    assert server in _mounted([_stub(server)], denies_in_force=frozenset({rule}))


def test_spec_described_crew_entries_stay_withheld(agents_dir):
    _grant(agents_dir, *_SHAPE_CHECKED)
    assert not set(_SHAPE_CHECKED) & set(_mounted([]))


def test_a_crew_server_that_reads_identity_from_its_process_is_withheld(agents_dir, monkeypatch):
    _grant(agents_dir, _WORK, _GUIDE)
    assert {_WORK, _GUIDE} <= set(_mounted([_stub(_WORK), _stub(_GUIDE)]))
    real = mcp_discovery_managed.managed_server_is_session_bound
    monkeypatch.setattr(
        mcp_discovery_managed,
        "managed_server_is_session_bound",
        lambda name: name == _GUIDE or real(name),
    )
    mounted = _mounted([_stub(_WORK), _stub(_GUIDE)])
    assert _WORK in mounted
    assert _GUIDE not in mounted


def test_a_third_party_stub_is_unchanged(agents_dir):
    """A stub for a server that is not Crew's was never withheld and still mounts."""
    _grant(agents_dir, "acme")
    stub = _with_env("acme", [])
    stub["args"] = _stub_args("acme")
    assert "acme" in _mounted([stub], session_token="")
