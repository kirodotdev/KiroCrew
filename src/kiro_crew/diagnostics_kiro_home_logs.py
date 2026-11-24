"""kiro-cli 3.x's per-process protocol logs, ``<kiro home>/logs/<stamp>-<pid>/``.

kiro-cli 3.x writes each process's logs to a folder of its own under the Kiro home
(``<stamp>-<pid>``; earlier 3.x builds name it ``<stamp>`` alone, and any name is
read), which none of the shared ``kiro-log`` folders ``diagnostics._kiro_log_dirs``
lists reaches, so a host can hold an empty ``kiro-log`` copy of ``mcp.log`` while the
populated one sits here. :func:`protocol_logs` adds these to
``diagnostics._kiro_cli_extra_logs``, the one source list behind both the
``kiro_cli_logs`` tool and the bundle, so every guard that list's readers apply
covers them unchanged. Only ``mcp.log`` is taken; nothing else a folder holds
(``kiro.log``, ``powers.log``) is ever returned. Every folder's log has the same name,
so :func:`folder_of` gives each one its folder's name to be labelled by. Its lines are
JSON events, and :func:`json_event_timestamp` reads the timestamp that makes one a log
record.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.config.paths import kiro_home

#: How many per-process folders are read, newest first: the last few processes
#: are the ones a failure report is about, and the tool's response budget is
#: divided among its sources.
MAX_FOLDERS = 3


def _root() -> Path:
    return kiro_home() / "logs"


def protocol_logs() -> list[Path]:
    """The non-empty ``mcp.log`` of the :data:`MAX_FOLDERS` newest folders.

    A folder counts only when it holds one, and the newest is the one whose
    ``mcp.log`` was written last. The root and each folder must pass
    ``diagnostics._usable_dir`` and each log ``diagnostics._usable_log``, so a
    linked (symlink or junction) root, folder or log is never followed.
    """
    # Imported here: diagnostics imports this module, and the guards stay one definition.
    from kiro_crew.diagnostics import _usable_dir, _usable_log

    root = _root()
    try:
        # Inside the try: on Python 3.12 `is_dir()` raises for an unsearchable Kiro home.
        if not _usable_dir(root):
            return []
        children = list(root.iterdir())
    except OSError:
        return []
    found: list[tuple[float, str, Path]] = []
    for child in children:
        try:
            log = child / "mcp.log"
            if _usable_dir(child) and _usable_log(log) and log.stat().st_size > 0:
                found.append((log.stat().st_mtime, child.name, log))
        except OSError:
            continue
    found.sort(reverse=True)
    return [log for _, _, log in found[:MAX_FOLDERS]]


def json_event_timestamp(line: str) -> str | None:
    """The ``timestamp`` of a line that is one whole kiro-cli 3.x JSON event; else None.

    3.x's ``mcp.log`` writes one JSON object per line, ``{"timestamp": ...}`` first, so
    such a line starts a record exactly as a date-led text line does. It counts only
    when ``{`` is the line's first character (an indented object is nested in a
    frame) and the WHOLE line parses as an object whose ``timestamp`` is a string
    ``diagnostics._RECORD_START`` matches: a pretty-printed frame's ``{`` or
    ``"key": {`` line does not parse alone, and a JSON-RPC envelope has no such field.
    """
    from kiro_crew.diagnostics import _RECORD_START

    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except (ValueError, RecursionError):
        # RecursionError: a planted line of deep nesting must read as a continuation.
        return None
    stamp = event.get("timestamp") if isinstance(event, dict) else None
    return stamp if isinstance(stamp, str) and _RECORD_START.match(stamp) else None


def folder_of(log: Path) -> str | None:
    """The folder name of a log :func:`protocol_logs` returned; None for any other log."""
    return log.parent.name if log.parent.parent == _root() else None
