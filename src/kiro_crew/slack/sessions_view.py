"""Shared Slack sessions view helpers.

Three Slack surfaces render the same recent-sessions list:

- ``/<command> sessions`` slash handler (``events._handle_sessions``)
- ``sessions`` keyword in DMs (``handler._handle_sessions_command``)
- App Home Tab "🧵 Sessions" section (``events._publish_home_tab``)

The data collection is channel-neutral and lives in
:mod:`kiro_crew.messaging.sessions_view`, shared with the chat channels'
``/sessions``. This module owns the Slack half — the Block Kit rendering
(:func:`_build_sessions_blocks`) — and re-exports the collector so the three
surfaces above, and every existing monkeypatch of these names, keep resolving
here. Living in its own module also breaks the ``events`` ↔ ``handler``
circular import that would otherwise force in-function imports in
``handler._handle_sessions_command``.

**``_SESSIONS_DIR`` stays a Slack-module override.** The lazy-data-home ratchet
and the Slack suites patch this name, so the wrappers below thread it into the
neutral collector explicitly rather than shadowing the neutral module's own
override — a patch that set an attribute nothing reads would leave those tests
passing while the collector read the operator's real data home.

Beyond ``kiro_crew.slack.blocks.session_task_card`` this module has no
slack-internal dependencies; it does not import ``events`` or ``handler``,
which is what keeps the import graph acyclic.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.config.paths import data_home
from kiro_crew.messaging.sessions_view import (  # noqa: F401 — re-exported surface
    _SESSION_KIND_DASHBOARD,
    _SESSION_KIND_OTHER,
    _SESSION_KIND_TASKRUNNER,
    _SESSIONS_DEFAULT_LIMIT,
    _classify_session_key,
)
from kiro_crew.messaging.sessions_view import _collect_recent_sessions as _collect_neutral
from kiro_crew.messaging.sessions_view import (  # noqa: F401 — re-exported surface
    _default_session_title,
)
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
_HOME_TAB_SESSIONS_PER_KIND = 5


def _sessions_dir() -> Path:
    """Sessions directory, resolved against the live data home."""
    return _SESSIONS_DIR if _SESSIONS_DIR is not None else data_home() / "sessions"


# ---------------------------------------------------------------------------
# Collector (neutral implementation, Slack-owned data-home override)
# ---------------------------------------------------------------------------


def _collect_recent_sessions(
    sessions: "SessionManager | None" = None,
    *,
    limit: int = _SESSIONS_DEFAULT_LIMIT,
    kind: "str | Iterable[str] | None" = None,
) -> list[dict]:
    """Slack's view of :func:`messaging.sessions_view._collect_recent_sessions`.

    Threads this module's ``_SESSIONS_DIR`` through explicitly so a patch of it
    is what the read actually uses. Synchronous filesystem I/O — async callers
    MUST use :func:`_collect_recent_sessions_off_loop`.
    """
    return _collect_neutral(sessions, limit=limit, kind=kind, sessions_dir=_sessions_dir())


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

    Dispatches through this module's own ``_collect_recent_sessions`` so a
    monkeypatch of that name (several Slack suites use one) is honored.
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
