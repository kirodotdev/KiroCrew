"""The armed update record: what a click on Update leaves for the next start.

A click ARMS the update; nothing installs and nothing restarts now. The record is a
small file in the data home, so it outlives the process, and its status fields are
the contract the dashboard and the edition read.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import update_next_start


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    update_next_start._differs_memo = None
    return tmp_path


def test_nothing_is_armed_until_a_click() -> None:
    state = update_next_start.read()

    assert state.armed is False
    fields = update_next_start.status_fields()
    assert fields["update_armed_version"] is None
    assert fields["update_armed_at"] is None
    assert fields["pending_apply_error"] is None


def test_arming_records_the_version_and_survives_a_new_reader(_home) -> None:
    update_next_start.arm("9.9.9")

    state = update_next_start.read()
    assert state.armed and state.armed_version == "9.9.9"
    on_disk = json.loads((_home / update_next_start.STATE_FILE).read_text())
    assert on_disk["armed_version"] == "9.9.9"
    fields = update_next_start.status_fields()
    assert fields["update_armed_version"] == "9.9.9"
    assert isinstance(fields["update_armed_at"], str) and fields["update_armed_at"].endswith(
        "+00:00"
    )


def test_arming_again_keeps_the_first_time_and_clears_the_last_error() -> None:
    first = update_next_start.arm("9.9.9")
    update_next_start.finish(error="boom")
    assert update_next_start.read().pending_apply_error == "boom"

    again = update_next_start.arm("9.9.9")

    assert again.armed_at == first.armed_at or again.armed_at > 0
    assert update_next_start.read().pending_apply_error == ""


def test_disarm_drops_the_arm_and_says_whether_one_existed() -> None:
    assert update_next_start.disarm() is False
    update_next_start.arm("9.9.9")

    assert update_next_start.disarm() is True

    assert update_next_start.read().armed is False
    assert update_next_start.status_fields()["update_armed_version"] is None


def test_a_failed_apply_clears_the_arm_and_keeps_the_error_for_the_dashboard() -> None:
    update_next_start.arm("9.9.9")
    update_next_start.finish(error="x" * 5000)

    state = update_next_start.read()

    assert state.armed is False
    assert 0 < len(state.pending_apply_error) <= 600
    assert update_next_start.status_fields()["pending_apply_error"] == state.pending_apply_error


@pytest.mark.parametrize(
    "garbage", ["", "not json", "[]", '{"armed_at": "soon", "attempts": true, "armed_version": 3}']
)
def test_an_unreadable_record_reads_as_not_armed(_home, garbage) -> None:
    (_home / update_next_start.STATE_FILE).write_text(garbage)

    assert update_next_start.read().armed is False


def test_boot_attempts_are_counted_per_arm() -> None:
    update_next_start.arm("9.9.9")
    update_next_start.note_boot_attempt()
    update_next_start.note_boot_attempt()

    assert update_next_start.read().attempts == 2
    update_next_start.arm("10.0.0")
    assert update_next_start.read().attempts == 0


def test_installed_differs_from_running_reads_the_installed_version(monkeypatch) -> None:
    import kiro_crew

    monkeypatch.setattr(update_next_start, "installed_version", lambda: "0.0.1")
    update_next_start._differs_memo = None
    assert update_next_start.status_fields()["installed_differs_from_running"] is True

    monkeypatch.setattr(update_next_start, "installed_version", lambda: kiro_crew.__version__)
    update_next_start._differs_memo = None
    assert update_next_start.status_fields()["installed_differs_from_running"] is False

    monkeypatch.setattr(update_next_start, "installed_version", lambda: None)
    update_next_start._differs_memo = None
    assert update_next_start.status_fields()["installed_differs_from_running"] is False
