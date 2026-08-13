"""Shared Slack sessions view helpers.

Three Slack surfaces render the same recent-sessions list:

- ``/<command> sessions`` slash handler (``events._handle_sessions``)
- ``sessions`` keyword in DMs (``handler._handle_sessions_command``)
- App Home Tab "🧵 Sessions" section (``events._publish_home_tab``)

This module owns the data-collection (:func:`_collect_recent_sessions`)
and Block Kit rendering (:func:`_build_sessions_blocks`) so all three
surfaces share a single code path. Living in its own module — instead
of being defined inside ``events.py`` — also breaks the
``events`` ↔ ``handler`` circular import that would otherwise force
in-function imports in ``handler._handle_sessions_command``.

The module has **no slack-internal dependencies** beyond
``kiro_crew.slack.blocks.session_task_card``; it does not import
``events`` or ``handler``, which is what keeps the import graph acyclic.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.config.paths import data_home
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.slack.blocks import session_task_card

if TYPE_CHECKING:
    from kiro_crew.session import SessionManager


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage.py is the reference implementation.
_SESSIONS_DIR: Path | None = None
_SESSIONS_MAX_MSG_CHARS = 4000
_SESSIONS_MAX_PREVIEW = 5
_SESSIONS_DEFAULT_LIMIT = 10
_HOME_TAB_SESSIONS_PER_KIND = 5

_SESSION_KIND_DASHBOARD = "dashboard"
_SESSION_KIND_TASKRUNNER = "taskrunner"
_SESSION_KIND_OTHER = "other"


def _sessions_dir() -> Path:
    """Sessions directory, resolved against the live data home."""
    return _SESSIONS_DIR if _SESSIONS_DIR is not None else data_home() / "sessions"


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
    sessions_dir = _sessions_dir()
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
            key = "dashboard:" + raw_key[len("dashboard_"):]
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
            lines = jsonl.read_text(encoding="utf-8").splitlines()
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
            except (ValueError, json.JSONDecodeError):
                continue
            if d.get("_type") == "metadata":
                title = d.get("title") or title
                agent = d.get("agent") or agent
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
    """
    return await asyncio.to_thread(_collect_recent_sessions, sessions, limit=limit, kind=kind)


# ---------------------------------------------------------------------------
# Block Kit rendering
# ---------------------------------------------------------------------------


def _build_sessions_blocks(
    rows: list[dict], *, for_home_tab: bool = False
) -> list[dict]:
    """Render rows from :func:`_collect_recent_sessions` as Block Kit blocks.

    Returns task_card + actions pairs separated by dividers, using the
    shared :func:`kiro_crew.slack.blocks.session_task_card` builder so the
    slash command and ``sessions`` keyword share identical Block Kit
    output and Resume button wiring.

    *for_home_tab=True* swaps in a section-based row layout. Slack's
    ``views.publish`` API (the Home Tab surface) rejects ``task_card``
    blocks with ``unsupported type: task_card`` — they are only valid
    in message-posting APIs like ``chat.postMessage``.
    """
    blocks: list[dict] = []
    for i, row in enumerate(rows):
        # Redact title and agent here since they aren't routed through
        # session_task_card. Message content is redacted by session_task_card
        # itself: blocks._msg_elements -> security.redact_and_truncate, which
        # applies BOTH redact_exfiltration_urls() and redact_credentials() in
        # that order (exfiltration first, then credentials). Section/task-card
        # title and agent strings, plus
        # the Home-Tab section text, still need explicit redaction here
        # because they bypass _msg_elements entirely.
        safe_title, _ = redact_exfiltration_urls(row["title"])
        safe_title, _ = redact_credentials(safe_title)
        safe_agent, _ = redact_exfiltration_urls(row["agent"])
        safe_agent, _ = redact_credentials(safe_agent)
        if for_home_tab:
            blocks.extend(_session_home_tab_blocks(row, safe_title, safe_agent))
        else:
            status = "active" if row["active"] else "inactive"
            blocks.extend(
                session_task_card(
                    idx=i,
                    key=row["key"],
                    title=safe_title,
                    agent=safe_agent,
                    status=status,
                    messages=row["msgs"],
                )
            )
        if i < len(rows) - 1:
            blocks.append({"type": "divider"})
    return blocks


def _session_home_tab_blocks(
    row: dict, safe_title: str, safe_agent: str
) -> list[dict]:
    """Section + actions row for ``views.publish`` (Home Tab).

    Slack's ``views.publish`` API rejects ``task_card`` blocks, so the
    Home Tab uses a plain ``section`` with the same 🟢/⚫ status emoji
    plus the canonical ``mc_session_resume_{key}`` button.
    """
    emoji = "🟢" if row["active"] else "⚫"
    agent = safe_agent or "kirocrew"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} *{safe_title}* — _{agent} agent_",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "▶️ Resume"},
                    "action_id": f"mc_session_resume_{row['key']}",
                    "value": json.dumps({"key": row["key"], "title": safe_title}),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "⏹️ End"},
                    "action_id": f"mc_session_end_{row['key']}",
                    "value": row["key"],
                    "style": "danger",
                },
            ],
        },
    ]
