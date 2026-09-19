"""Evidence-built sets of Slack's OWN native error strings.

What this module is
-------------------
A record of the literal error codes Slack surfaces in an ``ok:false`` body's
``error`` field, grouped by the kind of fault the string names. Every string is
copied verbatim from the official Slack method reference pages
(api.slack.com/methods/*); a new string is added by reading the page, never by
guessing or by inferring it from the shape of another string.

What this module is NOT
-----------------------
It does NOT classify a Slack error string into a taxonomy. It defines no error
enum, no classification return type, and no ``classify_*`` function, and it does
not import the connector control plane. The error-string -> classification
MAPPING is delivered separately by consuming W01's
``kiro_crew.connections.control_plane`` ``ErrorClass`` / ``operation_error``
(owner W01, tracked as ``it_d610cbb5``). This module supplies only the raw,
verified Slack-native strings that mapping reads from.

The groupings below name the fault each string describes (auth, scope, ...) so a
reader can see why Slack returns it; the group names are documentation, not a
classification the code acts on.
"""

from __future__ import annotations

#: Auth: the token/grant itself is rejected.
_AUTH_CODES = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "account_inactive",
        "token_expired",
        "token_revoked",
        "no_authed_user",
    }
)

#: Scope: a valid credential missing the OAuth scope, or a disallowed token type.
_SCOPE_CODES = frozenset(
    {
        "missing_scope",
        "not_allowed_token_type",
        "team_access_not_granted",
    }
)

#: Not-found: the addressed resource is absent or invisible to this token.
_NOT_FOUND_CODES = frozenset(
    {
        "channel_not_found",
        "message_not_found",
        "user_not_found",
        "file_not_found",
        "file_deleted",
        "users_not_found",
        "thread_not_found",
        "unknown_method",
    }
)

#: Forbidden: the resource exists, but this subject may not act on it.
_FORBIDDEN_CODES = frozenset(
    {
        "not_in_channel",
        "is_archived",
        "no_permission",
        "access_denied",
        "ekm_access_denied",
        "restricted_action",
        "user_is_external_guest",
        "cannot_dm_bot",
        "file_uploads_disabled",
        "file_uploads_except_images_disabled",
    }
)

#: Throttle: Slack asks the caller to slow down (Retry-After).
_THROTTLE_CODES = frozenset({"ratelimited", "rate_limited"})

#: Quota: a hard ceiling is reached; the same call does not succeed on retry.
_QUOTA_CODES = frozenset(
    {
        "storage_limit_reached",
        "file_upload_size_restricted",
        "msg_too_long",
        "too_many_attachments",
    }
)

#: Conflict: the operation clashes with the resource's current state.
_CONFLICT_CODES = frozenset(
    {
        "already_reacted",
        "already_in_channel",
        "already_pinned",
        "already_starred",
        "message_not_modified",
    }
)

#: Input: the request itself is malformed / carries invalid arguments.
_INPUT_CODES = frozenset(
    {
        "invalid_arguments",
        "invalid_arg_name",
        "invalid_array_arg",
        "invalid_charset",
        "invalid_form_data",
        "invalid_post_type",
        "missing_post_type",
        "missing_argument",
        "invalid_blocks",
        "invalid_blocks_format",
        "invalid_cursor",
        "unknown_type",
        "unknown_snippet_type",
        "unknown_subtype",
        "snippet_too_large",
        "alt_txt_too_large",
        "no_text",
        "file_type_not_allowed",
    }
)

#: Temporary: transient server-side failures.
_TEMPORARY_CODES = frozenset(
    {
        "internal_error",
        "fatal_error",
        "service_unavailable",
        "request_timeout",
    }
)

#: The union of every native error string recorded here. Disjoint by
#: construction: a string names one fault group, so the union's size equals the
#: sum of the group sizes (asserted by a unit test).
NATIVE_ERROR_CODES: frozenset[str] = (
    _AUTH_CODES
    | _SCOPE_CODES
    | _NOT_FOUND_CODES
    | _FORBIDDEN_CODES
    | _THROTTLE_CODES
    | _QUOTA_CODES
    | _CONFLICT_CODES
    | _INPUT_CODES
    | _TEMPORARY_CODES
)
