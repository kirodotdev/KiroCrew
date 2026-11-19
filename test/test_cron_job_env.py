"""A script or command cron's child sees the job's own ``env`` block.

``CronJob.env`` already reaches an agent cron's session. The script and command
runners take it as ``job_env`` and add it to the cleaned child environment, so the
cron deny set, the granted secret names and the ``sanitize_spec_env`` strip still win.

One real script child and one real command child prove the delivery end to end; the
filtering cases read the environment the runner hands the spawn, so they start no
interpreter.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_crew.cron_script as cron_script
from kiro_crew.cron_script import (
    compute_secret_env_pin,
    run_command_sandboxed,
    run_script_sandboxed,
)
from kiro_crew.secrets import SecretVault

pytestmark = [
    pytest.mark.skipif(os.name == "nt", reason="POSIX spawn plumbing"),
    pytest.mark.xdist_group("cron_job_env"),
]

# Half the suite's 120 s per-test timeout, so a stalled child fails by name.
_CHILD_TIMEOUT = 60


@pytest.fixture(autouse=True)
def _isolated_home(_floor_monkeypatch, tmp_path, named_cron_caller, posix_test_shell):
    monkeypatch = _floor_monkeypatch
    home = tmp_path / "crew-home"
    home.mkdir()
    # The absence asserts must not depend on what the host exports.
    monkeypatch.delenv("K6_ENV", raising=False)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr("kiro_crew.cron_script.config_dir", lambda: home)
    monkeypatch.setattr("kiro_crew.agent_scratch.config_dir", lambda: home)
    src_dir = str(Path(cron_script.__file__).resolve().parents[1])
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", src_dir + (os.pathsep + existing if existing else ""))
    monkeypatch.setattr("kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None))
    monkeypatch.setattr("kiro_crew.cron_script._resolve_command_shell", lambda: posix_test_shell)
    return home


def _script(body: str) -> str:
    crons = cron_script.config_dir() / "crons"
    crons.mkdir(parents=True, exist_ok=True)
    path = crons / "env_probe.py"
    path.write_text("import os\nfrom kiro_crew.cron_script import Report\n" + body)
    return f"{path}:run"


@pytest.fixture
def spawn_env(monkeypatch):
    """The ``env`` the runner hands the spawn, with no child started."""
    seen: list[dict[str, str]] = []

    def _record(argv, **kwargs):
        seen.append(kwargs["env"])
        raise OSError("no child in this test")

    monkeypatch.setattr(cron_script, "popen_limited", _record)

    def _run(runner, *args, **kwargs):
        try:
            runner(*args, **kwargs)
        except OSError:
            pass
        assert len(seen) == 1
        return seen[0]

    return _run


def test_script_child_sees_the_job_env(tmp_path, monkeypatch):
    monkeypatch.setenv("K6_ENV", "parent")
    spec = _script(
        "def run(ctx):\n"
        "    keys = ('K6_ENV', 'K6_ONLY', 'KIROCREW_SESSION_KEY')\n"
        "    raise Report('|'.join(repr(os.environ.get(k)) for k in keys))\n"
    )
    job_env = {"K6_ENV": "1", "K6_ONLY": "x", "KIROCREW_SESSION_KEY": "cron:evil"}
    with patch("pathlib.Path.home", return_value=tmp_path):
        result = run_script_sandboxed(  # flake-ok: child deadline at half the 120 s test timeout
            spec, "envjob", timeout=_CHILD_TIMEOUT, job_env=job_env
        )
    # The job value beats the inherited one; the runner's own identity beats the job's.
    assert result == {"status": "report", "message": "'1'|'x'|'cron:envjob'"}


def test_command_child_sees_the_job_env():
    result = run_command_sandboxed(
        'echo "K6_ENV=[${K6_ENV}]"',
        timeout=_CHILD_TIMEOUT,  # flake-ok: child deadline at half the 120 s test timeout
        job_id="envcmd",
        job_env={"K6_ENV": "1"},
    )
    assert result["status"] == "ok", result
    assert result["output"].strip() == "K6_ENV=[1]"


_REFUSED = {
    "KIROCREW_APPROVAL_MODE": "auto",
    "KIROCREW_SECURITY_POLICY": "/tmp/p.json",
    "KIROCREW_ADMISSION_POLICY": "/tmp/a.json",
    "KIROCREW_HOME": "/tmp/job-home",
    "KIROCREW_PROFILE": "job-profile",
    "KIROCREW_INTERNAL_SECRET": "job-secret",
    "SLACK_BOT_TOKEN": "xoxb-job",
    "slack_bot_token": "xoxb-job-lower",
    "LD_PRELOAD": "/tmp/x.so",
    "HOME": "/tmp/job-home-dir",
    "home": "/tmp/job-home-lower",
    "BASH_ENV": "/tmp/job-bashrc",
    "PYTHONPATH": "/tmp/job-pypath",
    "BASH_FUNC_echo%%": "() { :; }",
    "PATH": "/x:$PATH",
    "SHELLOPTS": "xtrace",
    "PS4": "$(cat ~/.ssh/id_ed25519)",
    "BASHOPTS": "extglob",
    "PROMPT_COMMAND": "id",
    "GIT_SSH_COMMAND": "cat ~/.ssh/id_ed25519; exit 1",
    "NODE_OPTIONS": "--require /tmp/x.js",
    "NODE_PATH": "/tmp/node",
    "pager": "/tmp/pager",
    "_KIROCREW_DIAL_PORT": "1",
    "_KIROCREW_CRON_SCRIPT_CHILD": "job",
}


@pytest.mark.parametrize("kind", ["script", "command"])
def test_reserved_and_denied_keys_never_reach_the_child(kind, spawn_env, tmp_path):
    job_env = {**_REFUSED, "K6_ENV": "1"}
    if kind == "command":
        env = spawn_env(run_command_sandboxed, "true", job_id="envcmd2", job_env=job_env)
    else:
        spec = _script("def run(ctx):\n    pass\n")
        with patch("pathlib.Path.home", return_value=tmp_path):
            env = spawn_env(run_script_sandboxed, spec, "envjob2", job_env=job_env)
    assert env["K6_ENV"] == "1"
    for key, value in _REFUSED.items():
        assert env.get(key) != value, key


def test_no_job_env_leaves_the_child_env_unchanged(spawn_env):
    env = spawn_env(run_command_sandboxed, "true", job_id="envcmd3")
    assert "K6_ENV" not in env


def test_a_secret_granted_script_gets_no_job_env(spawn_env, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.cron_script.wrap_argv",
        lambda argv, **k: ([argv[0], "-X", "utf8", *argv[1:]], None),
    )
    SecretVault(cron_script.config_dir()).set_sync("slack-sandbox", "xoxb-script")
    spec = _script("def run(ctx):\n    pass\n")
    grant = {"MY_SANDBOX_TOKEN": "slack-sandbox"}
    with patch("pathlib.Path.home", return_value=tmp_path):
        pin = compute_secret_env_pin(spec, "", job_id="grantjob", grant=grant)
        env = spawn_env(
            run_script_sandboxed,
            spec,
            "grantjob",
            secret_env=grant,
            secret_env_pin=pin,
            job_env={"K6_ENV": "1"},
        )
    assert "K6_ENV" not in env
