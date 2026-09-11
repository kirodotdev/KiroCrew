"""Host-stdio MCP bridge for containerized sessions."""

from __future__ import annotations

import ast
import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

from kiro_crew import devcontainer as devc
from kiro_crew import devcontainer_mcp as bridge


def _short_bridge_dir(unique: str, tmp_path: Path) -> Path:
    """See ``test_devcontainer._short_bridge_dir`` — same Windows /tmp footgun."""
    if sys.platform == "win32":
        path = tmp_path / "kc-mb"
    else:
        path = Path("/tmp") / "kc-mb" / unique[:12]
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def trust_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(devc, "config_dir", lambda: home)
    short = _short_bridge_dir(tmp_path.name, tmp_path)
    monkeypatch.setattr(bridge, "host_bridge_dir", lambda _p: short)
    gtmp = tmp_path / "gtmp"
    gtmp.mkdir()
    monkeypatch.setattr(devc, "_gateway_tmp_root", lambda: gtmp)
    yield home
    shutil.rmtree(short, ignore_errors=True)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".devcontainer").mkdir()
    (project / ".devcontainer" / "devcontainer.json").write_text(
        json.dumps({"image": "ubuntu:24.04"}), encoding="utf-8"
    )
    return project


class TestBridgeClientIsStdlibOnly:
    def test_client_module_imports_nothing_from_kiro_crew(self) -> None:
        src = Path(bridge._CLIENT_SOURCE).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("kiro_crew")
            elif isinstance(node, ast.ImportFrom):
                assert node.module is None or not node.module.startswith("kiro_crew")


class TestBridgeMountIsInjectedAfterTheScreen:
    def test_write_build_config_appends_the_bridge_bind(
        self, tmp_path: Path, trust_home: Path
    ) -> None:
        project = _project(tmp_path)
        cfg = project / ".devcontainer" / "devcontainer.json"
        out = devc.write_build_config(str(project), devc.config_digest(cfg))
        written = json.loads(out.read_text(encoding="utf-8"))
        mounts = written["mounts"]
        assert any(bridge.MCP_BRIDGE_CONTAINER_DIR in str(m) for m in mounts)
        host = str(bridge.host_bridge_dir(str(project)))
        assert any(host in str(m) for m in mounts)
        client = bridge.host_bridge_dir(str(project)) / bridge.MCP_BRIDGE_CLIENT_NAME
        assert client.is_file()
        assert "No ``kiro_crew`` imports" in client.read_text(encoding="utf-8")

    def test_an_ordinary_config_is_still_accepted(self, tmp_path: Path, trust_home: Path) -> None:
        project = _project(tmp_path)
        cfg = project / ".devcontainer" / "devcontainer.json"
        out = devc.write_build_config(str(project), devc.config_digest(cfg))
        written = json.loads(out.read_text(encoding="utf-8"))
        assert written["image"] == "ubuntu:24.04"


class TestSessionServerEntries:
    def test_unix_entries_point_at_the_in_container_client(self) -> None:
        entries = bridge.session_server_entries("abc123", transport="unix", secret="s3cret")
        names = {e["name"] for e in entries}
        assert names == {"kirocrew-core", "kirocrew-cron", "kirocrew-computer"}
        for entry in entries:
            assert entry["command"] == "python3"
            assert entry["args"][0] == f"{bridge.MCP_BRIDGE_CONTAINER_DIR}/client.py"
            assert entry["args"][1].startswith(f"{bridge.MCP_BRIDGE_CONTAINER_DIR}/abc123.")
            assert entry["args"][1].endswith(".sock")
            assert entry["args"][2] == "s3cret"

    def test_tcp_entries_point_at_host_docker_internal(self) -> None:
        ports = {"mcp-core": 41001, "mcp-cron": 41002, "mcp-computer": 41003}
        entries = bridge.session_server_entries(
            "abc123", transport="tcp", ports=ports, secret="s3cret"
        )
        names = {e["name"] for e in entries}
        assert names == {"kirocrew-core", "kirocrew-cron", "kirocrew-computer"}
        by_name = {e["name"]: e for e in entries}
        assert by_name["kirocrew-core"]["args"] == [
            f"{bridge.MCP_BRIDGE_CONTAINER_DIR}/client.py",
            "tcp",
            "host.docker.internal",
            "41001",
            "s3cret",
        ]

    def test_tcp_entries_without_ports_are_omitted(self) -> None:
        assert (
            bridge.session_server_entries("abc123", transport="tcp", ports={}, secret="s3cret")
            == []
        )

    def test_empty_secret_ships_no_entries(self) -> None:
        assert bridge.session_server_entries("abc123", transport="unix", secret="") == []


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX MCP bridge is Linux-only")
@pytest.mark.asyncio
async def test_accept_spawns_host_child_with_session_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connect on the unix socket starts the host MCP invocation."""
    seen_env: dict[str, str] = {}
    real_exec = bridge.asyncio.create_subprocess_exec

    async def wrapping_exec(*argv: str, **kwargs: object) -> object:
        env = kwargs.get("env")
        if isinstance(env, dict):
            seen_env.update({str(k): str(v) for k, v in env.items()})
        return await real_exec(*argv, **kwargs)

    monkeypatch.setattr(bridge, "uses_unix_bridge", lambda: True)
    monkeypatch.setattr(bridge.asyncio, "create_subprocess_exec", wrapping_exec)
    monkeypatch.setattr(
        bridge,
        "_host_mcp_argv",
        lambda _sub: [
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)",
        ],
    )
    short = _short_bridge_dir(tmp_path.name, tmp_path)
    monkeypatch.setattr(bridge, "host_bridge_dir", lambda _p: short)

    handle = await bridge.start_bridge(
        str(tmp_path / "proj"),
        "rt1",
        session_env={"KIROCREW_SESSION_KEY": "slot-9", "KIROCREW_HOME": "/tmp/home"},
    )
    try:
        # Async connect so the accept loop can run; a blocking socket on this
        # thread would stall the event loop and the child would never spawn.
        path = str(bridge.host_socket_path(str(tmp_path / "proj"), "rt1", "mcp-core"))
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(handle._secret.encode("ascii") + b"\nping")
        await writer.drain()
        writer.write_eof()
        got = await asyncio.wait_for(reader.read(16), timeout=5)
        writer.close()
        await writer.wait_closed()
        assert got == b"ping"
        assert seen_env["KIROCREW_SESSION_KEY"] == "slot-9"
    finally:
        await handle.close()
        shutil.rmtree(short, ignore_errors=True)


@pytest.mark.asyncio
async def test_tcp_accept_spawns_host_child_with_session_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connect on 127.0.0.1 starts the host MCP invocation."""
    seen_env: dict[str, str] = {}
    real_exec = bridge.asyncio.create_subprocess_exec

    async def wrapping_exec(*argv: str, **kwargs: object) -> object:
        env = kwargs.get("env")
        if isinstance(env, dict):
            seen_env.update({str(k): str(v) for k, v in env.items()})
        return await real_exec(*argv, **kwargs)

    monkeypatch.setattr(bridge, "uses_unix_bridge", lambda: False)
    monkeypatch.setattr(bridge.asyncio, "create_subprocess_exec", wrapping_exec)
    monkeypatch.setattr(
        bridge,
        "_host_mcp_argv",
        lambda _sub: [
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)",
        ],
    )
    short = _short_bridge_dir(tmp_path.name, tmp_path)
    monkeypatch.setattr(bridge, "host_bridge_dir", lambda _p: short)

    handle = await bridge.start_bridge(
        str(tmp_path / "proj"),
        "rt1",
        session_env={"KIROCREW_SESSION_KEY": "slot-9", "KIROCREW_HOME": "/tmp/home"},
    )
    try:
        entries = handle.session_servers()
        core = next(e for e in entries if e["name"] == "kirocrew-core")
        assert core["args"][1:3] == ["tcp", "host.docker.internal"]
        port = int(core["args"][3])
        assert core["args"][4] == handle._secret
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(handle._secret.encode("ascii") + b"\nping")
        await writer.drain()
        writer.write_eof()
        got = await asyncio.wait_for(reader.read(16), timeout=5)
        writer.close()
        await writer.wait_closed()
        assert got == b"ping"
        assert seen_env["KIROCREW_SESSION_KEY"] == "slot-9"
        sockets = handle._servers[0].sockets
        assert sockets
        assert sockets[0].getsockname()[0] == "127.0.0.1"
    finally:
        await handle.close()
        shutil.rmtree(short, ignore_errors=True)


@pytest.mark.asyncio
async def test_tcp_wrong_secret_does_not_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connecting to the port without this runtime's secret is not enough."""
    spawned = False
    real_exec = bridge.asyncio.create_subprocess_exec

    async def wrapping_exec(*argv: str, **kwargs: object) -> object:
        nonlocal spawned
        spawned = True
        return await real_exec(*argv, **kwargs)

    monkeypatch.setattr(bridge, "uses_unix_bridge", lambda: False)
    monkeypatch.setattr(bridge.asyncio, "create_subprocess_exec", wrapping_exec)
    monkeypatch.setattr(
        bridge,
        "_host_mcp_argv",
        lambda _sub: [
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)",
        ],
    )
    short = _short_bridge_dir(tmp_path.name, tmp_path)
    monkeypatch.setattr(bridge, "host_bridge_dir", lambda _p: short)

    handle = await bridge.start_bridge(str(tmp_path / "proj"), "rt1")
    try:
        entries = handle.session_servers()
        core = next(e for e in entries if e["name"] == "kirocrew-core")
        port = int(core["args"][3])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"wrong-secret\nping")
        await writer.drain()
        writer.write_eof()
        got = await asyncio.wait_for(reader.read(16), timeout=5)
        writer.close()
        await writer.wait_closed()
        assert got == b""
        assert spawned is False
    finally:
        await handle.close()
        shutil.rmtree(short, ignore_errors=True)


def test_host_gateway_is_injected_only_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    parsed: dict = {"runArgs": ["--cap-add=SYS_PTRACE"]}
    monkeypatch.setattr(bridge, "uses_unix_bridge", lambda: True)
    bridge.inject_host_gateway(parsed)
    assert bridge.HOST_GATEWAY_RUNARG in parsed["runArgs"]
    parsed_desktop: dict = {"runArgs": ["--cap-add=SYS_PTRACE"]}
    monkeypatch.setattr(bridge, "uses_unix_bridge", lambda: False)
    bridge.inject_host_gateway(parsed_desktop)
    assert bridge.HOST_GATEWAY_RUNARG not in parsed_desktop["runArgs"]
