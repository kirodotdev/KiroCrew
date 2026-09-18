"""GitHub connector: per-operation instance data and GitHub-specific wire
parsing for the connector campaign's W02 stream.

This package interprets GitHub's own API surface -- its two pagination
contracts, its rate-limit headers, the mapping from its HTTP status/body onto
the shared control plane's neutral error classes, and the mapping from a
capability signature to a campaign operation_id -- and carries the instance
data for each required operation. It builds no request and holds no credential:
real authorization and transport are a later stream, gated on the shared
control plane's landed interfaces. It consumes the control plane's ``Effect``
and ``ErrorClass`` vocabularies rather than restating them.
"""

from .decoder import (
    GithubDecodeError,
    decode_cursor_page,
    decode_rest_page,
    decode_single,
)
from .descriptors import (
    DESCRIPTORS,
    Effect,
    GithubOperationDescriptor,
    IdempotencyClass,
    Pagination,
    PolicyScopes,
    get_descriptor,
)
from .dispatch import (
    GITHUB_SERVICE_ID,
    GITHUB_SLUG,
    GithubDispatchError,
    assert_schema_versions,
    build_github_transport,
    control_plane_descriptor,
    decode_for,
    dispatch_operation,
    open_page_walk,
    walk_pages,
)
from .errors import ERROR_CLASSES, GithubFailure, classify_github_failure
from .locator import (
    CURSOR_ARG,
    GITHUB_API_BASE,
    PER_PAGE_ARG,
    GithubLocatorError,
    build_request,
    locate,
)
from .pagination import (
    MAX_PER_PAGE,
    CursorPageRequest,
    RestPageRequest,
    clamp_per_page,
    next_page_url,
    parse_link_header,
)
from .permissions import (
    probe_collaborator_subjects,
    probe_repo_is_private,
    resolve_repo_subjects,
)
from .rate_limit import RateLimitSnapshot, read_rate_limit
from .signatures import known_tool_names, resolve_operation_id

__all__ = [
    "DESCRIPTORS",
    "Effect",
    "GithubOperationDescriptor",
    "IdempotencyClass",
    "Pagination",
    "PolicyScopes",
    "get_descriptor",
    "ERROR_CLASSES",
    "GithubFailure",
    "classify_github_failure",
    "CursorPageRequest",
    "MAX_PER_PAGE",
    "RestPageRequest",
    "clamp_per_page",
    "next_page_url",
    "parse_link_header",
    "probe_collaborator_subjects",
    "probe_repo_is_private",
    "resolve_repo_subjects",
    "RateLimitSnapshot",
    "read_rate_limit",
    "known_tool_names",
    "resolve_operation_id",
    # W02 PR-3: real invocation path (locator -> executor/transport -> decode -> page)
    "GithubDecodeError",
    "decode_cursor_page",
    "decode_rest_page",
    "decode_single",
    "GITHUB_API_BASE",
    "CURSOR_ARG",
    "PER_PAGE_ARG",
    "GithubLocatorError",
    "build_request",
    "locate",
    "GITHUB_SERVICE_ID",
    "GITHUB_SLUG",
    "GithubDispatchError",
    "assert_schema_versions",
    "build_github_transport",
    "control_plane_descriptor",
    "decode_for",
    "dispatch_operation",
    "open_page_walk",
    "walk_pages",
]
