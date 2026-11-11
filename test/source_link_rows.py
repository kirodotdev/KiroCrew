"""Transcript rows that make a session OWN a source link, for the chip suites.

A session card shows only links the chat owns (``source_providers/ownership.py``):
a URL its messages merely mention makes no chip. The chip suites need a slot that
carries chips, so they build it from the row a real ``gh pr create`` /
``gh issue create`` leaves: a tool row whose command ran gh and whose output is
the new URL followed by the shell's ``Exit Code: 0`` trailer.
"""

from __future__ import annotations

import json
from typing import Any


def gh_create_meta(*urls: str, exit_code: int = 0) -> dict[str, Any]:
    """The ``meta`` of one tool row that created every URL in *urls*."""
    verbs = []
    if any("/issues/" not in url for url in urls):
        verbs.append("gh pr create --fill")
    if any("/issues/" in url for url in urls):
        verbs.append("gh issue create --title t --body b")
    command = " && ".join(verbs or ["gh pr create --fill"])
    output = "Output:\n" + "\n".join(urls) + f"\n\n\nExit Code: {exit_code}"
    return {"input": json.dumps({"command": command}), "kind": "execute", "output": output}


def gh_create_row(*urls: str, ts: str = "", exit_code: int = 0) -> dict[str, Any]:
    """A whole tool-row dict, for a test that assigns ``slot.messages`` directly."""
    row: dict[str, Any] = {
        "role": "tool",
        "content": "🔧 Open the pull request",
        "meta": gh_create_meta(*urls, exit_code=exit_code),
    }
    if ts:
        row["ts"] = ts
    return row


def append_created(slot: Any, *urls: str, ts: str | None = None, **kwargs: Any) -> None:
    """Append one tool row to *slot* that created every URL in *urls*.

    Other keyword arguments (``broadcast``, ``cls``) pass through to ``append``.
    """
    kwargs["meta"] = gh_create_meta(*urls)
    if ts is not None:
        kwargs["ts"] = ts
    slot.append("tool", "🔧 Open the pull request", **kwargs)
