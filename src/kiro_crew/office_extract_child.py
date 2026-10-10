"""OOXML text extraction child: ``python -m kiro_crew.office_extract_child``.

Runs under :data:`kiro_crew.sandbox.RLIMIT_PROFILE_EXTRACTOR`, spawned by
:func:`kiro_crew.office_extract.extract_office_text`. It runs
:func:`kiro_crew.doc_parser.extract_text` on a ``.docx`` / ``.pptx`` read from
stdin. The parent kills it at its deadline, and the profile's ``RLIMIT_CPU`` and
``RLIMIT_AS`` bound it where the parent cannot.

Protocol (policy numbers on argv, the document on stdin):

* argv: ``--format=docx|pptx --max-chars=N --max-rss=BYTES``.
* stdin: the document bytes. The parent has already bounded their size.
* stdout: exactly one JSON line. ``{"text": "..."}`` on success, or
  ``{"error": "memory" | "parse", "detail": "<exception class | rss>"}`` on
  failure. ``json.dumps`` keeps the line ASCII, so it is at most twelve bytes per
  character of text plus a fixed frame.
* exit status: 0 after ``text``, :data:`EXIT_FAILED` after ``error``. A kill by
  the kernel leaves no line, which the parent reads as a resource failure.

The peak-RSS watchdog is :func:`kiro_crew.pdf_extract_child.start_rss_watchdog`,
the ceiling on macOS, where ``RLIMIT_AS`` is accepted and not enforced.
"""

from __future__ import annotations

import io
import json
import sys

from kiro_crew.pdf_extract_child import EXIT_FAILED, start_rss_watchdog

#: Formats this child extracts.
FORMATS = ("docx", "pptx")
_INT_ARGS = ("--max-chars", "--max-rss")


def _parse_args(argv: list[str]) -> tuple[str, int, int]:
    values: dict[str, str] = {}
    for item in argv:
        name, sep, raw = item.partition("=")
        if not sep or name not in ("--format", *_INT_ARGS):
            raise SystemExit(f"office_extract_child: unknown or malformed argument {item!r}")
        values[name] = raw
    missing = [name for name in ("--format", *_INT_ARGS) if name not in values]
    if missing:
        raise SystemExit(f"office_extract_child: missing {', '.join(missing)}")
    fmt = values["--format"]
    if fmt not in FORMATS:
        raise SystemExit(f"office_extract_child: --format must be one of {', '.join(FORMATS)}")
    numbers: list[int] = []
    for name in _INT_ARGS:
        try:
            value = int(values[name])
        except ValueError:
            raise SystemExit(f"office_extract_child: {name} needs an integer") from None
        if value <= 0:
            raise SystemExit(f"office_extract_child: {name} must be positive")
        numbers.append(value)
    return fmt, numbers[0], numbers[1]


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload))
    sys.stdout.write("\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    fmt, max_chars, max_rss = _parse_args(sys.argv[1:] if argv is None else argv)
    start_rss_watchdog(max_rss)
    data = sys.stdin.buffer.read()
    try:
        from kiro_crew.doc_parser import extract_text

        name = f"attachment.{fmt}"
        text = extract_text(name, filename=name, max_chars=max_chars, fileobj=io.BytesIO(data))
    except MemoryError:
        _emit({"error": "memory", "detail": "MemoryError"})
        return EXIT_FAILED
    except Exception as exc:
        _emit({"error": "parse", "detail": type(exc).__name__})
        return EXIT_FAILED
    _emit({"text": text[:max_chars]})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # The parent stopped reading (its deadline passed). Nothing to report.
        raise SystemExit(EXIT_FAILED)
