"""Integration-layer fixtures: the real gateway, booted in THIS process.

This directory is the middle layer of the test pyramid. A unit test in
``test/`` builds a bare ``web.Application()`` with a handful of routes and
mocks the rest; the E2E suites (``test/test_e2e_smoke.py``, ``test/e2e/``,
``test/test_playwright_e2e.py``) spawn ``kirocrew gateway`` as a subprocess
behind ``KIROCREW_E2E=1``. Neither answers "do the real modules work when
wired together?" -- that is what a test here answers:

* the real ``GatewayOrchestrator`` boot sequence (``run()``), not a hand-copied
  subset of it, so a new boot step is covered the day it lands;
* a real ``KIROCREW_HOME`` under ``tmp_path`` -- real session store, real
  config, real memory bindings, real agent-spec directory;
* the ONLY fake is the model: ``kiro_crew.testing.fake_acp_backend`` stands in
  for ``kiro-cli`` (tests MUST NOT spawn the real one);
* the HTTP client talks to the port the gateway actually bound, on loopback,
  from the same interpreter and event loop -- so an event-loop stall in one
  request is observable from another, and a "restart" is a second boot on the
  SAME home.

Why boot through ``run()`` and not through ``_init_dashboard()`` alone: the
bugs this layer exists for live in the seams BETWEEN boot steps -- memory
preparation vs workflow init, spec scanning vs tool policy, admission vs the
resource controller. A fixture that re-implements the sequence drifts from
the real one silently.

The one thing ``run()`` does that a test cannot survive is its last line:
``_shutdown_and_exit`` ends in ``os._exit``. Everything before that line is the
REAL shutdown sequence -- the run-marker settle and clear, ``_shutdown()``, the
orphaned-session cleanup, the crew-log and event-log drains, the log-queue
drain -- and a harness that re-implemented it drifted from it one leftover
at a time. So the boot helper does not re-implement it: teardown sets
``shutdown_event`` exactly as SIGTERM would, lets ``run()`` walk its own exit
path, and intercepts only ``os._exit``, which raises :class:`HarnessExit`
carrying the exit code instead of ending the interpreter. The interception is
held while a boot is live (a ``run()`` that exits on its own mid-test must
raise, not end pytest) and pinned by
``test_boot_smoke.py::test_shutdown_and_exit_ends_in_os_exit``.

The other process-ending path a boot arms is the loop-stall watchdog's
``faulthandler.dump_traceback_later(exit=True)`` timer, which
``start_dashboard`` arms because pytest enables ``faulthandler`` in every
worker. :func:`hard_exit_timer_disabled` is held for the same window and turns
the hard timer's arm into a no-op (the soft stack-dump half stays), so a
25-second loop stall is a failing test, not a dead xdist worker.

Shape: an ``async with`` helper, not an async fixture
-----------------------------------------------------
By this repo's convention (``testing-conventions.md``, "Async tests") the boot
is an ``async with booted_gateway(home)`` block inside the test rather than an
``@pytest_asyncio.fixture``: the teardown has to AWAIT the shutdown on the
test's own loop, and the pinned pytest-asyncio does not run async fixture
teardown there. ``gateway_boot`` is the sync fixture that binds the helper to
an isolated home::

    @pytest.mark.asyncio
    async def test_x(gateway_boot):
        async with gateway_boot() as gw:
            body = await gw.get_json("/api/health", auth=False)

Opt-in
------
The layer runs only with ``KIROCREW_INTEGRATION=1`` (the ``integration`` job
in ``ci.yml`` sets it). A bare ``pytest`` collects these files and skips them:
a boot per test is too slow for the per-commit unit shards, and the Windows
shards must not pay for a POSIX signal-handler dance they do not need.

This directory is a package (``__init__.py``) so this file imports as
``integration.conftest``. The unit files import ``test/conftest.py`` by the
bare name ``conftest``; a second top-level ``conftest`` would shadow it.

The boot reaches no real channel
--------------------------------
``KiroCrewConfig.load_credentials`` merges every ``CREDENTIAL_KEYS`` name from
``os.environ``, and the orchestrator opens Slack/Discord/... transports for
any it finds. A developer with ``SLACK_APP_TOKEN`` exported would otherwise
have a TEST connect their real workspace. ``integration_home`` deletes every
recognised credential variable for the test's duration, so the boot always
sees the no-channel configuration.

One process, many homes: what a boot leaves behind, and who puts it back
-------------------------------------------------------------------------
The whole layer runs in one interpreter, and every boot is a fresh
``KIROCREW_HOME``. A production gateway is one process for one home, so
startup pins home-derived state in module globals, installs process-wide
hooks and raises process limits, and never expects any of it to change or to
be undone. Left alone, the second boot in a worker inherits the first home's
copies and the last test's residue. Four mechanisms cover it. The lists ARE
the contract, and ``test_a_second_boot_touches_only_known_module_globals`` is
the witness that keeps them honest: it diffs every loaded ``kiro_crew`` module's
globals across a second boot, so a home-derived global startup grows that is
on no list goes red there, naming itself, rather than surfacing as a
cross-home flake three tests later.

1. **Reset** by ``_reset_home_bound_globals``, before every boot and at the
   end of every teardown (normal, ``restart()``, and the exceptional exit):

   * ``dashboard.token_secret._SECRET`` -- cached token signing key, read
     from ``<home>/token_signing.key``; a stale one signs tokens the new
     home's gateway still accepts.
   * ``dashboard.token_auth._revoked_store_singleton`` -- revoked-nonce store
     pinned to the first ``config_dir()``; ``_state`` and ``_app_perms_cache``
     -- nonce and permission state for the previous gateway's tokens.
   * ``dashboard.revocation_gen._gen`` -- memoised revocation generation,
     loaded from the home's counter file.
   * ``crash_guard._CRASH_LOG`` -- crash-record path pinned to the home by
     ``install_loop_handler``.
   * ``safety_override`` singleton and the pushed yolo-policy verdict -- an
     override a test activates (``POST /api/chat/mode`` with ``yolo``) must
     not survive into the replacement gateway; queued breadcrumb writes are
     drained first so none lands after the home is gone.
   * ``config.live`` snapshot and subscriptions -- the process-global config
     watcher every point-of-use reader prefers over its own config.
   * ``autonudge._INSTANCE`` -- the process-global service reference the
     gateway publishes; stopped and cleared if ``_shutdown()`` left it.
   * ``platform.context`` active :class:`PlatformContext` and
     ``platform.bootstrap`` boot state -- config and governance the first
     boot composed; a ``restart()`` must compose its own.
   * ``embeddings`` shared embedder and model-download manager -- both pinned
     to the home's model path, and both already ship the "KIROCREW_HOME
     changes" reset this calls.
   * ``crew_log.emit`` caches and its shutdown-drain flag -- the real exit
     path drains the session log and marks the writer as draining for
     shutdown, which would keep the next boot's writer from starting.

2. **Snapshot and restore** around the boot by ``booted_gateway``, on every
   exit: the SIGINT/SIGTERM handlers ``run()`` installs; the event loop's
   exception handler ``crash_guard.install_loop_handler`` replaces; the
   ``RLIMIT_NOFILE`` soft limit ``raise_nofile_soft_limit`` raises; and the
   ``os.environ`` keys startup writes (``KIROCREW_BOUND_PORT`` /
   ``KIROCREW_BOUND_HOST``).

3. **Waited for** by ``_teardown_boot``: the memory-preparation worker is a
   THREAD behind a process-wide fence (``kiro_crew.memory_startup``) that
   ``_shutdown()`` cannot join, so teardown polls until the fence drops.

4. **Reaped** by ``_teardown_boot``: every asyncio task the boot created that
   ``_shutdown()`` did not end. Production never needs to -- ``os._exit``
   follows -- so the MCP probe, the terminal-title poller, the browser-snapshot
   pruner, the follow-up sweep and their kin keep running against a home the
   test has discarded. Teardown snapshots ``asyncio.all_tasks()`` before the
   boot, cancels every survivor the boot added, and fails the test if one
   refuses to end within ``TASK_REAP_SECS``.

A boot that times out, is cancelled, or whose ``run()`` dies reaps its own
task and runs the same teardown before the error propagates.

What this boot does NOT run
---------------------------
The only FAKE is the model. But the boot is ``GatewayOrchestrator.run()``, not
``kirocrew gateway``, and it runs with ``test_mode=True`` and ``no_crons=True``,
so these production steps are skipped here and belong to the E2E layer:

* everything ``run_gateway()`` does before it constructs the orchestrator --
  platform boot, ``_apply_slice_limits``, the agents-dir janitor, the agent
  scratch sweep, the kiro-cli log cap and the telemetry beacon;
* ``test_mode``: the kiro-cli readiness probe (``assume_kiro_ready``) and the
  policy-distribution refresher with its ceiling projection, both outbound;
* ``no_crons``: cron arming and reconciliation (jobs load, none fires).

The flags are fixed: a test that needs one of those steps is an E2E test
today, and the helper grows the flag when the first such in-tree test does.

Route coverage
--------------
Every request made through :class:`IntegrationGateway` is attributed to the
aiohttp ROUTE it resolved to (``/api/sessions/abc`` counts toward
``GET /api/sessions/{key}``). With ``KIROCREW_INTEGRATION_HITS_DIR`` set, each
pytest process writes its hit set plus the full registered-route list at
session end; ``scripts/check_integration_route_coverage.py`` unions them and
reports the share of registered routes this suite exercised. That share is
the layer's primary coverage metric (line coverage is secondary here: a route
served end-to-end proves the wiring, a line reached by a mock does not).

The dump directory must be an ABSOLUTE path OUTSIDE the repository checkout,
and the run refuses at configure time otherwise. pytest's CWD is the repo
root, so a relative value would land the dumps in the checkout -- the exact
residue the rootdir conftest exists to prevent -- and a value inside the
checkout, ignored or not, is a file the run leaves behind. CI passes
``${{ runner.temp }}``; locally use a fresh ``mktemp -d``.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import json
import os
import re
import signal
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator, NoReturn

import pytest
from aiohttp import ClientSession, ClientTimeout

from kiro_crew import (
    autonudge,
    crash_guard,
    embeddings,
    memory_startup,
    safety_override,
    shutdown_event,
)
from kiro_crew.config import live as config_live
from kiro_crew.config.loader import CREDENTIAL_KEYS
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.dashboard import loop_watchdog, revocation_gen, token_auth, token_secret
from kiro_crew.platform import bootstrap as platform_bootstrap
from kiro_crew.platform import context as platform_context
from kiro_crew.testing import fake_acp_backend

try:  # POSIX only; the layer is Linux-only in CI, but keep the import honest.
    import resource as _resource
except ImportError:  # pragma: no cover - Windows
    _resource = None  # type: ignore[assignment]

#: Opt-in switch for the whole directory (see module docstring, "Opt-in").
INTEGRATION_ENV = "KIROCREW_INTEGRATION"

#: How long the in-process boot may take before the helper gives up. The
#: subprocess harness budgets 5-15s for the same boot plus interpreter start;
#: in-process there is no interpreter start, but memory preparation and the
#: fake-ACP probe are real work. A test that needs a different deadline passes
#: ``boot_secs`` to :func:`booted_gateway` itself.
DEFAULT_BOOT_TIMEOUT_SECS = 60.0

#: How long teardown waits for the memory-preparation thread to drop its fence
#: after ``_shutdown()``. The worker has no stop check inside its longest steps
#: (store repair, index rebuild), so this is the bound on one of those.
MEMORY_FENCE_DRAIN_SECS = 30.0

#: How long the safety-override breadcrumb worker may take to drain its queue
#: before a reset; a write still queued would land after the home is gone.
BREADCRUMB_DRAIN_SECS = 5.0

#: Env var naming the directory the route-hit / route-registry dumps go to.
HITS_DIR_ENV = "KIROCREW_INTEGRATION_HITS_DIR"

#: The checkout this file lives in; a dump directory may not resolve inside it.
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Routes hit by every request made through :class:`IntegrationGateway`, and
#: the routes the real app registered, per process. xdist workers each write
#: their own file; the coverage script unions them.
_HIT_ROUTES: set[tuple[str, str]] = set()
_REGISTERED_ROUTES: set[tuple[str, str]] = set()

#: Type of what ``gateway_boot`` returns: call it for a fresh boot on the
#: fixture's home, ``async with`` the result.
GatewayBoot = Callable[[], "contextlib.AbstractAsyncContextManager[IntegrationGateway]"]

#: aiohttp's dynamic-segment token, ``{name}`` or ``{name:regex}``.
_ROUTE_TOKEN = re.compile(r"(\{[^}]*\})")


#: How long a boot's leftover asyncio task gets to honour cancellation before
#: teardown fails the test that booted it.
TASK_REAP_SECS = 10.0

#: How long ``run()`` gets to walk its own exit path after ``shutdown_event``
#: is set: the product's graceful budget plus its bounded drains, with room.
SHUTDOWN_PATH_SECS = 60.0


class HarnessExit(BaseException):
    """``os._exit`` intercepted at the end of ``_shutdown_and_exit``.

    A ``BaseException`` so no ``except Exception`` on the exit path can swallow
    it. ``code`` is what the gateway would have exited with.
    """

    def __init__(self, code: int) -> None:
        super().__init__(f"gateway exit intercepted (code {code})")
        self.code = code


@contextlib.contextmanager
def hard_exit_timer_disabled() -> "Iterator[None]":
    """Keep the loop-stall watchdog's C-level exit timer off while a boot is live.

    ``start_dashboard`` arms :class:`~kiro_crew.dashboard.loop_watchdog.LoopStallWatchdog`
    whenever ``faulthandler`` is enabled -- which pytest does for every worker --
    and its hard timer is ``faulthandler.dump_traceback_later(exit=True)``: 25s
    without a loop beat and the PROCESS exits, which here is the xdist worker
    and every test it still owed. A stalled loop in this layer is a test
    failure to report, not a gateway to put down. The soft half of the watchdog
    (stack dump without exit) stays armed; only the two module-level arm/cancel
    callables the hard half resolves at call time are replaced, and any timer
    already pending is cancelled on exit.
    """
    real_arm = loop_watchdog._default_arm_later
    real_cancel = loop_watchdog._default_cancel_later

    def _arm_nothing(timeout: float, file: Any = None) -> None:
        return None

    def _cancel_nothing() -> None:
        return None

    loop_watchdog._default_arm_later = _arm_nothing
    loop_watchdog._default_cancel_later = _cancel_nothing
    try:
        yield
    finally:
        loop_watchdog._default_arm_later = real_arm
        loop_watchdog._default_cancel_later = real_cancel
        faulthandler.cancel_dump_traceback_later()


def hard_exit_timer_is_disabled() -> bool:
    """Whether :func:`hard_exit_timer_disabled` is in force."""
    return loop_watchdog._default_arm_later.__name__ == "_arm_nothing"


@contextlib.contextmanager
def intercepted_os_exit() -> "Iterator[None]":
    """Turn ``os._exit`` into :class:`HarnessExit` for the duration.

    Process-wide by necessity (``gateway.py`` calls the ``os`` module's
    attribute), so it is held only while a boot is live. Another ``os._exit``
    inside that window raises too, which in a test process is the better
    outcome: a killed interpreter reports nothing.
    """
    real_exit = os._exit

    def _raise(code: int = 0) -> "NoReturn":
        raise HarnessExit(int(code))

    os._exit = _raise  # type: ignore[assignment]
    try:
        yield
    finally:
        os._exit = real_exit


def resolve_dump_dir(raw: str | None) -> Path | None:
    """The validated dump directory, or ``None`` when dumps are off.

    Refuses a relative path and any path that resolves inside the repository
    checkout (module docstring, "Route coverage"). Raises ``pytest.UsageError``
    so a misconfigured run stops before it boots anything.
    """
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise pytest.UsageError(
            f"{HITS_DIR_ENV} must be an absolute path outside the checkout, got {raw!r}: "
            "pytest's CWD is the repository root, so a relative value writes the route "
            "dumps into the checkout"
        )
    resolved = candidate.resolve()
    if resolved == _REPO_ROOT or _REPO_ROOT in resolved.parents:
        raise pytest.UsageError(
            f"{HITS_DIR_ENV}={raw!r} resolves inside the repository checkout {_REPO_ROOT}; "
            "point it at a temporary directory outside the checkout (CI uses runner.temp)"
        )
    return resolved


def pytest_configure(config: pytest.Config) -> None:
    """Fail a misconfigured dump directory before any gateway boots."""
    resolve_dump_dir(os.environ.get(HITS_DIR_ENV))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip this directory unless the layer was asked for."""
    if os.environ.get(INTEGRATION_ENV):
        return
    here = Path(__file__).resolve().parent
    skip = pytest.mark.skip(reason=f"integration layer; set {INTEGRATION_ENV}=1 to run")
    for item in items:
        if Path(str(item.path)).resolve().is_relative_to(here):
            item.add_marker(skip)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Flush this process's route-hit set and route registry for the script."""
    directory = resolve_dump_dir(os.environ.get(HITS_DIR_ENV))
    if directory is None or not _REGISTERED_ROUTES:
        return
    directory.mkdir(parents=True, exist_ok=True)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    stem = f"{worker}-{os.getpid()}"
    (directory / f"hits-{stem}.json").write_text(
        json.dumps(sorted(list(pair) for pair in _HIT_ROUTES)), encoding="utf-8"
    )
    (directory / f"routes-{stem}.json").write_text(
        json.dumps(sorted(list(pair) for pair in _REGISTERED_ROUTES)), encoding="utf-8"
    )


def _canonical_path(resource: Any) -> str | None:
    info = resource.get_info()
    return info.get("path") or info.get("formatter") or None


def _token_pattern(token: str) -> str:
    """``{name}`` -> one path segment; ``{name:regex}`` -> that regex."""
    inner = token[1:-1]
    return "(" + (inner.split(":", 1)[1] if ":" in inner else "[^/]+") + ")"


def _path_matches(canonical: str, concrete: str) -> bool:
    """Match a concrete request path against an aiohttp resource path.

    Handles the literal, ``{name}`` and ``{name:regex}`` forms. The literal
    spans are regex-escaped so a metachar in a fixed segment (a ``.`` in a
    filename route) cannot over-match; only the tokens become groups. Static
    prefix mounts (``PrefixResource``) carry no ``path``/``formatter`` and are
    not counted as routes: serving a bundled asset is not a contract this
    layer is about.
    """
    if canonical == concrete:
        return True
    if "{" not in canonical:
        return False
    pattern = "".join(
        _token_pattern(piece) if _ROUTE_TOKEN.fullmatch(piece) else re.escape(piece)
        for piece in _ROUTE_TOKEN.split(canonical)
    )
    return re.fullmatch(pattern, concrete) is not None


def _record_registered_routes(app: Any) -> None:
    for resource in app.router.resources():
        canonical = _canonical_path(resource)
        if not canonical:
            continue
        for route in resource:
            if route.method in ("HEAD", "OPTIONS"):
                continue
            _REGISTERED_ROUTES.add((route.method, canonical))


@dataclass
class IntegrationGateway:
    """Handle on one in-process gateway boot.

    ``home`` outlives a ``restart()``; the orchestrator, port and token do not.
    """

    home: Path
    orchestrator: Any
    port: int
    token: str
    _run_task: "asyncio.Task[None]"
    _client: ClientSession
    _boot_secs: float
    _tasks_before: frozenset["asyncio.Task[Any]"]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def state(self) -> Any:
        """The live ``DashboardState`` -- for assertions on in-memory state."""
        return self.orchestrator.dashboard_state

    @property
    def app(self) -> Any:
        """The real ``web.Application`` the orchestrator built."""
        runner = self.orchestrator._dashboard_runner
        return runner.app if runner is not None else None

    def _note_hit(self, method: str, path: str) -> None:
        # "Hit" means REQUESTED, whatever the status came back: the metric
        # measures which contracts the suite exercised, and a 4xx a test
        # proves on purpose is one of them.
        app = self.app
        if app is None:
            return
        for resource in app.router.resources():
            canonical = _canonical_path(resource)
            if not canonical or not _path_matches(canonical, path):
                continue
            for route in resource:
                if route.method in (method, "*"):
                    _HIT_ROUTES.add((route.method, canonical))
                    return

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        auth: bool = True,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> Any:
        """One HTTP request against the live gateway. Returns the response.

        ``auth=True`` (default) sends the boot token as ``?token=``; pass
        ``auth=False`` to prove the 401/403 side of a contract.
        """
        url = f"{self.base_url}{path}"
        params = dict(kwargs.pop("params", {}) or {})
        if auth:
            params["token"] = self.token
        self._note_hit(method.upper(), path.split("?", 1)[0])
        return await self._client.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=headers,
            timeout=ClientTimeout(total=timeout),
            **kwargs,
        )

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, json_body: Any = None, **kw: Any) -> Any:
        return await self.request("POST", path, json_body=json_body, **kw)

    async def put(self, path: str, json_body: Any = None, **kw: Any) -> Any:
        return await self.request("PUT", path, json_body=json_body, **kw)

    async def patch(self, path: str, json_body: Any = None, **kw: Any) -> Any:
        return await self.request("PATCH", path, json_body=json_body, **kw)

    async def delete(self, path: str, **kw: Any) -> Any:
        return await self.request("DELETE", path, **kw)

    async def get_json(self, path: str, *, expect: int = 200, **kw: Any) -> Any:
        resp = await self.get(path, **kw)
        body = await resp.text()
        assert resp.status == expect, f"GET {path} -> {resp.status}: {body[:500]}"
        return json.loads(body) if body else None

    async def post_json(
        self, path: str, json_body: Any = None, *, expect: int = 200, **kw: Any
    ) -> Any:
        resp = await self.post(path, json_body=json_body, **kw)
        body = await resp.text()
        assert resp.status == expect, f"POST {path} -> {resp.status}: {body[:500]}"
        return json.loads(body) if body else None

    async def shutdown(self) -> None:
        """Stop this boot gracefully (no ``os._exit``). ``home`` is kept.

        The client closes whatever the teardown reports: a task that refused
        cancellation is a test failure, not a reason to leak a connector.
        """
        try:
            await _teardown_boot(self.orchestrator, self._run_task, self._tasks_before)
        finally:
            await self._client.close()

    async def restart(self) -> "IntegrationGateway":
        """Stop and boot again on the SAME home -- what ``kirocrew restart`` does.

        The handle is updated in place so the enclosing ``async with`` closes
        the new boot. Everything a real restart loses (in-memory state) is lost
        here too, which is the point: the home-bound globals are reset between
        the two boots exactly as a new process would start without them.
        """
        await self.shutdown()
        fresh = await _boot(self.home, boot_secs=self._boot_secs)
        self.orchestrator = fresh.orchestrator
        self.port = fresh.port
        self.token = fresh.token
        self._run_task = fresh._run_task
        self._client = fresh._client
        self._tasks_before = fresh._tasks_before
        return self


@dataclass
class _ProcessSnapshot:
    """What ``booted_gateway`` puts back on every exit (docstring, item 2)."""

    signals: dict[int, Any]
    environ: dict[str, str]
    loop_exception_handler: Any
    nofile_limit: tuple[int, int] | None

    @classmethod
    def take(cls, loop: asyncio.AbstractEventLoop) -> "_ProcessSnapshot":
        return cls(
            signals={sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)},
            environ=dict(os.environ),
            loop_exception_handler=loop.get_exception_handler(),
            nofile_limit=(
                _resource.getrlimit(_resource.RLIMIT_NOFILE) if _resource is not None else None
            ),
        )

    def restore(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig, handler in self.signals.items():
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)
            with contextlib.suppress(Exception):
                signal.signal(sig, handler)
        loop.set_exception_handler(self.loop_exception_handler)
        if _resource is not None and self.nofile_limit is not None:
            with contextlib.suppress(Exception):
                _resource.setrlimit(_resource.RLIMIT_NOFILE, self.nofile_limit)
        _restore_environ(self.environ)


def _restore_environ(before: dict[str, str]) -> None:
    """Put ``os.environ`` back to exactly ``before`` -- added keys go, changed
    values revert. Startup writes ``KIROCREW_BOUND_PORT`` and friends and has
    no teardown for them; this is that teardown."""
    for key in set(os.environ) - set(before):
        del os.environ[key]
    for key, value in before.items():
        if os.environ.get(key) != value:
            os.environ[key] = value


def _reset_home_bound_globals() -> None:
    """Forget every module global a boot derives from its home (docstring, item 1).

    Mirrors what ``test/conftest.py`` and ``test/test_token_auth.py`` isolate
    per test, gathered in one place because a ``restart()`` needs it BETWEEN
    two boots inside one test, where no fixture boundary runs.
    """
    # Platform context first: dropping it fires the ceiling-invalidation
    # callbacks, and safety_override's re-creates its singleton to answer
    # them -- so it must go before the singleton reset, not after.
    platform_context.reset_context()
    platform_bootstrap._reset_boot_state()
    # Drain before reset: a queued breadcrumb publish that runs after the
    # singleton is gone would write into a home the test already discarded.
    safety_override.flush_breadcrumb_writes(BREADCRUMB_DRAIN_SECS)
    safety_override.reset_singleton()
    safety_override.reset_yolo_policy_state()
    token_secret._SECRET = None
    token_auth._revoked_store_singleton = None
    token_auth._state.clear_all()
    token_auth._app_perms_cache.clear()
    revocation_gen._gen = None
    crash_guard._CRASH_LOG = None
    config_live.reset_for_tests()
    embeddings.reset_shared_embedder()
    embeddings.reset_download_manager()
    crew_log_emit.reset_caches()
    live_nudge = autonudge._INSTANCE
    if live_nudge is not None:
        with contextlib.suppress(Exception):
            live_nudge.stop()
        autonudge._INSTANCE = None


def home_bound_globals_are_clear() -> bool:
    """Whether no home-derived module global is currently populated."""
    return (
        token_secret._SECRET is None
        and token_auth._revoked_store_singleton is None
        and revocation_gen._gen is None
        and not token_auth._app_perms_cache
        and crash_guard._CRASH_LOG is None
        and autonudge._INSTANCE is None
        and platform_context._ACTIVE is None
        and safety_override._singleton is None
        and embeddings._shared_embedder is None
        and embeddings._download_manager is None
    )


def memory_fence_held() -> bool:
    """Whether a memory-preparation owner still holds the process-wide fence."""
    return memory_startup._active is not None


async def _drain_memory_fence() -> None:
    """Wait for the memory-preparation thread to release its fence.

    ``_shutdown()`` fences new work and cancels the awaiter, but the worker
    thread keeps running to the end of its current step and only then clears
    the module-level owner. Polled rather than joined: the orchestrator holds
    no handle on the thread, only on the ``to_thread`` awaiter it cancelled.
    A worker still holding the fence at the deadline fails the teardown by
    name -- it is still writing to a home the test is done with, and the next
    boot on this worker would be refused with "Another gateway is still
    preparing memory" -- rather than being left to surface as that refusal.
    """
    deadline = time.monotonic() + MEMORY_FENCE_DRAIN_SECS
    while memory_fence_held() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    if memory_fence_held():
        raise RuntimeError(
            "the memory-preparation worker still held its fence "
            f"{MEMORY_FENCE_DRAIN_SECS}s after shutdown; it may still be writing "
            "to the discarded home"
        )


def _live_tasks() -> set["asyncio.Task[Any]"]:
    return {t for t in asyncio.all_tasks() if not t.done()}


async def _reap_boot_tasks(tasks_before: frozenset["asyncio.Task[Any]"]) -> None:
    """Cancel every task the boot added and ``_shutdown()`` left running.

    Production relies on ``os._exit`` to end the gateway's background loops, so
    ``_shutdown()`` cancels only what it owns. Here the process lives on, and a
    loop still polling a discarded home is residue by definition (docstring,
    item 4). A survivor that ignores cancellation for ``TASK_REAP_SECS`` fails
    the boot's teardown by name -- silence would only move the failure.
    """
    current = asyncio.current_task()
    leftover = [t for t in _live_tasks() - tasks_before if t is not current]
    if not leftover:
        return
    for task in leftover:
        task.cancel()
    _done, pending = await asyncio.wait(leftover, timeout=TASK_REAP_SECS)
    if pending:
        names = sorted(f"{t.get_name()} ({t.get_coro()!r})" for t in pending)
        raise RuntimeError(
            f"{len(pending)} task(s) the boot started did not end within "
            f"{TASK_REAP_SECS}s of cancellation: {names}"
        )


async def _teardown_boot(
    orchestrator: Any,
    run_task: "asyncio.Task[None]",
    tasks_before: frozenset["asyncio.Task[Any]"],
) -> None:
    """Let ``run()`` shut the gateway down its own way, then clean up the process.

    ``shutdown_event`` is set the way a SIGTERM would set it; ``run()`` then
    runs ``_shutdown_and_exit`` -- the real sequence, not a copy -- until its
    ``os._exit``, which :func:`intercepted_os_exit` (held by ``booted_gateway``
    for the whole boot) turns into :class:`HarnessExit`. A ``run()`` that does
    not reach its exit within ``SHUTDOWN_PATH_SECS`` (a boot wedged before it
    can observe the event), or that already ended on its own without passing
    through ``_shutdown_and_exit``, gets ``_shutdown()`` awaited directly, and
    the error that ended it is what the test sees.

    Every step runs whatever the earlier ones did: the reap and the reset are
    the residue guarantees, and a failure in one must not skip the other. The
    first error is re-raised once everything has run.
    """
    failure: BaseException | None = None
    shutdown_owed = False
    if not run_task.done():
        shutdown_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(run_task), timeout=SHUTDOWN_PATH_SECS)
        except HarnessExit:
            pass
        except asyncio.TimeoutError:
            failure = RuntimeError(
                f"run() did not reach its exit within {SHUTDOWN_PATH_SECS}s of "
                "shutdown_event; cancelled instead"
            )
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception, HarnessExit):
                await run_task
            shutdown_owed = True
        except (asyncio.CancelledError, Exception) as exc:
            failure = exc
    if run_task.done() and not run_task.cancelled():
        exit_error = run_task.exception()
        if exit_error is None or not isinstance(exit_error, HarnessExit):
            # run() ended on its own -- returned, or died on an exception -- so
            # it never reached _shutdown_and_exit and its _shutdown() never ran.
            # The dashboard, the task-store writer thread and its SQLite
            # connection are still up; shut them down here, and keep the error
            # that ended run() as the one the test sees.
            shutdown_owed = True
            if exit_error is not None:
                failure = failure or exit_error
    if shutdown_owed:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(orchestrator._shutdown(), timeout=30)
    runner = getattr(orchestrator, "_dashboard_runner", None)
    if runner is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(runner.cleanup(), timeout=15)
    try:
        await _drain_memory_fence()
    except BaseException as exc:  # noqa: BLE001 -- re-raised below, after the reset
        failure = failure or exc
    try:
        await _reap_boot_tasks(tasks_before)
    except BaseException as exc:  # noqa: BLE001 -- re-raised below, after the reset
        failure = failure or exc
    _reset_home_bound_globals()
    if failure is not None:
        raise failure


async def _boot(home: Path, *, boot_secs: float) -> IntegrationGateway:
    """Boot one gateway on ``home`` and return a handle once it serves HTTP.

    The orchestrator flags are the offline boot (``no_crons=True``,
    ``test_mode=True``; see the module docstring, "What this boot does NOT run").

    Every exceptional exit -- the deadline, a ``run()`` that died, a
    cancellation from outside -- reaps the boot before the error propagates
    (module docstring, "One process, many homes").
    """
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.dashboard.token_auth import MAX_SESSION_TTL_SECS, generate_token
    from kiro_crew.slack.gateway import GatewayOrchestrator

    shutdown_event.clear()
    _reset_home_bound_globals()
    tasks_before = frozenset(_live_tasks())

    cfg = KiroCrewConfig.load()
    orchestrator = GatewayOrchestrator(
        cfg,
        no_crons=True,
        no_open=True,
        port_override="auto",
        approval_mode="reads",
        test_mode=True,
    )
    run_task = asyncio.create_task(orchestrator.run(), name="integration-gateway-run")

    deadline = time.monotonic() + boot_secs
    client = ClientSession()
    try:
        while True:
            if run_task.done():
                exc = run_task.exception() if not run_task.cancelled() else None
                raise RuntimeError(f"gateway run() ended during boot: {exc!r}")
            port = getattr(orchestrator, "_dashboard_port", 0)
            if port and orchestrator.dashboard_state is not None:
                try:
                    async with client.get(
                        f"http://127.0.0.1:{port}/api/health", timeout=ClientTimeout(total=2)
                    ) as resp:
                        if resp.status < 500:
                            break
                except Exception:
                    pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"gateway did not serve HTTP within {boot_secs}s")
            await asyncio.sleep(0.05)
    except BaseException:
        await client.close()
        await _teardown_boot(orchestrator, run_task, tasks_before)
        raise

    handle = IntegrationGateway(
        home=home,
        orchestrator=orchestrator,
        port=int(orchestrator._dashboard_port),
        token=generate_token(
            orchestrator._owner_id or "local-startup", ttl_seconds=MAX_SESSION_TTL_SECS
        ),
        _run_task=run_task,
        _client=client,
        _boot_secs=boot_secs,
        _tasks_before=tasks_before,
    )
    if handle.app is not None:
        _record_registered_routes(handle.app)
    return handle


@asynccontextmanager
async def booted_gateway(
    home: Path, *, boot_secs: float = DEFAULT_BOOT_TIMEOUT_SECS
) -> AsyncIterator[IntegrationGateway]:
    """The real gateway, booted in-process on ``home``, for one ``async with``.

    Every boot is a fresh boot, on purpose: the bugs this layer chases are
    state bugs, and a shared boot would let one test's residue explain
    another's failure. Boot cost (~2s here) is the price of that isolation.
    """
    loop = asyncio.get_running_loop()
    # Snapshot BEFORE the boot installs the gateway's own handlers, raises the
    # fd limit and writes its bound-address variables; restore the snapshot
    # after -- a restart() in between must not make the gateway's own state
    # the thing we "restore" to.
    snapshot = _ProcessSnapshot.take(loop)

    def _put_back() -> None:
        snapshot.restore(loop)
        shutdown_event.clear()

    # Both held for the WHOLE boot, not just the teardown: a run() that exits on
    # its own during the test body (a fatal config it refuses to serve, an early
    # owner stop) reaches os._exit then, and must raise instead of ending pytest;
    # and the watchdog's hard timer is armed by the boot and re-armed by every
    # heartbeat, so it must find no-ops from the first arm to the last.
    with intercepted_os_exit(), hard_exit_timer_disabled():
        try:
            handle = await _boot(home, boot_secs=boot_secs)
        except BaseException:
            _put_back()
            raise
        try:
            yield handle
        finally:
            # Nested: a teardown that raises (a task refusing cancellation, a
            # fence that never drops) is a test failure, and the process
            # snapshot is put back regardless -- otherwise the next boot's
            # snapshot would record this gateway's handlers as the baseline and
            # the drift would be permanent for the worker.
            try:
                await handle.shutdown()
            finally:
                _put_back()


@pytest.fixture
def integration_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh, isolated ``KIROCREW_HOME`` with the fake model wired in.

    Mirrors ``kiro_crew.testing.harness.spawn_feature_gateway``'s environment
    so a test that passes here and fails in E2E differs only in the process
    boundary, never in configuration.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    # Isolate the agent-spec home too: boot rewrites managed MCP specs under
    # ``kiro_agents_dir()``, which must never be the operator's ``~/.kiro/agents``.
    monkeypatch.setenv("KIRO_HOME", str(home / "kiro"))
    monkeypatch.setenv("KIROCREW_KIRO_BIN", str(fake_acp_backend.__file__))
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    # Strict on-loop persistence, set HERE rather than in the CI job's env: the
    # rootdir conftest deletes this name before every test body, so a job-level
    # value never reaches the boot. Set per test it makes an on-loop store write
    # raise instead of warn, the same discipline the other gateway-booting jobs
    # enforce.
    monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_PERSIST", "1")
    # No channel credential may reach the boot (module docstring, "The boot
    # reaches no real channel"): the orchestrator would open the transport.
    for key in CREDENTIAL_KEYS:
        monkeypatch.delenv(key, raising=False)
    return home


@pytest.fixture
def gateway_boot(integration_home: Path) -> GatewayBoot:
    """``async with gateway_boot() as gw:`` -- a fresh boot on this test's home."""
    return lambda: booted_gateway(integration_home)
