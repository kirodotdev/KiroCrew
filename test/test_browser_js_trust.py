"""``js`` runs with no card under "Trust all tools", YOLO or a site's act grant.

The owner's ruling: under YOLO and trust, browser JS needs no approval. Pinned: the card lifts for ``js`` on a person's slot that trusts all
tools or that YOLO reaches, and in the current tab of a host with an act grant;
it stays with no trust, a read grant, another site, a named tab, an app's slot,
a Dashboard card's turn, and for ``cdp`` and ``http_get`` whatever is trusted.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from test_browser_card_ask import _State
from test_browser_ops_route import _install, _no_stream, _Supervisor

from kiro_crew import browser_host_trust as bht
from kiro_crew.browser_harness import live_hub as hub_mod
from kiro_crew.dashboard.handlers import browser_ops as mod

_SESSION = "dashboard:chat-1"
_JS = {"expression": "document.title"}
_PAGE = "https://app.example/form"


@pytest.fixture(autouse=True)
def _quiet(_floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    from unittest.mock import MagicMock

    _floor_monkeypatch.setattr(mod, "_sel", lambda: MagicMock())
    _floor_monkeypatch.setattr(bht, "_pending", {})
    hub_mod.reset_live_hub()
    yield
    hub_mod.reset_live_hub()


class _YoloState(_State):
    def __init__(self, *, yolo: bool, reaches: bool = True) -> None:
        super().__init__()
        self._yolo, self._reaches = yolo, reaches

    def is_yolo_active(self) -> bool:
        return self._yolo

    def yolo_reaches(self, slot: Any) -> bool:
        return self._reaches


def _slot(**attrs: Any) -> SimpleNamespace:
    return SimpleNamespace(
        key="chat-1", _trusted_patterns=set(), _trusted_pattern_deadlines={}, **attrs
    )


def _with_grant(state: _State, host: str, cls: str) -> _State:
    slot = state._slots["chat-1"]
    slot._trusted_patterns.add(bht.grant_pattern(bht.trust_key(host, cls)))
    return state


def _state(*, yolo: bool = False, reaches: bool = True, **slot_attrs: Any) -> _State:
    state = _YoloState(yolo=yolo, reaches=reaches)
    state._slots["chat-1"] = _slot(**slot_attrs)
    return state


def _page(url: str) -> None:
    hub_mod.get_live_hub()._session(bht.browser_session_key(_SESSION)).url = url


def _ran(sup: _Supervisor, op: str) -> bool:
    if op == "js":
        return any(h == "js" and kw.get("expression") == _JS["expression"] for h, kw in sup.calls)
    return any(h == op for h, _kw in sup.calls)


async def _call(
    monkeypatch: pytest.MonkeyPatch,
    state: _State,
    op: str = "js",
    args: dict[str, Any] | None = None,
    tab_url: str = _PAGE,
) -> tuple[dict[str, Any], _Supervisor]:
    sup = _Supervisor()
    sup.tab = {"targetId": "T1", "url": tab_url, "title": "t"}
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    _page(_PAGE)
    answer, _ = await mod.run_browser_op(_SESSION, op, dict(args or _JS), state=state)
    return answer, sup


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        pytest.param(lambda: _state(yolo=True), id="yolo"),
        pytest.param(lambda: _state(_trust=True), id="trust-all-tools"),
        pytest.param(lambda: _with_grant(_state(), "app.example", "act"), id="act-site-grant"),
    ],
)
async def test_js_runs_with_no_card_under_yolo_or_trust(
    monkeypatch: pytest.MonkeyPatch, state: Any
) -> None:
    built = state()
    answer, sup = await _call(monkeypatch, built)
    assert built.opened == [] and answer.get("code") != "needs_approval"
    assert _ran(sup, "js")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        pytest.param(lambda: _state(), id="no-trust"),
        pytest.param(lambda: _with_grant(_state(), "app.example", "read"), id="read-grant"),
        pytest.param(lambda: _with_grant(_state(), "other.example", "act"), id="other-site"),
        pytest.param(lambda: _state(yolo=True, reaches=False), id="yolo-not-reaching"),
        pytest.param(lambda: _state(_trust_reads=True), id="trust-reads"),
        pytest.param(lambda: _state(_trust=True, _app="someapp"), id="app-slot"),
    ],
)
async def test_js_keeps_its_card_without_that_trust(
    monkeypatch: pytest.MonkeyPatch, state: Any
) -> None:
    built = state()
    answer, sup = await _call(monkeypatch, built)
    assert answer["code"] == "needs_approval" and len(built.opened) == 1
    assert not _ran(sup, "js")


@pytest.mark.asyncio
async def test_an_act_grant_does_not_cover_js_in_a_named_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _with_grant(_state(), "app.example", "act")
    answer, _sup = await _call(monkeypatch, state, args={**_JS, "target_id": "T2"})
    assert answer["code"] == "needs_approval" and len(state.opened) == 1


@pytest.mark.asyncio
async def test_js_on_a_grant_refuses_a_tab_that_left_the_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _with_grant(_state(), "app.example", "act")
    answer, sup = await _call(monkeypatch, state, tab_url="https://evil.example/")
    assert answer["code"] == bht.SCOPE_CHANGED_CODE and state.opened == []
    assert not _ran(sup, "js")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op", "args"),
    [("cdp", {"method": "Page.reload"}), ("http_get", {"url": "https://app.example/"})],
)
async def test_cdp_and_http_get_still_ask_under_yolo_and_every_trust(
    monkeypatch: pytest.MonkeyPatch, op: str, args: dict[str, Any]
) -> None:
    state = _with_grant(_state(yolo=True, _trust=True), "app.example", "act")
    answer, _sup = await _call(monkeypatch, state, op=op, args=args)
    assert answer["code"] == "needs_approval" and len(state.opened) == 1


@pytest.mark.asyncio
async def test_a_site_grant_never_narrows_trust_all_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The tab left the granted host: session-wide trust still runs the script.
    state = _with_grant(_state(_trust=True), "app.example", "act")
    answer, sup = await _call(monkeypatch, state, tab_url="https://other.example/")
    assert state.opened == [] and answer.get("code") != bht.SCOPE_CHANGED_CODE
    assert _ran(sup, "js")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        pytest.param(lambda: _state(yolo=True, _trust=True), id="trust-and-yolo"),
        pytest.param(lambda: _with_grant(_state(), "app.example", "act"), id="act-site-grant"),
    ],
)
async def test_a_dashboard_card_s_turn_keeps_the_js_card(
    monkeypatch: pytest.MonkeyPatch, state: Any
) -> None:
    # A card's turn carries a page's words: no trust auto-approves its steps.
    from kiro_crew.dashboard import card_send

    built = state()
    with card_send.card_turn(_SESSION):
        answer, sup = await _call(monkeypatch, built)
    assert answer["code"] == "needs_approval" and len(built.opened) == 1
    assert not _ran(sup, "js")


def test_a_card_turn_stopped_while_its_request_waits_keeps_the_card() -> None:
    # The turn's mark is gone, but the request arrived from a card's turn.
    from kiro_crew.dashboard import card_send
    from kiro_crew.dashboard.handlers import browser_card_ask as card

    state = _with_grant(_state(yolo=True, _trust=True), "app.example", "act")
    _page(_PAGE)
    arrived = SimpleNamespace(
        get={card_send.CARD_TURN_AT_ARRIVAL: True}.get, headers={"X-Session-Key": _SESSION}
    )
    assert card.trusts_all_tools(state, _SESSION, "js") is True
    assert card.trusts_all_tools(state, _SESSION, "js", arrived) is False
    assert card.site_grant_host(state, _SESSION, "js", _JS, arrived) == ""
