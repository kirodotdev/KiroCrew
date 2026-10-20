"""The spine agent runner's activity feed: whole lines and named tool targets.

The UI polls a feed of what the unattended agent is doing. ``_LineBuffer`` coalesces the
provider's sub-word text chunks into whole lines; ``_tool_detail`` names the file or
command a tool call targets, and ``_detail_is_richer`` decides when a refinement upgrades
the line the feed already shows; ``_summarize_stream_event`` turns one ``stream-json``
line of the subprocess runner into a feed event.

``kiro_crew.apps.builtins.auto_improvement.spine.agent_runner`` serves every name here and
is the import path callers use; a patch through it reaches this module
(``kiro_crew._facade_mirror``).
"""

from __future__ import annotations

# Longest single buffered line we'll let accumulate before force-flushing — so a wall of
# prose with no newline still emits readable chunks instead of one giant blob.
_LINE_FLUSH_LEN = 200


class _LineBuffer:
    """Coalesce a stream of tiny text chunks into COHERENT lines for the activity feed.

    The Kiro Crew provider streams assistant text as many sub-word fragments ("ismatched-",
    "ncation would"). Emitting one feed event per fragment makes the live log unreadable
    (the exact symptom the user reported). This buffers fragments and flushes a line only
    on a real boundary — a newline, a sentence end (``. ``/``! ``/``? ``), or when the
    buffer exceeds :data:`_LINE_FLUSH_LEN` — so each emitted ``💭`` line is a whole thought.

    ``emit(line)`` is the sink (called with each completed, stripped, non-empty line)."""

    def __init__(self, emit) -> None:
        self._emit = emit
        self._buf = ""

    def feed(self, chunk: str) -> None:
        self._buf += chunk
        # Flush every complete newline-terminated line.
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._send(line)
        # No newline yet, but a long buffer: flush at the last sentence boundary so we don't
        # sit on a paragraph that never contains a newline.
        if len(self._buf) >= _LINE_FLUSH_LEN:
            cut = max(self._buf.rfind(". "), self._buf.rfind("! "), self._buf.rfind("? "))
            if cut >= 40:  # only split on a boundary that yields a substantial line
                self._send(self._buf[: cut + 1])
                self._buf = self._buf[cut + 2 :]
            else:  # no good boundary — emit the whole overlong buffer as one line
                self._send(self._buf)
                self._buf = ""

    def flush(self) -> None:
        """Emit whatever's left as a final line (call at a tool boundary / turn end)."""
        if self._buf.strip():
            self._send(self._buf)
        self._buf = ""

    def _send(self, line: str) -> None:
        line = line.strip()
        if line:
            self._emit(line[:_LINE_FLUSH_LEN])


# Common rawInput keys that name a tool's TARGET (the thing the operator wants to see in the
# feed: which file was read/edited, which command ran). Checked in order; the first present,
# non-empty value wins. Mirrors how the dashboard derives a tool pill's subtitle.
_TOOL_TARGET_KEYS = (
    "path",
    "file_path",
    "filePath",
    "abs_path",
    "command",
    "cmd",
    "pattern",
    "query",
    "url",
)


def _tool_detail(ev: object) -> str:
    """A human-readable detail for a tool-call feed line — preferring the TARGET (file path /
    command) over a generic label. The initial ``EVENT_TOOL_CALL`` from claude-agent-acp
    often carries only a generic ``title`` ("Read File") with empty ``tool_input``; the real
    path arrives in the ``EVENT_TOOL_CALL_UPDATE`` refinement (``raw_tool_params`` populated).
    This pulls the target from ``raw_tool_params`` first, then falls back to a non-generic
    ``title``, then the raw ``tool_input`` — so the feed shows "Read File · src/.../ruler.py"
    instead of a bare "Read File"."""
    raw = getattr(ev, "raw_tool_params", None)
    target = ""
    if isinstance(raw, dict):
        for k in _TOOL_TARGET_KEYS:
            v = raw.get(k)
            if isinstance(v, str) and v.strip():
                target = v.strip()
                break
    title = (getattr(ev, "title", "") or "").strip()
    if target:
        # Combine the label + target when the title is a generic label (no path in it);
        # if the title already contains the target, just use the title.
        if title and target not in title:
            return f"{title} · {target}"[:160]
        return (title or target)[:160]
    # No structured target — fall back to the title, else the raw tool_input snippet.
    if title:
        return title[:160]
    return (getattr(ev, "tool_input", "") or "")[:80]


def _detail_is_richer(refined: str, prior: str) -> bool:
    """True if ``refined`` is a more informative tool detail than ``prior`` — i.e. it is
    LONGER or contains a path-like segment the prior line lacked. Guards the refinement
    re-emit so a tool_call_update that adds nothing (same generic label) does not double the
    feed, but one that adds the file path DOES upgrade the line."""
    if not refined:
        return False
    if not prior:
        return True
    if "/" in refined and "/" not in prior:
        return True
    return len(refined) > len(prior) + 3


def _summarize_stream_event(obj: dict) -> dict | None:
    """Turn one ``stream-json`` line into a concise activity event for the UI feed, or
    None to skip. We surface the human-meaningful turns: assistant text (truncated), a
    tool invocation (which tool + a short target), and the final result line. We avoid
    dumping raw tool *output* (can be huge / noisy) — the UI feed wants "what is the
    agent doing", not full payloads."""
    t = obj.get("type")
    # Assistant turn: may carry text and/or tool_use blocks.
    if t == "assistant":
        msg = obj.get("message", {}) or {}
        content = msg.get("content", []) or []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "tool_use":
                name = block.get("name", "tool")
                inp = block.get("input", {}) or {}
                # a short, safe target hint per common tool
                hint = (
                    inp.get("file_path")
                    or inp.get("path")
                    or inp.get("pattern")
                    or inp.get("command")
                    or inp.get("description")
                    or ""
                )
                if isinstance(hint, str) and len(hint) > 80:
                    hint = hint[:77] + "…"
                return {"kind": "tool", "tool": name, "detail": str(hint)}
            if bt == "text":
                txt = (block.get("text") or "").strip()
                if txt:
                    return {"kind": "text", "detail": txt[:200]}
        return None
    if t == "result":
        return {"kind": "result", "detail": ("done" if not obj.get("is_error") else "error")}
    return None
