"""Browser credentials: the edition seam and how it reaches Kiro's Chromium
(``browser_harness/credentials.py`` and the supervisor's start).

Every test is deterministic: the relay is a fake that records the CDP it is asked to
send, and no browser, daemon or network is involved.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.browser_harness import HARNESS_VERSION, credentials, supervisor
from kiro_crew.browser_harness.supervisor import HarnessSupervisor
from kiro_crew.platform import interfaces
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.defaults import DefaultBrowserCredentialProvider

#: A value no output except the browser-bound CDP params may ever carry.
SECRET = "s3cr3t-cookie-value-9f1c"
FUTURE = time.time() + 86_400


class FakeRelay:
    """Records every relay-owned CDP request; ``fail_batches`` refuses multi-cookie sends."""

    def __init__(self, *, fail_batches: bool = False, refuse: set[str] | None = None) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.fail_batches = fail_batches
        self.refuse = refuse or set()
        self.closed = False
        self.on_upstream_closed: Any = None

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        params = params or {}
        self.sent.append((method, params))
        cookies = params.get("cookies") or []
        if self.fail_batches and len(cookies) > 1:
            raise supervisor.CdpError(method, {"message": "Invalid cookie fields"})
        if any(c.get("name") in self.refuse for c in cookies):
            raise supervisor.CdpError(method, {"message": "Invalid cookie fields"})
        return {}

    async def start(self, upstream: Any) -> str:
        return "http://127.0.0.1:1/secret"

    async def close(self) -> None:
        self.closed = True

    async def close_agent_targets(self) -> None:
        return None

    def agent_targets(self) -> list[str]:
        return []

    def cookies_set(self) -> list[dict[str, Any]]:
        return [c for m, p in self.sent if m == "Storage.setCookies" for c in p["cookies"]]


class FakeProvider:
    def __init__(
        self,
        cookies: list[dict[str, Any]] | None = None,
        *,
        domains: tuple[str, ...] = ("example.com",),
        raises: Exception | None = None,
        defaults: dict[str, Any] | None = None,
    ) -> None:
        self._cookies = cookies if cookies is not None else [_cookie()]
        self._domains = domains
        self._raises = raises
        self._defaults = (
            defaults if defaults is not None else {"enabled": True, "domains": list(domains)}
        )
        self.targets: list[Any] = []
        self.fp: str | None = None
        self.status_value: Any = None

    def name(self) -> str:
        return "Test sign-in"

    def domains(self) -> tuple[str, ...]:
        return self._domains

    def cookies(self, target: Any) -> list[dict[str, Any]]:
        self.targets.append(target)
        if self._raises is not None:
            raise self._raises
        return list(self._cookies)

    def default_settings(self) -> dict[str, Any]:
        return dict(self._defaults)

    def fingerprint(self) -> str | None:
        return self.fp

    def status(self) -> Any:
        return self.status_value


async def _apply(relay: Any, provider: Any, kind: str = "kiro_chromium", **kw: Any) -> Any:
    """``credentials.apply`` with the settings the supervisor would resolve (no config)."""
    settings = kw.pop("settings", None) or credentials.effective_settings(None, provider)
    return await credentials.apply(
        relay, provider=provider, target=_target(kind), settings=settings
    )


def _cookie(**over: Any) -> dict[str, Any]:
    base = {
        "name": "session",
        "value": SECRET,
        "domain": ".example.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "sameSite": "Lax",
        "expires": FUTURE,
    }
    base.update(over)
    return base


def _target(kind: str = "kiro_chromium", reason: str = "start") -> Any:
    return credentials.Target(kind=kind, headless=True, reason=reason)


# ── the seam ──


def test_the_default_context_composes_a_provider_that_supplies_nothing() -> None:
    from kiro_crew.config.loader import KiroCrewConfig

    ctx = build_default_context(KiroCrewConfig())
    provider = ctx.browser_credentials
    assert isinstance(provider, DefaultBrowserCredentialProvider)
    assert provider.name() is None
    assert list(provider.domains()) == []
    assert list(provider.cookies(_target())) == []
    assert interfaces.BrowserCredentialProvider is not None
    assert interfaces.BrowserCredentialTarget is credentials.Target


@pytest.mark.asyncio
async def test_no_provider_writes_nothing() -> None:
    relay = FakeRelay()
    status = await _apply(relay, DefaultBrowserCredentialProvider())
    assert relay.sent == []
    assert status.state == credentials.STATE_NONE
    assert status.as_dict()["provider"] is None and status.as_dict()["applied"] == 0


@pytest.mark.asyncio
async def test_a_provider_s_cookies_are_set_through_the_relay(tmp_path: Path) -> None:
    relay = FakeRelay()
    provider = FakeProvider([_cookie(), _cookie(name="pref", expires=None, httpOnly=False)])
    status = await _apply(relay, provider)
    assert [m for m, _ in relay.sent] == ["Storage.setCookies"]
    names = {c["name"] for c in relay.cookies_set()}
    assert names == {"session", "pref"}
    pref = next(c for c in relay.cookies_set() if c["name"] == "pref")
    assert pref["httpOnly"] is False
    assert all("expires" not in c for c in relay.cookies_set())  # session cookies only
    assert status.state == credentials.STATE_APPLIED
    snap = status.as_dict()
    assert snap["provider"] == "Test sign-in" and snap["applied"] == 2
    assert status.applied_domains == ("example.com",)
    (seen,) = provider.targets
    assert (seen.kind, seen.headless, seen.reason) == ("kiro_chromium", True, "start")


@pytest.mark.asyncio
async def test_only_the_provider_s_declared_domains_are_set(tmp_path: Path) -> None:
    relay = FakeRelay()
    provider = FakeProvider(
        [
            _cookie(domain="example.com"),
            _cookie(name="sub", domain="app.example.com"),
            _cookie(name="other", domain="example.org"),
            _cookie(name="suffix", domain="notexample.com"),
            _cookie(name="parent", domain=".com"),
        ],
        domains=("Example.com", "com", "127.0.0.1", "*.wild.example", "host:443"),
    )
    await _apply(relay, provider)
    assert {c["name"] for c in relay.cookies_set()} == {"session", "sub"}


def test_declared_domains_refuse_anything_broader_than_a_host() -> None:
    assert credentials.normalize_declared_domains(
        [".Example.com", "com", "localhost", "10.0.0.1", "*.x.com", "a.b:80", 7, "intra.example"]
    ) == ("example.com", "intra.example")


@pytest.mark.asyncio
async def test_the_person_s_own_chrome_is_never_filled() -> None:
    for kind in ("user_chrome", "edge"):
        relay = FakeRelay()
        provider = FakeProvider()
        status = await _apply(relay, provider, kind)
        assert relay.sent == [] and provider.targets == []
        assert (status.state, status.reason) == (credentials.STATE_SKIPPED, "user_browser")


@pytest.mark.asyncio
async def test_one_bad_cookie_does_not_lose_the_batch(tmp_path: Path) -> None:
    relay = FakeRelay(fail_batches=True, refuse={"bad"})
    provider = FakeProvider([_cookie(), _cookie(name="bad"), _cookie(name="ok2")])
    status = await _apply(relay, provider)
    assert status.applied == 2
    assert status.state == credentials.STATE_APPLIED


@pytest.mark.asyncio
async def test_a_failing_provider_is_a_code_and_never_its_message(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    relay = FakeRelay()
    provider = FakeProvider(raises=RuntimeError(f"cookie jar said {SECRET}"))
    with caplog.at_level(logging.DEBUG):
        status = await _apply(relay, provider)
    assert relay.sent == []
    assert (status.state, status.reason) == (credentials.STATE_ERROR, "provider_failed")
    assert SECRET not in json.dumps(status.as_dict())
    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_a_hung_provider_times_out_and_the_browser_still_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    gate = threading.Event()

    class Hung(FakeProvider):
        def cookies(self, target: Any) -> list[dict[str, Any]]:
            gate.wait()
            return []

    monkeypatch.setattr(credentials, "PROVIDER_TIMEOUT_SECS", 0.05)
    try:
        status = await _apply(FakeRelay(), Hung())
    finally:
        gate.set()
    assert status.reason == "provider_timeout"


@pytest.mark.asyncio
async def test_a_provider_returning_a_bad_shape_is_refused() -> None:
    class Odd(FakeProvider):
        def cookies(self, target: Any) -> Any:
            return {"not": "a list"}

    status = await _apply(FakeRelay(), Odd())
    assert status.reason == "provider_bad_shape"


# ── normalising ──


def test_a_cookie_is_normalised_to_a_cdp_session_cookie() -> None:
    now = time.time()
    raw = {
        "name": "a",
        "value": "v",
        "domain": ".Example.COM",
        "expires": FUTURE,
        "secure": True,
        "sameSite": "None",
    }
    assert credentials.normalize_cookie(raw, now) == {
        "name": "a",
        "value": "v",
        "domain": ".example.com",
        "path": "/",
        "secure": True,
        "httpOnly": False,
        "sameSite": "None",
    }
    # SameSite=None without Secure is refused by Chromium; it is set as Lax instead.
    lax = credentials.normalize_cookie({"name": "a", "domain": "x.com", "sameSite": "None"}, now)
    assert lax is not None and lax["sameSite"] == "Lax"


@pytest.mark.parametrize(
    "raw",
    [
        "not an object",
        {"domain": "x.com"},
        {"name": "a"},
        {"name": "a", "domain": "."},
        {"name": "a\n", "domain": "x.com"},
        {"name": "a", "domain": "evil\n.x.com"},
        {"name": "a", "domain": "a b=c.x.com"},
        {"name": "a", "domain": "x.com", "value": "v\x00"},
        {"name": "a", "domain": "x.com", "value": 7},
        {"name": "a", "domain": "x.com", "secure": "yes"},
        {"name": "a", "domain": "x.com", "httpOnly": 2},
        {"name": "a", "domain": "x.com", "expires": 5},
    ],
)
def test_a_malformed_or_expired_cookie_is_dropped(raw: Any) -> None:
    assert credentials.normalize_cookie(raw, time.time()) is None


# ── agent-visible surfaces ──


def test_the_snapshot_walker_never_reads_document_cookie() -> None:
    walker = (Path(supervisor.__file__).parent / "snapshot_walker.js").read_text(encoding="utf-8")
    assert "document.cookie" not in walker
    assert "cookieStore" not in walker


def test_no_helper_the_agent_reaches_can_read_cookies() -> None:
    for name in supervisor.HELPERS:
        assert "cookie" not in name.lower()
    from kiro_crew.browser_harness import ops

    assert not any("cookie" in op.lower() for op in ops.OPS)


# ── the supervisor: the apply point, before the daemon, on every start ──


class _Proc:
    def __init__(self) -> None:
        self.returncode: int | None = None


def _supervisor_with_fakes(
    home: Path, monkeypatch: pytest.MonkeyPatch, provider: Any, kind: str = "kiro_chromium"
) -> tuple[HarnessSupervisor, list[str], list[FakeRelay], list[list[str]]]:
    events: list[str] = []
    relays: list[FakeRelay] = []
    argvs: list[list[str]] = []
    exe = home / "chrome"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        supervisor.install,
        "install_state",
        lambda _home: supervisor.install.InstallState(
            prefix=home,
            installed=True,
            version=HARNESS_VERSION,
            python=Path(sys.executable),
            chromium=supervisor.install.chromium.ChromiumState(
                version="x", platform="mac-arm64", present=True, executable=exe
            ),
        ),
    )

    def new_relay() -> FakeRelay:
        relay = FakeRelay()
        original = relay.send

        async def send(method: str, params: Any = None, session_id: Any = None) -> Any:
            events.append(method)
            return await original(method, params, session_id)

        relay.send = send  # type: ignore[method-assign]
        relays.append(relay)
        return relay

    monkeypatch.setattr(supervisor, "CdpRelay", new_relay)
    sup = HarnessSupervisor(home, config_reader=lambda: {}, credential_provider=lambda: provider)
    monkeypatch.setattr(sup, "runtime_dir", lambda: home / "run")
    choice = supervisor.discover.BrowserChoice(kind, exe, kind == "kiro_chromium", None)  # type: ignore[arg-type]
    monkeypatch.setattr(sup, "_choose", lambda state: choice)

    async def launch(self: HarnessSupervisor, argv: list[str], *_rest: Any) -> Any:
        argvs.append(argv)
        return _Proc(), "ws://127.0.0.1:1/x"

    monkeypatch.setattr(HarnessSupervisor, "_launch_chromium_pipe", launch)
    monkeypatch.setattr(HarnessSupervisor, "_launch_chromium_port", launch)

    async def user_ws(_choice: Any) -> str:
        return "ws://127.0.0.1:1/devtools/browser/x"

    async def start_daemon(python: Any, relay_url: str) -> Any:
        events.append("daemon")
        return _Proc()

    async def start_worker(handle: Any) -> Any:
        return _Proc()

    async def nothing(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(sup, "_user_browser_ws_url", user_ws)
    monkeypatch.setattr(sup, "_start_daemon", start_daemon)
    monkeypatch.setattr(sup, "_start_worker", start_worker)
    monkeypatch.setattr(sup, "_ipc_shutdown", nothing)
    monkeypatch.setattr(sup, "_shutdown_stray_daemon", nothing)
    monkeypatch.setattr(supervisor, "_stop_process", nothing)
    return sup, events, relays, argvs


@pytest.mark.asyncio
async def test_cookies_land_before_the_daemon_and_again_after_a_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider()
    sup, events, relays, _argvs = _supervisor_with_fakes(tmp_path, monkeypatch, provider)
    assert sup.status()["credentials"]["state"] == credentials.STATE_PENDING
    handle = await sup.ensure()
    assert events == ["Storage.setCookies", "daemon"]
    status = sup.status()["credentials"]
    assert status["state"] == credentials.STATE_APPLIED and status["applied"] == 1
    # The browser died: the next ensure() relaunches it and fills it again.
    handle.daemon.returncode = 1  # type: ignore[union-attr]
    await sup.ensure()
    assert events == ["Storage.setCookies", "daemon", "Storage.setCookies", "daemon"]
    assert len(relays) == 2 and relays[1].cookies_set()[0]["name"] == "session"
    assert [t.reason for t in provider.targets] == ["start", "start"]
    assert SECRET not in json.dumps(sup.status())


@pytest.mark.asyncio
async def test_the_person_s_chrome_gets_no_cookies_from_the_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider()
    sup, events, relays, argvs = _supervisor_with_fakes(
        tmp_path, monkeypatch, provider, kind="user_chrome"
    )
    await sup.ensure()
    assert events == ["daemon"] and argvs == [] and provider.targets == []
    assert sup.status()["credentials"]["state"] == credentials.STATE_SKIPPED


@pytest.mark.asyncio
async def test_without_a_provider_the_start_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup, events, _relays, _argvs = _supervisor_with_fakes(
        tmp_path, monkeypatch, DefaultBrowserCredentialProvider()
    )
    assert sup.status()["credentials"]["state"] == credentials.STATE_NONE
    await sup.ensure()
    assert events == ["daemon"]
    assert await sup.refresh_credentials_if_source_changed() is False
    assert sup.status()["credentials"]["provider"] is None


# ── opt-in, domains, provider status ──


def test_settings_precedence_config_then_edition_then_off() -> None:
    edition = FakeProvider(defaults={"enabled": True, "domains": ["example.com"]})
    assert credentials.effective_settings(None, edition) == credentials.CredentialSettings(
        True, ("example.com",)
    )
    owner = credentials.effective_settings(
        {"enabled": False, "domains": ["app.example.com"]}, edition
    )
    assert owner == credentials.CredentialSettings(False, ("app.example.com",))
    # An empty domains list is "unset": the edition's list applies.
    mixed = credentials.effective_settings({"enabled": True, "domains": []}, edition)
    assert mixed == credentials.CredentialSettings(True, ("example.com",))
    silent = FakeProvider(defaults={})
    assert credentials.effective_settings(None, silent) == credentials.CredentialSettings()
    # Without a provider nothing can supply defaults; the owner's values still read back.
    none = credentials.effective_settings(
        {"enabled": True, "domains": ["x.example"]}, DefaultBrowserCredentialProvider()
    )
    assert none == credentials.CredentialSettings(True, ("x.example",))


@pytest.mark.asyncio
async def test_a_disabled_provider_is_not_asked_for_cookies() -> None:
    relay = FakeRelay()
    provider = FakeProvider(defaults={})
    status = await _apply(relay, provider)
    assert relay.sent == [] and provider.targets == []
    assert (status.state, status.reason) == (credentials.STATE_DISABLED, "disabled")
    assert status.as_dict()["provider"] == "Test sign-in"


@pytest.mark.asyncio
async def test_only_domains_both_lists_allow_are_set(tmp_path: Path) -> None:
    relay = FakeRelay()
    provider = FakeProvider(
        [
            _cookie(),
            _cookie(name="app", domain="app.example.com"),
            _cookie(name="deep", domain="a.app.example.com"),
        ]
    )
    settings = credentials.CredentialSettings(True, ("app.example.com",))
    await _apply(relay, provider, settings=settings)
    assert {c["name"] for c in relay.cookies_set()} == {"app", "deep"}


@pytest.mark.asyncio
async def test_the_provider_status_is_reduced_and_needs_refresh_is_a_state(tmp_path: Path) -> None:
    provider = FakeProvider()
    provider.status_value = {
        "needs_refresh": True,
        "message": "Run the sign-in tool\x1b[31m again" + "x" * 500,
        "earliest_expiry": float("nan"),
        "count": True,
        "secret": SECRET,
    }
    status = await _apply(FakeRelay(), provider)
    shown = status.as_dict()["provider_status"]
    assert set(shown) == {"needs_refresh", "message"} and shown["needs_refresh"] is True
    assert "\x1b" not in shown["message"] and len(shown["message"]) == 200
    assert status.state == credentials.STATE_NEEDS_REFRESH
    assert SECRET not in json.dumps(status.as_dict())


@pytest.mark.asyncio
async def test_a_missing_or_expired_source_is_needs_refresh_not_a_crash(tmp_path: Path) -> None:
    from kiro_crew.browser_harness.credential_files import CredentialSourceError

    missing = FakeProvider(raises=CredentialSourceError("source_missing"))
    status = await _apply(FakeRelay(), missing)
    assert (status.state, status.reason) == (credentials.STATE_NEEDS_REFRESH, "source_missing")
    stale = FakeProvider([_cookie(expires=10.0)])
    status = await _apply(FakeRelay(), stale)
    assert (status.state, status.reason) == (credentials.STATE_NEEDS_REFRESH, "source_expired")


# ── the supervisor: audit, and the re-pull before a navigation ──


class _Sel:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.rows.append(kw)


@pytest.mark.asyncio
async def test_each_injection_writes_one_audit_row_without_a_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kiro_crew.sel as sel_module

    fake = _Sel()
    monkeypatch.setattr(sel_module, "sel", lambda: fake)
    sup, _events, _relays, _argvs = _supervisor_with_fakes(tmp_path, monkeypatch, FakeProvider())
    await sup.ensure()
    (row,) = fake.rows
    assert row["operation"] == "browser_harness.credentials_injected"
    assert row["resources"] == "provider=Test sign-in reason=start cookies=1 domains=example.com"
    assert SECRET not in json.dumps(fake.rows)


@pytest.mark.asyncio
async def test_a_changed_fingerprint_re_pulls_before_the_next_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider()
    provider.fp = "v1"
    sup, events, _relays, _argvs = _supervisor_with_fakes(tmp_path, monkeypatch, provider)
    await sup.ensure()
    assert await sup.refresh_credentials_if_source_changed() is False
    provider.fp = "v2"  # the person re-ran their sign-in tool
    assert await sup.refresh_credentials_if_source_changed() is True
    assert [t.reason for t in provider.targets] == ["start", "source_changed"]
    assert await sup.refresh_credentials_if_source_changed() is False
    provider.fp = None  # cannot tell: never re-pulled
    assert await sup.refresh_credentials_if_source_changed() is False
    assert events.count("Storage.setCookies") == 2


@pytest.mark.asyncio
async def test_the_op_route_asks_before_navigating_and_never_fails_the_op() -> None:
    from kiro_crew.dashboard.handlers import browser_ops

    assert {"navigate", "tabs"} <= browser_ops._NAVIGATING_OPS
    assert "click" not in browser_ops._NAVIGATING_OPS

    class Broken:
        async def refresh_credentials_if_source_changed(self) -> bool:
            raise RuntimeError(SECRET)

    await browser_ops._refresh_credentials(Broken())
    await browser_ops._refresh_credentials(object())  # a supervisor without the hook


# ── file hygiene helpers ──


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner and mode bits")
def test_read_source_file_refuses_what_a_provider_must_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from kiro_crew.browser_harness import credential_files as cf

    def code(path: Path, **kw: Any) -> str:
        with pytest.raises(cf.CredentialSourceError) as err:
            cf.read_source_file(path, **kw)
        assert str(tmp_path) not in str(err.value)
        return err.value.code

    good = tmp_path / "jar"
    good.write_bytes(b"data")
    os.chmod(good, 0o600)
    assert cf.read_source_file(good) == b"data"
    assert code(tmp_path / "absent") == cf.SOURCE_MISSING
    link = tmp_path / "link"
    link.symlink_to(good)
    assert code(link) == cf.SOURCE_SYMLINK
    assert code(tmp_path) == cf.SOURCE_NOT_REGULAR
    assert code(good, max_bytes=3) == cf.SOURCE_TOO_LARGE
    os.chmod(good, 0o640)
    assert code(good) == cf.SOURCE_PERMISSIONS
    os.chmod(good, 0o600)
    monkeypatch.setattr(cf.platform_compat, "effective_uid", lambda: os.getuid() + 1)
    assert code(good) == cf.SOURCE_NOT_OWNED


def test_source_fingerprint_moves_when_the_file_does(tmp_path: Path) -> None:
    import os

    from kiro_crew.browser_harness.credential_files import source_fingerprint

    path = tmp_path / "jar"
    assert source_fingerprint(path) is None
    path.write_bytes(b"one")
    first = source_fingerprint(path)
    path.write_bytes(b"two-longer")
    os.utime(path, ns=(1, 2_000_000_000))
    assert first is not None and source_fingerprint(path) != first


@pytest.mark.asyncio
async def test_a_flipped_switch_reads_back_at_once_and_reaches_the_next_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Settings switch is bound to this ``enabled``: it must read back as flipped
    while the browser still holds the last apply, and the next navigation applies it."""
    provider = FakeProvider()
    sup, events, _relays, _argvs = _supervisor_with_fakes(tmp_path, monkeypatch, provider)
    cfg: dict[str, Any] = {"credentials": {"enabled": False}}
    sup._config_reader = lambda: cfg
    await sup.ensure()
    assert sup.status()["credentials"]["state"] == credentials.STATE_DISABLED
    cfg["credentials"] = {"enabled": True}
    snap = sup.status()["credentials"]
    assert snap["enabled"] is True and snap["state"] == credentials.STATE_PENDING
    assert await sup.refresh_credentials_if_source_changed() is True
    assert events.count("Storage.setCookies") == 1
    assert sup.status()["credentials"]["state"] == credentials.STATE_APPLIED
    assert await sup.refresh_credentials_if_source_changed() is False


@pytest.mark.asyncio
async def test_a_provider_status_that_hangs_does_not_hold_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    gate = threading.Event()

    class Hangs(FakeProvider):
        def status(self) -> Any:
            gate.wait()
            return {"count": 1}

    monkeypatch.setattr(credentials, "PROVIDER_TIMEOUT_SECS", 0.05)
    try:
        assert await credentials._provider_status(Hangs()) is None
    finally:
        gate.set()


@pytest.mark.asyncio
async def test_a_slow_re_pull_never_holds_the_navigation_past_its_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    gate = threading.Event()

    class Slow(FakeProvider):
        def cookies(self, target: Any) -> list[dict[str, Any]]:
            if target.reason == "source_changed":
                gate.wait()
            return super().cookies(target)

    provider = Slow()
    provider.fp = "v1"
    sup, _events, _relays, _argvs = _supervisor_with_fakes(tmp_path, monkeypatch, provider)
    await sup.ensure()
    monkeypatch.setattr(supervisor, "NAV_REFRESH_BUDGET_SECS", 0.05)
    provider.fp = "v2"
    try:
        assert await asyncio.wait_for(sup.refresh_credentials_if_source_changed(), 30.0) is False
    finally:
        gate.set()
    assert sup.status()["credentials"]["reason"] == "refresh_timeout"
    # The old key is kept, so the next navigation tries again.
    monkeypatch.setattr(supervisor, "NAV_REFRESH_BUDGET_SECS", 5.0)
    assert await sup.refresh_credentials_if_source_changed() is True
