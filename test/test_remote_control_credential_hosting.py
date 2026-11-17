"""A set lease credential starts hosting; a sign-in alone never does; off wins.

Every ``config.json`` stores ``tunnel.relay_enabled: false`` (the schema is saved in
full), so hosting for a team Crew follows ``tunnel.relay_lease_credential``, and
turning Remote Control off writes its own marker, ``tunnel.relay_turned_off``.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.edition_locks import relay_selected
from kiro_crew.relay_client.provider import RelaySettings
from kiro_crew.remote_control import setting
from kiro_crew.remote_control.guard import RemoteControlGuard


@pytest.fixture
def config_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    monkeypatch.setattr(
        "kiro_crew.config.loader.config_local_path", lambda: tmp_path / "local.json"
    )
    return path


def _store(path: Path, **tunnel: Any) -> KiroCrewConfig:
    # As this build saves it: the whole schema, so relay_turned_off is stored too. A
    # document with no relay_turned_off at all is an older build's, which the one-shot
    # upgrade check keeps off (test_remote_control_legacy_off.py).
    stored = {"relay_enabled": False, "relay_turned_off": False, **tunnel}
    path.write_text(json.dumps({"tunnel": stored}), encoding="utf-8")
    return KiroCrewConfig.load()


def _hosts(cfg: KiroCrewConfig) -> bool:
    on = relay_selected(cfg)
    # Every reader agrees: the boot choice, the client's settings and the setting.
    assert RelaySettings.from_config(cfg).enabled is on
    assert setting.requested() is on
    return on


@pytest.mark.parametrize(
    "credential",
    ["secret://crew-key", "/abs/crew-key", "workload-identity:jwt-svid", "not-a-ref"],
    ids=["secret", "file", "workload-identity", "malformed"],
)
def test_a_set_credential_starts_hosting_over_the_stored_false(config_file, credential) -> None:
    assert _hosts(_store(config_file, relay_lease_credential=credential)) is True


@pytest.mark.parametrize("credential", ["", "  ", "kiro-sign-in"])
def test_a_sign_in_alone_never_starts_hosting(config_file, credential) -> None:
    # An IdC or outside-IdP sign-in with no credential set stays a personal Crew:
    # only `kirocrew remote-control on` starts it.
    assert _hosts(_store(config_file, relay_lease_credential=credential)) is False


def test_turning_it_off_wins_over_the_credential_and_on_clears_it(config_file, capsys) -> None:
    from test_remote_control_setting import _cli

    from kiro_crew.remote_control import cli

    _store(config_file, relay_lease_credential="secret://crew-key")
    cli.run(_cli("off"))  # the Settings one-click off calls the same setting.turn_off
    stored = json.loads(config_file.read_text(encoding="utf-8"))["tunnel"]
    assert (stored["relay_enabled"], stored.get("relay_turned_off")) == (False, True)
    assert _hosts(KiroCrewConfig.load()) is False
    assert cli.CREDENTIAL_NOTICE in capsys.readouterr().out
    KiroCrewConfig.load().save()  # a later save keeps it off
    assert _hosts(KiroCrewConfig.load()) is False
    setting.turn_on()
    assert json.loads(config_file.read_text())["tunnel"]["relay_turned_off"] is False
    assert _hosts(KiroCrewConfig.load()) is True


@pytest.mark.asyncio
async def test_the_settings_one_click_off_keeps_a_credential_host_off(
    config_file, monkeypatch
) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.config import live
    from kiro_crew.dashboard.handlers import remote_control as handler

    async def _owner(_request, _op):
        return None

    monkeypatch.setattr(live, "_WATCH", None)
    monkeypatch.setattr(handler, "require_owner_dashboard_request", _owner)
    monkeypatch.setattr(handler, "sel", lambda: SimpleNamespace(log_api_access=lambda **_: None))
    _store(config_file, relay_lease_credential="secret://crew-key")
    app = web.Application()
    app.router.add_put("/api/remote-control", handler.api_remote_control_put)
    async with TestClient(TestServer(app)) as client:
        resp = await client.put("/api/remote-control", json={"enabled": False})
        assert resp.status == 200
        assert (await resp.json())["requested"] is False
    assert json.loads(config_file.read_text())["tunnel"].get("relay_turned_off") is True
    assert _hosts(KiroCrewConfig.load()) is False  # and after a reload


def test_off_writes_the_marker_into_an_overlay_that_names_relay_enabled(
    config_file, tmp_path
) -> None:
    _store(config_file, relay_lease_credential="secret://crew-key")
    overlay = tmp_path / "local.json"
    overlay.write_text(json.dumps({"tunnel": {"relay_enabled": True}}), encoding="utf-8")
    setting.turn_off()
    assert json.loads(overlay.read_text())["tunnel"] == {
        "relay_enabled": False,
        "relay_turned_off": True,
    }
    assert setting.requested() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["builder_id", "social"])
async def test_builder_id_and_social_sign_ins_are_still_refused(identity) -> None:
    from kiro_crew.relay_client.lease import LeaseCredentialError
    from kiro_crew.relay_client.lease_signin import read_lease_sign_in

    token = SimpleNamespace(
        identity=identity, profile_arn="arn:p", access_token="t", auth_method="social"
    )

    class _Provider:
        async def current(self, *, rejected_access_token=None):
            return token

    with pytest.raises(LeaseCredentialError, match="can't host a personal Crew"):
        await read_lease_sign_in(provider=_Provider())


def test_the_computed_rule_is_never_written_to_disk(config_file) -> None:
    cfg = _store(config_file, relay_lease_credential="secret://crew-key")
    assert cfg.tunnel.relay_enabled is False
    cfg.save()
    stored = json.loads(config_file.read_text(encoding="utf-8"))["tunnel"]
    assert stored["relay_enabled"] is False
    assert stored.get("relay_turned_off", False) is False


@pytest.mark.asyncio
async def test_the_guard_keeps_a_credential_host_up_and_stops_it_on_off() -> None:
    from test_remote_control_setting import _FakeManager, _FakeProvider

    manager = _FakeManager(_FakeProvider())
    guard = RemoteControlGuard(manager, manager.provider)
    cfg = KiroCrewConfig()
    tunnel = dataclasses.replace(cfg.tunnel, relay_lease_credential="secret://crew-key")

    def _change(**fields: Any) -> Any:
        new = dataclasses.replace(cfg, tunnel=dataclasses.replace(tunnel, **fields))
        return SimpleNamespace(old=None, new=new, changed=frozenset({"tunnel"}))

    guard.on_config(_change(name_override="renamed"))  # an unrelated tunnel edit
    await guard.tick()
    assert manager.stops == 0
    guard.on_config(_change(relay_turned_off=True))
    await guard.tick()
    assert manager.stops == 1


def test_the_browser_cannot_clear_the_off_marker() -> None:
    # Clearing it would start a credential host from a dashboard request: turning
    # Remote Control on stays the terminal command's job, as for relay_enabled.
    from kiro_crew.dashboard.handlers.config_all_settings import READONLY_PATHS, generic_spec

    assert "tunnel.relay_turned_off" in READONLY_PATHS
    assert generic_spec("tunnel.relay_turned_off") is None
