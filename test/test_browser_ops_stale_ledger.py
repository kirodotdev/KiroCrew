"""The op route's bind when the relay's tab ledger and the browser disagree.

The ledger (``CdpRelay.agent_targets``) drops a tab on ``Target.targetDestroyed``.
Pinned here: a close that event never reported does not leave the chat failing
every op with Harness's ``No target with given id found`` until a restart. The
bind's failed ``switch_tab`` asks the browser itself (``Target.getTargets``), drops
a tab the browser does not list and binds the way an observed close does; a switch
that fails for any other reason, or when the browser cannot be asked, stays the
op's error and keeps the binding. The fakes are ``test_browser_ops_route``'s.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from test_browser_ops_route import _ClosedTabSupervisor, _install, _no_stream, _op

from kiro_crew.browser_harness import live_hub as hub_mod
from kiro_crew.browser_harness.relay import CdpError, RelayClosed
from kiro_crew.dashboard.handlers import browser_ops as mod


@pytest.fixture(autouse=True)
def _quiet(_floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    _floor_monkeypatch.setattr(mod, "_sel", lambda: MagicMock())
    hub_mod.reset_live_hub()
    yield
    hub_mod.reset_live_hub()


class _StaleLedgerSupervisor(_ClosedTabSupervisor):
    """A browser whose tabs and the relay's ledger can disagree.

    ``browser_tabs`` is what the browser has; ``ledger`` is what the relay believes.
    A ``switch_tab`` to a tab the browser lost fails as Harness does, and
    ``Target.getTargets`` through the relay answers the browser's own list. With
    ``switch_fails`` set, every ``switch_tab`` fails with that message instead.
    """

    def __init__(self) -> None:
        super().__init__()
        self.browser_tabs = ["T1"]
        self.switch_fails: str | None = None
        self.probes = 0
        self.relay.send = self._send
        self.relay.forget_target.side_effect = self._forget

    def _forget(self, target: str) -> None:
        if target in self.ledger:
            self.ledger.remove(target)

    async def _send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert method == "Target.getTargets" and params == {}
        self.probes += 1
        return {"targetInfos": [{"targetId": t, "type": "page"} for t in self.browser_tabs]}

    async def call(self, helper: str, **kwargs: Any) -> Any:
        if helper == "switch_tab" and (
            self.switch_fails or kwargs["target"] not in self.browser_tabs
        ):
            self.calls.append((helper, kwargs))
            raise mod.HelperError(
                self.switch_fails
                or "RuntimeError: {'code': -32602, 'message': 'No target with given id found'}"
            )
        if helper == "current_tab" and self.current not in self.browser_tabs:
            self.calls.append((helper, kwargs))
            raise mod.HelperError("RuntimeError: cdp_disconnected")
        if helper == "list_tabs":
            self.calls.append((helper, kwargs))
            return [
                {"target_id": t, "url": f"https://{t}", "title": "t"} for t in self.browser_tabs
            ]
        got = await super().call(helper, **kwargs)
        if helper == "new_tab":
            self.browser_tabs.append(got)
        return got


@pytest.mark.asyncio
async def test_a_tab_the_browser_lost_without_the_ledger_hearing_is_dropped_and_rebound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ledger still lists the chat's tab, the browser has closed it: the op recovers.

    ``Target.targetDestroyed`` never reached the relay, so the bind's switch to the
    tab fails. The failed switch asks the browser, drops the tab from the ledger
    and binds the way an observed close does; without that, every op of the chat
    fails the same way until a restart rebuilds the ledger.
    """
    sup = _StaleLedgerSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    assert (await _op("chat-a"))["ok"] is True
    assert hub.target_of("chat-a") == "T1"
    sup.browser_tabs.remove("T1")  # the person closed it; the relay heard nothing
    assert sup.ledger == ["T1"] and sup.probes == 0
    listed = await _op("chat-a", "tabs", action="list")
    assert listed["ok"] is True, listed
    assert sup.probes == 1 and sup.ledger == ["T-new1"], "the browser was asked once; T1 is gone"
    assert hub.target_of("chat-a") == "T-new1" and sup.current == "T-new1"
    assert [t["target_id"] for t in listed["result"]["tabs"]] == ["T-new1"]
    assert ("new_tab", {"url": "about:blank"}) in sup.calls
    sup.calls.clear()
    assert (await _op("chat-a"))["ok"] is True
    assert sup.probes == 1 and "switch_tab" not in [c[0] for c in sup.calls]
    opened = await _op("chat-a", "tabs", action="new", url="about:blank")
    assert opened["ok"] is True and opened["result"]["target_id"] == "T-new2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("browser_has_it", "probe_breaks"),
    [
        (True, None),
        (False, RelayClosed("relay closed")),
        (False, CdpError("Target.getTargets", {"message": "refused"})),
    ],
    ids=["the browser still lists the tab", "the relay hung up", "the browser refused the ask"],
)
async def test_a_switch_that_fails_for_another_reason_stays_an_error_and_keeps_the_binding(
    monkeypatch: pytest.MonkeyPatch, browser_has_it: bool, probe_breaks: Exception | None
) -> None:
    """Only a tab the browser says is gone is dropped; every other failure surfaces as is."""
    sup = _StaleLedgerSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    await _op("chat-b")  # the daemon is on B's tab, so A's next bind must switch
    assert sup.current == "T-new1"
    sup.switch_fails = "RuntimeError: switch timed out"
    if not browser_has_it:
        sup.browser_tabs.remove("T1")
    if probe_breaks is not None:

        async def _broken(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            sup.probes += 1
            raise probe_breaks

        sup.relay.send = _broken
    sup.calls.clear()
    answer = await _op("chat-a")
    assert answer["ok"] is False and answer["code"] == "browser_error"
    assert "switch timed out" in answer["error"]
    assert sup.probes == 1
    assert hub.target_of("chat-a") == "T1" and sup.ledger == ["T1", "T-new1"], "binding kept"
    assert "new_tab" not in [c[0] for c in sup.calls]


@pytest.mark.asyncio
async def test_an_observed_close_rebinds_without_asking_the_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tab the ledger already dropped takes the same path with no probe and no switch."""
    sup = _StaleLedgerSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    sup.browser_tabs.remove("T1")
    sup.ledger.remove("T1")  # the browser reported T1 destroyed
    sup.calls.clear()
    assert (await _op("chat-a"))["ok"] is True
    assert sup.probes == 0 and "switch_tab" not in [c[0] for c in sup.calls]
    assert hub.target_of("chat-a") == "T-new1"


@pytest.mark.asyncio
async def test_a_stale_popup_returns_the_chat_to_its_opener_like_an_observed_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lost tab is a popup the chat followed: the bind walks back to the opener.

    A sign-in window the chat moved onto closed without the relay hearing it. The
    dropped tab takes the observed-close path, so the chat returns to the page
    that opened the popup and the answer reports the move, not a blank new tab.
    """
    sup = _StaleLedgerSupervisor()
    openers = {"P1": "T1"}
    sup.relay.opener_of.side_effect = lambda t: openers.get(t, "")
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    sup.ledger.append("P1")
    sup.browser_tabs.append("P1")
    assert (await _op("chat-a"))["ok"] is True, "the op follows the popup"
    assert hub.target_of("chat-a") == "P1" and sup.current == "P1"
    sup.browser_tabs.remove("P1")  # the window closed itself; no targetDestroyed arrived
    sup.calls.clear()
    answer = await _op("chat-a")
    assert answer["ok"] is True, answer
    closed = answer["result"]["popup_closed"]
    assert closed["target_id"] == "P1" and closed["returned_to"] == "T1"
    assert hub.target_of("chat-a") == "T1" and sup.ledger == ["T1"] and sup.probes == 1
    assert "new_tab" not in [c[0] for c in sup.calls]
