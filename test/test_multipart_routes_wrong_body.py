"""The multipart upload routes answer a body that is not multipart with a coded 400.

``request.multipart()`` asserts a ``multipart/*`` content type and needs a boundary,
so a JSON (or any other) body sent to an upload route raised out of the handler as a
500. Each route now opens its reader through ``_shared.open_multipart`` and answers
``not_multipart`` instead.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import portability
from kiro_crew.dashboard.handlers.files import api_upload_file
from kiro_crew.dashboard.handlers.knowledge import ingest_file

#: (headers, body) pairs that are not a usable multipart request.
_WRONG_BODIES = [
    ({"Content-Type": "application/json"}, b"[1, 2]"),
    ({"Content-Type": "application/json"}, b'{"file": "x"}'),
    ({"Content-Type": "text/plain"}, b"hello"),
    ({"Content-Type": "multipart/form-data"}, b"no boundary"),
]


@web.middleware
async def _owner(request: web.Request, handler):
    # An empty app id marks a dashboard subject; the owner gate reads ``user``.
    request["user"] = "owner"
    request["app"] = ""
    return await handler(request)


def _make_app(tmp_path: Path) -> web.Application:
    app = web.Application(middlewares=[_owner])
    app["state"] = SimpleNamespace(owner_id="owner", knowledge_store=MagicMock())
    app["knowledge_pipeline"] = SimpleNamespace(
        ingest_file=AsyncMock(),
        reserve_import_budget=AsyncMock(return_value=None),
        release_import_budget=MagicMock(),
        ingestion_in_flight=contextlib.nullcontext,
    )
    app.router.add_post("/api/upload/file", api_upload_file)
    app.router.add_post("/api/knowledge/ingest", ingest_file)
    app.router.add_post("/api/portability/import", portability.api_portability_import)
    app.router.add_post("/api/portability/preview", portability.api_portability_preview)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/upload/file",
        "/api/knowledge/ingest",
        "/api/portability/import",
        "/api/portability/preview",
    ],
)
@pytest.mark.parametrize(("headers", "body"), _WRONG_BODIES)
async def test_a_non_multipart_body_is_a_coded_400(
    path: str, headers: dict[str, str], body: bytes, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", tmp_path / "uploads")
    with patch("kiro_crew.dashboard.handlers.files._sel"), patch.object(portability, "_sel"):
        async with TestClient(TestServer(_make_app(tmp_path))) as client:
            response = await client.post(path, data=body, headers=headers)
            payload = await response.json()

    assert response.status == 400, payload
    assert payload["code"] == "not_multipart"


@pytest.mark.asyncio
async def test_open_multipart_passes_a_real_multipart_request(tmp_path: Path) -> None:
    from aiohttp import FormData

    from kiro_crew.dashboard.handlers._shared import open_multipart

    async def handler(request: web.Request) -> web.Response:
        reader, error = await open_multipart(request)
        assert error is None and reader is not None
        part = await reader.next()
        return web.json_response({"name": part.name})  # type: ignore[union-attr]

    app = web.Application()
    app.router.add_post("/x", handler)
    form = FormData()
    form.add_field("file", b"data", filename="a.txt")
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/x", data=form)
        assert response.status == 200
        assert (await response.json()) == {"name": "file"}
