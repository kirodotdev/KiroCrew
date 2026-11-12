"""Per-tab stream scoping: send a slot's streamed frames only to tabs that show it.

A dashboard tab declares :data:`CHUNK_SCOPE_CAPABILITY` in the ``caps`` query
parameter of ``/api/ws`` and then names, in ``chunk_watch`` frames, every slot it
renders a transcript for. For such a socket the hub sends the high-rate stream
frames (:data:`SCOPED_STREAM_FRAMES`) of a slot only while the socket watches it.
For a slot it does not watch the socket gets only what its session list draws:

* a ``chat_phase`` frame when the slot's live phase moves to ``streaming`` or
  ``thinking`` (one per transition, not one per chunk);
* a ``tool_status`` frame per tool call, carrying the fields the status line is
  built from, with a large ``input_preview`` shrunk to what the title needs;
* a ``tool_status`` frame with ``returned: true`` when a call returns, so the
  status line moves off the finished call. It carries ``output`` only when the
  client copies it into a slot's scrollback (a spawn launch or an answered ask);
* the slot's reasoning, held back and sent as one ``chat_thinking`` frame per run.
  Reasoning is never persisted, so the transcript re-read cannot restore it: the
  held text goes out before the next frame of that slot reaches the socket, when
  it passes :data:`_THINK_FLUSH_CHARS`, or when the socket starts watching the
  slot.

Every coarse frame (``slots``, ``slot_patch``, ``chat_message``, ``chat_status``,
``chat_done``, ...) still reaches every socket. A socket without the capability
(an older bundle, a companion window, an app token) receives every frame, as
before.
"""

from __future__ import annotations

import json
from typing import Any

#: Capability a dashboard tab declares in ``?caps=`` when its bundle sends
#: ``chunk_watch`` and handles ``chat_phase``, ``tool_status`` and
#: ``chunk_watch_ack``.
CHUNK_SCOPE_CAPABILITY = "chunk_scope.1"

#: Socket attribute recording that declaration. Only a dashboard-user socket
#: carries it.
CHUNK_SCOPE_WS_FLAG = "_chunk_scope"

#: Socket attribute holding the slots a capable tab renders a stream for.
CHUNK_WATCH_WS_KEY = "_chunk_watch"

#: Socket attribute holding ``{slot: phase}``: the last phase a capable socket
#: was told for each slot it does not watch.
CHUNK_PHASE_WS_KEY = "_chunk_phase"

#: The frames the watch set routes.
SCOPED_STREAM_FRAMES = frozenset({"chat_chunk", "chat_thinking", "tool_call", "tool_result"})

#: Client message that REPLACES a socket's watch set, and the server's answer.
CHUNK_WATCH_FRAME = "chunk_watch"
CHUNK_WATCH_ACK_FRAME = "chunk_watch_ack"

#: Coarse frames an unwatched slot's stream turns into.
CHAT_PHASE_FRAME = "chat_phase"
TOOL_STATUS_FRAME = "tool_status"

#: The most slots one socket may watch, and the longest key accepted.
MAX_CHUNK_WATCH_SLOTS = 64
MAX_CHUNK_WATCH_KEY_LEN = 256

#: Socket attribute holding ``{slot: {tool_call_id, ...}}``: calls whose final
#: ``tool_result`` already went to a capable socket for a slot it does not watch.
CHUNK_DONE_WS_KEY = "_chunk_done"

#: Socket attribute holding ``{slot: [chars, [text, ...]]}``: reasoning held back
#: from a capable socket for a slot it does not watch, not yet sent, with its
#: running length so each append costs O(1).
CHUNK_THINK_WS_KEY = "_chunk_think"

#: Held reasoning past this many characters goes out at once.
_THINK_FLUSH_CHARS = 16384

#: The most slots a socket's phase map holds before it is cleared. A turn's
#: ``chat_done`` removes its entry, so only a slot that never finishes stays.
_MAX_PHASE_ENTRIES = 1024

#: ``tool_call`` fields the status line reads (``chatStream.applyToolStatus``).
_TOOL_STATUS_FIELDS = (
    "slot",
    "tool",
    "kind",
    "purpose",
    "tool_call_id",
    "auto",
    "is_update",
    "is_shell",
    "tool_name",
    "mcp_server",
)

#: An ``input_preview`` at most this long is passed on whole.
_PREVIEW_KEEP_CHARS = 2048
#: Longer previews keep each string value up to this length.
_PREVIEW_VALUE_CHARS = 512
_PREVIEW_LIST_ITEMS = 16
_PREVIEW_MAX_DEPTH = 4

_PHASE_OF = {"chat_chunk": "streaming", "chat_thinking": "thinking"}

#: Output the client copies from a ``tool_result`` into a background slot's
#: scrollback (``sseToolResult``): a spawn launch and an answered ask_question.
#: Mirrors ``SPAWN_LAUNCH_MARKER`` and ``ASK_ANSWERED_HEADER`` in the website.
_SPAWN_LAUNCH_MARKER = "subagent(s)."
_ASK_ANSWERED_HEADER = "User has answered your questions:"

#: The most finished call ids one socket keeps per slot before it starts over.
_MAX_DONE_IDS = 256


def declared(caps: set[str]) -> bool:
    """Whether *caps*, already stripped of ``KIROCREW_WS_DISABLED_CAPS``
    (``ws_slot_diff.without_disabled_caps``), turns stream scoping on."""
    return CHUNK_SCOPE_CAPABILITY in caps


def clean_watch(slots: object) -> frozenset[str]:
    """The valid slot keys of a ``chunk_watch`` frame's ``slots``, capped."""
    kept: list[str] = []
    if isinstance(slots, (list, tuple)):
        for slot in slots:
            if len(kept) >= MAX_CHUNK_WATCH_SLOTS:
                break
            if (
                isinstance(slot, str)
                and slot
                and len(slot) <= MAX_CHUNK_WATCH_KEY_LEN
                and slot.isprintable()
                and slot not in kept
            ):
                kept.append(slot)
    return frozenset(kept)


def frame_slot(msg_type: str, data: object) -> str | None:
    """The slot a scoped stream frame belongs to, or None for any other frame."""
    if msg_type not in SCOPED_STREAM_FRAMES or not isinstance(data, dict):
        return None
    slot = data.get("slot")
    return slot if isinstance(slot, str) and slot else None


def delivered_type(msg_type: str) -> str:
    """The frame type a socket that does not watch the slot receives for *msg_type*."""
    if msg_type in ("tool_call", "tool_result"):
        return TOOL_STATUS_FRAME
    if msg_type == "chat_chunk":
        return CHAT_PHASE_FRAME
    return msg_type


def routes(ws: Any, slot: str) -> bool:
    """Whether *ws* is a scoped socket that does NOT watch *slot*.

    A socket that has sent no ``chunk_watch`` yet watches every slot: between
    registration and its first set, holding a stream back would drop chunks a
    client that never reads the ack (the Mochi panel) has no way to recover.
    """
    if not ws.get(CHUNK_SCOPE_WS_FLAG, False):
        return False
    watched = ws.get(CHUNK_WATCH_WS_KEY)
    if watched is None:
        return False
    return slot not in watched


def _shrink(value: Any, depth: int) -> Any:
    if isinstance(value, str):
        return value[:_PREVIEW_VALUE_CHARS]
    if depth >= _PREVIEW_MAX_DEPTH:
        return None
    if isinstance(value, dict):
        return {str(k): _shrink(v, depth + 1) for k, v in list(value.items())[:64]}
    if isinstance(value, list):
        return [_shrink(v, depth + 1) for v in value[:_PREVIEW_LIST_ITEMS]]
    return value


def slim_preview(preview: object) -> object:
    """A ``tool_call`` ``input_preview`` cut down to what a status title reads.

    A short preview passes whole. A long JSON object keeps its keys with each
    string cut to :data:`_PREVIEW_VALUE_CHARS` (paths, commands and names survive;
    file bodies do not), re-encoded so the client parses it the same way. A long
    diff keeps its header lines. Anything else long is dropped: the status line
    then falls back to the tool's own title.
    """
    if not isinstance(preview, str):
        return _shrink(preview, 0)
    if len(preview) <= _PREVIEW_KEEP_CHARS:
        return preview
    stripped = preview.lstrip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            return ""
        if isinstance(parsed, dict):
            return json.dumps(_shrink(parsed, 0))
        return ""
    if preview.startswith("---"):
        return preview[:_PREVIEW_VALUE_CHARS]
    return ""


def _result_output(output: object) -> str:
    if not isinstance(output, str):
        return ""
    if _SPAWN_LAUNCH_MARKER in output or output.lstrip().startswith(_ASK_ANSWERED_HEADER):
        return output
    return ""


def returned_payload(data: dict[str, Any]) -> dict[str, Any]:
    """The ``tool_status`` data that tells an unwatched slot's client a call returned.

    Not a ``tool_result``: a tab that cached the call's tool-log entry while it
    watched the slot would otherwise overwrite that entry's output.
    """
    out: dict[str, Any] = {
        "slot": data.get("slot"),
        "tool_call_id": data.get("tool_call_id"),
        "returned": True,
    }
    output = _result_output(data.get("output"))
    if output:
        out["output"] = output
    return out


def _final_call_id(data: dict[str, Any]) -> str | None:
    """The call id of a ``tool_result`` that ends its call, else None."""
    tcid = data.get("tool_call_id")
    if data.get("final") is False or not isinstance(tcid, str) or not tcid:
        return None
    return tcid


def tool_status_payload(data: dict[str, Any]) -> dict[str, Any]:
    out = {k: data[k] for k in _TOOL_STATUS_FIELDS if k in data}
    if "input_preview" in data:
        out["input_preview"] = slim_preview(data["input_preview"])
    return out


class UnwatchedFrames:
    """The coarse frame one fan-out sends to scoped sockets that do not watch its slot.

    Built once per fan-out and asked once per socket, so each coarse frame is
    serialized at most once however many sockets receive it.
    """

    def __init__(self, msg_type: str, slot: str, data: dict[str, Any]) -> None:
        self._msg_type = msg_type
        self._slot = slot
        self._data = data
        self._text: str | None = None
        self._held_text: dict[str, str] = {}

    def _encoded(self) -> str:
        if self._text is None:
            if self._msg_type == "tool_call":
                frame = {"type": TOOL_STATUS_FRAME, "data": tool_status_payload(self._data)}
            elif self._msg_type == "tool_result":
                frame = {"type": TOOL_STATUS_FRAME, "data": returned_payload(self._data)}
            else:
                phase = _PHASE_OF[self._msg_type]
                frame = {"type": CHAT_PHASE_FRAME, "data": {"slot": self._slot, "phase": phase}}
            self._text = json.dumps(frame)
        return self._text

    def for_socket(self, ws: Any) -> list[str]:
        """The texts to send *ws* for this frame, in order; empty when none."""
        if self._msg_type == "chat_thinking":
            return self._thinking_for(ws)
        text = self._coarse_for(ws)
        return [] if text is None else [text]

    def _thinking_for(self, ws: Any) -> list[str]:
        out: list[str] = []
        phase = self._coarse_for(ws)
        if phase is not None:
            out.append(phase)
        content = self._data.get("content")
        if isinstance(content, str) and content:
            held = _held_thinking(ws)
            entry = held.setdefault(self._slot, [0, []])
            entry[0] += len(content)
            entry[1].append(content)
            if entry[0] >= _THINK_FLUSH_CHARS:
                flushed = pop_thinking(ws, self._slot, self._held_text)
                if flushed is not None:
                    out.append(flushed)
        return out

    def _coarse_for(self, ws: Any) -> str | None:
        phases = _phases(ws)
        if self._msg_type == "tool_result":
            tcid = _final_call_id(self._data)
            if tcid is None:
                return None
            done = _done_ids(ws, self._slot)
            if len(done) >= _MAX_DONE_IDS:
                done.clear()
            done.add(tcid)
            # The client moves the line to thinking only when it still shows
            # this call, so the next reasoning run must send its phase again.
            phases[self._slot] = "result"
            return self._encoded()
        if self._msg_type == "tool_call":
            # A refinement relayed after its call's final result must not put
            # the finished call back on the status line. The client checks its
            # tool log for this, and an unwatched slot has none.
            tcid = self._data.get("tool_call_id")
            if self._data.get("is_update") is True and tcid in _done_ids(ws, self._slot):
                return None
            phases[self._slot] = "tool"
            return self._encoded()
        phase = _PHASE_OF[self._msg_type]
        if phases.get(self._slot) == phase:
            return None
        phases[self._slot] = phase
        return self._encoded()


def _held_thinking(ws: Any) -> dict[str, list[Any]]:
    held = ws.get(CHUNK_THINK_WS_KEY)
    if not isinstance(held, dict):
        held = {}
        ws[CHUNK_THINK_WS_KEY] = held
    return held


def pop_thinking(ws: Any, slot: object, encoded: dict[str, str] | None = None) -> str | None:
    """Take *ws*'s held reasoning for *slot* as one ``chat_thinking`` frame text.

    None when nothing is held, which is the common case and costs one lookup.
    *encoded* is one fan-out's cache from held text to frame text: every socket
    that did not watch the slot holds the same text, so it is encoded once.
    """
    held = ws.get(CHUNK_THINK_WS_KEY)
    if not isinstance(held, dict) or not held or not isinstance(slot, str):
        return None
    entry = held.pop(slot, None)
    if not isinstance(entry, list) or len(entry) != 2 or not entry[1]:
        return None
    text = "".join(entry[1])
    if encoded is not None and text in encoded:
        return encoded[text]
    frame = json.dumps({"type": "chat_thinking", "data": {"slot": slot, "content": text}})
    if encoded is not None:
        encoded[text] = frame
    return frame


def _done_ids(ws: Any, slot: str) -> set[str]:
    done = ws.get(CHUNK_DONE_WS_KEY)
    if not isinstance(done, dict) or len(done) > _MAX_PHASE_ENTRIES:
        done = {}
        ws[CHUNK_DONE_WS_KEY] = done
    ids = done.get(slot)
    if not isinstance(ids, set):
        ids = set()
        done[slot] = ids
    return ids


def _phases(ws: Any) -> dict[str, str]:
    phases = ws.get(CHUNK_PHASE_WS_KEY)
    if not isinstance(phases, dict) or len(phases) > _MAX_PHASE_ENTRIES:
        phases = {}
        ws[CHUNK_PHASE_WS_KEY] = phases
    return phases


def note_coarse(ws: Any, msg_type: str, data: object) -> None:
    """Keep a scoped socket's phase map in step with the frames every socket gets.

    ``chat_status`` replaces the status line with a label, so the next chunk must
    move it to ``streaming`` again; ``chat_done`` ends the turn, so the next turn's
    first chunk is a transition too.
    """
    if msg_type not in ("chat_status", "chat_done") or not ws.get(CHUNK_SCOPE_WS_FLAG, False):
        return
    done = ws.get(CHUNK_DONE_WS_KEY)
    if msg_type == "chat_done" and isinstance(done, dict) and isinstance(data, dict):
        finished = data.get("slot")
        if isinstance(finished, str):
            done.pop(finished, None)
    phases = ws.get(CHUNK_PHASE_WS_KEY)
    if not isinstance(phases, dict) or not isinstance(data, dict):
        return
    slot = data.get("slot")
    if not isinstance(slot, str) or slot not in phases:
        return
    if msg_type == "chat_done":
        del phases[slot]
    else:
        phases[slot] = "status"
