"""The managed-environment client against a loopback fake service."""

from __future__ import annotations

import logging
import stat
import sys

import pytest
from managed_env_fake_service import FakeService

from kiro_crew.managed_env import guard as guard_mod
from kiro_crew.managed_env.client import (
    Credential,
    GuardRefused,
    SandboxClient,
    SandboxError,
    SandboxStateError,
)
from kiro_crew.managed_env.guard import SandboxGuard
from kiro_crew.managed_env.settings import LaneSettings

TOKEN = "tok-secret-value"
ARN = "arn:aws:codewhisperer:ex-test-1:000000000000:profile/EXAMPLE"


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def svc():
    f = FakeService()
    yield f
    f.close()


@pytest.fixture
def world(svc, tmp_path):
    clock = Clock()
    state = {"cred": Credential(TOKEN, "sign-in-a", ARN)}
    guard = SandboxGuard(tmp_path / "state", now=clock)
    sleeps: list[float] = []
    client = SandboxClient(
        lambda: state["cred"],
        guard,
        settings=lambda: LaneSettings(endpoint=svc.endpoint),
        sleep=sleeps.append,
    )
    return client, guard, clock, state, sleeps


def _own(client, svc) -> str:
    return client.create_sandbox("crew-a")["execEnvironmentId"]


# ---- what is sent, and to where ------------------------------------------------------


def test_a_call_carries_the_bearer_profile_and_protocol(world, svc):
    client, *_ = world
    client.list_sandboxes()
    headers = {k.lower(): v for k, v in svc.requests[0]["headers"].items()}
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["x-kiro-profile-arn"] == ARN
    assert headers["smithy-protocol"] == "rpc-v2-cbor"


def test_a_sign_in_with_no_profile_sends_no_profile_header(world, svc):
    client, _, _, state, _ = world
    state["cred"] = Credential(TOKEN, "sign-in-a")
    client.list_sandboxes()
    assert "x-kiro-profile-arn" not in {k.lower() for k in svc.requests[0]["headers"]}


def test_a_missing_endpoint_sends_nothing_and_names_the_key(world, svc):
    client, *_ = world
    client._settings = lambda: LaneSettings()
    with pytest.raises(SandboxError) as ei:
        client.list_sandboxes()
    assert ei.value.kind == "not_configured" and "managed_env_endpoint" in str(ei.value)
    assert svc.requests == []


@pytest.mark.parametrize("cred", [Credential(""), None])
def test_no_token_sends_nothing(world, svc, cred):
    client, _, _, state, _ = world

    def broken():
        raise RuntimeError(f"refresh failed: {TOKEN}")

    client._credential = (lambda: cred) if cred else broken
    with pytest.raises(SandboxError) as ei:
        client.list_sandboxes()
    assert ei.value.kind == "no_token" and TOKEN not in str(ei.value)
    assert svc.requests == []


def test_a_redirect_is_not_followed(world, svc):
    client, *_ = world
    svc.redirect = True
    with pytest.raises(SandboxError) as ei:
        client.list_sandboxes()
    assert ei.value.status == 302
    assert len(svc.requests) == 1


def test_the_bearer_is_in_no_error_and_no_log(world, svc, caplog):
    client, *_ = world
    caplog.set_level(logging.DEBUG)
    svc.answer("ListSandboxes", 404, {"__type": f"x#{TOKEN}", "message": "m"})
    svc.answer("ListSandboxes", 404, {"__type": "x#Not a type: tok", "message": "m"})
    svc.answer("ListSandboxes", 400, {"__type": "x#ValidationException", "message": f"bad {TOKEN}"})
    svc.answer("ListSandboxes", 403, {"__type": "x#AccessDenied", "message": f"echo {TOKEN}"})
    for _ in range(4):
        with pytest.raises(SandboxError) as ei:
            client.list_sandboxes()
        assert TOKEN not in str(ei.value) and "Not a type" not in str(ei.value)
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(Credential(TOKEN, "x"))


# ---- ids ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    ["get_sandbox", "stop_sandbox", "resume_sandbox", "delete_sandbox", "start_ssh_session"],
)
def test_an_id_crew_did_not_create_is_refused_with_no_call(world, svc, call):
    client, *_ = world
    other = svc.add("RUNNING", "crew-a")  # even with a Crew-looking name
    with pytest.raises(GuardRefused) as ei:
        getattr(client, call)(other)
    assert ei.value.reason == "not_own_id"
    assert svc.requests == []


def test_own_ids_take_every_action(world, svc):
    client, guard, *_ = world
    sid = _own(client, svc)
    assert client.get_sandbox(sid)["status"] == "PENDING"
    assert client.stop_sandbox(sid)["status"] == "STOPPING"
    assert client.resume_sandbox(sid)["status"] == "PENDING"
    session = client.start_ssh_session(sid)
    assert session.session_id == "s-1" and "tv" not in repr(session)
    assert client.delete_sandbox(sid)["status"] == "DELETING"
    assert sid not in guard.own_ids()
    # a repeated Delete reads the 404 as gone; nothing else may use the id now
    assert client.delete_sandbox(sid)["gone"] is True
    with pytest.raises(GuardRefused):
        client.get_sandbox(sid)


def test_an_incomplete_ssh_session_is_refused(world, svc):
    client, *_ = world
    sid = _own(client, svc)
    svc.answer("StartSshSession", 200, {"ssmSessionId": "s", "streamUrl": "", "tokenValue": "t"})
    with pytest.raises(SandboxError) as ei:
        client.start_ssh_session(sid)
    assert ei.value.kind == "bad_reply"


def test_the_list_reads_every_page(world, svc):
    client, *_ = world
    for _ in range(5):
        svc.add()
    svc.page_size = 2
    assert len(client.list_sandboxes()) == 5
    assert svc.ops() == ["ListSandboxes"] * 3


# ---- Create --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        (200, b"\xff"),  # a 200 that is not CBOR
        (200, {"status": "PENDING"}),  # a 200 with no id
    ],
)
def test_a_lost_create_reply_is_recorded_and_pauses_new_creates(world, svc, script):
    client, guard, clock, *_ = world
    svc.answer("CreateSandbox", *script)
    with pytest.raises((SandboxError, GuardRefused)):
        client.create_sandbox("crew-a")
    assert (guard._dir / guard_mod.UNCONFIRMED_NAME).exists()
    with pytest.raises(GuardRefused) as ei:
        client.create_sandbox("crew-b")
    assert ei.value.reason == "create_unconfirmed"
    assert svc.ops().count("CreateSandbox") == 1  # never retried
    clock.t += guard_mod.HOLD_AFTER_UNCONFIRMED_S
    assert client.create_sandbox("crew-b")["status"] == "PENDING"


def test_an_unreachable_create_is_recorded(world, svc):
    client, guard, *_ = world
    svc.close()
    with pytest.raises(SandboxError) as ei:
        client.create_sandbox("crew-a")
    assert ei.value.kind == "unreachable"
    assert (guard._dir / guard_mod.UNCONFIRMED_NAME).exists()


def test_a_create_whose_attempt_cannot_be_recorded_sends_nothing(world, svc, monkeypatch):
    client, guard, *_ = world

    def full(name, data):
        raise OSError("no space")

    monkeypatch.setattr(guard, "_write", full)
    with pytest.raises(GuardRefused) as ei:
        client.create_sandbox("crew-a")
    assert ei.value.reason == "state_unwritable"
    assert svc.requests == []


def test_a_create_whose_ledger_write_fails_keeps_the_pause(world, svc, monkeypatch):
    client, guard, *_ = world
    real = guard._write

    def ledger_full(name, data):
        if name == guard_mod.LEDGER_NAME:
            raise OSError("no space")
        real(name, data)

    monkeypatch.setattr(guard, "_write", ledger_full)
    with pytest.raises(OSError):
        client.create_sandbox("crew-a")
    monkeypatch.setattr(guard, "_write", real)
    with pytest.raises(GuardRefused) as ei:
        client.create_sandbox("crew-b")
    assert ei.value.reason == "create_unconfirmed"
    assert svc.ops().count("CreateSandbox") == 1


def test_a_refused_create_clears_its_record(world, svc):
    client, guard, *_ = world
    svc.answer("CreateSandbox", 400, {"__type": "x#ValidationException", "message": "bad"})
    with pytest.raises(SandboxError):
        client.create_sandbox("crew-a")
    assert not (guard._dir / guard_mod.UNCONFIRMED_NAME).exists()
    assert client.create_sandbox("crew-b")["status"] == "PENDING"


def test_a_create_at_the_accounts_cap_is_not_a_backoff(world, svc):
    client, *_ = world
    svc.answer("CreateSandbox", 402, {"__type": "x#ServiceQuotaExceededException"})
    with pytest.raises(SandboxError) as ei:
        client.create_sandbox("crew-a")
    assert ei.value.kind == "quota"
    client.list_sandboxes()  # no hold


def test_a_bad_display_name_sends_nothing(world, svc):
    client, *_ = world
    with pytest.raises(ValueError):
        client.create_sandbox("")
    assert svc.requests == []


# ---- backoff ---------------------------------------------------------------------------


def test_the_backoff_grows_ends_on_time_and_on_a_new_sign_in(world, svc):
    client, _, clock, state, _ = world
    for status, hold in ((503, 60), (429, 120), (401, 300), (500, 300)):
        svc.answer("ListSandboxes", status, {})
        with pytest.raises(SandboxError):
            client.list_sandboxes()
        with pytest.raises(GuardRefused) as ei:
            client.list_sandboxes()
        assert ei.value.reason == "backoff"
        clock.t += hold - 1
        with pytest.raises(GuardRefused):
            client.list_sandboxes()
        clock.t += 1
    assert svc.ops() == ["ListSandboxes"] * 4
    svc.answer("ListSandboxes", 500, {})
    with pytest.raises(SandboxError):
        client.list_sandboxes()
    state["cred"] = Credential(TOKEN, "sign-in-b")
    client.list_sandboxes()  # a new sign-in ends the hold
    # a good reply resets the steps
    svc.answer("ListSandboxes", 500, {})
    with pytest.raises(SandboxError):
        client.list_sandboxes()
    clock.t += 60
    client.list_sandboxes()


@pytest.mark.parametrize("status", [400, 404, 409])
def test_other_errors_hold_nothing(world, svc, status):
    client, *_ = world
    svc.answer("ListSandboxes", status, {"__type": "x#Whatever", "message": "no"})
    with pytest.raises(SandboxError) as ei:
        client.list_sandboxes()
    assert ei.value.message == "no"
    client.list_sandboxes()


# ---- wait_running ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statuses, want",
    [
        (["PENDING", "RUNNING"], "RUNNING"),
        (["STOPPED", "PENDING", "RUNNING"], "RUNNING"),
        (["PENDING", "FAILED"], "failed"),
        (["DELETED"], "deleted"),
        (["PENDING", "STOPPED"], "stopped"),
    ],
)
def test_wait_running(world, svc, statuses, want):
    client, _, _, _, sleeps = world
    sid = _own(client, svc)
    for st in statuses:
        svc.answer("GetSandbox", 200, {"execEnvironmentId": sid, "status": st})
    if want == "RUNNING":
        assert client.wait_running(sid)["status"] == "RUNNING"
    else:
        with pytest.raises(SandboxStateError) as ei:
            client.wait_running(sid)
        assert ei.value.kind == want
    assert len(sleeps) == len(statuses) - 1


def test_a_passing_outage_costs_the_poll_one_read(world, svc):
    client, _, clock, *_ = world
    sid = _own(client, svc)
    svc.answer("GetSandbox", 503, {})
    svc.answer("GetSandbox", 200, {"execEnvironmentId": sid, "status": "RUNNING"})
    # read 1: the 503, which starts a 60 s backoff; read 2 (30 s on): refused by the
    # backoff; read 3 (60 s on): RUNNING. Neither the 503 nor the backoff ends the poll.
    client._sleep = lambda s: setattr(clock, "t", clock.t + 30)
    assert client.wait_running(sid)["status"] == "RUNNING"
    assert svc.ops().count("GetSandbox") == 2


def test_a_lasting_refusal_still_ends_the_poll(world, svc):
    client, *_ = world
    sid = _own(client, svc)
    svc.answer("GetSandbox", 403, {"__type": "x#AccessDenied", "message": "no"})
    with pytest.raises(SandboxError) as ei:
        client.wait_running(sid)
    assert ei.value.status == 403


def test_a_cleanup_delete_goes_out_inside_the_backoff(world, svc):
    client, *_ = world
    sid = _own(client, svc)
    svc.answer("ListSandboxes", 503, {})
    with pytest.raises(SandboxError):
        client.list_sandboxes()
    with pytest.raises(GuardRefused):
        client.delete_sandbox(sid)
    assert client.delete_sandbox(sid, cleanup=True)["status"] == "DELETING"


def test_wait_running_gives_up(world, svc, monkeypatch):
    client, *_ = world
    sid = _own(client, svc)
    monkeypatch.setattr("kiro_crew.managed_env.client.POLL_MAX", 2)
    with pytest.raises(SandboxStateError) as ei:
        client.wait_running(sid)
    assert ei.value.kind == "timeout"


# ---- state on disk -------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_the_state_directory_is_0700_and_its_files_0600(world, svc):
    client, guard, *_ = world
    _own(client, svc)
    assert stat.S_IMODE(guard._dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((guard._dir / guard_mod.LEDGER_NAME).stat().st_mode) == 0o600


def test_a_damaged_ledger_refuses_rather_than_resetting(world, svc):
    client, guard, *_ = world
    (guard._dir / guard_mod.LEDGER_NAME).write_text("{", encoding="utf-8")
    with pytest.raises(GuardRefused) as ei:
        client.create_sandbox("crew-a")
    assert ei.value.reason == "state_corrupt"
    assert svc.requests == []  # refused before the create, so no environment is orphaned
    (guard._dir / guard_mod.LEDGER_NAME).write_text('{"sandboxes": 1}', encoding="utf-8")
    with pytest.raises(GuardRefused):
        guard.own_ids()


def test_a_damaged_pause_record_holds_from_the_files_time(world, svc):
    client, guard, clock, *_ = world
    path = guard._dir / guard_mod.UNCONFIRMED_NAME
    path.write_text("not json", encoding="utf-8")
    clock.t = path.stat().st_mtime
    with pytest.raises(GuardRefused) as ei:
        client.create_sandbox("crew-a")
    assert ei.value.reason == "create_unconfirmed"
