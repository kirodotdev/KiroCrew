"""The agent's browser obeys the governance ``network.egress`` host rule.

Pinned, with the real evaluator (only the boot-frozen ceiling is swapped in):

* a deny rule refuses the URL the agent asks for (``navigate`` and ``tabs new``,
  in the shim) and every page the browser lands on (a redirect, a click, a tab),
  judged in the gateway;
* an allow rule refuses every host it does not name;
* with no rule both gates are exactly the address gate they were, on an LTS build
  too, where the built-in baseline answers an unnamed ``network.egress``;
* a refusal names the rule, never the page;
* a ceiling that cannot be evaluated: the gateway refuses (fail-closed, as its
  ``capabilities.browse`` check), the shim has no opinion (fail-open, as its own);
* on LTS, a rule only the caller's profile sets is judged on its own, never
  against the baseline;
* a numeric IP host is judged in the canonical form the browser dials;
* the gateway reads the caller's profile off the event loop, once per op.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from test_browser_harness_ops import FakeBrowser, _committing_page

from kiro_crew.browser_harness import governance, ops, url_gate
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import browser_ops as route
from kiro_crew.mcp_tools import browser as shim

_CALLER = "dashboard:c-1"


def _policy(mode: str, hosts: list[str]) -> dict[str, Any]:
    return {
        "version": 1,
        "boot": {"fail_closed": True},
        "network": {"egress": {"mode": mode, mode: hosts}},
    }


@pytest.fixture(autouse=True)
def _quiet(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    import kiro_crew.sel as sel_mod

    _floor_monkeypatch.setattr(sel_mod, "sel", lambda: MagicMock())
    _floor_monkeypatch.setattr(shim.mcp_core, "_session_key_header_error", lambda sk: None)
    _floor_monkeypatch.setattr(shim.mcp_core, "_vet_browse_governance", lambda s: None)
    _floor_monkeypatch.setattr(shim.mcp_core, "_resolve_session_key", lambda: _CALLER)
    _floor_monkeypatch.setattr(shim.mcp_core, "_api_port", lambda: 6776)
    cfg = SimpleNamespace(dashboard=SimpleNamespace(browser_local_origins=["localhost:5173"]))
    _floor_monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))


@pytest.fixture
def ceiling(_floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a boot-frozen ceiling; returns a setter (``None`` = ungoverned).

    The document is composed the way boot composes it (``compose_tier_ladder``),
    as the *tier* that holds it, so an LTS build folds its baseline in.
    """
    from dataclasses import replace

    from kiro_crew.platform import context as pc
    from kiro_crew.platform.governance import TIER_HOME, compose_tier_ladder, parse_policy

    def _set(doc: dict[str, Any] | None, tier: str = TIER_HOME) -> None:
        parsed = None
        if doc is not None:
            parsed = compose_tier_ladder(replace(parse_policy(doc), tier=tier))
        _floor_monkeypatch.setattr(
            pc, "current_context", lambda: SimpleNamespace(governance=parsed)
        )

    _set(None)
    return _set


@pytest.fixture
def posts(_floor_monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []

    def _post(_k: str, op: str, *_a: Any) -> tuple[int, dict[str, Any]]:
        sent.append(op)
        return 200, {"ok": True, "result": {"ok": True, "url": "https://x.example/"}}

    _floor_monkeypatch.setattr(shim, "_post_command", _post)
    return sent


@pytest.fixture
def fake(tmp_path: Path) -> FakeBrowser:
    return FakeBrowser(tmp_path)


def _judge() -> governance.EgressJudge:
    """*_CALLER*'s rule as the gateway reads it once per op."""
    return governance.egress_judge(_CALLER)


def _route_ctx() -> ops.OpContext:
    """The context the op route builds for *_CALLER*."""
    return route.build_context(
        _CALLER, MagicMock(), MagicMock(), caller_key=_CALLER, egress=_judge()
    )


#: The route context's governance gates a scripted context borrows.
_GATES = ("url_allowed", "egress_rule", "egress_rule_set")


def _gateway_ctx(fake: FakeBrowser) -> ops.OpContext:
    """:func:`_route_ctx`'s URL gates on a scripted browser."""
    ctx = _route_ctx()
    base = fake.ctx()
    for name in _GATES:
        if hasattr(ctx, name):
            setattr(base, name, getattr(ctx, name))
    return base


def run(coro: Any) -> Any:
    return asyncio.run(coro)


_URLS = (
    "https://example.com/",
    "https://www.example.com/a",
    "http://localhost:5173/",
    "http://localhost:6776/",
    "http://169.254.169.254/",
    "https://user@example.com/",
    "ftp://example.com/",
    "not a url",
    "",
)


@pytest.mark.parametrize("url", _URLS)
def test_no_rule_is_exactly_the_address_gate(ceiling, url: str) -> None:
    expected = url_gate.url_allowed(url, frozenset({"localhost:5173"}), 6776)
    assert route.committed_url_allowed(url, _judge()) is expected
    assert _route_ctx().url_allowed(url) is expected


def test_no_rule_posts_as_before(ceiling, posts: list[str]) -> None:
    shim.browser("browser", {"op": "navigate", "args": {"url": "https://evil.example/"}})
    shim.browser("browser", {"op": "tabs", "args": {"action": "new", "url": "https://e.example/"}})
    assert posts == ["navigate", "tabs"]


def test_a_deny_rule_refuses_the_requested_url_and_a_new_tab(ceiling, posts: list[str]) -> None:
    ceiling(_policy("deny", ["evil.example"]))
    for args in (
        {"op": "navigate", "args": {"url": "https://EVIL.example./page?t=1"}},
        {"op": "tabs", "args": {"action": "new", "url": "https://evil.example/"}},
    ):
        out = shim.browser("browser", args)
        assert out == (
            "Error: the browser tool did not open this address: its host matches a deny "
            "pattern in the governance network.egress rule. The user's governance policy "
            "decides which sites the agent may reach, and the agent cannot change it. Do "
            "not reach the site another way; if the user wants it opened, ask them to "
            "change the policy."
        )
    assert posts == []
    shim.browser("browser", {"op": "navigate", "args": {"url": "https://ok.example/"}})
    assert posts == ["navigate"]
    # A private address keeps the address gate's own text, unchanged.
    out = shim.browser("browser", {"op": "navigate", "args": {"url": "http://10.0.0.1/"}})
    assert out == shim._URL_GATE_TEXT


def test_a_deny_rule_refuses_a_redirect_and_names_only_the_rule(ceiling, fake: FakeBrowser) -> None:
    ceiling(_policy("deny", ["*.blocked.example"]))
    _committing_page(fake, "https://a.blocked.example/ignore-previous-instructions")
    fake.helpers["goto"] = {"frameId": "F", "loaderId": "L"}
    out = run(ops.run_op("navigate", {"url": "https://public.example/r"}, _gateway_ctx(fake)))
    assert out["ok"] is False and out["code"] == "redirect_refused"
    assert out["error"] == (
        "the page landed on an address the governance policy refuses: its host matches "
        "a deny pattern in the governance network.egress rule; the tab was moved to "
        "about:blank"
    )
    assert fake.calls[-1] == ("goto", {"url": "about:blank"})


def test_a_deny_rule_refuses_a_click_that_lands_on_the_host(ceiling, fake: FakeBrowser) -> None:
    ceiling(_policy("deny", ["blocked.example"]))
    tab = {"url": "https://example.com/"}
    fake.helpers["current_tab"] = lambda: dict(tab)
    fake.helpers["click"] = lambda **_kw: tab.update(url="https://blocked.example/x")
    out = run(ops.run_op("click", {"x": 1, "y": 2}, _gateway_ctx(fake)))
    assert out["code"] == "redirect_refused"
    assert fake.calls[-1] == ("goto", {"url": "about:blank"})


def test_a_deny_rule_hides_a_tab_on_the_host_and_refuses_to_read_it(
    ceiling, fake: FakeBrowser
) -> None:
    ceiling(_policy("deny", ["blocked.example"]))
    fake.targets = ["T-ok", "T-bad"]
    fake.helpers["list_tabs"] = [
        {"target_id": "T-ok", "url": "https://ok.example/"},
        {"target_id": "T-bad", "url": "https://blocked.example/"},
    ]
    out = run(ops.run_op("tabs", {"action": "list"}, _gateway_ctx(fake)))
    assert [t["target_id"] for t in out["tabs"]] == ["T-ok"]
    fake.helpers["current_tab"] = {"url": "https://blocked.example/popup"}
    out = run(ops.run_op("snapshot", {}, _gateway_ctx(fake)))
    assert out["code"] == "redirect_refused"
    assert "snapshot" not in fake.page_methods()


def test_an_allow_rule_refuses_every_other_host(ceiling, posts: list[str]) -> None:
    ceiling(_policy("allow", ["*.example.com", "example.com"]))
    gate = _route_ctx().url_allowed
    assert gate("https://example.com/")
    assert gate("https://docs.example.com/")
    assert not gate("https://badexample.com/")
    assert not gate("https://other.example/")
    assert route.committed_egress_rule("https://other.example/", _judge()) == (
        "its host is not allowed by the governance network.egress rule"
    )
    out = shim.browser("browser", {"op": "navigate", "args": {"url": "https://other.example/"}})
    assert out.startswith("Error: the browser tool did not open this address: its host is not")
    assert posts == []


def test_the_address_gate_still_names_its_own_cause(ceiling) -> None:
    ceiling(_policy("allow", ["example.com"]))
    assert not _route_ctx().url_allowed("http://169.254.169.254/latest")
    assert route.committed_egress_rule("http://169.254.169.254/latest", _judge()) is None


def test_an_unevaluable_ceiling_refuses_in_the_gateway_and_not_in_the_shim(
    _floor_monkeypatch: pytest.MonkeyPatch, posts: list[str]
) -> None:
    from kiro_crew.platform import context as pc

    def _broken() -> Any:
        raise RuntimeError("ceiling unreadable")

    _floor_monkeypatch.setattr(pc, "current_context", _broken)
    assert not _route_ctx().url_allowed("https://example.com/")
    assert route.committed_egress_rule("https://example.com/", _judge()) == (
        "the governance network.egress rule could not be evaluated"
    )
    shim.browser("browser", {"op": "navigate", "args": {"url": "https://example.com/"}})
    assert posts == ["navigate"]


# An LTS build answers a scope the policy leaves out with its built-in baseline,
# which for ``network.egress`` names no host. That is no rule: the browser keeps
# exactly what it did before the policy set one.

_BROWSE_ON = {"capabilities": {"browse": {"enabled": True}}}


@pytest.fixture
def lts(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")


def _lts_policy(**extra: Any) -> dict[str, Any]:
    return {"version": 1, "boot": {"fail_closed": True}, **_BROWSE_ON, **extra}


@pytest.mark.parametrize("doc", [None, _lts_policy()], ids=["no-policy", "browse-on"])
def test_lts_without_an_egress_rule_opens_a_public_site_as_before(
    lts: None, ceiling, posts: list[str], fake: FakeBrowser, doc: dict[str, Any] | None
) -> None:
    ceiling(doc)
    out = shim.browser("browser", {"op": "navigate", "args": {"url": "https://example.com/"}})
    assert posts == ["navigate"], out
    for url in _URLS:
        expected = url_gate.url_allowed(url, frozenset({"localhost:5173"}), 6776)
        assert _route_ctx().url_allowed(url) is expected, url
        assert route.committed_egress_rule(url, _judge()) is None, url
    _committing_page(fake, "https://www.example.com/landed")
    fake.helpers["goto"] = {"frameId": "F", "loaderId": "L"}
    out = run(ops.run_op("navigate", {"url": "https://example.com/"}, _gateway_ctx(fake)))
    assert out.get("code") != "redirect_refused", out
    assert ("goto", {"url": "about:blank"}) not in fake.calls


def test_lts_deny_rule_still_refuses_every_committed_page(
    lts: None, ceiling, posts: list[str], fake: FakeBrowser
) -> None:
    ceiling(
        _lts_policy(network={"egress": {"mode": "deny", "deny": ["blocked.example"]}}),
        tier="central",
    )
    out = shim.browser("browser", {"op": "navigate", "args": {"url": "https://blocked.example/"}})
    assert out.startswith("Error: the browser tool did not open this address: its host matches")
    assert posts == []
    assert not _route_ctx().url_allowed("https://blocked.example/a")
    assert _route_ctx().url_allowed("https://example.com/")
    _committing_page(fake, "https://blocked.example/landed")
    fake.helpers["goto"] = {"frameId": "F", "loaderId": "L"}
    out = run(ops.run_op("navigate", {"url": "https://example.com/r"}, _gateway_ctx(fake)))
    assert out["code"] == "redirect_refused"
    assert fake.calls[-1] == ("goto", {"url": "about:blank"})


def test_lts_allow_rule_still_refuses_every_other_host(
    lts: None, ceiling, posts: list[str]
) -> None:
    ceiling(
        _lts_policy(network={"egress": {"mode": "allow", "allow": ["example.com"]}}),
        tier="central",
    )
    gate = _route_ctx().url_allowed
    assert gate("https://example.com/")
    assert not gate("https://other.example/")
    assert route.committed_egress_rule("https://other.example/", _judge()) == (
        "its host is not allowed by the governance network.egress rule"
    )
    out = shim.browser("browser", {"op": "navigate", "args": {"url": "https://other.example/"}})
    assert out.startswith("Error: the browser tool did not open this address: its host is not")
    assert posts == []


_AUTHORITIES = ("home", "central")


@pytest.mark.parametrize("tier", _AUTHORITIES)
def test_lts_a_composed_ceiling_that_names_no_egress_rule_opens_a_public_site(
    lts: None, ceiling, posts: list[str], fake: FakeBrowser, tier: str
) -> None:
    from kiro_crew.platform import context as pc

    ceiling(_lts_policy(), tier=tier)
    # Boot folded the baseline's allow-nothing ``network.egress`` into the controls.
    assert "network.egress" in pc.current_context().governance.controls
    out = shim.browser("browser", {"op": "navigate", "args": {"url": "https://example.com/"}})
    assert posts == ["navigate"], out
    assert _route_ctx().url_allowed("https://example.com/")
    assert route.committed_egress_rule("https://www.example.com/a", _judge()) is None
    _committing_page(fake, "https://www.example.com/landed")
    fake.helpers["goto"] = {"frameId": "F", "loaderId": "L"}
    out = run(ops.run_op("navigate", {"url": "https://example.com/"}, _gateway_ctx(fake)))
    assert out.get("code") != "redirect_refused", out
    assert ("goto", {"url": "about:blank"}) not in fake.calls


@pytest.mark.parametrize("tier", _AUTHORITIES)
def test_lts_a_composed_ceiling_deny_rule_still_refuses_its_host(
    lts: None, ceiling, posts: list[str], tier: str
) -> None:
    rule = {"egress": {"mode": "deny", "deny": ["blocked.example"]}}
    ceiling(_lts_policy(network=rule), tier=tier)
    out = shim.browser("browser", {"op": "navigate", "args": {"url": "https://blocked.example/"}})
    assert out.startswith("Error: the browser tool did not open this address: its host")
    assert posts == []
    assert not _route_ctx().url_allowed("https://blocked.example/a")
    assert route.committed_egress_rule("https://blocked.example/a", _judge()) is not None


# A rule only the caller's profile sets.


@pytest.fixture
def profile(_floor_monkeypatch: pytest.MonkeyPatch) -> Any:
    """Bind a profile's ``network.egress`` rule to a session key; returns the setter.

    The setter returns the list of threads every profile read ran on.
    """
    from kiro_crew.platform import governance_profiles as gp
    from kiro_crew.platform.governance import parse_profile

    bound: dict[str, Any] = {}
    reads: list[int] = []

    def _resolve(session_key: str, **_kw: Any) -> Any:
        reads.append(threading.get_ident())
        return bound.get(session_key)

    def _set(rule: dict[str, Any], key: str = _CALLER) -> list[int]:
        bound[key] = parse_profile({"name": "narrow", "network": {"egress": rule}})
        return reads

    _floor_monkeypatch.setattr(gp, "resolve_active_scope", _resolve)
    return _set


@pytest.mark.parametrize("doc", [None, _lts_policy()], ids=["no-policy", "browse-on"])
def test_lts_a_profile_deny_refuses_its_host_and_opens_every_other(
    lts: None, ceiling, profile, posts: list[str], doc: dict[str, Any] | None
) -> None:
    ceiling(doc)
    profile({"mode": "deny", "deny": ["blocked.example"]})
    out = shim.browser("browser", {"op": "navigate", "args": {"url": "https://example.com/"}})
    assert posts == ["navigate"], out
    out = shim.browser("browser", {"op": "navigate", "args": {"url": "https://blocked.example/"}})
    assert out.startswith("Error: the browser tool did not open this address: its host matches")
    assert posts == ["navigate"]
    gate = _route_ctx().url_allowed
    assert gate("https://example.com/") and gate("https://www.example.com/a")
    assert not gate("https://blocked.example/a")
    assert route.committed_egress_rule("https://blocked.example/a", _judge()) == (
        "its host matches a deny pattern in the governance network.egress rule"
    )


def test_lts_a_profile_allow_list_is_the_whole_rule(
    lts: None, ceiling, profile, posts: list[str]
) -> None:
    ceiling(_lts_policy())
    profile({"mode": "allow", "allow": ["example.com"]})
    shim.browser("browser", {"op": "navigate", "args": {"url": "https://example.com/"}})
    assert posts == ["navigate"]
    out = shim.browser("browser", {"op": "navigate", "args": {"url": "https://other.example/"}})
    assert out.startswith("Error: the browser tool did not open this address: its host is not")
    gate = _route_ctx().url_allowed
    assert gate("https://example.com/") and not gate("https://other.example/")


def test_a_policy_rule_still_caps_a_profile_rule(ceiling, profile) -> None:
    ceiling(_policy("deny", ["evil.example"]))
    profile({"mode": "deny", "deny": ["blocked.example"]})
    gate = _route_ctx().url_allowed
    assert gate("https://example.com/")
    assert not gate("https://evil.example/") and not gate("https://blocked.example/")


# Numeric IP spellings: the browser dials the canonical address.

_ONE_ONE = (
    "http://0x01010101/",
    "http://01.01.01.01/",
    "http://16843009/",
    "http://1.65793/",
    "http://1.1.257/",
    "http://0x1.0x1.0x1.0x1/",
    "http://[::ffff:1.1.1.1]/",
    "http://[::ffff:101:101]/",
)


@pytest.mark.parametrize("url", _ONE_ONE)
def test_a_numeric_ip_spelling_is_judged_as_the_address_it_dials(
    ceiling, posts: list[str], url: str
) -> None:
    ceiling(_policy("deny", ["1.1.1.1"]))
    for args in (
        {"op": "navigate", "args": {"url": url}},
        {"op": "tabs", "args": {"action": "new", "url": url}},
    ):
        out = shim.browser("browser", args)
        assert out.startswith("Error: the browser tool did not open this address: its host matches")
    assert posts == []
    assert url_gate.browser_host(url) == "1.1.1.1"
    assert not _route_ctx().url_allowed(url)


def test_a_numeric_ip_on_an_allow_list_opens_in_any_spelling(ceiling, posts: list[str]) -> None:
    ceiling(_policy("allow", ["1.1.1.1"]))
    shim.browser("browser", {"op": "navigate", "args": {"url": "http://0x01010101/"}})
    assert posts == ["navigate"]


def test_a_host_that_is_not_ascii_is_not_folded_into_an_address() -> None:
    wide = "\uff11.\uff11.\uff11.\uff11"
    assert url_gate.browser_host(f"http://{wide}/") == wide
    assert not url_gate.target_is_public(f"http://{wide}/")
    assert url_gate.browser_host("https://Example.COM./") == "example.com"


# The gateway's reads: once per op, off the event loop.


def test_the_gateway_reads_the_profile_off_the_loop_and_judges_pages_without_io(
    profile, fake: FakeBrowser
) -> None:
    reads = profile({"mode": "deny", "deny": ["blocked.example"]})

    async def _go() -> tuple[int, int, dict[str, Any]]:
        judge = await governance.egress_judge_async(_CALLER)
        before = len(reads)
        ctx = fake.ctx()
        ctx.url_allowed = lambda url: route.committed_url_allowed(url, judge)
        ctx.egress_rule = lambda url: route.committed_egress_rule(url, judge)
        _committing_page(fake, "https://blocked.example/landed")
        fake.helpers["goto"] = {"frameId": "F", "loaderId": "L"}
        out = await ops.run_op("navigate", {"url": "https://example.com/r"}, ctx)
        return threading.get_ident(), len(reads) - before, out

    loop_thread, on_loop_reads, out = run(_go())
    assert out["code"] == "redirect_refused"
    assert on_loop_reads == 0
    assert reads and loop_thread not in reads


def test_the_op_route_judges_with_the_callers_profile_read_off_the_loop(
    profile, _floor_monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_browser_ops_route import _install, _request, _Supervisor

    reads = profile({"mode": "deny", "deny": ["blocked.example"]}, key="k")
    sup = _Supervisor()
    sup.tab = {"targetId": "T1", "url": "https://blocked.example/", "title": "t"}
    _install(_floor_monkeypatch, sup)

    async def _go() -> tuple[int, Any]:
        resp = await route.api_browser_op(_request({"op": "snapshot", "session_key": "k"}))
        return threading.get_ident(), resp

    loop_thread, resp = run(_go())
    body = json.loads(resp.body)
    assert body["ok"] is False and body["code"] == "redirect_refused", body
    assert reads and loop_thread not in reads


# Focus inside frames: every frame on the chain holding focus is judged before any key
# reaches it, read by the helper with no live view (the real ``focus_chain`` over a
# scripted tab).


class _FocusTab:
    """A top page on example.com whose focus runs down the frames *urls*, outermost first.

    *unreadable* names a frame (by its index in *urls*) whose document cannot be asked."""

    def __init__(self, urls: list[str], unreadable: int | None = None) -> None:
        self.urls = urls
        self.unreadable = unreadable
        self.calls: list[str] = []

    def _tree(self, depth: int) -> dict[str, Any]:
        url = "https://example.com/" if depth == 0 else self.urls[depth - 1]
        node: dict[str, Any] = {"frame": {"id": f"F{depth}", "url": url}}
        if depth < len(self.urls):
            node["childFrames"] = [self._tree(depth + 1)]
        return node

    def cdp(self, method: str, **params: Any) -> Any:
        self.calls.append(method)
        if method == "Page.getFrameTree":
            return {"frameTree": self._tree(0)}
        if method == "Page.createIsolatedWorld":
            depth = int(params["frameId"][1:])
            if self.unreadable is not None and depth == self.unreadable + 1:
                raise RuntimeError("frame runs in another process")
            return {"executionContextId": depth}
        if method == "Runtime.evaluate":
            depth = params["contextId"]
            return {"result": {"objectId": f"active-{depth}"}}
        if method == "DOM.describeNode":
            depth = int(params["objectId"].rsplit("-", 1)[1])
            return {"node": {"frameId": f"F{depth + 1}"} if depth < len(self.urls) else {}}
        return {}


def _focus_ctx(
    urls: list[str], unreadable: int | None = None
) -> tuple[ops.OpContext, list[tuple[str, Any]]]:
    from kiro_crew.browser_harness import helper_worker

    tab = _FocusTab(urls, unreadable)
    sent: list[tuple[str, Any]] = []

    async def helper(name: str, **kwargs: Any) -> Any:
        sent.append((name, kwargs))
        if name == "current_tab":
            return {"url": "https://example.com/"}
        if name == "focus_chain":
            return helper_worker._local_focus_chain(tab)
        if name == "js_isolated":
            return {"focused": True, "frame": True}
        return {"ok": True}

    # The route's own gates; no live view: nobody is streaming this tab.
    route_ctx = _route_ctx()
    gates = {n: getattr(route_ctx, n) for n in _GATES if hasattr(route_ctx, n)}
    return ops.OpContext(helper=helper, frames=lambda: None, **gates), sent


_INPUT = [
    ("type", {"text": "hello"}),
    ("press_key", {"key": "a"}),
    ("press_key", {"key": "Enter"}),
]
_IDS = ["type", "printable-key", "enter"]
_ENTER = {"key": "Enter"}


def _sent_input(sent: list[tuple[str, Any]]) -> bool:
    return any(name in ("type_text", "press_key") for name, _ in sent)


def _refused(op: str, args: dict[str, Any], out: dict[str, Any], code: str) -> None:
    """With no live view, ``type`` and a printable key already stop at the field check
    (``focus_in_frame``); Enter reads no field, so only the focus chain stops it."""
    assert out["ok"] is False, out
    if args == _ENTER:
        assert out["code"] == code, out
    else:
        assert out["code"] in (code, "focus_in_frame"), out


@pytest.mark.parametrize(("op", "args"), _INPUT, ids=_IDS)
@pytest.mark.parametrize(
    "urls",
    [["https://blocked.example/form"], ["https://forms.example.org/", "https://blocked.example/"]],
    ids=["focused-frame", "nested-in-allowed-wrapper"],
)
def test_input_into_a_focused_frame_on_a_denied_host_is_refused_and_sends_nothing(
    ceiling, op: str, args: dict[str, Any], urls: list[str]
) -> None:
    ceiling(_policy("deny", ["blocked.example"]))
    ctx, sent = _focus_ctx(urls)
    out = run(ops.run_op(op, args, ctx))
    _refused(op, args, out, "redirect_refused")
    assert not _sent_input(sent)
    if args == _ENTER:
        assert "network.egress" in out["error"]
        assert ("goto", {"url": "about:blank"}) in sent


def test_input_into_nested_frames_on_allowed_hosts_is_sent(ceiling) -> None:
    ceiling(_policy("deny", ["blocked.example"]))
    ctx, sent = _focus_ctx(["https://forms.example.org/", "https://pay.example.net/"])
    out = run(ops.run_op("press_key", _ENTER, ctx))
    assert out["ok"], out
    assert "press_key" in [name for name, _ in sent]


@pytest.mark.parametrize(("op", "args"), _INPUT, ids=_IDS)
def test_with_a_rule_an_unreadable_focus_chain_sends_nothing(
    ceiling, op: str, args: dict[str, Any]
) -> None:
    ceiling(_policy("deny", ["blocked.example"]))
    ctx, sent = _focus_ctx(["https://forms.example.org/", "https://pay.example.net/"], 0)
    out = run(ops.run_op(op, args, ctx))
    _refused(op, args, out, "focus_unreadable")
    assert not _sent_input(sent)


@pytest.mark.parametrize(("op", "args"), _INPUT, ids=_IDS)
def test_with_a_rule_a_failed_focus_read_sends_nothing(
    ceiling, op: str, args: dict[str, Any]
) -> None:
    ceiling(_policy("deny", ["blocked.example"]))
    ctx, sent = _focus_ctx(["https://forms.example.org/"])
    inner = ctx.helper

    async def helper(name: str, **kwargs: Any) -> Any:
        if name == "focus_chain":
            raise RuntimeError("the helper is gone")
        return await inner(name, **kwargs)

    ctx.helper = helper
    out = run(ops.run_op(op, args, ctx))
    _refused(op, args, out, "focus_unreadable")
    assert not _sent_input(sent)


def test_with_no_rule_an_unreadable_focus_chain_keeps_the_input(ceiling) -> None:
    ctx, sent = _focus_ctx(["https://forms.example.org/", "https://pay.example.net/"], 0)
    assert ctx.egress_rule_set is False
    out = run(ops.run_op("press_key", _ENTER, ctx))
    assert out["ok"], out
    assert _sent_input(sent)


def test_with_no_rule_a_focused_frame_on_a_private_address_takes_no_input(ceiling) -> None:
    """The same check holds the address gate: a private frame is never typed into."""
    ctx, sent = _focus_ctx(["https://forms.example.org/", "http://10.0.0.5/form"])
    out = run(ops.run_op("press_key", _ENTER, ctx))
    assert out["code"] == "redirect_refused", out
    assert not _sent_input(sent)


def test_the_focus_chain_is_judged_with_no_live_view_and_no_frame_read() -> None:
    """The helper asks only which element holds focus; nothing in a frame is read."""
    from kiro_crew.browser_harness import helper_worker

    tab = _FocusTab(["https://a.example/", "https://b.example/"])
    read = helper_worker._local_focus_chain(tab)
    assert read == {"chain": ["https://a.example/", "https://b.example/"], "complete": True}
    assert tab.calls.count("Runtime.evaluate") == 3
