"""Local machine: keep a shared MCP server's locally turned-off tools off (B14).

A ticked stdio server may have tools turned off in this computer's MCP config
(``disabledTools``). The grant snapshots that list, and the connector runs the
server's stream through a :class:`ToolFilter`, so the limit is enforced HERE,
not trusted to the remote gateway:

* gateway -> server: every line is parsed as JSON-RPC and the RE-ENCODED message
  is forwarded, so the server reads exactly what was checked (a duplicate
  ``name`` key cannot smuggle a turned-off tool past a parser that keeps the
  first key). A ``tools/call`` for a turned-off tool is not forwarded and gets a
  tool error; a line that is not JSON, or longer than :data:`MAX_LINE_BYTES`,
  is dropped whole.
* server -> gateway: output goes out in whole lines, and the reply to a
  ``tools/list`` this filter forwarded lists only the tools still on. Other
  lines pass byte for byte. A line past :data:`MAX_LINE_BYTES` passes through
  unparsed (it cannot be a tool list this filter must trim without holding it
  all); a call to a hidden tool is still refused on the way in.

An entry is matched as kiro-cli reads ``disabledTools``
(:func:`tool_is_turned_off`): exactly, and one holding ``*`` or ``?`` also as a
glob, so ``delete_*`` keeps ``delete_repo`` off here too.

A server with no tool turned off gets no filter: its stream is relayed as is.
Sans-IO: the connector owns the pipes and the credit (:mod:`.connector`).
"""

from __future__ import annotations

import fnmatch
import json
from typing import Any, Final, Iterable

#: Longest JSON-RPC line judged; the gateway refuses longer requests itself.
MAX_LINE_BYTES: Final[int] = 8 * 1024 * 1024
#: Most turned-off tools one shared server may carry.
MAX_DISABLED_TOOLS: Final[int] = 512
#: Most ``tools/list`` replies awaited at once; one past is refused.
MAX_TRACKED_LISTS: Final[int] = 64

_TURNED_OFF: Final[str] = "this tool is turned off on this computer"


def _reject_constant(name: str) -> Any:
    raise ValueError(f"not JSON: {name}")


def _parse(line: bytes) -> Any:
    return json.loads(line, parse_constant=_reject_constant)


def _encode(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def tool_is_turned_off(tool: str, entries: Iterable[str]) -> bool:
    """Whether a ``disabledTools`` entry in *entries* covers *tool*.

    The rule :func:`kiro_crew.cron_script_mcp._tool_is_switched_off` applies, kept
    here so the connector needs none of that module's imports (a test pins the two
    equal): an exact match, and an entry with ``*`` or ``?`` also as a
    :func:`fnmatch.fnmatchcase` glob. A glob holding ``{``, ``}`` or ``\\``, whose
    meaning ``fnmatch`` does not share, covers every tool.
    """
    for entry in entries:
        if entry == tool:
            return True
        if "*" in entry or "?" in entry:
            if any(c in entry for c in "{}\\") or fnmatch.fnmatchcase(tool, entry):
                return True
    return False


class ToolFilter:
    """One shared server's stream, with ``disabled`` tools kept off."""

    def __init__(self, disabled: Iterable[str]) -> None:
        names = frozenset(str(n) for n in disabled)
        if len(names) > MAX_DISABLED_TOOLS:
            raise ValueError(f"at most {MAX_DISABLED_TOOLS} tools can be turned off")
        self.disabled = names
        self._in = bytearray()
        self._in_skipping = False
        self._out = bytearray()
        self._out_passing = False
        self._lists: set[str] = set()

    # -- gateway -> server -------------------------------------------------------
    def inbound(self, data: bytes) -> tuple[bytes, bytes]:
        """``data`` from the gateway -> (bytes for the server, replies for the gateway)."""
        to_child = bytearray()
        replies = bytearray()
        self._in += data
        while True:
            end = self._in.find(b"\n")
            if end < 0:
                if len(self._in) > MAX_LINE_BYTES:
                    self._in.clear()
                    self._in_skipping = True
                break
            line = bytes(self._in[:end])
            del self._in[: end + 1]
            if self._in_skipping:
                self._in_skipping = False
                continue
            out, reply = self._judge(line)
            to_child += out
            replies += reply
        return bytes(to_child), bytes(replies)

    def inbound_eof(self) -> tuple[bytes, bytes]:
        """The gateway's input ended: judge a last line sent without a newline."""
        line = bytes(self._in)
        self._in.clear()
        skipping, self._in_skipping = self._in_skipping, False
        if skipping or not line.strip():
            return b"", b""
        return self._judge(line)

    def _judge(self, line: bytes) -> tuple[bytes, bytes]:
        if not line.strip():
            return b"", b""
        try:
            msg = _parse(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return b"", b""
        if isinstance(msg, list):
            kept: list[Any] = []
            replies = bytearray()
            for item in msg:
                keep, reply = self._one(item)
                if keep:
                    kept.append(item)
                replies += reply
            return (_encode(kept) if kept else b""), bytes(replies)
        keep, reply = self._one(msg)
        return (_encode(msg) if keep else b""), reply

    def _one(self, msg: Any) -> tuple[bool, bytes]:
        """Whether to forward ``msg``, and the reply sent in its place if not."""
        if not isinstance(msg, dict):
            return True, b""
        method = msg.get("method")
        has_id = "id" in msg
        if method == "tools/call":
            params = msg.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            if not isinstance(name, str) or tool_is_turned_off(name, self.disabled):
                if not has_id:
                    return False, b""
                result = {"content": [{"type": "text", "text": _TURNED_OFF}], "isError": True}
                return False, _encode({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        elif method == "tools/list" and has_id:
            key = json.dumps(msg["id"], sort_keys=True)
            if key not in self._lists and len(self._lists) >= MAX_TRACKED_LISTS:
                error = {"code": -32000, "message": "too many tool lists requested at once"}
                return False, _encode({"jsonrpc": "2.0", "id": msg["id"], "error": error})
            self._lists.add(key)
        return True, b""

    def _hidden(self, tool: Any) -> bool:
        if not isinstance(tool, dict):
            return False
        name = tool.get("name")
        return isinstance(name, str) and tool_is_turned_off(name, self.disabled)

    # -- server -> gateway -------------------------------------------------------
    @property
    def at_boundary(self) -> bool:
        """True when no partial output line has been sent to the gateway."""
        return not self._out_passing

    def outbound(self, data: bytes) -> bytes:
        """Server output -> what to send the gateway now (whole lines, trimmed lists)."""
        out = bytearray()
        while data:
            if self._out_passing:
                end = data.find(b"\n")
                if end < 0:
                    out += data
                    return bytes(out)
                out += data[: end + 1]
                data = data[end + 1 :]
                self._out_passing = False
                continue
            self._out += data
            data = b""
            while True:
                end = self._out.find(b"\n")
                if end < 0:
                    break
                line = bytes(self._out[: end + 1])
                del self._out[: end + 1]
                out += self._trim(line)
            if len(self._out) > MAX_LINE_BYTES:
                out += self._out
                self._out.clear()
                self._out_passing = True
        return bytes(out)

    def outbound_eof(self) -> bytes:
        """The server's output ended: what is left, as it was."""
        tail = bytes(self._out)
        self._out.clear()
        self._out_passing = False
        return tail

    def _trim(self, line: bytes) -> bytes:
        if not self._lists:
            return line
        try:
            msg = _parse(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return line
        if not isinstance(msg, dict) or "id" not in msg or "method" in msg:
            return line
        key = json.dumps(msg["id"], sort_keys=True)
        if key not in self._lists:
            return line
        self._lists.discard(key)
        result = msg.get("result")
        if not isinstance(result, dict):
            return line
        tools = result.get("tools")
        if not isinstance(tools, list):
            return line
        result["tools"] = [t for t in tools if not self._hidden(t)]
        return _encode(msg)
