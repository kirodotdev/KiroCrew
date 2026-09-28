"""Per-operation instance data for the GitHub connector, and the descriptor
shape that carries it.

This module OWNS the GitHub-specific facts about each operation the campaign
tracks -- its endpoint and HTTP method, its effect, the auth modes and scopes
it needs, the account types and surfaces it reaches, the tool name(s) it is
invoked through, its own pagination contract, its own idempotency class, and
the five governance-layer scope names its dispatch is gated by. It is pure
data plus the descriptor dataclass; it builds no request, performs no I/O, and
consumes no credential.

Two invariants are enforced at import time, against the SHARED single sources
of truth this stream must not fork:

* **Policy values are governance scope-catalog membership.** Each of the five
  ``policy`` fields is either ``None`` (that layer imposes no additional scope)
  or an EXACT-STRING member of the live
  ``kiro_crew.platform.governance.SCOPE_CATALOG``. This stream registers NO new
  scope -- that is the control plane's (W01's) decision. A value that is not a
  live catalog member fails :func:`_validate` at import, which is the same
  fail-closed shape the governance engine uses for an unknown matcher.
* **The idempotency class and pagination contract are drawn from closed
  vocabularies** the manifest schema fixes, so a typo cannot invent a class the
  downstream conformance runner cannot switch on.

The values here mirror the campaign's GitHub evidence catalog; the descriptor
FIELD shape mirrors the manifest-entry schema in
``docs/system-specs/modules/connector-capability-manifest.md``. This stream
supplies the instance data and the GitHub wire parsing beside it; it does not
implement the validator, the runner, or any transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple

from kiro_crew.connections.control_plane import CREDENTIAL_MODES, EFFECTS, Effect
from kiro_crew.platform.governance import SCOPE_CATALOG

# The four ``effect`` values GitHub's own operations exhibit, out of the
# control plane's seven-value :data:`~kiro_crew.connections.control_plane.EFFECTS`
# closed set. ``share`` / ``external_send`` / ``billable`` are legal control-plane
# values GitHub simply has no operation for in this required set. Named here as
# module constants so the table reads legibly while still typing as the shared
# ``Effect`` and validating against the shared closed set.
READ: Effect = "read"
WRITE: Effect = "write"
DELETE: Effect = "delete"
ADMIN: Effect = "admin"


class IdempotencyClass(str, Enum):
    """The closed idempotency/retry vocabulary the manifest schema fixes.

    ``NONE_READ`` is the read-only case (no ``retry`` object is required for a
    ``read`` effect); the other three are the manifest's ``idempotency_class``
    enum for mutating effects. Naming which class an operation actually has is
    the point -- a manifest entry never claims a guarantee an operation lacks.
    """

    BASE_SHA_GUARD = "base_sha_guard"
    GENERATE_IDS_PREALLOCATION = "generate_ids_preallocation"
    EXTERNAL_ID_UPSERT = "external_id_upsert"
    NONE_VERIFY_BY_READBACK = "none_verify_by_readback"
    # Read-only operations carry no retry idempotency class; a sentinel keeps
    # the descriptor total (every op has a value) without pretending a read
    # needs a write-idempotency guarantee.
    NONE_READ = "none_read"


class Pagination(str, Enum):
    """The pagination contract an operation advances by.

    GitHub carries two contracts (see ``pagination.py``); an operation that
    paginates uses exactly one. ``NONE`` marks an operation that returns a
    single object or an unpaginated whole (a single file, a git tree, a
    rate-limit snapshot). ``MIXED`` marks a multi-method tool whose sub-methods
    do not share one contract -- ``pull_request_read`` pages most methods by
    ``page``/``perPage`` but advances ``get_review_comments`` by a cursor -- so
    a single scalar would falsely claim one contract for all of them; the
    evidence models it as one operation with a compound pagination fact, and
    ``MIXED`` carries that faithfully rather than picking a dominant one.
    """

    REST_PAGE = "rest_page_per_page"
    CURSOR_AFTER = "cursor_after"
    MIXED = "mixed"
    NONE = "none"


# The five governance layers, in the manifest's own field order. Each maps to a
# SCOPE_CATALOG key or None. Kept as an ordered tuple so the descriptor and any
# consumer agree on the layer sequence.
POLICY_LAYERS: Tuple[str, ...] = (
    "platform_scope",
    "workspace_scope",
    "session_scope",
    "connection_scope",
    "provider_scope",
)


@dataclass(frozen=True)
class PolicyScopes:
    """The five-layer governance hook shape for one operation.

    Each field NAMES a governance scope-catalog entry (or ``None`` for "this
    layer adds no scope"); it carries no allow/deny VALUE, which the governance
    engine evaluates at runtime and is never precomputed here.
    """

    platform_scope: Optional[str]
    workspace_scope: Optional[str]
    session_scope: Optional[str]
    connection_scope: Optional[str]
    provider_scope: Optional[str]

    def values(self) -> Tuple[Optional[str], ...]:
        return (
            self.platform_scope,
            self.workspace_scope,
            self.session_scope,
            self.connection_scope,
            self.provider_scope,
        )


@dataclass(frozen=True)
class GithubOperationDescriptor:
    """GitHub-specific instance data for one campaign operation.

    Named ``Github...`` to keep it distinct from the control plane's own
    minimal dispatch :class:`kiro_crew.connections.control_plane.OperationDescriptor`
    (a five-field ``TypedDict``): this is the richer campaign instance-data row,
    mirroring the manifest-entry schema, and consumes the shared ``Effect`` type
    rather than restating it. Immutable so the table below is a constant.
    """

    operation_id: str
    endpoint: str
    http_method: str
    effect: Effect
    auth_modes: Tuple[str, ...]
    scopes: Tuple[str, ...]
    account_types: Tuple[str, ...]
    surfaces: Tuple[str, ...]
    tool_names: Tuple[str, ...]
    pagination: Pagination
    idempotency_class: IdempotencyClass
    policy: PolicyScopes
    required: bool
    # A short, non-normative note on the operation's own documented seam
    # (a contradiction the evidence recorded, an optimistic-concurrency guard,
    # etc.). Carries no validation weight.
    note: str = ""


# ---------------------------------------------------------------------------
# Policy-layer construction
#
# The GitHub connector maps the five governance layers onto EXISTING catalog
# scopes only (this stream registers none). The mapping, per the W01
# scope-catalog contract's per-layer evidence:
#
# * platform_scope  -> "network.egress": every GitHub operation egresses to the
#                      vendor's API host, so the platform-class gate for all of
#                      them is the host-matched egress scope.
# * workspace_scope -> None: a connector operation touches no local filesystem
#                      or folder, so it imposes no additional workspace scope.
# * session_scope   -> "approval_mode" for a MUTATING effect (write/delete/
#                      admin), None for a read. This is the per-effect
#                      governance the campaign requires, expressed on an
#                      existing ordinal scope rather than a new one.
# * connection_scope-> None: no connection-binding scope exists in the catalog
#                      today (a W01 design decision, not this stream's), and
#                      null is the legal, honest encoding of "no such scope".
# * provider_scope  -> "mcp" for operations that dispatch through a
#                      github-mcp-server tool (the mcp scope, "@server covers
#                      @server/tool", gates at provider granularity); None for
#                      the ones that do NOT -- the raw legacy branch-protection
#                      REST endpoints and the meta rate-limit endpoint, whose
#                      own notes record they are not wrapped by a distinct MCP
#                      tool -- so the provider layer makes no false mcp claim.
# ---------------------------------------------------------------------------

_MUTATING_EFFECTS = frozenset({WRITE, DELETE, ADMIN})


def _policy_for(effect: Effect, *, provider_via_mcp: bool = True) -> PolicyScopes:
    """Build the five-layer policy for an operation of the given effect.

    Every returned scope name is an existing SCOPE_CATALOG member (or None);
    :func:`_validate` enforces this at import. ``provider_via_mcp`` is False for
    the operations that do NOT dispatch through a github-mcp-server tool -- the
    raw legacy branch-protection REST endpoints and the meta rate-limit
    endpoint -- so their ``provider_scope`` is None rather than a false ``mcp``
    claim their own notes contradict.
    """
    session_scope = "approval_mode" if effect in _MUTATING_EFFECTS else None
    return PolicyScopes(
        platform_scope="network.egress",
        workspace_scope=None,
        session_scope=session_scope,
        connection_scope=None,
        provider_scope="mcp" if provider_via_mcp else None,
    )


# Common axis values, named once. Every value is a member of the control
# plane's CredentialMode closed set (oauth_user / fine_grained_pat /
# service_to_service): a GitHub App installation token is the service-to-service
# credential class, so the legacy/meta endpoints that also accept one carry
# ``service_to_service`` rather than a vendor-specific string outside the
# shared vocabulary. ``_validate`` pins auth_modes to that set at import.
_OAUTH_PAT: Tuple[str, ...] = ("oauth_user", "fine_grained_pat")
_OAUTH_PAT_S2S: Tuple[str, ...] = ("oauth_user", "fine_grained_pat", "service_to_service")

_ACCT_COMMON: Tuple[str, ...] = ("personal", "organization", "enterprise_cloud")
_ACCT_NO_ENTERPRISE: Tuple[str, ...] = ("personal", "organization")
_ACCT_WITH_GHES: Tuple[str, ...] = (
    "personal",
    "organization",
    "enterprise_cloud",
    "enterprise_server",
)

# Every operation here is reachable from an agentic chat turn, a background
# task, and a workflow; none is surface-restricted by a checkable provider or
# policy fact, so all carry the same surface set. (An auth mode is never
# excluded from a surface by its name alone -- that rule is the manifest's.)
_SURFACES_ALL: Tuple[str, ...] = ("chat", "app", "workflow", "background")


def _d(
    operation_id: str,
    endpoint: str,
    http_method: str,
    effect: Effect,
    tool_names: Tuple[str, ...],
    pagination: Pagination,
    idempotency_class: IdempotencyClass,
    required: bool,
    *,
    auth_modes: Tuple[str, ...] = _OAUTH_PAT,
    scopes: Tuple[str, ...],
    account_types: Tuple[str, ...] = _ACCT_COMMON,
    surfaces: Tuple[str, ...] = _SURFACES_ALL,
    provider_via_mcp: bool = True,
    note: str = "",
) -> GithubOperationDescriptor:
    """Terse constructor for a descriptor row; policy is derived from effect."""
    return GithubOperationDescriptor(
        operation_id=operation_id,
        endpoint=endpoint,
        http_method=http_method,
        effect=effect,
        auth_modes=auth_modes,
        scopes=scopes,
        account_types=account_types,
        surfaces=surfaces,
        tool_names=tool_names,
        pagination=pagination,
        idempotency_class=idempotency_class,
        policy=_policy_for(effect, provider_via_mcp=provider_via_mcp),
        required=required,
        note=note,
    )


# ---------------------------------------------------------------------------
# The 45 operation descriptors, grouped by github-mcp-server toolset.
# ---------------------------------------------------------------------------

_DESCRIPTOR_LIST: Tuple[GithubOperationDescriptor, ...] = (
    # --- repos toolset -----------------------------------------------------
    _d(
        "gh_search_repositories",
        "GET /search/repositories",
        "GET",
        READ,
        ("search_repositories",),
        Pagination.REST_PAGE,
        IdempotencyClass.NONE_READ,
        True,
        scopes=("repo",),
        note="REST search endpoints carry a stricter primary rate limit than general REST.",
    ),
    _d(
        "gh_get_file_contents",
        "GET /repos/{owner}/{repo}/contents/{path}",
        "GET",
        READ,
        ("get_file_contents",),
        Pagination.NONE,
        IdempotencyClass.NONE_READ,
        True,
        scopes=("repo",),
        note="Single file is unpaginated; directory listing returns the full list.",
    ),
    _d(
        "gh_get_repository_tree",
        "GET /repos/{owner}/{repo}/git/trees/{tree_sha}",
        "GET",
        READ,
        ("get_repository_tree",),
        Pagination.NONE,
        IdempotencyClass.NONE_READ,
        False,
        scopes=("repo",),
        note="Git trees API returns the whole tree; very large trees are truncated server-side.",
    ),
    _d(
        "gh_create_repository",
        "POST /user/repos or POST /orgs/{org}/repos",
        "POST",
        WRITE,
        ("create_repository",),
        Pagination.NONE,
        IdempotencyClass.EXTERNAL_ID_UPSERT,
        False,
        scopes=("repo",),
        account_types=_ACCT_NO_ENTERPRISE,
        note="Duplicate name in a namespace returns 422; recover by checking availability first.",
    ),
    _d(
        "gh_fork_repository",
        "POST /repos/{owner}/{repo}/forks",
        "POST",
        WRITE,
        ("fork_repository",),
        Pagination.NONE,
        IdempotencyClass.EXTERNAL_ID_UPSERT,
        False,
        scopes=("repo",),
        account_types=_ACCT_NO_ENTERPRISE,
        note="Async; re-invoking on an existing fork returns the existing fork.",
    ),
    _d(
        "gh_create_branch",
        "POST /repos/{owner}/{repo}/git/refs",
        "POST",
        WRITE,
        ("create_branch",),
        Pagination.NONE,
        IdempotencyClass.EXTERNAL_ID_UPSERT,
        True,
        scopes=("repo",),
        note="Creating an existing branch name returns 422 Reference already exists.",
    ),
    _d(
        "gh_create_or_update_file",
        "PUT /repos/{owner}/{repo}/contents/{path}",
        "PUT",
        WRITE,
        ("create_or_update_file",),
        Pagination.NONE,
        IdempotencyClass.BASE_SHA_GUARD,
        True,
        scopes=("repo", "workflow"),
        note="Requires the current blob sha on update as an optimistic-concurrency guard; "
        "a stale sha returns 409/422.",
    ),
    _d(
        "gh_push_files",
        "POST (Git Data API: blobs/trees/commits/refs)",
        "POST",
        WRITE,
        ("push_files",),
        Pagination.NONE,
        IdempotencyClass.NONE_VERIFY_BY_READBACK,
        False,
        scopes=("repo", "workflow"),
        note="No documented expected-head-sha param, unlike create_or_update_file -- a recorded "
        "contradiction; recovery is re-read branch head before retry.",
    ),
    _d(
        "gh_delete_file",
        "DELETE /repos/{owner}/{repo}/contents/{path}",
        "DELETE",
        DELETE,
        ("delete_file",),
        Pagination.NONE,
        IdempotencyClass.NONE_VERIFY_BY_READBACK,
        False,
        scopes=("repo", "workflow"),
        note="No documented base-sha concurrency param, unlike create_or_update_file -- the same "
        "recorded contradiction as push_files.",
    ),
    _d(
        "gh_delete_repository",
        "DELETE /repos/{owner}/{repo}",
        "DELETE",
        DELETE,
        ("delete_repository",),
        Pagination.NONE,
        IdempotencyClass.NONE_VERIFY_BY_READBACK,
        False,
        scopes=("delete_repo", "repo"),
        account_types=_ACCT_NO_ENTERPRISE,
        note="Irreversible; classic PAT needs delete_repo scope explicitly, not repo alone.",
    ),
    _d(
        "gh_list_branches",
        "GET /repos/{owner}/{repo}/branches",
        "GET",
        READ,
        ("list_branches",),
        Pagination.REST_PAGE,
        IdempotencyClass.NONE_READ,
        True,
        scopes=("repo",),
    ),
    _d(
        "gh_get_commit",
        "GET /repos/{owner}/{repo}/commits/{sha}",
        "GET",
        READ,
        ("get_commit",),
        Pagination.REST_PAGE,
        IdempotencyClass.NONE_READ,
        True,
        scopes=("repo",),
        note="An expired/stale sha (force-pushed away) returns 404/422 -- the expired-SHA failure mode.",
    ),
    _d(
        "gh_list_commits",
        "GET /repos/{owner}/{repo}/commits",
        "GET",
        READ,
        ("list_commits",),
        Pagination.REST_PAGE,
        IdempotencyClass.NONE_READ,
        True,
        scopes=("repo",),
    ),
    _d(
        "gh_search_code",
        "GET /search/code",
        "GET",
        READ,
        ("search_code",),
        Pagination.REST_PAGE,
        IdempotencyClass.NONE_READ,
        False,
        scopes=("repo",),
        note="Query max 256 chars; code search has its own tighter primary rate limit.",
    ),
    _d(
        "gh_search_commits",
        "GET /search/commits",
        "GET",
        READ,
        ("search_commits",),
        Pagination.REST_PAGE,
        IdempotencyClass.NONE_READ,
        False,
        scopes=("repo",),
        note="Searches default branch only.",
    ),
    # --- context toolset ---------------------------------------------------
    _d(
        "gh_get_diagnostics_actors_permissions_context",
        "GET /user (get_me); GET /user/teams; GET /orgs/{org}/teams/{team_slug}/members",
        "GET",
        READ,
        ("get_me", "get_teams", "get_team_members"),
        Pagination.REST_PAGE,
        IdempotencyClass.NONE_READ,
        True,
        scopes=("read:org",),
        note="get_me needs no extra scope; recommended as the first call to establish actor identity.",
    ),
    # --- issues toolset ----------------------------------------------------
    _d(
        "gh_issue_read_get",
        "GET /repos/{owner}/{repo}/issues/{issue_number}",
        "GET",
        READ,
        ("issue_read",),
        Pagination.NONE,
        IdempotencyClass.NONE_READ,
        True,
        scopes=("repo",),
        note="Under lockdown mode this ERRORS (does not filter) when the author lacks push access.",
    ),
    _d(
        "gh_issue_read_get_comments",
        "GET /repos/{owner}/{repo}/issues/{issue_number}/comments",
        "GET",
        READ,
        ("issue_read",),
        Pagination.REST_PAGE,
        IdempotencyClass.NONE_READ,
        True,
        scopes=("repo",),
        note="Under lockdown mode this FILTERS non-push-access authors' comments rather than erroring.",
    ),
    _d(
        "gh_search_issues",
        "GET /search/issues",
        "GET",
        READ,
        ("search_issues",),
        Pagination.REST_PAGE,
        IdempotencyClass.NONE_READ,
        True,
        scopes=("repo",),
    ),
    _d(
        "gh_list_issues",
        "GraphQL-backed issue listing",
        "POST",
        READ,
        ("list_issues",),
        Pagination.CURSOR_AFTER,
        IdempotencyClass.NONE_READ,
        True,
        scopes=("repo",),
        note="Cursor-paginated via 'after', NOT page number -- a recorded pagination inconsistency.",
    ),
    _d(
        "gh_issue_write_create",
        "POST /repos/{owner}/{repo}/issues",
        "POST",
        WRITE,
        ("issue_write",),
        Pagination.NONE,
        IdempotencyClass.NONE_VERIFY_BY_READBACK,
        True,
        scopes=("repo",),
        note="Retrying creates a duplicate issue; no client-supplied idempotency key.",
    ),
    _d(
        "gh_issue_write_update",
        "PATCH /repos/{owner}/{repo}/issues/{issue_number}",
        "PATCH",
        WRITE,
        ("issue_write",),
        Pagination.NONE,
        IdempotencyClass.NONE_VERIFY_BY_READBACK,
        True,
        scopes=("repo",),
        note="PATCH-idempotent for identical values; no expected-version guard, unlike file writes.",
    ),
    _d(
        "gh_add_issue_comment",
        "POST /repos/{owner}/{repo}/issues/{issue_number}/comments",
        "POST",
        WRITE,
        ("add_issue_comment",),
        Pagination.NONE,
        IdempotencyClass.NONE_VERIFY_BY_READBACK,
        True,
        scopes=("repo",),
        note="Comments are not deduped server-side (reactions are); body xor reaction.",
    ),
    # --- pull_requests toolset ---------------------------------------------
    _d(
        "gh_pull_request_read",
        "GET /repos/{owner}/{repo}/pulls/{pull_number} and sub-resources",
        "GET",
        READ,
        ("pull_request_read",),
        Pagination.MIXED,
        IdempotencyClass.NONE_READ,
        True,
        scopes=("repo",),
        note="One tool, nine methods with a compound pagination fact: page/perPage for most, but "
        "get_review_comments is GraphQL-backed cursor 'after' -- hence MIXED, not one scalar. "
        "Lockdown errors-vs-filters split the same two ways as issues.",
    ),
    _d(
        "gh_list_pull_requests",
        "GET /repos/{owner}/{repo}/pulls",
        "GET",
        READ,
        ("list_pull_requests",),
        Pagination.REST_PAGE,
        IdempotencyClass.NONE_READ,
        True,
        scopes=("repo",),
    ),
    _d(
        "gh_search_pull_requests",
        "GET /search/issues",
        "GET",
        READ,
        ("search_pull_requests",),
        Pagination.REST_PAGE,
        IdempotencyClass.NONE_READ,
        False,
        scopes=("repo",),
        note="PRs are a subset of the issues search index.",
    ),
    _d(
        "gh_create_pull_request",
        "POST /repos/{owner}/{repo}/pulls",
        "POST",
        WRITE,
        ("create_pull_request",),
        Pagination.NONE,
        IdempotencyClass.EXTERNAL_ID_UPSERT,
        True,
        scopes=("repo",),
        note="422 if a PR already exists for the same head/base -- a natural duplicate guard.",
    ),
    _d(
        "gh_update_pull_request",
        "PATCH /repos/{owner}/{repo}/pulls/{pull_number}",
        "PATCH",
        WRITE,
        ("update_pull_request",),
        Pagination.NONE,
        IdempotencyClass.NONE_VERIFY_BY_READBACK,
        True,
        scopes=("repo",),
        note="PATCH-idempotent for identical values; no expected-version param.",
    ),
    _d(
        "gh_pull_request_review_write",
        "POST/PUT/DELETE /repos/{owner}/{repo}/pulls/{pull_number}/reviews (+ GraphQL threads)",
        "POST",
        WRITE,
        ("pull_request_review_write",),
        Pagination.NONE,
        IdempotencyClass.NONE_VERIFY_BY_READBACK,
        True,
        scopes=("repo",),
        note="Review submit is not idempotent; resolve/unresolve_thread is. threadId is a GraphQL "
        "node ID (PRRT_...), a distinct identifier space from numeric ids.",
    ),
    _d(
        "gh_add_comment_to_pending_review",
        "POST /repos/{owner}/{repo}/pulls/{pull_number}/comments",
        "POST",
        WRITE,
        ("add_comment_to_pending_review",),
        Pagination.NONE,
        IdempotencyClass.NONE_VERIFY_BY_READBACK,
        False,
        scopes=("repo",),
        note="Operates implicitly on the caller's own latest pending review; no explicit review id.",
    ),
    _d(
        "gh_add_reply_to_pull_request_comment",
        "POST /repos/{owner}/{repo}/pulls/comments/{comment_id}/replies",
        "POST",
        WRITE,
        ("add_reply_to_pull_request_comment",),
        Pagination.NONE,
        IdempotencyClass.NONE_VERIFY_BY_READBACK,
        False,
        scopes=("repo",),
        note="commentId is the NUMERIC REST id, not the GraphQL PRRT_ thread id -- two id spaces "
        "across two tools in one domain.",
    ),
    _d(
        "gh_merge_pull_request",
        "PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge",
        "PUT",
        WRITE,
        ("merge_pull_request",),
        Pagination.NONE,
        IdempotencyClass.BASE_SHA_GUARD,
        True,
        scopes=("repo",),
        note="Branch-protected write. expectedHeadSha is the optimistic-concurrency guard (405 if "
        "head moved); rejection under protection is 405 with the unmet requirement in the body.",
    ),
    _d(
        "gh_update_pull_request_branch",
        "PUT /repos/{owner}/{repo}/pulls/{pull_number}/update-branch",
        "PUT",
        WRITE,
        ("update_pull_request_branch",),
        Pagination.NONE,
        IdempotencyClass.BASE_SHA_GUARD,
        False,
        scopes=("repo",),
        note="expectedHeadSha concurrency guard; 422 when already up to date, safe to retry with a "
        "freshly re-read sha.",
    ),
    # --- actions toolset ---------------------------------------------------
    _d(
        "gh_actions_list",
        "GET /repos/{owner}/{repo}/actions/workflows (and run/job/artifact lists)",
        "GET",
        READ,
        ("actions_list",),
        Pagination.REST_PAGE,
        IdempotencyClass.NONE_READ,
        True,
        scopes=("repo",),
        note="perPage default 30, max 100; resource_id semantics vary by method.",
    ),
    _d(
        "gh_actions_get",
        "GET /repos/{owner}/{repo}/actions/workflows/{id} (and run/job/artifact detail)",
        "GET",
        READ,
        ("actions_get",),
        Pagination.NONE,
        IdempotencyClass.NONE_READ,
        True,
        scopes=("repo",),
        note="resource_id must match the ID type the chosen method documents.",
    ),
    _d(
        "gh_get_job_logs",
        "GET /repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
        "GET",
        READ,
        ("get_job_logs",),
        Pagination.NONE,
        IdempotencyClass.NONE_READ,
        False,
        scopes=("repo",),
        note="tail_lines is a size limiter, not pagination; failed_only requires run_id.",
    ),
    _d(
        "gh_actions_run_trigger",
        "POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches (+ rerun/cancel)",
        "POST",
        WRITE,
        ("actions_run_trigger",),
        Pagination.NONE,
        IdempotencyClass.NONE_VERIFY_BY_READBACK,
        True,
        scopes=("repo",),
        note="Workflow-dispatch write. run_workflow queues a new run each call (not idempotent); "
        "rerun/cancel are effectively idempotent against a run_id. Needs on: workflow_dispatch.",
    ),
    # --- code_security / dependabot toolsets -------------------------------
    _d(
        "gh_list_code_scanning_alerts",
        "GET /repos/{owner}/{repo}/code-scanning/alerts",
        "GET",
        READ,
        ("list_code_scanning_alerts",),
        Pagination.REST_PAGE,
        IdempotencyClass.NONE_READ,
        False,
        scopes=("security_events",),
        note="Requires GitHub Advanced Security on private repos; code scanning must have run once.",
    ),
    _d(
        "gh_list_dependabot_alerts",
        "GET /repos/{owner}/{repo}/dependabot/alerts",
        "GET",
        READ,
        ("list_dependabot_alerts",),
        Pagination.CURSOR_AFTER,
        IdempotencyClass.NONE_READ,
        False,
        scopes=("security_events",),
        note="Cursor-paginated via 'after'; requires Dependabot alerts enabled.",
    ),
    # --- governance toolset (rulesets) -------------------------------------
    _d(
        "gh_repository_ruleset_read",
        "GET /repos/{owner}/{repo}/rulesets (or org/enterprise)",
        "GET",
        READ,
        ("repository_ruleset_read",),
        Pagination.REST_PAGE,
        IdempotencyClass.NONE_READ,
        False,
        scopes=("repo", "read:org", "read:enterprise"),
        account_types=_ACCT_WITH_GHES,
        note="Modern rulesets coexist with legacy branch protection; both can gate a merge -- check "
        "both surfaces.",
    ),
    _d(
        "gh_create_repository_ruleset",
        "POST /repos/{owner}/{repo}/rulesets (or org/enterprise)",
        "POST",
        ADMIN,
        ("create_repository_ruleset",),
        Pagination.NONE,
        IdempotencyClass.NONE_VERIFY_BY_READBACK,
        False,
        scopes=("repo", "admin:org", "admin:enterprise"),
        account_types=_ACCT_WITH_GHES,
        note="Not idempotent; no upsert-by-name.",
    ),
    # --- legacy branch-protection REST (not MCP-wrapped) -------------------
    _d(
        "gh_get_branch_protection_legacy",
        "GET /repos/{owner}/{repo}/branches/{branch}/protection",
        "GET",
        READ,
        ("get_branch_protection_legacy",),
        Pagination.NONE,
        IdempotencyClass.NONE_READ,
        True,
        scopes=("repo",),
        auth_modes=_OAUTH_PAT_S2S,
        provider_via_mcp=False,
        account_types=_ACCT_WITH_GHES,
        note="Raw REST, not wrapped by a distinct MCP tool; 404 when a branch has no protection.",
    ),
    _d(
        "gh_update_branch_protection_legacy",
        "PUT /repos/{owner}/{repo}/branches/{branch}/protection",
        "PUT",
        ADMIN,
        ("update_branch_protection_legacy",),
        Pagination.NONE,
        IdempotencyClass.NONE_VERIFY_BY_READBACK,
        False,
        scopes=("repo",),
        auth_modes=_OAUTH_PAT_S2S,
        provider_via_mcp=False,
        account_types=_ACCT_WITH_GHES,
        note="PUT-replace (not merge): new users/teams arrays REPLACE prior values; combined list "
        "capped at 100.",
    ),
    _d(
        "gh_update_status_check_protection_legacy",
        "PATCH /repos/{owner}/{repo}/branches/{branch}/protection/required_status_checks",
        "PATCH",
        ADMIN,
        ("update_status_check_protection_legacy",),
        Pagination.NONE,
        IdempotencyClass.NONE_VERIFY_BY_READBACK,
        False,
        scopes=("repo",),
        auth_modes=_OAUTH_PAT_S2S,
        provider_via_mcp=False,
        account_types=_ACCT_WITH_GHES,
        note="The plain 'contexts' field is deprecated (closing-down) in favour of checks[] with "
        "app_id binding.",
    ),
    # --- meta toolset ------------------------------------------------------
    _d(
        "gh_rate_limit_status",
        "GET /rate_limit",
        "GET",
        READ,
        ("get_rate_limit",),
        Pagination.NONE,
        IdempotencyClass.NONE_READ,
        True,
        scopes=(),
        auth_modes=_OAUTH_PAT_S2S,
        provider_via_mcp=False,
        account_types=_ACCT_WITH_GHES,
        note="Does not count against the primary limit but CAN count against the secondary; prefer "
        "reading x-ratelimit-* headers on ordinary calls.",
    ),
)


# Indexed by operation_id for O(1) lookup and to reject a duplicate id at import.
DESCRIPTORS: Dict[str, GithubOperationDescriptor] = {}


def _validate() -> None:
    """Fail-closed import-time checks against the shared single sources.

    * Every ``policy`` value is ``None`` or a live SCOPE_CATALOG member.
    * Every ``effect`` is a member of the control plane's ``EFFECTS`` set.
    * Every ``auth_modes`` value is a member of the control plane's
      ``CREDENTIAL_MODES`` set (no vendor-specific string outside the shared
      vocabulary).
    * No duplicate operation_id.
    * A ``read`` effect carries ``NONE_READ``; a mutating effect never does.
    """
    catalog_keys = set(SCOPE_CATALOG.keys())
    credential_modes = set(CREDENTIAL_MODES)
    for descriptor in _DESCRIPTOR_LIST:
        if descriptor.operation_id in DESCRIPTORS:
            raise ValueError(f"duplicate GitHub operation_id: {descriptor.operation_id}")
        if descriptor.effect not in EFFECTS:
            raise ValueError(
                f"{descriptor.operation_id}.effect={descriptor.effect!r} is not a member of the "
                f"control plane's EFFECTS closed set"
            )
        for auth_mode in descriptor.auth_modes:
            if auth_mode not in credential_modes:
                raise ValueError(
                    f"{descriptor.operation_id}.auth_modes contains {auth_mode!r}, not a member of "
                    f"the control plane's CREDENTIAL_MODES closed set"
                )
        for layer, value in zip(POLICY_LAYERS, descriptor.policy.values()):
            if value is None:
                continue
            if value not in catalog_keys:
                raise ValueError(
                    f"{descriptor.operation_id}.policy.{layer}={value!r} is not a live "
                    f"SCOPE_CATALOG member; this stream registers no new scope"
                )
        is_read = descriptor.effect == READ
        is_read_idem = descriptor.idempotency_class is IdempotencyClass.NONE_READ
        if is_read != is_read_idem:
            raise ValueError(
                f"{descriptor.operation_id}: read effect and NONE_READ idempotency must agree "
                f"(effect={descriptor.effect}, idempotency={descriptor.idempotency_class.value})"
            )
        DESCRIPTORS[descriptor.operation_id] = descriptor


_validate()


def get_descriptor(operation_id: str) -> Optional[GithubOperationDescriptor]:
    """Return the descriptor for an operation_id, or ``None`` if unknown."""
    return DESCRIPTORS.get(operation_id)
