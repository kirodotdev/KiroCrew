"""The pictures of one panel tab: the live stream, sized for a panel in the room.

A panel is a dashboard page shown on a flat surface in the owner's XR page. Its
pictures are the harness's own screencast (:class:`screencast.LiveStream`: the
relay-owned session, the password mask, the viewer's ack, the pacing, the
watchdog). What differs is the picture:

* **Its size is the panel's.** A panel asks for a page of ``css_w x css_h`` CSS
  pixels and for ``texels`` picture pixels per CSS pixel: about as many as the
  panel covers on the person's display, and no more. The ladder of
  ``screencast.PICTURES`` is for a card in a chat column and is not used; the
  adaptive controller only paces the frame rate here.
* **Up to four pictures are in flight.** The panel acks a picture twice: once it is
  decoded (the pace), and once it is in its texture, where the person sees it
  ("landed": what a wait for the person's own sight reads, :meth:`PanelStream.ack`).
  Chromium makes the next frame only after the stream's ack, and the stream
  gives that ack while fewer than ``panel_pace.PANEL_IN_FLIGHT`` pictures are
  on their way and their bytes fit the socket
  (:meth:`PanelStream._wait_card_ack`, ``panel_pace.Flight``).
* **A moving picture is at most** ``MOVING_MAX_WIDTH`` **wide.** The lossless
  still of a page at rest keeps the panel's full size.
* **Nothing is sent for a page that did not change.** Chromium makes a frame
  when the page painted, and also when something else asked the page for its
  pixels. A frame whose bytes equal the last picture sent is such a repeat: it
  is acked to Chromium and goes nowhere (:meth:`PanelStream._skip_frame`).
* **A still page gets one lossless picture.** Once no moving picture has come
  for ``SETTLE_S``, one PNG picture of the page is sent, at the same size.
  The next one waits for a new moving picture, and the frame the still
  itself sets off is a repeat, so a still never asks for another still.
* **Every picture shows the page and nothing else.** A list or a picker that
  the browser draws by itself is in no cast, and it is in a capture. So the
  still is a cast's frame too: the first frame of a PNG cast on a second
  session of the tab (:meth:`PanelStream._take_still`). A press where a
  picture shows something is then a press on that thing.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import struct
import weakref
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from .panel_pace import (
    PANEL_FLIGHT_BYTES,
    PANEL_SOCKET_BYTES,
    PANEL_STEP_BETWEEN,
    Flight,
    PanelController,
)
from .relay import CdpError, RelayClosed
from .screencast import (
    CARD_ACK_TIMEOUT_S,
    SETTLE_MIN_INTERVAL_S,
    SETTLE_S,
    Clock,
    LinkMemory,
    LiveConfig,
    LiveStream,
    Relay,
    Sleep,
    encode_frame,
    jpeg_size,
)

logger = logging.getLogger(__name__)

#: ``type`` of the JSON header before every panel picture on the owner's socket.
PANEL_FRAME_TYPE = "xr_panel_frame"
#: A picture of another shape than its page's is acked no sooner than this: the
#: page is laid out again first, and a tab that keeps the wrong size is not asked
#: for sixty such pictures a second.
MISSHAPEN_ACK_S = 0.25
#: Longest wait for the first frame of a still's own cast. The browser sends it at once (about 50 ms).
STILL_WAIT_S = 3.0
#: Longest a new size waits for the page's own layout before its cast starts again (D96 7). Two frames of a page
#: at 60 a second are about 35 ms; a page that never paints (a dialog open, a busy script) costs this much and no more.
LAYOUT_WAIT_S = 0.25
#: A size's hold of the cast's pictures ends this long after it began, whatever `fit_window`
#: and `apply_layout` do (M78): a browser slow to take a size never stops the pictures for
#: longer. The page's own question keeps its `LAYOUT_WAIT_S`.
SIZE_HOLD_MOST_S = 1.0
#: The gateway's own fixed question to the page, in the hub's own world of it: two of the page's own frames.
TWO_FRAMES_JS = (
    "new Promise(done => requestAnimationFrame(() => requestAnimationFrame(() => done(true))))"
)

#: The most a window may measure more than the view in it, each way, in CSS
#: pixels. A larger answer is not a window's frame, and the window is left alone.
MAX_WINDOW_FRAME = 400

#: Picture pixels per CSS pixel a panel may ask for. The tab is laid out at a
#: device scale of 2, so 2 is the most there is; below 0.5 text is a smear.
MIN_TEXELS = 0.5
MAX_TEXELS = 2.0
#: The steps a request is rounded UP to, so a hand moving a panel a little
#: nearer does not restart the screencast on every frame.
TEXEL_STEPS: tuple[float, ...] = (0.5, 0.75, 1.0, 1.4, 2.0)

#: The page a panel may ask for, in CSS pixels.
MIN_CSS = (320, 240)
MAX_CSS = (2560, 1600)

#: The most pixels across of a moving picture. A wider one (2048 for a page of 1024
#: at 2 picture pixels) is 180 to 228 kB at quality 80 and goes one or two at a time;
#: at this width it is about 110 to 150 kB and goes four at a time. The still that a
#: page gets once it comes to rest is not held to it (:meth:`PanelStream.picture_size`).
MOVING_MAX_WIDTH = 1434
#: JPEG quality of a moving picture of a page that a pointer is on. The same as with
#: no pointer on it (D136, M117): at 80 a real page's picture grew to about 270 kB,
#: and a page that scrolled under his pointer got 10 a second for it.
MOVING_QUALITY = 60
#: JPEG quality of a moving picture of a page that no pointer is on. The lossless
#: still that a page gets once it comes to rest is the same either way.
HELD_QUALITY = 60
#: Frames per second a panel's pictures never exceed.
PANEL_MAX_FPS = 30

#: Shortest time between two moving pictures of a page in view that no pointer is
#: on. Such a page is watched, not worked on: text that streams in, a counter or a
#: list shows at ten pictures a second, and the pages held this way share
#: ``HELD_PICTURES_PER_S`` (:meth:`PanelStream.idle_period`).
IDLE_PERIOD_S = 0.1

#: The most pictures a second that all the pages held for no pointer get together.
HELD_PICTURES_PER_S = 30

#: A held page's cast is stopped for its wait only when the wait is at least this
#: long: a start's first picture comes 70 to 130 ms after it, so a shorter wait
#: with the cast stopped would halve the page's pictures instead of saving work.
HOLD_WITHOUT_CAST_MIN_S = 0.25

#: How many pictures in a row of a page that moves say whether the page browser keeps up with it.
BROWSER_SHORT_PICTURES = 8
#: A frame that came later than this after it was asked for is the page's own quiet, not the browser's work.
BROWSER_MAKE_MAX_S = 0.25
#: The wait between two pictures of a held page while a page that moves is short for the
#: page browser: long enough that the held page's cast is stopped for it (:meth:`PanelStream.idle_period`).
HELD_YIELD_PERIOD_S = 1.0
#: How long the held pages go on yielding after a page that moves was last short. Twice
#: as long each time the page is short again right after they came back, up to the most.
HELD_YIELD_S = 30.0
HELD_YIELD_MAX_S = 240.0
#: Short again within this long after the held pages came back: their return was the cause.
HELD_RETURN_S = 5.0
#: A page whose last frame is older than this has stopped moving: no held page waits for it.
BROWSER_MOVES_S = 1.0

#: How long a page that is held back waits for its last picture to land before its cast is stopped for the wait.
LANDED_WAIT_S = 0.25

#: The two words of a panel's ack (``ack``): the picture is decoded, or it is in the texture. No word is an older XR page, whose one ack meant both.
ACK_KINDS = (None, "decoded", "landed")

#: The scale the panel browser lays every tab out at (``panel_browser.DEVICE_SCALE_ARG``).
DEVICE_SCALE = 2.0

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

PageEvent = Callable[[dict[str, Any]], Awaitable[None]]

#: The panels of each page browser, by its relay: they share its one compositing thread.
_BROWSER_PAGES: weakref.WeakKeyDictionary[Any, weakref.WeakSet[PanelStream]] = (
    weakref.WeakKeyDictionary()
)


def png_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a PNG's header chunk, or ``None``."""
    if len(data) < 24 or not data.startswith(_PNG_MAGIC) or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack_from(">II", data, 16)
    return (width, height) if width > 0 and height > 0 else None


def has_shape(width: int, height: int, css_w: int, css_h: int) -> bool:
    """Whether a picture of *width* by *height* has the shape of a page of *css_w* by *css_h*.

    To within one pixel of the picture: the browser scales a frame into the
    box it was asked for and rounds each side. A picture of another shape is
    one of a tab whose view is not the page's size: the panel would stretch
    it over the page, and a press would land beside what the person sees.
    """
    if width <= 0 or height <= 0 or css_w <= 0 or css_h <= 0:
        return False
    return abs(width * css_h - height * css_w) <= max(css_w, css_h)


def from_view(metadata: Any, css_w: int, css_h: int) -> bool | None:
    """Whether a cast frame says it was made from a view of the page's CSS size.

    ``None`` when the frame does not say (``metadata.deviceWidth`` and
    ``deviceHeight``). To within one pixel each way. This is what makes a
    picture right; its shape is only a mark of it, and a mark that a view of
    another size can share: a page of 1403 by 720 has the shape of a window's
    own view of 1280 by 657, and a frame of that view is the page cut at the
    right and at the bottom and drawn too large.
    """
    if not isinstance(metadata, dict):
        return None
    sides: list[float] = []
    for side in (metadata.get("deviceWidth"), metadata.get("deviceHeight")):
        if isinstance(side, bool) or not isinstance(side, (int, float)):
            return None
        if side != side or side <= 0 or side > 1e6:
            return None
        sides.append(float(side))
    return abs(sides[0] - css_w) <= 1 and abs(sides[1] - css_h) <= 1


def texel_step(value: Any) -> float:
    """The smallest step that gives at least *value* picture pixels per CSS pixel."""
    try:
        want = float(value)
    except (TypeError, ValueError):
        return MAX_TEXELS
    if want != want:  # NaN
        return MAX_TEXELS
    for step in TEXEL_STEPS:
        if want <= step + 1e-6:
            return step
    return MAX_TEXELS


def css_size(width: Any, height: Any) -> tuple[int, int] | None:
    """A page size a panel may ask for, or ``None``."""
    for value in (width, height):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if value != value or abs(value) > 1e6:
            return None
    w, h = int(round(width)), int(round(height))
    if not (MIN_CSS[0] <= w <= MAX_CSS[0] and MIN_CSS[1] <= h <= MAX_CSS[1]):
        return None
    return w, h


class PanelStream(LiveStream):
    """One panel tab's screencast, delivered to the one socket that opened the panel.

    ``on_page_event`` receives every CDP event of the tab's session after the
    stream has read it: the hub's address rule, sign-in watch and guard report
    hang there, on the one session the stream already owns.
    """

    def __init__(
        self,
        relay: Relay,
        target_id: str,
        sink: Any,
        *,
        panel_id: str,
        css_w: int,
        css_h: int,
        texels: float = MAX_TEXELS,
        on_page_event: PageEvent | None = None,
        on_tick: Callable[[Any], Awaitable[None]] | None = None,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
        auto_control: bool = True,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if clock is not None:
            kwargs["clock"] = clock
        if sleep is not None:
            kwargs["sleep"] = sleep
        super().__init__(
            relay,
            target_id,
            sink,
            session_key=panel_id,
            # The mask is always on for a panel: no setting turns it off.
            config=LiveConfig(max_fps=PANEL_MAX_FPS, mask_passwords=True),
            auto_control=auto_control,
            on_tick=on_tick,
            # A panel's pace is its own: never a chat card's remembered rung.
            link_memory=LinkMemory(),
            **kwargs,
        )
        self.panel_id = panel_id
        self.css_w = css_w
        self.css_h = css_h
        self.texels = texel_step(texels)
        self.on_page_event = on_page_event
        #: No pointer is on the page: its JPEG quality is ``HELD_QUALITY``.
        self.idle = True
        #: The page he looks at (the XR page says which, one a socket): no pointer is on it, and it
        #: still gets the moving pace (:attr:`held` is false), at the held quality.
        self.watched = False
        #: How many held pages (this one among them) sent a picture in the last second: the hub's count, or none.
        self.held_count: Callable[[], int] | None = None
        #: Set when a hand comes onto the page or sends it an input: an idle wait ends at once.
        self._hurry = asyncio.Event()
        #: Pictures that were dropped: made from a view of another size than the
        #: page's (:func:`from_view`), or of another shape (:func:`has_shape`).
        self.misshapen = 0
        #: The tab's window and what it measures more than the view in it, in
        #: CSS pixels; ``None`` when the browser did not say (:meth:`fit_window`).
        self._window: tuple[int, int, int] | None = None
        self._window_asked = False
        #: The bytes of the last moving picture sent, as a digest (``_skip_frame``).
        self._last_sent = b""
        #: A still is being taken: a frame that arrives now was made at the still's scale.
        self._shooting = False
        #: A new size waits for the page's own layout: a frame that arrives now shows its old one.
        #: The hold is on from the size until its end, and at most `SIZE_HOLD_MOST_S` (M78).
        self._holding = False
        self._hold_began = 0.0
        #: How many sizes the page has been given: a still whose cast began before the last one is dropped (M73).
        self._size_gen = 0
        #: The hub's own isolated world of the page (``Panel.world``), or ``None``; the hub sets it.
        self.world_of: Callable[[], int | None] = lambda: None
        #: The tab's second session, for the still's own cast, and the frame it waits for.
        self._still_session = ""
        self._still_frame: asyncio.Future[dict[str, Any]] | None = None
        #: One change of the cast at a time (``set_visible``, ``restart``).
        self._cast_lock = asyncio.Lock()
        #: The cast is stopped for an idle wait and starts again when it is over
        #: (:meth:`_hold_without_cast`): the page is in view and well with no cast.
        self.holding = False
        #: ``metrics.seq`` at the last ack: a newer one means a picture went out since.
        self._acked_seq = 0
        #: From a picture's send to the panel's ack, in ms, for the last one acked:
        #: it rides in the next picture's header for the XR page's readout.
        self._round_ms: int | None = None
        self._sent_at = 0.0
        #: The newest picture that the panel says is in its texture, and the event a wait for it hears.
        self.landed_seq = 0
        self._landed = asyncio.Event()
        # A panel's late rule is past its own round trip (``panel_pace``).
        self.pace = PanelController(
            fps_floor=self.config.fps_floor(), picture_floor=self.config.picture_floor()
        )
        self.controller = self.pace
        #: When the stream last asked the browser for a picture (an ack, or a start of
        #: the cast), and when the browser's last frame came: a held page's period
        #: counts from the first, and the gap between them says the page was quiet.
        self._chrome_acked_at = float("-inf")
        self._frame_in_at = 0.0
        #: How long the browser took for each of the last pictures of this page while it
        #: moved, and when they last showed the browser short (:meth:`_note_make`).
        self._makes: deque[float] = deque(maxlen=BROWSER_SHORT_PICTURES)
        self._short_at = float("-inf")
        self._yield_s = HELD_YIELD_S
        _BROWSER_PAGES.setdefault(self.relay, weakref.WeakSet()).add(self)
        #: The JPEG quality that the running cast was started with (:meth:`quality`).
        self._cast_quality = MOVING_QUALITY
        #: The pictures on their way to the XR page, and the event an ack sets (:meth:`_wait_card_ack`).
        self.flight = Flight()
        self._flight_moved = asyncio.Event()
        #: A picture was given up for want of an ack, and none came since: one goes at a time until one does (M118).
        self._gave_up = False
        #: The bytes of the picture about to go out (the moving one at ``_skip_frame``, a still at its send).
        self._next_bytes = 0
        #: The picture bytes on their way on this panel's socket, every panel's: the hub's count, or this one's.
        self.socket_flight: Callable[[], int] | None = None

    # ── the picture ────────────────────────────────────────────────────────

    def picture_size(self) -> tuple[int, int]:
        """The picture's box in pixels: the page's CSS size times ``texels``. The still's size."""
        return max(1, round(self.css_w * self.texels)), max(1, round(self.css_h * self.texels))

    def moving_size(self) -> tuple[int, int]:
        """The box of a moving picture: the picture's, made smaller in its own shape to ``MOVING_MAX_WIDTH`` across."""
        width, height = self.picture_size()
        if width <= MOVING_MAX_WIDTH:
            return width, height
        return MOVING_MAX_WIDTH, max(1, round(height * MOVING_MAX_WIDTH / width))

    def quality(self) -> int:
        """The JPEG quality that the page's moving pictures want now: lower while no pointer is on it."""
        return HELD_QUALITY if self.idle else MOVING_QUALITY

    async def _ask_for_cast(self) -> None:
        width, height = self.moving_size()
        self._cast_quality = self.quality()
        await self.relay.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": self._cast_quality,
                "maxWidth": width,
                "maxHeight": height,
                "everyNthFrame": 1,
                # The browser makes the next frame only after the stream's ack, so
                # it is the newest; the stream holds up to four on their way to the panel.
                "maxFramesInFlight": 1,
                "sendLastFrame": True,
            },
            self.session_id,
        )

    async def _start_cast(self) -> None:
        """Start the cast, and believe that it runs only when the browser said so.

        The browser refuses a start while a cast of its own runs. That is a cast
        whose start was never answered, or whose stop failed: the stream took it
        for stopped. A refused start is therefore asked once more behind a stop,
        so the cast that runs afterwards is the one this panel asked for.
        """
        try:
            await self._ask_for_cast()
        except CdpError:
            with contextlib.suppress(CdpError):
                await self.relay.send("Page.stopScreencast", {}, self.session_id)
            self.casting = False
            await self._ask_for_cast()
        self.casting = True

    async def _stop_cast(self) -> None:
        """Stop the cast. It counts as stopped once the browser answered, not before."""
        try:
            await self.relay.send("Page.stopScreencast", {}, self.session_id)
        except CdpError:
            # An answer all the same: the tab is gone, and its cast with it.
            self.casting = False
            raise
        finally:
            # A picture of a cast that stopped is one no newer picture is held behind.
            self.flight.clear()
            self._flight_moved.set()
        self.casting = False

    def ack(self, seq: int, kind: Any = None) -> bool:  # type: ignore[override]
        """The panel's ack of picture ``seq``: ``decoded`` paces the next picture; ``landed`` says it is seen.

        Every rule that waits for the person's own sight reads the landed ack (the
        wait before a held page's cast is stopped: :meth:`_wait_landed`); the pace
        reads the decoded one. An older XR page sends one ack with no word: it is both.
        """
        if kind not in ACK_KINDS:
            return False
        if kind != "decoded" and seq > self.landed_seq:
            self.landed_seq = seq
            self._landed.set()
        released = False
        if kind != "landed":
            released, own = self.flight.acked(seq, self._clock())
            if released:
                self._gave_up = False
            if own is not None:
                self.pace.record(*own)
                self._round_ms = round(own[1] * 1000)
            self._flight_moved.set()
        return super().ack(seq) or released

    async def _wait_landed(self, seq: int) -> None:
        """Wait until picture ``seq`` (or a newer one) is in the panel's texture, bounded by ``LANDED_WAIT_S``."""
        if self.landed_seq >= seq or self._hurry.is_set():
            return
        self._landed.clear()
        heard = asyncio.ensure_future(self._landed.wait())
        hand = asyncio.ensure_future(self._hurry.wait())
        bound = asyncio.ensure_future(self._sleep(LANDED_WAIT_S))
        try:
            while self.landed_seq < seq and not bound.done() and not hand.done():
                await asyncio.wait({heard, hand, bound}, return_when=asyncio.FIRST_COMPLETED)
                if heard.done() and self.landed_seq < seq:
                    self._landed.clear()
                    heard = asyncio.ensure_future(self._landed.wait())
        finally:
            for task in (heard, hand, bound):
                if not task.done():
                    task.cancel()
            await asyncio.gather(heard, hand, bound, return_exceptions=True)

    @property
    def held(self) -> bool:
        """Whether its pictures are paced at :meth:`idle_period`: no pointer is on it and he does not look at it."""
        return self.idle and not self.watched

    def idle_period(self) -> float:
        """The wait between two pictures of this held page.

        ``IDLE_PERIOD_S``, or the share of the held pages' sum; ``HELD_YIELD_PERIOD_S``
        while a page that moves is short for the page browser (:meth:`short_for_browser`).
        """
        n = self.held_count() if self.held_count is not None else 1
        period = max(IDLE_PERIOD_S, max(1, n) / HELD_PICTURES_PER_S)
        now = self._clock()
        others = _BROWSER_PAGES.get(self.relay, ())
        if any(other is not self and other.short_for_browser(now) for other in others):
            # The wait is long enough that this page's cast is stopped for it
            # (``HOLD_WITHOUT_CAST_MIN_S``): only a stopped cast gives the thread back.
            period = max(period, HELD_YIELD_PERIOD_S)
        return period

    def short_for_browser(self, now: float) -> bool:
        """Whether this page moves (he looks at it, or a pointer is on it) and the page browser made its pictures late.

        The page browser draws every page with a running cast on one compositing
        thread, in turn: three full-size pages that all draw gave the page he looks
        at 15 pictures a second at an aim of 24, with nothing waiting on the socket.
        The pages nobody looks at then give the thread back (:meth:`idle_period`).
        Late is later than a period of ``PANEL_STEP_BETWEEN``: a page that gets 24
        a second at an aim of 30 takes nothing from them.
        Only while it moves: a second after its last frame the held pages are
        back at their own pace. With the thread to itself the page reads as not short, so after
        ``HELD_YIELD_S`` the held pages come back; short again right after, they
        stay away twice as long. shortcut: a page that by itself draws slower than
        its aim reads as short too, and the held pages then wait for nothing;
        upgrade when the browser says how long a frame took it.
        """
        return (
            self.running
            and self.visible
            and not self.held
            and now - self._short_at < self._yield_s
            and now - self._frame_in_at < BROWSER_MOVES_S
        )

    def _note_make(self, now: float) -> None:
        """A frame of the cast came: how long after it was asked for, while the page moves."""
        make = now - self._chrome_acked_at
        if self.held or not self.visible or make > BROWSER_MAKE_MAX_S:
            self._makes.clear()
            return
        self._makes.append(make)
        if len(self._makes) == BROWSER_SHORT_PICTURES and sum(
            self._makes
        ) > BROWSER_SHORT_PICTURES / min(self.pace.aim(), PANEL_STEP_BETWEEN):
            since = now - self._short_at
            if since >= self._yield_s + HELD_RETURN_S:
                self._yield_s = HELD_YIELD_S
            elif since >= self._yield_s:
                self._yield_s = min(self._yield_s * 2, HELD_YIELD_MAX_S)
            self._short_at = now

    def sent_lately(self, now: float) -> bool:
        """Whether this page is held for no pointer and sent a picture in the last second (the hub's count)."""
        return self.held and self.visible and self.metrics.seq > 0 and now - self._sent_at < 1.0

    async def _paced_ack(self, chrome_session: Any, *, floor: float = 0.0) -> None:
        fresh = self.metrics.seq != self._acked_seq
        self._acked_seq = self.metrics.seq
        if self.held:
            # A page no pointer is on gets its next picture one period after its last
            # ack to the browser: the browser's capture, the send, and the panel's
            # decode and landing all run inside that period, so a slow link or a slow
            # capture does not add to it. After a quiet spell (the browser had nothing
            # to send for longer than a period) the grid would ack at once, and the
            # browser would send its next picture at once too: that picture's period
            # starts now instead. A hand ends the wait the moment it is there: the
            # browser makes no picture until this ack. The grid's own slot (the
            # pace) counts from the last ack too, so it adds no wait of its own here.
            # A hand or an input from here on ends every wait below (the wait for the
            # picture to land too): the flag is cleared once, here, and not by a wait.
            self._hurry.clear()
            period = self.idle_period()
            quiet = self._frame_in_at - self._chrome_acked_at > period

            def wait_left() -> float:
                due = (self._clock() if quiet else self._chrome_acked_at) + period
                return max(0.0, due - self._clock())

            wait = wait_left()
            if fresh and not floor and wait >= HOLD_WITHOUT_CAST_MIN_S:
                # The cast is stopped only once the picture is seen and none is on its way.
                # A picture whose ack was lost is given up once it is CARD_ACK_TIMEOUT_S
                # old, and the wait it cost is followed by a whole period with no cast.
                await self._wait_landed(self.metrics.seq)
                lost = await self._wait_flight_settled()
                wait = max(wait_left(), period) if lost else wait_left()
                if (
                    not self.flight.pictures
                    and wait >= HOLD_WITHOUT_CAST_MIN_S
                    and await self._hold_without_cast(wait)
                ):
                    return
            await self._idle_wait(max(floor, wait))
        # A pointer came onto the page or left it: the cast starts again at the
        # quality that it wants now, and the start's picture is the next one.
        if not floor and self.casting and self._cast_quality != self.quality():
            with contextlib.suppress(CdpError, RelayClosed, asyncio.TimeoutError, OSError):
                await self.restart()
                self._chrome_acked_at = self._clock()
                return
        # The panel's own step between two of the ladder's is a longer period on the ladder's faster one.
        await super()._paced_ack(chrome_session, floor=floor or self.pace.period_floor())
        self._chrome_acked_at = self._clock()

    async def _hold_without_cast(self, seconds: float) -> bool:
        """Wait out an idle period after a picture with the cast stopped, then start it again.

        A cast that runs makes the page browser's one compositing thread work
        for every frame that the page draws, also while no picture is asked
        for: a page held at two pictures a second cost that thread 0.62 of a
        core, and every other page drew less for it. Stopped, it costs 0.21.
        A start gives a picture at once, of the page as it is then, so a change
        shows within the wait and the start's 70 to 130 ms; a hand or an input
        ends the wait at once (:meth:`hurry`). While the cast is stopped the
        page counts as shown (``holding``): the hub's look does not start one.

        After a picture only: a repeat of the last one (a page at rest) is
        acked as before, with the cast left on, since a page that does not
        draw costs the thread nothing. shortcut: a start's picture of a page
        that did not change has new bytes in about 6 of 10 starts, so a page
        that stops changing gets a few more pictures until two match; upgrade
        when a picture can be compared by its pixels.

        Returns ``False`` when the cast could not be stopped: the wait then
        runs with the cast on, as before.
        """
        async with self._cast_lock:
            if not (self.running and self.visible and self.casting):
                return False
            try:
                await self._stop_cast()
            except (CdpError, RelayClosed, asyncio.TimeoutError, OSError):
                logger.debug("xr panel: the cast was not stopped for the wait", exc_info=True)
            if self.casting:
                return False
            self.holding = True
        try:
            await self._idle_wait(seconds)
        finally:
            self.holding = False
        async with self._cast_lock:
            # A panel that left the view meanwhile stays without its cast; one that
            # came back, or a restart, has started it already.
            if self.running and self.visible and not self.casting:
                self._last_ack_due = max(self._last_ack_due, self._clock())
                # A start asks for a picture as an ack does: the next period counts from it.
                self._chrome_acked_at = self._clock()
                try:
                    await self._start_cast()
                except (CdpError, RelayClosed, asyncio.TimeoutError, OSError):
                    # The hub's look once a second asks again: a page in view and
                    # not holding with no cast is one it starts (``_show``).
                    logger.debug("xr panel: the cast did not start after the wait", exc_info=True)
        return True

    async def _idle_wait(self, seconds: float) -> None:
        """Wait *seconds*, or until :meth:`hurry`, whichever is first (the flag is cleared at the ack, :meth:`_paced_ack`)."""
        if self._hurry.is_set():
            return
        nap = asyncio.ensure_future(self._sleep(seconds))
        hand = asyncio.ensure_future(self._hurry.wait())
        try:
            await asyncio.wait({nap, hand}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (nap, hand):
                if not task.done():
                    task.cancel()
            await asyncio.gather(nap, hand, return_exceptions=True)

    def hurry(self) -> None:
        """A hand is on the page: a picture that waits out the idle period goes on now."""
        self._hurry.set()

    def note_input(self) -> None:
        super().note_input()
        self._hurry.set()

    def _skip_frame(self, jpeg: bytes) -> bool:
        """A frame with the bytes of the last picture sent shows nothing new."""
        digest = hashlib.blake2b(jpeg, digest_size=16).digest()
        if digest == self._last_sent:
            return True
        self._last_sent = digest
        self._next_bytes = len(jpeg)
        return False

    async def _misshapen(self, chrome_session: Any = None) -> None:
        """A picture not made from a view of the page's size: counted, never sent, the page laid out again.

        The tab's view was not the page's size when the browser made it (a tab
        that comes in front gets its window's own size), or the browser made
        the frame on a surface of another size than the view: the first frame
        after a show is at times 56 CSS pixels taller than a view that is
        right. The frame is acked so the next one comes: it is judged the same
        way. The ack is on the grid with a floor, not behind the idle period:
        the page has had no picture from this frame, so one that is dropped
        after a quiet spell costs no wait, and a tab that keeps the wrong size
        is asked ``1 / MISSHAPEN_ACK_S`` times a second.
        """
        self.misshapen += 1
        try:
            await self.apply_layout()
        except (CdpError, RelayClosed, asyncio.TimeoutError, OSError):
            logger.debug("xr panel: the page was not laid out again", exc_info=True)
        await LiveStream._paced_ack(self, chrome_session, floor=MISSHAPEN_ACK_S)

    async def _frame(self, params: dict[str, Any]) -> None:
        data = params.get("data")
        if isinstance(data, str):
            try:
                size = jpeg_size(base64.b64decode(data, validate=True))
            except (ValueError, TypeError):
                size = None
            # Two tests, and a frame passes both. The view that the cast names is
            # the page's size: that is what makes the picture right. And the
            # picture has the page's shape: the browser makes a frame at times on
            # a surface of another size than the view it names.
            other_view = from_view(params.get("metadata"), self.css_w, self.css_h) is False
            if other_view or (
                size is not None and not has_shape(size[0], size[1], self.css_w, self.css_h)
            ):
                await self._misshapen(params.get("sessionId"))
                return
        seq, last = self.metrics.seq, self._last_sent
        sent = False
        try:
            await super()._frame(params)
            sent = self.metrics.seq != seq
        finally:
            if not sent:
                for gone in [s for s in self.flight.pictures if s > seq]:
                    del self.flight.pictures[gone]
                # No picture went out: the panel left the view while the page was
                # asked for its secret fields, or the socket did not take it. The
                # frame is then not "the last picture sent". Kept as that, the
                # same bytes would be dropped as a repeat the next time the
                # browser makes them, and a page that does not change would never
                # be drawn. That the browser makes them again is the caller's
                # doing: it acks a frame that failed (``LiveStream._on_event``).
                self._last_sent = last

    async def settle(self) -> bool:
        """Send one lossless picture of a page that has gone still.

        Once per still: after a moving picture, ``SETTLE_S`` with no newer one, the
        panel in view, no input in flight, and at most one per
        ``SETTLE_MIN_INTERVAL_S``. Returns whether a still was sent.
        """
        if not (self.running and self.visible and self.casting):
            return False
        if self.metrics.seq <= self._settled_after:
            return False
        now = self._clock()
        if self.metrics.last_frame_at < 0 or now - self.metrics.last_frame_at < SETTLE_S:
            return False
        if self._input_pending or self._frame_lock.locked():
            return False
        # A new size waits for the page's own layout (`_let_page_lay_out`): a still's own cast would catch the page
        # before it, and send that picture lossless. The next look after the wait takes it (M60).
        if self._settling:
            return False
        if self._settle_at >= 0 and now - self._settle_at < SETTLE_MIN_INTERVAL_S:
            return False
        self._settle_at = now
        self._shooting = True
        try:
            return await self._send_still()
        finally:
            self._shooting = False

    async def _on_still_event(self, msg: dict[str, Any]) -> None:
        """The still's own session: the first frame of its cast is the still."""
        waiter = self._still_frame
        if msg.get("method") == "Page.screencastFrame" and waiter is not None and not waiter.done():
            waiter.set_result(msg.get("params") or {})

    async def _take_still(self) -> dict[str, Any] | None:
        """The first frame of a PNG cast on a second session of the tab, or ``None``.

        A capture of the page (``Page.captureScreenshot``) shows what the
        browser draws over it by itself: an open list of a ``select``, a
        colour or date picker, in the page's own document or in a frame. A
        cast does not. The moving pictures are a cast's, so a still that was
        a capture showed a list that no moving picture had, and a press where
        it stood went to what lay behind it. The still is therefore a cast's
        frame as well: at 2 picture pixels for a CSS pixel it is the capture's
        picture, pixel for pixel, and below that it is the cast's smaller
        picture, a little softer.

        The cast is the still's own, on a second session of the tab: the
        stream's cast of moving pictures runs on beside it and is not stopped
        or started for a still. The frame names the view it was made from
        (``metadata``), so a still is judged as a moving picture is.
        """
        if not self._still_session:
            self._still_session = await self.relay.attach(self.target_id, self._on_still_event)
        width, height = self.picture_size()
        waiter: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._still_frame = waiter
        try:
            await self.relay.send(
                "Page.startScreencast",
                {"format": "png", "maxWidth": width, "maxHeight": height, "everyNthFrame": 1},
                self._still_session,
            )
            # The frame, or the bound on the stream's own clock, whichever is first.
            nap = asyncio.ensure_future(self._sleep(STILL_WAIT_S))
            first: set[asyncio.Future[Any]] = {waiter, nap}
            try:
                await asyncio.wait(first, return_when=asyncio.FIRST_COMPLETED)
            finally:
                if not nap.done():
                    nap.cancel()
                await asyncio.gather(nap, return_exceptions=True)
            return waiter.result() if waiter.done() else None
        finally:
            if not waiter.done():
                waiter.cancel()
            self._still_frame = None
            with contextlib.suppress(CdpError, RelayClosed, asyncio.TimeoutError, OSError):
                await self.relay.send("Page.stopScreencast", {}, self._still_session)

    async def stop(self) -> None:
        session, self._still_session = self._still_session, ""
        try:
            await super().stop()
        finally:
            if session:
                with contextlib.suppress(Exception):
                    await self.relay.detach(session)

    async def _send_still(self) -> bool:
        async with self._frame_lock:
            run = self._run
            gen = self._size_gen
            shot = await self._take_still()
            if shot is None:
                return False
            # Its cast began before a new size: its frame may be the page at the new size
            # but its old layout, and it would pass the shape test (M73). Not sent; the
            # next look after the size takes a still again.
            if gen != self._size_gen:
                return False
            try:
                png = base64.b64decode(str(shot.get("data") or ""), validate=True)
            except (ValueError, TypeError):
                return False
            size = png_size(png)
            if size is None or not self.running or run != self._run:
                return False
            # The two tests of a moving picture: the view it names is the page's, and it has the page's shape.
            other_view = from_view(shot.get("metadata"), self.css_w, self.css_h) is False
            if other_view or not has_shape(size[0], size[1], self.css_w, self.css_h):
                # Not sent, and not "settled": the next look takes a still again.
                self.misshapen += 1
                with contextlib.suppress(CdpError, RelayClosed, asyncio.TimeoutError, OSError):
                    await self.apply_layout()
                return False
            mask = await self._mask_rects(*size)
            # The view is looked at again here: the panel may have left it while
            # the browser took the picture, and a page out of view sends none.
            if not self.running or not self.visible or run != self._run:
                return False
            self.metrics.seq += 1
            seq = self.metrics.seq
            self._next_bytes = len(png)
            header = self._frame_header(seq, size[0], size[1], mask)
            waiter = asyncio.Event()
            self._acks[seq] = waiter
            try:
                await self.sink.frame(header, encode_frame(header, png))
                await self._wait_card_ack(waiter)
            finally:
                self._acks.pop(seq, None)
                # A still that did not go out is on no way (one that did was released by now).
                self.flight.pictures.pop(seq, None)
            self._settled_after = seq
            self.metrics.settles += 1
        return True

    async def _apply_rung(self, before: int, after: int) -> bool:
        """A rung change is pacing only: the picture follows the panel, not the ladder."""
        return False

    def _frame_header(
        self, seq: int, width: int, height: int, mask: list[list[int]]
    ) -> dict[str, Any]:
        # Made right before the picture goes out: its round trip counts from here, and
        # it is on its way from here, so an ack that comes before the send returns counts.
        self._sent_at = self._clock()
        self.flight.sent(seq, self._sent_at, self._next_bytes, measured=not self._shooting)
        return {
            "type": PANEL_FRAME_TYPE,
            "panel": self.panel_id,
            "stream": self.stream_id,
            "seq": seq,
            "w": width,
            "h": height,
            "css": [self.css_w, self.css_h],
            "mask": mask,
            # How many pictures of another shape than the page's were dropped so far.
            "misshapen": self.misshapen,
            # For the XR page's readout (``?fps=1``), what only the gateway knows: the
            # round trip of the picture before, the pace it stands at, and whether the
            # page is held back (no pointer on it, and he does not look at it).
            "round_ms": self._round_ms,
            "pace": self.pace.aim(),
            "held": self.held,
        }

    async def _wait_card_ack(self, waiter: asyncio.Event) -> bool:
        """Pictures in flight: the picture just sent lets the next be made while fewer than ``PANEL_IN_FLIGHT`` are on their way.

        The browser makes the next picture only after its ack (``maxFramesInFlight``
        is 1), so the round trip to the XR page and its decode ran inside every
        picture's period. Now the next one is made while this one is on its way.
        It waits for this one's own ack instead when it is a lossless still, and
        for an older one's when one more picture would put more of this panel's
        than ``PANEL_FLIGHT_BYTES`` on the socket, or more than
        ``PANEL_SOCKET_BYTES`` of every panel's: the socket also carries the
        room's own messages. A picture no ack came for in
        ``CARD_ACK_TIMEOUT_S`` is given up as a late delivery.
        """
        seq = self.metrics.seq
        still = self._shooting
        bound = asyncio.ensure_future(self._sleep(CARD_ACK_TIMEOUT_S))
        try:
            while not self._released(seq, still=still) and not bound.done():
                self._flight_moved.clear()
                moved = asyncio.ensure_future(self._flight_moved.wait())
                # The oldest picture on its way is given up at its own CARD_ACK_TIMEOUT_S, not at this one's.
                oldest = min(p.sent_at for p in self.flight.pictures.values())
                lost = asyncio.ensure_future(
                    self._sleep(max(0.0, oldest + CARD_ACK_TIMEOUT_S - self._clock()))
                )
                try:
                    await asyncio.wait({moved, bound, lost}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in (moved, lost):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(moved, lost, return_exceptions=True)
        finally:
            if not bound.done():
                bound.cancel()
            await asyncio.gather(bound, return_exceptions=True)
        if self._released(seq, still=still):
            return True
        if any(s <= seq for s in self.flight.pictures):
            self._gave_up = True
        for nbytes, delivery in self.flight.given_up(seq, self._clock()):
            self.pace.record(nbytes, delivery)
        return False

    async def _wait_flight_settled(self) -> bool:
        """Wait until no picture is on its way, or a hand comes. Returns whether one was given up as lost."""
        lost = False
        while self.flight.pictures and not self._hurry.is_set():
            if self._expire_flight():
                lost = True
                continue
            oldest = min(p.sent_at for p in self.flight.pictures.values())
            self._flight_moved.clear()
            moved = asyncio.ensure_future(self._flight_moved.wait())
            hand = asyncio.ensure_future(self._hurry.wait())
            nap = asyncio.ensure_future(
                self._sleep(max(0.0, oldest + CARD_ACK_TIMEOUT_S - self._clock()))
            )
            try:
                await asyncio.wait({moved, hand, nap}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (moved, hand, nap):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(moved, hand, nap, return_exceptions=True)
        return lost

    def _expire_flight(self) -> bool:
        """Give up every picture that no ack came for in ``CARD_ACK_TIMEOUT_S``. Returns whether any was."""
        before = len(self.flight.pictures)
        for nbytes, delivery in self.flight.expired(self._clock(), CARD_ACK_TIMEOUT_S):
            self.pace.record(nbytes, delivery)
        dropped = len(self.flight.pictures) != before
        if dropped:
            self._gave_up = True
            # A wait for the picture before it (`_wait_card_ack`) hears it at once.
            self._flight_moved.set()
        return dropped

    def flight_bytes(self) -> int:
        """The bytes of this panel's pictures on their way, a lost one given up first (the hub's socket count)."""
        self._expire_flight()
        return self.flight.bytes()

    def _released(self, seq: int, *, still: bool) -> bool:
        # An older picture whose ack was lost holds nothing back once it is given up.
        self._expire_flight()
        if seq not in self.flight.pictures:
            return True
        # A link that brings no ack carries nothing more for a second picture: after one was
        # given up, one goes at a time until an ack comes (M118).
        cap = 1 if self._gave_up else self.pace.flight_cap()
        if still or self.flight.older_than(seq) > cap - 2:
            return False
        own = self.flight_bytes()
        on_socket = self.socket_flight() if self.socket_flight is not None else own
        nbytes = self.flight.pictures[seq].nbytes
        # shortcut: another panel's ack that frees the socket's bytes does not wake this
        # wait, so it ends at this picture's own ack; upgrade to a socket-wide event if
        # three moving pages show it.
        return own + nbytes <= PANEL_FLIGHT_BYTES and on_socket + nbytes <= PANEL_SOCKET_BYTES

    # ── the cast, one change at a time ─────────────────────────────────────

    async def set_visible(self, visible: bool) -> None:
        """In view or out of it, behind any change of the cast that is under way.

        The browser starts and stops the cast on a message, and ``casting`` is
        true only once it answered. A panel that left the view while its cast was
        being started found no cast to stop; the cast then ran for a page nobody
        saw, and to the hub a cast that runs is a panel that is shown, so no later
        look at the panel started one: a live page in view with no picture.

        A panel that comes into view asks the browser for its cast whatever the
        stream believes of it. ``casting`` is the stream's own book, and one
        answer of the browser that does not arrive makes it wrong; the start is
        what puts it right (:meth:`_start_cast`). A start that fails leaves the
        stream out of view, so the next look at the panel asks again.
        """
        async with self._cast_lock:
            visible = bool(visible)
            if visible == self.visible:
                return
            self.visible = visible
            if not self.running:
                return
            if visible:
                # The panel waits for a picture when it comes back: it takes no
                # press until one lands. A page at rest makes its first frame
                # with the bytes of the last one sent, so that one is sent too.
                self._last_sent = b""
                try:
                    await self._start_cast()
                except BaseException:
                    self.visible = False
                    raise
            elif self.casting:
                await self._stop_cast()

    async def restart(self, *, settle: bool = False) -> None:
        """Stop the cast and start it again, with the book put right when it does not end.

        A restart is a stop and then a start. When the browser did the stop and
        its answer did not arrive, the restart ends there with the book at "a
        cast runs" and no cast in the browser, and to the hub's look such a
        panel is well. A restart that does not end therefore leaves the book
        at "no cast": the next look asks, and a start that the browser refuses,
        because its cast still runs, is asked again behind a stop.
        """
        async with self._cast_lock:
            try:
                await super().restart(settle=settle)
            except BaseException:
                self.casting = False
                raise

    # ── what the panel asks for ────────────────────────────────────────────

    @property
    def _settling(self) -> bool:
        """A size's hold is on: from the size until its end, and for `SIZE_HOLD_MOST_S` at most."""
        return self._holding and self._clock() - self._hold_began < SIZE_HOLD_MOST_S

    async def set_size(self, css_w: int, css_h: int) -> bool:
        """Lay the page out at a new CSS size. Returns whether it changed."""
        if (css_w, css_h) == (self.css_w, self.css_h):
            return False
        self.css_w, self.css_h = css_w, css_h
        self._size_gen += 1
        if not self.running:
            return True
        # The hold starts BEFORE the browser takes the new size (M73): a frame made while
        # `fit_window` and `apply_layout` run, or while the page lays itself out, shows
        # the old layout in the new view, and a still that starts then is not taken.
        self._holding = True
        self._hold_began = self._clock()
        try:
            await self.fit_window()
            await self.apply_layout()
            await self._let_page_lay_out()
        finally:
            self._holding = False
        await self.restart()
        return True

    async def _let_page_lay_out(self) -> None:
        """Hold the cast's pictures back until the page has had two frames of its own at the new size.

        The browser takes a new size at once, but the page lays itself out
        after that: a picture taken in between shows its old layout in the
        new view, and passes the shape test (D90 3, D96 7). So, for a stream
        in view that casts, its frames are acked and not sent (as a still's
        are) while the page is asked, in the hub's own world of it, for two
        animation frames: the gateway's own fixed text, nothing from the tab
        or a link. At ``LAYOUT_WAIT_S``, or at any error of the browser (a
        navigation ends the question), it goes on exactly as before: the
        restart follows either way. While it waits, this socket's later ops
        wait too (``browser_panels.py``: one op at a time), for the limit at
        most; acks do not.
        """
        world = self.world_of()
        if not (self.visible and self.casting) or world is None:
            return
        try:
            await asyncio.wait_for(
                self.relay.send(
                    "Runtime.evaluate",
                    {
                        "expression": TWO_FRAMES_JS,
                        "contextId": world,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                    self.session_id,
                ),
                LAYOUT_WAIT_S,
            )
        except (CdpError, RelayClosed, asyncio.TimeoutError, OSError):
            logger.debug("xr panel: the page's layout was not waited for", exc_info=True)

    async def fit_window(self) -> None:
        """Give the tab's window the page's size as well, where the browser lets it.

        A tab that comes in front takes its window's own view. With the window
        as large as the page plus its own frame, that view is the page's size
        too. The frame is what the window measures more than the view in it. It
        is read once, on the first call, which therefore comes before the tab's
        first layout: a tab that is laid out answers with the page's size, not
        with its view's. A browser that does not say, or refuses the new size,
        leaves the window as it is. The picture does not rest on this: the tab
        is laid out again after a show, and a frame of another shape is dropped.
        """
        try:
            if not self._window_asked:
                self._window_asked = True
                self._window = await self._measure_window()
            if self._window is None:
                return
            window_id, extra_w, extra_h = self._window
            await self.relay.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window_id,
                    "bounds": {"width": self.css_w + extra_w, "height": self.css_h + extra_h},
                },
            )
        except (CdpError, RelayClosed, asyncio.TimeoutError, OSError):
            logger.debug("xr panel: the window kept its size", exc_info=True)

    async def _measure_window(self) -> tuple[int, int, int] | None:
        """The tab's window id and its frame's width and height, or ``None``."""
        window = await self.relay.send("Browser.getWindowForTarget", {"targetId": self.target_id})
        metrics = await self.relay.send("Page.getLayoutMetrics", {}, self.session_id)
        window_id, bounds = window.get("windowId"), window.get("bounds")
        view = metrics.get("cssLayoutViewport")
        if not isinstance(window_id, int) or not isinstance(bounds, dict):
            return None
        if not isinstance(view, dict):
            return None
        sizes: list[Any] = [
            bounds.get("width"),
            bounds.get("height"),
            view.get("clientWidth"),
            view.get("clientHeight"),
        ]
        if any(isinstance(v, bool) or not isinstance(v, int) for v in sizes):
            return None
        extra_w, extra_h = sizes[0] - sizes[2], sizes[1] - sizes[3]
        if not (0 <= extra_w <= MAX_WINDOW_FRAME and 0 <= extra_h <= MAX_WINDOW_FRAME):
            return None
        return window_id, extra_w, extra_h

    async def apply_layout(self) -> None:
        """Give the tab its viewport: the panel's CSS size, the browser's own scale."""
        await self.relay.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": self.css_w, "height": self.css_h, "deviceScaleFactor": 0, "mobile": False},
            self.session_id,
        )

    async def set_texels(self, texels: Any) -> bool:
        """Ask for a sharper or a smaller picture. Returns whether it changed."""
        step = texel_step(texels)
        if step == self.texels:
            return False
        self.texels = step
        if self.running and self.visible:
            await self.restart()
        return True

    # ── events ─────────────────────────────────────────────────────────────

    async def _on_event(self, msg: dict[str, Any]) -> None:
        frame = msg.get("method") == "Page.screencastFrame"
        if frame:
            self._frame_in_at = self._clock()
            self._note_make(self._frame_in_at)
        if self.on_page_event is not None and not frame:
            try:
                await self.on_page_event(msg)
            except Exception:  # noqa: BLE001 - a watcher never costs the stream an event
                logger.debug("panel page event failed", exc_info=True)
        if (
            frame
            and (self._shooting or self._settling)
            and msg.get("sessionId") in (None, self.session_id)
        ):
            # The browser made this frame while it took the still, at the still's
            # own scale, or while a new size waits for the page's layout, so it
            # shows the old one. Sent on, a still's frame would count as the page
            # having moved: the still would set off a picture, and that picture
            # the next still. It is acked and dropped. The frame the browser sends
            # next is the page as it is: new if the page changed, else the bytes
            # sent last.
            async with self._frame_lock:
                if self.running:
                    await self._paced_ack((msg.get("params") or {}).get("sessionId"))
            return
        await super()._on_event(msg)


__all__ = [
    "DEVICE_SCALE",
    "IDLE_PERIOD_S",
    "MAX_CSS",
    "MAX_TEXELS",
    "MIN_CSS",
    "MIN_TEXELS",
    "MOVING_MAX_WIDTH",
    "HELD_QUALITY",
    "MOVING_QUALITY",
    "PANEL_FRAME_TYPE",
    "PANEL_MAX_FPS",
    "PanelStream",
    "TEXEL_STEPS",
    "css_size",
    "png_size",
    "texel_step",
]
