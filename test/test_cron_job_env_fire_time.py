"""A script or command cron's ``env`` values are data, not command text, at fire time.

The runner drops process-behavior and reserved names from the block, and the child runs
in the job's own sandbox tier. ``vet_job_at_fire_time`` does not run the values through the shell composition scan, so ordinary data that reads as shell
(compact JSON, ``$`` before a digit) still fires, as it does for an agent cron.
"""

from __future__ import annotations

import pytest

from kiro_crew import mcp_cron
from kiro_crew.cron import CronJob


@pytest.fixture(autouse=True)
def _gates(_floor_monkeypatch, named_cron_caller):
    monkeypatch = _floor_monkeypatch
    monkeypatch.setattr(mcp_cron, "_vet_cron_capability_governance", lambda **_kw: None)
    monkeypatch.setattr(mcp_cron, "_vet_script_file", lambda _p: None)
    monkeypatch.setattr(mcp_cron, "resolve_script_path", lambda spec, **_k: (spec, "run"))


@pytest.mark.parametrize("value", ['{"a":1,"b":2}', "$5 per run", "~/.aws/config"])
@pytest.mark.parametrize("kind", ["command", "script"])
def test_an_env_value_that_reads_as_shell_still_fires(kind, value):
    body = {"command": "df -h"} if kind == "command" else {"script": "job.py:run"}
    job = CronJob(id="envdata", name="envdata", message="", env={"CFG": value}, **body)
    assert mcp_cron.vet_job_at_fire_time(job) is None
