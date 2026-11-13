"""Spot a turn that keeps making the same tool call and getting the same result.

An agent stuck in a doom loop re-runs one action that does not work, over and
over, instead of changing strategy. The signal is narrow on purpose: the SAME
tool with the SAME input coming back with the SAME result (status and output)
:data:`REPEAT_LOOP_THRESHOLD` times in one turn. Repeating such a call again
cannot change its outcome, so the turn is told so once, in-band, and left to
decide. Nothing is aborted: a call that is deliberately polled for an outside
change trips the same signal, and the notice says to carry on in that case.

Tools that drive something outside the turn (a browser page, a desktop app)
are left out by identity, see :data:`_EXEMPT_TOOL_IDS`: their result text
echoes the action (``Browser press_key: {"key": "ArrowDown"}``) while the page
behind it changes every time, so an identical result says nothing about a loop.

Known gap, left for a follow-up: a call's streak is kept per signature, so
other calls made in between do not reset it.

A leaf: stdlib only, so every surface that runs a turn can hold one tracker
per turn without importing the dashboard.
"""

from __future__ import annotations

import hashlib
import json
import re

#: Identical results of one identical call, in one turn, before the notice.
REPEAT_LOOP_THRESHOLD = 3

#: Bound on the call ids and signatures one tracker holds; a turn past it stops
#: tracking new ones rather than growing without limit.
_MAX_TRACKED = 512

#: Longest slice of the call's title quoted back in the notice.
_TITLE_LIMIT = 200

#: Bare tool names never tracked. Each acts on state outside the turn, so the
#: same result text after the same input is normal, not a loop. ``browser`` is
#: ``kirocrew-core``'s browser tool (``mcp_tools/browser.py``); the
#: ``computer_*`` names are the ``kirocrew-computer`` server's tools
#: (``computer_use/types.py``). Matched on the name's last segment, so every
#: harness spelling (``browser``, ``kirocrew-core::browser``,
#: ``mcp__kirocrew-core__browser``, ``@kirocrew-core/browser``) is covered.
_EXEMPT_TOOL_IDS: frozenset[str] = frozenset(
    {
        "browser",
        "computer_list_apps",
        "computer_launch_app",
        "computer_get_state",
        "computer_click",
        "computer_drag",
        "computer_type_text",
        "computer_press_key",
        "computer_set_value",
        "computer_scroll",
        "computer_perform_action",
        "computer_end_turn",
    }
)

#: The host's generic wrapper for a deferred tool: the real tool is the
#: ``tool_id`` field of its input.
_WRAPPER_TOOL = "tool_call"

#: ``tool_id`` read from a wrapper input that is not whole JSON (cut short).
_TOOL_ID_RE = re.compile(r'"tool_id"\s*:\s*"([^"\\]{1,512})"')

#: Separators between a server and a tool in the spellings harnesses use.
_NAME_SEP_RE = re.compile(r"::|__|/")


def _tool_leaf(name: str) -> str:
    """The bare tool part of a possibly server-qualified tool name."""
    return _NAME_SEP_RE.split((name or "").strip())[-1]


def _wrapped_tool_id(tool_input: str) -> str:
    """The ``tool_id`` a ``tool_call`` wrapper's input names, or ""."""
    text = tool_input or ""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        value = parsed.get("tool_id")
        return value.strip() if isinstance(value, str) else ""
    match = _TOOL_ID_RE.search(text)
    return match.group(1).strip() if match else ""


def _real_tool_name(tool_name: str, tool_input: str) -> str:
    """The tool a call runs: the wrapped ``tool_id`` for ``tool_call``, else *tool_name*.

    A frame that names no tool at all is read the same way, since a wrapper
    call's input is then the only place its tool is named.
    """
    if not (tool_name or "").strip() or _tool_leaf(tool_name) == _WRAPPER_TOOL:
        return _wrapped_tool_id(tool_input) or tool_name
    return tool_name


def is_exempt_tool(tool_name: str, tool_input: str = "") -> bool:
    """True for a call to a tool in :data:`_EXEMPT_TOOL_IDS`, wrapped or not."""
    return _tool_leaf(_real_tool_name(tool_name, tool_input)) in _EXEMPT_TOOL_IDS


def _digest(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()


def build_repeat_loop_notice(title: str, count: int) -> str:
    """The in-band notice for a call repeated *count* times with one result."""
    shown = " ".join((title or "").split())[:_TITLE_LIMIT] or "a tool call"
    return (
        f"[Kiro Crew host notice] You have made the same tool call {count} times "
        f"this turn and got the same result each time: {shown}\n\n"
        "Running it again will not change the outcome. Stop and diagnose: read "
        "the result, check what you assumed, then try a genuinely different "
        "approach, or say plainly what blocks you. If you are deliberately "
        "waiting for something outside this turn to change, carry on."
    )


class RepeatLoopTracker:
    """Per-turn count of identical calls that return identical results."""

    def __init__(self) -> None:
        self._calls: dict[str, tuple[str, str]] = {}
        self._outputs: dict[str, str] = {}
        self._last_outcome: dict[str, str] = {}
        self._streak: dict[str, int] = {}
        self._warned: set[str] = set()

    def note_call(self, tool_call_id: str, tool_name: str, tool_input: str, title: str) -> None:
        """Record what a call is, so its result can be matched to its input.

        Called again for the same id (an input refinement) it replaces the
        earlier record, because the refined input is the real one. A call to an
        exempt tool (:func:`is_exempt_tool`) is never recorded, so it is never
        counted and never draws the notice.
        """
        if not tool_call_id or not (tool_input or "").strip():
            return
        key = _digest(tool_call_id)
        if is_exempt_tool(tool_name, tool_input):
            # A refinement may name the tool the first frame did not.
            self._calls.pop(key, None)
            self._outputs.pop(key, None)
            return
        if key not in self._calls and len(self._calls) >= _MAX_TRACKED:
            return
        name = _real_tool_name(tool_name, tool_input)
        shown = " ".join((title or name or "").split())[:_TITLE_LIMIT]
        self._calls[key] = (_digest(tool_name or "", tool_input), shown)

    def note_result(
        self,
        tool_call_id: str,
        *,
        status: str,
        output: str,
        output_digest: str = "",
        terminal: bool = True,
    ) -> str:
        """Feed one result frame; return the notice once the loop is seen.

        A backend may send a call's output and its terminal status in separate
        frames, so a non-terminal frame only keeps its output for the terminal
        one. Returns "" otherwise, and at most one notice per call signature
        per turn.
        """
        key = _digest(tool_call_id) if tool_call_id else ""
        if not key or key not in self._calls:
            return ""
        seen = output_digest or (_digest(output) if output else "")
        if not terminal:
            if seen:
                self._outputs[key] = seen
            return ""
        signature, title = self._calls.pop(key)
        kept = self._outputs.pop(key, "")
        outcome = _digest(status or "", seen or kept)
        if signature not in self._streak and len(self._streak) >= _MAX_TRACKED:
            return ""
        if self._last_outcome.get(signature) == outcome:
            self._streak[signature] += 1
        else:
            self._last_outcome[signature] = outcome
            self._streak[signature] = 1
        if self._streak[signature] < REPEAT_LOOP_THRESHOLD or signature in self._warned:
            return ""
        self._warned.add(signature)
        return build_repeat_loop_notice(title, self._streak[signature])
