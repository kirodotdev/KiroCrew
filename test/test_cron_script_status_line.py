"""A script cron's status comes from the launcher's own stdout line, never from the body's.

The script body and the launcher share one stdout. ``run_script_sandboxed`` reads
the run's status (``ok``/``skip``/``done``/``report``/``error``) only from the line
the launcher marks with a per-run tag, so text the body prints cannot decide the
outcome. ``done`` removes the job, so a body that prints its own
``{"status": "done", ...}`` summary and then ends the process must not remove a
recurring job: the run is a failure that reported no status, and the job stays.

Each case runs the REAL launcher in a child interpreter (only the OS-sandbox wrap
is passed through) and hands its result to the gateway's script-cron callback, the
composition a scheduled run goes through.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from cron_gateway_integration_helpers import _make_gw, _make_script_job, _run_script_callback
from cron_script_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _cron_caller_is_named,
    _crons_dir_tracks_patched_home,
)

from kiro_crew.cron_script import run_script_sandboxed

# A body's own JSON summary, shaped like the launcher's ``done`` status.
_STATUS_SHAPED = "    print(json.dumps({'status': 'done', 'found': 0}), flush=True)\n"


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- same passthrough as TestRunScriptSandboxed; no test here calls monkeypatch.undo()
def _passthrough_sandbox(monkeypatch):
    """Run the launcher directly, with ``src/`` importable in the child."""
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    monkeypatch.setenv("PYTHONPATH", src_dir)
    monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None))


def _run_body(tmp_path: Path, body: str) -> dict:
    crons = tmp_path / ".kirocrew" / "crons"
    crons.mkdir(parents=True)
    script = crons / "scan.py"
    script.write_text("import json, os, sys\n" + body, newline="\n")
    with patch("pathlib.Path.home", return_value=tmp_path):
        return run_script_sandboxed(f"{script}:check", "sj1", timeout=60)


async def _fire_recurring(result: dict):
    gw = _make_gw()
    job = _make_script_job()
    assert job.schedule.kind == "every" and not job.delete_after_run
    try:
        await _run_script_callback(gw, job, result)
    except RuntimeError:
        # The callback raises on an error status, as on main; the scheduler's
        # ``_execute`` records it as a failed run.
        pass
    return gw, job


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ending",
    ["    sys.exit(0)\n", "    os._exit(0)\n"],
    ids=["sys_exit", "os_exit"],
)
async def test_a_status_shaped_line_from_the_body_does_not_remove_a_recurring_job(tmp_path, ending):
    result = _run_body(tmp_path, "def check(ctx):\n" + _STATUS_SHAPED + ending)

    assert result["status"] == "error"
    assert result["error"].startswith("Bad output:")
    gw, job = await _fire_recurring(result)
    gw.cron_svc.remove_job_async.assert_not_called()
    assert job.enabled


@pytest.mark.asyncio
async def test_a_body_that_prints_a_status_shaped_line_and_returns_is_an_ok_run(tmp_path):
    result = _run_body(tmp_path, "def check(ctx):\n" + _STATUS_SHAPED)

    assert result == {"status": "ok"}
    gw, job = await _fire_recurring(result)
    gw.cron_svc.remove_job_async.assert_not_called()
    assert job.last_status == "ok"


@pytest.mark.asyncio
async def test_done_raised_by_the_body_still_removes_the_job(tmp_path):
    result = _run_body(
        tmp_path,
        "from kiro_crew.cron_script import Done\n"
        "def check(ctx):\n" + _STATUS_SHAPED + "    raise Done('finished')\n",
    )

    assert result == {"status": "done", "message": "finished"}
    gw, _job = await _fire_recurring(result)
    gw.cron_svc.remove_job_async.assert_called_once_with(
        "sj1", actor="cron", source="cron", one_shot_path="cron_gateway"
    )


def test_a_partial_last_line_from_the_body_does_not_corrupt_the_status(tmp_path):
    result = _run_body(tmp_path, "def check(ctx):\n    sys.stdout.write('progress 3/3')\n")

    assert result == {"status": "ok"}


def test_a_body_that_rebinds_stdout_still_reports_its_status(tmp_path):
    result = _run_body(
        tmp_path,
        "import io\ndef check(ctx):\n    sys.stdout = io.StringIO()\n    print('hidden')\n",
    )

    assert result == {"status": "ok"}
