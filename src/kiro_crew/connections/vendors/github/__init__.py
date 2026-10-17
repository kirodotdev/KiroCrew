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

from .descriptors import (
    DESCRIPTORS,
    Effect,
    GithubOperationDescriptor,
    IdempotencyClass,
    Pagination,
    PolicyScopes,
    get_descriptor,
)
from .errors import ERROR_CLASSES, GithubFailure, classify_github_failure
from .pagination import (
    MAX_PER_PAGE,
    CursorPageRequest,
    RestPageRequest,
    clamp_per_page,
    next_page_url,
    parse_link_header,
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
    "RateLimitSnapshot",
    "read_rate_limit",
    "known_tool_names",
    "resolve_operation_id",
]
