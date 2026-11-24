"""A panel's pace rule: a picture is late only past the panel's own round trip.

A chat card's ladder (:class:`screencast.AdaptiveController`) counts a frame as
late when its whole delivery, from the send to the card's ack, is longer than
1.5 periods. A panel's delivery also holds the round trip to the XR page and its
decode. On a far link that alone is 100 to 300 ms, so every picture counted as
late while the link still had room.

A panel's picture is late only when its delivery goes past its floor: the lowest
recent delivery of a picture at least half its size, which is the round trip
with such a picture on it. A smaller picture comes back sooner and is no floor
for a bigger one (the nearly blank page of a load). The floor never counts as
more than :data:`PANEL_FLOOR_CAP_S`, so a decode that is slow from the first
picture, or a queue that stands from the first, is late by what it takes past
that, and so is a picture with no recent one of half its size behind it.

A picture's busy time runs from the later of its send and the decoded ack of the
picture before it, to its own ack. With several pictures in flight a whole delivery
also holds the wait behind the ones ahead, so bytes over whole deliveries read the
link at a fraction of its rate. But one picture at a time, the busy time is the whole
round: the ladder then reads the round's delay, not the link, and on a far link it
stops at 10 a second however much the link carries. So the ladder is handed each
picture's link time (:func:`link_time`): the share of the round that pictures in
flight cannot hide, or the time it stood in line, whichever is longer. A chat
card's ladder is not touched: only a panel's pace reads this.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .screencast import (
    CALM_WINDOWS_UP,
    DOWN_UTILISATION,
    FPS_STEPS,
    LADDER,
    LATE_PERIODS,
    LATE_SHARE_DOWN,
    UP_HEADROOM,
    AdaptiveController,
    TickStats,
)

#: A picture counts as late (and steps the pace down) only when its delivery goes
#: past the floor by this long, or by 1.5 of its periods, whichever is longer.
PANEL_LATE_S = 0.1
#: How many recent deliveries the floor is the lowest of.
PANEL_FLOOR_PICTURES = 30
#: The most of a delivery that counts as round trip: a far link with a picture on it.
PANEL_FLOOR_CAP_S = 0.25
#: A recent picture is a floor for this one only when it had at least this share of its bytes.
PANEL_FLOOR_SIZE_SHARE = 0.5


#: The most pictures of one panel on their way to the XR page at once. A round of
#: 94 ms holds three periods of the panel's top (30 a second) and a round of 150 ms
#: holds four and a half, so four in flight reach 24 a second or more at both.
PANEL_IN_FLIGHT = 4
#: The most on their way while the aim stands on the ladder's lowest step.
PANEL_IN_FLIGHT_BOTTOM = 2
#: The most picture bytes on their way on one owner socket for one more picture of a
#: panel to go. The socket also carries the room's own messages (the slots, the asks),
#: which wait behind them. The release rule (``PanelStream._released``) counts the
#: picture just sent twice: once on the socket, and once more as the size of the next
#: picture that a release would let the browser make. So pictures of 160 kB go four at
#: a time, of 213 kB three, of 320 kB two, and a larger one goes alone
#: (:func:`in_flight`).
PANEL_FLIGHT_BYTES = 640_000
#: The most picture bytes on their way on one owner socket, every panel's together:
#: three panels with their own :data:`PANEL_FLIGHT_BYTES` each. Past it a panel sends
#: one picture at a time (``PanelStream._released``).
PANEL_SOCKET_BYTES = 3 * PANEL_FLIGHT_BYTES
#: A panel's own step between the ladder's 20 and 30. Four pictures in flight give
#: 26.7 a second at a round of 150 ms: under the 30 that the ladder's next step asks
#: for, so without this step such a round stood at 20. The ladder's steps are the chat
#: card's too (``screencast.FPS_STEPS``) and are left as they are: the panel stands on
#: the ladder's 30 and paces itself at this (:meth:`PanelController.aim`).
PANEL_STEP_BETWEEN = 24
#: The most bytes a second that one panel's moving pictures may take: the aim never
#: asks for more pictures a second than fit this at their size. A moving picture of a
#: real page is about 200 kB at 1434 px across: 30 a second of those fit, and 24 of a
#: picture of 250 kB (D136, M117).
PANEL_BYTES_PER_S = 6_000_000
#: A step down this soon (in ticks) after a climb is a climb past what the link or the
#: XR page carries.
PANEL_CLIMB_TICKS = 10
#: After such a climb the aim stays at or under what that second showed the link to
#: carry for this many ticks, then twice as long each time the next climb fails too.
PANEL_HOLD_TICKS = 30
PANEL_HOLD_TICKS_MAX = 240
#: A climb on a tick waits until the aim has not changed for this many ticks: with the
#: step down that a failed climb brings, the aim then changes twice in that time at most.
#: A climb in a stream's first seconds waits for nothing (a page's load is not its
#: link), and a hand's (``wake``) never does.
PANEL_QUIET_TICKS = 20
PANEL_START_TICKS = 10
#: Ticks after a failed climb whose pictures are still the climb's own: a step down
#: that they ask for is not taken, and one picture goes at a time
#: (:meth:`PanelController.flight_cap`).
PANEL_SETTLE_TICKS = 1
#: A step counts as carried when its bytes a second are within this of what the
#: overloaded second showed: a link that carries a step exactly reads a hair under it.
PANEL_CARRIED_SLACK = 1.02


def in_flight(nbytes: int) -> int:
    """How many pictures of *nbytes* may be on their way at once: as many as fit the socket's bytes, at most four."""
    return max(1, min(PANEL_IN_FLIGHT, PANEL_FLIGHT_BYTES // max(1, nbytes)))


def link_time(nbytes: int, delivery_s: float, busy_s: float, floor_s: float) -> float:
    """The time a picture tells the ladder that the link took for it: what the ladder's link reads.

    The card's rule reads the link as bytes over this time and climbs while that fits a
    faster step with :data:`~.screencast.UP_HEADROOM` to spare. Handed the busy time, it
    reads the round's delay and not the link: at a round of 94 ms one picture at a time
    gives at most 10 a second, whatever the link carries. So two things are taken apart:

    - What the link carried. The round with no wait in it (*floor_s*) is taken out of
      the busy time: what is left is the time it stood in line behind the picture
      before. On a link with room that is nothing; on a full one it is the picture's
      own time on the wire, so its bytes over it are what the link carries.
    - What the pictures on their way can give at this round: one picture's whole
      delivery shared by those in flight, less the headroom the rule wants above any
      step (so a step that pictures in flight do give is reached). A picture of more
      than half :data:`PANEL_FLIGHT_BYTES` goes alone (``PanelStream._released``).
      Other panels' bytes on the same socket are not counted: with them a page gets
      fewer than its aim, as it does today.

    The link time is the longer of the two: the link is read as the lower.
    """
    queued = max(0.0, min(busy_s, delivery_s - floor_s))
    return max(queued, delivery_s / (in_flight(nbytes) * UP_HEADROOM))


class PanelController(AdaptiveController):
    """The ladder of a panel: late past the panel's floor, everything else as for a card.

    A panel's picture is recorded at its own decoded ack (:meth:`record`, from
    :class:`Flight`), not when the stream asks the browser for the next one: with
    several pictures in flight that is an older one's ack. The stream's own call
    (:meth:`observe`) therefore records nothing.

    The card's ladder climbs whenever a calm second fits a faster step. A link, or an
    XR page's decode, that carries a rate between two steps then makes the aim go up
    and down for ever: at the lower step nothing waits, so the faster one looks free.
    A panel's aim remembers a climb that was taken back (:meth:`tick`): it goes to the
    fastest step that the overloaded second showed to be carried, and stays at or
    under it for :data:`PANEL_HOLD_TICKS`. And it never asks for more bytes a second
    than :data:`PANEL_BYTES_PER_S`.
    """

    def __init__(self, *, fps_floor: int = 0, picture_floor: int = 0) -> None:
        super().__init__(fps_floor=fps_floor, picture_floor=picture_floor)
        #: Recent pictures as (bytes, whole delivery).
        self._recent: deque[tuple[int, float]] = deque(maxlen=PANEL_FLOOR_PICTURES)
        #: Ticks so far: the controller's only clock.
        self._ticks = 0
        #: The step the last climb started from and the tick it was made at, until it has stood.
        self._climbed: tuple[int, int] | None = None
        #: The fastest step allowed while a failed climb is remembered, and the tick that ends it.
        self._ceiling = 0
        self._held_until = 0
        self._hold = PANEL_HOLD_TICKS
        #: The last tick whose pictures are still those of the failed climb.
        self._settles_at = 0
        #: The step a failed climb showed to be carried, kept after its hold: the next climb tries one step past it.
        self._known: int | None = None
        #: The panel stands on the ladder's 30 and paces itself at :data:`PANEL_STEP_BETWEEN`.
        self._between = False
        #: The tick of the aim's last change.
        self._changed_at = 0
        #: Seconds in a row in which the link gave the ladder's 30, and the tick before which 24 is not left for it.
        self._gave_top = 0
        self._between_until = 0

    def aim(self) -> int:
        """Pictures a second the panel aims at now: the ladder's step, or the panel's own step between 20 and 30."""
        return PANEL_STEP_BETWEEN if self._between else FPS_STEPS[self.fps_step]

    def period_floor(self) -> float:
        """The shortest time between two pictures that the aim allows past the ladder's own step: 0 when the step is the aim."""
        return 1.0 / PANEL_STEP_BETWEEN if self._between else 0.0

    def floor(self, nbytes: int) -> float:
        """The round trip of a picture of *nbytes*: the lowest recent delivery of one at least half its size.

        With none (the first big picture after a load's small ones), the cap.
        """
        near = [d for b, d in self._recent if b >= nbytes * PANEL_FLOOR_SIZE_SHARE]
        return min(min(near, default=PANEL_FLOOR_CAP_S), PANEL_FLOOR_CAP_S)

    def observe(self, nbytes: int, delivery_s: float) -> None:
        """The stream's release of a picture: not a delivery for a panel (:meth:`record` is)."""

    def record(self, nbytes: int, delivery_s: float, busy_s: float | None = None) -> None:
        """Record one picture: late by its whole delivery past its floor; its link time for the rest.

        *busy_s* is the time the link spent on it (:meth:`Flight.acked`); ``None`` (a
        picture that no ack came for): the whole delivery, and nothing is taken out.
        """
        floor = self.floor(nbytes)
        late = delivery_s - floor > max(PANEL_LATE_S, LATE_PERIODS * LADDER[self.rung].period)
        self._recent.append((nbytes, delivery_s))
        w = self.window
        counted = w.late
        super().observe(
            nbytes, delivery_s if busy_s is None else link_time(nbytes, delivery_s, busy_s, floor)
        )
        # The card's own late test read what it was handed: the panel's reads the whole delivery past the floor.
        w.late = counted + int(late)

    def tick(self) -> TickStats:
        """The card's tick, then the panel's two bounds: a failed climb is remembered, and the bytes a second."""
        before, was_between, aim_before = self.fps_step, self._between, self.aim()
        rates = sorted(self.window.rates)
        stats = super().tick()
        self._ticks += 1
        if (
            was_between
            and self.fps_step > before
            and self._last is not None
            and stats.late_share <= LATE_SHARE_DOWN
            and rates
            and self._last[0] * PANEL_STEP_BETWEEN <= rates[len(rates) // 4] * DOWN_UTILISATION
        ):
            # The ladder judged its own 30, which the panel stands on. The panel paces
            # itself at 24, and the link gives that: nothing was late, and 24 a second
            # fit what even the slower pictures of this second showed (the lower
            # quarter). So the step down is not taken.
            self.fps_step = before
        if self._held_until and self._ticks >= self._held_until:
            self._held_until = 0
        climbed = self._climbed
        if (
            self.fps_step > before
            and climbed is not None
            and self._ticks - climbed[1] <= PANEL_CLIMB_TICKS
        ):
            # The climb was past what is carried. What is carried is what the slower
            # pictures of this second showed (the lower quarter: the ones that stood
            # in line), never faster than one step under the climb, never slower than
            # the step the climb started from, which stood.
            per_frame = self._last[0] if self._last is not None else 0.0
            carried = (rates[len(rates) // 4] if rates else 0.0) * PANEL_CARRIED_SLACK
            bottom = len(FPS_STEPS) - 1
            step = before + 1
            while step < bottom and per_frame * FPS_STEPS[step] > carried:
                step += 1
            self._ceiling = min(max(step, before + 1), max(climbed[0], before + 1))
            self._held_until = self._ticks + self._hold
            self._settles_at = self._ticks + PANEL_SETTLE_TICKS
            self._hold = min(self._hold * 2, PANEL_HOLD_TICKS_MAX)
            self._climbed = None
            self.fps_step = self._ceiling
            self._known = self._ceiling
        elif self._held_until and self.fps_step > self._ceiling:
            if self._ticks <= self._settles_at:
                # The pictures of the failed climb are still on their way: this second
                # read them, not the step the aim went back to.
                self.fps_step = self._ceiling
            else:
                # The step it went back to is not carried either: the aim does not
                # climb back to it while the climb is remembered.
                self._ceiling = self._known = self.fps_step
        self._bound()
        self._step_between(calm=self.calm >= CALM_WINDOWS_UP, ticked=True)
        if (
            self.aim() > aim_before
            and self._ticks > PANEL_START_TICKS
            and self._ticks - self._changed_at < PANEL_QUIET_TICKS
        ):
            # A climb, and the aim changed not long ago: it waits. A climb may fail and
            # be taken back, so two changes are the most that it brings in that time.
            self.fps_step, self._between = before, was_between
        self._note_change(aim_before)
        self._note_climb(before)
        stats.rung = self.rung
        return stats

    def flight_cap(self) -> int:
        """The most pictures on their way now: four, two on the lowest step.

        One right after a failed climb, so that its line of pictures empties.

        A link that carries a step exactly never empties a line by itself: every
        picture then stays late by the line's length, and the step that is carried
        would be given up too.
        """
        if self._ticks < self._settles_at:
            return 1
        # On the lowest step the link may carry less than the aim, and the aim cannot
        # go lower: each picture more on its way is then one more that a press waits
        # behind, and no more pictures arrive for it.
        return PANEL_IN_FLIGHT_BOTTOM if self.fps_step == len(FPS_STEPS) - 1 else PANEL_IN_FLIGHT

    def wake(self) -> int:
        before, aim_before = self.fps_step, self.aim()
        super().wake()
        self._bound()
        self._step_between(calm=True, ticked=False)
        self._note_change(aim_before)
        self._note_climb(before)
        return self.rung

    def _bound(self) -> None:
        """Keep the aim at or under a remembered ceiling, and under the bytes a second a panel may take."""
        if self._held_until:
            self.fps_step = max(self.fps_step, self._ceiling)
        elif self._known is not None:
            # After a remembered climb the next one tries one step, not the top.
            self.fps_step = max(self.fps_step, self._known - 1)
        if self._last is not None:
            bottom = len(FPS_STEPS) - 1
            while self.fps_step < bottom and self._last[0] * self.aim() > PANEL_BYTES_PER_S:
                self.fps_step += 1
                self._between = False

    def _step_between(self, *, calm: bool, ticked: bool) -> None:
        """Stand on the panel's own step when the link gives it and not the ladder's 30.

        From 20 it is a climb like any other (after calm seconds, never past a
        remembered ceiling, never past the bytes a second). On 30 with a link that
        gives 24 and not 30 the aim is named and paced as 24: the pictures in flight
        would hold it there anyway. From 24 to 30 it goes as steadily as any climb:
        only once the link gave 30 for three seconds in a row,
        and not within :data:`PANEL_HOLD_TICKS` of having come down from it.
        """
        top, under = FPS_STEPS.index(30), FPS_STEPS.index(20)
        if self._last is None or self.fps_floor > top or self.fps_step not in (top, under):
            self._between = False
            self._gave_top = 0
            return
        per_frame, link = self._last

        def fits(fps: int) -> bool:
            return per_frame * fps * UP_HEADROOM <= link and per_frame * fps <= PANEL_BYTES_PER_S

        if ticked:
            self._gave_top = self._gave_top + 1 if fits(FPS_STEPS[top]) else 0
        if self.fps_step == top:
            if not self._between:
                if not fits(FPS_STEPS[top]) and fits(PANEL_STEP_BETWEEN):
                    self._between = True
                    self._between_until = self._ticks + PANEL_HOLD_TICKS
            elif self._gave_top > CALM_WINDOWS_UP and self._ticks >= self._between_until:
                self._between = False
            return
        self._between = False
        held = bool(self._held_until) and self._ceiling > top
        if calm and not held and fits(PANEL_STEP_BETWEEN):
            self.fps_step, self._between = top, True
            self._between_until = 0

    def _note_change(self, aim_before: int) -> None:
        if self.aim() != aim_before:
            self._changed_at = self._ticks

    def _note_climb(self, before: int) -> None:
        if self.fps_step < before:
            if self._climbed is None:
                self._climbed = (before, self._ticks)
        elif self._climbed is not None and self._ticks - self._climbed[1] > PANEL_CLIMB_TICKS:
            # The climb stood: the next failed one is held for the first time again,
            # and the next climb may try one step more.
            self._climbed = None
            self._hold = PANEL_HOLD_TICKS
            if self._known is not None:
                self._known = self.fps_step if self.fps_step > self.fps_floor else None


@dataclass
class _Picture:
    sent_at: float
    nbytes: int
    #: A moving picture: its delivery is evidence for the pace. A lossless still is not.
    measured: bool


@dataclass
class Flight:
    """The pictures of one panel that the XR page has not said it decoded. No I/O, no clock."""

    pictures: dict[int, _Picture] = field(default_factory=dict)
    #: When the last decoded ack came: a picture's busy time starts no sooner.
    acked_at: float = float("-inf")

    def sent(self, seq: int, sent_at: float, nbytes: int, *, measured: bool) -> None:
        self.pictures[seq] = _Picture(sent_at, nbytes, measured)

    def bytes(self) -> int:
        return sum(p.nbytes for p in self.pictures.values())

    def older_than(self, seq: int) -> int:
        """How many pictures sent before ``seq`` are still on their way."""
        return sum(1 for s in self.pictures if s < seq)

    def acked(self, seq: int, now: float) -> tuple[bool, tuple[int, float, float] | None]:
        """The decoded ack of ``seq``: it releases every picture up to it.

        Returns whether any was released, and ``(bytes, delivery, busy)`` of ``seq``
        itself when it was a moving picture on its way: *busy* runs from the later of
        its send and the decoded ack before it. A picture released by a newer one's
        ack has no delivery of its own: it was replaced before its upload.
        """
        done = [s for s in self.pictures if s <= seq]
        own = self.pictures.get(seq)
        for s in done:
            del self.pictures[s]
        since, self.acked_at = self.acked_at, now if done else self.acked_at
        if own is None or not own.measured:
            return bool(done), None
        return True, (own.nbytes, now - own.sent_at, now - max(own.sent_at, since))

    def given_up(self, seq: int, now: float) -> list[tuple[int, float]]:
        """Drop every picture up to ``seq`` that no ack came for: each moving one is a late delivery."""
        return self._drop([s for s in self.pictures if s <= seq], now)

    def expired(self, now: float, age: float) -> list[tuple[int, float]]:
        """Drop every picture sent *age* or more ago that no ack came for: each moving one is a late delivery."""
        return self._drop([s for s, p in self.pictures.items() if now - p.sent_at >= age], now)

    def _drop(self, seqs: list[int], now: float) -> list[tuple[int, float]]:
        lost = [self.pictures.pop(s) for s in seqs]
        return [(p.nbytes, now - p.sent_at) for p in lost if p.measured]

    def clear(self) -> None:
        self.pictures.clear()


__all__ = [
    "PANEL_BYTES_PER_S",
    "PANEL_CLIMB_TICKS",
    "PANEL_FLIGHT_BYTES",
    "PANEL_FLOOR_CAP_S",
    "PANEL_FLOOR_PICTURES",
    "PANEL_FLOOR_SIZE_SHARE",
    "PANEL_HOLD_TICKS",
    "PANEL_HOLD_TICKS_MAX",
    "PANEL_IN_FLIGHT",
    "PANEL_LATE_S",
    "PANEL_QUIET_TICKS",
    "PANEL_SOCKET_BYTES",
    "PANEL_STEP_BETWEEN",
    "Flight",
    "PanelController",
    "in_flight",
    "link_time",
]
