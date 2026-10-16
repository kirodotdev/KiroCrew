"""The refusals of ``crew_log.store_removal``: what each failure answers, and what it leaves.

The session trash and the retention sweep act on these answers, so each one is pinned with
the state it leaves behind. A hold that cannot be made is reported as not held, a release
that cannot reach its unit is logged rather than raised, a unit whose lease cannot be taken
stays where it is, a move whose sync fails is put back, a restore that cannot move a unit
reports no id, and a companion cleanup that raises is a refusal. Every call goes through
``kiro_crew.crew_log.store``, the path the trash and the sweep import, and every fault is
patched there. The root conftest pins ``KIROCREW_HOME`` to a per-test directory, so each
unit is written into a data home of the test's own.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, store

CREW = "crew-held"
LOGGER = "kiro_crew.crew_log.store"


def _unit() -> Path:
    """A real crew unit on disk, its handle dropped so nothing holds its lease."""
    log = CrewLog.create(lg.KIND_CREW, CREW)
    directory = log.path.parent
    del log
    return directory


def _staging(directory: Path) -> Path:
    return store.crew_log_trash_root() / "batch" / directory.name


def _refuse(*_args: object, **_kwargs: object) -> None:
    raise OSError("refused by the test")


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == LOGGER]


def test_an_absent_unit_is_not_held() -> None:
    assert store.hold_unit(lg.KIND_CREW, CREW) is False


def test_a_root_that_cannot_be_read_holds_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    directory = _unit()
    monkeypatch.setattr(store, "_checked_crew_log_root", _refuse)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        assert store.hold_unit(lg.KIND_CREW, CREW) is False
    assert store.is_trash_held(directory) is False
    assert any("could not hold" in message for message in _messages(caplog))


def test_a_mark_that_cannot_be_created_is_not_a_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _unit()
    monkeypatch.setattr(store, "_write_hold", _refuse)
    assert store.hold_unit(lg.KIND_CREW, CREW) is False
    assert store.is_trash_held(directory) is False


def test_a_mark_whose_sync_fails_still_holds_the_unit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The mark exists, and the mark is what the sweep reads, so the unit is held."""
    directory = _unit()
    monkeypatch.setattr(store, "_sync_hold_dir", _refuse)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert store.hold_unit(lg.KIND_CREW, CREW) is True
    assert store.is_trash_held(directory) is True
    assert any("could not sync the hold" in message for message in _messages(caplog))


def test_releasing_a_unit_with_no_hold_leaves_it_as_it_is() -> None:
    directory = _unit()
    store.release_unit_hold(lg.KIND_CREW, CREW)
    assert directory.is_dir()
    assert store.is_trash_held(directory) is False


def test_a_release_that_cannot_reach_the_unit_is_logged_and_keeps_the_hold(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    directory = _unit()
    assert store.hold_unit(lg.KIND_CREW, CREW) is True
    monkeypatch.setattr(store, "_checked_crew_log_root", _refuse)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        store.release_unit_hold(lg.KIND_CREW, CREW)
    assert store.is_trash_held(directory) is True
    assert any("could not release the hold" in message for message in _messages(caplog))


def test_a_staged_path_outside_the_trash_or_not_a_directory_is_not_held(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    trash = store.crew_log_trash_root()
    trash.mkdir(parents=True)
    (trash / "a-file").write_text("", encoding="utf-8")
    assert store.hold_staged_unit(outside) is False
    assert store.hold_staged_unit(trash / "a-file") is False
    assert store.hold_staged_unit(trash / "missing") is False
    assert store.is_trash_held(outside) is False


def test_staging_an_absent_unit_answers_absent(tmp_path: Path) -> None:
    destination = store.crew_log_trash_root() / "batch" / "absent"
    assert store.stage_unit(lg.KIND_CREW, CREW, destination) == store.REMOVE_ABSENT
    assert not destination.exists()


def test_staging_outside_the_trash_is_refused_and_moves_nothing(tmp_path: Path) -> None:
    directory = _unit()
    with pytest.raises(lg.CrewLogError) as refused:
        store.stage_unit(lg.KIND_CREW, CREW, tmp_path / "outside")
    assert refused.value.code == lg.CODE_BAD_ROOT
    assert directory.is_dir()
    assert not (tmp_path / "outside").exists()


def test_a_unit_whose_lease_cannot_be_taken_is_not_staged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    directory = _unit()
    destination = _staging(directory)
    monkeypatch.setattr(store, "acquire_lease", _refuse)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert store.stage_unit(lg.KIND_CREW, CREW, destination) == store.REMOVE_FAILED
    assert directory.is_dir()
    assert not destination.exists()
    assert any("cannot lease" in message for message in _messages(caplog))


def test_a_unit_whose_lease_cannot_be_taken_is_not_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _unit()
    monkeypatch.setattr(store, "acquire_lease", _refuse)
    assert (
        store.remove_unit(lg.KIND_CREW, CREW, guard=lambda _directory: True) == store.REMOVE_FAILED
    )
    assert directory.is_dir()
    assert store.segment_paths(lg.KIND_CREW, CREW) != []


def test_a_move_whose_sync_fails_after_the_rename_is_put_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _unit()
    destination = _staging(directory)
    real_sync_down = store._sync_down
    syncs: list[Path] = []

    def _fails_once_the_rename_happened(top: Path, leaf: Path) -> None:
        syncs.append(leaf)
        if len(syncs) == 2:  # the first sync after the rename
            raise OSError("sync refused by the test")
        real_sync_down(top, leaf)

    monkeypatch.setattr(store, "_sync_down", _fails_once_the_rename_happened)
    assert store._durable_rename(directory, destination) is False
    assert directory.is_dir()
    assert not destination.exists()
    assert len(syncs) == 4


def test_a_restore_whose_move_fails_reports_no_id(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    directory = _unit()
    destination = _staging(directory)
    assert store.stage_unit(lg.KIND_CREW, CREW, destination) == store.REMOVE_REMOVED
    monkeypatch.setattr(store, "_durable_rename", _refuse)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert store.restore_staged_unit(lg.KIND_CREW, destination) is None
    assert destination.is_dir()
    assert not directory.exists()
    assert any("could not restore" in message for message in _messages(caplog))


def test_a_restore_onto_a_recreated_unit_leaves_both_where_they_are() -> None:
    """A unit recreated since the stage is newer than the staged copy, so it wins."""
    directory = _unit()
    destination = _staging(directory)
    assert store.stage_unit(lg.KIND_CREW, CREW, destination) == store.REMOVE_REMOVED
    assert _unit() == directory
    assert store.restore_staged_unit(lg.KIND_CREW, destination) is None
    assert destination.is_dir()
    assert directory.is_dir()


def test_a_directory_that_cannot_be_listed_counts_one_failure_and_no_history(
    tmp_path: Path,
) -> None:
    assert store._remove_unit_contents(tmp_path / "missing") == (1, 0)


def test_a_companion_cleanup_that_raises_is_a_refusal_logged_as_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _companion() -> bool:
        raise RuntimeError("companion cleanup refused by the test")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert store._run_in_hold(_companion, lg.KIND_CREW, CREW) is False
    records = [record for record in caplog.records if record.name == LOGGER]
    assert records, "the refusal was not logged"
    assert all(record.exc_info is None for record in records)
    assert any("RuntimeError" in record.getMessage() for record in records)
