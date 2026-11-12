"""A restart ends a chat's approval mode, and the owner is told so.

Trust and Read live in memory only, so a restarted gateway rebuilds every chat
without them. That is the design, and nothing here restores a mode. What these
tests pin is the notice: the shutdown save every restart runs, and the periodic
flush that a crash leaves behind, record which open chats held a mode, and the
next boot's restore posts a bell note and a chat row for each one that came back
without it.

A restart here is two dashboard states over one data home. The first boots
through the gateway's restore seam, gets Trust on a chat and stops the way a
stopping gateway does; the second boots through the same seam.

A chat whose tools still run unasked with nobody acting is owed no notice: an
app-owned chat, whose app sets its mode again, a chat a live auto-approve grant
reaches, and a crewmate's chat, whose Profile seeds its mode again at its next
turn.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from test_mate_worker_dispatch import _create, _fresh_create_budget, crewmate  # noqa: F401

from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
from kiro_crew.dashboard import chat_persistence
from kiro_crew.dashboard import server as srv
from kiro_crew.dashboard.chat_handlers import api_chat_mode
from kiro_crew.dashboard.chat_utils import _history_key_for, slot_history_key
from kiro_crew.dashboard.crewmate_approval import seed_for_turn
from kiro_crew.execution_context import read_session_execution
from kiro_crew.safety_override import safety_override
from kiro_crew.testing.wait import await_until

KEY = "chat-1-nightly"
TITLE = "Nightly monitor loop"
#: Where the gateway records which chats held a mode (``chat_trust_restart``).
RECORD_FILE = "chat_trust_held.json"


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        dashboard=SimpleNamespace(
            restore_window_minutes=30,
            restore_sessions=True,
            idle_slot_sweep_days=0,
            surface_channel_sessions=False,
        )
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data home holding one open chat with a transcript."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    seed = _make_state(tmp_path / "sessions")
    seed.conversation_log.append(_history_key_for(KEY), "user", "Check the nightly job.")
    seed.conversation_log.append(_history_key_for(KEY), "assistant", "On it.")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": [KEY], "ts": 0.0}), encoding="utf-8"
    )
    return tmp_path


async def _boot(home: Path):
    """A gateway's boot: a fresh state restored through the dashboard restore seam."""
    state = _make_state(home / "sessions")
    await await_until(
        asyncio.ensure_future(srv._restore_dashboard_sessions(state, _cfg())),
        what="dashboard restore",
    )
    assert state.get_slot(KEY) is not None, "the open chat was not restored"
    return state


async def _booted_with_mode(home: Path, mode: str):
    state = await _boot(home)
    slot = state.get_slot(KEY)
    slot.title = TITLE
    slot._titled = True
    setattr(slot, "_trust" if mode == "trust" else "_trust_reads", True)
    return state


def _notes_for(state) -> list[dict]:
    return [n for n in state._notification_log if n.get("slot") == KEY]


def _rows_about_mode(state) -> list[dict]:
    return [
        m
        for m in state.get_slot(KEY).messages
        if m.get("role") == "system" and "restarted" in m.get("content", "")
    ]


def _arm_startup_grant(state, grant: str) -> None:
    """Arm the grant a boot arms before its restore: a declared one, or the launch one.

    ``agent.dangerously_skip_permissions`` declares the first, ``gateway --approval
    yolo`` the second (``_apply_startup_yolo``, run by ``start_dashboard``).
    """
    agent = SimpleNamespace(dangerously_skip_permissions=grant == "declared", yolo_duration="6h")
    launch = "yolo" if grant == "launch" else None
    with patch("kiro_crew.safety_override.sel"):
        srv._apply_startup_yolo(state, SimpleNamespace(agent=agent), approval_mode=launch)
    assert safety_override().is_active() is True


async def _settle(state) -> None:
    pending = getattr(state, "last_notification_persist", None)
    if pending is not None:
        await await_until(asyncio.ensure_future(pending), what="note persist")


class TestTheRestartIsReported:
    @pytest.mark.asyncio
    async def test_a_trusted_chat_restored_by_a_restart_gets_a_note_and_a_row(
        self, home: Path
    ) -> None:
        before = await _booted_with_mode(home, "trust")
        # Every restart path (the in-place re-exec, ``kirocrew restart``'s SIGTERM,
        # an update) runs this save before the process goes.
        chat_persistence.save_all_slots_to_history(before)

        after = await _boot(home)
        try:
            slot = after.get_slot(KEY)
            # The mode itself does not come back.
            assert slot._trust is False
            [note] = _notes_for(after)
            assert note["kind"] == "agent"
            assert note["title"] == "Trust mode was turned off by a restart"
            # Named by the live chat's own title, never by text from the record.
            assert f'"{slot.display_title}"' in note["body"]
            assert "restarted" in note["body"] and "approval" in note["body"]
            assert note["approval_mode"] == "trust"
            [row] = _rows_about_mode(after)
            assert row["content"].startswith("Trust mode was turned off")
            # The dashboard draws its localized card, with the re-pick, off these.
            assert row["meta"]["kind"] == "trust_reset_notice"
            assert row["meta"]["approval_mode"] == "trust"
        finally:
            await _settle(after)

    @pytest.mark.asyncio
    async def test_a_crash_after_the_periodic_flush_is_reported_too(self, home: Path) -> None:
        before = await _booted_with_mode(home, "trust")
        # No shutdown save: the process dies after a flush tick.
        before._flush_dirty_slots()

        after = await _boot(home)
        try:
            assert after.get_slot(KEY)._trust is False
            assert len(_notes_for(after)) == 1
            assert len(_rows_about_mode(after)) == 1
        finally:
            await _settle(after)

    @pytest.mark.asyncio
    async def test_read_mode_is_reported_by_its_own_name(self, home: Path) -> None:
        before = await _booted_with_mode(home, "trust_reads")
        chat_persistence.save_all_slots_to_history(before)

        after = await _boot(home)
        try:
            assert after.get_slot(KEY)._trust_reads is False
            [note] = _notes_for(after)
            assert note["title"] == "Read mode was turned off by a restart"
            assert note["approval_mode"] == "trust_reads"
        finally:
            await _settle(after)

    @pytest.mark.asyncio
    async def test_one_restart_is_reported_once(self, home: Path) -> None:
        before = await _booted_with_mode(home, "trust")
        chat_persistence.save_all_slots_to_history(before)
        after = await _boot(home)
        await _settle(after)
        assert len(_rows_about_mode(after)) == 1
        # The boot took the record, and this gateway never trusted the chat, so
        # its own stop records nothing.
        assert not (home / RECORD_FILE).exists()
        chat_persistence.save_all_slots_to_history(after)
        assert not (home / RECORD_FILE).exists()

        again = await _boot(home)
        # The row the first report wrote is on disk; a second report would add one.
        assert len(_rows_about_mode(again)) == 1


class TestNothingIsOwed:
    @pytest.mark.asyncio
    async def test_a_chat_that_never_held_a_mode_gets_no_notice(self, home: Path) -> None:
        before = await _boot(home)
        chat_persistence.save_all_slots_to_history(before)

        after = await _boot(home)
        assert _notes_for(after) == []
        assert _rows_about_mode(after) == []

    @pytest.mark.asyncio
    async def test_trust_turned_off_before_the_restart_gets_no_notice(self, home: Path) -> None:
        before = await _booted_with_mode(home, "trust")
        before._flush_dirty_slots()
        before.get_slot(KEY)._trust = False
        chat_persistence.save_all_slots_to_history(before)

        after = await _boot(home)
        assert _notes_for(after) == []

    @pytest.mark.asyncio
    async def test_a_state_that_did_not_boot_records_nothing(self, home: Path) -> None:
        # The record is written only after a boot took the previous one, so a
        # state built any other way cannot overwrite a record nobody has read.
        state = _make_state(home / "sessions")
        chat_persistence.restore_open_slots(state)
        state.get_slot(KEY)._trust = True
        state._flush_dirty_slots()
        chat_persistence.save_all_slots_to_history(state)
        assert not (home / RECORD_FILE).exists()

    @pytest.mark.parametrize("grant", ["declared", "launch"])
    @pytest.mark.asyncio
    async def test_a_chat_a_live_auto_approve_grant_reaches_gets_no_notice(
        self, home: Path, grant: str
    ) -> None:
        # Trust on the chat, then the operator declares auto-approve and restarts:
        # the grant is back before the restore, so the chat's tools ask for nothing.
        # A note saying they ask would be false, and its remedy (Trust on this one
        # chat) would end a declared grant for every other chat.
        before = await _booted_with_mode(home, "trust")
        chat_persistence.save_all_slots_to_history(before)

        _arm_startup_grant(before, grant)
        after = await _boot(home)
        slot = after.get_slot(KEY)
        assert slot._trust is False
        assert after.is_yolo_active() and after.yolo_reaches(slot)
        assert _notes_for(after) == []
        assert _rows_about_mode(after) == []

    @pytest.mark.asyncio
    async def test_a_chat_the_launch_grant_does_not_reach_is_still_told(self, home: Path) -> None:
        # ``gateway --approval yolo`` leaves a channel-linked chat asking, so that
        # chat is still owed the notice.
        from kiro_crew.dashboard import chat_trust_restart

        state = await _boot(home)
        _arm_startup_grant(state, "launch")
        slot = state.get_slot(KEY)
        slot._slack_channel = "C1"
        assert state.is_yolo_active() and not state.yolo_reaches(slot)

        await chat_trust_restart.report(state, (chat_trust_restart.HeldMode(KEY, "trust"),))
        await _settle(state)
        assert len(_notes_for(state)) == 1
        assert len(_rows_about_mode(state)) == 1

    @pytest.mark.asyncio
    async def test_an_app_owned_chat_is_not_recorded(self, home: Path) -> None:
        # The app that owns the chat sets its mode again after the restart, so
        # telling the owner to turn it back on would be wrong.
        before = await _booted_with_mode(home, "trust")
        before.get_slot(KEY)._app = "an-app"
        before._flush_dirty_slots()
        chat_persistence.save_all_slots_to_history(before)
        assert not (home / RECORD_FILE).exists()


def _set_profile_mode(home: Path, mode: str) -> None:
    """Write ``agents.pixel.approval_mode``, the setting the Profile tab writes."""
    path = home / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["agents"]["pixel"]["approval_mode"] = mode
    path.write_text(json.dumps(config), encoding="utf-8")
    _invalidate_config_cache()


async def _restored_crewmate_chat(state, thread, which: str):
    """The crewmate's conductor thread or one of its workers, as a restart leaves it."""
    chat = thread
    if which == "worker":
        chat = state.get_slot((await asyncio.to_thread(_create, state, thread))["target"])
    # Rebuilt with no mode, and its Profile not seeded into it yet.
    chat._trust = chat._trust_reads = False
    chat._member_approval_seeded = False
    return chat


class TestACrewmatesProfileGivesTheModeBack:
    @pytest.mark.parametrize("which", ["conductor", "worker"])
    @pytest.mark.parametrize(
        "profile, owed",
        [("", False), ("trust", False), ("trust_reads", True), ("normal", True)],
    )
    @pytest.mark.asyncio
    async def test_a_crewmate_chat_is_told_only_when_its_profile_does_not_seed_trust(
        self, crewmate, tmp_path: Path, which: str, profile: str, owed: bool  # noqa: F811
    ) -> None:
        # Imported here, so the other tests still run on a tree without the module.
        from kiro_crew.dashboard import chat_trust_restart

        state, thread, _execution = crewmate
        await asyncio.to_thread(_set_profile_mode, tmp_path, profile)
        chat = await _restored_crewmate_chat(state, thread, which)

        held = (chat_trust_restart.HeldMode(chat.key, "trust"),)
        await chat_trust_restart.report(state, held)
        await _settle(state)
        notes = [n for n in state._notification_log if n.get("slot") == chat.key]
        assert len(notes) == (1 if owed else 0)

        # The chat's next turn start seeds its Profile's mode: Trust is back
        # exactly when no notice was posted.
        config = await asyncio.to_thread(KiroCrewConfig.load)
        execution = await asyncio.to_thread(
            read_session_execution, slot_history_key(chat), required=True
        )
        assert seed_for_turn(state, chat, config, execution)
        assert chat._trust is (not owed)


@web.middleware
async def _owner(request: web.Request, handler):
    request["app"] = ""
    request["user"] = "local-app"
    return await handler(request)


async def _pick(state, slot_key: str, mode: str) -> None:
    """The notice's Turn Trust back on button: the picker's own ``POST /api/chat/mode``."""
    app = web.Application(middlewares=[_owner])
    app["state"] = state
    app.router.add_post("/api/chat/mode", api_chat_mode)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/mode", json={"mode": mode, "slot": slot_key})
        assert resp.status == 200, await resp.text()


class TestTrustAgainIsThePersonsPick:
    @pytest.mark.asyncio
    async def test_the_notice_grants_nothing_and_its_pick_raises_the_verified_worker(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.crew_log import session_tree_projection
        from kiro_crew.dashboard import chat_trust_restart
        from kiro_crew.dashboard import session_control as sc
        from kiro_crew.dashboard.slot_persistence.metadata_codec import _read_created_by

        state = await _boot(home)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.owner_id = ""
        lead = state.get_slot(KEY)
        # A worker the lead dispatched, restored the way a restart restores it: its
        # creator claim confirmed by the crew log, following its creator again.
        worker = state.get_or_create_slot("chat-2-worker")
        monkeypatch.setattr(sc, "_creation_parent", lambda key: KEY)
        monkeypatch.setattr(
            session_tree_projection,
            "creator_of",
            lambda key: KEY if key == worker.key else "",
        )
        _read_created_by(SimpleNamespace(meta={"created_by": KEY}, slot=worker))
        assert worker._created_by == KEY and worker._posture_inherited is True

        await chat_trust_restart.report(state, (chat_trust_restart.HeldMode(KEY, "trust"),))
        await _settle(state)
        [row] = _rows_about_mode(state)
        assert row["meta"]["kind"] == "trust_reset_notice"
        assert row["meta"]["approval_mode"] == "trust"
        # The notice alone turns nothing on, for the lead or the worker it created.
        assert lead._trust is False and worker._trust is False
        assert state.sessions.get_approval_policy(slot_history_key(lead)) != "auto"

        await _pick(state, KEY, row["meta"]["approval_mode"])
        assert lead._trust is True
        # The picker's walk re-raised the worker on its recorded creation edge.
        assert worker._trust is True
