"""Tests for the SEC-009 no-isolation fallback warning in sandbox.wrap_argv.

When no OS-level sandbox backend is available, ``wrap_argv`` must NOT silently
fall back to running the agent subprocess unprotected. It must surface a loud
SECURITY warning unless the operator has explicitly opted in via
``agent.sandbox_allow_no_isolation`` (in which case it is logged at info level).
"""

from __future__ import annotations

import logging

import kiro_crew.sandbox as sb


def _reset_warned():
    # wrap_argv caches a one-shot "_warned" flag on the function object.
    if hasattr(sb.wrap_argv, "_warned"):
        delattr(sb.wrap_argv, "_warned")


def test_no_backend_emits_security_warning(monkeypatch, caplog):
    """Default (not opted in): falling back to no isolation logs a WARNING."""
    _reset_warned()
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "none")
    monkeypatch.setattr(sb, "_allow_no_isolation", lambda: False)
    monkeypatch.setattr(sb, "_allow_unsandboxed_exec", lambda: True)

    with caplog.at_level(logging.WARNING, logger=sb.logger.name):
        argv, cleanup = sb.wrap_argv(["echo", "hi"], mode="standard")

    # Behavior is graceful: command still runs, no sandbox wrapper, no cleanup.
    assert argv == ["echo", "hi"]
    assert cleanup is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a SECURITY warning when no sandbox backend is available"
    assert "WITHOUT credential isolation" in warnings[0].getMessage()


def test_no_backend_opted_in_demotes_to_info(monkeypatch, caplog):
    """When the operator opts in, the fallback is logged at info, not warning."""
    _reset_warned()
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "none")
    monkeypatch.setattr(sb, "_allow_no_isolation", lambda: True)
    monkeypatch.setattr(sb, "_allow_unsandboxed_exec", lambda: True)

    with caplog.at_level(logging.INFO, logger=sb.logger.name):
        sb.wrap_argv(["echo", "hi"], mode="standard")

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("opted in" in r.getMessage() for r in infos)


def test_warning_emitted_once_per_process(monkeypatch, caplog):
    """The warning is one-shot — repeated calls do not spam the log."""
    _reset_warned()
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "none")
    monkeypatch.setattr(sb, "_allow_no_isolation", lambda: False)
    monkeypatch.setattr(sb, "_allow_unsandboxed_exec", lambda: True)

    with caplog.at_level(logging.WARNING, logger=sb.logger.name):
        sb.wrap_argv(["echo", "1"], mode="standard")
        sb.wrap_argv(["echo", "2"], mode="standard")

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_mode_off_does_not_warn(monkeypatch, caplog):
    """mode='off' is an explicit operator choice — it returns early, no warning."""
    _reset_warned()
    monkeypatch.setattr(sb, "_allow_no_isolation", lambda: False)

    with caplog.at_level(logging.WARNING, logger=sb.logger.name):
        argv, cleanup = sb.wrap_argv(["echo", "hi"], mode="off")

    assert argv == ["echo", "hi"]
    assert cleanup is None
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_scrub_env_drops_credential_keys():
    """scrub_env removes AWS/SSH/Slack-token keys, keeps benign ones."""
    env = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "AWS_SECRET_ACCESS_KEY": "sk",
        "AWS_SESSION_TOKEN": "st",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "SLACK_BOT_TOKEN": "xoxb-1",
        "KIROCREW_OWNER_ID": "U123",
    }
    out = sb.scrub_env(env)
    assert out == {"PATH": "/usr/bin", "HOME": "/home/x"}


def test_scrub_env_extra_prefixes_strips_python_env():
    """extra_prefixes drops PYTHONPATH/PYTHONHOME on top of the credential set."""
    env = {"PATH": "/usr/bin", "PYTHONPATH": "/site", "PYTHONHOME": "/py"}
    out = sb.scrub_env(env, extra_prefixes=sb._PYTHON_ENV_PREFIXES)
    assert out == {"PATH": "/usr/bin"}


def test_strip_python_env_holds_on_fail_open_path(monkeypatch):
    """On the opted-in no-backend path wrap_argv returns argv unmodified (no
    launcher strips PYTHONPATH), so sandboxed_spawn_argv MUST strip the Python
    env vars from the returned env itself (review-bot finding on security-review 92e24570)."""
    _reset_warned()
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "none")
    monkeypatch.setattr(sb, "_allow_no_isolation", lambda: True)
    monkeypatch.setattr(sb, "_allow_unsandboxed_exec", lambda: True)
    # This test asserts the bare fail-open argv (the PYTHONPATH-strip is via the
    # parent-level scrub, not a launcher). Neutralize the cgroup v2 scope probe
    # so a host WITH systemd cgroup delegation doesn't prepend `systemd-run` and
    # break the argv assertion — cgroup wrapping itself is covered by
    # test_sandbox_argv.py.
    monkeypatch.setattr(sb, "_probe_cgroup_scope", lambda: (False, "disabled-in-test"))

    base = {"PATH": "/usr/bin", "PYTHONPATH": "/kirocrew/site", "PYTHONHOME": "/py"}
    argv, env, cleanup = sb.sandboxed_spawn_argv(
        ["mcp-server"], mode="standard", env=base, strip_python_env=True
    )
    # Fail-open: no wrapper, no launcher, no cleanup.
    assert argv == ["mcp-server"]
    assert cleanup is None
    # ...but the Python-env guarantee still holds via the parent-level scrub.
    assert "PYTHONPATH" not in env
    assert "PYTHONHOME" not in env
    assert env["PATH"] == "/usr/bin"


def test_strip_python_env_false_keeps_python_env(monkeypatch):
    """Without strip_python_env, the chokepoint leaves PYTHONPATH intact (our own
    sandboxed Python children import kiro_crew via it)."""
    _reset_warned()
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "none")
    monkeypatch.setattr(sb, "_allow_no_isolation", lambda: True)
    monkeypatch.setattr(sb, "_allow_unsandboxed_exec", lambda: True)

    base = {"PATH": "/usr/bin", "PYTHONPATH": "/kirocrew/site"}
    _, env, _ = sb.sandboxed_spawn_argv(["python", "-m", "x"], env=base)
    assert env["PYTHONPATH"] == "/kirocrew/site"
