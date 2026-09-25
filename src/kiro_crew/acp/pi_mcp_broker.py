"""Host-side MCP broker for Pi sessions (clears GPT F1).

Pi's sandbox must never see secret-bearing server configuration. The sealed
bridge extension therefore does **not** spawn Crew MCP children itself and does
**not** read a servers JSON file inside the sandbox. Instead this module — which
runs in the unsandboxed ACP client / gateway process — holds the real
``{name, command, args, env}`` specs, spawns the stdio MCP children, and serves
a thin NDJSON IPC protocol over an owner-only local endpoint (unix socket /
named pipe via :mod:`kiro_crew.mcp_gateway.transport`).

The Pi bridge connects with only the endpoint address
(``KIROCREW_PI_MCP_BROKER_SOCK``); that address carries no credentials. Peer
admission reuses :func:`kiro_crew.mcp_gateway.socketsec.check_peer_is_self`.
Every call additionally consumes one host approval bound to its call id, tool
name and exact arguments; knowing the endpoint is not permission to run tools.

Soft-fail: if the broker cannot bind or no servers are supplied, callers leave
the session chat-only and must not advertise Crew tools as mounted.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from kiro_crew import platform_compat
from kiro_crew.acp.mcp_session_report import sanitize_sink_text
from kiro_crew.mcp_gateway import socketsec, transport
from kiro_crew.mcp_gateway.pool import READ_BUFFER_LIMIT_BYTES
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    create_subprocess_limited,
    wrap_argv,
    wrap_argv_async,
)

logger = logging.getLogger(__name__)

#: Env var naming the broker endpoint address the Pi bridge connects to.
#: On POSIX this is the unix-socket path; on Windows it is the named-pipe
#: address :func:`transport.resolve_address` derives. Never a secret.
ENV_BROKER_SOCK = "KIROCREW_PI_MCP_BROKER_SOCK"

# IPC + MCP-child stdout share the gateway's read ceiling. Asyncio's stdlib
# default (64 KiB) truncates a real ``kirocrew-core`` tools/list (~100 KiB+ of
# schemas) mid-frame and surfaces as a false "exited" handshake failure while
# leaner siblings like ``kirocrew-cron`` still mount.
_DEFAULT_READ_LIMIT = READ_BUFFER_LIMIT_BYTES
_HANDSHAKE_TIMEOUT_SECS = 30.0
_CALL_TIMEOUT_SECS = 120.0
_STARTUP_TIMEOUT_SECS = 60.0
_DELIVERY_TIMEOUT_SECS = 5.0
# Pi's bridge receive buffer is 8 MiB. Leave envelope headroom and reject a
# server whose schemas cannot fit, without losing already admitted siblings.
_TOOL_INDEX_MAX_BYTES = 7 * 1024 * 1024
_TOOL_DESCRIPTION_MAX_CHARS = 4096

# Env keys a host-spawned MCP child may inherit from the broker process —
# never the full process.env (sibling secrets / gateway vars).
_CHILD_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "COLORTERM",
    "NODE_PATH",
    "NODE_OPTIONS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "PYTHONPATH",
    "VIRTUAL_ENV",
)


def _normalize_server_env(server_env: Any) -> dict[str, str]:
    """Allowlisted process env + this server's env overlay."""
    out: dict[str, str] = {}
    for key in _CHILD_ENV_ALLOWLIST:
        val = os.environ.get(key)
        if val:
            out[key] = val
    if not server_env:
        return out
    if isinstance(server_env, list):
        for row in server_env:
            if isinstance(row, dict) and isinstance(row.get("name"), str):
                out[row["name"]] = "" if row.get("value") is None else str(row["value"])
        return out
    if isinstance(server_env, dict):
        for key, val in server_env.items():
            if val is not None:
                out[str(key)] = str(val)
    return out


@dataclass
class _Pending:
    future: asyncio.Future[dict[str, Any]]


@dataclass
class _McpChild:
    name: str
    process: asyncio.subprocess.Process
    reader_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    sandbox_cleanup: str | None = None
    buffer: str = ""
    next_id: int = 1
    pending: dict[int, _Pending] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)
    disabled_tools: set[str] = field(default_factory=set)

    async def request(self, method: str, params: Any = None, *, timeout: float) -> Any:
        req_id = self.next_id
        self.next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending[req_id] = _Pending(future=fut)
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        await self.process.stdin.drain()
        try:
            msg = await asyncio.wait_for(fut, timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self.pending.pop(req_id, None)
            raise
        if msg.get("error"):
            err = msg["error"]
            if isinstance(err, dict):
                raise RuntimeError(err.get("message") or f"MCP error {err.get('code')}")
            raise RuntimeError(str(err))
        return msg.get("result")

    def notify(self, method: str, params: Any = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if self.process.stdin is None:
            return
        try:
            self.process.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            logger.debug("pi-mcp-broker: %s notify failed: %s", self.name, exc)

    def _on_line(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            logger.debug(
                "pi-mcp-broker: %s non-JSON line: %s",
                sanitize_sink_text(self.name, 128),
                sanitize_sink_text(line, 200),
            )
            return
        if not isinstance(msg, dict) or msg.get("id") is None:
            return
        try:
            req_id = int(msg["id"])
        except (TypeError, ValueError):
            return
        pending = self.pending.pop(req_id, None)
        if pending and not pending.future.done():
            pending.future.set_result(msg)

    async def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        fail: Exception | None = None
        try:
            while True:
                chunk = await self.process.stdout.readline()
                if not chunk:
                    break
                line = chunk.decode("utf-8", errors="replace").strip()
                if line:
                    self._on_line(line)
        except ValueError as exc:
            # asyncio.StreamReader.readline raises ValueError when a frame
            # exceeds ``limit`` ("Separator is found, but chunk is longer…").
            fail = RuntimeError(
                f"{self.name} stdout frame exceeded read limit "
                f"({_DEFAULT_READ_LIMIT} bytes): {exc}"
            )
            logger.warning("pi-mcp-broker: %s", fail)
        finally:
            err = fail or RuntimeError(f"{self.name} exited")
            for pending in list(self.pending.values()):
                if not pending.future.done():
                    pending.future.set_exception(err)
            self.pending.clear()

    async def handshake(self) -> None:
        await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "kiro-crew-pi-mcp-broker", "version": "0.1.0"},
            },
            timeout=_HANDSHAKE_TIMEOUT_SECS,
        )
        self.notify("notifications/initialized")
        listed = await self.request("tools/list", {}, timeout=_HANDSHAKE_TIMEOUT_SECS)
        tools = listed.get("tools") if isinstance(listed, dict) else None
        self.tools = []
        for tool in tools if isinstance(tools, list) else []:
            if not isinstance(tool, dict):
                continue
            description = tool.get("description")
            self.tools.append(
                {
                    "name": tool.get("name"),
                    "description": (
                        description[:_TOOL_DESCRIPTION_MAX_CHARS]
                        if isinstance(description, str)
                        else ""
                    ),
                    "inputSchema": tool.get("inputSchema"),
                }
            )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self.request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=_CALL_TIMEOUT_SECS,
        )

    async def kill(self) -> None:
        if self.stderr_task is not None:
            self.stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.stderr_task
            self.stderr_task = None
        if self.reader_task is not None:
            self.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.reader_task
            self.reader_task = None
        if platform_compat.IS_WINDOWS:
            await platform_compat.terminate_windows_asyncio_tree(self.process)
        elif self.process.returncode is None:
            with contextlib.suppress(Exception):
                await platform_compat.kill_process_tree_async(self.process.pid)
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                with contextlib.suppress(Exception):
                    await platform_compat.kill_process_tree_async(
                        self.process.pid, platform_compat.SIGKILL
                    )
                with contextlib.suppress(Exception):
                    await self.process.wait()

        if self.sandbox_cleanup:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(os.unlink, self.sandbox_cleanup)
            self.sandbox_cleanup = None


def broker_socket_path(*, artifact_dir: str, pid: int, nonce: str) -> str:
    """Filesystem path (POSIX) / lock-file anchor (Windows) for one session broker."""
    if not nonce or not str(nonce).strip():
        raise ValueError("pi MCP broker requires a non-empty session nonce")
    # Keep the leaf short: macOS AF_UNIX sockaddr is ~104 bytes; a deep
    # config_dir plus a long nonce would otherwise fail bind at session start.
    return os.path.join(artifact_dir, f"pmb_{pid}_{nonce[:12]}.sock")


class PiMcpBroker:
    """Host-side broker: secrets stay here; Pi talks over :data:`ENV_BROKER_SOCK`."""

    def __init__(
        self,
        servers: list[dict[str, Any]],
        *,
        socket_path: str,
        sandbox_mode: str = "standard",
        hidden_dirs: tuple[str, ...] = (),
        host_control_plane_servers: frozenset[str] = frozenset(),
    ) -> None:
        self._sandbox_mode = sandbox_mode
        self._hidden_dirs = hidden_dirs
        self._host_control_plane_servers = host_control_plane_servers
        self._specs = [s for s in servers if isinstance(s, dict)]
        self._socket_path = socket_path
        self._endpoint = transport.resolve_address(socket_path)
        self._server: Optional[transport.TransportServer] = None
        self._children: dict[str, _McpChild] = {}
        self._tool_index: list[dict[str, Any]] = []
        self._started = False
        self._clients: set[asyncio.Task[None]] = set()
        self.server_failures: dict[str, str] = {}
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        self._approved_calls: dict[str, tuple[str, str]] = {}
        self._delivering: dict[str, asyncio.Event] = {}
        self._approval_generations: dict[str, object] = {}
        self._delivery_generations: dict[str, object] = {}
        self._grant_generations: dict[str, object] = {}

    def _child_hidden_dirs(self, name: str, raw_name: str) -> tuple[str, ...]:
        """Use Pi's credential mask except for a verified host control-plane child."""
        return (
            ()
            if raw_name == name and name in self._host_control_plane_servers
            else self._hidden_dirs
        )

    def note_permission(self, request_id: str, envelope: dict[str, Any]) -> None:
        """Remember the exact call the existing host permission path evaluates."""
        self.reject_permission(request_id)
        call_id = envelope.get("toolCallId")
        if isinstance(call_id, str):
            self.finish_call(call_id)
        if (
            envelope.get("truncated")
            or not isinstance(envelope.get("input"), dict)
            or not str(envelope.get("title", "")).startswith("mcp__")
        ):
            return
        self._pending_approvals[request_id] = envelope
        self._approval_generations[request_id] = object()

    def stage_permission(self, request_id: str) -> object | None:
        envelope = self._pending_approvals.get(request_id)
        if envelope is not None:
            self._delivering[envelope["toolCallId"]] = asyncio.Event()
            self._delivery_generations[envelope["toolCallId"]] = self._approval_generations[
                request_id
            ]
            return self._approval_generations[request_id]
        return None

    def approve_permission(self, request_id: str, generation: object | None) -> None:
        if generation is None or self._approval_generations.get(request_id) is not generation:
            return
        self._approval_generations.pop(request_id, None)
        envelope = self._pending_approvals.pop(request_id, None)
        if envelope is not None:
            self._grant_generations[envelope["toolCallId"]] = generation
            self._delivery_generations.pop(envelope["toolCallId"], None)
            self._approved_calls[envelope["toolCallId"]] = (
                envelope["title"],
                json.dumps(envelope["input"], sort_keys=True, separators=(",", ":")),
            )
            event = self._delivering.pop(envelope["toolCallId"], None)
            if event is not None:
                event.set()

    def reject_permission(self, request_id: str, generation: object | None = None) -> None:
        if generation is not None and self._approval_generations.get(request_id) is not generation:
            return
        self._approval_generations.pop(request_id, None)
        envelope = self._pending_approvals.pop(request_id, None)
        if envelope is not None:
            self._approved_calls.pop(envelope["toolCallId"], None)
            self._grant_generations.pop(envelope["toolCallId"], None)
            self._delivery_generations.pop(envelope["toolCallId"], None)
            event = self._delivering.pop(envelope["toolCallId"], None)
            if event is not None:
                event.set()

    async def wait_for_delivery(self, call_id: Any) -> object | None:
        if not isinstance(call_id, str):
            return None
        event = self._delivering.get(call_id)
        if event is None:
            return self._grant_generations.get(call_id)
        generation = self._delivery_generations.get(call_id)
        try:
            await asyncio.wait_for(event.wait(), timeout=_DELIVERY_TIMEOUT_SECS)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._revoke_generation(call_id, generation)
            raise
        return generation

    def _revoke_generation(self, call_id: str, generation: object | None) -> None:
        """Retire a failed delivery even if its grant was published during the wait."""
        if generation is None:
            return
        for request_id, envelope in list(self._pending_approvals.items()):
            if (
                envelope["toolCallId"] == call_id
                and self._approval_generations.get(request_id) is generation
            ):
                self.reject_permission(request_id, generation)
        if self._grant_generations.get(call_id) is generation:
            self._grant_generations.pop(call_id, None)
            self._approved_calls.pop(call_id, None)
        if self._delivery_generations.get(call_id) is generation:
            self._delivery_generations.pop(call_id, None)
            event = self._delivering.pop(call_id, None)
            if event is not None:
                event.set()

    def finish_call(self, call_id: str) -> None:
        self._approved_calls.pop(call_id, None)
        self._grant_generations.pop(call_id, None)
        self._delivery_generations.pop(call_id, None)
        for request_id, envelope in list(self._pending_approvals.items()):
            if envelope["toolCallId"] == call_id:
                self.reject_permission(request_id)
        event = self._delivering.pop(call_id, None)
        if event is not None:
            event.set()

    def _consume_approval(
        self, call_id: Any, tool: str, arguments: dict[str, Any], *, generation: object | None
    ) -> None:
        expected = self._approved_calls.get(call_id) if isinstance(call_id, str) else None
        actual = (tool, json.dumps(arguments, sort_keys=True, separators=(",", ":")))
        if (
            expected is None
            or expected != actual
            or (self._grant_generations.get(call_id) is not generation)
        ):
            raise PermissionError("Pi MCP call has no matching host-approved permission")
        self._approved_calls.pop(call_id, None)
        self._grant_generations.pop(call_id, None)

    @property
    def endpoint(self) -> str:
        """Address to place in :data:`ENV_BROKER_SOCK` for the Pi child."""
        return self._endpoint

    @property
    def socket_path(self) -> str:
        return self._socket_path

    @property
    def initialized_servers(self) -> frozenset[str]:
        return frozenset(self._children)

    @property
    def tool_count(self) -> int:
        return len(self._tool_index)

    async def start(self) -> None:
        """Spawn MCP children and bind the IPC endpoint."""
        if self._started:
            return
        transport.prepare_dir(self._socket_path)
        # Drop a stale socket file from a previous soft-fail race (POSIX only).
        if not platform_compat.IS_WINDOWS and os.path.exists(self._socket_path):
            with contextlib.suppress(Exception):
                os.unlink(self._socket_path)
        try:
            await asyncio.wait_for(self._spawn_children(), timeout=_STARTUP_TIMEOUT_SECS)
            self._server = await transport.serve(
                self._socket_path, self._accept_client, limit=_DEFAULT_READ_LIMIT
            )
            if not platform_compat.IS_WINDOWS:
                with contextlib.suppress(Exception):
                    platform_compat.restrict_to_owner(self._socket_path)
        except BaseException:
            try:
                await self.stop()
            except BaseException:
                logger.warning("pi-mcp-broker: cleanup failed after startup failure")
            raise
        self._started = True
        logger.info(
            "pi-mcp-broker: listening on %s with %d tool(s) from %d server(s)",
            self._endpoint,
            len(self._tool_index),
            len(self._children),
        )

    async def stop(self) -> None:
        """Tear down the endpoint and every MCP child."""
        server = self._server
        self._server = None
        self._started = False
        if server is not None:
            with contextlib.suppress(Exception):
                server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        for task in list(self._clients):
            task.cancel()
        if self._clients:
            await asyncio.gather(*self._clients, return_exceptions=True)
        self._clients.clear()
        cleanup_errors: list[BaseException] = []
        try:
            for child in list(self._children.values()):
                try:
                    await child.kill()
                except BaseException as exc:
                    cleanup_errors.append(exc)
        finally:
            self._children.clear()
            self._tool_index.clear()
            for event in self._delivering.values():
                event.set()
            self._delivering.clear()
            self._delivery_generations.clear()
            self._grant_generations.clear()
            self._approval_generations.clear()
            self._pending_approvals.clear()
            self._approved_calls.clear()
            if not platform_compat.IS_WINDOWS:
                with contextlib.suppress(Exception):
                    os.unlink(self._socket_path)
        if cleanup_errors:
            raise cleanup_errors[0]

    async def _spawn_children(self) -> None:
        for spec in self._specs:
            raw_name = str(spec.get("name") or "")
            name = raw_name.strip()
            command = spec.get("command")
            if raw_name != name:
                logger.warning("pi-mcp-broker: refusing server with padded name: %r", raw_name)
                continue
            if not name or not command or not isinstance(command, str):
                continue
            if "__" in name:
                logger.warning("pi-mcp-broker: refusing server whose name contains '__': %r", name)
                continue
            if spec.get("disabled"):
                continue
            if spec.get("type", "stdio") not in ("stdio", None, ""):
                continue
            args = [str(a) for a in (spec.get("args") or [])]
            env = _normalize_server_env(spec.get("env"))
            disabled = {t for t in (spec.get("disabledTools") or []) if isinstance(t, str) and t}
            argv, cleanup = await wrap_argv_async(
                [command, *args],
                mode=self._sandbox_mode,
                # The Pi adapter needs the credential mask. The host's own
                # verified control-plane child needs its protected binding.
                extra_hidden_dirs=self._child_hidden_dirs(name, raw_name),
                _prepare=wrap_argv,
            )
            try:
                argv = await asyncio.to_thread(cgroup_scope_argv, argv)
                process = await platform_compat.create_windows_cleanup_owned_process(
                    functools.partial(
                        create_subprocess_limited,
                        *argv,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                        limit=_DEFAULT_READ_LIMIT,
                        start_new_session=platform_compat.IS_POSIX,
                        creationflags=(
                            platform_compat.CREATE_NEW_PROCESS_GROUP
                            | platform_compat._SUBPROCESS_NO_WINDOW
                            | platform_compat.CREATE_SUSPENDED
                        ),
                    )
                )
            except BaseException:
                if cleanup:
                    with contextlib.suppress(OSError):
                        await asyncio.to_thread(os.unlink, cleanup)
                raise
            child = _McpChild(
                name=name, process=process, disabled_tools=disabled, sandbox_cleanup=cleanup
            )
            child.reader_task = asyncio.create_task(
                child._read_stdout(), name=f"pi-mcp-broker-{name}-stdout"
            )
            # Drain stderr so a chatty server cannot fill the pipe.
            child.stderr_task = asyncio.create_task(
                self._drain_stderr(name, process), name=f"pi-mcp-broker-{name}-stderr"
            )
            self._children[name] = child
            try:
                await child.handshake()
            except Exception as exc:
                self.server_failures[name] = "MCP server initialization failed"
                logger.warning(
                    "pi-mcp-broker: handshake failed for %s: %s",
                    sanitize_sink_text(name, 128),
                    sanitize_sink_text(str(exc), 2000),
                )
                await child.kill()
                self._children.pop(name, None)
                continue
            self._children[name] = child
            server_tools: list[dict[str, Any]] = []
            for tool in child.tools:
                tool_name = tool.get("name")
                if not isinstance(tool_name, str) or not tool_name:
                    continue
                if tool_name in disabled or "__" in tool_name:
                    continue
                server_tools.append(
                    {
                        "server": name,
                        "name": tool_name,
                        "description": tool.get("description") or "",
                        "inputSchema": tool.get("inputSchema"),
                    }
                )
            candidate = self._tool_index + server_tools
            if len(json.dumps(candidate, separators=(",", ":")).encode()) > _TOOL_INDEX_MAX_BYTES:
                self.server_failures[name] = "MCP tool metadata exceeds bridge size limit"
                await child.kill()
                self._children.pop(name, None)
                continue
            self._tool_index = candidate
            logger.info("pi-mcp-broker: %s listed %d tool(s)", name, len(child.tools))

    async def _drain_stderr(self, name: str, process: asyncio.subprocess.Process) -> None:
        if process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.debug(
                        "pi-mcp-broker: %s stderr: %s",
                        sanitize_sink_text(name, 128),
                        sanitize_sink_text(text, 2000),
                    )
        except Exception:
            pass

    def _accept_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._on_client(reader, writer))
        self._clients.add(task)
        task.add_done_callback(self._clients.discard)

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        verdict = socketsec.check_peer_is_self(writer)
        if verdict is not socketsec.PeerCredResult.MATCH:
            logger.warning("pi-mcp-broker: refusing peer (principal %s)", verdict.value)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    await self._write(
                        writer,
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": "parse error"},
                        },
                    )
                    continue
                if not isinstance(msg, dict):
                    continue
                await self._dispatch(writer, msg)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _dispatch(self, writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
        req_id = msg.get("id")
        method = msg.get("method")
        raw_params = msg.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        try:
            if method == "bridge/list":
                result: Any = {"tools": list(self._tool_index)}
            elif method == "bridge/call":
                server = str(params.get("server") or "")
                tool = str(params.get("tool") or "")
                arguments = params.get("arguments")
                if not isinstance(arguments, dict):
                    arguments = {}
                generation = await self.wait_for_delivery(params.get("toolCallId"))
                self._consume_approval(
                    params.get("toolCallId"),
                    f"mcp__{server}__{tool}",
                    arguments,
                    generation=generation,
                )
                child = self._children.get(server)
                if child is None:
                    raise RuntimeError(f"unknown server {server!r}")
                if tool in child.disabled_tools:
                    raise RuntimeError(f"tool {tool!r} is disabled on {server}")
                result = await child.call_tool(tool, arguments)
            else:
                await self._write(
                    writer,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"method not found: {method}"},
                    },
                )
                return
            await self._write(writer, {"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception as exc:
            await self._write(
                writer,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": str(exc)},
                },
            )

    async def _write(self, writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
        writer.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        await writer.drain()
