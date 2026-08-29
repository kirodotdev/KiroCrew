"""HTTP route handlers for the Writing Review builtin app.

Registered under ``/api/apps/writing-review``. Every handler is async
because scan jobs are dispatched as background tasks and the polling
endpoint reads a shared in-memory job store.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any

from aiohttp import BodyPartReader, web

from kiro_crew.apps.builtins.writing_review import (
    ReviewContext,
    run_scan,
)
from kiro_crew.apps.builtins.writing_review.backend import store
from kiro_crew.loop_lock import LoopBoundLock

logger = logging.getLogger("kirocrew.app.writing-review")


# --- In-memory job store ----------------------------------------------------

# Jobs are kept only in memory; they exist for the lifetime of one scan
# request plus the frontend's polling window (about 60s in practice).
# Persistent results land in the reviews store via :func:`store.save_review`.
_JOBS: dict[str, dict[str, Any]] = {}
# ``LoopBoundLock`` instead of a bare ``asyncio.Lock()`` at module scope: a
# bare lock binds to the first running loop it sees, and acquiring it from
# a second loop (pytest-asyncio workers, an in-process gateway restart)
# raises ``RuntimeError`` on 3.10+. ``LoopBoundLock`` keeps one inner lock
# per running loop, matching the pattern the code-review-sage app and the
# rest of the tree adopted in #4800.
_JOBS_LOCK = LoopBoundLock()
_MAX_JOB_AGE_S = 60 * 60

# Length of the hexadecimal uuid prefix used for both job IDs and the
# storage filename of browse-uploaded docs. Kept as a named constant so
# the regex in ``__init__.py::_UUID_STORAGE_PREFIX`` and the ``hex[:N]``
# slices below never fall out of sync. Changing this requires updating
# the regex in tandem — pinning the length here makes that dependency
# visible.
_STORAGE_UUID_HEX_LENGTH = 16

# Upper bound on the size of a pasted document body. Prevents an
# accidental or malicious paste from writing an unbounded blob into the
# uploads directory (and blocking the event loop while doing so). 2 MiB
# is roughly a 400k-token prompt for prose — still well over the
# reliably-scannable size for standard 200K-context models but not so
# generous that a runaway paste can spike memory. A request over this
# is rejected with a 413.
_MAX_DOC_TEXT_BYTES = 2 * 1024 * 1024

# Strong references to fire-and-forget background tasks. Without this the
# event loop only holds a weak reference to a task created via
# ``asyncio.create_task``; the garbage collector can then destroy the task
# before it finishes, silently dropping the state update it was carrying.
# Callbacks discard the reference once the task completes so the set does
# not grow without bound.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()
_MAX_PERSISTED_JOBS = 50


def _now() -> float:
    return time.time()


def _jobs_file_path() -> Path:
    """Return the on-disk path where the job dict is mirrored."""
    return Path(store.crew_home()) / "apps" / store.APP_NAME / "data" / "jobs.json"


async def _persist_jobs_locked() -> None:
    """Mirror ``_JOBS`` to ``jobs.json``. Must hold ``_JOBS_LOCK``.

    Serialisation is best-effort: a filesystem write failure is logged but
    never bubbles up to the caller. The scan continues in memory; the only
    consequence is that a gateway restart won't recover this specific job's
    state. Keeps the newest ``_MAX_PERSISTED_JOBS`` by ``updated_at`` so the
    file cannot grow without bound. Write is atomic (temp file + ``rename``)
    so a crash mid-write leaves ``jobs.json`` on its previous good contents
    rather than a truncated file that would defeat the startup recovery pass.

    The disk write itself runs inside :func:`asyncio.to_thread` so a slow
    filesystem does not stall the aiohttp event loop. The lock is held
    across the thread hop so successive calls remain serialised: the
    on-disk file reflects the last-committed ``_JOBS`` state at all times.
    """
    try:
        jobs_by_recency = sorted(
            _JOBS.values(),
            key=lambda job: job.get("updated_at", 0),
            reverse=True,
        )[:_MAX_PERSISTED_JOBS]
        target_path = _jobs_file_path()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        serialised_payload = json.dumps(jobs_by_recency, indent=2)
        temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
        await asyncio.to_thread(
            _atomic_write_json_snapshot,
            temp_path,
            target_path,
            serialised_payload,
        )
    except Exception:  # noqa: BLE001 - best-effort mirror
        logger.warning("failed to persist jobs.json", exc_info=True)


def _atomic_write_json_snapshot(temp_path: Path, target_path: Path, payload: str) -> None:
    """Write ``payload`` atomically to ``target_path`` via a sibling temp file.

    Split out so the disk-touching branch runs inside
    :func:`asyncio.to_thread` while the caller stays on the event loop.
    ``os.replace`` on POSIX is atomic within the same directory (same
    filesystem guaranteed for a sibling); a crash mid-write leaves the
    previous good copy intact.
    """
    temp_path.write_text(payload, encoding="utf-8")
    os.replace(temp_path, target_path)


def _load_jobs_from_disk() -> None:
    """Read ``jobs.json`` on gateway startup and rehydrate ``_JOBS``.

    Any job still marked ``running`` at load time is downgraded to
    ``interrupted``: its background ``asyncio`` task died with the previous
    process and cannot be resumed. The frontend surfaces ``interrupted`` as a
    distinct terminal state so users know their prior scan didn't finish and
    can decide whether to re-run.

    Mirrors ``code_review_sage/backend/routes.py:_load_runs`` verbatim in
    intent; different data shape, same recovery discipline.
    """
    global _JOBS
    try:
        target_path = _jobs_file_path()
        if not target_path.is_file():
            return
        raw = json.loads(target_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return
        rehydrated: dict[str, dict[str, Any]] = {}
        for job_record in raw:
            if not isinstance(job_record, dict):
                continue
            job_id = str(job_record.get("id") or "")
            if not job_id:
                continue
            if job_record.get("status") == "running":
                # A running record with phase="done" is the signature of a
                # late on_phase("done") write racing the completion write.
                # The scan itself finished; the review store already carries
                # the persisted record. Recover that specific shape as
                # ``done`` so the frontend clears its poll spinner cleanly.
                # All other ``running`` records are genuinely mid-scan
                # (their background task died with the previous gateway
                # process and cannot be resumed) and downgrade to
                # ``interrupted``.
                if job_record.get("phase") == "done":
                    job_record["status"] = "done"
                    job_record["error"] = None
                else:
                    job_record["status"] = "interrupted"
                    job_record["error"] = "Interrupted by a gateway restart -- re-run the review."
            rehydrated[job_id] = job_record
        _JOBS = rehydrated
    except Exception:  # noqa: BLE001 - defensive; jobs.json is best-effort
        logger.warning("failed to load jobs.json on startup", exc_info=True)


async def _record_job_state(
    job_id: str,
    *,
    status: str,
    phase: str,
    detail: dict[str, Any] | None = None,
    review_id: str | None = None,
    error: str | None = None,
    doc_name: str | None = None,
) -> None:
    """Atomically update the state of a single job, pruning stale entries.

    Persists ``_JOBS`` to ``jobs.json`` after every write so a browser
    hard-refresh sees the current job state. ``doc_name`` is preserved
    across state transitions -- passed once when the job is created and
    carried on subsequent updates without needing to be re-supplied.

    Monotonic terminal invariant: once a job's status is ``done``, ``failed``,
    or ``interrupted``, this function refuses any subsequent write that would
    downgrade it back to ``running``. Rationale: fire-and-forget ``on_phase``
    updates from ``run_scan`` are queued via ``asyncio.create_task`` and can
    execute AFTER the driver's completion write, clobbering the finished job
    record with a stale ``running`` state. ``run_scan`` no longer emits an
    ``on_phase("done")`` update (the callback stops at the last scanner
    phase), and this guard is the belt-and-braces layer that also covers any
    future progress callback we add: a callback cannot regress the invariant.
    """
    async with _JOBS_LOCK:
        previous = _JOBS.get(job_id) or {}
        previous_status = previous.get("status")
        if previous_status in _TERMINAL_JOB_STATUSES and status == "running":
            logger.warning(
                "refusing to downgrade job %s from %s to running (late "
                "state write after completion)",
                job_id,
                previous_status,
            )
            return
        resolved_doc_name = doc_name if doc_name is not None else previous.get("doc_name")
        _JOBS[job_id] = {
            "id": job_id,
            "status": status,
            "phase": phase,
            "detail": detail or {},
            "review_id": review_id,
            "error": error,
            "doc_name": resolved_doc_name,
            "updated_at": _now(),
        }
        _prune_stale_jobs_locked()
        await _persist_jobs_locked()


# Terminal job statuses -- once a job's status reaches one of these, later
# writes cannot re-open it. Named as a frozenset so a future addition (e.g.
# "cancelled") is a single-line change and so callers can import + assert on
# the vocabulary without duplicating the list.
_TERMINAL_JOB_STATUSES: frozenset[str] = frozenset({"done", "failed", "interrupted"})


def _prune_stale_jobs_locked() -> None:
    """Drop jobs older than ``_MAX_JOB_AGE_S`` (must be called with the lock held)."""
    cutoff_timestamp = _now() - _MAX_JOB_AGE_S
    stale_job_ids = [
        job_id for job_id, job in _JOBS.items() if job.get("updated_at", 0) < cutoff_timestamp
    ]
    for job_id in stale_job_ids:
        _JOBS.pop(job_id, None)


# --- Handlers ---------------------------------------------------------------


async def handle_scan(request: web.Request) -> web.Response:
    """POST /scan -- accept a doc, launch a background scan, return a job_id."""
    try:
        body_payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)

    # Strip stray whitespace on every user-supplied string field. Paths pasted
    # in from terminals almost always arrive with a trailing space; without
    # trimming, ``Path(...).is_file()`` returns False and the scan silently
    # fails before it ever reaches the pool.
    submitted_path_raw = body_payload.get("doc_path")
    submitted_path = str(submitted_path_raw).strip() if submitted_path_raw else ""
    submitted_text = body_payload.get("doc_text") or ""
    if (
        isinstance(submitted_text, str)
        and len(submitted_text.encode("utf-8")) > _MAX_DOC_TEXT_BYTES
    ):
        # Cap the pasted-doc size before we allocate uploads-dir bytes.
        # 413 signals "the payload is too large" — the honest status code
        # for a request that would otherwise silently write megabytes to
        # disk and stall the event loop doing it.
        return web.json_response(
            {
                "error": f"doc_text exceeds maximum size ({_MAX_DOC_TEXT_BYTES} bytes)",
                "code": "doc_text_too_large",
            },
            status=413,
        )
    submitted_doc_name_raw = body_payload.get("doc_name")
    # The frontend's browse-file flow sends the original filename here so
    # the sidebar / review record can display something readable instead of
    # the ``{uuid}_pasted.md`` storage key. Sanitised on entry — the value
    # never becomes a path, but it can end up in filesystem components via
    # ``_stash_pasted_document``, so the same guardrail applies both places.
    submitted_doc_name = (
        _sanitize_doc_filename(str(submitted_doc_name_raw).strip())
        if submitted_doc_name_raw
        else ""
    )
    if not submitted_path and not submitted_text:
        return web.json_response(
            {"error": "doc_path or doc_text is required", "code": "doc_required"},
            status=400,
        )

    if submitted_text and not submitted_path:
        submitted_path = await _stash_pasted_document(
            submitted_text,
            original_filename=submitted_doc_name or None,
        )

    context_payload = body_payload.get("context") or {}
    review_context = ReviewContext(
        audience=str(context_payload.get("audience", "")).strip(),
        doc_type=str(context_payload.get("doc_type", "")).strip(),
        tone=str(context_payload.get("tone", "")).strip(),
        ask=str(context_payload.get("ask", "")).strip(),
        additional_context=[
            str(context_note_entry).strip()
            for context_note_entry in context_payload.get(
                "additional_context",
                # Backward compat: dev-machine review records saved BEFORE
                # the rename used the ``exceptions`` key. Read from that
                # if the new key is absent so pre-rename records reload
                # cleanly during manual testing.
                context_payload.get("exceptions", []),
            )
            if str(context_note_entry).strip()
        ],
    )
    scanner_toggles = body_payload.get("scanner_toggles")
    if scanner_toggles is not None and not isinstance(scanner_toggles, dict):
        return web.json_response(
            {"error": "scanner_toggles must be an object", "code": "scanner_toggles_invalid"},
            status=400,
        )

    job_id = uuid.uuid4().hex[:_STORAGE_UUID_HEX_LENGTH]
    # Derive a readable doc_name for the sidebar in-progress card. A user-
    # supplied ``doc_name`` (browse-uploaded doc) wins over the on-disk
    # basename, because the on-disk name is the uuid-prefixed storage key
    # and the user wants to see their own filename.
    if submitted_doc_name:
        initial_doc_name = submitted_doc_name
    elif submitted_path:
        initial_doc_name = Path(submitted_path).name
    else:
        initial_doc_name = "pasted document"
    await _record_job_state(job_id, status="running", phase="starting", doc_name=initial_doc_name)

    scan_job_task = asyncio.create_task(
        _run_scan_job(
            job_id=job_id,
            doc_path=submitted_path,
            context=review_context,
            scanner_toggles=scanner_toggles,
        )
    )
    # Strong-ref the task; ``asyncio.create_task``'s own reference is weak,
    # so without this the GC can destroy the running scan before it writes
    # the terminal state to the job record.
    _BACKGROUND_TASKS.add(scan_job_task)
    scan_job_task.add_done_callback(_BACKGROUND_TASKS.discard)
    return web.json_response({"job_id": job_id})


async def _stash_pasted_document(
    document_text: str, *, original_filename: str | None = None
) -> str:
    """Save pasted text to an uploads directory so the driver can read a file.

    The stored filename is ``{uuid}_{sanitized_original}`` when an
    ``original_filename`` is supplied (browse-uploaded docs — the frontend
    forwards the user's filename here), or the legacy ``{uuid}_pasted.md``
    default for callers that don't have one (raw paste, older callers).
    The uuid prefix (``_STORAGE_UUID_HEX_LENGTH`` hex chars) guarantees
    uniqueness so two browse-uploads of files that happen to share a name
    cannot clobber each other on disk.

    The disk write is routed through ``asyncio.to_thread`` so a large paste
    does not block the event loop while ``write_text`` runs. Callers must
    already have enforced ``_MAX_DOC_TEXT_BYTES``; this function trusts
    the size check.
    """
    uploads_dir = store.crew_home() / "apps" / store.APP_NAME / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    sanitized_suffix = (
        _sanitize_doc_filename(original_filename)
        if original_filename is not None
        else _FILENAME_FALLBACK
    )
    uuid_prefix = uuid.uuid4().hex[:_STORAGE_UUID_HEX_LENGTH]
    pasted_filename = f"{uuid_prefix}_{sanitized_suffix}"
    pasted_path = uploads_dir / pasted_filename
    await asyncio.to_thread(pasted_path.write_text, document_text, encoding="utf-8")
    return str(pasted_path)


async def _stash_uploaded_binary(raw_bytes: bytes, *, original_filename: str | None = None) -> str:
    """Save raw uploaded bytes under a sanitised filename in the uploads dir.

    Analogous to :func:`_stash_pasted_document` but writes raw bytes with
    ``write_bytes`` instead of UTF-8 encoded text. The binary path exists so
    that ``.docx`` uploads (a ZIP bundle of XML) reach disk byte-identical.
    A text-decoded round trip re-encodes arbitrary bytes as UTF-8 and
    corrupts the archive; python-docx then cannot open it.
    """
    uploads_dir = store.crew_home() / "apps" / store.APP_NAME / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    sanitized_suffix = (
        _sanitize_doc_filename(original_filename)
        if original_filename is not None
        else _FILENAME_FALLBACK
    )
    uuid_prefix = uuid.uuid4().hex[:_STORAGE_UUID_HEX_LENGTH]
    stored_filename = f"{uuid_prefix}_{sanitized_suffix}"
    stored_path = uploads_dir / stored_filename
    # ``write_bytes`` on a Path is a plain wrapper around
    # ``open('wb').write(...)`` -- no encoding shenanigans, no UTF-8
    # round-trip. The bytes reach disk exactly as the browser sent
    # them, so python-docx can open the ZIP later without seeing a
    # corrupted archive.
    await asyncio.to_thread(stored_path.write_bytes, raw_bytes)
    return str(stored_path)


async def handle_upload(request: web.Request) -> web.Response:
    """Accept a multipart file upload and stash it byte-for-byte.

    Response body: ``{"doc_path": "<absolute path>", "doc_name": "<name>"}``.
    The frontend feeds these back into a subsequent ``POST /scan`` on
    the ``doc_path`` field; the scan flow is otherwise unchanged.

    Size cap matches ``_MAX_DOC_TEXT_BYTES`` (2 MiB) so an oversized
    upload cannot exhaust the uploads directory bytes before the
    scan request even arrives. Filename is sanitised via the same
    helper the paste path uses.
    """
    if not request.content_type or not request.content_type.startswith("multipart/"):
        return web.json_response(
            {"error": "expected multipart/form-data", "code": "not_multipart"},
            status=400,
        )
    multipart_reader = await request.multipart()
    uploaded_field: BodyPartReader | None = None
    while True:
        next_part = await multipart_reader.next()
        if next_part is None:
            break
        # A nested ``MultipartReader`` (multipart-inside-multipart) has no
        # ``name`` / ``filename`` / ``read_chunk`` and is not the file upload
        # this endpoint accepts — skip past it. Narrowing to ``BodyPartReader``
        # is what makes the field-name / drain / chunked-read below type-safe.
        if not isinstance(next_part, BodyPartReader):
            continue
        if next_part.name == "file":
            uploaded_field = next_part
            break
        # Drain any part we don't care about; leaving it undrained
        # can block the reader.
        while await next_part.read_chunk():
            pass
    if uploaded_field is None:
        return web.json_response(
            {"error": "missing file field", "code": "missing_file_field"},
            status=400,
        )
    # Read the file body in chunks so a single 2 MiB read allocation
    # does not spike memory, and enforce the size cap as chunks arrive so
    # an oversized upload is rejected before the whole body lands in
    # memory. This is chunked reception with an in-flight size cap, not a
    # true stream to disk: the final buffered ``write_bytes`` is one
    # allocation once the cap is known to be satisfied.
    collected_bytes = bytearray()
    while True:
        chunk = await uploaded_field.read_chunk()
        if not chunk:
            break
        collected_bytes.extend(chunk)
        if len(collected_bytes) > _MAX_DOC_TEXT_BYTES:
            return web.json_response(
                {
                    "error": f"upload exceeds {_MAX_DOC_TEXT_BYTES} byte limit",
                    "code": "upload_too_large",
                },
                status=413,
            )
    if len(collected_bytes) == 0:
        return web.json_response(
            {"error": "uploaded file was empty", "code": "upload_empty"}, status=400
        )
    raw_filename = uploaded_field.filename or ""
    # Reject an empty filename outright: falling back to the pasted.md
    # default writes docx (ZIP) bytes under a ``.md`` suffix, which the
    # downstream extension router then hands to the markdown parser and
    # produces garbage sections. Browsers reliably send ``filename=``;
    # a missing one is either a malformed client or an attack surface,
    # neither of which the extension-routed parser can handle safely.
    if not raw_filename.strip():
        return web.json_response(
            {"error": "missing filename", "code": "missing_filename"}, status=400
        )
    stored_path = await _stash_uploaded_binary(
        bytes(collected_bytes), original_filename=raw_filename
    )
    stored_doc_name = Path(stored_path).name
    return web.json_response({"doc_path": stored_path, "doc_name": stored_doc_name})


# Filename hygiene for browse-uploaded docs. The browsed filename is a raw
# user-supplied string — never a path — and this sanitiser is the guardrail
# that keeps it from escaping the uploads directory or producing an
# unusable filename.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_CONSECUTIVE_UNDERSCORES = re.compile(r"_+")
_FILENAME_MAX_LENGTH = 128
_FILENAME_FALLBACK = "pasted.md"


def _sanitize_doc_filename(original_filename: str) -> str:
    """Return a safe filename derived from a user-supplied original name.

    Rules, in order:

    1. Take the basename after splitting on both POSIX and Windows path
       separators, so an ``original_filename`` of ``../../etc/passwd`` is
       reduced to ``passwd``. The uploads directory is the ceiling and
       cannot be escaped.
    2. Collapse any character outside ``[A-Za-z0-9._-]`` to ``_`` — this
       covers spaces, brackets, null bytes, and all ASCII / Unicode control
       characters. Consecutive underscores then collapse to one so a messy
       input like ``my doc (v2).md`` reads as ``my_doc_v2_.md``.
    3. Cap the total length at ``_FILENAME_MAX_LENGTH``, preserving the
       extension when a dot is present. The cap is well below the 255-byte
       POSIX component limit so the ``{uuid}_`` prefix has room.
    4. If the sanitised result is empty, dot-only, or underscore-only,
       fall back to ``_FILENAME_FALLBACK`` — the uploads directory must
       never contain a file whose name is just the uuid prefix, because
       downstream extension routing (``.docx`` vs ``.md``) would fail.
    """
    if not original_filename:
        return _FILENAME_FALLBACK
    # ``PureWindowsPath`` normalises both POSIX (forward slash) and Windows
    # (backslash) separators on every host OS, so browser-supplied filenames
    # from either platform yield the same basename. String slicing on a
    # single slash was rejected by the cross-platform-portability CI gate
    # because it silently drops Windows separators on POSIX hosts.
    basename = PureWindowsPath(original_filename).name
    if not basename:
        return _FILENAME_FALLBACK
    replaced_unsafe = _UNSAFE_FILENAME_CHARS.sub("_", basename)
    collapsed = _CONSECUTIVE_UNDERSCORES.sub("_", replaced_unsafe)
    truncated = _truncate_preserving_extension(collapsed, _FILENAME_MAX_LENGTH)
    if not truncated or set(truncated) <= {"_", ".", "-"}:
        return _FILENAME_FALLBACK
    return truncated


def _truncate_preserving_extension(filename: str, max_length: int) -> str:
    """Cap ``filename`` at ``max_length``, keeping the extension if present."""
    if len(filename) <= max_length:
        return filename
    stem, separator, extension = filename.rpartition(".")
    if separator and len(extension) < max_length - 1:
        stem_budget = max_length - len(extension) - 1
        return stem[:stem_budget] + "." + extension
    return filename[:max_length]


async def _run_scan_job(
    *,
    job_id: str,
    doc_path: str,
    context: ReviewContext,
    scanner_toggles: dict[str, bool] | None,
) -> None:
    """Background task: run the scan, save the review, update job status."""

    def on_phase(phase_name: str, phase_detail: dict[str, Any]) -> None:
        # Fire-and-forget the update on the running event loop. A strong
        # reference to the task is held in ``_BACKGROUND_TASKS`` for the
        # task's lifetime — asyncio only weak-refs a bare ``create_task``
        # return, and without our own strong ref the GC can destroy the
        # task before it completes, silently dropping the state update.
        # The done-callback discards the reference so the set stays bounded.
        try:
            phase_update_task = asyncio.get_running_loop().create_task(
                _record_job_state(job_id, status="running", phase=phase_name, detail=phase_detail)
            )
        except RuntimeError:  # pragma: no cover - loop closed during shutdown
            return
        _BACKGROUND_TASKS.add(phase_update_task)
        phase_update_task.add_done_callback(_BACKGROUND_TASKS.discard)

    try:
        scan_result = await run_scan(
            doc_path=doc_path,
            context=context,
            scanner_toggles=scanner_toggles,
            on_phase=on_phase,
            review_id=job_id,
        )
    except Exception as scan_exception:
        logger.exception("Scan job %s failed", job_id)
        await _record_job_state(
            job_id,
            status="failed",
            phase="error",
            error=str(scan_exception),
        )
        return

    review_id = await asyncio.to_thread(store.save_review, scan_result)
    await _record_job_state(
        job_id,
        status="done",
        phase="done",
        review_id=review_id,
        detail={"verdict": scan_result.verdict, "findings": len(scan_result.findings)},
    )


async def handle_job(request: web.Request) -> web.Response:
    """GET /jobs/{job_id} -- return the current status of a scan job."""
    requested_job_id = request.match_info["job_id"]
    async with _JOBS_LOCK:
        _prune_stale_jobs_locked()
        job_snapshot = _JOBS.get(requested_job_id)
    if job_snapshot is None:
        return web.json_response({"error": "job not found", "code": "job_not_found"}, status=404)
    return web.json_response(job_snapshot)


async def handle_list_jobs(request: web.Request) -> web.Response:
    """GET /jobs -- return every tracked job so the frontend can rehydrate.

    Optional ``?status=running`` narrows to in-flight jobs, which is what the
    provider queries on mount to restore the sidebar in-progress card. Any
    caller can pass a full listing to also inspect completed / interrupted
    jobs from a prior session.
    """
    status_filter = request.query.get("status")
    async with _JOBS_LOCK:
        _prune_stale_jobs_locked()
        await _persist_jobs_locked()  # keep disk in sync with the prune
        all_jobs = list(_JOBS.values())
    if status_filter:
        all_jobs = [job for job in all_jobs if job.get("status") == status_filter]
    all_jobs.sort(key=lambda job: job.get("updated_at", 0), reverse=True)
    return web.json_response({"jobs": all_jobs})


async def handle_list_reviews(_request: web.Request) -> web.Response:
    """GET /reviews -- summary list of every stored review, newest first."""
    summaries = await asyncio.to_thread(store.list_reviews)
    return web.json_response({"reviews": summaries})


async def handle_get_review(request: web.Request) -> web.Response:
    """GET /reviews/{review_id} -- full review record."""
    requested_review_id = request.match_info["review_id"]
    review_record = await asyncio.to_thread(store.load_review, requested_review_id)
    if review_record is None:
        return web.json_response(
            {"error": "review not found", "code": "review_not_found"}, status=404
        )
    return web.json_response(review_record)


async def handle_review_context(request: web.Request) -> web.Response:
    """GET /reviews/{review_id}/context -- full bundle for the discussion agent.

    Returns the review record + the raw document content + the absolute
    path to the scanner-briefs directory so the writing-review-reviewer agent can
    cold-load everything it needs on its first turn without a follow-up
    tool call.
    """
    from pathlib import Path as _Path

    requested_review_id = request.match_info["review_id"]
    review_record = await asyncio.to_thread(store.load_review, requested_review_id)
    if review_record is None:
        return web.json_response(
            {"error": "review not found", "code": "review_not_found"}, status=404
        )

    stored_doc_path = review_record.get("doc_path", "")
    document_content = ""
    if stored_doc_path:
        try:
            stored_doc_path_lower = stored_doc_path.lower()
            if stored_doc_path_lower.endswith(".docx"):
                # docx is a ZIP of XML -- ``read_text`` on the raw bytes
                # raises ``UnicodeDecodeError`` (not caught by the OSError
                # clause) and the endpoint 500s, leaving the frontend to
                # fall back to the compact ``review_id`` marker. Route
                # docx through the same extractor the scanners use so the
                # discussion agent sees the readable prose (headings +
                # body + table cells + VISUAL placeholders), not the ZIP.
                from kiro_crew.apps.builtins.writing_review import parse_doc

                extracted_sections = await asyncio.to_thread(parse_doc, stored_doc_path)
                rendered_pieces = []
                for extracted_section in extracted_sections:
                    if extracted_section.heading:
                        rendered_pieces.append(f"# {extracted_section.heading}")
                    if extracted_section.body:
                        rendered_pieces.append(extracted_section.body)
                document_content = "\n\n".join(rendered_pieces)
            else:
                document_content = await asyncio.to_thread(
                    _Path(stored_doc_path).read_text, encoding="utf-8"
                )
        except (
            FileNotFoundError,
            PermissionError,
            OSError,
            UnicodeDecodeError,
        ):
            # UnicodeDecodeError also caught here so any future binary
            # extension we forget to branch on degrades gracefully to
            # the same "unavailable" fallback rather than 500ing the
            # endpoint and starving the discussion agent of context.
            document_content = "(document no longer available at original path)"

    scanner_brief_dir = str(_Path(__file__).parent.parent / "scanners")
    return web.json_response(
        {
            "review": review_record,
            "document_content": document_content,
            "scanner_brief_dir": scanner_brief_dir,
        }
    )


async def handle_delete_review(request: web.Request) -> web.Response:
    """DELETE /reviews/{review_id} -- remove a review from disk."""
    requested_review_id = request.match_info["review_id"]
    was_deleted = await asyncio.to_thread(store.delete_review, requested_review_id)
    if not was_deleted:
        return web.json_response(
            {"error": "review not found", "code": "review_not_found"}, status=404
        )
    return web.json_response({"deleted": True})


async def handle_get_settings(_request: web.Request) -> web.Response:
    """GET /settings -- current user settings, defaulted if never saved."""
    settings_payload = await asyncio.to_thread(store.load_settings)
    return web.json_response(settings_payload)


async def handle_patch_settings(request: web.Request) -> web.Response:
    """PATCH /settings -- merge partial settings into the stored payload."""
    try:
        patch_payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(patch_payload, dict):
        return web.json_response(
            {"error": "settings must be an object", "code": "settings_body_invalid"},
            status=400,
        )
    updated_settings = await asyncio.to_thread(store.update_settings, patch_payload)
    return web.json_response(updated_settings)


# --- Registration -----------------------------------------------------------


ROUTE_PREFIX = "/api/apps/writing-review"


def register_routes(app: web.Application) -> None:
    """Register every writing-review route on the given aiohttp app.

    Also rehydrates ``_JOBS`` from ``jobs.json`` on the first registration,
    which is our gateway-startup hook: the module-level ``_JOBS`` dict is
    empty when the process boots, and this is the single place we know the
    dashboard is coming up.
    """
    _load_jobs_from_disk()
    app.router.add_post(f"{ROUTE_PREFIX}/scan", handle_scan)
    app.router.add_post(f"{ROUTE_PREFIX}/uploads", handle_upload)
    app.router.add_get(f"{ROUTE_PREFIX}/jobs", handle_list_jobs)
    app.router.add_get(f"{ROUTE_PREFIX}/jobs/{{job_id}}", handle_job)
    app.router.add_get(f"{ROUTE_PREFIX}/reviews", handle_list_reviews)
    app.router.add_get(f"{ROUTE_PREFIX}/reviews/{{review_id}}", handle_get_review)
    app.router.add_get(
        f"{ROUTE_PREFIX}/reviews/{{review_id}}/context",
        handle_review_context,
    )
    app.router.add_delete(f"{ROUTE_PREFIX}/reviews/{{review_id}}", handle_delete_review)
    app.router.add_get(f"{ROUTE_PREFIX}/settings", handle_get_settings)
    app.router.add_patch(f"{ROUTE_PREFIX}/settings", handle_patch_settings)


def _reset_jobs_for_tests() -> None:
    """Test helper: clear the in-memory job store between tests."""
    _JOBS.clear()
