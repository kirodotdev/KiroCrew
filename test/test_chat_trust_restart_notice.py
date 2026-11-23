"""A chat's Trust or Read is kept across a gateway exit the user did not choose.

The shutdown save every restart runs, the periodic flush a crash leaves behind, and
a write right after each mode change record which open chats hold a mode, in a
signed record the agent cannot reach (``kept_grants``). The next boot gives each
kept chat its mode back and says so in the chat. A stop the user chose ends them:
the next boot restores nothing and posts nothing. A recorded chat that comes back
without its mode (the policy now denies it, its record could not be verified, it is
a crewmate chat or a worker that follows another chat) is told that it was turned
off.

A restart here is two dashboard states over one data home. The first boots through
the gateway's restore seam, gets a mode on a chat and stops the way a stopping
gateway does; the second boots through the same seam as a new process.

A chat whose tools still run unasked with nobody acting is owed no turned-off
notice: an app-owned chat, whose app sets its mode again, a chat a live auto-approve
grant reaches, and a crewmate's chat, whose Profile seeds its mode again at its next
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

from kiro_crew import kept_grants
from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
from kiro_crew.dashboard import chat_persistence, chat_trust_restart
from kiro_crew.dashboard import server as srv
from kiro_crew.dashboard import token_secret
from kiro_crew.dashboard.chat_handlers import api_chat_mode
from kiro_crew.dashboard.chat_utils import _history_key_for, slot_history_key
from kiro_crew.dashboard.crewmate_approval import seed_for_turn
from kiro_crew.execution_context import read_session_execution
from kiro_crew.safety_override import safety_override
from kiro_crew.testing.wait import async_wait_until, await_until

KEY = "chat-1-nightly"
TITLE = "Nightly monitor loop"


def _record(home: Path) -> Path:
    return home / kept_grants.KEPT_GRANTS_LEAF / kept_grants.CHATS_RECORD


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
    """A data home holding one open chat with a transcript, and its own signing key."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(token_secret, "_SECRET", None)
    monkeypatch.setattr(token_secret, "_EPHEMERAL_SECRET", None)
    seed = _make_state(tmp_path / "sessions")
    seed.conversation_log.append(_history_key_for(KEY), "user", "Check the nightly job.")
    seed.conversation_log.append(_history_key_for(KEY), "assistant", "On it.")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": [KEY], "ts": 0.0}), encoding="utf-8"
    )
    return tmp_path


def _new_process() -> None:
    """The module state a fresh gateway image starts with."""
    kept_grants._reset_for_tests()
    chat_trust_restart._reset_for_tests()


async def _boot(home: Path):
    """A gateway's boot: a fresh state restored through the dashboard restore seam."""
    _new_process()
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


def _turned_off_rows(state) -> list[dict]:
    return [
        m for m in _rows_about_mode(state) if m.get("meta", {}).get("kind") == "trust_reset_notice"
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


class TestARestartKeepsTheMode:
    @pytest.mark.asyncio
    async def test_a_restart_keeps_a_trusted_chat_in_trust_and_says_so(self, home: Path) -> None:
        before = await _booted_with_mode(home, "trust")
        # Every restart path (the in-place re-exec, ``kirocrew restart``'s SIGTERM,
        # an update) runs this save before the process goes.
        chat_persistence.save_all_slots_to_history(before)

        after = await _boot(home)
        try:
            slot = after.get_slot(KEY)
            assert slot._trust is True
            # The session's stored policy follows, as a picker pick writes it.
            after.sessions.set_approval_policy.assert_any_call(slot_history_key(slot), "auto")
            [note] = _notes_for(after)
            assert note["kind"] == "agent"
            assert note["title"] == "Trust mode was kept after a restart"
            # Named by the live chat's own title, never by text from the record.
            assert f'"{slot.display_title}"' in note["body"]
            assert note["approval_mode"] == "trust"
            [row] = _rows_about_mode(after)
            assert row["content"].startswith("Trust mode is still on")
            assert _turned_off_rows(after) == []
        finally:
            await _settle(after)

    @pytest.mark.asyncio
    async def test_a_crash_after_the_periodic_flush_keeps_it_too(self, home: Path) -> None:
        before = await _booted_with_mode(home, "trust")
        # No shutdown save: the process dies after a flush tick.
        before._flush_dirty_slots()

        after = await _boot(home)
        try:
            assert after.get_slot(KEY)._trust is True
            assert len(_notes_for(after)) == 1
        finally:
            await _settle(after)

    @pytest.mark.asyncio
    async def test_read_mode_is_kept_by_its_own_name(self, home: Path) -> None:
        before = await _booted_with_mode(home, "trust_reads")
        chat_persistence.save_all_slots_to_history(before)

        after = await _boot(home)
        try:
            slot = after.get_slot(KEY)
            assert slot._trust_reads is True and slot._trust is False
            [note] = _notes_for(after)
            assert note["title"] == "Read mode was kept after a restart"
            assert note["approval_mode"] == "trust_reads"
        finally:
            await _settle(after)

    @pytest.mark.asyncio
    async def test_a_mode_turned_off_just_before_a_crash_is_not_kept(self, home: Path) -> None:
        before = await _booted_with_mode(home, "trust")
        before._flush_dirty_slots()
        assert _record(home).exists()
        # Normal picked, then the process dies before the next flush tick: the
        # write the change scheduled is what the next boot reads.
        before.get_slot(KEY)._trust = False
        await async_wait_until(lambda: not _record(home).exists(), off_loop=True)

        after = await _boot(home)
        assert after.get_slot(KEY)._trust is False
        assert _notes_for(after) == []

    @pytest.mark.asyncio
    async def test_turning_a_mode_off_removes_the_record_before_the_change_returns(
        self, home: Path
    ) -> None:
        before = await _booted_with_mode(home, "trust")
        before._flush_dirty_slots()
        assert _record(home).exists()
        # Nothing yields between the change and the check: a crash here, before the
        # scheduled rewrite, must not find a record that restores the mode.
        before.get_slot(KEY)._trust = False
        assert not _record(home).exists()

        after = await _boot(home)
        assert after.get_slot(KEY)._trust is False
        assert _notes_for(after) == []


class TestAUserStopEndsTheMode:
    @pytest.mark.asyncio
    async def test_a_user_stop_ends_the_mode_and_owes_no_notice(self, home: Path) -> None:
        before = await _booted_with_mode(home, "trust")
        chat_persistence.save_all_slots_to_history(before)
        # ``kirocrew stop``, the desktop Quit or Ctrl+C, before the process goes.
        assert kept_grants.mark_user_stop("cli")

        with patch("kiro_crew.sel.sel") as audit:
            after = await _boot(home)
        slot = after.get_slot(KEY)
        assert slot._trust is False
        assert (slot_history_key(slot), "auto") not in [
            c.args for c in after.sessions.set_approval_policy.call_args_list
        ]
        assert _notes_for(after) == []
        assert _rows_about_mode(after) == []
        assert not _record(home).exists()
        cleared = [
            c.kwargs
            for c in audit.return_value.log_api_access.call_args_list
            if c.kwargs.get("operation") == "mode_change:normal"
        ]
        assert cleared and "cleared_by_user_stop" in cleared[0]["resources"]

    @pytest.mark.asyncio
    async def test_the_next_restart_after_that_keeps_again(self, home: Path) -> None:
        before = await _booted_with_mode(home, "trust")
        chat_persistence.save_all_slots_to_history(before)
        kept_grants.mark_user_stop("cli")
        stopped = await _boot(home)
        stopped.get_slot(KEY)._trust = True
        chat_persistence.save_all_slots_to_history(stopped)

        after = await _boot(home)
        try:
            assert after.get_slot(KEY)._trust is True
        finally:
            await _settle(after)


class TestARecordItCannotTrustKeepsNothing:
    @pytest.mark.parametrize("tamper", ["mode", "unsigned"])
    @pytest.mark.asyncio
    async def test_a_tampered_or_unsigned_record_keeps_nothing(
        self, home: Path, tamper: str
    ) -> None:
        before = await _booted_with_mode(home, "trust_reads")
        chat_persistence.save_all_slots_to_history(before)
        envelope = json.loads(_record(home).read_text(encoding="utf-8"))
        if tamper == "mode":
            envelope["body"]["chats"][0]["mode"] = "trust"
        else:
            del envelope["mac"]
        _record(home).write_text(json.dumps(envelope), encoding="utf-8")

        after = await _boot(home)
        slot = after.get_slot(KEY)
        assert slot._trust is False and slot._trust_reads is False
        assert not _record(home).exists()

    @pytest.mark.asyncio
    async def test_a_policy_that_now_denies_the_mode_keeps_nothing_and_says_so(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = await _booted_with_mode(home, "trust")
        chat_persistence.save_all_slots_to_history(before)

        monkeypatch.setattr(
            "kiro_crew.approval_mode_policy.session_mode_permitted", lambda mode: mode == "normal"
        )
        after = await _boot(home)
        try:
            assert after.get_slot(KEY)._trust is False
            [note] = _notes_for(after)
            assert note["title"] == "Trust mode was turned off by a restart"
            [row] = _turned_off_rows(after)
            assert row["meta"]["approval_mode"] == "trust"
        finally:
            await _settle(after)

    @pytest.mark.asyncio
    async def test_a_restore_the_slot_refuses_leaves_no_auto_policy(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ceiling denies Trust between the restore's check and the slot's own:
        # the chat stays Normal and its session must not keep an "auto" policy.
        before = await _booted_with_mode(home, "trust")
        chat_persistence.save_all_slots_to_history(before)
        monkeypatch.setattr(
            "kiro_crew.dashboard.state_parts.chat_slot.session_mode_permitted",
            lambda mode: mode == "normal",
        )
        after = await _boot(home)
        try:
            slot = after.get_slot(KEY)
            assert slot._trust is False
            assert (slot_history_key(slot), "auto") not in [
                c.args for c in after.sessions.set_approval_policy.call_args_list
            ]
        finally:
            await _settle(after)

    @pytest.mark.asyncio
    async def test_a_deny_landing_as_the_restore_writes_its_policy_clears_it(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = await _booted_with_mode(home, "trust")
        chat_persistence.save_all_slots_to_history(before)
        real_boot_state = chat_trust_restart.restore_or_report

        async def _deny_on_the_policy_write(state, held):
            def _write(key, policy):
                if policy == "auto":
                    # The ceiling denies Trust right as the restore publishes it.
                    monkeypatch.setattr(
                        "kiro_crew.dashboard.state_parts.chat_slot.session_mode_permitted",
                        lambda mode: mode == "normal",
                    )

            state.sessions.set_approval_policy.side_effect = _write
            await real_boot_state(state, held)

        monkeypatch.setattr(chat_trust_restart, "restore_or_report", _deny_on_the_policy_write)
        after = await _boot(home)
        try:
            slot = after.get_slot(KEY)
            assert slot._trust is False
            calls = [c.args for c in after.sessions.set_approval_policy.call_args_list]
            assert calls[-1] == (slot_history_key(slot), "")
        finally:
            await _settle(after)

    @pytest.mark.asyncio
    async def test_a_pick_before_the_restore_survives_a_crash(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = await _booted_with_mode(home, "trust")
        chat_persistence.save_all_slots_to_history(before)

        async def _pick_then_crash(state, held):
            chat_trust_restart.note_mode_pick(state, state.get_slot(KEY))

        with monkeypatch.context() as crash:
            crash.setattr(chat_trust_restart, "restore_or_report", _pick_then_crash)
            await _boot(home)
        after = await _boot(home)
        assert after.get_slot(KEY)._trust is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("before_restore", ["flush", "mode_change"])
    async def test_a_crash_before_the_restore_resolves_keeps_the_record(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, before_restore: str
    ) -> None:
        # The flush tick or a mode change elsewhere (another chat's restore) lands
        # while this chat is still Normal, then the process dies before its restore.
        before = await _booted_with_mode(home, "trust")
        chat_persistence.save_all_slots_to_history(before)

        async def _tick_then_crash(state, held):
            if before_restore == "flush":
                state._flush_dirty_slots()
            else:
                chat_trust_restart.note_trust_changed()

        with monkeypatch.context() as crash:
            crash.setattr(chat_trust_restart, "restore_or_report", _tick_then_crash)
            await _boot(home)
        assert _record(home).exists()
        after = await _boot(home)
        try:
            assert after.get_slot(KEY)._trust is True
        finally:
            await _settle(after)

    @pytest.mark.asyncio
    async def test_a_pick_before_the_restore_stays_won_after_a_flush(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = await _booted_with_mode(home, "trust")
        chat_persistence.save_all_slots_to_history(before)

        async def _pick_flush_then_crash(state, held):
            chat_trust_restart.note_mode_pick(state, state.get_slot(KEY))
            state._flush_dirty_slots()

        with monkeypatch.context() as crash:
            crash.setattr(chat_trust_restart, "restore_or_report", _pick_flush_then_crash)
            await _boot(home)
        after = await _boot(home)
        assert after.get_slot(KEY)._trust is False

    @pytest.mark.asyncio
    async def test_a_pick_made_while_the_boot_restores_wins(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The owner picks Normal on the chat after it came back but before the kept
        # restore reached it: the restore must not put Trust back over that pick.
        before = await _booted_with_mode(home, "trust")
        chat_persistence.save_all_slots_to_history(before)
        real = chat_trust_restart.restore_or_report

        async def _owner_picks_first(state, held):
            chat_trust_restart.note_mode_pick(state, state.get_slot(KEY))
            await real(state, held)

        monkeypatch.setattr(chat_trust_restart, "restore_or_report", _owner_picks_first)
        after = await _boot(home)
        try:
            assert after.get_slot(KEY)._trust is False
        finally:
            await _settle(after)

    @pytest.mark.asyncio
    async def test_a_record_a_denied_restore_left_is_not_kept_for_a_later_boot(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = await _booted_with_mode(home, "trust")
        chat_persistence.save_all_slots_to_history(before)
        with monkeypatch.context() as denied:
            denied.setattr(
                "kiro_crew.approval_mode_policy.session_mode_permitted",
                lambda mode: mode == "normal",
            )
            refused = await _boot(home)
            await _settle(refused)
        # The chat came back Normal and nothing changed it, yet the next flush must
        # not leave the old record behind for a boot whose policy permits Trust.
        assert refused.get_slot(KEY)._trust is False
        refused._flush_dirty_slots()
        assert not _record(home).exists()

        after = await _boot(home)
        assert after.get_slot(KEY)._trust is False

    @pytest.mark.asyncio
    async def test_a_chat_now_on_another_session_keeps_nothing(self, home: Path) -> None:
        before = await _booted_with_mode(home, "trust")
        chat_persistence.save_all_slots_to_history(before)
        body = kept_grants.read_record(kept_grants.CHATS_RECORD, "chats")
        body["chats"][0]["session"] = "dashboard:someone-else"
        kept_grants.write_record(kept_grants.CHATS_RECORD, "chats", body)

        after = await _boot(home)
        try:
            assert after.get_slot(KEY)._trust is False
            assert len(_turned_off_rows(after)) == 1
        finally:
            await _settle(after)

    @pytest.mark.asyncio
    async def test_the_older_unsigned_record_only_reports(self, home: Path) -> None:
        # What an earlier release left at the data-home root: a notice, never a grant.
        (home / chat_trust_restart.LEGACY_RECORD_FILE).write_text(
            json.dumps({"version": 1, "chats": [{"key": KEY, "mode": "trust"}]}),
            encoding="utf-8",
        )
        after = await _boot(home)
        try:
            assert after.get_slot(KEY)._trust is False
            assert len(_turned_off_rows(after)) == 1
            assert not (home / chat_trust_restart.LEGACY_RECORD_FILE).exists()
        finally:
            await _settle(after)


class TestNothingIsOwed:
    @pytest.mark.asyncio
    async def test_a_chat_that_never_held_a_mode_gets_no_notice(self, home: Path) -> None:
        before = await _boot(home)
        chat_persistence.save_all_slots_to_history(before)

        after = await _boot(home)
        assert _notes_for(after) == []
        assert _rows_about_mode(after) == []

    @pytest.mark.asyncio
    async def test_a_state_that_did_not_boot_records_nothing(self, home: Path) -> None:
        # The record is written only after a boot took the previous one, so a
        # state built any other way cannot overwrite a record nobody has read.
        state = _make_state(home / "sessions")
        chat_persistence.restore_open_slots(state)
        state.get_slot(KEY)._trust = True
        state._flush_dirty_slots()
        chat_persistence.save_all_slots_to_history(state)
        assert not _record(home).exists()

    @pytest.mark.parametrize("grant", ["declared", "launch"])
    @pytest.mark.asyncio
    async def test_a_kept_chat_keeps_its_own_trust_beside_a_startup_grant(
        self, home: Path, grant: str
    ) -> None:
        # The chat's own Trust is independent of a grant boot armed before the
        # restore, so it outlasts that grant ending later.
        before = await _booted_with_mode(home, "trust")
        chat_persistence.save_all_slots_to_history(before)

        _arm_startup_grant(before, grant)
        after = await _boot(home)
        try:
            slot = after.get_slot(KEY)
            assert slot._trust is True
            assert after.is_yolo_active() and after.yolo_reaches(slot)
            assert _turned_off_rows(after) == []
        finally:
            await _settle(after)

    @pytest.mark.asyncio
    async def test_a_chat_the_launch_grant_does_not_reach_is_still_told(self, home: Path) -> None:
        # ``gateway --approval yolo`` leaves a channel-linked chat asking, so that
        # chat is still owed the notice.
        state = await _boot(home)
        _arm_startup_grant(state, "launch")
        slot = state.get_slot(KEY)
        slot._slack_channel = "C1"
        assert state.is_yolo_active() and not state.yolo_reaches(slot)

        await chat_trust_restart.restore_or_report(
            state, (chat_trust_restart.HeldMode(KEY, "trust"),)
        )
        await _settle(state)
        assert len(_notes_for(state)) == 1
        assert len(_turned_off_rows(state)) == 1

    @pytest.mark.asyncio
    async def test_an_app_owned_chat_is_not_recorded(self, home: Path) -> None:
        # The app that owns the chat sets its mode again after the restart.
        before = await _booted_with_mode(home, "trust")
        before.get_slot(KEY)._app = "an-app"
        before._flush_dirty_slots()
        chat_persistence.save_all_slots_to_history(before)
        assert not _record(home).exists()


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
        state, thread, _execution = crewmate
        await asyncio.to_thread(_set_profile_mode, tmp_path, profile)
        chat = await _restored_crewmate_chat(state, thread, which)

        held = (chat_trust_restart.HeldMode(chat.key, "trust"),)
        await chat_trust_restart.restore_or_report(state, held)
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

    def test_a_seeded_crewmate_chat_is_recorded_as_not_kept(self) -> None:
        # Its Profile seeds the mode at its next turn, so it is not the owner's to keep.
        slot = SimpleNamespace(_member_approval_seeded=True)
        assert chat_trust_restart._keepable(slot) is False


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


def _lead_and_worker(state, monkeypatch: pytest.MonkeyPatch):
    """The open chat and a worker it dispatched, restored the way a restart restores it."""
    from kiro_crew.crew_log import session_tree_projection
    from kiro_crew.dashboard import session_control as sc
    from kiro_crew.dashboard.slot_persistence.metadata_codec import _read_created_by

    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.owner_id = ""
    lead = state.get_slot(KEY)
    # Its creator claim confirmed by the crew log, following its creator again.
    worker = state.get_or_create_slot("chat-2-worker")
    monkeypatch.setattr(sc, "_creation_parent", lambda key: KEY)
    monkeypatch.setattr(
        session_tree_projection,
        "creator_of",
        lambda key: KEY if key == worker.key else "",
    )
    _read_created_by(SimpleNamespace(meta={"created_by": KEY}, slot=worker))
    assert worker._created_by == KEY and worker._posture_inherited is True
    return lead, worker


class TestTheWorkersFollowTheirCreator:
    @pytest.mark.asyncio
    async def test_a_kept_lead_raises_its_verified_worker_as_a_pick_would(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = await _boot(home)
        lead, worker = _lead_and_worker(state, monkeypatch)
        entry = chat_trust_restart.HeldMode(KEY, "trust", slot_history_key(lead), True)

        await chat_trust_restart.restore_or_report(state, (entry,))
        await _settle(state)
        assert lead._trust is True
        # The picker's walk re-raised the worker on its recorded creation edge.
        assert worker._trust is True
        assert _turned_off_rows(state) == []

    def test_an_inherited_worker_is_recorded_as_not_kept(self) -> None:
        # It follows its creator again, so its own mode is not recorded as kept.
        slot = SimpleNamespace(_posture_inherited=True)
        assert chat_trust_restart._keepable(slot) is False

    @pytest.mark.asyncio
    async def test_the_turned_off_notice_grants_nothing_and_its_pick_does(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = await _boot(home)
        lead, worker = _lead_and_worker(state, monkeypatch)

        await chat_trust_restart.restore_or_report(
            state, (chat_trust_restart.HeldMode(KEY, "trust"),)
        )
        await _settle(state)
        [row] = _turned_off_rows(state)
        assert row["meta"]["approval_mode"] == "trust"
        # The notice alone turns nothing on, for the lead or the worker it created.
        assert lead._trust is False and worker._trust is False
        assert state.sessions.get_approval_policy(slot_history_key(lead)) != "auto"

        await _pick(state, KEY, row["meta"]["approval_mode"])
        assert lead._trust is True
        assert worker._trust is True
