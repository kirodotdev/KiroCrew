"""The policy line goes once a later call for that box succeeds.

The case: a box's StartSshSession is refused 403 by the organization's policy, an admin then
allows the account, and the row must not keep the policy line under "Connected". The rules:

- the line shows only while the box's latest outcome is the policy refusal: a served tuple, a
  ResumeSandbox 200 or a GetSandbox 200 for that box clears it (and the hold) at once;
- a List that shows the box ends its hold once the refusal is ``POLICY_LIST_ENDS_HOLD_S`` old, so
  the next ssh ask makes one call; the line stays until that call (or another for the box)
  succeeds, and a new 403 there holds the box again;
- the guard rules and the hold texts are unchanged.
"""

from __future__ import annotations

import pytest
from managed_env_fake_bff import FakeBff
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_policy_403 import _items, _queue_403, _routes, _ssh_path
from test_managed_env_routes import call, own

from kiro_crew.managed_env.client import SandboxError
from kiro_crew.managed_env.guard import (
    POLICY_HOLD_S,
    POLICY_LIST_ENDS_HOLD_S,
    POLICY_TEXT,
    GuardRefused,
)


@pytest.fixture
def bff():
    b = FakeBff()
    yield b
    b.close()


def _refused(bff, routes, guard, sid: str) -> None:
    """The box's ssh ask gets the policy 403: its line is on, and the hold refuses from memory."""
    _queue_403(bff, "StartSshSession")
    assert call(routes, "POST", _ssh_path(sid))[0] == 424
    assert _items(routes.list_sync())[sid]["policy_line"] == POLICY_TEXT
    assert guard.policy_line(sid) == POLICY_TEXT
    asked = bff.count("StartSshSession")
    status, body = call(routes, "POST", _ssh_path(sid))
    assert status == 422 and body["error"] == POLICY_TEXT  # the hold's text
    assert bff.count("StartSshSession") == asked


def test_a_403_then_a_resume_200_for_the_same_box_clears_the_line_and_the_hold(
    bff, tmp_path, monkeypatch
):
    routes, reg, guard, client, _ = _routes(bff, tmp_path, monkeypatch)
    a = own(bff, guard, reg, name="crew-sandbox-01-kc-dfc40d", crew="crew-sandbox-01")
    b = own(bff, guard, reg, name="crew-sandbox-02-kc-00fcd1", crew="crew-sandbox-02")
    routes.list_sync()
    _refused(bff, routes, guard, a)
    _refused(bff, routes, guard, b)
    client.resume_sandbox(a)  # 200: the policy allowed this box's action
    assert guard.policy_line(a) == "" and guard.policy_line(b) == POLICY_TEXT
    items = _items(routes.list_sync())
    assert "policy_line" not in items[a]  # the row's line goes with no restart and no reload
    assert items[b]["policy_line"] == POLICY_TEXT  # the other box is its own
    # the hold ended too: once the box is up, the next ssh ask makes its call at once, inside
    # POLICY_HOLD_S
    bff.sandboxes[a]["status"] = "RUNNING"
    asked = bff.count("StartSshSession")
    status, body = call(routes, "POST", _ssh_path(a))
    assert status == 200 and body["ssmSessionId"] and bff.count("StartSshSession") == asked + 1
    assert guard.stopped == ""


def test_a_403_then_a_get_200_for_the_same_box_clears_the_line_and_the_hold(
    bff, tmp_path, monkeypatch
):
    routes, reg, guard, client, _ = _routes(bff, tmp_path, monkeypatch)
    a = own(bff, guard, reg, name="crew-sandbox-01-kc-dfc40d", crew="crew-sandbox-01")
    routes.list_sync()
    _refused(bff, routes, guard, a)
    assert client.get_sandbox(a)["status"] == "RUNNING"
    assert guard.policy_line(a) == ""
    assert "policy_line" not in _items(routes.list_sync())[a]
    asked = bff.count("StartSshSession")
    assert client.start_ssh_session(a).ssm_session_id
    assert bff.count("StartSshSession") == asked + 1


def test_a_403_then_a_served_tuple_clears_the_line_in_the_next_list(bff, tmp_path, monkeypatch):
    routes, reg, guard, client, clock = _routes(bff, tmp_path, monkeypatch)
    a = own(bff, guard, reg, name="crew-sandbox-01-kc-dfc40d", crew="crew-sandbox-01")
    routes.list_sync()
    _refused(bff, routes, guard, a)
    clock.advance(POLICY_LIST_ENDS_HOLD_S)
    routes.list_sync()  # ends the hold; the line stays until a call succeeds
    assert guard.policy_line(a) == POLICY_TEXT
    status, _ = call(routes, "POST", _ssh_path(a))
    assert status == 200
    assert guard.policy_line(a) == "" and "policy_line" not in _items(routes.list_sync())[a]


def test_the_operators_case_the_list_ends_the_hold_and_the_next_refresh_clears_the_line(
    bff, tmp_path, monkeypatch
):
    """The last 403, then the admin's allow about 100 s later; the page's List every 30 s."""
    routes, reg, guard, client, clock = _routes(bff, tmp_path, monkeypatch)
    a = own(bff, guard, reg, name="crew-sandbox-01-kc-dfc40d", crew="crew-sandbox-01")
    routes.list_sync()
    _refused(bff, routes, guard, a)
    asked = bff.count("StartSshSession")
    # the floor: a List inside POLICY_LIST_ENDS_HOLD_S keeps the hold (a moving row lists each 3 s)
    clock.advance(POLICY_LIST_ENDS_HOLD_S - 1)
    assert _items(routes.list_sync())[a]["policy_line"] == POLICY_TEXT
    with pytest.raises(GuardRefused) as got:
        client.start_ssh_session(a)
    assert got.value.reason == "policy_denied" and bff.count("StartSshSession") == asked
    # the page's next List, still under the deny: the line stays, and the hold ends
    clock.advance(1)
    assert _items(routes.list_sync())[a]["policy_line"] == POLICY_TEXT
    # a refresh under the deny: one call, refused again, and the box is held again (no loop)
    _queue_403(bff, "StartSshSession")
    with pytest.raises(SandboxError):
        client.start_ssh_session(a)
    assert bff.count("StartSshSession") == asked + 1 and guard.stopped == ""
    assert guard.policy_line(a) == POLICY_TEXT
    for _ in range(3):
        with pytest.raises(GuardRefused):
            client.start_ssh_session(a)
    assert bff.count("StartSshSession") == asked + 1
    # the admin allows; the page's next List ends the hold, and the next refresh is served
    clock.advance(POLICY_LIST_ENDS_HOLD_S)
    routes.list_sync()
    assert client.start_ssh_session(a).ssm_session_id
    assert bff.count("StartSshSession") == asked + 2
    # well inside the POLICY_HOLD_S of the last 403, the row reads with no line
    assert POLICY_LIST_ENDS_HOLD_S * 2 < POLICY_HOLD_S
    assert "policy_line" not in _items(routes.list_sync())[a]
    assert guard.stopped == ""


def test_with_no_list_and_no_success_the_r11_hold_is_unchanged(bff, tmp_path, monkeypatch):
    routes, reg, guard, client, clock = _routes(bff, tmp_path, monkeypatch)
    a = own(bff, guard, reg, name="crew-sandbox-01-kc-dfc40d", crew="crew-sandbox-01")
    routes.list_sync()
    _refused(bff, routes, guard, a)
    asked = bff.count("StartSshSession")
    clock.advance(POLICY_HOLD_S - 1)
    with pytest.raises(GuardRefused):
        client.start_ssh_session(a)
    assert bff.count("StartSshSession") == asked
    clock.advance(1)
    assert client.start_ssh_session(a).ssm_session_id
    assert bff.count("StartSshSession") == asked + 1 and guard.policy_line(a) == ""


def test_a_failed_get_or_resume_keeps_the_line(bff, tmp_path, monkeypatch):
    routes, reg, guard, client, _ = _routes(bff, tmp_path, monkeypatch)
    a = own(bff, guard, reg, name="crew-sandbox-01-kc-dfc40d", crew="crew-sandbox-01")
    routes.list_sync()
    _refused(bff, routes, guard, a)
    _queue_403(bff, "ResumeSandbox")  # the policy again: refused, not a success
    with pytest.raises(SandboxError):
        client.resume_sandbox(a)
    bff.queue_error("get_404", op="GetSandbox")
    with pytest.raises(SandboxError):
        client.get_sandbox(a)
    assert guard.policy_line(a) == POLICY_TEXT and guard.stopped == ""
    assert _items(routes.list_sync())[a]["policy_line"] == POLICY_TEXT
