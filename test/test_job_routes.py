"""Coverage tests for kiro_crew.apps.job_routes — the shared ``_jobs/*`` HTTP
surface mounted once for every app.

Everything runs in-process against ``aiohttp``'s ``TestServer`` with
``KIROCREW_HOME`` pointed at ``tmp_path`` and the guards monkeypatched — no
network, no subprocess. The routes resolve the app from the URL and look its
:class:`JobSDK` up in a process-global registry, so a fixture forgets the SDK
on teardown to keep tests from leaking into the rest of the suite.

The two guards the handler consults are patched on their ORIGINATING modules,
because the module imports them lazily inside the request:
``is_owner_dashboard_request`` / ``_owner_denial_response`` on the dashboard
handler modules, and ``_is_restricted_session`` on ``_shared`` — patching a
copy on ``job_routes`` would never be reached. ``is_app_enabled`` is imported
at ``job_routes`` module scope, so it is patched there.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.apps.job_routes as jr
import kiro_crew.apps.job_sdk as js
import kiro_crew.dashboard.handlers._shared as shared
import kiro_crew.dashboard.handlers.source_providers as sp
from kiro_crew.apps.job_sdk import JobError, JobSDK, forget_sdk, get_sdk, register_sdk
from kiro_crew.testing.wait import async_wait_until, wait_until

APP = "jobs-cov-app"

# A terminal-state poll deadline: runs are real daemon threads, so we wait for
# the worker to reach a terminal state rather than sleeping a fixed amount.
_DEADLINE = 5.0


@pytest.fixture
def sdk(tmp_path: Path) -> Any:
    """A registered JobSDK for APP whose store lives under tmp_path.

    Registered in the process-global registry so the routes resolve it, and
    forgotten on teardown so it never leaks into another test.

    Teardown also stops the app's workers: forgetting the SDK makes a run
    unreachable but does not end it, and a leftover worker keeps the SDK's lock
    and writes records during later tests. ``remove_all_async`` owns that
    stop-and-join contract, so teardown calls it instead of copying it.
    """
    instance = JobSDK(APP, tmp_path / "app-data")
    register_sdk(instance)
    try:
        yield instance
    finally:
        # OSError only: a broad except would also hide a NameError and turn
        # this teardown into a silent no-op.
        try:
            asyncio.run(instance.remove_all_async())
        except OSError:
            pass
        forget_sdk(APP)


def _setup_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool = True,
    owner: bool = True,
    granted: bool = True,
) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(jr, "is_app_enabled", lambda name: enabled)

    # The guard re-reads the manifest rather than trusting the process registry,
    # so authorization survives a grant revoked after the SDK was published.
    # `granted=False` is that revoked case.
    class _Perms:
        jobs = granted

    class _Manifest:
        permissions = _Perms()

    monkeypatch.setattr(jr, "get_app_manifest", lambda name: _Manifest())
    monkeypatch.setattr(sp, "is_owner_dashboard_request", lambda r: owner)
    monkeypatch.setattr(
        shared,
        "_owner_denial_response",
        lambda r, m="dashboard owner required", c="dashboard_owner_required": web.json_response(
            {"error": m, "code": c}, status=403
        ),
    )


def _make_app(*, state: Any = None) -> web.Application:
    app = web.Application()
    if state is not None:
        app["state"] = state
    jr.register_job_routes(app)
    return app


def _base(app: str = APP) -> str:
    return f"/api/apps/{app}/_jobs"


async def _wait_terminal(sdk: JobSDK, run_id: str, deadline: float = _DEADLINE) -> js.JobRun:
    """Poll until the run reaches a terminal state or the deadline passes.

    Each ``sdk.get`` runs off the event loop (``off_loop=True``), as in
    ``job_routes.py``: on the loop ``read_bytes_with_retry`` re-raises the
    Windows sharing-violation ``PermissionError`` instead of retrying.
    """

    def _terminal() -> js.JobRun | None:
        run = sdk.get(run_id)
        return run if run is not None and run.is_terminal else None

    def _state() -> str:
        run = sdk.get(run_id)
        return f"run {run_id} did not reach a terminal state: {run and run.status}"

    return await async_wait_until(_terminal, timeout=deadline, off_loop=True, describe=_state)


async def _wait_status(
    sdk: JobSDK, run_id: str, status: str, deadline: float = _DEADLINE
) -> js.JobRun:
    def _reached() -> js.JobRun | None:
        run = sdk.get(run_id)
        return run if run is not None and run.status == status else None

    def _state() -> str:
        run = sdk.get(run_id)
        return f"run {run_id} never reached {status!r}: {run and run.status}"

    return await async_wait_until(_reached, timeout=deadline, off_loop=True, describe=_state)


# 1-3: the guard chain (enabled -> owner -> SDK present)


@pytest.mark.asyncio
async def test_disabled_app_is_403_app_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_guards(tmp_path, monkeypatch, enabled=False)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"{_base()}/active")
        assert resp.status == 403
        assert (await resp.json())["code"] == "app_disabled"


@pytest.mark.asyncio
async def test_non_owner_is_owner_denial_with_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_guards(tmp_path, monkeypatch, owner=False)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"{_base()}/active")
        assert resp.status == 403
        body = await resp.json()
        assert body["code"]  # the denial carries a machine-readable code


@pytest.mark.asyncio
async def test_enabled_owner_but_no_sdk_is_404_jobs_not_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_guards(tmp_path, monkeypatch)
    forget_sdk(APP)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"{_base()}/active")
        assert resp.status == 404
        assert (await resp.json())["code"] == "jobs_not_enabled"


@pytest.mark.asyncio
async def test_missing_app_name_is_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The ``{app}`` segment cannot be empty via the router, but the guard's
    # empty-name branch is still worth pinning: register a route with a blank
    # app captured and drive the guarded handler directly.
    _setup_guards(tmp_path, monkeypatch)

    async def _passthrough(request: web.Request, app_name: str, sdk: Any) -> web.StreamResponse:
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/probe/{app}/x", jr._guarded(_passthrough))

    # aiohttp will not match an empty path segment, so exercise the branch by
    # patching match_info via a middleware that blanks the app name.
    @web.middleware
    async def _blank(request: web.Request, handler: Any) -> web.StreamResponse:
        request.match_info["app"] = ""
        return await handler(request)

    app2 = web.Application(middlewares=[_blank])
    app2.router.add_get("/probe/{app}/x", jr._guarded(_passthrough))
    async with TestClient(TestServer(app2)) as client:
        resp = await client.get("/probe/anything/x")
        assert resp.status == 400
        assert (await resp.json())["code"] == "app_name_required"


# 4-6: POST {kind}/start


@pytest.mark.asyncio
async def test_start_registered_kind_returns_run_without_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    _setup_guards(tmp_path, monkeypatch)

    def _runner(handle: Any, **params: Any) -> dict[str, Any]:
        return {"done": True}

    sdk.register("quick", _runner)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(f"{_base()}/quick/start", json={"params": {"a": 1}})
        assert resp.status == 200
        body = await resp.json()
    run = body["run"]
    # The public view withholds params (and origin/pid) on purpose.
    assert "params" not in run
    assert "origin" not in run
    assert "pid" not in run
    # ...and carries the fields the UI renders.
    for field in ("run_id", "kind", "status", "cancellable"):
        assert field in run
    assert run["kind"] == "quick"
    assert run["cancellable"] is False
    await _wait_terminal(sdk, run["run_id"])


@pytest.mark.asyncio
async def test_the_interruption_fields_reach_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    """``interrupted_from`` and ``interrupt_cause`` are served, unlike origin/pid.

    They are not host facts: they are the two facts a client needs in order to
    recover from an interruption -- whether the run may have committed side
    effects, and whether retrying it is possible now -- and the client is the only
    party that can act on either. A record that carries them behind an API that
    withholds them leaves the record honest and the API not.
    """
    from kiro_crew.apps.job_sdk import (
        CAUSE_RUNNER_UNREGISTERED,
        INTERRUPTED,
        STARTING,
        JobRun,
    )

    _setup_guards(tmp_path, monkeypatch)
    run_id = "5c" * 16
    sdk.store.write(
        JobRun(
            run_id=run_id,
            app=sdk.app_name,
            kind="vanished",
            status=STARTING,
            origin="a-process-that-is-gone",
        )
    )
    assert sdk.reconcile() == 1

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"{_base()}/{run_id}")
        assert resp.status == 200
        run = (await resp.json())["run"]
    assert run["status"] == INTERRUPTED
    assert run["interrupted_from"] == STARTING
    assert run["interrupt_cause"] == CAUSE_RUNNER_UNREGISTERED
    # Still withheld: these have no client meaning and never gained one.
    assert "origin" not in run
    assert "pid" not in run


@pytest.mark.asyncio
async def test_work_observed_reaches_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    """``work_observed`` is served: a client reads whether the run did its work
    instead of re-deriving it. It is an SDK-minted boolean, so it needs no
    sanitizing the way the result channel does.
    """
    from kiro_crew.apps.job_sdk import DONE, JobRun

    _setup_guards(tmp_path, monkeypatch)
    run_id = "7d" * 16
    sdk.store.write(
        JobRun(
            run_id=run_id,
            app=sdk.app_name,
            kind="observed",
            status=DONE,
            work_observed=True,
        )
    )

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"{_base()}/{run_id}")
        assert resp.status == 200
        run = (await resp.json())["run"]
    assert run["work_observed"] is True


@pytest.mark.asyncio
async def test_the_callers_own_strings_are_withheld_from_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    """``dedupe_key`` and ``params`` are the app's business, not the run's state.

    Serving them would publish an account id, a target or a scope the client
    never supplied and cannot act on, so the view withholds them like the host
    facts.
    """
    from kiro_crew.apps.job_sdk import DONE, JobRun

    _setup_guards(tmp_path, monkeypatch)
    run_id = "8e" * 16
    run = JobRun(
        run_id=run_id,
        app=sdk.app_name,
        kind="named",
        status=DONE,
        dedupe_key="123456789012",
    )
    run.params = {"account": "123456789012", "mode": "full"}
    sdk.store.write(run)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"{_base()}/{run_id}")
        assert resp.status == 200
        served = (await resp.json())["run"]
    assert "params" not in served
    assert "dedupe_key" not in served
    assert "origin" not in served
    assert "pid" not in served
    # Nothing the app chose is reachable by value either, under any key.
    assert "123456789012" not in json.dumps(served)
    assert served["status"] == DONE


@pytest.mark.asyncio
async def test_unknown_kind_error_is_scrubbed_before_it_is_reflected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    """The 404 body quotes the caller's own `kind`, so it must be scrubbed.

    A path parameter is caller-controlled, so an unscrubbed reflection would
    echo a credential-shaped kind straight back to anyone reaching the surface.
    """
    _setup_guards(tmp_path, monkeypatch)
    secret = "AKIAIOSFODNN7EXAMPLE"
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(f"{_base()}/aws_secret_access_key{secret}/start")
        assert resp.status == 404
        body = await resp.json()
        assert body["code"] == "unknown_job_kind"
        assert secret not in body["error"]


@pytest.mark.asyncio
async def test_start_with_non_string_dedupe_key_is_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    _setup_guards(tmp_path, monkeypatch)
    sdk.register("k", lambda handle, **p: None)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(f"{_base()}/k/start", json={"dedupe_key": 42})
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_dedupe_key"


@pytest.mark.asyncio
async def test_start_with_malformed_body_is_treated_as_empty_not_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    _setup_guards(tmp_path, monkeypatch)
    sdk.register("k", lambda handle, **p: {"ok": True})
    async with TestClient(TestServer(_make_app())) as client:
        # A body that is not JSON at all must not 500 — it is treated as {}.
        resp = await client.post(
            f"{_base()}/k/start",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 200
        run = (await resp.json())["run"]
    await _wait_terminal(sdk, run["run_id"])


@pytest.mark.asyncio
async def test_a_start_whose_run_the_cap_removed_still_answers_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    # A fast run finishes, and a second run's sweep removes its record before the
    # start route reads it back. The run did start, so the route answers with the
    # record as it stood at the start, never a 500 a client might retry.
    _setup_guards(tmp_path, monkeypatch)
    sdk.register("k", lambda handle, **p: None, retention=1)
    real_get = sdk.get

    def swept(run_id: str) -> bool:
        live = sdk.cancelling_and_live_ids()[1]
        return run_id not in live and run_id not in sdk._unswept  # noqa: SLF001

    def get_after_a_newer_run(run_id: str) -> js.JobRun | None:
        wait_until(lambda: swept(run_id))
        newer = sdk.start("k")
        wait_until(lambda: swept(newer))
        return real_get(run_id)

    monkeypatch.setattr(sdk, "get", get_after_a_newer_run)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(f"{_base()}/k/start", json={})
        assert resp.status == 200
        run = (await resp.json())["run"]
    assert run["status"] == js.STARTING
    assert not (sdk.store.dir / f"{run['run_id']}.json").exists()


@pytest.mark.asyncio
async def test_a_capped_kind_answers_400_to_an_overlong_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    _setup_guards(tmp_path, monkeypatch)
    sdk.register("k", lambda handle, **p: None, retention=1)
    key = "secret-" + "k" * js._MAX_DEDUPE_KEY
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(f"{_base()}/k/start", json={"dedupe_key": key})
        assert resp.status == 400
        body = await resp.json()
    assert body["code"] == "invalid_dedupe_key"
    assert "secret-" not in body["error"]
    assert not sdk.store.dir.is_dir() or not list(sdk.store.dir.glob("*.json"))


@pytest.mark.asyncio
async def test_start_unknown_kind_is_404_unknown_job_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    _setup_guards(tmp_path, monkeypatch)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(f"{_base()}/ghost/start", json={})
        assert resp.status == 404
        assert (await resp.json())["code"] == "unknown_job_kind"


# 7: GET active / recent


@pytest.mark.asyncio
async def test_active_and_recent_return_runs_array(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    _setup_guards(tmp_path, monkeypatch)
    sdk.register("k", lambda handle, **p: {"ok": True})
    rid = sdk.start("k")
    await _wait_terminal(sdk, rid)
    async with TestClient(TestServer(_make_app())) as client:
        active = await client.get(f"{_base()}/active")
        recent = await client.get(f"{_base()}/recent")
        assert active.status == 200
        assert recent.status == 200
        assert isinstance((await active.json())["runs"], list)
        recent_runs = (await recent.json())["runs"]
    assert isinstance(recent_runs, list)
    assert any(r["run_id"] == rid for r in recent_runs)


@pytest.mark.asyncio
async def test_recent_non_integer_limit_is_400_invalid_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    _setup_guards(tmp_path, monkeypatch)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"{_base()}/recent", params={"limit": "abc"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_limit"


@pytest.mark.asyncio
async def test_recent_limit_above_cap_is_clamped_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    _setup_guards(tmp_path, monkeypatch)
    seen: dict[str, int] = {}

    def _capture(kind: str, limit: int) -> list[Any]:
        seen["limit"] = limit
        return []

    monkeypatch.setattr(sdk, "list_recent", _capture)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"{_base()}/recent", params={"limit": "10000"})
        assert resp.status == 200
    # Clamped to the module cap rather than refused.
    assert seen["limit"] == jr._RECENT_MAX


# 8: GET {run_id}


@pytest.mark.asyncio
async def test_get_missing_run_is_404_job_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    _setup_guards(tmp_path, monkeypatch)
    async with TestClient(TestServer(_make_app())) as client:
        # A valid-shaped but unknown hex run id.
        resp = await client.get(f"{_base()}/{'a' * 32}")
        assert resp.status == 404
        assert (await resp.json())["code"] == "job_not_found"


@pytest.mark.asyncio
async def test_get_existing_run_is_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    _setup_guards(tmp_path, monkeypatch)
    sdk.register("k", lambda handle, **p: {"ok": True})
    rid = sdk.start("k")
    await _wait_terminal(sdk, rid)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"{_base()}/{rid}")
        assert resp.status == 200
        assert (await resp.json())["run"]["run_id"] == rid


# 9: POST {run_id}/cancel


@pytest.mark.asyncio
async def test_cancel_missing_run_is_404_job_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    _setup_guards(tmp_path, monkeypatch)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(f"{_base()}/{'b' * 32}/cancel")
        assert resp.status == 404
        assert (await resp.json())["code"] == "job_not_found"


@pytest.mark.asyncio
async def test_a_run_the_retention_cap_removed_reads_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    # Every route reads the store, so a record the cap removed answers like an
    # id that never existed, and the run the cap kept is served as before.
    _setup_guards(tmp_path, monkeypatch)
    sdk.register("k", lambda handle, **p: None, retention=1)

    def swept(run_id: str) -> bool:
        # Out of the live table and past its own sweep (``_unswept``), so the
        # sweeps run in the order the runs finish.
        live = sdk.cancelling_and_live_ids()[1]
        return run_id not in live and run_id not in sdk._unswept  # noqa: SLF001

    first = sdk.start("k")
    await _wait_terminal(sdk, first)
    await async_wait_until(lambda: swept(first), off_loop=True)
    second = sdk.start("k")
    await _wait_terminal(sdk, second)
    await async_wait_until(lambda: swept(second), off_loop=True)
    # Checked by name, not by reading it: on Windows an open record blocks its delete.
    assert not (sdk.store.dir / f"{first}.json").exists()
    async with TestClient(TestServer(_make_app())) as client:
        recent = await client.get(f"{_base()}/recent")
        assert [r["run_id"] for r in (await recent.json())["runs"]] == [second]
        got = await client.get(f"{_base()}/{first}")
        cancel = await client.post(f"{_base()}/{first}/cancel")
        for resp in (got, cancel):
            assert resp.status == 404
            assert (await resp.json())["code"] == "job_not_found"
        assert (await client.get(f"{_base()}/{second}")).status == 200


@pytest.mark.asyncio
async def test_cancel_non_cancellable_run_is_409_with_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    _setup_guards(tmp_path, monkeypatch)
    # A terminal, non-cancellable run: cancel_async returns False.
    sdk.register("k", lambda handle, **p: {"ok": True})
    rid = sdk.start("k")
    await _wait_terminal(sdk, rid)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(f"{_base()}/{rid}/cancel")
        assert resp.status == 409
        body = await resp.json()
        assert body["code"] == "job_not_cancellable"
        assert body["run"]["run_id"] == rid


@pytest.mark.asyncio
async def test_cancel_live_cancellable_run_is_200_cancelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    _setup_guards(tmp_path, monkeypatch)
    started = threading.Event()

    def _blocking(handle: Any, **params: Any) -> None:
        started.set()
        # Block until asked to cancel; poll rather than sleep a fixed amount.
        handle.cancelled.wait(timeout=_DEADLINE)

    sdk.register("blocker", _blocking, cancellable=True)
    rid = sdk.start("blocker")
    assert started.wait(timeout=_DEADLINE), "runner never started"
    await _wait_status(sdk, rid, js.RUNNING)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(f"{_base()}/{rid}/cancel")
        assert resp.status == 200
        assert (await resp.json())["cancelling"] is True
    # The worker observes the signal and settles into CANCELLED.
    run = await _wait_terminal(sdk, rid)
    assert run.status == js.CANCELLED


# 9b: a requested cancel survives a fresh read


def _parked_cancellable(sdk: JobSDK, kind: str = "slow") -> tuple[threading.Event, threading.Event]:
    """Register a cancellable runner whose next checkpoint is far away.

    It does not poll ``handle.cancelled`` until ``release`` is set, which holds
    the cancel-requested window open deterministically instead of racing a
    runner that exits the moment it is cancelled.
    """
    started = threading.Event()
    release = threading.Event()

    def _runner(handle: Any, **params: Any) -> None:
        started.set()
        release.wait(timeout=_DEADLINE)

    sdk.register(kind, _runner, cancellable=True)
    return started, release


@pytest.mark.asyncio
async def test_requested_cancel_is_visible_to_a_later_read_of_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    """A GET after the cancel — a reload, a second tab, a fresh mount.

    A client that only re-reads the run must still see that a cancel was
    requested, not just ``running``.
    """
    _setup_guards(tmp_path, monkeypatch)
    started, release = _parked_cancellable(sdk)
    rid = sdk.start("slow")
    try:
        assert started.wait(timeout=_DEADLINE), "runner never started"
        await _wait_status(sdk, rid, js.RUNNING)
        async with TestClient(TestServer(_make_app())) as client:
            assert (await client.post(f"{_base()}/{rid}/cancel")).status == 200
            resp = await client.get(f"{_base()}/{rid}")
            assert resp.status == 200
            run = (await resp.json())["run"]
        assert run["cancelling"] is True
        # Still running: the worker has not reached its checkpoint. Both facts
        # are served together, which is what lets a UI say "cancelling…".
        assert run["status"] == js.RUNNING
    finally:
        release.set()
    assert (await _wait_terminal(sdk, rid)).status == js.CANCELLED


@pytest.mark.asyncio
async def test_requested_cancel_is_visible_in_the_active_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    """``active`` is what a fresh mount adopts, so it must carry the request too."""
    _setup_guards(tmp_path, monkeypatch)
    started, release = _parked_cancellable(sdk)
    rid = sdk.start("slow")
    try:
        assert started.wait(timeout=_DEADLINE), "runner never started"
        await _wait_status(sdk, rid, js.RUNNING)
        async with TestClient(TestServer(_make_app())) as client:
            assert (await client.post(f"{_base()}/{rid}/cancel")).status == 200
            resp = await client.get(f"{_base()}/active")
            assert resp.status == 200
            runs = (await resp.json())["runs"]
        adopted = [r for r in runs if r["run_id"] == rid]
        assert len(adopted) == 1
        assert adopted[0]["cancelling"] is True
    finally:
        release.set()
    assert (await _wait_terminal(sdk, rid)).status == js.CANCELLED


@pytest.mark.asyncio
async def test_a_run_nobody_cancelled_reads_not_cancelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    """The flag is derived, not decoration: an untouched run must read false."""
    _setup_guards(tmp_path, monkeypatch)
    started, release = _parked_cancellable(sdk)
    rid = sdk.start("slow")
    try:
        assert started.wait(timeout=_DEADLINE), "runner never started"
        await _wait_status(sdk, rid, js.RUNNING)
        async with TestClient(TestServer(_make_app())) as client:
            run = (await (await client.get(f"{_base()}/{rid}")).json())["run"]
        assert run["cancelling"] is False
        assert run["status"] == js.RUNNING
    finally:
        release.set()
    await _wait_terminal(sdk, rid)


@pytest.mark.asyncio
async def test_a_recorded_cancel_is_no_longer_cancelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    """Once the status carries the answer, nothing is pending.

    Without the terminal guard a read landing between the worker's write and the
    live entry being dropped would serve ``status: cancelled`` and
    ``cancelling: true`` together — the same cancel both finished and still
    outstanding.
    """
    _setup_guards(tmp_path, monkeypatch)
    started, release = _parked_cancellable(sdk)
    rid = sdk.start("slow")
    try:
        assert started.wait(timeout=_DEADLINE), "runner never started"
        await _wait_status(sdk, rid, js.RUNNING)
        async with TestClient(TestServer(_make_app())) as client:
            assert (await client.post(f"{_base()}/{rid}/cancel")).status == 200
            release.set()
            assert (await _wait_terminal(sdk, rid)).status == js.CANCELLED
            run = (await (await client.get(f"{_base()}/{rid}")).json())["run"]
        assert run["status"] == js.CANCELLED
        assert run["cancelling"] is False
    finally:
        release.set()


# 9c: the live field — a durable record vs a worker actually driving it


def _write_raw_running(sdk: JobSDK, run_id: str, kind: str = "slow") -> js.JobRun:
    """Persist a ``running`` record no thread in this process is driving.

    The shape a record from another gateway process has, and the same shape a
    twice-failed terminal write leaves behind: durable, non-terminal, absent
    from the live table. Written raw because going through ``start`` would
    register exactly the live entry whose absence is the subject.
    """
    run = js.JobRun(
        run_id=run_id,
        app=APP,
        kind=kind,
        status=js.RUNNING,
        origin="foreign-origin-token",
    )
    store = sdk.store
    store.dir.mkdir(parents=True, exist_ok=True)
    (store.dir / f"{run.run_id}.json").write_text(json.dumps(run.to_dict()), encoding="utf-8")
    return run


@pytest.mark.asyncio
async def test_a_driven_run_reads_live_true_then_false_once_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    """``live`` follows the worker, not the record.

    While a registered thread drives the run the view says so; once the run is
    terminal the same read says false — the work is done, nothing is driven.
    """
    _setup_guards(tmp_path, monkeypatch)
    started, release = _parked_cancellable(sdk)
    rid = sdk.start("slow")
    try:
        assert started.wait(timeout=_DEADLINE), "runner never started"
        await _wait_status(sdk, rid, js.RUNNING)
        async with TestClient(TestServer(_make_app())) as client:
            run = (await (await client.get(f"{_base()}/{rid}")).json())["run"]
            assert run["live"] is True
            assert run["status"] == js.RUNNING
            release.set()
            await _wait_terminal(sdk, rid)
            done = (await (await client.get(f"{_base()}/{rid}")).json())["run"]
        assert done["live"] is False
    finally:
        release.set()


@pytest.mark.asyncio
async def test_a_stale_running_record_reads_live_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    """``running`` on disk, nothing driving it.

    ``origin`` and ``pid`` are withheld, so ``live`` is the only served field
    that tells this record apart from real work.
    """
    _setup_guards(tmp_path, monkeypatch)
    stale = _write_raw_running(sdk, "e" * 32)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"{_base()}/{stale.run_id}")
        assert resp.status == 200
        run = (await resp.json())["run"]
    assert run["status"] == js.RUNNING  # the record still claims work…
    assert run["live"] is False  # …and the view says nothing drives it


@pytest.mark.asyncio
async def test_active_list_tells_a_live_run_from_a_stale_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    """``active`` filters on terminal status alone, so a stale-running record
    lists as active forever — the field must let a consumer tell the two rows
    apart within one response."""
    _setup_guards(tmp_path, monkeypatch)
    stale = _write_raw_running(sdk, "d" * 32)
    started, release = _parked_cancellable(sdk)
    rid = sdk.start("slow")
    try:
        assert started.wait(timeout=_DEADLINE), "runner never started"
        await _wait_status(sdk, rid, js.RUNNING)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"{_base()}/active")
            assert resp.status == 200
            runs = {r["run_id"]: r for r in (await resp.json())["runs"]}
        assert runs[rid]["live"] is True
        assert runs[stale.run_id]["live"] is False
    finally:
        release.set()
    await _wait_terminal(sdk, rid)


# 10: restricted session refused on a mutation


@pytest.mark.asyncio
async def test_restricted_session_is_403_restricted_session_on_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    _setup_guards(tmp_path, monkeypatch)
    sdk.register("k", lambda handle, **p: None)
    # _mutating consults request.app["state"]; give it any truthy state and
    # patch the restricted-session predicate on its originating module.
    monkeypatch.setattr(shared, "_is_restricted_session", lambda state, request: True)
    app = _make_app(state=object())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"{_base()}/k/start", json={})
        assert resp.status == 403
        assert (await resp.json())["code"] == "restricted_session"


# 11: route ordering — /active is not a run id


@pytest.mark.asyncio
async def test_active_is_not_matched_as_a_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    # If ``{run_id}`` were registered before ``active``, GET /_jobs/active would
    # be captured as run_id="active" and 404 as job_not_found. It must return
    # the active list instead.
    _setup_guards(tmp_path, monkeypatch)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"{_base()}/active")
        assert resp.status == 200
        body = await resp.json()
        assert "runs" in body
        assert "code" not in body


@pytest.mark.asyncio
async def test_revoked_grant_is_refused_even_with_an_sdk_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grant revoked AFTER the SDK was published must stop serving.

    The SDK lives in the registry for the gateway's life, so the guard re-reads
    the manifest; here the SDK stays registered on purpose.
    """
    _setup_guards(tmp_path, monkeypatch, granted=False)
    sdk = JobSDK(APP, tmp_path / "data")
    sdk.register("k", lambda h, **kw: {})
    register_sdk(sdk)
    try:
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"{_base()}/active")
            assert resp.status == 404
            assert (await resp.json())["code"] == "jobs_not_enabled"
        # The registry entry is still there: the refusal came from the manifest
        # re-read, not from a missing SDK.
        assert get_sdk(APP) is sdk
    finally:
        forget_sdk(APP)


@pytest.mark.asyncio
async def test_sdk_refusal_becomes_a_coded_503_not_a_bare_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A JobError must not reach aiohttp's default handler.

    `start` raises JobError when it cannot persist the initial record or the host
    refuses a thread. Uncaught, that is a bare 500 with no code a client can
    switch on.
    """
    _setup_guards(tmp_path, monkeypatch)
    sdk = JobSDK(APP, tmp_path / "data")
    sdk.register("k", lambda h, **kw: {})
    register_sdk(sdk)

    def refuse(*_a: Any, **_kw: Any) -> str:
        raise JobError("could not persist the initial record for job kind 'k'")

    # The start route calls ``_start_run``, the body of ``start``.
    monkeypatch.setattr(sdk, "_start_run", refuse)
    try:
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"{_base()}/k/start", json={})
            assert resp.status == 503
            assert (await resp.json())["code"] == "job_start_failed"
    finally:
        forget_sdk(APP)


# The off-loop-read invariant behind the polling helpers


def test_wait_helpers_are_coroutine_functions() -> None:
    """Synchronous helpers would read on the event loop and flake on Windows only.

    On the loop, ``read_bytes_with_retry`` re-raises the sharing-violation
    ``PermissionError`` instead of retrying, so the helpers must stay ``async``.
    """
    assert inspect.iscoroutinefunction(_wait_terminal)
    assert inspect.iscoroutinefunction(_wait_status)


@pytest.mark.asyncio
async def test_on_loop_read_propagates_permission_error_offloaded_read_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sdk: JobSDK
) -> None:
    """Prove the sharing-violation defect directly, without a Windows shard.

    With Windows semantics shimmed (``platform_compat.IS_WINDOWS`` true) and the
    record's first ``Path.read_bytes`` raising ``PermissionError`` — the sharing
    violation a concurrent ``os.replace`` produces — a synchronous ``sdk.get``
    on the event loop propagates the error, while the helpers' off-loop polls
    retry and succeed. Shim the module attribute only, never ``os.name``, which
    breaks ``Path.home()`` on POSIX.
    """
    from kiro_crew import platform_compat

    sdk.register("k", lambda handle, **p: {"ok": True})
    rid = sdk.start("k")
    # Settle the run first so no real writer races the injected failure.
    await _wait_terminal(sdk, rid)

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    real_read_bytes = Path.read_bytes
    inject = {"armed": False}

    def flaky_read_bytes(self: Path) -> bytes:
        if inject["armed"] and self.name == f"{rid}.json":
            inject["armed"] = False
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", flaky_read_bytes)

    # A sync read on the event loop: one attempt, the error escapes.
    inject["armed"] = True
    with pytest.raises(PermissionError):
        sdk.get(rid)
    inject["armed"] = False

    # Both helpers poll off-loop, so the retry budget applies and the poll
    # survives the same injected sharing violation.
    inject["armed"] = True
    run = await _wait_terminal(sdk, rid)
    assert run.run_id == rid
    assert not inject["armed"], "_wait_terminal never hit the injected error"

    inject["armed"] = True
    run = await _wait_status(sdk, rid, js.DONE)
    assert run.status == js.DONE
    assert not inject["armed"], "_wait_status never hit the injected error"
