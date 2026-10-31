"""``/api/milestones*`` and the hook sites that count milestone events."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from body_stream_helpers import attach_body
from chat_test_helpers import _make_ready_kiro_prerequisite
from turn_harness import SlotSpec, TurnScript, run_turn

from kiro_crew import milestones as m
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.dashboard import milestones_api as api

_REAL_POLICY_PERMITS = api._policy_permits
_REAL_MILESTONES_POLICY = m._policy_permits


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Path:
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _floor_monkeypatch.setattr(m, "_catalog_cache", None)
    # No platform context is installed in a unit test; the policy itself is
    # covered by the tests that restore the real check.
    _floor_monkeypatch.setattr(m, "_policy_permits", lambda: True)
    # No backfill probe reads this host's audit log or credentials.
    _floor_monkeypatch.setattr(m, "_probe", lambda signal: False)
    # The policy layer is not under test here; each test that needs it says so.
    _floor_monkeypatch.setattr(api, "_policy_permits", lambda: True)
    return tmp_path


def _request(body: object = None, *, restricted: bool = False) -> MagicMock:
    request = MagicMock()
    state = MagicMock()
    state._restricted_keys = {"dashboard:chat-incognito"} if restricted else set()
    state._slots = {}
    request.app = {"state": state}
    request.headers = {
        "X-Session-Key": "dashboard:chat-incognito" if restricted else "dashboard:ui"
    }
    if body is not None:
        attach_body(request, body)
    return request


def _json(resp: Any) -> dict[str, Any]:
    return json.loads(resp.text)


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_enabled", lambda: True)


class TestGet:
    @pytest.mark.asyncio
    async def test_shape_for_an_enabled_install(self, enabled: None) -> None:
        body = _json(await api.api_milestones_get(_request()))
        assert set(body) == {"enabled", "features", "streak", "groups", "recommended", "notices"}
        assert body["features"] == {"done": 0, "total": 7}
        assert [g["id"] for g in body["groups"]] == ["features", "sessions", "turns", "streak"]
        assert body["recommended"] == ["widgets", "scheduled-task", "mate"]
        assert m.load()["backfilled"] is True
        item = body["groups"][0]["items"][0]
        assert set(item) == {"id", "state", "done_at", "source", "video", "starter", "doc"}
        assert "backfill_when" not in json.dumps(body)

    @pytest.mark.asyncio
    async def test_restricted_session_is_disabled_and_writes_nothing(
        self, enabled: None, _home: Path
    ) -> None:
        body = _json(await api.api_milestones_get(_request(restricted=True)))
        assert body == {"enabled": False}
        assert not (_home / "milestones.json").exists()

    @pytest.mark.asyncio
    async def test_config_switch_off(self) -> None:
        cfg = MagicMock()
        cfg.dashboard.milestones_enabled = False
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg):
            assert _json(await api.api_milestones_get(_request())) == {"enabled": False}

    @pytest.mark.asyncio
    async def test_policy_outranks_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(api, "_policy_permits", _REAL_POLICY_PERMITS)
        monkeypatch.setattr(m, "_policy_permits", _REAL_MILESTONES_POLICY)
        monkeypatch.setattr("kiro_crew.platform.context.installed_context", lambda: object())
        cfg = MagicMock()
        cfg.dashboard.milestones_enabled = True
        with (
            patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.edition_locks.policy_permits", return_value=False) as permits,
        ):
            assert _json(await api.api_milestones_get(_request())) == {"enabled": False}
            resp = await api.api_milestones_mark_done(_request({"id": "slack"}))
        assert resp.status == 403
        assert m.load()["reached"] == {}
        permits.assert_called_with("capabilities.milestones")

    def test_config_key_defaults_on(self) -> None:
        from kiro_crew.config.sections_dashboard import DashboardConfig

        cfg = DashboardConfig()
        assert cfg.milestones_enabled is True


class TestMarking:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["chats-opened", "streak-best", "nope", 5, None])
    async def test_mark_done_refuses_non_feature_ids(self, bad: object) -> None:
        resp = await api.api_milestones_mark_done(_request({"id": bad}))
        assert resp.status == 400
        assert m.load()["reached"] == {}

    @pytest.mark.asyncio
    async def test_mark_done_stamps_server_time_and_manual(self) -> None:
        before = time.time()
        resp = await api.api_milestones_mark_done(_request({"id": "slack", "at": 1.0, "count": 9}))
        assert resp.status == 200
        reached = m.load()["reached"]["slack"]
        assert reached["source"] == "manual" and reached["at"] >= before
        assert "slack" not in m.load()["counters"]

    @pytest.mark.asyncio
    async def test_mark_all_done_and_skip_all(self, enabled: None) -> None:
        assert (await api.api_milestones_skip_all(_request({}))).status == 200
        body = _json(await api.api_milestones_get(_request()))
        assert body["recommended"] == [] and body["features"]["done"] == 0
        assert (await api.api_milestones_mark_all_done(_request({}))).status == 200
        body = _json(await api.api_milestones_get(_request()))
        assert body["features"] == {"done": 7, "total": 7}
        assert {i["source"] for i in body["groups"][0]["items"]} == {"manual"}

    @pytest.mark.asyncio
    async def test_a_failed_write_is_not_acknowledged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_st: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(m, "_save", boom)
        resp = await api.api_milestones_mark_done(_request({"id": "slack"}))
        assert resp.status == 500
        assert _json(resp)["code"] == "write_failed"

    @pytest.mark.asyncio
    async def test_restricted_writes_are_dropped(self, _home: Path) -> None:
        resp = await api.api_milestones_mark_done(_request({"id": "slack"}, restricted=True))
        assert resp.status == 200
        assert not (_home / "milestones.json").exists()


class TestEvent:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {"id": "widgets", "action": "explode"},
            {"id": "chats-opened", "action": "starter_clicked"},
            {"id": "widgets", "action": "skipped", "surface": "elsewhere"},
            {"id": 3, "action": "skipped"},
        ],
    )
    async def test_bad_events_are_refused(self, body: dict) -> None:
        assert (await api.api_milestones_event(_request(body))).status == 400

    @pytest.mark.asyncio
    async def test_skip_and_starter_click_are_recorded(self) -> None:
        await api.api_milestones_event(_request({"id": "mate", "action": "skipped"}))
        await api.api_milestones_event(
            _request({"id": "widgets", "action": "starter_clicked", "surface": "panel"})
        )
        st = m.load()
        assert st["skipped"] == ["mate"]
        assert "widgets" in st["starter_clicked"]


# ── Hook sites ──


class TestArtifactHook:
    def test_counts_after_a_successful_create_only(self, tmp_path: Path) -> None:
        from kiro_crew.artifacts import ArtifactStore

        store = ArtifactStore(root=tmp_path / "artifacts")
        with pytest.raises(Exception):
            store.create(name="bad", content="x", kind="not-a-kind")
        assert "widgets" not in m.load()["counters"]
        art = store.create(name="Calendar", content="<div>hi</div>", kind="widget")
        assert m.load()["counters"]["widgets"]["count"] == 1
        store.delete(art.slug)
        assert m.load()["counters"]["widgets"]["count"] == 1


class TestCronHook:
    def test_counts_after_the_job_is_stored_only(self, tmp_path: Path) -> None:
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path / "cron")
        with pytest.raises(Exception):
            svc.add_job(name="bad", message="m", every_secs=300, timezone="Not/AZone")
        assert "scheduled-task" not in m.load()["counters"]
        job = svc.add_job(name="daily", message="m", cron_expr="0 9 * * *")
        assert m.load()["counters"]["scheduled-task"]["count"] == 1
        svc.remove_job(job.id, actor="test", source="test")
        assert m.load()["counters"]["scheduled-task"]["count"] == 1
        # A job an app registers for itself is not the user scheduling anything.
        svc.add_job(name="app", message="m", every_secs=300, created_by="app:digest")
        assert m.load()["counters"]["scheduled-task"]["count"] == 1


class TestSlotBirthHook:
    def test_counts_person_started_chats_only(self, tmp_path: Path) -> None:
        from kiro_crew.dashboard.state import DashboardState, SlotOrigin
        from kiro_crew.history import ConversationLog

        sessions = MagicMock(count=0)
        sessions.remove = AsyncMock()
        sessions.recycle_background = AsyncMock()
        sessions.get_pid = MagicMock(return_value=None)
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path / "log"),
        )
        state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
        state.get_or_create_slot(origin=SlotOrigin.USER)
        state.get_or_create_slot(origin=SlotOrigin.CRON, count_user_session=True)
        for kwargs in (
            {"memory_mode": "incognito"},
            {"memory_mode": "temporary"},
            {"ephemeral": True},
            {"memory_mode": "persistent", "ephemeral": True},
            {"memory_mode": "incognito", "ephemeral": True},
        ):
            state.get_or_create_slot(origin=SlotOrigin.USER, count_user_session=True, **kwargs)
            assert "chats-opened" not in m.load()["counters"]
        state.get_or_create_slot(origin=SlotOrigin.USER, count_user_session=True)
        assert m.load()["counters"]["chats-opened"]["count"] == 1
        state.get_or_create_slot(
            origin=SlotOrigin.USER, count_user_session=True, memory_mode="persistent"
        )
        assert m.load()["counters"]["chats-opened"]["count"] == 2


class TestRecordWhen:
    def test_counts_only_when_the_probe_fires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        m.record_when("slack", "channel_connected:slack")
        assert "slack" not in m.load()["counters"]
        monkeypatch.setattr(m, "_probe", lambda signal: signal == "channel_connected:slack")
        m.record_when("slack", "channel_connected:slack")
        assert m.load()["counters"]["slack"]["count"] == 1


_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
_REPLY = AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")


class TestTurnHooks:
    """Driven end to end through the turn harness; the counting seam is ``m.record``."""

    @pytest.fixture
    def recorded(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        seen: list[str] = []
        monkeypatch.setattr(m, "record", seen.append)
        monkeypatch.setattr(m, "record_active_day", lambda: seen.append("active-day"))
        return seen

    @pytest.mark.asyncio
    async def test_a_user_turn_counts_once(self, recorded: list[str]) -> None:
        await run_turn(TurnScript(events=[_REPLY, _DONE]))
        assert recorded == ["turns-sent", "active-day"]

    @pytest.mark.asyncio
    async def test_a_channel_user_turn_counts_once(self, recorded: list[str]) -> None:
        result = await run_turn(
            TurnScript(
                events=[_REPLY, _DONE],
                user_row=True,
                run_kwargs={"_directive_channel_origin": True},
            )
        )
        assert result.stop_reason == STOP_REASON_END_TURN
        assert recorded == ["turns-sent", "active-day"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("actor", ["cron", "app", "crew"])
    async def test_a_user_origin_turn_with_a_non_user_actor_counts_nothing(
        self, recorded: list[str], actor: str
    ) -> None:
        # A script cron's ctx.send_to_session reaches /api/chat with
        # _directive_user_origin True but _turn_actor "cron": nobody typed it.
        await run_turn(
            TurnScript(
                events=[_REPLY, _DONE],
                user_row=True,
                run_kwargs={"_directive_user_origin": True, "_turn_actor": actor},
            )
        )
        assert recorded == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel_origin", [False, True])
    async def test_a_turn_nobody_sent_counts_nothing(
        self, recorded: list[str], channel_origin: bool
    ) -> None:
        await run_turn(
            TurnScript(
                events=[_REPLY, _DONE],
                user_row=False,
                run_kwargs={"_directive_channel_origin": channel_origin},
            )
        )
        assert recorded == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("memory_mode", ["incognito", "temporary"])
    async def test_a_restricted_user_turn_counts_nothing(
        self, recorded: list[str], memory_mode: str
    ) -> None:
        result = await run_turn(
            TurnScript(events=[_REPLY, _DONE]), slot=SlotSpec(memory_mode=memory_mode)
        )
        assert result.stop_reason == STOP_REASON_END_TURN
        assert recorded == []

    @pytest.mark.asyncio
    async def test_a_synthetic_recovery_counts_nothing(self, recorded: list[str]) -> None:
        result = await run_turn(
            TurnScript(events=[_REPLY, _DONE], run_kwargs={"_synthetic_recovery_turn": True})
        )
        assert result.stop_reason == STOP_REASON_END_TURN
        assert recorded == []

    @pytest.mark.asyncio
    async def test_a_hook_error_never_breaks_the_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_st: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(m, "_save", boom)
        record = await run_turn(TurnScript(events=[_REPLY, _DONE]))
        assert record.stop_reason == STOP_REASON_END_TURN
        assert record.rows("assistant")[0]["content"] == "ok"


class TestDirectiveHook:
    """``pr-watch`` counts once the gateway really armed the loop."""

    def _apply(self, monkeypatch: pytest.MonkeyPatch, result: object) -> list[str]:
        import asyncio as aio

        from kiro_crew.dashboard import session_directive_apply as sda

        seen: list[str] = []
        monkeypatch.setattr(m, "record", seen.append)

        async def fake_start(*_a: object, **_k: object) -> object:
            if isinstance(result, BaseException):
                raise result
            return result

        monkeypatch.setattr(sda, "_monitor_start", fake_start)
        monkeypatch.setattr(sda, "_audit", lambda *a, **k: None)
        aio.run(
            sda.apply_session_directive_outcome(
                MagicMock(),
                MagicMock(),
                "dashboard:chat-1",
                "monitor_start",
                {},
                producer_is_user_facing=True,
            )
        )
        return seen

    def test_an_armed_loop_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._apply(monkeypatch, "Monitor armed.") == ["pr-watch"]

    def test_an_error_result_counts_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._apply(monkeypatch, "Error: no client attached.") == []

    def test_a_refusal_counts_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.dashboard import session_directive_apply as sda

        monkeypatch.setattr(sda, "_surface_arm_refusal", lambda *a, **k: None)
        assert self._apply(monkeypatch, sda._DirectiveDenied("occupied")) == []
