"""Windows stops Kiro's Chromium with a close request first, then the forced stop.

``terminate_and_reap`` has no graceful step on Windows, so a forced tree kill left
the profile marked ``Crashed`` and lost the cookies Chromium had not committed.
There is no Windows host here: ``IS_WINDOWS`` is faked, the browser is a stand-in
process object, and its debugging endpoint is a loopback websocket this test owns.
The real Windows behaviour is for a maintainer to check on Windows.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import WSMsgType, web

from kiro_crew.browser_harness import supervisor
from kiro_crew.browser_harness.supervisor import HarnessSupervisor
from kiro_crew.testing.wait import await_until, finished

_BROWSER_PATH = "/devtools/browser/test"
_CLOSE_REQUEST = "Browser.close"
_VERSION_REQUEST = "Browser.getVersion"
_FORCED = "forced stop"
_PATHS = ["stop_handle", "abandon_start", "stop_unowned"]
#: A grace short enough to wait out; the close request it bounds never completes.
_SHORT_GRACE = 0.2


class _Browser:
    """Kiro's Chromium as the supervisor sees it: alive until it exits."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdin: Any = None
        self._exited = asyncio.Event()
        #: When set, ``wait`` reports the grace expiring once this event is set.
        self.expire_on: asyncio.Event | None = None

    def exit(self) -> None:
        self.returncode = 0
        self._exited.set()

    async def wait(self) -> int | None:
        if self.expire_on is not None:
            await self.expire_on.wait()
            raise asyncio.TimeoutError
        await self._exited.wait()
        return self.returncode


class _Relay:
    closed = False

    async def close(self) -> None:
        self.closed = True


class _Endpoint:
    """The browser's debugging websocket: records requests, optionally exits on a close."""

    def __init__(self, browser: _Browser, events: list[str]) -> None:
        self.browser = browser
        self.events = events
        self.exits_on_close = True
        self.close_seen = asyncio.Event()
        self.port = 0
        self.product = "HeadlessChrome/154.0.8037.0"
        #: When set, the raw frames sent instead of the version reply, then a hang-up.
        self.version_frames: list[str] | None = None

    async def serve(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type is WSMsgType.TEXT:
                method = json.loads(msg.data)["method"]
                self.events.append(method)
                if method == _VERSION_REQUEST and self.version_frames is not None:
                    for frame in self.version_frames:
                        await ws.send_str(frame)
                    await ws.close()
                    break
                if method == _VERSION_REQUEST:
                    reply = {"id": json.loads(msg.data)["id"], "result": {"product": self.product}}
                    await ws.send_str(json.dumps(reply))
                if method == _CLOSE_REQUEST:
                    self.close_seen.set()
                if method == _CLOSE_REQUEST and self.exits_on_close:
                    self.browser.exit()
        return ws


@pytest_asyncio.fixture
async def endpoint(tmp_path: Path) -> AsyncIterator[_Endpoint]:
    events: list[str] = []
    ep = _Endpoint(_Browser(), events)
    app = web.Application()
    app.router.add_get(_BROWSER_PATH, ep.serve)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    server: Any = site._server
    ep.port = server.sockets[0].getsockname()[1]
    try:
        yield ep
    finally:
        await runner.cleanup()


@pytest.fixture
def sup(tmp_path: Path, endpoint: _Endpoint, monkeypatch: pytest.MonkeyPatch) -> HarnessSupervisor:
    """A Windows supervisor whose forced stop only records that it ran."""
    monkeypatch.setattr(supervisor.platform_compat, "IS_WINDOWS", True)

    async def _forced(proc: Any, **_kw: Any) -> None:
        endpoint.events.append(_FORCED)

    monkeypatch.setattr(supervisor.platform_compat, "terminate_and_reap", _forced)
    s = HarnessSupervisor(tmp_path)

    async def _no_ipc() -> None:
        return None

    s._ipc_shutdown = _no_ipc  # type: ignore[method-assign]
    s.profile_dir.mkdir(parents=True)
    (s.profile_dir / supervisor.DEVTOOLS_PORT_FILE).write_text(
        f"{endpoint.port}\n{_BROWSER_PATH}\n", encoding="utf-8"
    )
    return s


async def _stop(sup: HarnessSupervisor, browser: _Browser, path: str) -> None:
    if path == "stop_handle":
        handle = supervisor.HarnessHandle(
            choice=None,  # type: ignore[arg-type]
            relay=_Relay(),  # type: ignore[arg-type]
            daemon=None,
            worker=None,
            browser=browser,  # type: ignore[arg-type]
        )
        await sup._stop_handle(handle, close_tabs=False)
    elif path == "abandon_start":
        await sup._abandon_start(_Relay(), browser, None)  # type: ignore[arg-type]
    else:
        await supervisor._stop_unowned(browser, sup.profile_dir)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_windows_asks_the_browser_to_close_before_any_forced_stop(
    sup: HarnessSupervisor, endpoint: _Endpoint, path: str
) -> None:
    await finished(_stop(sup, endpoint.browser, path), "the stop")
    assert endpoint.events == [_CLOSE_REQUEST], "a browser that closed needs no forced stop"
    assert endpoint.browser.returncode == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_windows_stops_a_browser_that_ignores_the_close_request(
    sup: HarnessSupervisor, endpoint: _Endpoint, path: str
) -> None:
    """The grace expires only after the request was sent, so the order is not a race."""
    endpoint.exits_on_close = False
    endpoint.browser.expire_on = endpoint.close_seen
    await finished(_stop(sup, endpoint.browser, path), "the stop")
    assert endpoint.events == [_CLOSE_REQUEST, _FORCED]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_windows_close_request_that_never_completes_is_cut_at_the_grace(
    sup: HarnessSupervisor,
    endpoint: _Endpoint,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    """A connection that hangs spends the grace, then the forced stop runs."""

    async def _hang(_url: str) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(supervisor, "request_browser_close", _hang)
    monkeypatch.setattr(supervisor, "_STOP_GRACE_SECS", _SHORT_GRACE)  # flake-ok: bound under test
    await finished(_stop(sup, endpoint.browser, path), "the stop")
    assert endpoint.events == [_FORCED]


@pytest.mark.asyncio
async def test_windows_without_a_debugging_port_goes_straight_to_the_forced_stop(
    sup: HarnessSupervisor, endpoint: _Endpoint
) -> None:
    (sup.profile_dir / supervisor.DEVTOOLS_PORT_FILE).unlink()
    await finished(_stop(sup, endpoint.browser, "stop_handle"), "the stop")
    assert endpoint.events == [_FORCED]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_posix_stop_sends_no_close_request(
    sup: HarnessSupervisor,
    endpoint: _Endpoint,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    monkeypatch.setattr(supervisor.platform_compat, "IS_WINDOWS", False)
    await finished(_stop(sup, endpoint.browser, path), "the stop")
    assert endpoint.events == [_FORCED]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["stop_handle", "abandon_start"])
async def test_a_cancel_during_the_close_request_still_runs_the_forced_stop(
    sup: HarnessSupervisor, endpoint: _Endpoint, path: str
) -> None:
    """A restart's deadline cancels the wait for the exit; the browser must still be killed.

    ``_stop_unowned`` is shielded, so a cancel never reaches its close request.
    """
    endpoint.exits_on_close = False
    task = asyncio.ensure_future(_stop(sup, endpoint.browser, path))
    await await_until(endpoint.close_seen, what="Browser.close to reach the endpoint")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert endpoint.events == [_CLOSE_REQUEST, _FORCED]


# ── a browser an earlier gateway life left on the profile ──

_EARLIER = "ws://127.0.0.1:9/devtools/browser/earlier"
_NEW = "ws://127.0.0.1:9/devtools/browser/new"
_SESSION_FILE = ("Default", "Sessions", "Session_1")
_NAMED = "left by an earlier run is still using"
_HELD = "left by an earlier run may still be using"


class _Earlier:
    """The profile as a start sees it, with an earlier run's browser maybe holding it.

    *answers* is what the endpoint in the port file does with the close request:
    ``"closes"``, ``"stays"`` (took it, never exits), ``"sent_then_stalls"`` (took
    it, the bound ran out in the handshake, never exits), ``"not_chromium"``,
    ``"refuses"`` or ``"stalls"`` (the bound ran out before any reply). A browser
    that closes writes its tabs back, as Chromium does. With *keeps_lock* its
    ``lockfile`` cannot be removed, as while a process still holds it.

    The supervisor's grace is not shortened: every probe after the close request
    moves the module's clock past it, so no outcome waits on real time.
    """

    def __init__(
        self, sup: HarnessSupervisor, clock: Any, *, answers: str, keeps_lock: bool
    ) -> None:
        self.profile = sup.profile_dir
        self.clock = clock
        self.answers = answers
        self.keeps_lock = keeps_lock
        self.live = True
        self.holds = answers in ("closes", "stays", "sent_then_stalls")
        self.requests: list[str] = []
        self.spawned: list[list[str]] = []
        self.forced: list[Any] = []

    @property
    def lock(self) -> Path:
        return self.profile / "lockfile"

    @property
    def port_file(self) -> Path:
        return self.profile / supervisor.DEVTOOLS_PORT_FILE

    async def url(self, profile: Path) -> str | None:
        assert profile == self.profile
        if self.requests:
            self.clock.advance(supervisor._STOP_GRACE_SECS)
        if self.spawned and not self.holds:
            return _NEW
        return _EARLIER if self.live and self.port_file.exists() else None

    async def close(self, url: str, sent: asyncio.Event | None = None) -> bool:
        self.requests.append(url)
        if self.answers == "refuses":
            raise ConnectionError("handshake refused")
        if self.answers == "stalls":
            raise asyncio.TimeoutError  # what the bound raises before any reply
        if self.answers == "not_chromium":
            return False
        assert sent is not None
        sent.set()
        if self.answers == "sent_then_stalls":
            raise asyncio.TimeoutError  # the bound ran out in the close handshake
        if self.answers == "closes":
            self.live = False
            session = self.profile.joinpath(*_SESSION_FILE)
            session.parent.mkdir(parents=True, exist_ok=True)
            session.write_text("tabs of the earlier life", encoding="utf-8")
            if not self.keeps_lock:
                self.lock.unlink()
                self.holds = False
        return True

    async def spawn(self, argv: list[str], **_kw: Any) -> Any:
        assert not self.port_file.exists()
        assert not self.profile.joinpath(*_SESSION_FILE).exists(), "the old tabs come back"
        self.spawned.append(argv)
        browser = _Browser()
        if self.holds:
            browser.returncode = 21  # Chromium hands a launch on a held profile to the holder
        return browser


def _held_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, answers: str, keeps_lock: bool = False
) -> tuple[HarnessSupervisor, _Earlier, Callable[[], Awaitable[Any]]]:
    """A Windows supervisor whose profile holds an earlier run's port file and lock."""
    from kiro_crew.browser_harness import chromium, discover, earlier_chromium, install
    from kiro_crew.testing.clock import ManualClock

    monkeypatch.setattr(supervisor.platform_compat, "IS_WINDOWS", True)
    clock = ManualClock()
    clock.install(monkeypatch, earlier_chromium)
    sup = HarnessSupervisor(tmp_path / "home", base_env={})
    earlier = _Earlier(sup, clock, answers=answers, keeps_lock=keeps_lock)
    sup.profile_dir.mkdir(parents=True)
    earlier.port_file.write_text("9\n/devtools/browser/earlier\n", encoding="utf-8")
    if keeps_lock:
        earlier.lock.mkdir()  # a lock that will not unlink, like one a process holds
        (earlier.lock / "held").write_text("", encoding="utf-8")
    else:
        earlier.lock.write_text("", encoding="utf-8")
    sup._spawn = earlier.spawn  # type: ignore[method-assign]

    async def _forced(proc: Any, **_kw: Any) -> None:
        earlier.forced.append(proc)

    monkeypatch.setattr(supervisor.platform_compat, "terminate_and_reap", _forced)
    monkeypatch.setattr(supervisor, "_devtools_ws_url", earlier.url)
    monkeypatch.setattr(earlier_chromium, "request_chromium_close", earlier.close)
    exe = tmp_path / "chrome.exe"
    exe.write_text("", encoding="utf-8")
    state = install.InstallState(
        prefix=tmp_path,
        installed=True,
        version=supervisor.HARNESS_VERSION,
        python=None,
        chromium=chromium.ChromiumState(
            version="x", platform="win64", present=True, executable=exe
        ),
    )
    choice = discover.BrowserChoice("kiro_chromium", exe, True, sup.profile_dir)
    return sup, earlier, lambda: sup._launch_chromium(choice, state)


@pytest.mark.asyncio
async def test_windows_start_closes_an_earlier_runs_browser_then_launches_without_its_tabs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sup, earlier, start = _held_profile(tmp_path, monkeypatch, answers="closes")
    _proc, url = await finished(start(), "the start")
    assert earlier.requests == [_EARLIER]
    assert len(earlier.spawned) == 1 and url == _NEW
    assert earlier.forced == [], "nothing is ended by pid"


@pytest.mark.asyncio
@pytest.mark.parametrize("answers", ["stays", "sent_then_stalls"])
async def test_an_earlier_browser_that_does_not_close_fails_the_start_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answers: str
) -> None:
    """Asked once: the next start sends no second request and names the holder instead."""
    _sup, earlier, start = _held_profile(tmp_path, monkeypatch, answers=answers)
    with pytest.raises(supervisor.BrowserUnavailable, match=_NAMED):
        await finished(start(), "the start")
    assert earlier.requests == [_EARLIER]
    assert earlier.spawned == [], "nothing is started on a profile another browser holds"
    assert earlier.forced == [], "nothing is ended by pid"
    with pytest.raises(supervisor.BrowserUnavailable, match=_HELD):
        await finished(start(), "the next start")
    assert earlier.requests == [_EARLIER], "the same port is never asked again"
    assert earlier.forced == []


@pytest.mark.asyncio
async def test_a_closed_port_whose_lock_is_still_held_fails_the_start_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A browser still flushing its profile has closed its port but not let go of it."""
    _sup, earlier, start = _held_profile(tmp_path, monkeypatch, answers="closes", keeps_lock=True)
    with pytest.raises(supervisor.BrowserUnavailable, match=_NAMED):
        await finished(start(), "the start")
    assert earlier.requests == [_EARLIER]
    assert earlier.spawned == [], "nothing is started on a profile another browser holds"
    assert earlier.forced == []


@pytest.mark.asyncio
@pytest.mark.parametrize("answers", ["not_chromium", "refuses", "stalls"])
async def test_an_endpoint_not_proven_to_be_the_profiles_browser_launches_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answers: str
) -> None:
    """A stale port file whose port another program now holds: nothing waits on it."""
    _sup, earlier, start = _held_profile(tmp_path, monkeypatch, answers=answers)
    _proc, url = await finished(start(), "the start")
    assert earlier.requests == [_EARLIER]
    assert len(earlier.spawned) == 1 and url == _NEW
    assert earlier.forced == []


@pytest.mark.asyncio
async def test_a_live_port_without_the_profile_lock_is_never_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No process holds the profile, so whatever answers on the port is not its browser."""
    _sup, earlier, start = _held_profile(tmp_path, monkeypatch, answers="closes")
    earlier.lock.unlink()
    earlier.holds = False
    await finished(start(), "the start")
    assert earlier.requests == []
    assert len(earlier.spawned) == 1


@pytest.mark.asyncio
async def test_posix_profile_preparation_asks_no_browser_and_still_clears_the_tabs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.browser_harness import earlier_chromium

    _sup, earlier, _start = _held_profile(tmp_path, monkeypatch, answers="closes")
    monkeypatch.setattr(supervisor.platform_compat, "IS_WINDOWS", False)
    session = earlier.profile.joinpath(*_SESSION_FILE)
    session.parent.mkdir(parents=True)
    session.write_text("tabs", encoding="utf-8")
    await finished(earlier_chromium.prepare_profile(earlier.profile), "the preparation")
    assert earlier.requests == []
    assert not session.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [True, False])
async def test_a_launch_that_exits_on_a_held_profile_names_the_likely_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: bool
) -> None:
    """No port file to find the holder by: the exit text still names it while the lock is held."""
    sup = HarnessSupervisor(tmp_path, base_env={})
    if held:
        (tmp_path / "lockfile").write_text("", encoding="utf-8")
    exited = _Browser()
    exited.returncode = 21

    async def _spawn(argv: list[str], **_kw: Any) -> Any:
        return exited

    async def _no_url(_profile: Path) -> None:
        return None

    async def _forced(proc: Any, **_kw: Any) -> None:
        return None

    sup._spawn = _spawn  # type: ignore[method-assign]
    monkeypatch.setattr(supervisor, "_devtools_ws_url", _no_url)
    monkeypatch.setattr(supervisor.platform_compat, "terminate_and_reap", _forced)
    with pytest.raises(supervisor.BrowserUnavailable) as raised:
        await finished(sup._launch_chromium_port(["chromium"], tmp_path), "the start")
    text = str(raised.value)
    assert text.startswith("Kiro's Chromium exited with code 21")
    assert (_HELD in text) is held


@pytest.mark.asyncio
async def test_the_close_request_goes_only_to_an_endpoint_that_answers_as_chromium(
    endpoint: _Endpoint,
) -> None:
    from kiro_crew.browser_harness.relay import request_chromium_close

    url = f"ws://127.0.0.1:{endpoint.port}{_BROWSER_PATH}"
    endpoint.product = "SomeProxy/1.0"
    assert await finished(request_chromium_close(url), "the probe") is False
    assert endpoint.events == [_VERSION_REQUEST]
    endpoint.product = "HeadlessChrome/154.0.8037.0"
    sent = asyncio.Event()
    assert await finished(request_chromium_close(url, sent), "the close") is True
    assert sent.is_set()
    await await_until(endpoint.close_seen, what="Browser.close to reach the endpoint")
    assert endpoint.events == [_VERSION_REQUEST, _VERSION_REQUEST, _CLOSE_REQUEST]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frames",
    [
        [],
        [json.dumps({"id": 7, "result": {"product": "Chrome/1"}})],
        [json.dumps({"id": 1, "result": "Chrome/1"})],
        [json.dumps(["not", "a", "reply"])],
    ],
    ids=["hangs-up", "other-id-only", "result-not-an-object", "not-an-object"],
)
async def test_an_endpoint_that_never_answers_as_chromium_is_not_asked_to_close(
    endpoint: _Endpoint, frames: list[str]
) -> None:
    from kiro_crew.browser_harness.relay import request_chromium_close

    endpoint.version_frames = frames
    url = f"ws://127.0.0.1:{endpoint.port}{_BROWSER_PATH}"
    assert await finished(request_chromium_close(url), "the probe") is False
    assert endpoint.events == [_VERSION_REQUEST]


@pytest.mark.asyncio
async def test_a_reply_that_is_not_json_raises_like_a_refused_handshake(
    endpoint: _Endpoint,
) -> None:
    from kiro_crew.browser_harness.relay import request_chromium_close

    endpoint.version_frames = ["not json"]
    url = f"ws://127.0.0.1:{endpoint.port}{_BROWSER_PATH}"
    with pytest.raises(ValueError):
        await finished(request_chromium_close(url), "the probe")
    assert endpoint.events == [_VERSION_REQUEST]
