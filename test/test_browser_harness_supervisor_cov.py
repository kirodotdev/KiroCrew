"""The supervisor's lifecycle edges without a browser: lock and cancel paths, the
launchers' failure cleanup, the daemon's IPC client, the worker protocol's fatal
replies, credential snapshots and the audit row.

Every spawn, relay and worker here is a stand-in; the IPC tests use a local
AF_UNIX or loopback listener this test owns.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew.browser_harness import HARNESS_VERSION, chromium, credentials, discover, supervisor
from kiro_crew.browser_harness.supervisor import HarnessSupervisor
from kiro_crew.testing.wait import await_until

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="pipe fds and AF_UNIX are POSIX")


class _Proc:
    """A spawned child as the supervisor sees it: alive until ``returncode`` is set."""

    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.stdin: Any = None


class _Relay:
    """The slice of :class:`CdpRelay` the supervisor touches at start and teardown."""

    def __init__(self, *, closed: bool = False, fail_close_targets: bool = False) -> None:
        self.closed = closed
        self.url = "http://127.0.0.1:1/s"
        self.on_upstream_closed: Any = None
        self.targets_closed = False
        self._fail = fail_close_targets
        self.start_gate: asyncio.Event | None = None
        self.started = asyncio.Event()

    async def start(self, upstream: Any) -> str:
        self.started.set()
        if self.start_gate is not None:
            await self.start_gate.wait()
        return self.url

    async def close_agent_targets(self) -> None:
        if self._fail:
            raise RuntimeError("target already gone")
        self.targets_closed = True

    async def close(self) -> None:
        self.closed = True

    def agent_targets(self) -> list[str]:
        return []


class _Provider:
    """An edition credential provider with a name, a fingerprint and defaults."""

    def __init__(
        self,
        name: str | None = "Corp",
        fingerprint: str = "fp-1",
        defaults: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._fp = fingerprint
        self._defaults = defaults or {}

    def name(self) -> str | None:
        return self._name

    def fingerprint(self) -> str:
        return self._fp

    def default_settings(self) -> dict[str, Any]:
        return self._defaults


def _choice(kind: str = "kiro_chromium", profile: Path | None = None) -> discover.BrowserChoice:
    return discover.BrowserChoice(kind, None, True, profile)  # type: ignore[arg-type]


def _handle(
    *,
    relay: Any = None,
    daemon: Any = None,
    worker: Any = None,
    browser: Any = None,
    kind: str = "kiro_chromium",
) -> supervisor.HarnessHandle:
    return supervisor.HarnessHandle(
        choice=_choice(kind),
        relay=relay if relay is not None else _Relay(),
        daemon=daemon,
        worker=worker,
        browser=browser,
    )


def _installed(home: Path, *, python: Path | None, exe: Path | None = None) -> Any:
    return supervisor.install.InstallState(
        prefix=home,
        installed=True,
        version=HARNESS_VERSION,
        python=python,
        chromium=chromium.ChromiumState(
            version="x", platform="mac-arm64", present=exe is not None, executable=exe
        ),
    )


@pytest.fixture
def stopped(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record every child the supervisor stops instead of signalling anything."""
    seen: list[Any] = []

    async def _stop(proc: Any) -> None:
        if proc is not None:
            seen.append(proc)

    monkeypatch.setattr(supervisor, "_stop_process", _stop)
    return seen


@pytest.fixture
def short_dir() -> Any:
    """A directory short enough to bind an AF_UNIX socket in."""
    path = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX + "bhs-", dir=short_tmp_base()))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# ── paths ──


def test_on_windows_the_runtime_dir_is_the_leaf_with_no_socket_length_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor.platform_compat, "IS_WINDOWS", True)
    deep = HarnessSupervisor(tmp_path / ("x" * 150))
    assert deep.runtime_dir() == deep.ipc_dir / "run"


def test_a_home_that_cannot_be_resolved_is_measured_as_spelled_and_resolved_once() -> None:
    calls: list[int] = []

    class _Unresolvable:
        def resolve(self) -> Any:
            calls.append(1)
            raise OSError("loop in symlinks")

    sup = HarnessSupervisor(Path("/tmp/k"))
    odd = _Unresolvable()
    sup.home = odd  # type: ignore[assignment]
    assert sup._resolved_home() is odd
    assert sup._resolved_home() is odd
    assert calls == [1]


# ── credentials snapshot and provider seam ──


def test_a_named_provider_with_no_live_browser_reads_pending_or_disabled(tmp_path: Path) -> None:
    cfg: dict[str, Any] = {"credentials": {"enabled": True, "domains": ["a.test"]}}
    sup = HarnessSupervisor(
        tmp_path, config_reader=lambda: cfg, credential_provider=lambda: _Provider()
    )
    snap = sup._credentials_snapshot()
    assert snap["provider"] == "Corp" and snap["enabled"] is True
    assert snap["state"] == credentials.STATE_PENDING
    assert snap["domains"] == ["a.test"]
    cfg["credentials"] = {"enabled": False, "domains": ["a.test"]}
    snap = sup._credentials_snapshot()
    assert snap["state"] == credentials.STATE_DISABLED and snap["reason"] == "disabled"


def test_an_unreadable_config_falls_back_to_the_providers_defaults(tmp_path: Path) -> None:
    def broken() -> dict[str, Any]:
        raise RuntimeError("config is mid-write")

    provider = _Provider(defaults={"enabled": True, "domains": ["d.test"]})
    sup = HarnessSupervisor(tmp_path, config_reader=broken, credential_provider=lambda: provider)
    settings = sup._credential_settings(provider)
    assert settings.enabled is True and settings.domains == ("d.test",)


def test_a_broken_provider_seam_reads_as_no_provider_but_a_composition_error_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from kiro_crew.platform.context import PlatformCompositionError

    def broken() -> Any:
        raise RuntimeError("seam blew up")

    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {}, credential_provider=broken)
    with caplog.at_level(logging.WARNING, logger=supervisor.__name__):
        assert sup._provider() is None
    assert "provider lookup failed" in caplog.text

    def fail_closed() -> Any:
        raise PlatformCompositionError("companion missing")

    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {}, credential_provider=fail_closed)
    with pytest.raises(PlatformCompositionError):
        sup._provider()


# ── ensure / stop / test ──


@pytest.mark.asyncio
async def test_ensure_returns_the_live_handle_without_starting_again(tmp_path: Path) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    live = _handle(daemon=_Proc())
    sup._handle = live

    async def _no_start() -> Any:
        raise AssertionError("a connected supervisor must not start a second browser")

    sup._start = _no_start  # type: ignore[method-assign]
    assert sup.connected is True
    assert await sup.ensure() is live


@pytest.mark.asyncio
async def test_stop_cancels_a_start_in_flight_and_ends_disconnected(tmp_path: Path) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    started = asyncio.Event()

    async def _waits_on_allow() -> Any:
        started.set()
        await asyncio.Event().wait()

    sup._start = _waits_on_allow  # type: ignore[method-assign]
    first = sup.ensure_in_background()
    assert sup.ensure_in_background() is first, "a second caller joins the start in flight"
    await await_until(started, what="the start to reach the Allow wait")
    await sup.stop()
    assert first.cancelled()
    status = sup.status()
    assert status["connection"] == supervisor.DISCONNECTED and status["detail"] == ""


@pytest.mark.asyncio
async def test_test_swallows_a_start_failure_and_reports_status(tmp_path: Path) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})

    async def _fails() -> Any:
        raise RuntimeError("chrome crashed")

    sup.ensure = _fails  # type: ignore[method-assign]
    report = await sup.test(wait_secs=60.0)
    assert report["connection"] == supervisor.DISCONNECTED
    assert "tabs" not in report


@pytest.mark.asyncio
async def test_test_counts_tabs_when_connected_and_reports_a_failed_count(
    tmp_path: Path,
) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    live = _handle(daemon=_Proc())
    sup._handle = live
    answers: list[Any] = [["t1", "t2"], supervisor.HelperError("worker gone")]

    async def _ensure() -> Any:
        return live

    async def _call(helper: str, **kwargs: Any) -> Any:
        assert helper == "list_tabs" and kwargs == {"include_chrome": False}
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    sup.ensure = _ensure  # type: ignore[method-assign]
    sup.call = _call  # type: ignore[method-assign]
    report = await sup.test(wait_secs=60.0)
    assert report["tabs"] == 2
    report = await sup.test(wait_secs=60.0)
    assert report["tabs"] is None and report["detail"] == "worker gone"


# ── call ──


@pytest.mark.asyncio
async def test_call_reuses_a_live_worker_discards_a_fatal_one_and_raises_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stopped: list[Any]
) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    first = _Proc()
    live = _handle(daemon=_Proc(), worker=first)
    replacements: list[_Proc] = []
    sent: list[dict[str, Any]] = []
    replies: list[dict[str, Any]] = [
        {"ok": True, "result": "about:blank"},
        {"ok": False, "error": "helper timed out after 90s", "fatal": True},
        {"ok": False},
    ]

    async def _ensure() -> Any:
        return live

    async def _start_worker(handle: Any) -> Any:
        replacements.append(_Proc())
        return replacements[-1]

    async def _request(proc: Any, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        sent.append({**body, "proc": proc})
        return {"id": body["id"], **replies.pop(0)}

    sup.ensure = _ensure  # type: ignore[method-assign]
    sup._start_worker = _start_worker  # type: ignore[method-assign]
    monkeypatch.setattr(supervisor, "_worker_request", _request)

    assert await sup.call("page_info") == "about:blank"
    assert replacements == [] and sent[0]["proc"] is first
    with pytest.raises(supervisor.HelperError, match="timed out"):
        await sup.call("wait_for_load", timeout=5)
    assert live.worker is None and stopped == [first]
    with pytest.raises(supervisor.HelperError, match="helper failed"):
        await sup.call("current_tab")
    assert sent[2]["proc"] is replacements[0], "the next call starts a fresh worker"
    assert [b["id"] for b in sent] == sorted({b["id"] for b in sent})


# ── _choose / _start ──


def test_choose_uses_defaults_on_a_config_error_and_none_on_a_probe_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> dict[str, Any]:
        raise RuntimeError("unreadable")

    sup = HarnessSupervisor(tmp_path, config_reader=broken)
    seen: list[Any] = []
    picked = _choice()

    def _choose_browser(cfg: Any, **_kw: Any) -> Any:
        seen.append(cfg)
        return picked

    monkeypatch.setattr(supervisor.discover, "choose_browser", _choose_browser)
    state = _installed(tmp_path, python=None)
    assert sup._choose(state) is picked and seen == [{}]

    def _probe_fails(cfg: Any, **_kw: Any) -> Any:
        raise OSError("plist unreadable")

    monkeypatch.setattr(supervisor.discover, "choose_browser", _probe_fails)
    assert sup._choose(state) is None


@pytest.mark.asyncio
async def test_start_with_no_browser_to_pick_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    monkeypatch.setattr(
        supervisor.install, "install_state", lambda home: _installed(home, python=Path("py"))
    )
    monkeypatch.setattr(sup, "runtime_dir", lambda: tmp_path / "run")
    monkeypatch.setattr(sup, "_choose", lambda state: None)
    with pytest.raises(supervisor.BrowserUnavailable, match="could not pick a browser"):
        await sup._start()
    assert sup.status()["connection"] == supervisor.ERROR


@pytest.mark.asyncio
async def test_a_start_cancelled_after_launch_stops_the_browser_and_closes_the_relay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stopped: list[Any]
) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    monkeypatch.setattr(
        supervisor.install, "install_state", lambda home: _installed(home, python=Path("py"))
    )
    monkeypatch.setattr(sup, "runtime_dir", lambda: tmp_path / "run")
    monkeypatch.setattr(sup, "_choose", lambda state: _choice())
    relay = _Relay()
    relay.start_gate = asyncio.Event()
    monkeypatch.setattr(supervisor, "CdpRelay", lambda: relay)
    browser = _Proc()

    async def _launch(choice: Any, state: Any) -> Any:
        return browser, "ws://127.0.0.1:1/devtools/browser/x"

    monkeypatch.setattr(sup, "_launch_chromium", _launch)
    task = asyncio.ensure_future(sup._start())
    await await_until(relay.started, what="the relay to start")
    assert sup.status()["detail"] == "connecting to Kiro's Chromium"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert relay.closed and stopped == [browser]
    status = sup.status()
    assert status["connection"] == supervisor.DISCONNECTED and status["detail"] == ""
    assert sup._handle is None


# ── credentials refresh before a navigation ──


def _refreshable(tmp_path: Path, provider: Any) -> HarnessSupervisor:
    sup = HarnessSupervisor(
        tmp_path,
        config_reader=lambda: {"credentials": {"enabled": True, "domains": ["a.test"]}},
        credential_provider=lambda: provider,
    )
    sup._handle = _handle()
    sup._applied_key = ("fp-old", credentials.CredentialSettings(True, ("a.test",)))
    return sup


@pytest.mark.asyncio
async def test_refresh_does_nothing_for_a_provider_with_no_name(tmp_path: Path) -> None:
    sup = _refreshable(tmp_path, _Provider(name=None))
    assert await sup.refresh_credentials_if_source_changed() is False


@pytest.mark.asyncio
async def test_refresh_skips_while_a_start_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup = _refreshable(tmp_path, _Provider(fingerprint="fp-new"))
    monkeypatch.setattr(supervisor, "CREDENTIAL_LOCK_WAIT_SECS", 0)
    await sup._ensure_lock.acquire()
    try:
        assert await sup.refresh_credentials_if_source_changed() is False
    finally:
        sup._ensure_lock.release()
    assert sup._applied_key[0] == "fp-old", "the old key is kept so the next one retries"


@pytest.mark.asyncio
async def test_refresh_skips_and_releases_the_lock_when_the_browser_went_away(
    tmp_path: Path,
) -> None:
    sup = _refreshable(tmp_path, _Provider(fingerprint="fp-new"))
    # The handle has no daemon: not connected by the time the lock is held.
    assert sup.connected is False
    assert await sup.refresh_credentials_if_source_changed() is False
    assert not sup._ensure_lock.locked()


# ── teardown ──


@pytest.mark.asyncio
async def test_teardown_survives_a_failed_tab_close_and_stops_every_child(
    tmp_path: Path, stopped: list[Any]
) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    shutdowns: list[int] = []

    async def _shutdown() -> None:
        shutdowns.append(1)

    sup._ipc_shutdown = _shutdown  # type: ignore[method-assign]
    worker, daemon, browser = _Proc(), _Proc(), _Proc()
    relay = _Relay(fail_close_targets=True)
    sup._handle = _handle(relay=relay, daemon=daemon, worker=worker, browser=browser)
    await sup._teardown()
    assert stopped == [worker, daemon, browser]
    assert relay.closed and shutdowns == [1] and sup._handle is None

    closed_relay = _Relay(closed=True)
    sup._handle = _handle(relay=closed_relay)
    await sup._teardown()
    assert closed_relay.targets_closed is False, "a closed relay is not asked to close tabs"


@pytest.mark.asyncio
async def test_an_upstream_close_disconnects_only_a_connected_supervisor(tmp_path: Path) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    sup._set(supervisor.CONNECTED, "")
    await sup._on_upstream_closed()
    assert sup._connection == supervisor.DISCONNECTED
    assert sup._detail == "the browser closed the connection"
    sup._set(supervisor.ERROR, "earlier failure")
    await sup._on_upstream_closed()
    assert (sup._connection, sup._detail) == (supervisor.ERROR, "earlier failure")


# ── the person's own browser ──


@pytest.mark.asyncio
async def test_user_browser_url_names_what_the_person_must_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    with pytest.raises(supervisor.BrowserUnavailable, match="Edge has no profile directory"):
        await sup._user_browser_ws_url(_choice("user_edge", None))
    answers: list[str | None] = [None, "ws://127.0.0.1:9/devtools/browser/a"]

    async def _url(profile: Path) -> str | None:
        assert profile == tmp_path
        return answers.pop(0)

    monkeypatch.setattr(supervisor, "_devtools_ws_url", _url)
    with pytest.raises(supervisor.BrowserUnavailable, match="Chrome is not accepting"):
        await sup._user_browser_ws_url(_choice("user_chrome", tmp_path))
    assert sup._connection == supervisor.DISCONNECTED
    assert sup._detail == "turn on Chrome's remote debugging"
    url = await sup._user_browser_ws_url(_choice("user_chrome", tmp_path))
    assert url == "ws://127.0.0.1:9/devtools/browser/a"


# ── Kiro's Chromium launchers ──


@pytest.mark.asyncio
async def test_launch_chromium_refuses_without_a_download_and_adapts_to_root_and_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup = HarnessSupervisor(tmp_path, base_env={})
    with pytest.raises(supervisor.BrowserUnavailable, match="not downloaded"):
        await sup._launch_chromium(_choice(), _installed(tmp_path, python=None))
    exe = tmp_path / "chrome"
    exe.write_text("", encoding="utf-8")
    seen: list[tuple[str, list[str]]] = []

    async def _pipe(self: Any, argv: list[str]) -> Any:
        seen.append(("pipe", argv))
        return _Proc(), "pipe"

    async def _port(self: Any, argv: list[str], profile: Path) -> Any:
        seen.append(("port", argv))
        return _Proc(), "ws://127.0.0.1:1/x"

    monkeypatch.setattr(HarnessSupervisor, "_launch_chromium_pipe", _pipe)
    monkeypatch.setattr(HarnessSupervisor, "_launch_chromium_port", _port)
    monkeypatch.setattr(supervisor.platform_compat, "effective_uid", lambda: 0)
    monkeypatch.setattr(supervisor.platform_compat, "IS_WINDOWS", False)
    state = _installed(tmp_path, python=None, exe=exe)
    await sup._launch_chromium(_choice(), state)
    assert seen[-1][0] == "pipe" and supervisor._NO_SANDBOX_ARG in seen[-1][1]
    monkeypatch.setattr(supervisor.platform_compat, "effective_uid", lambda: 501)
    monkeypatch.setattr(supervisor.platform_compat, "IS_WINDOWS", True)
    _proc, url = await sup._launch_chromium(_choice(), state)
    assert seen[-1][0] == "port" and url == "ws://127.0.0.1:1/x"
    assert supervisor._NO_SANDBOX_ARG not in seen[-1][1]


def _open_fds() -> set[str]:
    return set(os.listdir("/dev/fd"))


@POSIX_ONLY
@pytest.mark.asyncio
async def test_a_pipe_launch_that_cannot_spawn_leaves_no_pipe_end_open(tmp_path: Path) -> None:
    sup = HarnessSupervisor(tmp_path, base_env={})

    async def _spawn(argv: list[str], **kw: Any) -> Any:
        raise OSError("no /bin/sh")

    sup._spawn = _spawn  # type: ignore[method-assign]
    before = _open_fds()
    with pytest.raises(OSError, match="no /bin/sh"):
        await sup._launch_chromium_pipe(["chromium"])
    assert _open_fds() == before


@POSIX_ONLY
@pytest.mark.asyncio
async def test_a_pipe_launch_whose_upstream_fails_stops_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stopped: list[Any]
) -> None:
    sup = HarnessSupervisor(tmp_path, base_env={})
    browser = _Proc()
    passed: list[tuple[int, ...]] = []

    async def _spawn(argv: list[str], **kw: Any) -> Any:
        passed.append(kw["pass_fds"])
        return browser

    async def _upstream(read_fd: int, write_fd: int) -> Any:
        os.close(read_fd)
        os.close(write_fd)
        raise OSError("pipe broke")

    sup._spawn = _spawn  # type: ignore[method-assign]
    monkeypatch.setattr(supervisor, "pipe_upstream_from_fds", _upstream)
    before = _open_fds()
    with pytest.raises(OSError, match="pipe broke"):
        await sup._launch_chromium_pipe(["chromium"])
    assert stopped == [browser]
    assert all(fd >= supervisor._FIRST_SAFE_FD for fd in passed[0])
    assert _open_fds() == before


@pytest.mark.asyncio
async def test_the_port_launcher_clears_a_stale_port_file_and_reads_the_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stopped: list[Any]
) -> None:
    sup = HarnessSupervisor(tmp_path, base_env={})
    port_file = tmp_path / supervisor.DEVTOOLS_PORT_FILE
    port_file.write_text("1\n/devtools/browser/stale\n", encoding="utf-8")
    browser = _Proc()
    argvs: list[list[str]] = []

    async def _spawn(argv: list[str], **kw: Any) -> Any:
        assert not port_file.exists(), "the previous run's port is never trusted"
        argvs.append(argv)
        return browser

    async def _url(profile: Path) -> str:
        return "ws://127.0.0.1:5/devtools/browser/new"

    sup._spawn = _spawn  # type: ignore[method-assign]
    monkeypatch.setattr(supervisor, "_devtools_ws_url", _url)
    proc, url = await sup._launch_chromium_port(["chromium"], tmp_path)
    assert proc is browser and url.endswith("/new")
    assert argvs[0][-1] == supervisor._CHROMIUM_PORT_ARG
    assert stopped == []


@pytest.mark.asyncio
async def test_the_port_launcher_gives_up_when_chromium_exits_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stopped: list[Any]
) -> None:
    sup = HarnessSupervisor(tmp_path, base_env={})
    exited = _Proc(returncode=21)

    async def _spawn(argv: list[str], **kw: Any) -> Any:
        return exited

    async def _no_url(profile: Path) -> None:
        return None

    sup._spawn = _spawn  # type: ignore[method-assign]
    monkeypatch.setattr(supervisor, "_devtools_ws_url", _no_url)
    with pytest.raises(supervisor.BrowserUnavailable, match="did not open its debugging port"):
        await sup._launch_chromium_port(["chromium"], tmp_path)
    assert stopped == [exited]


# ── daemon and worker start ──


@pytest.mark.asyncio
async def test_the_daemon_start_returns_once_the_daemon_answers_its_ping(
    tmp_path: Path, stopped: list[Any]
) -> None:
    sup = HarnessSupervisor(tmp_path, base_env={})
    daemon = _Proc()
    pings = [False, True]
    envs: list[dict[str, str]] = []

    async def _spawn(argv: list[str], **kw: Any) -> Any:
        envs.append(kw["env"])
        return daemon

    async def _ping() -> bool:
        return pings.pop(0)

    async def _no_stray() -> None:
        return None

    sup._spawn = _spawn  # type: ignore[method-assign]
    sup._ipc_ping = _ping  # type: ignore[method-assign]
    sup._shutdown_stray_daemon = _no_stray  # type: ignore[method-assign]
    sup._daemon_env = lambda url: {"BU_CDP_URL": url}  # type: ignore[method-assign]
    assert await sup._start_daemon(Path("python"), "http://relay") is daemon
    assert envs == [{"BU_CDP_URL": "http://relay"}] and pings == [] and stopped == []


@pytest.mark.asyncio
async def test_the_daemon_start_gives_up_past_its_deadline_and_stops_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stopped: list[Any]
) -> None:
    sup = HarnessSupervisor(tmp_path, base_env={})
    daemon = _Proc()

    async def _spawn(argv: list[str], **kw: Any) -> Any:
        return daemon

    async def _no_stray() -> None:
        return None

    sup._spawn = _spawn  # type: ignore[method-assign]
    sup._shutdown_stray_daemon = _no_stray  # type: ignore[method-assign]
    sup._daemon_env = lambda url: {}  # type: ignore[method-assign]
    monkeypatch.setattr(supervisor, "DAEMON_START_TIMEOUT_SECS", 0)
    with pytest.raises(supervisor.BrowserUnavailable, match="did not start in time"):
        await sup._start_daemon(Path("python"), "http://relay")
    assert stopped == [daemon]


@pytest.mark.asyncio
async def test_the_worker_start_checks_install_ping_and_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stopped: list[Any]
) -> None:
    sup = HarnessSupervisor(tmp_path, base_env={})
    sup._daemon_env = lambda url: {"BU_CDP_URL": url}  # type: ignore[method-assign]
    states = [_installed(tmp_path, python=None)]
    monkeypatch.setattr(supervisor.install, "install_state", lambda home: states[0])
    with pytest.raises(supervisor.HarnessNotInstalled):
        await sup._start_worker(_handle())

    states[0] = _installed(tmp_path, python=Path("/venv/bin/python"))
    spawned: list[tuple[list[str], dict[str, Any]]] = []
    procs: list[_Proc] = []
    pongs: list[dict[str, Any]] = [{"id": None, "ok": False, "error": "import failed: x"}]

    async def _spawn(argv: list[str], **kw: Any) -> Any:
        spawned.append((argv, kw))
        procs.append(_Proc())
        return procs[-1]

    async def _request(proc: Any, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        assert body == {"id": 0, "ping": True}
        return pongs.pop(0) if pongs else {"id": 0, "ok": True, "result": {"pong": True}}

    sup._spawn = _spawn  # type: ignore[method-assign]
    monkeypatch.setattr(supervisor, "_worker_request", _request)
    with pytest.raises(supervisor.BrowserUnavailable, match="import failed: x"):
        await sup._start_worker(_handle())
    assert stopped == [procs[0]]
    proc = await sup._start_worker(_handle())
    assert proc is procs[1] and stopped == [procs[0]]
    argv, kw = spawned[-1]
    assert argv[:2] == [str(Path("/venv/bin/python")), "-I"] and argv[2].endswith(
        "helper_worker.py"
    )
    assert kw["pipes"] is True and kw["env"] == {"BU_CDP_URL": "http://127.0.0.1:1/s"}


# ── the one spawn ──


@pytest.mark.asyncio
async def test_spawn_wires_pipes_fds_and_a_process_group_per_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def _exec(*argv: str, **kw: Any) -> Any:
        calls.append((argv, kw))
        return _Proc()

    monkeypatch.setattr(supervisor.asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(supervisor.platform_compat, "IS_WINDOWS", False)
    sup = HarnessSupervisor(tmp_path)
    await sup._spawn(["py", "-I", "w.py"], env={"A": "1"}, pipes=True)
    argv, kw = calls[-1]
    assert argv == ("py", "-I", "w.py") and kw["env"] == {"A": "1"}
    assert kw["stdin"] == subprocess.PIPE and kw["stdout"] == subprocess.PIPE
    assert kw["stderr"] == subprocess.DEVNULL and kw["limit"] == supervisor.WORKER_LINE_LIMIT
    assert kw["start_new_session"] is True and "pass_fds" not in kw

    await sup._spawn(["sh"], env={}, pipes=False, pass_fds=(7, 8))
    _argv, kw = calls[-1]
    assert kw["pass_fds"] == (7, 8) and kw["stdin"] == subprocess.DEVNULL
    assert kw["stdout"] == subprocess.DEVNULL and "limit" not in kw

    monkeypatch.setattr(supervisor.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        supervisor.platform_compat, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False
    )
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    await sup._spawn(["chrome.exe"], env={}, pipes=False)
    _argv, kw = calls[-1]
    assert kw["creationflags"] == 0x200 | 0x08000000
    assert "start_new_session" not in kw


# ── the daemon's IPC, client side ──


def _point_runtime_at(sup: HarnessSupervisor, path: Path) -> None:
    sup.runtime_dir = lambda: path  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_ipc_needs_a_runtime_dir(tmp_path: Path) -> None:
    sup = HarnessSupervisor(tmp_path)

    def _refused() -> Path:
        raise supervisor.BrowserUnavailable("too deep")

    sup.runtime_dir = _refused  # type: ignore[method-assign]
    assert await sup._ipc_request({"meta": "ping"}) is None
    assert await sup._ipc_ping() is False


@POSIX_ONLY
@pytest.mark.asyncio
async def test_ipc_over_the_unix_socket_reads_one_reply_line(short_dir: Path) -> None:
    sup = HarnessSupervisor(short_dir)
    _point_runtime_at(sup, short_dir)
    replies = [b'{"pong": true}\n', b"not json\n", b""]
    received: list[dict[str, Any]] = []

    async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(json.loads(await reader.readline()))
        writer.write(replies.pop(0))
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(_serve, path=str(short_dir / supervisor._SOCK_FILE))
    try:
        assert await sup._ipc_ping() is True
        assert await sup._ipc_request({"meta": "ping"}) is None, "a garbled reply is no reply"
        assert await sup._ipc_request({"meta": "shutdown"}) is None, "a closed pipe is no reply"
    finally:
        server.close()
        await server.wait_closed()
    assert received == [{"meta": "ping"}, {"meta": "ping"}, {"meta": "shutdown"}]


@pytest.mark.asyncio
async def test_ipc_on_windows_uses_the_port_file_and_sends_its_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor.platform_compat, "IS_WINDOWS", True)
    sup = HarnessSupervisor(tmp_path)
    runtime = sup.runtime_dir()
    runtime.mkdir(parents=True)
    # No port file yet: the daemon is not running.
    assert await sup._ipc_request({"meta": "ping"}) is None
    received: list[dict[str, Any]] = []

    async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(json.loads(await reader.readline()))
        writer.write(b'{"pong": true}\n')
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        (runtime / supervisor._PORT_FILE).write_text(
            json.dumps({"port": port, "token": "t0k"}), encoding="utf-8"
        )
        assert await sup._ipc_ping() is True
        (runtime / supervisor._PORT_FILE).write_text(json.dumps({"port": port}), encoding="utf-8")
        assert await sup._ipc_ping() is False, "a port file with no token is refused"
    finally:
        server.close()
        await server.wait_closed()
    assert received == [{"meta": "ping", "token": "t0k"}]


@pytest.mark.asyncio
async def test_a_stray_daemon_is_shut_down_and_its_runtime_files_removed(
    tmp_path: Path,
) -> None:
    sup = HarnessSupervisor(tmp_path)
    runtime = tmp_path / "run"
    runtime.mkdir()
    _point_runtime_at(sup, runtime)
    (runtime / supervisor._PORT_FILE).write_text("{}", encoding="utf-8")
    # A directory where the socket should be: unlink fails, and that is tolerated.
    (runtime / supervisor._SOCK_FILE).mkdir()
    pings = [True, True, False]
    shutdowns: list[int] = []

    async def _ping() -> bool:
        return pings.pop(0)

    async def _shutdown() -> None:
        shutdowns.append(1)

    sup._ipc_ping = _ping  # type: ignore[method-assign]
    sup._ipc_shutdown = _shutdown  # type: ignore[method-assign]
    await sup._shutdown_stray_daemon()
    assert shutdowns == [1] and pings == []
    assert not (runtime / supervisor._PORT_FILE).exists()


@pytest.mark.asyncio
async def test_no_stray_check_without_a_runtime_dir(tmp_path: Path) -> None:
    sup = HarnessSupervisor(tmp_path)

    def _refused() -> Path:
        raise supervisor.BrowserUnavailable("too deep")

    async def _ping() -> bool:
        raise AssertionError("no runtime dir, nothing to ping")

    sup.runtime_dir = _refused  # type: ignore[method-assign]
    sup._ipc_ping = _ping  # type: ignore[method-assign]
    await sup._shutdown_stray_daemon()


# ── audit row ──


def test_the_audit_row_names_domains_up_to_its_cap_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from kiro_crew import sel as sel_module

    rows: list[dict[str, Any]] = []

    class _Sel:
        def log_api_access(self, **row: Any) -> None:
            rows.append(row)

    monkeypatch.setattr(sel_module, "sel", lambda: _Sel())
    supervisor._audit_injection(credentials.CredentialStatus(), "start")
    assert rows == [], "nothing applied, nothing audited"
    domains = tuple(f"d{i}.test" for i in range(24)) + ("evil\x1b.test",)
    status = credentials.CredentialStatus(
        provider="Corp", applied=30, applied_domains=("bad\nhost.test",) + domains
    )
    supervisor._audit_injection(status, "source_changed")
    (row,) = rows
    assert row["operation"] == "browser_harness.credentials_injected"
    assert "provider=Corp reason=source_changed cookies=30" in row["resources"]
    assert "badhost.test" in row["resources"] and "\n" not in row["resources"]
    assert row["resources"].endswith(",+6 more")

    def _broken() -> Any:
        raise RuntimeError("SEL disk full")

    monkeypatch.setattr(sel_module, "sel", _broken)
    with caplog.at_level(logging.WARNING, logger=supervisor.__name__):
        supervisor._audit_injection(status, "start")
    assert "SEL write failed" in caplog.text


# ── the default config reader ──


@dataclasses.dataclass
class _BrowserSection:
    choice: str = "headless"


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        (None, {}),
        (_BrowserSection(), {"choice": "headless"}),
        ({"choice": "user_chrome"}, {"choice": "user_chrome"}),
        ("not a section", {}),
    ],
)
def test_the_default_config_reader_tolerates_every_section_shape(
    monkeypatch: pytest.MonkeyPatch, section: Any, expected: dict[str, Any]
) -> None:
    from kiro_crew import config

    class _Loaded:
        browser = section

    monkeypatch.setattr(config.KiroCrewConfig, "load", lambda: _Loaded())
    assert supervisor._default_config_reader() == expected


# ── module helpers ──


def test_fd_at_or_above_keeps_an_fd_already_above_the_floor_and_makes_it_inheritable() -> None:
    read_fd, write_fd = os.pipe()
    try:
        assert not os.get_inheritable(read_fd)
        assert supervisor._fd_at_or_above(read_fd, read_fd) == read_fd
        assert os.get_inheritable(read_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.asyncio
async def test_stop_process_tolerates_a_dead_pipe_and_a_child_that_will_not_die(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaped: list[Any] = []

    async def _reap(proc: Any, *, grace: float) -> None:
        reaped.append(proc)
        raise ProcessLookupError("already reaped")

    class _BadPipe:
        def close(self) -> None:
            raise BrokenPipeError("gone")

    monkeypatch.setattr(supervisor.platform_compat, "terminate_and_reap", _reap)
    child = _Proc()
    child.stdin = _BadPipe()
    await supervisor._stop_process(child)
    assert reaped == [child]
    await supervisor._stop_process(_Proc(returncode=0))
    await supervisor._stop_process(None)
    assert reaped == [child], "a finished child and no child are not signalled"


class _Stdin:
    def __init__(self, fail: BaseException | None = None) -> None:
        self.written: list[bytes] = []
        self._fail = fail

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        if self._fail is not None:
            raise self._fail


class _Stdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _PipedProc(_Proc):
    def __init__(self, lines: list[bytes], *, fail: BaseException | None = None) -> None:
        super().__init__()
        self.stdin = _Stdin(fail)
        self.stdout = _Stdout(lines)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lines", "error"),
    [
        ([], "helper worker exited"),
        ([b"{garbage\n"], "helper worker answered garbage"),
        ([b"[1, 2]\n"], "bad reply"),
    ],
)
async def test_a_worker_reply_that_cannot_be_trusted_is_fatal(
    lines: list[bytes], error: str
) -> None:
    proc = _PipedProc(lines)
    reply = await supervisor._worker_request(proc, {"id": 4, "helper": "page_info"}, 60.0)  # type: ignore[arg-type]
    assert reply == {"ok": False, "error": error, "fatal": True}
    assert json.loads(proc.stdin.written[0]) == {"id": 4, "helper": "page_info"}


@pytest.mark.asyncio
async def test_a_worker_whose_pipe_broke_or_whose_budget_is_spent_is_fatal() -> None:
    broken = _PipedProc([], fail=BrokenPipeError("EPIPE"))
    reply = await supervisor._worker_request(broken, {"id": 1}, 60.0)  # type: ignore[arg-type]
    assert reply["fatal"] is True and reply["error"].startswith("helper worker is gone:")
    spent = _PipedProc([b'{"id": 1, "ok": true}\n'])
    reply = await supervisor._worker_request(spent, {"id": 1}, 0)  # type: ignore[arg-type]
    assert reply == {"ok": False, "error": "helper timed out after 0s", "fatal": True}


@pytest.mark.asyncio
async def test_a_stale_reply_is_skipped_and_a_null_id_answers_the_request() -> None:
    proc = _PipedProc([b'{"id": 7, "ok": true, "result": "OLD"}\n', b'{"id": 8, "ok": true}\n'])
    assert await supervisor._worker_request(proc, {"id": 8}, 60.0) == {  # type: ignore[arg-type]
        "id": 8,
        "ok": True,
    }
    proc = _PipedProc([b'{"id": null, "ok": false, "error": "invalid JSON"}\n'])
    reply = await supervisor._worker_request(proc, {"id": 9}, 60.0)  # type: ignore[arg-type]
    assert reply["id"] is None and reply["error"] == "invalid JSON"


@pytest.mark.asyncio
async def test_a_failed_background_start_is_logged_and_a_cancelled_one_is_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _fails() -> Any:
        raise supervisor.BrowserUnavailable("no browser")

    failed = asyncio.ensure_future(_fails())
    with pytest.raises(supervisor.BrowserUnavailable):
        await failed
    cancelled = asyncio.ensure_future(asyncio.Event().wait())
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    with caplog.at_level(logging.INFO, logger=supervisor.__name__):
        supervisor._log_ensure_outcome(failed)  # type: ignore[arg-type]
        supervisor._log_ensure_outcome(cancelled)  # type: ignore[arg-type]
    assert caplog.text.count("browser harness not connected") == 1
    assert "no browser" in caplog.text


@pytest.mark.skipif(sys.platform == "win32", reason="the pipe launcher's fd move is POSIX-only")
def test_fd_at_or_above_moves_a_low_fd_up_even_with_free_slots_below_the_floor() -> None:
    """Two free fds below the floor made the old dup loop bounce between them forever."""
    import os

    r, w = os.pipe()
    low, spare = r, w
    floor = max(r, w) + 40
    os.close(spare)  # a second free slot below the floor, next to *low*
    moved = supervisor._fd_at_or_above(low, floor)
    assert moved >= floor
    assert os.get_inheritable(moved)
    with pytest.raises(OSError):
        os.fstat(low)
    os.close(moved)


@pytest.mark.asyncio
async def test_no_start_launches_kiros_chromium_while_a_redownload_replaces_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    monkeypatch.setattr(
        supervisor.install, "install_state", lambda home: _installed(home, python=Path("py"))
    )
    monkeypatch.setattr(sup, "runtime_dir", lambda: tmp_path / "run")
    monkeypatch.setattr(sup, "_choose", lambda state: _choice("kiro_chromium"))

    async def _must_not_launch(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("Kiro's Chromium launched during its own replacement")

    monkeypatch.setattr(sup, "_launch_chromium", _must_not_launch)
    await sup.stop_for_chromium_replace()
    with pytest.raises(supervisor.BrowserUnavailable, match="being redownloaded"):
        await sup._start()
    assert sup.status()["detail"] == supervisor.CHROMIUM_REPLACING_DETAIL


def test_runs_kiro_chromium_reads_the_live_handle_not_the_configured_choice(
    tmp_path: Path,
) -> None:
    sup = HarnessSupervisor(tmp_path, config_reader=lambda: {})
    assert sup.runs_kiro_chromium is False, "nothing up, nothing starting"
    sup._handle = SimpleNamespace(choice=_choice("user_chrome"))  # type: ignore[assignment]
    assert sup.runs_kiro_chromium is False
    sup._handle = SimpleNamespace(choice=_choice("kiro_chromium"))  # type: ignore[assignment]
    assert sup.runs_kiro_chromium is True
    sup._handle = None
