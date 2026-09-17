"""Phase-4 cron producer + residuals: CLI-inject path, shell_refused, outcome.

* An agent-message job owned by a ``kiro-cli:<id>`` session is diverted to the
  wake queue instead of running the LLM callback in a gateway session, and its
  history record carries ``outcome='injected'``.
* A ``script``/``command`` job, and an agent-message job owned by anything else,
  runs the ordinary callback path unchanged.
* Residual 0: ``POST /api/crons`` for a ``command=`` job refuses at add time
  with ``shell_refused`` when the host has no POSIX-strict shell.
* Residual 2: ``CronRunRecord`` round-trips the new ``outcome`` field.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.dashboard.handlers.cron as cron_handler
from kiro_crew.cron import CronJob, CronService
from kiro_crew.cron_history import CronRunRecord
from kiro_crew.dashboard.handlers.cron import api_crons_create
from test.body_stream_helpers import attach_body

# -- residual 2: history outcome round-trip -----------------------------------


def test_cron_run_record_round_trips_outcome() -> None:
    rec = CronRunRecord(job_id="j1", status="success", outcome="injected")
    d = rec.to_dict()
    assert d["outcome"] == "injected"
    back = CronRunRecord.from_dict(d)
    assert back.outcome == "injected"


def test_cron_run_record_outcome_defaults_empty() -> None:
    # A legacy record (no outcome key) loads with outcome="" — "no finer signal".
    rec = CronRunRecord.from_dict({"job_id": "j1", "status": "failure"})
    assert rec.outcome == ""


# -- residual 0: shell_refused ------------------------------------------------


def _shell_request(body: dict):
    add = AsyncMock(return_value=CronJob(id="j1", name="x", message=""))
    state = SimpleNamespace(crons=SimpleNamespace(add_job_async=add), push_refresh=MagicMock())
    request = MagicMock()
    request.app = {"state": state}
    request.get = lambda k, d=None: d
    request.headers = {}
    attach_body(request, body)
    return request, add


@pytest.mark.asyncio
async def test_command_job_refused_when_no_posix_shell(monkeypatch) -> None:
    monkeypatch.setattr(cron_handler, "_resolve_command_shell", lambda: None)
    req, add = _shell_request({"name": "backup", "command": "echo hi", "every": 3600})
    resp = await api_crons_create(req)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["code"] == "shell_refused"
    # Never stored: no orphan job on refusal.
    add.assert_not_awaited()


@pytest.mark.asyncio
async def test_command_job_accepted_when_shell_resolves(monkeypatch) -> None:
    monkeypatch.setattr(cron_handler, "_resolve_command_shell", lambda: "/bin/sh")
    req, add = _shell_request({"name": "backup", "command": "echo hi", "every": 3600})
    resp = await api_crons_create(req)
    assert resp.status == 200
    assert add.await_args.kwargs["command"] == "echo hi"


@pytest.mark.asyncio
async def test_script_job_not_gated_on_shell(monkeypatch) -> None:
    # A script job does not use sh -c; a refused shell must not block it.
    monkeypatch.setattr(cron_handler, "_resolve_command_shell", lambda: None)
    monkeypatch.setattr(cron_handler, "resolve_script_path", lambda s: ("/tmp/ok.py", "run"))
    monkeypatch.setattr(cron_handler, "_vet_script_file", lambda p: None)
    req, add = _shell_request(
        {"name": "poll", "script": "~/.kiro/crew/crons/x.py:run", "every": 3600}
    )
    resp = await api_crons_create(req)
    assert resp.status == 200


# -- producer: _execute divert for a kiro-cli owner ---------------------------


def _svc_with_hooks(tmp_path, enqueued: list, on_job) -> CronService:
    svc = CronService(base_dir=tmp_path, on_job=on_job, _defer_initial_load=False)

    async def _wake(job: CronJob) -> bool:
        enqueued.append(job.id)
        return True

    svc.set_kiro_cli_message_callback(_wake)
    return svc


@pytest.mark.asyncio
async def test_kiro_cli_message_job_enqueues_and_skips_callback(tmp_path) -> None:
    called: list[str] = []
    enqueued: list[str] = []

    async def on_job(job: CronJob) -> None:
        called.append(job.id)

    svc = _svc_with_hooks(tmp_path, enqueued, on_job)
    job = CronJob(id="j1", name="poll", message="check", session_key="kiro-cli:sess-1")

    await svc._execute(job)  # noqa: SLF001

    # Enqueued, not run in a gateway session.
    assert enqueued == ["j1"]
    assert called == []
    assert job.last_status == "ok"
    # Marked so _run_job_isolated's finally does not double-record.
    assert getattr(job, "cli_injected", False) is True
    # The authoritative record carries outcome='injected'.
    records, total = await svc._history.get_job_history("j1")  # noqa: SLF001
    assert total == 1
    assert records[0]["outcome"] == "injected"
    assert records[0]["status"] == "success"


@pytest.mark.asyncio
async def test_non_supervised_message_job_runs_callback(tmp_path) -> None:
    called: list[str] = []
    enqueued: list[str] = []

    async def on_job(job: CronJob) -> None:
        called.append(job.id)

    svc = _svc_with_hooks(tmp_path, enqueued, on_job)
    job = CronJob(id="j2", name="poll", message="check", session_key="cron:j2")

    await svc._execute(job)  # noqa: SLF001

    assert enqueued == []
    assert called == ["j2"]
    assert getattr(job, "cli_injected", False) is False


@pytest.mark.asyncio
async def test_command_job_owned_by_cli_still_runs_in_gateway(tmp_path) -> None:
    # A zero-token command job is deterministic; it must NOT divert even for a
    # kiro-cli owner (nothing to run in the CLI's model session).
    called: list[str] = []
    enqueued: list[str] = []

    async def on_job(job: CronJob) -> None:
        called.append(job.id)

    svc = _svc_with_hooks(tmp_path, enqueued, on_job)
    job = CronJob(
        id="j3", name="poll", message="", command="echo hi", session_key="kiro-cli:sess-1"
    )

    await svc._execute(job)  # noqa: SLF001

    assert enqueued == []
    assert called == ["j3"]


@pytest.mark.asyncio
async def test_inject_failure_falls_through_to_error(tmp_path) -> None:
    async def on_job(job: CronJob) -> None:
        pass

    svc = CronService(base_dir=tmp_path, on_job=on_job, _defer_initial_load=False)

    async def _wake(job: CronJob) -> bool:
        return False  # queue reported not delivered

    svc.set_kiro_cli_message_callback(_wake)
    job = CronJob(id="j4", name="poll", message="check", session_key="kiro-cli:sess-1")

    await svc._execute(job)  # noqa: SLF001

    assert job.last_status == "error"
    assert getattr(job, "cli_injected", False) is False



# -- boot wiring: the divert must be attached on the REAL boot order ----------
#
# P3 (phase-4 proof): ``_init_cron`` runs before ``dashboard_state`` exists, so
# a hook wired only inside ``_init_cron`` is dead on a normal boot and every
# supervised agent-message cron silently ran as an in-gateway LLM turn. The
# wiring now lives in ``_wire_cron_dashboard_hooks``, called again post-dashboard.


def _bare_orchestrator(cron_svc, dashboard_state):
    from types import SimpleNamespace

    return SimpleNamespace(cron_svc=cron_svc, dashboard_state=dashboard_state)


def test_wire_cron_hooks_noop_before_dashboard_exists(tmp_path) -> None:
    """The boot-time call (dashboard not yet up) must attach nothing and not fail."""
    from kiro_crew.slack.gateway import GatewayOrchestrator

    svc = CronService(base_dir=tmp_path, on_job=None, _defer_initial_load=False)
    GatewayOrchestrator._wire_cron_dashboard_hooks(_bare_orchestrator(svc, None))  # type: ignore[arg-type]
    assert getattr(svc, "_on_kiro_cli_message", None) is None
    # Symmetric: a dashboard without a scheduler (``--no-crons`` ordering) is a no-op too.
    GatewayOrchestrator._wire_cron_dashboard_hooks(_bare_orchestrator(None, object()))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_wire_cron_hooks_after_dashboard_diverts_supervised_job(tmp_path) -> None:
    """Post-dashboard wiring attaches both hooks and the divert reaches the wake queue."""
    from types import SimpleNamespace

    from kiro_crew.slack.gateway import GatewayOrchestrator

    called: list[str] = []
    enqueued: list[tuple] = []

    async def on_job(job: CronJob) -> None:
        called.append(job.id)

    class _Queue:
        async def enqueue(self, owner, kind, handle, message):
            enqueued.append((owner, kind, handle, message))
            return "wake-1"

    refreshed: list[str] = []
    dashboard_state = SimpleNamespace(
        push_refresh=lambda what: refreshed.append(what),
        wake_queue=lambda: _Queue(),
    )
    svc = CronService(base_dir=tmp_path, on_job=on_job, _defer_initial_load=False)
    orch = _bare_orchestrator(svc, dashboard_state)

    # Boot order: cron first (no dashboard) ...
    GatewayOrchestrator._wire_cron_dashboard_hooks(_bare_orchestrator(svc, None))  # type: ignore[arg-type]
    assert svc._should_inject_to_cli(  # noqa: SLF001
        CronJob(id="j", name="n", message="m", session_key="kiro-cli:s")
    ) is False
    # ... then the post-dashboard pass attaches the hooks.
    GatewayOrchestrator._wire_cron_dashboard_hooks(orch)  # type: ignore[arg-type]
    assert svc._push_refresh is dashboard_state.push_refresh  # noqa: SLF001

    job = CronJob(id="j3", name="poll", message="check", session_key="kiro-cli:sess-3")
    await svc._execute(job)  # noqa: SLF001

    assert enqueued == [("kiro-cli:sess-3", "cron", "j3", "check")]
    assert called == []
    records, total = await svc._history.get_job_history("j3")  # noqa: SLF001
    assert total == 1 and records[0]["outcome"] == "injected"
