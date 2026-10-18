"""Exporting and importing ``.knowledge`` bundles, and the shape an imported bundle must
have before any of it is stored.

Served through :mod:`kiro_crew.dashboard.handlers.knowledge`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from aiohttp import web

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.handlers.knowledge_access import _require_knowledge_owner, _sel_log, _store
from kiro_crew.knowledge.ingestion import _redact
from kiro_crew.knowledge.store import BUNDLE_STATE_KEY_COL, KnowledgeBundleError

logger = logging.getLogger("kiro_crew.dashboard.handlers.knowledge")


_BUNDLE_LIST_FIELDS = (
    "items",
    "entities",
    "relations",
    "sources",
    "source_locations",
    "mentions",
    *BUNDLE_STATE_KEY_COL,
)
# The fields import_bundle's redaction loops pass to _redact(); each has to be
# a string or null before it reaches _redact() -> redact_exfiltration_urls(),
# whose regex .finditer() raises an unhandled TypeError on anything else.
#
# A state row's display name and document locator are text another instance wrote,
# so they go through the same redaction as an item's title. ``file_path`` does not:
# it is half of a PRIMARY KEY that the folder scan matches against a real path on
# disk, and rewriting it would leave the row unable to match its own file, which is
# how a file gets ingested a second time.
_BUNDLE_REDACTED_FIELDS = {
    "items": ("title", "summary", "content"),
    "entities": ("name", "description"),
    "relations": ("relation_type", "description"),
    "artifact_item_state": ("name",),
    "agent_item_state": ("name", "source_uri"),
}

#: Per-document state columns the validator type-checks without redacting them.
#: ``file_path`` and the key columns land in a PRIMARY KEY, so they have to be real
#: text. ``mtime`` is absent on purpose: it is host-local and the importer writes a
#: fixed value rather than reading the bundle's, so its shape does not matter.
_BUNDLE_STATE_PLAIN_FIELDS = {
    "folder_file_state": ("file_path", "content_hash", "text_hash", "last_seen"),
    "artifact_item_state": ("slug", "content_hash", "updated_at", "kind"),
    "agent_item_state": ("slug", "content_hash", "updated_at"),
}

#: Fields of an ``import_bundle`` ``withheld`` entry that carry BUNDLE-AUTHORED text,
#: redacted on the way OUT rather than on the way in. Every one of them is a value the
#: store has to keep byte-exact -- a document key lands in a PRIMARY KEY matched against
#: a real slug or path, and the ids are matched against ``items.id`` -- which is why
#: ``_BUNDLE_STATE_PLAIN_FIELDS`` deliberately exempts them from the inbound loop. That
#: leaves the response as the one place bundle text reaches a reader unfiltered: a
#: document key is unbounded, and the whole account is returned verbatim by
#: ``web.json_response``. The SEL copy is already covered by ``_redact_and_clip``.
#: ``reason`` and ``table`` are module constants, so neither is listed.
_WITHHELD_REDACTED_FIELDS = ("key", "items", "item_id", "source_id")


def _validate_knowledge_bundle(body: object) -> str | None:
    """Return an error string if body isn't an importable bundle shape, else None.

    Runs before the redaction loops and the store call so a malformed bundle
    fails with a clean 400 instead of an unhandled AttributeError/TypeError
    (non-dict body or entries) or a silently-committed corrupt row (non-JSON
    sources.properties / entities.aliases, which every reader parses with
    json.loads()).
    """
    if not isinstance(body, dict):
        return "bundle must be a JSON object"
    for field in _BUNDLE_LIST_FIELDS:
        value = body.get(field, [])
        if not isinstance(value, list):
            return f"'{field}' must be a list"
        for entry in value:
            if not isinstance(entry, dict):
                return f"'{field}' entries must be objects"
    for field, keys in _BUNDLE_REDACTED_FIELDS.items():
        for entry in body.get(field, []):
            for key in keys:
                value = entry.get(key)
                if value is not None and not isinstance(value, str):
                    return f"'{field}.{key}' must be a string or null"
    # store.import_bundle() writes these two columns through unparsed (with
    # '{}'/'[]' defaults when ABSENT), so anything present must already be the
    # JSON text every reader json.loads() back: readers such as the source
    # detail handlers parse the raw column with no empty-string guard, and
    # find_entity() calls .lower() on each parsed alias.  Only absent/null
    # falls through to the store defaults; a present non-string (1 -> TEXT
    # "1"), an empty string, or the wrong parsed shape would commit a row
    # that crashes a later, unrelated read.  json.loads raises RecursionError
    # (not ValueError) on deeply-nested input, so catch it here too.
    for src in body.get("sources", []):
        props = src.get("properties")
        if props is None:
            continue
        if not isinstance(props, str):
            return "'sources.properties' must be a JSON object string or null"
        try:
            parsed = json.loads(props)
        except (ValueError, RecursionError):
            return "'sources.properties' must be valid JSON"
        if not isinstance(parsed, dict):
            return "'sources.properties' must be a JSON object"
    for ent in body.get("entities", []):
        aliases = ent.get("aliases")
        if aliases is None:
            continue
        if not isinstance(aliases, str):
            return "'entities.aliases' must be a JSON array string or null"
        try:
            parsed = json.loads(aliases)
        except (ValueError, RecursionError):
            return "'entities.aliases' must be valid JSON"
        if not isinstance(parsed, list) or not all(isinstance(a, str) for a in parsed):
            return "'entities.aliases' must be a JSON array of strings"
    for field, keys in _BUNDLE_STATE_PLAIN_FIELDS.items():
        for entry in body.get(field, []):
            for key in keys:
                value = entry.get(key)
                if value is not None and not isinstance(value, str):
                    return f"'{field}.{key}' must be a string or null"
    return None


# ---------- Export / Import ----------


async def export_item(request: web.Request) -> web.Response:
    """GET /api/knowledge/items/{id}/export -- .knowledge JSON bundle."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.item.export")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    item_id = request.match_info["id"]
    bundle = await asyncio.to_thread(store.export_item, item_id)
    if not bundle:
        return web.json_response({"error": "not found"}, status=404)
    _sel_log("export_item", item_id=item_id)
    return web.json_response(
        bundle, headers={"Content-Disposition": "attachment; filename=item.knowledge"}
    )


async def export_all(request: web.Request) -> web.Response:
    """GET /api/knowledge/export -- full .knowledge JSON bundle, optionally filtered by namespace."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.export")
    if owner_denied is not None:
        return owner_denied
    namespace = request.query.get("namespace")
    _sel_log("export_all", namespace=namespace)
    store = _store(request)
    bundle = await asyncio.to_thread(store.export_all, namespace=namespace)
    safe_ns = re.sub(r"[^\w.-]", "_", namespace) if namespace else None
    filename = f"{safe_ns}.knowledge" if safe_ns else "knowledge.knowledge"
    return web.json_response(
        bundle, headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


async def import_bundle(request: web.Request) -> web.Response:
    """POST /api/knowledge/import -- accept .knowledge JSON bundle."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.import")
    if owner_denied is not None:
        return owner_denied
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    shape_error = _validate_knowledge_bundle(body)
    if shape_error is not None:
        _sel_log("import", outcome="rejected", reason=shape_error)
        return web.json_response(
            {"error": f"malformed bundle: {shape_error}", "code": "malformed_knowledge_bundle"},
            status=400,
        )
    # Redact imported text fields (may contain LLM-derived content from another instance)
    for item in body.get("items", []):
        redacted_title = _redact(item.get("title"))
        item["title"] = redacted_title if redacted_title is not None else ""
        item["summary"] = _redact(item.get("summary"))
        redacted_content = _redact(item.get("content"))
        item["content"] = redacted_content if redacted_content is not None else ""
    for ent in body.get("entities", []):
        redacted_name = _redact(ent.get("name"))
        ent["name"] = redacted_name if redacted_name is not None else ""
        ent["description"] = _redact(ent.get("description"))
    for rel in body.get("relations", []):
        redacted_type = _redact(rel.get("relation_type"))
        rel["relation_type"] = redacted_type if redacted_type is not None else ""
        rel["description"] = _redact(rel.get("description"))
    for field, keys in _BUNDLE_REDACTED_FIELDS.items():
        if field not in BUNDLE_STATE_KEY_COL:
            continue
        for row in body.get(field, []):
            for key in keys:
                row[key] = _redact(row.get(key))
    store = _store(request)

    # BEGIN IMMEDIATE takes the write lock eagerly (busy_timeout 10s) and a
    # large bundle inserts thousands of rows before the commit rebuilds the
    # entity graph with a full table scan -- never on the event loop. The
    # success audit rides in the same worker as the commit: a client
    # disconnect cancels the awaiting coroutine, and a cancellation landing
    # after the worker committed must not skip the SEL record (the worker
    # thread runs to completion regardless; SEL writes are lock-guarded).
    # The worker re-raises here, so every arm below still catches exactly
    # what the synchronous call raised; the rejection arms keep their own
    # audit lines because they are tied to the HTTP response they build.
    def _import_and_audit() -> dict:
        result = store.import_bundle(body)
        _sel_log("import", **result)
        return result

    try:
        result = await asyncio.to_thread(_import_and_audit)
    except KnowledgeBundleError as exc:
        # The store enforces the JSON-column well-formedness invariant
        # (sources.properties / entities.aliases) at the writer; surface its
        # typed rejection as a clean 400.  Unlike the driver errors below,
        # its message is validator-crafted (the same class as the handler's
        # own shape errors above), so it is safe to render verbatim.
        _sel_log("import", outcome="rejected", reason=str(exc))
        return web.json_response(
            {"error": f"malformed bundle: {exc}", "code": "malformed_knowledge_bundle"},
            status=400,
        )
    except (
        KeyError,
        OverflowError,
        sqlite3.IntegrityError,
        sqlite3.ProgrammingError,
        sqlite3.DataError,
    ) as exc:
        # Only failures that genuinely mean a bad bundle earn a 400:
        # IntegrityError (constraint/FK violations), ProgrammingError and
        # DataError (bad values reaching the SQL layer), KeyError (missing
        # required field), and OverflowError (a bundle integer field, e.g.
        # chunk_index, too large for SQLite's 64-bit INTEGER, raised at bind
        # time inside the store call -- neither a KeyError nor a
        # sqlite3.Error, so it needs its own arm).  The exception detail
        # stays server-side: the dashboard renders ``error`` verbatim, so
        # raw driver text must not reach the client.
        logger.warning("Knowledge bundle import rejected: %s", exc)
        _sel_log("import", outcome="rejected", reason=str(exc))
        return web.json_response(
            {"error": "malformed bundle", "code": "malformed_knowledge_bundle"},
            status=400,
        )
    except sqlite3.Error as exc:
        # Operational store failures -- OperationalError from a locked DB
        # past busy_timeout or a full disk, and every other sqlite3.Error --
        # are not the client's fault: a 400 "malformed bundle" for a valid
        # file sends the user off debugging their export.  Surface them as a
        # 5xx with a generic body; the detail is logged server-side only.
        logger.exception("Knowledge bundle import failed in the store")
        _sel_log("import", outcome="error", reason=str(exc))
        return web.json_response(
            {"error": "internal server error", "code": "knowledge_import_failed"},
            status=500,
        )
    # The withheld account names the content that did not arrive, and it names it in the
    # bundle's OWN words: a document key and the item ids it claimed. Those are the
    # values the inbound loop leaves alone on purpose, because the store matches them
    # against a PRIMARY KEY and against ``items.id`` and so needs them byte-exact --
    # which makes this response the one path on which bundle-authored text reaches a
    # reader unfiltered. Redacted HERE, after the store has used and stored the real
    # values, so the rule the loop above applies to ``name`` and ``source_uri`` on those
    # very rows holds for the account of them too.
    for entry in result.get("withheld", []):
        if not isinstance(entry, dict):
            continue
        for field in _WITHHELD_REDACTED_FIELDS:
            if isinstance(entry.get(field), str):
                entry[field] = _redact(entry[field])
    return web.json_response(result)
