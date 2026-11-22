"""A bounded digest of a CSV or TSV the person attached in dashboard chat.

An upload reaches the agent as a bare path. With ``dashboard.attachment_sheet_digest``
on, the prompt builder follows each such path with the file's shape (header, a type
per column, a row count, a few rows), so the agent can plan before it opens the file.
Every step is bounded, it runs on the thread that builds the prompt (callers offload
it), and only files the dashboard stored in its own uploads folder qualify.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Bytes read from one file, data rows typed and shown, columns shown, characters per
#: shown cell, digests per prompt and characters per digest.
MAX_READ_BYTES = 256 * 1024
MAX_TYPED_ROWS = 200
SAMPLE_ROWS = 3
MAX_COLUMNS = 20
MAX_CELL_CHARS = 40
MAX_FILES = 3
MAX_DIGEST_CHARS = 4000

_DELIMITERS = {".csv": ",", ".tsv": "\t"}
#: Characters that may wrap a path in prose: one may precede it or trail it.
_WRAPPERS = "\"'`<>()[]{},;"
_BOOLEANS = ("true", "false")


def digest_enabled() -> bool:
    """Read ``dashboard.attachment_sheet_digest``; False on any failure."""
    try:
        from kiro_crew.config import KiroCrewConfig

        return bool(KiroCrewConfig.load().dashboard.attachment_sheet_digest)
    except Exception:
        logger.debug("sheet digest setting lookup failed; adding no digest")
        return False


def uploaded_sheet_paths(scan: str, spans: list[tuple[int, int]]) -> list[Path]:
    """Distinct CSV and TSV files directly in the uploads folder that *scan*'s user
    *spans* name, in order, at most :data:`MAX_FILES`. *scan* has literal text masked.

    A path is the uploads folder as written plus one stored name, so a space in the
    folder is kept; a stored name never holds whitespace.
    """
    from kiro_crew.config.paths import data_home
    from kiro_crew.hooks import validate_file_path

    folder = str(data_home() / "uploads")
    uploads = validate_file_path(folder) or ""
    found: list[Path] = []
    for start, end in spans if uploads else ():
        text = scan[start:end]
        at = text.find(folder)
        while at >= 0 and len(found) < MAX_FILES:
            tail = text[at + len(folder) :]
            lead = text[at - 1 : at] if at else " "
            at = text.find(folder, at + 1)
            if not (lead.isspace() or lead in _WRAPPERS) or tail[:1] not in ("/", "\\"):
                continue
            raw = folder + tail.split(maxsplit=1)[0].rstrip(_WRAPPERS)
            real = validate_file_path(raw) if Path(raw).suffix.lower() in _DELIMITERS else None
            if real and Path(real).parent == Path(uploads) and Path(real) not in found:
                found.append(Path(real))
    return found


def _cell(value: str) -> str:
    """One whitespace-folded cell, cut to :data:`MAX_CELL_CHARS`.

    Folding turns a newline inside a cell into a space, so a value can never
    start a line of its own in the prompt.
    """
    text = " ".join(value.split())
    return text if len(text) <= MAX_CELL_CHARS else text[:MAX_CELL_CHARS] + "…"


def _kind(value: str) -> str:
    """The type one non-empty cell reads as."""
    if value.lower() in _BOOLEANS:
        return "boolean"
    body = value[1:] if value[:1] in "+-" else value
    if body.isascii() and body.isdigit():
        return "integer"
    if any(ch.isdigit() for ch in value):
        try:
            float(value)
            return "number"
        except ValueError:
            pass
        try:
            _dt.datetime.fromisoformat(value)
            return "date"
        except ValueError:
            pass
    return "text"


def _column_type(kinds: set[str]) -> str:
    if len(kinds) == 1:
        return next(iter(kinds))
    return "empty" if not kinds else "number" if kinds == {"integer", "number"} else "text"


def digest_text(name: str, text: str, delimiter: str, *, partial: bool) -> str | None:
    """The digest of the sheet *text*, or None when it is not a readable table.

    *partial* is True when *text* is only the start of the file, so the row count
    is a floor. Returns None for an empty file or a field the reader refuses.
    """
    rows = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        header = next(rows, None)
        if not header:
            return None
        kinds: list[set[str]] = [set() for _ in header]
        samples: list[list[str]] = []
        count = 0
        for row in rows:
            if not row:
                continue
            count += 1
            if count <= SAMPLE_ROWS:
                samples.append([_cell(v) for v in row[:MAX_COLUMNS]])
            if count <= MAX_TYPED_ROWS:
                for index, value in enumerate(row[: len(kinds)]):
                    value = value.strip()
                    if value:
                        kinds[index].add(_kind(value))
    except csv.Error:
        return None
    shown = min(len(header), MAX_COLUMNS)
    columns = ", ".join(
        f"{_cell(header[i]) or '(blank)'} ({_column_type(kinds[i])})" for i in range(shown)
    )
    if len(header) > shown:
        columns += f", and {len(header) - shown} more columns"
    rows_note = f"{count}{'+' if partial else ''} data rows"
    if partial:
        rows_note += f" (first {MAX_READ_BYTES // 1024} KiB read)"
    lines = [f"[sheet digest: {name}]", f"{rows_note}; {len(header)} columns: {columns}"]
    lines += [f"sample: {' | '.join(sample)}" for sample in samples]
    if count > MAX_TYPED_ROWS:
        lines.append(f"types come from the first {MAX_TYPED_ROWS} data rows")
    return "\n".join(lines)[:MAX_DIGEST_CHARS]


def digest_file(path: Path) -> str | None:
    """The digest of the CSV or TSV at *path*, or None when there is none to give."""
    from kiro_crew.hooks import safe_read_file_bytes_nolink

    delimiter = _DELIMITERS.get(path.suffix.lower())
    if delimiter is None:
        return None
    try:
        size = path.stat().st_size
        data = safe_read_file_bytes_nolink(str(path), max_bytes=MAX_READ_BYTES, allow_truncate=True)
    except Exception:
        logger.debug("sheet digest: could not read %s", path.name, exc_info=True)
        return None
    if not data or b"\x00" in data:
        return None
    partial = size > len(data)
    if partial:
        # The read stopped mid-line: drop the unfinished last row (LF, CRLF or CR).
        data = data[: max(data.rfind(b"\n"), data.rfind(b"\r")) + 1]
    try:
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        text = data.decode("utf-8-sig", errors="replace")
        # Redacted in the attachment path's order, so a secret in a cell never reaches the prompt.
        text, _ = redact_exfiltration_urls(text)
        text, _ = redact_credentials(text)
        return digest_text(path.name, text, delimiter, partial=partial)
    except Exception:
        logger.debug("sheet digest: could not digest %s", path.name, exc_info=True)
        return None


def sheet_digests(scan: str, spans: list[tuple[int, int]]) -> str:
    """The digest blocks for the sheets named in *scan*, each after a blank line,
    or ``""`` when the setting is off or no sheet qualifies. Never raises."""
    try:
        paths = uploaded_sheet_paths(scan, spans)
        if not paths or not digest_enabled():
            return ""
        blocks = [block for block in (digest_file(path) for path in paths) if block]
        return "".join(f"\n\n{block}" for block in blocks)
    except Exception:
        logger.debug("sheet digest: skipped", exc_info=True)
        return ""
