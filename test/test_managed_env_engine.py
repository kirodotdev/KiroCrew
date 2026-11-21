"""The persistent-sandbox ``LaunchEngine`` against the loopback fake service and a fake registry.

Nothing reaches a network beyond the loopback fake, and nothing sleeps. The shape oracle
is ``test_devspace_lane.py``.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

import pytest
from managed_env_fake_bff import FakeBff, FakeTime
from managed_env_fake_box import LaunchBox
from managed_env_fake_ssh import FakeSandboxSsh
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401

from kiro_crew.managed_env import PROVISIONER_ID
from kiro_crew.managed_env.client import BffClient
from kiro_crew.managed_env.engine import (
    SandboxEngineError,
    SandboxLaunchEngine,
    reset_shared_client,
    sandbox_id_from_host,
    ssh_host_for,
)
from kiro_crew.managed_env.guard import SandboxGuard

TOKEN = "tok-SECRET-0123456789"
TAG = "kc-1a2b3c"


@dataclass
class Row:
    id: str
    name: str
    ssh_host: str
    provisioner_id: str = PROVISIONER_ID
    connection_method: str = "ssh"
    remote_port: int = 5476
    remote_bin: str = ""


class FakeRegistry:
    def __init__(self) -> None:
        self.rows: dict[str, Row] = {}
        self.fail_add = False
        self.fail_remove = False

    def list(self):
        return list(self.rows.values())

    def add(self, *, name, connection_method, ssh_host, remote_port, provisioner_id, remote_bin=""):
        if self.fail_add:
            raise OSError("disk full")
        row = Row(
            f"row-{len(self.rows) + 1}",
            name,
            ssh_host,
            provisioner_id,
            connection_method,
            remote_port,
            remote_bin,
        )
        self.rows[row.id] = row
        return row

    def update(self, row_id, **fields):
        for k, v in fields.items():
            setattr(self.rows[row_id], k, v)

    def remove_cascade(self, row_id):
        if self.fail_remove:
            raise OSError("locked")
        self.rows.pop(row_id, None)


@pytest.fixture
def bff():
    b = FakeBff()
    yield b
    b.close()


@pytest.fixture
def ft():
    return FakeTime()


@pytest.fixture
def make(bff, ft, tmp_path):
    def build(token=TOKEN):
        guard = SandboxGuard(tmp_path / "state", now=ft.now)
        client = BffClient(
            lambda: token, guard, endpoint=bff.endpoint, sleep=ft.sleep, clock=ft.clock
        )
        reg = FakeRegistry()
        ssh = FakeSandboxSsh()
        # Crew and kiro-cli on the box, signed in: provision writes the hook, register starts it.
        box = LaunchBox(installed=True).with_hook()
        engine = SandboxLaunchEngine(
            client=client,
            registry_factory=lambda: reg,
            ssh=ssh.run,
            clock=ft.clock,
            sleep=ft.sleep,
            install_runner=box,
        )
        return engine, reg, guard

    return build


def launch(engine, tag=TAG, crew="kirocrew-main"):
    return engine.provision(tag=tag, size_key="xlarge", profile=crew, region="")


# -- happy path ----------------------------------------------------------------------


def test_launch_happy_path_preflight_provision_signin_register(make, bff):
    engine, reg, guard = make()
    bff.get_statuses["env-001"] = deque(["PENDING", "RUNNING"])
    engine.preflight("kirocrew-main", "")
    host = launch(engine)
    assert host == "crew-sbx-env-001"
    assert bff.ops() == [
        "ListSandboxes",
        "ListSandboxes",
        "CreateSandbox",
        "GetSandbox",
        "GetSandbox",
    ]
    create = [r for r in bff.requests if r["op"] == "CreateSandbox"][0]
    assert create["body"] == {
        "displayName": f"kirocrew-main-{TAG}"
    }  # the crew name, then the launch tag
    row = guard.ledger()[0]  # written on the answer, before the poll
    assert (row["execEnvironmentId"], row["displayName"]) == ("env-001", f"kirocrew-main-{TAG}")
    handle = engine.begin_signin(instance_id=host, profile="kirocrew-main", region="")
    assert not handle.error and handle.already_logged_in and not handle.code and not handle.url
    engine.register(instance_id=host, tag=TAG, profile="kirocrew-main", region="")
    [r] = reg.list()
    assert (r.name, r.ssh_host, r.provisioner_id, r.connection_method) == (
        "kirocrew-main",
        "crew-sbx-env-001",
        PROVISIONER_ID,
        "ssh",
    )


def test_the_ledger_is_written_before_the_poll_starts(make, bff):
    engine, _, guard = make()
    seen = []
    real = engine.client.wait_running

    def spy(sid, **kw):
        seen.append([r["execEnvironmentId"] for r in guard.ledger()])
        return real(sid, **kw)

    engine.client.wait_running = spy
    bff.get_statuses["env-001"] = deque(["RUNNING"])
    launch(engine)
    assert seen == [["env-001"]]


def test_register_twice_for_one_crew_repoints_the_row(make):
    engine, reg, _ = make()
    engine.register(instance_id="crew-sbx-env-001", tag=TAG, profile="kirocrew-main", region="")
    engine.register(
        instance_id="crew-sbx-env-002", tag="kc-000000", profile="kirocrew-main", region=""
    )
    assert [r.ssh_host for r in reg.list()] == ["crew-sbx-env-002"]


def test_register_writes_the_launcher_on_projects_as_remote_bin_on_add_and_update(make):
    from kiro_crew.managed_env.install import WRAPPER

    engine, reg, _ = make()
    engine.register(instance_id="crew-sbx-env-001", tag=TAG, profile="kirocrew-main", region="")
    [r] = reg.list()
    assert r.remote_bin == WRAPPER == "/projects/.crew/bin/kirocrew"
    r.remote_bin = ""  # an older row: the update writes it
    engine.register(
        instance_id="crew-sbx-env-002", tag="kc-000000", profile="kirocrew-main", region=""
    )
    [r] = reg.list()
    assert (r.ssh_host, r.remote_bin) == ("crew-sbx-env-002", WRAPPER)


@pytest.mark.parametrize("stamp", ["", PROVISIONER_ID])
def test_register_over_a_row_of_the_same_host_stamps_it(make, stamp):
    """A crew row first added another way (by hand, or by an older build) has no stamp, and the
    row actions draw only on the lane's stamp: the update branch stamps it."""
    engine, reg, _ = make()
    reg.rows["row-hand"] = Row("row-hand", "by-hand", "crew-sbx-env-001", provisioner_id=stamp)
    engine.register(instance_id="crew-sbx-env-001", tag=TAG, profile="kirocrew-main", region="")
    [r] = reg.list()
    assert (r.id, r.ssh_host, r.provisioner_id) == ("row-hand", "crew-sbx-env-001", PROVISIONER_ID)


def test_register_keeps_another_provisioners_stamp_and_says_so(make, caplog):
    engine, reg, _ = make()
    reg.rows["row-ds"] = Row("row-ds", "ds", "crew-sbx-env-001", provisioner_id="other_lane")
    with caplog.at_level("WARNING", logger="kiro_crew.managed_env.engine"):
        engine.register(instance_id="crew-sbx-env-001", tag=TAG, profile="kirocrew-main", region="")
    [r] = reg.list()
    assert (r.ssh_host, r.provisioner_id) == ("crew-sbx-env-001", "other_lane")
    said = [m for m in caplog.messages if "keeps provisioner" in m]
    assert len(said) == 1 and "other_lane" in said[0] and "row-ds" in said[0]


def test_register_with_no_matching_row_adds_a_stamped_row(make):
    engine, reg, _ = make()
    reg.rows["row-other"] = Row("row-other", "other", "some-other-host", provisioner_id="")
    engine.register(instance_id="crew-sbx-env-001", tag=TAG, profile="kirocrew-main", region="")
    rows = {r.ssh_host: r for r in reg.list()}
    assert rows["crew-sbx-env-001"].provisioner_id == PROVISIONER_ID
    assert rows["some-other-host"].provisioner_id == ""  # an unrelated row is left alone


def test_the_core_registry_takes_the_stamp_on_update(make, tmp_path):
    from kiro_crew.instances.registry import InstancesRegistry

    engine, _, _ = make()
    real = InstancesRegistry(tmp_path / "instances.json")
    real.add(name="by-hand", connection_method="ssh", ssh_host="crew-sbx-env-001", remote_port=5476)
    engine._registry_factory = lambda: real
    engine.register(instance_id="crew-sbx-env-001", tag=TAG, profile="kirocrew-main", region="")
    [row] = InstancesRegistry(tmp_path / "instances.json").list()
    assert (row.name, row.provisioner_id) == ("by-hand", PROVISIONER_ID)


def test_the_core_registry_and_token_mint_take_the_launcher_verbatim(make, tmp_path):
    """The core's own checks accept the path, and its mint runs an explicit ``remote_bin``
    verbatim (no run-marker lookup), so the mint reaches the install on /projects."""
    from kiro_crew.instances import token_mint
    from kiro_crew.instances.registry import InstancesRegistry
    from kiro_crew.instances.validation import validate_remote_bin
    from kiro_crew.managed_env.install import WRAPPER

    assert validate_remote_bin(WRAPPER) == WRAPPER
    engine, _, _ = make()
    real = InstancesRegistry(tmp_path / "instances.json")
    engine._registry_factory = lambda: real
    engine.register(instance_id="crew-sbx-env-001", tag=TAG, profile="kirocrew-main", region="")
    engine.register(instance_id="crew-sbx-env-002", tag=TAG, profile="kirocrew-main", region="")
    [row] = InstancesRegistry(tmp_path / "instances.json").list()
    assert (row.ssh_host, row.remote_bin) == ("crew-sbx-env-002", WRAPPER)
    cmd = token_mint.build_remote_token_command(row.remote_bin, port=row.remote_port)
    assert cmd.startswith(f'"{WRAPPER}" ')
    assert cmd != token_mint.build_remote_token_command(
        "", port=row.remote_port
    )  # no candidate search


def test_register_failure_raises_registration_unavailable_without_leaking(make):
    from kiro_crew.cloud.launch_job import RegistrationUnavailable

    engine, reg, _ = make()
    reg.fail_add = True
    with pytest.raises(RegistrationUnavailable) as ei:
        engine.register(instance_id="crew-sbx-env-001", tag=TAG, profile="kirocrew-main", region="")
    assert "crew-sbx-env-001" in str(ei.value) and "disk full" not in str(ei.value)


def test_register_refuses_an_instance_that_is_not_a_sandbox_alias(make):
    from kiro_crew.cloud.launch_job import RegistrationUnavailable

    engine, reg, _ = make()
    with pytest.raises(RegistrationUnavailable):
        engine.register(instance_id="ds-not-a-sandbox", tag=TAG, profile="kirocrew-main", region="")
    assert reg.list() == []


# -- preflight -----------------------------------------------------------------------


def test_product_rules_another_clients_sandbox_neither_stops_nor_refuses_the_launch(make, bff):
    engine, _, guard = make()
    other = bff.add_sandbox("RUNNING", "somebody-else")
    engine.preflight("kirocrew-main", "")
    bff.get_statuses["env-002"] = deque(["RUNNING"])
    launch(engine)
    assert bff.count("CreateSandbox") == 1
    assert guard.stopped == ""
    assert other not in guard.own_ids()


def test_product_rules_preflight_with_only_another_clients_sandbox_listed(make, bff):
    engine, _, guard = make()
    other = bff.add_other("RUNNING")
    engine.preflight("kirocrew-main", "")
    bff.get_statuses["env-001"] = deque(["RUNNING"])
    host = launch(engine)
    assert bff.count("CreateSandbox") == 1
    assert [r["execEnvironmentId"] for r in guard.ledger()] == ["env-001"]
    assert host == "crew-sbx-env-001"
    assert bff.sandboxes[other]["status"] == "RUNNING" and bff.naming(other) == []


def test_product_rules_an_own_live_sandbox_no_longer_refuses(make, bff):
    # no own 1-live cap; the service's cap governs
    engine, _, guard = make()
    mine = bff.add_sandbox("RUNNING", "mine")
    guard.record_created(dict(bff.sandboxes[mine]), "mine")
    bff.add_other("RUNNING")
    bff.add_other("STOPPED")
    engine.preflight("kirocrew-main", "")  # no refusal
    out = engine.client.create_sandbox(TAG)
    assert bff.count("CreateSandbox") == 1 and guard.is_own(out["execEnvironmentId"])
    assert guard.stopped == ""


def _cap_launch(engine, bff, key, n_other):
    others = [bff.add_other("STOPPED" if i % 2 else "RUNNING") for i in range(n_other)]
    bff.queue_error(key)
    with pytest.raises(SandboxEngineError) as ei:
        launch(engine)
    return ei.value, others


def test_product_rules_the_cap_is_a_text_and_no_stop(make, bff, ft):
    from kiro_crew.managed_env.engine import CARD_TEXT_LIMIT

    engine, _, guard = make()
    exc, others = _cap_launch(engine, bff, "create_429", 10)
    text = str(exc)
    assert exc.code == "sandbox_quota"
    # the service's cap is the only one, said plainly, no code
    assert text == (
        "You have 10 sandboxes, the most allowed. " "Delete one under Your crews to create another."
    )
    assert "minutes" not in text and "sandbox_quota" not in text
    assert len(text) <= CARD_TEXT_LIMIT
    assert bff.count("CreateSandbox") == 1  # no retry
    assert "DeleteSandbox" not in bff.ops()  # nothing was created
    assert TAG in engine._never_created and guard.ledger() == []
    assert guard.stopped == ""
    n = bff.count("ListSandboxes")
    assert len(engine.client.list_sandboxes()) == 10 and bff.count("ListSandboxes") == n + 1
    # nothing counted and no hold: the counters file is neither read nor written
    assert not guard.unconfirmed_path.exists()
    assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is True
    assert bff.count("DeleteSandbox") == 0


def test_product_rules_a_second_launch_right_after_the_cap_is_sent(make, bff, ft):
    engine, _, guard = make()
    _cap_launch(engine, bff, "create_429", 10)
    ft.advance(60)
    bff.queue_error("create_429")  # the account is still at its cap
    with pytest.raises(SandboxEngineError) as ei:
        launch(engine, tag="kc-000002")
    assert ei.value.code == "sandbox_quota"  # the service's answer; no gap refused it
    assert bff.count("CreateSandbox") == 2  # the second Create was sent


def test_product_rules_the_cap_from_the_services_402_counts_the_lists_entries(make, bff):
    engine, _, guard = make()
    exc, _ = _cap_launch(engine, bff, "create_402", 4)
    assert exc.code == "sandbox_quota"
    assert str(exc) == (
        "You have 4 sandboxes, the most allowed. " "Delete one under Your crews to create another."
    )
    assert guard.stopped == "" and bff.count("CreateSandbox") == 1
    assert TAG in engine._never_created


def test_a_throttle_on_create_stops_the_client_in_product_rules(make, bff):
    engine, _, guard = make()
    bff.queue_error("create_429_throttle")
    with pytest.raises(SandboxEngineError) as ei:
        launch(engine)
    assert ei.value.code == "ThrottlingException"
    assert guard.stopped


def test_the_quota_text_without_counts_says_to_launch_again():
    from kiro_crew.managed_env.engine import sandbox_quota_text

    text = sandbox_quota_text(None, None)
    assert text == (
        "You have the most sandboxes allowed. " "Delete one under Your crews to create another."
    )
    assert "minutes" not in text


def test_preflight_rejects_a_bad_crew_name(make):
    engine, _, _ = make()
    with pytest.raises(SandboxEngineError) as ei:
        engine.preflight("Bad Name", "")
    assert ei.value.code == "bad_crew_name"


def test_preflight_needs_a_token_and_names_the_remedy(make, bff):
    engine, _, _ = make(token="")
    with pytest.raises(Exception) as ei:
        engine.preflight("kirocrew-main", "")
    assert "no_token" in str(ei.value) or "token" in str(ei.value)
    assert bff.requests == []


def test_one_shared_client_per_process(monkeypatch, tmp_path):
    from kiro_crew.managed_env import engine as eng

    monkeypatch.setattr(eng, "_state_dir", lambda: tmp_path / "s")
    reset_shared_client()
    try:
        assert eng.shared_client() is eng.shared_client()
        assert SandboxLaunchEngine().client is eng.shared_client()
    finally:
        reset_shared_client()


# -- failure and cancel: rollback by ledger id ------------------------------------------


def test_a_failed_poll_deletes_the_sandbox_by_its_ledger_id(make, bff):
    engine, _, guard = make()
    bff.get_statuses["env-001"] = deque(["PENDING", "FAILED"])
    with pytest.raises(SandboxEngineError) as ei:
        launch(engine)
    assert ei.value.code == "failed"
    deletes = [r for r in bff.requests if r["op"] == "DeleteSandbox"]
    assert [d["body"] for d in deletes] == [{"execEnvironmentId": "env-001"}]
    assert guard.ledger()[0].get("deletedAt")
    assert bff.sandboxes["env-001"]["status"] == "DELETED"


def test_cancel_mid_poll_is_rolled_back_by_the_core_through_teardown(make, bff, tmp_path):
    """The core never interrupts provision: it sees the cancel when provision returns, then
    calls teardown(tag). Drive the real run_launch with the cancel set during the poll."""
    from kiro_crew.cloud import launch_job as lj

    engine, reg, guard = make()
    cancel = threading.Event()
    bff.get_statuses["env-001"] = deque(["PENDING", "PENDING", "RUNNING"])
    real_sleep = engine.client._sleep

    def sleep_then_cancel(s):
        real_sleep(s)
        cancel.set()

    engine.client._sleep = sleep_then_cancel
    store = lj.LaunchJobStore(tmp_path / "jobs")
    job = lj.LaunchJob(
        id="a" * 12,
        profile="kirocrew-main",
        region="",
        size_key="xlarge",
        tag=TAG,
        provider_id=PROVISIONER_ID,
    )
    store.save(job)
    out = lj.run_launch(job, store, engine, cancel=cancel)
    assert out.status == lj.CANCELLED
    assert [r["body"] for r in bff.requests if r["op"] == "DeleteSandbox"] == [
        {"execEnvironmentId": "env-001"}
    ]
    assert bff.sandboxes["env-001"]["status"] == "DELETED"
    assert guard.ledger()[0].get("deletedAt")
    assert reg.list() == []
    assert "Removed" in out.step(lj.STEP_PROVISION).detail


def test_failed_poll_then_core_teardown_makes_no_second_delete(make, bff):
    engine, _, _ = make()
    bff.get_statuses["env-001"] = deque(["FAILED"])
    with pytest.raises(SandboxEngineError):
        launch(engine)
    before = bff.count("DeleteSandbox")
    assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is True
    assert bff.count("DeleteSandbox") == before == 1


# -- teardown ------------------------------------------------------------------------


def test_teardown_deletes_by_ledger_id_reads_back_and_drops_the_row(make, bff):
    engine, reg, guard = make()
    bff.get_statuses["env-001"] = deque(["RUNNING"])
    host = launch(engine)
    engine.register(instance_id=host, tag=TAG, profile="kirocrew-main", region="")
    n = len(bff.requests)
    assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is True
    assert bff.ops()[n:] == ["DeleteSandbox", "GetSandbox", "ListSandboxes"]
    assert reg.list() == []
    assert guard.ledger()[0].get("deletedAt")


def test_teardown_with_no_ledger_entry_makes_no_bff_call(make, bff, caplog):
    engine, _, _ = make()
    bff.add_sandbox("RUNNING", TAG)  # same name, but not ours: a name is never a key
    with caplog.at_level("WARNING"):
        assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is False
    assert bff.requests == []
    assert "no ledger entry" in caplog.text


def test_teardown_after_a_refused_create_reports_nothing_to_remove(make, bff):
    engine, _, _ = make()
    bff.queue_error("create_400")
    with pytest.raises(SandboxEngineError):
        launch(engine)
    assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is True
    assert bff.count("DeleteSandbox") == 0


def test_teardown_after_a_dropped_create_answer_is_not_claimed_clean(make, bff):
    from managed_env_fake_bff import DROP

    engine, _, _ = make()
    bff.queue("CreateSandbox", DROP)
    with pytest.raises(SandboxEngineError):
        launch(engine)
    assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is False
    assert bff.count("DeleteSandbox") == 0  # no id, no call


def test_teardown_is_false_when_the_delete_does_not_read_back(make, bff):
    engine, reg, _ = make()
    bff.get_statuses["env-001"] = deque(["RUNNING"])
    launch(engine)
    bff.queue("DeleteSandbox", 200, {"execEnvironmentId": "env-001", "status": "DELETING"})
    # the fake keeps the box RUNNING because the queued answer replaced the real delete
    assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is False


def test_teardown_is_false_when_get_still_answers_200_though_list_is_empty(make, bff):
    engine, _, _ = make()
    bff.get_statuses["env-001"] = deque(["RUNNING"])
    launch(engine)
    bff.queue("GetSandbox", 200, {"execEnvironmentId": "env-001", "status": "RUNNING"})
    bff.queue("ListSandboxes", 200, {"sandboxes": []})
    assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is False


def test_teardown_is_false_when_get_is_404_but_list_still_shows_the_id(make, bff):
    engine, _, _ = make()
    bff.get_statuses["env-001"] = deque(["RUNNING"])
    launch(engine)
    bff.queue_error("get_404")
    bff.queue(
        "ListSandboxes",
        200,
        {"sandboxes": [{"execEnvironmentId": "env-001", "status": "RUNNING", "displayName": TAG}]},
    )
    assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is False


def test_a_retried_teardown_of_an_already_deleted_id_still_reports_a_stuck_row(make, bff):
    engine, reg, guard = make()
    bff.get_statuses["env-001"] = deque(["FAILED"])
    with pytest.raises(SandboxEngineError):
        launch(engine)  # deleted by the belt; the ledger row is marked deleted
    reg.rows["r1"] = Row("r1", "kirocrew-main", "crew-sbx-env-001")
    reg.fail_remove = True
    assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is False
    reg.fail_remove = False
    assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is True
    assert reg.list() == []


def test_a_retry_after_a_failed_read_back_confirms_before_it_drops_the_row(make, bff):
    engine, reg, guard = make()
    bff.get_statuses["env-001"] = deque(["RUNNING"])
    host = launch(engine)
    engine.register(instance_id=host, tag=TAG, profile="kirocrew-main", region="")
    # first teardown: Delete is accepted (ledger marked deleted) but Get still says 200
    bff.queue("GetSandbox", 200, {"execEnvironmentId": "env-001", "status": "DELETING"})
    assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is False
    assert guard.ledger()[0].get("deletedAt") and len(reg.list()) == 1
    # retry by tag: the read-back still fails (Get 200), so False, row kept, no new Delete
    bff.queue("GetSandbox", 200, {"execEnvironmentId": "env-001", "status": "DELETING"})
    assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is False
    assert len(reg.list()) == 1 and bff.count("DeleteSandbox") == 1
    # retry whose List still shows the id: also False, row kept
    bff.queue_error("get_404")
    bff.queue(
        "ListSandboxes",
        200,
        {"sandboxes": [{"execEnvironmentId": "env-001", "status": "DELETING", "displayName": TAG}]},
    )
    assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is False
    assert len(reg.list()) == 1 and bff.count("DeleteSandbox") == 1


def test_a_retry_whose_read_back_passes_drops_the_row_with_no_new_delete(make, bff):
    engine, reg, guard = make()
    bff.get_statuses["env-001"] = deque(["RUNNING"])
    host = launch(engine)
    engine.register(instance_id=host, tag=TAG, profile="kirocrew-main", region="")
    bff.queue("GetSandbox", 200, {"execEnvironmentId": "env-001", "status": "DELETING"})
    assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is False
    # the fake really deleted the box, so the retry's Get is 404 and List is empty
    assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is True
    assert reg.list() == [] and bff.count("DeleteSandbox") == 1


def test_teardown_is_false_when_the_row_cannot_be_removed(make, bff):
    engine, reg, _ = make()
    bff.get_statuses["env-001"] = deque(["RUNNING"])
    host = launch(engine)
    engine.register(instance_id=host, tag=TAG, profile="kirocrew-main", region="")
    reg.fail_remove = True
    assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is False


def test_teardown_when_the_client_is_stopped_makes_no_call_and_is_false(make, bff):
    engine, _, guard = make()
    bff.get_statuses["env-001"] = deque(["RUNNING"])
    launch(engine)
    guard.stop("test")
    n = len(bff.requests)
    assert engine.teardown(tag=TAG, profile="kirocrew-main", region="") is False
    assert len(bff.requests) == n


# -- helpers -------------------------------------------------------------------------


def test_ssh_alias_round_trip_and_rejects_other_hosts():
    assert sandbox_id_from_host(ssh_host_for("env-001")) == "env-001"
    for bad in ("", "ds-x", "crew-sbx-", "crew-sbx-a b", "crew-sbx-../x", None):
        assert sandbox_id_from_host(bad) == ""
