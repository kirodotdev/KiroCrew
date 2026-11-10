"""KAS projection: a stdio MCP server gets the gateway's Kerberos and ssh-agent pointers.

kiro-cli's MCP children inherit its environment, so they see the ``KRB5CCNAME`` and
``SSH_AUTH_SOCK`` the gateway armed for it. KAS starts a stdio server with a fixed
minimal environment instead, so the projection hands the server the same two
pointers through Crew's launcher (``kiro_crew.kas_env_shim``) and its owner-only
snapshot. These tests pin that both pointers reach the server's process and never
the wire, that the socket follows the KAS spawn's own forward verdict, that a
value the server declares wins, and that a server which starts today still starts
when the snapshot cannot be written.

The conftest floor pins the relay empty for every other test, so a projection
there does not depend on the runner's own ssh-agent or Kerberos cache; the
``relay`` fixture here puts the real one back over a gateway environment it sets.
"""

from __future__ import annotations

import json
import sys

import pytest
from kas_agents_helpers import _spec
from test_kas_env_launch import _run_entry, _snapshot, _wire_text

from kiro_crew import env as env_mod
from kiro_crew import kas_env_shim, platform_compat, sandbox
from kiro_crew.acp import kas_env_launch
from kiro_crew.acp.kas_agents import to_client_custom_agent

SESSION = "dashboard_chat-kas-auth-1"
SOCK_LEAF = "agent.kas-auth-sock-4242"
CACHE_LEAF = "krb5cc_kas-auth-4242"

#: The real relay, read at import, before the conftest floor pins it empty.
_REAL_INHERITED_AUTH_ENV = kas_env_launch.inherited_auth_env

#: A stdio MCP server that answers ``initialize`` and one ``tools/call`` with the
#: two pointers it sees, then exits.
_FAKE_SERVER = r"""
import json, os, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "tools/call":
        seen = {k: os.environ.get(k, "<missing>") for k in ("KRB5CCNAME", "SSH_AUTH_SOCK")}
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": seen}), flush=True)
        break
    print(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}), flush=True)
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    data_home = tmp_path / "crew-home"
    data_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(data_home))
    return data_home


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "fake_auth_server.py"
    path.write_text(_FAKE_SERVER, encoding="utf-8")
    return path


class _Gateway:
    """The gateway environment one test sets, and the forward verdict it gives."""

    def __init__(self, tmp_path):
        self.sock = str(tmp_path / SOCK_LEAF)
        self.ccache = f"FILE:{tmp_path / CACHE_LEAF}"
        self.forward = True
        self.verdicts: list[tuple[str, tuple[str, ...]]] = []


@pytest.fixture
def relay(tmp_path, monkeypatch):
    """The real relay, over a gateway holding an ssh-agent socket and a FILE cache."""
    gateway = _Gateway(tmp_path)
    monkeypatch.setattr(kas_env_launch, "inherited_auth_env", _REAL_INHERITED_AUTH_ENV)
    monkeypatch.setenv("SSH_AUTH_SOCK", gateway.sock)
    monkeypatch.setenv("KRB5CCNAME", gateway.ccache)

    def verdict(mode, hidden_dirs):
        gateway.verdicts.append((mode, tuple(hidden_dirs)))
        return True

    # Re-deriving the verdict now would say "forward": the relay must not ask.
    monkeypatch.setattr(sandbox, "_forward_ssh_auth_sock", verdict)
    return gateway


def _bare(script) -> dict:
    return {"command": sys.executable, "args": [str(script)]}


def _project(
    servers: dict, session_key: str = SESSION, forward_ssh_auth_sock: bool = True, **kwargs
) -> dict:
    spec = _spec(mcpServers={"kirocrew-core": {"command": "x"}, **servers})
    return to_client_custom_agent(
        "kirocrew",
        spec,
        "p",
        session_key=session_key,
        forward_ssh_auth_sock=forward_ssh_auth_sock,
        **kwargs,
    )


def _seen(entry: dict) -> dict:
    """The two pointers the started server reported."""
    result = _run_entry(entry)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])["result"]


def _snapshot_env(home, entry: dict) -> dict:
    return json.loads(_snapshot(home, entry).read_text(encoding="utf-8"))["env"]


class TestProjection:
    def test_both_pointers_reach_a_server_with_no_env_and_never_the_wire(self, home, script, relay):
        agent = _project({"plain": _bare(script)})
        launched = agent["mcpServers"]["plain"]
        assert kas_env_shim.SHIM_MODULE in launched["args"], "started through the launcher"
        wire = _wire_text(agent)
        assert relay.sock not in wire and relay.ccache not in wire, "no pointer on the wire"
        assert _seen(launched) == {"KRB5CCNAME": relay.ccache, "SSH_AUTH_SOCK": relay.sock}

    def test_the_socket_follows_the_engines_own_launch_verdict(self, home, script, relay):
        """Withheld from the engine's spawn, so withheld from its servers, even
        though a verdict re-derived now (config or floor loosened since) says yes."""
        launched = _project({"plain": _bare(script)}, forward_ssh_auth_sock=False)
        entry = launched["mcpServers"]["plain"]
        assert _snapshot_env(home, entry) == {"KRB5CCNAME": relay.ccache}
        assert relay.verdicts == [], "the verdict is never re-derived"
        assert _seen(entry)["SSH_AUTH_SOCK"] == "<missing>"

    def test_a_malformed_env_stays_reported_when_only_pointers_launch_it(self, home, script, relay):
        withheld: dict = {}
        entry = {**_bare(script), "env": ["A=B"]}
        agent = _project({"bad": entry}, withheld_out=withheld)
        assert kas_env_shim.SHIM_MODULE in agent["mcpServers"]["bad"]["args"]
        assert "env" in withheld["bad"][1], "the dropped env is still named"

    def test_a_value_the_server_declares_wins(self, home, script, relay):
        own = {**_bare(script), "env": {"KRB5CCNAME": "FILE:/spec/own-cache"}}
        launched = _project({"own": own})["mcpServers"]["own"]
        assert _seen(launched) == {
            "KRB5CCNAME": "FILE:/spec/own-cache",
            "SSH_AUTH_SOCK": relay.sock,
        }

    def test_a_reference_the_wire_keeps_is_not_overridden(self, home, script, relay, monkeypatch):
        from kiro_crew.platform.defaults import DefaultMcpToolingProvider

        monkeypatch.setattr(
            DefaultMcpToolingProvider,
            "kas_relayed_env_references",
            lambda self: frozenset({"SSH_AUTH_SOCK"}),
            raising=False,
        )
        entry = {**_bare(script), "env": {"SSH_AUTH_SOCK": "${SSH_AUTH_SOCK}"}}
        launched = _project({"ref": entry})["mcpServers"]["ref"]
        assert launched["env"]["SSH_AUTH_SOCK"] == "${SSH_AUTH_SOCK}"
        assert _snapshot_env(home, launched) == {"KRB5CCNAME": relay.ccache}

    def test_a_managed_and_a_pooled_server_get_nothing(self, home, script, relay):
        agent = _project({"pooled": _bare(script)}, stub_server_names=frozenset({"pooled"}))
        assert "pooled" not in agent["mcpServers"], "declared by the session, not here"
        core = agent["mcpServers"]["kirocrew-core"]
        assert core["command"] == "x"
        assert not {"KRB5CCNAME", "SSH_AUTH_SOCK"} & set(core.get("env") or {})
        assert not (home / kas_env_shim.SNAPSHOT_LEAF).exists()

    def test_a_remote_server_is_left_as_projected(self, home, relay):
        agent = _project({"remote": {"url": "https://x.invalid/mcp"}})
        assert agent["mcpServers"]["remote"] == {"url": "https://x.invalid/mcp"}

    def test_no_session_key_relays_nothing(self, home, script, relay):
        agent = _project({"plain": _bare(script)}, session_key="")
        assert agent["mcpServers"]["plain"] == _bare(script)

    def test_an_unwritable_snapshot_leaves_a_server_with_nothing_of_its_own_bare(
        self, home, script, relay, monkeypatch
    ):
        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(kas_env_launch, "atomic_write", boom)
        launched = _project({"plain": _bare(script)})["mcpServers"]["plain"]
        assert launched == _bare(script), "it starts as it does without the pointers"
        assert _seen(launched) == {"KRB5CCNAME": "<missing>", "SSH_AUTH_SOCK": "<missing>"}

    def test_an_unwritable_snapshot_still_refuses_a_server_with_withheld_env(
        self, home, script, relay, monkeypatch
    ):
        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(kas_env_launch, "atomic_write", boom)
        entry = {**_bare(script), "env": {"API_TOKEN": "s3cr3t-kas-auth"}}
        launched = _project({"own": entry})["mcpServers"]["own"]
        assert kas_env_shim.SHIM_MODULE in launched["args"]
        assert str(script) not in launched["args"]
        result = _run_entry(launched)
        assert result.returncode == 78, result.stderr


class TestInheritedAuthEnv:
    @pytest.fixture
    def gateway(self, monkeypatch, tmp_path):
        monkeypatch.setattr(kas_env_launch, "inherited_auth_env", _REAL_INHERITED_AUTH_ENV)
        monkeypatch.delenv("KRB5CCNAME", raising=False)
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        cache = str(tmp_path / CACHE_LEAF)
        monkeypatch.setattr(env_mod, "usable_file_ccache", lambda: cache)
        return cache

    @pytest.mark.parametrize("keyring", [None, "KEYRING:persistent:1000"])
    def test_linux_points_an_unset_or_keyring_cache_at_the_users_file_cache(
        self, gateway, monkeypatch, keyring
    ):
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        if keyring:
            monkeypatch.setenv("KRB5CCNAME", keyring)
        assert kas_env_launch.inherited_auth_env(True) == {"KRB5CCNAME": f"FILE:{gateway}"}

    def test_another_platform_gets_no_cache_redirect(self, gateway, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_LINUX", False)
        assert kas_env_launch.inherited_auth_env(True) == {}

    @pytest.mark.parametrize("native", ["API:501", "KCM:", "DIR:/run/user/1/krb5cc"])
    def test_a_native_cache_passes_through_unchanged(self, gateway, monkeypatch, native):
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        monkeypatch.setenv("KRB5CCNAME", native)
        assert kas_env_launch.inherited_auth_env(True) == {"KRB5CCNAME": native}

    def test_a_socket_is_relayed_only_with_the_forward(self, gateway, monkeypatch, tmp_path):
        monkeypatch.setattr(platform_compat, "IS_LINUX", False)
        monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / SOCK_LEAF))
        assert kas_env_launch.inherited_auth_env(True) == {
            "SSH_AUTH_SOCK": str(tmp_path / SOCK_LEAF)
        }
        assert kas_env_launch.inherited_auth_env(False) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("forwarded", [True, False])
async def test_the_runtime_hands_the_harness_its_launch_verdict(forwarded):
    """The verdict the launch tail applied to the engine reaches session_extras; a
    runtime that never launched (no attribute) hands False."""
    from types import SimpleNamespace

    from kiro_crew.acp import runtime_session_setup
    from kiro_crew.acp.harness import SessionExtras

    seen: dict = {}

    async def session_extras(agent, **kw):
        seen.update(kw)
        return SessionExtras()

    runtime = SimpleNamespace(
        _agent_capabilities={},
        _harness=SimpleNamespace(session_extras=session_extras),
        _mcp_gateway_overlay=None,
        _work_dir=None,
        _refuse_if_loader_unreachable=lambda *_a: None,
        _ssh_auth_sock_forwarded=forwarded,
    )
    await runtime_session_setup._kas_custom_agents(runtime, "a")
    assert seen["ssh_auth_sock_forwarded"] is forwarded
    del runtime._ssh_auth_sock_forwarded
    await runtime_session_setup._kas_custom_agents(runtime, "a")
    assert seen["ssh_auth_sock_forwarded"] is False


@pytest.mark.parametrize(
    ("launch", "now", "spawn_mode", "relayed"),
    [
        (True, True, "strict", True),
        # A floor was raised or a consent revoked since the launch.
        (True, False, "strict", False),
        # The engine (or its sandbox wrap) was denied it: a verdict re-derived now
        # never re-adds it.
        (False, True, "strict", False),
        # No spawn recorded a tier.
        (True, True, None, False),
    ],
)
def test_the_kas_harness_relays_the_socket_only_when_both_verdicts_agree(
    tmp_path, monkeypatch, launch, now, spawn_mode, relayed
):
    import asyncio

    from test_worker_agent_spawn_gate import _DEFAULT_SPEC_ON_DISK, AGENT_FILENAME

    from kiro_crew import agent, agent_state
    from kiro_crew.acp import kas_agents as kas_agents_mod
    from kiro_crew.acp.harness import harness_for
    from kiro_crew.acp.types import ACP_BACKEND_KAS

    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
    monkeypatch.setattr(agent_state, "config_dir", lambda: tmp_path)
    (tmp_path / AGENT_FILENAME).write_text(json.dumps(_DEFAULT_SPEC_ON_DISK), encoding="utf-8")
    agent._install_worker_agent()
    import kiro_crew.config.paths as paths_mod

    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(agent, "require_fork_governance", lambda a, w: None)
    judged: list[tuple] = []
    monkeypatch.setattr(
        sandbox, "_forward_ssh_auth_sock", lambda mode, hidden: judged.append((mode, hidden)) or now
    )
    seen: list[bool] = []
    real_build = kas_agents_mod.build_kas_custom_agents

    def _capture(agents_dir, agent_id, spec, **kwargs):
        seen.append(kwargs["forward_ssh_auth_sock"])
        return real_build(agents_dir, agent_id, spec, **kwargs)

    monkeypatch.setattr(kas_agents_mod, "build_kas_custom_agents", _capture)
    harness = harness_for(ACP_BACKEND_KAS)
    harness.spawn_sandbox_mode = spawn_mode
    asyncio.run(
        harness.session_extras(
            "kirocrew-worker", work_dir=str(tmp_path / "wd"), ssh_auth_sock_forwarded=launch
        )
    )
    assert seen == [relayed]
    assert judged in ([], [("strict", ())])
