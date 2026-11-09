"""Connected computers: the completion-review findings, pinned before their fixes.

* S1 (B02, B13) -- the CLI has no share verb: a terminal check and a typed name
  are passed by any pseudo-terminal wrapper, so sharing needs the desktop app's
  native dialog and folder picker, or the local dashboard's native consent.
* S2 (B05) -- on Linux a confined child cannot reach the user's kernel keyring
  (``keyctl``, ``add_key``, ``request_key``), and the self-test proves it.
* S3 (R2) -- the Secret Service tool is resolved from fixed system folders,
  never from ``PATH``.
* S4 (B01, B07) -- a grant revoked or turned off by ANOTHER process stops new
  work on an already-open channel at the next frame, not at the next re-check.
* Many-to-many -- two local entries reaching one remote gateway keep one grant:
  the newer consent replaces the older one, and no queued revoke ends it.
* B14 -- a tool turned off locally after sharing is off at the next start of
  that server, and a server removed or turned off locally does not start.

Authored for CI; this host runs no tests locally.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace

import pytest
from test_connected_computers_local import FakeSession, FakeWs, RefusingPlan, _grant, _hello

from kiro_crew.connected_computers import confinement
from kiro_crew.connected_computers import protected_store as ps
from kiro_crew.connected_computers import protocol, seccomp
from kiro_crew.connected_computers.confinement import ConfinementUnavailable, judge_self_test
from kiro_crew.connected_computers.connector import Connector
from kiro_crew.connected_computers.local_mcp import live_limits
from kiro_crew.connected_computers.local_store import ExposedMcp, LocalGrantStore
from kiro_crew.connected_computers.protected_store import ProtectionUnavailable
from kiro_crew.mcp_discovery_server_info import McpServerInfo
from kiro_crew.secrets.vault import SecretVault

pytestmark = pytest.mark.asyncio


class _MemoryProtector:
    backend = "test"

    def __init__(self) -> None:
        self.key = None

    def load(self):
        return self.key

    def save(self, key: bytes) -> None:
        self.key = bytes(key)


@pytest.fixture
def store(tmp_path):
    return LocalGrantStore(SecretVault(tmp_path / "local-vault"), protector=_MemoryProtector())


# -- S1: no CLI share verb ---------------------------------------------------------------


def _subcommands() -> set[str]:
    from kiro_crew.connected_computers.cli import register_parser

    parser = argparse.ArgumentParser()
    register_parser(parser.add_subparsers(dest="command"))
    cc = parser._subparsers._group_actions[0].choices["connected-computer"]  # type: ignore[union-attr]
    return set(cc._subparsers._group_actions[0].choices)  # type: ignore[union-attr]


def test_s1_b13_the_cli_cannot_share_a_folder():
    names = _subcommands()
    assert "allow" not in names, "a pty wrapper passes a terminal check: no CLI consent"
    # Narrowing stays reachable from a terminal.
    assert {"status", "off", "reset-protection"} <= names


def test_s1_b13_an_allow_invocation_is_rejected_by_the_parser():
    from kiro_crew.connected_computers.cli import register_parser

    parser = argparse.ArgumentParser()
    register_parser(parser.add_subparsers(dest="command"))
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["connected-computer", "allow", "--instance", "inst-1", "--root", "/work"]
        )


# -- S2: the kernel keyring --------------------------------------------------------------

_SECCOMP_DATA_FMT = "<iIQ6Q"


def _verdict(machine: str, call: str) -> int:
    import struct

    table = seccomp.TABLES[machine]
    program = seccomp.program(machine)
    data = struct.pack(_SECCOMP_DATA_FMT, table.numbers[call], table.arch, 0, 0, 0, 0, 0, 0, 0)
    insns = [struct.unpack("<HBBI", program[i : i + 8]) for i in range(0, len(program), 8)]
    acc = 0
    pc = 0
    for _ in range(len(insns) + 1):
        code, jt, jf, k = insns[pc]
        if code == 0x20:
            acc = struct.unpack_from("<I", data, k)[0]
            pc += 1
        elif code == 0x15:
            pc += 1 + (jt if acc == k else jf)
        elif code == 0x35:
            pc += 1 + (jt if acc >= k else jf)
        elif code == 0x06:
            return k
        else:
            raise AssertionError(f"unexpected BPF opcode {code:#x}")
    raise AssertionError("the program did not return")


@pytest.mark.parametrize("machine", seccomp.MACHINES)
@pytest.mark.parametrize("call", ["keyctl", "add_key", "request_key"])
def test_s2_b05_the_kernel_keyring_is_refused(machine, call):
    assert _verdict(machine, call) == seccomp.RET_EPERM


@pytest.mark.parametrize("machine", seccomp.MACHINES)
def test_s2_ordinary_calls_still_pass_after_the_keyring_rules(machine):
    assert _verdict(machine, "read") == seccomp.RET_ALLOW
    assert _verdict(machine, "socketpair") == seccomp.RET_ALLOW


def test_s2_the_keyring_numbers_are_the_kernel_ones():
    assert {
        k: seccomp.TABLES["x86_64"].numbers[k] for k in ("add_key", "request_key", "keyctl")
    } == {
        "add_key": 248,
        "request_key": 249,
        "keyctl": 250,
    }
    assert {
        k: seccomp.TABLES["aarch64"].numbers[k] for k in ("add_key", "request_key", "keyctl")
    } == {"add_key": 217, "request_key": 218, "keyctl": 219}


def _answers(confined: bool, *, keyring: str) -> dict[str, str]:
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
        "mach": "skip",
        "keyring": keyring,
    }


_TOKENS = {"root": "TOKEN-ROOT", "out": "TOKEN-OUT"}


def test_s2_r10_a_confined_child_that_reaches_the_keyring_fails_the_self_test():
    with pytest.raises(ConfinementUnavailable, match="keyring"):
        judge_self_test(
            _answers(False, keyring="allowed"),
            _answers(True, keyring="allowed"),
            tokens=_TOKENS,
            mach=False,
            keyring=True,
        )


def test_s2_r10_a_fenced_keyring_passes():
    judge_self_test(
        _answers(False, keyring="allowed"),
        _answers(True, keyring="denied"),
        tokens=_TOKENS,
        mach=False,
        keyring=True,
    )


def test_s2_a_host_whose_own_keyring_is_closed_still_passes():
    # A container's own seccomp often refuses keyctl: nothing there to protect.
    judge_self_test(
        _answers(False, keyring="denied"),
        _answers(True, keyring="denied"),
        tokens=_TOKENS,
        mach=False,
        keyring=True,
    )


def test_s2_an_unreadable_keyring_answer_is_refused():
    with pytest.raises(ConfinementUnavailable, match="keyring"):
        judge_self_test(
            _answers(False, keyring="allowed"),
            _answers(True, keyring="error"),
            tokens=_TOKENS,
            mach=False,
            keyring=True,
        )


def test_s2_off_linux_the_keyring_probe_is_skipped():
    judge_self_test(
        _answers(False, keyring="skip"),
        _answers(True, keyring="skip"),
        tokens=_TOKENS,
        mach=False,
        keyring=False,
    )
    with pytest.raises(ConfinementUnavailable):
        judge_self_test(
            _answers(False, keyring="skip"),
            _answers(True, keyring="allowed"),
            tokens=_TOKENS,
            mach=False,
            keyring=False,
        )


def test_s2_the_probe_asks_for_the_keyring_on_linux_only():
    assert confinement.keyctl_number("linux", "x86_64") == 250
    assert confinement.keyctl_number("linux", "aarch64") == 219
    assert confinement.keyctl_number("darwin", "arm64") == 0
    assert confinement.keyctl_number("linux", "riscv64") == 0


# -- S3: secret-tool off PATH --------------------------------------------------------------


def test_s3_r2_a_secret_tool_on_path_only_is_never_used(monkeypatch, tmp_path):
    planted = tmp_path / "bin" / "secret-tool"
    planted.parent.mkdir()
    planted.write_text("#!/bin/sh\ncat > /dev/null\n")
    planted.chmod(0o755)
    monkeypatch.setenv("PATH", str(planted.parent))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1/bus")
    monkeypatch.setattr(ps, "trusted_system_bin_quiet", lambda name: None)
    with pytest.raises(ProtectionUnavailable):
        ps.LibsecretStore.for_account("acct")


def test_s3_r2_the_trusted_secret_tool_is_used(monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1/bus")
    seen = []

    def resolve(name):
        seen.append(name)
        return "/usr/bin/secret-tool"

    monkeypatch.setattr(ps, "trusted_system_bin_quiet", resolve)
    store = ps.LibsecretStore.for_account("acct")
    assert seen == ["secret-tool"]
    assert store._tool == "/usr/bin/secret-tool"


# -- S4: a revoke from another process stops the next frame ------------------------------


class _Inbound:
    def __init__(self) -> None:
        self.fed: list[bytes] = []

    def feed(self, data: bytes) -> None:
        self.fed.append(data)

    def feed_eof(self) -> None:
        self.fed.append(b"")


class _OpenChannel:
    """An already-open tool server channel, as a long-lived workspace channel is."""

    def __init__(self, ch: int = 5) -> None:
        # The connector removes a closed channel by its own number.
        self.ch = ch
        self.inbound = _Inbound()
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def _connector(grant, ws, active) -> Connector:
    return Connector(
        grant,
        url="ws://127.0.0.1:1/x",
        plan=RefusingPlan(),
        grant_still_active=active,
        session_factory=lambda: FakeSession(ws),
    )


@pytest.mark.parametrize("why", ["revoked", "switch_off"])
async def test_s4_b01_b07_work_on_an_open_channel_stops_once_access_ends_elsewhere(
    monkeypatch, why
):
    from kiro_crew.connected_computers import flag

    monkeypatch.setattr(flag, "local_enabled", lambda cfg=None: why != "switch_off")
    grant = _grant()
    [frame] = protocol.data_frames(5, b'{"jsonrpc":"2.0","id":1,"method":"tools/call"}\n')
    ws = FakeWs([_hello(grant), json.dumps(frame)])
    connector = _connector(grant, ws, active=lambda: why != "revoked")
    channel = _OpenChannel()
    connector._channels[5] = channel  # type: ignore[assignment]
    why_ended = await asyncio.wait_for(connector.run(), 30)
    assert channel.inbound.fed == [], "the command never reaches the tool server"
    assert channel.stopped, "the channel's process tree is stopped"
    assert "turned off" in why_ended


async def test_s4_an_active_grant_still_forwards_data(monkeypatch):
    from kiro_crew.connected_computers import flag

    monkeypatch.setattr(flag, "local_enabled", lambda cfg=None: True)
    grant = _grant()
    payload = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
    [frame] = protocol.data_frames(5, payload)
    ws = FakeWs([_hello(grant), json.dumps(frame)])
    connector = _connector(grant, ws, active=lambda: True)
    channel = _OpenChannel()
    connector._channels[5] = channel  # type: ignore[assignment]
    await asyncio.wait_for(connector.run(), 30)
    assert channel.inbound.fed == [payload]


async def test_s4_narrowing_frames_need_no_recheck(monkeypatch):
    from kiro_crew.connected_computers import flag

    monkeypatch.setattr(flag, "local_enabled", lambda cfg=None: True)
    grant = _grant()
    checks = []

    def active() -> bool:
        checks.append(1)
        return True

    ws = FakeWs([_hello(grant), json.dumps({"op": "close", "ch": 5})])
    connector = _connector(grant, ws, active=active)
    channel = _OpenChannel()
    connector._channels[5] = channel  # type: ignore[assignment]
    await asyncio.wait_for(connector.run(), 30)
    assert channel.stopped and checks == []


# -- many-to-many: one grant per remote gateway ------------------------------------------


def test_one_gateway_reached_through_two_entries_keeps_one_grant(store):
    first = store.add(_grant("desktop-7001"))
    second = store.add(replace(_grant("inst-b"), gateway_id=first.gateway_id))
    assert [g.grant_id for g in store.active()] == [second.grant_id]
    assert store.get(first.grant_id).active is False
    assert store.pending_forgets() == [], "the same device id: a revoke would end the new one"


def test_a_revoke_queued_through_another_entry_never_ends_the_new_grant(store):
    store.install_key()
    old = _grant("desktop-7001")
    store.queue_forget(old.instance_id, old.gateway_id, old.device_id)
    assert len(store.pending_forgets()) == 1
    store.add(replace(_grant("inst-b"), gateway_id=old.gateway_id))
    assert store.pending_forgets() == []


def test_different_gateways_keep_their_own_grants(store):
    a = store.add(_grant("inst-a"))
    b = store.add(_grant("inst-b"))
    assert {g.grant_id for g in store.active()} == {a.grant_id, b.grant_id}


def test_a_grant_without_a_gateway_id_never_matches_another(store):
    a = store.add(replace(_grant("inst-a"), gateway_id=""))
    b = store.add(replace(_grant("inst-b"), gateway_id=""))
    assert {g.grant_id for g in store.active()} == {a.grant_id, b.grant_id}


# -- B14: local limits read at each start ------------------------------------------------


def _exposed(name: str = "notes", off: tuple[str, ...] = ()) -> ExposedMcp:
    return ExposedMcp(
        name=name, command="/usr/bin/notes-mcp", args=("--stdio",), env=(), disabled_tools=off
    )


def test_b14_a_tool_turned_off_after_sharing_is_off_at_the_next_start():
    live = [McpServerInfo(name="notes", command="/usr/bin/notes-mcp", disabled_tools=["rm"])]
    assert live_limits(_exposed(off=("mv",)), live) == ("mv", "rm")


def test_b14_turning_a_tool_back_on_locally_never_widens_the_shared_snapshot():
    live = [McpServerInfo(name="notes", command="/usr/bin/notes-mcp", disabled_tools=[])]
    assert live_limits(_exposed(off=("rm",)), live) == ("rm",)


@pytest.mark.parametrize("case", ["removed", "turned_off", "now_remote"])
def test_b14_a_server_gone_or_off_locally_no_longer_starts(case):
    live = {
        "removed": [],
        "turned_off": [McpServerInfo(name="notes", command="/usr/bin/notes-mcp", disabled=True)],
        "now_remote": [McpServerInfo(name="notes", url="https://mcp.example.test/sse")],
    }[case]
    assert live_limits(_exposed(), live) is None


async def test_b14_the_connector_refuses_a_server_removed_locally(monkeypatch):
    from kiro_crew.connected_computers import flag, local_mcp

    monkeypatch.setattr(flag, "local_enabled", lambda cfg=None: True)
    monkeypatch.setattr(local_mcp, "configured_servers", lambda: [])
    grant = replace(_grant(), mcp=(_exposed(),))
    ws = FakeWs([_hello(grant), json.dumps({"op": "open", "ch": 4, "kind": "mcp:notes"})])
    connector = _connector(grant, ws, active=lambda: True)
    await asyncio.wait_for(connector.run(), 30)
    refused = [f for f in ws.sent if f["op"] == "refused"]
    assert refused and refused[0]["ch"] == 4 and "this computer" in refused[0]["reason"]


def test_b14_the_live_list_decides_the_filter_at_each_start():
    grant = replace(_grant(), mcp=(_exposed("plain"),))
    connector = _connector(grant, FakeWs([]), active=lambda: True)
    assert connector._filter_for("mcp:plain") is None
    live = connector._filter_for("mcp:plain", ("rm",))
    assert live is not None and live.disabled == frozenset({"rm"})
