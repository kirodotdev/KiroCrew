"""The crew-home fixtures shared by ``test_portability.py`` and its topic siblings.

``fake_kirocrew_home`` builds a realistic crew directory under ``tmp_path``: config,
hooks, crons, notifications, the memory databases, workspace memory, plan memory and a
skill, plus the secret files, pid lists, snapshots and outbox an export must leave out.
``patched_config_dir`` points ``kiro_crew.portability`` and ``KIROCREW_HOME`` at it.
Every module whose tests request them binds both by name, because
``patched_config_dir`` requests ``fake_kirocrew_home``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from unittest.mock import patch

import pytest


@pytest.fixture
def fake_kirocrew_home(tmp_path):
    """Create a realistic ~/.kirocrew directory structure for testing."""
    mc = tmp_path / ".kirocrew"
    mc.mkdir()

    # config.json
    config = {
        "agent": {"provider": "acp", "model": "auto", "yolo": False},
        "session": {"timeout_secs": 3600},
        "memory": {"embedding_provider": "none"},
    }
    (mc / "config.json").write_text(json.dumps(config, indent=2))

    # hooks.json
    (mc / "hooks.json").write_text(json.dumps({"hooks": [{"id": "h1", "cmd": "echo hi"}]}))

    # crons.json
    # The real schema: `CronService` serialises `"schedule": asdict(j.schedule)`,
    # so it is an OBJECT with a `kind`. A bare cron string here would be a shape
    # the product never writes and `CronService._load` cannot read — it subscripts
    # `j["schedule"]["kind"]`, so a string raises TypeError out of the load.
    crons = {
        "jobs": [
            {
                "id": "c1",
                "name": "daily-check",
                "message": "check",
                "schedule": {"kind": "cron", "cron_expr": "0 9 * * *"},
            }
        ]
    }
    (mc / "crons.json").write_text(json.dumps(crons, indent=2))

    # notifications.jsonl
    (mc / "notifications.jsonl").write_text(
        json.dumps({"ts": "1700000000", "title": "test", "body": "notification"}) + "\n"
    )

    # memory.db (SQLite)
    db_path = mc / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE semantic_memory (key TEXT PRIMARY KEY, value_json TEXT, confidence REAL, source TEXT, created_at TEXT, updated_at TEXT, embedding BLOB, is_deleted INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO semantic_memory (key, value_json, confidence, source, created_at, updated_at, is_deleted) VALUES ('user.name', '\"Alice\"', 0.9, 'agent', '2026-01-01', '2026-01-01', 0)"
    )
    conn.execute(
        "CREATE TABLE episodic_memories (id TEXT PRIMARY KEY, conversation_id TEXT, text TEXT, embedding BLOB, tags TEXT, importance REAL, created_at TEXT, last_accessed_at TEXT, is_deleted INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO episodic_memories (id, conversation_id, text, importance, created_at, last_accessed_at, is_deleted) VALUES ('ep1', 'conv1', 'user asked about deployment', 0.8, '2026-01-01', '2026-01-01', 0)"
    )
    conn.execute(
        "CREATE TABLE knowledge_facts (subject TEXT, predicate TEXT, object TEXT, episode_id TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE knowledge_edges (source_key TEXT, target_key TEXT, relation TEXT, weight REAL, metadata TEXT, created_at TEXT)"
    )
    conn.commit()
    conn.close()

    # memory_index.db (FTS5)
    idx_path = mc / "memory_index.db"
    conn = sqlite3.connect(str(idx_path))
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(path, content, tokenize='porter unicode61')"
    )
    conn.execute(
        "INSERT INTO memory_fts (path, content) VALUES ('preferences.md', 'user prefers dark mode')"
    )
    conn.commit()
    conn.close()

    # workspace/memory/
    mem_dir = mc / "workspace" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "preferences.md").write_text(
        "# User Preferences\n\n- Prefers dark mode\n- Uses vim\n"
    )
    (mem_dir / "projects.md").write_text(
        "# Active Projects\n\n## KiroCrew\nWorking on portability feature\n"
    )
    hist_dir = mem_dir / "history"
    hist_dir.mkdir()
    (hist_dir / "2026-05-17.md").write_text(
        "# 2026-05-17\n\n#### 09:00 PDT\nDiscussed architecture\n"
    )
    (hist_dir / "2026-05-18.md").write_text(
        "# 2026-05-18\n\n#### 10:00 PDT\nImplemented export feature\n"
    )

    # plan_memory/
    pm_dir = mc / "plan_memory"
    pm_dir.mkdir()
    (pm_dir / "current_plan.md").write_text("# Plan\n\nStep 1: Export\nStep 2: Import\n")

    # skills/
    sk_dir = mc / "skills" / "my-skill"
    sk_dir.mkdir(parents=True)
    (sk_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Test skill\n---\n# My Skill\n"
    )

    # Credential files that must be EXCLUDED
    (mc / ".env").write_text("SLACK_BOT_TOKEN=xoxb-secret\nSLACK_APP_TOKEN=xapp-secret\n")
    (mc / ".local_secret").write_text("dashboard-auth-token-xyz")
    (mc / "sel_hmac.key").write_text("hmac-key-content")
    (mc / "telemetry_salt").write_text("salt-value")
    (mc / "session_map.json").write_text(json.dumps({"dashboard:chat-1": {"sid": "abc"}}))
    (mc / "kiro_session_pids.txt").write_text("12345\n67890\n")
    (mc / "kiro_pids.txt").write_text("111:222\n333:444\n")

    # Directories that must be excluded
    (mc / "snapshots").mkdir()
    (mc / "snapshots" / "old-snapshot.tar.gz").write_text("fake")
    (mc / "outbox").mkdir()
    (mc / "outbox" / "file.txt").write_text("delivered")

    return mc


@pytest.fixture
def patched_config_dir(fake_kirocrew_home):
    """Patch config_dir() to return our fake directory."""
    with patch("kiro_crew.portability.config_dir", return_value=fake_kirocrew_home):
        with patch.dict(os.environ, {"KIROCREW_HOME": str(fake_kirocrew_home)}):
            yield fake_kirocrew_home
