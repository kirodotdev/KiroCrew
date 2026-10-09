from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from kiro_crew.config import config_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  window_days INTEGER NOT NULL,
  cataloged INTEGER NOT NULL,
  selected INTEGER NOT NULL,
  analyzed INTEGER NOT NULL,
  served_model TEXT,
  report_path TEXT,
  report_digest TEXT,
  artifact_slug TEXT,
  status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
  run_id TEXT NOT NULL,
  claim_id TEXT NOT NULL,
  dimension TEXT NOT NULL,
  behavior_predicate TEXT,
  text TEXT NOT NULL,
  session_keys TEXT NOT NULL,
  owner_rating TEXT,
  PRIMARY KEY (run_id, claim_id)
);
CREATE TABLE IF NOT EXISTS actions (
  run_id TEXT NOT NULL,
  action_id TEXT NOT NULL,
  action_key TEXT NOT NULL,
  action_class TEXT NOT NULL,
  behavior_predicate TEXT,
  title TEXT NOT NULL,
  payload TEXT NOT NULL,
  state TEXT NOT NULL,
  applied_at REAL,
  verified_at REAL,
  undone_at REAL,
  target_identity TEXT,
  baseline_sessions INTEGER,
  PRIMARY KEY (run_id, action_id)
);
CREATE TABLE IF NOT EXISTS facet_cache (
  session_key TEXT NOT NULL,
  content_digest TEXT NOT NULL,
  served_model TEXT NOT NULL,
  facet TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (session_key, content_digest, served_model)
);
"""


def insights_home() -> Path:
    home = config_dir() / "personal-insights"
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    return home


class RunRepository:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or insights_home() / "runs.sqlite"
        existed = self.path.exists()
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        if not existed:
            os.chmod(self.path, 0o600)

    def close(self) -> None:
        self.db.close()

    def start_run(self, *, window_days: int, cataloged: int, selected: int) -> str:
        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
        self.db.execute(
            "INSERT INTO runs (run_id, created_at, window_days, cataloged, selected, analyzed, status)"
            " VALUES (?, ?, ?, ?, ?, 0, 'started')",
            (run_id, time.time(), window_days, cataloged, selected),
        )
        self.db.commit()
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        analyzed: int,
        served_model: str,
        report_path: str,
        report_digest: str,
        status: str = "complete",
    ) -> None:
        self.db.execute(
            "UPDATE runs SET analyzed=?, served_model=?, report_path=?, report_digest=?, status=?"
            " WHERE run_id=?",
            (analyzed, served_model, report_path, report_digest, status, run_id),
        )
        self.db.commit()

    def set_artifact(self, run_id: str, slug: str) -> None:
        self.db.execute("UPDATE runs SET artifact_slug=? WHERE run_id=?", (slug, run_id))
        self.db.commit()

    def record_claims(self, run_id: str, claims: list[dict[str, Any]]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO claims (run_id, claim_id, dimension, behavior_predicate, text,"
            " session_keys) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    c["claim_id"],
                    c["dimension"],
                    c.get("behavior_predicate"),
                    c["text"],
                    json.dumps(c.get("session_keys", [])),
                )
                for c in claims
            ],
        )
        self.db.commit()

    def record_actions(
        self, run_id: str, actions: list[dict[str, Any]], baseline_sessions: int
    ) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO actions (run_id, action_id, action_key, action_class,"
            " behavior_predicate, title, payload, state, baseline_sessions)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?)",
            [
                (
                    run_id,
                    a["action_id"],
                    a["action_key"],
                    a["action_class"],
                    a.get("behavior_predicate"),
                    a["title"],
                    json.dumps(a, sort_keys=True),
                    baseline_sessions,
                )
                for a in actions
            ],
        )
        self.db.commit()

    def cached_facet(self, session_key: str, content_digest: str, served_model: str) -> dict | None:
        row = self.db.execute(
            "SELECT facet FROM facet_cache WHERE session_key=? AND content_digest=? AND served_model=?",
            (session_key, content_digest, served_model),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def store_facet(
        self, session_key: str, content_digest: str, served_model: str, facet: dict
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO facet_cache (session_key, content_digest, served_model, facet,"
            " created_at) VALUES (?, ?, ?, ?, ?)",
            (
                session_key,
                content_digest,
                served_model,
                json.dumps(facet, sort_keys=True),
                time.time(),
            ),
        )
        self.db.commit()

    def get_action(self, action_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT run_id, action_id, action_key, action_class, behavior_predicate, title, payload,"
            " state, applied_at, verified_at, undone_at, target_identity, baseline_sessions"
            " FROM actions WHERE action_id=? ORDER BY run_id DESC LIMIT 1",
            (action_id,),
        ).fetchone()
        if not row:
            return None
        keys = [
            "run_id",
            "action_id",
            "action_key",
            "action_class",
            "behavior_predicate",
            "title",
            "payload",
            "state",
            "applied_at",
            "verified_at",
            "undone_at",
            "target_identity",
            "baseline_sessions",
        ]
        record = dict(zip(keys, row))
        record["payload"] = json.loads(record["payload"])
        return record

    def set_action_state(
        self,
        run_id: str,
        action_id: str,
        state: str,
        *,
        target_identity: str | None = None,
        applied: bool = False,
        verified: bool = False,
        undone: bool = False,
    ) -> None:
        now = time.time()
        sets = ["state=?"]
        params: list[Any] = [state]
        if target_identity is not None:
            sets.append("target_identity=?")
            params.append(target_identity)
        if applied:
            sets.append("applied_at=?")
            params.append(now)
        if verified:
            sets.append("verified_at=?")
            params.append(now)
        if undone:
            sets.append("undone_at=?")
            params.append(now)
        params.extend([run_id, action_id])
        self.db.execute(
            f"UPDATE actions SET {', '.join(sets)} WHERE run_id=? AND action_id=?", params
        )
        self.db.commit()

    def claims_for_action(self, run_id: str, claim_ids: list[str]) -> list[dict[str, Any]]:
        if not claim_ids:
            return []
        marks = ",".join("?" for _ in claim_ids)
        rows = self.db.execute(
            f"SELECT claim_id, dimension, behavior_predicate, text, session_keys FROM claims"
            f" WHERE run_id=? AND claim_id IN ({marks})",
            [run_id, *claim_ids],
        ).fetchall()
        return [
            {
                "claim_id": r[0],
                "dimension": r[1],
                "behavior_predicate": r[2],
                "text": r[3],
                "session_keys": json.loads(r[4]),
            }
            for r in rows
        ]

    def prior_applied_actions(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT run_id, action_id, action_key, action_class, behavior_predicate, title, payload,"
            " state, applied_at, verified_at, undone_at, baseline_sessions FROM actions"
            " WHERE state IN ('applied_verified', 'changed_unverified') ORDER BY applied_at"
        ).fetchall()
        keys = [
            "run_id",
            "action_id",
            "action_key",
            "action_class",
            "behavior_predicate",
            "title",
            "payload",
            "state",
            "applied_at",
            "verified_at",
            "undone_at",
            "baseline_sessions",
        ]
        return [dict(zip(keys, row)) for row in rows]

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT run_id, created_at, window_days, cataloged, selected, analyzed, served_model,"
            " report_path, artifact_slug, status FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        keys = [
            "run_id",
            "created_at",
            "window_days",
            "cataloged",
            "selected",
            "analyzed",
            "served_model",
            "report_path",
            "artifact_slug",
            "status",
        ]
        return [dict(zip(keys, row)) for row in rows]
