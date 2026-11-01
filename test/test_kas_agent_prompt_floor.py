"""KAS below kiro-cli 2.33.1 drops a custom agent's prompt: warn, never refuse.

KAS 0.66.43 (kiro-cli 2.32.0) dropped the prompt of every ``customAgents`` entry,
the crewmate worker's included; KAS 0.66.58 (kiro-cli 2.33.1) applies it. The
relay spawn records the notice for the release it launched, a session that
registers a ``customAgents`` batch carries it, and a new chat shows it in the row
the steer floor (``steer_version_issue``) uses. The chat still runs and the card is unchanged.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.acp import client as client_mod
from kiro_crew.acp import kas_bundle as kas_bundle_install
from kiro_crew.acp.harness import kas as kas_harness
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.agent_sdk.backend_cards import card_payload
from kiro_crew.dashboard import chat_runner
from kiro_crew.providers.acp import AcpProvider

KAS_FRAMES = Path(__file__).resolve().parent / "fixtures" / "acp_frames" / "kas"
#: A custom agent bound with customAgents + set_mode on KAS 0.66.43 (kiro-cli 2.32.0).
OLD_CAPTURE = KAS_FRAMES / "saved-spec-live.jsonl"
#: The crewmate worker kit, whose added prompt KAS 0.66.58 (kiro-cli 2.33.1) followed.
NEW_CAPTURE = KAS_FRAMES / "worker-kit-live.jsonl"
AGENTS = [{"id": "kirocrew"}]


def _header(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8").splitlines()[0])["_meta"]


def _capture_kiro_cli(path: Path) -> tuple[int, int, int]:
    """The kiro-cli release a capture was taken on, read from its header."""
    meta = _header(path)
    text = meta.get("kiro_cli_version") or re.search(r"kiro-cli (\d+\.\d+\.\d+)", meta["note"])[1]
    return tuple(int(part) for part in text.split("."))  # type: ignore[return-value]


OLD, NEW = _capture_kiro_cli(OLD_CAPTURE), _capture_kiro_cli(NEW_CAPTURE)


def test_the_captures_are_the_measured_engines():
    assert (_header(OLD_CAPTURE)["agent_version"], OLD) == ("0.66.43", (2, 32, 0))
    assert (_header(NEW_CAPTURE)["agent_version"], NEW) == ("0.66.58", (2, 33, 1))


def test_the_old_capture_warns_with_installed_minimum_and_fix():
    issue = kas_harness.kas_agent_prompt_issue(OLD)
    assert "kiro-cli 2.32.0 is older than 2.33.1" in issue
    assert "'kiro-cli update'" in issue


@pytest.mark.parametrize("version", [NEW, (2, 35, 1), None])
def test_the_new_capture_and_an_unknown_version_are_silent(version):
    assert kas_harness.kas_agent_prompt_issue(version) == ""


def _relay(monkeypatch, tmp_path, version):
    async def _bin(**_kw):
        return str(tmp_path / "kiro-cli")

    async def _no_vault():
        return False

    monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: False)
    monkeypatch.setattr(kas_harness, "installed_kiro_cli_version", lambda: version)
    monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", _bin)
    monkeypatch.setattr(kas_harness, "vault_holds_identity_off_loop", _no_vault)


def _ctx(tmp_path):
    return SpawnContext(agent="a", work_dir=str(tmp_path), model=None, environ={}, home=tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(("version", "warns"), [(OLD, True), (NEW, False)])
async def test_the_relay_spawn_records_the_notice_for_its_release(
    monkeypatch, tmp_path, version, warns
):
    harness = kas_harness.KasHarness()
    _relay(monkeypatch, tmp_path, version)
    plan = await harness.resolve_spawn(_ctx(tmp_path))
    assert plan.argv
    assert bool(harness.agent_prompt_notice) is warns


@pytest.mark.asyncio
async def test_a_bundle_respawn_clears_what_a_relay_spawn_recorded(monkeypatch, tmp_path):
    """Crew's own engine is not described by kiro-cli's release."""
    harness = kas_harness.KasHarness()
    _relay(monkeypatch, tmp_path, OLD)
    await harness.resolve_spawn(_ctx(tmp_path))
    assert harness.agent_prompt_notice
    bundle = SimpleNamespace(
        version="0.66.33", node_version="22", node_bin=tmp_path / "node", acp_server=tmp_path / "s"
    )
    monkeypatch.setattr(kas_harness, "_bundle_route_selected", lambda: True)
    monkeypatch.setattr(kas_bundle_install, "installed_bundle", lambda: bundle)
    await harness.resolve_spawn(_ctx(tmp_path))
    assert harness.agent_prompt_notice == ""


@pytest.mark.parametrize(("agents", "warns"), [(AGENTS, True), ([], False), (None, False)])
def test_a_session_carries_it_only_when_it_registered_custom_agents(agents, warns):
    harness = kas_harness.KasHarness()
    harness.agent_prompt_notice = kas_harness.kas_agent_prompt_issue(OLD)
    handle = SimpleNamespace()
    harness.record_session_projection(handle, agents, "kirocrew")
    assert bool(handle.kas_agent_prompt_notice) is warns


def test_the_provider_reads_the_session_record():
    notice = kas_harness.kas_agent_prompt_issue(OLD)
    session = AcpSessionProvider.__new__(AcpSessionProvider)
    session._handle = SimpleNamespace(kas_agent_prompt_notice=notice)
    assert session.kas_agent_prompt_notice == notice
    read = AcpProvider.agent_prompt_upgrade_notice.fget
    assert read(SimpleNamespace(_client=session)) == notice
    assert read(SimpleNamespace(_client=SimpleNamespace())) == ""


def _surface(monkeypatch, **client):
    rows = []
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *args, **kw: rows.append(args))
    chat_runner._notice_steer_upgrade(object(), object(), SimpleNamespace(**client))
    return rows


def test_a_new_session_below_the_floor_gets_one_notice_row(monkeypatch):
    notice = kas_harness.kas_agent_prompt_issue(OLD)
    rows = _surface(monkeypatch, steer_upgrade_notice="", agent_prompt_upgrade_notice=notice)
    assert len(rows) == 1
    assert rows[0][2] == "notice"
    assert "2.33.1" in rows[0][3] and "kiro-cli update" in rows[0][3]


@pytest.mark.parametrize("notice", ["", None, object()])
def test_no_row_at_or_above_the_floor(monkeypatch, notice):
    assert _surface(monkeypatch, agent_prompt_upgrade_notice=notice) == []


def test_the_card_is_unchanged(monkeypatch):
    before = card_payload("kas")
    monkeypatch.setattr(kas_harness, "installed_kiro_cli_version", lambda: OLD)
    assert card_payload("kas") == before
    assert all(line["available"] for line in before["capabilities"])
