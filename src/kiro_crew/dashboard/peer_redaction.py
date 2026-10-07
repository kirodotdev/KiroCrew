"""The redaction sink for text that arrives from a connected peer crew.

A peer is a SEPARATE machine with its own agents, environment and secrets, so
any string it sends -- a proxied reply, a capability label, a session title --
can quote a credential or an exfiltration URL that no local redaction pass has
ever seen. Every such string runs this one chain before it is rendered,
broadcast or persisted here, so the callers share one registered sink instead of
each inventing a redactor pair.

The peer redacts its own copy with the same chain, which makes this pass
idempotent in the healthy case; that is why it is cheap enough to not depend on
the peer having done it. Both redactors return their input unchanged when
nothing matches, so clean prose passes through byte-identical.
"""

from __future__ import annotations

from kiro_crew.security import redact_credentials, redact_exfiltration_urls


def redact_peer_text(text: str) -> str:
    """Redact one peer-supplied string, in the repo's fixed order."""
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text
