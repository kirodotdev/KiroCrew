"""The one dashboard note that a stored Kiro CLI pick now runs on the KAS engine.

the a-kiro-cli-choice-runs-on-kas entry in docs/decisions: the user is told once,
after the KAS floor answers, and only when the pick really runs on KAS.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from kiro_crew import kiro_on_kas_notice as notice
from kiro_crew.agent_sdk import backends as registry
from kiro_crew.config import loader, loader_document

KAS = registry.ACP_BACKEND_KAS
KIRO = registry.ACP_BACKEND_KIRO


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(registry, "_baseline", {KIRO, KAS})
    monkeypatch.setattr(registry, "_selectable", {KIRO, KAS})
    monkeypatch.setattr(registry, "_host_unservable", {})
    # The floor's positive verdict, which is what moves a Kiro CLI pick onto KAS.
    monkeypatch.setattr(registry, "_kiro_cli_offers_kas", True)
    return tmp_path


def _stored(monkeypatch, value: bool, *, bundle: bool = False) -> None:
    """Store a real ``config.json`` that holds the Kiro CLI pick, or a KAS one."""
    import json
    import tempfile
    from pathlib import Path

    from kiro_crew.config.loader import _invalidate_config_cache

    agent = {"acp_backend": "kiro" if value else "kas", "kas_bundle": bundle}
    path = Path(tempfile.mkdtemp(dir=loader.config_dir())) / "config.json"
    path.write_text(json.dumps({"agent": agent}), encoding="utf-8")
    monkeypatch.setattr(loader, "config_path", lambda: path)
    monkeypatch.setattr(loader_document, "acp_backend_stored_as_kiro", lambda: value)
    _invalidate_config_cache()


def test_the_note_is_claimed_once(home, monkeypatch):
    _stored(monkeypatch, True)
    note = notice.prepare_note()
    assert note == {"title": notice.NOTE_TITLE, "body": notice.NOTE_BODY, "url": "/settings/agent"}
    assert (home / notice.MARKER_FILENAME).exists()
    assert notice.prepare_note() is None


def test_no_note_when_the_pick_is_not_kiro_cli(home, monkeypatch):
    _stored(monkeypatch, False)
    assert notice.prepare_note() is None
    assert not (home / notice.MARKER_FILENAME).exists()


def test_no_note_on_a_kiro_cli_without_v3(home, monkeypatch):
    """The floor marked KAS unservable, so the pick keeps the v2 launch: nothing to tell."""
    _stored(monkeypatch, True)
    registry.mark_backend_unservable(KAS, "this kiro-cli has no --agent-engine flag")
    assert notice.prepare_note() is None
    assert not (home / notice.MARKER_FILENAME).exists()


def test_no_note_on_the_bundle_route(home, monkeypatch):
    """The bundle route keeps a Kiro CLI pick on kiro-cli, so nothing to tell."""
    _stored(monkeypatch, True, bundle=True)
    assert notice.prepare_note() is None
    assert not (home / notice.MARKER_FILENAME).exists()


def test_no_note_without_a_positive_verdict(home, monkeypatch):
    """An unknown probe never confirmed v3, so the pick is still on kiro-cli."""
    _stored(monkeypatch, True)
    monkeypatch.setattr(registry, "_kiro_cli_offers_kas", False)
    assert notice.prepare_note() is None
    assert not (home / notice.MARKER_FILENAME).exists()


def test_no_note_when_policy_denies_kas(home, monkeypatch):
    _stored(monkeypatch, True)
    registry.apply_selectable_denials({KAS})
    assert notice.prepare_note() is None


def test_delivery_waits_for_the_floor_then_notifies(home, monkeypatch):
    _stored(monkeypatch, True)
    state = MagicMock()

    async def _run():
        floor_done = asyncio.Event()

        async def _floor():
            await floor_done.wait()
            # The floor's verdict: an old kiro-cli. Read only after it lands.
            registry.mark_backend_unservable(KAS, "too old")

        floor = asyncio.ensure_future(_floor())
        delivery = asyncio.ensure_future(notice.deliver_when_floor_lands(state, floor))
        await asyncio.sleep(0)
        assert not delivery.done()
        floor_done.set()
        return await asyncio.wait_for(delivery, timeout=30)

    assert asyncio.run(_run()) is False
    state.notify.assert_not_called()


def test_delivery_after_a_v3_verdict_sends_one_bell_note(home, monkeypatch):
    _stored(monkeypatch, True)
    state = MagicMock()

    async def _run():
        async def _floor():
            return None

        floor = asyncio.ensure_future(_floor())
        first = await asyncio.wait_for(notice.deliver_when_floor_lands(state, floor), timeout=30)
        second = await asyncio.wait_for(notice.deliver_when_floor_lands(state, floor), timeout=30)
        return first, second

    assert asyncio.run(_run()) == (True, False)
    state.notify.assert_called_once_with(
        notice.NOTE_KIND,
        notice.NOTE_TITLE,
        notice.NOTE_BODY,
        url=notice.NOTE_URL,
        channel=notice.NOTE_CHANNEL,
    )


def test_a_failed_floor_sends_nothing(home, monkeypatch):
    """A failed probe confirmed nothing, so the pick stays on kiro-cli and no note goes out."""
    _stored(monkeypatch, True)
    monkeypatch.setattr(registry, "_kiro_cli_offers_kas", False)
    state = MagicMock()

    async def _run():
        async def _floor():
            raise RuntimeError("probe crashed")

        floor = asyncio.ensure_future(_floor())
        return await asyncio.wait_for(notice.deliver_when_floor_lands(state, floor), timeout=30)

    assert asyncio.run(_run()) is False
    state.notify.assert_not_called()


def test_the_panel_flag_reads_the_same_answer(home, monkeypatch):
    from kiro_crew.dashboard.handlers import acp_backend_status

    _stored(monkeypatch, True)
    assert acp_backend_status._kiro_selection_runs_on_kas() is True
    registry.mark_backend_unservable(KAS, "too old")
    assert acp_backend_status._kiro_selection_runs_on_kas() is False


def test_no_offer_when_the_overlay_owns_the_pick(home, monkeypatch):
    """The panel writes config.json; an overlay-owned pick would not change."""
    _stored(monkeypatch, True)
    monkeypatch.setattr(loader_document, "acp_backend_overlay_owned", lambda: True)
    assert notice.kiro_selection_runs_on_kas() is False
    assert notice.prepare_note() is None
    assert not (home / notice.MARKER_FILENAME).exists()


def test_the_overlay_check_reads_config_local(tmp_path, monkeypatch):
    import json

    local = tmp_path / "config.local.json"
    monkeypatch.setattr(loader, "config_local_path", lambda: local)
    assert loader_document.acp_backend_overlay_owned() is False
    local.write_text(json.dumps({"agent": {"streaming": False}}), encoding="utf-8")
    assert loader_document.acp_backend_overlay_owned() is False
    local.write_text(json.dumps({"agent": {"acp_backend": "kiro"}}), encoding="utf-8")
    assert loader_document.acp_backend_overlay_owned() is True
