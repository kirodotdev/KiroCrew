"""The lane names its signed-in user (``sandbox.signed_in_user``) with one service
``GetUserInfo`` per sign-in, through the lane's client and its guard, and never logs the
e-mail. Against the loopback fake service; nothing reaches a network.
"""

from __future__ import annotations

import json
import logging

import pytest
from managed_env_fake_bff import FakeBff, FakeTime
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_routes import _env, call

from kiro_crew.managed_env import core_signin, signed_in_user
from kiro_crew.managed_env.call_log import CallLog
from kiro_crew.managed_env.client import PORTAL_SERVICE_PATH, BffClient
from kiro_crew.managed_env.guard import SandboxGuard

EMAIL = "test0.user@example.com"
USER_ID = "user-0123456789abcdef"


@pytest.fixture
def bff():
    b = FakeBff()
    yield b
    b.close()


@pytest.fixture
def lane(monkeypatch):
    """The lane on, the test user signed in, an empty cache, and a fingerprint to move."""
    state = {"fp": "fp-1", "signed_in": True, "lane": True}
    monkeypatch.setattr(signed_in_user, "_cache", {})
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: state["lane"])
    monkeypatch.setattr(
        core_signin,
        "sign_in_status",
        lambda: {"state": "signed_in" if state["signed_in"] else "signin_required"},
    )
    monkeypatch.setattr("kiro_crew.auth.bridge.vault_identity_fingerprint", lambda: state["fp"])
    return state


def _client(bff, tmp_path, *, log=None):
    ft = FakeTime()
    guard = SandboxGuard(tmp_path / "state", now=ft.now)
    return BffClient(
        lambda: "tok", guard, endpoint=bff.endpoint, sleep=ft.sleep, clock=ft.clock, call_log=log
    )


def test_one_call_per_sign_in_on_the_portal_service(bff, tmp_path, lane):
    client = _client(bff, tmp_path)
    bff.queue("GetUserInfo", 200, {"email": EMAIL, "userId": USER_ID, "status": "Active"})
    assert signed_in_user.read(lambda: client) == {"email": EMAIL}
    assert signed_in_user.read(lambda: client) == {"email": EMAIL}  # a poll: the cache
    assert bff.count("GetUserInfo") == 1
    assert bff.requests[0]["path"] == PORTAL_SERVICE_PATH + "GetUserInfo"
    lane["fp"] = "fp-2"  # a new sign-in
    bff.queue("GetUserInfo", 200, {"userId": "other-user-99999999"})
    assert signed_in_user.read(lambda: client) == {"user_id_tail": "99999999"}
    assert bff.count("GetUserInfo") == 2


@pytest.mark.parametrize(
    "answer, shape",
    [
        ({"email": "no-at-sign", "userId": USER_ID}, {"user_id_tail": USER_ID[-8:]}),
        ({"email": "a@b" + "x" * 260, "userId": USER_ID}, {"user_id_tail": USER_ID[-8:]}),
        ({"email": "bad\nline@example.com"}, {"state": "unknown"}),
        ({"status": "Active"}, {"state": "unknown"}),
    ],
)
def test_the_answer_is_untrusted(bff, tmp_path, lane, answer, shape):
    bff.queue("GetUserInfo", 200, answer)
    assert signed_in_user.read(lambda: _client(bff, tmp_path)) == shape


def test_an_error_reads_unknown_once(bff, tmp_path, lane):
    client = _client(bff, tmp_path)
    bff.queue("GetUserInfo", 403, {"__type": "AccessDeniedException", "message": EMAIL})
    assert signed_in_user.read(lambda: client) == {"state": "unknown"}
    assert signed_in_user.read(lambda: client) == {"state": "unknown"}
    assert bff.count("GetUserInfo") == 1


@pytest.mark.parametrize("off", ["lane", "signed_in"])
def test_no_call_with_the_lane_off_or_another_sign_in(bff, tmp_path, lane, off):
    lane[off] = False

    def no_client():
        raise AssertionError("the client was built: a call was due")

    assert signed_in_user.read(no_client) == {"state": "signed_out"}
    assert bff.requests == []


def test_the_e_mail_is_in_no_log_line_and_not_in_the_call_log(bff, tmp_path, lane, caplog):
    log_file = tmp_path / "calls.jsonl"
    client = _client(bff, tmp_path, log=CallLog(log_file))
    bff.queue("GetUserInfo", 200, {"email": EMAIL, "userId": USER_ID})
    with caplog.at_level(logging.DEBUG):
        assert signed_in_user.read(lambda: client) == {"email": EMAIL}
    assert EMAIL not in caplog.text and USER_ID not in caplog.text
    line = json.loads(log_file.read_text(encoding="utf-8").splitlines()[-1])
    assert line["op"] == "GetUserInfo" and EMAIL not in json.dumps(line)


def test_the_route_answers_and_row_lifecycle_makes_no_bff_call(bff, tmp_path, monkeypatch, lane):
    routes, _reg, _guard, _ = _env(bff, tmp_path, monkeypatch)
    bff.queue("GetUserInfo", 200, {"email": EMAIL})
    assert call(routes, "GET", "/api/managed-environments/signed-in-user") == (
        200,
        {"email": EMAIL},
    )
    n = len(bff.requests)
    status, body = call(routes, "GET", "/api/managed-environments/row-lifecycle")
    assert status == 200 and len(bff.requests) == n and EMAIL not in json.dumps(body)
