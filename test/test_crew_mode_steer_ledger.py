"""Crew Mode decision 7: a person's steer into a worker is one ``steer`` event on
that worker's item in the root's work ledger.

Every case names the rule it guards and fails when that rule is removed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR, WORKER, _new_item

from kiro_crew import work_ledger as wl

_isolated_home = work_ledger_helpers._isolated_home


def _bound_item() -> str:
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_id, worker_session_key=WORKER)
    return item_id


# ── the store ────────────────────────────────────────────────────────────────


def test_a_steer_is_one_event_and_moves_no_field():
    item_id = _bound_item()
    before = wl.read_work_item(CONDUCTOR, item_id).to_dict()
    out = wl.apply_person_steer(CONDUCTOR, item_id, "use the other branch")
    assert out is not None and out["event"].kind == "steer"
    assert out["event"].text == "use the other branch"
    after = wl.read_work_item(CONDUCTOR, item_id).to_dict()
    # Acceptance, verdict and state stay the conductor's and the worker's.
    assert {k: v for k, v in after.items() if k != "updated_at"} == {
        k: v for k, v in before.items() if k != "updated_at"
    }
    kinds = [e.kind for e in wl.read_events(CONDUCTOR, item_id)]
    assert kinds[-1] == "steer"


def test_a_steer_text_is_cut_to_the_event_cap():
    item_id = _bound_item()
    out = wl.apply_person_steer(CONDUCTOR, item_id, "x" * 2000)
    assert out is not None and len(out["event"].text) == wl.MAX_EVENT_TEXT_CHARS


def test_a_steer_on_a_closed_item_writes_nothing():
    item_id = _bound_item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="abandoned")
    assert wl.apply_person_steer(CONDUCTOR, item_id, "too late") is None
    assert "steer" not in [e.kind for e in wl.read_events(CONDUCTOR, item_id)]


def test_the_fold_keeps_a_person_steer_as_an_event_only():
    from kiro_crew.crew_log.projection_work import _work_apply

    item: dict[str, Any] = {"item_id": "it_1", "events": [], "status": "progress"}
    _work_apply(
        item,
        {
            "actor": "person",
            "action": "steer",
            "status": "done",
            "summary": "forged",
            "event_kind": "steer",
            "event": "stop and rebase",
        },
        1000,
    )
    assert item["status"] == "progress" and "summary" not in item
    assert [(e["kind"], e["text"]) for e in item["events"]] == [("steer", "stop and rebase")]


def test_the_entry_type_admits_the_person_and_the_steer():
    from kiro_crew.work_vocab import WORK_ACTIONS, WORK_ACTORS, WORK_EVENT_KINDS

    assert "person" in WORK_ACTORS
    assert "steer" in WORK_ACTIONS and "steer" in WORK_EVENT_KINDS


# ── the steer route ──────────────────────────────────────────────────────────


@pytest.fixture
def _route(monkeypatch):
    """``person_steer`` with the ledger read and the crew log stubbed."""
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.dashboard.handlers import crew_workers
    from kiro_crew.dashboard.handlers import work_ledger as wlh
    from kiro_crew.dashboard.handlers import work_ledger_worker as wlw

    entries: list[dict[str, Any]] = []
    for mod in (wlh, wlw):
        monkeypatch.setattr(mod, "_steer_unit", lambda *_a, **_k: "unit-1")
    monkeypatch.setattr(crew_log_emit, "enabled", lambda: True)

    def fake_record(unit: str, data: dict[str, Any], **_k: Any) -> bool:
        entries.append(data)
        return True

    monkeypatch.setattr(crew_log_emit, "on_work_recorded", fake_record)
    return SimpleNamespace(cw=crew_workers, entries=entries, monkeypatch=monkeypatch)


@pytest.mark.asyncio
async def test_a_steer_lands_on_the_workers_item_redacted(_route):
    item_id = _bound_item()
    facts = _route.cw.LedgerFacts(item_id, "", "")
    _route.monkeypatch.setattr(_route.cw, "_ledger_items", lambda _root: {WORKER: facts})
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    got = (await _route.cw.person_steer(SimpleNamespace(), CONDUCTOR, WORKER, f"use {secret}"))[
        "ledger_event"
    ]
    assert got == item_id
    [entry] = _route.entries
    assert entry["actor"] == "person" and entry["action"] == "steer"
    assert entry["event_kind"] == "steer"
    assert secret not in json.dumps(entry)
    [steer] = [e for e in wl.read_events(CONDUCTOR, item_id) if e.kind == "steer"]
    assert secret not in steer.text and steer.text.startswith("use ")


@pytest.mark.asyncio
async def test_a_worker_with_no_item_gets_the_steer_and_nothing_is_written(_route):
    _route.monkeypatch.setattr(_route.cw, "_ledger_items", lambda _root: {})
    got = (await _route.cw.person_steer(SimpleNamespace(), CONDUCTOR, WORKER, "hi"))["ledger_event"]
    assert got is None and _route.entries == []


@pytest.mark.asyncio
async def test_an_unrecorded_steer_is_undone(_route):
    from kiro_crew.crew_log import emit as crew_log_emit

    item_id = _bound_item()
    facts = _route.cw.LedgerFacts(item_id, "", "")
    _route.monkeypatch.setattr(_route.cw, "_ledger_items", lambda _root: {WORKER: facts})
    _route.monkeypatch.setattr(crew_log_emit, "on_work_recorded", lambda *_a, **_k: False)
    got = (await _route.cw.person_steer(SimpleNamespace(), CONDUCTOR, WORKER, "hi"))["ledger_event"]
    assert got is None
    assert "steer" not in [e.kind for e in wl.read_events(CONDUCTOR, item_id)]


def test_the_steer_route_writes_the_event_after_delivery():
    from kiro_crew.dashboard.handlers import crew_workers

    source = open(crew_workers.__file__, encoding="utf-8").read()
    body = source.split("async def api_crew_worker_steer", 1)[1]
    assert body.index("_deliver_as_person") < body.index("person_steer(")


def test_the_conductor_guidance_reads_steers_before_deciding_and_never_overrules():
    from pathlib import Path

    from kiro_crew import agent

    prompt = agent._CONDUCTOR_SYSTEM_PROMPT
    assert "A person's steer outranks your decision" in prompt
    assert "never overrule" in prompt and "accept_eval.py" in prompt
    skills = Path(agent.__file__).parent / "builtin_skills"
    goal = (skills / "goal-conductor" / "SKILL.md").read_text(encoding="utf-8")
    lead = (skills / "team-lead" / "SKILL.md").read_text(encoding="utf-8")
    assert "A person's steer outranks your decision" in goal
    assert "The person's steer outranks you" in lead
