"""The panel browser talks to its gateway only, cannot be taken hold of, and never hangs on a question.

Rule 5 read strictly (``panel_fence.py``), the debugging transport that nothing
can open again and the host check beside it (``panel_browser.py``), the windows
a page opens, the browser's dialogs, and a panel that outlives the renewal of
the owner's access cookie (``panel_hub.py``). No browser starts here: the fake
relay of ``browser_panel_helpers`` stands in, and time never passes.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from browser_panel_helpers import ORIGIN, Client, make_hub, open_live

from kiro_crew.browser_harness import panel_browser as pb
from kiro_crew.browser_harness import panel_cover, panel_fence
from kiro_crew.browser_harness import panel_hub as ph
from kiro_crew.browser_harness import panel_page, supervisor
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

TOKEN_A = "token-of-the-owner-a"

# ── the fence ──────────────────────────────────────────────────────────────


def test_the_fence_sends_everything_but_the_gateway_to_a_proxy_that_cannot_be_reached() -> None:
    flags = panel_fence.launch_flags("http://127.0.0.1:6777")
    assert f"--proxy-server={panel_fence.FENCE_PROXY}" in flags
    assert panel_fence.FENCE_PROXY.split("//")[1].split(":")[0].endswith(".invalid")
    # The implicit rule that sends the loopback direct is taken away; only the gateway's port goes direct.
    assert "--proxy-bypass-list=<-loopback>;127.0.0.1:6777" in flags
    # No host resolves but the gateway's own, so the proxy's name is never looked up.
    assert "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1" in flags
    # The spelling that the real browser was seen to obey: with it no datagram left.
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in flags


def test_the_fence_keeps_the_brackets_of_an_ipv6_gateway_in_its_bypass_rule() -> None:
    flags = panel_fence.launch_flags("http://[::1]:6777")
    assert "--proxy-bypass-list=<-loopback>;[::1]:6777" in flags
    assert "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE ::1" in flags


@pytest.mark.parametrize(
    "origin",
    ["", "127.0.0.1:6777", "http://127.0.0.1", "ftp://127.0.0.1:21", "http://a:b@127.0.0.1:6777"],
)
def test_no_fence_is_built_for_an_origin_that_is_not_a_plain_host_and_port(origin: str) -> None:
    with pytest.raises(panel_fence.FenceError):
        panel_fence.launch_flags(origin)
    with pytest.raises(panel_fence.FenceError):
        panel_fence.launch_flags(origin + "/chat" if origin else "http://127.0.0.1:6777/chat")


def test_inside_the_fence_are_the_gateway_and_addresses_of_the_pages_own_making() -> None:
    for url in (ORIGIN + "/api/x", ORIGIN + "/", "about:blank", "about:srcdoc", "blob:x", "data:,"):
        assert panel_fence.passes(url, ORIGIN) is True
    for url in (
        "http://127.0.0.1:1234/",
        "http://localhost:6777/",
        "http://10.0.0.5/",
        "https://example.com/",
        "ws://127.0.0.1:6777/",
        "file:///etc/passwd",
        "",
        None,
    ):
        assert panel_fence.passes(url, ORIGIN) is False


# ── documents, dialogs and windows, in the hub ─────────────────────────────


@pytest.mark.asyncio
async def test_a_frames_document_that_leaves_the_gateway_is_failed_without_a_word() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    told = len(client.told)
    for n, url in enumerate(
        ["http://127.0.0.1:1234/x", "http://10.0.0.5/", "https://evil.example/"]
    ):
        await relay.push(
            panel.target_id,
            "Fetch.requestPaused",
            {"requestId": f"r{n}", "frameId": "F-inner", "request": {"url": url}},
        )
    assert [p["requestId"] for _, p, _ in relay.methods("Fetch.failRequest")] == ["r0", "r1", "r2"]
    assert relay.methods("Fetch.continueRequest") == []
    # A frame inside a page is not the panel's own address: the panel says nothing for it.
    assert client.told[told:] == []


@pytest.mark.asyncio
async def test_a_frames_document_of_the_gateway_or_of_the_pages_own_making_goes_on() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    urls = [
        ORIGIN + "/api/artifacts/x/frame",
        "about:srcdoc",
        "blob:" + ORIGIN + "/1",
        "data:text/html,x",
    ]
    for n, url in enumerate(urls):
        await relay.push(
            panel.target_id,
            "Fetch.requestPaused",
            {"requestId": f"ok{n}", "frameId": "F-inner", "request": {"url": url}},
        )
    assert [p["requestId"] for _, p, _ in relay.methods("Fetch.continueRequest")] == [
        "ok0",
        "ok1",
        "ok2",
        "ok3",
    ]
    assert relay.methods("Fetch.failRequest") == []


@pytest.mark.asyncio
async def test_a_browser_dialog_is_answered_with_a_no_at_once_and_the_panel_says_so() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    panel = await open_live(hub, browser, client)
    relay = browser.relay
    for kind in ("alert", "confirm", "prompt", "beforeunload"):
        await relay.push(panel.target_id, "Page.javascriptDialogOpening", {"type": kind})
    answers = relay.methods("Page.handleJavaScriptDialog")
    assert [p for _, p, _ in answers] == [{"accept": False}] * 4
    assert {sid for _, _, sid in answers} == {panel.stream.session_id}
    assert [m for m in client.told if m.get("op") == "refused"] == [
        {"op": "refused", "id": "a", "why": "dialog"}
    ] * 4


@pytest.mark.asyncio
async def test_a_window_that_a_page_opened_gives_its_panel_one_line() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    first = await open_live(hub, browser, client, "a")
    second = await open_live(hub, browser, client, "b")
    assert browser.on_stray is not None
    await browser.on_stray(
        {"targetId": "X", "browserContextId": "ctx1", "openerId": second.target_id}
    )
    assert client.last("refused") == {"op": "refused", "id": "b", "why": "window"}
    # The browser did not say which page opened it: the socket's first panel says the line.
    await browser.on_stray({"targetId": "Y", "browserContextId": "ctx1"})
    assert client.last("refused") == {"op": "refused", "id": first.id, "why": "window"}
    told = len(client.told)
    await browser.on_stray({"targetId": "Z", "browserContextId": "another-context"})
    assert len(client.told) == told


@pytest.mark.asyncio
async def test_a_page_whose_window_was_closed_is_fitted_and_laid_out_again() -> None:
    # The window a page opens is a tab of the page's own window and comes in front. Once it is
    # closed the page's tab is in front again with its window's own view, as after a show.
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    relay = browser.relay
    relay.window_bounds = {"left": 20, "top": 20, "width": 1280, "height": 800}
    relay.view = {"clientWidth": 1280, "clientHeight": 657}
    first = await open_live(hub, browser, client, "a")
    second = await open_live(hub, browser, client, "b")
    names = ("Browser.setWindowBounds", "Emulation.setDeviceMetricsOverride")

    def since(mark: int) -> list[tuple[str, Any]]:
        return [
            (m, p.get("windowId") if m == names[0] else sid)
            for m, p, sid in relay.calls[mark:]
            if m in names
        ]

    assert browser.on_stray_closed is not None
    mark = len(relay.calls)
    await browser.on_stray_closed(
        {"targetId": "X", "browserContextId": "ctx1", "openerId": second.target_id}
    )
    # The opener alone: its window fitted, then its tab laid out, on its own session.
    assert since(mark) == [
        (names[0], relay.windows[second.target_id]),
        (names[1], second.stream.session_id),
    ]
    # The browser did not say which page opened it: every page of the socket that is in view.
    await hub.set_visible(client.socket.key, "a", False)
    mark = len(relay.calls)
    await browser.on_stray_closed({"targetId": "Y", "browserContextId": "ctx1"})
    assert since(mark) == [
        (names[0], relay.windows[second.target_id]),
        (names[1], second.stream.session_id),
    ]
    assert first.curtained
    # A window of another socket's context is no business of these pages.
    mark = len(relay.calls)
    await browser.on_stray_closed({"targetId": "Z", "browserContextId": "another-context"})
    assert since(mark) == []


@pytest.mark.asyncio
async def test_the_hub_makes_its_tab_and_its_curtain_through_the_browsers_own_door() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    await open_live(hub, browser, client)
    assert browser.origins == [ORIGIN]
    assert browser.pages == [("ctx1", True, False), ("ctx1", False, True)]


# ── the browser's own door, and strangers ──────────────────────────────────


class _Relay:
    """A relay that records, and answers ``Target.createTarget`` when a test lets it."""

    closed = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.made: asyncio.Future[str] | None = None

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        self.calls.append((method, dict(params or {}), session_id))
        if method == "Target.createTarget":
            assert self.made is not None
            return {"targetId": await self.made}
        return {}

    def named(self, method: str) -> list[tuple[dict[str, Any], str | None]]:
        return [(p, s) for m, p, s in self.calls if m == method]


def _running(tmp_path: Path) -> tuple[pb.PanelBrowser, _Relay]:
    browser = pb.PanelBrowser(tmp_path, accepts_attach=lambda: True)
    relay = _Relay()
    browser._relay = relay  # type: ignore[assignment]
    browser._origin = ORIGIN
    return browser, relay


def _attached(target: str, session: str, *, waits: bool = True, **info: Any) -> dict[str, Any]:
    return {
        "method": "Target.attachedToTarget",
        "params": {
            "sessionId": session,
            "waitingForDebugger": waits,
            "targetInfo": {"targetId": target, "type": "page", "browserContextId": "ctx1", **info},
        },
    }


@pytest.mark.asyncio
async def test_a_browser_that_runs_for_one_loopback_address_is_not_handed_out_for_the_other(
    tmp_path: Path,
) -> None:
    browser, relay = _running(tmp_path)
    assert await browser.ensure(ORIGIN) is relay
    other = "http://[::1]:" + ORIGIN.rsplit(":", 1)[1]
    assert other != ORIGIN
    with pytest.raises(pb.PanelUnavailable) as refused:
        await browser.ensure(other)
    # A reason of its own: the panel says which address to use, not that its page is broken.
    assert refused.value.code == "other_address"
    assert browser._origin == ORIGIN and await browser.ensure(ORIGIN) is relay


@pytest.mark.asyncio
async def test_a_page_the_gateway_makes_is_let_go_on_whichever_comes_first(tmp_path: Path) -> None:
    browser, relay = _running(tmp_path)
    strays: list[dict[str, Any]] = []

    async def on_stray(info: dict[str, Any]) -> None:
        strays.append(info)

    browser.on_stray = on_stray
    loop = asyncio.get_running_loop()
    # The browser tells of the page before it answers the request that made it.
    relay.made = loop.create_future()
    making = asyncio.ensure_future(browser.new_page("ctx1", new_window=True))
    await asyncio.sleep(0)
    await browser._browser_event(_attached("T1", "S1"))
    relay.made.set_result("T1")
    assert await making == "T1"
    # And the other way round: the answer first, then the word of the page.
    relay.made = loop.create_future()
    making = asyncio.ensure_future(browser.new_page("ctx1", new_window=False, background=True))
    await asyncio.sleep(0)
    relay.made.set_result("T2")
    await asyncio.sleep(0)
    await browser._browser_event(_attached("T2", "S2"))
    assert await making == "T2"
    assert relay.named("Runtime.runIfWaitingForDebugger") == [({}, "S1"), ({}, "S2")]
    assert relay.named("Target.closeTarget") == [] and strays == []
    # A session the gateway attaches to its own page later is told the same way: it is no stranger.
    await browser._browser_event(_attached("T1", "S9", waits=False))
    assert relay.named("Target.closeTarget") == [] and strays == []


@pytest.mark.asyncio
async def test_a_page_the_gateway_did_not_make_is_held_told_of_and_closed(tmp_path: Path) -> None:
    browser, relay = _running(tmp_path)
    order: list[str] = []

    async def on_stray(info: dict[str, Any]) -> None:
        order.append("told " + str(info.get("targetId")))

    async def on_stray_closed(info: dict[str, Any]) -> None:
        # Told once the window is closed: the tab it stood in front of is in front again.
        closed = [p["targetId"] for p, _ in relay.named("Target.closeTarget")]
        order.append(f"closed {info.get('targetId')} after {closed}")

    browser.on_stray = on_stray
    browser.on_stray_closed = on_stray_closed
    await browser._browser_event(_attached("X", "SX", openerId="T1"))
    assert order == ["told X", "closed X after ['X']"]
    done = [m for m, _, _ in relay.calls]
    # Every request it could make waits for an answer that never comes, before it is let go and closed.
    assert done.index("Fetch.enable") < done.index("Runtime.runIfWaitingForDebugger")
    assert relay.named("Fetch.enable") == [({"patterns": [{"urlPattern": "*"}]}, "SX")]
    assert relay.named("Target.closeTarget") == [({"targetId": "X"}, None)]
    # A page that the browser could not hold at its start is a stranger all the same.
    await browser._browser_event(_attached("Y", "SY", waits=False))
    assert [p["targetId"] for p, _ in relay.named("Target.closeTarget")] == ["X", "Y"]


@pytest.mark.asyncio
async def test_a_window_opened_while_the_gateway_makes_a_page_waits_and_is_then_closed(
    tmp_path: Path,
) -> None:
    browser, relay = _running(tmp_path)
    relay.made = asyncio.get_running_loop().create_future()
    making = asyncio.ensure_future(browser.new_page("ctx1", new_window=True))
    await asyncio.sleep(0)
    await browser._browser_event(_attached("POPUP", "SP", openerId="T0"))
    # Which of the two is the gateway's own is not known yet: neither is let go.
    assert relay.named("Runtime.runIfWaitingForDebugger") == []
    await browser._browser_event(_attached("T1", "S1"))
    relay.made.set_result("T1")
    assert await making == "T1"
    assert ({}, "S1") in relay.named("Runtime.runIfWaitingForDebugger")
    assert relay.named("Target.closeTarget") == [({"targetId": "POPUP"}, None)]


def test_the_browser_is_asked_to_hold_every_new_page_and_is_fenced_at_its_start() -> None:
    source = Path(pb.__file__).read_text(encoding="utf-8")
    assert '"waitForDebuggerOnStart": True' in source
    assert "panel_fence.launch_flags(origin)" in source and "*fence]" in source


# ── the transport and the host ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_panel_browsers_transport_is_sockets_which_nothing_can_open_again() -> None:
    seen: dict[str, Any] = {}

    async def spawn(argv: list[str], *, pipes: bool, pass_fds: tuple[int, ...], stderr: int) -> Any:
        seen["argv"] = argv
        seen["kinds"] = [stat.S_ISSOCK(os.fstat(fd).st_mode) for fd in pass_fds]
        # What any other process of the same user would try: the open fails for a socket.
        seen["opens"] = []
        for fd in pass_fds:
            try:
                os.close(os.open(f"/proc/self/fd/{fd}", os.O_RDWR | os.O_NONBLOCK))
                seen["opens"].append("opened")
            except OSError as exc:
                seen["opens"].append(exc.errno)
        return object()

    _proc, upstream, _tail = await supervisor.launch_chromium_pipe(
        ["chromium"], spawn, pair=pb._socket_pair
    )
    await upstream.close()
    assert seen["kinds"] == [True, True]
    assert "opened" not in seen["opens"]
    # A shell's ``/dev/fd`` reopen cannot move a socket onto 3 and 4: the remap duplicates them.
    assert supervisor._SH_REMAP not in seen["argv"] and supervisor._PY_REMAP in seen["argv"]
    assert seen["argv"][-2:] == ["chromium", supervisor._CHROMIUM_PIPE_ARG]


def test_the_panel_browser_and_its_children_can_leave_no_core_file() -> None:
    """The real launcher line, run for real: what it becomes reads its own limit, and a child's."""
    grandchild = "import resource; print(resource.getrlimit(resource.RLIMIT_CORE))"
    child = (
        "import resource, subprocess, sys\n"
        "print(resource.getrlimit(resource.RLIMIT_CORE))\n"
        f"print(subprocess.run([sys.executable, '-c', {grandchild!r}], capture_output=True,"
        f" encoding='utf-8', timeout={FALLBACK_TIMEOUT_SECS}).stdout.strip())\n"
    )
    one, other = socket.socketpair()
    try:
        fds = (one.fileno(), other.fileno())
        ran = subprocess.run(
            [sys.executable, "-I", "-S", "-c", supervisor._PY_REMAP]
            + [str(fd) for fd in fds]
            + [sys.executable, "-c", child],
            pass_fds=fds,
            capture_output=True,
            encoding="utf-8",
            timeout=FALLBACK_TIMEOUT_SECS,
        )
    finally:
        one.close()
        other.close()
    assert ran.returncode == 0, ran.stderr
    # Soft and hard, in the process the launcher becomes and in a child of it.
    assert ran.stdout.split("\n")[:2] == ["(0, 0)", "(0, 0)"]
    # The limit is set before anything else, and nothing catches a failure to set it.
    lines = supervisor._PY_REMAP.split("\n")
    assert lines[1] == "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))"
    assert "try" not in supervisor._PY_REMAP and lines.index(lines[1]) < len(lines) - 2


@pytest.mark.asyncio
async def test_the_agents_own_browser_keeps_the_launcher_it_had() -> None:
    seen: dict[str, Any] = {}

    async def spawn(argv: list[str], *, pipes: bool, pass_fds: tuple[int, ...], stderr: int) -> Any:
        seen["argv"] = argv
        seen["kinds"] = [stat.S_ISFIFO(os.fstat(fd).st_mode) for fd in pass_fds]
        return object()

    _proc, upstream, _tail = await supervisor.launch_chromium_pipe(["chromium"], spawn)
    await upstream.close()
    assert seen["kinds"] == [True, True] and supervisor._SH_REMAP in seen["argv"]


def test_the_panel_browser_asks_for_the_sockets() -> None:
    source = Path(pb.__file__).read_text(encoding="utf-8")
    assert "launch_chromium_pipe(argv, spawn, pair=_socket_pair)" in source
    assert "launch_chromium_pipe(argv, spawn)" not in source


@pytest.mark.parametrize(
    ("scope", "refusal"),
    [
        ("0\n", pb.HOST_ATTACH_OPEN),
        ("1\n", None),
        ("2", None),
        ("3", None),
        ("", pb.HOST_ATTACH_UNKNOWN),
        ("x", pb.HOST_ATTACH_UNKNOWN),
        (None, pb.HOST_ATTACH_UNKNOWN),
    ],
)
def test_the_host_must_say_that_one_process_may_not_attach_to_another(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scope: str | None, refusal: str | None
) -> None:
    file = tmp_path / "ptrace_scope"
    if scope is not None:
        file.write_text(scope, encoding="ascii")
    monkeypatch.setattr(pb, "PTRACE_SCOPE_FILE", file)
    monkeypatch.setattr(pb.sys, "platform", "linux")
    assert pb.host_attach_refusal() == refusal


def test_a_host_that_is_not_linux_does_not_say_and_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    file = tmp_path / "ptrace_scope"
    file.write_text("1", encoding="ascii")
    monkeypatch.setattr(pb, "PTRACE_SCOPE_FILE", file)
    monkeypatch.setattr(pb.sys, "platform", "darwin")
    assert pb.host_attach_refusal() == pb.HOST_ATTACH_UNKNOWN


class _NoBrowser:
    class chromium:  # noqa: N801 - the shape of ``install.install_state``'s answer
        present = False


class _ABrowser:
    class chromium:  # noqa: N801 - the shape of ``install.install_state``'s answer
        present = True
        executable = Path("chromium")


@pytest.mark.asyncio
async def test_a_wheel_turned_sideways_is_no_step_back_in_a_panels_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Behind a panel's first page is the empty page its tab was made with; a
    step back to it ends the panel. A finger dragged sideways is a sideways wheel."""
    seen: dict[str, list[str]] = {}

    async def launch(argv: list[str], _spawn: Any, *, pair: Any) -> Any:
        seen["argv"] = argv
        raise OSError("no browser is started here")

    monkeypatch.setattr(pb, "host_attach_refusal", lambda: None)
    monkeypatch.setattr(pb.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(pb.install, "install_state", lambda _home: _ABrowser)
    monkeypatch.setattr(pb.tempfile, "mkdtemp", lambda prefix: str(tmp_path / prefix))
    monkeypatch.setattr(pb, "launch_chromium_pipe", launch)
    with pytest.raises(OSError):
        await pb.PanelBrowser(tmp_path).ensure(ORIGIN)
    off = [arg for arg in seen["argv"] if arg.startswith("--disable-features")]
    # One flag of that name: a browser reads the last one only.
    assert off == ["--disable-features=OverscrollHistoryNavigation"]


@pytest.mark.parametrize(
    ("scope", "accepts", "code"),
    [
        ("0", False, pb.HOST_ATTACH_OPEN),
        (None, False, pb.HOST_ATTACH_UNKNOWN),
        # Past the host check, the next thing a start needs is the browser itself.
        ("0", True, "no_browser"),
        ("1", False, "no_browser"),
        (None, True, "no_browser"),
    ],
)
@pytest.mark.asyncio
async def test_the_browser_starts_on_an_open_host_only_by_the_owners_own_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scope: str | None, accepts: bool, code: str
) -> None:
    file = tmp_path / "ptrace_scope"
    if scope is not None:
        file.write_text(scope, encoding="ascii")
    monkeypatch.setattr(pb, "PTRACE_SCOPE_FILE", file)
    monkeypatch.setattr(pb.sys, "platform", "linux")
    monkeypatch.setattr(pb.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(pb.install, "install_state", lambda _home: _NoBrowser)
    with pytest.raises(pb.PanelUnavailable) as refused:
        await pb.PanelBrowser(tmp_path, accepts_attach=lambda: accepts).ensure(ORIGIN)
    assert refused.value.code == code


def test_the_setting_is_off_when_the_config_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.config import live as live_config

    def broken() -> Any:
        raise RuntimeError("no config")

    monkeypatch.setattr(live_config, "snapshot", broken)
    assert pb._accepts_attach() is False


# ── a panel outlives the renewal of the owner's access cookie ──────────────


def _token(**claims: Any) -> str:
    payload = {
        "sub": "owner",
        "kind": "session",
        "iat": 1000,
        "exp": 1300,
        "session_exp": 73000,
        "nonce": "n1",
        "gen": 1,
    }
    payload.update(claims)
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{body}.signature-{payload['nonce']}"


def test_a_renewed_sign_in_is_the_same_claims_at_a_later_time() -> None:
    old = _token()
    assert ph.renews(old, _token(iat=2000, exp=2300, session_exp=74000, nonce="n2", gen=2)) is True
    # Any other owner sign-in is not the opener's: another person, other bounds, another kind.
    assert ph.renews(old, _token(iat=2000, nonce="n2", sub="someone-else")) is False
    assert ph.renews(old, _token(iat=2000, nonce="n2", peer_key="a-device")) is False
    assert ph.renews(_token(peer_key="a-device"), _token(iat=2000, nonce="n2")) is False
    assert ph.renews(old, _token(iat=2000, nonce="n2", kind="link")) is False
    assert ph.renews(old, _token(iat=2000, nonce="n2", app="an-app")) is False
    # Never an older token, and never something that is not a token.
    assert ph.renews(old, _token(iat=500, nonce="n0")) is False
    assert ph.renews(old, "not-a-token") is False
    assert ph.renews("not-a-token", old) is False
    assert ph.renews(old, _token(iat="soon", nonce="n2")) is False


@pytest.mark.asyncio
async def test_a_panel_goes_on_with_its_openers_renewed_sign_in_and_signs_its_tabs_in_again() -> (
    None
):
    hub, browser, _clock = make_hub()
    old, new = _token(), _token(iat=2000, exp=2300, session_exp=74000, nonce="n2")
    client = Client(1, old)
    panel = await open_live(hub, browser, client)
    browser.running = True
    relay = browser.relay
    assert len(relay.methods("Storage.setCookies")) == 1
    out = await hub.input(new, panel.handle, {"type": "mouse", "action": "click", "x": 10, "y": 10})
    assert out["sent"] == 2
    assert client.socket.token == new
    cookies = [p["cookies"][0]["value"] for _, p, _ in relay.methods("Storage.setCookies")]
    assert cookies == [old, new]
    # The token it was opened with is the older one now: it does not drive the panel.
    with pytest.raises(ph.PanelRefused) as refused:
        await hub.input(old, panel.handle, {"type": "mouse", "action": "click", "x": 10, "y": 10})
    assert refused.value.code == "not_your_panel"
    assert (await hub.ask(new, panel.handle, "state"))["path"] == "/chat"


@pytest.mark.asyncio
async def test_another_owner_sign_in_does_not_take_a_panel_by_looking_like_a_renewal() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, _token())
    panel = await open_live(hub, browser, client)
    for other in (_token(iat=2000, nonce="n2", peer_key="a-device"), "an-unrelated-token"):
        with pytest.raises(ph.PanelRefused) as refused:
            await hub.input(
                other, panel.handle, {"type": "mouse", "action": "move", "x": 1, "y": 1}
            )
        assert refused.value.code == "not_your_panel"
    assert client.socket.token == _token()
    assert len(browser.relay.methods("Storage.setCookies")) == 1


# ── secret fields are drawn covered at the source ──────────────────────────


def test_the_cover_selects_what_the_mask_looks_for_and_draws_no_text_there() -> None:
    css = panel_cover.COVER_CSS
    for selector in (
        "input[type=password]",
        "input[autocomplete~=current-password]",
        "input[autocomplete~=new-password]",
        "input[autocomplete~=one-time-code]",
        "input[autocomplete~=cc-csc]",
        "input[data-kc-asked]",
        f"[{panel_cover.COVER_ATTR}]",
    ):
        assert selector in css
    # Not as dots either: the text is not drawn, in the field, in its selection or as a placeholder.
    assert "color:transparent!important" in css
    assert "-webkit-text-fill-color:transparent!important" in css
    assert "::selection" in css and "::placeholder" in css
    # What a style sheet cannot select, the script marks: a hidden-text style, a secret name.
    assert "webkitTextSecurity" in panel_cover.COVER_JS and "apikey" in panel_cover.COVER_JS
    assert "adoptedStyleSheets" in panel_cover.COVER_JS


def test_the_sheet_is_adopted_again_when_the_page_took_it_out() -> None:
    js = panel_cover.COVER_JS
    # Adopting looks whether the sheet is in the document's list, and adds it when it is not:
    # the same call puts it back after a page's script emptied that list.
    assert "if(root.adoptedStyleSheets.indexOf(sheet)<0)" in js
    assert "root.adoptedStyleSheets=[...root.adoptedStyleSheets,sheet];" in js
    # At each sweep of the document or of what a change added, and by the word that the hub
    # puts in front of each question.
    assert "const sweep=(root,d)=>{if(d===0)adopt(document);" in js
    assert "globalThis.__kcCoverKeep=()=>{adopt(document);return true;};" in js
    assert panel_page.KEEP_SHEET_JS == "globalThis.__kcCoverKeep&&globalThis.__kcCoverKeep()"


def test_the_sheet_draws_a_selects_list_in_the_page_and_puts_back_what_that_takes() -> None:
    css = panel_cover.LIST_CSS
    assert css in panel_cover.COVER_CSS
    marked = f"select[{panel_cover.LIST_ATTR}]:not([multiple])"
    # The control and its picker both: with one of the two the browser draws the list by itself.
    assert f"{marked},{marked}::picker(select){{appearance:base-select!important}}" in css
    # A page that drew the control itself has an arrow of its own: no second one beside it.
    assert (
        f"select[{panel_cover.LIST_ATTR}=own]:not([multiple])::picker-icon{{display:none!important}}"
        in css
    )
    # A control that the browser drew gets the browser's ground and border back, at no weight:
    # whatever the page says of it still wins.
    assert (
        f":where(select[{panel_cover.LIST_ATTR}=auto]:not([multiple])){{background-color:Field;"
        in css
    )
    # Nothing but the appearance and the second arrow is forced on the page.
    assert css.count("!important") == 2
    # A select with no list to open is not marked, and one that gets ``multiple`` later is not styled.
    js = panel_cover.COVER_JS
    assert "if(e.hasAttribute(LIST)||e.multiple||e.size>1)return;" in js
    # The mark says what the page gave the control, read before the sheet changes it.
    assert "getComputedStyle(e).appearance==='none'?'own':'auto'" in js
    assert "for(const e of root.querySelectorAll('select'))list(e);" in js
    assert "if(root.tagName==='SELECT')list(root);" in js


@pytest.mark.asyncio
async def test_every_document_of_a_tab_gets_the_cover_before_the_page_loads() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, TOKEN_A)
    await open_live(hub, browser, client)
    calls = browser.relay.calls
    covers = [
        i
        for i, (method, params, _sid) in enumerate(calls)
        if method == "Page.addScriptToEvaluateOnNewDocument"
        and params.get("source") == panel_cover.COVER_JS
    ]
    assert len(covers) == 1
    params = calls[covers[0]][1]
    # In the panel's own world, where page script cannot reach it, and at once for the document that is there.
    assert params["worldName"] == panel_page.PANEL_WORLD and params["runImmediately"] is True
    navigations = [i for i, (method, _p, _s) in enumerate(calls) if method == "Page.navigate"]
    assert navigations and covers[0] < navigations[0]
