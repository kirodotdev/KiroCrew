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

    def test_marked_new_home_ignores_legacy_writeback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A COMPLETED migration (marker present) then a legacy dir REAPPEARS
        # with data. This design has NO downgrade/rollback path (migration
        # force-deletes legacy; security.py _CREW_HOME_PREFIXES note +
        # config.md "No rollback"), so a legacy dir present after the marker is
        # resurrection DEBRIS, never authoritative. The completion marker is
        # authoritative: the new home is trusted and the debris legacy is NOT
        # promoted over it. (The old rule re-migrated "legacy always wins",
        # which reverted authoritative state — the split-brain data loss GPT
        # 5.6 flagged.)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / "config.json").write_text('{"authoritative": true}', encoding="utf-8")
        (new_home / paths.MIGRATION_MARKER_NAME).write_text("migrated\n", encoding="utf-8")
        # Debris legacy reappears with DIFFERENT (stale) data.
        legacy = _seed_legacy(tmp_path)
        (legacy / "config.json").write_text('{"stale_debris": true}', encoding="utf-8")
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        # The marker wins: authoritative new-home data is preserved, debris
        # is neither promoted nor deleted (left under the protected prefix).
        assert result == new_home
        assert (new_home / "config.json").read_text(
            encoding="utf-8"
        ) == '{"authoritative": true}'
        assert legacy.exists()

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

    def test_readonly_conflicting_dest_file_is_overwritten(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Regression: git writes packfiles (and app-source checkouts under the
        # data home carry them) mode 0o444. When the new home is already
        # populated, the merge must OVERWRITE a read-only destination file — the
        # default copytree copy2 opens the dest for writing and raises
        # PermissionError on a 0o444 file, which aborted the whole migration and
        # trapped the user in a permanent split-brain (legacy authoritative, new
        # home half-populated, gateway pinned to legacy). The custom
        # copy_function must clear the read-only bit so legacy still wins.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        pack_rel = Path("apps") / "x" / ".git" / "objects" / "pack" / "p.pack"
        (legacy / pack_rel).parent.mkdir(parents=True)
        (legacy / pack_rel).write_text("legacy-pack", encoding="utf-8")
        new_home = tmp_path / ".kiro" / "crew"
        (new_home / pack_rel).parent.mkdir(parents=True)
        dest_pack = new_home / pack_rel
        dest_pack.write_text("stale-pack", encoding="utf-8")
        os.chmod(dest_pack, 0o444)  # read-only, exactly like a real git packfile
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)

        result = paths.config_dir()

        assert result == new_home  # migration completed (did not abort)
        assert not legacy.exists()  # legacy deleted → no split-brain
        assert (new_home / pack_rel).read_text(encoding="utf-8") == "legacy-pack"
        assert (new_home / paths.MIGRATION_MARKER_NAME).exists()

    def test_divergent_new_home_gateway_live_joins_new_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Safety + coherence: if a gateway is actively live on the pre-existing
        # new home, migration must NOT force-overwrite underneath it — the
        # calling process JOINS the live gateway's home (NOT legacy; returning
        # legacy pinned every fresh CLI/MCP process to a home whose
        # .local_secret the gateway never loaded → 403 on every internal call
        # + split-brain resurrection). The completion marker is NOT written on
        # this liveness skip — it is reserved for a verified copy, so a
        # fail-safe _gateway_is_live OSError can't brand a partial home as
        # migrated (GPT 5.6 HIGH); the one-time copy completes on a clean cold
        # start.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / "config.json").write_text('{"live": true}', encoding="utf-8")
        # A gateway holds the NEW home's lock (but not legacy's).
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: home == new_home)

        result = paths.config_dir()

        assert result == new_home  # join the live gateway's home
        assert legacy.exists()  # nothing relocated under the running process
        assert (new_home / "config.json").read_text(
            encoding="utf-8"
        ) == '{"live": true}'  # untouched — no forced overwrite
        # Migration still pending: NO completion marker written on a skip
        # (reserved for a verified copy).
        assert not (new_home / paths.MIGRATION_MARKER_NAME).exists()

    def test_marker_authoritative_cold_start_debris_not_promoted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Split-brain data-loss regression (GPT 5.6 HIGH). Migration COMPLETED
        # (marker present) and the new home holds authoritative state. A legacy
        # dir REAPPEARS as debris (a stale pre-move process wrote logs back).
        # This is a COLD start — no gateway live on either home. The OLD rule
        # "marker + legacy present -> re-migrate, legacy always wins" would copy
        # the debris OVER the authoritative new home, reverting same-named files
        # (sel_hmac.key / logs / workspace/). Under the marker-authoritative
        # rule the marker wins unconditionally: new home is returned, the debris
        # legacy is NEVER promoted, and the authoritative state is preserved.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / paths.MIGRATION_MARKER_NAME).write_text("done\n", encoding="utf-8")
        (new_home / "sel_hmac.key").write_text("AUTHORITATIVE", encoding="utf-8")
        legacy = _seed_legacy(tmp_path)  # resurrection debris
        (legacy / "sel_hmac.key").write_text("STALE-DEBRIS", encoding="utf-8")
        # Cold start: no gateway live anywhere. Fail loudly if migrate_home is
        # even reached — the marker must short-circuit before it.
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: False)
        monkeypatch.setattr(
            home_migration, "_do_migrate",
            lambda **kw: pytest.fail("migrate must not run when marker present"),
        )

        result = paths.config_dir()

        assert result == new_home
        # Authoritative state untouched — debris did NOT overwrite it.
        assert (new_home / "sel_hmac.key").read_text(encoding="utf-8") == "AUTHORITATIVE"
        # Debris is left in place and RETAINED (still under the credential-
        # protected ``.kirocrew`` prefix; NOT auto-swept — manual cleanup, now
        # surfaced by config_dir()'s warning + kirocrew doctor).
        assert legacy.exists()

    def test_marker_authoritative_regardless_of_gateway(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The marker short-circuit fires before any gateway probe, so a
        # re-created legacy is benign whether or not a gateway is live — the
        # recreate / TOCTOU race can never promote stale state (GPT 5.6 HIGH).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / paths.MIGRATION_MARKER_NAME).write_text("done\n", encoding="utf-8")
        _seed_legacy(tmp_path)
        # Even asserting a gateway is live on legacy must not change the outcome.
        monkeypatch.setattr(home_migration, "_gateway_is_live", lambda home: True)

        assert paths.config_dir() == new_home

    def test_detect_data_home_conflict_flags_marker_plus_debris(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Observability (GPT 5.6): marker + NON-EMPTY legacy is a conflicted
        # state the detector surfaces (for a config_dir() WARNING + doctor).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / paths.MIGRATION_MARKER_NAME).write_text("done\n", encoding="utf-8")
        # No legacy yet → clean.
        assert paths.detect_data_home_conflict() is None
        # Empty legacy dir → not a conflict (nothing to lose).
        legacy = tmp_path / ".kirocrew"
        legacy.mkdir()
        assert paths.detect_data_home_conflict() is None
        # Non-empty legacy alongside the marker → conflict, with cleanup hint.
        (legacy / "audit.log").write_text("stale", encoding="utf-8")
        msg = paths.detect_data_home_conflict()
        assert msg is not None and str(legacy) in msg and "rm -rf" in msg

    def test_detect_data_home_conflict_none_under_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # KIROCREW_HOME override never migrates → never a conflict.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "override"))
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / paths.MIGRATION_MARKER_NAME).write_text("done\n", encoding="utf-8")
        legacy = _seed_legacy(tmp_path)
        assert legacy.is_dir()
        assert paths.detect_data_home_conflict() is None

    def test_detect_data_home_conflict_invalid_override_still_detects(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # An INVALID (system-dir) KIROCREW_HOME is rejected by config_dir and
        # falls back to the default home, so the conflict check must STILL run —
        # it must not be suppressed like a valid override (GPT 5.6 MEDIUM).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", "/")  # system dir → rejected
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / paths.MIGRATION_MARKER_NAME).write_text("done\n", encoding="utf-8")
        _seed_legacy(tmp_path)  # non-empty debris
        assert paths.detect_data_home_conflict() is not None  # not suppressed

    def test_config_dir_warns_on_conflict(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The marker branch emits a one-time WARNING when a non-empty legacy
        # coexists — the silent conflicted state is now observable.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / paths.MIGRATION_MARKER_NAME).write_text("done\n", encoding="utf-8")
        _seed_legacy(tmp_path)  # non-empty debris
        with caplog.at_level("WARNING"):
            assert paths.config_dir() == new_home
        assert any("data-home conflict" in r.message for r in caplog.records)

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
    def test_marker_present_under_lock_never_recopies_debris(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Concurrent-starter race (GPT 5.6 HIGH): two starters both saw no
        # marker and entered migrate_home; the winner migrated and wrote the
        # marker but could NOT delete legacy (permission / open handle), so the
        # legacy dir still exists. The blocked second starter, now under the
        # lock, must treat the marker as authoritative and return new_home —
        # NOT fall through to _do_migrate and recopy the now-debris legacy over
        # the authoritative new home. (Pre-fix the under-lock recheck required
        # `not legacy.is_dir()`, so a surviving legacy re-triggered the copy.)
        legacy = _seed_legacy(tmp_path)
        (legacy / "config.json").write_text('{"stale_debris": true}', encoding="utf-8")
        new_home = tmp_path / ".kiro" / "crew"
        new_home.mkdir(parents=True)
        (new_home / "config.json").write_text('{"authoritative": true}', encoding="utf-8")
        marker = new_home / paths.MIGRATION_MARKER_NAME
        marker.write_text("done\n", encoding="utf-8")  # winner already marked
        # Fail loudly if the destructive copy is even reached.
        monkeypatch.setattr(
            home_migration, "_do_migrate",
            lambda **kw: pytest.fail("must not migrate when marker present under lock"),
        )

        result = home_migration.migrate_home(legacy=legacy, new_home=new_home, marker=marker)

        assert result == new_home
        assert (new_home / "config.json").read_text(
            encoding="utf-8"
        ) == '{"authoritative": true}'  # debris did not overwrite it
        assert legacy.is_dir()  # debris retained, not promoted

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


def _seed_legacy_venv(legacy: Path, name: str = "venv") -> Path:
    """Add a representative managed venv inside the legacy home.

    Mirrors what ``cli.sh`` produced when it created its venv under the data
    home: a ``pyvenv.cfg`` plus a console script whose shebang hard-codes the
    absolute interpreter path (the part that made a copied venv unusable).
    """
    venv = legacy / name
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {venv}/bin\n", encoding="utf-8")
    (venv / "bin" / "kirocrew").write_text(
        f"#!{venv}/bin/python3.13\nprint('hi')\n", encoding="utf-8"
    )
    (venv / "lib" / "python3.13" / "site-packages" / "kiro_crew").mkdir(parents=True)
    (venv / "lib" / "python3.13" / "site-packages" / "kiro_crew" / "cli.py").write_text(
        "# entry point\n", encoding="utf-8"
    )
    return venv


class TestNestedVenvIsPreserved:
    """The migration must never move or delete a venv nested in the legacy home.

    Regression for the install-destroying bug: ``cli.sh`` used to create its
    managed venv at ``~/.kirocrew/venv``, so the blanket ``rmtree(legacy)``
    deleted the running interpreter and its ``site-packages`` mid-migration.
    The user was left with a dangling ``~/.local/bin/kirocrew`` symlink, a
    ``ModuleNotFoundError`` from the half-unloaded current process, and a copied
    venv at the new home whose shebang pointed at the now-deleted interpreter.
    """

    def test_data_migrates_while_venv_stays_put(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        venv = _seed_legacy_venv(legacy)

        result = paths.config_dir()

        # Data moved as usual.
        assert (result / ".env").read_text(encoding="utf-8") == "SLACK_BOT_TOKEN=xoxb-secret"
        assert (result / "sessions" / "a.jsonl").read_text(encoding="utf-8") == "hello"
        assert (result / paths.MIGRATION_MARKER_NAME).exists()

        # The venv survives IN PLACE, byte-for-byte, and the legacy root with it.
        assert legacy.is_dir()
        assert venv.is_dir()
        assert (venv / "pyvenv.cfg").read_text(encoding="utf-8") == f"home = {venv}/bin\n"
        assert (venv / "bin" / "kirocrew").read_text(encoding="utf-8").startswith(
            f"#!{venv}/bin/python3.13"
        )
        assert (venv / "lib" / "python3.13" / "site-packages" / "kiro_crew" / "cli.py").exists()

        # Legacy DATA is still removed — only the venv is kept.
        assert not (legacy / ".env").exists()
        assert not (legacy / "sessions").exists()
        assert not (legacy / "config.json").exists()
        assert sorted(p.name for p in legacy.iterdir()) == ["venv"]

        # The venv is NOT copied to the new home: it is not relocatable, so a
        # copy there would be a dead-on-arrival interpreter.
        assert not (result / "venv").exists()

    @pytest.mark.parametrize("name", ["venv", ".venv", "venvs"])
    def test_every_preserved_name_survives(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        venv = _seed_legacy_venv(legacy, name=name)

        result = paths.config_dir()

        assert venv.is_dir()
        assert (venv / "pyvenv.cfg").exists()
        assert not (result / name).exists()
        assert (result / paths.MIGRATION_MARKER_NAME).exists()

    def test_legacy_root_still_removed_when_no_venv_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Without a venv the behavior is unchanged: the legacy root goes away
        # entirely, so this fix does not leave empty directories behind.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)

        paths.config_dir()

        assert not legacy.exists()

    def test_a_file_named_venv_is_treated_as_data(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Only DIRECTORIES are preserved. A regular file that happens to be
        # named "venv" is ordinary data and must migrate like anything else.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        (legacy / "venv").write_text("not a venv", encoding="utf-8")

        result = paths.config_dir()

        assert (result / "venv").read_text(encoding="utf-8") == "not a venv"
        assert not legacy.exists()

    @pytest.mark.parametrize("name", ["models", "cache", "venv"])
    def test_file_sharing_an_excluded_dir_name_does_not_stall_migration(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
    ) -> None:
        # The top-level skip list names DIRECTORIES, but ``_verify_copy`` prunes
        # only ``dirs``. Skipping a same-named regular FILE would therefore make
        # verification report it missing and abort the migration on EVERY start
        # — a permanent stall. The copy-ignore gates on is_dir() to prevent that.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        (legacy / name).write_text("regular file", encoding="utf-8")

        result = paths.config_dir()

        assert (result / name).read_text(encoding="utf-8") == "regular file"
        assert (result / paths.MIGRATION_MARKER_NAME).exists()
        assert not legacy.exists()


class TestPreservedVenvIsNotReportedAsConflict:
    """A legacy dir kept only for its venv is expected, not resurrection debris."""

    def test_venv_only_legacy_is_not_a_conflict(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        _seed_legacy_venv(legacy)

        paths.config_dir()

        # Marker present + legacy dir present, but its only content is the
        # preserved venv -> no conflict, so no scary warning and no instruction
        # to delete what is actually the user's live interpreter.
        assert paths.detect_data_home_conflict() is None

    def test_real_writeback_debris_is_still_flagged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Guard against over-suppression: a non-venv leftover beside the venv is
        # genuine resurrection debris and must still surface.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        _seed_legacy_venv(legacy)

        paths.config_dir()
        (legacy / "config.json").write_text("{}", encoding="utf-8")

        conflict = paths.detect_data_home_conflict()
        assert conflict is not None
        assert str(legacy) in conflict

    def test_preserved_entries_lists_only_directories(self, tmp_path: Path) -> None:
        (tmp_path / "venv").mkdir()
        (tmp_path / ".venv").mkdir()
        (tmp_path / "venvs").write_text("file, not a dir", encoding="utf-8")
        (tmp_path / "sessions").mkdir()

        assert paths.preserved_entries(tmp_path) == [".venv", "venv"]

    def test_preserved_entries_on_missing_home_is_empty(self, tmp_path: Path) -> None:
        assert paths.preserved_entries(tmp_path / "nope") == []


class TestRunningInterpreterFailSafe:
    """Never delete the legacy tree when this process runs from inside it."""

    def test_uncovered_interpreter_layout_aborts_migration(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        # An interpreter under the legacy home that _PRESERVED_TOP_LEVEL_DIRS
        # does NOT cover (hand-rolled layout). Migrating would delete it.
        odd = legacy / "runtime" / "py"
        odd.mkdir(parents=True)
        monkeypatch.setattr(home_migration.sys, "prefix", str(odd))

        result = home_migration.migrate_home(
            legacy=legacy,
            new_home=tmp_path / ".kiro" / "crew",
            marker=tmp_path / ".kiro" / "crew" / paths.MIGRATION_MARKER_NAME,
        )

        # Declined: stays on legacy, data intact, no marker stamped.
        assert result == legacy
        assert (legacy / ".env").exists()
        assert odd.is_dir()
        assert not (tmp_path / ".kiro" / "crew" / paths.MIGRATION_MARKER_NAME).exists()

    def test_covered_venv_layout_still_migrates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The common case: the interpreter IS the preserved venv. The fail-safe
        # must not fire, because the venv is already protected from deletion.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        venv = _seed_legacy_venv(legacy)
        monkeypatch.setattr(home_migration.sys, "prefix", str(venv))

        result = paths.config_dir()

        assert result == tmp_path / ".kiro" / "crew"
        assert (result / ".env").exists()
        assert venv.is_dir()
        assert (result / paths.MIGRATION_MARKER_NAME).exists()

    def test_interpreter_outside_legacy_is_not_flagged(self, tmp_path: Path) -> None:
        legacy = tmp_path / ".kirocrew"
        legacy.mkdir()
        assert home_migration._running_interpreter_under(legacy) is False

    def test_interpreter_at_legacy_root_is_flagged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        legacy = tmp_path / ".kirocrew"
        legacy.mkdir()
        monkeypatch.setattr(home_migration.sys, "prefix", str(legacy))
        assert home_migration._running_interpreter_under(legacy) is True


class TestFailSafeChecksContainmentNotExistence:
    """The fail-safe must verify the interpreter IS a preserved venv.

    Regression for a hole in the first cut of this fix: the guard read
    ``not preserved_entries(legacy)``, so the mere EXISTENCE of any preserved
    directory disabled it. An unrelated helper venv at ``<legacy>/venv`` would
    vouch for an interpreter at ``<legacy>/runtime``, and the migration went on
    to delete ``<legacy>/runtime`` — destroying the running install, which is
    the precise failure this module exists to prevent.
    """

    def test_unrelated_preserved_venv_does_not_vouch_for_interpreter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        # A preserved venv EXISTS, but it is not the interpreter we run from.
        helper = _seed_legacy_venv(legacy)
        running = legacy / "runtime" / "py"
        (running / "lib").mkdir(parents=True)
        (running / "lib" / "marker.txt").write_text("live interpreter", encoding="utf-8")
        monkeypatch.setattr(home_migration.sys, "prefix", str(running))

        result = home_migration.migrate_home(
            legacy=legacy,
            new_home=tmp_path / ".kiro" / "crew",
            marker=tmp_path / ".kiro" / "crew" / paths.MIGRATION_MARKER_NAME,
        )

        # Declined: the running interpreter is untouched and legacy data intact.
        assert result == legacy
        assert (running / "lib" / "marker.txt").read_text(encoding="utf-8") == (
            "live interpreter"
        )
        assert (legacy / ".env").exists()
        assert helper.is_dir()
        assert not (tmp_path / ".kiro" / "crew" / paths.MIGRATION_MARKER_NAME).exists()

    def test_interpreter_nested_deep_inside_preserved_venv_still_migrates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Containment, not equality: a prefix BELOW the preserved dir (as a real
        # venv layout has) must still count as protected so the move proceeds.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        venv = _seed_legacy_venv(legacy)
        nested = venv / "lib" / "python3.13"
        monkeypatch.setattr(home_migration.sys, "prefix", str(nested))

        result = paths.config_dir()

        assert result == tmp_path / ".kiro" / "crew"
        assert (result / ".env").exists()
        assert venv.is_dir()

    def test_interpreter_is_preserved_requires_containment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "venv").mkdir()
        (tmp_path / "runtime").mkdir()

        monkeypatch.setattr(home_migration.sys, "prefix", str(tmp_path / "venv"))
        assert home_migration._interpreter_is_preserved(tmp_path) is True

        monkeypatch.setattr(home_migration.sys, "prefix", str(tmp_path / "venv" / "x"))
        assert home_migration._interpreter_is_preserved(tmp_path) is True

        # Exists-but-unrelated: the hole this test class exists for.
        monkeypatch.setattr(home_migration.sys, "prefix", str(tmp_path / "runtime"))
        assert home_migration._interpreter_is_preserved(tmp_path) is False

    def test_interpreter_is_preserved_fails_safe_when_unresolvable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # An unreadable prefix must report NOT-preserved, so the caller declines
        # the migration rather than deleting an unverifiable layout.
        (tmp_path / "venv").mkdir()
        monkeypatch.setattr(home_migration, "_resolved_prefix", lambda: None)
        assert home_migration._interpreter_is_preserved(tmp_path) is False

    def test_symlinked_preserved_dir_still_protects_interpreter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The comparison resolves both sides, so a preserved entry reached
        # through a symlink still matches a prefix expressed as the real path.
        legacy = tmp_path / ".kirocrew"
        legacy.mkdir()
        real = tmp_path / "real-venv"
        real.mkdir()
        (legacy / "venv").symlink_to(real, target_is_directory=True)
        monkeypatch.setattr(home_migration.sys, "prefix", str(real))

        assert home_migration._interpreter_is_preserved(legacy) is True

    def test_path_contains_algebra(self, tmp_path: Path) -> None:
        root = tmp_path / "a"
        assert home_migration._path_contains(root, root) is True
        assert home_migration._path_contains(root, root / "b" / "c") is True
        assert home_migration._path_contains(root, tmp_path / "ab") is False
        assert home_migration._path_contains(root / "b", root) is False


def _seed_app_venv(legacy: Path, app: str = "issue-radar") -> Path:
    """Add a per-app venv at ``apps/<app>/.venv``, as apps/backend.py creates.

    Includes the ``bin/python`` SYMLINK a real venv carries, because the copy
    deliberately skips symlinks — that is precisely why a copied app venv ends
    up without an interpreter.
    """
    root = legacy / "apps" / app
    venv = root / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {venv}/bin\n", encoding="utf-8")
    (venv / "bin" / "pip").write_text(f"#!{venv}/bin/python\n", encoding="utf-8")
    (root / "requirements.txt").write_text("requests\n", encoding="utf-8")
    (root / "app.json").write_text('{"name": "x"}', encoding="utf-8")
    return venv


class TestNestedAppVenvsAreNotRelocated:
    """A venv below the legacy root must not be copied either.

    ``_PRESERVED_TOP_LEVEL_DIRS`` only matches the root, so an installed app's
    ``apps/<name>/.venv`` was still being copied. A copied venv is broken (the
    walk skips symlinks, so ``bin/python`` never arrives, and shebangs point into
    the deleted legacy tree), and ``apps/backend.py`` only rebuilds when the
    directory is ABSENT — so the app was left permanently unable to install its
    dependencies. Excluding it from the copy lets the app recreate a working one.
    """

    def test_app_venv_is_not_copied_but_app_data_is(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        _seed_app_venv(legacy)

        result = paths.config_dir()

        # The app itself migrated — only its venv was left out.
        assert (result / "apps" / "issue-radar" / "app.json").exists()
        assert (result / "apps" / "issue-radar" / "requirements.txt").exists()
        assert not (result / "apps" / "issue-radar" / ".venv").exists()

        # Absent (not broken) at the new home is what lets the app rebuild it.
        assert (result / paths.MIGRATION_MARKER_NAME).exists()
        assert not legacy.exists()

    def test_nested_venv_does_not_stall_verification(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The copy skips the nested venv, so verification must prune it too —
        # otherwise its files count as "missing", the copy is judged incomplete,
        # and the migration aborts and retries on every single start.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        _seed_app_venv(legacy)

        assert home_migration._verify_copy(legacy, tmp_path / "empty-dest") != []

        result = paths.config_dir()

        # Marker written + legacy gone proves verification passed.
        assert (result / paths.MIGRATION_MARKER_NAME).exists()
        assert not legacy.exists()

    def test_venv_detected_by_marker_not_name(self, tmp_path: Path) -> None:
        oddly_named = tmp_path / "py3-env"
        oddly_named.mkdir()
        (oddly_named / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        assert home_migration._is_venv_dir(oddly_named) is True

        # A directory merely NAMED like a venv but without the marker is data.
        decoy = tmp_path / ".venv"
        decoy.mkdir()
        (decoy / "notes.txt").write_text("user data", encoding="utf-8")
        assert home_migration._is_venv_dir(decoy) is False

    def test_nested_dir_named_venv_without_marker_still_migrates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Detection is by pyvenv.cfg, so a nested user directory that happens to
        # be called .venv but is not one must be carried over as ordinary data.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        decoy = legacy / "apps" / "notes" / ".venv"
        decoy.mkdir(parents=True)
        (decoy / "keep.txt").write_text("real user data", encoding="utf-8")

        result = paths.config_dir()

        assert (result / "apps" / "notes" / ".venv" / "keep.txt").read_text(
            encoding="utf-8"
        ) == "real user data"


class TestUnmanagedNestedVenvsAreNeverLost:
    """Only a MANAGED app venv may be dropped from the copy.

    Regression for the trade the previous round got wrong: excluding *every*
    nested venv from the copy meant a user's own environment (e.g.
    ``tools/myenv``) was skipped and then removed with the legacy tree —
    permanent data loss, strictly worse than the broken copy it replaced.
    Silently discarding is only safe for ``apps/<name>/.venv``, which its owner
    rebuilds. For anything else the migration declines and says which paths to
    move.
    """

    def test_user_nested_venv_defers_migration_instead_of_deleting_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        mine = legacy / "tools" / "myenv"
        (mine / "lib").mkdir(parents=True)
        (mine / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        (mine / "lib" / "payload.txt").write_text("irreplaceable", encoding="utf-8")

        result = paths.config_dir()

        # Declined: still on legacy, the environment and its contents intact.
        assert result == legacy
        assert (mine / "lib" / "payload.txt").read_text(encoding="utf-8") == (
            "irreplaceable"
        )
        assert (legacy / ".env").exists()
        # No marker, so the move retries once the user relocates it.
        assert not (tmp_path / ".kiro" / "crew" / paths.MIGRATION_MARKER_NAME).exists()

    def test_managed_app_venv_alone_still_migrates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The one environment we own does NOT block the move — it is skipped and
        # rebuilt, which is the whole point of the previous round's fix.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        legacy = _seed_legacy(tmp_path)
        _seed_app_venv(legacy)

        result = paths.config_dir()

        assert result == tmp_path / ".kiro" / "crew"
        assert (result / "apps" / "issue-radar" / "app.json").exists()
        assert not (result / "apps" / "issue-radar" / ".venv").exists()
        assert not legacy.exists()

    def test_managed_app_venv_matched_structurally(self) -> None:
        assert home_migration._is_managed_app_venv(Path("apps/radar/.venv")) is True
        # Wrong depth, wrong root, or wrong leaf name are all NOT managed.
        assert home_migration._is_managed_app_venv(Path("apps/radar/sub/.venv")) is False
        assert home_migration._is_managed_app_venv(Path("tools/radar/.venv")) is False
        assert home_migration._is_managed_app_venv(Path("apps/radar/venv")) is False
        assert home_migration._is_managed_app_venv(Path(".venv")) is False

    def test_scan_reports_only_unmanaged(self, tmp_path: Path) -> None:
        def mk(rel: str) -> None:
            d = tmp_path / rel
            d.mkdir(parents=True)
            (d / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")

        mk("apps/radar/.venv")  # managed -> not reported
        mk("tools/myenv")  # unmanaged -> reported
        mk("scratch/deep/env")  # unmanaged -> reported

        found = home_migration._unmanaged_nested_venvs(tmp_path)
        assert sorted(found) == ["scratch/deep/env".replace("/", os.sep),
                                 "tools/myenv".replace("/", os.sep)]

    def test_scan_ignores_root_preserved_and_bulk_dirs(self, tmp_path: Path) -> None:
        # Root-level venvs are preserved in place (handled separately) and the
        # regenerable bulk dirs are never copied, so neither should trip the
        # abort and block every legacy install from ever migrating.
        for name in ("venv", ".venv", "venvs", "models", "cache"):
            d = tmp_path / name
            d.mkdir()
            (d / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        assert home_migration._unmanaged_nested_venvs(tmp_path) == []

    def test_scan_does_not_descend_into_a_venv(self, tmp_path: Path) -> None:
        # A venv contains nested environments in some layouts; the outer one is
        # reported once rather than every environment inside it.
        outer = tmp_path / "tools" / "outer"
        inner = outer / "share" / "inner"
        inner.mkdir(parents=True)
        (outer / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        (inner / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")

        assert home_migration._unmanaged_nested_venvs(tmp_path) == [
            str(Path("tools") / "outer")
        ]

    def test_unmanaged_venv_is_copied_not_skipped(self, tmp_path: Path) -> None:
        # Belt-and-braces: if the abort were ever bypassed, the copy must still
        # treat a user venv as ordinary data rather than silently dropping it.
        legacy = tmp_path / ".kirocrew"
        mine = legacy / "tools" / "myenv"
        mine.mkdir(parents=True)
        (mine / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        ignore = home_migration._make_copy_ignore(legacy)
        assert ignore(str(legacy / "tools"), ["myenv"]) == set()
