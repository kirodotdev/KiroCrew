"""PDF text extraction child: ``python -m kiro_crew.pdf_extract_child``.

Runs under :data:`kiro_crew.sandbox.RLIMIT_PROFILE_EXTRACTOR`, spawned by
:func:`kiro_crew.pdf_extract.extract_pdf_segments`. It exists because
``pdfplumber`` offers no length limit: ``page.extract_text()`` first builds
``page.chars`` for the WHOLE page, so any cap written in the parent runs after
the memory is already committed. A Flate stream inflates ~1000:1 on repetitive
text, so one page of a 25 MB input can be gigabytes of characters. The only
ceiling that can precede the allocation is one on a process the gateway can
afford to lose -- this one.

Three ceilings, by platform: the kernel's ``RLIMIT_AS`` (Linux, applied by the
spawn shim), a Job object (Windows, attached by the parent), and this process's
OWN peak-RSS watchdog (:func:`start_rss_watchdog`), which is the ceiling where
the kernel offers none -- macOS accepts ``RLIMIT_AS`` and does not enforce it --
and a second layer everywhere else.

Protocol (all policy numbers travel on argv, the document on stdin):

* argv: ``--max-chars=N --max-pages=M --max-rss=BYTES``.
* stdin: the PDF bytes. The parent has already bounded their size.
* stdout: one JSON object per line. ``{"label": "page 3", "text": "..."}`` for
  each page that yielded text, then exactly one terminal line:
  ``{"end": true, "truncated": bool, "pages": N}`` on success or
  ``{"error": "memory" | "parse", "detail": "<exception class | rss>"}`` on
  failure. ``json.dumps`` keeps every line ASCII, so a line is at most twelve
  bytes per character of text plus a fixed frame -- what the parent's read
  ceiling is sized from.
* exit status: 0 after ``end``, :data:`EXIT_FAILED` after ``error``. A kill by
  the kernel (``RLIMIT_CPU`` -> SIGXCPU, the OOM killer -> SIGKILL) leaves no
  terminal line at all, which the parent reads as a resource failure too.

Bounds enforced here, inside the ceiling: at most ``max_pages`` pages are
opened, and at most ``max_chars`` characters are emitted in total, a page being
cut at the remaining budget. Both stop the loop and set ``truncated``.

Imports are deliberately minimal: this module is executed as ``__main__`` by
an interpreter whose address space may be capped, and ``pdfplumber`` is
imported only once the arguments have parsed.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Callable

try:
    import resource as _resource
except ImportError:  # pragma: no cover - Windows has no getrusage; the Job object bounds it
    _resource = None  # type: ignore[assignment]

#: Exit status after an ``error`` line. Distinct from the interpreter's own 1
#: (an uncaught exception, which the parent treats as a protocol failure) and
#: from 2 (argparse usage error).
EXIT_FAILED = 3
#: How often the watchdog samples peak RSS. A refused inflate grows RSS at
#: memory bandwidth, so the overshoot past the ceiling is this interval's worth
#: of writes -- tens of MB, not the gigabyte the ceiling refuses.
_RSS_SAMPLE_SECS = 0.02

_ARGS = ("--max-chars", "--max-pages", "--max-rss")

#: Serialises stdout between the extraction loop and the watchdog, so a kill
#: never lands mid-line and hands the parent a half-written record.
_emit_lock = threading.Lock()


def _parse_args(argv: list[str]) -> dict[str, int]:
    values: dict[str, int] = {}
    for item in argv:
        name, sep, raw = item.partition("=")
        if not sep or name not in _ARGS:
            raise SystemExit(f"pdf_extract_child: unknown or malformed argument {item!r}")
        try:
            value = int(raw)
        except ValueError:
            raise SystemExit(f"pdf_extract_child: {name} needs an integer") from None
        if value <= 0:
            raise SystemExit(f"pdf_extract_child: {name} must be positive")
        values[name] = value
    missing = [name for name in _ARGS if name not in values]
    if missing:
        raise SystemExit(f"pdf_extract_child: missing {', '.join(missing)}")
    return values


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


def _emit(payload: dict, *, flush: bool = False) -> None:
    with _emit_lock:
        sys.stdout.write(json.dumps(payload))
        sys.stdout.write("\n")
        if flush:
            sys.stdout.flush()


def peak_rss_bytes() -> int | None:
    """This process's peak resident size in bytes, or ``None`` where unreadable.

    ``ru_maxrss`` is KiB on Linux and BYTES on macOS -- the one place the two
    differ, and the reason this is not a bare ``getrusage`` call at the use site.
    """
    if _resource is None:
        return None
    peak = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def watch_rss(
    limit: int,
    *,
    sample: Callable[[], int | None] = peak_rss_bytes,
    terminate: Callable[[int], None] = os._exit,
    interval: float = _RSS_SAMPLE_SECS,
) -> None:
    """Watchdog body: end the process once its peak RSS passes *limit*.

    Reports the same ``memory`` failure the kernel ceiling produces, so the
    parent and its callers see one outcome for one cause. ``os._exit`` rather
    than ``sys.exit``: the main thread may be inside a C-level inflate holding
    the very memory this refuses, and nothing about this process is worth
    unwinding.
    """
    while True:
        time.sleep(interval)
        peak = sample()
        if peak is not None and peak > limit:
            _emit({"error": "memory", "detail": "rss"}, flush=True)
            terminate(EXIT_FAILED)
            return


def start_rss_watchdog(limit: int) -> bool:
    """Start the peak-RSS watchdog thread; ``False`` where RSS cannot be read."""
    if peak_rss_bytes() is None:
        return False
    threading.Thread(target=watch_rss, args=(limit,), name="rss-watchdog", daemon=True).start()
    return True


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
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    start_rss_watchdog(args["--max-rss"])
    data = sys.stdin.buffer.read()
    try:
        import pdfplumber

        extract(
            data,
            max_chars=args["--max-chars"],
            max_pages=args["--max-pages"],
            pdfplumber_module=pdfplumber,
        )
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
