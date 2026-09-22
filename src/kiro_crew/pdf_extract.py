"""Memory-bounded PDF text extraction, shared by file-grep and knowledge ingest.

``pdfplumber`` has no length limit: ``page.extract_text()`` builds ``page.chars``
for the whole page before any caller can measure it, and a Flate stream inflates
~1000:1 on repetitive text, so a single page of a size-capped input can commit
gigabytes. No check written in the gateway can run before that allocation. The
bound therefore lives one process down: :func:`extract_pdf_segments` spawns
``python -m kiro_crew.pdf_extract_child`` through :func:`sandbox.popen_limited`
under :data:`sandbox.RLIMIT_PROFILE_EXTRACTOR`, whose ``RLIMIT_AS`` makes the
oversized allocation fail INSIDE the child (``MemoryError`` -> a reported
``memory`` failure) and whose ``RLIMIT_CPU`` ends a parse that never finishes.
The gateway's own address space is never the thing that grows. Windows has no
rlimits, so there the child starts suspended and a Job object with the same
memory number is attached before it runs an instruction -- or it is killed
unrun (:func:`_windows_ceiling`). macOS accepts ``RLIMIT_AS`` and does not
enforce it, so the child also polices its own peak RSS against the same number
(``pdf_extract_child.start_rss_watchdog``) -- the ceiling there, a second layer
everywhere else.

This module sits beside ``doc_parser.py`` rather than under ``dashboard`` or
``knowledge`` so both callers import it without one package depending on the
other. Each caller owns its degradation: file-grep counts a failed document as
skipped and marks the answer partial; ingest records a per-file error.

Cost: a child is ~0.2 s of interpreter start plus the ``pdfplumber`` import,
paid once per document.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import IO

from kiro_crew import platform_compat
from kiro_crew.sandbox import (
    _EXTRACTOR_MAX_AS_BYTES,
    RLIMIT_PROFILE_EXTRACTOR,
    popen_limited,
    scrub_env,
)

logger = logging.getLogger(__name__)

#: Default page ceiling. A page yielding no text still costs a parse, so the
#: character cap alone cannot bound work on a scanned or empty document.
PDF_MAX_PAGES = 2000
#: Bytes every PDF starts with. Checked before a spawn so a mislabelled file
#: costs no child.
_PDF_MAGIC = b"%PDF-"
#: How long teardown waits for a child that ignored the deadline.
_KILL_WAIT_SECS = 2.0
#: Bytes of child stderr kept for the log line.
_STDERR_TAIL = 512
#: Bytes of fixed frame per stdout line (``{"label": "page NNNNN", "text": ""}``
#: plus the newline); the read ceiling is sized from it.
_LINE_FRAME_BYTES = 64
#: Worst-case JSON expansion of one ``str`` character: a non-BMP code point is
#: one character to ``len`` but a surrogate PAIR to ``json.dumps`` -- two
#: ``\\uXXXX`` escapes, twelve bytes.
_JSON_BYTES_PER_CHAR = 12


@dataclass(frozen=True)
class PdfExtraction:
    """What one extraction produced.

    ``segments`` are ``(label, text)`` pairs, one per page that yielded text;
    ``pages`` counts every page the child opened, text or not. ``truncated`` is
    True when a budget stopped the read short: the character or page cap, the
    deadline, or a child that failed. ``failure`` is ``None`` when the child ran
    to its terminal line, otherwise one of

    * ``memory`` -- the child hit its address-space ceiling (the bound firing),
    * ``cpu`` -- the child hit its CPU ceiling,
    * ``timeout`` -- the caller's deadline passed and the child was killed,
    * ``killed`` -- the kernel killed the child (OOM killer),
    * ``parse`` -- the parser rejected the document,
    * ``unavailable`` -- ``pdfplumber`` is not installed,
    * ``spawn`` -- the child could not be started,
    * ``unbounded`` -- Windows only: the Job object that stands in for
      ``RLIMIT_AS`` could not be attached, so the child was killed unrun rather
      than parse without a ceiling,
    * ``protocol`` / ``exit:N`` / ``signal:N`` -- the child ended without a
      terminal line, which a caller treats like any other resource failure.

    A failure carries no segments: text from a document whose parse did not
    finish is not something a caller can label whole or partial page by page.
    """

    segments: tuple[tuple[str, str], ...]
    truncated: bool
    failure: str | None
    pages: int = 0

    @property
    def resource_failure(self) -> bool:
        """Whether the child was stopped by a ceiling rather than by the document."""
        return self.failure is not None and self.failure not in ("parse", "unavailable")


def pdfplumber_available() -> bool:
    """Whether the extractor child will find ``pdfplumber`` -- without importing it."""
    try:
        return importlib.util.find_spec("pdfplumber") is not None
    except (ImportError, ValueError):
        return False


def _child_argv(max_chars: int, max_pages: int) -> list[str]:
    # ``-P`` keeps the spawn CWD off ``sys.path`` so a project directory cannot
    # shadow the child module; ``-m`` resolves it from the same install as the
    # gateway. The caps are policy numbers, fine on a world-readable argv. The
    # RSS number is the profile's own: the child polices its peak RSS against
    # it where the kernel has no address-space ceiling (macOS).
    return platform_compat.isolated_python_argv(
        "-P",
        "-m",
        "kiro_crew.pdf_extract_child",
        f"--max-chars={max_chars}",
        f"--max-pages={max_pages}",
        f"--max-rss={_EXTRACTOR_MAX_AS_BYTES}",
    )


def _failed(failure: str) -> PdfExtraction:
    return PdfExtraction((), True, failure)


def extract_pdf_segments(
    source: bytes | IO[bytes],
    *,
    max_chars: int,
    deadline: float,
    max_pages: int = PDF_MAX_PAGES,
) -> PdfExtraction:
    """Extract *source* as ``(label, text)`` page segments inside the ceiling.

    *source* is the document: bytes the caller already holds, or an open binary
    file the child reads directly as its stdin (no copy through the gateway).
    *max_chars* bounds the total text returned and *max_pages* the pages opened;
    *deadline* is a ``time.monotonic()`` instant after which the child is killed.
    Never raises for anything the document or the child did: every outcome is a
    :class:`PdfExtraction`, and the caller decides what a failure means to it.
    """
    if max_chars <= 0 or max_pages <= 0:
        raise ValueError("extract_pdf_segments: max_chars and max_pages must be positive")
    if isinstance(source, (bytes, bytearray)):
        head = bytes(source[: len(_PDF_MAGIC)])
    else:
        source.seek(0)
        head = source.read(len(_PDF_MAGIC))
        source.seek(0)
        # A buffered seek back inside its own buffer does not move the
        # descriptor, and the descriptor is what the child inherits as stdin.
        os.lseek(source.fileno(), 0, os.SEEK_SET)
    if head != _PDF_MAGIC:
        return _failed("parse")
    if not pdfplumber_available():
        return _failed("unavailable")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _failed("timeout")
    argv = _child_argv(max_chars, max_pages)
    stdin: int | IO[bytes]
    payload: bytes | None
    if isinstance(source, (bytes, bytearray)):
        stdin, payload = subprocess.PIPE, bytes(source)
    else:
        stdin, payload = source, None
    try:
        # The document is untrusted input to a parser, so the child gets the
        # same credential-scrubbed environment an agent-influenced spawn gets.
        proc = popen_limited(
            argv,
            profile=RLIMIT_PROFILE_EXTRACTOR,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=scrub_env(),
            # Windows has no rlimit, so the ceiling is a Job object attached
            # while the child has executed no instruction (0 on POSIX).
            creationflags=platform_compat.CREATE_SUSPENDED,
        )
    except OSError as exc:
        logger.warning("pdf_extract: cannot start extractor child: %s", exc)
        return _failed("spawn")
    if platform_compat.IS_WINDOWS:
        ceiling = _windows_ceiling(proc)
        if ceiling is not None:
            return _failed(ceiling)
    try:
        out, err = proc.communicate(payload, timeout=remaining)
    except subprocess.TimeoutExpired:
        _kill(proc)
        return _failed("timeout")
    return _decode(out, err, proc.returncode, max_chars=max_chars, max_pages=max_pages)


def _windows_ceiling(proc: subprocess.Popen[bytes]) -> str | None:
    """Attach the Job-object memory ceiling to a suspended child, then resume it.

    The Windows stand-in for the profile's ``RLIMIT_AS``: ``JobMemoryLimit`` at
    the same byte count, ``ActiveProcessLimit`` of one (this child spawns
    nothing). FAILS CLOSED, unlike the agent-host spawns, which log and run on:
    an extractor with no ceiling is the exposure this module exists to remove,
    and a document that cannot be bounded is a document that is not read. The
    child is killed before it executes an instruction, so nothing was parsed.
    Returns the failure kind, or ``None`` once the child is running under the
    ceiling.
    """
    if not platform_compat.apply_job_limits(
        proc.pid, max_procs=1, max_memory_bytes=_EXTRACTOR_MAX_AS_BYTES
    ):
        logger.warning("pdf_extract: no memory ceiling could be attached; document skipped")
        _kill(proc)
        return "unbounded"
    if not platform_compat.resume_process_main_thread(proc.pid):
        logger.warning("pdf_extract: extractor child could not be resumed")
        _kill(proc)
        return "spawn"
    return None


def _kill(proc: subprocess.Popen[bytes]) -> None:
    proc.kill()
    try:
        proc.communicate(timeout=_KILL_WAIT_SECS)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass


def _decode(
    out: bytes, err: bytes, returncode: int | None, *, max_chars: int, max_pages: int
) -> PdfExtraction:
    """Turn the child's exit status and stdout into a :class:`PdfExtraction`.

    The read ceiling is applied first: the child bounds its own output, but the
    parent does not take that on faith -- a stream past what the caps allow is a
    protocol failure, not text.
    """
    ceiling = max_chars * _JSON_BYTES_PER_CHAR + (max_pages + 1) * _LINE_FRAME_BYTES
    if len(out) > ceiling:
        logger.warning("pdf_extract: child wrote %d bytes past the ceiling", len(out) - ceiling)
        return _failed("protocol")
    if returncode is not None and returncode < 0:
        sig = -returncode
        if os.name == "posix" and sig == getattr(signal, "SIGXCPU", None):
            return _failed("cpu")
        if sig == getattr(signal, "SIGKILL", None):
            return _failed("killed")
        return _failed(f"signal:{sig}")
    segments: list[tuple[str, str]] = []
    end: dict | None = None
    used = 0
    for raw in out.split(b"\n"):
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            return _protocol(err, "unparseable line")
        if not isinstance(record, dict):
            return _protocol(err, "non-object line")
        if "error" in record:
            kind = record.get("error")
            detail = record.get("detail")
            if kind not in ("memory", "parse"):
                return _protocol(err, f"unknown error kind {kind!r}")
            logger.info("pdf_extract: child reported %s failure (%s)", kind, detail)
            return _failed(kind)
        if record.get("end") is True:
            end = record
            break
        label, text = record.get("label"), record.get("text")
        if not isinstance(label, str) or not isinstance(text, str):
            return _protocol(err, "segment without label/text")
        used += len(text)
        if used > max_chars or len(segments) >= max_pages:
            return _protocol(err, "child exceeded its own caps")
        segments.append((label, text))
    if end is None or returncode != 0:
        return _protocol(err, f"exit {returncode} without a terminal line")
    pages = end.get("pages")
    if not isinstance(pages, int) or pages < 0 or pages > max_pages:
        return _protocol(err, "terminal line with a bad page count")
    return PdfExtraction(tuple(segments), bool(end.get("truncated")), None, pages)


def _protocol(err: bytes, why: str) -> PdfExtraction:
    tail = err[-_STDERR_TAIL:].decode("utf-8", "replace")
    logger.warning("pdf_extract: %s; stderr tail: %r", why, tail)
    return _failed("protocol")
