"""Deleting a crewmate destroys its pinned thread's session-map entries.

A crewmate's standing thread is keyed ``dashboard:member-<slug>`` (V1) or
``dashboard:member-<slug>.memory-<store>`` (a private store), in
``session_map.json``, and nothing else clears that entry. ``DELETE
/api/agents/{name}`` destroys it through the live session manager, so a running
process for the thread goes with it. The two prunes run the same step
(``test_pruned_crewmate_leftovers.py``).

Pinned beside the removal: a sibling whose slug only shares a prefix, an
unrelated chat, a slug another live crew still derives, and a delete with no
dashboard state (``kirocrew agent delete``), which touches no session.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard.handlers import agents as agents_mod
from kiro_crew.dashboard.handlers.agents import api_kirocrew_agent_delete
from kiro_crew.session_map import SESSION_MAP_FILENAME, SessionMap

GONE_V2 = "dashboard:member-mate.memory-member-mate-2b2eef2a151f4deba9af8ecae7da93e8"
GONE_V1 = "dashboard:member-mate"
SIBLING = "dashboard:member-mate-0a2620b15f41.memory-member-mate-0a2620b15f41-5805c68c"
UNRELATED = "dashboard:chat-1-1790000000"


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


class _Sessions:
    """The two session-manager calls the delete route makes, over a real map."""

    def __init__(self) -> None:
        self._session_map = SessionMap()
        self.destroyed: list[str] = []

    async def destroy(self, key: str) -> None:
        self.destroyed.append(key)
        self._session_map.delete(key)

    async def refresh_defaults(self) -> None:
        return None


def _seed() -> None:
    from kiro_crew.config import config_dir

    cfg = KiroCrewConfig()
    cfg.agents = {
        "kirocrew": KiroCrewAgentConfig(kiro_agent="kirocrew"),
        "mate": KiroCrewAgentConfig(kiro_agent="kirocrew", member_id="mate"),
        "mate2": KiroCrewAgentConfig(kiro_agent="kirocrew", member_id="mate-0a2620b15f41"),
    }
    cfg.default_agent = "kirocrew"
    cfg.save()
    keys = (GONE_V1, GONE_V2, SIBLING, UNRELATED)
    entries = {key: {"sid": f"sid-{n}"} for n, key in enumerate(keys)}
    path = config_dir() / SESSION_MAP_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


def _delete_request(name: str, state: object):
    request = MagicMock(spec=web.Request)
    request.method = "DELETE"
    request.match_info = {"name": name}
    request.app = {"state": state}
    return request


@pytest.mark.asyncio
async def test_delete_destroys_the_departed_crewmates_thread_entries():
    _seed()
    sessions = _Sessions()
    state = SimpleNamespace(sessions=sessions, crons=None)

    resp = await api_kirocrew_agent_delete(_delete_request("mate", state))

    assert resp.status == 200
    assert sorted(sessions.destroyed) == sorted([GONE_V1, GONE_V2])
    left = set(sessions._session_map._data)
    assert left == {SIBLING, UNRELATED}


@pytest.mark.asyncio
async def test_an_entry_a_fresh_start_left_without_a_sid_goes_too():
    """Fresh start clears the sid and keeps the key: the shape the issue reports."""
    _seed()
    sessions = _Sessions()
    sessions._session_map.clear_sid(GONE_V1)
    assert sessions._session_map.has_hint(GONE_V1)
    assert GONE_V1 not in sessions._session_map.mapped_sids_by_key()
    state = SimpleNamespace(sessions=sessions, crons=None)

    resp = await api_kirocrew_agent_delete(_delete_request("mate", state))

    assert resp.status == 200
    assert GONE_V1 in sessions.destroyed
    assert not sessions._session_map.has_hint(GONE_V1)


@pytest.mark.asyncio
async def test_a_slug_another_live_crew_still_derives_keeps_its_entries(monkeypatch):
    _seed()
    monkeypatch.setattr(agents_mod, "_member_slug_is_claimed", lambda slug: True)
    sessions = _Sessions()
    state = SimpleNamespace(sessions=sessions, crons=None)

    resp = await api_kirocrew_agent_delete(_delete_request("mate", state))

    assert resp.status == 200
    assert sessions.destroyed == []
    assert set(sessions._session_map._data) == {GONE_V1, GONE_V2, SIBLING, UNRELATED}


@pytest.mark.asyncio
async def test_a_failed_destroy_does_not_fail_the_committed_delete():
    _seed()

    class _Failing(_Sessions):
        async def destroy(self, key: str) -> None:
            raise RuntimeError("boom")

    sessions = _Failing()
    state = SimpleNamespace(sessions=sessions, crons=None)

    resp = await api_kirocrew_agent_delete(_delete_request("mate", state))

    assert resp.status == 200
    assert "mate" not in KiroCrewConfig.load().agents


@pytest.mark.asyncio
async def test_leftovers_without_a_session_manager_touch_no_session():
    """The CLI delete passes no manager: the map file is left as it was."""
    from kiro_crew.config import config_dir

    _seed()
    path = config_dir() / SESSION_MAP_FILENAME
    before = path.read_bytes()
    cfg = KiroCrewConfig.load()

    report = await agents_mod._remove_member_leftovers("mate", cfg, "", None, "cli")

    assert not [row for row in report if row[1] == "thread session"]
    assert path.read_bytes() == before
