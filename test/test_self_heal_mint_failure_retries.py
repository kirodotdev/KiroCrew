"""A self-heal attempt that fails on its token mint schedules the next attempt.

A failed mint installs no tunnel, so no tunnel exit calls back into self-heal.
Without a retry from the recovery itself it would end there: no next attempt,
no "Giving up" line and no diagnosis, with the crew down until a manual
reconnect.
"""

from __future__ import annotations

import logging

import pytest
from instances_helpers import _patch_port_probe

from kiro_crew.instances.token_mint import TokenMintError
from kiro_crew.testing.wait import async_wait_until


class _Tunnel:
    """Fake tunnel whose start result the test decides, one call at a time."""

    starts: list[bool] = []
    hosts: list[str] = []

    def __init__(self, iid, ssh_host, lp, rp, **_kw):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        self._S = TunnelState
        self.status = TunnelStatus(instance_id=iid, local_port=lp, remote_port=rp)
        self.pid = None
        type(self).hosts.append(ssh_host)

    async def start(self):
        ok = type(self).starts.pop(0) if type(self).starts else True
        self.status.state = self._S.CONNECTED if ok else self._S.ERROR
        return ok

    async def stop(self):
        self.status.state = self._S.STOPPED


def _manager(tmp_path, monkeypatch, mint):
    from kiro_crew.instances.registry import InstancesRegistry
    from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

    _patch_port_probe(monkeypatch)
    _Tunnel.starts = []
    _Tunnel.hosts = []
    reg = InstancesRegistry(path=tmp_path / "instances.json")
    mgr = SshTunnelManager(reg, base_port=53900, mint_token=mint, tunnel_factory=_Tunnel)
    # No backoff: the scheduling seam still runs, it just does not wait.
    mgr._recover_backoff_max = 0
    return reg, mgr


async def _settled(mgr, instance_id):
    await async_wait_until(
        lambda: not mgr._recovery_by_instance.get(instance_id),
        describe=lambda: f"recovery tasks still pending: {mgr._recovery_by_instance}",
    )


@pytest.mark.asyncio
async def test_a_failed_re_mint_is_retried_against_the_corrected_record(tmp_path, monkeypatch):
    minted_for: list[str] = []

    healing = {"on": False}

    async def mint(host, **_kw):
        if not healing["on"]:
            return "TOK"  # the initial connect
        minted_for.append(host)
        if len(minted_for) == 1:
            # The record is corrected by another writer while this attempt fails.
            reg.update("cd-1", ssh_host="new-host")
            raise TokenMintError("SSM send-command failed for i-old: InvalidInstanceId")
        return "TOK"

    reg, mgr = _manager(tmp_path, monkeypatch, mint)
    reg.add(name="CD", ssh_host="old-host", instance_id="cd-1")
    await mgr.connect("cd-1")
    healing["on"] = True

    from kiro_crew.instances.ssh_tunnel_manager import TunnelState

    mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
    # Attempt 1: tier 1 fails, tier 2's mint fails. Attempt 2: tier 1 succeeds.
    _Tunnel.starts = [False, True]
    await mgr._recover_after("cd-1", 0)
    await _settled(mgr, "cd-1")

    assert minted_for == ["old-host"]
    assert mgr.status("cd-1").state == TunnelState.CONNECTED
    assert _Tunnel.hosts[-1] == "new-host"
    assert mgr._recover_attempts.get("cd-1") == 2


@pytest.mark.asyncio
async def test_a_mint_that_never_succeeds_ends_in_the_give_up_and_diagnosis(
    tmp_path, monkeypatch, caplog
):
    calls = {"mint": 0}

    async def mint(host, **_kw):
        calls["mint"] += 1
        if calls["mint"] == 1:
            return "TOK"  # the initial connect
        raise TokenMintError("crew did not answer the mint")

    reg, mgr = _manager(tmp_path, monkeypatch, mint)
    mgr._max_recovery = 3
    reg.add(name="CD", ssh_host="host", instance_id="cd-1")
    await mgr.connect("cd-1")
    diagnosed: list[str] = []
    monkeypatch.setattr(mgr, "_schedule_diagnosis", diagnosed.append)

    from kiro_crew.instances.ssh_tunnel_manager import TunnelState

    mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
    _Tunnel.starts = [False] * 10  # every tier-1 rebuild fails
    with caplog.at_level(logging.WARNING, logger="kiro_crew.instances.ssh_tunnel_manager"):
        await mgr._recover_after("cd-1", 0)
        await _settled(mgr, "cd-1")

    # Exactly _max_recovery attempts reached the mint, then the next one gave up.
    assert calls["mint"] - 1 == mgr._max_recovery
    assert diagnosed == ["cd-1"]
    assert any("Giving up self-heal for cd-1" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_direct_recover_reports_the_mint_failure_and_schedules_nothing(
    tmp_path, monkeypatch
):
    """Direct ``_recover`` callers are not handed a background task: the retry
    is scheduled by ``_recover_after``, the seam that owns the backoff."""
    calls = {"mint": 0}

    async def mint(host, **_kw):
        calls["mint"] += 1
        if calls["mint"] == 1:
            return "TOK"
        raise TokenMintError("no answer")

    reg, mgr = _manager(tmp_path, monkeypatch, mint)
    reg.add(name="CD", ssh_host="host", instance_id="cd-1")
    await mgr.connect("cd-1")

    from kiro_crew.instances.ssh_tunnel_manager import TunnelState

    mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
    _Tunnel.starts = [False]
    assert await mgr._recover("cd-1") is True
    assert not mgr._recovery_by_instance.get("cd-1")


@pytest.mark.asyncio
async def test_a_failed_mint_during_reconfiguration_is_not_retried(tmp_path, monkeypatch):
    calls = {"mint": 0}

    async def mint(host, **_kw):
        calls["mint"] += 1
        if calls["mint"] == 1:
            return "TOK"
        mgr._reconfiguring.add("cd-1")
        raise TokenMintError("no answer")

    reg, mgr = _manager(tmp_path, monkeypatch, mint)
    reg.add(name="CD", ssh_host="host", instance_id="cd-1")
    await mgr.connect("cd-1")

    from kiro_crew.instances.ssh_tunnel_manager import TunnelState

    mgr._tunnels["cd-1"].status.state = TunnelState.ERROR
    _Tunnel.starts = [False]
    await mgr._recover_after("cd-1", 0)
    assert not mgr._recovery_by_instance.get("cd-1")
    assert calls["mint"] == 2
