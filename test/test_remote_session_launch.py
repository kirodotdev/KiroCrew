"""End-to-end tests for a remote session launch: bootstrap, bridge, runtime seam.

The "guest" is a temporary HOME on this machine reached through ``sh -c``, so
the exact install/start/stop shell scripts that ``coder ssh`` would run in a
workspace run here, and the bridge process is the real
``python -m kiro_crew.remote_session.client`` the runtime spawns. The fake
engine asks for an access token the way kiro-cli does with Crew as the auth
owner, then echoes. Every process is stopped in a ``finally``.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import runpy
import shutil
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.coder import remote_worker
from kiro_crew.remote_session import client as client_mod
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX guest shell")

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
        self.path = os.pathsep.join(
            filter(
                None,
                (
                    str(self.bin),
                    path_prefix,
                    str(Path(sys.executable).parent),
                    os.environ["PATH"],
                ),
            )
        )
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

    def _env(self) -> dict:
        return {
            **os.environ,
            "HOME": str(self.home),
            "PATH": self.path,
            "XDG_RUNTIME_DIR": str(self.runtime_dir),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.runtime_dir / 'bus'}",
            "KIRO_API_KEY": "must-not-leak",
        }

    async def run(self, command: str, stdin: bytes = b"") -> bytes:
        return await remote_worker.run_collect(["sh", "-c", command], stdin, self._env())

    def argv(self, command: str) -> list[str]:
        return [
            "env",
            f"HOME={self.home}",
            f"PATH={self.path}",
            f"XDG_RUNTIME_DIR={self.runtime_dir}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path={self.runtime_dir / 'bus'}",
            "KIRO_API_KEY=must-not-leak",
            "sh",
            "-c",
            command,
        ]


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
    launch = remote_worker.RemoteWorkerLaunch("sess-inst", shell, tmp_path / "secrets")
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
    tmp_path: Path, sid: str = "sess-e2e", *, service_launch: bool = False
) -> remote_worker.RemoteWorkerLaunch:
    return remote_worker.RemoteWorkerLaunch(
        sid,
        LocalShell(tmp_path / "guest", service_launch=service_launch),
        tmp_path / "secrets",
        orphan_timeout=60,
        link_options={"keepalive": 0.5, "stall": 5, "lease": 20},
    )


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


@pytest.mark.parametrize("service_launch", [False, True])
@sync
async def test_end_to_end_token_callback_and_echo(tmp_path, service_launch):
    launch = _launch(tmp_path, service_launch=service_launch)
    argv = await launch.local_argv([sys.executable, "-u", "-c", ENGINE])
    secret_file = Path(argv[argv.index("--secret-file") + 1])
    assert stat_mode(secret_file) == 0o600
    calls = (tmp_path / "guest" / "systemd-calls").read_text()
    assert "systemctl --user show-environment" in calls
    assert ("systemd-run --user" in calls) is service_launch
    bridge = await _spawn_bridge(argv)
    try:
        # The engine's token request arrives exactly as a local engine's would.
        request = await _readjson(bridge)
        assert request["method"] == "_kiro/auth/getAccessToken"
        reply = {"jsonrpc": "2.0", "id": request["id"], "result": {"accessToken": "t" * 40}}
        bridge.stdin.write(json.dumps(reply).encode() + b"\n")
        probe = await _readjson(bridge)
        assert probe["params"] == {"api_key": "absent", "token_len": 40}
        for size in (10, 1017, 4089, 200_000):
            line = json.dumps({"pad": "x" * size}).encode() + b"\n"
            bridge.stdin.write(line)
            assert await asyncio.wait_for(bridge.stdout.readline(), 30) == line
        # The bridge consumed its secret file; the guest unlinked its copy.
        assert not secret_file.exists()
        assert not (tmp_path / "guest/.kirocrew-worker/sessions/sess-e2e.key").exists()
        bridge.stdin.close()
        assert await asyncio.wait_for(bridge.wait(), 30) == client_mod.EXIT_FINISHED
    finally:
        if bridge.returncode is None:
            bridge.kill()
            await bridge.wait()
        await launch.stop()


@sync
async def test_bridge_reports_lost_when_worker_is_gone(tmp_path):
    launch = _launch(tmp_path, "sess-lost")
    launch.link_options = {"keepalive": 0.2, "stall": 0.5, "lease": 2}
    argv = await launch.local_argv([sys.executable, "-u", "-c", ENGINE])
    # The guest dies without warning (host reclaimed): kill the worker's whole
    # session, engine included, so nothing can answer the bridge's dials.
    port_file = tmp_path / "guest/.kirocrew-worker/sessions/sess-lost.port"
    pid = int(port_file.read_text().split()[1])
    try:
        os.killpg(pid, signal.SIGKILL)  # setsid made it a group leader (Linux)
    except (ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)  # no setsid (macOS): its engine sees EOF and exits
    bridge = await _spawn_bridge(argv)
    try:
        code = await asyncio.wait_for(bridge.wait(), 30)
        assert code in (client_mod.EXIT_LOST, client_mod.EXIT_RETIRING)
    finally:
        if bridge.returncode is None:
            bridge.kill()
            await bridge.wait()


@pytest.mark.skipif(shutil.which("python3.9") is None, reason="no python3.9 on this host")
@sync
async def test_worker_runs_on_the_oldest_guest_python(tmp_path):
    """The guest worker is stdlib-only and copied as-is, so it must run on the
    Python the guest image ships (3.9), not only on the gateway's."""
    launch = remote_worker.RemoteWorkerLaunch(
        "sess-py39",
        LocalShell(tmp_path / "guest", path_prefix=_old_guest_python_dir(tmp_path)),
        tmp_path / "secrets",
        orphan_timeout=60,
        link_options={"keepalive": 0.5, "stall": 5, "lease": 20},
    )
    argv = await launch.local_argv([sys.executable, "-u", "-c", ENGINE])
    bridge = await _spawn_bridge(argv)
    try:
        request = await _readjson(bridge)
        assert request["method"] == "_kiro/auth/getAccessToken"
        reply = {"jsonrpc": "2.0", "id": request["id"], "result": {"accessToken": "t" * 40}}
        bridge.stdin.write(json.dumps(reply).encode() + b"\n")
        assert (await _readjson(bridge))["params"]["api_key"] == "absent"
        line = json.dumps({"pad": "y" * 5000}).encode() + b"\n"
        bridge.stdin.write(line)
        assert await asyncio.wait_for(bridge.stdout.readline(), 30) == line
    finally:
        if bridge.returncode is None:
            bridge.kill()
            await bridge.wait()
        await launch.stop()


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


def test_private_secret_dir_is_enforced(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    with pytest.raises(remote_worker.RemoteWorkerError):
        remote_worker._write_private(shared, "k", b"x")


# -- runtime seam ---------------------------------------------------------------------


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
    # The whole-server core grant is narrowed to its non-local-only tools (see
    # test_remote_session_local_only_tools.py); everything else is as granted.
    core = [t for t in out[1]["tools"] if t.startswith("@kirocrew-core/")]
    assert core and "@kirocrew-core/file_send" not in core
    assert out[0]["tools"] == [*core, "@x/y", "@workspace"]
    assert out[1]["tools"] == [*core, "@workspace"]
    assert out[2]["tools"] == [*core, "@workspace"]
    assert all("/w" in a["prompt"] for a in out)
    assert agents[0]["tools"][0] == "read"  # the caller's projection is not mutated


def test_local_session_is_untouched():
    rt = _runtime(workspace=None)
    agents = [{"id": "a", "tools": ["read"]}]
    assert AcpRuntime._remote_workspace_agents(rt, agents, []) is agents
    assert AcpRuntime._remote_session_servers(rt, [{"name": "s"}]) == [{"name": "s"}]


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
    """The real bridge, as the local engine would launch it, reaching the real
    guest tool server through the worker: list tools, write, read, run, refuse."""
    launch = _launch(tmp_path, "sess-mcp")
    root = str(tmp_path / "guest" / "project")
    server = await launch.workspace_server(root)
    assert server["name"] == "workspace" and server["env"] == []
    # The dial runs in this (gateway) process behind a loopback relay: the bridge
    # is a sandboxed MCP child that cannot use the gateway's coder login.
    # The bridge only pipes to a gateway relay, which holds the link and the dial:
    # the bridge is a sandboxed MCP child that cannot use the gateway's coder login.
    assert "--pipe" in server["args"] and "ssh" not in " ".join(server["args"])
    assert launch.relay is not None and launch.relay.port > 0
    assert launch.session_id.startswith("sess-mcp-") and launch.session_id != launch.binding_id
    bridge = await _spawn_bridge([server["command"], *server["args"]])
    ident = 0

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
        assert (await call("initialize", {"protocolVersion": "2024-11-05"}))["result"][
            "serverInfo"
        ]["name"] == "kirocrew-workspace"
        names = {t["name"] for t in (await call("tools/list"))["result"]["tools"]}
        assert {"read_file", "write_file", "edit_file", "run_command", "grep"} <= names
        assert (await tool("write_file", path="src/a.txt", content="hello\nworld\n"))[1] is False
        assert (await tool("read_file", path="src/a.txt"))[0] == "hello\nworld\n"
        assert (await tool("edit_file", path="src/a.txt", old_str="world", new_str="crew"))[
            1
        ] is False
        assert "src/a.txt:2: crew" in (await tool("grep", pattern="cr.w"))[0]
        text, err = await tool("run_command", command="cat src/a.txt; echo key=$KIRO_API_KEY")
        assert not err and "hello\ncrew" in text and "key=\n" in text and "[exit 0]" in text
        assert (await tool("read_file", path="../../etc/passwd"))[1] is True
        assert (await tool("write_file", path="/etc/x", content="no"))[1] is True
        os.symlink("/etc", os.path.join(root, "out"))
        assert (await tool("read_file", path="out/hosts"))[1] is True
    finally:
        bridge.stdin.close()
        try:
            await await_until(asyncio.ensure_future(bridge.wait()), what="bridge exit")
        except AssertionError:
            bridge.kill()
            await bridge.wait()
        await launch.relay.close()
    assert not launch.relay.token_path.exists()


async def _mcp_initialize(bridge) -> dict:
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05"},
    }
    bridge.stdin.write(json.dumps(msg).encode() + b"\n")
    return await asyncio.wait_for(_readjson(bridge), 30)


async def _end(bridge) -> None:
    bridge.stdin.close()
    try:
        await await_until(asyncio.ensure_future(bridge.wait()), what="bridge exit")
    except AssertionError:
        bridge.kill()
        await bridge.wait()


@sync
async def test_the_server_command_works_every_time_it_is_launched(tmp_path):
    # Regression: the engine launches its stdio MCP server more than once per
    # runtime (a tools probe, a reload). The old bridge consumed a one-use secret
    # on its first launch, so every later launch died at once and the agent saw
    # "workspace: Connection closed". Each launch must get a working stream, at
    # the same time and one after another, each on its own guest worker.
    launch = _launch(tmp_path, "sess-relaunch")
    server = await launch.workspace_server(str(tmp_path / "guest" / "p"))
    argv = [server["command"], *server["args"]]
    sessions = tmp_path / "guest" / ".kirocrew-worker" / "sessions"
    try:
        spare_id = (await launch.relay._spare)[0]
        first, second = await _spawn_bridge(argv), await _spawn_bridge(argv)
        for bridge in (first, second):
            assert (await _mcp_initialize(bridge))["result"]["serverInfo"][
                "name"
            ] == "kirocrew-workspace"
        # Two connected workers plus one pre-started for the next launch, and the
        # first launch used the worker started ahead of it.
        await launch.relay._spare
        assert len(list(sessions.glob("*.port"))) == 3
        assert "gateway attached" in (sessions / f"{spare_id}.log").read_text()
        await _end(first)
        await _end(second)
        third = await _spawn_bridge(argv)
        assert (await _mcp_initialize(third))["result"]["serverInfo"][
            "name"
        ] == "kirocrew-workspace"
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
async def test_the_relay_refuses_a_connection_without_the_token(tmp_path):
    launch = _launch(tmp_path, "sess-token")
    await launch.workspace_server(str(tmp_path / "guest" / "p"))
    sessions = tmp_path / "guest" / ".kirocrew-worker" / "sessions"
    try:
        await launch.relay._spare
        before = sorted(sessions.glob("*.port"))
        reader, writer = await asyncio.open_connection("127.0.0.1", launch.relay.port)
        writer.write(b"not-the-token\n")
        assert (
            await await_until(asyncio.ensure_future(reader.read()), what="relay stream closed")
            == b""
        )
        writer.close()
        # Nothing was started for it, and the pre-started spare was not spent.
        assert sorted(sessions.glob("*.port")) == before and len(before) == 1
    finally:
        await launch.relay.close()


@sync
async def test_relay_is_loopback_one_at_a_time_and_logs_dial_failure(caplog):
    import sys

    from kiro_crew.remote_session.relay import DialRelay

    relay = DialRelay(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('auth failed: not signed in'); sys.exit(3)",
        ],
        name="t",
        min_dial_gap=0.0,
    )
    port = await relay.start()
    try:
        assert relay._server.sockets[0].getsockname()[0] == "127.0.0.1"
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert (
            await await_until(asyncio.ensure_future(reader.read()), what="relay stream closed")
            == b""
        )
        writer.close()
        await async_wait_until(
            lambda: "auth failed: not signed in" in caplog.text, describe=lambda: caplog.text
        )
        assert "dial exited 3: auth failed: not signed in" in caplog.text
    finally:
        await relay.close()


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
