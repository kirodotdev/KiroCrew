"""The forge read behind the crew mention wakes: comments updated since a cursor.

Split out of :mod:`crew_runtime` to keep that module under the file line cap; it
re-exports these names. The scan is by UPDATE time, and it reports whether it
reached the present, because only then may the caller move its creation floor:
everything here is about never letting that floor pass a comment nobody read.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from . import crew_store, provider

logger = logging.getLogger(__name__)

#: Notes reads (GitLab) one sweep may spend on mentions. The cursor stops at the last
#: thread read, so a burst is worked through over a few cycles.
MAX_MENTION_THREADS_PER_SWEEP = 10
#: The page the GitHub repository-comments read asks for; a full page means the scan
#: has not reached the present yet.
REPO_COMMENTS_PAGE = 100
#: Pages one sweep reads on through when every comment on a page has the same update
#: time (a bulk edit), so a tie wider than a page cannot hide a question behind it.
MAX_TIED_COMMENT_PAGES = 5
#: The GitLab updated-threads page, oldest update first, per kind (issues, MRs), and
#: how many such pages one sweep reads through when every thread on them is read.
THREAD_PAGE = 100
MAX_THREAD_PAGES = 5
_GL_PAGE = "glpage:"


def _recent_comments(
    key: provider.RepoKey,
    cursor: str,
    at_cursor: Sequence[str],
    identity: str = "",
    floor: str = "",
    seen: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], str, list[str], bool]:
    """Comments UPDATED at or after *cursor*: ``(comments, next_cursor, at_cursor,
    complete)``.

    The cursor is scan progress by UPDATE time, never by creation time: an edit moves
    an old comment past it, and a cursor kept by creation time could not step over a
    page of such edits. *complete* is whether the scan reached the present; only then
    may the caller move its creation floor.

    Each comment is ``{id, number, is_pr, actor, created_at, body}`` (GitLab adds the
    thread's ``labels``). GitHub serves every thread's comments from one endpoint,
    oldest update first; ``since`` is inclusive, so a comment at the cursor comes back
    and is dropped by its id. GitLab has no project-wide notes read, so the open
    issues and MRs updated since *cursor* are listed oldest update first and their
    notes read, up to the per-sweep cap. A thread at exactly the cursor is read unless
    *at_cursor* lists it as read already, so a tie at the cap is not skipped. A failed
    read stops the scan there, so nothing past it is skipped either.
    """
    client = provider.client_for(key)
    kwargs = provider.call_kwargs(key)
    repo_reader = getattr(client, "list_repo_comments_since", None)
    if callable(repo_reader):
        return _github_comments(key, repo_reader, kwargs, cursor, at_cursor, identity, floor, seen)
    return _gitlab_comments(key, client, kwargs, cursor, at_cursor, identity, floor)


def _github_comments(
    key: provider.RepoKey,
    repo_reader: Any,
    kwargs: dict[str, Any],
    cursor: str,
    at_cursor: Sequence[str],
    identity: str,
    floor: str,
    seen: frozenset[str],
) -> tuple[list[dict[str, Any]], str, list[str], bool]:
    owner, repo = key.owner, key.repo
    out: list[dict[str, Any]] = []
    # *at_cursor* holds at most ``page:<n>``: where a tie that outran the per-sweep
    # page cap resumes, plus ``top:<time>``, the newest update its pages held.
    resume = [k for k in at_cursor if k.startswith("page:") and k[5:].isdigit()]
    start = int(resume[0][5:]) if resume else 1
    tops = [k[4:] for k in at_cursor if k.startswith("top:")] if resume else []
    carry: list[str] = []
    # One second of overlap: rows at the cursor's own second are read again (and
    # dropped by id), so a boundary tie is never skipped, whether the forge's
    # ``since`` is inclusive or not.
    since = _second_before(cursor)
    # A comment edited while the pages are read moves to the end and shifts the rows
    # behind it up a page. Taken before the first read, this cutoff keeps any update
    # in or after its second out of the cursor, and the scan incomplete, so the next
    # sweep reads the window again instead of passing a row that moved.
    cutoff = _utc_now_stamp()

    def read(page: int) -> list[dict[str, Any]]:
        found = repo_reader(owner, repo, since, page=page, mention=identity, **kwargs)
        return [dict(c) for c in found or [] if isinstance(c, dict)]

    # A page number is not a stable position: comments read on earlier pages that
    # are edited again leave the tie for the end, and every row behind them moves up,
    # by any number of pages. So a resume first finds the earliest page that still
    # holds a comment nobody handled (unseen, created at or after the floor).
    first = 1 if start > 1 else start  # the first page this sweep reads
    if start > 1:
        unhandled = (
            p for p in range(1, start) if not all(_handled(c, seen, floor) for c in read(p))
        )
        start = next(unhandled, start)
    for page in range(start, start + MAX_TIED_COMMENT_PAGES):
        got = read(page)
        out += got
        stamps = [st for c in got if (st := crew_store.normalize_forge_time(c.get("updated_at")))]
        complete = len(got) < REPO_COMMENTS_PAGE
        # A full page whose rows all fall inside the overlap of its newest time
        # cannot move the next read past them, so the next page is read now.
        if complete or not stamps or min(stamps)[:19] < _second_before(max(stamps))[:19]:
            break
    else:
        carry = [f"page:{page + 1}"]
    seen_times = {crew_store.normalize_forge_time(c.get("updated_at")) for c in out}
    late = {st for st in seen_times if st and st[:19] >= cutoff[:19]}
    seen_times -= late
    top = max([cursor, *tops, *[st for st in seen_times if st]])
    if carry:
        # Every page read was full and one time each: the next sweep resumes near the
        # page after the last one read, so it must ask the SAME ``since`` (a newer
        # one would renumber the pages under it). The cursor waits.
        return out, cursor, [*carry, f"top:{top}"], False
    if page > first:
        # The read ran on through a tie: an edit while it did can have moved a row
        # behind it onto a page already read. The earlier pages are read once more
        # and any row on them nobody has handled joins this scan's comments, so the
        # cursor moving past its update time passes nothing unread.
        got_ids = {str(c.get("id") or "") for c in out}
        out += [
            c
            for p in range(first, page)
            for c in read(p)
            if str(c.get("id") or "") not in got_ids and not _handled(c, seen, floor)
        ]
    return out, top, [], complete and not late


def _handled(comment: dict[str, Any], seen: frozenset[str], floor: str) -> bool:
    created = crew_store.normalize_forge_time(comment.get("created_at"))
    return str(comment.get("id") or "") in seen or bool(created and floor and created < floor)


def _gitlab_comments(
    key: provider.RepoKey,
    client: Any,
    kwargs: dict[str, Any],
    cursor: str,
    at_cursor: Sequence[str],
    identity: str,
    floor: str,
) -> tuple[list[dict[str, Any]], str, list[str], bool]:
    owner, repo = key.owner, key.repo
    thread_reader = getattr(client, "list_thread_comments", None)
    lister = getattr(client, "list_threads_updated_since", None)
    if not callable(thread_reader) or not callable(lister):
        return [], cursor, list(at_cursor), False
    done_at_cursor = set(at_cursor)
    since = _second_before(cursor)
    threads: list[tuple[str, int, bool, list[str]]] = []

    def unread(t: tuple[str, int, bool, list[str]]) -> bool:
        return t[0] > cursor or (t[0] == cursor and _thread_key(t[1], t[2]) not in done_at_cursor)

    # Each list is oldest update first; a FULL page stops short of the present, so
    # nothing past its newest time is read this sweep, and the scan is incomplete. A
    # full page of threads all read already (a tie at the cursor) reads on. The two
    # lists are read one after the other, so a thread updated past a cutoff taken
    # BEFORE both waits for the next sweep: otherwise an MR updated after the issue
    # list was read would move the cursor past an issue updated in between.
    cutoff = _utc_now_stamp()
    bound, complete = "", True

    def listed(is_pr: bool, number: int) -> tuple[list[tuple[str, int, bool, list[str]]], bool]:
        nonlocal complete
        page = lister(owner, repo, since, pr=is_pr, limit=THREAD_PAGE, page=number, **kwargs)
        rows: list[tuple[str, int, bool, list[str]]] = []
        for row in page or []:
            stamp = crew_store.normalize_forge_time(row.get("updated_at"))
            # Whole seconds: a normalized stamp carries a fraction, and anything in
            # the cutoff's own second may still have moved after it.
            if stamp and stamp[:19] >= cutoff[:19]:
                complete = False
            elif isinstance(row.get("number"), int) and stamp:
                rows.append((stamp, row["number"], is_pr, list(row.get("labels") or [])))
        return rows, len(page or []) >= THREAD_PAGE

    # A tie wider than the per-sweep pages resumes on the page after the last one
    # read (``glpage:<#|!>:<n>`` in *at_cursor*), stepping back first to the earliest
    # page still holding an unread thread: a tied thread edited since moves the rest
    # up. The cursor waits at the tie, so the window's ``since`` stays the same.
    carry: list[str] = []
    for is_pr in (False, True):
        tag = f"{_GL_PAGE}{'!' if is_pr else '#'}:"
        saved = [k[len(tag) :] for k in at_cursor if k.startswith(tag)]
        start = int(saved[0]) if saved and saved[0].isdigit() else 1
        if start > 1:
            earlier = (p for p in range(1, start) if any(unread(t) for t in listed(is_pr, p)[0]))
            start = next(earlier, start)
        first = 1 if saved else start  # the first page this sweep lists
        mine: list[tuple[str, int, bool, list[str]]] = []
        for number in range(start, start + MAX_THREAD_PAGES):
            rows, full = listed(is_pr, number)
            mine += rows
            if not full or any(unread(t) for t in rows):
                if full and number > 1:
                    carry.append(f"{tag}{number}")
                break
        else:
            carry.append(f"{tag}{start + MAX_THREAD_PAGES}")
        if number > first:
            # As on GitHub: an edit while the pages were listed can have moved an
            # unread thread onto a page already listed. Those pages are listed once
            # more, and any unread thread on them joins this sweep's.
            have = {(t[1], t[0]) for t in mine}
            again = [t for p in range(first, number) for t in listed(is_pr, p)[0]]
            mine += [t for t in again if unread(t) and (t[1], t[0]) not in have]
        threads += mine
        if full:
            complete = False
            last = max((t[0] for t in mine), default="")
            bound = min(bound, last) if bound else last
    threads = sorted(t for t in threads if (not bound or t[0] <= bound) and unread(t))
    out: list[dict[str, Any]] = []
    next_cursor = cursor
    read_at = [k for k in at_cursor if not k.startswith(_GL_PAGE)] + carry
    for done, (stamp, number, is_pr, labels) in enumerate(threads):
        if done >= MAX_MENTION_THREADS_PER_SWEEP:
            return out, next_cursor, read_at, False
        try:
            # Read back to the creation floor, so no unread note is left behind.
            notes = thread_reader(
                owner, repo, number, pr=is_pr, mention=identity, since=floor, **kwargs
            )
        except Exception as exc:
            partial = getattr(exc, "notes", None)
            if not isinstance(partial, list):
                logger.debug(
                    "issue-radar: notes read for %s#%s failed", owner, number, exc_info=True
                )
                return out, next_cursor, read_at, False
            # One thread too busy to read back to the floor does not hold up the
            # rest: its newest notes are used and the floor stays where it is.
            logger.warning(
                "issue-radar: %s/%s#%s: older notes skipped: %s", owner, repo, number, exc
            )
            notes, complete = partial, False
        for note in notes or []:
            if isinstance(note, dict):
                out.append({**note, "number": number, "is_pr": is_pr, "labels": labels})
        if stamp > next_cursor:
            next_cursor, read_at = stamp, []
        read_at.append(_thread_key(number, is_pr))
    return out, next_cursor, read_at, complete


def _utc_now_stamp() -> str:
    """Now, as a normalized forge time (second precision, UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _second_before(stamp: str) -> str:
    """*stamp* (a normalized forge time) one second earlier, at second precision."""
    try:
        when = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return stamp[:19] + "Z"
    return (when - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _thread_key(number: int, is_pr: bool) -> str:
    return f"{'!' if is_pr else '#'}{number}"
