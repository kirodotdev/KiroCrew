"""``GET /api/artifacts/{slug}/docx``: a markdown or text artifact as a Word file."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from docx import Document  # type: ignore[import-untyped]

from kiro_crew import artifact_docx
from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.dashboard import server
from kiro_crew.testing.synthetic_credentials import github_classic

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_SAFE_HEADERS = {
    "Content-Security-Policy": "sandbox",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
}


@pytest.fixture(name="store")
def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ArtifactStore:
    store = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", store)
    return store


def _fetch(path: str) -> tuple[int, dict[str, str], bytes]:
    async def run() -> tuple[int, dict[str, str], bytes]:
        app = web.Application()
        server._register_mcp_routes(app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(path)
            return resp.status, dict(resp.headers), await resp.read()

    return asyncio.run(run())


def _paragraphs(body: bytes) -> list[tuple[str, str]]:
    return [(p.style.name, p.text) for p in Document(io.BytesIO(body)).paragraphs]


def test_a_markdown_artifact_is_built_off_the_loop_with_headings_styled_and_redacted(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = github_classic()
    store.create(
        name="Plan",
        content=f"# Title\n\n- one\n## Part\n{token}\n" + "\0".join(token),
        slug="plan",
        kind="markdown",
    )
    handed_off: list[Any] = []
    real = asyncio.to_thread

    async def spy(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        handed_off.append(func)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)

    status, headers, body = _fetch("/api/artifacts/plan/docx")

    assert status == 200
    assert headers["Content-Type"] == _DOCX
    assert headers["Content-Disposition"] == (
        "attachment; filename=\"plan-v1.docx\"; filename*=UTF-8''plan-v1.docx"
    )
    assert {name: headers[name] for name in _SAFE_HEADERS} == _SAFE_HEADERS
    styled = _paragraphs(body)
    assert styled[:4] == [
        ("Heading 1", "Title"),
        ("Normal", ""),
        ("Normal", "- one"),
        ("Heading 2", "Part"),
    ]
    assert token not in styled[4][1] + styled[5][1]  # the second copy is split by NUL characters
    assert artifact_docx.build in handed_off


def test_a_version_query_serves_that_snapshot_and_a_text_artifact_keeps_every_line(
    store: ArtifactStore,
) -> None:
    store.create(name="Doc", content="# old", slug="doc", kind="text")
    store.update("doc", content="new", snapshot=True)

    _s, head1, body1 = _fetch("/api/artifacts/doc/docx?version=1")
    _s, head2, body2 = _fetch("/api/artifacts/doc/docx")

    assert _paragraphs(body1) == [("Normal", "# old")]
    assert "doc-v1.docx" in head1["Content-Disposition"]
    assert _paragraphs(body2) == [("Normal", "new")]
    assert "doc-v2.docx" in head2["Content-Disposition"]


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("/api/artifacts/nope/docx", 404, "not_found"),
        ("/api/artifacts/Bad_Slug/docx", 400, "bad_slug"),
        ("/api/artifacts/doc/docx?version=x", 400, "bad_version"),
        ("/api/artifacts/data/docx", 400, "unsupported_kind"),
        ("/api/artifacts/long/docx", 413, "too_large"),
    ],
)
def test_a_bad_request_is_refused_with_a_code(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch, path: str, status: int, code: str
) -> None:
    monkeypatch.setattr(artifact_docx, "MAX_LINES", 2)
    store.create(name="Doc", content="x", slug="doc", kind="text")
    store.create(name="Long", content="a\nb\nc", slug="long", kind="text")
    store.create(name="Data", content='{"a": 1}', slug="data", kind="json")

    got_status, headers, body = _fetch(path)

    assert (got_status, json.loads(body)["code"]) == (status, code)
    assert "Content-Disposition" not in headers


def test_no_text_of_the_name_reaches_the_disposition_header(store: ArtifactStore) -> None:
    name = 'a"\r\nX-Evil: 1 ' + "\0".join(github_classic())
    store.create(name=name, content="x", slug="evil", kind="text")

    _s, evil, _b = _fetch("/api/artifacts/evil/docx")

    assert "X-Evil" not in evil
    assert evil["Content-Disposition"] == (
        "attachment; filename=\"evil-v1.docx\"; filename*=UTF-8''evil-v1.docx"
    )
