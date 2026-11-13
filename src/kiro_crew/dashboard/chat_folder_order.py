"""The ``order`` a newly created chat folder is given.

Every writer that appends a row to the chat folder store (the folder API, a
mate's home, a channel's folder, an arrival's ``Imported`` pair, Code Review
Sage's follow-up folder) takes its ``order`` from :func:`next_folder_order`, so
a new folder sorts after every folder already in the store.
"""

from __future__ import annotations

from typing import Any


def next_folder_order(folders: list[dict[str, Any]]) -> int:
    """One past the highest stored ``order`` (0 for an empty store).

    The store length is not that position: deletes leave gaps, so after two of
    four folders are removed the length equals a survivor's ``order`` and the
    new folder ties it, sorting between survivors by name. Each row's ``order``
    is read with the sidebar's own reader, so a missing or non-numeric value
    counts as 0 here exactly as it sorts there.
    """
    # Through the facade, the one import path its owners are reached by, and at
    # call time so loading a folder writer does not load the MCP tool table.
    from kiro_crew.mcp_dashboard import _chat_folder_order

    return max((_chat_folder_order(f) for f in folders), default=-1) + 1
