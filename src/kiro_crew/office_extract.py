"""Deadline-bounded ``.docx`` / ``.pptx`` text extraction in a child process.

:func:`extract_office_text` runs :func:`kiro_crew.doc_parser.extract_text` in
``python -m kiro_crew.office_extract_child``, spawned through
:func:`sandbox.popen_limited` under :data:`sandbox.RLIMIT_PROFILE_EXTRACTOR`. The
child is killed when the caller's deadline passes, so a document whose parse would
run for a long time costs the caller at most the time left before that deadline.
The spawn, the Windows Job-object ceiling and the kill are the ones
:mod:`kiro_crew.pdf_extract` uses.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass

from kiro_crew import platform_compat
from kiro_crew.office_extract_child import FORMATS
from kiro_crew.pdf_extract import _kill, _windows_ceiling
from kiro_crew.sandbox import (
    _EXTRACTOR_MAX_AS_BYTES,
    RLIMIT_PROFILE_EXTRACTOR,
    popen_limited,
    scrub_env,
)

logger = logging.getLogger(__name__)

#: Bytes every OOXML container (a ZIP) starts with.
_ZIP_MAGIC = b"PK\x03\x04"
#: Bytes of child stderr kept for the log line.
_STDERR_TAIL = 512
#: Fixed frame of the one stdout line (``{"text": ""}`` plus the newline).
_LINE_FRAME_BYTES = 64
#: Worst-case JSON expansion of one ``str`` character (a surrogate pair).
_JSON_BYTES_PER_CHAR = 12


@dataclass(frozen=True)
class OfficeExtraction:
    """What one extraction produced.

    ``text`` is empty when ``failure`` is set. ``failure`` is ``None`` when the
    child answered, otherwise one of ``timeout``, ``memory``, ``cpu``, ``killed``,
    ``parse``, ``spawn``, ``unbounded`` (Windows: no memory ceiling could be
    attached), or ``protocol``.
    """

    text: str
    failure: str | None


def _failed(failure: str) -> OfficeExtraction:
    return OfficeExtraction("", failure)


def _child_argv(fmt: str, max_chars: int) -> list[str]:
    return platform_compat.isolated_python_argv(
        "-P",
        "-m",
        "kiro_crew.office_extract_child",
        f"--format={fmt}",
        f"--max-chars={max_chars}",
        f"--max-rss={_EXTRACTOR_MAX_AS_BYTES}",
    )


def extract_office_text(
    data: bytes, fmt: str, *, max_chars: int, deadline: float
) -> OfficeExtraction:
    """Extract the text of the ``.docx`` / ``.pptx`` *data* inside the ceiling.

    *fmt* is ``"docx"`` or ``"pptx"``. *max_chars* bounds the text returned;
    *deadline* is a ``time.monotonic()`` instant after which the child is killed.
    Never raises for anything the document or the child did.
    """
    if fmt not in FORMATS:
        raise ValueError(f"extract_office_text: unsupported format {fmt!r}")
    if max_chars <= 0:
        raise ValueError("extract_office_text: max_chars must be positive")
    if not data.startswith(_ZIP_MAGIC):
        return _failed("parse")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _failed("timeout")
    try:
        proc = popen_limited(
            _child_argv(fmt, max_chars),
            profile=RLIMIT_PROFILE_EXTRACTOR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=scrub_env(),
            creationflags=platform_compat.CREATE_SUSPENDED,
        )
    except OSError as exc:
        logger.warning("office_extract: cannot start extractor child: %s", exc)
        return _failed("spawn")
    if platform_compat.IS_WINDOWS:
        ceiling = _windows_ceiling(proc)
        if ceiling is not None:
            return _failed(ceiling)
    try:
        out, err = proc.communicate(data, timeout=remaining)
    except subprocess.TimeoutExpired:
        _kill(proc)
        return _failed("timeout")
    return _decode(out, err, proc.returncode, max_chars=max_chars)


def _decode(out: bytes, err: bytes, returncode: int | None, *, max_chars: int) -> OfficeExtraction:
    if len(out) > max_chars * _JSON_BYTES_PER_CHAR + _LINE_FRAME_BYTES:
        return _protocol(err, "child wrote past the ceiling")
    if returncode is not None and returncode < 0:
        import signal

        sig = -returncode
        if sig == getattr(signal, "SIGXCPU", None):
            return _failed("cpu")
        if sig == getattr(signal, "SIGKILL", None):
            return _failed("killed")
        return _failed(f"signal:{sig}")
    try:
        record = json.loads(out)
    except ValueError:
        return _protocol(err, f"exit {returncode} without a result line")
    if not isinstance(record, dict):
        return _protocol(err, "non-object result")
    if "error" in record:
        kind = record.get("error")
        if kind not in ("memory", "parse"):
            return _protocol(err, f"unknown error kind {kind!r}")
        logger.info("office_extract: child reported %s failure (%s)", kind, record.get("detail"))
        return _failed(str(kind))
    text = record.get("text")
    if returncode != 0 or not isinstance(text, str) or len(text) > max_chars:
        return _protocol(err, "malformed result")
    return OfficeExtraction(text, None)


def _protocol(err: bytes, why: str) -> OfficeExtraction:
    tail = err[-_STDERR_TAIL:].decode("utf-8", "replace")
    logger.warning("office_extract: %s; stderr tail: %r", why, tail)
    return _failed("protocol")
