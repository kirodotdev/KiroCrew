"""``GET /api/project/session-files``: the files one chat session edited.

The side panel's Files tab leads with this list. It is derived on the server
from the session's own transcript -- the ``file_changes`` rows the turn runner
attaches to assistant messages -- plus the change snapshots of the turn still
running, so it survives a page reload and reads the same from any browser.

Nothing here admits a path to another route. The list names files; opening
one goes through the file-read route and its own gate, and the tree keeps its
project-only boundary.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew.dashboard.chat_utils_slots import effective_session_key, slot_history_key
from kiro_crew.dashboard.slot_ownership import deny_app_slot_session_access, slot_not_found
from kiro_crew.security import is_sensitive_path
from kiro_crew.session_environment import session_runs_remotely

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

#: Files one response lists, newest first. The tab shows a short list; a
#: session that edited more says so through ``truncated``; an edit the list
#: cannot name for another reason, through ``withheld``.
MAX_SESSION_FILES = 200

#: Longest path kept, in characters. A longer one is dropped, never shortened:
#: a cut path names a different file.
_MAX_PATH_CHARS = 4096

#: Longest row timestamp served. An ISO-8601 stamp with microseconds and an
#: offset is 32 characters.
_MAX_TS_CHARS = 40


def _served_ts(ts: object) -> str:
    """*ts* when it is an ISO-8601 timestamp, else ``""``. Pure.

    A transcript line is writable by the agent's own shell, so a row's ``ts``
    can hold any text; only a stamp leaves this route, never a string the
    redactor would have to judge.
    """
    if not isinstance(ts, str) or not ts or len(ts) > _MAX_TS_CHARS:
        return ""
    try:
        datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)
    except ValueError:
        return ""
    return ts


def _sel() -> Any:
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg.sel()


def _change_paths(rows: Iterable[Mapping[str, Any]]) -> Iterable[tuple[str, str]]:
    """``(path, ts)`` for every change an assistant row names, in row order. Pure.

    Only assistant rows count: the runner attaches file changes to those alone,
    so metadata on another role is not the agent's work. A write whose outcome
    could not be proven (``omitted_reason: unverified_write``) is skipped, as
    the Git tab's transcript seed skips it.
    """
    for row in rows:
        if not isinstance(row, Mapping) or row.get("role") != "assistant":
            continue
        meta = row.get("meta")
        changes = meta.get("file_changes") if isinstance(meta, Mapping) else None
        if not isinstance(changes, list):
            continue
        ts = row.get("ts")
        for change in changes:
            if not isinstance(change, Mapping):
                continue
            if change.get("omitted_reason") == "unverified_write":
                continue
            path = change.get("path")
            if isinstance(path, str) and path:
                yield path, ts if isinstance(ts, str) else ""


def rows_omit_files(rows: Iterable[Mapping[str, Any]]) -> bool:
    """Whether an assistant row records changed files its turn left out. Pure.

    The turn runner caps one turn's ``file_changes`` and counts the rest in
    ``meta.file_changes_omitted_files``; those files are edits this list cannot
    name, so the response says it is incomplete.
    """
    for row in rows:
        if not isinstance(row, Mapping) or row.get("role") != "assistant":
            continue
        meta = row.get("meta")
        omitted = meta.get("file_changes_omitted_files") if isinstance(meta, Mapping) else None
        if isinstance(omitted, int) and not isinstance(omitted, bool) and omitted > 0:
            return True
    return False


def newest_first(
    persisted: Iterable[tuple[str, str]], pending: Iterable[str], now: str
) -> list[tuple[str, str]]:
    """Distinct paths, the most recently changed first. Pure.

    *persisted* is in transcript order (oldest first); *pending* are the paths
    of the running turn, which are newer than anything persisted and stamped
    *now*. A path changed twice is listed once, at its latest change.
    """
    latest: dict[str, str] = {}
    for path, ts in persisted:
        latest.pop(path, None)
        latest[path] = ts
    for path in pending:
        latest.pop(path, None)
        latest[path] = now
    return list(reversed(latest.items()))


def _resolve(path: str, project: str) -> str | None:
    """The absolute form of a recorded *path*, or None when it has none."""
    if len(path) > _MAX_PATH_CHARS or "\0" in path:
        return None
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    if not project:
        return None
    return os.path.normpath(os.path.join(os.path.expanduser(project), expanded))


def _describe(entries: list[tuple[str, str]], project: str) -> tuple[list[dict], bool, bool]:
    """``(rows, capped, withheld)`` for *entries*. Worker thread only: it stats paths.

    *capped*: more files than :data:`MAX_SESSION_FILES`, the oldest left out.
    *withheld*: a path left out for what it is -- a sensitive path, one the
    credential redactor would mask (the masked string opens nothing, so its
    row could only fail), or one with no absolute form. The two are reported
    apart because the tab explains them differently.
    """
    # Through the handlers facade, which binds the redactor's own helpers.
    from kiro_crew.dashboard.handlers.files import _redact_project_path

    rows: list[dict] = []
    capped = withheld = False
    seen: set[str] = set()
    for raw, ts in entries:
        # The turn runner redacts a credential-shaped path before it is
        # recorded; the placeholder opens nothing, so it is withheld, not
        # shown as a file that is gone.
        if "[REDACTED" in raw:
            withheld = True
            continue
        path = _resolve(raw, project)
        if path is None or path in seen:
            withheld = withheld or path is None
            continue
        seen.add(path)
        if is_sensitive_path(path) or _redact_project_path(path) != path:
            withheld = True
            continue
        if len(rows) >= MAX_SESSION_FILES:
            capped = True
            break
        rows.append({"path": path, "ts": _served_ts(ts), "exists": os.path.lexists(path)})
    return rows, capped, withheld


def _read_rows(clog: Any, key: str, *, whole: bool = False) -> list[Mapping[str, Any]]:
    """Every file-change row of the tab's transcript chain, oldest first.

    Per chain member, in chain order: the rows size-rotation archived out of
    that member's file, then its live rows. Rotation keeps only the newest
    rows in the live file, and an edit in the archived head is still this
    chat's. Only ``rotate`` segments are read (``read_rotated_messages``),
    never content a rewind or compaction discarded. Each member's live rows
    are read before its archive: a rotation landing between the two reads can
    then only list a row twice, which ``newest_first`` folds, never drop it.

    A read that fails raises: an unreadable transcript is not an empty one,
    and the route answers it as an error the tab can retry.

    The list reads the cached path-only projection; *whole* reads the full live
    rows instead, which carry each change's ``before``/``after`` texts.
    """
    chained = getattr(clog, "chained_keys", None)
    keys = (chained(key) if callable(chained) else None) or [key]
    rotated = getattr(clog, "read_rotated_messages", None)
    rows: list[Mapping[str, Any]] = []
    for each in keys:
        live = clog.read_messages(each) if whole else clog.read_file_change_messages(each)
        if callable(rotated):
            rows.extend(rotated(each))
        rows.extend(live)
    return rows


def latest_change(
    rows: Iterable[Mapping[str, Any]], target: str, project: str
) -> Mapping[str, Any] | None:
    """The newest recorded change to the file *target* names, or None. Pure.

    *target* is an absolute, normalized path; a row's recorded path is resolved
    against *project* the way the list resolves it, so the two name one file.
    A write that could not be proven is skipped, as the list skips it.
    """
    found: Mapping[str, Any] | None = None
    for row in rows:
        if not isinstance(row, Mapping) or row.get("role") != "assistant":
            continue
        meta = row.get("meta")
        changes = meta.get("file_changes") if isinstance(meta, Mapping) else None
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, Mapping):
                continue
            if change.get("omitted_reason") == "unverified_write":
                continue
            path = change.get("path")
            if isinstance(path, str) and path and _resolve(path, project) == target:
                found = change
    return found


def change_pair(change: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(before, after)`` when *change* holds its whole texts, else None. Pure.

    A snapshot the turn cut (``truncated``) or left without content
    (``content_omitted``: an older turn, the turn budget) would diff wrong
    lines, so it is no pair at all. Nor is one whose before is not known to be
    the whole file: ``before_fragment`` is True for the replaced span alone,
    which against the whole after reads as nearly every line added, and absent
    on an entry written before the turn runner recorded it, which is either.
    """
    if change.get("truncated") or change.get("content_omitted"):
        return None
    if change.get("before_fragment") is not False:
        return None
    before, after = change.get("before"), change.get("after")
    if not isinstance(before, str) or not isinstance(after, str) or not (before or after):
        return None
    return before, after


def _pending_paths(slot: Any) -> list[str]:
    """Paths the running turn has snapshotted so far. Event loop only.

    A snapshot whose write failed or was refused is marked ``unverified_write``
    and persists as ``omitted_reason: unverified_write``, which the transcript
    read skips; it is skipped here too, so a failed write never shows as edited.
    """
    held = getattr(slot, "_file_changes", None)
    if not isinstance(held, list):
        return []
    return [
        c["path"]
        for c in held
        if isinstance(c, dict) and isinstance(c.get("path"), str) and not c.get("unverified_write")
    ]


def _gated_slot(
    request: web.Request, state: DashboardState, key: str, operation: str
) -> tuple[Any, web.Response | None]:
    """The slot *key* names, or the refusal: no key, an app token's foreign slot, none."""
    if not key:
        return None, web.json_response(
            {"error": "slot required", "code": "slot_required"}, status=400
        )
    slot = state.get_slot(key)
    denied = deny_app_slot_session_access(request.get("app", ""), slot, key, operation)
    if denied is not None:
        return None, denied
    if slot is None:
        return None, slot_not_found()
    return slot, None


def _not_edited() -> web.Response:
    return web.json_response(
        {"error": "This chat has no recorded change to that file", "code": "not_edited"},
        status=404,
    )


async def api_project_session_file_diff(request: web.Request) -> web.Response:
    """GET /api/project/session-file-diff?slot=...&path=... -- one edit's texts.

    ``{path, available, before, after}`` for the newest change this session's
    transcript records to the absolute *path*: the texts the turn runner already
    redacted and stored with that change, never a disk read. ``available`` is
    false, with both texts empty, when that change holds no whole pair (a cut or
    dropped snapshot) or the running turn is still changing the file; the tab
    then shows the working-tree diff instead. A path this session did not edit,
    or one the list withholds, answers 404 ``not_edited``.
    """
    state: DashboardState = request.app["state"]
    caller = request.get("user", "dashboard")
    key = request.query.get("slot", "").strip()
    slot, refusal = _gated_slot(request, state, key, "project_session_file_diff")
    if refusal is not None:
        return refusal
    raw = request.query.get("path", "")
    # Absolute as sent: the list hands out absolute paths, and expanding a
    # `~user` here would be an account lookup on the event loop.
    target = (
        os.path.normpath(raw)
        if raw and os.path.isabs(raw) and len(raw) <= _MAX_PATH_CHARS and "\0" not in raw
        else None
    )
    if target is None:
        return web.json_response(
            {"error": "absolute path required", "code": "path_required"}, status=400
        )
    from kiro_crew.dashboard.chat_utils_display import _redact_for_display
    from kiro_crew.dashboard.handlers.files import _redact_project_path

    def denied(reason: str) -> web.Response:
        _sel().log_api_access(
            caller=caller,
            operation="project_session_file_diff",
            outcome="denied",
            resources=f"slot={key}",
            error=reason,
        )
        return _not_edited()

    def withheld() -> bool:
        # Worker thread only: the sensitive-path check can resolve the path,
        # which blocks on a stalled mount.
        return is_sensitive_path(target) or _redact_project_path(target) != target

    # What the list withholds is not named here either.
    if "[REDACTED" in raw or await asyncio.to_thread(withheld):
        return denied("withheld_path")
    if await asyncio.to_thread(session_runs_remotely, effective_session_key(slot)):
        return denied("remote_session")
    project = slot.project or ""
    unavailable = {"path": target, "available": False, "before": "", "after": ""}
    # Read on the loop, where the running turn writes it; compared in the
    # worker, since resolving a recorded path can expand a `~user`.
    pending = _pending_paths(slot)
    clog = getattr(state, "conversation_log", None)
    history_key = slot_history_key(slot)

    def work() -> tuple[bool, Mapping[str, Any] | None, tuple[str, str] | None]:
        # The running turn's snapshot is still being rebuilt; its stored
        # pair, if any, is older than the file now.
        if any(_resolve(p, project) == target for p in pending):
            return True, None, None
        rows = _read_rows(clog, history_key, whole=True) if clog is not None else []
        change = latest_change(rows, target, project)
        pair = change_pair(change) if change is not None else None
        if pair is not None:
            # Redacted again as served, like every stored text the chat view
            # serves: the turn runner redacted it at capture, but a transcript
            # line is writable by the agent's own shell after that.
            pair = (_redact_for_display(pair[0]), _redact_for_display(pair[1]))
        return False, change, pair

    try:
        still_changing, change, pair = await asyncio.to_thread(work)
    except (OSError, ValueError) as exc:
        _sel().log_api_access(
            caller=caller,
            operation="project_session_file_diff",
            outcome="error",
            resources=f"slot={key}",
            error=type(exc).__name__,
        )
        return web.json_response(
            {"error": "Transcript could not be read", "code": "transcript_unreadable"},
            status=503,
        )
    if still_changing:
        return web.json_response(unavailable)
    _sel().log_api_access(
        caller=caller,
        operation="project_session_file_diff",
        outcome="allowed" if change is not None else "denied",
        resources=f"slot={key}",
    )
    if change is None:
        return _not_edited()
    if pair is None:
        return web.json_response(unavailable)
    return web.json_response(
        {"path": target, "available": True, "before": pair[0], "after": pair[1]}
    )


async def api_project_session_files(request: web.Request) -> web.Response:
    """GET /api/project/session-files?slot=... -- files this session edited.

    ``{files: [{path, ts, exists}], truncated, withheld}``: absolute paths, newest
    change first, at most :data:`MAX_SESSION_FILES`. ``ts`` is the transcript
    row's timestamp (the current time for the running turn); ``exists`` is
    false for a file deleted since. A relative recorded path is resolved
    against the slot's project directory and dropped without one.
    ``truncated``: the list hit its cap and the oldest files are left out.
    ``withheld``: some edits cannot be named here (a sensitive, masked or
    unresolvable path, or files a turn's own record left out). A transcript
    that cannot be read answers 503 ``transcript_unreadable``.
    """
    state: DashboardState = request.app["state"]
    caller = request.get("user", "dashboard")
    key = request.query.get("slot", "").strip()
    slot, refusal = _gated_slot(request, state, key, "project_session_files")
    if refusal is not None:
        return refusal
    project = slot.project or ""
    # A remote session edits files in its own workspace, not on this gateway.
    if await asyncio.to_thread(session_runs_remotely, effective_session_key(slot)):
        return web.json_response({"files": [], "truncated": False, "withheld": False})
    clog = getattr(state, "conversation_log", None)
    pending = _pending_paths(slot)
    history_key = slot_history_key(slot)

    def work() -> tuple[list[dict], bool, bool]:
        from datetime import timezone

        rows = _read_rows(clog, history_key) if clog is not None else []
        now = datetime.now(timezone.utc).isoformat()
        files, capped, withheld = _describe(
            newest_first(_change_paths(rows), pending, now), project
        )
        # A turn's own record leaving files out is not a cap of this list: those
        # files are edits it cannot name, like a withheld path.
        return files, capped, withheld or rows_omit_files(rows)

    try:
        files, truncated, withheld = await asyncio.to_thread(work)
    except (OSError, ValueError) as exc:
        _sel().log_api_access(
            caller=caller,
            operation="project_session_files",
            outcome="error",
            resources=f"slot={key}",
            error=type(exc).__name__,
        )
        return web.json_response(
            {"error": "Transcript could not be read", "code": "transcript_unreadable"},
            status=503,
        )
    _sel().log_api_access(
        caller=caller,
        operation="project_session_files",
        outcome="allowed",
        resources=f"slot={key}",
    )
    return web.json_response({"files": files, "truncated": truncated, "withheld": withheld})
