"""The GitLab reads the crew wake sweep makes for mentions: the threads updated since
its cursor, and one thread's newest notes.

Split out of :mod:`gitlab_client` to keep that module under the file line cap; callers
reach it as ``gitlab_client.list_thread_comments``. The transport and normalisation
helpers are looked up on :mod:`gitlab_client` at call time, so a test patching
``gitlab_client._glab_api`` reaches this read too.
"""

from __future__ import annotations

from urllib.parse import quote

from . import gitlab_client as _gl
from . import mention_match

#: The longest id or timestamp kept from a note; an ISO-8601 time with an offset fits.
_TIME_MAX = 64
#: The longest note body kept. A mention sits near the start of a comment; a longer
#: body is cut here rather than held whole for the sweep.
NOTE_BODY_MAX = 4000
#: Pages read back toward *since* before the read gives up as incomplete.
MAX_NOTE_PAGES = 5


class NotesIncomplete(RuntimeError):
    """The newest :data:`MAX_NOTE_PAGES` pages did not reach back to *since*.

    ``notes`` holds what those pages did return, in the read's own shape, so the
    caller can use them and move on rather than read the same pages forever.
    """

    def __init__(self, message: str, notes: list[dict]) -> None:
        super().__init__(message)
        self.notes = notes


def _bounded_time(value: object) -> str | None:
    return value[:_TIME_MAX] if isinstance(value, str) else None


def list_thread_comments(
    owner: str,
    repo: str,
    number: int,
    *,
    pr: bool = False,
    limit: int = 50,
    mention: str = "",
    since: str = "",
    host: str = "",
    timeout: float = _gl.GL_TIMEOUT_SEC,
) -> list[dict]:
    """The newest human notes on one issue (or MR when *pr*), oldest first.

    Newest *limit* notes a page, system notes dropped: the crew wake sweep reads
    only threads updated since its cursor and wants who said what and when, not the
    timeline. With *since* (the sweep's creation floor) pages are read back until one
    reaches a note created before it, so a burst of newer notes cannot hide an older
    unread one; :class:`NotesIncomplete` (carrying the notes read) when
    :data:`MAX_NOTE_PAGES` do not reach it, so the caller knows the read was cut.
    Returns ``[{id, actor, created_at, body, asked}]`` with ``id`` as text, ``body`` cut at :data:`NOTE_BODY_MAX` characters and ``asked`` whether the WHOLE
    body addresses ``@mention`` (:func:`mention_match.mentions`).
    """
    kind = "merge_requests" if pr else "issues"
    capped = max(1, min(int(limit), _gl._PAGE_SIZE))
    path = (
        f"projects/{_gl.project_path(owner, repo)}/{kind}/{int(number)}/notes"
        f"?order_by=created_at&sort=desc&per_page={capped}"
    )
    notes: list[dict] = []
    for page in range(1, MAX_NOTE_PAGES + 1):
        got = _gl._rows(_gl._glab_api(f"{path}&page={page}", host=host, timeout=timeout))
        notes += got[:capped]
        made = [t[:19] for n in got if (t := _bounded_time(n.get("created_at")))]
        if len(got) < capped or not since or (made and min(made) < since[:19]):
            return _human_notes(notes, mention)
    message = f"{kind}/{int(number)}: notes since {since} exceed {MAX_NOTE_PAGES} pages"
    raise NotesIncomplete(message, _human_notes(notes, mention))


def _human_notes(notes: list[dict], mention: str) -> list[dict]:
    out: list[dict] = []
    for note in notes:
        if note.get("system") or note.get("id") is None:
            continue
        out.append(
            {
                "id": str(note.get("id"))[:_TIME_MAX],
                "actor": (_gl._username(note.get("author")) or "")[:256],
                "created_at": _bounded_time(note.get("created_at")),
                "body": str(note.get("body") or "")[:NOTE_BODY_MAX],
                "asked": mention_match.mentions(str(note.get("body") or ""), mention),
            }
        )
    out.sort(key=lambda n: n.get("created_at") or "")
    return out


def list_threads_updated_since(
    owner: str,
    repo: str,
    since: str,
    *,
    pr: bool = False,
    limit: int = 100,
    page: int = 1,
    host: str = "",
    timeout: float = _gl.GL_TIMEOUT_SEC,
) -> list[dict]:
    """Issues (or MRs when *pr*) updated at or after *since*, OLDEST update first, in
    every state: a mention on a closed issue or a merged MR is still a question.

    Page *page* of at most *limit*: sorted this way, a full page ends short of the
    present and the rows after it are all newer, so a caller that moves its cursor
    only as far as it read never skips a thread. Returns ``[{number, labels,
    updated_at}]``.
    """
    kind = "merge_requests" if pr else "issues"
    capped = max(1, min(int(limit), _gl._PAGE_SIZE))
    path = (
        f"projects/{_gl.project_path(owner, repo)}/{kind}"
        f"?scope=all&order_by=updated_at&sort=asc&per_page={capped}&page={max(1, int(page))}"
    )
    if since:
        path += f"&updated_after={quote(since, safe='')}"
    out: list[dict] = []
    for row in _gl._rows(_gl._glab_api(path, host=host, timeout=timeout))[:capped]:
        if isinstance(row.get("iid"), int):
            labels = [str(name)[:256] for name in row.get("labels") or [] if name][:100]
            stamp = _bounded_time(row.get("updated_at"))
            out.append({"number": row["iid"], "labels": labels, "updated_at": stamp})
    return out
