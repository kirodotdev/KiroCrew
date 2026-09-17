"""Zoom error classification and credential redaction (vendor logic).

This module folds the GENERIC error-envelope and redaction concerns into the
W01 control plane and keeps only the Zoom-SPECIFIC semantics here:

* Classification targets the shared RUN-01 taxonomy
  (:data:`kiro_crew.connections.control_plane.ErrorClass`) -- a stream classifies
  its failures into that twelve-value closed set, it does not invent its own.
  Zoom's own contribution is the *mapping*: which Zoom code/status means which
  RUN-01 class, and in particular that code ``3001`` maps to ``ambiguous``
  because it cannot tell a genuine "meeting/webinar does not exist" apart from
  an un-double-encoded UUID Zoom could not resolve (see
  :mod:`kiro_crew.connections.vendors.zoom.identity`). Reading every ``3001`` as
  ``not_found`` is the false-negative this mapping exists to prevent.

* The typed failure a caller returns is the shared
  :class:`kiro_crew.connections.control_plane.OperationError`, built through
  :func:`kiro_crew.connections.control_plane.operation_error` so its ``detail``
  is redacted-then-truncated by the site-wide discipline -- this module opens no
  second error-text channel.

What stays vendor logic is the Zoom-SHAPE credential detector
(:func:`contains_zoom_credential`): signed ``download_url``/``play_url`` media
URLs (short-lived tokens in the query string) and OAuth bearer access/refresh
tokens are Zoom's own wire shapes, so recognizing them is a Zoom concern. It is
a detector the tests call to PROVE that no Zoom credential shape reaches a log
or an error string; the actual scrubbing of an error ``detail`` is the control
plane's ``redacted_detail`` (which runs the site-wide credential + exfil
scanners), not a second redactor here.

See ``docs/system-specs/modules/connector-zoom.md``.
"""

from __future__ import annotations

import re
from typing import Optional

from kiro_crew.connections.control_plane import (
    ErrorClass,
    OperationError,
    operation_error,
)

# --- Zoom -> RUN-01 classification -----------------------------------------

# Data-driven Zoom-code-to-RUN-01-class table: adding a newly-observed Zoom code
# is a data change, not a control-flow edit. 3001 is intentionally mapped to
# ``ambiguous`` (NOT ``not_found``) -- it carries the encoding-versus-absence
# ambiguity the identity unit's double-encoding rule creates.
_ZOOM_CODE_TO_CLASS: dict[int, ErrorClass] = {
    3001: "ambiguous",
    124: "auth",
    1001: "not_found",
    200: "forbidden",
    2314: "forbidden",
    300: "input",
    429: "throttle",
}

# The Zoom "meeting/webinar does not exist" ambiguity: 3001 is the code that is
# ambiguous between a real absence and an un-double-encoded UUID. Exposed so a
# caller can special-case the re-encode-and-retry decision without re-hardcoding
# the literal.
AMBIGUOUS_ABSENCE_CODE = 3001


def classify_error(error_code: Optional[int], http_status: Optional[int] = None) -> ErrorClass:
    """Classify a Zoom failure into the shared RUN-01 :data:`ErrorClass`.

    ``error_code`` is Zoom's own business code (the ``code`` field). When it is
    absent, ``http_status`` provides a fallback (401->auth, 403->forbidden,
    404->not_found, 429->throttle, 5xx->temporary). Zoom code ``3001`` always
    resolves to ``ambiguous``, never to ``not_found``, so the
    encoding-versus-absence ambiguity is preserved for the caller. An
    unclassified failure resolves to ``temporary`` -- the safe retryable default
    -- rather than being guessed into a more specific class.
    """
    if error_code is not None and error_code in _ZOOM_CODE_TO_CLASS:
        return _ZOOM_CODE_TO_CLASS[error_code]

    if http_status is not None:
        if http_status == 401:
            return "auth"
        if http_status == 403:
            return "forbidden"
        if http_status == 404:
            return "not_found"
        if http_status == 429:
            return "throttle"
        if 500 <= http_status < 600:
            return "temporary"

    return "temporary"


def zoom_operation_error(
    error_code: Optional[int],
    detail: str,
    http_status: Optional[int] = None,
) -> OperationError:
    """Build a control-plane :class:`OperationError` from a Zoom failure.

    Classifies via :func:`classify_error`, then redacts the detail in TWO
    composed passes before it becomes a typed error: first
    :func:`redact_zoom_secrets` scrubs the Zoom-SHAPE credentials the site-wide
    scanner does not recognize (signed ``/rec/`` media URLs, bare
    ``access_token``/``refresh_token`` fields), then the control plane's
    :func:`operation_error` runs the site-wide redact-then-truncate discipline.
    The Zoom pass is load-bearing: the site-wide ``redact_and_truncate`` catches
    ``Bearer`` tokens but NOT a Zoom signed media URL or a bare token field, so
    folding into the control plane alone would leak those shapes -- the Zoom
    vendor redaction is what closes that gap (negative fault test 6).
    """
    return operation_error(classify_error(error_code, http_status), redact_zoom_secrets(detail))


# --- Zoom-shape credential detector (vendor logic) -------------------------

# A signed Zoom media URL carries a short-lived token in its query string, e.g.
# a rec/download or rec/play URL with ?pwd=, ?access_token=, or a bare signed
# path. Matched broadly: any http(s) URL whose path or query carries a Zoom
# download/play/recording token shape.
_SIGNED_URL_RE = re.compile(
    r"""https?://[^\s"'<>]*
        (?:/rec/(?:download|play|archive)/|[?&](?:access_token|pwd|zak|tk)=)
        [^\s"'<>]*""",
    re.IGNORECASE | re.VERBOSE,
)

# OAuth bearer tokens: an Authorization: Bearer <jwt-ish>, or a JSON
# access_token/refresh_token field value.
_BEARER_RE = re.compile(r"""(?ix)
        (?:bearer\s+)                       # Authorization: Bearer prefix
        [A-Za-z0-9\-._~+/]+=*               # token body
    """)
_TOKEN_FIELD_RE = re.compile(r"""(?ix)
        (?:["']?(?:access_token|refresh_token|zak|download_access_token)["']?
          \s*[:=]\s*)
        ["']?(?:[A-Za-z0-9\-._~+/]{8,}=*)["']?
    """)


def contains_zoom_credential(text: str) -> bool:
    """True when ``text`` carries a Zoom signed URL or OAuth bearer credential.

    The Zoom-shape detector behind the redaction negative-fault test: any string
    about to be logged or placed in an error is screened with this to PROVE no
    Zoom credential shape survives. The actual scrubbing of an
    :class:`OperationError` ``detail`` is the control plane's ``redacted_detail``
    (via :func:`zoom_operation_error`), not a second redactor here.
    """
    if not text:
        return False
    return bool(
        _SIGNED_URL_RE.search(text) or _BEARER_RE.search(text) or _TOKEN_FIELD_RE.search(text)
    )


_REDACTED = "[REDACTED]"

_TOKEN_FIELD_SUB_RE = re.compile(r"""(?ix)
        (?P<key>["']?(?:access_token|refresh_token|zak|download_access_token)["']?
          \s*[:=]\s*)
        ["']?(?P<val>[A-Za-z0-9\-._~+/]{8,}=*)["']?
    """)


def redact_zoom_secrets(text: str) -> str:
    """Scrub Zoom-shape credentials the site-wide scanner does not recognize.

    The vendor half of the two-pass redaction :func:`zoom_operation_error`
    applies. It removes signed ``/rec/`` media URLs whole (the token lives in
    the URL, so keeping the prefix would still leak it), replaces
    ``access_token``/``refresh_token``-style field values in place, and turns
    ``Bearer <token>`` into ``Bearer [REDACTED]``. Idempotent: after this runs,
    :func:`contains_zoom_credential` on the result is False. The control plane's
    ``redacted_detail`` then runs the site-wide scanners over the whole string,
    so the two passes together leave no Zoom credential shape behind.
    """
    if not text:
        return text
    redacted = _SIGNED_URL_RE.sub(_REDACTED, text)
    redacted = _TOKEN_FIELD_SUB_RE.sub(lambda m: f"{m.group('key')}{_REDACTED}", redacted)
    redacted = _BEARER_RE.sub(f"Bearer {_REDACTED}", redacted)
    return redacted
