"""One catalog row from one transcript: what ``list_sessions`` and the listing index share.

:func:`listing_row` stats a transcript, reads its metadata line and, for a session whose
metadata carries no title, the head of the file, and returns the row ``list_sessions``
lists for it plus what the caller needs to keep the row: the file's stat, whether a
later listing must retry the read, and whether the head proved the transcript holds a
message. The directory scan calls it once per file; ``history_listing_index`` calls it
for the files whose stamp moved.

:mod:`kiro_crew.history_search` re-exports every name defined here and is the import path
callers use. A patch of a name defined here through the facade reaches the readers here,
because the facade forwards the write to every member that binds the name
(:mod:`kiro_crew._facade_mirror`). ``open`` is not such a name: a patch of it through the
facade reaches the head scan, which the facade passes in as *scan_head*, and not the
metadata read here.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, NamedTuple

from kiro_crew.history_search_query import logger
from kiro_crew.shared_crew.attribution import public_attribution_from_meta

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

#: ``scan_head`` title for a head whose read raised an I/O error; never cached.
_UNREADABLE = object()
#: ``scan_head`` title for a head whose read raised anything else, such as a JSON line
#: that is not an object; never cached. It reads the same until the file changes.
_MALFORMED = object()

#: Session files whose stat failed with something other than ENOENT, already
#: warned about. One such file marks every listing incomplete, which keeps the
#: dashboard's unread drain held for the process lifetime, so the warning must
#: be visible; the listing runs every few seconds, so it is also logged only
#: once per path per process rather than on every pass.
_unreadable_session_files_warned: set[str] = set()
#: Listings run on executor threads, so the check and the add share one lock.
_unreadable_session_files_lock = threading.Lock()

#: The metadata-line fields a catalog row carries so a reader can tell a Crew Mode
#: root from a crew member without opening the file: the creator stamp, the agent's
#: namespace and the Crew Mode stamp (``crew_mode.saved_row_is_root``).
_CREW_CATALOG_FIELDS: tuple[str, ...] = ("created_by", "agent_kind", "crew_mode_agent")


class ListingRow(NamedTuple):
    """What :func:`listing_row` read from one transcript.

    ``status`` is ``"row"`` with a ``row``, or ``"gone"`` (absent), ``"unreadable"``
    (stat failed) or ``"link"`` (a handoff alias, never listed) with ``row`` None.
    ``volatile`` is True when a read failed with an I/O error, which the next listing
    retries, so a caller that keeps rows must not keep this one past the next
    listing. ``reusable`` is False when any read raised, a metadata line that does
    not parse included, so the scan does not memoize a listing that holds the row.
    ``stat`` is the one stat the row was built after, for a row. ``has_messages`` is
    what :meth:`ConversationLog.has_messages` answers for the file this row was read
    from, when the reads that titled the row settled it, else None: a row titled by
    its metadata, a head of 21 lines holding only metadata, or a failed read.
    """

    status: str
    row: dict | None
    volatile: bool
    stat: os.stat_result | None
    has_messages: bool | None
    reusable: bool


def _project_crew_fields(meta: dict, line: dict) -> None:
    """Copy the non-empty :data:`_CREW_CATALOG_FIELDS` of metadata *line* onto catalog row *meta*."""
    for field in _CREW_CATALOG_FIELDS:
        value = line.get(field)
        if isinstance(value, str) and value:
            meta[field] = value


def _warn_unreadable_session_file(path: str, error: OSError) -> None:
    with _unreadable_session_files_lock:
        if path in _unreadable_session_files_warned:
            return
        _unreadable_session_files_warned.add(path)
    logger.warning(
        "Session file %s cannot be stat'd (%s); the session list is reported "
        "incomplete and unread dots stay held until it is readable or removed",
        path,
        error,
    )


def _apply_metadata(meta: dict, line: dict, key: str) -> None:
    """Copy the fields of metadata *line* that a catalog row carries onto *meta*."""
    if line.get("created_at"):
        meta["created"] = line["created_at"]
    if line.get("title"):
        meta["title"] = line["title"]
    if line.get("agent"):
        meta["agent"] = line["agent"]
    meta["memory_mode"] = line.get("memory_mode", "persistent")
    if line.get("folder_id"):
        meta["folder_id"] = line["folder_id"]
    _project_crew_fields(meta, line)
    meta["attribution"] = public_attribution_from_meta(line, key=key)


def listing_row(
    log: "ConversationLog",
    path: str,
    key: str,
    entry: "os.DirEntry[str] | None",
    scan_head: Callable[[str], tuple[object, bool | None]],
) -> ListingRow:
    """The :meth:`ConversationLog.list_sessions` row for the transcript at *path*.

    *entry* is the directory entry the caller already holds, or None. *scan_head*
    reads the head of an untitled transcript and returns ``(title, has_messages)``,
    the title being ``_UNREADABLE`` when the read raised an I/O error and
    ``_MALFORMED`` when it raised anything else.
    """
    volatile = False
    reusable = True
    # Snapshot the invalidation generation BEFORE the stat: the first-line fill
    # records this stat's cache identity, and the generation independently proves
    # the stat -> read -> publish window stayed write-free for this process.
    gen = log._cache_gen(key)
    try:
        # Windows DirEntry.stat() reports st_ino as 0, so its cache identity would
        # never equal the one every other reader takes from path.stat(); there the
        # entry answers only is_symlink.
        stat = os.stat(path) if entry is None or os.name == "nt" else entry.stat()
    except FileNotFoundError:
        return ListingRow("gone", None, False, None, None, True)
    except OSError as exc:
        _warn_unreadable_session_file(path, exc)
        return ListingRow("unreadable", None, False, None, None, True)
    identity = log._cache_identity(stat)
    # Skip symlinks: these are handoff aliases pointing to the real session.
    if os.path.islink(path) if entry is None else entry.is_symlink():
        return ListingRow("link", None, False, None, None, True)
    meta: dict = {
        "key": key,
        "messages": max(1, int(stat.st_size / 200)),
        "modified": stat.st_mtime,
        "created": datetime.fromtimestamp(stat.st_mtime).isoformat(),
    }
    cached_meta = log._meta_cache.get(key)
    if cached_meta and cached_meta[0] == identity and cached_meta[1] == log._cache_gen(key):
        _apply_metadata(meta, cached_meta[2], key)
    else:
        # Read only the first line for metadata.
        try:
            with open(path, encoding="utf-8") as f:
                first_line = f.readline().strip()
            if first_line:
                d = json.loads(first_line)
                if d.get("_type") == "metadata":
                    _apply_metadata(meta, d, key)
                    cached = d
                elif isinstance(d, dict):
                    # A first line that is an object but not metadata reads as no
                    # metadata, the same ``{}`` that ``_read_metadata`` caches for it,
                    # so the next listing answers from the cache instead of the file.
                    cached = {}
                else:
                    cached = None
                if cached is not None:
                    # Guarded publish: discard the fill if a write invalidated this
                    # key inside the stat -> read window (see the generation above).
                    log._publish_if_current(
                        log._meta_cache, key, (identity, gen, cached), key=key, gen=gen
                    )
        except Exception as exc:
            # Any failure keeps the scan's listing out of its memo. Only an I/O error
            # is retried by the next indexed listing: a line that does not parse reads
            # the same until the file changes, and a change moves its stamp or, from
            # this process, its generation.
            reusable = False
            volatile = isinstance(exc, OSError)
    meta.setdefault("memory_mode", "persistent")
    # A session with no readable metadata line predates attribution: it reads as the
    # owner's, unless its key is a messaging channel's (Unattributed).
    if "attribution" not in meta:
        meta["attribution"] = public_attribution_from_meta(None, key=key)
    has_messages: bool | None = None
    if "title" not in meta:
        # The first user message titles an untitled session.
        msg_cached = log._msg_cache.get(key)
        if msg_cached and msg_cached[0] == identity and msg_cached[1] == log._cache_gen(key):
            # Every parsed message is a non-metadata record; an empty parse does not
            # prove an empty file, since an unparseable line is content too.
            if msg_cached[2]:
                has_messages = True
            for m in msg_cached[2]:
                if m.get("role") == "user" and m.get("content"):
                    meta["title"] = m["content"][:80]
                    break
        else:
            scanned = log._title_scan_cache.get(key)
            if (
                scanned is not None
                and scanned[0] == (identity, stat.st_size)
                and scanned[1] == log._cache_gen(key)
            ):
                title, has_messages = scanned[2]
            else:
                title, has_messages = scan_head(path)
                if title is _UNREADABLE or title is _MALFORMED:
                    # As for the metadata read above: kept out of the scan's memo, and
                    # retried by the next indexed listing only after an I/O error.
                    reusable = False
                    volatile = volatile or title is _UNREADABLE
                    title = None
                else:
                    # Guarded like the metadata fill above.
                    log._publish_if_current(
                        log._title_scan_cache,
                        key,
                        ((identity, stat.st_size), gen, (title, has_messages)),
                        key=key,
                        gen=gen,
                    )
            if title is not None:
                meta["title"] = title
    if "title" not in meta:
        meta["title"] = key
    return ListingRow("row", meta, volatile, stat, has_messages, reusable)
