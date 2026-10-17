"""Shared credential / exfiltration-URL redactor for agent-authored data.

Agent-authored content (the plan queue, watch items, notify summaries, pin
labels) is persisted verbatim and can reach browser surfaces — an AKIA key or
webhook URL an LLM wrote into a free-text field must be scrubbed at every such
sink. This is the ONE canonical recursive redactor that both the HTTP routes
(``backend.routes``) and the in-process ``hooks`` import, so the two paths
cannot drift. It is a leaf module (only depends on ``kiro_crew.security``), so
both importers can take it at module scope without a circular import.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.security import redact_credentials, redact_exfiltration_urls


def redact_tree(value: Any) -> Any:
    """Recursively redact credentials + exfiltration URLs in a JSON-like value.

    Mirrors the activity-log sink, which redacts the same two ways at its write
    chokepoint. Strings are scrubbed; lists/dicts are walked; other scalars pass
    through unchanged.
    """
    if isinstance(value, str):
        redacted, _ = redact_credentials(value)
        redacted, _ = redact_exfiltration_urls(redacted)
        return redacted
    if isinstance(value, list):
        return [redact_tree(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_tree(item) for key, item in value.items()}
    return value
