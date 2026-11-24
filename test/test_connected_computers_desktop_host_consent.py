"""Connected computers: the consent-only desktop host, the dashboard's one share path.

While a local gateway runs this computer's connectors, a share asked for from
its dashboard is granted by a short-lived ``desktop-host`` the desktop app's
main process spawns with ``consent_only``. No HTTP route grants, so an app
backend or anything else holding the gateway's internal secret cannot share a
folder without the native dialogs (B13). These pin what that host may do:

* it serves only ``describe`` and ``share`` (B13);
* it may name the local gateway's own Remote Crew connections reached over SSH,
  never one without SSH or a malformed id (B13);
* the grant goes through the same share path and Off generation, so an Off by
  the gateway or the CLI while the dialogs are open refuses it (B01);
* it never takes the controller lock or starts a connector: the gateway that
  holds the lock runs the grant (G2).

Not run locally (host rule); CI runs it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_connected_computers_desktop_host import (
    INSTANCE,
    PEER_KEY,
    _MemoryProtector,
    _no_plan,
    _Out,
    _pipe,
)

from kiro_crew.connected_computers import desktop_host as host_mod
from kiro_crew.connected_computers.controller_lock import ControllerLock
from kiro_crew.connected_computers.local_store import LocalGrantStore
from kiro_crew.mcp_discovery_server_info import McpServerInfo
from kiro_crew.secrets.vault import SecretVault

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store(tmp_path):
    return LocalGrantStore(SecretVault(tmp_path / "local-vault"), protector=_MemoryProtector())


@pytest.fixture
def flag():
    return {"on": True}


@pytest.fixture
def enrolled(monkeypatch):
    from kiro_crew.connected_computers import enroll as enroll_mod

    calls: list = []
    forgotten: list = []

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

    async def fake_forget(instance, *, device_id, fingerprint, cgen, gateway_id=""):
        forgotten.append(device_id)

    monkeypatch.setattr(enroll_mod, "enroll", fake_enroll)
    monkeypatch.setattr(enroll_mod, "forget", fake_forget)
    return {"enrolled": calls, "forgotten": forgotten}


REMOTE = SimpleNamespace(
    id="cloud-crew", name="Cloud crew", ssh_host="clouddesk", remote_bin="", local_port=0
)
NO_SSH = SimpleNamespace(id="ssm-crew", name="SSM crew", ssh_host="", ssm_target="i-1")


@pytest.fixture
def registry():
    return {"cloud-crew": REMOTE, "ssm-crew": NO_SSH}


@pytest.fixture
def consent_host(store, tmp_path, flag, registry, monkeypatch):
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
        registry_lookup=registry.get,
    )


async def _hello(host, **extra) -> dict:
    return await host.handle(
        {"id": 1, "op": "hello", "instances": [], "consent_only": True, **extra}
    )


async def _describe(host, instance_id: str = "cloud-crew") -> dict:
    return await host.handle({"id": 2, "op": "describe", "instance_id": instance_id})


async def _share(host, consent, root, instance_id: str = "cloud-crew") -> dict:
    return await host.handle(
        {
            "id": 3,
            "op": "share",
            "instance_id": instance_id,
            "consent": consent,
            "root": str(root),
            "mcp": ["notes"],
        }
    )


async def test_b13_a_dashboard_share_writes_the_named_crew_root_and_tools(
    consent_host, store, enrolled, tmp_path
):
    root = tmp_path / "proj"
    root.mkdir()
    hello = await _hello(consent_host)
    assert hello["ok"] is True and hello["state"] == "consent"
    described = await _describe(consent_host)
    assert described["ok"] is True
    assert described["instance"] == {
        "id": "cloud-crew",
        "name": "Cloud crew",
        "ssh_host": "clouddesk",
    }
    assert [s["name"] for s in described["mcp"]] == ["notes"]
    from kiro_crew.connected_computers.confinement import describe_confinement

    # The dashboard share's dialog says plainly when commands are not fenced.
    assert described["confinement"] == describe_confinement()
    reply = await _share(consent_host, described["consent"], root)
    assert reply["ok"] is True, reply
    [grant] = store.active()
    assert grant.instance_id == "cloud-crew"
    assert grant.root == str(root.resolve())
    assert [m.name for m in grant.mcp] == ["notes"]
    [dialled] = enrolled["enrolled"]
    assert dialled.ssh_host == "clouddesk"
    assert "cred-from-ssh" not in str(reply)
    await consent_host.controller.shutdown()


async def test_g2_a_consent_only_host_never_takes_the_lock_or_starts_a_connector(
    consent_host, store, enrolled, tmp_path
):
    root = tmp_path / "proj"
    root.mkdir()
    await _hello(consent_host)
    described = await _describe(consent_host)
    assert (await _share(consent_host, described["consent"], root))["ok"] is True
    assert consent_host.controller._loop_task is None
    assert consent_host.controller._tasks == {}
    # The lock stays free for the local gateway, which runs the grant.
    other = ControllerLock(tmp_path / "controller.lock")
    assert other.acquire() is True
    other.release()
    await consent_host.controller.shutdown()


@pytest.mark.parametrize("op", ["status", "revoke", "off", "instances"])
async def test_b13_a_consent_only_host_serves_only_describe_and_share(consent_host, store, op):
    grant_before = list(store.list())
    await _hello(consent_host)
    reply = await consent_host.handle({"id": 4, "op": op})
    assert reply["ok"] is False and reply["code"] == "consent_only"
    assert store.list() == grant_before


@pytest.mark.parametrize("instance_id", ["ssm-crew", "nobody", "", "x" * 200, "bad\nid"])
async def test_b13_only_a_registry_crew_reached_over_ssh_can_be_named(
    consent_host, store, enrolled, instance_id
):
    await _hello(consent_host)
    reply = await _describe(consent_host, instance_id)
    assert reply["ok"] is False and reply["code"] == "unknown_instance"
    assert store.list() == [] and enrolled["enrolled"] == []


async def test_b13_a_host_that_is_not_consent_only_names_only_the_apps_connections(
    consent_host, store
):
    await consent_host.handle({"id": 1, "op": "hello", "instances": [INSTANCE]})
    reply = await _describe(consent_host)
    assert reply["ok"] is False and reply["code"] == "unknown_instance"
    await consent_host.controller.shutdown()


async def test_b01_an_off_while_the_dialogs_are_open_refuses_the_grant(
    consent_host, store, enrolled, tmp_path
):
    root = tmp_path / "proj"
    root.mkdir()
    await _hello(consent_host)
    described = await _describe(consent_host)
    store.revoke_all()  # the gateway's Off, or the CLI's, while the dialogs are open
    reply = await _share(consent_host, described["consent"], root)
    assert reply["ok"] is False and reply["code"] == "turned_off"
    assert store.active() == []
    assert enrolled["forgotten"] == ["laptop"]


async def test_b02_a_consent_is_single_use_and_a_made_up_one_shares_nothing(
    consent_host, store, enrolled, tmp_path
):
    root = tmp_path / "proj"
    root.mkdir()
    await _hello(consent_host)
    made_up = await _share(consent_host, "x" * 32, root)
    assert made_up["ok"] is False and made_up["code"] == "no_consent"
    described = await _describe(consent_host)
    assert (await _share(consent_host, described["consent"], root))["ok"] is True
    again = await _share(consent_host, described["consent"], root)
    assert again["ok"] is False and again["code"] == "no_consent"
    assert len(store.active()) == 1


async def test_flag_a_consent_only_host_shares_nothing_while_the_switch_is_off(
    consent_host, store, enrolled, flag, tmp_path
):
    await _hello(consent_host)
    described = await _describe(consent_host)
    flag["on"] = False
    reply = await _share(consent_host, described["consent"], tmp_path)
    assert reply["ok"] is False and reply["code"] == "connected_computers_disabled"
    assert store.list() == [] and enrolled["enrolled"] == []


async def test_flag_a_consent_only_hello_with_the_switch_off_reports_disabled(
    consent_host, store, flag
):
    flag["on"] = False
    reader, out = _pipe([{"id": 1, "op": "hello", "instances": [], "consent_only": True}])
    assert await consent_host.serve(reader, out.write) == host_mod.EXIT_DISABLED
    assert out.lines[0]["state"] == "disabled"


@pytest.mark.parametrize("value", [1, "yes", None])
async def test_b13_consent_only_must_be_a_boolean(consent_host, value):
    reply = await _hello(consent_host, consent_only=value)
    assert reply["ok"] is False and reply["code"] == "invalid_request"


async def test_b13_a_consent_only_host_revokes_nothing_at_hello(consent_host):
    reply = await _hello(consent_host, revoke_saved=True)
    assert reply["ok"] is False and reply["code"] == "invalid_request"


async def test_g2_stdin_eof_ends_a_consent_only_host(consent_host):
    reader, out = _pipe([{"id": 1, "op": "hello", "instances": [], "consent_only": True}])
    assert await consent_host.serve(reader, out.write) == host_mod.EXIT_OK
    assert isinstance(out, _Out) and out.lines[0]["state"] == "consent"
