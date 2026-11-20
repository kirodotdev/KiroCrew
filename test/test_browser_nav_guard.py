"""Every document request of an agent navigation is judged BEFORE it is sent.

The post-commit check judges where a navigation landed, after the browser already
sent each hop. A public URL that redirects to an address the navigate gate refuses
(a loopback OAuth ``redirect_uri``, a link-local metadata service) must not reach
that address at all. The fake relay below stands in for Chrome's ``Fetch`` domain:
``goto`` pauses one request per redirect hop, and the "server" behind a hop counts
a request only when the guard continued it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_browser_harness_ops import FakeBrowser, _committing_page

from kiro_crew.browser_harness import ops
from kiro_crew.browser_harness.nav_guard import GuardUnavailable, NavigationGuard

_PUBLIC = "https://login.example/authorize?redirect_uri=http://localhost:54321/callback"
_LOCAL = "http://localhost:54321/callback?code=secret"


def _gate(url: str) -> bool:
    return not url.startswith(("http://localhost", "http://127.", "http://169.254."))


class FakeRelay:
    """Chrome's Fetch domain on one tab: pauses each hop, delivers it if continued."""

    def __init__(self, *, fail_attach: bool = False, fail_enable: bool = False) -> None:
        self.fail_attach = fail_attach
        self.fail_enable = fail_enable
        self.sent: list[tuple[str, dict[str, Any], str]] = []
        self.on_event: Any = None
        self.detached: list[str] = []
        self.delivered: list[str] = []
        self._urls: dict[str, str] = {}

    async def attach(self, target_id: str, on_event: Any) -> str:
        if self.fail_attach:
            raise RuntimeError("no such target")
        self.on_event = on_event
        return f"S-{target_id}"

    async def detach(self, sid: str) -> None:
        self.detached.append(sid)

    async def send(self, method: str, params: dict[str, Any], sid: str) -> dict[str, Any]:
        self.sent.append((method, params, sid))
        if method == "Fetch.enable" and self.fail_enable:
            raise RuntimeError("Fetch domain unavailable")
        if method == "Fetch.continueRequest":
            self.delivered.append(self._urls[params["requestId"]])
        return {}

    async def hops(self, *urls: str) -> None:
        """Pause one request per hop, in order, as a redirect chain does."""
        for n, url in enumerate(urls):
            rid = f"R{n}"
            self._urls[rid] = url
            await self.on_event(
                {
                    "method": "Fetch.requestPaused",
                    "sessionId": "S-T-agent",
                    "params": {"requestId": rid, "request": {"url": url}},
                }
            )
            await asyncio.sleep(0)

    def methods(self) -> list[str]:
        return [m for m, _p, _s in self.sent]


@pytest.fixture
def fake(tmp_path: Path) -> FakeBrowser:
    return FakeBrowser(tmp_path)


def _guarded_ctx(fake: FakeBrowser, relay: Any, gate: Any = _gate) -> ops.OpContext:
    ctx = fake.ctx()
    ctx.url_allowed = gate
    ctx.nav_guard = lambda: NavigationGuard(relay, "T-agent", gate)
    return ctx


def _goto_with_hops(fake: FakeBrowser, relay: FakeRelay, *urls: str, error: str = "") -> None:
    async def goto(url: str) -> dict[str, Any]:
        if url == "about:blank":
            return {}
        await relay.hops(*urls)
        return {"errorText": error} if error else {"frameId": "F", "loaderId": "L"}

    original = fake.call

    async def call(helper: str, **kwargs: Any) -> Any:
        if helper == "goto":
            fake.calls.append((helper, kwargs))
            return await goto(**kwargs)
        return await original(helper, **kwargs)

    fake.call = call  # type: ignore[method-assign]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_a_redirect_to_a_refused_address_is_never_sent(fake: FakeBrowser) -> None:
    # The report's case: an OAuth authorize URL 302s to a localhost listener.
    relay = FakeRelay()
    _committing_page(fake, "about:blank")
    _goto_with_hops(fake, relay, _PUBLIC, _LOCAL, error="net::ERR_BLOCKED_BY_CLIENT")

    out = _run(ops.run_op("navigate", {"url": _PUBLIC}, _guarded_ctx(fake, relay)))

    assert relay.delivered == [_PUBLIC], "the localhost hop must receive zero requests"
    assert out["ok"] is False and out["code"] == "redirect_refused"
    assert "localhost:54321" in out["error"] and "not sent" in out["error"]
    fail = [p for m, p, _s in relay.sent if m == "Fetch.failRequest"]
    assert fail == [{"requestId": "R1", "errorReason": "BlockedByClient"}]
    assert relay.methods()[0] == "Fetch.enable" and "Fetch.disable" in relay.methods()
    assert relay.detached == ["S-T-agent"]
    assert ("goto", {"url": "about:blank"}) in fake.calls


def test_a_public_redirect_chain_is_continued_hop_by_hop(fake: FakeBrowser) -> None:
    relay = FakeRelay()
    _committing_page(fake, "https://b.example/home")
    _goto_with_hops(fake, relay, "https://a.example/", "https://b.example/home")

    out = _run(ops.run_op("navigate", {"url": "https://a.example/"}, _guarded_ctx(fake, relay)))

    assert out["ok"] is True and out["url"] == "https://b.example/home"
    assert relay.delivered == ["https://a.example/", "https://b.example/home"]
    assert "Fetch.failRequest" not in relay.methods()


@pytest.mark.parametrize("kwargs", [{"fail_attach": True}, {"fail_enable": True}])
def test_a_guard_that_cannot_be_armed_refuses_the_navigation(
    fake: FakeBrowser, kwargs: dict[str, bool]
) -> None:
    relay = FakeRelay(**kwargs)
    _committing_page(fake, "https://example.com/")
    _goto_with_hops(fake, relay, "https://example.com/")

    out = _run(ops.run_op("navigate", {"url": "https://example.com/"}, _guarded_ctx(fake, relay)))

    assert out["ok"] is False and out["code"] == "nav_guard_unavailable"
    assert "goto" not in fake.helper_names(), "nothing is sent unguarded"


def test_no_relay_or_no_tab_refuses_rather_than_skipping_the_gate() -> None:
    for relay, target in ((None, "T-agent"), (FakeRelay(), "")):
        with pytest.raises(GuardUnavailable):
            _run(NavigationGuard(relay, target, _gate).__aenter__())


def test_back_is_guarded_too(fake: FakeBrowser) -> None:
    relay = FakeRelay()
    _committing_page(fake, "https://example.com/")

    def back() -> dict[str, Any]:
        return {"ok": True}

    fake.page["back"] = back

    class _HistoryHop(NavigationGuard):
        # History moves through the page, not ``goto``: the hop pauses once armed.
        async def __aenter__(self) -> NavigationGuard:
            entered = await super().__aenter__()
            await relay.hops("http://169.254.169.254/latest")
            return entered

    ctx = _guarded_ctx(fake, relay)
    ctx.nav_guard = lambda: _HistoryHop(relay, "T-agent", _gate)
    out = _run(ops.run_op("back", {}, ctx))
    assert out["code"] == "redirect_refused"
    assert relay.delivered == []


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("about:blank", True),
        ("data:text/html,hi", True),
        ("blob:https://a.example/1", True),
        ("https://example.com/", True),
        ("http://127.0.0.1:8080/", False),
        ("http://169.254.169.254/", False),
        ("", False),
    ],
)
def test_the_guard_applies_the_navigate_gate(url: str, allowed: bool) -> None:
    def gate(candidate: str) -> bool:
        return candidate.startswith("https://")

    assert NavigationGuard(FakeRelay(), "T", gate).allows(url) is allowed


def test_a_gate_that_raises_blocks_the_request() -> None:
    def gate(_url: str) -> bool:
        raise ValueError("unparseable")

    assert NavigationGuard(FakeRelay(), "T", gate).allows("https://x/") is False


def test_the_route_wires_a_guard_for_the_bound_tab() -> None:
    from kiro_crew.dashboard.handlers import browser_ops as route

    class _Hub:
        def bound_relay(self, _key: str) -> Any:
            return None

        def target_of(self, _key: str) -> str:
            return "T-bound"

    class _Supervisor:
        relay = FakeRelay()

        async def call(self, *_a: Any, **_k: Any) -> None:
            return None

    ctx = route.build_context("k", _Supervisor(), _Hub())  # type: ignore[arg-type]
    guard = ctx.nav_guard()
    assert isinstance(guard, NavigationGuard)
    assert guard._target_id == "T-bound" and guard._relay is _Supervisor.relay
