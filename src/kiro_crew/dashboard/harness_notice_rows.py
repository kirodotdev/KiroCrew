"""The notice row a harness's own notice makes on the dashboard transcript.

``acp.harness_notices`` reads two kinds off a harness's frames (an advisory the
adapter composed, and the permission mode the session moved to) into a
``HarnessNotice``; :func:`notice_row` words it as the ``notice`` row the chat draws
with ``NoticeCard``, whose leading glyph is its tone: ⚠️ for a warning, ℹ️ otherwise.

An advisory's title and description are the adapter's own English, carried verbatim
with no notice token, as a native child's permission-rejection line is: the adapter
composes the whole sentence, so there is no fixed frame of Crew's to localize. A mode
change is Crew's own sentence around the mode's advertised name, so it carries
``GATEWAY_NOTICE_HARNESS_MODE_CHANGED`` and the name in ``notice_args``.

``acp.raw_sdk_tasks`` adds two more, each Crew's own sentence too: a background shell
or Monitor task that ended (``TaskEndNotice``: one token per outcome, the task's
description and what the harness's summary adds to it in ``notice_args``), and Claude
Code retrying a failed request (``RetryNotice``: the attempt, the retry budget, the
error category as Claude Code names it and the HTTP status, which the dashboard words
in the reader's language). A failed task and a retry are warnings.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.agent_sdk.drivers.acp_vocab import (
    END_COMPLETED,
    END_FAILED,
    END_STOPPED,
    NOTICE_KIND_ADAPTER,
    NOTICE_KIND_API_RETRY,
    NOTICE_KIND_MODE,
    NOTICE_KIND_TASK_ENDED,
    SEVERITY_WARNING,
    title_ends_sentence,
)
from kiro_crew.dashboard import gateway_notice as gn
from kiro_crew.dashboard.system_notices import HARNESS_ADVISORY_KIND
from kiro_crew.security import redact

#: The glyph ``NoticeCard`` reads a tone from (``parseNotice``).
_GLYPH_WARNING = "⚠️"
_GLYPH_INFO = "ℹ️"

#: A task's outcome -> its token, glyph and the word the English row uses.
_TASK_ENDS = {
    END_COMPLETED: (gn.GATEWAY_NOTICE_HARNESS_TASK_COMPLETED, _GLYPH_INFO, "completed"),
    END_FAILED: (gn.GATEWAY_NOTICE_HARNESS_TASK_FAILED, _GLYPH_WARNING, "failed"),
    END_STOPPED: (gn.GATEWAY_NOTICE_HARNESS_TASK_STOPPED, _GLYPH_INFO, "stopped"),
}


def _advisory_text(title: str, description: str) -> str:
    """The title and its description as one line, joined as the notice's text form
    joins them (``title_ends_sentence``)."""
    if not description:
        return title
    if title_ends_sentence(title):
        return f"{title} {description}"
    return f"{title}: {description}"


def notice_row(notice: Any) -> tuple[str, dict[str, Any] | None] | None:
    """The ``(content, meta)`` of the notice row *notice* makes, or None for none.

    The content is redacted here, whatever the reader did before: it reaches every
    window of the chat and the transcript on disk.
    """
    kind = getattr(notice, "kind", "")
    if kind == NOTICE_KIND_ADAPTER:
        text = _advisory_text(notice.title, notice.description)
        if not text:
            return None
        glyph = _GLYPH_WARNING if notice.severity == SEVERITY_WARNING else _GLYPH_INFO
        return redact(f"{glyph} {text}"), {"kind": HARNESS_ADVISORY_KIND}
    if kind == NOTICE_KIND_TASK_ENDED:
        return _task_row(notice)
    if kind == NOTICE_KIND_API_RETRY:
        return _retry_row(notice)
    if kind == NOTICE_KIND_MODE:
        name = redact(notice.mode_name or notice.mode_id)
        if not name:
            return None
        return (
            f"{_GLYPH_INFO} This session switched to {name} mode.",
            gn.gateway_notice_meta(gn.GATEWAY_NOTICE_HARNESS_MODE_CHANGED, mode=name),
        )
    return None


def _task_row(notice: Any) -> tuple[str, dict[str, Any]] | None:
    """``Background task <outcome>: <task>``, then `` — <summary>`` when there is one."""
    end = _TASK_ENDS.get(getattr(notice, "status", ""))
    task = redact(str(getattr(notice, "task", "") or ""))
    if end is None or not task:
        return None
    token, glyph, outcome = end
    detail = redact(str(getattr(notice, "detail", "") or ""))
    suffix = f" — {detail}" if detail else ""
    return (
        f"{glyph} Background task {outcome}: {task}{suffix}",
        gn.gateway_notice_meta(token, task=task, detail=detail),
    )


def _retry_row(notice: Any) -> tuple[str, dict[str, Any]] | None:
    """``Claude is retrying a failed request (<reason>), attempt N of M.``

    The reason is the error category in words (``rate limit``), else ``HTTP <status>``;
    ``notice_args`` carries the bare category and status for the dashboard to word.
    """
    attempt, max_retries = getattr(notice, "attempt", None), getattr(notice, "max_retries", None)
    if not isinstance(attempt, int) or not isinstance(max_retries, int):
        return None
    reason = redact(str(getattr(notice, "reason", "") or ""))
    status = getattr(notice, "status", None)
    args: dict[str, str | int] = {"attempt": attempt, "max": max_retries, "reason": reason}
    if isinstance(status, int) and not isinstance(status, bool):
        args["status"] = status
    words = reason.replace("_", " ") or (f"HTTP {status}" if "status" in args else "")
    because = f" ({words})" if words else ""
    return (
        f"{_GLYPH_WARNING} Claude is retrying a failed request{because}, "
        f"attempt {attempt} of {max_retries}.",
        gn.gateway_notice_meta(gn.GATEWAY_NOTICE_HARNESS_API_RETRY, **args),
    )
