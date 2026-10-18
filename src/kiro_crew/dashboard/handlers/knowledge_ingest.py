"""Ingesting a document the caller sends: text an agent fetched for a source
(``POST /api/knowledge/sources/{id}/ingest-text``), an uploaded file or archive, and
``GET /api/knowledge/jobs/{id}``, the job row a caller polls.

Served through :mod:`kiro_crew.dashboard.handlers.knowledge`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
import uuid
import zipfile
from functools import partial
from pathlib import Path

from aiohttp import web

from kiro_crew.dashboard import part_stream
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.handlers.files import _ZIP_CONTAINER_EXTS, _content_matches_ext
from kiro_crew.dashboard.handlers.knowledge_access import (
    _audited_write,
    _pipeline,
    _require_knowledge_owner,
    _sel_log,
    _store,
)
from kiro_crew.dashboard.handlers.knowledge_sync import (
    _add_source_unique,
    _finalize_sync_status,
    _hand_off_under_gate,
    _set_sync_status,
    _source_row,
)
from kiro_crew.dashboard.upload_limits import bytes_to_mb_figure, knowledge_upload_max_bytes
from kiro_crew.knowledge.ingestion import ImportChunkBudgetError, _redact
from kiro_crew.zip_vet import ZipInventoryRejected, vet_zip_inventory

logger = logging.getLogger("kiro_crew.dashboard.handlers.knowledge")


async def ingest_text(request: web.Request) -> web.Response:
    """POST /api/knowledge/sources/{id}/ingest-text -- agent submits fetched text."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.source.ingest_text")
    if owner_denied is not None:
        return owner_denied
    source_id = request.match_info["id"]
    store = _store(request)
    pipeline = _pipeline(request)
    if not pipeline:
        return web.json_response({"error": "pipeline not configured"}, status=503)
    # The gate is held from the source lookup through the ingest: the orphan
    # sweep reclaims an itemless source with a terminal status, and the body
    # read below is an await between the two, so the whole span is one hold.
    async with pipeline.ingestion_in_flight():
        source = await asyncio.to_thread(_source_row, store, source_id)
        if not source:
            return web.json_response({"error": "source not found"}, status=404)
        body, body_err = await read_bounded_json(request, max_bytes=None)
        if body_err is not None:
            return body_err
        assert body is not None  # read_bounded_json returns (dict, None) on success
        text = body.get("text", "")
        if not text:
            return web.json_response({"error": "no text provided"}, status=400)
        redacted = _redact(text)
        text = redacted if redacted is not None else text
        name = body.get("name", source["name"])
        namespace = body.get("namespace", "default")
        # Write to temp file and ingest
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".md", prefix="agent_sync_")
        try:
            tmp.write(text.encode())
            tmp.close()
            job_id = await pipeline.ingest_file(
                tmp.name, original_name=name, namespace=namespace, source_id=source_id
            )
            # Update source status. INVARIANT: the 'synced' write and the
            # source.ingest_text audit ride in ONE worker take (_audited_write),
            # with no await between them -- that is what makes the audit
            # un-skippable by a cancellation. Do not split them or move one
            # off-loop on its own.
            await _audited_write(
                partial(_set_sync_status, store, source_id, "synced"),
                event="source.ingest_text",
                fields={"source_id": source_id, "name": name},
            )
            return web.json_response({"ok": True, "job_id": job_id})
        except ImportChunkBudgetError as exc:
            # The cross-file import budget deferred this ingest. Surface the reasoned
            # refusal (429, not a generic 500) so the caller learns it is a transient
            # budget deferral it can retry, not a server fault. Nothing was written.
            return web.json_response(
                {"error": str(exc), "code": "import_budget_exceeded"}, status=429
            )
        except BaseException as exc:
            # ``except BaseException`` so a cancel is seen and re-raised (task
            # semantics), but this handler writes NO terminal status: it holds
            # no claim, so a row reading 'syncing' here belongs to the sync
            # task that requested this text, and that task's own finalizer
            # clears it (a shutdown cancels every ``_bg_tasks`` member, this
            # handler's caller included). Stamping 'error' from here would
            # steal a live sibling claim and admit a concurrent replacement
            # ingest. Non-cancel failures keep the existing 500 body.
            is_cancel = isinstance(exc, asyncio.CancelledError)
            if is_cancel:
                logger.debug("Agent ingest_text cancelled for source %s", source_id)
                raise
            logger.exception("Agent ingest_text failed for source %s", source_id)
            return web.json_response({"error": "internal server error"}, status=500)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


# ---------- Ingestion ----------


# The per-file ingest ceiling is ``upload_limits.knowledge_upload_max_bytes``
# (read per request): the smaller of ``dashboard.upload_max_mb`` and
# ``knowledge.max_ingest_file_mb``, the same figure the Knowledge page shows. An
# ingest upload streams to a staged file, so the ceiling bounds disk, not memory.
# Decompression-bomb bounds for zip-container uploads (.docx/.xlsx/.pptx/...).
# A valid PK signature passes the magic-byte gate but the archive can still be
# a bomb whose members expand unbounded once a parser (python-docx) opens it
# (CWE-770). Bound the declared aggregate uncompressed size and member count
# from the central directory before any parser touches the file.
_MAX_INGEST_ARCHIVE_UNCOMPRESSED = 200 * 1024 * 1024  # 200 MB uncompressed total
_MAX_INGEST_ARCHIVE_MEMBERS = 10000


def _inspect_zip_archive(path: str) -> str | None:
    """Bound a zip-container's member count + declared aggregate uncompressed
    size using central-directory metadata only (no extraction).

    Returns a short rejection reason, or ``None`` if within limits. This does
    synchronous zip I/O, so callers MUST run it off the event loop (via
    ``asyncio.to_thread``) — a large/hostile central directory would otherwise
    stall the gateway loop and heartbeat.

    The member cap runs TWICE, deliberately. The shared vet (kiro_crew.zip_vet)
    reads the archive tail first, so the declared central-directory size — the
    field ZipFile's construction loop actually reads and allocates from — is
    bounded BEFORE ZipFile exists. The infolist() pass below then bounds the
    aggregate declared expansion, which only the parsed records can answer. The
    rejection reasons are unchanged: an archive that the preflight refuses would
    have been refused by the count check anyway, just after the allocation.
    """
    try:
        vet_zip_inventory(path, max_members=_MAX_INGEST_ARCHIVE_MEMBERS)
    except ZipInventoryRejected as exc:
        # Same reasons this function already returns, so the API error body and
        # the SEL outcome do not change shape: a tail we cannot parse is a bad
        # archive, an over-cap inventory is too many members.
        if exc.reason in ("missing_eocd", "truncated_eocd", "unreadable"):
            return "bad_archive"
        return "too_many_members"
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > _MAX_INGEST_ARCHIVE_MEMBERS:
                return "too_many_members"
            uncompressed = 0
            for zi in infos:
                uncompressed += zi.file_size
                if uncompressed > _MAX_INGEST_ARCHIVE_UNCOMPRESSED:
                    return "uncompressed_too_large"
    except zipfile.BadZipFile:
        return "bad_archive"
    return None


async def ingest_file(request: web.Request) -> web.Response:
    """POST /api/knowledge/ingest -- multipart file upload."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.ingest")
    if owner_denied is not None:
        return owner_denied
    pipeline = _pipeline(request)
    if not pipeline:
        return web.json_response({"error": "ingestion pipeline not configured"}, status=503)

    namespace = request.query.get("namespace", "default")
    reader = await request.multipart()
    field = await reader.next()
    if not field or not hasattr(field, "read_chunk") or field.name != "file":  # type: ignore[union-attr]
        return web.json_response({"error": "missing 'file' field"}, status=400)

    filename = getattr(field, "filename", None) or "upload"
    suffix = Path(filename).suffix
    ext = suffix.lower()
    staged = Path(tempfile.gettempdir()) / f"kn_{uuid.uuid4().hex}{suffix}"
    max_ingest_bytes = await asyncio.to_thread(knowledge_upload_max_bytes)
    try:
        # The signature gate (CWE-434) and the byte ceiling are both enforced by
        # the shared streaming path, which judges the leading bytes while they
        # are still in memory, so rejected content never reaches the filesystem.
        # Cleanup on cancellation is the helper's, not this function's -- see
        # part_stream's docstring.
        await part_stream.stream_part_to_file(
            field,  # type: ignore[arg-type]
            staged,
            max_bytes=max_ingest_bytes,
            accepts=lambda head: _content_matches_ext(ext, head),
        )
    except part_stream.PartTooLarge:
        return web.json_response(
            {"error": f"file too large (max {bytes_to_mb_figure(max_ingest_bytes)} MB)"},
            status=413,
        )
    except part_stream.PartContentMismatch:
        _sel_log("ingest", filename=filename, outcome="rejected")
        return web.json_response(
            {"error": f"file content does not match its type: {ext}"}, status=400
        )

    # Bound before the try so the handler's own failure path can reclaim it even
    # if the reservation below never happened.
    budget_token: int | None = None
    try:
        # Decompression-bomb guard (CWE-770): a valid-signature OOXML/zip can
        # still be a bomb whose members expand unbounded once python-docx / the
        # zip parser opens it. Bound the declared member count and aggregate
        # uncompressed size from the central directory (metadata only, no
        # extraction) BEFORE the file reaches a parser; reject a breach or a
        # corrupt/lying archive. Run off the event loop so a hostile central
        # directory can't stall the gateway loop/heartbeat.
        if ext in _ZIP_CONTAINER_EXTS:
            reason = await asyncio.to_thread(_inspect_zip_archive, str(staged))
            if reason is not None:
                staged.unlink(missing_ok=True)
                _sel_log("ingest", filename=filename, outcome="rejected", reason=reason)
                return web.json_response(
                    {"error": f"{ext} archive rejected ({reason})"}, status=400
                )

        # Admission BEFORE acceptance. This route answers 'processing' and ingests
        # in the background, and the staged temp file is the only server-side copy
        # -- the finally below unlinks it -- so a refusal discovered after the
        # response would discard a file the client was told had been accepted.
        # Reserving here makes 429 the answer to an exhausted window, the same one
        # ingest_text gives, and the token is handed to ingest_file so admission
        # cannot be lost between the check and the work.
        try:
            budget_token = await pipeline.reserve_import_budget()
        except ImportChunkBudgetError as exc:
            staged.unlink(missing_ok=True)
            _sel_log("ingest", filename=filename, outcome="deferred")
            return web.json_response(
                {"error": str(exc), "code": "import_budget_exceeded"}, status=429
            )

        # Create source record immediately so it appears in the UI
        store = _store(request)
        uri = f"upload://{filename}"

        # Run extraction in background so response returns immediately. For a
        # created row the stamp lives here rather than in the handler so a
        # disconnect cannot commit 'syncing' and then fail to schedule the work
        # that clears it. A plain stamp, not a claim: an upload has no
        # single-flight rule, and refusing the second one would drop a file the
        # user just handed over.
        async def _bg_ingest(tmp_path: str, src_id: str, gate_taken: asyncio.Event) -> None:
            # The task holds the ingestion gate from its own re-read through the
            # ingest, so the deferred orphan sweep never reads the row's items,
            # entities and mentions half-written. The handler keeps its own
            # hold until `gate_taken` fires, so the two holds overlap and the
            # sweep never sees the row unprotected. The re-read guards the one
            # way the row can be gone by now -- a delete that landed between
            # the response and this task -- and then the ingest is skipped:
            # nothing is re-created behind a delete, and the client already
            # holds an id that the delete answered for.
            try:
                async with pipeline.ingestion_in_flight():
                    gate_taken.set()
                    # Off-loop: the store hands out one connection per thread.
                    if await asyncio.to_thread(_source_row, store, src_id) is None:
                        logger.info("Upload: source %s removed before ingest; skipping", src_id)
                        # ingest_file's finally is what returns the admission
                        # otherwise; on this path nothing else will.
                        pipeline.release_import_budget(budget_token)
                        return
                    # A progress marker, never a precondition. The staged temp file is
                    # this upload's only server-side copy and the finally below unlinks
                    # it, so a contended writer lock on a cosmetic status write must
                    # not reach the except arm and take the file with it -- the client
                    # was already told 'processing'. ingest_file stamps the terminal
                    # status itself on both its paths (ingestion.py:1128 / 1144), so a
                    # skipped stamp costs a UI hint and heals on its own.
                    stamp = asyncio.ensure_future(
                        asyncio.to_thread(_set_sync_status, store, src_id, "syncing")
                    )
                    try:
                        await asyncio.shield(stamp)
                    except asyncio.CancelledError:
                        # The stamp's worker outlives a cancel and can commit
                        # 'syncing' AFTER the outer arm's CAS finalize ran,
                        # which would re-strand the row. Drain it, then
                        # re-raise so the outer arm finalizes a stamp that has
                        # actually landed -- the same shield-and-drain the
                        # claim takes use.
                        with contextlib.suppress(BaseException):
                            await stamp
                        raise
                    except Exception:
                        # The row id, not the filename: an upload's name is
                        # client-supplied and can carry a secret, and the row is what
                        # a reader needs to correlate a status write that did not land.
                        logger.exception(
                            "Could not stamp 'syncing' for source %s; ingesting anyway", src_id
                        )
                    await pipeline.ingest_file(
                        tmp_path,
                        original_name=filename,
                        namespace=namespace,
                        source_id=src_id,
                        # Admission was settled above, so this call must not enter the
                        # budget again -- including when the reservation returned None
                        # because the budget is disabled, which is the default.
                        count_toward_import_budget=False,
                        import_budget_token=budget_token,
                    )
            except BaseException as exc:
                # No dedicated ImportChunkBudgetError branch here, and none is
                # reachable from the front door: admission was reserved above, so
                # an exhausted window answered 429 before this task existed and
                # this call cannot be refused for budget. What remains is a
                # genuine failure, for which 'error' is the honest terminal state
                # -- an upload's only copy is the staged temp file the finally
                # unlinks, and an upload:// source has no re-fetchable URI, so
                # unlike the local_file / agent-url paths there is nothing to
                # retry from and 'pending' would promise one.
                # ``except BaseException`` for the reason _ingest_local_file_task
                # documents: a cancel must not strand the row at 'syncing'.
                is_cancel = isinstance(exc, asyncio.CancelledError)
                if is_cancel:
                    logger.debug("Background ingestion cancelled for %s", src_id)
                else:
                    logger.exception("Background ingestion failed for %s", filename)
                # ('syncing', 'pending'): a cancel can land before this path's
                # best-effort 'syncing' stamp -- during the source re-read --
                # while a freshly inserted row still reads the INSERT default
                # 'pending'. A committed 'synced' is still never overwritten.
                # The re-read above sits between reserving the admission and
                # handing it to ingest_file, so a failure there leaves the
                # reservation open and its placeholder stranded in the window
                # for the process lifetime. Release is a no-op once ingest_file
                # has taken the token (its own finally settles or releases it),
                # so this is safe on every path through the try -- the cancel
                # path included. First, before the status write: that write
                # must not skip the reclaim.
                pipeline.release_import_budget(budget_token)
                if is_cancel:
                    await _finalize_sync_status(
                        store, src_id, "error", from_statuses=("syncing", "pending")
                    )
                    raise
                # Unconditional, NOT the CAS: this path's 'syncing' stamp is
                # best-effort, so a failed stamp co-occurring with a failed
                # ingest would leave the CAS nothing to move and the row
                # holding a stale prior status over a deleted upload.
                await asyncio.to_thread(_set_sync_status, store, src_id, "error")
            finally:
                Path(tmp_path).unlink(missing_ok=True)

        # The gate is held from the lookup, through the re-used row's 'syncing'
        # stamp, until the task holds its own gate: an itemless row in a
        # terminal status from an earlier upload of this name is what the
        # sweep reclaims, and the id goes back to the client in this response.
        async with pipeline.ingestion_in_flight():
            # One take: the UNIQUE uri decides, so a repeated upload of the same
            # filename cannot race two inserts. A row this call CREATES is
            # stamped by nothing here: the INSERT's own status keeps it out of
            # the orphan sweep until its ingest ends, and a disconnect cannot
            # commit a 'syncing' the scheduled work then never clears.
            source_id, created = await asyncio.to_thread(
                _add_source_unique,
                store,
                name=filename,
                source_type="local_file",
                uri=uri,
                properties={},
            )
            if not created:
                # Marking a re-used row owed an ingest keeps the id the client
                # receives the row the ingest fills; a disconnect in between
                # leaves a pre-existing row reading 'syncing', a hint the next
                # upload of the name clears.
                # A hint, never a precondition: this write sits inside the outer
                # try, whose except unlinks the staged file -- the upload's only
                # copy -- so a contended writer lock here must not turn a
                # re-upload into a 500 with nothing ingested. The gate is held
                # across the gap, and the task re-stamps under its own hold.
                try:
                    await asyncio.to_thread(_set_sync_status, store, source_id, "syncing")
                except Exception:
                    logger.exception(
                        "Could not stamp 'syncing' for re-used source %s; ingesting anyway",
                        source_id,
                    )
            staged_path = str(staged)
            await _hand_off_under_gate(
                request,
                pipeline,
                lambda gate_taken: _bg_ingest(staged_path, source_id, gate_taken),
            )

        _sel_log("ingest", filename=filename)
        return web.json_response({"source_id": source_id, "status": "processing"})
    except Exception:
        logger.exception("Ingestion failed for %s", filename)
        # Reclaim the admission if the failure landed between reserving it and
        # handing it to ingest_file; otherwise the placeholder would sit in the
        # window for its duration and refuse imports that should pass. Once the
        # background task exists, ingest_file's finally owns the token.
        pipeline.release_import_budget(budget_token)
        staged.unlink(missing_ok=True)
        return web.json_response({"error": "internal server error"}, status=500)


async def get_job(request: web.Request) -> web.Response:
    """GET /api/knowledge/jobs/{id}."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.job.read")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    row = await asyncio.to_thread(
        lambda: store.db.execute(
            "SELECT * FROM ingestion_jobs WHERE id = ?", (request.match_info["id"],)
        ).fetchone()
    )
    if not row:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(dict(row))
