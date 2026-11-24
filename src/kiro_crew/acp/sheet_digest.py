"""A bounded digest of a CSV, TSV or workbook the person attached in dashboard chat.

An upload reaches the agent as a bare path. With ``dashboard.attachment_sheet_digest``
on, the prompt builder follows each such path with the file's shape (header, a type
per column, a row count, a few rows), so the agent can plan before it opens the file.
A workbook gets one such block per sheet, read through the shared workbook walk.
Every step is bounded, it runs on the thread that builds the prompt (callers offload
it), and only files the dashboard stored in its own uploads folder qualify.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import itertools
import logging
import time
from collections.abc import Iterable
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
#: A workbook: the file size read, sheets shown, seconds of reading, and the archive
#: ceilings (members, bytes once expanded) checked before the reader opens it.
MAX_WORKBOOK_BYTES = 8 * 1024 * 1024
MAX_SHEETS = 5
WORKBOOK_WALL_SECS = 5
WORKBOOK_MAX_MEMBERS = 1024
WORKBOOK_MAX_EXPANDED_BYTES = 64 * 1024 * 1024
_WORKBOOK_STRIDE = 256

_DELIMITERS = {".csv": ",", ".tsv": "\t"}
_WORKBOOKS = (".xlsx", ".xlsm", ".xltx", ".xltm")
#: Characters that may wrap a path in prose: one may precede it or trail it.
_WRAPPERS = "\"'`<>()[]{},;"
#: What may follow a path at the end of a sentence, after the wrappers above.
_TRAILERS = _WRAPPERS + ".:!?"
#: Shown instead of cells when redaction moves a row or cell break (a CSV or a workbook).
_SPANS_NOTE = "column names, types and samples left out: a redacted value spans rows or cells"
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
    """Distinct CSV, TSV and workbook files directly in the uploads folder that
    *scan*'s user *spans* name, in order, at most :data:`MAX_FILES`. *scan* has
    literal text masked.

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
            raw = folder + tail.split(maxsplit=1)[0].rstrip(_TRAILERS)
            sheet = Path(raw).suffix.lower() in (*_DELIMITERS, *_WORKBOOKS)
            real = validate_file_path(raw) if sheet else None
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
        # float() reads "12_34" as a number; a code with an underscore is not one.
        if "_" not in value:
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
    if not kinds:
        return "empty"
    if kinds == {"integer", "number"}:
        return "number"
    return "text"


def _redacted(text: str) -> str:
    """*text* with exfiltration URLs and credentials removed, in the attachment path's order."""
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    text, _ = redact_exfiltration_urls(text)
    return redact_credentials(text)[0]


def digest_text(name: str, text: str, delimiter: str, *, partial: bool) -> str | None:
    """The digest of the sheet *text*, or None when it is not a readable table.

    *partial* is True when *text* is only the start of the file, so the row count
    is a floor. Returns None for an empty file or a field the reader refuses.

    Every number comes from the rows of *text* as written. The cells shown come from
    the same rows read after redaction; if that moves a row or cell break (a key
    spread over lines), the block gives only the counts and a note.
    """

    def read(source: str) -> list[list[str]]:
        return list(csv.reader(io.StringIO(source, newline=""), delimiter=delimiter))

    try:
        counted = read(text)
        if not counted or not counted[0]:
            return None
        rows = read(_redacted(text))
        read_note = f"first {MAX_READ_BYTES // 1024} KiB read"
        if [len(row) for row in rows] != [len(row) for row in counted]:
            data = sum(1 for row in counted[1:] if row)
            return _counts_only(name, data, len(counted[0]), partial=partial, read=read_note)
        return _shape(name, rows[0], iter(rows[1:]), partial=partial, read=read_note)
    except csv.Error:
        return None


def _csv_delimiter(text: str) -> str:
    """``;`` when the ``.csv`` *text* is plainly semicolon-separated, else ``,``.

    The first record, read with the comma rules ``digest_text`` uses, decides first: two or
    more fields keep ``,``. Only a one-field record lets ``;`` apply, and then the typing
    sample (the first :data:`MAX_TYPED_ROWS` data records, blank lines apart) must read as
    the same two or more cells in every record with ``;``, so a quoted ``;`` never counts.
    """
    try:
        first = next((r for r in csv.reader(io.StringIO(text, newline="")) if r), [])
        if len(first) > 1:
            return ","
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=";")
        rows = [row for row in itertools.islice(reader, MAX_TYPED_ROWS + 1) if row]
    except csv.Error:
        return ","
    width = len(rows[0]) if rows else 0
    return ";" if width > 1 and all(len(row) == width for row in rows) else ","


def _counts_only(name: str, count: int, width: int, *, partial: bool, read: str) -> str:
    """A block with the row count (a floor when *partial*), the column count and a note."""
    floor = f"+ data rows ({read})" if partial else " data rows"
    return f"[sheet digest: {name}]\n{count}{floor}; {width} columns\n{_SPANS_NOTE}"


def _shape(
    name: str, header: list[str], rows: Iterable[list[str]], *, partial: bool, read: str
) -> str:
    """The digest block for a table: its *header* and the data *rows* after it.

    *partial* marks the row count as a floor, and *read* says how much was read.
    """
    kinds: list[set[str]] = [set() for _ in header[:MAX_COLUMNS]]
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
    shown = len(kinds)
    columns = ", ".join(
        f"{_cell(header[i]) or '(blank)'} ({_column_type(kinds[i])})" for i in range(shown)
    )
    if len(header) > shown:
        columns += f", and {len(header) - shown} more columns"
    rows_note = f"{count}{'+' if partial else ''} data rows"
    if partial:
        rows_note += f" ({read})"
    lines = [f"[sheet digest: {name}]", f"{rows_note}; {len(header)} columns: {columns}"]
    lines += [f"sample: {' | '.join(sample)}" for sample in samples]
    if count > MAX_TYPED_ROWS:
        lines.append(f"types come from the first {MAX_TYPED_ROWS} data rows")
    return "\n".join(lines)[:MAX_DIGEST_CHARS]


def digest_file(path: Path) -> str | None:
    """The digest of the CSV, TSV or workbook at *path*, or None when there is none to give."""
    from kiro_crew.hooks import safe_read_file_bytes_nolink

    if path.suffix.lower() in _WORKBOOKS:
        return digest_workbook(path)
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
        # digest_text redacts what it shows, in the attachment path's order, so a secret in a
        # cell never reaches the prompt.
        text = data.decode("utf-8-sig", errors="replace")
        if path.suffix.lower() == ".csv":
            delimiter = _csv_delimiter(text)
        return digest_text(path.name, text, delimiter, partial=partial)
    except Exception:
        logger.debug("sheet digest: could not digest %s", path.name, exc_info=True)
        return None


def digest_workbook(path: Path) -> str | None:
    """One digest block per sheet of the workbook at *path*, or None when there is none.

    The rows come from :func:`kiro_crew.workbook_rows.read_workbook_rows`, which
    vets the archive and holds the character budget and the deadline; each sheet is
    then summarised like a CSV, and the sheet's name follows the file's. A refused,
    unreadable or oversized workbook, or a missing reader, gives None.
    """
    from kiro_crew.hooks import safe_read_file_bytes_nolink
    from kiro_crew.workbook_rows import read_workbook_rows

    try:
        if path.stat().st_size > MAX_WORKBOOK_BYTES:
            return None
        data = safe_read_file_bytes_nolink(str(path), max_bytes=MAX_WORKBOOK_BYTES)
        if not data:
            return None
        walk = read_workbook_rows(
            io.BytesIO(data),
            deadline=time.monotonic() + WORKBOOK_WALL_SECS,
            max_chars=MAX_READ_BYTES,
            separator="\t",
            stride=_WORKBOOK_STRIDE,
            collapse_whitespace=True,
            keep_blanks=True,
            mark_sheets=True,
            scrub_title=_redacted,
            max_members=WORKBOOK_MAX_MEMBERS,
            max_expanded_bytes=WORKBOOK_MAX_EXPANDED_BYTES,
        )
        partial = walk.cut or walk.failure is not None
        starts = walk.starts or [0]
        # One group per sheet reached; only the sheet the walk stopped in is partial.
        sheets = [
            (walk.rows[a:b], partial and a == starts[-1])
            for a, b in zip(starts, [*starts[1:], len(walk.rows)])
            if b > a
        ]
        blocks = []
        for rows, cut in sheets[:MAX_SHEETS]:
            texts = [text for _label, text in rows]
            if cut:
                texts = texts[:-1]  # the last row may have been cut short
            title = rows[0][0].rpartition(" · row ")[0]
            blocks.append(_sheet_block(f"{path.name} · {title}", texts, partial=cut))
        omitted = len(sheets) - MAX_SHEETS
        if partial and (omitted > 0 or blocks):
            # Sheets past the stop were never reached, so the count is a floor.
            more = (
                f"at least {omitted} more sheets not shown"
                if omitted > 0
                else "more sheets may follow"
            )
            blocks.append(f"{path.name}: {more}; the read stopped early")
        elif omitted > 0:
            blocks.append(f"{path.name}: {omitted} more sheets not shown")
        return "\n\n".join(block for block in blocks if block) or None
    except Exception:
        logger.debug("sheet digest: could not digest %s", path.name, exc_info=True)
        return None


def _sheet_block(name: str, texts: list[str], *, partial: bool) -> str:
    """The digest of one sheet whose rows are the tab-joined *texts*, or ``""``.

    Every number comes from *texts*. The typed or shown rows are redacted together, so a
    credential spread over rows is still seen; if that moves a line or cell break, the
    block gives only the counts and a note.
    """
    if not texts:
        return ""
    window = texts[: MAX_TYPED_ROWS + 1]
    kept = _redacted("\n".join(window)).split("\n")
    # The walk drops trailing blank labels: pad them back to the widest row of the
    # whole sheet, so a column first filled after the typed rows is still reported.
    width = max(text.count("\t") for text in texts) + 1
    read = f"first {MAX_READ_BYTES} characters read"
    if [line.count("\t") for line in kept] != [text.count("\t") for text in window]:
        return _counts_only(name, len(texts) - 1, width, partial=partial, read=read)
    rows = [line.split("\t") for line in kept]
    header = rows[0] + [""] * (width - len(rows[0]))
    counted = itertools.repeat([""], len(texts) - len(window))
    return _shape(name, header, itertools.chain(rows[1:], counted), partial=partial, read=read)


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
