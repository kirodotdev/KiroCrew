"""The persistent-sandbox service client against a fake service (loopback, CBOR).

One case per operation and per error row of Q279 section 2, plus the bounds: no
create retry, Resume's not-ready retry and its one Get, the Get poll's early stops,
one call at a time, the 1 s gap, and no token string anywhere in the captured log or
an error.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

import pytest
from managed_env_fake_bff import (
    BAD_STATUS,
    CUT,
    DROP,
    ERROR_TABLE,
    FakeBff,
    FakeTime,
    cut_error,
    err,
)
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401

from kiro_crew.managed_env import cbor
from kiro_crew.managed_env.client import (
    BffClient,
    SandboxError,
    SandboxStateError,
    SshTuple,
)
from kiro_crew.managed_env.guard import ClientStopped, GuardRefused, SandboxGuard

TOKEN = "tok-SECRET-0123456789"
PROFILE_ARN = "arn:aws:codewhisperer:zz-example-1::profile/EXAMPLE"


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
    def build(token=TOKEN, clock=None):
        guard = SandboxGuard(tmp_path / "state", now=ft.now)
        client = BffClient(
            lambda: token,
            guard,
            endpoint=bff.endpoint,
            profile_arn=PROFILE_ARN,
            sleep=ft.sleep,
            clock=clock or ft.clock,
        )
        return client, guard

    return build


def own(bff, guard, status="RUNNING", name="mine"):
    """A sandbox the fake holds AND this client's ledger knows (as if created earlier)."""
    sid = bff.add_sandbox(status, name)
    guard.record_created(dict(bff.sandboxes[sid]), name)
    return sid


# -- the wire ------------------------------------------------------------------------


def test_wire_headers_path_and_body(make, bff):
    client, guard = make()
    sid = own(bff, guard)
    client.get_sandbox(sid)
    req = bff.requests[-1]
    assert req["path"] == "/service/KiroWebBearerService/operation/GetSandbox"
    assert req["headers"]["smithy-protocol"] == "rpc-v2-cbor"
    assert req["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert req["headers"]["X-Kiro-Idp"] == "AWSIdC"
    assert req["headers"]["X-Kiro-Profile-Arn"] == PROFILE_ARN
    assert req["body"] == {"execEnvironmentId": sid}  # and no csrfToken
    assert "X-CSRF-Token" not in req["headers"]


def test_endpoint_must_be_https_unless_loopback(make, ft, tmp_path):
    guard = SandboxGuard(tmp_path / "s", now=ft.now)
    with pytest.raises(ValueError):
        BffClient(lambda: TOKEN, guard, endpoint="http://managed-env.example.test")
    BffClient(lambda: TOKEN, guard, endpoint="https://managed-env.example.test")


def test_no_token_means_no_call(make, bff):
    client, _ = make(token="")
    with pytest.raises(SandboxError) as ei:
        client.list_sandboxes()
    assert ei.value.kind == "no_token"
    assert bff.requests == []


def test_one_call_at_a_time_and_one_second_apart(make, bff, ft):
    client, guard = make()
    sid = own(bff, guard)
    client.get_sandbox(sid)
    client.get_sandbox(sid)
    client.get_sandbox(sid)
    assert ft.sleeps == [1.0, 1.0]  # the first call does not wait; each later one does

    bff.hold = threading.Event()
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(client.get_sandbox(sid))) for _ in range(3)
    ]
    for t in threads:
        t.start()
    bff.hold.set()
    for t in threads:
        t.join(10)
    assert len(results) == 3
    assert bff.max_inflight == 1


# -- ListSandboxes -------------------------------------------------------------------


def test_list_follows_pages_and_an_empty_page_with_next_token(make, bff):
    client, guard = make()
    a, b = own(bff, guard), own(bff, guard)
    bff.list_pages = [
        {"sandboxes": [bff.sandboxes[a]]},
        {"sandboxes": []},  # empty, and it still carries a nextToken
        {"sandboxes": [bff.sandboxes[b]]},
    ]
    out = client.list_sandboxes()
    assert [e["execEnvironmentId"] for e in out] == [a, b]
    assert bff.count("ListSandboxes") == 3
    assert bff.requests[0]["body"] == {"maxResults": 50}
    assert bff.requests[1]["body"] == {"maxResults": 50, "nextToken": "page-1"}


def test_product_rules_a_list_of_only_another_clients_sandbox(make, bff):
    client, guard = make()
    mine = own(bff, guard)
    other = bff.add_other("RUNNING")
    bff.list_pages = [{"sandboxes": [dict(bff.sandboxes[other])]}]  # the List shows only other-001
    out = client.list_sandboxes()
    assert [e["execEnvironmentId"] for e in out] == ["other-001"] == [other]
    assert guard.stopped == ""
    n = len(bff.requests)
    client.get_sandbox(mine)
    assert len(bff.requests) == n + 1 and bff.requests[-1]["op"] == "GetSandbox"


def test_every_id_taking_call_on_another_clients_id_is_refused_with_no_call(make, bff):
    """Own ids only, in both modes, even right after a List showed the id."""
    client, guard = make()
    other = bff.add_other("RUNNING")
    if True:
        assert other in [e["execEnvironmentId"] for e in client.list_sandboxes()]
    calls = (
        client.get_sandbox,
        client.stop_sandbox,
        client.resume_sandbox,
        client.delete_sandbox,
        client.start_ssh_session,
        client.wait_running,
    )
    for call in calls:
        with pytest.raises(GuardRefused) as ei:
            call(other)
        assert ei.value.reason == "not_own_id"
    assert bff.naming(other) == []  # no request body ever named it
    assert [r["op"] for r in bff.requests] == ["ListSandboxes"]


def test_list_401_stops_and_keeps_no_body_text(make, bff):
    client, _ = make()
    bff.queue(
        "ListSandboxes", 401, {"__type": "x#UnauthorizedException", "message": f"bad {TOKEN}"}
    )
    with pytest.raises(SandboxError) as ei:
        client.list_sandboxes()
    assert ei.value.kind == "UnauthorizedException" and ei.value.status == 401
    assert TOKEN not in str(ei.value) and ei.value.message == ""
    with pytest.raises(ClientStopped):
        client.list_sandboxes()
    assert bff.count("ListSandboxes") == 1


# -- GetSandbox ----------------------------------------------------------------------


def test_get_returns_the_summary_and_404_is_not_found(make, bff):
    client, guard = make()
    sid = own(bff, guard)
    got = client.get_sandbox(sid)
    assert got["status"] == "RUNNING" and got["execEnvironmentId"] == sid
    bff.queue_error("get_404")
    with pytest.raises(SandboxError) as ei:
        client.get_sandbox(sid)
    assert ei.value.not_found and ei.value.kind == "ResourceNotFoundException"
    client.get_sandbox(sid)  # a 404 does not stop the client


def test_wait_running_polls_every_10s(make, bff, ft):
    client, guard = make()
    sid = own(bff, guard, "WAITING")
    bff.get_statuses[sid].extend(["PENDING", "PENDING", "RUNNING"])
    got = client.wait_running(sid)
    assert got["status"] == "RUNNING" and bff.count("GetSandbox") == 3
    assert ft.sleeps.count(10.0) == 2


@pytest.mark.parametrize("last,kind", [("FAILED", "failed"), ("DELETED", "deleted")])
def test_wait_running_ends_on_failed_or_deleted(make, bff, last, kind):
    client, guard = make()
    sid = own(bff, guard, "WAITING")
    bff.get_statuses[sid].extend(["PENDING", last])
    with pytest.raises(SandboxStateError) as ei:
        client.wait_running(sid)
    assert ei.value.kind == kind and bff.count("GetSandbox") == 2


def test_wait_running_ends_on_stopped_after_a_start_showed(make, bff):
    client, guard = make()
    sid = own(bff, guard, "WAITING")
    bff.get_statuses[sid].extend(["PENDING", "STOPPED"])
    with pytest.raises(SandboxStateError) as ei:
        client.wait_running(sid)
    assert ei.value.kind == "stopped_after_start"


def test_wait_running_keeps_polling_a_stopped_one_until_a_start_shows(make, bff):
    client, guard = make()
    sid = own(bff, guard, "STOPPED")
    bff.get_statuses[sid].extend(["STOPPED", "STOPPED", "PENDING", "RUNNING"])
    assert client.wait_running(sid)["status"] == "RUNNING"
    assert bff.count("GetSandbox") == 4


def test_wait_running_gives_up_after_90_polls(make, bff, ft):
    client, guard = make()
    sid = own(bff, guard, "PENDING")  # stays PENDING
    with pytest.raises(SandboxStateError) as ei:
        client.wait_running(sid)
    assert ei.value.kind == "poll_timeout"
    assert bff.count("GetSandbox") == 90
    assert ft.sleeps.count(10.0) == 89


def test_wait_running_stops_early_on_a_non_200(make, bff):
    client, guard = make()
    sid = own(bff, guard, "PENDING")
    bff.queue_error("get_404")
    with pytest.raises(SandboxError):
        client.wait_running(sid)
    assert bff.count("GetSandbox") == 1


# -- CreateSandbox -------------------------------------------------------------------


def test_create_lists_first_calls_once_and_records_the_id(make, bff):
    client, guard = make()
    out = client.create_sandbox("crew-tag-1")
    assert bff.ops() == ["ListSandboxes", "CreateSandbox"]
    assert bff.requests[1]["body"] == {"displayName": "crew-tag-1"}
    assert out["status"] == "WAITING"
    assert guard.is_own(out["execEnvironmentId"])


@pytest.mark.parametrize("name", ["", "x" * 129, None])
def test_create_validates_the_display_name_before_any_call(make, bff, name):
    client, _ = make()
    with pytest.raises(ValueError):
        client.create_sandbox(name)
    assert bff.requests == []


@pytest.mark.parametrize(
    "key,stops",
    [
        ("create_429", False),  # the instance's cap does not stop
        ("create_403", True),
        ("create_400", False),
    ],
)
def test_product_rules_create_errors_are_not_retried_and_nothing_is_counted(make, bff, key, stops):
    client, guard = make()
    bff.queue_error(key)
    with pytest.raises(SandboxError) as ei:
        client.create_sandbox("t")
    assert ei.value.kind == ERROR_TABLE[key][2]
    assert bff.count("CreateSandbox") == 1  # no retry
    assert guard.ledger() == []
    assert bool(guard.stopped) is stops
    assert not guard.unconfirmed_path.exists()  # a definite answer: no hold


@pytest.mark.parametrize("key,status", [("create_429", 429), ("create_402", 402)])
def test_product_rules_the_cap_refusal_carries_the_lists_counts_and_no_stop(make, bff, key, status):
    client, guard = make()
    mine = own(bff, guard, "STOPPED")
    bff.sandboxes[mine]["status"] = "DELETED"  # gone: the fake lists it no more
    others = [bff.add_other("STOPPED") for _ in range(3)]
    bff.queue_error(key)
    with pytest.raises(SandboxError) as ei:
        client.create_sandbox("t")
    assert (ei.value.status, ei.value.kind) == (status, "ServiceQuotaExceededException")
    assert (ei.value.listed, ei.value.listed_other) == (3, 3)
    assert bff.count("CreateSandbox") == 1
    assert guard.stopped == ""
    assert [e["execEnvironmentId"] for e in client.list_sandboxes()] == others  # the client runs on


def test_a_throttle_on_create_still_stops_the_client(make, bff):
    client, guard = make()
    bff.queue_error("create_429_throttle")
    with pytest.raises(SandboxError) as ei:
        client.create_sandbox("t")
    assert (ei.value.status, ei.value.kind) == (429, "ThrottlingException")
    assert ei.value.listed is None
    assert guard.stopped
    with pytest.raises(ClientStopped):
        client.list_sandboxes()


def test_product_rules_a_dropped_answer_is_recorded_not_counted_and_holds_a_relaunch(make, bff, ft):
    client, guard = make()
    bff.queue("CreateSandbox", DROP)
    with pytest.raises(SandboxError) as ei:
        client.create_sandbox("t")
    assert ei.value.kind == "unreachable"
    assert bff.count("CreateSandbox") == 1
    assert guard.unconfirmed_path.exists() and guard.ledger() == []
    ft.advance(5)
    with pytest.raises(GuardRefused) as held:
        client.create_sandbox("t2")
    assert held.value.reason == "create_unconfirmed" and bff.count("CreateSandbox") == 1


# -- Stop / Resume / Delete ----------------------------------------------------------


def test_stop_is_idempotent_and_409_is_final(make, bff):
    client, guard = make()
    sid = own(bff, guard)
    assert client.stop_sandbox(sid)["status"] == "STOPPED"
    assert client.stop_sandbox(sid)["status"] == "STOPPED"
    bff.queue_error("stop_409")
    with pytest.raises(SandboxError) as ei:
        client.stop_sandbox(sid)
    assert ei.value.kind == "ConflictException" and ei.value.status == 409
    assert bff.count("StopSandbox") == 3  # the 409 was not retried
    assert not guard.stopped


NOT_READY = ["resume_409_service", "resume_409_text"]  # the service's own kind; the service's text


@pytest.mark.parametrize("key", NOT_READY)
def test_resume_retries_a_not_ready_409_every_3s_with_one_get(make, bff, ft, key):
    client, guard = make()
    sid = own(bff, guard, "STOPPED")
    bff.get_statuses[sid] = deque(["STOPPED"])
    bff.queue_error(key)
    bff.queue_error(key)
    out = client.resume_sandbox(sid)
    assert out["status"] == "PENDING"
    assert bff.ops() == ["ResumeSandbox", "GetSandbox", "ResumeSandbox", "ResumeSandbox"]
    assert not bff.get_statuses[sid]  # the one Get answered STOPPED
    assert ft.sleeps == [1.0, 3.0, 3.0]  # the client's 1 s gap before the Get, then two 3 s retries


@pytest.mark.parametrize("key", NOT_READY)
def test_resume_gives_up_after_60s_with_one_get(make, bff, ft, key):
    client, guard = make()
    sid = own(bff, guard, "STOPPED")
    start = ft.clock()
    for _ in range(40):
        bff.queue_error(key)
    with pytest.raises(SandboxError) as ei:
        client.resume_sandbox(sid)
    assert ei.value.status == 409 and ei.value.op == "ResumeSandbox"
    assert ft.clock() - start <= 60.0
    assert bff.count("GetSandbox") == 1
    assert bff.count("ResumeSandbox") == 20  # t = 0, then 4, 7, ... 58 (the Get's 1 s gap at t = 1)


@pytest.mark.parametrize("key", ["resume_409_other", "resume_400_kind", "resume_400_text"])
def test_resume_other_409_text_or_the_kind_on_another_status_is_final(make, bff, ft, key):
    client, guard = make()
    sid = own(bff, guard, "STOPPED")
    bff.queue_error(key)
    with pytest.raises(SandboxError) as ei:
        client.resume_sandbox(sid)
    assert ei.value.status == ERROR_TABLE[key][1] and ei.value.kind == ERROR_TABLE[key][2]
    assert bff.ops() == ["ResumeSandbox"] and ft.sleeps == []  # no Get, no retry


@pytest.mark.parametrize("key", NOT_READY)
# PENDING and RUNNING now end the Resume with no repeat (test_managed_env_resume_busy_line.py)
@pytest.mark.parametrize("status", ["WAITING", "HIBERNATING"])
def test_resume_keeps_retrying_on_any_status_but_failed_or_deleted(make, bff, ft, key, status):
    client, guard = make()
    sid = own(bff, guard, "STOPPED")
    bff.get_statuses[sid] = deque([status])
    bff.queue_error(key)
    bff.queue_error(key)
    out = client.resume_sandbox(sid)
    assert out["status"] == "PENDING" and bff.count("ResumeSandbox") == 3
    assert bff.count("GetSandbox") == 1 and not bff.get_statuses[sid]


@pytest.mark.parametrize("key", NOT_READY)
@pytest.mark.parametrize(
    "got, kind, word",
    [("FAILED", "failed", "FAILED"), ("DELETED", "deleted", "DELETED"), ("404", "gone", "gone")],
)
def test_resume_ends_at_once_on_a_failed_deleted_or_gone_sandbox(
    make, bff, ft, key, got, kind, word
):
    client, guard = make()
    sid = own(bff, guard, "STOPPED")
    for _ in range(40):  # it would answer not ready for the whole window
        bff.queue_error(key)
    if got == "404":
        bff.queue_error("get_404")
    else:
        bff.get_statuses[sid] = deque([got])
    with pytest.raises(SandboxStateError) as ei:
        client.resume_sandbox(sid)
    assert ei.value.kind == kind
    assert str(ei.value) == f"Resume cannot start sandbox {sid}: it is {word}"
    assert bff.ops() == ["ResumeSandbox", "GetSandbox"]
    assert ft.sleeps == [1.0]  # the Get's gap only: no retry
    assert not guard.stopped


@pytest.mark.parametrize("key", NOT_READY)
def test_resume_raises_the_get_error_when_the_get_fails(make, bff, ft, key):
    client, guard = make()
    sid = own(bff, guard, "STOPPED")
    for _ in range(40):
        bff.queue_error(key)
    bff.queue_error("any_500")
    with pytest.raises(SandboxError) as ei:
        client.resume_sandbox(sid)
    assert ei.value.op == "GetSandbox" and ei.value.status == 500
    assert guard.stopped
    with pytest.raises(ClientStopped):
        client.resume_sandbox(sid)
    assert bff.ops() == ["ResumeSandbox", "GetSandbox"]  # no more calls


@pytest.mark.parametrize("key", NOT_READY)
def test_resume_raises_a_get_answer_without_a_status(make, bff, ft, key):
    client, guard = make()
    sid = own(bff, guard, "STOPPED")
    for _ in range(40):
        bff.queue_error(key)
    bff.queue("GetSandbox", 200, {"execEnvironmentId": sid})  # no status member
    with pytest.raises(SandboxError) as ei:
        client.resume_sandbox(sid)
    assert ei.value.op == "GetSandbox" and ei.value.kind == "bad_response"
    assert bff.ops() == ["ResumeSandbox", "GetSandbox"]  # the retry did not go on


@pytest.mark.parametrize("key", NOT_READY)
@pytest.mark.parametrize("got", ["STOPPED", "FAILED"])
def test_resume_whose_first_409_comes_late_still_gets_the_one_get(make, bff, ft, key, got):
    """The first ResumeSandbox takes 58 s (the call timeout is 60 s): the Get still runs."""

    def clock():
        return ft.clock() + (58.0 if bff.count("ResumeSandbox") else 0.0)

    client, guard = make(clock=clock)
    sid = own(bff, guard, "STOPPED")
    for _ in range(40):
        bff.queue_error(key)
    bff.get_statuses[sid] = deque([got])
    if got == "FAILED":
        with pytest.raises(SandboxStateError) as es:
            client.resume_sandbox(sid)
        assert es.value.kind == "failed"
    else:
        with pytest.raises(SandboxError) as ee:
            client.resume_sandbox(sid)
        assert ee.value.op == "ResumeSandbox" and ee.value.status == 409  # the window is spent
    assert bff.ops() == ["ResumeSandbox", "GetSandbox"]
    assert not bff.get_statuses[sid]


@pytest.mark.parametrize("key", ["list_401", "any_500", "any_503", "any_429"])
def test_resume_401_or_5xx_stops_the_client_with_no_get(make, bff, key):
    client, guard = make()
    sid = own(bff, guard, "STOPPED")
    bff.queue_error(key, op="ResumeSandbox")
    with pytest.raises(SandboxError) as ei:
        client.resume_sandbox(sid)
    assert ei.value.status == ERROR_TABLE[key][1]
    assert guard.stopped
    with pytest.raises(ClientStopped):
        client.get_sandbox(sid)
    assert bff.ops() == ["ResumeSandbox"]


def test_resume_403_refuses_the_resume_only_under_the_product_rules(make, bff):
    # the policy 403 on a per-box action refuses that action; the client runs on
    client, guard = make()
    sid = own(bff, guard, "STOPPED")
    bff.queue_error("resume_403")
    with pytest.raises(SandboxError) as ei:
        client.resume_sandbox(sid)
    assert ei.value.kind == "ForbiddenException" and ei.value.message == ""
    assert not guard.stopped and bff.count("ResumeSandbox") == 1
    assert client.get_sandbox(sid)["status"] == "STOPPED"


def test_delete_own_id_then_404_means_gone(make, bff):
    client, guard = make()
    sid = own(bff, guard)
    assert client.delete_sandbox(sid)["status"] == "DELETED"
    again = client.delete_sandbox(sid)  # the fake answers 404 now
    assert again["status"] == "DELETED" and again["gone"] is True
    assert bff.count("DeleteSandbox") == 2


def test_delete_of_an_id_not_in_the_ledger_is_refused_with_no_call(make, bff):
    client, guard = make()
    foreign = bff.add_sandbox("RUNNING", "somebody-else")
    with pytest.raises(GuardRefused) as ei:
        client.delete_sandbox(foreign)
    assert ei.value.reason == "not_own_id"
    assert bff.requests == []


def test_a_display_name_match_never_selects_a_delete(make, bff):
    client, guard = make()
    twin = bff.add_sandbox("RUNNING", "crew-tag-1")  # same name, not ours
    mine = own(bff, guard, name="crew-tag-1")
    client.delete_sandbox(mine)
    assert bff.sandboxes[twin]["status"] == "RUNNING"
    with pytest.raises(GuardRefused):
        client.delete_sandbox(twin)


# -- StartSshSession -----------------------------------------------------------------


def test_ssh_session_uses_execution_environment_id_and_a_new_tuple_each_time(make, bff):
    client, guard = make()
    sid = own(bff, guard)
    a = client.start_ssh_session(sid)
    b = client.start_ssh_session(sid)
    assert isinstance(a, SshTuple) and a.ssm_session_id != b.ssm_session_id
    assert a.stream_url != b.stream_url and a.token_value != b.token_value
    assert bff.requests[0]["body"] == {"identifier": {"executionEnvironmentId": sid}}
    assert "STREAMSECRET" not in repr(a) + str(a) and "MGSSECRET" not in repr(a) + str(a)


def test_ssh_409_is_surfaced_and_never_resumes(make, bff):
    client, guard = make()
    sid = own(bff, guard, "STOPPED")
    with pytest.raises(SandboxError) as ei:
        client.start_ssh_session(sid)
    assert ei.value.kind == "EnvironmentNotReadyException" and ei.value.status == 409
    assert bff.ops() == ["StartSshSession"]  # one try: no retry, no ResumeSandbox
    assert not guard.stopped


def test_ssh_404_for_a_made_up_id_is_refused_before_the_call(make, bff):
    client, _ = make()
    with pytest.raises(GuardRefused):
        client.start_ssh_session("env-made-up")
    assert bff.requests == []


def test_ssh_answer_missing_a_member_is_an_error(make, bff):
    client, guard = make()
    sid = own(bff, guard)
    bff.queue("StartSshSession", 200, {"ssmSessionId": "s"})
    with pytest.raises(SandboxError) as ei:
        client.start_ssh_session(sid)
    assert ei.value.kind == "bad_response"


# -- stop rules for every operation ---------------------------------------------------


@pytest.mark.parametrize("key", ["any_500", "any_503", "any_429"])
def test_5xx_and_429_stop_without_retry(make, bff, key):
    client, guard = make()
    sid = own(bff, guard)
    bff.queue_error(key)
    with pytest.raises(SandboxError):
        client.get_sandbox(sid)
    assert guard.stopped
    with pytest.raises(ClientStopped):
        client.get_sandbox(sid)
    assert bff.count("GetSandbox") == 1


def test_an_error_body_without_a_type_is_still_an_error(make, bff):
    client, guard = make()
    sid = own(bff, guard)
    bff.queue("GetSandbox", 502, {})  # empty body: no __type
    with pytest.raises(SandboxError) as ei:
        client.get_sandbox(sid)
    assert ei.value.kind == "HTTP502" and guard.stopped


@pytest.mark.parametrize("lost,cls", [(CUT, "IncompleteRead"), (BAD_STATUS, "BadStatusLine")])
@pytest.mark.parametrize(
    "op",
    [
        "ListSandboxes",
        "GetSandbox",
        "StopSandbox",
        "ResumeSandbox",
        "DeleteSandbox",
        "StartSshSession",
        "GetNetworkConfiguration",
        "UpdateNetworkConfiguration",
    ],
)
def test_a_cut_answer_or_a_bad_status_line_is_unreachable_on_every_operation(
    make, bff, op, lost, cls
):
    """The shared wire call: a lost answer is a ``SandboxError``, not a raw http.client error."""
    client, guard = make()
    sid = own(bff, guard)
    calls = {
        "ListSandboxes": client.list_sandboxes,
        "GetSandbox": lambda: client.get_sandbox(sid),
        "StopSandbox": lambda: client.stop_sandbox(sid),
        "ResumeSandbox": lambda: client.resume_sandbox(sid),
        "DeleteSandbox": lambda: client.delete_sandbox(sid),
        "StartSshSession": lambda: client.start_ssh_session(sid),
        "GetNetworkConfiguration": client.netcfg_get,
        "UpdateNetworkConfiguration": lambda: client.netcfg_set("INTEGRATIONS_ONLY", []),
    }
    bff.sandboxes[sid]["status"] = "STOPPED" if op == "ResumeSandbox" else "RUNNING"
    bff.queue(op, lost)
    with pytest.raises(SandboxError) as ei:
        calls[op]()
    got = (ei.value.op, ei.value.kind, ei.value.status, ei.value.message)
    assert got == (op, "unreachable", None, cls)
    assert bff.count(op) == 1 and guard.stopped == ""  # no retry, no stop
    assert not guard.unconfirmed_path.exists()  # only a CreateSandbox starts the hold


@pytest.mark.parametrize(
    "op,key,stops",
    [
        ("GetSandbox", "any_503", True),
        ("StopSandbox", "any_500", True),
        ("GetSandbox", "get_404", False),  # the status is kept: a 404 still reads as not found
        # the instance's cap loses its kind with its body, so it reads as a throttle: fail closed
        ("CreateSandbox", "create_429", True),
    ],
)
def test_an_error_answer_with_a_cut_body_keeps_its_status_and_stop_rule(make, bff, op, key, stops):
    client, guard = make()
    sid = "" if op == "CreateSandbox" else own(bff, guard)  # a create needs no live sandbox
    status, body = err(key)
    bff.queue(op, cut_error(status), body)
    calls = {
        "GetSandbox": lambda: client.get_sandbox(sid),
        "StopSandbox": lambda: client.stop_sandbox(sid),
        "CreateSandbox": lambda: client.create_sandbox("t"),
    }
    with pytest.raises(SandboxError) as ei:
        calls[op]()
    assert (ei.value.op, ei.value.status, ei.value.kind) == (op, status, f"HTTP{status}")
    assert bool(guard.stopped) is stops
    assert ei.value.not_found is (status == 404)
    assert not guard.unconfirmed_path.exists()  # an answer with a status: no hold


def test_unreachable_endpoint(make, bff):
    client, guard = make()
    sid = own(bff, guard)
    bff.close()
    with pytest.raises(SandboxError) as ei:
        client.get_sandbox(sid)
    assert ei.value.kind == "unreachable"
    assert TOKEN not in str(ei.value)


# -- no token string in the log or in an error ----------------------------------------


def test_no_token_string_in_the_captured_log_or_errors(make, bff, caplog):
    caplog.set_level(logging.DEBUG)
    client, guard = make()
    created = client.create_sandbox("t")
    sid = created["execEnvironmentId"]
    bff.sandboxes[sid]["status"] = "RUNNING"
    tup = client.start_ssh_session(sid)
    client.stop_sandbox(sid)
    errors = []
    for key in ("get_404", "any_500"):
        status, body = err(key)
        body["message"] = f"your token {TOKEN} is bad"  # the server echoes the token
        bff.queue("GetSandbox", status, body)
        try:
            client.get_sandbox(sid)
        except SandboxError as e:
            errors.append(str(e))
    assert errors and all(TOKEN not in e for e in errors)
    text = caplog.text
    assert text  # something was logged
    assert TOKEN not in text
    for secret in (tup.stream_url, tup.token_value):
        assert secret not in text
    assert guard.stopped


def test_cbor_lift_round_trips():
    obj = {"a": [1, -2, "x", b"y", None, True], "b": {"c": 1.5}}
    assert cbor.loads(cbor.dumps(obj)) == obj


# -- redirects, orphans, deleted ids ---------------------------------------------------


def test_a_redirect_is_never_followed(make, bff, ft, tmp_path):
    other = FakeBff()  # the redirect target: it must see no request at all
    try:
        client, guard = make()
        sid = own(bff, guard)
        bff.queue(
            "GetSandbox",
            302,
            {"__location": other.endpoint + "/service/KiroWebBearerService/operation/GetSandbox"},
        )
        with pytest.raises(SandboxError) as ei:
            client.get_sandbox(sid)
        assert ei.value.status == 302 and ei.value.kind == "HTTP302"
        assert other.requests == []
        assert TOKEN not in str(ei.value)
    finally:
        other.close()


@pytest.mark.parametrize("code", [301, 303, 307, 308])
def test_every_3xx_is_an_error_not_a_hop(make, bff, code):
    other = FakeBff()
    try:
        client, guard = make()
        sid = own(bff, guard)
        bff.queue("GetSandbox", code, {"__location": other.endpoint + "/x"})
        with pytest.raises(SandboxError) as ei:
            client.get_sandbox(sid)
        assert ei.value.status == code and other.requests == []
    finally:
        other.close()


def test_product_rules_an_answer_without_an_id_is_recorded_and_holds_a_relaunch(make, bff, ft):
    client, guard = make()
    bff.queue("CreateSandbox", 200, {"status": "WAITING"})
    with pytest.raises(GuardRefused) as ei:
        client.create_sandbox("crew-tag-9")
    assert ei.value.reason == "create_no_id"
    assert "crew-tag-9" in str(ei.value) and "orphan" in str(ei.value)
    assert "recorded" in str(ei.value) and "counted" not in str(ei.value)
    assert bff.count("CreateSandbox") == 1
    assert guard.unconfirmed_path.exists() and guard.ledger() == []
    ft.advance(5)
    with pytest.raises(GuardRefused) as held:
        client.create_sandbox("crew-tag-10")
    assert held.value.reason == "create_unconfirmed" and "crew-tag-9" in str(held.value)
    assert bff.count("CreateSandbox") == 1


def test_ssh_on_a_deleted_id_is_404(make, bff):
    client, guard = make()
    sid = own(bff, guard)
    client.delete_sandbox(sid)
    with pytest.raises(SandboxError) as ei:
        client.start_ssh_session(sid)
    assert ei.value.status == 404 and ei.value.kind == "ResourceNotFoundException"
