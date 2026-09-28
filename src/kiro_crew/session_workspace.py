"""Session workspace — per-session directory for history, sub-agent results, and artifacts."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

_SESSIONS_DIR = "sessions"

_SAFE_ID_RE = __import__("re").compile(r"^[a-zA-Z0-9_.:-]+$")


def _validate_id(value: str, label: str = "id") -> str:
    if not value or ".." in value or "/" in value or "\\" in value or not _SAFE_ID_RE.fullmatch(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def is_valid_id(value: str) -> bool:
    """Whether :func:`_validate_id` would accept ``value``.

    Every function in this module validates by RAISING, which is right for an
    internal caller that has no answer for a bad id. An HTTP handler does have
    one -- a 400 -- and reaching it needs the question asked without the
    exception.

    This delegates to :func:`_validate_id` rather than re-testing
    ``_SAFE_ID_RE``, so a caller's notion of a valid id cannot drift from the
    one the path join actually enforces. Widening the validator widens this in
    the same commit, by construction.
    """
    try:
        _validate_id(value)
    except ValueError:
        return False
    return True


def workspace_dir(session_id: str) -> Path:
    """Return ~/.kiro/crew/sessions/{session_id}/, creating if needed."""
    d = config_dir() / _SESSIONS_DIR / _validate_id(session_id, "session_id")
    d.mkdir(parents=True, exist_ok=True)
    return d


def history_path(session_id: str) -> Path:
    """Path to history.jsonl within the session workspace."""
    return workspace_dir(session_id) / "history.jsonl"


def append_history(session_id: str, entry: dict) -> None:
    """Append a JSONL entry to the session's history.jsonl."""
    p = history_path(session_id)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_history(session_id: str) -> list[dict]:
    """Load all history entries for session recovery."""
    p = history_path(session_id)
    if not p.exists():
        return []
    entries = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed history line in %s", p)
    return entries


def result_path(session_id: str, agent_id: str) -> Path:
    """Path to a sub-agent result file."""
    return workspace_dir(session_id) / f"agent-{_validate_id(agent_id, 'agent_id')}.md"


def write_result(session_id: str, agent_id: str, content: str) -> Path:
    """Write sub-agent result to agent-{id}.md. Returns path."""
    p = result_path(session_id, agent_id)
    p.write_text(content, encoding="utf-8")
    return p


def append_result(session_id: str, agent_id: str, chunk: str) -> Path:
    """Append a chunk to a sub-agent result file (streaming). Returns path."""
    p = result_path(session_id, agent_id)
    with p.open("a", encoding="utf-8") as f:
        f.write(chunk)
    return p


def read_result(session_id: str, agent_id: str) -> str:
    """Read sub-agent result file. Returns empty string if not found."""
    p = result_path(session_id, agent_id)
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def list_results(session_id: str) -> list[dict]:
    """List all result files with metadata."""
    d = config_dir() / _SESSIONS_DIR / _validate_id(session_id, "session_id")
    if not d.exists():
        return []
    results = []
    for p in sorted(d.glob("agent-*.md")):
        agent_id = p.stem.removeprefix("agent-")
        try:
            size = p.stat().st_size
        except FileNotFoundError:
            continue
        results.append(
            {
                "agent_id": agent_id,
                "path": str(p),
                "size": size,
            }
        )
    return results


def cleanup(session_id: str) -> None:
    """Remove session workspace directory."""
    d = config_dir() / _SESSIONS_DIR / _validate_id(session_id, "session_id")
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
        logger.info("Cleaned up session workspace %s", session_id)
