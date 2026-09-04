"""Tests for ``container.supervisor.backend`` -- launch argv/env and readiness.

Readiness is exercised against a fake backend process that binds a real
loopback port and writes a real secret file. No Kiro Crew gateway is booted.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from container import common
from container.common import Settings
from container.supervisor import backend as backend_mod
from container.supervisor.backend import (
    BackendExited,
    BackendReadyTimeout,
    build_backend_argv,
    build_backend_env,
    require_api_key,
    start_backend,
    wait_until_ready,
)

from .test_supervisor_fakes import fake_argv, free_port, write_fake_backend


def make_settings(tmp_path: Path, port: int) -> Settings:
    data_home = tmp_path / "data"
    return Settings(
        backend_port=port,
        backend_run_dir=data_home / "run",
        front_port=8080,
        route_prefix="",
        control_secret=None,
        data_home=data_home,
        config_dir=data_home / "config",
        crew_name="test-crew",
        backup_bucket=None,
        backup_prefix="",
        backup_interval_secs=30,
    )


# --- argv ------------------------------------------------------------------


def test_argv_runs_dashboard_mode_never_no_dashboard(tmp_path):
    argv = build_backend_argv(make_settings(tmp_path, 8765))
    # Dashboard mode is required. --slack-only is the real flag that removes it
    # (cli.py:539); --no-dashboard is not even a real flag. Pass NEITHER.
    assert backend_mod.FLAG_SLACK_ONLY not in argv
    assert backend_mod.FLAG_NO_DASHBOARD not in argv
    assert backend_mod.GATEWAY_SUBCOMMAND in argv


def test_argv_arms_no_crons(tmp_path):
    # Arming the scheduler fires overdue jobs immediately on boot.
    assert backend_mod.FLAG_NO_CRONS in build_backend_argv(make_settings(tmp_path, 8765))


def test_argv_sets_approval_yolo(tmp_path):
    # Unattended crew: no one is there to answer a tool-approval prompt.
    argv = build_backend_argv(make_settings(tmp_path, 8765))
    assert backend_mod.FLAG_APPROVAL in argv
    i = argv.index(backend_mod.FLAG_APPROVAL)
    assert argv[i + 1] == backend_mod.APPROVAL_MODE == "yolo"


# --- credential ------------------------------------------------------------


def test_env_forwards_the_model_credential(tmp_path):
    env = build_backend_env(make_settings(tmp_path, 9001), base={"KIRO_API_KEY": "sk-test"})
    assert env[backend_mod.ENV_KIRO_API_KEY] == "sk-test"


def test_require_api_key_raises_when_absent_or_empty(tmp_path):
    with pytest.raises(common.ConfigError, match="KIRO_API_KEY"):
        require_api_key({})
    with pytest.raises(common.ConfigError, match="KIRO_API_KEY"):
        require_api_key({"KIRO_API_KEY": "   "})


def test_require_api_key_passes_when_present(tmp_path):
    require_api_key({"KIRO_API_KEY": "sk-live"})  # must not raise


# --- env -------------------------------------------------------------------


def test_env_points_at_shared_home_and_loopback_port(tmp_path):
    s = make_settings(tmp_path, 9001)
    env = build_backend_env(s, base={})
    assert env[backend_mod.ENV_HOME] == str(s.data_home)
    assert env[backend_mod.ENV_PORT] == "9001"
    # The bind address is pinned to loopback (dashboard/urls.py:208 reads this).
    assert env[backend_mod.ENV_BIND] == common.BACKEND_HOST == "127.0.0.1"


def test_env_pins_loopback_over_an_inherited_bind_all(tmp_path):
    # The official image sets KIROCREW_BIND=0.0.0.0; the backend must never be
    # network-reachable, so our env must override that back to loopback.
    env = build_backend_env(make_settings(tmp_path, 9001), base={"KIROCREW_BIND": "0.0.0.0"})
    assert env[backend_mod.ENV_BIND] == "127.0.0.1"


def test_env_disables_the_beacon(tmp_path):
    # KIROCREW_TELEMETRY_DISABLED (truthy) is the opt-out beacon.py actually reads.
    env = build_backend_env(make_settings(tmp_path, 9001), base={})
    assert env[backend_mod.ENV_TELEMETRY_DISABLED].strip().lower() in {"1", "true", "yes", "on"}


def test_env_strips_leaked_channel_credentials(tmp_path):
    base = {
        "TELEGRAM_BOT_TOKEN": "leaked",
        "SLACK_BOT_TOKEN": "leaked",
        "KEEP_ME": "yes",
    }
    env = build_backend_env(make_settings(tmp_path, 9001), base=base)
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert "SLACK_BOT_TOKEN" not in env
    assert env["KEEP_ME"] == "yes"  # unrelated env is preserved


# --- readiness -------------------------------------------------------------


def test_wait_until_ready_returns_when_port_and_secret_are_both_up(tmp_path):
    port = free_port()
    s = make_settings(tmp_path, port)
    script = write_fake_backend(tmp_path)
    pg = start_backend(
        s,
        argv=fake_argv(
            script,
            port=port,
            run_dir=str(s.backend_run_dir),
            secret_delay=0.4,
            ttl=30,
        ),
    )
    try:
        wait_until_ready(s, timeout=10.0, process=pg, poll_interval=0.05)
        # The secret the fake wrote is exactly what common reads back.
        assert common.read_boot_secret(s.backend_run_dir, port) == "fake-boot-secret"
    finally:
        pg.terminate(2.0)


def test_wait_until_ready_times_out_when_secret_present_but_port_closed(tmp_path):
    # A present secret alone is NOT ready. Pre-write the secret, start nothing.
    port = free_port()
    s = make_settings(tmp_path, port)
    s.backend_run_dir.mkdir(parents=True)
    common.secret_path(s.backend_run_dir, port).write_text("stale-from-a-prior-boot")

    with pytest.raises(BackendReadyTimeout) as exc:
        wait_until_ready(s, timeout=0.6, poll_interval=0.05)
    assert "secret_present=True" in str(exc.value)
    assert "port_open=False" in str(exc.value)


def test_wait_until_ready_times_out_when_port_open_but_no_secret(tmp_path):
    # A live port with no secret is NOT ready either.
    port = free_port()
    s = make_settings(tmp_path, port)
    script = write_fake_backend(tmp_path)
    # Fake binds the port but never writes a secret (no run-dir given).
    pg = start_backend(s, argv=fake_argv(script, port=port, ttl=30))
    try:
        with pytest.raises(BackendReadyTimeout) as exc:
            wait_until_ready(s, timeout=1.0, process=pg, poll_interval=0.05)
        assert "port_open=True" in str(exc.value)
        assert "secret_present=False" in str(exc.value)
    finally:
        pg.terminate(2.0)


def test_wait_until_ready_reports_backend_exit_without_waiting_out_timeout(tmp_path):
    port = free_port()
    s = make_settings(tmp_path, port)
    script = write_fake_backend(tmp_path)
    # ttl=0.2 so the fake exits almost immediately, never opening long.
    pg = start_backend(s, argv=fake_argv(script, ttl=0.2))
    t0 = time.monotonic()
    with pytest.raises(BackendExited):
        wait_until_ready(s, timeout=30.0, process=pg, poll_interval=0.05)
    assert time.monotonic() - t0 < 5.0, "should fail fast on exit, not wait 30s"


def test_start_backend_creates_the_run_directory(tmp_path):
    port = free_port()
    s = make_settings(tmp_path, port)
    assert not s.backend_run_dir.exists()
    script = write_fake_backend(tmp_path)
    pg = start_backend(s, argv=fake_argv(script, ttl=0.5))
    try:
        assert s.backend_run_dir.exists()
    finally:
        pg.terminate(2.0)
