"""``taskrunner_switch.task_runner_off``: the one switch every Task Runner start reads."""

from __future__ import annotations

import json

import pytest
from taskrunner_switch_helpers import turn_task_runner_off

from kiro_crew.apps import manager
from kiro_crew.config.loader import KiroCrewConfig, config_path
from kiro_crew.taskrunner_switch import (
    TASK_RUNNER_FORMER_APP,
    TASK_RUNNER_OFF_MESSAGE,
    task_runner_off,
)


def test_task_runner_is_on_when_nothing_is_set() -> None:
    assert KiroCrewConfig.load().taskrunner.enabled is True
    assert task_runner_off() is None


def test_turning_the_setting_off_refuses_a_start() -> None:
    turn_task_runner_off()
    assert task_runner_off() == TASK_RUNNER_OFF_MESSAGE


@pytest.mark.parametrize("value", [True, 0, "false", None])
def test_only_a_literal_false_turns_it_off(value: object) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"taskrunner": {"enabled": value}}), encoding="utf-8")
    assert KiroCrewConfig.load().taskrunner.enabled is True


def test_the_apps_policy_still_governs_it_under_its_old_app_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []

    def deny(name: str) -> str:
        asked.append(name)
        return "denied by policy"

    monkeypatch.setattr(manager, "_app_activation_denied", deny)
    refusal = task_runner_off()
    assert refusal is not None and "denied by policy" in refusal
    assert asked == [TASK_RUNNER_FORMER_APP] == ["projects"]


def test_a_policy_that_permits_it_leaves_the_setting_in_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(manager, "_app_activation_denied", lambda name: None)
    assert task_runner_off() is None
    turn_task_runner_off()
    assert task_runner_off() == TASK_RUNNER_OFF_MESSAGE
