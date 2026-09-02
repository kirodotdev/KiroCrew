"""The Windows symlink stand-in, exercised from a machine that is not Windows.

`O_NOFOLLOW` does not exist on Windows, so `getattr(os, "O_NOFOLLOW", 0)` returns 0
there and the flag contributes nothing — the write path had no symlink protection on
that platform at all. An explicit `is_symlink()` pre-check now stands in for it.

The point of this file is that the stand-in is verifiable HERE. Deleting the
attribute from the `os` module reproduces the platform difference the branch exists
for, so the branch is covered on Linux CI instead of being code nobody can run.

What it does and does not buy is asserted, not just documented: a pre-planted link is
refused, and the check-to-use window is NOT closed. The second assertion exists so a
future reader cannot mistake this for equivalence with `O_NOFOLLOW`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew.eval.bench.safepath import (
    UnsafePathError,
    open_write_nofollow,
    write_text_atomic_nofollow,
)


@pytest.fixture
def no_o_nofollow(monkeypatch):
    """Make `os` look like Windows for the one attribute that matters."""
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    assert not hasattr(os, "O_NOFOLLOW")
    return None


def _link_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - privilege dependent
        pytest.skip("symlink creation requires privileges on this platform")


def test_a_planted_link_is_refused_without_o_nofollow(
    tmp_path: Path, no_o_nofollow
) -> None:
    """The realistic case: something planted the link, then the harness ran.

    Target is an ORDINARY file so the path guard has nothing to object to -- the
    refusal has to come from the symlink check itself.
    """
    bystander = tmp_path / "someone-elses-notes.txt"
    bystander.write_text("KEEP ME\n", encoding="utf-8")
    link = tmp_path / "corpus.json.part"
    _link_or_skip(link, bystander)

    with pytest.raises(UnsafePathError) as excinfo:
        open_write_nofollow(link, what="corpus download staging file")
    message = str(excinfo.value)
    assert "symbolic link" in message
    # The message must not overstate the protection.
    assert "no O_NOFOLLOW" in message
    assert bystander.read_text(encoding="utf-8") == "KEEP ME\n", (
        "the victim was written to -- the stand-in did not actually prevent anything"
    )


def test_an_ordinary_write_still_works_without_o_nofollow(
    tmp_path: Path, no_o_nofollow
) -> None:
    """The stand-in must not make every write on Windows fail."""
    target = tmp_path / "report.json"
    fd = open_write_nofollow(target, what="JSON report")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("{}")
    assert target.read_text(encoding="utf-8") == "{}"


def test_overwriting_an_existing_plain_file_still_works(
    tmp_path: Path, no_o_nofollow
) -> None:
    """Re-running a benchmark with the same --stem must still work.

    The requirement is user-visible and unchanged; what changed is which writer
    carries it. The creating primitive now refuses an existing name, because opening
    one is what a planted link needs. Replacement goes through the atomic writer,
    which publishes by rename -- so this asserts the refusal AND the working path,
    on a platform with no O_NOFOLLOW.
    """
    target = tmp_path / "report.json"
    target.write_text("stale", encoding="utf-8")

    with pytest.raises(UnsafePathError, match="already exists"):
        open_write_nofollow(target, what="JSON report")
    assert target.read_text(encoding="utf-8") == "stale"

    write_text_atomic_nofollow(target, "fresh", what="JSON report")
    assert target.read_text(encoding="utf-8") == "fresh"


def test_the_stand_in_does_not_close_the_check_to_use_window(
    tmp_path: Path, no_o_nofollow, monkeypatch
) -> None:
    """Asserts the LIMIT, so nobody mistakes this for O_NOFOLLOW.

    A link swapped in after the `is_symlink` call is still followed on a platform
    without the flag. Simulated by planting the link during the check itself, which
    is the same ordering a real race would produce.

    If a future change closes this window -- ctypes
    `FILE_FLAG_OPEN_REPARSE_POINT`, or an exclusive-create strategy -- this test
    should start failing, and that failure is the signal to update the docstring in
    `open_write_nofollow` rather than to weaken the test.
    """
    bystander = tmp_path / "victim.txt"
    bystander.write_text("ORIGINAL\n", encoding="utf-8")
    target = tmp_path / "report.json"

    real_is_symlink = Path.is_symlink

    def is_symlink_then_plant(self: Path) -> bool:
        verdict = real_is_symlink(self)
        if self == target and not target.exists():
            target.symlink_to(bystander)
        return verdict

    monkeypatch.setattr(Path, "is_symlink", is_symlink_then_plant)

    try:
        fd = open_write_nofollow(target, what="JSON report")
    except (UnsafePathError, OSError):  # pragma: no cover - platform dependent
        pytest.skip("this platform refused the swapped link; the window is closed")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("REDIRECTED")

    assert bystander.read_text(encoding="utf-8") == "REDIRECTED", (
        "the swap did not land, so this test no longer describes the gap it names"
    )
