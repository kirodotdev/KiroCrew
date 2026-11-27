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


def test_a_markdown_artifact_downloads_with_its_headings_styled_and_redacted(
    store: ArtifactStore,
) -> None:
    token = github_classic()
    store.create(
        name="Plan", content=f"# Title\n\n- one\n## Part\n{token}", slug="plan", kind="markdown"
    )

    status, headers, body = _fetch("/api/artifacts/plan/docx")

    assert status == 200
    assert headers["Content-Type"] == _DOCX
    assert headers["Content-Disposition"] == (
        "attachment; filename=\"Plan-v1.docx\"; filename*=UTF-8''Plan-v1.docx"
    )
    assert {name: headers[name] for name in _SAFE_HEADERS} == _SAFE_HEADERS
    styled = _paragraphs(body)
    assert styled[:4] == [
        ("Heading 1", "Title"),
        ("Normal", ""),
        ("Normal", "- one"),
        ("Heading 2", "Part"),
    ]
    # The text is the detail route's redacted text, so a credential is not in the file.
    assert token not in styled[4][1]


def test_a_version_query_serves_that_snapshot_and_a_text_artifact_keeps_every_line(
    store: ArtifactStore,
) -> None:
    store.create(name="Doc", content="# old", slug="doc", kind="text")
    store.update("doc", content="new", snapshot=True)

    _s, head1, body1 = _fetch("/api/artifacts/doc/docx?version=1")
    _s, head2, body2 = _fetch("/api/artifacts/doc/docx")

    assert _paragraphs(body1) == [("Normal", "# old")]
    assert "Doc-v1.docx" in head1["Content-Disposition"]
    assert _paragraphs(body2) == [("Normal", "new")]
    assert "Doc-v2.docx" in head2["Content-Disposition"]


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("/api/artifacts/nope/docx", 404, "not_found"),
        ("/api/artifacts/Bad_Slug/docx", 400, "bad_slug"),
        ("/api/artifacts/doc/docx?version=x", 400, "bad_version"),
        ("/api/artifacts/data/docx", 400, "unsupported_kind"),
    ],
)
def test_a_bad_request_is_refused_with_a_code(
    store: ArtifactStore, path: str, status: int, code: str
) -> None:
    store.create(name="Doc", content="x", slug="doc", kind="text")
    store.create(name="Data", content='{"a": 1}', slug="data", kind="json")

    got_status, headers, body = _fetch(path)

    assert (got_status, json.loads(body)["code"]) == (status, code)
    assert "Content-Disposition" not in headers


def test_content_over_the_cap_is_a_coded_413(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(artifact_docx, "MAX_LINES", 2)
    store.create(name="Long", content="a\nb\nc", slug="long", kind="text")

    status, _headers, body = _fetch("/api/artifacts/long/docx")

    assert (status, json.loads(body)["code"]) == (413, "too_large")


def test_a_hostile_name_cannot_break_the_disposition_header(store: ArtifactStore) -> None:
    store.create(name='a"\r\nX-Evil: 1/../é', content="x", slug="evil", kind="text")
    store.create(name="日本語", content="x", slug="jp", kind="text")

    _s, evil, _b = _fetch("/api/artifacts/evil/docx")
    _s, jp, _b = _fetch("/api/artifacts/jp/docx")

    assert "X-Evil" not in evil
    assert evil["Content-Disposition"] == (
        "attachment; filename=\"aX-Evil 1-v1.docx\"; filename*=UTF-8''aX-Evil%201-v1.docx"
    )
    assert 'filename="jp-v1.docx"' in jp["Content-Disposition"]


def test_the_build_runs_off_the_event_loop(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.create(name="Doc", content="x", slug="doc", kind="text")
    handed_off: list[Any] = []
    real = asyncio.to_thread

    async def spy(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        handed_off.append(func)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)

    status, _headers, _body = _fetch("/api/artifacts/doc/docx")

    assert status == 200
    assert artifact_docx.build in handed_off
