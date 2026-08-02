"""Tests for _ensure_auth_staging_parent resilience to non-directory paths."""

from __future__ import annotations

from pathlib import Path

from kiro_crew.kiro_prerequisite import _ensure_auth_staging_parent


class TestEnsureAuthStagingParent:
    """_ensure_auth_staging_parent must boot even when the staging path is stray."""

    def test_creates_fresh_directory(self, tmp_path: Path) -> None:
        """Normal case: path does not exist yet, mkdir creates it."""
        result = _ensure_auth_staging_parent(tmp_path)
        assert result.is_dir()
        assert not result.is_symlink()

    def test_existing_directory_is_kept(self, tmp_path: Path) -> None:
        """A real existing directory is left intact."""
        staging = tmp_path / ".kiro" / "crew-auth-staging"
        staging.mkdir(parents=True)
        result = _ensure_auth_staging_parent(tmp_path)
        assert result == staging
        assert result.is_dir()

    def test_regular_file_is_quarantined(self, tmp_path: Path) -> None:
        """A stray regular file at the staging path is moved aside."""
        staging = tmp_path / ".kiro" / "crew-auth-staging"
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_text("stray content")
        assert staging.is_file()

        result = _ensure_auth_staging_parent(tmp_path)

        assert result.is_dir()
        assert not result.is_symlink()
        # The stray file was quarantined, not deleted
        quarantined = list(staging.parent.glob("crew-auth-staging.corrupt-*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text() == "stray content"

    def test_dangling_symlink_is_quarantined(self, tmp_path: Path) -> None:
        """A dangling symlink at the staging path is moved aside."""
        staging = tmp_path / ".kiro" / "crew-auth-staging"
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.symlink_to("/nonexistent/target/that/does/not/exist")
        assert staging.is_symlink()
        assert not staging.exists()  # dangling

        result = _ensure_auth_staging_parent(tmp_path)

        assert result.is_dir()
        assert not result.is_symlink()
        # The dangling symlink was quarantined, not deleted
        quarantined = list(staging.parent.glob("crew-auth-staging.corrupt-*"))
        assert len(quarantined) == 1
        assert quarantined[0].is_symlink()

    def test_symlink_to_directory_is_quarantined(self, tmp_path: Path) -> None:
        """A symlink pointing to a real directory is still quarantined.

        The post-mkdir guard already catches this case, but verify the
        pre-mkdir quarantine handles it too (the path must be a real
        directory, not a symlink to one).
        """
        real_dir = tmp_path / "real_target"
        real_dir.mkdir()
        staging = tmp_path / ".kiro" / "crew-auth-staging"
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.symlink_to(real_dir)
        assert staging.is_symlink()
        assert staging.is_dir()  # resolves to a directory

        result = _ensure_auth_staging_parent(tmp_path)

        assert result.is_dir()
        assert not result.is_symlink()
        quarantined = list(staging.parent.glob("crew-auth-staging.corrupt-*"))
        assert len(quarantined) == 1
