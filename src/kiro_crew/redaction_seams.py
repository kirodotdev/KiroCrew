"""The output seams where the owner's second credential-redaction switch applies.

``credential_redaction.json`` carries two owner switches (``security.redaction_switch``):

* ``enabled`` -- the Files view and its diff (``redaction_switch.owner_view``).
* ``seams_enabled`` -- the seams listed in :data:`OUTPUT_SEAMS` below.

This module is the ONE list of what ``seams_enabled: false`` turns off. A seam is
a call site that wraps its credential pass in :func:`output_seam`; inside that
scope, with the switch off, ``security.redaction``'s credential plan returns no
spans, so both the plan's own passes and the structured detectors are skipped.
Every redaction call that does not open the scope keeps redacting whatever the
owner chose. That includes every refusal gate that decides by comparing
``redact(x)`` with ``x``, so a seam left off this list fails closed.

What stays on with the switch off is :data:`ALWAYS_REDACTED`. The exfiltration-URL
pass is there on purpose: it is the control that stops a prompt-injected agent
from writing a secret into a URL the dashboard renders and the browser fetches,
which is a channel to a third party, not the owner reading their own data.

Where the verdict comes from
----------------------------
A process-wide value, :func:`publish`-ed by the gateway when it boots, on every
governance refresh and on every owner PUT
(``dashboard.handlers.credential_redaction``). Nothing on a seam reads a file: a
log record or a prompt build never waits on disk. A process that never publishes
(an MCP server, a cron script, the CLI) keeps the default, ``False``, and so
keeps redacting. That is the fail direction: a stale or missing verdict redacts.

An import leaf (stdlib only): ``log_redaction`` imports it, and that module
installs at CLI bootstrap, before the ``security`` package can load.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType

#: The earlier conversation replayed into the agent's prompt.
ACP_PROMPT = "acp_prompt"
#: The shape-based passes over operational log lines.
LOGS = "logs"

#: Every output seam the switch covers, with the call sites that open it.
OUTPUT_SEAMS: Mapping[str, str] = MappingProxyType(
    {
        ACP_PROMPT: (
            "the conversation history replayed into the agent's prompt: "
            "context.build_session_replay and context_assembly.session_start"
        ),
        LOGS: (
            "the shape-based passes over log records: log_redaction.SecretRedactionFilter. "
            "Not platform.context.redact_log_via_context: its callers clip the result "
            "and send it to sinks that stay redacted"
        ),
    }
)

#: What stays redacted with the switch off.
ALWAYS_REDACTED: tuple[str, ...] = (
    "vault secret values in logs (log_redaction.SecretRedactionFilter's literal pass)",
    "the exfiltration-URL pass (security.exfil)",
    "request-blocking gates, and every refusal gate that compares redact(x) with x",
    "chat, transcripts and the live chat stream",
    "channel egress: Slack, Discord and the other messaging channels",
    "the dashboard's live log stream and its ring replay, which reach non-owner sockets "
    "(dashboard.handlers.updates_streams._stream_safe)",
    "a loaded companion's own extra patterns on log lines",
)

#: Whether the owner has turned the seams off, as last published in this process.
_seams_off = False

#: The seam the calling context is inside with the switch off, or "".
_IN_SEAM: ContextVar[str] = ContextVar("kirocrew_redaction_output_seam", default="")


def publish(off: bool) -> None:
    """Record the switch position for this process (the gateway's writers only)."""
    global _seams_off
    _seams_off = bool(off)


def seams_off() -> bool:
    """Whether the switch was last published as off in this process."""
    return _seams_off


@contextmanager
def output_seam(name: str) -> Iterator[None]:
    """Mark the enclosed credential passes as the output seam *name*.

    With the switch off, ``security.redaction_switch.credential_pass_bypassed`` is
    True inside the scope; with it on, the scope changes nothing. An unknown name
    is a programming error and raises, so a seam cannot open under a misspelling
    that this table does not list. Re-entrant and task-local.
    """
    if name not in OUTPUT_SEAMS:
        raise ValueError(f"not an output seam: {name!r}")
    if not _seams_off:
        yield
        return
    token = _IN_SEAM.set(name)
    try:
        yield
    finally:
        _IN_SEAM.reset(token)


def in_bypassed_seam() -> bool:
    """Whether the calling context is inside an output seam with the switch off."""
    return bool(_IN_SEAM.get())
