"""``browser.step``: the optional decision-model hooks on browser ops.

What is pinned:

* every way the seam can say no -- no hooks, no model, no consent, governance
  denied, the owner's switch off, a timeout, a provider error, an out-of-domain
  answer -- leaves the op's result byte-identical to today's, and the gated-off
  ones never enter the provider;
* the element pick is used only on a clear answer, the step check rides on the
  result as a hint and never changes it;
* screenshots go out by default once consent stands (an explicit no to the image
  scope stops them), never while a password box is on
  the page or the person holds the browser, and the canvas locator clicks only
  inside a canvas.

The page is a fake ``helper`` that answers the walker's methods; the gate, the
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
from typing import Any

import pytest

from kiro_crew.browser_harness import decide as decide_mod
from kiro_crew.browser_harness import ops
from kiro_crew.config.sections import (
    DECISION_PROVIDER_ENDPOINT_DEFAULT,
    DecisionProviderConfig,
    DecisionsConfig,
)
from kiro_crew.decisions import gate
from kiro_crew.decisions.types import Answer, Box, Choice, ImageAsset, Noul

SESSION = "dashboard:chat-1"

NODES_BEFORE = [
    {"depth": 0, "role": "heading", "name": "Blue Kettle"},
    {"depth": 1, "role": "button", "name": "Add to cart", "ref": "e1", "state": {}},
    {"depth": 0, "role": "heading", "name": "Red Kettle"},
    {"depth": 1, "role": "button", "name": "Add to cart", "ref": "e2", "state": {}},
    {"depth": 0, "role": "textbox", "name": "Password", "ref": "e3", "state": {"password": True}},
]
NODES_AFTER = NODES_BEFORE + [{"depth": 0, "role": "heading", "name": "Added to your cart"}]


def _png(width: int = 4, height: int = 4) -> bytes:
    """A real, tiny PNG, so nothing here depends on an image library."""
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class FakePage:
    """The agent's tab: answers the walker's methods and the Harness helpers."""

    _CALL = re.compile(r"\A\.(\w+)\((.*)\)\Z", re.DOTALL)

    def __init__(self, *, password_boxes: int = 0, canvases: list | None = None) -> None:
        self.clicked = False
        self.password_boxes = password_boxes
        #: Fields the person filled through a quick ask that still hold a value.
        self.asked_values = 0
        self.canvases = canvases or []
        self.calls: list[tuple[str, Any]] = []
        self.shots = 0

    async def helper(self, name: str, **kwargs: Any) -> Any:
        if name != "js":
            self.calls.append((name, kwargs))
        if name == "js":
            expr = kwargs["expression"]
            assert expr.startswith(ops.WALKER_SOURCE)
            match = self._CALL.match(expr[len(ops.WALKER_SOURCE) :])
            assert match, expr[-80:]
            method, args = match.group(1), json.loads(f"[{match.group(2)}]")
            self.calls.append((method, args))
            return self._walker(method, args)
        if name == "click":
            self.clicked = True
            return None
        if name == "screenshot":
            self.shots += 1
            from pathlib import Path

            Path(kwargs["path"]).write_bytes(_png())
            return None
        if name == "page_info":
            return {"w": 1000, "h": 500}
        return None

    def _walker(self, method: str, args: list) -> Any:
        if method == "snapshot":
            nodes = NODES_AFTER if self.clicked else NODES_BEFORE
            return {"url": "https://shop.example/", "title": "Shop", "nodes": nodes}
        if method == "resolve":
            known = {"e1": 10.0, "e2": 30.0, "e3": 50.0}
            if args[0] not in known:
                return {"ok": False, "code": "ref_stale"}
            return {"ok": True, "x": known[args[0]], "y": 5.0, "tag": "button"}
        if method == "passwordBoxes":
            return self.password_boxes
        if method == "askedValues":
            return self.asked_values
        if method == "canvasRects":
            return {"w": 1000, "h": 500, "rects": self.canvases}
        raise AssertionError(f"unexpected walker method {method}")


class FakeProvider:
    """Records every request; answers from a function of the questions."""

    def __init__(self, answer=None, *, raises: BaseException | None = None, delay: float = 0.0):
        self.answer = answer or _clear_answers
        self.raises = raises
        self.delay = delay
        self.calls: list[dict] = []

    async def ask(self, state, questions, *, assets=None):
        self.calls.append({"state": state, "questions": questions, "assets": assets})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return self.answer(questions)


def _clear_answers(questions) -> dict[str, Answer]:
    """A confident provider: picks the Red Kettle's button, says the step worked."""
    out: dict[str, Answer] = {}
    for q in questions:
        if isinstance(q, Choice):
            out[q.id] = Answer(id=q.id, value="e2" if "e2" in q.options else q.options[0], p=0.9)
        elif isinstance(q, Box):
            out[q.id] = Answer(
                id=q.id,
                value={"left": 0.1, "top": 0.1, "right": 0.3, "bottom": 0.3},
                p=0.9,
                confidence=0.9,
            )
        elif isinstance(q, Noul):
            yes = q.id in {"present", "as_expected", "changed"}
            out[q.id] = Answer(id=q.id, value=0.95 if yes else 0.02, p=0.95)
    return out


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
        endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT, denied=False, provider=FakeProvider()
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
    state.keystone = keystone
    return state


def _ctx(
    page: FakePage, *, hooks: bool = True, in_control=lambda: False, tmp=None
) -> ops.OpContext:
    return ops.OpContext(
        helper=page.helper,
        in_control=in_control,
        screenshot_dir=tmp,
        decisions=decide_mod.BrowserDecisions(SESSION) if hooks else None,
    )


def _run(op: str, args: dict, ctx: ops.OpContext) -> dict:
    return asyncio.run(ops.run_op(op, args, ctx))


def _baseline(op: str, args: dict, tmp_path) -> tuple[dict, list]:
    page = FakePage()
    result = _run(op, args, _ctx(page, hooks=False, tmp=tmp_path))
    return result, page.calls


# -- every "no" is today's result, and the gated ones never reach the provider ----


GATED_OFF = ["no_model", "no_consent", "endpoint_moved", "governance_denied", "switch_off"]


def _gate_off(seam, how: str) -> None:
    if how == "no_model":
        seam.endpoint = "none"
    elif how == "no_consent":
        seam.write(enabled=False)
    elif how == "endpoint_moved":
        # Consent stands for another address; both browser scopes still read granted.
        seam.endpoint = "https://elsewhere.example/v1/systemone"
    elif how == "governance_denied":
        seam.denied = True
    elif how == "switch_off":
        seam.write(browser=False)


@pytest.mark.parametrize("how", GATED_OFF)
def test_a_gated_off_seam_is_todays_click_and_calls_nothing(seam, tmp_path, how):
    _gate_off(seam, how)
    args = {"ref": "e1"}
    expected, expected_calls = _baseline("click", args, tmp_path)
    page = FakePage()
    assert _run("click", args, _ctx(page, tmp=tmp_path)) == expected
    assert page.calls == expected_calls, "no extra page call when the seam is off"
    assert seam.provider.calls == []


@pytest.mark.parametrize("how", GATED_OFF)
def test_a_gated_off_seam_refuses_a_target_exactly_as_with_no_hooks(seam, tmp_path, how):
    _gate_off(seam, how)
    args = {"target": "the red kettle's add to cart button"}
    expected, _ = _baseline("click", args, tmp_path)
    assert expected["code"] == "needs_ref"
    page = FakePage()
    assert _run("click", args, _ctx(page, tmp=tmp_path)) == expected
    assert page.calls == [("current_tab", {})] and seam.provider.calls == []


def test_no_model_chosen_does_not_even_read_the_keystone(seam, tmp_path, monkeypatch):
    seam.endpoint = "none"

    def _boom(*_a, **_k):
        raise AssertionError("the keystone was read with no decision model chosen")

    monkeypatch.setattr("kiro_crew.decisions.consent.load_state", _boom)
    expected, _ = _baseline("click", {"ref": "e1"}, tmp_path)
    assert _run("click", {"ref": "e1"}, _ctx(FakePage(), tmp=tmp_path)) == expected


@pytest.mark.parametrize(
    "provider",
    [
        FakeProvider(delay=5.0),
        FakeProvider(raises=RuntimeError("provider down")),
        FakeProvider(lambda qs: {q.id: Answer(id=q.id, value="e99", p=0.9) for q in qs}),
    ],
    ids=["timeout", "error", "out-of-domain"],
)
def test_a_failed_decision_is_todays_result(seam, tmp_path, provider):
    seam.provider = provider
    args = {"ref": "e1"}
    expected, _ = _baseline("click", args, tmp_path)
    assert _run("click", args, _ctx(FakePage(), tmp=tmp_path)) == expected
    assert provider.calls, "the decision was attempted"
    expected, _ = _baseline("click", {"target": "red kettle add"}, tmp_path)
    assert _run("click", {"target": "red kettle add"}, _ctx(FakePage(), tmp=tmp_path)) == expected


def test_every_request_carries_the_point_s_own_wait_ceiling(seam, tmp_path, monkeypatch):
    seen: list = []
    real = gate.decide

    async def _spy(*args, **kwargs):
        seen.append((kwargs.get("images") is not None, kwargs.get("max_wait_s")))
        return await real(*args, **kwargs)

    monkeypatch.setattr(gate, "decide", _spy)
    seam.write(browser_images=True)
    _run("click", {"target": "red kettle add"}, _ctx(FakePage(), tmp=tmp_path))
    assert seen and all(
        wait == (decide_mod.IMAGE_WAIT_S if image else decide_mod.TEXT_WAIT_S)
        for image, wait in seen
    )


def test_a_frame_that_cannot_be_written_is_the_outline_path(seam, tmp_path):
    seam.write(browser_images=True)
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    expected, _ = _baseline("click", {"ref": "e1"}, tmp_path)
    page = FakePage()
    result = _run("click", {"ref": "e1"}, _ctx(page, tmp=blocked))
    assert result.pop("verify")["by"] == "outline"
    assert result == expected and page.shots == 0


def test_a_navigation_check_never_names_the_url(seam, tmp_path):
    hint = decide_mod._expectation("navigate", {"url": "https://x.example/cb#access_token=abc"}, {})
    assert "x.example" not in hint and "access_token" not in hint


# -- the element pick ---------------------------------------------------------


def test_a_clear_pick_clicks_the_chosen_ref_and_says_which(seam, tmp_path):
    page = FakePage()
    result = _run("click", {"target": "add the red kettle to the cart"}, _ctx(page, tmp=tmp_path))
    assert result["ok"] is True and result["ref"] == "e2" and result["x"] == 30.0
    assert result["picked"] == {
        "ref": "e2",
        "element": 'button "Add to cart" under heading "Red Kettle"',
        "by": "outline",
    }
    pick = seam.provider.calls[0]
    assert [type(q).__name__ for q in pick["questions"]] == [
        "Choice",
        "Noul",
        "Noul",
        "Noul",
        "Noul",
    ]
    choice = pick["questions"][0]
    # Identical buttons differ by the heading above them.
    assert choice.criteria["e1"] != choice.criteria["e2"]
    assert pick["assets"] is None


@pytest.mark.parametrize(
    "override",
    [{"present": 0.5}, {"pick_p": 0.4}, {"error_shown": 0.9}, {"bot_check": 0.9}],
    ids=["unsure-present", "unclear-pick", "error-page", "bot-check"],
)
def test_an_unclear_or_risky_pick_is_not_used(seam, tmp_path, override):
    def _answers(qs):
        out = _clear_answers(qs)
        for qid, value in override.items():
            if qid == "pick_p":
                out["pick"] = Answer(id="pick", value="e2", p=value)
            elif qid in out:
                out[qid] = Answer(id=qid, value=value, p=max(value, 1 - value))
        return out

    seam.provider = FakeProvider(_answers)
    page = FakePage()
    result = _run("click", {"target": "red kettle add"}, _ctx(page, tmp=tmp_path))
    assert result["code"] == "needs_ref"
    assert not page.clicked


# -- the step check -----------------------------------------------------------


def test_the_step_check_is_a_hint_beside_todays_result(seam, tmp_path):
    seam.write(browser_images=False)
    expected, _ = _baseline("click", {"ref": "e1"}, tmp_path)
    result = _run("click", {"ref": "e1"}, _ctx(FakePage(), tmp=tmp_path))
    hint = result.pop("verify")
    assert result == expected
    assert hint == {"as_expected": "yes", "changed": "yes", "error_shown": "no", "by": "outline"}
    request = seam.provider.calls[-1]
    assert request["assets"] is None
    assert "Added to your cart" in request["state"]["after_page"]
    assert "Added to your cart" not in request["state"]["before_page"]


def test_the_text_request_carries_no_password_value_or_url(seam, tmp_path):
    seam.write(browser_images=False)
    _run("click", {"ref": "e1"}, _ctx(FakePage(), tmp=tmp_path))
    sent = json.dumps([c["state"] for c in seam.provider.calls])
    assert "shop.example" not in sent
    assert '"Password" [ref=e3] [password]' in seam.provider.calls[-1]["state"]["before_page"]


# -- screenshots --------------------------------------------------------------


def test_screenshots_are_on_by_default_once_consent_stands(seam, tmp_path):
    page = FakePage()
    result = _run("click", {"ref": "e1"}, _ctx(page, tmp=tmp_path))
    assert result["verify"]["by"] == "screenshot" and page.shots == 2


def test_an_explicit_no_to_screenshots_sends_none(seam, tmp_path):
    seam.write(browser_images=False)
    page = FakePage()
    _run("click", {"ref": "e1"}, _ctx(page, tmp=tmp_path))
    assert page.shots == 0
    assert all(c["assets"] is None for c in seam.provider.calls)


def test_with_the_image_scope_the_check_compares_two_screenshots(seam, tmp_path):
    seam.write(browser_images=True)
    page = FakePage()
    result = _run("click", {"ref": "e1"}, _ctx(page, tmp=tmp_path))
    assert result["verify"]["by"] == "screenshot"
    request = seam.provider.calls[-1]
    assert set(request["assets"]) == {request["state"]["before"], request["state"]["after"]}
    assert all(
        isinstance(a, ImageAsset) and a.data.startswith(b"\x89PNG")
        for a in request["assets"].values()
    )
    assert page.shots == 2
    assert [kw for name, kw in page.calls if name == "screenshot"][0]["max_dim"] == 1568
    assert list(tmp_path.glob("shot-*.png")) == [], "frames are deleted once read"


def test_no_screenshot_while_a_password_box_is_on_the_page(seam, tmp_path):
    seam.write(browser_images=True)
    page = FakePage(password_boxes=1)
    result = _run("click", {"ref": "e1"}, _ctx(page, tmp=tmp_path))
    assert page.shots == 0
    assert result["verify"]["by"] == "outline"
    assert all(c["assets"] is None for c in seam.provider.calls)


def test_no_screenshot_while_a_quick_ask_field_holds_its_value(seam, tmp_path):
    # passwordBoxes stops at depth 8; askedValues fails closed deeper, so it is
    # asked too: the person's code or email never reaches the decision model.
    seam.write(browser_images=True)
    page = FakePage()
    page.asked_values = 1
    result = _run("click", {"ref": "e1"}, _ctx(page, tmp=tmp_path))
    assert page.shots == 0
    assert result["verify"]["by"] == "outline"
    assert all(c["assets"] is None for c in seam.provider.calls)


def test_no_screenshot_while_the_person_holds_the_browser(seam, tmp_path):
    seam.write(browser_images=True)
    held = {"n": 0}

    def _in_control() -> bool:
        # Free for run_op's own check, then the person takes over.
        held["n"] += 1
        return held["n"] > 1

    page = FakePage()
    _run("click", {"ref": "e1"}, _ctx(page, in_control=_in_control, tmp=tmp_path))
    assert page.shots == 0
    assert all(c["assets"] is None for c in seam.provider.calls)


def test_a_local_model_is_never_sent_a_screenshot(seam, tmp_path, monkeypatch):
    seam.write(browser_images=True)
    from kiro_crew.decisions import provider as provider_mod

    monkeypatch.setattr(
        provider_mod, "select", lambda *a, **k: provider_mod.get(provider_mod.PROVIDER_LOCAL)
    )
    page = FakePage()
    result = _run("click", {"ref": "e1"}, _ctx(page, tmp=tmp_path))
    assert page.shots == 0 and result["verify"]["by"] == "outline"


# -- the canvas locator -------------------------------------------------------


def _nothing_in_the_outline(qs):
    out = _clear_answers(qs)
    if "pick" in out:
        out["present"] = Answer(id="present", value=0.05, p=0.95)
    return out


def test_a_canvas_target_is_clicked_where_the_box_says(seam, tmp_path):
    seam.write(browser_images=True)
    seam.provider = FakeProvider(_nothing_in_the_outline)
    page = FakePage(canvases=[[0, 0, 400, 300]])
    result = _run("click", {"target": "the red circle on the drawing"}, _ctx(page, tmp=tmp_path))
    # Box centre (0.2, 0.2) of a 1000 x 500 viewport.
    assert result["ok"] is True and (result["x"], result["y"]) == (200.0, 100.0)
    assert result["picked"] == {"x": 200.0, "y": 100.0, "by": "screenshot"}
    box = [c for c in seam.provider.calls if any(isinstance(q, Box) for q in c["questions"])]
    assert len(box) == 1 and len(box[0]["assets"]) == 1


def test_a_box_outside_every_canvas_is_not_clicked(seam, tmp_path):
    seam.write(browser_images=True)
    seam.provider = FakeProvider(_nothing_in_the_outline)
    page = FakePage(canvases=[[600, 300, 100, 100]])
    result = _run("click", {"target": "the red circle"}, _ctx(page, tmp=tmp_path))
    assert result["code"] == "needs_ref" and not page.clicked


def test_no_canvas_means_no_screenshot_is_asked(seam, tmp_path):
    seam.write(browser_images=True)
    seam.provider = FakeProvider(_nothing_in_the_outline)
    page = FakePage()
    assert _run("click", {"target": "x"}, _ctx(page, tmp=tmp_path))["code"] == "needs_ref"
    assert page.shots == 0


def test_without_the_image_scope_there_is_no_canvas_locator(seam, tmp_path):
    seam.write(browser_images=False)
    seam.provider = FakeProvider(_nothing_in_the_outline)
    page = FakePage(canvases=[[0, 0, 400, 300]])
    assert _run("click", {"target": "x"}, _ctx(page, tmp=tmp_path))["code"] == "needs_ref"
    assert page.shots == 0


# -- the gate's image rules ---------------------------------------------------


def _image_request(**kwargs):
    return asyncio.run(
        gate.decide(
            "browser.step",
            {"screen": "a"},
            [Noul(id="present", prompt="?"), Box(id="where", prompt="?")],
            session_key=SESSION,
            **kwargs,
        )
    )


def test_an_image_without_an_image_scope_is_not_sent(seam):
    seam.write(browser_images=True)
    assert _image_request(images={"a": ImageAsset(_png())}) is None
    assert seam.provider.calls == []


def test_an_image_scope_not_granted_sends_nothing(seam):
    seam.write(browser_images=False)
    assert _image_request(images={"a": ImageAsset(_png())}, scopes=("browser_images",)) is None
    assert seam.provider.calls == []


def test_a_box_with_no_image_is_refused(seam):
    seam.write(browser_images=True)
    assert _image_request(scopes=("browser_images",)) is None
    assert seam.provider.calls == []


def test_a_granted_image_request_reaches_the_provider_with_its_assets(seam):
    seam.write(browser_images=True)
    answers = _image_request(images={"a": ImageAsset(_png())}, scopes=("browser_images",))
    assert answers is not None and answers["where"].value["right"] == 0.3
    assert set(seam.provider.calls[0]["assets"]) == {"a"}


# -- the System One wire shape for images and Box -----------------------------


def test_box_and_images_on_the_wire():
    from kiro_crew.decisions import impl_systemone as impl

    body = impl._to_wire(
        {"screen": "a"},
        "jev-latest",
        [Box(id="where", prompt="the button in `screen`")],
        {"a": ImageAsset(b"\x89PNG-bytes")},
    )
    assert body["questions"]["where"] == {"type": "box", "instructions": "the button in `screen`"}
    assert body["assets"]["a"]["type"] == "image" and body["assets"]["a"]["format"] == "png"
    assert "assets" not in impl._to_wire("s", "jev-latest", [Noul(id="n", prompt="?")])

    raw = {
        "answers": {
            "where": {
                "type": "box",
                "box": {"left": 0.1, "top": 0.2, "right": 0.5, "bottom": 0.6},
                "confidence": 0.8,
            }
        }
    }
    answer = impl._from_wire(raw, [Box(id="where", prompt="?")])["where"]
    assert (
        answer.value == {"left": 0.1, "top": 0.2, "right": 0.5, "bottom": 0.6} and answer.p == 0.8
    )


@pytest.mark.parametrize(
    "box",
    [
        {"left": 0.5, "top": 0.2, "right": 0.5, "bottom": 0.6},
        {"left": -0.1, "top": 0.2, "right": 0.5, "bottom": 0.6},
        {"left": 0.1, "top": 0.2, "right": 0.5},
    ],
    ids=["no-area", "out-of-range", "missing-edge"],
)
def test_a_malformed_box_is_a_protocol_error(box):
    from kiro_crew.decisions import impl_systemone as impl

    raw = {"answers": {"where": {"type": "box", "box": box, "confidence": 0.8}}}
    with pytest.raises(impl.ProviderProtocolError):
        impl._from_wire(raw, [Box(id="where", prompt="?")])


@pytest.mark.parametrize(
    "asset",
    [ImageAsset(b"", "png"), ImageAsset(b"x", "gif"), "not-an-asset"],
    ids=["empty", "gif", "type"],
)
def test_an_unsendable_image_is_a_protocol_error(asset):
    from kiro_crew.decisions import impl_systemone as impl

    with pytest.raises(impl.ProviderProtocolError):
        impl._to_wire("s", "jev-latest", [Noul(id="n", prompt="?")], {"a": asset})


def test_the_image_header_is_sent_only_with_an_image_or_a_box(monkeypatch):
    from kiro_crew.decisions import impl_systemone as impl

    sent: list[dict] = []

    class _Resp:
        status_code = 200
        headers: dict = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def aiter_bytes(self, _n):
            yield json.dumps({"answers": {"n": {"type": "noul", "noul": 0.9}}}).encode()

    class _Session:
        def stream(self, method, url, **kwargs):
            sent.append(kwargs["headers"])
            return _Resp()

    monkeypatch.setattr(impl, "_session_for_running_loop", lambda: _Session())
    provider = impl.SystemOneProvider(
        SimpleNamespace(
            endpoint="http://127.0.0.1:9/v1/systemone", model="m", api_key="", timeout_ms=1000
        )
    )
    asyncio.run(provider.ask("s", [Noul(id="n", prompt="?")]))
    asyncio.run(provider.ask("s", [Noul(id="n", prompt="?")], assets={"a": ImageAsset(b"x")}))
    header = impl.HOSTED_TYPESAFE.images_header
    assert header not in sent[0] and sent[1][header] == "true"


# -- the default-on outline scope across a consent cycle ----------------------


def test_cycling_consent_keeps_an_explicit_no_to_outlines(seam):
    from kiro_crew.decisions import consent

    consent.save_enabled(True, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT, browser=False)
    consent.save_enabled(False, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT)
    consent.save_enabled(True, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT)
    assert consent.consented_browser() is False


def test_cycling_consent_keeps_an_explicit_no_to_screenshots(seam):
    from kiro_crew.decisions import consent

    consent.save_enabled(True, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT, browser_images=False)
    state = consent.save_enabled(False, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT)
    assert state[consent.STATE_KEY_BROWSER_IMAGES] is False
    consent.save_enabled(True, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT)
    assert consent.consented_browser_images() is False


@pytest.mark.parametrize("scope", ["browser", "browser_images"])
def test_a_no_sent_while_consent_is_off_survives_turning_it_back_on(seam, scope):
    # Two tabs: one turns consent off, the other's scope-only "no" lands after it.
    from kiro_crew.decisions import consent

    consent.save_enabled(True, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT)
    consent.save_enabled(False, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT)
    consent.save_enabled(
        consent.KEEP_ENABLED, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT, **{scope: False}
    )
    consent.save_enabled(True, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT)
    read = consent.consented_browser if scope == "browser" else consent.consented_browser_images
    assert read() is False


def test_screenshots_read_granted_when_absent_and_a_yes_returns_to_the_default(seam):
    from kiro_crew.decisions import consent

    assert consent.consented_browser_images({}) is True
    consent.save_enabled(True, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT, browser_images=True)
    state = consent.save_enabled(False, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT)
    assert consent.STATE_KEY_BROWSER_IMAGES not in state


def test_cycling_consent_returns_a_yes_to_the_default(seam):
    from kiro_crew.decisions import consent

    consent.save_enabled(True, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT, browser=True)
    state = consent.save_enabled(False, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT)
    assert consent.STATE_KEY_BROWSER not in state
    consent.save_enabled(True, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT)
    assert consent.consented_browser() is True


def test_a_canvas_probe_that_fails_is_todays_refusal(seam, tmp_path):
    seam.write(browser_images=True)
    seam.provider = FakeProvider(_nothing_in_the_outline)

    class _Gone(FakePage):
        def _walker(self, method, args):
            if method == "canvasRects":
                raise RuntimeError("context destroyed")
            return super()._walker(method, args)

    assert _run("click", {"target": "x"}, _ctx(_Gone(), tmp=tmp_path))["code"] == "needs_ref"


def test_a_password_box_that_appears_during_capture_discards_the_frame(seam, tmp_path):
    seam.write(browser_images=True)

    class _LoginAppears(FakePage):
        async def helper(self, name, **kwargs):
            out = await super().helper(name, **kwargs)
            if name == "screenshot":
                self.password_boxes = 1
            return out

    page = _LoginAppears()
    result = _run("click", {"ref": "e1"}, _ctx(page, tmp=tmp_path))
    assert page.shots == 1
    assert result["verify"]["by"] == "outline"
    assert all(c["assets"] is None for c in seam.provider.calls)


def test_a_stalled_step_check_keeps_the_op_s_own_result(seam, tmp_path, monkeypatch):
    seam.write(browser_images=False)
    monkeypatch.setattr(decide_mod, "VERIFY_BUDGET_S", 0.05)
    expected, _ = _baseline("click", {"ref": "e1"}, tmp_path)

    class _Stalls(FakePage):
        def _walker(self, method, args):
            if method == "snapshot" and self.clicked:
                raise _Stall()
            return super()._walker(method, args)

        async def helper(self, name, **kwargs):
            try:
                return await super().helper(name, **kwargs)
            except _Stall:
                await asyncio.Event().wait()  # released only by the hook's own bound

    class _Stall(Exception):
        pass

    assert _run("click", {"ref": "e1"}, _ctx(_Stalls(), tmp=tmp_path)) == expected


def test_a_stalled_password_check_costs_the_op_at_most_the_frame_bound(seam, tmp_path, monkeypatch):
    monkeypatch.setattr(decide_mod, "FRAME_WAIT_S", 0.05)
    seam.write(browser_images=True)
    expected, _ = _baseline("click", {"ref": "e1"}, tmp_path)

    class _Stall(Exception):
        pass

    class _Stalls(FakePage):
        def _walker(self, method, args):
            if method == "passwordBoxes":
                raise _Stall()
            return super()._walker(method, args)

        async def helper(self, name, **kwargs):
            try:
                return await super().helper(name, **kwargs)
            except _Stall:
                await asyncio.Event().wait()  # released only by the hook's own bound

    page = _Stalls()
    result = _run("click", {"ref": "e1"}, _ctx(page, tmp=tmp_path))
    assert result.pop("verify")["by"] == "outline"
    assert result == expected and page.shots == 0


@pytest.mark.parametrize(
    "args", [{"ref": "e1"}, {"target": "red kettle add"}], ids=["ref", "target"]
)
def test_stalled_work_before_the_op_is_todays_result(seam, tmp_path, monkeypatch, args):
    monkeypatch.setattr(decide_mod, "PRE_OP_BUDGET_S", 0.05)
    expected, _ = _baseline("click", args, tmp_path)

    class _Stall(Exception):
        pass

    class _Stalls(FakePage):
        def _walker(self, method, a):
            if method == "snapshot" and not self.clicked:
                raise _Stall()
            return super()._walker(method, a)

        async def helper(self, name, **kwargs):
            try:
                return await super().helper(name, **kwargs)
            except _Stall:
                await asyncio.Event().wait()  # released only by the hook's own bound

    result = _run("click", args, _ctx(_Stalls(), tmp=tmp_path))
    result.pop("verify", None)
    assert result == expected


def test_the_route_gives_the_hooks_their_own_headroom():
    from kiro_crew.browser_harness import budgets

    assert decide_mod.PRE_OP_BUDGET_S + decide_mod.VERIFY_BUDGET_S == budgets.DECISION_HEADROOM_S
    assert budgets.SHIM_FAST_TIMEOUT_S > budgets.GATEWAY_FAST_WORST_S
    assert budgets.GATEWAY_FAST_WORST_S >= budgets.OP_TIMEOUT_S * 2 + budgets.DECISION_HEADROOM_S


def test_a_stalled_gate_probe_is_todays_op(seam, tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr(decide_mod, "PRE_OP_BUDGET_S", 0.05)
    release = threading.Event()

    def _stuck(_session):
        # A stand-in for a stuck keystone read; set() below releases it.
        release.wait(1)  # flake-ok: bounded so asyncio.run's executor join stays short
        return decide_mod.Armed(text=True)

    monkeypatch.setattr(decide_mod, "_probe", _stuck)
    expected, _ = _baseline("click", {"ref": "e1"}, tmp_path)
    try:
        assert _run("click", {"ref": "e1"}, _ctx(FakePage(), tmp=tmp_path)) == expected
        assert _run("click", {"target": "x"}, _ctx(FakePage(), tmp=tmp_path))["code"] == "needs_ref"
    finally:
        release.set()
    assert seam.provider.calls == []


# -- the frame file ------------------------------------------------------------


def test_a_screenshot_file_swapped_for_a_link_is_not_read(tmp_path):
    """The screenshot directory is agent-writable and the gateway is not sandboxed:
    a link to another file in the screenshot's place is no frame."""
    import os

    secret = tmp_path / "credentials"
    secret.write_bytes(b"\x89PNG\r\n\x1a\n" + b"aws_secret_access_key=x")
    shot = tmp_path / "shot.png"
    os.symlink(secret, shot)
    assert decide_mod._read_png(shot) is None


def test_a_screenshot_that_is_not_a_png_is_not_read(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"[default]\naws_secret_access_key=x\n")
    assert decide_mod._read_png(shot) is None
    shot.write_bytes(_png())
    assert decide_mod._read_png(shot) == _png()
