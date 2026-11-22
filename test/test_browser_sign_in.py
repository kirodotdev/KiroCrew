"""``browser_harness.sign_in``: an edition provider finishing a sign-in a navigate landed on.

Everything is faked: the op runner, the navigation and the provider. No browser, no
network, no sleeps; every wait is bounded by the module's own timeouts.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.browser_harness import credentials
from kiro_crew.browser_harness import ops as harness_ops
from kiro_crew.browser_harness import sign_in
from kiro_crew.browser_harness.supervisor import HarnessSupervisor
from kiro_crew.platform.defaults import DefaultBrowserCredentialProvider
from kiro_crew.testing.synthetic_credentials import synth

SECRET = synth(32, seed=41)
LOGIN = "https://login.example.com/start?next=%2Fapp"
FINISH = f"https://login.example.com/finish?code={SECRET}#state={SECRET}"
SETTINGS = credentials.CredentialSettings(enabled=True, domains=("example.com",))
TARGET = credentials.Target("kiro_chromium", True, "sign_in")


class Provider:
    """A provider with the optional method; records what it was asked."""

    def __init__(self, answer: Any = FINISH, *, domains: tuple[str, ...] = ("example.com",)):
        self.answer = answer
        self._domains = domains
        self.asked: list[tuple[Any, str]] = []

    def name(self) -> str:
        return "Test sign-in"

    def domains(self) -> tuple[str, ...]:
        return self._domains

    def complete_sign_in(self, target: Any, url: str) -> Any:
        self.asked.append((target, url))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class Supervisor:
    """The supervisor's credential seam, as ``sign_in._scope`` reads it."""

    def __init__(
        self,
        provider: Any,
        settings: credentials.CredentialSettings = SETTINGS,
        kind: str = "kiro_chromium",
    ):
        self._handle = SimpleNamespace(choice=SimpleNamespace(kind=kind, headless=True))
        self.provider, self.settings = provider, settings

    def _provider(self) -> Any:
        return self.provider

    def _credential_settings(self, provider: Any) -> credentials.CredentialSettings:
        return self.settings


class Rows:
    """Stands in for the SEL writer."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_api_access(self, **row: Any) -> None:
        self.rows.append(row)


@pytest.fixture
def rows(monkeypatch: pytest.MonkeyPatch) -> Rows:
    sink = Rows()
    import kiro_crew.sel as sel_mod

    monkeypatch.setattr(sel_mod, "sel", lambda: sink)
    return sink


def _ctx(*, held: bool = False, allowed: Any = None) -> tuple[harness_ops.OpContext, list[str]]:
    steps: list[str] = []
    ctx = harness_ops.OpContext(
        helper=lambda *a, **k: None,
        step=steps.append,
        in_control=lambda: held,
        url_allowed=allowed or (lambda url: True),
    )
    return ctx, steps


def _landed(monkeypatch: pytest.MonkeyPatch, url: str = LOGIN, *, opened: Any = None) -> list[str]:
    """``run_op`` answers a navigate that landed on *url*; ``navigate_to`` is faked."""
    calls: list[str] = []

    async def run_op(op: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {"ok": True, "url": url, "title": "Sign in"}

    async def navigate_to(ctx: Any, target: str, *, label: str) -> dict[str, Any]:
        calls.append(target)
        await harness_ops._step(ctx, label)
        if opened is not None:
            return opened  # type: ignore[no-any-return]
        return {"ok": True, "url": f"https://app.example.com/app?session={SECRET}", "title": "App"}

    monkeypatch.setattr(harness_ops, "run_op", run_op)
    monkeypatch.setattr(harness_ops, "navigate_to", navigate_to)
    return calls


async def _navigate(ctx: Any, supervisor: Any) -> dict[str, Any]:
    return await asyncio.wait_for(sign_in.run_op("navigate", {"url": LOGIN}, ctx, supervisor), 30)


# --- the Default and absent methods ------------------------------------------------


def test_the_default_provider_has_no_sign_in_method() -> None:
    assert not hasattr(DefaultBrowserCredentialProvider(), sign_in.METHOD)


@pytest.mark.asyncio
async def test_a_provider_without_the_method_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, rows: Rows
) -> None:
    calls = _landed(monkeypatch)
    ctx, _ = _ctx()
    bare = SimpleNamespace(name=lambda: "x", domains=lambda: ("example.com",))
    for provider in (bare, SimpleNamespace(**vars(bare), complete_sign_in="not callable")):
        result = await _navigate(ctx, Supervisor(provider))
        assert result == {"ok": True, "url": LOGIN, "title": "Sign in"}
    assert calls == [] and rows.rows == []


@pytest.mark.asyncio
async def test_off_the_cookies_opt_in_no_question_is_asked(
    monkeypatch: pytest.MonkeyPatch, rows: Rows
) -> None:
    calls = _landed(monkeypatch)
    ctx, _ = _ctx()
    provider = Provider()
    off = credentials.CredentialSettings(enabled=False, domains=("example.com",))
    for sup in (Supervisor(provider, kind="chrome"), Supervisor(provider, off)):
        assert "sign_in" not in await _navigate(ctx, sup)
    assert "sign_in" not in await _navigate(ctx, Supervisor(DefaultBrowserCredentialProvider()))
    assert provider.asked == [] and calls == [] and rows.rows == []


# --- when it is asked --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "landed, settings, domains",
    [
        ("http://login.example.com/start", SETTINGS, ("example.com",)),
        ("https://login.other.test/start", SETTINGS, ("example.com",)),
        (LOGIN, credentials.CredentialSettings(True, ("other.test",)), ("example.com",)),
        (LOGIN, SETTINGS, ("other.test",)),
    ],
    ids=["http", "off-both-lists", "off-owner-list", "off-provider-list"],
)
async def test_a_page_outside_both_lists_is_not_asked(
    monkeypatch: pytest.MonkeyPatch,
    rows: Rows,
    landed: str,
    settings: credentials.CredentialSettings,
    domains: tuple[str, ...],
) -> None:
    calls = _landed(monkeypatch, landed)
    ctx, _ = _ctx()
    provider = Provider(domains=domains)
    result = await _navigate(ctx, Supervisor(provider, settings))
    assert "sign_in" not in result and provider.asked == [] and calls == []
    assert rows.rows == []


@pytest.mark.asyncio
async def test_other_ops_and_failed_navigates_are_not_asked(
    monkeypatch: pytest.MonkeyPatch, rows: Rows
) -> None:
    _landed(monkeypatch)
    ctx, _ = _ctx()
    provider = Provider()
    result = await sign_in.run_op(
        "tabs", {"action": "new", "url": LOGIN}, ctx, Supervisor(provider)
    )
    assert "sign_in" not in result

    async def failed(op: str, args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {"ok": False, "code": "nav_timeout", "error": "x"}

    monkeypatch.setattr(harness_ops, "run_op", failed)
    assert (await _navigate(ctx, Supervisor(provider)))["code"] == "nav_timeout"
    assert provider.asked == []


@pytest.mark.asyncio
async def test_the_person_holding_the_tab_is_not_asked(
    monkeypatch: pytest.MonkeyPatch, rows: Rows
) -> None:
    calls = _landed(monkeypatch)
    ctx, _ = _ctx(held=True)
    provider = Provider()
    assert "sign_in" not in await _navigate(ctx, Supervisor(provider))
    assert provider.asked == [] and calls == []


# --- the returned URL is fenced ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        "http://login.example.com/finish?code=1",
        "https://user:pw@login.example.com/finish",
        "https://login.example.com/fin\nish",
        "https://login.example.com\\@127.0.0.1/",
        "https://login.example.com/ x",
        "https://127.0.0.1/finish",
        "https://login.other.test/finish",
        "https://login.example.com/" + "a" * 5000,
        "javascript:alert(1)",
    ],
    ids=["http", "userinfo", "control", "backslash", "space", "loopback", "off-list", "long", "js"],
)
async def test_a_returned_url_off_the_fence_is_not_opened(
    monkeypatch: pytest.MonkeyPatch, rows: Rows, answer: str
) -> None:
    calls = _landed(monkeypatch)
    ctx, _ = _ctx(allowed=lambda url: "127.0.0.1" not in url)
    result = await _navigate(ctx, Supervisor(Provider(answer)))
    assert result["sign_in"]["outcome"] == "url_refused"
    assert result["url"] == LOGIN and calls == []


def test_the_fence_refuses_a_host_only_one_list_names_and_a_gate_that_raises() -> None:
    url = "https://login.example.com/finish"
    assert sign_in.fenced(url, ("example.com",), ("example.com",), lambda u: True)
    assert not sign_in.fenced(url, ("example.com",), ("other.test",), lambda u: True)
    assert not sign_in.fenced(url, ("other.test",), ("example.com",), lambda u: True)
    assert not sign_in.fenced(url, ("example.com",), ("example.com",), lambda u: False)

    def broken(_url: str) -> bool:
        raise ValueError("unreadable")

    assert not sign_in.fenced(url, ("example.com",), ("example.com",), broken)
    assert not sign_in.fenced(None, ("example.com",), ("example.com",), lambda u: True)


# --- provider answers ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_none_is_not_offered(monkeypatch: pytest.MonkeyPatch, rows: Rows) -> None:
    calls = _landed(monkeypatch)
    ctx, _ = _ctx()
    provider = Provider(None)
    result = await _navigate(ctx, Supervisor(provider))
    assert result["sign_in"] == {"outcome": "not_offered", "provider": "Test sign-in"}
    assert result["url"] == LOGIN and calls == []
    ((target, url),) = provider.asked
    assert target.reason == "sign_in" and url == LOGIN


@pytest.mark.asyncio
async def test_a_raise_is_a_code_and_its_text_goes_nowhere(
    monkeypatch: pytest.MonkeyPatch, rows: Rows, caplog: pytest.LogCaptureFixture
) -> None:
    _landed(monkeypatch)
    ctx, _ = _ctx()
    result = await _navigate(ctx, Supervisor(Provider(RuntimeError(f"bad {SECRET}"))))
    assert result["sign_in"]["outcome"] == "provider_failed"
    assert SECRET not in repr(result) and SECRET not in caplog.text
    assert SECRET not in repr(rows.rows)


@pytest.mark.asyncio
async def test_a_wrong_shape_is_a_code(monkeypatch: pytest.MonkeyPatch, rows: Rows) -> None:
    _landed(monkeypatch)
    ctx, _ = _ctx()
    result = await _navigate(ctx, Supervisor(Provider({"url": FINISH})))
    assert result["sign_in"]["outcome"] == "provider_bad_shape"


@pytest.mark.asyncio
async def test_a_slow_provider_times_out(monkeypatch: pytest.MonkeyPatch, rows: Rows) -> None:
    _landed(monkeypatch)
    ctx, _ = _ctx()
    release = threading.Event()

    class Slow(Provider):
        def complete_sign_in(self, target: Any, url: str) -> Any:
            release.wait(5)
            return FINISH

    # The provider blocks until released, so this bound is the only way out.
    monkeypatch.setattr(sign_in, "SIGN_IN_PROVIDER_S", 0.05)  # flake-ok: always expires first
    try:
        result = await _navigate(ctx, Supervisor(Slow()))
    finally:
        release.set()
    assert result["sign_in"]["outcome"] == "provider_timeout"
    assert result["url"] == LOGIN


# --- completion ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_completion_never_shows_its_url(
    monkeypatch: pytest.MonkeyPatch, rows: Rows
) -> None:
    calls = _landed(monkeypatch)
    ctx, steps = _ctx()
    provider = Provider()
    result = await _navigate(ctx, Supervisor(provider))
    assert calls == [FINISH]
    assert result == {
        "ok": True,
        "url": "https://app.example.com/app",
        "title": "App",
        "sign_in": {"outcome": "completed", "provider": "Test sign-in"},
    }
    assert steps == ["Finish sign-in on login.example.com"]
    (row,) = rows.rows
    assert row["operation"] == "browser_harness.sign_in" and row["outcome"] == "completed"
    assert row["resources"] == ("provider=Testsign-in from=login.example.com to=login.example.com")
    for seen in (repr(result), repr(steps), repr(rows.rows)):
        assert SECRET not in seen and "code=" not in seen
    assert len(provider.asked) == 1


@pytest.mark.asyncio
async def test_a_completion_landing_off_the_gate_is_blank_and_host_only(
    monkeypatch: pytest.MonkeyPatch, rows: Rows
) -> None:
    refused = harness_ops.refusal("redirect_refused", f"the page redirected to {FINISH}")
    _landed(monkeypatch, opened=refused)
    ctx, _ = _ctx()
    result = await _navigate(ctx, Supervisor(Provider()))
    assert result["url"] == "about:blank"
    assert result["sign_in"]["outcome"] == "redirect_refused"
    assert result["sign_in"]["note"].count("login.example.com") == 1
    assert SECRET not in repr(result)


@pytest.mark.asyncio
async def test_a_completion_that_does_not_load_says_so(
    monkeypatch: pytest.MonkeyPatch, rows: Rows
) -> None:
    _landed(monkeypatch, opened=harness_ops.refusal("nav_timeout", f"last: {FINISH}"))
    ctx, _ = _ctx()
    result = await _navigate(ctx, Supervisor(Provider()))
    assert result["ok"] is True and result["sign_in"]["outcome"] == "nav_failed"
    assert SECRET not in repr(result)


@pytest.mark.asyncio
async def test_a_takeover_between_answer_and_open_holds(
    monkeypatch: pytest.MonkeyPatch, rows: Rows
) -> None:
    calls = _landed(monkeypatch)
    held = {"now": False}
    ctx, _ = _ctx()
    ctx.in_control = lambda: held["now"]

    class Grab(Provider):
        def complete_sign_in(self, target: Any, url: str) -> Any:
            held["now"] = True
            return FINISH

    result = await _navigate(ctx, Supervisor(Grab()))
    assert result["sign_in"]["outcome"] == "held" and calls == []


# --- the supervisor's scope -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind, provider, enabled, expected",
    [
        ("kiro_chromium", Provider(), True, True),
        ("chrome", Provider(), True, False),
        ("kiro_chromium", DefaultBrowserCredentialProvider(), True, False),
        ("kiro_chromium", Provider(), False, False),
    ],
    ids=["kiro", "person-chrome", "default", "off"],
)
async def test_the_scope_is_the_cookies_own_opt_in(
    tmp_path: Path, kind: str, provider: Any, enabled: bool, expected: bool
) -> None:
    cfg = {"credentials": {"enabled": enabled, "domains": ["example.com"]}}
    sup = HarnessSupervisor(
        tmp_path, config_reader=lambda: cfg, credential_provider=lambda: provider
    )
    sup._handle = SimpleNamespace(choice=SimpleNamespace(kind=kind, headless=True))  # type: ignore[assignment]
    scope = await asyncio.wait_for(sign_in._scope(sup), 30)
    assert (scope is not None) is expected
    if scope is not None:
        got, settings, target = scope
        assert got is provider and settings.domains == ("example.com",)
        assert (target.kind, target.reason) == ("kiro_chromium", "sign_in")


@pytest.mark.asyncio
async def test_no_browser_means_no_scope(tmp_path: Path) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {}, credential_provider=Provider)
    assert await asyncio.wait_for(sign_in._scope(sup), 30) is None
