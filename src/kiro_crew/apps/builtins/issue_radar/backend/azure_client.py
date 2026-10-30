"""Azure DevOps client for Issue Radar.

The THIRD function-for-function mirror of :mod:`github_client`, alongside
:mod:`gitlab_client`: same public names, same argument order, same return
shapes. The app's routes, caches and React components were written against
GitHub's field names, so this module normalizes Azure DevOps payloads INTO those
names rather than introducing a third vocabulary:

    Azure DevOps                     ->  normalized (GitHub-shaped)
    work item id                         number
    System.Title                         title
    System.State (template-defined)      state "open" / "closed"
    System.Tags "a; b"                   labels ["a", "b"]
    System.CommentCount                  comments
    System.CreatedBy.uniqueName          author
    pullRequestId                        number
    status active/completed/abandoned    state "open" / "closed"
    sourceRefName/targetRefName          head/base
    isDraft                              draft
    policy evaluation / build            check run

Three properties of Azure DevOps do not exist on either other provider and shape
everything below:

1. **Work items are PROJECT-scoped and carry no repository dimension.** There is
   no "issues in this repository" question to ask, so ``list_open_issues``
   ignores ``repo`` and lists the project's work items. Two repositories in one
   project legitimately return the same list.
2. **There is no filtered work-item list endpoint.** Listing is always two calls:
   a WIQL query that returns ids, then a batched hydrate of those ids.
3. **The state vocabulary belongs to the project's process template**, not to the
   platform. "Closed", "Done", "Completed" and "Resolved" are all real closing
   states in different templates, so nothing here hard-codes a state name: the
   closing states are read from the work item type's own state definitions and
   the open filter is built from that.

Auth and credential handling follow the same model as the other two clients: no
OAuth app, no PAT stored by Kiro Crew, no hosted backend. Every call shells out
to ``az devops invoke`` -- the Azure CLI's REST passthrough -- which owns its own
credential (an ``az login`` session, or ``AZURE_DEVOPS_EXT_PAT``). This module
only (a) parses a project URL safely and (b) runs ``az`` with a list argv (never
``shell=True``), so there is no shell-injection surface and no token in this
process.

``dev.azure.com`` is the ONLY reachable host. On-premises Azure DevOps Server is
out of scope because the ``azure-devops`` CLI extension does not support it at
all, so there is no credential path for it to use -- ``_resolve_host`` refuses
everything else, and refuses an empty host rather than defaulting, for the same
reason ``gitlab_client`` does: a call site that forgot the host must fail loudly
instead of silently targeting a host the caller never named.
"""

from __future__ import annotations

import importlib as _importlib
import json  # noqa: F401 -- historical module export
import sys as _sys
import tempfile  # noqa: F401 -- historical module export
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

# Bound here as well, so ``vars(azure_client)`` lists the API versions the client sends.
from .azure_client_cli import (  # noqa: F401
    _API_BUILD,
    _API_CORE,
    _API_GIT,
    _API_IDENTITY,
    _API_POLICY,
    _API_WIT,
    _API_WIT_COMMENTS,
    _API_WIT_STATES,
    _API_WIT_TAGS,
)
from .errors import RepoUrlError as RepoUrlError  # noqa: F401 -- public re-export

# This module is the facade of Issue Radar's Azure DevOps client, and every name is reached
# through it as ``kiro_crew.apps.builtins.issue_radar.backend.azure_client.<name>``:
# a read is served by the owner that binds the name, and a write -- ``monkeypatch.setattr``,
# ``mock.patch`` -- reaches every owner holding it (:mod:`kiro_crew._facade_mirror`). Owners
# never import this module. New work goes to the owner of its responsibility:
#
#   ``azure_client_cli`` -- exception aliases, timeouts, API versions, the ``az`` spawn, REST runner
#   ``azure_client_work_items`` -- project ids, state names, the identity, WIQL, the batched hydrate
#   ``azure_client_repo_reads`` -- repo access, work-item lists and their probe, labels, members
#   ``azure_client_detail`` -- work-item detail, reference summaries, the issue and PR timelines
#   ``azure_client_writes`` -- tags, state, assignees and comments on a work item or a PR
#   ``azure_client_pulls`` -- pull-request lists, detail, checks, enrichment and search
#   ``azure_client_pr_actions`` -- close or reopen, review, merge, auto-complete, a PR's builds


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.apps.builtins.issue_radar.backend.azure_client_cli",
    "kiro_crew.apps.builtins.issue_radar.backend.azure_client_work_items",
    "kiro_crew.apps.builtins.issue_radar.backend.azure_client_repo_reads",
    "kiro_crew.apps.builtins.issue_radar.backend.azure_client_detail",
    "kiro_crew.apps.builtins.issue_radar.backend.azure_client_writes",
    "kiro_crew.apps.builtins.issue_radar.backend.azure_client_pulls",
    "kiro_crew.apps.builtins.issue_radar.backend.azure_client_pr_actions",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.apps.builtins.issue_radar.backend.azure_client_cli import (  # noqa: F401
        _ARGUMENT_ALLOWED_CHARACTERS,
        _AZ_AUTH_MARKERS,
        _AZ_ENV_PASSTHROUGH,
        _AZ_MISSING_MARKERS,
        _BATCH_MAX_IDS,
        _BUILD_SCAN_TOP,
        _COMMENT_PAGE_SIZE,
        _ENRICH_MAX_PULLS,
        _GUID_RE,
        _LEGACY_HOST_SUFFIX,
        _LOGIN_RE,
        _MAX_PAGES,
        _MEASURED_HOSTILE_CHARACTERS,
        _PAGE_SIZE,
        _PERCENT_ENCODED_OCTET,
        _PR_LOOKUP_TOP,
        _PREVIEW_API_VERSIONS,
        _REPARSED_LAUNCHER_SUFFIXES,
        _RESERVED_SEGMENTS,
        _SEGMENT_RE,
        _SHA_RE,
        _SYNTHETIC_LABEL_COLOR,
        _URL_PATH_SEPARATOR,
        _WIQL_TOP,
        AZ_PAGINATE_TIMEOUT_SEC,
        AZ_TIMEOUT_SEC,
        AZURE_HOST,
        CONTRIB_WINDOW_DAYS,
        MAX_WINDOW_DAYS,
        PR_SEARCH_MAX,
        GhCliError,
        GhInvalidInputError,
        GhPermissionError,
        GhSetupError,
        _audit,
        _az_bin,
        _az_bin_cache,
        _az_env,
        _az_invoke,
        _az_invoke_paged,
        _az_run,
        _bad_segment,
        _body_file,
        _check_label,
        _check_repo,
        _field,
        _identity_id,
        _identity_login,
        _identity_logins,
        _is_forbidden,
        _norm_issue,
        _norm_issue_detail,
        _obj,
        _org_url,
        _raise_if_setup_failure,
        _reject_percent_that_is_not_encoding,
        _reject_reparsed_launcher_args,
        _resolve_host,
        _shape_labels,
        _split_owner,
        _stderr_tail,
        _tag_names,
        _tags_field,
        _url_path_segments,
        _values,
        _work_item_url,
        parse_azure_repo_url,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.azure_client_detail import (  # noqa: F401
        _TRACKED_UPDATE_FIELDS,
        _VOTE_PROPERTY_KEYS,
        _VOTE_STATES,
        _assignee_events,
        _norm_thread_comment,
        _norm_update,
        _norm_work_item_comment,
        _state_events,
        _tag_events,
        _thread_property,
        _update_actor,
        _update_when,
        _work_item,
        get_issue_detail,
        get_ref_summary,
        list_issue_timeline,
        list_pr_timeline,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.azure_client_pr_actions import (  # noqa: F401
        _BUILD_CONCLUSIONS,
        _EMPTY_GUID,
        _UNBINDABLE_REVIEW_VERBS,
        PR_REVIEW_EVENTS,
        _assert_build_belongs_to_repo,
        _pr_patch,
        _read_build,
        cancel_workflow_run,
        disable_auto_merge,
        enable_auto_merge,
        list_pr_workflow_runs,
        merge_pull_request,
        rerun_workflow_run,
        set_pr_state,
        submit_pr_review,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.azure_client_pulls import (  # noqa: F401
        _BUILD_FINISHED_STATUSES,
        _BUILD_RESULT_BUCKETS,
        _BUILD_RUNNING_STATUSES,
        _EVALUATION_BUCKETS,
        _MERGE_PENDING,
        _MERGE_STRATEGIES,
        _MERGE_STRATEGY_NAMES,
        _MERGE_SUCCEEDED,
        _STATE_BLOCKED,
        _STATE_CHECKING,
        _STATE_DIRTY,
        _STATE_DRAFT,
        _STATE_UNKNOWN,
        PR_MERGE_METHODS,
        _branch_name,
        _builds_for_sha,
        _enrich_rows,
        _evaluation_bucket,
        _list_pulls,
        _mergeable,
        _mergeable_state,
        _norm_build,
        _norm_evaluation,
        _norm_pull,
        _org_from_api_url,
        _policy_evaluations,
        _pr_labels,
        _pr_number_for_sha,
        _pr_web_url,
        _repo_id,
        _repo_id_cache,
        _resolve_login_id,
        build_pr_search_query,
        enrich_pulls,
        enrich_pulls_by_number,
        enrichment_complete,
        get_pr_detail,
        list_closed_pulls,
        list_open_pulls,
        list_open_pulls_first_page,
        list_pr_checks,
        search_pulls,
        summarize_checks,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.azure_client_repo_reads import (  # noqa: F401
        _PROBE_KINDS,
        _default_organization,
        _identity_matches,
        _iter_project_identities,
        _list_work_items,
        _permissions,
        derive_members,
        get_current_login,
        get_repo_permissions,
        list_closed_issues,
        list_contributed_repos,
        list_open_issues,
        list_open_issues_first_page,
        list_recent_open_issues,
        list_repo_collaborators,
        list_repo_labels,
        probe_open_list,
        verify_repo_access,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.azure_client_work_items import (  # noqa: F401
        _CLOSED_STATE_CATEGORIES,
        _FALLBACK_CLOSED_STATES,
        _WORK_ITEM_FIELDS,
        _closed_state_names,
        _closed_states_cache,
        _closing_state_name,
        _current_identity,
        _hydrate_work_items,
        _identity_cache,
        _open_state_name,
        _open_work_items_wiql,
        _project_closed_states,
        _project_id,
        _project_id_cache,
        _wiql_ids,
        _wiql_literal,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.azure_client_writes import (  # noqa: F401
        _THREAD_ID_RE,
        _comment_text,
        _patch_work_item,
        _write_tags,
        add_issue_comment,
        add_issue_labels,
        add_pr_comment,
        create_label,
        post_comment_batch,
        remove_issue_label,
        reply_to_comment_thread,
        set_issue_assignees,
        set_issue_state,
    )

if TYPE_CHECKING:  # names it serves from outside the owner family, from their homes
    import subprocess  # noqa: F401

    from kiro_crew.apps.builtins.issue_radar.backend import (  # noqa: F401
        azure_normalization as _normalization,
    )
    from kiro_crew.apps.builtins.issue_radar.backend import (  # noqa: F401
        azure_transport as _transport,
    )
    from kiro_crew.apps.builtins.issue_radar.backend import comment_batch  # noqa: F401
    from kiro_crew.apps.builtins.issue_radar.backend.errors import (  # noqa: F401
        ProviderCliError,
        ProviderInvalidInputError,
        ProviderPermissionError,
        ProviderSetupError,
        PrSearchError,
        sanitize_cli_stderr,
    )
    from kiro_crew.apps.registry import (  # noqa: F401
        minimal_env,
    )
    from kiro_crew.github_runner import (  # noqa: F401
        apply_session_bus_address,
    )
    from kiro_crew.security import (  # noqa: F401
        redact_credentials,
        redact_exfiltration_urls,
    )
    from kiro_crew.sel import (  # noqa: F401
        sel,
    )
