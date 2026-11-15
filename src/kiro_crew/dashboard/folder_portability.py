"""Carry the dashboard's chat folders through a whole-install export and import.

Each chat names its folder by ``folder_id`` in its transcript header; the folder
rows themselves live in ``folders.json`` at the data home's root. The export
carries that file as one of its root-level documents. The import adds every
archive folder whose ``id`` this install does not have and leaves every folder it
already has exactly as it is, in both modes -- the add-only rule the chats follow,
so an imported chat finds its folder again and no local folder is changed.

The archive is untrusted, so an imported row is rebuilt from the fields a person
can set when creating a folder, each checked as the create path checks it. What
only the server stamps (an app or crew member owner, a member's home, a channel
brand, the creating session), tags and steering folders are not carried. A row
is also not added when its parent already holds a folder of that name, as a
create with a unique name is refused.

A folder's ``project_dir`` is not carried: it names a directory on the computer
that made the archive, and judging an archive-chosen path here means resolving
it, which can reach a network share or a mount. The folder's chats open in the
default workspace until a project is set on the folder again.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from kiro_crew.atomic_write import atomic_write
from kiro_crew.bounded_read import bounded_read
from kiro_crew.dashboard.chat_folder_order import next_folder_order
from kiro_crew.dashboard.folder_repository import FOLDERS_FILE
from kiro_crew.home_path_rewrite import _open_regular
from kiro_crew.owner_only_files import OWNER_ONLY_FILE_MODE
from kiro_crew.user_json import exceeds_nesting, loads_user_json, strip_utf8_bom
from kiro_crew.validation import is_registered_agent_name

#: The largest ``folders.json`` an import reads, as the import's settings-document cap.
_MAX_FOLDERS_BYTES = 8 * 1024 * 1024

#: The longest folder id kept; the store's own ids are 12 characters.
_MAX_ID_CHARS = 64

#: The longest default agent name read before its name check.
_MAX_TEXT_CHARS = 4096

#: The create path's cap on a folder name.
_MAX_NAME_CHARS = 100

#: How long the import waits for the gateway's folder store to take the rows.
_LIVE_MERGE_TIMEOUT_SECS = 60.0


@dataclass(frozen=True)
class FolderMergeResult:
    """What one add of archive folders did."""

    added: frozenset[str] = frozenset()
    """Ids of the rows added."""
    already_here: int = 0
    """Archive rows whose id this install already has."""
    skipped: int = 0
    """Archive rows not added: a name taken in their parent, an id a local folder
    names as its parent, or the folder cap."""


#: Adds archive rows to the folder list.
FolderMerge = Callable[[list[dict[str, Any]]], FolderMergeResult]


def _refuse_constant(name: str) -> float:
    raise ValueError(f"folders.json holds {name}")


def _read_folders_file(path: Path) -> object:
    """*path* parsed, read without following a swapped-in link and within the cap."""
    with _open_regular(path) as fh:
        raw = bounded_read(fh, _MAX_FOLDERS_BYTES + 1)
    if len(raw) > _MAX_FOLDERS_BYTES:
        raise ValueError("folders.json is too large")
    # NaN and Infinity would fail the folder store's read-back of its own write.
    return json.loads(strip_utf8_bom(raw.decode("utf-8")), parse_constant=_refuse_constant)


def _text(value: object, cap: int) -> str:
    """*value* when it is a string of at most *cap* characters, else ``""``."""
    return value if isinstance(value, str) and len(value) <= cap else ""


def _is_folder_id(value: str) -> bool:
    """Whether *value* uses only the characters a folder route segment takes as is."""
    return bool(value) and all(c.isascii() and (c.isalnum() or c in "-_") for c in value)


def _clean_row(row: object) -> dict[str, Any] | None:
    """The archive row rebuilt from the fields a folder create takes, or ``None``.

    ``None`` for a row with no usable id or name. A new folder is shown, as a
    create makes it; ``project_dir`` is not carried (see the module docstring).
    """
    if not isinstance(row, dict):
        return None
    from kiro_crew.dashboard.chat_folders_icons import _is_single_emoji, _is_valid_folder_color
    from kiro_crew.dashboard.chat_folders_records import MAX_FOLDER_DEFAULT_PROMPT_CHARS
    from kiro_crew.instances.crew_ref import clean_crew_ref

    folder_id = _text(row.get("id"), _MAX_ID_CHARS)
    name = row.get("name")
    name = name.strip()[:_MAX_NAME_CHARS] if isinstance(name, str) else ""
    if not _is_folder_id(folder_id) or not name:
        return None
    agent = _text(row.get("default_agent"), _MAX_TEXT_CHARS).strip()
    clean: dict[str, Any] = {
        "id": folder_id,
        "name": name,
        "collapsed": row.get("collapsed") is True,
        "hidden": False,
        "parent_id": _text(row.get("parent_id"), _MAX_ID_CHARS),
        "default_agent": agent if is_registered_agent_name(agent) else "",
        "default_destination_crew": clean_crew_ref(row.get("default_destination_crew")),
    }
    # Stamped as the store stamps a create: the import is when it arrived here.
    clean["created_at"] = time.time()
    color = _text(row.get("color"), _MAX_NAME_CHARS).strip().lower()
    if color and _is_valid_folder_color(color):
        clean["color"] = color
    icon = _text(row.get("icon"), _MAX_NAME_CHARS).strip()
    if icon and _is_single_emoji(icon):
        clean["icon"] = icon
    prompt = _text(row.get("default_prompt"), MAX_FOLDER_DEFAULT_PROMPT_CHARS)
    if prompt.strip():
        clean["default_prompt"] = prompt
    return clean


def _break_parent_cycles(rows: list[dict[str, Any]]) -> None:
    """Move to the top level each row whose parent chain leads back to itself.

    A row above a loop keeps its parent; the loop is cut at the row met first.
    Each row is walked once, so this is linear in the row count.
    """
    by_id = {row["id"]: row for row in rows}
    done: set[str] = set()
    for row in rows:
        path: list[str] = []
        on_path: set[str] = set()
        cursor = row["id"]
        while cursor in by_id and cursor not in done:
            if cursor in on_path:
                by_id[cursor]["parent_id"] = ""
                break
            path.append(cursor)
            on_path.add(cursor)
            cursor = by_id[cursor]["parent_id"]
        done.update(path)


def read_archive_folders(staged: Path) -> tuple[list[dict[str, Any]], int] | None:
    """The staged archive's usable folder rows, cleaned, and how many it left out.

    ``None`` when the archive carries no ``folders.json``. Left out are the rows
    with no usable id or name and the usable rows past the folder cap; a repeat
    of an id is the same folder and is not counted. The first row of each id
    wins. Raises ``ValueError`` when the file is present
    but not a readable list of folders, ``OSError`` when it cannot be opened.
    """
    path = staged / FOLDERS_FILE
    if path.is_symlink() or not path.is_file():
        return None
    try:
        raw = _read_folders_file(path)
    except (UnicodeDecodeError, RecursionError) as exc:
        raise ValueError("folders.json is not valid JSON") from exc
    if not isinstance(raw, list) or exceeds_nesting(raw):
        raise ValueError("folders.json is not a list of folders")
    from kiro_crew.dashboard.chat_folders_records import MAX_CHAT_FOLDERS

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    omitted = 0
    for entry in raw:
        row = _clean_row(entry)
        if row is None:
            omitted += 1
        elif row["id"] not in seen:
            seen.add(row["id"])
            # shortcut: rows past the cap are left out even if earlier ones are
            # already here; upgrade if a real archive holds more than the cap.
            if len(rows) < MAX_CHAT_FOLDERS:
                rows.append(row)
            else:
                omitted += 1
    _break_parent_cycles(rows)
    return rows, omitted


def add_missing_folders(
    current: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> tuple[bool, FolderMergeResult]:
    """Append to *current* each *incoming* row whose ``id`` it lacks.

    A row whose parent is neither here nor added goes to the top level, and so does
    one whose parent is a crew member's home, which takes folders from that member
    only. A row is not added when its parent already holds a folder of that name
    (trimmed, case-folded, as the create path compares), or past the folder cap.
    Returns ``(changed, result)`` in the shape ``DashboardState.mutate_folders``
    takes from its callback.
    """
    from kiro_crew.dashboard.chat_folders_records import MAX_CHAT_FOLDERS
    from kiro_crew.dashboard.mate_folders import refuses_write_into

    local = [row for row in current if isinstance(row, dict)]
    have = {row.get("id") for row in local}
    # A local folder whose parent id names no folder here would move under an
    # archive folder of that id, so such an id is not added.
    claimed = {row.get("parent_id") for row in local} - have
    blocked = have | claimed
    pending = [dict(row) for row in incoming if row["id"] not in blocked]
    already_here = sum(1 for row in incoming if row["id"] in have)
    pending = pending[: max(0, MAX_CHAT_FOLDERS - len(local))]
    locked: dict[str, bool] = {}

    def refused(parent: str) -> bool:
        if parent not in locked:
            locked[parent] = refuses_write_into(current, parent, "")
        return locked[parent]

    def names_under(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
        return {
            (str(row.get("parent_id") or ""), row["name"].strip().casefold())
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        }

    local_names = names_under(current)
    # Dropping a row can orphan its children, which then move up and are judged
    # again; each pass drops at least one row or ends, so this is bounded.
    while True:
        ids = have | {row["id"] for row in pending}
        for row in pending:
            parent = row["parent_id"]
            if parent and (parent not in ids or (parent in have and refused(parent))):
                row["parent_id"] = ""
        taken = set(local_names)
        kept = []
        for row in pending:
            key = (row["parent_id"], row["name"].strip().casefold())
            if key not in taken:
                taken.add(key)
                kept.append(row)
        if len(kept) == len(pending):
            break
        pending = kept
    order = next_folder_order(local)
    for offset, row in enumerate(pending):
        # After the local folders, in the archive's order.
        row["order"] = order + offset
        current.append(row)
    result = FolderMergeResult(
        added=frozenset(row["id"] for row in pending),
        already_here=already_here,
        skipped=len(incoming) - already_here - len(pending),
    )
    return bool(pending), result


def merge_folders_on_disk(mc: Path) -> FolderMerge:
    """A :data:`FolderMerge` that writes ``folders.json`` directly.

    For an import with no gateway in the process; with one running, its own folder
    store must take the rows, or its in-memory copy writes them away again.

    shortcut: no cross-process lock; the one caller with a gateway passes
    :func:`live_folder_merge`. Upgrade if a CLI import is added.
    """

    def merge(incoming: list[dict[str, Any]]) -> FolderMergeResult:
        path = mc / FOLDERS_FILE
        current: list[dict[str, Any]] = []
        if path.exists():
            raw = loads_user_json(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("this install's folders.json is not a list")
            current = raw
        changed, result = add_missing_folders(current, incoming)
        if changed:
            atomic_write(path, json.dumps(current, indent=2), mode=OWNER_ONLY_FILE_MODE)
        return result

    return merge


def live_folder_merge(state: Any, loop: asyncio.AbstractEventLoop) -> FolderMerge | None:
    """A :data:`FolderMerge` through the running gateway's folder store, or ``None``.

    Built on the event loop; called from the import's worker thread, it runs the add
    as one ``mutate_folders`` transaction on *loop* and waits for it.
    """
    mutate = getattr(state, "mutate_folders", None)
    if mutate is None:
        return None

    def merge(incoming: list[dict[str, Any]]) -> FolderMergeResult:
        future = asyncio.run_coroutine_threadsafe(
            mutate(lambda current: add_missing_folders(current, incoming)), loop
        )
        try:
            return cast("FolderMergeResult", future.result(timeout=_LIVE_MERGE_TIMEOUT_SECS))
        except BaseException:
            # shortcut: a write already landing when this is cancelled is kept by the
            # store, so the summary can call folders skipped that did arrive; a
            # re-import adds nothing twice. Upgrade if a slow store is reported.
            future.cancel()
            raise

    return merge


def _refuse(summary: dict, why: str) -> None:
    summary["items"].append(f"folders (skipped: {why})")
    summary.setdefault("refused_merges", []).append("folders")


def import_folders(staged: Path, summary: dict, merge: FolderMerge) -> None:
    """Add the archive's folders through *merge* and record the result in *summary*."""
    try:
        read = read_archive_folders(staged)
    except OSError:
        _refuse(summary, "unreadable")
        return
    except ValueError:
        _refuse(summary, "not a list of folders")
        return
    if read is None:
        return
    rows, omitted = read
    result = FolderMergeResult()
    if rows:
        try:
            result = merge(rows)
        except Exception as exc:  # the store's own write or lock failure
            _refuse(summary, type(exc).__name__)
            return
        summary["items"].append(
            f"folders (merged: {len(result.added)} added, {result.already_here} already here)"
        )
    if not_added := result.skipped + omitted:
        # Said out loud: the dashboard warns about every item named here.
        _refuse(summary, f"{not_added} not added")
