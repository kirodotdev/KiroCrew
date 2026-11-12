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
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


def test_nothing_is_armed_until_a_click() -> None:
    state = update_next_start.read()

    assert state.armed is False
    fields = update_next_start.status_fields()
    assert fields["update_armed_version"] is None
    assert fields["pending_apply_error"] is None


def test_arming_records_the_version_and_survives_a_new_reader(_home) -> None:
    update_next_start.arm("9.9.9")

    state = update_next_start.read()
    assert state.armed and state.armed_version == "9.9.9"
    on_disk = json.loads((_home / update_next_start.STATE_FILE).read_text())
    assert on_disk["armed_version"] == "9.9.9"
    fields = update_next_start.status_fields()
    assert fields["update_armed_version"] == "9.9.9"


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


def test_a_git_arm_keeps_its_commit_distance_across_attempts_and_error_clears() -> None:
    update_next_start.arm("9.9.9", behind=4)
    update_next_start.note_boot_attempt()

    assert update_next_start.read().armed_behind == 4
    assert update_next_start.arm("9.9.9").armed_behind == 0


def test_a_failed_apply_remembers_the_version_it_ran_on(monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.__version__", "1.2.3")

    update_next_start.finish(error="boom")

    assert update_next_start.read().error_version == "1.2.3"
    update_next_start.finish()
    assert update_next_start.read().error_version == ""


class TestTheArmRecordIsReadOnlyToAgents:
    """The boot-time apply installs on this record alone, so nothing an agent drives may write it."""

    def test_the_leaf_is_sandbox_readonly_and_in_no_other_disposition(self) -> None:
        from kiro_crew import sandbox

        leaf = update_next_start.STATE_FILE
        assert leaf in sandbox._CREW_READONLY_LEAVES
        assert leaf not in sandbox._CREW_HIDDEN_LEAVES
        assert leaf not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES

    def test_the_leaf_is_precreated_so_the_seal_holds_before_any_click(self) -> None:
        from kiro_crew import sandbox_precreate

        assert (
            update_next_start.STATE_FILE in sandbox_precreate._CREW_PRECREATE_READONLY_FILE_LEAVES
        )

    def test_a_file_write_to_it_is_refused_but_a_read_is_not(self, _home) -> None:
        from kiro_crew import security

        path = str(_home / update_next_start.STATE_FILE)

        assert security.is_sensitive_write_path(path) is True
        assert security.is_sensitive_path(path) is False
