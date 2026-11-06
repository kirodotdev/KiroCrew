"""``GET``/``PUT /api/docs/{doc_id}``: read and save a doc session's markdown.

Owner-only: a doc is the person's own writing, so an app token, a peer crew or an
unattributed caller is refused before the id is even looked at. File I/O runs off
the event loop.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from kiro_crew.dashboard import doc_files
from kiro_crew.dashboard.doc_goal_loop import schedule_goal_sync
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request


def _refused() -> web.Response:
    return web.json_response({"error": "forbidden", "code": "doc_owner_only"}, status=403)


def _doc_id(request: web.Request) -> str | None:
    doc_id = request.match_info.get("doc_id", "")
    return doc_id if doc_files.is_doc_id(doc_id) else None


def _body(doc: doc_files.DocFile) -> dict[str, object]:
    return {"content": doc.content, "version": doc.version}


async def api_doc_get(request: web.Request) -> web.Response:
    if not is_owner_dashboard_request(request):
        return _refused()
    doc_id = _doc_id(request)
    if doc_id is None:
        return web.json_response({"error": "invalid doc id", "code": "invalid_doc_id"}, status=400)
    doc = await asyncio.to_thread(doc_files.read_doc, doc_id)
    # Opening a doc also brings its goal loop in line with the file, which covers a
    # doc saved before the gateway armed loops itself.
    schedule_goal_sync(request.app.get("state"), doc_id, doc.content)
    return web.json_response(_body(doc))


async def api_doc_put(request: web.Request) -> web.Response:
    if not is_owner_dashboard_request(request):
        return _refused()
    doc_id = _doc_id(request)
    if doc_id is None:
        return web.json_response({"error": "invalid doc id", "code": "invalid_doc_id"}, status=400)
    # The cap leaves room for JSON escaping on top of the document's own size limit,
    # which write_doc enforces on the decoded text.
    body, body_err = await read_bounded_json(request, max_bytes=doc_files.MAX_DOC_BYTES * 2)
    if body_err is not None:
        return body_err
    assert body is not None
    content = body.get("content")
    version = body.get("version")
    if not isinstance(content, str):
        return web.json_response(
            {"error": "content must be a string", "code": "invalid_content"}, status=400
        )
    if version is not None and not isinstance(version, str):
        return web.json_response(
            {"error": "version must be a string", "code": "invalid_version"}, status=400
        )
    try:
        doc = await asyncio.to_thread(
            doc_files.write_doc, doc_id, content, expected_version=version
        )
    except doc_files.DocConflictError as exc:
        return web.json_response(
            {
                "error": "the doc changed since it was opened",
                "code": "doc_conflict",
                "content": exc.current.content,
                "version": exc.current.version,
            },
            status=409,
        )
    except ValueError:
        return web.json_response({"error": "doc too large", "code": "doc_too_large"}, status=413)
    # A saved doc's goals run whether or not its page stays open.
    schedule_goal_sync(request.app.get("state"), doc_id, doc.content)
    return web.json_response(_body(doc))


def register_doc_routes(app: web.Application) -> None:
    app.router.add_get("/api/docs/{doc_id}", api_doc_get)
    app.router.add_put("/api/docs/{doc_id}", api_doc_put)
