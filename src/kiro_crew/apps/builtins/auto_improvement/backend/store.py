"""On-disk layout for the app's artifacts and chat-session records.

The upstream app wrote everything under its own host's data directory
via its own path module. Here the root comes from Kiro Crew's app manager
(``app_data_dir``), so it lands under ``$KIROCREW_HOME/apps/auto-improvement/data``
and honours a relocated data home.

Artifact layout is preserved from the original (the ruler/results/ledger shapes
are what the spine writes and the UI reads), with two renames carrying the
CR→PR vocabulary change:

    pr_queue/<fp>.diff, <fp>.pr.md     was cr_queue/<fp>.cr.md
    sessions/<key>.json                NEW — chat-session records (requirement 3)

``scratch`` deliberately lives OUTSIDE ``data/``: it holds the push-disabled
target clone and per-candidate worktrees, which are large, disposable, and must
never be confused with the durable record.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from kiro_crew.apps.manager import app_data_dir

logger = logging.getLogger(__name__)

APP_NAME = "auto-improvement"

#: Slot/record keys are used as filenames, so they are shape-restricted. A key
#: that does not match is rejected rather than sanitized — silently rewriting a
#: key would make two different sessions share one record.
#:
#: Length is 250, not 128: the client key is ``kind-repo-id`` (D-75, to stop
#: cross-repository collisions), so it embeds ``owner/repo`` — and GitHub allows
#: owner<=39 + repo<=100, making a legitimate PR key ~145 chars. At 128 those were
#: REJECTED by ``save_session`` after ``openSession`` had already seeded the chat
#: slot, so the link was lost and every retry spawned another orphaned chat. 250
#: clears the longest real key while keeping ``<key>.json`` (255-char component
#: limit on ext4/APFS/NTFS) safely in bounds. Raised by the GPT review.
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,249}$")


def data_dir() -> Path:
    """The app's durable data root, created on first use."""
    root = Path(app_data_dir(APP_NAME))
    root.mkdir(parents=True, exist_ok=True)
    return root


def scratch_dir() -> Path:
    """Disposable clone/worktree root — outside ``data/`` on purpose."""
    override = os.environ.get("AUTO_IMPROVEMENT_SCRATCH")
    root = Path(override).expanduser() if override else Path.home() / ".autoimprove-scratch"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _slugify(text: str) -> str:
    """A filesystem-safe workspace key from a repo display + branch.

    ``Zedmor/chess_test`` + ``origin/main`` -> ``zedmor_chess_test__main``. Empty
    input collapses to ``default`` so a run with no repo configured still has a
    home rather than writing to the data root.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return slug or "default"


def workspace_key(config: dict | None = None) -> str:
    """The repository+branch this run's findings belong to.

    Findings, the ruler, the PR queue, and profiles are all scoped to ONE
    repository+branch: switching either must show a different set, not a mixed
    pile. The key is derived from the active config so callers do not thread it
    through every path helper. ``config`` may be passed to avoid re-reading the
    file in a hot loop; otherwise it is read fresh.
    """
    cfg = config if isinstance(config, dict) else read_json(config_path(), {}) or {}
    repo = str(cfg.get("target_display") or cfg.get("target_url") or "")
    # Strip the remote prefix so ``origin/main`` and a future local ``main`` key
    # the same workspace — the branch identity is the name, not the remote.
    branch = str(cfg.get("branch") or "")
    branch = branch.split("/", 1)[1] if branch.startswith("origin/") else branch
    readable = f"{_slugify(repo)}__{_slugify(branch)}" if branch else _slugify(repo)
    # The slug alone is NOT an identity. `_slugify` maps every non-alphanumeric run to `_`,
    # so measured, `owner/a-b`, `owner/a_b`, `owner/a.b`, `owner/a--b` and `owner/a b` all
    # produced `owner_a_b` — and GitHub allows both `-` and `_`, so those are different
    # repositories. They shared a ledger, a ruler and a PR QUEUE, which means a manual draft
    # could apply one repository's queued diff to another. The digest carries the distinction
    # the slug throws away; the slug stays in front so the directory is still recognizable.
    # Lower-cased before hashing because GitHub repository names are case-insensitive, so
    # `owner/A-B` and `owner/a-b` must keep sharing one workspace. Raised by the GPT review.
    digest = hashlib.sha256(f"{repo.lower()}\x00{branch}".encode("utf-8")).hexdigest()[:10]
    return f"{readable}__{digest}"


def workspace_dir() -> Path:
    """The per-repository+branch root all run artifacts hang off.

    Under ``data/repos/<key>/``. Top-level ``config.json`` (the active target) and
    ``sessions/`` (chat links, which span repositories) stay at the data root; the
    ruler, results, PR queue, profiles, and ledger move here so a repo/branch
    switch swaps the whole set atomically.
    """
    path = data_dir() / "repos" / workspace_key()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sub(name: str) -> Path:
    path = workspace_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def ruler_dir() -> Path:
    return _sub("ruler")


def results_dir() -> Path:
    return _sub("results")


def pr_queue_dir() -> Path:
    """Durable draft-PR queue: ``<fp>.diff`` + ``<fp>.pr.md``."""
    return _sub("pr_queue")


def profiles_dir() -> Path:
    return _sub("profiles")


def sessions_dir() -> Path:
    """Chat-session records — one JSON file per linked session.

    Deliberately at the DATA ROOT, not per-workspace: a chat session is keyed by
    its own subject id and may reference any repo, so it is not scoped to the
    active one.
    """
    path = data_dir() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    return _sub("logs")


def config_path() -> Path:
    # At the data root: config names the ACTIVE repo+branch, so it cannot itself
    # live inside the per-repo subtree that name selects.
    return data_dir() / "config.json"


def ledger_path() -> Path:
    return workspace_dir() / "ledger.jsonl"


def ensure_layout() -> None:
    """Create every directory the app writes to. Idempotent."""
    for maker in (
        ruler_dir,
        results_dir,
        pr_queue_dir,
        profiles_dir,
        sessions_dir,
        logs_dir,
    ):
        maker()


# ── atomic JSON ──────────────────────────────────────────────────────────────


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write JSON via tmpfile + ``os.replace``.

    Load-bearing, not stylistic: the original used a plain ``write_text`` for the
    ruler and readers caught it mid-truncate ~31% of the time, reporting a
    calibrated ruler as ``uncalibrated``. A rename is atomic, so a reader sees
    either the old file or the new one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON, returning ``default`` on a missing or corrupt file."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ── chat-session records (requirement 3) ─────────────────────────────────────


def _validate_key(key: str) -> str:
    if not _SAFE_KEY_RE.match(key or ""):
        raise ValueError(f"unsafe session record key: {key!r}")
    return key


def session_path(key: str) -> Path:
    return sessions_dir() / f"{_validate_key(key)}.json"


def load_session(key: str) -> dict[str, Any] | None:
    """The session record for a subject key, or None when unlinked."""
    record = read_json(session_path(key))
    return record if isinstance(record, dict) else None


def save_session(key: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge ``patch`` into the record for ``key`` and return the result.

    A merge rather than a write so a caller can touch one field (e.g. bump
    ``updated_at`` on resume) without having to resend the whole record.
    """
    current = load_session(key) or {"key": key}
    current.update({k: v for k, v in patch.items() if v is not None})
    write_json_atomic(session_path(key), current)
    return current


def delete_session(key: str) -> bool:
    """Forget a session link. True when a record was removed."""
    try:
        session_path(key).unlink()
        return True
    except FileNotFoundError:
        return False


def list_sessions() -> list[dict[str, Any]]:
    """Every linked session record."""
    out: list[dict[str, Any]] = []
    for path in sorted(sessions_dir().glob("*.json")):
        record = read_json(path)
        if isinstance(record, dict):
            out.append(record)
    return out
