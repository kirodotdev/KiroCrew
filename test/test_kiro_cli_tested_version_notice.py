"""A kiro-cli, or the kiro-cli a KAS relay runs under, outside the tested range gets one row.

The chat still runs. The range is ``KIRO_CLI_TESTED_RANGE``, which
``test_real_adapter_fixture_pins.py`` holds to the kiro-cli and KAS captures.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.acp import chat_runtime_sharing
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import kas_bundle as kas_bundle_install
from kiro_crew.acp.client_turn_control import KIRO_CLI_TESTED_RANGE, untested_version_issue
from kiro_crew.acp.harness import kas as kas_harness
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.dashboard import chat_runner
from kiro_crew.providers.acp import AcpProvider

LABEL = "2.27.0 to 3.x"


def test_the_range_is_the_one_the_label_names():
    assert KIRO_CLI_TESTED_RANGE == ((2, 27, 0), (4, 0, 0))


@pytest.mark.parametrize("version", ["2.27.0", "2.27.1", "2.32.0", "3.1.0", "3.3.0", "3.99.9"])
def test_the_docker_pin_and_every_3x_release_are_silent(version):
    assert untested_version_issue(ACP_BACKEND_KIRO, version) == ""


@pytest.mark.parametrize("version", ["", "unknown", "3.3", "v3.3.0", None])
def test_an_unreadable_version_is_silent(version):
    assert untested_version_issue(ACP_BACKEND_KIRO, version) == ""


@pytest.mark.parametrize("version", ["2.26.9", "2.21.4", "1.0.0"])
def test_an_older_kiro_cli_is_told_to_update(version):
    issue = untested_version_issue(ACP_BACKEND_KIRO, version)
    assert f"kiro-cli {version} is outside the tested range {LABEL}" in issue
    assert "'kiro-cli update'" in issue


@pytest.mark.parametrize("version", ["4.0.0", "5.2.1"])
def test_a_newer_major_is_told_crew_has_not_caught_up(version):
    issue = untested_version_issue(ACP_BACKEND_KIRO, version)
    assert f"kiro-cli {version} is outside the tested range {LABEL}" in issue
    assert "kiro-cli update" not in issue
    assert "A Kiro Crew update moves the tested range." in issue


def _row(monkeypatch, client):
    rows = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *args, **kw: rows.append(args))
    provider = SimpleNamespace(_client=client, agent_version=getattr(client, "agent_version", ""))
    notice = AcpProvider.tested_version_notice.fget(provider)
    chat_runner._notice_steer_upgrade(
        object(), object(), SimpleNamespace(tested_version_notice=notice)
    )
    return rows


def test_a_new_kiro_cli_session_outside_the_range_gets_one_notice_row(monkeypatch):
    rows = _row(monkeypatch, SimpleNamespace(backend=ACP_BACKEND_KIRO, agent_version="2.21.4"))
    assert len(rows) == 1
    assert rows[0][2] == "notice"
    assert "kiro-cli 2.21.4" in rows[0][3] and LABEL in rows[0][3]


def test_a_client_that_names_no_backend_is_not_read_as_kiro_cli(monkeypatch):
    assert _row(monkeypatch, SimpleNamespace(agent_version="2.21.4")) == []


def test_a_kas_session_shows_its_relays_kiro_cli_notice(monkeypatch):
    notice = untested_version_issue(ACP_BACKEND_KIRO, "2.21.4")
    session = AcpSessionProvider.__new__(AcpSessionProvider)
    session._handle = SimpleNamespace(kas_untested_version_notice=notice)
    assert session.kas_untested_version_notice == notice
    client = SimpleNamespace(
        backend=ACP_BACKEND_KAS,
        agent_version="0.66.43",
        kas_untested_version_notice=session.kas_untested_version_notice,
    )
    rows = _row(monkeypatch, client)
    assert len(rows) == 1 and "kiro-cli 2.21.4" in rows[0][3]


def _relay(monkeypatch, tmp_path, version, engine=None):
    async def _bin(**_kw):
        return str(tmp_path / "kiro-cli")

    async def _no_vault():
        return False

    monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: False)
    monkeypatch.setattr(kas_harness, "installed_kiro_cli_version", lambda: version)
    monkeypatch.setattr(kas_harness, "resolved_kiro_cli_version", lambda _bin, **_kw: version)
    monkeypatch.setattr(
        chat_runtime_sharing,
        "relay_engine_version",
        lambda cli: engine if cli == version else "wrong-kiro-cli",
    )
    monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", _bin)
    monkeypatch.setattr(kas_harness, "vault_holds_identity_off_loop", _no_vault)


def _bundle(monkeypatch, tmp_path):
    bundle = SimpleNamespace(
        version="0.66.104", node_version="22", node_bin=tmp_path / "node", acp_server=tmp_path / "s"
    )
    monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: True)
    monkeypatch.setattr(kas_bundle_install, "installed_bundle", lambda: bundle)


def _ctx(tmp_path):
    return SpawnContext(agent="a", work_dir=str(tmp_path), model=None, environ={}, home=tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "warns"), [((2, 21, 4), True), ((4, 0, 0), True), ((3, 3, 0), False), (None, False)]
)
async def test_the_relay_spawn_records_the_notice_for_its_kiro_cli(
    monkeypatch, tmp_path, version, warns
):
    harness = kas_harness.KasHarness()
    _relay(monkeypatch, tmp_path, version)
    await harness.resolve_spawn(_ctx(tmp_path))
    assert bool(harness.untested_version_notice) is warns
    handle = SimpleNamespace()
    harness.record_session_projection(handle, None, "a")
    assert handle.kas_untested_version_notice == harness.untested_version_notice


@pytest.mark.asyncio
@pytest.mark.parametrize(("engine", "recorded"), [("0.66.91", "0.66.91"), (None, "")])
async def test_the_relay_spawn_records_the_engine_kiro_cli_unpacked(
    monkeypatch, tmp_path, engine, recorded
):
    harness = kas_harness.KasHarness()
    _relay(monkeypatch, tmp_path, (3, 1, 0), engine=engine)
    await harness.resolve_spawn(_ctx(tmp_path))
    assert harness.relay_engine_version == recorded
    handle = SimpleNamespace()
    harness.record_session_projection(handle, None, "a")
    assert handle.kas_relay_engine_version == recorded


@pytest.mark.asyncio
async def test_a_path_only_relay_records_the_engine_for_its_resolved_kiro_cli(
    monkeypatch, tmp_path
):
    harness = kas_harness.KasHarness()
    _relay(monkeypatch, tmp_path, (3, 1, 0))
    monkeypatch.setattr(kas_harness, "installed_kiro_cli_version", lambda: None)
    seen = []

    def _engine(cli):
        seen.append(cli)
        return "0.66.91"

    monkeypatch.setattr(chat_runtime_sharing, "relay_engine_version", _engine)
    await harness.resolve_spawn(_ctx(tmp_path))
    assert seen == [(3, 1, 0)]
    assert harness.relay_engine_version == "0.66.91"
    handle = SimpleNamespace()
    harness.record_session_projection(handle, None, "a")
    assert handle.kas_relay_engine_version == "0.66.91"


@pytest.mark.asyncio
async def test_an_unreadable_relay_engine_does_not_fail_the_spawn(monkeypatch, tmp_path):
    def _boom(_cli):
        raise OSError("extract unreadable")

    harness = kas_harness.KasHarness()
    _relay(monkeypatch, tmp_path, (3, 1, 0))
    monkeypatch.setattr(chat_runtime_sharing, "relay_engine_version", _boom)
    await harness.resolve_spawn(_ctx(tmp_path))
    assert harness.relay_engine_version == ""


@pytest.mark.asyncio
async def test_a_bundle_respawn_clears_the_relays_notice(monkeypatch, tmp_path):
    """Crew's own engine is the pinned one, so kiro-cli's release says nothing about it."""
    harness = kas_harness.KasHarness()
    _relay(monkeypatch, tmp_path, (2, 21, 4), engine="0.66.91")
    await harness.resolve_spawn(_ctx(tmp_path))
    assert harness.untested_version_notice
    assert harness.relay_engine_version == "0.66.91"
    _bundle(monkeypatch, tmp_path)
    await harness.resolve_spawn(_ctx(tmp_path))
    assert harness.untested_version_notice == ""
    assert harness.relay_engine_version == ""


@pytest.mark.asyncio
async def test_a_path_only_relay_is_read_by_the_binary_it_spawns(monkeypatch, tmp_path):
    """No pinned install answers, but the spawned kiro-cli is known to be old."""
    harness = kas_harness.KasHarness()
    _relay(monkeypatch, tmp_path, None)
    seen = []

    def _resolved(binary, **_kw):
        seen.append(binary)
        return (2, 21, 4)

    monkeypatch.setattr(kas_harness, "resolved_kiro_cli_version", _resolved)
    await harness.resolve_spawn(_ctx(tmp_path))
    assert str(tmp_path / "kiro-cli") in seen
    assert "kiro-cli 2.21.4" in harness.untested_version_notice
