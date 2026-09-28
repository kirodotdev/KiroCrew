"""Pull-request source data and owner-only review-thread mutation.

The browser sends a GitHub pull-request, GitLab merge-request or issue URL. The
handlers here authorize the dashboard owner, audit every call without sensitive
data, and map provider failures to HTTP responses; URL validation, the provider
CLI, the caches and the writes live in :mod:`kiro_crew.dashboard.source_providers`.
Credentials stay inside ``gh``/``glab`` and are never returned to the browser.
Credential-backed access is restricted to the configured dashboard owner.
Standalone local dashboards use their signed bootstrap identity as the implicit
owner when no channel owner is configured.

This module is also the subsystem's import and patch surface: every name it
defined before its owners moved out, and every import callers reach through it,
still resolves here, read from and written to the one module that holds it.
"""

from __future__ import annotations

import asyncio
import base64  # noqa: F401
import contextlib  # noqa: F401
import fnmatch  # noqa: F401
import hashlib  # noqa: F401
import importlib
import itertools  # noqa: F401
import json  # noqa: F401
import logging
import os  # noqa: F401
import re  # noqa: F401
import sys
import time  # noqa: F401
from collections.abc import (  # noqa: F401
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Sequence,
)
from dataclasses import dataclass, fields, replace  # noqa: F401
from pathlib import PurePosixPath  # noqa: F401
from types import ModuleType
from typing import (  # noqa: F401
    TYPE_CHECKING,
    Any,
    Protocol,
    TypedDict,
    TypeVar,
)
from urllib.parse import quote, urlparse, urlunparse  # noqa: F401

from aiohttp import web

from kiro_crew.dashboard.source_providers.contract import (
    ConfirmationRequired,
    SourceCapacityError,
    SourceProviderError,
)

logger = logging.getLogger(__name__)

# ── Re-export protocol ───────────────────────────────────────────────────────
# Production modules import these names from here, and tests read and patch them
# as attributes of this module. Two properties keep that true now that each name
# lives in an owner module:
#
# 1. A read here answers with the object the owner holds, resolved on each access
#    rather than copied at import, so it cannot drift from the owner.
# 2. A write here reaches the owner. A caller inside the owner resolves the name
#    through its own globals, so a patch applied only here would leave it running
#    the unpatched object; ``_ReExportModule`` forwards the write instead.
#
# Both resolve the owner from ``sys.modules``, so a value has one home (the
# owner's namespace) and a module has one home (``sys.modules``). The handlers
# below reach their owners the same way, never through a copied name.

_PACKAGE = "kiro_crew.dashboard.source_providers"

#: Owner module -> the names it holds the binding of: what it defines, plus the
#: imports callers reach through this module (a type, which is never rebound, is
#: listed under the first owner that imports it). In dependency order: each owner
#: imports only owners listed above it. The three errors the handlers catch, and
#: the standard-library modules and helpers this module has always bound (``json``,
#: ``re``, ``dataclass``, ...), which the owners import for themselves, are imported
#: above instead, so they are ordinary attributes of this module.
_OWNED_NAMES: dict[str, tuple[str, ...]] = {
    "contract": (
        "_MAX_URL_LENGTH",
        "RepoRef",
        "SourceChangeComment",
        "SourceChangeCommit",
        "SourceChangeFile",
        "SourceChangePayload",
        "_SourceChangePayloadExtras",
        "SourceProviderNotConfigured",
        "SourceProviderPlugin",
        "SourceRef",
    ),
    "sanitize": (
        "_MAX_PAYLOAD_BYTES",
        "_payload_size_bytes",
        "redact_credentials",
        "redact_exfiltration_urls",
        "_redact_provider_data",
        "_safe_error",
        "_SAFE_ERROR_RE",
        "_safe_error_text",
    ),
    "hosts": (
        "_allowed_gitlab_hosts",
        "_allowed_jira_hosts",
        "ensure_gitlab_hosts_loaded",
        "_gitlab_hosts_fresh",
        "_gitlab_hosts_generation",
        "gitlab_hosts_generation",
        "_gitlab_hosts_loaded_at",
        "_gitlab_hosts_lock",
        "_gitlab_hosts_snapshot",
        "_GITLAB_HOSTS_TTL_SECS",
        "_jira_hosts_snapshot",
        "_load_source_link_settings",
        "LoopBoundLock",
        "_publish_provider_hosts",
        "_publish_session_card_chips",
        "publish_session_card_chips_now",
        "_session_card_chips_snapshot",
        "session_card_source_links_enabled",
    ),
    "plugins": (
        "_BUILTIN_PROVIDER_IDS",
        "_MAX_CHIP_LABEL_LENGTH",
        "_MAX_PLUGIN_PATH_MARKER_LEN",
        "_MAX_PLUGIN_PATH_MARKERS",
        "_parse_registered_source_url",
        "_plugin_errors",
        "_plugin_for_change",
        "_plugin_setup_error",
        "_PROVIDER_ID_RE",
        "_register_search_ref_resolver",
        "register_source_provider",
        "registered_source_provider",
        "_require_plugin_hook",
        "_reset_search_ref_resolver",
        "reset_source_providers_for_tests",
        "source_link_path_markers",
        "_SOURCE_PROVIDER_PLUGINS",
        "source_ref_label",
        "source_search_ref",
    ),
    "links": (
        "_GITHUB_PATH_RE",
        "_GITHUB_SEGMENT_KINDS",
        "_GITLAB_PATH_MARKERS",
        "_gitlab_ref",
        "_JIRA_KEY_RE",
        "_jira_ref",
        "_parse_gitlab_path",
        "parse_repo_url",
        "parse_source_url",
        "_require_change_ref",
        "_strip_git_suffix",
    ),
    "runner": (
        "_audit_provider_cli",
        "_CHECKS_OUTPUT_BYTES",
        "_collect_process_output",
        "_COMMAND_TIMEOUT_SECS",
        "_ConditionalRead",
        "create_subprocess_limited",
        "_DIFF_OUTPUT_BYTES",
        "_DISCUSSION_OUTPUT_BYTES",
        "_GH_ENV_PASSTHROUGH",
        "github_runner",
        "gitlab_ambient_token_allowed",
        "_MAX_ERROR_BYTES",
        "_METADATA_OUTPUT_BYTES",
        "_parse_conditional_get",
        "_parse_json_success",
        "platform_compat",
        "_PROVIDER_AUTH_ENV_KEYS",
        "_PROVIDER_BASE_ENV_KEYS",
        "_PROVIDER_CONCURRENCY",
        "_PROVIDER_EXECUTABLE_CANDIDATES",
        "provider_executable_candidates",
        "_provider_failure_message",
        "_provider_semaphore",
        "_provider_setup_message",
        "_PROVIDER_SYSTEM_PATH",
        "_PROVIDER_TOOL_NAME",
        "_ProviderOutputTooLarge",
        "_read_stream_limited",
        "_resolve_provider_executable",
        "_run_json",
        "_run_provider",
        "sandboxed_spawn_argv",
        "sandboxed_spawn_argv_async",
        "_sel",
        "_STRICT_PROVIDER_BIN_ENV",
        "_strict_provider_bins",
        "_terminate_process",
        "_validate_provider_executable",
    ),
    "projection": (
        "_as_dict",
        "_as_list",
        "_author",
        "_chip_state",
        "_GITHUB_REACTION_KEYS",
        "_int_or_zero",
        "_issue_label",
        "_issue_labels",
        "_issue_milestone",
        "_mark_partial",
        "_merge_state_real",
        "_MERGE_STATE_REREAD_DELAY_SECS",
        "_MERGE_STATE_REREADS",
        "_merge_state_settled",
        "_or_empty",
        "_project_state",
        "_rollup_ci",
        "_safe_https_url",
        "_SECONDARY_PAGE_SIZE",
        "_TERMINAL_CHIP_STATES",
        "_UNSETTLED_MERGE_STATE",
    ),
    "github": (
        "_fetch_github",
        "_fetch_github_checks",
        "_fetch_github_issue",
        "_github_check",
        "_github_check_identity",
        "_github_check_rank",
        "_github_checks",
        "_github_comment",
        "_github_issue_comment",
        "_github_issue_reactions",
        "_github_linked_changes",
        "_github_merge_state",
        "_GITHUB_REVIEW_THREADS_QUERY",
        "_github_rollup_read",
        "_github_settled_merge_state",
        "_github_thread_ids",
        "_github_thread_map",
    ),
    "gitlab": (
        "_fetch_gitlab",
        "_fetch_gitlab_checks",
        "_fetch_gitlab_issue",
        "_gitlab_aggregate_ci",
        "_gitlab_check",
        "_gitlab_issue_reactions",
        "_gitlab_linked_changes",
        "_gitlab_merge_state",
        "_GITLAB_MERGE_STATE_MAP",
        "_gitlab_pipeline_as_check",
        "_gitlab_settled_merge_state",
        "_gitlab_status_bucket",
    ),
    "adf": (
        "_adf_apply_marks",
        "_adf_attr_label",
        "_ADF_BLOCK_CARD_TYPES",
        "_ADF_BLOCK_CONTAINER_TYPES",
        "_adf_block_to_markdown",
        "_ADF_BLOCK_TYPES",
        "_adf_cell_text",
        "_adf_emphasis_item",
        "_ADF_EMPHASIS_MARKS",
        "_adf_inline_run",
        "_adf_inline_sequence",
        "_adf_inline_to_markdown",
        "_adf_item_body",
        "_adf_join_blocks",
        "_adf_list_to_markdown",
        "_ADF_LIST_TYPES",
        "_adf_mark_key",
        "_ADF_MAX_DEPTH",
        "_adf_merge_marked_text",
        "_adf_plain_text",
        "_adf_table_to_markdown",
        "_adf_task_list_to_markdown",
        "_adf_to_markdown",
        "_adf_url_link",
        "_md_backtick_fence",
        "_MD_BLOCK_LEAD_RE",
        "_md_code_language",
        "_MD_CODE_LANGUAGE_RE",
        "_md_emit_emphasis",
        "_md_escape_block_leads",
        "_md_escape_inline",
        "_md_guard_line_expansion",
        "_md_hang_indent",
        "_md_inline_code",
        "_MD_INLINE_ESCAPE",
        "_md_link_target",
        "_MD_MAX_LIST_START",
        "_md_one_line",
        "_md_prefix_lines",
        "_md_redact_untruncated",
        "_md_wrap_emphasis",
        "_URL_SCAN_ESCAPES",
        "_URL_SPAN_RE",
    ),
    "jira": (
        "aiohttp",
        "config_dir",
        "CRED_JIRA_API_TOKEN",
        "_fetch_jira_issue",
        "_get_jira_auth",
        "_is_secret_ref",
        "_JIRA_FETCH_TIMEOUT",
        "_jira_fix_version_milestone",
        "_jira_fix_versions",
        "jira_global_token_applicable",
        "jira_host_token_name",
        "_jira_is_cloud",
        "_jira_linked_changes",
        "_JIRA_MAX_COMMENTS",
        "_jira_pick_fix_version",
        "_jira_version_is_done",
        "KiroCrewConfig",
        "normalize_jira_host",
        "read_capped_response",
        "read_env_file_credential",
        "_resolve_jira_token_from_vault",
        "SecretVault",
    ),
    "chip_status": (
        "_check_cache",
        "_CHECK_CACHE_MAX",
        "_CHECK_CONCURRENCY",
        "_check_flap",
        "_CHECK_FLAP_DAMP_THRESHOLD",
        "_check_flap_damped",
        "_check_force_pending",
        "_check_forced_at",
        "_check_generations",
        "_check_inflight",
        "_check_semaphore",
        "CHECK_STATUS_TTL_SECS",
        "_CHECK_TTL_SECS",
        "_check_update_callbacks",
        "_CHECK_UPDATE_DEBOUNCE_SECS",
        "_check_update_handle",
        "_CheckUpdateCallback",
        "_clear_check_flap",
        "_emit_status_delta",
        "_fetch_repo_visibility",
        "_flush_check_updates",
        "get_cached_check_status",
        "_invalidate_check_status",
        "is_repo_public",
        "_keep_known_merge_state",
        "_MERGE_STATE_FIELDS",
        "_MERGE_STATE_LIVE_STATES",
        "_note_check_flap",
        "_queue_check_update",
        "record_full_payload_status",
        "_record_merge_state",
        "_refresh_repo_visibility",
        "register_status_delta_sink",
        "schedule_visibility_refresh",
        "_status_delta_sinks",
        "status_from_full_payload",
        "_status_sig",
        "_StatusDeltaSink",
        "_trim_check_cache",
        "_trim_visibility_cache",
        "unregister_status_delta_sink",
        "_visibility_cache",
        "_VISIBILITY_CACHE_MAX",
        "_visibility_force_gen",
        "_visibility_inflight",
        "_visibility_key",
        "_VISIBILITY_PROVIDERS",
        "_VISIBILITY_TASKS",
        "_VISIBILITY_TTL_SECS",
    ),
    "cache": (
        "_CACHE",
        "_CACHE_LOCK",
        "_CACHE_MAX_BYTES",
        "_CACHE_MAX_ENTRIES",
        "_CACHE_TTL_SECS",
        "_capacity_exhausted_error",
        "_CHECKS_FETCH_INFLIGHT",
        "_CHECKS_FETCH_RESERVATION_BYTES",
        "_CLOSED_TTL_SECS",
        "_commit_revalidator",
        "_contributors_cache",
        "_contributors_inflight",
        "_contributors_lock",
        "_CONTRIBUTORS_MAX",
        "_CONTRIBUTORS_TTL_SECS",
        "_direct_fetch_capacity_free",
        "_DIRECT_FETCH_MAX_RESERVED_BYTES",
        "_DIRECT_FETCH_PENDING_MAX",
        "_DIRECT_FETCH_RESERVATIONS",
        "_direct_fetch_tasks",
        "_DIRECT_FETCH_WAIT_SECS",
        "_DIRECT_FETCH_WAITERS",
        "fetch_app_contributors",
        "_fetch_github_contributors",
        "fetch_issue",
        "_fetch_issue_uncached",
        "fetch_pull_request",
        "fetch_pull_request_checks",
        "_fetch_pull_request_checks_uncached",
        "_fetch_pull_request_uncached",
        "_finish_inflight",
        "_FULL_FETCH_GENERATIONS",
        "_FULL_FETCH_INFLIGHT",
        "_FULL_FETCH_RESERVATION_BYTES",
        "_FULL_FETCH_TASKS",
        "_full_payload_ttl",
        "_gh_conditional_get",
        "_GH_LOGIN_RE",
        "_invalidate_full_payload_cache",
        "_invalidate_pull_request_cache",
        "_ISSUE_CACHE",
        "_ISSUE_CACHE_LOCK",
        "_ISSUE_CACHE_MAX_BYTES",
        "_ISSUE_FETCH_INFLIGHT",
        "_ISSUE_FETCH_RESERVATION_BYTES",
        "_ISSUE_FETCH_TASKS",
        "_lifecycle_ttl",
        "_payload_is_terminal",
        "_probe_github_payload",
        "_PROBE_UNKNOWN",
        "_ProbeOutcome",
        "_reserve_direct_fetch",
        "_REVALIDATE_CHECK_RUNS_PAGE",
        "_revalidate_pull_request",
        "_REVALIDATED_MAX_AGE_SECS",
        "_revalidation_applies",
        "_Revalidator",
        "_REVALIDATORS",
        "_REVALIDATORS_MAX",
        "_T",
        "_TERMINAL_TTL_SECS",
        "_trim_revalidators",
        "_wait_for_direct_fetch_capacity",
        "_wake_direct_fetch_waiters",
    ),
    "chip_refresh": (
        "_CHECK_FORCE_MIN_INTERVAL_SECS",
        "_CHECK_PENDING_MAX",
        "CHECK_STATUS_PENDING_MAX",
        "_CHECK_TASKS",
        "_CHIP_CI_UNAVAILABLE",
        "_chip_refresh_due",
        "_fetch_check_status",
        "_refresh_check_status",
        "request_check_refresh_now",
        "schedule_check_refresh",
    ),
    "mutations": (
        "comment_on_pull_request",
        "enable_pull_request_auto_merge",
        "_GITHUB_AUTO_MERGE_MUTATION",
        "_GITHUB_MERGE_METHODS",
        "_GITHUB_NODE_ID_RE",
        "_github_pull_request_node",
        "_GITHUB_PULL_REQUEST_NODE_QUERY",
        "_GITHUB_READY_MUTATION",
        "_github_repository_node",
        "_GITHUB_RESOLVE_MUTATION",
        "_GITHUB_THREAD_ID_RE",
        "_github_thread_ref",
        "_GITHUB_THREAD_REPLY_MUTATION",
        "_GITHUB_UNRESOLVE_MUTATION",
        "_gitlab_has_pending_pipeline",
        "_gitlab_is_draft",
        "_gitlab_merge_request",
        "_GITLAB_PENDING_PIPELINE_STATUSES",
        "_GITLAB_SET_DRAFT_MUTATION",
        "_GITLAB_THREAD_ID_RE",
        "mark_pull_request_ready",
        "_MAX_COMMENT_CHARS",
        "_raise_on_graphql_errors",
        "reply_to_review_thread",
        "resolve_pull_request_thread",
        "unresolve_pull_request_thread",
        "_validated_comment_body",
    ),
    "review": (
        "_branch_pattern_matches",
        "_flatten_paginated",
        "_github_dismiss_review",
        "_github_graphql_dismisses_stale",
        "_github_pending_review",
        "_github_pending_review_comments",
        "_github_pull_request_head_sha",
        "_github_pull_request_state",
        "_github_rest_dismisses_stale",
        "_GITHUB_REVIEW_ID_RE",
        "_github_stale_dismissal_enabled",
        "pull_request_pending_review",
        "_review_content_digest",
        "_REVIEW_GATING_EVENTS",
        "_REVIEW_SUBMIT_EVENTS",
        "_segments_match",
        "submit_pull_request_review",
    ),
}

#: Re-exported name -> the dotted module that DEFINES it. Read by ``__getattr__``,
#: so this module binds none of these names in its own namespace.
_EXPORTS: dict[str, str] = {
    name: f"{_PACKAGE}.{leaf}" for leaf, names in _OWNED_NAMES.items() for name in names
}


def _module(dotted: str) -> ModuleType:
    """Return a module from where modules are stored, importing only on a miss.

    ``sys.modules`` is read first so a caller that rebinds ``importlib.import_module``
    for its own reasons cannot reroute this subsystem, and a purged and reimported
    owner is seen at once.
    """
    try:
        return sys.modules[dotted]
    except KeyError:
        return importlib.import_module(dotted)


def _submodule(leaf: str) -> ModuleType:
    """Return one owner module; the handlers below call their owners through it."""
    return _module(f"{_PACKAGE}.{leaf}")


# Hidden from type checkers, which then resolve each name a caller reads from the
# typed imports at the end of this module instead of accepting any name at all.
if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Read a re-exported name from the module that owns it (:pep:`562`)."""
        if name not in _EXPORTS:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        return getattr(_module(_EXPORTS[name]), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


def _provider_error_response(
    request: web.Request, operation: str, exc: SourceProviderError
) -> web.Response:
    """Audit and answer a failed provider read.

    Capacity pressure is reported under its own code and audit reason rather than
    as a generic provider error: nothing failed, the gateway was holding its
    concurrent-fetch memory ceiling. The code is what lets the client retry this
    one case instead of presenting it as a dead end, while a real provider error
    (auth, missing PR, malformed payload) still fails immediately.

    The audit event is emitted here rather than at the call sites so the reason
    cannot drift from the code the caller receives -- the two are one decision.
    Owning the ``json_response`` here also keeps the error-code contract scan
    honest: a helper that returned only the body would leave every call site
    passing an opaque variable (see test/test_error_code_contract.py).
    """
    if isinstance(exc, SourceCapacityError):
        code, reason = "source_busy", "capacity_exhausted"
    else:
        code, reason = "provider_error", "provider_error"
    _audit_source_api(request, operation, "failed", reason)
    return web.json_response({"error": str(exc), "code": code}, status=503)


async def api_pull_request_source(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request`` with ``{url, refresh?}``."""
    denied = _authorize_owner_request(request, "source.pull_request.read")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.read", "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        data = await _submodule("cache").fetch_pull_request(
            str(body.get("url") or ""), refresh=bool(body.get("refresh"))
        )
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.read", "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, "source.pull_request.read", "failed", "invalid_request")
        return web.json_response({"error": str(exc)}, status=400)
    except SourceProviderError as exc:
        return _provider_error_response(request, "source.pull_request.read", exc)
    _audit_source_api(request, "source.pull_request.read", "completed")
    return web.json_response(data)


async def api_issue_source(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/issue`` with ``{url, refresh?}``.

    Same authorization, audit, and error mapping as
    :func:`api_pull_request_source` -- an issue read is credential-backed
    provider data too, so it is gated on the dashboard owner identically.
    """
    denied = _authorize_owner_request(request, "source.issue.read")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, "source.issue.read", "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        data = await _submodule("cache").fetch_issue(
            str(body.get("url") or ""), refresh=bool(body.get("refresh"))
        )
    except asyncio.CancelledError:
        _audit_source_api(request, "source.issue.read", "failed", "request_cancelled")
        raise
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith("jira_no_credentials:"):
            code = "jira_no_credentials"
        elif msg.startswith("jira_config_error:"):
            code = "jira_config_error"
        else:
            code = "invalid_request"
        _audit_source_api(request, "source.issue.read", "failed", code)
        return web.json_response({"error": msg, "code": code}, status=400)
    except SourceProviderError as exc:
        return _provider_error_response(request, "source.issue.read", exc)
    _audit_source_api(request, "source.issue.read", "completed")
    return web.json_response(data)


async def api_app_contributors(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/contributors`` with ``{url, refresh?}``.

    Same authorization, audit, and error mapping as
    :func:`api_pull_request_source`: contributor data is credential-backed
    provider data, so it is gated on the dashboard owner identically.
    """
    denied = _authorize_owner_request(request, "source.contributors.read")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, "source.contributors.read", "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        contributors = await _submodule("cache").fetch_app_contributors(
            str(body.get("url") or ""), refresh=bool(body.get("refresh"))
        )
    except asyncio.CancelledError:
        _audit_source_api(request, "source.contributors.read", "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, "source.contributors.read", "failed", "invalid_request")
        return web.json_response({"error": str(exc), "code": "invalid_request"}, status=400)
    except SourceProviderError as exc:
        return _provider_error_response(request, "source.contributors.read", exc)
    _audit_source_api(request, "source.contributors.read", "completed")
    return web.json_response({"contributors": contributors})


async def api_pull_request_checks(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/checks`` with ``{url}``."""
    denied = _authorize_owner_request(request, "source.pull_request.checks")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.checks", "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        checks = await _submodule("cache").fetch_pull_request_checks(str(body.get("url") or ""))
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.checks", "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, "source.pull_request.checks", "failed", "invalid_request")
        return web.json_response({"error": str(exc)}, status=400)
    except SourceProviderError as exc:
        return _provider_error_response(request, "source.pull_request.checks", exc)
    _audit_source_api(request, "source.pull_request.checks", "completed")
    return web.json_response({"checks": checks})


# Upper bound on URLs accepted per status request. Matches the Changes tab's
# own source cap (website/src/utils/pullRequestLinks.ts MAX_PULL_REQUEST_SOURCES)
# so one request covers a full strip, and caps the parse work for a hostile body.
STATUS_URLS_MAX = 64


async def api_pull_request_status(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/status`` with ``{urls: [...]}``.

    Returns the *cached* lightweight ``{ci, state}`` for each URL and kicks a
    bounded background refresh for stale entries — the same cache and pacing the
    sidebar chips use. Never blocks on a provider call: unknown URLs simply come
    back absent and appear on a later poll.
    """
    denied = _authorize_owner_request(request, "source.pull_request.status")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, "source.pull_request.status", "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw_urls = body.get("urls")
    if not isinstance(raw_urls, list):
        _audit_source_api(request, "source.pull_request.status", "failed", "invalid_request")
        return web.json_response({"error": "A list of pull-request URLs is required."}, status=400)
    try:
        await _submodule("hosts").ensure_gitlab_hosts_loaded()
    except asyncio.CancelledError:
        # This is a direct await in the handler (unlike the read/checks/resolve
        # endpoints, which reach ensure() through a provider helper wrapped in
        # the cancellation guard below). A cancellation here would otherwise
        # unwind past the terminal ``completed`` audit, leaving an authorized
        # status attempt absent from the tamper-evident SEL chain. Pair it with
        # ``failed/request_cancelled`` — matching the body-parse guard above —
        # then re-raise.
        _audit_source_api(request, "source.pull_request.status", "failed", "request_cancelled")
        raise
    links = _submodule("links")
    canonical: list[str] = []
    for value in raw_urls[:STATUS_URLS_MAX]:
        if not isinstance(value, str):
            continue
        try:
            # An issue URL reaching here would be scheduled for a chip refresh
            # and answered from the pull-request namespace. `_require_change_ref`
            # raises ValueError, which this loop already treats as "not a
            # supported source URL" and skips.
            ref = links._require_change_ref(links.parse_source_url(value))
        except ValueError:
            continue
        if ref.url not in canonical:
            canonical.append(ref.url)
    chip_status = _submodule("chip_status")
    statuses = {
        url: status
        for url in canonical
        if (status := chip_status.get_cached_check_status(url)) is not None
    }
    refreshing = _submodule("chip_refresh").schedule_check_refresh(canonical)
    _audit_source_api(request, "source.pull_request.status", "completed")
    # ``refreshing`` lets the client re-poll shortly instead of on TTL pacing, so
    # a state change lands within seconds of the background refresh rather than up
    # to one extra TTL later. ``ttlSecs`` is the server's own cache TTL, so the
    # client's steady-state pacing tracks it instead of hardcoding a copy.
    return web.json_response(
        {
            "statuses": statuses,
            "refreshing": refreshing,
            "ttlSecs": chip_status.CHECK_STATUS_TTL_SECS,
        }
    )


_LOCAL_DASHBOARD_OWNER_SUBJECTS = frozenset({"local-app", "local-startup"})

# The one owner-gate denial that gets a machine-readable label of its own. A
# token subject is fixed at mint time as ``owner_id or <bootstrap subject>``,
# and every refresh re-mints from the INCOMING subject, so a session signed in
# before ``KIROCREW_OWNER_ID`` was configured carries `local-app` /
# `local-startup` for its whole life. Once an owner exists, the gate denies
# that subject — correctly — but a generic ``403 forbidden`` gives the user no
# way to tell "sign in again" apart from any other authorization failure.
STALE_OWNER_SESSION_CODE = "stale_session_reauth"


def stale_owner_session_response(request: web.Request) -> web.Response | None:
    """The distinct denial label for a signed pre-owner bootstrap session.

    Called strictly AFTER an owner-gate deny decision has been made: it never
    grants, widens, or re-orders access — it only chooses the response body for
    a request that is already refused. Returns the ``401 stale_session_reauth``
    body when the denied caller is a SIGNED dashboard-user bootstrap subject
    while an owner is configured, and ``None`` for every other denied caller,
    who keeps the call site's existing generic response. The discriminator is
    reserved for already-authenticated callers on purpose: an unsigned, absent,
    or app-token caller must not learn which denial class it hit.

    401 rather than 403 because re-authentication is the remedy — the caller's
    credential is stale, not merely under-privileged. Only a fresh sign-in (a
    newly minted token, whose subject is derived from the now-configured owner)
    clears it; a token refresh cannot, since refresh preserves the subject.
    """
    caller = str(request.get("user") or "")
    if request.get("app") != "":
        # App tokens keep their generic denial, and an absent app claim means
        # the middleware never authenticated this caller as a dashboard user.
        return None
    if caller not in _LOCAL_DASHBOARD_OWNER_SUBJECTS:
        return None
    state = request.app["state"]
    owner_id = str(getattr(state, "owner_id", "") or "")
    if not owner_id:
        return None
    return web.json_response(
        {
            "error": "this session predates the configured owner; sign in again",
            "code": STALE_OWNER_SESSION_CODE,
        },
        status=401,
    )


def owner_view_for_request(request: web.Request) -> bool:
    """Whether the owner's credential-redaction switch may apply to THIS request.

    :func:`is_owner_dashboard_request` with the fail direction spelled out: any
    request the owner predicate cannot evaluate (no ``state`` on the app, an
    unexpected shape) is NOT an owner view, so the caller falls back to the
    unconditional redaction pass. Used only by the owner-view seams
    (``security.redaction_switch``); a gate that must REFUSE on non-owner keeps
    calling the predicate directly.
    """
    try:
        return is_owner_dashboard_request(request)
    except Exception:
        return False


def is_owner_dashboard_request(request: web.Request) -> bool:
    """Return whether request has a configured or implicit local owner identity."""
    state = request.app["state"]
    owner_id = str(getattr(state, "owner_id", "") or "")
    caller = str(request.get("user") or "")
    if "app" not in request or request["app"] != "" or not caller:
        return False
    if owner_id:
        return caller == owner_id
    return caller in _LOCAL_DASHBOARD_OWNER_SUBJECTS


def _audit_source_api(
    request: web.Request,
    operation: str,
    outcome: str,
    error: str = "",
) -> None:
    """Best-effort source API audit without sensitive request or provider data."""
    caller = str(request.get("user") or "anonymous")
    try:
        _submodule("runner")._sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome=outcome,
            source="dashboard",
            error=error,
        )
    except Exception:
        logger.debug("SEL source API audit failed", exc_info=True)


def _authorize_owner_request(request: web.Request, operation: str) -> web.Response | None:
    """Require an explicit dashboard-user claim matching the configured owner.

    When no owner is configured, a signed machine-local bootstrap identity
    (``local-app`` / ``local-startup``) IS the owner, for reads and mutations
    alike. The allow branch delegates to :func:`is_owner_dashboard_request`
    rather than re-deriving it, so this gate cannot drift from the rule every
    other owner-gated dashboard surface follows, the secrets vault among them.
    Scoping the allowance to reads here made an install with no Slack credential
    render live mutation buttons it then refused, and pointed the user at a Slack
    setting that has nothing to do with the action: ``owner_id`` is only ever
    populated from ``KIROCREW_OWNER_ID``, so an install that never configures
    Slack could not act on a pull request at all.

    Once an owner is configured, every operation requires an exact owner match.
    App tokens, unsigned callers, and a mismatched subject always fail closed,
    which is what keeps a non-owner on a shared or network-exposed deployment
    from riding the owner's provider credentials. The per-case audit labels below
    are why the whole gate is not the predicate: a denial has to name which class
    it hit, which one boolean cannot.
    """
    state = request.app["state"]
    owner_id = str(getattr(state, "owner_id", "") or "")
    caller = str(request.get("user") or "")
    if not owner_id:
        if is_owner_dashboard_request(request):
            return None
        _audit_source_api(request, operation, "denied", "owner_not_configured")
        return web.json_response({"error": "forbidden"}, status=403)
    if "app" not in request or request["app"] != "":
        _audit_source_api(request, operation, "denied", "app_token_not_allowed")
        return web.json_response({"error": "forbidden"}, status=403)
    if not caller:
        _audit_source_api(request, operation, "denied", "non_owner")
        return web.json_response({"error": "forbidden"}, status=403)
    if caller != owner_id:
        _audit_source_api(request, operation, "denied", "non_owner")
        # Deny decision made above; the helper only relabels the response for a
        # signed pre-owner bootstrap subject. Every other caller stays generic.
        stale = stale_owner_session_response(request)
        if stale is not None:
            return stale
        return web.json_response({"error": "forbidden"}, status=403)
    return None


async def api_pull_request_resolve(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/resolve`` mutation.

    Credential-backed provider access requires an explicit dashboard-user claim.
    Configured installations require an exact owner match. Standalone local
    installations accept only signed local bootstrap subjects. App tokens and
    missing auth claims fail closed.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        await _submodule("mutations").resolve_pull_request_thread(
            str(body.get("url") or ""), str(body.get("threadId") or "")
        )
        return {"resolved": True}

    return await _owner_mutation_response(request, "source.pull_request.resolve", action)


async def api_pull_request_unresolve(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/unresolve`` mutation.

    The counterpart to resolve: a thread closed by mistake, or reopened because
    the fix did not hold, has to be recoverable from the same surface.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        await _submodule("mutations").unresolve_pull_request_thread(
            str(body.get("url") or ""), str(body.get("threadId") or "")
        )
        return {"resolved": False}

    return await _owner_mutation_response(request, "source.pull_request.unresolve", action)


async def api_pull_request_reply(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/reply`` mutation.

    Posts a reply into an existing review thread under the dashboard owner's
    provider identity. Same auth, audit, and cache-invalidation contract as
    resolve, plus the thread-ownership proof that keeps a browser-supplied thread
    id from reaching an unrelated pull request.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        await _submodule("mutations").reply_to_review_thread(
            str(body.get("url") or ""),
            str(body.get("threadId") or ""),
            str(body.get("body") or ""),
        )
        return {"posted": True}

    return await _owner_mutation_response(request, "source.pull_request.reply", action)


async def api_pull_request_comment(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/comment`` mutation.

    A top-level comment on the pull request conversation, for the case that is
    not a reply to anyone's line.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        await _submodule("mutations").comment_on_pull_request(
            str(body.get("url") or ""), str(body.get("body") or "")
        )
        return {"posted": True}

    return await _owner_mutation_response(request, "source.pull_request.comment", action)


async def _owner_mutation_response(
    request: web.Request,
    operation: str,
    action: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> web.Response:
    """Run one owner-only provider mutation with shared auth, audit, and errors.

    Every provider mutation shares the same contract: an explicit owner claim,
    a JSON body, and terminal audit events that distinguish a client disconnect
    (remote outcome unknown) from a rejected request or a provider failure.
    """
    denied = _authorize_owner_request(request, operation)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, operation, "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        payload = await action(body)
    except asyncio.CancelledError:
        # The provider may have accepted the mutation before the client
        # disconnected, so record the uncertain outcome and preserve task
        # cancellation for aiohttp's shutdown/disconnect handling.
        _audit_source_api(request, operation, "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, operation, "failed", "invalid_request")
        rejection: dict[str, Any] = {"error": str(exc)}
        if isinstance(exc, ConfirmationRequired):
            # Marks the refusal as answerable: the client may retry with the
            # acknowledgement, and only this reply justifies sending it.
            rejection["confirmationRequired"] = True
        return web.json_response(rejection, status=400)
    except SourceProviderError as exc:
        return _provider_error_response(request, operation, exc)
    except Exception:
        _audit_source_api(request, operation, "failed", "internal_error")
        raise
    _audit_source_api(request, operation, "completed")
    return web.json_response(payload)


async def api_pull_request_auto_merge(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/auto-merge`` mutation.

    Authorizes the provider to merge the pull request once its requirements
    pass. Same credential boundary as the resolve mutation.

    ``confirmImmediateMerge`` must be a real JSON boolean. Coercing it with
    ``bool()`` would let any truthy value -- notably the string ``"false"`` --
    read as consent, so a malformed client would silently satisfy the very guard
    that stands between it and an immediate merge.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        confirm = body.get("confirmImmediateMerge", False)
        if confirm is not True and confirm is not False:
            raise ValueError("confirmImmediateMerge must be true or false.")
        method = await _submodule("mutations").enable_pull_request_auto_merge(
            str(body.get("url") or ""),
            confirm_immediate_merge=confirm,
        )
        return {"autoMerge": True, "mergeMethod": method}

    return await _owner_mutation_response(request, "source.pull_request.auto_merge", action)


async def api_pull_request_ready(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/ready`` mutation.

    Takes the pull/merge request out of draft. Same credential boundary as the
    resolve mutation.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        await _submodule("mutations").mark_pull_request_ready(str(body.get("url") or ""))
        return {"ready": True}

    return await _owner_mutation_response(request, "source.pull_request.ready", action)


async def api_pull_request_pending_review(request: web.Request) -> web.Response:
    """POST ``/api/source/pull-request/pending-review`` with ``{url}``.

    A read of the same class of credential-backed provider data every other
    source route returns, gated on the shared dashboard-owner rule in
    :func:`_authorize_owner_request`.
    """
    operation = "source.pull_request.pending_review"
    denied = _authorize_owner_request(request, operation)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except asyncio.CancelledError:
        _audit_source_api(request, operation, "failed", "request_cancelled")
        raise
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        data = await _submodule("review").pull_request_pending_review(str(body.get("url") or ""))
    except asyncio.CancelledError:
        _audit_source_api(request, operation, "failed", "request_cancelled")
        raise
    except ValueError as exc:
        _audit_source_api(request, operation, "failed", "invalid_request")
        return web.json_response({"error": str(exc), "code": "invalid_request"}, status=400)
    except SourceProviderError as exc:
        return _provider_error_response(request, operation, exc)
    _audit_source_api(request, operation, "completed")
    return web.json_response(data)


async def api_pull_request_submit_review(request: web.Request) -> web.Response:
    """Owner-only POST ``/api/source/pull-request/submit-review`` mutation.

    Publishes a pending review the caller has already been shown. Same credential
    boundary as the resolve mutation, and the strictest of the provider writes in
    consequence: submitting is irreversible and visible to everyone on the pull
    request.
    """

    async def action(body: dict[str, Any]) -> dict[str, Any]:
        return await _submodule("review").submit_pull_request_review(
            str(body.get("url") or ""),
            str(body.get("reviewId") or ""),
            str(body.get("event") or ""),
            str(body.get("contentDigest") or ""),
        )

    return await _owner_mutation_response(request, "source.pull_request.submit_review", action)


class _ReExportModule(ModuleType):
    """Send a write or a delete of a re-exported name to the module that owns it.

    Binding the name here instead would shadow the owner permanently, since
    ``__getattr__`` runs only for a name this module does not hold, and a test
    harness restoring the value it read would install it here for the life of the
    process. Forwarding leaves one value to patch and one to put back, so
    ``monkeypatch`` and ``mock.patch`` restore exactly, nested in either order:
    ``mock.patch`` exits by deleting the name and then, finding it gone, writing its
    original back. With ``create=True`` it skips that write, so the owner would lose
    the name; ``test_source_providers_refactor_create_guard`` refuses such a patch.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _EXPORTS:
            setattr(_module(_EXPORTS[name]), name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _EXPORTS:
            delattr(_module(_EXPORTS[name]), name)
        else:
            super().__delattr__(name)


# Installed last, so forwarding is live for every caller but never runs while this
# module is still binding its own names.
sys.modules[__name__].__class__ = _ReExportModule

# Load every owner now, so one import loads the whole subsystem: the first request never
# imports on the event loop, and an owner that cannot import fails the import of
# this module rather than one route later.
for _leaf in _OWNED_NAMES:
    _submodule(_leaf)
del _leaf

#: Imported only to run the re-export machinery, so a star import leaves them out.
_MACHINERY = frozenset({"ModuleType", "TYPE_CHECKING", "importlib", "sys"})

# A star import reads this list and never reaches ``__getattr__``. Derived from
# the two authorities -- what this module binds and the export table -- so it is
# not a third list to keep in step.
__all__ = sorted(
    name
    for name in set(globals()) | set(_EXPORTS)
    if not name.startswith("_") and name not in _MACHINERY
)


if TYPE_CHECKING:  # the public surface and what production imports, for type checkers
    from kiro_crew.dashboard.source_providers.cache import (  # noqa: F401
        fetch_app_contributors,
        fetch_issue,
        fetch_pull_request,
        fetch_pull_request_checks,
    )
    from kiro_crew.dashboard.source_providers.chip_refresh import (  # noqa: F401
        CHECK_STATUS_PENDING_MAX,
        request_check_refresh_now,
        schedule_check_refresh,
    )
    from kiro_crew.dashboard.source_providers.chip_status import (  # noqa: F401
        CHECK_STATUS_TTL_SECS,
        get_cached_check_status,
        is_repo_public,
        record_full_payload_status,
        register_status_delta_sink,
        schedule_visibility_refresh,
        status_from_full_payload,
        unregister_status_delta_sink,
    )
    from kiro_crew.dashboard.source_providers.contract import (  # noqa: F401
        RepoRef,
        SourceChangeComment,
        SourceChangeCommit,
        SourceChangeFile,
        SourceChangePayload,
        SourceProviderNotConfigured,
        SourceProviderPlugin,
        SourceRef,
    )
    from kiro_crew.dashboard.source_providers.hosts import (  # noqa: F401
        ensure_gitlab_hosts_loaded,
        gitlab_hosts_generation,
        publish_session_card_chips_now,
        session_card_source_links_enabled,
    )
    from kiro_crew.dashboard.source_providers.links import (  # noqa: F401
        parse_repo_url,
        parse_source_url,
    )
    from kiro_crew.dashboard.source_providers.mutations import (  # noqa: F401
        comment_on_pull_request,
        enable_pull_request_auto_merge,
        mark_pull_request_ready,
        reply_to_review_thread,
        resolve_pull_request_thread,
        unresolve_pull_request_thread,
    )
    from kiro_crew.dashboard.source_providers.plugins import (  # noqa: F401
        register_source_provider,
        registered_source_provider,
        reset_source_providers_for_tests,
        source_link_path_markers,
        source_ref_label,
        source_search_ref,
    )
    from kiro_crew.dashboard.source_providers.review import (  # noqa: F401
        pull_request_pending_review,
        submit_pull_request_review,
    )
    from kiro_crew.dashboard.source_providers.runner import (  # noqa: F401
        _validate_provider_executable,
        provider_executable_candidates,
    )
