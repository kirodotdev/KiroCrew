"""The dashboard's answer to whether a rotation can re-seed a session (``dashboard.compaction_seed``).

The coordinator asks ``supports`` before it projects a ``soft`` rotation: the
dashboard replay is the only thing that re-seeds a rotated successor, so only a
tab's own session key answers ``True``. Driven with a fake state so the only
thing under test is the lookup.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import compaction_seed as cs


class _Log:
    def read_messages_chained(self, key: str) -> list[dict]:
        raise AssertionError("supports must not read the transcript")


def _slot(key: str = "chat-7", linked_session_key: str = "") -> SimpleNamespace:
    return SimpleNamespace(key=key, linked_session_key=linked_session_key)


def _state(slot: object | None) -> SimpleNamespace:
    return SimpleNamespace(
        get_slot=lambda name: (
            slot if slot is not None and name == getattr(slot, "key", "") else None
        ),
        conversation_log=_Log(),
    )


@pytest.fixture
def routed(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.dashboard_slot_key",
        lambda key: "chat-7" if key == "dashboard:chat-7" else "",
    )


class TestSupports:
    def test_a_channel_session_is_refused_before_any_tab_lookup(self, monkeypatch):
        # A channel-born conversation keeps its own key while a tab mirrors it,
        # so the answer is False for the key even when dashboard_slot_key would
        # answer with the mirroring tab: native runs rather than re-seeding a
        # channel session from a tab its user may never open.
        looked_up: list[str] = []

        def _slot_key(key: str) -> str:
            looked_up.append(key)
            return "chat-7"

        monkeypatch.setattr("kiro_crew.dashboard.chat_utils.dashboard_slot_key", _slot_key)
        state = _state(_slot())
        for key in ("slack:1726000000.000100", "discord:kirocrew:direct:42", "telegram:7"):
            assert cs.supports_seed(state, key) is False
        assert looked_up == []

    def test_no_tab_for_the_session_is_unsupported(self, routed):
        assert cs.supports_seed(_state(_slot()), "cron:job-7") is False

    @pytest.mark.parametrize(
        ("key", "slot"),
        (
            pytest.param(
                "cron:job-7",
                _slot("cron-job-7", "cron:job-7"),
                id="cron-job-tab",
            ),
            pytest.param(
                "cron:job-7:run-3",
                _slot("cron-job-7-run-3", "cron:job-7:run-3"),
                id="cron-run-tab",
            ),
            pytest.param(
                "workflow:run-7",
                _slot("workflow-run-7", "workflow:run-7"),
                id="workflow-result-tab",
            ),
        ),
    )
    def test_a_tab_linked_to_an_external_session_is_unsupported(self, monkeypatch, key, slot):
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_utils.dashboard_slot_key",
            lambda candidate: slot.key if candidate == key else "",
        )

        assert cs.supports_seed(_state(slot), key) is False

    def test_a_tab_that_is_gone_is_unsupported(self, routed):
        assert cs.supports_seed(_state(None), "dashboard:chat-7") is False

    def test_supports_reads_no_transcript(self, routed):
        # The coordinator asks this before projecting, on every threshold
        # crossing: it must stay a lookup, never a file parse (``_Log`` raises).
        assert cs.supports_seed(_state(_slot()), "dashboard:chat-7") is True

    def test_a_task_review_tabs_own_session_is_supported(self, monkeypatch):
        key = "taskrunner:task-7:chat:token-7"
        slot = _slot("task-review-token-7", key)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_utils.dashboard_slot_key",
            lambda candidate: slot.key if candidate == key else "",
        )

        assert cs.supports_seed(_state(slot), key) is True

    def test_the_registered_writer_binds_the_answer_to_one_state(self, routed):
        writer = cs.DashboardSeedWriter(_state(_slot()))
        assert writer.supports("dashboard:chat-7") is True
        assert writer.supports("slack:1726000000.000100") is False
