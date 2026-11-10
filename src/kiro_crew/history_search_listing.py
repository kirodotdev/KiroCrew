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
(:mod:`kiro_crew._facade_mirror`). A patch of ``open`` through the facade creates the
name here too, so it reaches the metadata read here, which also reads a cold untitled
transcript's head from its own handle, and the path head scan the facade passes in as
*scan_head*.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import datetime
from typing import IO, TYPE_CHECKING, NamedTuple

from kiro_crew.history_search_query import logger
from kiro_crew.mrkdwn_title import (
    is_slack_session_key,
    slack_title_text,
    stored_title_is_mrkdwn,
)
from kiro_crew.shared_crew.attribution import public_attribution_from_meta

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

#: ``scan_head`` title for a head whose read raised an I/O error; never cached.
_UNREADABLE = object()
#: ``scan_head`` title for a head whose read raised anything else, such as a JSON line
#: that is not an object; never cached. It reads the same until the file changes.
_MALFORMED = object()
#: :func:`listing_row`'s marker for a head-cache lookup it has not made yet.
_NOT_LOOKED_UP = object()

#: Longest title the catalog derives from a session's first user message.
_FALLBACK_TITLE_CHARS = 80
#: How much of a Slack first message is read to make that title.
_FALLBACK_SCAN_CHARS = 2000


def _catalog_title(key: str, title: str, *, cut: bool) -> str:
    """*title* as the catalog lists it for session *key*.

    A Slack conversation's text is Slack mrkdwn, so its title is read as plain
    text first (``mrkdwn_title``) and only then, when *cut*, bounded: cutting
    first splits a ``<url|label>`` and leaves a raw half-link. Read on every
    listing rather than rewritten on disk, so a title persisted raw by an earlier
    build lists cleanly with no migration. Every other session's title is its
    own words and is only bounded.
    """
    if is_slack_session_key(key):
        # A first message can be long and is read on every listing: only a
        # bounded head can reach the cut, and a construct the bound opens is
        # read like any other cut-open one.
        title = slack_title_text(title[:_FALLBACK_SCAN_CHARS] if cut else title)
    return title[:_FALLBACK_TITLE_CHARS] if cut else title


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
    if isinstance(line.get("cron_run"), str) and line["cron_run"]:
        meta["cron_run"] = line["cron_run"]
    _project_crew_fields(meta, line)
    meta["attribution"] = public_attribution_from_meta(line, key=key)


def _parse_metadata_line(meta: dict, first_line: str, key: str) -> dict | None:
    """Apply metadata line *first_line* to *meta*; the ``_meta_cache`` value, else None; may raise."""
    if not first_line:
        return None
    d = json.loads(first_line)
    if d.get("_type") == "metadata":
        _apply_metadata(meta, d, key)
        return d
    if isinstance(d, dict):
        # A first line that is an object but not metadata reads as no metadata, the
        # same ``{}`` that ``_read_metadata`` caches for it, so the next listing
        # answers from the cache instead of the file.
        return {}
    return None


def _cached_head(
    log: "ConversationLog", key: str, identity: object, stat: os.stat_result
) -> tuple[object, bool | None] | None:
    """``(title, has_messages)`` for an untitled row from a current cache, else None.

    ``_msg_cache`` answers first, then ``_title_scan_cache``; a miss means the head
    must be read.
    """
    msg_cached = log._msg_cache.get(key)
    if msg_cached and msg_cached[0] == identity and msg_cached[1] == log._cache_gen(key):
        # Every parsed message is a non-metadata record; an empty parse does not
        # prove an empty file, since an unparseable line is content too.
        for m in msg_cached[2]:
            if m.get("role") == "user" and m.get("content"):
                return _catalog_title(key, m["content"], cut=True), True
        return None, (True if msg_cached[2] else None)
    scanned = log._title_scan_cache.get(key)
    if (
        scanned is not None
        and scanned[0] == (identity, stat.st_size)
        and scanned[1] == log._cache_gen(key)
    ):
        title, has_messages = scanned[2]
        return title, has_messages
    return None


def listing_row(
    log: "ConversationLog",
    path: str,
    key: str,
    entry: "os.DirEntry[str] | None",
    scan_head: Callable[[str], tuple[object, bool | None]],
    scan_head_lines: Callable[[IO[str]], tuple[object, bool | None]],
) -> ListingRow:
    """The :meth:`ConversationLog.list_sessions` row for the transcript at *path*.

    *entry* is the directory entry the caller already holds, or None. *scan_head*
    reads the head of an untitled transcript and returns ``(title, has_messages)``,
    the title being ``_UNREADABLE`` when the read raised an I/O error and
    ``_MALFORMED`` when it raised anything else. *scan_head_lines* answers the same
    from an open handle: when this call reads the metadata line from disk, it reads
    the head from that handle, so a cold read of an untitled transcript opens the
    file once. *scan_head* reads the head when the metadata came from the cache or
    the metadata open raised.
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
    # The head scan's answer when it was read from the metadata line's open handle.
    head: tuple[object, bool | None] | None = None
    # The cached head answer, once looked up; _NOT_LOOKED_UP until then.
    cached_head: object = _NOT_LOOKED_UP
    cached_meta = log._meta_cache.get(key)
    # The metadata line the title came from, read for its ``title_origin``.
    title_line: dict = {}
    if cached_meta and cached_meta[0] == identity and cached_meta[1] == log._cache_gen(key):
        _apply_metadata(meta, cached_meta[2], key)
        title_line = cached_meta[2]
    else:
        # Read the first line for metadata and, for an untitled session whose head no
        # cache answers, the head from the same open handle.
        fill: dict | None = None
        try:
            with open(path, encoding="utf-8") as f:
                try:
                    fill = _parse_metadata_line(meta, f.readline().strip(), key)
                    # The metadata line the title came from, read for its ``title_origin``.
                    title_line = fill or {}
                except Exception as exc:
                    reusable = False
                    volatile = isinstance(exc, OSError)
                if "title" not in meta:
                    cached_head = _cached_head(log, key, identity, stat)
                    if cached_head is None:
                        head = scan_head_lines(f)
            if fill is not None:
                # After the handle closes: on Windows a writer cannot replace an open file.
                log._publish_if_current(
                    log._meta_cache, key, (identity, gen, fill), key=key, gen=gen
                )
        except Exception as exc:
            # Any failure keeps the scan's listing out of its memo. Only an I/O error
            # is retried by the next indexed listing: a line that does not parse reads
            # the same until the file changes, and a change moves its stamp or, from
            # this process, its generation.
            reusable = False
            volatile = volatile or isinstance(exc, OSError)
    meta.setdefault("memory_mode", "persistent")
    if (
        "title" in meta
        and is_slack_session_key(key)
        and stored_title_is_mrkdwn(meta["title"], title_line.get("title_origin"))
    ):
        # A stored title may be one an earlier build wrote raw (often a
        # cut-open link): listed as plain text, nothing rewritten on disk. A
        # person's rename and a non-string title are listed as stored.
        meta["title"] = _catalog_title(key, meta["title"], cut=False)
    # A session with no readable metadata line predates attribution: it reads as the
    # owner's, unless its key is a messaging channel's (Unattributed).
    if "attribution" not in meta:
        meta["attribution"] = public_attribution_from_meta(None, key=key)
    has_messages: bool | None = None
    if "title" not in meta:
        # The first user message titles an untitled session.
        if cached_head is _NOT_LOOKED_UP:
            cached_head = _cached_head(log, key, identity, stat)
        if isinstance(cached_head, tuple):
            title, has_messages = cached_head
        else:
            title, has_messages = head if head is not None else scan_head(path)
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
