"""Prune ``session/new`` stdio elements whose command cannot be found, before the harness sees them.

Two harnesses handle such an element badly, in opposite directions, and neither
says so to the user:

* goose drops the element and creates the session anyway
  (``test/fixtures/acp_frames/goose/mcp-stdio-dropped-live.jsonl``), so the session
  looks healthy while the server's tools are missing;
* deepseek fails the WHOLE ``session/new``
  (``test/fixtures/acp_frames/deepseek/mcp-stdio-rollback-live.jsonl``), so one bad
  element costs every other server and the session itself.

Crew can answer the most common cause -- a command that does not exist -- before the
request is sent: leave the element out, and say which server it was. The claim is
deliberately narrow. Only a command that is CERTAINLY absent is pruned: an absolute
path that names no executable, or a bare name not found on the MCP search path
(:func:`kiro_crew.env.mcp_search_path` over the element's own ``PATH``, the path the
dashboard probe and the gateway rewriter resolve the same command against). A relative
path with a directory part is resolved by the harness against a directory this module
does not know, so it is kept.
A command that exists but exits at once is not caught here either; it reaches the
harness exactly as before.

Pure apart from filesystem stats, and non-raising: an element this module cannot
judge is kept, because keeping it is the behaviour every caller already had.
"""

from __future__ import annotations

import os
import shutil
from typing import Any, Mapping, NamedTuple, Sequence

from kiro_crew.env import mcp_search_path


class PrunedServer(NamedTuple):
    """One element left out of a session's MCP array, and why."""

    name: str
    reason: str


def _element_path(element: Mapping[str, Any]) -> str:
    """The ``PATH`` the element's own ``env`` pairs set, or ``""``."""
    env = element.get("env")
    if not isinstance(env, list):
        return ""
    for pair in env:
        # Case-insensitive: a Windows-authored spec says ``Path``.
        if isinstance(pair, Mapping) and str(pair.get("name") or "").upper() == "PATH":
            value = pair.get("value")
            return value if isinstance(value, str) else ""
    return ""


def unstartable_reason(element: Any) -> str:
    """Why *element*'s command certainly cannot start, or ``""`` when it may.

    ``""`` for anything that is not a stdio element with a string ``command``: a
    remote (``url``) element has no command to find, and a malformed one is the
    translator's business, not this check's.
    """
    if not isinstance(element, Mapping) or element.get("url"):
        return ""
    command = element.get("command")
    if not isinstance(command, str) or not command:
        return ""
    if os.path.isabs(command):
        # ``which`` checks the one location an absolute command names, applying
        # ``PATHEXT`` on Windows, so a command written without ``.exe`` is found.
        if shutil.which(command):
            return ""
        return f"command not found: {command}"
    if os.sep in command or (os.altsep and os.altsep in command):
        return ""
    if shutil.which(command, path=mcp_search_path(_element_path(element))):
        return ""
    return f"command not found on PATH: {command}"


def prune_unstartable(
    servers: Sequence[Any],
) -> tuple[list[Any], list[PrunedServer]]:
    """Split *servers* into the elements to send and the ones left out.

    Order is kept for both halves. Never raises: an element whose check fails is
    kept, as it would have been without this module.
    """
    kept: list[Any] = []
    pruned: list[PrunedServer] = []
    for element in servers:
        try:
            reason = unstartable_reason(element)
        except Exception:
            reason = ""
        if reason:
            pruned.append(PrunedServer(str(element.get("name") or ""), reason))
        else:
            kept.append(element)
    return kept, pruned
