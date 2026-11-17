"""What a dashboard reader who is not the owner receives while redaction is off.

The owner's redaction setting governs only what the owner ALONE reads
(:func:`kiro_crew.redaction_seams.owner_alone`). The chat turn runs inside
``output_seam(CHAT)``, so with the setting off the text it produces (its websocket
frames, its chat SSE events, the rows a history read returns) is as written for every
reader. Each dashboard edge that hands that text to a reader asks who the reader is,
and a reader who is not the owner gets it redacted in full, whatever the setting:

* the websocket send chokepoint (``websocket_hub.WebSocketHub._spawn_ws_send``) and
  the direct sends of a socket's connect snapshot and subagent replay:
  :func:`for_socket`;
* the chat SSE writer (``chat_api.chat_send``): :class:`SseEvents`;
* every HTTP request: :func:`redaction_audience_middleware`, right after token auth
  on both server chains, runs a non-owner request's handler inside
  ``redaction_switch.always_scan``, so each credential pass it makes (a history read,
  a side or thread read, a slot detail) runs in full. A task the handler starts, a
  chat turn a teammate sends among them, inherits the scope and runs redacted in full
  for every reader.

A full pass here runs in a fresh :class:`contextvars.Context` under ``always_scan``,
as the live log stream's does (``handlers.updates_streams._stream_safe``), so no seam
or owner view the caller is inside stands it down.
"""

from __future__ import annotations

import contextvars
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from aiohttp import web

from kiro_crew import redaction_seams

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: The websocket frames whose ``content`` is one slice of a streamed text. A
#: credential can straddle two slices, so each socket runs its own stream redactor
#: per slot over them, the one the turn itself runs with the setting on.
_STREAMED_FRAMES = frozenset({"chat_chunk", "chat_thinking"})

#: The frame that ends a slot's streams.
_STREAM_END_FRAME = "chat_done"

#: Socket attribute holding ``{(frame type, slot): StreamRedactor}``. Kept across a
#: change of the setting, so a credential that started while it was off is still read
#: whole by the stream it started in.
_STREAMS_WS_KEY = "_non_owner_streams"

#: The most streams one socket holds; past it they start again.
_MAX_STREAMS_PER_SOCKET = 64

#: The frames redacted most recently, ``text -> (shown, slot whose streams it ends)``,
#: so one fan-out to N non-owner sockets costs one redaction. A frame whose text and
#: redacted copy together pass :data:`_MAX_FRAME_CHARS` is not kept, so the memo holds
#: at most ``_MAX_FRAMES * _MAX_FRAME_CHARS`` characters.
_MAX_FRAMES = 32
_MAX_FRAME_CHARS = 64 * 1024
_frames: dict[str, tuple[str, str]] = {}
_frames_generation = -1


def _in_full(fn: Callable[..., _T], *args: Any) -> _T:
    """``fn(*args)`` with every credential pass in it run in full."""
    from kiro_crew.security.redaction_switch import always_scan

    def run() -> _T:
        with always_scan():
            return fn(*args)

    return contextvars.Context().run(run)


def _redact_values(value: Any) -> Any:
    """*value*, a decoded JSON document, with every string value redacted."""
    from kiro_crew.security import redact

    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_values(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_values(item) for key, item in value.items()}
    return value


# ── the audience of a request ──


def request_audience(request: web.Request) -> str:
    """Who reads what *request* is answered with, as a ``redaction_seams`` audience.

    :data:`~kiro_crew.redaction_seams.OWNER_DASHBOARD` for the owner's own session
    (``handlers.source_providers.is_owner_dashboard_request``), and
    :data:`~kiro_crew.redaction_seams.MODEL` for the crew's own agent: a caller that
    proved the internal secret and carries no app claim (kiro-cli, an MCP server), whose
    answer goes to the model. Anything else, an app token included, is
    :data:`~kiro_crew.redaction_seams.NON_OWNER`, and so is a request the check fails on.
    """
    try:
        if request.get("internal_auth") is True and not request.get("app"):
            return redaction_seams.MODEL
        from kiro_crew.dashboard.handlers.source_providers import (
            is_host_owner_dashboard_request,
        )

        # The machine owner alone: a Shared Crew teammate passes the owner gate, but
        # the owner's OFF covers only what the owner sees.
        if is_host_owner_dashboard_request(request):
            return redaction_seams.OWNER_DASHBOARD
    except Exception:
        logger.debug("redaction audience: owner check failed; read as non-owner", exc_info=True)
    return redaction_seams.NON_OWNER


@web.middleware
async def redaction_audience_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Run the handler of a request the owner does not alone read with every pass in full.

    Inner to token auth, which sets the claims :func:`request_audience` reads. Decided
    whatever the setting reads now: a websocket's handler lives for the socket, and the
    setting can be turned off while it runs.
    """
    if redaction_seams.owner_alone(request_audience(request)):
        return await handler(request)
    from kiro_crew.security.redaction_switch import always_scan

    with always_scan():
        return await handler(request)


# ── websocket frames ──


def _remember(text: str, shown: str, ends: str) -> str:
    """Keep *shown* as what *text* is for every non-owner socket, and return it."""
    global _frames_generation
    generation = redaction_seams.generation()
    if generation != _frames_generation:
        _frames.clear()
        _frames_generation = generation
    if len(text) + len(shown) > _MAX_FRAME_CHARS:
        return shown
    if len(_frames) >= _MAX_FRAMES:
        del _frames[next(iter(_frames))]
    _frames[text] = (shown, ends)
    return shown


def _socket_streams(ws: Any) -> dict[tuple[str, str], Any] | None:
    """The stream redactors of *ws*, or None for a socket that holds no state."""
    try:
        streams = ws.get(_STREAMS_WS_KEY)
        if not isinstance(streams, dict):
            streams = ws[_STREAMS_WS_KEY] = {}
    except (AttributeError, TypeError):
        return None
    held: dict[tuple[str, str], Any] = streams
    return held


def _end_streams(ws: Any, slot: str) -> None:
    """Drop the streams of *slot* on *ws*: what they held never streams.

    The slot's saved rows, redacted in full for this socket, show it instead.
    """
    streams = _socket_streams(ws)
    if streams:
        for key in [key for key in streams if key[1] == slot]:
            del streams[key]


def _streamed_frame(ws: Any, frame: dict[str, Any], kind: str, slot: str) -> str | None:
    """A streamed slice for *ws*: what its stream redactor for the slot lets out.

    None when *ws* already holds :data:`_MAX_STREAMS_PER_SOCKET` streams: a slice of
    one more is not sent, and its slot's saved rows show it at ``chat_done``, so no
    redactor that holds a credential's head is ever dropped.
    """
    from kiro_crew.security import StreamRedactor

    shown = _in_full(_redact_values, frame)
    streams = _socket_streams(ws)
    if streams is not None:
        stream = streams.get((kind, slot))
        if stream is None:
            if len(streams) >= _MAX_STREAMS_PER_SOCKET:
                return None
            stream = streams[(kind, slot)] = StreamRedactor()
        shown["data"]["content"] = _in_full(stream.feed, frame["data"]["content"])
    return json.dumps(shown)


def _non_owner_text(ws: Any, text: str, off: bool) -> str | None:
    if not off:
        # On: a streamed slice goes as the turn's own redactor let it out, or through
        # the socket's stream redactor for a stream that began while the setting was
        # off, so a credential that straddles the change is read whole. Every other
        # frame is redacted in full, as while off: one built from what was read while
        # the setting was off (a cached subagent task, say) may still hold it.
        streamed = _on_streamed(ws, text)
        if streamed is not _NOT_STREAMED:
            return streamed
    hit = _frames.get(text)
    if hit is not None and _frames_generation == redaction_seams.generation():
        shown, ends = hit
        if ends:
            _end_streams(ws, ends)
        return shown
    try:
        frame = json.loads(text)
    except ValueError:
        from kiro_crew.security import redact

        return _remember(text, _in_full(redact, text), "")
    ends = ""
    data = frame.get("data") if isinstance(frame, dict) else None
    if isinstance(data, dict) and isinstance(data.get("slot"), str) and data["slot"]:
        kind, slot = frame.get("type"), data["slot"]
        if kind in _STREAMED_FRAMES and isinstance(data.get("content"), str):
            return _streamed_frame(ws, frame, kind, slot)
        if kind == _STREAM_END_FRAME:
            _end_streams(ws, slot)
            ends = slot
    redacted = _in_full(_redact_values, frame)
    # A frame the pass leaves as it was keeps its own bytes.
    return _remember(text, text if redacted == frame else json.dumps(redacted), ends)


#: What :func:`_on_streamed` answers for a frame that is not a streamed slice.
_NOT_STREAMED: Any = object()


def _on_streamed(ws: Any, text: str) -> Any:
    """A streamed slice for a non-owner socket while the setting is on, or
    :data:`_NOT_STREAMED` for any other frame (a ``chat_done`` ends the slot's streams).

    A slice of a stream that began while the setting was off goes on through the
    socket's redactor for it, which read its head, until the slot's ``chat_done``. A
    slice of a stream the turn started with the setting on goes as it is: the turn's
    own redactor held back header and value together, and a change to off then
    releases them in one slice.
    """
    if not any(f'"{kind}"' in text for kind in (*_STREAMED_FRAMES, _STREAM_END_FRAME)):
        return _NOT_STREAMED
    try:
        frame = json.loads(text)
    except ValueError:
        return _NOT_STREAMED
    data = frame.get("data") if isinstance(frame, dict) else None
    if not (isinstance(data, dict) and isinstance(data.get("slot"), str) and data["slot"]):
        return _NOT_STREAMED
    kind = frame.get("type")
    if kind in _STREAMED_FRAMES and isinstance(data.get("content"), str):
        if (kind, data["slot"]) in (_socket_streams(ws) or {}):
            return _streamed_frame(ws, frame, kind, data["slot"])
        return text
    if kind == _STREAM_END_FRAME:
        _end_streams(ws, data["slot"])
    return _NOT_STREAMED


def socket_reads_as_owner(ws: Any, owner_sockets: Any) -> bool:
    """Whether *ws* reads as the owner for redaction: an owner socket no teammate opened.

    A Shared Crew teammate's socket is an owner socket, so every owner frame reaches it,
    but it carries ``teammate_access.TEAMMATE_WS_FLAG`` and reads as any non-owner.
    """
    from kiro_crew.shared_crew.teammate_access import TEAMMATE_WS_FLAG

    if ws not in owner_sockets:
        return False
    getter = getattr(ws, "get", None)
    return not (callable(getter) and getter(TEAMMATE_WS_FLAG, False) is True)


def for_socket(ws: Any, text: str, *, owner: bool) -> str | None:
    """The frame *text* as socket *ws* may receive it, or None to send it nothing.

    *owner*: whether *ws* is an owner socket (``DashboardState._owner_ws_clients``,
    filled from ``is_owner_dashboard_request`` at connect). An owner socket gets *text*
    as it is. Any other socket gets the text of a streamed slice
    (:data:`_STREAMED_FRAMES`) through its own stream redactor for the slot while the
    setting is off, so a credential split across two slices is caught whole; a stream
    that began while it was off stays on that redactor until the slot's ``chat_done``
    whatever the setting reads, so turning it on mid-reply cannot release a credential
    whose head streamed while it was off (:func:`_on_streamed`). While the setting is off,
    every other frame reaches such a socket with every string value redacted in full;
    while it is on, as it is. A frame that cannot be redacted is not sent. Asked for
    every non-owner frame whatever the setting reads (the hub's chokepoint does).
    """
    if owner:
        return text
    try:
        return _non_owner_text(ws, text, redaction_seams.published_off())
    except Exception:
        logger.warning("a frame for a non-owner socket could not be redacted; not sent")
        return None


def for_reader(text: str, *, owner: bool) -> str | None:
    """The data *text* of one status-stream event as its reader may receive it, or
    None to send it nothing.

    The status stream (``/api/stream``) fans one note out to every reader, the
    owner's own tab and any other (a Shared Crew teammate, an app token), and the
    note was built inside the owner's seam, so with the setting off it may hold what
    only the owner may read. An owner reader gets *text* as it is; any other gets
    every string value of the JSON document redacted in full whatever the setting
    reads (text that is not JSON is redacted whole). A text that cannot be redacted
    is not sent.
    """
    if owner:
        return text
    try:
        try:
            value = json.loads(text)
        except ValueError:
            from kiro_crew.security import redact

            return _in_full(redact, text)
        return json.dumps(_in_full(_redact_values, value))
    except Exception:
        logger.warning("a status event for a non-owner reader could not be redacted; not sent")
        return None


# ── chat SSE events ──


class SseEvents:
    """The chat SSE events one request may read.

    As built for a request the owner alone reads (:func:`request_audience`), decided
    once per request; for any other, while the setting is off, every string value of
    each event redacted in full. A ``chunk`` event is one slice of the streamed reply,
    so their text runs through one stream redactor for the request; a run of chunks
    that began while the setting was off stays on it when the setting is turned on, as
    a non-owner socket's slices do (:func:`_on_streamed`), so the change cannot release a
    credential whose head streamed while it was off. What it holds back goes out as one
    more ``chunk`` event before the next other event, and at :meth:`close`.
    """

    def __init__(self, request: web.Request) -> None:
        self._owner = redaction_seams.owner_alone(request_audience(request))
        self._stream: Any = None
        self._last_chunk: dict[str, Any] | None = None

    def events(self, event: str) -> list[str]:
        """The events to write for the built event *event*, in order."""
        if self._owner:
            return [event]
        off = redaction_seams.published_off()
        if not off and (self._stream is None or '"chunk"' not in event):
            return [*self.close(), event]
        try:
            decoded = json.loads(event)
            if isinstance(decoded, dict) and decoded.get("type") == "chunk":
                if isinstance(decoded.get("content"), str):
                    return self._chunk(decoded)
            if not off:
                return [*self.close(), event]
            return [*self.close(), json.dumps(_in_full(_redact_values, decoded))]
        except Exception:
            logger.warning("a chat SSE event for a non-owner could not be redacted; not sent")
            return []

    def _chunk(self, event: dict[str, Any]) -> list[str]:
        from kiro_crew.security import StreamRedactor

        if self._stream is None:
            self._stream = StreamRedactor()
        self._last_chunk = event
        content = _in_full(self._stream.feed, event["content"])
        if not content:
            return []
        shown = _in_full(_redact_values, event)
        shown["content"] = content
        return [json.dumps(shown)]

    def done(self) -> bytes:
        """The end of the stream: every held event, then ``[DONE]``."""
        return (
            b"".join(f"data: {event}\n\n".encode() for event in self.close()) + b"data: [DONE]\n\n"
        )

    def close(self) -> list[str]:
        """What the chunk stream still holds, as one ``chunk`` event; ``[]`` when nothing."""
        stream, last, self._stream = self._stream, self._last_chunk, None
        if stream is None or last is None:
            return []
        try:
            tail = _in_full(stream.flush)
            if not tail:
                return []
            shown = _in_full(_redact_values, last)
        except Exception:
            logger.warning("a chat SSE event for a non-owner could not be redacted; not sent")
            return []
        shown["content"] = tail
        return [json.dumps(shown)]
