# SPDX-License-Identifier: Apache-2.0
"""Whether a transcript holds a message the PERSON sent.

Stricter than ``ConversationLog.has_messages``, which counts any row past the
metadata line. A row the person sent is a ``user`` row that is neither a
``session_send`` relay (``projection_workers._RELAY_PREFIX``) nor automation by
the rule the session summary reads (``session_summary.is_injected_text``), so
the roster and the summary agree on whether a person spoke. Gateway-authored
turns mostly write no ``user`` row: a welcome kickoff is hidden, a cron result
is ``inject`` and an auto-nudge is ``nudge``.

The live file is read under the transcript's lock, then the archived segments a
rotation moved older rows into, so a thread whose person-sent rows were rotated
out still answers yes. An absent file holds none. A file that cannot be read
raises ``OSError``; a record that cannot be read intact, or is not JSON, counts
as one, so unverifiable history is still history.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from kiro_crew.crew_log.projection_workers import _RELAY_PREFIX
from kiro_crew.history_files import ARCHIVE_DIR_NAME, ARCHIVE_SEGMENT_DELIMITER
from kiro_crew.jsonl_util import UnreadableRecord, strict_records
from kiro_crew.session_summary import is_injected_text


def is_person_sent_row(row: object) -> bool:
    """Whether one transcript row is a message the person sent."""
    if not isinstance(row, dict) or row.get("role") != "user":
        return False
    content = row.get("content")
    if not isinstance(content, str):
        return True
    return not (content.lstrip().startswith(_RELAY_PREFIX) or is_injected_text(content))


def _file_has_person_row(path: Path) -> bool:
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        return False
    with handle:
        try:
            for record in strict_records(handle, path):
                line = record.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    return True
                if is_person_sent_row(data):
                    return True
        except UnreadableRecord:
            return True
    return False


def transcript_has_person_message(conversation_log: Any, key: str) -> bool:
    """Whether *key*'s transcript in *conversation_log* holds a person-sent row.

    File IO that may read every file of the thread, so callers run it off the
    event loop and for as few threads as they need.
    """
    path = Path(conversation_log._path(key))
    with conversation_log._locked(key):
        if _file_has_person_row(path):
            return True
    archive = path.parent / ARCHIVE_DIR_NAME
    prefix = f"{path.stem}{ARCHIVE_SEGMENT_DELIMITER}"
    try:
        with os.scandir(archive) as entries:
            segments = sorted(
                e.name for e in entries if e.name.startswith(prefix) and e.name.endswith(".jsonl")
            )
    except FileNotFoundError:
        return False
    return any(_file_has_person_row(archive / name) for name in segments)
