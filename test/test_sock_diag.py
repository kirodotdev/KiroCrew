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
def test_fast_path_matches_the_proc_table(listener):
    """The netlink answer and the ``/proc/net/tcp{,6}`` parse name the same inodes.

    ``/proc/self/..`` is the same directory as ``/proc`` but not equal to
    ``Path("/proc")``, so it takes the ``/proc`` read the fast path replaces.
    """
    port, inode = listener
    fast = platform_compat_ports._linux_loopback_listener_inodes(port, Path("/proc"))
    slow = platform_compat_ports._linux_loopback_listener_inodes(port, Path("/proc/self/.."))
    assert fast == slow == {str(inode)}


@linux_only
def test_this_process_owns_its_own_listener(listener):
    port, _inode = listener
    assert platform_compat.process_owns_loopback_listener(os.getpid(), port) is True


@linux_only
def test_falls_back_to_proc_when_netlink_is_refused(listener):
    port, inode = listener
    with patch.object(sock_diag.socket, "socket", side_effect=OSError("refused")):
        assert sock_diag.listen_sockets(port) is None
        found = platform_compat_ports._linux_loopback_listener_inodes(port, Path("/proc"))
    assert found == {str(inode)}


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
