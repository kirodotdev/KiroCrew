"""Google Drive pagination + change-feed cursor reading.

Drive exposes two related but distinct cursor contracts, and conflating them
loses information a sync must keep:

* **A within-batch page cursor** -- ``files.list`` and ``changes.list`` return a
  ``nextPageToken`` while more pages remain, absent on the last page. A page walk
  advances on it and stops when it is absent.
* **A resume/resync boundary token** -- ``changes.getStartPageToken`` returns a
  ``startPageToken``, and a completed ``changes.list`` batch returns a
  ``newStartPageToken`` (present ONLY on the final page of a batch). This is the
  token the NEXT incremental sync resumes from -- the durable checkpoint -- and
  it is a different thing from the within-batch ``nextPageToken``.

Keeping them apart matters: the checkpoint the scheduler persists is the
``newStartPageToken`` (where to resume next time), NOT the transient
``nextPageToken`` (where to continue THIS batch). Persisting the wrong one either
re-reads a batch already consumed or skips changes.

This module reads these tokens out of a decoded JSON body. It builds no request
(see :mod:`.drive_api`), performs no I/O, and decides no retry. The
token-no-longer-usable -> resync recovery is recognised in :mod:`.operations`
(``_is_stale_page_token``, keyed on W01's neutral ``input`` error class -- see
that function for the documentation gap it rests on) and enacted by the
connector; this module only reads the tokens a healthy response carries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class ChangePage:
    """One decoded ``changes.list`` page.

    ``changes`` is the raw change objects (each a mapping with ``fileId``,
    ``removed``, and -- unless removed -- ``file``). ``next_page_token`` advances
    within this batch (None on the last page). ``new_start_page_token`` is the
    RESYNC/resume checkpoint, present ONLY on the final page of a batch (None on
    every non-final page) -- so a caller advances the checkpoint exactly once per
    batch, when it appears.
    """

    changes: Tuple[Mapping[str, Any], ...]
    next_page_token: Optional[str]
    new_start_page_token: Optional[str]

    @property
    def is_last_page(self) -> bool:
        """True on the final page of a batch (no more within-batch pages).

        Drive signals the end of a batch by OMITTING ``nextPageToken`` and
        INCLUDING ``newStartPageToken``; a healthy final page has both conditions
        together. ``next_page_token is None`` is the authoritative stop signal.
        """
        return self.next_page_token is None


@dataclass(frozen=True)
class FileListPage:
    """One decoded ``files.list`` page: the file objects + within-list cursor."""

    files: Tuple[Mapping[str, Any], ...]
    next_page_token: Optional[str]

    @property
    def is_last_page(self) -> bool:
        return self.next_page_token is None


def parse_file_list(body: Mapping[str, Any]) -> FileListPage:
    """Read a ``files.list`` JSON body into a :class:`FileListPage`.

    Tolerant of a body missing ``files`` (an empty page is ``files: []`` or the
    key absent) -- that is a valid empty result, not an error. A non-list
    ``files`` value is treated as empty rather than raising, so one malformed
    field does not crash a walk mid-page.
    """
    raw_files = body.get("files")
    files = (
        tuple(f for f in raw_files if isinstance(f, Mapping)) if isinstance(raw_files, list) else ()
    )
    token = body.get("nextPageToken")
    return FileListPage(
        files=files, next_page_token=token if isinstance(token, str) and token else None
    )


def parse_change_page(body: Mapping[str, Any]) -> ChangePage:
    """Read a ``changes.list`` JSON body into a :class:`ChangePage`.

    Reads BOTH cursor tokens distinctly (see the module docstring): the transient
    ``nextPageToken`` and the durable ``newStartPageToken``. Tolerant of a body
    missing ``changes`` (a batch with no changes is valid) and of malformed token
    fields (a non-string token reads as absent).
    """
    raw_changes = body.get("changes")
    changes = (
        tuple(c for c in raw_changes if isinstance(c, Mapping))
        if isinstance(raw_changes, list)
        else ()
    )
    next_token = body.get("nextPageToken")
    new_start = body.get("newStartPageToken")
    return ChangePage(
        changes=changes,
        next_page_token=next_token if isinstance(next_token, str) and next_token else None,
        new_start_page_token=new_start if isinstance(new_start, str) and new_start else None,
    )


def read_start_page_token(body: Mapping[str, Any]) -> Optional[str]:
    """Read the ``startPageToken`` from a ``changes.getStartPageToken`` body.

    None when absent/malformed -- a caller that cannot get a start token cannot
    establish a checkpoint and must surface that rather than silently syncing
    from nowhere.
    """
    token = body.get("startPageToken")
    return token if isinstance(token, str) and token else None


def is_removed_change(change: Mapping[str, Any]) -> bool:
    """True when a change record marks a file REMOVED (deleted / access lost).

    Drive marks a removed change with ``removed: true``; it may also carry a
    ``file`` whose ``trashed`` is true. Either shape means the row keyed by this
    change's ``fileId`` should be dropped from the source on an incremental sync.
    """
    if change.get("removed") is True:
        return True
    file_obj = change.get("file")
    if isinstance(file_obj, Mapping) and file_obj.get("trashed") is True:
        return True
    return False


def change_file_id(change: Mapping[str, Any]) -> Optional[str]:
    """The fileId a change record refers to, or None if malformed."""
    fid = change.get("fileId")
    return fid if isinstance(fid, str) and fid else None


__all__ = [
    "ChangePage",
    "FileListPage",
    "change_file_id",
    "is_removed_change",
    "parse_change_page",
    "parse_file_list",
    "read_start_page_token",
]
