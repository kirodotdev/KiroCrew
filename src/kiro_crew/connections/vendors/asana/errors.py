"""Asana error classification: HTTP status + JSON ``errors`` -> typed category.

WHAT THIS OWNS
==============
The pure-logic mapping from an Asana error response -- an HTTP status code plus
the response body's ``errors`` array -- into a closed, typed error category set
this connector reasons about. Network-free: the caller makes the call and hands
this module the ``(status, body)`` it observed; this module classifies it. It
holds no credential and makes no call.

This IS Asana's own vendor-error taxonomy (W08 owns it), distinct from the
shared control-plane error boundary W01 defines (now merged to main: RUN-01's
``ErrorClass`` / ``operation_error()``). The two are DELIBERATELY not merged
here: this classification is the vendor-specific reading of Asana's HTTP+JSON
shape, and folding it onto the shared envelope is the next wiring slice's job,
kept out of this pure-logic module on purpose so it stays network-free and
single-concern. ``connector-asana.md`` records that deferral as a deliberate
deviation, not an oversight; the mapping is not built here.

THE CATEGORY SET
================
:class:`AsanaErrorCategory` is a closed enum. Each value is grounded in Asana's
documented HTTP status model (docs/errors: a status code plus a JSON ``errors``
array of ``{message, help, phrase}``). The mapping reads the STATUS first and
consults the ``errors`` array for detail, never the reverse.

CROSS-WORKSPACE DENIAL IS ALWAYS EXPLICIT, NEVER A SILENT FALLBACK
==================================================================
The load-bearing rule of this module: when a GID belonging to a DIFFERENT
workspace than the caller's token is scoped to is rejected, the connector
raises an explicit :class:`CrossWorkspaceDenied`. It NEVER silently retries the
call against another workspace, falls back to a default workspace, or swallows
the denial -- doing any of those would let a caller believe it read workspace
B's data when it read workspace A's, or none. The denial is a hard, surfaced
error.

Crucially, this module does NOT assert WHICH HTTP status a cross-workspace GID
lookup returns. The evidence pass could not observe whether Asana answers 403
(forbidden) or 404 (not found) for this specific case. So
:func:`raise_cross_workspace_denial` is driven by the caller's own determination
that the target GID is out-of-scope (a fact the caller knows from its own
workspace binding), NOT by pattern-matching a status code this module has no
evidence for. No test or branch here asserts 403-vs-404 for this scenario.

WHAT THIS DELIBERATELY DOES NOT OWN
===================================
No retry policy (that is :mod:`kiro_crew.connections.vendors.asana.create` for the
create case, and W01 for the general one), no live call, no offset-expiry
detection (:mod:`kiro_crew.connections.vendors.asana.pagination`). A malformed input to
the classifier (a non-int status) raises :class:`AsanaErrorShapeError`, a plain
``ValueError`` subclass -- distinct from the classified vendor conditions, which
are the enum, not exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Mapping, NoReturn, Optional


class AsanaErrorShapeError(ValueError):
    """The input handed to the classifier was itself malformed.

    A programming/shaping fault (a non-int status, a body that is not a
    mapping) -- NOT a classified vendor error. The vendor conditions are values
    of :class:`AsanaErrorCategory`, returned, not raised.
    """


class AsanaErrorCategory(str, Enum):
    """Closed set of Asana error categories this connector reasons about.

    Grounded in Asana's documented status model. ``str``-valued so a category
    round-trips through a log or an evidence record as a stable token.
    """

    #: 400 -- the request itself was malformed (bad param, invalid body).
    INVALID_REQUEST = "invalid_request"
    #: 401 -- no valid authentication was presented.
    NOT_AUTHENTICATED = "not_authenticated"
    #: 402 -- the operation needs a paid plan the account does not have
    #: (e.g. custom fields, advanced search on a free workspace).
    PAYMENT_REQUIRED = "payment_required"
    #: 403 -- authenticated but not permitted for this resource.
    FORBIDDEN = "forbidden"
    #: 404 -- the resource does not exist or is not visible to the caller.
    NOT_FOUND = "not_found"
    #: A GID from another workspace than the token's scope. Surfaced from 403 OR
    #: 404 (the exact code is unobserved) whenever the caller determines the
    #: target is out-of-workspace-scope -- NEVER a silent fallback. See
    #: :func:`raise_cross_workspace_denial`.
    CROSS_WORKSPACE_DENIED = "cross_workspace_denied"
    #: 429 -- rate limited; the caller should back off (Retry-After).
    RATE_LIMITED = "rate_limited"
    #: 5xx -- an Asana-side server error; the request may be safe to retry.
    SERVER_ERROR = "server_error"
    #: Any status this closed set does not name specifically. Preserved as a
    #: category rather than dropped, so an unclassified response is still typed.
    OTHER = "other"


#: HTTP statuses that carry no dedicated category and fall through to OTHER are
#: handled in :func:`classify_error`; this map holds only the 1:1 status rows.
_STATUS_CATEGORY = {
    400: AsanaErrorCategory.INVALID_REQUEST,
    401: AsanaErrorCategory.NOT_AUTHENTICATED,
    402: AsanaErrorCategory.PAYMENT_REQUIRED,
    403: AsanaErrorCategory.FORBIDDEN,
    404: AsanaErrorCategory.NOT_FOUND,
    429: AsanaErrorCategory.RATE_LIMITED,
}


@dataclass(frozen=True)
class AsanaError:
    """A classified Asana error response.

    ``category`` is the typed reading; ``status`` is the raw HTTP status;
    ``messages`` are the human-readable ``message`` strings from the response's
    ``errors`` array (empty if none). Immutable so it can be recorded verbatim.
    """

    category: AsanaErrorCategory
    status: int
    messages: tuple[str, ...]


def _messages_from_body(body: Optional[Mapping[str, Any]]) -> tuple[str, ...]:
    """Pull the ``message`` strings out of an Asana ``{errors: [...]}`` body.

    Asana's error body is ``{"errors": [{"message", "help", "phrase"}, ...]}``.
    A body without a well-formed ``errors`` array yields an empty tuple rather
    than raising -- a missing detail array is not itself a shape fault (some
    5xx responses carry no JSON body at all).
    """

    if not isinstance(body, Mapping):
        return ()
    errors = body.get("errors")
    if not isinstance(errors, list):
        return ()
    messages: List[str] = []
    for entry in errors:
        if isinstance(entry, Mapping):
            message = entry.get("message")
            if isinstance(message, str) and message:
                messages.append(message)
    return tuple(messages)


def classify_error(status: object, body: Optional[Mapping[str, Any]] = None) -> AsanaError:
    """Classify a raw ``(status, body)`` into a typed :class:`AsanaError`.

    Reads the STATUS to pick the category (per Asana's documented status model),
    then attaches the ``errors[].message`` strings for detail. A 5xx maps to
    ``SERVER_ERROR``; any status not otherwise named maps to ``OTHER`` -- an
    unrecognized status is preserved as a typed ``OTHER``, never dropped.

    This function does NOT special-case cross-workspace denial: that condition
    depends on a fact only the caller holds (that the target GID is
    out-of-scope), and its HTTP code is unobserved, so it is classified through
    the dedicated :func:`raise_cross_workspace_denial` instead of guessed from a
    status here.

    Raises :class:`AsanaErrorShapeError` if ``status`` is not an int (a
    programming error in the caller), since a category cannot be chosen without
    a status.
    """

    if isinstance(status, bool) or not isinstance(status, int):
        raise AsanaErrorShapeError(f"status must be an int HTTP code, got {type(status).__name__}")
    messages = _messages_from_body(body)
    category = _STATUS_CATEGORY.get(status)
    if category is None:
        if 500 <= status <= 599:
            category = AsanaErrorCategory.SERVER_ERROR
        else:
            category = AsanaErrorCategory.OTHER
    return AsanaError(category=category, status=status, messages=messages)


@dataclass(frozen=True)
class CrossWorkspaceDenied(RuntimeError):
    """A GID from a workspace outside the caller's token scope was denied.

    Raised so the caller CANNOT proceed as if the resource were reachable and
    CANNOT silently fall back to another workspace. ``status`` records the raw
    HTTP code the caller observed (403 or 404 -- either is possible and this
    class asserts neither as canonical), ``requested_gid`` and
    ``token_workspace`` name the mismatch for the operator. It is a hard denial,
    not a retryable condition.
    """

    requested_gid: str
    token_workspace: str
    status: Optional[int] = None

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        code = f" (HTTP {self.status})" if self.status is not None else ""
        return (
            f"cross-workspace access denied{code}: GID {self.requested_gid!r} is not in the "
            f"token's workspace scope {self.token_workspace!r}; refusing to fall back to "
            "another workspace"
        )


def raise_cross_workspace_denial(
    *,
    requested_gid: str,
    token_workspace: str,
    observed_status: object = None,
) -> NoReturn:
    """RAISE the explicit cross-workspace denial from the caller's own facts.

    The caller determines the target GID is out-of-scope from its own workspace
    binding (which it holds and this pure layer does not) and calls this to get
    the hard, typed denial. ``observed_status`` is recorded verbatim if the
    caller saw one, but is NOT required and NOT asserted to be any particular
    value: whether Asana returns 403 or 404 for a cross-workspace GID is
    unobserved, so this function accepts either, or none, and pins neither.

    This RAISES :class:`CrossWorkspaceDenied` rather than returning it: the hard
    denial contract is that the only path past a cross-workspace GID is an
    exception the caller cannot ignore. Returning the exception object would let
    a caller keep it as a value and continue as if the resource were reachable
    -- the exact silent fallback this module exists to forbid.
    """

    gid = requested_gid.strip()
    ws = token_workspace.strip()
    if not gid:
        raise AsanaErrorShapeError("requested_gid must be a non-empty GID")
    if not ws:
        raise AsanaErrorShapeError("token_workspace must be a non-empty workspace GID")
    status: Optional[int] = None
    if observed_status is not None:
        if isinstance(observed_status, bool) or not isinstance(observed_status, int):
            raise AsanaErrorShapeError(
                f"observed_status must be an int or None, got {type(observed_status).__name__}"
            )
        status = observed_status
    raise CrossWorkspaceDenied(requested_gid=gid, token_workspace=ws, status=status)


def is_retryable(error: AsanaError) -> bool:
    """Whether a classified error is safe to retry on its own.

    ``RATE_LIMITED`` and ``SERVER_ERROR`` are transient and retryable (with
    backoff). Everything else -- a bad request, an auth failure, a permission
    denial, a not-found, a cross-workspace denial -- is a durable condition that
    a bare retry cannot fix, so it is NOT retryable. This answers only the
    read-path question; a create's retry safety is a separate concern owned by
    :mod:`kiro_crew.connections.vendors.asana.create` (Asana offers no idempotency key,
    so a write retry is never merely "safe").
    """

    return error.category in (
        AsanaErrorCategory.RATE_LIMITED,
        AsanaErrorCategory.SERVER_ERROR,
    )
