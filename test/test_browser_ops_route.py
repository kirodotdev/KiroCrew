"""``POST /api/browser/op``: the ``browser`` tool's forwarding leg.

A MACHINE-only strict route reached over the AF_UNIX internal-API socket, where
``request.remote`` is empty. It gates on ``request["internal_auth"] is True``
ALONE (mirroring ``api_computer_use_invoke``); an added loopback re-assert would
403 every unix-socket op. Pinned here: the auth gate, body validation, the
``not_installed`` answer, the result pass-through, and how a timeout or a raised
helper is turned into a code the shim can render. And the live card's glue: the
first op of a turn opens the hub's turn, every op is a step, and after an op the
stream follows the daemon's current tab.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.browser_harness import live_hub as hub_mod
from kiro_crew.browser_harness import ops as harness_ops
from kiro_crew.dashboard.handlers import browser_ops as mod


@pytest.fixture(autouse=True)
def _quiet_sel(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(mod, "_sel", lambda: MagicMock())


@pytest.fixture(autouse=True)
def _fresh_hub() -> Any:
    hub_mod.reset_live_hub()
    yield
    hub_mod.reset_live_hub()


def _request(body: dict[str, Any] | None, *, internal: bool = True) -> Any:
    req = make_mocked_request("POST", "/api/browser/op")
    if internal:
        req["internal_auth"] = True
    if body is not None:
        req._read_bytes = json.dumps(body).encode()
    return req


class _Supervisor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.relay = MagicMock()
        self.relay.agent_targets.return_value = ["T1"]
        self.tab = {"targetId": "T1", "url": "https://u", "title": "t"}

    def status(self) -> dict[str, Any]:
        return {"browser": {"kind": "kiro_chromium", "headless": True}, "connection": "connected"}

    async def call(self, helper: str, **kwargs: Any) -> Any:
        self.calls.append((helper, kwargs))
        if helper == "js":
            return {"url": "u", "title": "t", "nodes": []}
        if helper == "current_tab":
            return self.tab
        return {}


def _install(monkeypatch: pytest.MonkeyPatch, supervisor: Any) -> None:
    async def _sup(hub: Any, key: str) -> Any:
        return supervisor

    monkeypatch.setattr(mod, "_supervisor", _sup)


def _no_stream(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record ``start_stream`` calls instead of attaching a screencast to a MagicMock relay."""
    seen: list[tuple[str, str]] = []

    async def _start(self: Any, key: str, relay: Any, target: str, **kw: Any) -> None:
        seen.append((key, target))
        sess = self._session(key)
        sess.url, sess.title = kw.get("url", ""), kw.get("title", "")

    monkeypatch.setattr(hub_mod.LiveHub, "start_stream", _start)
    return seen


@pytest.mark.asyncio
async def test_missing_internal_auth_is_rejected() -> None:
    resp = await mod.api_browser_op(
        _request({"op": "snapshot", "session_key": "k"}, internal=False)
    )
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "loopback_only"


@pytest.mark.asyncio
async def test_non_loopback_remote_passes_when_internal_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # make_mocked_request's remote is not loopback; only internal_auth decides.
    _install(monkeypatch, _Supervisor())
    _no_stream(monkeypatch)
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    assert resp.status == 200
    assert json.loads(resp.body)["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body, code",
    [
        (None, "invalid_json"),
        ({"op": "evaluate", "session_key": "k"}, "bad_op"),
        ({"op": "snapshot", "session_key": "k", "args": []}, "args_must_be_object"),
        ({"op": "snapshot"}, "session_key_required"),
    ],
)
async def test_body_validation(body: dict[str, Any] | None, code: str) -> None:
    req = _request(body)
    if body is None:
        req._read_bytes = b"not json"
    resp = await mod.api_browser_op(req)
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == code


@pytest.mark.asyncio
async def test_not_installed_is_a_refusal_the_shim_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, None)
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["ok"] is False and body["code"] == "not_installed"
    assert "Settings -> Browser" in body["error"]


@pytest.mark.asyncio
async def test_result_passes_through_and_the_context_is_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _Supervisor()
    _install(monkeypatch, sup)
    streams = _no_stream(monkeypatch)
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "dashboard:chat-1"}))
    body = json.loads(resp.body)
    assert body == {"ok": True, "result": {"ok": True, "url": "u", "title": "t", "snapshot": ""}}
    # The bind comes first: the daemon's current tab (T1, in the relay's ledger and
    # owned by nobody) is adopted by this session, then the op runs on it.
    assert [c[0] for c in sup.calls[:2]] == ["current_tab", "js"]
    assert hub_mod.get_live_hub().target_of("chat-1") == "T1"
    # The live card's glue: the hub session is keyed by the SLOT key (the
    # ``dashboard:`` transport prefix folded off), the op is a step on an open
    # turn, and the stream now follows the daemon's current tab.
    sess = hub_mod.get_live_hub().get("chat-1")
    assert sess is not None and sess.turn_id and sess.state != hub_mod.STATE_DONE
    assert [st.label for st in sess.steps] == ["Read the page"]
    assert streams == [("chat-1", "T1")]
    assert sess.url == "https://u" and sess.title == "t"
    assert sup.calls[-1][0] == "current_tab"


@pytest.mark.asyncio
async def test_a_refusal_from_the_op_is_returned_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Supervisor())
    _no_stream(monkeypatch)
    resp = await mod.api_browser_op(
        _request({"op": "click", "session_key": "k", "args": {"ref": "nope"}})
    )
    body = json.loads(resp.body)
    assert body["ok"] is False and body["code"] == "bad_ref"


@pytest.mark.asyncio
async def test_a_raised_helper_becomes_browser_error_with_one_bounded_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Boom(_Supervisor):
        async def call(self, helper: str, **kwargs: Any) -> Any:
            raise RuntimeError("daemon gone\nTraceback (most recent call last) ..." + "x" * 500)

    _install(monkeypatch, _Boom())
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    body = json.loads(resp.body)
    assert body["ok"] is False and body["code"] == "browser_error"
    assert body["error"] == "daemon gone"


@pytest.mark.asyncio
async def test_a_supervisor_that_fails_to_start_is_browser_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _sup(hub: Any, key: str) -> Any:
        raise OSError("chrome refused the connection")

    monkeypatch.setattr(mod, "_supervisor", _sup)
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    body = json.loads(resp.body)
    assert body == {"ok": False, "code": "browser_error", "error": "chrome refused the connection"}


@pytest.mark.asyncio
async def test_an_op_past_its_budget_is_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Supervisor())

    async def _hang(op: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        await asyncio.Event().wait()  # never set: the wait_for budget below cancels it
        return {"ok": True}

    monkeypatch.setattr(harness_ops, "run_op", _hang)
    monkeypatch.setattr(mod, "OP_TIMEOUT_S", 0.01)
    monkeypatch.setattr(mod, "DECISION_HEADROOM_S", 0.0)
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    body = json.loads(resp.body)
    assert body["ok"] is False and body["code"] == "timeout"


def test_build_context_binds_the_live_hub_and_relay() -> None:
    hub = hub_mod.LiveHub()
    hub.turn_started("held", "t1")
    asyncio.run(hub.claim("held"))
    sup = _Supervisor()
    ctx = mod.build_context("held", sup, hub)
    assert ctx.in_control() is True
    ctx.step("Open x")
    assert [st.label for st in hub.get("held").steps] == ["Open x"]
    # ``tabs`` sees only the tabs THIS session owns, out of the relay's ledger.
    assert ctx.agent_targets() == []
    hub.bind_target("held", "T1")
    hub.bind_target("held", "T-gone")
    assert ctx.agent_targets() == ["T1"]
    assert ctx.helper == sup.call
    assert mod.build_context("free", sup, hub).in_control() is False


def test_live_session_key_is_the_slot_key_the_card_uses() -> None:
    assert mod.live_session_key("dashboard:chat-7") == "chat-7"
    assert mod.live_session_key("chat-7") == "chat-7"


def test_the_first_op_opens_a_turn_and_later_ops_join_it() -> None:
    hub = hub_mod.LiveHub()
    mod._open_turn(hub, "k")
    first = hub.get("k").turn_id
    assert first
    mod._open_turn(hub, "k")
    assert hub.get("k").turn_id == first, "the same turn until the chat runner ends it"
    hub.turn_ended("k")
    mod._open_turn(hub, "k")
    assert hub.get("k").turn_id != first, "a new turn after turn_ended"


def test_an_op_after_a_reopen_opens_a_new_turn_not_the_finished_one() -> None:
    """A reopened card reads ``live`` but its turn is over: the next op's steps
    belong to a new card, not the finished turn's strip."""
    hub = hub_mod.LiveHub()
    mod._open_turn(hub, "k")
    first = hub.get("k").turn_id
    hub.turn_ended("k")
    hub.get("k").state = hub_mod.STATE_LIVE  # what a reopen leaves behind
    mod._open_turn(hub, "k")
    assert hub.get("k").turn_id != first and hub.get("k").turn_over is False


@pytest.mark.asyncio
async def test_follow_current_tab_restarts_only_when_the_target_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = hub_mod.LiveHub()
    sup = _Supervisor()
    streams = _no_stream(monkeypatch)
    await mod.follow_current_tab(hub, "k", sup)
    assert streams == [("k", "T1")]
    # Same target again: only the address pill moves (page_changed), no restart.
    hub.get("k").stream = MagicMock(target_id="T1")
    sup.tab = {"targetId": "T1", "url": "https://u/2", "title": "t2"}
    await mod.follow_current_tab(hub, "k", sup)
    assert streams == [("k", "T1")]
    assert hub.get("k").url == "https://u/2"
    # The agent switched tabs: the stream is restarted on the new target.
    sup.tab = {"targetId": "T2", "url": "https://v", "title": "v"}
    await mod.follow_current_tab(hub, "k", sup)
    assert streams == [("k", "T1"), ("k", "T2")]


@pytest.mark.asyncio
async def test_follow_current_tab_swallows_a_failing_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Boom(_Supervisor):
        async def call(self, helper: str, **kwargs: Any) -> Any:
            raise RuntimeError("daemon gone")

    hub = hub_mod.LiveHub()
    streams = _no_stream(monkeypatch)
    await mod.follow_current_tab(hub, "k", _Boom())
    assert streams == []


@pytest.mark.asyncio
async def test_an_op_while_the_person_has_the_browser_answers_hold_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route never waits: it answers ``user_in_control`` with ``hold: true`` and
    touches nothing, and the shim parks the tool on ``/api/browser/handback``."""
    sup = _Supervisor()
    _install(monkeypatch, sup)
    streams = _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    hub.turn_started("k", "t1")
    await hub.claim("k")
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    body = json.loads(resp.body)
    assert body["ok"] is False and body["code"] == "user_in_control" and body["hold"] is True
    assert "runs once they hand it back" in body["error"]
    assert streams == [] and sup.calls == [], "nothing touches the browser while held"
    assert not mod._OP_LOCK.locked(), "the hold never sits on the cross-chat lock"
    # After the hand-back the same op runs as normal.
    await hub.release("k")
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    assert json.loads(resp.body)["ok"] is True
    assert streams == [("k", "T1")]


@pytest.mark.asyncio
async def test_the_handback_poll_waits_briefly_on_the_release_signal() -> None:
    """One poll: ``released`` at once when free, after the Esc when it lands inside
    the short wait, and ``False`` once the wait passes with the person still in control."""
    hub = hub_mod.get_live_hub()
    hub.turn_started("k", "t1")
    resp = await mod.api_browser_handback(_request({"session_key": "k"}))
    assert json.loads(resp.body) == {
        "released": True,
        "hold_timeout_s": mod.HOLD_TIMEOUT_S,
        "handed_back": None,
    }
    await hub.claim("k")
    poll = asyncio.ensure_future(mod.api_browser_handback(_request({"session_key": "k"})))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not poll.done()
    await hub.release("k")
    assert json.loads((await poll).body)["released"] is True
    await hub.claim("k")
    hub._sleep = _instant  # the bound, on the hub's injected sleep
    resp = await mod.api_browser_handback(_request({"session_key": "k"}))
    assert json.loads(resp.body)["released"] is False
    # Same trust class as the op route.
    resp = await mod.api_browser_handback(_request({"session_key": "k"}, internal=False))
    assert resp.status == 403
    resp = await mod.api_browser_handback(_request({}))
    assert resp.status == 400


async def _instant(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_waiting_allow_reaches_the_card_while_ensure_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_supervisor`` tells the hub ``waiting_allow`` as soon as the supervisor says so."""
    import types

    class _Sup:
        def __init__(self) -> None:
            self.polls = 0
            self.done = asyncio.Event()

        def ensure_in_background(self) -> Any:
            async def _wait() -> Any:
                await self.done.wait()
                return self

            return asyncio.ensure_future(_wait())

        def status(self) -> dict[str, Any]:
            self.polls += 1
            if self.polls >= 2:
                self.done.set()
            return {
                "connection": "waiting_allow",
                "browser": {"kind": "user_chrome", "headless": False},
                "detail": "click Allow",
            }

    sup = _Sup()
    fake_mod = types.SimpleNamespace(WAITING_ALLOW="waiting_allow", get_supervisor=lambda home: sup)
    monkeypatch.setattr(harness_ops, "browsing_available", lambda home: True)
    monkeypatch.setattr(mod.importlib, "import_module", lambda name: fake_mod)
    monkeypatch.setattr(mod, "_ENSURE_POLL_S", 0.001)
    hub = hub_mod.LiveHub()
    got = await mod._supervisor(hub, "k")
    assert got is sup
    sess = hub.get("k")
    assert sess is not None and sess.state == hub_mod.STATE_WAITING_ALLOW
    assert sess.browser == "user_chrome" and sess.headless is False


class _TabbedSupervisor(_Supervisor):
    """A daemon with a current tab that ``switch_tab`` and ``new_tab`` move."""

    def __init__(self) -> None:
        super().__init__()
        self.current = "T1"
        self.ledger = ["T1"]
        self.relay.agent_targets.side_effect = lambda: list(self.ledger)
        self.created = 0

    async def call(self, helper: str, **kwargs: Any) -> Any:
        self.calls.append((helper, kwargs))
        if helper == "current_tab":
            return {"targetId": self.current, "url": f"https://{self.current}", "title": "t"}
        if helper == "switch_tab":
            self.current = kwargs["target"]
            return "sid"
        if helper == "new_tab":
            self.created += 1
            self.current = f"T-new{self.created}"
            self.ledger.append(self.current)
            return self.current
        if helper == "js":
            return {"url": f"https://{self.current}", "title": "t", "nodes": []}
        return {}


async def _op(key: str, op: str = "snapshot", **args: Any) -> dict[str, Any]:
    resp = await mod.api_browser_op(_request({"op": op, "args": args, "session_key": key}))
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_two_chats_get_their_own_tabs_and_never_drive_each_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chat A adopts the daemon's tab; chat B gets a new one; A's next op switches back.

    Before the bind, B's ``click`` ran on whatever tab the daemon was on -- A's page,
    or the page A's person had taken over.
    """
    sup = _TabbedSupervisor()
    _install(monkeypatch, sup)
    streams = _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    assert (await _op("chat-a"))["ok"] is True
    assert hub.target_of("chat-a") == "T1" and sup.current == "T1"
    assert (await _op("chat-b"))["ok"] is True
    assert hub.target_of("chat-b") == "T-new1" and sup.current == "T-new1"
    assert ("new_tab", {"url": "about:blank"}) in sup.calls
    sup.calls.clear()
    assert (await _op("chat-a"))["ok"] is True
    assert [c[0] for c in sup.calls[:3]] == ["current_tab", "switch_tab", "js"]
    assert sup.calls[1][1] == {"target": "T1", "activate": False}
    assert sup.current == "T1"
    # Each card streams its own tab (the fake start_stream keeps no stream, so A's
    # second op restarts; a real stream on T1 would not).
    assert streams[:2] == [("chat-a", "T1"), ("chat-b", "T-new1")]
    assert hub.targets_of("chat-a") == {"T1"} and hub.targets_of("chat-b") == {"T-new1"}


@pytest.mark.asyncio
async def test_a_takeover_in_one_chat_fences_only_that_chats_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _TabbedSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    await _op("chat-b")
    assert await hub.claim("chat-a")
    sup.calls.clear()
    held = await _op("chat-a")
    assert held["code"] == "user_in_control" and held["hold"] is True
    assert sup.calls == [], "no tab switch, no helper, while the person holds A's tab"
    assert (await _op("chat-b"))["ok"] is True, "B keeps browsing while A is held"
    assert sup.current == "T-new1"
    await hub.release("chat-a")
    assert (await _op("chat-a"))["ok"] is True, "A's op runs on the hand-back"
    assert sup.current == "T1"


@pytest.mark.asyncio
async def test_a_session_whose_tab_was_closed_gets_a_fresh_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _TabbedSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    sup.ledger.remove("T1")  # the browser reported T1 destroyed
    sup.current = "T1"
    assert (await _op("chat-a"))["ok"] is True
    assert hub.target_of("chat-a") == "T-new1" and sup.current == "T-new1"


@pytest.mark.asyncio
async def test_ops_from_different_chats_run_one_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bind-then-op pair of one chat never interleaves with another's."""
    sup = _TabbedSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    gate = asyncio.Event()
    inner = sup.call

    async def slow_call(helper: str, **kwargs: Any) -> Any:
        if helper == "js":
            await gate.wait()
        return await inner(helper, **kwargs)

    sup.call = slow_call  # type: ignore[method-assign]
    first = asyncio.ensure_future(_op("chat-a"))
    second = asyncio.ensure_future(_op("chat-b"))
    for _ in range(20):
        await asyncio.sleep(0)
    # A is parked inside its op; B has not even asked for the current tab.
    assert [c[0] for c in sup.calls] == ["current_tab"]
    gate.set()
    assert (await first)["ok"] is True and (await second)["ok"] is True
    assert sup.current == "T-new1"


class _ParkingSupervisor(_Supervisor):
    """``activeField`` parks until ``gate`` is set, so a claim can land mid-op."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.parked = asyncio.Event()

    async def call(self, helper: str, **kwargs: Any) -> Any:
        self.calls.append((helper, kwargs))
        if helper == "js" and "activeField" in str(kwargs.get("expression")):
            self.parked.set()
            await self.gate.wait()
            return {"focused": True, "password": False, "tag": "input"}
        if helper == "current_tab":
            return self.tab
        return {}


@pytest.mark.asyncio
async def test_a_takeover_during_an_op_cancels_it_and_answers_interrupted_with_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The person clicks Take over while ``type`` awaits the page's ``activeField``.

    The claim and the op share the session's fence: the claim cancels the op in
    flight and is confirmed only once the route has left the fence, so the agent's
    ``type_text`` never runs under the person's hands. The cut-short op answers
    ``interrupted`` with ``hold: true``: the shim waits for the hand-back and then
    tells the agent to look before repeating it (the op is never re-run blind).
    """
    sup = _ParkingSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    key = "k"
    route = asyncio.ensure_future(
        mod.api_browser_op(_request({"op": "type", "args": {"text": "hello"}, "session_key": key}))
    )
    await sup.parked.wait()
    assert hub.op_fence(key).locked()
    claimed = await hub.claim(key)  # returns only once the op has left the fence
    assert claimed is True and hub.in_control(key) is True
    assert not hub.op_fence(key).locked()
    body = json.loads((await route).body)
    assert body["ok"] is False and body["code"] == "interrupted" and body["hold"] is True
    assert "`type`" in body["error"] and "snapshot" in body["error"]
    assert ("type_text", {"text": "hello"}) not in sup.calls


@pytest.mark.asyncio
async def test_a_fast_op_behind_a_held_lock_answers_browser_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded lock wait: the route answers inside the shim's budget instead of
    landing the op after the tool has reported it failed."""
    _install(monkeypatch, _Supervisor())
    monkeypatch.setattr(mod, "LOCK_WAIT_S", 0.01)
    await mod._OP_LOCK.acquire()
    try:
        resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    finally:
        mod._OP_LOCK.release()
    body = json.loads(resp.body)
    assert body["ok"] is False and body["code"] == "browser_busy"


def test_the_shims_budget_outlasts_the_routes_worst_case() -> None:
    """The tool's HTTP timeout is derived from this route's waits (one module), and is
    strictly longer than their sum, so a "did not answer" can never precede a landing."""
    from kiro_crew.browser_harness import budgets
    from kiro_crew.mcp_tools import browser as shim

    worst_fast = mod.ENSURE_TIMEOUT_S + mod.LOCK_WAIT_S + mod.OP_TIMEOUT_S + mod.OP_TIMEOUT_S
    worst_slow = mod.ENSURE_TIMEOUT_S + mod.LOCK_WAIT_S + mod.OP_TIMEOUT_S + mod.SLOW_OP_TIMEOUT_S
    # The hold is the shim's, in slices, never in one HTTP call: the gateway's
    # hand-back poll waits far less than the shim's step budget.
    assert mod.HOLD_TIMEOUT_S == 10 * 60
    assert budgets.HANDBACK_WAIT_S < budgets.HANDBACK_CALL_TIMEOUT_S < 60
    assert shim.FAST_COMMAND_TIMEOUT_SECS > worst_fast
    assert shim.SLOW_COMMAND_TIMEOUT_SECS > worst_slow
    assert shim.FAST_COMMAND_TIMEOUT_SECS == budgets.SHIM_FAST_TIMEOUT_S
    assert shim.SLOW_COMMAND_TIMEOUT_SECS == budgets.SHIM_SLOW_TIMEOUT_S


@pytest.mark.asyncio
async def test_status_is_read_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """``status()`` stats the marker and may run discovery probes: never on the loop
    (the ensure poll calls it 4x a second, ``follow_current_tab`` after every op)."""
    import threading

    loop_thread = threading.get_ident()
    threads: list[int] = []

    class Recording(_Supervisor):
        def status(self) -> dict[str, Any]:
            threads.append(threading.get_ident())
            return super().status()

        def ensure_in_background(self) -> Any:
            async def _never() -> None:
                await asyncio.Event().wait()  # never set: cancelled below

            return asyncio.ensure_future(_never())

    sup = Recording()
    _no_stream(monkeypatch)
    monkeypatch.setattr(mod.harness_ops, "browsing_available", lambda home: True)
    monkeypatch.setattr(mod, "_ENSURE_POLL_S", 0.001)
    monkeypatch.setattr(mod, "ENSURE_TIMEOUT_S", 0.02)
    fake_mod = MagicMock(get_supervisor=lambda home: sup, WAITING_ALLOW="waiting_allow")
    monkeypatch.setattr(mod.importlib, "import_module", lambda name: fake_mod)
    with pytest.raises(TimeoutError):
        await mod._supervisor(hub_mod.get_live_hub(), "k")
    await mod.follow_current_tab(hub_mod.get_live_hub(), "k", sup)
    assert threads and all(t != loop_thread for t in threads)


@pytest.mark.asyncio
async def test_a_tab_another_session_owns_is_not_streamed_to_this_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``follow_current_tab`` refused to BIND a foreign tab but still streamed it:
    chat B's card showed chat A's page and B's takeover drove A's tab."""
    hub = hub_mod.LiveHub()
    sup = _Supervisor()
    streams = _no_stream(monkeypatch)
    hub.bind_target("other", "T1")
    await mod.follow_current_tab(hub, "k", sup)
    assert streams == []
    assert hub.target_of("k") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["click", "type", "reflex", "navigate"])
async def test_no_agent_op_runs_while_a_quick_ask_waits(
    monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    # A parallel tool call must not press the robot check or the confirm button
    # (or type into the asked field) while the person is being asked.
    from types import SimpleNamespace

    sup = _Supervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    key = mod.live_session_key("k")
    hub._session(key).ask = SimpleNamespace(outcome="")
    resp = await mod.api_browser_op(
        _request({"op": op, "session_key": "k", "args": {"ref": "e1", "text": "x"}})
    )
    assert json.loads(resp.body)["code"] == "ask_pending"
    assert [c for c, _kw in sup.calls if c not in ("current_tab",)] == []
    # Once the ask has an outcome, ops run again.
    hub._session(key).ask = SimpleNamespace(outcome="filled")
    resp = await mod.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
    assert json.loads(resp.body)["ok"] is True
