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
        assert (
            cli_doctor._os_fix_hint("brew install ffmpeg", "static build") == "brew install ffmpeg"
        )

    def test_os_fix_hint_linux_returns_linux_guidance(self, monkeypatch) -> None:
        monkeypatch.setattr(cli_doctor._plat, "system", lambda: "Linux")
        assert cli_doctor._os_fix_hint("brew install ffmpeg", "static build") == "static build"


class TestDataHome:
    """`kirocrew doctor` Data Home section — location + leftover legacy home."""

    def test_legacy_present_says_will_retry(self, monkeypatch, tmp_path: Path, capsys) -> None:
        # A leftover ~/.kirocrew (a live gateway held it, the delete failed, or
        # this is the first cold start) is always transient now — migration
        # force-overwrites and deletes it on the next start, so "will retry" is
        # correct in every case (there is no more divergence-abort state).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)  # default-path case
        home = tmp_path / ".kiro" / "crew"
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: home)
        home.mkdir(parents=True)
        legacy = tmp_path / cli_doctor.LEGACY_CONFIG_DIR_NAME
        legacy.mkdir()
        (legacy / "config.json").write_text("{}", encoding="utf-8")

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "will retry on next cold start" in out

    def test_legacy_present_under_valid_override_says_ignored(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # Under a VALID KIROCREW_HOME override migration is bypassed on every
        # start, so a leftover legacy is NOT going to be migrated — the doctor
        # must not claim "will retry" (GPT 5.6 MEDIUM).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "override"))
        home = tmp_path / ".kiro" / "crew"
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: home)
        home.mkdir(parents=True)
        legacy = tmp_path / cli_doctor.LEGACY_CONFIG_DIR_NAME
        legacy.mkdir()

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "IGNORED" in out and "override active" in out
        assert "will retry on next cold start" not in out

    def test_legacy_override_points_at_legacy_says_active_not_ignored(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # KIROCREW_HOME=~/.kirocrew makes the legacy dir the ACTIVE home, not
        # ignored debris — the doctor must not mislabel the home the process is
        # actually using (GPT 5.6 MEDIUM).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = tmp_path / cli_doctor.LEGACY_CONFIG_DIR_NAME
        legacy.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(legacy))
        # config_dir() resolves to the override (== legacy) when set
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: legacy.resolve())

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "ACTIVE data home" in out
        assert "IGNORED" not in out
        assert "will retry on next cold start" not in out

    def test_marker_present_nonempty_legacy_renders_conflict(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # Marker present + a NON-EMPTY legacy dir → a genuine conflict: the
        # legacy is resurrection debris, NOT a pending migration. The doctor must
        # render the conflict (⚠ / NOT used) and never claim a retry (GPT 5.6
        # MEDIUM: pin the conflict-rendering branch so removing it fails a test).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        home = tmp_path / ".kiro" / "crew"
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: home)
        home.mkdir(parents=True)
        (home / cli_doctor.MIGRATION_MARKER_NAME).write_text("done\n", encoding="utf-8")
        legacy = tmp_path / cli_doctor.LEGACY_CONFIG_DIR_NAME
        legacy.mkdir()
        (legacy / "sessions.db").write_text("stale", encoding="utf-8")  # non-empty debris

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "conflict" in out and "NOT used" in out
        assert "will retry on next cold start" not in out

    def test_marker_present_empty_legacy_says_unused_not_retry(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # Marker present + an EMPTY recreated legacy dir: migration already
        # completed and is marker-authoritative, so it will NEVER retry. The
        # doctor must call the dir UNUSED leftover, not claim a pending retry
        # (GPT 5.6 MEDIUM — the misleading "will retry" would persist forever).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        home = tmp_path / ".kiro" / "crew"
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: home)
        home.mkdir(parents=True)
        (home / cli_doctor.MIGRATION_MARKER_NAME).write_text("done\n", encoding="utf-8")
        legacy = tmp_path / cli_doctor.LEGACY_CONFIG_DIR_NAME
        legacy.mkdir()  # empty debris

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "UNUSED" in out and "migration already completed" in out
        assert "will retry on next cold start" not in out

    def test_no_legacy_stays_quiet(self, monkeypatch, tmp_path: Path, capsys) -> None:
        # Fresh install / migration already completed: only the location line,
        # no leftover-legacy nag. There is no archive to report either way.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(cli_doctor, "config_dir", lambda: tmp_path / ".kiro" / "crew")

        cli_doctor._doctor_data_home()

        out = capsys.readouterr().out
        assert "Data Home" in out
        assert "legacy:" not in out
        assert "rollback copy" not in out
        assert "rm -rf" not in out
