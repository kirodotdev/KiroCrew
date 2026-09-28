"""PDF fixtures built in-test, shared by the extractor, file-grep and reader tests.

Nothing here is a committed binary: a structurally valid one-page PDF is a few
hundred bytes of literal objects, and the Flate bomb is a compressor fed a
repeating page over and over -- the compressed output stays small while the
inflated stream is what the memory bound has to refuse.
"""

from __future__ import annotations

import io
import zlib

#: Uncompressed size of the bomb's content stream. The extractor child runs under
#: ``RLIMIT_AS`` 1 GiB and needs ~270 MB of it before the first page, so a stream
#: whose inflate buffer alone is larger than the whole ceiling fails inside
#: ``zlib.decompress`` on the first call, before ``pdfminer`` parses a token --
#: fast, and independent of how much of the ceiling the interpreter already used.
BOMB_INFLATED_BYTES = 1200 * 1024 * 1024


def make_pdf(stream: bytes, *, compressed: bool = False) -> bytes:
    """A complete single-page PDF (catalog, page tree, page, content, font, xref).

    *stream* is the page's content stream; with *compressed* it is already
    Flate-encoded and declared so, which is how a real writer stores it.
    """
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    ]
    filt = b" /Filter /FlateDecode" if compressed else b""
    objs.append(b"<< /Length %d%s >>\nstream\n%s\nendstream" % (len(stream), filt, stream))
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (i, body))
    xref_pos = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objs) + 1))
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref_pos)
    )
    return out.getvalue()


def text_pdf(text: str) -> bytes:
    """A one-page PDF whose page shows *text* (ASCII, no parentheses)."""
    stream = ("BT /F1 24 Tf 72 700 Td (%s) Tj ET" % text).encode("ascii")
    return make_pdf(stream)


def flate_bomb_pdf(inflated_bytes: int = BOMB_INFLATED_BYTES) -> bytes:
    """A one-page PDF whose Flate content stream inflates to *inflated_bytes*.

    Built through a streaming compressor so this process never holds the
    inflated bytes: the input is a 1 MiB block of spaces repeated, which Flate
    stores as back-references, so the compressed stream is a few MB -- well under
    file-grep's 25 MB byte cap, which is the point of the exposure.
    """
    block = b" " * (1024 * 1024)
    compressor = zlib.compressobj(1)
    parts = [compressor.compress(block) for _ in range(inflated_bytes // len(block))]
    parts.append(compressor.flush())
    return make_pdf(b"".join(parts), compressed=True)
