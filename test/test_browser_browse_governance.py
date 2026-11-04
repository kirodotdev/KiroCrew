"""``capabilities.browse``: one deny turns the whole browser off, on the gateway too.

The shim's own pre-check is pinned in ``test_mcp_browser_tool.py``. This file pins
every gateway entry point: each one refuses with ``policy_denied`` under a real
policy deny (the evaluator runs; only the boot-frozen ceiling is swapped in), and
behaves as before when the ceiling permits. Also pinned: the fail-closed degrade,
the engine's own two checks, the startup auto-install, the dashboard config field,
and the shim ending a parked wait on the code.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from kiro_crew.browser_harness import governance as gov
from kiro_crew.browser_harness import install as harness_install
from kiro_crew.browser_harness import live_hub as lh
from kiro_crew.browser_harness.supervisor import HarnessSupervisor
from kiro_crew.dashboard import handlers
from kiro_crew.dashboard.handlers import browser_ask
from kiro_crew.dashboard.handlers import browser_harness_status as status_routes
from kiro_crew.dashboard.handlers import browser_live, browser_ops
from kiro_crew.dashboard.routes import browser_live as route_slice
from kiro_crew.mcp_tools import browser as shim
from kiro_crew.testing.wait import await_until

_DENY_DOC = {
    "version": 1,
    "boot": {"fail_closed": True},
    "capabilities": {"browse": {"enabled": False}},
}


@pytest.fixture
def ceiling(monkeypatch: pytest.MonkeyPatch):
    """Install a boot-frozen ceiling; returns a setter (``None`` = ungoverned)."""
    from kiro_crew.platform import context as pc
    from kiro_crew.platform.governance import parse_policy

    def _set(doc: dict | None) -> None:
        parsed = parse_policy(doc) if doc is not None else None
        monkeypatch.setattr(pc, "current_context", lambda: SimpleNamespace(governance=parsed))

    _set(None)
    return _set


@pytest.fixture
def stops(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every ``stop_running_harness`` a refusal schedules, instead of a real stop."""
    seen: list[int] = []
    import kiro_crew.dashboard.handlers.browser_policy as policy

    monkeypatch.setattr(policy, "stop_running_harness", lambda: seen.append(1))
    return seen


@pytest.fixture(autouse=True)
def _quiet(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _floor_monkeypatch.setattr(handlers, "sel", lambda: MagicMock())
    _floor_monkeypatch.setattr(browser_ops, "_sel", lambda: MagicMock())
    _floor_monkeypatch.setattr(
        browser_live, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
    )
    import kiro_crew.sel as sel_mod

    _floor_monkeypatch.setattr(sel_mod, "sel", lambda: MagicMock())
    lh.reset_live_hub()
    yield
    lh.reset_live_hub()


# --- the decision ---------------------------------------------------------------


def test_a_policy_deny_denies_and_no_policy_permits(ceiling) -> None:
    assert gov.browse_denied() is False
    assert gov.browse_denied(gov.HOST_KEY, audit=False) is False
    ceiling(_DENY_DOC)
    assert gov.browse_denied() is True
    assert gov.browse_denied(gov.HOST_KEY, audit=False) is True


def test_an_unevaluable_ceiling_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed: a wrong permit drives a browser on a fleet that forbade it."""
    from kiro_crew.platform import context as pc

    def _broken() -> Any:
        raise RuntimeError("ceiling unreadable")

    monkeypatch.setattr(pc, "current_context", _broken)
    assert gov.browse_denied() is True
    assert gov.browse_denied(audit=False) is True


def test_a_deny_is_audited_only_where_someone_acted(monkeypatch, ceiling) -> None:
    import kiro_crew.sel as sel_mod

    rows: list[dict] = []
    monkeypatch.setattr(
        sel_mod,
        "sel",
        lambda: SimpleNamespace(log_governance_decision=lambda **kw: rows.append(kw)),
    )
    ceiling(_DENY_DOC)
    gov.browse_denied(audit=False)
    assert rows == []
    gov.browse_denied()
    assert [(r["scope"], r["outcome"]) for r in rows] == [(gov.BROWSE_SCOPE, "denied")]


# --- the engine -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_engine_does_not_start_under_a_deny(tmp_path, monkeypatch, ceiling) -> None:
    reads: list[Any] = []
    real = harness_install.install_state
    monkeypatch.setattr(harness_install, "install_state", lambda home: reads.append(home))
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    ceiling(_DENY_DOC)
    with pytest.raises(gov.BrowsingDenied):
        await sup.ensure()
    assert reads == [], "nothing is read or launched under a deny"
    monkeypatch.setattr(harness_install, "install_state", real)
    assert sup.status()["detail"] == gov.DENIED_TEXT


@pytest.mark.asyncio
async def test_the_engine_does_not_start_when_permitted_is_unchanged(tmp_path, ceiling) -> None:
    from kiro_crew.browser_harness.supervisor import HarnessNotInstalled

    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    with pytest.raises(HarnessNotInstalled):
        await sup.ensure()


@pytest.mark.asyncio
async def test_a_deny_during_the_allow_wait_does_not_finish_the_start(
    tmp_path, monkeypatch, ceiling
) -> None:
    """The user's Chrome holds the start on its Allow sheet with no deadline: a deny
    that lands meanwhile must not start the daemon once Allow is clicked."""
    from kiro_crew.browser_harness import discover
    from kiro_crew.browser_harness import supervisor as sup_mod

    monkeypatch.setattr(
        harness_install,
        "install_state",
        lambda home: SimpleNamespace(installed=True, python=tmp_path / "python"),
    )
    monkeypatch.setattr(HarnessSupervisor, "runtime_dir", lambda self: tmp_path)
    choice = discover.BrowserChoice("user_chrome", None, False, tmp_path)
    monkeypatch.setattr(HarnessSupervisor, "_choose", lambda self, state: choice)

    async def _allow_clicked_after_the_deny(self: Any, _choice: Any) -> str:
        ceiling(_DENY_DOC)
        return "ws://127.0.0.1:1/devtools/browser/x"

    async def _relay_start(self: Any, upstream: Any) -> str:
        return "http://127.0.0.1:1"

    started: list[str] = []

    async def _must_not_start(self: Any, *a: Any, **k: Any) -> Any:
        started.append("daemon")

    monkeypatch.setattr(HarnessSupervisor, "_user_browser_ws_url", _allow_clicked_after_the_deny)
    monkeypatch.setattr(sup_mod.CdpRelay, "start", _relay_start)
    monkeypatch.setattr(HarnessSupervisor, "_start_daemon", _must_not_start)
    monkeypatch.setattr(HarnessSupervisor, "_apply_credentials", _must_not_start)
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    with pytest.raises(gov.BrowsingDenied):
        await sup.ensure()
    assert started == [] and not sup.connected
    # A policy stop, not a failure: Settings shows the policy line, not an error.
    assert (sup._connection, sup._detail) == (sup_mod.DISCONNECTED, gov.DENIED_TEXT)


@pytest.mark.asyncio
async def test_a_helper_under_a_deny_stops_the_engine_and_refuses(tmp_path, ceiling) -> None:
    """Every op step and every reflex tick runs a helper: a mid-op deny ends it there."""
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    stopped: list[int] = []

    async def _stop() -> None:
        stopped.append(1)

    async def _must_not_ensure() -> Any:
        raise AssertionError("no helper runs under a deny")

    sup.stop = _stop  # type: ignore[method-assign]
    sup.ensure = _must_not_ensure  # type: ignore[method-assign]
    ceiling(_DENY_DOC)
    with pytest.raises(gov.BrowsingDenied):
        await sup.call("list_tabs", include_chrome=False)
    assert stopped == [1]


# --- the op route and the two parked polls (internal secret) ---------------------


def _internal(path: str, body: dict[str, Any]) -> Any:
    req = make_mocked_request("POST", path)
    req["internal_auth"] = True
    req._read_bytes = json.dumps(body).encode()
    return req


async def _code(resp: web.Response) -> str:
    return str(json.loads(resp.text).get("code") or "")


@pytest.mark.asyncio
async def test_the_op_route_refuses_before_the_engine_is_reached(
    monkeypatch, ceiling, stops
) -> None:
    async def _must_not_start(hub: Any, key: str) -> Any:
        raise AssertionError("the engine is not reached under a deny")

    monkeypatch.setattr(browser_ops, "_supervisor", _must_not_start)
    ceiling(_DENY_DOC)
    body = {"op": "snapshot", "args": {}, "session_key": "dashboard:chat-1"}
    resp = await browser_ops.api_browser_op(_internal("/api/browser/op", body))
    assert resp.status == 403 and await _code(resp) == gov.DENIED_CODE
    assert stops == [1]


@pytest.mark.asyncio
async def test_the_op_route_when_permitted_is_unchanged(monkeypatch, ceiling) -> None:
    async def _none(hub: Any, key: str) -> Any:
        return None

    monkeypatch.setattr(browser_ops, "_supervisor", _none)
    body = {"op": "snapshot", "args": {}, "session_key": "dashboard:chat-1"}
    resp = await browser_ops.api_browser_op(_internal("/api/browser/op", body))
    assert resp.status == 200 and await _code(resp) == "not_installed"


@pytest.mark.asyncio
async def test_a_deny_mid_op_answers_policy_denied(monkeypatch, ceiling) -> None:
    class _Sup:
        relay = MagicMock()

        def status(self) -> dict[str, Any]:
            return {"browser": None}

        async def call(self, helper: str, **kwargs: Any) -> Any:
            raise gov.BrowsingDenied()

    async def _sup(hub: Any, key: str) -> Any:
        return _Sup()

    monkeypatch.setattr(browser_ops, "_supervisor", _sup)
    body = {"op": "snapshot", "args": {}, "session_key": "dashboard:chat-1"}
    resp = await browser_ops.api_browser_op(_internal("/api/browser/op", body))
    assert await _code(resp) == gov.DENIED_CODE


class _CallerSup:
    """A supervisor whose helpers are recorded; the engine's own key never denies.

    ``deny_at`` names the helper whose arrival flips the caller's profile to deny.
    """

    def __init__(self, deny: dict[str, bool], deny_at: str = "", relay: Any = None) -> None:
        self.helpers: list[str] = []
        self.deny = deny
        self.deny_at = deny_at
        self.relay = relay

    def status(self) -> dict[str, Any]:
        return {"browser": None}

    async def call(self, helper: str, **kwargs: Any) -> Any:
        self.helpers.append(helper)
        if helper == self.deny_at:
            self.deny["on"] = True
        if helper == "current_tab":
            return {"targetId": "T1", "url": "about:blank"}
        if helper == "new_tab":
            return "T1"
        return {"ok": True}


@pytest.fixture
def caller_deny(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A deny in the profile bound to ``slack:x`` only, switched by ``state["on"]``.

    Also records every browser stop and audit row the op route writes.
    """
    state: dict[str, Any] = {"on": False, "stops": [], "audits": []}

    async def _denied(session_key: str = gov.DASHBOARD_SURFACE_KEY, *, audit: bool = True) -> bool:
        return session_key == "slack:x" and state["on"]

    import kiro_crew.dashboard.handlers.browser_policy as policy

    monkeypatch.setattr(browser_ops, "browse_denied_async", _denied)
    monkeypatch.setattr(policy, "browse_denied_async", _denied)
    monkeypatch.setattr(browser_ops, "stop_running_harness", lambda: state["stops"].append(1))
    monkeypatch.setattr(policy, "stop_running_harness", lambda: state["stops"].append(1))
    monkeypatch.setattr(
        browser_ops, "_audit", lambda request, outcome, error="": state["audits"].append(outcome)
    )
    return state


async def _post_op(monkeypatch: pytest.MonkeyPatch, sup: _CallerSup) -> web.Response:
    async def _sup(hub: Any, key: str) -> Any:
        return sup

    monkeypatch.setattr(browser_ops, "_supervisor", _sup)
    body = {"op": "snapshot", "args": {}, "session_key": "slack:x"}
    return await browser_ops.api_browser_op(_internal("/api/browser/op", body))


@pytest.mark.asyncio
async def test_a_caller_deny_while_the_op_waited_for_the_lock_runs_no_helper(
    monkeypatch, caller_deny
) -> None:
    """The route's first check passed; the caller's own profile denies by the time the
    op holds the lock. Nothing in the browser runs, it stops, and the answer says why."""
    sup = _CallerSup(caller_deny)

    class _LockTakenAfterTheDeny:
        """The op's wait for the lock, during which the caller's profile denies."""

        async def acquire(self) -> bool:
            caller_deny["on"] = True
            return True

        def release(self) -> None:
            return None

    monkeypatch.setattr(browser_ops, "_OP_LOCK", _LockTakenAfterTheDeny())
    resp = await _post_op(monkeypatch, sup)
    assert await _code(resp) == gov.DENIED_CODE
    assert sup.helpers == [], "no helper runs under the caller's deny"
    assert caller_deny["stops"] and "allowed" not in caller_deny["audits"]


@pytest.mark.asyncio
async def test_a_caller_deny_mid_op_stops_it_at_its_next_helper(monkeypatch, caller_deny) -> None:
    """Each helper re-asks the caller's own profile, not only the engine's empty key."""
    sup = _CallerSup(caller_deny, deny_at="snapshot")

    async def _two_steps(op: str, args: Any, ctx: Any) -> Any:
        await ctx.helper("snapshot")
        await ctx.helper("click")
        return {"ok": True}

    monkeypatch.setattr(browser_ops.harness_ops, "run_op", _two_steps)
    resp = await _post_op(monkeypatch, sup)
    assert await _code(resp) == gov.DENIED_CODE
    assert "snapshot" in sup.helpers and "click" not in sup.helpers
    assert caller_deny["stops"], "the browser stops on the caller's deny too"
    assert "allowed" not in caller_deny["audits"]


@pytest.mark.asyncio
async def test_a_caller_deny_after_the_last_helper_is_not_answered_as_success(
    monkeypatch, caller_deny
) -> None:
    """A deny that lands after the op's own helpers: the follow's helper is refused
    and the answer is ``policy_denied``, with no ``allowed`` row."""
    sup = _CallerSup(caller_deny, relay=MagicMock())

    async def _done_then_deny(op: str, args: Any, ctx: Any) -> Any:
        caller_deny["on"] = True
        return {"ok": True}

    monkeypatch.setattr(browser_ops.harness_ops, "run_op", _done_then_deny)
    resp = await _post_op(monkeypatch, sup)
    assert await _code(resp) == gov.DENIED_CODE
    assert sup.helpers == ["current_tab", "new_tab"], "the follow's current_tab never ran"
    assert "allowed" not in caller_deny["audits"]


@pytest.mark.asyncio
async def test_a_frame_send_under_a_caller_deny_never_reaches_the_relay(caller_deny) -> None:
    sent: list[str] = []

    class _Relay:
        async def send(self, method: str, params: dict[str, Any], sid: str) -> Any:
            sent.append(method)
            return {}

    relay = browser_ops._GatedRelay(_Relay(), "slack:x")
    await relay.send("Runtime.evaluate", {}, "S1")
    caller_deny["on"] = True
    with pytest.raises(gov.BrowsingDenied):
        await relay.send("Runtime.evaluate", {}, "S1")
    assert sent == ["Runtime.evaluate"] and caller_deny["stops"]


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", [True, False])
async def test_an_op_whose_start_another_caller_stopped_says_why(
    denied: bool, monkeypatch, ceiling
) -> None:
    """A refused route cancels the start this op waits on (Allow sheet): the op names
    the policy and ends the card's waiting_allow; a plain Disconnect is browser_error."""
    import asyncio
    import contextlib

    from kiro_crew.browser_harness import supervisor as sup_mod

    stopped = asyncio.ensure_future(asyncio.Event().wait())
    stopped.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await stopped

    class _Sup:
        def ensure_in_background(self) -> Any:
            return stopped

    monkeypatch.setattr(browser_ops.harness_ops, "browsing_available", lambda home: True)
    monkeypatch.setattr(sup_mod, "get_supervisor", lambda home: _Sup())
    hub = lh.get_live_hub()
    hub.turn_started("chat-1", "t1")
    hub.set_state("chat-1", lh.STATE_WAITING_ALLOW)
    if denied:
        ceiling(_DENY_DOC)
    body = {"op": "snapshot", "args": {}, "session_key": "dashboard:chat-1"}
    monkeypatch.setattr(browser_ops, "browse_refusal", _never_refused)
    resp = await browser_ops.api_browser_op(_internal("/api/browser/op", body))
    if denied:
        assert resp.status == 403 and await _code(resp) == gov.DENIED_CODE
    else:
        assert await _code(resp) == "browser_error"
    assert hub.get("chat-1").state == lh.STATE_ERROR, "the card stops asking for Allow"


@pytest.mark.asyncio
async def test_settings_connect_reports_a_start_another_caller_stopped(tmp_path) -> None:
    """A policy stop cancels the start Settings' Connect waits on: Connect answers the
    status instead of failing the request."""
    import asyncio

    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {"choice": "headless"})
    entered = asyncio.Event()

    async def _waiting_on_allow() -> Any:
        entered.set()
        await asyncio.Event().wait()

    sup.ensure = _waiting_on_allow  # type: ignore[method-assign]
    connect = asyncio.ensure_future(sup.test(3600))
    await await_until(entered, what="the start Connect waits on")
    await await_until(sup.stop_soon(), what="the policy stop")
    report = await await_until(connect, what="Connect's answer")
    assert report["connection"] == "disconnected"


@pytest.mark.asyncio
async def test_a_quick_ask_posted_as_the_deny_lands_is_refused(monkeypatch, ceiling) -> None:
    from kiro_crew.dashboard.handlers import browser_ask as ask_mod

    class _Sup:
        relay = MagicMock()

        def status(self) -> dict[str, Any]:
            return {"browser": None}

        async def call(self, helper: str, **kwargs: Any) -> Any:
            return {"targetId": "T1", "url": "https://u", "title": "t"}

    async def _sup(hub: Any, key: str) -> Any:
        return _Sup()

    async def _no_stream(*_a: Any, **_k: Any) -> None:
        return None

    async def _ask_put_on_the_card(hub: Any, key: str, args: Any) -> dict[str, Any]:
        ceiling(_DENY_DOC)
        return {"ok": True, "ask_id": "a1"}

    stopped: list[int] = []
    monkeypatch.setattr(browser_ops, "_supervisor", _sup)
    monkeypatch.setattr(browser_ops, "follow_current_tab", _no_stream)
    monkeypatch.setattr(ask_mod, "run_ask", _ask_put_on_the_card)
    monkeypatch.setattr(browser_ops, "stop_running_harness", lambda: stopped.append(1))
    body = {"op": "ask", "args": {}, "session_key": "dashboard:chat-1"}
    resp = await browser_ops.api_browser_op(_internal("/api/browser/op", body))
    assert await _code(resp) == gov.DENIED_CODE and stopped == [1]


async def _never_refused(**_kw: Any) -> None:
    """The route's own pre-check passed: the deny lands while the start is parked."""
    return None


@pytest.mark.asyncio
async def test_an_op_that_caught_the_refusal_itself_still_answers_policy_denied(
    monkeypatch, ceiling
) -> None:
    """A reflex loop reads a refused helper as an unreadable page and stops on its own
    code; the route still says the policy stopped it, so the agent does not retry."""

    class _Sup:
        relay = MagicMock()

        def status(self) -> dict[str, Any]:
            return {"browser": None}

        async def call(self, helper: str, **kwargs: Any) -> Any:
            return {"targetId": "T1", "url": "https://u", "title": "t"}

    async def _sup(hub: Any, key: str) -> Any:
        return _Sup()

    async def _loop_stopped_on_its_own(op: str, args: Any, ctx: Any) -> dict[str, Any]:
        ceiling(_DENY_DOC)
        return {"ok": False, "code": "stopped", "error": "page state unreadable"}

    stopped: list[int] = []
    monkeypatch.setattr(browser_ops, "_supervisor", _sup)
    monkeypatch.setattr(browser_ops.harness_ops, "run_op", _loop_stopped_on_its_own)
    monkeypatch.setattr(browser_ops, "stop_running_harness", lambda: stopped.append(1))
    body = {"op": "reflex", "args": {}, "session_key": "dashboard:chat-1"}
    resp = await browser_ops.api_browser_op(_internal("/api/browser/op", body))
    assert await _code(resp) == gov.DENIED_CODE
    assert stopped == [1], "the browser stops with the refusal"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "path", "body"),
    [
        (browser_ops.api_browser_handback, "/api/browser/handback", {}),
        (browser_ask.api_browser_ask_poll, "/api/browser/ask/poll", {"ask_id": "a1"}),
    ],
)
async def test_a_parked_poll_ends_under_a_deny(handler, path, body, ceiling, stops) -> None:
    payload = {"session_key": "dashboard:chat-1", **body}
    allowed = await handler(_internal(path, payload))
    assert await _code(allowed) != gov.DENIED_CODE
    ceiling(_DENY_DOC)
    resp = await handler(_internal(path, payload))
    assert resp.status == 403 and await _code(resp) == gov.DENIED_CODE


# --- the live card's owner routes ------------------------------------------------


def _owner_app() -> web.Application:
    @web.middleware
    async def owner(request: web.Request, handler: Any) -> Any:
        request["user"], request["app"] = "local-app", ""
        return await handler(request)

    app = web.Application(middlewares=[owner])
    app["state"] = SimpleNamespace(
        owner_id="",
        _slots={"chat-1": object()},
        deliver_ws_owners=lambda *_a, **_k: None,
        deliver_ws_owners_bytes=lambda *_a, **_k: None,
    )
    route_slice.register(app)
    return app


GATED_POSTS = (
    "/api/browser/live/visibility",
    "/api/browser/live/ack",
    "/api/browser/live/open-in-browser",
    "/api/browser/live/reopen",
    "/api/browser/input/claim",
    "/api/browser/input/event",
    "/api/browser/ask/answer",
)
GATED_GETS = (
    "/api/browser/live/chat-1/recordings",
    "/api/browser/live/chat-1/recordings/t1",
    "/api/browser/live/chat-1/recordings/t1/frames/0.jpg",
)
#: Each only ends something, so a deny must not trap the person in it.
UNGATED_POSTS = ("/api/browser/input/release", "/api/browser/live/close")


async def _ask(client: TestClient, path: str) -> tuple[int, str]:
    if path in GATED_GETS:
        resp = await client.get(path)
    else:
        resp = await client.post(path, json={"session_key": "chat-1"})
    try:
        body = await resp.json()
    except Exception:  # noqa: BLE001 - a JPEG or a bare 404 has no code
        body = {}
    return resp.status, str(body.get("code") or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", GATED_POSTS + GATED_GETS)
async def test_every_card_route_refuses_under_a_deny(path: str, ceiling, stops) -> None:
    async with TestClient(TestServer(_owner_app())) as client:
        status, code = await _ask(client, path)
        assert code != gov.DENIED_CODE, "permitted: the route answers as before"
        ceiling(_DENY_DOC)
        status, code = await _ask(client, path)
    assert status == 403 and code == gov.DENIED_CODE
    assert stops == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", UNGATED_POSTS)
async def test_handing_back_and_folding_the_card_still_work_under_a_deny(
    path: str, ceiling, stops
) -> None:
    ceiling(_DENY_DOC)
    async with TestClient(TestServer(_owner_app())) as client:
        status, code = await _ask(client, path)
    assert code != gov.DENIED_CODE and status == 200
    assert stops == []


# --- Settings -> Browser and the startup auto-install ----------------------------


class _Req:
    """The owner's request, as ``test_browser_harness_routes`` builds it."""

    def __init__(self, path: str, sup: HarnessSupervisor) -> None:
        self.path = path
        self._claims: dict[str, Any] = {"user": "the-owner", "app": ""}
        self.app: dict[str, Any] = {
            "state": SimpleNamespace(owner_id="the-owner"),
            "browser_harness_supervisor": sup,
        }
        self.can_read_body = False

    def get(self, key: str, default: Any = None) -> Any:
        return self._claims.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self._claims

    def __getitem__(self, key: str) -> Any:
        return self._claims[key]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [
        status_routes.api_browser_harness_status,
        status_routes.api_browser_harness_install,
        status_routes.api_browser_harness_test,
    ],
)
async def test_settings_install_and_connect_refuse_under_a_deny(
    handler, tmp_path, monkeypatch, ceiling, stops
) -> None:
    started: list[Any] = []
    monkeypatch.setattr(status_routes, "_start_install_job", lambda *a, **k: started.append(a))
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {"choice": "headless"})
    ceiling(_DENY_DOC)
    resp = await handler(_Req("/api/browser/x", sup))
    assert resp.status == 403 and await _code(resp) == gov.DENIED_CODE
    assert started == []


@pytest.mark.asyncio
async def test_settings_status_when_permitted_is_unchanged(tmp_path, ceiling) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {"choice": "headless"})
    resp = await status_routes.api_browser_harness_status(_Req("/api/browser/status", sup))
    assert resp.status == 200 and json.loads(resp.text)["installed"] is False


@pytest.mark.asyncio
async def test_disconnect_still_stops_the_engine_under_a_deny(tmp_path, ceiling) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {"choice": "headless"})
    ceiling(_DENY_DOC)
    resp = await status_routes.api_browser_harness_disconnect(_Req("/api/browser/disconnect", sup))
    assert resp.status == 200


@pytest.mark.asyncio
async def test_the_startup_auto_install_does_nothing_under_a_deny(
    tmp_path, monkeypatch, ceiling
) -> None:
    started: list[Any] = []
    monkeypatch.setattr(status_routes, "_start_install_job", lambda *a, **k: started.append(a))
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    state = SimpleNamespace()
    ceiling(_DENY_DOC)
    outcome = await status_routes.auto_setup_browser_harness(state, supervisor=sup, environ={})
    assert outcome == status_routes.AUTO_POLICY_DENIED and started == []


# --- the dashboard's read, and the shim ------------------------------------------


@pytest.mark.asyncio
async def test_the_dashboard_config_reports_browse_enabled(ceiling) -> None:
    from kiro_crew.dashboard.handlers.files import api_dashboard_config

    resp = await api_dashboard_config(make_mocked_request("GET", "/api/dashboard/config"))
    assert json.loads(resp.text)["browse_enabled"] is True
    ceiling(_DENY_DOC)
    resp = await api_dashboard_config(make_mocked_request("GET", "/api/dashboard/config"))
    assert json.loads(resp.text)["browse_enabled"] is False


def test_the_tool_reads_the_gateway_refusal_as_final() -> None:
    denied = {"error": gov.DENIED_TEXT, "code": gov.DENIED_CODE}
    assert shim._settle("click", 403, denied) == f"Error: {gov.DENIED_TEXT}"
    assert shim._settle("click", 200, {"ok": False, **denied}) == f"Error: {gov.DENIED_TEXT}"


def test_a_parked_tool_stops_waiting_on_a_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    denied = (403, {"error": gov.DENIED_TEXT, "code": gov.DENIED_CODE})
    monkeypatch.setattr(shim, "_post_handback", lambda *a, **k: denied)
    monkeypatch.setattr(shim, "_post_ask_poll", lambda *a, **k: denied)
    monkeypatch.setattr(shim.mcp_core, "_post", lambda *a, **k: {})
    held = shim._HandbackWait(
        op="click",
        op_args={},
        body_key="k",
        session_header="k",
        timeout_s=1.0,
        interrupted=False,
        interrupted_text="",
    )
    asked = shim._AskWait(ask_id="a1", body_key="k", session_header="k")
    assert held.step() == f"Error: {gov.DENIED_TEXT}"
    assert asked.step() == f"Error: {gov.DENIED_TEXT}"


@pytest.mark.asyncio
async def test_a_refusal_stops_a_browser_that_is_still_running(monkeypatch) -> None:
    """A policy pushed mid-session: the next refused route stops the running engine."""
    import asyncio

    from kiro_crew.browser_harness import supervisor as sup_mod

    stopped = asyncio.Event()
    release = asyncio.Event()
    stops: list[int] = []
    sup = HarnessSupervisor(Path("unused"), config_reader=lambda: {})

    async def _stop() -> None:
        stops.append(1)
        stopped.set()
        await release.wait()

    sup.stop = _stop  # type: ignore[method-assign]
    # A handle whose daemon exited still holds a browser with pages in it: not
    # ``connected``, and still stopped.
    sup._handle = MagicMock()
    assert not sup.connected
    monkeypatch.setattr(sup_mod, "get_supervisor", lambda _home: sup)
    gov.stop_running_harness()
    await await_until(stopped, what="the running engine's stop")
    # A second refusal while the first stop runs reuses it: a second stop() would
    # cancel the start the first one is still tearing down.
    gov.stop_running_harness()
    release.set()
    await await_until(sup.stop_soon(), what="the one policy stop")
    assert stops == [1]


@pytest.mark.asyncio
async def test_a_refusal_stops_a_start_still_waiting_on_allow(tmp_path, monkeypatch) -> None:
    """A surface-bound deny the engine's own host check does not see: the start parked
    on Chrome's Allow sheet is cancelled by the refused route, not left to finish."""
    import asyncio

    from kiro_crew.browser_harness import supervisor as sup_mod

    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    allow = asyncio.Event()

    async def _waiting_on_allow() -> Any:
        await allow.wait()

    sup.ensure = _waiting_on_allow  # type: ignore[method-assign]
    pending = sup.ensure_in_background()
    assert sup.holds_browser and not sup.connected
    monkeypatch.setattr(sup_mod, "get_supervisor", lambda _home: sup)
    gov.stop_running_harness()
    with pytest.raises(asyncio.CancelledError):
        await await_until(pending, what="the parked start's cancellation")
    assert not sup.holds_browser
