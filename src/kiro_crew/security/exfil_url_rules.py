"""The rule tables a URL is judged against.

The pattern that finds a URL in text, the query patterns that flag a
credential-like payload, the percent-encoding detector, the decode bound, the
hard credential markers scanned across the whole path and query, and the
structural validator that lets an S3 presigned URL's own signature fields
through. The classifier that applies them, ``_exfil_url_warning``, stays in
:mod:`kiro_crew.security.exfil` with the exempt-host lookup it consults.

:mod:`kiro_crew.security.exfil` is the import path of every name defined here.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from urllib.parse import parse_qs

from kiro_crew.credential_patterns import AWS_KEY_ID


def exfil_query_min_len() -> int:
    """Public view of the long-query exfiltration threshold (chars)."""
    return _EXFIL_QUERY_MIN_LEN


# ── URL Exfiltration Detection ──
# Detects URLs whose path/query contain credential-like data. We flag the
# PAYLOAD, not the destination: any URL with secrets is suspicious regardless of
# host. The general redactors have one narrow carve-out for companion-supplied
# exact tenant hosts. A separate, opt-in carve-out for standard OAuth params is
# available only to ``oauth_url_contains_credential`` on the ACP banner path.
# Fixed/encoded credentials and heavy percent encoding remain unconditional.

# Host group (group 1) matches THREE host shapes so a raw-IP exfil destination
# is not silently skipped: a DNS name with a letter TLD, a raw
# IPv4 literal (``192.168.1.1``, incl. link-local/metadata ``169.254.169.254``),
# or a bracketed IPv6 literal (``[::1]``, ``[fd00::1]``). The prior regex required
# a ``.<letters>`` TLD, so ``http://169.254.169.254/latest/…/<secret>`` never
# matched _URL_RE and its path/query was never scanned. Group 3 stays the
# path+query so the scan/redact call sites are unchanged.
_URL_HOST = (
    # A host or port never ends inside a run of letters/digits: that would split a token.
    r"[a-zA-Z0-9._-]+\.[a-zA-Z]{2,}(?![a-zA-Z0-9])"  # DNS name with a letter TLD
    r"|\d{1,3}(?:\.\d{1,3}){3}(?![a-zA-Z0-9])"  # raw IPv4 literal
    r"|\[[0-9A-Fa-f:.]+\]"  # bracketed IPv6 literal (incl. IPv4-mapped ::ffff:d.d.d.d)
)
_URL_RE = re.compile(
    r"https?://("
    + _URL_HOST
    +
    # Group 3 = path AND/OR query. It must start with ``/`` (path) OR ``?``
    # (a query attached directly to the host, no path segment). The prior
    # ``/[...]*`` required a leading slash, so ``https://host?leak=<secret>``
    # yielded group(3)=None and both scan/redact bailed on ``qmark == -1``,
    # never inspecting the query — a real exfil bypass. ``[/?]`` admits both;
    # the ``path_and_query.find("?")`` split at the call sites is unchanged.
    r")(:\d+(?![a-zA-Z0-9]))?([/?][^\s)\"'>]*)?"
)

# The scanner's own match. ``_URL_RE`` ends a URL at ``)``, ``'`` or ``>``, the
# way a renderer usually does, but those bytes are legal inside a URL, so a
# scan of that span alone misses a payload placed after one
# (``https://host/a)b?data=<blob>``).
# Here the path/query is the ``_URL_RE`` span plus, when it stopped at one of
# those bytes, a tail that runs on to whitespace, a double quote, or the start
# of the next ``http(s)://<host>``, so the payload stays inside the match. The
# tail stops before a glued second URL, which is then scanned as its own match
# under its own host and never rides the first host's exemptions; the stop
# needs a real host after the inner scheme, or the second match would not
# exist. The scheme alone is case-folded, as RFC 3986 matches it, so an
# uppercase ``HTTPS://`` is scanned; the fold is ASCII-only (``(?ai:...)``) and
# scoped to the scheme, so ``\s`` still ends a URL at any Unicode space, as in
# ``_URL_RE``. Groups match ``_URL_RE``. :func:`_url_scan_candidates` decides
# which span is judged and replaced.
_URL_SCHEME = r"(?ai:https?)://"
_URL_SCAN_RE = re.compile(
    _URL_SCHEME + r"(" + _URL_HOST + r")(:\d+(?![a-zA-Z0-9]))?"
    r"([/?][^\s)\"'>]*(?:[)'>](?:(?!" + _URL_SCHEME + r"(?:" + _URL_HOST + r"))[^\s\"])*)?)?"
)

# Bytes that end a URL the way ``_URL_RE`` reads it.
_URL_DISPLAY_STOP_RE = re.compile(r"[)'>]")

# A tail made only of these bytes is wrapper or sentence punctuation: it holds no
# payload, so the wider span is not judged for it.
_URL_PUNCTUATION_TAIL_RE = re.compile(r"[)\]}>'`.,;:!?]*")

_NON_ASCII_RUN_RE = re.compile(r"[^\x00-\x7f]+")


def _url_scan_candidates(match: re.Match[str]) -> tuple[tuple[str, str], ...]:
    """The ``(url, path_and_query)`` spans to judge for one ``_URL_SCAN_RE`` match.

    The narrow span ends where ``_URL_RE`` ends a URL, so a flagged link is
    replaced exactly as before and the text after it is kept. The wide span is
    the whole match. A caller judges the narrow span first and replaces it when
    it is flagged; otherwise it judges the wide span, so a payload past a
    ``)``, ``'`` or ``>`` is still caught, and the whole wide span is replaced.
    The wide span is skipped when the bytes it adds are punctuation only. In
    the bytes it adds, each run of non-ASCII text is judged as one space: prose
    glued to a link with no space (common in Chinese or Japanese) is not a
    payload, and the run still splits the ASCII on either side of it. The
    returned URL is the whole match either way.
    """
    url = match.group(0)
    path_and_query = match.group(3) or ""
    stop = _URL_DISPLAY_STOP_RE.search(path_and_query)
    if stop is None:
        return ((url, path_and_query),)
    tail = path_and_query[stop.start() :]
    narrow = (url[: len(url) - len(tail)], path_and_query[: stop.start()])
    if _URL_PUNCTUATION_TAIL_RE.fullmatch(tail):
        return (narrow,)
    return (narrow, (url, narrow[1] + _NON_ASCII_RUN_RE.sub(" ", tail)))


# Where a URL starts: its scheme and host, nothing after.
_URL_START_RE = re.compile(_URL_SCHEME + r"(" + _URL_HOST + r")")


def _embedded_url_matches(
    match: re.Match[str], exempt_hosts: frozenset[str]
) -> Iterator[re.Match[str]]:
    """Each URL starting inside *match* on a host not in *exempt_hosts*, in order.

    The narrow span of a ``_URL_SCAN_RE`` match does not stop at a second
    ``http(s)://`` (a redirect carries one in its query), so a glued link such
    as ``https://exempt/r?x=1`[open](https://evil/?d=<blob>)`` is one match,
    judged under the exempt first host. Each embedded URL is matched on its
    own, ending no later than *match*, so the caller judges it under its own
    host. The next one is searched for from where the last one ends: a URL
    starting inside a clean embedded match is in its path or query, which was
    judged. A match can end early (a bare ``https://decoy.example.com`` has no
    path), so the walk goes on past it. Exempt hosts are skipped with the start
    pattern alone, and no byte is searched twice, which keeps the walk linear.
    """
    text = match.string
    end = match.end()
    pos = match.start() + 1
    while pos < end:
        start = next(
            (
                s
                for s in _URL_START_RE.finditer(text, pos, end)
                if s.group(1).lower() not in exempt_hosts
            ),
            None,
        )
        if start is None:
            return
        embedded = _URL_SCAN_RE.match(text, start.start(), end)
        if embedded is None:  # the start pattern is a prefix of the scan pattern
            return
        yield embedded
        pos = max(embedded.end(), start.start() + 1)


# Query string length threshold — normal URLs rarely exceed this
_EXFIL_QUERY_MIN_LEN = 200

# Patterns that indicate secrets or encoded data in query params
_EXFIL_PATTERNS = re.compile(
    r"(?:"
    r"[A-Za-z0-9+/]{40,}={0,2}|[A-Za-z0-9+/]{39}=|[A-Za-z0-9+/]{38}=="  # base64, "=" is padding
    r"|%[0-9A-Fa-f]{2}(?:%[0-9A-Fa-f]{2}){20,}"  # heavy URL-encoding (20+ encoded chars)
    f"|{AWS_KEY_ID}"  # AWS access key ID (shared spelling: credential_patterns)
    r"|(?:ssh-rsa|ssh-ed25519)[\s+%]"  # SSH public key
    r"|BEGIN[\s+%](?:RSA|DSA|EC|OPENSSH)[\s+%]PRIVATE[\s+%]KEY"  # private key header
    r"|xox[bpas]-[0-9a-zA-Z-]+"  # Slack token
    r")",
    re.IGNORECASE,
)

# Heavy URL-encoding detector — the same "20+ consecutive percent-encoded
# octets" branch carved out of _EXFIL_PATTERNS. This stays UNCONDITIONAL: the
# context-specific exemptions in ``exfil`` skip only the base64-blob and query-length
# heuristics (which false-positive on legitimate document pointers or banner
# state/PKCE), NOT this detector, so a heavily encoded payload is still caught.
_EXFIL_PERCENT_RE = re.compile(
    r"%[0-9A-Fa-f]{2}(?:%[0-9A-Fa-f]{2}){20,}",
    re.IGNORECASE,
)


def _is_encoded_path_text(run: str) -> bool:
    """True when a percent run is UTF-8 words (a CJK headline is 3 octets a char).

    Every octet must be non-ASCII and decode as strict UTF-8, and every char
    must be a letter, mark, number or punctuation whose NFKC form is not an
    ASCII letter or digit, so encoded ASCII or fullwidth ``A``-``Z`` stays flagged.
    """
    raw = bytes.fromhex(run.replace("%", ""))
    try:
        text = raw.decode("utf-8") if min(raw) >= 0x80 else ""
    except UnicodeDecodeError:
        return False
    folds = [unicodedata.normalize("NFKC", char) for char in text]
    return bool(text) and all(
        unicodedata.category(char)[0] in "LMNP" and not (fold.isascii() and fold.isalnum())
        for char, fold in zip(text, folds)
    )


def _heavy_percent_encoding(path_and_query: str, *, allow_path_text: bool) -> bool:
    """Whether a heavy percent run is present, not counting words in the PATH.

    The path gets no base64 or length heuristic, so the same words written
    unencoded already pass; the query keeps the strict rule.
    """
    path_end = (path_and_query + "?").find("?")
    return any(
        not (allow_path_text and m.end() <= path_end and _is_encoded_path_text(m.group()))
        for m in _EXFIL_PERCENT_RE.finditer(path_and_query)
    )


# Percent-decoding passes applied when re-scanning a URL for encoded
# credentials. More than one is required because a double-encoded payload
# survives a single pass; the bound stops a deliberately over-encoded URL from
# making the scan loop indefinitely.
_MAX_URL_DECODE_PASSES = 3


# S3 presigned URLs contain X-Amz-Signature (a 64-char hex string) that
# matches the base64-like blob pattern above.  These are intentional
# time-limited access tokens, not leaked credentials.  Skip the exfil
# check when ALL standard presigned-URL query params are present on an
# amazonaws.com domain.  Values are validated to prevent spoofing.
_S3_PRESIGNED_RE = re.compile(
    r"X-Amz-Algorithm=AWS4-HMAC-SHA256"
    f".*X-Amz-Credential={AWS_KEY_ID}"  # shared spelling: credential_patterns
    r"(?:%2F|/)"
    r".*X-Amz-Expires=\d{1,6}"
    r".*X-Amz-Signature=[0-9a-f]{64}",
    re.IGNORECASE,
)

# Only these parameter keys are allowed in a presigned URL.  Any extra
# keys cause the fast-path to reject, falling through to normal checks.
_S3_PRESIGNED_PARAMS = frozenset(
    {
        "X-Amz-Algorithm",
        "X-Amz-Credential",
        "X-Amz-Date",
        "X-Amz-Expires",
        "X-Amz-SignedHeaders",
        "X-Amz-Signature",
        "X-Amz-Security-Token",
    }
)


# Structural validators for presigned param values that would otherwise
# false-positive against _EXFIL_PATTERNS.  Each value is validated rather
# than exempted, so attacker-controlled data cannot be smuggled through.
_STS_TOKEN_RE = re.compile(r"^(?:FwoGZX|IQoJb3JpZ2lu)[A-Za-z0-9+/=%]{1,2000}$")
_CREDENTIAL_RE = re.compile(
    f"^{AWS_KEY_ID}"  # shared spelling: credential_patterns
    r"(?:%2F|/)[0-9]{8}"
    r"(?:%2F|/)[a-z0-9-]+(?:%2F|/)s3(?:%2F|/)aws4_request$"
)
_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")

_STRUCTURAL_VALIDATORS = {
    "X-Amz-Credential": _CREDENTIAL_RE,
    "X-Amz-Signature": _SIGNATURE_RE,
    "X-Amz-Security-Token": _STS_TOKEN_RE,
}


def _is_safe_presigned(domain: str, query: str) -> bool:
    """Return True if the URL is a valid S3 presigned URL with no extra parameters."""
    if not domain.endswith(".amazonaws.com"):
        return False
    if not _S3_PRESIGNED_RE.search(query):
        return False
    params = parse_qs(query, keep_blank_values=True)
    if not _S3_PRESIGNED_PARAMS.issuperset(params.keys()):
        return False
    # Structurally validate params that would false-positive against
    # _EXFIL_PATTERNS.  No values are fully exempt — each is checked.
    for key, values in params.items():
        validator = _STRUCTURAL_VALIDATORS.get(key)
        if validator:
            for val in values:
                if not validator.match(val):
                    return False
        else:
            for val in values:
                if _EXFIL_PATTERNS.search(val):
                    return False
    return True


# Hard, unambiguous credential markers scanned across the FULL URL path+query
# — a real AWS key / SSH-or-PEM header / Slack token in a URL is
# exfil even to an otherwise-safe host, and even with no ``?`` query (secret in
# the PATH). Distinct from the broader _EXFIL_PATTERNS base64/length heuristics,
# which stay query-only (long base64 PATH segments — CDN asset ids, git object
# hashes — are benign).
_HARD_CREDENTIAL_RE = re.compile(
    r"(?:"
    f"{AWS_KEY_ID}"  # AWS access key ID (shared spelling: credential_patterns)
    r'|(?:SecretAccessKey|aws_secret_access_key)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r'|(?:SessionToken|aws_session_token)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r'|(?:AccessKeyId|aws_access_key_id)["\']?\s*[:=]\s*["\']?[^\s"\',}]+'
    r"|(?:ssh-rsa|ssh-ed25519)[\s+%]"  # SSH public key
    r"|BEGIN[\s+%](?:RSA|DSA|EC|OPENSSH)[\s+%]PRIVATE[\s+%]KEY"  # private key header
    r"|xox[bpas]-[0-9a-zA-Z-]+"  # Slack token
    r")",
    re.IGNORECASE,
)
