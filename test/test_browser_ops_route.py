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
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.browser_harness import live_hub as hub_mod
from kiro_crew.browser_harness import ops as harness_ops
from kiro_crew.dashboard.handlers import browser_ops as mod
from kiro_crew.testing.wait import await_until, until_parked


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
        self.relay.context_of.return_value = ""  # every tab in the default context
        self.tab = {"targetId": "T1", "url": "https://u", "title": "t"}

    def status(self) -> dict[str, Any]:
        return {"browser": {"kind": "kiro_chromium", "headless": True}, "connection": "connected"}

    async def call(self, helper: str, **kwargs: Any) -> Any:
        self.calls.append((helper, kwargs))
        if helper in ("js", "js_isolated"):
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
async def test_a_repair_it_starts_is_its_own_code_with_the_retry_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``not_installed`` is rendered by the shim as "ask the user to install"; a repair
    already under way must reach the agent as "try again" instead."""
    from aiohttp import web

    from kiro_crew.dashboard.handlers import browser_harness_status

    _install(monkeypatch, None)
    started: list[Any] = []

    async def _repair(state: Any) -> bool:
        started.append(state)
        return True

    monkeypatch.setattr(browser_harness_status, "repair_browser_harness", _repair)
    app = web.Application()
    state = object()
    app["state"] = state
    req = make_mocked_request("POST", "/api/browser/op", app=app)
    req["internal_auth"] = True
    req._read_bytes = json.dumps({"op": "snapshot", "session_key": "k"}).encode()
    body = json.loads((await mod.api_browser_op(req)).body)
    assert started == [state]
    assert body["ok"] is False and body["code"] == "browser_repairing"
    assert "try again in a minute" in body["error"]


@pytest.mark.asyncio
async def test_an_op_while_an_install_job_runs_is_told_to_retry_not_to_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later op (the agent's retry, or after Settings started the repair) does not
    start a repair itself; it must still read "try again", not "ask to install"."""
    from aiohttp import web

    from kiro_crew.dashboard.handlers import browser_harness_status

    _install(monkeypatch, None)

    async def _repair(state: Any) -> bool:
        return False

    monkeypatch.setattr(browser_harness_status, "repair_browser_harness", _repair)

    class _State:
        _browser_harness_install_job = browser_harness_status.InstallJob.start(("harness",))

    app = web.Application()
    app["state"] = _State()
    req = make_mocked_request("POST", "/api/browser/op", app=app)
    req["internal_auth"] = True
    req._read_bytes = json.dumps({"op": "snapshot", "session_key": "k"}).encode()
    body = json.loads((await mod.api_browser_op(req)).body)
    assert body["ok"] is False and body["code"] == "browser_repairing"
    assert "try again in a minute" in body["error"]


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
    assert [c[0] for c in sup.calls[:4]] == ["current_tab", "current_tab", "current_tab", "js"]
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


@pytest.mark.asyncio
async def test_a_navigate_runs_through_the_sign_in_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route hands a navigate's task to ``browser_harness.sign_in`` with its own
    supervisor, so an edition's ``complete_sign_in`` is reachable from the agent's op."""
    from kiro_crew.browser_harness import sign_in

    sup = _Supervisor()
    _install(monkeypatch, sup)
    seen: list[tuple[str, Any]] = []

    async def _wrapped(op: str, args: dict[str, Any], ctx: Any, supervisor: Any) -> dict[str, Any]:
        seen.append((op, supervisor))
        return {"ok": True, "url": "https://example.com/", "title": "", "sign_in": {"outcome": "x"}}

    monkeypatch.setattr(sign_in, "run_op", _wrapped)
    resp = await mod.api_browser_op(
        _request({"op": "navigate", "args": {"url": "https://example.com/"}, "session_key": "k"})
    )
    body = json.loads(resp.body)
    assert seen == [("navigate", sup)]
    assert body["ok"] is True and body["result"]["sign_in"] == {"outcome": "x"}


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
async def test_follow_current_tab_leaves_a_tab_with_no_document_yet_to_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A popup that has not committed its first page reports url "": it is not sent
    to about:blank, and no frame of it is streamed until a committed URL is judged."""
    hub = hub_mod.LiveHub()
    sup = _Supervisor()
    sup.tab = {"targetId": "P1", "url": "", "title": ""}
    streams = _no_stream(monkeypatch)
    opener_stream = MagicMock(target_id="T1", stop=AsyncMock())
    hub._session("k").stream = opener_stream
    judged: list[str] = []

    async def _refuse(_ctx: Any, url: str) -> dict[str, Any] | None:
        assert url, "an empty url must not be judged"
        judged.append(url)
        return harness_ops.refusal("redirect_refused", "refused")

    monkeypatch.setattr(mod.harness_ops, "_refuse_committed_url", _refuse)
    assert await mod.follow_current_tab(hub, "k", sup) is None
    assert ("goto", {"url": "about:blank"}) not in sup.calls
    assert streams == [], "no frame of the loading popup is published"
    opener_stream.stop.assert_awaited_once()
    # It commits to an address the gate refuses: the next follow refuses it, still unstreamed.
    sup.tab = {"targetId": "P1", "url": "http://127.0.0.1:9/", "title": ""}
    refused = await mod.follow_current_tab(hub, "k", sup)
    assert refused is not None and judged == ["http://127.0.0.1:9/"]
    assert streams == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tab", [{"targetId": "P1"}, {"targetId": "P1", "url": None}])
async def test_follow_current_tab_still_judges_a_tab_with_no_url_string(
    monkeypatch: pytest.MonkeyPatch, tab: dict[str, Any]
) -> None:
    hub = hub_mod.LiveHub()
    sup = _Supervisor()
    sup.tab = tab
    _no_stream(monkeypatch)
    judged: list[str] = []

    async def _refuse(_ctx: Any, url: str) -> dict[str, Any]:
        judged.append(url)
        return harness_ops.refusal("redirect_refused", "refused")

    monkeypatch.setattr(mod.harness_ops, "_refuse_committed_url", _refuse)
    refused = await mod.follow_current_tab(hub, "k", sup)
    assert refused is not None and refused["code"] == "redirect_refused"
    assert judged == [""]


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
        if helper in ("js", "js_isolated"):
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
    assert [c[0] for c in sup.calls[:5]] == [
        "current_tab",
        "switch_tab",
        "current_tab",
        "current_tab",
        "js",
    ]
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


class _ClosedTabSupervisor(_TabbedSupervisor):
    """The real daemon: ``current_tab`` fails once the tab it is attached to is gone."""

    async def call(self, helper: str, **kwargs: Any) -> Any:
        if helper == "current_tab" and self.current not in self.ledger:
            self.calls.append((helper, kwargs))
            raise mod.HelperError("RuntimeError: cdp_disconnected")
        return await super().call(helper, **kwargs)


@pytest.mark.asyncio
async def test_a_chat_keeps_browsing_after_the_daemons_tab_was_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chat A ran the last op, then ended and its tab closed: B's next op still runs."""
    sup = _ClosedTabSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    await _op("chat-a")
    await _op("chat-b")
    await _op("chat-a")
    assert sup.current == "T1"
    sup.ledger.remove("T1")  # chat A ended and its tab was closed
    assert (await _op("chat-b"))["ok"] is True
    assert sup.current == "T-new1"


class _PopupSupervisor(_TabbedSupervisor):
    """A daemon whose pages can open popups: the relay ledgers each with its opener."""

    def __init__(self) -> None:
        super().__init__()
        self.openers: dict[str, str] = {}
        self.relay.opener_of.side_effect = lambda t: self.openers.get(t, "")
        #: The next op's page script opens a popup from the daemon's current tab.
        self.popup_on_next_op = ""

    def open_popup(self, popup: str, opener: str) -> None:
        self.ledger.append(popup)
        self.openers[popup] = opener

    async def call(self, helper: str, **kwargs: Any) -> Any:
        if helper == "list_tabs":
            self.calls.append((helper, kwargs))
            return [{"targetId": t, "url": f"https://{t}", "title": "t"} for t in self.ledger]
        if helper in ("js", "js_isolated") and self.popup_on_next_op:
            self.open_popup(self.popup_on_next_op, self.current)
            self.popup_on_next_op = ""
        return await super().call(helper, **kwargs)


class _ClosedTabPopupSupervisor(_PopupSupervisor, _ClosedTabSupervisor):
    """Popups, and ``current_tab`` fails once the daemon's tab is gone."""


class _ClosingPopupSupervisor(_PopupSupervisor):
    """The real daemon: a click that closes its own popup loses the CDP session."""

    async def call(self, helper: str, **kwargs: Any) -> Any:
        if helper in ("js", "js_isolated") and getattr(self, "close_on_next_op", False):
            self.close_on_next_op = False
            self.ledger.remove(self.current)
            self.calls.append((helper, kwargs))
            raise mod.HelperError("RuntimeError: cdp_disconnected")
        return await super().call(helper, **kwargs)


@pytest.mark.asyncio
async def test_a_click_that_closes_its_popup_answers_ok_and_returns_to_the_opener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The "Log in" button calls window.close(): the daemon's session dies mid-op."""
    sup = _ClosingPopupSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    sup.open_popup("P1", "T1")
    await _op("chat-a")
    assert hub.target_of("chat-a") == "P1"
    sup.close_on_next_op = True
    answer = await _op("chat-a", "click", ref="e1")
    assert answer["ok"] is True
    closed = answer["result"]["popup_closed"]
    assert closed["target_id"] == "P1" and closed["returned_to"] == "T1"
    assert hub.target_of("chat-a") == "T1" and sup.current == "T1"
    assert sup.created == 0


@pytest.mark.asyncio
async def test_a_closed_popup_that_sent_its_opener_to_a_refused_url_answers_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``window.opener.location = <private host>; window.close()``: the gate's refusal stands."""
    sup = _ClosingPopupSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    audits: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mod, "_audit", lambda _req, outcome, code="": audits.append((outcome, code))
    )
    await _op("chat-a")
    sup.open_popup("P1", "T1")
    await _op("chat-a")
    monkeypatch.setattr(mod, "committed_url_allowed", lambda url, _own=(): url != "https://T1")
    sup.close_on_next_op = True
    audits.clear()
    answer = await _op("chat-a", "click", ref="e1")
    assert answer["ok"] is False and answer["code"] == "redirect_refused"
    assert "popup_closed" not in answer
    assert audits == [("denied", "redirect_refused")]
    assert ("goto", {"url": "about:blank"}) in sup.calls, "the refused page was left"


@pytest.mark.asyncio
async def test_a_failure_on_a_tab_that_is_not_a_closed_popup_stays_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _ClosingPopupSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    monkeypatch.setattr(mod, "POPUP_CLOSE_GRACE_S", 0.0)
    await _op("chat-a")
    sup.open_popup("P1", "T1")
    await _op("chat-a")

    async def _raise(helper: str, **kwargs: Any) -> Any:
        if helper in ("js", "js_isolated"):
            raise mod.HelperError("RuntimeError: boom")
        return await _PopupSupervisor.call(sup, helper, **kwargs)

    monkeypatch.setattr(sup, "call", _raise)
    answer = await _op("chat-a", "click", ref="e1")
    assert answer["ok"] is False and answer["code"] == "browser_error"


@pytest.mark.asyncio
async def test_a_popup_whose_opener_popup_closed_is_still_claimed_and_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1 opens P1, P1 opens P2, P1 closes before the next op: P2 is still A's."""
    sup = _PopupSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    sup.open_popup("P1", "T1")
    sup.open_popup("P2", "P1")
    sup.ledger.remove("P1")  # the middle popup closed, its opener link kept
    answer = (await _op("chat-a"))["result"]
    assert answer["popup"]["target_id"] == "P2"
    assert hub.target_of("chat-a") == "P2" and "P2" in hub.targets_of("chat-a")


def test_claiming_through_closed_popups_stops_at_another_chats_tab() -> None:
    hub = hub_mod.get_live_hub()
    hub.bind_target("a", "T1")
    hub.bind_target("b", "TB")
    relay = MagicMock()
    relay.agent_targets.return_value = ["T1", "TB", "Q2"]
    # Q2's opener Q1 is gone; Q1 was opened by B's tab, which was opened from A's.
    relay.opener_of.side_effect = {"Q2": "Q1", "Q1": "TB", "TB": "T1"}.get
    assert mod.claim_popups(hub, "a", relay) == []
    assert mod.claim_popups(hub, "b", relay) == ["Q2"]


class _BindRaceSupervisor(_PopupSupervisor):
    """The bound popup closes while the bind reads ``current_tab``: the switch fails."""

    close_during_bind = ""

    async def call(self, helper: str, **kwargs: Any) -> Any:
        if helper == "current_tab" and self.close_during_bind:
            closing, self.close_during_bind = self.close_during_bind, ""
            self.ledger.remove(closing)
            self.current = "T-other"
            self.calls.append((helper, kwargs))
            return {"targetId": "T-other", "url": "https://T-other", "title": "t"}
        if helper == "switch_tab" and kwargs["target"] not in self.ledger:
            self.calls.append((helper, kwargs))
            raise mod.HelperError("RuntimeError: No target with given id found")
        return await super().call(helper, **kwargs)


@pytest.mark.asyncio
async def test_a_popup_that_closes_during_the_bind_still_runs_the_op_on_its_opener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bind's ledger snapshot still lists the popup; its switch fails. The op runs."""
    sup = _BindRaceSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    sup.open_popup("P1", "T1")
    await _op("chat-a")
    assert hub.target_of("chat-a") == "P1"
    sup.close_during_bind = "P1"
    sup.calls.clear()
    answer = await _op("chat-a", "snapshot")
    assert answer["ok"] is True
    assert "snapshot" in answer["result"], "the op itself ran"
    assert answer["result"]["url"] == "https://T1", "the snapshot ran on the opener"
    assert answer["result"]["popup_closed"]["returned_to"] == "T1", "and the move is reported"
    assert hub.target_of("chat-a") == "T1" and sup.current == "T1"


@pytest.mark.asyncio
async def test_a_ref_op_after_the_popup_closed_is_refused_and_a_navigate_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ref from the popup's snapshot must not land on the opener's ``e1``."""
    sup = _PopupSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    sup.open_popup("P1", "T1")
    await _op("chat-a")
    assert hub.target_of("chat-a") == "P1"
    sup.ledger.remove("P1")  # the popup closed between ops
    sup.calls.clear()
    clicked = await _op("chat-a", "click", ref="e1")
    assert clicked["ok"] is False and clicked["code"] == "popup_closed"
    assert hub.target_of("chat-a") == "T1" and sup.current == "T1"
    assert not any(h in ("click", "js") for h, _ in sup.calls), "nothing touched the opener's page"
    snap = (await _op("chat-a", "snapshot"))["result"]
    assert snap["ok"] is True and snap["url"] == "https://T1"


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [True, False])
@pytest.mark.parametrize("during_bind", [False, True])
async def test_read_after_popup_closed_refuses_only_a_scoped_read(
    monkeypatch: pytest.MonkeyPatch, scoped: bool, during_bind: bool
) -> None:
    """A popup ref must not read the opener's element with the same ref id."""
    sup = _BindRaceSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    sup.open_popup("P1", "T1")
    await _op("chat-a", "snapshot")
    assert hub.target_of("chat-a") == "P1"
    if during_bind:
        sup.close_during_bind = "P1"
    else:
        sup.ledger.remove("P1")
    read_targets: list[tuple[str, dict[str, Any]]] = []

    async def _read(op: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        assert op == "read"
        read_targets.append((sup.current, args))
        return {"ok": True, "content": "opener text"}

    monkeypatch.setattr(harness_ops, "run_op", _read)
    answer = await _op("chat-a", "read", **({"ref": "e1"} if scoped else {}))
    assert hub.target_of("chat-a") == "T1" and sup.current == "T1"
    if scoped:
        assert answer["ok"] is False and answer["code"] == "popup_closed"
        assert read_targets == [], "the opener's same ref must never be read"
        result = answer
    else:
        result = answer["result"]
        assert result["ok"] is True and result["content"] == "opener text"
        assert read_targets == [("T1", {})]
    assert result["popup_closed"]["returned_to"] == "T1"
    assert hub.pending_tab_move("chat-a") is None


@pytest.mark.asyncio
async def test_a_ref_op_after_a_late_popup_opened_is_refused_until_a_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _PopupSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    sup.open_popup("P1", "T1")  # opened after the last answer
    typed = await _op("chat-a", "type", ref="e1", text="x")
    assert typed["ok"] is False and typed["code"] == "popup_opened"
    assert hub.target_of("chat-a") == "P1"
    again = await _op("chat-a", "click", ref="e1")
    # The click itself ran on P1: the fake page has no elements, so its ref is stale.
    assert again["code"] == "ref_stale" and sup.current == "P1"


@pytest.mark.asyncio
async def test_a_failed_op_after_a_late_popup_still_reports_the_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``wait_for`` on the followed popup times out: the refusal carries ``popup``."""
    sup = _PopupSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    sup.open_popup("P1", "T1")  # opened after the last answer

    async def _refuse(op: str, args: Any, ctx: Any) -> dict[str, Any]:
        return harness_ops.refusal("wait_timeout", "the text did not appear")

    monkeypatch.setattr(harness_ops, "run_op", _refuse)
    answer = await _op("chat-a", "wait_for", text="Welcome")
    assert answer["ok"] is False and answer["code"] == "wait_timeout"
    assert answer["popup"]["target_id"] == "P1" and "popup window opened" in answer["popup"]["hint"]
    assert hub.pending_tab_move("chat-a") is None, "reported once, then cleared"


def test_a_move_no_answer_reported_stays_pending_and_refuses_page_targeted_ops() -> None:
    """A HOLD or an interrupted op is not an answer: the move waits for the next one."""
    hub = hub_mod.get_live_hub()
    hub.bind_target("k", "P1")
    hub.note_tab_moved("k", {"popup": {"target_id": "P1"}})
    held = object()
    assert mod._with_tab_move(hub, "k", held) is held
    assert hub.pending_tab_move("k") is not None
    assert mod._refused_for_tab_move(hub, "k", "snapshot", {}) is None, "reads still run"
    pasted = mod._refused_for_tab_move(hub, "k", "paste", {})
    assert pasted is not None and pasted["code"] == "popup_opened", "paste is refused like type"
    refused = mod._refused_for_tab_move(hub, "k", "click", {})
    assert refused is not None and refused["code"] == "popup_opened"
    told = mod._with_tab_move(hub, "k", refused)
    assert told["popup"]["target_id"] == "P1" and hub.pending_tab_move("k") is None
    assert mod._refused_for_tab_move(hub, "k", "click", {}) is None, "told once, then the op runs"


def test_a_governance_deny_leaves_the_tab_move_pending() -> None:
    hub = hub_mod.get_live_hub()
    hub.bind_target("k", "P1")
    hub.note_tab_moved("k", {"popup": {"target_id": "P1"}})
    denied = harness_ops.refusal(mod.DENIED_CODE, mod.DENIED_TEXT)
    assert "popup" not in mod._with_tab_move(hub, "k", denied)
    assert hub.pending_tab_move("k") is not None


@pytest.mark.asyncio
async def test_a_quick_ask_answer_leaves_the_tab_move_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ask's text shows only its outcome, so the notice must wait for the next op."""
    hub = hub_mod.get_live_hub()
    hub.bind_target("k", "P1")
    hub.note_tab_moved("k", {"popup": {"target_id": "P1"}})

    async def _ask_answer(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return {"ok": True, "outcome": "done"}

    monkeypatch.setattr(mod, "_run_fenced_op", _ask_answer)
    answer = await mod._fenced_op(_request({}), hub, "k", object(), MagicMock(), "ask", {}, 1.0)
    assert "popup" not in answer and hub.pending_tab_move("k") is not None


@pytest.mark.asyncio
async def test_a_popup_the_op_opens_becomes_the_chats_tab_and_the_answer_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A "Sign in" button that opens its own window: the agent lands in it."""
    sup = _PopupSupervisor()
    _install(monkeypatch, sup)
    streams = _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    sup.popup_on_next_op = "P1"
    answer = (await _op("chat-a"))["result"]
    assert answer["ok"] is True
    assert answer["popup"]["target_id"] == "P1"
    assert answer["popup"]["url"] == "https://P1"
    assert "popup window opened" in answer["popup"]["hint"]
    assert sup.current == "P1" and hub.target_of("chat-a") == "P1"
    assert hub.targets_of("chat-a") == {"T1", "P1"}
    assert ("switch_tab", {"target": "P1", "activate": False}) in sup.calls
    assert streams[-1] == ("chat-a", "P1"), "the card follows the popup"
    # The next op stays on the popup and says nothing more about it.
    again = (await _op("chat-a"))["result"]
    assert "popup" not in again and sup.current == "P1"


@pytest.mark.asyncio
async def test_a_popup_that_opened_after_the_answer_is_followed_by_the_next_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _PopupSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    sup.open_popup("P1", "T1")  # the window.open landed after the click answered
    answer = (await _op("chat-a"))["result"]
    assert answer["popup"]["target_id"] == "P1"
    assert hub.target_of("chat-a") == "P1" and sup.current == "P1"


def _late_popup(
    monkeypatch: pytest.MonkeyPatch, sup: _PopupSupervisor, on_look: int, after: str
) -> list[str]:
    """Open P1 from T1 on the *on_look*-th popup look after the *after* helper ran.

    Returns the key of each look taken after it, so a test counts them. No clock:
    the look itself is the point the popup lands at.
    """
    looks: list[str] = []
    real = mod.follow_popup

    async def _follow(hub: Any, key: str, *a: Any, **kw: Any) -> Any:
        if any(h == after for h, _ in sup.calls):
            looks.append(key)
            if len(looks) == on_look:
                sup.open_popup("P1", "T1")
        return await real(hub, key, *a, **kw)

    monkeypatch.setattr(mod, "follow_popup", _follow)
    monkeypatch.setattr(mod, "POPUP_LATE_POLL_S", 0)
    return looks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op", "args", "helper"),
    [("click", {"x": 1, "y": 2}, "click"), ("press_key", {"key": "Enter"}, "press_key")],
)
async def test_a_popup_that_opens_a_moment_after_a_click_is_followed_by_that_op(
    monkeypatch: pytest.MonkeyPatch, op: str, args: dict[str, Any], helper: str
) -> None:
    """``window.open`` after a fetch: the first look after the click finds nothing."""
    sup = _PopupSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    sup.calls.clear()
    looks = _late_popup(monkeypatch, sup, on_look=2, after=helper)
    resp = await mod.api_browser_op(_request({"op": op, "args": args, "session_key": "chat-a"}))
    answer = json.loads(resp.body)
    assert answer["ok"] is True
    assert answer["result"]["popup"]["target_id"] == "P1"
    assert hub.target_of("chat-a") == "P1" and looks == ["chat-a", "chat-a"]


@pytest.mark.asyncio
async def test_an_op_that_cannot_open_a_popup_looks_for_one_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _PopupSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    await _op("chat-a")
    sup.calls.clear()
    looks = _late_popup(monkeypatch, sup, on_look=0, after="js")
    assert (await _op("chat-a", "snapshot"))["ok"] is True
    assert looks == ["chat-a"]


@pytest.mark.asyncio
async def test_no_popup_after_a_click_takes_a_bounded_number_of_looks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _PopupSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    await _op("chat-a")
    sup.calls.clear()
    looks = _late_popup(monkeypatch, sup, on_look=0, after="click")
    assert (await _op("chat-a", "click", x=1, y=2))["ok"] is True
    assert len(looks) == 1 + mod.POPUP_LATE_READS


@pytest.mark.asyncio
async def test_the_late_looks_stop_once_the_person_holds_the_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _PopupSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    await _op("chat-a")
    sup.calls.clear()
    looks = _late_popup(monkeypatch, sup, on_look=0, after="click")
    real = mod.follow_popup

    held: list[str] = []
    monkeypatch.setattr(hub_mod.LiveHub, "in_control", lambda _self, key: key in held)

    async def _claim_then_look(h: Any, key: str, *a: Any, **kw: Any) -> Any:
        answer = await real(h, key, *a, **kw)
        if looks:
            held.append(key)  # the person takes the tab after the first late look
        return answer

    monkeypatch.setattr(mod, "follow_popup", _claim_then_look)
    await _op("chat-a", "click", x=1, y=2)
    assert looks == ["chat-a"], "no look after the person took the tab"


@pytest.mark.asyncio
async def test_a_takeover_during_the_late_wait_keeps_the_persons_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The person takes over while the poll waits and the opener's popup opens then:
    the session stays on the tab the person took, and its stream is not replaced."""
    sup = _PopupSupervisor()
    _install(monkeypatch, sup)
    streams = _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    sup.calls.clear()
    streams.clear()
    looks = _late_popup(monkeypatch, sup, on_look=2, after="click")
    asked: list[bool] = []

    def _in_control(_self: Any, key: str) -> bool:
        # After the first late look, the first ask is the one before the wait and
        # says free; every later ask comes after the wait, once the person took over.
        if not looks:
            return False
        asked.append(len(asked) > 0)
        return asked[-1]

    monkeypatch.setattr(hub_mod.LiveHub, "in_control", _in_control)
    await _op("chat-a", "click", x=1, y=2)
    assert looks == ["chat-a"], "no look after the wait the person took over in"
    assert hub.target_of("chat-a") == "T1" and sup.current == "T1"
    assert ("switch_tab", {"target": "P1", "activate": False}) not in sup.calls
    assert streams == [], "the stream the person took over is left alone"


@pytest.mark.asyncio
async def test_a_late_popup_from_chat_as_click_is_never_claimed_by_chat_b(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _PopupSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    await _op("chat-b")  # B is on T-new1
    await _op("chat-a")
    sup.calls.clear()
    _late_popup(monkeypatch, sup, on_look=2, after="click")
    clicked = await _op("chat-a", "click", x=1, y=2)
    assert clicked["result"]["popup"]["target_id"] == "P1"
    answer = await _op("chat-b")
    assert "popup" not in answer["result"]
    assert hub.owner_of_target("P1") == "chat-a" and "P1" not in hub.targets_of("chat-b")


class _LongTitledPopupSupervisor(_PopupSupervisor):
    """A popup whose page picked a URL and title far longer than any real one."""

    async def call(self, helper: str, **kwargs: Any) -> Any:
        if helper == "current_tab" and self.current == "P1":
            self.calls.append((helper, kwargs))
            return {"targetId": "P1", "url": "https://P1/" + "a" * 50_000, "title": "t" * 50_000}
        return await super().call(helper, **kwargs)


@pytest.mark.asyncio
async def test_a_popups_url_and_title_are_capped_before_they_reach_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page chooses both, so they get the live hub's caps like every other page text."""
    sup = _LongTitledPopupSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    await _op("chat-a")
    sup.open_popup("P1", "T1")
    popup = (await _op("chat-a"))["result"]["popup"]
    assert popup["url"] == ("https://P1/" + "a" * 50_000)[: mod.POPUP_URL_CAP]
    assert popup["title"] == "t" * mod.POPUP_TITLE_CAP


@pytest.mark.asyncio
async def test_when_the_popup_closes_the_chat_returns_to_the_tab_that_opened_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sign-in window closes itself after login: no fresh blank tab is opened."""
    sup = _ClosedTabPopupSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    sup.open_popup("P1", "T1")
    sup.open_popup("P2", "P1")  # a popup of the popup
    await _op("chat-a")
    assert hub.target_of("chat-a") == "P2"
    sup.ledger.remove("P2")
    sup.ledger.remove("P1")  # both windows closed
    assert (await _op("chat-a"))["ok"] is True
    assert hub.target_of("chat-a") == "T1" and sup.current == "T1"
    assert sup.created == 0


@pytest.mark.asyncio
async def test_a_popup_is_claimed_only_by_the_chat_whose_tab_opened_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sup = _PopupSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    await _op("chat-a")
    await _op("chat-b")  # B is on T-new1
    sup.open_popup("P-a", "T1")
    sup.open_popup("P-user", "T-user")  # the person's own tab opened this one
    answer = (await _op("chat-b"))["result"]
    assert "popup" not in answer
    assert hub.targets_of("chat-b") == {"T-new1"} and sup.current == "T-new1"
    listed = (await _op("chat-a", "tabs", action="list"))["result"]
    assert {t["target_id"] for t in listed["tabs"]} == {"T1", "P-a"}
    assert listed["popup"]["target_id"] == "P-a", "A's own popup is followed"
    assert hub.owner_of_target("P-user") is None


def test_claim_popups_walks_popups_of_popups_in_ledger_order() -> None:
    hub = hub_mod.get_live_hub()
    hub.bind_target("k", "T1")
    relay = MagicMock()
    relay.agent_targets.return_value = ["T1", "P1", "P2", "Q"]
    relay.opener_of.side_effect = {"P1": "T1", "P2": "P1", "Q": "X"}.get
    assert mod.claim_popups(hub, "k", relay) == ["P1", "P2"]
    assert hub.targets_of("k") == {"T1", "P1", "P2"}
    assert hub.target_of("k") == "T1", "claiming never moves the current tab"
    assert mod.claim_popups(hub, "k", relay) == []
    assert mod.claim_popups(hub, "k", None) == []


@pytest.mark.asyncio
async def test_ops_from_different_chats_run_one_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bind-then-op pair of one chat never interleaves with another's."""
    sup = _TabbedSupervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    gate = asyncio.Event()
    parked = asyncio.Event()
    inner = sup.call

    async def slow_call(helper: str, **kwargs: Any) -> Any:
        if helper in ("js", "js_isolated"):
            parked.set()
            await await_until(gate, what="release serialized browser op")
        return await inner(helper, **kwargs)

    sup.call = slow_call  # type: ignore[method-assign]
    first = asyncio.ensure_future(_op("chat-a"))
    second = asyncio.ensure_future(_op("chat-b"))
    try:
        await await_until(parked, what="first browser op entered the page read")
        await until_parked(mod._OP_LOCK, describe=lambda: "second chat awaits the browser lock")
        # A is parked inside its op; B has not even asked for the current tab.
        assert [c[0] for c in sup.calls] == ["current_tab"] * 3
    finally:
        gate.set()
        results = await await_until(asyncio.gather(first, second), what="serialized ops")
    assert all(result["ok"] for result in results)
    assert sup.current == "T-new1"


class _ParkingSupervisor(_Supervisor):
    """``activeField`` parks until ``gate`` is set, so a claim can land mid-op."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.parked = asyncio.Event()

    async def call(self, helper: str, **kwargs: Any) -> Any:
        self.calls.append((helper, kwargs))
        if helper in ("js", "js_isolated") and "activeField" in str(kwargs.get("expression")):
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
    await await_until(sup.parked, what="the supervisor op to park")
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
    # A navigate may go on to finish an edition sign-in inside its own budget.
    worst_slow += mod.SIGN_IN_WORST_S
    assert mod.LOCK_WAIT_S >= mod.SLOW_OP_TIMEOUT_S + mod.SIGN_IN_WORST_S
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


@pytest.mark.asyncio
async def test_follow_current_tab_refuses_private_link_before_streaming(monkeypatch) -> None:
    sup = _Supervisor()
    sup.tab.update(url="http://169.254.169.254/latest", title="private contents")
    streams = _no_stream(monkeypatch)
    hub = hub_mod.get_live_hub()
    hub.turn_started("k", "t")
    out = await mod.follow_current_tab(hub, "k", sup)
    assert out is not None and out["code"] == "redirect_refused"
    assert streams == []
    assert sup.calls[-1] == ("goto", {"url": "about:blank"})
    assert hub.get("k").url == "about:blank"
    assert hub.get("k").title == ""


@pytest.mark.asyncio
async def test_the_dashboard_under_a_name_the_gateway_trusts_is_never_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tunnel, tailnet or ``dashboard.url`` name serves this dashboard, and the agent's
    tab may be the person's own signed-in Chrome: the gateway refuses it, however
    spelled, before the browser sends the request, and a tab that lands there is left.
    Another public site still opens, a loopback origin the operator listed keeps its
    own rule, and an unparsable entry in the set breaks nothing."""
    from kiro_crew.browser_harness import url_gate

    sup = _Supervisor()
    _install(monkeypatch, sup)
    _no_stream(monkeypatch)
    app = web.Application()
    own = {"https://Dash.Example.com/", "http://8.8.8.8:5476", "http://localhost:3000", "http://[x"}
    app["allowed_origins"] = own

    async def _op(op: str, args: dict[str, Any]) -> dict[str, Any]:
        req = make_mocked_request("POST", "/api/browser/op", app=app)
        req["internal_auth"] = True
        req._read_bytes = json.dumps({"op": op, "session_key": "k", "args": args}).encode()
        return json.loads((await mod.api_browser_op(req)).body)

    for op, args in [
        ("navigate", {"url": "HTTPS://Dash.Example.com.:443/settings"}),
        ("navigate", {"url": "http://134744072:5476/"}),
        ("tabs", {"action": "new", "url": "https://dash.example.com/settings"}),
    ]:
        body = await _op(op, args)
        assert body["ok"] is False and body["code"] == "url_blocked", (args, body)
    assert sup.calls == [], "refused before the browser is touched"
    sup.tab["url"] = "https://dash.example.com/settings"
    body = await _op("tabs", {"action": "list"})
    assert body["code"] == "redirect_refused", body
    assert sup.calls[-1] == ("goto", {"url": "about:blank"})
    assert mod.committed_url_allowed("https://dash.example.org/settings", own) is True
    listed = frozenset({"localhost:3000"})
    assert url_gate.url_allowed("http://localhost:3000/", listed, 5476, own) is True
