"""Tests for cron dashboard chat threading (inject_cron_result_to_dashboard)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.cron_inject import inject_cron_result_to_dashboard
from kiro_crew.session_surface import set_dashboard_surfaced


@pytest.fixture(autouse=True)
def _reset_surface_registry():
    """inject_cron_result_to_dashboard publishes to the process-global
    dashboard-surface registry; reset it so keys from these mock states
    never leak into other tests."""
    set_dashboard_surfaced(())
    yield
    set_dashboard_surfaced(())


def _make_state(history_messages=None):
    """Create a mock DashboardState with conversation_log."""
    state = MagicMock()
    slots = {}
    # Real dict: inject_cron_result_to_dashboard publishes the surface registry
    # via _sync_dashboard_slots, which iterates state._slots.values().
    state._slots = slots

    def get_or_create_slot(name=None, agent=""):
        if name not in slots:
            slot = MagicMock()
            slot.key = name
            slot.linked_session_key = ""
            slot.messages = []
            slot.title = ""

            def append(role, content, cls, broadcast=True):
                slot.messages.append({"role": role, "content": content, "cls": cls})

            slot.append = append
            slots[name] = slot
        return slots[name]

    state.get_or_create_slot = get_or_create_slot
    state.conversation_log = MagicMock()
    state.conversation_log.read_messages.return_value = history_messages or []
    state.push_slots_update = MagicMock()
    return state


def _make_job(job_id="abc123", name="test-cron", last_result="Hello world"):
    job = MagicMock()
    job.id = job_id
    job.name = name
    job.last_result = last_result
    job.agent_id = ""
    return job


class TestInjectCronResultToDashboard:
    def test_sets_linked_session_key(self):
        state = _make_state()
        job = _make_job()
        inject_cron_result_to_dashboard(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert slot.linked_session_key == f"cron:{job.id}"

    def test_sets_title_from_job_name(self):
        state = _make_state()
        job = _make_job(name="daily-standup")
        inject_cron_result_to_dashboard(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert "daily-standup" in slot.title

    def test_hydrates_history_on_first_link(self):
        history = [
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "msg2"},
        ]
        state = _make_state(history_messages=history)
        job = _make_job()
        inject_cron_result_to_dashboard(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        # History (2) + result (1) = 3 messages
        assert len(slot.messages) == 3
        assert slot.messages[0]["content"] == "msg1"
        assert slot.messages[1]["content"] == "msg2"

    def test_hydrates_max_50_messages(self):
        history = [{"role": "assistant", "content": f"msg{i}"} for i in range(100)]
        state = _make_state(history_messages=history)
        job = _make_job()
        inject_cron_result_to_dashboard(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        # 50 from history + 1 result = 51
        assert len(slot.messages) == 51

    def test_does_not_rehydrate_on_second_call(self):
        history = [{"role": "assistant", "content": "old"}]
        state = _make_state(history_messages=history)
        job = _make_job()
        inject_cron_result_to_dashboard(state, job, "result1")
        inject_cron_result_to_dashboard(state, job, "result2")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        # history(1) + result1(1) + result2(1) = 3 (no re-hydration)
        assert len(slot.messages) == 3

    def test_dedup_prevents_duplicate_result(self):
        state = _make_state()
        job = _make_job()
        inject_cron_result_to_dashboard(state, job, "same result")
        inject_cron_result_to_dashboard(state, job, "same result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        # Only 1 message — dedup prevents second identical inject
        assert len(slot.messages) == 1

    def test_empty_result_creates_slot_without_message(self):
        state = _make_state()
        job = _make_job()
        inject_cron_result_to_dashboard(state, job, "")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert slot.linked_session_key == f"cron:{job.id}"
        assert len(slot.messages) == 0

    def test_pushes_slots_update(self):
        state = _make_state()
        job = _make_job()
        inject_cron_result_to_dashboard(state, job, "result")
        state.push_slots_update.assert_called_once()

    def test_publishes_the_tab_to_the_surface_registry(self):
        """Regression: the cron tab must be surfaced the moment it is created.

        Every gate that asks "does this session have a tab?" — sub-agent event
        routing, completion injection, widget/question delivery — reads the
        surface registry via has_dashboard_surface. A created-but-unpublished
        slot fails those gates until some unrelated slot change republishes,
        so the first cron run's sub-agents stayed invisible and their results
        were never injected."""
        from kiro_crew.dashboard.chat_utils import dashboard_slot_key
        from kiro_crew.session_surface import (
            has_dashboard_surface,
            set_dashboard_surfaced,
        )

        set_dashboard_surfaced(())
        try:
            state = _make_state()
            job = _make_job(job_id="188f71e5")
            inject_cron_result_to_dashboard(state, job, "result")
            assert has_dashboard_surface("cron:188f71e5") is True
            assert dashboard_slot_key("cron:188f71e5") == "cron-188f71e5"
        finally:
            set_dashboard_surfaced(())


class TestPersistsResultToConversationLog:
    """the result must be written to the canonical ConversationLog
    under the linked key cron:{id} so a dashboard follow-up turn
    (chat_runner.build_session_replay) has it as context."""

    def test_appends_result_to_conversation_log_under_linked_key(self):
        state = _make_state()
        job = _make_job(job_id="job1", name="my-cron")
        inject_cron_result_to_dashboard(state, job, "the result")
        # Persistence now goes through the atomic append_if_absent (the dup
        # check runs UNDER the session lock, not as a separate unlocked probe).
        assert state.conversation_log.append_if_absent.call_count == 1
        args, kwargs = state.conversation_log.append_if_absent.call_args
        assert args[0] == "cron:job1"
        assert args[1] == "assistant"
        assert "the result" in args[2]
        assert args[2].startswith("# Cron Job Result: my-cron")
        # The old unlocked append() persist path is gone (dup check is now
        # atomic inside append_if_absent). NB: read_messages is still called
        # once to hydrate the fresh slot — that is not the persistence probe.
        state.conversation_log.append.assert_not_called()

    def test_delegates_log_dedup_to_append_if_absent(self):
        # The log-level duplicate check is now performed ATOMICALLY inside
        # append_if_absent (under the per-session lock), not as a separate
        # unlocked read_messages probe at the inject layer. The inject path must
        # delegate to append_if_absent and no longer do its own log-persist.
        # (append_if_absent's own skip-on-duplicate behavior is covered by
        # test_history_locking_remediation::TestAppendIfAbsent.)
        state = _make_state()
        job = _make_job(job_id="job2", name="my-cron")
        inject_cron_result_to_dashboard(state, job, "the result")
        state.conversation_log.append_if_absent.assert_called_once()
        state.conversation_log.append.assert_not_called()

    def test_empty_result_does_not_persist(self):
        state = _make_state()
        job = _make_job(job_id="job3")
        inject_cron_result_to_dashboard(state, job, "")
        state.conversation_log.append_if_absent.assert_not_called()
        state.conversation_log.append.assert_not_called()

    def test_no_conversation_log_does_not_crash(self):
        state = _make_state()
        state.conversation_log = None
        job = _make_job(job_id="job4")
        # Must not raise when conversation_log is unavailable.
        inject_cron_result_to_dashboard(state, job, "result")
        slot = state.get_or_create_slot(name=f"cron-{job.id}")
        assert len(slot.messages) == 1


class TestHydrateSlotFromHistory:
    """Tests for hydrate_slot_from_history (accepts pre-loaded messages)."""

    def test_hydrates_messages_into_slot(self):
        from kiro_crew.dashboard.cron_inject import hydrate_slot_from_history

        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        state = _make_state(history_messages=history)
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, history)
        assert len(slot.messages) == 2
        assert slot.messages[0]["content"] == "hello"
        assert slot.messages[1]["content"] == "world"

    def test_empty_history_produces_no_messages(self):
        from kiro_crew.dashboard.cron_inject import hydrate_slot_from_history

        state = _make_state(history_messages=[])
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, [])
        assert len(slot.messages) == 0

    def test_skips_messages_with_empty_content(self):
        from kiro_crew.dashboard.cron_inject import hydrate_slot_from_history

        history = [
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "real message"},
        ]
        state = _make_state(history_messages=history)
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, history)
        assert len(slot.messages) == 1
        assert slot.messages[0]["content"] == "real message"

    def test_assigns_user_role_class(self):
        from kiro_crew.dashboard.cron_inject import hydrate_slot_from_history

        history = [
            {"role": "user", "content": "user msg"},
            {"role": "assistant", "content": "assistant msg"},
        ]
        state = _make_state(history_messages=history)
        slot = state.get_or_create_slot(name="cron-abc")
        hydrate_slot_from_history(slot, history)
        assert slot.messages[0]["cls"] == "msg msg-u"
        assert slot.messages[1]["cls"] == "msg msg-a"


class TestHasSlot:
    """Tests for DashboardState.has_slot method."""

    def test_returns_true_when_slot_exists(self):
        from kiro_crew.dashboard.state import DashboardState

        state = MagicMock(spec=DashboardState)
        state._slots = {"cron-abc": MagicMock()}
        state.has_slot = DashboardState.has_slot.__get__(state)
        assert state.has_slot("cron-abc") is True

    def test_returns_false_when_slot_missing(self):
        from kiro_crew.dashboard.state import DashboardState

        state = MagicMock(spec=DashboardState)
        state._slots = {}
        state.has_slot = DashboardState.has_slot.__get__(state)
        assert state.has_slot("nonexistent") is False
