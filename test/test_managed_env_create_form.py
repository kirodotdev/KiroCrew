"""The one Create form's setting reaches CreateSandbox as its own optional field
(``idleTimeoutMinutes``), carried in the provisioner's ``size_key``. A default is not sent (the
service reads absent the same), and a key or value outside the grammar refuses the launch before
any call. No extra volume, which the service refuses today. A Create's idle
switch 400 turns the form's field off, reads plainly and is not retried."""

from __future__ import annotations

import pytest
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_client import bff, ft, make  # noqa: F401  (fixtures)

from kiro_crew.managed_env import engine as engine_mod
from kiro_crew.managed_env.engine import (
    CREATE_IDLE_OFF_TEXT,
    CREATE_IDLE_RANGE,
    SandboxEngineError,
    SandboxLaunchEngine,
    create_options,
)
from kiro_crew.managed_env.routes import SandboxRoutes


@pytest.mark.parametrize(
    "key,want",
    [
        ("", None),
        ("idle=120", 120),
        ("idle=15", 15),
        ("idle=10080", 10080),
    ],
)
def test_the_form_settings_read_from_size_key(key, want):
    assert create_options(key) == want


@pytest.mark.parametrize(
    "key",
    [
        "idle=14",
        "idle=10081",
        "idle=x",
        "idle=",
        "idle=60;idle=60",
        "size=1",
        "volume=50",
        "idle=120;volume=50",
        "volume=0",
        "idle=60;",
        "idle=1e3",
    ],
)
def test_anything_else_refuses_before_any_call(key):
    with pytest.raises(SandboxEngineError) as got:
        create_options(key)
    assert got.value.code == "bad_options"


@pytest.mark.parametrize("key", ["xlarge", "balanced", "idle", ";"])
def test_a_bare_size_word_carries_no_setting_as_before(key):
    assert create_options(key) is None


def test_the_idle_range_is_the_services():
    assert CREATE_IDLE_RANGE == (15, 10080)


def test_a_default_create_keeps_its_exact_shape(make, bff):  # noqa: F811
    client, _ = make()
    client.create_sandbox("crew-tag-1", idle_timeout_minutes=None)
    assert bff.requests[-1]["body"] == {"displayName": "crew-tag-1"}


def test_the_form_value_is_sent_as_the_services_own_field_and_no_volume(make, bff):  # noqa: F811
    client, _ = make()
    client.create_sandbox("crew-tag-1", idle_timeout_minutes=120)
    assert bff.requests[-1]["body"] == {"displayName": "crew-tag-1", "idleTimeoutMinutes": 120}
    assert bff.ops().count("CreateSandbox") == 1  # never retried


class _Stop(Exception):
    pass


def _engine(make, monkeypatch):  # noqa: F811
    client, guard = make()
    engine = SandboxLaunchEngine(client=client, registry_factory=lambda: None)
    sent = []

    def _create(display, **kw):
        sent.append((display, kw))
        raise _Stop()

    monkeypatch.setattr(client, "create_sandbox", _create)
    return engine, sent


def test_provision_passes_the_form_values_to_create(make, monkeypatch):  # noqa: F811
    engine, sent = _engine(make, monkeypatch)
    with pytest.raises(_Stop):
        engine.provision(tag="kc-abc123", size_key="idle=240", profile="crew-sandbox-03")
    assert sent[0][1] == {"idle_timeout_minutes": 240}


def test_provision_with_the_defaults_sends_none_of_them(make, monkeypatch):  # noqa: F811
    engine, sent = _engine(make, monkeypatch)
    with pytest.raises(_Stop):
        engine.provision(tag="kc-abc123", size_key="", profile="crew-sandbox-03")
    assert sent[0][1] == {"idle_timeout_minutes": None}


def test_a_bad_size_key_creates_nothing(make, bff, monkeypatch):  # noqa: F811
    engine, sent = _engine(make, monkeypatch)
    with pytest.raises(SandboxEngineError):
        engine.provision(tag="kc-abc123", size_key="idle=5", profile="crew-sandbox-03")
    assert sent == [] and "kc-abc123" in engine._never_created
    assert bff.ops() == []


# ---- A Create's idle switch 400 --------------------------------------------------------


IDLE_OFF_400 = {
    "__type": "com.example.kiro#ValidationException",
    "message": "idleTimeoutMinutes isn't available while idle stop is off for this instance",
}


def test_a_creates_idle_switch_400_reads_plainly_turns_the_field_off_and_is_not_retried(
    make, bff, monkeypatch  # noqa: F811
):
    monkeypatch.setattr(engine_mod, "_idle_switch_off", [])
    client, guard = make()
    engine = SandboxLaunchEngine(client=client, registry_factory=lambda: None)
    bff.queue("CreateSandbox", 400, IDLE_OFF_400)
    with pytest.raises(SandboxEngineError) as got:
        engine.provision(tag="kc-abc123", size_key="idle=120", profile="crew-sandbox-03")
    assert got.value.code == "idle_off" and str(got.value) == CREATE_IDLE_OFF_TEXT
    assert CREATE_IDLE_OFF_TEXT == "Idle stop is off here, so the default applies. Launch again."
    assert bff.ops().count("CreateSandbox") == 1  # no retry
    assert "kc-abc123" in engine._never_created and not guard.stopped
    # the form reads idle stop as off: the List answer says so
    routes = SandboxRoutes(lambda: engine, run_in_thread=False)
    assert routes._idle_off() is True


def test_another_create_400_is_not_read_as_idle_off(make, bff, monkeypatch):  # noqa: F811
    monkeypatch.setattr(engine_mod, "_idle_switch_off", [])
    client, _ = make()
    engine = SandboxLaunchEngine(client=client, registry_factory=lambda: None)
    bff.queue_error("create_400")
    with pytest.raises(SandboxEngineError) as got:
        engine.provision(tag="kc-abc123", size_key="idle=120", profile="crew-sandbox-03")
    assert got.value.code != "idle_off" and engine_mod.idle_switch_off_at() is None
