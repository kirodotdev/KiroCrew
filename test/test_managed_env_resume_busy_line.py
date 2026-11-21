"""A Resume that meets a starting or running box is not an error, and its line goes.

The case: a Resume press meets the idle-timeout restart. The service's row reads STOPPED while the
host runs the box, so the client's not-ready retry (every 3 s, 60 s at most; one GetSandbox after
the first 409) can run out, and a raw "ResumeSandbox: HTTP 409 ConflictException" must not stay on
a row that reads "Connected". The rules:

- after the first not-ready 409, the one Get that reads the box PENDING or RUNNING ends the Resume
  with no error and no repeat call: the box is starting or up, and the row shows its state;
- a not-ready 409 that does end the Resume reads as one plain line, with no operation name and no
  HTTP code;
- a List that shows the box RUNNING clears a finished Resume's line;
- the retry itself (3 s, 60 s, FAILED / DELETED / 404 end it), every other Resume answer, the guard
  rules and the policy behaviour are unchanged.
"""

from __future__ import annotations

from collections import deque

import pytest
from managed_env_fake_bff import FakeBff
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_client import NOT_READY, ft, make, own  # noqa: F401  (fixtures)
from test_managed_env_policy_403 import _items, _queue_403, _routes
from test_managed_env_routes import own as own_row

from kiro_crew.managed_env.guard import POLICY_TEXT
from kiro_crew.managed_env.routes import RESUME_BUSY_TEXT

#: The raw line the operator saw, never shown now.
RAW = (
    "ResumeSandbox: HTTP 409 ConflictException: "
    "Sandbox is not in a state that allows this operation"
)


@pytest.fixture
def bff():
    b = FakeBff()
    yield b
    b.close()


# -- the client: the one Get after the first not-ready 409 ---------------------------------


@pytest.mark.parametrize("key", NOT_READY)
@pytest.mark.parametrize("status", ["PENDING", "RUNNING"])
def test_a_not_ready_409_on_a_starting_or_running_box_ends_the_resume_with_no_repeat(
    make, bff, ft, key, status  # noqa: F811
):
    client, guard = make()
    sid = own(bff, guard, "STOPPED")
    bff.get_statuses[sid] = deque([status])
    for _ in range(40):  # it would answer not ready for the whole window
        bff.queue_error(key)
    out = client.resume_sandbox(sid)
    assert out["status"] == status
    assert bff.ops() == ["ResumeSandbox", "GetSandbox"]  # no repeat call
    assert ft.sleeps == [1.0]  # only the client's gap before the Get
    assert not guard.stopped


@pytest.mark.parametrize("key", NOT_READY)
@pytest.mark.parametrize("status", ["STOPPED", "STOPPING", "WAITING", "HIBERNATING"])
def test_a_not_ready_409_on_a_box_not_yet_starting_keeps_the_designed_retry(
    make, bff, ft, key, status  # noqa: F811
):
    client, guard = make()
    sid = own(bff, guard, "STOPPED")
    bff.get_statuses[sid] = deque([status])
    bff.queue_error(key)
    bff.queue_error(key)
    out = client.resume_sandbox(sid)
    assert out["status"] == "PENDING" and bff.count("ResumeSandbox") == 3
    assert bff.count("GetSandbox") == 1


# -- the row: the line ---------------------------------------------------------------------


def _resume_box(bff, tmp_path, monkeypatch):
    routes, reg, guard, client, _ = _routes(bff, tmp_path, monkeypatch)
    a = own_row(bff, guard, reg, status="STOPPED", name="crew-sandbox-01-kc-dfc40d", crew="c1")
    routes.list_sync()
    return routes, guard, a


def test_the_operators_case_a_running_box_gets_no_line_and_no_repeat(bff, tmp_path, monkeypatch):
    routes, guard, a = _resume_box(bff, tmp_path, monkeypatch)
    for _ in range(40):
        bff.queue_error("resume_409_text")
    bff.get_statuses[a] = deque(
        ["RUNNING"]
    )  # the host already ran it (the service's row a moment later)
    routes.actions.begin(a, "resume")  # as the route does
    routes._run_action(a, "resume")
    entry = routes.actions.get(a)
    assert entry["running"] is False and entry["error"] == ""
    assert bff.count("ResumeSandbox") == 1 and guard.stopped == ""
    items = _items(routes.list_sync())
    assert items[a]["status"] == "RUNNING" and items[a]["action"]["error"] == ""


def test_a_resume_that_runs_out_on_not_ready_reads_one_plain_line(bff, tmp_path, monkeypatch):
    """The service's row read STOPPED for the whole retry window."""
    routes, guard, a = _resume_box(bff, tmp_path, monkeypatch)
    for _ in range(40):
        bff.queue_error("resume_409_text")
    bff.get_statuses[a] = deque(["STOPPED"])
    routes.actions.begin(a, "resume")  # as the route does
    routes._run_action(a, "resume")
    line = routes.actions.get(a)["error"]
    assert line == RESUME_BUSY_TEXT and line != RAW
    for word in ("ResumeSandbox", "HTTP", "409", "ConflictException"):
        assert word not in line
    assert guard.stopped == ""
    # the box still reads STOPPED: the line stays
    assert _items(routes.list_sync())[a]["action"]["error"] == RESUME_BUSY_TEXT
    # the next status read that shows it running clears it, with no new call for the box
    bff.sandboxes[a]["status"] = "RUNNING"
    resumes = bff.count("ResumeSandbox")
    items = _items(routes.list_sync())
    assert items[a]["status"] == "RUNNING" and items[a]["action"]["error"] == ""
    assert routes.actions.get(a)["error"] == "" and bff.count("ResumeSandbox") == resumes


def test_the_running_list_clears_only_a_finished_resumes_line(bff, tmp_path, monkeypatch):
    routes, guard, a = _resume_box(bff, tmp_path, monkeypatch)
    # a Stop that failed on a running box keeps its line (the box didn't stop)
    bff.sandboxes[a]["status"] = "RUNNING"
    routes.actions.begin(a, "stop")
    routes.actions.finish(a, "the stop failed")
    assert _items(routes.list_sync())[a]["action"]["error"] == "the stop failed"
    # a running Resume is left alone too
    routes.actions.begin(a, "resume")
    routes.list_sync()
    assert routes.actions.get(a)["running"] is True


def test_the_other_resume_answers_are_as_before(bff, tmp_path, monkeypatch):
    routes, guard, a = _resume_box(bff, tmp_path, monkeypatch)
    # the policy's 403 reads as its own plain line, with no retry and no stop
    _queue_403(bff, "ResumeSandbox")
    routes.actions.begin(a, "resume")  # as the route does
    routes._run_action(a, "resume")
    assert routes.actions.get(a)["error"] == POLICY_TEXT and bff.count("ResumeSandbox") == 1
    # another 409 text is final at once, in the service's words as before
    bff.queue_error("resume_409_other")
    routes.actions.begin(a, "resume")  # as the route does
    routes._run_action(a, "resume")
    line = routes.actions.get(a)["error"]
    assert line != RESUME_BUSY_TEXT and "an operator holds it" in line
    assert bff.count("ResumeSandbox") == 2 and guard.stopped == ""


def test_the_busy_line_is_plain():
    assert RESUME_BUSY_TEXT and RESUME_BUSY_TEXT.endswith(".")
    for word in (
        "ResumeSandbox",
        "HTTP",
        "409",
        "Conflict",
        "Exception",
        "the service",
        "the host",
    ):
        assert word not in RESUME_BUSY_TEXT
