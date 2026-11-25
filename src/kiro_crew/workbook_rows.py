"""One row walk over an ``.xlsx`` workbook, for every reader that indexes or searches one.

File grep's document pass and the Knowledge Library's reader both turn a workbook
into ``("<sheet> · row <n>", row text)`` pairs. Each keeps its own outcome contract
(grep answers a refused workbook as a settled empty result, the library reader as
an error), so this module only walks: it vets the archive, opens it read-only,
and builds each row inside a character budget under a deadline.
"""

from __future__ import annotations

import os
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO

from kiro_crew.zip_vet import (
    TAIL_WINDOW,
    WORKBOOK_MAX_EXPANDED_BYTES,
    WORKBOOK_MAX_MEMBERS,
    vet_zip_inventory_bytes,
)

__all__ = ["WorkbookRows", "read_workbook_rows"]

#: Characters of a sheet title a row label carries, the longest name the
#: spreadsheet application itself accepts. Every kept row holds its label, so the
#: title is bounded once per sheet; the character budget only counts the cells.
_TITLE_MAX = 31
#: Raw characters whitespace folding may read for each character of the budget.
#: One shared string can back every cell of a sheet, and a run of spaces folds to
#: one character however long it is, so the characters kept cannot bound the
#: characters read: this allowance does, over the whole walk.
_SCAN_PER_CHAR = 16


@dataclass
class WorkbookRows:
    """What one walk read: its rows, the sheets it reached, and why it stopped early."""

    rows: list[tuple[str, str]] = field(default_factory=list)
    sheets: int = 0
    #: The character budget or the deadline ended the walk before the last row.
    cut: bool = False
    #: The read broke after ``rows``; they are real, the rest was never seen.
    failure: Exception | None = None
    #: With *mark_sheets*, where in ``rows`` each sheet reached begins; the last is where it stopped.
    starts: list[int] | None = None


@dataclass
class _Budget:
    """What the walk may still spend: row characters, raw characters read, time."""

    deadline: float
    stride: int
    #: Characters of row text still to keep.
    chars: int
    #: Raw characters whitespace folding may still read.
    scan: int
    #: An empty cell before a later value in its row is kept, at the separator's cost.
    keep_blanks: bool = False

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline


def read_workbook_rows(
    fileobj: IO[bytes],
    *,
    deadline: float,
    max_chars: int,
    separator: str,
    stride: int,
    labels_count: bool = False,
    collapse_whitespace: bool = False,
    keep_blanks: bool = False,
    mark_sheets: bool = False,
    scrub_title: Callable[[str], str] | None = None,
    max_members: int = WORKBOOK_MAX_MEMBERS,
    max_expanded_bytes: int = WORKBOOK_MAX_EXPANDED_BYTES,
) -> WorkbookRows:
    """Walk the non-empty rows of the workbook in *fileobj* (seekable bytes).

    Two gates run BEFORE openpyxl sees the bytes, because it opens the container
    itself: the inventory vet bounds the declared member count, and the
    ``infolist()`` sum bounds expansion. Either refusal, an archive that is not a
    zip, a missing openpyxl or a workbook openpyxl cannot open raises; a failure
    after rows were read is returned in ``failure`` beside them.

    A row's cells join with *separator*, and the row is built inside what is left
    of *max_chars*, never joined first and cut after: openpyxl hands back one
    shared string for every cell that references it, so a wide row is N
    references until a join makes it N copies. Every non-empty cell costs at least
    one character, even one that folds to nothing. With *labels_count* a row also
    pays for its label plus three characters, the ``": "`` and newline of a
    ``<label>: <cells>`` line. A label names its sheet by at most the first
    ``_TITLE_MAX`` characters of the title; *scrub_title* (file grep passes its
    credential redactor) runs on the WHOLE title before that cut, because half a
    credential matches no pattern.
    With *keep_blanks* an empty cell before a later value in its row keeps its place
    as an empty string, so a reader that types columns sees each value in its own
    column; only those kept blanks cost budget, the separator's length each.
    *mark_sheets* fills ``starts``, so a reader can split rows by sheet.
    *collapse_whitespace* folds every whitespace run in a title or cell to one
    space, and the budget counts the folded text; folding reads at most
    ``_SCAN_PER_CHAR`` raw characters per character of *max_chars* over the walk.
    The deadline (a ``time.monotonic()`` reading) is checked every *stride* rows
    and every *stride* cells of a row, because the character budget cannot see an
    empty row and one small sheet can declare millions of them.
    """
    size = fileobj.seek(0, os.SEEK_END)
    fileobj.seek(max(0, size - TAIL_WINDOW))
    vet_zip_inventory_bytes(fileobj.read(TAIL_WINDOW), max_members=max_members)
    fileobj.seek(0)
    with zipfile.ZipFile(fileobj) as archive:
        if sum(info.file_size for info in archive.infolist()) > max_expanded_bytes:
            raise ValueError("workbook expands past its ceiling")
    fileobj.seek(0)
    # Imported only past both ceilings: parser setup a refusal should not pay for.
    import openpyxl  # type: ignore[import-untyped]

    book = openpyxl.load_workbook(fileobj, read_only=True, data_only=True)
    walk = WorkbookRows(starts=[] if mark_sheets else None)
    budget = _Budget(deadline, stride, max_chars, max_chars * _SCAN_PER_CHAR, keep_blanks)
    try:
        _walk(book, walk, budget, separator, labels_count, collapse_whitespace, scrub_title)
    except Exception as exc:
        walk.failure = exc
    finally:
        book.close()
    return walk


def _walk(
    book,
    walk: WorkbookRows,
    budget: _Budget,
    separator: str,
    labels_count: bool,
    collapse: bool,
    scrub_title: Callable[[str], str] | None,
) -> None:
    for sheet in book.worksheets:
        walk.sheets += 1
        if walk.starts is not None:
            walk.starts.append(len(walk.rows))
        if not hasattr(sheet, "iter_rows"):  # a chartsheet has no cell grid
            continue
        if hasattr(sheet, "reset_dimensions"):
            # A dimension record can lie, and the read-only reader trusts it.
            sheet.reset_dimensions()
        title = str(sheet.title)
        if scrub_title is not None:
            title = scrub_title(title)
        if collapse:
            title, _whole = _fold(title, _TITLE_MAX, budget)
        title = title[:_TITLE_MAX]
        for index, row in enumerate(sheet.iter_rows(values_only=True), 1):
            if (index - 1) % budget.stride == 0 and budget.expired():
                walk.cut = True
                return
            label = f"{title} · row {index}"
            room = budget.chars - (len(label) + 3 if labels_count else 0)
            if room <= 0:
                walk.cut = True
                return
            cells, left, whole = _row_cells(row, room, budget, separator, collapse)
            while budget.keep_blanks and cells and not cells[-1]:
                cells.pop()
            if cells:
                walk.rows.append((label, separator.join(cells)))
                budget.chars = left
            else:
                budget.chars -= room - left  # blank cells, and no label to pay for
            if not whole or budget.chars <= 0:
                walk.cut = True
                return


def _row_cells(
    row, room: int, budget: _Budget, separator: str, collapse: bool
) -> tuple[list[str], int, bool]:
    """The non-empty cells of *row* built inside *room*, the room left, and whether
    the row was read to its end.

    A cell that does not fit is cut to what is left and ends the row with no room.
    The row also ends early when its room is spent, when folding runs out of raw
    characters to read, or at the deadline, checked every ``budget.stride`` cells.
    """
    cells: list[str] = []
    for position, cell in enumerate(row):
        if position and position % budget.stride == 0 and budget.expired():
            return cells, room, False
        if cell is None:
            continue
        piece = str(cell)
        whole = True
        if collapse:
            piece, whole = _fold(piece, room, budget)
            if not piece:
                room -= 1  # a blank cell was still read, so it still costs
                if not whole or room <= 0:
                    return cells, room, False
                continue
        if budget.keep_blanks and position > len(cells):  # blanks before a value keep its column
            room -= (position - len(cells)) * len(separator)
            if room <= 0:
                return cells, room, False
            cells += [""] * (position - len(cells))
        if not whole or len(piece) >= room:
            cells.append(piece[: max(room, 0)])
            return cells, 0, False
        cells.append(piece)
        room -= len(piece) + len(separator)
        if room <= 0:
            return cells, room, False
    return cells, room, True


def _fold(text: str, limit: int, budget: _Budget) -> tuple[str, bool]:
    """*text* with each whitespace run folded to one space, cut once it passes
    *limit*, and whether the fold read far enough to know.

    The fold runs before the cut, so a long run of spaces costs one character of
    the budget, and the scan stops at the first word past *limit*: the result is
    longer than *limit* exactly when the folded cell does not fit, which is how
    the caller knows to mark the walk cut. The scan reads no more of *text* than
    ``budget.scan`` allows and charges what it read; when the allowance ends first,
    the flag is False and the rest of *text* was never seen.
    """
    end = min(len(text), max(budget.scan, 0))
    read = end
    words: list[str] = []
    size = -1  # the first word has no space before it
    i = 0
    while i < end:
        if text[i].isspace():
            i += 1
            continue
        start = i
        while i < end and not text[i].isspace():
            i += 1
        words.append(text[start:i])
        size += i - start + 1
        if size > limit:
            read = i
            break
    budget.scan -= read
    return " ".join(words), size > limit or read == len(text)
