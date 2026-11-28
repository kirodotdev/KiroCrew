"""``browser_harness.sign_in_guard`` and ``sign_in``: per-origin sign-in from an edition.

Everything is faked: the relay (a CDP stand-in with a cookie jar), the provider and
the op runner. No browser, no network, no sleeps.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest

from kiro_crew.browser_harness import credentials
from kiro_crew.browser_harness import ops as harness_ops
from kiro_crew.browser_harness import sign_in, sign_in_guard
from kiro_crew.browser_harness.sign_in_guard import SignInGuard
from kiro_crew.platform.defaults import DefaultBrowserCredentialProvider
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.synthetic_credentials import github_classic, synth
from kiro_crew.testing.wait import default_timeout

SECRET = synth(32, seed=43)
IDP = "signin.example.net"
SIGN_IN_URL = f"https://{IDP}/start?return=https%3A%2F%2Fapp.example.com%2Fx"
PAGE = "https://app.example.com/x"
TAB = "T1"
SID = "S1"
#: Only turns a hang into a named failure; every wait here is on a signal.
_HANG = default_timeout()


class Relay:
    """A CDP stand-in: records every send, keeps a jar per context."""

    def __init__(self, *, keep_cookies: bool = True, tab_url: str = PAGE) -> None:
        self.sent: list[tuple[str, dict[str, Any], str | None]] = []
        self.jar: dict[str, list[dict[str, Any]]] = {}
        self.keep_cookies = keep_cookies
        self.tab_url = tab_url
        self.on_tab_adopted: Any = None
        self.on_tab_event: Any = None
        self.adopted = {TAB: SID}

    def session_context_ids(self) -> list[str]:
        return ["C2"]

    def context_of(self, target_id: str) -> str:
        return "C1"

    def opener_of(self, target_id: str) -> str:
        return TAB if target_id == "P1" else ""

    def adopted_sessions(self) -> dict[str, str]:
        return dict(self.adopted)

    def methods(self, name: str) -> list[dict[str, Any]]:
        return [p for m, p, _ in self.sent if m == name]

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        params = params or {}
        self.sent.append((method, params, session_id))
        context = params.get("browserContextId", "")
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"browserContextId": "C1", "url": self.tab_url}}
        if method == "Storage.getCookies":
            return {"cookies": [dict(c) for c in self.jar.get(context, [])]}
        if method == "Storage.setCookies" and self.keep_cookies:
            jar = self.jar.setdefault(context, [])
            for cookie in params["cookies"]:
                domain = cookie.get("domain") or urlsplit(cookie["url"]).hostname
                jar[:] = [c for c in jar if (c["name"], c["domain"]) != (cookie["name"], domain)]
                if cookie.get("expires") != 1:
                    path = cookie.get("path") or "/"
                    jar.append(
                        {
                            "name": cookie["name"],
                            "domain": domain,
                            "path": path,
                            "value": cookie.get("value"),
                        }
                    )
        return {}


class Provider:
    """A provider with both optional methods; records what it was asked."""

    def __init__(self, answer: Any = None, *, hosts: Any = (IDP,)) -> None:
        self.answer = answer if answer is not None else {"ok": True, "cookies": [_cookie()]}
        self.hosts = hosts
        self.asked: list[tuple[Any, str, str, tuple[Any, ...]]] = []

    def name(self) -> str:
        return "Test sign-in"

    def domains(self) -> tuple[str, ...]:
        return ("example.com",)

    def sign_in_hosts(self) -> Any:
        return self.hosts

    def sign_in_for_origin(
        self, target: Any, origin: str, sign_in_url: str, *, site_cookies: Any
    ) -> Any:
        self.asked.append((target, origin, sign_in_url, site_cookies))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _cookie(**extra: Any) -> dict[str, Any]:
    return {"name": "site_token", "value": SECRET, "httpOnly": True, **extra}


def _guard(relay: Relay, provider: Provider, *, held: bool = False) -> SignInGuard:
    guard = SignInGuard(relay, provider, (IDP,), ("example.com",), ("example.com",))
    guard.held = lambda _t: held
    for tab in (TAB, "T2"):
        guard.note_caller(tab, "dashboard:chat-1")
    return guard


def _redirect(location: str = SIGN_IN_URL, *, frame: str = TAB) -> dict[str, Any]:
    return {
        "method": "Fetch.requestPaused",
        "params": {
            "requestId": "R1",
            "frameId": frame,
            "resourceType": "Document",
            "request": {"url": PAGE},
            "responseStatusCode": 302,
            "responseHeaders": [
                {"name": "Location", "value": location},
                {"name": "Set-Cookie", "value": f"state={SECRET}; Path=/; Secure"},
            ],
        },
    }


def _request(url: str, kind: str, *, frame: str = TAB) -> dict[str, Any]:
    return {
        "method": "Fetch.requestPaused",
        "params": {
            "requestId": "R2",
            "frameId": frame,
            "resourceType": kind,
            "request": {"url": url},
        },
    }


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sink: list[dict[str, Any]] = []
    import kiro_crew.sel as sel_mod

    monkeypatch.setattr(
        sel_mod, "sel", lambda: SimpleNamespace(log_api_access=lambda **r: sink.append(r))
    )
    return sink


# --- the declaration ---------------------------------------------------------------


def test_the_public_default_declares_no_sign_in_host() -> None:
    assert sign_in_guard.declared_hosts(DefaultBrowserCredentialProvider()) == ()


def test_sign_in_hosts_need_both_methods_and_valid_exact_hosts() -> None:
    provider = Provider(hosts=[IDP.upper(), "localhost", "10.0.0.1", "*.x.test", 7, "a.b.test"])
    assert sign_in_guard.declared_hosts(provider) == ("a.b.test", IDP)
    only_hosts = SimpleNamespace(sign_in_hosts=lambda: [IDP])
    assert sign_in_guard.declared_hosts(only_hosts) == ()
    assert sign_in_guard.declared_hosts(Provider(hosts=IDP)) == ()


def test_classify_signs_a_redirect_or_document_and_blocks_every_other_sign_in_request() -> None:
    hosts = (IDP,)
    assert sign_in_guard.classify(_redirect()["params"], hosts) == ("sign", SIGN_IN_URL)
    assert sign_in_guard.classify(_redirect("/next")["params"], hosts) == ("continue", "")
    img = _request(f"https://{IDP}/pixel.png", "Image")["params"]
    assert sign_in_guard.classify(img, hosts) == ("block", img["request"]["url"])
    doc = _request(SIGN_IN_URL, "Document")["params"]
    assert sign_in_guard.classify(doc, hosts) == ("sign", SIGN_IN_URL)
    port = _request(f"http://{IDP}:8443/login", "Document")["params"]
    assert sign_in_guard.classify(port, hosts)[0] == "sign"
    dotted = _request(f"https://{IDP}./x.js", "Script")["params"]
    assert sign_in_guard.classify(dotted, hosts)[0] == "block"
    assert sign_in_guard.classify(_redirect(f"https://{IDP}./s")["params"], hosts)[0] == "sign"
    longer = _request(f"https://{IDP}.example.org/", "Document")["params"]
    assert sign_in_guard.classify(longer, hosts) == ("continue", "")
    broken = _redirect("http://[::1")["params"]
    assert sign_in_guard.classify(broken, hosts) == ("continue", "")
    other = _request("https://cdn.example.org/a.js", "Script")["params"]
    assert sign_in_guard.classify(other, hosts) == ("continue", "")


# --- signing an origin ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_navigate_sent_to_sign_in_is_signed_for_its_own_origin_only(rows: Any) -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard.begin(TAB, PAGE)
    await guard.on_event(TAB, SID, _redirect())
    assert relay.methods("Fetch.failRequest") == [{"requestId": "R1", "errorReason": "Aborted"}]
    ((target, origin, url, site_cookies),) = provider.asked
    assert (target.reason, origin, url) == ("navigate", "https://app.example.com", SIGN_IN_URL)
    assert {"name": "state", "value": SECRET} in site_cookies
    written = relay.methods("Storage.setCookies")[0]
    assert written["browserContextId"] == "C1"
    (cookie,) = written["cookies"]
    assert cookie["url"] == "https://app.example.com" and "domain" not in cookie
    assert cookie["secure"] is True
    # The op re-opens the page itself; the guard does not.
    assert relay.methods("Page.navigate") == []
    block = sign_in_guard.told(guard.take(TAB))
    assert block == {
        "outcome": "signed_in",
        "provider": "Test sign-in",
        "host": "app.example.com",
        "reason": None,
    }
    assert SECRET not in repr(rows) and "start?return" not in repr(rows)


@pytest.mark.asyncio
async def test_a_page_that_bounces_by_itself_is_renewed_and_reopened() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    await guard.on_event(TAB, SID, _redirect())
    assert provider.asked[0][0].reason == "renew"
    assert relay.methods("Page.navigate") == [{"url": PAGE}]
    assert guard.take(TAB)[-1]["outcome"] == "signed_in"


@pytest.mark.asyncio
async def test_an_origin_the_agent_did_not_open_and_never_signed_is_not_signed() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard.begin(TAB, "https://other.example.com/")
    await guard.on_event(TAB, SID, _redirect())
    assert provider.asked == [] and relay.methods("Storage.setCookies") == []
    assert guard.take(TAB)[-1]["reason"] == "origin_not_requested"


@pytest.mark.asyncio
async def test_a_cookie_for_another_host_never_reaches_the_browser() -> None:
    answer = {
        "ok": True,
        "cookies": [_cookie(domain="other.example.com"), _cookie(domain=".app.example.com")],
    }
    relay, provider = Relay(), Provider(answer)
    guard = _guard(relay, provider)
    guard.begin(TAB, PAGE)
    await guard.on_event(TAB, SID, _redirect())
    assert relay.methods("Storage.setCookies") == []
    assert guard.take(TAB)[-1]["reason"] == "cross_host"


@pytest.mark.asyncio
async def test_a_cookie_the_browser_did_not_keep_reads_as_readback_missing() -> None:
    relay, provider = Relay(keep_cookies=False), Provider()
    guard = _guard(relay, provider)
    guard.begin(TAB, PAGE)
    await guard.on_event(TAB, SID, _redirect())
    assert sign_in_guard.told(guard.take(TAB)) == {
        "note": sign_in_guard.REASON_TEXT["readback_missing"],
        "outcome": "not_signed",
        "provider": "Test sign-in",
        "host": "app.example.com",
        "reason": "readback_missing",
    }


@pytest.mark.asyncio
async def test_a_second_bounce_right_after_a_sign_is_not_signed_again() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard.begin(TAB, PAGE)
    await guard.on_event(TAB, SID, _redirect())
    await guard.on_event(TAB, SID, _redirect())
    assert len(provider.asked) == 1
    assert [o["reason"] for o in guard.take(TAB)] == [None, "still_redirected"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ({"ok": False, "reason": "device_check_failed"}, "device_check_failed"),
        ({"ok": False, "reason": "not_signed_in"}, "not_signed_in"),
        ({"ok": False, "reason": "made_up"}, "other"),
        (RuntimeError(SECRET), "provider_failed"),
        (["not", "a", "mapping"], "provider_bad_shape"),
    ],
)
async def test_the_providers_reason_reaches_the_agent_as_a_fixed_code(
    answer: Any, reason: str
) -> None:
    relay = Relay()
    guard = _guard(relay, Provider(answer))
    guard.begin(TAB, PAGE)
    await guard.on_event(TAB, SID, _redirect())
    block = sign_in_guard.told(guard.take(TAB))
    assert block is not None and block["reason"] == reason
    assert block["note"] == sign_in_guard.REASON_TEXT[reason]
    assert SECRET not in repr(block)


@pytest.mark.asyncio
async def test_the_providers_message_is_cut_and_redacted() -> None:
    token = github_classic()
    message = f"run the sign-in again {token}\n" + "x" * 400
    relay = Relay()
    guard = _guard(relay, Provider({"ok": False, "reason": "not_signed_in", "message": message}))
    guard.begin(TAB, PAGE)
    await guard.on_event(TAB, SID, _redirect())
    note = sign_in_guard.told(guard.take(TAB))["note"]  # type: ignore[index]
    assert len(note) <= 200 and token not in note and note.startswith("run the sign-in")


@pytest.mark.asyncio
async def test_a_frame_or_a_held_tab_is_blocked_but_not_signed() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard.begin(TAB, PAGE)
    await guard.on_event(TAB, SID, _redirect(frame="F-child"))
    assert provider.asked == [] and len(relay.methods("Fetch.failRequest")) == 1
    held = _guard(relay, provider, held=True)
    held.begin(TAB, PAGE)
    await held.on_event(TAB, SID, _redirect())
    assert provider.asked == [] and held.take(TAB)[-1]["outcome"] == "held"


@pytest.mark.asyncio
async def test_every_request_to_a_sign_in_host_is_failed_and_others_continue() -> None:
    relay = Relay()
    guard = _guard(relay, Provider())
    await guard.on_event(TAB, SID, _request(f"https://{IDP}/me.json", "XHR"))
    await guard.on_event(TAB, SID, _request("https://app.example.com/a.css", "Stylesheet"))
    assert relay.methods("Fetch.failRequest") == [
        {"requestId": "R2", "errorReason": "BlockedByClient"}
    ]
    assert relay.methods("Fetch.continueRequest") == [{"requestId": "R2"}]


@pytest.mark.asyncio
async def test_sweep_deletes_only_sign_in_host_cookies_in_every_context() -> None:
    relay = Relay()
    relay.jar = {
        "": [
            {"name": "session", "domain": f".{IDP}", "path": "/"},
            {"name": "dotted", "domain": f"{IDP}.", "path": "/"},
        ],
        "C2": [
            {"name": "session", "domain": IDP, "path": "/"},
            {"name": "site_token", "domain": "app.example.com", "path": "/"},
        ],
    }
    assert await _guard(relay, Provider()).sweep() == 3
    assert relay.jar == {
        "": [],
        "C2": [{"name": "site_token", "domain": "app.example.com", "path": "/"}],
    }


# --- the bulk pull -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_bulk_pull_never_sets_a_sign_in_host_cookie() -> None:
    class Bulk(Provider):
        def cookies(self, target: Any) -> list[dict[str, Any]]:
            return [
                {"name": "session", "value": "v", "domain": IDP},
                {"name": "app", "value": "v", "domain": "app.example.com"},
            ]

        def status(self) -> None:
            return None

    relay = Relay()
    await credentials.apply(
        relay,
        provider=Bulk(),
        target=credentials.Target("kiro_chromium", True, "start"),
        settings=credentials.CredentialSettings(True, ("example.com", "example.net")),
    )
    (written,) = relay.methods("Storage.setCookies")
    assert [c["domain"] for c in written["cookies"]] == ["app.example.com"]


# --- turning it on and off -----------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_turns_interception_on_every_tab_and_off_again() -> None:
    relay, provider = Relay(), Provider()
    choice = SimpleNamespace(kind="kiro_chromium", headless=True)
    on = credentials.CredentialSettings(True, ("example.com",))
    guard = await sign_in_guard.reconcile(None, relay, provider, on, choice)
    assert guard is not None and relay.on_tab_event == guard.on_event
    (enable,) = [p for m, p, sid in relay.sent if m == "Fetch.enable" and sid == SID]
    assert {"urlPattern": f"*://{IDP}*", "requestStage": "Request"} in enable["patterns"]
    off = credentials.CredentialSettings(False, ("example.com",))
    assert await sign_in_guard.reconcile(guard, relay, provider, off, choice) is guard
    assert (
        relay.on_tab_event is None and len(relay.methods("Fetch.disable")) == 2
    )  # the tab and the browser
    user = SimpleNamespace(kind="chrome", headless=False)
    assert await sign_in_guard.reconcile(None, Relay(), provider, on, user) is None


# --- the op wrapper ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_navigate_that_got_signed_is_opened_again_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    opened: list[str] = []

    async def run_op(op: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        await guard.on_event(TAB, SID, _redirect())
        return {"ok": False, "code": "nav_error", "error": "aborted"}

    async def navigate_to(ctx: Any, url: str, *, label: str) -> dict[str, Any]:
        opened.append(url)
        return {"ok": True, "url": url, "title": "App"}

    async def helper(name: str, **_k: Any) -> Any:
        return {"targetId": TAB} if name == "current_tab" else None

    monkeypatch.setattr(harness_ops, "run_op", run_op)
    monkeypatch.setattr(harness_ops, "navigate_to", navigate_to)
    ctx = harness_ops.OpContext(helper=helper, step=lambda _s: None)
    supervisor = SimpleNamespace(active_sign_in_guard=lambda: guard)
    result = await sign_in.run_op("navigate", {"url": PAGE}, ctx, supervisor)
    assert opened == [PAGE]
    assert result["ok"] is True and result["sign_in"]["outcome"] == "signed_in"
    assert guard._requested == {}


@pytest.mark.asyncio
async def test_without_a_guard_the_op_runs_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run_op(op: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {"ok": True, "url": PAGE}

    monkeypatch.setattr(harness_ops, "run_op", run_op)
    supervisor = SimpleNamespace(active_sign_in_guard=lambda: None)
    ctx = harness_ops.OpContext(helper=lambda *a, **k: None, step=lambda _s: None)
    assert await sign_in.run_op("navigate", {"url": PAGE}, ctx, supervisor) == {
        "ok": True,
        "url": PAGE,
    }


# --- races, takeovers and shared signs ---------------------------------------------


async def _until(check: Any) -> None:
    """Await *check* turning true, bounded: a sign task that died fails the test."""
    import asyncio

    async def poll() -> None:
        while not check():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), _HANG)


class SlowProvider(Provider):
    """Answers only once *release* is set, from the provider's worker thread."""

    def __init__(self, release: Any, **kw: Any) -> None:
        super().__init__(**kw)
        self.release = release

    def sign_in_for_origin(self, *a: Any, **k: Any) -> Any:
        self.release.wait(10)
        return super().sign_in_for_origin(*a, **k)


@pytest.mark.asyncio
async def test_settle_waits_for_a_sign_whose_request_was_already_answered() -> None:
    import asyncio

    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard.begin(TAB, PAGE)
    handling = asyncio.ensure_future(guard.on_event(TAB, SID, _redirect()))
    await _until(lambda: bool(relay.methods("Fetch.failRequest")))
    await guard.settle(TAB)
    assert guard.take(TAB)[-1]["outcome"] == "signed_in"
    await handling


@pytest.mark.asyncio
async def test_a_takeover_or_a_turned_off_switch_during_the_providers_wait_writes_nothing() -> None:
    import asyncio
    import threading

    for change in ("held", "off"):
        release = threading.Event()
        relay = Relay()
        guard = _guard(relay, SlowProvider(release))
        state = {"held": False}
        guard.held = lambda _t: state["held"]
        guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
        handling = asyncio.ensure_future(guard.on_event(TAB, SID, _redirect()))
        await _until(lambda: bool(relay.methods("Storage.getCookies")))
        if change == "held":
            state["held"] = True
        else:
            guard.active, guard.generation = False, guard.generation + 1
        release.set()
        await asyncio.wait_for(handling, _HANG)
        assert relay.methods("Storage.setCookies") == [] and relay.methods("Page.navigate") == []
        expected = "held" if change == "held" else "not_signed"
        assert guard.take(TAB)[-1]["outcome"] == expected


@pytest.mark.asyncio
async def test_two_tabs_bouncing_together_share_one_sign_and_both_reopen() -> None:
    import asyncio
    import threading

    release = threading.Event()
    relay, provider = Relay(), SlowProvider(threading.Event())
    provider.release = release
    guard = _guard(relay, provider)
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    first = asyncio.ensure_future(guard.on_event(TAB, SID, _redirect()))
    await _until(lambda: bool(provider.asked or relay.methods("Storage.getCookies")))
    second = asyncio.ensure_future(guard.on_event("T2", "S2", _redirect(frame="T2")))
    await _until(lambda: len(relay.methods("Target.getTargetInfo")) >= 2)
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), _HANG)
    assert len(provider.asked) == 1
    sessions = [sid for m, _p, sid in relay.sent if m == "Page.navigate"]
    assert sorted(sessions) == ["S1", "S2"]
    assert guard.take("T2")[-1]["outcome"] == "signed_in"


@pytest.mark.asyncio
async def test_a_redirect_to_another_listed_host_gets_no_cookies() -> None:
    # The agent opened example.com; it 302s to app.example.com (listed, never signed),
    # which bounces to sign-in. Another site gets nothing.
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard.begin(TAB, "https://example.com/")
    hop = _redirect("https://app.example.com/x")
    hop["params"]["request"]["url"] = "https://example.com/"
    await guard.on_event(TAB, SID, hop)
    await guard.on_event(TAB, SID, _redirect())
    assert provider.asked == [] and relay.methods("Storage.setCookies") == []
    assert guard.take(TAB)[-1]["reason"] == "origin_not_requested"


@pytest.mark.asyncio
async def test_a_navigate_url_with_spaces_is_kept_as_the_host_it_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    seen: list[set[str]] = []

    async def run_op(op: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        seen.append(set(guard._requested.get(TAB, ())))
        return {"ok": True, "url": PAGE}

    async def helper(name: str, **_k: Any) -> Any:
        return {"targetId": TAB} if name == "current_tab" else None

    monkeypatch.setattr(harness_ops, "run_op", run_op)
    ctx = harness_ops.OpContext(helper=helper, step=lambda _s: None)
    supervisor = SimpleNamespace(active_sign_in_guard=lambda: guard)
    await sign_in.run_op("navigate", {"url": "https://app.example.com "}, ctx, supervisor)
    assert seen == [{"app.example.com"}]


@pytest.mark.asyncio
async def test_a_tabs_new_names_its_host_to_the_tab_it_opens() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard.expect_new(PAGE, "dashboard:chat-1")
    await guard.on_adopted("T9", "S9")
    await guard.on_event("T9", "S9", _redirect(frame="T9"))
    assert guard.take_new() == ["T9"]
    assert [sid for m, _p, sid in relay.sent if m == "Page.navigate"] == ["S9"]
    assert guard.take("T9")[-1]["outcome"] == "signed_in"


@pytest.mark.asyncio
async def test_the_cookies_a_sign_set_are_deleted_by_url_at_stop() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard.begin(TAB, PAGE)
    await guard.on_event(TAB, SID, _redirect())
    assert guard.placed.keys() == [("site_token", "app.example.com", "/")]
    await guard.placed.delete_all(relay)
    assert relay.jar["C1"] == []


# --- live settings, read-back and stop -------------------------------------------


@pytest.mark.asyncio
async def test_a_switch_turned_off_since_the_last_apply_stops_a_renewal() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard.settings_of = lambda _p: credentials.CredentialSettings(False, ("example.com",))
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    await guard.on_event(TAB, SID, _redirect())
    assert provider.asked == [] and relay.methods("Storage.setCookies") == []
    assert guard.take(TAB)[-1]["reason"] == "origin_not_requested"


@pytest.mark.asyncio
async def test_an_old_cookie_of_the_same_name_is_not_a_read_back() -> None:
    relay, provider = Relay(keep_cookies=False), Provider()
    relay.jar["C1"] = [
        {"name": "site_token", "domain": "app.example.com", "path": "/", "value": "stale"}
    ]
    guard = _guard(relay, provider)
    guard.begin(TAB, PAGE)
    await guard.on_event(TAB, SID, _redirect())
    assert guard.take(TAB)[-1]["reason"] == "readback_missing"


@pytest.mark.asyncio
async def test_halt_cancels_a_sign_in_flight_so_nothing_lands_after_the_stop() -> None:
    import asyncio
    import threading

    release = threading.Event()
    relay = Relay()
    guard = _guard(relay, SlowProvider(release))
    guard.begin(TAB, PAGE)
    handling = asyncio.ensure_future(guard.on_event(TAB, SID, _redirect()))
    await _until(lambda: bool(relay.methods("Storage.getCookies")))
    assert await sign_in_guard.halt(guard, relay) is guard
    release.set()
    await asyncio.gather(handling, return_exceptions=True)
    assert relay.methods("Storage.setCookies") == [] and guard.active is False
    assert await sign_in_guard.halt(guard, Relay()) is None


@pytest.mark.asyncio
async def test_a_tabs_new_on_a_reused_blank_tab_may_be_signed_and_reopened() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard.allow(TAB, PAGE)
    await guard.on_event(TAB, SID, _redirect())
    assert relay.methods("Page.navigate") == [{"url": PAGE}]
    assert guard.take(TAB)[-1]["outcome"] == "signed_in"


@pytest.mark.asyncio
async def test_a_renewal_between_ops_is_reported_but_does_not_reopen_the_next_navigate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard._record(
        TAB, {"outcome": "signed_in", "provider": "p", "host": "app.example.com", "reason": None}
    )
    opened: list[str] = []

    async def run_op(op: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {"ok": True, "url": PAGE}

    async def navigate_to(ctx: Any, url: str, *, label: str) -> dict[str, Any]:
        opened.append(url)
        return {"ok": True, "url": url}

    async def helper(name: str, **_k: Any) -> Any:
        return {"targetId": TAB} if name == "current_tab" else None

    monkeypatch.setattr(harness_ops, "run_op", run_op)
    monkeypatch.setattr(harness_ops, "navigate_to", navigate_to)
    ctx = harness_ops.OpContext(helper=helper, step=lambda _s: None)
    supervisor = SimpleNamespace(active_sign_in_guard=lambda: guard)
    result = await sign_in.run_op("navigate", {"url": PAGE}, ctx, supervisor)
    assert opened == [] and result["sign_in"]["outcome"] == "signed_in"


@pytest.mark.asyncio
async def test_a_write_cut_by_a_stop_still_leaves_its_cookie_to_delete() -> None:
    import asyncio

    class Hung(Relay):
        async def send(self, method: str, params: Any = None, session_id: Any = None) -> Any:
            if method == "Storage.setCookies":
                self.sent.append((method, params or {}, session_id))
                await asyncio.Event().wait()
            return await super().send(method, params, session_id)

    relay = Hung()
    guard = _guard(relay, Provider())
    guard.begin(TAB, PAGE)
    handling = asyncio.ensure_future(guard.on_event(TAB, SID, _redirect()))
    await _until(lambda: bool(relay.methods("Storage.setCookies")))
    await sign_in_guard.halt(guard, relay)
    await asyncio.gather(handling, return_exceptions=True)
    assert guard.placed.keys() == [("site_token", "app.example.com", "/")]


@pytest.mark.asyncio
async def test_a_sign_cut_by_a_takeover_never_reopens_the_page_later() -> None:
    import asyncio
    import threading

    release = threading.Event()
    relay = Relay()
    guard = _guard(relay, SlowProvider(release))
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    guard.allow(TAB, PAGE)
    handling = asyncio.ensure_future(guard.on_event(TAB, SID, _redirect()))
    await _until(lambda: bool(relay.methods("Storage.getCookies")))
    guard.end(TAB)  # the op's task was cancelled by a claim
    release.set()
    await asyncio.gather(handling, return_exceptions=True)
    await _until(lambda: not guard._inflight)
    assert relay.methods("Page.navigate") == [] and relay.methods("Storage.setCookies") == []


@pytest.mark.asyncio
async def test_a_renewal_under_a_browsing_denial_asks_nothing_and_writes_nothing() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)

    async def denied(keys: list[str]) -> bool:
        return "dashboard:chat-1" in keys

    guard.browse_denied = denied
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    await guard.on_event(TAB, SID, _redirect())
    assert provider.asked == [] and relay.methods("Page.navigate") == []


@pytest.mark.asyncio
async def test_a_cut_first_tab_leaves_the_shared_sign_to_the_tab_still_waiting(
    rows: Any,
) -> None:
    import asyncio
    import threading

    release = threading.Event()
    relay = Relay()
    guard = _guard(relay, SlowProvider(release))
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    guard.allow(TAB, PAGE)
    first = asyncio.ensure_future(guard.on_event(TAB, SID, _redirect()))
    await _until(lambda: bool(relay.methods("Storage.getCookies")))
    second = asyncio.ensure_future(guard.on_event("T2", "S2", _redirect(frame="T2")))
    await _until(lambda: len(guard._waiters.get(("C1", "app.example.com"), ())) == 2)
    guard.end(TAB)
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), _HANG)
    assert guard.take("T2")[-1]["outcome"] == "signed_in"
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_a_takeover_during_the_last_settings_read_writes_nothing() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    state = {"held": False, "reads": 0}
    guard.held = lambda _t: bool(state["held"])

    def settings(_p: Any) -> credentials.CredentialSettings:
        state["reads"] += 1
        if state["reads"] == 2:
            state["held"] = True  # the person claims the tab during the final read
        return credentials.CredentialSettings(True, ("example.com",))

    guard.settings_of = settings
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    await guard.on_event(TAB, SID, _redirect())
    assert provider.asked and relay.methods("Storage.setCookies") == []
    assert guard.take(TAB)[-1]["outcome"] == "held"


@pytest.mark.asyncio
async def test_a_tabs_new_cut_by_a_takeover_ends_the_new_tabs_sign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)

    async def run_op(op: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        await guard.on_adopted("T9", "S9")
        raise asyncio.CancelledError  # the person claimed the browser mid-op

    async def helper(name: str, **_k: Any) -> Any:
        return {"targetId": TAB} if name == "current_tab" else None

    monkeypatch.setattr(harness_ops, "run_op", run_op)
    ctx = harness_ops.OpContext(helper=helper, step=lambda _s: None)
    supervisor = SimpleNamespace(active_sign_in_guard=lambda: guard)
    with pytest.raises(asyncio.CancelledError):
        await sign_in.run_op("tabs", {"action": "new", "url": PAGE}, ctx, supervisor)
    assert "T9" not in guard._requested and guard.take_new() == []


@pytest.mark.asyncio
async def test_a_tab_joining_another_tabs_sign_is_judged_by_its_own_caller() -> None:
    import asyncio
    import threading

    release = threading.Event()
    relay = Relay()
    guard = _guard(relay, SlowProvider(release))

    async def denied(keys: list[str]) -> bool:
        return "denied-chat" in keys

    guard.browse_denied = denied
    guard.note_caller("T2", "denied-chat")
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    first = asyncio.ensure_future(guard.on_event(TAB, SID, _redirect()))
    await _until(lambda: bool(relay.methods("Storage.getCookies")))
    second = asyncio.ensure_future(guard.on_event("T2", "S2", _redirect(frame="T2")))
    await _until(lambda: len(guard._waiters.get(("C1", "app.example.com"), ())) == 2)
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), _HANG)
    assert [sid for m, _p, sid in relay.sent if m == "Page.navigate"] == [SID]
    assert guard.take("T2")[-1]["outcome"] == "not_signed"


@pytest.mark.asyncio
async def test_a_popup_is_judged_by_its_openers_chat_and_an_unknown_tab_by_none() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    seen: list[list[str]] = []

    async def denied(keys: list[str]) -> bool:
        seen.append(keys)
        return False

    guard.browse_denied = denied
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    await guard.on_adopted("P1", "SP")
    await guard.on_event("P1", "SP", _redirect(frame="P1"))
    assert ["", "dashboard:chat-1"] in seen
    assert [sid for m, _p, sid in relay.sent if m == "Page.navigate"] == ["SP"]
    await guard.on_adopted("U1", "SU")
    await guard.on_event("U1", "SU", _redirect(frame="U1"))
    assert len(provider.asked) == 1 and guard.take("U1")[-1]["outcome"] == "not_signed"


@pytest.mark.asyncio
async def test_a_popup_adopted_during_a_tabs_new_keeps_its_openers_chat() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard.note_caller(TAB, "chat-a")
    guard.expect_new(PAGE, "chat-b")
    await guard.on_adopted("P1", "SP")
    assert guard._callers["P1"] == "chat-a" and "P1" not in guard._requested
    assert guard.take_new() == []


@pytest.mark.asyncio
async def test_a_tab_that_moved_on_during_a_renewal_is_not_sent_back() -> None:
    import asyncio
    import threading

    release = threading.Event()
    relay = Relay()
    guard = _guard(relay, SlowProvider(release))
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    handling = asyncio.ensure_future(guard.on_event(TAB, SID, _redirect()))
    await _until(lambda: bool(relay.methods("Storage.getCookies")))
    guard.note_caller(TAB, "dashboard:chat-1")  # a click op started on the tab meanwhile
    release.set()
    await asyncio.wait_for(handling, _HANG)
    assert relay.methods("Storage.setCookies") and relay.methods("Page.navigate") == []
    assert guard.take(TAB)[-1]["reason"] == "moved_on"


@pytest.mark.asyncio
async def test_a_renewal_of_a_page_the_callers_gate_refuses_signs_nothing() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard.note_caller(TAB, "app:one", lambda url: url.endswith("/public"))
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    await guard.on_event(TAB, SID, _redirect())
    assert provider.asked == [] and relay.methods("Page.navigate") == []
    assert guard.take(TAB)[-1]["reason"] == "origin_not_requested"


@pytest.mark.asyncio
async def test_a_renewal_of_another_host_does_not_replay_a_navigate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    opened: list[str] = []

    async def run_op(op: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        guard._record(
            TAB,
            {"outcome": "signed_in", "provider": "p", "host": "app.example.com", "reason": None},
        )
        return {"ok": True, "url": "https://other.example.com/"}

    async def navigate_to(ctx: Any, url: str, *, label: str) -> dict[str, Any]:
        opened.append(url)
        return {"ok": True, "url": url}

    async def helper(name: str, **_k: Any) -> Any:
        return {"targetId": TAB} if name == "current_tab" else None

    monkeypatch.setattr(harness_ops, "run_op", run_op)
    monkeypatch.setattr(harness_ops, "navigate_to", navigate_to)
    ctx = harness_ops.OpContext(helper=helper, step=lambda _s: None)
    supervisor = SimpleNamespace(active_sign_in_guard=lambda: guard)
    await sign_in.run_op("navigate", {"url": "https://other.example.com/"}, ctx, supervisor)
    assert opened == []


@pytest.mark.asyncio
async def test_a_same_origin_login_hop_of_an_allowed_page_is_signed() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard.note_caller(TAB, "app:one", lambda url: url.endswith("/dashboard"))
    guard.begin(TAB, "https://app.example.com/dashboard")
    hop = _redirect()
    hop["params"]["request"]["url"] = "https://app.example.com/oauth2/start"
    await guard.on_event(TAB, SID, hop)
    assert provider.asked and guard.take(TAB)[-1]["outcome"] == "signed_in"


@pytest.mark.asyncio
async def test_a_read_only_op_during_a_renewal_does_not_count_as_a_move() -> None:
    import asyncio
    import threading

    release = threading.Event()
    relay = Relay()
    guard = _guard(relay, SlowProvider(release))
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    handling = asyncio.ensure_future(guard.on_event(TAB, SID, _redirect()))
    await _until(lambda: bool(relay.methods("Storage.getCookies")))
    guard.note_caller(TAB, "dashboard:chat-1", moves=False)  # a snapshot meanwhile
    release.set()
    await asyncio.wait_for(handling, _HANG)
    assert relay.methods("Page.navigate") == [{"url": PAGE}]


@pytest.mark.asyncio
async def test_a_popups_same_origin_login_hop_counts_as_its_openers_page() -> None:
    relay, provider = Relay(tab_url="about:blank"), Provider()
    guard = _guard(relay, provider)
    allowed = lambda url: url.endswith("/dashboard")  # noqa: E731
    guard.note_caller(TAB, "app:one", allowed)
    guard.begin(TAB, "https://app.example.com/dashboard")
    guard.end(TAB)
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    await guard.on_adopted("P1", "SP")
    hop = _redirect(frame="P1")
    hop["params"]["request"]["url"] = "https://app.example.com/oauth2/start"
    await guard.on_event("P1", "SP", hop)
    assert provider.asked and guard.take("P1")[-1]["outcome"] == "signed_in"
    # The guard never opens a page the caller's gate refuses.
    assert relay.methods("Page.navigate") == []


@pytest.mark.asyncio
async def test_the_bulk_pull_drops_a_parent_domain_cookie_the_sign_in_host_would_get() -> None:
    class Bulk(Provider):
        def cookies(self, target: Any) -> list[dict[str, Any]]:
            return [
                {"name": "sso", "value": "v", "domain": ".example.net"},
                {"name": "no_dot", "value": "v", "domain": "example.net"},
                {"name": "app", "value": "v", "domain": "app.example.com"},
            ]

        def status(self) -> None:
            return None

    relay = Relay()
    await credentials.apply(
        relay,
        provider=Bulk(),
        target=credentials.Target("kiro_chromium", True, "start"),
        settings=credentials.CredentialSettings(True, ("example.com", "example.net")),
    )
    (written,) = relay.methods("Storage.setCookies")
    assert [c["name"] for c in written["cookies"]] == ["app"]


@pytest.mark.asyncio
async def test_reconcile_also_blocks_sign_in_hosts_browser_wide() -> None:
    relay, provider = Relay(), Provider()
    relay.on_browser_paused = None
    choice = SimpleNamespace(kind="kiro_chromium", headless=True)
    on = credentials.CredentialSettings(True, ("example.com",))
    guard = await sign_in_guard.reconcile(None, relay, provider, on, choice)
    browser_wide = [p for m, p, sid in relay.sent if m == "Fetch.enable" and sid is None]
    assert browser_wide == [
        {"patterns": [{"urlPattern": f"*://{IDP}*", "requestStage": "Request"}]}
    ]
    assert relay.on_browser_paused == guard.on_browser_paused
    popup = {"params": {"requestId": "P", "request": {"url": f"https://{IDP}/login"}}}
    await guard.on_browser_paused(popup)
    other = {"params": {"requestId": "Q", "request": {"url": f"https://{IDP}.example.org/"}}}
    await guard.on_browser_paused(other)
    fails = [(p, sid) for m, p, sid in relay.sent if m == "Fetch.failRequest"]
    assert fails == [({"requestId": "P", "errorReason": "BlockedByClient"}, None)]
    assert [p for m, p, _ in relay.sent if m == "Fetch.continueRequest"] == [{"requestId": "Q"}]
    off = credentials.CredentialSettings(False, ("example.com",))
    await sign_in_guard.reconcile(guard, relay, provider, off, choice)
    assert relay.on_browser_paused is None
    assert None in [sid for m, _p, sid in relay.sent if m == "Fetch.disable"]


@pytest.mark.asyncio
async def test_a_failing_domains_lookup_keeps_the_sign_in_hosts_blocked() -> None:
    class Broken(Provider):
        def domains(self) -> tuple[str, ...]:
            raise RuntimeError("source unreadable")

    relay = Relay()
    choice = SimpleNamespace(kind="kiro_chromium", headless=True)
    on = credentials.CredentialSettings(True, ("example.com",))
    guard = await sign_in_guard.reconcile(None, relay, Broken(), on, choice)
    assert guard is not None and relay.on_tab_event == guard.on_event
    assert relay.methods("Fetch.enable")  # still intercepting
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    await guard.on_event(TAB, SID, _redirect())
    assert relay.methods("Fetch.failRequest") and relay.methods("Storage.setCookies") == []


@pytest.mark.asyncio
async def test_a_popup_of_a_held_tab_is_held_before_the_hub_claims_it() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard.held = lambda target: target == TAB  # the person holds the opener only
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    await guard.on_adopted("P1", "SP")
    await guard.on_event("P1", "SP", _redirect(frame="P1"))
    assert provider.asked == [] and relay.methods("Page.navigate") == []
    assert guard.take("P1")[-1]["outcome"] == "held"


@pytest.mark.asyncio
async def test_an_op_that_starts_while_the_tab_is_read_still_counts_as_a_move() -> None:
    class Slow(Relay):
        async def send(self, method: str, params: Any = None, session_id: Any = None) -> Any:
            if method == "Target.getTargetInfo":
                guard.note_caller(TAB, "dashboard:chat-1")  # a back op starts meanwhile
            return await super().send(method, params, session_id)

    relay, provider = Slow(), Provider()
    guard = _guard(relay, provider)
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    await guard.on_event(TAB, SID, _redirect())
    assert relay.methods("Page.navigate") == []
    assert guard.take(TAB)[-1]["reason"] == "moved_on"


@pytest.mark.asyncio
async def test_a_reopen_that_fails_to_load_reports_nav_failed() -> None:
    class Refused(Relay):
        async def send(self, method: str, params: Any = None, session_id: Any = None) -> Any:
            got = await super().send(method, params, session_id)
            return (
                {"errorText": "net::ERR_CONNECTION_REFUSED"} if method == "Page.navigate" else got
            )

    relay, provider = Refused(), Provider()
    guard = _guard(relay, provider)
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    await guard.on_event(TAB, SID, _redirect())
    last = guard.take(TAB)[-1]
    assert (last["outcome"], last["reason"]) == ("signed_in", "nav_failed")


@pytest.mark.asyncio
async def test_a_bounced_form_post_is_not_reopened_as_a_get() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    post = _redirect()
    post["params"]["request"]["method"] = "POST"
    await guard.on_event(TAB, SID, post)
    assert relay.methods("Storage.setCookies") and relay.methods("Page.navigate") == []
    last = guard.take(TAB)[-1]
    assert (last["outcome"], last["reason"]) == ("signed_in", "resubmit")


@pytest.mark.asyncio
async def test_a_malformed_cookie_for_the_right_host_is_a_bad_shape_not_cross_host() -> None:
    relay = Relay()
    guard = _guard(relay, Provider({"ok": True, "cookies": [_cookie(value="a;\x01b")]}))
    guard.begin(TAB, PAGE)
    await guard.on_event(TAB, SID, _redirect())
    assert relay.methods("Storage.setCookies") == []
    assert guard.take(TAB)[-1]["reason"] == "provider_bad_shape"


@pytest.mark.asyncio
async def test_a_new_gate_forgets_the_origins_the_old_one_passed() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard.note_caller(TAB, "app:one", lambda url: True)
    guard.begin(TAB, PAGE)
    guard.end(TAB)
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    relay.tab_url = "https://other.example.com/"
    guard.note_caller(
        TAB, "app:one", lambda url: urlsplit(url).hostname != "app.example.com"
    )  # revoked
    await guard.on_event(TAB, SID, _redirect())
    assert provider.asked == [] and guard.take(TAB)[-1]["reason"] == "origin_not_requested"


@pytest.mark.asyncio
async def test_a_takeover_of_one_tab_leaves_the_other_tabs_shared_renewal_alone() -> None:
    import asyncio
    import threading

    release = threading.Event()
    relay = Relay()
    guard = _guard(relay, SlowProvider(release))
    taken: set[str] = set()
    guard.held = lambda target: target in taken
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    first = asyncio.ensure_future(guard.on_event(TAB, SID, _redirect()))
    second = asyncio.ensure_future(guard.on_event("T2", "S2", _redirect(frame="T2")))
    await _until(lambda: len(relay.methods("Target.getTargetInfo")) >= 2)
    await _until(lambda: bool(relay.methods("Storage.getCookies")))  # the provider is asked
    taken.add(TAB)  # the person takes over the first tab while the provider answers
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), _HANG)
    assert guard.take(TAB)[-1]["outcome"] == "held"
    assert guard.take("T2")[-1]["outcome"] == "signed_in"
    assert [sid for m, _p, sid in relay.sent if m == "Page.navigate"] == ["S2"]


@pytest.mark.asyncio
async def test_sweep_deletes_a_parent_domain_sign_in_cookie_but_not_a_listed_sites() -> None:
    relay = Relay()
    relay.jar = {
        "": [
            {"name": "sso", "domain": ".example.net", "path": "/"},
            {"name": "parent_host_only", "domain": "example.net", "path": "/"},
            {"name": "site", "domain": ".example.com", "path": "/"},
        ]
    }
    guard = _guard(relay, Provider())
    assert await guard.sweep() == 1
    assert [c["name"] for c in relay.jar[""]] == ["parent_host_only", "site"]


@pytest.mark.asyncio
async def test_a_gate_withdrawn_while_the_provider_answers_writes_nothing() -> None:
    import asyncio
    import threading

    release = threading.Event()
    relay = Relay()
    guard = _guard(relay, SlowProvider(release))
    guard.note_caller(TAB, "app:one", lambda url: True)
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    handling = asyncio.ensure_future(guard.on_event(TAB, SID, _redirect()))
    await _until(lambda: bool(relay.methods("Storage.getCookies")))
    guard.note_caller(TAB, "app:one", lambda url: False, moves=False)  # site withdrawn
    release.set()
    await asyncio.wait_for(handling, _HANG)
    assert relay.methods("Storage.setCookies") == []
    assert guard.take(TAB)[-1]["reason"] == "origin_not_requested"


@pytest.mark.asyncio
async def test_a_reopened_page_that_lands_off_the_callers_sites_is_blocked() -> None:
    relay, provider = Relay(), Provider()
    guard = _guard(relay, provider)
    guard.note_caller(TAB, "app:one", lambda url: urlsplit(url).hostname == "app.example.com")
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    await guard.on_event(TAB, SID, _redirect())
    assert relay.methods("Page.navigate") == [{"url": PAGE}]
    hop = {
        "method": "Fetch.requestPaused",
        "params": {
            "requestId": "R5",
            "frameId": TAB,
            "resourceType": "Document",
            "request": {"url": "https://elsewhere.example.org/"},
            "responseStatusCode": 200,
            "responseHeaders": [],
        },
    }
    await guard.on_event(TAB, SID, hop)
    assert {"requestId": "R5", "errorReason": "BlockedByClient"} in relay.methods(
        "Fetch.failRequest"
    )
    hop["params"]["requestId"] = "R6"  # the re-open is over: later pages are the op route's
    await guard.on_event(TAB, SID, hop)
    assert {"requestId": "R6"} in relay.methods("Fetch.continueRequest")


@pytest.mark.asyncio
async def test_a_denied_waiter_does_not_stop_an_authorized_one_sharing_its_renewal() -> None:
    import asyncio
    import threading

    release = threading.Event()
    relay = Relay()
    guard = _guard(relay, SlowProvider(release))
    guard.note_caller(TAB, "denied-chat")

    async def denied(keys: list[str]) -> bool:
        return "denied-chat" in keys

    guard.browse_denied = denied
    guard._signed[("C1", "app.example.com")] = time.monotonic() - 3600
    gate = asyncio.Event()

    async def slow_settings_reader(keys: list[str]) -> bool:
        await gate.wait()
        return "denied-chat" in keys

    guard.browse_denied = slow_settings_reader
    first = asyncio.ensure_future(guard.on_event(TAB, SID, _redirect()))
    second = asyncio.ensure_future(guard.on_event("T2", "S2", _redirect(frame="T2")))
    await _until(lambda: len(guard._waiters.get(("C1", "app.example.com"), ())) == 2)
    gate.set()
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), _HANG)
    assert guard.take("T2")[-1]["outcome"] == "signed_in"
    assert relay.methods("Storage.setCookies")


class _CutOnce(Relay):
    """Cancels the first send of *method* it sees, as the supervisor's budget would."""

    def __init__(self, method: str) -> None:
        super().__init__()
        self.adopted = {TAB: SID, "T2": "S2"}
        self.cut, self.armed = method, True

    async def send(self, method: str, params: Any = None, session_id: Any = None) -> Any:
        import asyncio

        if method == self.cut and self.armed:
            self.armed = False
            raise asyncio.CancelledError
        return await super().send(method, params, session_id)


def _fetch_state(relay: Relay) -> dict[Any, str]:
    state: dict[Any, str] = {}
    for method, _params, sid in relay.sent:
        if method in ("Fetch.enable", "Fetch.disable"):
            state[sid] = method
    return state


async def _cut_then_retry(relay: Relay, provider: Any, settings: Any) -> Any:
    """One reconcile the budget cuts, then the next one: the next must finish the job."""
    import asyncio

    choice = SimpleNamespace(kind="kiro_chromium", headless=True)
    on = credentials.CredentialSettings(True, ("example.com",))
    guard = await sign_in_guard.reconcile(None, relay, provider, on, choice)
    relay.armed = True
    with pytest.raises(asyncio.CancelledError):
        await sign_in_guard.reconcile(guard, relay, provider, settings, choice)
    assert await sign_in_guard.reconcile(guard, relay, provider, settings, choice) is guard
    return guard


@pytest.mark.asyncio
async def test_a_turn_off_cut_at_its_first_disable_is_finished_by_the_next_run() -> None:
    relay, provider = _CutOnce("Fetch.disable"), Provider()
    relay.armed = False
    off = credentials.CredentialSettings(False, ("example.com",))
    guard = await _cut_then_retry(relay, provider, off)
    assert set(_fetch_state(relay).values()) == {"Fetch.disable"}
    assert set(_fetch_state(relay)) == {None, SID, "S2"}
    assert relay.on_tab_event is None and sign_in_guard.live(guard, relay) is None


@pytest.mark.asyncio
async def test_a_turn_off_cut_midway_keeps_answering_paused_requests() -> None:
    import asyncio

    relay, provider = _CutOnce("Fetch.disable"), Provider()
    relay.armed = False
    choice = SimpleNamespace(kind="kiro_chromium", headless=True)
    on = credentials.CredentialSettings(True, ("example.com",))
    off = credentials.CredentialSettings(False, ("example.com",))
    guard = await sign_in_guard.reconcile(None, relay, provider, on, choice)
    relay.armed = True
    with pytest.raises(asyncio.CancelledError):
        await sign_in_guard.reconcile(guard, relay, provider, off, choice)
    # Some tabs still intercept: their pauses must still be answered, never hang.
    assert relay.on_tab_event is not None
    await relay.on_tab_event("T2", "S2", _request("https://app.example.com/a.css", "Stylesheet"))
    assert {"requestId": "R2"} in relay.methods("Fetch.continueRequest")


@pytest.mark.asyncio
async def test_a_turn_on_cut_midway_is_finished_by_the_next_run() -> None:
    relay, provider = _CutOnce("Fetch.enable"), Provider()
    relay.armed = False
    choice = SimpleNamespace(kind="kiro_chromium", headless=True)
    on = credentials.CredentialSettings(True, ("example.com",))
    import asyncio

    relay.armed = True
    with pytest.raises(asyncio.CancelledError):
        await sign_in_guard.reconcile(None, relay, provider, on, choice)
    guard = sign_in_guard.prepare(None, relay, provider, on, choice)
    assert guard is not None and await sign_in_guard.converge(guard) is True
    assert _fetch_state(relay) == {None: "Fetch.enable", SID: "Fetch.enable", "S2": "Fetch.enable"}
    assert sign_in_guard.live(guard, relay) is guard


@pytest.mark.asyncio
async def test_a_host_change_cut_midway_is_finished_by_the_next_run() -> None:
    class Moved(Provider):
        def sign_in_hosts(self) -> Any:
            return ("other-signin.example.net",)

    relay = _CutOnce("Fetch.enable")
    relay.armed = False
    on = credentials.CredentialSettings(True, ("example.com",))
    choice = SimpleNamespace(kind="kiro_chromium", headless=True)
    guard = await sign_in_guard.reconcile(None, relay, Provider(), on, choice)
    import asyncio

    relay.armed = True
    with pytest.raises(asyncio.CancelledError):
        await sign_in_guard.reconcile(guard, relay, Moved(), on, choice)
    assert sign_in_guard.prepare(guard, relay, Moved(), on, choice) is guard
    assert await sign_in_guard.converge(guard) is True
    new = [p for m, p, sid in relay.sent if m == "Fetch.enable" and sid == "S2"][-1]
    assert {"urlPattern": "*://other-signin.example.net*", "requestStage": "Request"} in new[
        "patterns"
    ]
    assert set(guard._fetch.values()) == {("other-signin.example.net",)}


class _RefusesBrowserFetch(Relay):
    """The browser target refuses its first ``Fetch.enable``, as a CDP error would."""

    def __init__(self) -> None:
        super().__init__()
        self.refuse = True
        self.closed = False

    async def send(self, method: str, params: Any = None, session_id: Any = None) -> Any:
        if method == "Fetch.enable" and session_id is None and self.refuse:
            self.refuse = False
            raise RuntimeError("Fetch.enable: not allowed")
        return await super().send(method, params, session_id)


class _Bulk(Provider):
    def cookies(self, target: Any) -> list[dict[str, Any]]:
        return []

    def status(self) -> None:
        return None

    def fingerprint(self) -> str:
        return "fp"

    def default_settings(self) -> dict[str, Any]:
        return {"enabled": True, "domains": ["example.com"]}


def _supervisor(tmp_path: Any) -> Any:
    from kiro_crew.browser_harness.supervisor import HarnessSupervisor

    provider = _Bulk()
    return HarnessSupervisor(
        tmp_path, config_reader=lambda: {}, credential_provider=lambda: provider
    )


@pytest.mark.asyncio
async def test_a_start_whose_interception_cannot_turn_on_fails(tmp_path: Any) -> None:
    from kiro_crew.browser_harness.supervisor import BrowserUnavailable

    sup = _supervisor(tmp_path)
    choice = SimpleNamespace(kind="kiro_chromium", headless=True)
    with pytest.raises(BrowserUnavailable):
        await sup._apply_credentials(_RefusesBrowserFetch(), choice, "start")
    assert sup._applied_key is None


@pytest.mark.asyncio
async def test_an_unsettled_guard_is_run_again_at_the_next_navigation(tmp_path: Any) -> None:
    sup = _supervisor(tmp_path)
    relay = _RefusesBrowserFetch()
    choice = SimpleNamespace(kind="kiro_chromium", headless=True)
    await sup._apply_credentials(relay, choice, "source_changed")  # first enable refused
    assert sup._guard is not None and sup._guard.settled is False
    daemon = SimpleNamespace(returncode=None, pid=UNALLOCATABLE_PID)  # a running process
    sup._handle = SimpleNamespace(choice=choice, relay=relay, daemon=daemon)
    assert await sup.refresh_credentials_if_source_changed() is True
    assert sup._guard.settled is True and sup._applied_key is not None
    assert None in sup._guard._fetch


@pytest.mark.asyncio
async def test_a_turn_off_during_an_adoptions_enable_still_disables_that_tab() -> None:
    import asyncio

    class Slow(Relay):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()

        async def send(self, method: str, params: Any = None, session_id: Any = None) -> Any:
            if method == "Fetch.enable" and session_id == "S9":
                await self.release.wait()
            return await super().send(method, params, session_id)

    relay, provider = Slow(), Provider()
    choice = SimpleNamespace(kind="kiro_chromium", headless=True)
    on = credentials.CredentialSettings(True, ("example.com",))
    off = credentials.CredentialSettings(False, ("example.com",))
    guard = await sign_in_guard.reconcile(None, relay, provider, on, choice)
    adopting = asyncio.ensure_future(guard.on_adopted("P9", "S9"))  # a popup, in flight
    await _until(lambda: guard._fetch.get("S9") is not None)
    relay.adopted["P9"] = "S9"
    turning_off = asyncio.ensure_future(
        sign_in_guard.reconcile(guard, relay, provider, off, choice)
    )
    relay.release.set()
    await asyncio.wait_for(asyncio.gather(adopting, turning_off), _HANG)
    assert "S9" not in guard._fetch and guard.settled is True
    s9 = [m for m, _p, sid in relay.sent if sid == "S9" and m.startswith("Fetch.")]
    assert s9 == ["Fetch.enable", "Fetch.disable"]
    assert relay.on_tab_event is None
