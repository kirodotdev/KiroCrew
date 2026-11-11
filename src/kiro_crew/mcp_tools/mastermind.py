"""Mastermind's memory tools: open a line of the view, or date a message.

Listed while Mastermind is on (:func:`advertised`), answered for the Mastermind
session alone: the route checks the strictly verified caller, and stays the
enforcement point. The tools hold no state: each call is one loopback read of
the gateway's tree.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kiro_crew import mcp_core


def schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": "mastermind_zoom",
            "description": (
                "Mastermind session only. Open line id+n of the Mastermind memory view "
                "into the two lines of n/2 messages it was made from; n = 1 gives "
                "message id whole. A long message comes in pages from character `at`, "
                "saying where to go on. Zoom whenever a summary only mentions "
                "something you need, before you act, guess or ask."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "First message of the line.",
                    },
                    "n": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Messages the line covers (a power of 2).",
                    },
                    "at": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Character to start a page at.",
                    },
                },
                "required": ["id"],
            },
        },
        {
            "name": "mastermind_date",
            "description": "Mastermind session only. The date and time of Mastermind message id.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "integer", "minimum": 0}},
                "required": ["id"],
            },
        },
    ]


def mastermind_on() -> bool:
    """Whether Mastermind is on: its switch file, read by this MCP process."""
    from kiro_crew.mastermind.constants import ENABLED_FILE
    from kiro_crew.mastermind.slot import tree_dir

    return (tree_dir() / ENABLED_FILE).exists()


def advertised() -> list[dict[str, Any]]:
    """``schemas()`` while Mastermind is on, else nothing. Each Mastermind turn
    starts a fresh session, so a switch reaches the next turn's tool list."""
    return schemas() if mastermind_on() else []


def _strict_key() -> tuple[str, str]:
    return mcp_core.require_strict_session_key(
        "Error: this session's identity could not be verified strictly, so "
        "Mastermind's memory is not reachable from here."
    )


def _answer(d: dict[str, Any]) -> str:
    if d.get("error"):
        return f"Error: {d['error']}"
    return str(d.get("text", ""))


def mastermind_zoom(name: str, args: dict[str, Any]) -> str:
    try:
        msg_id, n, at = int(args["id"]), int(args.get("n") or 1), int(args.get("at") or 0)
    except (KeyError, TypeError, ValueError):
        return "Error: id (and n, at if given) must be integers"
    sk, err = _strict_key()
    if err:
        return err
    return _answer(mcp_core._get(f"/api/mastermind/zoom?id={msg_id}&n={n}&at={at}", session_key=sk))


def mastermind_date(name: str, args: dict[str, Any]) -> str:
    try:
        msg_id = int(args["id"])
    except (KeyError, TypeError, ValueError):
        return "Error: id must be an integer"
    sk, err = _strict_key()
    if err:
        return err
    return _answer(mcp_core._get(f"/api/mastermind/date?id={msg_id}", session_key=sk))


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "mastermind_zoom": mastermind_zoom,
    "mastermind_date": mastermind_date,
}
