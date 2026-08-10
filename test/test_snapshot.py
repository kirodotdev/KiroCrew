"""Tests for kiro_crew.snapshot — snapshot and restore."""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
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
        "artifacts/my-widget/versions",
        "artifacts/other-widget",
        "uploads",
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
    (d / "artifacts/my-widget/meta.json").write_text('{"slug": "my-widget", "version": 2}')
    (d / "artifacts/my-widget/current.html").write_text("<p>v2</p>")
    (d / "artifacts/my-widget/versions/v1.html").write_text("<p>v1</p>")
    (d / "artifacts/other-widget/meta.json").write_text('{"slug": "other-widget", "version": 1}')
    (d / "artifacts/other-widget/current.html").write_text("<p>other</p>")
    (d / "artifact_folders.json").write_text(
        json.dumps(
            [
                {"id": "aaaaaaaaaaaa", "name": "Reports", "order": 0, "parent_id": ""},
                {"id": "bbbbbbbbbbbb", "name": "Drafts", "order": 1, "parent_id": "aaaaaaaaaaaa"},
            ]
        )
    )
    (d / "uploads/aaa_doc.txt").write_text("uploaded doc")
    (d / "uploads/bbb_photo.png").write_bytes(b"\x89PNG fake image bytes")


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

    def test_replace_refuses_hardlinked_workspace_file(self, env, monkeypatch, capsys):
        """A hardlinked file would be skipped by the backup then rmtree'd —
        the restore must refuse up front instead of losing it."""
        _, _, tarball, tmp_path = env
        existing = tmp_path / "existing_hl"
        _setup_fake_kirocrew(existing)
        original = existing / "workspace" / "precious.md"
        original.write_text("irreplaceable")
        os.link(str(original), str(existing / "workspace" / "precious-link.md"))
        monkeypatch.setenv("KIROCREW_HOME", str(existing))
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 1
        assert "hardlink" in capsys.readouterr().out.lower()
        # Nothing was mutated: the file survives and no backup dir was left behind.
        assert original.read_text() == "irreplaceable"
        assert (existing / "workspace" / "precious-link.md").is_file()
        assert not [d for d in existing.iterdir() if d.name.startswith("pre-restore-")]

    def test_snapshot_refuses_hardlinked_workspace_file(self, tmp_path, monkeypatch, capsys):
        """Snapshot CREATION aborts (exit 1) on a hardlinked user file instead
        of reporting success while silently omitting it."""
        src = tmp_path / "src_hl_create"
        _setup_fake_kirocrew(src)
        original = src / "workspace" / "keeper.md"
        original.write_text("must not be silently dropped")
        os.link(str(original), str(src / "workspace" / "keeper-link.md"))
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        out_dir = tmp_path / "out_hl_create"
        ret = snapshot_main([str(out_dir)])
        assert ret == 1
        assert "hardlink" in capsys.readouterr().out.lower()
        assert not list(out_dir.glob("*.tar.gz")) if out_dir.is_dir() else True


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
        for c in (
            "memory",
            "crons",
            "config",
            "skills",
            "workspace",
            "notifications",
            "security",
            "artifacts",
            "uploads",
        ):
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


class TestArtifactsUploads:
    def test_snapshot_includes_artifacts_and_uploads(self, env):
        """Artifact slug dirs and upload files ride the snapshot; manifest counts them."""
        _, _, tarball, tmp_path = env
        extract = tmp_path / "extract_art"
        extract.mkdir()
        with tarfile.open(str(tarball)) as tar:
            tar.extractall(extract, filter=lambda t, _d="": t)
        snap = next(d for d in extract.iterdir() if d.name.startswith("kirocrew-snapshot-"))
        assert (snap / "artifacts/my-widget/meta.json").is_file()
        assert (snap / "artifacts/my-widget/current.html").is_file()
        assert (snap / "artifacts/my-widget/versions/v1.html").is_file()
        assert (snap / "artifacts/other-widget/current.html").is_file()
        assert (snap / "uploads/aaa_doc.txt").is_file()
        assert (snap / "uploads/bbb_photo.png").is_file()
        m = json.loads((snap / "MANIFEST.json").read_text(encoding="utf-8"))
        assert m["contents"]["artifact_count"] == 2
        assert m["contents"]["upload_files"] == 2

    def test_restore_fresh_copies_artifacts_and_uploads(self, env, monkeypatch):
        """Replace mode onto a fresh dir restores the full artifact library and uploads."""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh_art"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 0
        assert (fresh / "artifacts/my-widget/versions/v1.html").is_file()
        assert (fresh / "artifacts/other-widget/meta.json").is_file()
        assert (fresh / "uploads/aaa_doc.txt").read_text(encoding="utf-8") == "uploaded doc"
        assert (fresh / "uploads/bbb_photo.png").is_file()

    def test_replace_skips_existing_artifact_slug(self, env, capsys, monkeypatch):
        """An existing slug is never overwritten, even in replace mode; absent slugs import."""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst_art_skip"
        _setup_fake_kirocrew(dst)
        (dst / "artifacts/my-widget/current.html").write_text("<p>local v3</p>")
        shutil.rmtree(str(dst / "artifacts/other-widget"))
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 0
        # Local slug untouched — no file-level merge into an existing slug.
        html = (dst / "artifacts/my-widget/current.html").read_text(encoding="utf-8")
        assert html == "<p>local v3</p>"
        # Absent slug imported whole.
        assert (dst / "artifacts/other-widget/current.html").is_file()
        assert "Artifacts imported: 1 (skipped 1 existing)" in capsys.readouterr().out

    def test_merge_skips_existing_upload_filename(self, env, monkeypatch):
        """An existing upload filename wins over the snapshot copy; absent ones import."""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst_up_skip"
        _setup_fake_kirocrew(dst)
        (dst / "uploads/aaa_doc.txt").write_text("local edit")
        (dst / "uploads/bbb_photo.png").unlink()
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"])
        assert ret == 0
        assert (dst / "uploads/aaa_doc.txt").read_text(encoding="utf-8") == "local edit"
        assert (dst / "uploads/bbb_photo.png").is_file()

    def test_components_artifacts_only(self, env, monkeypatch):
        """--components artifacts restores only the artifact library."""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh_art_only"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        restore_main([str(tarball), "--mode", "replace", "--components", "artifacts", "--force"])
        assert (fresh / "artifacts/my-widget/meta.json").is_file()
        assert not (fresh / "memory.db").exists()
        assert not (fresh / "uploads").exists()
        assert not (fresh / "skills").exists()

    def test_old_snapshot_without_artifacts_restores(self, env, monkeypatch):
        """A snapshot created before these components existed restores unchanged."""
        _, _, tarball, tmp_path = env
        extract = tmp_path / "extract_old"
        extract.mkdir()
        with tarfile.open(str(tarball)) as tar:
            tar.extractall(extract, filter=lambda t, _d="": t)
        snap = next(d for d in extract.iterdir() if d.name.startswith("kirocrew-snapshot-"))
        shutil.rmtree(str(snap / "artifacts"))
        shutil.rmtree(str(snap / "uploads"))
        (snap / "artifact_folders.json").unlink()
        m = json.loads((snap / "MANIFEST.json").read_text(encoding="utf-8"))
        del m["contents"]["artifact_count"]
        del m["contents"]["upload_files"]
        (snap / "MANIFEST.json").write_text(json.dumps(m))
        old_tar = tmp_path / "old_format.tar.gz"
        with tarfile.open(str(old_tar), "w:gz") as tar:
            tar.add(str(snap), arcname=snap.name)
        fresh = tmp_path / "fresh_old"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(old_tar), "--mode", "replace", "--force"])
        assert ret == 0
        assert (fresh / "memory.db").is_file()
        assert not (fresh / "artifacts").exists()
        assert not (fresh / "uploads").exists()

    def test_snapshot_includes_artifact_folders_file(self, env):
        """The folder-tree metadata rides the snapshot beside the artifacts dir."""
        _, _, tarball, tmp_path = env
        extract = tmp_path / "extract_folders"
        extract.mkdir()
        with tarfile.open(str(tarball)) as tar:
            tar.extractall(extract, filter=lambda t, _d="": t)
        snap = next(d for d in extract.iterdir() if d.name.startswith("kirocrew-snapshot-"))
        raw = json.loads((snap / "artifact_folders.json").read_text(encoding="utf-8"))
        assert {f["id"] for f in raw} == {"aaaaaaaaaaaa", "bbbbbbbbbbbb"}

    def test_restore_merges_artifact_folders_target_wins(self, env, capsys, monkeypatch):
        """Folder records merge by id: target copies win, snapshot-only ids append."""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst_folders"
        _setup_fake_kirocrew(dst)
        (dst / "artifact_folders.json").write_text(
            json.dumps(
                [
                    # Same id as the snapshot's "Reports" but renamed locally.
                    {"id": "aaaaaaaaaaaa", "name": "Renamed", "order": 0, "parent_id": ""},
                    {"id": "cccccccccccc", "name": "Local only", "order": 2, "parent_id": ""},
                ]
            )
        )
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"])
        assert ret == 0
        raw = json.loads((dst / "artifact_folders.json").read_text(encoding="utf-8"))
        by_id = {f["id"]: f for f in raw}
        # Target's record for the shared id is untouched.
        assert by_id["aaaaaaaaaaaa"]["name"] == "Renamed"
        # Local-only folder survives; snapshot-only folder is appended.
        assert by_id["cccccccccccc"]["name"] == "Local only"
        assert by_id["bbbbbbbbbbbb"]["name"] == "Drafts"
        assert "Artifact folders imported: 1" in capsys.readouterr().out

    def test_restore_folders_onto_fresh_host(self, env, monkeypatch):
        """A fresh host gets the whole folder tree so folder_id refs resolve."""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh_folders"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 0
        raw = json.loads((fresh / "artifact_folders.json").read_text(encoding="utf-8"))
        assert {f["id"] for f in raw} == {"aaaaaaaaaaaa", "bbbbbbbbbbbb"}

    def test_folder_merge_fails_closed_on_non_list_target(self, env, monkeypatch):
        """A target folders file with valid-but-non-list JSON is never replaced."""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst_folders_nonlist"
        _setup_fake_kirocrew(dst)
        (dst / "artifact_folders.json").write_text('{"not": "a list"}')
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"])
        assert ret == 0
        raw = (dst / "artifact_folders.json").read_text(encoding="utf-8")
        assert raw == '{"not": "a list"}'

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
    def test_restore_created_dirs_are_owner_only(self, env, monkeypatch):
        """Restore-created artifacts/ and uploads/ deny traversal to other users."""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh_modes"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 0
        assert (fresh / "artifacts").stat().st_mode & 0o777 == 0o700
        assert (fresh / "uploads").stat().st_mode & 0o777 == 0o700

    def test_partial_slug_copy_removed_on_failure(self, tmp_path, monkeypatch):
        """An interrupted slug copy never strands a half-copied artifact.

        A stranded partial slug would be skipped as 'existing' by every later
        retry, freezing the corruption permanently.
        """
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_partial"
        dst = tmp_path / "dst_partial"
        (src / "my-widget" / "versions").mkdir(parents=True)
        (src / "my-widget" / "meta.json").write_text("{}")
        (src / "my-widget" / "versions" / "v1.html").write_text("<p>v1</p>")
        dst.mkdir()

        def _boom(s, d, **k):
            # Leave a genuinely partial copy behind before failing.
            Path(d).mkdir(exist_ok=True)
            (Path(d) / "half.html").write_text("partial")
            raise OSError("disk full")

        monkeypatch.setattr(snapshot_mod, "_copytree_safe", _boom)
        with pytest.raises(OSError):
            snapshot_mod._copy_artifacts_no_overwrite(src, dst)
        assert not (dst / "my-widget").exists()

    def test_interrupted_slug_copy_removed_on_keyboard_interrupt(self, tmp_path, monkeypatch):
        """Ctrl-C mid-slug-copy cleans the partial slug before propagating.

        Cleanup gated on ``except OSError`` would let a KeyboardInterrupt
        strand the half-copied slug, which every later additive restore then
        skips as 'existing' — freezing the corruption permanently.
        """
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_intr_slug"
        dst = tmp_path / "dst_intr_slug"
        (src / "my-widget" / "versions").mkdir(parents=True)
        (src / "my-widget" / "meta.json").write_text("{}")
        dst.mkdir()

        def _interrupt(s, d, **k):
            Path(d).mkdir(exist_ok=True)
            (Path(d) / "half.html").write_text("partial")
            raise KeyboardInterrupt

        monkeypatch.setattr(snapshot_mod, "_copytree_safe", _interrupt)
        with pytest.raises(KeyboardInterrupt):
            snapshot_mod._copy_artifacts_no_overwrite(src, dst)
        assert not (dst / "my-widget").exists()

    def test_interrupted_file_copy_removed_on_keyboard_interrupt(self, tmp_path, monkeypatch):
        """Ctrl-C during a top-level artifact file copy never strands the
        zero-byte placeholder created by the exclusive open."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_intr_file"
        dst = tmp_path / "dst_intr_file"
        src.mkdir()
        dst.mkdir()
        (src / "stray.json").write_text("{}")

        def _interrupt(s, d, **k):
            raise KeyboardInterrupt

        monkeypatch.setattr(snapshot_mod.shutil, "copy2", _interrupt)
        with pytest.raises(KeyboardInterrupt):
            snapshot_mod._copy_artifacts_no_overwrite(src, dst)
        monkeypatch.undo()
        assert not (dst / "stray.json").exists()

    def test_raced_slug_is_skipped_not_deleted(self, tmp_path, monkeypatch):
        """A slug created by a concurrent writer after the exists() probe is
        treated as existing — never copied over and never cleaned up."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_race"
        dst = tmp_path / "dst_race"
        (src / "my-widget").mkdir(parents=True)
        (src / "my-widget" / "meta.json").write_text("{}")
        dst.mkdir()
        (dst / "my-widget").mkdir()
        (dst / "my-widget" / "meta.json").write_text('{"local": "wins"}')

        # Simulate the exists() probe losing the race: it reports absent while
        # the directory is really there, so the atomic mkdir reservation is
        # what must catch the conflict.
        monkeypatch.setattr(Path, "exists", lambda self: False)
        imported, skipped = snapshot_mod._copy_artifacts_no_overwrite(src, dst)
        monkeypatch.undo()
        assert imported == 0
        assert skipped == 1
        assert (dst / "my-widget" / "meta.json").read_text(encoding="utf-8") == '{"local": "wins"}'

    def test_restore_skipped_when_owner_only_lockdown_fails(self, env, capsys, monkeypatch):
        """If the destination cannot be made owner-only, nothing is copied."""
        from kiro_crew import snapshot as snapshot_mod

        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh_lockdown"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        monkeypatch.setattr(
            snapshot_mod,
            "_make_restore_dir_owner_only",
            lambda dd: (dd.mkdir(parents=True, exist_ok=True), False)[1],
        )
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "artifacts not restored" in out
        assert "uploads not restored" in out
        assert not (fresh / "artifacts" / "my-widget").exists()
        assert not (fresh / "uploads" / "aaa_doc.txt").exists()

    def test_folder_merge_ignores_malformed_ids(self, env, monkeypatch):
        """Non-string / empty folder ids never crash the restore merge."""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst_bad_ids"
        _setup_fake_kirocrew(dst)
        (dst / "artifact_folders.json").write_text(
            json.dumps(
                [
                    {"id": ["not", "hashable"], "name": "Broken"},
                    {"id": "", "name": "Empty"},
                    {"id": "cccccccccccc", "name": "Local only"},
                ]
            )
        )
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"])
        assert ret == 0
        raw = json.loads((dst / "artifact_folders.json").read_text(encoding="utf-8"))
        names = {f["name"] for f in raw}
        # Merge completed (snapshot folders imported), malformed records kept
        # untouched, no TypeError.
        assert {"Broken", "Empty", "Local only", "Reports", "Drafts"} <= names

    def test_oversized_folder_metadata_skipped(self, tmp_path, monkeypatch, capsys):
        """A snapshot artifact_folders.json past the byte cap is rejected
        before parsing; the target is left untouched."""
        from kiro_crew import snapshot as snapshot_mod

        monkeypatch.setattr(snapshot_mod, "_FOLDERS_MAX_BYTES", 64)
        src_f = tmp_path / "snap_folders.json"
        dst_f = tmp_path / "live_folders.json"
        src_f.write_text(json.dumps([{"id": "a" * 200, "name": "Bloated"}]))
        dst_f.write_text(
            json.dumps([{"id": "keep00000000", "name": "Local", "order": 0, "parent_id": ""}])
        )
        snapshot_mod._merge_artifact_folders(src_f, dst_f)
        assert "size cap" in capsys.readouterr().out
        raw = json.loads(dst_f.read_text(encoding="utf-8"))
        assert [f["id"] for f in raw] == ["keep00000000"]

    def test_folder_metadata_record_cap(self, tmp_path, monkeypatch, capsys):
        """A snapshot folder list past the record cap is rejected wholesale;
        the target is left untouched."""
        from kiro_crew import snapshot as snapshot_mod

        monkeypatch.setattr(snapshot_mod, "_FOLDERS_MAX_RECORDS", 2)
        src_f = tmp_path / "snap_folders_many.json"
        dst_f = tmp_path / "live_folders_many.json"
        src_f.write_text(
            json.dumps(
                [{"id": f"id{i:010d}", "name": f"F{i}", "order": i, "parent_id": ""} for i in range(3)]
            )
        )
        dst_f.write_text(
            json.dumps([{"id": "keep00000000", "name": "Local", "order": 0, "parent_id": ""}])
        )
        snapshot_mod._merge_artifact_folders(src_f, dst_f)
        assert "record cap" in capsys.readouterr().out
        raw = json.loads(dst_f.read_text(encoding="utf-8"))
        assert [f["id"] for f in raw] == ["keep00000000"]

    def test_store_tmp_files_not_staged(self, tmp_path, monkeypatch):
        """The artifact store's atomic-write *.tmp staging files never ride."""
        src = tmp_path / "src_tmpfiles"
        _setup_fake_kirocrew(src)
        (src / "artifacts" / "my-widget" / "meta.json.tmp").write_text("{torn")
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        out = tmp_path / "out_tmpfiles"
        tarball = _make_snapshot(src, out)
        extract = tmp_path / "extract_tmpfiles"
        extract.mkdir()
        with tarfile.open(str(tarball)) as tar:
            tar.extractall(extract, filter=lambda t, _d="": t)
        snap = next(d for d in extract.iterdir() if d.name.startswith("kirocrew-snapshot-"))
        assert not (snap / "artifacts" / "my-widget" / "meta.json.tmp").exists()
        assert (snap / "artifacts" / "my-widget" / "meta.json").is_file()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
    def test_linked_component_roots_not_staged(self, tmp_path, monkeypatch):
        """Symlinked artifacts/ or uploads/ roots stage nothing."""
        src = tmp_path / "src_linked_roots"
        _setup_fake_kirocrew(src)
        shutil.rmtree(str(src / "artifacts"))
        shutil.rmtree(str(src / "uploads"))
        outside = tmp_path / "outside_roots"
        (outside / "secrets").mkdir(parents=True)
        (outside / "secrets" / "id_key").write_text("credential material")
        (src / "artifacts").symlink_to(outside)
        (src / "uploads").symlink_to(outside)
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        out = tmp_path / "out_linked_roots"
        tarball = _make_snapshot(src, out)
        extract = tmp_path / "extract_linked_roots"
        extract.mkdir()
        with tarfile.open(str(tarball)) as tar:
            tar.extractall(extract, filter=lambda t, _d="": t)
        snap = next(d for d in extract.iterdir() if d.name.startswith("kirocrew-snapshot-"))
        assert not (snap / "artifacts" / "secrets").exists()
        assert not (snap / "uploads" / "secrets").exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
    def test_linked_restore_root_not_mutated(self, env, tmp_path, monkeypatch):
        """A linked destination root is refused BEFORE any permission change."""
        _, _, tarball, _ = env
        dst = tmp_path / "dst_linked_restore"
        _setup_fake_kirocrew(dst)
        shutil.rmtree(str(dst / "uploads"))
        outside = tmp_path / "outside_perm_target"
        outside.mkdir()
        outside.chmod(0o755)
        (dst / "uploads").symlink_to(outside)
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"])
        assert ret == 0
        # The link's target kept its permissions AND received no files.
        assert outside.stat().st_mode & 0o777 == 0o755
        assert not any(outside.iterdir())

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
    def test_linked_destination_component_in_uploads_skipped(self, tmp_path, monkeypatch, env):
        """Restore never writes through an existing linked uploads subpath."""
        _, _, tarball, _ = env
        dst = tmp_path / "dst_linked_subpath"
        _setup_fake_kirocrew(dst)
        outside = tmp_path / "outside_subpath"
        outside.mkdir()
        # A dangling link named exactly like an incoming upload: exists() says
        # False, so the naive copy would write THROUGH it to the link target.
        (dst / "uploads" / "aaa_doc.txt").unlink()
        (dst / "uploads" / "aaa_doc.txt").symlink_to(outside / "gone.txt")
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"])
        assert ret == 0
        assert not (outside / "gone.txt").exists()

    def test_slug_vanishing_mid_copy_skipped_not_fatal(self, tmp_path, monkeypatch):
        """A slug deleted while being staged is dropped, not a snapshot abort."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_vanish"
        dst = tmp_path / "dst_vanish"
        (src / "doomed" / "versions").mkdir(parents=True)
        (src / "doomed" / "meta.json").write_text('{"version": 1}')
        (src / "keeper").mkdir()
        (src / "keeper" / "meta.json").write_text('{"version": 1}')
        dst.mkdir()

        real_copy = snapshot_mod._copytree_safe

        def _vanish_copy(s, d, **k):
            if Path(s).name == "doomed":
                shutil.rmtree(str(Path(s)))
                raise FileNotFoundError("source vanished")
            real_copy(s, d, **k)

        monkeypatch.setattr(snapshot_mod, "_copytree_safe", _vanish_copy)
        snapshot_mod._stage_artifact_slugs(src, dst)
        monkeypatch.undo()
        assert not (dst / "doomed").exists()
        assert (dst / "keeper" / "meta.json").is_file()

    def test_metaless_slug_never_staged(self, tmp_path):
        """A slug without readable meta.json (mid-create/delete) never rides."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_metaless"
        dst = tmp_path / "dst_metaless"
        (src / "half-created").mkdir(parents=True)
        (src / "half-created" / "current.html").write_text("<p>no meta yet</p>")
        dst.mkdir()
        snapshot_mod._stage_artifact_slugs(src, dst)
        assert not (dst / "half-created").exists()

    def test_folder_merge_proceeds_despite_unrelated_port_listener(self, env, capsys, monkeypatch):
        """An unrelated listener on the default port must NOT block the folder
        merge: ownership of THIS data home is decided by the mc-scoped
        ``gateway.lock`` flock alone (a gateway owning the home holds it and
        is covered by the flock test below)."""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh_gateway_folders"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "1")
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "folder tree not merged" not in out
        assert (fresh / "artifacts" / "my-widget" / "meta.json").is_file()
        assert (fresh / "artifact_folders.json").is_file()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX flock")
    def test_folder_merge_skipped_when_gateway_lock_held(self, env, capsys, monkeypatch):
        """A gateway on a custom port is still detected via gateway.lock."""
        import fcntl

        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh_lock_folders"
        fresh.mkdir()
        # Simulate a live gateway on a non-default port: no port probe hit,
        # but an exclusive flock held on gateway.lock.
        lock_path = fresh / "gateway.lock"
        lock_path.write_text("1")
        holder = os.open(str(lock_path), os.O_RDWR)
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            monkeypatch.setenv("KIROCREW_HOME", str(fresh))
            monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
            ret = restore_main([str(tarball), "--mode", "replace", "--force"])
            assert ret == 0
            assert "folder tree not merged" in capsys.readouterr().out
            assert not (fresh / "artifact_folders.json").exists()
        finally:
            os.close(holder)

    def test_file_squatting_on_component_name_skips_component(self, env, capsys, monkeypatch):
        """A regular FILE named uploads/ must not abort the whole restore."""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst_squat"
        _setup_fake_kirocrew(dst)
        shutil.rmtree(str(dst / "uploads"))
        (dst / "uploads").write_text("I am a file, not a directory")
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"])
        assert ret == 0
        assert "uploads not restored" in capsys.readouterr().out
        # The squatting file survives untouched; other components restored.
        assert (dst / "uploads").is_file()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX hardlinks")
    def test_snapshot_refuses_hardlinked_upload(self, tmp_path, monkeypatch, capsys):
        """Snapshot CREATION aborts (exit 1) on a hardlinked upload instead of
        reporting success while silently omitting it — mirroring the
        workspace/skills components."""
        src = tmp_path / "src_hl_upload"
        _setup_fake_kirocrew(src)
        original = src / "uploads" / "aaa_doc.txt"
        os.link(str(original), str(src / "uploads" / "doc-link.txt"))
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        out_dir = tmp_path / "out_hl_upload"
        ret = snapshot_main([str(out_dir)])
        assert ret == 1
        assert "hardlink" in capsys.readouterr().out.lower()
        assert not list(out_dir.glob("*.tar.gz")) if out_dir.is_dir() else True

    @pytest.mark.skipif(os.name == "nt", reason="POSIX hardlinks")
    def test_snapshot_refuses_hardlinked_artifact_file(self, tmp_path, monkeypatch, capsys):
        """Snapshot CREATION aborts (exit 1) on a hardlinked file inside an
        artifact slug instead of shipping a snapshot that silently lacks it."""
        src = tmp_path / "src_hl_artifact"
        _setup_fake_kirocrew(src)
        original = src / "artifacts" / "my-widget" / "current.html"
        os.link(str(original), str(src / "artifacts" / "my-widget" / "linked.html"))
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        out_dir = tmp_path / "out_hl_artifact"
        ret = snapshot_main([str(out_dir)])
        assert ret == 1
        assert "hardlink" in capsys.readouterr().out.lower()
        assert not list(out_dir.glob("*.tar.gz")) if out_dir.is_dir() else True

    def test_unstable_upload_skipped_from_snapshot(self, tmp_path, monkeypatch):
        """A file whose (size, mtime) moves during every copy attempt is
        dropped from this snapshot generation rather than shipped truncated."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_unstable"
        dst = tmp_path / "dst_unstable"
        src.mkdir()
        (src / "steady.txt").write_text("complete upload")
        moving = src / "inflight.bin"
        moving.write_text("chunk-0")

        real_pinned = snapshot_mod._pinned_copy_file
        counter = {"n": 0}

        def _copy_and_grow(s, d, **k):
            real_pinned(s, d, **k)
            if Path(s).name == "inflight.bin":
                # Simulate the upload handler appending after our copy.
                counter["n"] += 1
                with open(s, "a", encoding="utf-8") as f:
                    f.write(f"chunk-{counter['n']}")

        monkeypatch.setattr(snapshot_mod, "_pinned_copy_file", _copy_and_grow)
        snapshot_mod._stage_uploads_stable(src, dst)
        monkeypatch.undo()
        assert (dst / "steady.txt").read_text(encoding="utf-8") == "complete upload"
        assert not (dst / "inflight.bin").exists()

    def test_concurrent_restore_never_overwrites_restored_upload(self, tmp_path, monkeypatch):
        """The guarded copy creates files exclusively — a file that appears
        between the walk and the write is owned by someone else and skipped."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_excl"
        dst = tmp_path / "dst_excl"
        src.mkdir()
        dst.mkdir()
        (src / "doc.txt").write_text("from snapshot")
        (dst / "doc.txt").write_text("from concurrent restore")
        # exists() lies (simulating the race window); O_EXCL must catch it.
        monkeypatch.setattr(Path, "exists", lambda self: False)
        snapshot_mod._copy_tree_no_overwrite_guarded(src, dst)
        monkeypatch.undo()
        assert (dst / "doc.txt").read_text(encoding="utf-8") == "from concurrent restore"

    def test_guarded_copy_opens_destination_binary(self, tmp_path, monkeypatch):
        """The exclusive-create descriptor carries O_BINARY where it exists.

        Writing through a Windows CRT text-mode fd expands 0x0A to 0x0D 0x0A,
        permanently corrupting every restored binary upload — the same hazard
        ``_read_meta_bounded`` already guards on its read path.
        """
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_obinary"
        dst = tmp_path / "dst_obinary"
        src.mkdir()
        dst.mkdir()
        payload = b"\x89PNG\r\n\x1a\n\x0adata"
        (src / "img.png").write_bytes(payload)
        fake_flag = 0x40000000  # stands in for os.O_BINARY on POSIX
        monkeypatch.setattr(os, "O_BINARY", fake_flag, raising=False)
        seen: dict[str, int] = {}
        real_open = os.open

        def _capture(path, flags, *a, **k):
            seen[str(path)] = flags
            return real_open(path, flags & ~fake_flag, *a, **k)

        monkeypatch.setattr(snapshot_mod.os, "open", _capture)
        snapshot_mod._copy_tree_no_overwrite_guarded(src, dst)
        monkeypatch.undo()
        # dir_fd-pinned creation opens by bare name relative to the parent
        # descriptor, so match on the basename rather than the full path.
        file_opens = [f for p, f in seen.items() if Path(p).name == "img.png"]
        assert file_opens and all(f & fake_flag for f in file_opens)
        assert (dst / "img.png").read_bytes() == payload

    def test_upload_path_type_conflict_skipped(self, env, capsys, monkeypatch, tmp_path):
        """A local FILE occupying a snapshot DIRECTORY path skips additively."""
        _, _, tarball, _ = env
        # Build a snapshot whose uploads/ contains a subdirectory.
        extract = tmp_path / "extract_typeconf"
        extract.mkdir()
        with tarfile.open(str(tarball)) as tar:
            tar.extractall(extract, filter=lambda t, _d="": t)
        snap = next(d for d in extract.iterdir() if d.name.startswith("kirocrew-snapshot-"))
        (snap / "uploads" / "subdir").mkdir()
        (snap / "uploads" / "subdir" / "nested.txt").write_text("nested upload")
        conf_tar = tmp_path / "typeconf.tar.gz"
        with tarfile.open(str(conf_tar), "w:gz") as tar:
            tar.add(str(snap), arcname=snap.name)
        dst = tmp_path / "dst_typeconf"
        _setup_fake_kirocrew(dst)
        # Local FILE squats on the directory name the snapshot wants.
        (dst / "uploads" / "subdir").write_text("I am a file")
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(conf_tar), "--mode", "merge", "--force"])
        assert ret == 0
        assert "path type conflict" in capsys.readouterr().out
        # Local file untouched; sibling uploads still restored.
        assert (dst / "uploads" / "subdir").is_file()
        assert (dst / "uploads" / "bbb_photo.png").is_file()

    def test_folder_merge_survives_deeply_nested_json(self, env, monkeypatch, tmp_path, capsys):
        """A pathologically nested folders file skips the merge, not the restore."""
        _, _, tarball, _ = env
        extract = tmp_path / "extract_nested"
        extract.mkdir()
        with tarfile.open(str(tarball)) as tar:
            tar.extractall(extract, filter=lambda t, _d="": t)
        snap = next(d for d in extract.iterdir() if d.name.startswith("kirocrew-snapshot-"))
        (snap / "artifact_folders.json").write_text("[" * 40000 + "]" * 40000)
        nested_tar = tmp_path / "nested.tar.gz"
        with tarfile.open(str(nested_tar), "w:gz") as tar:
            tar.add(str(snap), arcname=snap.name)
        fresh = tmp_path / "fresh_nested"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(nested_tar), "--mode", "replace", "--force"])
        assert ret == 0
        # Artifacts restored despite the poisoned folders file.
        assert (fresh / "artifacts" / "my-widget" / "meta.json").is_file()

    def test_slug_recopied_when_meta_changes_mid_copy(self, tmp_path, monkeypatch):
        """A slug whose meta.json moves during the copy is re-copied whole."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_metarace"
        dst = tmp_path / "dst_metarace"
        (src / "my-widget").mkdir(parents=True)
        (src / "my-widget" / "meta.json").write_text('{"version": 1}')
        (src / "my-widget" / "current.html").write_text("<p>v1</p>")

        real_copy = snapshot_mod._copytree_safe
        bumped = {"done": False}

        def _racy_copy(s, d, **k):
            real_copy(s, d, **k)
            if not bumped["done"]:
                # Simulate a live update landing mid-copy: meta and content
                # advance AFTER this copy completed.
                bumped["done"] = True
                (src / "my-widget" / "meta.json").write_text('{"version": 2}')
                (src / "my-widget" / "current.html").write_text("<p>v2</p>")

        monkeypatch.setattr(snapshot_mod, "_copytree_safe", _racy_copy)
        snapshot_mod._stage_artifact_slugs(src, dst)
        # The retry captured the post-update state coherently.
        assert (dst / "my-widget" / "meta.json").read_text(encoding="utf-8") == '{"version": 2}'
        assert (dst / "my-widget" / "current.html").read_text(encoding="utf-8") == "<p>v2</p>"

    def test_slug_recopied_when_content_changes_but_meta_does_not(self, tmp_path, monkeypatch):
        """A mid-copy update that touches content/version files WITHOUT
        rewriting meta.json must still fail the stability probe — a meta-only
        check would ship the torn copy."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_torn"
        dst = tmp_path / "dst_torn"
        (src / "my-widget").mkdir(parents=True)
        (src / "my-widget" / "meta.json").write_text('{"version": 1}')
        (src / "my-widget" / "current.html").write_text("<p>v1</p>")

        real_copy = snapshot_mod._copytree_safe
        bumped = {"done": False}

        def _racy_copy(s, d, **k):
            real_copy(s, d, **k)
            if not bumped["done"]:
                # Content advances mid-copy; meta.json is left untouched
                # (updater writes meta last — or not at all for history files).
                bumped["done"] = True
                content = src / "my-widget" / "current.html"
                content.write_text("<p>v2</p>")
                os.utime(content, ns=(1, 1))  # force a distinct mtime_ns

        monkeypatch.setattr(snapshot_mod, "_copytree_safe", _racy_copy)
        snapshot_mod._stage_artifact_slugs(src, dst)
        # The retry re-copied and captured the post-update content.
        assert (dst / "my-widget" / "current.html").read_text(encoding="utf-8") == "<p>v2</p>"

    def test_folder_merge_sanitizes_malformed_fields(self, env, monkeypatch, tmp_path):
        """Imported folder records are coerced to the store's exact shape.

        A non-numeric ``order`` from a foreign snapshot would otherwise
        persist and crash the folder listing's int() coercion server-side.
        """
        _, _, tarball, _ = env
        # Build a snapshot whose folders file carries malformed fields.
        extract = tmp_path / "extract_sanitize"
        extract.mkdir()
        with tarfile.open(str(tarball)) as tar:
            tar.extractall(extract, filter=lambda t, _d="": t)
        snap = next(d for d in extract.iterdir() if d.name.startswith("kirocrew-snapshot-"))
        (snap / "artifact_folders.json").write_text(
            json.dumps(
                [
                    {"id": "dddddddddddd", "name": 42, "order": "NaN", "parent_id": None},
                ]
            )
        )
        bad_tar = tmp_path / "sanitize.tar.gz"
        with tarfile.open(str(bad_tar), "w:gz") as tar:
            tar.add(str(snap), arcname=snap.name)
        fresh = tmp_path / "fresh_sanitize"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        ret = restore_main([str(bad_tar), "--mode", "replace", "--force"])
        assert ret == 0
        raw = json.loads((fresh / "artifact_folders.json").read_text(encoding="utf-8"))
        rec = next(f for f in raw if f["id"] == "dddddddddddd")
        assert isinstance(rec["order"], int)
        assert isinstance(rec["name"], str)
        assert rec["parent_id"] == ""
        # The store's own listing path must survive the imported record.
        from kiro_crew.artifacts import ArtifactFolderStore

        store = ArtifactFolderStore(path=fresh / "artifact_folders.json")
        assert any(f["id"] == "dddddddddddd" for f in store.list())

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink")
    def test_symlinked_meta_json_marks_slug_unstable(self, tmp_path, capsys):
        """A meta.json that is a symlink is never raw-read; the slug skips.

        A link to an endless source (device node, FIFO) would turn the bare
        read into an unbounded hang/OOM; the bounded reader refuses the link
        at open, so the stability check fails and the slug never rides.
        """
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_linkmeta"
        dst = tmp_path / "dst_linkmeta"
        (src / "linked").mkdir(parents=True)
        real = tmp_path / "outside.json"
        real.write_text('{"version": 1}')
        (src / "linked" / "meta.json").symlink_to(real)
        (src / "ok-slug").mkdir()
        (src / "ok-slug" / "meta.json").write_text('{"version": 1}')
        dst.mkdir()
        snapshot_mod._stage_artifact_slugs(src, dst)
        assert not (dst / "linked").exists()
        assert (dst / "ok-slug" / "meta.json").is_file()
        assert "skipped this generation" in capsys.readouterr().out

    def test_oversized_meta_json_marks_slug_unstable(self, tmp_path, monkeypatch):
        """A meta.json past the size cap fails the stability check (skip)."""
        from kiro_crew import snapshot as snapshot_mod

        monkeypatch.setattr(snapshot_mod, "_META_MAX_BYTES", 16)
        src = tmp_path / "src_bigmeta"
        dst = tmp_path / "dst_bigmeta"
        (src / "bloated").mkdir(parents=True)
        (src / "bloated" / "meta.json").write_text("x" * 64)
        (src / "small").mkdir()
        (src / "small" / "meta.json").write_text('{"v": 1}')
        dst.mkdir()
        snapshot_mod._stage_artifact_slugs(src, dst)
        assert not (dst / "bloated").exists()
        assert (dst / "small" / "meta.json").is_file()

    def test_slug_copy_permission_error_fails_loudly(self, tmp_path, monkeypatch):
        """A non-disappearance copy failure aborts the snapshot stage.

        Suppressing EACCES/ENOSPC would report a successful snapshot that is
        silently missing artifacts; only a vanished source may be skipped.
        """
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_eacces"
        dst = tmp_path / "dst_eacces"
        (src / "denied").mkdir(parents=True)
        (src / "denied" / "meta.json").write_text('{"version": 1}')
        dst.mkdir()

        def _denied_copy(s, d, **k):
            raise PermissionError("copy denied")

        monkeypatch.setattr(snapshot_mod, "_copytree_safe", _denied_copy)
        with pytest.raises(PermissionError):
            snapshot_mod._stage_artifact_slugs(src, dst)

    def test_upload_copy_permission_error_fails_loudly(self, tmp_path, monkeypatch):
        """Upload staging re-raises non-disappearance copy failures."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_up_eacces"
        dst = tmp_path / "dst_up_eacces"
        src.mkdir()
        (src / "doc.txt").write_text("payload")
        dst.mkdir()

        def _denied_copy(s, d, **k):
            raise PermissionError("copy denied")

        monkeypatch.setattr(snapshot_mod, "_pinned_copy_file", _denied_copy)
        with pytest.raises(PermissionError):
            snapshot_mod._stage_uploads_stable(src, dst)

    def test_vanished_upload_skipped_not_fatal(self, tmp_path, monkeypatch):
        """A source file vanishing mid-copy is dropped, not a snapshot abort."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_up_vanish"
        dst = tmp_path / "dst_up_vanish"
        src.mkdir()
        (src / "gone.txt").write_text("rejected upload")
        (src / "kept.txt").write_text("complete upload")
        dst.mkdir()
        real_pinned = snapshot_mod._pinned_copy_file

        def _vanish_copy(s, d, **k):
            if Path(s).name == "gone.txt":
                raise FileNotFoundError("vanished")
            real_pinned(s, d, **k)

        monkeypatch.setattr(snapshot_mod, "_pinned_copy_file", _vanish_copy)
        snapshot_mod._stage_uploads_stable(src, dst)
        monkeypatch.undo()
        assert not (dst / "gone.txt").exists()
        assert (dst / "kept.txt").read_text(encoding="utf-8") == "complete upload"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX flock")
    def test_folder_merge_holds_gateway_lock_across_merge(self, env, monkeypatch, tmp_path):
        """The gateway.lock flock is HELD during the merge, then released.

        A probe-then-release check leaves a window: a gateway starting after
        the probe loads the pre-merge folder list and its next save erases
        the merged records. While the merge runs, an independent exclusive
        acquire must fail; after the restore it must succeed again.
        """
        import fcntl

        from kiro_crew import snapshot as snapshot_mod

        _, _, tarball, _ = env
        fresh = tmp_path / "fresh_lock_held"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        real_merge = snapshot_mod._merge_artifact_folders
        seen = {"locked_during_merge": None}

        def _probing_merge(src_path, dst_path):
            probe = os.open(str(fresh / "gateway.lock"), os.O_RDWR)
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen["locked_during_merge"] = False
                fcntl.flock(probe, fcntl.LOCK_UN)
            except OSError:
                seen["locked_during_merge"] = True
            finally:
                os.close(probe)
            real_merge(src_path, dst_path)

        monkeypatch.setattr(snapshot_mod, "_merge_artifact_folders", _probing_merge)
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        monkeypatch.undo()
        assert ret == 0
        assert seen["locked_during_merge"] is True
        assert (fresh / "artifact_folders.json").is_file()
        # Released after the merge: a fresh exclusive acquire succeeds.
        fd = os.open(str(fresh / "gateway.lock"), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ── Comment 8: New edge-case tests ───────────────────────────────────────────


class TestDescriptorPinnedStaging:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX hardlinks")
    def test_pinned_copy_refuses_hardlink_at_descriptor(self, tmp_path):
        """The staged-read gate validates the inode on the OPEN DESCRIPTOR, so
        a hardlink that slipped past a by-name screen is still refused at
        copy time — credential bytes never enter the stage."""
        from kiro_crew import snapshot as snapshot_mod

        secret = tmp_path / "credential"
        secret.write_text("AKIA-secret")
        link = tmp_path / "innocent.html"
        os.link(str(secret), str(link))
        dstf = tmp_path / "staged.html"
        with pytest.raises(RuntimeError, match="hardlink"):
            snapshot_mod._pinned_copy_file(str(link), str(dstf), on_hardlink="abort")
        assert not dstf.exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
    def test_pinned_copy_skips_symlink_without_dereference(self, tmp_path, capsys):
        """A symlink swapped in after the listing screen surfaces as ELOOP on
        the O_NOFOLLOW open and is skipped — never dereferenced."""
        from kiro_crew import snapshot as snapshot_mod

        secret = tmp_path / "credential"
        secret.write_text("AKIA-secret")
        link = tmp_path / "swapped.html"
        link.symlink_to(secret)
        dstf = tmp_path / "staged.html"
        snapshot_mod._pinned_copy_file(str(link), str(dstf), on_hardlink="abort")
        assert not dstf.exists()
        assert "Skipping symlink" in capsys.readouterr().out

    def test_slug_staging_copies_through_pinned_gate(self, tmp_path, monkeypatch):
        """Abort-mode tree staging routes every file copy through the
        descriptor-pinned gate — no bare by-name copy remains."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_gate"
        dst = tmp_path / "dst_gate"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_text("a")
        (src / "sub" / "b.txt").write_text("b")
        seen: list[str] = []
        real_pinned = snapshot_mod._pinned_copy_file

        def _record(s, d, **k):
            seen.append(Path(s).name)
            real_pinned(s, d, **k)

        monkeypatch.setattr(snapshot_mod, "_pinned_copy_file", _record)
        snapshot_mod._copytree_safe(src, dst, on_hardlink="abort")
        assert sorted(seen) == ["a.txt", "b.txt"]
        assert (dst / "sub" / "b.txt").read_text(encoding="utf-8") == "b"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX hardlinks")
    def test_snapshot_refuses_hardlinked_folder_metadata(self, tmp_path, monkeypatch, capsys):
        """Snapshot CREATION aborts (exit 1) on a hardlinked
        artifact_folders.json instead of shipping a snapshot whose fresh
        restore silently loses the folder organization."""
        src = tmp_path / "src_hl_folders"
        _setup_fake_kirocrew(src)
        os.link(str(src / "artifact_folders.json"), str(src / "folders-link.json"))
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        out_dir = tmp_path / "out_hl_folders"
        ret = snapshot_main([str(out_dir)])
        assert ret == 1
        assert "hardlink" in capsys.readouterr().out.lower()

    def test_snapshot_staging_oserror_reported_not_traceback(
        self, tmp_path, monkeypatch, capsys
    ):
        """A filesystem failure during staging (ENOSPC, EACCES) exits 1 with
        the module's error message instead of escaping as a traceback."""
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_oserr"
        _setup_fake_kirocrew(src)
        monkeypatch.setenv("KIROCREW_HOME", str(src))

        def _no_space(*a, **k):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(snapshot_mod, "_stage_uploads_stable", _no_space)
        ret = snapshot_main([str(tmp_path / "out_oserr")])
        assert ret == 1
        assert "No space left" in capsys.readouterr().out

    def test_restore_oserror_reported_not_traceback(self, env, monkeypatch, capsys):
        """A filesystem failure mid-restore exits 1 with the module's error
        message instead of a traceback after partial restoration."""
        from kiro_crew import snapshot as snapshot_mod

        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh_oserr"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))

        def _disk_full(*a, **k):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(snapshot_mod, "_do_replace", _disk_full)
        ret = restore_main([str(tarball), "--mode", "replace", "--force"])
        assert ret == 1
        assert "No space left" in capsys.readouterr().out

    @pytest.mark.skipif(
        os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
        reason="POSIX permissions, non-root",
    )
    def test_unreadable_meta_fails_snapshot_not_skips(self, tmp_path):
        """An EACCES meta.json FAILS the stage instead of silently omitting
        the whole slug from a backup that then reports success (an absent
        meta.json still skips — covered by test_metaless_slug_never_staged).
        """
        from kiro_crew import snapshot as snapshot_mod

        src = tmp_path / "src_meta_eacces"
        dst = tmp_path / "dst_meta_eacces"
        (src / "locked").mkdir(parents=True)
        meta = src / "locked" / "meta.json"
        meta.write_text('{"version": 1}')
        dst.mkdir()
        meta.chmod(0o000)
        try:
            with pytest.raises(PermissionError):
                snapshot_mod._stage_artifact_slugs(src, dst)
        finally:
            meta.chmod(0o600)
        assert not (dst / "locked").exists()

    def test_guarded_copy_pins_destination_traversal(self, tmp_path, monkeypatch):
        """Every destination component is opened relative to its parent's
        descriptor (dir_fd + bare name): no by-name re-traversal remains
        between the link validation and the write, so an ancestor swapped
        for a symlink after validation cannot redirect the restore."""
        from kiro_crew import snapshot as snapshot_mod

        if not snapshot_mod._GUARDED_DIR_FD_OK:
            pytest.skip("dir_fd not supported on this platform")
        src = tmp_path / "src_pin"
        dst = tmp_path / "dst_pin"
        (src / "nested" / "deep").mkdir(parents=True)
        (src / "nested" / "deep" / "f.txt").write_text("payload")
        dst.mkdir()
        calls: list[tuple[str, bool]] = []
        real_open = os.open

        def _capture(path, flags, *a, **k):
            calls.append((str(path), k.get("dir_fd") is not None))
            return real_open(path, flags, *a, **k)

        monkeypatch.setattr(snapshot_mod.os, "open", _capture)
        snapshot_mod._copy_tree_no_overwrite_guarded(src, dst)
        monkeypatch.undo()
        assert (dst / "nested" / "deep" / "f.txt").read_text(encoding="utf-8") == "payload"
        # The destination root is opened by full path; every component below
        # it by bare name relative to a directory descriptor.
        non_root = [c for c in calls if c[0] != str(dst)]
        assert non_root and all(pinned for _path, pinned in non_root)
        assert all(os.sep not in path for path, _pinned in non_root)


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
