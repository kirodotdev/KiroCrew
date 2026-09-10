"""Unit tests for the pure layer of the sandbox connect fence.

Everything here is syscall-free: BPF bytes, sockaddr decode, and the
fence verdict matrix. The launcher install path and the supervisor loop are
integration-tested on sandbox-capable Linux runners.
"""

from __future__ import annotations

import errno
import socket
import struct

import pytest

from kiro_crew.security import connect_fence as cf


def _sockaddr_in(port: int, ip: str) -> bytes:
    return struct.pack("<H", socket.AF_INET) + struct.pack("!H", port) + socket.inet_aton(ip)


def _sockaddr_in6(port: int, ip: str) -> bytes:
    return (
        struct.pack("<H", socket.AF_INET6)
        + struct.pack("!H", port)
        + b"\x00\x00\x00\x00"  # flowinfo
        + socket.inet_pton(socket.AF_INET6, ip)
    )


class TestBpfProgram:
    def test_known_arches_build(self) -> None:
        for machine in ("x86_64", "aarch64"):
            prog = cf.build_connect_notif_prog(machine)
            assert len(prog) == cf.BPF_INSN_COUNT * 8

    def test_unknown_arch_fails_closed_at_build(self) -> None:
        with pytest.raises(cf.FenceUnsupportedArch):
            cf.build_connect_notif_prog("riscv64")

    def test_program_ends_in_allow_and_notif_returns(self) -> None:
        prog = cf.build_connect_notif_prog("x86_64")
        allow = struct.unpack_from("<HBBI", prog, 5 * 8)
        enosys = struct.unpack_from("<HBBI", prog, 6 * 8)
        notif = struct.unpack_from("<HBBI", prog, 7 * 8)
        assert allow[3] == cf.SECCOMP_RET_ALLOW
        assert enosys[3] == cf.SECCOMP_RET_ERRNO | errno.ENOSYS
        assert notif[3] == cf.SECCOMP_RET_USER_NOTIF

    def test_x32_alias_space_refused(self) -> None:
        # The x32 ABI spells connect under bit-30 numbers no native equality
        # match sees; the program refuses the whole alias space with ENOSYS.
        prog = cf.build_connect_notif_prog("x86_64")
        jset = struct.unpack_from("<HBBI", prog, 3 * 8)
        assert jset[0] == cf._BPF_JMP | cf._BPF_JSET | cf._BPF_K
        assert jset[3] == 0x40000000


class TestParseSockaddr:
    def test_ipv4(self) -> None:
        target = cf.parse_sockaddr(_sockaddr_in(22, "127.0.0.1"))
        assert target is not None
        assert (target.family, str(target.address), target.port) == (
            socket.AF_INET,
            "127.0.0.1",
            22,
        )

    def test_ipv6(self) -> None:
        target = cf.parse_sockaddr(_sockaddr_in6(22, "::1"))
        assert target is not None
        assert (target.family, str(target.address), target.port) == (
            socket.AF_INET6,
            "::1",
            22,
        )

    def test_af_unix_is_not_subject_matter(self) -> None:
        raw = struct.pack("<H", socket.AF_UNIX) + b"/tmp/sock\x00"
        assert cf.parse_sockaddr(raw) is None

    def test_truncated_buffers_are_not_subject_matter(self) -> None:
        assert cf.parse_sockaddr(b"") is None
        assert cf.parse_sockaddr(b"\x02") is None
        assert cf.parse_sockaddr(_sockaddr_in(22, "127.0.0.1")[:6]) is None
        assert cf.parse_sockaddr(_sockaddr_in6(22, "::1")[:20]) is None


class TestFenceVerdict:
    SELF = frozenset({"192.0.2.10", "2001:db8::10"})

    def _target(self, raw: bytes) -> cf.ConnectTarget:
        target = cf.parse_sockaddr(raw)
        assert target is not None
        return target

    def test_loopback_v4_port_22_denied(self) -> None:
        assert cf.fence_verdict(self._target(_sockaddr_in(22, "127.0.0.1")), self.SELF)

    def test_loopback_v4_nonstandard_loopback_denied(self) -> None:
        assert cf.fence_verdict(self._target(_sockaddr_in(22, "127.8.9.10")), self.SELF)

    def test_loopback_v6_port_22_denied(self) -> None:
        assert cf.fence_verdict(self._target(_sockaddr_in6(22, "::1")), self.SELF)

    def test_self_adapter_address_denied(self) -> None:
        assert cf.fence_verdict(self._target(_sockaddr_in(22, "192.0.2.10")), self.SELF)

    def test_self_adapter_v6_denied(self) -> None:
        assert cf.fence_verdict(self._target(_sockaddr_in6(22, "2001:db8::10")), self.SELF)

    def test_v4_mapped_v6_folds_onto_v4_identity(self) -> None:
        assert cf.fence_verdict(
            self._target(_sockaddr_in6(22, "::ffff:192.0.2.10")), self.SELF
        )

    def test_unspecified_v4_port_22_denied(self) -> None:
        # The kernel delivers a connect to 0.0.0.0 at loopback: self by
        # another spelling, denied even with an empty swept set.
        assert cf.fence_verdict(self._target(_sockaddr_in(22, "0.0.0.0")), frozenset())

    def test_unspecified_v6_port_22_denied(self) -> None:
        assert cf.fence_verdict(self._target(_sockaddr_in6(22, "::")), frozenset())

    def test_unspecified_other_port_continues(self) -> None:
        assert not cf.fence_verdict(self._target(_sockaddr_in(443, "0.0.0.0")), self.SELF)

    def test_foreign_host_port_22_continues(self) -> None:
        assert not cf.fence_verdict(self._target(_sockaddr_in(22, "198.51.100.7")), self.SELF)

    def test_self_address_other_port_continues(self) -> None:
        assert not cf.fence_verdict(self._target(_sockaddr_in(443, "192.0.2.10")), self.SELF)

    def test_loopback_other_port_continues(self) -> None:
        assert not cf.fence_verdict(self._target(_sockaddr_in(8080, "127.0.0.1")), self.SELF)

    def test_none_target_continues(self) -> None:
        assert not cf.fence_verdict(None, self.SELF)


class TestSelfAddressStrings:
    def test_hostnames_dropped_ips_kept(self) -> None:
        got = cf.self_address_strings(["192.0.2.10", "devhost.example", "::1"])
        assert got == frozenset({"192.0.2.10", "::1"})

    def test_v4_mapped_folded(self) -> None:
        got = cf.self_address_strings(["::ffff:192.0.2.10"])
        assert got == frozenset({"192.0.2.10"})

    def test_empty_input_empty_set(self) -> None:
        assert cf.self_address_strings([]) == frozenset()


def _nlmsg(msg_type: int, payload: bytes) -> bytes:
    total = 16 + len(payload)
    hdr = struct.pack("<IHHII", total, msg_type, 0, 1, 0)
    pad = b"\x00" * ((-total) % 4)
    return hdr + payload + pad


def _rtm_newaddr(family: int, attr_type: int, addr_bytes: bytes) -> bytes:
    ifaddrmsg = struct.pack("BBBBI", family, 24, 0, 0, 2)
    attr = struct.pack("<HH", 4 + len(addr_bytes), attr_type) + addr_bytes
    attr += b"\x00" * ((-len(attr)) % 4)
    return _nlmsg(20, ifaddrmsg + attr)


class TestNetlinkDumpParser:
    def test_v4_and_v6_addresses_extracted(self) -> None:
        buf = _rtm_newaddr(socket.AF_INET, 1, socket.inet_aton("192.0.2.10")) + _rtm_newaddr(
            socket.AF_INET6, 1, socket.inet_pton(socket.AF_INET6, "2001:db8::10")
        )
        addrs, done = cf.parse_rtm_newaddr_dump(buf)
        assert addrs == ["192.0.2.10", "2001:db8::10"]
        assert not done

    def test_secondary_address_via_ifa_local_extracted(self) -> None:
        buf = _rtm_newaddr(socket.AF_INET, 2, socket.inet_aton("198.51.100.9"))
        addrs, _done = cf.parse_rtm_newaddr_dump(buf)
        assert addrs == ["198.51.100.9"]

    def test_done_message_terminates(self) -> None:
        buf = _rtm_newaddr(socket.AF_INET, 1, socket.inet_aton("192.0.2.10")) + _nlmsg(3, b"")
        addrs, done = cf.parse_rtm_newaddr_dump(buf)
        assert addrs == ["192.0.2.10"]
        assert done

    def test_error_message_terminates(self) -> None:
        _addrs, done = cf.parse_rtm_newaddr_dump(_nlmsg(2, b"\x00" * 20))
        assert done

    def test_malformed_length_ends_walk_without_raising(self) -> None:
        bogus = struct.pack("<IHHII", 8, 20, 0, 1, 0)  # len < header size
        addrs, done = cf.parse_rtm_newaddr_dump(bogus)
        assert addrs == []
        assert done

    def test_unknown_attr_types_skipped(self) -> None:
        ifaddrmsg = struct.pack("BBBBI", socket.AF_INET, 24, 0, 0, 2)
        attr = struct.pack("<HH", 8, 3) + b"\x01\x02\x03\x04"  # IFA_LABEL-ish
        addrs, _done = cf.parse_rtm_newaddr_dump(_nlmsg(20, ifaddrmsg + attr))
        assert addrs == []

    def test_empty_buffer(self) -> None:
        assert cf.parse_rtm_newaddr_dump(b"") == ([], False)

    def test_ptp_message_prefers_ifa_local_over_peer(self) -> None:
        # Point-to-point link: IFA_LOCAL is this host, IFA_ADDRESS is the
        # PEER — the peer must not be folded into the self set (it would
        # false-deny ssh to the tunnel remote).
        ifaddrmsg = struct.pack("BBBBI", socket.AF_INET, 32, 0, 0, 7)
        local = struct.pack("<HH", 8, 2) + socket.inet_aton("10.8.0.2")
        peer = struct.pack("<HH", 8, 1) + socket.inet_aton("10.8.0.1")
        addrs, _done = cf.parse_rtm_newaddr_dump(_nlmsg(20, ifaddrmsg + local + peer))
        assert addrs == ["10.8.0.2"]

    def test_v6_message_with_only_ifa_address_still_extracted(self) -> None:
        # Ordinary v6 addresses carry only IFA_ADDRESS — the fallback path.
        buf = _rtm_newaddr(socket.AF_INET6, 1, socket.inet_pton(socket.AF_INET6, "2001:db8::7"))
        addrs, _done = cf.parse_rtm_newaddr_dump(buf)
        assert addrs == ["2001:db8::7"]


class TestSupervisorSource:
    """Pin the SHIPPED supervisor: the module owns its source, so these
    tests execute the exact verdict code the launcher interpolates."""

    def _shipped_denied(self, ports=frozenset({22}), self_addrs=frozenset()):
        import ipaddress
        import textwrap

        header = cf.SUPERVISOR_SOURCE.split("def _fence_loop")[0]
        ns = {
            "_fence_nfd": 0,  # satisfies the guard; no loop/thread in this slice
            "_struct": struct,
            "_fence_socket": socket,
            "_fence_ip": ipaddress,  # launcher-header binding (see name contract)
            "_FENCE_PORTS": ports,
            "_FENCE_SELF": self_addrs,
        }
        exec(textwrap.dedent(header), ns)  # nosemgrep: python.lang.security.audit.exec-detected.exec-detected -- executes this repo's own shipped launcher-header source verbatim so the parity assertions below pin what the template bakes in; no external input reaches it  # noqa: S102, E501
        return ns["_fence_denied"]

    def test_ioctl_numbers_match_kernel_encoding(self) -> None:
        assert cf.SECCOMP_IOCTL_NOTIF_RECV == 0xC0502100
        assert cf.SECCOMP_IOCTL_NOTIF_SEND == 0xC0182101
        assert cf.SECCOMP_IOCTL_NOTIF_ID_VALID == 0x40082102

    def test_constants_are_formatted_into_source(self) -> None:
        src = cf.SUPERVISOR_SOURCE
        assert hex(cf.SECCOMP_IOCTL_NOTIF_RECV) in src
        assert hex(cf.SECCOMP_IOCTL_NOTIF_SEND) in src
        assert hex(cf.SECCOMP_IOCTL_NOTIF_ID_VALID) in src
        assert "{" not in src and "}" not in src  # f-string template safety

    def test_shipped_verdict_matches_fence_verdict_matrix(self) -> None:
        self_set = frozenset({"192.0.2.10", "2001:db8::10"})
        denied = self._shipped_denied(self_addrs=self_set)
        cases = [
            _sockaddr_in(22, "127.0.0.1"),
            _sockaddr_in(22, "127.8.9.10"),
            _sockaddr_in6(22, "::1"),
            _sockaddr_in(22, "192.0.2.10"),
            _sockaddr_in6(22, "2001:db8::10"),
            _sockaddr_in6(22, "::ffff:192.0.2.10"),
            _sockaddr_in(22, "0.0.0.0"),
            _sockaddr_in6(22, "::"),
            _sockaddr_in(443, "0.0.0.0"),
            _sockaddr_in(22, "198.51.100.7"),
            _sockaddr_in(443, "192.0.2.10"),
            _sockaddr_in(8080, "127.0.0.1"),
            struct.pack("<H", socket.AF_UNIX) + b"/tmp/sock\x00",
            b"",
            b"\x02",
        ]
        for raw in cases:
            module_says_deny = cf.fence_verdict(cf.parse_sockaddr(raw), self_set)
            shipped_hit = denied(raw)
            assert (shipped_hit is not None) == module_says_deny, raw
        assert (denied(None) is not None) is False

    def test_loop_closes_fd_on_unexpected_error(self) -> None:
        # Defined-state contract: unexpected RECV errors must close the
        # notify fd (ENOSYS for trapped connects), never leave it open.
        loop_src = cf.SUPERVISOR_SOURCE.split("def _fence_loop", 1)[1]
        body = loop_src.split("_fence_threading.Thread", 1)[0]
        assert "os.close(_fence_nfd)" in body

    def test_loop_denies_unreadable_sockaddr(self) -> None:
        # Fail-closed contract: a peer that hides its memory from the
        # supervisor (non-dumpable) is denied, never continued.
        loop_src = cf.SUPERVISOR_SOURCE.split("def _fence_loop", 1)[1]
        body = loop_src.split("_fence_threading.Thread", 1)[0]
        assert '("unreadable-sockaddr", 0)' in body
        assert body.index("_raw is None") < body.index("_fence_denied(_raw)")

    def test_seccomp_syscall_nr_fails_closed_on_unknown_arch(self) -> None:
        with pytest.raises(cf.FenceUnsupportedArch):
            cf.seccomp_syscall_nr("mips64")


class TestKernelGate:
    def test_pre_continue_kernels_refused(self) -> None:
        assert not cf.kernel_supports_notif_continue("5.4.0-150-generic")
        assert not cf.kernel_supports_notif_continue("5.0.0")
        assert not cf.kernel_supports_notif_continue("4.19.0-27-amd64")

    def test_continue_capable_kernels_pass(self) -> None:
        assert cf.kernel_supports_notif_continue("5.5.0")
        assert cf.kernel_supports_notif_continue("5.15.167.4-microsoft-standard-WSL2")
        assert cf.kernel_supports_notif_continue("6.1.94")

    def test_unparseable_release_fails_toward_not_arming(self) -> None:
        assert not cf.kernel_supports_notif_continue("")
        assert not cf.kernel_supports_notif_continue("linux")
