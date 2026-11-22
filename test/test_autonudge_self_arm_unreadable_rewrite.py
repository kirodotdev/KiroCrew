"""An arm never replaces the self-arm record with only its own entry.

Each entry vouches for one armed loop. Arming a new loop over a record that was
cut off or had the wrong shape replaced it with a file holding only the new
entry, and the old bytes were gone; a record that merely could not be opened for
a moment (a Windows sharing violation) did the same to every other loop's entry.
A damaged record is set aside first and the arm goes ahead; an unopenable one
is left alone and the arm raises ``OSError``, which the authorizer answers with
its 503 (self-armed slot) or arms unrecorded (ordinary slot). Windows is
simulated: the maintainer checks it on a real host.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from windows_sim import read_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import autonudge_selfarm as sa
from kiro_crew import platform_compat


@pytest.fixture(autouse=True)
def _home(_floor_monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    _floor_monkeypatch.setattr(sa, "data_home", lambda: tmp_path)
    return tmp_path


def _two_armed() -> bytes:
    sa.record_self_arm("loop0001", "member-a")
    sa.record_self_arm("loop0002", "member-b")
    return sa.self_arm_record_path().read_bytes()


def test_arming_over_a_cut_off_record_sets_it_aside_and_starts_again() -> None:
    before = _two_armed()
    path = sa.self_arm_record_path()
    cut = before[: len(before) // 2]
    path.write_bytes(cut)
    sa.record_self_arm("loop0003", "member-c")
    set_aside = list(path.parent.glob(path.name + ".corrupt-*"))
    assert [aside.read_bytes() for aside in set_aside] == [cut]
    assert sa.is_recorded_self_arm("loop0003", "member-c") is True


def test_arming_over_a_record_that_cannot_be_opened_refuses_and_keeps_the_other_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _two_armed()
    real_bytes, real_text = Path.read_bytes, Path.read_text

    def refuse(path: Path) -> None:
        if path.name == sa.SELF_ARM_RECORD_NAME:
            raise PermissionError(13, "Permission denied", str(path))

    def read_bytes(self: Path) -> bytes:
        refuse(self)
        return real_bytes(self)

    def read_text(self: Path, *args: object, **kwargs: object) -> str:
        refuse(self)
        return real_text(self, *args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as patched:
        patched.setattr(Path, "read_bytes", read_bytes)
        patched.setattr(Path, "read_text", read_text)
        with pytest.raises(PermissionError):
            sa.record_self_arm("loop0003", "member-c")
        assert sa.is_recorded_self_arm("loop0003", "member-c") is False
    assert sa.self_arm_record_path().read_bytes() == before
    assert sa.is_recorded_self_arm("loop0001", "member-a") is True
    assert sa.is_recorded_self_arm("loop0002", "member-b") is True


def test_windows_sharing_violation_is_waited_out_and_every_entry_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _two_armed()
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)
    with read_sharing_violation(match=sa.SELF_ARM_RECORD_NAME, times=3) as sim:
        sa.record_self_arm("loop0003", "member-c")
    assert sim["n"] == 4  # 3 refused reads, then the real one
    for loop_id, slot in (
        ("loop0001", "member-a"),
        ("loop0002", "member-b"),
        ("loop0003", "member-c"),
    ):
        assert sa.is_recorded_self_arm(loop_id, slot) is True


def test_windows_hold_that_outlasts_the_retry_refuses_and_keeps_the_old_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _two_armed()
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)
    with read_sharing_violation(match=sa.SELF_ARM_RECORD_NAME, times=10_000) as sim:
        with pytest.raises(PermissionError):
            sa.record_self_arm("loop0003", "member-c")
    assert sim["n"] == aw._REPLACE_MAX_ATTEMPTS
    assert sa.self_arm_record_path().read_bytes() == before


def test_arming_still_arms_when_the_damaged_record_cannot_be_set_aside(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _two_armed()
    path = sa.self_arm_record_path()
    path.write_bytes(before[: len(before) // 2])

    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(sa, "replace_with_retry", refuse, raising=False)
    sa.record_self_arm("loop0003", "member-c")
    assert sa.is_recorded_self_arm("loop0003", "member-c") is True
    assert not list(path.parent.glob(path.name + ".corrupt-*"))


def test_arming_with_no_record_yet_still_creates_it() -> None:
    sa.record_self_arm("loop0001", "member-a")
    assert sa.is_recorded_self_arm("loop0001", "member-a") is True
