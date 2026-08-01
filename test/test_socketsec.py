"""Tests for the gateway's peer-identity and endpoint-permission primitives.

These had no direct coverage: every existing gateway test stubbed them out to
reach the code behind them, so the primitives themselves -- the deny-by-default
authorization decisions -- were only exercised transitively, and never on their
deny paths. This module covers them directly, including the platform dispatch
that cannot be reached from Linux.
"""

from __future__ import annotations

import os
import socket
import struct
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.mcp_gateway import socketsec
from kiro_crew.mcp_gateway.socketsec import PeerCredResult

# --- socket_owner_only -------------------------------------------------------


@pytest.mark.skipif(pc.IS_WINDOWS, reason="POSIX mode bits")
@pytest.mark.parametrize(
    "mode,expected",
    [
        (0o600, True),
        (0o700, True),
        (0o640, False),  # group read
        (0o604, False),  # other read
        (0o660, False),
        (0o666, False),
    ],
)
def test_socket_owner_only_reads_the_mode_bits(
    tmp_path: Path, mode: int, expected: bool
) -> None:
    p = tmp_path / "gateway.sock"
    p.write_text("")
    p.chmod(mode)
    assert socketsec.socket_owner_only(p) is expected


def test_socket_owner_only_denies_a_missing_endpoint(tmp_path: Path) -> None:
    """Fail closed: a caller uses this to decide whether to admit a connection
    it cannot otherwise verify, so 'not there' must not read as 'fine'."""
    assert socketsec.socket_owner_only(tmp_path / "absent.sock") is False


# --- get_peer_pid ------------------------------------------------------------


@pytest.mark.skipif(not socketsec.PEER_IDENTITY_SUPPORTED, reason="no mechanism here")
@pytest.mark.skipif(pc.IS_WINDOWS, reason="socketpair is the POSIX path")
def test_get_peer_pid_reads_our_own_pid_over_a_socketpair() -> None:
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert socketsec.get_peer_pid(a) == os.getpid()
    finally:
        a.close()
        b.close()


@pytest.mark.skipif(pc.IS_WINDOWS, reason="AF_INET guard is the POSIX path")
def test_get_peer_pid_refuses_a_non_unix_socket() -> None:
    """The peer credentials of a TCP socket mean nothing here, and reading them
    would be the mistake a loopback transport invites."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        assert socketsec.get_peer_pid(s) is None
    finally:
        s.close()


def test_get_peer_pid_returns_none_without_a_socket() -> None:
    assert socketsec.get_peer_pid(object()) is None


@pytest.mark.skipif(pc.IS_WINDOWS, reason="patches the POSIX dispatch")
def test_get_peer_pid_macos_branch_uses_local_peerpid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The macOS path is additive -- it can only supply a PID where there was
    none -- so it is wired even though there is no macOS CI job to run it
    against. This exercises the branch from Linux."""
    monkeypatch.setattr(socketsec, "_SO_PEERCRED", None)
    monkeypatch.setattr(pc, "IS_MACOS", True)

    class _FakeSock:
        family = socket.AF_UNIX

        def getsockopt(self, level: int, optname: int, buflen: int) -> bytes:
            assert (level, optname) == (socketsec._SOL_LOCAL, socketsec._LOCAL_PEERPID)
            return struct.pack("@i", 4321)

    assert socketsec.get_peer_pid(_FakeSock()) == 4321


@pytest.mark.skipif(pc.IS_WINDOWS, reason="patches the POSIX dispatch")
def test_get_peer_pid_macos_branch_degrades_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socketsec, "_SO_PEERCRED", None)
    monkeypatch.setattr(pc, "IS_MACOS", True)

    class _FailingSock:
        family = socket.AF_UNIX

        def getsockopt(self, level: int, optname: int, buflen: int) -> bytes:
            raise OSError("unsupported")

    assert socketsec.get_peer_pid(_FailingSock()) is None


@pytest.mark.skipif(pc.IS_WINDOWS, reason="patches the POSIX dispatch")
def test_get_peer_pid_is_none_where_no_mechanism_is_wired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A POSIX platform that is neither Linux nor macOS gets no PID rather than
    a guess."""
    monkeypatch.setattr(socketsec, "_SO_PEERCRED", None)
    monkeypatch.setattr(pc, "IS_MACOS", False)

    class _Sock:
        family = socket.AF_UNIX

        def getsockopt(self, *_a: Any) -> bytes:  # pragma: no cover - not reached
            raise AssertionError("should not be consulted")

    assert socketsec.get_peer_pid(_Sock()) is None


# --- check_peer_is_self ------------------------------------------------------


@pytest.mark.skipif(pc.IS_WINDOWS, reason="socketpair is the POSIX path")
@pytest.mark.skipif(not socketsec.PEER_IDENTITY_SUPPORTED, reason="no mechanism here")
def test_check_peer_is_self_matches_our_own_socket() -> None:
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert socketsec.check_peer_is_self(a) is PeerCredResult.MATCH
    finally:
        a.close()
        b.close()


@pytest.mark.skipif(pc.IS_WINDOWS, reason="socketpair is the POSIX path")
@pytest.mark.skipif(not socketsec.PEER_IDENTITY_SUPPORTED, reason="no mechanism here")
def test_check_peer_is_self_reports_mismatch_for_a_foreign_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MISMATCH is a positive finding, distinct from UNVERIFIABLE: the caller
    must reject either way, but only one of them means the OS answered."""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    # Capture the real uid BEFORE patching: socketsec.os is this module's own
    # os, so a lambda that called os.getuid() would recurse into the stub.
    foreign = os.getuid() + 1
    monkeypatch.setattr(socketsec.os, "getuid", lambda: foreign)
    try:
        assert socketsec.check_peer_is_self(a) is PeerCredResult.MISMATCH
    finally:
        a.close()
        b.close()


def test_check_peer_is_self_is_unverifiable_without_a_socket() -> None:
    """Never MATCH by omission -- the whole point of the tri-state."""
    assert socketsec.check_peer_is_self(object()) is PeerCredResult.UNVERIFIABLE


@pytest.mark.skipif(pc.IS_WINDOWS, reason="AF_INET guard is the POSIX path")
def test_check_peer_is_self_is_unverifiable_for_a_non_unix_socket() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        assert socketsec.check_peer_is_self(s) is PeerCredResult.UNVERIFIABLE
    finally:
        s.close()


@pytest.mark.skipif(pc.IS_WINDOWS, reason="patches the POSIX dispatch")
def test_check_peer_is_self_is_unverifiable_on_macos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS deliberately has no principal check.

    A wrong one would return MISMATCH and lock every macOS user out of their own
    gateway, and there is no macOS CI job to catch that. UNVERIFIABLE routes
    admission to the filesystem gate, which does work there.
    """
    monkeypatch.setattr(socketsec, "_SO_PEERCRED", None)
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert socketsec.check_peer_is_self(a) is PeerCredResult.UNVERIFIABLE
    finally:
        a.close()
        b.close()


# --- Platform capability flag ------------------------------------------------


def test_peer_identity_supported_matches_the_platform() -> None:
    if pc.IS_WINDOWS:
        assert socketsec.PEER_IDENTITY_SUPPORTED is True
    elif pc.IS_MACOS:
        assert socketsec.PEER_IDENTITY_SUPPORTED is False
    else:
        assert socketsec.PEER_IDENTITY_SUPPORTED is True


# --- Handle / socket resolution ----------------------------------------------


@pytest.mark.skipif(pc.IS_WINDOWS, reason="AF_UNIX is the POSIX socket shape")
def test_resolve_socket_accepts_a_transport_socket_lookalike() -> None:
    """asyncio hands back a TransportSocket, not a socket.socket. A strict
    isinstance check here once degraded every real connection to UNVERIFIABLE,
    which is why resolution is duck-typed."""

    class _TransportSocketish:
        family = socket.AF_UNIX

        def getsockopt(self, *_a: Any) -> bytes:  # pragma: no cover
            return b""

    class _Writer:
        def get_extra_info(self, key: str) -> Any:
            return _TransportSocketish() if key == "socket" else None

    assert socketsec._resolve_socket(_Writer()) is not None


def test_resolve_socket_returns_none_for_an_unrelated_object() -> None:
    assert socketsec._resolve_socket(object()) is None
    assert socketsec._resolve_socket(None) is None


@pytest.mark.parametrize("handle", [7, 12345])
def test_resolve_pipe_handle_accepts_a_raw_int(handle: int) -> None:
    assert socketsec._resolve_pipe_handle(handle) == handle


def test_resolve_pipe_handle_accepts_a_pipe_handle_object() -> None:
    class _PipeHandle:
        handle = 99

    assert socketsec._resolve_pipe_handle(_PipeHandle()) == 99


def test_resolve_pipe_handle_reads_the_public_extra_info_seam() -> None:
    """``get_extra_info("pipe")`` is why Windows peer identity costs no
    private-API surface."""

    class _PipeHandle:
        handle = 4242

    class _Writer:
        def get_extra_info(self, key: str) -> Any:
            return _PipeHandle() if key == "pipe" else None

    assert socketsec._resolve_pipe_handle(_Writer()) == 4242


def test_resolve_pipe_handle_returns_none_when_unreachable() -> None:
    class _Writer:
        def get_extra_info(self, key: str) -> Any:
            return None

    assert socketsec._resolve_pipe_handle(_Writer()) is None
    assert socketsec._resolve_pipe_handle(object()) is None


# --- Windows principal check --------------------------------------------------


def test_windows_check_is_unverifiable_without_a_pipe_handle() -> None:
    assert (
        socketsec._windows_check_peer_is_self(object()) is PeerCredResult.UNVERIFIABLE
    )


def test_windows_check_is_unverifiable_without_our_own_sid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to compare against means no answer, not a permissive one."""
    monkeypatch.setattr(pc, "current_user_sid", lambda: None)
    assert socketsec._windows_check_peer_is_self(7) is PeerCredResult.UNVERIFIABLE


@pytest.mark.skipif(not pc.IS_WINDOWS, reason="exercises the real Win32 calls")
def test_windows_check_matches_a_pipe_we_connected_to_ourselves(
    tmp_path: Path,
) -> None:
    """End-to-end on Windows: impersonate the client of a real pipe and confirm
    the SID comparison lands on MATCH for a peer that is us."""
    import asyncio

    from kiro_crew.mcp_gateway import transport

    sock = tmp_path / "gateway.sock"
    transport.prepare_dir(sock)
    outcome: list[PeerCredResult] = []
    done = asyncio.Event()

    async def run() -> None:
        def on_connect(r: Any, w: Any) -> None:
            outcome.append(socketsec.check_peer_is_self(w))
            w.close()
            done.set()

        server = await transport.serve(sock, on_connect, limit=1 << 16)
        try:
            reader, writer = await transport.connect(sock)
            try:
                await asyncio.wait_for(done.wait(), timeout=30)
            finally:
                writer.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
    assert outcome == [PeerCredResult.MATCH]


# The Windows check reads the peer PROCESS's token rather than impersonating the
# pipe client, so these need no ctypes fakes -- only the two seams it consults.


def _win_check(
    monkeypatch: pytest.MonkeyPatch,
    *,
    our_sid: str | None,
    peer_pid: int | None,
    peer_sid: str | None,
) -> PeerCredResult:
    monkeypatch.setattr(pc, "current_user_sid", lambda: our_sid)
    monkeypatch.setattr(socketsec, "_windows_peer_pid", lambda _h: peer_pid)
    monkeypatch.setattr(pc, "process_owner_sid", lambda _p: peer_sid)
    return socketsec._windows_check_peer_is_self(7)


def test_windows_check_matches_when_peer_process_is_owned_by_us(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "S-1-5-21-1-2-3-1001"
    assert (
        _win_check(monkeypatch, our_sid=sid, peer_pid=4321, peer_sid=sid)
        is PeerCredResult.MATCH
    )


def test_windows_check_mismatches_a_foreign_sid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        _win_check(
            monkeypatch,
            our_sid="S-1-5-21-1-2-3-1001",
            peer_pid=4321,
            peer_sid="S-1-5-21-9-9-9-1002",
        )
        is PeerCredResult.MISMATCH
    )


def test_windows_check_is_unverifiable_without_a_peer_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never MATCH on a missing PID -- the admission gate treats anything short
    of MATCH as a rejection, so degrading here must not become admitting."""
    assert (
        _win_check(
            monkeypatch,
            our_sid="S-1-5-21-1-2-3-1001",
            peer_pid=None,
            peer_sid="S-1-5-21-1-2-3-1001",
        )
        is PeerCredResult.UNVERIFIABLE
    )


def test_windows_check_is_unverifiable_without_a_peer_owner_sid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``process_owner_sid`` returns None on any failure (and always on POSIX);
    that must not be read as agreement with our own SID."""
    assert (
        _win_check(
            monkeypatch,
            our_sid="S-1-5-21-1-2-3-1001",
            peer_pid=4321,
            peer_sid=None,
        )
        is PeerCredResult.UNVERIFIABLE
    )


def test_windows_check_does_not_impersonate_the_pipe_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Impersonation cannot work at connection-admission time.

    Per Microsoft's documentation ``ImpersonateNamedPipeClient`` adopts the
    context of "the last message read from the pipe"; the gate runs before the
    Register frame is read, so there is no such message and the call fails.
    Because the gate is deny-by-default, that rejected every Windows connection
    -- the feature was fully non-functional while appearing merely strict. It
    also borrowed the peer's token onto the event loop thread. Both are gone, so
    fail loudly if either call ever returns.
    """
    import ctypes as _real_ctypes

    forbidden = ("ImpersonateNamedPipeClient", "RevertToSelf", "OpenThreadToken")

    class _Tripwire:
        def __getattr__(self, name: str) -> object:
            if name in forbidden:
                raise AssertionError(f"{name} must not be used by the peer check")
            return getattr(_real_ctypes, name)

        def WinDLL(self, *_a: object, **_kw: object) -> object:  # noqa: N802
            raise AssertionError("the peer check must not load a DLL directly")

        def get_last_error(self) -> int:
            return 0

    monkeypatch.setattr(socketsec, "_ct", _Tripwire())
    sid = "S-1-5-21-1-2-3-1001"
    assert (
        _win_check(monkeypatch, our_sid=sid, peer_pid=4321, peer_sid=sid)
        is PeerCredResult.MATCH
    )


# --- Client-side server principal check ---------------------------------------


def _srv_check(
    monkeypatch: pytest.MonkeyPatch,
    *,
    our_sid: str | None,
    server_pid: int | None,
    server_sid: str | None,
) -> PeerCredResult:
    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    monkeypatch.setattr(pc, "current_user_sid", lambda: our_sid)
    monkeypatch.setattr(socketsec, "_windows_server_pid", lambda _h: server_pid)
    monkeypatch.setattr(pc, "process_owner_sid", lambda _p: server_sid)
    return socketsec.check_server_is_self(7)


def test_server_check_is_a_noop_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX needs no client-side check: the endpoint sits in a 0700 directory, so
    no other principal can create a socket at that path. Returning MATCH keeps
    ``connect`` free of a platform branch."""
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    assert socketsec.check_server_is_self(7) is PeerCredResult.MATCH


def test_server_check_matches_a_pipe_owned_by_us(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "S-1-5-21-1-2-3-1001"
    assert (
        _srv_check(monkeypatch, our_sid=sid, server_pid=99, server_sid=sid)
        is PeerCredResult.MATCH
    )


def test_server_check_rejects_a_squatted_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attack this exists for: another local principal pre-created the pipe
    name and is waiting to receive our register/claim frames."""
    assert (
        _srv_check(
            monkeypatch,
            our_sid="S-1-5-21-1-2-3-1001",
            server_pid=99,
            server_sid="S-1-5-21-9-9-9-1002",
        )
        is PeerCredResult.MISMATCH
    )


@pytest.mark.parametrize(
    "server_pid,server_sid",
    [(None, "S-1-5-21-1-2-3-1001"), (99, None)],
)
def test_server_check_never_matches_on_an_incomplete_lookup(
    monkeypatch: pytest.MonkeyPatch, server_pid: int | None, server_sid: str | None
) -> None:
    """Anything short of a positive confirmation must not be read as agreement --
    ``connect`` refuses on non-MATCH, which degrades to a per-session server."""
    assert (
        _srv_check(
            monkeypatch,
            our_sid="S-1-5-21-1-2-3-1001",
            server_pid=server_pid,
            server_sid=server_sid,
        )
        is PeerCredResult.UNVERIFIABLE
    )
