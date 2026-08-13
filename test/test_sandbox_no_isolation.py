"""Tests for the SEC-009 no-isolation fallback warning in sandbox.wrap_argv.

When no OS-level sandbox backend is available, ``wrap_argv`` must NOT silently
fall back to running the agent subprocess unprotected. It must surface a loud
SECURITY warning unless the operator has explicitly opted in via
``agent.sandbox_allow_no_isolation`` (in which case it is logged at info level).

When mode='off' is explicitly configured, ``wrap_argv`` emits a SECURITY warning
about both isolation layers being inactive (Fix #3 of the insecure-defaults audit).
"""

from __future__ import annotations

import logging

import kiro_crew.sandbox as sb


def _reset_warned():
    # wrap_argv caches a one-shot "_warned" flag on the function object.
    if hasattr(sb.wrap_argv, "_warned"):
        delattr(sb.wrap_argv, "_warned")
    # The mode-off warning has its own per-branch latch.
    if hasattr(sb._warn_mode_off_unconfined, "_warned_set"):
        delattr(sb._warn_mode_off_unconfined, "_warned_set")
    if hasattr(sb._warn_mode_off_unconfined, "_info_logged"):
        delattr(sb._warn_mode_off_unconfined, "_info_logged")


def _neutralize_passthrough(monkeypatch):
    """Prevent the 'already inside a sandbox' passthrough from short-circuiting."""
    monkeypatch.setattr(sb, "_inside_kirocrew_sandbox", lambda: False)
    monkeypatch.setattr(sb, "_macos_sandbox_state", lambda: None)
    # Neutralize cgroup scope so it doesn't prepend systemd-run
    monkeypatch.setattr(sb, "_probe_cgroup_scope", lambda: (False, "disabled-in-test"))


def test_no_backend_emits_security_warning(monkeypatch, caplog):
    """Default (not opted in): falling back to no isolation logs a WARNING."""
    _reset_warned()
    _neutralize_passthrough(monkeypatch)
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
    _neutralize_passthrough(monkeypatch)
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
    _neutralize_passthrough(monkeypatch)
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "none")
    monkeypatch.setattr(sb, "_allow_no_isolation", lambda: False)
    monkeypatch.setattr(sb, "_allow_unsandboxed_exec", lambda: True)

    with caplog.at_level(logging.WARNING, logger=sb.logger.name):
        sb.wrap_argv(["echo", "1"], mode="standard")
        sb.wrap_argv(["echo", "2"], mode="standard")

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_mode_off_emits_security_warning(monkeypatch, caplog):
    """mode='off' now emits a SECURITY warning when kiro-cli's internal sandbox
    is NOT active (Fix #3: both layers inactive = loud degradation signal)."""
    _reset_warned()
    _neutralize_passthrough(monkeypatch)
    monkeypatch.setattr(sb, "kiro_internal_sandbox_enabled", lambda: False)

    with caplog.at_level(logging.WARNING, logger=sb.logger.name):
        argv, cleanup = sb.wrap_argv(["echo", "hi"], mode="off")

    assert argv == ["echo", "hi"]
    assert cleanup is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a SECURITY warning when mode=off with no delegation"
    msg = warnings[0].getMessage()
    assert "sandbox='off'" in msg or "both" in msg.lower() or "no OS-level" in msg.lower()


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
    _neutralize_passthrough(monkeypatch)
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "none")
    monkeypatch.setattr(sb, "_allow_no_isolation", lambda: True)
    monkeypatch.setattr(sb, "_allow_unsandboxed_exec", lambda: True)

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
    _neutralize_passthrough(monkeypatch)
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "none")
    monkeypatch.setattr(sb, "_allow_no_isolation", lambda: True)
    monkeypatch.setattr(sb, "_allow_unsandboxed_exec", lambda: True)

    base = {"PATH": "/usr/bin", "PYTHONPATH": "/kirocrew/site"}
    _, env, _ = sb.sandboxed_spawn_argv(["python", "-m", "x"], env=base)
    assert env["PYTHONPATH"] == "/kirocrew/site"
