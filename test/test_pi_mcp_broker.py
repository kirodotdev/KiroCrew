"""Host-side Pi MCP broker — secrets stay out of Pi's namespace (GPT F1)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import signal
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest
from test_update_provider import _UNALLOCATABLE_PID
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew.acp.pi_mcp_broker import (
    ENV_BROKER_SOCK,
    AdmittedServerRoster,
    PiMcpBroker,
    admit_server_roster,
    broker_socket_path,
)
from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.pool import READ_BUFFER_LIMIT_BYTES


def _broker(
    servers: list[dict[str, Any]],
    *,
    roster: AdmittedServerRoster | None = None,
    **kwargs,
) -> PiMcpBroker:
    """Construct through the same admitted-roster seam production uses."""
    return PiMcpBroker(
        roster=roster if roster is not None else admit_server_roster(servers), **kwargs
    )


@pytest.fixture
def broker_dir(tmp_path: Path) -> Path:
    with tempfile.TemporaryDirectory(
        prefix=SHORT_TMP_PREFIX + "pmb-", dir=short_tmp_base()
    ) as path:
        yield Path(path)


@pytest.fixture(autouse=True)
def isolate_broker_child_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the protocol with fixture children, independent of host userns."""
    from kiro_crew.acp import pi_mcp_broker

    async def fixture_argv(argv, *, mode, strip_python_env, extra_hidden_dirs, _prepare):
        del mode, strip_python_env, extra_hidden_dirs, _prepare
        return argv, None

    monkeypatch.setattr(pi_mcp_broker, "wrap_argv_async", fixture_argv)


def test_broker_socket_path_requires_nonce(broker_dir: Path) -> None:
    with pytest.raises(ValueError, match="nonce"):
        broker_socket_path(artifact_dir=str(broker_dir), pid=1, nonce="")


def test_env_name_is_stable() -> None:
    assert ENV_BROKER_SOCK == "KIROCREW_PI_MCP_BROKER_SOCK"


def test_server_roster_bounds_spec_size_before_retention(
    broker_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.acp import pi_mcp_broker

    monkeypatch.setattr(pi_mcp_broker, "_SERVER_SPEC_MAX_BYTES", 128)
    healthy = {"name": "healthy", "command": "echo", "env": {"TOKEN": "original"}}
    broker = _broker(
        [
            {"name": "large", "command": "echo", "env": {"TOKEN": "x" * 1000}},
            healthy,
        ],
        socket_path=str(broker_dir / "bounded-spec.sock"),
    )
    healthy["env"]["TOKEN"] = "changed"
    assert [spec["name"] for spec in broker._specs] == ["healthy"]
    assert broker._specs[0]["env"]["TOKEN"] == "original"
    assert "size limit" in broker.server_failures["large"]


def test_server_roster_bounds_count_before_child_launch(
    broker_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.acp import pi_mcp_broker

    monkeypatch.setattr(pi_mcp_broker, "_SERVER_SPEC_MAX_COUNT", 2)
    broker = _broker(
        [{"name": name, "command": "echo"} for name in ("first", "second", "third")],
        socket_path=str(broker_dir / "bounded-count.sock"),
    )
    assert [spec["name"] for spec in broker._specs] == ["first", "second"]
    assert broker.server_failures["<additional servers>"] == "MCP server count limit exceeded"


@pytest.mark.parametrize(
    ("names", "overflow_name"),
    [
        (("<additional servers>",), "<additional servers 1>"),
        (("<additional  servers>", "<additional servers 1>"), "<additional servers 2>"),
    ],
)
def test_server_count_failure_survives_report_name_collision(
    broker_dir: Path, tmp_path: Path, names: tuple[str, ...], overflow_name: str
) -> None:
    from kiro_crew.acp import pi_mcp_broker
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp_backends import ACP_BACKEND_PI

    servers = [
        *({"name": name, "command": "echo"} for name in names),
        *(
            {"name": f"server-{index}", "command": "echo"}
            for index in range(pi_mcp_broker._SERVER_SPEC_MAX_COUNT - len(names))
        ),
        {"name": "over-limit", "command": "echo"},
    ]
    roster = admit_server_roster(servers)
    assert len(roster.specs) == pi_mcp_broker._SERVER_SPEC_MAX_COUNT
    assert roster.expected_names[-1] == overflow_name
    assert roster.failures[overflow_name] == "MCP server count limit exceeded"
    assert names[0] in (spec["name"] for spec in roster.specs)

    broker = _broker(servers, socket_path=str(broker_dir / "colliding-report.sock"), roster=roster)
    broker._children[names[0]] = object()
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
    client._pi_mcp_broker = broker
    client._pi_mcp_expected_servers = roster.expected_names
    client._begin_session_report([])
    report = client.mcp_session_report().payload()
    assert report is not None
    assert "<additional servers>" in report["ready"]
    assert overflow_name in report["failed"]
    assert report["failures"][overflow_name] == "MCP server count limit exceeded"


def test_admitted_roster_bounds_report_names_and_is_reused_by_broker(
    broker_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.acp import pi_mcp_broker

    monkeypatch.setattr(pi_mcp_broker, "_SERVER_SPEC_MAX_COUNT", 3)
    monkeypatch.setattr(pi_mcp_broker, "_SERVER_NAME_MAX_BYTES", 8)
    servers = [
        {"name": "too-long-name", "command": "echo"},
        {"name": "healthy", "command": "echo"},
        {"name": "large", "command": "echo", "env": {"TOKEN": "x" * 100_000}},
        *({"name": f"tail-{index}", "command": "echo"} for index in range(1_000)),
    ]
    roster = admit_server_roster(servers)
    assert roster.expected_names == (
        "server[0]",
        "healthy",
        "large",
        "<additional servers>",
    )
    assert [spec["name"] for spec in roster.specs] == ["healthy"]
    assert all(len(name.encode("utf-8")) <= 512 for name in roster.expected_names)

    broker = _broker(servers, socket_path=str(broker_dir / "bounded-report.sock"), roster=roster)
    assert [spec["name"] for spec in broker._specs] == ["healthy"]
    assert set(broker.server_failures) == {
        "server[0]",
        "large",
        "<additional servers>",
    }


@pytest.mark.asyncio
async def test_broker_caps_active_clients_and_readmits_after_disconnect(
    broker_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    from kiro_crew.acp import pi_mcp_broker

    monkeypatch.setattr(pi_mcp_broker, "_MAX_ACTIVE_CLIENTS", 2)
    broker = _broker([], socket_path=str(broker_dir / "active-client-cap.sock"))
    release = asyncio.Event()
    release_audit = asyncio.Event()
    audits: list[tuple[str, str, str]] = []

    async def parked_client(_reader, _writer) -> None:
        await release.wait()

    async def audit_peer(caller: str, outcome: str, error: str = "") -> None:
        audits.append((caller, outcome, error))
        await release_audit.wait()

    monkeypatch.setattr(broker, "_on_client", parked_client)
    monkeypatch.setattr(broker, "_audit_peer", audit_peer)
    first, second, overflow, next_writer = (Mock() for _ in range(4))
    try:
        for writer in (first, second, overflow):
            broker._accept_client(asyncio.StreamReader(), writer)
        assert len(broker._clients) == 2
        overflow.close.assert_called_once_with()
        for _ in range(100):
            refused = Mock()
            broker._accept_client(asyncio.StreamReader(), refused)
            refused.close.assert_called_once_with()
        await asyncio.sleep(0)
        assert len(broker._capacity_audits) == 1
        assert audits == [("unverified-peer", "denied", "capacity")]
        release_audit.set()
        await asyncio.gather(*broker._capacity_audits)
        assert audits == [("unverified-peer", "denied", "capacity")]
        first.close.assert_not_called()
        second.close.assert_not_called()

        release.set()
        await asyncio.gather(*broker._clients)
        await asyncio.sleep(0)
        assert not broker._clients

        broker._accept_client(asyncio.StreamReader(), next_writer)
        next_writer.close.assert_not_called()
    finally:
        release_audit.set()
        release.set()
        await asyncio.gather(*broker._clients, return_exceptions=True)


def test_only_verified_host_children_omit_the_pi_credential_mask(broker_dir: Path) -> None:
    broker = _broker(
        [],
        socket_path=str(broker_dir / "unused.sock"),
        hidden_dirs=("/private/credentials",),
        host_control_plane_servers=frozenset({"kirocrew-core"}),
    )
    assert broker._child_hidden_dirs("kirocrew-core", "kirocrew-core") == ()
    assert broker._child_hidden_dirs("kirocrew-core", " kirocrew-core") == ("/private/credentials",)
    assert broker._child_hidden_dirs("kirocrew-cron", "kirocrew-cron") == ("/private/credentials",)
    assert broker._child_hidden_dirs("third-party", "third-party") == ("/private/credentials",)


@pytest.mark.asyncio
async def test_padded_server_name_cannot_collide_with_managed_core(broker_dir: Path) -> None:
    broker = _broker(
        [{"name": " kirocrew-core", "command": "/not/a/real/command"}],
        socket_path=str(broker_dir / "padded.sock"),
        hidden_dirs=("/private/credentials",),
        host_control_plane_servers=frozenset({"kirocrew-core"}),
    )
    await broker.start()
    try:
        assert broker.initialized_servers == frozenset()
        assert not broker._tool_index
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_broker_lists_and_calls_a_stdio_echo_server(
    broker_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Minimal stdio MCP server: initialize → tools/list → tools/call.
    from kiro_crew.acp import pi_mcp_broker

    events: list[tuple[str, dict]] = []

    class AuditLog:
        def log_api_access(self, **kwargs):
            events.append(("peer", kwargs))

        def log_tool_invocation(self, **kwargs):
            events.append(("tool", kwargs))

    monkeypatch.setattr(pi_mcp_broker, "sel", AuditLog)
    script = tmp_path / "echo_mcp.py"
    script.write_text(
        """\
import json, os, sys
def recv():
    line = sys.stdin.readline()
    return json.loads(line) if line else None
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()
while True:
    msg = recv()
    if msg is None:
        break
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"echo","version":"0"}}})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send({"jsonrpc":"2.0","id":mid,"result":{"tools":[{"name":"ping","description":"pong","inputSchema":{"type":"object","properties":{}}}]}})
    elif method == "tools/call":
        args = (msg.get("params") or {}).get("arguments") or {}
        send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":"pong:"+json.dumps(args)+":"+os.getcwd()}],"isError":args.get("fail",False)}})
    else:
        send({"jsonrpc":"2.0","id":mid,"error":{"code":-32601,"message":"unknown"}})
""",
        encoding="utf-8",
    )
    sock = broker_socket_path(artifact_dir=str(broker_dir), pid=99, nonce="abc")
    broker = _broker(
        [{"name": "echo", "command": sys.executable, "args": [str(script)]}],
        socket_path=sock,
        work_dir=str(tmp_path),
    )
    await broker.start()
    try:
        assert len(broker._tool_index) == 1
        reader, writer = await transport.connect(sock)
        try:
            writer.write(
                (
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "bridge/list", "params": {}})
                    + "\n"
                ).encode()
            )
            await writer.drain()
            listed = json.loads((await reader.readline()).decode())
            assert listed["id"] == 1
            tools = listed["result"]["tools"]
            assert tools[0]["server"] == "echo"
            assert tools[0]["name"] == "ping"
            broker.note_permission(
                "r", {"toolCallId": "call", "title": "mcp__echo__ping", "input": {"x": 1}}
            )
            broker.approve_permission("r", broker.stage_permission("r"))
            writer.write(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "bridge/call",
                            "params": {
                                "toolCallId": "call",
                                "server": "echo",
                                "tool": "ping",
                                "arguments": {"x": 1},
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            called = json.loads((await reader.readline()).decode())
            assert called["id"] == 2
            assert "pong" in called["result"]["content"][0]["text"]
            assert str(tmp_path) in called["result"]["content"][0]["text"]
            broker.note_permission(
                "r-error",
                {
                    "toolCallId": "call-error",
                    "title": "mcp__echo__ping",
                    "input": {"fail": True},
                },
            )
            broker.approve_permission("r-error", broker.stage_permission("r-error"))
            writer.write(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "bridge/call",
                            "params": {
                                "toolCallId": "call-error",
                                "server": "echo",
                                "tool": "ping",
                                "arguments": {"fail": True},
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            failed = json.loads((await reader.readline()).decode())
            assert failed["result"]["isError"] is True
            writer.write(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 4,
                            "method": "bridge/call",
                            "params": {
                                "toolCallId": "call",
                                "server": "echo",
                                "tool": "ping",
                                "arguments": {"x": 1},
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            replay = json.loads((await reader.readline()).decode())
            assert "host-approved" in replay["error"]["message"]
            # The next response waits behind the replay's SEL audit on this
            # connection, so assertions below see a settled audit sequence.
            writer.write(
                (json.dumps({"jsonrpc": "2.0", "id": 5, "method": "bridge/list"}) + "\n").encode()
            )
            await writer.drain()
            assert json.loads((await reader.readline()).decode())["id"] == 5
            assert (
                "peer",
                {
                    "caller": "pi-session",
                    "operation": "pi-mcp-broker.connect",
                    "outcome": "allowed",
                    "source": "acp",
                    "error": "",
                },
            ) in events
            assert [row["outcome"] for kind, row in events if kind == "tool"] == [
                "completed",
                "failed",
                "denied",
            ]
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_broker_audits_denied_peer(broker_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.acp import pi_mcp_broker
    from kiro_crew.mcp_gateway.socketsec import PeerCredResult

    events: list[dict] = []

    class AuditLog:
        def log_api_access(self, **kwargs):
            events.append(kwargs)

    monkeypatch.setattr(pi_mcp_broker, "sel", AuditLog)
    monkeypatch.setattr(
        pi_mcp_broker.socketsec, "check_peer_is_self", lambda _writer: PeerCredResult.MISMATCH
    )
    writer = MagicMock()
    writer.wait_closed = AsyncMock()
    broker = _broker([], socket_path=str(broker_dir / "denied.sock"))
    await broker._on_client(asyncio.StreamReader(), writer)
    writer.close.assert_called_once()
    assert events == [
        {
            "caller": "unverified-peer",
            "operation": "pi-mcp-broker.connect",
            "outcome": "denied",
            "source": "acp",
            "error": "mismatch",
        }
    ]


@pytest.mark.asyncio
async def test_broker_rejects_missing_session_work_dir(broker_dir: Path) -> None:
    broker = _broker(
        [{"name": "echo", "command": sys.executable}],
        socket_path=str(broker_dir / "missing-cwd.sock"),
    )
    with pytest.raises(ValueError, match="session work directory"):
        await broker.start()


@pytest.mark.asyncio
async def test_broker_rejects_invalid_specs_without_losing_sibling(
    broker_dir: Path, tmp_path: Path
) -> None:
    script = _stdio_mcp_script(tmp_path, tools=[{"name": "ping"}], name="healthy")
    broker = _broker(
        [
            {"name": "relative", "command": "./echo"},
            {"name": "duplicate", "command": sys.executable},
            {"name": "duplicate", "command": sys.executable},
            {"name": "healthy", "command": sys.executable, "args": [str(script)]},
        ],
        socket_path=str(broker_dir / "relative-command.sock"),
        work_dir=str(tmp_path),
    )
    try:
        await broker.start()
        assert broker.initialized_servers == {"healthy"}
        assert broker.server_failures["relative"] == (
            "Pi MCP child command must be absolute or a bare executable name"
        )
        assert broker.server_failures["duplicate"] == "Pi MCP child name is duplicated"
    finally:
        await broker.stop()


def test_windows_spawn_env_screens_path_case_and_workspace_entries(tmp_path: Path) -> None:
    from kiro_crew.acp.pi_mcp_broker import _windows_spawn_env

    safe_dir = os.path.dirname(sys.executable)
    screened = _windows_spawn_env(
        {"PATH": "/shadowed", "Path": os.pathsep.join((".", str(tmp_path), safe_dir))},
        str(tmp_path),
    )
    assert "Path" not in screened
    assert "PATH" in screened
    assert str(tmp_path) not in screened["PATH"].split(os.pathsep)
    assert os.path.realpath(safe_dir) in screened["PATH"].split(os.pathsep)


@pytest.mark.asyncio
async def test_work_dir_fd_close_finishes_off_loop_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    from types import SimpleNamespace

    from kiro_crew.acp import pi_mcp_broker

    read_fd, write_fd = os.pipe()
    started = threading.Event()
    release = threading.Event()
    close_threads: list[int] = []

    def close(fd: int) -> None:
        close_threads.append(threading.get_ident())
        started.set()
        release.wait(timeout=10)
        os.close(fd)

    monkeypatch.setattr(pi_mcp_broker, "os", SimpleNamespace(close=close))
    try:
        closing = asyncio.create_task(pi_mcp_broker._close_work_dir_fd(read_fd))
        assert await asyncio.to_thread(started.wait, 10)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert len(close_threads) == 1
        assert close_threads[0] != threading.get_ident()
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        release.set()
        with contextlib.suppress(OSError):
            os.close(read_fd)
        os.close(write_fd)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable and workspace pin")
async def test_broker_never_resolves_a_command_from_the_workspace(
    broker_dir: Path, tmp_path: Path
) -> None:
    script = _stdio_mcp_script(tmp_path, tools=[{"name": "ping"}])
    command = os.path.basename(sys.executable)
    marker = tmp_path / "wrong-executable-ran"
    planted = tmp_path / command
    planted.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    planted.chmod(0o755)
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    broker = _broker(
        [
            {
                "name": "echo",
                "command": command,
                "args": [str(script)],
                "env": {
                    "PATH": os.pathsep.join(
                        (".", str(tmp_path), str(alias), os.path.dirname(sys.executable))
                    )
                },
            }
        ],
        socket_path=str(broker_dir / "screened-path.sock"),
        work_dir=str(tmp_path),
    )
    try:
        await broker.start()
        assert broker.initialized_servers == {"echo"}
        assert not marker.exists()
    finally:
        await broker.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_broker_refuses_to_signal_a_group_without_an_owned_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from kiro_crew.acp import pi_mcp_broker

    child = pi_mcp_broker._McpChild(
        name="stale",
        process=SimpleNamespace(pid=_UNALLOCATABLE_PID, returncode=0),
        start_id="old-process",
        pgid=_UNALLOCATABLE_PID,
    )
    signal_group = Mock()
    monkeypatch.setattr(pi_mcp_broker.process_identity, "_group_member_vouches", lambda _: False)
    monkeypatch.setattr(pi_mcp_broker.platform_compat, "pgroup_exists", lambda _: True)
    monkeypatch.setattr(pi_mcp_broker.platform_compat, "kill_process_group", signal_group)
    with pytest.raises(RuntimeError, match="no verified member"):
        child._kill_verified_group()
    signal_group.assert_not_called()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
@pytest.mark.parametrize("allowed", [False, True])
def test_broker_cleans_verified_detached_children_even_if_group_was_reused(
    monkeypatch: pytest.MonkeyPatch, allowed: bool
) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from kiro_crew.acp import pi_mcp_broker

    child = pi_mcp_broker._McpChild(
        name="detached",
        process=SimpleNamespace(pid=_UNALLOCATABLE_PID, returncode=0),
        start_id="old-process",
        pgid=500,
        descendant_start_ids={601: "detached-start", 602: "in-group", 603: "recycled"},
    )
    starts = {601: "detached-start", 602: "in-group", 603: "new-owner"}
    signalled = Mock()
    authorize = Mock(return_value=allowed)
    monkeypatch.setattr(pi_mcp_broker.process_identity, "_group_member_vouches", lambda _: False)
    monkeypatch.setattr(pi_mcp_broker.platform_compat, "pgroup_exists", lambda _: True)
    monkeypatch.setattr(pi_mcp_broker.platform_compat, "kill_process_group", Mock())
    monkeypatch.setattr(pi_mcp_broker, "authorize_runtime_kill", authorize)
    monkeypatch.setattr(
        pi_mcp_broker.platform_compat, "get_process_start_id", lambda pid: starts.get(pid)
    )
    monkeypatch.setattr(pi_mcp_broker.platform_compat, "pid_is_zombie", lambda _: False)
    monkeypatch.setattr(
        pi_mcp_broker.platform_compat, "pgroup_of", lambda pid: 500 if pid == 602 else 601
    )
    monkeypatch.setattr(pi_mcp_broker.platform_compat, "kill_pid_pinned", signalled)

    with pytest.raises(RuntimeError, match="no verified member"):
        child._kill_verified_tree()
    authorize.assert_called_once_with(
        601,
        reason="Pi MCP detached helper teardown",
        caller="pi_mcp_broker._McpChild.kill",
    )
    if allowed:
        signalled.assert_called_once_with(
            601, "detached-start", pi_mcp_broker.platform_compat.SIGKILL
        )
    else:
        signalled.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
async def test_repeated_descendants_are_bounded_and_overflow_stops_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kiro_crew.acp import pi_mcp_broker

    monkeypatch.setattr(pi_mcp_broker, "_DESCENDANT_IDENTITY_MAX_COUNT", 3)
    snapshots = [
        (601, 602, 603),
        (602, 603, 604),
        (603, 604, 605),
        (603, 604, 605, 606),
    ]
    gone: set[int] = set()
    child = pi_mcp_broker._McpChild(
        name="spawning",
        process=SimpleNamespace(pid=_UNALLOCATABLE_PID, returncode=None),
        start_id="root",
        pgid=_UNALLOCATABLE_PID,
    )
    kill = AsyncMock()
    monkeypatch.setattr(child, "kill", kill)
    monkeypatch.setattr(
        pi_mcp_broker.platform_compat,
        "process_descendant_identities",
        lambda root: [
            pi_mcp_broker.platform_compat.ProcessDescendantIdentity(pid, root, f"start-{pid}")
            for pid in snapshots.pop(0)
        ],
    )
    monkeypatch.setattr(
        pi_mcp_broker.platform_compat,
        "get_process_start_id",
        lambda pid: None if pid in gone else f"start-{pid}",
    )
    monkeypatch.setattr(
        pi_mcp_broker.platform_compat,
        "pid_liveness",
        lambda pid: (
            pi_mcp_broker.platform_compat.PID_DEAD
            if pid in gone
            else pi_mcp_broker.platform_compat.PID_ALIVE
        ),
    )
    await child.remember_descendants()
    assert set(child.descendant_start_ids) == {601, 602, 603}
    gone.add(601)
    await child.remember_descendants()
    assert set(child.descendant_start_ids) == {602, 603, 604}
    gone.add(602)
    await child.remember_descendants()
    assert set(child.descendant_start_ids) == {603, 604, 605}
    kill.assert_not_awaited()

    with pytest.raises(RuntimeError, match="descendant identity limit"):
        await child.remember_descendants()
    assert len(child.descendant_start_ids) == 3
    assert child.descendant_overflowed
    kill.assert_awaited_once()
    with pytest.raises(RuntimeError, match="descendant identity limit"):
        await child.request("tools/call", {}, timeout=1)
    assert not snapshots


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform not in {"linux", "darwin"}, reason="atomic POSIX descendant identities"
)
async def test_broker_reaps_helper_that_detaches_after_identity_capture(
    tmp_path: Path,
) -> None:
    from kiro_crew import platform_compat, process_identity
    from kiro_crew.acp.pi_mcp_broker import _McpChild

    script = tmp_path / "detaching_server.py"
    child_pid_file = tmp_path / "child.pid"
    detach_trigger = tmp_path / "detach"
    detached_marker = tmp_path / "detached"
    script.write_text(
        """
import os
import sys
import time
from pathlib import Path

pid_file, trigger, detached = (Path(arg) for arg in sys.argv[1:])
if os.fork() == 0:
    pid_file.write_text(str(os.getpid()))
    while not trigger.exists():
        time.sleep(0.01)
    os.setsid()
    detached.write_text("ready")
while True:
    time.sleep(1)
""",
        encoding="utf-8",
    )

    async def read_when_present(path: Path) -> str:
        for _ in range(500):
            if path.exists():
                return path.read_text(encoding="utf-8")
            await asyncio.sleep(0.01)
        raise AssertionError(f"{path.name} did not appear")

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        str(child_pid_file),
        str(detach_trigger),
        str(detached_marker),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=tmp_path,
        start_new_session=True,
    )
    helper_pid = -1
    helper_start: str | None = None
    try:
        helper_pid = int(await read_when_present(child_pid_file))
        helper_start = platform_compat.get_process_start_id(helper_pid)
        root_start = platform_compat.get_process_start_id(process.pid)
        assert helper_start is not None and root_start is not None
        child = _McpChild(
            name="detaching",
            process=process,
            start_id=root_start,
            pgid=process_identity.isolated_group_of(process.pid, root_start),
        )
        assert child.pgid == process.pid
        await child.remember_descendants()
        assert child.descendant_start_ids[helper_pid] == helper_start
        detach_trigger.write_text("go", encoding="utf-8")
        await read_when_present(detached_marker)
        assert platform_compat.pgroup_of(helper_pid) != child.pgid

        await child.kill()
        for _ in range(200):
            if (
                platform_compat.get_process_start_id(helper_pid) != helper_start
                or platform_compat.pid_is_zombie(helper_pid) is True
            ):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("recorded detached MCP helper survived broker teardown")
    finally:
        if helper_start and platform_compat.get_process_start_id(helper_pid) == helper_start:
            with contextlib.suppress(ProcessLookupError):
                platform_compat.kill_pid(helper_pid, platform_compat.SIGKILL)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        await asyncio.wait_for(process.wait(), timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [False, True])
async def test_broker_replies_before_slow_tool_audit(
    broker_dir: Path, monkeypatch: pytest.MonkeyPatch, approved: bool
) -> None:
    from types import SimpleNamespace

    broker = _broker([], socket_path=str(broker_dir / "audit-order.sock"))
    audit_started = asyncio.Event()
    release_audit = asyncio.Event()
    replied = asyncio.Event()
    frames: list[bytes] = []

    async def audit(_server: str, _tool: str, _outcome: str) -> None:
        audit_started.set()
        await release_audit.wait()

    async def call_tool(_tool: str, _arguments: dict) -> dict:
        return {"content": [{"type": "text", "text": "ok"}]}

    class Writer:
        def write(self, data: bytes) -> None:
            frames.append(data)

        async def drain(self) -> None:
            replied.set()

    monkeypatch.setattr(broker, "_audit_tool_call", audit)
    broker._children["echo"] = SimpleNamespace(disabled_tools=set(), call_tool=call_tool)
    if approved:
        broker.note_permission("r", {"toolCallId": "c", "title": "mcp__echo__ping", "input": {}})
        broker.approve_permission("r", broker.stage_permission("r"))
    task = asyncio.create_task(
        broker._dispatch(
            Writer(),
            asyncio.Lock(),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "bridge/call",
                "params": {
                    "toolCallId": "c",
                    "server": "echo",
                    "tool": "ping",
                    "arguments": {},
                },
            },
        )
    )
    try:
        await asyncio.wait_for(replied.wait(), timeout=2)
        await asyncio.wait_for(audit_started.wait(), timeout=2)
        assert not task.done()
        response = json.loads(frames[0])
        assert ("result" in response) is approved
    finally:
        release_audit.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"server": "s" * 513, "tool": "ping"},
        {"server": "echo", "tool": "t" * 513},
        {"server": "é" * 257, "tool": "ping"},
        {"server": "echo", "tool": ["ping"]},
    ],
)
async def test_invalid_bridge_call_names_never_reach_audit(
    broker_dir: Path, monkeypatch: pytest.MonkeyPatch, params: dict
) -> None:
    broker = _broker([], socket_path=str(broker_dir / "bounded-name.sock"))
    frames: list[dict] = []
    audits: list[tuple[str, str, str]] = []

    async def audit(server: str, tool: str, outcome: str) -> None:
        audits.append((server, tool, outcome))

    class Writer:
        def write(self, data: bytes) -> None:
            frames.append(json.loads(data))

        async def drain(self) -> None:
            pass

    monkeypatch.setattr(broker, "_audit_tool_call", audit)
    await broker._dispatch(
        Writer(),
        asyncio.Lock(),
        {"jsonrpc": "2.0", "id": 1, "method": "bridge/call", "params": params},
    )
    assert frames[0]["error"]["message"] == "bridge/call names exceed size limit"
    assert audits == [("", "", "denied")]


@pytest.mark.asyncio
async def test_oversized_call_result_returns_error_and_keeps_bridge_usable(
    broker_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from kiro_crew.acp import pi_mcp_broker

    monkeypatch.setattr(pi_mcp_broker, "_BRIDGE_RESPONSE_MAX_BYTES", 256)
    broker = _broker([], socket_path=str(broker_dir / "bounded-result.sock"))
    frames: list[dict] = []
    outcomes: list[str] = []

    async def call_tool(_tool: str, arguments: dict) -> dict:
        text = "x" * 1000 if arguments["large"] else "ok"
        return {"content": [{"type": "text", "text": text}]}

    async def audit(_server: str, _tool: str, outcome: str) -> None:
        outcomes.append(outcome)

    class Writer:
        def write(self, data: bytes) -> None:
            frames.append(json.loads(data))

        async def drain(self) -> None:
            pass

    monkeypatch.setattr(broker, "_audit_tool_call", audit)
    broker._children["echo"] = SimpleNamespace(disabled_tools=set(), call_tool=call_tool)
    for call_id, large in (("large-call", True), ("small-call", False)):
        arguments = {"large": large}
        broker.note_permission(
            call_id,
            {"toolCallId": call_id, "title": "mcp__echo__ping", "input": arguments},
        )
        broker.approve_permission(call_id, broker.stage_permission(call_id))
        await broker._dispatch(
            Writer(),
            asyncio.Lock(),
            {
                "jsonrpc": "2.0",
                "id": call_id,
                "method": "bridge/call",
                "params": {
                    "toolCallId": call_id,
                    "server": "echo",
                    "tool": "ping",
                    "arguments": arguments,
                },
            },
        )
    assert frames[0]["error"]["message"] == "MCP tool result exceeds bridge size limit"
    assert frames[1]["result"]["content"][0]["text"] == "ok"
    assert outcomes == ["failed", "completed"]


@pytest.mark.asyncio
async def test_oversized_child_error_stays_within_bridge_frame_and_keeps_connection_usable(
    broker_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from kiro_crew.acp import pi_mcp_broker

    broker = _broker([], socket_path=str(broker_dir / "bounded-child-error.sock"))
    frames: list[bytes] = []

    async def call_tool(_tool: str, arguments: dict) -> dict:
        if arguments["large"]:
            raise RuntimeError("x" * (8 * 1024 * 1024))
        return {"content": [{"type": "text", "text": "ok"}]}

    async def audit(_server: str, _tool: str, _outcome: str) -> None:
        pass

    class Writer:
        def write(self, data: bytes) -> None:
            frames.append(data)

        async def drain(self) -> None:
            pass

    monkeypatch.setattr(broker, "_audit_tool_call", audit)
    broker._children["echo"] = SimpleNamespace(disabled_tools=set(), call_tool=call_tool)
    for call_id, large in (("large-error", True), ("next-call", False)):
        arguments = {"large": large}
        broker.note_permission(
            call_id,
            {"toolCallId": call_id, "title": "mcp__echo__ping", "input": arguments},
        )
        broker.approve_permission(call_id, broker.stage_permission(call_id))
        await broker._dispatch(
            Writer(),
            asyncio.Lock(),
            {
                "jsonrpc": "2.0",
                "id": call_id,
                "method": "bridge/call",
                "params": {
                    "toolCallId": call_id,
                    "server": "echo",
                    "tool": "ping",
                    "arguments": arguments,
                },
            },
        )
    assert len(frames[0]) < pi_mcp_broker._BRIDGE_RESPONSE_MAX_BYTES
    assert len(json.loads(frames[0])["error"]["message"]) == pi_mcp_broker._BRIDGE_ERROR_MAX_CHARS
    assert json.loads(frames[1])["result"]["content"][0]["text"] == "ok"


@pytest.mark.asyncio
async def test_child_error_redacts_secrets_before_bridge_reply(
    broker_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.acp import pi_mcp_broker

    secret = "sk-ant-api03-" + "A" * 100
    exfil_url = "https://collector.example/upload?data=" + "aB3_xY9-" * 30
    script = _stdio_mcp_script(
        tmp_path,
        tools=[{"name": "ping"}],
        name="error",
        error=f"child refused Authorization: Bearer {secret} at {exfil_url}",
    )
    socket_path = broker_socket_path(artifact_dir=str(broker_dir), pid=42, nonce="secret")
    broker = _broker(
        [{"name": "error", "command": sys.executable, "args": [str(script)]}],
        socket_path=socket_path,
        work_dir=str(tmp_path),
    )

    async def audit(_server: str, _tool: str, _outcome: str) -> None:
        pass

    monkeypatch.setattr(broker, "_audit_tool_call", audit)
    await broker.start()
    try:
        reader, writer = await transport.connect(socket_path)
        try:
            broker.note_permission(
                "request", {"toolCallId": "call", "title": "mcp__error__ping", "input": {}}
            )
            broker.approve_permission("request", broker.stage_permission("request"))
            writer.write(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": "call",
                            "method": "bridge/call",
                            "params": {
                                "toolCallId": "call",
                                "server": "error",
                                "tool": "ping",
                                "arguments": {},
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            frame = json.loads(await reader.readline())
            message = frame["error"]["message"]
            assert "child refused" in message
            assert secret not in message
            assert "collector.example/upload" not in message
            assert exfil_url not in message
            assert len(message) <= pi_mcp_broker._BRIDGE_ERROR_MAX_CHARS
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await broker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["result", "error"])
async def test_opaque_server_env_is_scrubbed_from_child_output(
    broker_dir: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
) -> None:
    from kiro_crew.acp import pi_mcp_broker

    secret = "~?~?~?~?"
    representations = (
        secret,
        base64.b64encode(secret.encode()).decode(),
        base64.urlsafe_b64encode(secret.encode()).decode(),
    )
    script = tmp_path / "opaque_mcp.py"
    script.write_text(
        "import base64, json, os, sys\n"
        "secret = os.environ['OPAQUE_CHILD_KEY']\n"
        "encoded = base64.b64encode(secret.encode()).decode()\n"
        "encoded_url = base64.urlsafe_b64encode(secret.encode()).decode()\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    mid = msg.get('id')\n"
        "    if mid is None:\n"
        "        continue\n"
        "    method = msg.get('method')\n"
        "    if method == 'initialize':\n"
        "        result = {'protocolVersion': '2024-11-05', 'capabilities': {}}\n"
        "    elif method == 'tools/list':\n"
        "        result = {'tools': [{'name': 'echo', 'inputSchema': {'type': 'object'}}]}\n"
        "    else:\n"
        "        print('child stdout ' + secret + ' ' + encoded, flush=True)\n"
        "        print('child stderr ' + secret + ' ' + encoded_url, "
        "file=sys.stderr, flush=True)\n"
        f"        if {outcome!r} == 'error':\n"
        "            print(json.dumps({'jsonrpc': '2.0', 'id': mid, "
        "'error': {'code': -32000, 'message': 'child error ' + secret + ' ' + "
        "encoded + ' ' + encoded_url}}), flush=True)\n"
        "            continue\n"
        "        result = {'content': [{'type': 'text', 'text': 'child result ' + secret "
        "+ ' ' + encoded + ' ' + encoded_url}], "
        "'nested': {'key-' + secret: [secret, encoded, encoded_url]}}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': mid, 'result': result}), flush=True)\n",
        encoding="utf-8",
    )
    sock = str(broker_dir / "opaque.sock")
    broker = _broker(
        [
            {
                "name": "opaque",
                "command": sys.executable,
                "args": [str(script)],
                "env": {"OPAQUE_CHILD_KEY": secret},
            }
        ],
        socket_path=sock,
        work_dir=str(tmp_path),
    )
    caplog.set_level("DEBUG", logger=pi_mcp_broker.__name__)
    await broker.start()
    try:
        assert broker._tool_index[0]["name"] == "echo"
        reader, writer = await transport.connect(sock)
        try:
            broker.note_permission(
                "request", {"toolCallId": "call", "title": "mcp__opaque__echo", "input": {}}
            )
            broker.approve_permission("request", broker.stage_permission("request"))
            writer.write(
                b'{"jsonrpc":"2.0","id":"call","method":"bridge/call",'
                b'"params":{"toolCallId":"call","server":"opaque","tool":"echo","arguments":{}}}\n'
            )
            await writer.drain()
            frame = await reader.readline()
            assert all(value.encode() not in frame for value in representations)
            assert b"[REDACTED: server env]" in frame
            payload = json.loads(frame)
            if outcome == "error":
                assert "child error" in payload["error"]["message"]
            else:
                assert "child result" in payload["result"]["content"][0]["text"]
                assert (
                    payload["result"]["nested"]["key-[REDACTED: server env]"]
                    == ["[REDACTED: server env]"] * 3
                )
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await broker.stop()
    assert all(value not in caplog.text for value in representations)
    assert "child stdout [REDACTED: server env] [REDACTED: server env]" in caplog.text
    assert "child stderr [REDACTED: server env] [REDACTED: server env]" in caplog.text


@pytest.mark.parametrize("secret", ["a", "~?~?"])
def test_unpadded_encoded_child_env_is_scrubbed(secret: str) -> None:
    from kiro_crew.acp.pi_mcp_broker import _redact_metadata

    standard = base64.b64encode(secret.encode()).decode().rstrip("=")
    urlsafe = base64.urlsafe_b64encode(secret.encode()).decode().rstrip("=")
    payload = {"content": [{"text": standard}, {"text": urlsafe}]}
    assert _redact_metadata(payload, (secret,)) == {
        "content": [
            {"text": "[REDACTED: server env]"},
            {"text": "[REDACTED: server env]"},
        ]
    }


@pytest.mark.parametrize("encode", [base64.b64encode, base64.urlsafe_b64encode])
@pytest.mark.parametrize("padding", [True, False])
def test_child_env_inside_encoded_basic_auth_is_scrubbed(encode, padding: bool) -> None:
    from kiro_crew.acp.pi_mcp_broker import _redact_metadata

    secret = "~?~?~?~?"
    token = encode(("user:" + secret).encode()).decode()
    if not padding:
        token = token.rstrip("=")
    assert _redact_metadata("Basic " + token, (secret,)) == "Basic [REDACTED: server env]"


def test_metadata_redaction_excludes_identity_labels_but_keeps_short_tokens() -> None:
    from kiro_crew.acp.pi_mcp_broker import _metadata_secret_values

    secrets = _metadata_secret_values(
        {
            "KIROCREW_BOUND_PORT": "8080",
            "KIROCREW_CHANNEL_ID": "dashboard:1",
            "DEBUG": "1",
            "OPAQUE_CHILD_KEY": "a",
            "CUSTOM_TOKEN": "~?~?",
        }
    )
    assert secrets == {"a", "~?~?"}


def test_ordinary_declared_env_value_neither_redacts_metadata_nor_hides_a_tool() -> None:
    """A length-only secret rule made a server's own hostname censor its tools."""
    from kiro_crew.acp.pi_mcp_broker import _metadata_secret_values, _sanitize_tool_list

    declared = {
        "GITHUB_HOST": "github.com",
        "NODE_ENV": "production",
        "AWS_REGION": "eu-west-1",
        "MCP_TRANSPORT": "stdio-pipe",
        "LOG_LEVEL": "information",
    }
    assert _metadata_secret_values(declared) == set()

    secrets = tuple(sorted(_metadata_secret_values(declared), key=len, reverse=True))
    tools, withheld = _sanitize_tool_list(
        [
            {
                "name": "github.com_search_issues",
                "description": "Search issues on github.com in production.",
                "inputSchema": {"type": "object", "properties": {"host": {"const": "github.com"}}},
            }
        ],
        secrets,
        "github",
    )
    assert withheld == 0
    assert [tool["name"] for tool in tools] == ["github.com_search_issues"]
    assert tools[0]["description"] == "Search issues on github.com in production."
    assert tools[0]["inputSchema"]["properties"]["host"]["const"] == "github.com"


def test_credential_declared_env_values_still_redact_and_withhold() -> None:
    """The fail-safe direction: a named credential, and an unnamed token's shape."""
    from kiro_crew.acp.pi_mcp_broker import (
        _metadata_secret_values,
        _redact_metadata,
        _sanitize_tool_list,
    )

    named = "ghp0A1b2C3d4E5f6G7h8"
    shaped = "Zk9xQ2p7Rt4Lm8Nv3Bw6"
    cookie = "abcdefghijklmnopqrstuvwxyz"
    cookies = "shortsessioncrumb"
    declared = {
        "GITHUB_HOST": "github.com",
        "GITHUB_TOKEN": named,
        "SIDECAR_VALUE": shaped,
        "GITHUB_APIKEY": "s3",
        "COOKIE": cookie,
        "SESSION_COOKIES": cookies,
    }
    assert _metadata_secret_values(declared) == {named, shaped, "s3", cookie, cookies}

    secrets = tuple(sorted(_metadata_secret_values(declared), key=len, reverse=True))
    tools, withheld = _sanitize_tool_list(
        [
            {"name": f"leak_{named}", "description": "", "inputSchema": {}},
            {
                "name": "search_issues",
                "description": f"Uses {named}, {shaped}, and {cookie} against github.com.",
                "inputSchema": {"type": "object", "properties": {"t": {"const": shaped}}},
            },
        ],
        secrets,
        "github",
    )
    assert withheld == 1
    assert [tool["name"] for tool in tools] == ["search_issues"]
    assert named not in tools[0]["description"]
    assert shaped not in tools[0]["description"]
    assert cookie not in tools[0]["description"]
    assert "github.com" in tools[0]["description"]
    assert shaped not in json.dumps(tools[0]["inputSchema"])
    assert (
        cookies
        not in _redact_metadata({"content": [{"text": f"Child returned {cookies}"}]}, secrets)[
            "content"
        ][0]["text"]
    )


@pytest.mark.parametrize("secrets", [(), ("child-secret-value",)])
def test_child_exfiltration_url_is_scrubbed_before_pi_receives_metadata(secrets) -> None:
    from kiro_crew.acp.pi_mcp_broker import _redact_metadata
    from kiro_crew.security import EXFILTRATION_REDACTION_TAG_PREFIX

    url = "https://evil.example.com/steal?data=" + "A" * 250
    payload = {"content": [{"text": "Result: " + url}]}
    scrubbed = _redact_metadata(payload, secrets)
    assert url not in str(scrubbed)
    assert EXFILTRATION_REDACTION_TAG_PREFIX in scrubbed["content"][0]["text"]


@pytest.mark.asyncio
async def test_cancelled_broker_call_finishes_its_audit(
    broker_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    broker = _broker([], socket_path=str(broker_dir / "cancel-audit.sock"))
    call_started = asyncio.Event()
    audit_started = asyncio.Event()
    release_audit = asyncio.Event()
    outcomes: list[str] = []

    async def call_tool(_tool: str, _arguments: dict) -> None:
        call_started.set()
        await asyncio.Event().wait()

    async def audit(_server: str, _tool: str, outcome: str) -> None:
        outcomes.append(outcome)
        audit_started.set()
        await release_audit.wait()

    broker._children["echo"] = SimpleNamespace(disabled_tools=set(), call_tool=call_tool)
    broker.note_permission(
        "request-1",
        {
            "toolCallId": "call-1",
            "title": "mcp__echo__ping",
            "input": {},
        },
    )
    broker.approve_permission("request-1", broker.stage_permission("request-1"))
    monkeypatch.setattr(broker, "_audit_tool_call", audit)
    dispatch = asyncio.create_task(
        broker._dispatch(
            object(),
            asyncio.Lock(),
            {
                "id": 1,
                "method": "bridge/call",
                "params": {
                    "server": "echo",
                    "tool": "ping",
                    "toolCallId": "call-1",
                    "arguments": {},
                },
            },
        )
    )
    await call_started.wait()
    dispatch.cancel()
    await audit_started.wait()
    assert not dispatch.done()
    release_audit.set()
    with pytest.raises(asyncio.CancelledError):
        await dispatch
    assert outcomes == ["cancelled"]


@pytest.mark.asyncio
async def test_windows_broker_resumes_child_before_handshake(
    broker_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kiro_crew.acp import client as acp_client
    from kiro_crew.acp import pi_mcp_broker

    steps: list[str] = []
    stdout = asyncio.StreamReader()
    stderr = asyncio.StreamReader()
    stdout.feed_eof()
    stderr.feed_eof()
    process = SimpleNamespace(pid=_UNALLOCATABLE_PID, stdout=stdout, stderr=stderr, returncode=0)

    async def create_owned(factory):
        assert factory.keywords["cwd"] == str(tmp_path)
        return process

    async def handshake(_child):
        assert steps == ["resume"]
        steps.append("handshake")

    monkeypatch.setattr(pi_mcp_broker.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        pi_mcp_broker.platform_compat, "create_windows_cleanup_owned_process", create_owned
    )
    monkeypatch.setattr(
        pi_mcp_broker.platform_compat, "terminate_windows_asyncio_tree", AsyncMock()
    )
    monkeypatch.setattr(
        acp_client, "finish_suspended_spawn", lambda *_args, **_kwargs: steps.append("resume")
    )
    monkeypatch.setattr(pi_mcp_broker._McpChild, "handshake", handshake)
    broker = _broker(
        [{"name": "echo", "command": sys.executable}],
        socket_path=str(broker_dir / "windows.sock"),
        work_dir=str(tmp_path),
    )
    try:
        await broker._spawn_children()
        assert steps == ["resume", "handshake"]
    finally:
        await broker.stop()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
async def test_broker_reaps_child_after_mcp_leader_exits(broker_dir: Path, tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    exit_file = tmp_path / "leader-exit"
    script = tmp_path / "leader.py"
    script.write_text(
        """\
import json, os, subprocess, sys, time
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
with open(sys.argv[1], "w", encoding="utf-8") as out:
    out.write(str(child.pid))
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "leader", "version": "0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "ping", "inputSchema": {"type": "object"}}]}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
    if method == "tools/list":
        while not os.path.exists(sys.argv[2]):
            time.sleep(0.01)
        break
""",
        encoding="utf-8",
    )
    broker = _broker(
        [
            {
                "name": "leader",
                "command": sys.executable,
                "args": [str(script), str(pid_file), str(exit_file)],
            }
        ],
        socket_path=str(broker_dir / "leader.sock"),
        work_dir=str(tmp_path),
    )
    grandchild_pid: int | None = None
    try:
        await broker.start()
        child = broker._children["leader"]
        assert child.pgid == child.process.pid
        grandchild_pid = int(pid_file.read_text())
        assert os.getpgid(grandchild_pid) == child.pgid
        exit_file.touch()
        await asyncio.wait_for(child.process.wait(), timeout=5)
        await broker.stop()
        for _ in range(50):
            if not _running(grandchild_pid):
                break
            await asyncio.sleep(0.05)
        assert not _running(grandchild_pid)
    finally:
        await broker.stop()
        if grandchild_pid is not None and _running(grandchild_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(grandchild_pid, signal.SIGKILL)


def _running(pid: int) -> bool:
    from kiro_crew import platform_compat

    return platform_compat.pid_exists(pid) and platform_compat.pid_is_zombie(pid) is not True


@pytest.mark.asyncio
async def test_broker_endpoint_carries_no_server_env(broker_dir: Path, tmp_path: Path) -> None:
    """F1 posture: the advertised endpoint string must not embed secrets."""
    script = tmp_path / "noop_mcp.py"
    script.write_text(
        "import json,sys\n"
        "def recv():\n"
        "    line=sys.stdin.readline()\n"
        "    return json.loads(line) if line else None\n"
        "def send(o):\n"
        "    sys.stdout.write(json.dumps(o)+'\\n'); sys.stdout.flush()\n"
        "while True:\n"
        "    m=recv()\n"
        "    if m is None: break\n"
        "    if m.get('method')=='initialize':\n"
        "        send({'jsonrpc':'2.0','id':m['id'],'result':{'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'n','version':'0'}}})\n"
        "    elif m.get('method')=='tools/list':\n"
        "        send({'jsonrpc':'2.0','id':m['id'],'result':{'tools':[]}})\n",
        encoding="utf-8",
    )
    secret = "super-secret-bearer-token-xyz"
    sock = broker_socket_path(artifact_dir=str(broker_dir), pid=7, nonce="nonce1")
    broker = _broker(
        [
            {
                "name": "core",
                "command": sys.executable,
                "args": [str(script)],
                "env": [{"name": "KIROCREW_SESSION_KEY", "value": secret}],
            }
        ],
        socket_path=sock,
        work_dir=str(tmp_path),
    )
    await broker.start()
    try:
        assert secret not in broker.endpoint
        assert secret not in broker._socket_path
        assert ENV_BROKER_SOCK not in broker.endpoint
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_broker_sanitizes_spec_env_before_trusted_identity(
    broker_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.acp import pi_mcp_broker

    monkeypatch.setenv("HTTPS_PROXY", "http://host-user:host-secret@proxy.example:8080")
    monkeypatch.setenv("http_proxy", "http://proxy.example:8080")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "untrusted-modules"))
    monkeypatch.setenv("NODE_PATH", str(tmp_path / "untrusted-node"))
    monkeypatch.setenv("NODE_OPTIONS", "--require untrusted")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "untrusted-venv"))
    script = _stdio_mcp_script(tmp_path, tools=[], name="env")
    spawn = pi_mcp_broker.create_subprocess_limited
    wrap = pi_mcp_broker.wrap_argv_async
    observed: list[dict[str, str]] = []
    strip_flags: list[bool] = []

    async def capture_spawn(*args, **kwargs):
        observed.append(dict(kwargs["env"]))
        return await spawn(*args, **kwargs)

    async def capture_wrap(*args, **kwargs):
        strip_flags.append(kwargs.get("strip_python_env", False))
        return await wrap(*args, **kwargs)

    monkeypatch.setattr(pi_mcp_broker, "create_subprocess_limited", capture_spawn)
    monkeypatch.setattr(pi_mcp_broker, "wrap_argv_async", capture_wrap)
    broker = _broker(
        [
            {
                "name": "env",
                "command": sys.executable,
                "args": [str(script)],
                "env": [
                    {"name": "LD_PRELOAD", "value": "/untrusted/library.so"},
                    {"name": "dyld_insert_libraries", "value": "/untrusted/library.dylib"},
                    {"name": "PYTHONPATH", "value": "/untrusted"},
                    {"name": "KIROCREW_SESSION_KEY", "value": "forged"},
                    {"name": "KIROCREW_INTERNAL_SECRET", "value": "forged"},
                    {"name": "HELLO", "value": "world"},
                ],
            }
        ],
        socket_path=str(broker_dir / "sanitized-env.sock"),
        work_dir=str(tmp_path),
        host_control_plane_servers=frozenset({"env"}),
        trusted_server_env={"env": {"KIROCREW_SESSION_KEY": "host-session"}},
    )
    try:
        await broker.start()
        assert broker.initialized_servers == frozenset({"env"})
        assert len(observed) == 1
        assert strip_flags == [True]
        env = observed[0]
        assert env["HELLO"] == "world"
        assert env["KIROCREW_SESSION_KEY"] == "host-session"
        assert "KIROCREW_INTERNAL_SECRET" not in env
        assert "LD_PRELOAD" not in env
        assert "dyld_insert_libraries" not in env
        assert env.get("PYTHONPATH") != "/untrusted"
        assert "HTTPS_PROXY" not in env
        assert env["http_proxy"] == "http://proxy.example:8080"
        assert "PYTHONPATH" not in env
        assert "NODE_PATH" not in env
        assert "NODE_OPTIONS" not in env
        assert "VIRTUAL_ENV" not in env
        assert env["PYTHONNOUSERSITE"] == "1"
        assert env["PYTHONSAFEPATH"] == "1"
    finally:
        await broker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["write", "drain"])
async def test_child_request_releases_pending_on_stdin_failure(failure_at: str) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from kiro_crew.acp.pi_mcp_broker import _McpChild

    stdin = Mock()
    stdin.drain = AsyncMock()
    if failure_at == "write":
        stdin.write.side_effect = BrokenPipeError("closed stdin")
    else:
        stdin.drain.side_effect = BrokenPipeError("closed stdin")
    child = _McpChild(name="broken", process=SimpleNamespace(stdin=stdin))

    for _ in range(3):
        with pytest.raises(BrokenPipeError, match="closed stdin"):
            await child.request("tools/call", {"name": "read"}, timeout=1)
        assert not child.pending


@pytest.mark.asyncio
async def test_child_metadata_scrubs_spawn_env_before_bridge_list(
    broker_dir: Path, tmp_path: Path
) -> None:
    from kiro_crew.mcp_gateway import transport

    declared_secret = "local-secret-value-5937"
    trusted_secret = "trusted-key-4857"
    script = tmp_path / "echo_env_mcp.py"
    script.write_text(
        "import json, os, sys\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    mid = msg.get('id')\n"
        "    if mid is None:\n"
        "        continue\n"
        "    method = msg.get('method')\n"
        "    if method == 'initialize':\n"
        "        result = {'protocolVersion': '2024-11-05', 'capabilities': {}, "
        "'serverInfo': {'name': 'echo-env', 'version': '0'}}\n"
        "    elif method == 'tools/list':\n"
        "        token = os.environ['OPAQUE_KEY']\n"
        "        trusted = os.environ['KIROCREW_SESSION_KEY']\n"
        "        result = {'tools': [\n"
        "            {'name': 'ping', 'description': "
        "'Use ' + token + ' ' + trusted + ' AKIAIOSFODNN7EXAMPLE', "
        "'inputSchema': {'type': 'object', 'properties': {"
        "'input': {'type': 'string', 'description': token}, "
        "'secret-' + trusted: {'type': 'string'}}, 'examples': [trusted]}},\n"
        "            {'name': 'secret-' + token, 'inputSchema': {'type': 'object'}},\n"
        "        ]}\n"
        "    else:\n"
        "        result = {}\n"
        "    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': mid, 'result': result}) + '\\n')\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    sock = str(broker_dir / "echo-env.sock")
    broker = _broker(
        [
            {
                "name": "echo-env",
                "command": sys.executable,
                "args": [str(script)],
                "env": {"OPAQUE_KEY": declared_secret},
            }
        ],
        socket_path=sock,
        work_dir=str(tmp_path),
        trusted_server_env={"echo-env": {"KIROCREW_SESSION_KEY": trusted_secret}},
    )
    await broker.start()
    try:
        reader, writer = await transport.connect(sock)
        try:
            writer.write(b'{"jsonrpc":"2.0","id":1,"method":"bridge/list"}\n')
            await writer.drain()
            frame = await reader.readline()
            tools = json.loads(frame)["result"]["tools"]
            assert len(tools) == 1
            assert tools[0]["name"] == "ping"
            assert tools[0]["inputSchema"]["properties"]["input"]["type"] == "string"
            assert b"local-secret-value-5937" not in frame
            assert b"trusted-key-4857" not in frame
            assert b"AKIAIOSFODNN7EXAMPLE" not in frame
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await broker.stop()


def _stdio_mcp_script(
    tmp_path: Path, *, tools: list[dict], name: str = "fixture", error: str | None = None
) -> Path:
    """Write a minimal NDJSON MCP server that serves the given tools/list."""
    script = tmp_path / f"{name}_mcp.py"
    tools_literal = json.dumps(tools)
    script.write_text(
        "import json, sys\n"
        f"TOOLS = json.loads({tools_literal!r})\n"
        "NAME = " + repr(name) + "\n"
        "ERROR = " + repr(error) + "\n"
        "def recv():\n"
        "    line = sys.stdin.readline()\n"
        "    return json.loads(line) if line else None\n"
        "def send(obj):\n"
        "    sys.stdout.write(json.dumps(obj) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "while True:\n"
        "    msg = recv()\n"
        "    if msg is None:\n"
        "        break\n"
        "    mid = msg.get('id')\n"
        "    method = msg.get('method')\n"
        "    if method == 'initialize':\n"
        "        send({'jsonrpc':'2.0','id':mid,'result':{"
        "'protocolVersion':'2024-11-05','capabilities':{},"
        "'serverInfo':{'name': NAME,'version':'0'}}})\n"
        "    elif method == 'notifications/initialized':\n"
        "        pass\n"
        "    elif method == 'tools/list':\n"
        "        send({'jsonrpc':'2.0','id':mid,'result':{'tools': TOOLS}})\n"
        "    elif method == 'tools/call':\n"
        "        if ERROR is None:\n"
        "            send({'jsonrpc':'2.0','id':mid,'result':{"
        "'content':[{'type':'text','text':'ok'}]}})\n"
        "        else:\n"
        "            send({'jsonrpc':'2.0','id':mid,'error':{"
        "'code':-32000,'message':ERROR}})\n"
        "    else:\n"
        "        send({'jsonrpc':'2.0','id':mid,'error':{"
        "'code':-32601,'message':'unknown'}})\n",
        encoding="utf-8",
    )
    return script


@pytest.mark.asyncio
async def test_default_control_plane_port_does_not_mangle_tool_metadata(
    broker_dir: Path, tmp_path: Path
) -> None:
    script = _stdio_mcp_script(
        tmp_path,
        tools=[
            {
                "name": "read_8080",
                "description": "Read on port 8080",
                "inputSchema": {"type": "object", "description": "port 8080"},
            }
        ],
        name="port",
    )
    broker = _broker(
        [{"name": "port", "command": sys.executable, "args": [str(script)]}],
        socket_path=str(broker_dir / "port.sock"),
        work_dir=str(tmp_path),
        trusted_server_env={
            "port": {"KIROCREW_BOUND_PORT": "8080", "KIROCREW_CHANNEL_ID": "dashboard:1"}
        },
    )
    await broker.start()
    try:
        assert broker._tool_index == [
            {
                "server": "port",
                "name": "read_8080",
                "description": "Read on port 8080",
                "inputSchema": {"type": "object", "description": "port 8080"},
            }
        ]
        assert "port" not in broker.server_failures
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_secret_bearing_tool_name_is_reported_when_withheld(
    broker_dir: Path, tmp_path: Path
) -> None:
    secret = "opaque-child-secret"
    script = _stdio_mcp_script(
        tmp_path,
        tools=[{"name": "safe"}, {"name": "leaked_" + secret}],
        name="secret-name",
    )
    broker = _broker(
        [
            {
                "name": "secret-name",
                "command": sys.executable,
                "args": [str(script)],
                "env": {"OPAQUE_CHILD_KEY": secret},
            }
        ],
        socket_path=str(broker_dir / "secret-name.sock"),
        work_dir=str(tmp_path),
    )
    await broker.start()
    try:
        assert [tool["name"] for tool in broker._tool_index] == ["safe"]
        assert broker.server_failures["secret-name"].startswith("1 MCP tool name")
        assert secret not in broker.server_failures["secret-name"]
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_reader_exit_retires_child_and_unpublishes_its_tools(
    broker_dir: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "exit-now"
    script = tmp_path / "exiting_mcp.py"
    script.write_text(
        "import json, os, sys, time\n"
        f"MARKER = {str(marker)!r}\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    if 'id' not in msg:\n"
        "        continue\n"
        "    method = msg['method']\n"
        "    result = {'tools': [{'name': 'ping'}]} if method == 'tools/list' else {}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}), flush=True)\n"
        "    if method == 'tools/list':\n"
        "        while not os.path.exists(MARKER):\n"
        "            time.sleep(0.01)\n"
        "        break\n",
        encoding="utf-8",
    )
    broker = _broker(
        [{"name": "exiting", "command": sys.executable, "args": [str(script)]}],
        socket_path=str(broker_dir / "exiting.sock"),
        work_dir=str(tmp_path),
    )
    await broker.start()
    try:
        assert broker.initialized_servers == {"exiting"}
        assert [tool["name"] for tool in broker._tool_index] == ["ping"]
        marker.touch()
        for _ in range(100):
            if not broker.initialized_servers:
                break
            await asyncio.sleep(0.02)
        assert broker.initialized_servers == frozenset()
        assert broker._tool_index == []
        assert broker.server_failures["exiting"] == "MCP server stdout reader exited"
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_over_nested_child_frame_does_not_kill_stdout_reader() -> None:
    from types import SimpleNamespace

    from kiro_crew.acp.pi_mcp_broker import _McpChild, _Pending

    stream = asyncio.StreamReader(limit=10_000)
    child = _McpChild(name="nested", process=SimpleNamespace(stdout=stream))
    ready = asyncio.get_running_loop().create_future()
    child.pending[1] = _Pending(future=ready)
    reading = asyncio.create_task(child._read_stdout())
    try:
        stream.feed_data(b"[" * 1500 + b"0" + b"]" * 1500 + b"\n")
        stream.feed_data(b'{"jsonrpc":"2.0","id":1,"result":"ok"}\n')
        assert (await asyncio.wait_for(ready, timeout=1))["result"] == "ok"
        stream.feed_eof()
        await asyncio.wait_for(reading, timeout=1)
    finally:
        reading.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reading


@pytest.mark.asyncio
async def test_child_request_rejects_a_finished_stdout_reader() -> None:
    from types import SimpleNamespace

    from kiro_crew.acp.pi_mcp_broker import _McpChild

    child = _McpChild(name="dead", process=SimpleNamespace())
    child.reader_task = asyncio.create_task(asyncio.sleep(0))
    await child.reader_task
    with pytest.raises(RuntimeError, match="stdout reader exited"):
        await child.request("tools/call", timeout=0.01)
    assert not child.pending


@pytest.mark.asyncio
async def test_slow_child_keeps_healthy_sibling_and_is_reaped(
    broker_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.acp import pi_mcp_broker

    slow = _stdio_mcp_script(tmp_path, tools=[{"name": "stuck"}], name="slow")
    healthy = _stdio_mcp_script(tmp_path, tools=[{"name": "ping"}], name="healthy")
    broker = _broker(
        [
            {"name": name, "command": sys.executable, "args": [str(script)]}
            for name, script in (("slow", slow), ("healthy", healthy))
        ],
        socket_path=str(broker_dir / "slow.sock"),
        work_dir=str(tmp_path),
    )
    original_handshake = pi_mcp_broker._McpChild.handshake
    healthy_started = asyncio.Event()
    slow_children: list[pi_mcp_broker._McpChild] = []
    overlapped: list[bool] = []

    async def handshake(child: pi_mcp_broker._McpChild) -> None:
        if child.name == "slow":
            slow_children.append(child)
            try:
                await asyncio.Event().wait()
            finally:
                overlapped.append(healthy_started.is_set())
        else:
            healthy_started.set()
            await original_handshake(child)

    monkeypatch.setattr(pi_mcp_broker._McpChild, "handshake", handshake)
    monkeypatch.setattr(pi_mcp_broker, "_CHILD_STARTUP_TIMEOUT_SECS", 3.0)
    monkeypatch.setattr(pi_mcp_broker, "_STARTUP_TIMEOUT_SECS", 6.0)
    await broker.start()
    try:
        assert overlapped == [True], "the healthy child should start while the slow one waits"
        assert broker.initialized_servers == {"healthy"}
        assert [tool["name"] for tool in broker._tool_index] == ["ping"]
        assert broker.server_failures["slow"] == "MCP server initialization timed out"
        assert slow_children[0].process.returncode is not None
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_broker_mounts_oversized_tools_list_like_core(
    broker_dir: Path, tmp_path: Path
) -> None:
    """Regression: kirocrew-core's tools/list exceeds asyncio's 64 KiB default.

    Without ``limit=READ_BUFFER_LIMIT_BYTES`` on the MCP child pipes, readline
    raises mid-frame and the broker reports a false "exited" handshake while
    leaner siblings (cron) still mount.
    """
    # A schema pushes both the MCP and bridge NDJSON frames past 64 KiB.
    padding = "x" * (70 * 1024)
    tools = [
        {
            "name": "spawn_run",
            "description": "spawn",
            "inputSchema": {"type": "object", "description": padding, "properties": {}},
        },
        {
            "name": "wait",
            "description": "short",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]
    core_script = _stdio_mcp_script(tmp_path, tools=tools, name="coreish")
    cron_tools = [
        {
            "name": "cron_list",
            "description": "list",
            "inputSchema": {"type": "object", "properties": {}},
        }
    ]
    cron_script = _stdio_mcp_script(tmp_path, tools=cron_tools, name="cronish")
    sock = broker_socket_path(artifact_dir=str(broker_dir), pid=42, nonce="biglist")
    broker = _broker(
        [
            {
                "name": "kirocrew-core",
                "command": sys.executable,
                "args": [str(core_script)],
            },
            {
                "name": "kirocrew-cron",
                "command": sys.executable,
                "args": [str(cron_script)],
            },
        ],
        socket_path=sock,
        work_dir=str(tmp_path),
    )
    await broker.start()
    try:
        assert "kirocrew-core" in broker._children
        assert "kirocrew-cron" in broker._children
        assert len(broker._tool_index) == 3
        # bridge/list of the oversized index must also clear the IPC read limit.
        reader, writer = await transport.connect(sock, limit=READ_BUFFER_LIMIT_BYTES)
        try:
            writer.write(
                (
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "bridge/list", "params": {}})
                    + "\n"
                ).encode()
            )
            await writer.drain()
            listed = json.loads((await reader.readline()).decode())
            assert listed["id"] == 1
            names = {(t["server"], t["name"]) for t in listed["result"]["tools"]}
            assert ("kirocrew-core", "spawn_run") in names
            assert ("kirocrew-core", "wait") in names
            assert ("kirocrew-cron", "cron_list") in names
            # Frame really was > 64 KiB (the failure mode this pins).
            assert len(json.dumps(listed).encode()) > 65536
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await broker.stop()


@pytest.mark.parametrize("mutation", ["missing", "arguments", "tool", "replay", "denied"])
def test_broker_requires_exact_single_use_host_approval(broker_dir, mutation):
    broker = _broker([], socket_path=str(broker_dir / "b.sock"))
    envelope = {"toolCallId": "call1", "title": "mcp__echo__ping", "input": {"x": 1}}
    if mutation != "missing":
        broker.note_permission("request1", envelope)
        if mutation == "denied":
            broker.reject_permission("request1")
        else:
            broker.approve_permission("request1", broker.stage_permission("request1"))
    if mutation == "replay":
        broker._consume_approval(
            "call1", "mcp__echo__ping", {"x": 1}, generation=broker._grant_generations.get("call1")
        )
    with pytest.raises(PermissionError, match="host-approved"):
        broker._consume_approval(
            "call1",
            "mcp__echo__other" if mutation == "tool" else "mcp__echo__ping",
            {"x": 2} if mutation == "arguments" else {"x": 1},
            generation=broker._grant_generations.get("call1"),
        )


def test_broker_caps_combined_pending_and_approved_calls(broker_dir, monkeypatch):
    from kiro_crew.acp import pi_mcp_broker

    monkeypatch.setattr(pi_mcp_broker, "_APPROVAL_MAX_OUTSTANDING", 2)
    broker = _broker([], socket_path=str(broker_dir / "bounded-approvals.sock"))

    def note(index: int) -> None:
        broker.note_permission(
            f"request-{index}",
            {
                "toolCallId": f"call-{index}",
                "title": "mcp__echo__ping",
                "input": {"index": index},
                "unrelated": "x" * 10000,
            },
        )

    note(1)
    assert "x" * 10000 not in repr(broker._pending_approvals)
    broker.approve_permission("request-1", broker.stage_permission("request-1"))
    note(2)
    note(3)
    assert broker.stage_permission("request-3") is None
    assert len(broker._pending_approvals) + len(broker._approved_calls) == 2
    broker.reject_permission("request-2")
    note(3)
    assert broker.stage_permission("request-3") is not None


def test_broker_bounds_each_retained_approval_field(broker_dir, monkeypatch):
    from kiro_crew.acp import pi_mcp_broker

    monkeypatch.setattr(pi_mcp_broker, "_APPROVAL_REQUEST_ID_MAX_BYTES", 4)
    monkeypatch.setattr(pi_mcp_broker, "_APPROVAL_CALL_ID_MAX_BYTES", 4)
    monkeypatch.setattr(pi_mcp_broker, "_APPROVAL_TITLE_MAX_BYTES", 12)
    monkeypatch.setattr(pi_mcp_broker, "_APPROVAL_ARGS_MAX_BYTES", 64)
    broker = _broker([], socket_path=str(broker_dir / "bounded-fields.sock"))

    for request_id, call_id, title, arguments in [
        ("ééé", "c", "mcp__e__p", {}),
        ("r", "ééé", "mcp__e__p", {}),
        ("r", "c", "mcp__e__tool_is_too_long", {}),
        ("r", "c", "mcp__e__p", {"x": "x" * 100}),
    ]:
        broker.note_permission(
            request_id,
            {"toolCallId": call_id, "title": title, "input": arguments},
        )
        assert broker.stage_permission(request_id) is None
        assert not broker._pending_approvals
        assert not broker._approved_calls

    broker.note_permission("r", {"toolCallId": "c", "title": "mcp__e__p", "input": {}})
    broker.approve_permission("r", broker.stage_permission("r"))
    broker._consume_approval("c", "mcp__e__p", {}, generation=broker._grant_generations.get("c"))


@pytest.mark.asyncio
async def test_broker_start_cancellation_reaps_registered_children(broker_dir, monkeypatch):
    from unittest.mock import AsyncMock

    broker = _broker([], socket_path=str(broker_dir / "b.sock"))
    child = AsyncMock()

    async def interrupted_spawn():
        broker._children["echo"] = child
        raise asyncio.CancelledError()

    monkeypatch.setattr(broker, "_spawn_children", interrupted_spawn)
    with pytest.raises(asyncio.CancelledError):
        await broker.start()
    child.kill.assert_awaited_once()
    assert not broker._children


@pytest.mark.asyncio
async def test_broker_prepares_endpoint_directory_off_loop(broker_dir, monkeypatch):
    loop_thread = threading.get_ident()
    prepare_threads: list[int] = []
    prepare_dir = transport.prepare_dir

    def record_prepare(socket_path):
        prepare_threads.append(threading.get_ident())
        prepare_dir(socket_path)

    monkeypatch.setattr(transport, "prepare_dir", record_prepare)
    broker = _broker([], socket_path=str(broker_dir / "off-loop.sock"))
    try:
        await broker.start()
    finally:
        await broker.stop()
    assert prepare_threads and prepare_threads[0] != loop_thread


def test_mismatched_request_does_not_consume_legitimate_approval(broker_dir):
    broker = _broker([], socket_path=str(broker_dir / "b.sock"))
    broker.note_permission("r", {"toolCallId": "c", "title": "mcp__echo__ping", "input": {"x": 1}})
    broker.approve_permission("r", broker.stage_permission("r"))
    with pytest.raises(PermissionError):
        broker._consume_approval(
            "c", "mcp__echo__ping", {"x": 2}, generation=broker._grant_generations.get("c")
        )
    broker._consume_approval(
        "c", "mcp__echo__ping", {"x": 1}, generation=broker._grant_generations.get("c")
    )


@pytest.mark.asyncio
async def test_stderr_is_redacted_and_bounded(broker_dir, caplog):
    from types import SimpleNamespace

    from kiro_crew.acp import pi_mcp_broker

    broker = _broker([], socket_path=str(broker_dir / "b.sock"))
    secret = "sk-ant-api03-" + "A" * 100
    stream = asyncio.StreamReader()
    stream.feed_data(("Authorization: Bearer " + secret + " " + "x" * 5000 + "\n").encode())
    stream.feed_eof()
    caplog.set_level("DEBUG", logger=pi_mcp_broker.__name__)
    await broker._drain_stderr("fixture", SimpleNamespace(stderr=stream))
    assert secret not in caplog.text
    assert len(caplog.records[-1].message) < 2200


@pytest.mark.asyncio
async def test_overlong_child_stderr_keeps_draining(broker_dir, caplog):
    from types import SimpleNamespace

    from kiro_crew.acp import pi_mcp_broker

    broker = _broker([], socket_path=str(broker_dir / "b.sock"))
    secret = "opaque-stderr-value-q7M4p2"
    stream = asyncio.StreamReader(limit=64)
    stream.feed_data(b"x" * 100 + b"\n" + ("later " + secret + "\n").encode())
    stream.feed_eof()
    caplog.set_level("DEBUG", logger=pi_mcp_broker.__name__)
    await broker._drain_stderr("fixture", SimpleNamespace(stderr=stream), (secret,))
    assert "stderr frame exceeded read limit" in caplog.text
    assert "later [REDACTED: server env]" in caplog.text
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_long_diagnostics_omit_secret_prefixes(broker_dir, caplog) -> None:
    from types import SimpleNamespace

    from kiro_crew.acp import pi_mcp_broker
    from kiro_crew.acp.pi_mcp_broker import _McpChild

    secret = "private-" + "s" * 3000
    stream = asyncio.StreamReader(limit=10_000)
    stream.feed_data(("debug " + secret + "\n").encode())
    stream.feed_eof()
    broker = _broker([], socket_path=str(broker_dir / "b.sock"))
    caplog.set_level("DEBUG", logger=pi_mcp_broker.__name__)
    await broker._drain_stderr("fixture", SimpleNamespace(stderr=stream), (secret,))
    child = _McpChild(name="fixture", process=SimpleNamespace(), metadata_secrets=(secret,))
    child._on_line("non-json " + secret)
    assert "private-" not in caplog.text
    assert "stderr diagnostic exceeded log limit" in caplog.text
    assert "non-JSON line exceeded diagnostic limit" in caplog.text


@pytest.mark.asyncio
async def test_overlong_child_stdout_fails_current_call_and_reads_next_frame() -> None:
    from types import SimpleNamespace

    from kiro_crew.acp.pi_mcp_broker import _McpChild, _Pending

    stream = asyncio.StreamReader(limit=64)
    child = _McpChild(name="fixture", process=SimpleNamespace(stdout=stream))
    loop = asyncio.get_running_loop()
    first = loop.create_future()
    child.pending[1] = _Pending(future=first)
    reading = asyncio.create_task(child._read_stdout())
    try:
        stream.feed_data(b"x" * 100)
        with pytest.raises(RuntimeError, match="stdout frame exceeded read limit"):
            await asyncio.wait_for(first, timeout=1)
        assert not reading.done()
        assert not child.pending
        assert child.stdout_frame_discarding
        with pytest.raises(RuntimeError, match="stdout frame exceeds read limit"):
            await child.request("tools/call", timeout=0.01)

        forged = loop.create_future()
        child.pending[99] = _Pending(future=forged)
        stream.feed_data(b'{"jsonrpc":"2.0","id":99,"result":"forged"}\n')
        second = loop.create_future()
        child.pending[2] = _Pending(future=second)
        stream.feed_data(b'{"jsonrpc":"2.0","id":2,"result":"ok"}\n')
        assert (await asyncio.wait_for(second, timeout=1))["result"] == "ok"
        assert not forged.done()
        child.pending.pop(99)
        forged.cancel()
        stream.feed_eof()
        await asyncio.wait_for(reading, timeout=1)
    finally:
        reading.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reading


@pytest.mark.parametrize("replacement", ["terminal", "same_request", "new_request"])
def test_late_delivery_cannot_resurrect_superseded_call(broker_dir, replacement):
    broker = _broker([], socket_path=str(broker_dir / "b.sock"))
    envelope = {"toolCallId": "c", "title": "mcp__echo__ping", "input": {"x": 1}}
    broker.note_permission("old", envelope)
    old_generation = broker.stage_permission("old")
    if replacement == "terminal":
        broker.finish_call("c")
    else:
        new_request = "old" if replacement == "same_request" else "new"
        broker.note_permission(new_request, envelope)
        broker.reject_permission(new_request)
    broker.approve_permission("old", old_generation)
    assert not broker._approved_calls
    assert not broker._pending_approvals
    assert not broker._delivering


@pytest.mark.asyncio
async def test_delivery_timeout_invalidates_late_allow(broker_dir, monkeypatch):
    from kiro_crew.acp import pi_mcp_broker

    broker = _broker([], socket_path=str(broker_dir / "b.sock"))
    broker.note_permission("r", {"toolCallId": "c", "title": "mcp__echo__ping", "input": {}})
    generation = broker.stage_permission("r")
    monkeypatch.setattr(pi_mcp_broker, "_DELIVERY_TIMEOUT_SECS", 0.001)
    with pytest.raises(asyncio.TimeoutError):
        await broker.wait_for_delivery("c")
    broker.approve_permission("r", generation)
    assert not broker._approved_calls
    assert not broker._approval_generations


@pytest.mark.asyncio
async def test_overlapping_call_id_cannot_consume_new_generation(broker_dir):
    broker = _broker([], socket_path=str(broker_dir / "b.sock"))
    envelope = {"toolCallId": "c", "title": "mcp__echo__ping", "input": {}}
    broker.note_permission("old", envelope)
    old_generation = broker.stage_permission("old")
    waiter = asyncio.create_task(broker.wait_for_delivery("c"))
    await asyncio.sleep(0)
    broker.note_permission("new", envelope)
    new_generation = broker.stage_permission("new")
    broker.approve_permission("old", old_generation)
    broker.approve_permission("new", new_generation)
    observed = await waiter
    with pytest.raises(PermissionError):
        broker._consume_approval("c", "mcp__echo__ping", {}, generation=observed)
    broker._consume_approval("c", "mcp__echo__ping", {}, generation=new_generation)


@pytest.mark.asyncio
async def test_stop_retains_failed_children_for_retry_and_closes_endpoint(broker_dir):
    from unittest.mock import AsyncMock

    socket_path = broker_dir / "b.sock"
    socket_path.touch()
    broker = _broker([], socket_path=str(socket_path))
    first, second, retiring = AsyncMock(), AsyncMock(), AsyncMock()
    first.kill.side_effect = [OSError("cleanup failed"), None]
    retiring.kill.side_effect = [OSError("retirement failed"), None]
    broker._children = {"first": first, "second": second}
    broker._retiring_children = {"retiring": retiring}
    with pytest.raises(OSError, match="cleanup failed"):
        await broker.stop()
    first.kill.assert_awaited_once()
    second.kill.assert_awaited_once()
    retiring.kill.assert_awaited_once()
    assert broker._children == {"first": first}
    assert broker._retiring_children == {"retiring": retiring}
    assert not broker._approved_calls
    if not __import__("kiro_crew.platform_compat", fromlist=["IS_WINDOWS"]).IS_WINDOWS:
        assert not socket_path.exists()
    with pytest.raises(RuntimeError, match="awaiting teardown"):
        await broker.start()
    await broker.stop()
    assert not broker._children
    assert not broker._retiring_children
    second.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_child_teardown_retries_while_broker_is_retained(broker_dir, monkeypatch):
    from unittest.mock import AsyncMock

    from kiro_crew.acp import pi_mcp_broker

    monkeypatch.setattr(pi_mcp_broker, "_TEARDOWN_RETRY_DELAY_SECS", 0.01)
    broker = _broker([], socket_path=str(broker_dir / "retry.sock"))
    child = AsyncMock()
    child.kill.side_effect = [OSError("transient teardown failure"), None]
    broker._children["child"] = child

    with pytest.raises(OSError, match="transient teardown failure"):
        await broker.stop()
    retry = broker._teardown_retry_task
    assert retry in pi_mcp_broker._FAILED_TEARDOWN_TASKS
    assert broker._children["child"] is child

    await asyncio.wait_for(retry, timeout=2)
    assert not broker._children
    assert broker._teardown_retry_task is None
    assert retry not in pi_mcp_broker._FAILED_TEARDOWN_TASKS
    assert child.kill.await_count == 2


@pytest.mark.asyncio
async def test_start_preserves_original_error_when_cleanup_fails(broker_dir, monkeypatch):
    from unittest.mock import AsyncMock

    broker = _broker([], socket_path=str(broker_dir / "b.sock"))
    monkeypatch.setattr(
        broker, "_spawn_children", AsyncMock(side_effect=ValueError("startup failed"))
    )
    monkeypatch.setattr(broker, "stop", AsyncMock(side_effect=OSError("cleanup failed")))
    with pytest.raises(ValueError, match="startup failed"):
        await broker.start()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.TimeoutError, asyncio.CancelledError])
@pytest.mark.parametrize("replacement", [None, "pending", "delivering", "approved"])
async def test_failed_delivery_revokes_published_generation_only(
    broker_dir, monkeypatch, failure, replacement
):
    broker = _broker([], socket_path=str(broker_dir / "b.sock"))
    envelope = {"toolCallId": "c", "title": "mcp__echo__ping", "input": {}}
    broker.note_permission("old", envelope)
    old_generation = broker.stage_permission("old")
    new_generation = None

    async def publish_then_fail(waiter, *, timeout):
        nonlocal new_generation
        waiter.close()
        broker.approve_permission("old", old_generation)
        assert broker._grant_generations["c"] is old_generation
        if replacement is not None:
            broker.note_permission("new", envelope)
            new_generation = broker._approval_generations["new"]
            if replacement in ("delivering", "approved"):
                broker.stage_permission("new")
            if replacement == "approved":
                broker.approve_permission("new", new_generation)
        raise failure()

    monkeypatch.setattr(asyncio, "wait_for", publish_then_fail)
    with pytest.raises(failure):
        await broker.wait_for_delivery("c")
    assert old_generation not in broker._approval_generations.values()
    assert old_generation not in broker._delivery_generations.values()
    assert old_generation not in broker._grant_generations.values()
    if replacement is None:
        assert not broker._approved_calls
        assert not broker._pending_approvals
        assert not broker._delivering
    else:
        if replacement != "approved":
            assert broker._approval_generations["new"] is new_generation
            broker.approve_permission("new", new_generation)
        broker._consume_approval("c", "mcp__echo__ping", {}, generation=new_generation)


@pytest.mark.asyncio
@pytest.mark.parametrize("large_field", ["description", "inputSchema"])
async def test_large_metadata_does_not_break_bridge_list(broker_dir, tmp_path, large_field):
    """A valid 9 MiB tools/list must not overflow Pi's 8 MiB receiver."""
    from kiro_crew.acp.pi_mcp_broker import _TOOL_DESCRIPTION_MAX_CHARS

    padding = "x" * (9 * 1024 * 1024)
    tool = {"name": "large", "description": "large", "inputSchema": {"type": "object"}}
    tool[large_field] = padding if large_field == "description" else {"description": padding}
    large = _stdio_mcp_script(tmp_path, tools=[tool], name="large")
    healthy = _stdio_mcp_script(tmp_path, tools=[{"name": "ping"}], name="healthy")
    sock = broker_socket_path(artifact_dir=str(broker_dir), pid=42, nonce="metadata")
    broker = _broker(
        [
            {"name": name, "command": sys.executable, "args": [str(script)]}
            for name, script in [("large", large), ("healthy", healthy)]
        ],
        socket_path=sock,
        work_dir=str(tmp_path),
    )
    await broker.start()
    try:
        reader, writer = await transport.connect(sock, limit=8 * 1024 * 1024)
        try:
            writer.write(b'{"jsonrpc":"2.0","id":1,"method":"bridge/list"}\n')
            await writer.drain()
            frame = await asyncio.wait_for(reader.readline(), timeout=5)
            assert len(frame) < 8 * 1024 * 1024
            tools = json.loads(frame)["result"]["tools"]
            assert tools[-1]["name"] == "ping"
            if large_field == "description":
                assert broker.initialized_servers == {"large", "healthy"}
                assert tools[0]["description"] == padding[:_TOOL_DESCRIPTION_MAX_CHARS]
                assert tools[0]["inputSchema"] == {"type": "object"}
            else:
                assert broker.initialized_servers == {"healthy"}
                assert "size limit" in broker.server_failures["large"]
                assert len(tools) == 1
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await broker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "tools"),
    [
        ("tool count", [{"name": "one"}, {"name": "two"}, {"name": "three"}]),
        ("tool name", [{"name": "tool_name_exceeds_limit"}]),
    ],
)
async def test_tool_metadata_overflow_records_failure_and_keeps_siblings(
    broker_dir, tmp_path, monkeypatch, case, tools
):
    from kiro_crew.acp import pi_mcp_broker

    monkeypatch.setattr(pi_mcp_broker, "_TOOL_LIST_MAX_COUNT", 2)
    monkeypatch.setattr(pi_mcp_broker, "_TOOL_NAME_MAX_BYTES", 8)
    oversized = _stdio_mcp_script(tmp_path, tools=tools, name="oversized")
    healthy = _stdio_mcp_script(tmp_path, tools=[{"name": "ping"}], name="healthy")
    broker = _broker(
        [
            {"name": "oversized", "command": sys.executable, "args": [str(oversized)]},
            {"name": "healthy", "command": sys.executable, "args": [str(healthy)]},
        ],
        socket_path=broker_socket_path(
            artifact_dir=str(broker_dir), pid=42, nonce=case.replace(" ", "-")
        ),
        work_dir=str(tmp_path),
    )
    await broker.start()
    try:
        assert broker.initialized_servers == {"healthy"}
        assert [(tool["server"], tool["name"]) for tool in broker._tool_index] == [
            ("healthy", "ping")
        ]
        assert broker.server_failures["oversized"] == (
            f"MCP tool metadata size limit exceeded: {case}"
        )
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_metadata_budget_covers_all_servers_without_losing_admitted_tools(
    broker_dir, tmp_path, monkeypatch
):
    from kiro_crew.acp import pi_mcp_broker

    monkeypatch.setattr(pi_mcp_broker, "_TOOL_INDEX_MAX_BYTES", 2000)
    schema = {"type": "object", "description": "x" * 1000}
    specs = []
    for name, tools in [
        ("first", [{"name": "one", "inputSchema": schema}]),
        ("second", [{"name": "two", "inputSchema": schema}]),
        ("last", [{"name": "three"}]),
    ]:
        script = _stdio_mcp_script(tmp_path, tools=tools, name=name)
        specs.append({"name": name, "command": sys.executable, "args": [str(script)]})
    broker = _broker(
        specs,
        socket_path=broker_socket_path(artifact_dir=str(broker_dir), pid=42, nonce="aggregate"),
        work_dir=str(tmp_path),
    )
    await broker.start()
    try:
        assert broker.initialized_servers == {"first", "last"}
        assert len(broker._tool_index) == 2
        assert [(t["server"], t["name"]) for t in broker._tool_index] == [
            ("first", "one"),
            ("last", "three"),
        ]
        assert broker._tool_index[0]["inputSchema"] == schema
        assert "size limit" in broker.server_failures["second"]
    finally:
        await broker.stop()


@pytest.mark.asyncio
async def test_disabled_tool_preserves_third_party_siblings(broker_dir, tmp_path):
    script = _stdio_mcp_script(
        tmp_path, tools=[{"name": "read"}, {"name": "write"}], name="third_party"
    )
    broker = _broker(
        [
            {
                "name": "third-party",
                "command": sys.executable,
                "args": [str(script)],
                "disabledTools": ["write"],
            }
        ],
        socket_path=broker_socket_path(artifact_dir=str(broker_dir), pid=99, nonce="deny"),
        work_dir=str(tmp_path),
    )
    try:
        await broker.start()
        assert broker.initialized_servers == frozenset({"third-party"})
        assert [(tool["server"], tool["name"]) for tool in broker._tool_index] == [
            ("third-party", "read")
        ]
    finally:
        await broker.stop()
