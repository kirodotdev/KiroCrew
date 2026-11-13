"""Outbound redaction: ``ctx.scrub.outbound`` for code that holds no context."""

from __future__ import annotations

from kiro_crew.apps.scrub_sdk import ScrubResult, ScrubSDK

__all__ = ["ScrubResult", "scrub_outbound"]


def scrub_outbound(text: str) -> ScrubResult:
    """Redact *text* for sending off the machine, with counts of what was removed.

    The same pass as ``ctx.scrub.outbound`` (``apps.scrub_sdk``): credentials and
    exfiltration URLs through the ACTIVE policy, so an edition companion's extra
    patterns apply. Only ``ScrubResult.text`` may leave. It raises
    ``PlatformCompositionError`` on a host whose companion could not compose;
    abandon the send when it does. Neither single pass is offered alone, as on
    ``ctx.scrub``.
    """
    return ScrubSDK().outbound(text)
