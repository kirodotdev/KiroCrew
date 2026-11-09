"""Connected computers: the desktop app's own local controller (G1/G2).

When the desktop window points at a remote gateway and no local gateway runs,
the desktop main process supervises ``kirocrew connected-computer desktop-host``
over a private stdio pipe. These pin what that host may do:

* G1 -- it serves the native menu's share, status, revoke and Off with the same
  grant authority as the local gateway (one store, one Off generation, the OS
  keyring), and it opens no listener of its own.
* G2 -- it runs this computer's connectors only while it holds the controller
  lock, so a local gateway and the desktop app never run them twice; stdin EOF
  (the app quit) stops every connector and keeps every grant.
* B01/B02/B13 -- nothing is shared before ``hello``, for an instance the app did
  not name, with the switch off or without OS protection; a malformed line is
  refused, never guessed at.
* FLAG -- off at start revokes what is still active, off while running revokes
  and stops; nothing is restored when it comes back on.

Authored before the module; not run locally (host rule), CI runs it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kiro_crew.connected_computers import desktop_host as host_mod
from kiro_crew.connected_computers.confinement import ConfinementUnavailable
from kiro_crew.connected_computers.controller import LocalController
from kiro_crew.connected_computers.controller_lock import ControllerLock
from kiro_crew.connected_computers.local_store import Grant, LocalGrantStore, new_grant_id
from kiro_crew.mcp_discovery_server_info import McpServerInfo
from kiro_crew.secrets.vault import SecretVault
from kiro_crew.testing.wait import async_wait_until

pytestmark = pytest.mark.asyncio

PEER_KEY = "ab" * 32


class _MemoryProtector:
    backend = "test keyring"

    def __init__(self) -> None:
        self.key = None

    def load(self):
        return self.key

    def save(self, key: bytes) -> None:
        self.key = bytes(key)


class _NoProtection:
    backend = "none"

    def load(self):
        from kiro_crew.connected_computers.protected_store import ProtectionUnavailable

        raise ProtectionUnavailable("no keyring on this computer")

    def save(self, key: bytes) -> None:
        from kiro_crew.connected_computers.protected_store import ProtectionUnavailable

        raise ProtectionUnavailable("no keyring on this computer")


@pytest.fixture
def store(tmp_path):
    return LocalGrantStore(SecretVault(tmp_path / "local-vault"), protector=_MemoryProtector())


def _no_plan():
    raise ConfinementUnavailable("not exercised in this test")


INSTANCE = {
    "id": "desktop-7778",
    "name": "Cloud crew",
    "ssh_host": "clouddesk",
    "remote_bin": "",
    "remote_port": 7778,
    "local_port": 7778,
}


@pytest.fixture
def flag():
    return {"on": True}


@pytest.fixture
def host(store, tmp_path, flag, monkeypatch):
    from kiro_crew.connected_computers import local_mcp

    monkeypatch.setattr(
        local_mcp,
        "configured_servers",
        lambda: [McpServerInfo(name="notes", command="/usr/bin/notes-mcp")],
    )
    return host_mod.DesktopHost(
        store,
        enabled=lambda: flag["on"],
        lock=ControllerLock(tmp_path / "controller.lock"),
        plan_factory=_no_plan,
        registry_lookup=lambda _id: None,
    )


@pytest.fixture
def enrolled(monkeypatch):
    from kiro_crew.connected_computers import enroll as enroll_mod

    calls: list = []

    async def fake_enroll(instance, *, base, fingerprint, label, renew=False):
        calls.append(instance)
        return enroll_mod.Enrolled(
            device_id="laptop",
            gateway_id="gw-cloud",
            gateway_label="cloud",
            credential="cred-from-ssh",
            expires=4_000_000_000.0,
            peer_key=PEER_KEY,
        )

    forgotten: list = []

    async def fake_forget(instance, *, device_id, fingerprint):
        forgotten.append(device_id)

    monkeypatch.setattr(enroll_mod, "enroll", fake_enroll)
    monkeypatch.setattr(enroll_mod, "forget", fake_forget)
    return {"enrolled": calls, "forgotten": forgotten}


def _grant(store, instance: str = "desktop-7778", gateway: str = "gw-cloud") -> Grant:
    # The store keeps one grant per remote gateway (LocalGrantStore.add), so a
    # second live grant needs its own gateway.
    return store.add(
        Grant(
            grant_id=new_grant_id(),
            instance_id=instance,
            gateway_id=gateway,
            gateway_label="cloud",
            device_id="laptop",
            root="/work/proj",
            credential="cred",
            credential_expires=4_000_000_000.0,
            peer_key=PEER_KEY,
        )
    )


async def _hello(host, instances=(INSTANCE,)) -> dict:
    return await host.handle({"id": 1, "op": "hello", "instances": list(instances)})


async def _describe(host, instance_id: str = "desktop-7778") -> dict:
    """What the app asks before its native dialogs open; it opens the consent."""
    return await host.handle({"id": 9, "op": "describe", "instance_id": instance_id})


async def _share(host, *, consent, root, mcp=(), instance_id: str = "desktop-7778") -> dict:
    return await host.handle(
        {
            "id": 2,
            "op": "share",
            "instance_id": instance_id,
            "consent": consent,
            "root": str(root),
            "mcp": list(mcp),
        }
    )


# -- G1: the native share path, with the gateway's own grant authority -----------------


async def test_g1_b02_share_writes_exactly_the_named_instance_root_and_tools(
    host, store, enrolled, tmp_path
):
    root = tmp_path / "proj"
    root.mkdir()
    assert (await _hello(host))["state"] == "running"
    described = await _describe(host)
    assert described["ok"] is True
    reply = await _share(host, consent=described["consent"], root=root, mcp=["notes"])
    assert reply["ok"] is True, reply
    [grant] = store.active()
    assert grant.instance_id == "desktop-7778"
    assert grant.root == str(root.resolve())
    assert [m.name for m in grant.mcp] == ["notes"]
    # The SSH enrollment used the target the desktop app named, nothing else.
    [instance] = enrolled["enrolled"]
    assert instance.ssh_host == "clouddesk" and instance.local_port == 7778
    # No secret crosses the pipe back to the desktop app.
    assert "cred-from-ssh" not in json.dumps(reply)
    await host.controller.shutdown()


async def test_g1_describe_shows_what_the_app_named_and_the_exposable_tools(host):
    await _hello(host)
    reply = await host.handle({"id": 2, "op": "describe", "instance_id": "desktop-7778"})
    assert reply["ok"] is True
    assert reply["instance"] == {
        "id": "desktop-7778",
        "name": "Cloud crew",
        "ssh_host": "clouddesk",
    }
    assert [s["name"] for s in reply["mcp"]] == ["notes"]


async def test_b13_nothing_is_served_before_hello(host, store, enrolled):
    for op in ("share", "describe", "status", "revoke", "off"):
        reply = await host.handle({"id": 3, "op": op, "instance_id": "desktop-7778"})
        assert reply["ok"] is False and reply["code"] == "hello_first"
    assert store.list() == [] and enrolled["enrolled"] == []


async def test_b13_an_instance_the_app_did_not_name_is_refused(host, store, enrolled, tmp_path):
    await _hello(host)
    reply = await host.handle(
        {"id": 4, "op": "share", "instance_id": "desktop-9999", "root": str(tmp_path), "mcp": []}
    )
    assert reply["ok"] is False and reply["code"] == "unknown_instance"
    assert store.list() == [] and enrolled["enrolled"] == []


@pytest.mark.parametrize(
    "bad",
    [
        {**INSTANCE, "id": "inst-1"},
        {**INSTANCE, "ssh_host": "-oProxyCommand=evil"},
        {**INSTANCE, "ssh_host": "host;rm"},
        {**INSTANCE, "remote_bin": "$(evil)"},
        {**INSTANCE, "local_port": 0},
        {**INSTANCE, "remote_port": 70000},
        {**INSTANCE, "local_port": "7778"},
    ],
)
async def test_b13_a_malformed_instance_is_refused_and_names_nothing(host, bad):
    reply = await _hello(host, instances=[bad])
    assert reply["ok"] is False and reply["code"] == "invalid_request"
    assert host.instances == {}


async def test_b13_too_many_instances_are_refused(host):
    items = [{**INSTANCE, "id": f"desktop-{5000 + n}", "local_port": 5000 + n} for n in range(17)]
    reply = await _hello(host, instances=items)
    assert reply["ok"] is False and reply["code"] == "invalid_request"


async def test_b13_unknown_ops_and_fields_are_refused(host):
    await _hello(host)
    assert (await host.handle({"id": 5, "op": "grant_everything"}))["code"] == "unknown_op"
    reply = await host.handle({"id": 6, "op": "status", "extra": 1})
    assert reply["ok"] is False and reply["code"] == "invalid_request"


async def test_r2_no_os_protection_refuses_before_any_enrollment(tmp_path, flag, enrolled):
    store = LocalGrantStore(SecretVault(tmp_path / "v"), protector=_NoProtection())
    host = host_mod.DesktopHost(
        store,
        enabled=lambda: True,
        lock=ControllerLock(tmp_path / "controller.lock"),
        plan_factory=_no_plan,
        registry_lookup=lambda _id: None,
    )
    hello = await _hello(host)
    assert hello["ok"] is True and hello["protection"] == ""
    # The dialogs never open: the consent cannot start without OS protection.
    described = await _describe(host)
    assert described["ok"] is False and described["code"] == "protection_unavailable"
    root = tmp_path / "proj"
    root.mkdir()
    reply = await host.handle(
        {"id": 2, "op": "share", "instance_id": "desktop-7778", "root": str(root), "mcp": []}
    )
    assert reply["ok"] is False and reply["code"] == "no_consent"
    assert enrolled["enrolled"] == [] and store.list() == []
    await host.controller.shutdown()


async def test_flag_share_is_refused_while_the_switch_is_off(host, store, flag, enrolled, tmp_path):
    await _hello(host)
    flag["on"] = False
    reply = await host.handle(
        {"id": 2, "op": "share", "instance_id": "desktop-7778", "root": str(tmp_path), "mcp": []}
    )
    assert reply["ok"] is False and reply["code"] == "connected_computers_disabled"
    assert enrolled["enrolled"] == [] and store.list() == []
    await host.controller.shutdown()


# -- B01 / B07: revoke and Off from the native menu ---------------------------------------


async def test_b07_revoke_one_grant_leaves_the_other(host, store, enrolled):
    first, second = _grant(store), _grant(store, "desktop-7779", "gw-cloud-2")
    await _hello(host)
    reply = await host.handle({"id": 2, "op": "revoke", "grant_id": first.grant_id})
    assert reply == {"id": 2, "ok": True, "revoked": first.grant_id}
    assert [g.grant_id for g in store.active()] == [second.grant_id]
    unknown = await host.handle({"id": 3, "op": "revoke", "grant_id": "nope"})
    assert unknown["ok"] is False and unknown["code"] == "unknown_grant"
    await host.controller.shutdown()


async def test_b01_off_revokes_every_grant_and_bumps_the_off_generation(host, store, enrolled):
    _grant(store)
    _grant(store, "desktop-7779", "gw-cloud-2")
    before = store.off_generation()
    await _hello(host)
    reply = await host.handle({"id": 2, "op": "off"})
    assert reply["ok"] is True and len(reply["revoked"]) == 2
    assert store.active() == []
    assert store.off_generation() == before + 1
    await host.controller.shutdown()


async def test_b01_off_works_while_the_switch_is_off(host, store, flag, enrolled):
    _grant(store)
    await _hello(host)
    flag["on"] = False
    reply = await host.handle({"id": 2, "op": "off"})
    assert reply["ok"] is True
    assert store.active() == []
    await host.controller.shutdown()


# -- FLAG: off at start, off while running, back on ---------------------------------------


async def test_flag_off_at_start_revokes_what_is_active_and_reports_disabled(
    host, store, flag, enrolled
):
    _grant(store)
    flag["on"] = False
    reply = await _hello(host)
    assert reply["ok"] is True and reply["state"] == "disabled"
    assert store.active() == []
    # Coming back on restores nothing: a new native consent is needed.
    flag["on"] = True
    assert store.active() == []
    await host.controller.shutdown()


async def test_flag_unreadable_at_start_keeps_saved_grants(host, store, monkeypatch):
    store.fingerprint()
    grant = _grant(store)
    generation = store.off_generation()
    # Use the production tri-state reader, with an unreadable switch result.
    monkeypatch.setattr(host, "_enabled", host_mod.local_enabled)
    monkeypatch.setattr(host_mod, "local_switch", lambda: None)
    monkeypatch.setattr(host.controller, "_enabled", lambda: False)
    try:
        reply = await _hello(host)
        assert reply["ok"] is True and reply["state"] == "running"
        assert store.get(grant.grant_id).active
        assert store.off_generation() == generation
        assert store.pending_forgets() == []
        assert host.controller._tasks == {}
    finally:
        await host.controller.shutdown()


async def test_flag_off_while_running_revokes_stops_and_exits_disabled(host, store, flag, enrolled):
    # A shared computer was enrolled, which made its install key: only then is
    # there a remote record to revoke (local_store._queue_forget).
    store.fingerprint()
    _grant(store)
    host.poll_secs = 0.01
    reader, out = _pipe([{"id": 1, "op": "hello", "instances": [INSTANCE]}], eof=False)
    task = asyncio.ensure_future(host.serve(reader, out.write))
    await async_wait_until(lambda: out.lines and out.lines[0].get("state") == "running")
    flag["on"] = False
    code = await asyncio.wait_for(task, 30)
    assert code == host_mod.EXIT_DISABLED
    assert store.active() == []
    # The remote was told before the host exited (R8 order: stop, then remote).
    assert enrolled["forgotten"] == ["laptop"]


# -- G2: one controller per computer, and quitting the app keeps grants ------------------


async def test_g2_a_held_controller_lock_means_elsewhere_and_no_connector(store, tmp_path):
    path = tmp_path / "controller.lock"
    gateway_lock = ControllerLock(path)
    assert gateway_lock.acquire() is True

    def new_host() -> host_mod.DesktopHost:
        return host_mod.DesktopHost(
            store,
            enabled=lambda: True,
            lock=ControllerLock(path),
            plan_factory=_no_plan,
            registry_lookup=lambda _id: None,
        )

    try:
        host = new_host()
        reply = await _hello(host)
        assert reply["ok"] is True and reply["state"] == "elsewhere"
        await host.controller.shutdown()
        # A fresh host process says hello once, on its pipe.
        reader, out = _pipe([{"id": 1, "op": "hello", "instances": [INSTANCE]}])
        assert await new_host().serve(reader, out.write) == host_mod.EXIT_ELSEWHERE
    finally:
        gateway_lock.release()


async def test_g2_a_controller_without_the_lock_runs_no_connector(store, tmp_path):
    path = tmp_path / "controller.lock"
    holder = ControllerLock(path)
    assert holder.acquire()
    try:
        _grant(store)
        started: list = []
        ctl = LocalController(
            store,
            instance_lookup=lambda _id: None,
            plan_factory=lambda: started.append(1) or _no_plan(),
            lock=ControllerLock(path),
            # The switch on, so only the lock keeps the connector from starting.
            enabled=lambda: True,
        )
        await ctl.reconcile()
        await asyncio.sleep(0)
        assert started == []
        [row] = ctl.status()
        assert "another Kiro Crew process" in row["state"]
    finally:
        holder.release()
    await ctl.reconcile()
    await ctl.shutdown()


async def test_flag_the_controller_reads_the_switch_it_was_given(store):
    _grant(store)
    started: list = []
    ctl = LocalController(
        store,
        instance_lookup=lambda _id: None,
        plan_factory=lambda: started.append(1) or _no_plan(),
        enabled=lambda: False,
    )
    await ctl.reconcile()
    await asyncio.sleep(0)
    assert started == [] and ctl._tasks == {}
    assert ctl._still_active(store.active()[0].grant_id) is False
    await ctl.shutdown()


async def test_g1_share_runs_the_shared_share_path(host, store, enrolled, tmp_path, monkeypatch):
    from kiro_crew.connected_computers import share as share_mod

    seen: list = []
    real = share_mod.share_computer

    async def spy(controller, **kwargs):
        seen.append(kwargs["instance_id"])
        return await real(controller, **kwargs)

    monkeypatch.setattr(share_mod, "share_computer", spy)
    root = tmp_path / "proj"
    root.mkdir()
    await _hello(host)
    described = await _describe(host)
    reply = await _share(host, consent=described["consent"], root=root)
    assert reply["ok"] is True and seen == ["desktop-7778"]
    await host.controller.shutdown()


async def test_b05_a_refused_root_shares_nothing(host, store, enrolled):
    await _hello(host)
    described = await _describe(host)
    reply = await _share(host, consent=described["consent"], root="/")
    assert reply["ok"] is False and reply["code"] == "refused"
    assert enrolled["enrolled"] == [] and store.list() == []
    await host.controller.shutdown()


def test_g2_two_locks_on_one_path_exclude_each_other(tmp_path):
    a, b = ControllerLock(tmp_path / "l"), ControllerLock(tmp_path / "l")
    assert a.acquire() and a.acquire()  # re-acquiring a held lock is a no-op
    assert b.acquire() is False
    a.release()
    assert b.acquire() is True
    b.release()


async def test_g2_stdin_eof_stops_the_controller_and_keeps_every_grant(host, store):
    grant = _grant(store)
    reader, out = _pipe([{"id": 1, "op": "hello", "instances": [INSTANCE]}])
    code = await asyncio.wait_for(host.serve(reader, out.write), 30)
    assert code == host_mod.EXIT_OK
    assert [g.grant_id for g in store.active()] == [grant.grant_id]
    # The lock is released, so a local gateway started next can take over.
    other = ControllerLock(host.controller._lock.path)
    assert other.acquire() is True
    other.release()


async def test_b13_a_first_line_that_is_not_hello_ends_the_host(host, store):
    reader, out = _pipe([{"id": 1, "op": "share", "instance_id": "desktop-7778"}])
    assert await host.serve(reader, out.write) == host_mod.EXIT_PROTOCOL
    assert out.lines[0]["ok"] is False
    assert store.list() == []


async def test_b13_an_oversized_or_non_json_line_is_refused(host):
    reader = asyncio.StreamReader(limit=host_mod.MAX_LINE_BYTES * 2)
    reader.feed_data(json.dumps({"id": 1, "op": "hello", "instances": [INSTANCE]}).encode() + b"\n")
    reader.feed_data(b"not json\n")
    reader.feed_data(b"{" + b" " * host_mod.MAX_LINE_BYTES + b"}\n")
    reader.feed_eof()
    out = _Out()
    assert await asyncio.wait_for(host.serve(reader, out.write), 30) == host_mod.EXIT_OK
    codes = [line.get("code") for line in out.lines[1:]]
    assert codes == ["invalid_request", "invalid_request"]


def test_g1_the_host_opens_no_listener():
    source = Path(host_mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("start_server", "web.Application", "create_server", ".bind(", "listen("):
        assert forbidden not in source


def test_g1_the_cli_registers_the_desktop_host_subcommand():
    import argparse

    from kiro_crew.connected_computers.cli import register_parser

    parser = argparse.ArgumentParser()
    register_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["connected-computer", "desktop-host"])
    assert args.cc_command == "desktop-host"


# -- helpers ------------------------------------------------------------------------------


class _Out:
    def __init__(self) -> None:
        self.lines: list[dict] = []

    async def write(self, data: bytes) -> None:
        for raw in data.decode("utf-8").splitlines():
            if raw.strip():
                self.lines.append(json.loads(raw))


def _pipe(messages, *, eof: bool = True):
    reader = asyncio.StreamReader()
    for msg in messages:
        reader.feed_data(json.dumps(msg).encode("utf-8") + b"\n")
    if eof:
        reader.feed_eof()
    return reader, _Out()


async def test_b13_a_share_with_no_open_consent_is_refused(host, store, enrolled, tmp_path):
    await _hello(host)
    reply = await host.handle(
        {"id": 2, "op": "share", "instance_id": "desktop-7778", "root": str(tmp_path), "mcp": []}
    )
    assert reply["ok"] is False and reply["code"] == "no_consent"
    assert enrolled["enrolled"] == [] and store.list() == []
    await host.controller.shutdown()


async def test_b01_an_off_while_the_native_dialogs_are_open_refuses_the_grant(
    host, store, enrolled, tmp_path
):
    root = tmp_path / "proj"
    root.mkdir()
    await _hello(host)
    described = await _describe(host)  # the dialogs are now open
    assert described["ok"] is True
    store.revoke_all()  # `kirocrew connected-computer off` in another terminal
    reply = await _share(host, consent=described["consent"], root=root)
    assert reply["ok"] is False and reply["code"] == "turned_off"
    assert store.active() == []
    await host.controller.shutdown()


async def test_b01_a_later_dialog_never_lends_an_earlier_one_its_generation(
    host, store, enrolled, tmp_path
):
    root = tmp_path / "proj"
    root.mkdir()
    await _hello(host)
    first = await _describe(host)  # dialog A opens
    store.revoke_all()  # Off while A is open
    second = await _describe(host)  # dialog B opens after the Off
    assert first["consent"] != second["consent"]
    reply = await _share(host, consent=first["consent"], root=root)
    assert reply["ok"] is False and reply["code"] == "turned_off"
    assert store.active() == []
    await host.controller.shutdown()


async def test_b13_a_consent_token_is_single_use_and_bound_to_its_instance(
    host, store, enrolled, tmp_path
):
    root = tmp_path / "proj"
    root.mkdir()
    await _hello(host)
    described = await _describe(host)
    wrong = await _share(host, consent=described["consent"], root=root, instance_id="desktop-9999")
    assert wrong["ok"] is False and wrong["code"] == "unknown_instance"
    made_up = await _share(host, consent="x" * 32, root=root)
    assert made_up["ok"] is False and made_up["code"] == "no_consent"
    first = await _share(host, consent=described["consent"], root=root)
    assert first["ok"] is True, first
    again = await _share(host, consent=described["consent"], root=root)
    assert again["ok"] is False and again["code"] == "no_consent"
    await host.controller.shutdown()


async def test_a_stalled_switch_read_in_the_watcher_does_not_hold_the_loop(store, tmp_path):
    """The switch watcher reads off the loop: a watchdog still fires while it stalls."""
    from test_connected_computers_state import _loop_stays_free, _stalled_switch

    from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

    read, entered, release, outcome = _stalled_switch()
    host = host_mod.DesktopHost(
        store,
        enabled=read,
        lock=ControllerLock(tmp_path / "controller.lock"),
        plan_factory=_no_plan,
        registry_lookup=lambda _id: None,
    )
    watching = asyncio.ensure_future(host._watch_switch())
    try:
        await _loop_stays_free(entered, outcome)
    finally:
        release.set()
    # The read answered off: the watcher revokes what is active and returns.
    await asyncio.wait_for(watching, FALLBACK_TIMEOUT_SECS)
    assert outcome["released"] is True
    await host.controller.shutdown()


async def test_flag_an_unreadable_switch_revokes_nothing(store, tmp_path, monkeypatch):
    """A config read that fails is unknown, not the user's Off: no grant is revoked."""
    from kiro_crew.connected_computers import flag as flag_mod

    grant = _grant(store)
    host = host_mod.DesktopHost(
        store,
        lock=ControllerLock(tmp_path / "controller.lock"),
        plan_factory=_no_plan,
        registry_lookup=lambda _id: None,
    )
    monkeypatch.setattr(host_mod, "local_switch", lambda cfg=None: None)
    monkeypatch.setattr(flag_mod, "local_switch", lambda cfg=None: None)
    reply = await _hello(host)
    assert reply["ok"] is True and reply["state"] != "disabled"
    assert [g.grant_id for g in store.active()] == [grant.grant_id]
    assert store.off_generation() == 0
    await host.controller.shutdown()


def test_flag_local_switch_reports_unknown_when_config_cannot_load(monkeypatch):
    from kiro_crew.config import live, loader
    from kiro_crew.connected_computers import flag as flag_mod

    def boom():
        raise OSError("unreadable")

    monkeypatch.setattr(live, "snapshot", lambda: None)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(boom))
    assert flag_mod.local_switch() is None
    assert flag_mod.local_enabled() is False
