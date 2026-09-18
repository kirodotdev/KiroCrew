"""Tests for ``container.supervisor.__main__`` -- the startup ORDER and the drained
teardown order.

The backup subsystem was extracted from this PR, so there is no restore phase and no
sidecar: the supervisor gates the environment, installs the bundle, starts the backend,
waits for readiness, starts the front, and drains front then backend at teardown. The seams
are stubbed; the point here is the ordering guarantees, proven by the recorded call sequence.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest
from container.common import ConfigError, Settings
from container.supervisor import __main__ as entry
from container.supervisor import backend as backend_mod
from container.supervisor.backend import require_api_key as _real_require_api_key
from container.supervisor.bundle import install_bundle as _real_install_bundle


def make_settings(tmp_path: Path, *, bucket: str | None = "smc-test-bucket") -> Settings:
    """Settings for the supervise phase.

    ``bucket`` still exists because the front's on-demand transcript fetch reads it; it no
    longer gates a sidecar (there is none). It defaults to a value so the transcript-read
    configuration is exercised; pass ``bucket=None`` for the no-bucket case.
    """
    data_home = tmp_path / "data"
    return Settings(
        backend_port=8765,
        backend_run_dir=data_home / "run",
        front_port=8080,
        route_prefix="",
        control_secret=None,
        data_home=data_home,
        config_dir=data_home,  # Kiro Crew keeps everything under one root
        crew_name="test-crew",
        backup_bucket=bucket,
        backup_prefix="crews/",
    )


class FakePG:
    def __init__(self, name, events):
        self.name = name
        self._events = events
        # _teardown excludes the known children from its orphan sweep by pid, so the double
        # has to answer that too. A real pid, this process's own, because the sweep compares
        # against live /proc entries and an invented number could collide with a real process.
        self.pid = os.getpid()

    def poll(self):
        return None

    def returncode(self):
        return None

    def terminate(self, drain_timeout, poll_interval=0.05):
        self._events.append(f"term:{self.name}")
        return 0


@pytest.fixture
def wired(monkeypatch):
    """Stub every seam and record the order calls happen in."""
    events: list[str] = []

    def fake_start_backend(settings, *, env=None, **kw):
        events.append("start_backend")
        return FakePG("backend", events)

    def fake_wait_ready(settings, timeout, *, process=None, poll_interval=0.25):
        events.append("wait_ready")

    def fake_start_front(settings):
        events.append("start_front")
        return FakePG("front", events)

    monkeypatch.setattr(backend_mod, "start_backend", fake_start_backend)
    monkeypatch.setattr(backend_mod, "wait_until_ready", fake_wait_ready)
    monkeypatch.setattr(entry, "_start_front", fake_start_front)
    # Neutralise the environment gates for the ORDERING tests; each has its own
    # dedicated test below.
    monkeypatch.setattr(backend_mod, "build_backend_env", lambda settings: {})
    monkeypatch.setattr(backend_mod, "require_api_key", lambda env: None)
    monkeypatch.setattr(entry, "verify_sandbox", lambda settings, **kw: None)
    monkeypatch.setattr(entry.bundle_mod, "install_bundle", lambda settings, **kw: None)
    return events


def test_backend_is_ready_before_the_front_starts(wired, tmp_path):
    entry.run(make_settings(tmp_path), wait_for_shutdown=lambda children: "signal")
    assert wired.index("wait_ready") < wired.index("start_front")


def test_full_happy_path_order(wired, tmp_path):
    entry.run(make_settings(tmp_path), wait_for_shutdown=lambda children: "signal")
    startup = [e for e in wired if not e.startswith("term:")]
    assert startup == [
        "start_backend",
        "wait_ready",
        "start_front",
    ]


def test_teardown_drains_front_then_backend(wired, tmp_path):
    entry.run(make_settings(tmp_path), wait_for_shutdown=lambda children: "signal")
    teardown = [e for e in wired if e.startswith("term:")]
    assert teardown == ["term:front", "term:backend"]


def test_readiness_failure_tears_down_backend_and_never_starts_the_front(
    wired, tmp_path, monkeypatch
):
    def not_ready(settings, timeout, *, process=None, poll_interval=0.25):
        wired.append("wait_ready")
        raise backend_mod.BackendReadyTimeout("nope")

    monkeypatch.setattr(backend_mod, "wait_until_ready", not_ready)
    with pytest.raises(backend_mod.BackendReadyTimeout):
        entry.run(make_settings(tmp_path), wait_for_shutdown=lambda c: "signal")

    assert "start_front" not in wired
    # The backend we started is drained rather than orphaned.
    assert "term:backend" in wired


def test_teardown_runs_even_if_supervise_raises(wired, tmp_path):
    def blow_up(children):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        entry.run(make_settings(tmp_path), wait_for_shutdown=blow_up)
    # Both were still drained.
    assert {"term:front", "term:backend"} <= set(wired)


# --- verify_layout (the Dockerfile open item) ------------------------------


def test_verify_layout_accepts_the_single_root_layout(tmp_path):
    entry.verify_layout(make_settings(tmp_path))  # must not raise


def test_verify_layout_rejects_a_config_subdir(tmp_path):
    # SMC_CONFIG_DIR=<home>/config is where common defaults it today, but the
    # backend writes open_slots.json / session_map.json at the home ROOT.
    s = make_settings(tmp_path)
    bad = dataclasses.replace(s, config_dir=s.data_home / "config")
    with pytest.raises(ConfigError, match="SMC_CONFIG_DIR"):
        entry.verify_layout(bad)


def test_verify_layout_rejects_a_stray_run_dir(tmp_path):
    s = make_settings(tmp_path)
    bad = dataclasses.replace(s, backend_run_dir=s.data_home / "elsewhere")
    with pytest.raises(ConfigError, match="SMC_BACKEND_RUN_DIR"):
        entry.verify_layout(bad)


def test_run_verifies_layout_before_starting_the_backend(wired, tmp_path, monkeypatch):
    # A bad layout must abort before the backend starts.
    s = make_settings(tmp_path)
    bad = dataclasses.replace(s, config_dir=s.data_home / "config")
    with pytest.raises(ConfigError):
        entry.run(bad, wait_for_shutdown=lambda c: "signal")
    assert "start_backend" not in wired


# --- yolo precondition: home must not be a default/live home ---------------


def test_verify_layout_rejects_a_default_home(tmp_path, monkeypatch):
    # SMC_DATA_HOME resolving to ~/.kiro/crew would make --approval yolo refuse.
    fake_home = tmp_path / "fakehome"
    (fake_home / ".kiro" / "crew").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    s = make_settings(tmp_path)
    bad = dataclasses.replace(
        s,
        data_home=fake_home / ".kiro" / "crew",
        config_dir=fake_home / ".kiro" / "crew",
        backend_run_dir=fake_home / ".kiro" / "crew" / "run",
    )
    with pytest.raises(ConfigError, match="default/live"):
        entry.verify_layout(bad)


# --- verify_sandbox --------------------------------------------------------


def test_verify_sandbox_ok_when_namespaces_available(tmp_path):
    entry.verify_sandbox(make_settings(tmp_path), probe=lambda: entry.SANDBOX_AVAILABLE)


def test_verify_sandbox_refuses_when_the_probe_cannot_answer(tmp_path):
    # An undetermined probe refuses like a denial: sandboxed-only means the guard may
    # never proceed on the absence of an answer, only on a positive one.
    verdict = f"{entry.SANDBOX_UNDETERMINED_PREFIX}this platform has no os.unshare"
    with pytest.raises(ConfigError, match="could not be determined"):
        entry.verify_sandbox(make_settings(tmp_path), probe=lambda: verdict)


def test_verify_sandbox_fails_loud_when_no_user_namespace(tmp_path):
    # Sandboxed-only: no user namespace means refuse to start. There is no opt-in
    # escape, and the message must not point at the removed config key.
    with pytest.raises(ConfigError, match="sandboxed-only") as exc:
        entry.verify_sandbox(make_settings(tmp_path), probe=lambda: entry.SANDBOX_DENIED)
    assert "sandbox_allow_unsandboxed_exec" not in str(exc.value)


# --- run() aborts on a missing credential before the backend starts --------


def test_run_refuses_without_api_key_before_the_backend(wired, tmp_path, monkeypatch):
    # Undo wired's neutralised gates so the real credential check runs against
    # an env with no KIRO_API_KEY.
    monkeypatch.setattr(backend_mod, "build_backend_env", lambda settings: {})
    monkeypatch.setattr(backend_mod, "require_api_key", _real_require_api_key)
    with pytest.raises(ConfigError, match="KIRO_API_KEY"):
        entry.run(make_settings(tmp_path), wait_for_shutdown=lambda c: "signal")
    assert "start_backend" not in wired


# --- run() installs the crew bundle before the backend starts --------------


def test_run_installs_the_bundle_before_the_backend_starts(wired, tmp_path, monkeypatch):
    # Undo wired's no-op install and record the call in the sequence instead.
    def recording_install(settings, **kw):
        wired.append("install_bundle")

    monkeypatch.setattr(entry.bundle_mod, "install_bundle", recording_install)
    entry.run(make_settings(tmp_path), wait_for_shutdown=lambda c: "signal")
    assert "install_bundle" in wired
    assert wired.index("install_bundle") < wired.index("start_backend")


def test_run_refuses_a_bad_bundle_before_the_backend(wired, tmp_path, monkeypatch):
    # Real install_bundle against a bundle dir that does not exist: run() must
    # abort before the backend starts.
    monkeypatch.setattr(entry.bundle_mod, "install_bundle", _real_install_bundle)
    s = make_settings(tmp_path)
    bad = dataclasses.replace(s, bundle_dir=tmp_path / "nope")
    with pytest.raises(ConfigError, match="bundle dir present"):
        entry.run(bad, wait_for_shutdown=lambda c: "signal")
    assert "start_backend" not in wired


def test_no_bucket_still_boots(wired, tmp_path):
    # No bucket means the front's transcript fetch reads nothing, not a boot failure.
    entry.run(make_settings(tmp_path, bucket=None), wait_for_shutdown=lambda c: "signal")
    assert "start_backend" in wired
    assert "start_front" in wired
