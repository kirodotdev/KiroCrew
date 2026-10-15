"""The markdown panel's file routes: ``GET /api/file-read``, ``GET /api/file-raw``, ``POST /api/file-write`` and ``GET /api/file-diff``."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import subprocess
import tempfile
import urllib.parse
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _CRED_RUN_REVERSED_RE,
        _FILE_DIFF_MAX_BYTES,
        _FILE_READ_CAP,
        _MAX_UPLOAD_BYTES,
        _READ_PATH_EXTRA_MAGIC,
        _STREAM_HOLDBACK_JWT_MAX,
        _STREAM_HOLDBACK_MAX,
        SNIFF_BYTES,
        _file_write_blocking,
        _open_checked,
        _open_checked_file,
        _OpenDenied,
        _OpenRefusal,
        _owner_view_bypasses_credential_pass,
        _PathProbeBusy,
        _probe_busy_response,
        _probe_request_path,
        _read_request_path,
        _resolve_project_relative,
        _run_path_probe,
        _sel,
        is_sensitive_path,
        read_bounded_json,
        redact,
        redact_owner_view_via_context,
        require_owner_dashboard_request,
        sniff_raster_mime,
    )


def _read_safe_end(text: str, cap: int) -> int:
    """An end for *text*, a read cut off at its bound, that severs no token.

    A credential is a contiguous run of credential-class characters, so the
    trailing run is the only one the read's own end may sever. A run that
    starts at or past *cap* is dropped; nothing below the cap depends on it.
    A run crossing *cap* that holds a JWT header (``eyJ``) is dropped whole,
    whatever its length. One without it is cut a streaming holdback before the
    end, the same bound the streaming redactor holds an anchorless run to.
    """
    run = _CRED_RUN_REVERSED_RE.match(text[::-1])
    # A ``*`` pattern matches the empty string, so a match always exists.
    assert run is not None
    run_start = len(text) - run.end()
    if run_start >= cap or "eyJ" in text[run_start:]:
        return run_start
    return len(text) - _STREAM_HOLDBACK_MAX


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
            0 if request.method == "HEAD" else read_cap + _STREAM_HOLDBACK_JWT_MAX,
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
        # OWNER-VIEW seam: when the requester IS the dashboard owner, the owner's
        # credential-redaction switch applies to this read of their own disk
        # (``security.redaction_switch``). A non-owner dashboard user (a Slack
        # allow-listed ``!dashboard`` caller) gets the unconditional pass. The only
        # other opener in this module is ``api_file_diff``, which feeds the SAME
        # panel the ``original`` this buffer is compared against; the outbox
        # flagged-file check and the upload gates keep the unconditional ``redact``.
        owner_view = await _owner_view_bypasses_credential_pass(request)

        def redact_fn(text: str) -> str:
            return redact_owner_view_via_context(text) if owner_view else redact(text)

        # The read runs past the cap by the redactor's own longest-token bound,
        # so a credential straddling the cap is matched whole there. The body
        # keeps only what redacting the capped text and redacting the whole read
        # agree on: it holds no prefix the capped pass alone left unmatched, and
        # no raw text from past the cap that the shorter redacted output exposes.
        # A read that stops short of its bound holds the whole file. A full one
        # is first cut back to an end no token crosses (``_read_safe_end``), so
        # the whole-read pass is one batch call that never sees a severed token.
        as_written = content[:read_cap]
        content = redact_fn(as_written)
        if truncated:
            whole = outcome.content
            if len(whole) >= read_cap + _STREAM_HOLDBACK_JWT_MAX:
                whole = whole[: _read_safe_end(whole, read_cap)]
            content = os.path.commonprefix([content, redact_fn(whole)])
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
        if not as_written.startswith(content):
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
            session_key="dashboard",
            tool_name="file_raw",
            outcome=outcome,
            resources=res,
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


def _resolve_diff_path(raw: str) -> tuple[str, bool]:
    """Canonicalize a diff target; say whether it is a regular file.

    Blocking (``realpath`` then ``isfile``) -- callers run it on a worker thread.
    """
    path = os.path.realpath(os.path.expanduser(raw))
    return path, os.path.isfile(path)


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


async def api_file_diff(request: web.Request) -> web.Response:
    """GET /api/file-diff?path=... — returns git diff and HEAD content for a file."""
    owner_denied = await require_owner_dashboard_request(request, "file_diff")
    if owner_denied is not None:
        return owner_denied
    raw_path = request.query.get("path", "").strip()
    if not raw_path:
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="file_diff",
            outcome="allowed",
            resources="empty_path",
        )
        return web.json_response({"diff": "", "original": ""})
    # Off-loop: realpath then the isfile probe, on a caller-supplied path.
    try:
        raw_path, path_is_file = await _run_path_probe(_resolve_diff_path, raw_path)
    except _PathProbeBusy:
        return _probe_busy_response(
            resource=raw_path, operation="file_diff", caller=request.get("user", "dashboard")
        )
    if not path_is_file:
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="file_diff",
            outcome="allowed",
            resources=f"path={raw_path}",
            error="not_found",
        )
        return web.json_response({"diff": "", "original": ""})
    if is_sensitive_path(raw_path):
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="file_diff",
            outcome="denied",
            resources=raw_path,
            error="sensitive path",
        )
        return web.json_response({"error": "Access denied"}, status=403)

    dirpath = os.path.dirname(raw_path)

    # Disable textconv/filter drivers and fsmonitor to prevent code execution
    # via .gitattributes or .git/config in untrusted repos.
    _git = [
        "git",
        "-c",
        "diff.textconv=",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "core.fsmonitor=",
    ]
    _env = {**os.environ, "GIT_ATTR_NOSYSTEM": "1"}

    def _run() -> dict:
        try:
            subprocess.run(
                [*_git, "rev-parse", "--git-dir"],
                cwd=dirpath,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=True,
                env=_env,
            )
        except (
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
            FileNotFoundError,
            UnicodeDecodeError,
        ):
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
                cwd=dirpath,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                env=_env,
            ).stdout.strip()
            rel = os.path.relpath(raw_path, root)
            head = subprocess.run(
                # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args -- fixed git argv; the path is repo-relative and passed as one argument
                [*_git, "show", "--no-textconv", f"HEAD:{rel}"],
                cwd=dirpath,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                env=_env,
            )
            original = head.stdout if head.returncode == 0 else ""
            # Get diff
            r = subprocess.run(
                [*_git, "diff", "--no-textconv", "--no-ext-diff", "HEAD", "--", raw_path],
                cwd=dirpath,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                env=_env,
            )
            diff = r.stdout.strip() if r.returncode == 0 else ""
            if not diff:
                # Check for untracked file
                r2 = subprocess.run(
                    [*_git, "status", "--porcelain", "--", raw_path],
                    cwd=dirpath,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                    env=_env,
                )
                if r2.returncode == 0 and r2.stdout.strip().startswith("??"):
                    r3 = subprocess.run(
                        [
                            *_git,
                            "diff",
                            "--no-textconv",
                            "--no-ext-diff",
                            "--no-index",
                            "/dev/null",
                            raw_path,
                        ],
                        cwd=dirpath,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=10,
                        env=_env,
                    )
                    diff = r3.stdout if r3.stdout else ""
                    return {"diff": diff, "original": "", "status": "untracked", "_rel": rel}
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
            return {"diff": diff, "original": original, "status": status, "_rel": rel}
        except (
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
            FileNotFoundError,
            UnicodeDecodeError,
        ):
            return {"diff": "", "original": "", "status": "error"}

    def _diff_redacted_bodies(old: str, new: str, rel: str, *, new_file: bool, redact_fn) -> str:
        """Git's unified diff of two ALREADY-REDACTED bodies.

        A patch shows lines out of their surrounding text, which is what the
        redactor reads, so the pass runs on whole bodies and only masked text is
        diffed. The temp files hold masked text only, in a private dir outside
        any repo.
        """
        with tempfile.TemporaryDirectory(prefix="kc-file-diff-") as tmp:
            a = os.path.join(tmp, "a")
            b = os.path.join(tmp, "b")
            with open(a, "w", encoding="utf-8", newline="") as fh:
                fh.write(old)
            with open(b, "w", encoding="utf-8", newline="") as fh:
                fh.write(new)
            r = subprocess.run(
                [*_git, "diff", "--no-index", "--no-textconv", "--no-ext-diff", "--", a, b],
                cwd=tmp,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                env=_env,
            )
        # --no-index answers 1 for "differs"; anything else is a failure, and a
        # failure must not fall back to the raw patch.
        if r.returncode not in (0, 1):
            raise subprocess.CalledProcessError(r.returncode, "git diff --no-index")
        # "\n" only: splitlines() also cuts on form feed, U+2028 and other
        # characters that sit inside a patch line.
        lines = r.stdout.rstrip("\n").split("\n")
        first = next((i for i, ln in enumerate(lines) if ln.startswith("@@")), None)
        name = redact_fn(rel).replace("\n", " ")
        header = [f"diff --git a/{name} b/{name}"]
        if first is None:
            # No hunks: no change, or git judged the text binary.
            if not r.stdout.strip():
                return ""
            return "\n".join([*header, f"Binary files a/{name} and b/{name} differ"])
        header += ["--- /dev/null" if new_file else f"--- a/{name}", f"+++ b/{name}"]
        return "\n".join([*header, *lines[first:]])

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
        # masks. This runs in this worker thread rather than on the event loop
        # because the input is caller-sized.
        result = _run()
        rel = result.pop("_rel", None)

        def redact_fn(text: str) -> str:
            return redact_owner_view_via_context(text) if bypass else redact(text)

        original = result.get("original", "")
        raw_diff = result.get("diff", "")
        # Git's patch of the raw file is never served: it is only the verdict
        # that something changed. The served patch is the diff of the two
        # WHOLE bodies, each masked before diffing.
        result["diff"] = ""
        lines = raw_diff.split("\n") if raw_diff else []
        is_binary = not any(line.startswith("@@") for line in lines) and any(
            line.startswith("Binary files ") and line.endswith(" differ") for line in lines
        )
        result["original"] = redact_fn(original)
        if raw_diff and rel is not None:
            if is_binary:
                name = redact_fn(rel).replace("\n", " ")
                result["diff"] = (
                    f"diff --git a/{name} b/{name}\n" f"Binary files a/{name} and b/{name} differ"
                )
                return result
            try:
                # The same checked, no-follow open the file read uses, so a
                # path swapped after the first check is refused, not read.
                checked = _open_checked_file(
                    raw_path, tool_name="file_diff", log_open_failure=False
                )
                if isinstance(checked, _OpenDenied):
                    raise OSError(checked.code)
                with checked.file as fh:
                    data = fh.read(_FILE_DIFF_MAX_BYTES + 1)
                if len(data) > _FILE_DIFF_MAX_BYTES:
                    result["status"] = "error"
                    return result
                # Universal newlines, as git's text-mode stdout gives HEAD, so
                # a CRLF file does not diff as every line changed.
                current = (
                    data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
                )
                result["diff"] = _diff_redacted_bodies(
                    result["original"],
                    redact_fn(current),
                    rel,
                    new_file=result.get("status") == "untracked",
                    redact_fn=redact_fn,
                )
            except (OSError, subprocess.SubprocessError):
                # Fail closed: no patch at all beats a raw one.
                result["status"] = "error"
        return result

    result = await asyncio.to_thread(_run_redacted)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="file_diff",
        outcome="allowed",
        resources=f"path={raw_path}",
    )
    return web.json_response(result)
