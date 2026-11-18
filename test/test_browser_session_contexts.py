"""``browser.isolation: per_session``: each chat browses in its own browser context.

Driven through ``browser_ops`` with a real ``CdpRelay`` whose upstream send is
recorded and answered here (a new context id per ``Target.createBrowserContext``),
and a daemon stand-in whose ``new_tab`` reads the relay's create context the way
the relay's ``Target.createTarget`` rewrite does. Pinned: two chats get two
contexts, a chat keeps its own on later ops and is seeded once, the person's tab
is never adopted, a flip of the setting either way neither lists nor keeps a tab
from the other side, an unreadable setting refuses the op, the route leaves no
create context behind, an ended chat's context is disposed, and the default
(``shared``) binds exactly as before.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.browser_harness import live_hub
from kiro_crew.browser_harness import ops as harness_ops
from kiro_crew.browser_harness import session_contexts
from kiro_crew.browser_harness.relay import CdpRelay
from kiro_crew.browser_harness.supervisor import BrowserRestarted
from kiro_crew.config import loader as config_loader
from kiro_crew.dashboard.handlers import browser_live, browser_ops

pytestmark = pytest.mark.asyncio


class _Daemon:
    """The gateway's supervisor as ``browser_ops`` uses it, over a recorded relay."""

    def __init__(self, relay: CdpRelay) -> None:
        self.relay = relay
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.opened: list[tuple[str, str]] = []
        self.seeded: list[str] = []
        self.current = "T-person"
        self.ensured = 0
        relay._agent_targets = ["T-person"]

    async def ensure(self) -> Any:
        self.ensured += 1
        return SimpleNamespace(relay=self.relay)

    def status(self) -> dict[str, Any]:
        return {"browser": {"kind": "kiro_chromium", "headless": True}, "connection": "connected"}

    async def seed_context(self, relay: CdpRelay, context: str) -> None:
        assert relay is self.relay
        self.seeded.append(context)

    async def call(self, helper: str, **kwargs: Any) -> Any:
        # The pin ``HarnessSupervisor.call`` honours: a helper of an isolated op
        # bound on another relay is refused.
        relay = kwargs.pop("relay", None)
        if relay is not None and relay is not self.relay:
            raise BrowserRestarted("the browser restarted during the op; its context is gone")
        self.calls.append((helper, kwargs))
        if helper == "current_tab":
            return {"targetId": self.current, "url": "about:blank", "title": ""}
        if helper == "new_tab":
            target = f"T{len(self.opened) + 1}"
            self.opened.append((target, self.relay.create_context))
            await self.relay._record_agent_target(target, self.relay.create_context)
            self.current = target
            return target
        if helper == "list_tabs":
            return [
                {"targetId": t, "url": "about:blank", "title": ""}
                for t in self.relay.agent_targets()
            ]
        if helper == "switch_tab":
            self.current = kwargs["target"]
        if helper in ("js", "js_isolated"):
            return {"url": "u", "title": "t", "nodes": []}
        return {}


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[_Daemon, list[Any]]]:
    live_hub.reset_live_hub()
    relay = CdpRelay()
    sent: list[Any] = []
    created = 0

    async def send(method: str, params: Any = None, session_id: Any = None, **_kw: Any) -> Any:
        nonlocal created
        sent.append((method, params))
        if method == "Target.createBrowserContext":
            assert params == {"disposeOnDetach": True}
            created += 1
            return {"browserContextId": f"CTX-{created}"}
        return {}

    monkeypatch.setattr(relay, "send", send)
    monkeypatch.setattr(browser_live, "_current_relay", lambda: relay)
    yield _Daemon(relay), sent
    live_hub.reset_live_hub()


@pytest.fixture
def per_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_contexts, "per_session", lambda: True)


def _sent_recorder(sent: list[Any]) -> Any:
    async def send(method: str, params: Any = None, session_id: Any = None, **_kw: Any) -> Any:
        sent.append((method, params))
        if method == "Target.createBrowserContext":
            return {"browserContextId": f"CTX-fresh-{len(sent)}"}
        return {}

    return send


def _contexts_created(sent: list[Any]) -> int:
    return sum(method == "Target.createBrowserContext" for method, _params in sent)


async def test_each_chat_browses_in_a_context_of_its_own(
    daemon: tuple[_Daemon, list[Any]], per_session: None
) -> None:
    sup, sent = daemon
    hub = live_hub.get_live_hub()
    assert await browser_ops._bind_target(hub, "chat-a", sup) == "T1"
    assert await browser_ops._bind_target(hub, "chat-b", sup) == "T2"
    # The person's tab was current, ledgered and unowned: never adopted.
    assert hub.owner_of_target("T-person") is None
    assert sup.opened == [("T1", "CTX-1"), ("T2", "CTX-2")]
    assert sup.seeded == ["CTX-1", "CTX-2"]
    # Chat A's next op goes back to its own tab, in the context it already has.
    assert await browser_ops._bind_target(hub, "chat-a", sup) == "T1"
    assert sup.calls[-1] == ("switch_tab", {"target": "T1", "activate": False})
    assert sup.relay.create_context == "CTX-1"
    assert _contexts_created(sent) == 2 and sup.seeded == ["CTX-1", "CTX-2"]


async def test_a_dead_daemon_is_restarted_before_the_context_is_made(
    daemon: tuple[_Daemon, list[Any]], per_session: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A daemon that exited while idle leaves a stale handle: the bind must create
    # the context on the relay the restart gives, not on the one about to be torn
    # down, or the first tab would open in the shared default context.
    sup, _sent = daemon
    stale = sup.relay
    fresh = CdpRelay()
    fresh._agent_targets = []

    monkeypatch.setattr(fresh, "send", _sent_recorder([]))

    async def ensure() -> Any:
        sup.relay = fresh
        return SimpleNamespace(relay=fresh)

    sup.ensure = ensure  # type: ignore[method-assign]
    hub = live_hub.get_live_hub()
    assert await browser_ops._bind_target(hub, "chat-a", sup) == "T1"
    assert fresh.session_context_ids() == ["CTX-fresh-1"] and not stale.session_context_ids()
    assert sup.opened == [("T1", "CTX-fresh-1")]


async def test_a_browser_restarted_during_the_bind_refuses_the_op(
    daemon: tuple[_Daemon, list[Any]], per_session: None
) -> None:
    sup, _sent = daemon
    bound = sup.relay
    hub = live_hub.get_live_hub()
    ctx = browser_ops.build_context("chat-a", sup, hub, caller_key="chat-a")
    pinned = ctx.helper

    async def call(helper: str, **kwargs: Any) -> Any:
        if helper == "new_tab":
            sup.relay = CdpRelay()  # the daemon died: its helper came back on a new relay
        return await pinned(helper, **kwargs)

    with pytest.raises(BrowserRestarted):
        await browser_ops._bind_target(hub, "chat-a", sup, call)
    assert sup.opened == []
    assert bound.session_context_ids() == []  # the half-made context is not kept


async def test_a_browser_restarted_between_the_bind_and_a_step_refuses_the_op(
    daemon: tuple[_Daemon, list[Any]], per_session: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every helper of an isolated op is pinned to the relay it bound on, not only
    # the bind's: a restart before the navigate must not run it in the new
    # browser's default context, which holds the shared sign-ins.
    sup, _sent = daemon
    hub = live_hub.get_live_hub()
    ctx = browser_ops.build_context("chat-a", sup, hub, caller_key="chat-a")
    assert await browser_ops._bind_target(hub, "chat-a", sup, ctx.helper) == "T1"
    sup.relay = CdpRelay()
    with pytest.raises(BrowserRestarted):
        await ctx.helper("navigate", url="https://example.com/")
    assert all(helper != "navigate" for helper, _kw in sup.calls)
    # Through the route the refusal names its cause.
    req = make_mocked_request("POST", "/api/browser/op")
    req["internal_auth"] = True
    req._read_bytes = json.dumps(
        {
            "op": "navigate",
            "args": {"url": "https://example.com/"},
            "session_key": "dashboard:chat-a",
        }
    ).encode()

    async def _supervisor(_hub: Any, _key: str) -> Any:
        return sup

    real_bind = browser_ops._bind_target

    async def bind_then_restart(*args: Any, **kwargs: Any) -> str:
        target = await real_bind(*args, **kwargs)
        sup.relay = CdpRelay()
        return target

    sup.relay = CdpRelay()
    monkeypatch.setattr(sup.relay, "send", _sent_recorder(_sent))
    monkeypatch.setattr(browser_ops, "_supervisor", _supervisor)
    monkeypatch.setattr(browser_ops, "_sel", lambda: MagicMock())
    monkeypatch.setattr(browser_ops, "_bind_target", bind_then_restart)
    body = json.loads((await browser_ops.api_browser_op(req)).body)
    assert body["ok"] is False and body["code"] == "browser_restarted"


async def test_a_browser_restarted_before_the_follow_refuses_instead_of_streaming(
    daemon: tuple[_Daemon, list[Any]], per_session: None
) -> None:
    # An op's own steps may read a restart as a failed step (a frame check that
    # answers None) and never raise; the post-op follow must still not bind and
    # stream the new browser's default-context tab on the isolated chat's card.
    sup, _sent = daemon
    hub = live_hub.get_live_hub()
    assert await browser_ops._bind_target(hub, "chat-a", sup) == "T1"
    sup.calls.clear()
    sup.relay = CdpRelay()
    refused = await browser_ops.follow_current_tab(hub, "chat-a", sup, caller_key="chat-a")
    assert refused is not None and refused["code"] == "browser_restarted"
    assert all(helper != "current_tab" for helper, _kw in sup.calls)
    assert hub.get("chat-a").stream is None


async def test_the_follow_is_not_pinned_while_the_browser_is_shared(
    daemon: tuple[_Daemon, list[Any]],
) -> None:
    sup, _sent = daemon
    hub = live_hub.get_live_hub()
    await browser_ops._bind_target(hub, "chat-a", sup)
    sup.calls.clear()
    sup.relay = CdpRelay()  # shared mode records no pin: a restart changes nothing
    assert await browser_ops.follow_current_tab(hub, "chat-a", sup, caller_key="chat-a") is None
    assert any(helper == "current_tab" for helper, _kw in sup.calls)


async def test_a_tab_bound_while_the_browser_was_shared_is_not_kept(
    daemon: tuple[_Daemon, list[Any]], per_session: None
) -> None:
    sup, _sent = daemon
    hub = live_hub.get_live_hub()
    hub.bind_target("chat-a", "T-person")  # bound before isolation was turned on
    assert await browser_ops._bind_target(hub, "chat-a", sup) == "T1"
    assert sup.opened == [("T1", "CTX-1")]


async def test_shared_is_the_default_and_binds_as_before(
    daemon: tuple[_Daemon, list[Any]],
) -> None:
    sup, sent = daemon
    hub = live_hub.get_live_hub()
    assert session_contexts.per_session() is False
    assert await browser_ops._bind_target(hub, "chat-a", sup) == "T-person"
    assert await browser_ops._bind_target(hub, "chat-b", sup) == "T1"
    assert sup.opened == [("T1", "")]
    assert sent == [] and sup.seeded == []


async def test_the_op_route_leaves_no_create_context_behind(
    daemon: tuple[_Daemon, list[Any]], per_session: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup, _sent = daemon

    async def _supervisor(_hub: Any, _key: str) -> Any:
        return sup

    async def _no_stream(self: Any, key: str, relay: Any, target: str, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(browser_ops, "_supervisor", _supervisor)
    monkeypatch.setattr(browser_ops, "_sel", lambda: MagicMock())
    monkeypatch.setattr(live_hub.LiveHub, "start_stream", _no_stream)
    req = make_mocked_request("POST", "/api/browser/op")
    req["internal_auth"] = True
    req._read_bytes = json.dumps({"op": "snapshot", "session_key": "dashboard:chat-a"}).encode()
    body = json.loads((await browser_ops.api_browser_op(req)).body)
    assert body["ok"] is True
    assert sup.opened == [("T1", "CTX-1")]
    assert live_hub.get_live_hub().target_of("chat-a") == "T1"
    assert sup.relay.create_context == ""


async def test_an_ended_chat_s_context_is_disposed_and_the_other_kept(
    daemon: tuple[_Daemon, list[Any]], per_session: None
) -> None:
    sup, sent = daemon
    hub = live_hub.get_live_hub()
    await browser_ops._bind_target(hub, "chat-a", sup)
    await browser_ops._bind_target(hub, "chat-b", sup)
    sent.clear()
    state = MagicMock()
    state._slots = {"chat-b": object()}
    await browser_ops._close_tabs_of(state, "chat-a")
    assert sent == [
        ("Target.closeTarget", {"targetId": "T1"}),
        ("Target.disposeBrowserContext", {"browserContextId": "CTX-1"}),
    ]
    assert sup.relay.session_context_ids() == ["CTX-2"]


def _point_config_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_loader, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(config_loader, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(config_loader, "config_local_path", lambda: tmp_path / "config.local.json")


def _isolation(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(session_contexts, "per_session", lambda: value)


async def _tabs_listed(sup: _Daemon, key: str) -> list[str]:
    ctx = browser_ops.build_context(key, sup, live_hub.get_live_hub())
    answer = await harness_ops.run_op("tabs", {"action": "list"}, ctx)
    return [t["target_id"] for t in answer["tabs"]]


async def test_a_chat_that_turned_isolation_on_never_sees_its_shared_tab_again(
    daemon: tuple[_Daemon, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    sup, _sent = daemon
    hub = live_hub.get_live_hub()
    _isolation(monkeypatch, False)
    assert await browser_ops._bind_target(hub, "chat-a", sup) == "T-person"
    assert await _tabs_listed(sup, "chat-a") == ["T-person"]
    _isolation(monkeypatch, True)
    assert await browser_ops._bind_target(hub, "chat-a", sup) == "T1"
    # The shared-era tab is still the chat's and ledgered, but in the default
    # context: ``tabs list`` hides it, so ``tabs switch`` cannot go back to it.
    assert "T-person" in hub.targets_of("chat-a")
    assert await _tabs_listed(sup, "chat-a") == ["T1"]
    ctx = browser_ops.build_context("chat-a", sup, hub)
    refused = await harness_ops.run_op("tabs", {"action": "switch", "target_id": "T-person"}, ctx)
    assert refused["code"] == "not_agent_tab"
    assert await browser_ops._bind_target(hub, "chat-a", sup) == "T1"


async def test_a_chat_that_turned_isolation_off_leaves_its_context_tabs_behind(
    daemon: tuple[_Daemon, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    sup, _sent = daemon
    hub = live_hub.get_live_hub()
    _isolation(monkeypatch, True)
    assert await browser_ops._bind_target(hub, "chat-a", sup) == "T1"
    sup.relay.create_context = ""
    _isolation(monkeypatch, False)
    # T1 sits in CTX-1: not kept, not listed; the shared chat adopts the daemon's
    # current tab (its own T1 is current but in the wrong context, so a new one).
    assert await browser_ops._bind_target(hub, "chat-a", sup) == "T2"
    assert sup.opened == [("T1", "CTX-1"), ("T2", "")]
    assert await _tabs_listed(sup, "chat-a") == ["T2"]


@pytest.mark.parametrize("broken", ["config.json", "config.local.json"])
async def test_an_unreadable_setting_refuses_the_op(
    daemon: tuple[_Daemon, list[Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    broken: str,
) -> None:
    # ``KiroCrewConfig.load`` answers a file that will not parse with DEFAULTS, which
    # would read as ``shared``: the person chose isolation, so the op is refused.
    sup, _sent = daemon
    hub = live_hub.get_live_hub()
    _point_config_at(tmp_path, monkeypatch)
    monkeypatch.setattr(session_contexts, "_last_clean", None)
    (tmp_path / "config.json").write_text(
        json.dumps({"browser": {"isolation": "per_session"}}), encoding="utf-8"
    )
    assert session_contexts.per_session() is True
    (tmp_path / broken).write_text('{"browser": {"isolation": ', encoding="utf-8")
    with pytest.raises(session_contexts.IsolationUnknown):
        await browser_ops._bind_target(hub, "chat-a", sup)
    assert sup.opened == [] and hub.target_of("chat-a") == ""
    # Through the route the refusal names its cause, not a generic browser error.
    req = make_mocked_request("POST", "/api/browser/op")
    req["internal_auth"] = True
    req._read_bytes = json.dumps({"op": "snapshot", "session_key": "dashboard:chat-a"}).encode()

    async def _supervisor(_hub: Any, _key: str) -> Any:
        return sup

    monkeypatch.setattr(browser_ops, "_supervisor", _supervisor)
    monkeypatch.setattr(browser_ops, "_sel", lambda: MagicMock())
    body = json.loads((await browser_ops.api_browser_op(req)).body)
    assert body["ok"] is False and body["code"] == "isolation_unknown"


@pytest.mark.parametrize("ever_read", [False, True])
async def test_a_shared_chat_keeps_browsing_on_a_broken_config(
    daemon: tuple[_Daemon, list[Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ever_read: bool,
) -> None:
    # The default path is not changed by a broken file: nobody chose isolation, so
    # the op runs in the shared browser as it always did.
    sup, _sent = daemon
    hub = live_hub.get_live_hub()
    _point_config_at(tmp_path, monkeypatch)
    monkeypatch.setattr(session_contexts, "_last_clean", None)
    if ever_read:
        (tmp_path / "config.json").write_text("{}", encoding="utf-8")
        assert session_contexts.per_session() is False
    (tmp_path / "config.json").write_text('{"browser": {"isolation": ', encoding="utf-8")
    assert session_contexts.per_session() is False
    assert await browser_ops._bind_target(hub, "chat-a", sup) == "T-person"
    assert _contexts_created(_sent) == 0


async def test_per_session_is_read_from_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.json"
    _point_config_at(tmp_path, monkeypatch)
    cfg.write_text(json.dumps({"browser": {"isolation": "per_session"}}), encoding="utf-8")
    assert session_contexts.per_session() is True
    cfg.write_text(json.dumps({"browser": {"isolation": "shared"}}), encoding="utf-8")
    assert session_contexts.per_session() is False


async def test_a_context_whose_first_tab_failed_is_rolled_back(
    daemon: tuple[_Daemon, list[Any]], per_session: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup, sent = daemon
    hub = live_hub.get_live_hub()
    hub.bind_target("chat-a", "T-person")  # held while the browser was shared
    real_call = sup.call

    async def failing_new_tab(helper: str, **kwargs: Any) -> Any:
        if helper == "new_tab":
            raise RuntimeError("daemon gone")
        return await real_call(helper, **kwargs)

    monkeypatch.setattr(sup, "call", failing_new_tab)
    with pytest.raises(RuntimeError):
        await browser_ops._bind_target(hub, "chat-a", sup)
    assert sup.relay.session_context_ids() == [] and sup.relay.create_context == ""
    assert ("Target.disposeBrowserContext", {"browserContextId": "CTX-1"}) in sent
    monkeypatch.setattr(sup, "call", real_call)
    # The next op starts over: a new, seeded context, never the shared-era tab.
    assert await browser_ops._bind_target(hub, "chat-a", sup) == "T1"
    assert sup.seeded == ["CTX-1", "CTX-2"] and sup.opened[-1] == ("T1", "CTX-2")


async def test_a_reconnected_relay_gives_the_chat_a_fresh_context(
    daemon: tuple[_Daemon, list[Any]], per_session: None
) -> None:
    sup, _sent = daemon
    hub = live_hub.get_live_hub()
    await browser_ops._bind_target(hub, "chat-a", sup)
    sup.relay._session_contexts.clear()  # what a new relay starts with
    assert await browser_ops._bind_target(hub, "chat-a", sup) == "T2"
    assert sup.opened == [("T1", "CTX-1"), ("T2", "CTX-2")]
    assert len(sup.seeded) == 2


async def test_a_failed_tab_close_still_disposes_the_context(
    daemon: tuple[_Daemon, list[Any]], per_session: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup, sent = daemon
    hub = live_hub.get_live_hub()
    await browser_ops._bind_target(hub, "chat-a", sup)

    async def boom(_targets: Any = None) -> None:
        raise RuntimeError("relay hiccup")

    monkeypatch.setattr(sup.relay, "close_agent_targets", boom)
    state = MagicMock()
    state._slots = {}
    await browser_ops._close_tabs_of(state, "chat-a")
    assert sent[-1] == ("Target.disposeBrowserContext", {"browserContextId": "CTX-1"})
    assert sup.relay.session_context_ids() == []
