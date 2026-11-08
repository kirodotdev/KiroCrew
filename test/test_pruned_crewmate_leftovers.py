"""The two prunes run the crew delete's teardown on the crewmates they remove.

``POST /api/agents/sync`` prunes a package crewmate whose spec is gone, and the
one-time startup pass removes a never-chatted crewmate an older sync generated.
Both run ``_remove_member_leftovers``, the teardown ``DELETE /api/agents/{name}``
runs: the avatar, the goal check-ins, the thread session, the worktrees and the
private template copy go with the row. A crewmate that stays keeps all of its own.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import crewmate_worktree
from kiro_crew.agent_discovery import AgentInfo
from kiro_crew.config import config_dir
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.crewmate_prune_migration import PruneReport
from kiro_crew.cron import CronService
from kiro_crew.dashboard import goal_reminder
from kiro_crew.dashboard.agent_admin import avatars
from kiro_crew.member_goals import CADENCES
from kiro_crew.members import member_slug, member_thread_session_alias
from kiro_crew.session_map import SessionMap

GONE = "omni-old"
STAYS = "omni-live"


def _stage_avatar(name: str) -> None:
    avatar_dir = avatars._avatars_dir()
    avatar_dir.mkdir(parents=True, exist_ok=True)
    (avatar_dir / f"{avatars._avatar_stem(name)}.pending.png").write_bytes(b"\x89PNG")


def _add_check_ins(svc: CronService, slug: str) -> None:
    for cadence in CADENCES:
        svc.add_managed_job(
            goal_reminder.managed_key(slug, cadence),
            name=f"{slug} {cadence}",
            message="x",
            cron_expr="0 9 * * *",
        )


def _job_names() -> list[str]:
    jobs = CronService(base_dir=config_dir()).list_jobs(include_disabled=True)
    return sorted(j.name for j in jobs)


def _seed(names: list[str], *, keep_rows: list[str]) -> CronService:
    """Rows for *keep_rows*; a staged avatar and goal check-ins for every name."""
    cfg = KiroCrewConfig.load()
    for name in names:
        cfg.agents[name] = KiroCrewAgentConfig(kiro_agent=name, source="package")
    slugs = {name: member_slug(name, cfg) for name in names}
    for name in names:
        if name not in keep_rows:
            del cfg.agents[name]
    cfg.save()
    svc = CronService(base_dir=config_dir())
    svc.add_job("unrelated", "do it", every_secs=3600)
    for name in names:
        _stage_avatar(name)
        _add_check_ins(svc, slugs[name])
    return svc


class _Sessions:
    """The session manager's ``destroy``, recorded, over a map holding each name's thread."""

    def __init__(self, names: list[str]) -> None:
        self._session_map = SessionMap()
        for n, name in enumerate(names):
            # A package crewmate with no member_id: its slug is its name.
            self._session_map.set(member_thread_session_alias(name), f"sid-{n}")
        self.destroyed: list[str] = []

    async def destroy(self, key: str) -> None:
        self.destroyed.append(key)


def _expected_jobs(kept: str) -> list[str]:
    slug = member_slug(kept, KiroCrewConfig.load())
    return sorted(["unrelated", *(f"{slug} {c}" for c in CADENCES)])


@pytest.mark.asyncio
async def test_agent_sync_prune_removes_the_pruned_crews_leftovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard.handlers import agents

    svc = _seed([GONE, STAYS], keep_rows=[GONE, STAYS])
    worktree_calls: list[str] = []

    def _remove_for_member(name, _cfg):
        worktree_calls.append(name)
        return [], []

    monkeypatch.setattr(crewmate_worktree, "remove_for_member", _remove_for_member)
    sessions = _Sessions([GONE, STAYS])
    request = MagicMock()
    request.get.return_value = "dashboard"
    request.app = {"state": SimpleNamespace(crons=svc, sessions=sessions)}
    installed = AgentInfo(
        name=STAYS,
        filename=f"{STAYS}.json",
        description="",
        model="auto",
        source="package",
    )

    with (
        patch.object(agents, "list_agents", return_value=[installed]),
        patch.object(agents, "_sel", return_value=MagicMock()),
    ):
        response = await agents._do_agents_sync(request)

    assert response.status == 200
    assert GONE not in KiroCrewConfig.load().agents
    assert STAYS in KiroCrewConfig.load().agents
    assert avatars._pending_avatar_path(GONE) is None
    assert avatars._pending_avatar_path(STAYS) is not None
    assert _job_names() == _expected_jobs(STAYS)
    assert worktree_calls == [GONE]
    assert sessions.destroyed == [member_thread_session_alias(GONE)]


@pytest.mark.asyncio
async def test_startup_prune_removes_the_removed_crewmates_leftovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard import server

    # The pass has already deleted GONE's row; STAYS was kept.
    svc = _seed([GONE, STAYS], keep_rows=[STAYS])
    worktree_calls: list[str] = []

    def _remove_for_member(name, _cfg):
        worktree_calls.append(name)
        return [], []

    monkeypatch.setattr(crewmate_worktree, "remove_for_member", _remove_for_member)
    monkeypatch.setattr(
        server, "prune_synced_crewmates", lambda *_a, **_k: PruneReport(removed=[GONE])
    )
    state = SimpleNamespace(
        conversation_log=None,
        crewmate_prune_abandon=threading.Event(),
        crewmate_prune_settled=asyncio.Event(),
        _background_tasks=set(),
        crons=svc,
        sessions=_Sessions([GONE, STAYS]),
    )

    server._kick_crewmate_prune(state)
    await asyncio.wait_for(state.crewmate_prune_settled.wait(), timeout=30)

    assert avatars._pending_avatar_path(GONE) is None
    assert avatars._pending_avatar_path(STAYS) is not None
    assert _job_names() == _expected_jobs(STAYS)
    assert worktree_calls == [GONE]
    assert state.sessions.destroyed == [member_thread_session_alias(GONE)]


@pytest.mark.asyncio
async def test_startup_teardown_skips_a_name_made_again_since_the_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard import server

    # The pass removed GONE, and a new crewmate took the name before the teardown.
    svc = _seed([GONE], keep_rows=[GONE])
    worktree_calls: list[str] = []
    monkeypatch.setattr(
        crewmate_worktree,
        "remove_for_member",
        lambda name, _cfg: worktree_calls.append(name) or ([], []),
    )
    monkeypatch.setattr(
        server, "prune_synced_crewmates", lambda *_a, **_k: PruneReport(removed=[GONE])
    )
    state = SimpleNamespace(
        conversation_log=None,
        crewmate_prune_abandon=threading.Event(),
        crewmate_prune_settled=asyncio.Event(),
        _background_tasks=set(),
        crons=svc,
    )

    server._kick_crewmate_prune(state)
    await asyncio.wait_for(state.crewmate_prune_settled.wait(), timeout=30)

    assert avatars._pending_avatar_path(GONE) is not None
    assert _job_names() == _expected_jobs(GONE)
    assert worktree_calls == []


@pytest.mark.asyncio
async def test_a_slug_a_live_crew_still_derives_keeps_its_worktrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pruned crew and a surviving one resolve the same slug: the worktrees
    under it are the survivor's, so they stay. The avatar is keyed by name and goes."""
    from kiro_crew.dashboard.handlers import agents
    from kiro_crew.members_parts.member_space import slug_for_name

    svc = _seed([GONE, STAYS], keep_rows=[GONE, STAYS])
    cfg = KiroCrewConfig.load()
    cfg.agents[STAYS].member_id = slug_for_name(GONE)
    cfg.save()
    worktree_calls: list[str] = []
    monkeypatch.setattr(
        crewmate_worktree,
        "remove_for_member",
        lambda name, _cfg: worktree_calls.append(name) or ([], []),
    )
    request = MagicMock()
    request.get.return_value = "dashboard"
    request.app = {"state": SimpleNamespace(crons=svc)}
    installed = AgentInfo(
        name=STAYS, filename=f"{STAYS}.json", description="", model="auto", source="package"
    )

    with (
        patch.object(agents, "list_agents", return_value=[installed]),
        patch.object(agents, "_sel", return_value=MagicMock()),
    ):
        response = await agents._do_agents_sync(request)

    assert response.status == 200
    assert GONE not in KiroCrewConfig.load().agents
    assert avatars._pending_avatar_path(GONE) is None
    assert worktree_calls == []
