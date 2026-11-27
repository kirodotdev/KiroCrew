"""A code-channel slash command is dropped only as a repeat of the same ``trigger_id``."""

from __future__ import annotations

import pytest

from kiro_crew.slack.code_channels import commands


@pytest.fixture(autouse=True)
def _fresh_cache(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(commands, "_seen", None)


def _payload(**extra: str) -> dict[str, str]:
    return {"channel_id": "C1", "user_id": "U1", "command": "/summarize", "text": "", **extra}


def test_a_redelivery_with_the_same_trigger_id_is_dropped() -> None:
    assert commands._first_delivery(_payload(trigger_id="t1")) is True
    assert commands._first_delivery(_payload(trigger_id="t1")) is False
    assert commands._first_delivery(_payload(trigger_id="t2")) is True


def test_a_repeat_without_a_trigger_id_still_runs() -> None:
    assert commands._first_delivery(_payload()) is True
    assert commands._first_delivery(_payload()) is True
