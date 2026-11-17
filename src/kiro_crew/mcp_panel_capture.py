"""``dashboard_capture`` on ``kirocrew-panel``: your own page as the person's tab shows it.

The tool asks ``GET /api/agent-panel/dashboard/capture``, which asks the owner's open,
visible Dashboard tab showing this Bot's page to read it: the page's visible text and
the state of its checkboxes, radios and selects (never what a person typed into a text
field, and never a password field). The tab shows a sign while it does. Nothing is written, nothing is kept, and the whole answer crosses the server's
redaction pass like every other reply (the caller passes it through ``redact``).

STATELESS: the session is resolved strictly on every call and every call asks afresh.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

TOOL_NAME: Final[str] = "dashboard_capture"
ROUTE: Final[str] = "/api/agent-panel/dashboard/capture"

#: The route waits up to five seconds for a tab, after its own caller gate.
READ_TIMEOUT_SECS: Final[float] = 15.0

TOOL_DEFINITION: Final[dict[str, Any]] = {
    "name": TOOL_NAME,
    "description": (
        "READ your Bot's own Dashboard page as the person's open tab shows it right "
        "now: its visible text and the state of its checkboxes, radios and selects, up "
        "to 16,000 characters and 64 controls. Text a person typed into a field is never "
        "read, and neither are password fields. The tab shows a sign "
        "while it reads, and only a visible tab showing your live page answers, so "
        "with no such tab open the answer is 'not captured'. Use it to check what a "
        "later push or a page script put on the page; "
        "dashboard_snapshot draws the page offline instead. Read only: nothing is "
        "saved, applied or kept. A page that runs no script cannot be read this way. "
        "Takes no arguments."
    ),
    "inputSchema": {"type": "object", "properties": {}},
}

_REASONS: Final[dict[str, str]] = {
    "no_tab": (
        "no open, visible Dashboard tab showing your live page answered; the person "
        "has to have it on screen"
    ),
    "busy": "another capture of your page is running; ask again in a moment",
    "static_page": (
        "your page runs without script here, so the tab cannot read it; "
        "dashboard_snapshot draws it instead"
    ),
    "not_loaded": "the tab has not finished loading your page; ask again in a moment",
}


def render_capture(d: dict[str, Any]) -> str:
    """The text answer for the route's reply *d* (before redaction)."""
    if d.get("error"):
        return f"Error: {d['error']}"
    raw = d.get("capture")
    capture: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if capture.get("status") != "captured":
        reason = str(capture.get("reason") or "the tab sent nothing")
        return f"Not captured: {_REASONS.get(reason, reason)}."
    lines = ["Your page as the person's tab shows it now:", "", str(capture.get("text") or "")]
    fields = capture.get("fields")
    if isinstance(fields, list) and fields:
        lines += ["", "Form controls (typed text is never read):"]
        for f in fields:
            if isinstance(f, dict):
                lines.append(f"- {f.get('label') or '(no label)'}: {f.get('value')}")
    if capture.get("truncated") is True:
        lines += ["", "(Cut at the size limit: the page holds more than this.)"]
    return "\n".join(lines)


def call_capture(sk: str, get: Callable[..., dict[str, Any]]) -> str:
    """Run ``dashboard_capture`` for the strictly resolved session *sk*."""
    return render_capture(get(ROUTE, session_key=sk, timeout=READ_TIMEOUT_SECS))
