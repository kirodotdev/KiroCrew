"""Socket-layer fence for the agent sandbox.

Denies ``connect(2)`` from sandboxed processes to this host's own sshd. The
text tier (``sandbox-escape-ssh-self`` catalog rule + argv floor) refuses the
command lines it can see; this fence closes the residual class the text tier
structurally cannot: interpreter indirection, script bodies, and non-ssh
clients.

Mechanism: a seccomp user-notification filter installed by the sandbox
launcher child. ``connect(2)`` traps to a supervisor in the gateway process,
which decodes the sockaddr and answers deny (``EPERM``) when the target is a
self address on a fenced port, or continue otherwise. Every function in this
module is pure and unit-tested; the launcher template (which is import-free
by design) inlines the runtime — install, fd handoff with a supervisor-live
ack, verdict loop — with this module's program bytes and wire-format
constants baked in as build-time literals, so the tests here pin exactly
what the template bakes in.

Verdict precedence makes stacking safe: the launcher's existing kill-filter
returns ALLOW for ``connect``, and USER_NOTIF outranks ALLOW, so adding this
filter never weakens the existing one.

Declared limit (also in the PR body): seccomp user-notification sockaddr
inspection carries the documented user-memory race — a multithreaded caller
can rewrite the sockaddr between decode and verdict. The fence raises the
boundary from text-only to socket-layer-with-a-documented-race; full closure
is the netns egress design, which is out of scope for this module.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import struct
from dataclasses import dataclass

# ── Architecture table ──────────────────────────────────────────────────────
# AUDIT_ARCH values and connect(2) syscall numbers for the platforms the
# Linux sandbox backend supports. The BPF program pins BOTH: a filter built
# for the wrong architecture must fail closed at build time, not misread
# syscall numbers at run time.

AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_AARCH64 = 0xC00000B7

_ARCH_TABLE: dict[str, tuple[int, int]] = {
    # machine -> (audit_arch, __NR_connect)
    "x86_64": (AUDIT_ARCH_X86_64, 42),
    "aarch64": (AUDIT_ARCH_AARCH64, 203),
}


class FenceUnsupportedArch(RuntimeError):
    """Raised when no seccomp arch entry exists for this machine."""


def arch_entry(machine: str) -> tuple[int, int]:
    """Return ``(audit_arch, connect_nr)`` for *machine* or raise."""
    try:
        return _ARCH_TABLE[machine]
    except KeyError as exc:
        raise FenceUnsupportedArch(
            f"connect fence has no seccomp arch entry for {machine!r}"
        ) from exc


# ── BPF program (pure bytes) ────────────────────────────────────────────────
# Layout of struct seccomp_data: nr (offset 0), arch (offset 4),
# instruction_pointer (8), args[0..5] (16 + 8*i).

_BPF_LD = 0x00
_BPF_W = 0x00
_BPF_ABS = 0x20
_BPF_JMP = 0x05
_BPF_JEQ = 0x10
_BPF_JSET = 0x40
_BPF_RET = 0x06
_BPF_K = 0x00

SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_USER_NOTIF = 0x7FC00000
SECCOMP_RET_ERRNO = 0x00050000

#: Bit 30 marks the x32 syscall ABI on x86-64. An x32 caller reaches
#: ``connect`` under ``0x40000000 | 42`` — a number no equality match on the
#: native table sees — so the program refuses the whole alias space instead.
_X32_SYSCALL_BIT = 0x40000000

_SECCOMP_DATA_NR_OFFSET = 0
_SECCOMP_DATA_ARCH_OFFSET = 4


def _insn(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("<HBBI", code, jt, jf, k)


def build_connect_notif_prog(machine: str) -> bytes:
    """BPF program: trap ``connect`` to user-notif, refuse the x32 alias
    space with ENOSYS, allow everything else.

    Wrong-arch syscalls are ALLOWED through (they fall to the existing
    kill-filter and the kernel's own arch handling) rather than killed: this
    filter's only job is the connect trap, and returning ALLOW for foreign
    arches keeps it composable with the launcher's deny filter.

    Under the MATCHED arch, any syscall number with the x32 bit set returns
    ENOSYS: the x32 ABI reaches ``connect`` (and every denied syscall) under
    numbers the native equality matches never see. Off x86-64 that bit only
    names invalid syscall numbers the kernel itself answers with ENOSYS, so
    one program shape serves every supported arch.
    """
    audit_arch, connect_nr = arch_entry(machine)
    return b"".join(
        [
            _insn(_BPF_LD | _BPF_W | _BPF_ABS, 0, 0, _SECCOMP_DATA_ARCH_OFFSET),
            _insn(_BPF_JMP | _BPF_JEQ | _BPF_K, 0, 3, audit_arch),
            _insn(_BPF_LD | _BPF_W | _BPF_ABS, 0, 0, _SECCOMP_DATA_NR_OFFSET),
            _insn(_BPF_JMP | _BPF_JSET | _BPF_K, 2, 0, _X32_SYSCALL_BIT),
            _insn(_BPF_JMP | _BPF_JEQ | _BPF_K, 2, 0, connect_nr),
            _insn(_BPF_RET | _BPF_K, 0, 0, SECCOMP_RET_ALLOW),
            _insn(_BPF_RET | _BPF_K, 0, 0, SECCOMP_RET_ERRNO | errno.ENOSYS),
            _insn(_BPF_RET | _BPF_K, 0, 0, SECCOMP_RET_USER_NOTIF),
        ]
    )


BPF_INSN_COUNT = 8


# ── sockaddr decode (pure) ──────────────────────────────────────────────────

_SOCKADDR_IN_LEN = 8  # family + port + addr; trailing pad not required
_SOCKADDR_IN6_MIN_LEN = 24  # family + port + flowinfo + 16-byte addr


@dataclass(frozen=True)
class ConnectTarget:
    family: int
    address: ipaddress.IPv4Address | ipaddress.IPv6Address
    port: int


def parse_sockaddr(raw: bytes) -> ConnectTarget | None:
    """Decode an AF_INET/AF_INET6 sockaddr; ``None`` for every other family.

    ``None`` means "not fence subject matter" (AF_UNIX, netlink, truncated
    buffers): the caller must answer CONTINUE, never deny, so a decode gap
    can only ever fail open toward the existing text tier — the fence adds
    denials, it does not invent them.
    """
    if len(raw) < 2:
        return None
    family = struct.unpack_from("<H", raw, 0)[0]
    if family == socket.AF_INET and len(raw) >= _SOCKADDR_IN_LEN:
        port = struct.unpack_from("!H", raw, 2)[0]
        addr = ipaddress.IPv4Address(raw[4:8])
        return ConnectTarget(family, addr, port)
    if family == socket.AF_INET6 and len(raw) >= _SOCKADDR_IN6_MIN_LEN:
        port = struct.unpack_from("!H", raw, 2)[0]
        addr = ipaddress.IPv6Address(raw[8:24])
        return ConnectTarget(family, addr, port)
    return None


# ── Fence policy (pure) ─────────────────────────────────────────────────────

FENCE_PORTS: frozenset[int] = frozenset({22})


def _normalized(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Fold IPv4-mapped IPv6 (::ffff:a.b.c.d) onto its IPv4 identity."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def fence_verdict(
    target: ConnectTarget | None,
    self_addresses: frozenset[str],
    ports: frozenset[int] = FENCE_PORTS,
) -> bool:
    """True to DENY the connect; False to let it continue.

    Denies when the port is fenced AND the address is this host: any
    loopback (127.0.0.0/8, ::1), any unspecified address (0.0.0.0, ::) —
    the kernel delivers a connect to the unspecified address at loopback,
    so it is a self address by another spelling — or any address in
    *self_addresses* (the interface-table sweep from
    ``fence_self_addresses``). Unparsed or non-INET targets always
    continue — see ``parse_sockaddr``.
    """
    if target is None or target.port not in ports:
        return False
    addr = _normalized(target.address)
    if addr.is_loopback or addr.is_unspecified:
        return True
    return str(addr) in self_addresses


def self_address_strings(candidates: list[str]) -> frozenset[str]:
    """Canonicalize the host-address seed into comparable strings.

    Silently drops entries that do not parse as IP literals (hostnames have
    no place at the socket layer) and folds v4-mapped forms, so one set
    serves both families in ``fence_verdict``.
    """
    out: set[str] = set()
    for cand in candidates:
        try:
            addr = ipaddress.ip_address(cand)
        except ValueError:
            continue
        out.add(str(_normalized(addr)))
    return frozenset(out)


# ── seccomp wire format (pure) ──────────────────────────────────────────────
# These constants are the single source for the runtime: SUPERVISOR_SOURCE
# below formats them into the supervisor text the launcher interpolates, and
# the child-install flags are baked into the template by the builder. The
# unit tests pin the encodings; nothing re-types them by hand.

import errno

_SYS_SECCOMP_X86_64 = 317
_SYS_SECCOMP_AARCH64 = 277
SECCOMP_SET_MODE_FILTER = 1
SECCOMP_FILTER_FLAG_NEW_LISTENER = 1 << 3

_IOC_WRITE = 1
_IOC_READ = 2
_SECCOMP_IOC_TYPE = 0x21  # '!'

_NOTIF_SIZE = 80  # id(8) pid(4) flags(4) + seccomp_data(64)
_RESP_SIZE = 24  # id(8) val(8) error(4) flags(4)
_ID_SIZE = 8

SECCOMP_USER_NOTIF_FLAG_CONTINUE = 1


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (_SECCOMP_IOC_TYPE << 8) | nr


SECCOMP_IOCTL_NOTIF_RECV = _ioc(_IOC_READ | _IOC_WRITE, 0, _NOTIF_SIZE)
SECCOMP_IOCTL_NOTIF_SEND = _ioc(_IOC_READ | _IOC_WRITE, 1, _RESP_SIZE)
SECCOMP_IOCTL_NOTIF_ID_VALID = _ioc(_IOC_WRITE, 2, _ID_SIZE)

_NOTIF_DATA_ARGS_OFFSET = 16 + 16  # notif header (16) + nr/arch/ip (16)


def seccomp_syscall_nr(machine: str) -> int:
    if machine == "x86_64":
        return _SYS_SECCOMP_X86_64
    if machine == "aarch64":
        return _SYS_SECCOMP_AARCH64
    raise FenceUnsupportedArch(
        f"connect fence has no seccomp(2) number for {machine!r}"
    )


_USER_NOTIF_CONTINUE_KERNEL = (5, 5)


def kernel_supports_notif_continue(release: str) -> bool:
    """True when *release* (``uname -r``) supports USER_NOTIF_FLAG_CONTINUE.

    ``SECCOMP_FILTER_FLAG_NEW_LISTENER`` exists from 5.0 but ``CONTINUE``
    responses only from 5.5, so a 5.0-5.4 kernel would accept the filter
    install and then reject every allow verdict — hanging all sandboxed
    connects. The builder therefore refuses to arm the fence below 5.5
    (degrading to the text tier). Unparseable releases return False: the
    gate fails toward NOT arming, never toward a filter that cannot be
    answered.
    """
    match = re.match(r"(\d+)\.(\d+)", release)
    if match is None:
        return False
    return (int(match.group(1)), int(match.group(2))) >= _USER_NOTIF_CONTINUE_KERNEL


# ── Supervisor source (single spelling) ─────────────────────────────────────
# The launcher template is import-free and built as an f-string, so the
# runtime supervisor cannot import this module. Instead the module OWNS the
# supervisor's source text and ``sandbox._build_launcher_script`` interpolates
# it verbatim: there is exactly one spelling of the verdict/wire code, the
# ioctl numbers below are formatted into it from the same constants the unit
# tests pin, and an edit to the launcher template cannot silently diverge
# from the tested logic. The text is pre-indented for the template's
# parent-branch block and deliberately brace-free (f-string safety).
#
# Loop exit contract: any unexpected RECV error CLOSES the notify fd before
# returning, so trapped connects fail fast with ENOSYS instead of blocking
# on a filter nobody answers; EINTR retries. Verdicts: deny = EPERM after an
# ID_VALID re-check (notification-reuse race), allow = CONTINUE. A peer whose
# memory is unreadable (a non-dumpable target closes /proc/<pid>/mem to the
# supervisor) is DENIED, not continued: hiding the sockaddr from the verdict
# is itself grounds for denial, so the fence fails closed on that path.
#
# Name contract: the text is import-free (the launcher imports everything
# pre-isolation, module-level — see the import-contract note in the template
# header), so the header must bind: os, sys, _struct, _fence_socket,
# _fence_fcntl, _fence_ip, _fence_threading, _FENCE_PORTS, _FENCE_SELF,
# _fence_nfd.

SUPERVISOR_SOURCE = '''\
        if _fence_nfd >= 0:
            def _fence_denied(raw):
                if raw is None or len(raw) < 8:
                    return None
                _fam = _struct.unpack_from("<H", raw, 0)[0]
                if _fam == _fence_socket.AF_INET:
                    _fport = _struct.unpack_from("!H", raw, 2)[0]
                    _fip = _fence_ip.ip_address(raw[4:8])
                elif _fam == _fence_socket.AF_INET6 and len(raw) >= 24:
                    _fport = _struct.unpack_from("!H", raw, 2)[0]
                    _fip = _fence_ip.ip_address(raw[8:24])
                else:
                    return None
                if _fport not in _FENCE_PORTS:
                    return None
                if getattr(_fip, "ipv4_mapped", None) is not None:
                    _fip = _fip.ipv4_mapped
                if _fip.is_loopback or _fip.is_unspecified or str(_fip) in _FENCE_SELF:
                    return (str(_fip), _fport)
                return None

            def _fence_loop():
                while True:
                    _nbuf = bytearray({NOTIF_SIZE})
                    try:
                        _fence_fcntl.ioctl(_fence_nfd, {RECV}, _nbuf)
                    except InterruptedError:
                        continue
                    except OSError:
                        try:
                            os.close(_fence_nfd)  # trapped connects: ENOSYS
                        except OSError:
                            pass
                        return
                    _nid, _npid = _struct.unpack_from("<QI", _nbuf, 0)
                    _nargs = _struct.unpack_from("<6Q", _nbuf, {ARGS_OFFSET})
                    _raw = None
                    try:
                        _mfd = os.open("/proc/" + str(_npid) + "/mem", os.O_RDONLY)
                        try:
                            _raw = os.pread(_mfd, min(_nargs[2], 128), _nargs[1])
                        finally:
                            os.close(_mfd)
                    except OSError:
                        _raw = None
                    if _raw is None:
                        _hit = ("unreadable-sockaddr", 0)
                    else:
                        _hit = _fence_denied(_raw)
                    if _hit is not None:
                        try:
                            _fence_fcntl.ioctl(_fence_nfd, {IDVALID}, _struct.pack("<Q", _nid))
                        except OSError:
                            continue
                        print("sandbox: connect-fence DENIED " + _hit[0] + ":" + str(_hit[1]),
                              file=sys.stderr)
                        _resp = _struct.pack("<QqiI", _nid, 0, {EPERM_NEG}, 0)
                    else:
                        _resp = _struct.pack("<QqiI", _nid, 0, 0, {CONTINUE})
                    try:
                        _fence_fcntl.ioctl(_fence_nfd, {SEND}, _resp)
                    except OSError:
                        pass

            _fence_threading.Thread(target=_fence_loop, daemon=True).start()
'''.format(
    NOTIF_SIZE=_NOTIF_SIZE,
    RECV=hex(SECCOMP_IOCTL_NOTIF_RECV),
    SEND=hex(SECCOMP_IOCTL_NOTIF_SEND),
    IDVALID=hex(SECCOMP_IOCTL_NOTIF_ID_VALID),
    ARGS_OFFSET=_NOTIF_DATA_ARGS_OFFSET,
    EPERM_NEG=-errno.EPERM,
    CONTINUE=SECCOMP_USER_NOTIF_FLAG_CONTINUE,
)


# ── Self-address seed (stdlib-only) ─────────────────────────────────────────
# The fence resolves "this host" without importing the text tier's identity
# machinery: the launcher template must stay import-free, and the supervisor
# must never do DNS at connect time. Loopback denial in ``fence_verdict`` is
# unconditional, so this sweep only WIDENS coverage to the host's adapter
# addresses; an empty sweep degrades to loopback-only, never to open.
#
# Primary source: a netlink RTM_GETADDR dump — the same complete kernel
# interface-address table ``ip addr`` reads, including secondary addresses
# and every family, which hostname lookups and egress probes structurally
# miss on multi-homed hosts. Hostname A/AAAA records are kept as an additive
# second source for NAT-mapped names that resolve to addresses not bound on
# any local interface.

_NLMSG_DONE = 3
_NLMSG_ERROR = 2
_RTM_GETADDR = 22
_RTM_NEWADDR = 20
_NLM_F_REQUEST_DUMP = 0x0001 | 0x0300  # NLM_F_REQUEST | NLM_F_ROOT|NLM_F_MATCH
_IFA_ADDRESS = 1
_IFA_LOCAL = 2
_NLMSG_HDR_LEN = 16  # len(u32) type(u16) flags(u16) seq(u32) pid(u32)
_IFADDRMSG_LEN = 8  # family prefixlen flags scope (u8 x4) + index(u32)


def parse_rtm_newaddr_dump(buf: bytes) -> tuple[list[str], bool]:
    """Pure parser for one netlink recv buffer of an RTM_GETADDR dump.

    Returns ``(addresses, done)`` where *done* reports whether NLMSG_DONE
    (or an error message) terminated the dump inside this buffer. Malformed
    lengths end the walk rather than raising: the seed is best-effort and
    the caller degrades to whatever was collected.
    """
    out: list[str] = []
    offset = 0
    while offset + _NLMSG_HDR_LEN <= len(buf):
        msg_len, msg_type = struct.unpack_from("<IH", buf, offset)
        if msg_len < _NLMSG_HDR_LEN or offset + msg_len > len(buf):
            return out, True
        if msg_type in (_NLMSG_DONE, _NLMSG_ERROR):
            return out, True
        if msg_type == _RTM_NEWADDR and msg_len >= _NLMSG_HDR_LEN + _IFADDRMSG_LEN:
            family = buf[offset + _NLMSG_HDR_LEN]
            attr_off = offset + _NLMSG_HDR_LEN + _IFADDRMSG_LEN
            msg_end = offset + msg_len
            # Per-message attribute preference: on point-to-point/tunnel
            # links IFA_LOCAL is this host's address and IFA_ADDRESS is the
            # PEER's — folding the peer into the self set would false-deny a
            # legitimate ssh to the tunnel remote. On ordinary links the two
            # are equal (v4) or only IFA_ADDRESS is present (v6). So: collect
            # both per message, keep IFA_LOCAL when any is present, fall back
            # to IFA_ADDRESS otherwise.
            locals_: list[str] = []
            addrs_: list[str] = []
            while attr_off + 4 <= msg_end:
                attr_len, attr_type = struct.unpack_from("<HH", buf, attr_off)
                if attr_len < 4 or attr_off + attr_len > msg_end:
                    break
                if attr_type in (_IFA_ADDRESS, _IFA_LOCAL):
                    data = buf[attr_off + 4 : attr_off + attr_len]
                    try:
                        decoded = None
                        if family == socket.AF_INET and len(data) >= 4:
                            decoded = str(ipaddress.IPv4Address(data[:4]))
                        elif family == socket.AF_INET6 and len(data) >= 16:
                            decoded = str(ipaddress.IPv6Address(data[:16]))
                    except ValueError:
                        decoded = None
                    if decoded is not None:
                        (locals_ if attr_type == _IFA_LOCAL else addrs_).append(decoded)
                attr_off += (attr_len + 3) & ~3
            out.extend(locals_ if locals_ else addrs_)
        offset += (msg_len + 3) & ~3
    return out, False


def _netlink_interface_addresses() -> list[str]:
    """Dump the kernel interface-address table; empty list on any failure."""
    if not hasattr(socket, "AF_NETLINK"):
        return []
    collected: list[str] = []
    try:
        nl = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)  # NETLINK_ROUTE
    except OSError:
        return []
    try:
        nl.settimeout(2.0)
        request = struct.pack(
            "<IHHII", _NLMSG_HDR_LEN + _IFADDRMSG_LEN, _RTM_GETADDR,
            _NLM_F_REQUEST_DUMP, 1, 0,
        ) + bytes(_IFADDRMSG_LEN)
        nl.send(request)
        for _ in range(64):  # bounded: a dump is a handful of buffers
            addrs, done = parse_rtm_newaddr_dump(nl.recv(65536))
            collected.extend(addrs)
            if done:
                break
    except OSError:
        pass
    finally:
        nl.close()
    return collected


def fence_self_addresses() -> frozenset[str]:
    """Complete-table adapter-address sweep via stdlib, packet-less.

    Sources: the netlink RTM_GETADDR dump (authoritative interface table),
    plus hostname A/AAAA lookups (NAT-mapped coverage). Each is optional;
    both failing degrades to loopback-only via ``fence_verdict``.
    """
    candidates: list[str] = list(_netlink_interface_addresses())
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    if hostname:
        try:
            for info in socket.getaddrinfo(hostname, None):
                candidates.append(str(info[4][0]).split("%", 1)[0])
        except OSError:
            pass
    return self_address_strings(candidates)
