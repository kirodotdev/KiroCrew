"""The kiro-cli sessions a person started in a terminal, read for Kiro Crew's History.

kiro-cli keeps every conversation it runs at ``<kiro home>/sessions/cli/<sid>.json``
(the envelope: id, ``cwd``, timestamps, ``title`` and the resumable
``session_state``) plus ``<sid>.jsonl`` (the event log). Kiro Crew's own sessions,
sub-agents, crons and consolidation runs land in the SAME directory, and every
envelope says ``session_created_reason: "subagent"`` whoever started it, so that
field cannot tell a terminal session apart. :func:`is_terminal_session` is the one
rule that does, from facts the files and the session map carry.

This module only READS kiro-cli's files. Nothing here writes, moves or deletes
them, so the person can still ``kiro-cli chat --resume-id <sid>`` a session after
continuing it in Crew: "Continue in Crew" copies the pair into a FRESH sid through
the session-transfer installer (:mod:`kiro_crew.dashboard.session_transfer`).

Every function that takes a session id first checks it against
:data:`SESSION_ID_RE` (the UUID shape kiro-cli mints) and resolves it to a direct
child of :func:`kiro_cli_sessions_dir`, so no request text ever names a path.

**Blocking IO throughout**: callers on the event loop run these in a thread.
"""

from __future__ import annotations

import errno
import json
import logging
import math
import os
import re
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Set
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home, kiro_sessions_dir, legacy_home
from kiro_crew.jsonl_util import bounded_raw_records, open_regular_nofollow

logger = logging.getLogger("kiro_crew.kiro_cli_sessions")

#: The session ids kiro-cli mints: a lower-case UUID. Anything else is refused
#: before it can reach the filesystem.
SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

#: Directory names that only ever hold a Kiro Crew data home or workspace: the
#: legacy home, a dev gateway's home, the pod homes root and the workspaces root.
#: Matched as whole path segments, so another gateway on the same machine (a dev
#: checkout, a pod) is filtered even when this process cannot name its root.
CREW_DIR_SEGMENTS: frozenset[str] = frozenset(
    {".kirocrew", ".kirocrew-dev", ".kirocrew-pods", "kirocrew-workspace"}
)

#: The first prompt of every Crew-driven turn starts with one of these blocks
#: (``context_assembly``, ``chat_persistence``), and kiro-cli titles a session
#: from its first prompt. A person typing in a terminal does not write them.
CREW_PROMPT_PREFIXES: tuple[str, ...] = (
    "[AGENT SYSTEM PROMPT]",
    "[SESSION CONTEXT",
    "[Previous chat history for this tab",
    "[sent by session",
)

#: The agent spec name prefix Crew generates for its managed skill views.
CREW_AGENT_PREFIX = "kirocrew-skill-view-"

#: How much of an envelope the list reads. The identity fields come first and the
#: (large) ``session_state`` after them, so the head is enough on every file this
#: was measured on; a file whose head does not hold them is parsed whole, up to
#: :data:`_FULL_PARSE_MAX_BYTES`.
_HEAD_BYTES = 16 * 1024
_FULL_PARSE_MAX_BYTES = 32 * 1024 * 1024
_STATE_KEY_RE = re.compile(rb'\s*,\s*"session_state"\s*:')
_TAIL_BYTES = 4 * 1024

#: The first instant :func:`time.gmtime` cannot render as a four-digit year
#: (10000-01-01). A message timestamp at or past it, or one that is not finite,
#: is treated as absent: kiro-cli never writes one, and a planted one must not
#: fail the preview or the copy.
_MAX_TIMESTAMP = 253_402_300_800
_AGENT_RE = re.compile(rb'"agent_name"\s*:\s*"((?:[^"\\]|\\.)*)"')

#: Preview bounds: messages per page, and characters per message.
PREVIEW_DEFAULT_LIMIT = 100
PREVIEW_MAX_LIMIT = 500
PREVIEW_MAX_CHARS = 20_000

#: How often a snapshot re-reads a pair that changed while it was being read.
_SNAPSHOT_ATTEMPTS = 5
_SNAPSHOT_RETRY_SECS = 0.2


@dataclass(frozen=True)
class CliSessionMeta:
    """The listable facts of one kiro-cli session envelope."""

    session_id: str
    title: str
    cwd: str
    created_at: str
    updated_at: str
    agent_name: str = ""
    parent_session_id: str = ""

    def to_row(self) -> dict[str, str]:
        return {
            "id": self.session_id,
            "title": self.title,
            "cwd": self.cwd,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class CliSessionSnapshot:
    """A consistent copy of one session's pair, taken while neither file changed.

    *envelope* is the parsed ``<sid>.json``; *events* is the ``<sid>.jsonl`` text
    byte for byte, minus a trailing record a live writer had not finished;
    *messages* is the visible conversation extracted from those same events.
    """

    meta: CliSessionMeta
    envelope: dict[str, Any]
    events: str
    messages: list[dict[str, str]]


class SnapshotRefused(Exception):
    """A session that cannot be copied; ``code`` is the wire code, the text says why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def kiro_cli_sessions_dir() -> Path:
    """kiro-cli's session store; the one directory this module reads."""
    return kiro_sessions_dir()


def valid_session_id(sid: object) -> bool:
    return isinstance(sid, str) and bool(SESSION_ID_RE.match(sid))


def session_paths(sid: str) -> tuple[Path, Path] | None:
    """``(<sid>.json, <sid>.jsonl)`` for a well-formed *sid*, else ``None``.

    The id is checked against :data:`SESSION_ID_RE` first, so it cannot hold a
    separator or a ``..``; the resolved parent is compared as well, so the answer
    is a direct child of the sessions dir whatever the id contains.
    """
    if not valid_session_id(sid):
        return None
    base = kiro_cli_sessions_dir()
    json_path = base / f"{sid}.json"
    if json_path.parent != base:
        return None
    return json_path, base / f"{sid}.jsonl"


def _regular_file(path: Path) -> os.stat_result | None:
    """``lstat`` of *path* when it is a regular file, not a link; else ``None``."""
    try:
        info = path.lstat()
    except OSError:
        return None
    return info if stat.S_ISREG(info.st_mode) else None


#: The read ceiling for a file whose reads are each bounded by the caller
#: (the envelope's head, tail and capped fallback; the preview's per-record
#: frames): the open still refuses a FIFO or a link, and the caller's own
#: sizes bound the memory.
_UNCAPPED = sys.maxsize - 1  # the opener reads one past this, which must fit an index


# --------------------------------------------------------------------------- #
# Envelope reads
# --------------------------------------------------------------------------- #


def _meta_from_envelope(doc: Any) -> CliSessionMeta | None:
    if not isinstance(doc, dict):
        return None
    sid = doc.get("session_id")
    if not isinstance(sid, str) or not valid_session_id(sid):
        return None
    state = doc.get("session_state")
    agent = state.get("agent_name") if isinstance(state, dict) else None

    def text(key: str) -> str:
        value = doc.get(key)
        return value.strip() if isinstance(value, str) else ""

    cwd = doc.get("cwd")
    return CliSessionMeta(
        session_id=sid,
        title=text("title"),
        # Verbatim: a path is never trimmed. "/projects/widget " is its own
        # folder, and trimming it would make a sibling the chat's project.
        cwd=cwd if isinstance(cwd, str) else "",
        created_at=text("created_at"),
        updated_at=text("updated_at"),
        agent_name=agent if isinstance(agent, str) else "",
        parent_session_id=text("parent_session_id"),
    )


#: Ingress bounds for every string a :class:`CliSessionMeta` retains. kiro-cli's
#: sessions dir is writable by the agent's shell, and these values are held in
#: :data:`_META_CACHE` and served in every list response, so each is capped once,
#: here, before anything keeps it. Display text is cut; the working directory is
#: never cut (a cut path names another folder), so a longer one is refused.
_MAX_TITLE_CHARS = 300
_MAX_AGENT_CHARS = 128
#: ISO-8601 stamps and kiro-cli ids are far shorter; anything longer is cut.
_MAX_STAMP_CHARS = 64
_MAX_PARENT_ID_CHARS = 64
#: PATH_MAX on Linux; no folder a person ran in is longer.
_MAX_CWD_CHARS = 4096


def _cut_whole_words(text: str, limit: int) -> str:
    """*text* cut to at most *limit* characters without keeping part of a word.

    The redactors that run on these values when they are served recognise a
    credential only whole (a bare 40-character key is matched as a 40-run), so a
    cut through the middle of one would leave a fragment none of them matches.
    A word the limit falls inside is therefore dropped entirely, and an ellipsis
    marks the cut; a value with no break before the limit becomes the ellipsis.
    """
    if len(text) <= limit:
        return text
    kept = text[: limit - 1]
    if not text[limit - 1].isspace():
        # The cut lands inside a word: back off to the last break before it.
        breaks = [i for i, ch in enumerate(kept) if ch.isspace()]
        kept = kept[: breaks[-1]] if breaks else ""
    return kept.rstrip() + "\u2026"


def _bounded(meta: CliSessionMeta | None, source: str) -> CliSessionMeta | None:
    """*meta* with every retained string within its ingress bound, or ``None``
    when its working directory cannot be a folder (an embedded NUL, which path
    calls reject with ``ValueError``, or longer than :data:`_MAX_CWD_CHARS`).

    The one pass every envelope goes through before it is cached, listed or
    copied. The session id is already a validated UUID. Cuts and the refusal are
    logged once for *source*, with the field names only, never their values.
    """
    if meta is None:
        return None
    refused = "\x00" in meta.cwd or len(meta.cwd) > _MAX_CWD_CHARS
    limits = {
        "title": _MAX_TITLE_CHARS,
        "agent_name": _MAX_AGENT_CHARS,
        "created_at": _MAX_STAMP_CHARS,
        "updated_at": _MAX_STAMP_CHARS,
        "parent_session_id": _MAX_PARENT_ID_CHARS,
    }
    cut = (
        {}
        if refused
        else {
            name: _cut_whole_words(getattr(meta, name), limit)
            for name, limit in limits.items()
            if len(getattr(meta, name)) > limit
        }
    )
    if not refused and not cut:
        return meta
    logger.info(
        "kiro-cli envelope %s: %s",
        source,
        (
            "refused (working directory cannot be a folder)"
            if refused
            else "cut " + ", ".join(sorted(cut))
        ),
    )
    return None if refused else replace(meta, **cut)


def read_envelope_meta(path: Path) -> CliSessionMeta | None:
    """The identity fields of the envelope at *path*, bounded at ingress
    (:func:`_bounded`), or ``None`` when unreadable or not a session."""
    return _bounded(_read_envelope_meta_raw(path), path.name)


def _read_envelope_meta_raw(path: Path) -> CliSessionMeta | None:
    """The identity fields of the envelope at *path*, or ``None`` when unreadable.

    Reads only the head when it holds every identity field (``session_state``
    follows them in kiro-cli's own writer), so a list of thousands of sessions
    does not parse megabytes of context each. A head that does not end where
    expected -- another key order, a partial write -- falls back to a whole
    parse, and a malformed or non-UTF-8 file answers ``None`` rather than raising.
    """
    info = _regular_file(path)
    if info is None:
        return None
    try:
        # Through the shared no-follow opener: kiro-cli's sessions dir is
        # writable by the agent's shell, so the name may be a planted FIFO or
        # link, which the open refuses on every platform.
        with open_regular_nofollow(path, max_bytes=_UNCAPPED) as f:
            head = f.read(_HEAD_BYTES)
            match = _STATE_KEY_RE.search(head)
            if match is not None:
                try:
                    meta = _meta_from_envelope(json.loads(head[: match.start()] + b"}"))
                except ValueError:
                    meta = None
                if meta is not None:
                    tailed = _with_tail_agent(f, info.st_size, meta)
                    if tailed is not None:
                        return tailed
            if info.st_size > _FULL_PARSE_MAX_BYTES:
                return None
            f.seek(0)
            # Capped like the size check above: a file that grew since its
            # lstat is never read past the cap.
            raw = f.read(_FULL_PARSE_MAX_BYTES + 1)
        if len(raw) > _FULL_PARSE_MAX_BYTES:
            return None
        return _meta_from_envelope(json.loads(raw))
    except (OSError, ValueError):
        return None


def _with_tail_agent(f: Any, size: int, meta: CliSessionMeta) -> CliSessionMeta | None:
    """*meta* with the ``session_state.agent_name`` read from the file's tail.

    The agent name is one of the state's last keys, after the conversation, so
    the head read cannot see it; the tail holds it. The value is only ever
    compared with :data:`CREW_AGENT_PREFIX`, so a tail that does not hold it
    simply leaves the name empty. ``None`` when the tail names a
    ``parent_session_id`` -- a top-level key written AFTER the state, which only
    a whole parse reads reliably.
    """
    f.seek(max(0, size - _TAIL_BYTES))
    tail = f.read(_TAIL_BYTES)
    if b'"parent_session_id"' in tail:
        return None
    found = _AGENT_RE.findall(tail)
    if not found:
        return meta
    try:
        agent = json.loads(b'"' + found[-1] + b'"')
    except ValueError:
        return meta
    return replace(meta, agent_name=agent) if isinstance(agent, str) else meta


#: ``name -> ((mtime_ns, size), meta)``: the list re-reads only envelopes that
#: changed. Bounded by the directory's own size; entries for vanished files are
#: dropped on every scan.
_META_CACHE: dict[str, tuple[tuple[int, int], CliSessionMeta | None]] = {}
#: The most envelopes :data:`_META_CACHE` holds. Each entry is bounded by
#: :func:`_bounded`; past this many a scan still reads every envelope, it just
#: stops remembering new ones, so a flood of planted files costs time, not memory.
_META_CACHE_MAX = 20_000


def scan_sessions(base: Path | None = None) -> list[CliSessionMeta]:
    """Every readable envelope in the sessions dir, newest first. Never raises."""
    base = base or kiro_cli_sessions_dir()
    found: list[CliSessionMeta] = []
    seen: set[str] = set()
    try:
        entries = list(os.scandir(base))
    except OSError:
        return []
    for entry in entries:
        name = entry.name
        if not name.endswith(".json") or not valid_session_id(name[:-5]):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        seen.add(name)
        key = (info.st_mtime_ns, info.st_size)
        cached = _META_CACHE.get(name)
        if cached is not None and cached[0] == key:
            meta = cached[1]
        else:
            meta = read_envelope_meta(Path(entry.path))
            if name in _META_CACHE or len(_META_CACHE) < _META_CACHE_MAX:
                _META_CACHE[name] = (key, meta)
        # The file's own name is the id the routes accept; an envelope naming a
        # different id is not this file's session.
        if meta is not None and meta.session_id == name[:-5]:
            found.append(meta)
    for stale in set(_META_CACHE) - seen:
        _META_CACHE.pop(stale, None)
    # Newest first, the id breaking a tie, so the order is total and a page
    # can resume after any row (:func:`list_terminal_sessions`).
    found.sort(key=_order_key, reverse=True)
    return found


# --------------------------------------------------------------------------- #
# The filter
# --------------------------------------------------------------------------- #


#: The copies "Continue in Crew" made, by sid, kept in the data home. A copy is
#: Crew's session, but its first prompt is the person's own terminal prompt, so it
#: is titled like a terminal session; the session map identifies it only while
#: the chat stays joined to it. This record identifies it after a reset or a
#: failed start has dropped the join, so the copy never comes back to the list as
#: a second "terminal" session.
CONTINUED_FILE = "kiro-cli-continued.json"
#: Oldest entries go first past this many; a copy that old has long been rotated.
_CONTINUED_MAX = 5000
_continued_lock = threading.Lock()


def _continued_path() -> Path:
    return data_home() / CONTINUED_FILE


#: The record's ceiling: :data:`_CONTINUED_MAX` sids of 36 characters with
#: their JSON punctuation fit well inside it.
_CONTINUED_MAX_BYTES = 1024 * 1024


def _read_continued() -> list[str]:
    """The recorded sids, oldest first; empty when the record is missing,
    malformed, or not a regular file. Read through the shared no-follow opener:
    the data home is writable by the agent's shell, so a FIFO or link planted
    at the record's name is refused instead of blocking the list for good."""
    try:
        with open_regular_nofollow(_continued_path(), max_bytes=_CONTINUED_MAX_BYTES) as f:
            raw = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        return []
    sids = raw.get("sids") if isinstance(raw, dict) else None
    return [s for s in sids if valid_session_id(s)] if isinstance(sids, list) else []


def continued_sids() -> frozenset[str]:
    """Every sid :func:`record_continued` recorded. **Blocking**; never raises."""
    return frozenset(_read_continued())


def record_continued(sid: str) -> None:
    """Remember *sid* as a copy Crew made. **Blocking**; a failure is logged, not raised."""
    if not valid_session_id(sid):
        return
    with _continued_lock:
        ordered = [s for s in _read_continued() if s != sid] + [sid]
        try:
            atomic_write(_continued_path(), json.dumps({"sids": ordered[-_CONTINUED_MAX:]}))
        except OSError:
            logger.warning("kiro_cli_sessions: could not record continued sid", exc_info=True)


def crew_working_roots() -> tuple[str, ...]:
    """The directories Kiro Crew runs its own sessions in, realpath-normalised.

    The data home (dashboard chats, sub-agents and consolidation run under it),
    the default and legacy homes, the workspaces root (``_bg``, ``_consolidate``,
    ``cron_*`` and per-channel directories) and the pod homes root. Each is read
    best-effort: a root that cannot be resolved is left out, and
    :data:`CREW_DIR_SEGMENTS` still covers its default name.
    """
    from kiro_crew.config.loader_locations import workspace_root

    roots: list[Path] = []
    for resolve in (
        data_home,
        lambda: Path.home() / ".kiro" / "crew",
        legacy_home,
        lambda: workspace_root(create=False),
    ):
        try:
            roots.append(resolve())
        except Exception:
            logger.debug("kiro_cli_sessions: could not resolve a Crew root", exc_info=True)
    pod_root = os.environ.get("KIROCREW_POD_ROOT")
    if pod_root:
        roots.append(Path(pod_root))
    return tuple(dict.fromkeys(os.path.realpath(str(r)) for r in roots))


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def is_terminal_session(
    meta: CliSessionMeta,
    *,
    owned_sids: Set[str],
    crew_roots: Iterable[str],
    realpath: Any = os.path.realpath,
) -> bool:
    """Whether *meta* is a session a person started in a terminal: the History list rule.

    Refused, in order:

    * a session Kiro Crew owns -- *owned_sids*: the session map's joins and the
      copies Continue in Crew made;
    * a sub-agent of another session (``parent_session_id``);
    * an untitled session (an empty title, or kiro-cli's ``(no title)``);
    * one titled from a Crew-written first prompt (:data:`CREW_PROMPT_PREFIXES`)
      or run under a Crew-generated agent (:data:`CREW_AGENT_PREFIX`);
    * one whose working directory is inside a Crew root (*crew_roots*) or passes
      through a Crew-only directory name (:data:`CREW_DIR_SEGMENTS`, and the
      ``.kiro/crew`` pair): that is where Crew's background, cron, consolidation
      and pod runs live, on this gateway or another one on the machine.
    """
    if meta.session_id in owned_sids or meta.parent_session_id:
        return False
    title = meta.title
    if not title or title == "(no title)":
        return False
    if title.startswith(CREW_PROMPT_PREFIXES) or meta.agent_name.startswith(CREW_AGENT_PREFIX):
        return False
    if not meta.cwd:
        return True
    parts = Path(meta.cwd).parts
    if CREW_DIR_SEGMENTS.intersection(parts):
        return False
    if any(a == ".kiro" and b == "crew" for a, b in zip(parts, parts[1:])):
        return False
    try:
        resolved = realpath(meta.cwd)
    except (OSError, ValueError):
        # A path no filesystem call accepts is not a folder anyone ran in.
        return False
    return not any(_under(resolved, root) or _under(meta.cwd, root) for root in crew_roots)


def _order_key(meta: CliSessionMeta) -> tuple[str, str]:
    """Where *meta* sorts in the list: by when it was last updated, then by id."""
    return meta.updated_at, meta.session_id


def list_terminal_sessions(
    owned_sids: Set[str],
    *,
    query: str = "",
    after: tuple[str, str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """One page of :func:`is_terminal_session` rows, newest first, with the total.

    *after* is the ``(updated_at, id)`` of the last row already shown: the page
    holds the rows that sort after it. A cursor rather than an offset, because a
    terminal keeps writing while someone pages: a session that moves to the top
    in between would shift every row down one place, and an offset would then
    serve the last row shown a second time.
    """
    roots = crew_working_roots()
    resolved: dict[str, str] = {}

    def cached_realpath(path: str) -> str:
        hit = resolved.get(path)
        if hit is None:
            hit = resolved[path] = os.path.realpath(path)
        return hit

    rows = [
        m
        for m in scan_sessions()
        if is_terminal_session(m, owned_sids=owned_sids, crew_roots=roots, realpath=cached_realpath)
    ]
    needle = query.strip().lower()
    if needle:
        rows = [m for m in rows if needle in m.title.lower() or needle in m.cwd.lower()]
    total = len(rows)
    if after is not None:
        rows = [m for m in rows if _order_key(m) < after]
    page = rows[:limit]
    return {
        "sessions": [m.to_row() for m in page],
        "total": total,
        "has_more": len(rows) > len(page),
    }


# --------------------------------------------------------------------------- #
# The conversation
# --------------------------------------------------------------------------- #


def _visible_message(record: Any) -> dict[str, str] | None:
    """The user or assistant text one event carries, or ``None`` for anything else.

    A ``Prompt`` is the person's turn and an ``AssistantMessage`` the model's;
    only their ``text`` parts are shown. Thinking, tool calls, tool results and
    compaction summaries are context, not conversation.
    """
    if not isinstance(record, dict):
        return None
    kind = record.get("kind")
    role = {"Prompt": "user", "AssistantMessage": "assistant"}.get(
        kind if isinstance(kind, str) else ""
    )
    data = record.get("data")
    if role is None or not isinstance(data, dict):
        return None
    content = data.get("content")
    if not isinstance(content, list):
        return None
    text = "\n\n".join(
        part["data"]
        for part in content
        if isinstance(part, dict)
        and part.get("kind") == "text"
        and isinstance(part.get("data"), str)
    ).strip()
    if not text:
        return None
    ts = ""
    meta = data.get("meta")
    stamp = meta.get("timestamp") if isinstance(meta, dict) else None
    if (
        isinstance(stamp, (int, float))
        and not isinstance(stamp, bool)
        # The range first: it holds for an int of any size and is False for
        # NaN and both infinities, so an integer past float range never reaches
        # the float conversion math.isfinite makes (OverflowError).
        and 0 < stamp < _MAX_TIMESTAMP
        and math.isfinite(stamp)
    ):
        try:
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))
        except (OverflowError, ValueError, OSError):
            ts = ""
    return {"role": role, "content": text, "ts": ts}


def read_preview(
    sid: str,
    *,
    before: int | None = None,
    limit: int = PREVIEW_DEFAULT_LIMIT,
    sanitize_assistant: Callable[[str], str] | None = None,
) -> dict[str, Any] | None:
    """A read-only page of *sid*'s visible conversation, or ``None`` when absent.

    Pages back from the newest message: ``before`` is the index (exclusive) the
    page ends at, defaulting to the end, and the answer's ``start`` is what the
    next older page passes as ``before``. *sanitize_assistant* runs over each
    assistant message on the page before it is cut to :data:`PREVIEW_MAX_CHARS`,
    so a scrub never meets a value the cut has split. The event log is read a
    record at a time; a record that is not valid UTF-8 JSON (a torn tail, a
    corrupt line) is skipped and counted in ``skipped`` rather than failing the
    read.

    Memory is bounded by the page, not the conversation: a first pass counts
    the visible messages and keeps none of their text, and a second keeps only
    the page's messages, each scrubbed and cut as it is read. A log that changes
    between the passes is a terminal still appending, which only adds messages
    after the page, so the page's indexes still hold.
    """
    paths = session_paths(sid)
    if paths is None:
        return None
    json_path, jsonl_path = paths
    meta = read_envelope_meta(json_path)
    if meta is None or meta.session_id != sid:
        return None
    has_transcript = _regular_file(jsonl_path) is not None
    total = skipped = 0
    if has_transcript:
        try:
            for msg in _visible_records(jsonl_path):
                if msg is None:
                    skipped += 1
                else:
                    total += 1
        except OSError:
            return None
    end = total if before is None else max(0, min(before, total))
    limit = max(1, min(limit, PREVIEW_MAX_LIMIT))
    start = max(0, end - limit)
    page: list[dict[str, Any]] = []
    if end > start:
        try:
            index = 0
            for msg in _visible_records(jsonl_path):
                if msg is None:
                    continue
                if index >= end:
                    break
                if index >= start:
                    page.append(_page_entry(msg, sanitize_assistant))
                index += 1
        except OSError:
            return None
    return {
        **meta.to_row(),
        "messages": page,
        "start": start,
        "total": total,
        "skipped": skipped,
        "has_transcript": has_transcript,
    }


def _visible_records(jsonl_path: Path) -> Iterator[dict[str, str] | None]:
    """Each record of the event log at *jsonl_path* as its visible message,
    nothing for a record that shows none, and ``None`` for one that is not
    UTF-8 JSON. **Blocking IO.** Raises ``OSError``.

    Bounded: the terminal's log is not Crew's to trust, and a record past the
    cap is skipped rather than held whole.
    """
    with open_regular_nofollow(jsonl_path, max_bytes=_UNCAPPED) as f:
        for line in bounded_raw_records(f, jsonl_path, label="kiro-cli preview"):
            if not line.strip():
                continue
            try:
                record = json.loads(line.decode("utf-8"))
            except ValueError:
                yield None
                continue
            msg = _visible_message(record)
            if msg is not None:
                yield msg


def _page_entry(
    msg: dict[str, str], sanitize_assistant: Callable[[str], str] | None
) -> dict[str, Any]:
    """*msg* as a preview row: scrubbed when it is the assistant's, then cut."""
    content = msg["content"]
    if sanitize_assistant is not None and msg["role"] == "assistant":
        content = sanitize_assistant(content)
    truncated = len(content) > PREVIEW_MAX_CHARS
    return {**msg, "content": content[:PREVIEW_MAX_CHARS], "truncated": truncated}


def _stat_key(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_ino, info.st_mtime_ns, info.st_size


def snapshot_session(sid: str, max_bytes: int | None = None) -> CliSessionSnapshot:
    """Copy *sid*'s pair as it stood at one instant. Raises :class:`SnapshotRefused`.

    *max_bytes* is the pair size the caller reserved memory for: a pair that has
    grown past it since is a terminal still writing, refused as busy rather than
    read into memory nobody reserved.

    A terminal may still be writing the session. The pair is read and both files
    are stat'ed before and after; a read during which either changed is thrown
    away and taken again, up to :data:`_SNAPSHOT_ATTEMPTS` times, so the envelope
    and the event log describe the same moment. A last record without its newline
    is a write still in progress and is left out, so no torn line is ever copied.
    Every other record must be UTF-8 JSON: the copy is resumed by ``session/load``,
    which a corrupt log would fail, so a corrupt one is refused rather than copied.

    The files are only read; nothing here writes to the sessions dir.
    """
    paths = session_paths(sid)
    if paths is None:
        raise SnapshotRefused("kiro_cli_session_invalid_id", "not a kiro-cli session id")
    json_path, jsonl_path = paths
    for attempt in range(_SNAPSHOT_ATTEMPTS):
        before = (_regular_file(json_path), _regular_file(jsonl_path))
        if before[0] is None:
            raise SnapshotRefused("kiro_cli_session_not_found", "this kiro-cli session is gone")
        if before[1] is None:
            raise SnapshotRefused(
                "kiro_cli_session_no_transcript",
                "this kiro-cli session has no conversation file to continue from",
            )
        if max_bytes is not None and before[0].st_size + before[1].st_size > max_bytes:
            raise SnapshotRefused(
                "kiro_cli_session_busy",
                "this kiro-cli session is still being written; try again once the terminal is idle",
            )
        # Each file is read only up to the size just measured, which the check
        # above held within *max_bytes*: the shared opener refuses a planted
        # FIFO or link and charges every read against that size, so one that
        # grew or was swapped since (EFBIG) is read again or refused as busy,
        # never taken whole.
        try:
            with open_regular_nofollow(json_path, max_bytes=before[0].st_size) as f:
                envelope_raw = f.read()
            with open_regular_nofollow(jsonl_path, max_bytes=before[1].st_size) as f:
                events_raw = f.read()
        except OSError as exc:
            if exc.errno == errno.EFBIG:
                if attempt + 1 < _SNAPSHOT_ATTEMPTS:
                    time.sleep(_SNAPSHOT_RETRY_SECS)
                continue
            raise SnapshotRefused(
                "kiro_cli_session_unreadable", "this kiro-cli session could not be read"
            ) from exc
        after = (_regular_file(json_path), _regular_file(jsonl_path))
        if all(
            a is not None and b is not None and _stat_key(a) == _stat_key(b)
            for a, b in zip(before, after)
        ):
            break
        if attempt + 1 < _SNAPSHOT_ATTEMPTS:
            time.sleep(_SNAPSHOT_RETRY_SECS)
    else:
        raise SnapshotRefused(
            "kiro_cli_session_busy",
            "this kiro-cli session is still being written; try again once the terminal is idle",
        )
    return _parse_snapshot(sid, envelope_raw, events_raw)


def _lines(text: str) -> Iterator[str]:
    """*text*'s lines, one at a time, so the log is never held twice as a list.

    Broken on "\\n" only: ``splitlines`` also breaks on U+2028 and friends, which
    kiro-cli's writer leaves unescaped inside a JSON string.
    """
    start = 0
    while start < len(text):
        end = text.find("\n", start)
        if end < 0:
            end = len(text)
        yield text[start:end]
        start = end + 1


def _parse_snapshot(sid: str, envelope_raw: bytes, events_raw: bytes) -> CliSessionSnapshot:
    unreadable = SnapshotRefused(
        "kiro_cli_session_unreadable", "this kiro-cli session's files are not valid"
    )
    try:
        envelope = json.loads(envelope_raw.decode("utf-8"))
    except ValueError as exc:
        raise unreadable from exc
    meta = _bounded(_meta_from_envelope(envelope), f"{sid}.json")
    if (
        meta is None
        or meta.session_id != sid
        or not isinstance(envelope.get("session_state"), dict)
    ):
        raise unreadable
    if events_raw and not events_raw.endswith(b"\n"):
        tail_start = events_raw.rfind(b"\n") + 1
        try:
            json.loads(events_raw[tail_start:].decode("utf-8"))
        except ValueError:
            # The writer's newline has not landed: an unfinished record.
            events_raw = events_raw[:tail_start]
    try:
        events = events_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise unreadable from exc
    messages: list[dict[str, str]] = []
    for line in _lines(events):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise unreadable from exc
        msg = _visible_message(record)
        if msg is not None:
            messages.append(msg)
    if not messages:
        raise SnapshotRefused(
            "kiro_cli_session_empty", "this kiro-cli session has no conversation to continue"
        )
    return CliSessionSnapshot(meta=meta, envelope=envelope, events=events, messages=messages)
