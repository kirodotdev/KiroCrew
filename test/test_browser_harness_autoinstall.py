"""The startup auto-install (``browser_harness_status.auto_setup_browser_harness``).

No network, no pip, no browser: ``harness_install.install`` is replaced, and the
supervisor is a stand-in whose status the test scripts. What is pinned: what gets
installed (the Harness when absent or stale, Chromium only when discovery would
launch it), that the job is the Settings job, that only a headless Kiro Chromium
is warmed, one attempt per gateway, and the test/CI skip.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.browser_harness import discover
from kiro_crew.browser_harness import install as harness_install
from kiro_crew.browser_harness.supervisor import HarnessSupervisor
from kiro_crew.dashboard.handlers import browser_harness_status as routes
from kiro_crew.testing.wait import await_until

#: A gateway's environment: none of the skip markers set.
_GATEWAY_ENV: dict[str, str] = {}


class _State:
    pass


class _FakeSupervisor(HarnessSupervisor):
    """A supervisor whose status the test sets; ``test()`` records instead of launching."""

    def __init__(self, home: Path, *, kind: str, headless: bool, installed: bool, chromium: bool):
        super().__init__(home, config_reader=lambda: {})
        self.kind, self.headless = kind, headless
        self.installed, self.chromium_present = installed, chromium
        self.tested: list[float] = []

    def status(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "browser": {"kind": self.kind, "headless": self.headless},
            "chromium": {"present": self.chromium_present},
            "connection": "disconnected",
        }

    async def test(self, wait_secs: float) -> dict[str, Any]:
        self.tested.append(wait_secs)
        return self.status()


@pytest.fixture
def installs(monkeypatch) -> list[tuple[str, ...]]:
    """Every install the job runs, by components; the fake succeeds at once."""
    seen: list[tuple[str, ...]] = []

    async def fake_install(home, progress, *, components, refresh=False):
        seen.append(components)
        return harness_install.install_state(home)

    monkeypatch.setattr(harness_install, "install", fake_install)
    return seen


def _current(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(harness_install, "harness_current", lambda _home: value)


def test_a_test_rig_gateway_outside_pytest_and_ci_is_skipped_too() -> None:
    # scripts/gui-user-test/boot.sh and testing/harness.py pin the fake-agent mode;
    # neither sets PYTEST_CURRENT_TEST or CI when run by hand.
    assert "KIROCREW_FAKE_ACP_TEST_MODE" in routes.AUTO_SKIP_ENV
    # A real cloud microVM gateway sets this one, and needs its browser.
    assert "KIROCREW_SKIP_MODEL_DOWNLOAD" not in routes.AUTO_SKIP_ENV


@pytest.mark.asyncio
@pytest.mark.parametrize("name", routes.AUTO_SKIP_ENV)
async def test_a_test_or_ci_process_never_installs(tmp_path, installs, name) -> None:
    sup = _FakeSupervisor(
        tmp_path, kind="kiro_chromium", headless=True, installed=False, chromium=False
    )
    state = _State()
    outcome = await routes.auto_setup_browser_harness(state, supervisor=sup, environ={name: "1"})
    assert outcome == routes.AUTO_SKIPPED
    assert installs == [] and sup.tested == []
    assert not hasattr(state, "_browser_harness_install_job")


@pytest.mark.asyncio
async def test_a_false_spelled_marker_does_not_skip(tmp_path, installs, monkeypatch) -> None:
    _current(monkeypatch, False)
    sup = _FakeSupervisor(
        tmp_path, kind="user_chrome", headless=False, installed=False, chromium=False
    )
    outcome = await routes.auto_setup_browser_harness(
        _State(), supervisor=sup, environ={"CI": "false", "PYTEST_CURRENT_TEST": ""}
    )
    assert outcome == routes.AUTO_READY
    assert installs == [("harness",)]


@pytest.mark.asyncio
async def test_with_the_users_chrome_only_the_harness_installs_and_nothing_connects(
    tmp_path, installs, monkeypatch
) -> None:
    """Chromium is not downloaded where Chrome will be driven, and Chrome is not
    connected at startup: connecting is what raises its Allow sheet."""
    _current(monkeypatch, False)
    sup = _FakeSupervisor(
        tmp_path, kind="user_chrome", headless=False, installed=False, chromium=False
    )
    state = _State()
    outcome = await routes.auto_setup_browser_harness(state, supervisor=sup, environ=_GATEWAY_ENV)
    assert outcome == routes.AUTO_READY
    assert installs == [("harness",)]
    job = state._browser_harness_install_job
    assert job.status == routes.STATUS_SUCCEEDED
    assert sup.tested == []


@pytest.mark.asyncio
async def test_the_fallback_gets_chromium_too_and_a_headless_one_is_warmed(
    tmp_path, installs, monkeypatch
) -> None:
    _current(monkeypatch, False)
    sup = _FakeSupervisor(
        tmp_path, kind="kiro_chromium", headless=True, installed=False, chromium=False
    )

    async def fake_install(home, progress, *, components, refresh=False):
        installs.append(components)
        sup.installed, sup.chromium_present = True, True
        return harness_install.install_state(home)

    monkeypatch.setattr(harness_install, "install", fake_install)
    outcome = await routes.auto_setup_browser_harness(
        _State(), supervisor=sup, environ=_GATEWAY_ENV
    )
    assert outcome == routes.AUTO_WARMED
    assert installs == [("harness", "chromium")]
    assert sup.tested == [routes.TEST_WAIT_SECS]


@pytest.mark.asyncio
async def test_a_headed_kiro_chromium_is_installed_but_not_opened(
    tmp_path, installs, monkeypatch
) -> None:
    _current(monkeypatch, True)
    sup = _FakeSupervisor(
        tmp_path, kind="kiro_chromium", headless=False, installed=True, chromium=False
    )
    outcome = await routes.auto_setup_browser_harness(
        _State(), supervisor=sup, environ=_GATEWAY_ENV
    )
    assert outcome == routes.AUTO_READY
    assert installs == [("chromium",)]
    assert sup.tested == []


@pytest.mark.asyncio
async def test_a_current_install_starts_no_job(tmp_path, installs, monkeypatch) -> None:
    _current(monkeypatch, True)
    sup = _FakeSupervisor(
        tmp_path, kind="user_chrome", headless=False, installed=True, chromium=False
    )
    state = _State()
    outcome = await routes.auto_setup_browser_harness(state, supervisor=sup, environ=_GATEWAY_ENV)
    assert outcome == routes.AUTO_READY
    assert installs == [] and not hasattr(state, "_browser_harness_install_job")


@pytest.mark.asyncio
async def test_a_failure_is_one_attempt_and_stays_on_the_job(tmp_path, monkeypatch) -> None:
    _current(monkeypatch, False)
    calls: list[tuple[str, ...]] = []

    async def failing_install(home, progress, *, components, refresh=False):
        calls.append(components)
        raise harness_install.InstallError(harness_install.STAGE_INSTALLING_HARNESS, "offline")

    monkeypatch.setattr(harness_install, "install", failing_install)
    sup = _FakeSupervisor(
        tmp_path, kind="user_chrome", headless=False, installed=False, chromium=False
    )
    state = _State()
    first = await routes.auto_setup_browser_harness(state, supervisor=sup, environ=_GATEWAY_ENV)
    again = await routes.auto_setup_browser_harness(state, supervisor=sup, environ=_GATEWAY_ENV)
    assert (first, again) == (routes.AUTO_INSTALL_FAILED, routes.AUTO_ALREADY_ATTEMPTED)
    assert calls == [("harness",)]
    snap = state._browser_harness_install_job.snapshot()
    assert snap["status"] == routes.STATUS_FAILED and "offline" in snap["error"]


@pytest.mark.asyncio
async def test_an_install_the_owner_already_started_is_left_alone(
    tmp_path, installs, monkeypatch
) -> None:
    _current(monkeypatch, False)
    state = _State()
    state._browser_harness_install_job = routes.InstallJob.start(("harness",))
    sup = _FakeSupervisor(
        tmp_path, kind="user_chrome", headless=False, installed=False, chromium=False
    )
    outcome = await routes.auto_setup_browser_harness(state, supervisor=sup, environ=_GATEWAY_ENV)
    assert outcome == routes.AUTO_INSTALL_RUNNING
    assert installs == []


@pytest.mark.asyncio
async def test_settings_sees_the_auto_job_running_and_refuses_a_second(
    tmp_path, monkeypatch
) -> None:
    _current(monkeypatch, False)
    release = asyncio.Event()

    entered = asyncio.Event()

    async def slow_install(home, progress, *, components, refresh=False):
        entered.set()
        await release.wait()
        return harness_install.install_state(home)

    monkeypatch.setattr(harness_install, "install", slow_install)
    sup = _FakeSupervisor(
        tmp_path, kind="user_chrome", headless=False, installed=False, chromium=False
    )
    state = _State()
    task = asyncio.create_task(
        routes.auto_setup_browser_harness(state, supervisor=sup, environ=_GATEWAY_ENV)
    )
    await await_until(entered, what="the auto job's install began")
    assert routes._install_active(state)

    class _Req:
        app = {"state": state, "browser_harness_supervisor": sup}

    payload = routes._status_payload(_Req())  # what GET /api/browser/status answers
    assert payload["installing"] is True
    assert payload["install_job"]["status"] == routes.STATUS_RUNNING
    release.set()
    assert await task == routes.AUTO_READY


def test_discovery_decides_whether_chromium_is_needed(tmp_path, monkeypatch) -> None:
    """The real ``_auto_components`` over the real discovery order, host facts injected."""
    _current(monkeypatch, True)
    chrome = discover.HostFacts(chrome=Path("/Applications/Chrome"), chrome_profile=tmp_path)
    monkeypatch.setattr(discover, "probe_host", lambda *_a, **_k: chrome)
    auto = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    assert routes._auto_components(auto) == ()
    pinned = HarnessSupervisor(tmp_path, config_reader=lambda: {"choice": "kiro_chromium"})
    assert routes._auto_components(pinned) == ("chromium",)
    monkeypatch.setattr(discover, "probe_host", lambda *_a, **_k: discover.HostFacts())
    assert routes._auto_components(auto) == ("chromium",)


def test_harness_current_reads_the_lock_digest(tmp_path, monkeypatch) -> None:
    prefix = harness_install.harness_prefix(tmp_path)
    python = harness_install.venv_python(prefix)
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    marker = prefix / harness_install.MARKER_FILE

    def write(lock: str) -> None:
        body = {"version": harness_install.HARNESS_VERSION, "lock_sha256": lock}
        marker.write_text(json.dumps(body), encoding="utf-8")

    assert harness_install.harness_current(tmp_path) is False
    write("stale")
    assert harness_install.harness_current(tmp_path) is False
    write(harness_install.lock_digest())
    assert harness_install.harness_current(tmp_path) is True


@pytest.mark.asyncio
async def test_the_startup_hook_reclaims_first_then_auto_sets_up(monkeypatch) -> None:
    """The on_startup hook does not await the work; the task stops a stray daemon,
    then runs the auto-setup, in that order."""
    from aiohttp import web

    from kiro_crew.dashboard import server
    from kiro_crew.dashboard.handlers import browser_harness_status

    order: list[str] = []

    class _Sup:
        async def stop(self) -> None:
            order.append("stop")

    async def fake_auto(state, **_kw) -> str:
        order.append("auto")
        return routes.AUTO_READY

    monkeypatch.setattr("kiro_crew.browser_harness.supervisor.get_supervisor", lambda _home: _Sup())
    monkeypatch.setattr(browser_harness_status, "auto_setup_browser_harness", fake_auto)

    class _DashState:
        def __init__(self) -> None:
            self._background_tasks: set[asyncio.Task] = set()

    state = _DashState()
    app = web.Application()
    server._register_browser_harness_lifecycle(app, state)
    unregister: list[str] = []
    monkeypatch.setattr(
        server, "register_pre_exec_stop", lambda *_a: lambda: unregister.append("x")
    )
    for hook in app.on_startup:
        await hook(app)
    await asyncio.gather(*list(state._background_tasks))
    assert order == ["stop", "auto"]
