"""``sock_diag.listen_sockets`` and the Linux listener-inode fast path built on it."""

from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat, platform_compat_ports, sock_diag

linux_only = pytest.mark.skipif(
    not platform_compat.IS_LINUX, reason="NETLINK_SOCK_DIAG is Linux-only"
)


@pytest.fixture
def listener():
    """A real loopback LISTEN socket on an ephemeral port, closed after the test."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        yield sock.getsockname()[1], os.fstat(sock.fileno()).st_ino
    finally:
        sock.close()


@linux_only
def test_reports_a_real_loopback_listener_with_its_inode(listener):
    port, inode = listener
    rows = sock_diag.listen_sockets(port)
    if rows is None:
        pytest.skip("this kernel or sandbox refuses NETLINK_SOCK_DIAG")
    assert ("127.0.0.1", inode) in rows


@linux_only
def test_fast_path_and_the_proc_table_both_name_the_listener(listener):
    """The netlink answer and the ``/proc/net/tcp{,6}`` parse both carry its inode.

    ``/proc/self/..`` is the same directory as ``/proc`` but not equal to
    ``Path("/proc")``, so it takes the ``/proc`` read the fast path replaces.
    Membership, not equality: an unrelated host listener on the same port number
    in the other address family is in both answers too.
    """
    port, inode = listener
    fast = platform_compat_ports._linux_loopback_listener_inodes(port, Path("/proc"))
    slow = platform_compat_ports._linux_loopback_listener_inodes(port, Path("/proc/self/.."))
    assert str(inode) in fast
    assert str(inode) in slow


@linux_only
def test_this_process_owns_its_own_listener(listener):
    port, _inode = listener
    assert platform_compat.process_owns_loopback_listener(os.getpid(), port) is True


def _bound(family: int, address: str):
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((address, 0))
    except OSError as exc:
        sock.close()
        pytest.skip(f"cannot bind {address}: {exc}")
    sock.listen(1)
    return sock


@linux_only
@pytest.mark.parametrize(
    "family, address",
    [(socket.AF_INET, "127.0.0.2"), (socket.AF_INET6, "::1")],
    ids=["interface-address", "v6-loopback-only"],
)
@pytest.mark.parametrize("proc_root", [None, Path("/proc/self/..")], ids=["netlink", "proc"])
def test_a_gateway_bound_off_127_0_0_1_still_proves_its_port(family, address, proc_root):
    """A gateway on one interface address, or on ``::1`` alone, has no socket that
    answers 127.0.0.1. Boot's marker prune and sibling-port check read the proof as
    "that gateway is alive", so it must still hold for such a gateway."""
    from kiro_crew.cli_server import _gateway_owns_port

    sock = _bound(family, address)
    try:
        port = sock.getsockname()[1]
        assert platform_compat.process_owns_port_listener(os.getpid(), port, proc_root=proc_root)
        with (
            patch("kiro_crew.cli_server.run_marker.read_pid", return_value=os.getpid()),
            patch("kiro_crew.cli_server._is_kirocrew_process", return_value=True),
        ):
            assert _gateway_owns_port(port) is True
    finally:
        sock.close()


@linux_only
def test_another_pid_on_the_port_does_not_prove_it(listener):
    port, _inode = listener
    assert platform_compat.process_owns_port_listener(os.getppid(), port) is False


@linux_only
def test_falls_back_to_proc_when_netlink_is_refused(listener):
    port, inode = listener
    with patch.object(sock_diag.socket, "socket", side_effect=OSError("refused")):
        assert sock_diag.listen_sockets(port) is None
        found = platform_compat_ports._linux_loopback_listener_inodes(port, Path("/proc"))
    assert found is not None and str(inode) in found


class _TruncatedReply:
    """A netlink socket whose reply header claims more bytes than it carries."""

    def settimeout(self, _seconds):
        pass

    def sendall(self, _data):
        pass

    def recv(self, _size):
        return sock_diag._NLMSG_HEADER.pack(4096, sock_diag._SOCK_DIAG_BY_FAMILY, 0, 1, 0)

    def close(self):
        pass


def test_a_reply_that_does_not_parse_is_no_answer():
    with patch.object(sock_diag.socket, "socket", return_value=_TruncatedReply(), create=True):
        assert sock_diag.listen_sockets(6776) is None


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_an_impossible_port_is_no_answer(port):
    assert sock_diag.listen_sockets(port) is None


@pytest.mark.parametrize(
    ("family", "address", "text"),
    [
        (socket.AF_INET, "127.0.0.1", "127.0.0.1"),
        (socket.AF_INET6, "::", "::"),
        (socket.AF_INET6, "::ffff:127.0.0.1", "::ffff:127.0.0.1"),
    ],
)
def test_address_text_spells_a_mapped_address_the_same_on_every_python(family, address, text):
    raw = ipaddress.ip_address(address).packed.ljust(16, b"\0")
    assert sock_diag._address_text(family, raw) == text
