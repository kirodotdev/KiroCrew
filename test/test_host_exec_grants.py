"""The pod gate is authorized by a gateway-written grant minted only on an
owner-authenticated enable, never derived from the jail-writable enablement record.

These lock in the fix for the sandbox-escape where a sandboxed shell forged
``apps/dev-fleet/installed.json`` to turn Dev Fleet on and start a pod outside the
jail. The authority is ``host-exec-grants/<app>``; it is written only by ``enable_app``
and revoked on any disabling write, so re-writing a forged record (for example on the
boot re-registration of a builtin) can never produce one.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.apps import host_exec_grants as heg


@pytest.fixture
def crew_home(tmp_path, monkeypatch):
    home = tmp_path / "crew"
    home.mkdir()
    monkeypatch.setattr(heg, "config_dir", lambda: home)
    return home


def test_grant_then_revoke_roundtrips(crew_home):
    assert heg.host_exec_granted("dev-fleet") is False
    heg.grant_host_exec("dev-fleet")
    assert heg.host_exec_granted("dev-fleet") is True
    assert (crew_home / "host-exec-grants" / "dev-fleet").is_file()
    heg.revoke_host_exec("dev-fleet")
    assert heg.host_exec_granted("dev-fleet") is False


def test_revoke_without_a_grant_is_a_noop(crew_home):
    heg.revoke_host_exec("dev-fleet")  # must not raise
    assert heg.host_exec_granted("dev-fleet") is False


def test_a_forged_installed_record_does_not_grant(crew_home):
    """The escape: a sandboxed shell plants an ``enabled: true`` installed.json. That
    file is never consulted by the gate, so no grant exists and no pod may start."""
    forged = crew_home / "apps" / "dev-fleet"
    forged.mkdir(parents=True)
    (forged / "installed.json").write_text(
        '{"name":"dev-fleet","enabled":true,"origin":"builtin"}', encoding="utf-8"
    )
    assert heg.host_exec_granted("dev-fleet") is False


def test_write_installed_never_grants_and_revokes_on_disable(crew_home, monkeypatch):
    """_write_installed must NEVER mint a grant -- it runs on the boot re-registration
    of builtins, which reads back a forgeable installed.json, so minting from
    ``enabled`` there would let a forged record plus a restart produce a real grant.
    It only revokes when a write leaves the app disabled."""
    from kiro_crew.apps import manager_records as mr
    from kiro_crew.apps.manager_records import InstalledApp

    app = crew_home / "apps" / "dev-fleet"
    app.mkdir(parents=True)
    # Patch the shared name through the apps.manager facade, never on the owner
    # module directly (the facade-mirror guard forbids the latter).
    monkeypatch.setattr("kiro_crew.apps.manager.app_dir", lambda name: crew_home / "apps" / name)

    # A write with enabled=True (as the boot re-registration of a forged record does)
    # must NOT produce a grant.
    enabled = InstalledApp(name="dev-fleet", version="1", origin="builtin", enabled=True)
    mr._write_installed("dev-fleet", enabled)
    assert heg.host_exec_granted("dev-fleet") is False

    # A pre-existing grant (from a real enable) is dropped by a disabling write, so no
    # disable path can leave a stale grant that still starts a pod.
    heg.grant_host_exec("dev-fleet")
    assert heg.host_exec_granted("dev-fleet") is True
    disabled = InstalledApp(name="dev-fleet", version="1", origin="builtin", enabled=False)
    mr._write_installed("dev-fleet", disabled)
    assert heg.host_exec_granted("dev-fleet") is False


def test_disable_revokes_even_when_record_already_reads_disabled(crew_home, monkeypatch):
    """A sandboxed shell can forge enabled:false in installed.json. An owner disable
    must still revoke a grant minted by a real earlier enable, even though disable_app
    takes its already-disabled early return."""
    from kiro_crew.apps import manager_activation as ma

    app = crew_home / "apps" / "dev-fleet"
    app.mkdir(parents=True)
    (app / "installed.json").write_text(
        '{"name":"dev-fleet","version":"1","origin":"builtin","enabled":false}',
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.apps.manager.app_dir", lambda name: crew_home / "apps" / name)

    heg.grant_host_exec("dev-fleet")
    assert heg.host_exec_granted("dev-fleet") is True

    result = ma.disable_app("dev-fleet")
    assert result.ok is True
    assert heg.host_exec_granted("dev-fleet") is False


def test_filesystem_errors_are_contained(crew_home, monkeypatch):
    """A filesystem error must never crash an enable/disable or wrongly report a grant:
    grant/revoke swallow it (best-effort), and host_exec_granted fails closed to False."""

    def boom():
        raise OSError("disk gone")

    monkeypatch.setattr(heg, "config_dir", boom)
    heg.grant_host_exec("dev-fleet")  # must not raise
    heg.revoke_host_exec("dev-fleet")  # must not raise
    assert heg.host_exec_granted("dev-fleet") is False


def test_pod_gate_denies_without_a_grant_and_allows_with_one(crew_home, monkeypatch):
    from kiro_crew.apps.builtins.dev_fleet import agent_pod_api

    monkeypatch.setattr(agent_pod_api, "host_exec_granted", heg.host_exec_granted)

    calls: list[str] = []

    @agent_pod_api._require_enabled
    async def _handler(_request):
        calls.append("ran")
        return "OK"

    class _Req:
        pass

    denied = asyncio.run(_handler(_Req()))
    assert getattr(denied, "status", None) == 403
    assert calls == []

    heg.grant_host_exec("dev-fleet")
    allowed = asyncio.run(_handler(_Req()))
    assert allowed == "OK"
    assert calls == ["ran"]
