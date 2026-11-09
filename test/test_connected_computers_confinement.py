"""Connected computers: the local confinement and the connector's peer check.

R4  -- the macOS profile names the Mach services a child may reach; it never
       allows every service (LaunchServices, Apple Events, preferences, the
       keychain and the pasteboard reach past the shared folder).
R6  -- the connector proves credential possession without sending its bearer;
       every subsequent frame is bound to the handshake and its direction.
R7  -- Linux: a confined child cannot create an AF_UNIX socket (so no abstract
       socket of the user's session -- X11, D-Bus -- is reachable) nor an
       io_uring (which would create one past seccomp). IP networking stays as
       the plan allows; it is a separate policy from credential protection.
R10 -- the self-test runs its probe twice, unconfined and confined, against
       witnesses it creates itself, so a plan with no fence cannot pass and a
       missing ``~/.ssh`` does not read as "denied".

The BPF program is checked with a small interpreter over the instruction
encodings the program uses, so the rule is verified on every machine CI runs
on; whether the kernel and bubblewrap load it is a native probe.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import struct
import sys
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_connected_computers_local import FakeWs

from kiro_crew.connected_computers import confinement, protocol, seccomp
from kiro_crew.connected_computers.authenticated import SessionFrames
from kiro_crew.connected_computers.confinement import (
    ConfinementUnavailable,
    LinuxPlan,
    judge_self_test,
    macos_profile,
)
from kiro_crew.connected_computers.connector import (
    AuthenticatedRefusal,
    Connector,
    ConnectorRefused,
)
from kiro_crew.connected_computers.credentials import (
    CREDENTIAL_TTL_SECS,
    connector_auth,
    gateway_proof,
    mint,
    peer_key_from_secret,
)
from kiro_crew.connected_computers.devices import DeviceRegistry
from kiro_crew.connected_computers.local_store import Grant, new_grant_id
from kiro_crew.secrets.vault import SecretVault
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

pytestmark = pytest.mark.asyncio

PEER_KEY = "ab" * 32


def _grant(**kw: Any) -> Grant:
    base = dict(
        grant_id=new_grant_id(),
        instance_id="inst-1",
        gateway_id="gw-inst-1",
        gateway_label="remote",
        device_id="laptop",
        root="/work/proj",
        credential="cred-secret-value.signature",
        credential_expires=4_000_000_000.0,
        peer_key=PEER_KEY,
    )
    base.update(kw)
    return Grant(**base)


# -- R7: the Linux seccomp program ----------------------------------------------------

_SECCOMP_DATA = struct.Struct("<iIQ6Q")


def _run_bpf(program: bytes, *, arch: int, nr: int, arg0: int = 0) -> int:
    """Interpret the classic-BPF subset the program uses; return its verdict."""
    data = _SECCOMP_DATA.pack(nr, arch, 0, arg0, 0, 0, 0, 0, 0)
    insns = [struct.unpack("<HBBI", program[i : i + 8]) for i in range(0, len(program), 8)]
    acc = 0
    pc = 0
    for _ in range(len(insns) + 1):
        code, jt, jf, k = insns[pc]
        if code == 0x20:  # LD | W | ABS
            acc = struct.unpack_from("<I", data, k)[0]
            pc += 1
        elif code == 0x15:  # JMP | JEQ | K
            pc += 1 + (jt if acc == k else jf)
        elif code == 0x35:  # JMP | JGE | K
            pc += 1 + (jt if acc >= k else jf)
        elif code == 0x06:  # RET | K
            return k
        else:
            raise AssertionError(f"unexpected BPF opcode {code:#x}")
    raise AssertionError("the program did not return")


@pytest.mark.parametrize("machine", seccomp.MACHINES)
class TestSeccompProgram:
    def _verdict(self, machine: str, call: str, arg0: int = 0, arch: int | None = None) -> int:
        table = seccomp.TABLES[machine]
        return _run_bpf(
            seccomp.program(machine),
            arch=table.arch if arch is None else arch,
            nr=table.numbers[call] if call in table.numbers else int(call),
            arg0=arg0,
        )

    def test_r7_an_af_unix_socket_is_refused(self, machine):
        assert self._verdict(machine, "socket", arg0=1) == seccomp.RET_EPERM

    def test_r7_an_af_unix_socket_with_garbage_in_the_upper_half_is_refused(self, machine):
        # int arguments: only the low 32 bits are the kernel's value.
        assert self._verdict(machine, "socket", arg0=(0xDEAD << 32) | 1) == seccomp.RET_EPERM

    @pytest.mark.parametrize("family", [2, 10, 16])  # AF_INET, AF_INET6, AF_NETLINK
    def test_r7_ip_and_other_sockets_stay_allowed(self, machine, family):
        assert self._verdict(machine, "socket", arg0=family) == seccomp.RET_ALLOW

    @pytest.mark.parametrize("call", ["io_uring_setup", "io_uring_enter", "io_uring_register"])
    def test_r7_io_uring_is_refused(self, machine, call):
        assert self._verdict(machine, call) == seccomp.RET_EPERM

    def test_r7_socketpair_and_ordinary_calls_stay_allowed(self, machine):
        assert self._verdict(machine, "socketpair", arg0=1) == seccomp.RET_ALLOW
        assert self._verdict(machine, "read") == seccomp.RET_ALLOW

    def test_r7_another_architecture_is_killed(self, machine):
        # A 32-bit entry (int 0x80, AArch32) uses other numbers: never allowed.
        assert self._verdict(machine, "read", arch=0x40000003) == seccomp.RET_KILL_PROCESS

    def test_r7_the_x32_number_space_is_refused(self, machine):
        nr = seccomp.X32_SYSCALL_BIT | seccomp.TABLES[machine].numbers["socket"]
        assert self._verdict(machine, str(nr), arg0=1) == seccomp.RET_EPERM


def test_r7_an_unknown_machine_has_no_program_and_no_plan(monkeypatch):
    with pytest.raises(ValueError):
        seccomp.program("riscv64")
    monkeypatch.setattr(confinement, "_bin", lambda name: "/usr/bin/bwrap")
    with pytest.raises(ConfinementUnavailable, match="processor"):
        confinement.plan_for_this_machine("linux", machine="riscv64")


def test_r7_the_linux_plan_hands_bwrap_the_program_on_an_inherited_fd(tmp_path):
    root = tmp_path / "root"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    confined = LinuxPlan("/usr/bin/bwrap", machine="x86_64").wrap(
        ["/usr/bin/tool"], root=str(root), scratch=str(scratch)
    )
    try:
        argv = confined.argv
        at = argv.index("--seccomp")
        fd = int(argv[at + 1])
        assert confined.pass_fds == (fd,)
        assert at < argv.index("--"), "the filter is a bwrap option, not the child's"
        assert os.read(fd, 4096) == seccomp.program("x86_64")
        assert "--unshare-net" not in argv, "IP networking is not this fence's job"
    finally:
        confined.close()
    with pytest.raises(OSError):
        os.fstat(fd)


# -- R4: the macOS profile ------------------------------------------------------------


def _profile(tmp_path) -> str:
    root = tmp_path / "root"
    scratch = tmp_path / "scratch"
    return macos_profile(root=str(root), scratch=str(scratch), reads=())


@pytest.mark.skipif(sys.platform == "win32", reason="the macOS profile is built from POSIX paths")
def test_r4_the_profile_never_allows_every_mach_service(tmp_path):
    lines = _profile(tmp_path).splitlines()
    assert "(allow mach-lookup)" not in lines
    mach = [line for line in lines if "mach-lookup" in line]
    assert mach and all("(global-name " in line for line in mach)


@pytest.mark.skipif(sys.platform == "win32", reason="the macOS profile is built from POSIX paths")
@pytest.mark.parametrize(
    "service",
    [
        "com.apple.coreservices.launchservicesd",  # `open -a Terminal`
        "com.apple.coreservices.appleevents",  # osascript to another app
        "com.apple.SecurityServer",  # keychain items
        "com.apple.cfprefsd.daemon",  # writes preferences outside the root
        "com.apple.cfprefsd.agent",
        "com.apple.pasteboard.1",
        "com.apple.FSEvents",
    ],
)
def test_r4_services_that_reach_past_the_folder_are_not_listed(tmp_path, service):
    assert f'"{service}"' not in _profile(tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="the macOS profile is built from POSIX paths")
def test_r4_r7_network_is_ip_only_and_unix_sockets_stay_in_the_folder(tmp_path):
    profile = _profile(tmp_path)
    lines = profile.splitlines()
    assert "(allow network*)" not in lines, "a bare network* reaches every unix socket"
    assert any("(remote ip " in line for line in lines), "IP egress stays as designed"
    unix = [line for line in lines if "subpath" in line and line.startswith("(allow network")]
    assert unix and all(str(tmp_path) in line for line in unix)


# -- R10: the self-test judge -----------------------------------------------------------


def _expected(confined: bool, *, mach: bool = False) -> dict[str, str]:
    deny = "denied" if confined else None
    return {
        "root_read": "TOKEN-ROOT",
        "root_write": "allowed",
        "out_read": deny or "TOKEN-OUT",
        "out_write": deny or "allowed",
        "out_list": deny or "allowed",
        "link_read": deny or "TOKEN-OUT",
        "ipc": deny or "allowed",
        "net": "allowed",
        "scratch_write": "allowed",
        "mach": ("denied" if confined else "allowed") if mach else "skip",
        # The keyring witness (S2) has its own tests in
        # test_connected_computers_completion.py; these runs skip it.
        "keyring": "skip",
    }


_TOKENS = {"root": "TOKEN-ROOT", "out": "TOKEN-OUT"}


def test_r10_a_fenced_result_passes():
    judge_self_test(_expected(False), _expected(True), tokens=_TOKENS, mach=False)


@pytest.mark.parametrize("probe", ["out_read", "out_write", "out_list", "link_read", "ipc"])
def test_r10_a_confined_probe_that_got_through_is_refused(probe):
    leaked = _expected(True)
    leaked[probe] = _expected(False)[probe]
    with pytest.raises(ConfinementUnavailable, match=probe):
        judge_self_test(_expected(False), leaked, tokens=_TOKENS, mach=False)


def test_r10_a_witness_the_unconfined_run_cannot_reach_proves_nothing():
    # Without the control run a missing ~/.ssh read as "denied" and passed.
    control = _expected(False)
    control["out_read"] = "denied"
    with pytest.raises(ConfinementUnavailable, match="prepare.*out_read"):
        judge_self_test(control, _expected(True), tokens=_TOKENS, mach=False)


def test_r10_a_plan_that_cannot_use_the_root_is_refused():
    broken = _expected(True)
    broken["root_read"] = "denied"
    with pytest.raises(ConfinementUnavailable, match="root_read"):
        judge_self_test(_expected(False), broken, tokens=_TOKENS, mach=False)


def test_r10_a_plan_that_cut_the_network_is_refused():
    # The network policy is the plan's own contract, judged apart from the fence.
    cut = _expected(True)
    cut["net"] = "denied"
    with pytest.raises(ConfinementUnavailable, match="net"):
        judge_self_test(_expected(False), cut, tokens=_TOKENS, mach=False)


def test_r4_r10_on_macos_a_reachable_excluded_mach_service_is_refused():
    leaked = _expected(True, mach=True)
    leaked["mach"] = "allowed"
    with pytest.raises(ConfinementUnavailable, match="mach"):
        judge_self_test(_expected(False, mach=True), leaked, tokens=_TOKENS, mach=True)


def test_r10_missing_or_extra_answers_are_refused():
    short = _expected(True)
    del short["ipc"]
    with pytest.raises(ConfinementUnavailable):
        judge_self_test(_expected(False), short, tokens=_TOKENS, mach=False)


class _NoFencePlan:
    """Runs the child exactly as given: the shape of a sandbox that silently failed."""

    name = "no-fence"

    def wrap(self, argv, *, root, scratch, reads=(), env=None):
        return confinement.Confined(
            list(argv), confinement.child_env(scratch, extra=env or {}), root
        )


def test_r10_a_plan_that_blocks_its_own_scratch_is_refused():
    # HOME and TMPDIR are the scratch: a profile that denies it breaks every tool.
    blocked = _expected(True)
    blocked["scratch_write"] = "denied"
    with pytest.raises(ConfinementUnavailable, match="scratch_write"):
        judge_self_test(_expected(False), blocked, tokens=_TOKENS, mach=False)


@pytest.mark.skipif(sys.platform == "win32", reason="the macOS profile is built from POSIX paths")
def test_r4_the_macos_plan_names_the_resolved_scratch_and_root(tmp_path):
    # mkdtemp on a Mac answers /var/folders/..., a link to /private/var/...;
    # Seatbelt matches the resolved path, so the link spelling denies writes.
    real_scratch = tmp_path / "private-scratch"
    real_root = tmp_path / "private-root"
    real_scratch.mkdir()
    real_root.mkdir()
    scratch_link = tmp_path / "scratch-link"
    root_link = tmp_path / "root-link"
    scratch_link.symlink_to(real_scratch)
    root_link.symlink_to(real_root)
    resolved_scratch = os.path.realpath(real_scratch)
    resolved_root = os.path.realpath(real_root)
    confined = confinement.MacPlan("/usr/bin/sandbox-exec").wrap(
        ["/usr/bin/true"], root=str(root_link), scratch=str(scratch_link)
    )
    profile = confined.argv[2]
    assert f'(allow file-read* file-write* (subpath "{resolved_scratch}"))' in profile
    assert f'(allow file-read* file-write* (subpath "{resolved_root}"))' in profile
    assert str(scratch_link) not in profile and str(root_link) not in profile
    assert confined.env["HOME"] == confined.env["TMPDIR"] == resolved_scratch
    assert confined.cwd == resolved_root


class _ScratchlessPlan:
    """Unconfined, but its child's TMPDIR cannot be written: a profile missing the scratch."""

    name = "scratchless"

    def wrap(self, argv, *, root, scratch, reads=(), env=None):
        gone = os.path.join(scratch, "not-made")
        return confinement.Confined(list(argv), confinement.child_env(gone), root)


@pytest.mark.skipif(sys.platform == "win32", reason="the connector has no Windows plan")
async def test_r10_the_probe_writes_the_scratch_through_tmpdir(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    w = await asyncio.to_thread(confinement._prepare, str(root), sys.platform)
    try:
        control = await confinement._run_probe(None, w, str(root), FALLBACK_TIMEOUT_SECS)
        blocked = await confinement._run_probe(
            _ScratchlessPlan(), w, str(root), FALLBACK_TIMEOUT_SECS
        )
    finally:
        await asyncio.to_thread(w.cleanup)
    assert control["scratch_write"] == "allowed"
    assert blocked["scratch_write"] == "denied"


@pytest.mark.skipif(sys.platform == "win32", reason="the connector has no Windows plan")
async def test_r10_a_plan_with_no_fence_fails_the_self_test(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ConfinementUnavailable, match="did not confine"):
        await asyncio.wait_for(confinement.self_test(_NoFencePlan(), str(root)), 60)
    assert sorted(os.listdir(root)) == [], "the probe leaves nothing in the shared folder"


# -- R6: the connector authenticates the gateway before the credential --------------


class FakeSession:
    def __init__(self, ws: FakeWs) -> None:
        self.ws = ws
        self.dialed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def ws_connect(self, url, *, headers, **_kw):
        self.dialed = True
        self.ws.headers = dict(headers)
        return self.ws


def _hello(grant: Grant, *, key: str = PEER_KEY, gateway: str | None = None):
    def build(headers: dict[str, str]) -> str:
        gw = gateway or grant.gateway_id
        g_nonce = secrets.token_hex(32)
        proof = gateway_proof(
            key, gw, grant.device_id, headers.get(protocol.NONCE_HEADER, ""), g_nonce
        )
        return json.dumps(
            {
                "op": "hello",
                "v": protocol.PROTOCOL_VERSION,
                "gateway": gw,
                "device": grant.device_id,
                "nonce": g_nonce,
                "proof": proof,
            }
        )

    return build


def _connector(grant: Grant, ws: FakeWs) -> tuple[Connector, FakeSession]:
    session = FakeSession(ws)
    return (
        Connector(
            grant,
            url="ws://127.0.0.1:1/x",
            plan=_NoFencePlan(),
            grant_still_active=lambda: True,
            session_factory=lambda: session,
        ),
        session,
    )


async def test_r6_the_upgrade_request_carries_no_credential():
    grant = _grant()
    ws = FakeWs([_hello(grant)])
    connector, _session = _connector(grant, ws)
    await asyncio.wait_for(connector.run(), 30)
    assert "Authorization" not in ws.headers
    assert grant.credential not in json.dumps(ws.headers)
    assert ws.headers[protocol.DEVICE_HEADER] == grant.device_id
    assert protocol.valid_nonce(ws.headers[protocol.NONCE_HEADER])


async def test_r6_a_peer_without_the_peer_key_never_receives_the_credential():
    grant = _grant()
    ws = FakeWs([_hello(grant, key="cd" * 32)])  # a squatter guessing a key
    connector, _session = _connector(grant, ws)
    with pytest.raises(ConnectorRefused):
        await asyncio.wait_for(connector.run(), 30)
    assert ws.sent == [], "nothing, least of all the credential, goes to an unproven peer"


async def test_r6_a_replayed_hello_for_another_nonce_is_refused():
    grant = _grant()
    stale = _hello(grant)({protocol.NONCE_HEADER: secrets.token_hex(32)})
    ws = FakeWs([stale])
    connector, _session = _connector(grant, ws)
    with pytest.raises(ConnectorRefused):
        await asyncio.wait_for(connector.run(), 30)
    assert ws.sent == []


async def test_r6_a_proven_gateway_gets_a_proof_then_authenticated_ready():
    grant = _grant()
    ws = FakeWs([_hello(grant)])
    connector, _session = _connector(grant, ws)
    await asyncio.wait_for(connector.run(), 30)
    assert [f["op"] for f in ws.sent[:2]] == ["auth", "ready"]
    assert "credential" not in ws.sent[0]
    assert grant.credential not in "".join(ws.wire_sent)
    # auth is the first authenticated frame, ready the second.
    assert [json.loads(text)["seq"] for text in ws.wire_sent[:2]] == [0, 1]


async def test_r6_a_grant_without_a_peer_key_never_dials():
    grant = _grant(peer_key="")
    ws = FakeWs([])
    connector, session = _connector(grant, ws)
    with pytest.raises(ConnectorRefused, match="again"):
        await asyncio.wait_for(connector.run(), 30)
    assert session.dialed is False


def test_r6_the_proof_binds_gateway_device_and_both_nonces():
    a, b = secrets.token_hex(32), secrets.token_hex(32)
    base = gateway_proof(PEER_KEY, "gw", "dev", a, b)
    assert len(base) == 64
    assert base != gateway_proof(PEER_KEY, "gw2", "dev", a, b)
    assert base != gateway_proof(PEER_KEY, "gw", "dev2", a, b)
    assert base != gateway_proof(PEER_KEY, "gw", "dev", b, a)
    assert base != gateway_proof("cd" * 32, "gw", "dev", a, b)


# -- R6: the gateway side of the handshake ---------------------------------------------

GATEWAY = "gw-remote-test"


@pytest.fixture
def registry(tmp_path):
    return DeviceRegistry(SecretVault(tmp_path / "remote-vault"))


@pytest_asyncio.fixture
async def gateway_client(monkeypatch, registry):
    from kiro_crew.connected_computers import ws_route

    # The route reads ``remote_refusal`` (the upgrade) and ``remote_enabled``
    # (the live watch); both are the preview + developer gate, turned on here.
    monkeypatch.setattr(ws_route, "remote_refusal", lambda cfg=None: None)
    monkeypatch.setattr(ws_route, "remote_enabled", lambda cfg=None: True)
    monkeypatch.setattr(ws_route, "_registry", lambda: registry)
    monkeypatch.setattr(ws_route, "_gateway_id", lambda: GATEWAY)
    app = web.Application()
    ws_route.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client


# Install fingerprints are SHA-256 hex (identity.FINGERPRINT_RE).
FP_1 = "1" * 64
FP_2 = "2" * 64


def _enroll(registry: DeviceRegistry, base: str = "laptop", fp: str = FP_1):
    return registry.enroll(base=base, fingerprint=fp, label="Laptop", owner="owner")


async def test_r6_the_gateway_refuses_a_credential_sent_in_the_upgrade(gateway_client, registry):
    from kiro_crew.connected_computers.ws_route import CONNECT_PATH

    rec = _enroll(registry)
    token, _ = mint(rec, GATEWAY)
    resp = await gateway_client.get(
        CONNECT_PATH,
        headers={
            "Authorization": f"Bearer {token}",
            protocol.DEVICE_HEADER: rec.device_id,
            protocol.NONCE_HEADER: secrets.token_hex(32),
        },
    )
    assert resp.status == 400
    assert (await resp.json())["code"] == "credential_too_early"


async def test_r6_an_unknown_device_is_refused_before_any_upgrade(gateway_client):
    from kiro_crew.connected_computers.ws_route import CONNECT_PATH

    resp = await gateway_client.get(
        CONNECT_PATH,
        headers={protocol.DEVICE_HEADER: "nobody", protocol.NONCE_HEADER: secrets.token_hex(32)},
    )
    assert resp.status == 401


async def test_r6_the_gateway_proves_the_peer_key_from_enrollment(gateway_client, registry):
    from kiro_crew.connected_computers.ws_route import CONNECT_PATH

    rec = _enroll(registry)
    key = peer_key_from_secret(rec, GATEWAY)
    nonce = secrets.token_hex(32)
    async with gateway_client.ws_connect(
        CONNECT_PATH, headers={protocol.DEVICE_HEADER: rec.device_id, protocol.NONCE_HEADER: nonce}
    ) as ws:
        hello = protocol.decode(
            await ws.receive_str(timeout=FALLBACK_TIMEOUT_SECS), allowed=frozenset({"hello"})
        )
        assert hello["gateway"] == GATEWAY and hello["device"] == rec.device_id
        expected = gateway_proof(key, GATEWAY, rec.device_id, nonce, hello["nonce"])
        assert hello["proof"] == expected
        frames = SessionFrames(key, (GATEWAY, rec.device_id, nonce, hello["nonce"]), gateway=False)
        await ws.send_str(frames.encode({"op": "auth", "payload": "e30", "proof": "0" * 64}))
        assert await _refusal_code(ws, frames) == "invalid_proof"
        assert ws.close_code == 4401


async def test_r6_another_devices_credential_is_refused_on_this_handshake(gateway_client, registry):
    from kiro_crew.connected_computers.ws_route import CONNECT_PATH

    mine = _enroll(registry, base="laptop", fp=FP_1)
    other = _enroll(registry, base="desktop", fp=FP_2)
    token, _ = mint(other, GATEWAY)
    async with gateway_client.ws_connect(
        CONNECT_PATH,
        headers={protocol.DEVICE_HEADER: mine.device_id, protocol.NONCE_HEADER: "0" * 64},
    ) as ws:
        hello = json.loads(await ws.receive_str(timeout=FALLBACK_TIMEOUT_SECS))
        bound = (GATEWAY, mine.device_id, "0" * 64, hello["nonce"])
        frames = SessionFrames(peer_key_from_secret(mine, GATEWAY), bound, gateway=False)
        await ws.send_str(frames.encode(connector_auth(token, bound)))
        assert await _refusal_code(ws, frames) == "wrong_device"
        assert ws.close_code == 4401


async def _refusal_code(ws: Any, frames: SessionFrames) -> str:
    """The gateway's authenticated refusal, then its close."""
    msg = await ws.receive(timeout=FALLBACK_TIMEOUT_SECS)
    assert msg.type == aiohttp.WSMsgType.TEXT, msg
    code = frames.decode(msg.data, allowed=protocol.BEFORE_ADMISSION)["code"]
    closing = await ws.receive(timeout=FALLBACK_TIMEOUT_SECS)
    assert closing.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
    return code


def test_r6_a_new_enrollment_changes_the_peer_key_and_a_renewal_keeps_it(registry):
    first = _enroll(registry)
    renewed = registry.enroll(
        base="laptop", fingerprint=FP_1, label="Laptop", owner="owner", renew=True
    )
    assert peer_key_from_secret(first, GATEWAY) == peer_key_from_secret(renewed, GATEWAY)
    again = _enroll(registry)
    assert peer_key_from_secret(first, GATEWAY) != peer_key_from_secret(again, GATEWAY)
    assert peer_key_from_secret(first, GATEWAY) != peer_key_from_secret(first, "gw-other")


# -- close codes are unauthenticated: only a MACed refusal refuses -----------------------


@pytest.mark.parametrize(
    "close",
    [
        (protocol.CLOSE_CREDENTIAL_REJECTED, "expired"),
        (protocol.CLOSE_CREDENTIAL_REJECTED, "invalid"),
        (protocol.CLOSE_HANDSHAKE_FAILED, "handshake failed"),
        (protocol.CLOSE_STATE_UNREADABLE, "state_unreadable"),
        (1000, ""),
    ],
)
async def test_a_forged_close_code_is_only_an_unadmitted_end(close):
    """A relay forwards a genuine hello, then substitutes the close: nothing follows."""
    grant = _grant()
    ws = FakeWs([_hello(grant)], close=close, admit=False)
    connector, _session = _connector(grant, ws)
    why = await asyncio.wait_for(connector.run(), FALLBACK_TIMEOUT_SECS)
    assert "before the gateway admitted" in why
    assert connector.admitted is False
    assert ws.closed


@pytest.mark.parametrize(
    ("code", "terminal", "expired"),
    [
        ("expired", False, True),
        ("invalid", False, False),
        ("state_unreadable", False, False),
        ("superseded", True, False),
        ("revoked", True, False),
        ("removed", True, False),
    ],
)
async def test_an_authenticated_refusal_raises_with_its_code(code, terminal, expired):
    grant = _grant()
    ws = FakeWs([_hello(grant), json.dumps({"op": "refusal", "code": code})], admit=False)
    connector, _session = _connector(grant, ws)
    with pytest.raises(AuthenticatedRefusal) as refused:
        await asyncio.wait_for(connector.run(), FALLBACK_TIMEOUT_SECS)
    assert refused.value.code == code
    assert (refused.value.terminal, refused.value.expired) == (terminal, expired)


async def test_an_unauthenticated_refusal_frame_is_a_protocol_failure():
    grant = _grant()
    forged = json.dumps({"op": "refusal", "code": "expired"})
    ws = FakeWs([_hello(grant), forged], protect=False, admit=False)
    connector, _session = _connector(grant, ws)
    why = await asyncio.wait_for(connector.run(), FALLBACK_TIMEOUT_SECS)
    assert "protocol error" in why
    assert connector.protocol_failed is True and connector.admitted is False


async def test_work_before_admission_is_a_protocol_failure():
    grant = _grant()
    ws = FakeWs(
        [_hello(grant), json.dumps({"op": "open", "ch": 1, "kind": "workspace"})], admit=False
    )
    connector, _session = _connector(grant, ws)
    why = await asyncio.wait_for(connector.run(), FALLBACK_TIMEOUT_SECS)
    assert "protocol error" in why
    assert [f["op"] for f in ws.sent] == ["auth", "ready"]


async def test_an_ordinary_close_after_admission_ends_the_connection():
    grant = _grant()
    ws = FakeWs([_hello(grant)], close=(1000, ""))
    connector, _session = _connector(grant, ws)
    why = await asyncio.wait_for(connector.run(), FALLBACK_TIMEOUT_SECS)
    assert why == "the connection ended"
    assert connector.admitted is True and connector.protocol_failed is False


class _BorrowedSession:
    """The test client's session, left open when the connector's ``async with`` ends."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _real_connector(gateway_client, rec, token, expires):
    from kiro_crew.connected_computers.ws_route import CONNECT_PATH

    grant = _grant(
        gateway_id=GATEWAY,
        device_id=rec.device_id,
        credential=token,
        credential_expires=expires,
        peer_key=peer_key_from_secret(rec, GATEWAY),
    )
    return Connector(
        grant,
        url=str(gateway_client.make_url(CONNECT_PATH)),
        plan=_NoFencePlan(),
        grant_still_active=lambda: True,
        session_factory=lambda: _BorrowedSession(gateway_client.session),
    )


async def test_a_dashboard_logout_does_not_refuse_the_real_handshake(
    gateway_client, registry, monkeypatch
):
    """A remote `kirocrew logout` bumps the dashboard's revocation generation only."""
    from kiro_crew.connected_computers import ws_route
    from kiro_crew.connected_computers.broker import ConnectionBroker
    from kiro_crew.dashboard import token_auth, token_auth_tokens
    from kiro_crew.testing.wait import async_wait_until

    broker = ConnectionBroker()
    monkeypatch.setattr(ws_route, "get_broker", lambda: broker)
    rec = _enroll(registry)
    token, expires = mint(rec, GATEWAY)
    minted_gen = token_auth_tokens.current_revocation_gen()
    monkeypatch.setattr(token_auth, "current_revocation_gen_or_none", lambda: minted_gen + 1)
    connector = _real_connector(gateway_client, rec, token, expires)
    task = asyncio.create_task(connector.run())
    try:
        await async_wait_until(lambda: broker.live(rec.device_id) is not None)
        await async_wait_until(lambda: connector.admitted)
        await asyncio.wait_for(broker.disconnect(rec.device_id, "done"), FALLBACK_TIMEOUT_SECS)
        assert "done" in await asyncio.wait_for(task, FALLBACK_TIMEOUT_SECS)
    finally:
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), FALLBACK_TIMEOUT_SECS)


@pytest.mark.parametrize("revoked", [False, True], ids=["active", "revoked"])
async def test_an_expired_credential_is_refused_by_the_record_first(
    gateway_client, registry, monkeypatch, manual_clock, revoked
):
    """Expired and active: ``expired`` (renew); expired and revoked: ``revoked`` (end)."""
    from kiro_crew.connected_computers import credentials
    from kiro_crew.dashboard import token_auth_tokens

    manual_clock.install(monkeypatch, token_auth_tokens)
    manual_clock.install(monkeypatch, credentials)
    rec = _enroll(registry)
    token, expires = mint(rec, GATEWAY)
    if revoked:
        registry.revoke(rec.device_id)
    manual_clock.advance(CREDENTIAL_TTL_SECS + 1)
    connector = _real_connector(gateway_client, rec, token, expires)
    with pytest.raises(AuthenticatedRefusal) as refused:
        await asyncio.wait_for(connector.run(), FALLBACK_TIMEOUT_SECS)
    assert refused.value.code == ("revoked" if revoked else "expired")
    assert refused.value.terminal is revoked and connector.admitted is False


async def test_an_unreadable_device_registry_is_an_authenticated_transient_refusal(
    gateway_client, registry, monkeypatch
):
    from kiro_crew.connected_computers.ws_route import CONNECT_PATH

    rec = _enroll(registry)
    token, _ = mint(rec, GATEWAY)
    real_get = registry.get
    reads: list[str] = []

    def flaky_get(device_id):
        reads.append(device_id)
        if len(reads) > 1:
            raise OSError("the vault could not be read")
        return real_get(device_id)

    monkeypatch.setattr(registry, "get", flaky_get)
    nonce = secrets.token_hex(32)
    async with gateway_client.ws_connect(
        CONNECT_PATH, headers={protocol.DEVICE_HEADER: rec.device_id, protocol.NONCE_HEADER: nonce}
    ) as ws:
        hello = json.loads(await ws.receive_str(timeout=FALLBACK_TIMEOUT_SECS))
        bound = (GATEWAY, rec.device_id, nonce, hello["nonce"])
        frames = SessionFrames(peer_key_from_secret(rec, GATEWAY), bound, gateway=False)
        await ws.send_str(frames.encode(connector_auth(token, bound)))
        code = await _refusal_code(ws, frames)
        assert code == "state_unreadable" and code not in protocol.TERMINAL_REFUSALS
        assert ws.close_code == protocol.CLOSE_STATE_UNREADABLE


# -- self_test cancellation leaves nothing behind -----------------------------------------


async def test_a_self_test_cancelled_while_preparing_releases_the_witnesses(monkeypatch, tmp_path):
    import threading

    from kiro_crew.testing.wait import async_wait_until, await_until

    release = threading.Event()
    preparing = asyncio.Event()
    loop = asyncio.get_running_loop()
    cleaned: list[str] = []

    class _Owned:
        def cleanup(self) -> None:
            cleaned.append("witnesses")

    def slow_prepare(root, platform):
        loop.call_soon_threadsafe(preparing.set)
        assert release.wait(FALLBACK_TIMEOUT_SECS), "the test never released the worker"
        return _Owned()

    monkeypatch.setattr(confinement, "_prepare", slow_prepare)
    monkeypatch.setattr(confinement._Witnesses, "cleanup", _Owned.cleanup)
    task = asyncio.create_task(confinement.self_test(_NoFencePlan(), str(tmp_path)))
    try:
        await await_until(preparing, what="the worker to start preparing")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned == [], "not yet: the worker still prepares"
    finally:
        release.set()
    await async_wait_until(lambda: cleaned == ["witnesses"])


async def test_a_self_test_cancelled_during_its_probe_kills_and_reaps_it(monkeypatch, tmp_path):
    from kiro_crew import platform_compat
    from kiro_crew.testing.ids import UNALLOCATABLE_PID
    from kiro_crew.testing.wait import await_until

    events: list[str] = []
    communicating = asyncio.Event()

    class _Witness:
        spec = {"keyctl": 0}
        tokens: dict = {}
        scratch = str(tmp_path)

        def cleanup(self) -> None:
            events.append("cleanup")

    class _Probe:
        pid = UNALLOCATABLE_PID
        returncode = None

        async def communicate(self):
            communicating.set()
            await asyncio.Event().wait()

    async def spawn(*_argv, **_kw):
        return _Probe()

    async def kill_and_reap(proc):
        assert proc.pid == UNALLOCATABLE_PID
        events.append("kill_and_reap")

    monkeypatch.setattr(confinement, "_prepare", lambda root, platform: _Witness())
    monkeypatch.setattr(confinement.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(platform_compat, "kill_and_reap", kill_and_reap)
    task = asyncio.create_task(confinement.self_test(_NoFencePlan(), str(tmp_path)))
    await await_until(communicating, what="the probe to run")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["kill_and_reap", "cleanup"], "the probe is reaped before the witnesses go"
