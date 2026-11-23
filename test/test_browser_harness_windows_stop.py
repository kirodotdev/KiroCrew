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
from collections.abc import AsyncIterator
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

    async def serve(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type is WSMsgType.TEXT:
                method = json.loads(msg.data)["method"]
                self.events.append(method)
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
