"""Dashboard file HTTP handlers: the facade over :mod:`kiro_crew.dashboard.file_api`.

Routes, the handlers package and tests import and patch every file handler through
this module. The handler families live in the ``file_api`` owners -- ``uploads``,
``workspaces``, ``pinned_io``, ``transfer``, ``office_preview``, ``sheet``,
``search``, ``path_complete``, ``grep``, ``browse``, ``project_dirs``,
``git_panel``, ``project_tree``, ``dashboard_config``, ``path_probe`` (the
``_run_path_probe`` chokepoint), ``outbox``, ``channel_upload`` (the Slack and channel
uploads with their shared admission gate), ``desktop`` (reveal, the native picker and
screenshot) and ``editor`` (file read, raw, write and diff) -- and ``_facade_compose.compose``
runs them on this module's globals, so a patch of ``files.<name>`` reaches them.

This module keeps the imports, constants and seams the owners read: the SEL accessor,
the file-send audit, the sub-agent session mapping, the request-body error code, the
search walk's clock, and ``_owner_view_bypasses_credential_pass``, the file viewer's
one owner-view verdict per request. The directory-picker routes ``api_browse_dirs``
and ``api_browse_files`` stay here too, over the ``browse`` listings: their skip sets
hold a literal the internal content scan refuses on an added line. New work goes to
the owner of its family; ``docs/system-specs/modules/learn-cron-dashboard.md`` records
the map.
"""

from __future__ import annotations

import asyncio
import contextlib  # noqa: F401
import datetime as _dt  # noqa: F401
import errno  # noqa: F401
import functools  # noqa: F401
import hashlib  # noqa: F401
import io  # noqa: F401
import json
import logging
import mimetypes
import ntpath  # noqa: F401
import os
import posixpath  # noqa: F401
import queue  # noqa: F401
import re
import shutil  # noqa: F401
import stat as _stat_mod  # noqa: F401
import subprocess  # noqa: F401
import sys  # noqa: F401
import tempfile  # noqa: F401
import threading  # noqa: F401
import time
import urllib.parse  # noqa: F401
import uuid  # noqa: F401
import zipfile  # noqa: F401
from dataclasses import asdict  # noqa: F401
from pathlib import Path, PurePath  # noqa: F401
from typing import BinaryIO, Callable, NamedTuple, TypeVar  # noqa: F401

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError  # noqa: F401
from aiohttp.multipart import BodyPartReader  # noqa: F401

from kiro_crew import executors, file_delivery_consent, pinned_fs, platform_compat  # noqa: F401
from kiro_crew._facade_compose import compose as _facade_compose
from kiro_crew.atomic_write import (  # noqa: F401
    atomic_write,
    open_access_control_source,
    pinned_parent_replace_supported,
)
from kiro_crew.config import loader as config_loader  # noqa: F401
from kiro_crew.config.loader import (  # noqa: F401
    KiroCrewConfig,
    WorkspaceConfig,
    WorkspaceDirUnusable,
    coerce_dict_section,
    config_dir,
    data_home,
    materialize_workspace_dir,
    update_config_locked,
)
from kiro_crew.config.sections import (  # noqa: F401
    LINK_PATTERN_PATTERN_MAX_LEN,
    LINK_PATTERN_URL_MAX_LEN,
    LINK_PATTERNS_MAX,
    link_pattern_url_ok,
)
from kiro_crew.dashboard import part_stream, upload_destination  # noqa: F401
from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async  # noqa: F401
from kiro_crew.dashboard.chat_utils import (  # noqa: F401
    dashboard_slot_key,
    drained_to_thread,
    run_config_write,
)
from kiro_crew.dashboard.file_api import browse as _owner_browse
from kiro_crew.dashboard.file_api import channel_upload as _owner_channel_upload
from kiro_crew.dashboard.file_api import dashboard_config as _owner_dashboard_config
from kiro_crew.dashboard.file_api import desktop as _owner_desktop
from kiro_crew.dashboard.file_api import editor as _owner_editor
from kiro_crew.dashboard.file_api import git_panel as _owner_git_panel
from kiro_crew.dashboard.file_api import grep as _owner_grep
from kiro_crew.dashboard.file_api import office_preview as _owner_office_preview
from kiro_crew.dashboard.file_api import outbox as _owner_outbox
from kiro_crew.dashboard.file_api import path_complete as _owner_path_complete
from kiro_crew.dashboard.file_api import path_probe as _owner_path_probe
from kiro_crew.dashboard.file_api import pinned_io as _owner_pinned_io
from kiro_crew.dashboard.file_api import project_dirs as _owner_project_dirs
from kiro_crew.dashboard.file_api import project_tree as _owner_project_tree
from kiro_crew.dashboard.file_api import search as _owner_search
from kiro_crew.dashboard.file_api import sheet as _owner_sheet
from kiro_crew.dashboard.file_api import transfer as _owner_transfer
from kiro_crew.dashboard.file_api import uploads as _owner_uploads
from kiro_crew.dashboard.file_api import workspaces as _owner_workspaces
from kiro_crew.dashboard.file_api.browse import (  # noqa: F401
    _browse_dirs_sync,
    _browse_drives_sync,
    _browse_entry_is_dir,
    _browse_files_sync,
    _browse_parent,
    _is_windows_drive_root,
)
from kiro_crew.dashboard.file_api.channel_upload import (  # noqa: F401
    _gate_upload_file,
    api_channel_upload_file,
    api_slack_upload_file,
)
from kiro_crew.dashboard.file_api.dashboard_config import (  # noqa: F401
    api_dashboard_config,
)
from kiro_crew.dashboard.file_api.desktop import (  # noqa: F401
    _screenshot_dir,
    api_reveal_path,
    api_screenshot,
    api_upload,
)
from kiro_crew.dashboard.file_api.editor import (  # noqa: F401
    _read_safe_end,
    _resolve_diff_path,
    api_file_diff,
    api_file_raw,
    api_file_read,
    api_file_write,
)
from kiro_crew.dashboard.file_api.git_panel import (  # noqa: F401
    _GIT_PANEL_STDOUT_CAP,
    _is_not_a_repo_verdict,
    _porcelain_unquote,
    _probe_git_dir,
    _project_directory_absent,
    _repo_filter_refusal_cause,
    _run_git_bounded,
    _worktree_probe_failure_is_empty_scope,
    api_project_git_log,
    api_project_git_status,
)
from kiro_crew.dashboard.file_api.grep import (  # noqa: F401
    _grep_doc_segments,
    _grep_docs,
    _grep_hit,
    _grep_pdf_segments,
    _grep_python,
    _grep_resolve_root,
    _grep_rg,
    _grep_rg_argv,
    _grep_rg_executable,
    _grep_rg_hit_of,
    _grep_rg_teardown,
    _grep_sensitive_globs,
    _grep_xlsx_segments,
    api_file_grep,
)
from kiro_crew.dashboard.file_api.office_preview import (  # noqa: F401
    _cap_slides,
    _PreviewUnsupported,
    _redact_block,
    _redact_blocks,
    _redact_value,
    api_file_office_preview,
)
from kiro_crew.dashboard.file_api.outbox import (  # noqa: F401
    _audit_consented_download,
    _audit_owner_grant_refusal,
    _owner_grant_releases_flagged,
    _read_outbox_file,
    api_outbox_download,
    api_outbox_list,
    api_outbox_notify,
)
from kiro_crew.dashboard.file_api.path_complete import (  # noqa: F401
    _complete_path_listing,
    _completion_segments,
    _open_completion_dir,
    _scan_completion_dir,
    api_path_complete,
)
from kiro_crew.dashboard.file_api.path_probe import (  # noqa: F401
    _PathProbe,
    _PathProbeBusy,
    _probe_busy_response,
    _probe_request_path,
    _run_path_probe,
    _validate_dashboard_path,
)
from kiro_crew.dashboard.file_api.pinned_io import (  # noqa: F401
    _CheckedFile,
    _classify_request_paths,
    _file_write_blocking,
    _open_checked,
    _open_checked_file,
    _open_rb_nofollow,
    _OpenDenied,
    _OpenedFile,
    _OpenRefusal,
    _read_request_path,
    _resolve_project_relative,
    _TextRead,
    api_file_kinds,
)
from kiro_crew.dashboard.file_api.project_dirs import (  # noqa: F401
    _git_head_path,
    _known_project_dirs,
    _match_known_project,
    _match_known_project_for,
    _project_git_branch,
    _read_git_meta_prefix,
    _redact_project_path,
    _resolve_project_git,
    _slot_project_snapshot,
    api_project_git,
)
from kiro_crew.dashboard.file_api.project_tree import (  # noqa: F401
    _project_tree_allot,
    _project_tree_body,
    _project_tree_fence,
    _project_tree_file_quotas,
    _project_tree_git_layout,
    _project_tree_identity,
    _project_tree_is_link,
    _project_tree_listing,
    _project_tree_pin,
    _project_tree_rebase,
    _project_tree_scandir,
    _project_tree_scandir_entries,
    _project_tree_subdir,
    _project_tree_walk,
    _project_tree_walk_pinned,
    _ProjectTreeFolderMoved,
    api_project_tree,
)
from kiro_crew.dashboard.file_api.search import (  # noqa: F401
    _audit_file_search_exit,
    _fuzzy_score,
    _resolve_search_root,
    _subsequence_run,
    api_file_search,
)
from kiro_crew.dashboard.file_api.sheet import (  # noqa: F401
    _load_sheet_payload,
    _parse_workbook_grid,
    _sheet_cell_json,
    _sheet_formula_text,
    _SheetRefusal,
    _vet_zip_eocd,
    api_file_sheet,
)
from kiro_crew.dashboard.file_api.transfer import (  # noqa: F401
    _parse_range_header,
    api_file_download,
    api_file_stream,
    api_file_watch,
)
from kiro_crew.dashboard.file_api.uploads import (  # noqa: F401
    _content_matches_ext,
    _content_mismatch_message,
    _log_upload_diagnostic,
    _resolve_raster_ext,
    _sniff_media_type,
    _stream_media_part,
    _upload_dir,
    _write_file_restricted,
    api_upload_file,
)
from kiro_crew.dashboard.file_api.workspaces import (  # noqa: F401
    _resolve_ws_dir,
    _WorkspaceConflict,
    api_workspaces,
    api_workspaces_create,
    api_workspaces_delete,
    api_workspaces_update,
)
from kiro_crew.dashboard.file_index import _SKIP_DIRS as _WALK_SKIP_DIRS  # noqa: F401
from kiro_crew.dashboard.handlers._shared import (  # noqa: F401
    _probe_persisted_session,
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.dashboard.handlers.messaging import _resolve_session_target  # noqa: F401
from kiro_crew.dashboard.origin import is_direct_local_request  # noqa: F401
from kiro_crew.dashboard.session_folders import (  # noqa: F401
    SESSION_FOLDER_ROOT_MAX_CHARS,
    validate_session_folder_root,
)
from kiro_crew.dashboard.slot_projection import summary_rows  # noqa: F401
from kiro_crew.dashboard.state import (  # noqa: F401
    VALID_MEMORY_MODES,
    DashboardState,
    append_and_surface,
    load_window_off_loop,
)
from kiro_crew.dashboard.upload_limits import (  # noqa: F401
    bytes_to_mb_figure,
    knowledge_ceiling_bytes,
    upload_max_bytes,
)
from kiro_crew.doc_blocks import extract_blocks  # noqa: F401
from kiro_crew.doc_parser import extract_slides, extract_text, join_slides  # noqa: F401
from kiro_crew.git_config_hooks import (  # noqa: F401
    ConfigHookScanError,
    config_hook_disable_args_sandboxed,
)
from kiro_crew.git_worktree_scope import worktree_probe_failure_is_empty_scope  # noqa: F401
from kiro_crew.github_runner import validate_provider_executable  # noqa: F401
from kiro_crew.hooks import (  # noqa: F401
    FileTooLargeError,
    is_unc_shape,
    safe_read_file_bytes,
    safe_read_prefix,
)
from kiro_crew.messaging.display_safety import redact_for_display  # noqa: F401
from kiro_crew.messaging.outbound_files import OutboundFile  # noqa: F401
from kiro_crew.messaging.raster import SNIFF_BYTES, sniff_raster_mime  # noqa: F401
from kiro_crew.owner_only_files import ensure_directory  # noqa: F401 - owner: uploads
from kiro_crew.pdf_extract import PdfExtraction, extract_pdf_segments  # noqa: F401
from kiro_crew.platform import binary_content_is_flagged  # noqa: F401
from kiro_crew.platform import wide_content_is_flagged  # noqa: F401
from kiro_crew.platform import redact_via_context as redact  # noqa: F401
from kiro_crew.platform.context import (  # noqa: F401
    redact_log_via_context,
    redact_owner_view_via_context,
)
from kiro_crew.sandbox import (  # noqa: F401
    cgroup_scope_argv,
    popen_limited,
    sandboxed_spawn_argv,
    wrap_argv,
)
from kiro_crew.security import (  # noqa: F401
    _CRED_CLASS,
    _STREAM_HOLDBACK_JWT_MAX,
    _STREAM_HOLDBACK_MAX,
    BINARY_MIME_ALLOWLIST,
    is_sensitive_canonical_path,
    is_sensitive_path,
    is_sensitive_resolved_path,
    path_contains_sensitive,
    redact_credentials,
    redact_exfiltration_urls,
    redact_path_segments,
    redaction_switch,
    sandbox_credential_targets,
)
from kiro_crew.validation import (  # noqa: F401
    FILE_READ_SCHEMA,
    MODEL_ID_RE,
    MODEL_PICKER_HIDDEN_MODEL_ID_RE,
    MODEL_PICKER_HIDDEN_MODELS_MAX,
    ValidationError,
    validate_tool_args,
)
from kiro_crew.zip_vet import (  # noqa: F401
    ZipInventoryRejected,
    vet_zip_inventory_bytes,
)

# Register OOXML office MIME types explicitly. The system mimetypes
# database on AL2/AL2023 build hosts does NOT include .docx, .xlsx, or
# .pptx by default, so mimetypes.guess_type() returns (None, None) for
# those. Registering at module import time keeps api_file_download's
# Content-Type header correct for the most common Word/Excel/PowerPoint
# downloads.
mimetypes.add_type(
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx",
)
mimetypes.add_type(
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx",
)
mimetypes.add_type(
    "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx",
)

_INLINE_DISPOSITION_PREFIXES = frozenset({"audio/", "video/", "image/", "application/pdf"})

#: Session-key namespace of a sub-agent run. A sub-agent has no tab of its own, so
#: its file card belongs to the PARENT's tab — the surface every other sub-agent
#: output already routes to (``subagent_manager.monitoring``'s completion
#: injection, ``chat_utils.subagent_event_slot``'s WS frames).
_SUBAGENT_SESSION_PREFIX = "subagent:"


logger = logging.getLogger(__name__)


# The Slack probe a channel upload hands to the destination oracle, defined in the
# ``channel_upload`` owner. Bound by attribute: the one ``import`` of this name stays the
# function-local one from ``kiro_crew.slack.handler``.
is_tracked_channel = _owner_channel_upload.is_tracked_channel


def _subagent_parent_session_key(state: DashboardState, session_key: str) -> str:
    """The parent session key of the sub-agent running under *session_key*, or ``""``.

    Matches on BOTH spellings a run can be keyed by — its ``conversation_key`` (a
    continuable run) and ``subagent:<id>`` — the same comparison
    ``subagent_manager.continuation`` makes, because a continuable run's key is not
    derivable from its id. Returns ``""`` when the manager is absent or the run is
    unknown, so the caller SUPPRESSES the card rather than guessing a tab.
    """
    manager = getattr(state, "subagents", None)
    if manager is None:
        return ""
    try:
        # A PROPERTY, not a method (``subagent.py`` ``@property all_agents``).
        # Calling it invoked the returned LIST, so every lookup raised TypeError,
        # the except below swallowed it, and the card was suppressed for every
        # sub-agent -- the routing this function exists to do never happened once.
        agents = list(manager.all_agents)
    except Exception:
        logger.warning("outbox notify: sub-agent roster unavailable", exc_info=True)
        return ""
    matches = [
        info
        for info in agents
        if (getattr(info, "conversation_key", "") or f"{_SUBAGENT_SESSION_PREFIX}{info.id}")
        == session_key
    ]
    if not matches:
        return ""
    # More than one record can carry ONE key: a continuation is minted as a new run
    # whose ``conversation_key`` is the original's ``subagent:<id>``, and the
    # original (spawned with an empty conversation_key) resolves to that same
    # string. Their parents differ whenever a DIFFERENT session continued the
    # conversation -- so taking the first match routes the card to whichever chat
    # happens to sit earlier in the roster, which is the PREVIOUS owner's tab.
    #
    # Newest ACTIVE run wins: a live run is the one the card belongs to, and among
    # equals the most recently started. Ranked rather than filtered so a roster of
    # only-finished records still answers with the latest instead of nothing.

    def _rank(info: object) -> tuple[int, float]:
        # Defensive reads: a stubbed manager can hand back non-bool/non-number here,
        # and a comparison against those raises inside the sort rather than routing.
        done = getattr(info, "done", False)
        started = getattr(info, "started", 0.0)
        return (
            0 if (done is True) else 1,
            float(started) if isinstance(started, (int, float)) else 0.0,
        )

    best = max(matches, key=_rank)
    parent = getattr(best, "parent_session_key", "")
    # isinstance, not truthiness: a stubbed manager can hand back a
    # non-str here and dashboard_slot_key would treat it as a key.
    return parent if isinstance(parent, str) else ""


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811
    return _pkg.sel()


def _audit_file_send(
    *,
    leg: str,
    outcome: str,
    error: str | None = None,
    downstream: str | None = None,
    resources: str | None = None,
) -> None:
    """The one audit shape both ``file_send`` delivery legs write.

    Every record the Slack and channel endpoints emit is the same tool
    invocation under a different ``tool_kind`` (the leg), so the shape lives
    here rather than being spelled out at each of the dozen decision sites that
    write it -- a copy per site puts a drifted field one edit away. Optional
    fields are OMITTED when unset: skips carry no ``downstream_service``,
    refusals and deliveries do.
    """
    extra: dict[str, str] = {}
    if error is not None:
        extra["error"] = error
    if downstream is not None:
        extra["downstream_service"] = downstream
    if resources is not None:
        extra["resources"] = resources
    _sel().log_tool_invocation(
        session_key="api",
        source="api",
        tool_name="file_send",
        tool_kind=leg,
        outcome=outcome,
        **extra,
    )


def _body_err_code(body_err: web.Response) -> str:
    """SEL error label for a refused body read.

    Derived from the guard response's machine-readable ``code`` so the audit
    record distinguishes a parse failure from an oversized body (413
    ``payload_too_large``) instead of filing every refusal as a JSON error.
    """
    try:
        parsed = json.loads(body_err.text or "")
    except ValueError:
        return "invalid_json_body"
    code = parsed.get("code") if isinstance(parsed, dict) else None
    return str(code) if code else "invalid_json_body"


# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage_store.py is the reference implementation.
_SCREENSHOT_DIR: Path | None = None

_UPLOAD_DIR: Path | None = None


_MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB per file
#: Video gets its own, larger ceiling: a 30-second retina screen recording is
#: routinely 60-150 MB, so the 50 MB document cap would reject the dominant
#: case and make the feature read as broken. Safe to raise only because video
#: parts STREAM to disk (:func:`_stream_video_part`) instead of accumulating in
#: memory the way every other accepted type does.
_MAX_VIDEO_UPLOAD_BYTES = 512 * 1024 * 1024  # 512 MB per video
_MAX_UPLOAD_FILES = 20  # max files per request

# Fallback-walk budgets. ``_WALK_MAX_SCAN_*`` bounds entries scored PER KIND
# (anti-starvation); ``_WALK_MAX_DIRS_VISITED`` bounds directories entered and is
# what guarantees termination -- see ``_walk_file_search``. Not a multiple of the
# per-kind budget: in a narrow-deep tree directory names grow at the same rate as
# directories visited, so a derived ceiling is unreachable exactly when it is
# needed. Module-level so tests can shrink them.
_WALK_MAX_SCAN_SCOPED = 50_000
_WALK_MAX_SCAN_UNSCOPED = 5_000
_WALK_MAX_DIRS_VISITED = 20_000

#: Wall-clock budget for ONE ``_walk_file_search`` pass. The scan/dirs ceilings
#: above bound how MANY filesystem calls the walk makes, not how LONG each one
#: takes. On local disk a ceiling-reaching walk measured 4.20s worst, so the
#: client's own 15s deadline (``FILE_SEARCH_TIMEOUT_MS`` in ``api/client/files.ts``)
#: has ~3.6x headroom there -- but that figure is local disk only. On an
#: NFS/SSHFS-class mount each ``os.stat``/``scandir`` is a network round trip, so
#: the same bounded number of calls can run many times longer and blow straight
#: past the client's 15s; the client's Retry re-enters the same bound, so a
#: healthy-but-slow store fails every attempt. This bounds the one term store
#: speed inflates: when a per-directory or per-stride clock check finds the
#: budget spent, the walk stops and returns the partial set it gathered, the
#: same early stop the entry/dir ceilings already make for size, reached by
#: elapsed time. The check is cooperative, so a single ``os.stat`` that blocks
#: on a dead mount can overrun the budget before the next check runs; this
#: shortens the common slow-but-alive case rather than guaranteeing a hard
#: wall-clock cap. 10s is chosen to sit
#: under the client's 15s on a store that
#: answers each call, with room for admission and round trips.
#: Module-level so a test can shrink it, exactly as ``_GREP_TIME_BUDGET_SECS``.
_WALK_TIME_BUDGET_SECS = 10.0

#: How many entries one ``_collect`` scans between wall-clock checks in
#: ``_walk_file_search``. A single enormous directory could otherwise overrun the
#: budget between the per-directory checks, so the entry loop re-reads the clock
#: every stride -- the same trick, and for the same reason, as the grep path's
#: ``_GREP_ROW_DEADLINE_STRIDE``. Module-level so a test can shrink it.
_WALK_DEADLINE_STRIDE = 512


#: The monotonic clock ``_walk_file_search`` reads for its wall-clock budget,
#: named on the module (the facade every owner function resolves against) rather
#: than called as ``time.monotonic`` inline. A deadline test drives the walk's
#: clock deterministically by patching THIS name -- advancing it per yielded
#: directory so the budget crosses on a chosen directory -- which touches only
#: the walk and leaves the stdlib ``time`` module untouched for the event loop
#: and every other test (tests-are-deterministic). ``time.monotonic`` is a
#: facade name no owner captures, so the patch cannot silently miss a caller.
def _walk_monotonic() -> float:
    return time.monotonic()


# Hard ceiling on the caller-supplied ``limit`` of /api/file-search. The walk
# collects ``max_results * 10`` candidates per kind, so the limit multiplies real
# filesystem work; a fixed server-side ceiling keeps a hostile ``?limit=`` from
# turning the endpoint into a filesystem-walk amplifier. Mirrored client-side as
# SEARCH_RESULT_LIMIT_MAX in FolderPanel.tsx.
_SEARCH_LIMIT_CEILING = 60
_ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_ALLOWED_TEXT_EXT = {
    ".txt",
    ".text",
    ".xwiki",
    ".md",
    ".json",
    ".jsonl",
    # Excalidraw scene JSON — the composer's sketch pad attaches one per
    # sketch, and the dashboard has a dedicated read-only renderer for it
    # (FileRenderers routes on this exact extension). Content-wise it is
    # ordinary JSON text.
    ".excalidraw",
    ".har",
    ".yaml",
    ".yml",
    ".xml",
    # draw.io / diagrams.net XML source.
    ".drawio",
    ".csv",
    ".tsv",
    ".log",
    # CNAB remittance (.rem) and return (.ret) files — the Brazilian banking
    # (FEBRABAN) interchange format. Fixed-width ASCII/Latin-1 records, so they
    # read as plain text; the read path and FileRenderers already treat an
    # unknown extension as text, this just lets them past the upload boundary.
    ".rem",
    ".ret",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".css",
    ".sh",
    ".bash",
    ".rb",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
}
_ALLOWED_DOC_EXT = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".odt",
    ".ods",
    ".odp",
    ".rtf",
    ".zip",
    ".tar",
    ".gz",
}
#: Video containers accepted at the upload boundary. Deliberately narrower than
#: ``FileRenderers``' VIDEO_EXTS: every entry here must be verifiable by
#: :func:`_sniff_media_type` AND playable by ``<video>``, so an accepted upload
#: is always one the chat can actually show. ``.mkv`` is excluded — it shares
#: WebM's EBML signature but browser playback is unreliable, and accepting a
#: file that then refuses to play is worse than refusing it at the door.
_ALLOWED_VIDEO_EXT = {".mp4", ".m4v", ".mov", ".webm"}
#: Media containers a browser will often play but the upload boundary does not
#: accept. Rejecting them with the bare "Unsupported file type" reads as "video
#: is not supported at all", when the actual remedy is a re-encode -- so the
#: refusal for one of these names the containers that do work. VIDEO containers
#: only: naming the video set to an audio upload (``.m4a``) would tell its
#: sender to re-encode audio into a video container, which is worse than the
#: bare refusal.
_VIDEO_HINT_EXT = frozenset(
    {".mkv", ".ogv", ".avi", ".mpg", ".mpeg", ".wmv", ".flv", ".3gp"}
)
#: Media type :func:`_sniff_media_type` must report for the claimed video
#: extension. The MP4 family (mp4/m4v/mov) all carry a ``ftyp`` box at offset 4
#: and sniff as ``video/mp4``; QuickTime's brand differs but the box does not.
#:
#: This gate proves the bytes are the claimed FAMILY, not the exact container:
#: ``.webm`` and ``.mkv`` share the EBML magic, so an ``.mkv`` renamed to
#: ``.webm`` passes here even though the ``.mkv`` extension is refused.
#: Distinguishing them needs the EBML DocType, which is not worth parsing for
#: this boundary -- the gate's job is to keep NON-media bytes off disk (CWE-434),
#: and the extension set is what carries the narrower "accepted means playable"
#: promise.
_VIDEO_EXT_MIME: dict[str, str] = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/mp4",
    ".webm": "video/webm",
}
#: Audio containers accepted at the upload boundary. Every entry must be
#: verifiable by :func:`_sniff_media_type` and playable by ``<audio>``.
_ALLOWED_AUDIO_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".flac"}
#: Media type :func:`_sniff_media_type` must report for each audio extension.
#: Ogg carries Vorbis and Opus alike. ``.m4a`` shares the BMFF ``ftyp`` family
#: with MP4, which the sniffer reports as ``video/mp4`` regardless of track type.
_AUDIO_EXT_MIME: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".m4a": "video/mp4",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
}
#: Every media extension whose bytes are gated by :func:`_sniff_media_type`.
_MEDIA_EXT_MIME: dict[str, str] = {**_VIDEO_EXT_MIME, **_AUDIO_EXT_MIME}


# Magic-byte signatures for content-type validation at the upload boundary
# (CWE-434). The extension is attacker-controlled, so binary types are verified
# against their file signature BEFORE the bytes are written. Raster types are
# verified by the shared sniffer (:mod:`kiro_crew.messaging.raster`), so all
# consumers agree on what counts as each image type (including WebP's form tag
# at offset 8, which a bare ``RIFF`` prefix would not check). Text formats (and
# SVG, which is XML) have no reliable magic and remain gated by the extension
# allowlist only.
_ZIP_CONTAINER_EXTS = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".zip"}
#: Raster extensions and the mime :func:`sniff_raster_mime` must report for
#: the claimed extension to be accepted.
_RASTER_EXT_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
}
#: Non-raster binary types that still carry a reliable leading signature.
_MAGIC_PREFIXES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".gz": (b"\x1f\x8b",),
}
#: Read-path extras the shared raster table does not cover (served by
#: ``api_file_raw`` but never accepted at the upload boundary).
_READ_PATH_EXTRA_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"II\x2a\x00", "image/tiff"),
    (b"MM\x00\x2a", "image/tiff"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
)


#: Canonical upload extension per sniffed raster type: the suffix a mislabelled
#: raster is stored under so every downstream consumer that infers the mime
#: from the path (ACP image inlining, /api/file-raw) reads the true type.
_RASTER_MIME_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
}
#: ISO-BMFF brands of still-image containers (HEIF/HEIC/AVIF). An iPhone photo
#: that reaches the browser as ``IMG_1234.jpeg`` is routinely one of these, and
#: the generic "not really a .jpeg" sentence leaves the user guessing why.
_HEIF_BRANDS = frozenset(
    {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis"}
)


#: Every control character: C0, DEL, and the C1 block.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


_ProbeT = TypeVar("_ProbeT")

#: How long a request waits for a free worker before it is refused with 503.
#: Sized to absorb a burst of healthy probes (each takes milliseconds), not to
#: outwait a dead mount; the same figure the sensitive-path resolver's own budget
#: uses.
_PATH_PROBE_ADMIT_TIMEOUT_SECS = 2.0
#: Execution ceiling handed to ``run_in_cron_pool``, which requires one. It is
#: deliberately NOT a request timeout: this change bounds how many probes can be
#: wedged, not how long a client waits on one, and a shorter figure here would
#: add a second refusal class this endpoint family does not yet define. Large
#: enough that no healthy transfer under the module's own caps reaches it.
_PATH_PROBE_EXEC_CEILING_SECS = 3600.0


#: Most paths one ``POST /api/file-kinds`` request may name. A transcript view
#: asks for a few dozen; the cap bounds how long one request holds its probe
#: worker, and the client splits a larger set across requests.
_FILE_KINDS_MAX_PATHS = 100


#: How much of a file /api/file-read returns, in CHARACTERS -- the unit matters,
#: because the decode is a text wrapper over a byte descriptor and a byte count
#: here would mis-set ``X-Truncated`` on multi-byte content.
_FILE_READ_CAP = 512_000

#: One credential-class character (the streaming redactor's ``_CRED_CLASS``).
#: Matched against the REVERSED read, so finding the trailing run is linear.
_CRED_RUN_REVERSED_RE = re.compile("[" + "".join(re.escape(c) for c in sorted(_CRED_CLASS)) + "]*")


#: How much of a file the binary sniff reads before deciding, in BYTES. 8 KiB is
#: the window the Files app already uses (``_is_binary_file`` in
#: ``apps/builtins/file_explorer/server.py``); the two surfaces disagreeing about
#: what "binary" means is a worse outcome than either window being wrong.
_FILE_READ_SNIFF_BYTES = 8192

#: Extensions that are binary by format, answered BEFORE the NUL sniff.
#:
#: The sniff alone is not sufficient: a NUL-free binary format reads as text and
#: opens an editable buffer over bytes a save would corrupt -- a GNU thin `.a`
#: archive stores only ASCII member-header references, so its first 8 KiB can
#: hold no NUL at all. The sniff still runs after this check and remains what
#: catches an extension-LESS binary, which is why neither half is redundant.
#:
#: Kept byte-identical to ``BINARY_EXTS`` in
#: ``src/kiro_crew/apps/builtins/file_explorer/server.py`` -- the Files app and
#: this endpoint must answer "is this binary" the same way, and
#: ``test_dashboard_file_io.py`` fails if the two sets ever diverge.
_FILE_READ_BINARY_EXTS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".pdf",
        ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
        ".so", ".dylib", ".dll", ".exe", ".class", ".jar", ".war", ".o", ".a",
        ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv", ".webm",
        ".sqlite", ".db", ".duckdb",
        ".ttf", ".otf", ".woff", ".woff2", ".eot",
    }
)


async def _owner_view_bypasses_credential_pass(request: web.Request) -> bool:
    """Whether THIS request renders the owner's own view with the credential pass
    stood down: the requester is the dashboard owner AND the owner's switch is OFF.

    The two handlers that feed the file viewer -- ``api_file_read`` (the buffer)
    and ``api_file_diff`` (the ``original`` it is compared against) -- call this
    with the same request, so both sides of one render carry the same verdict.
    A non-owner never bypasses; the verdict is read off the event loop per
    request, so it is never older than the response it shapes. The keystone is a
    fixed, trusted path (not caller-supplied), so the default executor is the
    right one.
    """
    from kiro_crew.dashboard.handlers.source_providers import (  # lazy: import cycle
        owner_view_for_request,
    )

    if not owner_view_for_request(request):
        return False
    switch = await asyncio.to_thread(redaction_switch.read_state)
    return not switch.enabled


# Extensions previewable via kiro_crew.doc_parser (OOXML docx/pptx). Legacy
# binary formats (.doc, .ppt), the OpenDocument family (.odt/.ods/.odp), and
# spreadsheet formats (.xls/.xlsx) fall through to the download card because
# doc_parser only understands ZIP+XML OOXML, and adding openpyxl or a legacy
# OLE reader would grow the dependency tree noticeably for a preview feature.
_OFFICE_PREVIEWABLE_EXT = {".docx", ".pptx"}
# Cap the returned text so a huge .docx doesn't blow the JSON payload / DOM.
# Mirrors api_file_read's 512 KB read cap. Anything larger is truncated and
# the frontend shows a "Download for full contents" affordance.
_OFFICE_PREVIEW_CAP = 512_000

#: Block keys whose value is structure, not document text: a fixed vocabulary the
#: frontend switches on, a nesting level, or a formatting flag. None can carry a
#: secret, and rewriting one could only corrupt the shape.
_BLOCK_STRUCTURAL_KEYS = frozenset({"type", "level", "ordered", "bold", "italic"})


# ── /api/file-stream: Range-capable audio/video serving (file_api/transfer.py) ─
# The media cap is deliberately larger than _MAX_UPLOAD_BYTES: screen
# recordings routinely exceed 50 MB, and unlike file-raw this endpoint never
# materializes the file in memory -- Range streaming reads bounded chunks, so
# the cap only bounds what one URL can address, not per-request memory.
_STREAM_MAX_BYTES = 2 * 1024 * 1024 * 1024
_STREAM_CHUNK_BYTES = 256 * 1024
# Text-exfiltration probe window. Real media is binary within the first
# bytes; content that decodes as UTF-8 text this deep is a text file wearing
# a media magic, which the redaction scan in ``api_file_stream`` must see.
_STREAM_TEXT_PROBE_BYTES = 64 * 1024


# Container signature -> Content-Type. Sniffed from the file's first bytes so
# the endpoint serves media by CONTENT, not by extension claim (CWE-434 shape,
# same posture as file-raw's image allowlist). Entries are (offset, magic,
# mime). MP4-family uses the ftyp box at offset 4 (bytes 0-3 are the box
# size); WebM and Matroska share the EBML magic and both play in <video>.
_MEDIA_MAGIC: tuple[tuple[int, bytes, str], ...] = (
    (4, b"ftyp", "video/mp4"),          # mp4 / m4v / m4a / mov (BMFF family)
    (0, b"\x1a\x45\xdf\xa3", "video/webm"),  # webm / mkv (EBML)
    (0, b"OggS", "audio/ogg"),          # ogg audio or video; <audio>/<video> both accept
    (0, b"fLaC", "audio/flac"),
    (0, b"ID3", "audio/mpeg"),          # mp3 with ID3v2 tag
    (0, b"\xff\xfb", "audio/mpeg"),     # bare mp3 frame sync (MPEG1 layer3)
    (0, b"\xff\xf3", "audio/mpeg"),
    (0, b"\xff\xf2", "audio/mpeg"),
)


# ── Path completion (/api/path-complete; file_api/path_complete.py) ──────────
#
# The chat composer's shell-style `./` / `../` completion. A sibling of
# ``api_file_search`` rather than a mode of it, for two reasons that are
# not cosmetic:
#
# * ``/api/file-search`` answers "which files ANYWHERE under this root fuzzily
#   match these characters"; completion answers "what is IN this one directory".
#   A recursive fuzzy hit cannot be turned back into the path the user is typing
#   -- the entry name alone is not the path -- so the row set has to come from a
#   single directory level.
# * ``?project=`` on the search endpoint is any path on the host, by design.
#   Completion must be the opposite: the caller names a KNOWN project directory
#   (the same allow-list ``api_project_git`` / ``api_project_tree`` use) and the
#   ``../`` segments are resolved and then re-checked for containment, so no
#   token typed in the composer can enumerate a directory outside the project.

#: Rows returned by one completion request. The composer popup shows a handful;
#: this bounds the response for a directory with thousands of entries, which is
#: also where a shell's own completion stops being useful.
_PATH_COMPLETE_MAX_ENTRIES = 50

#: Either separator ends a segment of a typed path token. Both, on every platform:
#: a backslash IS a separator on Windows, so a token carrying one must be SPLIT
#: rather than appended as a single literal name that the OS then re-interprets at
#: the open -- which is how `..\..\etc` escaped a root that had already been
#: checked. The composer's own grammar is POSIX-style, so on POSIX this only
#: refuses to treat a backslash as part of a filename, which no completion token
#: means it to be.
_PATH_TOKEN_SEPARATORS = re.compile(r"[/\\]+")

#: Directory entries EXAMINED per request, independent of how many survive the
#: prefix filter. The listing is one level deep, so this is the only ceiling
#: needed -- it bounds ``node_modules``-sized directories, where the scan (not
#: the response) is the cost.
_PATH_COMPLETE_MAX_SCAN = 5000


# ── Content search (/api/file-grep; engines in file_api/grep.py) ──────────────
#
# The side-panel Files rail's Content mode: "which files CONTAIN this text".
# ``POST``, because the query travels in the body -- see ``api_file_grep``.
# Filename search is ``api_file_search``. The Files app's own search
# (``apps/builtins/file_explorer/server.py``) answers the same question in a
# separate process with its own allow-root model; the row shape is shared with
# it (``file``/``line``/``preview``; see `_grep_hit` for why there is no column)
# but nothing is shared at runtime -- gating here goes through this module's
# ``_validate_dashboard_path`` / ``is_sensitive_path`` chokepoint.

#: Wall-clock budget for ONE request, shared by the text and document passes.
#: The rail queries on a keystroke debounce, so a partial answer that SAYS it is
#: partial (``truncated``) beats a complete one that arrives after the user
#: stopped waiting.
_GREP_TIME_BUDGET_SECS = 2.0
#: Ceiling on returned hits. Both engines report one hit per file, so this also
#: bounds how many files a response names.
_GREP_MAX_RESULTS = 200
#: Per-file read ceiling for the python fallback; ripgrep gets the same number
#: via ``--max-filesize`` so both engines skip the same files.
_GREP_MAX_FILE_BYTES = 2 * 1024 * 1024
#: Preview length, matching the Files app's search rows.
_GREP_PREVIEW_CHARS = 400
#: A location label is a few words, but a workbook's sheet title is author text
#: of any length, so it is bounded like the preview.
_GREP_LABEL_CHARS = 120
#: A one-character query is a whole-tree read, not a search. Same floor as
#: ``api_file_search``.
_GREP_MIN_QUERY_CHARS = 2
_GREP_MAX_QUERY_CHARS = 200
#: Directories either pass may descend into. The deadline alone does not bound
#: traversal: a tree of empty directories advances the walk without reading a
#: file.
_GREP_MAX_DIRS_VISITED = 20_000

#: Containers the document pass extracts. Their bytes hold no searchable plain
#: text, so the text pass skips them and this pass owns them -- no file is
#: reported twice. ``.pdf`` is extracted in a memory-bounded child
#: (``kiro_crew.pdf_extract``), never in this process: ``pdfplumber`` exposes no
#: length limit, so the only ceiling that can precede its allocation is a kernel
#: one on a process the gateway can afford to lose.
_GREP_DOC_EXTS = frozenset({".docx", ".pdf", ".pptx", ".xlsx"})
#: Largest document the pass will open. Extraction is CPU-bound parsing, so
#: this is about parse cost, not read cost.
_GREP_DOC_MAX_BYTES = 25 * 1024 * 1024
#: Extracted text per document, handed to ``extract_text``'s ``max_chars``.
_GREP_DOC_MAX_CHARS = 400_000
#: How often the worksheet row loop reads the clock. An empty row yields no text,
#: so the character cap cannot see it and only the deadline can; a stride bounds
#: the overshoot to a fixed row count without paying ``monotonic()`` per row.
_GREP_ROW_DEADLINE_STRIDE = 512

#: How long teardown waits for the reader thread and the killed child. Short:
#: a search must not hold a transfer-pool worker on something being torn down.
_GREP_RG_TEARDOWN_SECS = 2.0
#: How long the record loop blocks on one ``get`` before re-checking the deadline
#: and whether the reader died. Bounds only how long a FINISHED search whose
#: sentinel was dropped goes unnoticed; the search's budget is the deadline.
_GREP_RG_POLL_SECS = 0.05
#: Stands in for a record too large to queue. A real ``rg --json`` record starts
#: with ``{``, so a NUL-led string cannot be one.
_GREP_RG_OVERSIZE = "\0oversize"
#: Lines held between the rg reader thread and the parse loop. Small on purpose:
#: the reader blocks on a full queue, so a fast rg cannot buffer ahead of the
#: deadline check.
_GREP_RG_QUEUE_LINES = 64
#: Largest ``rg --json`` record the reader queues. ``--max-count 1`` bounds
#: records per FILE and ``--max-filesize`` bounds the file, but a matching line
#: in a minified blob is one multi-megabyte record, and a queue of those is
#: hundreds of MB. A record past this is skipped and marks the answer short.
_GREP_RG_MAX_RECORD_BYTES = 64 * 1024

#: Header ``doc_parser._extract_pptx`` writes before each slide's text.
_GREP_PPTX_SLIDE_RE = re.compile(r"^-{3}\s*Slide\s+(\d+)\s*-{3}$")

#: One document's extracted ``((label, text), ...)`` plus ``whole``: did the
#: reader see all of its text? False after the deadline, a mid-read failure or
#: the character cap -- any of them can hide a match, so the answer says it is
#: short.
_DocSegments = tuple[tuple[tuple[str, str], ...], bool]


# Whole-text diff budget, independent of the file preview's smaller read cap.
_FILE_DIFF_MAX_BYTES = 32 * 1024 * 1024


#: A Windows drive root -- ``C:``, ``C:\\`` or ``C:/`` -- with nothing after it.
#: Only such a path has a parent the filesystem cannot name: ``ntpath.dirname``
#: answers ``C:\\`` for ``C:\\``, which the browser reads as "no parent" and
#: hides its Back control on, stranding the user on one drive.
_WIN_DRIVE_ROOT_RE = re.compile(r"[A-Za-z]:[\\/]?")


async def api_browse_dirs(request: web.Request) -> web.Response:
    """GET /api/browse-dirs?path=... — list subdirectories for directory browser.

    ``?drives=1`` (Windows only) lists the mounted drive roots instead, as the
    virtual level above every ``X:\\``; the response carries ``path: ""`` --
    the one listing that is not a directory -- and ``parent: ""`` so the picker
    knows it is at the top. On other platforms the flag is a 400: there is no
    such level to show.
    """
    owner_denied = await require_owner_dashboard_request(request, "browse_dirs")
    if owner_denied is not None:
        return owner_denied
    caller = request.get("user", "dashboard")
    if request.query.get("drives") == "1":
        if not platform_compat.IS_WINDOWS:
            return web.json_response({"error": "Drive listing is only available on Windows", "code": "drives_windows_only"}, status=400)
        try:
            drives = await _run_path_probe(_browse_drives_sync, transfer=True)
        except _PathProbeBusy:
            return _probe_busy_response(resource="drives", operation="browse_dirs", caller=caller)
        _sel().log_api_access(caller=caller, operation="browse_dirs", outcome="allowed", resources="drives")
        return web.json_response({"path": "", "parent": "", "dirs": drives})
    raw = request.query.get("path", "").strip()
    # Off-loop: realpath then the isdir probe, on a caller-supplied root (the
    # shared resolver answers $HOME for an unnamed one). is_sensitive_path below
    # resolves on its own bounded pool, so it cannot wedge the loop.
    try:
        base, base_is_dir = await _run_path_probe(_resolve_search_root, raw)
    except _PathProbeBusy:
        return _probe_busy_response(resource=raw, operation="browse_dirs", caller=caller)
    if not base_is_dir:
        # Coded so the UI can name a permanent path refusal: without `code` the
        # cause classifier degrades this to the recoverable arm and offers a
        # Refresh that can never succeed.
        return web.json_response(
            {"error": "Not a directory", "code": "not_a_directory", "path": base},
            status=400,
        )
    if is_sensitive_path(base):
        _sel().log_api_access(caller=caller, operation="browse_dirs", outcome="denied", resources=base, error="sensitive path")
        return web.json_response({"error": "Access denied", "code": "access_denied"}, status=403)
    skip = {".git", "node_modules", "__pycache__", ".cache", ".venv", "venv", "env", ".kirocrew", ".kiro", ".aim"}
    skip |= _HIDDEN_TOOL_DIRS
    try:
        dirs = await _run_path_probe(_browse_dirs_sync, base, skip, transfer=True)
    except _PathProbeBusy:
        return _probe_busy_response(resource=base, operation="browse_dirs", caller=caller)
    _sel().log_api_access(caller=caller, operation="browse_dirs", outcome="allowed", resources=base)
    return web.json_response({"path": base, "parent": _browse_parent(base), "dirs": dirs})


#: Depth ceiling for the walk-up that looks for a repository root. A project
#: directory nested deeper than this below its repo root is reported as
#: not-a-repo rather than paying an unbounded number of stat calls per request.
_GIT_ROOT_WALK_LIMIT = 40

#: A HEAD file is one short line; cap the read so a hostile symlink to something
#: enormous cannot be slurped into memory.
_HEAD_READ_LIMIT = 4096


_MACOS_TEMP_PROJECT_PREFIX_RE = re.compile(
    r"\A/private/var/folders/[a-z0-9]{2}/[a-z0-9_]{30}/T(?=/|\Z)"
)


async def api_browse_files(request: web.Request) -> web.Response:
    """GET /api/browse-files?path=... — list files and subdirectories for the activity-panel file browser.

    Mirrors api_browse_dirs security model (sensitive-path filtering, access logging,
    skip set for build artifacts) but returns files alongside directories. Entries
    are sorted dirs-first then alphabetically; hidden files and common build dirs
    are skipped.
    """
    owner_denied = await require_owner_dashboard_request(request, "browse_files")
    if owner_denied is not None:
        return owner_denied
    caller = request.get("user", "dashboard")
    raw = request.query.get("path", "").strip()
    # Off-loop: realpath then the isdir probe, on a caller-supplied root (the
    # shared resolver answers $HOME for an unnamed one). is_sensitive_path below
    # resolves on its own bounded pool, so it cannot wedge the loop.
    try:
        base, base_is_dir = await _run_path_probe(_resolve_search_root, raw)
    except _PathProbeBusy:
        return _probe_busy_response(resource=raw, operation="browse_files", caller=caller)
    if not base_is_dir:
        # Same code as browse_dirs: the folder panel classifies both listings.
        return web.json_response(
            {"error": "Not a directory", "code": "not_a_directory", "path": base},
            status=400,
        )
    if is_sensitive_path(base):
        _sel().log_api_access(caller=caller, operation="browse_files", outcome="denied", resources=base, error="sensitive path")
        return web.json_response({"error": "Access denied", "code": "access_denied"}, status=403)
    skip = {".git", "node_modules", "__pycache__", ".cache", ".venv", "venv", "env", ".kirocrew", ".kiro", ".aim", "build", "dist", ".next"}
    skip |= _HIDDEN_TOOL_DIRS
    try:
        dirs, files = await _run_path_probe(_browse_files_sync, base, skip, transfer=True)
    except _PathProbeBusy:
        return _probe_busy_response(resource=base, operation="browse_files", caller=caller)
    _sel().log_api_access(caller=caller, operation="browse_files", outcome="allowed", resources=base)
    return web.json_response({"path": base, "parent": _browse_parent(base), "dirs": dirs, "files": files})


# ── /api/file-sheet: xlsx → JSON cell grid (file_api/sheet.py) ───────────────
# Caps bound what one request can materialize server-side and ship to the
# browser. 500 rows matches CsvViewer's display cap so the two table viewers
# truncate consistently. The member/expansion caps bound zip inflation: the
# on-disk size cap only limits the COMPRESSED archive, and a crafted workbook
# can expand orders of magnitude larger than it stores.
_SHEET_MAX_SHEETS = 20
_SHEET_MAX_ROWS = 500
_SHEET_MAX_COLS = 100
_SHEET_MAX_MEMBERS = 4096
_SHEET_MAX_EXPANDED_BYTES = 200 * 1024 * 1024
# Text amplification caps. Shared strings are stored once in the archive but
# referenced per cell, so the expansion cap above does not bound the RESPONSE:
# one 32 KiB string referenced by every cell would amplify into gigabytes of
# JSON. Cells truncate individually, and the whole workbook gets a cumulative
# text budget past which the preview refuses (the frontend degrades to the
# download card).
_SHEET_MAX_CELL_CHARS = 2000
_SHEET_MAX_TEXT_CHARS = 5 * 1000 * 1000


# Generous per-entry allowance for the central-directory size preflight: a
# record is 46 bytes plus name/extra/comment, and OOXML part names are short.
_SHEET_MAX_CDIR_ENTRY_BYTES = 512


# ── Git status & log endpoints (file_api/git_panel.py) ──────────────────────


_GIT_PROBE_STDERR_CAP = 4096


# Repo-scoped config keys that hand git a program to run when it touches file
# content (status re-hashes modified files through ``filter.<name>.clean``).
# ``-c`` cannot neutralize arbitrary driver names, so a repo declaring one is
# refused outright — the same fail-closed stance as worktree.py's
# ``_checkout_filter``.
#
# The refusal message says the config DECLARES a driver and that policy refuses
# the check. It must not claim the program runs: matching is deliberately wider
# than execution. ``smudge`` fires on checkout, not on the status re-hash; and a
# driver no ``.gitattributes`` path maps to never runs at all. A probe that
# merely FAILS is a DIFFERENT fact -- no driver is known to exist there -- so it
# refuses under its own ``"unreadable"`` cause rather than borrowing this one.
# Refusing both is correct, since neither can be proven safe, but telling the
# reader a program ran is not, and neither is handing them both causes at once.
_GIT_FILTER_KEY_RE = re.compile(
    r"^filter\..+\.(process|smudge|clean)$", re.IGNORECASE
)


# Cap on the ROWS api_project_tree returns -- files and directory rows
# together (``_project_tree_allot``). One number for both kinds, because every
# row costs the same to redact, serialize and render, and the dashboard asks
# for this listing every 10 s while a tree is open: the work per poll has to be
# bounded by this number, not by the size of the project.
_PROJECT_TREE_MAX_ENTRIES = 10_000

# Directory entries the non-git walk may READ per listing, shared across the
# folders of each depth (``_project_tree_walk``). This is what makes the walk's
# cost a function of this number instead of the size of the tree: reading an
# entry costs about half a microsecond, so the whole budget is a fraction of a
# second in the worker thread. It is larger than the row cap so the walk can
# see past the rows it will show -- the subfolders of a large folder, and files
# for the round-robin sampling to share out -- and a folder cut by it is named
# as truncated, whether or not the row cap was also reached.
_PROJECT_TREE_SCAN_LIMIT = 20 * _PROJECT_TREE_MAX_ENTRIES

# Whether this platform lists a directory through a descriptor. Read once: it is
# a property of the platform, and a test seam that wraps ``os.scandir`` must not
# silently turn it off and move every read onto the by-name branch.
_PROJECT_TREE_SCANDIR_TAKES_FD = os.scandir in os.supports_fd


# Directories never worth listing in a workspace tree. Applied only on the
# non-git fallback walk — git listings already honor .gitignore.
_PROJECT_TREE_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        ".next",
        ".cache",
        "target",
        ".gradle",
        ".idea",
        # Kiro Crew state, hidden as in the Open/Browse picker.
        ".kiro",
        ".kirocrew",
    }
)
# The dot-named tool folders of the tree's set, also hidden by the Open/Browse
# picker and the file browser.
_HIDDEN_TOOL_DIRS = frozenset(d for d in _PROJECT_TREE_SKIP_DIRS if d.startswith("."))


# Every function the owners define runs on this module's globals, so a patch of
# ``kiro_crew.dashboard.handlers.files.<name>`` reaches it wherever it lives;
# see ``kiro_crew.dashboard.file_api``. Run once, after this body has bound every
# name.
_facade_compose(
    globals(),
    (
        _owner_uploads,
        _owner_workspaces,
        _owner_pinned_io,
        _owner_transfer,
        _owner_office_preview,
        _owner_sheet,
        _owner_search,
        _owner_path_complete,
        _owner_grep,
        _owner_browse,
        _owner_project_dirs,
        _owner_git_panel,
        _owner_project_tree,
        _owner_dashboard_config,
        _owner_path_probe,
        _owner_outbox,
        _owner_channel_upload,
        _owner_desktop,
        _owner_editor,
    ),
)
