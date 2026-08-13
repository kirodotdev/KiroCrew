"""Document text extraction for .docx, .pdf, and .pptx files.

Uses stdlib zipfile plus a hardened XML parser (defusedxml) for .docx and
.pptx since these are ZIP archives containing XML.  PDF extraction uses
a best-effort binary text scan (no third-party deps required).

The XML comes from user-supplied uploads, so parsing goes through
defusedxml rather than the stdlib xml.etree parser: the stdlib one resolves
external entities, exposing an XXE (local-file disclosure / entity-expansion
DoS) on a crafted document. defusedxml.fromstring is a drop-in that rejects
DTDs and external entities.

All functions accept a file path and return extracted text as a string.
They never raise — on failure they return an empty string and log a warning.
"""

from __future__ import annotations

import logging
import re
import zipfile
import zlib
from pathlib import Path

# Optional so a stale install (git pull without `pip install -e .`) degrades
# to "docx/pptx parsing unavailable" instead of killing every CLI entry at
# import time — this module sits on the gateway's import path. NEVER fall
# back to stdlib xml.etree: it resolves external entities (XXE).
try:
    from defusedxml.ElementTree import fromstring as _xml_fromstring
except ModuleNotFoundError:  # pragma: no cover — exercised via monkeypatch
    _xml_fromstring = None  # type: ignore[assignment]

from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

# ── Size limits ──

_MAX_ZIP_ENTRY = 50 * 1024 * 1024  # 50 MB per ZIP entry (decompressed)
_MAX_DECOMPRESS = 50 * 1024 * 1024  # 50 MB for zlib decompression

# ── Public API ──

# Mimetypes that map to document parsers
DOC_MIMETYPES: dict[str, str] = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/pdf": "pdf",
    "application/msword": "docx",
    "application/vnd.ms-powerpoint": "pptx",
}

# File extensions that map to document parsers
DOC_EXTENSIONS: set[str] = {".docx", ".pdf", ".pptx"}


def is_parseable_document(mimetype: str = "", filename: str = "") -> bool:
    """Return True if the file can be parsed by this module."""
    if mimetype in DOC_MIMETYPES:
        return True
    ext = Path(filename).suffix.lower() if filename else ""
    return ext in DOC_EXTENSIONS


def extract_text(path: str, mimetype: str = "", filename: str = "") -> str:
    """Extract readable text from a document file.

    Detects format from *mimetype* first, then falls back to file extension.
    Returns empty string on any failure.
    """
    if is_sensitive_path(path):
        logger.warning("Refusing to read sensitive path: %s", path)
        sel().log_api_access(
            caller="doc_parser",
            operation="extract_text",
            outcome="denied",
            source="local",
            resources=path,
            error="sensitive_path_rejected",
        )
        return ""
    fmt = DOC_MIMETYPES.get(mimetype, "")
    if not fmt:
        ext = Path(filename or path).suffix.lower()
        fmt = {".docx": "docx", ".pptx": "pptx", ".pdf": "pdf"}.get(ext, "")
    if not fmt:
        return ""
    if fmt in ("docx", "pptx") and _xml_fromstring is None:
        logger.warning(
            "Cannot parse %s: defusedxml is not installed (checkout newer "
            "than installed deps?). Fix: pip install -e .",
            filename or path,
        )
        return ""
    try:
        if fmt == "docx":
            return _extract_docx(path)
        if fmt == "pptx":
            return _extract_pptx(path)
        if fmt == "pdf":
            return _extract_pdf(path)
    except Exception:
        logger.warning("Failed to extract text from %s", path, exc_info=True)
    return ""


# ── Decompression safety ──


def _safe_decompress(data: bytes, max_size: int | None = None) -> bytes:
    """Decompress zlib data with an output size limit to prevent zip bombs."""
    if max_size is None:
        max_size = _MAX_DECOMPRESS
    dobj = zlib.decompressobj()
    result = dobj.decompress(data, max_size)
    if dobj.unconsumed_tail:
        raise ValueError("decompressed stream exceeds size limit")
    return result


def _read_zip_entry(
    zf: zipfile.ZipFile, name: str, max_size: int | None = None,
) -> bytes | None:
    """Read a ZIP entry with an *actual* decompressed-size limit.

    Returns ``None`` if the real decompressed output exceeds *max_size*,
    regardless of what the ZIP header declares.
    """
    if max_size is None:
        max_size = _MAX_ZIP_ENTRY
    with zf.open(name) as f:
        data = f.read(max_size + 1)
        if len(data) > max_size:
            logger.warning("ZIP entry too large (actual): %s", name)
            return None
        return data


# ── DOCX parser (Office Open XML) ──

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _extract_docx(path: str) -> str:
    """Extract text from a .docx file (ZIP containing word/document.xml).

    Must only be called from extract_text() which enforces is_sensitive_path().
    """
    assert _xml_fromstring is not None  # extract_text() gates the None case
    if is_sensitive_path(path):
        return ""
    paragraphs: list[str] = []
    with zipfile.ZipFile(path, "r") as zf:
        if "word/document.xml" not in zf.namelist():
            return ""
        data = _read_zip_entry(zf, "word/document.xml")
        if data is None:
            return ""
        root = _xml_fromstring(data)
        for para in root.iter(f"{_W_NS}p"):
            texts: list[str] = []
            for t_elem in para.iter(f"{_W_NS}t"):
                if t_elem.text:
                    texts.append(t_elem.text)
            if texts:
                paragraphs.append("".join(texts))
    return "\n".join(paragraphs)


# ── PPTX parser (Office Open XML) ──

_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")


def _extract_pptx(path: str) -> str:
    """Extract text from a .pptx file (ZIP containing ppt/slides/*.xml).

    Must only be called from extract_text() which enforces is_sensitive_path().
    """
    assert _xml_fromstring is not None  # extract_text() gates the None case
    if is_sensitive_path(path):
        return ""
    slides: list[tuple[int, str]] = []
    with zipfile.ZipFile(path, "r") as zf:
        slide_names = sorted(
            (n for n in zf.namelist() if _SLIDE_RE.match(n)),
            key=lambda n: int(_SLIDE_RE.match(n).group(1)),  # type: ignore[union-attr]
        )
        for slide_name in slide_names:
            data = _read_zip_entry(zf, slide_name)
            if data is None:
                continue
            num = int(_SLIDE_RE.match(slide_name).group(1))  # type: ignore[union-attr]
            root = _xml_fromstring(data)
            texts: list[str] = []
            for t_elem in root.iter(f"{_A_NS}t"):
                if t_elem.text:
                    texts.append(t_elem.text)
            if texts:
                slides.append((num, "\n".join(texts)))
    parts: list[str] = []
    for num, text in slides:
        parts.append(f"--- Slide {num} ---\n{text}")
    return "\n\n".join(parts)


# ── PDF parser (best-effort binary text extraction) ──

# Matches text between BT (begin text) and ET (end text) PDF operators,
# then extracts parenthesized string literals.  This is a rough heuristic
# that works for many simple PDFs but won't handle CIDFont encodings or
# compressed streams.
_PDF_TEXT_RE = re.compile(rb"\(([^)]*)\)")


def _extract_pdf(path: str) -> str:
    """Best-effort text extraction from a PDF using binary scanning.

    Must only be called from extract_text() which enforces is_sensitive_path().
    """
    if is_sensitive_path(path):
        return ""
    raw = Path(path).read_bytes()
    # Try to decompress FlateDecode streams first
    chunks: list[bytes] = []
    # Scan for stream..endstream blocks and try zlib decompression
    stream_re = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
    for m in stream_re.finditer(raw):
        try:
            decompressed = _safe_decompress(m.group(1))
            chunks.append(decompressed)
        except (zlib.error, OSError):
            # Not valid zlib — might be an uncompressed text stream
            chunks.append(m.group(1))
        except ValueError:
            # Size limit exceeded — skip entirely (zip bomb defense)
            logger.warning("PDF stream exceeded decompression limit in %s", path)
    if not chunks:
        chunks = [raw]
    # Extract parenthesized text strings from all chunks
    text_parts: list[str] = []
    for chunk in chunks:
        for m in _PDF_TEXT_RE.finditer(chunk):
            try:
                decoded = m.group(1).decode("utf-8", errors="replace")
                # Skip very short fragments that are likely operators
                if len(decoded) > 1:
                    text_parts.append(decoded)
            except Exception:
                pass
    if not text_parts:
        return ""
    # Join and clean up
    result = " ".join(text_parts)
    # Collapse multiple spaces
    result = re.sub(r" {2,}", " ", result)
    return result.strip()
