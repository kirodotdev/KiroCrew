"""Saved grants never lend credentials to a replacement connection."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import test_connected_computers_protected as helpers

from kiro_crew.connected_computers import controller as controller_mod
from kiro_crew.connected_computers import disable, enroll, flag
from kiro_crew.connected_computers.local_store import LocalGrantStore
from kiro_crew.connected_computers.share import (
    COORDINATE_FIELDS,
    approved_connection,
    connection_coordinates,
)
from kiro_crew.testing.wait import finished

vault = helpers.vault
protector = helpers.protector
store = helpers.store


def target():
    return SimpleNamespace(ssh_host="remote", local_port=1)


def controller(store, instance):
    return controller_mod.LocalController(
        store, instance_lookup=lambda _id: instance, enabled=lambda: True, clock=lambda: 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", COORDINATE_FIELDS)
async def test_changed_renewal_destination_sends_nothing_and_requires_consent(
    store, monkeypatch, field
):
    instance = target()
    grant = store.add(replace(helpers._grant(), coordinates=connection_coordinates(instance)))
    grant = store.unsealed(grant.grant_id)
    setattr(instance, field, 2 if type(getattr(instance, field, "")) is int else "different")
    send = AsyncMock()
    monkeypatch.setattr(enroll, "renew", send)
    owner = controller(store, instance)
    with pytest.raises(controller_mod.FreshConsentRequired, match="Share this computer again"):
        await finished(owner._renew(grant), "reject the changed renewal target")
    send.assert_not_awaited()
    assert "Share this computer again" in store.get(grant.grant_id).ended


@pytest.mark.asyncio
async def test_changed_reconnect_destination_starts_no_connector(store, monkeypatch):
    instance = target()
    grant = store.add(replace(helpers._grant(), coordinates=connection_coordinates(instance)))
    instance.ssh_host = "replacement"
    owner = controller(store, instance)
    monkeypatch.setattr(owner, "_checked_plan", AsyncMock(return_value=object()))
    spawn = Mock()
    monkeypatch.setattr(controller_mod, "Connector", spawn)
    await finished(owner._run_grant(grant), "reject the changed reconnect target")
    spawn.assert_not_called()
    assert "Share this computer again" in store.get(grant.grant_id).ended


def test_a_new_local_tunnel_port_keeps_the_approval():
    # A Remote Crew reconnect allocates a fresh local port; the remote is the same.
    instance = target()
    approved = connection_coordinates(instance)
    instance.local_port = 0
    assert "local_port" not in COORDINATE_FIELDS
    assert connection_coordinates(instance) == approved
    instance.local_port = 2
    assert connection_coordinates(instance) == approved


def test_the_approved_snapshot_dials_through_the_live_tunnel_port():
    instance = target()
    approved = connection_coordinates(instance)
    instance.local_port = 18790
    assert approved_connection(instance, approved).local_port == 18790
    instance.local_port = 0
    assert approved_connection(instance, approved).local_port == 0


@pytest.mark.asyncio
async def test_changed_queued_forget_destination_sends_nothing(store, monkeypatch):
    store.install_key()
    instance = target()
    grant = store.add(replace(helpers._grant(), coordinates=connection_coordinates(instance)))
    store.revoke(grant.grant_id)
    instance.ssh_host = "replacement"
    send = AsyncMock()
    monkeypatch.setattr(enroll, "forget", send)
    assert (
        await finished(
            controller_mod.drain_pending_forgets(store, lambda _id: instance),
            "withhold remote revoke from replacement target",
        )
        == 0
    )
    send.assert_not_awaited()
    assert len(store.pending_forgets()) == 1


@pytest.mark.asyncio
async def test_legacy_grant_without_coordinates_requires_fresh_consent(store, monkeypatch):
    grant = store.add(replace(helpers._grant(), coordinates=()))
    send = AsyncMock()
    monkeypatch.setattr(enroll, "renew", send)
    with pytest.raises(controller_mod.FreshConsentRequired):
        await finished(controller(store, target())._renew(grant), "reject an unbound grant")
    send.assert_not_awaited()
    assert store.get(grant.grant_id).ended


def test_coordinates_survive_restart_renewal_and_revocation(store, vault, protector):
    store.install_key()
    expected = connection_coordinates(target())
    grant = store.add(replace(helpers._grant(), coordinates=expected))
    restarted = LocalGrantStore(vault, protector=protector)
    assert restarted.get(grant.grant_id).coordinates == expected
    assert restarted.unsealed(grant.grant_id).coordinates == expected
    restarted.set_credential(grant.grant_id, "new-secret", 4000000000.0, "ab" * 32)
    assert restarted.get(grant.grant_id).coordinates == expected
    restarted.revoke(grant.grant_id)
    assert restarted.pending_forgets()[0].coordinates == expected
    assert "coordinates" not in grant.public()


@pytest.mark.asyncio
@pytest.mark.parametrize("degraded", [{"telegram"}, {"agent", "telegram"}, {"__config__"}])
async def test_unrelated_degradation_keeps_real_off_authoritative(monkeypatch, degraded):
    from kiro_crew.shared_crew import preview

    monkeypatch.setattr(preview, "preview_enabled", lambda: False)
    on = SimpleNamespace(
        dashboard=SimpleNamespace(feature_previews={flag.PREVIEW_NAME: True}),
        degraded_sections=degraded,
    )
    off = SimpleNamespace(
        dashboard=SimpleNamespace(feature_previews={}), degraded_sections=degraded
    )
    assert flag.local_switch(on) is True
    assert flag.preview_on(on) is True
    assert flag.local_switch(off) is False
    revoke = AsyncMock()
    monkeypatch.setattr(disable, "turn_off", revoke)
    monkeypatch.setattr(disable, "_TASKS", set())
    disable.apply(SimpleNamespace(old=on, new=off, touched=lambda *_args: True))
    import asyncio

    await finished(asyncio.gather(*list(disable._TASKS)), "apply authoritative Off")
    revoke.assert_awaited_once()


def test_current_read_overrides_sticky_dashboard_degradation(monkeypatch):
    from kiro_crew.shared_crew import preview

    monkeypatch.setattr(preview, "preview_enabled", lambda: False)
    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(feature_previews={flag.PREVIEW_NAME: True}),
        degraded_sections={"dashboard", "telegram", "__config__"},
        _read_degraded_sections=frozenset(),
    )
    assert flag.local_switch(cfg) is True
    cfg.dashboard.feature_previews = {}
    assert flag.local_switch(cfg) is False
    cfg._read_degraded_sections = frozenset({"dashboard"})
    assert flag.local_switch(cfg) is None


def test_live_torn_snapshot_carries_current_read_provenance(monkeypatch):
    from kiro_crew.config.live import _keep_last_valid
    from kiro_crew.config.loader import KiroCrewConfig

    prev = KiroCrewConfig()
    prev.dashboard.feature_previews = {flag.PREVIEW_NAME: True}
    torn = KiroCrewConfig(_base_unreadable=True, _read_degraded_sections=frozenset())
    kept = _keep_last_valid(prev, torn, torn_now=True)
    assert kept.dashboard.feature_previews == prev.dashboard.feature_previews
    assert kept._base_unreadable is True
    assert flag.local_switch(kept) is None
    assert "_read_degraded_sections" not in kept.to_dict()


def test_config_build_records_only_this_reads_degraded_sections(tmp_path, monkeypatch):
    from kiro_crew.config import loader, loader_pipeline, resolution

    monkeypatch.setattr(resolution, "_OBSERVED_DEGRADED_SECTIONS", {"dashboard"})
    # Through the facade: its mirror rebinds every module that holds the name.
    monkeypatch.setattr(loader, "_OBSERVED_DEGRADED_SECTIONS", {"dashboard"})
    doc = loader_pipeline.ConfigDocument(
        ticket=1,
        path=tmp_path / "config.json",
        loaded=True,
        content_digest="test",
        data={"dashboard": {"feature_previews": {flag.PREVIEW_NAME: True}}, "telegram": None},
    )
    cfg = loader_pipeline.build_config(doc)
    assert "dashboard" in cfg.degraded_sections
    assert "dashboard" not in cfg._read_degraded_sections
    assert "telegram" in cfg._read_degraded_sections


@pytest.mark.asyncio
async def test_unknown_to_off_persists_revocation_across_store_restart(
    store, vault, protector, monkeypatch
):
    import asyncio

    from kiro_crew.connected_computers import broker
    from kiro_crew.shared_crew import preview

    monkeypatch.setattr(preview, "preview_enabled", lambda: False)
    grant = store.add(helpers._grant())
    generation = store.off_generation()
    owner = controller(store, target())
    monkeypatch.setattr(owner, "_kick_drain", lambda: None)
    monkeypatch.setattr(controller_mod, "get_controller", lambda: owner)
    sockets = SimpleNamespace(shutdown_all=AsyncMock(return_value=0))
    monkeypatch.setattr(broker, "get_broker", lambda: sockets)
    monkeypatch.setattr(disable, "_TASKS", set())
    old = SimpleNamespace(
        dashboard=SimpleNamespace(feature_previews={flag.PREVIEW_NAME: True}),
        _base_unreadable=True,
    )
    off = SimpleNamespace(dashboard=SimpleNamespace(feature_previews={}))
    try:
        disable.apply(SimpleNamespace(old=old, new=off, touched=lambda *_args: True))
        await finished(asyncio.gather(*list(disable._TASKS)), "persist authoritative Off")
        restarted = LocalGrantStore(vault, protector=protector)
        assert restarted.get(grant.grant_id).active is False
        assert restarted.off_generation() == generation + 1
        assert restarted.active() == []
        sockets.shutdown_all.assert_awaited_once()
    finally:
        await finished(owner.shutdown(), "release the local controller")
