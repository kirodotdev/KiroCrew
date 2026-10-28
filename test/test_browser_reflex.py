"""``reflex``: the fast observe -> judge -> act loop inside one browser op.

What is pinned:

* the loop runs with no LLM turn: one decision request per step, the picked
  action through the ordinary op handlers, one step label per action;
* it stops on each of its own reasons -- done, stuck, off-path, a code terminal
  (``until_text``), the step and time budgets, a takeover, an action judged hard
  to undo, every action withdrawn as a no-op, a refused landing URL;
* off (no hooks, a gated-off seam, a first decision that fails) answers
  ``not_available`` with the page untouched and, gated off, no provider call;
* a typed value must come from the goal, a fact or the page.

The page is a fake ``helper`` running a one-row sliding game; the gate, the
keystone and the provider registry are the real ones, with the System One client
replaced by a recording fake.
"""

from __future__ import annotations

import asyncio
import json
import re
import struct
import zlib
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from kiro_crew.browser_harness import budgets
from kiro_crew.browser_harness import decide as decide_mod
from kiro_crew.browser_harness import ops, reflex
from kiro_crew.config.sections import (
    DECISION_PROVIDER_ENDPOINT_DEFAULT,
    DecisionProviderConfig,
    DecisionsConfig,
)
from kiro_crew.decisions import gate
from kiro_crew.decisions.types import Answer, Choice, Noul

SESSION = "dashboard:chat-1"


class Game:
    """A one-row game: ArrowLeft scores, ArrowRight does nothing. ``e1`` is a Pay
    button, ``e2`` a text field whose value shows in the outline."""

    _CALL = re.compile(r"\A\.(\w+)\((.*)\)\Z", re.DOTALL)

    def __init__(self) -> None:
        self.row = ["2", "", "2", ""]
        self.pressed: list[str] = []
        self.clicked: list[Any] = []
        self.typed: list[str] = []
        self.href = "https://game.example/"
        self.over = False
        self.password_boxes = 0
        self.frames = 0

    def elements(self) -> list[dict[str, Any]]:
        return [
            {"ref": "e1", "role": "button", "name": "Pay now", "ops": ["click"], "g": "g1"},
            {
                "ref": "e2",
                "role": "textbox",
                "name": "City",
                "value": "".join(self.typed),
                "ops": ["click", "type"],
                "g": "g2",
            },
        ]

    def _left(self) -> None:
        # Every ArrowLeft scores: the first cell grows, so the state always changes.
        self.row[0] = str(int(self.row[0] or "0") + 2)

    async def helper(self, name: str, **kwargs: Any) -> Any:
        if name == "js":
            expr = kwargs["expression"]
            assert expr.startswith(ops.WALKER_SOURCE)
            match = self._CALL.match(expr[len(ops.WALKER_SOURCE) :])
            assert match, expr[-80:]
            method, args = match.group(1), json.loads(f"[{match.group(2)}]")
            return self._walker(method, args)
        if name == "press_key":
            self.pressed.append(kwargs["key"])
            if kwargs["key"] == "ArrowLeft":
                self._left()
            return None
        if name == "click":
            self.clicked.append((kwargs["x"], kwargs["y"]))
            return None
        if name == "type_text":
            self.typed.append(kwargs["text"])
            return None
        if name == "goto":
            self.href = kwargs["url"]
            return None
        if name == "screenshot":
            # A canvas: the picture changes with the score, the outline does not.
            from pathlib import Path

            Path(kwargs["path"]).write_bytes(_png(int(self.row[0] or "0") + 1))
            return None
        return None

    def _walker(self, method: str, args: list) -> Any:
        if method == "cells":
            if args[0] == ".cell":
                return {
                    "ok": True,
                    "cells": [
                        {"t": t, "x": 10.0 + 20 * i, "y": 5.0} for i, t in enumerate(self.row)
                    ],
                }
            return {"ok": False, "code": "bad_selector"}
        if method == "snapshot":
            nodes = [
                {"depth": 0, "role": "button", "name": "Pay now", "ref": "e1", "state": {}},
                {
                    "depth": 0,
                    "role": "textbox",
                    "name": "City " + "".join(self.typed),
                    "ref": "e2",
                    "state": {},
                },
                {"depth": 0, "role": "generic", "name": " ".join(t or "." for t in self.row)},
            ]
            return {"url": self.href, "title": "Game", "nodes": nodes}
        if method == "state":
            return {"readyState": "complete", "stale": "", "href": self.href, "title": "Game"}
        if method == "textPresent":
            return self.over and args[0] == "Game over"
        if method == "observe":
            opts = args[0]
            out: dict[str, Any] = {
                "ok": True,
                "url": self.href,
                "title": "Game",
                "mut": len(self.pressed),
                "frames": self.frames,
                "fp": "|".join(self.row) + "|" + "".join(self.typed),
            }
            if opts.get("table"):
                out["elements"] = self.elements()
            if opts.get("text"):
                out["text"] = " ".join(t or "." for t in self.row) + " City " + "".join(self.typed)
            if opts.get("until"):
                out["until"] = self.over and opts["until"] == "Game over"
            return out
        if method == "check":
            ref, kind = args[0], args[1]
            if ref not in ("e1", "e2") or (kind == "type" and ref != "e2"):
                return {"ok": False, "code": "gone"}
            return {"ok": True, "x": 5.0, "y": 5.0}
        if method == "ticks":
            self.frames += 2
            return {"m": 0, "f": self.frames}
        if method == "selectText":
            return {"ok": True}
        if method == "resolve":
            return {"ok": True, "x": 5.0, "y": 5.0, "tag": "button"}
        if method == "focus":
            return {"ok": True, "focused": True}
        if method == "activeField":
            return {"focused": True, "password": False, "tag": "input"}
        if method == "passwordBoxes":
            return self.password_boxes
        if method == "askedValues":
            return 0
        raise AssertionError(f"unexpected walker method {method}")


def _png(width: int) -> bytes:
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(2))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, 2, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


Judge = Callable[[list, dict], dict[str, Answer]]


def judge(
    pick: str = "PRESS_ARROWLEFT",
    *,
    done_after: int | None = None,
    stuck: float = 0.02,
    off: float = 0.02,
    risk: Callable[[str], float] = lambda _label: 0.02,
    counter: list | None = None,
) -> Judge:
    """A provider answer function: picks *pick* when offered, else the first option."""
    seen = counter if counter is not None else []

    def _answers(questions: list, _state: dict) -> dict[str, Answer]:
        seen.append(1)
        out: dict[str, Answer] = {}
        for q in questions:
            if isinstance(q, Choice):
                value = pick if pick in q.options else q.options[0]
                out[q.id] = Answer(id=q.id, value=value, p=0.9)
            elif isinstance(q, Noul):
                if q.id == "done":
                    v = 0.95 if done_after is not None and len(seen) > done_after else 0.02
                elif q.id == "stuck":
                    v = stuck
                elif q.id == "off_path":
                    v = off
                else:
                    v = risk(q.prompt)
                out[q.id] = Answer(id=q.id, value=v, p=max(v, 1 - v))
        return out

    return _answers


class Provider:
    def __init__(self, answer: Judge, *, raises: BaseException | None = None) -> None:
        self.answer = answer
        self.raises = raises
        self.calls: list[dict] = []

    async def ask(self, state, questions, *, assets=None):
        self.calls.append({"state": state, "questions": questions, "assets": assets})
        if self.raises is not None:
            raise self.raises
        return self.answer(questions, state)


@pytest.fixture
def seam(tmp_path, monkeypatch):
    """The real gate over a consented keystone and a recording provider."""
    keystone = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: keystone)
    keystone.write_text(
        json.dumps({"enabled": True, "endpoint": DECISION_PROVIDER_ENDPOINT_DEFAULT}),
        encoding="utf-8",
    )
    state = SimpleNamespace(
        endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT, denied=False, provider=Provider(judge())
    )

    def _config():
        return SimpleNamespace(
            decisions=DecisionsConfig(
                provider=DecisionProviderConfig(endpoint=state.endpoint, timeout_ms=1000)
            )
        )

    monkeypatch.setattr(gate, "_snapshot", _config)
    monkeypatch.setattr(
        "kiro_crew.decisions.capability.is_decisions_denied", lambda *a, **k: state.denied
    )
    import kiro_crew.decisions.impl_systemone as impl

    monkeypatch.setattr(impl, "SystemOneProvider", lambda _provider: state.provider)

    def _write(**fields):
        current = json.loads(keystone.read_text(encoding="utf-8"))
        keystone.write_text(json.dumps({**current, **fields}), encoding="utf-8")

    state.write = _write
    return state


def _ctx(game: Game, tmp_path, *, hooks: bool = True, **kw: Any) -> tuple[ops.OpContext, list]:
    labels: list[str] = []

    async def _sleep(_s: float) -> None:
        return None

    ctx = ops.OpContext(
        helper=game.helper,
        step=labels.append,
        screenshot_dir=tmp_path,
        sleep=_sleep,
        decisions=decide_mod.BrowserDecisions(SESSION) if hooks else None,
        **kw,
    )
    return ctx, labels


def _run(args: dict, ctx: ops.OpContext) -> dict:
    return asyncio.run(ops.run_op("reflex", args, ctx))


GAME = {"goal": "Merge the tiles", "keys": ["ArrowRight", "ArrowLeft"], "grid": ".cell"}


# -- the loop -----------------------------------------------------------------


def test_the_loop_plays_until_the_model_says_done(seam, tmp_path):
    seam.provider = Provider(judge(done_after=5))
    game = Game()
    ctx, labels = _ctx(game, tmp_path)
    result = _run({**GAME, "max_steps": 50}, ctx)
    assert result["ok"] is True and result["stop"] == "done"
    assert result["moves"] == 5 and game.pressed == ["ArrowLeft"] * 5
    assert result["decisions"] == 6 and len(seam.provider.calls) == 6
    assert result["actions"] == {"Press ArrowLeft": 5}
    assert labels[0].startswith("Reflex 1: Press ArrowLeft")
    assert labels[-1].startswith("Reflex done: 5 moves")
    # The state is the grid the code read, not the outline.
    first = seam.provider.calls[0]
    assert first["state"]["grid"] == [["2", "", "2", ""]] and "page" not in first["state"]
    assert result["state"] == [["12", "", "2", ""]]
    choice = first["questions"][0]
    assert choice.options == ["PRESS_ARROWRIGHT", "PRESS_ARROWLEFT"]


def test_risk_is_asked_once_per_action(seam, tmp_path):
    seam.provider = Provider(judge(done_after=3))
    _run(GAME, _ctx(Game(), tmp_path)[0])
    risk_counts = [
        sum(1 for q in call["questions"] if q.id.startswith("risk_"))
        for call in seam.provider.calls
    ]
    # Arrow keys are judged safe by code; only the click is asked about, once.
    assert risk_counts == [0, 0, 0, 0]
    seam.provider = Provider(judge(done_after=2))
    _run({**GAME, "keys": ["ArrowLeft", "Enter"], "click": ["e2"]}, _ctx(Game(), tmp_path)[0])
    first = seam.provider.calls[0]["questions"]
    assert [q.prompt.split("'")[1] for q in first if q.id.startswith("risk_")] == [
        "Press Enter",
        'Click textbox "City"',
    ]
    assert not any(q.id.startswith("risk_") for q in seam.provider.calls[1]["questions"])


@pytest.mark.parametrize("which,stop", [("stuck", "stuck"), ("off", "off_path")])
def test_the_model_s_stuck_or_off_path_stops_it_before_acting(seam, tmp_path, which, stop):
    seam.provider = Provider(judge(**{which: 0.9}))
    game = Game()
    result = _run(GAME, _ctx(game, tmp_path)[0])
    assert result["stop"] == stop and game.pressed == []


def test_a_no_op_action_is_withdrawn_until_the_state_changes(seam, tmp_path):
    seam.provider = Provider(judge(pick="PRESS_ARROWRIGHT", done_after=4))
    game = Game()
    result = _run(GAME, _ctx(game, tmp_path)[0])
    # Right changes nothing, so it is dropped and Left is the only option left;
    # once Left changes the grid, Right is offered again.
    assert game.pressed[:2] == ["ArrowRight", "ArrowLeft"]
    offered = [call["questions"][0].options for call in seam.provider.calls]
    assert offered[1] == ["PRESS_ARROWLEFT"] and offered[2] == [
        "PRESS_ARROWRIGHT",
        "PRESS_ARROWLEFT",
    ]
    assert result["stop"] == "done"


def test_every_action_a_no_op_stops_with_no_actions_left(seam, tmp_path):
    result = _run({**GAME, "keys": ["ArrowRight"]}, _ctx(Game(), tmp_path)[0])
    assert result["stop"] == "no_actions_left" and result["moves"] == 1


def test_the_step_budget_stops_it(seam, tmp_path):
    game = Game()
    result = _run({**GAME, "max_steps": 3}, _ctx(game, tmp_path)[0])
    assert result["stop"] == "budget" and result["steps"] == 3 and len(game.pressed) == 3


def test_the_time_budget_stops_it(seam, tmp_path, monkeypatch):
    clock = iter(float(i) for i in range(10_000))
    monkeypatch.setattr(reflex.time, "monotonic", lambda: next(clock))
    result = _run({**GAME, "max_seconds": 30, "max_steps": 300}, _ctx(Game(), tmp_path)[0])
    assert result["stop"] == "budget" and 0 < result["steps"] < 30


def test_a_code_terminal_stops_it(seam, tmp_path):
    game = Game()
    game.over = True
    result = _run({**GAME, "until_text": "Game over"}, _ctx(game, tmp_path)[0])
    assert result["stop"] == "until" and game.pressed == [] and seam.provider.calls == []


def test_a_takeover_stops_it_at_the_next_step(seam, tmp_path):
    game = Game()
    checks = {"n": 0}

    def _held() -> bool:
        checks["n"] += 1
        return checks["n"] > 5

    result = _run(GAME, _ctx(game, tmp_path, in_control=_held)[0])
    assert result["stop"] == "user_in_control" and 0 < len(game.pressed) < 3


def test_an_action_judged_hard_to_undo_is_handed_to_the_person(seam, tmp_path):
    seam.provider = Provider(
        judge(pick="CLICK", risk=lambda prompt: 0.9 if "Pay now" in prompt else 0.02)
    )
    game = Game()
    result = _run({"goal": "Buy it", "click": ["e1"]}, _ctx(game, tmp_path)[0])
    assert result["stop"] == "risk" and "take over" in result["handoff"]
    assert game.clicked == []


def test_a_risky_action_is_not_offered_while_others_remain(seam, tmp_path):
    seam.provider = Provider(
        judge(pick="CLICK", done_after=2, risk=lambda p: 0.9 if "Pay now" in p else 0.02)
    )
    game = Game()
    result = _run({**GAME, "click": ["e1"]}, _ctx(game, tmp_path)[0])
    # Step 1 learns e1 is risky and the pick was e1: stop before clicking.
    assert result["stop"] == "risk" and game.clicked == []


def test_a_refused_landing_url_stops_it_and_leaves_the_page(seam, tmp_path):
    game = Game()
    game.href = "http://10.0.0.1/admin"
    ctx, _ = _ctx(game, tmp_path, url_allowed=lambda url: not url.startswith("http://10."))
    result = _run(GAME, ctx)
    assert result["stop"] == "redirect_refused" and game.href == "about:blank"


def test_a_refused_page_is_never_read_or_sent_to_the_provider(seam, tmp_path):
    seam.provider = Provider(judge())
    game = Game()
    game.href = "http://10.0.0.1/admin"
    ctx, _ = _ctx(game, tmp_path, url_allowed=lambda url: not url.startswith("http://10."))
    result = _run(GAME, ctx)
    assert result["stop"] == "redirect_refused"
    assert seam.provider.calls == [], "the URL gate runs before the page text leaves"


def test_an_action_nobody_has_judged_never_runs_before_it_is_judged(seam, tmp_path, monkeypatch):
    # One risk question per request: the first judges e1, while the model picks e2.
    monkeypatch.setattr(reflex, "RISK_PER_REQUEST", 1)
    base = judge(done_after=2)
    game = Game()
    clicks_at_call: list[int] = []

    def _answers(questions: list, state: dict) -> dict[str, Answer]:
        clicks_at_call.append(len(game.clicked))
        out = base(questions, state)
        for q in questions:
            if isinstance(q, Choice) and q.id == "operation":
                out[q.id] = Answer(id=q.id, value="CLICK", p=0.9)
            elif isinstance(q, Choice) and "e2" in q.options:
                out[q.id] = Answer(id=q.id, value="e2", p=0.9)
        return out

    seam.provider = Provider(_answers)
    result = _run({"goal": "Press it", "click": ["e1", "e2"]}, _ctx(game, tmp_path)[0])
    risk = [
        [q.prompt for q in call["questions"] if q.id.startswith("risk_")]
        for call in seam.provider.calls
    ]
    assert len(risk[0]) == 1 and "City" not in risk[0][0]  # e2 is not judged yet
    assert any("City" in p for p in risk[1])  # the next request judges the pick
    # No click before the request that judged it: the first two calls saw none.
    assert clicks_at_call[:2] == [0, 0]
    assert result["ok"] and game.clicked == [(5.0, 5.0)]


def test_type_only_sends_a_value_it_can_point_to(seam, tmp_path):
    game = Game()
    refused = _run({"goal": "Fill the city", "type": ["e2=Portland"]}, _ctx(game, tmp_path)[0])
    assert refused["code"] == "bad_value" and seam.provider.calls == []
    seam.provider = Provider(judge(pick="TYPE_E2", done_after=1))
    ok = _run(
        {"goal": "Fill the city", "type": ["e2=Portland"], "facts": ["Ship to Portland"]},
        _ctx(game, tmp_path)[0],
    )
    assert ok["stop"] == "done" and game.typed == ["Portland"]


def test_see_sends_the_masked_frame_and_compares_it_for_no_ops(seam, tmp_path):
    seam.provider = Provider(judge(done_after=3))
    game = Game()
    result = _run(
        {"goal": "Play the canvas game", "keys": ["ArrowLeft"], "see": True},
        _ctx(game, tmp_path)[0],
    )
    # The outline never changes on a canvas; the frame does, so nothing is withdrawn.
    assert result["stop"] == "done" and result["moves"] == 3 and result["see"] is True
    for call in seam.provider.calls:
        screen = call["state"]["screen"]
        assert set(call["assets"]) == {screen}
        assert "`screen`" in call["questions"][0].prompt


def test_see_sends_no_frame_while_a_password_box_is_on_the_page(seam, tmp_path):
    seam.provider = Provider(judge(done_after=1))
    game = Game()
    game.password_boxes = 1
    _run({"goal": "Play", "keys": ["ArrowLeft"], "see": True}, _ctx(game, tmp_path)[0])
    assert seam.provider.calls and all(c["assets"] is None for c in seam.provider.calls)
    assert all("screen" not in c["state"] for c in seam.provider.calls)


def test_see_without_the_image_scope_runs_on_text(seam, tmp_path):
    seam.write(browser_images=False)
    seam.provider = Provider(judge(done_after=1))
    result = _run({"goal": "Play", "keys": ["ArrowLeft"], "see": True}, _ctx(Game(), tmp_path)[0])
    assert result["see"] is False and seam.provider.calls[0]["assets"] is None


# -- off is free --------------------------------------------------------------


def test_no_hooks_is_not_available_and_touches_nothing(seam, tmp_path):
    game = Game()
    result = _run(GAME, _ctx(game, tmp_path, hooks=False)[0])
    assert result["ok"] is False and result["code"] == "not_available"
    assert game.pressed == [] and seam.provider.calls == []


@pytest.mark.parametrize("how", ["no_model", "no_consent", "governance_denied", "switch_off"])
def test_a_gated_off_seam_is_not_available_with_no_provider_call(seam, tmp_path, how):
    if how == "no_model":
        seam.endpoint = "none"
    elif how == "no_consent":
        seam.write(enabled=False)
    elif how == "governance_denied":
        seam.denied = True
    else:
        seam.write(browser=False)
    game = Game()
    ctx, labels = _ctx(game, tmp_path)
    result = _run(GAME, ctx)
    assert result["code"] == "not_available"
    assert game.pressed == [] and labels == [] and seam.provider.calls == []


def test_a_first_decision_that_fails_is_not_available_with_the_page_untouched(seam, tmp_path):
    seam.provider = Provider(judge(), raises=RuntimeError("provider down"))
    game = Game()
    result = _run(GAME, _ctx(game, tmp_path)[0])
    assert result["code"] == "not_available" and game.pressed == []


def test_a_later_decision_that_fails_ends_with_a_summary(seam, tmp_path):
    calls = {"n": 0}

    def _answers(qs, st):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("down")
        return judge()(qs, st)

    class _Flaky(Provider):
        async def ask(self, state, questions, *, assets=None):
            self.calls.append({"state": state})
            return _answers(questions, state)

    seam.provider = _Flaky(judge())
    result = _run(GAME, _ctx(Game(), tmp_path)[0])
    assert result["ok"] is True and result["stop"] == "decision_unavailable"
    assert result["moves"] == 2


# -- shapes -------------------------------------------------------------------


@pytest.mark.parametrize(
    "args,code",
    [
        ({"keys": ["ArrowLeft"]}, "bad_goal"),
        ({"goal": "g"}, "no_actions"),
        ({"goal": "g", "keys": ["F13"]}, "bad_key"),
        ({"goal": "g", "click": ["button"]}, "bad_ref"),
        ({"goal": "g", "drag": ["e1-e2"]}, "bad_ref"),
        ({"goal": "g", "type": ["e1"]}, "bad_value"),
        ({"goal": "g", "keys": ["a"] * 41}, "too_many_actions"),
    ],
)
def test_a_malformed_call_is_refused(args, code):
    refused = reflex.parse(args)
    assert isinstance(refused, dict) and refused["code"] == code


def test_arrange_fills_slots_from_items_and_keeps_the_bigger_of_a_merge():
    slots = [{"t": "", "x": x, "y": y} for y in (10, 30) for x in (10, 30)]
    items = [
        {"t": "2", "x": 11, "y": 9},
        {"t": "4", "x": 29, "y": 31},
        {"t": "8", "x": 31, "y": 29},
    ]
    assert reflex.arrange(slots, items) == [["2", ""], ["", "8"]]


def test_the_loop_ends_inside_the_route_s_budget():
    assert "reflex" in ops.SLOW_OPS and "reflex" in ops.OPS
    assert reflex.MAX_SECONDS + 5 < budgets.SLOW_OP_TIMEOUT_S


# -- what the live card and the replay are told --------------------------------


def _reporting_ctx(game: Game, tmp_path, **kw: Any):
    """A context whose loop counters and inner replay records are kept."""
    loops: list[dict] = []
    records: list[tuple] = []
    marks = iter(range(1, 10_000))
    ctx, labels = _ctx(
        game,
        tmp_path,
        loop=loops.append,
        mark=lambda: next(marks),
        record=lambda *a: records.append(a),
        **kw,
    )
    return ctx, labels, loops, records


def test_the_loop_reports_its_counters_and_ends_with_its_stop(seam, tmp_path):
    seam.provider = Provider(judge(done_after=3))
    ctx, _labels, loops, _records = _reporting_ctx(Game(), tmp_path)
    _run({**GAME, "max_steps": 40, "max_seconds": 30}, ctx)
    # One report as it starts, one per action, one with the stop.
    assert len(loops) == 1 + 3 + 1
    assert set(loops[0]) == {
        "moves",
        "rate",
        "steps",
        "max_steps",
        "seconds",
        "max_seconds",
        "stop",
    }
    assert [r["steps"] for r in loops] == [0, 1, 2, 3, 3]
    assert [r["moves"] for r in loops] == [0, 1, 2, 3, 3]
    assert all(r["max_steps"] == 40 and r["max_seconds"] == 30 for r in loops)
    assert [r["stop"] for r in loops] == [None, None, None, None, "done"]


@pytest.mark.parametrize(
    "args,setup,stop",
    [
        ({"max_steps": 2}, None, "budget"),
        ({"until_text": "Game over"}, "over", "until"),
    ],
)
def test_each_stop_reaches_the_loop_line(seam, tmp_path, args, setup, stop):
    game = Game()
    if setup == "over":
        game.over = True
    ctx, _labels, loops, _records = _reporting_ctx(game, tmp_path)
    _run({**GAME, **args}, ctx)
    assert loops[-1]["stop"] == stop


def test_a_takeover_reaches_the_loop_line_as_user_in_control(seam, tmp_path):
    checks = {"n": 0}

    def _held() -> bool:
        checks["n"] += 1
        return checks["n"] > 5

    ctx, _labels, loops, _records = _reporting_ctx(Game(), tmp_path, in_control=_held)
    _run(GAME, ctx)
    assert loops[-1]["stop"] == "user_in_control"


def test_a_first_decision_that_fails_says_so_on_the_loop_line(seam, tmp_path):
    seam.provider = Provider(judge(), raises=RuntimeError("provider down"))
    ctx, _labels, loops, records = _reporting_ctx(Game(), tmp_path)
    assert _run(GAME, ctx)["code"] == "not_available"
    assert loops[-1]["stop"] == "decision_unavailable" and records == []


def test_every_action_is_its_own_replay_record(seam, tmp_path):
    seam.provider = Provider(judge(done_after=2))
    ctx, _labels, _loops, records = _reporting_ctx(Game(), tmp_path)
    _run(GAME, ctx)
    assert [(op, args, mark) for op, args, _result, mark in records] == [
        ("press_key", {"key": "ArrowLeft"}, 1),
        ("press_key", {"key": "ArrowLeft"}, 2),
    ]
    assert all(result["ok"] for _op, _args, result, _mark in records)
