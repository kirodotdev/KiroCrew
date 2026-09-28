"""Tests for ``container.supervisor.__main__`` -- the startup ORDER and the drained
teardown order.

The backup subsystem was extracted from this PR, so there is no restore phase and no
sidecar: the supervisor gates the environment, installs the bundle, starts the backend,
waits for readiness, starts the front, and drains front then backend at teardown. The seams
are stubbed; the point here is the ordering guarantees, proven by the recorded call sequence.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path

import pytest
from container.common import ConfigError, Settings
from container.supervisor import __main__ as entry
from container.supervisor import backend as backend_mod
from container.supervisor.backend import build_backend_env as _real_build_backend_env
from container.supervisor.backend import require_model_identity as _real_require_model_identity
from container.supervisor.backend import seed_model_identity as _real_seed_model_identity
from container.supervisor.bundle import install_bundle as _real_install_bundle

#: The guard itself, kept under its own name so a test can restore it after the
#: ``wired`` fixture stubs it out.
_real_verify_sandbox = entry.verify_sandbox

#: One delivered model identity, in the shape Secrets Manager carries it. Expiry far
#: in the future so the access token alone is usable, rather than the fixture leaning
#: on a refresh token to look live.
IDENTITY_JSON = json.dumps(
    {
        "access_token": "atk-test",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "provider": "BuilderId",
        "identity": "builder_id",
    }
)


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


class Events(list):
    """The recorded call sequence, plus what the backend spawn was handed.

    A ``list`` subclass so every existing ordering assertion still reads the
    sequence directly, while a test about the spawn can read ``spawned_env``
    without a second fixture that would have to re-stub the same seam.
    """

    def __init__(self):
        super().__init__()
        self.spawned_env: list = []


@pytest.fixture
def wired(monkeypatch):
    """Stub every seam and record the order calls happen in."""
    events = Events()

    def fake_start_backend(settings, *, env=None, **kw):
        events.append("start_backend")
        events.spawned_env.append(env)
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
    monkeypatch.setattr(backend_mod, "seed_model_identity", lambda settings, **kw: True)
    monkeypatch.setattr(backend_mod, "require_model_identity", lambda settings: None)
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


# --- verify_sandbox, reached through run() ---------------------------------
#
# The unit cases for the guard itself live in test_supervisor_sandbox.py. What only
# this file can measure is whether its refusal is REACHABLE on the real call path, and
# that it lands before the backend starts.


def test_run_refuses_before_the_backend_when_the_host_cannot_sandbox(wired, tmp_path, monkeypatch):
    """Driven through ``run()``, with only the probe stubbed.

    Nothing about the backend environment is stubbed here, so the refusal has to come
    from the host verdict rather than from a hand-built dictionary. It must land before
    ``start_backend``: a container that spawned the worker first and refused afterwards
    would already have run it unsandboxed.
    """
    monkeypatch.setenv("KIRO_IDENTITY", IDENTITY_JSON)
    monkeypatch.setattr(backend_mod, "build_backend_env", _real_build_backend_env)
    monkeypatch.setattr(backend_mod, "seed_model_identity", _real_seed_model_identity)
    monkeypatch.setattr(backend_mod, "require_model_identity", _real_require_model_identity)
    monkeypatch.setattr(
        entry,
        "verify_sandbox",
        lambda s, **kw: _real_verify_sandbox(s, probe=lambda: entry.SANDBOX_DENIED, **kw),
    )

    with pytest.raises(ConfigError, match="sandboxed-only"):
        entry.run(make_settings(tmp_path), wait_for_shutdown=lambda c: "signal")
    assert "start_backend" not in wired


def test_run_starts_on_a_host_that_can_sandbox(wired, tmp_path, monkeypatch):
    """The other half, on the same path, so the refusal above is not vacuous.

    A test that only ever refuses cannot tell a working guard from one that refuses
    everything.
    """
    monkeypatch.setenv("KIRO_IDENTITY", IDENTITY_JSON)
    monkeypatch.setattr(backend_mod, "build_backend_env", _real_build_backend_env)
    monkeypatch.setattr(backend_mod, "seed_model_identity", _real_seed_model_identity)
    monkeypatch.setattr(backend_mod, "require_model_identity", _real_require_model_identity)
    monkeypatch.setattr(
        entry,
        "verify_sandbox",
        lambda s, **kw: _real_verify_sandbox(s, probe=lambda: entry.SANDBOX_AVAILABLE, **kw),
    )

    entry.run(make_settings(tmp_path), wait_for_shutdown=lambda c: "signal")
    assert "start_backend" in wired


# --- the spawn boundary ----------------------------------------------------


def test_the_env_handed_to_the_spawn_carries_no_credential(wired, tmp_path, monkeypatch):
    """Asserted at the SPAWN, not at ``build_backend_env``.

    ``verify_sandbox`` reads the environment several statements before
    ``start_backend`` receives it. Nothing writes to it in between today, so the
    guard is sound today -- but a later edit that re-injected a credential there
    would pass every check in this file while handing the worker exactly what the
    interlock exists to withhold. This test is what makes the invariant "absent all
    the way to the spawn" rather than "absent on one line", and it is also the only
    thing that reds if the two checks are reordered around the build.
    """
    monkeypatch.setattr(backend_mod, "build_backend_env", _real_build_backend_env)
    monkeypatch.setattr(backend_mod, "seed_model_identity", _real_seed_model_identity)
    monkeypatch.setattr(backend_mod, "require_model_identity", _real_require_model_identity)
    monkeypatch.setenv("KIRO_IDENTITY", IDENTITY_JSON)
    monkeypatch.setenv("KIRO_API_KEY", "sk-live")

    entry.run(make_settings(tmp_path), wait_for_shutdown=lambda c: "signal")

    assert wired.spawned_env, "start_backend was never reached"
    spawned = wired.spawned_env[-1]
    assert "KIRO_IDENTITY" not in spawned
    assert "KIRO_API_KEY" not in spawned
    # The identity did arrive and was consumed, so the absence above is the
    # withholding and not a test that simply delivered nothing.
    backend_mod.require_model_identity(make_settings(tmp_path))


# --- F1: a delivery that stored nothing is fatal -------------------------------


@pytest.mark.parametrize("delivered", ["", "   ", "\t\n"])
def test_run_refuses_when_nothing_was_delivered_rather_than_using_what_is_there(
    wired, tmp_path, monkeypatch, delivered
):
    """A blank secret must not fall through to the vault check.

    ``seed_model_identity`` returns False for an absent or whitespace delivery, and
    discarding that bool would let ``require_model_identity`` accept a slot a prior task
    left on this persistent volume -- starting the task authenticated as the previous
    account with nothing saying so. The vault is seeded here with a DIFFERENT, usable
    identity precisely so the test cannot pass merely because the vault is empty.
    """
    settings = make_settings(tmp_path)
    _real_seed_model_identity(settings, source={"KIRO_IDENTITY": IDENTITY_JSON})
    monkeypatch.setenv("KIRO_IDENTITY", delivered)
    monkeypatch.setattr(backend_mod, "seed_model_identity", _real_seed_model_identity)
    monkeypatch.setattr(backend_mod, "require_model_identity", _real_require_model_identity)

    with pytest.raises(ConfigError, match="no model identity was delivered"):
        entry.run(settings, wait_for_shutdown=lambda c: "signal")
    assert "start_backend" not in wired


# --- F3: the credential is not left published through procfs -------------------


@pytest.mark.parametrize("name", ["KIRO_IDENTITY", "KIRO_API_KEY"])
def test_run_drops_both_credential_shapes_from_its_own_environment(
    wired, tmp_path, monkeypatch, name
):
    """The front inherits this process's environment whole, so neither name may stay.

    ``build_backend_env`` only ever cleaned the COPY handed to the backend, and
    ``_start_front`` is spawned with no env argument at all -- and the front's own exec
    resets the dumpable flag this process clears for itself, so its ``/proc`` entry is
    readable by a same-uid worker. Dropping both here is what keeps a credential out of
    that second process.

    ``KIRO_API_KEY`` is included even though nothing is meant to deliver it: the secrets
    path derives each destination from its secret's name with no allowlist refusing that
    one, so an operator can provision it. Parametrised over the set rather than written
    once, because covering one name and leaving its sibling is how the same route stays
    open beside the fix.
    """
    monkeypatch.setenv("KIRO_IDENTITY", IDENTITY_JSON)
    monkeypatch.setenv("KIRO_API_KEY", "sk-should-not-survive")
    monkeypatch.setattr(backend_mod, "seed_model_identity", _real_seed_model_identity)
    monkeypatch.setattr(backend_mod, "require_model_identity", _real_require_model_identity)

    entry.run(make_settings(tmp_path), wait_for_shutdown=lambda c: "signal")

    assert name not in os.environ, f"the supervisor kept {name} for the front to inherit"


def test_run_makes_this_process_non_dumpable_before_spawning_the_backend(
    wired, tmp_path, monkeypatch
):
    """Ordering matters: after the spawn the window is already open.

    Clearing the variable does not remove it from ``/proc/<pid>/environ``, so the
    dumpable flag is what actually stops a same-uid worker reading it. It has to be
    cleared before any child exists.
    """
    calls: list[str] = []
    monkeypatch.setenv("KIRO_IDENTITY", IDENTITY_JSON)
    monkeypatch.setattr(backend_mod, "seed_model_identity", _real_seed_model_identity)
    monkeypatch.setattr(backend_mod, "require_model_identity", _real_require_model_identity)
    monkeypatch.setattr(entry, "make_non_dumpable", lambda **kw: calls.append("non_dumpable"))
    real_start = backend_mod.start_backend

    def recording_start(settings, **kw):
        calls.append("start_backend")
        return real_start(settings, **kw)

    monkeypatch.setattr(backend_mod, "start_backend", recording_start)

    entry.run(make_settings(tmp_path), wait_for_shutdown=lambda c: "signal")

    assert calls.index("non_dumpable") < calls.index("start_backend"), calls


def test_a_prctl_that_ran_and_refused_is_fatal_on_linux(monkeypatch):
    """Failing open here would serve turns with the credential published to the worker."""
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ConfigError, match="non-dumpable"):
        entry.make_non_dumpable(clear=lambda: "prctl(PR_SET_DUMPABLE, 0) failed with errno 1")


def test_a_platform_without_prctl_only_warns(monkeypatch):
    """This module is imported by tests on runners that are not the image."""
    monkeypatch.setattr(sys, "platform", "win32")
    entry.make_non_dumpable(clear=lambda: "libc.so.6 not loadable")  # must not raise


def test_the_real_prctl_call_succeeds_where_the_image_runs():
    """The shipped call and the platform must not drift apart.

    A ``prctl`` that started failing would make the container refuse to boot
    everywhere rather than fail a test, so the contract is pinned here: on Linux the
    real call returns success. Skipped elsewhere, because elsewhere is not the image.
    """
    if not sys.platform.startswith("linux"):
        pytest.skip("the crew container runs on Linux; prctl is a Linux contract")
    assert entry._clear_dumpable() == ""


# --- run() aborts on a missing credential before the backend starts --------


def test_run_refuses_without_a_model_identity_before_the_backend(wired, tmp_path, monkeypatch):
    # Undo wired's neutralised gates so the real vault check runs against a crew
    # whose vault was never seeded.
    monkeypatch.delenv("KIRO_IDENTITY", raising=False)
    monkeypatch.setattr(backend_mod, "build_backend_env", lambda settings: {})
    monkeypatch.setattr(backend_mod, "seed_model_identity", _real_seed_model_identity)
    monkeypatch.setattr(backend_mod, "require_model_identity", _real_require_model_identity)
    with pytest.raises(ConfigError, match="KIRO_IDENTITY"):
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
