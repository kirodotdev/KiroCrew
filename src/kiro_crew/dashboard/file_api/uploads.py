"""Upload intake: ``POST /api/upload/file`` (composer attachments) and ``POST /api/directory-upload`` (Files panel), their per-type content checks and the media streaming path."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import ntpath
import os
import re
import stat as _stat_mod
import uuid
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from aiohttp.multipart import BodyPartReader

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _ALLOWED_AUDIO_EXT,
        _ALLOWED_DOC_EXT,
        _ALLOWED_IMAGE_EXT,
        _ALLOWED_TEXT_EXT,
        _ALLOWED_VIDEO_EXT,
        _CONTROL_CHARS_RE,
        _HEIF_BRANDS,
        _MAGIC_PREFIXES,
        _MAX_UPLOAD_FILES,
        _MAX_VIDEO_UPLOAD_BYTES,
        _MEDIA_EXT_MIME,
        _MEDIA_MAGIC,
        _RASTER_EXT_MIME,
        _RASTER_MIME_EXT,
        _UPLOAD_DIR,
        _VIDEO_HINT_EXT,
        _ZIP_CONTAINER_EXTS,
        OWNER_ONLY_FILE_MODE,
        SNIFF_BYTES,
        WINDOWS_DEVICE_STEMS,
        _PathProbeBusy,
        _probe_busy_response,
        _run_path_probe,
        _sel,
        _validate_dashboard_path,
        data_home,
        ensure_directory,
        is_owner_only_target,
        logger,
        part_stream,
        pinned_fs,
        platform_compat,
        sniff_raster_mime,
        upload_max_bytes,
    )


def _upload_dir() -> Path:
    """Uploads directory, resolved against the live data home."""
    return _UPLOAD_DIR if _UPLOAD_DIR is not None else data_home() / "uploads"


def _write_file_restricted(path: Path, data: bytes) -> None:
    """Write file with owner-only permissions (0o600)."""
    fd = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _log_upload_diagnostic(dest: Path, data: bytearray, ext: str, safe_name: str) -> None:
    """Log the received bytes' sha256 against the bytes that landed on disk.

    Pins a later "uploaded .docx is corrupted" report to the upload pipeline or
    to post-upload tampering. Blocking by design -- callers run it through
    ``asyncio.to_thread``. ``data`` is hashed in place and the file is hashed in
    chunks, so no second whole-file copy is held in memory. A failure is logged
    and never breaks the upload.
    """
    try:
        sent_sha = hashlib.sha256(data).hexdigest()
        disk_hash = hashlib.sha256()
        disk_size = 0
        head = b""
        with dest.open("rb") as fh:
            while chunk := fh.read(1024 * 1024):
                if not head:
                    head = chunk[:4]
                disk_hash.update(chunk)
                disk_size += len(chunk)
        disk_sha = disk_hash.hexdigest()
        is_zip = (
            zipfile.is_zipfile(str(dest))
            if ext in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".zip"}
            else None
        )
        logger.info(
            "upload.file diagnostic: name=%s ext=%s sent_size=%d disk_size=%d "
            "sent_sha256=%s disk_sha256=%s match=%s magic=%s is_zipfile=%s",
            safe_name,
            ext,
            len(data),
            disk_size,
            sent_sha,
            disk_sha,
            sent_sha == disk_sha,
            head.hex(),
            is_zip,
        )
    except Exception:
        logger.exception("upload.file diagnostic failed for %s", safe_name)


def _content_matches_ext(ext: str, data: bytes) -> bool:
    """Best-effort magic-byte check that ``data`` matches the claimed ``ext``.

    Returns False only when the signature is KNOWN and does not match, so an
    attacker can't store arbitrary bytes (e.g. an HTML/script payload) under an
    allowed binary extension (CWE-434). Unknown / text extensions (and ``.svg``)
    return True — there is no reliable signature — and stay gated by the
    extension allowlist alone.
    """
    if ext in _ZIP_CONTAINER_EXTS:
        # OOXML / ODF / zip all begin with a local-file-header, empty-archive,
        # or spanned-archive PK signature.
        return data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
    expected_media = _MEDIA_EXT_MIME.get(ext)
    if expected_media is not None:
        # Reuses the read path's container sniffer so the upload boundary and
        # /api/file-stream agree on what each signature means. ``data`` may be
        # just the leading chunk here — every signature involved lives in the
        # first 12 bytes, so a header is sufficient and a whole-file read is
        # never needed.
        return _sniff_media_type(data[:SNIFF_BYTES]) == expected_media
    expected = _RASTER_EXT_MIME.get(ext)
    if expected is not None:
        return sniff_raster_mime(data[:SNIFF_BYTES]) == expected
    prefixes = _MAGIC_PREFIXES.get(ext)
    if prefixes is None:
        return True  # text / svg / unknown — nothing to enforce
    return any(data.startswith(p) for p in prefixes)


def _resolve_raster_ext(ext: str, data: bytes) -> str | None:
    """The extension a raster upload declared as *ext* is stored under.

    Returns *ext* when the leading bytes match it, the sniffed type's canonical
    extension when they are a DIFFERENT accepted raster, and ``None`` when they
    are no raster at all. The relabel exists because browsers keep the URL's
    extension on "Save image as" while the body is whatever the server sent
    (a ``.jpeg`` that is really WebP is the everyday case), and a photo is a
    photo whichever suffix it wears. Security is unchanged: the bytes still
    have to be a raster the allowlist accepts, so the CWE-434 property --
    no HTML or script stored under an image extension -- holds; only the label
    is corrected instead of refused.
    """
    expected = _RASTER_EXT_MIME.get(ext)
    if expected is None:
        return None
    sniffed = sniff_raster_mime(data[:SNIFF_BYTES])
    if sniffed is None:
        return None
    if sniffed == expected:
        return ext
    return _RASTER_MIME_EXT[sniffed]


def _content_mismatch_message(ext: str, data: bytes) -> str:
    """User-facing sentence for a content-signature refusal.

    Names the remedy for the same reason the video branch does: telling the
    user their file "does not match its type" says what is wrong without
    saying what to do about it, and the fix (convert or re-export) is not
    guessable from the sentence.
    """
    if ext in _RASTER_EXT_MIME:
        accepted = ", ".join(sorted(_RASTER_EXT_MIME))
        if data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
            return (
                f"This {ext} file is really a HEIC/AVIF photo — convert it to "
                f"one of: {accepted} and upload again"
            )
        return f"This file is not really a {ext} image — re-export it as one of: {accepted}"
    return f"File content does not match its type: {ext}"


async def _stream_media_part(
    part: BodyPartReader,
    dest: Path,
    *,
    max_bytes: int,
    accepted_exts: set[str],
    media_name: str,
) -> tuple[int, tuple[str, str, str] | None]:
    """Stream a media *part* to *dest*, gating on its container signature.

    Returns ``(bytes_written, None)`` on success, or ``(bytes_written,
    (audit_reason, refusal_kind, user_message))`` on refusal. The caller maps
    the refusal kind to constant response codes and statuses, preserving the
    endpoint's statically readable error contract.

    :func:`~kiro_crew.dashboard.part_stream.stream_part_to_file` owns the temp
    through a synchronous context manager. A cancellable coroutine cannot own a
    file safely, so this function only translates the helper's exceptions.
    """
    ext = dest.suffix.lower()
    try:
        total = await part_stream.stream_part_to_file(
            part,
            dest,
            max_bytes=max_bytes,
            accepts=lambda head: _content_matches_ext(ext, head),
        )
    except part_stream.PartTooLarge as too_large:
        cap_mb = max_bytes // 1024 // 1024
        return too_large.total, (
            f"too_large:{too_large.total}",
            "too_large",
            f"{media_name.title()} too large (max {cap_mb}MB)",
        )
    except part_stream.PartContentMismatch:
        accepted = ", ".join(sorted(accepted_exts))
        return 0, (
            f"content_signature_mismatch:{ext}",
            "content_mismatch",
            f"This file is not really a {ext} — re-export it as one of: {accepted}",
        )
    return total, None


async def api_upload_file(request: web.Request) -> web.Response:
    """POST /api/upload/file — cross-platform multipart file upload.

    Accepts multipart form data with one or more 'file' fields.
    Saves files to the data home's uploads/ and returns server-side paths
    that ACP's _send_prompt() can detect for image inlining.
    """

    upload_dir = _upload_dir()
    ensure_directory(upload_dir)  # 0700 in the data home: uploads are user files
    # Read once per request, off the loop: a config load stats and may parse
    # files. ``dashboard.upload_max_mb``; video keeps _MAX_VIDEO_UPLOAD_BYTES.
    max_upload_bytes = await asyncio.to_thread(upload_max_bytes)
    max_upload_mb = max_upload_bytes // 1024 // 1024
    reader = await request.multipart()
    paths: list[str] = []
    allowed = (
        _ALLOWED_IMAGE_EXT
        | _ALLOWED_TEXT_EXT
        | _ALLOWED_DOC_EXT
        | _ALLOWED_VIDEO_EXT
        | _ALLOWED_AUDIO_EXT
    )
    caller = request.get("user", "dashboard")

    async def _cleanup(*also: Path) -> None:
        """Remove this request's files, plus *also*, off the serving loop.

        The ONE cleanup entry point for this handler, and a coroutine so it
        cannot be called the blocking way by accident. Every refusal and error
        path in a 20-file request may unlink up to 20 paths (a video among them
        up to 512 MB), and `Path.unlink` is a synchronous syscall: on a slow or
        network filesystem doing that inline stalls chat and heartbeat for the
        whole gateway. It also absorbs the destination itself via *also*, so no
        site pairs a bare ``dest.unlink()`` with a cleanup call and none can
        drift back to unlinking on the loop.
        """
        targets = [*paths, *(str(p) for p in also)]

        def _rm() -> None:
            for p in targets:
                Path(p).unlink(missing_ok=True)

        await asyncio.to_thread(_rm)

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
                await _cleanup()
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
                await _cleanup()
                _sel().log_api_access(
                    caller=caller,
                    operation="upload.file",
                    outcome="rejected",
                    source="dashboard",
                    resources=f"file:{fname} reason:unsupported_type:{ext}",
                )
                detail = f"Unsupported file type: {ext}"
                if ext in _VIDEO_HINT_EXT:
                    # Name the way out. A browser plays several containers this
                    # boundary refuses, so the bare refusal reads as "no video
                    # support" when the remedy is a re-encode.
                    accepted = ", ".join(sorted(_ALLOWED_VIDEO_EXT))
                    detail = f"{detail} — accepted video containers: {accepted}"
                return web.json_response(
                    {"error": detail, "code": "unsupported_file_type"},
                    status=400,
                )
            # UUID prefix guarantees uniqueness even within a single request.
            # Resolved before any byte is read because media streams straight
            # to this destination rather than buffering the part first.
            dest = upload_dir / f"{uuid.uuid4().hex}_{safe_name}"
            if not dest.resolve().is_relative_to(upload_dir.resolve()):
                await _cleanup()
                _sel().log_api_access(
                    caller=caller,
                    operation="upload.file",
                    outcome="rejected",
                    source="dashboard",
                    resources=f"file:{fname} reason:path_traversal",
                )
                return web.json_response({"error": "Invalid filename"}, status=400)
            if ext in _ALLOWED_VIDEO_EXT or ext in _ALLOWED_AUDIO_EXT:
                is_video = ext in _ALLOWED_VIDEO_EXT
                max_bytes = _MAX_VIDEO_UPLOAD_BYTES if is_video else max_upload_bytes
                accepted_exts = _ALLOWED_VIDEO_EXT if is_video else _ALLOWED_AUDIO_EXT
                media_name = "video" if is_video else "audio"
                # Media streams to an unpublished temp file because neither ACP
                # content blocks nor the player need the whole body in memory.
                # The shared helper checks the signature before its first write
                # and atomically publishes only a complete, accepted file.
                try:
                    written, refusal = await _stream_media_part(
                        part,
                        dest,
                        max_bytes=max_bytes,
                        accepted_exts=accepted_exts,
                        media_name=media_name,
                    )
                except (Exception, asyncio.CancelledError):
                    # CancelledError derives from BaseException. Name it so a
                    # disconnect or shutdown cannot leave a partial media file
                    # or an earlier sibling from this request behind.
                    await _cleanup(dest)
                    raise
                if refusal is not None:
                    await _cleanup(dest)
                    reason, refusal_kind, message = refusal
                    _sel().log_api_access(
                        caller=caller,
                        operation="upload.file",
                        outcome="rejected",
                        source="dashboard",
                        resources=f"file:{fname} reason:{reason}",
                    )
                    # Each response states constant status and code values so
                    # the endpoint's error contract remains statically readable.
                    if refusal_kind == "too_large":
                        if is_video:
                            return web.json_response(
                                {"error": message, "code": "video_too_large"},
                                status=413,
                            )
                        return web.json_response(
                            {"error": message, "code": "audio_too_large"},
                            status=413,
                        )
                    if is_video:
                        return web.json_response(
                            {"error": message, "code": "video_content_mismatch"},
                            status=400,
                        )
                    return web.json_response(
                        {"error": message, "code": "audio_content_mismatch"},
                        status=400,
                    )
                logger.info(
                    "upload.file %s: name=%s ext=%s size=%d",
                    media_name,
                    safe_name,
                    ext,
                    written,
                )
                paths.append(str(dest))
                continue
            # Read with size limit
            data = bytearray()
            while True:
                chunk = await part.read_chunk(8192)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > max_upload_bytes:
                    await _cleanup()
                    _sel().log_api_access(
                        caller=caller,
                        operation="upload.file",
                        outcome="rejected",
                        source="dashboard",
                        resources=f"file:{fname} reason:too_large:{len(data)}",
                    )
                    return web.json_response(
                        {
                            "error": (
                                f"File too large: {Path(fname).name} (max {max_upload_mb} MB). "
                                "To share it anyway, type @ followed by its location on the "
                                "computer running Kiro Crew, for example "
                                f"@~/Downloads/{Path(fname).name}."
                            ),
                            "code": "file_too_large",
                        },
                        status=413,
                    )
            # Content-signature gate (CWE-434): verify magic bytes match the
            # claimed extension BEFORE writing, so an allowed extension can't
            # smuggle arbitrary/binary content (e.g. a .png that is really HTML).
            # A raster whose bytes are a different ACCEPTED raster is relabelled
            # rather than refused: the content passed the same allowlist, only
            # the filename lied, and the stored suffix must tell the truth for
            # everything downstream that infers the mime from the path.
            if ext in _RASTER_EXT_MIME:
                true_ext = _resolve_raster_ext(ext, bytes(data))
                content_ok = true_ext is not None
                if true_ext is not None and true_ext != ext:
                    logger.info(
                        "upload.file relabel: name=%s declared=%s stored=%s",
                        safe_name,
                        ext,
                        true_ext,
                    )
                    ext = true_ext
                    dest = dest.with_suffix(true_ext)
            else:
                content_ok = _content_matches_ext(ext, bytes(data))
            if not content_ok:
                await _cleanup()
                _sel().log_api_access(
                    caller=caller,
                    operation="upload.file",
                    outcome="rejected",
                    source="dashboard",
                    resources=f"file:{fname} reason:content_signature_mismatch:{ext}",
                )
                return web.json_response(
                    {
                        "error": _content_mismatch_message(ext, bytes(data)),
                        "code": "content_mismatch",
                    },
                    status=400,
                )
            try:
                await asyncio.to_thread(_write_file_restricted, dest, bytes(data))
            except Exception:
                await _cleanup(dest)
                raise
            # Register the stored file for cleanup BEFORE the next await: a
            # request cancelled during the diagnostic below must remove it.
            paths.append(str(dest))
            # Diagnostic logging for binary uploads (docx/xlsx/pptx/odt/zip/
            # pdf, images) where any byte mismatch breaks the file; text
            # uploads aren't worth the I/O. Hashing and reading back a file
            # of up to the configured ceiling is seconds of CPU and disk, so
            # it runs on a worker thread, never on the gateway's event loop.
            if ext in _ALLOWED_DOC_EXT or ext in _ALLOWED_IMAGE_EXT:
                await asyncio.to_thread(_log_upload_diagnostic, dest, data, ext, safe_name)
    except (Exception, asyncio.CancelledError):
        # Same blind spot as the video branch above: a cancelled request (gateway
        # shutdown, client disconnect) raises CancelledError, which is NOT an
        # Exception, so without naming it every file this request already wrote
        # is orphaned in uploads/ with nothing left to reference or remove it.
        await _cleanup()
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


def _sniff_media_type(header: bytes) -> str | None:
    """Return the media Content-Type for ``header`` bytes, or None."""
    for offset, magic, mime in _MEDIA_MAGIC:
        if header[offset : offset + len(magic)] == magic:
            return mime
    # WAV: RIFF....WAVE compound signature (offset 8 discriminates from WebP)
    if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return "audio/wav"
    return None


async def api_directory_upload(request: web.Request) -> web.Response:
    """POST /api/directory-upload?dir=<path> — write an uploaded file into a
    directory already visible in the Files panel.

    Sibling of :func:`api_upload_file`, for a different destination: that
    endpoint always writes into the fixed chat-attachment scratch directory
    (``_upload_dir()``); this one writes into whatever directory the caller
    named -- a drag-and-drop target row, the full-pane drop overlay's root, or
    the "Upload files..." row-menu action -- which may be anywhere the
    dashboard's own path gate (``_validate_dashboard_path`` /
    ``is_sensitive_path``) permits, the same path gate ``/api/file-write``
    trusts for writing INTO the workspace. Like ``/api/file-write``, the
    handler opens with the dashboard owner gate
    (``require_owner_dashboard_request``) before any query parse, path probe or
    multipart read, so a non-owner holding a channel-created dashboard session
    cannot use it to plant files into the workspace.

    Reuses the existing admission gates rather than inventing new ones: the
    SAME extension allowlist and the SAME magic-byte content-signature check
    (``_content_matches_ext``)
    ``api_upload_file`` applies to a chat attachment. The per-file size cap
    here is ``dashboard.upload_max_mb`` (``upload_max_bytes``) for EVERY admitted file, video
    containers included: unlike ``api_upload_file``, which streams a video
    against the larger ``_MAX_VIDEO_UPLOAD_BYTES`` (512 MB), this endpoint
    applies no media-specific cap, so a video or audio file the shared
    allowlist admits is still refused past that limit (``file_too_large``). There is no
    credential/secret content scan here, deliberately matching
    ``api_upload_file``'s own boundary rather than the OUTBOUND ``file_send``
    gate's (``_gate_upload_file``'s credential scan): that scan protects
    content leaving the workspace toward a third party, which is the opposite
    trust direction from a user dropping their OWN file into their OWN
    workspace, so porting it here would be inventing a new gate rather than
    reusing an existing one. The ``file_delivery_consent`` module the handlers
    module already imports is not the missing piece either: its one class
    (``CLASS_OWNER_DASHBOARD``) gates handing workspace content OUT to the
    dashboard, so consulting it here would be asking permission for the
    opposite direction of travel.

    Unlike ``api_upload_file``'s aggressive ``[^\\w.\\-]`` character
    replacement (harmless there -- every scratch upload is UUID-prefixed and
    never user-facing by name), the filename here is the file's permanent,
    user-visible identity in their own workspace, so only the traversal-
    relevant part is stripped (``Path.name``) and the rest is kept as typed.
    An empty name, ``.``, ``..`` or a name with a control character is
    refused with ``invalid_filename`` on every platform. On a Windows host a
    name Windows cannot store (``<>:"\\|?*``, a trailing dot or space, or a
    device name such as ``CON``) is refused with the same 400 before any
    write; a Linux or macOS host stores such a name as typed, like every
    other tool that writes into the workspace.

    One file per request, by design: the frontend sends one multipart POST
    per dropped/picked file (mirroring the Knowledge Base upload), which is
    what makes per-file name-collision handling possible -- a batch endpoint
    would have to decide the collision policy for the whole batch at once.

    Uploads are create-only: an existing file at the destination name is
    never replaced. A name collision is always refused with
    ``code: "name_collision"`` and a 409, so the frontend can tell the user
    the file already exists.

    **Destination gating, not just directory gating.** ``dir`` alone passing
    ``_validate_dashboard_path`` is not enough: the crew-home ROOT is
    deliberately unfenced (only its leaves -- ``security_policy.json``,
    ``profiles/``, ``admission_policy.json``, ``computer_use.json`` -- are, per
    ``security/paths.py``), so ``dir=<crew-home>`` with a ``file`` part named
    ``security_policy.json`` would otherwise create the keystone where it is
    absent -- exactly the ``api_file_write`` sibling's own gate, which runs
    the FULL path (filename included). ``dest`` is therefore re-validated here
    the identical way, after the filename is known and before either the
    extension check or the body is read, so a sensitive destination is refused
    before any byte of the upload is even accepted off the wire.

    **Ancestor-swap and partial-write safety.** ``pinned_fs.pin_parent``
    walks ``target_dir`` -- already realpath-canonicalized by
    ``_validate_dashboard_path`` above, so this walks that RECORDED chain
    component-by-component with ``O_NOFOLLOW`` rather than re-resolving it (a
    fresh resolution here would just as happily follow a swap that landed after
    validation as the original lookup would). ``pin_parent`` on the parent, not
    ``open_dir_pinned`` on the target -- ``open_dir_pinned`` resolves its own
    parent chain internally, which is exactly the re-resolution this avoids. A
    single ``os.open(dest, ..., O_NOFOLLOW)`` only guards the FINAL component;
    the parents on the way to it are not covered by any flag on that one call.
    The file is then created by ``pinned_fs.write_new_file_pinned``: an
    ``O_EXCL`` create under the pinned descriptor, so an existing name is
    never replaced, and an identity-checked rollback when any write, flush or
    close fails. The bytes are written under the final name, so a reader can
    see a partially written file while the upload runs, and a failed write
    removes it. The existing-target lstat that decides
    collision/symlink/directory happens through the SAME pinned descriptor,
    so it is not a second by-name resolution the ancestor pin does not cover.

    Windows cannot pin a directory descriptor at all (no ``O_NOFOLLOW`` +
    ``dir_fd`` support -- see ``pinned_fs.supports_pinned_walk``), so the
    ancestor-swap protection above is NOT available in that FORM there -- but
    the property it buys IS still available, through a different primitive:
    ``platform_compat.pin_directory(target_dir)`` opens a handle without
    ``FILE_SHARE_DELETE``, and Windows refuses to rename or delete that
    directory OR ANY DIRECTORY ABOVE IT for as long as the handle stays open
    (see ``TestPinDirectory::test_a_pinned_directory_cannot_be_renamed_or_removed``
    in ``test_platform_compat_filesystem.py``). The open refuses a link or
    junction at ``target_dir``'s own name and pins a reparse directory that
    holds its own contents, such as a OneDrive cloud-files folder
    (``links_only=True``). The handle's kernel-reported final path
    must first match the already-validated target, so an ancestor swap that
    redirects the open itself is refused rather than trusted as a lock on the
    wrong directory. Held across the whole classify-then-publish below, the
    verified handle prevents a later ancestor swap, so the SAME by-name
    classification and publish the POSIX floor already uses become safe on
    Windows too -- ``platform_compat.is_link_or_junction`` still catches a
    planted symlink OR junction at the final name (which
    ``path.is_symlink()`` alone would miss), and the same ``O_EXCL`` create,
    write and close run by name, with the failed-write rollback going through
    ``pinned_fs.unlink_verified_by_name``. Where NEITHER
    ancestor-pinning primitive exists on a platform (unreachable in the
    current CI matrix: every one of Linux/macOS/Windows takes one branch or
    the other), the upload refuses outright with ``ancestor_pin_unsupported``
    rather than publish with no ancestor guard at all. This mirrors the
    documented residual in ``pinned_fs.write_file_pinned``: the
    everywhere-floor is "a planted NAME is never followed," not "no
    same-process race exists."
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    # Ahead of the query parse, the path probe and the multipart read. This route
    # creates files anywhere off the sensitive floor -- which includes
    # the steering documents, the skills and the MCP config whose own write routes are
    # owner-gated -- so leaving it open would hand a non-owner (a channel-allowlisted
    # user's dashboard session, created by send_dashboard_link) every file those gates
    # protect. It is the same gate, and the same ordering, api_file_write uses. Ahead
    # of the probe too, so whether a path exists is not a non-owner's to learn.
    owner_denied = await require_owner_dashboard_request(request, "upload.directory")
    if owner_denied is not None:
        return owner_denied

    caller = request.get("user", "dashboard")
    # NOT stripped: a POSIX directory name may legitimately end in a space, and
    # trimming one would resolve the upload to the space-less sibling instead
    # -- silently writing the file into a different directory than the client
    # named. An untrimmed value that is blank or bad is rejected below.
    raw_dir = request.query.get("dir", "")

    def _denied(code: str, message: str, status: int, *, resources: str = "") -> web.Response:
        _sel().log_api_access(
            caller=caller,
            operation="upload.directory",
            outcome="rejected",
            source="dashboard",
            resources=resources or f"reason:{code}",
        )
        # Dispatched through literal-status branches, EACH with its own
        # dict-literal body, rather than one `web.json_response(body,
        # status=status)` call: the error-code contract gate
        # (test/test_error_code_contract.py) statically reads a call's
        # `status=` keyword and its body argument, and either one arriving as
        # a variable is indistinguishable from a status/body that is
        # genuinely runtime-decided -- which the gate correctly cannot verify
        # and buckets as a violation-shaped finding on every call, new or
        # not. Every status this handler ever passes is one of these five
        # literals (400/403/409/413/501), so branching on the value keeps `_denied` as the one
        # place that assembles `code`/``message`` while still giving the gate
        # a literal status AND a literal, transparent, ``code``-bearing dict
        # it can verify at each site.
        if status == 400:
            return web.json_response({"error": message, "code": code}, status=400)
        if status == 403:
            return web.json_response({"error": message, "code": code}, status=403)
        if status == 409:
            return web.json_response({"error": message, "code": code}, status=409)
        if status == 413:
            return web.json_response({"error": message, "code": code}, status=413)
        if status == 501:
            return web.json_response({"error": message, "code": code}, status=501)
        raise ValueError(f"api_directory_upload._denied: unhandled status {status!r}")

    if not raw_dir:
        return _denied("missing_required_fields", "dir is required", 400)
    if not (request.content_type or "").startswith("multipart/"):
        return _denied("not_multipart", "expected multipart/form-data", 400)

    def _validate_directory(raw: str) -> str:
        """``_validate_dashboard_path`` plus the existence check, off the loop.

        Returns the validated directory, or ``""`` when it is refused or absent.

        Both are blocking filesystem calls -- ``validate_file_path`` resolves
        ``realpath`` and walks ``is_sensitive_path``'s candidate forms,
        ``os.path.isdir`` is a ``stat`` -- so they go through
        :func:`_run_path_probe`, this module's only sanctioned route for
        filesystem work on a caller-supplied path. Not ``asyncio.to_thread``:
        that is the loop's shared default executor, and a thread wedged in an
        uninterruptible ``stat`` on a dead mount never returns, so a caller
        naming one repeatedly would retire a shared worker per request until
        every unrelated ``to_thread`` user queued behind them. On the probe
        pool the same caller exhausts that pool and nothing else, and the
        refusal surfaces as a 503 rather than a silent stall.
        """
        validated = _validate_dashboard_path(raw)
        if not validated or not os.path.isdir(validated):
            return ""
        return validated

    try:
        target_dir = await _run_path_probe(_validate_directory, raw_dir)
    except _PathProbeBusy:
        return _probe_busy_response(
            resource=raw_dir, operation="upload.directory", caller=caller, source="dashboard"
        )
    if not target_dir:
        return _denied(
            "invalid_directory", "invalid or forbidden directory", 400, resources=raw_dir
        )

    reader = await request.multipart()
    part = await reader.next()
    if part is None or not isinstance(part, BodyPartReader) or part.name != "file":
        return _denied("missing_required_fields", "file is required", 400)

    filename = part.filename or ""
    # Strip any directory components the client sent (Path.name) -- the only
    # traversal-relevant sanitization a filename needs. It also means `dest`
    # below (`target_dir / safe_name`) is LEXICALLY always a direct child of
    # `target_dir`: safe_name never carries a `/` or a `..` segment, so there
    # is nothing left for a resolve()-based containment check to catch that
    # candidate_dest's own re-validation just below does not already cover.
    safe_name = Path(filename).name
    if (
        not safe_name
        or safe_name in (".", "..")
        or _CONTROL_CHARS_RE.search(safe_name)
        or (
            platform_compat.IS_WINDOWS
            and (
                any(ch in '<>:"\\|?*' for ch in safe_name)
                or safe_name.endswith((".", " "))
                or safe_name.split(".", 1)[0].lower() in WINDOWS_DEVICE_STEMS
            )
        )
    ):
        return _denied("invalid_filename", "invalid filename", 400, resources=filename)

    # Re-gate the FULL destination -- directory validation above is not
    # enough on its own: the crew-home ROOT is deliberately not itself
    # sensitive (only its leaves are -- security_policy.json, profiles/,
    # admission_policy.json, computer_use.json), so dir=<crew-home> passes
    # target_dir's own check and a filename naming one of those leaves would
    # otherwise reach the keystone unfenced. This mirrors api_file_write,
    # which runs its caller-supplied path (filename included) through this
    # SAME gate -- the destination gets no lighter a check here just because
    # part of it was already validated. Checked before the extension check
    # and before any byte of the body is read, so a sensitive destination is
    # refused as cheaply as every other early rejection in this handler.
    # Through the probe pool for the same reason target_dir's own validation is
    # above -- never the shared default executor.
    candidate_dest = os.path.join(target_dir, safe_name)
    try:
        dest_ok = await _run_path_probe(_validate_dashboard_path, candidate_dest)
    except _PathProbeBusy:
        return _probe_busy_response(
            resource=candidate_dest,
            operation="upload.directory",
            caller=caller,
            source="dashboard",
        )
    if not dest_ok:
        return _denied(
            "sensitive_destination",
            "destination path is not allowed",
            403,
            resources=candidate_dest,
        )

    ext = Path(safe_name).suffix.lower()
    allowed = (
        _ALLOWED_IMAGE_EXT
        | _ALLOWED_TEXT_EXT
        | _ALLOWED_DOC_EXT
        | _ALLOWED_VIDEO_EXT
        | _ALLOWED_AUDIO_EXT
    )
    if ext not in allowed:
        detail = f"Unsupported file type: {ext}"
        if ext in _VIDEO_HINT_EXT:
            accepted = ", ".join(sorted(_ALLOWED_VIDEO_EXT))
            detail = f"{detail} — accepted video containers: {accepted}"
        return _denied("unsupported_file_type", detail, 400, resources=filename)

    # ``dashboard.upload_max_mb``, read off the loop like api_upload_file.
    max_upload_bytes = await asyncio.to_thread(upload_max_bytes)
    data = bytearray()
    while True:
        chunk = await part.read_chunk(8192)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > max_upload_bytes:
            return _denied(
                "file_too_large",
                f"File too large (max {max_upload_bytes // 1024 // 1024}MB)",
                413,
                resources=filename,
            )
    raw = bytes(data)
    if not raw:
        return _denied("empty_upload", "no file content received", 400, resources=filename)
    if not _content_matches_ext(ext, raw):
        return _denied(
            "content_signature_mismatch",
            f"File content does not match its type: {ext}",
            400,
            resources=filename,
        )

    # No dest.resolve().is_relative_to(target_dir) check here: safe_name is
    # already traversal-free (see its own comment above), so dest is
    # LEXICALLY always a direct child of target_dir, and candidate_dest --
    # the identical path -- was already re-validated through
    # _validate_dashboard_path above. A resolve()-based containment check
    # would add nothing that check does not already cover, while adding two
    # more blocking realpath walks directly on the event loop.
    dest = Path(target_dir) / safe_name

    def _write() -> str | None:
        """Create ``dest`` holding ``raw``, ancestor-pinned, or leave nothing.

        See the handler's own docstring for why ``pin_parent`` walks the
        already-resolved ``target_dir`` rather than re-resolving it. The
        pinned branch needs what ``pinned_fs.write_new_file_pinned`` needs:
        a descriptor-relative open, plus the descriptor-relative stat and
        unlink its identity-checked rollback uses.

        The existing-target CLASSIFICATION below (missing/symlink/dir/file)
        happens entirely through stat-shaped checks (``pinned_fs.stat_at``,
        ``platform_compat.is_link_or_junction``, ``Path.is_dir``/``is_file``)
        BEFORE any open is attempted, never by opening the destination and
        branching on which exception comes back: opening an existing DIRECTORY
        with write flags is ``IsADirectoryError`` on POSIX but a different type
        on Windows, so classifying first keeps the 409 the same on both.

        The classification only describes the destination at the moment it
        ran. The create itself is ``O_EXCL``, so of two concurrent same-name
        uploads that both classified `missing`, exactly one creates the file
        and the other gets ``FileExistsError``, reported as `name_collision`.
        An existing `file` is a `name_collision` too.
        """
        pinned = pinned_fs.supports_pinned_walk() and {os.unlink, os.stat}.issubset(
            os.supports_dir_fd
        )
        dir_fd: int | None = None
        ancestor_guard_fd: int | None = None
        if pinned:
            try:
                dir_fd = pinned_fs.pin_parent(target_dir, what="upload target directory")
            except pinned_fs.PinnedPathRefusal:
                return "symlink_refused"
            except OSError:
                return "invalid_directory"
        elif platform_compat.IS_WINDOWS:
            # Windows has no O_NOFOLLOW + dir_fd combination to pin a PARENT
            # CHAIN with (pinned_fs.supports_pinned_walk() is False there),
            # so the by-name classification and publish below are, on their
            # own, exactly the by-name write an ancestor swapped for a
            # junction after _validate_dashboard_path could still redirect --
            # reaching a keystone leaf would be a governance-ceiling bypass,
            # which AGENTS.md treats as absolute. platform_compat.pin_directory
            # closes that window a different way: the handle it returns is
            # opened WITHOUT FILE_SHARE_DELETE, and for as long as it stays
            # open, Windows refuses to rename OR delete the directory it
            # names -- NOR ANY DIRECTORY ABOVE IT (its own docstring; pinned
            # by TestPinDirectory::
            # test_a_pinned_directory_cannot_be_renamed_or_removed in
            # test_platform_compat_filesystem.py). The handle's final path is compared
            # with the already-validated target before trusting that lock: an
            # ancestor swapped for a junction before the open can otherwise
            # redirect the open itself and make it pin the attacker's target.
            # Held across the whole classify-then-publish below, a verified
            # handle prevents any later ancestor swap. The open refuses a link
            # or junction already sitting at target_dir's own name and pins a
            # reparse directory that holds its own contents, such as a OneDrive
            # cloud-files folder. Any other refusal of the open (a sharing
            # violation, EACCES) propagates and answers upload_failed.
            try:
                ancestor_guard_fd = platform_compat.pin_directory(target_dir, links_only=True)
            except OSError as exc:
                if isinstance(exc, NotADirectoryError) or exc.errno == errno.ELOOP:
                    return "symlink_refused"
                raise
            pinned_target = pinned_fs.fd_real_path(ancestor_guard_fd)
            if pinned_target is None or ntpath.normcase(
                ntpath.normpath(pinned_target)
            ) != ntpath.normcase(ntpath.normpath(target_dir)):
                os.close(ancestor_guard_fd)
                ancestor_guard_fd = None
                return "symlink_refused"
        else:
            # Neither ancestor-pinning primitive exists on this platform: no
            # descriptor-relative walk (POSIX without O_NOFOLLOW/dir_fd
            # support) and no Windows handle-based anti-rename lock either.
            # Publishing by name here would be an unguarded ancestor-swap
            # window with no mitigation at all, so this refuses the upload
            # outright rather than risk a write landing wherever an ancestor
            # was swapped to -- including a keystone leaf. In the current CI
            # matrix (Linux, macOS, Windows) this branch is unreachable: all
            # three either satisfy `pinned` above or are Windows, which takes
            # the branch above instead.
            return "ancestor_pin_unsupported"
        try:
            # 'missing' | 'symlink' | 'dir' | 'file' for whatever currently
            # sits at the destination name. On POSIX this stats through the
            # pinned dir_fd, so no ancestor is resolved by name. On Windows it
            # checks by name, under the pin_directory handle that stays held
            # across this check and the create below, so no ancestor can be
            # renamed in between. platform_compat.is_link_or_junction catches
            # a planted symlink OR junction, where ``Path.is_symlink()`` alone
            # would miss a junction.
            if dir_fd is not None:
                st = pinned_fs.stat_at(dir_fd, safe_name)
                if st is None:
                    kind = "missing"
                elif _stat_mod.S_ISLNK(st.st_mode):
                    kind = "symlink"
                elif _stat_mod.S_ISDIR(st.st_mode):
                    kind = "dir"
                elif _stat_mod.S_ISREG(st.st_mode):
                    kind = "file"
                else:
                    # FIFO / socket / device planted at the name: refused the
                    # same way a symlink is, never opened as a plain file.
                    kind = "symlink"
            elif platform_compat.is_link_or_junction(dest):
                kind = "symlink"
            elif dest.is_dir():
                kind = "dir"
            elif dest.is_file():
                kind = "file"
            else:
                kind = "missing"

            if kind == "symlink":
                return "symlink_refused"
            if kind == "dir":
                return "is_a_directory"
            if kind == "file":
                return "name_collision"

            # Owner-only inside the data home, 0o666 under the umask elsewhere:
            # the default mode atomic_write gives a new file.
            mode = OWNER_ONLY_FILE_MODE if is_owner_only_target(dest) else 0o666
            try:
                if dir_fd is not None:
                    pinned_fs.write_new_file_pinned(dir_fd, safe_name, raw, mode=mode, fsync=True)
                    return None
                # Windows: no dir_fd to create relative to. The directory and
                # its ancestors are held by ancestor_guard_fd, so the by-name
                # O_EXCL create lands in the validated directory, and the
                # rollback removes the name only while it is still this file.
                fd = os.open(
                    dest,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_BINARY", 0),
                    mode,
                )
            except FileExistsError:
                return "name_collision"
            created: os.stat_result | None = None
            try:
                created = os.fstat(fd)
                written = 0
                while written < len(raw):
                    written += os.write(fd, raw[written:])
                os.fsync(fd)
                fd, open_fd = -1, fd
                os.close(open_fd)
            except BaseException:
                if fd >= 0:
                    if created is None:
                        # Re-ask the identity through the descriptor (a
                        # transient EIO usually answers the second time); with
                        # no identity the name stays, as in write_new_file_pinned.
                        try:
                            created = os.fstat(fd)
                        except OSError:
                            pass
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if created is not None:
                    pinned_fs.unlink_verified_by_name(
                        Path(target_dir),
                        safe_name,
                        (created.st_dev, created.st_ino),
                        links_only=True,
                    )
                raise
            return None
        finally:
            if dir_fd is not None:
                os.close(dir_fd)
            if ancestor_guard_fd is not None:
                os.close(ancestor_guard_fd)

    # transfer=True: this holds a worker for the length of the write, so it
    # belongs on the transfer pool rather than the probe pool a burst of these
    # would otherwise starve validation behind.
    try:
        outcome = await _run_path_probe(_write, transfer=True)
    except _PathProbeBusy:
        return _probe_busy_response(
            resource=str(dest), operation="upload.directory", caller=caller, source="dashboard"
        )
    except OSError as exc:
        # An unwritable destination, a full disk, or a Windows pin refused for a
        # reason other than a link; _write maps every other OSError to an outcome.
        logger.exception("upload.directory write failed for %s", dest)
        _sel().log_api_access(
            caller=caller,
            operation="upload.directory",
            outcome="failure",
            source="dashboard",
            resources=str(dest),
            error=exc.strerror or type(exc).__name__,
        )
        return web.json_response(
            {"error": "failed to write file", "code": "upload_failed"}, status=500
        )
    if outcome == "name_collision":
        return _denied(
            "name_collision",
            f"A file named {safe_name!r} already exists",
            409,
            resources=str(dest),
        )
    if outcome == "is_a_directory":
        return _denied(
            "is_a_directory",
            "a directory with that name already exists",
            409,
            resources=str(dest),
        )
    if outcome == "symlink_refused":
        return _denied(
            "symlink_refused", "refusing to write through a symlink", 403, resources=str(dest)
        )
    if outcome == "invalid_directory":
        return _denied(
            "invalid_directory", "invalid or forbidden directory", 400, resources=str(dest)
        )
    if outcome == "ancestor_pin_unsupported":
        return _denied(
            "ancestor_pin_unsupported",
            "this platform cannot guard the upload directory's ancestors "
            "against being swapped mid-request",
            501,
            resources=str(dest),
        )

    _sel().log_api_access(
        caller=caller,
        operation="upload.directory",
        outcome="success",
        source="dashboard",
        resources=str(dest),
    )
    return web.json_response({"ok": True, "path": str(dest), "name": safe_name})
