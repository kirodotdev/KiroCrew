"""Optional decision-model help for browser ops: the ``browser.step`` point.

The browser works fully without this module doing anything. When the owner's
decision seam is armed and the point's ``browser`` scope is on (the default once
consent stands), three things may happen, each one request through
``kiro_crew.decisions.gate.decide`` and so through the provider registry:

* **pick** -- an op that names its element by description (``target``) instead of
  a ``ref`` gets one Choice over the outline's refs, asked together with "is it
  there" and three "does this need the big model" Nouls. The pick is used only on a
  clear answer; otherwise the op is refused with ``needs_ref``.
* **verify** -- after ``click``, ``type`` and ``navigate`` the before and after
  outlines (or screenshots, with the ``browser_images`` scope) are compared and the
  answer rides on the op result as a ``verify`` hint. The op's own result is never
  changed.
* **reflex** -- one request per decision of the ``reflex`` op's loop
  (:func:`reflex_step`): an operation head, a speculative target head per
  targeted operation, and the stop Nouls. The loop lives in
  ``browser_harness.reflex``; it reads the provider's declared capabilities
  (:func:`capabilities`) and opens its connection early (:func:`warm`) through
  the gate, never naming a provider.
* **box** -- a ``click``/``hover`` whose target is not in the outline, on a page
  with a visible ``<canvas>``, may be located on a screenshot. Used only when the
  box's centre is inside a canvas.

Every refusal, timeout or unclear answer leaves today's path: no hint, no pick.
No screenshot is taken while a password field has a box on the page (the live
view's mask rule) or while the person holds the browser. The decisions package is
imported inside functions only, so it stays off the browser's import graph.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from kiro_crew.browser_harness import budgets as _budgets
from kiro_crew.browser_harness import ops as _ops

logger = logging.getLogger(__name__)

POINT = "browser.step"
#: The keystone request scope that authorizes screenshots (``consent.STATE_KEY_BROWSER_IMAGES``).
IMAGE_SCOPE = "browser_images"

#: Per-call ceilings. Measured p95 was 177-201 ms for text and 345-363 ms for an
#: image request; each is also capped by ``provider.timeout_ms``.
TEXT_WAIT_S = 0.5
IMAGE_WAIT_S = 0.8
#: Ceilings on the hook's own page work, separate from the op's budget: one frame
#: capture, and the whole step check after the op ran. Past either, the op's own
#: result is returned as it is.
FRAME_WAIT_S = 3.0
VERIFY_BUDGET_S = _budgets.DECISION_POST_OP_S
#: Ceiling on ALL the hook's work before the op (outline, pick, canvas locator,
#: before-frame). With :data:`VERIFY_BUDGET_S` after it, the hooks add at most 12 s
#: to an op, and the route adds exactly that headroom to the op's own budget
#: (``budgets.DECISION_HEADROOM_S``).
PRE_OP_BUDGET_S = _budgets.DECISION_PRE_OP_S

BOX_OPS = frozenset({"click", "hover"})
VERIFY_OPS = frozenset({"click", "type", "navigate"})

#: Noul bands: above YES is yes, below NO is no, between is unsure.
YES = 0.7
NO = 0.3
#: The least probability a Choice pick and a Box confidence must carry to be used.
PICK_P_MIN = 0.6
BOX_CONFIDENCE_MIN = 0.6
#: Choice options per request; the provider takes at most 255.
MAX_OPTIONS = 200
#: Longest image side the provider accepts.
IMAGE_MAX_DIM = 1568

_WORD_RE = re.compile(r"[a-z0-9]+")

_Handler = Callable[[_ops.OpContext, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Armed:
    """What this op may send: page text, and screenshots as well."""

    text: bool = False
    image: bool = False


def _probe(session_key: str) -> Armed:
    """The gate's answer for this session. Blocking; run off the loop."""
    from kiro_crew.decisions import gate

    if not gate.is_enabled(POINT, session_key=session_key):
        return Armed()
    if not gate.request_scope_granted(IMAGE_SCOPE):
        return Armed(text=True)
    return Armed(text=True, image=gate.provider_supports(frozenset({"image"})))


def _band(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "unsure"
    return "yes" if value > YES else "no" if value < NO else "unsure"


def _noul_value(answers: dict[str, Any], qid: str) -> float:
    answer = answers.get(qid)
    value = getattr(answer, "value", None)
    return float(value) if isinstance(value, (int, float)) else 0.5


def candidates(nodes: list[Any]) -> list[tuple[str, str]]:
    """``(ref, description)`` per element with a ref, in page order.

    Each description carries the nearest heading above the element, so two
    identical "Add to cart" buttons under different product headings differ.
    """
    out: list[tuple[str, str]] = []
    heading = ""
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = "" if node.get("name") is None else str(node.get("name"))
        if node.get("role") == "heading" and not node.get("ref"):
            heading = name
            continue
        ref = node.get("ref")
        if not _ops._is_ref(ref):
            continue
        text = f'{node.get("role") or "generic"} "{name}"'
        raw_state = node.get("state")
        state: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}
        flags = [flag for flag in ("password", "checked", "disabled") if state.get(flag)]
        if flags:
            text += " [" + ", ".join(flags) + "]"
        if heading:
            text += f' under heading "{heading}"'
        out.append((str(ref), text))
    return out


def _prefilter(cands: list[tuple[str, str]], target: str) -> list[tuple[str, str]]:
    """At most :data:`MAX_OPTIONS`, the ones sharing most words with *target*, in page order."""
    if len(cands) <= MAX_OPTIONS:
        return cands
    words = set(_WORD_RE.findall(target.lower()))
    scored = sorted(
        range(len(cands)),
        key=lambda i: -len(words & set(_WORD_RE.findall(cands[i][1].lower()))),
    )
    keep = sorted(scored[:MAX_OPTIONS])
    return [cands[i] for i in keep]


def _expectation(op: str, args: dict[str, Any], result: dict[str, Any]) -> str:
    if op == "navigate":
        # Never the URL: an address can carry a token the scrub does not recognise.
        return "the requested page opened"
    if op == "type":
        submitted = " and submitted" if args.get("submit") else ""
        return f"text was typed into the field{submitted}"
    ref = result.get("ref") or args.get("ref")
    return f"the result of clicking {ref or 'the page'}"


class BrowserDecisions:
    """The ``browser.step`` hooks for one caller's session. Holds no page state."""

    def __init__(self, session_key: str) -> None:
        self.session_key = session_key

    async def run(
        self, op: str, args: dict[str, Any], ctx: _ops.OpContext, handler: _Handler
    ) -> dict[str, Any]:
        """Run *handler* with whatever help is armed; today's path when none is."""
        wants_pick = _ops.names_target_only(op, args)
        wants_verify = op in VERIFY_OPS
        if not (wants_pick or wants_verify):
            return await handler(ctx, args)
        # The gate probe and every page call before the op share ONE bound, so the
        # op keeps its own budget whatever the keystone read or the page does.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PRE_OP_BUDGET_S
        try:
            armed = await asyncio.wait_for(
                asyncio.to_thread(_probe, self.session_key), timeout=PRE_OP_BUDGET_S
            )
        except asyncio.TimeoutError:
            armed = Armed()
        if not armed.text:
            return _ops.needs_ref() if wants_pick else await handler(ctx, args)

        try:
            before_outline, picked, before_frame = await asyncio.wait_for(
                self._before(ctx, op, args, armed, wants_pick, wants_verify),
                timeout=max(0.0, deadline - loop.time()),
            )
        except asyncio.TimeoutError:
            before_outline, picked, before_frame = None, None, None
        if wants_pick:
            if picked is None:
                return _ops.needs_ref()
            args = {**args, **picked["args"]}

        result = await handler(ctx, args)
        if picked is not None:
            result = {**result, "picked": picked["hint"]}
        if not (wants_verify and result.get("ok")):
            return result
        try:
            hint = await asyncio.wait_for(
                self._verify(ctx, op, args, result, before_outline, before_frame),
                timeout=VERIFY_BUDGET_S,
            )
        except asyncio.TimeoutError:
            # The op already ran: its result stands, only the hint is lost.
            return result
        return {**result, "verify": hint} if hint else result

    async def _before(
        self,
        ctx: _ops.OpContext,
        op: str,
        args: dict[str, Any],
        armed: Armed,
        wants_pick: bool,
        wants_verify: bool,
    ) -> tuple[str | None, dict[str, Any] | None, bytes | None]:
        """The work before the op: ``(outline, pick, before-frame)``, each optional."""
        snap = await _snapshot(ctx)
        picked = None
        if wants_pick and snap is not None:
            picked = await self._pick(ctx, op, str(args["target"]), snap, armed)
            if picked is None:
                return None, None, None
        before_frame = await self._frame(ctx) if armed.image and wants_verify else None
        return (snap[0] if snap else None), picked, before_frame

    async def _pick(
        self,
        ctx: _ops.OpContext,
        op: str,
        target: str,
        snap: tuple[str, str, list[Any]],
        armed: Armed,
    ) -> dict[str, Any] | None:
        from kiro_crew.decisions import gate
        from kiro_crew.decisions.types import Choice, Noul, Question

        outline, title, nodes = snap
        cands = _prefilter(candidates(nodes), target)
        if not cands:
            return await self._box(ctx, op, target, armed)
        questions: list[Question] = [
            Choice(
                id="pick",
                prompt="Which control on `page` should the user use next to do `goal`?",
                options=[ref for ref, _text in cands],
                criteria=dict(cands),
            ),
            Noul(
                id="present",
                prompt="Does `page` have a control that does `goal`?",
                true_means="One of the controls in `page` does what `goal` asks.",
                false_means="No control in `page` does what `goal` asks.",
            ),
            Noul(id="error_shown", prompt="Does `page` show an error message?"),
            Noul(id="not_found", prompt="Is `page` a 'page not found' page?"),
            Noul(id="bot_check", prompt="Is `page` asking the user to prove they are a human?"),
        ]
        state = {"goal": target, "page_title": title, "page": outline}
        answers = await gate.decide(
            POINT,
            state,
            questions,
            session_key=self.session_key,
            extra={"kind": "pick", "op": op},
            max_wait_s=TEXT_WAIT_S,
        )
        if answers is None:
            return None
        present = _noul_value(answers, "present")
        risky = any(
            _noul_value(answers, q) > YES for q in ("error_shown", "not_found", "bot_check")
        )
        choice = answers.get("pick")
        if present > YES and not risky and choice is not None and choice.p >= PICK_P_MIN:
            ref = str(choice.value)
            name = dict(cands).get(ref, "")
            return {"args": {"ref": ref}, "hint": {"ref": ref, "element": name, "by": "outline"}}
        if present < NO and not risky:
            return await self._box(ctx, op, target, armed)
        return None

    async def _box(
        self, ctx: _ops.OpContext, op: str, target: str, armed: Armed
    ) -> dict[str, Any] | None:
        """Locate *target* on a screenshot, only where it lands inside a visible canvas."""
        if op not in BOX_OPS or not armed.image:
            return None
        try:
            probe = await _ops._page(ctx, "canvasRects")
        except Exception:  # noqa: BLE001 - no locator is today's refusal, never a failed op
            logger.debug("browser decisions: canvas probe failed", exc_info=True)
            return None
        # The viewport comes from the same call as the canvas boxes, so the box is
        # scaled into the CSS pixels the canvases and ``click`` use.
        if not isinstance(probe, dict) or not probe.get("w") or not probe.get("h"):
            return None
        rects = probe.get("rects")
        if not isinstance(rects, list) or not rects:
            return None
        frame = await self._frame(ctx)
        if frame is None:
            return None
        from kiro_crew.decisions import gate
        from kiro_crew.decisions.types import Box, ImageAsset, Noul

        screen = str(uuid.uuid4())
        answers = await gate.decide(
            POINT,
            {"goal": target, "screen": screen},
            [
                Noul(id="present", prompt="Is the thing `goal` describes visible in `screen`?"),
                Box(id="where", prompt="the thing `goal` describes, in `screen`"),
            ],
            session_key=self.session_key,
            extra={"kind": "box", "op": op},
            scopes=(IMAGE_SCOPE,),
            images={screen: ImageAsset(frame)},
            max_wait_s=IMAGE_WAIT_S,
        )
        if answers is None or _noul_value(answers, "present") <= YES:
            return None
        where = answers.get("where")
        if where is None or where.p < BOX_CONFIDENCE_MIN or not isinstance(where.value, dict):
            return None
        box = where.value
        x = (box["left"] + box["right"]) / 2 * float(probe["w"])
        y = (box["top"] + box["bottom"]) / 2 * float(probe["h"])
        if not any(_inside(x, y, rect) for rect in rects):
            return None
        return {
            "args": {"x": round(x, 1), "y": round(y, 1)},
            "hint": {"x": round(x, 1), "y": round(y, 1), "by": "screenshot"},
        }

    async def _verify(
        self,
        ctx: _ops.OpContext,
        op: str,
        args: dict[str, Any],
        result: dict[str, Any],
        before_outline: str | None,
        before_frame: bytes | None,
    ) -> dict[str, Any] | None:
        from kiro_crew.decisions import gate
        from kiro_crew.decisions.types import ImageAsset, Noul, Question

        expect = _expectation(op, args, result)
        images: dict[str, Any] | None = None
        after_frame = await self._frame(ctx) if before_frame is not None else None
        if before_frame is not None and after_frame is not None:
            before, after = str(uuid.uuid4()), str(uuid.uuid4())
            state: dict[str, Any] = {"action": op, "before": before, "after": after}
            images = {before: ImageAsset(before_frame), after: ImageAsset(after_frame)}
            names, by = ("before", "after"), "screenshot"
        else:
            if before_outline is None:
                return None
            snap = await _snapshot(ctx)
            if snap is None:
                return None
            state = {"action": op, "before_page": before_outline, "after_page": snap[0]}
            names, by = ("before_page", "after_page"), "outline"
        b, a = names
        questions: list[Question] = [
            Noul(id="as_expected", prompt=f"Compared with `{b}`, does `{a}` show {expect}?"),
            Noul(id="changed", prompt=f"Does `{a}` look different from `{b}`?"),
            Noul(id="error_shown", prompt=f"Does `{a}` show an error message?"),
        ]
        answers = await gate.decide(
            POINT,
            state,
            questions,
            session_key=self.session_key,
            extra={"kind": "verify", "op": op, "by": by},
            scopes=(IMAGE_SCOPE,) if images else (),
            images=images,
            max_wait_s=IMAGE_WAIT_S if images else TEXT_WAIT_S,
        )
        if answers is None:
            return None
        hint = {
            qid: _band(_noul_value(answers, qid))
            for qid in ("as_expected", "changed", "error_shown")
        }
        return {**hint, "by": by}

    async def _frame(self, ctx: _ops.OpContext) -> bytes | None:
        """One PNG of the agent's tab, or ``None`` when one may not be sent now.

        The checks and the capture share one :data:`FRAME_WAIT_S` bound, so a page
        that stalls any of them costs the op at most that long.
        """
        try:
            return await asyncio.wait_for(_capture(ctx), timeout=FRAME_WAIT_S)
        except asyncio.TimeoutError:
            return None


async def _capture(ctx: _ops.OpContext) -> bytes | None:
    """The checks, the screenshot and the re-check, unbounded; see ``_frame``."""
    if not await _may_capture(ctx):
        return None
    path = None
    try:
        path = await asyncio.to_thread(_ops._screenshot_path, ctx)
        await ctx.helper("screenshot", path=str(path), max_dim=IMAGE_MAX_DIM)
        data = await asyncio.to_thread(path.read_bytes)
    except Exception:  # noqa: BLE001 - no frame is the text path, never a failed op
        logger.debug("browser decisions: frame capture failed", exc_info=True)
        return None
    finally:
        if path is not None:
            await asyncio.to_thread(_unlink_quietly, path)
    # Judged again AFTER the capture: a password field or a takeover that
    # arrived while the frame was taken discards it.
    if not await _may_capture(ctx):
        return None
    return data or None


async def _may_capture(ctx: _ops.OpContext) -> bool:
    """No takeover, no password box, and no field the person filled through a quick
    ask still holding its value (unknown counts as one)."""
    if await _ops._in_control(ctx):
        return False
    try:
        boxes = await _ops._page(ctx, "passwordBoxes")
        asked = await _ops._page(ctx, "askedValues")
    except Exception:  # noqa: BLE001 - unknown means a password may be on screen
        return False
    return boxes == 0 and asked == 0


def _unlink_quietly(path: Any) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("browser decisions: could not delete a frame", exc_info=True)


def _inside(x: float, y: float, rect: Any) -> bool:
    try:
        left, top, width, height = (float(v) for v in rect)
    except (TypeError, ValueError):
        return False
    return left <= x <= left + width and top <= y <= top + height


async def _snapshot(ctx: _ops.OpContext) -> tuple[str, str, list[Any]] | None:
    """``(outline, title, nodes)`` of the current page, or ``None``."""
    try:
        data = await _ops._page(ctx, "snapshot")
    except Exception:  # noqa: BLE001 - no outline means no decision, never a failed op
        logger.debug("browser decisions: snapshot failed", exc_info=True)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        return None
    outline = _ops.format_outline(data["nodes"])[: _ops.TEXT_CAP]
    return outline, str(data.get("title", "")), data["nodes"]


def capabilities() -> Any:
    """What the configured provider declares (``decisions.provider.Capabilities``), or ``None``.

    The ``reflex`` loop paces itself by these, never by a provider's name.
    """
    from kiro_crew.decisions import gate

    return gate.capabilities()


async def warm(session_key: str) -> bool:
    """Open the provider's connection before a burst of ``reflex`` decisions."""
    from kiro_crew.decisions import gate

    return await gate.warm(POINT, session_key=session_key)


async def reflex_step(
    session_key: str,
    state: dict[str, Any],
    operations: dict[str, str],
    targets: dict[str, dict[str, str]],
    values: list[str],
    risk_labels: list[str],
    *,
    until: str | None,
    subject: str,
    frame: bytes | None,
    step: int,
    wait_s: float | None = None,
) -> dict[str, Any] | None:
    """One decision of the ``reflex`` loop (``browser_harness.reflex``): one request.

    A Choice ``operation`` over *operations* (option name -> label); per targeted
    operation a Choice ``<operation>_target`` over its elements (option -> words),
    each asked as if that operation were the one picked; a Choice ``value`` over
    *values* when typing is offered with more than one; the ``done``, ``stuck`` and
    ``off_path`` Nouls, the caller's own ``until`` Noul, and a ``risk_<i>`` Noul per
    label in *risk_labels*. With *frame* the masked screenshot goes along under the
    image scope and the questions name it. Answers ``{operation, picks, value, done,
    stuck, off_path, until, risk}`` or ``None`` for every refusal; the loop decides.
    *wait_s* widens the per-call ceiling for a provider that declares itself slow;
    the gate still clamps it to the provider's own budget.
    """
    from kiro_crew.decisions import gate
    from kiro_crew.decisions.types import Choice, ImageAsset, Noul, Question

    images = None
    scopes: tuple[str, ...] = ()
    named = f"`{subject}`"
    if frame is not None:
        screen = str(uuid.uuid4())
        state = {**state, "screen": screen}
        images = {screen: ImageAsset(frame)}
        scopes = (IMAGE_SCOPE,)
        named = "`screen`"
    questions: list[Question] = [
        Choice(
            id="operation",
            prompt=f"Which operation, done now, moves {named} closest to `goal`?",
            options=list(operations),
            criteria=dict(operations),
        )
    ]
    for op, cands in targets.items():
        questions.append(
            Choice(
                id=f"{op.lower()}_target",
                prompt=(
                    f"If the next operation is {op}, which element in `elements` should it "
                    "act on to move toward `goal`? Another question decides the operation."
                ),
                options=list(cands),
                criteria=dict(cands),
            )
        )
    if "TYPE_TEXT" in targets and len(values) > 1:
        questions.append(
            Choice(
                id="value",
                prompt="If text is typed next, which of these values belongs in the field?",
                options=[f"v{i}" for i in range(len(values))],
                criteria={f"v{i}": v for i, v in enumerate(values)},
            )
        )
    questions += [
        Noul(
            id="done",
            prompt=f"Does {named} show that `goal` has been reached?",
            true_means="The goal is fully reached in what is shown.",
            false_means="Something the goal asks for is still left to do.",
        ),
        Noul(
            id="stuck",
            prompt=f"Does {named} show that no further progress toward `goal` is possible?",
            true_means=(
                "The page shows the task can no longer continue, such as a game over "
                "or a blocking error."
            ),
            false_means="There is still a way to keep going.",
        ),
        Noul(
            id="off_path",
            prompt=f"Is {named} about something unrelated to `goal`?",
            true_means=(
                "The page shown is not where the goal can be done, such as a sign-in "
                "wall or another site."
            ),
            false_means="The page shown is where the goal is being done.",
        ),
    ]
    if until:
        questions.append(Noul(id="until", prompt=f"Looking at {named}: {until}"))
    for i, label in enumerate(risk_labels):
        questions.append(
            Noul(
                id=f"risk_{i}",
                prompt=(
                    f"Would doing '{label}' on this page be hard to undo, such as "
                    "paying, deleting, sending or publishing?"
                ),
                true_means="It commits something that cannot simply be taken back.",
                false_means=(
                    "It only moves, selects, plays or fills in, and can be undone or redone."
                ),
            )
        )
    answers = await gate.decide(
        POINT,
        state,
        questions,
        session_key=session_key,
        extra={"kind": "reflex", "step": step},
        scopes=scopes,
        images=images,
        max_wait_s=max(wait_s or 0.0, IMAGE_WAIT_S if images else TEXT_WAIT_S),
    )
    if answers is None:
        return None

    def _pick(qid: str) -> str | None:
        answer = answers.get(qid)
        return str(answer.value) if answer is not None else None

    value = values[0] if len(values) == 1 else None
    picked = _pick("value")
    if picked and picked.startswith("v") and picked[1:].isdigit() and int(picked[1:]) < len(values):
        value = values[int(picked[1:])]
    return {
        "operation": _pick("operation"),
        "picks": {op: _pick(f"{op.lower()}_target") for op in targets},
        "value": value,
        "done": _noul_value(answers, "done"),
        "stuck": _noul_value(answers, "stuck"),
        "off_path": _noul_value(answers, "off_path"),
        "until": _noul_value(answers, "until") if until else 0.0,
        "risk": [_noul_value(answers, f"risk_{i}") for i in range(len(risk_labels))],
    }
