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
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import requires_symlinks
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
@pytest.mark.parametrize("finish_early", [False, True], ids=["running", "failed"])
async def test_startup_leaves_a_job_started_during_discovery_alone(
    tmp_path, monkeypatch, finish_early
) -> None:
    _current(monkeypatch, False)
    sup = _FakeSupervisor(
        tmp_path, kind="user_chrome", headless=False, installed=False, chromium=False
    )
    state = SimpleNamespace()
    discovering, resume_discovery = asyncio.Event(), asyncio.Event()
    installing, finish_install = asyncio.Event(), asyncio.Event()
    to_thread = asyncio.to_thread
    runs: list[tuple[str, ...]] = []

    async def gated_to_thread(func, /, *args, **kwargs):
        if func is routes._auto_components:
            discovering.set()
            await await_until(resume_discovery, what="startup discovery released")
        return await to_thread(func, *args, **kwargs)

    async def fake_install(home, progress, *, components, refresh=False):
        runs.append(components)
        if len(runs) == 1:
            installing.set()
            await await_until(finish_install, what="the existing install released")
            raise harness_install.InstallError("preparing", "offline")
        return harness_install.install_state(home)

    monkeypatch.setattr(routes.asyncio, "to_thread", gated_to_thread)
    monkeypatch.setattr(harness_install, "install", fake_install)
    startup = asyncio.create_task(
        routes.auto_setup_browser_harness(state, supervisor=sup, environ=_GATEWAY_ENV)
    )
    existing_task = None
    try:
        await await_until(discovering, what="startup awaiting component discovery")
        routes._start_install_job(state, (harness_install.COMPONENT_HARNESS,))
        existing_job = state._browser_harness_install_job
        existing_task = state._browser_harness_install_task
        await await_until(installing, what="the competing install started")
        if finish_early:
            finish_install.set()
            await await_until(existing_task, what="the competing install failed")
        resume_discovery.set()
        outcome = await await_until(startup, what="startup observed the existing job")
        assert state._browser_harness_install_job is existing_job
        assert state._browser_harness_install_task is existing_task
        assert runs == [(harness_install.COMPONENT_HARNESS,)]
        assert outcome == (
            routes.AUTO_INSTALL_FAILED if finish_early else routes.AUTO_INSTALL_RUNNING
        )
        assert existing_job.status == (
            routes.STATUS_FAILED if finish_early else routes.STATUS_RUNNING
        )
        assert sup.tested == []
    finally:
        resume_discovery.set()
        finish_install.set()
        tasks = {startup}
        if existing_task is not None:
            tasks.add(existing_task)
        current_task = getattr(state, "_browser_harness_install_task", None)
        if current_task is not None:
            tasks.add(current_task)
        await await_until(asyncio.gather(*tasks), what="startup and install cleanup")


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


# ── an install that breaks after the startup check ──


def _install_whose_interpreter_is_gone(home: Path) -> None:
    """A finished install whose venv links to a Python an update has since removed."""
    prefix = harness_install.harness_prefix(home)
    python = harness_install.venv_python(prefix)
    python.parent.mkdir(parents=True)
    python.symlink_to(home / "runtime-0.0.1" / "bin" / "python3")
    marker = {"version": harness_install.HARNESS_VERSION, "lock_sha256": "x", "source": "runtime"}
    (prefix / harness_install.MARKER_FILE).write_text(json.dumps(marker), encoding="utf-8")


@requires_symlinks
@pytest.mark.asyncio
async def test_an_install_broken_after_startup_is_reinstalled_once_without_a_click(
    tmp_path, installs, monkeypatch
) -> None:
    _install_whose_interpreter_is_gone(tmp_path)
    assert harness_install.install_state(tmp_path).installed is False
    sup = _FakeSupervisor(
        tmp_path, kind="kiro_chromium", headless=True, installed=False, chromium=True
    )
    state = _State()
    # The startup check already ran and found the install whole.
    state._browser_harness_auto_attempted = True
    _current(monkeypatch, False)
    assert await routes.repair_browser_harness(state, supervisor=sup, environ=_GATEWAY_ENV)
    await await_until(state._browser_harness_install_task, what="the repair's install finished")
    assert installs == [(harness_install.COMPONENT_HARNESS,)]
    assert state._browser_harness_install_job.status == routes.STATUS_SUCCEEDED
    # Once per gateway: a repair that did not take is the person's Retry, not a loop.
    assert not await routes.repair_browser_harness(state, supervisor=sup, environ=_GATEWAY_ENV)
    assert len(installs) == 1


@requires_symlinks
@pytest.mark.asyncio
async def test_repair_leaves_a_never_installed_home_a_failed_job_and_test_rigs_alone(
    tmp_path, installs, monkeypatch
) -> None:
    _current(monkeypatch, False)
    fresh = _FakeSupervisor(
        tmp_path / "fresh", kind="kiro_chromium", headless=True, installed=False, chromium=True
    )
    assert not await routes.repair_browser_harness(_State(), supervisor=fresh, environ={})
    broken_home = tmp_path / "broken"
    _install_whose_interpreter_is_gone(broken_home)
    broken = _FakeSupervisor(
        broken_home, kind="kiro_chromium", headless=True, installed=False, chromium=True
    )
    failed = _State()
    failed._browser_harness_install_job = routes.InstallJob(
        "j", ("harness",), routes.STATUS_FAILED, "preparing", 0.0, 0.0
    )
    assert not await routes.repair_browser_harness(failed, supervisor=broken, environ={})
    assert not await routes.repair_browser_harness(_State(), supervisor=broken, environ={"CI": "1"})
    assert installs == []


@requires_symlinks
@pytest.mark.asyncio
async def test_two_repairs_at_once_start_one_job_and_keep_its_failure(
    tmp_path, monkeypatch
) -> None:
    """Both callers pass the first checks before either starts a job; the second must
    not replace the first's job, even after it failed fast."""
    _install_whose_interpreter_is_gone(tmp_path)
    _current(monkeypatch, False)
    runs: list[tuple[str, ...]] = []

    async def failing_install(home, progress, *, components, refresh=False):
        runs.append(components)
        raise harness_install.InstallError("preparing", "boom")

    monkeypatch.setattr(harness_install, "install", failing_install)
    gates = [asyncio.Event(), asyncio.Event()]
    waiting: list[int] = []
    both_waiting = asyncio.Event()

    async def gated_policy(_key: str) -> bool:
        mine = len(waiting)
        waiting.append(mine)
        if len(waiting) == 2:
            both_waiting.set()
        await await_until(gates[mine], what=f"policy check {mine} released")
        return False

    monkeypatch.setattr(routes, "browse_denied_async", gated_policy)
    sup = _FakeSupervisor(
        tmp_path, kind="kiro_chromium", headless=True, installed=False, chromium=True
    )
    state = _State()
    first = asyncio.create_task(
        routes.repair_browser_harness(state, supervisor=sup, environ=_GATEWAY_ENV)
    )
    second = asyncio.create_task(
        routes.repair_browser_harness(state, supervisor=sup, environ=_GATEWAY_ENV)
    )
    try:
        await await_until(both_waiting, what="both repairs at the policy check")
        # Either task may have reached the check first; release that one alone.
        gates[0].set()
        done, pending = await await_until(
            asyncio.ensure_future(
                asyncio.wait({first, second}, return_when=asyncio.FIRST_COMPLETED)
            ),
            what="one repair finished",
        )
        assert len(done) == 1 and done.pop().result() is True
        await await_until(state._browser_harness_install_task, what="the repair's install failed")
        failed = state._browser_harness_install_job
        assert failed.status == routes.STATUS_FAILED
        gates[1].set()
        assert not await await_until(pending.pop(), what="the second repair returned")
        assert state._browser_harness_install_job is failed
        assert runs == [(harness_install.COMPONENT_HARNESS,)]
    finally:
        for gate in gates:
            gate.set()
        tasks = {first, second}
        install_task = getattr(state, "_browser_harness_install_task", None)
        if install_task is not None:
            tasks.add(install_task)
        await await_until(
            asyncio.gather(*tasks, return_exceptions=True), what="repairs and install cleanup"
        )
