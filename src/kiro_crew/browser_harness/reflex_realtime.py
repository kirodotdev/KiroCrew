"""The ``reflex`` op's two timed modes: ``realtime`` (pipelined) and ``virtual_time``.

``realtime`` follows the decision loop of hosseintoussi/jev-flappy-bird (MIT,
Copyright (c) 2026 Hossein Toussi; see NOTICE), with no game in it: a question is
asked every ``tick_ms`` without waiting for earlier answers, up to ``max_in_flight`` at once (never more than the provider
declares it takes). An answer is used only while its question still describes the
page: once an action has run after it was asked -- or, with ``supersede:
"change"``, once the page's mutation counter moved -- it is discarded and counted
as superseded. With ``lead`` the state carries ``lead_ms`` (the measured median
round trip) and an answer that comes back sooner waits until that moment; a late
one is used when it lands. Code never projects the page forward: the model may.
A WAIT changes nothing and supersedes nothing; after an applied action the next
question is asked at once. Superseded answers are counted, never put on the card.

``virtual_time`` holds the page's timers and animation frames while it reads the
page and decides, then lets the page run for the input and ``step_ms`` after it, so a slow or one-at-a-time provider still drives a moving
page. The hold is a break on the page's next timer or animation-frame callback
(``page_clock``), not CDP virtual time, which Chromium cannot hand back to a page.
What it does not hold: CSS animations and transitions, network callbacks and the
page's wall clock (``performance.now``, ``Date`` and frame timestamps keep
running), so a page that integrates unclamped elapsed time sees each hold as one
long frame; most animation loops clamp their frame step. A tab whose browser
refuses the hold runs the call in ``step`` mode and says so.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import time
from dataclasses import dataclass
from typing import Any

from kiro_crew.browser_harness import ops as _ops
from kiro_crew.browser_harness import reflex as _reflex
from kiro_crew.browser_harness.reflex_space import Move, Space

logger = logging.getLogger(__name__)

#: Failed decisions in a row that end a timed run; one that never had an answer
#: ends after this many, as ``not_available``.
FAILURES_IN_A_ROW = 10
FIRST_FAILURES = 3
#: After a failed decision, no new question for this long (an outage is probed,
#: not hammered).
FAILURE_PAUSE_S = 0.5


@dataclass
class Ask:
    seq: int
    asked: float
    epoch: int
    mut: int
    space: Space
    risk: list[Move]
    lead_s: float
    space_rows: list[dict[str, Any]] | None


async def run(loop: _reflex.Loop) -> dict[str, Any]:
    """Run *loop* in its plan's timed mode; open the provider's connection first."""
    try:
        await asyncio.wait_for(_reflex._decide.warm(loop.session_key), timeout=2.0)
    except asyncio.TimeoutError:
        pass
    if loop.plan.mode == "virtual_time":
        return await _virtual_time(loop)
    return await _Realtime(loop).run()


class _Realtime:
    def __init__(self, loop: _reflex.Loop) -> None:
        self.loop = loop
        cap = int(getattr(loop.caps, "max_in_flight", 1) or 1)
        self.width = max(1, min(loop.plan.max_in_flight, cap))
        self.epoch = 0
        self.mut = 0
        self.superseded = 0
        self.stale = 0
        self.unjudged = 0
        self.waits = 0
        self.applied = 0
        self.held_ms: list[float] = []
        self.peak = 0
        self.in_a_row = 0
        self.pending: dict[asyncio.Task[Any], Ask] = {}
        self.due: list[tuple[float, int, Ask, dict[str, Any]]] = []
        self.seq = itertools.count(1)
        self.state: dict[str, Any] | None = None
        self.next_ask = time.monotonic()
        self.hold_until = 0.0

    def counters(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "waits": self.waits,
            "superseded": self.superseded,
            "stale": self.stale,
            "unjudged": self.unjudged,
            "in_flight_max": self.peak,
            "in_flight_cap": self.width,
            "rtt_p50_ms": self.loop.rtt_ms(0.5) if self.loop.rtts else None,
            "lead_ms": self.loop.lead_ms() if self.loop.plan.lead else None,
            "held_ms_avg": round(sum(self.held_ms) / len(self.held_ms)) if self.held_ms else 0,
        }

    async def finish(self, stop: str, **more: Any) -> dict[str, Any]:
        self.loop.extra.update(self.counters())
        return await self.loop.finish(stop, self.state, **more)

    async def run(self) -> dict[str, Any]:
        try:
            return await self._run()
        finally:
            for task in self.pending:
                task.cancel()

    async def _run(self) -> dict[str, Any]:
        loop = self.loop
        while True:
            if await _ops._in_control(loop.ctx):
                return await self.finish(_reflex.STOP_USER)
            now = time.monotonic()
            if loop.out_of_time_or_steps():
                return await self.finish(_reflex.STOP_BUDGET)
            # At the decision limit no new question is asked, but the answers to
            # those already asked are still used, within the time and step limits.
            asking = loop.decisions < loop.plan.max_decisions
            if not asking and not self.pending and not self.due:
                return await self.finish(_reflex.STOP_BUDGET)
            if (
                asking
                and now >= self.next_ask
                and now >= self.hold_until
                and len(self.pending) < self.width
            ):
                stop = await self.ask()
                if stop is not None:
                    return stop
            stop = await self.collect(asking)
            if stop is not None:
                return stop
            while self.due and self.due[0][0] <= time.monotonic():
                _due, _n, ask, verdict = heapq.heappop(self.due)
                stop = await self.apply(ask, verdict)
                if stop is not None:
                    return stop

    async def ask(self) -> dict[str, Any] | None:
        loop = self.loop
        # The URL gate runs before the page is read or sent anywhere (as in step mode).
        refused = await loop.landed_refusal()
        if refused is not None:
            self.state = None
            return await self.finish(_reflex.STOP_REFUSED, error=refused["error"])
        seen = await loop.observe()
        if seen is None:
            if loop.answered == 0 and loop.decisions == 0:
                return _reflex.not_available("the page state could not be read")
            # Mid-navigation or a busy page: try again on the next tick.
            self.next_ask = time.monotonic() + loop.plan.tick_s
            return None
        self.state, self.mut = seen.state, seen.mut
        if seen.until_hit:
            return await self.finish(_reflex.STOP_UNTIL)
        space = loop.space(seen, wait=loop.plan.wait)
        risk = loop.risk_batch(space)
        lead = loop.lead_ms() if loop.plan.lead else None
        ask = Ask(
            next(self.seq),
            time.monotonic(),
            self.epoch,
            seen.mut,
            space,
            risk,
            (lead or 0) / 1000.0,
            seen.rows,
        )
        task = asyncio.ensure_future(loop.request(seen, space, risk, lead_ms=lead))
        self.pending[task] = ask
        self.peak = max(self.peak, len(self.pending))
        self.next_ask = max(self.next_ask + loop.plan.tick_s, ask.asked)
        return None

    async def collect(self, asking: bool = True) -> dict[str, Any] | None:
        """Wait for the next tick, a held answer's moment, or an answer; file answers."""
        now = time.monotonic()
        # A full pipeline, or one that asks no more, has no tick to wake for: only an
        # answer, a held moment or the deadline.
        full = len(self.pending) >= self.width or not asking
        wake = float("inf") if full else max(self.next_ask, self.hold_until)
        if self.due:
            wake = min(wake, self.due[0][0])
        wake = min(wake, self.loop.started + self.loop.plan.max_seconds)
        timeout = max(0.0, wake - now)
        if not self.pending:
            await asyncio.sleep(timeout)
            return None
        done, _ = await asyncio.wait(
            list(self.pending), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            ask = self.pending.pop(task)
            verdict = task.result()
            if verdict is None:
                self.in_a_row += 1
                self.hold_until = time.monotonic() + FAILURE_PAUSE_S
                if self.loop.answered == 0 and self.loop.failed >= FIRST_FAILURES:
                    return _reflex.not_available("no decision model answered")
                if self.in_a_row >= FAILURES_IN_A_ROW:
                    return await self.finish(_reflex.STOP_DECISION)
                continue
            self.in_a_row = 0
            heapq.heappush(self.due, (ask.asked + ask.lead_s, ask.seq, ask, verdict))
        return None

    async def apply(self, ask: Ask, verdict: dict[str, Any]) -> dict[str, Any] | None:
        loop = self.loop
        changed = False
        if loop.plan.supersede == "change" and ask.epoch == self.epoch:
            # Re-read the counter now: with the pipeline full no question was asked
            # since, so the last observed value may predate a change. Unreadable
            # counts as changed.
            ticks = await _ops._page(loop.ctx, "ticks")
            self.mut = ticks.get("m", -1) if isinstance(ticks, dict) else -1
            changed = self.mut != ask.mut
        if ask.epoch != self.epoch or changed:
            self.superseded += 1
            return None
        self.held_ms.append(max(0.0, (time.monotonic() - ask.asked) * 1000))
        stop = loop.stop_for(verdict)
        if stop is not None:
            return await self.finish(stop)
        move = ask.space.resolve(verdict["operation"], verdict["picks"])
        if move is None:
            self.stale += 1
            return None
        if move.kind == "wait":
            self.waits += 1
            return None
        if move.risky is None:
            self.unjudged += 1
            if move not in loop.pending_risk:
                loop.pending_risk.append(move)
            return None
        if move.risky >= _reflex.RISK_STOP:
            return await self.finish(_reflex.STOP_RISK, handoff=_reflex._handoff(move.label))
        if loop.out_of_time_or_steps():
            # An answer that lands past the deadline is not acted on.
            return await self.finish(_reflex.STOP_BUDGET)
        if await _ops._in_control(loop.ctx):
            return await self.finish(_reflex.STOP_USER)
        result = await loop.act(move, verdict["value"], ask.space.guards.get(move.key))
        if not result.get("ok"):
            if result.get("code") == "password_field":
                return await self.finish(_reflex.STOP_RISK, handoff=result["error"])
            self.stale += 1
            return None
        self.applied += 1
        self.epoch += 1
        # The next question must see the page after the input, not before the page
        # handled it: the same short settle as step mode (two frames, at most 50 ms).
        await loop.settle(move, ask.space_rows)
        # The action changed the page's path: ask about the new one now.
        self.next_ask = time.monotonic()
        refused = await loop.landed_refusal()
        if refused is not None:
            self.state = None
            return await self.finish(_reflex.STOP_REFUSED, error=refused["error"])
        return None


class _Held:
    """The page's hold: timers and animation frames stop at their next callback."""

    def __init__(self, loop: _reflex.Loop) -> None:
        self.loop = loop
        self.held = False

    async def clock(self, state: str) -> bool:
        try:
            await self.loop.ctx.helper("page_clock", state=state)
            return True
        except Exception:  # noqa: BLE001 - a browser that refuses the hold: the caller falls back
            logger.debug("reflex: page_clock %s failed", state, exc_info=True)
            return False

    async def hold(self) -> bool:
        self.held = await self.clock("hold")
        return self.held

    async def run(self) -> None:
        if self.held:
            await self.clock("run")
            self.held = False


async def _virtual_time(loop: _reflex.Loop) -> dict[str, Any]:
    """Hold the page, read it, decide, let it run, act, run ``step_ms``; repeat.

    The read and the decision happen while the page is held, so the model judges
    the page as it is when it is let go. A held page drops input, so it runs again
    before the input, and the executor's target check (same element, not covered)
    happens after that, against the page as the input meets it.
    """
    page = _Held(loop)
    state: dict[str, Any] | None = None
    try:
        while True:
            if await _ops._in_control(loop.ctx):
                return await loop.finish(_reflex.STOP_USER, state)
            # The URL gate runs before the page is read or sent anywhere (as in step mode).
            refused = await loop.landed_refusal()
            if refused is not None:
                return await loop.finish(_reflex.STOP_REFUSED, None, error=refused["error"])
            # A frame cannot be taken while the page is held: take it just before.
            frame = await _reflex._decide._capture(loop.ctx) if loop.image else None
            if not await page.hold():
                if loop.decisions == 0:
                    loop.notes.append("the browser refused to hold the page; ran in step mode")
                    loop.plan.mode = "step"
                    return await loop.run_step()
                note = "the browser stopped holding the page; later decisions saw it moving"
                if note not in loop.notes:
                    loop.notes.append(note)
            seen = await loop.observe(frames=False)
            if seen is None:
                if loop.decisions == 0:
                    return _reflex.not_available("the page state could not be read")
                return await loop.finish(_reflex.STOP_DECISION, state, why="page state unreadable")
            seen.frame = frame
            state = seen.state
            if seen.until_hit:
                return await loop.finish(_reflex.STOP_UNTIL, state)
            if loop.over_budget():
                return await loop.finish(_reflex.STOP_BUDGET, state)
            space = loop.space(seen, wait=loop.plan.wait)
            verdict = await loop.request(seen, space, loop.risk_batch(space))
            await page.run()
            if verdict is None:
                if loop.answered == 0:
                    return _reflex.not_available("no decision model answered")
                return await loop.finish(_reflex.STOP_DECISION, state)
            stop = loop.stop_for(verdict)
            if stop is not None:
                return await loop.finish(stop, state)
            move = space.resolve(verdict["operation"], verdict["picks"])
            if move is None:
                return await loop.finish(_reflex.STOP_DECISION, state, why="no action picked")
            if move.kind != "wait" and move.risky is None:
                loop.pending_risk.append(move)
                continue
            if move.kind != "wait":
                if move.risky is not None and move.risky >= _reflex.RISK_STOP:
                    return await loop.finish(
                        _reflex.STOP_RISK, state, handoff=_reflex._handoff(move.label)
                    )
                if loop.out_of_time_or_steps():
                    return await loop.finish(_reflex.STOP_BUDGET, state)
                if await _ops._in_control(loop.ctx):
                    return await loop.finish(_reflex.STOP_USER, state)
                result = await loop.act(move, verdict["value"], space.guards.get(move.key))
                if result.get("code") == "password_field":
                    return await loop.finish(_reflex.STOP_RISK, state, handoff=result["error"])
                if result.get("ok"):
                    refused = await loop.landed_refusal()
                    if refused is not None:
                        return await loop.finish(_reflex.STOP_REFUSED, None, error=refused["error"])
            await loop.ctx.sleep(loop.plan.step_s)
    finally:
        # Always let the page go: a Take over cancels this task mid-decision.
        page.held = True
        await page.clock("release")
