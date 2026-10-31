"""The ``reflex`` op: a fast observe -> judge -> act loop inside ONE browser op.

The agent names a goal and a CLOSED set of moves: keys to press, drags, scrolls,
bound text to type, and the operations whose target the decision model picks from
the page's own element table (``targets``: click, hover, type, select). The gateway
then loops with no LLM turn in between. Each decision is ONE request through the
decision seam (``browser_harness.decide``): an operation head, a speculative target
head per targeted operation, and the done / stuck / off-path / risk Nouls. The
model only judges; the stop rules, the risk threshold, the no-op withdrawal, the
budgets, the terminal checks and every safety gate are code here.

Three modes, one op (``mode``):

* ``step`` -- observe, decide, act, settle, repeat. For forms, menus, grids.
* ``realtime`` -- a decision is asked every ``tick_ms`` without waiting for the
  last one, up to ``max_in_flight`` at once (``reflex_realtime``). An answer whose
  question was asked before an action ran (or, with ``supersede: "change"``, before
  the page changed) is discarded and counted; an early answer can be held until
  the moment it was asked about (``lead``). For pages that move on their own.
* ``virtual_time`` -- the page's script is held while each decision is pending and
  runs ``step_ms`` between decisions (``reflex_realtime``). For a moving page with a
  provider too slow, or too narrow, for ``realtime``.

The loop reads what the configured provider declares (concurrency, latency, and
through the gate's own check whether it takes images) and never its name: no image
support means structured state only, a provider that takes one request at a time
turns ``realtime`` into ``virtual_time``. Both are said in the result, never an
error.

Off is free: without an armed ``browser.step`` point (no provider, no consent,
governance denied), or when the first decision fails, the op answers
``not_available`` before it has touched the page. No other op's path changes.

Fences are the ones every op has: the route's gateway lock, the per-session tab and
the claim fence (a Take over cancels the op task; a held page is always let go).
The loop also checks ``in_control`` before each action and after each decision. The
landed URL after every action goes through the navigate gate. Text typed must
appear verbatim in the goal, a fact or the page, so the model never writes text;
a target is re-checked (same element, still enabled, visible, not covered) just
before input; a password field is never typed into and never photographed. An
action judged hard to undo stops the loop and hands it to the person. Every
APPLIED action is one step label on the live card; discarded answers are counted.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from kiro_crew.browser_harness import decide as _decide
from kiro_crew.browser_harness import ops as _ops
from kiro_crew.browser_harness import reflex_space as _space
from kiro_crew.browser_harness.reflex_space import Move, Space

logger = logging.getLogger(__name__)

MODES = ("step", "realtime", "virtual_time")

#: Caps on what one call may ask for. ``max_seconds`` stays inside the route's slow
#: op budget, so a loop always ends with its own summary, never a route timeout.
MAX_ACTIONS = 40
MAX_STEPS = 300
DEFAULT_STEPS = 60
MAX_SECONDS = 45.0
DEFAULT_SECONDS = 20.0
MAX_SETTLE_MS = 1000
#: Decision requests per call, whatever the mode: the hard bound on spend.
DEFAULT_DECISIONS = 600
MAX_DECISIONS = 2000
#: ``realtime``: the ask clock and the requests in flight.
MIN_TICK_MS = 50
DEFAULT_TICK_MS = 50
DEFAULT_IN_FLIGHT = 6
MAX_IN_FLIGHT = 16
#: ``virtual_time``: how long the page runs between two decisions.
DEFAULT_STEP_MS = 50
MIN_STEP_MS = 16
MAX_STEP_MS = 500
#: After an action, the next read waits for two animation frames (at most 50 ms),
#: or after typing into a combobox for its suggestions (at most 200 ms).
SETTLE_FRAMES = 2
SETTLE_S = 0.05
SETTLE_COMBOBOX_S = 0.2
SETTLE_POLL_S = 0.01
#: ``lead``: the round-trip quantile a question looks ahead by, over this many.
LEAD_QUANTILE = 0.9
LEAD_WINDOW = 20
#: An explicit WAIT in ``step`` mode.
WAIT_S = 0.1
#: Risk questions asked per request; the rest are asked on later decisions.
RISK_PER_REQUEST = 8
#: A risk Noul at or above this stops the loop before the action runs.
RISK_STOP = 0.5
#: The grid sent as state is cut to this size.
GRID_CELL_CAP = 64
#: Moves kept in the result, and declarative reads per call.
RECENT_MOVES = 20
MAX_READS = 16
_FACT_CAP = 500
_SELECTOR_CAP = 200
_VALUE_CAP = 200

STOP_DONE = "done"
STOP_STUCK = "stuck"
STOP_OFF_PATH = "off_path"
STOP_RISK = "risk"
STOP_UNTIL = "until"
STOP_BUDGET = "budget"
STOP_USER = "user_in_control"
STOP_NO_ACTIONS = "no_actions_left"
STOP_DECISION = "decision_unavailable"
STOP_REFUSED = "redirect_refused"

#: Keys that only move focus, scroll or steer and never submit anything: judged
#: safe by code, so no risk question is spent on them (Enter, Space and
#: characters are still asked about, since they can press or submit).
SAFE_KEYS = frozenset(
    {
        "ArrowLeft",
        "ArrowUp",
        "ArrowRight",
        "ArrowDown",
        "Tab",
        "Escape",
        "Home",
        "End",
        "PageUp",
        "PageDown",
    }
)
_TARGET_OPS = ("click", "hover", "type", "select")
_READ_PROP = re.compile(r"\A(text|value|checked|box|count|attr:[A-Za-z_:][-A-Za-z0-9_:.]{0,63})\Z")
#: Element states that mean a refused input target is stale, not broken.
_STALE_CODES = frozenset({"gone", "stale", "covered", "offscreen", "disabled"})


def not_available(why: str) -> dict[str, Any]:
    return _ops.refusal(
        "not_available",
        f"reflex is not available ({why}); drive the page with single ops instead",
    )


@dataclass
class Plan:
    goal: str
    facts: list[str]
    mode: str
    moves: list[Move]
    targets: tuple[str, ...]
    allowed_refs: frozenset[str] | None
    values: list[str]
    region: str | None
    read: list[dict[str, str]]
    text: bool
    grid: str | None
    grid_items: str | None
    see: bool
    until_text: str | None
    until_url: str | None
    until_noul: str | None
    max_steps: int
    max_seconds: float
    max_decisions: int
    settle_s: float | None
    tick_s: float
    max_in_flight: int
    lead: bool
    supersede: str
    step_s: float
    wait: bool


@dataclass
class Seen:
    """One observation: what the model reads, and what the loop compares."""

    state: dict[str, Any]
    fp: str
    frame: bytes | None
    rows: list[dict[str, Any]] | None
    mut: int
    url: str
    until_hit: bool


def _text(value: Any, cap: int) -> str | None:
    return value.strip()[:cap] if isinstance(value, str) and value.strip() else None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _int(value: Any, default: int, low: int, high: int) -> int:
    number = _ops._number(value)
    return default if number is None else max(low, min(high, int(number)))


def _point(raw: str) -> dict[str, Any] | None:
    """A drag end as a ref (``eN``) or a pixel pair (``x,y``), as the drag op takes it."""
    raw = raw.strip()
    if _ops._is_ref(raw):
        return {"ref": raw}
    x, sep, y = raw.partition(",")
    nx, ny = _number_text(x), _number_text(y)
    if sep and nx is not None and ny is not None and nx >= 0 and ny >= 0:
        return {"x": nx, "y": ny}
    return None


def _number_text(raw: str) -> float | None:
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    return value if value == value and abs(value) != float("inf") else None


def parse(args: dict[str, Any]) -> Plan | dict[str, Any]:
    """The call's plan, or a refusal. Checks shapes only; values are checked later."""
    goal = _text(args.get("goal"), 500)
    if goal is None:
        return _ops.refusal("bad_goal", "reflex needs a `goal` string")
    mode = args.get("mode", "step")
    if mode not in MODES:
        return _ops.refusal("bad_mode", f"`mode` is one of {', '.join(MODES)}")
    moves: list[Move] = []
    for key in _strings(args.get("keys")):
        if key == "Space":
            key = " "
        if key not in _ops._NAMED_KEYS and len(key) != 1:
            return _ops.refusal("bad_key", f"unknown key in `keys`: {key!r}")
        name = "Space" if key == " " else key
        moves.append(
            Move(
                f"press {name}",
                "key",
                f"Press {name}",
                {"key": key},
                risky=0.0 if key in SAFE_KEYS else None,
            )
        )
    for n, pair in enumerate(_strings(args.get("drag")), 1):
        src, _, dst = pair.partition(">")
        start, end = _point(src), _point(dst)
        if start is None or end is None:
            return _ops.refusal("bad_ref", f"`drag` entries are 'eA>eB' or 'x,y>x,y', got {pair!r}")
        drag_args = {**start, **{f"to_{k}": v for k, v in end.items()}}
        moves.append(Move(f"drag {n}", "drag", f"Drag {src.strip()} to {dst.strip()}", drag_args))
    if args.get("scroll") is True:
        moves.append(Move("scroll up", "scroll", "Scroll up", {"dy": -1}, risky=0.0))
        moves.append(Move("scroll down", "scroll", "Scroll down", {"dy": 1}, risky=0.0))
    for entry in _strings(args.get("type")):
        ref, sep, value = entry.partition("=")
        ref = ref.strip()
        if not sep or not _ops._is_ref(ref) or not value:
            return _ops.refusal("bad_value", f"`type` entries are 'eN=value', got {entry[:60]!r}")
        moves.append(
            Move(f"type {ref}", "type", f"Type into {ref}", {"ref": ref, "text": value}, risky=0.0)
        )
    targets = tuple(t for t in _TARGET_OPS if t in _strings(args.get("targets")))
    unknown = set(_strings(args.get("targets"))) - set(_TARGET_OPS)
    if unknown:
        return _ops.refusal("bad_targets", f"`targets` takes {', '.join(_TARGET_OPS)}")
    refs = _strings(args.get("click"))
    for ref in refs:
        if not _ops._is_ref(ref):
            return _ops.refusal("bad_ref", f"not a valid ref in `click`: {ref!r}")
    if refs and "click" not in targets:
        targets = ("click", *targets)
    values = [v[:_VALUE_CAP] for v in _strings(args.get("values"))][:20]
    if "type" in targets and not values:
        return _ops.refusal("bad_value", "`targets` with type needs `values` to type")
    reads = _reads(args.get("read"))
    if isinstance(reads, dict):
        return reads
    if not moves and not targets and mode == "step":
        return _ops.refusal(
            "no_actions", "reflex needs at least one of: keys, click, drag, scroll, type, targets"
        )
    if len(moves) > MAX_ACTIONS:
        return _ops.refusal("too_many_actions", f"reflex takes at most {MAX_ACTIONS} actions")
    seconds = _ops._number(args.get("max_seconds"))
    grid = _text(args.get("grid"), _SELECTOR_CAP)
    settle = _ops._number(args.get("settle_ms"))
    supersede = args.get("supersede", "action")
    if supersede not in ("action", "change"):
        return _ops.refusal("bad_supersede", "`supersede` is 'action' or 'change'")
    text = args.get("text")
    return Plan(
        goal=goal,
        facts=[f[:_FACT_CAP] for f in _strings(args.get("facts"))][:20],
        mode=mode,
        moves=moves,
        targets=targets,
        allowed_refs=frozenset(refs) if refs else None,
        values=values,
        region=_text(args.get("region"), _SELECTOR_CAP),
        read=reads,
        text=text if isinstance(text, bool) else not (grid or reads),
        grid=grid,
        grid_items=_text(args.get("grid_items"), _SELECTOR_CAP),
        see=args.get("see") is True,
        until_text=_text(args.get("until_text"), 200),
        until_url=_text(args.get("until_url"), 500),
        until_noul=_text(args.get("until_noul"), 300),
        max_steps=_int(args.get("max_steps"), DEFAULT_STEPS, 1, MAX_STEPS),
        max_seconds=DEFAULT_SECONDS if seconds is None else max(1.0, min(MAX_SECONDS, seconds)),
        max_decisions=_int(args.get("max_decisions"), DEFAULT_DECISIONS, 1, MAX_DECISIONS),
        settle_s=None if settle is None else max(0.0, min(MAX_SETTLE_MS, settle)) / 1000.0,
        tick_s=_int(args.get("tick_ms"), DEFAULT_TICK_MS, MIN_TICK_MS, 5000) / 1000.0,
        max_in_flight=_int(args.get("max_in_flight"), DEFAULT_IN_FLIGHT, 1, MAX_IN_FLIGHT),
        lead=args.get("lead") is True,
        supersede=supersede,
        step_s=_int(args.get("step_ms"), DEFAULT_STEP_MS, MIN_STEP_MS, MAX_STEP_MS) / 1000.0,
        wait=args.get("wait") is True or mode != "step",
    )


_READ_NAME = re.compile(r"\A([A-Za-z_][A-Za-z0-9_]{0,39})=(.+)\Z", re.DOTALL)


def _read_entry(item: Any) -> dict[str, Any] | None:
    """``'name=css@prop'`` (the tool's flat form) or ``{name?, sel, prop?}``."""
    if isinstance(item, dict):
        return item
    if not isinstance(item, str):
        return None
    name, body = None, item.strip()
    named = _READ_NAME.match(body)
    if named:
        name, body = named.group(1), named.group(2)
    sel, at, prop = body.rpartition("@")
    if not at:
        sel, prop = body, "text"
    return {"name": name, "sel": sel, "prop": prop}


def _reads(raw: Any) -> list[dict[str, str]] | dict[str, Any]:
    """Declarative page reads: a selector and what to read off it, never script."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_READS:
        return _ops.refusal("bad_read", f"`read` is a list of at most {MAX_READS} entries")
    out = []
    for i, entry in enumerate(raw):
        item = _read_entry(entry) or {}
        sel = _text(item.get("sel"), _SELECTOR_CAP)
        prop = item.get("prop", "text")
        if sel is None or not isinstance(prop, str) or not _READ_PROP.match(prop):
            return _ops.refusal(
                "bad_read", "each `read` entry is 'name=css@text|value|checked|box|count|attr:NAME'"
            )
        name = _text(item.get("name"), 40) or f"r{i}"
        out.append({"name": name, "sel": sel, "prop": prop})
    return out


def values_sourced(plan: Plan, page_text: str) -> str | None:
    """The first value to type found in neither the goal, a fact nor the page, or ``None``."""
    sources = [plan.goal, *plan.facts, page_text]
    wanted = [str(m.args["text"]) for m in plan.moves if m.kind == "type"] + plan.values
    for value in wanted:
        if not any(value in source for source in sources):
            return value
    return None


def arrange(slots: list[dict[str, Any]], items: list[dict[str, Any]] | None) -> list[list[str]]:
    """Slots (cells) as rows by their centres; with *items*, each slot takes the
    highest-numbered (else last) item whose centre is nearest to it."""
    if not slots:
        return []
    values = [str(s.get("t") or "") for s in slots]
    if items is not None:
        values = [""] * len(slots)
        for item in items:
            try:
                ix, iy = float(item["x"]), float(item["y"])
            except (KeyError, TypeError, ValueError):
                continue
            best = min(
                range(len(slots)),
                key=lambda i: (float(slots[i]["x"]) - ix) ** 2 + (float(slots[i]["y"]) - iy) ** 2,
            )
            text = str(item.get("t") or "")
            values[best] = _higher(values[best], text)
    rows: list[list[tuple[float, str]]] = []
    tops: list[float] = []
    order = sorted(range(len(slots)), key=lambda i: (float(slots[i]["y"]), float(slots[i]["x"])))
    for i in order:
        y = float(slots[i]["y"])
        if tops and abs(y - tops[-1]) < 4.0:
            rows[-1].append((float(slots[i]["x"]), values[i]))
        else:
            tops.append(y)
            rows.append([(float(slots[i]["x"]), values[i])])
    return [[v[:16] for _x, v in sorted(row)][:GRID_CELL_CAP] for row in rows][:GRID_CELL_CAP]


def _higher(a: str, b: str) -> str:
    """Two tiles in one slot (a merge still animating): the larger number wins."""
    if not a:
        return b
    try:
        return a if float(a) >= float(b) else b
    except ValueError:
        return b or a


async def _cells(ctx: _ops.OpContext, selector: str) -> list[dict[str, Any]] | None:
    found = await _ops._page(ctx, "cells", selector)
    if (
        not isinstance(found, dict)
        or not found.get("ok")
        or not isinstance(found.get("cells"), list)
    ):
        return None
    return [c for c in found["cells"] if isinstance(c, dict)]


class Loop:
    """The state one ``reflex`` call carries, and the parts every mode shares."""

    def __init__(
        self, ctx: _ops.OpContext, plan: Plan, session_key: str, image: bool, caps: Any
    ) -> None:
        self.ctx = ctx
        self.plan = plan
        self.session_key = session_key
        self.image = image
        self.caps = caps
        self.moves: list[str] = []
        self.counts: dict[str, int] = {}
        self.decisions = 0
        self.answered = 0
        self.failed = 0
        self.decision_ms = 0
        self.rtts: list[float] = []
        self.known: dict[str, Move] = {}
        self.pending_risk: list[Move] = []
        self.dropped: set[str] = set()
        self.step_no = 0
        self.notes: list[str] = []
        self.extra: dict[str, Any] = {}
        self.started = time.monotonic()
        latency = getattr(caps, "latency_ms", 0) or 0
        #: A provider that says it is slow gets a longer per-call ceiling.
        self.wait_s = latency * 3 / 1000.0 if latency else None

    # -- observation ------------------------------------------------------------

    async def observe(self, *, frames: bool = True) -> Seen | None:
        plan = self.plan
        opts: dict[str, Any] = {"table": bool(plan.targets), "text": plan.text}
        if plan.region:
            opts["region"] = plan.region
        if plan.read:
            opts["read"] = plan.read
        if plan.until_text:
            opts["until"] = plan.until_text
        try:
            seen = await _ops._page(self.ctx, "observe", opts)
        except Exception:  # noqa: BLE001 - mid-navigation; the caller judges a None
            logger.debug("reflex: observe failed", exc_info=True)
            return None
        if not isinstance(seen, dict) or not seen.get("ok"):
            return None
        state: dict[str, Any] = {"goal": plan.goal}
        if plan.facts:
            state["facts"] = plan.facts
        state["page_title"] = str(seen.get("title", ""))
        rows = seen.get("elements") if isinstance(seen.get("elements"), list) else None
        if rows is not None:
            state["elements"] = _space.element_state(rows)
        if plan.text:
            state["text"] = str(seen.get("text", ""))
        if plan.read:
            state["read"] = seen.get("read") or {}
        fp = str(seen.get("fp", ""))
        if plan.grid:
            slots = await _cells(self.ctx, plan.grid)
            items = await _cells(self.ctx, plan.grid_items) if plan.grid_items else None
            if slots is None or (plan.grid_items and items is None):
                return None
            grid = arrange(slots, items)
            state["grid"] = grid
            fp += repr(grid)
        frame = None
        if self.image and frames:
            # The masked-frame rules of every screenshot the seam sends: none while a
            # password box is on the page or the person holds the browser. On a
            # canvas the DOM never changes, so the frame is the fingerprint too.
            frame = await _decide._capture(self.ctx)
            if frame is not None:
                fp += hashlib.sha256(frame).hexdigest()
        mut = seen.get("mut")
        return Seen(
            state=state,
            fp=fp,
            frame=frame,
            rows=rows,
            mut=mut if isinstance(mut, int) else 0,
            url=str(seen.get("url", "")),
            until_hit=bool(seen.get("until"))
            or bool(plan.until_url and plan.until_url in str(seen.get("url", ""))),
        )

    # -- the offer and one decision ---------------------------------------------

    def offer(self, move: Move) -> bool:
        return move.key not in self.dropped and (move.risky or 0.0) < RISK_STOP

    def space(self, seen: Seen, *, wait: bool) -> Space:
        plan = self.plan
        return _space.build(
            plan.moves,
            plan.targets,
            plan.allowed_refs,
            seen.rows,
            plan.values,
            self.known,
            wait=wait,
            offer=self.offer,
        )

    def risk_batch(self, space: Space) -> list[Move]:
        """Moves to judge with this request: picked-but-unjudged first, then the rest."""
        batch: list[Move] = []
        offered = [m for cands in space.targets.values() for m in cands.values()]
        for move in [*self.pending_risk, *space.moves.values(), *offered]:
            if move.risky is None and move not in batch:
                batch.append(move)
            if len(batch) >= RISK_PER_REQUEST:
                break
        return batch

    def rtt_ms(self, share: float) -> int:
        """The *share* quantile of the recent round trips; the declared latency before any."""
        recent = sorted(self.rtts[-LEAD_WINDOW:])
        if not recent:
            return int(getattr(self.caps, "latency_ms", 0) or 0)
        return int(recent[min(len(recent) - 1, int(len(recent) * share))] * 1000)

    def lead_ms(self) -> int:
        """How far ahead a pipelined question looks: the recent p90 round trip, so
        nearly every answer is early and is held to the moment it was about, rather
        than half of them landing late."""
        return self.rtt_ms(LEAD_QUANTILE)

    async def request(
        self, seen: Seen, space: Space, risk: list[Move], *, lead_ms: int | None = None
    ) -> dict[str, Any] | None:
        state = seen.state
        if lead_ms is not None:
            # The state is this many ms old when the answer lands; the model may
            # project it forward. Code projects nothing.
            state = {**state, "lead_ms": lead_ms}
        self.decisions += 1
        # The words each risk question asks about, as this request sends them.
        asked = [m.label for m in risk]
        started = time.monotonic()
        verdict = await _decide.reflex_step(
            self.session_key,
            state,
            space.operations,
            space.target_criteria(),
            space.values,
            asked,
            until=self.plan.until_noul,
            subject="page" if not self.plan.grid else "grid",
            frame=seen.frame,
            step=self.decisions,
            wait_s=self.wait_s,
        )
        elapsed = time.monotonic() - started
        if verdict is None:
            self.failed += 1
            return None
        self.answered += 1
        self.decision_ms += int(elapsed * 1000)
        self.rtts.append(elapsed)
        for move, label, value in zip(risk, asked, verdict["risk"]):
            if move.label != label:
                # The element reads differently now: this verdict was about its old
                # words, so it waits for a question about the new ones.
                continue
            # Two requests in flight may judge the same move: the more cautious
            # answer stands, so the order the answers land in cannot lower it.
            move.risky = value if move.risky is None else max(move.risky, value)
            if move in self.pending_risk:
                self.pending_risk.remove(move)
        return verdict

    def stop_for(self, verdict: dict[str, Any]) -> str | None:
        if verdict["done"] > _decide.YES:
            return STOP_DONE
        if verdict["stuck"] > _decide.YES:
            return STOP_STUCK
        if verdict["off_path"] > _decide.YES:
            return STOP_OFF_PATH
        if verdict["until"] > _decide.YES:
            return STOP_UNTIL
        return None

    # -- acting -----------------------------------------------------------------

    async def act(self, move: Move, value: str | None, guard: str | None = None) -> dict[str, Any]:
        """Run *move* through the executor's guards and the ordinary op handlers.

        *guard* is the target's guard as the request that chose it saw it
        (``Space.guards``); the move's own is the latest any request saw.
        """
        ctx = self.ctx
        if move.kind == "wait":
            return {"ok": True}
        self.step_no += 1
        rate = self.rate()
        prefix = f"Reflex {self.step_no}: "
        suffix = f" ({rate:.1f}/s)" if rate else ""

        def _step(label: str) -> Any:
            return ctx.step(f"{prefix}{label}{suffix}")

        inner = dataclasses.replace(ctx, step=_step, decisions=None)
        handlers = _ops._HANDLERS
        args = dict(move.args)
        mark = ctx.mark()
        if move.kind == "key":
            op, result = "press_key", await handlers["press_key"](inner, args)
        elif move.kind == "drag":
            op, result = "drag", await handlers["drag"](inner, args)
        elif move.kind == "scroll":
            info = await ctx.helper("page_info")
            height = float(info.get("h", 800)) if isinstance(info, dict) else 800.0
            args = {"dy": args["dy"] * round(height * 0.8)}
            op, result = "scroll", await handlers["scroll"](inner, args)
        else:
            op = {"select": "select_option"}.get(move.kind, move.kind)
            result = await self._targeted(inner, move, value, guard)
        # Each action is its own step on the turn replay's timeline.
        ctx.record(op, args, result, mark)
        if result.get("ok"):
            self.moves.append(move.label)
            self.counts[move.label] = self.counts.get(move.label, 0) + 1
        else:
            self.step_no -= 1
        self.report(None)
        return result

    def report(self, stop: str | None) -> None:
        """The live card's loop line: counters so far, and ``stop`` once it ended."""
        elapsed = time.monotonic() - self.started
        self.ctx.loop(
            {
                "moves": len(self.moves),
                "rate": round(len(self.moves) / elapsed, 2) if elapsed > 0 else 0.0,
                "steps": self.step_no,
                "max_steps": self.plan.max_steps,
                "seconds": round(elapsed, 2),
                "max_seconds": self.plan.max_seconds,
                "stop": stop,
            }
        )

    async def _targeted(
        self, inner: _ops.OpContext, move: Move, value: str | None, guard: str | None
    ) -> dict[str, Any]:
        ref = move.args["ref"]
        kind = "click" if move.kind == "hover" else move.kind
        seen = guard if guard is not None else move.guard
        found = await _ops._page(
            self.ctx, "check", ref, kind, seen or None, move.args.get("index", -1)
        )
        if not isinstance(found, dict) or not found.get("ok"):
            code = found.get("code") if isinstance(found, dict) else "gone"
            if code == "password_field":
                return _ops._password_refusal(ref)
            return _ops.refusal(str(code or "gone"), f"{ref} changed before the answer was used")
        if move.kind in ("click", "hover"):
            x, y = float(found["x"]), float(found["y"])
            await _ops._step(inner, move.label)
            if move.kind == "click":
                await self.ctx.helper("click", x=x, y=y, button="left", clicks=1)
            else:
                await self.ctx.helper("mouse_move", x=x, y=y)
            return {"ok": True, "x": x, "y": y}
        if move.kind == "select":
            return await _ops._HANDLERS["select_option"](
                inner, {"ref": ref, "values": [str(found.get("value", ""))]}
            )
        text = move.args.get("text", value)
        if not isinstance(text, str) or not text:
            return _ops.refusal("bad_value", "no bound value to type")
        selected = await _ops._page(self.ctx, "selectText", ref)
        if isinstance(selected, dict) and selected.get("code") == "password_field":
            return _ops._password_refusal(ref)
        return await _ops._HANDLERS["type"](inner, {"ref": ref, "text": text})

    async def settle(self, move: Move, rows: list[dict[str, Any]] | None) -> None:
        """The short wait after an action, before the next read."""
        if self.plan.settle_s is not None:
            if self.plan.settle_s:
                await self.ctx.sleep(self.plan.settle_s)
            return
        if move.kind == "wait":
            await self.ctx.sleep(WAIT_S)
            return
        combobox = move.kind == "type" and any(
            r.get("ref") == move.args.get("ref") and r.get("role") == "combobox" for r in rows or []
        )
        deadline = time.monotonic() + (SETTLE_COMBOBOX_S if combobox else SETTLE_S)
        first = await _ops._page(self.ctx, "ticks")
        start = first.get("f", 0) if isinstance(first, dict) else 0
        while time.monotonic() < deadline:
            if combobox:
                if await _ops._page(self.ctx, "optionsShown", move.args["ref"]):
                    return
            else:
                now = await _ops._page(self.ctx, "ticks")
                if isinstance(now, dict) and now.get("f", 0) >= start + SETTLE_FRAMES:
                    return
            await self.ctx.sleep(SETTLE_POLL_S)

    def rate(self) -> float:
        elapsed = time.monotonic() - self.started
        return len(self.moves) / elapsed if elapsed > 0 and self.moves else 0.0

    async def landed_refusal(self) -> dict[str, Any] | None:
        return await _landed_refusal(self.ctx)

    def over_budget(self) -> bool:
        return self.decisions >= self.plan.max_decisions or self.out_of_time_or_steps()

    def out_of_time_or_steps(self) -> bool:
        """Whether an answer already in hand may still be acted on."""
        plan = self.plan
        return self.step_no >= plan.max_steps or time.monotonic() >= self.started + plan.max_seconds

    def summary(self, stop: str, state: dict[str, Any] | None, **more: Any) -> dict[str, Any]:
        elapsed = time.monotonic() - self.started
        out: dict[str, Any] = {
            "ok": True,
            "stop": stop,
            "mode": self.plan.mode,
            "steps": self.step_no,
            "moves": len(self.moves),
            "recent": self.moves[-RECENT_MOVES:],
            "actions": dict(sorted(self.counts.items())),
            "decisions": self.decisions,
            "decision_ms_avg": round(self.decision_ms / self.answered) if self.answered else 0,
            "seconds": round(elapsed, 2),
            "rate": round(len(self.moves) / elapsed, 2) if elapsed > 0 else 0.0,
            "see": self.image,
        }
        if self.failed:
            out["failed"] = self.failed
        out.update(self.extra)
        if self.notes:
            out["notes"] = list(self.notes)
        if state is not None:
            out["state"] = (
                state.get("grid")
                if "grid" in state
                else state.get("read") if "read" in state else str(state.get("text", ""))[:1500]
            )
        out.update(more)
        return out

    async def finish(self, stop: str, state: dict[str, Any] | None, **more: Any) -> dict[str, Any]:
        result = self.summary(stop, state, **more)
        await _ops._step(
            self.ctx, f"Reflex {stop}: {result['moves']} moves, {result['rate']:.1f}/s"
        )
        self.report(stop)
        return result

    # -- step mode ----------------------------------------------------------------

    async def run_step(self) -> dict[str, Any]:
        last_fp: str | None = None
        last_pick: str | None = None
        state: dict[str, Any] | None = None
        while True:
            if await _ops._in_control(self.ctx):
                return await self.finish(STOP_USER, state)
            # The URL gate runs before the page is read or sent anywhere, so a redirect
            # that landed after the last action (or a takeover navigation) is caught
            # before its content reaches the decision provider or a terminal verdict.
            refused = await self.landed_refusal()
            if refused is not None:
                return await self.finish(STOP_REFUSED, None, error=refused["error"])
            seen = await self.observe()
            if seen is None:
                if self.decisions == 0:
                    return not_available("the page state could not be read")
                return await self.finish(STOP_DECISION, state, why="page state unreadable")
            state = seen.state
            # No-op withdrawal: an action that left the state as it was is not
            # offered again until the state changes.
            if last_fp is not None:
                if seen.fp == last_fp and last_pick is not None:
                    self.dropped.add(last_pick)
                elif seen.fp != last_fp:
                    self.dropped.clear()
            last_fp, last_pick = seen.fp, None
            if seen.until_hit:
                return await self.finish(STOP_UNTIL, state)
            if self.over_budget():
                return await self.finish(STOP_BUDGET, state)
            space = self.space(seen, wait=self.plan.wait)
            if not space.operations:
                return await self._nothing_left(state)
            verdict = await self.request(seen, space, self.risk_batch(space))
            if verdict is None:
                if self.answered == 0:
                    return not_available("no decision model answered")
                return await self.finish(STOP_DECISION, state)
            stop = self.stop_for(verdict)
            if stop is not None:
                return await self.finish(stop, state)
            move = space.resolve(verdict["operation"], verdict["picks"])
            if move is None:
                return await self.finish(STOP_DECISION, state, why="no action picked")
            if move.risky is None:
                # Judged before it may run: the next request carries its risk question.
                self.pending_risk.append(move)
                continue
            if move.risky >= RISK_STOP:
                return await self.finish(STOP_RISK, state, handoff=_handoff(move.label))
            if self.out_of_time_or_steps():
                # The answer came back past the deadline: it is not acted on.
                return await self.finish(STOP_BUDGET, state)
            if await _ops._in_control(self.ctx):
                return await self.finish(STOP_USER, state)
            result = await self.act(move, verdict["value"], space.guards.get(move.key))
            if not result.get("ok"):
                if result.get("code") == "password_field":
                    return await self.finish(STOP_RISK, state, handoff=result["error"])
                if result.get("code") not in _STALE_CODES:
                    # A move that failed for good (an unfocusable field, a refused
                    # key): withdraw it. A target that was only stale (moved,
                    # covered, re-rendered) stays on offer; the decision and time
                    # budgets bound a page that never settles.
                    self.dropped.add(move.key)
                continue
            last_pick = move.key
            await self.settle(move, seen.rows)

    async def _nothing_left(self, state: dict[str, Any] | None) -> dict[str, Any]:
        risky = [m.label for m in self.known.values() if (m.risky or 0.0) >= RISK_STOP]
        if risky and not any(self.offer(m) for m in self.plan.moves) and not self.plan.targets:
            return await self.finish(STOP_RISK, state, handoff=_handoff(risky[0]))
        return await self.finish(STOP_NO_ACTIONS, state)


async def _landed_refusal(ctx: _ops.OpContext) -> dict[str, Any] | None:
    """The URL gate on the page the tab is on now, before it is read or sent."""
    try:
        state = await _ops._page(ctx, "state")
    except Exception:  # noqa: BLE001 - mid-navigation; the next read judges it
        return None
    href = str(state.get("href", "")) if isinstance(state, dict) else ""
    if href and href != "about:blank":
        return await _ops._refuse_committed_url(ctx, href)
    return None


def _handoff(label: str) -> str:
    return (
        f"'{label}' looks hard to undo, so the loop stopped before it. Ask the user to "
        "take over the browser or to confirm, then do that step with a single op."
    )


def _pace(plan: Plan, caps: Any) -> tuple[str, str | None]:
    """The mode this provider can carry for *plan*, and why it changed, if it did."""
    if plan.mode != "realtime":
        return plan.mode, None
    width = int(getattr(caps, "max_in_flight", 1) or 1)
    if width < 2:
        return "virtual_time", "the decision provider takes one request at a time"
    return "realtime", None


async def run(ctx: _ops.OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """The ``reflex`` op. See the module docstring."""
    plan = parse(args)
    if isinstance(plan, dict):
        return plan
    hooks = ctx.decisions
    session_key = getattr(hooks, "session_key", None)
    if not isinstance(session_key, str) or not session_key:
        return not_available("browser help is off")
    try:
        armed = await asyncio.wait_for(
            asyncio.to_thread(_decide._probe, session_key), timeout=_decide.PRE_OP_BUDGET_S
        )
    except asyncio.TimeoutError:
        return not_available("browser help did not answer")
    if not armed.text:
        return not_available("browser help is off")
    if any(m.kind == "type" for m in plan.moves) or plan.values:
        # The page is read below to source the values: the URL gate comes first.
        refused = await _landed_refusal(ctx)
        if refused is not None:
            return refused
        try:
            seen = await _ops._page(ctx, "observe", {"table": True, "text": True})
        except Exception:  # noqa: BLE001 - unreadable page: nothing can be pointed to
            seen = None
        page = ""
        if isinstance(seen, dict):
            names = [
                f"{r.get('name', '')} {r.get('value', '')}" for r in seen.get("elements") or []
            ]
            page = "\n".join([str(seen.get("text", "")), *names])
        missing = values_sourced(plan, page)
        if missing is not None:
            return _ops.refusal(
                "bad_value",
                f"the value {missing[:60]!r} is not in the goal, the facts or the page; "
                "reflex only types values it can point to",
            )
    caps = _decide.capabilities()
    loop = Loop(ctx, plan, session_key, plan.see and armed.image, caps)
    if plan.see and not loop.image:
        loop.notes.append("screenshots are off (not granted or not supported); state only")
    mode, why = _pace(plan, caps)
    if why:
        loop.notes.append(f"realtime ran as virtual_time: {why}")
    loop.plan = dataclasses.replace(plan, mode=mode)
    loop.report(None)
    if mode == "step":
        result = await loop.run_step()
    else:
        timed = importlib.import_module("kiro_crew.browser_harness.reflex_realtime")
        result = await timed.run(loop)
    if "stop" not in result:
        # Ended before any verdict (``not_available``): the loop line still ends.
        loop.report(STOP_DECISION)
    return result  # type: ignore[no-any-return]
