"""The source-provider handler keeps its whole historical surface over its owners.

``kiro_crew.dashboard.handlers.source_providers`` is imported by name from dozens of
production modules, reached as ``sp.<name>`` by more, and patched through as a module
attribute by the test suite. Every name it defined, and every import callers reach
through it, is therefore part of its contract, private names included. Its owners now live in
``kiro_crew.dashboard.source_providers``; these tests pin that a read through the
handler answers from the owner, that a write through it reaches every caller, and
that the owners keep the one-way import order that makes both true.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import logging
import sys
import typing
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest

from kiro_crew.dashboard.handlers import source_providers as sp
from kiro_crew.dashboard.source_providers import LOGGER_NAME

#: Every module-level name the handler bound before its owners moved out of it,
#: its standard-library and typing imports included. A name dropped here is a
#: caller, a patch or a star import that reaches nothing.
_HISTORICAL_SURFACE = frozenset("""
    CHECK_STATUS_PENDING_MAX CHECK_STATUS_TTL_SECS CRED_JIRA_API_TOKEN ConfirmationRequired
    KiroCrewConfig LoopBoundLock RepoRef STALE_OWNER_SESSION_CODE STATUS_URLS_MAX SecretVault
    SourceCapacityError SourceChangeComment SourceChangeCommit SourceChangeFile
    SourceChangePayload SourceProviderError SourceProviderNotConfigured SourceProviderPlugin
    SourceRef _ADF_BLOCK_CARD_TYPES _ADF_BLOCK_CONTAINER_TYPES _ADF_BLOCK_TYPES
    _ADF_EMPHASIS_MARKS _ADF_LIST_TYPES _ADF_MAX_DEPTH _BUILTIN_PROVIDER_IDS _CACHE _CACHE_LOCK
    _CACHE_MAX_BYTES _CACHE_MAX_ENTRIES _CACHE_TTL_SECS _CHECKS_FETCH_INFLIGHT
    _CHECKS_FETCH_RESERVATION_BYTES _CHECKS_OUTPUT_BYTES _CHECK_CACHE_MAX _CHECK_CONCURRENCY
    _CHECK_FLAP_DAMP_THRESHOLD _CHECK_FORCE_MIN_INTERVAL_SECS _CHECK_PENDING_MAX _CHECK_TASKS
    _CHECK_TTL_SECS _CHECK_UPDATE_DEBOUNCE_SECS _CHIP_CI_UNAVAILABLE _CLOSED_TTL_SECS
    _COMMAND_TIMEOUT_SECS _CONTRIBUTORS_MAX _CONTRIBUTORS_TTL_SECS _CheckUpdateCallback
    _ConditionalRead _DIFF_OUTPUT_BYTES _DIRECT_FETCH_MAX_RESERVED_BYTES
    _DIRECT_FETCH_PENDING_MAX _DIRECT_FETCH_RESERVATIONS _DIRECT_FETCH_WAITERS
    _DIRECT_FETCH_WAIT_SECS _DISCUSSION_OUTPUT_BYTES _FULL_FETCH_GENERATIONS
    _FULL_FETCH_INFLIGHT _FULL_FETCH_RESERVATION_BYTES _FULL_FETCH_TASKS _GH_ENV_PASSTHROUGH
    _GH_LOGIN_RE _GITHUB_AUTO_MERGE_MUTATION _GITHUB_MERGE_METHODS _GITHUB_NODE_ID_RE
    _GITHUB_PATH_RE _GITHUB_PULL_REQUEST_NODE_QUERY _GITHUB_REACTION_KEYS
    _GITHUB_READY_MUTATION _GITHUB_RESOLVE_MUTATION _GITHUB_REVIEW_ID_RE
    _GITHUB_REVIEW_THREADS_QUERY _GITHUB_SEGMENT_KINDS _GITHUB_THREAD_ID_RE
    _GITHUB_THREAD_REPLY_MUTATION _GITHUB_UNRESOLVE_MUTATION _GITLAB_HOSTS_TTL_SECS
    _GITLAB_MERGE_STATE_MAP _GITLAB_PATH_MARKERS _GITLAB_PENDING_PIPELINE_STATUSES
    _GITLAB_SET_DRAFT_MUTATION _GITLAB_THREAD_ID_RE _ISSUE_CACHE _ISSUE_CACHE_LOCK
    _ISSUE_CACHE_MAX_BYTES _ISSUE_FETCH_INFLIGHT _ISSUE_FETCH_RESERVATION_BYTES
    _ISSUE_FETCH_TASKS _JIRA_FETCH_TIMEOUT _JIRA_KEY_RE _JIRA_MAX_COMMENTS
    _LOCAL_DASHBOARD_OWNER_SUBJECTS _MAX_CHIP_LABEL_LENGTH _MAX_COMMENT_CHARS _MAX_ERROR_BYTES
    _MAX_PAYLOAD_BYTES _MAX_PLUGIN_PATH_MARKERS _MAX_PLUGIN_PATH_MARKER_LEN _MAX_URL_LENGTH
    _MD_BLOCK_LEAD_RE _MD_CODE_LANGUAGE_RE _MD_INLINE_ESCAPE _MD_MAX_LIST_START
    _MERGE_STATE_FIELDS _MERGE_STATE_LIVE_STATES _MERGE_STATE_REREADS
    _MERGE_STATE_REREAD_DELAY_SECS _METADATA_OUTPUT_BYTES _PROBE_UNKNOWN
    _PROVIDER_AUTH_ENV_KEYS _PROVIDER_BASE_ENV_KEYS _PROVIDER_CONCURRENCY
    _PROVIDER_EXECUTABLE_CANDIDATES _PROVIDER_ID_RE _PROVIDER_SYSTEM_PATH _PROVIDER_TOOL_NAME
    _ProbeOutcome _ProviderOutputTooLarge _REVALIDATED_MAX_AGE_SECS _REVALIDATE_CHECK_RUNS_PAGE
    _REVALIDATORS _REVALIDATORS_MAX _REVIEW_GATING_EVENTS _REVIEW_SUBMIT_EVENTS _Revalidator
    _SAFE_ERROR_RE _SECONDARY_PAGE_SIZE _SOURCE_PROVIDER_PLUGINS _STRICT_PROVIDER_BIN_ENV
    _SourceChangePayloadExtras _StatusDeltaSink _T _TERMINAL_CHIP_STATES _TERMINAL_TTL_SECS
    _UNSETTLED_MERGE_STATE _URL_SCAN_ESCAPES _URL_SPAN_RE _VISIBILITY_CACHE_MAX
    _VISIBILITY_PROVIDERS _VISIBILITY_TASKS _VISIBILITY_TTL_SECS _adf_apply_marks
    _adf_attr_label _adf_block_to_markdown _adf_cell_text _adf_emphasis_item _adf_inline_run
    _adf_inline_sequence _adf_inline_to_markdown _adf_item_body _adf_join_blocks
    _adf_list_to_markdown _adf_mark_key _adf_merge_marked_text _adf_plain_text
    _adf_table_to_markdown _adf_task_list_to_markdown _adf_to_markdown _adf_url_link
    _allowed_gitlab_hosts _allowed_jira_hosts _as_dict _as_list _audit_provider_cli
    _audit_source_api _author _authorize_owner_request _branch_pattern_matches
    _capacity_exhausted_error _check_cache _check_flap _check_flap_damped _check_force_pending
    _check_forced_at _check_generations _check_inflight _check_semaphore
    _check_update_callbacks _check_update_handle _chip_refresh_due _chip_state
    _clear_check_flap _collect_process_output _commit_revalidator _contributors_cache
    _contributors_inflight _contributors_lock _direct_fetch_capacity_free _direct_fetch_tasks
    _emit_status_delta _fetch_check_status _fetch_github _fetch_github_checks
    _fetch_github_contributors _fetch_github_issue _fetch_gitlab _fetch_gitlab_checks
    _fetch_gitlab_issue _fetch_issue_uncached _fetch_jira_issue
    _fetch_pull_request_checks_uncached _fetch_pull_request_uncached _fetch_repo_visibility
    _finish_inflight _flatten_paginated _flush_check_updates _full_payload_ttl _get_jira_auth
    _gh_conditional_get _github_check _github_check_identity _github_check_rank _github_checks
    _github_comment _github_dismiss_review _github_graphql_dismisses_stale
    _github_issue_comment _github_issue_reactions _github_linked_changes _github_merge_state
    _github_pending_review _github_pending_review_comments _github_pull_request_head_sha
    _github_pull_request_node _github_pull_request_state _github_repository_node
    _github_rest_dismisses_stale _github_rollup_read _github_settled_merge_state
    _github_stale_dismissal_enabled _github_thread_ids _github_thread_map _github_thread_ref
    _gitlab_aggregate_ci _gitlab_check _gitlab_has_pending_pipeline _gitlab_hosts_fresh
    _gitlab_hosts_generation _gitlab_hosts_loaded_at _gitlab_hosts_lock _gitlab_hosts_snapshot
    _gitlab_is_draft _gitlab_issue_reactions _gitlab_linked_changes _gitlab_merge_request
    _gitlab_merge_state _gitlab_pipeline_as_check _gitlab_ref _gitlab_settled_merge_state
    _gitlab_status_bucket _int_or_zero _invalidate_check_status _invalidate_full_payload_cache
    _invalidate_pull_request_cache _is_secret_ref _issue_label _issue_labels _issue_milestone
    _jira_fix_version_milestone _jira_fix_versions _jira_hosts_snapshot _jira_is_cloud
    _jira_linked_changes _jira_pick_fix_version _jira_ref _jira_version_is_done
    _keep_known_merge_state _lifecycle_ttl _load_source_link_settings _mark_partial
    _md_backtick_fence _md_code_language _md_emit_emphasis _md_escape_block_leads
    _md_escape_inline _md_guard_line_expansion _md_hang_indent _md_inline_code _md_link_target
    _md_one_line _md_prefix_lines _md_redact_untruncated _md_wrap_emphasis _merge_state_real
    _merge_state_settled _note_check_flap _or_empty _owner_mutation_response
    _parse_conditional_get _parse_gitlab_path _parse_json_success _parse_registered_source_url
    _payload_is_terminal _payload_size_bytes _plugin_errors _plugin_for_change
    _plugin_setup_error _probe_github_payload _project_state _provider_error_response
    _provider_failure_message _provider_semaphore _provider_setup_message
    _publish_provider_hosts _publish_session_card_chips _queue_check_update
    _raise_on_graphql_errors _read_stream_limited _record_merge_state _redact_provider_data
    _refresh_check_status _refresh_repo_visibility _register_search_ref_resolver
    _require_change_ref _require_plugin_hook _reserve_direct_fetch _reset_search_ref_resolver
    _resolve_jira_token_from_vault _resolve_provider_executable _revalidate_pull_request
    _revalidation_applies _review_content_digest _rollup_ci _run_json _run_provider _safe_error
    _safe_error_text _safe_https_url _segments_match _sel _session_card_chips_snapshot
    _status_delta_sinks _status_sig _strict_provider_bins _strip_git_suffix _terminate_process
    _trim_check_cache _trim_revalidators _trim_visibility_cache _validate_provider_executable
    _validated_comment_body _visibility_cache _visibility_force_gen _visibility_inflight
    _visibility_key _wait_for_direct_fetch_capacity _wake_direct_fetch_waiters aiohttp
    api_app_contributors api_issue_source api_pull_request_auto_merge api_pull_request_checks
    api_pull_request_comment api_pull_request_pending_review api_pull_request_ready
    api_pull_request_reply api_pull_request_resolve api_pull_request_source
    api_pull_request_status api_pull_request_submit_review api_pull_request_unresolve asyncio
    comment_on_pull_request config_dir create_subprocess_limited enable_pull_request_auto_merge
    ensure_gitlab_hosts_loaded fetch_app_contributors fetch_issue fetch_pull_request
    fetch_pull_request_checks get_cached_check_status github_runner
    gitlab_ambient_token_allowed gitlab_hosts_generation is_owner_dashboard_request
    is_repo_public jira_global_token_applicable jira_host_token_name json logger
    mark_pull_request_ready normalize_jira_host owner_view_for_request parse_repo_url
    parse_source_url platform_compat provider_executable_candidates
    publish_session_card_chips_now pull_request_pending_review read_capped_response
    read_env_file_credential record_full_payload_status redact_credentials
    redact_exfiltration_urls register_source_provider register_status_delta_sink
    registered_source_provider reply_to_review_thread request_check_refresh_now
    reset_source_providers_for_tests resolve_pull_request_thread sandboxed_spawn_argv
    sandboxed_spawn_argv_async schedule_check_refresh schedule_visibility_refresh
    session_card_source_links_enabled source_link_path_markers source_ref_label
    source_search_ref stale_owner_session_response status_from_full_payload
    submit_pull_request_review time unregister_status_delta_sink unresolve_pull_request_thread
    web
    Any Awaitable Callable Iterable Iterator Protocol PurePosixPath Sequence TypeVar TypedDict
    annotations base64 contextlib dataclass fields fnmatch hashlib itertools logging os quote
    re replace urlparse urlunparse
    """.split())


def test_every_historical_name_still_resolves() -> None:
    missing = sorted(name for name in _HISTORICAL_SURFACE if not hasattr(sp, name))
    assert missing == []


def _owner(name: str) -> ModuleType:
    return sys.modules[sp._EXPORTS[name]]


def _owner_modules() -> list[ModuleType]:
    return [sys.modules[f"{sp._PACKAGE}.{leaf}"] for leaf in sp._OWNED_NAMES]


def test_the_handler_and_its_owners_split_the_surface_exactly() -> None:
    """Each historical name has exactly one home: the handler or one owner."""
    exported = set(sp._EXPORTS)
    bound_here = _HISTORICAL_SURFACE & set(vars(sp))
    assert exported & set(vars(sp)) == set()
    assert exported | bound_here == _HISTORICAL_SURFACE
    listed = [name for names in sp._OWNED_NAMES.values() for name in names]
    assert len(listed) == len(set(listed))


def test_a_read_through_the_handler_answers_from_the_owner() -> None:
    for name in sorted(sp._EXPORTS):
        owner = _owner(name)
        assert name in vars(owner), f"{owner.__name__} does not define {name}"
        assert getattr(sp, name) is vars(owner)[name], name


def test_every_forwarded_value_has_exactly_one_binding_in_the_owners() -> None:
    """A write reaches one owner, so a value two owners bind would be half-patched.

    Types are the one exception: they are copied by design and never rebound.
    """
    shared = {}
    for name in sp._EXPORTS:
        value = getattr(sp, name)
        if isinstance(value, type) or typing.get_origin(value) is not None:
            continue
        binders = [m.__name__ for m in _owner_modules() if name in vars(m)]
        if binders != [sp._EXPORTS[name]]:
            shared[name] = binders
    assert shared == {}


def test_a_write_through_the_handler_reaches_the_owner() -> None:
    sentinel = object()
    # A private MonkeyPatch, so restoring these writes cannot unwind any fixture's.
    with pytest.MonkeyPatch.context() as patch:
        for name in sorted(sp._EXPORTS):
            patch.setattr(sp, name, sentinel)
            assert vars(_owner(name))[name] is sentinel, name
            assert name not in vars(sp), name
    for name in sorted(sp._EXPORTS):
        assert vars(_owner(name))[name] is not sentinel, name


def test_a_patch_through_the_handler_reaches_a_caller_in_another_owner(monkeypatch) -> None:
    """``github`` calls ``runner._run_json``; patching the handler must reach it."""
    calls: list[tuple[str, ...]] = []

    async def fake_run_json(*argv: str, **_kwargs: object) -> object:
        calls.append(argv)
        return [] if argv[2].endswith(("/comments?per_page=100", "/timeline?per_page=100")) else {}

    monkeypatch.setattr(sp, "_run_json", fake_run_json)
    ref = sp.parse_source_url("https://github.com/o/r/issues/7")

    issue = asyncio.run(sp._fetch_github_issue(ref))

    assert issue["url"] == "https://github.com/o/r/issues/7"
    assert calls[0] == ("gh", "api", "repos/o/r/issues/7")


def test_the_handlers_never_read_a_moved_name_directly() -> None:
    """A handler reading a moved name as a bare global would raise at runtime.

    The type-checking imports make such a read look fine to flake8 and mypy, so
    it is checked here: outside that block the handler module never loads a
    name the export table owns.
    """
    tree = ast.parse(Path(inspect.getfile(sp)).read_text(encoding="utf-8"))
    runtime = [
        node
        for node in tree.body
        if not (isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING")
    ]
    bare = sorted(
        {
            node.id
            for statement in runtime
            for node in ast.walk(statement)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in sp._EXPORTS
        }
    )
    assert bare == []


def _sibling_imports(module: ModuleType) -> list[tuple[str, str]]:
    """``(leaf, name)`` for every sibling import in *module*; ``name`` is ``""``
    for a module import (``from <package> import <leaf>``)."""
    tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.module == sp._PACKAGE:
            found.extend((alias.name, "") for alias in node.names)
        elif node.module.startswith(f"{sp._PACKAGE}."):
            leaf = node.module.rsplit(".", 1)[1]
            found.extend((leaf, alias.name) for alias in node.names)
    return found


@pytest.mark.parametrize("leaf", list(sp._OWNED_NAMES))
def test_an_owner_copies_only_types_from_a_sibling(leaf: str) -> None:
    """A copied function or constant would never see a patch; a type never changes."""
    module = sys.modules[f"{sp._PACKAGE}.{leaf}"]
    for sibling, name in _sibling_imports(module):
        if not name or sibling == "LOGGER_NAME":
            continue
        value = getattr(sys.modules[f"{sp._PACKAGE}.{sibling}"], name)
        assert (
            isinstance(value, type) or typing.get_origin(value) is not None
        ), f"{leaf} copies {sibling}.{name}; reach it as {sibling}.{name} instead"


@pytest.mark.parametrize("leaf", list(sp._OWNED_NAMES))
def test_an_owner_imports_only_owners_listed_before_it(leaf: str) -> None:
    """The export table's order is the import order, so the owners form no cycle."""
    order = list(sp._OWNED_NAMES)
    module = sys.modules[f"{sp._PACKAGE}.{leaf}"]
    later = sorted(
        {
            sibling
            for sibling, _name in _sibling_imports(module)
            if sibling in order and order.index(sibling) >= order.index(leaf)
        }
    )
    assert later == []


def test_every_owner_logs_under_the_handlers_historical_name() -> None:
    assert sp.logger.name == LOGGER_NAME
    for module in _owner_modules():
        logger = vars(module).get("logger")
        if logger is not None:
            assert isinstance(logger, logging.Logger)
            assert logger.name == LOGGER_NAME, module.__name__


def test_the_module_lists_its_moved_names() -> None:
    listed = dir(sp)

    assert "fetch_pull_request" in listed
    assert "api_pull_request_source" in listed
    assert "fetch_pull_request" in sp.__all__
    assert "_run_json" not in sp.__all__


def _binding(name: str) -> object:
    return vars(_owner(name))[name]


def test_a_delete_through_the_handler_reaches_the_owner() -> None:
    owner = vars(_owner("_as_list"))
    original = owner["_as_list"]
    with pytest.MonkeyPatch.context() as patch:
        patch.delattr(sp, "_as_list")

        assert "_as_list" not in owner
        assert not hasattr(sp, "_as_list")
    assert owner["_as_list"] is original


def test_mock_patch_through_the_handler_puts_the_owners_binding_back() -> None:
    """``mock.patch`` exits by deleting the name, then writes its original back."""
    original = _binding("_run_json")

    with mock.patch.object(sp, "_run_json") as by_object:
        assert _binding("_run_json") is by_object
    assert _binding("_run_json") is original

    with mock.patch(f"{sp.__name__}._run_json") as by_name:
        assert _binding("_run_json") is by_name
    assert _binding("_run_json") is original


def test_monkeypatch_undo_puts_the_owners_binding_back() -> None:
    original = _binding("_run_json")
    patch = pytest.MonkeyPatch()
    patch.setattr(sp, "_run_json", object())
    patch.setattr(sp, "_run_json", object())

    patch.undo()

    assert _binding("_run_json") is original


def test_nested_patches_through_the_handler_unwind_in_order() -> None:
    """A fixture's patch survives a test's own patch of the same name."""
    original = _binding("_run_json")
    fixture_fake = object()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sp, "_run_json", fixture_fake)
        with mock.patch.object(sp, "_run_json") as outer:
            with mock.patch.object(sp, "_run_json"):
                pass
            assert _binding("_run_json") is outer
        assert _binding("_run_json") is fixture_fake
        with mock.patch.object(sp, "_run_json") as around:
            with pytest.MonkeyPatch.context() as inner:
                inner.setattr(sp, "_run_json", object())
            assert _binding("_run_json") is around
        assert _binding("_run_json") is fixture_fake
    assert _binding("_run_json") is original


@pytest.mark.parametrize("nested", ["monkeypatch", "assignment"])
def test_a_patch_is_undone_after_the_owner_rebinds_the_name_under_it(nested: str) -> None:
    """Code under test rebinding the patched name, then another write inside the patch."""
    name = "_gitlab_hosts_generation"
    owner = _owner(name)
    original = _binding(name)
    try:
        with mock.patch.object(sp, name, object()):
            setattr(owner, name, object())
            if nested == "monkeypatch":
                with pytest.MonkeyPatch.context() as patch:
                    patch.setattr(sp, name, object())
            else:
                setattr(sp, name, object())

        assert _binding(name) is original
    finally:
        setattr(owner, name, original)


def test_a_star_import_binds_every_public_name_the_handler_always_bound(tmp_path) -> None:
    public = {name for name in _HISTORICAL_SURFACE if not name.startswith("_")}
    probe = tmp_path / "star_import_probe.py"
    probe.write_text(f"from {sp.__name__} import *  # noqa: F403\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("star_import_probe", probe)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)

    bound = {name for name in vars(module) if not name.startswith("__")}
    assert sorted(sp.__all__) == sorted(public)
    assert bound == public
    assert [name for name in sorted(public) if vars(module)[name] is not getattr(sp, name)] == []


def test_type_checkers_resolve_what_production_imports_without_the_forwarding_hook() -> None:
    """A visible ``__getattr__`` would let mypy accept any name read through the handler."""
    tree = ast.parse(Path(inspect.getfile(sp)).read_text(encoding="utf-8"))
    hooks = [
        (ast.unparse(node.test) if isinstance(node, ast.If) else "", statement.name)
        for node in tree.body
        for statement in (node.body if isinstance(node, ast.If) else [node])
        if isinstance(statement, ast.FunctionDef) and statement.name == "__getattr__"
    ]
    assert hooks == [("not TYPE_CHECKING", "__getattr__")]

    typed = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
        for statement in node.body
        if isinstance(statement, ast.ImportFrom)
        for alias in statement.names
    }
    package = Path(inspect.getfile(sp)).parents[2]
    imported: set[str] = set()
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if sp.__name__ not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module == sp.__name__:
                imported.update(alias.name for alias in node.names)
    assert imported, "the scan found no production importer, so it measured nothing"
    assert sorted(imported - typed - set(vars(sp))) == []
