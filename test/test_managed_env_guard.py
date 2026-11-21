"""The managed-environment guard: ledger, the unconfirmed-create hold and the stop rules.

Everything runs in a temp dir.
"""

from __future__ import annotations

import json
import os
import stat
import time

import pytest
from managed_env_fake_bff import BAD_STATUS, CUT, DROP, FakeBff, FakeTime, cut_error
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401

from kiro_crew.managed_env.client import BffClient, SandboxError
from kiro_crew.managed_env.guard import (
    HOLD_AFTER_UNCONFIRMED_S,
    ClientStopped,
    GuardRefused,
    SandboxGuard,
)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def guard_for(tmp_path, home, **kw):
    return SandboxGuard(tmp_path / "state", **kw)


@pytest.fixture
def stack(tmp_path, home):
    bff = FakeBff()
    ft = FakeTime(time.time())
    g = guard_for(tmp_path, home, now=ft.now)
    c = BffClient(lambda: "t", g, endpoint=bff.endpoint, sleep=ft.sleep, clock=ft.clock)
    yield bff, ft, g, c
    bff.close()


def test_product_rules_another_clients_sandbox_does_not_block_the_create(stack):
    bff, ft, g, c = stack
    other = bff.add_other("RUNNING")
    created = c.create_sandbox("a")
    assert bff.count("CreateSandbox") == 1
    assert g.stopped == ""
    assert g.own_ids() == {created["execEnvironmentId"]} and other not in g.own_ids()


def test_state_dir_is_0700_and_files_are_0600(tmp_path, home):
    g = guard_for(tmp_path, home)
    g.record_created({"execEnvironmentId": "env-1", "displayName": "n", "createdAt": 5}, "n")
    assert stat.S_IMODE(g.ledger_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(g.ledger_path.stat().st_mode) == 0o600


def test_a_loose_existing_state_dir_is_tightened(tmp_path, home):
    d = tmp_path / "state"
    d.mkdir(mode=0o755)
    d.chmod(0o755)
    SandboxGuard(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_the_ledger_row_shape_and_persistence(tmp_path, home):
    g = guard_for(tmp_path, home)
    g.record_created({"execEnvironmentId": "env-1", "displayName": "n", "createdAt": 5}, "n")
    g.record_created(
        {"execEnvironmentId": "env-1", "displayName": "n", "createdAt": 5}, "n"
    )  # no dup
    rows = guard_for(tmp_path, home).ledger()
    assert rows == [
        {"execEnvironmentId": "env-1", "displayName": "n", "createdAt": 5, "source": "kiro-crew"}
    ]


def test_the_ledger_holds_no_secret_fields(tmp_path, home):
    g = guard_for(tmp_path, home)
    g.record_created(
        {"execEnvironmentId": "env-1", "displayName": "n", "createdAt": 5, "tokenValue": "SECRET"},
        "n",
    )
    assert "SECRET" not in g.ledger_path.read_text()


def test_require_own_refuses_unknown_ids(tmp_path, home):
    g = guard_for(tmp_path, home)
    g.record_created({"execEnvironmentId": "env-1"}, "n")
    g.require_own("env-1")
    for bad in ("env-2", "", "env-1 "):
        with pytest.raises(GuardRefused) as ei:
            g.require_own(bad)
        assert ei.value.reason == "not_own_id"


# -- stop rules -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,stops",
    [
        (401, True),
        (403, True),
        (429, True),
        (500, True),
        (503, True),
        (400, False),
        (404, False),
        (409, False),
        (423, False),
    ],
)
def test_note_status_stop_set(tmp_path, home, status, stops):
    g = guard_for(tmp_path, home)
    g.note_status("GetSandbox", status)
    assert bool(g.stopped) is stops
    if stops:
        with pytest.raises(ClientStopped):
            g.check_not_stopped()


@pytest.mark.parametrize(
    "op,status,kind",
    [
        ("CreateSandbox", 429, "ThrottlingException"),  # a throttle still stops
        ("CreateSandbox", 429, ""),
        ("GetSandbox", 429, "ServiceQuotaExceededException"),  # the exception is Create's only
        ("CreateSandbox", 503, "ServiceQuotaExceededException"),
        ("CreateSandbox", 403, "ServiceQuotaExceededException"),
    ],
)
def test_every_other_429_or_5xx_still_stops(tmp_path, home, op, status, kind):
    g = guard_for(tmp_path, home)
    g.note_status(op, status, kind)
    assert g.stopped


def _lose_the_answer(bff, how):
    """A CreateSandbox whose answer Crew can't confirm: (the error's kind or reason)."""
    if how == "drop":
        bff.queue("CreateSandbox", DROP)  # no answer at all
        return "unreachable"
    if how == "not_cbor":
        bff.queue("CreateSandbox", 200, b"<html>gateway timeout</html>")
        return "bad_response"
    if how == "not_a_map":
        bff.queue("CreateSandbox", 200, [1, 2])  # type: ignore[arg-type]
        return "bad_response"
    if how == "cut_body":
        bff.queue("CreateSandbox", CUT)  # the sandbox is made; its 200 stops after 5 bytes
        return "unreachable"
    if how == "bad_status":
        bff.queue("CreateSandbox", BAD_STATUS)  # the sandbox is made; the status line is garbage
        return "unreachable"
    bff.queue("CreateSandbox", 200, {"status": "WAITING"})  # a 200 with no id
    return "create_no_id"


def _kind(exc):
    return getattr(exc, "reason", None) or exc.kind


def test_product_rules_an_immediate_relaunch_is_sent(stack):
    bff, ft, g, c = stack
    first = c.create_sandbox("kc-000001")
    c.delete_sandbox(first["execEnvironmentId"])
    ft.advance(5)
    c.create_sandbox("kc-000002")
    assert bff.count("CreateSandbox") == 2


def test_product_rules_a_second_create_inside_ten_minutes_is_sent(stack):
    bff, ft, g, c = stack
    first = c.create_sandbox("a")
    c.delete_sandbox(first["execEnvironmentId"])
    ft.advance(HOLD_AFTER_UNCONFIRMED_S - 60)
    c.create_sandbox("b")
    assert bff.count("CreateSandbox") == 2


def test_product_rules_the_seventh_create_after_six_is_sent(stack):
    bff, ft, g, c = stack
    for i in range(6 + 1):
        sid = c.create_sandbox(f"n{i}")["execEnvironmentId"]
        c.delete_sandbox(sid)
        ft.advance(HOLD_AFTER_UNCONFIRMED_S + 1)
    assert bff.count("CreateSandbox") == 6 + 1


@pytest.mark.parametrize("key", ["create_429", "create_402", "create_400"])
def test_product_rules_a_definite_refusal_counts_nothing_and_starts_no_hold(stack, key):
    bff, ft, g, c = stack
    bff.queue_error(key)
    with pytest.raises(SandboxError) as ei:
        c.create_sandbox("kc-000001")
    assert ei.value.status is not None
    assert not g.unconfirmed_path.exists()
    ft.advance(5)
    c.create_sandbox("kc-000002")
    assert bff.count("CreateSandbox") == 2


@pytest.mark.parametrize(
    "how", ["drop", "not_cbor", "not_a_map", "no_id", "cut_body", "bad_status"]
)
def test_product_rules_a_lost_create_answer_holds_new_launches_for_ten_minutes(stack, how):
    bff, ft, g, c = stack
    want = _lose_the_answer(bff, how)
    with pytest.raises((SandboxError, GuardRefused)) as ei:
        c.create_sandbox("kc-abc123")
    assert _kind(ei.value) == want
    # written before the error reached the caller
    record = json.loads(g.unconfirmed_path.read_text())
    assert record == {"at": ft.now(), "displayName": "kc-abc123"}
    assert stat.S_IMODE(g.unconfirmed_path.stat().st_mode) == 0o600
    assert g.ledger() == [] and g.stopped == ""
    if how == "no_id":
        text = str(ei.value)
        assert "kc-abc123" in text and "orphan" in text
        assert "recorded" in text and "wait 10 minutes" in text and "counted" not in text
    ft.advance(5)
    with pytest.raises(GuardRefused) as held:
        c.create_sandbox("kc-def456")
    assert held.value.reason == "create_unconfirmed"
    assert bff.count("CreateSandbox") == 1  # no second Create in the hold
    assert "(kc-abc123)" in str(held.value) and "in 10 minutes" in str(held.value)
    ft.advance(HOLD_AFTER_UNCONFIRMED_S + 1 - 5)  # the record's time + 601 s
    c.create_sandbox("kc-def456")
    assert bff.count("CreateSandbox") == 2


def test_product_rules_a_create_5xx_with_a_cut_body_stops_and_starts_no_hold(stack):
    """A gateway 502 whose body is lost keeps its status: it stops the client, so no relaunch."""
    bff, ft, g, c = stack
    body = {"__type": "com.example.kiro#BadGatewayException", "message": "the upstream timed out"}
    bff.queue("CreateSandbox", cut_error(502), body)
    with pytest.raises(SandboxError) as ei:
        c.create_sandbox("kc-000001")
    assert (ei.value.status, ei.value.kind) == (502, "HTTP502")
    assert "502" in g.stopped
    assert not g.unconfirmed_path.exists()
    ft.advance(5)
    with pytest.raises(ClientStopped):
        c.create_sandbox("kc-000002")
    assert bff.count("CreateSandbox") == 1


def test_the_hold_text_names_the_lost_launch_and_the_minutes_left():
    from kiro_crew.managed_env.engine import CARD_TEXT_LIMIT
    from kiro_crew.managed_env.guard import unconfirmed_text

    text = unconfirmed_text("kc-abc123", 595)
    assert text == (
        "Crew's last launch (kc-abc123) got no clear answer from the sandbox service, so a "
        "sandbox may have been made without Crew knowing it. If kc-abc123 is listed under "
        "Sandboxes without a crew (made outside this Crew), delete it in the Kiro web app. "
        "Crew can launch again in 10 minutes."
    )
    assert len("create_unconfirmed: " + text) <= CARD_TEXT_LIMIT
    assert unconfirmed_text("kc-abc123", 30).endswith("again in 1 minute.")
    assert unconfirmed_text("", 61).startswith("Crew's last launch got no clear answer")


@pytest.mark.parametrize("failure", ["no_token", "not_signed_in"])
def test_product_rules_no_hold_when_no_create_left_the_machine(tmp_path, home, failure):
    from kiro_crew.managed_env.engine import SandboxEngineError

    answers = ["t", failure]  # the List gets a token, the Create does not

    def provider():
        a = answers.pop(0) if answers else "t"
        if a == "not_signed_in":
            raise SandboxEngineError("not_signed_in", "sign in to Kiro first")
        return "" if a == "no_token" else a

    bff = FakeBff()
    try:
        ft = FakeTime(time.time())
        g = guard_for(tmp_path, home, now=ft.now)
        c = BffClient(provider, g, endpoint=bff.endpoint, sleep=ft.sleep, clock=ft.clock)
        with pytest.raises((SandboxError, SandboxEngineError)) as ei:
            c.create_sandbox("kc-000001")
        assert getattr(ei.value, "kind", None) == "no_token" or ei.value.code == "not_signed_in"
        assert bff.count("CreateSandbox") == 0 and not g.unconfirmed_path.exists()
        ft.advance(5)
        c.create_sandbox("kc-000002")
        assert bff.count("CreateSandbox") == 1
    finally:
        bff.close()


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "",
        "[]",
        '{"at": "soon", "displayName": "kc-x"}',
        '{"at": true}',
        '{"at": 1e300}',
    ],
)
def test_product_rules_a_record_that_cannot_be_parsed_holds_from_its_mtime(stack, content):
    bff, ft, g, c = stack
    g.unconfirmed_path.write_text(content)
    os.utime(g.unconfirmed_path, (ft.now(), ft.now()))
    with pytest.raises(GuardRefused) as ei:
        c.create_sandbox("kc-000001")
    assert ei.value.reason == "create_unconfirmed" and bff.count("CreateSandbox") == 0
    ft.advance(HOLD_AFTER_UNCONFIRMED_S + 1)  # mtime + 601 s
    c.create_sandbox("kc-000001")
    assert bff.count("CreateSandbox") == 1


def test_product_rules_a_record_that_cannot_be_written_stops_the_client(stack, monkeypatch):
    bff, ft, g, c = stack

    def boom(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(g, "_write_json", boom)
    bff.queue("CreateSandbox", DROP)
    with pytest.raises(SandboxError) as ei:
        c.create_sandbox("kc-000001")
    assert ei.value.kind == "unreachable"  # the create's own error, not the write's
    assert "unconfirmed" in g.stopped
    with pytest.raises(ClientStopped):
        c.list_sandboxes()
    assert bff.count("CreateSandbox") == 1


def test_product_rules_a_record_that_cannot_be_examined_refuses(tmp_path, home):
    g = guard_for(tmp_path, home)
    g.ledger_path.parent.chmod(0o600)  # no search permission: the record can't be examined
    try:
        if os.access(g.unconfirmed_path.parent, os.X_OK):
            pytest.skip("running as a user that can search a mode 600 directory")
        with pytest.raises(GuardRefused) as ei:
            g.before_create()
        assert ei.value.reason == "state_unreadable"
    finally:
        g.ledger_path.parent.chmod(0o700)


def _spy_paths(monkeypatch, seen):
    import builtins

    def wrap(fn):
        def spy(path, *a, **kw):
            seen.append(str(path))
            return fn(path, *a, **kw)

        return spy

    monkeypatch.setattr(os, "listdir", wrap(os.listdir))
    monkeypatch.setattr(os, "lstat", wrap(os.lstat))
    monkeypatch.setattr(builtins, "open", wrap(builtins.open))
