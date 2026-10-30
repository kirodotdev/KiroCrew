"""End-to-end remote session launch: bootstrap, link, runtime seam.

The guest is a temporary HOME reached through ``sh -c`` running the scripts
``coder ssh`` would; link, relay and bridge are real, and the fake engine asks
for an access token as kiro-cli does, then echoes.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import runpy
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.acp import runtime as runtime_mod
from kiro_crew.acp.kas_permissions import KAS_TOOL_IDS_BY_KIRO_TOOL
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.coder import remote_worker
from kiro_crew.remote_session import link as link_mod
from kiro_crew.remote_session import workspace_tools
from kiro_crew.remote_session.link import RemoteLink
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest shell")
# The guest's worker-identity check reads /proc/<pid>/cmdline; Coder guests are Linux.
needs_proc = pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="Linux guest /proc")


@pytest.fixture(autouse=True)
def _fresh_worker_records(_floor_monkeypatch):
    # The reaped-binding and unconfirmed-stop records are process-wide; each test
    # starts from none, so a rerun in one process sees what a first run sees.
    _floor_monkeypatch.setattr(remote_worker, "_REAPED", set())
    _floor_monkeypatch.setattr(remote_worker, "_UNCONFIRMED_STOPS", {})


SRC = str(Path(remote_worker.__file__).resolve().parents[2])

ENGINE = r"""
import json, os, sys
out = sys.stdout
def send(msg):
    out.write(json.dumps(msg) + "\n"); out.flush()
send({"jsonrpc": "2.0", "id": 900, "method": "_kiro/auth/getAccessToken", "params": {}})
token = json.loads(sys.stdin.readline())["result"]["accessToken"]
send({"jsonrpc": "2.0", "method": "probe/env", "params": {
    "api_key": os.environ.get("KIRO_API_KEY", "absent"), "token_len": len(token)}})
for line in sys.stdin:
    out.write(line); out.flush()
"""


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 90))

    return wrapper


class LocalShell:
    """A guest with tmp-owned HOME, service commands and bus locators."""

    def __init__(self, home: Path, path_prefix: str = "", *, service_launch: bool = False) -> None:
        self.home = home
        self.bin = home / "guest-bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.runtime_dir = home / "runtime"
        self.runtime_dir.mkdir(exist_ok=True)
        parts = (str(self.bin), path_prefix, str(Path(sys.executable).parent), os.environ["PATH"])
        self.path = os.pathsep.join(filter(None, parts))
        systemctl = self.bin / "systemctl"
        systemctl.write_text(
            '#!/bin/sh\nprintf "%s\\n" "systemctl $*" >> "$HOME/systemd-calls"\n'
            + ('[ "$*" = "--user show-environment" ]\n' if service_launch else "exit 1\n")
        )
        systemd_run = self.bin / "systemd-run"
        systemd_run.write_text(
            '#!/bin/sh\nprintf "%s\\n" "systemd-run $*" >> "$HOME/systemd-calls"\n'
            'while [ "$#" -gt 0 ]; do\n'
            '  case "$1" in\n'
            "    --user|--collect|--quiet|--unit=*) shift;;\n"
            "    -E|-p) shift 2;;\n"
            "    *) break;;\n"
            "  esac\n"
            "done\n"
            "if command -v setsid >/dev/null 2>&1; then L=setsid; else L=; fi\n"
            'nohup $L "$@" >>"$HOME/service.log" 2>&1 </dev/null &\n'
        )
        systemctl.chmod(0o700)
        systemd_run.chmod(0o700)

    def _guest_env(self) -> dict:
        return {
            "HOME": str(self.home),
            "PATH": self.path,
            "XDG_RUNTIME_DIR": str(self.runtime_dir),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.runtime_dir / 'bus'}",
            "KIRO_API_KEY": "must-not-leak",
        }

    def _env(self) -> dict:
        return {**os.environ, **self._guest_env()}

    async def run(self, command: str, stdin: bytes = b"") -> bytes:
        return await remote_worker.run_collect(["sh", "-c", command], stdin, self._env())

    def argv(self, command: str) -> list[str]:
        pairs = [f"{name}={value}" for name, value in self._guest_env().items()]
        return ["env", *pairs, "sh", "-c", command]


@sync
async def test_install_writes_every_worker_file_in_one_round_trip(tmp_path):
    """Each ``coder ssh`` costs seconds on the start path, so install is one call."""
    shell = LocalShell(tmp_path / "guest")
    commands: list[str] = []
    real_run = shell.run

    async def counting_run(command, stdin=b""):
        commands.append(command)
        return await real_run(command, stdin)

    shell.run = counting_run
    launch = remote_worker.RemoteWorkerLaunch("sess-inst", shell)
    await launch.install()
    assert len(commands) == 1
    here = Path(remote_worker.wire.__file__).resolve().parent
    guest = tmp_path / "guest" / ".kirocrew-worker"
    for name in remote_worker._WORKER_FILES:
        assert (guest / name).read_bytes() == (here / name).read_bytes()
        assert stat_mode(guest / name) == 0o600
        assert not (guest / f"{name}.tmp").exists()


def test_install_payload_refuses_a_file_outside_the_worker_set():
    with pytest.raises(ValueError):
        remote_worker._install_payload({"../evil.py": b"x"})


def _old_guest_python_dir(tmp_path: Path) -> str:
    """A test-owned bin dir holding the guest image's Python 3.9 ``python3``."""
    interpreter = shutil.which("python3.9")
    if interpreter is None:
        pytest.skip("no python3.9 on this host")
    shim_dir = tmp_path / "py39-shim"
    shim_dir.mkdir(mode=0o700)
    shim = shim_dir / "python3"
    shim.write_text(f'#!/bin/sh\nexec "{interpreter}" "$@"\n')
    shim.chmod(0o700)
    return str(shim_dir)


def test_collection_does_not_write_python_shims(tmp_path, monkeypatch):
    real_which = shutil.which
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(
        shutil, "which", lambda name: sys.executable if name == "python3.9" else real_which(name)
    )
    runpy.run_path(str(Path(__file__)))
    assert list(tmp_path.iterdir()) == []


def _launch(
    tmp_path: Path, sid: str = "sess-e2e", *, service_launch: bool = False, path_prefix: str = ""
) -> remote_worker.RemoteWorkerLaunch:
    return remote_worker.RemoteWorkerLaunch(
        sid,
        LocalShell(tmp_path / "guest", path_prefix, service_launch=service_launch),
        orphan_timeout=60,
        link_options={"keepalive": 0.5, "stall": 5, "lease": 20},
    )


def _with_token(launch, server: dict, scratch: Path) -> dict:
    """``server`` pointing at a token file the runtime would write into ``scratch``."""
    scratch.mkdir(mode=0o700, exist_ok=True)
    return {**server, "args": [*server["args"][:-1], str(launch.relay.write_token(scratch))]}


async def _spawn_bridge(argv: list[str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": SRC},
        limit=1024 * 1024,
    )


async def _readjson(proc) -> dict:
    return json.loads(await asyncio.wait_for(proc.stdout.readline(), 30))


def _link(
    launch: remote_worker.RemoteWorkerLaunch, worker_id: str, secret: bytes, port: int
) -> RemoteLink:
    """The link the relay opens to one guest worker, started."""
    link = RemoteLink(worker_id, secret, launch.dial(port), **launch.link_kwargs())
    link.start()
    return link


async def _start_worker(launch: remote_worker.RemoteWorkerLaunch, worker_id: str):
    """Install and start one guest worker running ENGINE; return its secret and port."""
    await launch.install()
    secret = remote_worker.wire.new_secret()
    port = await launch.start_worker(worker_id, [sys.executable, "-u", "-c", ENGINE], secret)
    return secret, port


async def _start_link(launch: remote_worker.RemoteWorkerLaunch, worker_id: str) -> RemoteLink:
    """Start one guest worker and link to it as the relay does."""
    return _link(launch, worker_id, *await _start_worker(launch, worker_id))


async def _link_line(link: RemoteLink) -> bytes:
    return await asyncio.wait_for(link.stdout.readline(), 30)


async def _answer_link_token(link: RemoteLink) -> dict:
    """Answer the engine's token request over the link; return its env probe."""
    request = json.loads(await _link_line(link))
    assert request["method"] == "_kiro/auth/getAccessToken"
    reply = {"jsonrpc": "2.0", "id": request["id"], "result": {"accessToken": "t" * 40}}
    await link.write(json.dumps(reply).encode() + b"\n")
    return json.loads(await _link_line(link))


@pytest.mark.parametrize("service_launch", [False, True])
@sync
async def test_end_to_end_token_callback_and_echo(tmp_path, service_launch):
    launch = _launch(tmp_path, service_launch=service_launch)
    link = await _start_link(launch, "sess-e2e")
    calls = (tmp_path / "guest" / "systemd-calls").read_text()
    assert "systemctl --user show-environment" in calls
    assert ("systemd-run --user" in calls) is service_launch
    try:
        # The token request arrives exactly as a local engine's would.
        probe = await _answer_link_token(link)
        assert probe["params"] == {"api_key": "absent", "token_len": 40}
        for size in (10, 1017, 4089, 200_000):
            line = json.dumps({"pad": "x" * size}).encode() + b"\n"
            await link.write(line)
            assert await _link_line(link) == line
        # The guest unlinked its copy of the one-use secret.
        assert not (tmp_path / "guest/.kirocrew-worker/sessions/sess-e2e.key").exists()
        link.close_input()
        await async_wait_until(lambda: link.state == link_mod.FINISHED, describe=lambda: link.state)
    finally:
        await link.aclose()
        await launch.stop_worker("sess-e2e")


@sync
async def test_link_reports_lost_when_worker_is_gone(tmp_path):
    launch = _launch(tmp_path, "sess-lost")
    launch.link_options = {"keepalive": 0.2, "stall": 0.5, "lease": 2}
    secret, port = await _start_worker(launch, "sess-lost")
    # The host is reclaimed: kill the worker's session so nothing answers the dials.
    port_file = tmp_path / "guest/.kirocrew-worker/sessions/sess-lost.port"
    pid = int(port_file.read_text().split()[1])
    try:
        os.killpg(pid, signal.SIGKILL)  # setsid made it a group leader (Linux)
    except (ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)  # no setsid (macOS): its engine sees EOF and exits
    # Hold the freed port, bound but not listening, so every dial is refused at
    # connect. Left free, the kernel can hand it to another test's worker, whose
    # hello names a different session: the link then ends REFUSED, not LOST.
    hold = await async_wait_until(lambda: _bind_loopback(port), ignoring=(OSError,))
    link = _link(launch, "sess-lost", secret, port)
    try:
        ended = (link_mod.LOST, link_mod.RETIRING)
        await async_wait_until(lambda: link.state in ended, describe=lambda: link.state)
    finally:
        await link.aclose()
        hold.close()


def _bind_loopback(port: int) -> socket.socket:
    """A socket bound to ``127.0.0.1:port`` that never listens.

    No ``SO_REUSEADDR``, so no other socket can bind the port while this one holds it.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        raise
    return sock


@pytest.mark.skipif(shutil.which("python3.9") is None, reason="no python3.9 on this host")
@sync
async def test_worker_runs_on_the_oldest_guest_python(tmp_path):
    launch = _launch(tmp_path, "sess-py39", path_prefix=_old_guest_python_dir(tmp_path))
    link = await _start_link(launch, "sess-py39")
    try:
        assert (await _answer_link_token(link))["params"]["api_key"] == "absent"
        line = json.dumps({"pad": "y" * 5000}).encode() + b"\n"
        await link.write(line)
        assert await _link_line(link) == line
    finally:
        await link.aclose()
        await launch.stop_worker("sess-py39")


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_secret_never_reaches_argv_or_commands(tmp_path):
    secret = b"\x11" * 32
    command = remote_worker._start_command("sess-1", ["kiro-cli", "acp"], 60)
    assert secret.hex() not in command
    assert "env -u KIRO_API_KEY kiro-cli acp" in command


def test_hostile_values_are_rejected_or_quoted():
    with pytest.raises(ValueError):
        remote_worker._start_command("x; rm -rf ~", ["kiro-cli"], 60)
    command = remote_worker._start_command("ok", ["kiro-cli", "$(touch /tmp/p)"], 60)
    assert "'$(touch /tmp/p)'" in command
    with pytest.raises(Exception):
        remote_worker.CoderShell("coder", "bad name; id", "https://coder.example")


def test_coder_dial_never_autostarts():
    shell = remote_worker.CoderShell("coder", "my-ws", "https://coder.example")
    expected = ["coder", "--url", "https://coder.example", "ssh", "--disable-autostart", "my-ws"]
    assert shell.argv("true") == [*expected, "--", "true"]


_WS = {"server": {"name": "workspace", "command": "bridge", "args": [], "env": []}, "workdir": "/w"}


def _runtime(backend=ACP_BACKEND_KAS, workspace=_WS):
    return SimpleNamespace(acp_backend=backend, _process=None, _remote_workspace=workspace)


def test_remote_workspace_unmounts_every_builtin_tool():
    rt = _runtime()
    servers = [{"name": "kirocrew-core"}, {"name": "workspace"}]
    agents = [
        {"id": "a", "prompt": "p", "tools": ["read", "write", "shell", "@kirocrew-core", "@x/y"]},
        {"id": "b", "prompt": "", "tools": "*"},
        {"id": "c", "tools": ["*", "read"]},
    ]
    out = AcpRuntime._remote_workspace_agents(rt, agents, servers)
    # The core grant is narrowed to non-local-only tools (test_remote_session_local_only_tools.py).
    core = [t for t in out[1]["tools"] if t.startswith("@kirocrew-core/")]
    assert core and "@kirocrew-core/file_send" not in core
    # read + write + shell: their workspace tools, not glob/grep, no whole grant.
    assert out[0]["tools"] == [
        *core,
        "@x/y",
        "@workspace/read_file",
        "@workspace/list_directory",
        "@workspace/write_file",
        "@workspace/edit_file",
        "@workspace/run_command",
    ]
    # "*" granted the tool_search loader too, and it stays: deferral was enabled
    # from that grant, so a session without it could reach no deferred tool.
    assert out[1]["tools"] == [*core, "@workspace", "tool_search"]
    assert out[2]["tools"] == [*core, "@workspace", "tool_search"]
    assert all("/w" in a["prompt"] for a in out)
    assert agents[0]["tools"][0] == "read"  # the caller's projection is not mutated


@pytest.mark.parametrize(
    "tools,expected",
    [
        ([], []),
        (None, []),
        (["@x/y"], ["@x/y"]),
        (["fs_read"], ["@workspace/read_file", "@workspace/list_directory"]),
        (
            ["read", "glob", "grep", "web_fetch"],
            [
                "@workspace/read_file",
                "@workspace/list_directory",
                "@workspace/glob",
                "@workspace/grep",
            ],
        ),
        (["@builtin/fs_write"], ["@workspace/write_file", "@workspace/edit_file"]),
        (["shell"], ["@workspace/run_command"]),
        (["@builtin"], ["@workspace", "tool_search"]),
        (
            ["fs_read", "tool_search"],
            ["@workspace/read_file", "@workspace/list_directory", "tool_search"],
        ),
        (["fs_read", "glob", "grep", "fs_write", "execute_bash"], ["@workspace"]),
        # KAS's own ids keep their scope: one workspace tool each, none wider.
        (["read_file", "run_command"], ["@workspace/read_file", "@workspace/run_command"]),
        (
            ["list_directory", "file_search", "grep_search", "str_replace", "execute_pwsh"],
            [
                "@workspace/list_directory",
                "@workspace/glob",
                "@workspace/grep",
                "@workspace/edit_file",
                "@workspace/run_command",
            ],
        ),
        (["fs_append", "delete_file"], []),
    ],
)
def test_remote_workspace_grants_follow_the_agents_own_allowlist(tools, expected):
    # An agent kept from writing or running commands here is kept from it there.
    [agent] = AcpRuntime._remote_workspace_agents(
        _runtime(), [{"id": "a", "tools": tools}], [{"name": "workspace"}]
    )
    assert agent["tools"] == expected


def test_every_mapped_workspace_tool_is_one_the_guest_server_serves():
    served = {spec["name"] for spec in workspace_tools.tool_specs("/w")}
    mapped = runtime_mod._WORKSPACE_TOOLS_BY_BUILTIN
    assert {t for row in mapped.values() for t in row} == served
    assert set(mapped) <= set(KAS_TOOL_IDS_BY_KIRO_TOOL)
    by_kas = runtime_mod._WORKSPACE_TOOLS_BY_KAS_TOOL
    assert {t for row in by_kas.values() for t in row} <= served


def test_local_session_is_untouched():
    rt = _runtime(workspace=None)
    agents = [{"id": "a", "tools": ["read"]}]
    assert AcpRuntime._remote_workspace_agents(rt, agents, []) is agents
    assert AcpRuntime._remote_session_servers(rt, [{"name": "s"}]) == [{"name": "s"}]


@pytest.mark.parametrize(("tier", "lent"), [("strict", False), ("standard", True)])
def test_remote_workspace_git_notice_is_true_on_every_tier(monkeypatch, tier, lent):
    """The proxy reads the tier per request, so the notice is tier-neutral: it never
    promises private repositories, and it names what ``strict`` refuses."""
    from kiro_crew import sandbox
    from kiro_crew.config.loader import config_dir
    from kiro_crew.remote_session import git_gateway

    (config_dir() / "config.json").write_text(
        json.dumps({"agent": {"sandbox": tier}}), encoding="utf-8"
    )
    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    assert git_gateway.gateway_logins_lent() is lent
    out = AcpRuntime._remote_workspace_agents(_runtime(), [{"id": "a", "tools": "*"}], [])
    prompt = " ".join(out[0]["prompt"].split())
    assert "private repositories included" not in prompt
    assert (
        "For a host the gateway has a git login for and is allowed to lend, git goes "
        "through the gateway, which adds that login; any other host uses the "
        "workspace's own git login, if it has one."
    ) in prompt
    assert (
        "Under the strict agent sandbox the gateway lends none, so only the workspace's "
        "own login applies."
    ) in prompt
    assert (
        "A push through the gateway that updates or deletes a protected branch such as "
        "main is refused whole, as it is for a local session; push a feature branch instead."
    ) in prompt
    assert "do not unset git's url rewrites" in prompt
    assert "GIT_ASKPASS" not in prompt  # the workspace's own askpass is left alone


def test_workspace_server_is_added_once():
    rt = _runtime()
    servers = AcpRuntime._remote_session_servers(rt, [{"name": "workspace"}, {"name": "core"}])
    assert [s["name"] for s in servers] == ["core", "workspace"]
    assert servers[-1]["command"] == "bridge"


def test_remote_workspace_needs_the_kas_backend():
    with pytest.raises(AcpRuntimeError, match="KAS"):
        AcpRuntime.check_remote_workspace_supported(_runtime(backend="kiro"))
    AcpRuntime.check_remote_workspace_supported(_runtime())


@sync
async def test_end_to_end_mcp_tools_over_the_tunnel(tmp_path):
    """The real bridge to the real guest tool server: list, write, read, run, refuse."""
    launch = _launch(tmp_path, "sess-mcp")
    root = str(tmp_path / "guest" / "project")
    server = await launch.workspace_server(root)
    assert server["name"] == "workspace" and server["env"] == []
    server = _with_token(launch, server, tmp_path / "scratch")
    # The sandboxed bridge only pipes to a gateway relay that holds the coder dial.
    assert "--pipe" in server["args"] and "ssh" not in " ".join(server["args"])
    assert launch.relay is not None and launch.relay.port > 0
    assert launch.session_id.startswith("sess-mcp-") and launch.session_id != launch.binding_id
    bridge = await _spawn_bridge([server["command"], *server["args"]])
    ident = 1  # _mcp_initialize sends id 1

    async def call(method, params=None):
        nonlocal ident
        ident += 1
        msg = {"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}
        bridge.stdin.write(json.dumps(msg).encode() + b"\n")
        return await _readjson(bridge)

    async def tool(name, **arguments):
        reply = await call("tools/call", {"name": name, "arguments": arguments})
        result = reply["result"]
        return result["content"][0]["text"], result["isError"]

    try:
        assert await _mcp_initialize(bridge) == "kirocrew-workspace"
        names = {t["name"] for t in (await call("tools/list"))["result"]["tools"]}
        assert {"read_file", "write_file", "edit_file", "run_command", "grep"} <= names
        assert (await tool("write_file", path="src/a.txt", content="hello\nworld\n"))[1] is False
        assert (await tool("read_file", path="src/a.txt"))[0] == "hello\nworld\n"
        assert (await tool("edit_file", path="src/a.txt", old_str="world", new_str="crew"))[
            1
        ] is False
        assert "src/a.txt:2: crew" in (await tool("grep", pattern="cr.w", path="."))[0]
        text, err = await tool("run_command", command="cat src/a.txt; echo key=$KIRO_API_KEY")
        assert not err and "hello\ncrew" in text and "key=\n" in text and "[exit 0]" in text
        assert (await tool("read_file", path="../../etc/passwd"))[1] is True
        assert (await tool("write_file", path="/etc/x", content="no"))[1] is True
        os.symlink("/etc", os.path.join(root, "out"))
        assert (await tool("read_file", path="out/hosts"))[1] is True
    finally:
        await _end(bridge)
        await launch.relay.close()
    assert not launch.relay.token_path.exists()


async def _mcp_initialize(bridge) -> str:
    """Initialize over ``bridge``; return the server's name."""
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    msg["params"] = {"protocolVersion": "2024-11-05"}
    bridge.stdin.write(json.dumps(msg).encode() + b"\n")
    return (await asyncio.wait_for(_readjson(bridge), 30))["result"]["serverInfo"]["name"]


async def _end(bridge) -> None:
    bridge.stdin.close()
    try:
        await await_until(asyncio.ensure_future(bridge.wait()), what="bridge exit")
    except AssertionError:
        bridge.kill()
        await bridge.wait()


@sync
async def test_the_server_command_works_every_time_it_is_launched(tmp_path):
    # The engine launches its MCP server more than once per runtime (probe,
    # reload): each launch, concurrent or sequential, gets its own guest worker.
    launch = _launch(tmp_path, "sess-relaunch")
    server = await launch.workspace_server(str(tmp_path / "guest" / "p"))
    server = _with_token(launch, server, tmp_path / "scratch")
    argv = [server["command"], *server["args"]]
    sessions = tmp_path / "guest" / ".kirocrew-worker" / "sessions"
    try:
        spare_id = (await launch.relay._spare)[0]
        first, second = await _spawn_bridge(argv), await _spawn_bridge(argv)
        for bridge in (first, second):
            assert await _mcp_initialize(bridge) == "kirocrew-workspace"
        # Two connected workers plus a spare; the first launch used the spare.
        await launch.relay._spare
        assert len(list(sessions.glob("*.port"))) == 3
        assert "gateway attached" in (sessions / f"{spare_id}.log").read_text()
        await _end(first)
        await _end(second)
        third = await _spawn_bridge(argv)
        assert await _mcp_initialize(third) == "kirocrew-workspace"
        await _end(third)
    finally:
        await launch.relay.close()
    # A finished connection stops its own worker, and the token goes with the relay.
    await async_wait_until(
        lambda: not list(sessions.glob("*.port")),
        describe=lambda: list(sessions.glob("*.port")),
        off_loop=True,
    )
    assert not list(sessions.glob("*.port"))
    assert not Path(server["args"][-1]).exists()


@sync
async def test_a_replacement_waits_for_an_unconfirmed_worker_stop(tmp_path):
    # A worker whose stop did not confirm may still run a command, so the
    # replacement must not share the workspace with it until the stop confirms.
    launch = _launch(tmp_path, "sess-unconfirmed")
    root = str(tmp_path / "guest" / "p")
    await launch.workspace_server(root)
    spare_id = (await launch.relay._spare)[0]
    sessions = tmp_path / "guest" / ".kirocrew-worker" / "sessions"
    real_run = launch.shell.run
    refused: list[str] = []

    async def stops_fail(command, stdin=b""):
        if command == remote_worker._stop_command(spare_id):
            refused.append(spare_id)
            raise remote_worker.RemoteWorkerError("stop did not confirm")
        return await real_run(command, stdin)

    launch.shell.run = stops_fail
    try:
        await launch.close_relay()
        assert refused and (sessions / f"{spare_id}.port").exists()
        with pytest.raises(remote_worker.RemoteWorkerError, match="previous engine"):
            await launch.workspace_server(root)
        assert launch.relay is None  # nothing was attached
    finally:
        launch.shell.run = real_run
    # Once the stop confirms, the replacement starts and the old worker is gone.
    await launch.workspace_server(root)
    try:
        assert not (sessions / f"{spare_id}.port").exists()
        assert spare_id not in remote_worker._UNCONFIRMED_STOPS.get(launch.binding_id, set())
    finally:
        await launch.relay.close()


@sync
async def test_a_second_runtime_on_the_binding_leaves_the_live_relays_workers(tmp_path):
    # A subagent shares its parent's binding: starting it must not stop workers the
    # parent's live relay still owns.
    root = str(tmp_path / "guest" / "p")
    parent = _launch(tmp_path, "sess-shared")
    await parent.workspace_server(root)
    spare_id = (await parent.relay._spare)[0]
    sessions = tmp_path / "guest" / ".kirocrew-worker" / "sessions"
    child = remote_worker.RemoteWorkerLaunch("sess-shared", parent.shell, orphan_timeout=60)
    await child.workspace_server(root)
    try:
        assert (sessions / f"{spare_id}.port").exists()
        assert parent.relay._spare is not None and not parent.relay._spare.cancelled()
    finally:
        await child.relay.close()
        await parent.relay.close()


@needs_proc
@sync
async def test_the_stale_worker_reaper_waits_for_each_exit(tmp_path):
    # A worker left by an earlier gateway process may still be running a command, so
    # the reaper returns only once it has exited.
    shell = LocalShell(tmp_path / "guest")
    sessions = tmp_path / "guest" / ".kirocrew-worker" / "sessions"
    sessions.mkdir(parents=True)
    record = sessions / "sess-stale-gzzzzzz-0001.port"
    ready = tmp_path / "ready"
    slow_exit = (
        "import pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGUSR1, lambda *_: (time.sleep(1.5), sys.exit(0)))\n"
        f"pathlib.Path({str(ready)!r}).write_text('ok')\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", slow_exit, "--port-file", str(record)])
    try:
        record.write_text(f"1 {proc.pid}")
        await async_wait_until(
            ready.exists, describe=lambda: "worker never installed its handler", off_loop=True
        )
        launch = remote_worker.RemoteWorkerLaunch("sess-stale", shell)
        await launch.reap_stale_workers()
        assert proc.poll() is not None  # exited before the reaper returned
        assert not record.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@needs_proc
@sync
async def test_a_stop_that_times_out_keeps_the_workers_record(tmp_path, monkeypatch):
    # A worker that does not exit in time may still be running a command: its record
    # stays, so the next confirmation finds it again instead of reading it as gone.
    short_wait = remote_worker._WAIT_GONE.replace(
        f"-ge {remote_worker._STOP_WAIT_SECONDS} ]", "-ge 1 ]"
    )
    assert short_wait != remote_worker._WAIT_GONE
    monkeypatch.setattr(remote_worker, "_WAIT_GONE", short_wait)
    shell = LocalShell(tmp_path / "guest")
    sessions = tmp_path / "guest" / ".kirocrew-worker" / "sessions"
    sessions.mkdir(parents=True)
    record = sessions / "sess-paused-g000000-0001.port"
    ready = tmp_path / "ready"
    ignores_stop = (
        "import pathlib, signal, time\n"
        "signal.signal(signal.SIGUSR1, signal.SIG_IGN)\n"
        f"pathlib.Path({str(ready)!r}).write_text('ok')\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", ignores_stop, "--port-file", str(record)])
    try:
        record.write_text(f"1 {proc.pid}")
        await async_wait_until(
            ready.exists, describe=lambda: "worker never installed its handler", off_loop=True
        )
        launch = remote_worker.RemoteWorkerLaunch("sess-paused", shell)
        with pytest.raises(remote_worker.RemoteWorkerError):
            await launch.stop_worker("sess-paused-g000000-0001")
        assert record.exists() and proc.poll() is None
        assert "sess-paused-g000000-0001" in remote_worker._UNCONFIRMED_STOPS["sess-paused"]
        with pytest.raises(remote_worker.RemoteWorkerError, match="previous engine"):
            await launch.confirm_earlier_workers_stopped()
    finally:
        remote_worker._UNCONFIRMED_STOPS.pop("sess-paused", None)
        proc.kill()
        proc.wait()


@sync
async def test_a_failed_reap_refuses_the_start_and_is_retried(tmp_path):
    launch = _launch(tmp_path, "sess-reap-fails")
    real_run = launch.shell.run
    reap = remote_worker._reap_stale_command(
        launch.binding_id[: remote_worker._WORKER_ID_PREFIX_CHARS], remote_worker._GATEWAY_EPOCH
    )

    async def reap_fails(command, stdin=b""):
        if command == reap:
            raise remote_worker.RemoteWorkerError("a stale worker did not exit")
        return await real_run(command, stdin)

    launch.shell.run = reap_fails
    with pytest.raises(remote_worker.RemoteWorkerError, match="previous engine"):
        await launch.workspace_server(str(tmp_path / "guest" / "p"))
    assert launch.binding_id not in remote_worker._REAPED and launch.relay is None
    launch.shell.run = real_run
    await launch.workspace_server(str(tmp_path / "guest" / "p"))
    try:
        assert launch.binding_id in remote_worker._REAPED
    finally:
        await launch.relay.close()


@sync
async def test_the_relay_refuses_a_connection_without_the_token(tmp_path):
    launch = _launch(tmp_path, "sess-token")
    await launch.workspace_server(str(tmp_path / "guest" / "p"))
    sessions = tmp_path / "guest" / ".kirocrew-worker" / "sessions"
    try:
        await launch.relay._spare
        # The relay listens on loopback only.
        assert launch.relay._server.sockets[0].getsockname()[0] == "127.0.0.1"
        before = sorted(sessions.glob("*.port"))
        reader, writer = await asyncio.open_connection("127.0.0.1", launch.relay.port)
        writer.write(b"not-the-token\n")
        assert (
            await await_until(asyncio.ensure_future(reader.read()), what="relay stream closed")
            == b""
        )
        writer.close()
        # Nothing started for it, and the pre-started spare is unspent.
        assert sorted(sessions.glob("*.port")) == before and len(before) == 1
    finally:
        await launch.relay.close()


def test_local_shell_services_and_bus_are_tmp_owned(tmp_path):
    shell = LocalShell(tmp_path / "guest")
    env = shell._env()
    # Refuse before executing anything if a fixture change exposes a host binary.
    for name in ("systemctl", "systemd-run"):
        assert shutil.which(name, path=env["PATH"]) == str(shell.bin / name)
        assert str(shell.bin).startswith(str(tmp_path))
    assert env["XDG_RUNTIME_DIR"] == str(shell.runtime_dir)
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={shell.runtime_dir / 'bus'}"
    assert f"XDG_RUNTIME_DIR={shell.runtime_dir}" in shell.argv("true")


@sync
async def test_git_proxy_runs_in_the_guest_and_answers_through_the_gateway(tmp_path):
    """Install, worker, real RemoteLink, channel: the whole path, upstream faked."""
    import http.client

    from test_remote_git_proxy import HttpBackendUpstream

    launch = _launch(tmp_path, "sess-git")
    await launch.install()
    upstream = HttpBackendUpstream(tmp_path, status=404)

    async def credential(host):
        return "Basic eC1hY2Nlc3MtdG9rZW46dA=="

    try:
        await launch.start_git_proxy(
            "dashboard:chat-git",
            credential_source=credential,
            agent="researcher",
            egress_permitted=lambda key, host, agent: True,
            http_session=upstream,
        )
        port_file = tmp_path / "guest" / ".kirocrew-worker" / launch.git_proxy_port_file
        await async_wait_until(
            port_file.exists, off_loop=True, describe=lambda: "guest proxy port is unpublished"
        )
        port = int(port_file.read_text().strip())

        def fetch() -> int:
            # A loopback connection to the guest proxy: only the port varies.
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            try:
                connection.request(
                    "GET",
                    "/github.com/acme/w/info/refs?service=git-upload-pack",
                    headers={"Authorization": f"Bearer {launch.git_proxy_secret}"},
                )
                return connection.getresponse().status
            finally:
                connection.close()

        assert await asyncio.wait_for(asyncio.to_thread(fetch), 60) == 404
        assert upstream.calls[0]["url"] == (
            "https://github.com/acme/w/info/refs?service=git-upload-pack"
        )
    finally:
        await launch.stop_git_proxy()
    assert launch.git_proxy is None
    # The proxy is gone with its link: its port file goes with it.
    await async_wait_until(
        lambda: not port_file.exists(),
        off_loop=True,
        describe=lambda: "guest proxy port file remains after shutdown",
    )
    assert not port_file.exists()


@sync
async def test_an_older_relay_closing_does_not_stop_the_live_git_proxy(tmp_path):
    """A start can make more than one relay; only the current one owns the proxy.

    Seen live: the first relay of a start idled out 16 minutes in and stopped the
    proxy while the session was still working through the second relay.
    """
    launch = _launch(tmp_path, "sess-relays")
    await launch.install()
    stops: list[str] = []

    async def stop() -> None:
        stops.append("stop")

    launch.stop_git_proxy = stop  # observe, do not reach the guest
    first = await launch.workspace_server(str(tmp_path / "guest"))
    first_relay = launch.relay
    await launch.workspace_server(str(tmp_path / "guest"))
    second_relay = launch.relay
    try:
        assert first and first_relay is not second_relay
        await first_relay.close()
        assert stops == []  # the old relay is gone; the proxy stays
        await second_relay.close()
        assert stops == ["stop"]
    finally:
        await first_relay.close()
        await second_relay.close()


@sync
async def test_a_runtime_ending_normally_stops_its_git_proxy(tmp_path):
    """The normal end of a runtime goes through ``close_relay``, which forgets
    ``launch.relay`` before the close runs: the proxy must still stop."""
    launch = _launch(tmp_path, "sess-runtime-end")
    await launch.install()
    stops: list[str] = []

    async def stop() -> None:
        stops.append("stop")

    launch.stop_git_proxy = stop  # observe, do not reach the guest
    await launch.workspace_server(str(tmp_path / "guest"))
    relay = launch.relay
    try:
        alive = [True]
        launch.close_relay_when_ended(lambda: alive[0])
        alive[0] = False
        await async_wait_until(
            lambda: launch.relay is None and stops,
            describe=lambda: f"relay={launch.relay!r} stops={stops!r}",
        )
        assert stops == ["stop"]
    finally:
        await relay.close()
