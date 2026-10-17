"""Every auto-nudge loop stop is also appended to a JSONL file that outlives restarts."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from kiro_crew import autonudge as _an
from kiro_crew import autonudge_stop_log as stoplog
from kiro_crew.autonudge import AutoNudgeService

# Obviously fake GitHub classic PAT shape, split so no scanner sees a literal.
_FAKE_PAT = "ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef12"


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture(autouse=True)
def _unpublish():
    yield
    _an._INSTANCE = None


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _stop_one(base_dir, *, slot="chat-7", reason="runtime_budget"):
    async def body():
        svc = AutoNudgeService(base_dir=base_dir)
        loop = await svc.add(slot, "tick", idle_secs=60)
        await svc.update(loop.id, active=False, stopped_reason=reason)
        svc.stop()
        return loop.id

    return asyncio.run(body())


def test_a_stop_lands_in_the_stop_file(tmp_path):
    loop_id = _stop_one(tmp_path)
    [rec] = _lines(stoplog.stops_path(tmp_path))
    assert stoplog.stops_path(tmp_path) == tmp_path / "logs" / "autonudge_stops.jsonl"
    assert (rec["loop_id"], rec["slot_key"], rec["reason"]) == (loop_id, "chat-7", "runtime_budget")
    assert isinstance(rec["ts"], float)


def test_the_file_keeps_stops_across_service_restarts(tmp_path):
    first = _stop_one(tmp_path, slot="chat-1", reason="cycle_cap")
    second = _stop_one(tmp_path, slot="chat-2", reason="user_stop")
    got = [(r["loop_id"], r["reason"]) for r in _lines(stoplog.stops_path(tmp_path))]
    assert got == [(first, "cycle_cap"), (second, "user_stop")]


def test_the_file_holds_the_same_scrubbed_text_as_the_log_line(tmp_path):
    before = stoplog.active_summaries([{"id": "a", "slot_key": "s", "active": True}])
    [record] = stoplog.stop_records(before, [], {"a": ("autonudge_stop", f"done {_FAKE_PAT}")})
    stoplog.append_record(tmp_path, record)
    text = stoplog.stops_path(tmp_path).read_text(encoding="utf-8")
    assert "ghp_" not in text
    assert _FAKE_PAT[4:12] not in text


def test_a_newline_in_store_text_cannot_forge_a_second_line(tmp_path):
    stoplog.append_record(tmp_path, {"loop_id": 'a\n{"forged":1}', "reason": "r\r\n"})
    [line] = stoplog.stops_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["loop_id"] == 'a\n{"forged":1}'


def test_the_file_rotates_at_its_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(stoplog, "STOPS_MAX_BYTES", 1)
    stoplog.append_record(tmp_path, {"loop_id": "old"})
    stoplog.append_record(tmp_path, {"loop_id": "new"})
    live = stoplog.stops_path(tmp_path)
    assert [r["loop_id"] for r in _lines(live)] == ["new"]
    assert [r["loop_id"] for r in _lines(live.with_name(live.name + ".1"))] == ["old"]


def test_an_unwritable_stop_file_keeps_the_store_write_and_the_log_line(tmp_path, caplog):
    # ``logs`` is a plain file, so the directory cannot be made and every append fails.
    (tmp_path / "logs").write_text("not a directory", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        loop_id = _stop_one(tmp_path)
    messages = [r.getMessage() for r in caplog.records]
    assert any("reason='runtime_budget'" in m for m in messages)
    assert any("could not append a stop record" in m for m in messages)
    stored = json.loads((tmp_path / "autonudge.json").read_text(encoding="utf-8"))
    [row] = [r for r in stored["loops"] if r["id"] == loop_id]
    assert (row["active"], row["stopped_reason"]) == (False, "runtime_budget")
