"""``mcp_gateway.socketsec`` — the degrade-to-refusal paths on the POSIX build.

Every function here is deny-by-default, so the lines that matter most are the
ones that turn a FAILED lookup into ``UNVERIFIABLE`` / ``None`` rather than into
an accidental allow. Those are exactly the lines the existing suite leaves
unobserved:

* ``chmod_socket_0600`` — best-effort, so it must not raise on a real file;
* ``get_peer_pid`` when ``getsockopt`` itself fails, and its Windows dispatch;
* ``check_peer_is_self`` when ``getsockopt`` fails, and the macOS dispatch it
  takes when the platform has no ``SO_PEERCRED``;
* ``check_server_is_self``'s two early refusals (no pipe handle, no own SID) —
  reached here by faking the Windows flag, since the whole function is a
  Windows-only squatter defence;
* ``_resolve_pipe_handle`` resolving an integer published under
  ``get_extra_info("pipe")``, the shape asyncio's proactor transport uses.

The Win32 ctypes bodies (``_windows_peer_pid`` / ``_windows_server_pid``) are NOT
covered: they open with ``from ctypes import wintypes``, which does not import on
a POSIX host, so they are only reachable on a real Windows runner (where
``test_socketsec.py``'s ``skipif(not IS_WINDOWS)`` block exercises them).
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.mcp_gateway import socketsec

pytestmark = pytest.mark.skipif(pc.IS_WINDOWS, reason="exercises the POSIX build")

# Resolved through getattr because a class DEFAULT ARGUMENT is evaluated at import
# time, before `pytestmark` can skip anything: naming socket.AF_UNIX directly makes
# this module fail to collect on Windows, which has no AF_UNIX. The fallback value is
# never used -- every test here is skipped on Windows.
_AF_UNIX = getattr(socket, "AF_UNIX", -1)


class _FailingSock:
    """A socket-shaped object whose ``getsockopt`` fails the way a kernel that
    does not populate peer credentials would."""

    def __init__(self, family: int = _AF_UNIX) -> None:
        self.family = family

    def getsockopt(self, *_a: Any, **_kw: Any) -> bytes:
        raise OSError(22, "Invalid argument")


class _XucredSock:
    """A socket-shaped object returning a caller-supplied raw option buffer."""

    def __init__(self, raw: bytes, family: int = _AF_UNIX) -> None:
        self.family = family
        self._raw = raw

    def getsockopt(self, *_a: Any, **_kw: Any) -> bytes:
        return self._raw


class _PipeCarrier:
    """asyncio's proactor transport publishes the pipe under ``get_extra_info``."""

    def __init__(self, pipe: Any) -> None:
        self._pipe = pipe

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self._pipe if name == "pipe" else default


class TestChmodSocket:
    def test_tightening_a_real_socket_file_does_not_raise(self, tmp_path: Path) -> None:
        target = tmp_path / "gateway.sock"
        target.write_bytes(b"")
        socketsec.chmod_socket_0600(target)
        assert target.stat().st_mode & 0o777 == 0o600

    def test_a_missing_path_is_swallowed(self, tmp_path: Path) -> None:
        """Best-effort by contract: the 0700 home directory is the primary gate,
        so a chmod failure must not abort daemon startup."""
        socketsec.chmod_socket_0600(tmp_path / "not-there.sock")


class TestSocketOwnerOnly:
    def test_a_group_readable_socket_is_denied(self, tmp_path: Path) -> None:
        target = tmp_path / "loose.sock"
        target.write_bytes(b"")
        target.chmod(0o640)
        assert socketsec.socket_owner_only(target) is False

    def test_a_missing_socket_is_denied(self, tmp_path: Path) -> None:
        assert socketsec.socket_owner_only(tmp_path / "absent.sock") is False

    def test_an_owner_only_socket_is_allowed(self, tmp_path: Path) -> None:
        target = tmp_path / "tight.sock"
        target.write_bytes(b"")
        target.chmod(0o600)
        assert socketsec.socket_owner_only(target) is True


class TestGetPeerPid:
    def test_a_getsockopt_failure_loses_the_pid_rather_than_the_connection(self) -> None:
        assert socketsec.get_peer_pid(_FailingSock()) is None

    def test_a_non_unix_socket_has_no_peer_pid(self) -> None:
        assert socketsec.get_peer_pid(_FailingSock(family=socket.AF_INET)) is None

    def test_an_object_with_no_socket_at_all(self) -> None:
        assert socketsec.get_peer_pid(object()) is None

    def test_the_windows_dispatch_goes_through_the_pipe_handle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(socketsec, "_windows_peer_pid", lambda handle: handle + 1)
        assert socketsec.get_peer_pid(41) == 42

    def test_the_windows_dispatch_refuses_without_a_handle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(
            socketsec,
            "_windows_peer_pid",
            lambda handle: pytest.fail("must not be called without a handle"),
        )
        assert socketsec.get_peer_pid(object()) is None


class TestCheckPeerIsSelf:
    def test_a_getsockopt_failure_is_unverifiable_not_mismatch(self) -> None:
        """UNVERIFIABLE is a policy question for the caller; MISMATCH is a hard
        refusal, so a failed syscall must never be reported as one."""
        assert socketsec.check_peer_is_self(_FailingSock()) is socketsec.PeerCredResult.UNVERIFIABLE

    def test_no_underlying_socket_is_unverifiable(self) -> None:
        assert socketsec.check_peer_is_self(object()) is socketsec.PeerCredResult.UNVERIFIABLE

    def test_a_platform_with_no_mechanism_is_unverifiable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(socketsec, "_SO_PEERCRED", None)
        monkeypatch.setattr(pc, "IS_MACOS", False)
        sock_a, sock_b = socket.socketpair()
        try:
            assert socketsec.check_peer_is_self(sock_a) is socketsec.PeerCredResult.UNVERIFIABLE
        finally:
            sock_a.close()
            sock_b.close()

    def test_without_so_peercred_a_mac_takes_the_local_peercred_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dispatch, not the parser: an unknown ``cr_version`` degrades to
        UNVERIFIABLE, which proves the macOS branch ran."""
        monkeypatch.setattr(socketsec, "_SO_PEERCRED", None)
        monkeypatch.setattr(pc, "IS_MACOS", True)
        bad_version = (99).to_bytes(4, "little") + b"\x00" * (socketsec._XUCRED_SIZE - 4)
        assert (
            socketsec.check_peer_is_self(_XucredSock(bad_version))
            is socketsec.PeerCredResult.UNVERIFIABLE
        )

    def test_a_short_xucred_buffer_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(socketsec, "_SO_PEERCRED", None)
        monkeypatch.setattr(pc, "IS_MACOS", True)
        assert (
            socketsec.check_peer_is_self(_XucredSock(b"\x00\x00"))
            is socketsec.PeerCredResult.UNVERIFIABLE
        )

    def test_the_windows_dispatch_is_taken_when_the_flag_is_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(
            socketsec,
            "_windows_check_peer_is_self",
            lambda _t: socketsec.PeerCredResult.MISMATCH,
        )
        assert socketsec.check_peer_is_self(object()) is socketsec.PeerCredResult.MISMATCH


class TestCheckServerIsSelf:
    def test_posix_has_no_squatter_question_to_answer(self) -> None:
        """On POSIX the endpoint lives in a 0700 directory, so no other principal
        can create a socket at that path."""
        assert socketsec.check_server_is_self(object()) is socketsec.PeerCredResult.MATCH

    def test_no_pipe_handle_is_unverifiable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        assert socketsec.check_server_is_self(object()) is socketsec.PeerCredResult.UNVERIFIABLE

    def test_own_sid_unavailable_is_unverifiable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "current_user_sid", lambda: "")
        assert socketsec.check_server_is_self(1234) is socketsec.PeerCredResult.UNVERIFIABLE


class TestResolvePipeHandle:
    def test_an_integer_is_its_own_handle(self) -> None:
        assert socketsec._resolve_pipe_handle(77) == 77

    def test_an_object_exposing_handle(self) -> None:
        class _H:
            handle = 88

        assert socketsec._resolve_pipe_handle(_H()) == 88

    def test_an_integer_published_as_extra_info(self) -> None:
        assert socketsec._resolve_pipe_handle(_PipeCarrier(99)) == 99

    def test_a_pipe_object_published_as_extra_info(self) -> None:
        class _Pipe:
            handle = 111

        assert socketsec._resolve_pipe_handle(_PipeCarrier(_Pipe())) == 111

    def test_nothing_resolvable_is_none(self) -> None:
        assert socketsec._resolve_pipe_handle(_PipeCarrier(None)) is None
        assert socketsec._resolve_pipe_handle(object()) is None
