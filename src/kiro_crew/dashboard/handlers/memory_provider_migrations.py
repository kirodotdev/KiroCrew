"""``/api/memory/provider`` moves, export and import.

| Route | Who may call |
|---|---|
| ``POST /migrations`` | the owner |
| ``GET /migrations``, ``GET /migrations/{id}`` | the owner |
| ``POST /migrations/{id}/resume``, ``POST /migrations/{id}/rollback`` | the owner |
| ``DELETE /migrations/{id}`` | the owner |
| ``GET /export`` | the owner |
| ``POST /import`` | the owner |

A move never switches memory and never deletes from Crew's own memory. The job logic
is :mod:`kiro_crew.memory_provider.migration`; the caller checks and audit records
are shared with :mod:`.memory_provider`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers import memory_provider as base
from kiro_crew.memory_provider import builtin, migration
from kiro_crew.memory_provider.contract import KINDS, MemoryProvider, ProviderError


def _notify(app: web.Application) -> migration.Notify:
    def push(job: migration.MigrationJob) -> None:
        try:
            app["state"].push_refresh("memory_provider")
        except Exception:  # noqa: BLE001 - a missed refresh costs a stale card only
            pass

    return push


def _job_body(job: migration.MigrationJob) -> dict[str, Any]:
    from kiro_crew.dashboard.handlers._shared import _redact_memory_field

    return _redact_memory_field(base._scrub_meta_keys(job.to_dict()))


def _job_or_404(job_id: str, caller: str) -> migration.MigrationJob | web.Response:
    job = migration.get_job(job_id)
    if job is None:
        return base._json_error("no such move", "not_found", 404)
    return job


def _parse_kinds(raw: object) -> list[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(k, str) and k in KINDS for k in raw):
        raise ValueError("kinds must be a list of contract kinds")
    return list(raw) or None


async def _endpoints(
    store: str, source_id: str, target_id: str
) -> tuple[MemoryProvider, MemoryProvider] | web.Response:
    _, source, error = await asyncio.to_thread(base._target, store, source_id)
    if source is None:
        return base._json_error(error, "provider_unavailable", 503)
    _, target, error = await asyncio.to_thread(base._target, store, target_id)
    if target is None:
        return base._json_error(error, "provider_unavailable", 503)
    for provider in (source, target):
        unprepared = await base._prepare_builtin(store, provider)
        if unprepared is not None:
            return unprepared
    return source, target


async def _into_builtin_refusal(target: MemoryProvider, store: str) -> web.Response | None:
    if target is builtin.BUILTIN and await asyncio.to_thread(base._builtin_dark, store):
        return base._json_error(
            "Switch back to Crew memory before moving memory into it.",
            base.CODE_PROVIDER_ACTIVE,
            409,
        )
    return None


async def api_memory_provider_migrations(request: web.Request) -> web.Response:
    """GET (list) / POST (plan or start) /api/memory/provider/migrations."""
    if request.method == "GET":
        caller = await base._caller(request, "migrations.list")
        jobs = await asyncio.to_thread(migration.list_jobs)
        return base._respond({"migrations": [j.to_record() for j in jobs]})
    caller = await base._caller(request, "migrations.create")
    from kiro_crew.dashboard.handlers._shared import read_bounded_json

    body, refusal = await read_bounded_json(request)
    if refusal is not None:
        return refusal
    assert body is not None
    dry_run = body.get("dry_run") is True
    source_id = str(body.get("from") or "")
    target_id = str(body.get("to") or "")
    if not source_id or not target_id or source_id == target_id:
        return base._json_error("from and to must name two different providers", "invalid_move")
    try:
        store = await asyncio.to_thread(base._store_name, body.get("store"))
        kinds = _parse_kinds(body.get("kinds"))
    except ValueError as exc:
        return base._json_error(str(exc), "invalid_move")
    resolved = await _endpoints(store, source_id, target_id)
    if isinstance(resolved, web.Response):
        return resolved
    source, target = resolved
    if not dry_run:
        refused = await _into_builtin_refusal(target, store)
        if refused is not None:
            return refused
        if migration.active_job_for(target_id, store or "default") is not None:
            return base._json_error(
                "A move into this provider is already running.", "move_running", 409
            )
    job = await asyncio.to_thread(
        lambda: migration.new_job(
            source=source_id,
            target=target_id,
            store=store,
            kinds=kinds,
            include_retired=body.get("include_retired") is True,
            preserve_timestamps=body.get("preserve_timestamps") is not False,
            dry_run=dry_run,
            caller=caller,
        )
    )
    base._audit(
        caller, "migrations.start", "allowed", resources=f"{job.id}:{source_id}>{target_id}"
    )
    migration.start(
        migration.run_job(
            job, source, target, base._scope(store, caller), notify=_notify(request.app)
        )
    )
    return web.json_response(_job_body(job), status=202)


async def api_memory_provider_migration(request: web.Request) -> web.Response:
    """GET (status) / DELETE (cancel) /api/memory/provider/migrations/{id}."""
    operation = "migrations.get" if request.method == "GET" else "migrations.cancel"
    caller = await base._caller(request, operation)
    found = await asyncio.to_thread(_job_or_404, request.match_info["id"], caller)
    if isinstance(found, web.Response):
        return found
    if request.method == "GET":
        return web.json_response(_job_body(found))
    job = await asyncio.to_thread(migration.cancel, found.id)
    base._audit(caller, "migrations.cancel", "allowed", resources=found.id)
    return web.json_response(_job_body(job or found))


_BUSY = "This move is already running or starting."


async def api_memory_provider_migration_resume(request: web.Request) -> web.Response:
    """POST /api/memory/provider/migrations/{id}/resume: continue from the checkpoint."""
    caller = await base._caller(request, "migrations.resume")
    found = await asyncio.to_thread(_job_or_404, request.match_info["id"], caller)
    if isinstance(found, web.Response):
        return found
    # One runner per job: reserved before any await, freed when the runner ends or
    # when this request refuses.
    if not migration.reserve(found.id):
        return base._json_error(_BUSY, "job_busy", 409)
    started: list[bool] = []
    try:
        return await _resume(request, caller, found, started)
    finally:
        if not started:
            migration.unreserve(found.id)


async def _resume(
    request: web.Request, caller: str, found: Any, started: list[bool]
) -> web.Response:
    if found.state not in migration.RESUMABLE_STATES or found.dry_run:
        return base._json_error("Only a paused or failed move can resume.", "not_resumable", 409)
    if found.from_file:
        return base._json_error(
            "A file import cannot resume; import the file again. Items already written "
            "are found by the provider's dedup.",
            "not_resumable",
            409,
        )
    store = "" if found.store == "default" else found.store
    resolved = await _endpoints(store, found.source, found.target)
    if isinstance(resolved, web.Response):
        return resolved
    source, target = resolved
    refused = await _into_builtin_refusal(target, store)
    if refused is not None:
        return refused
    base._audit(caller, "migrations.resume", "allowed", resources=found.id)
    migration.start(
        migration.reserved(
            found.id,
            migration.run_job(
                found, source, target, base._scope(store, caller), notify=_notify(request.app)
            ),
        )
    )
    started.append(True)
    return web.json_response(_job_body(found), status=202)


async def api_memory_provider_migration_rollback(request: web.Request) -> web.Response:
    """POST /api/memory/provider/migrations/{id}/rollback: delete what this move inserted."""
    caller = await base._caller(request, "migrations.rollback")
    found = await asyncio.to_thread(_job_or_404, request.match_info["id"], caller)
    if isinstance(found, web.Response):
        return found
    if not migration.reserve(found.id):
        return base._json_error(_BUSY, "job_busy", 409)
    started: list[bool] = []
    try:
        return await _rollback(request, caller, found, started)
    finally:
        if not started:
            migration.unreserve(found.id)


async def _rollback(
    request: web.Request, caller: str, found: Any, started: list[bool]
) -> web.Response:
    if found.dry_run or found.state not in migration.ROLLBACK_STATES:
        return base._json_error("This move cannot be rolled back now.", "not_rollbackable", 409)
    store = "" if found.store == "default" else found.store
    source = None
    file_items = None
    if found.pending_ids and found.from_file:
        # A batch's reply was lost: the same file, sent with the undo, settles it.
        raw = await _read_capped(request, migration.MAX_IMPORT_BYTES)
        if not raw or migration.file_source_id(raw) != found.source:
            return base._json_error(
                f"{len(found.pending_ids)} writes were sent but never acknowledged. Send "
                "the file this import read with the undo, so they can be settled.",
                "unacknowledged_writes",
                409,
            )
        file_items, _errors, _warnings = await asyncio.to_thread(migration.parse_import, raw)
    elif found.pending_ids:
        _, source, error = await asyncio.to_thread(base._target, store, found.source)
        if source is None:
            return base._json_error(error, "provider_unavailable", 503)
        unprepared = await base._prepare_builtin(store, source)
        if unprepared is not None:
            return unprepared
    _, target, error = await asyncio.to_thread(base._target, store, found.target)
    if target is None:
        return base._json_error(error, "provider_unavailable", 503)
    if target is builtin.BUILTIN:
        # Crew's own memory is never a rollback target: no job deletes from it.
        return base._json_error(
            "A move into Crew's own memory is not rolled back; edit it with the memory editor routes.",
            "not_rollbackable",
            409,
        )
    base._audit(caller, "migrations.rollback", "allowed", resources=found.id)
    migration.start(
        migration.reserved(
            found.id,
            migration.rollback(
                found,
                target,
                base._scope(store, caller),
                source=source,
                file_items=file_items,
                notify=_notify(request.app),
            ),
        )
    )
    started.append(True)
    return web.json_response(_job_body(found), status=202)


# ── Export and import ────────────────────────────────────────────────────────


async def api_memory_provider_export(request: web.Request) -> web.StreamResponse:
    """GET /api/memory/provider/export: JSONL ``kirocrew-memory/1``.

    ``?include=retired,revisions`` adds retired items and each item's history.
    """
    caller = await base._caller(request, "export")
    resolved = await base._resolve(request, writing=False)
    if isinstance(resolved, web.Response):
        return resolved
    store, provider_id, provider = resolved
    include = {part for part in request.query.get("include", "").split(",") if part}
    if not include <= {"retired", "revisions"}:
        return base._json_error("include takes retired and revisions", "invalid_export")
    from kiro_crew import __version__
    from kiro_crew.dashboard.handlers._shared import _redact_memory_field

    response = web.StreamResponse(
        headers={
            "Content-Type": "application/x-ndjson; charset=utf-8",
            "Content-Disposition": 'attachment; filename="kirocrew-memory.jsonl"',
        }
    )
    await response.prepare(request)
    try:
        # Each record is scrubbed as data before it is encoded, so a redaction can
        # never break the JSON or swallow the newline that ends the record.
        async for line in migration.export_lines(
            provider,
            provider_id,
            base._scope(store, caller),
            include_retired="retired" in include,
            include_revisions="revisions" in include,
            crew_version=__version__,
            scrub=lambda record: _redact_memory_field(base._scrub_meta_keys(record)),
        ):
            await response.write(line.encode("utf-8"))
    except ProviderError as exc:
        await response.write(
            (
                json.dumps(
                    {"error": _redact_memory_field(exc.message), "code": f"provider_{exc.code}"}
                )
                + "\n"
            ).encode()
        )
    await response.write_eof()
    return response


async def _read_capped(request: web.Request, cap: int) -> bytes:
    """The whole body, or its first ``cap + 1`` bytes so the caller can refuse it as too big.

    ``StreamReader.read(n)`` returns what is buffered, not ``n`` bytes, so it loops to EOF.
    """
    chunks: list[bytes] = []
    total = 0
    while total <= cap:
        chunk = await request.content.read(cap + 1 - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


async def api_memory_provider_import(request: web.Request) -> web.Response:
    """POST /api/memory/provider/import: a ``kirocrew-memory/1`` file, as a move job.

    ``?dry_run=1`` plans it. The answer is the job, which the
    caller follows at ``GET /migrations/{id}``.
    """
    caller = await base._caller(request, "import")
    dry_run = request.query.get("dry_run") == "1"
    resolved = await base._resolve(request, writing=not dry_run)
    if isinstance(resolved, web.Response):
        return resolved
    store, provider_id, provider = resolved
    raw = await _read_capped(request, migration.MAX_IMPORT_BYTES)
    items, errors, warnings = await asyncio.to_thread(migration.parse_import, raw)
    if errors and not items:
        return base._json_error("; ".join(errors[:5]), "invalid_import")
    job = await asyncio.to_thread(
        lambda: migration.new_job(
            source=migration.file_source_id(raw),
            target=provider_id,
            store=store,
            kinds=None,
            include_retired=True,
            preserve_timestamps=True,
            dry_run=dry_run,
            caller=caller,
        )
    )
    job.notes.extend(warnings + errors[:20])
    base._audit(caller, "import", "allowed", resources=f"{job.id}:{provider_id}")
    migration.start(
        migration.run_job(
            job,
            None,
            provider,
            base._scope(store, caller),
            items=items,
            notify=_notify(request.app),
        )
    )
    return web.json_response(_job_body(job), status=202)
