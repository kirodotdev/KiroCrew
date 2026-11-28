"""Content checks: what the gateway's own scanners find in text an app already holds.

Each name answers about the caller's own string and rewrites nothing. They are for a
refusal, such as declining to serve or upload a file that carries a key, and for
writing the gateway's own placeholder where the app removed something itself. Text
that leaves the machine still goes through :func:`scrub_outbound`, the one redaction
pass, because neither check is a redactor and neither reads the active policy's
extra patterns.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from kiro_crew import security

__all__ = ["CREDENTIAL_PLACEHOLDER", "contains_exfiltration_url", "iter_credential_matches"]

#: The text the gateway writes where it removed a credential. Bound from the
#: gateway's own ``security.REDACTED_CREDENTIAL_TAG``, so an app that excises a span
#: itself marks it exactly as the gateway's redactor does.
CREDENTIAL_PLACEHOLDER: str = security.REDACTED_CREDENTIAL_TAG


def iter_credential_matches(text: str) -> Iterator[re.Match[str]]:
    """Each credential-shaped run in *text*, as a match on *text*, in order.

    ``kiro_crew.security.iter_credential_matches``, looked up at call time: the
    gateway's credential pattern scan, linear in the length of *text*. It matches more
    than the redactor masks (an AWS access key ID is a match here and an identifier
    there), so use it to refuse content that carries one, and :func:`scrub_outbound`
    to send text. ``any(iter_credential_matches(text))`` is the yes-or-no answer.
    """
    return security.iter_credential_matches(text)


def contains_exfiltration_url(text: str) -> bool:
    """Whether a URL in *text* looks like it carries data out, by the gateway's rules.

    ``kiro_crew.security.scan_exfiltration_urls``, looked up at call time, answered as
    a bool: a long, encoded or random-looking query, or a credential left in the URL,
    whatever its host. The private scan's warning strings quote part of the flagged
    URL, which is where an escaped token would sit, so only the answer is offered. It
    reads the edition's exempt hosts, and raises when they cannot be read: refuse the
    text when it does.
    """
    return bool(security.scan_exfiltration_urls(text))
