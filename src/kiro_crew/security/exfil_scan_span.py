"""Which URL span the exfil scanner judges and replaces.

The walk over one ``_URL_SCAN_RE`` match: its narrow span, its whole span, and
each URL embedded in it on a host the exempt set does not hold. The per-URL
verdict and the Slack app-link check stay in :mod:`kiro_crew.security.exfil`,
which passes them in, so this module never imports that facade.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from itertools import chain

from .exfil_url_rules import (
    _URL_SCAN_RE,
    _embedded_url_matches,
    _is_safe_presigned,
    _url_scan_candidates,
)

#: ``(domain, path_and_query, port, is_https) -> warning or None``.
_UrlJudge = Callable[[str, str, str, bool], "str | None"]
#: ``(domain, path, query, port, is_https) -> alias or None`` for our own Slack link.
_SlackAlias = Callable[[str, str, str, str, bool], "str | None"]


def _walk_flagged_span(
    match: re.Match[str],
    exempt_hosts: frozenset[str],
    judge: _UrlJudge,
    slack_alias: _SlackAlias,
) -> tuple[re.Match[str], str, str, str] | None:
    """``(judged_match, url, path_and_query, warning)`` for the span to replace, or None.

    Each span from ``_url_scan_candidates`` is judged narrowest first, and the
    first flagged one is returned. When *match* is clean, each URL embedded in
    it on a host *exempt_hosts* does not hold is judged the same way under its
    own host, and the first flagged one's span is replaced; the outer host's
    exemption covers only the outer span.
    """
    for judged in chain((match,), _embedded_url_matches(match, exempt_hosts)):
        domain = judged.group(1)
        port = judged.group(2) or ""
        narrow = ""
        for url, path_and_query in _url_scan_candidates(judged):
            is_https = url.lower().startswith("https://")
            judged_pq = path_and_query
            if narrow:
                tail = path_and_query[len(narrow) :]
                judged_pq = (
                    _tail_after_exempt_span(domain, narrow, tail, port, is_https, slack_alias)
                    or path_and_query
                )
            warning = judge(domain, judged_pq, port, is_https)
            if warning:
                return judged, url, path_and_query, warning
            narrow = path_and_query
    return None


def _tail_after_exempt_span(
    domain: str, narrow: str, tail: str, port: str, is_https: bool, slack_alias: _SlackAlias
) -> str | None:
    """What to judge for the wide span when a structural exemption passed *narrow*.

    A valid S3 presigned query or our own Slack app-create link is recognized
    by its exact parameter set, so bytes glued after it (``)**`` from markdown
    bold, ``)|`` from a table cell) would break that shape and undo the
    exemption the narrow span earned. In those two cases the added bytes are
    judged as a query of their own: every presigned value was already checked
    against its own validator, and the Slack alias, that link's one
    caller-controlled span, is judged with them. Anything else returns None:
    narrow plus tail is judged as one query, so a payload split across the stop
    byte is still whole.
    """
    qmark = narrow.find("?")
    if qmark == -1:
        return None
    query = narrow[qmark + 1 :]
    if query and _is_safe_presigned(domain, query):
        return "?" + tail
    alias = slack_alias(domain.lower(), narrow[:qmark], query, port, is_https)
    if alias is not None:
        return "?" + alias + tail
    return None


def _walk_span_urls(text: str, exempt_hosts: frozenset[str]) -> set[str]:
    """Every URL span the redactor can judge and replace in *text*.

    The same walk as :func:`_walk_flagged_span`: each scan match, then the URLs
    embedded in it on a non-exempt host, each read as its narrow and its whole
    span. A caller that must recognize a redacted address as its own reads them
    here instead of re-deriving the spans.
    """
    return {
        url
        for match in _URL_SCAN_RE.finditer(text)
        for judged in chain((match,), _embedded_url_matches(match, exempt_hosts))
        for url, _ in _url_scan_candidates(judged)
    }
