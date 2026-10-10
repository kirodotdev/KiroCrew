"""Dashboard chat: uploaded documents reach the model as extracted text."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pdf_test_helpers import text_pdf

from kiro_crew.dashboard import attachment_documents as ad
from kiro_crew.office_extract import OfficeExtraction
from kiro_crew.pdf_extract import PdfExtraction

_UUID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "uploads"
    target.mkdir()
    monkeypatch.setattr("kiro_crew.dashboard.handlers.files._UPLOAD_DIR", target)
    return target


def _upload(upload_dir: Path, name: str, data: bytes = b"%PDF-1.4\n") -> Path:
    path = upload_dir / f"{_UUID}_{name}"
    path.write_bytes(data)
    return path


def _stub_pdf(monkeypatch, outcome: PdfExtraction) -> list[int]:
    calls: list[int] = []

    def _fake(source, *, max_chars, deadline, **_kw):
        calls.append(max_chars)
        return outcome

    monkeypatch.setattr(ad, "extract_pdf_segments", _fake)
    return calls


def test_pdf_becomes_a_document_block_with_the_uploaded_name(upload_dir, monkeypatch):
    path = _upload(upload_dir, "report.pdf")
    _stub_pdf(
        monkeypatch,
        PdfExtraction((("page 1", "Revenue grew"), ("page 2", "Margins held")), False, None, 2),
    )

    blocks = ad.attachment_document_blocks([str(path)])

    assert blocks == [
        "[Document: report.pdf]\n--- Page 1 ---\nRevenue grew\n\n"
        "--- Page 2 ---\nMargins held\n[End of document]"
    ]


def test_pdf_goes_through_the_bounded_extractor_not_doc_parser(upload_dir, monkeypatch):
    """PDFs are extracted by extract_pdf_segments; doc_parser is not called."""
    path = _upload(upload_dir, "report.pdf")
    calls = _stub_pdf(monkeypatch, PdfExtraction((("page 1", "text"),), False, None, 1))
    monkeypatch.setattr(ad, "extract_office_text", lambda *a, **k: pytest.fail("office path"))

    ad.attachment_document_blocks([str(path)])

    # One past the injection cap, so a document cut at the cap is detectable.
    assert calls == [ad._LIMITS.max_text_inject + 1]


def test_extraction_failure_tells_the_model(upload_dir, monkeypatch):
    path = _upload(upload_dir, "scan.pdf")
    _stub_pdf(monkeypatch, PdfExtraction((), True, "memory"))

    assert ad.attachment_document_blocks([str(path)]) == [
        "[Attached document: scan.pdf — could not extract text]"
    ]


def test_image_only_pdf_tells_the_model(upload_dir, monkeypatch):
    path = _upload(upload_dir, "scan.pdf")
    _stub_pdf(monkeypatch, PdfExtraction((), False, None, 3))

    assert ad.attachment_document_blocks([str(path)]) == [
        "[Attached document: scan.pdf — could not extract text]"
    ]


def test_extracted_text_is_redacted_and_capped(upload_dir, monkeypatch):
    path = _upload(upload_dir, "creds.pdf")
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    long_text = "x" * (ad._LIMITS.max_text_inject + 500)
    _stub_pdf(
        monkeypatch,
        PdfExtraction((("page 1", f"key {secret}"), ("page 2", long_text)), True, None, 2),
    )

    (block,) = ad.attachment_document_blocks([str(path)])

    assert secret not in block
    assert block.endswith("[… truncated]\n[End of document]")


def test_paths_outside_uploads_are_not_read(upload_dir, tmp_path, monkeypatch):
    outside = tmp_path / "elsewhere.pdf"
    outside.write_bytes(b"%PDF-1.4\n")
    _stub_pdf(monkeypatch, PdfExtraction((("page 1", "leak"),), False, None, 1))

    assert ad.attachment_document_blocks([str(outside)]) == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges")
def test_a_symlink_in_uploads_is_judged_by_its_target(upload_dir, tmp_path, monkeypatch):
    outside = tmp_path / "private.pdf"
    outside.write_bytes(b"%PDF-1.4\n")
    link = upload_dir / f"{_UUID}_linked.pdf"
    link.symlink_to(outside)
    _stub_pdf(monkeypatch, PdfExtraction((("page 1", "leak"),), False, None, 1))

    assert ad.attachment_document_blocks([str(link)]) == []


def test_the_raw_path_is_screened_before_anything_resolves_it(upload_dir, monkeypatch):
    """A refused path (a Windows UNC share, here) gets no filesystem call: the
    hardened validator sees the raw string first, and its refusal ends the screen."""
    unc = r"\\attacker-host\share\x.pdf"
    seen: list[str] = []

    def _refuse(raw):
        seen.append(raw)
        return None

    def _probe(*_a, **_k):
        pytest.fail("the raw path was resolved before the screen")

    root = upload_dir.resolve()
    monkeypatch.setattr(ad, "_upload_root", lambda: root)
    monkeypatch.setattr(ad, "validate_file_path", _refuse)
    monkeypatch.setattr(ad.Path, "resolve", _probe)
    monkeypatch.setattr(ad.Path, "is_file", _probe)
    monkeypatch.setattr(ad, "extract_pdf_segments", lambda *a, **k: pytest.fail("extracted"))

    assert ad.attachment_document_blocks([unc]) == []
    assert seen == [unc]


def test_the_validator_verdict_wins_for_an_existing_upload(upload_dir, monkeypatch):
    """An upload the validator refuses (for example, as sensitive) is skipped even
    though it exists inside uploads/."""
    path = _upload(upload_dir, "report.pdf")
    monkeypatch.setattr(ad, "validate_file_path", lambda raw: None)
    monkeypatch.setattr(ad, "extract_pdf_segments", lambda *a, **k: pytest.fail("extracted"))

    assert ad.attachment_document_blocks([str(path)]) == []


def test_a_path_with_an_embedded_nul_is_skipped(upload_dir, monkeypatch):
    monkeypatch.setattr(ad, "extract_pdf_segments", lambda *a, **k: pytest.fail("extracted"))

    assert ad.attachment_document_blocks([f"{upload_dir}/bad\x00.pdf"]) == []
    assert ad.attachment_document_context(["bad\x00.pdf"]) == ""


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges")
def test_an_upload_swapped_for_a_symlink_after_admission_is_not_followed(
    upload_dir, tmp_path, monkeypatch
):
    """The entry is replaced between the screen and the read; the pinned open
    refuses the link instead of reading its target."""
    private = tmp_path / "private.pdf"
    private.write_bytes(b"%PDF-1.4\nsecret")
    upload = _upload(upload_dir, "report.pdf")
    screen = ad._admitted_name

    def _screen_then_swap(raw, root):
        name = screen(raw, root)
        upload.unlink()
        upload.symlink_to(private)
        return name

    monkeypatch.setattr(ad, "_admitted_name", _screen_then_swap)
    monkeypatch.setattr(ad, "extract_pdf_segments", lambda *a, **k: pytest.fail("extracted"))

    assert ad.attachment_document_blocks([str(upload)]) == [
        "[Attachment report.pdf — could not be processed]"
    ]


def test_a_file_in_a_subdirectory_of_uploads_is_not_read(upload_dir, monkeypatch):
    nested = upload_dir / "sub"
    nested.mkdir()
    path = nested / f"{_UUID}_report.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(ad, "extract_pdf_segments", lambda *a, **k: pytest.fail("extracted"))

    assert ad.attachment_document_blocks([str(path)]) == []


def test_the_parser_receives_the_bytes_that_were_read(upload_dir, monkeypatch):
    data = b"%PDF-1.4\nexact bytes"
    path = _upload(upload_dir, "report.pdf", data)
    seen: list[object] = []

    def _fake(source, **_kw):
        seen.append(source)
        return PdfExtraction((("page 1", "t"),), False, None, 1)

    monkeypatch.setattr(ad, "extract_pdf_segments", _fake)

    ad.attachment_document_blocks([str(path)])

    assert seen == [data]


def test_non_document_attachments_contribute_nothing(upload_dir, monkeypatch):
    txt = _upload(upload_dir, "notes.txt", b"hello")
    png = _upload(upload_dir, "shot.png", b"\x89PNG")
    missing = upload_dir / f"{_UUID}_gone.pdf"
    monkeypatch.setattr(ad, "extract_pdf_segments", lambda *a, **k: pytest.fail("extracted"))

    assert ad.attachment_document_blocks([str(txt), str(png), str(missing), ""]) == []


def test_oversized_document_is_refused_without_parsing(upload_dir, monkeypatch):
    path = _upload(upload_dir, "big.pdf")
    # Measured on the opened descriptor, not by a stat before the open.
    monkeypatch.setattr(ad._LIMITS, "max_document_bytes", 4)
    monkeypatch.setattr(ad, "extract_pdf_segments", lambda *a, **k: pytest.fail("extracted"))

    (block,) = ad.attachment_document_blocks([str(path)])

    assert block.startswith("[Attached document: big.pdf — too large to extract")


def test_docx_goes_through_the_bounded_office_extractor(upload_dir, monkeypatch):
    path = _upload(upload_dir, "plan.docx", b"PK\x03\x04data")
    calls: list[tuple[bytes, str, int]] = []

    def _fake(data, fmt, *, max_chars, deadline):
        calls.append((data, fmt, max_chars))
        return OfficeExtraction("Quarterly plan", None)

    monkeypatch.setattr(ad, "extract_office_text", _fake)

    (block,) = ad.attachment_document_blocks([str(path)])

    assert calls == [(b"PK\x03\x04data", "docx", ad._LIMITS.max_text_inject + 1)]
    assert block == "[Document: plan.docx]\nQuarterly plan\n[End of document]"


def test_an_office_extractor_timeout_tells_the_model(upload_dir, monkeypatch):
    path = _upload(upload_dir, "deep.docx", b"PK\x03\x04")
    monkeypatch.setattr(ad, "extract_office_text", lambda *a, **k: OfficeExtraction("", "timeout"))

    assert ad.attachment_document_blocks([str(path)]) == [
        "[Attached document: deep.docx — could not extract text]"
    ]


def test_every_document_of_a_message_shares_one_deadline(upload_dir, monkeypatch):
    paths = [str(_upload(upload_dir, f"d{i}.docx", b"PK\x03\x04")) for i in range(3)]
    deadlines: list[float] = []

    def _fake(data, fmt, *, max_chars, deadline):
        deadlines.append(deadline)
        return OfficeExtraction("t", None)

    monkeypatch.setattr(ad, "extract_office_text", _fake)

    ad.attachment_document_blocks(paths)

    assert len(deadlines) == 3 and len(set(deadlines)) == 1


def test_docx_uses_doc_parser(upload_dir):
    import docx

    doc = docx.Document()
    doc.add_paragraph("Quarterly plan")
    path = upload_dir / f"{_UUID}_plan.docx"
    doc.save(str(path))

    (block,) = ad.attachment_document_blocks([str(path)])

    assert block.startswith("[Document: plan.docx]\n")
    assert "Quarterly plan" in block


def test_blocks_are_bounded_and_deduplicated(upload_dir, monkeypatch):
    _stub_pdf(monkeypatch, PdfExtraction((("page 1", "t"),), False, None, 1))
    paths = [str(_upload(upload_dir, f"d{i}.pdf")) for i in range(ad._LIMITS.max_attachments + 3)]

    blocks = ad.attachment_document_blocks([paths[0], paths[0], *paths])

    assert len(blocks) == ad._LIMITS.max_attachments
    assert blocks[0].startswith("[Document: d0.pdf]")
    assert blocks[1].startswith("[Document: d1.pdf]")


def test_context_is_empty_without_documents(upload_dir):
    assert ad.attachment_document_context([]) == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX rlimits, as test_pdf_extract")
def test_a_real_pdf_is_extracted_end_to_end(upload_dir):
    path = _upload(upload_dir, "hello.pdf", text_pdf("Hello from the dashboard"))

    context = ad.attachment_document_context([str(path)])

    assert context.startswith("\n\n[Document: hello.pdf]\n--- Page 1 ---\n")
    assert "Hello from the dashboard" in context
    assert context.endswith("[End of document]\n\n")
