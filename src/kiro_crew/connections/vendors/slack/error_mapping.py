"""Map a Slack native error string onto the shared RUN-01 error taxonomy.

Slack reports a failure as an ``ok:false`` body whose ``error`` field is a
literal string (``invalid_auth`` / ``channel_not_found`` / ``ratelimited`` ...).
The verified inventory of those strings lives, as data, in
:mod:`kiro_crew.connections.vendors.slack.errors`. This module answers the one
question that inventory deliberately does not: given a Slack native error
string, which provider-neutral class is it?

The neutral vocabulary is the RUN-01 typed error taxonomy the shared control
plane owns and exports:
:data:`kiro_crew.connections.control_plane.ErrorClass` -- the twelve-value
closed set ``auth`` / ``scope`` / ``consent`` / ``not_found`` / ``forbidden`` /
``quota`` / ``throttle`` / ``conflict`` / ``input`` / ``temporary`` /
``partial`` / ``ambiguous``. This module CONSUMES that taxonomy: it imports
:data:`~kiro_crew.connections.control_plane.ErrorClass`,
:func:`~kiro_crew.connections.control_plane.operation_error`, and
:data:`~kiro_crew.connections.control_plane.ERROR_CLASSES`, and it neither
restates the vocabulary nor forks a second enum. A provider stream classifies
its failures INTO the control plane's set so one governance/retry hook switches
on one vocabulary rather than sniffing free-text messages.

Scope of the mapping
--------------------
:data:`SLACK_ERROR_CLASSES` covers every string
:data:`kiro_crew.connections.vendors.slack.errors.NATIVE_ERROR_CODES` records,
and only strings that inventory records. Three RUN-01 classes -- ``consent``,
``partial`` (the FAILURE-side class in
:mod:`kiro_crew.connections.control_plane.errors`), and ``ambiguous`` -- have no
Slack native string, because Slack's ``ok:false`` bodies surface none: the
mapping therefore assigns none of them. A string Slack has not documented, or
one recorded outside this inventory, is unknown to this mapping;
:func:`classify_slack_error` treats an unrecognized string as ``ambiguous`` --
the same "a value the failure path should not have seen" reading the sibling
GitHub mapping gives a non-error status -- so an unmapped code degrades to a
neutral, non-actionable class rather than a wrong specific one.

Redaction discipline
--------------------
:func:`slack_operation_error` builds the typed :class:`OperationError` through
the control plane's :func:`operation_error` constructor, which is the taxonomy's
redaction boundary: it runs ``detail`` through the site-wide credential /
exfiltration-URL scanners and the 200-char cap before storing it. This module
opens no error-text channel of its own.
"""

from __future__ import annotations

from kiro_crew.connections.control_plane import (
    ERROR_CLASSES,
    ErrorClass,
    OperationError,
    operation_error,
)

#: The Slack native error string -> RUN-01 class table.
#:
#: Each key is a literal string Slack surfaces in an ``ok:false`` body's
#: ``error`` field, recorded and spelling-verified in
#: :data:`kiro_crew.connections.vendors.slack.errors.NATIVE_ERROR_CODES`. Each
#: value is the RUN-01 class that string names. The keys are exactly that
#: inventory (a test asserts equality in both directions), so a string added to
#: the inventory without a class here, or classified here without being
#: recorded there, is a failure the test surfaces rather than a silent gap.
SLACK_ERROR_CLASSES: dict[str, ErrorClass] = {
    # Auth: the token / grant itself is rejected.
    "invalid_auth": "auth",
    "not_authed": "auth",
    "account_inactive": "auth",
    "token_expired": "auth",
    "token_revoked": "auth",
    "no_authed_user": "auth",
    # Scope: a valid credential missing the OAuth scope, or a disallowed token
    # type. A missing scope is distinct from a rejected credential (``auth``)
    # and from a permission denial on an existing resource (``forbidden``): the
    # credential is accepted, but it does not carry the grant this call needs.
    "missing_scope": "scope",
    "not_allowed_token_type": "scope",
    "team_access_not_granted": "scope",
    # Not-found: the addressed resource is absent or invisible to this token.
    "channel_not_found": "not_found",
    "message_not_found": "not_found",
    "user_not_found": "not_found",
    "file_not_found": "not_found",
    "file_deleted": "not_found",
    "users_not_found": "not_found",
    "thread_not_found": "not_found",
    "unknown_method": "not_found",
    # Forbidden: the resource exists, but this subject may not act on it. The
    # credential and its scope are sufficient; the specific action is refused.
    "not_in_channel": "forbidden",
    "is_archived": "forbidden",
    "no_permission": "forbidden",
    "access_denied": "forbidden",
    "ekm_access_denied": "forbidden",
    "restricted_action": "forbidden",
    "user_is_external_guest": "forbidden",
    "cannot_dm_bot": "forbidden",
    "file_uploads_disabled": "forbidden",
    "file_uploads_except_images_disabled": "forbidden",
    # Throttle: Slack asks the caller to slow down and retry later. Both
    # documented spellings map to the same class.
    "ratelimited": "throttle",
    "rate_limited": "throttle",
    # Quota: a hard ceiling is reached; the identical call does not succeed on a
    # retry, which is what separates it from ``throttle``.
    "storage_limit_reached": "quota",
    "file_upload_size_restricted": "quota",
    "msg_too_long": "quota",
    "too_many_attachments": "quota",
    # Conflict: the operation clashes with the resource's current state. A
    # retry of the same call stays refused until that state changes.
    "already_reacted": "conflict",
    "already_in_channel": "conflict",
    "already_pinned": "conflict",
    "already_starred": "conflict",
    "message_not_modified": "conflict",
    # Input: the request itself is malformed or carries invalid arguments.
    "invalid_arguments": "input",
    "invalid_arg_name": "input",
    "invalid_array_arg": "input",
    "invalid_charset": "input",
    "invalid_form_data": "input",
    "invalid_post_type": "input",
    "missing_post_type": "input",
    "missing_argument": "input",
    "invalid_blocks": "input",
    "invalid_blocks_format": "input",
    "invalid_cursor": "input",
    "unknown_type": "input",
    "unknown_snippet_type": "input",
    "unknown_subtype": "input",
    "snippet_too_large": "input",
    "alt_txt_too_large": "input",
    "no_text": "input",
    "file_type_not_allowed": "input",
    # Temporary: transient server-side failures that may succeed on retry.
    "internal_error": "temporary",
    "fatal_error": "temporary",
    "service_unavailable": "temporary",
    "request_timeout": "temporary",
}


def classify_slack_error(code: str) -> ErrorClass:
    """Return the RUN-01 class for a Slack native error string.

    Pure mapping: the same string always yields the same class, with no I/O and
    no retry/backoff timing decision. A recognized string returns its recorded
    class; an unrecognized string returns ``ambiguous`` -- a failure classifier
    handed a string it has no evidence for cannot honestly name a specific
    class, so it degrades to the neutral, non-actionable one rather than
    guessing. The returned value is always a member of the control plane's
    :data:`~kiro_crew.connections.control_plane.ERROR_CLASSES`.
    """
    return SLACK_ERROR_CLASSES.get(code, "ambiguous")


def slack_operation_error(code: str, detail: str) -> OperationError:
    """Build a typed :class:`OperationError` from a Slack native error string.

    Classifies ``code`` with :func:`classify_slack_error`, then builds the
    envelope through the control plane's :func:`operation_error` constructor so
    ``detail`` passes the taxonomy's redaction boundary (site-wide scanners then
    the 200-char cap) on the way in. This module stores no un-redacted error
    text of its own.
    """
    return operation_error(classify_slack_error(code), detail)


# Re-exported for a caller that wants the control plane's closed set without a
# second import; it IS the control plane's tuple, not a copy.
__all__ = [
    "ERROR_CLASSES",
    "SLACK_ERROR_CLASSES",
    "classify_slack_error",
    "slack_operation_error",
]
