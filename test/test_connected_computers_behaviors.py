"""Connected computers: B03, B04, B05, B08 through the real seams, and old providers.

* B03 -- the chat's file and command tools open only on the computer it is bound
  to, the gateway names no command or path, and the agent loop, memory and the
  gateway's own servers stay on the remote gateway.
* B04 -- an exposed server or workspace tool is never auto-approved, so a
  declined approval leaves the call unsent; nothing starts on the computer before
  the bridge dials.
* B05 -- every child starts through the confinement plan on the granted root;
  the tool server refuses symlink and ``..`` escapes; only the root and the
  scratch are writable inside the sandbox.
* B08 -- a lost computer never moves work to another computer and a lost link
  never redials.
* B14 -- exposed servers reach the agent only through its own grants.
* Old providers -- Coder / VM sessions keep their servers, wording, token
  writing, choice providers and launch route.

Authored for CI; this host runs no tests locally.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from test_connected_computers_broker import CALL, FakeSocket, _record
from test_connected_computers_local import FakeSession, FakeWs, _grant, _hello

from kiro_crew.connected_computers import protocol
from kiro_crew.connected_computers.broker import ConnectionBroker, DeviceConnection, DeviceOffline
from kiro_crew.connected_computers.connector import Connector, ConnectorRefused
from kiro_crew.connected_computers.launch import ConnectedComputerLaunch
from kiro_crew.connected_computers.local_store import ExposedMcp
from kiro_crew.remote_session.link import LinkUnavailable
from kiro_crew.testing.wait import async_wait_until

pytestmark = pytest.mark.asyncio


def _connect(broker: ConnectionBroker, device_id: str):
    sock = FakeSocket()
    conn = DeviceConnection(_record(device_id), sock.send, root="/work", closer=sock.close)
    broker.attach(conn)
    return conn, sock


async def _dial_open(launch: ConnectedComputerLaunch, conn, sock, kind=protocol.KIND_WORKSPACE):
    port = await launch.start_worker(f"w-{kind}", [kind], b"unused")
    task = asyncio.ensure_future(launch.dial(port)())
    await async_wait_until(lambda: sock.ops("open"))
    conn.handle({"op": "opened", "ch": sock.ops("open")[-1]["ch"]})
    return await asyncio.wait_for(task, 30)


# -- B03 / B09: only the bound computer runs the chat's tools -------------------------


async def test_b03_b09_tool_servers_open_only_on_the_bound_computer():
    broker = ConnectionBroker()
    _conn_a, sock_a = _connect(broker, "laptop")
    conn_b, sock_b = _connect(broker, "laptop-2")
    launch = ConnectedComputerLaunch("laptop-2", "Laptop", session_key="chat-1", broker=broker)

    port = await launch.start_worker("w1", [protocol.KIND_WORKSPACE], b"unused")
    # B04: recording a worker starts nothing on any computer.
    assert sock_a.frames == [] and sock_b.frames == []

    task = asyncio.ensure_future(launch.dial(port)())
    await async_wait_until(lambda: sock_b.ops("open"))
    conn_b.handle({"op": "opened", "ch": sock_b.ops("open")[-1]["ch"]})
    await asyncio.wait_for(task, 30)

    assert [f["kind"] for f in sock_b.ops("open")] == [protocol.KIND_WORKSPACE]
    assert sock_a.frames == [], "a computer with the same label is never asked"


@pytest.mark.parametrize(
    "argv",
    [
        ["/bin/sh", "-c", "id"],
        ["workspace", "--root", "/etc"],
        ["/bin/sh"],
        ["mcp:../escape"],
    ],
)
async def test_b03_the_gateway_names_no_command_or_path(argv):
    launch = ConnectedComputerLaunch("laptop", "Laptop", session_key="chat-1")
    with pytest.raises((ValueError, protocol.ProtocolError)):
        await launch.start_worker("w1", argv, b"unused")


def test_b03_the_session_gets_the_computers_tools_and_keeps_the_gateways_own():
    from kiro_crew.acp.runtime_remote_workspace import _remote_session_servers
    from kiro_crew.session_environment import LOCAL_ONLY_SERVERS

    local_only = sorted(LOCAL_ONLY_SERVERS)[0]
    workspace = {
        "server": {"name": "workspace", "command": "bridge"},
        "extra_servers": [{"server": {"name": "computer-notes", "command": "bridge"}}],
    }
    runtime = SimpleNamespace(_remote_workspace=workspace)
    servers = [
        {"name": "kirocrew-core"},
        {"name": local_only},
        {"name": "workspace", "command": "stale"},
        {"name": "computer-notes", "command": "stale"},
    ]
    sent = _remote_session_servers(runtime, servers)  # type: ignore[arg-type]
    assert [s["name"] for s in sent] == ["kirocrew-core", "workspace", "computer-notes"]
    assert all(s.get("command") != "stale" for s in sent)


def test_b03_built_in_file_and_shell_tools_become_the_computers():
    from kiro_crew.acp.runtime_remote_workspace import _remote_workspace_agents
    from kiro_crew.connected_computers.provider import notice_for

    notice = notice_for("Laptop", "/work/proj")
    runtime = SimpleNamespace(
        _remote_workspace={
            "server": {"name": "workspace"},
            "workdir": "/work/proj",
            "notice": notice,
        }
    )
    [agent] = _remote_workspace_agents(
        runtime, [{"name": "a", "tools": ["@builtin"], "prompt": "base"}], []  # type: ignore[arg-type]
    )
    from kiro_crew.agent_sdk.tool_search import TOOL_SEARCH_LOADER_TOOL

    # ``@builtin`` also mounts the tool_search loader, which acts on nothing.
    assert [t for t in agent["tools"] if t != TOOL_SEARCH_LOADER_TOOL] == ["@workspace"]
    assert agent["prompt"] == "base" + notice
    assert "gateway's own file and shell tools are not available" in notice


async def test_b03_a_connected_choice_starts_on_its_computer_and_reads_no_profile(
    tmp_path, monkeypatch
):
    from kiro_crew.coder import session_launch
    from kiro_crew.config import live
    from kiro_crew.connected_computers import provider
    from kiro_crew.session_environment import DEVELOPER_FLAG, clear_start_stage

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    # A recorded choice is read only behind Session Environments' developer flag.
    monkeypatch.setenv(DEVELOPER_FLAG, "1")
    (tmp_path / "coder").mkdir()
    session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json").set(
        "chat-1", "laptop-2", session_launch.CONNECTED_COMPUTER_PROVIDER_ID
    )
    seen: list[tuple[str, str]] = []
    prepared = object()

    async def fake_prepare(session_key: str, device_id: str) -> Any:
        seen.append((session_key, device_id))
        return prepared

    def no_profiles() -> Any:
        raise AssertionError("a connected computer is not a configured profile")

    monkeypatch.setattr(provider, "prepare_connected_session", fake_prepare)
    monkeypatch.setattr(live, "snapshot", no_profiles)
    try:
        assert await session_launch.prepare_for_session("chat-1") is prepared
    finally:
        clear_start_stage("chat-1")
    assert seen == [("chat-1", "laptop-2")]


# -- B04: never auto-approved; a declined call is never sent --------------------------


@pytest.mark.parametrize(
    "ref", ["@computer-notes", "@computer-notes/write_note", "@workspace", "@workspace/run_command"]
)
def test_b04_the_computers_tools_always_meet_the_gate(ref):
    from kiro_crew.platform.governance import may_skip_gate

    assert may_skip_gate(ref, None) is False


def test_b04_old_servers_keep_their_auto_approve():
    from kiro_crew.platform.governance import may_skip_gate

    assert may_skip_gate("@notes", None) is True
    assert may_skip_gate("@computernotes", None) is True


async def test_b04_a_call_the_bridge_never_wrote_never_reaches_the_computer():
    broker = ConnectionBroker()
    conn, sock = _connect(broker, "laptop")
    launch = ConnectedComputerLaunch("laptop", "Laptop", session_key="chat-1", broker=broker)
    port = await launch.start_worker("w1", [protocol.KIND_WORKSPACE], b"unused")
    link = launch.make_link("w1", b"unused", launch.dial(port), **launch.link_kwargs())
    link.start()
    await async_wait_until(lambda: sock.ops("open"))
    conn.handle({"op": "opened", "ch": sock.ops("open")[-1]["ch"]})
    await asyncio.wait_for(link.wait_connected(30), 30)

    # The approval was declined, so the bridge wrote nothing; the session ends.
    await launch.stop_worker("w1")
    assert sock.ops("data") == []
    assert len(link.pending) == 0
    assert sock.ops("close"), "the computer is told to stop the idle tool server"


# -- B05: the granted root, confinement for every child, no escapes -------------------


def _connector(grant, ws, plan) -> Connector:
    return Connector(
        grant,
        url="ws://127.0.0.1:1/x",
        plan=plan,
        grant_still_active=lambda: True,
        session_factory=lambda: FakeSession(ws),
    )


class _RecordingPlan:
    """Records what it was asked to wrap, then refuses to start it."""

    name = "recording-plan"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def wrap(self, argv, *, root, scratch, reads=(), env=None):
        self.calls.append({"argv": list(argv), "root": root, "env": dict(env or {})})
        raise RuntimeError("not started in this test")


NOTES = ExposedMcp(name="notes", command="/opt/notes-mcp", args=("--db", "n.db"), env=(("K", "v"),))


@pytest.mark.parametrize("kind", ["workspace", "mcp:notes"])
async def test_b05_every_child_goes_through_the_plan_on_the_granted_root(monkeypatch, kind):
    from kiro_crew.connected_computers import flag, local_mcp
    from kiro_crew.mcp_discovery_server_info import McpServerInfo

    monkeypatch.setattr(flag, "local_enabled", lambda cfg=None: True)
    # Each start re-reads the local MCP config (B14 live limits); still there.
    monkeypatch.setattr(
        local_mcp,
        "configured_servers",
        lambda: [McpServerInfo(name="notes", command="/opt/notes-mcp", args=["--db", "n.db"])],
    )
    grant = _grant(root="/work/proj", mcp=(NOTES,))
    ws = FakeWs([_hello(grant), json.dumps({"op": "open", "ch": 1, "kind": kind})])
    plan = _RecordingPlan()
    await asyncio.wait_for(_connector(grant, ws, plan).run(), 30)

    [call] = plan.calls
    assert call["root"] == "/work/proj"
    if kind == "workspace":
        assert call["argv"][-2:] == ["--root", "/work/proj"]
        assert call["argv"][-3].endswith("workspace_tools.py")
        assert call["env"] == {}
    else:
        assert call["argv"] == ["/opt/notes-mcp", "--db", "n.db"]
        assert call["env"] == {"K": "v"}
    # B06: a plan that cannot start it refuses; nothing runs unconfined.
    [refused] = [f for f in ws.sent if f["op"] == "refused"]
    assert "confined" in refused["reason"]
    assert not [f for f in ws.sent if f["op"] == "opened"]


def test_b05_an_unshared_server_has_no_argv():
    connector = _connector(_grant(mcp=(NOTES,)), FakeWs([]), _RecordingPlan())
    with pytest.raises(ConnectorRefused, match="not shared"):
        connector._argv_for("mcp:browser")


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "proj"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("keep out")
    (root / "inside.txt").write_text("ok")
    (root / "link-out").symlink_to(outside, target_is_directory=True)
    (root / "file-out").symlink_to(outside / "secret.txt")
    (root / "link-in").symlink_to(root / "inside.txt")
    return root, outside


@pytest.mark.parametrize(
    "path", ["file-out", "link-out/secret.txt", "../outside/secret.txt", "{outside}/secret.txt"]
)
def test_b05_the_tool_server_refuses_reads_out_of_the_root(tree, path):
    from kiro_crew.remote_session.workspace_tools import ToolError, Workspace

    root, outside = tree
    ws = Workspace(str(root))
    with pytest.raises(ToolError, match="outside the workspace"):
        ws.read_file(path.format(outside=outside))


def test_b05_the_tool_server_refuses_writes_and_cwds_out_of_the_root(tree):
    from kiro_crew.remote_session.workspace_tools import ToolError, Workspace

    root, outside = tree
    ws = Workspace(str(root))
    with pytest.raises(ToolError):
        ws.write_file("link-out/new.txt", "x")
    assert not (outside / "new.txt").exists()
    with pytest.raises(ToolError, match="outside the workspace"):
        ws.run_command("true", cwd="link-out")


def test_b05_a_symlink_inside_the_root_still_works(tree):
    from kiro_crew.remote_session.workspace_tools import Workspace

    root, _outside = tree
    assert Workspace(str(root)).read_file("link-in") == "ok"


def test_b05_only_the_root_and_scratch_are_writable_in_the_linux_sandbox(tmp_path):
    from kiro_crew.connected_computers.confinement import LinuxPlan

    root = tmp_path / "proj"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    confined = LinuxPlan("/usr/bin/bwrap", machine="x86_64").wrap(
        ["/usr/bin/tool", "--x"], root=str(root), scratch=str(scratch)
    )
    try:
        argv = confined.argv
        writable = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
        assert writable == [str(scratch), str(root)]
        # Every descendant shares the namespaces and dies with the sandbox.
        for flag_name in ("--unshare-pid", "--die-with-parent", "--new-session"):
            assert flag_name in argv
        assert os.path.expanduser("~") not in writable
        assert argv[argv.index("--") + 1 :] == ["/usr/bin/tool", "--x"]
    finally:
        confined.close()


@pytest.mark.skipif(sys.platform == "win32", reason="the macOS profile is built from POSIX paths")
def test_b05_the_macos_profile_writes_only_the_root_scratch_and_devices(tmp_path):
    from kiro_crew.connected_computers.confinement import macos_profile

    root, scratch = "/Users/me/proj", "/private/tmp/kc-1"
    profile = macos_profile(root=root, scratch=scratch, reads=())
    for line in profile.splitlines():
        if "file-write" in line:
            assert root in line or scratch in line or '"/dev/' in line, line
    assert "(deny default)" in profile


# -- B08: no target switch, no redial ----------------------------------------------------


async def test_b08_a_lost_computer_never_moves_work_to_another():
    broker = ConnectionBroker()
    conn_a, sock_a = _connect(broker, "laptop")
    _conn_b, sock_b = _connect(broker, "laptop-2")
    launch = ConnectedComputerLaunch("laptop", "Laptop", session_key="chat-1", broker=broker)
    port = await launch.start_worker("w1", [protocol.KIND_WORKSPACE], b"unused")
    link = launch.make_link("w1", b"unused", launch.dial(port), **launch.link_kwargs())
    link.start()
    await async_wait_until(lambda: sock_a.ops("open"))
    conn_a.handle({"op": "opened", "ch": sock_a.ops("open")[-1]["ch"]})
    await asyncio.wait_for(link.wait_connected(30), 30)
    await link.write(CALL)

    broker.detach(conn_a, "the socket dropped")
    answer = json.loads((await asyncio.wait_for(link.stdout.read(1 << 20), 30)).decode())
    assert answer["result"]["isError"] is True

    with pytest.raises(LinkUnavailable):
        await link.write(CALL)
    with pytest.raises(DeviceOffline):
        await launch.workspace_server("/work")
    assert sock_b.frames == [], "the other computer never receives the call"


async def test_b08_a_lost_link_never_redials():
    broker = ConnectionBroker()
    conn, sock = _connect(broker, "laptop")
    launch = ConnectedComputerLaunch("laptop", "Laptop", session_key="chat-1", broker=broker)
    port = await launch.start_worker("w1", [protocol.KIND_WORKSPACE], b"unused")
    dial = launch.dial(port)
    dials = []

    async def counted() -> Any:
        dials.append(1)
        return await dial()

    link = launch.make_link("w1", b"unused", counted, **launch.link_kwargs())
    link.start()
    await async_wait_until(lambda: sock.ops("open"))
    conn.handle({"op": "opened", "ch": sock.ops("open")[-1]["ch"]})
    await asyncio.wait_for(link.wait_connected(30), 30)
    broker.detach(conn, "the socket dropped")
    _connect(broker, "laptop")  # the same computer comes back
    with pytest.raises(LinkUnavailable):
        await link.write(CALL)
    assert dials == [1]


# -- B14: exposed servers reach the agent through its own grants ----------------------


def _agents(tools: Any, servers: list[dict[str, Any]]) -> dict[str, Any]:
    from kiro_crew.acp.runtime_remote_workspace import _remote_workspace_agents

    runtime = SimpleNamespace(
        _remote_workspace={
            "server": {"name": "workspace"},
            "workdir": "/work",
            "extra_servers": [{"server": {"name": "computer-notes"}}],
            "notice": "\n\nnotice",
        }
    )
    [agent] = _remote_workspace_agents(
        runtime, [{"name": "a", "tools": tools, "prompt": ""}], servers  # type: ignore[arg-type]
    )
    return agent


def test_b14_an_agent_granted_every_server_gets_the_exposed_one():
    servers = [{"name": "workspace"}, {"name": "computer-notes"}]
    assert "@computer-notes" in _agents("*", servers)["tools"]


def test_b14_an_agent_that_names_the_exposed_server_keeps_it():
    assert "@computer-notes" in _agents(["@builtin", "@computer-notes"], [])["tools"]


def test_b14_an_agent_that_does_not_grant_it_does_not_gain_it():
    assert "@computer-notes" not in _agents(["@builtin"], [])["tools"]


# -- old providers stay unchanged -------------------------------------------------------


def test_old_providers_choice_providers_only_add_the_connected_computer():
    from kiro_crew.coder.session_launch import CHOICE_PROVIDERS, CONNECTED_COMPUTER_PROVIDER_ID
    from kiro_crew.config.session_environments import PROVIDER_IDS

    assert set(PROVIDER_IDS) <= CHOICE_PROVIDERS
    assert CHOICE_PROVIDERS - set(PROVIDER_IDS) == {CONNECTED_COMPUTER_PROVIDER_ID}


def test_old_providers_session_servers_are_unchanged():
    from kiro_crew.acp.runtime_remote_workspace import _remote_session_servers

    entry = {"name": "workspace", "command": "coder-bridge"}
    runtime = SimpleNamespace(_remote_workspace={"server": entry})
    servers = [{"name": "kirocrew-core"}, {"name": "github"}, {"name": "workspace"}]
    sent = _remote_session_servers(runtime, servers)  # type: ignore[arg-type]
    assert sent == [{"name": "kirocrew-core"}, {"name": "github"}, entry]


def test_old_providers_keep_the_remote_workspace_wording():
    from kiro_crew.acp.runtime_remote_workspace import _remote_workspace_agents

    runtime = SimpleNamespace(
        _remote_workspace={"server": {"name": "workspace"}, "workdir": "/srv/repo"}
    )
    [agent] = _remote_workspace_agents(
        runtime, [{"name": "a", "tools": ["@builtin"], "prompt": ""}], []  # type: ignore[arg-type]
    )
    assert "remote workspace at /srv/repo" in agent["prompt"]
    assert "user's own computer" not in agent["prompt"]


async def test_old_providers_write_one_token_and_no_extras(tmp_path):
    from kiro_crew.acp.runtime_remote_workspace import _write_remote_token

    written: list[str] = []

    def write_token(scratch: Any) -> str:
        written.append(str(scratch))
        return str(tmp_path / "token")

    workspace = {
        "server": {"name": "workspace", "args": ["--token-file", ""]},
        "write_token": write_token,
    }
    runtime = SimpleNamespace(_remote_workspace=workspace, work_scratch_dir=tmp_path)
    await _write_remote_token(runtime)  # type: ignore[arg-type]
    assert written == [str(tmp_path)]
    assert workspace["server"]["args"] == ["--token-file", str(tmp_path / "token")]
    assert "extra_servers" not in workspace


async def test_old_providers_a_coder_choice_never_reaches_the_connected_provider(
    tmp_path, monkeypatch
):
    from kiro_crew.coder import session_launch
    from kiro_crew.config import live
    from kiro_crew.connected_computers import provider
    from kiro_crew.session_environment import DEVELOPER_FLAG, clear_start_stage

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    # A recorded choice is read only behind Session Environments' developer flag.
    monkeypatch.setenv(DEVELOPER_FLAG, "1")
    (tmp_path / "coder").mkdir()
    session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json").set(
        "chat-1", "dev-box", session_launch.PROVIDER_ID
    )

    class _Reached(Exception):
        pass

    async def never(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("a Coder session is not a connected computer")

    def reached() -> Any:
        raise _Reached()

    monkeypatch.setattr(provider, "prepare_connected_session", never)
    monkeypatch.setattr(live, "snapshot", reached)
    try:
        with pytest.raises(_Reached):
            await session_launch.prepare_for_session("chat-1")
    finally:
        clear_start_stage("chat-1")
