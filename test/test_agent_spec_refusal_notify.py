"""Tests for the operator notice while agent sessions are refused.

Drives :class:`kiro_crew.notifications.agent_spec.AgentSpecRefusalNotifier`
against a real :class:`NotificationBus` with a list sink, an injected cause and
an injected clock, and once end to end through the real record in
:mod:`kiro_crew.agent`.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.notifications.agent_spec import (
    CHANNEL,
    GROUP_KEY,
    REALERT_SECS,
    AgentSpecRefusalNotifier,
)
from kiro_crew.notifications.bus import SYSTEM_CHANNELS, NotificationBus

UNREADABLE = "the agent spec kirocrew.json could not be read; check the file's permissions"
INCOMPLETE = "the agent spec kirocrew.json reads, but its rebuild did not complete"


@pytest.fixture
def notes() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def notifier(notes: list[dict[str, Any]]) -> AgentSpecRefusalNotifier:
    return AgentSpecRefusalNotifier(NotificationBus(sink=notes.append), reason_fn=lambda: None)


def test_the_channel_is_a_registered_system_channel() -> None:
    assert CHANNEL in SYSTEM_CHANNELS


def test_a_refusal_pushes_one_critical_note_naming_the_cause(notifier, notes) -> None:
    notifier.observe(UNREADABLE, 0.0)
    notifier.observe(UNREADABLE, 5.0)
    assert len(notes) == 1
    note = notes[0]
    assert note["channel"] == CHANNEL
    assert note["priority"] == "critical"
    assert note["group_key"] == GROUP_KEY
    assert UNREADABLE in note["body"]
    assert "cron jobs" in note["body"]


def test_no_refusal_pushes_nothing(notifier, notes) -> None:
    notifier.observe(None, 0.0)
    notifier.observe(None, REALERT_SECS * 2)
    assert notes == []


def test_a_lasting_refusal_is_pushed_again_on_the_realert_cadence(notifier, notes) -> None:
    notifier.observe(UNREADABLE, 0.0)
    notifier.observe(UNREADABLE, REALERT_SECS - 1)
    assert len(notes) == 1
    notifier.observe(UNREADABLE, REALERT_SECS)
    assert len(notes) == 2
    assert notes[1]["title"] == "Agent sessions are still refused"


def test_a_changed_cause_is_pushed_again(notifier, notes) -> None:
    notifier.observe(UNREADABLE, 0.0)
    notifier.observe(INCOMPLETE, 5.0)
    assert len(notes) == 2
    assert INCOMPLETE in notes[1]["body"]


def test_recovery_pushes_one_note_and_closes_the_episode(notifier, notes) -> None:
    notifier.observe(UNREADABLE, 0.0)
    notifier.observe(None, 5.0)
    notifier.observe(None, 10.0)
    assert [n["title"] for n in notes] == [
        "Agent sessions are refused: kirocrew.json is not re-projected",
        "Agent sessions can start again",
    ]
    assert notes[1]["priority"] != "critical"
    notifier.observe(UNREADABLE, 15.0)
    assert len(notes) == 3


def test_a_failing_read_never_raises_into_the_heartbeat(notes) -> None:
    def _boom() -> str | None:
        raise RuntimeError("broken")

    AgentSpecRefusalNotifier(NotificationBus(sink=notes.append), reason_fn=_boom).sample()
    assert notes == []


def test_the_recorded_refusal_reaches_the_bus(monkeypatch, notes) -> None:
    """The default reader is the agent module's own record."""
    import kiro_crew.agent as agent_mod

    notifier = AgentSpecRefusalNotifier(NotificationBus(sink=notes.append))
    monkeypatch.setattr(agent_mod, "_main_spec_unprojected", None)
    notifier.sample()
    assert notes == []
    monkeypatch.setattr(agent_mod, "_main_spec_unprojected", UNREADABLE)
    notifier.sample()
    assert len(notes) == 1 and UNREADABLE in notes[0]["body"]
    monkeypatch.setattr(agent_mod, "_main_spec_unprojected", None)
    notifier.sample()
    assert notes[-1]["title"] == "Agent sessions can start again"
