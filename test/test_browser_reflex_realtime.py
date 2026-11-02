"""``reflex`` timed modes and the operation/target heads.

What is pinned:

* ``realtime`` asks on a clock without waiting, never past the provider's declared
  width; an answer asked before an action ran (or, with ``supersede: "change"``,
  before the page changed) is discarded and counted; a WAIT supersedes nothing;
  ``lead`` puts ``lead_ms`` in the state and holds an early answer;
* ``virtual_time`` asks only while the page is held, lets it run before input, and
  always lets it go -- on a stop, a failure and a cancelled task; a browser that
  refuses the hold runs ``step``;
* the loop reads the provider's capabilities, not its name: one request at a time
  turns ``realtime`` into ``virtual_time``, no image support means state only --
  proved through the REAL gate with two registry entries of different shape;
* one request carries the operation head and a target head per targeted
  operation; only the picked operation's head is used, and a target is re-checked
  just before input.

The page is a fake ``helper``: a value that climbs with wall time until ``s`` is
pressed, plus a small element table.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.browser_harness import decide as decide_mod
from kiro_crew.browser_harness import helper_worker, ops, reflex, reflex_space
from kiro_crew.config.sections import (
    DECISION_PROVIDER_ENDPOINT_DEFAULT,
    DecisionProviderConfig,
    DecisionsConfig,
)
from kiro_crew.decisions import gate
from kiro_crew.decisions import provider as provider_mod
from kiro_crew.decisions.provider import Capabilities
from kiro_crew.decisions.types import Answer, Choice, Noul
from kiro_crew.testing.wait import async_wait_until

SESSION = "dashboard:chat-1"
FAST = Capabilities(max_in_flight=8, latency_ms=50)
NARROW = Capabilities(max_in_flight=1, latency_ms=300)


class Page:
    """A value climbing 100/s until ``s`` is pressed; a Stop button and a select."""

    def __init__(
        self, *, refuse_hold: bool = False, churn: bool = False, drift: bool = False
    ) -> None:
        self.t0 = time.monotonic()
        self.stopped_at: float | None = None
        self.pressed: list[str] = []
        self.clicked: list[tuple[float, float]] = []
        self.selected: list[Any] = []
        self.clock: list[str] = []
        self.held = False
        self.refuse_hold = refuse_hold
        self.churn = churn
        self.drift = drift
        self.observes = 0
        self.stale_refs: set[str] = set()

    def value(self) -> int:
        end = self.stopped_at if self.stopped_at is not None else time.monotonic()
        return int((end - self.t0) * 100)

    async def helper(self, name: str, **kw: Any) -> Any:
        if name == "js":
            expr = kw["expression"]
            assert expr.startswith(ops.WALKER_SOURCE)
            method, _, rest = expr[len(ops.WALKER_SOURCE) + 1 :].partition("(")
            return self._walker(method, json.loads(f"[{rest[:-1]}]"))
        if name == "press_key":
            self.pressed.append(kw["key"])
            if kw["key"] == "s" and self.stopped_at is None:
                self.stopped_at = time.monotonic()
            return None
        if name == "click":
            self.clicked.append((kw["x"], kw["y"]))
            return None
        if name == "page_clock":
            if self.refuse_hold:
                raise RuntimeError("Not allowed")
            self.clock.append(kw["state"])
            self.held = kw["state"] == "hold"
            return {"state": kw["state"]}
        if name == "screenshot":
            from pathlib import Path

            Path(kw["path"]).write_bytes(b"\x89PNG\r\n\x1a\nframe")
            return None
        return None

    def _walker(self, method: str, args: list) -> Any:
        if method == "observe":
            self.observes += 1
            opts = args[0]
            out: dict[str, Any] = {
                "ok": True,
                "url": "https://page.example/",
                "title": "Page",
                "mut": self.observes if self.churn else len(self.pressed) + len(self.clicked),
                "frames": self.observes,
                "fp": str(self.value()),
            }
            if opts.get("read"):
                out["read"] = {"v": self.value()}
            if opts.get("table"):
                out["elements"] = [
                    {"ref": "e1", "role": "button", "name": "Stop", "ops": ["click"], "g": "g1"},
                    {
                        "ref": "e2",
                        "role": "combobox",
                        "name": "Size",
                        "value": "S",
                        "ops": ["select"],
                        "options": [{"i": 1, "label": "M", "value": "m"}],
                        "g": "g2",
                    },
                ]
            return out
        if method == "check":
            if args[0] in self.stale_refs:
                return {"ok": False, "code": "stale"}
            if args[1] == "select":
                return {"ok": True, "value": "m"}
            return {"ok": True, "x": 10.0, "y": 20.0}
        if method == "selectOption":
            self.selected.append(args)
            return {"ok": True, "selected": args[1]}
        if method == "ticks":
            # ``drift``: the page mutates on its own, with or without a read.
            m = int(time.monotonic() * 1000) if self.drift else self.observes
            return {"m": m, "f": self.observes + 10}
        if method == "state":
            return {"href": "https://page.example/"}
        if method == "passwordBoxes":
            return 0
        if method == "askedValues":
            return 0
        if method == "activeField":
            return {"focused": False}
        raise AssertionError(f"unexpected walker method {method}")


class Model:
    """A fake decision provider at the gate seam: latency, width, and a rule."""

    def __init__(
        self,
        rule,
        *,
        latency: float = 0.0,
        page: Page | None = None,
        fail: bool = False,
        together: int = 0,
    ):
        self.rule = rule
        self.latency = latency
        #: The first answers are held until this many questions are in flight, so a
        #: test of pipelining does not hang on a loop that ran late.
        self.together = together
        self.page = page
        self.fail = fail
        self.calls: list[dict[str, Any]] = []
        self.now = 0
        self.peak = 0

    async def decide(self, point, state, questions, **kw):
        self.now += 1
        self.peak = max(self.peak, self.now)
        self.calls.append(
            {
                "state": state,
                "questions": questions,
                "held": self.page.held if self.page else None,
                "images": kw.get("images"),
            }
        )
        try:
            await async_wait_until(
                lambda: self.now >= self.together or len(self.calls) >= self.together
            )
            await asyncio.sleep(self.latency)
            if self.fail:
                return None
            want = self.rule(state, questions, len(self.calls))
            out = {}
            for q in questions:
                if isinstance(q, Choice):
                    value = want.get(q.id, q.options[0])
                    out[q.id] = Answer(
                        id=q.id, value=value if value in q.options else q.options[0], p=0.9
                    )
                elif isinstance(q, Noul):
                    v = float(want.get(q.id, 0.02))
                    out[q.id] = Answer(id=q.id, value=v, p=max(v, 1 - v))
            return out
        finally:
            self.now -= 1


@pytest.fixture
def seam(monkeypatch):
    state = SimpleNamespace(caps=FAST, image=False)
    monkeypatch.setattr(
        decide_mod, "_probe", lambda _sk: decide_mod.Armed(text=True, image=state.image)
    )
    monkeypatch.setattr(decide_mod, "capabilities", lambda: state.caps)

    async def _warm(_sk):
        return False

    monkeypatch.setattr(decide_mod, "warm", _warm)

    def _use(model: Model) -> Model:
        monkeypatch.setattr(gate, "decide", model.decide)
        return model

    state.use = _use
    return state


def _ctx(page: Page, labels: list | None = None) -> ops.OpContext:
    return ops.OpContext(
        helper=page.helper,
        step=(labels if labels is not None else []).append,
        sleep=asyncio.sleep,
        decisions=SimpleNamespace(session_key=SESSION),
    )


def _run(args: dict, ctx: ops.OpContext) -> dict:
    return asyncio.run(asyncio.wait_for(reflex.run(ctx, args), 30))


READ = [{"name": "v", "sel": "#v", "prop": "text"}]
STOP_AT = {"goal": "Press s when the value reaches 50", "keys": ["s"], "read": READ}


def stop_rule(state, _questions, _n):
    v = (state.get("read") or {}).get("v", 0)
    return {"operation": "PRESS_S" if v >= 20 else "WAIT"}


# -- realtime -------------------------------------------------------------------------


def test_realtime_pipelines_within_the_declared_width(seam):
    page = Page()
    model = seam.use(Model(stop_rule, latency=0.12, page=page, together=2))
    result = _run({**STOP_AT, "mode": "realtime", "max_in_flight": 3, "max_steps": 1}, _ctx(page))
    assert result["mode"] == "realtime" and result["stop"] == "budget"
    assert 2 <= model.peak <= 3 and result["in_flight_cap"] == 3
    assert page.pressed == ["s"] and result["applied"] == 1


def test_the_width_never_exceeds_what_the_provider_declares(seam):
    seam.caps = Capabilities(max_in_flight=2, latency_ms=50)
    page = Page()
    model = seam.use(Model(lambda *_: {"operation": "WAIT"}, latency=0.1, page=page))
    result = _run(
        {**STOP_AT, "mode": "realtime", "max_in_flight": 9, "max_decisions": 8}, _ctx(page)
    )
    assert model.peak <= 2 and result["in_flight_cap"] == 2


def test_an_answer_asked_before_an_action_is_superseded_and_never_on_the_card(seam):
    page = Page()
    labels: list[str] = []
    seam.use(Model(lambda *_: {"operation": "PRESS_S"}, latency=0.15, page=page, together=2))
    result = _run({**STOP_AT, "mode": "realtime", "max_steps": 2}, _ctx(page, labels))
    assert result["applied"] == 2 and result["superseded"] >= 1
    # One card step per APPLIED action plus the summary line; none for discards.
    assert len([x for x in labels if x.startswith("Reflex ") and ": Press" in x]) == 2


def test_waits_supersede_nothing(seam):
    page = Page()
    seam.use(Model(lambda *_: {"operation": "WAIT"}, latency=0.05, page=page))
    result = _run({**STOP_AT, "mode": "realtime", "max_decisions": 10}, _ctx(page))
    # Questions still in flight at the budget stop are cancelled, never counted.
    assert result["superseded"] == 0 and result["applied"] == 0
    assert result["waits"] >= 10 - result["in_flight_cap"]
    assert page.pressed == []


def test_supersede_on_change_discards_answers_about_a_page_that_moved(seam):
    page = Page(churn=True)
    seam.use(Model(lambda *_: {"operation": "PRESS_S"}, latency=0.1, page=page))
    result = _run(
        {**STOP_AT, "mode": "realtime", "supersede": "change", "max_decisions": 6}, _ctx(page)
    )
    # The fake page changes when it is read; once the decision limit stops the
    # reads, the last answer still describes it and may be used.
    assert result["applied"] <= 1 and result["superseded"] >= 3


def test_supersede_on_change_rereads_the_counter_when_the_pipeline_is_full(seam):
    page = Page(drift=True)
    seam.use(Model(lambda *_: {"operation": "PRESS_S"}, latency=0.05, page=page))
    result = _run(
        {
            **STOP_AT,
            "mode": "realtime",
            "supersede": "change",
            "max_in_flight": 1,
            "max_decisions": 4,
        },
        _ctx(page),
    )
    assert result["applied"] == 0 and result["superseded"] >= 3 and page.pressed == []


def test_two_risk_answers_for_one_move_keep_the_more_cautious(seam, monkeypatch):
    answers = iter([0.9, 0.1])

    async def _step(*_a, **_k):
        return {
            "operation": "WAIT",
            "picks": {},
            "value": None,
            "done": 0.0,
            "stuck": 0.0,
            "off_path": 0.0,
            "until": 0.0,
            "risk": [next(answers)],
        }

    monkeypatch.setattr(decide_mod, "reflex_step", _step)
    plan = reflex.parse({**STOP_AT, "mode": "realtime"})
    loop = reflex.Loop(_ctx(Page()), plan, SESSION, False, FAST)
    move = plan.moves[0]
    seen = reflex.Seen({}, "", None, None, 0, "", False)
    space = reflex_space.Space({}, {}, {}, [])
    asyncio.run(loop.request(seen, space, [move]))
    asyncio.run(loop.request(seen, space, [move]))
    assert move.risky == 0.9


def test_lead_tells_the_model_how_old_the_state_will_be_and_holds_early_answers(seam):
    page = Page()
    model = seam.use(Model(lambda *_: {"operation": "WAIT"}, latency=0.05, page=page))
    result = _run({**STOP_AT, "mode": "realtime", "lead": True, "max_decisions": 12}, _ctx(page))
    leads = [c["state"]["lead_ms"] for c in model.calls]
    assert leads[0] == FAST.latency_ms and leads[-1] >= 40
    assert result["lead_ms"] >= 40 and result["held_ms_avg"] >= 40


def test_realtime_with_no_answers_at_all_is_not_available(seam):
    page = Page()
    seam.use(Model(stop_rule, latency=0.01, page=page, fail=True))
    result = _run({**STOP_AT, "mode": "realtime"}, _ctx(page))
    assert result["code"] == "not_available" and page.pressed == []


def test_the_decision_budget_bounds_a_realtime_run(seam):
    page = Page()
    model = seam.use(Model(lambda *_: {"operation": "WAIT"}, latency=0.02, page=page))
    result = _run({**STOP_AT, "mode": "realtime", "max_decisions": 5}, _ctx(page))
    assert result["stop"] == "budget" and len(model.calls) == 5 == result["decisions"]


# -- virtual_time -----------------------------------------------------------------------


def test_virtual_time_decides_only_while_the_page_is_held_and_lets_it_go(seam):
    page = Page()
    model = seam.use(Model(stop_rule, latency=0.05, page=page))
    result = _run({**STOP_AT, "mode": "virtual_time", "max_steps": 1}, _ctx(page))
    assert result["mode"] == "virtual_time" and page.pressed == ["s"]
    assert model.calls and all(c["held"] is True for c in model.calls)
    assert page.clock[-1] == "release" and page.clock[0] == "hold"
    # Run before input: the key press came after a "run".
    assert page.clock[: page.clock.index("run") + 1][-1] == "run"


def test_virtual_time_lets_the_page_go_when_the_op_is_cancelled(seam):
    page = Page()
    model = seam.use(Model(stop_rule, latency=5.0, page=page))

    async def _cancel():
        task = asyncio.ensure_future(reflex.run(_ctx(page), {**STOP_AT, "mode": "virtual_time"}))
        # The decision is being made on the held page.
        await async_wait_until(lambda: model.calls)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_cancel())
    assert page.clock[0] == "hold" and page.clock[-1] == "release"


def test_a_browser_that_refuses_the_hold_runs_in_step_mode(seam):
    page = Page(refuse_hold=True)
    seam.use(Model(stop_rule, latency=0.01, page=page))
    result = _run({**STOP_AT, "mode": "virtual_time", "max_steps": 1}, _ctx(page))
    assert result["mode"] == "step" and page.pressed == ["s"]
    assert any("refused to hold" in n for n in result["notes"])


def test_a_one_at_a_time_provider_runs_realtime_as_virtual_time(seam):
    seam.caps = NARROW
    page = Page()
    seam.use(Model(stop_rule, latency=0.01, page=page))
    result = _run({**STOP_AT, "mode": "realtime", "max_steps": 1}, _ctx(page))
    assert result["mode"] == "virtual_time" and "hold" in page.clock
    assert any("one request at a time" in n for n in result["notes"])


# -- operation + target heads ----------------------------------------------------------


def test_one_request_carries_an_operation_head_and_a_head_per_targeted_operation(seam):
    page = Page()
    model = seam.use(Model(lambda *_: {"operation": "SELECT", "click_target": "e1"}, page=page))
    result = _run(
        {"goal": "Pick size M", "targets": ["click", "select"], "max_steps": 1}, _ctx(page)
    )
    questions = {q.id: q for q in model.calls[0]["questions"]}
    assert questions["operation"].options == ["CLICK", "SELECT"]
    assert questions["click_target"].options == ["e1"]
    assert questions["select_target"].options == ["e2:1"]
    # SELECT was picked, so the click head's e1 is unused: nothing was clicked.
    assert page.clicked == [] and page.selected == [["e2", ["m"]]]
    assert result["moves"] == 1


def test_a_target_that_changed_before_input_is_not_used(seam):
    page = Page()
    page.stale_refs = {"e1"}
    seam.use(Model(lambda *_: {"operation": "CLICK"}, page=page))
    result = _run({"goal": "Stop it", "targets": ["click"], "max_decisions": 3}, _ctx(page))
    assert page.clicked == [] and result["moves"] == 0


def test_space_offers_only_compatible_targets_and_resolves_only_the_picked_head():
    rows = [
        {"ref": "e1", "role": "button", "name": "Go", "ops": ["click"], "g": "a"},
        {"ref": "e2", "role": "textbox", "name": "City", "ops": ["click", "type"], "g": "b"},
    ]
    space = reflex_space.build([], ("click", "type"), None, rows, ["Paris"], {}, wait=True)
    assert set(space.targets["CLICK"]) == {"e1", "e2"} and set(space.targets["TYPE_TEXT"]) == {"e2"}
    assert space.resolve("TYPE_TEXT", {"CLICK": "e1", "TYPE_TEXT": "e2"}).key == "type e2"
    assert space.resolve("TYPE_TEXT", {"TYPE_TEXT": "e1"}) is None
    assert space.resolve("WAIT", {}).kind == "wait"


# -- the provider contract, through the real gate ----------------------------------------


class _RegistryProvider:
    def __init__(self, latency: float, log: list) -> None:
        self.latency = latency
        self.log = log

    async def ask(self, state, questions, *, assets=None):
        self.log.append({"assets": assets, "ids": [q.id for q in questions]})
        await asyncio.sleep(self.latency)
        out = {}
        for q in questions:
            if isinstance(q, Choice):
                pick = (
                    "PRESS_S"
                    if q.id == "operation" and (state.get("read") or {}).get("v", 0) >= 20
                    else None
                )
                value = (
                    pick if pick in q.options else ("WAIT" if "WAIT" in q.options else q.options[0])
                )
                out[q.id] = Answer(id=q.id, value=value, p=0.9)
            else:
                out[q.id] = Answer(id=q.id, value=0.02, p=0.98)
        return out


@pytest.fixture
def real_gate(tmp_path, monkeypatch):
    keystone = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: keystone)
    keystone.write_text(
        json.dumps({"enabled": True, "endpoint": DECISION_PROVIDER_ENDPOINT_DEFAULT}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        gate,
        "_snapshot",
        lambda: SimpleNamespace(
            decisions=DecisionsConfig(
                provider=DecisionProviderConfig(
                    endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT, timeout_ms=2000
                )
            )
        ),
    )
    monkeypatch.setattr("kiro_crew.decisions.capability.is_decisions_denied", lambda *a, **k: False)

    def _register(spec):
        monkeypatch.setattr(provider_mod, "select", lambda *a, **k: spec)

    return _register


def _spec(name, latency, width, images, log):
    supports = provider_mod.QUESTION_KINDS | (
        {provider_mod.KIND_BOX, provider_mod.CAPABILITY_IMAGE} if images else set()
    )
    return provider_mod.ProviderSpec(
        id=name,
        kind=provider_mod.KIND_HOSTED,
        lane=provider_mod.LANE_ENDPOINT,
        egress=provider_mod.EGRESS_THIRD_PARTY,
        authority=provider_mod.AUTHORITY_KEYSTONE,
        governance_row=provider_mod.ROW_DECISIONS,
        supports=frozenset(supports),
        timeout_window=None,
        factory=lambda _cfg, _model: _RegistryProvider(latency, log),
        max_in_flight=width,
        latency_ms=int(latency * 1000),
    )


@pytest.mark.parametrize(
    "name,latency,width,images,mode,sent_images",
    [
        ("fake-fast-wide-images", 0.05, 8, True, "realtime", True),
        ("fake-slow-single-text", 0.3, 1, False, "virtual_time", False),
    ],
)
def test_one_loop_drives_providers_of_different_shape_through_the_registry(
    real_gate, monkeypatch, name, latency, width, images, mode, sent_images
):
    log: list = []
    real_gate(_spec(name, latency, width, images, log))
    # The image grant is the keystone's own scope; held true here so only the
    # provider's declared support decides whether a frame goes.
    monkeypatch.setattr(gate, "request_scope_granted", lambda _scope: True)
    page = Page()
    args = {**STOP_AT, "mode": "realtime", "see": True, "max_steps": 1}
    result = _run(args, _ctx(page))
    assert result["ok"] is True and result["mode"] == mode and page.pressed == ["s"]
    assert any(entry["assets"] for entry in log) is sent_images
    if not sent_images:
        assert any("state only" in n for n in result["notes"])


def test_capabilities_are_read_off_the_selected_spec():
    caps = gate.capabilities(
        SimpleNamespace(decisions=DecisionsConfig(provider=DecisionProviderConfig()))
    )
    hosted = provider_mod.get(provider_mod.PROVIDER_HOSTED)
    assert caps == hosted.capabilities() and caps.max_in_flight > 1
    llm = provider_mod.get(provider_mod.PROVIDER_SESSION_LLM).capabilities()
    assert llm.max_in_flight == 1


def test_warm_opens_nothing_without_consent(monkeypatch):
    monkeypatch.setattr(gate, "_consented_for", lambda *a, **k: False)
    assert asyncio.run(gate.warm(decide_mod.POINT, session_key=SESSION)) is False


@pytest.mark.parametrize("denied", [True, False])
def test_warm_sends_no_head_to_an_endpoint_the_fleet_lists_deny(monkeypatch, denied):
    opened: list[str] = []

    async def opener() -> None:
        opened.append("head")

    cfg = SimpleNamespace(decisions=DecisionsConfig(provider=DecisionProviderConfig()))
    monkeypatch.setattr(gate, "_snapshot", lambda: cfg)
    monkeypatch.setattr(gate, "_consented_for", lambda *a, **k: True)
    monkeypatch.setattr(gate, "_sampled", lambda *a, **k: True)
    monkeypatch.setattr(gate, "_targets_denied", lambda *a, **k: denied)
    monkeypatch.setattr(gate, "_oracle", lambda *a, **k: SimpleNamespace(warm=opener))
    assert asyncio.run(gate.warm(decide_mod.POINT, session_key=SESSION)) is (not denied)
    assert opened == ([] if denied else ["head"])


# -- the page clock helper -------------------------------------------------------------


def test_page_clock_breaks_on_page_callbacks_and_lets_them_go():
    sent: list[tuple] = []

    class _Helpers:
        def cdp(self, method, **params):
            sent.append((method, params.get("eventName")))
            if method == "Debugger.resume":
                raise RuntimeError("not paused")
            return {}

    h = _Helpers()
    helper_worker._local_page_clock(h, "hold")
    assert sent[0] == ("Debugger.enable", None)
    assert {name for m, name in sent if m == "EventBreakpoints.setInstrumentationBreakpoint"} == {
        "requestAnimationFrame.callback",
        "setTimeout.callback",
        "setInterval.callback",
    }
    sent.clear()
    helper_worker._local_page_clock(h, "release")
    assert [m for m, _ in sent][-2:] == ["Debugger.resume", "Debugger.disable"]
    with pytest.raises(ValueError):
        helper_worker._local_page_clock(h, "pause")


# -- shapes ------------------------------------------------------------------------------


def test_reads_take_the_tool_s_flat_form_and_never_script():
    plan = reflex.parse(
        {"goal": "g", "keys": ["s"], "read": ["y=#c@attr:data-bird-y", "#out", "n=li@count"]}
    )
    assert plan.read == [
        {"name": "y", "sel": "#c", "prop": "attr:data-bird-y"},
        {"name": "r1", "sel": "#out", "prop": "text"},
        {"name": "n", "sel": "li", "prop": "count"},
    ]
    for bad in (["#c@eval"], ["#c@attr:on click"], [{"sel": "#c", "prop": "innerHTML"}]):
        refused = reflex.parse({"goal": "g", "keys": ["s"], "read": bad})
        assert isinstance(refused, dict) and refused["code"] == "bad_read"


@pytest.mark.parametrize(
    "args,code",
    [
        ({"goal": "g", "keys": ["s"], "mode": "turbo"}, "bad_mode"),
        ({"goal": "g", "targets": ["type"]}, "bad_value"),
        ({"goal": "g", "targets": ["paste"]}, "bad_targets"),
        ({"goal": "g", "keys": ["s"], "supersede": "always"}, "bad_supersede"),
    ],
)
def test_a_malformed_timed_call_is_refused(args, code):
    refused = reflex.parse(args)
    assert isinstance(refused, dict) and refused["code"] == code


def test_timed_modes_offer_wait_and_step_mode_only_when_asked():
    assert reflex.parse({"goal": "g", "keys": ["s"], "mode": "realtime"}).wait is True
    assert reflex.parse({"goal": "g", "keys": ["s"]}).wait is False
    assert reflex.parse({"goal": "g", "keys": ["s"], "wait": True}).wait is True
    assert reflex.parse({"goal": "g", "keys": ["s"], "tick_ms": 5}).tick_s == 0.05


# -- the live card and the URL gate, in the timed modes ------------------------------


@pytest.mark.parametrize("mode", ["realtime", "virtual_time"])
def test_a_refused_page_is_never_read_or_sent_in_a_timed_mode(seam, mode):
    page = Page()
    model = seam.use(Model(stop_rule, page=page))
    ctx = dataclasses.replace(_ctx(page), url_allowed=lambda url: "page.example" not in url)
    result = _run({**STOP_AT, "mode": mode}, ctx)
    assert result["stop"] == "redirect_refused"
    assert model.calls == [] and page.observes == 0, "the URL gate runs before the read"


@pytest.mark.parametrize("mode", ["realtime", "virtual_time"])
def test_a_timed_run_feeds_the_loop_line_and_records_each_action(seam, mode):
    page = Page()
    seam.use(Model(stop_rule, page=page))
    loops: list[dict] = []
    records: list[tuple] = []
    ctx = dataclasses.replace(
        _ctx(page), loop=loops.append, mark=lambda: "m", record=lambda *a: records.append(a)
    )
    result = _run({**STOP_AT, "mode": mode, "max_steps": 1}, ctx)
    assert result["stop"] == "budget" and page.pressed == ["s"]
    assert loops[0]["stop"] is None and loops[0]["moves"] == 0
    assert loops[-1]["stop"] == "budget" and loops[-1]["moves"] == 1
    assert [(op, args, mark) for op, args, _r, mark in records] == [
        ("press_key", {"key": "s"}, "m")
    ]


def test_a_timed_run_with_no_answer_ends_the_loop_line(seam):
    page = Page()
    seam.use(Model(stop_rule, page=page, fail=True))
    loops: list[dict] = []
    ctx = dataclasses.replace(_ctx(page), loop=loops.append)
    assert _run({**STOP_AT, "mode": "realtime"}, ctx)["code"] == "not_available"
    assert loops[-1]["stop"] == "decision_unavailable"


def test_a_targeted_click_records_where_it_landed(seam):
    page = Page()
    seam.use(Model(lambda *_: {"operation": "CLICK", "click_target": "e1"}, page=page))
    records: list[tuple] = []
    ctx = dataclasses.replace(_ctx(page), record=lambda *a: records.append(a))
    _run({"goal": "Press Stop", "click": ["e1"], "max_steps": 1}, ctx)
    assert page.clicked == [(10.0, 20.0)]
    op, args, result, _mark = records[0]
    assert op == "click" and args == {"ref": "e1"} and (result["x"], result["y"]) == (10, 20)


def test_an_element_that_reads_differently_is_judged_again():
    known: dict[str, reflex_space.Move] = {}
    row = {"ref": "e1", "role": "button", "name": "Next", "ops": ["click"], "g": "g1"}
    first = reflex_space.build([], ("click",), None, [row], [], known, wait=False)
    first.targets["CLICK"]["e1"].risky = 0.02  # judged safe as "Next"
    again = reflex_space.build([], ("click",), None, [row], [], known, wait=False)
    assert again.targets["CLICK"]["e1"].risky == 0.02, "the same words keep their verdict"
    renamed = {**row, "name": "Place order"}
    later = reflex_space.build([], ("click",), None, [renamed], [], known, wait=False)
    move = later.targets["CLICK"]["e1"]
    assert move.risky is None and "Place order" in move.label


def test_each_request_keeps_the_guard_it_saw():
    known: dict[str, reflex_space.Move] = {}
    row = {"ref": "e1", "role": "button", "name": "Preview", "ops": ["click"], "g": "g1"}
    asked = reflex_space.build([], ("click",), None, [row], [], known, wait=False)
    later = reflex_space.build([], ("click",), None, [{**row, "g": "g2"}], [], known, wait=False)
    # One shared Move, refreshed by the later read; the earlier request's check uses g1.
    assert asked.targets["CLICK"]["e1"] is later.targets["CLICK"]["e1"]
    assert asked.guards["click e1"] == "g1" and later.guards["click e1"] == "g2"


def test_an_answer_that_lands_past_the_deadline_is_not_acted_on(seam):
    page = Page()
    seam.use(Model(lambda *_: {"operation": "PRESS_S"}, latency=1.3, page=page))
    result = _run({**STOP_AT, "mode": "realtime", "max_in_flight": 1, "max_seconds": 1}, _ctx(page))
    assert result["stop"] == "budget" and page.pressed == []


def test_a_risk_answer_about_old_words_does_not_judge_the_new_ones(seam):
    # Realtime: request A asks about "Next"; before it lands the same node reads
    # "Place order". A's "safe" must not become the verdict for "Place order".
    seam.use(Model(lambda *_: {"operation": "CLICK", "click_target": "e1"}))
    plan = reflex.parse({"goal": "Go on", "click": ["e1"], "mode": "realtime"})
    loop = reflex.Loop(_ctx(Page()), plan, SESSION, False, FAST)
    row = {"ref": "e1", "role": "button", "name": "Next", "ops": ["click"], "g": "g1"}
    seen = reflex.Seen({"goal": "Go on"}, "", None, [row], 0, "", False)
    space = loop.space(seen, wait=False)
    move = space.targets["CLICK"]["e1"]

    async def _race():
        pending = asyncio.ensure_future(loop.request(seen, space, loop.risk_batch(space)))
        await asyncio.sleep(0)  # A is asked about "Next"
        renamed = reflex.Seen(seen.state, "", None, [{**row, "name": "Place order"}], 0, "", 0)
        loop.space(renamed, wait=False)  # B's read relabels the shared move
        await pending

    asyncio.run(_race())
    assert "Place order" in move.label and move.risky is None
    assert move in loop.risk_batch(space), "the next request asks about its new words"


def test_virtual_time_does_not_act_on_an_answer_past_the_deadline(seam):
    page = Page()
    seam.use(Model(lambda *_: {"operation": "PRESS_S"}, latency=1.3, page=page))
    result = _run({**STOP_AT, "mode": "virtual_time", "max_seconds": 1}, _ctx(page))
    assert result["stop"] == "budget" and page.pressed == []
    assert page.clock[-1] == "release"


def test_realtime_acts_on_the_answer_to_its_last_allowed_question(seam):
    page = Page()
    seam.use(Model(lambda *_: {"operation": "PRESS_S"}, page=page))
    result = _run({**STOP_AT, "mode": "realtime", "max_decisions": 1}, _ctx(page))
    assert page.pressed == ["s"] and result["applied"] == 1


def test_values_are_sourced_only_after_the_url_gate(seam):
    page = Page()
    model = seam.use(Model(stop_rule, page=page))
    ctx = dataclasses.replace(_ctx(page), url_allowed=lambda url: "page.example" not in url)
    result = _run({"goal": "Fill it", "type": ["e2=M"], "mode": "realtime"}, ctx)
    assert result["code"] == "redirect_refused"
    assert page.observes == 0 and model.calls == []


@pytest.mark.parametrize("lead", [False, True])
def test_at_the_decision_limit_answers_already_asked_are_still_used(seam, lead):
    page = Page()
    seam.use(Model(lambda *_: {"operation": "PRESS_S"}, latency=0.2, page=page))
    result = _run({**STOP_AT, "mode": "realtime", "max_decisions": 1, "lead": lead}, _ctx(page))
    assert page.pressed == ["s"] and result["applied"] == 1 and result["stop"] == "budget"


def test_step_mode_does_not_act_on_an_answer_past_the_deadline(seam):
    page = Page()
    seam.use(Model(lambda *_: {"operation": "PRESS_S"}, latency=1.3, page=page))
    result = _run({**STOP_AT, "max_seconds": 1}, _ctx(page))
    assert result["stop"] == "budget" and page.pressed == []
