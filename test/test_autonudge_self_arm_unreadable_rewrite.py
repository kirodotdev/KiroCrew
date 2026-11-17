"""A damaged self-arm record is set aside before an arm rewrites it.

Each entry vouches for one armed loop. Arming a new loop over a record that was
cut off or had the wrong shape replaced it with a file holding only the new
entry, and the old bytes were gone. The arm itself goes ahead in every case, as
it does without the set-aside.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import autonudge_selfarm as sa


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


def test_arming_over_a_record_that_cannot_be_opened_still_arms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _two_armed()
    real = Path.read_text

    def unreadable(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == sa.SELF_ARM_RECORD_NAME:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as patched:
        patched.setattr(Path, "read_text", unreadable)
        sa.record_self_arm("loop0003", "member-c")
    assert sa.is_recorded_self_arm("loop0003", "member-c") is True


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
