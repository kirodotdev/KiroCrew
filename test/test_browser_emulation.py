"""``set_appearance`` and ``resize`` (``browser_harness.emulation``), against the fake
browser of ``test_browser_harness_ops``: what each sends through ``ctx.emulate``,
what it refuses before anything is sent, and how the op route wires the seam.
The relay half (one session per tab, ledger only) is in ``test_browser_harness_relay``.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from test_browser_harness_ops import FakeBrowser

from kiro_crew.browser_harness import emulation, op_classes, ops
from kiro_crew.browser_harness.live_hub import LiveHub
from kiro_crew.dashboard.handlers import browser_ops as route
from kiro_crew.mcp_tools.catalog.browser import BROWSER_OPS


class _Emulated:
    """Records every ``ctx.emulate`` call."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any]]] = []

    async def __call__(self, target: str, method: str, params: dict[str, Any]) -> None:
        self.sent.append((target, method, params))


@pytest.fixture
def fake(tmp_path: Any) -> FakeBrowser:
    fake = FakeBrowser(tmp_path)
    fake.helpers["current_tab"] = {"targetId": "T-agent", "url": "https://example.com/"}
    return fake


def _run(op: str, args: dict[str, Any], fake: FakeBrowser, sent: _Emulated | None) -> Any:
    ctx = replace(fake.ctx(), emulate=sent)
    return asyncio.run(ops.run_op(op, args, ctx))


def test_both_ops_are_advertised_and_classed_like_scroll() -> None:
    for op in ("set_appearance", "resize"):
        assert op in ops.OPS and op in BROWSER_OPS
        assert op_classes.host_trust_class(op, {}) == op_classes.CLASS_READ
        # They change the view, so a hand-back with a note still holds them.
        assert not op_classes.reads_only(op, {})
        # No ref or point: after a popup they run on the new tab, like navigate.
        assert op not in route._PAGE_TARGETED_OPS


@pytest.mark.parametrize(
    ("scheme", "features"),
    [
        ("dark", [{"name": "prefers-color-scheme", "value": "dark"}]),
        ("light", [{"name": "prefers-color-scheme", "value": "light"}]),
        ("reset", []),
    ],
)
def test_set_appearance_sends_the_color_scheme_on_the_current_tab(
    fake: FakeBrowser, scheme: str, features: list[Any]
) -> None:
    sent = _Emulated()
    out = _run("set_appearance", {"scheme": scheme}, fake, sent)
    assert out == {"ok": True, "scheme": scheme}
    assert sent.sent == [("T-agent", "Emulation.setEmulatedMedia", {"features": features})]
    assert fake.steps


@pytest.mark.parametrize("args", [{}, {"scheme": "Dark"}, {"scheme": "sepia"}, {"scheme": 1}])
def test_set_appearance_refuses_a_bad_scheme_before_it_sends(
    fake: FakeBrowser, args: dict[str, Any]
) -> None:
    sent = _Emulated()
    assert _run("set_appearance", args, fake, sent)["code"] == "bad_scheme"
    assert sent.sent == []


def test_resize_sets_the_viewport_and_reset_clears_it(fake: FakeBrowser) -> None:
    sent = _Emulated()
    assert _run("resize", {"width": 390, "height": 844}, fake, sent) == {
        "ok": True,
        "width": 390,
        "height": 844,
    }
    assert _run("resize", {"reset": True}, fake, sent) == {"ok": True, "reset": True}
    assert sent.sent == [
        (
            "T-agent",
            "Emulation.setDeviceMetricsOverride",
            {"width": 390, "height": 844, "deviceScaleFactor": 0, "mobile": False},
        ),
        ("T-agent", "Emulation.clearDeviceMetricsOverride", {}),
    ]


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"width": 800},
        {"width": emulation.WIDTH_MIN - 1, "height": 600},
        {"width": emulation.WIDTH_MAX + 1, "height": 600},
        {"width": 800, "height": emulation.HEIGHT_MIN - 1},
        {"width": 800, "height": emulation.HEIGHT_MAX + 1},
        {"width": 800.5, "height": 600},
        {"width": True, "height": 600},
        {"width": "800", "height": 600},
        {"reset": True, "width": 800, "height": 600},
        {"reset": "yes"},
        {"reset": False},
    ],
)
def test_resize_refuses_a_bad_size_before_it_sends(fake: FakeBrowser, args: dict[str, Any]) -> None:
    sent = _Emulated()
    assert _run("resize", args, fake, sent)["code"] == "bad_size"
    assert sent.sent == []


def test_the_bounds_themselves_are_accepted(fake: FakeBrowser) -> None:
    sent = _Emulated()
    for w, h in (
        (emulation.WIDTH_MIN, emulation.HEIGHT_MIN),
        (emulation.WIDTH_MAX, emulation.HEIGHT_MAX),
    ):
        assert _run("resize", {"width": w, "height": h}, fake, sent)["ok"] is True
    assert len(sent.sent) == 2


@pytest.mark.parametrize(
    ("op", "args"), [("set_appearance", {"scheme": "dark"}), ("resize", {"reset": True})]
)
def test_a_tab_the_agent_did_not_open_is_never_touched(
    fake: FakeBrowser, op: str, args: dict[str, Any]
) -> None:
    fake.helpers["current_tab"] = {"targetId": "T-person", "url": "https://example.com/"}
    sent = _Emulated()
    assert _run(op, args, fake, sent)["code"] == "not_agent_tab"
    assert sent.sent == []


def test_without_the_seam_both_ops_are_not_available(fake: FakeBrowser) -> None:
    assert _run("set_appearance", {"scheme": "dark"}, fake, None)["code"] == "not_available"
    assert _run("resize", {"width": 800, "height": 600}, fake, None)["code"] == "not_available"


def test_the_route_sends_through_the_relay_and_asks_the_ceiling_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    denied = {"on": False}
    stops: list[int] = []

    async def _denied(_key: str = "", *, audit: bool = True) -> bool:
        return denied["on"]

    monkeypatch.setattr(route, "browse_denied_async", _denied)
    monkeypatch.setattr(route, "stop_running_harness", lambda: stops.append(1))
    sent: list[tuple[str, str, dict[str, Any]]] = []

    async def emulate(target: str, method: str, params: dict[str, Any]) -> None:
        sent.append((target, method, params))

    sup = SimpleNamespace(relay=SimpleNamespace(emulate=emulate))
    ctx = route.build_context("chat-a", sup, LiveHub(), caller_key="chat-a")
    assert ctx.emulate is not None
    asyncio.run(ctx.emulate("T1", "Emulation.clearDeviceMetricsOverride", {}))
    assert sent == [("T1", "Emulation.clearDeviceMetricsOverride", {})]
    denied["on"] = True
    with pytest.raises(route.BrowsingDenied):
        asyncio.run(ctx.emulate("T1", "Emulation.clearDeviceMetricsOverride", {}))
    assert len(sent) == 1 and stops == [1]
