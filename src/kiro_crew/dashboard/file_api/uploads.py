"""Composer upload intake: ``POST /api/upload/file``, its per-type content checks and the media streaming path."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import re
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
        _HEIF_BRANDS,
        _MAGIC_PREFIXES,
        _MAX_UPLOAD_BYTES,
        _MAX_UPLOAD_FILES,
        _MAX_VIDEO_UPLOAD_BYTES,
        _MEDIA_EXT_MIME,
        _MEDIA_MAGIC,
        _RASTER_EXT_MIME,
        _RASTER_MIME_EXT,
        _UPLOAD_DIR,
        _VIDEO_HINT_EXT,
        _ZIP_CONTAINER_EXTS,
        SNIFF_BYTES,
        _sel,
        data_home,
        ensure_directory,
        logger,
        part_stream,
        sniff_raster_mime,
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
        # os.write may accept only a prefix; a prefix left as the file would be
        # served as the whole upload.
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(errno.EIO, "write accepted no bytes")
            view = view[written:]
    finally:
        os.close(fd)


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
                max_bytes = _MAX_VIDEO_UPLOAD_BYTES if is_video else _MAX_UPLOAD_BYTES
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
                if len(data) > _MAX_UPLOAD_BYTES:
                    await _cleanup()
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
