"""W01 · L01: the RUN-01 typed error taxonomy for connector operations.

RUN-01 is the campaign's unified typed error taxonomy -- the twelve-value closed
set ``connector-capability-manifest.md`` names for the ``W05 -> W06`` edge:
``auth`` / ``scope`` / ``consent`` / ``not_found`` / ``forbidden`` / ``quota`` /
``throttle`` / ``conflict`` / ``input`` / ``temporary`` / ``partial`` /
``ambiguous``. Every provider stream classifies its failures against THIS set,
so a governance hook and a retry policy switch on one vocabulary instead of each
stream re-deriving its own -- which is exactly the string-sniffing anti-pattern
(``l1_smoke._RECONSENT_TOKENS`` matching ``"unauthorized"`` / ``"forbidden"`` in
a free-text message) RUN-01 exists to replace. Reconnecting that classifier to
RUN-01 is a SEPARATE later slice; this module only fixes the taxonomy it will
target, and does not touch ``l1_smoke``.

The twelve values are a closed set copied verbatim from the manifest. This slice
neither invents a thirteenth nor drops one; a genuinely new failure class is a
scoped revision of the manifest's own list, under its owning-spec rule.

Redaction discipline for ``detail``
-----------------------------------
A typed error carries a short, human-readable ``detail``, and that string can
reflect text a provider returned. It therefore goes through the SAME redaction
discipline every other error surface in this subsystem uses -- redact THEN
truncate, capped at the same 200 characters ``l1_smoke._redacted_detail`` caps
at (``_MAX_ERROR_CHARS = 200``). :func:`redacted_detail` is the one way to
build a ``detail``, so this module opens no new un-redacted error-text channel:
it delegates to :func:`kiro_crew.security.redact_and_truncate`, the in-repo
primitive that runs the site-wide credential / exfiltration-URL scanners over
the whole string before slicing it (truncating first would bisect a credential
straddling the boundary and leak the prefix past the regex -- the exact reason
``_redacted_detail`` redacts before it truncates).
"""

from __future__ import annotations

from typing import Literal, TypedDict

from kiro_crew.security import redact_and_truncate

#: Bumped when this taxonomy's shape changes, mirroring the sibling modules.
ERRORS_SCHEMA_VERSION = 1

#: Same 200-char cap ``l1_smoke._MAX_ERROR_CHARS`` uses, so every connector
#: error surface truncates at one length.
MAX_ERROR_CHARS = 200

# --- RUN-01: the twelve-value closed set, verbatim from the manifest -------
#: The grant is missing or the credential itself was rejected.
ERROR_AUTH = "auth"
#: The credential is valid but lacks the scope this operation needs.
ERROR_SCOPE = "scope"
#: The user must (re)consent before the operation can proceed.
ERROR_CONSENT = "consent"
#: The addressed resource does not exist.
ERROR_NOT_FOUND = "not_found"
#: The resource exists but the subject is not permitted to act on it.
ERROR_FORBIDDEN = "forbidden"
#: A hard quota was exhausted.
ERROR_QUOTA = "quota"
#: A rate limit asked the caller to slow down and retry later.
ERROR_THROTTLE = "throttle"
#: The operation conflicts with the resource's current state.
ERROR_CONFLICT = "conflict"
#: The request itself was malformed or invalid.
ERROR_INPUT = "input"
#: A transient failure that may succeed on retry.
ERROR_TEMPORARY = "temporary"
#: The operation partially applied (FAILURE side -- distinct from the success
#: envelope's ``partial`` in :mod:`kiro_crew.connections.control_plane.result`).
ERROR_PARTIAL = "partial"
#: The request was ambiguous and could not be resolved to one action.
ERROR_AMBIGUOUS = "ambiguous"

#: The RUN-01 typed error class -- a twelve-value closed set. A stream
#: classifies every failure into exactly one of these; a governance/retry hook
#: switches on it rather than sniffing a free-text message.
ErrorClass = Literal[
    "auth",
    "scope",
    "consent",
    "not_found",
    "forbidden",
    "quota",
    "throttle",
    "conflict",
    "input",
    "temporary",
    "partial",
    "ambiguous",
]

#: Tuple form of :data:`ErrorClass`'s closed set, in the manifest's own order.
ERROR_CLASSES: tuple[ErrorClass, ...] = (
    "auth",
    "scope",
    "consent",
    "not_found",
    "forbidden",
    "quota",
    "throttle",
    "conflict",
    "input",
    "temporary",
    "partial",
    "ambiguous",
)


class OperationError(TypedDict):
    """A typed failure for one connector operation invocation.

    Every field present, matching the sibling descriptors' shape.

    ``error_class`` -- exactly one RUN-01 value. ``detail`` -- a short,
    already-redacted, already-truncated human-readable note; build it ONLY with
    :func:`redacted_detail` so no un-redacted error text reaches this field.
    """

    error_class: ErrorClass
    detail: str


def redacted_detail(detail: str) -> str:
    """Redact THEN truncate an error detail, capped at :data:`MAX_ERROR_CHARS`.

    The single sanctioned way to produce an :class:`OperationError` ``detail``.
    Reuses :func:`kiro_crew.security.redact_and_truncate` -- the same
    redact-before-truncate discipline ``l1_smoke._redacted_detail`` applies --
    so a credential a provider reflected into an error message is scrubbed by
    the site-wide scanners over the full string before the 200-char slice, and
    this module never opens an un-redacted error-text channel of its own.
    """

    return redact_and_truncate(detail, max_chars=MAX_ERROR_CHARS)


def operation_error(error_class: ErrorClass, detail: str) -> OperationError:
    """Build an :class:`OperationError`, redacting ``detail`` on the way in.

    A convenience constructor so a caller cannot accidentally place raw text in
    ``detail``: the redaction is not optional, it happens here.
    """

    return {"error_class": error_class, "detail": redacted_detail(detail)}
