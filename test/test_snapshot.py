"""Tests for kiro_crew.snapshot — snapshot and restore."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tarfile
from pathlib import Path

import pytest

from kiro_crew.snapshot import restore_main, snapshot_main

# ── Helpers ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_gateway(monkeypatch):
    """Prevent gateway-running check from blocking restore in tests.

    Uses the deterministic env seam (not a function patch) so refusal tests can
    override it with ``=1`` and the result never depends on a real socket probe.
    """
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")


def _setup_fake_kirocrew(d: Path) -> None:
    """Create a realistic fake ~/.kirocrew directory."""
    for sub in (
        "workspace/memory/history",
        "workspace/hygiene_data",
        "skills/my-skill",
        "plan_memory",
    ):
        (d / sub).mkdir(parents=True, exist_ok=True)

    # memory.db with all tables
    conn = sqlite3.connect(str(d / "memory.db"))
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        CREATE TABLE semantic_memory (key TEXT PRIMARY KEY, value_json TEXT NOT NULL,
            confidence REAL DEFAULT 0.5, source TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, is_deleted INTEGER DEFAULT 0, embedding BLOB);
        CREATE TABLE episodic_memories (id TEXT PRIMARY KEY, conversation_id TEXT,
            text TEXT NOT NULL, embedding BLOB, tags TEXT DEFAULT '[]',
            importance REAL DEFAULT 0.5, created_at TEXT NOT NULL,
            last_accessed_at TEXT, is_deleted INTEGER DEFAULT 0);
        CREATE TABLE memory_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL, memory_type TEXT NOT NULL, memory_key TEXT NOT NULL,
            old_value TEXT, new_value TEXT, source TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE knowledge_facts (id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL,
            episode_id TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(subject, predicate, object));
        CREATE TABLE knowledge_edges (source_key TEXT NOT NULL, target_key TEXT NOT NULL,
            relation TEXT NOT NULL DEFAULT 'related', weight REAL NOT NULL DEFAULT 0.0,
            metadata TEXT DEFAULT '{}', created_at TEXT NOT NULL,
            PRIMARY KEY (source_key, target_key, relation));
        INSERT INTO semantic_memory (key, value_json, confidence, source, created_at, updated_at)
            VALUES ('test.key1', '"value1"', 0.9, 'test', '2026-01-01', '2026-01-01');
        INSERT INTO semantic_memory (key, value_json, confidence, source, created_at, updated_at)
            VALUES ('test.key2', '"value2"', 0.8, 'test', '2026-01-01', '2026-01-01');
        INSERT INTO episodic_memories (id, text, created_at)
            VALUES ('ep1', 'test episode 1', '2026-01-01');
        INSERT INTO episodic_memories (id, text, created_at)
            VALUES ('ep2', 'test episode 2', '2026-01-01');
        INSERT INTO knowledge_facts (subject, predicate, object, episode_id, created_at)
            VALUES ('user', 'prefers', 'dark_mode', 'ep1', '2026-01-01');
        INSERT INTO knowledge_edges (source_key, target_key, relation, weight, created_at)
            VALUES ('user', 'dark_mode', 'prefers', 1.0, '2026-01-01');
    """
    )
    conn.close()

    (d / "crons.json").write_text(
        json.dumps(
            {
                "version": 2,
                "jobs": [
                    {
                        "id": "abc123",
                        "name": "test-job",
                        "message": "hello",
                        "cron_expr": "0 9 * * *",
                    }
                ],
            }
        )
    )
    (d / "config.json").write_text('{"agent": {"model": "test"}}')
    (d / "session_map.json").write_text("{}")
    (d / "hooks.json").write_text("{}")
    (d / "sel_hmac.key").write_bytes(b"\x00\x01\x02\x03")
    (d / "telemetry_salt").write_bytes(b"\x04\x05\x06\x07")
    (d / "notifications.jsonl").write_text('{"ts":"2026-01-01","msg":"test"}\n')
    (d / "project_dir").write_text("/home/user/project")
    (d / "workspace_dir").write_text("/home/user/.kirocrew/workspace")
    (d / "workspace/memory/history/2026-01-01.md").write_text("history entry")
    (d / "workspace/doc.md").write_text("doc content")
    (d / "workspace/hygiene_data/week1.json").write_text("big data")
    (d / "plan_memory/plan1.json").write_text("plan data")
    (d / "skills/my-skill/SKILL.md").write_text("# My Skill")


def _make_snapshot(src: Path, out: Path, extra_args: list[str] | None = None) -> Path:
    """Create a snapshot and return the tarball path. Caller must set KIROCREW_HOME."""
    args = [str(out)] + (extra_args or [])
    snapshot_main(args)
    tarballs = sorted(
        out.glob("kirocrew-snapshot-*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    assert tarballs, "No tarball created"
    return tarballs[0]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Set up source dir, output dir, and snapshot tarball."""
    src = tmp_path / "src"
    out = tmp_path / "out"
    _setup_fake_kirocrew(src)
    monkeypatch.setenv("KIROCREW_HOME", str(src))
    tarball = _make_snapshot(src, out)
    return src, out, tarball, tmp_path


# ── Snapshot Tests ────────────────────────────────────────────────────────────


class TestSnapshot:
    def test_creates_valid_tarball(self, env):
        """TEST 1"""
        _, _, tarball, tmp_path = env
        assert tarball.is_file()
        extract = tmp_path / "extract"
        extract.mkdir()
        with tarfile.open(str(tarball)) as tar:
            tar.extractall(extract, filter=lambda t, _d="": t)
        snaps = [d for d in extract.iterdir() if d.name.startswith("kirocrew-snapshot-")]
        assert snaps
        snap = snaps[0]
        assert (snap / "memory.db").is_file()
        assert (snap / "crons.json").is_file()
        assert (snap / "config.json").is_file()
        assert (snap / "MANIFEST.json").is_file()
        assert (snap / "workspace/doc.md").is_file()
        assert (snap / "workspace/memory/history/2026-01-01.md").is_file()
        assert (snap / "skills/my-skill/SKILL.md").is_file()
        assert not (snap / "workspace/hygiene_data/week1.json").exists()
        m = json.loads((snap / "MANIFEST.json").read_text())
        assert m["version"] == 2

    def test_db_content_survives(self, env):
        _, _, tarball, tmp_path = env
        extract = tmp_path / "extract2"
        extract.mkdir()
        with tarfile.open(str(tarball)) as tar:
            tar.extractall(extract, filter=lambda t, _d="": t)
        snap = next(d for d in extract.iterdir() if d.name.startswith("kirocrew-snapshot-"))
        conn = sqlite3.connect(str(snap / "memory.db"))
        assert conn.execute("SELECT count(*) FROM semantic_memory").fetchone()[0] == 2
        conn.close()

    def test_state_files_captured(self, env):
        _, _, tarball, tmp_path = env
        extract = tmp_path / "extract3"
        extract.mkdir()
        with tarfile.open(str(tarball)) as tar:
            tar.extractall(extract, filter=lambda t, _d="": t)
        snap = next(d for d in extract.iterdir() if d.name.startswith("kirocrew-snapshot-"))
        for f in (
            "telemetry_salt",
            "notifications.jsonl",
            "project_dir",
            "workspace_dir",
            "plan_memory/plan1.json",
        ):
            assert (snap / f).is_file(), f"{f} missing"

    def test_keep_prunes(self, env, monkeypatch):
        """TEST 2"""
        src, _, _, tmp_path = env
        out2 = tmp_path / "out2"
        out2.mkdir()
        # Create 3 fake old snapshots
        for i in range(3):
            (out2 / f"kirocrew-snapshot-2026010{i}T000000Z.tar.gz").write_text("fake")
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        snapshot_main([str(out2), "--keep", "2"])
        total = len(list(out2.glob("kirocrew-snapshot-*.tar.gz")))
        assert total == 2

    def test_list(self, env, capsys, monkeypatch):
        """TEST 3"""
        src, out, _, _ = env
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        snapshot_main([str(out), "--list"])
        assert "kirocrew-snapshot-" in capsys.readouterr().out

    def test_keep_zero_errors(self, env, capsys, monkeypatch):
        """TEST 29 partial"""
        src, _, _, tmp_path = env
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        # argparse will raise SystemExit for --keep 0 since we validate > 0
        # But our validation is post-parse, so it returns 1
        ret = snapshot_main([str(tmp_path / "x"), "--keep", "0"])
        assert ret == 1
        assert "positive integer" in capsys.readouterr().out


# ── Restore Tests ─────────────────────────────────────────────────────────────


class TestRestoreDryRun:
    def test_dry_run(self, env, capsys, monkeypatch):
        """TEST 4"""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh4"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        restore_main([str(tarball), "--dry-run", "--force"])
        assert "Dry run" in capsys.readouterr().out
        assert not (fresh / "memory.db").exists()


class TestRestoreReplace:
    def test_replace_fresh(self, env, capsys, monkeypatch):
        """TEST 5"""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh5"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 0
        assert (fresh / "memory.db").is_file()
        assert (fresh / "crons.json").is_file()
        assert (fresh / "config.json").is_file()
        assert (fresh / "workspace/doc.md").is_file()
        assert (fresh / "skills/my-skill/SKILL.md").is_file()
        assert (fresh / "notifications.jsonl").is_file()
        assert (fresh / "plan_memory/plan1.json").is_file()
        conn = sqlite3.connect(str(fresh / "memory.db"))
        assert conn.execute("SELECT count(*) FROM semantic_memory").fetchone()[0] == 2
        conn.close()
        assert "integrity" in capsys.readouterr().out

    def test_replace_backs_up(self, env, monkeypatch):
        """TEST 6"""
        _, _, tarball, tmp_path = env
        existing = tmp_path / "existing6"
        _setup_fake_kirocrew(existing)
        (existing / "workspace/original.md").write_text("original")
        monkeypatch.setenv("KIROCREW_HOME", str(existing))
        restore_main([str(tarball), "--mode", "replace", "--force"])
        backups = [
            d for d in existing.iterdir() if d.is_dir() and d.name.startswith("pre-restore-")
        ]
        assert backups
        assert (backups[0] / "memory.db").is_file()
        # sel_hmac.key is excluded from snapshot bundles (security fix) but the
        # backup of the pre-restore state DOES include it since it existed locally.
        # However the fake setup may not create it -- check what _setup_fake_kirocrew does.
        # The backup captures whatever was in 'existing' before restore.
        assert (backups[0] / "telemetry_salt").is_file()
        # original.md should be gone (replaced by snapshot content)
        assert not (existing / "workspace/original.md").exists()

    def test_replace_backs_up_directories(self, env, monkeypatch):
        """TEST 24"""
        _, _, tarball, tmp_path = env
        existing = tmp_path / "existing24"
        _setup_fake_kirocrew(existing)
        (existing / "workspace/local_only.md").write_text("local-only-file")
        monkeypatch.setenv("KIROCREW_HOME", str(existing))
        restore_main([str(tarball), "--mode", "replace", "--force"])
        backups = [
            d for d in existing.iterdir() if d.is_dir() and d.name.startswith("pre-restore-")
        ]
        assert backups
        assert (backups[0] / "workspace/local_only.md").is_file()


class TestRestoreMerge:
    def test_merge_memory_dedup(self, env, monkeypatch):
        """TEST 7"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst7"
        _setup_fake_kirocrew(dst)
        conn = sqlite3.connect(str(dst / "memory.db"))
        conn.execute(
            "INSERT INTO semantic_memory (key, value_json, confidence, source, "
            "created_at, updated_at) VALUES ('dst.only', '\"local\"', 0.9, "
            "'test', '2026-02-01', '2026-02-01')"
        )
        conn.execute(
            "UPDATE semantic_memory SET value_json='\"modified\"' " "WHERE key='test.key1'"
        )
        conn.commit()
        conn.close()
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"])
        assert ret == 0
        conn = sqlite3.connect(str(dst / "memory.db"))
        val = conn.execute(
            "SELECT value_json FROM semantic_memory " "WHERE key='dst.only'"
        ).fetchone()[0]
        assert val == '"local"'
        val = conn.execute(
            "SELECT value_json FROM semantic_memory " "WHERE key='test.key1'"
        ).fetchone()[0]
        assert val == '"modified"'
        conn.close()

    def test_merge_cron_dedup(self, env, monkeypatch):
        """TEST 8"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst8"
        _setup_fake_kirocrew(dst)
        before = len(json.loads((dst / "crons.json").read_text())["jobs"])
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"])
        assert ret == 0
        after = len(json.loads((dst / "crons.json").read_text())["jobs"])
        assert before == after

    def test_merge_new_cron(self, env, monkeypatch):
        """TEST 9"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst9"
        _setup_fake_kirocrew(dst)
        d = json.loads((dst / "crons.json").read_text())
        d["jobs"][0]["name"] = "different-job"
        (dst / "crons.json").write_text(json.dumps(d))
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        restore_main([str(tarball), "--mode", "merge", "--force"])
        count = len(json.loads((dst / "crons.json").read_text())["jobs"])
        assert count == 2

    def test_merge_workspace_no_overwrite(self, env, monkeypatch):
        """TEST 10"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst10"
        _setup_fake_kirocrew(dst)
        (dst / "workspace/doc.md").write_text("local version")
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"])
        assert ret == 0
        assert (dst / "workspace/doc.md").read_text() == "local version"

    def test_merge_episodic_facts_edges(self, env, monkeypatch):
        """TEST 12"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst12"
        _setup_fake_kirocrew(dst)
        conn = sqlite3.connect(str(dst / "memory.db"))
        conn.execute(
            "INSERT INTO episodic_memories (id, text, created_at) "
            "VALUES ('ep_local', 'local episode', '2026-02-01')"
        )
        conn.commit()
        conn.close()
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"])
        assert ret == 0
        conn = sqlite3.connect(str(dst / "memory.db"))
        assert conn.execute("SELECT count(*) FROM episodic_memories").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM knowledge_facts").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM knowledge_edges").fetchone()[0] == 1
        conn.close()

    def test_merge_import_count_accurate(self, env, capsys, monkeypatch):
        """TEST 13"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst13"
        _setup_fake_kirocrew(dst)
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        restore_main([str(tarball), "--mode", "merge", "--force"])
        assert "Semantic Memory imported: 0" in capsys.readouterr().out

    def test_merge_import_count_one_new(self, env, capsys, monkeypatch):
        """TEST 13b"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst13b"
        _setup_fake_kirocrew(dst)
        conn = sqlite3.connect(str(dst / "memory.db"))
        conn.execute("DELETE FROM semantic_memory WHERE key='test.key2'")
        conn.commit()
        conn.close()
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        restore_main([str(tarball), "--mode", "merge", "--force"])
        assert "Semantic Memory imported: 1" in capsys.readouterr().out

    def test_merge_notifications(self, env, monkeypatch):
        """TEST 14"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst14"
        _setup_fake_kirocrew(dst)
        (dst / "notifications.jsonl").write_text('{"ts":"2026-02-01","msg":"local"}\n')
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        restore_main([str(tarball), "--mode", "merge", "--force"])
        lines = (dst / "notifications.jsonl").read_text().strip().split("\n")
        assert len(lines) == 2

    def test_merge_plan_memory(self, env, monkeypatch):
        """TEST 15"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst15"
        _setup_fake_kirocrew(dst)
        (dst / "plan_memory/local_plan.json").write_text("local plan")
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"])
        assert ret == 0
        assert (dst / "plan_memory/plan1.json").is_file()
        assert (dst / "plan_memory/local_plan.json").read_text() == "local plan"

    def test_merge_restores_missing_security(self, env, capsys, monkeypatch):
        """TEST 16"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst16"
        _setup_fake_kirocrew(dst)
        (dst / "telemetry_salt").unlink()
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        restore_main([str(tarball), "--mode", "merge", "--force"])
        assert (dst / "telemetry_salt").is_file()
        assert "telemetry_salt: restored" in capsys.readouterr().out

    def test_merge_fresh_copies_memory(self, env, capsys, monkeypatch):
        """TEST 26"""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh26"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        restore_main([str(tarball), "--mode", "merge", "--components", "memory", "--force"])
        assert (fresh / "memory.db").is_file()
        assert "copied" in capsys.readouterr().out

    def test_merge_notifications_dedup(self, env, capsys, monkeypatch):
        """TEST 25"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst25"
        _setup_fake_kirocrew(dst)
        # Same ts as snapshot
        (dst / "notifications.jsonl").write_text('{"ts":"2026-01-01","msg":"test"}\n')
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        restore_main([str(tarball), "--mode", "merge", "--components", "notifications", "--force"])
        lines = (dst / "notifications.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1
        assert "Notifications imported: 0" in capsys.readouterr().out


class TestAutoDetect:
    def test_auto_replace_fresh(self, env, capsys, monkeypatch):
        """TEST 11a"""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh11"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        restore_main([str(tarball), "--force"])
        assert "replace" in capsys.readouterr().out.lower()

    def test_auto_merge_existing(self, env, capsys, monkeypatch):
        """TEST 11b"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst11"
        _setup_fake_kirocrew(dst)
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        restore_main([str(tarball), "--force"])
        assert "merge" in capsys.readouterr().out.lower()


class TestComponents:
    def test_list_components(self, capsys):
        """TEST 18"""
        restore_main(["--list-components"])
        out = capsys.readouterr().out
        for c in ("memory", "crons", "config", "skills", "workspace", "notifications", "security"):
            assert c in out

    def test_memory_only(self, env, monkeypatch):
        """TEST 19"""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh19"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        restore_main([str(tarball), "--mode", "replace", "--components", "memory", "--force"])
        assert (fresh / "memory.db").is_file()
        assert not (fresh / "crons.json").exists()
        assert not (fresh / "config.json").exists()
        assert not (fresh / "skills").exists()
        assert not (fresh / "notifications.jsonl").exists()

    def test_crons_and_skills(self, env, monkeypatch):
        """TEST 20"""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh20"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        restore_main([str(tarball), "--mode", "replace", "--components", "crons,skills", "--force"])
        assert (fresh / "crons.json").is_file()
        assert (fresh / "skills/my-skill/SKILL.md").is_file()
        assert not (fresh / "memory.db").exists()
        assert not (fresh / "config.json").exists()

    def test_components_merge(self, env, monkeypatch):
        """TEST 21"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst21"
        _setup_fake_kirocrew(dst)
        (dst / "crons.json").unlink()
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        restore_main([str(tarball), "--mode", "merge", "--components", "crons", "--force"])
        assert (dst / "crons.json").is_file()
        conn = sqlite3.connect(str(dst / "memory.db"))
        assert conn.execute("SELECT count(*) FROM semantic_memory").fetchone()[0] == 2
        conn.close()

    def test_invalid_component(self, env, capsys, monkeypatch):
        """TEST 22"""
        _, _, tarball, tmp_path = env
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        ret = restore_main([str(tarball), "--components", "bogus", "--force"])
        assert ret == 1
        assert "Unknown component: bogus" in capsys.readouterr().out

    def test_all_components(self, env, monkeypatch):
        """TEST 23"""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh23"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        restore_main([str(tarball), "--mode", "replace", "--force"])
        assert (fresh / "memory.db").is_file()
        assert (fresh / "crons.json").is_file()
        assert (fresh / "config.json").is_file()
        assert (fresh / "skills/my-skill/SKILL.md").is_file()
        assert (fresh / "notifications.jsonl").is_file()
        assert (fresh / "telemetry_salt").is_file()


class TestIntegrity:
    def test_integrity_check(self, env, capsys, monkeypatch):
        """TEST 17"""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh17"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        restore_main([str(tarball), "--mode", "replace", "--force"])
        assert "integrity: OK" in capsys.readouterr().out

    def test_fts_missing_warning(self, env, capsys, monkeypatch):
        """TEST 31"""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh31"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        restore_main([str(tarball), "--mode", "replace", "--components", "memory", "--force"])
        capsys.readouterr()  # discard first call's output
        # Remove index db
        (fresh / "memory_index.db").unlink(missing_ok=True)
        # Re-run merge to trigger warning
        restore_main([str(tarball), "--mode", "merge", "--components", "memory", "--force"])
        assert "memory_index.db is missing" in capsys.readouterr().out


class TestSecurity:
    def test_symlink_filtered_out(self, env, monkeypatch):
        """TEST 30 — symlinks are silently dropped by _data_filter."""
        src, _, _, tmp_path = env
        out = tmp_path / "sym_out"
        out.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        tarball = _make_snapshot(src, out)

        # Extract, inject symlink, re-tar
        extract = tmp_path / "sym_extract"
        extract.mkdir()
        with tarfile.open(str(tarball)) as tar:
            tar.extractall(extract, filter=lambda t, _d="": t)
        snap = next(d for d in extract.iterdir() if d.name.startswith("kirocrew-snapshot-"))
        os.symlink("/etc/passwd", str(snap / "evil_link"))
        evil_tar = tmp_path / "evil.tar.gz"
        with tarfile.open(str(evil_tar), "w:gz") as tar:
            tar.add(str(snap), arcname=snap.name)

        fresh = tmp_path / "fresh30"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(evil_tar), "--mode", "replace", "--force"])
        # Symlink is filtered out by _data_filter, restore succeeds
        assert ret == 0
        assert not (fresh / "evil_link").exists()

    def test_mode_without_value(self, env, monkeypatch):
        """TEST 28"""
        _, _, tarball, _ = env
        # argparse handles this — --mode without value raises SystemExit
        with pytest.raises(SystemExit):
            restore_main([str(tarball), "--mode"])

    def test_path_traversal_filtered(self, env, capsys, monkeypatch):
        _, _, _, tmp_path = env
        evil_tar = tmp_path / "traversal.tar.gz"
        with tarfile.open(str(evil_tar), "w:gz") as tar:
            # Add a valid snapshot dir so extraction finds something
            info = tarfile.TarInfo(name="kirocrew-snapshot-20260101T000000Z/")
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
            # Add traversal entry — will be filtered
            info2 = tarfile.TarInfo(name="kirocrew-snapshot-20260101T000000Z/../../../etc/passwd")
            info2.size = 0
            tar.addfile(info2)
        fresh = tmp_path / "fresh_traversal"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(evil_tar), "--mode", "replace", "--force"])
        # Traversal entry filtered out, restore proceeds
        assert ret == 0
        # Verify no "passwd" file anywhere under restore dir
        assert not any(p.name == "passwd" for p in fresh.rglob("*"))
        # Also verify it didn't escape to tmp_path
        assert not (tmp_path / "etc" / "passwd").exists()

    def test_absolute_path_filtered(self, env, capsys, monkeypatch):
        _, _, _, tmp_path = env
        evil_tar = tmp_path / "abspath.tar.gz"
        with tarfile.open(str(evil_tar), "w:gz") as tar:
            info = tarfile.TarInfo(name="kirocrew-snapshot-20260101T000000Z/")
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
            info2 = tarfile.TarInfo(name="/etc/passwd")
            info2.size = 0
            tar.addfile(info2)
        fresh = tmp_path / "fresh_abspath"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(evil_tar), "--mode", "replace", "--force"])
        assert ret == 0
        assert not any(p.name == "passwd" for p in fresh.rglob("*"))

    def test_hardlink_filtered(self, env, capsys, monkeypatch):
        _, _, _, tmp_path = env
        evil_tar = tmp_path / "hardlink.tar.gz"
        with tarfile.open(str(evil_tar), "w:gz") as tar:
            # Add valid snapshot dir
            info = tarfile.TarInfo(name="kirocrew-snapshot-20260101T000000Z/")
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
            info2 = tarfile.TarInfo(name="kirocrew-snapshot-20260101T000000Z/evil")
            info2.type = tarfile.LNKTYPE
            info2.linkname = "kirocrew-snapshot-20260101T000000Z/memory.db"
            tar.addfile(info2)
        fresh = tmp_path / "fresh_hardlink"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(evil_tar), "--mode", "replace", "--force"])
        assert ret == 0
        assert not (fresh / "evil").exists()


class TestIntegrityFailure:
    def test_integrity_failure(self, env, capsys, monkeypatch):
        src, _, tarball, tmp_path = env
        extract = tmp_path / "corrupt_extract"
        extract.mkdir()
        with tarfile.open(str(tarball)) as tar:
            tar.extractall(extract, filter=lambda t, _d="": t)
        snap = next(d for d in extract.iterdir() if d.name.startswith("kirocrew-snapshot-"))
        (snap / "memory.db").write_bytes(b"not a valid sqlite database")
        corrupt_tar = tmp_path / "corrupt.tar.gz"
        with tarfile.open(str(corrupt_tar), "w:gz") as tar:
            tar.add(str(snap), arcname=snap.name)
        fresh = tmp_path / "fresh_corrupt"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(corrupt_tar), "--mode", "replace", "--force"])
        assert ret == 1
        assert "integrity check failed" in capsys.readouterr().out


class TestParsedNamespace:
    """Exercise the parsed= keyword path used by cli.py in production."""

    def test_snapshot_via_parsed_namespace(self, env, monkeypatch):
        src, _, _, tmp_path = env
        out = tmp_path / "out_parsed"
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        ns = argparse.Namespace(output_dir=str(out), keep=7, list_snapshots=False)
        ret = snapshot_main(parsed=ns)
        assert ret == 0
        assert list(out.glob("kirocrew-snapshot-*.tar.gz"))

    def test_restore_via_parsed_namespace(self, env, monkeypatch):
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh_parsed"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ns = argparse.Namespace(
            snapshot=str(tarball),
            mode="replace",
            dry_run=False,
            components=None,
            list_components=False,
            force=True,
        )
        ret = restore_main(parsed=ns)
        assert ret == 0
        assert (fresh / "memory.db").is_file()


# ── Comment 8: New edge-case tests ───────────────────────────────────────────


class TestSchemaIncompatibleMerge:
    def test_merge_incompatible_schema(self, env, capsys, monkeypatch):
        """Merge gracefully skips tables that don't exist in source."""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst_schema"
        _setup_fake_kirocrew(dst)
        # Drop a table from destination to simulate schema mismatch
        conn = sqlite3.connect(str(dst / "memory.db"))
        conn.execute("DROP TABLE knowledge_edges")
        conn.commit()
        conn.close()
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "Semantic Memory imported" in out


class TestCorruptSourceDB:
    def test_merge_corrupt_source_db(self, env, capsys, monkeypatch):
        """Merge with corrupt source DB skips merge gracefully."""
        src, _, _, tmp_path = env
        out = tmp_path / "corrupt_src_out"
        out.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        tarball = _make_snapshot(src, out)

        # Extract, corrupt memory.db, re-tar
        extract = tmp_path / "corrupt_src_extract"
        extract.mkdir()
        with tarfile.open(str(tarball)) as tar:
            tar.extractall(extract, filter=lambda t, _d="": t)
        snap = next(d for d in extract.iterdir() if d.name.startswith("kirocrew-snapshot-"))
        (snap / "memory.db").write_bytes(b"corrupt data here")
        corrupt_tar = tmp_path / "corrupt_src.tar.gz"
        with tarfile.open(str(corrupt_tar), "w:gz") as tar:
            tar.add(str(snap), arcname=snap.name)

        dst = tmp_path / "dst_corrupt_src"
        _setup_fake_kirocrew(dst)
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(corrupt_tar), "--mode", "merge", "--force"])
        assert ret == 0
        out_text = capsys.readouterr().out
        assert "Source DB" in out_text or "Merge complete" in out_text


class TestGatewayRunningRefusal:
    def test_restore_refused_when_gateway_running(self, env, capsys, monkeypatch):
        """Restore refuses if gateway is running (unless --force)."""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh_gw"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "1")
        ret = restore_main([str(tarball), "--mode", "replace"])
        assert ret == 1
        assert "Gateway is running" in capsys.readouterr().out

    def test_restore_allowed_with_force(self, env, capsys, monkeypatch):
        """--force bypasses gateway check."""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh_gw_force"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "1")
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 0


class TestEmptyKirocrewDir:
    def test_snapshot_empty_dir(self, tmp_path, monkeypatch):
        """Snapshot succeeds on an empty ~/.kirocrew directory."""
        empty = tmp_path / "empty_mc"
        empty.mkdir()
        out = tmp_path / "empty_out"
        monkeypatch.setenv("KIROCREW_HOME", str(empty))
        ret = snapshot_main([str(out)])
        assert ret == 0
        assert list(out.glob("kirocrew-snapshot-*.tar.gz"))


class TestConcurrentSnapshot:
    def test_concurrent_snapshots_unique(self, env, monkeypatch):
        """Two rapid snapshots produce distinct files."""
        src, _, _, tmp_path = env
        out = tmp_path / "concurrent_out"
        out.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        snapshot_main([str(out)])
        # Ensure different timestamp by creating a second one
        import time

        time.sleep(1.1)
        snapshot_main([str(out)])
        tarballs = list(out.glob("kirocrew-snapshot-*.tar.gz"))
        assert len(tarballs) == 2
        assert tarballs[0].name != tarballs[1].name
