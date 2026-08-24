"""Meeting audio upload — the browser hands over one recorded blob at stop.

``POST …/{id}/audio``  store one meeting recording for post-meeting transcription

The browser records the meeting with ``MediaRecorder`` (opus/webm) and uploads
the whole file here when the meeting stops, BEFORE calling ``…/{id}/stop``. The
stop hook (see :mod:`.meeting_lifecycle`) then transcribes the file with the
app's existing local-whisper batch path and deletes it — transcribe-then-delete
retention, because meeting audio is sensitive.

Nothing here transcribes: this route only validates and persists the blob. The
write goes to :func:`store.recording_path`, which passes through the same
containment barrier every other meeting path does, and runs off the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from http import HTTPStatus
from typing import Any

from aiohttp import web
from aiohttp.multipart import BodyPartReader

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.routes._common import (
    BadRequest,
    audit,
    data_root,
)

logger = logging.getLogger("kirocrew.app.meetings")

#: The multipart form field the frontend puts the recorded blob in.
_AUDIO_FIELD = "audio"
#: Read the streamed upload in bounded chunks so a client cannot force one huge
#: allocation; the running total is checked against the cap after every chunk.
_UPLOAD_CHUNK_BYTES = 1024 * 1024


def _meeting_id(request: web.Request) -> str:
    return store.safe_meeting_id(request.match_info.get("meeting_id", ""))


def _open_temp_recording(meeting_id: str, root: Any) -> tuple[Any, Any, Any]:
    """Create the meeting dir and open a per-request UNIQUE temp file for append.

    Returns ``(temp_path, final_path, file_handle)``. Both paths pass through
    :func:`store.recording_path` / :func:`store.meeting_dir` (the same containment
    barrier every meeting path does), so the temp name — ``recording.<uuid>.part``
    — cannot escape the data root. The uuid makes two concurrent uploads for the
    same meeting write to DISTINCT temp files, so neither corrupts the other; the
    winner of the final :func:`os.replace` is whichever finishes last, and the
    file it lands is always one client's whole upload, never an interleave.

    BLOCKING (directory creation + open); the caller offloads it.
    """
    final_path = store.recording_path(meeting_id, root)
    mdir = store.meeting_dir(meeting_id, root)
    mdir.mkdir(parents=True, exist_ok=True)
    temp_path = store.contain(
        mdir / f"{final_path.stem}.{uuid.uuid4().hex}.part",
        operation="meetings.recording_temp",
        root=root,
    )
    handle = temp_path.open("wb")
    return temp_path, final_path, handle


def _append_chunk(handle: Any, chunk: bytes) -> None:
    """Append one chunk to the open temp file. BLOCKING; the caller offloads it."""
    handle.write(chunk)


def _finalize_recording(handle: Any, temp_path: Any, final_path: Any) -> None:
    """Flush+close the temp file and atomically move it onto ``recording.webm``.

    BLOCKING; the caller offloads it. ``os.replace`` is atomic on the same
    filesystem, so a reader (the stop-hook transcription) never sees a partial
    file — it sees either the old recording or the complete new one.
    """
    handle.flush()
    os.fsync(handle.fileno())
    handle.close()
    os.replace(temp_path, final_path)


def _finalize_if_active(
    meeting_id: str, handle: Any, temp_path: Any, final_path: Any, root: Any
) -> bool:
    """Finalize the upload ONLY if the meeting still exists and is not ended (B2).

    BLOCKING; the caller offloads it. The check-and-``os.replace`` runs under
    ``store.meta_transaction()`` — the SAME lock ``handle_stop_meeting`` /
    ``handle_delete_meeting`` hold — so it is atomic against a concurrent stop:
    either this finalizes before the stop reads ``recording.is_file()`` (the stop
    then transcribes it), or the stop wins and this refuses. Without the recheck a
    stop landing in another tab AFTER this handler's initial not-ended check but
    BEFORE the replace would leave ``recording.webm`` on disk that the already-run
    stop hook never picks up — untranscribed, undeleted, sensitive audio.

    Returns ``True`` when the recording was finalized, ``False`` when the meeting
    was ended/deleted mid-upload (in which case the temp is discarded here).
    """
    with store.meta_transaction():
        meta = store.read_meeting_meta(meeting_id, root)
        if meta is None or str(meta.get("status") or "") == k.STATUS_ENDED:
            _discard_temp(handle, temp_path)
            return False
        _finalize_recording(handle, temp_path, final_path)
        return True


def _discard_temp(handle: Any, temp_path: Any) -> None:
    """Close and unlink the temp file, leaving no partial behind. Never raises.

    BLOCKING; the caller offloads it. Used on every non-success exit (over-cap,
    read error, empty upload) so an aborted upload leaves the meeting dir clean.
    """
    try:
        handle.close()
    except OSError:  # pragma: no cover — defensive
        pass
    try:
        temp_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:  # pragma: no cover — defensive
        logger.warning("meetings: could not unlink temp recording %s", temp_path, exc_info=True)


async def _stream_audio_to_temp(request: web.Request, handle: Any) -> int:
    """Stream the uploaded audio to the open temp handle, enforcing the size cap.

    Accepts a multipart field named ``audio`` (how the browser sends it) and
    falls back to a raw request body, so a simple ``fetch(blob)`` also works.
    Each chunk is written straight to the temp file off the event loop — nothing
    is buffered whole in memory — and the running total is checked after every
    chunk, so an over-cap upload is refused (413) without ever allocating the
    whole recording. Returns the byte count on success; raises :class:`BadRequest`
    on over-cap / missing-field / empty. The caller owns cleanup of the temp file
    on any exception.
    """
    cap = k.MAX_RECORDING_BYTES
    content_type = (request.headers.get("Content-Type") or "").lower()

    if content_type.startswith("multipart/"):
        reader = await request.multipart()
        field = await reader.next()
        while field is not None:
            # A part is either a leaf BodyPartReader (a form field, which has
            # ``name``/``read_chunk``) or a nested MultipartReader (which has
            # neither). Only the leaf can be our audio field; the isinstance guard
            # both skips a nested part and narrows the union for the type checker.
            if isinstance(field, BodyPartReader) and field.name == _AUDIO_FIELD:
                total = 0
                while True:
                    chunk = await field.read_chunk(_UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > cap:
                        raise BadRequest(
                            f"recording exceeds the {cap} byte limit",
                            status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                            code="recording_too_large",
                        )
                    await asyncio.to_thread(_append_chunk, handle, chunk)
                if total == 0:
                    raise BadRequest("empty recording upload", code="no_audio")
                return total
            field = await reader.next()
        raise BadRequest(f"multipart body has no '{_AUDIO_FIELD}' field", code="no_audio")

    # Raw body fallback. Refuse an oversized upload up front on Content-Length,
    # then read defensively in chunks in case the header understated the size.
    if request.content_length is not None and request.content_length > cap:
        raise BadRequest(
            f"recording exceeds the {cap} byte limit",
            status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            code="recording_too_large",
        )
    total = 0
    while True:
        chunk = await request.content.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise BadRequest(
                f"recording exceeds the {cap} byte limit",
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                code="recording_too_large",
            )
        await asyncio.to_thread(_append_chunk, handle, chunk)
    if total == 0:
        raise BadRequest("empty recording upload", code="no_audio")
    return total


async def handle_upload_recording(request: web.Request) -> web.Response:
    """Store one uploaded meeting recording for later batch transcription.

    404 ``meeting_not_found`` when the meeting has no metadata on disk;
    409 ``meeting_ended`` when it is already ended (its transcription has run or
    is running, so a late upload would be transcribed by nothing); 413
    ``recording_too_large`` past the size cap. On success ``{ok, bytes}``.

    The upload is streamed straight to a per-request unique temp file
    (``recording.<uuid>.part``) with the size cap enforced during the stream, then
    atomically ``os.replace``'d onto ``recording.webm`` on success. Nothing is
    buffered whole in memory, and an aborted/over-cap upload leaves no partial
    behind — two concurrent uploads write to distinct temp files and cannot
    corrupt each other's bytes.
    """
    meeting_id = _meeting_id(request)
    root = data_root(request)

    meta = await asyncio.to_thread(store.read_meeting_meta, meeting_id, root)
    if meta is None:
        return web.json_response(
            {"error": "meeting not found", "code": "meeting_not_found"}, status=404
        )
    if str(meta.get("status") or "") == k.STATUS_ENDED:
        audit("meetings.recording", meeting_id, outcome="denied", error="meeting ended")
        return web.json_response(
            {"error": "meeting already ended", "code": "meeting_ended"}, status=409
        )

    temp_path, final_path, handle = await asyncio.to_thread(_open_temp_recording, meeting_id, root)
    try:
        written = await _stream_audio_to_temp(request, handle)
        # B2: a stop/delete can land in another tab WHILE this upload streams. If
        # it did, the stop hook already checked recording.is_file() and found
        # nothing (the temp is still a .part), so finalizing now would leave
        # untranscribed audio that nothing will ever pick up. Re-check under the
        # meta lock immediately before the atomic os.replace, and refuse to
        # finalize an ended/deleted meeting — discard the temp instead.
        finalized = await asyncio.to_thread(
            _finalize_if_active, meeting_id, handle, temp_path, final_path, root
        )
        if not finalized:
            audit(
                "meetings.recording", meeting_id, outcome="denied", error="meeting ended mid-upload"
            )
            return web.json_response(
                {"error": "meeting already ended", "code": "meeting_ended"}, status=409
            )
    except BaseException:
        # Over-cap, read error, disconnect — leave no partial in the meeting dir.
        await asyncio.to_thread(_discard_temp, handle, temp_path)
        raise
    audit("meetings.recording", meeting_id, outcome="ok")
    return web.json_response({"ok": True, "bytes": written})
