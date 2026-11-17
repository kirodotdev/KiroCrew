"""Replies for the fake ACP backend, read from a script file a test writes.

The packaged fake backend (:mod:`kiro_crew.testing.fake_acp_backend`) answers
every prompt with one canned line unless the prompt carries a marker. That is
enough for Kiro Crew's own end-to-end suite, but someone testing an agent spec,
a hook, a cron script or an app needs the model to say particular things in a
particular order. ``KIROCREW_FAKE_ACP_SCRIPT`` names a JSON file of those
replies; with it unset nothing here runs and the fake behaves exactly as before.

The file is one object with a ``replies`` list. Each entry is an object::

    {"when": "deploy", "text": "Not without approval.", "stop": "refusal"}
    {"text": "Running the tests.", "tools": [{"title": "Run tests",
        "kind": "execute", "input": {"command": "pytest"}, "status": "completed"}]}
    {"error": "model unavailable"}

* ``when`` -- answer every prompt whose text contains this string (plain,
  case-sensitive substring). The first such entry wins, every time it matches.
* An entry without ``when`` is answered once, in file order, per session: the
  first prompt of a session that no ``when`` entry claimed gets the first one,
  the next gets the second, and so on. After the last, prompts fall back to
  the fake's built-in behaviour.
* ``text`` -- the reply, streamed as one message chunk.
* ``tools`` -- tool calls streamed before the reply: ``title`` (required),
  ``kind`` (default ``other``), ``input`` (an object, default empty) and
  ``status`` (``completed`` or ``failed``, default ``completed``).
* ``stop`` -- ``end_turn`` (default), ``max_tokens`` or ``refusal``.
* ``error`` -- fail the turn with a JSON-RPC error carrying this message
  instead of replying; it cannot be combined with ``text`` or ``tools``.

A tool call or an error message is one protocol frame and cannot be split, so
one whose serialized form passes ``MAX_FRAME_CHARS`` is dropped with a warning
when the file loads; ``text`` has no such limit because the backend sends it in
chunks.

Nothing in a script can make a turn fail that would not fail without it,
except an explicit ``error`` entry. An unreadable file, a file that is not this
shape, or an entry that breaks a rule is dropped, the fake keeps its built-in
answer for what was dropped, and a warning naming what was dropped and why is
written to stderr once.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.terminal_safe import safe_terminal_line

SCRIPT_ENV = "KIROCREW_FAKE_ACP_SCRIPT"
STOP_REASONS = ("end_turn", "max_tokens", "refusal")
TOOL_STATUSES = ("completed", "failed")
#: What one tool-call or error frame may serialize to. The transports that read
#: these frames discard a line past 10 MiB, so this stays well under it.
MAX_FRAME_CHARS = 1024 * 1024
_ENTRY_KEYS = frozenset({"when", "text", "tools", "stop", "error"})
_TOOL_KEYS = frozenset({"title", "kind", "input", "status"})


@dataclass(frozen=True)
class ScriptedTool:
    title: str
    kind: str = "other"
    input: dict[str, Any] = field(default_factory=dict)
    status: str = "completed"


@dataclass(frozen=True)
class ScriptedReply:
    text: str = ""
    tools: tuple[ScriptedTool, ...] = ()
    stop: str = "end_turn"
    error: str | None = None


@dataclass
class Script:
    matched: list[tuple[str, ScriptedReply]] = field(default_factory=list)
    ordered: list[ScriptedReply] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _cursor: dict[str, int] = field(default_factory=dict)

    def reply_for(self, session_id: str, prompt: str) -> ScriptedReply | None:
        """The scripted answer to *prompt*, or ``None`` to use the built-in one."""
        for needle, reply in self.matched:
            if needle in prompt:
                return reply
        index = self._cursor.get(session_id, 0)
        if index >= len(self.ordered):
            return None
        self._cursor[session_id] = index + 1
        return self.ordered[index]


def _oversized(*values: Any) -> bool:
    """True when *values* serialize past ``MAX_FRAME_CHARS`` (or cannot serialize)."""
    try:
        return sum(len(json.dumps(value)) for value in values) > MAX_FRAME_CHARS
    except (TypeError, ValueError, RecursionError):
        return True


def _parse_tool(raw: Any, where: str, warnings: list[str]) -> ScriptedTool | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("title"), str):
        warnings.append(f"{where}: dropped, a tool needs a string 'title'")
        return None
    for key in sorted(set(raw) - _TOOL_KEYS):
        warnings.append(f"{where}: ignored unknown key {key!r}")
    kind = raw.get("kind", "other")
    if not isinstance(kind, str):
        warnings.append(f"{where}: 'kind' is not a string, used 'other'")
        kind = "other"
    tool_input = raw.get("input", {})
    if not isinstance(tool_input, dict):
        warnings.append(f"{where}: 'input' is not an object, used {{}}")
        tool_input = {}
    status = raw.get("status", "completed")
    if status not in TOOL_STATUSES:
        warnings.append(f"{where}: 'status' is not one of {TOOL_STATUSES}, used 'completed'")
        status = "completed"
    if _oversized(raw["title"], kind, tool_input):
        warnings.append(f"{where}: dropped, its frame would pass {MAX_FRAME_CHARS} characters")
        return None
    return ScriptedTool(title=raw["title"], kind=kind, input=tool_input, status=status)


def _parse_entry(raw: Any, where: str, warnings: list[str]) -> ScriptedReply | None:
    if not isinstance(raw, dict):
        warnings.append(f"{where}: dropped, an entry must be an object")
        return None
    for key in sorted(set(raw) - _ENTRY_KEYS):
        warnings.append(f"{where}: ignored unknown key {key!r}")
    error = raw.get("error")
    if error is not None:
        if not isinstance(error, str) or "text" in raw or "tools" in raw:
            warnings.append(f"{where}: dropped, 'error' must be a string and stand alone")
            return None
        if _oversized(error):
            warnings.append(f"{where}: dropped, its frame would pass {MAX_FRAME_CHARS} characters")
            return None
        return ScriptedReply(error=error)
    text = raw.get("text", "")
    if not isinstance(text, str):
        warnings.append(f"{where}: dropped, 'text' must be a string")
        return None
    raw_tools = raw.get("tools", [])
    if not isinstance(raw_tools, list):
        warnings.append(f"{where}: 'tools' is not a list, sent no tool calls")
        raw_tools = []
    tools = []
    for n, raw_tool in enumerate(raw_tools):
        tool = _parse_tool(raw_tool, f"{where}.tools[{n}]", warnings)
        if tool is not None:
            tools.append(tool)
    stop = raw.get("stop", "end_turn")
    if stop not in STOP_REASONS:
        warnings.append(f"{where}: 'stop' is not one of {STOP_REASONS}, used 'end_turn'")
        stop = "end_turn"
    return ScriptedReply(text=text, tools=tuple(tools), stop=stop)


def parse_script(text: str, name: str) -> Script:
    """Build a :class:`Script` from the file's *text*; *name* labels warnings."""
    script = Script()
    try:
        data = json.loads(text)
    except ValueError:
        script.warnings.append(f"{name}: ignored, not valid JSON")
        return script
    except RecursionError:
        script.warnings.append(f"{name}: ignored, nested too deeply to read")
        return script
    replies = data.get("replies") if isinstance(data, dict) else None
    if not isinstance(replies, list):
        script.warnings.append(f"{name}: ignored, expected an object with a 'replies' list")
        return script
    for n, raw in enumerate(replies):
        where = f"{name} replies[{n}]"
        reply = _parse_entry(raw, where, script.warnings)
        if reply is None:
            continue
        when = raw.get("when")
        if when is None:
            script.ordered.append(reply)
        elif isinstance(when, str) and when:
            script.matched.append((when, reply))
        else:
            script.warnings.append(f"{where}: dropped, 'when' must be a non-empty string")
    return script


def load_script(path: str) -> Script:
    """Read and parse the script at *path* through the guarded file reader.

    A refusal or read failure names only the file and the failure's class: the
    exception text can quote the resolved target of a link.
    """
    name = os.path.basename(path) or path
    try:
        from kiro_crew.hooks import safe_read_file

        text = safe_read_file(path)
    except (OSError, UnicodeDecodeError, ImportError) as exc:
        return Script(warnings=[f"{name}: ignored, could not be read ({type(exc).__name__})"])
    return parse_script(text, name)


_LOADED: dict[str, Script] = {}


def active_script() -> Script | None:
    """The script ``KIROCREW_FAKE_ACP_SCRIPT`` names, loaded once per path.

    ``None`` when the variable is unset. Warnings go to stderr on first load.
    """
    path = os.environ.get(SCRIPT_ENV, "").strip()
    if not path:
        return None
    script = _LOADED.get(path)
    if script is None:
        script = load_script(path)
        _LOADED[path] = script
        for warning in script.warnings:
            sys.stderr.write(f"fake ACP backend script: {safe_terminal_line(warning)}\n")
    return script
