"""``js`` runs with no card under "Trust all tools", YOLO or a site's act grant.

The owner's ruling: under YOLO and trust, browser JS needs no approval. Pinned: the card lifts for ``js`` on a person's slot that trusts all
tools or that YOLO reaches, and in the current tab of a host with an act grant;
it stays with no trust, a read grant, another site, a named tab, an app's slot,
a Dashboard card's turn, and for ``cdp`` and ``http_get`` whatever is trusted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from test_browser_card_ask import _State
from test_browser_ops_route import _install, _no_stream, _Supervisor

from kiro_crew import browser_host_trust as bht
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.agent_sdk.capabilities import capabilities_for
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


async def _rung_approves_js_on(tmp_path: Any, host: str) -> None:
    """A dashboard turn's ``js`` call that the trusted-pattern rung approves on *host*'s act grant."""
    from test_browser_host_trust import _browser
    from test_browser_host_trust import _call as _params
    from test_browser_host_trust import _context_builder, _run_answered, _turn_harness

    h, run_chat = _turn_harness()
    state, client = h._make_state(tmp_path, context_builder=_context_builder(h))
    client.capabilities = capabilities_for(ACP_BACKEND_KAS)
    slot = h._make_slot("chat-1")
    slot._trusted_patterns.add(bht.grant_pattern(bht.trust_key(host, "act")))
    h._set_stream(client, [_browser(h, "req-1", _params("js", **_JS)), h._complete_event()])
    _page(_PAGE)
    await _run_answered(h, run_chat, state, slot, asyncio.ensure_future(asyncio.sleep(0)))
    client.approve_tool.assert_any_call("req-1")


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
        pytest.param(lambda: _state(_trust=True), id="trust-all-tools"),
        pytest.param(lambda: _state(yolo=True), id="yolo"),
    ],
)
async def test_session_trust_runs_js_whose_host_note_names_the_page_it_left(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, state: Any
) -> None:
    # The permission path's site-grant rung approves this call on app.example and
    # notes the host before session-wide trust is read; the tab moves on since.
    await _rung_approves_js_on(tmp_path, "app.example")
    built = _with_grant(state(), "app.example", "act")
    answer, sup = await _call(monkeypatch, built, tab_url="https://other.example/")
    assert built.opened == [] and answer.get("code") != bht.SCOPE_CHANGED_CODE
    assert _ran(sup, "js")
    # The note is spent, so a later identical call never inherits it.
    assert bht.take_host_approval(bht.browser_session_key(_SESSION), "js", _JS) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        pytest.param(lambda: _state(_trust=True), id="trust-all-tools"),
        pytest.param(lambda: _state(yolo=True), id="yolo"),
    ],
)
async def test_session_trust_keeps_the_host_check_of_a_card_click_s_note(
    monkeypatch: pytest.MonkeyPatch, state: Any
) -> None:
    # A person approved this js on app.example's card (a held Bot limit drew it):
    # session-wide trust never widens that host-scoped approval.
    built = _with_grant(state(), "app.example", "act")
    key = bht.trust_key("app.example", "act")
    assert bht.note_host_approval(_SESSION, key, {"op": "js", "args": _JS})
    answer, sup = await _call(monkeypatch, built, tab_url="https://other.example/")
    assert answer["code"] == bht.SCOPE_CHANGED_CODE and built.opened == []
    assert not _ran(sup, "js")
    assert bht.take_host_approval(bht.browser_session_key(_SESSION), "js", _JS) is None


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
