"""Runner-mediated Windows MCP connections, without a sandbox-visible endpoint.

The outside launcher owns this helper. It hands two anonymous control handles
to the attested MXC root. A stub duplicates those from that root and requests
one already-registered gateway connection. The host checks a signed declaration
and a live descendant identity before duplicating the connection into the stub.
Only the host opens the owner-only gateway pipe; no pipe ACL is widened.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import importlib
import json
import os
import sys
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.mcp_gateway import native_scope, socketsec, transport

_MAX_FRAME = 32_768


def _kernel() -> Any:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel.DuplicateHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.CreatePipe.restype = wintypes.BOOL
    return kernel


@contextlib.contextmanager
def _process(pid: int):
    kernel = _kernel()
    handle = kernel.OpenProcess(0x00100000 | 0x1000 | 0x40, False, pid)
    if not handle:
        raise PermissionError("Native MCP process identity is unavailable")
    try:
        yield int(handle)
    finally:
        kernel.CloseHandle(handle)


def _identity(handle: int) -> tuple[int, int]:
    from kiro_crew.platform_compat_identity import _windows_process_handle_identity

    result = _windows_process_handle_identity(handle, await_exit_time=False)
    if result is None:
        raise PermissionError("Native MCP process generation is unavailable")
    api: Any = importlib.import_module("_winapi")

    if api.WaitForSingleObject(handle, 0) != api.WAIT_TIMEOUT:
        raise PermissionError("Native MCP process is no longer live")
    return result[0], result[1]


def _duplicate(source: int, handle: int, target: int, *, access: int = 0) -> int:
    kernel = _kernel()
    result = wintypes.HANDLE()
    if not kernel.DuplicateHandle(
        source, handle, target, ctypes.byref(result), access, False, 0 if access else 2
    ):
        raise PermissionError("Native MCP handle transfer was refused")
    if result.value is None:
        raise PermissionError("Native MCP transfer returned no handle")
    return int(result.value)


def _pipe() -> tuple[int, int]:
    kernel = _kernel()
    reader, writer = wintypes.HANDLE(), wintypes.HANDLE()
    with transport._owner_only_security_attributes() as attrs:
        if not kernel.CreatePipe(
            ctypes.byref(reader), ctypes.byref(writer), ctypes.byref(attrs), 65536
        ):
            raise OSError("Native MCP control pipe could not be created")
    if reader.value is None or writer.value is None:
        raise OSError("Native MCP control pipe returned no handle")
    return int(reader.value), int(writer.value)


def _descendant(pid: int, root: tuple[int, int]) -> bool:
    """Pin a chronological live ancestry; reused parent PIDs prove nothing."""
    seen: set[int] = set()
    with contextlib.ExitStack() as pins:
        child_start = None
        for _ in range(256):
            if pid <= 1 or pid in seen:
                return False
            seen.add(pid)
            handle = pins.enter_context(_process(pid))
            identity = _identity(handle)
            if child_start is not None and identity[1] > child_start:
                return False
            if pid == root[0]:
                return identity == root
            child_start = identity[1]
            pid = platform_compat.get_ppid(pid)
    return False


def _write(handle: int, payload: dict[str, Any], *, overlapped: bool = False) -> None:
    api: Any = importlib.import_module("_winapi")

    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    if len(data) > _MAX_FRAME:
        raise ValueError("Native MCP control frame exceeds its limit")
    result, _ = api.WriteFile(handle, data, overlapped=overlapped)
    if overlapped:
        if api.WaitForSingleObject(result.event, 3000) != api.WAIT_OBJECT_0:
            result.cancel()
            result.GetOverlappedResult(True)
            raise TimeoutError("Native MCP gateway registration timed out")
        result.GetOverlappedResult(True)


def _connect_pipe(address: str) -> int:
    api: Any = importlib.import_module("_overlapped")
    deadline = time.monotonic() + 1
    delay = 0.001
    while True:
        try:
            return int(api.ConnectPipe(address))
        except OSError as exc:
            if getattr(exc, "winerror", None) != api.ERROR_PIPE_BUSY:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Native MCP gateway pipe remained busy") from exc
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.05)


def _broker_handle(address: str, register: dict[str, Any]) -> int:

    api: Any = importlib.import_module("_winapi")

    handle = _connect_pipe(address)
    try:
        pipe = type("Pipe", (), {"handle": handle})()
        writer = type(
            "Writer", (), {"get_extra_info": lambda self, key: pipe if key == "pipe" else None}
        )()
        if socketsec.check_server_is_self(writer) is not socketsec.PeerCredResult.MATCH:
            raise PermissionError("Native MCP gateway owner is unverified")
        api.SetNamedPipeHandleState(handle, 0, None, None)
        # Commit the REGISTER before handing over the connection. A native
        # process can never send a first-frame claim, approval or daemon control.
        frame = native_scope.canonical_register(register)
        _write(handle, frame, overlapped=True)
        return int(handle)
    except BaseException:
        api.CloseHandle(handle)
        raise


def _allocate(
    request: dict[str, Any], root: tuple[int, int], root_handle: int, instance: str, address: str
) -> dict[str, Any]:
    pid, start = request.get("pid"), request.get("start")
    register, scope = request.get("register"), request.get("scope")
    if type(pid) is not int or type(start) is not int or not isinstance(register, dict):
        raise PermissionError("Native MCP session identity was rejected")
    if not isinstance(scope, str) or not native_scope.verify(scope, register, instance):
        raise PermissionError("Native MCP launch scope was rejected")
    from kiro_crew.mcp_gateway import launch_approval
    from kiro_crew.mcp_gateway.daemon.launch import _approval_env_identity
    from kiro_crew.mcp_gateway.pool import PoolKey
    from kiro_crew.mcp_gateway.required import NativeLaunchApprovalRequired

    key = PoolKey.from_register(register)
    if not launch_approval.launch_approved(
        key.server_name, key.command_args_hash, _approval_env_identity(key)
    ):
        raise NativeLaunchApprovalRequired("Native MCP launch needs operator approval")
    with _process(pid) as target:
        if (
            _identity(target) != (pid, start)
            or _identity(root_handle) != root
            or not _descendant(pid, root)
            or platform_compat.process_owner_sid(pid) != platform_compat.current_user_sid()
        ):
            raise PermissionError("Native MCP descendant identity was rejected")
        try:
            handle = _broker_handle(address, register)
        except OSError as exc:
            raise ConnectionRefusedError("Native MCP gateway is unavailable") from exc
        try:
            if _identity(root_handle) != root or _identity(target) != (pid, start):
                raise PermissionError("Native MCP runtime ended during connection handoff")
            # Data + attribute reads + SYNCHRONIZE only. In particular no
            # FILE_CREATE_PIPE_INSTANCE/FILE_APPEND_DATA, DELETE or DACL writes.
            duplicate = _duplicate(_kernel().GetCurrentProcess(), handle, target, access=0x00100083)
        finally:
            _kernel().CloseHandle(handle)
    return {"handle": duplicate}


def host(descriptor: Path, address: str, instance: str) -> None:
    """Outside-MXC owner; its parent is the ACP runtime's pinned launcher."""
    api: Any = importlib.import_module("_winapi")
    # Cold approval imports belong to host startup, before control handles are
    # published and a client's bounded handoff budget starts.
    importlib.import_module("kiro_crew.mcp_gateway.daemon.launch")

    stop = threading.Event()

    def await_parent() -> None:
        while os.read(0, 1):
            pass
        stop.set()

    threading.Thread(target=await_parent, daemon=True, name="native-mcp-parent").start()
    # Rust publishes the exact spawned root into this precreated, read-sealed
    # file. Do not discover an arbitrary process by executable name or PID alone.
    deadline = time.monotonic() + 15
    while not stop.is_set():
        try:
            initial = json.loads(descriptor.read_text("ascii"))
            pid = initial["pid"]
            if initial.get("instance") != instance:
                raise PermissionError("Native MCP runtime instance was rejected")
            if type(pid) is not int or pid <= 1:
                raise ValueError("Invalid native root")
            break
        except (ValueError, KeyError, FileNotFoundError):
            if time.monotonic() > deadline:
                raise TimeoutError("Native MCP root publication timed out")
            stop.wait(0.01)
    else:
        return
    with _process(pid) as root_handle, contextlib.ExitStack() as stack:
        root = _identity(root_handle)
        request_read, request_write = _pipe()
        response_read, response_write = _pipe()
        for handle in (request_read, request_write, response_read, response_write):
            stack.callback(_kernel().CloseHandle, handle)
        current = _kernel().GetCurrentProcess()
        native_request = _duplicate(current, request_write, root_handle, access=0x00100002)
        native_response = _duplicate(current, response_read, root_handle, access=0x00100081)
        ready = {
            "pid": pid,
            "start": root[1],
            "request": native_request,
            "response": native_response,
            "instance": instance,
        }
        # In-place publication preserves the pinned identity. MXC's exact READ
        # rule prevents the workload from writing, replacing or deleting it.
        descriptor.write_text(json.dumps(ready), encoding="ascii")
        buffer = b""
        allocated = 0
        while not stop.is_set():
            available = api.PeekNamedPipe(request_read, 0)[-2]
            if not available:
                stop.wait(0.01)
                continue
            chunk, _ = api.ReadFile(request_read, min(available, _MAX_FRAME))
            buffer += chunk
            if len(buffer) > _MAX_FRAME:
                raise ValueError("Native MCP control buffer exceeds its limit")
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                response: dict[str, Any]
                request: Any = None
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict) or not isinstance(
                        request.get("request_id"), str
                    ):
                        raise ValueError("Invalid native MCP request")
                    if allocated >= 4096:
                        raise PermissionError("Native MCP allocation budget is exhausted")
                    response = _allocate(request, root, root_handle, instance, address)
                    allocated += 1
                except (ValueError, TypeError, KeyError, OSError) as exc:
                    # Never return exception text: no tokens, environment or
                    # provider information crosses the error channel.
                    from kiro_crew.mcp_gateway.required import NativeLaunchApprovalRequired

                    response = {
                        "error": (
                            "launch_approval_required"
                            if isinstance(exc, NativeLaunchApprovalRequired)
                            else (
                                "identity_rejected"
                                if isinstance(exc, PermissionError)
                                else "connection_failed"
                            )
                        )
                    }
                if isinstance(request, dict):
                    response.update(
                        {key: request.get(key) for key in ("request_id", "pid", "start")}
                    )
                _write(response_write, response)


def _request(descriptor: Path, payload: dict[str, Any], scope: str) -> int:
    msvcrt: Any = importlib.import_module("msvcrt")

    info = json.loads(descriptor.read_text("ascii"))
    with _process(info["pid"]) as root:
        if _identity(root) != (info["pid"], info["start"]):
            raise PermissionError("Native MCP root generation was rejected")
        current = _kernel().GetCurrentProcess()
        request = _duplicate(root, info["request"], current)
        try:
            response = _duplicate(root, info["response"], current)
        except BaseException:
            _kernel().CloseHandle(request)
            raise
    request_fd = msvcrt.open_osfhandle(request, os.O_WRONLY | getattr(os, "O_BINARY", 0))
    response_fd = msvcrt.open_osfhandle(response, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    with (
        os.fdopen(request_fd, "wb", buffering=0) as writer,
        os.fdopen(response_fd, "rb", buffering=0),
    ):
        # Serialization is availability protection only. Authentication is the
        # host MAC + kernel descendant identity, never this writable lock file.
        with descriptor.with_suffix(".lock").open("a+b") as lock:
            with platform_compat.file_lock(lock.fileno(), exclusive=True, timeout=2):
                with _process(os.getpid()) as own:
                    pid, start = _identity(own)
                request_id = uuid.uuid4().hex
                frame = {
                    "request_id": request_id,
                    "pid": pid,
                    "start": start,
                    "register": payload,
                    "scope": scope,
                }
                data = json.dumps(frame, separators=(",", ":")).encode() + b"\n"
                if len(data) > _MAX_FRAME:
                    raise ValueError("Native MCP request exceeds its limit")
                writer.write(data)
                result = _read_reply(response, request_id, pid, start)
    if result.get("error"):
        if result["error"] == "launch_approval_required":
            from kiro_crew.mcp_gateway.required import GatewayRequiredError

            raise GatewayRequiredError("launch_approval_required")
        if result["error"] == "identity_rejected":
            raise PermissionError("Native MCP session identity was rejected")
        raise ConnectionRefusedError("Native MCP gateway connection failed")
    if type(result.get("handle")) is not int or result["handle"] <= 0:
        raise ConnectionRefusedError("Native MCP returned no connection")
    return result["handle"]


def _read_reply(handle: int, request_id: str, pid: int, start: int) -> dict[str, Any]:
    """A killed previous client must not poison the next client's handle reply."""
    api: Any = importlib.import_module("_winapi")

    deadline = time.monotonic() + 2
    buffer = b""
    while time.monotonic() < deadline:
        available = api.PeekNamedPipe(handle, 0)[-2]
        if not available:
            time.sleep(0.01)
            continue
        chunk, _ = api.ReadFile(handle, min(available, _MAX_FRAME))
        buffer += chunk
        if len(buffer) > _MAX_FRAME:
            raise ConnectionRefusedError("Native MCP reply exceeds its limit")
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            result = json.loads(line)
            if (result.get("request_id"), result.get("pid"), result.get("start")) == (
                request_id,
                pid,
                start,
            ):
                return result
    raise TimeoutError("Native MCP connection handoff timed out")


async def connect_registered(payload: dict[str, Any], *, limit: int):
    """Return one pre-registered connection, owned by this exact stub process."""
    windows_utils: Any = importlib.import_module("asyncio.windows_utils")

    descriptor = Path(os.environ[native_scope.DESCRIPTOR_ENV])
    scope = os.environ[native_scope.SCOPE_ENV]
    task = asyncio.create_task(asyncio.to_thread(_request, descriptor, payload, scope))
    cancelled = False
    while True:
        try:
            handle = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                handle = task.result()
                break
    if cancelled:
        _kernel().CloseHandle(handle)
        raise asyncio.CancelledError
    pipe = windows_utils.PipeHandle(handle)
    try:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=limit)
        protocol = asyncio.StreamReaderProtocol(reader)
        pipe_transport = loop._make_duplex_pipe_transport(pipe, protocol)  # type: ignore[attr-defined]
        return reader, asyncio.StreamWriter(pipe_transport, protocol, reader, loop)
    except BaseException:
        pipe.close()
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--descriptor", required=True, type=Path)
    parser.add_argument("--address", required=True)
    parser.add_argument("--instance", required=True)
    args = parser.parse_args()
    try:
        host(args.descriptor, args.address, args.instance)
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print("Native MCP handle service failed; restart the Crew session", file=sys.stderr)
        return 125


if __name__ == "__main__":
    sys.exit(main())
