"""The ``reply`` worker status, behind the ``work_ledger.remote_dispatch`` Feature Preview.

rfc-conductor-remote-dispatch §3b and wave 7. With the preview on, a worker answers a
conductor's follow-up with ``work_report status=reply``: it needs no evidence, claims
nothing finished, wakes the conductor through both the bus push (``conductor_wake``) and
the watch's gate (``ledger_wake`` via the probe), and is refused ``item_closed`` on a
closed item like any report. With it off (the default) every surface is the one from
before the status existed: the advertised schema, the accepted set, the wake set and the
refusal ``reply`` gets.

The switch is driven through the real ``config.json`` read, never by patching the
reader, so a test also proves the setting reaches each decision point.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import test_work_ledger_tools as tools
from conductor_wake_helpers import CONDUCTOR as LOOP_CONDUCTOR
from conductor_wake_helpers import _Loop, _Svc
from test_work_ledger_tools import CONDUCTOR_A, WORKER_A, _dispatch, _record, _report

from kiro_crew import conductor_wake, ledger_wake, mcp_work
from kiro_crew import work_ledger as wl
from kiro_crew import work_preview
from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
from kiro_crew.validation import ValidationError
from kiro_crew.work_vocab import WORK_WAKE_STATUSES, wake_statuses, worker_statuses

# The route harness's fixtures, bound here by name so pytest registers them.
_isolated_home = tools._isolated_home
_open_route = tools._open_route
_clean_slots = tools._clean_slots

CONDUCTOR = "chat-reply-conductor"

#: ``work_report``'s status enum on main before this change, spelled out so the
#: flag-off pins below cannot drift with the code they check.
_OLD_STATUSES = ["progress", "done", "blocked", "question"]


@pytest.fixture(autouse=True)
def _fresh_wake_registry():
    conductor_wake.reset_for_tests()
    yield
    conductor_wake.reset_for_tests()


def _preview(tmp_path: Path, on: object) -> None:
    """Write ``work_ledger.remote_dispatch`` into this test's own ``config.json``."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"work_ledger": {"remote_dispatch": on}}), encoding="utf-8"
    )
    _invalidate_config_cache()


def _item(title: str = "answer a follow-up") -> str:
    wl.ensure_conductor(CONDUCTOR, goal="g")
    created = wl.apply_conductor_action(
        CONDUCTOR, "create", title=title, acceptance={"kind": "human_approval"}
    )
    return str(created["item"].item_id)


# ── the switch itself ─────────────────────────────────────────────────────


def test_the_preview_is_off_by_default():
    assert KiroCrewConfig.load().work_ledger.remote_dispatch is False
    assert work_preview.remote_dispatch() is False


def test_the_preview_reads_true_from_config_without_a_restart(tmp_path: Path):
    _preview(tmp_path, True)
    assert work_preview.remote_dispatch() is True
    _preview(tmp_path, False)
    assert work_preview.remote_dispatch() is False


@pytest.mark.parametrize("bad", ["true", 1, None, [True], {"on": True}])
def test_a_value_that_is_not_a_bool_reads_as_off(tmp_path: Path, bad: object):
    _preview(tmp_path, bad)
    assert work_preview.remote_dispatch() is False


def test_an_unreadable_config_reads_as_off(monkeypatch: pytest.MonkeyPatch):
    def _boom(*_a, **_k):
        raise OSError("config unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", _boom)
    assert work_preview.remote_dispatch() is False


def test_the_module_tuples_name_reply_and_the_accessors_drop_it_while_off():
    """The tuples are importable as data; the flag is resolved where a decision is made."""
    assert "reply" in WORK_WAKE_STATUSES
    assert list(worker_statuses(False)) == _OLD_STATUSES
    assert list(wake_statuses(False)) == ["done", "blocked", "question"]
    assert "reply" in worker_statuses(True) and "reply" in wake_statuses(True)
    assert "progress" not in wake_statuses(True)


# ── flag off: nothing changes ─────────────────────────────────────────────


def test_off_the_advertised_tools_are_the_ones_from_before(tmp_path: Path):
    _preview(tmp_path, False)
    tools_now = mcp_work._list_tools()
    assert tools_now == mcp_work._base_tool_definitions()
    report = next(t for t in tools_now if t["name"] == "work_report")
    status = report["inputSchema"]["properties"]["status"]
    assert status["enum"] == _OLD_STATUSES
    assert status["description"] == "Where the work stands, in one of four values."
    assert "reply" not in report["description"]


def test_off_the_mcp_schema_refuses_reply(tmp_path: Path):
    _preview(tmp_path, False)
    with pytest.raises(ValidationError) as refused:
        mcp_work._validate_args("work_report", {"status": "reply", "summary": "s"})
    assert refused.value.field == "status"


def test_off_the_store_refuses_reply_as_an_unknown_status(tmp_path: Path):
    _preview(tmp_path, False)
    item_id = _item()
    with pytest.raises(wl.WorkLedgerError) as refused:
        wl.apply_worker_report(CONDUCTOR, item_id, status="reply", summary="answer")
    assert refused.value.code == wl.CODE_INVALID_STATUS
    assert str(refused.value) == (
        "status must be one of ['blocked', 'done', 'progress', 'question']; got 'reply'"
    )


@pytest.mark.asyncio
async def test_off_the_route_refuses_reply_and_records_nothing(tmp_path: Path):
    _preview(tmp_path, False)
    item_id = await _dispatch(CONDUCTOR_A, WORKER_A, "item A", {"kind": "human_approval"})
    before = wl.item_path(CONDUCTOR_A, item_id).read_bytes()
    status, body = await _report(WORKER_A, {"status": "reply", "summary": "answer"})
    assert status == 400, body
    assert body.get("code") == wl.CODE_INVALID_STATUS
    assert body.get("error") == "status: must be one of: blocked, done, progress, question"
    assert wl.item_path(CONDUCTOR_A, item_id).read_bytes() == before


# ── flag on: the wave-7 exit criteria ─────────────────────────────────────


def test_on_the_schema_offers_reply_and_says_it_wakes(tmp_path: Path):
    _preview(tmp_path, True)
    report = next(t for t in mcp_work._list_tools() if t["name"] == "work_report")
    status = report["inputSchema"]["properties"]["status"]
    assert status["enum"] == _OLD_STATUSES + ["reply"]
    assert len(status["enum"]) == 5, "the description's count"
    assert status["description"] == "Where the work stands, in one of five values."
    assert "'reply' and 'done' wake it" in report["description"]
    assert "needs no evidence" in report["description"]
    assert (
        mcp_work._validate_args("work_report", {"status": "reply", "summary": "s"})["status"]
        == "reply"
    )


def test_the_off_definitions_are_not_mutated_by_an_on_read(tmp_path: Path):
    """The on surface is built from a fresh copy, so an off read after it is unchanged."""
    before = copy.deepcopy(mcp_work._tool_definitions(False))
    mcp_work._tool_definitions(True)
    assert mcp_work._tool_definitions(False) == before


@pytest.mark.asyncio
async def test_on_a_reply_without_artifacts_is_admitted(tmp_path: Path):
    """Unlike ``done``, which is refused ``done_without_evidence`` with neither."""
    _preview(tmp_path, True)
    item_id = await _dispatch(CONDUCTOR_A, WORKER_A, "item A", {"kind": "human_approval"})
    status, body = await _report(WORKER_A, {"status": "reply", "summary": "the answer"})
    assert status == 200, body
    item = wl.read_work_item(CONDUCTOR_A, item_id)
    assert item is not None
    assert (item.status, item.summary, item.state) == ("reply", "the answer", "open")
    status, body = await _report(WORKER_A, {"status": "done", "summary": "finished"})
    assert (status, body.get("code")) == (400, "done_without_evidence")


@pytest.mark.asyncio
async def test_on_a_reply_on_a_closed_item_is_refused_item_closed(tmp_path: Path):
    _preview(tmp_path, True)
    item_id = await _dispatch(CONDUCTOR_A, WORKER_A, "item A", {"kind": "human_approval"})
    status, body = await _record(
        CONDUCTOR_A, {"action": "close", "item_id": item_id, "state": "accepted"}
    )
    assert status == 200, body
    status, body = await _report(WORKER_A, {"status": "reply", "summary": "late answer"})
    assert body.get("code") == wl.CODE_ITEM_CLOSED, (status, body)


def test_on_the_store_refuses_reply_on_a_closed_item(tmp_path: Path):
    _preview(tmp_path, True)
    item_id = _item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    with pytest.raises(wl.WorkLedgerError) as refused:
        wl.apply_worker_report(CONDUCTOR, item_id, status="reply", summary="late")
    assert refused.value.code == wl.CODE_ITEM_CLOSED


def _rows(status: str, stamp: str) -> list[tuple[str, str, str]]:
    return [("it_a", stamp, status)]


def _record_fires(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    fired: list[tuple[str, str]] = []

    async def _fake_fire(_svc, board, item_id=""):
        fired.append((board, item_id))
        return "loop-1"

    monkeypatch.setattr(conductor_wake, "_fire", _fake_fire)
    return fired


@pytest.mark.parametrize("on", [True, False])
def test_conductor_wake_pushes_for_a_reply_only_while_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, on: bool
):
    _preview(tmp_path, on)
    fired = _record_fires(monkeypatch)
    svc = _Svc(_Loop())
    asyncio.run(conductor_wake._observe_board(svc, LOOP_CONDUCTOR, _rows("reply", "t1")))
    asyncio.run(conductor_wake._observe_board(svc, LOOP_CONDUCTOR, _rows("reply", "t2")))
    assert fired == ([(LOOP_CONDUCTOR, "it_a")] if on else [])


def test_conductor_wake_reads_no_config_for_the_old_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Only a preview status pays for the switch's read."""
    reads: list[int] = []
    monkeypatch.setattr(work_preview, "remote_dispatch", lambda: reads.append(1) or False)
    fired = _record_fires(monkeypatch)
    svc = _Svc(_Loop())
    asyncio.run(conductor_wake._observe_board(svc, LOOP_CONDUCTOR, _rows("done", "t1")))
    asyncio.run(conductor_wake._observe_board(svc, LOOP_CONDUCTOR, _rows("done", "t2")))
    assert fired == [(LOOP_CONDUCTOR, "it_a")]
    assert reads == []


def _ctx():
    class _Job:
        id = "loop-1"

    class _Ctx:
        message = json.dumps({"conductor": CONDUCTOR})
        job = _Job()

    return _Ctx()


def _gate_tick():
    """One run of the work-ledger watch: ``Report`` is a wake, ``Skip`` is quiet."""
    from kiro_crew import irq
    from kiro_crew.cron_script import Report, Skip
    from kiro_crew.probes.work_ledger import WorkLedgerProbe

    try:
        irq.run(_ctx(), WorkLedgerProbe(worker_running=lambda _key: True))
    except Report as delivered:
        return str(delivered)
    except Skip:
        return None
    raise AssertionError("the watch neither delivered nor skipped")


def test_on_the_watch_gate_wakes_for_a_reply(tmp_path: Path):
    _preview(tmp_path, True)
    item_id = _item()
    wl.apply_worker_report(CONDUCTOR, item_id, status="reply", summary="the answer")
    delivered = _gate_tick()
    assert delivered is not None and f"item={item_id} status=reply" in delivered


def test_the_watch_gate_reads_no_config_for_the_old_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The probe, like the push, pays for the switch's read only on a preview status."""
    item_id = _item()
    wl.apply_worker_report(CONDUCTOR, item_id, status="question", summary="which?")
    reads: list[int] = []
    monkeypatch.setattr(work_preview, "remote_dispatch", lambda: reads.append(1) or False)
    delivered = _gate_tick()
    assert delivered is not None and "status=question" in delivered
    assert reads == []


def test_off_the_watch_gate_does_not_wake_for_a_stored_reply(tmp_path: Path):
    """A reply written while on, read after the switch went off, wakes nobody."""
    _preview(tmp_path, True)
    item_id = _item()
    wl.apply_worker_report(CONDUCTOR, item_id, status="reply", summary="the answer")
    _preview(tmp_path, False)
    assert _gate_tick() is None
    assert not ledger_wake.is_actionable_event("report", "reply")


# ── nearby inputs ─────────────────────────────────────────────────────────


def test_on_a_reply_on_an_item_with_a_pending_verdict_keeps_the_verdict_and_wakes(
    tmp_path: Path,
):
    """A kept-open worker answers while its ``done`` is being evaluated: the report
    moves only the worker's fields, so the ``pending`` ruling stands."""
    _preview(tmp_path, True)
    item_id = _item()
    wl.apply_worker_report(
        CONDUCTOR, item_id, status="done", summary="shipped", artifacts={"pr": "1"}
    )
    wl.apply_conductor_action(CONDUCTOR, "verdict", item_id=item_id, verdict="pending")
    _gate_tick()  # the done's own wake
    wl.apply_worker_report(CONDUCTOR, item_id, status="reply", summary="the answer")
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    assert (item.status, item.verdict, item.state) == ("reply", "pending", "open")
    delivered = _gate_tick()
    assert delivered is not None and "status=reply" in delivered


@pytest.mark.asyncio
async def test_on_a_reply_from_an_unbound_session_is_refused_not_bound(tmp_path: Path):
    _preview(tmp_path, True)
    status, body = await _report("chat-nobody", {"status": "reply", "summary": "hi"})
    assert body.get("code") == "not_bound", (status, body)


def test_a_reply_never_reads_stale_because_the_next_move_is_the_conductors(tmp_path: Path):
    """Like ``done``: after a reply the conductor relays it, so the worker's silence is
    expected. A worker whose session is gone afterwards is not flagged either."""
    _preview(tmp_path, True)
    item_id = _item()
    wl.apply_worker_report(CONDUCTOR, item_id, status="reply", summary="the answer")
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    later = datetime.now(timezone.utc) + timedelta(days=2)
    assert not wl.is_stale(item, worker_running=False, now=later)
    assert not wl.is_stale(item, worker_running=False, worker_closed=True, now=later)
