"""Tests for the gateway's local IPC transport layer.

Three jobs:

1. Exercise the POSIX branch against real sockets -- this is the code every
   existing gateway test now runs through, so a break here is a break
   everywhere.
2. Cover the platform dispatch that cannot be reached from Linux, using the
   ``monkeypatch(IS_WINDOWS)`` pattern established by ``test_platform_compat``.
3. Pin the two private ``asyncio`` attributes the Windows server pipe factory
   depends on. Those assertions run only on Windows, where they are meaningful,
   so a future CPython that renames either one fails in CI rather than on a
   user's machine.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.mcp_gateway import transport

# --- Address resolution ------------------------------------------------------


#: Conservative ``sun_path`` capacity. The real limit is 108 bytes on Linux and
#: 104 on macOS/BSD, both including the NUL terminator; 100 leaves headroom for
#: the longest filename these tests append.
_SUN_PATH_BUDGET = 100


@pytest.mark.skipif(pc.IS_WINDOWS, reason="sun_path limit is a POSIX constraint")
def test_sock_dir_stays_within_the_af_unix_limit(sock_dir: Path) -> None:
    """Guard the ``sock_dir`` fixture, on a platform where it can be checked.

    ``sock_dir`` exists because a long endpoint path fails ``bind`` with
    ``OSError: AF_UNIX path too long`` -- six binds in this module failed that way
    the first time the suite ran on macOS. But nothing asserts the property the
    fixture is supposed to deliver, and a regression would NOT fail on Linux:
    pytest's temp root there already fits inside ``sun_path``, which is exactly why
    the original defect stayed invisible until a macOS runner existed.

    Asserting the length rather than the platform means a regression is caught on
    the matrix that runs every PR, instead of waiting for the macOS job.
    """
    sock = sock_dir / "gateway.sock"
    assert len(str(sock).encode()) < _SUN_PATH_BUDGET, (
        f"sock_dir yields a {len(str(sock).encode())}-byte endpoint path, over the "
        f"{_SUN_PATH_BUDGET}-byte budget -- AF_UNIX bind fails with 'path too long' "
        "on runners with a long temp root (macOS TMPDIR is /var/folders/...)"
    )


@pytest.mark.skipif(pc.IS_WINDOWS, reason="the pass-through is the POSIX branch")
def test_posix_address_is_the_socket_path(tmp_path: Path) -> None:
    sock = tmp_path / "gateway.sock"
    assert transport.resolve_address(sock) == str(sock)


def test_windows_address_is_a_pipe_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    addr = transport.resolve_address("/some/home/mcp-gateway/gateway.sock")
    assert addr.startswith(r"\\.\pipe\kirocrew-mcp-")
    # No path separators survive into the name: a pipe name is a flat namespace.
    assert "/" not in addr[len(r"\\.\pipe\\"):]


def test_windows_address_is_deterministic_and_path_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stub and gatewayd derive the name independently from --socket.

    They exchange nothing, so the same path must always give the same name, and
    two installations (different $KIROCREW_HOME) must not collide on the
    machine-global pipe namespace.
    """
    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    a1 = transport.resolve_address("/home/alice/.kiro/crew/mcp-gateway/gateway.sock")
    a2 = transport.resolve_address("/home/alice/.kiro/crew/mcp-gateway/gateway.sock")
    b = transport.resolve_address("/home/bob/.kiro/crew/mcp-gateway/gateway.sock")
    assert a1 == a2
    assert a1 != b


def test_windows_address_folds_case_and_separators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Equivalent spellings of one Windows path must yield ONE pipe name.

    The filesystem is case-insensitive but a hash is not, so without folding a
    daemon started as ``C:\\Users\\Foo\\...`` and a stub handed
    ``c:\\users\\foo\\...`` would bind and connect to two different pipes and
    never meet -- with no error, just a gateway nothing can reach.

    ``ntpath.normcase`` is substituted for ``os.path.normcase`` because the
    latter is the identity function on this POSIX runner: it IS ntpath's
    implementation on Windows, so this exercises the real folding rather than a
    no-op. The Windows-only test below asserts the same property natively.
    """
    import ntpath

    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    monkeypatch.setattr(transport.os.path, "normcase", ntpath.normcase)

    upper = transport.resolve_address(r"C:\Users\Foo\.kiro\crew\gateway.sock")
    lower = transport.resolve_address(r"c:\users\foo\.kiro\crew\gateway.sock")
    assert upper == lower, "case-different spellings of one path gave two pipe names"

    # Distinct installations must still not collide.
    other = transport.resolve_address(r"C:\Users\Bar\.kiro\crew\gateway.sock")
    assert upper != other


@pytest.mark.skipif(not pc.IS_WINDOWS, reason="native path semantics")
def test_windows_address_folds_case_natively() -> None:
    """The same property without substituting normcase -- on Windows the real
    ``os.path.normcase`` does the folding."""
    upper = transport.resolve_address(r"C:\Users\Foo\.kiro\crew\gateway.sock")
    lower = transport.resolve_address(r"c:\users\foo\.kiro\crew\gateway.sock")
    slashes = transport.resolve_address("C:/Users/Foo/.kiro/crew/gateway.sock")
    assert upper == lower == slashes


def test_lock_path_sits_beside_the_endpoint(tmp_path: Path) -> None:
    lock = transport.lock_path_for(tmp_path / "gateway.sock")
    assert lock == tmp_path / "gateway.sock.lock"


# --- Directory + singleton lock ----------------------------------------------


@pytest.mark.skipif(pc.IS_WINDOWS, reason="POSIX mode bits")
def test_prepare_dir_creates_owner_only_directory(tmp_path: Path) -> None:
    sock = tmp_path / "nested" / "deeper" / "gateway.sock"
    transport.prepare_dir(sock)
    assert sock.parent.is_dir()
    assert stat.S_IMODE(sock.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(pc.IS_WINDOWS, reason="POSIX mode bits")
def test_prepare_dir_tightens_a_preexisting_loose_directory(tmp_path: Path) -> None:
    """mkdir's mode is ignored for an existing directory, which is exactly the
    upgrade case: a home created before the 0700 guarantee."""
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    transport.prepare_dir(loose / "gateway.sock")
    assert stat.S_IMODE(loose.stat().st_mode) == 0o700


def test_singleton_lock_admits_exactly_one_holder(tmp_path: Path) -> None:
    sock = tmp_path / "gateway.sock"
    transport.prepare_dir(sock)
    first = transport.acquire_singleton_lock(sock)
    assert first is not None
    try:
        assert transport.acquire_singleton_lock(sock) is None
    finally:
        os.close(first)
    # Once released, the next daemon can take it.
    second = transport.acquire_singleton_lock(sock)
    assert second is not None
    os.close(second)


def test_singleton_lock_file_is_created_beside_the_endpoint(tmp_path: Path) -> None:
    sock = tmp_path / "gateway.sock"
    transport.prepare_dir(sock)
    fd = transport.acquire_singleton_lock(sock)
    assert fd is not None
    try:
        assert transport.lock_path_for(sock).exists()
    finally:
        os.close(fd)


# --- POSIX serve / connect ---------------------------------------------------


def _close_immediately(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Accept-and-drop callback for tests that only need a bound endpoint.

    Not a no-op: ``probe_live`` really connects, and since Python 3.12
    ``Server.wait_closed()`` waits for every accepted connection to finish, so a
    callback that never closes its writer wedges teardown.
    """
    writer.close()


@pytest.fixture()
def sock_dir(tmp_path: Path) -> Iterator[Path]:
    """A directory short enough to hold a bindable ``AF_UNIX`` endpoint.

    ``sockaddr_un.sun_path`` is a FIXED-SIZE char array — 104 bytes on macOS/BSD,
    108 on Linux — and the kernel rejects a longer path with ``OSError: AF_UNIX
    path too long`` at ``bind`` time. It is a limit on the path, not on any
    configurable buffer, so there is nothing to raise.

    ``tmp_path`` cannot be used by the tests that BIND: pytest derives it from
    ``TMPDIR`` and appends the test's own name, and on macOS ``TMPDIR`` is already
    a ~50-byte per-user path under ``/var/folders/...``. A test whose name is long
    enough — ``test_harden_endpoint_makes_the_socket_owner_only`` — pushed the
    total to 132 bytes and failed on every macOS checkout while passing on Linux,
    where the shorter ``/tmp`` and the 108-byte cap both help.

    ``/tmp`` directly, with a short unique leaf: the path stays ~25 bytes, so it
    fits on either platform regardless of how the test is named. Only the tests
    that actually bind a socket need this; the ones asserting path arithmetic
    (``lock_path_for``, ``resolve_address``) are unaffected and keep ``tmp_path``.
    """
    base = Path(tempfile.mkdtemp(prefix="kcs-", dir="/tmp"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.skipif(pc.IS_WINDOWS, reason="exercises the AF_UNIX branch")
@pytest.mark.asyncio
async def test_serve_and_connect_round_trip_preserves_newline_framing(
    sock_dir: Path,
) -> None:
    """Two whole frames in one write must come back as two reads.

    The gateway's protocol is newline-delimited JSON on a byte stream; this is
    the property the Windows read-mode flip exists to preserve, asserted here on
    the branch where it is free.
    """
    sock = sock_dir / "gateway.sock"
    transport.prepare_dir(sock)
    seen: list[bytes] = []
    done = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(await reader.readuntil(b"\n"))
        seen.append(await reader.readuntil(b"\n"))
        writer.write(b'{"type":"pong"}\n')
        await writer.drain()
        # Since Python 3.12 Server.wait_closed() waits for accepted connections
        # to finish, so a handler that leaks its writer wedges teardown.
        writer.close()
        done.set()

    def on_connect(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        asyncio.create_task(handle(r, w))

    server = await transport.serve(sock, on_connect, limit=1 << 20)
    try:
        assert server.is_serving()
        reader, writer = await transport.connect(sock, limit=1 << 20)
        try:
            writer.write(b'{"id":1}\n{"id":2}\n')
            await writer.drain()
            assert await asyncio.wait_for(reader.readuntil(b"\n"), timeout=5) == (
                b'{"type":"pong"}\n'
            )
        finally:
            writer.close()
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        server.close()
        await server.wait_closed()
    assert seen == [b'{"id":1}\n', b'{"id":2}\n']


@pytest.mark.skipif(pc.IS_WINDOWS, reason="exercises the AF_UNIX branch")
@pytest.mark.asyncio
async def test_harden_endpoint_makes_the_socket_owner_only(sock_dir: Path) -> None:
    sock = sock_dir / "gateway.sock"
    transport.prepare_dir(sock)
    server = await transport.serve(sock, _close_immediately, limit=1 << 16)
    try:
        transport.harden_endpoint(sock)
        assert stat.S_IMODE(sock.stat().st_mode) & 0o077 == 0
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.skipif(pc.IS_WINDOWS, reason="AF_UNIX connect probe")
def test_connect_to_a_missing_endpoint_raises_oserror(tmp_path: Path) -> None:
    async def go() -> None:
        await transport.connect(tmp_path / "absent.sock")

    with pytest.raises(OSError):
        asyncio.run(go())


# --- Liveness / staleness ----------------------------------------------------


@pytest.mark.skipif(pc.IS_WINDOWS, reason="AF_UNIX connect probe")
@pytest.mark.asyncio
async def test_probe_live_distinguishes_a_bound_endpoint(sock_dir: Path) -> None:
    sock = sock_dir / "gateway.sock"
    transport.prepare_dir(sock)
    assert transport.probe_live(sock) is False
    server = await transport.serve(sock, _close_immediately, limit=1 << 16)
    try:
        assert transport.probe_live(sock) is True
        assert transport.endpoint_exists(sock) is True
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.skipif(pc.IS_WINDOWS, reason="POSIX socket files")
@pytest.mark.asyncio
async def test_remove_stale_unlinks_a_dead_socket(sock_dir: Path) -> None:
    sock = sock_dir / "gateway.sock"
    transport.prepare_dir(sock)
    # A bound-then-abandoned socket file: what a crash leaves behind.
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(sock))
    s.close()
    assert sock.exists()
    await transport.remove_stale(sock)
    assert not sock.exists()


@pytest.mark.skipif(pc.IS_WINDOWS, reason="POSIX socket files")
@pytest.mark.asyncio
async def test_remove_stale_refuses_a_live_endpoint(sock_dir: Path) -> None:
    """Unlinking a live socket would strand the running daemon and send every
    stub to per-session fallback; the bind must be allowed to fail instead."""
    sock = sock_dir / "gateway.sock"
    transport.prepare_dir(sock)
    server = await transport.serve(sock, _close_immediately, limit=1 << 16)
    try:
        await transport.remove_stale(sock)
        assert sock.exists()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.skipif(pc.IS_WINDOWS, reason="POSIX socket files")
@pytest.mark.asyncio
async def test_remove_stale_leaves_a_non_socket_alone(tmp_path: Path) -> None:
    """A regular file at the endpoint path is operator error, not our call."""
    sock = tmp_path / "gateway.sock"
    sock.write_text("not a socket")
    await transport.remove_stale(sock)
    assert sock.exists()


@pytest.mark.skipif(pc.IS_WINDOWS, reason="POSIX socket files")
@pytest.mark.asyncio
async def test_remove_stale_is_a_noop_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named pipe has no persistent name, so there is nothing to clean up --
    and nothing that may be deleted, since the path may hold the lock file."""
    sock = tmp_path / "gateway.sock"
    sock.write_text("x")
    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    await transport.remove_stale(sock)
    assert sock.exists()


@pytest.mark.skipif(pc.IS_WINDOWS, reason="POSIX socket files")
def test_teardown_unlinks_on_posix_and_not_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock = tmp_path / "gateway.sock"
    sock.write_text("x")
    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    transport.teardown(sock)
    assert sock.exists()
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    transport.teardown(sock)
    assert not sock.exists()


def test_teardown_tolerates_a_missing_endpoint(tmp_path: Path) -> None:
    transport.teardown(tmp_path / "never-existed.sock")


# --- TransportServer wrapper -------------------------------------------------


class _FakePipeServer:
    def __init__(self) -> None:
        self._closed = False

    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True


@pytest.mark.asyncio
async def test_pipe_server_shape_reports_serving_and_closes() -> None:
    """PipeServer has close()/closed() but no wait_closed(); the wrapper
    supplies the missing half so no call site has to branch."""
    a, b = _FakePipeServer(), _FakePipeServer()
    server = transport.TransportServer(pipe_servers=[a, b])
    assert server.is_pipe is True
    assert server.is_serving() is True
    server.close()
    assert (a.closed(), b.closed()) == (True, True)
    assert server.is_serving() is False
    await server.wait_closed()  # must not raise


def test_pipe_server_with_no_instances_is_not_serving() -> None:
    assert transport.TransportServer(pipe_servers=[]).is_serving() is False


def test_pipe_server_close_survives_a_raising_instance() -> None:
    """One instance failing to close must not abort teardown of the rest."""

    class _Boom(_FakePipeServer):
        def close(self) -> None:
            raise OSError("nope")

    good = _FakePipeServer()
    transport.TransportServer(pipe_servers=[_Boom(), good]).close()
    assert good.closed() is True


# --- Windows security descriptor ---------------------------------------------


def test_owner_only_sddl_names_the_current_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pc, "current_user_sid", lambda: "S-1-5-21-1-2-3-1001")
    sddl = transport._owner_only_sddl()
    # D:P -- protected, so no inherited ACE can widen it. FA -- full access.
    assert sddl == "D:P(A;;FA;;;S-1-5-21-1-2-3-1001)"


def test_owner_only_sddl_refuses_without_a_sid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse rather than fall back to the default descriptor.

    A NULL-descriptor pipe grants Everyone and Anonymous Logon read access, so a
    silent fallback would publish session traffic. Mirrors restrict_to_owner,
    which raises for the same reason.
    """
    monkeypatch.setattr(pc, "current_user_sid", lambda: None)
    with pytest.raises(OSError, match="owner-only DACL"):
        transport._owner_only_sddl()


# --- Private-API contract pins (Windows only) --------------------------------


@pytest.mark.skipif(not pc.IS_WINDOWS, reason="pins Windows asyncio internals")
def test_asyncio_pipe_server_still_exposes_the_seams_we_wrap() -> None:
    """The whole private surface of the Windows transport, asserted in one place.

    ``_server_pipe_handle`` is replaced (not wrapped) because asyncio hardcodes
    ``lpSecurityAttributes = NULL`` inside it, and the replacement adds the
    instance to ``_free_instances`` exactly as the original does.
    """
    from asyncio import windows_events

    assert hasattr(windows_events.PipeServer, "_server_pipe_handle")

    # A real name, not a placeholder: PipeServer.__init__ calls
    # _server_pipe_handle(True) immediately, so an address without the
    # \\.\pipe\ prefix makes CreateNamedPipe fail and the constructor raise
    # before the attribute can be inspected.
    server = windows_events.PipeServer(
        transport._PIPE_NAME_PREFIX + f"seamprobe-{os.getpid()}"
    )
    try:
        assert hasattr(server, "_free_instances")
    finally:
        server.close()


@pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows pipe creation")
def test_create_server_pipe_raises_on_a_name_the_os_rejects() -> None:
    """The failure path of the handle check.

    CreateNamedPipeW reports failure by returning INVALID_HANDLE_VALUE, which
    on a 64-bit build arrives through ctypes as an unsigned pointer value --
    comparing it against -1 silently succeeds and hands a dead handle to the
    proactor. Only a real rejected name exercises that comparison.
    """
    with pytest.raises(OSError):
        transport._create_server_pipe("not-a-pipe-name", first=True)


@pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows pipe creation")
def test_create_server_pipe_closes_the_handle_when_the_read_mode_flip_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise between create and hand-off must not orphan a kernel handle.

    The handle has no Python owner until the caller wraps it in a PipeHandle, and
    ``PipeServer.close()`` only walks ``_free_instances``, which it has not
    entered yet. So nothing would ever reclaim it: the named-pipe instance would
    leak once per failed accept for the life of the gateway.
    """
    import _winapi

    real_close = _winapi.CloseHandle
    closed: list[int] = []
    address = transport.resolve_address(tmp_path / "gateway.sock")

    def _boom(*_args: object) -> None:
        raise OSError("read-mode flip failed")

    def _spy_close(handle: int) -> None:
        closed.append(int(handle))
        real_close(handle)

    with monkeypatch.context() as m:
        m.setattr(transport._winapi, "SetNamedPipeHandleState", _boom)
        m.setattr(transport._winapi, "CloseHandle", _spy_close)
        with pytest.raises(OSError, match="read-mode flip"):
            transport._create_server_pipe(address, first=True)

    assert len(closed) == 1, "the orphaned pipe handle was not closed"
    # Proof the instance is really gone: FILE_FLAG_FIRST_PIPE_INSTANCE refuses a
    # second first-instance while any handle to the name is still open, so this
    # only succeeds if the failed attempt released it.
    handle = transport._create_server_pipe(address, first=True)
    try:
        assert handle
    finally:
        real_close(handle)


@pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows path separators")
def test_windows_address_is_separator_insensitive(tmp_path: Path) -> None:
    """The stub and gatewayd receive --socket as text and may disagree on the
    separator; they must still derive the same pipe name."""
    native = tmp_path / "sub" / "gateway.sock"
    assert transport.resolve_address(native) == transport.resolve_address(
        str(native).replace("\\", "/")
    )


@pytest.mark.skipif(not pc.IS_WINDOWS, reason="pins Windows asyncio internals")
def test_pipe_server_has_no_wait_closed_so_the_shim_is_needed() -> None:
    from asyncio import windows_events

    assert hasattr(windows_events.PipeServer, "close")
    assert not hasattr(windows_events.PipeServer, "wait_closed")


@pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows-only stdlib surface")
def test_winapi_exposes_the_calls_the_transport_needs() -> None:
    """No pywin32, no hand-rolled prototype: both come from the stdlib."""
    import _winapi

    assert hasattr(_winapi, "SetNamedPipeHandleState")
    assert hasattr(_winapi, "WaitNamedPipe")


@pytest.mark.skipif(not pc.IS_WINDOWS, reason="exercises the named-pipe branch")
@pytest.mark.asyncio
async def test_windows_round_trip_over_a_hardened_pipe(tmp_path: Path) -> None:
    """End-to-end on the real transport: bind, connect, frame, tear down.

    The frame is deliberately larger than asyncio's 8 KiB pipe buffer, which is
    the condition that would surface ``ERROR_MORE_DATA`` if the read mode had
    not been flipped to bytes.
    """
    sock = tmp_path / "gateway.sock"
    transport.prepare_dir(sock)
    payload = b'{"pad":"' + b"x" * (64 * 1024) + b'"}\n'
    got: list[bytes] = []
    done = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        got.append(await reader.readuntil(b"\n"))
        done.set()
        writer.close()

    def on_connect(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        asyncio.create_task(handle(r, w))

    server = await transport.serve(sock, on_connect, limit=1 << 20)
    try:
        reader, writer = await transport.connect(sock, limit=1 << 20)
        try:
            writer.write(payload)
            await writer.drain()
            await asyncio.wait_for(done.wait(), timeout=30)
        finally:
            writer.close()
    finally:
        server.close()
        await server.wait_closed()
    assert got == [payload]


@pytest.mark.skipif(not pc.IS_WINDOWS, reason="exercises the named-pipe branch")
@pytest.mark.asyncio
async def test_windows_probe_live_tracks_the_pipe(tmp_path: Path) -> None:
    sock = tmp_path / "gateway.sock"
    transport.prepare_dir(sock)
    assert transport.probe_live(sock) is False
    server = await transport.serve(sock, _close_immediately, limit=1 << 16)
    try:
        assert transport.probe_live(sock) is True
        # endpoint_exists must not fall back to a filesystem check here: the
        # pipe has no directory entry, so exists() would always be False.
        assert transport.endpoint_exists(sock) is True
        assert not sock.exists()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.skipif(not pc.IS_WINDOWS, reason="Windows DACL")
@pytest.mark.asyncio
async def test_windows_pipe_dacl_excludes_everyone(tmp_path: Path) -> None:
    """The reason the factory replaces asyncio's method rather than patching up
    after it: the descriptor must never have been the permissive default.

    ``WD`` is Everyone and ``AN`` is Anonymous Logon in SDDL; the measured
    default descriptor grants both FILE_GENERIC_READ.
    """
    import ctypes
    from ctypes import wintypes

    sock = tmp_path / "gateway.sock"
    transport.prepare_dir(sock)
    address = transport.resolve_address(sock)
    handle = transport._create_server_pipe(address, first=True)
    try:
        advapi32: Any = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        psd = ctypes.c_void_p()
        rc = advapi32.GetSecurityInfo(
            wintypes.HANDLE(handle),
            ctypes.c_int(6),  # SE_KERNEL_OBJECT
            wintypes.DWORD(0x4),  # DACL_SECURITY_INFORMATION
            None,
            None,
            None,
            None,
            ctypes.byref(psd),
        )
        assert rc == 0, f"GetSecurityInfo rc={rc}"
        try:
            out = wintypes.LPWSTR()
            size = wintypes.ULONG()
            ok = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                psd,
                wintypes.DWORD(1),
                wintypes.DWORD(0x4),
                ctypes.byref(out),
                ctypes.byref(size),
            )
            assert ok, f"SDDL conversion failed: {ctypes.get_last_error()}"  # type: ignore[attr-defined]
            sddl = out.value or ""
            kernel32.LocalFree(out)
        finally:
            kernel32.LocalFree(psd)
    finally:
        import _winapi

        _winapi.CloseHandle(handle)

    assert ";;;WD)" not in sddl, f"Everyone is in the pipe DACL: {sddl}"
    assert ";;;AN)" not in sddl, f"Anonymous Logon is in the pipe DACL: {sddl}"

    # Compare against the SDDL Windows itself produces for "protected, full
    # access to exactly us", rather than looking for our raw SID in the string.
    # SDDL abbreviates well-known principals to two-letter aliases, so on a
    # runner whose account is one of them the descriptor reads
    # "D:P(A;;FA;;;LA)" and a substring check for "S-1-5-21-...-500" fails
    # against a DACL that is in fact exactly right. Round-tripping the expected
    # descriptor through the same converter normalises both sides, and pins the
    # whole descriptor instead of only the presence of one ACE.
    ours = pc.current_user_sid()
    assert ours, "own SID unavailable"
    expected = _normalize_sddl(f"D:P(A;;FA;;;{ours})")
    assert sddl == expected, f"pipe DACL {sddl} != expected {expected}"


def _normalize_sddl(sddl: str) -> str:
    """Round-trip an SDDL string through Win32 so aliasing matches what the OS
    emits when reading a descriptor back."""
    import ctypes
    from ctypes import wintypes

    advapi32: Any = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    psd = ctypes.c_void_p()
    ok = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        ctypes.c_wchar_p(sddl), wintypes.DWORD(1), ctypes.byref(psd), None
    )
    assert ok, f"SDDL parse failed for {sddl!r}: {ctypes.get_last_error()}"  # type: ignore[attr-defined]
    try:
        out = wintypes.LPWSTR()
        size = wintypes.ULONG()
        ok = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            psd,
            wintypes.DWORD(1),
            wintypes.DWORD(0x4),  # DACL_SECURITY_INFORMATION
            ctypes.byref(out),
            ctypes.byref(size),
        )
        assert ok, f"SDDL re-emit failed: {ctypes.get_last_error()}"  # type: ignore[attr-defined]
        try:
            return out.value or ""
        finally:
            kernel32.LocalFree(out)
    finally:
        kernel32.LocalFree(psd)


@pytest.mark.skipif(pc.IS_WINDOWS, reason="exercises the AF_UNIX branch")
@pytest.mark.asyncio
async def test_wait_closed_returns_once_connections_are_cancelled(
    sock_dir: Path,
) -> None:
    """Pins the shutdown ordering gatewayd depends on.

    Since Python 3.12 ``asyncio.Server.wait_closed()`` waits for every accepted
    connection to complete, so ``close()`` -> ``wait_closed()`` -> drain hangs for
    as long as one stub stays connected. The daemon therefore closes, then
    drains and cancels, and only then awaits. This test fails if that order is
    ever reversed back.

    The "it would have blocked" half only holds on 3.12+; on 3.10
    ``wait_closed()`` returns immediately regardless, so the reordering is a
    no-op there and only the positive assertion is checked.
    """
    sock = sock_dir / "gateway.sock"
    transport.prepare_dir(sock)
    handlers: list[asyncio.Task[None]] = []
    started = asyncio.Event()

    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        started.set()
        try:
            await asyncio.sleep(300)  # a stub that is still attached
        finally:
            writer.close()

    def on_connect(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        handlers.append(asyncio.create_task(hold(r, w)))

    server = await transport.serve(sock, on_connect, limit=1 << 16)
    reader, writer = await transport.connect(sock)
    await asyncio.wait_for(started.wait(), timeout=5)

    server.close()
    if sys.version_info >= (3, 12):
        # Awaiting here first is the bug: prove it would block. Gated because
        # the semantics are version-dependent -- measured blocking on 3.12.13,
        # and CI showed 3.10 returning immediately, which matches the CPython
        # change that made wait_closed() await accepted connections landing in
        # 3.12.0. On 3.10 the reordering is a harmless no-op, so only the
        # positive assertion below applies there.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(server.wait_closed()), timeout=0.5)

    for task in handlers:
        task.cancel()
    await asyncio.gather(*handlers, return_exceptions=True)
    writer.close()
    # With the connections gone it returns promptly, which is why the daemon
    # drains and cancels before awaiting.
    await asyncio.wait_for(server.wait_closed(), timeout=5)


def test_invalid_handle_constant_is_the_unsigned_pointer_form() -> None:
    """Guards a 64-bit comparison bug that a literal -1 would reintroduce.

    ``CreateNamedPipeW``'s restype is ``wintypes.HANDLE``, i.e. ``c_void_p``, so
    ctypes hands back the UNSIGNED pointer value. ``INVALID_HANDLE_VALUE`` is
    ``(HANDLE)-1``, which arrives as 18446744073709551615 on 64-bit -- a literal
    ``-1`` never compares equal, so a failed pipe creation would be accepted as a
    valid handle. Architecture-derived rather than OS-derived, so this is
    checkable off Windows.
    """
    import ctypes

    width = ctypes.sizeof(ctypes.c_void_p) * 8
    assert transport._INVALID_HANDLE_VALUE == 2**width - 1
    assert transport._INVALID_HANDLE_VALUE != -1


def test_null_handle_returns_as_none_from_a_pointer_restype() -> None:
    """The other half of the same check: a NULL return is ``None``, not ``0``,
    so the failure test has to accept both shapes."""
    import ctypes

    assert ctypes.c_void_p(0).value is None


# --- Windows dispatch reachable from Linux -----------------------------------


def test_probe_live_treats_a_busy_pipe_as_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server with no free instance right now is still a server.

    Mapping either busy code to False would let a second gatewayd conclude the
    endpoint is dead and bind over a live one.
    """
    monkeypatch.setattr(pc, "IS_WINDOWS", True)

    for winerror in (transport._ERROR_SEM_TIMEOUT, transport._ERROR_PIPE_BUSY):

        def _busy(_address: str, _timeout: int, code: int = winerror) -> None:
            exc = OSError("busy")
            exc.winerror = code  # type: ignore[attr-defined]
            raise exc

        monkeypatch.setattr(
            transport, "_winapi", type("W", (), {"WaitNamedPipe": staticmethod(_busy)})
        )
        assert transport.probe_live("C:/state/gateway.sock") is True


def test_probe_live_reports_an_unknown_error_as_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pc, "IS_WINDOWS", True)

    def _other(_address: str, _timeout: int) -> None:
        exc = OSError("nope")
        exc.winerror = 5  # ERROR_ACCESS_DENIED  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(
        transport, "_winapi", type("W", (), {"WaitNamedPipe": staticmethod(_other)})
    )
    assert transport.probe_live("C:/state/gateway.sock") is False


@pytest.mark.asyncio
async def test_serve_rejects_a_loop_without_pipe_support(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Running under SelectorEventLoop must say so, not fail as an
    AttributeError deep inside the bind.

    The absent ``start_serving_pipe`` is simulated rather than obtained by
    running under a real SelectorEventLoop: on Windows the loop under test IS a
    proactor loop, so asserting on the ambient loop passed on Linux (no such
    attribute) and vacuously failed to raise on Windows -- the one platform the
    guard exists for.
    """
    monkeypatch.setattr(pc, "IS_WINDOWS", True)

    class _NoPipeLoop:
        """A loop object with everything except ``start_serving_pipe``."""

    monkeypatch.setattr(transport.asyncio, "get_running_loop", lambda: _NoPipeLoop())

    async def _cb(_r: Any, _w: Any) -> None:  # pragma: no cover - never called
        raise AssertionError("must not accept")

    with pytest.raises(RuntimeError, match="ProactorEventLoop"):
        await transport.serve(tmp_path / "gateway.sock", _cb, limit=1 << 16)


def test_installing_the_pipe_factory_twice_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard exists so a second serve() cannot stack a replacement on top of
    an already-replaced method."""
    if not pc.IS_WINDOWS:
        pytest.skip("touches asyncio.windows_events")
    from asyncio import windows_events

    monkeypatch.setattr(transport, "_pipe_factory_installed", False)
    transport._install_pipe_factory()
    first = windows_events.PipeServer._server_pipe_handle
    transport._install_pipe_factory()
    assert windows_events.PipeServer._server_pipe_handle is first


# --- prepare_dir must not run on the event loop -------------------------------

# ``prepare_dir`` -> ``platform_compat.make_owner_only_dir`` shells out to
# ``icacls`` on Windows with a multi-second timeout. Both call sites are
# coroutines, so an inline call stalls the loop it runs on -- for the manager
# that is the live gateway's loop (a dashboard toggle freezes chat turns and the
# liveness heartbeat), and for the daemon it is the loop already serving its
# signal handlers.
#
# These assert the observable property (did it run on a thread with no running
# loop) rather than "was asyncio.to_thread called", so they stay honest if the
# offload mechanism changes. They are platform-independent: the offload is
# unconditional, and only the work inside it is Windows-specific.


class _LoopProbe:
    """Records whether it was invoked on a thread with a running event loop."""

    def __init__(self) -> None:
        self.calls = 0
        self.on_event_loop: bool | None = None

    def __call__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls += 1
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No loop in this thread -> it was offloaded.
            self.on_event_loop = False
        else:
            self.on_event_loop = True


@pytest.mark.asyncio
async def test_manager_offloads_prepare_dir_from_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.mcp_gateway import manager as mgr

    probe = _LoopProbe()
    monkeypatch.setattr(mgr.transport, "prepare_dir", probe)

    spec = MagicMock()
    spec.socket_path = tmp_path / "gateway.sock"
    manager = mgr.GatewayManager(spec)

    # Drive _start_locked as far as prepare_dir, then stop: no incumbent to
    # adopt, nothing stale to clear, and a spawn that fails is swallowed into a
    # False return.
    monkeypatch.setattr(manager, "_ping_once", AsyncMock(return_value=False))
    monkeypatch.setattr(manager, "_clear_stale_socket", AsyncMock(return_value=None))
    monkeypatch.setattr(
        manager, "_spawn_once", AsyncMock(side_effect=RuntimeError("stop here"))
    )

    assert await manager._start_locked() is False
    assert probe.calls == 1
    assert probe.on_event_loop is False, "prepare_dir ran inline on the gateway loop"


@pytest.mark.asyncio
async def test_gatewayd_offloads_prepare_dir_from_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.mcp_gateway import gatewayd as gw

    probe = _LoopProbe()
    monkeypatch.setattr(gw.transport, "prepare_dir", probe)
    # Lose the singleton election immediately after prepare_dir so the daemon
    # returns without binding anything.
    monkeypatch.setattr(gw.transport, "acquire_singleton_lock", lambda _p: None)

    await gw.run_gatewayd(
        socket_path=tmp_path / "gateway.sock",
        max_backends=1,
        idle_timeout_secs=60,
        stop_event=asyncio.Event(),
    )

    assert probe.calls == 1
    assert probe.on_event_loop is False, "prepare_dir ran inline on the daemon loop"


# --- connect() honours the server principal verdict ---------------------------


class _FakePipe:
    handle = 7


class _FakeTransport:
    """Minimal stand-in for a proactor duplex pipe transport."""

    def __init__(self) -> None:
        self.closed = False

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return _FakePipe() if name == "pipe" else default

    def close(self) -> None:
        self.closed = True

    # StreamWriter touches these during construction / close.
    def is_closing(self) -> bool:
        return self.closed

    def set_write_buffer_limits(self, *_a: Any, **_kw: Any) -> None:
        return None


def _install_fake_windows_connect(monkeypatch: pytest.MonkeyPatch) -> _FakeTransport:
    """Make ``connect`` take its Windows branch against a fake pipe.

    The REAL running loop is used with only ``create_pipe_connection`` replaced --
    ``StreamReader`` and ``StreamReaderProtocol`` reach back into the loop for
    ``get_debug`` and friends, so a hand-rolled loop stub would be testing the
    stub rather than ``connect``.
    """
    transport_obj = _FakeTransport()

    async def _fake_create_pipe_connection(
        protocol_factory: Any, _address: str
    ) -> tuple[Any, Any]:
        return transport_obj, protocol_factory()

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "create_pipe_connection", _fake_create_pipe_connection, raising=False
    )
    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    # The Windows-only import block binds these to None off Windows. Restore the
    # real ones -- asyncio.streams is cross-platform, and SetNamedPipeHandleState
    # is a genuine Win32 call that has to be neutralised here.
    from asyncio import streams as _real_streams

    monkeypatch.setattr(transport, "_asyncio_streams", _real_streams)
    monkeypatch.setattr(
        transport,
        "_winapi",
        type("_W", (), {"SetNamedPipeHandleState": staticmethod(lambda *a: None)})(),
    )
    return transport_obj


@pytest.mark.asyncio
async def test_connect_refuses_a_pipe_whose_server_is_not_us(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A squatted pipe must be refused BEFORE the caller can write a register or
    claim frame, since those carry session keys.

    ``ConnectionRefusedError`` is an ``OSError``, the shape every connect call
    site already treats as "gateway unreachable" -- so the stub degrades to a
    per-session MCP server instead of trusting an unattributable endpoint.
    """
    transport_obj = _install_fake_windows_connect(monkeypatch)
    monkeypatch.setattr(
        transport.socketsec,
        "check_server_is_self",
        lambda _w: transport.socketsec.PeerCredResult.MISMATCH,
    )

    with pytest.raises(ConnectionRefusedError, match="server principal not confirmed"):
        await transport.connect(tmp_path / "gateway.sock")
    assert transport_obj.closed, "the refused connection must be closed, not leaked"


@pytest.mark.asyncio
async def test_connect_returns_the_stream_when_the_server_is_us(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Control for the test above -- without it, a connect() that always raised
    would still satisfy the refusal assertion."""
    _install_fake_windows_connect(monkeypatch)
    monkeypatch.setattr(
        transport.socketsec,
        "check_server_is_self",
        lambda _w: transport.socketsec.PeerCredResult.MATCH,
    )

    reader, writer = await transport.connect(tmp_path / "gateway.sock")
    assert isinstance(reader, asyncio.StreamReader)
    assert writer is not None
