"""PDF text extraction child: ``python -m kiro_crew.pdf_extract_child``.

Runs under :data:`kiro_crew.sandbox.RLIMIT_PROFILE_EXTRACTOR`, spawned by
:func:`kiro_crew.pdf_extract.extract_pdf_segments`. It exists because
``pdfplumber`` offers no length limit: ``page.extract_text()`` first builds
``page.chars`` for the WHOLE page, so any cap written in the parent runs after
the memory is already committed. A Flate stream inflates ~1000:1 on repetitive
text, so one page of a 25 MB input can be gigabytes of characters. The only
ceiling that can precede the allocation is a kernel one on a process the
gateway can afford to lose -- this one.

Protocol (all policy numbers travel on argv, the document on stdin):

* argv: ``--max-chars=N --max-pages=M``.
* stdin: the PDF bytes. The parent has already bounded their size.
* stdout: one JSON object per line. ``{"label": "page 3", "text": "..."}`` for
  each page that yielded text, then exactly one terminal line:
  ``{"end": true, "truncated": bool, "pages": N}`` on success or
  ``{"error": "memory" | "parse", "detail": "<exception class>"}`` on failure.
  ``json.dumps`` keeps every line ASCII, so a line is at most six bytes per
  character of text plus a fixed frame -- what the parent's read ceiling is
  sized from.
* exit status: 0 after ``end``, :data:`EXIT_FAILED` after ``error``. A kill by
  the kernel (``RLIMIT_CPU`` -> SIGXCPU, the OOM killer -> SIGKILL) leaves no
  terminal line at all, which the parent reads as a resource failure too.

Bounds enforced here, inside the ceiling: at most ``max_pages`` pages are
opened, and at most ``max_chars`` characters are emitted in total, a page being
cut at the remaining budget. Both stop the loop and set ``truncated``.

Imports are deliberately minimal: this module is executed as ``__main__`` by
an interpreter whose address space is capped, and ``pdfplumber`` is imported
only once the arguments have parsed.
"""

from __future__ import annotations

import json
import sys

#: Exit status after an ``error`` line. Distinct from the interpreter's own 1
#: (an uncaught exception, which the parent treats as a protocol failure) and
#: from 2 (argparse usage error).
EXIT_FAILED = 3


def _parse_args(argv: list[str]) -> tuple[int, int]:
    max_chars = max_pages = -1
    for item in argv:
        name, sep, raw = item.partition("=")
        if not sep:
            raise SystemExit(f"pdf_extract_child: malformed argument {item!r}")
        try:
            value = int(raw)
        except ValueError:
            raise SystemExit(f"pdf_extract_child: {name} needs an integer") from None
        if value <= 0:
            raise SystemExit(f"pdf_extract_child: {name} must be positive")
        if name == "--max-chars":
            max_chars = value
        elif name == "--max-pages":
            max_pages = value
        else:
            raise SystemExit(f"pdf_extract_child: unknown argument {name!r}")
    if max_chars < 0 or max_pages < 0:
        raise SystemExit("pdf_extract_child: --max-chars and --max-pages are required")
    return max_chars, max_pages


def _is_memory_failure(exc: BaseException) -> bool:
    """Whether *exc* is, or wraps, a ``MemoryError``.

    ``pdfplumber`` re-raises whatever ``pdfminer`` threw as ``PdfminerException(e)``
    from inside an ``except`` block, so the ``MemoryError`` from a refused
    ``zlib.decompress`` buffer sits on ``__context__`` rather than being the
    exception itself. Walked with a step cap so a pathological chain cannot
    loop.
    """
    seen = 0
    current: BaseException | None = exc
    while current is not None and seen < 16:
        if isinstance(current, MemoryError):
            return True
        current = current.__cause__ or current.__context__
        seen += 1
    return False


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload))
    sys.stdout.write("\n")


def _release_page(page: object) -> None:
    # pdfplumber caches the parsed layout on each Page. Release it before parsing
    # the next page so a long document does not keep every page's layout
    # resident until the document closes. Page.close() also clears the text-map
    # cache when available; pdfplumber 0.10 only exposes flush_cache().
    close_page = getattr(page, "close", None)
    if close_page is None:
        close_page = getattr(page, "flush_cache")
    close_page()


def extract(data: bytes, *, max_chars: int, max_pages: int, pdfplumber_module: object) -> None:
    """Extract *data* to stdout under the caps. Raises whatever the parser raises."""
    import io

    open_pdf = getattr(pdfplumber_module, "open")
    budget = max_chars
    truncated = False
    seen = 0
    with open_pdf(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            if seen >= max_pages:
                truncated = True
                break
            seen += 1
            try:
                text = page.extract_text() or ""
            finally:
                _release_page(page)
            if not text:
                continue
            if len(text) > budget:
                text = text[:budget]
                truncated = True
            budget -= len(text)
            _emit({"label": f"page {seen}", "text": text})
            if budget <= 0:
                # A page that ended exactly at the cap is only whole if it was
                # the last one; that is settled by whether another page exists.
                truncated = truncated or seen < len(pdf.pages)
                break
    _emit({"end": True, "truncated": truncated, "pages": seen})


def main(argv: list[str] | None = None) -> int:
    max_chars, max_pages = _parse_args(sys.argv[1:] if argv is None else argv)
    data = sys.stdin.buffer.read()
    try:
        import pdfplumber

        extract(data, max_chars=max_chars, max_pages=max_pages, pdfplumber_module=pdfplumber)
    except Exception as exc:
        # The failed allocation is released by the time this runs, so the
        # small strings below fit even after a refused gigabyte.
        kind = "memory" if _is_memory_failure(exc) else "parse"
        _emit({"error": kind, "detail": type(exc).__name__})
        sys.stdout.flush()
        return EXIT_FAILED
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # The parent stopped reading (its deadline passed). Nothing to report.
        raise SystemExit(EXIT_FAILED)
