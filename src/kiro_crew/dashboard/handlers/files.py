"""File I/O, outbox, upload, workspace CRUD, and file search handlers."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError
from aiohttp.multipart import BodyPartReader

from kiro_crew.config.loader import KiroCrewConfig, WorkspaceConfig
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.security import (
    BINARY_MIME_ALLOWLIST,
    is_sensitive_path,
)
from kiro_crew.slack.handler import is_tracked_channel
from kiro_crew.validation import (
    FILE_READ_SCHEMA,
    FILE_SEND_SCHEMA,
    ValidationError,
    validate_tool_args,
)

# Register OOXML office MIME types explicitly. The system mimetypes
# database on AL2/AL2023 build hosts does NOT include .docx, .xlsx, or
# .pptx by default, so mimetypes.guess_type() returns (None, None) for
# those. Registering at module import time keeps api_file_download's
# Content-Type header correct for the most common Word/Excel/PowerPoint
# downloads (the Stores Discovery docx case that motivated this CR).
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


logger = logging.getLogger(__name__)


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811
    return _pkg.sel()


async def api_reveal_path(request: web.Request) -> web.Response:
    """POST /api/reveal — reveal a file/folder in Finder or open with default app."""
    import shutil  # noqa: F811
    import subprocess  # noqa: F811
    import sys  # noqa: F811

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "invalid JSON body"}, status=400)
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
    if action == "open":
        if not os.path.isfile(path):
            return web.json_response({"error": "not a regular file"}, status=400)
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", path])
        else:
            return web.json_response({"ok": True, "copy": path})
    else:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(Path(path).parent)])
        else:
            return web.json_response({"ok": True, "copy": path})
    _sel().log_tool_invocation(
        session_key="api", source="api", tool_name="reveal_path",
        outcome="success", resources=path, metadata={"action": action})
    return web.json_response({"ok": True})


async def api_outbox_notify(request: web.Request) -> web.Response:
    """POST /api/outbox/notify — agent sent a file, notify the user."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):

        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="notify",
            outcome="denied",
            error="invalid_json_body",
        )
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    raw_path = body.get("path", "")
    raw_filename = body.get("filename", "")
    raw_desc = body.get("description", "")
    # Reject files whose names/paths contain sensitive patterns (per bobvo review)
    if redact(raw_filename) != raw_filename or redact(raw_path) != raw_path:

        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="notify",
            outcome="denied",
            error="sensitive_filename_rejected",
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
    # Validate file is readable + UTF-8 before creating a persistent card
    from pathlib import Path  # noqa: F811

    from kiro_crew.config.loader import outbox_dir  # noqa: F811
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes  # noqa: F811

    resolved = Path(file_data["path"]).resolve()
    if not resolved.is_relative_to(outbox_dir().resolve()):

        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="notify",
            outcome="denied",
            error="path_outside_outbox",
        )
        return web.json_response({"error": "path must be inside outbox"}, status=403)
    try:
        raw = safe_read_file_bytes(str(resolved))
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
    # Text files: check for sensitive content. Binary files: skip content scan
    # and validate MIME against the shared BINARY_MIME_ALLOWLIST.
    try:
        text = raw.decode("utf-8")
        if redact(text) != text:
            _sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="file_send",
                tool_kind="notify",
                outcome="denied",
                error="sensitive_content_detected",
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
    # Inject into the caller's chat slot so the card persists in the correct session
    if state._slots:
        # Prefer the caller's own slot via X-Session-Key header
        session_key = request.headers.get("X-Session-Key", "").strip()
        active = None
        if session_key.startswith("dashboard:"):
            slot_key = session_key.removeprefix("dashboard:")
            active = state.get_slot(slot_key)
        elif session_key.startswith("cron:"):
            slot_key = f"cron-{session_key.removeprefix('cron:')}"
            active = state.get_slot(slot_key)
        # An explicitly header-targeted slot receives the file even when empty
        header_targeted = active is not None
        # Fallback: most recently active slot
        if not active:
            active = max(
                state._slots.values(),
                key=lambda s: s.messages[-1]["ts"] if s.messages else "",
            )
        if active and (active.messages or header_targeted):
            # Route through the context-aware redact() so a loaded companion's
            # extra credential regexes scrub the broadcast file JSON too — the
            # same overlay-aware pass the filename/path/description gates use.
            redacted_file_json = redact(json.dumps(file_data))
            active.append("file", redacted_file_json)
            # Only broadcast explicitly when _has_reader suppresses append's
            # built-in _on_message callback. Avoids duplicate file cards.
            if getattr(active, "_has_reader", False):
                state.broadcast_ws("chat_message", {
                    "slot": active.key,
                    "role": "file",
                    "content": redacted_file_json,
                    "ts": active.messages[-1]["ts"],
                })

    _sel().log_tool_invocation(
        session_key="api",
        source="api",
        tool_name="file_send",
        tool_kind="notify",
        outcome="completed",
        resources=f"filename={file_data['filename']}",
    )
    return web.json_response({"ok": True})


async def api_outbox_download(request: web.Request) -> web.StreamResponse:
    """GET /api/outbox/{filename} — download a file from the outbox."""
    import urllib.parse  # noqa: F811

    from kiro_crew.config.loader import outbox_dir  # noqa: F811
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes  # noqa: F811

    filename = request.match_info["filename"]
    path = (outbox_dir() / filename).resolve()
    if not path.is_relative_to(outbox_dir().resolve()):
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="download",
            outcome="denied",
            error=f"path_traversal: {filename}",
        )
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        raw = safe_read_file_bytes(str(path))
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
    # For text files, scan for sensitive content; binary files served as-is
    # against the shared BINARY_MIME_ALLOWLIST (deny-by-default).
    is_text = True
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        is_text = False
    if is_text:
        redacted = redact(text)
        if redacted != text:
            _sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="file_send",
                tool_kind="download",
                outcome="denied",
                error="content_redacted",
            )
            return web.json_response(
                {"error": "file content was redacted; download aborted"}, status=400
            )
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


async def api_slack_upload_file(request: web.Request) -> web.Response:
    """POST /api/slack/upload-file — upload a file to Slack (internal, called by file_send)."""
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes  # noqa: F811

    state: DashboardState = request.app["state"]
    slack = state.slack_client
    if not slack:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="slack",
            outcome="skipped",
            error="no_slack_client",
        )
        return web.json_response({"ok": True, "skipped": "no_slack"})
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="slack",
            outcome="denied",
            error="invalid_json_body",
        )
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    file_path_raw = body.get("file_path", "")
    filename = body.get("filename", "")
    thread_ts = body.get("thread_ts")
    if not file_path_raw or not filename:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="slack",
            outcome="denied",
            error="missing_required_fields",
        )
        return web.json_response({"error": "file_path, filename required"}, status=400)
    file_path = file_path_raw
    resolved = Path(file_path).resolve()
    from kiro_crew.config.loader import outbox_dir, workspace_root  # noqa: F811

    allowed_outbox = outbox_dir().resolve()
    allowed_workspace = workspace_root().resolve()
    if not (resolved.is_relative_to(allowed_outbox) or resolved.is_relative_to(allowed_workspace)):
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="slack",
            outcome="denied",
            downstream_service="slack",
            error=f"path_not_allowed: {file_path}",
        )
        return web.json_response({"error": "file_path must be under ~/.kirocrew/"}, status=403)
    try:
        raw = safe_read_file_bytes(str(resolved))
    except FileTooLargeError as e:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="slack",
            outcome="denied",
            downstream_service="slack",
            error=f"file_too_large: {e}",
        )
        return web.json_response({"error": str(e)}, status=413)
    if raw is None:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="slack",
            outcome="denied",
            downstream_service="slack",
            error=f"safe_read_file_bytes rejected: {file_path}",
        )
        return web.json_response(
            {"error": f"File not found or access denied: {file_path}"}, status=404
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Binary file — only allow known-safe media types
        guessed_type = mimetypes.guess_type(filename)[0] or ""
        if guessed_type not in BINARY_MIME_ALLOWLIST:
            _sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="file_send",
                tool_kind="slack",
                outcome="denied",
                downstream_service="slack",
                error=f"binary_mime_not_allowed: {guessed_type}",
            )
            return web.json_response(
                {"error": f"Binary file type not allowed: {guessed_type or 'unknown'}"}, status=400
            )
        text = None  # signal: skip text redaction path
        # Scan binary content for embedded credentials (e.g. base64-encoded keys in PDFs)
        binary_text = raw.decode("latin-1")
        if redact(binary_text) != binary_text:
            _sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="file_send",
                tool_kind="slack",
                outcome="denied",
                downstream_service="slack",
                error="binary_credential_detected",
            )
            return web.json_response(
                {"error": "binary file contains embedded credentials"}, status=400
            )
    if text is not None:
        try:
            redacted = redact(text)
            if redacted != text:
                _sel().log_tool_invocation(
                    session_key="api",
                    source="api",
                    tool_name="file_send",
                    tool_kind="slack",
                    outcome="denied",
                    downstream_service="slack",
                    error="content_redacted",
                )
                return web.json_response(
                    {"error": "file content was redacted; upload aborted"}, status=400
                )
        except Exception as redact_err:
            _sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="file_send",
                tool_kind="slack",
                outcome="error",
                downstream_service="slack",
                error=f"redaction_failed: {redact_err}",
            )
            return web.json_response({"error": f"Redaction failed: {redact_err}"}, status=500)
    # Resolve thread_ts and channel from linked slot when not explicitly provided
    target_channel = body.get("channel", "")
    channel_from_session_map = False
    session_key = request.headers.get("X-Session-Key", "").strip()
    if not thread_ts and session_key.startswith("dashboard:") and state.sessions:
        link_ts, link_ch = state.sessions.get_slack_link(session_key)
        if link_ts and (not target_channel or target_channel == link_ch):
            thread_ts = link_ts
            if not target_channel and link_ch:
                target_channel = link_ch
                channel_from_session_map = True
    # Resolve channel: use explicit channel if provided, else owner DM
    channel = ""
    if target_channel:
        try:
            validate_tool_args(
                {"path": "x", "channel": target_channel}, FILE_SEND_SCHEMA
            )
        except ValidationError:
            _sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="file_send",
                tool_kind="slack",
                outcome="denied",
                downstream_service="slack",
                error="channel_validation_failed",
            )
            return web.json_response(
                {"error": "invalid channel value"}, status=400
            )
        # Session-map-sourced channels are trusted (system created the link).
        # Only enforce tracking check for user-supplied channels.
        # Defense-in-depth: session-map channels must be DMs (D-prefix) or tracked.
        if not channel_from_session_map:
            try:
                tracked = is_tracked_channel(target_channel)
            except Exception:
                tracked = False  # deny-by-default extends to uncertainty
            if not tracked:
                _sel().log_tool_invocation(
                    session_key="api",
                    source="api",
                    tool_name="file_send",
                    tool_kind="slack",
                    outcome="denied",
                    downstream_service="slack",
                    error=f"channel_not_tracked: {target_channel}",
                )
                return web.json_response(
                    {"error": "channel not in tracked channels"}, status=403
                )
        else:
            try:
                allowed = target_channel.startswith("D") or is_tracked_channel(target_channel)
            except Exception:
                allowed = False  # deny-by-default extends to uncertainty
            if not allowed:
                _sel().log_tool_invocation(
                    session_key="api",
                    source="api",
                    tool_name="file_send",
                    tool_kind="slack",
                    outcome="denied",
                    downstream_service="slack",
                    error=f"session_map_channel_not_authorized: {target_channel}",
                )
                return web.json_response(
                    {"error": "channel not authorized"}, status=403
                )
        channel = target_channel
    else:
        try:
            creds = KiroCrewConfig.load().load_credentials()
            owner_id = creds.get("KIROCREW_OWNER_ID", "")
            if owner_id:
                channel = await slack.open_dm(owner_id)
        except Exception:
            pass
    if not channel:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="slack",
            outcome="skipped",
            error="no_channel",
        )
        return web.json_response({"ok": True, "skipped": "no_channel"})
    try:
        safe_filename = filename
        if redact(safe_filename) != safe_filename:
            _sel().log_tool_invocation(
                session_key="api",
                source="api",
                tool_name="file_send",
                tool_kind="slack",
                outcome="denied",
                downstream_service="slack",
                error="sensitive_filename_rejected",
            )
            return web.json_response({"error": "filename contains sensitive content"}, status=400)
        await slack.upload_file(
            channel,
            thread_ts or "",
            str(resolved),
            safe_filename,
            safe_filename,
        )
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="slack",
            outcome="completed",
            downstream_service="slack",
            resources=f"channel={channel} file={file_path}",
        )
        return web.json_response({"ok": True})
    except Exception as e:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="file_send",
            tool_kind="slack",
            outcome="error",
            downstream_service="slack",
            error=str(e),
        )
        return web.json_response({"error": str(e)}, status=500)


async def api_upload(request: web.Request) -> web.Response:
    """POST /api/upload — open native file picker and return selected paths."""
    if sys.platform != "darwin":
        return web.json_response({"error": "File picker is only available on macOS"}, status=400)

    proc = await asyncio.create_subprocess_exec(
        "osascript",
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


_SCREENSHOT_DIR = Path.home() / ".kirocrew" / "screenshots"

_UPLOAD_DIR = Path.home() / ".kirocrew" / "uploads"
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB per file
_MAX_UPLOAD_FILES = 20  # max files per request
_ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_ALLOWED_TEXT_EXT = {
    ".txt",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".csv",
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


def _write_file_restricted(path: Path, data: bytes) -> None:
    """Write file with owner-only permissions (0o600)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


async def api_upload_file(request: web.Request) -> web.Response:
    """POST /api/upload/file — cross-platform multipart file upload.

    Accepts multipart form data with one or more 'file' fields.
    Saves files to ~/.kirocrew/uploads/ and returns server-side paths
    that ACP's _send_prompt() can detect for image inlining.
    """

    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    reader = await request.multipart()
    paths: list[str] = []
    allowed = _ALLOWED_IMAGE_EXT | _ALLOWED_TEXT_EXT | _ALLOWED_DOC_EXT
    caller = request.get("user", "dashboard")

    def _cleanup() -> None:
        for p in paths:
            Path(p).unlink(missing_ok=True)

    try:
        while True:
            part = await reader.next()
            if part is None:
                break
            if not isinstance(part, BodyPartReader):
                continue
            if part.name != "file":
                continue
            if len(paths) >= _MAX_UPLOAD_FILES:
                _cleanup()
                _sel().log_api_access(
                    caller=caller,
                    operation="upload.file",
                    outcome="rejected",
                    source="dashboard",
                    resources=f"reason:too_many_files:{_MAX_UPLOAD_FILES}",
                )
                return web.json_response(
                    {"error": f"Too many files (max {_MAX_UPLOAD_FILES})"},
                    status=400,
                )
            fname = part.filename or "upload"
            # Sanitize: strip path components to prevent traversal
            safe_name = re.sub(r"[^\w.\-]", "_", Path(fname).name)
            ext = Path(safe_name).suffix.lower()
            if ext not in allowed:
                _cleanup()
                _sel().log_api_access(
                    caller=caller,
                    operation="upload.file",
                    outcome="rejected",
                    source="dashboard",
                    resources=f"file:{fname} reason:unsupported_type:{ext}",
                )
                return web.json_response(
                    {"error": f"Unsupported file type: {ext}"},
                    status=400,
                )
            # Read with size limit
            data = bytearray()
            while True:
                chunk = await part.read_chunk(8192)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > _MAX_UPLOAD_BYTES:
                    _cleanup()
                    _sel().log_api_access(
                        caller=caller,
                        operation="upload.file",
                        outcome="rejected",
                        source="dashboard",
                        resources=f"file:{fname} reason:too_large:{len(data)}",
                    )
                    return web.json_response(
                        {"error": f"File too large (max {_MAX_UPLOAD_BYTES // 1024 // 1024}MB)"},
                        status=413,
                    )
            # UUID prefix guarantees uniqueness even within a single request
            dest = _UPLOAD_DIR / f"{uuid.uuid4().hex}_{safe_name}"
            if not dest.resolve().is_relative_to(_UPLOAD_DIR.resolve()):
                _cleanup()
                _sel().log_api_access(
                    caller=caller,
                    operation="upload.file",
                    outcome="rejected",
                    source="dashboard",
                    resources=f"file:{fname} reason:path_traversal",
                )
                return web.json_response({"error": "Invalid filename"}, status=400)
            try:
                await asyncio.to_thread(_write_file_restricted, dest, bytes(data))
            except Exception:
                dest.unlink(missing_ok=True)
                raise
            # Diagnostic logging for binary uploads. Compares the bytes
            # we received in memory against the bytes that landed on
            # disk after _write_file_restricted, so a future report of
            # "uploaded .docx is corrupted" can be pinned to the
            # upload pipeline vs post-upload tampering. Logged for
            # extensions that are binary archives (docx/xlsx/pptx/odt/
            # zip/pdf etc.) where any byte mismatch breaks the file;
            # text uploads aren't worth the I/O.
            if ext in _ALLOWED_DOC_EXT or ext in _ALLOWED_IMAGE_EXT:
                try:
                    sent_sha = hashlib.sha256(bytes(data)).hexdigest()
                    on_disk = dest.read_bytes()
                    disk_sha = hashlib.sha256(on_disk).hexdigest()
                    head_hex = on_disk[:4].hex() if on_disk else ""
                    is_zip_ext = ext in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".zip"}
                    is_zip = zipfile.is_zipfile(str(dest)) if is_zip_ext else None
                    logger.info(
                        "upload.file diagnostic: name=%s ext=%s sent_size=%d disk_size=%d "
                        "sent_sha256=%s disk_sha256=%s match=%s magic=%s is_zipfile=%s",
                        safe_name,
                        ext,
                        len(data),
                        len(on_disk),
                        sent_sha,
                        disk_sha,
                        sent_sha == disk_sha,
                        head_hex,
                        is_zip,
                    )
                except Exception:
                    # Diagnostic failure must never break the upload.
                    logger.exception("upload.file diagnostic failed for %s", safe_name)
            paths.append(str(dest))
    except Exception:
        _cleanup()
        _sel().log_api_access(
            caller=caller,
            operation="upload.file",
            outcome="error",
            source="dashboard",
            resources=f"files_written:{len(paths)}",
        )
        raise
    if not paths:
        _sel().log_api_access(
            caller=caller,
            operation="upload.file",
            outcome="rejected",
            source="dashboard",
            resources="reason:no_files",
        )
        return web.json_response({"error": "No files uploaded"}, status=400)
    _sel().log_api_access(
        caller=caller,
        operation="upload.file",
        outcome="success",
        source="dashboard",
        resources=f"files:{len(paths)}",
    )
    return web.json_response({"paths": paths})


async def api_screenshot(request: web.Request) -> web.Response:
    """POST /api/screenshot — capture screen region and return file path.

    macOS only — uses built-in screencapture. Linux cloud desktops
    (AL2, headless) don't have a display server so this is unavailable.
    """
    if sys.platform != "darwin":
        return web.json_response({"error": "Screenshot is only available on macOS"}, status=400)

    _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    dest = _SCREENSHOT_DIR / f"screenshot_{ts}.png"

    proc = await asyncio.create_subprocess_exec(
        "screencapture",
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


# ── Workspace API ──
async def api_workspaces(request: web.Request) -> web.Response:
    """GET /api/workspaces — list configured workspaces."""
    cfg = KiroCrewConfig.load()
    default_ws = cfg.default_workspace
    result = []
    for name, ws in cfg.workspaces.items():
        result.append({"name": name, "path": ws.dir, "is_default": name == default_ws})
    if not result:
        result.append({"name": "default", "path": "workspace", "is_default": True})
    return web.json_response({"workspaces": result, "default": default_ws})


async def api_workspaces_create(request: web.Request) -> web.Response:
    """POST /api/workspaces — create a new workspace."""
    import asyncio  # noqa: F811
    import shutil  # noqa: F811

    from kiro_crew.config.loader import config_dir  # noqa: F811
    from kiro_crew.validation import WORKSPACE_NAME_RE  # noqa: F811

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    name = body.get("name", "").strip()
    if not name:
        return web.json_response({"error": "Workspace name is required"}, status=400)
    if not WORKSPACE_NAME_RE.match(name):
        return web.json_response(
            {"error": "Invalid workspace name (use alphanumeric, hyphens, underscores)"},
            status=400,
        )
    cfg = KiroCrewConfig.load()
    if name in cfg.workspaces:
        return web.json_response({"error": f"Workspace '{name}' already exists"}, status=409)
    copy_from = body.get("copy_from", "").strip()
    if copy_from:
        if copy_from not in cfg.workspaces:
            return web.json_response(
                {"error": f"Source workspace '{copy_from}' not found"}, status=404
            )
        # New workspace gets its own directory, named after the workspace
        ws_dir = body.get("dir", f"workspace-{name}")
        # Check for directory collision with existing workspaces
        existing_dirs = {ws.dir for ws in cfg.workspaces.values()}
        if ws_dir in existing_dirs:
            return web.json_response(
                {"error": f"Directory '{ws_dir}' is already used by another workspace"},
                status=409,
            )
        # Recursively copy source workspace data to the new directory
        src_path = config_dir() / cfg.workspaces[copy_from].dir
        dst_path = config_dir() / ws_dir
        # Guard against path traversal
        if not dst_path.resolve().is_relative_to(config_dir().resolve()):
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="workspace.create",
                outcome="denied",
                source="dashboard",
                resources=name,
            )
            return web.json_response({"error": "Invalid directory path"}, status=400)
        if not src_path.resolve().is_relative_to(config_dir().resolve()):
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="workspace.create",
                outcome="denied",
                source="dashboard",
                resources=name,
            )
            return web.json_response({"error": "Invalid source directory path"}, status=400)
        # Reject config root itself to avoid copying .env / config.json
        cfg_root = config_dir().resolve()
        if src_path.resolve() == cfg_root or dst_path.resolve() == cfg_root:
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="workspace.create",
                outcome="denied",
                source="dashboard",
                resources=name,
            )
            return web.json_response(
                {"error": "Cannot use config root as workspace directory"}, status=400
            )
        if src_path.is_dir():
            # Use is_sensitive_path to filter entries instead of hardcoded names
            from kiro_crew.security import is_sensitive_path  # noqa: F811

            def _ignore_sensitive(directory: str, entries: list[str]) -> set[str]:
                from pathlib import Path as _Path  # noqa: F811

                skip: set[str] = set()
                for entry in entries:
                    full = str(_Path(directory, entry).resolve())
                    if is_sensitive_path(full):
                        skip.add(entry)
                return skip

            await asyncio.to_thread(
                shutil.copytree,
                src_path,
                dst_path,
                dirs_exist_ok=True,
                symlinks=True,
                ignore=_ignore_sensitive,
            )
    else:
        ws_dir = body.get("dir", f"workspace-{name}")
    # Guard against path traversal for relative paths; absolute paths are allowed
    from kiro_crew.security import is_sensitive_path as _isp  # noqa: F811

    _abs = Path(ws_dir).expanduser().is_absolute()
    final_path = Path(ws_dir).expanduser().resolve() if _abs else config_dir() / ws_dir

    # Check for directory collision with existing workspaces (resolve both sides)
    def _resolve_ws_dir(d: str) -> Path:
        p = Path(d).expanduser()
        return p.resolve() if p.is_absolute() else (config_dir() / d).resolve()

    existing_resolved = {_resolve_ws_dir(ws.dir) for ws in cfg.workspaces.values()}
    if _resolve_ws_dir(ws_dir) in existing_resolved:
        return web.json_response(
            {"error": f"Directory '{ws_dir}' is already used by another workspace"},
            status=409,
        )
    if _isp(str(final_path.resolve())):
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="workspace.create",
            outcome="denied",
            source="dashboard",
            resources=name,
        )
        return web.json_response({"error": "Invalid directory path"}, status=400)
    if not _abs and not final_path.resolve().is_relative_to(config_dir().resolve()):
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="workspace.create",
            outcome="denied",
            source="dashboard",
            resources=name,
        )
        return web.json_response({"error": "Invalid directory path"}, status=400)
    if final_path.resolve() == config_dir().resolve():
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="workspace.create",
            outcome="denied",
            source="dashboard",
            resources=name,
        )
        return web.json_response(
            {"error": "Cannot use config root as workspace directory"}, status=400
        )
    cfg.workspaces[name] = WorkspaceConfig(dir=ws_dir)
    cfg.save()
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="workspace.create",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True, "name": name})


async def api_workspaces_update(request: web.Request) -> web.Response:
    """PUT /api/workspaces/{name} — update a workspace."""
    from kiro_crew.config.loader import config_dir  # noqa: F811

    name = request.match_info["name"]
    cfg = KiroCrewConfig.load()
    if name not in cfg.workspaces:
        return web.json_response({"error": f"Workspace '{name}' not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    if "dir" in body:
        new_dir = body["dir"]
        from kiro_crew.security import is_sensitive_path as _isp  # noqa: F811

        _abs = Path(new_dir).expanduser().is_absolute()
        resolved = Path(new_dir).expanduser().resolve() if _abs else (config_dir() / new_dir).resolve()
        if _isp(str(resolved)):
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="workspace.update",
                outcome="denied",
                source="dashboard",
                resources=name,
            )
            return web.json_response({"error": "Invalid directory path"}, status=400)
        if not _abs and not resolved.is_relative_to(config_dir().resolve()):
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="workspace.update",
                outcome="denied",
                source="dashboard",
                resources=name,
            )
            return web.json_response({"error": "Invalid directory path"}, status=400)
        if resolved == config_dir().resolve():
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="workspace.update",
                outcome="denied",
                source="dashboard",
                resources=name,
            )
            return web.json_response(
                {"error": "Cannot use config root as workspace directory"}, status=400
            )
        existing_dirs = {
            (config_dir() / ws.dir).resolve()
            if not Path(ws.dir).expanduser().is_absolute()
            else Path(ws.dir).expanduser().resolve()
            for n, ws in cfg.workspaces.items() if n != name
        }
        if resolved in existing_dirs:
            return web.json_response(
                {"error": f"Directory '{new_dir}' is already used by another workspace"},
                status=409,
            )
        cfg.workspaces[name].dir = new_dir
    cfg.save()
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="workspace.update",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True, "name": name})


async def api_workspaces_delete(request: web.Request) -> web.Response:
    """DELETE /api/workspaces/{name} — delete a workspace."""

    name = request.match_info["name"]
    cfg = KiroCrewConfig.load()
    if name not in cfg.workspaces:
        return web.json_response({"error": f"Workspace '{name}' not found"}, status=404)
    if name == cfg.default_workspace:
        return web.json_response(
            {"error": f"Cannot delete default workspace '{name}'. Change default_workspace first."},
            status=409,
        )
    referencing = [a for a, ac in cfg.agents.items() if ac.workspace == name]
    if referencing:
        return web.json_response(
            {"error": f"Workspace '{name}' is referenced by agents: {', '.join(referencing)}"},
            status=409,
        )
    del cfg.workspaces[name]
    cfg.save()
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="workspace.delete",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True})


def _validate_dashboard_path(raw: str) -> str | None:
    """Validate a file path through hooks.py enforcement layer."""
    from kiro_crew.hooks import validate_file_path  # noqa: F811

    return validate_file_path(raw)


async def api_file_watch(request: web.Request) -> web.StreamResponse:
    """GET /api/file-watch?path=... — SSE stream of file content changes."""

    raw_path = request.query.get("path", "")
    try:
        validate_tool_args({"path": raw_path}, FILE_READ_SCHEMA)
    except ValidationError:
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_watch", outcome="denied", resources=raw_path
        )
        return web.json_response({"error": "invalid input"}, status=400)

    path = _validate_dashboard_path(raw_path)
    if not path:
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_watch", outcome="denied", resources=raw_path
        )
        return web.json_response({"error": "invalid or forbidden path"}, status=400)

    _sel().log_tool_invocation(
        session_key="dashboard", tool_name="file_watch", outcome="success", resources=path
    )

    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    await resp.prepare(request)

    poll_interval = 1.0
    read_cap = 512_000
    last_mtime: float = 0.0
    last_content = ""
    resolved_at_start = await asyncio.to_thread(os.path.realpath, path)

    def _read_file(p: str, cap: int) -> str:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read(cap)

    try:
        while not (request.transport is None or request.transport.is_closing()):
            try:
                stat = await asyncio.to_thread(os.stat, path)
                mtime = stat.st_mtime
            except FileNotFoundError:
                await asyncio.sleep(poll_interval)
                continue

            if mtime != last_mtime:
                last_mtime = mtime
                current_resolved = await asyncio.to_thread(os.path.realpath, path)
                if current_resolved != resolved_at_start:
                    logger.warning(
                        "file-watch: symlink changed after validation: %s -> %s",
                        resolved_at_start,
                        current_resolved,
                    )
                    _sel().log_tool_invocation(
                        session_key="dashboard",
                        tool_name="file_watch",
                        outcome="denied",
                        resources=path,
                    )
                    break
                try:
                    content = await asyncio.to_thread(_read_file, current_resolved, read_cap)
                    content = redact(content)
                except Exception:
                    logger.warning("file-watch read error for %s", path, exc_info=True)
                    await asyncio.sleep(poll_interval)
                    continue

                if content != last_content:
                    last_content = content
                    # ensure_ascii=False keeps multi-byte content (e.g. CJK)
                    # inspectable as-is in DevTools instead of \uXXXX escapes,
                    # and produces smaller payloads. Body bytes are still
                    # valid UTF-8 because we explicitly .encode() below.
                    payload = json.dumps({"content": content, "mtime": mtime}, ensure_ascii=False)
                    await resp.write(f"data: {payload}\n\n".encode("utf-8"))

            await asyncio.sleep(poll_interval)
    except (ConnectionResetError, asyncio.CancelledError, ClientConnectionResetError):
        pass

    return resp


async def api_file_read(request: web.Request) -> web.Response:
    """GET /api/file-read?path=... — read file content for the markdown panel."""
    import logging  # noqa: F811
    import os  # noqa: F811

    from kiro_crew.validation import (  # noqa: F811
        FILE_READ_SCHEMA,
        ValidationError,
        validate_tool_args,
    )

    raw_path = request.query.get("path", "")
    # Resolve relative paths against project dir when resolve=1
    if request.query.get("resolve") == "1" and raw_path and not raw_path.startswith(("/", "~")):
        proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
        if not proj:
            return web.json_response(
                {"error": "cannot resolve: no project dir configured"},
                status=400,
            )
        raw_path = os.path.join(proj, raw_path)
        # Ensure resolved path stays within project directory
        resolved = os.path.realpath(raw_path)
        resolved_proj = os.path.realpath(proj)
        if not (resolved == resolved_proj or resolved.startswith(resolved_proj + os.sep)):
            return web.json_response(
                {"error": "path outside project directory"},
                status=400,
            )
        raw_path = resolved

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

    path = _validate_dashboard_path(raw_path)
    if not path:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="file_read",
            outcome="denied",
            resources=raw_path,
        )
        return web.json_response({"error": "invalid or forbidden path"}, status=400)
    if not os.path.isfile(path):
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_read", outcome="not_found", resources=path
        )
        return web.json_response({"error": "not found"}, status=404)
    if request.method == "HEAD":
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_read", outcome="success", resources=path
        )
        return web.Response(status=200)
    try:
        read_cap = 512_000
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(read_cap + 1)
        truncated = len(content) > read_cap
        content = content[:read_cap]
        content = redact(content)
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_read", outcome="success", resources=path
        )
        headers = {"X-Truncated": "true"} if truncated else {}
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


async def api_file_download(request: web.Request) -> web.Response:
    """GET /api/file-download?path=... — download a file as raw bytes.

    Sibling of /api/file-read. file-read decodes content as UTF-8 with
    errors='replace' to render text in the markdown panel; that mode
    corrupts binary files (.docx, .pdf, images) by replacing non-text
    bytes with U+FFFD. This endpoint streams the original bytes, sets
    Content-Disposition: attachment, and applies X-Content-Type-Options:
    nosniff to keep the browser from rendering the response inline.

    Security: same path-validation as file-read (validate_tool_args,
    _validate_dashboard_path, sensitive-path filter). Symlinks rejected
    via O_NOFOLLOW. Files larger than _MAX_UPLOAD_BYTES are rejected.
    Text files are still scanned for sensitive content (credentials and
    exfiltration URLs); a positive hit aborts the download. Binary
    files are served as-is without a MIME allowlist, since attachment
    disposition + nosniff prevents inline rendering on the dashboard
    origin.
    """
    # ``_h`` is a late-binding alias for the parent ``handlers`` package so that
    # tests can monkey-patch ``kiro_crew.dashboard.handlers._validate_dashboard_path``;
    # this is the same pattern api_file_raw uses (legitimate circular-import
    # workaround, listed as an exception in the top-level-imports rule).
    import kiro_crew.dashboard.handlers as _h  # noqa: F811  # circular import

    raw_path = request.query.get("path", "")
    # Resolve relative paths against project dir when resolve=1 (mirrors api_file_read)
    if request.query.get("resolve") == "1" and raw_path and not raw_path.startswith(("/", "~")):
        proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
        if not proj:
            return web.json_response(
                {"error": "cannot resolve: no project dir configured"}, status=400,
            )
        raw_path = os.path.join(proj, raw_path)
        resolved = os.path.realpath(raw_path)
        resolved_proj = os.path.realpath(proj)
        if not (resolved == resolved_proj or resolved.startswith(resolved_proj + os.sep)):
            return web.json_response(
                {"error": "path outside project directory"}, status=400,
            )
        raw_path = resolved

    try:
        validate_tool_args({"path": raw_path}, FILE_READ_SCHEMA)
    except ValidationError:
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_download",
            outcome="denied", resources=raw_path,
        )
        return web.json_response({"error": "invalid input"}, status=400)

    path = _h._validate_dashboard_path(raw_path)
    if not path:
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_download",
            outcome="denied", resources=raw_path,
        )
        return web.json_response({"error": "invalid or forbidden path"}, status=400)
    if is_sensitive_path(path):
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_download",
            outcome="denied", resources=path, error="sensitive_path",
        )
        return web.json_response({"error": "sensitive path blocked"}, status=403)
    if not os.path.isfile(path):
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_download",
            outcome="not_found", resources=path,
        )
        return web.json_response({"error": "not found"}, status=404)

    # Read raw bytes via O_NOFOLLOW to atomically reject symlinks (no TOCTOU race).
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if st.st_size > _MAX_UPLOAD_BYTES:
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="file_download",
                    outcome="denied", resources=path, error="file_too_large",
                )
                return web.json_response({"error": "file too large"}, status=413)
            data = f.read()
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # symlink with O_NOFOLLOW
            _sel().log_tool_invocation(
                session_key="dashboard", tool_name="file_download",
                outcome="denied", resources=path, error="symlink_rejected",
            )
            return web.json_response({"error": "symlinks not allowed"}, status=403)
        logger.exception("file_download read failed for %s", path)
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_download",
            outcome="failure", resources=path,
        )
        return web.json_response({"error": "cannot read file"}, status=500)

    # Defense in depth: scan content for credentials / exfil URLs via the
    # context-aware redact() shim, which runs BOTH the exfil-URL and credential
    # passes (exfil URLs first so embedded credentials in URL fragments are
    # caught) and additionally applies a loaded companion's extra regexes before
    # content reaches an external surface.
    #
    # Mostly-binary files can still hide credential patterns in their
    # decodable runs (e.g. an ASCII-art `AKIA...` with one stray non-UTF-8
    # byte). Decoding with errors='replace' for the *scan only* (the served
    # bytes are still raw) ensures the credential pass cannot be bypassed
    # by sprinkling a single non-UTF-8 byte into the file.
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    # Route through the context-aware redact() so a loaded companion's extra
    # credential regexes also abort the download; the scrubbed != text diff is
    # the gate (no count needed).
    scrubbed = redact(text)
    if scrubbed != text:
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_download",
            outcome="denied", resources=path, error="content_redacted",
        )
        return web.json_response(
            {"error": "file content was redacted; download aborted"}, status=400,
        )

    safe_name = urllib.parse.quote(os.path.basename(path), safe="")
    content_type, _ = mimetypes.guess_type(path)
    if not content_type:
        content_type = "application/octet-stream"

    _sel().log_tool_invocation(
        session_key="dashboard", tool_name="file_download",
        outcome="success", resources=path,
    )
    return web.Response(
        body=data,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{safe_name}",
            "Content-Type": content_type,
            "X-Content-Type-Options": "nosniff",
        },
    )


async def api_file_raw(request: web.Request) -> web.Response:
    """GET /api/file-raw?path=... — serve a file with its native content type (images, etc.)."""
    import os  # noqa: F811

    import kiro_crew.dashboard.handlers as _h  # noqa: F811

    def _log(outcome: str, res: str) -> None:
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_raw", outcome=outcome, resources=res,
        )

    raw_path = request.query.get("path", "")
    path = _h._validate_dashboard_path(raw_path)
    if not path:
        _log("denied", raw_path)
        return web.json_response({"error": "invalid or forbidden path"}, status=400)
    from kiro_crew.security import is_sensitive_path as _isp  # noqa: F811
    if _isp(path):
        _log("denied", path)
        return web.json_response({"error": "sensitive path blocked"}, status=403)
    if not os.path.isfile(path):
        _log("not_found", path)
        return web.json_response({"error": "not found"}, status=404)
    # Open with O_NOFOLLOW to atomically reject symlinks (no TOCTOU race).
    # Read header + full content through the same fd to avoid re-opening.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if st.st_size > _MAX_UPLOAD_BYTES:
                _log("denied", path)
                return web.json_response({"error": "file too large"}, status=413)
            header = f.read(12)
            f.seek(0)
            data = f.read()
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # symlink with O_NOFOLLOW
            _log("denied", path)
            return web.json_response({"error": "symlinks not allowed"}, status=403)
        _log("failure", path)
        return web.json_response({"error": "cannot read file"}, status=500)
    _image_magic = (
        (b"\x89PNG", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"BM", "image/bmp"),
        (b"II\x2a\x00", "image/tiff"),
        (b"MM\x00\x2a", "image/tiff"),
        (b"\x00\x00\x01\x00", "image/x-icon"),
    )
    content_type = None
    # WebP: RIFF....WEBP compound signature
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        content_type = "image/webp"
    else:
        for magic, mime in _image_magic:
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
    headers = {"Content-Type": content_type, "X-Content-Type-Options": "nosniff"}
    if content_type == "image/svg+xml":
        headers["Content-Security-Policy"] = "script-src 'none'; style-src 'unsafe-inline'"
    return web.Response(body=data, headers=headers)


async def api_file_write(request: web.Request) -> web.Response:
    """POST /api/file-write — write file content from the markdown panel."""
    import logging  # noqa: F811
    import os  # noqa: F811

    from kiro_crew.validation import (  # noqa: F811
        FILE_WRITE_SCHEMA,
        ValidationError,
        validate_tool_args,
    )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON body"}, status=400)

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

    path = _validate_dashboard_path(body.get("path", ""))
    if not path:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="file_write",
            outcome="denied",
            resources=body.get("path", ""),
        )
        return web.json_response({"error": "invalid or forbidden path"}, status=400)
    if not os.path.isfile(path):
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_write", outcome="not_found", resources=path
        )
        return web.json_response({"error": "not found"}, status=404)
    try:
        import os  # noqa: F811
        import shutil  # noqa: F811
        import tempfile  # noqa: F811

        tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))
        try:
            try:
                shutil.copymode(path, tmp_path)
            except OSError:
                pass
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(body.get("content", ""))
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
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


def _fuzzy_score(q: str, name: str, rel: str) -> float:
    """Score a file match. Higher = better. Returns 0 for no match."""
    nl = name.lower()
    rl = rel.lower()
    score = 0.0

    # Exact filename match (sans extension)
    stem = nl.rsplit(".", 1)[0] if "." in nl else nl
    if q == nl or q == stem:
        score += 100.0
    elif nl.startswith(q):
        score += 50.0
    elif q in nl:
        score += 30.0
    elif q in rl:
        score += 10.0
    else:
        # Fuzzy: check if query chars appear in order in filename
        matched_on_name = True
        qi = 0
        consecutive = 0
        max_run = 0
        for ch in nl:
            if qi < len(q) and ch == q[qi]:
                qi += 1
                consecutive += 1
                max_run = max(max_run, consecutive)
            else:
                consecutive = 0
        if qi < len(q):
            # Try path if filename didn't match all chars
            matched_on_name = False
            qi = 0
            consecutive = 0
            max_run = 0
            for ch in rl:
                if qi < len(q) and ch == q[qi]:
                    qi += 1
                    consecutive += 1
                    max_run = max(max_run, consecutive)
                else:
                    consecutive = 0
        if qi < len(q):
            return 0.0  # not all query chars found
        # Score based on coverage ratio and longest consecutive run
        matched_len = len(nl) if matched_on_name else len(rl)
        coverage = len(q) / max(matched_len, 1)
        score += 5.0 + 15.0 * (max_run / len(q)) + 5.0 * coverage

    # Bonus: shorter filenames are more relevant
    score += max(0.0, 5.0 - len(nl) * 0.1)
    return score


async def api_file_search(request: web.Request) -> web.Response:
    """GET /api/file-search?q=... — fuzzy filename search for the @-mention file picker."""
    import os  # noqa: F811
    import time  # noqa: F811

    from kiro_crew.security import is_sensitive_path  # noqa: F811

    caller = request.get("user", "dashboard")
    query = request.query.get("q", "").strip().lower()
    if len(query) < 2:
        return web.json_response({"results": []})

    max_results = 15

    # Scope search to project (arbitrary path) or workspace
    project = request.query.get("project", "")
    ws_name = request.query.get("workspace", "")
    search_roots: list[str] = []
    if project:
        project = os.path.realpath(os.path.expanduser(project))
        if is_sensitive_path(project):
            _sel().log_api_access(caller=caller, operation="file_search", outcome="denied", resources=project, error="sensitive path")
            return web.json_response({"error": "Access denied"}, status=403)
        if os.path.isdir(project):
            search_roots.append(project)
        else:
            return web.json_response(
                {"results": [], "error": "Project directory not found"}, status=404
            )
    elif ws_name:
        from kiro_crew.config.loader import workspace_dir_for  # noqa: F811
        ws_path = str(workspace_dir_for(ws_name))
        if os.path.isdir(ws_path):
            search_roots.append(ws_path)

    scoped = bool(search_roots)

    if not search_roots:
        # Fallback: project dir, kirocrew workspace, home
        proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
        if proj and os.path.isdir(proj):
            search_roots.append(proj)
        mc_workspace = os.path.expanduser("~/.kirocrew/workspace")
        if os.path.isdir(mc_workspace):
            search_roots.append(mc_workspace)
        home = os.path.expanduser("~")
        if home not in search_roots:
            search_roots.append(home)

    # Filter out sensitive roots
    safe_roots: list[str] = []
    for r in search_roots:
        if is_sensitive_path(r):
            _sel().log_api_access(caller=caller, operation="file_search", outcome="denied", resources=r, error="sensitive path")
        else:
            safe_roots.append(r)

    # Fast path: use in-memory index when available for a single scoped project
    state: DashboardState = request.app["state"]
    if scoped and len(safe_roots) == 1:
        idx = state.file_indexes.get(safe_roots[0])
        if idx and idx.is_ready and not idx.truncated:
            results = await asyncio.to_thread(idx.search, query, _fuzzy_score, max_results)
            trimmed = [{k: v for k, v in r.items() if k != "_score"} for r in results]
            _sel().log_api_access(caller=caller, operation="file_search", outcome="allowed", resources=f"q={query} indexed=true entries={idx.entry_count} results={len(trimmed)}")
            return web.json_response({"results": trimmed, "root": safe_roots[0]})

    # Fallback: walk filesystem per request
    # Dot-prefixed dirs (.kirocrew, .kiro, .aim) excluded by startswith(".") guard below.
    skip_dirs = {
        ".git", "node_modules", "__pycache__", ".cache", ".venv", "venv",
        "dist", "build", "env", "out", "target",
    }

    max_scan = 50_000 if scoped else 5_000
    max_collect = max_results * 10  # collect enough candidates for good scoring, then stop

    def _walk_file_search() -> list[dict]:
        """Blocking file-system walk — offloaded via asyncio.to_thread."""
        results: list[dict] = []
        walked = 0
        for root_dir in safe_roots:
            if walked >= max_scan or len(results) >= max_collect:
                break
            for dirpath, dirnames, filenames in os.walk(root_dir):
                dirnames[:] = [
                    d for d in dirnames
                    if not d.startswith(".") and d not in skip_dirs
                ]
                for fname in filenames:
                    if walked >= max_scan or len(results) >= max_collect:
                        break
                    walked += 1
                    if fname.startswith("."):
                        continue
                    fpath = os.path.join(dirpath, fname)
                    rel = os.path.relpath(fpath, root_dir)
                    sc = _fuzzy_score(query, fname, rel)
                    if sc <= 0:
                        continue
                    if is_sensitive_path(fpath):
                        continue
                    try:
                        st = os.stat(fpath)
                    except OSError:
                        continue
                    results.append({"path": fpath, "name": fname, "size": st.st_size, "mtime": int(st.st_mtime), "_score": sc})
                if walked >= max_scan or len(results) >= max_collect:
                    break
        return results

    results = await asyncio.to_thread(_walk_file_search)

    # Sort by score descending, then shorter name, then recency
    now = time.time()
    results.sort(key=lambda r: (-r["_score"], len(r["name"]), now - r["mtime"]))

    # Strip internal scoring field before response
    trimmed = [{k: v for k, v in r.items() if k != "_score"} for r in results[:max_results]]

    _sel().log_api_access(caller=caller, operation="file_search", outcome="allowed", resources=f"q={query} roots={len(safe_roots)} results={len(trimmed)}")
    return web.json_response({
        "results": trimmed,
        "root": safe_roots[0] if scoped and safe_roots else "",
    })


async def api_file_diff(request: web.Request) -> web.Response:
    """GET /api/file-diff?path=... — returns git diff and HEAD content for a file."""
    raw_path = request.query.get("path", "").strip()
    if not raw_path:
        _sel().log_api_access(caller=request.get("user", "dashboard"), operation="file_diff", outcome="allowed", resources="empty_path")
        return web.json_response({"diff": "", "original": ""})
    raw_path = os.path.realpath(os.path.expanduser(raw_path))
    if not os.path.isfile(raw_path):
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
                cwd=dirpath, capture_output=True, timeout=5, check=True, env=_env,
            )
            # Get HEAD content
            root = subprocess.run(
                [*_git, "rev-parse", "--show-toplevel"],
                cwd=dirpath, capture_output=True, text=True, timeout=5, env=_env,
            ).stdout.strip()
            rel = os.path.relpath(raw_path, root)
            head = subprocess.run(
                [*_git, "show", "--no-textconv", f"HEAD:{rel}"],
                cwd=dirpath, capture_output=True, text=True, timeout=10, env=_env,
            )
            original = head.stdout if head.returncode == 0 else ""
            # Get diff
            r = subprocess.run(
                [*_git, "diff", "--no-textconv", "--no-ext-diff", "HEAD", "--", raw_path],
                cwd=dirpath, capture_output=True, text=True, timeout=10, env=_env,
            )
            diff = r.stdout.strip() if r.returncode == 0 else ""
            if not diff:
                # Check for untracked file
                r2 = subprocess.run(
                    [*_git, "status", "--porcelain", "--", raw_path],
                    cwd=dirpath, capture_output=True, text=True, timeout=5, env=_env,
                )
                if r2.returncode == 0 and r2.stdout.strip().startswith("??"):
                    r3 = subprocess.run(
                        [*_git, "diff", "--no-textconv", "--no-ext-diff", "--no-index", "/dev/null", raw_path],
                        cwd=dirpath, capture_output=True, text=True, timeout=10, env=_env,
                    )
                    diff = r3.stdout if r3.stdout else ""
                    return {"diff": diff, "original": "", "status": "untracked"}
            status = "modified" if diff else "clean"
            return {"diff": diff, "original": original, "status": status}
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError, UnicodeDecodeError):
            return {"diff": "", "original": "", "status": "not_git"}

    result = await asyncio.to_thread(_run)
    _sel().log_api_access(caller=request.get("user", "dashboard"), operation="file_diff", outcome="allowed", resources=f"path={raw_path}")
    return web.json_response(result)


async def api_browse_dirs(request: web.Request) -> web.Response:
    """GET /api/browse-dirs?path=... — list subdirectories for directory browser."""
    import os  # noqa: F811

    from kiro_crew.security import is_sensitive_path  # noqa: F811

    caller = request.get("user", "dashboard")
    raw = request.query.get("path", "").strip()
    base = os.path.realpath(os.path.expanduser(raw)) if raw else os.path.realpath(os.path.expanduser("~"))
    if not os.path.isdir(base):
        return web.json_response({"error": "Not a directory", "path": base}, status=400)
    if is_sensitive_path(base):
        _sel().log_api_access(caller=caller, operation="browse_dirs", outcome="denied", resources=base, error="sensitive path")
        return web.json_response({"error": "Access denied"}, status=403)
    skip = {".git", "node_modules", "__pycache__", ".cache", ".venv", "venv", "env", ".kirocrew", ".kiro", ".aim"}
    dirs: list[dict] = []
    try:
        for entry in sorted(os.scandir(base), key=lambda e: e.name.lower()):
            if entry.is_dir(follow_symlinks=True) and entry.name not in skip and not entry.name.startswith("."):
                # Resolve symlinks before the sensitivity check — a symlink in
                # a benign dir pointing at ~/.aws would otherwise pass through.
                if is_sensitive_path(os.path.realpath(entry.path)):
                    continue
                dirs.append({"name": entry.name, "path": entry.path})
    except PermissionError:
        pass
    _sel().log_api_access(caller=caller, operation="browse_dirs", outcome="allowed", resources=base)
    return web.json_response({"path": base, "parent": os.path.dirname(base), "dirs": dirs})


async def api_browse_files(request: web.Request) -> web.Response:
    """GET /api/browse-files?path=... — list files and subdirectories for the activity-panel file browser.

    Mirrors api_browse_dirs security model (sensitive-path filtering, access logging,
    skip set for build artifacts) but returns files alongside directories. Entries
    are sorted dirs-first then alphabetically; hidden files and common build dirs
    are skipped.
    """
    caller = request.get("user", "dashboard")
    raw = request.query.get("path", "").strip()
    base = os.path.realpath(os.path.expanduser(raw)) if raw else os.path.realpath(os.path.expanduser("~"))
    if not os.path.isdir(base):
        return web.json_response({"error": "Not a directory", "path": base}, status=400)
    if is_sensitive_path(base):
        _sel().log_api_access(caller=caller, operation="browse_files", outcome="denied", resources=base, error="sensitive path")
        return web.json_response({"error": "Access denied"}, status=403)
    skip = {".git", "node_modules", "__pycache__", ".cache", ".venv", "venv", "env", ".kirocrew", ".kiro", ".aim", "build", "dist", ".next"}
    dirs: list[dict] = []
    files: list[dict] = []
    try:
        # Sort: dirs before files, then alphabetical
        for entry in sorted(os.scandir(base), key=lambda e: (not e.is_dir(follow_symlinks=True), e.name.lower())):
            if entry.name.startswith("."):
                continue
            # Resolve symlinks before the sensitivity check — a symlink in a
            # benign dir pointing at ~/.aws would otherwise pass through.
            if is_sensitive_path(os.path.realpath(entry.path)):
                continue
            # Capture mtime so the activity-panel browser can offer a
            # sort-by-date option; fall back to 0 on a race (entry removed
            # mid-scan) so one unstattable entry never breaks the listing.
            try:
                mtime = int(entry.stat(follow_symlinks=True).st_mtime)
            except OSError:
                mtime = 0
            if entry.is_dir(follow_symlinks=True):
                if entry.name not in skip:
                    dirs.append({"name": entry.name, "path": entry.path, "mtime": mtime})
            elif entry.is_file(follow_symlinks=True):
                files.append({"name": entry.name, "path": entry.path, "mtime": mtime})
    except PermissionError:
        pass
    _sel().log_api_access(caller=caller, operation="browse_files", outcome="allowed", resources=base)
    return web.json_response({"path": base, "parent": os.path.dirname(base), "dirs": dirs, "files": files})


async def api_dashboard_config(request: web.Request) -> web.Response:
    """GET/PUT /api/dashboard/config — read or write dashboard settings."""
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F811

    cfg = KiroCrewConfig.load()
    if request.method == "PUT":
        try:
            body = await request.json()
        except Exception:
            _sel().log_tool_invocation(
                session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
            )
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            _sel().log_tool_invocation(
                session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
            )
            return web.json_response({"error": "request body must be a JSON object"}, status=400)
        _allowed = {"restore_sessions", "restore_window_minutes", "merge_queued_messages", "widget_density", "quick_send", "session_grid"}
        unknown = set(body.keys()) - _allowed
        if unknown:
            _sel().log_tool_invocation(
                session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
            )
            return web.json_response({"error": f"Unknown fields: {unknown}"}, status=400)
        if "restore_sessions" in body:
            val = body["restore_sessions"]
            if not isinstance(val, bool):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response(
                    {"error": "restore_sessions must be a boolean"}, status=400
                )
            cfg.dashboard.restore_sessions = val
        try:
            if "restore_window_minutes" in body:
                cfg.dashboard.restore_window_minutes = max(
                    0, min(1440, int(body["restore_window_minutes"]))
                )
        except (TypeError, ValueError):
            _sel().log_tool_invocation(
                session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
            )
            return web.json_response(
                {"error": "restore_window_minutes must be an integer"}, status=400
            )
        if "merge_queued_messages" in body:
            val = body["merge_queued_messages"]
            if not isinstance(val, bool):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response(
                    {"error": "merge_queued_messages must be a boolean"}, status=400
                )
            cfg.dashboard.merge_queued_messages = val
        if "widget_density" in body:
            val = body["widget_density"]
            if val not in ("more", "less"):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response(
                    {"error": "widget_density must be 'more' or 'less'"}, status=400
                )
            cfg.dashboard.widget_density = val
        if "quick_send" in body:
            val = body["quick_send"]
            if not isinstance(val, bool):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response(
                    {"error": "quick_send must be a boolean"}, status=400
                )
            cfg.dashboard.quick_send = val
        if "session_grid" in body:
            val = body["session_grid"]
            if not isinstance(val, bool):
                _sel().log_tool_invocation(
                    session_key="dashboard", tool_name="dashboard_config_write", outcome="failure"
                )
                return web.json_response(
                    {"error": "session_grid must be a boolean"}, status=400
                )
            cfg.dashboard.session_grid = val
        cfg.save()
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="dashboard_config_write", outcome="success"
        )
        return web.json_response({"ok": True})
    _sel().log_tool_invocation(
        session_key="dashboard", tool_name="dashboard_config_read", outcome="success"
    )
    return web.json_response(
        {
            "restore_sessions": cfg.dashboard.restore_sessions,
            "restore_window_minutes": cfg.dashboard.restore_window_minutes,
            "merge_queued_messages": cfg.dashboard.merge_queued_messages,
            "widget_density": cfg.dashboard.widget_density,
            "quick_send": cfg.dashboard.quick_send,
            "session_grid": cfg.dashboard.session_grid,
        }
    )
