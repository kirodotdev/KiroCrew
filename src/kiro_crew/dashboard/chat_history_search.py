"""One chat's history search window: find older messages without loading them.

``GET /api/chat/slots/{slot}/history/search`` walks the slot's transcript from the
newest row back, one indexed page at a time (``read_messages_chained_page``), and
lists the user and assistant messages that match the text query, the date range
and the attachment kind. Each reply scans at most ``SCAN_BUDGET_ROWS`` rows and
says where it stopped (``next_before``), so a long history is searched in steps
and never read whole by one request.

``GET /api/chat/slots/{slot}/history/around`` reads one bounded range of the same
rows, prepared for display the way the slot detail read prepares them, so the
dashboard can preview a hit with the messages around it without touching the
chat's own loaded window.

Both are per-slot routes: the slot-ownership checkpoint decides an app caller
(owner app only), and the dashboard's own auth admits the person.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from aiohttp import web

from kiro_crew import history_search
from kiro_crew.dashboard.chat_pins import slot_pins
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.chat_utils_display import _prepare_messages
from kiro_crew.dashboard.display_floor import _hidden_row_count, clip_to_floor, read_display_floor
from kiro_crew.dashboard.handlers.files import _ALLOWED_VIDEO_EXT
from kiro_crew.dashboard.handlers.sessions import message_hits, message_query
from kiro_crew.dashboard.slot_ownership import (
    deny_app_slot_access,
    is_member_thread,
    slot_not_found,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.system_notices import is_speech_row
from kiro_crew.dashboard.transcript_snapshot import SNAPSHOT_ATTEMPTS
from kiro_crew.history import SEARCH_MIN_CHARS
from kiro_crew.history_projection import TranscriptRevisionChanged
from kiro_crew.messaging.outbound_files import (
    is_remote_destination,
    iter_local_refs,
    local_destination,
)
from kiro_crew.security import redact
from kiro_crew.validation import sanitize_string
from kiro_crew.widget_parse import parse_widgets

logger = logging.getLogger(__name__)

#: Rows decoded per indexed page read while the search walks back.
SCAN_PAGE_ROWS = 200
#: Most rows one search reply looks at; ``next_before`` resumes the walk.
SCAN_BUDGET_ROWS = 4000
#: Most rows one ``counts`` reply looks at, the whole of almost any chat; past
#: it the reply says ``complete: false``.
COUNT_BUDGET_ROWS = 100_000
#: Hits per reply, by default and at most.
DEFAULT_HITS = 30
MAX_HITS = 50
#: Most rows one ``around`` read returns.
MAX_AROUND_ROWS = 80
#: Characters of a filter-only hit's excerpt (no text query to anchor on).
PLAIN_SNIPPET_MAX = 200
#: Thumbnail paths listed per hit; ``image_count`` still counts them all.
MAX_THUMBS = 4
#: Names listed per hit for each of its videos, PDFs, HTML files and widgets;
#: the ``*_count`` fields still count them all.
MAX_NAMES = 4
#: The user-row ``meta`` fields a hit keeps, so search names a sender the way the transcript does.
_SENDER_META = ("author", "turnActor", "appLabel")
#: Saved-artifact links classified per message; each costs one ``kind_of`` read.
#: shortcut: a link past this many in one reply is not classified; raise it if
#: replies with longer link lists become common.
MAX_ARTIFACT_LINKS = 64
#: Characters kept of each listed name or title; a longer one is cut with an ellipsis.
MAX_LABEL_CHARS = 120
#: A thumbnail path longer than this is left out rather than cut (a cut path names another file).
MAX_THUMB_PATH_CHARS = 1024
#: The roles a person reads as messages; tool and system rows never match.
_ROLES = frozenset({"user", "assistant"})
#: ``kind`` values: every message, one with an attached file, one with a picture,
#: one with a web link, a video, a PDF, an HTML file, or a widget.
KINDS = ("", "files", "images", "links", "videos", "pdfs", "html", "widgets", "pinned")
#: The video containers the chat accepts at its upload boundary, the one list.
_VIDEO_EXTS = frozenset(_ALLOWED_VIDEO_EXT)
_HTML_EXTS = frozenset({".html", ".htm"})
#: How a saved artifact appears in chat text (``mcp_core._artifact_ref_link``).
_ARTIFACT_LINK = "](/artifacts/"
#: Characters read after ``_ARTIFACT_LINK`` for a slug: the store's longest is 80.
_ARTIFACT_SLUG_SCAN = 81
_ATTACHED_FILE_RE = re.compile(r"\[attached_file \d+\] (\S+)")
_IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|bmp|svg)$", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>()\[\]|`]+")
#: The label and the URL both stop at ``[``, the one character a match starts
#: at, so a run of ``[`` or of ``[a](https://`` is scanned once, not once per ``[``.
_MD_LINK_RE = re.compile(r"\[([^\[\]\n]*)\]\((https?://[^\s()\[\]]+)\)")
#: Markdown punctuation a snippet drops: table pipes and rules, headings,
#: emphasis, list markers, quote markers and code fences. Linear on any input:
#: the line-start blanks are ``[ \t]*`` (one line, read once), and a table rule
#: starts only at a pipe or at the first dash of a run (``(?<!-)``), so no
#: whitespace or dash run is re-read from each of its characters.
_MD_NOISE_RE = re.compile(
    r"(?m)^[ \t]*(?:#{1,6}\s+|>\s?|[-*+]\s+(?=\S))"
    r"|(?:\|[ \t]*)?(?<!-):?-{3,}:?\s*(?=\||$)|[|`]|\*\*|__|~~"
)
#: Longest URL a snippet keeps whole; a longer one shows its host only.
URL_SNIPPET_MAX = 40
_BAD = object()
T = TypeVar("T")


def _read_settled(read: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """``read(*args, **kwargs)``, run again from a fresh probe when a write moved
    the transcript under it, at most ``SNAPSHOT_ATTEMPTS`` times in all (the slot
    detail read's bound); the last attempt's ``TranscriptRevisionChanged`` is
    raised. Blocking IO, off the loop."""
    for attempt in range(1, SNAPSHOT_ATTEMPTS):
        try:
            return read(*args, **kwargs)
        except TranscriptRevisionChanged:
            logger.debug("history read moved (attempt %d/%d); retrying", attempt, SNAPSHOT_ATTEMPTS)
    return read(*args, **kwargs)


def _default_artifact_store() -> Any:
    from kiro_crew.artifacts import get_default_store

    return get_default_store()


def _bad(code: str, error: str) -> web.Response:
    return web.json_response({"error": error, "code": code}, status=400)


def _epoch(raw: str) -> float | None | object:
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return _BAD
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return _BAD
    return value


def _int(raw: str | None) -> int | None | object:
    if raw is None or raw.strip() == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        return _BAD
    return _BAD if value < 0 else value


def _row_epoch(ts: object) -> float | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def row_files(msg: dict) -> list[str]:
    """The files attached to *msg*, the way the chat's own renderer reads them."""
    meta = msg.get("meta")
    files = meta.get("files") if isinstance(meta, dict) else None
    if isinstance(files, list):
        listed = [f for f in files if isinstance(f, str) and f]
        if listed:
            return listed
    content = msg.get("content")
    return _ATTACHED_FILE_RE.findall(content) if isinstance(content, str) else []


def row_images(msg: dict, files: list[str]) -> list[str]:
    """Local pictures *msg* shows: attached image files, then inline images."""
    # Absolute local paths only: a URL here would make the panel load a remote
    # picture with no consent, so a "file" that is a link is never a thumbnail.
    images = [
        str(path)
        for f in files
        if _IMAGE_EXT_RE.search(f)
        and (path := local_destination(f)) is not None
        and not is_remote_destination(str(path))
    ]
    content = msg.get("content")
    if isinstance(content, str) and "![" in content:
        try:
            # shortcut: remote (http) images are not counted, since the window has
            # no consent to load them; count them too if a reader asks for it.
            images.extend(
                str(path)
                for ref in iter_local_refs(content)
                # The chat plays a video ref as a video, never as a picture.
                if _ext(ref.dest) not in _VIDEO_EXTS
                and (path := local_destination(ref.dest)) is not None
                and not is_remote_destination(str(path))
                and str(path) not in images
            )
        except Exception:
            logger.debug("history search: image scan failed", exc_info=True)
    return images


def _basename(path: str) -> str:
    """The last segment of *path*, on either separator."""
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _ext(path: str) -> str:
    """*path*'s extension, lower case, with its dot (``""`` when none)."""
    return posixpath.splitext(_basename(path))[1].lower()


def file_record(msg: dict) -> dict | None:
    """The record a ``file_send`` row carries (``file_api/outbox.py``): JSON with
    at least a ``filename``. None for any other row or a malformed record."""
    content = msg.get("content")
    if msg.get("role") != "file" or not isinstance(content, str):
        return None
    try:
        record = json.loads(content)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    name = record.get("filename")
    return record if isinstance(name, str) and name.strip() else None


def row_videos(msg: dict, files: list[str]) -> list[str]:
    """The videos *msg* shows: attached video files, then local inline refs to one."""
    videos = [f for f in files if _ext(f) in _VIDEO_EXTS]
    content = msg.get("content")
    if msg.get("role") in _ROLES and isinstance(content, str) and "![" in content:
        try:
            videos.extend(
                ref.dest
                for ref in iter_local_refs(content)
                if _ext(ref.dest) in _VIDEO_EXTS and ref.dest not in videos
            )
        except Exception:
            logger.debug("history search: video scan failed", exc_info=True)
    return videos


def readable_text(content: str) -> str:
    """*content* without its attachment markers and local image markup.

    The window shows pictures and files as badges, so their paths would only
    repeat them, and a query should match what the person wrote, not a path.
    """
    text = _ATTACHED_FILE_RE.sub("", content)
    if "![" not in text:
        return text
    try:
        refs = iter_local_refs(text)
    except Exception:
        logger.debug("history search: image scan failed", exc_info=True)
        return text
    for ref in reversed(refs):
        text = text[: ref.start] + text[ref.end :]
    return text


def row_links(text: str) -> list[str]:
    """The web links *text* carries, in order."""
    return _URL_RE.findall(text)


def artifact_links(text: str) -> list[tuple[str, str]]:
    """The first ``MAX_ARTIFACT_LINKS`` saved-artifact links in *text*, as ``(label, slug)``.

    Every one is classified, so a later link still counts; ``MAX_NAMES`` caps
    only the names a hit lists.

    Plain ``str.find`` scans, each from where the last one stopped, so the walk is
    linear in *text*. The slug is whatever sits before the ``)``, unchecked: the
    store's ``kind_of`` decides whether it names an artifact.
    """
    links: list[tuple[str, str]] = []
    start = 0
    while len(links) < MAX_ARTIFACT_LINKS:
        at = text.find(_ARTIFACT_LINK, start)
        if at < 0:
            break
        slug_at = at + len(_ARTIFACT_LINK)
        end = text.find(")", slug_at, slug_at + _ARTIFACT_SLUG_SCAN)
        opener = text.rfind("[", start, at)
        start = slug_at
        if end < 0 or opener < 0:
            continue
        label = text[opener + 1 : at]
        if "]" in label or "\n" in label:
            continue
        links.append((label.strip(), text[slug_at:end]))
        start = end + 1
    return links


def _short_url(match: re.Match) -> str:
    url = match.group(0)
    if len(url) <= URL_SNIPPET_MAX:
        return url
    host = url.split("://", 1)[1].split("/", 1)[0]
    return host + "/…"


def snippet_text(text: str) -> str:
    """*text* as a person reads it: link labels instead of markdown links, long
    URLs cut to their host, and table and emphasis markup dropped."""
    text = _MD_LINK_RE.sub(lambda m: m.group(1) or m.group(2), text)
    text = _URL_RE.sub(_short_url, text)
    return _MD_NOISE_RE.sub(" ", text)


def _plain_snippet(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= PLAIN_SNIPPET_MAX else flat[:PLAIN_SNIPPET_MAX] + "…"


def _without_widgets(content: str, widgets: list) -> str:
    """*content* with every widget span cut out, the prose around them kept."""
    for widget in reversed(widgets):
        start, end = widget.span
        content = content[:start] + "\n" + content[end:]
    return content


class _Row:
    """What one transcript row carries, read once: the text a query matches, the
    excerpt source, and each attachment kind. ``kinds`` names every ``kind``
    filter the row passes ("" always)."""

    __slots__ = (
        "role",
        "match",
        "prose",
        "shown",
        "files",
        "images",
        "links",
        "videos",
        "pdfs",
        "html",
        "html_labels",
        "widgets",
        "kinds",
    )

    def __init__(self, role: str, files: list[str], prose: str, record: dict | None) -> None:
        self.role = role
        self.files = files
        self.prose = prose
        self.widgets: list[str] = []
        self.match = prose
        self.shown = ""
        self.images: list[str] = []
        self.links: list[str] = []
        self.videos: list[str] = []
        content_type = str(record.get("content_type") or "") if record else ""
        self.pdfs = [f for f in files if _ext(f) == ".pdf"]
        self.html = [f for f in files if _ext(f) in _HTML_EXTS]
        # A sent file's own type, when its name has no telling extension.
        if record and not self.pdfs and content_type == "application/pdf":
            self.pdfs = list(files)
        if record and not self.html and content_type == "text/html":
            self.html = list(files)
        # Labels of linked saved html artifacts: names as the chat shows them, not paths.
        self.html_labels: list[str] = []
        self.kinds: set[str] = set()


def _read_row(
    msg: dict, *, artifact_kind: Callable[[str], str | None] | None = None
) -> _Row | None:
    """*msg* as a search reads it, or None when it is not a message the chat
    draws: only speech (``is_speech_row``, the backend twin of what a chat draws
    as a message) and ``file_send`` cards, which both chats draw.

    *artifact_kind* names the kind of a linked artifact the caller may see (None
    otherwise); with it, a link to a saved html artifact counts as HTML."""
    role, content = msg.get("role"), msg.get("content")
    if role == "file":
        record = file_record(msg)
        if record is None:
            return None
        path = record.get("path")
        name = record["filename"].strip()
        attached = path if isinstance(path, str) and path.strip() else name
        row = _Row("file", [attached], "", record)
        # A sent file's card names it: the name is its text and its excerpt,
        # matched as the redacted form the result shows.
        row.shown = redact(name)
        row.match = row.shown
        row.images = row_images({"role": "file"}, row.files)
        row.videos = row_videos(msg, row.files)
    else:
        if role not in _ROLES or not isinstance(content, str):
            return None
        if not is_speech_row(role, content, msg.get("meta"), variants=msg.get("variants")):
            return None
        widgets = parse_widgets(content) if role == "assistant" and "<mcwidget" in content else []
        # A widget's HTML body is not what the message says: its title is.
        prose = readable_text(_without_widgets(content, widgets) if widgets else content)
        row = _Row(role, row_files(msg), prose, None)
        row.widgets = [w.title for w in widgets]
        row.images = row_images(msg, row.files)
        row.links = row_links(prose)
        row.videos = row_videos(msg, row.files)
        if artifact_kind is not None and _ARTIFACT_LINK in prose:
            # A saved html artifact is an HTML file the chat links instead of attaching.
            row.html_labels.extend(
                label or slug
                for label, slug in artifact_links(prose)
                if artifact_kind(slug) == "html"
            )
        # The query matches the redacted text the result shows. Redacted FIRST:
        # shortening a URL drops the scheme a credential pattern keys on.
        shown_prose = redact(prose)
        row.match = (
            "\n".join([shown_prose, *(redact(t) for t in row.widgets)]) if widgets else shown_prose
        )
        row.shown = snippet_text(shown_prose) if prose.strip() else redact(", ".join(row.widgets))
    row.kinds = {
        k
        for k, has in (
            ("", True),
            ("files", row.files),
            ("images", row.images),
            ("links", row.links),
            ("videos", row.videos),
            ("pdfs", row.pdfs),
            ("html", row.html or row.html_labels),
            ("widgets", row.widgets),
        )
        if has
    }
    return row


def _row_matches(row: _Row, query: Any) -> bool:
    """Whether *row* survives the text query, or, with none, has anything to show."""
    if query is not None:
        # The query matches what the message says, redacted, URLs whole.
        return history_search.needles_match_text(
            query.needles, row.match.casefold(), query.adjacency_floor
        )
    return bool(row.match.strip()) or len(row.kinds) > 1


def _label(text: str) -> str:
    """*text* redacted, then cut to ``MAX_LABEL_CHARS``, as a hit lists it."""
    shown = redact(text)
    return shown if len(shown) <= MAX_LABEL_CHARS else shown[: MAX_LABEL_CHARS - 1] + "\u2026"


def _names(paths: list[str]) -> list[str]:
    return [_label(_basename(p)) for p in paths[:MAX_NAMES]]


def _hit_of(msg: dict, index: int, row: _Row, query: Any) -> dict:
    """The hit *row* makes at *index*: the excerpt, counts and preview names."""
    shown = row.shown
    if query is not None:
        found, _count = message_hits(
            [{**msg, "role": "user", "content": shown}], query, _ROLES, max_hits=1
        )
        snippet = found[0]["snippet"] if found else _plain_snippet(shown)
    else:
        snippet = _plain_snippet(shown)
    hit: dict[str, Any] = {"index": index, "role": row.role, "snippet": snippet}
    if isinstance(msg.get("ts"), str):
        hit["ts"] = msg["ts"]
    meta = msg.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("mid"), str):
        hit["mid"] = meta["mid"]
    # Who sent a user row, as the transcript names it (`MessageByline`): the
    # server-stamped author, and an app send's actor and app label.
    sender = (
        {k: meta[k] for k in _SENDER_META if k in meta}
        if isinstance(meta, dict) and row.role == "user"
        else {}
    )
    if sender:
        hit["meta"] = sender
    other_files = [f for f in row.files if not _IMAGE_EXT_RE.search(f)]
    if other_files:
        hit["file_count"] = len(other_files)
    if row.links:
        hit["link_count"] = len(row.links)
    if row.images:
        hit["image_count"] = len(row.images)
        # A path redact would change carries a secret; a redacted path names
        # another file, so that picture gets no thumbnail (it is still counted).
        thumbs = [p for p in row.images if len(p) <= MAX_THUMB_PATH_CHARS and redact(p) == p][
            :MAX_THUMBS
        ]
        if thumbs:
            hit["images"] = thumbs
    for field, paths in (("video", row.videos), ("pdf", row.pdfs), ("html", row.html)):
        if paths:
            hit[f"{field}_count"] = len(paths)
            hit[f"{field}_names"] = _names(paths)
    if row.html_labels:
        # A label is shown whole: cutting it at a "/" like a path would drop part of it.
        hit["html_count"] = len(row.html) + len(row.html_labels)
        names = hit.get("html_names", []) + [_label(t) for t in row.html_labels]
        hit["html_names"] = names[:MAX_NAMES]
    if row.widgets:
        hit["widget_count"] = len(row.widgets)
        hit["widget_titles"] = [_label(t) for t in row.widgets[:MAX_NAMES]]
    return hit


def _hit(
    msg: dict,
    index: int,
    query: Any,
    kind: str,
    *,
    artifact_kind: Callable[[str], str | None] | None = None,
) -> dict | None:
    """The hit *msg* makes at *index*, or None when a filter drops it."""
    row = _read_row(msg, artifact_kind=artifact_kind)
    if row is None or kind not in row.kinds or not _row_matches(row, query):
        return None
    return _hit_of(msg, index, row, query)


def artifact_kinds(get_store: Callable[[], Any], app: str) -> Callable[[str], str | None]:
    """One request's linked-artifact kind lookup: each slug read once (``kind_of``).

    For an app caller an artifact counts only when its token may read it through
    the artifact routes (``GET /api/artifacts/<slug>``, the same app-scope rule
    the auth middleware applies there); the dashboard user (no *app*) sees all.
    The store is opened at the first lookup. Blocking IO (the store, meta.json,
    the app's grant): call it off the loop.
    """
    from kiro_crew.dashboard.token_auth_app_scope import app_token_path_allowed

    cache: dict[str, str | None] = {}
    stores: list[Any] = []

    def kind(slug: str) -> str | None:
        if slug in cache:
            return cache[slug]
        found = None
        try:
            if not stores:
                stores.append(get_store())
            found = stores[0].kind_of(slug)
        except Exception:
            logger.debug("history search: artifact lookup failed", exc_info=True)
        if found is not None and app and not app_token_path_allowed(app, f"/api/artifacts/{slug}"):
            found = None
        cache[slug] = found
        return found

    return kind


def search_history(
    log: Any,
    key: str,
    *,
    query: Any,
    kind: str,
    since: float | None,
    until: float | None,
    before: int | None,
    limit: int,
    floor: str = "",
    artifact_kind: Callable[[str], str | None] | None = None,
    pinned: Pinned | None = None,
    budget: int | None = None,
    pin_revision: bool = True,
) -> dict[str, Any]:
    """Walk *key*'s rows newest first from *before*; blocking file IO, off the loop.

    Stops at *limit* hits, after *budget* rows (``SCAN_BUDGET_ROWS`` when None),
    at the transcript's first row, or at a page whose every dated row is older
    than *since* (rows are written in time order). ``next_before`` is the index
    the next reply resumes below, or None when nothing older is left to search.
    A *limit* of 0 lists no hit and only counts. With *pin_revision* off the
    pages are not pinned to the probe's revision, so a write landing during a
    long walk does not void it.

    The ``file_send`` cards are searched too, by file name. ``counts`` says, for every ``kind``, how many of the
    rows this reply scanned match the query and dates with that kind, the active
    one included: the rows a later reply scans add to it.

    *artifact_kind* (``artifact_kinds``) lets a linked saved html artifact count
    as HTML; *pinned* (``pinned_of``) names the rows this chat has pinned.
    """
    hits: list[dict] = []
    counts = dict.fromkeys(KINDS, 0)
    probe = log.read_messages_chained_page(key, limit=0)
    total = probe.total
    revision = probe.revision if pin_revision else None
    budget = SCAN_BUDGET_ROWS if budget is None else budget
    cursor = total if before is None else min(before, total)
    scanned = 0
    later_users: set[tuple[str, str | None]] = set()
    if cursor < total:
        # A resumed reply starts below rows an earlier reply already read: read
        # the page above it again, so a steer there whose successor sits above
        # the resume point stays hidden, as it was on the first reply.
        # shortcut: one page (SCAN_PAGE_ROWS) above; a successor further up is
        # not seen, which only shows a hidden steer once.
        above = log.read_messages_chained_page(
            key,
            limit=min(SCAN_PAGE_ROWS, total - cursor),
            before=min(total, cursor + SCAN_PAGE_ROWS),
            expected_revision=revision,
        )
        for msg in above.messages:
            later = _successor_key(msg) if isinstance(msg, dict) else None
            if later is not None:
                later_users.add(later)
    while cursor > 0 and scanned < budget:
        page = log.read_messages_chained_page(
            key, limit=min(SCAN_PAGE_ROWS, cursor), before=cursor, expected_revision=revision
        )
        rows = page.messages
        start = page.next_before
        floor_reached = False
        if floor:
            rows, start, floor_reached = clip_to_floor(rows, start, floor)
        all_older = bool(rows)
        for offset in range(len(rows) - 1, -1, -1):
            index = start + offset
            msg = rows[offset]
            scanned += 1
            at = _row_epoch(msg.get("ts")) if isinstance(msg, dict) else None
            if at is None or since is None or at >= since:
                all_older = False
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                meta = msg.get("meta")
                meta = meta if isinstance(meta, dict) else {}
                content = msg["content"]
                successor = meta.get("supersededBy")
                # Twin of groupDisplayItems.supersededSuccessorIdx: only a later
                # non-steer user row with the same text replaces this steer.
                # The id-less fallback covers the chat's queue-pop rows.
                if (
                    meta.get("steer") is True
                    and meta.get("steerState") == "superseded"
                    and isinstance(successor, str)
                    and successor
                    and ((content, successor) in later_users or (content, None) in later_users)
                ):
                    continue
                later = _successor_key(msg)
                if later is not None:
                    later_users.add(later)
            if at is not None and (
                (until is not None and at >= until) or (since is not None and at < since)
            ):
                continue
            row = _read_row(msg, artifact_kind=artifact_kind)
            if row is None or not _row_matches(row, query):
                continue
            if pinned is not None and pinned.holds(msg):
                row.kinds.add("pinned")
            for k in row.kinds:
                counts[k] += 1
            if limit <= 0 or kind not in row.kinds:
                continue
            hits.append(_hit_of(msg, index, row, query))
            if len(hits) >= limit:
                return {
                    "hits": hits,
                    "next_before": index or None,
                    "total": total,
                    "counts": counts,
                }
        cursor = start
        if floor_reached or (since is not None and all_older) or not rows:
            cursor = 0
    return {"hits": hits, "next_before": cursor or None, "total": total, "counts": counts}


class Pinned:
    """The rows one chat has pinned, as the pins store names them (``chat_pins``).

    A pin names its row by ``mid``; a pin saved before rows had ids names it by
    its ``message_ts`` alone, so such a pin matches the row with that time.
    """

    __slots__ = ("mids", "times")

    def __init__(self, pins: list[dict]) -> None:
        self.mids = {p["mid"] for p in pins if isinstance(p.get("mid"), str) and p["mid"]}
        self.times = {
            p["message_ts"]
            for p in pins
            if not p.get("mid") and isinstance(p.get("message_ts"), str) and p["message_ts"]
        }

    def holds(self, msg: dict) -> bool:
        meta = msg.get("meta")
        mid = meta.get("mid") if isinstance(meta, dict) else None
        if isinstance(mid, str) and mid in self.mids:
            return True
        return bool(self.times) and msg.get("ts") in self.times


def _successor_key(msg: dict) -> tuple[str, str | None] | None:
    """The ``(text, mid)`` a non-steer user row offers a superseded steer above it, or None."""
    content = msg.get("content")
    if msg.get("role") != "user" or not isinstance(content, str):
        return None
    meta = msg.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    mid = meta.get("mid")
    if meta.get("steer") or not (isinstance(mid, str) or not mid):
        return None
    return (content, mid or None)


def read_around(
    log: Any,
    key: str,
    *,
    start: int,
    end: int,
    floor: str = "",
    speech_only: bool = False,
    mid: str | None = None,
) -> dict[str, Any] | None:
    """Rows ``[start, end)`` of *key*, clipped to the transcript; blocking IO.

    *speech_only* keeps only the messages a bot chat draws (``is_speech_row``)
    and its ``file_send`` cards.
    ``has_earlier`` is False at the first row or at the display floor.
    With *mid*, re-centre on that message in one pinned revision. Check the
    requested range first, then scan at most ``SCAN_BUDGET_ROWS`` recent rows.
    Return None if the message is not found or is behind the display floor.
    """
    probe = log.read_messages_chained_page(key, limit=0)
    total = probe.total
    width = end - start
    end = min(end, total)
    start = min(start, end)
    page = log.read_messages_chained_page(
        key, limit=end - start, before=end, expected_revision=probe.revision
    )
    if mid is not None:
        cursor, scanned = total, 0
        while True:
            index = None
            for offset, row in enumerate(page.messages):
                meta = row.get("meta") if isinstance(row, dict) else None
                if isinstance(meta, dict) and meta.get("mid") == mid:
                    index = page.next_before + offset
                    break
            if index is not None:
                start = max(0, index - width // 2)
                end = min(total, index + (width + 1) // 2)
                page = log.read_messages_chained_page(
                    key, limit=end - start, before=end, expected_revision=probe.revision
                )
                break
            if cursor <= 0 or scanned >= SCAN_BUDGET_ROWS:
                # shortcut: a moved hit outside the scan budget needs a fresh
                # search; add a resumable id lookup if that becomes common.
                return None
            limit = min(SCAN_PAGE_ROWS, cursor, SCAN_BUDGET_ROWS - scanned)
            page = log.read_messages_chained_page(
                key, limit=limit, before=cursor, expected_revision=probe.revision
            )
            scanned += limit
            cursor = page.next_before
    rows, first = page.messages, page.next_before
    reached = False
    if floor:
        rows, first, reached = clip_to_floor(rows, first, floor)
        if not reached and first > 0:
            # A page that starts exactly at the floor holds no hidden row, yet
            # nothing older is served: the check `clip_page_off_loop` makes.
            reached = first <= _hidden_row_count(log, key, floor)
    last = first + len(rows)
    if speech_only:
        rows = [
            m
            for m in rows
            if isinstance(m, dict)
            and (
                is_speech_row(
                    m.get("role"), m.get("content"), m.get("meta"), variants=m.get("variants")
                )
                or file_record(m) is not None
            )
        ]
    if mid is not None and not any(
        isinstance(m.get("meta"), dict) and m["meta"].get("mid") == mid for m in rows
    ):
        return None
    return {
        "rows": rows,
        "start": first,
        "end": last,
        "total": total,
        "has_earlier": first > 0 and not reached,
    }


def _slot_or_refusal(
    request: web.Request, operation: str
) -> tuple[Any, DashboardState] | web.Response:
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, operation)
    if denied is not None:
        return denied
    if not state.conversation_log:
        return web.json_response(
            {"error": "history is not available", "code": "history_unavailable"}, status=503
        )
    return slot, state


async def _floor(state: DashboardState, key: str) -> str:
    log = state.conversation_log
    if log is None or not key.startswith("cron:"):
        return ""
    return await asyncio.to_thread(read_display_floor, log, key)


def _filters(
    request: web.Request,
) -> tuple[Any, float | None, float | None, bool] | web.Response:
    """The ``q`` / ``since`` / ``until`` both search routes read: the parsed
    query (None under ``SEARCH_MIN_CHARS``), the two times, and whether the
    query parses to no searchable term (nothing can match)."""
    q = sanitize_string(request.query.get("q", "")).strip()[:256]
    since = _epoch(request.query.get("since", ""))
    until = _epoch(request.query.get("until", ""))
    if since is _BAD or until is _BAD:
        return _bad("invalid_time", "since and until must be times in seconds")
    query = message_query(q) if len(q) >= SEARCH_MIN_CHARS else None
    return (
        query,
        since if isinstance(since, float) else None,
        until if isinstance(until, float) else None,
        query is not None and not query.needles,
    )


def _caller_scope(
    request: web.Request, state: DashboardState
) -> tuple[Callable[[str], str | None], Pinned]:
    """What the caller may see: of a linked artifact (the caller the auth
    middleware named: an app sees only the artifacts its token may read) and
    of the pins (the pins list's own rule, ``slot_pins``: an app sees only its
    pins)."""
    app = request.get("app", "") or ""
    return (
        artifact_kinds(_default_artifact_store, app),
        Pinned(slot_pins(state, request.match_info["slot"], app)),
    )


async def api_chat_slot_history_search(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/history/search — matching older messages, newest first.

    Query params, all optional, at least one of ``q`` / ``kind`` / ``since`` /
    ``until`` required (none answers an empty list):
      - ``q``: text, the session search's syntax (at least ``SEARCH_MIN_CHARS``)
      - ``kind``: ``files`` (an attached or sent file), ``images`` (an image),
        ``links`` (a web link), ``videos``, ``pdfs``, ``html`` (a video, PDF or
        HTML file; HTML also a linked saved html artifact the caller may read),
        ``widgets`` (a reply with a widget) or ``pinned`` (a message this chat
        pinned, of the pins the caller may see)
      - ``since`` / ``until``: epoch seconds; ``until`` is exclusive
      - ``before``: resume below this row index (a previous reply's ``next_before``)
      - ``limit``: hits per reply (default ``DEFAULT_HITS``, at most ``MAX_HITS``)

    Returns ``{hits: [{index, role, ts?, mid?, snippet, file_count?, link_count?,
    image_count?, images?, video_count?, video_names?, pdf_count?, pdf_names?,
    html_count?, html_names?, widget_count?, widget_titles?}], next_before, total,
    counts}``. Excerpts, names and titles are redacted.
    """
    found = _slot_or_refusal(request, "chat.slot_history_search")
    if isinstance(found, web.Response):
        return found
    slot, state = found
    filters = _filters(request)
    if isinstance(filters, web.Response):
        return filters
    query, since, until, empty = filters
    kind = request.query.get("kind", "").strip()
    if kind not in KINDS:
        return _bad("invalid_kind", "kind must be one of " + ", ".join(k for k in KINDS if k))
    before = _int(request.query.get("before"))
    limit = _int(request.query.get("limit"))
    if before is _BAD or limit is _BAD:
        return _bad("invalid_query_params", "before and limit must be whole numbers")
    hits_wanted = max(1, min(limit if isinstance(limit, int) else DEFAULT_HITS, MAX_HITS))
    if empty or (query is None and not kind and since is None and until is None):
        return web.json_response({"hits": [], "next_before": None, "total": 0})
    key = slot_history_key(slot)
    floor = await _floor(state, key)
    artifact_kind, pinned = _caller_scope(request, state)
    try:
        body = await asyncio.to_thread(
            _read_settled,
            search_history,
            state.conversation_log,
            key,
            query=query,
            kind=kind,
            since=since,
            until=until,
            before=before if isinstance(before, int) else None,
            limit=hits_wanted,
            floor=floor,
            artifact_kind=artifact_kind,
            pinned=pinned,
        )
    except (OSError, TranscriptRevisionChanged):
        logger.warning("history search failed for %s", key, exc_info=True)
        return web.json_response(
            {"error": "could not read this chat's history", "code": "history_unreadable"},
            status=503,
        )
    return web.json_response(body)


async def api_chat_slot_history_counts(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/history/counts — every tab's count, whole chat.

    Takes ``q``, ``since`` and ``until`` as ``history/search`` reads them, and no
    ``kind``: one reply counts, for every ``kind`` (``""`` for all), the chat's
    messages that match the query and dates with that kind, over its whole
    history (at most ``COUNT_BUDGET_ROWS`` rows, newest first). So the panel's
    counts do not depend on the tab open or on how far its list has paged.

    Returns ``{counts, total, complete}``; ``complete`` is False when the walk
    stopped at the row budget before the chat's first row.
    """
    found = _slot_or_refusal(request, "chat.slot_history_counts")
    if isinstance(found, web.Response):
        return found
    slot, state = found
    filters = _filters(request)
    if isinstance(filters, web.Response):
        return filters
    query, since, until, empty = filters
    if empty:
        return web.json_response({"counts": dict.fromkeys(KINDS, 0), "total": 0, "complete": True})
    key = slot_history_key(slot)
    floor = await _floor(state, key)
    artifact_kind, pinned = _caller_scope(request, state)
    try:
        body = await asyncio.to_thread(
            _read_settled,
            search_history,
            state.conversation_log,
            key,
            query=query,
            kind="",
            since=since,
            until=until,
            before=None,
            limit=0,
            floor=floor,
            budget=COUNT_BUDGET_ROWS,
            # Rows are appended, so an index read stays the row it was while
            # the chat writes; a pinned walk this long would rarely finish in
            # a busy chat. shortcut: a rotation during the walk can skew one
            # reply's counts; the next read is right again.
            pin_revision=False,
            artifact_kind=artifact_kind,
            pinned=pinned,
        )
    except (OSError, TranscriptRevisionChanged):
        logger.warning("history counts failed for %s", key, exc_info=True)
        return web.json_response(
            {"error": "could not read this chat's history", "code": "history_unreadable"},
            status=503,
        )
    return web.json_response(
        {
            "counts": body["counts"],
            "total": body["total"],
            "complete": body["next_before"] is None,
        }
    )


async def api_chat_slot_history_around(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/history/around?start=&end= — rows ``[start, end)``.

    Read only, for the search panel's preview. At most ``MAX_AROUND_ROWS`` rows;
    a wider range is cut from its start. Returns ``{messages, start, end, total,
    has_earlier}``, the messages prepared and redacted as the slot detail read
    sends them; in a bot chat only the messages its chat draws. Optional ``mid``
    re-centres the range on that message, or answers ``404 message_not_found``
    if it is absent from the bounded lookup or behind the display floor.
    """
    found = _slot_or_refusal(request, "chat.slot_history_around")
    if isinstance(found, web.Response):
        return found
    slot, state = found
    start = _int(request.query.get("start"))
    end = _int(request.query.get("end"))
    if not isinstance(start, int) or not isinstance(end, int) or end < start:
        return _bad("invalid_range", "start and end must be whole numbers, start <= end")
    mid = request.query.get("mid")
    if mid is not None and (not mid.strip() or len(mid) > 256 or sanitize_string(mid) != mid):
        return _bad("invalid_mid", "mid must be a nonempty message id of at most 256 characters")
    start = max(start, end - MAX_AROUND_ROWS)
    key = slot_history_key(slot)
    floor = await _floor(state, key)
    try:
        read = await asyncio.to_thread(
            _read_settled,
            read_around,
            state.conversation_log,
            key,
            start=start,
            end=end,
            floor=floor,
            speech_only=is_member_thread(slot),
            mid=mid,
        )
    except (OSError, TranscriptRevisionChanged):
        logger.warning("history preview read failed for %s", key, exc_info=True)
        return web.json_response(
            {"error": "could not read this chat's history", "code": "history_unreadable"},
            status=503,
        )
    if read is None:
        return web.json_response(
            {"error": "message not found; search again", "code": "message_not_found"}, status=404
        )
    messages = _prepare_messages(read["rows"], False, live_child="", workspace=slot.workspace)
    return web.json_response(
        {
            "messages": messages,
            "start": read["start"],
            "end": read["end"],
            "total": read["total"],
            "has_earlier": read["has_earlier"],
        }
    )


__all__ = [
    "KINDS",
    "Pinned",
    "MAX_AROUND_ROWS",
    "SCAN_BUDGET_ROWS",
    "COUNT_BUDGET_ROWS",
    "api_chat_slot_history_around",
    "api_chat_slot_history_counts",
    "api_chat_slot_history_search",
    "read_around",
    "file_record",
    "row_files",
    "row_images",
    "row_videos",
    "search_history",
]
