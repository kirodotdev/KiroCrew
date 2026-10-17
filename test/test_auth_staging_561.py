"""Regression: gateway boot self-heals a stray auth-staging path.

A stray file or dangling symlink at ``<home>/.kiro/crew-auth-staging`` would
make ``mkdir(exist_ok=True)`` raise ``FileExistsError`` and crash boot. It must
now be removed (unlinked, so no sensitive contents survive to a readable
sibling) and a fresh private directory created.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import platform_compat
from kiro_crew.kiro_prerequisite import _AUTH_STAGING_RELATIVE, _ensure_auth_staging_parent


def test_stray_file_is_removed(tmp_path: Path) -> None:
    staging = tmp_path / _AUTH_STAGING_RELATIVE
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text("stray")  # a FILE where a directory must be

    result = _ensure_auth_staging_parent(tmp_path)

    assert result.is_dir() and not result.is_symlink()
    # The stray file's (possibly sensitive) contents are gone — not renamed to a
    # readable sibling.
    assert list(result.iterdir()) == []
    assert not any(".broken-" in p.name for p in staging.parent.iterdir())


@requires_symlinks
def test_dangling_symlink_is_removed(tmp_path: Path) -> None:
    staging = tmp_path / _AUTH_STAGING_RELATIVE
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.symlink_to(tmp_path / "nonexistent-target")  # dangling symlink

    result = _ensure_auth_staging_parent(tmp_path)

    assert result.is_dir() and not result.is_symlink()


def test_existing_directory_is_left_intact(tmp_path: Path) -> None:
    first = _ensure_auth_staging_parent(tmp_path)
    (first / "keep").write_text("x")

    second = _ensure_auth_staging_parent(tmp_path)

    assert (second / "keep").exists()  # not quarantined or recreated


def test_concurrent_boot_race_is_tolerated(tmp_path: Path, monkeypatch) -> None:
    # A second gateway boot may win the race and remove the stray path first, so
    # our unlink() loses with FileNotFoundError. Boot must still self-heal (mkdir
    # the private dir) rather than abort. (#561, concurrent-boot race)
    import os as _os
    from pathlib import Path as _P

    staging = tmp_path / _AUTH_STAGING_RELATIVE
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text("stray")

    real_unlink = _P.unlink

    def racing_unlink(self, *a, **k):
        # The "winner" already removed it; our unlink then loses the race.
        if _os.path.lexists(self):
            real_unlink(self)
        raise FileNotFoundError(2, "No such file or directory", str(self))

    monkeypatch.setattr(_P, "unlink", racing_unlink)

    result = _ensure_auth_staging_parent(tmp_path)
    assert result.is_dir() and not result.is_symlink()


def test_junction_at_staging_path_is_not_certified_as_private(tmp_path: Path, monkeypatch) -> None:
    """A Windows directory junction must not pass the private-directory gate.

    A junction answers ``Path.is_symlink() == False`` and ``Path.is_dir() ==
    True``, so a guard that tests only those two certifies it as the private
    staging root -- and credential material is then staged THROUGH it into the
    junction's target. ``platform_compat.is_link_or_junction`` is the only
    predicate that sees a junction. It is stubbed here (junctions cannot be
    created on this platform) to report True for the staging path the function
    prepares, so a real directory stands in for one: the gate must then refuse
    the path with the existing OSError rather than return it as the private
    root.
    """
    from kiro_crew import kiro_prerequisite as kp

    def reads_as_junction(path):
        return Path(path).name == Path(_AUTH_STAGING_RELATIVE).name

    monkeypatch.setattr(kp.platform_compat, "is_link_or_junction", reads_as_junction)

    with pytest.raises(OSError, match="not a private directory"):
        _ensure_auth_staging_parent(tmp_path)


@pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="junctions exist only on Windows")
def test_real_junction_at_staging_path_is_cleared_and_target_untouched(tmp_path: Path) -> None:
    """A real Windows junction at the staging path is removed, not accepted.

    The symlink-stub test cannot exercise the removal path on POSIX, so this
    runs on the Windows CI lane with a genuine junction created through
    ``_winapi.CreateJunction``. The junction must be cleared and replaced with a
    real private directory, and the directory it pointed at must keep its
    contents.
    """
    import _winapi

    target = tmp_path / "target"
    target.mkdir()
    (target / "sentinel").write_text("keep")

    staging = tmp_path / _AUTH_STAGING_RELATIVE
    staging.parent.mkdir(parents=True, exist_ok=True)
    _winapi.CreateJunction(str(target), str(staging))
    assert platform_compat.is_link_or_junction(staging)  # a junction is planted

    result = _ensure_auth_staging_parent(tmp_path)

    assert result.is_dir()
    assert not platform_compat.is_link_or_junction(result)  # a real dir, not the junction
    assert list(result.iterdir()) == []  # the junction's target did not leak in
    assert (target / "sentinel").read_text() == "keep"  # target untouched
