"""Campaign storage: the campaigns SQLite table and the campaign directory.

Owns where a campaign lives (data-home paths, resolved per call), the schema
and its migrations, the on-loop connection guard, the agent-facing file
interface (status sidecar, guidance, questions, cycle findings) and the reads
that surface campaign rows and findings, scrubbed through ``untrusted``.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.auto_research.campaign import LOGGER_NAME, untrusted
from kiro_crew.apps.builtins.auto_research.session_keys import is_campaign_id
from kiro_crew.atomic_write import read_json_or
from kiro_crew.config.paths import data_home
from kiro_crew.on_loop_db import OnLoopDBGuard

logger = logging.getLogger(LOGGER_NAME)

# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage.py is the reference implementation.
RESEARCH_DIR: Path | None = None
DB_PATH: Path | None = None


def research_dir() -> Path:
    """Research workspace dir, resolved against the live data home."""
    return RESEARCH_DIR if RESEARCH_DIR is not None else data_home() / "workspace" / "research"


def db_path() -> Path:
    """Campaigns sqlite DB path, resolved against the live data home."""
    return (
        DB_PATH if DB_PATH is not None else data_home() / "apps" / "auto-research" / "campaigns.db"
    )


# Serializes the one-time WAL switch + schema init per DB file (see
# _ensure_schema). Keyed by DB path so per-test temp DBs each init once.
_DB_INIT_LOCK = threading.Lock()
_INITIALIZED_DBS: set[str] = set()


# Execution mode + recursive-exploration budget defaults (RL v2). The SQLite
# column DEFAULTs in _get_db() mirror these — keep them in sync.
VALID_EXECUTION_MODES = ("agent", "workflow")
DEFAULT_EXECUTION_MODE = "agent"
DEFAULT_MAX_SUBQUESTIONS_PER_ROUND = 3
DEFAULT_DEPTH_DECAY = 0.5
DEFAULT_RESERVE_FRACTION = 0.15


class CampaignStatus(str, Enum):
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    STAGNANT = "stagnant"
    NEEDS_INPUT = "needs_input"
    COMPLETE = "complete"
    FAILED = "failed"
    STOPPED = "stopped"


def _validate_campaign_id(campaign_id: str) -> bool:
    """Reject IDs that could cause path traversal."""
    return is_campaign_id(campaign_id)


def _safe_campaign_dir(campaign_id: str) -> Path | None:
    """Return campaign dir only if it resolves within the research dir."""
    if not _validate_campaign_id(campaign_id):
        return None
    root = research_dir()
    d = (root / campaign_id).resolve()
    if not d.is_relative_to(root.resolve()):
        return None
    return d


# --- Database ---


# The campaigns DB carries a 30s busy timeout, so one on-loop lock wait can
# outlast the 25s loop-stall watchdog budget and kill the gateway. All six call
# sites are offloaded behind this guard, which delegates to the shared
# implementation in ``kiro_crew.on_loop_db``. Defaults are deliberate: this
# surface IS fully offloaded, so it stays on the shared
# ``KIROCREW_STRICT_ON_LOOP_PERSIST`` switch (which the e2e harness exports) and
# keeps the dev-mode arm, where a raise means genuinely new drift.
_ON_LOOP_DB_GUARD = OnLoopDBGuard(
    label="auto_research campaigns DB",
    remedy=(
        "Offload the DB section (asyncio.to_thread / run_in_executor) like the "
        "surrounding handlers do."
    ),
)


def _get_db() -> sqlite3.Connection:
    _ON_LOOP_DB_GUARD.check()
    dbp = db_path()
    dbp.parent.mkdir(parents=True, exist_ok=True)
    # Explicit 30s busy timeout (vs the 5s driver default). The research worker
    # writes findings/status every cycle while the app's HTTP handlers also
    # read/write; the longer busy timeout absorbs brief write contention instead
    # of surfacing "database is locked". WAL journal mode is set once per DB in
    # _ensure_schema() below (it is persistent in the DB header).
    conn = sqlite3.connect(str(dbp), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # Belt-and-suspenders: also set busy_timeout via PRAGMA so it applies even if
    # a driver ignores the connect kwarg. Neither this nor connect() acquires a
    # DB lock, so it is safe before the schema init runs.
    conn.execute("PRAGMA busy_timeout=30000")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Switch the DB into WAL mode and create/migrate the schema -- exactly once
    per DB file, serialized by a process-wide lock.

    ``journal_mode=WAL`` is persistent in the DB header, and *switching into*
    WAL needs a brief exclusive lock. Running that switch on every connection
    raced with concurrent writers (validate/create run off the event loop via
    run_in_executor) and surfaced "database is locked" on the PRAGMA itself --
    ``busy_timeout`` cannot resolve exclusive-lock contention where several
    connections all try to flip a not-yet-WAL DB at once. Performing it once,
    under a Python-level lock, guarantees a single connection does the switch
    while no other connection holds a DB lock; later connections find WAL
    already set and skip straight to serving queries. Keyed by DB path so
    per-test temp DBs each initialize independently.
    """
    dbp = db_path()
    key = str(dbp)
    if key in _INITIALIZED_DBS and dbp.exists() and dbp.stat().st_size > 0:
        return
    with _DB_INIT_LOCK:
        if key in _INITIALIZED_DBS and dbp.exists() and dbp.stat().st_size > 0:
            return  # double-checked locking
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN")
            conn.execute("""CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, question TEXT NOT NULL,
                sub_questions TEXT NOT NULL DEFAULT '[]', sources TEXT NOT NULL DEFAULT '[]',
                max_cycles INTEGER NOT NULL DEFAULT 30, idle_secs INTEGER NOT NULL DEFAULT 120,
                status TEXT NOT NULL DEFAULT 'ready',
                created_at REAL NOT NULL, started_at REAL, completed_at REAL,
                total_cycles INTEGER NOT NULL DEFAULT 0, error_message TEXT,
                success_criteria TEXT, auto_approve INTEGER NOT NULL DEFAULT 0)""")
            # Migrate DBs created before later columns were added.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(campaigns)")}
            if "success_criteria" not in cols:
                conn.execute("ALTER TABLE campaigns ADD COLUMN success_criteria TEXT")
            if "auto_approve" not in cols:
                conn.execute(
                    "ALTER TABLE campaigns ADD COLUMN auto_approve INTEGER NOT NULL DEFAULT 0"
                )
            if "parent_id" not in cols:
                conn.execute("ALTER TABLE campaigns ADD COLUMN parent_id TEXT")
            if "scope_constraints" not in cols:
                conn.execute("ALTER TABLE campaigns ADD COLUMN scope_constraints TEXT")
            if "parallel_workers" not in cols:
                conn.execute(
                    "ALTER TABLE campaigns ADD COLUMN parallel_workers "
                    "INTEGER NOT NULL DEFAULT 1"
                )
            if "report_artifact_slug" not in cols:
                conn.execute("ALTER TABLE campaigns ADD COLUMN report_artifact_slug TEXT")
            # RL v2: dual execution mode + recursive-exploration budget. NOT NULL
            # with a DEFAULT so existing rows backfill automatically (DEFAULTs
            # mirror the DEFAULT_* constants above).
            if "execution_mode" not in cols:
                conn.execute(
                    "ALTER TABLE campaigns ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'agent'"
                )
            if "max_subquestions_per_round" not in cols:
                conn.execute(
                    "ALTER TABLE campaigns ADD COLUMN max_subquestions_per_round "
                    "INTEGER NOT NULL DEFAULT 3"
                )
            if "depth_decay" not in cols:
                conn.execute(
                    "ALTER TABLE campaigns ADD COLUMN depth_decay REAL NOT NULL DEFAULT 0.5"
                )
            if "reserve_fraction" not in cols:
                conn.execute(
                    "ALTER TABLE campaigns ADD COLUMN reserve_fraction REAL NOT NULL DEFAULT 0.15"
                )
            # Explicit per-campaign model pick ('' = inherit the research
            # agent's / backend's default — never a hardcoded id).
            if "model" not in cols:
                conn.execute("ALTER TABLE campaigns ADD COLUMN model TEXT NOT NULL DEFAULT ''")
            conn.commit()
            _INITIALIZED_DBS.add(key)
        except Exception:
            conn.rollback()
            raise


# The worker is *prompted* to write findings as `cycle_NNN.json` (NNN zero-padded
# to 3 digits). But it's an LLM driving a file interface, so near-miss filenames
# happen — especially when a dropped mid-cycle write forces an improvised recovery
# turn (the agent re-derives the name from scratch and drifts on padding, the
# `_`/`-` separator, or case). A strict `glob("cycle_*.json")` silently ignores
# those files, so a campaign that IS producing findings reads as 0/stalled forever.
# Tolerate the realistic deviations and sort by the captured cycle number (a plain
# lexical sort also mis-orders unpadded names: `cycle_10` < `cycle_2`).
_CYCLE_FILE_RE = re.compile(r"^cycle[_-]?(\d+)\.json$", re.IGNORECASE)


def _cycle_index(path: Path) -> int:
    """Cycle number parsed from a finding filename, or -1 if it doesn't match."""
    m = _CYCLE_FILE_RE.match(path.name)
    return int(m.group(1)) if m else -1


def _cycle_finding_files(findings_dir: Path) -> list[Path]:
    """All cycle-finding files in a dir, ordered by cycle number (oldest first).

    Matches the canonical `cycle_NNN.json` plus tolerated near-misses
    (`cycle_7.json`, `cycle-007.json`, `Cycle_007.JSON`). One file per logical
    cycle: if multiple name variants parse to the same cycle number (e.g.
    `cycle_001.json` + `cycle-1.json`), only the lexically-first name is kept so
    duplicates can't inflate cycle counts or surface twice.

    SECURITY: this only widens which files are *discovered*; it does not bypass
    redaction. Every content-surfacing reader still routes each matched file
    through `_redact_finding()` (credentials + exfiltration URLs, fail-closed) —
    `get_findings()` for the dashboard and `_read_finding_file()` for the watchdog
    SSE feed — so a near-miss-named finding is scrubbed exactly like a canonical
    one before it reaches any external surface. (`check_stagnation()` reads only
    the integer `new_findings_count` and surfaces nothing.)
    """
    if not findings_dir.exists():
        return []
    # Glob ALL entries (not "*.json") so the case-insensitive regex governs the
    # match — Path.glob is case-sensitive, so "*.json" would miss "Cycle_002.JSON".
    matched = [(p, _cycle_index(p)) for p in findings_dir.glob("*") if p.is_file()]
    matched = [(p, i) for p, i in matched if i >= 0]
    by_cycle: dict[int, Path] = {}
    for p, i in sorted(matched, key=lambda t: (t[1], t[0].name)):
        by_cycle.setdefault(i, p)
    return [by_cycle[i] for i in sorted(by_cycle)]


def _campaign_dir(campaign_id: str) -> Path:
    """Create and return campaign dir. Only call with validated IDs."""
    d = research_dir() / campaign_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "findings").mkdir(exist_ok=True)
    return d


def _read_text_or_missing(path: Path) -> str | None:
    """Read *path*, or return ``None`` when it does not exist.

    Blocking; call through ``asyncio.to_thread``. The existence check rides
    with the read so the pair costs one worker hop and a file removed between
    them reads as missing rather than raising. Any OTHER ``OSError`` still
    propagates, so an unreadable file keeps its 500 rather than being
    downgraded to "no findings yet".

    UTF-8 is pinned because the file is agent-written prose (FINDINGS.md): on
    Windows the default locale encoding is the ANSI code page, so an em dash or
    a CJK character would raise. ``errors="replace"`` keeps a partially
    corrupt report readable instead of turning an export into a 500.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None


def _read_json_or_missing(path: Path) -> Any:
    """Parse a JSON file, or return ``None`` when it is missing or unusable.

    Blocking; call through ``asyncio.to_thread``. Both callers already treated
    a corrupt file as "no data", so the parse error is folded in here.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _write_text(path: Path, text: str) -> None:
    """Write *text* to *path*. Blocking; call off-loop.

    Deliberately does NOT create missing parents: both callers wrote into an
    existing campaign directory before the off-loop move, so a concurrent
    campaign deletion must keep winning (recreating the directory here would
    resurrect a deleted campaign's data).

    UTF-8 is pinned: the payload is LLM prose (FINDINGS.md,
    findings_for_knowledge.md), so the Windows ANSI code page would raise
    UnicodeEncodeError on the first em dash or CJK character and no report
    would ever be produced.
    """
    path.write_text(text, encoding="utf-8")


def _write_new_cycle_files(pending: list[tuple[Path, str]]) -> bool:
    """Write each cycle file that does not exist yet. Blocking; call off-loop.

    The "already written by an earlier poll" check stays with the write it
    guards, so idempotence costs no per-cycle stat on the event loop. Returns
    whether anything was written.
    """
    wrote = False
    for fpath, text in pending:
        if fpath.exists():
            continue
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(text, encoding="utf-8")
        wrote = True
    return wrote


def _copy_parent_findings(src: Path, dst: Path) -> None:
    """Seed a forked campaign with its parent's findings. Blocking; call off-loop."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Agent-written prose on both ends: pin UTF-8 so a fork does not lose the
        # parent's context to a locale-encoding error, and absorb bad bytes.
        content = src.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    dst.write_text(content, encoding="utf-8")


def _unlink_if_present(path: Path) -> bool:
    """Remove *path*, reporting whether it was there. Blocking; call off-loop."""
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _questions_path(campaign_id: str) -> Path | None:
    """Path to the agent's pending clarification question (if any)."""
    d = _safe_campaign_dir(campaign_id)
    return (d / "questions.json") if d else None


def _pending_question(campaign_id: str) -> str | None:
    """Read the agent's pending clarification question text, if present."""
    p = _questions_path(campaign_id)
    if not p or not p.exists():
        return None
    try:
        # The agent authors questions.json and its clarification text is very
        # often non-ASCII, so UTF-8 must be explicit: a locale-encoding failure
        # here 500s get_campaign and strands the campaign in NEEDS_INPUT.
        raw = p.read_text(encoding="utf-8", errors="replace")
        return str(json.loads(raw).get("question", "")) or None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def write_status(campaign_id: str, status: str, **extra: Any) -> None:
    if not _validate_campaign_id(campaign_id):
        return
    d = _campaign_dir(campaign_id)
    (d / "status.json").write_text(
        json.dumps(
            {"status": status, "campaign_id": campaign_id, "ts": time.time(), **extra},
            indent=2,
        ),
        encoding="utf-8",
    )


def write_guidance(campaign_id: str, text: str) -> None:
    if not _validate_campaign_id(campaign_id):
        return
    d = _campaign_dir(campaign_id)
    # User-typed mid-campaign guidance — non-ASCII is the norm, not the edge case.
    (d / "guidance.txt").write_text(text, encoding="utf-8")


def get_findings(campaign_id: str) -> list[dict]:
    d = _safe_campaign_dir(campaign_id)
    if not d:
        return []
    findings_dir = d / "findings"
    if not findings_dir.exists():
        return []
    results = []
    for f in _cycle_finding_files(findings_dir):
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
            results.append(untrusted._redact_finding(json.loads(raw)))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
    return results


def _list_cycle_files(campaign_id: str) -> list[Path]:
    """Return cycle finding paths ordered by cycle number (newest last) WITHOUT
    reading them.

    Used by the watchdog for a cheap O(1)-read count on every poll; the actual
    file is only parsed (via _read_finding_file) when the count advances.
    """
    safe_dir = _safe_campaign_dir(campaign_id)
    findings_dir = (safe_dir / "findings") if safe_dir else None
    if not findings_dir or not findings_dir.exists():
        return []
    return _cycle_finding_files(findings_dir)


def _read_finding_file(path: Path) -> dict:
    """Read + redact a single cycle finding file; {} on parse/IO/shape error.

    The file is LLM-written, so valid-but-wrong-shape JSON (`[]`, a bare
    string) is as reachable as malformed JSON. `_redact_finding` requires a
    dict (`.items()`), so a non-object payload must be rejected here — letting
    it raise would abort the watchdog iteration mid-cycle (e.g. the stall
    verdict would never settle the campaign, leaving it RUNNING forever).

    UTF-8 is pinned but bad bytes are NOT replaced, deliberately: this reader
    feeds the stall verdict, so a genuinely corrupt file must keep reading as
    absent ({}) rather than as mojibake. Before the explicit encoding a
    perfectly valid UTF-8 finding hit that same {} branch on a Windows ANSI
    console, so the watchdog saw zero new findings and failed a healthy
    campaign as stalled.
    """
    data = read_json_or(path, {}, logger=logger, what="research finding")
    if not isinstance(data, dict):
        return {}
    return untrusted._redact_finding(data)


def _redact_campaign(campaign: dict) -> dict:
    """Redact user/LLM-generated fields in campaign metadata."""
    for field in ("question", "name", "error_message", "success_criteria", "pending_question"):
        if isinstance(campaign.get(field), str):
            campaign[field] = untrusted._redact_finding({"v": campaign[field]})["v"]
    # sub_questions/sources are JSON-encoded lists — decode, redact, re-encode.
    for field in ("sub_questions", "sources"):
        raw = campaign.get(field)
        if isinstance(raw, str):
            try:
                items = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            campaign[field] = json.dumps(untrusted._redact_finding({"v": items})["v"])
    return campaign


def get_campaign(campaign_id: str) -> dict | None:
    if not _validate_campaign_id(campaign_id):
        return None
    db = _get_db()
    row = db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    db.close()
    if not row:
        return None
    return _redact_campaign(
        {
            **dict(row),
            "findings": get_findings(campaign_id),
            "pending_question": _pending_question(campaign_id),
        }
    )


def list_campaigns() -> list[dict]:
    db = _get_db()
    rows = db.execute("SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()
    db.close()
    return [_redact_campaign(dict(r)) for r in rows]


def _campaign_execution_mode(campaign_id: str) -> str:
    db = _get_db()
    row = db.execute("SELECT execution_mode FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    db.close()
    return (row["execution_mode"] if row else DEFAULT_EXECUTION_MODE) or DEFAULT_EXECUTION_MODE
