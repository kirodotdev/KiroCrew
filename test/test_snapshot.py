"""Tests for kiro_crew.snapshot — snapshot and restore."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tarfile
import threading
import time
from pathlib import Path

import pytest

from conftest import requires_o_nofollow, requires_symlinks
from kiro_crew import snapshot as snapshot_mod
from kiro_crew.jsonl_util import OversizedRecord, UndecodableRecord, UnreadableRecord
from kiro_crew.snapshot import restore_main, snapshot_main

# ── Helpers ───────────────────────────────────────────────────────────────────


def unpinnable_argv() -> list[str]:
    """``--allow-unpinned-staging``, but ONLY where the platform cannot pin a tree walk.

    ``_staging_is_pinned`` refuses rather than falling back when there are no directory
    descriptors, which is deliberate: a by-name walk is the mechanism whose failure closed
    two pull requests, so the weaker mode is never something the tool picks on the
    operator's behalf. A test that drives snapshot or restore therefore has to say the same
    thing an operator on such a platform has to say, or it dies at the refusal instead of
    reaching its own subject -- which is what left the whole snapshot suite red on Windows.

    Returned CONDITIONALLY, never unconditionally. Passing the flag everywhere would move
    Linux onto the by-name traversal too and quietly delete this suite's coverage of the
    pinned path, which is the path that actually ships. Where pinning works this is empty
    and nothing changes.

    Not for a test whose SUBJECT is the pinned guarantee itself (an ancestor swap being
    refused, a nested symlink not being copied). That guarantee does not exist on a
    platform without descriptors, so such a test skips there rather than asserting a
    promise the platform cannot keep.
    """
    from kiro_crew import pinned_fs

    return [] if pinned_fs.supports_pinned_tree_walk() else ["--allow-unpinned-staging"]


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
        "workspace/knowledge",
        "workspace/hygiene_data",
        "skills/my-skill",
        "plan_memory",
    ):
        (d / sub).mkdir(parents=True, exist_ok=True)

    # The markdown half of memory, which the `memory` component claims alongside the
    # databases so restoring memory does not require the whole workspace.
    (d / "workspace/memory/preferences.md").write_text("- prefers terse answers\n")
    (d / "workspace/memory/projects.md").write_text("# Active Projects\n")
    (d / "workspace/knowledge/kb.sqlite3").write_bytes(b"SQLite format 3\x00stub")

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
    (d / "telemetry_salt").write_bytes(b"\x04" * snapshot_mod._TELEMETRY_SALT_BYTES)
    (d / "notifications.jsonl").write_text('{"ts":"2026-01-01","msg":"test"}\n')
    (d / "project_dir").write_text("/home/user/project")
    (d / "workspace_dir").write_text("/home/user/.kirocrew/workspace")
    (d / "workspace/memory/history/2026-01-01.md").write_text("history entry")
    (d / "workspace/doc.md").write_text("doc content")
    (d / "workspace/hygiene_data/week1.json").write_text("big data")
    (d / "plan_memory/plan1.json").write_text("plan data")
    (d / "skills/my-skill/SKILL.md").write_text("# My Skill")


def _make_snapshot(src: Path, out: Path, extra_args: list[str] | None = None) -> Path:
    """Create a snapshot and return the tarball path. Caller must set KIROCREW_HOME.

    ``unpinnable_argv()`` is appended, not optional: on a platform with no directory
    descriptors ``_staging_is_pinned`` refuses instead of falling back, so without it every
    consumer of this helper dies in the helper itself and reports as a fixture ERROR rather
    than as its own subject failing. Empty where pinning works, so Linux still exercises the
    pinned path.
    """
    args = [str(out)] + (extra_args or []) + unpinnable_argv()
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
        assert m["version"] == 3
        # v3 is additive over v2 — every v2 key is still present, so a restore built
        # before the purpose seam reads a v3 bundle correctly instead of refusing it.
        for v2_key in (
            "created_at",
            "hostname",
            "user",
            "kirocrew_dir",
            "contents",
        ):
            assert v2_key in m, v2_key
        assert m["purpose"] == "backup"
        assert m["components"]["memory"] == "unresolved"
        assert m["components"]["config"] == "unresolved"

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
        snapshot_main([str(out2), "--keep", "2"] + unpinnable_argv())
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
        ret = restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv())
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
        restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv())
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
        restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv())
        backups = [
            d for d in existing.iterdir() if d.is_dir() and d.name.startswith("pre-restore-")
        ]
        assert backups
        assert (backups[0] / "workspace/local_only.md").is_file()

    @requires_symlinks
    def test_replace_swaps_nothing_when_a_tree_backup_refuses(self, env, monkeypatch):
        """Ordering ratchet for issue #2844, failure mode 3.

        The ENTIRE rollback set must exist before the first core-file swap. A
        tree backup can refuse through its fatal skip reporter (a symlink in
        the live tree is the injectable case), and that refusal must arrive
        with every live core file untouched -- the old ordering swapped the
        databases first, so the abort left mixed state (new databases, old
        trees) behind an incomplete rollback set.
        """
        _, _, tarball, tmp_path = env
        existing = tmp_path / "existing2844"
        _setup_fake_kirocrew(existing)
        # Make the live core files byte-distinguishable from the snapshot's, so
        # "unchanged" below cannot pass by the two sides being identical.
        conn = sqlite3.connect(str(existing / "memory.db"))
        conn.execute(
            "INSERT INTO semantic_memory"
            " (key, value_json, confidence, source, created_at, updated_at)"
            " VALUES ('local.only', '\"survivor\"', 0.9, 'test', '2026-01-02', '2026-01-02')"
        )
        conn.commit()
        conn.close()
        (existing / "crons.json").write_text('{"version": 2, "jobs": []}')
        # A symlink inside the live workspace is an entry the pinned backup walk
        # skips, and the backup pass reports skips through fatal_skip_reporter,
        # which refuses the whole replace.
        os.symlink(str(existing / "workspace/doc.md"), str(existing / "workspace/alias.md"))
        before_db = (existing / "memory.db").read_bytes()
        before_crons = (existing / "crons.json").read_bytes()
        monkeypatch.setenv("KIROCREW_HOME", str(existing))

        ret = restore_main([str(tarball), "--mode", "replace", "--force"])

        assert ret == 1
        assert (existing / "memory.db").read_bytes() == before_db
        assert (existing / "crons.json").read_bytes() == before_crons


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
        ret = restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv())
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

    def test_a_refused_notification_record_exits_1_instead_of_tracebacking(
        self, tmp_path, monkeypatch, capsys
    ):
        """The CLI must report a refusal, not crash with it.

        ``_merge_notifications`` aborts on a record it cannot deliver intact so a
        partial copy is never reported as success. That refusal had nowhere to
        land on this path: ``restore_main`` contains a ``try`` whose whole stated
        purpose is that "a traceback would read like a crash and bury the one
        sentence saying what to do about it", and ``UnreadableRecord`` was simply
        missing from its list of arms.

        Drives the real command end to end rather than calling the merge
        directly, because the defect was entirely in what the BOUNDARY does with
        the exception -- a unit test on the merge passes either way.
        """
        src, out, dst = tmp_path / "src9", tmp_path / "out9", tmp_path / "dst9"
        _setup_fake_kirocrew(src)
        # Both sides must have the file or the merge is never reached: a missing
        # destination takes the copy branch instead.
        (src / "notifications.jsonl").write_bytes(b'{"ts":1,"msg":"from snapshot"}\n')
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        tarball = _make_snapshot(src, out)

        _setup_fake_kirocrew(dst)
        # 0xff is invalid UTF-8 anywhere, which is how a real file gets here.
        (dst / "notifications.jsonl").write_bytes(b'{"ts":2,"msg":"\xff"}\n')
        monkeypatch.setenv("KIROCREW_HOME", str(dst))

        ret = restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv())

        assert ret == 1, "a refusal must be an exit code, not an exception"
        out_text = capsys.readouterr().out
        assert "not valid UTF-8" in out_text, "the refusal must say WHY"
        assert out_text.count("❌") >= 1, "reported through the same channel as its neighbours"

    def test_merge_cron_dedup(self, env, monkeypatch):
        """TEST 8"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst8"
        _setup_fake_kirocrew(dst)
        before = len(json.loads((dst / "crons.json").read_text(encoding="utf-8"))["jobs"])
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv())
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
        restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv())
        count = len(json.loads((dst / "crons.json").read_text(encoding="utf-8"))["jobs"])
        assert count == 2

    def test_merge_malformed_snapshot_crons_skips_without_changing_local_file(
        self, env, capsys, monkeypatch
    ):
        src, _, _, tmp_path = env
        (src / "crons.json").write_text("{malformed", encoding="utf-8")
        tarball = _make_snapshot(src, tmp_path / "malformed-snapshot-out")
        dst = tmp_path / "dst_malformed_snapshot_crons"
        _setup_fake_kirocrew(dst)
        before = (dst / "crons.json").read_bytes()

        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main(
            [str(tarball), "--mode", "merge", "--components", "crons", "--force"]
            + unpinnable_argv()
        )

        assert ret == 0
        assert (dst / "crons.json").read_bytes() == before
        output = capsys.readouterr().out
        assert "crons.json" in output
        assert "skipping cron merge" in output

    def test_merge_malformed_local_crons_skips_without_changing_local_file(
        self, env, capsys, monkeypatch
    ):
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst_malformed_local_crons"
        _setup_fake_kirocrew(dst)
        malformed = b"{malformed"
        (dst / "crons.json").write_bytes(malformed)

        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main(
            [str(tarball), "--mode", "merge", "--components", "crons", "--force"]
            + unpinnable_argv()
        )

        assert ret == 0
        assert (dst / "crons.json").read_bytes() == malformed
        output = capsys.readouterr().out
        assert str(dst / "crons.json") in output
        assert "skipping cron merge" in output

    def test_merge_workspace_no_overwrite(self, env, monkeypatch):
        """TEST 10"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst10"
        _setup_fake_kirocrew(dst)
        (dst / "workspace/doc.md").write_text("local version")
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv())
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
        ret = restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv())
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
        restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv())
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
        restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv())
        assert "Semantic Memory imported: 1" in capsys.readouterr().out

    def test_merge_notifications(self, env, monkeypatch):
        """TEST 14"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst14"
        _setup_fake_kirocrew(dst)
        (dst / "notifications.jsonl").write_text('{"ts":"2026-02-01","msg":"local"}\n')
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv())
        lines = (dst / "notifications.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2

    def test_merge_plan_memory(self, env, monkeypatch):
        """TEST 15"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst15"
        _setup_fake_kirocrew(dst)
        (dst / "plan_memory/local_plan.json").write_text("local plan")
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        ret = restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv())
        assert ret == 0
        assert (dst / "plan_memory/plan1.json").is_file()
        assert (dst / "plan_memory/local_plan.json").read_text(encoding="utf-8") == "local plan"

    def test_merge_still_imports_other_components_after_a_crons_refusal(
        self, tmp_path, monkeypatch
    ):
        """One unreadable component must not abort the whole merge."""
        src = tmp_path / "src-partial"
        _setup_fake_kirocrew(src)
        (src / "crons.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        tarball = _make_snapshot(src, tmp_path / "out-partial")

        dst = tmp_path / "dst-partial"
        _setup_fake_kirocrew(dst)
        (dst / "telemetry_salt").unlink()
        monkeypatch.setenv("KIROCREW_HOME", str(dst))

        assert restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv()) == 0
        assert (dst / "telemetry_salt").is_file()

    def test_merge_refuses_a_crons_file_that_is_not_an_object(self, env, capsys, monkeypatch):
        """Valid JSON is not a valid cron file; `jobs` is looked up on it."""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst-list-crons"
        _setup_fake_kirocrew(dst)
        (dst / "crons.json").write_text('["not", "a", "cron file"]', encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(dst))

        ret = restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv())

        assert ret == 0
        assert "skipping" in capsys.readouterr().out.lower()

    @pytest.mark.parametrize(
        "body",
        [
            '{"jobs": null}',  # present but not iterable
            '{"jobs": "not-a-list"}',  # iterable, but of characters
            '{"jobs": [123]}',  # a list whose entries have no .get
            '{"jobs": [{"name": []}]}',  # a present name must be hashable text
            '{"jobs": [{"name": "\\ud800"}]}',  # a lone surrogate cannot be UTF-8 encoded
        ],
    )
    def test_merge_refuses_a_crons_file_whose_jobs_are_the_wrong_shape(
        self, env, capsys, monkeypatch, body
    ):
        """The merge reads each job and hashes present names, so the shape it
        relies on has to hold before it starts."""
        _, _, tarball, tmp_path = env
        dst = tmp_path / f"dst-shape-{abs(hash(body))}"
        _setup_fake_kirocrew(dst)
        (dst / "crons.json").write_text(body, encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(dst))

        ret = restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv())

        assert ret == 0
        assert "skipping" in capsys.readouterr().out.lower()
        assert (dst / "crons.json").read_text(encoding="utf-8") == body

    def test_merge_refuses_an_incoming_crons_file_with_a_non_object_job(
        self, tmp_path, capsys, monkeypatch
    ):
        """Same contract on the incoming side."""
        src = tmp_path / "src-bad-job"
        _setup_fake_kirocrew(src)
        (src / "crons.json").write_text('{"jobs": [123]}', encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        tarball = _make_snapshot(src, tmp_path / "out-bad-job")

        dst = tmp_path / "dst-bad-job"
        _setup_fake_kirocrew(dst)
        keep = (dst / "crons.json").read_text(encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(dst))

        ret = restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv())

        assert ret == 0
        assert "skipping" in capsys.readouterr().out.lower()
        assert (dst / "crons.json").read_text(encoding="utf-8") == keep

    def test_merge_treats_a_missing_jobs_key_as_empty(self, env, monkeypatch):
        """Preservation: absent `jobs` already meant "no jobs" and still does."""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst-no-jobs-key"
        _setup_fake_kirocrew(dst)
        (dst / "crons.json").write_text('{"version": 1}', encoding="utf-8")
        monkeypatch.setenv("KIROCREW_HOME", str(dst))

        assert restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv()) == 0

        merged = json.loads((dst / "crons.json").read_text(encoding="utf-8"))
        assert len(merged["jobs"]) == 1

    def test_merge_still_imports_a_well_formed_crons_file(self, env, monkeypatch):
        """Preservation: the guard must not change the ordinary merge."""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst-good-crons"
        _setup_fake_kirocrew(dst)
        d = json.loads((dst / "crons.json").read_text(encoding="utf-8"))
        d["jobs"][0]["name"] = "different-job"
        (dst / "crons.json").write_text(json.dumps(d))
        monkeypatch.setenv("KIROCREW_HOME", str(dst))

        restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv())

        assert len(json.loads((dst / "crons.json").read_text(encoding="utf-8"))["jobs"]) == 2

    def test_merge_restores_missing_security(self, env, capsys, monkeypatch):
        """TEST 16"""
        _, _, tarball, tmp_path = env
        dst = tmp_path / "dst16"
        _setup_fake_kirocrew(dst)
        (dst / "telemetry_salt").unlink()
        monkeypatch.setenv("KIROCREW_HOME", str(dst))
        restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv())
        assert (dst / "telemetry_salt").is_file()
        assert "telemetry_salt: restored" in capsys.readouterr().out

    def test_merge_fresh_copies_memory(self, env, capsys, monkeypatch):
        """TEST 26"""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh26"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        restore_main(
            [str(tarball), "--mode", "merge", "--components", "memory", "--force"]
            + unpinnable_argv()
        )
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
        restore_main(
            [str(tarball), "--mode", "merge", "--components", "notifications", "--force"]
            + unpinnable_argv()
        )
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
        restore_main(
            [str(tarball), "--mode", "replace", "--components", "memory", "--force"]
            + unpinnable_argv()
        )
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
        restore_main(
            [str(tarball), "--mode", "replace", "--components", "crons,skills", "--force"]
            + unpinnable_argv()
        )
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
        restore_main(
            [str(tarball), "--mode", "merge", "--components", "crons", "--force"]
            + unpinnable_argv()
        )
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
        out = capsys.readouterr().out
        # The refusal must name the offending component and the known set, so the
        # operator can fix the invocation without reading the source.
        assert "unknown component" in out.lower()
        assert "bogus" in out
        assert "memory" in out

    def test_all_components(self, env, monkeypatch):
        """TEST 23"""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh23"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv())
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
        restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv())
        assert "integrity: OK" in capsys.readouterr().out

    def test_fts_missing_warning(self, env, capsys, monkeypatch):
        """TEST 31"""
        _, _, tarball, tmp_path = env
        fresh = tmp_path / "fresh31"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        restore_main(
            [str(tarball), "--mode", "replace", "--components", "memory", "--force"]
            + unpinnable_argv()
        )
        capsys.readouterr()  # discard first call's output
        # Remove index db
        (fresh / "memory_index.db").unlink(missing_ok=True)
        # Re-run merge to trigger warning
        restore_main(
            [str(tarball), "--mode", "merge", "--components", "memory", "--force"]
            + unpinnable_argv()
        )
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
        ret = restore_main([str(evil_tar), "--mode", "replace", "--force"] + unpinnable_argv())
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
        ret = restore_main([str(evil_tar), "--mode", "replace", "--force"] + unpinnable_argv())
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
        ret = restore_main([str(evil_tar), "--mode", "replace", "--force"] + unpinnable_argv())
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
        ret = restore_main([str(evil_tar), "--mode", "replace", "--force"] + unpinnable_argv())
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
        ns = argparse.Namespace(
            output_dir=str(out),
            keep=7,
            list_snapshots=False,
            allow_unpinned=bool(unpinnable_argv()),
        )
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
            allow_unpinned=bool(unpinnable_argv()),
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
        ret = restore_main([str(tarball), "--mode", "merge", "--force"] + unpinnable_argv())
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
        ret = restore_main([str(corrupt_tar), "--mode", "merge", "--force"] + unpinnable_argv())
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
        ret = restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv())
        assert ret == 0


class TestEmptyKirocrewDir:
    def test_snapshot_empty_dir(self, tmp_path, monkeypatch):
        """Snapshot succeeds on an empty ~/.kirocrew directory."""
        empty = tmp_path / "empty_mc"
        empty.mkdir()
        out = tmp_path / "empty_out"
        monkeypatch.setenv("KIROCREW_HOME", str(empty))
        ret = snapshot_main([str(out)] + unpinnable_argv())
        assert ret == 0
        assert list(out.glob("kirocrew-snapshot-*.tar.gz"))


class TestConcurrentSnapshot:
    def test_concurrent_snapshots_unique(self, env, monkeypatch):
        """Two rapid snapshots produce distinct files."""
        src, _, _, tmp_path = env
        out = tmp_path / "concurrent_out"
        out.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(src))
        snapshot_main([str(out)] + unpinnable_argv())
        # Ensure different timestamp by creating a second one
        import time

        time.sleep(1.1)
        snapshot_main([str(out)] + unpinnable_argv())
        tarballs = list(out.glob("kirocrew-snapshot-*.tar.gz"))
        assert len(tarballs) == 2
        assert tarballs[0].name != tarballs[1].name


class TestTheArchiveIsLockedDownBeforeItIsPublished:
    """The snapshot tarball can contain ``sel_hmac.key``, so it is secret-bearing.

    It was built at a ``.tmp`` sibling, renamed into place, and only then locked
    down — so between the rename and the lockdown the archive sat at its final,
    predictable path under whatever the destination directory gave it. Unlike
    the other writers in this family that window is not Windows-only: ``tarfile``
    does not create its file ``0600``, so on POSIX the archive is readable at the
    final path until the ``chmod`` lands too.

    Locking the temp down before the rename closes it on both platforms, and
    makes the "abort rather than ship an under-protected archive" promise in the
    code's own comment true by construction: a failure now happens before there
    is anything published to take back.
    """

    def test_the_lockdown_runs_before_the_archive_is_published(self, tmp_path, monkeypatch):
        from kiro_crew import platform_compat

        src = tmp_path / "src"
        out = tmp_path / "out"
        _setup_fake_kirocrew(src)
        monkeypatch.setenv("KIROCREW_HOME", str(src))

        real = platform_compat.restrict_to_owner
        locked: list[Path] = []

        def _recording(path):
            locked.append(Path(path))
            return real(path)

        monkeypatch.setattr("kiro_crew.platform_compat.restrict_to_owner", _recording)
        snapshot_main([str(out)] + unpinnable_argv())

        published = sorted(out.glob("kirocrew-snapshot-*.tar.gz"))
        assert published, "no snapshot was produced"
        archive_locks = [p for p in locked if p.parent == out]
        assert archive_locks, "the snapshot archive was never locked down"
        assert not [p for p in archive_locks if p in published], (
            "the archive was locked down AFTER it was published at its final "
            f"path, leaving a secret-bearing tarball readable first: {archive_locks}"
        )

    def test_a_failed_lockdown_publishes_no_archive(self, tmp_path, monkeypatch):
        """The comment promises an abort; nothing may be left at the final path."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        _setup_fake_kirocrew(src)
        monkeypatch.setenv("KIROCREW_HOME", str(src))

        monkeypatch.setattr(
            "kiro_crew.platform_compat.restrict_to_owner",
            lambda path: (_ for _ in ()).throw(OSError("icacls: transient failure")),
        )

        with pytest.raises(OSError):
            snapshot_main([str(out)] + unpinnable_argv())

        assert not list(
            out.glob("kirocrew-snapshot-*.tar.gz")
        ), "an archive whose lockdown failed was left at its final path"
        assert not list(out.glob("*.tmp")), "the temp archive was not cleaned up"

    def test_a_successful_snapshot_is_still_owner_only(self, tmp_path, monkeypatch):
        """Preservation: the permission the lockdown exists to apply still lands."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        _setup_fake_kirocrew(src)
        monkeypatch.setenv("KIROCREW_HOME", str(src))

        snapshot_main([str(out)] + unpinnable_argv())
        tarball = sorted(out.glob("kirocrew-snapshot-*.tar.gz"))[0]
        assert tarball.is_file()
        if os.name == "posix":
            assert tarball.stat().st_mode & 0o777 == 0o600, oct(tarball.stat().st_mode)


class TestMergeRestoreLocksBeforePublish:
    """#5346: merge restore of a missing security file must lock the temp first.

    Merge only copies when the destination is absent, so a restrict failure
    must leave that name uncreated rather than unlinking a published secret.
    """

    _SALT = b"s" * 32

    def test_restrict_runs_on_the_temp_not_the_published_path(self, tmp_path, monkeypatch):
        snap = tmp_path / "snap"
        home = tmp_path / "home"
        snap.mkdir()
        home.mkdir()
        (snap / "telemetry_salt").write_bytes(self._SALT)

        locked: list[Path] = []
        dest = home / "telemetry_salt"
        real = snapshot_mod.platform_compat.restrict_to_owner

        def _recording(path):
            locked.append(Path(path))
            assert not dest.exists(), "payload was published before the temp was locked"
            return real(path)

        monkeypatch.setattr(snapshot_mod.platform_compat, "restrict_to_owner", _recording)
        snapshot_mod._do_merge(snap, home, ["security"], allow_unpinned=bool(unpinnable_argv()))

        assert dest.is_file()
        assert dest.read_bytes() == self._SALT
        assert locked, "restrict_to_owner was never called"
        assert dest not in locked
        if os.name == "posix":
            assert dest.stat().st_mode & 0o777 == 0o600

    def test_a_failed_lockdown_leaves_the_destination_uncreated(self, tmp_path, monkeypatch):
        snap = tmp_path / "snap"
        home = tmp_path / "home"
        snap.mkdir()
        home.mkdir()
        (snap / "telemetry_salt").write_bytes(self._SALT)

        monkeypatch.setattr(
            snapshot_mod.platform_compat,
            "restrict_to_owner",
            lambda path: (_ for _ in ()).throw(OSError("icacls: transient failure")),
        )
        snapshot_mod._do_merge(snap, home, ["security"], allow_unpinned=bool(unpinnable_argv()))

        assert not (home / "telemetry_salt").exists()
        assert not list(home.glob("*.tmp"))

    def test_an_existing_dest_is_not_overwritten(self, tmp_path):
        src = tmp_path / "from-archive"
        dst = tmp_path / "telemetry_salt"
        src.write_bytes(self._SALT)
        dst.write_bytes(b"live")
        snapshot_mod._copy_locked(src, dst)
        assert dst.read_bytes() == b"live"

    def test_an_oversized_source_is_refused_before_publish(self, tmp_path):
        src = tmp_path / "from-archive"
        dst = tmp_path / "telemetry_salt"
        src.write_bytes(b"x" * 33)
        assert snapshot_mod._copy_locked(src, dst) is False
        assert not dst.exists()

    def test_an_oversized_salt_does_not_abort_merge(self, tmp_path):
        snap = tmp_path / "snap"
        home = tmp_path / "home"
        snap.mkdir()
        home.mkdir()
        (snap / "telemetry_salt").write_bytes(b"x" * 33)
        snapshot_mod._do_merge(snap, home, ["security"], allow_unpinned=bool(unpinnable_argv()))
        assert not (home / "telemetry_salt").exists()

    def test_a_dest_created_before_link_is_not_clobbered(self, tmp_path, monkeypatch):
        src = tmp_path / "from-archive"
        dst = tmp_path / "telemetry_salt"
        src.write_bytes(self._SALT)
        real_link = os.link

        def _link(source, dest):
            Path(dest).write_bytes(b"live")
            return real_link(source, dest)

        monkeypatch.setattr(snapshot_mod.os, "link", _link)
        snapshot_mod._copy_locked(src, dst)
        assert dst.read_bytes() == b"live"

    def test_a_hardlink_failure_does_not_abort_merge(self, tmp_path, monkeypatch):
        snap = tmp_path / "snap"
        home = tmp_path / "home"
        snap.mkdir()
        home.mkdir()
        (snap / "telemetry_salt").write_bytes(self._SALT)

        def _link(_source, _dest):
            raise OSError("Invalid cross-device link")

        monkeypatch.setattr(snapshot_mod.os, "link", _link)
        snapshot_mod._do_merge(snap, home, ["security"], allow_unpinned=bool(unpinnable_argv()))
        assert not (home / "telemetry_salt").exists()

    def test_a_failed_close_does_not_abort_merge(self, tmp_path, monkeypatch):
        snap = tmp_path / "snap"
        home = tmp_path / "home"
        snap.mkdir()
        home.mkdir()
        (snap / "telemetry_salt").write_bytes(self._SALT)

        real_close = os.close
        fired = False

        def _close(fdnum):
            nonlocal fired
            real_close(fdnum)
            if fired:
                return
            fired = True
            raise OSError("close: delayed writeback")

        monkeypatch.setattr(snapshot_mod.os, "close", _close)
        snapshot_mod._do_merge(snap, home, ["security"], allow_unpinned=bool(unpinnable_argv()))
        assert not (home / "telemetry_salt").exists()


class TestNotificationMergeWriteSideContract:
    """The notification merge copies records, so its bytes must be valid FOR THE DESTINATION.

    Every fixture here is real BYTES written to a real file. A synthesized
    ``UnicodeDecodeError`` would route the merge down a healthy path and prove
    nothing: the defect was that the decode happened at ``for line in f``,
    OUTSIDE the ``try``, so the failure never took the branch a fake exception
    would have taken.
    """

    LIVE = b'{"ts":"2026-02-01T00:00:00Z","msg":"local"}\n'
    GOOD = b'{"ts":"2026-03-01T00:00:00Z","msg":"snap"}\n'
    # 0xff is invalid UTF-8 anywhere; a truncated multi-byte lead is the
    # ordinary way a real file gets there.
    BAD_UTF8 = b'{"ts":"2026-03-02T00:00:00Z","msg":"\xff"}\n'

    def _files(self, tmp_path, src_bytes: bytes, dst_bytes: bytes):
        src = tmp_path / "snap-notifications.jsonl"
        dst = tmp_path / "live-notifications.jsonl"
        src.write_bytes(src_bytes)
        dst.write_bytes(dst_bytes)
        return src, dst

    @staticmethod
    def _merge_must_not_abort(src, dst) -> None:
        """Call the merge, turning an escaping exception into a NAMED failure.

        Several properties here are "a record of this shape must not abort the
        restore", and the pre-fix behaviour was an escaping exception. Letting it
        surface raw would report the red as an incidental error; ``pytest.fail``
        makes the red say which property broke.
        """
        try:
            snapshot_mod._merge_notifications(src, dst)
        except Exception as exc:  # noqa: BLE001 — the subject is that it does not
            pytest.fail(f"the merge aborted instead of handling the record: {exc!r}")

    # ── encoding ──────────────────────────────────────────────────────────

    def test_an_invalid_utf8_source_record_never_reaches_the_live_file(self, tmp_path, capsys):
        """The whole point: the live file must stay loadable.

        Its loader decodes the WHOLE file and returns no rows at all on one bad
        byte, after which the next rewrite persists that empty view -- so a
        single appended bad record costs every record that was already there.
        """
        src, dst = self._files(tmp_path, self.GOOD + self.BAD_UTF8, self.LIVE)
        with pytest.raises(UndecodableRecord):
            snapshot_mod._merge_notifications(src, dst)
        after = dst.read_bytes()
        assert b"\xff" not in after
        after.decode("utf-8")  # the property that matters: still loadable
        out = capsys.readouterr().out
        assert "Notifications imported:" not in out, "reported success on a failed merge"
        assert "not valid UTF-8" in out

    def test_the_failure_reaches_the_caller_and_is_not_only_printed(self, tmp_path):
        """A warn-and-return would tell an API caller the import succeeded.

        ``apply_import_zip`` appends ``notifications (merged)`` to its summary
        unconditionally, and the dashboard handler answers ``ok: True`` with a
        SEL ``outcome="ok"``. Neither sees stdout. So the merge has to raise, or
        an import that left records behind is reported as one that did not.
        Pinned separately from the byte-level assertions because it is a
        contract with the CALLER, not with the file.
        """
        for label, src_bytes, dst_bytes in (
            ("source", self.GOOD + self.BAD_UTF8, self.LIVE),
            ("destination", self.GOOD, self.LIVE + self.BAD_UTF8),
        ):
            src, dst = self._files(tmp_path, src_bytes, dst_bytes)
            with pytest.raises(UnreadableRecord):
                snapshot_mod._merge_notifications(src, dst)
            assert True, label

    def test_an_invalid_utf8_record_already_in_the_live_file_is_a_true_no_op(
        self, tmp_path, capsys
    ):
        """The destination scan is its own failure domain and must write nothing.

        This is the site the issue's own criterion required and did not name.
        The scan used to run in text mode too, so pre-existing live corruption
        aborted the merge with a traceback before the copy loop was reached; it
        still aborts, but now with a named reason and without having touched the
        destination.
        """
        src, dst = self._files(tmp_path, self.GOOD, self.LIVE + self.BAD_UTF8)
        before = dst.read_bytes()
        with pytest.raises(UndecodableRecord):
            snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == before, "destination-scan failure was not a no-op"
        out = capsys.readouterr().out
        assert "Notifications imported:" not in out
        assert "merge aborted" in out

    def test_the_success_line_is_printed_exactly_once(self, tmp_path, capsys):
        """A count, not a membership test, because a duplicate is invisible to `in`.

        The restructure that added the raising posture left the pre-existing
        success print in place below the new one, so the CLI reported the import
        count twice. Every other assertion here spells the check
        ``"Notifications imported:" in out``, which passes just as happily on two
        copies as on one -- and no linter or type check sees a doubled print
        either. Found by the First Principles lane, pinned here.
        """
        src, dst = self._files(tmp_path, self.GOOD, self.LIVE)
        snapshot_mod._merge_notifications(src, dst)
        out = capsys.readouterr().out
        assert out.count("Notifications imported:") == 1, f"success line not printed once:\n{out}"

    # ── boundary and byte-exactness ───────────────────────────────────────

    def test_a_crlf_terminated_record_keeps_its_carriage_return(self, tmp_path):
        """Row D of the measurement: text mode DROPPED the ``\\r``.

        Universal newlines translated ``\\r\\n`` to ``\\n`` on read and
        ``os.linesep`` put back only ``\\n`` on write, so the appended record
        was one byte shorter than the record on disk. This fires on a pure UTF-8
        host with fully valid UTF-8 input, which is why an explicit
        ``encoding=`` does not address it -- ``newline=`` is a separate axis.
        """
        src_bytes = b'{"ts":"2026-03-03T00:00:00Z","msg":"crlf"}\r\n'
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE + src_bytes

    def test_a_record_holding_a_bare_carriage_return_is_not_silently_lost(self, tmp_path, capsys):
        """Row E: the strongest demonstration, and it needs no bad encoding.

        A VALID UTF-8 record containing a bare ``\\r`` was split by universal
        newlines, both halves then failed ``json.loads``, the
        ``except (ValueError, TypeError): pass`` swallowed both, and the merge
        printed ``imported: 0`` and returned success. The record was gone from
        the live file permanently. Data loss on the default configuration.
        """
        src_bytes = b'{"ts":"2026-03-04T00:00:00Z","msg":"a\rb"}\n'
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        self._merge_must_not_abort(src, dst)
        assert dst.read_bytes() == self.LIVE + src_bytes
        assert "Notifications imported: 0" not in capsys.readouterr().out

    # ── framing ───────────────────────────────────────────────────────────

    def test_an_unterminated_live_record_gains_a_terminator_before_any_append(self, tmp_path):
        """A crash mid-append leaves the live file without a final terminator.

        Appending onto it glued two records into one line that parses as
        neither, so both were lost to the loader.
        """
        unterminated = b'{"ts":"2026-02-02T00:00:00Z","msg":"torn"}'
        src, dst = self._files(tmp_path, self.GOOD, unterminated)
        snapshot_mod._merge_notifications(src, dst)
        after = dst.read_bytes()
        assert after == unterminated + b"\n" + self.GOOD
        rows = [json.loads(line) for line in after.splitlines() if line.strip()]
        assert [r["msg"] for r in rows] == ["torn", "snap"]

    def test_an_unterminated_source_record_is_terminated_as_it_is_appended(self, tmp_path):
        """Otherwise the NEXT append glues onto it, moving the same defect."""
        src, dst = self._files(tmp_path, b'{"ts":"2026-03-05T00:00:00Z"}', self.LIVE)
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE + b'{"ts":"2026-03-05T00:00:00Z"}\n'

    # ── dedupe key type ───────────────────────────────────────────────────

    def test_a_non_object_record_does_not_abort_the_merge(self, tmp_path, capsys):
        """``json.loads(raw).get("ts")`` raised AttributeError on a JSON array.

        The record is unparseable AS A NOTIFICATION but its bytes are still
        history, so it is copied and keyed by its raw form -- not dropped, which
        would delete it.
        """
        src_bytes = b"[1, 2]\n" + self.GOOD
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        self._merge_must_not_abort(src, dst)
        assert dst.read_bytes() == self.LIVE + src_bytes
        assert "Notifications imported: 2" in capsys.readouterr().out

    def test_an_unhashable_ts_does_not_abort_the_merge(self, tmp_path, capsys):
        """A list or dict ``ts`` raised TypeError on set insert."""
        src_bytes = b'{"ts":[1,2],"msg":"weird"}\n' + b'{"ts":{"a":1},"msg":"weirder"}\n'
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        self._merge_must_not_abort(src, dst)
        assert dst.read_bytes() == self.LIVE + src_bytes
        assert "Notifications imported: 2" in capsys.readouterr().out

    def test_a_numeric_ts_still_deduplicates_on_its_value(self, tmp_path, capsys):
        """Keying only `str` would REGRESS against the predecessor.

        `json.loads(line).get("ts") or line.strip()` keyed a numeric ts on the
        number, so two rows carrying the same numeric ts with different bytes --
        one normalised, one not -- deduplicated. Falling back to the raw form for
        them instead persists a duplicate, which is the loss class this whole
        change is about. Found by the GPT lane.
        """
        live = b'{"ts":1767225600,"msg":"same row, live spelling"}\n'
        src_bytes = b'{"msg":"same row, snapshot spelling","ts":1767225600}\n'
        src, dst = self._files(tmp_path, src_bytes, live)
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == live, "a numeric ts stopped deduplicating"
        assert "Notifications imported: 0" in capsys.readouterr().out

    def test_an_int_and_an_equal_float_ts_deduplicate(self, tmp_path, capsys):
        """`1` and `1.0` are equal and hash equal, so they are ONE row.

        The kind tag is `"num"` for both, deliberately coarser than the Python
        type: tagging with `type(ts).__name__` split a row written as an integer
        on one side and a float on the other -- an ordinary serializer artefact
        -- into two records and persisted the duplicate. Found by the GPT lane.
        """
        live = b'{"ts":1767225600,"msg":"one row"}\n'
        src_bytes = b'{"ts":1767225600.0,"msg":"one row, float spelling"}\n'
        src, dst = self._files(tmp_path, src_bytes, live)
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == live, "an int and an equal float ts stopped deduplicating"
        assert "Notifications imported: 0" in capsys.readouterr().out

    def test_a_boolean_ts_does_not_collide_with_the_number_one(self, tmp_path, capsys):
        """`True == 1` and `hash(True) == hash(1)`, so `bool` is its own kind.

        Widening the predicate to any hashable `ts` is what exposes this: without
        a distinct tag these two records are one set member and the second is
        DELETED as a duplicate -- the loss class this change closes, re-created by
        the fix for it. `bool` is checked before the numeric arm because it is a
        subclass of `int`.
        """
        src_bytes = b'{"ts":true,"msg":"boolean"}\n{"ts":1,"msg":"number"}\n'
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE + src_bytes, "a boolean ts collided with 1"
        assert "Notifications imported: 2" in capsys.readouterr().out

    def test_a_fragment_of_a_split_record_is_not_skipped_against_a_truncated_live_row(
        self, tmp_path
    ):
        """Content is not identity, and using it as identity DELETES bytes.

        A crash mid-append leaves a truncated row in the live file. A source
        record holding a bare carriage return is split at it, because the
        reader's boundaries are the universal-newline set -- and the source's
        first piece can strip to exactly that truncated live row. Keyed on
        content, that piece was skipped as a duplicate while the second piece was
        appended, so the live file gained a dangling line and the source's
        carriage return was gone, while the merge printed success. Measured on
        these exact bytes. Found by the GPT lane.
        """
        live = b'{"ts":"2026-01-01T00:00:00Z","msg":"a\n'
        src_bytes = b'{"ts":"2026-01-01T00:00:00Z","msg":"a\rb"}\n'
        src, dst = self._files(tmp_path, src_bytes, live)
        snapshot_mod._merge_notifications(src, dst)
        after = dst.read_bytes()
        assert after == live + src_bytes, "a record fragment was skipped as a duplicate"
        assert b"\r" in after, "the source carriage return was lost"

    def test_two_distinct_records_with_no_ts_both_land(self, tmp_path, capsys):
        """The add-guards are what keep `None` out of the seen-set.

        If either `existing.add` accepted a `None` key, the FIRST identity-less
        record would seed it and every later one would match -- so a source
        holding several such rows would append one and silently drop the rest.
        Two rows are the smallest input that observes it; one row cannot.
        """
        src_bytes = b'{"msg":"first with no ts"}\n{"msg":"second with no ts"}\n'
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE + src_bytes, "an identity-less row was dropped"
        assert "Notifications imported: 2" in capsys.readouterr().out

    def test_a_parsing_ts_less_row_deduplicates_on_its_raw_bytes(self, tmp_path, capsys):
        """A ts-less row that PARSES is a whole record, so bytes are its identity.

        Rewritten rather than adjusted: the predecessor of this test pinned a
        re-append as declared behaviour, and that premise is gone. Keying such a
        row on its unstripped bytes restores idempotence for a re-run without
        restoring the deletion, because byte-equal records ARE the same record.
        """
        src_bytes = b'{"msg":"no ts at all"}\n'
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE + src_bytes
        assert "Notifications imported: 1" in capsys.readouterr().out
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE + src_bytes, "a re-run must NOT re-append it"
        assert "Notifications imported: 0" in capsys.readouterr().out

    def test_an_UNTERMINATED_ts_less_row_also_deduplicates_on_re_import(self, tmp_path, capsys):
        """The key must be the bytes that LAND, not the bytes that arrive.

        The test above uses a source row that already ends in a newline, so it
        cannot see this: an UNTERMINATED row is written with a terminator
        appended, so keying the arriving form makes a second import compare the
        source's unterminated bytes against the terminated row the first import
        itself wrote, miss, and append a second copy. Reproduced before the fix --
        two copies after two imports, with "imported: 1" both times.

        Asserted by COUNT rather than by membership, because membership is
        structurally blind to duplication, which is the whole defect here.
        """
        src_bytes = b'{"msg":"no ts, unterminated"}'  # deliberately no terminator
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        landed = self.LIVE + src_bytes + b"\n"

        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == landed
        assert "Notifications imported: 1" in capsys.readouterr().out

        snapshot_mod._merge_notifications(src, dst)
        after = dst.read_bytes()
        assert after.count(src_bytes) == 1, "the row must appear exactly ONCE"
        assert after == landed, "a re-import must append nothing"
        assert "Notifications imported: 0" in capsys.readouterr().out

    def test_two_parsed_rows_differing_ONLY_in_terminator_both_survive(self, tmp_path):
        """Pins the DIRECTION of the normalization, not merely its presence.

        Found by mutation: replacing the ``+ b"\\n"`` normalization with
        ``rstrip(b"\\r\\n")`` left the whole suite green, so nothing distinguished
        the safe direction from the predecessor's deleter. The existing
        strip-alike test cannot catch it -- its fragment is UNPARSEABLE, so it
        takes the ``None`` path and never reaches the raw key at all.

        These two rows PARSE, so both are byte-keyed. They land as distinct bytes
        (``...\\r`` is kept as-is, ``...`` gains ``\\n``), so both must survive.
        Under ``rstrip`` they collapse to one key and the second is deleted.
        """
        cr_row = b'{"msg":"same payload"}\r'
        bare_row = b'{"msg":"same payload"}'
        src, dst = self._files(tmp_path, cr_row + bare_row, self.LIVE)
        snapshot_mod._merge_notifications(src, dst)
        after = dst.read_bytes()
        assert after == self.LIVE + cr_row + bare_row + b"\n", "both rows must land"
        assert after.count(b'{"msg":"same payload"}') == 2, "neither may be skipped"

    def test_an_unterminated_DESTINATION_row_keeps_its_identity(self, tmp_path):
        """The destination side takes the same normalization, so the two agree.

        A crash leaves the live file's final record unterminated; the merge
        terminates it before appending. Its key therefore has to be the
        terminated form too, or the row it becomes stops matching the row it was
        and a later import of the same content duplicates it.
        """
        row = b'{"msg":"crash-truncated tail"}'
        src, dst = self._files(tmp_path, row + b"\n", self.LIVE + row)
        snapshot_mod._merge_notifications(src, dst)
        snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes().count(row) == 1, "one row, not two"

    def test_two_rows_that_only_STRIP_alike_both_survive(self, tmp_path):
        """Round 5's deletion, pinned against the key that caused it.

        A crash truncates a live row to ``b'{"a": "x'``; framing splits a source
        record at its bare carriage return, yielding ``b'{"a": "x\\r'``. Those two
        are NOT byte-equal but they strip alike, which is exactly how the
        predecessor deleted the source's bytes. Both must survive.
        """
        truncated = b'{"a": "x'
        src, dst = self._files(tmp_path, b'{"a": "x\ry"}\n', self.LIVE + truncated)
        snapshot_mod._merge_notifications(src, dst)
        after = dst.read_bytes()
        assert truncated in after, "the live truncated row must remain"
        assert b'{"a": "x\r' in after, "the source fragment must not be skipped"
        assert b'y"}' in after, "the source tail must be appended"

    def test_a_byte_key_can_never_collide_with_a_ts_key(self, tmp_path):
        """``json.loads`` cannot produce a value whose type is named ``raw``.

        The kind tag is what keeps the two key families apart, so this pins the
        claim the docstring makes rather than leaving it as prose.
        """
        k = lambda b: snapshot_mod._notification_key(b, tmp_path / "x.jsonl")  # noqa: E731

        # A HASHABLE ts always tags with its kind, never "raw" -- json.loads can
        # only yield str/int/float/bool here, so type(ts).__name__ cannot be "raw"
        # and the string "raw" as a VALUE tags as `str`, not as the byte family.
        for value in (b'{"ts": "raw"}', b'{"ts": 1}', b'{"ts": 1.0}', b'{"ts": true}'):
            key = k(value + b"\n")
            assert key is not None and key[0] != "raw", f"{value!r} collided"
        # An UNHASHABLE ts has no usable identity of its own, so it takes the byte
        # key. Safe for the same reason: byte-equal records are the same record.
        for value in (b'{"ts": [1]}', b'{"ts": {"n": 1}}'):
            key = k(value + b"\n")
            assert key is not None and key[0] == "raw", f"{value!r} should byte-key"
        # Parsed but no ts -> byte key. Unparseable -> no key at all.
        assert k(b'{"m":1}\n')[0] == "raw"
        assert k(b"not json\n") is None

    # ── bound ─────────────────────────────────────────────────────────────

    def test_an_over_cap_record_aborts_instead_of_being_skipped(
        self, tmp_path, capsys, monkeypatch
    ):
        """A skipped record here is a permanently deleted one, so it aborts.

        The cap is moved rather than writing a 128 MiB fixture; the read is a
        global lookup at call time for exactly that reason.
        """
        monkeypatch.setattr(snapshot_mod, "_NOTIFICATION_RECORD_CAP", 64)
        oversized = b'{"ts":"2026-03-06T00:00:00Z","msg":"' + b"x" * 200 + b'"}\n'
        src, dst = self._files(tmp_path, self.GOOD + oversized, self.LIVE)
        with pytest.raises(OversizedRecord):
            snapshot_mod._merge_notifications(src, dst)
        after = dst.read_bytes()
        assert b"x" * 200 not in after
        out = capsys.readouterr().out
        assert "Notifications imported:" not in out
        assert "record over 64 bytes" in out

    # ── failure posture ───────────────────────────────────────────────────

    def test_an_archive_derived_path_cannot_forge_terminal_output(self, tmp_path, capsys):
        """A bundle chooses its own inner root, so a path can carry ANSI controls.

        `_safe_name` exists for exactly this and is used at nineteen sites in this
        module; its docstring names archive root directories. These three prints
        are new code and were bypassing it, so a crafted root printed raw would
        move the cursor and overwrite lines right above the prompt where the
        operator decides whether to trust the restore. Found by the GPT lane.
        """
        hostile = tmp_path / "kirocrew-snapshot-\x1b[2K\x1b[1Aevil"
        hostile.mkdir()
        src = hostile / "notifications.jsonl"
        dst = tmp_path / "live.jsonl"
        src.write_bytes(self.BAD_UTF8)
        dst.write_bytes(self.LIVE)
        with pytest.raises(UndecodableRecord):
            snapshot_mod._merge_notifications(src, dst)
        out = capsys.readouterr().out
        assert "\x1b" not in out, f"a raw escape reached the terminal:\n{out!r}"
        assert "evil" in out, "the name was suppressed rather than escaped"

    def test_the_residual_copy_failure_also_escapes_its_archive_path(
        self, tmp_path, capsys, monkeypatch
    ):
        """The SECOND source print needs its own test or its sanitizer is untested.

        `_safe_name` guards two source prints, and a mutation of this one could not
        redden while only the pre-validation path had coverage. Reaching it needs
        the residual condition -- the source changing between the validation pass
        and the copy -- which cannot be produced by file content alone, so the
        reader is made to succeed once and refuse once.

        The injected refusal is legitimate HERE, unlike elsewhere in this class,
        because the property under test is that the print escapes its path. The
        exception is only the trigger that reaches the print; it is not the thing
        being verified, so it does not have to be a real undecodable byte.
        """
        real = snapshot_mod.strict_raw_records
        calls = {"n": 0}

        def flaky(handle, path, **kw):
            calls["n"] += 1
            if calls["n"] >= 3:  # 1 = destination scan, 2 = validation, 3 = the copy
                raise UnreadableRecord("source changed under the merge")
            yield from real(handle, path, **kw)

        hostile = tmp_path / "kirocrew-snapshot-\x1b[2K-forged"
        hostile.mkdir()
        src = hostile / "notifications.jsonl"
        dst = tmp_path / "live.jsonl"
        src.write_bytes(self.GOOD)
        dst.write_bytes(self.LIVE)
        monkeypatch.setattr(snapshot_mod, "strict_raw_records", flaky)
        with pytest.raises(UnreadableRecord):
            snapshot_mod._merge_notifications(src, dst)
        out = capsys.readouterr().out
        assert "Stopped merging" in out, f"the copy-loop print was not reached:\n{out!r}"
        assert "\x1b" not in out, f"a raw escape reached the terminal:\n{out!r}"
        assert "forged" in out, "the name was suppressed rather than escaped"

    def test_a_ts_less_row_before_a_bad_one_is_not_duplicated_by_a_retry(self, tmp_path, capsys):
        """The source is validated WHOLE before the destination is opened.

        An identity-less row cannot be deduplicated, by construction. So if a
        later record aborts the copy, the rows already appended include ones a
        retry has no way to recognise -- and the retry appends them again. The
        pre-validation pass is what removes the prefix entirely, which is the only
        fix that does not require identity for rows that have none. Found by the
        GPT lane.
        """
        src_bytes = b'{"msg":"no ts, would double"}\n' + self.BAD_UTF8
        src, dst = self._files(tmp_path, src_bytes, self.LIVE)
        for attempt in ("first", "retry"):
            with pytest.raises(UndecodableRecord):
                snapshot_mod._merge_notifications(src, dst)
            assert dst.read_bytes() == self.LIVE, f"{attempt}: a prefix was appended"
        out = capsys.readouterr().out
        assert "Notifications imported:" not in out
        assert "after 1 record(s)" not in out, "the copy loop ran despite a bad source"

    def test_a_bad_source_record_leaves_the_live_file_untouched(self, tmp_path, capsys):
        """A source-side refusal is a no-op now, not an abort with a prefix.

        Earlier revisions appended everything up to the bad record and kept it,
        relying on the seen-set rebuilt from the destination to make a re-run
        idempotent. That reasoning only holds for rows that HAVE identity, and
        identity-less rows are exactly the ones this function may not deduplicate,
        so the prefix was a retry-duplication hazard. Validating the whole source
        first removes it. A prefix can still survive the residual case -- the
        source changing between the two passes -- which is why the copy loop keeps
        its own handler.
        """
        src, dst = self._files(tmp_path, self.GOOD + self.BAD_UTF8, self.LIVE)
        with pytest.raises(UndecodableRecord):
            snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE, "a prefix was appended before the refusal"
        out = capsys.readouterr().out
        assert "merge aborted" in out
        assert "Notifications imported:" not in out
        with pytest.raises(UndecodableRecord):
            snapshot_mod._merge_notifications(src, dst)
        assert dst.read_bytes() == self.LIVE, "the retry appended something"


# ── Issue #8181: the copy branch installed unvalidated notification bytes ──────


class _NotificationCopyFixtures:
    """Shared fixtures for the copy branch, held apart from any skip marker.

    A mixin rather than a base test class: the copy-behaviour tests cannot run where
    ``O_NOFOLLOW`` is absent (the copy refuses outright there, by design), while the
    refusal tests must run EVERYWHERE. A skip on a shared base would be inherited by
    both, so the marker goes on one subclass and the fixtures live here.
    """

    GOOD = b'{"ts":"2026-03-01T00:00:00Z","msg":"snap"}\n'
    MORE = b'{"ts":"2026-03-02T00:00:00Z","msg":"snap2"}\n'
    BAD_UTF8 = b'{"ts":"2026-03-03T00:00:00Z","msg":"\xff"}\n'

    def _snap(self, tmp_path, src_bytes: bytes) -> tuple[Path, Path]:
        """An extracted-snapshot staging dir and a data home with no live file."""
        snap = tmp_path / "snap"
        home = tmp_path / "home"
        snap.mkdir(parents=True)
        home.mkdir(parents=True)
        (snap / "notifications.jsonl").write_bytes(src_bytes)
        return snap, home

    def _merge(self, snap: Path, home: Path) -> None:
        """Drive the real restore, so the CALL SITE is under test, not the helper."""
        snapshot_mod._do_merge(
            snap, home, ["notifications"], allow_unpinned=bool(unpinnable_argv())
        )


@requires_o_nofollow
class TestNotificationCopyWhenNoLiveFileExists(_NotificationCopyFixtures):
    """The OTHER branch of the same ``if``, which validated nothing.

    ``_merge_notifications`` runs when a live ``notifications.jsonl`` exists. When
    one does not -- a fresh install, a first restore -- the restore took
    ``shutil.copy2`` instead, so the same snapshot that ABORTED the restore in one
    case was installed silently in the other. A byte-exact copy is correct as a
    copy and that is the defect: it faithfully installs bytes the destination's own
    reader refuses, and that reader loses the WHOLE file to one of them.

    Every fixture is real bytes on a real file, for the reason the merge class
    states: a synthesized ``UnicodeDecodeError`` routes the copy down a healthy
    path and proves nothing. The undecodable record is measured as INSTALLED
    without the fix -- ``strict_raw_records`` frames and bounds records but does
    not decode, so framing alone reproduces ``copy2``'s behaviour exactly.
    """

    def test_an_undecodable_record_is_never_installed(self, tmp_path, capsys):
        """The whole point, and it must reach the CALLER, not only stdout.

        ``apply_import_zip`` appends ``notifications (copied)`` to its summary and
        the dashboard handler answers ``ok: True``; neither sees a print. So the
        posture is the merge branch's -- abort by raising -- and the success lines
        must not be reached.
        """
        snap, home = self._snap(tmp_path, self.GOOD + self.BAD_UTF8)
        with pytest.raises(UndecodableRecord):
            self._merge(snap, home)
        assert not (
            home / "notifications.jsonl"
        ).exists(), "a partially copied file was left where the reader will find it"
        out = capsys.readouterr().out
        assert "Notifications: copied" not in out, "reported success on a refused copy"
        assert "✅ notifications" not in out
        assert "not valid UTF-8" in out

    def test_the_reader_loads_every_record_the_copy_installs(self, tmp_path, monkeypatch):
        """The consequence, asserted through the reader that actually loses the file.

        This is the test that separates a real fix from a no-op. Framing the
        records without decoding them installs the bad byte just as ``copy2`` did,
        and every byte-level assertion above still passes; only loading the
        installed file through ``_load_notifications`` shows it. Measured on the
        unfixed branch: 0 rows from a file holding 2 valid records, then the next
        rewrite persists that empty view.
        """
        snap, home = self._snap(tmp_path, self.GOOD + self.MORE + self.BAD_UTF8)
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        with pytest.raises(UndecodableRecord):
            self._merge(snap, home)

        # Same source without the bad record: the copy must be fully loadable.
        clean, home2 = self._snap(tmp_path / "clean", self.GOOD + self.MORE)
        monkeypatch.setenv("KIROCREW_HOME", str(home2))
        self._merge(clean, home2)
        from kiro_crew.dashboard import state as dashboard_state

        assert len(dashboard_state._load_notifications()) == 2

    def test_a_clean_source_is_copied_record_for_record(self, tmp_path):
        """Byte-exactness is the property ``copy2`` had and the fix must keep.

        A ``\\r\\n`` terminator survives and a bare ``\\r`` inside the file ends a
        record without being rewritten -- the text-mode round trip that #7771
        removed from the merge is not reintroduced here.
        """
        src_bytes = b'{"ts":"1","msg":"a"}\r\n{"ts":"2","msg":"b"}\r{"ts":"3","msg":"c"}\n'
        snap, home = self._snap(tmp_path, src_bytes)
        self._merge(snap, home)
        assert (home / "notifications.jsonl").read_bytes() == src_bytes

    def test_an_unterminated_final_record_gains_a_terminator(self, tmp_path):
        """Otherwise the first notification after the restore glues onto it.

        ``_persist_notification`` appends ``json.dumps(note) + "\\n"``, so an
        unterminated last line plus that append is one line that parses as neither
        row. The merge branch makes the same repair through ``dst_unterminated``;
        writing record-wise makes it this branch's job too.
        """
        snap, home = self._snap(tmp_path, b'{"ts":"1","msg":"a"}\n{"ts":"2","msg":"b"}')
        self._merge(snap, home)
        installed = (home / "notifications.jsonl").read_bytes()
        assert installed.endswith(b"\n")
        assert installed == b'{"ts":"1","msg":"a"}\n{"ts":"2","msg":"b"}\n'

    def test_an_oversized_record_installs_nothing(self, tmp_path, monkeypatch):
        """The cap is a memory bound, and its refusal owes the same all-or-nothing.

        Pinned separately from the encoding case because it is a DIFFERENT
        exception out of the same reader, and an ``except`` narrowed to the
        encoding one would leave this reason escaping past the cleanup.
        """
        monkeypatch.setattr(snapshot_mod, "_NOTIFICATION_RECORD_CAP", 64)
        snap, home = self._snap(tmp_path, self.GOOD + b'{"msg":"' + b"x" * 200 + b'"}\n')
        with pytest.raises(OversizedRecord):
            self._merge(snap, home)
        assert not (home / "notifications.jsonl").exists()

    def test_a_concurrent_notification_is_not_deleted_by_the_rollback(self, tmp_path, monkeypatch):
        """A file this call did not create must survive its refusal.

        Review's finding, and the slip it names is one an exclusive create invites:
        creating the live file with ``O_EXCL`` proves this call CREATED it, not that
        it is the only thing that has since written to it. ``apply_import_zip`` runs
        inside the live gateway, so the dashboard's notification sink can append to
        a file the copy just created -- and the rollback for a later bad record then
        deleted that operator's notification along with the prefix.

        Now structural rather than defended: the whole source is validated before the
        destination is created, so the ordinary refusal has nothing to roll back. The
        delivery is injected at the validation of the record that aborts, which is
        the interleaving that broke the earlier revision. Measured on it, the live
        file and the delivered note are both gone.
        """
        snap, home = self._snap(tmp_path, self.GOOD + self.BAD_UTF8)
        live = home / "notifications.jsonl"
        delivered = b'{"ts":"2026-03-09T00:00:00Z","msg":"delivered during the restore"}\n'
        real_key = snapshot_mod._notification_key
        seen: list[bytes] = []

        def keyed(record, path):
            seen.append(record)
            if len(seen) == 2:
                # What `_persist_notification` does, in its own mode.
                with open(live, "a", encoding="utf-8") as f:
                    f.write(delivered.decode())
            return real_key(record, path)

        monkeypatch.setattr(snapshot_mod, "_notification_key", keyed)
        with pytest.raises(UndecodableRecord):
            self._merge(snap, home)
        assert live.is_file(), "the rollback deleted a file this call did not create"
        assert live.read_bytes() == delivered, "the delivered notification was altered or lost"

    def test_a_live_file_that_appears_mid_copy_is_not_replaced(self, tmp_path):
        """A clean source must not clobber a name that filled while it validated.

        The branch was chosen because no live file existed; by publish time one can.
        Replacing it would delete whatever the dashboard persisted in between, so
        the publish refuses and leaves the operator's file exactly as it found it.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        live = home / "notifications.jsonl"
        appeared = b'{"ts":"2026-03-09T00:00:00Z","msg":"appeared"}\n'
        live.write_bytes(appeared)
        with pytest.raises(FileExistsError):
            snapshot_mod._copy_notifications(snap / "notifications.jsonl", live)
        assert live.read_bytes() == appeared

    def test_the_data_home_gains_nothing_but_the_notifications_file(self, tmp_path):
        """No intermediate artifact, which is the second review finding's whole point.

        A temp file in the data home is published through a NAME, and a same-user
        process that can list the directory can swap what that name holds between
        the write and the publish. Writing straight to an ``O_EXCL`` destination
        needs no such name. Asserted as "the directory holds nothing else" rather
        than "no file called .tmp", so any future intermediate is caught whatever it
        is named.
        """
        snap, home = self._snap(tmp_path, self.GOOD + self.BAD_UTF8)
        with pytest.raises(UndecodableRecord):
            self._merge(snap, home)
        assert sorted(p.name for p in home.iterdir()) == []

        clean, home2 = self._snap(tmp_path / "clean", self.GOOD)
        self._merge(clean, home2)
        assert sorted(p.name for p in home2.iterdir()) == ["notifications.jsonl"]

    def test_the_installed_file_is_no_tighter_than_one_the_product_writes(self, tmp_path):
        """A restored file the dashboard cannot manage is its own outage.

        ``os.open`` takes an explicit mode, so this is a real choice and not a
        default: 0o666 lets the kernel apply the umask, which is what the plain
        ``open(path, "a")`` in ``_persist_notification`` gets.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        self._merge(snap, home)
        by_product = home / "written-by-the-product.jsonl"
        with open(by_product, "a", encoding="utf-8") as f:
            f.write('{"ts":"1"}\n')
        installed = (home / "notifications.jsonl").stat().st_mode & 0o777
        assert installed == by_product.stat().st_mode & 0o777, oct(installed)

    # ── read once: the precondition, not the consequence ──────────────────

    def test_a_source_truncated_after_the_read_cannot_affect_the_install(
        self, tmp_path, monkeypatch
    ):
        """Replaces a retired test, and the reversal is the point.

        The predecessor read the file twice and this scenario was its worst outcome: a
        source truncated between the passes gave pass 2 a clean EOF, so nothing raised,
        the success line printed, and the install was silently short -- measured at 5
        records validated, 2 installed. That test asserted the damage was BOUNDED.

        Reading once makes the scenario unrepresentable rather than bounded, so the
        same setup now asserts the opposite: truncating the source after it has been
        read changes nothing, because there is no name left to resolve and no handle
        left open. This pins the PRECONDITION -- one read -- and reddens on any design
        that goes back to the file, which "no partial install" would not, since that
        passes trivially once the window is gone.

        The truncation is injected at the first validation call, which is after the
        read and before the write.
        """
        snap, home = self._snap(tmp_path, self.GOOD + self.MORE)
        source = snap / "notifications.jsonl"
        real_key = snapshot_mod._notification_key
        fired: list[int] = []

        def keyed(record, path):
            if not fired:
                fired.append(1)
                source.write_bytes(b"")  # the source is gone from here on
            return real_key(record, path)

        monkeypatch.setattr(snapshot_mod, "_notification_key", keyed)
        self._merge(snap, home)

        assert fired, "the injection point never ran, so this test proved nothing"
        installed = (home / "notifications.jsonl").read_bytes()
        assert installed == self.GOOD + self.MORE, "the install followed the file, not the bytes"

    # ── the whole-file cap: the load-bearing addition ──────────────────────

    def test_a_source_over_the_cap_is_refused_with_its_size_named(self, tmp_path, monkeypatch):
        """Over-cap must REFUSE, loudly, and install nothing.

        A whole-file cap is what single-read needs and streaming did not: the
        per-record cap bounds one record, not a file made of many, and the source is
        archive-derived so nothing bounds it from outside. The failure mode to avoid is
        not a crash -- it is a cap that quietly drops the tail, which would rebuild the
        silent-partial-install defect this redesign removes, one layer up.

        The cap is patched small rather than writing 32 MiB to disk. The size must
        appear in the message so an operator can tell an over-cap refusal from a
        corrupt archive without reading code.
        """
        monkeypatch.setattr(snapshot_mod, "_NOTIFICATION_SOURCE_CAP", 64)
        oversized = self.GOOD * 4
        assert len(oversized) > 64
        snap, home = self._snap(tmp_path, oversized)

        with pytest.raises(OSError) as caught:
            self._merge(snap, home)
        message = str(caught.value)
        assert str(len(oversized)) in message, f"the size was not named: {message}"
        assert "64" in message, f"the limit was not named: {message}"
        assert not (
            home / "notifications.jsonl"
        ).exists(), "an over-cap source was partially installed instead of refused"

    def test_the_source_cap_is_the_value_this_design_was_measured_for(self):
        """A ratchet on the cap, because the cap is the part that can actually break.

        Every other property here is now structural -- there is no window to reopen --
        so the live risk is somebody raising this number for convenience on a large
        archive and reintroducing an unbounded hold. This assertion goes red on that
        day, which is the day it matters.

        32 MiB is ~4x the product-bounded worst case (400 records at the largest
        record observed on a live install, 8,316,000 bytes) and a quarter of the
        per-record cap this same file already accepts for ONE record. Peak held is
        about twice the cap. Raising it is a memory decision, so it should require
        editing a test that says so.
        """
        assert snapshot_mod._NOTIFICATION_SOURCE_CAP == 32 * 1024 * 1024
        assert snapshot_mod._NOTIFICATION_SOURCE_CAP < snapshot_mod._NOTIFICATION_RECORD_CAP

    # ── one resolution per path (the class behind three findings) ──────────

    @requires_symlinks
    def test_a_source_swapped_for_a_symlink_between_the_passes_is_not_followed(self, tmp_path):
        """The second by-name open was the finding; the descriptor is the fix.

        A running agent replaces the extracted notification file with a symlink to
        a credential after the first pass has validated it. The earlier revision
        opened the NAME again for the copy, followed the link, and wrote the secret
        into an agent-readable ``notifications.jsonl``. Reusing the held descriptor
        makes the swap invisible, so the assertion is that the archive's own bytes
        land and the secret does not -- not that the copy is refused.
        """
        snap, home = self._snap(tmp_path, self.GOOD + self.MORE)
        source = snap / "notifications.jsonl"
        secret = tmp_path / "dot-env"
        secret.write_bytes(b'{"ts":"1","msg":"AWS_SECRET_ACCESS_KEY=hunter2"}\n')
        real_key = snapshot_mod._notification_key
        seen: list[bytes] = []

        def keyed(record, path):
            seen.append(record)
            # Pass 1 has validated both records; swap before pass 2 would re-resolve.
            if len(seen) == 2:
                source.unlink()
                source.symlink_to(secret)
            return real_key(record, path)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(snapshot_mod, "_notification_key", keyed)
            self._merge(snap, home)

        installed = (home / "notifications.jsonl").read_bytes()
        assert b"hunter2" not in installed, "the swapped symlink was followed"
        assert installed == self.GOOD + self.MORE

    @requires_symlinks
    def test_a_source_that_is_already_a_symlink_is_refused(self, tmp_path):
        """``is_file()`` follows links, so the branch is entered on one.

        ``O_NOFOLLOW`` refuses at the open rather than reading the target, which is
        the same posture ``_backup_and_copy`` already takes for a symlinked file
        coming out of an archive.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        source = snap / "notifications.jsonl"
        secret = tmp_path / "dot-env"
        secret.write_bytes(b'{"ts":"1","msg":"AWS_SECRET_ACCESS_KEY=hunter2"}\n')
        source.unlink()
        source.symlink_to(secret)
        with pytest.raises(OSError):
            self._merge(snap, home)
        assert not (home / "notifications.jsonl").exists()

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
    def test_a_source_that_became_a_fifo_fails_instead_of_hanging(self, tmp_path):
        """Pins ``O_NONBLOCK``. Not a review finding -- found by auditing the span.

        The caller selected this branch on ``is_file()``, which is False for a FIFO,
        so reaching one means the name changed afterwards. Without ``O_NONBLOCK`` the
        open blocks forever waiting for a writer and the restore HANGS, which is
        worse than failing because a hang reports nothing at all. Dropping the flag
        makes this test TIME OUT rather than fail, and the timeout is the evidence.

        Calls the helper directly, since ``_do_merge`` cannot reach a FIFO through
        its own ``is_file()`` gate -- the hazard is the name changing after it.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        source = snap / "notifications.jsonl"
        source.unlink()
        os.mkfifo(source)
        with pytest.raises(OSError):
            snapshot_mod._copy_notifications(source, home / "notifications.jsonl")
        assert not (home / "notifications.jsonl").exists()

    @pytest.mark.skipif(not os.path.exists("/dev/null"), reason="needs a character device")
    def test_a_non_regular_source_is_refused_not_read_as_empty(self, tmp_path):
        """Pins the ``S_ISREG`` judgement on the descriptor, which ``seek`` does not.

        Written after the mutation harness showed the first version of this coverage
        was passing for the wrong reason: a FIFO is caught by ``seek`` failing on a
        pipe, so removing the ``S_ISREG`` check reddened nothing. A character device
        is the case ``seek`` cannot catch -- it is seekable, so without the check the
        read simply returns EOF, and the restore CREATES AN EMPTY
        ``notifications.jsonl`` and reports success. That is the outcome being
        refused: not a crash, a silent claim to have imported a history that is gone.
        """
        dst = tmp_path / "notifications.jsonl"
        with pytest.raises(OSError):
            snapshot_mod._copy_notifications(Path("/dev/null"), dst)
        assert not dst.exists(), "an empty notification file was installed and called success"

    def test_a_notification_delivered_during_the_copy_survives(self, tmp_path, monkeypatch):
        """``O_APPEND``, and it needs a CONCURRENT writer to be observable at all.

        The dashboard's sink appends with ``O_APPEND``, so its write goes to
        end-of-file; an ordinary write goes to this handle's own offset, which is
        stale once buffered. The flush then wrote over the delivered row. A test that
        only copies into an empty destination cannot tell the two apart -- every
        byte-level assertion passes either way -- so the delivery has to happen
        mid-copy. Measured on the pre-fix flags: 152 bytes with the row gone, against
        203 with both writers' records present.

        The seam is the DESTINATION OPEN, not a per-record hook. The single-read
        redesign calls ``_notification_key`` once per record during validation and not
        at all during the write, so the old per-record injection point went dead and
        this test silently stopped delivering anything -- it passed while proving
        nothing, which is the same failure mode as a mutation that never applies.
        Injecting when the ``O_CREAT`` open happens puts the delivery exactly where it
        belongs: after the live file exists, before this function's writes flush.
        """
        snap, home = self._snap(tmp_path, self.GOOD + self.MORE)
        live = home / "notifications.jsonl"
        delivered = b'{"ts":"2026-03-09T00:00:00Z","msg":"delivered-mid-copy"}\n'
        real_open = os.open

        def opening(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT:
                # What `_persist_notification` does, in its own mode, the instant the
                # live file exists.
                with open(live, "a", encoding="utf-8") as f:
                    f.write(delivered.decode())
            return fd

        monkeypatch.setattr(os, "open", opening)
        self._merge(snap, home)

        body = live.read_bytes()
        assert delivered in body, "the delivered notification was overwritten"
        assert self.GOOD in body and self.MORE in body, "an archive record was lost"
        body.decode("utf-8")

    def test_both_descriptors_ask_for_binary_mode(self, tmp_path, monkeypatch):
        """``os.open`` is the one API here that can be in TEXT mode.

        This is a CONVENTION fix, not a corruption fix, and the difference is worth
        recording. A review lane held that the missing flag corrupts records on
        Windows; ``test_a_clean_source_is_copied_record_for_record`` asserts byte-exact
        ``\\r\\n`` survival through these very descriptors, carries no skip marker, and
        passed on the Windows lane -- so the mechanism was tested and did not occur.
        What remains is that every sibling passes the flag (``crash_dump_store`` uses
        this exact read-flag triple) and this one did not.

        Asserted on the FLAGS, because what they guard against cannot be reproduced on
        a POSIX host: ``O_BINARY`` does not exist here, so its value is simulated and
        the assertion is that both opens carry it. The helper is called directly rather
        than through ``_do_merge`` so that every recorded ``os.open`` is one of this
        function's two and the count can be asserted exactly.
        """
        monkeypatch.setattr(os, "O_BINARY", 1 << 20, raising=False)
        seen: list[int] = []
        real_open = os.open

        def recording(path, flags, *args, **kwargs):
            seen.append(flags)
            return real_open(path, flags, *args, **kwargs)

        snap, home = self._snap(tmp_path, self.GOOD)
        monkeypatch.setattr(os, "open", recording)
        snapshot_mod._copy_notifications(snap / "notifications.jsonl", home / "notifications.jsonl")

        assert len(seen) == 2, f"expected exactly a source and a destination open: {seen}"
        assert all(
            f & os.O_BINARY for f in seen
        ), f"an open in this span did not ask for binary mode: {[hex(f) for f in seen]}"

    def test_a_note_delivered_during_the_copy_survives_the_READER(self, tmp_path, monkeypatch):
        """The one that matters: `O_APPEND` saves the bytes and loses the row anyway.

        `O_APPEND` stops a concurrent notification being OVERWRITTEN, and then orders it
        BEFORE the archive's rows -- the dashboard's append reaches end-of-file
        immediately while this copy's writes are still buffered. `_load_notifications`
        keeps the last `_MAX_PERSISTED_NOTIFICATIONS` rows POSITIONALLY, not the newest
        by timestamp, so importing a full 200-record history pushes the live row out of
        the window. Measured before the fix: line 0 of 201, and the reader returned 200
        rows without it.

        Asserted on the ORDERING, not on who wins a race. The first version of this test
        submitted the append and then checked the file, which passed with the
        serialisation removed because the copy's remaining writes happened to finish
        first -- a flaky test that proves nothing, caught by the mutation run. What makes
        the guarantee observable is that the single worker CANNOT run the queued append
        while the copy occupies it: the wait below times out with the fix and returns
        immediately without it, because a free worker executes a trivial job at once.

        The note is SUBMITTED to the executor rather than written inline, because that is
        the product's own route and the only one the serialisation can order.
        """
        from kiro_crew.dashboard import state as dashboard_state

        snap, home = self._snap(
            tmp_path,
            b"".join(
                b'{"ts":"2026-01-%02dT00:00:00Z","msg":"archive-%d","kind":"agent"}\n'
                % ((i % 28) + 1, i)
                for i in range(dashboard_state._MAX_PERSISTED_NOTIFICATIONS)
            ),
        )
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        pool = dashboard_state._notification_io_executor()
        note = {"ts": "2026-09-05T00:00:00Z", "msg": "LIVE-NOTE", "kind": "agent"}
        ran_during_copy = threading.Event()
        ran_while_worker_held: list[bool] = []
        submitted: list[object] = []
        dst = str(home / "notifications.jsonl")
        real_open = os.open

        def append_note() -> None:
            dashboard_state._persist_notification(note)
            ran_during_copy.set()

        def opening(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            # Scoped to the copy's own destination open, not to "the first O_CREAT
            # seen anywhere": this hook patches `os.open` PROCESS-WIDE, and on a
            # loaded CI shard the first creating open can belong to another thread
            # entirely (issue #8893 -- both ordering tests in this class fired on
            # shard 4 for PRs whose diffs never touch this path). A foreign trigger
            # submits the append while the worker is still FREE, and the assertion
            # then reads a real ordering guarantee as broken. The destination is the
            # one open the copy performs while it occupies the worker, so it is the
            # only trigger that measures the serialisation.
            if flags & os.O_CREAT and os.fspath(path) == dst and not submitted:
                submitted.append(pool.submit(append_note))
                # A FREE worker runs this in microseconds; a worker the copy is running
                # on cannot run it at all until the copy returns. RECORDED HERE rather
                # than read back after the merge: the instant the copy returns, the
                # worker is released and runs the queued append -- which is CORRECT and
                # sets the event. `is_set()` after `_merge` therefore measured whether
                # the main thread reached the assertion before that legitimate run, a
                # footrace it loses on a loaded shard (issue #8893, second cause: #8992
                # scoped the trigger but left this observation point). The wait's own
                # return value is taken while the copy still holds the worker, which is
                # the only window in which the defect -- and nothing else -- can set it.
                ran_while_worker_held.append(ran_during_copy.wait(timeout=1.0))
            return fd

        monkeypatch.setattr(os, "open", opening)
        self._merge(snap, home)

        assert submitted, "the note was never queued, so this test proved nothing"
        assert ran_while_worker_held == [False], (
            "the append ran while the copy was still writing, so the two are concurrent "
            "rather than ordered"
        )
        # Drained BEFORE any teardown, deliberately. `monkeypatch.undo()` here reverts
        # the patched `KIROCREW_HOME` while the append is still queued, so the worker
        # resolves a different data home and writes the note somewhere this assertion
        # never looks -- which reads exactly like the defect and is not it. FIFO, so
        # waiting on a no-op drains the append ahead of it.
        pool.submit(lambda: None).result()

        rows = dashboard_state._load_notifications()
        assert any(r.get("msg") == "LIVE-NOTE" for r in rows), (
            "the live notification was ordered ahead of the archive and dropped by the "
            f"reader's positional cap ({len(rows)} rows returned)"
        )

    def test_running_on_the_notification_worker_does_not_deadlock(self, tmp_path, monkeypatch):
        """The serialisation must not wait for a queue only it can drain.

        If the copy is ever reached from ON the single worker, submitting to that worker
        blocks for a job that cannot start until the submitter returns. The failure mode
        is a HANG, not an error, so it is worth a test even though nothing reaches it
        today: a deadlock inside a restore reports nothing at all.

        Against a THROWAWAY pool with the same thread-name prefix, not the product's, so
        that a regression wedges this test's worker rather than the shared one every
        other test in the process depends on. The timeout turns the hang into a named
        failure instead of a stalled suite.
        """
        import concurrent.futures

        snap, home = self._snap(tmp_path, self.GOOD)
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        from kiro_crew.dashboard import state as dashboard_state

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="notif-io")
        monkeypatch.setattr(dashboard_state, "_notification_io_pool", pool)
        try:
            future = pool.submit(
                snapshot_mod._copy_notifications,
                snap / "notifications.jsonl",
                home / "notifications.jsonl",
            )
            future.result(timeout=15)
        finally:
            pool.shutdown(wait=False)
        assert (home / "notifications.jsonl").read_bytes() == self.GOOD

    def test_a_FRESH_gateway_still_orders_the_copy_against_a_delivery(self, tmp_path, monkeypatch):
        """The writer is what makes the pool, so "no pool" never meant "no writer".

        Review's finding, and the branch it names was an absence-reading: the copy asked
        whether an executor existed and ran inline when none did. On a fresh gateway
        nothing has persisted a notification yet, so a delivery arriving during the
        restore CREATES the executor and appends through it, concurrently -- which puts
        the live row back at line 0 of 201 and outside the reader's positional window.
        Measured on the pre-fix branch: `append ran DURING the copy: True`, `note
        position: [0] of 201`, `survives reader: False`.

        Acquiring rather than asking closes it: the delivery gets the SAME executor the
        copy is occupying and queues behind it. Asserted for the `pool is None` path
        specifically, because that is the branch whose reasoning was wrong.
        """
        from kiro_crew.dashboard import state as dashboard_state

        snap, home = self._snap(
            tmp_path,
            b"".join(
                b'{"ts":"2026-01-%02dT00:00:00Z","msg":"archive-%d","kind":"agent"}\n'
                % ((i % 28) + 1, i)
                for i in range(dashboard_state._MAX_PERSISTED_NOTIFICATIONS)
            ),
        )
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        # A FRESH gateway: nothing has persisted a notification, so there is no pool.
        monkeypatch.setattr(dashboard_state, "_notification_io_pool", None)
        note = {"ts": "2026-09-05T00:00:00Z", "msg": "LIVE-NOTE", "kind": "agent"}
        ran_during_copy = threading.Event()
        ran_while_worker_held: list[bool] = []
        fired: list[int] = []
        dst = str(home / "notifications.jsonl")
        real_open = os.open

        def append_note() -> None:
            dashboard_state._persist_notification(note)
            ran_during_copy.set()

        def opening(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            # Same scoping as the READER test above, same reason (issue #8893): the
            # hook patches `os.open` PROCESS-WIDE, and on a loaded shard the first
            # O_CREAT can come from a foreign thread while the notification worker is
            # still FREE -- the append then runs immediately and the assertion reads
            # the ordering guarantee as broken. Only the copy's own destination open
            # happens while the copy occupies the worker, so it is the only trigger
            # that measures the serialisation this test is about.
            if flags & os.O_CREAT and os.fspath(path) == dst and not fired:
                fired.append(1)
                # The delivery sink's own route on a fresh gateway: it ACQUIRES the
                # executor, creating it if the copy did not.
                dashboard_state._notification_io_executor().submit(append_note)
                # Measured INSIDE the trigger, for the reason the READER test above
                # records: after the copy returns, the released worker runs the queued
                # append legitimately and sets the event, so a post-merge `is_set()`
                # read is a footrace against correct behaviour rather than a
                # measurement of the ordering.
                ran_while_worker_held.append(ran_during_copy.wait(timeout=1.0))
            return fd

        monkeypatch.setattr(os, "open", opening)
        self._merge(snap, home)

        assert fired, "the delivery was never queued, so this test proved nothing"
        assert ran_while_worker_held == [False], (
            "a delivery on a fresh gateway ran concurrently with the copy: 'no pool' "
            "was read as 'no writer'"
        )
        dashboard_state._notification_io_executor().submit(lambda: None).result()
        rows = dashboard_state._load_notifications()
        assert any(
            r.get("msg") == "LIVE-NOTE" for r in rows
        ), f"the live notification was dropped by the positional cap ({len(rows)} rows)"

    def test_the_ordering_trigger_ignores_a_foreign_O_CREAT_open(self, tmp_path, monkeypatch):
        """The loaded-shard condition itself, kept as a test (issue #8893).

        Both ordering tests in this class patch ``os.open`` PROCESS-WIDE, and both
        fired on a contended CI shard for PRs that never touch this path: some other
        thread's ``O_CREAT`` open arrived first, the append was submitted while the
        notification worker was still FREE, and it ran immediately -- the assertion
        then read a scheduling accident as a broken ordering guarantee. The fix is
        the destination-path scope on the trigger, and this test is that condition
        made deterministic: a churn thread is CONFIRMED to have done a creating open
        before the merge even starts, so under the old first-O_CREAT-anywhere shape
        the trigger is GUARANTEED foreign and the test fails every run, not one CI
        run in a thousand. Under the scoped shape the churn is invisible, however
        the scheduler interleaves it, and the ordering measurement is captured
        INSIDE the trigger -- the wait's return value, taken while the copy still
        holds the worker -- where the append's legitimate post-copy run cannot
        race it: the trigger fired on the destination, the worker stayed held,
        the note survived.
        """
        from kiro_crew.dashboard import state as dashboard_state

        snap, home = self._snap(
            tmp_path,
            b"".join(
                b'{"ts":"2026-01-%02dT00:00:00Z","msg":"archive-%d","kind":"agent"}\n'
                % ((i % 28) + 1, i)
                for i in range(dashboard_state._MAX_PERSISTED_NOTIFICATIONS)
            ),
        )
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setattr(dashboard_state, "_notification_io_pool", None)
        note = {"ts": "2026-09-05T00:00:00Z", "msg": "LIVE-NOTE", "kind": "agent"}
        ran_during_copy = threading.Event()
        ran_while_worker_held: list[bool] = []
        fired: list[str] = []
        dst = str(home / "notifications.jsonl")
        real_open = os.open

        def append_note() -> None:
            dashboard_state._persist_notification(note)
            ran_during_copy.set()

        def opening(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if flags & os.O_CREAT and os.fspath(path) == dst and not fired:
                fired.append(os.fspath(path))
                dashboard_state._notification_io_executor().submit(append_note)
                # The measurement is the WAIT'S RETURN VALUE, captured here. A free
                # worker runs the queued append in microseconds and the wait returns
                # True; the worker this copy occupies cannot run it at all, so the
                # wait times out False. Re-reading the EVENT after the merge returns
                # would race the append's legitimate post-copy run instead (the
                # churner join below hands the worker exactly that window).
                ran_while_worker_held.append(ran_during_copy.wait(timeout=1.0))
            return fd

        monkeypatch.setattr(os, "open", opening)

        noise_dir = tmp_path / "foreign-o-creat"
        noise_dir.mkdir()
        stop = threading.Event()
        churned_once = threading.Event()

        def churn() -> None:
            i = 0
            while not stop.is_set():
                p = noise_dir / f"scratch-{i}.tmp"
                fd = os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)
                p.unlink(missing_ok=True)
                churned_once.set()
                i += 1

        churner = threading.Thread(target=churn, name="foreign-o-creat", daemon=True)
        churner.start()
        try:
            assert churned_once.wait(timeout=5.0), "the churn thread never opened a file"
            self._merge(snap, home)
        finally:
            stop.set()
            churner.join(timeout=5.0)

        assert fired == [dst], f"the trigger fired on the wrong open(s): {fired}"
        assert ran_while_worker_held == [False], (
            "the append ran while the copy occupied the worker: a foreign trigger "
            "submitted it while the worker was still free, or the ordering broke"
        )
        dashboard_state._notification_io_executor().submit(lambda: None).result()
        rows = dashboard_state._load_notifications()
        assert any(
            r.get("msg") == "LIVE-NOTE" for r in rows
        ), f"the live notification was dropped ({len(rows)} rows)"

    def test_an_import_failure_that_is_not_ImportError_is_not_swallowed(
        self, tmp_path, monkeypatch
    ):
        """The narrowing itself, which the broad ``except`` made untestable.

        ``except Exception`` turned "I could not check" into "there is nothing to check":
        an import failing inside a live gateway for any reason other than absence would
        have run the copy inline and lost the ordering with no signal. Widening the
        clause back cannot be caught by any test that only exercises ``ImportError``,
        because a wider except is a superset -- so this drives the case the narrowing
        exists for and requires it to PROPAGATE.
        """
        import builtins

        snap, home = self._snap(tmp_path, self.GOOD)
        real_import = builtins.__import__

        def failing(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "kiro_crew.dashboard" and "state" in (fromlist or ()):
                raise RuntimeError("dashboard state failed to initialise")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", failing)
        with pytest.raises(RuntimeError, match="failed to initialise"):
            snapshot_mod._copy_notifications(
                snap / "notifications.jsonl", home / "notifications.jsonl"
            )

    def test_an_unimportable_dashboard_falls_back_instead_of_failing(self, tmp_path, monkeypatch):
        """The other absence-reading, narrowed to ``ImportError``.

        A genuinely absent dashboard module is the CLI restore, which has no writer to
        order against, so running inline is correct there. What was wrong was catching
        ``Exception``: an import failing for any other reason inside a live gateway would
        have degraded the ordering in silence. This asserts the narrow fallback still
        works; it cannot assert ORDERING, because by construction there is no dashboard
        to order against on this path.
        """
        import sys

        snap, home = self._snap(tmp_path, self.GOOD)
        # `None` in sys.modules makes the import raise ImportError, which is the exact
        # branch under test rather than a stand-in for it.
        monkeypatch.setitem(sys.modules, "kiro_crew.dashboard.state", None)
        snapshot_mod._copy_notifications(snap / "notifications.jsonl", home / "notifications.jsonl")
        assert (home / "notifications.jsonl").read_bytes() == self.GOOD

    def test_concurrent_callers_all_get_the_SAME_executor(self, monkeypatch):
        """Issue #8788: the lazy init was an unlocked check-then-set.

        Two threads could each observe ``None``, each construct a pool, and each
        proceed -- one assignment won the global while the loser's worker was already
        live with its job queued, so the two callers were not serialised against one
        another at all. That voids the only guarantee this executor provides, and it is
        the precondition of the ordering this restore path depends on.

        The construction window is WIDENED on purpose. Measured first: with 16 threads
        released from a barrier and the lock removed, this passed 5 out of 5 rounds --
        CPython's switch interval lets the first thread finish constructing before any
        other is scheduled, so the natural window is real but far too small to observe.
        A test that cannot fail proves nothing, so the constructor is made slow enough
        that a second caller is guaranteed to be inside the window. What is being tested
        is the mutual exclusion, not the timing.

        Asserted by object identity, because calling the accessor twice in sequence
        returns the same object with or without the lock.
        """
        import concurrent.futures

        from kiro_crew.dashboard import state as dashboard_state

        real_executor = concurrent.futures.ThreadPoolExecutor

        class SlowToBuild(real_executor):  # type: ignore[misc,valid-type]
            def __init__(self, *args, **kwargs):
                time.sleep(0.2)  # the window, held open
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(dashboard_state, "_notification_io_pool", None)
        monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", SlowToBuild)
        threads = 8
        barrier = threading.Barrier(threads)
        created: list[object] = []
        lock = threading.Lock()

        def racer() -> None:
            barrier.wait(timeout=10)
            pool = dashboard_state._notification_io_executor()
            with lock:
                created.append(pool)

        workers = [threading.Thread(target=racer) for _ in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=30)

        assert len(created) == threads, f"only {len(created)} of {threads} racers reported"
        distinct = {id(p) for p in created}
        assert len(distinct) == 1, (
            f"{len(distinct)} different executors were created, so callers holding "
            "different pools are not serialised against each other"
        )
        # Whatever the race produced must also be what the module kept, or a caller is
        # queuing onto a pool the module no longer hands out.
        assert created[0] is dashboard_state._notification_io_pool

    @requires_symlinks
    def test_a_dangling_symlink_at_the_live_name_is_refused(self, tmp_path):
        """``is_file()`` calls a dangling link absent, and ``copy2`` wrote THROUGH it.

        So the branch was chosen because "no live file exists" while a link sat at
        the name, and the archive's bytes landed on the link's target -- outside
        the data home. The publish refuses instead, and the refusal must not delete
        the file the link points at either.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        outside = tmp_path / "outside.jsonl"
        (home / "notifications.jsonl").symlink_to(outside)
        with pytest.raises(FileExistsError):
            self._merge(snap, home)
        assert not outside.exists(), "the archive's bytes were written outside the data home"
        assert (home / "notifications.jsonl").is_symlink(), "the operator's link was removed"


# ── Issue #8217: the restore status line must not claim success over a refused
# cron merge ───────────────────────────────────────────────────────────────────


class TestNotificationCopyRefusalWithoutONofollow(_NotificationCopyFixtures):
    """The platform refusal, which must be exercised on EVERY platform.

    Deliberately NOT marked ``requires_o_nofollow``: on a platform that truly lacks
    the flag these are the only tests in this area that can run, and they are the
    ones that matter there. On a platform that has it the absence is simulated by
    deleting the constant, the idiom this suite already uses.
    """

    def test_a_platform_without_o_nofollow_refuses_the_copy_ENTIRELY(self, tmp_path, monkeypatch):
        """No ``O_NOFOLLOW`` means no safe copy, so the copy does not happen at all.

        Simulated the way this suite already simulates that platform -- by deleting the
        constant -- because the behaviour is a property of the flag being absent, not of
        Windows.

        The source here is a PERFECTLY ORDINARY regular file: no link, no reparse point,
        nothing wrong with it. That is the point of the test. The predecessor checked
        ``is_reparse_point`` by name and then opened the same name, which an adversary
        that chooses the timing walks straight through, and ``fstat``'s ``S_ISREG`` does
        not close it because a reparse point resolving to a regular ``.env`` passes it.
        So the refusal cannot be conditional on the source looking wrong at check time --
        a test that only refuses a symlinked source would pass with the old, racy floor
        still in place.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

        with pytest.raises(snapshot_mod.NotificationCopyUnsupported) as caught:
            snapshot_mod._copy_notifications(
                snap / "notifications.jsonl", home / "notifications.jsonl"
            )

        assert not (home / "notifications.jsonl").exists(), "a destination was created"
        message = str(caught.value)
        # A silent skip is the bug class being fixed, so the diagnosis has to be IN the
        # message: the missing primitive, and what was not imported.
        assert "O_NOFOLLOW" in message, message
        assert "NOT imported" in message, message
        assert "notifications.jsonl" in message, message

    def test_the_refusal_happens_before_anything_is_opened_or_acquired(self, tmp_path, monkeypatch):
        """The refusal is a question about the platform, not about this source.

        So it must be decided before the executor is acquired and before the source is
        opened -- otherwise a platform that can never do this safely still pays a thread
        hop and still touches the file. Asserted by counting, so an edit that moves the
        gate below the acquire reddens here.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        opens: list[object] = []
        acquires: list[object] = []
        real_open = os.open

        def counting_open(path, *a, **kw):
            opens.append(path)
            return real_open(path, *a, **kw)

        from kiro_crew.dashboard import state as dashboard_state

        real_exec = dashboard_state._notification_io_executor

        def counting_exec():
            acquires.append(1)
            return real_exec()

        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        monkeypatch.setattr(os, "open", counting_open)
        monkeypatch.setattr(dashboard_state, "_notification_io_executor", counting_exec)

        with pytest.raises(snapshot_mod.NotificationCopyUnsupported):
            snapshot_mod._copy_notifications(
                snap / "notifications.jsonl", home / "notifications.jsonl"
            )

        assert opens == [], f"the source was opened despite the refusal: {opens}"
        assert acquires == [], "the executor was acquired despite the refusal"

    def test_the_restore_CONTINUES_and_reports_the_skip_rather_than_aborting(
        self, tmp_path, monkeypatch, capsys
    ):
        """A platform refusal skips ONE component; it does not fail the restore.

        The distinction is deliberate and it is not the abort posture the undecodable
        record has. An undecodable record says THIS ARCHIVE is bad, so the restore
        stops. A missing ``O_NOFOLLOW`` says THIS PLATFORM can never do this safely,
        which is not a reason to refuse the other components -- and the records are
        still in the snapshot, so nothing is destroyed by skipping.

        What must NOT happen is the restore reporting a copy it did not make. That is
        why the skip is asserted on the OUTPUT and the absence of the file together.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        (snap / "config.json").write_text('{"k": "v"}')
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

        raised = None
        try:
            self._merge(snap, home)
        except BaseException as exc:  # noqa: BLE001 - the point is that none arrives
            raised = exc

        out = capsys.readouterr().out
        assert raised is None, f"the whole restore aborted on a platform refusal: {raised!r}"
        assert not (home / "notifications.jsonl").exists(), "records were installed anyway"
        assert "SKIPPED" in out, out
        assert "O_NOFOLLOW" in out, out
        assert "Notifications: copied" not in out, "reported a copy it did not make"

    def test_no_by_name_reparse_check_is_made_on_either_path(self, tmp_path):
        """The by-name check is gone, not merely bypassed where the open can decide.

        It used to be a floor gated on the flag's absence. It is now nothing: where the
        flag exists the open decides, and where it does not the copy is refused before
        reaching here. Counting calls rather than reading the source, so reintroducing
        the check -- the racy remedy this PR removed -- reddens here.
        """
        snap, home = self._snap(tmp_path, self.GOOD)
        calls: list[object] = []
        real = snapshot_mod.pinned_fs.is_reparse_point

        def counted(path):
            calls.append(path)
            return real(path)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(snapshot_mod.pinned_fs, "is_reparse_point", counted)
            self._merge(snap, home)
        assert calls == [], f"a by-name reparse check ran: {calls}"


class TestARefusedCronMergeIsVisibleInTheRestoreStatus:
    """`_do_merge` printed "✅ crons" whatever `_merge_crons` decided.

    The terminal shows the merger's own warning right above, but the status
    line is the summary an operator scans — a checkmark over a refusal is the
    same false success the import summary had (#8217).
    """

    @staticmethod
    def _job(name: str) -> dict:
        return {
            "id": "j1",
            "name": name,
            "message": "check",
            "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
        }

    def test_a_refused_merge_prints_a_skip_not_a_checkmark(self, tmp_path, capsys):
        snap = tmp_path / "snap"
        home = tmp_path / "home"
        snap.mkdir()
        home.mkdir()
        (snap / "crons.json").write_text(json.dumps({"jobs": [self._job("imported")]}))
        (home / "crons.json").write_text("{not json")

        snapshot_mod._do_merge(snap, home, ["crons"], allow_unpinned=bool(unpinnable_argv()))

        out = capsys.readouterr().out
        assert "✅ crons" not in out, out
        assert "crons: merge skipped" in out, out
        # The refusal wrote nothing.
        assert (home / "crons.json").read_text() == "{not json"

    def test_a_genuine_merge_still_prints_the_checkmark(self, tmp_path, capsys):
        snap = tmp_path / "snap"
        home = tmp_path / "home"
        snap.mkdir()
        home.mkdir()
        (snap / "crons.json").write_text(json.dumps({"jobs": [self._job("imported")]}))
        (home / "crons.json").write_text(json.dumps({"jobs": [self._job("existing")]}))

        snapshot_mod._do_merge(snap, home, ["crons"], allow_unpinned=bool(unpinnable_argv()))

        out = capsys.readouterr().out
        assert "✅ crons" in out, out
        assert "crons: merge skipped" not in out, out
