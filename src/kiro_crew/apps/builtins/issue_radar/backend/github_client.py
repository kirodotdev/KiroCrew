"""Issue Radar — GitHub access via the user's existing ``gh`` CLI session.

No GitHub App, no PAT, no KiroCrew-hosted credential storage: every call
shells out to ``gh``, which owns its own token storage/refresh. This module
only needs to (a) parse a repo URL safely and (b) run ``gh api`` with a list
argv (never ``shell=True``) so there is no shell-injection surface.

The URL-parsing security model (host allowlist against SSRF, strict
owner/repo charset before it ever reaches a subprocess argv) mirrors
``code_review_sage/sage_lib/adapters.py:parse_repo_url`` — that implementation
is reused deliberately rather than re-derived, since it already defends
against SSRF/shell-injection and duplicating that logic slightly differently
would be a regression, not a rewrite.
"""

from __future__ import annotations

import importlib as _importlib
import logging
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror, github_runner

# Bound here as well: ``__all__`` names the two error classes, and a test patches
# ``probe_open_list`` with ``**kwargs``, which a mirrored facade must treat as create=True.
from .errors import PrSearchError  # noqa: F401
from .errors import RepoUrlError  # noqa: F401
from .github_client_repo_reads import probe_open_list  # noqa: F401

# This module is the facade of Issue Radar's GitHub client, and every name is reached through it
# as ``kiro_crew.apps.builtins.issue_radar.backend.github_client.<name>``: a read is served by the
# owner that binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches
# every owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module. New
# work goes to the owner of its responsibility:
#
#   ``github_client_cli`` -- exception aliases, timeouts, the ``gh`` spawn and REST runner
#   ``github_client_repo_reads`` -- repo access, issue lists, probes, labels, members
#   ``github_client_detail`` -- issue detail, references, timelines, dependencies, crew claim
#   ``github_client_writes`` -- triage writes and every PR action, with its workflow-run read
#   ``github_client_pulls`` -- pull-request lists, detail, checks, enrichment, search
#   ``github_client_ci`` -- the viewer's open-PR heads and recent workflow runs


__all__ = [
    "GhCliError",
    "GhInvalidInputError",
    "GhMergeRefusedError",
    "GhPermissionError",
    "GhSetupError",
    "PrSearchError",
    "RepoUrlError",
]

logger = logging.getLogger(__name__)


# Owner/repo URL parsing lives in the shared runner; re-exported here because
# this module is its long-standing import location (~26 internal call sites,
# routes.py, provider.py, and the tests all reach it as
# ``github_client.parse_github_repo_url``). ``errors.RepoUrlError`` is an alias
# of the runner's class, so existing ``except`` clauses keep catching it.
parse_github_repo_url = github_runner.parse_github_repo_url


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.apps.builtins.issue_radar.backend.github_client_cli",
    "kiro_crew.apps.builtins.issue_radar.backend.github_client_repo_reads",
    "kiro_crew.apps.builtins.issue_radar.backend.github_client_detail",
    "kiro_crew.apps.builtins.issue_radar.backend.github_client_writes",
    "kiro_crew.apps.builtins.issue_radar.backend.github_client_pulls",
    "kiro_crew.apps.builtins.issue_radar.backend.github_client_ci",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.apps.builtins.issue_radar.backend.github_client_ci import (  # noqa: F401
        _CI_NAME_MAX_CHARS,
        _CI_TOKEN_MAX_CHARS,
        _CI_URL_MAX_CHARS,
        _RECENT_RUN_JQ,
        _SEARCH_REPO_SEGMENT_RE,
        _VIEWER_PULLS_QUERY,
        CI_RECENT_RUNS_PER_REPO,
        CI_RUN_PR_NUMBERS_MAX,
        CI_SEARCH_MAX_PAGES,
        CI_SEARCH_PAGE_SIZE,
        CI_SEARCH_REPOS_PER_QUERY,
        _bounded_str,
        _own_pull_row,
        _own_pulls_page,
        list_recent_workflow_runs,
        search_viewer_open_pull_heads,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.github_client_cli import (  # noqa: F401
        _GH_AUTH_MARKERS,
        _GH_OVERRIDE_ENV,
        GH_PAGINATE_TIMEOUT_SEC,
        GH_TIMEOUT_SEC,
        GhCliError,
        GhInvalidInputError,
        GhMergeRefusedError,
        GhPermissionError,
        GhSetupError,
        _gh_bin,
        _gh_run,
        _raise_if_auth_failure,
        _run_gh_api,
        _stderr_tail,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.github_client_detail import (  # noqa: F401
        _DEP_ISSUE_JQ,
        _ISSUE_DETAIL_JQ,
        _PR_REVIEW_COMMENT_JQ,
        _REF_SUMMARY_JQ,
        _actor_login,
        _batch_dependency_graph,
        _dep_node_kind,
        _dep_node_state,
        _inferred_blockers_from_events,
        _inferred_blockers_from_timeline,
        _is_absent_or_forbidden,
        _is_deps_feature_absent,
        _norm_reactions,
        _normalize_timeline_event,
        _parse_crew_marker,
        fetch_dependency_edges,
        find_crew_claim,
        get_issue_detail,
        get_ref_summary,
        list_issue_blocked_by,
        list_issue_timeline,
        list_pr_review_comments,
        list_pr_timeline,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.github_client_pulls import (  # noqa: F401
        _CHECK_RUN_JQ,
        _COMMIT_STATUS_JQ,
        _MERGEABLE_RETRY_DELAY_SEC,
        _PR_DETAIL_JQ,
        _PR_JQ,
        _PR_READINESS_SELECTION,
        _PR_SEARCH_JQ,
        _PR_SUMMARY_JQ_BODY,
        _PR_SUMMARY_SELECTION,
        _READINESS_BATCH,
        _SUMMARY_BATCH,
        PR_SEARCH_MAX,
        _apply_summaries,
        _check_bucket,
        _check_identity,
        _count_context_buckets,
        _dedupe_checks,
        _enrich_readiness,
        _enrich_summaries,
        _fetch_pr_detail_once,
        _graphql_mergeable,
        _list_pulls,
        _lower_or_none,
        _parse_readiness_rows,
        _parse_summary_rows,
        _rest_pr_state,
        build_pr_search_query,
        enrich_pulls,
        enrich_pulls_by_number,
        enrichment_complete,
        fetch_pr_readiness,
        fetch_pr_readiness_by_number,
        fetch_pr_summaries,
        fetch_pr_summaries_by_number,
        get_pr_detail,
        list_closed_pulls,
        list_open_pulls,
        list_open_pulls_first_page,
        list_pr_checks,
        search_pulls,
        summarize_checks,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.github_client_repo_reads import (  # noqa: F401
        _CONTRIB_EVENT_TYPES,
        _EVENT_JQ,
        _EVENT_PAGE_SIZE,
        _ISSUE_JQ,
        _ISSUE_POLL_JQ,
        _LIST_PROBE_JQ,
        _MEMBER_ASSOC_RANK,
        _PROBE_KINDS,
        CONTRIB_WINDOW_DAYS,
        MAX_WINDOW_DAYS,
        _list_issues,
        _parse_gh_timestamp,
        derive_members,
        get_current_login,
        list_closed_issues,
        list_contributed_repos,
        list_open_issues,
        list_open_issues_first_page,
        list_recent_open_issues,
        list_repo_collaborators,
        list_repo_labels,
        verify_repo_access,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.github_client_writes import (  # noqa: F401
        _DISABLE_AUTO_MERGE,
        _ENABLE_AUTO_MERGE,
        _MERGE_ASYNC_POLL_DELAYS,
        _RUN_CANCELLABLE_STATES,
        _WORKFLOW_RUN_JQ,
        MERGE_ASYNC_WAIT_SEC,
        PR_MERGE_METHODS,
        PR_REVIEW_EVENTS,
        _merge_async_outcome,
        _merge_async_uuid,
        _merge_pending,
        _pr_node_id,
        _run_gh_graphql_mutation,
        _run_gh_write,
        _shape_labels,
        add_issue_comment,
        add_issue_labels,
        add_pr_comment,
        cancel_workflow_run,
        create_label,
        create_pull_request,
        disable_auto_merge,
        enable_auto_merge,
        get_repo_permissions,
        list_pr_workflow_runs,
        merge_pull_request,
        remove_issue_label,
        rerun_workflow_run,
        set_issue_assignees,
        set_issue_state,
        set_pr_state,
        submit_pr_review,
        update_issue_comment,
    )

if TYPE_CHECKING:  # names it serves from outside the owner family, from their homes
    import time  # noqa: F401

    from kiro_crew.apps.builtins.issue_radar.backend import (  # noqa: F401
        github_normalization,
        github_queries,
        github_transport,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.errors import (  # noqa: F401
        ProviderCliError,
        ProviderInvalidInputError,
        ProviderMergeRefusedError,
        ProviderPermissionError,
        ProviderSetupError,
        sanitize_cli_stderr,
    )
