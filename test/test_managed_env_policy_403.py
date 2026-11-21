"""A policy 403 on one box does not stop the whole Crew client.

The case: one StartSshSession answered 403 ``ForbiddenException`` because the service's policy
check denied the account's profile. That 403 on a per-box action (StartSshSession, CreateSandbox,
ResumeSandbox, StartEnvironment) refuses that action only, with one plain line on that box; the
List, GetUserInfo and the other boxes keep working. Every other stop rule is unchanged.
"""

from __future__ import annotations

import json

import pytest
from managed_env_fake_bff import FakeBff, FakeTime, err
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_crews_from_list import _env
from test_managed_env_engine import TAG, ft, launch, make  # noqa: F401  (fixtures)
from test_managed_env_routes import call, own

from kiro_crew.managed_env.call_log import CallLog
from kiro_crew.managed_env.client import SandboxError
from kiro_crew.managed_env.engine import SandboxEngineError
from kiro_crew.managed_env.guard import (
    POLICY_HOLD_S,
    POLICY_TEXT,
    GuardRefused,
    is_policy_refusal,
)

#: The operator's 403, as a run read it from the call log (field names only).
RECORDED_403 = {
    "at": "2026-11-19T17:16:17Z",
    "guard": "product",
    "kind": "ForbiddenException",
    "op": "StartSshSession",
    "request_fields": ["identifier", "identifier.executionEnvironmentId"],
    "response_fields": ["__type", "message"],
    "status": 403,
}
#: The stop line the operator saw on both rows: never shown after a policy 403 now.
STOP_LINE = (
    "Crew stopped calling the sandbox service to keep your sandboxes safe. "
    "Restart the dashboard to try again."
)


@pytest.fixture
def bff():
    b = FakeBff()
    yield b
    b.close()


def _forbidden(op: str, kind: str = "ForbiddenException") -> tuple[int, dict]:
    # The recorded answer's two fields; a 403's message is never kept, so any text stands in.
    return 403, {"__type": f"com.example.kiro#{kind}", "message": "policy denies"}


def _queue_403(bff, op: str, kind: str = "ForbiddenException") -> None:
    status, body = _forbidden(op, kind)
    bff.queue(op, status, body)


def _routes(bff, tmp_path, monkeypatch, **kw):
    routes, reg, guard, client, *_ = _env(bff, tmp_path, monkeypatch, **kw)
    clock = FakeTime()
    guard._now = clock.now  # the hold's clock, moved by the test
    return routes, reg, guard, client, clock


def _items(answer: dict) -> dict:
    return {s["sandbox_id"]: s for s in answer["sandboxes"]}


def _ssh_path(sid: str) -> str:
    return f"/api/managed-environments/{sid}/ssh-session"


def test_the_recorded_ssh_403_refuses_that_box_only_and_the_list_runs_on(
    bff, tmp_path, monkeypatch
):
    routes, reg, guard, client, _ = _routes(bff, tmp_path, monkeypatch)
    log = tmp_path / "bff-calls.log"
    client._call_log = CallLog(log)
    a = own(bff, guard, reg, name="crew-sandbox-02-kc-00fcd1", crew="crew-sandbox-02")
    b = own(bff, guard, reg, name="crew-sandbox-01-kc-dfc40d", crew="crew-sandbox-01")
    assert routes.list_sync()["error"] == ""
    _queue_403(bff, "StartSshSession")
    status, body = call(routes, "POST", _ssh_path(a))
    # the box's ssh ask is refused with the plain line (the core's mint reads it), and no stop
    assert status == 424 and body["error"] == POLICY_TEXT
    assert guard.stopped == "" and not guard.stopped_by_service
    # the call log's line is the recorded one (field names only)
    line = json.loads(log.read_text().splitlines()[-1])
    assert {k: v for k, v in line.items() if k != "at"} == {
        k: v for k, v in RECORDED_403.items() if k != "at"
    }
    lists = bff.count("ListSandboxes")
    answer = routes.list_sync()
    assert bff.count("ListSandboxes") == lists + 1  # the following List is still sent
    assert answer["error"] == "" and "client_stopped" not in answer
    items = _items(answer)
    assert items[a]["status"] == "RUNNING" and items[a]["policy_line"] == POLICY_TEXT
    assert "policy_line" not in items[b]  # only that box
    assert STOP_LINE not in json.dumps(answer)
    # the other box still connects, and GetUserInfo still goes out
    status, body = call(routes, "POST", _ssh_path(b))
    assert status == 200 and body["ssmSessionId"]
    bff.queue("GetUserInfo", 200, {"email": "test-1@example.com"})
    assert client.call("GetUserInfo", {})["email"] == "test-1@example.com"
    assert guard.stopped == ""


def test_inside_the_hold_the_box_is_refused_from_memory_then_asked_again(
    bff, tmp_path, monkeypatch
):
    routes, reg, guard, client, clock = _routes(bff, tmp_path, monkeypatch)
    a = own(bff, guard, reg, name="crew-sandbox-02-kc-00fcd1", crew="crew-sandbox-02")
    routes.list_sync()
    _queue_403(bff, "StartSshSession")
    assert call(routes, "POST", _ssh_path(a))[0] == 424
    assert bff.count("StartSshSession") == 1
    # the tunnel's token refresh asks again: refused plainly, with no call
    for _ in range(3):
        status, body = call(routes, "POST", _ssh_path(a))
        assert status == 422 and body["error"] == POLICY_TEXT
    with pytest.raises(GuardRefused) as got:
        client.start_ssh_session(a)
    assert got.value.reason == "policy_denied"
    assert bff.count("StartSshSession") == 1 and guard.stopped == ""
    # after the hold one call asks again; a tuple served clears the line
    clock.advance(POLICY_HOLD_S)
    status, body = call(routes, "POST", _ssh_path(a))
    assert status == 200 and bff.count("StartSshSession") == 2
    assert "policy_line" not in _items(routes.list_sync())[a]


def test_a_new_sign_in_asks_the_service_afresh(bff, tmp_path, monkeypatch):
    routes, reg, guard, client, _ = _routes(bff, tmp_path, monkeypatch)
    a = own(bff, guard, reg, name="crew-sandbox-02-kc-00fcd1", crew="crew-sandbox-02")
    _queue_403(bff, "StartSshSession")
    with pytest.raises(SandboxError):
        client.start_ssh_session(a)
    assert guard.policy_line(a) == POLICY_TEXT
    routes._landing_list()  # the new sign-in's landing List
    assert guard.policy_line(a) == ""
    assert client.start_ssh_session(a).ssm_session_id  # asked again at once


def test_a_resume_refused_by_the_policy_reads_plainly_on_that_box_with_no_stop(
    bff, tmp_path, monkeypatch
):
    routes, reg, guard, client, _ = _routes(bff, tmp_path, monkeypatch)
    a = own(bff, guard, reg, status="STOPPED", name="crew-sandbox-02-kc-00fcd1", crew="c2")
    routes.list_sync()
    _queue_403(bff, "ResumeSandbox")
    routes._run_action(a, "resume")
    assert routes.actions.get(a)["error"] == POLICY_TEXT
    assert guard.stopped == "" and bff.count("ResumeSandbox") == 1  # no retry
    assert routes.list_sync()["error"] == ""


def test_a_create_refused_by_the_policy_says_so_plainly_with_no_stop(make, bff):  # noqa: F811
    engine, _, guard = make()
    _queue_403(bff, "CreateSandbox")
    with pytest.raises(SandboxEngineError) as got:
        launch(engine)
    assert got.value.code == "policy_denied" and str(got.value) == POLICY_TEXT
    assert guard.stopped == "" and bff.count("CreateSandbox") == 1
    assert guard.ledger() == [] and not guard.unconfirmed_path.exists()  # a definite answer
    engine.client.list_sandboxes()  # the List still works


@pytest.mark.parametrize(
    "op,kind",
    [
        ("StartSshSession", "AccessDeniedException"),  # another kind: not the policy answer
        ("CreateSandbox", "SandboxUnavailableException"),  # the feature-off 403
        ("ListSandboxes", "ForbiddenException"),  # not a per-box action
        ("GetSandbox", "ForbiddenException"),
        ("StopSandbox", "ForbiddenException"),
        ("DeleteSandbox", "ForbiddenException"),
    ],
)
def test_every_other_403_still_stops_under_the_product_rules(make, bff, op, kind):  # noqa: F811
    engine, _, guard = make()
    sid = bff.add_sandbox("RUNNING", "mine")
    guard.record_created(dict(bff.sandboxes[sid]), "mine")
    _queue_403(bff, op, kind)
    body = {"execEnvironmentId": sid}
    if op == "StartSshSession":
        body = {"identifier": {"executionEnvironmentId": sid}}
    with pytest.raises(SandboxError):
        engine.client.call(op, body)
    assert guard.stopped == f"{op} answered HTTP 403"


@pytest.mark.parametrize(
    "key,stops",
    [
        ("list_401", True),
        ("any_500", True),
        ("any_429", True),
        ("create_402", False),
    ],
)
def test_the_other_stop_rules_are_as_they_were(make, bff, key, stops):  # noqa: F811
    engine, _, guard = make()
    sid = bff.add_sandbox("RUNNING", "mine")
    guard.record_created(dict(bff.sandboxes[sid]), "mine")
    status, body = err(key)
    op = {"list_401": "ListSandboxes", "create_402": "CreateSandbox"}.get(key, "GetSandbox")
    bff.queue(op, status, body)
    with pytest.raises(SandboxError):
        engine.client.call(op, {"execEnvironmentId": sid} if op == "GetSandbox" else {})
    assert bool(guard.stopped) is stops


def test_the_policy_answer_is_matched_by_op_status_and_kind():
    for op in ("StartSshSession", "CreateSandbox", "ResumeSandbox", "StartEnvironment"):
        assert is_policy_refusal(op, 403, "ForbiddenException")
        assert not is_policy_refusal(op, 401, "ForbiddenException")
        assert not is_policy_refusal(op, 403, "AccessDeniedException")
    for op in ("ListSandboxes", "GetSandbox", "StopSandbox", "DeleteSandbox", "GetUserInfo"):
        assert not is_policy_refusal(op, 403, "ForbiddenException")


def test_the_line_is_plain():
    assert ":" not in POLICY_TEXT.split(" ", 1)[0]
    assert "Forbidden" not in POLICY_TEXT and "403" not in POLICY_TEXT
    assert "managed environments" in POLICY_TEXT and "Kiro admin" in POLICY_TEXT
