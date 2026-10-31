"""The live stream: one ``Page.startScreencast`` on a relay-owned session, paced
by the card and adapted to the link.

Shape, from the measured relay spike (RFC "Live browser in chat", section 3):

* **Source.** ``Page.startScreencast`` (JPEG) on a flat session the relay owns, so
  no frame ever reaches Harness's shared event buffer.
* **Pacing.** Chrome is acked only after the card has acked the frame's ``seq``
  (or :data:`CARD_ACK_TIMEOUT_S` passed), on a fixed time grid, so transit time
  counts toward the frame interval instead of adding to it.
* **Ladder.** Once a second :class:`AdaptiveController` picks a frame rate
  (:data:`FPS_STEPS`) and a picture (:data:`PICTURES`) as two separate axes. A
  slow link costs frame rate first: fps drops at once when more than 20% of
  frames arrive late or the link cannot carry it, and that is pacing only, with
  no restart. The picture drops only when, at the lowest fps, one frame has taken
  over :data:`PICTURE_DOWN_DELIVERY_S` to land for :data:`PICTURE_DOWN_WINDOWS`
  windows in a row, and climbs back one step after :data:`PICTURE_UP_CALM`
  windows with room; a picture change restarts the screencast on the same
  session. A window with no frame (a still page) is no evidence and
  changes nothing.
* **Link memory.** A stream starts where the previous stream of the same card on
  the same relay left off (a new turn, a tab switch), within
  :data:`LINK_MEMORY_S`, so a slow link is not re-learned on every step.
* **Settle frame.** When no frame has come for :data:`SETTLE_S`, the screencast is
  restarted once at :data:`SETTLE_QUALITY` and the sharpest picture's width;
  Chrome answers with one frame of the still page, masked like every frame.
  The next real change goes back to the ladder's picture. Not during takeover
  input, and not with a fixed ``medium`` or ``low`` quality.
* **Off screen, off.** :meth:`LiveStream.set_visible` stops the screencast while
  the card is hidden and starts it again when it shows.
* **Watchdog.** Input sent with no frame for :data:`WATCHDOG_S` restarts the
  screencast (a Windows headless run stalled one tab at 0-2 fps in the spike).
* **Password mask.** Before a frame is sent the page is asked, at most every
  :data:`MASK_MIN_INTERVAL_S`, for the boxes of visible password fields; the card
  paints over them. Off when ``browser.live.mask_passwords`` is false.

Every clock read and every sleep goes through the two callables the constructor
takes, so the ladder, the pacing and the watchdog run against a fake clock in
tests with no real waiting (Determinism contract D2/D3). Nothing here imports the
dashboard: the frame leaves through the injected :class:`FrameSink`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import struct
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from kiro_crew.browser_harness.frames import FrameOps, FrameRegistry, enable_auto_attach

logger = logging.getLogger(__name__)

#: ``type`` of the JSON header that precedes every binary frame on the owner websocket.
FRAME_HEADER_TYPE = "browser_frame"

#: Big-endian header-length prefix: ``>I``, four bytes.
_HEADER_LEN = struct.Struct(">I")

#: Longest JSON header the decoder accepts; a real one is under 200 bytes.
MAX_HEADER_BYTES = 64 * 1024


@dataclass(frozen=True)
class Rung:
    """One step of the ladder: frames per second, JPEG quality, frame width."""

    fps: int
    quality: int
    width: int

    @property
    def period(self) -> float:
        return 1.0 / self.fps

    @property
    def height(self) -> int:
        """The matching ``maxHeight``: a 5:3 box, as the spike measured."""
        return int(self.width * 0.6)


@dataclass(frozen=True)
class Picture:
    """JPEG quality and frame width: what the person sees, apart from motion."""

    quality: int
    width: int


#: Frame rates, fastest first. Changing between them is pacing only.
FPS_STEPS: tuple[int, ...] = (60, 45, 30, 20, 15, 10, 5)

#: Pictures, sharpest first. Changing between them restarts the screencast.
PICTURES: tuple[Picture, ...] = (
    Picture(60, 1280),
    Picture(45, 960),
    Picture(40, 800),
    Picture(30, 640),
)

#: Every (picture, fps) pair as one index, ``picture * len(FPS_STEPS) + fps``:
#: the ``rung`` the hub reports. Index 0 is 60 fps at the sharpest picture.
LADDER: tuple[Rung, ...] = tuple(
    Rung(fps, pic.quality, pic.width) for pic in PICTURES for fps in FPS_STEPS
)


def rung_index(picture: int, fps_step: int) -> int:
    """The :data:`LADDER` index of a picture and a frame-rate step."""
    return picture * len(FPS_STEPS) + fps_step


#: The frame-rate step of 15 fps: the stream's ceiling during takeover.
TAKEOVER_FPS_STEP = FPS_STEPS.index(15)
#: The rung a takeover lands on from the top: 15 fps, picture unchanged.
TAKEOVER_RUNG = rung_index(0, TAKEOVER_FPS_STEP)

#: Seconds without a frame after an input before the screencast is restarted.
WATCHDOG_S = 3.0
#: Longest wait for the card's ack before the frame counts as late and Chrome is
#: acked anyway, so a card that stopped acking slows the stream instead of
#: freezing it.
CARD_ACK_TIMEOUT_S = 2.0
#: Shortest interval between two password-box queries to the page.
MASK_MIN_INTERVAL_S = 0.25
#: A frame is late when it reaches the card after this many periods.
LATE_PERIODS = 1.5
#: Share of late frames in one window that steps the ladder down.
LATE_SHARE_DOWN = 0.2
#: Calm windows (no step-down trigger) before the frame rate may step up.
CALM_WINDOWS_UP = 2
#: Seconds one frame may take to reach the card at the lowest fps before the
#: picture counts as too heavy for the link (under 2 frames a second).
PICTURE_DOWN_DELIVERY_S = 0.5
#: Windows in a row with too heavy a picture before it drops a step.
PICTURE_DOWN_WINDOWS = 2
#: Windows in a row with room for the sharper picture before it climbs a step.
PICTURE_UP_CALM = 5
#: Seconds a card's last ladder position is kept for its next stream.
LINK_MEMORY_S = 600.0
#: Seconds with no frame before the settle frame is asked for.
SETTLE_S = 0.4
#: JPEG quality of the settle frame.
SETTLE_QUALITY = 85
#: Shortest interval between two settle frames: a page that changes every half
#: second (a blinking text cursor) gets one sharp frame per interval, not per blink.
SETTLE_MIN_INTERVAL_S = 2.0
#: Seconds after the settle frame lands in which another frame still counts as
#: part of the settle restart rather than as a page change.
SETTLE_GRACE_S = 0.25
#: Interval of the control loop: the settle check runs on every step, the ladder
#: tick every ``TICK_S``.
CONTROL_STEP_S = 0.2
#: Headroom the measured link must have over a higher rung's need to climb to it.
UP_HEADROOM = 1.3
#: A rung is kept only while its need stays under this share of the link rate.
DOWN_UTILISATION = 0.9
#: A step-down lands on the first rung whose estimate fits this share of the link.
DOWN_TARGET_UTILISATION = 0.8
#: Seconds between two controller ticks.
TICK_S = 1.0

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class LiveConfig:
    """The ``browser.live.*`` settings the stream reads, with the RFC defaults.

    Built from a loaded config through :meth:`from_config`; every read is a
    ``getattr`` with a default, so the stream works before the settings section
    exists and after a key is removed.
    """

    enabled: bool = True
    quality: str = "auto"
    max_fps: int = 60
    mask_passwords: bool = True

    @classmethod
    def from_config(cls, cfg: Any) -> "LiveConfig":
        browser = getattr(cfg, "browser", None)
        live = getattr(browser, "live", None)
        if live is None:
            return cls()
        max_fps = getattr(live, "max_fps", 60)
        quality = str(getattr(live, "quality", "auto") or "auto")
        return cls(
            enabled=bool(getattr(live, "enabled", True)),
            quality=quality if quality in _QUALITY_FLOORS else "auto",
            max_fps=int(max_fps) if max_fps in (15, 30, 60) else 60,
            mask_passwords=bool(getattr(live, "mask_passwords", True)),
        )

    def fps_floor(self) -> int:
        """The fastest :data:`FPS_STEPS` step ``max_fps`` admits."""
        return next(i for i, fps in enumerate(FPS_STEPS) if fps <= self.max_fps)

    def picture_floor(self) -> int:
        """The sharpest :data:`PICTURES` step ``quality`` admits."""
        return _QUALITY_FLOORS[self.quality]

    @property
    def settles(self) -> bool:
        """Whether a still page gets the sharp settle frame (``auto`` and ``high``)."""
        return self.picture_floor() == 0

    def best_rung(self) -> int:
        """The highest rung this config admits: the ``max_fps`` and ``quality`` floors."""
        return rung_index(self.picture_floor(), self.fps_floor())


#: ``browser.live.quality`` as the picture a fixed quality pins the ladder's top to.
_QUALITY_FLOORS: dict[str, int] = {"auto": 0, "high": 0, "medium": 1, "low": 2}


def encode_frame(header: Mapping[str, Any], jpeg: bytes) -> bytes:
    """The owner-websocket binary message: 4-byte header length, JSON header, JPEG."""
    head = json.dumps(dict(header), separators=(",", ":")).encode("utf-8")
    return _HEADER_LEN.pack(len(head)) + head + jpeg


def decode_frame(blob: bytes) -> tuple[dict[str, Any], bytes]:
    """Split a binary frame message back into its header and JPEG bytes.

    Raises ``ValueError`` on a short, oversized or non-object header: the card's
    decoder and the tests share this one definition of the wire shape.
    """
    if len(blob) < _HEADER_LEN.size:
        raise ValueError("frame shorter than its length prefix")
    (length,) = _HEADER_LEN.unpack_from(blob, 0)
    if length > MAX_HEADER_BYTES or len(blob) < _HEADER_LEN.size + length:
        raise ValueError("frame header length out of range")
    start = _HEADER_LEN.size
    header = json.loads(blob[start : start + length].decode("utf-8"))
    if not isinstance(header, dict):
        raise ValueError("frame header is not a JSON object")
    return header, blob[start + length :]


def frame_header(
    session_key: str,
    seq: int,
    width: int,
    height: int,
    mask: list[list[int]],
    stream: str = "",
) -> dict[str, Any]:
    """The header the card reads, with exactly the Wire-contract fields.

    ``stream`` names the :class:`LiveStream` the frame came from. ``seq`` restarts
    at 1 with every stream (a new turn, a tab switch, a gateway restart), so the
    card scopes its once-per-frame ack to ``(stream, seq)`` and never holds a new
    stream's frames behind an old stream's high-water mark.
    """
    return {
        "type": FRAME_HEADER_TYPE,
        "session_key": session_key,
        "stream": stream,
        "seq": seq,
        "w": width,
        "h": height,
        "mask": mask,
    }


def clip_mask_rects(
    boxes: Any, scale: float, width: int, height: int, *, limit: int = 32
) -> list[list[int]]:
    """CSS-pixel boxes -> whole frame pixels, clipped to the frame, empties dropped.

    The card's decoder refuses a frame whose mask reaches past its edge, so a box
    that overhangs (a full-width field plus the one-pixel rounding guard) must be
    cut at the frame, never sent as is. Every visible field pixel stays covered:
    the origin is floored and the far edge is ceiled before the clip.
    """
    rects: list[list[int]] = []
    if not isinstance(boxes, list) or width <= 0 or height <= 0:
        return rects
    for box in boxes[:limit]:
        if not (isinstance(box, list) and len(box) == 4):
            continue
        try:
            x, y, w, h = (float(v) for v in box)
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        left = max(0, int(x * scale))
        top = max(0, int(y * scale))
        right = min(width, int((x + w) * scale + 1))
        bottom = min(height, int((y + h) * scale + 1))
        if right <= left or bottom <= top:
            continue
        rects.append([left, top, right - left, bottom - top])
    return rects


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a JPEG's first frame header, or ``None``.

    Walks the marker segments to the first SOF marker (``FFC0``..``FFCF`` minus the
    DHT/JPG/DAC markers) and reads its height and width. No decoder, no PIL.
    """
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return None
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if pos + 4 > len(data):
            return None
        (length,) = struct.unpack_from(">H", data, pos + 2)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if pos + 9 > len(data):
                return None
            height, width = struct.unpack_from(">HH", data, pos + 5)
            return width, height
        pos += 2 + length
    return None


@dataclass
class _Window:
    """One controller window's measurements, reset every tick."""

    frames: int = 0
    bytes: int = 0
    late: int = 0
    delivery_s: float = 0.0


@dataclass
class TickStats:
    """What one controller tick measured and decided; the hub reads it for the card."""

    rung: int
    fps: float
    frames: int
    late_share: float
    kb_per_frame: float
    restarted: bool = False


class AdaptiveController:
    """The pure ladder decision: measurements in, frame rate and picture out. No I/O, no clock.

    Two axes. ``fps_step`` indexes :data:`FPS_STEPS` and moves freely, since it is
    pacing only. ``picture`` indexes :data:`PICTURES` and moves rarely, since a
    change restarts the screencast and the person sees the picture go soft or
    sharp (see :meth:`_pick_picture`). ``rung`` folds the two into one
    :data:`LADDER` index for the hub.
    """

    def __init__(self, *, fps_floor: int = 0, picture_floor: int = 0) -> None:
        self.fps_floor = max(0, min(fps_floor, len(FPS_STEPS) - 1))
        self.picture_floor = max(0, min(picture_floor, len(PICTURES) - 1))
        self.fps_step = self.fps_floor
        self.picture = self.picture_floor
        self.calm = 0
        self.squeezed = 0
        self.roomy = 0
        self.window = _Window()

    @property
    def rung(self) -> int:
        return rung_index(self.picture, self.fps_step)

    def observe(self, nbytes: int, delivery_s: float) -> None:
        """Record one frame: its size and the time from send to the card's ack."""
        w = self.window
        w.frames += 1
        w.bytes += nbytes
        w.delivery_s += delivery_s
        if delivery_s > LATE_PERIODS * LADDER[self.rung].period:
            w.late += 1

    def set_floors(self, *, fps_floor: int | None = None, picture_floor: int | None = None) -> int:
        """Move the fastest fps and the sharpest picture in play; the position is clamped."""
        if fps_floor is not None:
            self.fps_floor = max(0, min(fps_floor, len(FPS_STEPS) - 1))
            self.fps_step = max(self.fps_step, self.fps_floor)
        if picture_floor is not None:
            self.picture_floor = max(0, min(picture_floor, len(PICTURES) - 1))
            self.picture = max(self.picture, self.picture_floor)
        return self.rung

    def restore(self, picture: int, fps_step: int) -> int:
        """Start from a remembered position, clamped into the floors."""
        self.picture = max(self.picture_floor, min(picture, len(PICTURES) - 1))
        self.fps_step = max(self.fps_floor, min(fps_step, len(FPS_STEPS) - 1))
        return self.rung

    def tick(self) -> TickStats:
        """Close the window and pick the next position. Returns what it measured."""
        w, self.window = self.window, _Window()
        n = max(w.frames, 1)
        per_frame = w.bytes / n
        late_share = w.late / n if w.frames else 0.0
        stats = TickStats(
            rung=self.rung,
            fps=float(w.frames) / TICK_S,
            frames=w.frames,
            late_share=round(late_share, 3),
            kb_per_frame=round(per_frame / 1024, 1),
        )
        if not w.frames:
            # A still page sends nothing: no evidence about the link either way.
            return stats
        # Link rate: bytes the card took divided by the time it took to take them.
        # A window with no measurable delivery time is a link faster than we can see.
        link = w.bytes / w.delivery_s if w.delivery_s > 1e-3 and w.bytes else float("inf")
        cur = PICTURES[self.picture]

        def need(picture: int, fps_step: int) -> float:
            p = PICTURES[picture]
            scale = (p.quality / cur.quality) * (p.width / cur.width) ** 2
            return per_frame * scale * FPS_STEPS[fps_step]

        bottom = len(FPS_STEPS) - 1
        if (
            late_share > LATE_SHARE_DOWN
            or need(self.picture, self.fps_step) > link * DOWN_UTILISATION
        ):
            self.calm = 0
            # Frame rate first: the slowest step still needed, at least one down.
            target = min(self.fps_step + 1, bottom)
            while target < bottom and need(self.picture, target) > link * DOWN_TARGET_UTILISATION:
                target += 1
            self.fps_step = max(target, self.fps_floor)
        else:
            self.calm += 1
            if self.calm >= CALM_WINDOWS_UP:
                best = self.fps_step
                for step in range(self.fps_step - 1, self.fps_floor - 1, -1):
                    if need(self.picture, step) * UP_HEADROOM <= link:
                        best = step
                    else:
                        break
                self.fps_step = best
        self._pick_picture(w.delivery_s / w.frames, at_bottom=self.fps_step == bottom)
        stats.rung = self.rung
        return stats

    def _pick_picture(self, delivery: float, *, at_bottom: bool) -> None:
        """The picture axis: judged on how long one frame takes to land.

        Frames are acked one at a time, so a high round trip alone only slows
        the frame rate; a smaller picture would barely help it. The picture gives
        way only when one frame takes longer than :data:`PICTURE_DOWN_DELIVERY_S`
        at the lowest fps, :data:`PICTURE_DOWN_WINDOWS` windows in a row, and
        climbs one step after :data:`PICTURE_UP_CALM` windows in a row in which
        the sharper picture's frame (scaled by its size) would land in time with
        :data:`UP_HEADROOM` to spare.
        """
        cur = PICTURES[self.picture]

        def size_ratio(picture: int) -> float:
            p = PICTURES[picture]
            return (p.quality / cur.quality) * (p.width / cur.width) ** 2

        if at_bottom and delivery > PICTURE_DOWN_DELIVERY_S:
            self.roomy = 0
            self.squeezed += 1
            if self.squeezed >= PICTURE_DOWN_WINDOWS and self.picture < len(PICTURES) - 1:
                self.squeezed = 0
                pic = self.picture + 1
                while (
                    pic < len(PICTURES) - 1 and delivery * size_ratio(pic) > PICTURE_DOWN_DELIVERY_S
                ):
                    pic += 1
                self.picture = pic
            return
        self.squeezed = 0
        if (
            self.picture > self.picture_floor
            and delivery * size_ratio(self.picture - 1) * UP_HEADROOM <= PICTURE_DOWN_DELIVERY_S
        ):
            self.roomy += 1
            if self.roomy >= PICTURE_UP_CALM:
                self.roomy = 0
                self.picture -= 1
        else:
            self.roomy = 0


class LinkMemory:
    """Where each card's ladder stood when its last stream ticked or stopped.

    Keyed on the relay (weakly: a closed browser drops its entries) and then on
    the card's session key, so a new turn or a tab switch on the same card
    starts from the measured link instead of the top rung.
    """

    def __init__(self) -> None:
        self._by_relay: weakref.WeakKeyDictionary[Any, dict[str, tuple[float, int, int]]]
        self._by_relay = weakref.WeakKeyDictionary()

    def remember(self, relay: Any, key: str, at: float, picture: int, fps_step: int) -> None:
        if not key:
            return
        try:
            self._by_relay.setdefault(relay, {})[key] = (at, picture, fps_step)
        except TypeError:
            # A relay that cannot be weakly referenced: no memory, as before.
            return

    def recall(self, relay: Any, key: str, now: float) -> tuple[int, int] | None:
        try:
            entry = self._by_relay.get(relay, {}).get(key)
        except TypeError:
            return None
        if entry is None or now - entry[0] > LINK_MEMORY_S or now < entry[0]:
            return None
        return entry[1], entry[2]


#: The process-wide memory a stream uses unless it is handed its own.
LINK_MEMORY = LinkMemory()


class FrameSink(Protocol):
    """Where a finished frame goes: the hub, which sends it to owner websockets.

    A sink MAY also record (the hub's replay recorder): it then has a ``records``
    attribute that reads true, and a ``record(jpeg, width, height, boxes, mask_ok)``
    method the stream calls, synchronously and before the card's frame, with the
    password boxes of THAT frame in frame pixels and whether the box query that
    produced them succeeded. A sink without them is a plain card sink.
    """

    async def frame(self, header: Mapping[str, Any], blob: bytes) -> None: ...


#: Shortest interval between two Chrome acks while the card is hidden and the stream
#: casts only for the recorder: the recorder's own frame-rate ceiling (5 fps).
HIDDEN_RECORD_PERIOD_S = 0.2


class Relay(Protocol):
    """The slice of ``CdpRelay`` this module uses (CONTRACT ``relay.py``)."""

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]: ...

    async def attach(
        self, target_id: str, on_event: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> str: ...

    async def detach(self, session_id: str) -> None: ...


#: Boxes of visible password fields, in CSS pixels of the viewport. Read-only,
#: no page state touched; ``returnByValue`` keeps the reply a plain array. Open
#: shadow roots are walked too (a web-component login form keeps its password
#: input inside one, where ``document.querySelectorAll`` does not look), to the
#: same depth the snapshot walker descends; a closed root cannot be entered.
#:
#: A secret field is ``input[type=password]``, an input whose ``autocomplete``
#: names a password or a one-time code (a "show password" toggle turns the type
#: to ``text`` and usually keeps that), one styled with ``-webkit-text-security``,
#: or one the person filled through a quick ask (``data-kc-asked``, quick_ask.py).
#: The reply is ``{"boxes": [...], "frames": [...], "partial": bool}``.
#: Same-origin iframes are walked too, their boxes offset by the iframe's content
#: box; ``frames`` holds the boxes of visible embedded documents that cannot be
#: read or placed exactly (cross-origin, a scaled or transformed iframe, any
#: ``object``/``embed``/``fencedframe``/``portal``), which the card leaves alone and
#: the turn replay paints over. ``partial`` is set when nesting runs past the depth
#: cap, and the replay treats that mask as unknown. ``sx``/``sy`` are the page's
#: scroll offset when the boxes were read: the replay keeps a frame only when they
#: match the offset Chrome stamped on the frame (a scroll between the capture and
#: the query would put every box in the wrong place, or off the frame).
_MASK_JS = (
    "(()=>{const r=[],f=[];let partial=false;"
    "const SEL='input[type=password],input[autocomplete~=current-password],"
    "input[autocomplete~=new-password],input[autocomplete~=one-time-code],input[data-kc-asked]';"
    "const OPAQUE={OBJECT:1,EMBED:1,FENCEDFRAME:1,PORTAL:1};"
    "const vis=b=>b.width>0&&b.height>0;"
    "const plain=e=>{for(let n=e;n;n=n.parentElement||(n.getRootNode&&n.getRootNode().host)||null){"
    "const c=getComputedStyle(n);"
    "if((c.rotate&&c.rotate!=='none')||(c.scale&&c.scale!=='none'))return false;"
    "const t=c.transform;if(t&&t!=='none'){"
    "if(t.indexOf('matrix(')!==0)return false;"
    "const v=t.slice(7,-1).split(',').map(Number);"
    "if(Math.abs(v[0]-1)>.001||Math.abs(v[1])>.001||Math.abs(v[2])>.001||Math.abs(v[3]-1)>.001)return false;}}"
    "return true;};"
    "const box=(b,ox,oy)=>[b.left+ox,b.top+oy,b.width,b.height];"
    "const walk=(root,d,ox,oy)=>{"
    "for(const e of root.querySelectorAll('input')){let s=e.matches(SEL);"
    "if(!s){const t=getComputedStyle(e).webkitTextSecurity;s=!!t&&t!=='none';}"
    "if(s){const b=e.getBoundingClientRect();if(vis(b))r.push(box(b,ox,oy));}}"
    "for(const h of root.querySelectorAll('*')){const tag=h.tagName;"
    "const isF=tag==='IFRAME'||tag==='FRAME';"
    "if(!h.shadowRoot&&!isF&&!OPAQUE[tag])continue;"
    "if(d>=8){partial=true;continue;}"
    "if(h.shadowRoot)walk(h.shadowRoot,d+1,ox,oy);"
    "if(!isF&&!OPAQUE[tag])continue;const b=h.getBoundingClientRect();"
    "if(!vis(b))continue;let doc=null;"
    "if(isF){try{doc=h.contentDocument;}catch(_){doc=null;}}"
    "const scaled=!plain(h)||Math.abs(b.width-h.offsetWidth)>1||Math.abs(b.height-h.offsetHeight)>1;"
    "if(doc&&!scaled){const cs=getComputedStyle(h);"
    "walk(doc,d+1,ox+b.left+h.clientLeft+(parseFloat(cs.paddingLeft)||0),"
    "oy+b.top+h.clientTop+(parseFloat(cs.paddingTop)||0));}"
    "else{f.push(box(b,ox,oy));}}};"
    "walk(document,0,0,0);return {boxes:r,frames:f,partial:partial,sx:scrollX,sy:scrollY};})()"
)


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e9


#: Most boxes one query may report; more than this and the mask is unknown.
MASK_BOX_LIMIT = 32
#: How far, in CSS pixels, the page's scroll at the box query may sit from the
#: scroll Chrome stamped on the frame and still describe that frame.
MASK_SCROLL_TOLERANCE_PX = 1.0


def frame_scroll(meta: Mapping[str, Any]) -> tuple[float, float] | None:
    """The page's scroll offset (CSS px) when Chrome captured the frame, or ``None``."""
    try:
        x, y = meta.get("scrollOffsetX"), meta.get("scrollOffsetY")
    except AttributeError:
        return None
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    return float(x), float(y)


def _same_scroll(value: Any, scroll: tuple[float, float] | None) -> bool:
    """The box query read the page at the frame's scroll (no stamp: nothing to check)."""
    if scroll is None:
        return True
    got = (
        frame_scroll({"scrollOffsetX": value.get("sx"), "scrollOffsetY": value.get("sy")})
        if isinstance(value, dict)
        else None
    )
    if got is None:
        return False
    return (
        abs(got[0] - scroll[0]) <= MASK_SCROLL_TOLERANCE_PX
        and abs(got[1] - scroll[1]) <= MASK_SCROLL_TOLERANCE_PX
    )


#: Oldest a held frame may be when the tick offers it to the recorder.
HELD_FRAME_MAX_AGE_S = 1.5


@dataclass
class _Metrics:
    frames: int = 0
    bytes: int = 0
    seq: int = 0
    last_frame_at: float = field(default=-1.0)
    restarts: int = 0
    settles: int = 0


class LiveStream:
    """One screencast on one agent target, delivered to one card.

    ``relay``, ``target_id`` and ``sink`` are the contract; ``session_key`` names
    the card. ``clock`` and ``sleep`` default to the real monotonic clock and
    ``asyncio.sleep`` and are the only time the class reads. With
    ``auto_control=False`` the once-a-second tick and the settle check are the
    caller's to drive (tests, or a hub that ticks every stream from one timer).
    ``link_memory`` defaults to the process-wide :data:`LINK_MEMORY`.
    """

    def __init__(
        self,
        relay: Relay,
        target_id: str,
        sink: FrameSink,
        *,
        session_key: str = "",
        config: LiveConfig | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        auto_control: bool = True,
        on_tick: Callable[[TickStats], Awaitable[None]] | None = None,
        link_memory: LinkMemory | None = None,
    ) -> None:
        self.relay = relay
        self.target_id = target_id
        self.sink = sink
        self.session_key = session_key
        self.config = config or LiveConfig()
        self._clock = clock
        self._sleep = sleep
        self._auto_control = auto_control
        self._on_tick = on_tick
        self.controller = AdaptiveController(
            fps_floor=self.config.fps_floor(), picture_floor=self.config.picture_floor()
        )
        self._memory = LINK_MEMORY if link_memory is None else link_memory
        remembered = self._memory.recall(relay, session_key, clock())
        if remembered is not None:
            self.controller.restore(*remembered)
        self.session_id = ""
        #: The tab's cross-origin iframes, each on a child session of ours
        #: (``browser_harness.frames``): the mask, the snapshot and quick asks read them.
        self.frames = FrameRegistry()
        #: Names this stream on every frame header; the card scopes acks to it.
        self.stream_id = secrets.token_hex(4)
        #: Frame pixels per viewport CSS pixel of the last frame (1.0 until one lands).
        self.scale = 1.0
        #: The viewport's CSS width from the last frame's metadata (0 until one
        #: lands): takeover input scales by the width of the frame the CARD drew
        #: against this (``takeover.frame_scale``), not by :attr:`scale`. A viewport
        #: resized between the card's frame and the newest one is off until the card
        #: draws a frame of the new viewport, one frame period.
        self.device_width = 0.0
        self.metrics = _Metrics()
        self.running = False
        self.visible = True
        self.casting = False
        self._last_ack_due = -1.0
        self._frame_lock = asyncio.Lock()
        self._acks: dict[int, asyncio.Event] = {}
        self._input_at = -1.0
        self._input_pending = False
        self._takeover = False
        self._mask_at = -1.0
        #: Last password boxes the page reported, in viewport CSS pixels; scaled
        #: per frame so a frame at another width is masked at its own scale.
        self._mask_boxes: list[Any] = []
        #: The recorder's mask for the frame in hand (see :meth:`_mask_rects`): a
        #: frame whose boxes did not come from its own answered query is never kept.
        self._mask_ok = False
        self._rec_boxes: list[list[int]] = []
        #: The latest frame the recorder's 5 fps gate turned away: (jpeg, w, h, at).
        self._held: tuple[bytes, int, int, float, tuple[float, float] | None] | None = None
        #: The recorder's last answered query (boxes, frame size); ``None`` after a
        #: query that did not answer. :meth:`flush_held` compares against it.
        self._rec_last: list[list[int]] | None = None
        self._rec_last_size: tuple[int, int] = (0, 0)
        self._control_task: asyncio.Task[None] | None = None
        #: The screencast runs at the settle picture (one sharp frame of a still page).
        self._settled = False
        #: The settle restart's own frame has not finished landing yet.
        self._settle_landing = False
        #: Until when a frame still belongs to the settle restart.
        self._settle_grace_until = -1.0
        #: ``metrics.seq`` of the last ladder frame a settle already answered.
        self._settled_after = -1
        self._settle_at = -1.0

    @property
    def records(self) -> bool:
        """The sink records frames for replay (read live, so a config change applies)."""
        try:
            return bool(getattr(self.sink, "records", False))
        except Exception:  # noqa: BLE001 - an unreadable flag records nothing
            return False

    @property
    def _casts_hidden(self) -> bool:
        """Keep casting while the card is hidden, for the recorder alone."""
        return not self.visible and self.records

    # ── lifecycle ──────────────────────────────────────────────────────────

    @property
    def rung(self) -> int:
        return self.controller.rung

    async def start(self) -> None:
        """Attach the relay-owned session and start the screencast at the top rung."""
        if self.running:
            return
        self.session_id = await self.relay.attach(self.target_id, self._on_event)
        self.frames.parent = self.session_id
        self.running = True
        await self.relay.send("Page.enable", {}, self.session_id)
        try:
            await enable_auto_attach(self.relay, self.session_id)
        except Exception:  # noqa: BLE001 - the stream runs without frame access
            logger.debug("live stream: frame auto-attach not enabled", exc_info=True)
        if self.visible or self._casts_hidden:
            await self._start_cast()
        if self._auto_control:
            self._control_task = asyncio.ensure_future(self._control_loop())

    async def stop(self) -> None:
        """Stop the screencast, detach the session, release every waiter."""
        if not self.running:
            return
        self.running = False
        self._remember()
        task, self._control_task = self._control_task, None
        if task is not None:
            task.cancel()
        try:
            if self.casting:
                await self._stop_cast()
        except Exception:
            # The tab is gone or mid-navigation: there is no screencast left to
            # stop, and a caller replacing this stream must not lose its new one.
            logger.debug("live stream stopScreencast failed", exc_info=True)
        finally:
            for ev in self._acks.values():
                ev.set()
            self._acks.clear()
            try:
                await self.relay.detach(self.session_id)
            except Exception:
                logger.debug("live stream detach failed", exc_info=True)

    def _cast_picture(self) -> Rung:
        """What the screencast runs at: the ladder's rung, or the settle picture."""
        r = LADDER[self.controller.rung]
        if self._settled:
            return Rung(r.fps, SETTLE_QUALITY, PICTURES[0].width)
        return r

    async def _start_cast(self) -> None:
        r = self._cast_picture()
        await self.relay.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": r.quality,
                "maxWidth": r.width,
                "maxHeight": r.height,
                "everyNthFrame": 1,
            },
            self.session_id,
        )
        self.casting = True

    async def _stop_cast(self) -> None:
        self.casting = False
        await self.relay.send("Page.stopScreencast", {}, self.session_id)

    async def restart(self, *, settle: bool = False) -> None:
        """Stop and start the screencast on the same session.

        For a picture change or the watchdog the cast comes back at the ladder's
        rung; with ``settle`` it comes back at the settle picture.
        """
        if not self.running or not self.visible:
            return
        self.metrics.restarts += 1
        self._settled = settle
        self._settle_landing = settle
        if self.casting:
            await self._stop_cast()
        await self._start_cast()

    def _remember(self) -> None:
        c = self.controller
        self._memory.remember(self.relay, self.session_key, self._clock(), c.picture, c.fps_step)

    # ── card-side signals ──────────────────────────────────────────────────

    async def set_visible(self, visible: bool) -> None:
        """Off screen, off: stop the screencast while the card is hidden.

        Unless the sink records: then the cast keeps running, frames go to the
        recorder only, and Chrome is acked at :data:`HIDDEN_RECORD_PERIOD_S`.
        """
        visible = bool(visible)
        if visible == self.visible:
            return
        self.visible = visible
        if not self.running:
            return
        was_settled = self._settled or self._settle_landing
        self._settled = self._settle_landing = False
        if visible and not self.casting:
            await self._start_cast()
        elif self.casting and was_settled:
            # A recorder-only cast kept running at the settle picture: put it back
            # on the ladder's picture, or it would stay sharp while the flag says not.
            await self._stop_cast()
            if visible or self._casts_hidden:
                await self._start_cast()
        elif self.casting and not visible and not self._casts_hidden:
            await self._stop_cast()

    def ack(self, seq: int) -> bool:
        """The card received frame ``seq``. Returns whether a frame was waiting on it."""
        ev = self._acks.get(seq)
        if ev is None:
            return False
        ev.set()
        return True

    def note_input(self) -> None:
        """An input was dispatched to the page; the watchdog starts counting."""
        self._input_at = self._clock()
        self._input_pending = True

    async def set_takeover(self, active: bool) -> None:
        """Cap the frame rate at 15 fps while the person holds input; the picture stays."""
        self._takeover = bool(active)
        before = self.controller.rung
        fps_floor = self.config.fps_floor()
        if active:
            fps_floor = max(TAKEOVER_FPS_STEP, fps_floor)
        after = self.controller.set_floors(fps_floor=fps_floor)
        if after != before:
            await self._apply_rung(before, after)

    # ── frames ─────────────────────────────────────────────────────────────

    async def _on_event(self, msg: dict[str, Any]) -> None:
        self.frames.handle(msg)
        if msg.get("method") != "Page.screencastFrame" or not self.running:
            return
        if msg.get("sessionId") not in (None, self.session_id):
            return
        params = msg.get("params") or {}
        async with self._frame_lock:
            if not self.running:
                return
            try:
                await self._frame(params)
            except Exception:
                logger.debug("live frame dropped", exc_info=True)

    async def _frame(self, params: dict[str, Any]) -> None:
        data = params.get("data")
        if not isinstance(data, str):
            return
        try:
            jpeg = base64.b64decode(data, validate=True)
        except (ValueError, TypeError):
            return
        now = self._clock()
        self.metrics.last_frame_at = now
        self._input_pending = False
        settle_frame = self._settled
        # Captured at the settle picture even when it ends the settle below, so it
        # is never ladder evidence.
        sharp = settle_frame
        if settle_frame and not self._settle_landing and now > self._settle_grace_until:
            # The page changed after the still: back to the ladder's picture. This
            # frame still goes out (it is current); the restart's frame follows.
            settle_frame = False
            await self.restart()
        meta = params.get("metadata") or {}
        size = jpeg_size(jpeg)
        if size is None:
            size = (int(meta.get("deviceWidth") or 0), int(meta.get("deviceHeight") or 0))
        width, height = size
        self._note_scale(meta, width)
        records = self.records
        if records:
            note = getattr(self.sink, "note_size", None)
            if callable(note):
                # Every frame, not only kept ones: a point placed now uses this scale.
                note(width, height)
        want = records and self._recorder_wants()
        scroll = frame_scroll(meta)
        boxes = await self._mask_rects(width, height, force=want, scroll=scroll)
        mask = boxes if self.config.mask_passwords else []
        if want:
            self._held = None
            self._record(jpeg, width, height, self._rec_boxes, self._mask_ok)
        elif records:
            # Inside the recorder's interval: held, so the last picture of a burst
            # is kept by the next tick (with a query of its own) if nothing follows.
            self._held = (jpeg, width, height, now, scroll)
        if not self.visible:
            # Cast only for the recorder: nothing goes to a card nobody sees, and the
            # ladder learns nothing from a frame that never crossed the link.
            await self._paced_ack(params.get("sessionId"), floor=HIDDEN_RECORD_PERIOD_S)
            return
        self.metrics.seq += 1
        seq = self.metrics.seq
        header = frame_header(self.session_key, seq, width, height, mask, self.stream_id)
        waiter = asyncio.Event()
        self._acks[seq] = waiter
        t0 = self._clock()
        try:
            await self.sink.frame(header, encode_frame(header, jpeg))
            await self._wait_card_ack(waiter)
        finally:
            self._acks.pop(seq, None)
        if not self.running:
            return
        took = self._clock() - t0
        self.metrics.frames += 1
        self.metrics.bytes += len(jpeg)
        if settle_frame:
            # A settle frame is bigger than the ladder's picture by design; it says
            # nothing about whether the rung fits.
            self._settled_after = seq
            if self._settle_landing:
                self._settle_landing = False
                self._settle_grace_until = self._clock() + SETTLE_GRACE_S
        elif not sharp:
            self.controller.observe(len(jpeg), took)
        await self._paced_ack(params.get("sessionId"))

    def _recorder_wants(self) -> bool:
        """The recorder would keep a frame now (its 5 fps gate is open)."""
        wants = getattr(self.sink, "wants_frame", None)
        if not callable(wants):
            return True
        try:
            return bool(wants())
        except Exception:  # noqa: BLE001 - an unreadable gate offers the frame
            return True

    def _record(
        self,
        jpeg: bytes,
        width: int,
        height: int,
        boxes: list[list[int]],
        mask_ok: bool,
        age_s: float = 0.0,
    ) -> None:
        """Hand the frame to the recorder with ITS boxes; never fails the frame."""
        record = getattr(self.sink, "record", None)
        if not callable(record):
            return
        try:
            if age_s > 0:
                record(jpeg, width, height, list(boxes), mask_ok, age_s=age_s)
            else:
                record(jpeg, width, height, list(boxes), mask_ok)
        except Exception:  # noqa: BLE001 - the recorder never costs the live view a frame
            logger.debug("live frame record failed", exc_info=True)

    async def flush_held(self) -> bool:
        """Offer the frame held inside the recorder's interval, if its mask is provable.

        Called from the once-a-second tick. The page may have changed since the frame
        was captured (Chrome sends the next frame only after our ack), so a query run
        now does not by itself describe the held frame. It is offered only when the
        query BEFORE it (the last frame offered) and the query now both answered and
        found the same boxes: the password fields stood still across the frame's
        whole window. Otherwise it is dropped. Returns whether a frame was offered.
        """
        held = self._held
        if held is None or not self.running or not self.records or not self._recorder_wants():
            return False
        async with self._frame_lock:
            if self._held is not held:
                return False
            self._held = None
            jpeg, width, height, at, scroll = held
            before, before_size = self._rec_last, self._rec_last_size
            await self._mask_rects(width, height, force=True, scroll=scroll)
            if not (
                self._mask_ok
                and before is not None
                and before_size == (width, height)
                and before == self._rec_boxes
                and self._clock() - at <= HELD_FRAME_MAX_AGE_S
            ):
                return False
            self._record(jpeg, width, height, self._rec_boxes, True, max(0.0, self._clock() - at))
        return True

    def _note_scale(self, meta: Mapping[str, Any], width: int) -> None:
        """Remember frame-pixels-per-CSS-pixel from the frame's metadata.

        ``Page.startScreencast``'s ``maxWidth`` shrinks the JPEG but not the
        viewport, so a card coordinate is ``viewport * scale``; takeover input
        divides by this before ``Input.dispatchMouseEvent`` takes it as CSS pixels.
        """
        try:
            device_w = float(meta.get("deviceWidth") or 0)
        except (TypeError, ValueError):
            device_w = 0.0
        if device_w > 0 and width > 0:
            self.scale = width / device_w
            self.device_width = device_w

    async def _wait_card_ack(self, waiter: asyncio.Event) -> bool:
        """Wait for the card's ack, bounded by :data:`CARD_ACK_TIMEOUT_S` on the injected sleep."""
        if waiter.is_set():
            return True
        wait = asyncio.ensure_future(waiter.wait())
        timeout = asyncio.ensure_future(self._sleep(CARD_ACK_TIMEOUT_S))
        try:
            await asyncio.wait({wait, timeout}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (wait, timeout):
                if not t.done():
                    t.cancel()
        return waiter.is_set()

    async def _paced_ack(self, chrome_session: Any, *, floor: float = 0.0) -> None:
        """Ack Chrome at the next slot of a fixed grid: target-time pacing.

        *floor* is the shortest period allowed (the hidden recorder-only cast).
        """
        period = max(LADDER[self.controller.rung].period, floor)
        now = self._clock()
        due = max(self._last_ack_due + period, now - period)
        if due > now:
            await self._sleep(due - now)
        self._last_ack_due = due
        if not self.running or not self.casting:
            return
        try:
            await self.relay.send(
                "Page.screencastFrameAck", {"sessionId": chrome_session}, self.session_id
            )
        except Exception:
            # A late ack after a restart or stop is Chrome telling us the frame is
            # gone; the next frame carries its own session id.
            logger.debug("late screencastFrameAck", exc_info=True)

    async def _mask_rects(
        self,
        width: int,
        height: int,
        *,
        force: bool = False,
        scroll: tuple[float, float] | None = None,
    ) -> list[list[int]]:
        """Password boxes in frame pixels, re-read at most every :data:`MASK_MIN_INTERVAL_S`.

        Queried when ``mask_passwords`` is on or *force* is (a recorder is attached:
        it burns the boxes in whatever the card does). *force* also re-reads on
        EVERY frame: the recorder's boxes must belong to the frame they are burned
        into, never to a cached query from before a field appeared, a scroll or a
        ladder step changed the frame's size.

        Sets the recorder's view of this frame: ``_mask_ok`` is true only when the
        query ran for this frame, answered in the expected shape, and reported no
        more than :data:`MASK_BOX_LIMIT` boxes, and, when the frame carries its
        *scroll* offset, the query read the page at that same offset (within
        :data:`MASK_SCROLL_TOLERANCE_PX`); ``_rec_boxes`` are the password boxes
        plus every opaque (cross-origin) iframe's box, in frame pixels.

        The page's boxes are kept in CSS pixels and scaled per frame, so a frame
        at another width (a picture change, the settle frame) is masked at its
        own scale, never at the previous frame's.
        """
        self._mask_ok = False
        self._rec_boxes = []
        if force:
            self._rec_last = None
        if not (self.config.mask_passwords or force):
            return []
        now = self._clock()
        if not force and self._mask_at >= 0 and now - self._mask_at < MASK_MIN_INTERVAL_S:
            return clip_mask_rects(
                self._mask_boxes, self.scale, width, height, limit=MASK_BOX_LIMIT
            )
        self._mask_at = now
        try:
            reply = await self.relay.send(
                "Runtime.evaluate",
                {"expression": _MASK_JS, "returnByValue": True},
                self.session_id,
            )
        except Exception:
            # Keep the last boxes rather than unmask on a transient failure.
            return clip_mask_rects(
                self._mask_boxes, self.scale, width, height, limit=MASK_BOX_LIMIT
            )
        value = ((reply.get("result") or {}).get("value")) if isinstance(reply, dict) else None
        frames: Any = []
        if isinstance(value, dict):
            boxes, frames = value.get("boxes"), value.get("frames", [])
        else:
            boxes = value
        if not isinstance(boxes, list) or not isinstance(frames, list):
            # An exception inside the page, or a reply of another shape: the boxes
            # are unknown, which is not the same as "no password field".
            return clip_mask_rects(
                self._mask_boxes, self.scale, width, height, limit=MASK_BOX_LIMIT
            )
        boxes = boxes + await self._frame_boxes()
        self._mask_boxes = boxes
        rects = clip_mask_rects(boxes, self.scale, width, height, limit=MASK_BOX_LIMIT)
        partial = isinstance(value, dict) and bool(value.get("partial"))
        if (
            len(boxes) + len(frames) <= MASK_BOX_LIMIT
            and not partial
            and _same_scroll(value, scroll)
        ):
            opaque = clip_mask_rects(frames, self.scale, width, height, limit=MASK_BOX_LIMIT)
            self._rec_boxes = rects + opaque
            self._mask_ok = True
            if force:
                self._rec_last = list(self._rec_boxes)
                self._rec_last_size = (width, height)
        return list(rects)

    async def _frame_boxes(self) -> list[Any]:
        """Secret-field boxes inside the tab's cross-origin iframes, in page CSS pixels.

        Each frame is asked with the same query in its own session and its boxes are
        moved by its owner's content box. A frame that cannot be placed exactly adds
        none: its whole box is already among the opaque ``frames`` the replay paints.
        """
        out: list[Any] = []
        place = FrameOps(self.relay, self.session_id, self.frames, "")
        for frame_id, sid in self.frames.frames():
            try:
                reply = await self.relay.send(
                    "Runtime.evaluate", {"expression": _MASK_JS, "returnByValue": True}, sid
                )
            except Exception:  # noqa: BLE001 - the frame went away
                continue
            value = (reply.get("result") or {}).get("value") if isinstance(reply, dict) else None
            boxes = value.get("boxes") if isinstance(value, dict) else None
            if not isinstance(boxes, list) or not boxes:
                continue
            at = await place.owner_origin_box(frame_id, (sid, None))
            if at is None:
                continue
            for b in boxes:
                # The frame's own script answered this: numbers only, or the whole
                # frame is masked instead.
                nums = b if isinstance(b, list) and len(b) == 4 else None
                if nums is None or not all(_finite(v) for v in nums):
                    out.append([at[0], at[1], 100000.0, 100000.0])
                    break
                out.append(
                    [float(nums[0]) + at[0], float(nums[1]) + at[1], float(nums[2]), float(nums[3])]
                )
        return out

    # ── control ────────────────────────────────────────────────────────────

    async def _control_loop(self) -> None:
        per_tick = max(1, round(TICK_S / CONTROL_STEP_S))
        step = 0
        while self.running:
            await self._sleep(CONTROL_STEP_S)
            if not self.running:
                return
            step += 1
            try:
                await self.settle()
                if step % per_tick == 0:
                    await self.tick()
            except Exception:
                logger.debug("live stream control step failed", exc_info=True)

    async def settle(self) -> bool:
        """Ask for one sharp frame of a page that has gone still.

        Fires once per still: after a ladder frame, :data:`SETTLE_S` with no newer
        frame, the card visible, no takeover and no input in flight,
        at most one per :data:`SETTLE_MIN_INTERVAL_S`, and a quality that is not
        pinned below the sharpest picture. The
        screencast restarts at the settle picture; Chrome answers with one frame
        of the current page, which ``_frame`` masks and sends like any other.
        Returns whether it restarted.
        """
        if not (self.running and self.visible and self.casting) or self._settled:
            return False
        if not self.config.settles or self.metrics.seq <= self._settled_after:
            return False
        now = self._clock()
        if self.metrics.last_frame_at < 0 or now - self.metrics.last_frame_at < SETTLE_S:
            return False
        if self._input_pending or self._takeover:
            return False
        if self._settle_at >= 0 and now - self._settle_at < SETTLE_MIN_INTERVAL_S:
            return False
        if self._frame_lock.locked():
            return False
        self._settle_at = now
        self._settled_after = self.metrics.seq
        self.metrics.settles += 1
        await self.restart(settle=True)
        return True

    async def tick(self) -> TickStats:
        """One controller window: pick the rung, run the watchdog, report."""
        before = self.controller.rung
        stats = self.controller.tick()
        if stats.rung != before:
            stats.restarted = await self._apply_rung(before, stats.rung)
        self._remember()
        if await self._watchdog():
            stats.restarted = True
        try:
            await self.flush_held()
        except Exception:  # noqa: BLE001 - the recorder never costs the stream a tick
            logger.debug("live stream held-frame flush failed", exc_info=True)
        if self._on_tick is not None:
            await self._on_tick(stats)
        return stats

    async def _apply_rung(self, before: int, after: int) -> bool:
        """Apply a rung change: fps is pacing only; quality or width restarts the cast."""
        old, new = LADDER[before], LADDER[after]
        if (old.quality, old.width) == (new.quality, new.width) or self._settled:
            # Pacing only; or the cast is showing the settle frame, and the next
            # page change restarts it at the new picture anyway.
            return False
        if self.running and self.visible:
            await self.restart()
            return True
        return False

    async def _watchdog(self) -> bool:
        """Input sent and no frame for :data:`WATCHDOG_S`: restart the screencast once."""
        if not self._input_pending or not self.running or not self.visible:
            return False
        if self._clock() - self._input_at < WATCHDOG_S:
            return False
        self._input_pending = False
        await self.restart()
        return True


__all__ = [
    "AdaptiveController",
    "CARD_ACK_TIMEOUT_S",
    "FPS_STEPS",
    "FRAME_HEADER_TYPE",
    "FrameSink",
    "LADDER",
    "LINK_MEMORY",
    "LinkMemory",
    "LiveConfig",
    "LiveStream",
    "MASK_MIN_INTERVAL_S",
    "PICTURES",
    "Picture",
    "Relay",
    "Rung",
    "SETTLE_QUALITY",
    "SETTLE_S",
    "TAKEOVER_FPS_STEP",
    "TAKEOVER_RUNG",
    "TickStats",
    "WATCHDOG_S",
    "clip_mask_rects",
    "decode_frame",
    "encode_frame",
    "frame_header",
    "jpeg_size",
    "rung_index",
]
