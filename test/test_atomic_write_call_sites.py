"""Writers that hand-rolled temp-file-and-rename now go through ``atomic_write``.

Each site keeps the mode and durability it had, and inherits what the shared writer
adds: a unique temp name, the Windows rename retry, and a temp reclaimed on ANY exit
-- ``BaseException`` included -- with the previous file untouched.

The routing assertions record the keyword arguments each site hands the module-level
``atomic_write`` it imported, so a reverted site records nothing and a site that drops
or gains a mode, an fsync or the owner-only lockdown fails on the exact keyword. The
interrupt assertions raise from ``os.replace`` for the destination only, the one seam
every form of these writes reaches, so pytest's own renames and other threads are
untouched.
"""

from __future__ import annotations

import errno
import os
import stat
import time
from pathlib import Path
from typing import Callable

import pytest

from kiro_crew import file_delivery_consent
from kiro_crew import session_ledger as sl
from kiro_crew.platform import app_update_request, update_layout, update_stepup

SLOT = "chat-1"


def _recorder(monkeypatch: pytest.MonkeyPatch, module: object) -> list[dict]:
    """Record the keyword arguments of every ``atomic_write`` call *module* makes."""
    calls: list[dict] = []
    original = module.atomic_write  # type: ignore[attr-defined]

    def recording(path, content, **kwargs):
        calls.append(dict(kwargs))
        original(path, content, **kwargs)

    monkeypatch.setattr(module, "atomic_write", recording)
    return calls


def _interrupt_the_rename_of(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """Raise ``KeyboardInterrupt`` when a write renames its temp onto *target*."""
    real_replace = os.replace
    needle = str(target)

    def guarded(src, dst, **kwargs):  # noqa: ANN001 - mirrors os.replace
        if str(dst) == needle:
            raise KeyboardInterrupt("simulated Ctrl-C at the atomic rename")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", guarded)


def _arm_grant(_tmp: Path) -> Path:
    file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
    return file_delivery_consent.pending_grant_path()


def _arm_update(_tmp: Path) -> Path:
    update_stepup.arm("9.9.9", "stable")
    return update_stepup.pending_path()


def _arm_app_update(tmp_path: Path) -> Path:
    path = tmp_path / "updates" / "req.json"
    # A new version each call: re-arming the same live ask writes nothing.
    version = "1.2.3-" + os.urandom(4).hex()
    app_update_request.AppUpdateRequests(now=lambda: 1_000_000.0, path=lambda: path).arm(
        target_version=version, requested_by="agent"
    )
    return path


def _set_channel(_tmp: Path) -> Path:
    update_layout.set_release_channel("nightly")
    return update_layout.data_home() / "channel"


def _rewrite_ledger_lines(tmp_path: Path) -> Path:
    path = tmp_path / "control" / "units.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    sl._rewrite_lines(path, ("a", "b"))
    return path


#: site -> (owning module, writer, the keywords it must hand atomic_write)
_SITES: dict[str, tuple[object, Callable[[Path], Path], dict]] = {
    "file_delivery_consent.arm_grant": (
        file_delivery_consent,
        _arm_grant,
        {"restrict_to_owner": True},
    ),
    "update_stepup.arm": (update_stepup, _arm_update, {"restrict_to_owner": True}),
    "app_update_request._write": (app_update_request, _arm_app_update, {"mode": 0o600}),
    "update_layout.set_release_channel": (update_layout, _set_channel, {}),
    "session_ledger._rewrite_lines": (sl, _rewrite_ledger_lines, {"fsync": True}),
}


@pytest.mark.parametrize("site", sorted(_SITES))
def test_each_site_writes_through_atomic_write_with_its_own_guarantees(
    site: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, write, expected = _SITES[site]
    calls = _recorder(monkeypatch, module)

    path = write(tmp_path)

    assert calls == [expected]
    assert path.read_text(encoding="utf-8")


@pytest.mark.parametrize("site", sorted(_SITES))
def test_an_interrupted_rewrite_leaves_no_temp_and_the_old_file(
    site: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _module, write, _expected = _SITES[site]
    path = write(tmp_path)
    before = path.read_bytes()
    listing = sorted(p.name for p in path.parent.iterdir())
    _interrupt_the_rename_of(monkeypatch, path)

    with pytest.raises(KeyboardInterrupt):
        write(tmp_path)

    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == listing


def _pending(**overrides) -> file_delivery_consent.PendingGrant:
    fields = dict(
        request_id="r-1",
        nonce="n" * 64,
        destination_class=file_delivery_consent.CLASS_OWNER_DASHBOARD,
        created_at=time.time(),
        safety_epoch="epoch-1",
    )
    fields.update(overrides)
    return file_delivery_consent.PendingGrant(**fields)


def test_a_restored_grant_is_written_owner_only_like_the_armed_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _recorder(monkeypatch, file_delivery_consent)

    assert file_delivery_consent.restore_pending_grant(_pending()) is True

    assert calls == [{"restrict_to_owner": True}]
    restored = file_delivery_consent.read_pending_grant()
    assert restored is not None
    assert (restored.request_id, restored.safety_epoch) == ("r-1", "epoch-1")


def test_a_restore_never_overwrites_a_newer_request(monkeypatch: pytest.MonkeyPatch) -> None:
    newer = file_delivery_consent.arm_grant(file_delivery_consent.CLASS_OWNER_DASHBOARD)
    calls = _recorder(monkeypatch, file_delivery_consent)

    assert file_delivery_consent.restore_pending_grant(_pending()) is False

    assert calls == []
    assert file_delivery_consent.read_pending_grant().request_id == newer.request_id


def test_an_interrupted_restore_leaves_nothing_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    path = file_delivery_consent.pending_grant_path()
    _interrupt_the_rename_of(monkeypatch, path)

    with pytest.raises(KeyboardInterrupt):
        file_delivery_consent.restore_pending_grant(_pending())

    assert not os.path.lexists(path)
    assert list(path.parent.iterdir()) == []


# --------------------------------------------------------------------------- #
# The ledger's control files: one directory sync per transaction that needs it
# --------------------------------------------------------------------------- #


def _record_syncs(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    def recording(directory, *, best_effort=False):
        events.append(f"{'sync?' if best_effort else 'sync'}:{Path(directory).name}")

    monkeypatch.setattr(sl, "fsync_dir", recording)


def _fail_syncs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A device that rejects every directory sync, honouring ``best_effort`` as the real one does."""

    def failing(_directory, *, best_effort=False):
        if not best_effort:
            raise OSError(errno.EIO, "device did not take the write")

    monkeypatch.setattr(sl, "fsync_dir", failing)


def _record_rewrites(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    real = sl._rewrite_lines

    def recording(path, lines):
        events.append(f"rewrite:{path.name}")
        real(path, lines)

    monkeypatch.setattr(sl, "_rewrite_lines", recording)


def test_a_delete_syncs_the_control_directory_once_after_both_renames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The caller unlinks the transcript next, so both the exclusion and the tombstone
    # have to survive a crash that the unlink survives.
    events: list[str] = []
    _record_rewrites(monkeypatch, events)
    _record_syncs(monkeypatch, events)

    recorded = sl.exclude_units(SLOT, ("u-old",))

    assert recorded.carry_tombstoned is True
    control = sl.control_dir(SLOT).name
    assert events == [
        f"rewrite:{sl._DELETED_UNITS_FILE}",
        f"rewrite:{sl._CARRIED_FILE}",
        f"sync:{control}",
    ]


def test_a_delete_whose_sync_fails_is_refused_and_taken_back_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_syncs(monkeypatch)

    with pytest.raises(sl.LedgerExclusionError):
        sl.exclude_units(SLOT, ("u-old",))

    assert sl._excluded_units(SLOT) == frozenset()
    # The tombstone this call committed is withdrawn too: the delete did not happen,
    # so the session it would have silenced still owns its earlier state.
    assert not sl._carry_committed(sl._control_file(SLOT, sl._CARRIED_FILE))


def test_a_refused_delete_syncs_its_take_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # A crash that kept the exclusion's rename and lost the take-back's would leave the
    # spared session's record excluded, so the take-back gets its own sync.
    events: list[str] = []
    _record_rewrites(monkeypatch, events)

    def first_sync_fails(directory, *, best_effort=False):
        first = not any(event.startswith("sync:") for event in events)
        events.append(f"sync:{Path(directory).name}")
        if first:
            raise OSError(errno.EIO, "device did not take the write")

    monkeypatch.setattr(sl, "fsync_dir", first_sync_fails)

    with pytest.raises(sl.LedgerExclusionError):
        sl.exclude_units(SLOT, ("u-old",))

    control = sl.control_dir(SLOT).name
    assert events == [
        f"rewrite:{sl._DELETED_UNITS_FILE}",
        f"rewrite:{sl._CARRIED_FILE}",
        f"sync:{control}",
        f"rewrite:{sl._DELETED_UNITS_FILE}",
        f"sync:{control}",
    ]


def test_a_take_back_whose_sync_fails_is_reported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _fail_syncs(monkeypatch)

    with caplog.at_level("ERROR", logger=sl.logger.name), pytest.raises(sl.LedgerExclusionError):
        sl.exclude_units(SLOT, ("u-old",))

    assert any("take-back reached disk" in r.getMessage() for r in caplog.records)


def test_a_commit_that_lands_then_raises_is_still_withdrawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The marker is renamed into place before its read-back, so a read-back that
    # raises leaves a committed tombstone the call never reported.
    real = sl._settle_carry_locked

    def landed_then_raised(path, *, landed):
        real(path, landed=landed)
        raise OSError(errno.EIO, "read-back failed")

    monkeypatch.setattr(sl, "_settle_carry_locked", landed_then_raised)

    with pytest.raises(sl.LedgerExclusionError):
        sl.exclude_units(SLOT, ("u-old",))

    assert sl._excluded_units(SLOT) == frozenset()
    assert not sl._carry_committed(sl._control_file(SLOT, sl._CARRIED_FILE))


def test_a_refused_delete_keeps_a_marker_it_did_not_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An earlier delete's tombstone is that delete's proof, never this call's to lift.
    sl.exclude_units(SLOT, ("u-first",))
    _fail_syncs(monkeypatch)

    with pytest.raises(sl.LedgerExclusionError):
        sl.exclude_units(SLOT, ("u-second",))

    assert sl._excluded_units(SLOT) == frozenset({"u-first"})
    assert sl._carry_committed(sl._control_file(SLOT, sl._CARRIED_FILE))


def test_a_rollback_with_nothing_to_drop_rewrites_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sl.exclude_units(SLOT, ("u-kept",))
    events: list[str] = []
    _record_rewrites(monkeypatch, events)
    _record_syncs(monkeypatch, events)

    sl.unexclude_units(SLOT, ("u-never-added",))

    assert events == []
    assert sl._excluded_units(SLOT) == frozenset({"u-kept"})


def test_an_exclusion_past_the_unlink_stands_when_its_sync_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The transcript is already gone, so a take-back would leave the deleted
    # conversation's unit neither excluded nor removed.
    _fail_syncs(monkeypatch)

    recorded = sl.exclude_units(SLOT, ("u-own",), refusable=False)

    assert recorded.added == ("u-own",)
    assert sl._excluded_units(SLOT) == frozenset({"u-own"})


def test_a_rollback_whose_sync_fails_still_reports_it_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Raising here would answer "the record reads empty" for one that folds normally.
    recorded = sl.exclude_units(SLOT, ("u-spared",))
    assert recorded.carry_tombstoned is True
    _fail_syncs(monkeypatch)

    sl.unexclude_units(SLOT, recorded.added, restore_carry=True)

    assert sl._excluded_units(SLOT) == frozenset()
    assert not sl._carry_committed(sl._control_file(SLOT, sl._CARRIED_FILE))


def test_a_rollback_that_only_withdraws_the_tombstone_syncs_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = sl.exclude_units(SLOT, ())
    assert recorded.carry_tombstoned is True
    events: list[str] = []
    _record_rewrites(monkeypatch, events)
    _record_syncs(monkeypatch, events)

    sl.unexclude_units(SLOT, (), restore_carry=True)

    assert events == [f"sync?:{sl.control_dir(SLOT).name}"]
    assert not sl._carry_committed(sl._control_file(SLOT, sl._CARRIED_FILE))


def test_a_carry_commit_syncs_the_directory_once(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = sl._control_file(SLOT, sl._CARRIED_FILE, create=True)
    marker.write_text("pending\n", encoding="utf-8")
    events: list[str] = []
    _record_syncs(monkeypatch, events)

    assert sl._finish_carry(SLOT, landed=True) is True
    assert sl._finish_carry(SLOT, landed=True) is False

    assert events == [f"sync?:{sl.control_dir(SLOT).name}"]


def test_a_carry_commit_whose_sync_fails_still_reports_it_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = sl._control_file(SLOT, sl._CARRIED_FILE, create=True)
    marker.write_text("pending\n", encoding="utf-8")
    _fail_syncs(monkeypatch)

    assert sl._finish_carry(SLOT, landed=True) is True
    assert sl._carry_committed(marker)


def test_a_unit_order_move_does_not_sync_the_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    # A lost move leaves the previous order, which the unit's next record corrects.
    sl._note_unit_order(SLOT, "u-a")
    sl._note_unit_order(SLOT, "u-b")
    events: list[str] = []
    _record_rewrites(monkeypatch, events)
    _record_syncs(monkeypatch, events)

    sl._note_unit_order(SLOT, "u-a")

    assert events == [f"rewrite:{sl._UNIT_ORDER_FILE}"]
    assert sl._recorded_unit_order(SLOT) == ("u-b", "u-a")


def test_a_temp_left_by_a_killed_writer_is_swept_by_the_next_rewrite() -> None:
    # atomic_write names each temp uniquely, so a SIGKILL mid-rewrite leaves one
    # that no later write would ever overwrite.
    directory = sl._control_file(SLOT, sl._DELETED_UNITS_FILE, create=True).parent
    (directory / "tmpabandoned.tmp").write_text("u-half\n", encoding="utf-8")

    sl.exclude_units(SLOT, ("u-old",))

    assert not list(directory.glob("*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_the_control_directory_is_owner_only() -> None:
    directory = sl.control_dir(SLOT)
    directory.mkdir(parents=True, mode=0o755)
    directory.chmod(0o755)

    sl.exclude_units(SLOT, ("u-old",))

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
