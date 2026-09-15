"""Channel-neutral recent-sessions collector.

Every surface that offers a "recent sessions" list — the Slack slash command,
the Slack Home Tab, and a chat channel's ``/sessions`` — needs the same three
answers about each transcript on disk: what it is called, which agent ran it,
and whether it is live. That reading is pure filesystem work with no channel in
it, so it lives here and each surface owns only its own rendering.

Dependency direction stays one-way: this module imports ``config.paths`` and
``security`` and nothing from ``kiro_crew.slack`` / ``kiro_crew.dashboard``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.config.paths import data_home
from kiro_crew.security import redact
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.session import SessionManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SESSIONS_MAX_MSG_CHARS = 4000
_SESSIONS_MAX_PREVIEW = 5
_SESSIONS_DEFAULT_LIMIT = 10

_SESSION_KIND_DASHBOARD = "dashboard"
_SESSION_KIND_TASKRUNNER = "taskrunner"
_SESSION_KIND_OTHER = "other"


def _sessions_dir() -> Path:
    """Sessions directory, resolved per call against the live data home.

    Never captured at import: an import-time binding freezes the data home and
    defeats pod isolation, the lazy legacy-home migration and test isolation.
    A caller that owns its own override passes ``sessions_dir=`` instead of
    shadowing this — there is one override knob, not two.
    """
    return data_home() / "sessions"


# ---------------------------------------------------------------------------
# Classification + default titles
# ---------------------------------------------------------------------------


def _classify_session_key(key: str) -> str:
    """Classify a session key as ``dashboard``, ``taskrunner``, or ``other``."""
    if key.startswith("dashboard:") or key.startswith("dashboard_"):
        return _SESSION_KIND_DASHBOARD
    if key.startswith("taskrunner:") or key.startswith("taskrunner_"):
        return _SESSION_KIND_TASKRUNNER
    return _SESSION_KIND_OTHER


def _default_session_title(key: str, kind: str) -> str:
    """Build a default title for a session that has no metadata title.

    The taskrunner branch drops the leading ``taskrunner_`` plus the next
    segment so that on-disk keys like ``taskrunner_run_<task_id>`` (from
    ``taskrunner.py`` after ``_safe_key`` colon→underscore mangling) render as
    ``Task Runner <task_id>`` instead of ``Task Runner run_<task_id>``.
    """
    if kind == _SESSION_KIND_DASHBOARD:
        if ":" in key:
            return f"Dashboard {key.split(':', 1)[1]}"
        # Defensive: _collect_recent_sessions normalises ``dashboard_xxx`` to
        # ``dashboard:xxx`` before classifying, so this branch is unreachable
        # via the canonical path. Kept for callers that pass raw filenames.
        if "_" in key:
            return f"Dashboard {key.split('_', 1)[1]}"
    if kind == _SESSION_KIND_TASKRUNNER:
        if ":" in key:
            return f"Task Runner {key.split(':', 2)[-1]}"
        if "_" in key:
            return f"Task Runner {key.split('_', 2)[-1]}"
    return key


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


def _collect_recent_sessions(
    sessions: "SessionManager | None" = None,
    *,
    limit: int = _SESSIONS_DEFAULT_LIMIT,
    kind: "str | Iterable[str] | None" = None,
    sessions_dir: Path | None = None,
    with_messages: bool = True,
) -> list[dict]:
    """Read JSONLs under ``<config_dir>/sessions/`` and return a sorted list.

    Each row: ``{key, title, agent, mtime, active, kind, msgs}`` where
    ``msgs`` is a list of ``{"role": str, "content": str}`` dicts (last
    ``_SESSIONS_MAX_PREVIEW`` user/assistant messages, truncated to
    ``_SESSIONS_MAX_MSG_CHARS`` chars but **not** redacted — redaction
    happens in ``_build_sessions_blocks`` via ``session_task_card``).

    *sessions* is an optional ``SessionManager``-like object exposing
    ``has_session(key) -> bool`` for the active marker. Pass ``None`` to
    skip the active check (returned ``active`` will always be ``False``).

    *kind* filters by ``_SESSION_KIND_*``. Accepts a single kind string,
    an iterable of kinds (the Home Tab uses this to fetch dashboard +
    taskrunner in a single directory scan), or ``None`` for no filter.

    Sorted by mtime descending, capped at *limit*. The kind filter and the
    mtime sort key are both derivable without opening a file (kind from the
    filename stem, mtime from ``stat``), so only the newest *limit*
    matching transcripts are actually read — the directory can hold an
    unbounded number of historical sessions without the read cost growing
    with it. Files that turn out to be empty or unreadable are skipped and
    the scan continues down the mtime order, so the result still holds
    *limit* rows whenever enough valid transcripts exist.

    This function performs synchronous filesystem I/O (directory scan plus
    up to *limit* whole-file reads, each bounded only by transcript size).
    Callers on the asyncio event loop MUST use
    :func:`_collect_recent_sessions_off_loop` instead of calling this
    directly — a multi-MB transcript read on the loop stalls every other
    task, including the loop-watchdog heartbeat.
    """
    sessions_dir = sessions_dir if sessions_dir is not None else _sessions_dir()
    if not sessions_dir.exists():
        return []

    if kind is None:
        kinds_set: set[str] | None = None
    elif isinstance(kind, str):
        kinds_set = {kind}
    else:
        kinds_set = set(kind)

    # Pre-scan: classify + stat every entry WITHOUT reading it, then sort
    # newest-first so the read loop below opens at most ``limit`` valid
    # transcripts instead of every file in the directory.
    candidates: list[tuple[float, Path, str, str]] = []
    for jsonl in sessions_dir.glob("*.jsonl"):
        if jsonl.is_symlink():
            continue
        raw_key = jsonl.stem
        # Restore canonical session key form (filenames replace ':' with '_').
        if raw_key.startswith("dashboard_"):
            key = "dashboard:" + raw_key[len("dashboard_") :]
        else:
            key = raw_key

        row_kind = _classify_session_key(key)
        if kinds_set is not None and row_kind not in kinds_set:
            continue

        try:
            mtime = jsonl.stat().st_mtime
        except OSError:
            # Deleted between glob and stat — skip.
            continue
        candidates.append((mtime, jsonl, key, row_kind))

    # Stable sort keyed on mtime only, so equal-mtime entries keep
    # directory-enumeration order (same tie order the full-scan sort had).
    candidates.sort(key=lambda c: c[0], reverse=True)

    rows: list[dict] = []
    for mtime, jsonl, key, row_kind in candidates:
        if len(rows) >= limit:
            break

        try:
            if with_messages:
                lines = jsonl.read_text(encoding="utf-8").splitlines()
            else:
                # Only the metadata is needed, and it is ALWAYS line 0: every writer
                # in ``history`` rewrites ``lines[0]`` in place rather than appending
                # a second record. So read one line instead of pulling a transcript
                # that can be multiple MB into memory to throw all but its header
                # away. This is the saving the parameter exists for; skipping only
                # the ``msgs`` append would leave the read cost untouched.
                with jsonl.open(encoding="utf-8") as fh:
                    first = fh.readline()
                lines = [first] if first else []
        except OSError:
            continue
        if not lines:
            continue

        title = ""
        agent = "kirocrew"
        msgs: list[dict] = []

        for line in lines:
            try:
                d = json.loads(line.strip())
            except ValueError:
                # Covers json.JSONDecodeError, which subclasses ValueError.
                continue
            if not isinstance(d, dict):
                continue
            if d.get("_type") == "metadata":
                title = d.get("title") or title
                agent = d.get("agent") or agent
                continue
            if not with_messages:
                continue
            role = d.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content = (d.get("content") or "")[:_SESSIONS_MAX_MSG_CHARS]
            # Upstream truncation bounds the in-memory ``rows`` list before
            # rendering; ``session_task_card._msg_elements`` truncates again
            # to the same limit when building Block Kit text.
            if content:
                msgs.append({"role": role, "content": content})

        if not title:
            title = _default_session_title(key, row_kind)

        active = bool(sessions and sessions.has_session(key))
        rows.append(
            {
                "key": key,
                "title": title[:80],
                "agent": agent,
                "mtime": mtime,
                "active": active,
                "kind": row_kind,
                "msgs": msgs[-_SESSIONS_MAX_PREVIEW:],
            }
        )

    return rows


async def _collect_recent_sessions_off_loop(
    sessions: "SessionManager | None" = None,
    *,
    limit: int = _SESSIONS_DEFAULT_LIMIT,
    kind: "str | Iterable[str] | None" = None,
    sessions_dir: Path | None = None,
    with_messages: bool = True,
) -> list[dict]:
    """Run :func:`_collect_recent_sessions` in a worker thread.

    The collector does synchronous filesystem I/O (a directory scan plus up
    to *limit* whole-transcript reads, each bounded only by transcript
    size). Run on the event loop, that starves every other task — including
    the loop-watchdog heartbeat, which hard-exits the process after
    sustained silence. This wrapper is the single chokepoint async callers
    must use; it keeps the offload decision out of each call site.

    The collector is safe to run off-loop: it is pure I/O + parsing, and
    the only shared-state touch is ``SessionManager.has_session``, a plain
    dict-membership read.

    *sessions_dir* overrides where to read, for a surface that owns its own
    data-home override; ``None`` resolves the live home per call.

    *with_messages* builds the ``msgs`` preview. Pass ``False`` when the caller
    renders only the title, agent and active marker: the preview is the whole
    reason each transcript is read to the end and JSON-parsed line by line, so a
    caller that discards it otherwise pays a multi-MB read per row for nothing.
    A ``False`` row still carries ``msgs``, as an empty list, so the shape does
    not fork.
    """
    return await asyncio.to_thread(
        _collect_recent_sessions,
        sessions,
        limit=limit,
        kind=kind,
        sessions_dir=sessions_dir,
        with_messages=with_messages,
    )


async def collect_recent_sessions_audited(
    sessions: "SessionManager | None" = None,
    *,
    caller: str,
    source: str,
    limit: int = _SESSIONS_DEFAULT_LIMIT,
    kind: "str | Iterable[str] | None" = None,
    with_messages: bool = True,
) -> list[dict] | None:
    """The collector plus its SEL audit, as one call. ``None`` = already audited.

    Reading this directory reaches into the operator's data home and hands what it
    finds to an external surface, so the read is audited — on BOTH outcomes. The
    failure path is the one that matters most and the one easiest to leave out: an
    I/O error that is not audited makes the access attempt invisible to the
    security pipeline, which is exactly when knowing someone asked is worth most.

    Every surface that lists sessions owes the same two events with the same
    resource strings, so they live here rather than being re-typed per channel —
    a fourth copy is how one of them ends up auditing only the success.

    *caller* and *source* are the audit's subject and surface (e.g. a session key
    and ``"telegram"``). Returns the rows, or ``None`` when the collector failed
    and the caller only has to choose its own wording.
    """
    try:
        rows = await _collect_recent_sessions_off_loop(
            sessions, limit=limit, kind=kind, with_messages=with_messages
        )
    except Exception as exc:
        sel().log_api_access(
            caller=caller,
            operation=f"{source}.sessions_data_access",
            outcome="error",
            source=source,
            resources="0 sessions read (collector failed)",
            # Redact-then-truncate, so truncation cannot split a credential
            # pattern out of the matcher's reach.
            error=redact(str(exc))[:200],
        )
        logger.exception("%s: recent-sessions collector failed", source)
        return None
    sel().log_api_access(
        caller=caller,
        operation=f"{source}.sessions_data_access",
        outcome="allowed",
        source=source,
        resources=f"{len(rows)} sessions read",
    )
    return rows
