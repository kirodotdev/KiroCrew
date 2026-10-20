"""Shared fixture and helpers of the onboarding-import behaviour tests.

The autouse ``_register_predecessor_source`` fixture, with ``_install`` and
``_lineage_source``, registers the predecessor source so ``preview_import`` discovers it.
``_source``, ``_categories`` and ``_select`` read and narrow a plan; ``_write_jsonl``,
``_write_openclaw_session`` and ``_write_lineage_memory_db`` write foreign-agent files on
disk. ``test_onboarding_import.py`` and the four topic files its docstring names import what
they use by name, and each imports the autouse fixture so it applies to every test in that
file. ``_api`` is not here: each of those files defines its own, because the patch guards in
``test_onboarding_import_refactor_contract_patch_guards.py`` resolve ``api = _api()`` only
through a function the scanned file defines.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import reset_context, set_context
from kiro_crew.platform.interfaces import ImportSource


def _install(*sources: ImportSource) -> None:
    """Compose a context whose edition contributes *sources*."""

    class _Provider:
        def import_sources(self) -> list[ImportSource]:
            return list(sources)

    base = build_default_context(KiroCrewConfig())
    set_context(dataclasses.replace(base, import_sources=_Provider()))


def _lineage_source(**overrides) -> ImportSource:
    fields: dict = {
        "id": "predecessor",
        "display_name": "Predecessor",
        "env_vars": ("PREDECESSOR_HOME",),
        "home_dir": ".predecessor",
    }
    fields.update(overrides)
    return ImportSource(**fields)


@pytest.fixture(autouse=True)
def _register_predecessor_source():
    """Register the predecessor source so preview_import discovers it."""
    _install(_lineage_source())
    yield
    reset_context()


def _source(result: dict, source_id: str) -> dict:
    return next(source for source in result["sources"] if source["id"] == source_id)


def _categories(result: dict, source_id: str) -> dict[str, int]:
    source = _source(result, source_id)
    return {category["id"]: category["count"] for category in source["categories"]}


def _select(plan: dict, *pairs: tuple[str, str]) -> dict:
    wanted = set(pairs)
    plan["selection"] = [
        item for item in plan["selection"] if (item["source_id"], item["category_id"]) in wanted
    ]
    for source in plan["sources"]:
        for category in source["categories"]:
            category["selected"] = (source["id"], category["id"]) in wanted
    return plan


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _write_openclaw_session(
    state: Path,
    *,
    agent_id: str = "main",
    session_id: str = "session-1",
    session_key: str = "agent:main:main",
    entry_updates: dict[str, object] | None = None,
) -> Path:
    sessions = state / "agents" / agent_id / "sessions"
    transcript = sessions / f"{session_id}.jsonl"
    _write_jsonl(
        transcript,
        [
            {"role": "user", "content": f"question from {session_id}"},
            {"role": "assistant", "content": f"answer from {session_id}"},
        ],
    )
    entry: dict[str, object] = {
        "sessionId": session_id,
        "sessionFile": transcript.name,
        "createdVia": "operator",
        "createdActor": {"type": "human"},
    }
    if entry_updates:
        entry.update(entry_updates)
    (sessions / "sessions.json").write_text(
        json.dumps({session_key: entry}),
        encoding="utf-8",
    )
    return transcript


def _write_lineage_memory_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE semantic_memory (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                confidence REAL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                is_deleted INTEGER DEFAULT 0
            );
            CREATE TABLE episodic_memories (
                id TEXT PRIMARY KEY,
                conversation_id TEXT,
                text TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                importance REAL DEFAULT 0.5,
                created_at TEXT NOT NULL,
                is_deleted INTEGER DEFAULT 0
            );
            INSERT INTO semantic_memory
                (key, value_json, confidence, source, created_at, updated_at, is_deleted)
                VALUES
                ('pref.editor', '"vim"', 0.95, 'user_explicit', '2026-01-01', '2026-01-01', 0),
                ('pref.deleted', '"ignore"', 1.0, 'user_explicit', '2026-01-01', '2026-01-01', 1);
            INSERT INTO episodic_memories
                (id, conversation_id, text, tags, importance, created_at, is_deleted)
                VALUES
                ('episode-1', 'chat-1', 'Remember the dashboard uses port 6777.', '["dev"]',
                 0.8, '2026-01-01', 0),
                ('episode-deleted', 'chat-1', 'Ignore this deleted memory.', '[]',
                 0.5, '2026-01-01', 1);
            """)
