"""The title a harness gives its own session, read on ``AcpClient``.

On a backend in ``ACP_BACKENDS_SESSION_TITLE`` (claude), claude-agent-acp names every
session with a standard ACP ``session_info_update``, needing no client capability
(0.107.0 ``session-titles.js`` ``publish``)::

    {"sessionUpdate": "session_info_update", "title": ..., "updatedAt": ...}

It sends one once per session: at the first prompt of 10 characters or more, while
that turn runs (``onPromptQueued``), else at the end of the turn (``onTurnEnd``), and
again at the first prompt after a resume. When generation fails it sends the
session's ``summary`` instead, which for a session Crew drives is the whole first
prompt Crew sent, cut at 255 characters and an ellipsis (``sanitizeTitle``). Nothing
on the wire tells the two apart; the dashboard's consumer
(``dashboard.chat_title_harness``) refuses that shape.

:func:`note` keeps the newest title of the reader's own session, folded to one line
of printable text and redacted, at most :data:`MAX_HARNESS_TITLE_CHARS` long; a
longer one is dropped, counted and logged. It consumes no frame and makes no event:
every reader still handles the frame as it did. A frame of another session (a native
child's included) or with no string ``title`` (an AIR ``_meta`` frame, a file-change
report, ACP's ``null`` clear) changes nothing. :func:`session_title` answers the
dashboard, for an ``AcpProvider``'s client or an ``AcpSessionProvider``'s handle,
and only while the reader still runs the session the title names.

Each function here whose first parameter is ``reader`` writes ``_harness_title`` on
that ``AcpClient`` or ``AcpSessionHandle`` (created on the first title) and reads its
``backend`` and ``_session_id``.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, replace
from typing import Any

from kiro_crew.acp._dispatch import redact_text
from kiro_crew.acp.types import METHOD_SESSION_UPDATE, UPDATE_SESSION_INFO, JsonRpcMessage
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_SESSION_TITLE

logger = logging.getLogger(__name__)

#: The longest title kept, the adapter's own bound (``MAX_TITLE_LENGTH`` plus its
#: ellipsis). A longer one is not the adapter's and is dropped, not cut.
MAX_HARNESS_TITLE_CHARS = 256

#: Characters read as a space: controls (newlines, tabs, ESC), format characters
#: (bidi overrides, zero-width marks) and the line and paragraph separators.
_SPACED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


@dataclass(frozen=True)
class HarnessTitle:
    """The newest title a reader's own session was given, and the titles dropped."""

    session_id: str = ""
    title: str = ""
    dropped: int = 0


def _in_set(reader: Any) -> bool:
    backend = getattr(reader, "backend", "")
    return isinstance(backend, str) and backend in ACP_BACKENDS_SESSION_TITLE


def _one_line(text: str) -> str:
    spaced = "".join(" " if unicodedata.category(c) in _SPACED_CATEGORIES else c for c in text)
    return " ".join(spaced.split())


def _drop(reader: Any, kept: HarnessTitle, length: int) -> None:
    kept = replace(kept, dropped=kept.dropped + 1)
    reader._harness_title = kept
    logger.warning(
        "dropped an over-long session title (%d chars, bound %d, %d dropped)",
        length,
        MAX_HARNESS_TITLE_CHARS,
        kept.dropped,
    )


def note(reader: Any, msg: JsonRpcMessage) -> None:
    """Keep *msg*'s title when it is a ``session_info_update`` naming *reader*'s session."""
    if not _in_set(reader) or msg.method != METHOD_SESSION_UPDATE or msg.id is not None:
        return
    params = msg.params if isinstance(msg.params, dict) else {}
    own = getattr(reader, "_session_id", "")
    update = params.get("update")
    if not own or params.get("sessionId") != own or not isinstance(update, dict):
        return
    raw = update.get("title")
    if update.get("sessionUpdate") != UPDATE_SESSION_INFO or not isinstance(raw, str):
        return
    kept = getattr(reader, "_harness_title", None)
    if not isinstance(kept, HarnessTitle):
        kept = HarnessTitle()
    if len(raw) > MAX_HARNESS_TITLE_CHARS:
        _drop(reader, kept, len(raw))
        return
    # Folded first, so the redactor reads the text as it would be shown.
    title = _one_line(redact_text(_one_line(raw)))
    if len(title) > MAX_HARNESS_TITLE_CHARS:
        _drop(reader, kept, len(title))
        return
    if title:
        reader._harness_title = HarnessTitle(own, title, kept.dropped)


def session_title(provider: Any) -> str:
    """The title *provider*'s session was given by its harness, or ``""``.

    Peeks: it reads what the reader already holds and starts nothing. ``""`` for a
    backend outside ``ACP_BACKENDS_SESSION_TITLE``, before the first title, and once
    the reader runs another session (``new_conversation``).
    """
    inner = getattr(provider, "client", provider)
    reader = getattr(inner, "_handle", None) or inner
    if not _in_set(reader):
        return ""
    kept = getattr(reader, "_harness_title", None)
    own = getattr(reader, "_session_id", "")
    if not isinstance(kept, HarnessTitle) or not own or kept.session_id != own:
        return ""
    return kept.title
