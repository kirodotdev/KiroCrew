"""Dashboard file HTTP handlers: the facade over :mod:`kiro_crew.dashboard.file_api`.

Routes, the handlers package and tests import and patch every file handler through
this module. The handler families live in the ``file_api`` owners -- ``uploads``,
``workspaces``, ``pinned_io``, ``transfer``, ``office_preview``, ``sheet``,
``search``, ``path_complete``, ``grep``, ``browse``, ``project_dirs``,
``git_panel``, ``project_tree`` and ``dashboard_config`` -- and ``file_api.compose``
runs them on this module's globals, so a patch of ``files.<name>`` reaches them.

This module keeps the imports, constants and seams the owners read, the
``_run_path_probe`` chokepoint, file delivery (the outbox routes and the Slack and
channel uploads with their shared admission gate), and the handlers repository
guards read here by path: ``api_reveal_path``, ``api_upload``, ``api_screenshot``,
``api_file_read`` with ``_owner_view_bypasses_credential_pass``, ``api_file_diff``,
``api_file_raw``, ``api_file_write`` and ``api_file_grep``. The directory-picker
routes ``api_browse_dirs`` and ``api_browse_files`` stay here too, over the
``browse`` listings. New work goes to the owner of its family; ``docs/system-specs/modules/learn-cron-dashboard.md`` records
the map.
"""

from __future__ import annotations

import asyncio
import contextlib  # noqa: F401
import datetime as _dt  # noqa: F401
import errno  # noqa: F401
import functools
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
import subprocess
import sys
import threading  # noqa: F401
import time
import urllib.parse
import uuid  # noqa: F401
import zipfile  # noqa: F401
from dataclasses import asdict  # noqa: F401
from pathlib import Path, PurePath  # noqa: F401
from typing import BinaryIO, Callable, NamedTuple, TypeVar  # noqa: F401

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError  # noqa: F401
from aiohttp.multipart import BodyPartReader  # noqa: F401

from kiro_crew import executors, file_delivery_consent, pinned_fs, platform_compat  # noqa: F401
from kiro_crew.atomic_write import (  # noqa: F401
    atomic_write,
    open_access_control_source,
    pinned_parent_replace_supported,
)
from kiro_crew.config import loader as config_loader
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
from kiro_crew.dashboard import file_api as _file_api
from kiro_crew.dashboard import part_stream, upload_destination  # noqa: F401
from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async
from kiro_crew.dashboard.chat_utils import (  # noqa: F401
    dashboard_slot_key,
    drained_to_thread,
    run_config_write,
)
from kiro_crew.dashboard.file_api import browse as _owner_browse
from kiro_crew.dashboard.file_api import dashboard_config as _owner_dashboard_config
from kiro_crew.dashboard.file_api import git_panel as _owner_git_panel
from kiro_crew.dashboard.file_api import grep as _owner_grep
from kiro_crew.dashboard.file_api import office_preview as _owner_office_preview
from kiro_crew.dashboard.file_api import path_complete as _owner_path_complete
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
from kiro_crew.dashboard.file_api.dashboard_config import (  # noqa: F401
    api_dashboard_config,
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
)
from kiro_crew.dashboard.file_api.office_preview import (  # noqa: F401
    _cap_slides,
    _PreviewUnsupported,
    _redact_block,
    _redact_blocks,
    _redact_value,
    api_file_office_preview,
)
from kiro_crew.dashboard.file_api.path_complete import (  # noqa: F401
    _complete_path_listing,
    _completion_segments,
    _open_completion_dir,
    _scan_completion_dir,
    api_path_complete,
)
from kiro_crew.dashboard.file_api.pinned_io import (  # noqa: F401
    _CheckedFile,
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
    _project_tree_scandir,
    _project_tree_scandir_entries,
    _project_tree_walk,
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
from kiro_crew.dashboard.handlers._shared import (
    _probe_persisted_session,
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.dashboard.handlers.messaging import _resolve_session_target
from kiro_crew.dashboard.origin import is_direct_local_request
from kiro_crew.dashboard.state import (  # noqa: F401
    VALID_MEMORY_MODES,
    DashboardState,
    append_and_surface,
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
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.outbound_files import OutboundFile
from kiro_crew.messaging.raster import SNIFF_BYTES, sniff_raster_mime
from kiro_crew.pdf_extract import PdfExtraction, extract_pdf_segments  # noqa: F401
from kiro_crew.platform import binary_content_is_flagged
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.platform import wide_content_is_flagged
from kiro_crew.platform.context import redact_log_via_context, redact_owner_view_via_context
from kiro_crew.sandbox import (  # noqa: F401
    cgroup_scope_argv,
    popen_limited,
    sandboxed_spawn_argv,
    wrap_argv,
)
from kiro_crew.security import (  # noqa: F401
    BINARY_MIME_ALLOWLIST,
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


def is_tracked_channel(channel_id: str) -> bool:
    """Load the Slack probe only when a file delivery needs it."""
    from kiro_crew.slack.handler import is_tracked_channel as probe

    return probe(channel_id)


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


async def api_reveal_path(request: web.Request) -> web.Response:
    """POST /api/reveal — reveal a file/folder in Finder or open with default app."""
    # Default cap: the body is a path and an action flag.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    path = body.get("path", "")
    action = body.get("action", "reveal")  # "reveal" or "open"
    if not path or ".." in Path(path).parts:
        return web.json_response({"error": "invalid path"}, status=400)
    if is_sensitive_path(path):
        _sel().log_tool_invocation(
            session_key="api", source="api", tool_name="reveal_path",
            outcome="denied", error="sensitive_path",
            resources=path, metadata={"action": action})
        return web.json_response({"error": "access denied"}, status=403)
    # Gate: only spawn native openers from direct-local requests. Remote/tunneled
    # callers get the copy-to-clipboard fallback — spawning Finder on a machine
    # the user is not looking at is surprising and useless.
    if not is_direct_local_request(request):
        _sel().log_tool_invocation(
            session_key="api", source="api", tool_name="reveal_path",
            outcome="denied", error="remote_request",
            resources=path, metadata={"action": action})
        # Degrade to a clipboard copy: `copy` is the path to write. The remote
        # cause is recorded in the SEL audit above (error="remote_request"); the
        # response body carries no path, host, or exception detail beyond `copy`.
        return web.json_response({"ok": True, "copy": path})
    # Every ALLOWED outcome leaves through the single audited return below —
    # including the clipboard answer, which is a granted decision whose host
    # simply had no file manager. An early return here would drop that decision
    # from the SEL log, so the branches record what happened instead of exiting.
    #
    # Both spawns live in platform_compat, which owns the safety properties:
    # absolute trusted launchers rather than bare argv names, a folder rather
    # than the file on the platforms where handing a file to the file manager
    # would launch it, and Windows refused outright for the launch-by-association
    # verb. They answer False both for a host with no launcher and for one that
    # refuses to start, and either way this degrades to the clipboard rather than
    # failing a click in the file viewer.
    if action == "open":

        def _stat_then_launch() -> tuple[bool, bool]:
            """The regular-file check AND the launch, in one worker transaction.

            Both belong off the loop: the stat is unbounded on a caller-supplied
            path, and the launch spawns a process. They must not be SPLIT across
            an ``await``, though. The launcher takes a path, not the descriptor
            this stat looked at, so the two calls are a check-then-use pair; an
            ``await`` between them is a scheduler yield inside that window, which
            is long enough for the path to be replaced with a symlink the
            sensitive-path gate above already refused. The launcher follows it and
            opens the substituted target in the user's default application.

            Keeping them in one transaction holds the window to what it is when
            the two run back-to-back: no suspension point, and the GIL not
            released between them. Closing it entirely needs a launcher that
            takes a descriptor, which no platform's open-by-association verb
            does, so this is the narrow form rather than the closed form.
            """
            if not os.path.isfile(path):
                return (False, False)
            return (True, platform_compat.open_with_default_app(path))

        try:
            is_regular_file, launched = await _run_path_probe(_stat_then_launch)
        except _PathProbeBusy:
            return _probe_busy_response(
                resource=path, tool_name="reveal_path", session_key="api", source="api"
            )
        if not is_regular_file:
            return web.json_response({"error": "not a regular file"}, status=400)
        copied = not launched
    else:
        # Off-loop: the reveal spawns a file-manager process. No stat pairs with
        # it, so there is no check-then-use window to hold here.
        copied = not await asyncio.to_thread(platform_compat.reveal_in_file_manager, path)
    _sel().log_tool_invocation(
        session_key="api", source="api", tool_name="reveal_path",
        outcome="success", resources=path, metadata={"action": action})
    # A local grant whose host had no working file manager degrades to the
    # clipboard; `copy` is the path to write.
    if copied:
        return web.json_response({"ok": True, "copy": path})
    return web.json_response({"ok": True})


def _read_outbox_file(raw_path: str, relative: bool = False) -> tuple[Path | None, bytes | None]:
    """Resolve, authorize, read and close on one bounded transfer worker.

    Only bytes and a path leave the worker, so cancelling its waiter never
    transfers descriptor cleanup to a cancelled coroutine.
    """
    from kiro_crew.hooks import safe_read_file_bytes  # noqa: F811

    outbox = config_loader.outbox_dir()
    path = ((outbox / raw_path) if relative else Path(raw_path)).resolve()
    if not path.is_relative_to(outbox.resolve()):
        return None, None
    return path, safe_read_file_bytes(str(path))


async def api_outbox_notify(request: web.Request) -> web.Response:
    """POST /api/outbox/notify — agent sent a file, notify the user."""
    state: DashboardState = request.app["state"]
    # Default cap: the body names an outbox file (path, filename, short
    # description, size) — the file bytes themselves never travel in it.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="notify",
            outcome="denied",
            error=_body_err_code(body_err),
        )
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success

    raw_path = body.get("path", "")
    raw_filename = body.get("filename", "")
    raw_desc = body.get("description", "")
    # Reject files whose names/paths contain sensitive patterns
    if redact(raw_filename) != raw_filename or redact(raw_path) != raw_path:

        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="notify",
            outcome="denied",
            error="sensitive_filename_rejected",
        )
        file_delivery_consent.audit_refusal(
            file_delivery_consent.CLASS_OWNER_DASHBOARD,
            leg="notify",
            name=redact(raw_filename),
            reason="flagged name or path",
        )
        return web.json_response(
            {"error": "filename or path contains sensitive content"}, status=400
        )
    file_data = {
        "filename": raw_filename,
        "path": raw_path,
        "description": redact(raw_desc),
        "size": body.get("size", 0),
        "content_type": mimetypes.guess_type(raw_filename)[0] or "application/octet-stream",
    }
    # Validate file is readable + UTF-8 before creating a persistent card.
    try:
        resolved, raw = await _run_path_probe(_read_outbox_file, raw_path, transfer=True)
        if resolved is None:
            _sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="file_send",
                tool_kind="notify",
                outcome="denied",
                error="path_outside_outbox",
            )
            return web.json_response({"error": "path must be inside outbox"}, status=403)
    except _PathProbeBusy:
        return _probe_busy_response(
            resource=raw_path, tool_name="file_send", session_key="api", source="api"
        )
    except FileTooLargeError as e:

        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="notify",
            outcome="denied",
            error=f"file_too_large: {e}",
        )
        return web.json_response({"error": str(e)}, status=413)
    if raw is None:

        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="notify",
            outcome="denied",
            error="file_not_found_or_access_denied",
        )
        return web.json_response({"error": "File not found or access denied"}, status=404)
    # Content is scanned whichever way it decodes: UTF-8 text below, non-UTF-8
    # bytes through the shared ``binary_content_is_flagged``. Binary must also
    # carry an allow-listed MIME type.
    try:
        text = raw.decode("utf-8")
        # The owner's grant covers this leg: the card renders in the owner's own
        # authenticated dashboard. No DELIVERY entry here -- that decision is
        # already recorded by the tool leg, and the byte handover is recorded by
        # the download route; a third entry for rendering a card would only bury
        # the two that answer a real question.
        #
        # The store read goes through a thread: ``is_granted`` ends in a
        # synchronous file read, and a coroutine that waits on storage stalls the
        # whole gateway. Ordered after the scan so a clean file never reads it.
        #
        # The wide pass runs here as well as on the binary branch below: a
        # credential written at UTF-16/UTF-32 spacing is NUL-interleaved ASCII,
        # which is valid UTF-8, so it decodes cleanly into this branch and the
        # contiguous-ASCII detectors in ``redact`` match none of it.
        flagged = redact(text) != text or await asyncio.to_thread(
            wide_content_is_flagged, raw
        )
        if flagged and not await asyncio.to_thread(
            file_delivery_consent.is_granted, file_delivery_consent.CLASS_OWNER_DASHBOARD
        ):
            _sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="file_send",
                tool_kind="notify",
                outcome="denied",
                error="sensitive_content_detected",
            )
            file_delivery_consent.audit_refusal(
                file_delivery_consent.CLASS_OWNER_DASHBOARD,
                leg="notify",
                name=raw_filename,
                reason="flagged content",
            )
            return web.json_response({"error": "file content contains sensitive data"}, status=400)
    except UnicodeDecodeError:
        # Binary file — only allow known-safe media types
        guessed_type = mimetypes.guess_type(raw_filename)[0] or ""
        if guessed_type not in BINARY_MIME_ALLOWLIST:
            _sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="file_send",
                tool_kind="notify",
                outcome="denied",
                error=f"binary_mime_not_allowed: {guessed_type}",
            )
            return web.json_response(
                {"error": f"Binary file type not allowed: {guessed_type or 'unknown'}"}, status=400
            )
        # An allow-listed media type is a container, not a guarantee about its
        # contents, so the same grant decides here as on the text branch above.
        # Off the event loop: the scan is CPU work over up to the read cap, and a
        # media file is routinely orders of magnitude larger than a text one.
        if await asyncio.to_thread(binary_content_is_flagged, raw) and not await asyncio.to_thread(
            file_delivery_consent.is_granted, file_delivery_consent.CLASS_OWNER_DASHBOARD
        ):
            _sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="file_send",
                tool_kind="notify",
                outcome="denied",
                error="binary_credential_detected",
            )
            file_delivery_consent.audit_refusal(
                file_delivery_consent.CLASS_OWNER_DASHBOARD,
                leg="notify",
                name=raw_filename,
                reason="flagged binary content",
            )
            return web.json_response(
                {
                    "error": "binary file contains embedded credentials",
                    "code": "binary_credential_detected",
                },
                status=400,
            )
    # Inject the file card into the caller's chat slot so it persists in the
    # correct session. This runs even when ``state._slots`` is empty: a headless
    # script cron typically has no dashboard tab open at all, and its origin slot
    # is rehydrated from history below — gating the whole block on
    # ``if state._slots`` skipped exactly that case.
    # Prefer the caller's own slot via X-Session-Key header
    session_key = request.headers.get("X-Session-Key", "").strip()
    active = None
    if session_key.startswith("cron:"):
        # A cron slot is named cron-<job-id>, which is not the session key folded.
        # Only the JOB ID: a cron turn's key can carry a further segment
        # (`cron:<job>:<run>` for a per-run session, `cron:<job>:<agent>` for a
        # multi-agent one), and folding the whole tail asks for a `cron-<job>:<run>`
        # slot that never exists — so every suffixed turn missed its own open tab
        # and fell through to origin resolution or suppression.
        job_id = session_key.removeprefix("cron:").split(":", 1)[0]
        active = state.get_slot(f"cron-{job_id}")
        if active is None:
            # A headless script cron has no live "cron-<id>" slot. Rather than
            # leak the card into whichever tab happens to be focused, route it to
            # the cron's ORIGIN dashboard session — the chat that created the cron
            # — through the same resolver ``send_message(session="origin")`` uses,
            # so both delivery paths agree on where a cron's output belongs.
            origin_slot_key, _origin_job = _resolve_session_target(state, "origin", session_key)
            if origin_slot_key:
                # get_slot is the hot path (O(1)); on a miss the origin session
                # exists on disk but has no tab open, so rehydrate it — with the
                # transcript read off the loop, the shape the sibling origin path
                # established, because a large store would otherwise stall the
                # gateway. A truly-gone session (never persisted, deleted, or
                # closed) returns None and falls through to suppression below; no
                # phantom empty tab is ever created.
                active = state.get_slot(origin_slot_key)
                if active is None:
                    active = await rehydrate_slot_from_history_async(state, origin_slot_key)
    elif session_key.startswith(_SUBAGENT_SESSION_PREFIX):
        # A sub-agent has no tab of its own, so route its card to the PARENT slot
        # — the same destination its completion injection and its ``subagent_*`` WS
        # frames already use. Only the dedicated-process arm arrives here: a
        # shared-runtime sub-agent's MCP stub carries the parent's own key and is
        # resolved by the branch below. An unknown run or a parent with no open tab
        # yields "" and falls through to suppression — never into an unrelated
        # conversation.
        parent_key = _subagent_parent_session_key(state, session_key)
        parent_slot_key = dashboard_slot_key(parent_key) if parent_key else ""
        if parent_slot_key:
            active = state.get_slot(parent_slot_key)
    else:
        # A channel-born conversation keeps its channel key (slack:<ts>)
        # while its tab is open, so the slot name comes from the surface
        # lookup — stripping a "dashboard:" prefix would miss it and drop the
        # card into whichever tab happened to be active last.
        slot_key = dashboard_slot_key(session_key)
        if slot_key:
            active = state.get_slot(slot_key)
    # An explicitly header-targeted slot receives the file even when empty
    header_targeted = active is not None
    # Fallback: most recently active slot — ONLY for a legacy headerless caller,
    # the best-effort case it was written for. A key that IS present but resolves
    # to nothing names a session we could not reach (a cron with no originating
    # chat, an unknown job, a sub-agent whose parent has no tab, a task-runner or
    # webhook session that owns no chat, a closed tab); suppress the card rather
    # than surface it in an unrelated conversation.
    if not active and not session_key and state._slots:
        active = max(
            state._slots.values(),
            key=lambda s: s.messages[-1]["ts"] if s.messages else "",
        )
    delivered = False
    if active is not None and (active.messages or header_targeted):
        delivered = True
        # Route through the context-aware redact() so a loaded companion's
        # extra credential regexes scrub the broadcast file JSON too — the
        # same overlay-aware pass the filename/path/description gates use.
        redacted_file_json = redact(json.dumps(file_data))
        # append_and_surface, not a hand-built broadcast_ws: hand-built
        # frames ship the row a second time and carry no ``meta.mid``, so
        # the client cannot recognise the redelivery and renders a
        # duplicate card.
        append_and_surface(state, active, "file", redacted_file_json)
    else:
        # Suppression is the RIGHT outcome — better nowhere than in an unrelated
        # conversation — but it is silent, and a caller that reads `ok: true` has
        # no way to tell a delivered card from a vanished one. So say so once, at
        # the only point that knows both that a key was supplied and that it
        # resolved to no destination. The key is logged because it is the whole
        # diagnosis (which namespace, which id); the file is already named in the
        # audit event below.
        logger.info(
            "outbox notify: no destination for session key %r; file card suppressed",
            session_key or "<none>",
        )

    _sel().log_tool_invocation(
        session_key="api",
        source="api",
        tool_name="file_send",
        tool_kind="notify",
        outcome="completed",
        # `delivered` distinguishes the two outcomes this endpoint folds into one
        # 200: the card reached a session, or it was suppressed for want of a
        # destination. Carried here rather than as a separate SEL outcome so the
        # existing "completed" consumers keep working.
        resources=f"filename={file_data['filename']} delivered={int(delivered)}",
    )
    return web.json_response({"ok": True})


async def api_outbox_download(request: web.Request) -> web.StreamResponse:
    """GET /api/outbox/{filename} — download a file from the outbox."""
    filename = request.match_info["filename"]
    try:
        path, raw = await _run_path_probe(_read_outbox_file, filename, True, transfer=True)
        if path is None:
            _sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="file_send",
                tool_kind="download",
                outcome="denied",
                error=f"path_traversal: {filename}",
            )
            return web.json_response({"error": "forbidden"}, status=403)
    except _PathProbeBusy:
        return _probe_busy_response(
            resource=filename, tool_name="file_send", session_key="api", source="api"
        )
    except FileTooLargeError as e:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="download",
            outcome="denied",
            error=f"file_too_large: {e}",
        )
        return web.json_response({"error": str(e)}, status=413)
    if raw is None:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="download",
            outcome="denied",
            error=f"safe_read_file_bytes rejected: {filename}",
        )
        return web.json_response({"error": "forbidden"}, status=403)
    # Content is scanned whichever way it decodes: UTF-8 text here, non-UTF-8
    # bytes once the MIME allow-list below has admitted the type.
    is_text = True
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        is_text = False

    def _grant_permits_this_handover(granted: bool) -> bool:
        """Whether the owner's recorded grant releases flagged bytes to THIS caller.

        This is where the flagged bytes actually leave for the owner's browser, so
        a grant is honoured here AND the handover is audited -- the refusal it
        replaces was self-evident in the 400, whereas a successful consented
        download would otherwise leave no trace.

        *granted* arrives already resolved because reading it ends in a
        synchronous store read, and this closure is called from a coroutine: each
        caller resolves the grant with ``asyncio.to_thread`` inside its own
        flagged-content branch, which keeps the read off the gateway event loop
        and keeps a clean file from touching the store at all. What stays here is
        the in-memory half of the test.

        TWO conjuncts, and the second is not redundant. This route is absent from
        every ``token_auth`` bypass list, which establishes that it needs
        AUTHENTICATION -- not that it needs OWNER IDENTITY. A Slack allow-listed
        non-owner running ``!dashboard`` authenticates with ``app == ""`` and
        ``sub != owner_id``, so ordinary token auth admits them while
        ``is_owner_dashboard_request`` does not. Without the owner conjunct the
        grant would convert a clean 400-for-everyone into raw bytes for every
        authenticated caller -- widening the audience as a side effect of a control
        meant to narrow it, and contradicting the "owner's own authenticated
        browser" audience this class is scoped to.

        One function for both content kinds on purpose: a text file and a media
        file carrying the same credential reach the same audience through this
        route, so a second copy of the test is a second thing to forget.
        """
        from kiro_crew.dashboard.handlers.source_providers import (  # lazy: import cycle
            is_owner_dashboard_request,
        )

        return granted and is_owner_dashboard_request(request)

    def _requester_is_not_the_owner() -> bool:
        """Whether THIS caller is somebody other than the dashboard owner.

        The refusal entry has to say which of the gate's two conjuncts stopped the
        handover, and the grant cannot answer that: the conjunction short-circuits,
        so in the default no-grant state a non-owner is refused before identity is
        ever examined. Keying the entry off the grant would therefore record a Slack
        allow-listed non-owner reaching for a flagged file as an ordinary scanner
        hold-back, byte-identical to the owner's own -- losing the attribution
        exactly in the configuration almost every install runs. Identity answers it
        in every configuration, and a non-owner is refused here whether or not a
        grant exists.

        Pure attribute reads, so unlike the grant this needs no thread: it consults
        the request's own claims and the in-memory owner id.
        """
        from kiro_crew.dashboard.handlers.source_providers import (  # lazy: import cycle
            is_owner_dashboard_request,
        )

        return not is_owner_dashboard_request(request)

    def _audit_consented_handover() -> None:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="download",
            outcome="completed",
            error="sensitive_content_delivered_with_consent",
        )
        file_delivery_consent.audit_decision(
            file_delivery_consent.CLASS_OWNER_DASHBOARD,
            outcome="delivered",
            detail=f"download: {path.name}",
        )

    if is_text:
        # Two passes on this branch, not one. ``redact`` reads the correctly
        # decoded text, which is the more accurate read of it; the wide pass reads
        # the raw bytes, because a credential at UTF-16/UTF-32 spacing is
        # NUL-interleaved ASCII, decodes as valid UTF-8 into this very branch, and
        # arrives with its characters separated so no contiguous-ASCII detector
        # matches. Short-circuited, so a file the text pass already flags pays no
        # second scan.
        redacted = redact(text)
        narrow_flagged = redacted != text
        if narrow_flagged or await asyncio.to_thread(wide_content_is_flagged, raw):
            granted = await asyncio.to_thread(
                file_delivery_consent.is_granted, file_delivery_consent.CLASS_OWNER_DASHBOARD
            )
            if not _grant_permits_this_handover(granted):
                _sel().log_tool_invocation(
                    session_key="api",
                    source="api",
                    tool_name="file_send",
                    tool_kind="download",
                    outcome="denied",
                    error="content_redacted" if narrow_flagged else "wide_credential_detected",
                )
                # The two conjuncts fail for different events, so the entry must
                # not read the same for both. Identity is the discriminator, not
                # the grant: the conjunction short-circuits, so a non-owner in the
                # default no-grant state never reaches the owner check, and an
                # entry keyed off the grant would call that a scanner hold-back and
                # name the other principal nowhere.
                cross_principal = _requester_is_not_the_owner()
                file_delivery_consent.audit_refusal(
                    file_delivery_consent.CLASS_OWNER_DASHBOARD,
                    leg="download",
                    name=path.name,
                    reason=(
                        "flagged content, non-owner caller"
                        if cross_principal
                        else "flagged content, no grant"
                    ),
                    caller=str(request.get("user") or "unknown") if cross_principal else "",
                )
                return web.json_response(
                    {"error": "file content was redacted; download aborted"}, status=400
                )
            _audit_consented_handover()
    safe_name = urllib.parse.quote(path.name, safe="")
    content_type, _ = mimetypes.guess_type(path.name)
    if not content_type:
        content_type = "application/octet-stream"
    # Binary files must be in the allowlist
    if not is_text and content_type not in BINARY_MIME_ALLOWLIST:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="download",
            outcome="denied",
            error=f"binary_mime_not_allowed: {content_type}",
        )
        return web.json_response(
            {"error": f"Binary file type not allowed: {content_type}"}, status=403
        )
    if not is_text:
        # An allow-listed media type says the browser can render these bytes
        # safely, not that a credential cannot be sitting inside them. Scanned
        # after the allow-list so a type this route refuses outright is never
        # scanned, and off the event loop because the scan is CPU work over up to
        # the read cap and a media file is routinely far larger than a text one.
        if await asyncio.to_thread(binary_content_is_flagged, raw):
            granted = await asyncio.to_thread(
                file_delivery_consent.is_granted, file_delivery_consent.CLASS_OWNER_DASHBOARD
            )
            if not _grant_permits_this_handover(granted):
                _sel().log_tool_invocation(
                    session_key="api",
                    source="api",
                    tool_name="file_send",
                    tool_kind="download",
                    outcome="denied",
                    error="binary_credential_detected",
                )
                # Same discriminator as the text branch above: identity, because the
                # conjunction short-circuits before the owner check in the default
                # no-grant state.
                cross_principal = _requester_is_not_the_owner()
                file_delivery_consent.audit_refusal(
                    file_delivery_consent.CLASS_OWNER_DASHBOARD,
                    leg="download",
                    name=path.name,
                    reason=(
                        "flagged binary content, non-owner caller"
                        if cross_principal
                        else "flagged binary content, no grant"
                    ),
                    caller=str(request.get("user") or "unknown") if cross_principal else "",
                )
                return web.json_response(
                    {
                        "error": "binary file contains embedded credentials; download aborted",
                        "code": "binary_credential_detected",
                    },
                    status=400,
                )
            _audit_consented_handover()
    # Inline disposition for media types the browser can render
    disposition = "inline" if any(content_type.startswith(t) for t in _INLINE_DISPOSITION_PREFIXES) else "attachment"
    # SVG can contain scripts — never serve inline on the dashboard origin
    if content_type == "image/svg+xml":
        disposition = "attachment"
    # Text files always attachment — prevents content injection via crafted filenames
    if is_text:
        disposition = "attachment"
    _sel().log_tool_invocation(
        session_key="api",
        source="api",
        tool_name="file_send",
        tool_kind="download",
        outcome="completed",
        resources=f"filename={filename}",
    )
    return web.Response(
        body=raw,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{safe_name}",
            "Content-Type": content_type,
            "X-Content-Type-Options": "nosniff",
        },
    )


async def api_outbox_list(request: web.Request) -> web.Response:
    """GET /api/outbox — list files in the outbox."""
    from kiro_crew.config.loader import outbox_dir  # noqa: F811

    entries = []
    odir = outbox_dir()
    if not odir.is_dir():
        return web.json_response({"files": []})
    for f in odir.iterdir():
        try:
            st = f.stat()
        except FileNotFoundError:
            continue
        if f.is_file() and redact(f.name) == f.name:
            entries.append({"filename": f.name, "size": st.st_size, "modified": st.st_mtime})
    entries.sort(key=lambda x: float(x["modified"]), reverse=True)  # type: ignore[arg-type,return-value]

    _sel().log_tool_invocation(
        session_key="api",
        source="api",
        tool_name="file_send",
        tool_kind="list",
        outcome="completed",
        resources=f"count={len(entries)}",
    )
    return web.json_response({"files": entries[:50]})


def _gate_upload_file(
    file_path: str, filename: str, *, tool_kind: str
) -> tuple[web.Response | None, Path | None, bytes | None]:
    """The shared admission gate for shipping a local file to a channel.

    One site computes the judgment for every channel-upload endpoint —
    containment (outbox or workspace root), the descriptor-safe read, the
    binary MIME allowlist, and the content credential scans — so the Slack
    and channel legs cannot drift apart gate by gate. Returns
    ``(error_response, None, None)`` on refusal, ``(None, resolved, bytes)``
    when the file may ship. *tool_kind* keys the SEL records so each caller
    keeps its own audit lane.

    Blocking by design (a full read of up to ``MAX_FILE_BYTES`` plus content
    regex scans): async handlers MUST run it off the event loop via
    ``asyncio.to_thread`` — SEL appends are internally locked, so the audit
    calls are thread-safe. The loader is called through its module so tests
    (and config reloads) resolve at call time, not import time.
    """

    def _audit_denial(error: str, *, outcome: str = "denied") -> None:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind=tool_kind,
            outcome=outcome,
            downstream_service=tool_kind,
            error=error,
        )

    if not file_path or not filename:
        _audit_denial("missing_required_fields")
        return (
            web.json_response(
                {"error": "file_path, filename required", "code": "missing_required_fields"},
                status=400,
            ),
            None,
            None,
        )
    # The name is DELIVERED (Slack upload title, Telegram document name,
    # Discord message text fallback), so a credential embedded in it leaves
    # with the file. Checked in the shared gate so no leg can drift from the
    # others, and before path resolution so a sensitive name never even
    # selects a file. Mirrors the MCP-side file_send refusal.
    if redact(filename) != filename:
        _audit_denial(f"sensitive_filename_rejected: {redact(filename)}")
        return (
            web.json_response(
                {
                    "error": "filename contains sensitive content",
                    "code": "sensitive_filename",
                },
                status=400,
            ),
            None,
            None,
        )
    resolved = Path(file_path).resolve()
    allowed_outbox = config_loader.outbox_dir().resolve()
    allowed_workspace = config_loader.workspace_root().resolve()
    if not (resolved.is_relative_to(allowed_outbox) or resolved.is_relative_to(allowed_workspace)):
        _audit_denial(f"path_not_allowed: {file_path}")
        return (
            web.json_response(
                {
                    "error": "file_path must be under the outbox directory or the workspace root",
                    "code": "path_not_allowed",
                },
                status=403,
            ),
            None,
            None,
        )
    try:
        raw = safe_read_file_bytes(str(resolved))
    except FileTooLargeError as e:
        _audit_denial(f"file_too_large: {e}")
        return (
            web.json_response({"error": str(e), "code": "file_too_large"}, status=413),
            None,
            None,
        )
    if raw is None:
        _audit_denial(f"safe_read_file_bytes rejected: {file_path}")
        return (
            web.json_response(
                {
                    "error": f"File not found or access denied: {file_path}",
                    "code": "file_not_found",
                },
                status=404,
            ),
            None,
            None,
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Binary file — only allow known-safe media types
        guessed_type = mimetypes.guess_type(filename)[0] or ""
        if guessed_type not in BINARY_MIME_ALLOWLIST:
            _audit_denial(f"binary_mime_not_allowed: {guessed_type}")
            return (
                web.json_response(
                    {
                        "error": f"Binary file type not allowed: {guessed_type or 'unknown'}",
                        "code": "binary_mime_not_allowed",
                    },
                    status=400,
                ),
                None,
                None,
            )
        text = None  # signal: skip text redaction path
        # An allow-listed media type is a container, not a guarantee about its
        # contents: base64 key material inside a PDF is the case this catches.
        # Unconditional on this leg. The owner-facing gates weigh a recorded
        # owner decision against a positive result; this leg has a third-party
        # audience and so has nothing to weigh, which is why it reads no store at
        # all -- a property asserted on this function's own source.
        if binary_content_is_flagged(raw):
            _audit_denial(f"binary_credential_detected: {filename}")
            return (
                web.json_response(
                    {
                        "error": "binary file contains embedded credentials",
                        "code": "binary_credential_detected",
                    },
                    status=400,
                ),
                None,
                None,
            )
    if text is not None:
        try:
            redacted = redact(text)
            if redacted != text:
                _audit_denial(f"content_redacted: {filename}")
                return (
                    web.json_response(
                        {
                            "error": "file content was redacted; upload aborted",
                            "code": "content_redacted",
                        },
                        status=400,
                    ),
                    None,
                    None,
                )
            # Wide-encoded credentials reach this branch rather than the binary one
            # above: NUL-interleaved ASCII is valid UTF-8, so the decode succeeds
            # and ``redact`` sees characters separated by NUL, which matches no
            # detector. Unconditional here for the same reason the binary scan is:
            # this leg has a third-party audience and no owner grant to weigh.
            if wide_content_is_flagged(raw):
                _audit_denial(f"wide_credential_detected: {filename}")
                return (
                    web.json_response(
                        {
                            "error": "file contains embedded credentials",
                            "code": "wide_credential_detected",
                        },
                        status=400,
                    ),
                    None,
                    None,
                )
        except Exception as redact_err:
            _audit_denial(f"redaction_failed: {redact_err}", outcome="error")
            return (
                web.json_response(
                    {"error": f"Redaction failed: {redact_err}", "code": "redaction_failed"},
                    status=500,
                ),
                None,
                None,
            )
    return None, resolved, raw


async def api_slack_upload_file(request: web.Request) -> web.Response:
    """POST /api/slack/upload-file — upload a file to Slack (internal, called by file_send).

    Destination and authorization come from the shared oracle
    (:func:`kiro_crew.dashboard.upload_destination.resolve_slack`), which holds
    this leg's ladder — the ``channels``-scope governance vet, the
    restricted-session ceiling, then a request-named channel, a
    session-map-linked thread, or the owner-DM fallback with its tracked-channel
    authorization — next to the non-Slack leg's, so the two cannot drift apart
    rung by rung. What stays here is what only this leg can
    answer: the Slack client, its upload verb, and the response shapes.

    The client-presence check stays AHEAD of the body parse: a gateway with no
    Slack client answers ``skipped: no_slack`` even for a malformed body.
    """
    state: DashboardState = request.app["state"]
    slack = state.slack_client
    if not slack:
        _audit_file_send(leg="slack", outcome="skipped", error="no_slack_client")
        return web.json_response({"ok": True, "skipped": "no_slack"})
    # Default cap: the body carries a file path, a filename, and Slack routing
    # ids — the file bytes are read from disk, never from this body.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        _audit_file_send(leg="slack", outcome="denied", error=_body_err_code(body_err))
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    file_path_raw = body.get("file_path", "")
    filename = body.get("filename", "")
    # Off-loop: the gate reads up to MAX_FILE_BYTES and regex-scans the content
    # (no-blocking-call-on-event-loop).
    error_resp, resolved, raw = await asyncio.to_thread(
        _gate_upload_file, file_path_raw, filename, tool_kind="slack"
    )
    if error_resp is not None:
        return error_resp
    assert resolved is not None and raw is not None  # narrowed by the gate
    # ``is_tracked_channel`` and the persisted-transcript probe are handed to the
    # oracle rather than imported there: one binding site, and the module stays
    # free of both the Slack handler's config dependency and the ``dashboard``
    # package ``messaging.upload_gate`` may not import.
    destination = await upload_destination.resolve_slack(
        state,
        slack,
        session_key=request.headers.get("X-Session-Key", "").strip(),
        requested_channel=body.get("channel", ""),
        thread_ts=body.get("thread_ts"),
        tracked_probe=is_tracked_channel,
        persisted_probe=_probe_persisted_session,
    )
    if isinstance(destination, upload_destination.Refusal):
        _audit_file_send(
            leg="slack",
            outcome="denied",
            error=destination.audit_error,
            downstream=destination.downstream,
        )
        # One branch per literal status, body inline. `status=<expression>` and a
        # body hoisted into a variable are both invisible to the error-code
        # contract scanner, which counts either as its own bucket
        # (test_error_code_contract) -- so the refusal says WHICH answer it is
        # and each answer is spelled out here.
        if destination.status == 400:
            return web.json_response(
                {"error": destination.error, "code": destination.code}, status=400
            )
        return web.json_response(
            {"error": destination.error, "code": destination.code}, status=403
        )
    if isinstance(destination, upload_destination.Skip):
        _audit_file_send(leg="slack", outcome="skipped", error=destination.reason)
        return web.json_response({"ok": True, "skipped": destination.reason})
    try:
        # The filename was already cleared by the shared admission gate above —
        # same predicate, same value, strictly earlier in this function — so the
        # leg does not re-check it. That gate is the one site for the rule; a
        # second copy here could only drift from it.
        await slack.upload_file(
            destination.channel,
            destination.thread_ts,
            str(resolved),
            filename,
            filename,
        )
        _audit_file_send(
            leg="slack",
            outcome="completed",
            downstream="slack",
            resources=f"channel={destination.channel} file={file_path_raw}",
        )
        return web.json_response({"ok": True})
    except Exception as e:
        # A Slack SDK / network exception can carry file paths, host and URL
        # fragments, or credentials embedded in a URL. Sanitize before it
        # reaches the client or the audit record (see api_slack_pins).
        safe_error, _ = redact_credentials(str(e))
        safe_error, _ = redact_exfiltration_urls(safe_error)
        _audit_file_send(leg="slack", outcome="error", downstream="slack", error=safe_error)
        return web.json_response({"error": safe_error}, status=500)


async def api_channel_upload_file(request: web.Request) -> web.Response:
    """POST /api/channel/upload-file — deliver a file to the caller's own
    conversation on a non-Slack channel (internal, called by file_send).

    Destination and authorization come from the shared oracle
    (:func:`kiro_crew.dashboard.upload_destination.resolve_channel`), which for
    this leg is the SAME send ladder the cross-surface reply mirror uses
    (``_resolve_mirror_target``): channel-scope governance, transport
    registration, proactive-send capability, and ``may_send_to`` recipient
    re-authorization, all fail-closed and SEL-audited in one place — plus the
    restricted-session ceiling the renderers' extraction path enforces, on the
    same shared predicate. The destination comes exclusively from the caller's
    session map entry — a request cannot name an arbitrary conversation, which is
    what keeps this endpoint from being a broadcast primitive. The oracle also
    resolves the delivery verb, since which channels have one is part of "can
    this file land here": Telegram and Discord today, each via its own
    purpose-built name-preserving ``send_document``; every other channel is a
    skip until its transport grows that verb. The Slack counterpart above
    resolves through the same module, one rung table away.

    "Cannot deliver here" is a SKIP (``delivered: false``), not an error: most
    sessions mirror nowhere, and the caller falls back to the dashboard card
    and the Slack leg.
    """
    state: DashboardState = request.app["state"]
    # Default cap: same shape as the Slack leg — a path, a filename, and a
    # short description; the file bytes are read from disk by the gate.
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        _audit_file_send(leg="channel", outcome="denied", error=_body_err_code(body_err))
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success

    def _skip(reason: str) -> web.Response:
        _audit_file_send(leg="channel", outcome="skipped", error=reason)
        return web.json_response({"ok": True, "delivered": False, "skipped": reason})

    destination = await upload_destination.resolve_channel(
        state,
        request.headers.get("X-Session-Key", "").strip(),
        persisted_probe=_probe_persisted_session,
    )
    if isinstance(destination, upload_destination.Skip):
        return _skip(destination.reason)
    link, deliver = destination.link, destination.deliver
    # Off-loop: the gate reads up to MAX_FILE_BYTES and regex-scans the content
    # (no-blocking-call-on-event-loop).
    error_resp, resolved, raw = await asyncio.to_thread(
        _gate_upload_file,
        body.get("file_path", ""),
        body.get("filename", ""),
        tool_kind="channel",
    )
    if error_resp is not None:
        return error_resp
    assert resolved is not None and raw is not None  # narrowed by the gate
    filename = body.get("filename", "")
    # Display-form redaction, not just literal: redact() scans bytes, and the
    # channel's renderer strips markup at display time — ``AKIA**…**`` passes
    # a literal scan and displays as an intact key. Same boundary rule every
    # renderer sink applies (``redact_for_display``) before text reaches a
    # transport.
    description, _ = redact_for_display(body.get("description", "") or "", redact)
    outbound = OutboundFile(
        path=str(resolved),
        data=raw,
        alt=description,
        mime=mimetypes.guess_type(filename)[0] or "application/octet-stream",
    )
    try:
        mid = await deliver(
            link.channel_id,
            outbound,
            caption=description,
            thread_id=link.thread_id,
        )
    except Exception as e:
        # A transport / network exception can carry file paths, host and URL
        # fragments, or credentials embedded in a URL. Sanitize before it
        # reaches the client or the audit record (see api_slack_upload_file).
        safe_error, _ = redact_credentials(str(e))
        safe_error, _ = redact_exfiltration_urls(safe_error)
        _audit_file_send(
            leg="channel",
            outcome="error",
            downstream=link.channel_type,
            error=safe_error,
        )
        return web.json_response({"error": safe_error}, status=502)
    if not mid:
        # The transport reported failure without raising (the clients return
        # an empty id on an API-level refusal).
        _audit_file_send(
            leg="channel",
            outcome="error",
            downstream=link.channel_type,
            error="delivery_reported_no_message_id",
        )
        return web.json_response({"error": "channel delivery failed"}, status=502)
    _audit_file_send(
        leg="channel",
        outcome="completed",
        downstream=link.channel_type,
        resources=f"channel_type={link.channel_type} file={body.get('file_path', '')}",
    )
    return web.json_response(
        {"ok": True, "delivered": True, "channel_type": link.channel_type}
    )


async def api_upload(request: web.Request) -> web.Response:
    """POST /api/upload — open native file picker and return selected paths.

    The dialog binary is resolved from the fixed system directories rather than
    PATH. A gateway's PATH can lead with an agent-writable directory (a worktree
    venv's ``bin``, ``~/.local/bin``), so a bare argv name lets a planted shim
    run with the gateway's environment and outside the sandbox. ``None`` is a
    refusal, never a fallback to the bare name — that would reinstate the hazard.
    """
    if sys.platform != "darwin":
        return web.json_response({"error": "File picker is only available on macOS"}, status=400)

    osascript = platform_compat.trusted_system_bin("osascript")
    if osascript is None:
        return web.json_response(
            {
                "error": "File picker is unavailable on this system",
                "code": "file_picker_unavailable",
            },
            status=501,
        )

    proc = await asyncio.create_subprocess_exec(
        osascript,
        "-e",
        "set f to choose file with multiple selections allowed\n"
        'set out to ""\n'
        "repeat with p in f\n"
        "  set out to out & POSIX path of p & linefeed\n"
        "end repeat\n"
        "return out",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()
        return web.json_response({"error": "Finder dialog timed out"}, status=504)
    paths = [ln for ln in stdout.decode("utf-8", errors="replace").strip().splitlines() if ln]

    if not paths:
        return web.json_response({"paths": []})
    return web.json_response({"paths": paths})


# Resolved per call, never captured at import: an import-time binding freezes
# the data home and defeats pod isolation, the lazy legacy-home migration and
# test isolation. The name below is an opt-in override (None = live home) so
# existing monkeypatch call sites keep working. See config.md "Data Home";
# dashboard/handlers/usage.py is the reference implementation.
_SCREENSHOT_DIR: Path | None = None

_UPLOAD_DIR: Path | None = None


def _screenshot_dir() -> Path:
    """Screenshots directory, resolved against the live data home."""
    return _SCREENSHOT_DIR if _SCREENSHOT_DIR is not None else data_home() / "screenshots"


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


async def api_screenshot(request: web.Request) -> web.Response:
    """POST /api/screenshot — capture screen region and return file path.

    macOS only — uses built-in screencapture. Linux cloud desktops
    (AL2, headless) don't have a display server so this is unavailable.

    The capture binary is resolved from the fixed system directories rather than
    PATH, for the reason :func:`api_upload` states, and an unresolvable one is a
    refusal rather than a bare-name spawn.
    """
    if sys.platform != "darwin":
        return web.json_response({"error": "Screenshot is only available on macOS"}, status=400)

    screencapture = platform_compat.trusted_system_bin("screencapture")
    if screencapture is None:
        return web.json_response(
            {
                "error": "Screenshot is unavailable on this system",
                "code": "screenshot_unavailable",
            },
            status=501,
        )

    screenshot_dir = _screenshot_dir()
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    dest = screenshot_dir / f"screenshot_{ts}.png"

    proc = await asyncio.create_subprocess_exec(
        screencapture,
        "-i",
        str(dest),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=120)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return web.json_response({"error": "screenshot timed out"}, status=504)
    if not dest.exists():
        return web.json_response({"path": ""})  # user cancelled
    return web.json_response({"path": str(dest)})


#: Every control character: C0, DEL, and the C1 block.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _validate_dashboard_path(raw: str) -> str | None:
    """Validate a file path through hooks.py enforcement layer.

    Refuses a control character in the RAW path first. ``FILE_READ_SCHEMA``
    declares that class but cannot enforce it: ``validate_tool_args`` matches the
    *sanitized* copy of the value, from which ``strip_hidden_unicode`` has already
    removed every control character but CR, LF and TAB, while the raw string is
    what travels on. So the class is unobservable at the schema and has to be
    refused here.

    It is refused HERE rather than inside ``validate_file_path`` because that is a
    shared chokepoint whose other callers deliberately handle such a name -- a
    diagnostic that enumerates an agent-writeable directory reports on a
    control-character-named file and escapes the name for display, and refusing it
    there would suppress that report. The class is a property of what this
    boundary accepts from a caller, not of what a path can be.

    What it buys at this boundary: the raw path is recorded in the request's audit
    entry and echoed in diagnostics, so CR or LF forges a line and ESC or an 8-bit
    C1 (U+009B CSI, U+0085 NEL) is a terminal escape. No file a dashboard caller
    means to open is named with one, so the refusal costs nothing legitimate --
    unlike the punctuation an allowlist omits, which is the defect this gate's
    denylist exists to stop causing.

    Blocking: ``validate_file_path`` canonicalizes with ``realpath`` and, on
    Windows, walks the path's ancestors with one ``lstat`` each. Callers reach it
    through :func:`_probe_request_path` on a worker thread rather than calling it
    from an ``async def`` body.
    """
    from kiro_crew.hooks import validate_file_path  # noqa: F811

    if _CONTROL_CHARS_RE.search(raw):
        return None
    return validate_file_path(raw)


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


class _PathProbeBusy(Exception):
    """No worker on the chosen pool freed up within the admission window."""


async def _run_path_probe(
    fn: Callable[..., _ProbeT], /, *args: object, transfer: bool = False
) -> _ProbeT:
    """Run a blocking request-path call on a dedicated bounded pool, or refuse.

    The only sanctioned route for filesystem work on a caller-supplied path in
    this module -- never ``asyncio.to_thread``. That is the loop's default
    executor, shared by MCP, crons and the rest of the dashboard: a thread wedged
    in an uninterruptible ``stat`` on a dead mount never returns, so a caller
    repeatedly naming one would retire a shared worker per request until every
    unrelated ``to_thread`` user queued behind them. On its own pool the same
    caller exhausts that pool and nothing else.

    ``transfer`` picks the pool. Probes -- validation and stats, milliseconds
    when healthy -- go to :func:`executors.path_probe_executor`; calls that hold
    a worker for the length of a bounded transfer (the open-and-check envelope's
    full read, the search walk, the browse listings, the document parses) go to
    :func:`executors.path_transfer_executor`, so a burst of large downloads
    cannot starve validation behind them.

    Admission and the refusal are :func:`executors.run_in_cron_pool`'s: it
    submits, waits at most ``_PATH_PROBE_ADMIT_TIMEOUT_SECS`` for a worker to
    CLAIM the call, and if none does it cancels the still-queued call and raises
    ``CronQueueTimeout`` -- which becomes :class:`_PathProbeBusy` here and a 503
    at every endpoint via :func:`_probe_busy_response`. A call a worker claims at
    the deadline is not refused: it is running, and a thread cannot be taken
    back. Capacity accounting is the pool's own worker count, so a client that
    gives up on a wedged call cannot make the pool believe a slot is free while
    the thread is still parked; nothing here has to track that.
    """
    pool = (
        executors.path_transfer_executor() if transfer else executors.path_probe_executor()
    )
    try:
        return await executors.run_in_cron_pool(
            fn,
            *args,
            timeout=_PATH_PROBE_EXEC_CEILING_SECS,
            queue_timeout=_PATH_PROBE_ADMIT_TIMEOUT_SECS,
            executor=pool,
        )
    except executors.CronQueueTimeout:
        raise _PathProbeBusy() from None


def _probe_busy_response(
    *,
    resource: str,
    tool_name: str = "",
    operation: str = "",
    caller: str = "dashboard",
    session_key: str = "dashboard",
    source: str = "",
) -> web.Response:
    """The one answer for a refused probe: 503, coded, audited.

    Pass ``tool_name`` for endpoints that audit through ``log_tool_invocation``
    and ``operation`` for those that use ``log_api_access``, matching whichever
    the endpoint's other outcomes already use. One ``json_response`` site, so the
    error-code contract counts every adopter as one.
    """
    if tool_name:
        _sel().log_tool_invocation(
            session_key=session_key, source=source, tool_name=tool_name,
            outcome="failure", error="path_probe_busy", resources=resource,
        )
    else:
        _sel().log_api_access(
            caller=caller, operation=operation, outcome="failure",
            resources=resource, error="path_probe_busy",
        )
    return web.json_response(
        {"error": "file system probe capacity exhausted; retry shortly", "code": "path_probe_busy"},
        status=503,
    )


class _PathProbe(NamedTuple):
    """What one off-loop filesystem probe of a request path found.

    ``path`` is the validated canonical path, or ``""`` when validation refused
    it -- which the endpoint answers as "invalid or forbidden path", exactly as
    a ``None`` from :func:`_validate_dashboard_path` did. ``is_file`` and
    ``is_dir`` are the stat answers for that path, both ``False`` on a refusal so
    a caller that only reads them still takes its not-found branch.
    """

    path: str
    is_file: bool
    is_dir: bool


def _probe_request_path(raw: str) -> _PathProbe:
    """Validate and stat a request path -- ONE blocking hop, off the loop.

    Groups the filesystem syscalls an endpoint needs before it can answer:
    ``validate_file_path``'s ``realpath`` plus linked-ancestor walk, and the
    ``isfile`` / ``isdir`` probe. One helper means one pool hop per request, and
    it means these cannot be reintroduced on the event loop a call at a time.

    Blocking by design, and unboundedly so: a path whose mount is unresponsive
    (a disconnected network share, a wedged FUSE filesystem) makes ``realpath``
    and ``stat`` block for however long the kernel takes, and those syscalls are
    uninterruptible. Run on the event loop, ONE such request stalls every
    endpoint in the process -- dashboard, tunnel, MCP and crons alike -- and a
    stall outlasting ``dashboard.loop_stall_exit_after_secs`` makes the loop
    watchdog kill the gateway. Which mount the path lands on is the caller's
    choice, not this process's.

    A ``ValueError`` from a malformed path (an embedded NUL makes ``realpath``
    raise) is deliberately NOT caught: it propagates exactly as it did when this
    ran inline, so no caller's answer for that input changes here.

    This probe is a verdict, not a handle. A caller that goes on to OPEN the path
    must not re-derive the descriptor from this answer in a second hop -- see
    :func:`_read_request_path` for why.

    Always reached through :func:`_run_path_probe`, never ``asyncio.to_thread``:
    a thread wedged in an uninterruptible ``stat`` never returns, so a caller
    repeatedly naming one dead mount would otherwise retire a default-executor
    worker per request until the rest of the gateway starves behind them. The
    dedicated pool caps the wedged threads and then refuses with 503, and no other
    ``to_thread`` user ever queues behind a probe.
    """
    path = _validate_dashboard_path(raw)
    if not path:
        return _PathProbe("", False, False)
    return _PathProbe(path, os.path.isfile(path), os.path.isdir(path))


#: How much of a file /api/file-read returns, in CHARACTERS -- the unit matters,
#: because the decode is a text wrapper over a byte descriptor and a byte count
#: here would mis-set ``X-Truncated`` on multi-byte content.
_FILE_READ_CAP = 512_000


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


async def api_file_read(request: web.Request) -> web.Response:
    """GET /api/file-read?path=... — read file content for the markdown panel."""
    owner_denied = await require_owner_dashboard_request(request, "file_read")
    if owner_denied is not None:
        return owner_denied
    from kiro_crew.validation import (  # noqa: F811
        FILE_READ_SCHEMA,
        ValidationError,
        validate_tool_args,
    )

    raw_path = request.query.get("path", "")
    # Resolve relative paths against project dir when resolve=1. Off-loop: the
    # resolution is a pair of realpath calls, and the schema check below needs
    # the resolved string, so it cannot be folded into the probe.
    if request.query.get("resolve") == "1":
        try:
            raw_path, _resolve_err = await _run_path_probe(_resolve_project_relative, raw_path)
        except _PathProbeBusy:
            return _probe_busy_response(resource=raw_path, tool_name="file_read")
        if _resolve_err == "cannot_resolve":
            return web.json_response(
                {"error": "cannot resolve: no project dir configured"},
                status=400,
            )
        if _resolve_err == "outside_project":
            return web.json_response(
                {"error": "path outside project directory"},
                status=400,
            )

    try:
        validate_tool_args({"path": raw_path}, FILE_READ_SCHEMA)
    except ValidationError:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="file_read",
            outcome="denied",
            resources=raw_path,
        )
        return web.json_response({"error": "invalid input"}, status=400)

    read_cap = _FILE_READ_CAP
    # ONE off-loop transaction: validation, the stats, the no-follow open and the
    # capped read. Off-loop because each of those blocks for as long as the mount
    # takes; ONE because splitting the open from the validation is a symlink
    # TOCTOU (see _read_request_path). HEAD passes cap 0 -- it answers from the
    # stat and opens nothing.
    try:
        outcome = await _run_path_probe(
            _read_request_path,
            raw_path,
            0 if request.method == "HEAD" else read_cap + 1,
            transfer=request.method != "HEAD",
        )
    except _PathProbeBusy:
        return _probe_busy_response(resource=raw_path, tool_name="file_read")
    path = outcome.path
    if outcome.kind == "invalid":
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="file_read",
            outcome="denied",
            resources=raw_path,
        )
        return web.json_response({"error": "invalid or forbidden path"}, status=400)
    if outcome.kind in ("dir", "missing"):
        # Both a directory and a missing path are 404 for a READ — there is no
        # file content to return either way — but the caller needs to tell them
        # apart. The dashboard renders a markdown path chip as a folder
        # affordance when the path is a directory and suppresses the chip
        # entirely when the path is not on disk; without this header both look
        # like "file not found", which is actively wrong for a directory.
        #
        # Reached for GET and HEAD alike: the transaction above stats before it
        # opens, so both methods answer from the same verdict. `path` is already
        # realpath-canonical and denylist-checked, so naming the kind discloses
        # nothing the status code did not already.
        is_dir = outcome.kind == "dir"
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_read", outcome="not_found", resources=path
        )
        return web.json_response(
            {"error": "is a directory" if is_dir else "not found"},
            status=404,
            headers={"X-Path-Kind": "dir" if is_dir else "missing"},
        )
    if request.method == "HEAD":
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_read", outcome="success", resources=path
        )
        return web.Response(status=200, headers={"X-Path-Kind": "file"})
    try:
        if outcome.kind == "read_failed":
            raise OSError(f"file_read could not read {path}")
        if outcome.kind == "binary":
            _sel().log_tool_invocation(
                session_key="dashboard", tool_name="file_read", outcome="success", resources=path
            )
            # Empty content rather than decoded garbage, and the verdict as a
            # HEADER as well as a body field: a .json TEXT file is served as
            # ``application/json`` too, so the content type cannot tell this
            # envelope apart from a file whose own body is JSON. The header and
            # the empty body are the whole contract -- the panel's card names
            # the file by its path and offers the download, nothing more.
            return web.json_response(
                {"binary": True, "content": ""},
                headers={"X-File-Binary": "true"},
            )
        content = outcome.content
        truncated = len(content) > read_cap
        content = content[:read_cap]
        # OWNER-VIEW seam: when the requester IS the dashboard owner, the owner's
        # credential-redaction switch applies to this read of their own disk
        # (``security.redaction_switch``). A non-owner dashboard user (a Slack
        # allow-listed ``!dashboard`` caller) gets the unconditional pass. The only
        # other opener in this module is ``api_file_diff``, which feeds the SAME
        # panel the ``original`` this buffer is compared against; the outbox
        # flagged-file check and the upload gates keep the unconditional ``redact``.
        as_written = content
        if await _owner_view_bypasses_credential_pass(request):
            content = redact_owner_view_via_context(content)
        else:
            content = redact(content)
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_read", outcome="success", resources=path
        )
        # All three headers say the same thing to the viewer: this body is not
        # the file as written. The panel keeps the last copy of a file deleted
        # outside the dashboard and offers to download it; a capped, redacted
        # or lossily decoded body must not be offered under the file's own
        # name as if it were whole. The verdict rides in headers because
        # nothing in the body can carry it: a file may quote the redaction tag
        # verbatim, or contain the replacement character itself.
        headers = {}
        if truncated:
            headers["X-Truncated"] = "true"
        if content != as_written:
            headers["X-Redacted"] = "true"
        if outcome.lossy:
            headers["X-Lossy-Decode"] = "true"
        # Pick a sensible content_type per file extension so browsers and
        # debuggers (DevTools "Response" preview, curl) interpret the body
        # correctly. JSON files in particular benefit from application/json
        # so DevTools renders the body as a tree instead of raw text.
        # aiohttp appends "; charset=utf-8" automatically when text= is set.
        #
        # Security: HTML files are deliberately served as text/plain to
        # prevent stored-XSS via <script> tags or on* attribute handlers in
        # user/LLM-generated content. The dashboard's HtmlViewer renders
        # HTML files via a sandboxed srcDoc iframe, so the file-read
        # endpoint never needs to deliver executable HTML.
        ext = os.path.splitext(path)[1].lower()
        if ext == ".json":
            ct = "application/json"
        elif ext == ".jsonl":
            # JSONL (newline-delimited JSON) is NOT a valid JSON document —
            # the registered MIME type is application/x-ndjson. Serving it
            # as application/json would make DevTools / JsonViewer try to
            # parse the whole body as one JSON value and fail.
            ct = "application/x-ndjson"
        elif ext == ".csv":
            ct = "text/csv"
        elif ext in (".md", ".markdown"):
            ct = "text/markdown"
        else:
            ct = "text/plain"
        return web.Response(text=content, content_type=ct, headers=headers)
    except Exception:
        logging.getLogger(__name__).exception("file_read failed for %s", path)
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_read", outcome="failure", resources=path
        )
        return web.json_response({"error": "failed to read file"}, status=500)


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


async def api_file_raw(request: web.Request) -> web.Response:
    """GET /api/file-raw?path=... — serve a file with its native content type (images, etc.)."""
    # A named App Kit app keeps its manifest-scoped path; the owner gate
    # binds the dashboard-user class, whose reach is the whole host.
    if not request.get("app"):
        owner_denied = await require_owner_dashboard_request(request, "file_raw")
        if owner_denied is not None:
            return owner_denied
    # Envelope (validate -> sensitive -> nofollow-open -> bounded read) is
    # shared with api_file_download so a hardening change lands on both.
    # Offloaded to a worker thread: the envelope is synchronous file I/O and
    # must not block the event loop (same shape as api_file_stream's _open_media).
    try:
        opened = await _run_path_probe(
            functools.partial(
                _open_checked,
                request.query.get("path", ""),
                tool_name="file_raw",
                max_bytes=_MAX_UPLOAD_BYTES,
            ),
            transfer=True,
        )
    except _PathProbeBusy:
        return _probe_busy_response(resource=request.query.get("path", ""), tool_name="file_raw")
    if isinstance(opened, _OpenRefusal):
        return opened.response
    path, data = opened.path, opened.data
    # SNIFF_BYTES: the shared raster sniffer's documented minimum, and enough
    # for every magic matched below (WebP's form tag ends at byte 12).
    header = data[:SNIFF_BYTES]

    def _log(outcome: str, res: str) -> None:
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_raw", outcome=outcome, resources=res,
        )

    # Raster types are detected by the shared sniffer
    # (kiro_crew.messaging.raster), which requires the full PNG signature and
    # WebP's form tag at offset 8 — so a RIFF/WAVE audio file is not served as
    # an image. TIFF and ICO keep local rows (_READ_PATH_EXTRA_MAGIC).
    content_type = sniff_raster_mime(header)
    if content_type is None:
        for magic, mime in _READ_PATH_EXTRA_MAGIC:
            if header.startswith(magic):
                content_type = mime
                break
    # SVG: XML-based, no magic bytes
    if not content_type:
        stripped = data.lstrip(b"\xef\xbb\xbf").lstrip()
        if stripped.startswith(b"<svg") or (
            stripped.startswith(b"<?xml") and b"<svg" in data[:4096]
        ):
            content_type = "image/svg+xml"
    # PDF: %PDF magic bytes
    if not content_type:
        if header.startswith(b"%PDF"):
            content_type = "application/pdf"
    if not content_type:
        _log("denied", path)
        return web.json_response({"error": "file content is not a recognized format"}, status=403)
    _log("success", path)
    # inline (not attachment) keeps the PDF/image rendering in the viewer's
    # <iframe>/<img>, while naming the file so the browser's native Download /
    # Save-as saves under the real name instead of "file-raw" -- the last
    # segment of this endpoint's URL. Sibling parity with api_file_stream.
    safe_name = urllib.parse.quote(os.path.basename(path), safe="")
    headers = {
        "Content-Type": content_type,
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"inline; filename*=UTF-8''{safe_name}",
    }
    if content_type == "image/svg+xml":
        headers["Content-Security-Policy"] = "script-src 'none'; style-src 'unsafe-inline'"
    return web.Response(body=data, headers=headers)


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


def _resolve_diff_path(raw: str) -> tuple[str, bool]:
    """Canonicalize a diff target; say whether it is a regular file.

    Blocking (``realpath`` then ``isfile``) -- callers run it on a worker thread.
    """
    path = os.path.realpath(os.path.expanduser(raw))
    return path, os.path.isfile(path)


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


async def api_file_write(request: web.Request) -> web.Response:
    """POST /api/file-write — write file content from the markdown panel."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
    from kiro_crew.validation import (  # noqa: F811
        FILE_WRITE_SCHEMA,
        ValidationError,
        validate_tool_args,
    )

    # Ahead of the body read and the path probe. This route rewrites any existing
    # file off the sensitive floor, which includes the steering documents, the
    # skills and the MCP config whose own write routes are owner-gated; leaving it
    # open would hand a non-owner (a Slack-allowlisted user's dashboard session) every
    # file those gates protect. Ahead of the probe too, so whether a path exists is
    # not a non-owner's to learn from a 404.
    owner_denied = await require_owner_dashboard_request(request, "file_write")
    if owner_denied is not None:
        return owner_denied

    # max_bytes=None: the body carries the file's whole contents, which has no
    # defensible byte ceiling.
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success

    try:
        validate_tool_args(
            {"path": body.get("path", ""), "content": body.get("content", "")}, FILE_WRITE_SCHEMA
        )
    except ValidationError:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="file_write",
            outcome="denied",
            resources=body.get("path", ""),
        )
        return web.json_response({"error": "invalid input"}, status=400)

    # Off-loop: validation and the stat are filesystem syscalls that must not
    # run on the event loop (see _probe_request_path).
    try:
        probe = await _run_path_probe(_probe_request_path, body.get("path", ""))
    except _PathProbeBusy:
        return _probe_busy_response(resource=body.get("path", ""), tool_name="file_write")
    path = probe.path
    if not path:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="file_write",
            outcome="denied",
            resources=body.get("path", ""),
        )
        return web.json_response({"error": "invalid or forbidden path"}, status=400)
    if not probe.is_file:
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_write", outcome="not_found", resources=path
        )
        return web.json_response({"error": "not found"}, status=404)
    try:
        # Off the event loop: see _file_write_blocking's own note on why the
        # whole transaction is offloaded rather than each call individually.
        outcome = await asyncio.to_thread(_file_write_blocking, path, body.get("content", ""))
        if outcome == "notfound":
            _sel().log_tool_invocation(
                session_key="dashboard",
                tool_name="file_write",
                outcome="not_found",
                resources=path,
            )
            return web.json_response({"error": "not found", "code": "not_found"}, status=404)
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_write", outcome="success", resources=path
        )
        return web.json_response({"ok": True})
    except Exception:
        logging.getLogger(__name__).exception("file_write failed for %s", path)
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_write", outcome="failure", resources=path
        )
        return web.json_response({"error": "failed to write file"}, status=500)


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


async def api_file_grep(request: web.Request) -> web.Response:
    """POST /api/file-grep ``{"root", "q"}`` — content search under one directory.

    A POST with the query in the BODY, not a GET with it in the URL: the query is
    secret-class text (see below), and a request target is what proxies and
    access logs retain. The same reason keeps it off the ripgrep argv.

    Answers ``{"results", "truncated", "engine", "skipped_docs", "root"}``: a
    text pass (ripgrep where the host has a vetted one, an equivalent python walk
    otherwise) then a document pass over ``.docx``/``.pptx``/``.xlsx``, inside
    one wall-clock budget.

    Every filesystem call lives in a helper handed to :func:`_run_path_probe`:
    this coroutine runs on the gateway's only event loop and the root is the
    caller's to choose. Naming the engine costs a ``$PATH`` walk, so the refusal
    shapes below report an empty engine rather than probe for ripgrep on the
    loop. The search takes a TRANSFER slot, not a probe slot, because it holds
    its worker for the length of the search.
    """
    owner_denied = await require_owner_dashboard_request(request, "file_grep")
    if owner_denied is not None:
        return owner_denied
    caller = request.get("user", "dashboard")
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    query = str(body.get("q") or "").strip()
    raw_root = str(body.get("root") or "").strip()
    # The query is secret-class text (a user grepping for a token VALUE types the
    # token), so it is redacted before anything durable sees it. Context-aware so
    # a loaded companion's stronger credential regexes apply.
    logged_query = redact_log_via_context(query)
    empty: dict = {
        "results": [],
        "truncated": False,
        "engine": "",
        "skipped_docs": 0,
        "root": "",
    }
    # A newline joins the length bounds rather than earning a code: both engines
    # are line-oriented, so such a pattern matches nothing either way -- and
    # `--file` would read two lines as two patterns OR-ed together.
    if (
        not raw_root
        or not _GREP_MIN_QUERY_CHARS <= len(query) <= _GREP_MAX_QUERY_CHARS
        or "\n" in query
        or "\r" in query
    ):
        return web.json_response(empty)

    try:
        root, root_is_dir = await _run_path_probe(_grep_resolve_root, raw_root)
    except _PathProbeBusy:
        return _probe_busy_response(resource=raw_root, operation="file_grep", caller=caller)
    # An empty root is a refused name or a credential store; both get the 403.
    if not root:
        _sel().log_api_access(
            caller=caller, operation="file_grep", outcome="denied",
            resources=raw_root, error="sensitive path",
        )
        return web.json_response(
            {"error": "Access denied", "code": "sensitive_path"}, status=403
        )
    if not root_is_dir:
        # Spelled out rather than spread from `empty`: the error-code ratchet
        # reads response bodies as literals.
        return web.json_response(
            {
                "results": [],
                "truncated": False,
                "engine": "",
                "skipped_docs": 0,
                "root": "",
                "error": "Search root is not a directory",
                "code": "not_a_directory",
            },
            status=404,
        )

    def _search() -> tuple[list[dict], bool, str, int]:
        """The whole search on one transfer worker. Blocking by construction."""
        deadline = time.monotonic() + _GREP_TIME_BUDGET_SECS
        attempt = _grep_rg(root, query, deadline)
        if attempt is None:
            engine = "python"
            results, truncated = _grep_python(root, query, deadline)
        else:
            engine = "rg"
            results, truncated = attempt
        doc_hits, skipped, doc_truncated = _grep_docs(root, query, deadline, len(results))
        return results + doc_hits, truncated or doc_truncated, engine, skipped

    try:
        results, truncated, engine, skipped_docs = await _run_path_probe(
            _search, transfer=True
        )
    except _PathProbeBusy:
        return _probe_busy_response(
            resource=f"q={logged_query} root={root}", operation="file_grep", caller=caller
        )

    _sel().log_api_access(
        caller=caller, operation="file_grep", outcome="allowed",
        resources=(
            f"q={logged_query} root={root} engine={engine} results={len(results)} "
            f"truncated={truncated} skipped_docs={skipped_docs}"
        ),
    )
    return web.json_response({
        "results": results,
        "truncated": truncated,
        "engine": engine,
        "skipped_docs": skipped_docs,
        "root": root,
    })


async def api_file_diff(request: web.Request) -> web.Response:
    """GET /api/file-diff?path=... — returns git diff and HEAD content for a file."""
    owner_denied = await require_owner_dashboard_request(request, "file_diff")
    if owner_denied is not None:
        return owner_denied
    raw_path = request.query.get("path", "").strip()
    if not raw_path:
        _sel().log_api_access(caller=request.get("user", "dashboard"), operation="file_diff", outcome="allowed", resources="empty_path")
        return web.json_response({"diff": "", "original": ""})
    # Off-loop: realpath then the isfile probe, on a caller-supplied path.
    try:
        raw_path, path_is_file = await _run_path_probe(_resolve_diff_path, raw_path)
    except _PathProbeBusy:
        return _probe_busy_response(
            resource=raw_path, operation="file_diff", caller=request.get("user", "dashboard")
        )
    if not path_is_file:
        _sel().log_api_access(caller=request.get("user", "dashboard"), operation="file_diff", outcome="allowed", resources=f"path={raw_path}", error="not_found")
        return web.json_response({"diff": "", "original": ""})
    if is_sensitive_path(raw_path):
        _sel().log_api_access(caller=request.get("user", "dashboard"), operation="file_diff", outcome="denied", resources=raw_path, error="sensitive path")
        return web.json_response({"error": "Access denied"}, status=403)

    dirpath = os.path.dirname(raw_path)

    def _run() -> dict:
        # Disable textconv/filter drivers and fsmonitor to prevent code execution
        # via .gitattributes or .git/config in untrusted repos.
        _git = ["git", "-c", "diff.textconv=", "-c", "core.attributesFile=/dev/null", "-c", "core.fsmonitor="]
        _env = {**os.environ, "GIT_ATTR_NOSYSTEM": "1"}
        try:
            subprocess.run(
                [*_git, "rev-parse", "--git-dir"],
                cwd=dirpath, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=5, check=True, env=_env,
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError, UnicodeDecodeError):
            # Only a failed repository preflight may claim "not a git repo":
            # the client renders not_git as "there is no baseline", which is a
            # statement about the file, not about git's health. Failures past
            # this point (a timeout on a slow repo, git disappearing mid-flight)
            # are computation failures and must report "error" instead.
            return {"diff": "", "original": "", "status": "not_git"}
        try:
            # Get HEAD content
            root = subprocess.run(
                [*_git, "rev-parse", "--show-toplevel"],
                cwd=dirpath, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=5, env=_env,
            ).stdout.strip()
            rel = os.path.relpath(raw_path, root)
            head = subprocess.run(
                [*_git, "show", "--no-textconv", f"HEAD:{rel}"],
                cwd=dirpath, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=10, env=_env,
            )
            original = head.stdout if head.returncode == 0 else ""
            # Get diff
            r = subprocess.run(
                [*_git, "diff", "--no-textconv", "--no-ext-diff", "HEAD", "--", raw_path],
                cwd=dirpath, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=10, env=_env,
            )
            diff = r.stdout.strip() if r.returncode == 0 else ""
            if not diff:
                # Check for untracked file
                r2 = subprocess.run(
                    [*_git, "status", "--porcelain", "--", raw_path],
                    cwd=dirpath, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=5, env=_env,
                )
                if r2.returncode == 0 and r2.stdout.strip().startswith("??"):
                    r3 = subprocess.run(
                        [*_git, "diff", "--no-textconv", "--no-ext-diff", "--no-index", "/dev/null", raw_path],
                        cwd=dirpath, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=10, env=_env,
                    )
                    diff = r3.stdout if r3.stdout else ""
                    return {"diff": diff, "original": "", "status": "untracked"}
            if r.returncode != 0:
                # `git diff` failed and the untracked probe above did not claim
                # the file. This must stay distinguishable from a genuinely
                # unmodified file: falling through would report status "clean",
                # presenting a git failure as "no changes" — a false negative on
                # a question users act on. The probe runs FIRST because the
                # dominant non-zero exit is `fatal: bad revision 'HEAD'` in a
                # freshly-initialized repo with no commits, where every file is
                # simply untracked and the all-added diff is the true answer.
                # Still HTTP 200: the request succeeded, only the diff did not.
                return {"diff": "", "original": original, "status": "error"}
            status = "modified" if diff else "clean"
            return {"diff": diff, "original": original, "status": status}
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError, UnicodeDecodeError):
            return {"diff": "", "original": "", "status": "error"}

    # Same verdict as ``api_file_read`` for the same caller: the panel compares
    # the buffer that read served against this ``original``, so the two MUST be
    # redacted alike -- one side raw and the other masked renders an unchanged
    # credential line as a hunk, in either direction.
    bypass = await _owner_view_bypasses_credential_pass(request)

    def _run_redacted() -> dict:
        # Both text fields carry file content, so they pass through the same
        # redactor ``api_file_read`` applies to the panel's buffer. The panel's
        # diff view compares that redacted buffer against this ``original``, so
        # leaving one side raw makes an unchanged credential line render as a
        # hunk, and serves a secret committed in HEAD that ``/api/file-read``
        # masks. Redacting the assembled result covers every branch, including
        # ones added later, and runs in this worker thread rather than on the
        # event loop because the input is caller-sized.
        result = _run()
        # Deliberately NOT truncated first: slicing before the pass can cut a
        # credential's regex-required tail, and the surviving prefix is then
        # served as real bytes. Redacting whole text costs an unbounded scan,
        # which is why it runs here rather than on the event loop.
        if bypass:
            result["original"] = redact_owner_view_via_context(result.get("original", ""))
            result["diff"] = redact_owner_view_via_context(result.get("diff", ""))
        else:
            result["original"] = redact(result.get("original", ""))
            result["diff"] = redact(result.get("diff", ""))
        return result

    result = await asyncio.to_thread(_run_redacted)
    _sel().log_api_access(caller=request.get("user", "dashboard"), operation="file_diff", outcome="allowed", resources=f"path={raw_path}")
    return web.json_response(result)


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
_file_api.compose(
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
    ),
)
