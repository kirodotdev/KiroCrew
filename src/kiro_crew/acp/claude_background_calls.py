"""A Claude turn that ended while background agents' tool calls were still running.

claude-agent-acp answers ``session/prompt`` with ``-32603`` and
``data.errorKind: "incomplete_tool_call"`` when a turn reaches ``end_turn`` while
tool calls it counts as the turn's own have no result. A background or Workflow
agent's call that reaches the adapter's ``canUseTool`` before the agent's task is
registered is sent on the root session with no ``parentToolUseId`` and counted as
one of them, although its result goes to the agent's own transcript and never to
the turn. The error then names calls the turn never made, after its answer was
delivered whole; read as a plain error, it would show a finished turn as failed and
interrupted.

Claude Code keeps each subagent's transcript under the session's own directory,
``<config dir>/projects/<project>/<sid>/subagents/``. ``_prompt_loop`` asks
:func:`settle_background_only` about every prompt error on the claude backend: it
reads the ids out of the adapter's sentence and answers an ``end_turn`` completion
when every one of them is a ``tool_use`` in one of those transcripts. An id found
nowhere there, the parent's own unfinished call included, keeps the error. The read
is off the event loop and bounded: the newest transcripts first, the tail of each.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import string
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from kiro_crew.acp.types import STOP_REASON_END_TURN, JsonRpcMessage
from kiro_crew.agent_sdk.claude_transcripts import claude_session_dir

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

__all__ = [
    "background_agent_tool_calls",
    "incomplete_tool_call_ids",
    "settle_background_only",
]

logger = logging.getLogger(__name__)

_JSONRPC_INTERNAL_ERROR = -32603
#: The ``data.errorKind`` the adapter builds for this error.
_INCOMPLETE_TOOL_CALL_KIND = "incomplete_tool_call"
#: The adapter's sentence, read whole: the optional prefix
#: ``RequestError.internalError`` adds, then the lead-in, then the ids it names,
#: each ``toolu_`` plus ASCII letters, digits and underscores, joined by ``", "``.
_INTERNAL_ERROR_PREFIX = "Internal error: "
_INCOMPLETE_TOOL_CALL_LEAD = "Claude ended the turn without returning results for tool calls: "
_TOOL_USE_ID_PREFIX = "toolu_"
_TOOL_USE_ID_CHARS = frozenset(string.ascii_letters + string.digits + "_")
_ID_SEPARATOR = ", "
#: The most ids one error is read for. More keeps the error.
_MAX_IDS = 64
#: How many ``.jsonl`` files of a session's subagents are listed before the newest
#: are chosen, and how many of those are read.
_MAX_LISTED = 1024
_MAX_TRANSCRIPTS = 64
#: How much of the end of each transcript is read. A call the error names was asked
#: for during the turn that just ended, so it sits near the end of its agent's file.
_TRANSCRIPT_TAIL_BYTES = 1 << 20


def incomplete_tool_call_ids(error: object) -> tuple[str, ...]:
    """The tool call ids an ``incomplete_tool_call`` prompt error names, or ``()``.

    Structural, as the adapter builds it: code ``-32603`` and a ``data`` mapping whose
    ``errorKind`` is the kind. The sentence alone, or a ``data`` string, never
    matches. The ids are read from the adapter's own sentence, matched whole.
    """
    if not isinstance(error, dict) or error.get("code") != _JSONRPC_INTERNAL_ERROR:
        return ()
    data = error.get("data")
    if not isinstance(data, dict) or data.get("errorKind") != _INCOMPLETE_TOOL_CALL_KIND:
        return ()
    message = error.get("message")
    if not isinstance(message, str):
        return ()
    sentence = message.strip().removeprefix(_INTERNAL_ERROR_PREFIX)
    if not sentence.startswith(_INCOMPLETE_TOOL_CALL_LEAD):
        return ()
    listed = sentence[len(_INCOMPLETE_TOOL_CALL_LEAD) :].split(_ID_SEPARATOR)
    if not all(_is_tool_use_id(call_id) for call_id in listed):
        return ()
    ids = tuple(dict.fromkeys(listed))
    return ids if len(ids) <= _MAX_IDS else ()


def _is_tool_use_id(value: str) -> bool:
    """Whether *value* is ``toolu_`` followed by one or more ``_TOOL_USE_ID_CHARS``."""
    if not value.startswith(_TOOL_USE_ID_PREFIX):
        return False
    rest = value[len(_TOOL_USE_ID_PREFIX) :]
    return bool(rest) and _TOOL_USE_ID_CHARS.issuperset(rest)


def _newest_transcripts(directory: Path) -> list[str]:
    """Up to ``_MAX_TRANSCRIPTS`` regular ``.jsonl`` files under *directory*, newest first."""
    listed: list[tuple[float, str]] = []
    for dirpath, _dirnames, filenames in os.walk(directory):
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                listed.append((info.st_mtime, path))
            if len(listed) >= _MAX_LISTED:
                break
        if len(listed) >= _MAX_LISTED:
            break
    listed.sort(reverse=True)
    return [path for _mtime, path in listed[:_MAX_TRANSCRIPTS]]


def _tail_lines(path: str) -> list[bytes]:
    """The whole lines in the last ``_TRANSCRIPT_TAIL_BYTES`` of *path*, or ``[]``.

    Opened non-blocking and checked to be a regular file, so nothing at the path can
    hold the read.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return []
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return []
        start = max(0, info.st_size - _TRANSCRIPT_TAIL_BYTES)
        os.lseek(fd, start, os.SEEK_SET)
        chunks: list[bytes] = []
        left = _TRANSCRIPT_TAIL_BYTES
        while left > 0:
            chunk = os.read(fd, left)
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
    except OSError:
        return []
    finally:
        os.close(fd)
    lines = b"".join(chunks).split(b"\n")
    # A cut tail starts inside a line.
    return lines[1:] if start else lines


def _tool_use_ids(row: Any) -> Iterable[str]:
    """The ``tool_use`` ids of one transcript row's message content."""
    message = row.get("message") if isinstance(row, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ()
    return [
        block["id"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and isinstance(block.get("id"), str)
    ]


def background_agent_tool_calls(session_id: str, cwd: str, ids: Iterable[str]) -> frozenset[str]:
    """The ids among *ids* that a subagent's transcript of the session holds as its ``tool_use``.

    Blocking file reads: call it off the event loop. A session directory that cannot
    be spelled, or no subagent transcript, finds nothing.
    """
    wanted = set(ids)
    session_dir = claude_session_dir(session_id, cwd)
    if not wanted or session_dir is None:
        return frozenset()
    found: set[str] = set()
    for path in _newest_transcripts(session_dir / "subagents"):
        for line in _tail_lines(path):
            if not any(tid.encode() in line for tid in wanted - found):
                continue
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                continue
            found.update(wanted.intersection(_tool_use_ids(row)))
        if found == wanted:
            break
    return frozenset(found)


async def settle_background_only(client: AcpClient, msg: JsonRpcMessage) -> JsonRpcMessage | None:
    """An ``end_turn`` answer for an error that names only background agents' calls, or ``None``.

    ``None`` keeps the error: it is not ``incomplete_tool_call``, or any id it names
    is not found in a subagent's transcript of this session.
    """
    ids = incomplete_tool_call_ids(msg.error)
    if not ids:
        return None
    session_id = client._session_id or ""
    cwd = str(getattr(client, "_work_dir", "") or "")
    found = await asyncio.to_thread(background_agent_tool_calls, session_id, cwd, ids)
    if len(found) != len(ids):
        return None
    logger.info(
        "claude session %s ended its turn with %d background agent tool call(s) still "
        "running (%s); the turn is complete",
        session_id,
        len(ids),
        ", ".join(ids),
    )
    return JsonRpcMessage(id=msg.id, result={"stopReason": STOP_REASON_END_TURN})
