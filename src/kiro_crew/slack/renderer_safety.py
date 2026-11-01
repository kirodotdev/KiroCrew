"""Credential and URL redaction for Slack's rendered output."""

from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.security import redact_credentials, redact_exfiltration_urls


def _redact_all(text: str) -> str:
    """Both outbound redactors as one callable, in the canonical order."""
    text, _ = redact_exfiltration_urls(text)
    return redact_credentials(text)[0]


def _display_safe(text: str) -> str:
    """Redact against what Slack RENDERS, not only the bytes sent.

    The twin of the Discord renderer's ``_redact_transformed``, and applied at
    EVERY model-authored egress in the Slack renderer. Neither ``AKIA**<rest>**`` nor
    ``[AKIA](https://x)<rest>`` matches a credential pattern as written, and Slack
    renders the markup away and shows the reader an intact key -- so a literal-only
    scan is not a floor, it is a scan of one of the two forms that leave here.

    Idempotent, so applying it twice on a path (a released tail, then its append)
    costs nothing and keeps the guarantee at the sink instead of at the caller.
    """
    return redact_for_display(text, _redact_all)[0]
