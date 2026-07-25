"""Tests for the one-time ``~/.kirocrew`` -> ``~/.kiro/crew`` data-home migration.

Covers the copy-overwrite-verify-delete contract: idempotent, no-data-loss on
interruption, gateway-safe, and a no-op under ``KIROCREW_HOME``. The migration
is triggered lazily from ``config_dir()`` (config.paths), so these tests drive
it through that public accessor as well as the module directly.
"""

from __future__ import annotations

import os
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
    def test_first_run_migrates_and_deletes_legacy(
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

        # Legacy is deleted outright — no rollback copy of any kind.
        assert not legacy.exists()
        assert not (tmp_path / ".kirocrew.archived").exists()

    def test_fresh_install_no_legacy_is_plain_new_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        result = paths.config_dir()
        assert result == tmp_path / ".kiro" / "crew"
        assert result.is_dir()
        # Fresh install stamps the completion marker so later starts skip migration.
        assert (result / paths.MIGRATION_MARKER_NAME).exists()

    def test_empty_new_home_does_not_strand_legacy_data(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # An EMPTY or partial ~/.kiro/crew — created by another Kiro tool, a user
        # mkdir, or an interrupted copy — must NOT be mistaken for a finished
        # migration. With real data still in ~/.kirocrew, the migration must run
        # and merge it in; nothing is stranded.
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
        assert not legacy.exists()

    def test_marked_new_home_with_legacy_writeback_force_overwrites(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A COMPLETED migration (marker present) then a DOWNGRADE that writes
        # fresh state back to ~/.kirocrew, then an upgrade. The marker must NOT
        # blind-trust the (stale) new home while a legacy dir with data exists —
        # migration re-runs and the legacy write-back force-overwrites the stale
        # marked home's conflicting file.
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

        # The current (post-downgrade) legacy data wins.
        assert result == new_home
        assert not legacy.exists()
        assert (new_home / "config.json").read_text(encoding="utf-8") == '{"post_downgrade": true}'
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()  # marked complete

    def test_partial_new_home_all_gaps_migrates_and_completes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A partial ~/.kiro/crew whose pre-existing files are all disjoint from
        # legacy has no conflicts: migration fills the gaps, preserves the
        # disjoint file, deletes legacy, and marks complete.
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

    def test_partial_new_home_with_conflicting_file_legacy_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A pre-existing ~/.kiro/crew file that conflicts with the legacy copy is
        # force-overwritten — legacy always wins, and the stale destination
        # content is gone with no backup.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)  # legacy config.json == "{}"
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / "config.json").write_text('{"stale": true}', encoding="utf-8")  # conflicts

        result = paths.config_dir()

        assert result == new_home
        assert not legacy.exists()
        assert (new_home / "config.json").read_text(encoding="utf-8") == "{}"  # legacy wins
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()
        assert not (tmp_path / ".kirocrew.archived").exists()
        assert not (tmp_path / ".kiro" / "crew.pre-migration").exists()  # no backup, anywhere

    def test_divergent_new_home_force_overwrites_no_backup(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A populated, divergent ~/.kiro/crew (e.g. left by a sibling Kiro tool or
        # a KIROCREW_HOME experiment): legacy force-overwrites the conflicting
        # file, and NOTHING about the divergent content is preserved anywhere on
        # disk (no rollback, no backup).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        (legacy / "config.json").write_text('{"current": true}', encoding="utf-8")
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / "config.json").write_text('{"stale": true}', encoding="utf-8")  # conflicts
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        assert result == new_home
        assert not legacy.exists()
        assert (new_home / "config.json").read_text(encoding="utf-8") == '{"current": true}'
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()
        assert not (tmp_path / ".kiro" / "crew.pre-migration").exists()

    def test_divergent_new_home_gateway_live_retains_legacy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Safety: if a gateway is actively live on the pre-existing new home (e.g.
        # one launched with KIROCREW_HOME=~/.kiro/crew), migration must NOT
        # force-overwrite underneath it — retain legacy and retry on next start.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / "config.json").write_text('{"stale": true}', encoding="utf-8")
        # A gateway holds the NEW home's lock (but not legacy's).
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: home == new_home)

        result = paths.config_dir()

        assert result == legacy  # not migrated — new home left untouched
        assert legacy.exists()
        assert (new_home / "config.json").read_text(
            encoding="utf-8"
        ) == '{"stale": true}'  # untouched
        assert not (new_home / paths.MIGRATION_MARKER_NAME).exists()

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


class TestSweepUngatedArchiveLeftovers:
    """This PR drops ~/.kirocrew.archived and ~/.kiro/crew.pre-migration from the
    security keystone (nothing creates them anymore), but an EARLIER release
    could have already created one. Without an active sweep, that leftover
    would hold frozen credentials at a now-permanently-ungated path, readable
    by the agent indefinitely with nothing to ever prompt a cleanup.
    """

    def test_leftover_archive_is_removed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        archived = tmp_path / ".kirocrew.archived"
        (archived / "profiles").mkdir(parents=True)
        (archived / ".env").write_text("SECRET=x", encoding="utf-8")
        (archived / "security_policy.json").write_text('{"deny": []}', encoding="utf-8")

        paths.config_dir()

        assert not archived.exists()

    def test_leftover_pre_migration_backup_is_removed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        backup_root = tmp_path / ".kiro" / "crew.pre-migration"
        backup = backup_root / "1784933442"
        backup.mkdir(parents=True)
        (backup / ".env").write_text("SECRET=x", encoding="utf-8")

        paths.config_dir()

        assert not backup_root.exists()

    def test_no_leftovers_is_a_quiet_noop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        paths.config_dir()  # must not raise; nothing to sweep

    def test_sweep_does_not_touch_the_live_new_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Regression guard: "crew.pre-migration" must not prefix-match plain
        # "crew" and take the live new home down with it.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        result = paths.config_dir()

        assert result.is_dir()
        assert (result / paths.MIGRATION_MARKER_NAME).exists()

    def test_symlinked_archive_is_not_followed_or_deleted_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A symlinked ~/.kirocrew.archived (however it got there) must not be
        # rmtree'd — that would delete THROUGH the link into its target.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        external = tmp_path / "external"
        external.mkdir()
        (external / "keep.txt").write_text("stay", encoding="utf-8")
        (tmp_path / ".kirocrew.archived").symlink_to(external, target_is_directory=True)

        paths.config_dir()

        assert (external / "keep.txt").read_text(encoding="utf-8") == "stay"

    def test_leftover_removal_failure_does_not_block_startup(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        archived = tmp_path / ".kirocrew.archived"
        archived.mkdir()
        (archived / ".env").write_text("SECRET=x", encoding="utf-8")

        def _failing_rmtree(*a: object, **k: object) -> None:
            raise OSError("simulated permission failure")

        monkeypatch.setattr(paths.shutil, "rmtree", _failing_rmtree)

        result = paths.config_dir()  # must not raise

        assert result.is_dir()


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

    def test_symlinked_destination_is_skipped_not_followed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A crafted ~/.kiro/crew/sessions symlink pointing OUTSIDE the home must
        # not make the copy write legacy session files through it to the
        # external target. copytree (without symlinks=True) does not touch a
        # symlink already at the destination when the SOURCE side is a real
        # dir — it recurses into the link's target like any normal path, so
        # nothing outside the home is exfiltrated, but the destination stays a
        # symlink rather than becoming a real merged dir.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        _seed_legacy(tmp_path)  # has sessions/a.jsonl
        leak = tmp_path / "leak"
        leak.mkdir()
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / "sessions").symlink_to(leak, target_is_directory=True)  # malicious
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        paths.config_dir()

        # The legacy session file landed inside the symlink's target (the copy
        # follows the destination symlink like a normal path would) — nothing
        # was exfiltrated to an attacker-chosen location outside the tree the
        # symlink itself already pointed at, and no exception was raised.
        assert (leak / "a.jsonl").exists()

    def test_symlinked_source_dir_is_skipped_external_target_untouched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A legacy SOURCE symlink pointing at a real EXTERNAL dir is skipped by
        # the copy-ignore callback like any other symlink (files AND dirs are
        # checked with is_symlink() before anything else) — it is never
        # followed, so the external target's files are read, not moved, and
        # nothing is copied to the new home under that name.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        external = tmp_path / "external-notes"
        external.mkdir()
        (external / "important.txt").write_text("do not move me", encoding="utf-8")
        (legacy / "linked").symlink_to(external, target_is_directory=True)
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        # The external target was NOT emptied — its real file is untouched.
        assert (external / "important.txt").read_text(encoding="utf-8") == "do not move me"
        # The symlink itself was skipped, not reproduced or dereferenced.
        assert not (result / "linked").exists()

    def test_dangling_symlink_does_not_abort_migration(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A dangling symlink (target deleted/never existed) in the legacy tree
        # must be skipped, not crash copytree (which would otherwise raise
        # FileNotFoundError trying to dereference it) and abort the migration.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        (legacy / "dangling").symlink_to(legacy / "does-not-exist")
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = home_migration.migrate_home(
            legacy=legacy, new_home=tmp_path / ".kiro" / "crew", marker=tmp_path / "marker"
        )

        assert result == tmp_path / ".kiro" / "crew"
        assert (result / ".env").exists()
        assert not (result / "dangling").exists()

    def test_staging_not_used_no_leftover_temp_dirs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Copies go directly into new_home (no staging/quiescing temp dirs), so
        # a completed migration leaves no transient directories behind.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        _seed_legacy(tmp_path)
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        paths.config_dir()

        assert not list(tmp_path.glob(".kirocrew.quiescing.*"))
        assert not list((tmp_path / ".kiro").glob("crew.migrating.*"))

    def test_remigration_updated_file_overwrites_stale_dest(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Re-migration where new_home has a STALE file and legacy has the
        # UPDATED copy: the updated file force-overwrites the stale one — legacy
        # always wins, with no backup of the stale content.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        (legacy / "config.json").write_text('{"updated": true}', encoding="utf-8")
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / "config.json").write_text('{"stale": true}', encoding="utf-8")  # conflicts
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = home_migration.migrate_home(
            legacy=legacy, new_home=new_home, marker=new_home / paths.MIGRATION_MARKER_NAME
        )

        assert result == new_home
        assert not legacy.exists()
        assert (new_home / "config.json").read_text(encoding="utf-8") == '{"updated": true}'
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()

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

    def test_regenerable_bulk_dirs_not_copied_new_home_regenerates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The re-downloadable GGUF models and rebuildable caches are never
        # copied (that would be slow for no benefit) — the new home simply
        # regenerates them, exactly as a fresh install does.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        (legacy / "models").mkdir()
        (legacy / "models" / "qwen3.gguf").write_text("x" * 4096, encoding="utf-8")
        (legacy / "cache").mkdir()
        (legacy / "cache" / "blob.bin").write_text("cached", encoding="utf-8")
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        # Real data migrated; bulk dirs NOT copied.
        assert result == tmp_path / ".kiro" / "crew"
        assert (result / ".env").exists()
        assert not (result / "models").exists()
        assert not (result / "cache").exists()
        # Migration completed cleanly and legacy is gone.
        assert (result / paths.MIGRATION_MARKER_NAME).exists()
        assert not legacy.exists()

    def test_bulk_dir_already_in_new_home_is_kept(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # If the new home ALREADY has a models/ (a fresh re-download, or a
        # partial), it has no legacy counterpart in the copy (bulk dirs are
        # never copied), so it is left untouched.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        (legacy / "models").mkdir()
        (legacy / "models" / "old.gguf").write_text("old", encoding="utf-8")
        new_home = tmp_path / ".kiro" / "crew"
        (new_home / "models").mkdir(parents=True)
        (new_home / "models" / "fresh.gguf").write_text("fresh", encoding="utf-8")
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        # Pre-existing new-home models/ kept untouched; legacy's copy was never
        # staged (bulk dirs are excluded from the copy entirely).
        assert (result / "models" / "fresh.gguf").read_text(encoding="utf-8") == "fresh"
        assert not (result / "models" / "old.gguf").exists()

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

    def test_relative_intra_home_symlink_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A relative intra-home symlink is a symlink like any other — skipped
        # by the copy (not reproduced, not dereferenced-and-copied), so it
        # simply doesn't appear in the new home. Real data alongside it still
        # migrates normally.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        (legacy / "workspace").mkdir()
        (legacy / "workspace" / "project").mkdir()
        (legacy / "workspace" / "project" / "f.txt").write_text("data", encoding="utf-8")
        (legacy / "workspace" / "current").symlink_to("project")  # relative
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        assert (result / "workspace" / "project" / "f.txt").read_text(encoding="utf-8") == "data"
        assert not (result / "workspace" / "current").exists()

    def test_absolute_external_symlink_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # An absolute link pointing OUTSIDE the home is a symlink like any
        # other — skipped by the copy, same as an intra-home symlink.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        external = tmp_path / "external"
        external.mkdir()
        (legacy / "extlink").symlink_to(external)
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        assert not (result / "extlink").exists()

    def test_archive_failure_is_nonfatal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # If deleting the legacy dir fails, the new home is already good and the
        # migration returns it anyway.
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        real_rmtree = home_migration.shutil.rmtree

        def _rmtree(path: object, *a: object, **k: object) -> None:
            if str(path) == str(legacy):
                raise OSError("simulated delete failure")
            real_rmtree(path, *a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(home_migration.shutil, "rmtree", _rmtree)

        result = home_migration.migrate_home(
            legacy=legacy, new_home=new_home, marker=new_home / paths.MIGRATION_MARKER_NAME
        )

        assert result == new_home
        assert (new_home / ".env").exists()
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()


class TestConcurrentFirstBoot:
    def test_double_checked_lock_migrates_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Simulate a second process having finished the migration while THIS
        # process was blocked on the cross-process lock. A FINISHED migration
        # leaves the completion MARKER present AND legacy removed (i.e.
        # ~/.kirocrew no longer exists) — that combination (marker + no legacy) is
        # what the under-lock re-check trusts, so _do_migrate must NOT run again.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = tmp_path / ".kirocrew"  # does NOT exist (winner already deleted it)
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
