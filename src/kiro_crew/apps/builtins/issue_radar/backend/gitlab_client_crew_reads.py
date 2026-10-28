"""The GitLab read the crew wake sweep makes for mentions: one thread's newest notes.

Split out of :mod:`gitlab_client` to keep that module under the file line cap; callers
reach it as ``gitlab_client.list_thread_comments``. The transport and normalisation
helpers are looked up on :mod:`gitlab_client` at call time, so a test patching
``gitlab_client._glab_api`` reaches this read too.
"""

from __future__ import annotations

from . import gitlab_client as _gl

#: The longest id or timestamp kept from a note; an ISO-8601 time with an offset fits.
_TIME_MAX = 64
#: The longest note body kept. A mention sits near the start of a comment; a longer
#: body is cut here rather than held whole for the sweep.
NOTE_BODY_MAX = 4000


def _bounded_time(value: object) -> str | None:
    return value[:_TIME_MAX] if isinstance(value, str) else None


def list_thread_comments(
    owner: str,
    repo: str,
    number: int,
    *,
    pr: bool = False,
    limit: int = 50,
    host: str = "",
    timeout: float = _gl.GL_TIMEOUT_SEC,
) -> list[dict]:
    """The newest human notes on one issue (or MR when *pr*), oldest first.

    ONE page, newest *limit* notes, system notes dropped: the crew wake sweep reads
    only threads updated since its cursor and wants who said what and when, not the
    timeline. Returns ``[{id, actor, created_at, body}]`` with ``id`` as text and
    ``body`` cut at :data:`NOTE_BODY_MAX` characters.
    """
    kind = "merge_requests" if pr else "issues"
    capped = max(1, min(int(limit), _gl._PAGE_SIZE))
    path = (
        f"projects/{_gl.project_path(owner, repo)}/{kind}/{int(number)}/notes"
        f"?order_by=created_at&sort=desc&per_page={capped}"
    )
    out: list[dict] = []
    for note in _gl._rows(_gl._glab_api(path, host=host, timeout=timeout))[:capped]:
        if note.get("system") or note.get("id") is None:
            continue
        out.append(
            {
                "id": str(note.get("id"))[:_TIME_MAX],
                "actor": (_gl._username(note.get("author")) or "")[:256],
                "created_at": _bounded_time(note.get("created_at")),
                "body": str(note.get("body") or "")[:NOTE_BODY_MAX],
            }
        )
    out.sort(key=lambda n: n.get("created_at") or "")
    return out
