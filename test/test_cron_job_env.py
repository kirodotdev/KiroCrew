"""A script or command cron's child sees the job's own ``env`` block.

``CronJob.env`` already reaches an agent cron's session. The script and command
runners take it as ``job_env`` and add it to the cleaned child environment, so the
cron deny set, the granted secret names and the ``sanitize_spec_env`` strip still win.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_crew.cron_script as cron_script
from kiro_crew.cron_script import run_command_sandboxed, run_script_sandboxed

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX spawn plumbing")


@pytest.fixture(autouse=True)
def _isolated_home(_floor_monkeypatch, tmp_path, named_cron_caller, posix_test_shell):
    monkeypatch = _floor_monkeypatch
    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr("kiro_crew.cron_script.config_dir", lambda: home)
    monkeypatch.setattr("kiro_crew.agent_scratch.config_dir", lambda: home)
    src_dir = str(Path(cron_script.__file__).resolve().parents[1])
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", src_dir + (os.pathsep + existing if existing else ""))
    monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None))
    monkeypatch.setattr("kiro_crew.cron_script._resolve_command_shell", lambda: posix_test_shell)
    return home


def _script(tmp_path: Path, keys: tuple[str, ...]) -> str:
    crons = cron_script.config_dir() / "crons"
    crons.mkdir(parents=True, exist_ok=True)
    path = crons / "env_probe.py"
    path.write_text(
        "import os\n"
        "from kiro_crew.cron_script import Report\n"
        "def run(ctx):\n"
        f"    raise Report('|'.join(repr(os.environ.get(k)) for k in {keys!r}))\n"
    )
    return f"{path}:run"


def _run_script(tmp_path, keys, job_env):
    with patch("pathlib.Path.home", return_value=tmp_path):
        return run_script_sandboxed(_script(tmp_path, keys), "envjob", timeout=30, job_env=job_env)


def test_script_cron_sees_the_job_env(tmp_path):
    result = _run_script(tmp_path, ("K6_ENV",), {"K6_ENV": "1"})
    assert result == {"status": "report", "message": "'1'"}


def test_script_cron_without_a_job_env_is_unchanged(tmp_path):
    result = _run_script(tmp_path, ("K6_ENV",), None)
    assert result["message"] == "None"


def test_command_cron_sees_the_job_env():
    result = run_command_sandboxed(
        'echo "K6_ENV=[${K6_ENV}]"', job_id="envcmd", job_env={"K6_ENV": "1"}
    )
    assert result["status"] == "ok", result
    assert result["output"].strip() == "K6_ENV=[1]"


def test_job_env_beats_the_inherited_value(tmp_path, monkeypatch):
    monkeypatch.setenv("K6_ENV", "parent")
    assert _run_script(tmp_path, ("K6_ENV",), {"K6_ENV": "job"})["message"] == "'job'"


_REFUSED = {
    "KIROCREW_APPROVAL_MODE": "auto",
    "KIROCREW_SECURITY_POLICY": "/tmp/p.json",
    "KIROCREW_ADMISSION_POLICY": "/tmp/a.json",
    "KIROCREW_HOME": "/tmp/job-home",
    "KIROCREW_PROFILE": "job-profile",
    "SLACK_BOT_TOKEN": "xoxb-job",
    "KIROCREW_INTERNAL_SECRET": "job-secret",
    "LD_PRELOAD": "/tmp/x.so",
}


def test_script_cron_never_receives_reserved_or_denied_keys(tmp_path):
    keys = tuple(_REFUSED)
    result = _run_script(tmp_path, keys + ("K6_ENV",), {**_REFUSED, "K6_ENV": "1"})
    values = result["message"].split("|")
    assert values[-1] == "'1'"
    for key, value in zip(keys, values):
        assert repr(_REFUSED[key]) != value, key


def test_command_cron_never_receives_reserved_or_denied_keys():
    keys = " ".join(f"${{{k}-unset}}" for k in _REFUSED)
    result = run_command_sandboxed(f'echo "{keys}"', job_id="envcmd2", job_env=_REFUSED)
    assert result["status"] == "ok", result
    for value in _REFUSED.values():
        assert value not in result["output"]


def test_job_env_cannot_override_the_script_childs_own_identity(tmp_path):
    result = _run_script(tmp_path, ("KIROCREW_SESSION_KEY",), {"KIROCREW_SESSION_KEY": "cron:evil"})
    assert result["message"] == "'cron:envjob'"
