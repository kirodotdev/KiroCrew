"""Local IPC transport for the stub <-> gatewayd hop.

The gateway multiplexes many stubs onto shared MCP backends over one local
endpoint. Two properties make that endpoint a security boundary rather than a
convenience:

* **Only the owning user may connect.** The endpoint carries every tool call and
  every tool result for every pooled session.
* **The server can learn who connected.** ``gatewayd`` resolves a caller
  identity from the peer's process ancestry when a stub registers without one,
  and indexes the connection under the peer's host PID chain so a later
  ``claim`` frame lands on the right connection (see ``socketsec.get_peer_pid``).

On POSIX both fall out of an ``AF_UNIX`` stream socket: a ``0600`` socket file
inside a ``0700`` directory gates connections, and ``SO_PEERCRED`` (Linux) or
``LOCAL_PEERPID`` (macOS) supplies the peer PID.

Windows has neither. ``asyncio`` does not expose ``AF_UNIX`` there at all --
``start_unix_server`` / ``open_unix_connection`` live in ``asyncio.unix_events``,
which ``asyncio/__init__.py`` never imports under ``sys.platform == "win32"``, so
the names are absent from the namespace rather than raising at call time. This
module supplies a **named pipe** instead, which preserves both properties:

* ``GetNamedPipeClientProcessId`` gives a kernel-attested peer PID, reached
  through the public ``get_extra_info("pipe")`` seam.
* An explicit owner-only DACL gates connections.

The DACL is not optional. A pipe created with ``lpSecurityAttributes = NULL``
receives a default security descriptor that (measured on a Windows CI runner)
grants ``FILE_GENERIC_READ`` to both ``Everyone`` and ``Anonymous Logon`` --
i.e. any local principal could attach and read the server-to-client direction of
a pooled session. ``asyncio`` hardcodes that NULL, which is why the server pipe
factory here replaces ``PipeServer._server_pipe_handle`` outright instead of
post-processing its result: fixing the descriptor after creation would leave a
window in which the pipe is world-readable.

Two further Windows adjustments ride along in the same replacement:

* **Byte read mode.** ``asyncio`` creates its pipes
  ``PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE``. In message read mode a
  ``ReadFile`` whose buffer is smaller than the pending message fails with
  ``ERROR_MORE_DATA``, which ``IocpProactor.recv`` does not handle; the
  gateway's frames run to 1 MiB against an 8 KiB pipe buffer, so a short buffer
  is the normal case. A MESSAGE-*type* pipe may still be read in BYTE mode, so
  each instance is flipped with ``SetNamedPipeHandleState``. The gateway's
  newline-delimited framing then needs no reframing.
* **First-instance exclusivity.** ``FILE_FLAG_FIRST_PIPE_INSTANCE`` on the first
  handle is what stops a second process from serving the same pipe name; it is
  the Windows half of the singleton guarantee and is preserved verbatim.

``windows_events.PipeServer._server_pipe_handle`` and its ``_free_instances``
attribute are the entire private-API surface. Both have been stable since the
proactor loop landed, and ``test_mcp_gateway_transport.py`` pins their existence
so a future CPython change fails in CI rather than on a user's machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import hashlib
import logging
import os
import socket as _socket
import stat
import sys
from ctypes import wintypes  # type aliases only; imports cleanly on every platform
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, cast

from kiro_crew import platform_compat
from kiro_crew.mcp_gateway import socketsec

logger = logging.getLogger(__name__)

if sys.platform == "win32":  # pragma: no cover - Windows-only import block
    from asyncio import streams as _asyncio_streams
    from asyncio import windows_events as _windows_events
    from asyncio import windows_utils as _windows_utils

    import _winapi
else:  # pragma: no cover - keeps the module importable on POSIX
    # cast(Any, None) rather than a bare None: on a POSIX host mypy only sees
    # this branch and would otherwise narrow every name below to None and flag
    # each attribute access. The bodies never run off Windows.
    _asyncio_streams = cast(Any, None)
    _windows_events = cast(Any, None)
    _windows_utils = cast(Any, None)
    _winapi = cast(Any, None)

# ``ctypes.WinDLL`` / ``ctypes.get_last_error`` exist only in the Windows ctypes
# stubs; reach them through an Any-typed alias so a POSIX mypy run does not flag
# call sites that are already inside Windows-only code paths.
_ct: Any = ctypes

ClientConnectedCb = Callable[[asyncio.StreamReader, asyncio.StreamWriter], None]

#: Mirrors ``asyncio.streams._DEFAULT_LIMIT``, the buffer ceiling
#: ``open_unix_connection`` applies when no ``limit`` is passed. Named here so
#: the control-plane call sites (claim, abort) that relied on that default keep
#: it explicitly instead of inheriting a private constant.
DEFAULT_READ_LIMIT = 2**16

# --- Windows constants -------------------------------------------------------
# Public, stable Win32 values. Declared here rather than taken from ``_winapi``
# because the pipe is created through ctypes (see module docstring) and because
# ``_winapi`` exports only the MESSAGE spellings of the read-mode flags.

_PIPE_ACCESS_DUPLEX = 0x00000003  # noqa: N806 - Windows API constant
_FILE_FLAG_OVERLAPPED = 0x40000000  # noqa: N806 - Windows API constant
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000  # noqa: N806 - Windows API constant
_PIPE_TYPE_MESSAGE = 0x00000004  # noqa: N806 - Windows API constant
_PIPE_READMODE_MESSAGE = 0x00000002  # noqa: N806 - Windows API constant
_PIPE_WAIT = 0x00000000  # noqa: N806 - Windows API constant
_PIPE_UNLIMITED_INSTANCES = 255  # noqa: N806 - Windows API constant
_NMPWAIT_WAIT_FOREVER = 0xFFFFFFFF  # noqa: N806 - Windows API constant
# INVALID_HANDLE_VALUE is ``(HANDLE)-1``. Derived rather than written as -1:
# ``CreateNamedPipeW``'s restype is ``wintypes.HANDLE`` (a ``c_void_p``), so
# ctypes hands back the UNSIGNED pointer value -- 18446744073709551615 on
# 64-bit -- and a literal -1 would never compare equal, letting a failed
# creation through as a valid-looking handle. A NULL return arrives as
# ``None``, which is why the check below tests both shapes.
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value  # noqa: N806 - Windows API constant
_SDDL_REVISION_1 = 1  # noqa: N806 - Windows API constant
_ERROR_FILE_NOT_FOUND = 2  # noqa: N806 - Windows API constant
_ERROR_SEM_TIMEOUT = 121  # noqa: N806 - Windows API constant
_ERROR_PIPE_BUSY = 231  # noqa: N806 - Windows API constant

# ``PIPE_READMODE_BYTE`` and ``PIPE_WAIT`` are both 0 in the Win32 headers, so a
# state of 0 means "byte read mode, blocking".
_PIPE_READMODE_BYTE_AND_WAIT = 0

#: Pipe names are machine-global, so the name has to be derived from something
#: per-installation. The socket path already is: it sits under
#: ``$KIROCREW_HOME``, which is per-user. Hashing it keeps the name short, legal
#: (no path separators) and identical for any two processes handed the same
#: ``--socket`` value, which is how the stub and gatewayd agree without sharing
#: state. A collision across users still fails closed: the DACL admits only the
#: creating user and ``FILE_FLAG_FIRST_PIPE_INSTANCE`` refuses a second server.
_PIPE_NAME_PREFIX = r"\\.\pipe\kirocrew-mcp-"

_SINGLETON_LOCK_SUFFIX = ".lock"

#: Set once the server pipe factory has been installed, so repeated ``serve()``
#: calls in one process do not stack wrappers.
_pipe_factory_installed = False


class _SecurityAttributes(ctypes.Structure):
    """Win32 ``SECURITY_ATTRIBUTES``, carrying the owner-only pipe DACL.

    Module scope is load-bearing: ``ctypes.POINTER(T)`` memoises T in a
    module-level ctypes dict that is never evicted, so declaring this inside the
    factory below would pin a fresh type object per call. That factory runs once
    per pipe INSTANCE -- i.e. per accepted MCP client connection -- so a local
    declaration grows gatewayd without bound.
    """

    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


# --- Address resolution ------------------------------------------------------


def resolve_address(socket_path: str | os.PathLike[str]) -> str:
    """Return the platform address the transport binds/connects to.

    POSIX: the socket path itself. Windows: a named pipe derived from it. Both
    the stub and gatewayd call this with the same ``--socket`` value, so they
    arrive at the same address without exchanging anything.

    The Windows digest is taken over the ``normcase``-folded path because the
    filesystem is case-insensitive while a hash is not: ``C:\\Users\\Foo`` and
    ``c:\\users\\foo`` name one file but would otherwise produce two different
    pipe names, so a daemon and a stub handed different spellings of the same
    path would bind and connect to different pipes and never meet. Separator
    folding is already done by ``Path`` (it renders ``/`` as ``\\`` on Windows);
    ``normcase`` additionally lowercases, and covers both in one step.
    """
    if platform_compat.IS_WINDOWS:
        canonical = os.path.normcase(str(Path(socket_path)))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return _PIPE_NAME_PREFIX + digest[:32]
    return str(socket_path)


def lock_path_for(socket_path: str | os.PathLike[str]) -> Path:
    """Path of the singleton lock file guarding ``socket_path``.

    A real file on both platforms: the Windows transport has no filesystem
    entry of its own, but the singleton guard still needs one to lock, and
    ``platform_compat.try_acquire_lock`` requires a dedicated file (it locks
    byte 0).
    """
    p = Path(socket_path)
    return p.parent / (p.name + _SINGLETON_LOCK_SUFFIX)


# --- Windows security descriptor ---------------------------------------------


def _owner_only_sddl() -> str:
    """SDDL granting full control to this user alone.

    ``D:P`` marks the DACL protected, so no inherited ACE can widen it, and the
    single ACE names the current user's SID. ``platform_compat`` already
    resolves and memoises that SID for ``restrict_to_owner``; reusing it keeps
    one source of truth for "who is the owner" across files and pipes.
    """
    sid = platform_compat.current_user_sid()
    if not sid:
        # Mirror restrict_to_owner: refuse rather than fall back to a default
        # descriptor. The measured default grants Everyone read access, so a
        # silent fallback would hand out session traffic.
        raise OSError(
            "cannot determine the current user SID; refusing to create a "
            "gateway pipe without an owner-only DACL"
        )
    return f"D:P(A;;FA;;;{sid})"


@contextlib.contextmanager
def _owner_only_security_attributes() -> Iterator[Any]:
    """Yield a ``SECURITY_ATTRIBUTES`` describing an owner-only DACL.

    The descriptor is allocated by ``ConvertStringSecurityDescriptorToSecurity
    DescriptorW`` (LocalAlloc) and freed on exit; the struct must outlive the
    ``CreateNamedPipeW`` call, which is why this is a context manager rather
    than a plain factory.
    """
    advapi32 = _ct.WinDLL("advapi32", use_last_error=True)
    kernel32 = _ct.WinDLL("kernel32", use_last_error=True)

    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

    psd = ctypes.c_void_p()
    ok = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        _owner_only_sddl(),
        wintypes.DWORD(_SDDL_REVISION_1),
        ctypes.byref(psd),
        None,
    )
    if not ok:
        raise OSError(
            f"ConvertStringSecurityDescriptorToSecurityDescriptorW failed: "
            f"{_ct.get_last_error()}"
        )
    try:
        sa = _SecurityAttributes()
        sa.nLength = ctypes.sizeof(_SecurityAttributes)
        sa.lpSecurityDescriptor = psd
        sa.bInheritHandle = False
        yield sa
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(psd)


def _create_server_pipe(address: str, first: bool) -> int:
    """Create one server pipe instance with byte read mode and an owner DACL.

    Mirrors ``asyncio.windows_events.PipeServer._server_pipe_handle``'s flags
    exactly -- including ``FILE_FLAG_FIRST_PIPE_INSTANCE`` on the first
    instance, which is the Windows half of the singleton guarantee -- and
    differs only in passing a real security descriptor and flipping the read
    mode before the handle is ever returned.
    """
    kernel32 = _ct.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel32.CreateNamedPipeW.restype = wintypes.HANDLE

    open_mode = _PIPE_ACCESS_DUPLEX | _FILE_FLAG_OVERLAPPED
    if first:
        open_mode |= _FILE_FLAG_FIRST_PIPE_INSTANCE

    with _owner_only_security_attributes() as sa:
        handle = kernel32.CreateNamedPipeW(
            address,
            wintypes.DWORD(open_mode),
            wintypes.DWORD(_PIPE_TYPE_MESSAGE | _PIPE_READMODE_MESSAGE | _PIPE_WAIT),
            wintypes.DWORD(_PIPE_UNLIMITED_INSTANCES),
            wintypes.DWORD(_windows_utils.BUFSIZE),
            wintypes.DWORD(_windows_utils.BUFSIZE),
            wintypes.DWORD(_NMPWAIT_WAIT_FOREVER),
            ctypes.byref(sa),
        )
    if handle is None or handle == 0 or handle == _INVALID_HANDLE_VALUE:
        raise OSError(f"CreateNamedPipeW({address!r}) failed: {_ct.get_last_error()}")

    # Flip to byte read mode before the handle is handed to the proactor, so no
    # read can ever observe message framing.
    #
    # The handle has no Python owner yet -- the caller is what wraps it in a
    # PipeHandle -- so a raise here would orphan a kernel pipe instance that
    # nothing can reclaim: PipeServer.close() only walks ``_free_instances``,
    # which this handle has not entered. Close it on the way out. BaseException,
    # not OSError, so a KeyboardInterrupt landing in this window cannot orphan it
    # either.
    try:
        _winapi.SetNamedPipeHandleState(handle, _PIPE_READMODE_BYTE_AND_WAIT, None, None)
    except BaseException:
        _winapi.CloseHandle(handle)
        raise
    return int(handle)


def _install_pipe_factory() -> None:
    """Replace ``PipeServer._server_pipe_handle`` with the hardened factory.

    ``PipeServer`` mints a fresh handle per accepted client, so the read-mode
    flip and the DACL have to be applied per instance -- which is why this
    wraps the factory method rather than adjusting one handle at startup.
    Idempotent: installing twice would stack wrappers on repeated ``serve()``
    calls in one process.
    """
    global _pipe_factory_installed
    if _pipe_factory_installed:
        return

    def _server_pipe_handle(self: Any, first: bool) -> Any:
        if self.closed():
            return None
        handle = _create_server_pipe(self._address, first)
        pipe = _windows_utils.PipeHandle(handle)
        self._free_instances.add(pipe)
        return pipe

    _windows_events.PipeServer._server_pipe_handle = _server_pipe_handle
    _pipe_factory_installed = True


# --- Server ------------------------------------------------------------------


class TransportServer:
    """Uniform handle over the two server shapes.

    ``asyncio.start_unix_server`` returns an ``asyncio.Server``;
    ``loop.start_serving_pipe`` returns a list of ``PipeServer``, which has
    ``close()`` and ``closed()`` but no ``wait_closed()``. Callers await
    ``wait_closed()`` unconditionally, so the Windows branch supplies the
    missing half rather than making every call site branch.
    """

    def __init__(
        self,
        *,
        unix_server: Optional[asyncio.AbstractServer] = None,
        pipe_servers: Optional[list[Any]] = None,
    ) -> None:
        self._unix_server = unix_server
        self._pipe_servers = pipe_servers or []

    @property
    def is_pipe(self) -> bool:
        """True when this is a Windows named-pipe server."""
        return self._unix_server is None

    def is_serving(self) -> bool:
        """Whether the accept loop is still live.

        The zombie watchdog polls this to catch an accept loop that died
        without the daemon noticing. ``PipeServer`` exposes ``closed()``
        rather than ``is_serving()``, so invert it; a server with no
        instances at all is not serving either.
        """
        if self._unix_server is not None:
            return self._unix_server.is_serving()
        if not self._pipe_servers:
            return False
        return not any(server.closed() for server in self._pipe_servers)

    def close(self) -> None:
        if self._unix_server is not None:
            self._unix_server.close()
            return
        for server in self._pipe_servers:
            with contextlib.suppress(Exception):
                server.close()

    async def wait_closed(self) -> None:
        if self._unix_server is not None:
            await self._unix_server.wait_closed()
            return
        # PipeServer.close() is synchronous and complete -- it closes every
        # free instance and marks the server closed. There is nothing further
        # to await, so this is a shim that keeps the caller's shape uniform.
        return None


async def serve(
    socket_path: str | os.PathLike[str],
    client_connected_cb: ClientConnectedCb,
    *,
    limit: int,
) -> TransportServer:
    """Bind the local endpoint for ``socket_path`` and start accepting.

    ``client_connected_cb`` receives ``(reader, writer)`` per connection, the
    same contract ``asyncio.start_unix_server`` provides.
    """
    address = resolve_address(socket_path)
    if not platform_compat.IS_WINDOWS:
        server = await asyncio.start_unix_server(
            client_connected_cb, path=address, limit=limit
        )
        return TransportServer(unix_server=server)

    loop: Any = asyncio.get_running_loop()
    if not hasattr(loop, "start_serving_pipe"):
        raise RuntimeError(
            "the MCP gateway needs a ProactorEventLoop on Windows; the running "
            f"loop is {type(loop).__name__}"
        )
    _install_pipe_factory()

    def protocol_factory() -> Any:
        # asyncio ships no start_serving_pipe counterpart to
        # start_unix_server's reader/writer contract, so wire
        # StreamReaderProtocol by hand -- it is what start_unix_server does
        # internally with the same limit.
        reader = asyncio.StreamReader(limit=limit, loop=loop)
        return _asyncio_streams.StreamReaderProtocol(
            reader, client_connected_cb, loop=loop
        )

    pipe_servers = await loop.start_serving_pipe(protocol_factory, address)
    return TransportServer(pipe_servers=list(pipe_servers))


# --- Client ------------------------------------------------------------------


async def connect(
    socket_path: str | os.PathLike[str], *, limit: int = DEFAULT_READ_LIMIT
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to the local endpoint for ``socket_path``.

    Raises the same errors as ``asyncio.open_unix_connection`` --
    ``FileNotFoundError`` when nothing is listening, ``ConnectionRefusedError``
    / ``OSError`` otherwise -- so callers keep their existing except clauses.
    """
    address = resolve_address(socket_path)
    if not platform_compat.IS_WINDOWS:
        return await asyncio.open_unix_connection(path=address, limit=limit)

    loop: Any = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=limit, loop=loop)
    protocol = _asyncio_streams.StreamReaderProtocol(reader, loop=loop)
    transport, _ = await loop.create_pipe_connection(lambda: protocol, address)
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    # The client handle's read mode is whatever CreateFile defaulted to. Set it
    # explicitly so framing does not depend on that default: a MESSAGE-mode
    # client read would truncate at the message boundary and desynchronise the
    # newline-delimited protocol.
    pipe = writer.get_extra_info("pipe")
    if pipe is not None:
        with contextlib.suppress(OSError):
            _winapi.SetNamedPipeHandleState(
                pipe.handle, _PIPE_READMODE_BYTE_AND_WAIT, None, None
            )
    # Verify the endpoint is served by us BEFORE the caller writes anything.
    # The pipe namespace is machine-global and the name is derivable, so a local
    # principal can pre-create it and collect the register/claim frames -- which
    # carry session keys. Checking here covers every connect call site at once,
    # and checking before the first write also means a squatter never receives a
    # message to impersonate us from. Refusing raises ConnectionRefusedError (an
    # OSError), the shape callers already handle as "gateway unreachable", so the
    # stub degrades to a per-session MCP server rather than trusting it.
    verdict = socketsec.check_server_is_self(writer)
    if verdict is not socketsec.PeerCredResult.MATCH:
        # close() and do NOT await wait_closed(): that waits for the peer's
        # connection_lost, and this peer has just been judged untrustworthy --
        # a squatter that never closes would hang the caller's startup here.
        # close() already releases the handle, which is all we need.
        writer.close()
        raise ConnectionRefusedError(
            f"refusing pipe {address}: server principal not confirmed "
            f"({verdict.value})"
        )
    return reader, writer


# --- Endpoint lifecycle ------------------------------------------------------


def prepare_dir(socket_path: str | os.PathLike[str]) -> None:
    """Create the endpoint's containing directory, owner-only.

    Needed on both platforms: even where the transport itself has no
    filesystem entry, the singleton lock file, the out-of-band ``.backends``
    reap list and the hot-key store all live in this directory.

    See ``platform_compat.make_owner_only_dir`` for why this is not
    ``restrict_to_owner`` on POSIX (that helper applies ``0o600``, which leaves a
    directory untraversable).
    """
    parent = Path(socket_path).parent
    # Called by attribute so the hermetic-test stub in conftest can intercept
    # the Windows icacls path.
    platform_compat.make_owner_only_dir(parent)


def acquire_singleton_lock(socket_path: str | os.PathLike[str]) -> Optional[int]:
    """Take the exclusive advisory lock guarding ``socket_path``.

    Returns the held fd, or ``None`` when another live daemon holds it. The
    OS releases the lock on process death, so there is no stale-lock mode.
    """
    lock_file = lock_path_for(socket_path)
    # O_CLOEXEC keeps the fd out of spawned backends. It does not exist on
    # Windows, where PEP 446 already makes every fd Python opens
    # non-inheritable, so the flag is simply absent there.
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(str(lock_file), flags, 0o600)
    if not platform_compat.try_acquire_lock(fd, exclusive=True):
        os.close(fd)
        return None
    return fd


def probe_live(socket_path: str | os.PathLike[str]) -> bool:
    """Blocking check for a server currently listening on the endpoint.

    Runs in a thread at every call site (it blocks for up to a second) and
    never raises: an inconclusive probe reports ``True`` so callers, which use
    this to decide whether it is safe to clobber an endpoint, err toward
    leaving it alone.
    """
    address = resolve_address(socket_path)
    if not platform_compat.IS_WINDOWS:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        try:
            s.settimeout(1.0)
            s.connect(address)
            return True
        except (ConnectionRefusedError, OSError):
            return False
        finally:
            s.close()

    # No filesystem entry to inspect: ask the OS whether the name resolves.
    # WaitNamedPipe raises FileNotFoundError when no server has ever created
    # the name, and ERROR_SEM_TIMEOUT / ERROR_PIPE_BUSY when a server exists
    # but has no free instance right now -- both of the latter mean "live".
    try:
        _winapi.WaitNamedPipe(address, 1)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        # getattr: ``winerror`` exists only on the Windows OSError shape, and
        # this module is type-checked on Linux where the attribute is absent.
        return getattr(exc, "winerror", None) in (_ERROR_SEM_TIMEOUT, _ERROR_PIPE_BUSY)


def endpoint_exists(socket_path: str | os.PathLike[str]) -> bool:
    """Whether the endpoint is currently reachable.

    Replaces a bare ``path.exists()`` at readiness-poll sites. On Windows the
    pipe has no directory entry, so reachability is the only observable form of
    "it is there".
    """
    if platform_compat.IS_WINDOWS:
        return probe_live(socket_path)
    return Path(socket_path).exists()


async def remove_stale(socket_path: str | os.PathLike[str]) -> None:
    """Remove an endpoint left behind by a prior crash.

    POSIX only in effect: a Windows named pipe has no persistent name -- the
    kernel drops it when the last handle closes -- so there is nothing to
    clean up and nothing that could block a rebind.
    """
    if platform_compat.IS_WINDOWS:
        return
    path = Path(socket_path)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        logger.warning(
            "path %s exists and is not a socket (mode=%o); leaving in place",
            path,
            st.st_mode,
        )
        return
    if await asyncio.to_thread(probe_live, path):
        logger.warning(
            "socket %s is live (connect succeeded); refusing to unlink — "
            "another gatewayd instance may be running",
            path,
        )
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("could not remove stale socket %s: %s", path, exc)


def teardown(socket_path: str | os.PathLike[str]) -> None:
    """Release the endpoint on clean shutdown.

    Only meaningful on POSIX, where the bound socket file outlives the process
    that created it.
    """
    if platform_compat.IS_WINDOWS:
        return
    try:
        Path(socket_path).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("could not unlink gateway socket %s: %s", socket_path, exc)


def harden_endpoint(socket_path: str | os.PathLike[str]) -> None:
    """Tighten the freshly-bound endpoint to owner-only.

    POSIX: chmod the socket file to ``0600``. Windows: nothing to do -- the
    owner-only DACL is applied at creation, because a NULL-descriptor pipe is
    readable by ``Everyone`` and fixing it afterwards would leave a window.
    """
    if platform_compat.IS_WINDOWS:
        return
    platform_compat.chmod_safe(Path(socket_path), 0o600)
