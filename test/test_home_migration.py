"""Tests for the one-time ``~/.kirocrew`` -> ``~/.kiro/crew`` data-home migration.

Covers the KITCHEN-111-style contract: copy-then-verify-then-archive, idempotent,
no-data-loss on interruption, gateway-safe, and a no-op under ``KIROCREW_HOME``.
The migration is triggered lazily from ``config_dir()`` (config.paths), so these
tests drive it through that public accessor as well as the module directly.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from kiro_crew import home_migration
from kiro_crew.config import paths


@pytest.fixture(autouse=True)
def _reset_migration_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the once-per-process resolved-home cache so each test migrates fresh."""
    monkeypatch.setattr(paths, "_resolved_home", None)
    monkeypatch.delenv("KIROCREW_HOME", raising=False)


def _seed_legacy(home: Path) -> Path:
    """Create a representative pre-move ~/.kirocrew tree with a secret + nesting."""
    legacy = home / ".kirocrew"
    (legacy / "sessions").mkdir(parents=True)
    (legacy / "profiles").mkdir()
    (legacy / ".env").write_text("SLACK_BOT_TOKEN=xoxb-secret", encoding="utf-8")
    (legacy / "config.json").write_text("{}", encoding="utf-8")
    (legacy / "security_policy.json").write_text('{"deny": []}', encoding="utf-8")
    (legacy / "sessions" / "a.jsonl").write_text("hello", encoding="utf-8")
    (legacy / "profiles" / "default.json").write_text("{}", encoding="utf-8")
    return legacy


class TestConfigDirTriggersMigration:
    def test_first_run_migrates_and_archives(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)

        result = paths.config_dir()

        # New home is the resolved target, data copied verbatim.
        assert result == tmp_path / ".kiro" / "crew"
        assert (result / ".env").read_text(encoding="utf-8") == "SLACK_BOT_TOKEN=xoxb-secret"
        assert (result / "sessions" / "a.jsonl").read_text(encoding="utf-8") == "hello"
        assert (result / "profiles" / "default.json").exists()

        # Legacy is archived (rollback copy) with a breadcrumb; original name gone.
        archived = tmp_path / ".kirocrew.archived"
        assert archived.is_dir()
        assert (archived / ".env").exists()  # rollback copy retains the secret
        assert (archived / home_migration._BREADCRUMB_NAME).exists()
        assert not legacy.exists()

    def test_fresh_install_no_legacy_is_plain_new_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        result = paths.config_dir()
        assert result == tmp_path / ".kiro" / "crew"
        assert result.is_dir()
        assert not (tmp_path / ".kirocrew.archived").exists()
        # Fresh install stamps the completion marker so later starts skip migration.
        assert (result / paths.MIGRATION_MARKER_NAME).exists()

    def test_empty_new_home_does_not_strand_legacy_data(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # HIGH regression (GPT 5.6 review): an EMPTY or partial ~/.kiro/crew —
        # created by another Kiro tool, a user mkdir, or an interrupted copy —
        # must NOT be mistaken for a finished migration. With real data still in
        # ~/.kirocrew, the migration must run and merge it in; nothing is stranded.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)  # empty dir, NO completion marker

        result = paths.config_dir()

        assert result == new_home
        # Legacy data was migrated in, not stranded.
        assert (new_home / ".env").read_text(encoding="utf-8") == "SLACK_BOT_TOKEN=xoxb-secret"
        assert (new_home / "sessions" / "a.jsonl").read_text(encoding="utf-8") == "hello"
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()
        assert (tmp_path / ".kirocrew.archived").is_dir()
        assert not legacy.exists()

    def test_marked_new_home_with_legacy_writeback_reconciles_not_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # HIGH regression (GPT 5.6 round 4): a COMPLETED migration (marker present)
        # then a DOWNGRADE that writes fresh state back to ~/.kirocrew, then an
        # upgrade. The marker must NOT blind-trust the (stale) new home while a
        # legacy dir with data exists — migration re-runs and reconciles. Here the
        # legacy write-back diverges from the marked new home; the legacy home holds
        # the current data, so reconciliation makes LEGACY authoritative at the new
        # home (completing the switch), preserving the stale marked home as a
        # recoverable backup rather than leaving the user stranded on ~/.kirocrew.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / "config.json").write_text('{"old": true}', encoding="utf-8")
        (new_home / paths.MIGRATION_MARKER_NAME).write_text("migrated\n", encoding="utf-8")
        # Post-downgrade write-back: legacy reappears with DIFFERENT current data.
        legacy = _seed_legacy(tmp_path)
        (legacy / "config.json").write_text('{"post_downgrade": true}', encoding="utf-8")
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        # The current (post-downgrade) legacy data wins and is now authoritative at
        # the new home; the switch completes and the stale home is backed up.
        assert result == new_home
        assert not legacy.exists()  # legacy promoted into the new home
        assert (new_home / "config.json").read_text(encoding="utf-8") == '{"post_downgrade": true}'
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()  # marked complete
        backups = list((tmp_path / ".kiro" / "crew.pre-migration").glob("*"))
        assert len(backups) == 1  # stale marked home preserved, recoverable
        assert (backups[0] / "config.json").read_text(encoding="utf-8") == '{"old": true}'

    def test_partial_new_home_all_gaps_migrates_and_completes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A partial ~/.kiro/crew whose pre-existing files are all IDENTICAL to (or
        # disjoint from) legacy has no divergence: migration fills the gaps,
        # archives legacy, and marks complete.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        # Pre-existing file that does NOT exist in legacy (disjoint → no conflict).
        (new_home / "extra.txt").write_text("mine", encoding="utf-8")

        result = paths.config_dir()

        assert result == new_home
        assert (new_home / "extra.txt").read_text(encoding="utf-8") == "mine"  # preserved
        assert (new_home / ".env").exists()  # legacy gap filled
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()
        assert not legacy.exists()

    def test_partial_new_home_with_diverging_file_reconciles_legacy_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A pre-existing ~/.kiro/crew file that DIFFERS from the legacy copy means
        # the no-overwrite merge would keep the (possibly stale) destination. The
        # legacy home holds the current data, so reconciliation makes LEGACY
        # authoritative at the new home (completing the switch to ~/.kiro/crew) and
        # preserves the divergent destination as a recoverable backup — rather than
        # leaving the user stranded on ~/.kirocrew forever.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)  # legacy config.json == "{}"
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / "config.json").write_text('{"stale": true}', encoding="utf-8")  # diverges

        result = paths.config_dir()

        assert result == new_home  # switch completes on the new home
        assert not legacy.exists()  # legacy promoted into the new home
        assert (new_home / "config.json").read_text(
            encoding="utf-8"
        ) == "{}"  # legacy authoritative
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()  # marked complete
        assert not (
            tmp_path / ".kirocrew.archived"
        ).exists()  # no separate archive (backup instead)
        backups = list((tmp_path / ".kiro" / "crew.pre-migration").glob("*"))
        assert len(backups) == 1  # divergent home preserved, recoverable
        assert (backups[0] / "config.json").read_text(encoding="utf-8") == '{"stale": true}'

    def test_divergent_new_home_switch_preserves_models_and_hardens_backup(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Real-world scenario (the reason this fix exists): a populated, divergent
        # ~/.kiro/crew (e.g. left by a sibling Kiro tool or a KIROCREW_HOME
        # experiment) previously left the user stranded on ~/.kirocrew forever.
        # Now the switch completes with legacy authoritative: regenerable bulk dirs
        # ride along (no re-download), and the backup is owner-locked (it may hold
        # credential leaves at a path the security keystone does not gate).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        (legacy / "config.json").write_text('{"current": true}', encoding="utf-8")
        (legacy / "models").mkdir()
        (legacy / "models" / "embed.gguf").write_text("weights", encoding="utf-8")
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / "config.json").write_text('{"stale": true}', encoding="utf-8")  # diverges
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        assert result == new_home
        assert not legacy.exists()
        assert (new_home / "config.json").read_text(encoding="utf-8") == '{"current": true}'
        assert (new_home / "models" / "embed.gguf").read_text(encoding="utf-8") == "weights"
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()
        backups = list((tmp_path / ".kiro" / "crew.pre-migration").glob("*"))
        assert len(backups) == 1
        assert (backups[0] / "config.json").read_text(encoding="utf-8") == '{"stale": true}'
        # Backup is locked down — not readable/writable by group or other.
        assert backups[0].stat().st_mode & 0o077 == 0

    def test_divergent_new_home_gateway_live_retains_legacy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Safety: if a gateway is actively live on the pre-existing new home (e.g.
        # one launched with KIROCREW_HOME=~/.kiro/crew), reconciliation must NOT
        # yank it aside — retain legacy and retry on the next start.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / "config.json").write_text('{"stale": true}', encoding="utf-8")  # diverges
        # A gateway holds the NEW home's lock (but not legacy's).
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: home == new_home)

        result = paths.config_dir()

        assert result == legacy  # not migrated — new home left untouched
        assert legacy.exists()
        assert (new_home / "config.json").read_text(
            encoding="utf-8"
        ) == '{"stale": true}'  # untouched
        assert not (new_home / paths.MIGRATION_MARKER_NAME).exists()
        assert not list(
            (tmp_path / ".kiro" / "crew.pre-migration").glob("*")
        )  # nothing moved aside

    def test_divergent_new_home_retarget_is_best_effort_and_completes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The intra-home symlink retarget runs AFTER the promote (on the now-live
        # home, not a rollback source) and is best-effort — exactly like the copy
        # path's staging retarget. A reported un-rewritable link does NOT abort the
        # switch: the data is fully present, so migration completes rather than
        # stranding the user; only a rare convenience link would dangle. The failure
        # list is NOT silently discarded — it is surfaced prominently on stderr so
        # the user can repair the dangling link.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / "config.json").write_text('{"stale": true}', encoding="utf-8")  # diverges
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)
        # Simulate the retarget reporting a link it could not rewrite.
        monkeypatch.setattr(
            home_migration,
            "_retarget_intra_home_symlinks",
            lambda *a, **k: ["ws/current"],
        )

        result = paths.config_dir()

        assert result == new_home  # switch still completes (best-effort)
        assert not legacy.exists()
        assert (new_home / "config.json").read_text(
            encoding="utf-8"
        ) == "{}"  # legacy authoritative
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()
        assert len(list((tmp_path / ".kiro" / "crew.pre-migration").glob("*"))) == 1
        # The un-rewritable link is surfaced, not silently swallowed.
        err = capsys.readouterr().err
        assert "could not be re-pointed" in err
        assert "ws/current" in err

    def test_divergent_new_home_gateway_appears_after_probe_retains_legacy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # On the no-lock swap path (Windows, or a POSIX lock we could not open) a
        # gateway can start on the new home AFTER the initial _gateway_is_live probe.
        # A pre-promote re-check must catch it and retain legacy rather than vacating
        # a live home. Force the no-lock path and make liveness flip to True on the
        # SECOND probe (the pre-promote re-check).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / "config.json").write_text('{"stale": true}', encoding="utf-8")  # diverges
        monkeypatch.setattr(home_migration.platform_compat, "IS_POSIX", False)  # no lock held
        calls = {"n": 0}

        def _liveness(home: Path) -> bool:
            # False on the first (entry) probe so we proceed; True on the re-check.
            if home == new_home:
                calls["n"] += 1
                return calls["n"] >= 2
            return False

        monkeypatch.setattr(home_migration, "_gateway_is_live", _liveness)

        result = paths.config_dir()

        assert result == legacy  # retained — did not vacate the now-live new home
        assert legacy.exists()
        assert (new_home / "config.json").read_text(encoding="utf-8") == '{"stale": true}'
        # No backup minted, no completion marker written.
        assert not (tmp_path / ".kiro" / "crew.pre-migration").exists()
        assert not (new_home / paths.MIGRATION_MARKER_NAME).exists()

    def test_crash_mid_swap_recovers_via_legacy_no_data_loss(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The restore-through-legacy ordering guarantees a crash mid-swap leaves the
        # data at a CANONICAL path. Simulate the on-disk state right after the
        # "vacate new_home" step crashed: legacy present at ~/.kirocrew, new_home
        # absent, a stray backup left behind. The next start must recover via a
        # NORMAL legacy->new_home migration — never a fresh empty home, never lost
        # data.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        stray_backup = tmp_path / ".kiro" / "crew.pre-migration" / "123"
        stray_backup.mkdir(parents=True)
        (stray_backup / "config.json").write_text('{"stale": true}', encoding="utf-8")
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        new_home = tmp_path / ".kiro" / "crew"
        assert result == new_home  # recovered via normal migration
        assert (new_home / ".env").exists()  # legacy data safely migrated, not lost
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()
        assert not legacy.exists()  # archived normally
        # The stray backup is an untouched, harmless orphan.
        assert (stray_backup / "config.json").read_text(encoding="utf-8") == '{"stale": true}'

    def test_idempotent_second_call_no_reprocess(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        _seed_legacy(tmp_path)
        first = paths.config_dir()
        # Drop a file into the new home; a spurious re-migration would clobber it.
        (first / "post_migration.txt").write_text("keep me", encoding="utf-8")
        # Clear the resolved-home cache to simulate a FRESH process: re-resolution
        # must see the now-existing new home and return it without re-migrating.
        monkeypatch.setattr(paths, "_resolved_home", None)

        second = paths.config_dir()

        assert second == first
        assert (second / "post_migration.txt").read_text(encoding="utf-8") == "keep me"

    def test_ensure_data_home_runs_migration_eagerly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # ensure_data_home() is the synchronous pre-loop hook every entrypoint
        # calls so the blocking migration never lands on the asyncio event loop
        # (GPT 5.6 no-blocking-call-on-event-loop). It must resolve + migrate +
        # cache exactly like the first config_dir() would.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)

        result = paths.ensure_data_home()

        assert result == tmp_path / ".kiro" / "crew"
        assert (result / ".env").exists()  # migrated eagerly
        assert paths._resolved_home == result  # cached, so on-loop calls are cheap
        assert not legacy.exists()

    def test_main_resolves_data_home_before_asyncio_run(self) -> None:
        # Static guard: cli.main() must call ensure_data_home() BEFORE it ever
        # reaches an asyncio.run(...) dispatch, so the blocking migration can't be
        # first-triggered on the event loop. Assert on source order rather than
        # executing the heavy entrypoint.
        import inspect

        from kiro_crew import cli

        src = inspect.getsource(cli.main)
        assert "ensure_data_home()" in src
        first_ensure = src.index("ensure_data_home()")
        first_run = src.index("asyncio.run")
        assert first_ensure < first_run, "ensure_data_home() must precede asyncio.run in main()"

    def test_gatewayd_main_resolves_data_home_before_asyncio_run(self) -> None:
        # GPT 5.6 HIGH regression: the MCP-gateway daemon is a SEPARATE process
        # entrypoint (`python -m kiro_crew.mcp_gateway.gatewayd`), so its migration
        # cache starts empty. Its main() must call ensure_data_home() BEFORE
        # asyncio.run(_amain()), or the first on-loop config_dir() (e.g. via
        # _zombie_diagnostic_path() or the pool cfg_dir lookup) would fire the
        # blocking legacy→~/.kiro/crew migration on the event loop and could trip
        # the stall watchdog (no-blocking-call-on-event-loop).
        import inspect

        from kiro_crew.mcp_gateway import gatewayd

        src = inspect.getsource(gatewayd.main)
        assert "ensure_data_home()" in src
        first_ensure = src.index("ensure_data_home()")
        first_run = src.index("asyncio.run")
        assert first_ensure < first_run, "ensure_data_home() must precede asyncio.run in main()"

    def test_recovery_breadcrumb_written_outside_kiro(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The recovery pointer is written at ~/.kirocrew.breadcrumb — OUTSIDE
        # ~/.kiro/ — so it survives a ~/.kiro-wide uninstaller wipe and records
        # where the data home is. Contains the path, no secrets.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        result = paths.config_dir()

        crumb = tmp_path / paths.RECOVERY_BREADCRUMB_NAME
        assert crumb.is_file()
        # Lives beside ~/.kiro (parent is HOME), NOT under it — survives a
        # ~/.kiro-wide wipe. (String-prefix checks would false-match on the
        # ".kiro" in ".kirocrew.breadcrumb"; assert the real parent instead.)
        assert crumb.parent == tmp_path
        assert (tmp_path / ".kiro") not in crumb.parents
        body = crumb.read_text(encoding="utf-8")
        assert str(result) in body  # points at the data home

    def test_recovery_breadcrumb_not_written_under_kirocrew_home_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A KIROCREW_HOME override is the user's own chosen location (no ~/.kiro
        # wipe risk), so no breadcrumb is written.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "custom"))

        paths.config_dir()

        assert not (tmp_path / paths.RECOVERY_BREADCRUMB_NAME).exists()

    def test_recovery_breadcrumb_idempotent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A second resolution does not rewrite the breadcrumb when the recorded
        # path is unchanged (no per-start churn).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        paths.config_dir()
        crumb = tmp_path / paths.RECOVERY_BREADCRUMB_NAME
        crumb.write_text(crumb.read_text(encoding="utf-8") + "USER-EDIT\n", encoding="utf-8")
        monkeypatch.setattr(paths, "_resolved_home", None)  # fresh process

        paths.config_dir()

        # Path unchanged → not rewritten → the user edit survives.
        assert "USER-EDIT" in crumb.read_text(encoding="utf-8")

    def test_kirocrew_home_override_never_migrates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Even with a legacy dir present, an explicit KIROCREW_HOME wins and no
        # migration occurs (dev/pod/worktree isolation).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        _seed_legacy(tmp_path)
        override = tmp_path / "custom"
        monkeypatch.setenv("KIROCREW_HOME", str(override))

        result = paths.config_dir()

        assert result == override.resolve()
        assert not (tmp_path / ".kiro" / "crew").exists()
        assert (tmp_path / ".kirocrew").exists()  # legacy untouched


class TestMigrateHomeDirect:
    def test_no_data_loss_when_verification_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Force the post-copy verification to report a missing file: the source
        # must stay fully intact and the caller must fall back to the legacy home.
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        monkeypatch.setattr(home_migration, "_verify_copy", lambda a, b: ["sessions/a.jsonl"])

        result = home_migration.migrate_home(
            legacy=legacy, new_home=new_home, marker=new_home / paths.MIGRATION_MARKER_NAME
        )

        assert result == legacy
        assert legacy.is_dir() and (legacy / ".env").exists()
        assert not new_home.exists()  # partial copy cleaned up
        assert not (tmp_path / ".kirocrew.archived").exists()

    def test_skips_when_gateway_live(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: True)

        result = home_migration.migrate_home(
            legacy=legacy, new_home=new_home, marker=new_home / paths.MIGRATION_MARKER_NAME
        )

        assert result == legacy
        assert legacy.is_dir()
        assert not new_home.exists()

    def test_skipped_migration_pins_legacy_home_for_whole_process(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # When migration is skipped (gateway live), config_dir() must return the
        # intact legacy home on EVERY call — not just the first. A bare
        # "attempted" boolean guard would let call #1 return ~/.kirocrew and
        # call #2+ return the empty ~/.kiro/crew, splitting the process across
        # two data roots.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: True)

        first = paths.config_dir()
        second = paths.config_dir()

        assert first == legacy
        assert second == legacy  # NOT tmp_path/.kiro/crew
        assert paths._resolved_home == legacy

    def test_symlinked_destination_dir_does_not_exfiltrate_legacy_data(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # HIGH regression (GPT 5.6 round 4): a crafted ~/.kiro/crew/sessions
        # symlink pointing OUTSIDE the home must not make the merge move legacy
        # session files through it to the external target. The merge refuses to
        # follow the symlink, the divergence guard then flags the un-migrated legacy
        # files, and reconciliation makes LEGACY authoritative at the new home
        # (completing the switch) while the malicious symlink is sidelined into the
        # backup — so nothing is exfiltrated and no data is lost.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)  # has sessions/a.jsonl
        leak = tmp_path / "leak"
        leak.mkdir()
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / "sessions").symlink_to(leak, target_is_directory=True)  # malicious
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        assert result == new_home  # switch completes, legacy promoted
        assert not legacy.exists()
        assert (new_home / "sessions" / "a.jsonl").exists()  # legacy sessions promoted intact
        assert not (leak / "a.jsonl").exists()  # nothing exfiltrated through the link
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()  # marked complete
        # The malicious symlink was moved verbatim into the backup, not followed.
        backups = list((tmp_path / ".kiro" / "crew.pre-migration").glob("*"))
        assert len(backups) == 1
        assert (backups[0] / "sessions").is_symlink()

    def test_symlinked_source_dir_is_not_followed_into_external_target(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # MEDIUM regression (adversarial review, finding A): a legacy SOURCE
        # symlink pointing at a real EXTERNAL dir, when new_home already has a
        # real same-named dir (→ merge path), must NOT be followed. Following it
        # would shutil.move the external target's real files into the home,
        # physically emptying an external directory during a copy-only migration.
        # The link is relocated verbatim only into a gap; the external target's
        # files stay put.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        external = tmp_path / "external-notes"
        external.mkdir()
        (external / "important.txt").write_text("do not move me", encoding="utf-8")
        # Legacy has a symlinked subdir pointing OUTSIDE the home.
        (legacy / "linked").symlink_to(external, target_is_directory=True)
        # new_home already has a REAL same-named dir → the merge path recurses.
        new_home = tmp_path / ".kiro" / "crew"
        (new_home / "linked").mkdir(parents=True)
        (new_home / "linked" / "mine.txt").write_text("kept", encoding="utf-8")
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        paths.config_dir()

        # The external target was NOT emptied — its real file is untouched (the
        # merge never followed the source symlink; the promote renames the link
        # verbatim and only retargets links that point INTO the old home).
        assert (external / "important.txt").read_text(encoding="utf-8") == "do not move me"
        # Reconciliation promoted legacy: new_home/linked is now the legacy symlink
        # to the external dir; the pre-existing real dest dir was preserved in the
        # backup (no data lost, nothing overwritten).
        new_home = tmp_path / ".kiro" / "crew"
        assert (new_home / "linked").is_symlink()
        backups = list((tmp_path / ".kiro" / "crew.pre-migration").glob("*"))
        assert len(backups) == 1
        assert (backups[0] / "linked" / "mine.txt").read_text(encoding="utf-8") == "kept"

    def test_symlinked_source_relocated_verbatim_into_gap(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A legacy SOURCE symlink whose name is a GAP in new_home is relocated as
        # a symlink (never followed): the resulting new_home entry is still a
        # symlink to the external target, and the target's files are not moved.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        external = tmp_path / "external-notes"
        external.mkdir()
        (external / "keep.txt").write_text("stay", encoding="utf-8")
        (legacy / "linked").symlink_to(external, target_is_directory=True)
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)  # empty → "linked" is a gap
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        assert (external / "keep.txt").read_text(encoding="utf-8") == "stay"
        # Relocated as a symlink, not a materialized copy of the target.
        assert (result / "linked").is_symlink()

    def test_staging_dir_name_is_per_pid(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # MEDIUM regression (adversarial review, finding B): the staging dir name
        # must be per-PID so two first-boot processes on the degraded unlocked
        # path never share (and rmtree) each other's staging.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        seen: list[str] = []
        real_copytree = home_migration.shutil.copytree

        def _spy_copytree(src: object, dst: object, **kw: object) -> object:
            seen.append(Path(str(dst)).name)
            return real_copytree(src, dst, **kw)

        monkeypatch.setattr(home_migration.shutil, "copytree", _spy_copytree)
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        home_migration.migrate_home(
            legacy=legacy, new_home=new_home, marker=new_home / paths.MIGRATION_MARKER_NAME
        )

        assert len(seen) == 1
        assert seen[0] == f"crew.migrating.{os.getpid()}"

    def test_remigration_with_existing_archive_no_ungated_leak(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # If ~/.kirocrew.archived already exists (a prior migration's rollback),
        # a fresh ~/.kirocrew must NOT be renamed to ~/.kirocrew.archived.new —
        # that path is not in the security keystone, so its secrets would become
        # agent-readable. The redundant (already-copied) legacy dir is removed
        # instead; the older archive stays as the rollback.
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        (tmp_path / ".kirocrew.archived").mkdir()
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = home_migration.migrate_home(
            legacy=legacy, new_home=new_home, marker=new_home / paths.MIGRATION_MARKER_NAME
        )

        assert result == new_home
        assert not (tmp_path / ".kirocrew.archived.new").exists()
        assert not legacy.exists()  # redundant copy removed
        assert (new_home / ".env").exists()

    def test_remigration_reconciles_legacy_wins_updated_file_preserved(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # HIGH regression (GPT 5.6 round 2): re-migration where new_home has a
        # STALE file, legacy has the UPDATED copy, and a prior archive exists. The
        # no-overwrite merge keeps the stale dest file, so treating the dest as
        # authoritative would lose the only current copy. Reconciliation makes
        # LEGACY authoritative at the new home (the updated file survives and the
        # switch completes), preserving the stale dest as a recoverable backup.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        (legacy / "config.json").write_text('{"updated": true}', encoding="utf-8")
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / "config.json").write_text('{"stale": true}', encoding="utf-8")  # shadows legacy
        (tmp_path / ".kirocrew.archived").mkdir()  # prior archive predates the update
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = home_migration.migrate_home(
            legacy=legacy, new_home=new_home, marker=new_home / paths.MIGRATION_MARKER_NAME
        )

        # The updated config.json survives and is authoritative; switch completes.
        assert result == new_home
        assert not legacy.exists()  # legacy promoted into the new home
        assert (new_home / "config.json").read_text(encoding="utf-8") == '{"updated": true}'
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()  # marked complete
        backups = list((tmp_path / ".kiro" / "crew.pre-migration").glob("*"))
        assert len(backups) == 1  # stale dest preserved, recoverable
        assert (backups[0] / "config.json").read_text(encoding="utf-8") == '{"stale": true}'

    def test_legacy_quiesced_before_compare_closes_write_after_compare_race(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # HIGH regression (GPT 5.6): the migration lock only serializes MIGRATIONS;
        # a legacy-era writer (an old release's CLI) does NOT hold it. If the engine
        # compared the LIVE legacy tree and THEN archived it, a write landing in the
        # gap between compare and archive would be carried into the rollback archive
        # only — absent from the live new home (silent data loss).
        #
        # The fix renames legacy to a private quiesced snapshot BEFORE comparing, so
        # a writer using the canonical legacy path cannot touch the bytes that get
        # compared-then-archived. We prove the window is closed by having the compare
        # itself write to the ORIGINAL legacy path: because legacy was already renamed
        # away, that write lands on a fresh (racer-recreated) dir, and the archived
        # snapshot still holds exactly the pre-compare bytes.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        real_cmp = home_migration._legacy_files_not_identical_in

        def _racing_cmp(walk_root: Path, dest: Path, **kw: object) -> list[str]:
            # Simulate a concurrent legacy-era writer touching the CANONICAL legacy
            # path mid-compare. It must NOT affect the snapshot being compared.
            legacy.mkdir(parents=True, exist_ok=True)
            (legacy / "config.json").write_text('{"raced": true}', encoding="utf-8")
            return real_cmp(walk_root, dest, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(home_migration, "_legacy_files_not_identical_in", _racing_cmp)

        result = home_migration.migrate_home(
            legacy=legacy, new_home=new_home, marker=new_home / paths.MIGRATION_MARKER_NAME
        )

        # Migration completed on the new home; the compare walked the frozen snapshot
        # (identical to staging), so no false divergence.
        assert result == new_home
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()
        # The archived rollback is the PRE-compare snapshot — the racing write did
        # NOT contaminate it (it went to the recreated canonical path instead).
        archived = tmp_path / ".kirocrew.archived"
        assert archived.is_dir()
        assert (archived / "config.json").read_text(encoding="utf-8") == "{}"
        # No leftover transient quiescing dir.
        assert not list(tmp_path.glob(".kirocrew.quiescing.*"))

    def test_quiesce_rename_failure_retains_intact_legacy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # If legacy cannot be renamed out to the quiesced snapshot (a rare
        # same-directory rename failure), the engine cannot guarantee a race-free
        # compare, so it must leave legacy FULLY intact, not archive, not mark
        # complete, and fall back to legacy for this run.
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        real_replace = os.replace

        def _replace(src: object, dst: object) -> None:
            if ".kirocrew.quiescing." in str(dst):
                raise OSError("simulated quiesce rename failure")
            real_replace(src, dst)

        monkeypatch.setattr(home_migration.os, "replace", _replace)

        result = home_migration.migrate_home(
            legacy=legacy, new_home=new_home, marker=new_home / paths.MIGRATION_MARKER_NAME
        )

        assert result == legacy
        assert legacy.is_dir() and (legacy / ".env").exists()
        assert not (new_home / paths.MIGRATION_MARKER_NAME).exists()
        assert not (tmp_path / ".kirocrew.archived").exists()

    def test_vanished_legacy_waits_for_racer_marker(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # HIGH regression (GPT 5.6, degraded unlocked path): if another
        # first-boot process quiesced legacy out from under us, adopt new_home
        # ONLY once the racer's completion marker is observed — the racer's
        # divergence check can still fail and restore legacy as authoritative.
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        marker = new_home / paths.MIGRATION_MARKER_NAME
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)
        real_replace = os.replace

        def racing_replace(src: object, dst: object, **kw: object) -> None:
            if ".quiescing." in str(dst):
                # Simulate the racer: it already renamed legacy away AND
                # finalized (marker written, legacy gone).
                shutil.rmtree(legacy)
                new_home.mkdir(parents=True, exist_ok=True)
                marker.write_text("migrated\n", encoding="utf-8")
                raise FileNotFoundError(str(src))
            real_replace(src, dst, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(home_migration.os, "replace", racing_replace)

        result = home_migration.migrate_home(legacy=legacy, new_home=new_home, marker=marker)

        assert result == new_home  # racer finalized -> adopt

    def test_vanished_legacy_respects_racer_restore(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The racer's divergence check failed and it RESTORED legacy: this
        # process must fall back to the restored legacy (no marker), not adopt
        # the stale new home.
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        marker = new_home / paths.MIGRATION_MARKER_NAME
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)
        real_replace = os.replace
        state = {"raised": False}

        def racing_replace(src: object, dst: object, **kw: object) -> None:
            if ".quiescing." in str(dst) and not state["raised"]:
                state["raised"] = True
                # Racer quiesced (rename fails for us) but restored legacy after
                # its failed divergence check — legacy is still on disk.
                raise FileNotFoundError(str(src))
            real_replace(src, dst, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(home_migration.os, "replace", racing_replace)

        result = home_migration.migrate_home(legacy=legacy, new_home=new_home, marker=marker)

        assert result == legacy  # restored legacy retained
        assert not marker.exists()

    def test_leftover_quiescing_snapshot_blocks_marker_and_is_rehomed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # HIGH regression (GPT 5.6): the prior-archive branch rmtree's the quiesced
        # snapshot with ignore_errors=True. If that silently leaves a residue, the
        # snapshot (~/.kirocrew.quiescing.<pid>) still holds the frozen legacy
        # secrets (.env, keys, policy) at a path the keystone does NOT gate. Writing
        # the completion marker anyway would make every future start skip cleanup,
        # leaving those secrets agent-readable forever. The secret-safety guard must
        # instead re-home the residue to the keystone-gated legacy path and NOT mark
        # complete, so a later cold start reconciles.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        (tmp_path / ".kirocrew.archived").mkdir()  # prior archive → rmtree branch
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)
        # Simulate rmtree silently failing to remove the quiesced snapshot (the
        # exact ignore_errors=True hazard the guard defends against).
        monkeypatch.setattr(home_migration.shutil, "rmtree", lambda *a, **k: None)

        result = home_migration.migrate_home(
            legacy=legacy, new_home=new_home, marker=new_home / paths.MIGRATION_MARKER_NAME
        )

        # Not marked complete — a leftover ungated secret snapshot must not be sealed.
        assert not (new_home / paths.MIGRATION_MARKER_NAME).exists()
        # The residue was re-homed to the keystone-gated legacy path (its secrets are
        # gated there), and no ungated .quiescing.<pid> tree is left behind.
        assert result == legacy
        assert legacy.is_dir() and (legacy / ".env").exists()
        assert not list(tmp_path.glob(".kirocrew.quiescing.*"))

    def test_absolute_intra_home_symlink_retargeted_to_new_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # HIGH regression (GPT 5.6 round 7): an ABSOLUTE symlink pointing inside
        # the old home would dangle after legacy is archived (copytree copies the
        # target verbatim). The migration must rewrite it to the corresponding
        # path under the new home so it still resolves.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        (legacy / "workspace").mkdir()
        (legacy / "workspace" / "project").mkdir()
        (legacy / "workspace" / "project" / "f.txt").write_text("data", encoding="utf-8")
        # Absolute intra-home link → must be retargeted.
        (legacy / "workspace" / "current").symlink_to(legacy / "workspace" / "project")
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        link = result / "workspace" / "current"
        assert link.is_symlink()
        # Retargeted under the new home and RESOLVES (not dangling).
        assert os.readlink(link) == str(result / "workspace" / "project")
        assert (link / "f.txt").read_text(encoding="utf-8") == "data"

    def test_relative_intra_home_symlink_left_untouched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A RELATIVE intra-home link already resolves within the moved tree — it
        # must be preserved verbatim (not rewritten), and still resolve.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        (legacy / "workspace").mkdir()
        (legacy / "workspace" / "project").mkdir()
        (legacy / "workspace" / "current").symlink_to("project")  # relative
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        link = result / "workspace" / "current"
        assert link.is_symlink()
        assert os.readlink(link) == "project"  # unchanged
        assert (link).resolve() == (result / "workspace" / "project").resolve()

    def test_absolute_external_symlink_left_untouched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # An absolute link pointing OUTSIDE the home is still valid after the move
        # and must NOT be rewritten.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        external = tmp_path / "external"
        external.mkdir()
        (legacy / "extlink").symlink_to(external)
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        assert os.readlink(result / "extlink") == str(external)  # unchanged

    def test_legacy_symlink_shadowed_at_dest_reconciles_symlink_preserved(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # HIGH regression (GPT 5.6 round 6): a prior archive exists (re-migration
        # rmtree branch), the legacy home has a SYMLINK whose name ALSO exists in
        # new_home. The no-overwrite merge drops the staged legacy link (dest
        # exists); if the divergence guard skipped symlinks it would declare the
        # homes identical and destroy the link. The symlink-aware guard flags it,
        # and reconciliation makes LEGACY authoritative at the new home — the link
        # survives (promoted) while the shadowing dest dir is preserved in a backup.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        external = tmp_path / "ext"
        external.mkdir()
        (legacy / "mylink").symlink_to(external, target_is_directory=True)
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        # A DIFFERENT thing already occupies the "mylink" name in the new home.
        (new_home / "mylink").mkdir()
        (tmp_path / ".kirocrew.archived").mkdir()  # prior archive → rmtree branch
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = home_migration.migrate_home(
            legacy=legacy, new_home=new_home, marker=new_home / paths.MIGRATION_MARKER_NAME
        )

        # The legacy symlink survives (promoted to the new home); switch completes.
        assert result == new_home
        assert not legacy.exists()
        assert (new_home / "mylink").is_symlink()
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()  # marked complete
        backups = list((tmp_path / ".kiro" / "crew.pre-migration").glob("*"))
        assert len(backups) == 1
        assert (backups[0] / "mylink").is_dir()  # shadowing dir preserved, recoverable

    def test_stale_will_dangle_dest_symlink_reconciles_and_retargets(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Adversarial-review finding: new_home pre-exists with a STALE absolute
        # symlink still pointing INTO legacy (e.g. a user `cp -a ~/.kirocrew
        # ~/.kiro/crew` before upgrading). The no-overwrite merge keeps that stale
        # dest link and discards the retargeted staged link, so the divergence guard
        # flags it. Reconciliation makes LEGACY authoritative at the new home AND
        # retargets its absolute intra-home links, so the promoted link resolves
        # within the new home instead of dangling once the old path is gone.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        (legacy / "ws").mkdir()
        (legacy / "ws" / "x").mkdir()
        (legacy / "ws" / "current").symlink_to(legacy / "ws" / "x")  # absolute intra-legacy
        new_home = tmp_path / ".kiro" / "crew"
        (new_home / "ws").mkdir(parents=True)
        # Stale dest link still points into the OLD home (will dangle post-archive).
        (new_home / "ws" / "current").symlink_to(legacy / "ws" / "x")
        (tmp_path / ".kirocrew.archived").mkdir()  # rmtree re-migration branch
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = home_migration.migrate_home(
            legacy=legacy, new_home=new_home, marker=new_home / paths.MIGRATION_MARKER_NAME
        )

        # Switch completes; the promoted link is retargeted into the new home and
        # resolves to a real dir (not dangling into the gone legacy path).
        assert result == new_home
        assert not legacy.exists()
        assert (new_home / "ws" / "x").is_dir()
        assert (new_home / "ws" / "current").is_symlink()
        assert os.readlink(new_home / "ws" / "current") == str(new_home / "ws" / "x")
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()  # marked complete

    def test_legacy_symlink_identical_at_dest_allows_archive(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The converse: a legacy symlink that is byte-identically reproduced at the
        # destination (same target) is NOT divergence — migration proceeds.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        (legacy / "mylink").symlink_to("relative/target")
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / "mylink").symlink_to("relative/target")  # identical link
        (tmp_path / ".kirocrew.archived").mkdir()
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = home_migration.migrate_home(
            legacy=legacy, new_home=new_home, marker=new_home / paths.MIGRATION_MARKER_NAME
        )

        assert result == new_home
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()
        assert not legacy.exists()  # redundant legacy removed (prior archive existed)

    def test_retarget_symlink_is_atomic_preserves_original_on_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The intra-home retarget must be ATOMIC: if creating the replacement link
        # fails (e.g. Windows without symlink privilege), the ORIGINAL link must be
        # preserved (never unlinked-then-lost) and the failure reported. We force the
        # atomic os.replace to fail and assert the original link survives untouched.
        staging = tmp_path / "staging"
        staging.mkdir()
        legacy = tmp_path / ".kirocrew"
        new_home = tmp_path / ".kiro" / "crew"
        (staging / "current").symlink_to(str(legacy / "x"))  # absolute intra-legacy → retargeted

        real_replace = home_migration.os.replace

        def failing_replace(src: object, dst: object) -> None:
            raise OSError("simulated symlink-replace failure")

        monkeypatch.setattr(home_migration.os, "replace", failing_replace)
        failed = home_migration._retarget_intra_home_symlinks(
            staging, legacy=legacy, new_home=new_home
        )
        monkeypatch.setattr(home_migration.os, "replace", real_replace)

        assert failed == ["current"]  # reported
        assert (staging / "current").is_symlink()  # original preserved (not destroyed)
        assert os.readlink(staging / "current") == str(legacy / "x")  # unchanged target
        # No orphaned temp retarget link left behind.
        assert not list(staging.glob("*.retarget.*"))

    def test_stale_fifo_does_not_abort_migration(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A stale non-regular special file in the legacy tree (e.g. the
        # mcp-gateway socket a crashed gateway left behind, simulated here with a
        # FIFO to avoid the AF_UNIX path-length limit) must be skipped, not crash
        # copytree and abort the migration on every boot.
        if not hasattr(os, "mkfifo"):
            pytest.skip("os.mkfifo not available on this platform")

        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        sock_dir = legacy / "mcp-gateway"
        sock_dir.mkdir()
        os.mkfifo(str(sock_dir / "gateway.sock"))
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = home_migration.migrate_home(
            legacy=legacy, new_home=new_home, marker=new_home / paths.MIGRATION_MARKER_NAME
        )

        assert result == new_home
        assert (new_home / ".env").exists()
        # The special file itself is a runtime artifact — not carried over.
        assert not (new_home / "mcp-gateway" / "gateway.sock").exists()

    def test_regenerable_bulk_dirs_relocated_into_new_home_not_in_archive(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arbiter item 3: the re-downloadable GGUF models and rebuildable caches are
        # never COPIED (that would be slow + a permanent duplicate), but destroying
        # them strands offline/air-gapped users (embeddings are always-on in this
        # fork — the model can't be re-downloaded without a network). So they are
        # RELOCATED (moved, not copied) from the quiesced snapshot into the new home:
        # the model bytes survive the upgrade, there is still no slow copy and no
        # permanent second on-disk copy, and the archive holds no duplicate.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        (legacy / "models").mkdir()
        (legacy / "models" / "qwen3.gguf").write_text("x" * 4096, encoding="utf-8")
        (legacy / "cache").mkdir()
        (legacy / "cache" / "blob.bin").write_text("cached", encoding="utf-8")
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        # Real data migrated; bulk dirs RELOCATED into the new home (bytes survive).
        assert result == tmp_path / ".kiro" / "crew"
        assert (result / ".env").exists()
        assert (result / "models" / "qwen3.gguf").read_text(encoding="utf-8") == "x" * 4096
        assert (result / "cache" / "blob.bin").read_text(encoding="utf-8") == "cached"
        # ...and NOT duplicated into the archive (no second copy of the model bytes).
        archived = tmp_path / ".kirocrew.archived"
        assert archived.is_dir()
        assert (archived / ".env").exists()
        assert not (archived / "models").exists()
        assert not (archived / "cache").exists()
        # Migration completed cleanly.
        assert (result / paths.MIGRATION_MARKER_NAME).exists()
        assert not legacy.exists()

    def test_bulk_dir_already_in_new_home_is_kept_snapshot_copy_stripped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # If the new home ALREADY has a models/ (a fresh re-download, or a partial),
        # it is authoritative — the relocate only fills a gap, so the pre-existing
        # dir is kept and the snapshot's copy is stripped (not carried into the
        # archive as a duplicate).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        (legacy / "models").mkdir()
        (legacy / "models" / "old.gguf").write_text("old", encoding="utf-8")
        new_home = tmp_path / ".kiro" / "crew"
        (new_home / "models").mkdir(parents=True)
        (new_home / "models" / "fresh.gguf").write_text("fresh", encoding="utf-8")
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        # Pre-existing new-home models/ kept; legacy's copy did NOT overwrite it.
        assert (result / "models" / "fresh.gguf").read_text(encoding="utf-8") == "fresh"
        assert not (result / "models" / "old.gguf").exists()
        # The snapshot's redundant copy is not carried into the archive.
        assert not (tmp_path / ".kirocrew.archived" / "models").exists()

    def test_nested_models_dir_is_not_excluded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The bulk-dir exclusion is anchored at the legacy ROOT only. A dir named
        # "models"/"cache" NESTED under real data (e.g. an app's own subdir) is
        # user data and must be migrated normally.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        (legacy / "apps" / "myapp" / "models").mkdir(parents=True)
        (legacy / "apps" / "myapp" / "models" / "keep.txt").write_text("data", encoding="utf-8")
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        assert (result / "apps" / "myapp" / "models" / "keep.txt").read_text(
            encoding="utf-8"
        ) == "data"

    def test_archive_is_locked_to_owner(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arbiter finding: the credential-bearing archive must be owner-only so a
        # frozen .env / key is not left readable to backup/sync tools. On POSIX
        # the archive root is 0o700 and the secret leaf files 0o600.
        if os.name != "posix":
            pytest.skip("POSIX permission bits only")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        _seed_legacy(tmp_path)  # seeds .env (a secret leaf)
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        paths.config_dir()

        archived = tmp_path / ".kirocrew.archived"
        assert archived.is_dir()
        assert (archived.stat().st_mode & 0o777) == 0o700
        assert ((archived / ".env").stat().st_mode & 0o777) == 0o600

    def test_stale_archive_credentials_shredded_governance_retained(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # EOL: once the marker is older than the grace window, the archive's
        # replaceable CREDENTIALS are removed. HIGH regression (GPT 5.6 round 9):
        # the governance/security CEILING files (security_policy.json, profiles,
        # admission_policy.json, denied_commands.json) + audit chain must be
        # RETAINED — expiring them would let a downgrade that restores the archive
        # boot without its ceiling (permission widening). Non-secret rollback data
        # is retained too.
        archived = tmp_path / ".kirocrew.archived"
        (archived / "profiles").mkdir(parents=True)
        (archived / "profiles" / "default.json").write_text("{}", encoding="utf-8")
        (archived / ".env").write_text("SECRET=x", encoding="utf-8")
        (archived / "token_signing.key").write_text("key", encoding="utf-8")
        (archived / "security_policy.json").write_text('{"v":1}', encoding="utf-8")
        (archived / "admission_policy.json").write_text('{"mode":"open"}', encoding="utf-8")
        (archived / "denied_commands.json").write_text("{}", encoding="utf-8")
        (archived / "sel_hmac.key").write_text("hmac", encoding="utf-8")
        (archived / "config.json").write_text("{}", encoding="utf-8")  # NON-secret
        marker = tmp_path / ".kiro" / "crew" / paths.MIGRATION_MARKER_NAME
        marker.parent.mkdir(parents=True)
        marker.write_text("migrated\n", encoding="utf-8")
        # Marker "aged" well past the grace window.
        monkeypatch.setattr(
            home_migration, "_clock_now", lambda: marker.stat().st_mtime + 30 * 24 * 3600
        )

        home_migration.shred_archive_secrets_if_stale(
            archived, marker, min_age_seconds=home_migration._ARCHIVE_SECRET_GRACE_SECONDS
        )

        # Replaceable credentials shredded...
        assert not (archived / ".env").exists()
        assert not (archived / "token_signing.key").exists()
        # ...but the security CEILING + audit chain RETAINED (rollback keeps its ceiling)...
        assert (archived / "security_policy.json").exists()
        assert (archived / "profiles" / "default.json").exists()
        assert (archived / "admission_policy.json").exists()
        assert (archived / "denied_commands.json").exists()
        assert (archived / "sel_hmac.key").exists()
        # ...and non-secret rollback data retained.
        assert (archived / "config.json").exists()

    def test_fresh_archive_secrets_not_shredded_within_grace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Within the grace window (marker just written) the secret leaves stay —
        # rollback is still viable.
        archived = tmp_path / ".kirocrew.archived"
        archived.mkdir()
        (archived / ".env").write_text("SECRET=x", encoding="utf-8")
        marker = tmp_path / ".kiro" / "crew" / paths.MIGRATION_MARKER_NAME
        marker.parent.mkdir(parents=True)
        marker.write_text("migrated\n", encoding="utf-8")
        monkeypatch.setattr(
            home_migration, "_clock_now", lambda: marker.stat().st_mtime + 60
        )  # 1 minute old

        home_migration.shred_archive_secrets_if_stale(
            archived, marker, min_age_seconds=home_migration._ARCHIVE_SECRET_GRACE_SECONDS
        )

        assert (archived / ".env").exists()  # retained — grace not elapsed

    def test_shred_noop_when_no_archive(self, tmp_path: Path) -> None:
        # No archive (fresh install) → the sweep is a harmless no-op.
        marker = tmp_path / ".kiro" / "crew" / paths.MIGRATION_MARKER_NAME
        marker.parent.mkdir(parents=True)
        marker.write_text("fresh-install\n", encoding="utf-8")
        home_migration.shred_archive_secrets_if_stale(
            tmp_path / ".kirocrew.archived", marker, min_age_seconds=0
        )  # must not raise

    def test_archive_failure_is_nonfatal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # If archiving the legacy dir fails, the new home is already good and the
        # migration returns it anyway (only the rollback copy is missing).
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        real_replace = os.replace

        def _replace(src: object, dst: object) -> None:
            # Let the staging->new_home promotion succeed, fail the legacy archive.
            if str(dst).endswith(".kirocrew.archived"):
                raise OSError("simulated archive failure")
            real_replace(src, dst)

        monkeypatch.setattr(home_migration.os, "replace", _replace)

        result = home_migration.migrate_home(
            legacy=legacy, new_home=new_home, marker=new_home / paths.MIGRATION_MARKER_NAME
        )

        assert result == new_home
        assert (new_home / ".env").exists()


class TestConcurrentFirstBoot:
    def test_double_checked_lock_migrates_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Simulate a second process having finished the migration while THIS
        # process was blocked on the cross-process lock. A FINISHED migration
        # leaves the completion MARKER present AND the legacy home archived (i.e.
        # ~/.kirocrew no longer exists) — that combination (marker + no legacy) is
        # what the under-lock re-check trusts, so _do_migrate must NOT run again.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        # Legacy already archived by the "winner" (renamed away): no ~/.kirocrew.
        archived = tmp_path / ".kirocrew.archived"
        archived.mkdir()
        legacy = tmp_path / ".kirocrew"  # does NOT exist (winner archived it)
        new_home = tmp_path / ".kiro" / "crew"
        marker = new_home / paths.MIGRATION_MARKER_NAME
        # Pre-create the finished new home + marker (the "winner" process's result).
        new_home.mkdir(parents=True)
        (new_home / "winner.txt").write_text("done", encoding="utf-8")
        marker.write_text("migrated\n", encoding="utf-8")

        calls: list[str] = []
        real_do = home_migration._do_migrate
        monkeypatch.setattr(
            home_migration,
            "_do_migrate",
            lambda **kw: (calls.append("ran"), real_do(**kw))[1],
        )

        result = home_migration.migrate_home(legacy=legacy, new_home=new_home, marker=marker)

        assert result == new_home
        assert calls == []  # re-check short-circuited before _do_migrate
        assert archived.exists()  # winner's archive intact

    def test_lock_released_after_success(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # After a successful migration the lock file's lock must be released so a
        # later process can take it (no wedged lock). Acquiring it again succeeds.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        _seed_legacy(tmp_path)
        paths.config_dir()  # performs migration, releases the lock

        lock_path = tmp_path / ".kiro" / ".crew-migration.lock"
        assert lock_path.exists()
        from kiro_crew import platform_compat

        fd = os.open(str(lock_path), os.O_RDWR)
        try:
            assert platform_compat.try_acquire_lock(fd, exclusive=True) is True
            platform_compat.release_lock(fd)
        finally:
            os.close(fd)


class TestGatewayLiveProbe:
    def test_no_lockfile_means_not_live(self, tmp_path: Path) -> None:
        assert home_migration._gateway_is_live(tmp_path) is False

    def test_unheld_lockfile_means_not_live(self, tmp_path: Path) -> None:
        from kiro_crew.gateway_lock import LOCK_FILENAME

        (tmp_path / LOCK_FILENAME).write_text("123\n", encoding="utf-8")
        # No process holds the advisory lock, so the probe can take it -> not live.
        assert home_migration._gateway_is_live(tmp_path) is False
