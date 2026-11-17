"""One rewrite for the credential pass: what :func:`redact_with_findings` returns.

The rule table masks every credential value, inside a URL or not, and a URL is
never replaced whole, so there is one pass and one rewrite. ``compose`` keeps the
three-part result its callers read: the text, the credential warnings, and the URL
warnings, which are always empty (a credential inside a URL is a credential
warning like any other).

:func:`kiro_crew.security.text_redaction.redact_with_findings` is the caller.
"""

from __future__ import annotations

from kiro_crew.security import redaction


def compose(text: str) -> tuple[str, list[str], list[str]]:
    """``(redacted text, credential warnings, URL warnings)`` for *text*."""
    spans, warnings, _rules = redaction._credential_redaction_plan(text)
    if not spans:
        return text, list(warnings), []
    return redaction._splice(text, spans), list(warnings), []
