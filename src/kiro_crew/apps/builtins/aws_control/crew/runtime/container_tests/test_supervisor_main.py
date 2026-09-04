"""Tests for ``container.supervisor.__main__`` -- the startup ORDER, which the
contract makes a correctness requirement, and the drained teardown order.

The seams are stubbed (no real restore, backend, front or sidecar): the point
here is the ordering guarantees, proven by the sequence of recorded calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from container.common import Settings
from container.supervisor import __main__ as entry
from container.supervisor import backend as backend_mod


def make_settings(tmp_path: Path, *, bucket: str | None = "smc-test-bucket") -> Settings:
    """Settings for the supervise phase.

    ``bucket`` defaults to a bucket because most of these tests assert the
    sidecar's own wiring, which only exists when there is somewhere to sync to.
    Pass ``bucket=None`` for a chatbot crew. The default used to be None, so every
    test here asserted the sidecar starts in exactly the configuration where
    starting it is a boot loop.
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
        backup_interval_secs=30,
    )


class FakePG:
    def __init__(self, name, events):
        self.name = name
        self._events = events

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

    def fake_restore(settings):
        events.append("restore")
        # run() now acts on the result, so the fake must return one. A clean
        # ok result (not partial) is the happy path these ordering tests assume.
        from container.backup.restore import RestoreResult

        return RestoreResult()

    def fake_start_backend(settings, *, env=None, **kw):
        events.append("start_backend")
        return FakePG("backend", events)

    def fake_wait_ready(settings, timeout, *, process=None, poll_interval=0.25):
        events.append("wait_ready")

    def fake_start_front(settings):
        events.append("start_front")
        return FakePG("front", events)

    def fake_start_sidecar(settings):
        events.append("start_sidecar")
        return FakePG("sidecar", events)

    monkeypatch.setattr(entry, "_run_restore", fake_restore)
    monkeypatch.setattr(backend_mod, "start_backend", fake_start_backend)
    monkeypatch.setattr(backend_mod, "wait_until_ready", fake_wait_ready)
    monkeypatch.setattr(entry, "_start_front", fake_start_front)
    monkeypatch.setattr(entry, "_start_sidecar", fake_start_sidecar)
    # Neutralise the environment gates for the ORDERING tests; each has its own
    # dedicated test below.
    monkeypatch.setattr(backend_mod, "build_backend_env", lambda settings: {})
    monkeypatch.setattr(backend_mod, "require_api_key", lambda env: None)
    monkeypatch.setattr(entry, "verify_sandbox", lambda settings, **kw: None)
    # install_bundle has its own tests (test_supervisor_bundle.py) and a
    # dedicated ordering test below; a no-op here keeps it out of the recorded
    # sequence the other ordering assertions pin.
    monkeypatch.setattr(entry.bundle_mod, "install_bundle", lambda settings, **kw: None)
    return events


def test_restore_completes_before_the_backend_starts(wired, tmp_path):
    entry.run(make_settings(tmp_path), wait_for_shutdown=lambda children: "signal")
    assert wired.index("restore") < wired.index("start_backend")


def test_backend_is_ready_before_front_and_sidecar_start(wired, tmp_path):
    entry.run(make_settings(tmp_path), wait_for_shutdown=lambda children: "signal")
    assert wired.index("wait_ready") < wired.index("start_front")
    assert wired.index("wait_ready") < wired.index("start_sidecar")


def test_full_happy_path_order(wired, tmp_path):
    entry.run(make_settings(tmp_path), wait_for_shutdown=lambda children: "signal")
    startup = [e for e in wired if not e.startswith("term:")]
    assert startup == [
        "restore",
        "start_backend",
        "wait_ready",
        "start_front",
        "start_sidecar",
    ]


def test_teardown_drains_front_then_backend_then_sidecar(wired, tmp_path):
    entry.run(make_settings(tmp_path), wait_for_shutdown=lambda children: "signal")
    teardown = [e for e in wired if e.startswith("term:")]
    assert teardown == ["term:front", "term:backend", "term:sidecar"]


def test_restore_failure_aborts_before_the_backend_starts(wired, tmp_path, monkeypatch):
    def boom(settings):
        wired.append("restore")
        raise RuntimeError("restore failed")

    monkeypatch.setattr(entry, "_run_restore", boom)
    with pytest.raises(RuntimeError, match="restore failed"):
        entry.run(make_settings(tmp_path), wait_for_shutdown=lambda c: "signal")
    # The backend must never have started on a failed restore -- a backend on an
    # incompletely restored home flushes over the gap.
    assert "start_backend" not in wired


def test_readiness_failure_tears_down_backend_and_never_starts_the_rest(
    wired, tmp_path, monkeypatch
):
    def not_ready(settings, timeout, *, process=None, poll_interval=0.25):
        wired.append("wait_ready")
        raise backend_mod.BackendReadyTimeout("nope")

    monkeypatch.setattr(backend_mod, "wait_until_ready", not_ready)
    with pytest.raises(backend_mod.BackendReadyTimeout):
        entry.run(make_settings(tmp_path), wait_for_shutdown=lambda c: "signal")

    assert "start_front" not in wired
    assert "start_sidecar" not in wired
    # The backend we started is drained rather than orphaned.
    assert "term:backend" in wired


def test_teardown_runs_even_if_supervise_raises(wired, tmp_path):
    def blow_up(children):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        entry.run(make_settings(tmp_path), wait_for_shutdown=blow_up)
    # All three were still drained.
    assert {"term:front", "term:backend", "term:sidecar"} <= set(wired)


# --- verify_layout (the Dockerfile open item) ------------------------------

import dataclasses
import json

import pytest as _pytest
from container.common import ConfigError
from container.supervisor.backend import require_api_key as _real_require_api_key
from container.supervisor.bundle import install_bundle as _real_install_bundle


def test_verify_layout_accepts_the_single_root_layout(tmp_path):
    entry.verify_layout(make_settings(tmp_path))  # must not raise


def test_verify_layout_rejects_a_config_subdir(tmp_path):
    # SMC_CONFIG_DIR=<home>/config is where common defaults it today, but the
    # backend writes open_slots.json / session_map.json at the home ROOT.
    s = make_settings(tmp_path)
    bad = dataclasses.replace(s, config_dir=s.data_home / "config")
    with _pytest.raises(ConfigError, match="SMC_CONFIG_DIR"):
        entry.verify_layout(bad)


def test_verify_layout_rejects_a_stray_run_dir(tmp_path):
    s = make_settings(tmp_path)
    bad = dataclasses.replace(s, backend_run_dir=s.data_home / "elsewhere")
    with _pytest.raises(ConfigError, match="SMC_BACKEND_RUN_DIR"):
        entry.verify_layout(bad)


def test_run_verifies_layout_before_touching_restore(wired, tmp_path, monkeypatch):
    # A bad layout must abort before restore runs or the backend starts.
    s = make_settings(tmp_path)
    bad = dataclasses.replace(s, config_dir=s.data_home / "config")
    with _pytest.raises(ConfigError):
        entry.run(bad, wait_for_shutdown=lambda c: "signal")
    assert "restore" not in wired
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
    with _pytest.raises(ConfigError, match="default/live"):
        entry.verify_layout(bad)


# --- verify_sandbox --------------------------------------------------------


def test_verify_sandbox_ok_when_namespaces_available(tmp_path):
    entry.verify_sandbox(make_settings(tmp_path), probe=lambda: True)  # no raise


def test_verify_sandbox_ok_when_probe_unknown(tmp_path):
    # Non-Linux / can't probe -> do not block.
    entry.verify_sandbox(make_settings(tmp_path), probe=lambda: None)


def test_verify_sandbox_fails_loud_when_unavailable_and_optin_off(tmp_path):
    with _pytest.raises(ConfigError, match="sandbox_allow_unsandboxed_exec"):
        entry.verify_sandbox(make_settings(tmp_path), probe=lambda: False)


def test_verify_sandbox_allows_when_optin_true_in_config(tmp_path):
    s = make_settings(tmp_path)
    s.data_home.mkdir(parents=True, exist_ok=True)
    (s.data_home / "config.json").write_text(
        json.dumps({"agent": {"sandbox_allow_unsandboxed_exec": True}})
    )
    entry.verify_sandbox(s, probe=lambda: False)  # opt-in accepted -> no raise


def test_optin_reader_honours_config_local_overlay(tmp_path):
    s = make_settings(tmp_path)
    s.data_home.mkdir(parents=True, exist_ok=True)
    (s.data_home / "config.json").write_text(
        json.dumps({"agent": {"sandbox_allow_unsandboxed_exec": False}})
    )
    (s.data_home / "config.local.json").write_text(
        json.dumps({"agent": {"sandbox_allow_unsandboxed_exec": True}})
    )
    assert entry._unsandboxed_exec_opted_in(s) is True


# --- run() aborts on a missing credential before touching restore ----------


def test_run_refuses_without_api_key_before_restore(wired, tmp_path, monkeypatch):
    # Undo wired's neutralised gates so the real credential check runs against
    # an env with no KIRO_API_KEY.
    monkeypatch.setattr(backend_mod, "build_backend_env", lambda settings: {})
    monkeypatch.setattr(backend_mod, "require_api_key", _real_require_api_key)
    with _pytest.raises(ConfigError, match="KIRO_API_KEY"):
        entry.run(make_settings(tmp_path), wait_for_shutdown=lambda c: "signal")
    assert "restore" not in wired
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
    # It is a step-0 gate, so it also runs before restore -- nothing has started.
    assert wired.index("install_bundle") < wired.index("restore")


def test_run_refuses_a_bad_bundle_before_restore(wired, tmp_path, monkeypatch):
    # Real install_bundle against a bundle dir that does not exist: run() must
    # abort before restore runs or the backend starts. Uses the reference
    # captured at import (the wired fixture has replaced the module attribute).
    monkeypatch.setattr(entry.bundle_mod, "install_bundle", _real_install_bundle)
    s = make_settings(tmp_path)
    bad = dataclasses.replace(s, bundle_dir=tmp_path / "nope")
    with _pytest.raises(ConfigError, match="bundle dir present"):
        entry.run(bad, wait_for_shutdown=lambda c: "signal")
    assert "restore" not in wired
    assert "start_backend" not in wired


# --- run() acts on the RestoreResult (fail closed on partial, boot on empty) --
#
# run_restore does not raise on a degraded restore; it returns a RestoreResult
# and leaves the boot/abort decision to the supervisor. These pin that the
# supervisor actually makes it: a partial restore must abort BEFORE the backend
# starts (a backend on an incompletely restored home flushes over the gap and
# erases the conversation record), while an empty bucket is a clean first boot
# that must proceed.

from container.backup.restore import RestoreResult as _RestoreResult


def _restore_returning(result, events):
    def _fake(settings):
        events.append("restore")
        return result

    return _fake


def test_partial_restore_aborts_before_the_backend_starts(wired, tmp_path, monkeypatch):
    partial = _RestoreResult(partial=True, missing=["open_slots.json"])
    monkeypatch.setattr(entry, "_run_restore", _restore_returning(partial, wired))
    with _pytest.raises(ConfigError, match="PARTIAL"):
        entry.run(make_settings(tmp_path), wait_for_shutdown=lambda c: "signal")
    # The whole point: restore ran, but the backend never did, so no flush could
    # land over the gap. Front and sidecar never started either.
    assert "restore" in wired
    assert "start_backend" not in wired
    assert "start_front" not in wired
    assert "start_sidecar" not in wired


def test_partial_restore_abort_names_the_missing_file(wired, tmp_path, monkeypatch):
    partial = _RestoreResult(partial=True, missing=["session_map.json"])
    monkeypatch.setattr(entry, "_run_restore", _restore_returning(partial, wired))
    with _pytest.raises(ConfigError, match="session_map.json"):
        entry.run(make_settings(tmp_path), wait_for_shutdown=lambda c: "signal")


def test_empty_restore_is_a_clean_first_boot_and_proceeds(wired, tmp_path, monkeypatch):
    # empty (nothing in the bucket) is NOT partial: it must boot.
    empty = _RestoreResult(empty=True)
    monkeypatch.setattr(entry, "_run_restore", _restore_returning(empty, wired))
    entry.run(make_settings(tmp_path), wait_for_shutdown=lambda c: "signal")
    assert "start_backend" in wired
    assert wired.index("restore") < wired.index("start_backend")


def test_ok_restore_proceeds(wired, tmp_path, monkeypatch):
    ok = _RestoreResult(restored=2)  # not partial, not empty
    assert ok.ok is True
    monkeypatch.setattr(entry, "_run_restore", _restore_returning(ok, wired))
    entry.run(make_settings(tmp_path), wait_for_shutdown=lambda c: "signal")
    assert "start_backend" in wired


def test_disabled_restore_no_bucket_still_boots(wired, tmp_path, monkeypatch):
    # disabled (SMC_BACKUP_BUCKET unset -> chatbot mode) is not partial: boot.
    disabled = _RestoreResult(disabled=True)
    monkeypatch.setattr(entry, "_run_restore", _restore_returning(disabled, wired))
    entry.run(make_settings(tmp_path, bucket=None), wait_for_shutdown=lambda c: "signal")
    assert "start_backend" in wired
