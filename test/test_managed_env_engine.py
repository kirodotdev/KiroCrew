"""The managed-environment launch engine against a fake service, box and registry."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from managed_env_fake_box import FakeBox, FakeRegistry
from managed_env_fake_service import FakeService

from kiro_crew.cloud.launch_job import RegistrationUnavailable
from kiro_crew.managed_env import PROVISIONER_ID, remote
from kiro_crew.managed_env.client import Credential, SandboxClient
from kiro_crew.managed_env.engine import SandboxEngineError, SandboxLaunchEngine
from kiro_crew.managed_env.guard import SandboxGuard
from kiro_crew.managed_env.settings import LaneSettings

TAG = "kc-0a1b2c"


def _settings(endpoint: str, **over) -> LaneSettings:
    base = dict(
        endpoint=endpoint,
        ssh_region="ex-test-1",
        kas_endpoint="https://runtime.example.com",
        kas_control_plane_endpoint="https://control.example.com",
        kas_region="ex-test-1",
        box_token_path="/var/example/token",
        box_env_drop=("EXAMPLE_TOKEN",),
    )
    return LaneSettings(**{**base, **over})


@pytest.fixture
def svc():
    f = FakeService()
    yield f
    f.close()


@pytest.fixture
def world(svc, tmp_path):
    lane: dict[str, Any] = {"settings": _settings(svc.endpoint), "shipped": True, "ssh_block": True}
    clock = {"t": 1_000_000.0}

    def sleep(s):
        clock["t"] += s

    client = SandboxClient(
        lambda: Credential("tok", "a"),
        SandboxGuard(tmp_path / "state", now=lambda: clock["t"]),
        settings=lambda: lane["settings"],
        sleep=sleep,
    )
    box, reg = FakeBox(), FakeRegistry()
    engine = SandboxLaunchEngine(
        client=client,
        registry_factory=lambda: reg,
        run=box,
        settings=lambda: lane["settings"],
        sleep=lambda s: None,
        machine_shipped=lambda: lane["shipped"],
        ssh_block=lambda: lane["ssh_block"],
    )
    return engine, box, reg, lane


def _launch(engine):
    engine.preflight("crew-a", "")
    host = engine.provision(tag=TAG, profile="crew-a")
    handle = engine.begin_signin(instance_id=host, profile="crew-a", region="")
    assert handle.already_logged_in and handle.error == "" and handle.wait(threading.Event())
    engine.register(instance_id=host, tag=TAG, profile="crew-a", region="")
    return host


def _run_all(svc):
    for row in svc.sandboxes.values():
        row["status"] = "RUNNING"


def test_a_launch_creates_installs_signs_in_and_registers(world, svc):
    engine, box, reg, _ = world
    svc.answer("GetSandbox", 200, {"execEnvironmentId": "env-001", "status": "RUNNING"})
    host = _launch(engine)
    assert host == "crew-sbx-env-001"
    assert svc.sandboxes["env-001"]["displayName"] == f"crew-a-{TAG}"
    assert box.steps() == [
        "install",
        "machine-config",
        "hook-write",
        "whoami",
        "hook-run",
        "health",
    ]
    (row,) = reg.list()
    assert (row.name, row.ssh_host, row.provisioner_id) == ("crew-a", host, PROVISIONER_ID)
    assert row.remote_bin == remote.WRAPPER


def test_a_missing_setting_is_refused_by_name_before_any_call(world, svc):
    engine, box, _, lane = world
    lane["settings"] = LaneSettings(endpoint=svc.endpoint)
    with pytest.raises(SandboxEngineError) as ei:
        engine.preflight("crew-a", "")
    assert ei.value.code == "not_configured"
    for key in ("managed_env_ssh_region", "managed_env_kas_endpoint", "managed_env_box_token_path"):
        assert key in str(ei.value)
    with pytest.raises(SandboxEngineError):
        engine.provision(tag=TAG, profile="crew-a")
    assert svc.requests == [] and box.scripts == []
    assert engine.teardown(tag=TAG, profile="crew-a", region="") is True  # nothing was made


def test_a_bad_crew_name_is_refused(world, svc):
    engine, *_ = world
    with pytest.raises(SandboxEngineError) as ei:
        engine.preflight("Bad Name", "")
    assert ei.value.code == "bad_crew_name"


@pytest.mark.parametrize("step", ["install", "machine-config", "hook-write"])
def test_a_failed_install_step_deletes_the_environment_by_its_id(world, svc, step):
    engine, box, *_ = world
    svc.answer("GetSandbox", 200, {"execEnvironmentId": "env-001", "status": "RUNNING"})
    box.fail.add(step)
    with pytest.raises(SandboxEngineError) as ei:
        engine.provision(tag=TAG, profile="crew-a")
    assert ei.value.code == "install_failed" and "could not be installed" in str(ei.value)
    assert svc.ops()[-1] == "DeleteSandbox" and "env-001" not in svc.sandboxes


def test_an_environment_that_never_runs_is_deleted(world, svc):
    engine, box, *_ = world
    svc.answer("GetSandbox", 200, {"execEnvironmentId": "env-001", "status": "FAILED"})
    with pytest.raises(SandboxEngineError) as ei:
        engine.provision(tag=TAG, profile="crew-a")
    assert ei.value.code == "not_running"
    assert "env-001" not in svc.sandboxes and box.scripts == []


def test_a_create_at_the_cap_says_so(world, svc):
    engine, *_ = world
    svc.answer("CreateSandbox", 429, {"__type": "x#ServiceQuotaExceededException"})
    with pytest.raises(SandboxEngineError) as ei:
        engine.provision(tag=TAG, profile="crew-a")
    assert ei.value.code == "quota"
    # nothing was created, so the core's rollback must not say it removed something
    assert engine.resource_exists(tag=TAG, profile="crew-a", region="") is False
    assert engine.resource_exists(tag="kc-other", profile="crew-a", region="") is True


@pytest.mark.parametrize(
    "account, hook, want",
    [
        ("user", "started", "signin"),
        ("machine", "not_machine", "register"),
        ("machine", "failed", "register"),
    ],
)
def test_a_box_not_on_machine_mode_stops_with_a_line(world, svc, account, hook, want):
    engine, box, *_ = world
    svc.answer("GetSandbox", 200, {"execEnvironmentId": "env-001", "status": "RUNNING"})
    box.account, box.hook = account, hook
    host = engine.provision(tag=TAG, profile="crew-a")
    handle = engine.begin_signin(instance_id=host, profile="crew-a", region="")
    if want == "signin":
        assert not handle.already_logged_in and "machine" in handle.error
        return
    with pytest.raises(RegistrationUnavailable):
        engine.register(instance_id=host, tag=TAG, profile="crew-a", region="")


def test_another_identity_is_refused_at_preflight_and_at_sign_in(world, svc):
    from kiro_crew.cloud.launch_job import _check_signin_target_supported
    from kiro_crew.cloud.login_target import KiroLoginTarget

    engine, box, *_ = world
    target = KiroLoginTarget(
        license="pro", start_url="https://example.awsapps.com/start", region="ex-test-1"
    )
    assert engine.login_target_refusal(KiroLoginTarget()) == ""
    assert engine.login_target_refusal(None) == ""
    assert "machine sign-in" in engine.login_target_refusal(target)
    job = type("J", (), {"login_target": target, "provider_id": PROVISIONER_ID})()
    with pytest.raises(RuntimeError, match="cannot sign in as"):
        _check_signin_target_supported(engine, job)
    # a sign-in retry that reaches the handle is refused there too, with no ssh call
    svc.answer("GetSandbox", 200, {"execEnvironmentId": "env-001", "status": "RUNNING"})
    host = engine.provision(tag=TAG, profile="crew-a")
    ran = len(box.scripts)
    handle = engine.begin_signin(instance_id=host, profile="crew-a", region="", login_target=target)
    assert not handle.already_logged_in and "machine sign-in" in handle.error
    assert len(box.scripts) == ran


def test_a_gateway_that_never_answers_stops_register(world, svc, monkeypatch):
    engine, box, *_ = world
    svc.answer("GetSandbox", 200, {"execEnvironmentId": "env-001", "status": "RUNNING"})
    box.healthy = False
    monkeypatch.setattr(remote, "PROBE_UP_TO_SECS", 0)
    host = engine.provision(tag=TAG, profile="crew-a")
    with pytest.raises(RegistrationUnavailable) as ei:
        engine.register(instance_id=host, tag=TAG, profile="crew-a", region="")
    assert "did not answer" in str(ei.value)


@pytest.mark.parametrize(
    "listed, confirmed", [("absent", True), ("DELETED", True), ("DELETING", False)]
)
def test_teardown_deletes_unfinished_ledger_ids_and_removes_nothing(world, svc, listed, confirmed):
    engine, box, reg, _ = world
    svc.answer("GetSandbox", 200, {"execEnvironmentId": "env-001", "status": "RUNNING"})
    _launch(engine)
    other = svc.add("RUNNING", f"crew-b-{TAG}")  # another client's environment, same tag
    if listed != "absent":
        svc.answer(
            "ListSandboxes",
            200,
            {"sandboxes": [{"execEnvironmentId": "env-001", "status": listed}]},
        )
    assert engine.teardown(tag=TAG, profile="crew-a", region="") is confirmed
    assert other in svc.sandboxes and "env-001" not in svc.sandboxes
    assert svc.ops().count("DeleteSandbox") == 1 and svc.ops()[-1] == "ListSandboxes"
    # It removes nothing: the Remote Crew row and the ledger row both stay.
    assert len(reg.list()) == 1
    (row,) = engine.client._guard.ledger()
    assert row["execEnvironmentId"] == "env-001" and "deletedAt" in row
    # A retry sends no second Delete for an id whose Delete was accepted.
    engine.teardown(tag=TAG, profile="crew-a", region="")
    assert svc.ops().count("DeleteSandbox") == 1


def test_a_refused_fresh_delete_is_not_confirmed(world, svc):
    engine, box, reg, _ = world
    svc.answer("GetSandbox", 200, {"execEnvironmentId": "env-001", "status": "RUNNING"})
    _launch(engine)
    svc.answer("DeleteSandbox", 500, {})
    assert engine.teardown(tag=TAG, profile="crew-a", region="") is False


def test_a_failed_list_stops_preflight(world, svc):
    engine, *_ = world
    svc.answer("ListSandboxes", 500, {})
    with pytest.raises(SandboxEngineError):
        engine.preflight("crew-a", "")


def test_register_updates_an_existing_row_for_the_same_box(world, svc):
    engine, box, reg, _ = world
    reg.add(name="old", ssh_host="crew-sbx-env-001", provisioner_id="")
    engine.register(instance_id="crew-sbx-env-001", tag=TAG, profile="crew-a", region="")
    (row,) = reg.list()
    assert row.name == "old" and row.provisioner_id == PROVISIONER_ID
    with pytest.raises(RegistrationUnavailable):
        engine.register(instance_id="not-an-alias", tag=TAG, profile="crew-a", region="")


def test_an_unreadable_ledger_deletes_nothing(world, svc):
    engine, *_ = world
    (engine.client._guard._dir / "ledger.json").write_text("{", encoding="utf-8")
    assert engine.teardown(tag=TAG, profile="crew-a", region="") is False
    assert svc.requests == []


def test_the_shared_client_is_one_per_gateway(monkeypatch, tmp_path):
    import kiro_crew.managed_env.engine as eng

    monkeypatch.setattr(eng, "_state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(eng, "_shared", None)
    assert eng.shared_client() is eng.shared_client()
    assert SandboxLaunchEngine().client is eng.shared_client()


@pytest.mark.parametrize(
    "missing, code", [("shipped", "no_machine_mode"), ("ssh_block", "ssh_config_missing")]
)
def test_preflight_refuses_before_any_create(world, svc, missing, code):
    engine, box, _, lane = world
    lane[missing] = False
    with pytest.raises(SandboxEngineError) as ei:
        engine.preflight("crew-a", "")
    assert ei.value.code == code
    assert "CreateSandbox" not in svc.ops() and box.scripts == []


def test_a_passing_outage_while_starting_costs_a_read_not_the_launch(world, svc):
    engine, *_ = world
    svc.answer("GetSandbox", 503, {})
    svc.answer("GetSandbox", 200, {"execEnvironmentId": "env-001", "status": "RUNNING"})
    assert engine.provision(tag=TAG, profile="crew-a") == "crew-sbx-env-001"


def test_the_cleanup_delete_is_sent_inside_the_backoff(world, svc, monkeypatch):
    engine, *_ = world
    svc.answer("GetSandbox", 200, {"execEnvironmentId": "env-001", "status": "RUNNING"})

    def install_fails_during_an_outage(host, cell):
        engine.client._guard.note_reply("ListSandboxes", 503, "", "a")
        raise RuntimeError("install")

    monkeypatch.setattr(engine, "_install", install_fails_during_an_outage)
    with pytest.raises(SandboxEngineError):
        engine.provision(tag=TAG, profile="crew-a")
    assert "env-001" not in svc.sandboxes


def test_a_create_reply_with_no_id_is_not_read_as_nothing_made(world, svc):
    engine, *_ = world
    svc.answer("CreateSandbox", 200, {"status": "PENDING"})
    with pytest.raises(SandboxEngineError):
        engine.provision(tag=TAG, profile="crew-a")
    # nothing to delete by id, and the rollback must not report the launch as clean
    assert engine.teardown(tag=TAG, profile="crew-a", region="") is False


@pytest.mark.parametrize("kind", ["no_token", "not_configured"])
def test_an_unsent_create_confirms_nothing_was_made(world, svc, monkeypatch, kind):
    engine, box, _, _ = world
    engine.preflight("crew-a", "")
    if kind == "no_token":
        monkeypatch.setattr(engine.client, "_credential", lambda: Credential(""))
    else:
        monkeypatch.setattr(engine.client, "_settings", lambda: LaneSettings())
    with pytest.raises(SandboxEngineError) as ei:
        engine.provision(tag=TAG, profile="crew-a")
    assert ei.value.code == kind
    assert svc.ops() == ["ListSandboxes"] and box.scripts == []
    assert engine.client._guard.ledger() == []
    assert engine.resource_exists(tag=TAG, profile="crew-a", region="") is False
    assert engine.teardown(tag=TAG, profile="crew-a", region="") is True


@pytest.mark.parametrize("kind", ["unreachable", "bad_reply", "unknown"])
def test_an_uncertain_create_is_not_confirmed_as_never_created(world, svc, monkeypatch, kind):
    from kiro_crew.managed_env.client import SandboxError

    engine, *_ = world

    def failed_post(*args):
        raise SandboxError("CreateSandbox", kind)

    monkeypatch.setattr(engine.client, "_post", failed_post)
    with pytest.raises(SandboxEngineError) as ei:
        engine.provision(tag=TAG, profile="crew-a")
    assert ei.value.code == kind
    assert svc.requests == []
    assert engine.resource_exists(tag=TAG, profile="crew-a", region="") is True
    assert engine.teardown(tag=TAG, profile="crew-a", region="") is False
