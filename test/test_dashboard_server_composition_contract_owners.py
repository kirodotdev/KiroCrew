"""The clauses the dashboard bootstrap carries, characterized where its owners hold them.

This file pins the asset cache policy, the audited actor, the loop heartbeat and the
local decision model's release, and the owners' own behaviour: the skill-learning
consolidator and staged-skill bell, the diag recorder, the loop watchdog and the
crash-dump report, the dashboard's inner middleware layers and the headless audit,
the listener sidecars, the workflow service hooks and the optional post-bind work.
The suite is described in ``test_dashboard_server_composition_contract.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import threading
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from dashboard_server_composition_contract_helpers import (
    _FACADE,
)

import kiro_crew.dashboard.server as server

# ── the clauses the bootstrap carries ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "status", "expected"),
    [
        ("/assets/index-abc.js", 200, "public, max-age=31536000, immutable"),
        ("/assets/index-abc.js", 206, "public, max-age=31536000, immutable"),
        ("/assets/index-abc.js", 304, "public, max-age=31536000, immutable"),
        ("/assets/diffWorker-abc.js", 200, "public, max-age=60, stale-if-error=86400"),
        ("/assets/Subset-WORKER.chunk-1.js", 304, "public, max-age=60, stale-if-error=86400"),
        ("/assets/index-abc.js", 404, "no-store, no-cache, must-revalidate, max-age=0"),
        ("/assets/diffWorker-abc.js", 503, "no-store, no-cache, must-revalidate, max-age=0"),
        ("/vendor/react.js", 200, "no-store, no-cache, must-revalidate, max-age=0"),
        ("/api/status", 200, "no-store, no-cache, must-revalidate, max-age=0"),
    ],
)
@pytest.mark.asyncio
async def test_the_asset_cache_policy_by_status_and_filename(
    path: str, status: int, expected: str
) -> None:
    """Hashed chunks are immutable, workers short-lived, everything else and every
    error no-store -- decided early in the header middleware and corrected once the
    status is final."""
    app = web.Application()
    response = web.Response(status=200)
    server._apply_security_headers(response, app, path)
    response.set_status(status)
    request = make_mocked_request("GET", path, app=app)
    await server._finalize_asset_cache_control(request, response)
    early = server._asset_cache_control(path) if status in (200, 206, 304) else None
    if early is not None:
        assert response.headers["Cache-Control"] == early == expected
    else:
        assert response.headers["Cache-Control"] == expected
        assert response.headers["Pragma"] == "no-cache"
        assert response.headers["Expires"] == "0"


@pytest.mark.parametrize(
    ("app_claim", "internal", "proxied", "expected"),
    [
        ("", None, False, "dashboard_user"),
        ("", None, True, "dashboard_user_via_proxy"),
        ("my-app", None, False, "my-app"),
        ("my-app", True, False, "dashboard_user:my-app"),
        ("my-app", True, True, "dashboard_user:my-app_via_proxy"),
    ],
)
def test_the_audited_actor_names_the_app_and_the_transport(
    app_claim: str, internal: bool | None, proxied: bool, expected: str
) -> None:
    headers = {"X-Forwarded-For": "203.0.113.9"} if proxied else {}
    request = make_mocked_request("POST", "/api/x", headers=headers)
    request["app"] = app_claim
    if internal is not None:
        request["internal_auth"] = internal
    assert server.audit_actor(request, "dashboard_user") == expected


class _Watchdog:
    """A loop stall watchdog double recording what the heartbeat asks of it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def claim_lag_enrichment(self, lag: float) -> bool:
        self.calls.append(("claim", threading.current_thread().name))
        return sum(1 for name, _ in self.calls if name == "claim") == 1

    def beat(self) -> None:
        self.calls.append(("beat", threading.current_thread().name))

    def log_lag_enrichment(self, lag: float) -> None:
        self.calls.append(("log", threading.current_thread().name))


@pytest.mark.asyncio
async def test_the_heartbeat_claims_the_capture_before_it_beats_and_logs_off_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bounded enrichment per lag episode: the capture is claimed before the beat
    that would end the episode, and the collector runs on an executor thread behind a
    shielded, bounded wait, so the loop it watches never waits on it unbounded."""
    real_sleep = asyncio.sleep
    ticks: list[float] = []

    async def _sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        ticks.append(delay)
        # flake-ok: pre-existing asyncio.sleep stand-in that yields one tick instead of waiting; moved verbatim and tracked for a de-flake pass
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    state = MagicMock()
    state.resource_pressure_notifier.maybe_sample = AsyncMock()
    trimmer = MagicMock()
    trimmer.maybe_trim = AsyncMock(return_value=0)
    watchdog = _Watchdog()
    task = server._start_loop_heartbeat(state, watchdog, trimmer)
    try:
        for _ in range(200):
            if sum(1 for name, _ in watchdog.calls if name == "beat") >= 3:
                break
            # flake-ok: pre-existing 10 ms poll bounded at 200 rounds while the heartbeat beats three times; moved verbatim and tracked for a de-flake pass
            await real_sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    names = [name for name, _ in watchdog.calls]
    assert names[:4] == ["claim", "beat", "log", "claim"]
    main = threading.main_thread().name
    assert [thread for name, thread in watchdog.calls if name == "log"] != [main]
    assert all(thread == main for name, thread in watchdog.calls if name != "log")
    assert ticks and set(ticks) == {5.0}
    source = inspect.getsource(server._start_loop_heartbeat)
    assert "await asyncio.wait_for(asyncio.shield(capture), 2.0)" in source
    assert "None, _loop_watchdog.log_lag_enrichment, lag" in source


@pytest.mark.asyncio
async def test_the_local_decision_model_release_waits_for_its_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cleanup hook releases the local decision model only once its runtime module
    was imported at all -- a gateway that never ran one does not import it to stop it
    -- and the release waits for the model off the event loop."""
    import kiro_crew.decisions as decisions

    name = "kiro_crew.decisions.local_runtime"
    app = web.Application()
    server._register_stt_hooks(app)
    hook = next(h for h in app.on_cleanup if h.__name__ == "_local_decision_model_shutdown")
    monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delattr(decisions, "local_runtime", raising=False)
    await hook(app)
    assert name not in sys.modules
    calls: list[tuple[dict[str, Any], bool]] = []

    def _deactivate(**kwargs: Any) -> None:
        calls.append((kwargs, threading.current_thread() is threading.main_thread()))

    runtime = types.ModuleType(name)
    runtime.get_runtime = lambda: types.SimpleNamespace(deactivate=_deactivate)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, runtime)
    monkeypatch.setattr(decisions, "local_runtime", runtime, raising=False)
    await hook(app)
    assert calls == [({"wait": True}, False)]


@pytest.mark.asyncio
async def test_the_decision_http_release_waits_for_its_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cleanup hook closes the decision client's shared HTTP sessions only once
    that module was imported at all -- a gateway that never made a decision does not
    import it to close nothing."""
    import kiro_crew.decisions as decisions

    name = "kiro_crew.decisions.impl_jev"
    app = web.Application()
    server._register_stt_hooks(app)
    hook = next(h for h in app.on_cleanup if h.__name__ == "_decision_http_shutdown")
    monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delattr(decisions, "impl_jev", raising=False)
    await hook(app)
    assert name not in sys.modules
    closed: list[bool] = []

    async def _close_sessions() -> None:
        closed.append(True)

    module = types.ModuleType(name)
    module.close_sessions = _close_sessions  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(decisions, "impl_jev", module, raising=False)
    await hook(app)
    assert closed == [True]


# ── the owners' own behaviour ─────────────────────────────────────────────────


@web.middleware
async def _through(request: web.Request, handler: Any) -> web.StreamResponse:
    return await handler(request)


def _skill_settings() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        auto_create_from_sessions=True,
        auto_refine_on_deviation=False,
        auto_min_tool_calls=3,
        auto_similarity_threshold=0.8,
        approval_required=True,
        max_auto_skills=7,
        stale_after_days=30,
        archive_after_days=90,
        generate_scripts=False,
        judge_model="auto",
    )


def test_a_dashboard_only_launch_builds_its_own_consolidator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handed a conversation log but no consolidator, the dashboard builds one wired
    with the same skill defaults as the CLI; a failure leaves it without one."""
    built: list[dict[str, Any]] = []
    stores: list[MagicMock] = []

    def _consolidator(**kwargs: Any) -> str:
        built.append(kwargs)
        return "consolidator"

    def _store() -> MagicMock:
        stores.append(MagicMock())
        return stores[-1]

    monkeypatch.setattr("kiro_crew.history.HistoryConsolidator", _consolidator)
    monkeypatch.setattr("kiro_crew.memory.MemoryStore", _store)
    monkeypatch.setattr(server, "SkillsLoader", lambda **kwargs: ("loader", kwargs))
    loader = MagicMock()
    loader.load.return_value.skills = _skill_settings()
    monkeypatch.setattr(server, "KiroCrewConfig", loader)
    assert server._auto_create_consolidator("sessions", "lessons", None, "log") == "consolidator"
    stores[0].init.assert_called_once_with()
    assert built[0] == {
        "log": "log",
        "memory": stores[0],
        "sessions": "sessions",
        "lesson_store": "lessons",
        "skills_loader": ("loader", {"install_builtins": False}),
        "auto_skills_enabled": True,
        "auto_refine_enabled": False,
        "auto_min_tool_calls": 3,
        "auto_similarity_threshold": 0.8,
        "approval_required": True,
        "max_auto_skills": 7,
        "stale_after_days": 30,
        "archive_after_days": 90,
        "generate_scripts": False,
        "judge_model": "auto",
    }
    builder = types.SimpleNamespace(memory=MagicMock(), skills="builder-skills")
    assert server._auto_create_consolidator("s", "l", builder, "log") == "consolidator"
    builder.memory.init.assert_not_called()
    assert (built[1]["memory"], built[1]["skills_loader"]) == (builder.memory, "builder-skills")
    loader.load.side_effect = OSError("unreadable")
    assert server._auto_create_consolidator("s", "l", builder, "log") is None


def _skill_hooks(monkeypatch: pytest.MonkeyPatch, state: Any) -> tuple[Any, Any]:
    staged: list[Any] = []
    consumed: list[Any] = []
    monkeypatch.setattr(server, "set_pending_staged_hook", staged.append)
    monkeypatch.setattr(server, "set_pending_consumed_hook", consumed.append)
    server._register_pending_skill_hooks(state)
    return staged[0], consumed[0]


@pytest.mark.asyncio
async def test_a_staged_skill_rings_the_bell_on_the_serving_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate staged on a worker thread is announced on the gateway loop, and a
    consumed one retires its notification there."""
    state = MagicMock()
    state._background_tasks = set()
    state.serving_loop = asyncio.get_running_loop()
    state.resolve_skill_review_notifications = AsyncMock()
    on_staged, on_consumed = _skill_hooks(monkeypatch, state)
    info = {"slug": "tidy-up", "name": "Tidy", "kind": "update", "target": "tidy"}
    await asyncio.to_thread(on_staged, info)
    for _ in range(3):
        # flake-ok: pre-existing bounded yield ticks for the tasks the code under test schedules; moved verbatim and tracked for a de-flake pass
        await asyncio.sleep(0)
    payload = {"slug": "tidy-up", "candidate_kind": "update", "target": "tidy"}
    title, body, url, actions = server._pending_skill_notification(info)
    state.notify.assert_called_once_with(
        "skills", title, body, meta=payload, url=url, actions=actions
    )
    state.broadcast_ws.assert_called_once_with("skills.pending_changed", payload)
    await asyncio.to_thread(on_consumed, {"slug": "tidy-up", "consumed_at": "t1"})
    await asyncio.to_thread(on_consumed, {"slug": "", "consumed_at": "t2"})
    for _ in range(5):
        # flake-ok: pre-existing bounded yield ticks for the tasks the code under test schedules; moved verbatim and tracked for a de-flake pass
        await asyncio.sleep(0)
    state.resolve_skill_review_notifications.assert_awaited_once_with("tidy-up", "t1")
    assert state._background_tasks == set()


def test_without_a_serving_loop_the_bell_rings_inline_and_retires_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = MagicMock()
    state.serving_loop = None
    state.notify.side_effect = RuntimeError("feed unavailable")
    on_staged, on_consumed = _skill_hooks(monkeypatch, state)
    on_staged({"slug": "tidy-up", "name": "Tidy"})
    state.notify.assert_called_once()
    assert state.notify.call_args.args[0] == "skills"
    assert state.notify.call_args.kwargs["meta"]["slug"] == "tidy-up"
    state.broadcast_ws.assert_not_called()
    on_consumed({"slug": "tidy-up", "consumed_at": "t1"})
    state.resolve_skill_review_notifications.assert_not_called()

    def _refuse(_hook: Any) -> None:
        raise RuntimeError("skills module unavailable")

    monkeypatch.setattr(server, "set_pending_staged_hook", _refuse)
    server._register_pending_skill_hooks(state)


class _Recorder:
    """A diagnostic recorder double: the source it is given, and its start."""

    instances: list[_Recorder] = []
    refuse_start = False

    def __init__(self) -> None:
        self.sources: dict[str, Any] = {}
        self.loops: list[Any] = []
        _Recorder.instances.append(self)

    def register_source(self, name: str, source: Any) -> None:
        self.sources[name] = source

    def start(self, loop: Any) -> None:
        if _Recorder.refuse_start:
            raise RuntimeError("no disk")
        self.loops.append(loop)


@pytest.mark.asyncio
async def test_the_recorder_row_reads_the_gateway_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each field of the gateway's recorder row comes from the canonical accessor and
    degrades to None on its own; a cap of 0 means unknown, never a cap of zero."""
    monkeypatch.setattr(_Recorder, "instances", [])
    monkeypatch.setattr(_Recorder, "refuse_start", False)
    monkeypatch.setattr("kiro_crew.diag.recorder.Recorder", _Recorder)
    state = types.SimpleNamespace(sessions=types.SimpleNamespace(count=2), subagents=None)
    server._start_diag_recorder(state)
    recorder = _Recorder.instances[0]
    assert recorder.loops == [asyncio.get_running_loop()]
    adaptive = {
        "enabled": True,
        "last": {"action": "hold", "reason": "ok", "signals": ["cpu"], "paused": False},
        "last_sample": {"running": 1, "queued": 4, "loop_lag_ms": 12},
    }
    monkeypatch.setattr("kiro_crew.resource_status.adaptive_state", lambda: adaptive)
    monkeypatch.setattr("kiro_crew.resource_status.adaptive_exec_cap", lambda: 0)
    monkeypatch.setattr("kiro_crew.metrics.inventory_gauges.read_active_monitor_loops", lambda: 3)
    assert recorder.sources["gateway"]() == {
        "sessions": 2,
        "subagents": 0,
        "live_loops": 3,
        "adaptive_cap": None,
        "adaptive_action": "hold",
        "adaptive_reason": "ok",
        "adaptive_signals": ["cpu"],
        "adaptive_paused": False,
        "adaptive_enabled": True,
        "subagents_running": 1,
        "subagents_queued": 4,
        "controller_loop_lag_ms": 12,
    }

    def _broken() -> Any:
        raise RuntimeError("gauge unreadable")

    for target in (
        "kiro_crew.resource_status.adaptive_state",
        "kiro_crew.resource_status.adaptive_exec_cap",
        "kiro_crew.metrics.inventory_gauges.read_active_monitor_loops",
    ):
        monkeypatch.setattr(target, _broken)
    state.sessions = None
    state.subagents = types.SimpleNamespace()
    row = recorder.sources["gateway"]()
    assert {k: row[k] for k in ("sessions", "subagents", "live_loops", "adaptive_cap")} == {
        "sessions": None,
        "subagents": None,
        "live_loops": None,
        "adaptive_cap": None,
    }
    monkeypatch.setattr(_Recorder, "refuse_start", True)
    server._start_diag_recorder(state)


@pytest.mark.asyncio
async def test_the_recorder_stop_is_awaited_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = web.Application()
    server._register_diag_recorder_shutdown(app)
    hook = app.on_cleanup[-1]
    finished: list[str] = []

    async def _finish() -> None:
        finished.append("joined")

    recorder = MagicMock()
    recorder.stop.side_effect = lambda: asyncio.ensure_future(_finish())
    monkeypatch.setattr("kiro_crew.diag.recorder.get_recorder", lambda: recorder)
    await hook(app)
    assert finished == ["joined"]
    recorder.stop.side_effect = RuntimeError("probe thread wedged")
    await hook(app)
    monkeypatch.setattr("kiro_crew.diag.recorder.get_recorder", lambda: None)
    await hook(app)


@pytest.mark.asyncio
async def test_the_watchdog_budget_falls_back_to_the_managed_service_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config failure must not erase the grace a managed service launch asked for:
    the watchdog is armed with the environment's budget and a fresh dump file."""
    steps: list[str] = []
    monkeypatch.setattr(server, "sweep_stale_dumps", lambda: steps.append("sweep"))
    monkeypatch.setattr(server, "rotate_dumps", lambda: steps.append("rotate"))
    monkeypatch.setattr(server, "open_dump_file", lambda: "dump.txt")

    def _unreadable(_environ: Any) -> float:
        raise OSError("config unreadable")

    monkeypatch.setattr(server, "load_loop_stall_exit_after", _unreadable)
    monkeypatch.setattr(server, "resolve_loop_stall_exit_after", lambda *, environ: 40)
    watchdog = MagicMock()
    monkeypatch.setattr(server, "LoopStallWatchdog", watchdog)
    assert await server._open_loop_watchdog({"KIROCREW_MANAGED": "1"}) is watchdog.return_value
    assert steps == ["sweep", "rotate"]
    watchdog.assert_called_once_with(dump_file="dump.txt", exit_after=40.0)


@pytest.mark.asyncio
async def test_a_blocked_loop_is_logged_and_a_dead_heartbeat_reports_itself(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A tick that comes back late logs the lag, a trim worth reporting is logged, a
    capture that fails does not stop the beat, and a heartbeat that dies says so."""
    import logging

    real_sleep = asyncio.sleep

    async def _sleep(delay: float, *args: Any, **kwargs: Any) -> None:
        # flake-ok: pre-existing asyncio.sleep stand-in that yields one tick instead of waiting; moved verbatim and tracked for a de-flake pass
        await real_sleep(0)

    clock = iter([0.0, 10.0, 10.0, 15.5])
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    monkeypatch.setattr(server.platform_compat, "awake_now", lambda: next(clock))
    state = MagicMock()
    state.resource_pressure_notifier.maybe_sample = AsyncMock(
        side_effect=[None, RuntimeError("notifier gone")]
    )
    trimmer = MagicMock()
    trimmer.maybe_trim = AsyncMock(
        return_value=server.platform_compat.HEAP_TRIM_LOG_THRESHOLD_BYTES
    )
    watchdog = MagicMock()
    watchdog.claim_lag_enrichment.return_value = True
    watchdog.log_lag_enrichment.side_effect = RuntimeError("no stacks")
    with caplog.at_level(logging.DEBUG, logger=_FACADE):
        task = server._start_loop_heartbeat(state, watchdog, trimmer)
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(3):
            # flake-ok: pre-existing bounded yield ticks for the tasks the code under test schedules; moved verbatim and tracked for a de-flake pass
            await real_sleep(0)
    messages = [record.getMessage() for record in caplog.records]
    assert "event-loop heartbeat: lag 5.0s (loop was blocked)" in messages
    assert "Gateway heap trim returned 16 MiB to the OS" in messages
    assert "heartbeat lag capture not awaited" in messages
    assert "event-loop heartbeat task exited unexpectedly" in messages
    assert watchdog.beat.call_count == 2


@pytest.mark.asyncio
async def test_a_prior_crash_dump_is_reported_once_with_its_stacks(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    import logging

    dump = tmp_path / "stall.txt"
    claims: list[Path] = []
    monkeypatch.setattr(server, "newest_dump_with_stacks", lambda: dump)
    monkeypatch.setattr(server, "dump_age_seconds", lambda path: 7200.0)
    monkeypatch.setattr(server, "claim_dump_notification", lambda path: claims.append(path) or True)
    monkeypatch.setattr(server, "dump_replay_lines", lambda path: (["frame a", "frame b"], True))
    monkeypatch.setattr(server, "data_home", lambda: tmp_path)
    monkeypatch.setattr(server, "attribute_dump", lambda path, home: ("attributed", path, home))
    monkeypatch.setattr(server, "describe", lambda attribution: ["It was running cron nightly"])
    state = MagicMock()
    with caplog.at_level(logging.WARNING, logger=_FACADE):
        await server._report_prior_crash_dump(state)
    assert claims == [dump]
    replay = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Replaying")]
    assert replay == [
        "Replaying prior crash dump stacks:\nframe a\nframe b\n"
        "  [truncated — full dump at above path]"
    ]
    state.notify.assert_called_once_with(
        "heartbeat",
        "⚠️ Gateway restarted after an event-loop stall",
        "The previous gateway stopped responding and exited 2.0h ago, then restarted. "
        "Work in flight at that moment was interrupted and not saved. "
        f"It was running cron nightly. Thread stacks: {dump}",
        meta={"url": "/settings", "dump": str(dump)},
    )

    def _unattributable(path: Path, home: Path) -> Any:
        raise ValueError("unparseable dump")

    monkeypatch.setattr(server, "attribute_dump", _unattributable)
    state.notify.side_effect = RuntimeError("feed unavailable")
    await server._report_prior_crash_dump(state)
    assert "Thread stacks" in state.notify.call_args.args[2]
    assert "It was running" not in state.notify.call_args.args[2]
    monkeypatch.setattr(server, "claim_dump_notification", lambda path: False)
    state.notify.reset_mock()
    await server._report_prior_crash_dump(state)
    state.notify.assert_not_called()


def _dashboard_chain(**overrides: Any) -> web.Application:
    app = web.Application()
    kwargs: dict[str, Any] = {
        "deny_audit_middleware": _through,
        "host_canonical_redirect": _through,
        "host_validation_middleware": _through,
        "csrf_middleware": _through,
        "internal_secret": "per-boot",
        "port": 25300,
        "local_only": True,
        "tailnet_trust": None,
        "tailnet_host": "",
        "configured_host": "127.0.0.1",
        "dashboard_url": "",
    }
    server._install_dashboard_middlewares(app, **(kwargs | overrides))
    return app


def _layer(app: web.Application, name: str) -> Any:
    return next(mw for mw in app.middlewares if mw.__name__ == name)


def _sel_double(monkeypatch: pytest.MonkeyPatch, *targets: str) -> MagicMock:
    log = MagicMock()
    for target in targets:
        monkeypatch.setattr(target, lambda: log)
    return log


@pytest.mark.asyncio
async def test_the_dashboard_inner_layers_serve_the_shell_and_audit_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A route-less navigation is answered with the SPA shell and an API miss is
    not; a mutating API call is recorded once, with its outcome, and re-raised."""
    shell = web.Response(text="shell")
    monkeypatch.setattr(server.handlers, "index", AsyncMock(return_value=shell))
    app = _dashboard_chain()
    spa = _layer(app, "spa_fallback")

    async def _missing(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPNotFound()

    assert await spa(make_mocked_request("GET", "/crew", app=app), _missing) is shell
    with pytest.raises(web.HTTPNotFound):
        await spa(make_mocked_request("GET", "/api/nothing", app=app), _missing)
    log = _sel_double(monkeypatch, "kiro_crew.sel.sel")
    audit = _layer(app, "sel_audit_middleware")

    async def _created(_request: web.Request) -> web.StreamResponse:
        return web.Response(status=201)

    async def _broken(_request: web.Request) -> web.StreamResponse:
        raise RuntimeError("handler failed")

    await audit(make_mocked_request("POST", "/api/things", app=app), _created)
    with pytest.raises(RuntimeError):
        await audit(make_mocked_request("DELETE", "/api/things", app=app), _broken)
    await audit(make_mocked_request("GET", "/api/things", app=app), _created)
    assert [c.kwargs for c in log.log_api_access.call_args_list] == [
        {
            "caller": "dashboard_user",
            "forwarded": False,
            "operation": "POST /api/things",
            "outcome": "ok",
            "resources": "/api/things",
        },
        {
            "caller": "dashboard_user",
            "forwarded": False,
            "operation": "DELETE /api/things",
            "outcome": "error",
            "resources": "/api/things",
            "error": "handler failed",
        },
    ]


def test_a_remote_dashboard_url_is_refused_without_token_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dashboard.url`` widens the CSRF origin set, which is safe only behind token
    auth: with it the origin is added, without it the dashboard refuses to start."""
    app = _dashboard_chain(dashboard_url="https://crew.example.test")
    assert {"https://crew.example.test"} <= app["allowed_origins"]
    monkeypatch.setattr(server, "token_auth_middleware", lambda **kwargs: _through)
    with pytest.raises(RuntimeError, match="dashboard_url requires token auth middleware"):
        _dashboard_chain(dashboard_url="https://crew.example.test")


@pytest.mark.asyncio
async def test_the_headless_audit_records_every_api_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = web.Application()
    server._install_api_middlewares(
        app,
        deny_audit_middleware=_through,
        host_validation_middleware=_through,
        csrf_middleware=_through,
        internal_secret="per-boot",
        port=25300,
        local_only=True,
        tailnet_trust=None,
    )
    log = _sel_double(monkeypatch, f"{_FACADE}.sel")
    audit = _layer(app, "sel_audit_middleware")

    async def _broken(_request: web.Request) -> web.StreamResponse:
        raise RuntimeError("tool failed")

    async def _page(_request: web.Request) -> web.StreamResponse:
        return web.Response(text="page")

    with pytest.raises(RuntimeError):
        await audit(make_mocked_request("GET", "/api/spawn", app=app), _broken)
    assert (await audit(make_mocked_request("GET", "/health", app=app), _page)).text == "page"
    assert [c.kwargs for c in log.log_api_access.call_args_list] == [
        {
            "caller": "mcp_tool",
            "forwarded": False,
            "operation": "GET /api/spawn",
            "outcome": "error",
            "resources": "/api/spawn",
            "error": "tool failed",
        }
    ]


def test_a_rebound_listener_republishes_the_sidecar_it_owns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rebind re-advertises the claim recorded at publication, with this
    generation's secret; a failed write is logged and leaves the address unnoted."""
    written: list[tuple[Path, str]] = []
    noted: list[tuple[int, str]] = []
    monkeypatch.setattr(
        server,
        "run_marker",
        types.SimpleNamespace(
            listener_secret_path=lambda port, address: tmp_path / f"{port}-{address}",
            note_published_listener=lambda port, address: noted.append((port, address)),
        ),
    )
    monkeypatch.setattr(server, "_write_secret_file", lambda p, s: written.append((p, s)))
    state = types.SimpleNamespace()
    server._republish_listener_sidecar(state, "secondary")
    server._note_listener_sidecar(state, "secondary", 25301, "", "minted")
    assert not hasattr(state, "_listener_sidecars")
    server._note_listener_sidecar(state, "secondary", 25301, "::1", "minted")
    server._note_listener_sidecar(state, "primary", 25301, "127.0.0.1", "")
    server._republish_listener_sidecar(state, "primary")
    server._republish_listener_sidecar(state, "secondary")
    assert (written, noted) == ([(tmp_path / "25301-::1", "minted")], [(25301, "::1")])

    def _refuse(path: Path, secret: str) -> None:
        raise PermissionError("read-only run dir")

    monkeypatch.setattr(server, "_write_secret_file", _refuse)
    server._republish_listener_sidecar(state, "secondary")
    assert noted == [(25301, "::1")]


def test_a_second_listener_that_cannot_withdraw_ends_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Losing the second loopback family degrades to an explicit sign-in only while
    its sidecar was withdrawn; otherwise both guards are asked for the exit."""
    secondary = server.SecondaryLoopback(address="::1", site=MagicMock())
    state = types.SimpleNamespace(_listener_guard=MagicMock(), _secondary_listener_guard=None)
    monkeypatch.setattr(server, "_withdraw_listener_sidecar", lambda s, which: True)
    server._secondary_listener_given_up(state, 25301, secondary, "rebind refused")
    state._listener_guard.request_exit.assert_not_called()
    monkeypatch.setattr(server, "_withdraw_listener_sidecar", lambda s, which: False)
    state._secondary_listener_guard = MagicMock()
    server._secondary_listener_given_up(state, 25301, secondary, "rebind refused")
    state._listener_guard.request_exit.assert_called_once_with("rebind refused")
    state._secondary_listener_guard.request_exit.assert_called_once_with("rebind refused")


@pytest.mark.asyncio
async def test_the_workflow_service_hooks_redact_announce_and_authorize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workflow service's callbacks: a run event is broadcast with its session, a
    finished run's result opens an agent turn whose prompt names the run and its
    status, and a workflow nudge goes through the shared authorization chokepoint."""
    created: dict[str, Any] = {}

    async def _create(**kwargs: Any) -> Any:
        created.update(kwargs)
        return types.SimpleNamespace(timeout_secs=kwargs["timeout_secs"], registry=MagicMock())

    monkeypatch.setattr("kiro_crew.workflows.service.WorkflowService.create", _create)
    slot = MagicMock()
    slot.enqueue_or_run_prompt.return_value = True

    async def _inject(state_: Any, run_id: str, snapshot: dict, *, on_injected: Any) -> None:
        on_injected(slot, snapshot)
        on_injected(None, snapshot)

    monkeypatch.setattr("kiro_crew.dashboard.workflow_inject.inject_bound_workflow_result", _inject)
    loader = MagicMock()
    loader.load.side_effect = OSError("config unreadable")
    monkeypatch.setattr(server, "KiroCrewConfig", loader)
    state = MagicMock()
    state._background_tasks = set()
    state.workflow_startup_stopping = False
    state.sessions.admission_closed = False
    state.task_runner = None
    await server._initialize_workflow_service(state)
    assert state.workflow_startup_status == "ready"
    assert created["timeout_secs"] is None and created["concurrency"] == 4
    state.workflow_service.registry.get.return_value = types.SimpleNamespace(session_key="s1")
    created["on_event"]("run-1", {"kind": "step"})
    state.broadcast_ws.assert_called_once_with(
        "workflow_run_event", {"run_id": "run-1", "session_key": "s1", "kind": "step"}
    )
    state.broadcast_ws.side_effect = RuntimeError("socket gone")
    created["on_event"]("run-1", {"kind": "step"})

    created["on_done"]("run-1", {"name": "nightly", "status": "done"})
    for _ in range(3):
        # flake-ok: pre-existing bounded yield ticks for the tasks the code under test schedules; moved verbatim and tracked for a de-flake pass
        await asyncio.sleep(0)
    prompt = slot.enqueue_or_run_prompt.call_args.args[0]
    assert prompt.startswith("[Workflow `nightly` finished: done] Its result was just posted")
    state.push_slots_update.assert_called_once_with()
    assert state._background_tasks == set()

    authorize = AsyncMock(return_value=(None, "loop refused", 403))
    monkeypatch.setattr(server, "authorize_and_add_nudge", authorize)
    monkeypatch.setattr(server, "_autonudge_get", lambda: "autonudge")
    refusal = await created["nudge_authorizer"](
        slot_key="slot-1", message="keep going", idle_secs=60, max_cycles=3
    )
    assert refusal == "loop refused"
    authorize.assert_awaited_once_with(
        svc="autonudge",
        state=state,
        slot_key="slot-1",
        message="keep going",
        idle_secs=60,
        max_cycles=3,
        source="workflow",
    )


@pytest.mark.asyncio
async def test_the_optional_post_bind_work_never_fails_the_gateway(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A warm-mint shutdown, a decision-model resume and an own-address warm that
    fail are logged, and a resumed decision model says which one started."""
    import logging

    app = web.Application()
    server._register_connections_warm_lifecycle(app, MagicMock())
    shutdown = next(h for h in app.on_cleanup if h.__name__ == "_connections_warm_shutdown")
    monkeypatch.setattr(
        "kiro_crew.connections.warm.shutdown_warm_mint",
        AsyncMock(side_effect=RuntimeError("mint wedged")),
    )
    state = MagicMock()
    state._background_tasks = set()
    resume = AsyncMock(side_effect=["qwen-small", RuntimeError("no model")])
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.decisions.resume_local_decision_model", resume
    )
    with caplog.at_level(logging.INFO, logger=_FACADE):
        await shutdown(app)
        for _ in range(2):
            server._kick_local_decision_model(state)
            await asyncio.gather(*state._background_tasks)
        cancelled = asyncio.get_running_loop().create_future()
        cancelled.cancel()
        server._OWN_HOST_WARM_TASKS.add(cancelled)
        server._own_host_warm_done(cancelled)
    messages = [record.getMessage() for record in caplog.records]
    assert "Connections warm shutdown failed" in messages
    assert "local decision model: starting qwen-small" in messages
    assert "local decision model: resume at startup failed" in messages
    assert cancelled not in server._OWN_HOST_WARM_TASKS
