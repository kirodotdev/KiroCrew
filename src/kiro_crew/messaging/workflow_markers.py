"""Workflow marker rows, as a linked channel thread should read them.

The workflow router (``kas_workflows/router.py``) drives the turns it starts
with three machine-written chat rows, each carrying a 32-hex token that only
authorizes the turn::

    Start workflow `name` in `/path` (launch <token>)
    Reconnect workflow runs (reconnect <token>)
    [Workflow `name` completed] Kiro Agent's reply follows (observe <token>)

The dashboard draws them as one quiet line (``KasTurnMarker.tsx``). A Slack or
other channel thread linked to that chat is sent the user row as an echo, so
without this it would show the raw row, token and working directory included.
:func:`mirror_line` turns such a row into the same short human line the
dashboard shows (English, as every channel echo is), and leaves any other text
alone. The shapes need the token, so nothing a person types becomes a marker
unless it carries one too, and even then it only changes how the echo reads.

Separately, the router appends a ``notice`` row for every run end and every
step note to a run's parent; :func:`event_line` is that row's
English text, which is also what a channel echoes.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

_LAUNCH_RE = re.compile(r"^Start workflow `([^`\n]*)` in `[^`\n]*` \(launch [0-9a-f]{32}\)\s*$")
_RECONNECT_RE = re.compile(r"^Reconnect workflow runs \(reconnect [0-9a-f]{32}\)\s*$")
_OBSERVE_RE = re.compile(r"^\[Workflow `([^`\n]*)` ([a-z]+)\] .* \(observe [0-9a-f]{32}\)\s*$")

#: An observe row's word -> the line, as ``KasTurnMarker`` words it.
_OBSERVE_LINES = {
    "completed": "Workflow {name} completed",
    "failed": "Workflow {name} failed",
    "aborted": "Workflow {name} stopped",
    "update": "Update from workflow {name}",
    "waiting": "Workflow {name} is waiting for you",
    "error": "A step in workflow {name} failed",
}


def mirror_line(text: str) -> str | None:
    """The human line for a workflow marker row, or None when *text* is not one."""
    if not isinstance(text, str):
        return None
    m = _OBSERVE_RE.match(text)
    if m:
        name = m.group(1) or "workflow"
        return _OBSERVE_LINES.get(m.group(2), "Workflow {name} ended").format(name=name)
    m = _LAUNCH_RE.match(text)
    if m:
        return f"Starting workflow {m.group(1) or 'workflow'}"
    if _RECONNECT_RE.match(text):
        return "Reconnecting workflow runs"
    return None


def mirror_text(text: str) -> str:
    """*text* as a channel echo carries it: a marker row becomes its human line."""
    return mirror_line(text) or text


def format_duration(seconds: object) -> str | None:
    """``1h 2m``, ``1m 16s``, ``9s``; None for an unknown or negative duration."""
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
        return None
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


#: An outcome row's line by run status, with and without a known duration.
_OUTCOME_LINES = {
    "completed": ("Workflow {name} completed in {duration}.", "Workflow {name} completed."),
    "failed": ("Workflow {name} failed after {duration}.", "Workflow {name} failed."),
    "aborted": ("Workflow {name} was stopped after {duration}.", "Workflow {name} was stopped."),
}
#: A step note's prefix by severity.
_NOTE_PREFIX = {"warning": "Waiting for you: ", "error": "Error: "}


def event_line(event: Mapping[str, Any]) -> str:
    """The English line of one workflow event row (``meta.kas_event``).

    The router appends such a row to a run's chat for every run end and every
    step note to the run's parent; this is its ``content``, which a linked
    channel thread echoes as is. The dashboard renders the row from its
    structured meta instead, localized. Inputs are already redacted.
    """
    name = str(event.get("name") or "workflow")
    if event.get("type") == "outcome":
        with_time, without = _OUTCOME_LINES.get(
            str(event.get("status")),
            ("Workflow {name} ended after {duration}.", "Workflow {name} ended."),
        )
        duration = format_duration(event.get("durationSecs"))
        return (with_time if duration else without).format(name=name, duration=duration)
    step = str(event.get("step") or event.get("nodeId") or "A step")
    agent = event.get("agent")
    who = f"{step} ({agent})" if agent else step
    message = " ".join(str(event.get("message") or "").split())
    prefix = _NOTE_PREFIX.get(str(event.get("severity")), "")
    line = f"{prefix}{who} in workflow {name}"
    return f"{line}: {message}" if message else f"{line}."
