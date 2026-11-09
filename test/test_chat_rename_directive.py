"""``chat_rename`` — the stateless directive that lets an agent rename ITS OWN chat
session.

The tool validates its one argument and returns a directive; the session-aware
consumer applies it to the slot that produced it. The applier reuses
``session_rename``'s title rules and its refusals for a slot it cannot reach, then
writes through ``chat_title.apply_manual_title``, the sidebar's own rename. These
tests pin the tool's payload, the gates, every refusal leaving the title
unchanged, and that the stored title is final like a manual one.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew import agent as agent_mod
from kiro_crew import (
    crew_mode,
    directive_origin,
    execution_context,
    mate_worker,
    mcp_core,
    session_directive,
)
from kiro_crew.agent_materialization import conductor_agents
from kiro_crew.dashboard import chat_title, create_rate_limit, session_control
from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history
from kiro_crew.dashboard.chat_title import (
    _TITLE_ORIGIN_AGENT,
    _TITLE_ORIGIN_AUTO,
    _TITLE_ORIGIN_MEMBER,
    _TITLE_ORIGIN_USER,
    _refresh_blocked,
    apply_manual_title,
    apply_regenerated_title,
    name_member_thread,
)
from kiro_crew.dashboard.chat_turn.directives import unverified_directive_outcome
from kiro_crew.dashboard.chat_utils import _history_key_for, slot_history_key
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.dashboard.slot_persistence.metadata_values import _rehydrate_title_origin
from kiro_crew.mcp_tool_titles import declared_tool_title
from kiro_crew.sel import SecurityEventLog
from kiro_crew.testing.wait import async_wait_until
from kiro_crew.validation import ValidationError

# ───────────────────────────── the tool ──────────────────────────────────────


class TestChatRenameTool:
    """Stateless: the tool validates its argument and returns a directive. It
    takes no target and resolves no session identity."""

    def test_the_title_round_trips_through_the_directive(self):
        result = mcp_core._call_tool_inner("chat_rename", {"title": "Release notes"})
        assert session_directive.decode(result, "chat_rename") == {"title": "Release notes"}

    def test_the_reply_tells_the_agent_a_hand_set_title_is_kept(self):
        # The agent sees only this reply in its turn, never the applied outcome,
        # so the reply must say a refusal is possible and why.
        result = mcp_core._call_tool_inner("chat_rename", {"title": "Release notes"})
        reply = session_directive.strip_marker(result)
        assert "set by hand is kept" in reply
        assert "requested rather than done" in reply
        assert "this session only" in reply
        assert "not available in a conductor's session" in reply
        for caller in ("cron job", "sub-agent", "task run", "monitor wake", "an app sent"):
            assert caller in reply

    @pytest.mark.parametrize("args", [{}, {"title": ""}, {"title": "   "}])
    def test_a_missing_or_empty_title_is_refused_by_the_schema(self, args):
        with pytest.raises(ValidationError):
            mcp_core._call_tool_inner("chat_rename", args)

    def test_the_largest_title_the_applier_accepts_fits_the_marker(self):
        # An astral character is the costliest JSON escape (a surrogate pair,
        # twelve characters), so 200 of them is the largest encoded title the
        # 200-character rule lets through.
        title = "\U0001f600" * 200
        result = mcp_core._call_tool_inner("chat_rename", {"title": title})
        assert session_directive.decode(result, "chat_rename") == {"title": title}

    def test_an_oversized_title_is_refused_without_a_marker(self):
        result = mcp_core._call_tool_inner("chat_rename", {"title": "\U0001f600" * 500})
        assert not session_directive.has_marker(result)
        assert session_directive.is_refusal(result)
        assert result.startswith("Error: chat_rename arguments are too large")

    def test_the_descriptor_takes_only_a_title(self):
        descriptor = next(t for t in mcp_core._list_tools() if t["name"] == "chat_rename")
        schema = descriptor["inputSchema"]
        assert set(schema["properties"]) == {"title"}
        assert schema["required"] == ["title"]
        assert "session_rename" in descriptor["description"]
        for caller in ("cron jobs", "subagents", "task runs", "monitor wakes", "an app sent"):
            assert caller in descriptor["description"]
        assert "a crewmate thread's name" in descriptor["description"]
        assert declared_tool_title("kirocrew-core", "chat_rename") == "Rename session"

    def test_it_is_a_user_surface_directive_with_a_not_applied_line(self):
        assert "chat_rename" in session_directive.DIRECTIVE_TOOLS
        assert "chat_rename" in directive_origin.USER_SURFACE_DIRECTIVES
        assert "chat_rename" in directive_origin.USER_ORIGIN_DIRECTIVES
        assert unverified_directive_outcome("chat_rename") == "The session title was not changed."


# ───────────────────────────── the applier ───────────────────────────────────


def _persons_turn(slot):
    """Mark *slot*'s running turn as one the person sent, as ``turn_entry`` does."""
    slot._turn_user_sent = True
    slot._turn_channel_origin = False
    slot._turn_channel_narrowed = False


def _apply(state, slot, title, *, session_key=None, person=True, **flags):
    """Apply ``chat_rename``; *person* marks the slot's turn as the person's first."""
    flags.setdefault("producer_is_user_facing", True)
    if person:
        _persons_turn(slot)
    return asyncio.run(
        apply_session_directive(
            state,
            slot,
            session_key or slot_history_key(slot),
            "chat_rename",
            {"title": title},
            **flags,
        )
    )


def _hand_rename(state, slot, title):
    """The sidebar's rename of *slot*, as a person makes it."""
    return apply_manual_title(state, slot, title, route="rename_route", caller="dashboard")


def _fail_persist(monkeypatch):
    async def _no(state, slot):
        return False

    monkeypatch.setattr(chat_title, "_persist_title", _no)


@pytest.fixture
def sel_calls(monkeypatch):
    """Every SEL call the directive makes, recorded on the real logger's class.

    The two methods are replaced, not ``kiro_crew.sel.sel``: the first rename in a
    process imports modules that bind ``sel`` at import, and one imported while
    ``sel`` is replaced keeps the stand-in after the patch is undone.
    """
    fake = MagicMock()
    monkeypatch.setattr(SecurityEventLog, "log_api_access", fake.log_api_access)
    monkeypatch.setattr(SecurityEventLog, "log_tool_invocation", fake.log_tool_invocation)
    return fake


class TestChatRenameApplier:
    def test_a_persons_turn_renames_its_own_slot_as_a_final_title(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        peer = state.get_or_create_slot("chat-2")
        peer.title = "Peer title"
        epoch = slot._title_epoch
        pushed: list = []
        monkeypatch.setattr(state, "push_slot_title", lambda key, t, **kw: pushed.append((key, t)))

        result = _apply(state, slot, "  Release notes  ")

        assert result == 'Session renamed. The sidebar now shows "Release notes".'
        assert slot.title == "Release notes"
        assert slot._titled is True
        assert slot._title_origin == _TITLE_ORIGIN_AGENT
        assert slot._title_epoch == epoch + 1
        assert pushed == [("chat-1", "Release notes")]
        assert peer.title == "Peer title"
        # Final, like a manual rename: the background refresh will not start.
        assert _refresh_blocked(slot) is True

    def test_the_audit_carries_the_length_never_the_title(self, tmp_path, sel_calls):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")

        _apply(state, slot, "Private plan")

        sel_calls.log_api_access.assert_called_once_with(
            caller="mcp-directive",
            operation="chat.self_rename",
            outcome="allowed",
            source="mcp-directive",
            resources=f"chars={len('Private plan')}",
        )
        assert "Private plan" not in repr(sel_calls.log_api_access.mock_calls)
        rows = {
            c.kwargs["tool_name"]: c.kwargs for c in sel_calls.log_tool_invocation.call_args_list
        }
        directive_row = next(v for k, v in rows.items() if k != "chat.manual_rename")
        assert directive_row["outcome"] == "success"
        assert "Private plan" not in repr(directive_row)

    def test_an_applied_rename_writes_the_manual_rename_row(self, tmp_path, sel_calls):
        # Every rename writes chat.manual_rename; this one names its route and
        # the session itself as the caller.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        key = slot_history_key(slot)

        _apply(state, slot, "Release notes")

        [row] = [
            c.kwargs
            for c in sel_calls.log_tool_invocation.call_args_list
            if c.kwargs["tool_name"] == "chat.manual_rename"
        ]
        assert row["session_key"] == key
        assert row["resources"] == "chat-1"
        assert row["metadata"] == {
            "route": "chat_rename",
            "old_title": "chat-1",
            "new_title": "Release notes",
        }

    def test_a_credential_shaped_title_is_redacted_before_it_is_stored(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        secret = "AKIA" + "ABCDEFGHIJKLMNOP"

        result = _apply(state, slot, f"deploy with {secret}")

        assert slot.title.startswith("deploy with ")
        assert secret not in slot.title
        assert secret not in result

    def test_a_failed_save_says_the_title_is_live_but_not_saved(
        self, tmp_path, monkeypatch, sel_calls
    ):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        _fail_persist(monkeypatch)

        result = _apply(state, slot, "Not saved yet")

        assert result.startswith('Error: this session now shows the title "Not saved yet"')
        assert "title_persist_failed" in result
        assert slot.title == "Not saved yet"
        assert slot._title_origin == _TITLE_ORIGIN_AGENT
        # The decision, then the failed save.
        audited = [
            (c.kwargs["outcome"], c.kwargs["resources"])
            for c in sel_calls.log_api_access.call_args_list
        ]
        chars = f"chars={len('Not saved yet')}"
        assert audited == [("allowed", chars), ("error", f"{chars},title_persist_failed")]

    def test_a_close_that_cancels_the_save_leaves_the_decision_audited(
        self, tmp_path, monkeypatch, sel_calls
    ):
        # Closing a session cancels its turn, which can land in the save after the
        # title is on the slot; the allowed decision is already recorded by then.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        saving = threading.Event()

        async def _save_until_cancelled(state, slot):
            saving.set()
            # Parked until the cancel below; nothing ever sets this event.
            await asyncio.Event().wait()
            return True

        monkeypatch.setattr(chat_title, "_persist_title", _save_until_cancelled)
        _persons_turn(slot)

        async def _close_mid_save():
            task = asyncio.create_task(
                apply_session_directive(
                    state,
                    slot,
                    slot_history_key(slot),
                    "chat_rename",
                    {"title": "Closed mid-save"},
                    producer_is_user_facing=True,
                )
            )
            # The rename reaches the save only after the mirror probe's worker
            # thread returns, so wait on the save's own signal, not a turn count.
            await async_wait_until(
                lambda: saving.is_set() or task.done(),
                describe=lambda: f"save reached: {saving.is_set()}, task done: {task.done()}",
            )
            assert saving.is_set(), f"the rename ended before its save: {task.result()!r}"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_close_mid_save())

        assert slot.title == "Closed mid-save"
        sel_calls.log_api_access.assert_called_once_with(
            caller="mcp-directive",
            operation="chat.self_rename",
            outcome="allowed",
            source="mcp-directive",
            resources=f"chars={len('Closed mid-save')}",
        )


class TestChatRenameRefusals:
    @pytest.mark.parametrize(
        "flags",
        [
            pytest.param({"producer_is_user_facing": False}, id="cron_or_subagent"),
            pytest.param(
                {"producer_is_user_facing": False, "producer_is_self_wake": True}, id="wake"
            ),
        ],
    )
    def test_a_turn_no_person_sent_is_refused(self, tmp_path, flags):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")

        result = _apply(state, slot, "Hijacked", **flags)

        assert result.startswith("Error: chat_rename only works from a user-facing session")
        assert slot.title == "chat-1"
        assert slot._titled is False

    @pytest.mark.parametrize("actor", ["cron", "app", "crew"])
    def test_a_marked_turn_from_automation_is_refused(self, tmp_path, actor):
        # A script cron's send_to_session carries the user-origin mark too.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")

        result = _apply(state, slot, "Hijacked", producer_turn_actor=actor)

        assert "(not_user_turn); the session title was not changed." in result
        assert slot.title == "chat-1"
        assert slot._titled is False

    @pytest.mark.parametrize("actor", ["", "user"])
    def test_a_persons_actor_renames(self, tmp_path, actor):
        # "user" is a person's recovered turn.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")

        result = _apply(state, slot, "Release notes", producer_turn_actor=actor)

        assert result == 'Session renamed. The sidebar now shows "Release notes".'
        assert slot._title_origin == _TITLE_ORIGIN_AGENT

    @pytest.mark.parametrize(
        ("flag", "value"),
        [
            pytest.param("_turn_user_sent", False, id="peer_or_app_steer"),
            pytest.param("_turn_channel_narrowed", True, id="channel_steer"),
            pytest.param("_turn_channel_origin", True, id="channel_opened"),
        ],
    )
    def test_a_turn_the_person_did_not_send_alone_is_refused(self, tmp_path, flag, value):
        # The slot has no channel mirror here: after a channel's steer the rename
        # stays refused even once the mirror is gone.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        _persons_turn(slot)
        setattr(slot, flag, value)

        result = _apply(state, slot, "Hijacked", person=False)

        assert "(not_user_turn); the session title was not changed." in result
        assert slot.title == "chat-1"
        assert slot._titled is False

    def test_a_steer_landing_during_the_mirror_probe_is_refused(self, tmp_path, monkeypatch):
        # The second check, after the probe, reads the person check again.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")

        def _probe_then_steer(state_arg, slot_arg):
            slot_arg._turn_user_sent = False
            return False

        monkeypatch.setattr(session_control, "_has_channel_mirror", _probe_then_steer)

        result = _apply(state, slot, "Hijacked")

        assert "(not_user_turn); the session title was not changed." in result
        assert slot.title == "chat-1"
        assert slot._titled is False

    def test_an_unbound_channel_tab_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slack_1786300000.000100")
        slot.channel_origin = True
        assert slot.linked_session_key == "", "precondition: the tab is unbound"

        result = _apply(state, slot, "Renamed", session_key=f"dashboard:{slot.key}")

        assert "(linked_session_target); the session title was not changed." in result
        assert slot.title == "slack_1786300000.000100"
        assert slot._titled is False

    def test_a_channel_tab_with_a_stored_slack_link_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slack_1786300000.000100")
        slot.channel_origin = True
        key = f"dashboard:{slot.key}"
        state.sessions.set_slack_link(key, "1786300000.000100", "C123")

        result = _apply(state, slot, "Renamed", session_key=key)

        assert "(linked_session_target); the session title was not changed." in result
        assert slot.title == "slack_1786300000.000100"
        assert slot._titled is False

    def test_a_tab_a_person_named_like_a_channel_renames(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slack_notes")
        assert slot.channel_origin is False

        result = _apply(state, slot, "Release notes", session_key=f"dashboard:{slot.key}")

        assert result == 'Session renamed. The sidebar now shows "Release notes".'
        assert slot.title == "Release notes"

    @pytest.mark.parametrize("agent", sorted(crew_mode.CONDUCTOR_TEMPLATES))
    def test_a_crew_mode_conductor_session_is_refused(self, tmp_path, agent):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.agent = agent

        result = _apply(state, slot, "Renamed")

        assert "(conductor_session); the session title was not changed." in result
        assert slot.title == "chat-1"
        assert slot._titled is False

    def test_a_chat_stamped_into_crew_mode_on_its_agent_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.agent = "my-custom-conductor"
        setattr(slot, crew_mode.STAMP_FIELD, "my-custom-conductor")

        result = _apply(state, slot, "Renamed")

        assert "(conductor_session); the session title was not changed." in result
        assert slot.title == "chat-1"

    def test_a_crewmate_conductor_chat_is_refused(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        record = SimpleNamespace(
            selection_kind="member",
            template_id=mate_worker.MATE_CONDUCTOR_AGENT,
            member_id="ada",
        )
        monkeypatch.setattr(execution_context, "read_session_execution", lambda key: record)

        result = _apply(state, slot, "Renamed")

        assert "(conductor_session); the session title was not changed." in result
        assert slot.title == "chat-1"
        assert slot._titled is False

    def test_an_unreadable_execution_record_is_refused(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")

        def _unreadable(key):
            raise OSError("record unreadable")

        monkeypatch.setattr(execution_context, "read_session_execution", _unreadable)

        result = _apply(state, slot, "Renamed")

        assert "(conductor_session); the session title was not changed." in result
        assert slot.title == "chat-1"
        assert slot._titled is False

    def test_a_worker_a_crewmate_opened_renames(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        record = SimpleNamespace(
            selection_kind="template", template_id=mate_worker.MATE_WORKER_AGENT, member_id=None
        )
        monkeypatch.setattr(execution_context, "read_session_execution", lambda key: record)

        result = _apply(state, slot, "Release notes")

        assert result == 'Session renamed. The sidebar now shows "Release notes".'

    def test_a_default_agent_chat_renames(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        assert not crew_mode.runs_crew_mode(slot.agent, slot.agent_kind, slot.mode)

        result = _apply(state, slot, "Release notes")

        assert result == 'Session renamed. The sidebar now shows "Release notes".'

    def test_a_slotless_channel_caller_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        result = asyncio.run(
            apply_session_directive(
                state,
                None,
                "slack:C123:456",
                "chat_rename",
                {"title": "Hijacked"},
                producer_is_user_facing=True,
            )
        )
        assert result.startswith("Error: chat_rename targets this turn's chat slot")

    def test_a_replaced_slot_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        old = state.get_or_create_slot("chat-1")
        key = slot_history_key(old)
        state._slots.pop("chat-1")
        new = state.get_or_create_slot("chat-1")
        new.title = "Successor"

        result = _apply(state, old, "Stale", session_key=key)

        assert result.startswith("Error: this session was closed or replaced (target_replaced)")
        assert old.title == "chat-1"
        assert new.title == "Successor"

    @pytest.mark.parametrize(
        ("name", "setup", "code"),
        [
            pytest.param("cron-2c3b2e25", lambda s: None, "unattended_target", id="scheduled"),
            pytest.param("chat-1", lambda s: setattr(s, "_app", "some-app"), "app_scoped_target"),
            pytest.param(
                "chat-1",
                lambda s: setattr(s, "linked_session_key", "slack:1786300000.000100"),
                "linked_session_target",
            ),
        ],
    )
    def test_a_session_rename_cannot_reach_is_refused(self, tmp_path, name, setup, code):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot(name)
        setup(slot)
        before = slot.title

        # A dashboard key, so the user-surface gate admits the turn and the
        # applier's own refusal is the one under test.
        result = _apply(state, slot, "Renamed", session_key=f"dashboard:{name}")

        assert result.startswith("Error: ")
        assert f"({code}); the session title was not changed." in result
        assert slot.title == before
        assert slot._titled is False

    def test_the_mirror_probe_runs_off_the_event_loop(self, tmp_path, monkeypatch):
        # The probe reads the session store under a lock an off-loop writer holds
        # across its disk write, so it must not run on the loop thread.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        probe_threads: list[int] = []
        loop_threads: list[int] = []

        def _probe(state_arg, slot_arg):
            probe_threads.append(threading.get_ident())
            return False

        monkeypatch.setattr(session_control, "_has_channel_mirror", _probe)

        _persons_turn(slot)

        async def _run():
            loop_threads.append(threading.get_ident())
            return await apply_session_directive(
                state,
                slot,
                slot_history_key(slot),
                "chat_rename",
                {"title": "Off the loop"},
                producer_is_user_facing=True,
            )

        result = asyncio.run(_run())

        assert result == 'Session renamed. The sidebar now shows "Off the loop".'
        assert len(probe_threads) == 1
        assert probe_threads[0] != loop_threads[0]

    def test_a_slot_replaced_during_the_mirror_probe_is_refused(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        old = state.get_or_create_slot("chat-1")
        key = slot_history_key(old)
        replacement: list = []

        def _probe_then_replace(state_arg, slot_arg):
            state._slots.pop("chat-1")
            new = state.get_or_create_slot("chat-1")
            new.title = "Successor"
            replacement.append(new)
            return False

        monkeypatch.setattr(session_control, "_has_channel_mirror", _probe_then_replace)

        result = _apply(state, old, "Stale", session_key=key)

        assert result.startswith("Error: this session was closed or replaced (target_replaced)")
        assert old.title == "chat-1"
        assert old._titled is False
        assert replacement[0].title == "Successor"

    def test_a_channel_link_made_during_the_mirror_probe_is_refused(self, tmp_path, monkeypatch):
        # The re-check after the probe runs every slot-side refusal again, not
        # only the identity check.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")

        def _probe_then_link(state_arg, slot_arg):
            slot_arg.linked_session_key = "slack:1786300000.000100"
            return False

        monkeypatch.setattr(session_control, "_has_channel_mirror", _probe_then_link)

        result = _apply(state, slot, "Renamed")

        assert "(linked_session_target); the session title was not changed." in result
        assert slot.title == "chat-1"
        assert slot._titled is False

    def test_a_hand_rename_during_the_mirror_probe_is_kept(self, tmp_path, monkeypatch):
        # The sidebar's rename runs on the loop while the probe is in its thread;
        # it assigns the title before its first await, as modelled here.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")

        def _probe_while_a_person_renames(state_arg, slot_arg):
            slot_arg.title = "Typed by hand"
            slot_arg._titled = True
            slot_arg._title_origin = _TITLE_ORIGIN_USER
            slot_arg._title_epoch += 1
            return False

        monkeypatch.setattr(session_control, "_has_channel_mirror", _probe_while_a_person_renames)

        result = _apply(state, slot, "Agent name")

        assert "(manual_title); the session title was not changed." in result
        assert slot.title == "Typed by hand"
        assert slot._title_origin == _TITLE_ORIGIN_USER

    def test_a_session_mirrored_to_a_channel_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        state.sessions.set_mirror_link(slot_history_key(slot), "C123", "1777.0")

        result = _apply(state, slot, "Renamed")

        assert "(mirrored_target); the session title was not changed." in result
        assert slot.title == "chat-1"

    def test_a_refusal_is_audited_as_denied_with_its_code(self, tmp_path, sel_calls):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot._app = "some-app"

        _apply(state, slot, "Renamed")

        assert sel_calls.log_api_access.call_args.kwargs["outcome"] == "denied"
        assert sel_calls.log_api_access.call_args.kwargs["resources"] == "app_scoped_target"
        assert sel_calls.log_tool_invocation.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.parametrize(
        "title",
        [
            pytest.param("", id="empty"),
            pytest.param("   ", id="blank"),
            pytest.param("t" * 201, id="over_200"),
            pytest.param("line one\nline two", id="control_char"),
            pytest.param("abc\u202edef", id="bidi_control"),
            pytest.param("abc\u2800def", id="blank_glyph"),
        ],
    )
    def test_an_invalid_title_is_refused_and_nothing_changes(self, tmp_path, title):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        epoch = slot._title_epoch

        result = _apply(state, slot, title)

        assert result.startswith("Error: ")
        assert result.endswith("(invalid_title).")
        assert slot.title == "chat-1"
        assert slot._title_epoch == epoch


# ───────────────────────────── whose name it is ──────────────────────────────


class TestChatRenameTitleOwnership:
    """An agent may replace a generated name or its own, never a person's."""

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    def test_an_incognito_or_temporary_session_renames_itself(self, tmp_path, mode):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.memory_mode = mode

        result = _apply(state, slot, "Scratch work")

        assert result == 'Session renamed. The sidebar now shows "Scratch work".'
        assert slot.title == "Scratch work"
        assert slot._title_origin == _TITLE_ORIGIN_AGENT
        assert slot.memory_mode == mode

    def test_a_title_set_by_hand_is_kept(self, tmp_path, sel_calls):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        asyncio.run(_hand_rename(state, slot, "Typed by hand"))

        result = _apply(state, slot, "Agent name")

        assert result.startswith("Error: this session's title was set by a person or a tool")
        assert "the person can rename it in the sidebar" in result
        assert "(manual_title); the session title was not changed." in result
        assert slot.title == "Typed by hand"
        assert slot._title_origin == _TITLE_ORIGIN_USER
        assert sel_calls.log_api_access.call_args.kwargs["outcome"] == "denied"
        assert sel_calls.log_api_access.call_args.kwargs["resources"] == "manual_title"

    def test_a_generated_title_is_replaced(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.title = "Generated name"
        slot._titled = True
        slot._title_origin = _TITLE_ORIGIN_AUTO

        result = _apply(state, slot, "Agent name")

        assert result == 'Session renamed. The sidebar now shows "Agent name".'
        assert slot._title_origin == _TITLE_ORIGIN_AGENT

    def test_its_own_earlier_rename_is_replaced(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")

        _apply(state, slot, "First name")
        result = _apply(state, slot, "Second name")

        assert result == 'Session renamed. The sidebar now shows "Second name".'
        assert slot.title == "Second name"
        assert slot._title_origin == _TITLE_ORIGIN_AGENT

    def test_a_hand_rename_after_the_agent_rename_is_then_kept(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        _apply(state, slot, "Agent name")
        asyncio.run(_hand_rename(state, slot, "Typed by hand"))

        result = _apply(state, slot, "Another agent name")

        assert "(manual_title)" in result
        assert slot.title == "Typed by hand"

    def test_after_regenerate_title_the_agent_may_rename_again(self, tmp_path):
        # "Regenerate title" is the person's way to hand the name back.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        asyncio.run(_hand_rename(state, slot, "Typed by hand"))
        asyncio.run(apply_regenerated_title(state, slot, "Generated name"))

        result = _apply(state, slot, "Agent name")

        assert result == 'Session renamed. The sidebar now shows "Agent name".'
        assert slot._title_origin == _TITLE_ORIGIN_AGENT

    def test_a_crewmate_thread_keeps_the_agent_name_when_reopened(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        assert name_member_thread(state, slot, "Ada") is True
        assert slot._title_origin == _TITLE_ORIGIN_MEMBER

        _apply(state, slot, "Release notes")

        assert slot.title == "Release notes"
        assert name_member_thread(state, slot, "Ada") is False
        assert slot.title == "Release notes"
        assert slot._title_origin == _TITLE_ORIGIN_AGENT

    def test_the_agent_name_is_final_for_the_background_titler(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")

        _apply(state, slot, "Agent name")

        assert _refresh_blocked(slot) is True

    def test_the_agent_origin_survives_a_reload(self):
        assert _rehydrate_title_origin(True, _TITLE_ORIGIN_AGENT) == _TITLE_ORIGIN_AGENT

    def test_a_manual_title_cannot_be_written_with_a_refreshable_origin(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")

        with pytest.raises(ValueError):
            asyncio.run(
                apply_manual_title(
                    state, slot, "Name", route="test", caller="test", origin=_TITLE_ORIGIN_AUTO
                )
            )
        assert slot.title == "chat-1"
        assert slot._titled is False

    def test_a_title_given_at_creation_with_no_recorded_origin_is_kept(self, tmp_path, monkeypatch):
        # ``session_create`` titles a new session without writing an origin, and a
        # reload reads the missing origin as "user": protected either way.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr(session_control, "session_control_enabled", lambda: True)
        create_rate_limit.reset_for_tests()
        state = _make_state(tmp_path / "sessions")
        caller = state.get_or_create_slot("chat-1")
        try:
            created = asyncio.run(
                session_control.create_session(
                    state, caller_session_key=slot_history_key(caller), title="PR-123 review"
                )
            )
        finally:
            create_rate_limit.reset_for_tests()
        slot = state.get_slot(created["target"])
        assert slot._titled is True
        assert slot._title_origin == "", "precondition: this writer records no origin"

        result = _apply(state, slot, "Agent name")

        assert "(manual_title); the session title was not changed." in result
        assert slot.title == "PR-123 review"

        # A reload: the birth metadata carries no origin, so it rehydrates as "user".
        stored = state.conversation_log.get_metadata(slot_history_key(slot))
        assert "title_origin" not in stored
        slot._title_origin = _rehydrate_title_origin(True, stored.get("title_origin"))
        assert slot._title_origin == _TITLE_ORIGIN_USER

        result = _apply(state, slot, "Agent name")

        assert "(manual_title); the session title was not changed." in result
        assert slot.title == "PR-123 review"

    def test_an_agent_rename_is_persisted_and_survives_a_reload(self, tmp_path, monkeypatch):
        # Through the real save and the real restore: the reloaded slot reads
        # origin "agent" and stays final for the background titler.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = _make_state(tmp_path / "sessions")
        state.conversation_log.append(_history_key_for("chat-1"), "user", "hello")
        slot = state.get_or_create_slot("chat-1")

        assert _apply(state, slot, "Release notes") == (
            'Session renamed. The sidebar now shows "Release notes".'
        )
        stored = state.conversation_log.get_metadata(slot_history_key(slot))
        assert stored["title"] == "Release notes"
        assert stored["title_origin"] == _TITLE_ORIGIN_AGENT

        reloaded = _rehydrate_slot_from_history(_make_state(tmp_path / "sessions"), "chat-1")

        assert reloaded is not None
        assert reloaded.title == "Release notes"
        assert reloaded._titled is True
        assert reloaded._title_origin == _TITLE_ORIGIN_AGENT
        assert _refresh_blocked(reloaded) is True


# ───────────────────────────── conductors ────────────────────────────────────


def test_no_conductor_auto_approves_chat_rename():
    # A conductor reaches chat_rename only through an approval prompt: no shipped
    # conductor grant list names the tool, the whole core server, or a pattern.
    grant_lists = {
        "goal": conductor_agents._conductor_shipped_grants(),
        "pipeline": conductor_agents._conductor_shipped(
            agent_mod._PIPELINE_CONDUCTOR_CORE_GRANTS,
            agent_mod._PIPELINE_CONDUCTOR_DASHBOARD_GRANTS,
        ),
        "security": conductor_agents._conductor_shipped(
            agent_mod._PIPELINE_CONDUCTOR_CORE_GRANTS,
            agent_mod._SECURITY_CONDUCTOR_DASHBOARD_GRANTS,
        ),
    }
    for name, grants in grant_lists.items():
        assert grants, f"precondition: the {name} conductor ships grants"
        for grant in grants:
            assert grant not in ("@kirocrew-core", "@kirocrew-core/chat_rename"), (name, grant)
            assert not conductor_agents._is_wildcard(grant), (name, grant)
