"""The client's service call log: op, status, field names; never a value."""

from __future__ import annotations

import json
import stat

import pytest
from managed_env_fake_bff import FakeBff, FakeTime
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401

from kiro_crew.managed_env import call_log as cl
from kiro_crew.managed_env.client import BffClient, SandboxError
from kiro_crew.managed_env.guard import SandboxGuard

TOKEN = "tok-SECRET-0123456789"


@pytest.fixture
def rig(tmp_path):
    bff = FakeBff()
    ft = FakeTime()
    guard = SandboxGuard(tmp_path / "state", now=ft.now)
    log = cl.CallLog(tmp_path / "logs" / "bff-calls.log")
    client = BffClient(
        lambda: TOKEN, guard, endpoint=bff.endpoint, sleep=ft.sleep, clock=ft.clock, call_log=log
    )
    yield client, bff, log, guard
    bff.close()


def lines(log):
    return [json.loads(x) for x in log.path.read_text().splitlines()]


def test_every_call_is_one_line_with_status_and_field_names(rig):
    client, bff, log, guard = rig
    created = client.create_sandbox("kc-abc123")
    sid = created["execEnvironmentId"]
    client.get_sandbox(sid)
    client.stop_sandbox(sid)
    client.delete_sandbox(sid)
    rows = lines(log)
    assert [r["op"] for r in rows] == [
        "ListSandboxes",
        "CreateSandbox",
        "GetSandbox",
        "StopSandbox",
        "DeleteSandbox",
    ]
    assert all(r["status"] == 200 and r["at"].endswith("Z") for r in rows)
    create = rows[1]
    assert create["request_fields"] == ["displayName"]
    assert {"execEnvironmentId", "status", "displayName", "createdAt"} <= set(
        create["response_fields"]
    )
    assert rows[0]["response_fields"] == ["sandboxes"] and rows[0]["request_fields"] == [
        "maxResults"
    ]


def test_no_value_reaches_the_log(rig):
    client, bff, log, guard = rig
    sid = client.create_sandbox("kc-VALUE-xyz")["execEnvironmentId"]
    bff.sandboxes[sid]["status"] = "RUNNING"
    client.start_ssh_session(sid)
    text = log.path.read_text()
    for secret in (TOKEN, "kc-VALUE-xyz", sid, "STREAMSECRET", "MGSSECRET", "wss://", "ssm-"):
        assert secret not in text
    ssh = [r for r in lines(log) if r["op"] == "StartSshSession"][0]
    assert ssh["request_fields"] == ["identifier", "identifier.executionEnvironmentId"]
    assert ssh["response_fields"] == ["ssmSessionId", "streamUrl", "tokenValue"]


def test_errors_log_status_and_kind_and_answer_fields_only(rig):
    client, bff, log, guard = rig
    sid = client.create_sandbox("kc-aaaaaa")["execEnvironmentId"]
    bff.queue_error("get_404")
    with pytest.raises(SandboxError):
        client.get_sandbox(sid)
    bff.queue_error("any_500")
    with pytest.raises(SandboxError):
        client.get_sandbox(sid)
    last = lines(log)[-2:]
    assert last[0]["status"] == 404 and last[0]["kind"] == "ResourceNotFoundException"
    assert last[1]["status"] == 500 and last[1]["kind"] == "InternalFailureException"
    assert set(last[0]["response_fields"]) == {"__type", "message"}
    assert "no such sandbox" not in log.path.read_text() and "boom" not in log.path.read_text()


def test_no_answer_logs_a_null_status(tmp_path):
    ft = FakeTime()
    guard = SandboxGuard(tmp_path / "state", now=ft.now)
    log = cl.CallLog(tmp_path / "bff-calls.log")
    client = BffClient(
        lambda: TOKEN,
        guard,
        endpoint="http://127.0.0.1:1",
        sleep=ft.sleep,
        clock=ft.clock,
        call_log=log,
    )
    with pytest.raises(SandboxError):
        client.call("ListSandboxes", {"maxResults": 1})
    row = lines(log)[0]
    assert row["status"] is None and row["kind"] == "unreachable"


def test_file_is_0600_and_a_write_failure_never_breaks_a_call(rig, tmp_path):
    client, bff, log, guard = rig
    client.list_sandboxes()
    assert stat.S_IMODE(log.path.stat().st_mode) == 0o600
    broken = cl.CallLog(tmp_path / "bff-calls.log" / "child")  # parent is a file: OSError
    client._call_log = broken
    assert client.list_sandboxes() == []


def test_a_fresh_env_path_gets_the_line_and_nothing_else_does(tmp_path, monkeypatch):
    """A run with KIROCREW_SBX_CALL_LOG set writes only that fresh file."""
    bff = FakeBff()
    try:
        fresh = tmp_path / "run" / "fresh-calls.log"
        state = tmp_path / "state"
        monkeypatch.setenv(cl.ENV_PATH, str(fresh))
        assert not fresh.exists()
        ft = FakeTime()
        guard = SandboxGuard(state, now=ft.now)
        client = BffClient(
            lambda: TOKEN,
            guard,
            endpoint=bff.endpoint,
            sleep=ft.sleep,
            clock=ft.clock,
            call_log=cl.from_env(state),
        )
        client.list_sandboxes()
        rows = lines(cl.CallLog(fresh))
        assert [(r["op"], r["guard"]) for r in rows] == [("ListSandboxes", "product")]
        assert not (state / cl.FILE_NAME).exists()
        assert sorted(p.name for p in tmp_path.rglob("*.log")) == ["fresh-calls.log"]
    finally:
        bff.close()


def test_field_names_are_bounded_and_keys_are_sanitised():
    obj = {"a": {"b": {"c": {"d": 1}}}, "sandboxes": [{"x": 1}], "bad key with spaces/SECRET": 1}
    names = cl.field_names(obj)
    assert "a.b.c" in names and "a.b.c.d" not in names and "sandboxes[].x" in names
    assert not any("SECRET" in n for n in names) and "<key>" in names
    assert len(cl.field_names({f"k{i}": 1 for i in range(500)})) == 64


def test_env_path_overrides_the_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(cl.ENV_PATH, str(tmp_path / "run" / "calls.log"))
    assert cl.from_env(tmp_path / "state").path == tmp_path / "run" / "calls.log"
    monkeypatch.delenv(cl.ENV_PATH)
    assert cl.from_env(tmp_path / "state").path == tmp_path / "state" / cl.FILE_NAME
