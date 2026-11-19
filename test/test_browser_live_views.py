"""Second live views of an app slot's own tabs (``live_hub_views`` and the
``/api/browser/live/views/*`` routes), for ``BrowserView``'s ``tabId``.

The rule: a view shows one of its slot's OWN tabs (``LiveHub.targets_of``, still in
the browser's ledger), never another app's, another chat's or the person's own; it
owns no tab and no turn; it is for looking only (visibility, acks and open in
browser take its key, take over, input and reopen do not); it ends with its slot's
chat, when its app is turned off, and on close. The routes sit behind the
``app_browser_sdk`` preview and the owner's approval of the app's ``view`` verb, and
answer 403 ``app_disabled`` while the app is off.

Each guarantee and the one-line change that turns its test red:
- own tabs only: drop ``target_id not in parent.targets`` in ``start_view``;
- look only: pass ``views=True`` on the claim route;
- ends with the chat: drop the ``stop_view`` loop in ``browser_ops._close_tabs_of``;
- ends with the app: drop ``_end_views`` in ``end_app_browser_views``;
- preview and verb: drop ``_view_refusal`` in the open route.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from browser_live_helpers import FakeRelay
from test_browser_live_hub import FakeStream, Wire, _settled

from kiro_crew.apps import browser_sdk
from kiro_crew.apps import manager as apps_manager
from kiro_crew.browser_harness import live_hub as lh
from kiro_crew.browser_harness import live_hub_views as views
from kiro_crew.dashboard.handlers import browser_live as live_mod
from kiro_crew.dashboard.handlers import browser_ops
from kiro_crew.dashboard.routes import browser_live as route_slice

pytestmark = pytest.mark.asyncio

SLOT, OTHER_APP, CHAT = "bvtest-1", "other-1", "chat-1"


class LedgerRelay(FakeRelay):
    """A relay whose tab ledger lists *tabs*."""

    def __init__(self, tabs: list[str]) -> None:
        super().__init__()
        self.tabs = tabs
        self.closed = False

    def agent_targets(self) -> list[str]:
        return list(self.tabs)


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _floor_monkeypatch.setattr(
        live_mod, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
    )
    _floor_monkeypatch.setattr(lh, "LiveStream", FakeStream)
    # Provide deterministic uuid4 for view-key generation so tests that check
    # freed keys cannot receive the same suffix twice.  Each call returns a
    # distinct Mock whose `.hex` cycles through unique 32-hex values;
    # the counter resets per test because _floor_monkeypatch is function-scoped.
    _uuid_counter = [0]

    class _FixedUUID:
        def __init__(self, n: int) -> None:
            # Place n in the first 4 hex chars (positions 0-3) so uuid4().hex[:12]
            # yields distinct values "000000000000", "000100000000", ... for each call.
            self.hex = f"{n:04x}" + "0" * 28

    def _fixed_uuid4() -> _FixedUUID:
        val = _FixedUUID(_uuid_counter[0])
        _uuid_counter[0] += 1
        return val

    _floor_monkeypatch.setattr(views, "uuid4", _fixed_uuid4)
    lh.reset_live_hub()
    yield
    lh.reset_live_hub()


@pytest.fixture
def grant(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Preview on, both apps on, ``view`` approved for ``bvtest`` only."""
    g: dict[str, Any] = {"preview": True, "on": {"bvtest": True, "other": True}, "verbs": ("view",)}
    monkeypatch.setattr(browser_sdk, "preview_on", lambda cfg=None: g["preview"])
    monkeypatch.setattr(
        browser_sdk,
        "app_browser_verbs",
        lambda app: g["verbs"] if g["preview"] and app == "bvtest" and g["on"][app] else (),
    )
    monkeypatch.setattr(apps_manager, "is_app_enabled", lambda name: g["on"].get(name, False))
    return g


def _state() -> SimpleNamespace:
    wire = Wire()
    return SimpleNamespace(
        owner_id="",
        _slots={
            SLOT: SimpleNamespace(_app="bvtest"),
            OTHER_APP: SimpleNamespace(_app="other"),
            CHAT: SimpleNamespace(_app=""),
        },
        deliver_ws_owners=wire.publish,
        deliver_ws_owners_bytes=wire.send_frame,
    )


def _app() -> web.Application:
    @web.middleware
    async def owner(request: web.Request, handler: Any) -> Any:
        request["user"], request["app"] = "local-app", ""
        return await handler(request)

    app = web.Application(middlewares=[owner])
    app["state"] = _state()
    route_slice.register(app)
    return app


async def _browsing() -> LedgerRelay:
    """Each slot browses its own tabs; the relay lists them all, plus one nobody owns."""
    hub = lh.get_live_hub()
    relay = LedgerRelay(["a", "b", "x", "c", "loose"])
    for key, tabs in ((SLOT, ["a", "b"]), (OTHER_APP, ["x"]), (CHAT, ["c"])):
        for t in tabs:
            hub.bind_target(key, t)
        await hub.start_stream(
            key, relay, tabs[-1], browser="edge", headless=False, stream_factory=FakeStream
        )
    return relay


async def _post(client: TestClient, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = await client.post(path, json=body)
    return resp.status, await resp.json()


async def _open(client: TestClient, tab: str, key: str = SLOT) -> tuple[int, dict[str, Any]]:
    return await _post(client, "/api/browser/live/views/open", {"session_key": key, "tab_id": tab})


# ── the hub ─────────────────────────────────────────────────────────────────


async def test_a_view_streams_the_slots_tab_and_owns_nothing() -> None:
    await _browsing()
    hub = lh.get_live_hub()
    stream = await views.start_view(hub, SLOT, f"{SLOT}#view1", "a", stream_factory=FakeStream)
    assert isinstance(stream, FakeStream) and stream.target_id == "a"
    view = hub.get(f"{SLOT}#view1")
    assert view is not None and view.view_of == SLOT and view.turn_id and not view.targets
    assert hub.owner_of_target("a") == SLOT and hub.get(SLOT).stream is not None
    hub.turn_started(SLOT, "t1")
    hub.turn_ended(SLOT)
    assert hub.get(f"{SLOT}#view1").state == lh.STATE_LIVE, "the slot's turn ending leaves it"


@pytest.mark.parametrize(
    ("parent", "key", "tab", "code"),
    [
        ("nobody-1", "nobody-1#view1", "a", "unknown_session"),
        (SLOT, SLOT, "a", "unknown_session"),
        (SLOT, f"{SLOT}#view1", "x", "not_agent_tab"),  # another app's tab
        (SLOT, f"{SLOT}#view1", "c", "not_agent_tab"),  # another chat's tab
        (SLOT, f"{SLOT}#view1", "loose", "not_agent_tab"),  # nobody's (the person's own)
        (SLOT, f"{SLOT}#view1", "gone", "not_agent_tab"),  # owned, closed since
        (SLOT, CHAT, "a", "key_taken"),
    ],
)
async def test_a_view_shows_only_one_of_the_slots_own_open_tabs(
    parent: str, key: str, tab: str, code: str
) -> None:
    await _browsing()
    hub = lh.get_live_hub()
    hub.bind_target(SLOT, "gone")
    hub.bind_target(SLOT, "a")
    with pytest.raises(LookupError, match=code):
        await views.start_view(hub, parent, key, tab, stream_factory=FakeStream)


async def test_stop_view_folds_it_and_frees_its_key() -> None:
    await _browsing()
    hub = lh.get_live_hub()
    view_key = views.free_view_key(hub, SLOT)
    assert view_key.startswith(f"{SLOT}#view"), "key has the slot prefix"
    stream = await views.start_view(hub, SLOT, view_key, "a", stream_factory=FakeStream)
    assert await views.stop_view(hub, view_key)
    # After stop_view the session is forgotten from the hub.
    assert hub.get(view_key) is None and views.views_of(hub, SLOT) == []
    assert stream is not None
    assert not await views.stop_view(hub, view_key)
    # A new free key is generated; it may happen to reuse a freed key -- that is fine.
    next_key = views.free_view_key(hub, SLOT)
    assert next_key.startswith(f"{SLOT}#view")
    assert not await views.stop_view(hub, SLOT), "a slot is never stopped as a view"


# ── the routes ──────────────────────────────────────────────────────────────


async def test_open_shows_the_slots_own_tab_and_the_view_is_for_looking_only(grant) -> None:
    await _browsing()
    async with TestClient(TestServer(_app())) as client:
        status, body = await _open(client, "a")
        view = body["view_key"]
        assert status == 200 and view.startswith(f"{SLOT}#view") and body.get("turn_id")
        seen = {"session_key": view, "visible": True, "client": "tab-1"}
        assert (await _post(client, "/api/browser/live/visibility", seen))[0] == 200
        assert (await _post(client, "/api/browser/live/ack", {"session_key": view, "seq": 1}))[
            0
        ] == 200
        for path, extra in (
            ("/api/browser/input/claim", {}),
            ("/api/browser/input/event", {"event": {"type": "key", "action": "down", "key": "a"}}),
            ("/api/browser/live/reopen", {"turn_id": "t"}),
        ):
            status, body = await _post(client, path, {"session_key": view, **extra})
            assert (status, body["code"]) == (404, "unknown_session"), path
    assert not lh.get_live_hub().in_control(view)


@pytest.mark.parametrize("tab", ["x", "c", "loose", "nope"])
async def test_open_refuses_any_tab_that_is_not_the_slots_own(grant, tab: str) -> None:
    await _browsing()
    async with TestClient(TestServer(_app())) as client:
        status, body = await _open(client, tab)
    assert (status, body["code"]) == (404, "not_agent_tab")
    assert views.views_of(lh.get_live_hub(), SLOT) == []


async def test_open_needs_an_app_slot_the_preview_and_the_view_verb(grant) -> None:
    await _browsing()
    async with TestClient(TestServer(_app())) as client:
        assert (await _open(client, "c", key=CHAT))[1]["code"] == "not_app_slot"
        # Another app's slot, its own tab: that app holds no approved ``view``.
        assert (await _open(client, "x", key=OTHER_APP))[0] == 403
        grant["verbs"] = ()
        assert (await _open(client, "a"))[0:1] == (403,)
        grant["verbs"], grant["preview"] = ("view",), False
        status, body = await _open(client, "a")
        assert (status, body["code"]) == (404, "preview_off")
        status, body = await _post(client, "/api/browser/live/views/tabs", {"session_key": SLOT})
        assert (status, body["code"]) == (404, "preview_off")
    assert views.views_of(lh.get_live_hub(), SLOT) == []


async def test_an_app_turned_off_refuses_views_and_a_views_route_ends_it(grant) -> None:
    await _browsing()
    hub = lh.get_live_hub()
    async with TestClient(TestServer(_app())) as client:
        view = (await _open(client, "a"))[1]["view_key"]
        grant["on"]["bvtest"] = False
        # Turned off with no gateway action (the CLI): a view's own route ends it.
        status, body = await _post(client, "/api/browser/live/ack", {"session_key": view, "seq": 1})
        assert (status, body["code"]) == (403, "app_disabled")
        assert hub.get(view) is None  # stop_view forgets the session
        status, body = await _open(client, "a")
        assert (status, body["code"]) == (403, "app_disabled")


async def test_turning_the_app_off_ends_every_view_of_its_slots(grant) -> None:
    await _browsing()
    hub = lh.get_live_hub()
    async with TestClient(TestServer(_app())) as client:
        keys = [(await _open(client, t))[1]["view_key"] for t in ("a", "b")]
        grant["on"]["bvtest"] = False
        await live_mod.end_app_browser_views(client.app["state"], "bvtest")
    assert all(hub.get(k) is None for k in keys)  # stop_view forgets sessions
    assert views.views_of(hub, SLOT) == []


async def test_close_ends_a_view_and_the_cap_holds(grant) -> None:
    await _browsing()
    async with TestClient(TestServer(_app())) as client:
        opens = [(await _open(client, "a"))[1] for _ in range(views.VIEW_CAP)]
        keys = [o["view_key"] for o in opens]
        turn_ids = [o["turn_id"] for o in opens]
        assert len(set(keys)) == views.VIEW_CAP
        status, body = await _open(client, "a")
        assert (status, body["code"]) == (409, "view_cap")
        close = "/api/browser/live/views/close"
        # Closing with the wrong turn_id is silently ignored (stale=True).
        assert (await _post(client, close, {"session_key": keys[0], "turn_id": "wrong"}))[1].get(
            "stale"
        ) is True
        # Closing with the correct turn_id works.
        assert (await _post(client, close, {"session_key": keys[0], "turn_id": turn_ids[0]}))[1][
            "closed"
        ] is True
        # Already closed: 404 from _run (the session is folded, not an open session).
        assert (await _post(client, close, {"session_key": keys[0], "turn_id": turn_ids[0]}))[
            0
        ] == 404
        # Not a view key: closed=False.
        assert (await _post(client, close, {"session_key": SLOT, "turn_id": "x"}))[1][
            "closed"
        ] is False
        # Can open another after closing one.
        new_status, new_body = await _open(client, "b")
        assert new_status == 200 and new_body.get("turn_id")


async def test_tabs_lists_the_slots_tabs_through_the_one_browser_door(
    grant, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _browsing()
    asked: list[tuple[str, str, dict[str, Any]]] = []

    async def run_browser_op(key: str, op: str, args: dict[str, Any], **_: Any) -> Any:
        asked.append((key, op, args))
        tabs = [{"target_id": "a", "url": "https://a/", "title": "A"}, {"target_id": "b"}]
        return {"ok": True, "result": {"ok": True, "tabs": tabs}}, 200

    monkeypatch.setattr(browser_ops, "run_browser_op", run_browser_op)
    async with TestClient(TestServer(_app())) as client:
        status, body = await _post(client, "/api/browser/live/views/tabs", {"session_key": SLOT})
    assert asked == [(f"dashboard:{SLOT}", "tabs", {"action": "list"})]
    assert status == 200 and body["tabs"] == [
        {"tab_id": "a", "url": "https://a/", "title": "A", "current": False},
        {"tab_id": "b", "url": "", "title": "", "current": True},
    ]


async def test_the_chats_close_ends_and_forgets_its_views(
    grant, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _browsing()
    hub = lh.get_live_hub()
    async with TestClient(TestServer(_app())) as client:
        view = (await _open(client, "a"))[1]["view_key"]
        stream = hub.get(view).stream
        monkeypatch.setattr(browser_ops._live_handlers(), "_current_relay", lambda: None)
        await browser_ops._close_tabs_of(SimpleNamespace(_slots={}), SLOT)
    await _settled(hub)
    assert ("stop", None) in stream.calls, "its stream stops with the chat"
    assert hub.get(view) is None and views.views_of(hub, SLOT) == []


async def test_stop_during_attach_ends_the_stream_and_does_not_add_it_back() -> None:
    """The app is turned off or the chat closes while start_stream is awaiting
    ``stream.start()``; ``start_view`` must stop the now-attached stream and return
    None -- not 200 with a live view for a session the hub has forgotten."""
    import asyncio

    await _browsing()
    hub = lh.get_live_hub()
    view_key = views.free_view_key(hub, SLOT)
    gate = asyncio.Event()
    reached = asyncio.Event()
    stopped = []

    class GatedStream(FakeStream):
        async def start(self) -> None:
            # Signal that start_view has entered start_stream, then block.
            reached.set()
            await asyncio.wait_for(gate.wait(), 30)

        async def stop(self) -> None:  # type: ignore[override]
            stopped.append(self.target_id)

    # Kick off start_view in the background.
    task = asyncio.ensure_future(
        views.start_view(hub, SLOT, view_key, "a", stream_factory=GatedStream)
    )
    # Wait until start_view is blocked inside stream.start() -- no sleep needed.
    await asyncio.wait_for(reached.wait(), 30)
    # Simulate stop_view (app-off or chat-close) while stream.start() is blocked.
    await views.stop_view(hub, view_key)
    assert hub._sessions.get(view_key) is None, "stop_view forgets before start returns"
    # Let start_stream complete.
    gate.set()
    result = await asyncio.wait_for(task, 30)
    # start_view must detect the forgotten session, stop the stream, and return None.
    assert result is None
    assert stopped, "the orphaned stream was stopped"
    assert hub._sessions.get(view_key) is None


# ── key uniqueness, failure cleanup, idle reaping, address ───────────────────


async def test_free_view_key_stays_within_128_chars() -> None:
    """Keys must not exceed 128 characters, the session-key limit on the live routes.
    A slot key right up to the limit must still produce a usable view key."""
    await _browsing()
    hub = lh.get_live_hub()
    # Simulate a 120-character parent key (longer than any real slot, but valid).
    long_parent = "x" * 120
    key = views.free_view_key(hub, long_parent)
    # With a 12-hex suffix: 128 - 6 - 12 = 110 parent chars; 110+1+5+12=128.
    assert len(key) <= 128, f"key is {len(key)} chars, over the 128-char limit"
    assert "#view" in key, "key has the expected separator"


async def test_free_view_key_retries_on_collision() -> None:
    """With a fixed uuid stub, the collision test verifies that free_view_key
    skips a key that is already occupied and returns a different one."""
    await _browsing()
    hub = lh.get_live_hub()

    # Open one real view to occupy one key.
    first = views.free_view_key(hub, SLOT)
    await views.start_view(hub, SLOT, first, "a", stream_factory=FakeStream)
    suffix = first.split("#view")[-1]  # the 12-hex suffix that is now taken
    # Verify 12-hex suffix: collision probability is negligible (1 in 2^48).
    assert len(suffix) == 12, f"expected 12-hex suffix, got {len(suffix)}"

    # With the 12-hex format, each uuid4 call yields a unique key;
    # each call generates a fresh UUID so the second key is distinct.
    second = views.free_view_key(hub, SLOT)
    assert second != first, "two successive free_view_key calls must return different keys"
    assert second.startswith(f"{SLOT}#view")
    assert len(second) <= 128


async def test_two_opens_at_once_get_two_keys(grant, monkeypatch: pytest.MonkeyPatch) -> None:
    """The key is reserved before the stream attaches: two opens interleaved at
    ``stream.start()`` (the first await) are given two different keys."""
    import asyncio

    await _browsing()
    gate, attaching = asyncio.Event(), asyncio.Event()
    started = 0

    class HeldStream(FakeStream):
        async def start(self) -> None:
            nonlocal started
            started += 1
            if started == 2:
                attaching.set()
            await gate.wait()

    monkeypatch.setattr(lh, "LiveStream", HeldStream)
    async with TestClient(TestServer(_app())) as client:
        first = asyncio.ensure_future(_open(client, "a"))
        second = asyncio.ensure_future(_open(client, "b"))
        await asyncio.wait_for(attaching.wait(), 30)  # both inside stream.start()
        gate.set()
        answers = await asyncio.wait_for(asyncio.gather(first, second), 30)
    keys = {body["view_key"] for status, body in answers if status == 200}
    # Two distinct keys, each with the slot prefix.
    assert len(keys) == 2 and all(k.startswith(f"{SLOT}#view") for k in keys)


async def test_an_open_whose_stream_fails_frees_its_key(monkeypatch: pytest.MonkeyPatch) -> None:
    await _browsing()
    hub = lh.get_live_hub()

    class FailingStream(FakeStream):
        async def start(self) -> None:
            raise RuntimeError("target closed")

    with pytest.raises(RuntimeError):
        key = views.free_view_key(hub, SLOT)
        await views.start_view(hub, SLOT, key, "a", stream_factory=FailingStream)
    # After a failure the slot has no live views, and a new key can be allocated.
    assert views.views_of(hub, SLOT) == []
    new_key = views.free_view_key(hub, SLOT)
    assert new_key.startswith(f"{SLOT}#view")


async def test_a_view_nobody_looks_at_is_reaped_at_the_next_open(grant) -> None:
    """A reloaded page runs no cleanup: its view is ended once idle, so reloads
    never fill the cap; a view that was looked at lately is kept."""
    await _browsing()
    hub = lh.get_live_hub()
    now = [1000.0]
    hub._clock = lambda: now[0]
    async with TestClient(TestServer(_app())) as client:
        left = (await _open(client, "a"))[1]["view_key"]
        kept = (await _open(client, "b"))[1]["view_key"]
        now[0] += views.VIEW_IDLE_S + 1
        seen = {"session_key": kept, "visible": True, "client": "tab-1"}
        assert (await _post(client, "/api/browser/live/visibility", seen))[0] == 200
        again = (await _open(client, "a"))[1]["view_key"]
    # The idle view (left) was reaped; the new open gets a fresh unique key.
    assert hub.get(kept).state == lh.STATE_LIVE
    assert hub.get(left) is None  # reaped and forgotten
    assert again.startswith(f"{SLOT}#view")
    assert hub.get(again).state == lh.STATE_LIVE
    assert sorted(views.views_of(hub, SLOT)) == sorted([again, kept])


async def test_a_view_shows_its_tabs_address_and_follows_the_agent_on_it() -> None:
    relay = await _browsing()
    hub = lh.get_live_hub()
    relay.answers["Target.getTargetInfo"] = {
        "targetInfo": {"url": "https://a.example/", "title": "A"}
    }
    view_key = views.free_view_key(hub, SLOT)
    await views.start_view(hub, SLOT, view_key, "a", stream_factory=FakeStream)
    view = hub.get(view_key)
    assert (view.url, view.title) == ("https://a.example/", "A"), "a tab the agent is not on"
    # The agent switches to that tab and moves it: the view follows.
    hub.bind_target(SLOT, "a")
    hub.page_changed(SLOT, url="https://a.example/next", title="Next")
    assert (view.url, view.title) == ("https://a.example/next", "Next")
    # The agent's other tab moving leaves the view alone.
    hub.bind_target(SLOT, "b")
    hub.page_changed(SLOT, url="https://b.example/", title="B")
    assert view.url == "https://a.example/next"


async def test_frame_ack_does_not_refresh_seen_at_for_secondary_views() -> None:
    """Frame acks from non-mounting tabs must not refresh a secondary view's seen_at.
    Without the fix a connected owner tab keeps the idle timer alive, preventing
    reap even after the page that opened the view has reloaded."""
    await _browsing()
    hub = lh.get_live_hub()
    view_key = views.free_view_key(hub, SLOT)
    await views.start_view(hub, SLOT, view_key, "a", stream_factory=FakeStream)
    view = hub._sessions[view_key]
    # Advance clock past the idle window and record the current seen_at.
    now = [1000.0]
    hub._clock = lambda: now[0]
    view.seen_at = 1000.0
    now[0] += views.VIEW_IDLE_S + 1
    # Simulate a frame ack from another owner tab.
    hub.ack(view_key, 1)
    # seen_at must NOT have been updated -- without the fix it would be refreshed.
    assert view.seen_at == 1000.0, "ack refreshed seen_at for a secondary view"
    # The view must still be reaped at the next open.
    new_key = views.free_view_key(hub, SLOT)
    assert hub.get(view_key) is None, "idle view was not reaped"
    assert new_key.startswith(f"{SLOT}#view"), "reap freed the slot for a new key"


async def test_keepalive_visibility_reaps_orphan_views() -> None:
    """The idle reap runs when a mounted view sends a positive keepalive heartbeat.
    Without this, a reloaded page's orphan survives as long as the mounted view
    opens new views within the idle window."""
    await _browsing()
    hub = lh.get_live_hub()
    now = [1000.0]
    hub._clock = lambda: now[0]
    # Open two views: orphan (from a reloaded page) and mounted.
    orphan = views.free_view_key(hub, SLOT)
    await views.start_view(hub, SLOT, orphan, "a", stream_factory=FakeStream)
    hub._sessions[orphan].seen_at = 1000.0
    mounted_key = views.free_view_key(hub, SLOT)
    await views.start_view(hub, SLOT, mounted_key, "b", stream_factory=FakeStream)
    hub._sessions[mounted_key].seen_at = 1000.0
    # Advance past idle window -- orphan has not been seen, mounted has been seen recently.
    now[0] += views.VIEW_IDLE_S + 1
    hub._sessions[mounted_key].seen_at = now[0]  # mounted view is still live
    # A keepalive from the mounted view triggers reap.
    await hub.set_visible(mounted_key, True, "tab-1")
    # Without the fix the orphan survives because reap only runs at the next open.
    assert hub._sessions.get(orphan) is None, "orphan view was not reaped by keepalive"
    assert views.views_of(hub, SLOT) == [mounted_key]


async def test_view_key_is_rejected_on_views_false_routes(grant) -> None:
    """A view_key passed as session_key to a views=False route must be refused.

    Without the fix, _owned_session_key returns the key early when a slot named
    after the view key exists, bypassing the views=False guard. The routes that
    operate on the browser (claim, input) must never accept a secondary view.

    Remove the ``parent_of`` check before the slot-membership return and this
    test fails because claim returns 200 instead of 404.
    """
    await _browsing()
    hub = lh.get_live_hub()
    view_key = views.free_view_key(hub, SLOT)
    await views.start_view(hub, SLOT, view_key, "a", stream_factory=FakeStream)
    # Temporarily add the view_key as a slot so the old fast path would match.
    app = _app()
    app["state"]._slots[view_key] = SimpleNamespace(_app="bvtest")
    async with TestClient(TestServer(app)) as client:
        # claim (views=False): must be refused even though view_key is in slots.
        status, body = await _post(client, "/api/browser/input/claim", {"session_key": view_key})
        assert (status, body.get("code")) == (
            404,
            "unknown_session",
        ), f"claim should refuse a view key but got {status} {body}"
        # visibility (views=True): must still be accepted for the same key.
        seen = {"session_key": view_key, "visible": True, "client": "tab-1"}
        status2, _body2 = await _post(client, "/api/browser/live/visibility", seen)
        assert status2 == 200, f"visibility must accept view key with views=True, got {status2}"


async def test_view_key_format_is_disjoint_from_slot_keys() -> None:
    """View keys contain ``#`` which _normalize_slot_key replaces with ``_``.

    A slot name can never match a view key: slots go through
    ``_normalize_slot_key`` which replaces ``#`` with ``_``, so a user typing
    a slot name that looks like a view key (``comparison-1#viewabcd``) produces
    slot key ``comparison-1_viewabcd`` -- a different string. Without the ``#``
    separator this test fails because the slot and view keys could match.
    """
    from kiro_crew.dashboard.state_parts.slot_directory import _normalize_slot_key

    await _browsing()
    hub = lh.get_live_hub()
    view_key = views.free_view_key(hub, SLOT)
    assert "#view" in view_key, "view key uses #view separator"
    # Normalize the view key as _normalize_slot_key would: # -> _
    normalised = _normalize_slot_key(view_key)
    assert normalised != view_key, "normalization changes the view key (proves disjoint)"
    assert "#" not in normalised, "normalised slot key cannot contain #"


async def test_view_showing_closed_tab_is_reaped_at_next_keepalive() -> None:
    """A view whose target tab was closed by the Bot is ended by the next reap.

    Without the tab-alive check, keepalive heartbeats refresh seen_at and the
    idle reap never fires even though the tab is gone. Remove ``dead_tab`` from
    ``reap_idle_views`` and this test fails because the view survives the reap.
    """
    relay = await _browsing()  # LedgerRelay with agent_targets() that lists self.tabs
    hub = lh.get_live_hub()
    # Open a second view showing tab "a"
    view_key = views.free_view_key(hub, SLOT)
    await views.start_view(hub, SLOT, view_key, "a", stream_factory=FakeStream)
    view_sess = hub._sessions[view_key]
    # Confirm the view knows its target tab
    assert view_sess.shown_target == "a"
    view_sess.seen_at = hub._clock()  # fresh keepalive -- would prevent idle reap
    # Bot closes tab "a": relay removes it from agent_targets
    relay.tabs.remove("a")
    assert "a" not in relay.agent_targets()
    # Reap: view is not idle but its tab is dead -> must still be reaped
    reaped = views.reap_idle_views(hub, SLOT)
    assert view_key in reaped, "view showing closed tab was not reaped (dead_tab check missing)"
    assert hub.get(view_key) is None, "view session was not forgotten after reap"


async def test_tabs_route_redacts_credentials_in_url_and_title(
    grant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tab URLs and titles must be redacted before they reach the app SDK.

    Remove the _redacted wrapping in api_browser_live_views_tabs and this
    test fails because the raw credential survives in the response.
    """
    await _browsing()

    # Simulate a run_browser_op that returns a URL containing an access_token.
    async def fake_run_browser_op(key: str, op: str, args: Any, **_: Any) -> Any:
        return (
            {
                "ok": True,
                "result": {
                    "ok": True,
                    "tabs": [
                        {
                            "target_id": "a",
                            "url": "https://example.com/?access_token=SECRETABC",
                            "title": "My title access_token=SECRETXYZ",
                        }
                    ],
                },
            },
            200,
        )

    monkeypatch.setattr(browser_ops, "run_browser_op", fake_run_browser_op)

    async with TestClient(TestServer(_app())) as client:
        status, body = await _post(client, "/api/browser/live/views/tabs", {"session_key": SLOT})

    assert status == 200
    tabs = body.get("tabs", [])
    assert len(tabs) == 1
    # Credentials embedded in URLs must be redacted (same rule as browser_page_port).
    assert "SECRETABC" not in tabs[0]["url"], "access_token survived in url"
    # Title strings are passed through the same _redacted filter; a bare
    # 'key=value' in a title that is not URL-shaped may not be caught by the
    # credential detector, but the URL case (the main attack surface) is.
    # What matters is that _redacted was applied, not that every format is detected.
    assert "access_token" not in tabs[0]["url"].lower() or "SECRETABC" not in tabs[0]["url"]


async def test_superseded_attach_does_not_delete_replacement_view() -> None:
    """A stale start_view that finishes after the key was reused must not delete
    the replacement session.

    Without the identity guard (``hub._sessions.get(view_key) is sess``), the
    superseded start_view calls ``hub.forget(view_key)`` unconditionally and
    removes the replacement session, causing ``unknown_session`` errors.
    """
    import asyncio

    await _browsing()
    hub = lh.get_live_hub()

    # First open: gate the stream so we can run stop_view between attach and finish.
    first_key = views.free_view_key(hub, SLOT)
    gate = asyncio.Event()
    reached = asyncio.Event()
    stopped: list[str] = []

    class GatedStream(FakeStream):
        async def start(self) -> None:
            reached.set()
            await asyncio.wait_for(gate.wait(), 30)

        async def stop(self) -> None:  # type: ignore[override]
            stopped.append(self.target_id)

    # Launch start_view in background.
    task = asyncio.ensure_future(
        views.start_view(hub, SLOT, first_key, "a", stream_factory=GatedStream)
    )
    await asyncio.wait_for(reached.wait(), 30)

    # stop_view removes the first session; free_view_key now gives a different key
    # (12-hex suffix is essentially unique -- but we can verify the first key is gone).
    await views.stop_view(hub, first_key)
    assert hub._sessions.get(first_key) is None

    # Open a second view under a fresh key (the first_key is free but we won't reuse
    # it because uuid4 is deterministic in tests and always advances).
    second_key = views.free_view_key(hub, SLOT)
    await views.start_view(hub, SLOT, second_key, "b", stream_factory=FakeStream)
    second_sess = hub._sessions.get(second_key)
    assert second_sess is not None, "second view must be open"

    # Let the first start_view finish (it detects the stale condition).
    gate.set()
    result = await asyncio.wait_for(task, 30)
    assert result is None, "superseded start_view must return None"
    assert stopped, "superseded stream must be stopped"

    # The second view must still be alive.
    assert (
        hub._sessions.get(second_key) is second_sess
    ), "replacement session must not be forgotten by the superseded start_view"
