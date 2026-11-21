"""A 5XX or no answer on
ListSandboxes doesn't stop the client: the List retries on a capped backoff (15 s, 30 s, 60 s,
then every 2 min), the rows read "Can't reach the sandbox service right now; retrying.", and the
line clears on the next 200. Writes never retry.
Create's preflight reads plainly.
"""

from __future__ import annotations

import pytest
from managed_env_fake_bff import FakeBff
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_routes import _env, call, own

from kiro_crew.managed_env.client import SandboxError
from kiro_crew.managed_env.engine import SandboxEngineError
from kiro_crew.managed_env.guard import SERVICE_TEXT
from kiro_crew.managed_env.routes import LIST_BACKOFF_S

RETRYING = "Can't reach the sandbox service right now; retrying."


@pytest.fixture
def bff():
    b = FakeBff()
    yield b
    b.close()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _lists(bff):
    return sum(1 for r in bff.requests if r["op"] == "ListSandboxes")


def _clocked(routes):
    clock = Clock()
    routes._now = clock
    return clock


def test_a_list_503_retries_on_the_backoff_and_the_line_clears_on_the_next_200(
    bff, tmp_path, monkeypatch
):
    routes, reg, guard, _ = _env(bff, tmp_path, monkeypatch)
    own(bff, guard, reg)
    clock = _clocked(routes)
    bff.queue_error("any_503", op="ListSandboxes")
    first = routes.list_sync()
    assert first["error"] == RETRYING == SERVICE_TEXT["retrying"]
    assert not guard.stopped and "client_stopped" not in first  # not a stop
    assert _lists(bff) == 1
    clock.t += 14  # inside the first wait: no call, the same line
    assert routes.list_sync()["error"] == RETRYING and _lists(bff) == 1
    clock.t += 1  # 15 s: the retry, answered 200
    again = routes.list_sync()
    assert _lists(bff) == 2 and again["error"] == ""  # the line clears
    assert [s["status"] for s in again["sandboxes"]] == ["RUNNING"]
    # the 200 reset the backoff: a later outage waits 15 s again, not 30
    bff.queue_error("any_503", op="ListSandboxes")
    assert routes.list_sync()["error"] == RETRYING
    assert routes._list_retry_at - clock.t == 15.0


def test_the_backoff_is_capped_at_two_minutes(bff, tmp_path, monkeypatch):
    routes, reg, guard, _ = _env(bff, tmp_path, monkeypatch)
    clock = _clocked(routes)
    assert LIST_BACKOFF_S == (15.0, 30.0, 60.0, 120.0)
    waits = []
    for _ in range(6):
        bff.queue_error("any_503", op="ListSandboxes")
        n = _lists(bff)
        assert routes.list_sync()["error"] == RETRYING and _lists(bff) == n + 1
        waits.append(routes._list_retry_at - clock.t)
        clock.t = routes._list_retry_at
    assert waits == [15.0, 30.0, 60.0, 120.0, 120.0, 120.0]
    assert not guard.stopped


def test_no_answer_on_the_list_retries_too(bff, tmp_path, monkeypatch):
    routes, reg, guard, _ = _env(bff, tmp_path, monkeypatch)
    clock = _clocked(routes)
    client = routes.engine.client
    real = client._post
    failed = []

    def _lost(op, body, *a, **k):
        if op == "ListSandboxes" and not failed:
            failed.append(op)
            raise SandboxError(op, "unreachable", message="URLError")
        return real(op, body, *a, **k)

    monkeypatch.setattr(client, "_post", _lost)
    assert routes.list_sync()["error"] == RETRYING and not guard.stopped
    clock.t += 15
    assert routes.list_sync()["error"] == ""


def test_a_list_429_still_stops_under_the_product_rules(bff, tmp_path, monkeypatch):
    routes, reg, guard, _ = _env(bff, tmp_path, monkeypatch)
    bff.queue("ListSandboxes", 429, {"__type": "com.example.kiro#ThrottlingException"})
    answer = routes.list_sync()
    assert guard.stopped and answer["error"] == SERVICE_TEXT["unreachable"]


def test_a_write_5xx_is_never_retried(bff, tmp_path, monkeypatch):
    routes, reg, guard, _ = _env(bff, tmp_path, monkeypatch)
    sid = own(bff, guard, reg)
    routes.list_sync()
    bff.queue_error("any_503", op="StopSandbox")
    call(routes, "POST", f"/api/managed-environments/{sid}/stop")
    stops = sum(1 for r in bff.requests if r["op"] == "StopSandbox")
    assert stops == 1  # one try, no retry
    assert guard.stopped  # a write's 5XX keeps the stop rule, as today
    error = routes.actions.get(sid)["error"]
    assert "StopSandbox" in error or error == SERVICE_TEXT["unreachable"]  # its failure, as today


def test_creates_preflight_reads_plainly_once_stopped(bff, tmp_path, monkeypatch):
    # the Launch form shows the plain line, not the guard's text or its code
    routes, reg, guard, _ = _env(bff, tmp_path, monkeypatch)
    guard.note_status("ListSandboxes", 401)
    engine = routes.engine
    with pytest.raises(SandboxEngineError) as got:
        engine.preflight("my-crew", "")
    assert str(got.value) == SERVICE_TEXT["stopped"]
    assert "client stopped" not in str(got.value) and not str(got.value).startswith("stopped:")


def test_a_list_outage_in_creates_preflight_reads_plainly(bff, tmp_path, monkeypatch):
    # the Launch form, not the raw SandboxError text
    routes, reg, guard, _ = _env(bff, tmp_path, monkeypatch)
    engine = routes.engine
    bff.queue_error("any_503", op="ListSandboxes")
    with pytest.raises(SandboxEngineError) as got:
        engine.preflight("my-crew", "")
    assert str(got.value) == SERVICE_TEXT["try_later"]
    assert "HTTP" not in str(got.value) and "ListSandboxes" not in str(got.value)
    assert not guard.stopped
