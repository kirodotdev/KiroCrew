"""The live hub: per-session state behind the chat's browser card.

One :class:`LiveHub` per gateway holds a :class:`LiveSession` per session key: the
turn it belongs to, the step strip, the card's state, the stream and the input
lock. The ``browser`` tool reports through :meth:`LiveHub.turn_started`,
:meth:`LiveHub.step` and :meth:`LiveHub.turn_ended`, asks
:meth:`LiveHub.in_control` before every op and holds on
:meth:`LiveHub.wait_for_release` while the person has the browser; the card's
routes (``dashboard/handlers/browser_live.py``) call the rest.

Two things leave the hub, both to OWNER websockets only, through the two senders
:meth:`LiveHub.bind` installs: the ``browser_live`` JSON event
(:func:`live_event`) on every state change, and the binary frame the stream
built. The hub imports nothing from the dashboard; the dashboard binds itself in.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kiro_crew.browser_harness.budgets import TAKEOVER_IDLE_S
from kiro_crew.browser_harness.screencast import (
    LiveConfig,
    LiveStream,
    Relay,
    TickStats,
)
from kiro_crew.browser_harness.takeover import (
    USER,
    InputLock,
    dispatch_input,
    focused_field,
    frame_scale,
    parse_input_event,
)

if TYPE_CHECKING:
    from kiro_crew.browser_harness.recording import LiveRecorder

logger = logging.getLogger(__name__)

#: Owner-websocket event name for a card state change.
LIVE_EVENT = "browser_live"

#: The card states, in the Wire contract's words.
STATE_WAITING_ALLOW = "waiting_allow"
STATE_LIVE = "live"
STATE_IN_CONTROL = "in_control"
STATE_DONE = "done"
STATE_LOGIN_NEEDED = "login_needed"
STATE_ERROR = "error"
STATES = frozenset(
    {
        STATE_WAITING_ALLOW,
        STATE_LIVE,
        STATE_IN_CONTROL,
        STATE_DONE,
        STATE_LOGIN_NEEDED,
        STATE_ERROR,
    }
)

#: Longest step label kept; the tool's labels are a few words.
MAX_STEP_LABEL = 120
#: Steps kept per turn; older ones fold into the count.
MAX_STEPS = 200

Publish = Callable[[str, Mapping[str, Any]], Awaitable[Any]]
SendFrame = Callable[[bytes], Awaitable[Any]]


def _set_event() -> asyncio.Event:
    ev = asyncio.Event()
    ev.set()
    return ev


@dataclass
class Step:
    label: str
    status: str = "active"  # done | active | pending
    #: Reported while a loop op (``reflex``) ran: the card may fold a run of these.
    loop: bool = False


@dataclass
class LiveSession:
    """Everything the card shows for one session, plus the stream feeding it."""

    session_key: str
    turn_id: str = ""
    state: str = ""
    url: str = ""
    title: str = ""
    browser: str = ""
    headless: bool = False
    steps: list[Step] = field(default_factory=list)
    lock: InputLock = field(default_factory=InputLock)
    stream: LiveStream | None = None
    visible: bool = True
    rung: int = 0
    fps: float = 0.0
    started_at: float = 0.0
    ended_at: float = 0.0
    #: The turn ended while the person held the browser: ``release`` folds the card.
    fold_on_release: bool = False
    #: The turn the person's claim was taken in, and when the card last showed it
    #: is open (claim, input, ack, scrolled into view). Together they bound a
    #: takeover that outlived its turn (:meth:`LiveHub._expire_stale_claim`).
    claim_turn: str = ""
    seen_at: float = 0.0
    #: The card's turn folded (:meth:`LiveHub._fold`); cleared by the next turn.
    ended: bool = False
    #: The agent tab this session's ops run on, and every tab it has owned. The op
    #: route binds before each op (``browser_ops._bind_target``), so two chats never
    #: drive one daemon tab and a takeover in one chat fences exactly that chat's tab.
    target_id: str = ""
    targets: set[str] = field(default_factory=set)
    #: The relay the last stream ran on, kept after the stream stops so the finished
    #: card's "Open in Chrome" can still activate the retained tab.
    last_relay: Relay | None = None
    #: The tab the last stream showed: what the finished card's "Open live" reopens.
    shown_target: str = ""
    #: A reopen found ``shown_target`` gone; cleared when a stream starts again.
    tab_closed: bool = False
    #: The op fence (see :meth:`LiveHub.op_fence`): held by the op route for the
    #: whole of bind + op, taken by :meth:`LiveHub.claim` so a takeover lands
    #: between ops, never inside one.
    op_fence: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: The op running under the fence right now, so a claim can cancel it.
    op_task: asyncio.Task[Any] | None = None
    #: Set by :meth:`LiveHub.claim` before it cancels ``op_task``: the route reads it
    #: to answer ``user_in_control`` instead of propagating the cancellation.
    op_interrupted: bool = False
    #: Set while the person does NOT hold the input lock; cleared by a claim and set
    #: again by a release or the turn's end. :meth:`LiveHub.wait_for_release` awaits
    #: it, which is how an agent op holds until the hand-back instead of failing.
    released: asyncio.Event = field(default_factory=_set_event)
    #: Steps reported so far (never reset) and the latest label: an op's replay
    #: event takes the label of the step it reported (:meth:`LiveHub.record_op`).
    step_seq: int = 0
    last_step: str = ""
    #: The loop op's counters for the card's loop line (:meth:`LiveHub.loop`), or
    #: ``None``. Kept after the loop stops, so the card says why, until the next op.
    loop: dict[str, Any] | None = None
    #: Actions a loop op recorded for the replay itself (never reset): its own
    #: outer op then records no summary event of its own (:meth:`LiveHub.record_op`).
    inner_seq: int = 0
    #: What the person said when they handed back (:meth:`LiveHub.release`), until
    #: the agent's parked op takes it (:meth:`LiveHub.take_handback`).
    handback: dict[str, str] | None = None
    #: An agent op of this turn was answered ``hold`` (:meth:`LiveHub.park`) and
    #: waits for the hand-back: its poll will take a note. Cleared by the hand-back
    #: and at the turn's start and end, so a note goes to the agent only while an
    #: op is there to take it.
    parked: bool = False
    #: The hand-back choices the agent's parked op offered, or ``[]``: the card then
    #: shows its own defaults (:meth:`LiveHub.set_handback_options`).
    handback_options: list[str] = field(default_factory=list)
    #: The pending quick ask (``browser_harness.quick_ask.QuickAsk``), or ``None``.
    ask: Any = None
    #: A scoped takeover's rect (x, y, w, h) in viewport CSS pixels: while set, card
    #: input outside it is dropped (:meth:`LiveHub.input_event`). Cleared on release.
    input_rect: tuple[float, float, float, float] | None = None

    @property
    def relay(self) -> Relay | None:
        return self.stream.relay if self.stream is not None else self.last_relay

    @property
    def turn_over(self) -> bool:
        """The turn this card belongs to has ended (folded, or held by the person past it).

        A stream running on a session whose turn is over is a REOPENED view: the
        person's own look at the tab, which no turn will fold (see
        :meth:`LiveHub.reopen`).
        """
        return self.ended or self.fold_on_release

    @property
    def tab(self) -> str:
        """``open`` / ``closed`` for the tab the card showed, ``""`` when it showed none.

        ``open`` is the relay's ledger still listing the tab (a relay with no ledger
        is taken at its word); a reopen that found it gone sets ``closed`` for good.
        """
        if not self.shown_target:
            return ""
        if self.stream is not None:
            return TAB_OPEN
        return (
            TAB_OPEN
            if not self.tab_closed and tab_alive(self.last_relay, self.shown_target)
            else TAB_CLOSED
        )


#: ``LiveSession.tab`` values, on the wire as the event's ``tab`` field.
TAB_OPEN = "open"
TAB_CLOSED = "closed"

#: :meth:`LiveHub.reopen` outcomes.
REOPEN_LIVE = "live"
REOPEN_TAB_GONE = "tab_gone"
REOPEN_LIVE_OFF = "live_off"


def tab_alive(relay: Relay | None, target_id: str) -> bool:
    """Whether *relay* can still reach *target_id*: connected, and the tab in its ledger.

    The ledger (``CdpRelay.agent_targets``) drops a tab on ``Target.targetDestroyed``,
    so a tab the person or the agent closed reads gone without a CDP round trip. A
    relay with no ledger (a test stand-in) is taken at its word.
    """
    if relay is None or not target_id or getattr(relay, "closed", False):
        return False
    ledger = getattr(relay, "agent_targets", None)
    if not callable(ledger):
        return True
    try:
        return target_id in ledger()
    except Exception:  # noqa: BLE001 - a ledger that cannot be read is no proof the tab lives
        return False


def live_event(sess: LiveSession) -> dict[str, Any]:
    """The ``browser_live`` payload: exactly the Wire-contract fields."""
    active = next((i for i, s in enumerate(sess.steps) if s.status == "active"), None)
    step = (
        {"index": active, "label": sess.steps[active].label}
        if active is not None
        else {"index": len(sess.steps) - 1, "label": sess.steps[-1].label} if sess.steps else None
    )
    return {
        "session_key": sess.session_key,
        "turn_id": sess.turn_id,
        "state": sess.state,
        "url": sess.url,
        "title": sess.title,
        "browser": sess.browser,
        "headless": bool(sess.headless),
        "step": step,
        "steps": [_step_wire(s) for s in sess.steps],
        "rung": int(sess.rung),
        "fps": float(sess.fps),
        "turn_over": bool(sess.turn_over),
        "tab": sess.tab,
        "loop": dict(sess.loop) if sess.loop is not None else None,
        "handback_options": list(sess.handback_options),
        "ask": sess.ask.wire() if sess.ask is not None and not sess.ask.outcome else None,
    }


#: The person's hand-back note: their own words, at most this many characters.
HANDBACK_NOTE_MAX = 500
#: A hand-back choice (an agent option or the card's default id), and how many
#: options an agent may offer.
HANDBACK_CHOICE_MAX = 40
HANDBACK_OPTIONS_MAX = 4
#: :meth:`LiveHub.release` outcomes for a note or choice: the agent's op takes it,
#: or (no turn left to take it) the card sends it as the person's next message.
DELIVERED_AGENT = "agent"
DELIVERED_CHAT = "chat"
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def handback_text(value: Any, limit: int) -> str | None:
    """A note or choice as it may be kept: a string within *limit*, control
    characters dropped, ends trimmed; ``None`` for anything else or too long."""
    if not isinstance(value, str):
        return None
    text = _CONTROL_RE.sub("", value).strip()
    if len(text) > limit:
        return None
    return text


def handback_options(raw: Any) -> list[str]:
    """The agent's offered choices: up to four short distinct strings, else ``[]``."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw[: HANDBACK_OPTIONS_MAX * 2]:
        text = handback_text(item, HANDBACK_CHOICE_MAX)
        if text and text not in out:
            out.append(text)
        if len(out) == HANDBACK_OPTIONS_MAX:
            break
    return out


def _inside_scope(event: Any, rect: tuple[float, float, float, float], scale: float) -> bool:
    """Whether a scoped takeover lets *event* through: only a pointer inside *rect*
    (viewport CSS pixels; the event is in frame pixels at the stream's current
    *scale*). Never a key -- Space or Enter would act on whatever holds focus, and
    arrows or PageDown scroll the page under the box -- and never a wheel."""
    if event.type != "mouse":
        return False
    s = scale if scale > 0 else 1.0
    x, y, w, h = rect
    return x <= event.x / s <= x + w and y <= event.y / s <= y + h


def _step_wire(step: Step) -> dict[str, Any]:
    out: dict[str, Any] = {"label": step.label, "status": step.status}
    if step.loop:
        out["loop"] = True
    return out


_LOOP_STOP_RE = re.compile(r"[a-z_]{1,32}")
_LOOP_INTS = ("moves", "steps", "max_steps")
_LOOP_FLOATS = ("rate", "seconds", "max_seconds")


def _count(value: Any) -> float | None:
    """A finite, non-negative number, else ``None`` (a bool is not a count)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def loop_info(raw: Any) -> dict[str, Any] | None:
    """The ``loop`` wire field from what a loop op reported, or ``None`` for junk.

    ``stop`` is one of the loop op's own short tokens (``browser_harness.reflex``:
    ``done``, ``until``, ``stuck``, ``risk``, ``budget``, ``user_in_control``, and
    the rarer ones it names); the card has a sentence for each it knows. Every key
    is present; a counter the op did not send (or sent malformed) is ``null``.
    ``stop`` is ``null`` while the loop runs.
    """
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, Any] = {}
    for key in _LOOP_INTS:
        n = _count(raw.get(key))
        out[key] = int(n) if n is not None else None
    for key in _LOOP_FLOATS:
        n = _count(raw.get(key))
        out[key] = round(n, 2) if n is not None else None
    stop = raw.get("stop")
    out["stop"] = stop if isinstance(stop, str) and _LOOP_STOP_RE.fullmatch(stop) else None
    return out


class _StreamSink:
    """The stream's :class:`FrameSink`, bound to one session of the hub.

    Also the stream's recorder seam (``records`` / ``record``): with the hub's
    replay recorder attached and on, every frame is offered to it with its own
    password boxes and mask status (``browser_harness.recording``).
    """

    def __init__(self, hub: "LiveHub", sess: LiveSession) -> None:
        self._hub = hub
        self._sess = sess

    async def frame(self, header: Mapping[str, Any], blob: bytes) -> None:
        await self._hub._send_frame_blob(self._sess, blob)

    @property
    def records(self) -> bool:
        rec = self._hub.recorder
        return rec is not None and rec.turn_open(self._sess.session_key)

    def note_size(self, width: int, height: int) -> None:
        rec = self._hub.recorder
        if rec is not None:
            rec.note_size(self._sess.session_key, width, height)

    def wants_frame(self) -> bool:
        rec = self._hub.recorder
        return rec is not None and rec.wants_frame(self._sess.session_key)

    def record(
        self,
        jpeg: bytes,
        width: int,
        height: int,
        boxes: list[list[int]],
        mask_ok: bool,
        age_s: float = 0.0,
    ) -> None:
        rec = self._hub.recorder
        if rec is not None:
            rec.frame(self._sess.session_key, jpeg, width, height, boxes, mask_ok, age_s=age_s)


@dataclass(frozen=True)
class OpMark:
    """Where an op began, for its replay event: when, the step count, the page."""

    ts_ms: int
    step_seq: int
    url: str
    inner_seq: int = 0


class LiveHub:
    """Per-gateway registry of live sessions. Construct one; bind the dashboard in."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sessions: dict[str, LiveSession] = {}
        self._publish: Publish | None = None
        self._send_frame: SendFrame | None = None
        self._config = LiveConfig()
        self._clock = clock
        #: The one sleep the hold's bound runs on (tests inject one that returns
        #: at once, or never).
        self._sleep = sleep
        self._tasks: set[asyncio.Task[Any]] = set()
        #: The turn-replay recorder (``browser_harness.recording``); ``None`` records
        #: nothing. The dashboard attaches the gateway's one at startup.
        self.recorder: LiveRecorder | None = None

    # ── wiring ─────────────────────────────────────────────────────────────

    def bind(self, publish: Publish | None, send_frame: SendFrame | None) -> None:
        """Install the owner-only senders (JSON events, binary frames)."""
        self._publish = publish
        self._send_frame = send_frame

    def attach_recorder(self, recorder: LiveRecorder | None) -> None:
        self.recorder = recorder

    def configure(self, cfg: Any) -> None:
        """Read ``browser.live.*`` (and ``browser.recording.*``) from a loaded config.

        Applies to streams started after; the recorder's caps apply at its next write.
        """
        self._config = LiveConfig.from_config(cfg)
        if self.recorder is not None:
            self.recorder.configure(cfg)

    @property
    def config(self) -> LiveConfig:
        return self._config

    def get(self, session_key: str) -> LiveSession | None:
        return self._sessions.get(session_key)

    def _session(self, session_key: str) -> LiveSession:
        sess = self._sessions.get(session_key)
        if sess is None:
            sess = LiveSession(session_key=session_key)
            self._sessions[session_key] = sess
        return sess

    # ── the tool's side ────────────────────────────────────────────────────

    def turn_started(self, session_key: str, turn_id: str) -> None:
        """A turn began browsing: reset the strip; the card attaches to this turn."""
        sess = self._session(session_key)
        self._expire_stale_claim(sess)
        sess.turn_id = str(turn_id)
        sess.steps = []
        sess.loop = None
        sess.handback = None
        sess.parked = False
        sess.handback_options = []
        sess.started_at = self._clock()
        sess.ended_at = 0.0
        sess.ended = False
        # A reopened view (or a takeover held past the last turn) joins this turn:
        # the stream keeps running and this turn's end is what folds it.
        sess.fold_on_release = False
        if sess.state in ("", STATE_DONE):
            sess.state = ""
        if self.recorder is not None:
            self.recorder.turn_started(session_key, sess.turn_id, sess.url)
        self._emit(sess)

    def step(self, session_key: str, label: str) -> None:
        """The agent is on a new step: the previous active one is done."""
        sess = self._session(session_key)
        text = str(label or "")[:MAX_STEP_LABEL]
        for s in sess.steps:
            if s.status == "active":
                s.status = "done"
        running = sess.loop is not None and sess.loop.get("stop") is None
        sess.steps.append(Step(label=text, loop=running))
        sess.step_seq += 1
        sess.last_step = text
        if len(sess.steps) > MAX_STEPS:
            del sess.steps[: len(sess.steps) - MAX_STEPS]
        self._emit(sess)

    def turn_ended(self, session_key: str) -> None:
        """The turn is over: the stream stops, the lock opens, the card folds to one line.

        Unless the person holds the browser. An agent that cannot wait for the
        hand-back ends its turn while they are mid-takeover; folding the card then
        would stop the stream under their hands and throw their strokes away. The
        card stays theirs, and :meth:`release` folds it the moment they hand back.
        """
        sess = self._sessions.get(session_key)
        if sess is None:
            return
        if sess.ended:
            # This card's turn already ended. The chat runner calls this at the end of
            # EVERY turn, so a later turn that never browsed must not fold a view the
            # person reopened (:meth:`reopen`); only :meth:`close_view` does.
            return
        for s in sess.steps:
            if s.status == "active":
                s.status = "done"
        if sess.lock.held() and sess.lock.owner == USER:
            sess.fold_on_release = True
            self._emit(sess)
            return
        self._fold(sess)

    def _fold(self, sess: LiveSession) -> None:
        if sess.ask is not None:
            # The turn is over: nobody is left to read the answer.
            sess.ask.settle("cancelled")
            sess.ask = None
        sess.input_rect = None
        sess.fold_on_release = False
        sess.parked = False
        sess.ended = True
        sess.lock.release()
        sess.released.set()
        sess.ended_at = self._clock()
        sess.state = STATE_DONE
        stream, sess.stream = sess.stream, None
        if stream is not None:
            self._spawn(stream.stop())
        if self.recorder is not None:
            self.recorder.turn_ended(sess.session_key)
        self._emit(sess)

    # ── the replay recorder's events ───────────────────────────────────────

    def op_mark(self, session_key: str) -> OpMark:
        """Note where an op begins; :meth:`record_op` turns it into its replay event."""
        sess = self._session(session_key)
        if sess.loop is not None:
            # The loop line belongs to the op that ran it; the next op is a new step.
            sess.loop = None
            self._emit(sess)
        return self.inner_mark(session_key)

    def inner_mark(self, session_key: str) -> OpMark:
        """:meth:`op_mark` for an action a loop op runs inside itself."""
        from kiro_crew.browser_harness.recording import now_ms

        sess = self._session(session_key)
        return OpMark(
            ts_ms=now_ms(), step_seq=sess.step_seq, url=sess.url, inner_seq=sess.inner_seq
        )

    def loop(self, session_key: str, raw: Any) -> None:
        """A loop op's counters (``reflex``): the card's loop line and progress bar."""
        info = loop_info(raw)
        if info is None:
            return
        sess = self._session(session_key)
        sess.loop = info
        self._emit(sess)

    def record_op(
        self,
        session_key: str,
        op: str,
        args: Mapping[str, Any] | None,
        result: Mapping[str, Any] | None,
        mark: OpMark,
        *,
        loop: bool = False,
    ) -> None:
        """Record one agent op, generically, from the step it reported and its result.

        *loop* marks an action a loop op ran inside itself (``reflex``): it is its
        own replay event, tagged ``loop``. A loop op that recorded such actions
        records nothing for itself, since its steps already are the timeline.
        """
        rec = self.recorder
        sess = self._sessions.get(session_key)
        if rec is None or sess is None:
            return
        if loop:
            sess.inner_seq += 1
        elif sess.inner_seq > mark.inner_seq:
            return
        from kiro_crew.browser_harness.recording import agent_event

        label = sess.last_step if sess.step_seq > mark.step_seq else ""
        stream = sess.stream
        scale = float(getattr(stream, "scale", 1.0) or 1.0) if stream is not None else 1.0
        try:
            event = agent_event(
                op, args, result, ts=mark.ts_ms, label=label, url=mark.url, scale=scale
            )
        except Exception:  # noqa: BLE001 - a replay event never fails the op
            logger.debug("browser live: replay event not built", exc_info=True)
            return
        if loop:
            event["loop"] = True
        rec.event(session_key, event)

    def in_control(self, session_key: str) -> bool:
        """Whether the person holds the input lock; an agent op holds until they let go."""
        sess = self._sessions.get(session_key)
        if sess is None:
            return False
        self._expire_stale_claim(sess)
        return sess.lock.held()

    def _expire_stale_claim(self, sess: LiveSession) -> bool:
        """Hand back a takeover that outlived its turn and whose card went quiet.

        Applies only to a claim held by the person past the turn it was taken in
        (``turn_over``, or a later turn has started) with no sign of the card for
        ``TAKEOVER_IDLE_S``. Folds the card exactly as the hand-back would. A claim
        inside its own running turn is never expired here: the agent's hold
        (``HOLD_TIMEOUT_S``) bounds that one. Checked lazily wherever the agent
        side asks who holds the browser, so a dead card frees the chat on the
        next op or hand-back poll. Returns whether it folded.
        """
        if not (sess.lock.held() and sess.lock.owner == USER):
            return False
        if not (sess.turn_over or sess.claim_turn != sess.turn_id):
            return False
        if self._clock() - sess.seen_at < TAKEOVER_IDLE_S:
            return False
        logger.info("browser live: handing back an idle takeover of %s", sess.session_key)
        self._fold(sess)
        return True

    async def wait_for_release(self, session_key: str, timeout: float) -> bool:
        """Wait until the person hands the browser back, at most *timeout* seconds.

        ``True`` when the lock is free (at once if it already is), ``False`` when the
        bound passed with the person still in control. The op route holds an agent
        op on this instead of answering ``user_in_control`` at once, so the agent
        resumes by itself after the hand-back. Cancellable like any await; the bound
        runs on the hub's injected sleep, never on a blocking call.
        """
        sess = self._session(session_key)
        self._expire_stale_claim(sess)
        if not sess.lock.held():
            return True
        waiter = asyncio.ensure_future(sess.released.wait())
        timer = asyncio.ensure_future(self._sleep(timeout))
        try:
            await asyncio.wait({waiter, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (waiter, timer):
                if not t.done():
                    t.cancel()
        self._expire_stale_claim(sess)
        return not sess.lock.held()

    # ── the op fence (shared by the op route and the person's claim) ───────

    def op_fence(self, session_key: str) -> asyncio.Lock:
        """The per-session fence the op route holds from bind to the op's end.

        :meth:`claim` takes the same fence, so a takeover is confirmed only once the
        op in flight has drained (or been cancelled by the claim), and the route's
        ``in_control`` check at entry is then the whole story: no op can start
        while the person holds the lock, and none is still running when they get it.
        """
        return self._session(session_key).op_fence

    def op_started(self, session_key: str, task: asyncio.Task[Any]) -> None:
        """The route created the op's task under the fence; a claim may cancel it."""
        sess = self._session(session_key)
        sess.op_task = task
        sess.op_interrupted = False

    def op_finished(self, session_key: str) -> bool:
        """The route left the fence. Returns whether a claim interrupted the op."""
        sess = self._session(session_key)
        sess.op_task = None
        interrupted, sess.op_interrupted = sess.op_interrupted, False
        return interrupted

    # ── the tab binding (the op route's side) ──────────────────────────────

    def target_of(self, session_key: str) -> str:
        """The agent tab this session's ops run on, or ``""`` before the first bind."""
        sess = self._sessions.get(session_key)
        return sess.target_id if sess is not None else ""

    def targets_of(self, session_key: str) -> set[str]:
        """Every agent tab this session has owned (the ``tabs`` op's universe)."""
        sess = self._sessions.get(session_key)
        return set(sess.targets) if sess is not None else set()

    def bind_target(self, session_key: str, target_id: str) -> None:
        """Make *target_id* this session's current tab, and one of its own."""
        sess = self._session(session_key)
        sess.target_id = str(target_id)
        sess.targets.add(sess.target_id)

    def owner_of_target(self, target_id: str) -> str | None:
        """The session key that owns *target_id*, if any session does."""
        for key, sess in self._sessions.items():
            if target_id in sess.targets:
                return key
        return None

    # ── the supervisor's side ──────────────────────────────────────────────

    def set_state(
        self,
        session_key: str,
        state: str,
        *,
        url: str | None = None,
        title: str | None = None,
        browser: str | None = None,
        headless: bool | None = None,
    ) -> None:
        """A state the supervisor knows (waiting_allow, login_needed, error) or a page change."""
        if state not in STATES:
            raise ValueError(f"unknown live state {state!r}")
        sess = self._session(session_key)
        if state == STATE_IN_CONTROL and not sess.lock.held():
            state = STATE_LIVE
        sess.state = state
        self._update_page(sess, url=url, title=title, browser=browser, headless=headless)
        self._emit(sess)

    def page_changed(self, session_key: str, *, url: str = "", title: str = "") -> None:
        sess = self._session(session_key)
        self._update_page(sess, url=url, title=title)
        if self.recorder is not None and url:
            self.recorder.note_url(session_key, url)
        self._emit(sess)

    @staticmethod
    def _update_page(
        sess: LiveSession,
        *,
        url: str | None = None,
        title: str | None = None,
        browser: str | None = None,
        headless: bool | None = None,
    ) -> None:
        if url is not None:
            sess.url = str(url)[:2048]
        if title is not None:
            sess.title = str(title)[:300]
        if browser is not None:
            sess.browser = str(browser)
        if headless is not None:
            sess.headless = bool(headless)

    async def start_stream(
        self,
        session_key: str,
        relay: Relay,
        target_id: str,
        *,
        browser: str,
        headless: bool,
        url: str = "",
        title: str = "",
        stream_factory: Callable[..., LiveStream] | None = None,
    ) -> LiveStream | None:
        """Start the screencast of the agent's target for this session's card.

        Returns ``None`` when the live view is off in config. Replaces a stream that
        is already running (the agent moved to another tab).
        """
        sess = self._session(session_key)
        self._update_page(sess, url=url, title=title, browser=browser, headless=headless)
        if self.recorder is not None and url:
            self.recorder.note_url(session_key, url)
        if not self._config.enabled:
            # Live view off: no stream, no state, no event -- a card that could
            # only ever wait for a frame is not announced (see ``_emit_now``).
            return None
        old, sess.stream = sess.stream, None
        if old is not None:
            await old.stop()
        factory = stream_factory or LiveStream
        stream = factory(
            relay,
            target_id,
            _StreamSink(self, sess),
            session_key=session_key,
            config=self._config,
            on_tick=self._tick_for(sess),
        )
        stream.visible = sess.visible
        sess.last_relay = relay
        # Installed only once it runs: a stream whose ``start()`` raised (the target
        # closed mid-navigation) would otherwise sit on the session with the right
        # ``target_id`` and stop every later op from starting a working one.
        await stream.start()
        sess.stream = stream
        sess.shown_target = str(target_id)
        sess.tab_closed = False
        if sess.lock.held():
            await stream.set_takeover(True)
        sess.rung = stream.rung
        sess.state = STATE_IN_CONTROL if sess.lock.held() else STATE_LIVE
        self._emit(sess)
        return stream

    async def stop_stream(self, session_key: str) -> None:
        sess = self._sessions.get(session_key)
        if sess is None or sess.stream is None:
            return
        stream, sess.stream = sess.stream, None
        await stream.stop()

    def _tick_for(self, sess: LiveSession) -> Callable[[TickStats], Awaitable[None]]:
        async def _on_tick(stats: TickStats) -> None:
            changed = stats.rung != sess.rung or round(stats.fps) != round(sess.fps)
            sess.rung, sess.fps = stats.rung, stats.fps
            if changed:
                await self._emit_now(sess)

        return _on_tick

    # ── the card's side ────────────────────────────────────────────────────

    async def set_visible(self, session_key: str, visible: bool) -> None:
        sess = self._session(session_key)
        sess.visible = bool(visible)
        if sess.visible:
            sess.seen_at = self._clock()
        if sess.stream is not None:
            await sess.stream.set_visible(sess.visible)

    def ack(self, session_key: str, seq: int, stream: str | None = None) -> bool:
        """The card received frame ``seq`` of ``stream``.

        The frame header names its stream (``frame_header``) and ``seq`` restarts at 1
        with every stream, so an ack that names another stream (a late ack for the
        stream a tab switch just replaced) must not release the new stream's frame
        of the same number early. A caller that names no stream acks the current one.
        """
        sess = self._sessions.get(session_key)
        if sess is None or sess.stream is None:
            return False
        sess.seen_at = self._clock()
        if stream is not None and stream != getattr(sess.stream, "stream_id", stream):
            return False
        return sess.stream.ack(int(seq))

    async def claim(self, session_key: str) -> bool:
        """The person takes input. ``False`` only when someone else holds it.

        The lock is taken first, so the op route refuses anything that arrives from
        now on; then the op in flight (if any) is cancelled and the fence awaited,
        so the takeover is confirmed only once the agent has stopped touching the
        tab. The state is published ``in_control`` only if the lock is still held
        after the awaits: an Esc that lands in between has already said ``live``.
        """
        sess = self._session(session_key)
        if sess.state in (STATE_DONE, STATE_ERROR):
            # A finished card has no browser to take: the stream is gone and the
            # agent is not driving anything to be fenced off.
            raise LookupError("turn_over")
        if not sess.lock.claim(USER):
            return False
        sess.claim_turn = sess.turn_id
        sess.seen_at = self._clock()
        sess.released.clear()
        task = sess.op_task
        if task is not None and not task.done():
            sess.op_interrupted = True
            task.cancel()
        async with sess.op_fence:
            pass
        if not sess.lock.held():
            return True
        sess.state = STATE_IN_CONTROL
        if sess.stream is not None:
            await sess.stream.set_takeover(True)
        if sess.lock.held():
            await self._emit_now(sess)
        return True

    async def release(self, session_key: str, *, note: str = "", choice: str = "") -> str:
        """Hand back, with what the person said (already checked by the route).

        A *note* or *choice* goes to the agent's op parked on this takeover
        (:meth:`park`), whose hand-back poll takes it: ``"agent"``. With no op
        parked (the turn ended, or the agent is between browser ops and may end
        its turn without another), the card sends it as the person's next chat
        message instead -- queued behind a running turn like anything typed -- so
        it is never held for an op that may not come: ``"chat"``. Nothing said:
        ``""``. The note is recorded on the replay as the person's event.
        """
        sess = self._sessions.get(session_key)
        if sess is None:
            return ""
        said = bool(note or choice)
        if said:
            self._record_handback(sess, note, choice)
        was_held = sess.lock.held()
        if was_held and sess.fold_on_release:
            # The agent's turn ended while the person held the browser: this hand-back
            # is what folds the card (see ``turn_ended``).
            if sess.stream is not None:
                await sess.stream.set_takeover(False)
            self._fold(sess)
            return DELIVERED_CHAT if said else ""
        waiting, sess.parked = sess.parked, False
        if said and waiting and not sess.turn_over and sess.handback is None:
            sess.handback = {"choice": choice, "note": note}
            delivered = DELIVERED_AGENT
        elif said:
            delivered = DELIVERED_CHAT
        else:
            delivered = ""
        sess.handback_options = []
        sess.input_rect = None
        sess.lock.release()
        sess.released.set()
        if sess.stream is not None:
            await sess.stream.set_takeover(False)
        if sess.state == STATE_IN_CONTROL:
            sess.state = STATE_LIVE
        if was_held:
            await self._emit_now(sess)
        return delivered

    def park(self, session_key: str) -> None:
        """An agent op was answered ``hold``: it waits for this takeover's hand-back."""
        sess = self._sessions.get(session_key)
        if sess is not None and sess.lock.held():
            sess.parked = True

    def take_handback(self, session_key: str) -> dict[str, str] | None:
        """What the person said at the hand-back, once: the agent's op takes it."""
        sess = self._sessions.get(session_key)
        if sess is None or sess.handback is None:
            return None
        said, sess.handback = sess.handback, None
        return said

    def set_handback_options(self, session_key: str, raw: Any) -> None:
        """The choices the agent's parked op offers (see :func:`handback_options`)."""
        sess = self._sessions.get(session_key)
        options = handback_options(raw)
        if sess is None or options == sess.handback_options:
            return
        sess.handback_options = options
        self._emit(sess)

    async def ask_changed(self, session_key: str) -> None:
        """Publish the card's ``ask`` field after a quick ask was set, moved or ended."""
        sess = self._sessions.get(session_key)
        if sess is not None:
            await self._emit_now(sess)

    def _record_handback(self, sess: LiveSession, note: str, choice: str) -> None:
        rec = self.recorder
        if rec is None:
            return
        from kiro_crew.browser_harness.recording import now_ms, user_event

        said = " - ".join(t for t in (choice, note) if t)
        try:
            rec.event(
                sess.session_key,
                user_event("other", now_ms(), sess.url, label=said),
            )
        except Exception:  # noqa: BLE001 - a replay event never fails the hand-back
            logger.debug("browser live: hand-back event not recorded", exc_info=True)

    async def input_event(self, session_key: str, event: dict[str, Any]) -> int:
        """Dispatch one card event. Raises ``InputEventError`` or ``LookupError``."""
        sess = self._sessions.get(session_key)
        if sess is None or not sess.lock.held() or sess.lock.owner != USER:
            raise LookupError("not_in_control")
        sess.seen_at = self._clock()
        stream = sess.stream
        if stream is None or not stream.session_id:
            raise LookupError("no_stream")
        parsed = parse_input_event(event)
        # The ratio of the frame the card drew, not of the stream's newest frame
        # (see ``takeover.frame_scale``).
        scale = frame_scale(
            parsed,
            float(getattr(stream, "device_width", 0.0) or 0.0),
            float(getattr(stream, "scale", 1.0) or 1.0),
        )
        if sess.input_rect is not None and not _inside_scope(parsed, sess.input_rect, scale):
            # A scoped takeover (a human_check): only the widget's box takes input.
            return 0
        sent = await dispatch_input(stream.relay, stream.session_id, event, scale=scale)
        stream.note_input()
        if self.recorder is not None:
            # Validated already (parse_input_event raised otherwise). The recorder keeps
            # no typed text: keystrokes count as characters, never as what was typed.
            self.recorder.user_input(session_key, parsed, sess.url)
        return sent

    async def focused_field(self, session_key: str) -> dict[str, Any]:
        """The page's focused field kind while the person holds input (see ``takeover``)."""
        sess = self._sessions.get(session_key)
        if sess is None or sess.lock.owner != USER or sess.stream is None:
            return {"editable": False, "kind": "", "mode": ""}
        stream = sess.stream
        if not stream.session_id:
            return {"editable": False, "kind": "", "mode": ""}
        return await focused_field(stream.relay, stream.session_id)

    async def reopen(
        self,
        session_key: str,
        relay: Relay | None = None,
        *,
        stream_factory: Callable[..., LiveStream] | None = None,
    ) -> str:
        """The finished card's "Open live": stream the tab it showed again.

        The screencast restarts on ``shown_target`` through *relay* (the gateway's
        current one; the last stream's when ``None``) and the card is ``live`` again
        under the SAME ``turn_id``, so the card that folded is the card that grows
        back, and Take over / Hand back work on it exactly as during a turn. The
        session's turn stays over: a later turn that never browses does not fold
        the view (:meth:`turn_ended`), a turn that browses joins it, and
        :meth:`close_view` folds it on the person's word.

        Fenced like a claim: under the session's op fence, so it never lands inside
        an op. It moves no daemon tab and runs no op, so it does not take the
        gateway-wide op lock. Refuses a tab another session owns, and a tab the
        relay does not list (``tab_gone``, published as ``tab: closed``).
        """
        sess = self._sessions.get(session_key)
        if sess is None or not sess.shown_target:
            return REOPEN_TAB_GONE
        if sess.stream is not None:
            return REOPEN_LIVE
        if not sess.turn_over:
            # A turn that has not ended is the agent's: its own ops start the stream.
            raise LookupError("turn_running")
        if not self._config.enabled:
            return REOPEN_LIVE_OFF
        relay = relay if relay is not None else sess.last_relay
        target = sess.shown_target
        if not tab_alive(relay, target) or self._owned_elsewhere(session_key, target):
            return self._tab_gone(sess)
        assert relay is not None  # tab_alive is False without one
        async with sess.op_fence:
            if sess.stream is not None:
                return REOPEN_LIVE
            if self.recorder is not None:
                # The reopened view is the same card: it records into the same turn,
                # which :meth:`close_view` (through ``_fold``) ends again.
                self.recorder.turn_started(session_key, sess.turn_id, sess.url)
            try:
                stream = await self.start_stream(
                    session_key,
                    relay,
                    target,
                    browser=sess.browser,
                    headless=sess.headless,
                    stream_factory=stream_factory,
                )
            except Exception:  # noqa: BLE001 - the tab closed between the check and the attach
                logger.debug("browser live: reopen could not attach", exc_info=True)
                if self.recorder is not None:
                    self.recorder.turn_ended(session_key)
                return self._tab_gone(sess)
            if stream is None and self.recorder is not None:
                self.recorder.turn_ended(session_key)
        return REOPEN_LIVE if stream is not None else REOPEN_LIVE_OFF

    def _owned_elsewhere(self, session_key: str, target_id: str) -> bool:
        return any(
            key != session_key and target_id in other.targets
            for key, other in self._sessions.items()
        )

    def _tab_gone(self, sess: LiveSession) -> str:
        sess.tab_closed = True
        self._emit(sess)
        return REOPEN_TAB_GONE

    async def close_view(self, session_key: str) -> bool:
        """Fold a view whose turn is over (a reopened one, or a takeover held past it).

        Stops the stream, opens the lock and publishes ``done``. ``False`` when there
        is nothing to fold: no session, already done, or a turn still running (that
        card folds at the turn's end, never on this route).
        """
        sess = self._sessions.get(session_key)
        if sess is None or sess.state == STATE_DONE or not sess.turn_over:
            return False
        if sess.stream is not None and sess.lock.held():
            await sess.stream.set_takeover(False)
        self._fold(sess)
        return True

    async def open_in_browser(self, session_key: str) -> bool:
        """Focus the real tab the card shows or showed.

        ``Target.activateTarget`` on the session's tab goes through the relay's flat
        connection and needs no screencast: a finished turn (``turn_ended`` dropped
        the stream, the tab stays open in the person's Chrome) is still focusable.
        ``Page.bringToFront`` is added while a stream session is attached.
        """
        sess = self._sessions.get(session_key)
        if sess is None:
            return False
        stream = sess.stream
        target = stream.target_id if stream is not None else sess.target_id
        relay = sess.relay
        if relay is None or not target or getattr(relay, "closed", False):
            return False
        try:
            await relay.send("Target.activateTarget", {"targetId": target})
            if stream is not None and stream.session_id:
                await relay.send("Page.bringToFront", {}, stream.session_id)
        except Exception:
            logger.debug("open-in-browser failed", exc_info=True)
            return False
        return True

    # ── delivery ───────────────────────────────────────────────────────────

    async def _send_frame_blob(self, sess: LiveSession, blob: bytes) -> None:
        if self._send_frame is None:
            return
        try:
            await self._send_frame(blob)
        except Exception:
            logger.debug("live frame send failed", exc_info=True)

    def _publishable(self, sess: LiveSession) -> bool:
        """A state to show, a sender to show it through, and the live view turned on.

        ``browser.live.enabled=false`` is "no card": with no event the dashboard never
        mounts one, while the browser ops keep running underneath.
        """
        return self._publish is not None and bool(sess.state) and self._config.enabled

    async def _emit_now(self, sess: LiveSession) -> None:
        publish = self._publish
        if publish is None or not self._publishable(sess):
            return
        try:
            await publish(LIVE_EVENT, live_event(sess))
        except Exception:
            logger.debug("browser_live publish failed", exc_info=True)

    def _emit(self, sess: LiveSession) -> None:
        """Publish from a sync caller: scheduled on the running loop, dropped without one."""
        if not self._publishable(sess):
            return
        self._spawn(self._emit_now(sess))

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        task: asyncio.Task[Any] = loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        """Stop every stream (gateway shutdown)."""
        for sess in list(self._sessions.values()):
            stream, sess.stream = sess.stream, None
            if stream is not None:
                try:
                    await stream.stop()
                except Exception:
                    logger.debug("live stream stop failed", exc_info=True)
        for task in list(self._tasks):
            task.cancel()


_HUB: LiveHub | None = None


def get_live_hub() -> LiveHub:
    """The gateway's one hub, created on first use."""
    global _HUB
    if _HUB is None:
        _HUB = LiveHub()
    return _HUB


def reset_live_hub() -> None:
    """Drop the singleton (tests)."""
    global _HUB
    _HUB = None


__all__ = [
    "LIVE_EVENT",
    "LiveHub",
    "LiveSession",
    "OpMark",
    "STATES",
    "STATE_DONE",
    "STATE_ERROR",
    "STATE_IN_CONTROL",
    "STATE_LIVE",
    "STATE_LOGIN_NEEDED",
    "STATE_WAITING_ALLOW",
    "Step",
    "get_live_hub",
    "live_event",
    "reset_live_hub",
]
