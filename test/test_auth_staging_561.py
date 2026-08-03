"""Regression: gateway boot self-heals a stray auth-staging path (#561).

A stray file or dangling symlink at ``<home>/.kiro/crew-auth-staging`` used to
make ``mkdir(exist_ok=True)`` raise ``FileExistsError`` and crash boot. It must
now be removed (unlinked, so no sensitive contents survive to a readable
sibling) and a fresh private directory created.
"""

from __future__ import annotations

from pathlib import Path

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
