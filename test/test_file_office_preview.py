"""Tests for /api/file-office-preview — inline text extraction for .docx/.pptx.

Pins the endpoint's security envelope on the enforcing side (sensitive-path
403, unsupported-format 415 with SEL audit, resolve=1 via the shared
_resolve_project_relative helper) plus the response contract the frontend
relies on (text/truncated, no format/supported/empty fields) and the
aggregate extraction budget (a many-slide deck cannot accumulate unbounded
text — doc_parser stops at the caller's cap), including a credential that
straddles _OFFICE_PREVIEW_CAP: extraction is whole-paragraph / whole-slide and
redaction runs before the cut, so no prefix of it is ever served.

``format=blocks`` is covered here too: that the default stays byte-identical
text, that an unknown format is refused rather than silently answered with text,
and that the structured payload is redacted and carries the same security
envelope. The block EXTRACTION itself is test_doc_blocks.py's subject.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import zipfile
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from dashboard_owner_helpers import as_owner
from ooxml_fixtures import docx_para, docx_table, write_docx

from kiro_crew.dashboard.handlers import api_file_office_preview
from kiro_crew.dashboard.handlers import files as files_mod
from kiro_crew.dashboard.handlers.files import _MAX_UPLOAD_BYTES, _OFFICE_PREVIEW_CAP


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-office-preview", api_file_office_preview)
    return as_owner(app)


@pytest.fixture
def mock_sel():
    with (
        patch("kiro_crew.sel.sel") as m,
        patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=False),
    ):
        instance = MagicMock()
        m.return_value = instance
        yield instance


def _write_docx(path: str, paragraphs: list[str]) -> None:
    body = "\n".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)


def _write_pptx(path: str, slides: list[list[str]]) -> None:
    for i, texts in enumerate(slides, 1):
        shapes = "".join(
            f"<p:sp><p:txBody><a:p><a:r><a:t>{t}</a:t></a:r></a:p></p:txBody></p:sp>" for t in texts
        )
        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
            ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
            f"<p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:sld>"
        )
        with zipfile.ZipFile(path, "a") as zf:
            zf.writestr(f"ppt/slides/slide{i}.xml", xml)


# --- Happy path: contract the frontend relies on ---


@pytest.mark.asyncio
async def test_docx_preview_returns_text_and_truncated_only(tmp_path, mock_sel):
    f = tmp_path / "report.docx"
    _write_docx(str(f), ["Introduction", "First paragraph of the document."])
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 200
            body = await resp.json()
    assert "Introduction" in body["text"]
    assert body["truncated"] is False
    # Zero-consumer fields must NOT come back (review: dropped surface).
    assert "format" not in body
    assert "supported" not in body
    assert "empty" not in body


@pytest.mark.asyncio
async def test_docx_preview_carries_no_slides(tmp_path, mock_sel):
    # `slides` is the .pptx shape; a document has none, and an empty list
    # would make the panel probe a field that can never be populated.
    f = tmp_path / "report.docx"
    _write_docx(str(f), ["Introduction"])
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 200
            body = await resp.json()
    assert "slides" not in body


@pytest.mark.asyncio
async def test_pptx_preview_returns_slides_beside_the_flat_text(tmp_path, mock_sel):
    """A deck comes back slide by slide, and the flat text is the join of those slides.

    The panel renders `slides`; `text` stays for the .docx path and for any
    consumer that predates `slides`. Both come from ONE extraction, so the
    contract is equality, not resemblance.
    """
    f = tmp_path / "deck.pptx"
    _write_pptx(str(f), [["Roadmap", "Q3 goals"], [], ["Risks"]])
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 200
            body = await resp.json()
    # Deck numbering, not a renumbering: slide 2 carried no text.
    assert body["slides"] == [
        {"index": 1, "text": "Roadmap\nQ3 goals"},
        {"index": 3, "text": "Risks"},
    ]
    assert body["text"] == "--- Slide 1 ---\nRoadmap\nQ3 goals\n\n--- Slide 3 ---\nRisks"
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_pptx_slides_share_the_preview_cap(tmp_path, mock_sel):
    """The structured form never carries more text than the flat one.

    One budget of _OFFICE_PREVIEW_CAP is spent across the deck in slide
    order: the slide that crosses it is cut to what is left, and the slides
    after it are dropped -- not a second cap, and not an uncapped list beside
    a capped string.
    """
    f = tmp_path / "long.pptx"
    half = _OFFICE_PREVIEW_CAP // 2
    _write_pptx(str(f), [["a" * half], ["b" * half], ["c" * 10], ["d" * 10]])
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 200
            body = await resp.json()
    assert body["truncated"] is True
    assert len(body["text"]) == _OFFICE_PREVIEW_CAP
    assert [s["index"] for s in body["slides"]] == [1, 2]
    assert sum(len(s["text"]) for s in body["slides"]) == _OFFICE_PREVIEW_CAP


@pytest.mark.asyncio
async def test_pptx_slides_are_redacted_before_the_cap(tmp_path, mock_sel):
    # Same rule as the flat text (test_redaction_runs_before_truncation): a
    # credential straddling the per-slide cut must not leak as a prefix.
    f = tmp_path / "creds.pptx"
    secret = "AKIAIOSFODNN7EXAMPLE"
    _write_pptx(str(f), [["x" * (_OFFICE_PREVIEW_CAP - 10) + secret], ["next"]])
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 200
            body = await resp.json()
    joined = "".join(s["text"] for s in body["slides"])
    assert secret not in joined
    assert secret[:10] not in joined
    assert secret not in body["text"]


@pytest.mark.asyncio
async def test_truncation_flag_set_and_text_capped(tmp_path, mock_sel):
    f = tmp_path / "huge.docx"
    # One paragraph larger than the cap: extraction budget (cap + 1) keeps
    # the truncation detectable while the response text is cut to the cap.
    _write_docx(str(f), ["x" * (_OFFICE_PREVIEW_CAP + 100)])
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 200
            body = await resp.json()
    assert body["truncated"] is True
    assert len(body["text"]) == _OFFICE_PREVIEW_CAP


# --- Security envelope ---


@pytest.mark.asyncio
async def test_unsupported_extension_415_with_sel_audit(tmp_path, mock_sel):
    f = tmp_path / "legacy.xls"
    f.write_bytes(b"\xd0\xcf\x11\xe0old-ole-junk")
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 415
            body = await resp.json()
    assert body["code"] == "unsupported_preview_format"
    # The denial must leave an SEL record (review: audit gap).
    denied = [
        c
        for c in mock_sel.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "denied"
        and c.kwargs.get("error") == "unsupported_preview_format"
    ]
    assert denied, "unsupported-format 415 must be SEL-audited"


@pytest.mark.asyncio
async def test_sensitive_path_403(tmp_path, mock_sel):
    f = tmp_path / "secrets.docx"
    _write_docx(str(f), ["top secret"])
    with (
        patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)),
        patch(
            "kiro_crew.dashboard.handlers.files.is_sensitive_path",
            return_value=True,
        ),
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 403
            body = await resp.json()
    assert body["code"] == "sensitive_path"


@pytest.mark.asyncio
async def test_forbidden_path_400(mock_sel):
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=None):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-office-preview?path=/etc/passwd.docx")
            assert resp.status == 400
            body = await resp.json()
    assert body["code"] == "forbidden_path"


@pytest.mark.asyncio
async def test_resolve_uses_shared_helper(tmp_path, mock_sel):
    """resolve=1 goes through _resolve_project_relative (review: no inline copy)."""
    f = tmp_path / "proj" / "doc.docx"
    f.parent.mkdir()
    _write_docx(str(f), ["hello from project"])
    with (
        patch.dict(os.environ, {"KIROCREW_PROJECT_DIR": str(f.parent)}),
        patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)),
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-office-preview?path=doc.docx&resolve=1")
            assert resp.status == 200
            body = await resp.json()
    assert "hello from project" in body["text"]


@pytest.mark.asyncio
async def test_resolve_outside_project_denied_and_audited(tmp_path, mock_sel):
    proj = tmp_path / "proj"
    proj.mkdir()
    with patch.dict(os.environ, {"KIROCREW_PROJECT_DIR": str(proj)}):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/file-office-preview?path=../outside.docx&resolve=1")
            assert resp.status == 400
            body = await resp.json()
    assert body["code"] == "path_outside_project"
    denied = [
        c
        for c in mock_sel.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "denied" and c.kwargs.get("error") == "outside_project"
    ]
    assert denied, "resolve denial must be SEL-audited"


@pytest.mark.asyncio
async def test_oversized_file_413_before_any_parsing(tmp_path, mock_sel):
    """The size gate runs BEFORE zipfile ever opens the archive.

    The gate fstats the already-open O_NOFOLLOW fd (not the path), so the
    oversize is a real sparse file: a stat-the-path mock would not
    reach the code under test.
    """
    f = tmp_path / "huge.docx"
    _write_docx(str(f), ["small real content"])
    os.truncate(str(f), 51 * 1024 * 1024)  # sparse: st_size > cap, no disk cost
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 413
            body = await resp.json()
    assert body["code"] == "file_too_large"
    denied = [
        c
        for c in mock_sel.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "denied" and c.kwargs.get("error") == "file_too_large"
    ]
    assert denied, "oversized preview request must be SEL-audited"


@pytest.mark.asyncio
async def test_extraction_reads_through_the_prefix_fd_and_closes_it(tmp_path, mock_sel):
    """No stat→open TOCTOU, and the checked fd never crosses back to the loop.

    extract_text must receive the SAME open handle the shared prefix opened
    and fstat-ed (never re-open the path), and that handle must already be
    closed once the response is built: every path that opens it also closes
    it on the worker thread, so a cancellation cannot strand an open file in
    a discarded future or finalize one on the event loop.
    """
    f = tmp_path / "doc.docx"
    _write_docx(str(f), ["content"])
    seen: dict[str, Any] = {}
    real_extract = files_mod.extract_text

    def _spy(path, **kwargs):
        seen["fileobj"] = kwargs.get("fileobj")
        return real_extract(path, **kwargs)

    with (
        patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)),
        patch("kiro_crew.dashboard.handlers.files.extract_text", side_effect=_spy),
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 200
            body = await resp.json()
    assert body["text"] == "content"
    fobj = seen["fileobj"]
    assert fobj is not None, "extract_text must be handed the prefix's checked fd"
    assert fobj.closed, "the checked file object must be closed on the worker thread"


@pytest.mark.asyncio
async def test_open_envelope_is_the_shared_prefix_and_runs_off_the_loop(tmp_path, mock_sel):
    """The validate→sensitive→isfile→open→fstat envelope is shared AND off-loop.

    Two invariants in one, because they were one review finding: the endpoint
    must route through :func:`_open_checked_file` instead of hand-rolling a
    second spelling of that security boundary, and the whole envelope must
    run on a worker thread — an NFS/FUSE-backed document makes realpath/stat/
    open block for seconds, which on the event loop stalls every session's
    streaming and the liveness heartbeat.
    """
    f = tmp_path / "doc.docx"
    _write_docx(str(f), ["content"])
    seen: dict[str, Any] = {}
    real_prefix = files_mod._open_checked_file
    loop_thread = threading.current_thread()

    def _spy(*args, **kwargs):
        seen["thread"] = threading.current_thread()
        seen["kwargs"] = kwargs
        return real_prefix(*args, **kwargs)

    with (
        patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)),
        patch("kiro_crew.dashboard.handlers.files._open_checked_file", side_effect=_spy),
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 200
    assert (
        seen.get("thread") is not None
    ), "the endpoint must use the shared _open_checked_file prefix, not an inline copy"
    assert (
        seen["thread"] is not loop_thread
    ), "the open-and-check envelope must not run on the event loop"
    # The 50 MB ceiling is now expressed as the prefix's fstat_cap, enforced
    # on the fd before zipfile materializes the archive's central directory.
    assert seen["kwargs"]["fstat_cap"] == _MAX_UPLOAD_BYTES


@pytest.mark.asyncio
async def test_cancellation_is_sel_audited_and_reraised(tmp_path, mock_sel):
    """CancelledError during extraction records the access, then propagates."""
    f = tmp_path / "doc.docx"
    _write_docx(str(f), ["content"])
    request = make_mocked_request(
        "GET", f"/api/file-office-preview?path={f}", app=as_owner(web.Application())
    )
    request["user"] = "local-app"
    request["app"] = ""
    with (
        patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)),
        patch(
            # The parse is offloaded through the bounded transfer pool, so that
            # is the seam a cancellation arrives through.
            "kiro_crew.dashboard.handlers.files._run_path_probe",
            side_effect=asyncio.CancelledError(),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await api_file_office_preview(request)
    cancelled = [
        c
        for c in mock_sel.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "cancelled"
    ]
    assert cancelled, "cancelled extraction must still leave an SEL record"


@pytest.mark.asyncio
async def test_redaction_runs_before_truncation(tmp_path, mock_sel):
    """A credential straddling the cap boundary must not leak as a prefix.

    Redaction must see the FULL extracted text: slicing first would cut the
    secret mid-token so the redactor does not match it.
    """
    f = tmp_path / "creds.docx"
    # One paragraph: filler that ends 10 chars before the cap, then a fake
    # AKIA credential ID that straddles the boundary.
    secret = "AKIAIOSFODNN7EXAMPLE"
    filler = "x" * (_OFFICE_PREVIEW_CAP - 10)
    _write_docx(str(f), [filler + secret])
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}")
            assert resp.status == 200
            body = await resp.json()
    assert body["truncated"] is True
    # Neither the full secret nor its cap-cut prefix may appear.
    assert secret not in body["text"]
    assert secret[:10] not in body["text"]


# --- A credential straddling _OFFICE_PREVIEW_CAP ---
#
# The preview is safe at the cap because of TWO facts together, and these tests
# pin both from the endpoint's side (test_doc_parser.py pins the first at its
# source):
#
# 1. doc_parser's ``max_chars`` is a WHOLE-UNIT soft budget: extraction stops
#    after the paragraph (.docx) or slide (.pptx) that meets it, never inside
#    one. So the text ``redact`` sees never ends mid-token, unlike a raw byte
#    read (api_file_read), which cuts wherever the cap falls.
# 2. ``redact`` runs over that whole extraction, and ``_OFFICE_PREVIEW_CAP`` cuts
#    the REDACTED text. When a long token above the cap shrinks to a placeholder,
#    the cut reaches further into the extraction, but only into text redaction
#    already saw whole.
#
# Each case puts a credential across the cap in a shape that leaks when either
# fact stops holding: the secret's prefix is then served as real text.

_JWT_HEADER = '{"alg":"HS256","typ":"JWT"}'


def _b64url(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def _jwt(payload_chars: int) -> str:
    return _b64url(_JWT_HEADER) + "." + _b64url("x" * payload_chars) + "." + "s" * 43


async def _preview(path) -> dict[str, Any]:
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(path)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={path}")
            assert resp.status == 200
            return await resp.json()


def _slides_text(body: dict[str, Any]) -> str:
    return "".join(s["text"] for s in body["slides"])


@pytest.mark.asyncio
@pytest.mark.parametrize("before_cap", [4, 16])
async def test_docx_credential_straddling_the_cap_near_either_edge_is_masked(
    tmp_path, mock_sel, before_cap
):
    """The token is never cut at the cap, however little or much of it sits before it.

    test_redaction_runs_before_truncation covers the middle. A character-granular
    budget would serve the ``AKIA`` prefix at both of these offsets; 4 is the
    shortest prefix that still names the key family, 16 leaves only the last
    four characters past the cap.
    """
    secret = "AKIAIOSFODNN7EXAMPLE"
    f = tmp_path / "edge.docx"
    _write_docx(str(f), ["x" * (_OFFICE_PREVIEW_CAP - before_cap) + secret + " tail", "next"])
    body = await _preview(f)
    assert body["truncated"] is True
    assert "AKIA" not in body["text"]
    assert len(body["text"]) <= _OFFICE_PREVIEW_CAP


@pytest.mark.asyncio
async def test_docx_credential_in_the_paragraph_that_meets_the_budget_is_masked(tmp_path, mock_sel):
    """The paragraph the budget stops at is extracted whole, so its secret is seen whole."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    f = tmp_path / "own-paragraph.docx"
    _write_docx(str(f), ["x" * (_OFFICE_PREVIEW_CAP - 10), secret, "next"])
    body = await _preview(f)
    assert body["truncated"] is True
    assert "AKIA" not in body["text"]
    assert len(body["text"]) <= _OFFICE_PREVIEW_CAP


@pytest.mark.asyncio
async def test_docx_redaction_shrink_above_the_cap_pulls_no_unmasked_text_under_it(
    tmp_path, mock_sel
):
    """Tokens near the top shrink by thousands of characters when masked.

    The cut at the cap then reaches further into the extraction. Everything it
    reaches was redacted whole, and the secret that sits one redactor window past
    the cap is masked or never extracted, never served as a prefix.
    """
    jwt = _jwt(3000)
    head = [jwt] * 4
    used = sum(len(p) + 1 for p in head)
    margin = files_mod._STREAM_HOLDBACK_JWT_MAX
    secret = "AKIAIOSFODNN7EXAMPLE"
    f = tmp_path / "shrink.docx"
    _write_docx(str(f), [*head, "a" * (_OFFICE_PREVIEW_CAP + margin - 6 - used) + secret, "keep"])
    body = await _preview(f)
    assert body["truncated"] is True
    assert jwt not in body["text"]
    assert "AKIA" not in body["text"]
    assert len(body["text"]) <= _OFFICE_PREVIEW_CAP


@pytest.mark.asyncio
@pytest.mark.parametrize("before_cap", [4999, 8000])
async def test_docx_jwt_longer_than_the_redactor_window_across_the_cap_is_masked(
    tmp_path, mock_sel, before_cap
):
    """A token longer than 2x _STREAM_HOLDBACK_JWT_MAX is masked wherever it crosses the cap.

    Its header sits more than one window before the cap, so a read that extends
    the cap by one window would still sever it; whole-paragraph extraction does not.
    """
    jwt = _jwt(8500)
    assert len(jwt) > 2 * files_mod._STREAM_HOLDBACK_JWT_MAX
    f = tmp_path / "long-jwt.docx"
    _write_docx(str(f), ["a" * (_OFFICE_PREVIEW_CAP - before_cap) + " " + jwt, "keep"])
    body = await _preview(f)
    assert body["truncated"] is True
    assert jwt[:40] not in body["text"]
    assert len(body["text"]) <= _OFFICE_PREVIEW_CAP


@pytest.mark.asyncio
async def test_docx_private_key_one_line_per_paragraph_across_the_cap_is_masked(tmp_path, mock_sel):
    """A PEM block is the one secret that spans paragraph boundaries.

    Extraction stops on a base64 line inside the block, before its footer; the
    redactor's truncated-key branch (header plus base64 continuation lines) masks
    the body without the footer.
    """
    line = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7" + "x" * 12
    # Assembled so this test's own text carries no key marker.
    header = "-----BEGIN " + "PRIVATE KEY-----"
    footer = "-----END " + "PRIVATE KEY-----"
    f = tmp_path / "pem.docx"
    _write_docx(
        str(f), ["a" * (_OFFICE_PREVIEW_CAP - 200), header, *([line] * 100), footer, "after"]
    )
    body = await _preview(f)
    assert body["truncated"] is True
    assert line[:20] not in body["text"]
    assert len(body["text"]) <= _OFFICE_PREVIEW_CAP


@pytest.mark.asyncio
@pytest.mark.parametrize("before_cap", [4, 16])
async def test_pptx_credential_straddling_the_cap_is_masked_in_slides_and_text(
    tmp_path, mock_sel, before_cap
):
    """The slide the budget stops at is extracted whole; both payloads mask the secret.

    test_pptx_slides_are_redacted_before_the_cap covers the middle; these offsets
    are the ones a character-granular budget would leak (see the .docx twin).
    """
    secret = "AKIAIOSFODNN7EXAMPLE"
    f = tmp_path / "edge.pptx"
    _write_pptx(str(f), [["x" * (_OFFICE_PREVIEW_CAP - before_cap) + secret + " tail"], ["next"]])
    body = await _preview(f)
    assert body["truncated"] is True
    assert "AKIA" not in _slides_text(body)
    assert "AKIA" not in body["text"]
    assert sum(len(s["text"]) for s in body["slides"]) <= _OFFICE_PREVIEW_CAP
    assert len(body["text"]) <= _OFFICE_PREVIEW_CAP


@pytest.mark.asyncio
async def test_pptx_redaction_shrink_in_one_slide_frees_no_unmasked_text_in_the_next(
    tmp_path, mock_sel
):
    """_cap_slides spends one budget over REDACTED slide text.

    A slide that shrinks when masked leaves budget for later slides; each of
    those is itself redacted whole before its cut, so the freed budget never
    carries a raw secret.
    """
    jwt = _jwt(3000)
    secret = "AKIAIOSFODNN7EXAMPLE"
    filler = "a" * (_OFFICE_PREVIEW_CAP - 6 - 4 * (len(jwt) + 1))
    f = tmp_path / "shrink.pptx"
    _write_pptx(str(f), [[jwt] * 4, [filler], [secret + " more"], ["keep"]])
    body = await _preview(f)
    assert body["truncated"] is True
    assert jwt not in _slides_text(body)
    assert "AKIA" not in _slides_text(body)
    assert "AKIA" not in body["text"]


@pytest.mark.asyncio
async def test_pptx_jwt_longer_than_the_redactor_window_across_the_cap_is_masked(
    tmp_path, mock_sel
):
    jwt = _jwt(8500)
    f = tmp_path / "long-jwt.pptx"
    _write_pptx(str(f), [["a" * (_OFFICE_PREVIEW_CAP - 5000) + " " + jwt], ["keep"]])
    body = await _preview(f)
    assert jwt[:40] not in _slides_text(body)
    assert jwt[:40] not in body["text"]


# --- format=blocks: the structured preview ---


@pytest.mark.asyncio
async def test_default_and_explicit_text_are_the_same_response(tmp_path, mock_sel):
    """`format` is additive. Omitting it and passing `text` must produce the same
    bytes, and the same two-key shape existing callers already parse — a caller
    that never learned about the parameter cannot be affected by it."""
    f = tmp_path / "report.docx"
    _write_docx(str(f), ["Introduction", "Body."])
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            implicit = await (await client.get(f"/api/file-office-preview?path={f}")).read()
            explicit = await (
                await client.get(f"/api/file-office-preview?path={f}&format=text")
            ).read()
    assert implicit == explicit
    assert set(json.loads(implicit)) == {"text", "truncated"}


@pytest.mark.asyncio
async def test_unknown_format_is_refused_rather_than_answered_with_text(tmp_path, mock_sel):
    """A caller asking for a shape this build does not serve must learn that,
    not receive a plaintext blob to misread as structure."""
    f = tmp_path / "report.docx"
    _write_docx(str(f), ["Body."])
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}&format=html")
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_format"
    assert mock_sel.log_tool_invocation.call_args.kwargs["error"] == "invalid_format"


@pytest.mark.asyncio
async def test_blocks_returns_structure_and_no_text_field(tmp_path, mock_sel):
    f = tmp_path / "structured.docx"
    write_docx(
        str(f),
        docx_para("Overview", style="Heading1")
        + docx_para("Body copy.")
        + docx_table([["Region", "Total"]]),
    )
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}&format=blocks")
            assert resp.status == 200
            body = await resp.json()
    assert set(body) == {"blocks", "truncated"}
    assert [b["type"] for b in body["blocks"]] == ["heading", "paragraph", "table"]
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_blocks_text_is_credential_redacted(tmp_path, mock_sel):
    """The blocks payload carries the same document text the text mode does, so
    it goes through the same redaction — per string, so structure survives."""
    f = tmp_path / "leaky.docx"
    secret = "AKIAIOSFODNN7EXAMPLE"
    write_docx(
        str(f),
        docx_para(f"key {secret} here", style="Heading1") + docx_table([[f"cell {secret}"]]),
    )
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            body = await (
                await client.get(f"/api/file-office-preview?path={f}&format=blocks")
            ).json()
    assert secret not in json.dumps(body)
    # Redaction did not flatten the structure it walked through.
    assert [b["type"] for b in body["blocks"]] == ["heading", "table"]


@pytest.mark.asyncio
async def test_a_credential_split_across_two_runs_is_still_redacted(tmp_path, mock_sel):
    """Word splits a run at every formatting change, so a secret can arrive as two
    fragments that match nothing on their own. Redacting per RUN leaves both in
    place while the text mode -- which redacts the whole joined extraction --
    masks it, so the blocks payload would be the weaker of the two surfaces."""
    f = tmp_path / "split.docx"
    secret = "AKIAIOSFODNN7EXAMPLE"
    head, tail = secret[:8], secret[8:]
    write_docx(
        str(f),
        f"<w:p><w:r><w:t>{head}</w:t></w:r>"
        f"<w:r><w:rPr><w:b/></w:rPr><w:t>{tail}</w:t></w:r></w:p>",
    )
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            body = await (
                await client.get(f"/api/file-office-preview?path={f}&format=blocks")
            ).json()
    assert secret not in json.dumps(body)
    # Redacting the joined text has no run boundaries to map back onto, so the
    # paragraph collapses to one run. Formatting is lost only where a secret was.
    assert len(body["blocks"][0]["runs"]) == 1


@pytest.mark.asyncio
async def test_redaction_leaves_formatting_alone_when_nothing_matched(tmp_path, mock_sel):
    """The complement of the collapse above: an ordinary paragraph keeps its runs."""
    f = tmp_path / "clean.docx"
    write_docx(
        str(f),
        "<w:p><w:r><w:t>plain </w:t></w:r>"
        "<w:r><w:rPr><w:b/></w:rPr><w:t>strong</w:t></w:r></w:p>",
    )
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            body = await (
                await client.get(f"/api/file-office-preview?path={f}&format=blocks")
            ).json()
    assert body["blocks"][0]["runs"] == [
        {"text": "plain ", "bold": False, "italic": False},
        {"text": "strong", "bold": True, "italic": False},
    ]


def test_an_unknown_string_field_is_redacted_by_default():
    """The point of the inversion. Enumerating the keys that hold text means a
    block shape added later carries its text out unredacted until someone extends
    the list, and nothing fails while they have not."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    out = files_mod._redact_block({"type": "caption", "some_future_field": secret})
    assert isinstance(out, dict)
    assert secret not in str(out["some_future_field"])
    assert out["type"] == "caption", "a structural label must survive untouched"


def test_structural_values_survive_the_walk():
    """The complement: "redact everything" must not corrupt the shape the frontend
    switches on, or a heading level and a bold flag would ride out as strings."""
    block = {"type": "heading", "level": 3, "text": "Plain title"}
    assert files_mod._redact_block(block) == block
    para = {"type": "paragraph", "runs": [{"text": "hi", "bold": True, "italic": False}]}
    assert files_mod._redact_block(para) == para


@pytest.mark.asyncio
async def test_blocks_keeps_the_unsupported_format_refusal(tmp_path, mock_sel):
    f = tmp_path / "legacy.doc"
    f.write_bytes(b"\xd0\xcf\x11\xe0")
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}&format=blocks")
            assert resp.status == 415
            assert (await resp.json())["code"] == "unsupported_preview_format"


@pytest.mark.asyncio
async def test_blocks_keeps_the_sensitive_path_refusal(tmp_path, mock_sel):
    f = tmp_path / "secret.docx"
    _write_docx(str(f), ["Body."])
    with (
        patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)),
        patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=True),
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}&format=blocks")
            assert resp.status == 403
            assert (await resp.json())["code"] == "sensitive_path"


@pytest.mark.asyncio
async def test_blocks_reads_through_the_prefix_fd(tmp_path, mock_sel):
    """Same one-hop discipline as the text branch: the extractor parses the handle
    the prefix opened and fstat-ed, so the bytes parsed are the bytes measured."""
    f = tmp_path / "handle.docx"
    _write_docx(str(f), ["Body."])
    seen: list[object] = []
    real = files_mod.extract_blocks

    def spy(path, filename="", fileobj=None):
        seen.append(fileobj)
        return real(path, filename=filename, fileobj=fileobj)

    with (
        patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)),
        patch.object(files_mod, "extract_blocks", spy),
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}&format=blocks")
            assert resp.status == 200
    assert len(seen) == 1 and seen[0] is not None
    # Closed by the worker callback's `with`, never handed back to the loop.
    assert seen[0].closed is True


@pytest.mark.asyncio
async def test_empty_blocks_still_answers_200_for_the_frontend_to_fall_back(tmp_path, mock_sel):
    """A malformed container is not an error here: the frontend answers an empty
    list by retrying in text mode, and a 500 would look like a broken endpoint."""
    f = tmp_path / "broken.docx"
    f.write_bytes(b"PK\x03\x04 truncated garbage")
    with patch("kiro_crew.dashboard.handlers._validate_dashboard_path", return_value=str(f)):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/file-office-preview?path={f}&format=blocks")
            assert resp.status == 200
            assert await resp.json() == {"blocks": [], "truncated": False}


def test_the_block_character_budget_equals_the_text_preview_cap():
    """``format=blocks`` must not widen what one request extracts. The two caps
    are restated rather than imported -- doc_blocks sits below the dashboard
    handlers and cannot import from them -- so their equality is pinned here."""
    from kiro_crew import doc_blocks

    assert doc_blocks.MAX_CHARS == _OFFICE_PREVIEW_CAP
