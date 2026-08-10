"""Tests for kiro_crew.snapshot — snapshot and restore."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tarfile
from pathlib import Path

import pytest

from kiro_crew.gateway_lock import GatewayLock, GatewayLockError
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
    conn.executescript("""
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
    """)
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
        m = json.loads((snap / "MANIFEST.json").read_text(encoding="utf-8"))
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
        before = len(json.loads((dst / "crons.json").read_text(encoding="utf-8"))["jobs"])
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"])
        assert ret == 0
        after = len(json.loads((dst / "crons.json").read_text(encoding="utf-8"))["jobs"])
        assert before == after

    def test_merge_new_cron(self, env, monkeypatch):
        """TEST 9"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst9"
        _setup_fake_kirocrew(dst)
        d = json.loads((dst / "crons.json").read_text(encoding="utf-8"))
        d["jobs"][0]["name"] = "different-job"
        (dst / "crons.json").write_text(json.dumps(d))
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        restore_main([str(tarball), "--mode", "merge", "--force"])
        count = len(json.loads((dst / "crons.json").read_text(encoding="utf-8"))["jobs"])
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
        assert (dst / "workspace/doc.md").read_text(encoding="utf-8") == "local version"

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
        lines = (dst / "notifications.jsonl").read_text(encoding="utf-8").strip().split("\n")
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
        assert (dst / "plan_memory/local_plan.json").read_text(encoding="utf-8") == "local plan"

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
        lines = (dst / "notifications.jsonl").read_text(encoding="utf-8").strip().split("\n")
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
    def test_data_filter_drops_sel_hmac_key_at_trust_path(self):
        """The SEL key moved to trust/sel_hmac.key; NEVER_SNAPSHOT_FILES is
        matched by BASENAME so the key must be dropped from a bundle at BOTH
        the new and the legacy location."""
        from kiro_crew.snapshot import _data_filter

        legacy = tarfile.TarInfo(name="snap/sel_hmac.key")
        assert _data_filter(legacy) is None
        new = tarfile.TarInfo(name="snap/trust/sel_hmac.key")
        assert _data_filter(new) is None
        # An unrelated file in a trust/ dir is NOT dropped (basename match only).
        other = tarfile.TarInfo(name="snap/trust/notes.txt")
        assert _data_filter(other) is not None

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


# ── Sessions component ────────────────────────────────────────────────────────


def _meta_line(memory_mode: str) -> str:
    return (
        json.dumps({"_type": "metadata", "created_at": "2026-01-01", "memory_mode": memory_mode})
        + "\n"
    )


def _msg_line(content: str) -> str:
    return json.dumps({"role": "user", "content": content, "ts": "2026-01-01"}) + "\n"


def _add_fake_sessions(d: Path) -> None:
    """Populate a fake sessions/ tree: transcripts, archives, locks, workspaces."""
    s = d / "sessions"
    (s / "archive").mkdir(parents=True, exist_ok=True)
    # Persistent dashboard transcript + its archive segment + workspace dir
    (s / "dashboard_chat-1-111.jsonl").write_text(_meta_line("persistent") + _msg_line("hello"))
    (s / "dashboard_chat-1-111.jsonl.lock").write_text("")
    (s / "archive" / "dashboard_chat-1-111__20260101T000000Z.jsonl").write_text(
        _msg_line("older rotated line")
    )
    (s / "chat-1-111").mkdir(exist_ok=True)
    (s / "chat-1-111" / "agent-abc.md").write_text("subagent result")
    # Incognito dashboard transcript + its archive segment + workspace dir
    (s / "dashboard_chat-2-222.jsonl").write_text(_meta_line("incognito") + _msg_line("private"))
    (s / "archive" / "dashboard_chat-2-222__20260101T000000Z.jsonl").write_text(
        _msg_line("private rotated")
    )
    (s / "chat-2-222").mkdir(exist_ok=True)
    (s / "chat-2-222" / "agent-def.md").write_text("private subagent result")
    # Temporary channel transcript (sanitized slack key stem)
    (s / "slack_123.456.jsonl").write_text(_meta_line("temporary") + _msg_line("guest"))
    # Atomic-write staging file caught mid-replace: carries a full transcript
    # body (here an incognito one) under an unclassifiable mkstemp name.
    (s / "tmpa1b2c3.tmp").write_text(_meta_line("incognito") + _msg_line("mid-write private"))


@pytest.fixture
def sessions_env(tmp_path, monkeypatch):
    """Fake data home with a populated sessions/ tree, snapshotted."""
    src = tmp_path / "src"
    out = tmp_path / "out"
    _setup_fake_kirocrew(src)
    _add_fake_sessions(src)
    monkeypatch.setenv("KIROCREW_HOME", str(src))
    # Keep the kiro-cli session store hermetic (session-map reconciliation
    # probes it for native session files).
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home"))
    tarball = _make_snapshot(src, out)
    return src, out, tarball, tmp_path


def _extract(tarball: Path, dest: Path) -> Path:
    dest.mkdir(exist_ok=True)
    with tarfile.open(str(tarball)) as tar:
        tar.extractall(dest, filter=lambda t, _d="": t)
    return next(d for d in dest.iterdir() if d.name.startswith("kirocrew-snapshot-"))


class TestSessionsStaging:
    def test_persistent_sessions_staged(self, sessions_env, tmp_path):
        _, _, tarball, _ = sessions_env
        snap = _extract(tarball, tmp_path / "extract_sess")
        assert (snap / "sessions/dashboard_chat-1-111.jsonl").is_file()
        assert (snap / "sessions/archive/dashboard_chat-1-111__20260101T000000Z.jsonl").is_file()
        assert (snap / "sessions/chat-1-111/agent-abc.md").is_file()

    def test_incognito_and_temporary_excluded(self, sessions_env, tmp_path):
        _, _, tarball, _ = sessions_env
        snap = _extract(tarball, tmp_path / "extract_priv")
        # Incognito transcript, its archive segment, and its workspace dir
        assert not (snap / "sessions/dashboard_chat-2-222.jsonl").exists()
        assert not (snap / "sessions/archive/dashboard_chat-2-222__20260101T000000Z.jsonl").exists()
        assert not (snap / "sessions/chat-2-222").exists()
        # Temporary channel transcript
        assert not (snap / "sessions/slack_123.456.jsonl").exists()

    def test_lock_files_excluded(self, sessions_env, tmp_path):
        _, _, tarball, _ = sessions_env
        snap = _extract(tarball, tmp_path / "extract_lock")
        assert not any(p.name.endswith(".lock") for p in (snap / "sessions").rglob("*"))

    def test_atomic_write_tmp_files_excluded(self, sessions_env, tmp_path):
        """A transcript-bearing mkstemp ``.tmp`` caught mid-replace must not ride."""
        _, _, tarball, _ = sessions_env
        snap = _extract(tarball, tmp_path / "extract_tmp")
        assert not any(p.name.endswith(".tmp") for p in (snap / "sessions").rglob("*"))

    def test_session_vanishing_mid_copy_does_not_abort_snapshot(self, tmp_path, monkeypatch):
        """A session deleted while the copy walks the tree must be skipped,
        not abort the whole snapshot."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_vanish"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        doomed = src / "sessions" / "dashboard_chat-1-111.jsonl"

        real_copy = snapshot_mod._pinned_copy_file
        state = {"deleted": False}

        def _racy_copy(s, d, **kw):
            # Delete the transcript just before ITS OWN copy — models a live
            # dashboard deletion landing between listdir and the copy.
            if not state["deleted"] and str(s).endswith("dashboard_chat-1-111.jsonl"):
                state["deleted"] = True
                doomed.unlink()
            real_copy(s, d, **kw)

        monkeypatch.setattr(snapshot_mod, "_pinned_copy_file", _racy_copy)
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_vanish"))
        out = tmp_path / "out_vanish"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_vanish")
        # Snapshot completed; the vanished transcript is simply absent.
        assert (snap / "sessions").is_dir()
        assert not (snap / "sessions" / "dashboard_chat-1-111.jsonl").exists()

    def test_non_vanish_copy_error_still_aborts_sessions_staging(self, tmp_path, monkeypatch):
        """Vanish tolerance must not swallow real copy failures (EACCES)."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_eacces_sess"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)

        def _denied_copy(s, d, **kw):
            raise PermissionError(f"copy denied: {s}")

        monkeypatch.setattr(snapshot_mod, "_pinned_copy_file", _denied_copy)
        # The pinned walk propagates the error directly; the by-name fallback
        # (non-dir_fd platforms) aggregates it into shutil.Error.
        with pytest.raises((shutil.Error, PermissionError)):
            snapshot_mod._stage_sessions(src, tmp_path / "stage_eacces_sess")

    def test_hardlinked_file_in_session_workspace_not_staged(self, tmp_path, monkeypatch, capsys):
        """A hardlink to a credential planted inside a staged session workspace
        must not have its bytes copied into the snapshot — the copy is pinned
        to a descriptor whose fstat must show a regular file with nlink == 1."""
        src = tmp_path / "src_hardlink_sess"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        secret = tmp_path / "aws_credentials"
        secret.write_text("[default]\naws_secret_access_key = HUNTER2\n")
        planted = src / "sessions" / "chat-1-111" / "creds"
        os.link(str(secret), str(planted))

        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_hardlink"))
        out = tmp_path / "out_hardlink"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_hardlink")
        # Snapshot completed; the sibling regular file rode, the hardlink did not.
        assert (snap / "sessions" / "chat-1-111" / "agent-abc.md").is_file()
        assert not (snap / "sessions" / "chat-1-111" / "creds").exists()
        assert "Skipping hardlinked or non-regular file" in capsys.readouterr().out

    def test_orphaned_archive_segment_excluded(self, tmp_path, monkeypatch):
        """An archive segment with no live transcript cannot be classified — not staged."""
        src = tmp_path / "src_orphan"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        (src / "sessions/archive/gone-session__20260101T000000Z.jsonl").write_text(
            _msg_line("segment of a deleted session")
        )
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        out = tmp_path / "out_orphan"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_orphan")
        assert not (snap / "sessions/archive/gone-session__20260101T000000Z.jsonl").exists()
        assert (snap / "sessions/archive/dashboard_chat-1-111__20260101T000000Z.jsonl").is_file()

    def test_workspace_shared_with_restricted_session_excluded(self, tmp_path, monkeypatch):
        """A workspace dir whose name maps to BOTH a restricted channel session
        and a staged dashboard session must not ride — privacy wins over the
        staged sibling (the OR-authorization leak)."""
        src = tmp_path / "src_ambig"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        s = src / "sessions"
        # Incognito CHANNEL session: slack:999.888 -> stem slack_999.888
        (s / "slack_999.888.jsonl").write_text(_meta_line("incognito") + _msg_line("private"))
        # Persistent DASHBOARD session whose stem is dashboard_slack_999.888
        (s / "dashboard_slack_999.888.jsonl").write_text(
            _meta_line("persistent") + _msg_line("public")
        )
        # The shared sanitized workspace dir name both sessions map to
        (s / "slack_999.888").mkdir()
        (s / "slack_999.888" / "agent-xyz.md").write_text("possibly-private subagent result")
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        out = tmp_path / "out_ambig"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_ambig")
        # The persistent dashboard transcript rides; the incognito one and the
        # ambiguous workspace dir do not.
        assert (snap / "sessions/dashboard_slack_999.888.jsonl").is_file()
        assert not (snap / "sessions/slack_999.888.jsonl").exists()
        assert not (snap / "sessions/slack_999.888").exists()

    def test_ancestor_dir_swapped_for_link_mid_walk_not_followed(self, tmp_path, monkeypatch):
        """An allowlisted ancestor DIRECTORY swapped for a credential-directory
        link between the listing screen and the descend must not be followed.

        The final-component O_NOFOLLOW in ``_pinned_copy_file`` alone would
        miss this: files inside the swapped-in tree are plain regular files.
        The staging walk opens every directory component O_NOFOLLOW relative
        to its parent's pinned descriptor, so the swap is refused at the
        component that changed."""
        from kiro_crew import snapshot as snapshot_mod

        if not snapshot_mod._STAGE_DIR_FD_OK:
            pytest.skip("dir_fd traversal unsupported on this platform")
        src = tmp_path / "src_swap"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        secret_dir = tmp_path / "aws"
        secret_dir.mkdir()
        (secret_dir / "credentials").write_text("aws_secret_access_key = HUNTER2\n")
        workspace = src / "sessions" / "chat-1-111"

        real_pinned = snapshot_mod._stage_tree_pinned
        real_safe = snapshot_mod._copytree_safe
        state = {"swapped": False}

        def _swap_after_root_screen(inner):
            # Fires inside the ignore callback for the sessions ROOT: the
            # listing-time link screens have already run against the real
            # directory, and no entry has been processed yet — the exact
            # check-to-use window the finding describes.
            def wrapped(directory, contents):
                result = set(inner(directory, contents)) if inner else set()
                if not state["swapped"] and Path(directory) == src / "sessions":
                    state["swapped"] = True
                    shutil.rmtree(workspace)
                    os.symlink(str(secret_dir), str(workspace))
                return result

            return wrapped

        def _pinned_hooked(s, d, ignore=None):
            real_pinned(s, d, _swap_after_root_screen(ignore))

        def _safe_hooked(s, d, **kw):
            kw["ignore"] = _swap_after_root_screen(kw.get("ignore"))
            real_safe(s, d, **kw)

        monkeypatch.setattr(snapshot_mod, "_stage_tree_pinned", _pinned_hooked)
        monkeypatch.setattr(snapshot_mod, "_copytree_safe", _safe_hooked)
        stage = tmp_path / "stage_swap"
        snapshot_mod._stage_sessions(src, stage)
        assert state["swapped"], "swap hook never fired — test wiring broke"
        staged = stage / "sessions"
        # The swapped component was refused, not traversed: no credential
        # bytes anywhere in the stage.
        assert not (staged / "chat-1-111" / "credentials").exists()
        staged_bodies = "".join(p.read_text() for p in staged.rglob("*") if p.is_file())
        assert "HUNTER2" not in staged_bodies

    def test_manifest_counts_sessions(self, sessions_env, tmp_path):
        _, _, tarball, _ = sessions_env
        snap = _extract(tarball, tmp_path / "extract_manifest")
        m = json.loads((snap / "MANIFEST.json").read_text(encoding="utf-8"))
        assert m["contents"]["session_transcripts"] == 1
        assert m["contents"]["session_files"] >= 3  # transcript + archive + workspace file


class TestSessionsRestore:
    def test_restore_fresh(self, sessions_env, tmp_path, monkeypatch):
        _, _, tarball, _ = sessions_env
        fresh = tmp_path / "fresh_sess"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 0
        assert (fresh / "sessions/dashboard_chat-1-111.jsonl").is_file()
        assert (fresh / "sessions/chat-1-111/agent-abc.md").is_file()

    def test_restore_never_overwrites_local_transcript(self, sessions_env, tmp_path, monkeypatch):
        _, _, tarball, _ = sessions_env
        dst = tmp_path / "dst_sess"
        _setup_fake_kirocrew(dst)
        (dst / "sessions").mkdir()
        (dst / "sessions/dashboard_chat-1-111.jsonl").write_text("local transcript content")
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        for mode in ("replace", "merge"):
            ret = restore_main([str(tarball), "--mode", mode, "--force"])
            assert ret == 0
            local = (dst / "sessions/dashboard_chat-1-111.jsonl").read_text(encoding="utf-8")
            assert local == "local transcript content"
        # Files missing locally are still copied in
        assert (dst / "sessions/chat-1-111/agent-abc.md").is_file()

    def test_component_selection_excludes_sessions(self, sessions_env, tmp_path, monkeypatch):
        _, _, tarball, _ = sessions_env
        fresh = tmp_path / "fresh_sel"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        restore_main([str(tarball), "--mode", "replace", "--components", "memory", "--force"])
        assert not (fresh / "sessions").exists()

    def test_sessions_component_only(self, sessions_env, tmp_path, monkeypatch):
        _, _, tarball, _ = sessions_env
        fresh = tmp_path / "fresh_only"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        restore_main([str(tarball), "--mode", "replace", "--components", "sessions", "--force"])
        assert (fresh / "sessions/dashboard_chat-1-111.jsonl").is_file()
        assert not (fresh / "memory.db").exists()

    def test_old_snapshot_without_sessions_restores_unchanged(self, env, capsys, monkeypatch):
        """A snapshot with no sessions/ (pre-sessions layout) restores as before."""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh_old"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 0
        assert (fresh / "memory.db").is_file()
        assert not (fresh / "sessions").exists()

    def test_old_snapshot_explicit_sessions_component_notes_absence(self, env, capsys, monkeypatch):
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh_old_note"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main(
            [str(tarball), "--mode", "replace", "--components", "sessions", "--force"]
        )
        assert ret == 0
        assert "not present in snapshot" in capsys.readouterr().out


class TestSessionMapReconciliation:
    def test_dangling_entries_dropped(self, tmp_path, monkeypatch):
        src = tmp_path / "src_map"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        (src / "session_map.json").write_text(
            json.dumps(
                {
                    # Transcript staged in the snapshot → kept, unusable sid cleared
                    "dashboard:chat-1-111": {"sid": "aaaa-1111"},
                    # No transcript anywhere → dropped
                    "dashboard:chat-9-999": {"sid": "bbbb-9999"},
                    # Incognito transcript is excluded from the snapshot → dropped
                    "dashboard:chat-2-222": {"sid": "cccc-2222"},
                    # No sid (linkage-only) → kept
                    "slack:777.888": {"sid": None, "slack_thread_ts": "777.888"},
                    # Externally-stored provider → kept
                    "dashboard:chat-8-888": {"sid": "dddd-8888", "provider": "claude_code"},
                    # Dangling sid BUT durable privacy flag → kept, sid cleared
                    "slack:333.444": {"sid": "eeee-3333", "flags": {"incognito": True}},
                }
            )
        )
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_map"))
        out = tmp_path / "out_map"
        tarball = _make_snapshot(src, out)

        fresh = tmp_path / "fresh_map"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 0
        restored = json.loads((fresh / "session_map.json").read_text(encoding="utf-8"))
        assert set(restored) == {
            "dashboard:chat-1-111",
            "slack:777.888",
            "dashboard:chat-8-888",
            "slack:333.444",
        }
        # The flagged entry survives with its privacy state intact and the
        # unusable sid cleared.
        assert restored["slack:333.444"]["flags"] == {"incognito": True}
        assert restored["slack:333.444"]["sid"] == ""
        # The transcript-backed entry survives, but its sid is unusable on
        # this host and is cleared — a stale sid left in place would get the
        # whole entry deleted by SessionMap.prune() at first gateway start.
        assert restored["dashboard:chat-1-111"]["sid"] == ""

    def test_transcript_backed_unusable_sid_cleared_not_kept_stale(self, tmp_path, monkeypatch):
        """A transcript-backed entry never keeps a sid that is unusable here.

        SessionMap.prune() deletes ANY entry whose sid lacks a local kiro-cli
        session file — privacy flags and thread linkage included. Keeping the
        stale sid would therefore lose the durable state on the first gateway
        start; clearing just the sid keeps the entry prune-safe.
        """
        src = tmp_path / "src_stale_sid"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        (src / "session_map.json").write_text(
            json.dumps(
                {
                    # Transcript staged + durable flags, sid unusable → sid
                    # cleared, flags kept.
                    "dashboard:chat-1-111": {
                        "sid": "gggg-1111",
                        "flags": {"incognito": False},
                        "slack_thread_ts": "111.222",
                    },
                }
            )
        )
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_stale_sid"))
        out = tmp_path / "out_stale_sid"
        tarball = _make_snapshot(src, out)

        fresh = tmp_path / "fresh_stale_sid"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 0
        restored = json.loads((fresh / "session_map.json").read_text(encoding="utf-8"))
        entry = restored["dashboard:chat-1-111"]
        assert entry["sid"] == ""
        assert entry["flags"] == {"incognito": False}
        assert entry["slack_thread_ts"] == "111.222"

    def test_unsafe_sid_shape_cleared_on_transcript_backed_entry(self, tmp_path, monkeypatch):
        """A sid that fails the safe-shape check is unusable and gets cleared."""
        src = tmp_path / "src_unsafe_sid"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        (src / "session_map.json").write_text(
            json.dumps({"dashboard:chat-1-111": {"sid": "../evil/../../sid"}})
        )
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_unsafe_sid"))
        out = tmp_path / "out_unsafe_sid"
        tarball = _make_snapshot(src, out)

        fresh = tmp_path / "fresh_unsafe_sid"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 0
        restored = json.loads((fresh / "session_map.json").read_text(encoding="utf-8"))
        assert restored["dashboard:chat-1-111"]["sid"] == ""

    def test_entry_kept_when_cli_session_exists(self, tmp_path, monkeypatch):
        src = tmp_path / "src_cli"
        _setup_fake_kirocrew(src)
        (src / "session_map.json").write_text(
            json.dumps({"dashboard:chat-9-999": {"sid": "eeee-9999"}})
        )
        kiro_home = tmp_path / "kiro_home_cli"
        (kiro_home / "sessions" / "cli").mkdir(parents=True)
        (kiro_home / "sessions" / "cli" / "eeee-9999.json").write_text("{}")
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(kiro_home))
        out = tmp_path / "out_cli"
        tarball = _make_snapshot(src, out)

        fresh = tmp_path / "fresh_cli"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 0
        restored = json.loads((fresh / "session_map.json").read_text(encoding="utf-8"))
        assert "dashboard:chat-9-999" in restored

    def test_merge_with_local_map_untouched(self, sessions_env, tmp_path, monkeypatch):
        """Merge mode never reconciles a pre-existing local map."""
        _, _, tarball, _ = sessions_env
        dst = tmp_path / "dst_local_map"
        _setup_fake_kirocrew(dst)
        local_map = {"dashboard:chat-77-777": {"sid": "ffff-7777"}}
        (dst / "session_map.json").write_text(json.dumps(local_map))
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"])
        assert ret == 0
        after = json.loads((dst / "session_map.json").read_text(encoding="utf-8"))
        assert after == local_map

    def test_overlong_map_key_does_not_crash_reconciliation(self, tmp_path, monkeypatch):
        """A session-map key of filesystem-component length raises
        ENAMETOOLONG from the transcript probe — one corrupt entry must not
        crash reconciliation and leave a partially applied restore."""
        src = tmp_path / "src_longkey"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        (src / "session_map.json").write_text(
            json.dumps({"dashboard:" + "x" * 4096: {"sid": "aaaa-1111"}})
        )
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_longkey"))
        out = tmp_path / "out_longkey"
        tarball = _make_snapshot(src, out)

        fresh = tmp_path / "fresh_longkey"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 0
        # Reconciliation completed: the map exists and the entry was treated
        # as transcript-absent (dropped/cleared), not crashed on.
        assert (fresh / "session_map.json").is_file()

    def test_config_only_restore_never_reconciles(self, tmp_path, monkeypatch):
        """--components config restores the map but not transcripts — the map
        must survive untouched rather than being reconciled against the
        deliberately-absent sessions."""
        src = tmp_path / "src_config_only"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        (src / "session_map.json").write_text(
            json.dumps({"dashboard:chat-1-111": {"sid": "aaaa-1111"}})
        )
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_config_only"))
        out = tmp_path / "out_config_only"
        tarball = _make_snapshot(src, out)

        fresh = tmp_path / "fresh_config_only"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(tarball), "--mode", "replace", "--components", "config", "--force"])
        assert ret == 0
        restored = json.loads((fresh / "session_map.json").read_text(encoding="utf-8"))
        # The entry's transcript was NOT restored (sessions excluded) and no
        # kiro-cli file exists — reconciliation would have dropped it.
        assert "dashboard:chat-1-111" in restored
        assert not (fresh / "sessions").exists()

    def test_pre_sessions_snapshot_map_not_reconciled(self, tmp_path, monkeypatch):
        """A snapshot without a sessions/ tree never triggers map reconciliation.

        A pre-sessions snapshot ships session_map.json but no transcripts;
        reconciling against that would delete every restored mapping.
        """
        src = tmp_path / "src_pre"
        _setup_fake_kirocrew(src)
        (src / "session_map.json").write_text(
            json.dumps({"dashboard:chat-9-999": {"sid": "aaaa-9999"}})
        )
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_pre"))
        out = tmp_path / "out_pre"
        tarball = _make_snapshot(src, out)
        # The fixture has no sessions/ tree, so the tarball matches the
        # pre-sessions layout as-is: a session map with no transcripts.
        snap = _extract(tarball, tmp_path / "extract_pre")
        assert not (snap / "sessions").exists()

        fresh = tmp_path / "fresh_pre"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 0
        restored = json.loads((fresh / "session_map.json").read_text(encoding="utf-8"))
        # Entry survives despite no transcript existing anywhere locally.
        assert "dashboard:chat-9-999" in restored

    def test_reconciliation_deferred_while_gateway_holds_lock(self, tmp_path, monkeypatch, capsys):
        """A --force restore under a live gateway never rewrites the map.

        The race being pinned: the gateway writes a new mapping after
        reconciliation reads the map, and reconciliation's stale whole-map
        write deletes it. The pass is gated on ``gateway.lock``, so while a
        live gateway holds it the map is left alone — the gateway's next
        in-memory save overwrites the restored file regardless, so skipping
        loses nothing the save would not already discard.
        """
        src = tmp_path / "src_gwlock"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        (src / "session_map.json").write_text(
            # No transcript, no durable state → reconciliation would drop it.
            json.dumps({"dashboard:chat-9-999": {"sid": "bbbb-9999"}})
        )
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_gwlock"))
        out = tmp_path / "out_gwlock"
        tarball = _make_snapshot(src, out)

        fresh = tmp_path / "fresh_gwlock"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        with GatewayLock(fresh):  # simulate the live gateway owning this home
            ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 0
        restored = json.loads((fresh / "session_map.json").read_text(encoding="utf-8"))
        # Reconciliation would have dropped this entry; the gated pass left
        # the map untouched instead of racing the (simulated) live gateway.
        assert "dashboard:chat-9-999" in restored
        assert "reconciliation skipped" in capsys.readouterr().out

    def test_reconciliation_pass_holds_gateway_lock(self, tmp_path, monkeypatch):
        """The map read-modify-write runs entirely under ``gateway.lock``.

        Probes at the write step: a second acquire of the same home's lock
        must be refused while the pass is writing, proving a concurrent
        gateway map write could not have interleaved with the pass.
        """
        import kiro_crew.snapshot as snapshot_mod

        mc = tmp_path / "mc_lock_probe"
        (mc / "sessions").mkdir(parents=True)
        (mc / "session_map.json").write_text(
            json.dumps({"dashboard:chat-9-999": {"sid": "bbbb-9999"}})
        )
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_probe"))

        held_at_write: dict[str, bool] = {}
        real_atomic_write = snapshot_mod.atomic_write

        def probe(path, content):
            try:
                GatewayLock(mc).acquire().release()
                held_at_write["value"] = False
            except GatewayLockError:
                held_at_write["value"] = True
            real_atomic_write(path, content)

        monkeypatch.setattr(snapshot_mod, "atomic_write", probe)
        snapshot_mod._reconcile_session_map(mc)

        # The write landed, and it happened while the pass held the lock.
        assert held_at_write == {"value": True}
        restored = json.loads((mc / "session_map.json").read_text(encoding="utf-8"))
        assert "dashboard:chat-9-999" not in restored


class TestPrivacyMarkerContract:
    """Locks the shared incognito-marker contract between history and snapshot.

    ``_restricted_session_stems`` reads the same metadata head-line marker that
    ``history.recent_from_source`` uses to skip private sessions. This test
    goes through the REAL producer (``ConversationLog.update_metadata``) so a
    change to how history persists ``memory_mode`` breaks here loudly instead
    of snapshots silently shipping incognito transcripts off-host.
    """

    def test_real_writer_transcript_classified_restricted(self, tmp_path, monkeypatch):
        from kiro_crew.history import INCOGNITO_MEMORY_MODES, ConversationLog

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        sessions = tmp_path / "home" / "sessions"
        log = ConversationLog(base_dir=sessions)
        log.append("dashboard:chat-3-333", "user", "hello")
        log.append("dashboard:chat-4-444", "user", "hello private")
        log.update_metadata("dashboard:chat-4-444", {"memory_mode": "incognito"})

        from kiro_crew.snapshot import _restricted_session_stems, _transcript_candidates

        restricted = _restricted_session_stems(
            _transcript_candidates(sessions), INCOGNITO_MEMORY_MODES
        )
        assert "dashboard_chat-4-444" in restricted
        assert "dashboard_chat-3-333" not in restricted

    def test_unclassifiable_transcripts_fail_closed(self, tmp_path, monkeypatch):
        """Malformed heads and unrecognized memory_mode values never ride.

        Classification is positive: only a metadata record whose memory_mode
        is absent (the store's persistent default) or "persistent" stages the
        transcript. Everything else — an unknown mode, a head with no metadata
        record — is unclassifiable and stays out of the snapshot.
        """
        src = tmp_path / "src_failclosed"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        s = src / "sessions"
        # Unrecognized mode string.
        (s / "dashboard_chat-6-666.jsonl").write_text(_meta_line("shadow") + _msg_line("x"))
        # No metadata record anywhere in the head.
        (s / "dashboard_chat-7-777.jsonl").write_text(_msg_line("no metadata at all"))
        # Absent memory_mode field == persistent (legacy transcripts).
        legacy_meta = json.dumps({"_type": "metadata", "created_at": "2026-01-01"}) + "\n"
        (s / "dashboard_chat-8-888.jsonl").write_text(legacy_meta + _msg_line("legacy ok"))
        # Present-but-malformed falsy values must NOT collapse to persistent.
        null_meta = json.dumps({"_type": "metadata", "memory_mode": None}) + "\n"
        (s / "dashboard_chat-10-010.jsonl").write_text(null_meta + _msg_line("null mode"))
        empty_meta = json.dumps({"_type": "metadata", "memory_mode": ""}) + "\n"
        (s / "dashboard_chat-11-011.jsonl").write_text(empty_meta + _msg_line("empty mode"))
        list_meta = json.dumps({"_type": "metadata", "memory_mode": []}) + "\n"
        (s / "dashboard_chat-12-012.jsonl").write_text(list_meta + _msg_line("list mode"))
        # Undecodable head: cannot classify, fails closed.
        (s / "dashboard_chat-13-013.jsonl").write_bytes(b"\xff\xfe garbage \xff" + b"\n")
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_failclosed"))
        out = tmp_path / "out_failclosed"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_failclosed")
        assert not (snap / "sessions" / "dashboard_chat-6-666.jsonl").exists()
        assert not (snap / "sessions" / "dashboard_chat-7-777.jsonl").exists()
        assert (snap / "sessions" / "dashboard_chat-8-888.jsonl").is_file()
        assert not (snap / "sessions" / "dashboard_chat-10-010.jsonl").exists()
        assert not (snap / "sessions" / "dashboard_chat-11-011.jsonl").exists()
        assert not (snap / "sessions" / "dashboard_chat-12-012.jsonl").exists()
        assert not (snap / "sessions" / "dashboard_chat-13-013.jsonl").exists()

    def test_session_map_flagged_session_excluded(self, tmp_path, monkeypatch):
        """A session flagged incognito/temporary in session_map.json never rides.

        Channel sessions (Slack ``!incognito``) persist privacy as a flag on
        the map entry, not in the transcript's metadata head — the head-based
        classifier alone would export them.
        """
        src = tmp_path / "src_map_flag"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        s = src / "sessions"
        # Transcript head says persistent; the map flag says incognito.
        (s / "slack_555.777.jsonl").write_text(_meta_line("persistent") + _msg_line("flagged"))
        (s / "archive" / "slack_555.777__20260101T000000Z.jsonl").write_text(
            _msg_line("flagged rotated")
        )
        (src / "session_map.json").write_text(
            json.dumps(
                {
                    "slack:555.777": {"sid": "ffff-5555", "flags": {"incognito": True}},
                    "dashboard:chat-1-111": {"sid": "aaaa-1111"},
                }
            )
        )
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_map_flag"))
        out = tmp_path / "out_map_flag"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_map_flag")
        assert not (snap / "sessions" / "slack_555.777.jsonl").exists()
        assert not (
            snap / "sessions" / "archive" / "slack_555.777__20260101T000000Z.jsonl"
        ).exists()
        # Unflagged sessions still ride.
        assert (snap / "sessions" / "dashboard_chat-1-111.jsonl").is_file()

    def test_flag_arriving_after_sweep_enforced_at_archive_time(self, tmp_path, monkeypatch):
        """A ``!incognito`` landing between the post-copy sweep and tar.add
        still excludes the session: the tar filter consults a fresh flag-map
        read per entry, so the privacy verdict is atomic with archive
        creation rather than fixed at staging time."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_late_flag"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        (src / "session_map.json").write_text(
            json.dumps({"dashboard:chat-1-111": {"sid": "aaaa-1111"}})
        )

        real_stage = snapshot_mod._stage_sessions

        def _stage_then_flag(mc, stage):
            real_stage(mc, stage)
            # The flag lands AFTER staging and the post-copy sweep have both
            # completed, BEFORE the tarball is written.
            (src / "session_map.json").write_text(
                json.dumps(
                    {
                        "dashboard:chat-1-111": {
                            "sid": "aaaa-1111",
                            "flags": {"incognito": True},
                        }
                    }
                )
            )

        monkeypatch.setattr(snapshot_mod, "_stage_sessions", _stage_then_flag)
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_late_flag"))
        out = tmp_path / "out_late_flag"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_late_flag")
        # The freshly flagged session was dropped at archive time: transcript,
        # archive segment, and workspace dir all excluded.
        assert not (snap / "sessions" / "dashboard_chat-1-111.jsonl").exists()
        assert not (
            snap / "sessions" / "archive" / "dashboard_chat-1-111__20260101T000000Z.jsonl"
        ).exists()
        assert not (snap / "sessions" / "chat-1-111").exists()
        # Unrelated snapshot content still rides.
        assert (snap / "MANIFEST.json").is_file()

    def test_unreadable_session_map_fails_closed(self, tmp_path, monkeypatch):
        """An unparseable session_map.json stages no sessions at all."""
        src = tmp_path / "src_bad_map"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        (src / "session_map.json").write_text("{not json")
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_bad_map"))
        out = tmp_path / "out_bad_map"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_bad_map")
        assert not any((snap / "sessions").rglob("*.jsonl"))

    def test_transcript_appearing_after_scan_is_not_staged(self, tmp_path, monkeypatch):
        """A transcript the candidate scan never saw is skipped by the copy walk.

        Covers an incognito session starting between classification and the
        copy: the walk allowlists root transcripts against the scanned set
        instead of denylisting against the restricted set.
        """
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_postscan"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        late = src / "sessions" / "dashboard_chat-9-999.jsonl"
        late.write_text(_meta_line("incognito") + _msg_line("started mid-snapshot"))

        real_candidates = snapshot_mod._transcript_candidates

        def _scan_missing_late(root):
            return [p for p in real_candidates(root) if p.name != late.name]

        monkeypatch.setattr(snapshot_mod, "_transcript_candidates", _scan_missing_late)
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_postscan"))
        out = tmp_path / "out_postscan"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_postscan")
        assert not (snap / "sessions" / "dashboard_chat-9-999.jsonl").exists()
        assert (snap / "sessions" / "dashboard_chat-1-111.jsonl").is_file()

    def test_flag_landing_during_copy_is_evicted_from_stage(self, tmp_path, monkeypatch):
        """A session flagged !incognito between classification and the copy
        walk must not ride: the post-copy sweep re-reads the map and evicts
        the staged transcript, archive segment, and workspace directory."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_flag_race"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        s = src / "sessions"
        (s / "slack_555.777.jsonl").write_text(_meta_line("persistent") + _msg_line("racy"))
        (s / "archive" / "slack_555.777__20260101T000000Z.jsonl").write_text(
            _msg_line("racy rotated")
        )
        (s / "slack_555.777").mkdir()
        (s / "slack_555.777" / "agent-racy.md").write_text("racy result")

        real_flag = snapshot_mod._flag_restricted_stems
        state = {"calls": 0}

        def _flag_with_race(mc):
            state["calls"] += 1
            out = real_flag(mc)
            if state["calls"] == 1:
                # After the pre-copy classification read, a channel session
                # is flagged !incognito while the copy walk runs.
                m = json.loads((mc / "session_map.json").read_text(encoding="utf-8"))
                m["slack:555.777"] = {"sid": "ffff-5555", "flags": {"incognito": True}}
                (mc / "session_map.json").write_text(json.dumps(m))
            return out

        monkeypatch.setattr(snapshot_mod, "_flag_restricted_stems", _flag_with_race)
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_flag_race"))
        out = tmp_path / "out_flag_race"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_flag_race")
        assert state["calls"] >= 2  # the post-copy re-read actually ran
        assert not (snap / "sessions" / "slack_555.777.jsonl").exists()
        assert not (
            snap / "sessions" / "archive" / "slack_555.777__20260101T000000Z.jsonl"
        ).exists()
        assert not (snap / "sessions" / "slack_555.777").exists()
        # Unaffected sessions still ride, workspace dirs included.
        assert (snap / "sessions" / "dashboard_chat-1-111.jsonl").is_file()
        assert (snap / "sessions" / "chat-1-111" / "agent-abc.md").is_file()

    def test_head_flipping_restricted_during_copy_is_evicted(self, tmp_path, monkeypatch):
        """A transcript whose head turns restricted after classification but
        before the copy is caught by the post-copy re-scan of STAGED heads."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_head_race"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        s = src / "sessions"
        flipping = s / "dashboard_chat-5-555.jsonl"
        flipping.write_text(_meta_line("persistent") + _msg_line("about to go private"))

        real_restricted = snapshot_mod._restricted_session_stems
        state = {"calls": 0}

        def _restricted_with_race(paths, modes):
            state["calls"] += 1
            out = real_restricted(paths, modes)
            if state["calls"] == 1:
                # The session flips to incognito after classification; the
                # copy walk then stages the already-private head.
                flipping.write_text(_meta_line("incognito") + _msg_line("now private"))
            return out

        monkeypatch.setattr(snapshot_mod, "_restricted_session_stems", _restricted_with_race)
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_head_race"))
        out = tmp_path / "out_head_race"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_head_race")
        assert state["calls"] >= 2
        assert not (snap / "sessions" / "dashboard_chat-5-555.jsonl").exists()
        assert (snap / "sessions" / "dashboard_chat-1-111.jsonl").is_file()

    def test_map_unreadable_after_copy_fails_closed(self, tmp_path, monkeypatch):
        """If the post-copy map re-read fails, the entire staged sessions tree
        is discarded: privacy flags can no longer be ruled out for anything."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_postcopy_bad_map"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)

        real_flag = snapshot_mod._flag_restricted_stems
        state = {"calls": 0}

        def _flag_then_corrupt(mc):
            state["calls"] += 1
            out = real_flag(mc)
            if state["calls"] == 1:
                (mc / "session_map.json").write_text("{not json")
            return out

        monkeypatch.setattr(snapshot_mod, "_flag_restricted_stems", _flag_then_corrupt)
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_postcopy_bad_map"))
        out = tmp_path / "out_postcopy_bad_map"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_postcopy_bad_map")
        assert state["calls"] >= 2
        assert not (snap / "sessions").exists()


class TestSessionsRestoreDestinationGuards:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
    def test_linked_local_sessions_root_refused(self, sessions_env, tmp_path, monkeypatch, capsys):
        _, _, tarball, _ = sessions_env
        dst = tmp_path / "dst_linked_root"
        _setup_fake_kirocrew(dst)
        outside = tmp_path / "outside_restore_root"
        outside.mkdir()
        (dst / "sessions").symlink_to(outside)
        # A map entry whose transcript is only in the snapshot: if refusal
        # wrongly triggered reconciliation, this entry would be dropped.
        (dst / "session_map.json").write_text(
            json.dumps({"dashboard:chat-1-111": {"sid": "aaaa-1111"}})
        )
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_linked_root"))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"])
        assert ret == 0
        assert "sessions not restored" in capsys.readouterr().out
        assert not (outside / "dashboard_chat-1-111.jsonl").exists()
        # Refused restore must NOT reconcile: the mapping survives.
        after = json.loads((dst / "session_map.json").read_text(encoding="utf-8"))
        assert "dashboard:chat-1-111" in after

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
    def test_restore_never_truncates_raced_transcript(self, sessions_env, tmp_path, monkeypatch):
        """A transcript created between the exists() probe and the copy is
        owned by the live gateway and never truncated (exclusive create)."""
        from kiro_crew import snapshot as snapshot_mod

        _, _, tarball, _ = sessions_env
        snap = _extract(tarball, tmp_path / "extract_race_tr")
        dd = tmp_path / "dd_race_tr"
        dd.mkdir()
        (dd / "dashboard_chat-1-111.jsonl").write_text("live gateway wrote this")
        # Simulate the race: exists() lies while the file is really there.
        monkeypatch.setattr(Path, "exists", lambda self: False)
        snapshot_mod._copy_sessions_no_overwrite(snap / "sessions", dd)
        monkeypatch.undo()
        content = (dd / "dashboard_chat-1-111.jsonl").read_text(encoding="utf-8")
        assert content == "live gateway wrote this"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
    def test_linked_destination_component_skipped(self, sessions_env, tmp_path, monkeypatch):
        """Restore never writes through an existing linked subdirectory."""
        _, _, tarball, _ = sessions_env
        dst = tmp_path / "dst_linked_archive"
        _setup_fake_kirocrew(dst)
        (dst / "sessions").mkdir()
        outside = tmp_path / "outside_archive"
        outside.mkdir()
        (dst / "sessions" / "archive").symlink_to(outside)
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"])
        assert ret == 0
        # Nothing escaped through the link; the transcript beside it landed.
        assert not any(outside.iterdir())
        assert (dst / "sessions" / "dashboard_chat-1-111.jsonl").is_file()

    def test_do_replace_never_restores_sessions(self, sessions_env, tmp_path, monkeypatch):
        """The shared _do_replace helper must not restore sessions.

        Portability's dashboard ZIP import calls _do_replace directly and
        documents that conversation data is excluded — a manifest-valid ZIP
        carrying a sessions/ directory must not smuggle transcripts in.
        Sessions restore belongs to restore_main only.
        """
        from kiro_crew import snapshot as snapshot_mod

        _, _, tarball, _ = sessions_env
        snap = _extract(tarball, tmp_path / "extract_dorplace")
        fresh = tmp_path / "fresh_doreplace"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        snapshot_mod._do_replace(snap, fresh, None)
        assert not (fresh / "sessions").exists()
        assert (fresh / "memory.db").is_file()


class TestSessionsLinkGuards:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
    def test_linked_sessions_root_refused(self, tmp_path, monkeypatch, capsys):
        """A sessions/ root that is a symlink stages nothing."""
        src = tmp_path / "src_root_link"
        _setup_fake_kirocrew(src)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("credential material")
        (src / "sessions").symlink_to(outside)
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        out = tmp_path / "out_root_link"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_root_link")
        assert not (snap / "sessions" / "secret.txt").exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
    def test_linked_child_dir_skipped(self, tmp_path, monkeypatch):
        """A symlinked entry inside sessions/ never enters the snapshot."""
        src = tmp_path / "src_child_link"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        outside = tmp_path / "outside_child"
        outside.mkdir()
        (outside / "id_rsa").write_text("credential material")
        (src / "sessions" / "chat-1-111" / "linked").symlink_to(outside)
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_child"))
        out = tmp_path / "out_child_link"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_child_link")
        assert not (snap / "sessions" / "chat-1-111" / "linked").exists()
        assert (snap / "sessions" / "chat-1-111" / "agent-abc.md").is_file()

    def test_orphaned_workspace_dir_excluded(self, tmp_path, monkeypatch):
        """A workspace dir with no staged owning transcript never rides along.

        Covers a deleted incognito session whose subagent results were
        retained: with the transcript gone the directory cannot be classified,
        so like archive segments it is allowlisted against staged transcripts.
        """
        src = tmp_path / "src_orphan_ws"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        (src / "sessions" / "chat-5-555").mkdir()
        (src / "sessions" / "chat-5-555" / "agent-xyz.md").write_text("orphaned private result")
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_orphan"))
        out = tmp_path / "out_orphan_ws"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_orphan_ws")
        assert not (snap / "sessions" / "chat-5-555").exists()
        assert (snap / "sessions" / "chat-1-111" / "agent-abc.md").is_file()

    def test_restricted_sibling_archive_not_leaked_by_prefix(self, tmp_path, monkeypatch):
        """An incognito stem that extends a staged stem never leaks its archives.

        The archive owner is everything before the LAST segment delimiter, so
        a segment owned by ``<staged>__private`` must not ride just because its
        name starts with ``<staged>__``.
        """
        src = tmp_path / "src_prefix_leak"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        s = src / "sessions"
        # Incognito sibling whose stem extends the staged persistent stem.
        (s / "dashboard_chat-1-111__private.jsonl").write_text(
            _meta_line("incognito") + _msg_line("secret")
        )
        (s / "archive" / "dashboard_chat-1-111__private__20260101T000000Z.jsonl").write_text(
            _msg_line("secret rotated")
        )
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_prefix"))
        out = tmp_path / "out_prefix_leak"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_prefix_leak")
        assert not (snap / "sessions" / "dashboard_chat-1-111__private.jsonl").exists()
        assert not (
            snap / "sessions" / "archive" / "dashboard_chat-1-111__private__20260101T000000Z.jsonl"
        ).exists()
        # The genuinely staged session's artifacts still ride.
        assert (
            snap / "sessions" / "archive" / "dashboard_chat-1-111__20260101T000000Z.jsonl"
        ).is_file()

    def test_legacy_slack_session_artifacts_staged(self, tmp_path, monkeypatch):
        """A pre-migration Slack transcript's canonical-stem artifacts ride along.

        The live transcript keeps the bare thread_ts filename, but archive
        segments and the workspace directory are named by the canonical
        ``slack:`` key — the allowlist must recognize both spellings.
        """
        src = tmp_path / "src_legacy_slack"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        s = src / "sessions"
        # Legacy live transcript: bare thread_ts stem, persistent.
        (s / "999.111.jsonl").write_text(_meta_line("persistent") + _msg_line("legacy hi"))
        # Canonical-stem archive segment + workspace dir for the same session.
        (s / "archive" / "slack_999.111__20260101T000000Z.jsonl").write_text(
            _msg_line("legacy rotated")
        )
        (s / "slack_999.111").mkdir()
        (s / "slack_999.111" / "agent-leg.md").write_text("legacy subagent result")
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_legacy"))
        out = tmp_path / "out_legacy_slack"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_legacy_slack")
        assert (snap / "sessions" / "999.111.jsonl").is_file()
        assert (snap / "sessions" / "archive" / "slack_999.111__20260101T000000Z.jsonl").is_file()
        assert (snap / "sessions" / "slack_999.111" / "agent-leg.md").is_file()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
    def test_linked_transcript_does_not_authorize_artifacts(self, tmp_path, monkeypatch):
        """A linked transcript never vouches for its stem's archives/workspace.

        The link itself is skipped at copy time, but it must also be excluded
        from the staging allowlist — otherwise its canonical alias would let
        orphaned archive segments and workspace files ride the snapshot.
        """
        src = tmp_path / "src_linked_transcript"
        _setup_fake_kirocrew(src)
        _add_fake_sessions(src)
        s = src / "sessions"
        outside = tmp_path / "outside_transcript"
        outside.mkdir()
        (outside / "real.jsonl").write_text(_meta_line("persistent") + _msg_line("elsewhere"))
        # Legacy-shaped stem via a symlink, plus canonical-stem artifacts that
        # only this link could authorize.
        (s / "888.222.jsonl").symlink_to(outside / "real.jsonl")
        (s / "archive" / "slack_888.222__20260101T000000Z.jsonl").write_text(
            _msg_line("orphaned rotated")
        )
        (s / "slack_888.222").mkdir()
        (s / "slack_888.222" / "agent-orph.md").write_text("orphaned result")
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro_home_linked_tr"))
        out = tmp_path / "out_linked_transcript"
        tarball = _make_snapshot(src, out)
        snap = _extract(tarball, tmp_path / "extract_linked_transcript")
        assert not (snap / "sessions" / "888.222.jsonl").exists()
        assert not (
            snap / "sessions" / "archive" / "slack_888.222__20260101T000000Z.jsonl"
        ).exists()
        assert not (snap / "sessions" / "slack_888.222").exists()
