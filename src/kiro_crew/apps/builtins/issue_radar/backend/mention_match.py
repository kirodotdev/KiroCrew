"""Whether a comment addresses ``@identity``: the one rule the crew wake sweep and
both forge reads share.

The forge reads check the WHOLE comment body against the gateway's login before they
cut the body they keep, so a mention after a long log still counts; the sweep checks
what it keeps. Both use this rule, in Python (:func:`mentions`) and, for ``gh --jq``,
in Go's regexp dialect (:func:`go_pattern`), which has no lookaround.
"""

from __future__ import annotations

import re

#: A login the Go pattern may embed: GitHub's charset (letters, digits, hyphens).
_LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")


def mentions(body: str, identity: str) -> bool:
    """Whether *body* addresses ``@identity`` (case-insensitive, whole username).

    A dot ends the name only as punctuation: ``@crew-bot.`` is this account, but
    ``@crew-bot.alice`` is another one (GitLab usernames may contain dots).
    """
    if not identity:
        return False
    pattern = rf"(?<![\w@.-])@{re.escape(identity)}(?![\w-]|\.[\w-])"
    return re.search(pattern, str(body or ""), re.IGNORECASE) is not None


def go_pattern(identity: str) -> str:
    """:func:`mentions` for a lower-cased body, in Go regexp syntax; ``""`` when
    *identity* is not a plain login (the caller then asks nothing)."""
    if not _LOGIN_RE.fullmatch(identity or ""):
        return ""
    login = identity.lower()
    return rf"(?:^|[^\w@.-])@{login}(?:$|[^\w.-]|\.(?:$|[^\w-]))"
