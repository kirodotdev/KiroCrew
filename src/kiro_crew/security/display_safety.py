"""What the product may show as it is: OAuth endpoint names and binary file types.

:func:`sanitized_oauth_endpoint` names the host and path of an OAuth URL the
credential scanner rejected, so the remedy is actionable, without surfacing the
query, fragment, port or userinfo and without echoing a component that carries a
credential at any percent-decode layer. :func:`sanitized_oauth_endpoint_display`
joins that pair into one copy-ready ``host/path`` only when writing it into
``oauth_endpoints.json`` would clear the rejection. ``BINARY_MIME_ALLOWLIST`` is the
deny-by-default list of binary file types ``file_send`` and the outbox accept,
because the dashboard origin renders them.

``kiro_crew.security`` is the import path. This module reads the package's
``_submodule`` resolver, so the package imports it at its foot, once that is bound.
New display-safety rules belong here.
"""

from __future__ import annotations

import unicodedata
from urllib.parse import unquote_plus, urlparse

from kiro_crew.security import _submodule

# Longest path echoed back by ``sanitized_oauth_endpoint``. Real authorization
# endpoint paths are short (the longest builtin is 31 chars); anything past this
# bound is noise at best and smuggled payload at worst, so it is truncated with
# an ellipsis rather than surfaced whole.
_SANITIZED_OAUTH_PATH_MAX_LEN = 200

# DNS caps a full hostname at 253 octets; a longer "host" is not a hostname.
_SANITIZED_OAUTH_HOST_MAX_LEN = 253


def _contains_format_characters(text: str) -> bool:
    """True when *text* carries Unicode format characters (category Cf).

    Zero-width and directional format characters (U+200B ZERO WIDTH SPACE,
    U+200D ZWJ, U+2060 WORD JOINER, RTL/LTR marks, ...) are invisible in a
    rendered banner: a credential split by them fails every substring pattern
    here yet visually reassembles in the browser. Real authorization-endpoint
    components are plain ASCII, so their mere presence is disqualifying.
    """
    return any(unicodedata.category(ch) == "Cf" for ch in text)


def _oauth_component_is_unsafe(text: str) -> bool:
    """True when a URL component carries credential-like material at ANY decode layer.

    Mirrors the rejection gate's decode budget (``_MAX_URL_DECODE_PASSES``): the
    gate rejects a double-encoded credential on a DEEPER decode pass, so a
    sanitizer that scanned only one layer would echo the very bytes the gate
    refused. Fail-closed like the gate: a component still percent-decodable when
    the budget runs out, or one carrying a heavy percent-encoded run, is unsafe
    even when no known pattern matched.
    """
    # Owners of the re-exported names read below, resolved through the
    # import system so the value is read from the one place it lives.
    _exfil = _submodule("exfil")
    _redaction = _submodule("redaction")

    if _exfil._EXFIL_PERCENT_RE.search(text):
        return True
    candidate = text
    for _ in range(_exfil._MAX_URL_DECODE_PASSES + 1):
        # Invisible format characters (category Cf) split a credential so no
        # substring pattern below can match it, while the browser renders the
        # fragments visually reassembled. No legitimate endpoint component
        # contains them, so presence alone is unsafe — checked on every decode
        # layer because %E2%80%8B only becomes U+200B after a decode pass.
        if _contains_format_characters(candidate):
            return True
        # _EXFIL_PATTERNS is included because it is a pattern family the
        # REJECTION itself can fire on (plus-delimited private-key headers,
        # SSH keys, token shapes) — a component must never be echoed when it
        # matches what the gate refused. Over-matching only redacts more.
        if (
            _redaction._contains_fixed_credential(candidate)
            or _redaction._text_contains_bare_secret(candidate)
            or _exfil._EXFIL_PATTERNS.search(candidate)
        ):
            return True
        # unquote_plus, not unquote: form-encoded material delimits with "+"
        # (e.g. a plus-separated private-key header), which only matches the
        # credential patterns once folded to spaces. Display never uses this
        # decoded form, so the wider fold cannot distort what is surfaced.
        decoded = unquote_plus(candidate)
        if decoded == candidate:
            return False
        candidate = decoded
    # Still decodable after the budget — same deliberate fail-closed posture as
    # the gate's saturation guard: refuse to echo what cannot be fully scanned.
    return True


def sanitized_oauth_endpoint(url: str) -> tuple[str, str] | None:
    """Best-effort ``(host, path)`` of an OAuth URL, safe to surface to users.

    :func:`oauth_url_contains_credential` answers only a boolean, so its
    callers cannot tell the user WHICH endpoint tripped the scanner — and the
    remedy (``oauth_endpoints.json``) needs an exact host+path to be
    actionable. This sibling names the endpoint without weakening the
    rejection:

    * only the lowercase hostname and the path are returned — NEVER the query,
      fragment, port, or userinfo, which is where state/PKCE material and
      smuggled credentials live;
    * both components are scanned at every percent-decode layer up to the
      gate's own budget: a credential-bearing path (raw, encoded, or
      over-encoded past the budget) is replaced with the shared redaction tag,
      and a credential-bearing HOSTNAME makes the whole helper return ``None``
      — a host is an identity, so a redacted host would name nothing;
    * both components are length-capped, so a pathological URL cannot bloat a
      banner or a log line; a capped component ends in ``…`` so a reader can
      tell a chopped name from a whole one.

    Returns ``None`` when the URL does not parse to a hostname, so callers fall
    back to their existing unnamed message. Deliberately independent of WHY the
    URL was rejected: it never re-runs the credential verdict.
    """
    # Owners of the re-exported names read below, resolved through the
    # import system so the value is read from the one place it lives.
    _exfil = _submodule("exfil")
    _redaction = _submodule("redaction")

    if not url:
        return None
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except ValueError:
        return None
    if not host:
        return None
    # A userinfo-bearing authority is never named. Raw userinfo is stripped by
    # parsed.hostname, but PERCENT-ENCODED userinfo (user%3Apass%40host, or the
    # double-encoded %2540 form that survives one decode pass) hides inside
    # what urlparse reports as the hostname — check for "@" at EVERY decode
    # layer up to the gate's budget, and refuse to name an authority that is
    # still decodable when the budget runs out.
    netloc_candidate = parsed.netloc
    for _ in range(_exfil._MAX_URL_DECODE_PASSES + 1):
        if "@" in netloc_candidate:
            return None
        decoded_netloc = unquote_plus(netloc_candidate)
        if decoded_netloc == netloc_candidate:
            break
        netloc_candidate = decoded_netloc
    else:
        return None
    # Scan BEFORE truncating (both components): a credential split by a length
    # cap must still trigger redaction, not survive in half.
    host = host.lower()
    if _oauth_component_is_unsafe(host):
        return None
    if not host.isascii():
        # Surface an internationalized host in A-label (punycode) form: it
        # defuses homoglyph spoofing in the banner and matches the ASCII-only
        # shape an oauth_endpoints.json entry must take anyway.
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
        # INVARIANT: the exact byte sequence surfaced must have passed the
        # scan in its FINAL form. IDNA's nameprep folds fullwidth characters
        # to ASCII, so a token-shaped fullwidth host that the pre-IDNA scan
        # could not match can NORMALIZE INTO a credential — re-scan the
        # transformed form and refuse to name it.
        if _oauth_component_is_unsafe(host):
            return None
    if len(host) > _SANITIZED_OAUTH_HOST_MAX_LEN:
        # Marked like the path below: a silently chopped host reads as a whole
        # hostname that nothing on disk will ever match.
        host = host[:_SANITIZED_OAUTH_HOST_MAX_LEN] + "…"
    path = parsed.path or "/"
    if _oauth_component_is_unsafe(path):
        path = _redaction._REDACTED_CREDENTIAL_TAG
    elif len(path) > _SANITIZED_OAUTH_PATH_MAX_LEN:
        path = path[:_SANITIZED_OAUTH_PATH_MAX_LEN] + "…"
    return host, path


def sanitized_oauth_endpoint_display(url: str) -> str | None:
    """A rejected endpoint as one copy-ready ``host/path`` string, or ``None``.

    :func:`sanitized_oauth_endpoint` answers a diagnostic ``(host, path)`` pair
    and, by contract, may hand back a component that is NOT pasteable: the
    shared redaction tag for a credential-bearing path, or a ``…``-capped host
    or path. A surface whose whole point is "write THIS into
    ``oauth_endpoints.json``" must not join those into text that reads as
    actionable and is not.

    So this helper returns a string only when writing the entry would WORK:

    * the host matches ``_OAUTH_EXTENSION_HOST_RE`` (lowercase DNS name with a
      letter TLD — so ``localhost``, IP literals and a capped host are refused);
    * the path passes ``_valid_oauth_extension_path`` (leading ``/``, no
      ``; ? # % \\ ..`` or whitespace) and is neither redacted nor capped;
    * the rejection is one the allowlist can clear
      (:func:`oauth_rejection_is_endpoint_exemptible`): the gate is re-run as
      if the endpoint were approved, and only a URL that then PASSES is named.
      A URL refused for a fixed credential, userinfo, a fragment, path
      parameters, heavy percent-encoding, ``http`` or an explicit port would be
      refused again after the entry is added, so it stays unnamed rather than
      advertise a remedy that cannot work.

    Callers fall back to their unnamed message on ``None``. Because the
    counterfactual re-runs the gate, this can stat the operator file (memoized),
    so callers treat it like the gate itself and run it off the event loop.
    """
    # Owners of the re-exported names read below, resolved through the
    # import system so the value is read from the one place it lives.
    _exfil = _submodule("exfil")
    _redaction = _submodule("redaction")

    endpoint = sanitized_oauth_endpoint(url)
    if endpoint is None:
        return None
    host, path = endpoint
    # A capped host needs no check of its own: the host rule below ends in a
    # letter TLD, which a trailing "…" can never satisfy.
    if path == _redaction._REDACTED_CREDENTIAL_TAG or path.endswith("…"):
        return None
    if not _exfil._OAUTH_EXTENSION_HOST_RE.fullmatch(
        host
    ) or not _exfil._valid_oauth_extension_path(path):
        return None
    if not _exfil.oauth_rejection_is_endpoint_exemptible(url):
        return None
    return f"{host}{path}"


# ── Binary File MIME Allowlist ──
# Files whose UTF-8 decode fails are accepted by file_send / outbox only when
# their guessed MIME type is in this allowlist. Deny-by-default; expand only
# when a use case is reviewed for safe rendering on the dashboard origin.
# SVG is intentionally excluded (can carry inline scripts); served as
# attachment by the download handler for defense-in-depth.
BINARY_MIME_ALLOWLIST: frozenset[str] = frozenset(
    {
        "audio/mpeg",
        "audio/wav",
        "audio/x-wav",
        "audio/ogg",
        "audio/flac",
        "audio/aac",
        "audio/mp4",
        "audio/webm",
        "audio/opus",
        "video/mp4",
        "video/webm",
        "video/ogg",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "application/pdf",
    }
)
