"""A relay KAS older than the one in kiro-cli 3.4.0 does not name Kiro Crew: warn, never refuse.

kiro-cli 3.4.0 ships KAS 0.66.111. Older kiro-cli releases ship a KAS that sends
the Kiro service another client's name for a ``kirocrew`` chat. The relay spawn
records the notice for the kiro-cli it launched, the session carries it, and a new
chat shows it in the same notice row as the other kiro-cli upgrade notices. Crew's
own engine bundle is pinned, so a bundle spawn clears it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.acp import client as client_mod
from kiro_crew.acp import kas_bundle as kas_bundle_install
from kiro_crew.acp.harness import kas as kas_harness
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.dashboard import chat_runner
from kiro_crew.providers.acp import AcpProvider


@pytest.mark.parametrize("version", [(2, 37, 1), (3, 1, 0), (3, 3, 0)])
def test_an_older_kiro_cli_is_told_to_update_and_the_chat_still_runs(version):
    issue = kas_harness.kas_client_origin_issue(version)
    installed = ".".join(map(str, version))
    assert f"kiro-cli {installed} is older than 3.4.0" in issue
    assert "The chat still runs." in issue
    assert "Update kiro-cli the way you installed it" in issue
    for command in ("kiro-cli update", "toolbox update kiro-cli", "brew upgrade --cask kiro-cli"):
        assert f"'{command}'" in issue


@pytest.mark.parametrize("version", [(3, 4, 0), (3, 7, 0), (4, 0, 0), None])
def test_the_floor_a_newer_release_and_an_unknown_version_are_silent(version):
    assert kas_harness.kas_client_origin_issue(version) == ""


def _relay(monkeypatch, tmp_path, version):
    async def _bin(**_kw):
        return str(tmp_path / "kiro-cli")

    async def _no_vault():
        return False

    monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: False)
    monkeypatch.setattr(kas_harness, "installed_kiro_cli_version", lambda: version)
    monkeypatch.setattr(kas_harness, "resolved_kiro_cli_version", lambda _bin, **_kw: version)
    monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", _bin)
    monkeypatch.setattr(kas_harness, "vault_holds_identity_off_loop", _no_vault)


def _ctx(tmp_path):
    return SpawnContext(agent="a", work_dir=str(tmp_path), model=None, environ={}, home=tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "warns"), [((3, 1, 0), True), ((3, 4, 0), False), (None, False)]
)
async def test_the_relay_spawn_records_the_notice_for_the_kiro_cli_it_spawns(
    monkeypatch, tmp_path, version, warns
):
    harness = kas_harness.KasHarness()
    _relay(monkeypatch, tmp_path, version)
    await harness.resolve_spawn(_ctx(tmp_path))
    assert bool(harness.client_origin_notice) is warns
    handle = SimpleNamespace()
    harness.record_session_projection(handle, None, "a")
    assert handle.kas_client_origin_notice == harness.client_origin_notice


@pytest.mark.asyncio
async def test_a_bundle_respawn_clears_the_relays_notice(monkeypatch, tmp_path):
    harness = kas_harness.KasHarness()
    _relay(monkeypatch, tmp_path, (3, 1, 0))
    await harness.resolve_spawn(_ctx(tmp_path))
    assert harness.client_origin_notice
    bundle = SimpleNamespace(
        version="0.66.117", node_version="22", node_bin=tmp_path / "node", acp_server=tmp_path / "s"
    )
    monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: True)
    monkeypatch.setattr(kas_bundle_install, "installed_bundle", lambda: bundle)
    await harness.resolve_spawn(_ctx(tmp_path))
    assert harness.client_origin_notice == ""


def test_a_new_chat_shows_the_notice_in_one_row(monkeypatch):
    notice = kas_harness.kas_client_origin_issue((3, 1, 0))
    session = AcpSessionProvider.__new__(AcpSessionProvider)
    session._handle = SimpleNamespace(kas_client_origin_notice=notice)
    assert session.kas_client_origin_notice == notice
    provider = SimpleNamespace(_client=SimpleNamespace(kas_client_origin_notice=notice))
    shown = AcpProvider.client_origin_notice.fget(provider)
    assert shown == notice
    rows = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *args, **kw: rows.append(args))
    chat_runner._notice_steer_upgrade(
        object(), object(), SimpleNamespace(client_origin_notice=shown)
    )
    assert len(rows) == 1
    assert rows[0][2] == "notice" and "kiro-cli 3.1.0 is older than 3.4.0" in rows[0][3]
