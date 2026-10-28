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

import json
import logging
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Iterable, Set
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home, kiro_sessions_dir, legacy_home

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

    return CliSessionMeta(
        session_id=sid,
        title=text("title"),
        cwd=text("cwd"),
        created_at=text("created_at"),
        updated_at=text("updated_at"),
        agent_name=agent if isinstance(agent, str) else "",
        parent_session_id=text("parent_session_id"),
    )


def read_envelope_meta(path: Path) -> CliSessionMeta | None:
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
        with open(path, "rb") as f:
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
            raw = f.read()
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
            _META_CACHE[name] = (key, meta)
        # The file's own name is the id the routes accept; an envelope naming a
        # different id is not this file's session.
        if meta is not None and meta.session_id == name[:-5]:
            found.append(meta)
    for stale in set(_META_CACHE) - seen:
        _META_CACHE.pop(stale, None)
    found.sort(key=lambda m: m.updated_at, reverse=True)
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


def _read_continued() -> list[str]:
    """The recorded sids, oldest first; empty when the record is missing or malformed."""
    try:
        raw = json.loads(_continued_path().read_text(encoding="utf-8"))
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
    resolved = realpath(meta.cwd)
    return not any(_under(resolved, root) or _under(meta.cwd, root) for root in crew_roots)


def list_terminal_sessions(
    owned_sids: Set[str],
    *,
    query: str = "",
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """One page of :func:`is_terminal_session` rows, newest first, with the total."""
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
    page = rows[offset : offset + limit]
    return {
        "sessions": [m.to_row() for m in page],
        "total": len(rows),
        "has_more": offset + len(page) < len(rows),
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
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and stamp > 0:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))
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
    so a scrub never meets a value the cut has split. The event log is read a record at a time; a record
    that is not valid UTF-8 JSON (a torn tail, a corrupt line) is skipped and
    counted in ``skipped`` rather than failing the read.
    """
    paths = session_paths(sid)
    if paths is None:
        return None
    json_path, jsonl_path = paths
    meta = read_envelope_meta(json_path)
    if meta is None or meta.session_id != sid:
        return None
    messages: list[dict[str, Any]] = []
    skipped = 0
    if _regular_file(jsonl_path) is not None:
        try:
            with open(jsonl_path, "rb") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line.decode("utf-8"))
                    except ValueError:
                        skipped += 1
                        continue
                    msg = _visible_message(record)
                    if msg is not None:
                        messages.append(msg)
        except OSError:
            return None
    total = len(messages)
    end = total if before is None else max(0, min(before, total))
    limit = max(1, min(limit, PREVIEW_MAX_LIMIT))
    start = max(0, end - limit)
    page = []
    for m in messages[start:end]:
        content = m["content"]
        if sanitize_assistant is not None and m["role"] == "assistant":
            content = sanitize_assistant(content)
        truncated = len(content) > PREVIEW_MAX_CHARS
        page.append({**m, "content": content[:PREVIEW_MAX_CHARS], "truncated": truncated})
    return {
        **meta.to_row(),
        "messages": page,
        "start": start,
        "total": total,
        "skipped": skipped,
        "has_transcript": _regular_file(jsonl_path) is not None,
    }


def _stat_key(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_ino, info.st_mtime_ns, info.st_size


def snapshot_session(sid: str) -> CliSessionSnapshot:
    """Copy *sid*'s pair as it stood at one instant. Raises :class:`SnapshotRefused`.

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
        try:
            envelope_raw = json_path.read_bytes()
            events_raw = jsonl_path.read_bytes()
        except OSError as exc:
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


def _parse_snapshot(sid: str, envelope_raw: bytes, events_raw: bytes) -> CliSessionSnapshot:
    unreadable = SnapshotRefused(
        "kiro_cli_session_unreadable", "this kiro-cli session's files are not valid"
    )
    try:
        envelope = json.loads(envelope_raw.decode("utf-8"))
    except ValueError as exc:
        raise unreadable from exc
    meta = _meta_from_envelope(envelope)
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
    # Split on "\n" only: ``splitlines`` also breaks on U+2028 and friends, which
    # kiro-cli's writer leaves unescaped inside a JSON string.
    for line in events.split("\n"):
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
