"""A cron tool whose change already happened answers with it, even when its audit row fails.

``cron_add``, ``cron_update``, ``cron_trigger`` and ``cron_secret_request``
write their ``log_api_access`` row AFTER the cron store has changed. When the
server process cannot open the security event log (``sel()`` raises, as in a
child the credential mask keeps from ``trust/``), that error must not become
the call's answer: the model would be told a call that took effect had failed
and retry it -- a second job, a second run, a repeated update.

Driven through ``_call_tool_inner`` so the shared invocation wrapper and its own
row are not in play: the rows under test are the handlers' own.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew import mcp_cron
from kiro_crew.cron import CronService
from kiro_crew.mcp_caller import CallerContext, set_current_caller
from kiro_crew.mcp_cron import _call_tool_inner

_KEY = "dashboard:audit-after-change"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    monkeypatch.setattr("kiro_crew.mcp_cron.config_dir", lambda: tmp_path)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
    monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
    monkeypatch.delenv("KIROCREW_CLI", raising=False)
    set_current_caller(CallerContext(session_key=_KEY, session_type="dashboard"))
    yield tmp_path
    set_current_caller(None)


def _unwritable_sel(monkeypatch) -> None:
    """``sel()`` fails the way it does under the credential mask."""

    def _boom():
        raise FileExistsError(17, "File exists", "<crew-home>/trust")

    monkeypatch.setattr(mcp_cron, "sel", _boom)


def _add_job(name: str) -> str:
    result = _call_tool_inner("cron_add", {"name": name, "message": "go", "every": 120})
    assert "Added job" in result, result
    return result.split()[2]


def _answer(name: str, args: dict) -> str:
    """The call's answer, or what escaped it: an escaped audit error is a wrong answer."""
    try:
        return _call_tool_inner(name, args)
    except Exception as exc:  # noqa: BLE001 - the escape IS the defect under test
        return f"raised {type(exc).__name__}: {exc}"


def _store(tmp_path) -> CronService:
    return CronService(base_dir=tmp_path)


def _assert_lost_row_logged(caplog, operation: str) -> None:
    lost = [
        r for r in caplog.records if r.name == "kiro_crew.mcp_cron" and operation in r.getMessage()
    ]
    assert lost, f"no warning names the lost {operation} row"
    assert lost[0].levelno == logging.WARNING
    assert lost[0].exc_info is not None


def test_cron_add_answers_the_job_it_created(monkeypatch, caplog, tmp_path) -> None:
    _unwritable_sel(monkeypatch)
    caplog.set_level(logging.WARNING, logger="kiro_crew.mcp_cron")

    result = _answer("cron_add", {"name": "nightly", "message": "go", "every": 120})

    assert result.startswith("Added job:"), result
    assert "File exists" not in result
    job_id = result.split()[2]
    assert _store(tmp_path).get_job(job_id) is not None
    _assert_lost_row_logged(caplog, "cron.create")


def test_cron_update_answers_the_update_it_made(monkeypatch, caplog, tmp_path) -> None:
    job_id = _add_job("nightly")
    _unwritable_sel(monkeypatch)
    caplog.set_level(logging.WARNING, logger="kiro_crew.mcp_cron")

    result = _answer("cron_update", {"job_id": job_id, "message": "changed"})

    assert result.startswith("Updated job"), result
    job = _store(tmp_path).get_job(job_id)
    assert job is not None and job.message == "changed"
    _assert_lost_row_logged(caplog, "cron.update")


def test_cron_trigger_answers_the_run_it_started(monkeypatch, caplog) -> None:
    job_id = _add_job("nightly")
    fired: list[str] = []

    def _fire(jid, _port, _secret_path):
        fired.append(jid)
        return True, f"Triggered job {jid}"

    monkeypatch.setattr(mcp_cron, "trigger_cron_job", _fire)
    monkeypatch.setattr(mcp_cron, "resolve_serving_port", lambda: 5476)
    _unwritable_sel(monkeypatch)
    caplog.set_level(logging.WARNING, logger="kiro_crew.mcp_cron")

    result = _answer("cron_trigger", {"job_id": job_id})

    assert result == f"Triggered job {job_id} - executing now.", result
    assert fired == [job_id]
    _assert_lost_row_logged(caplog, "cron.trigger")


def test_a_failed_trigger_still_answers_its_own_error(monkeypatch) -> None:
    """The guard keeps the trigger's OWN outcome; it does not turn a failure into success."""
    job_id = _add_job("nightly")
    monkeypatch.setattr(mcp_cron, "trigger_cron_job", lambda *_a: (False, "gateway unreachable"))
    monkeypatch.setattr(mcp_cron, "resolve_serving_port", lambda: 5476)
    _unwritable_sel(monkeypatch)

    assert _answer("cron_trigger", {"job_id": job_id}) == "Error: gateway unreachable"


def test_cron_secret_request_answers_the_pending_request_it_recorded(
    monkeypatch, caplog, tmp_path
) -> None:
    svc = _store(tmp_path)
    job = svc.add_job("j", "args", every_secs=3600, script="print('hi')\n", session_key=_KEY)
    # The pin is computed over the script file; it is not what this test is about.
    monkeypatch.setattr(mcp_cron, "compute_secret_env_pin", lambda *_a, **_k: "pin")
    _unwritable_sel(monkeypatch)
    caplog.set_level(logging.WARNING, logger="kiro_crew.mcp_cron")

    result = _answer(
        "cron_secret_request", {"job_id": job.id, "secrets": {"MY_TOKEN": "slack-sandbox"}}
    )

    assert "PENDING" in result, result
    reloaded = _store(tmp_path).get_job(job.id)
    assert reloaded is not None and reloaded.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}
    _assert_lost_row_logged(caplog, "cron.secret_request")


def test_a_written_row_is_unchanged(monkeypatch) -> None:
    """With a working SEL the post-change row is still written, field for field."""
    rows: list[dict] = []

    class _Recorder:
        def log_api_access(self, **fields):
            rows.append(fields)

        def __getattr__(self, _name):
            return lambda *_a, **_k: None

    monkeypatch.setattr(mcp_cron, "sel", lambda: _Recorder())
    job_id = _add_job("nightly")

    assert {
        "caller": "mcp",
        "operation": "cron.create",
        "outcome": "allowed",
        "source": "mcp",
        "resources": f"job_id={job_id}",
    } in rows
