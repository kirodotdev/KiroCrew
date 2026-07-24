"""Tests for the `kirocrew doctor` OS-aware fix hints.

Guards _os_fix_hint: it returns the macOS Homebrew command on Darwin and the
Linux/AL2023 guidance otherwise, so `kirocrew doctor` never prints a brew
command on Linux where there is no brew.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew import cli_doctor


class TestFixHint:
    """OS-aware `kirocrew doctor` fix hints."""

    def test_os_fix_hint_macos_returns_brew(self, monkeypatch) -> None:
        monkeypatch.setattr(cli_doctor._plat, "system", lambda: "Darwin")
        assert cli_doctor._os_fix_hint("brew install ffmpeg", "static build") == "brew install ffmpeg"

    def test_os_fix_hint_linux_returns_linux_guidance(self, monkeypatch) -> None:
        monkeypatch.setattr(cli_doctor._plat, "system", lambda: "Linux")
        assert cli_doctor._os_fix_hint("brew install ffmpeg", "static build") == "static build"


class TestHumanSize:
    """Compact human-readable byte rendering."""

    def test_bytes(self) -> None:
        assert cli_doctor._human_size(512) == "512 B"

    def test_megabytes(self) -> None:
        assert cli_doctor._human_size(3 * 1024 * 1024) == "3.0 MB"

    def test_gigabytes(self) -> None:
        assert cli_doctor._human_size(2 * 1024 * 1024 * 1024) == "2.0 GB"


class TestDataHomeArchiveAffordance:
    """`kirocrew doctor` Data Home section surfaces the leftover rollback archive."""

    def test_archive_present_prints_size_and_cleanup_command(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # Migration completed: new home exists, plus a leftover ~/.kirocrew.archived
        # rollback copy. Doctor must surface it (with size + rm command) so the
        # permanent secret-bearing archive has a user-driven end-of-life.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: tmp_path / ".kiro" / "crew")
        archived = tmp_path / cli_doctor.ARCHIVED_LEGACY_DIR_NAME
        archived.mkdir()
        (archived / ".env").write_text("SECRET=x", encoding="utf-8")

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "Data Home" in out
        assert str(archived) in out
        assert f"rm -rf {archived}" in out

    def test_diverged_legacy_surfaces_reconcile_not_retry(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # Arbiter item 3: when a completed migration (marker present) has a legacy
        # home that DIVERGES (downgrade write-back), the migration aborts every
        # start — doctor must say "ABORTED (not retrying)" + a reconcile command,
        # NOT the misleading "will retry on next cold start".
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        home = tmp_path / ".kiro" / "crew"
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: home)
        home.mkdir(parents=True)
        (home / "config.json").write_text('{"stale": true}', encoding="utf-8")
        (home / cli_doctor.MIGRATION_MARKER_NAME).write_text("migrated\n", encoding="utf-8")
        legacy = tmp_path / cli_doctor.LEGACY_CONFIG_DIR_NAME
        legacy.mkdir()
        (legacy / "config.json").write_text('{"current": true}', encoding="utf-8")  # diverges

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "ABORTED (not retrying)" in out
        assert "diff -r" in out
        assert "will retry on next cold start" not in out

    def test_transient_legacy_says_will_retry(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # No marker yet (first cold start / gateway-held): legacy present is
        # transient — the "will retry" message is correct here.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        home = tmp_path / ".kiro" / "crew"
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: home)
        home.mkdir(parents=True)  # no marker
        legacy = tmp_path / cli_doctor.LEGACY_CONFIG_DIR_NAME
        legacy.mkdir()
        (legacy / "config.json").write_text("{}", encoding="utf-8")

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "will retry on next cold start" in out
        assert "ABORTED" not in out

    def test_no_archive_stays_quiet(self, monkeypatch, tmp_path: Path, capsys) -> None:
        # Fresh install / archive already cleaned up: only the location line, no
        # spurious cleanup nag.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: tmp_path / ".kiro" / "crew")

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "Data Home" in out
        assert "rollback copy" not in out
        assert "rm -rf" not in out
