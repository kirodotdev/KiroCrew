"""R4: prevent stale Crew work attribution when pooled Workers become unbound."""
from __future__ import annotations

import json
from dataclasses import dataclass
from io import StringIO
from types import MethodType
from unittest.mock import patch

import pytest

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.crew_ledger_bridge import (
    CrewLedgerCorrelationError,
    add_crew_work_link_to_prompt,
    prepare_crew_work_ledger_prompt,
)
from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.protocol import initialize_result
from kiro_byo.session_events import derive_work_link


SLOT = "dashboard:conductor"
WORKER = "dashboard:worker"
ITEM = "it_cafebabe"


@dataclass(slots=True)
class Conductor:
    slot_key: str = SLOT
    generation: str = "generation-1"
    schema: int = 1
    private_note: str = "should not be forwarded"


@dataclass(slots=True)
class WorkItem:
    item_id: str = ITEM
    worker_session_key: str = WORKER
    state: str = "open"
    schema: int = 1
    private_content: str = "should not be forwarded"


class Ledger:
    def __init__(self):
        self.binding = (SLOT, ITEM)
        self.reads = []

    def read_binding(self, worker):
        assert worker == WORKER
        self.reads.append("binding")
        return self.binding

    def read_conductor(self, slot, *, strict):
        assert slot == SLOT and strict
        self.reads.append("conductor")
        return Conductor()

    def read_work_item(self, slot, item, *, strict):
        assert slot == SLOT and item == ITEM and strict
        self.reads.append("item")
        return WorkItem()


def prepared(ledger, params=None, *, expected=False, backend="kiro-byo"):
    return prepare_crew_work_ledger_prompt(
        params or {"sessionId": "session"}, backend=backend,
        initialize_response=initialize_result(), worker_session_key=WORKER,
        ledger=ledger, expected_work_item=expected,
    )


def test_slots_records_are_supported_without_exposing_private_fields():
    ledger = Ledger()
    output = prepared(ledger, expected=True)
    work = output["_meta"]["kiro"]["byoWorkLink"]
    assert work["taskId"].startswith("crew-work:") and work["role"] == "worker"
    assert "private" not in json.dumps(output)
    assert ledger.reads == ["binding", "conductor", "item", "binding"]


def test_unbound_worker_explicitly_detaches_without_taking_over_other_backends():
    ledger = Ledger()
    ledger.binding = None
    params = {"sessionId": "session", "_meta": {"kiro": {"customAgents": ["a"]}, "other": 1}}
    original = json.loads(json.dumps(params))
    output = prepared(ledger, params)
    assert output["_meta"]["kiro"] == {"customAgents": ["a"], "byoWorkLink": {}}
    assert output["_meta"]["other"] == 1 and params == original
    assert ledger.reads == ["binding"]
    ledger.reads.clear()
    assert prepared(ledger, params, backend="kas") == params
    assert ledger.reads == []
    with pytest.raises(CrewLedgerCorrelationError, match="no active Worker binding"):
        prepared(ledger, params, expected=True)
    with pytest.raises(CrewLedgerCorrelationError):
        add_crew_work_link_to_prompt(params, backend="kiro-byo", extension_advertised=True, work_link={})


def test_unbound_crew_turn_clears_prior_run_lineage_before_next_run(tmp_path):
    """Exercise the Crew helper through real ACP prompt acceptance and Run storage."""
    ledger = Ledger()
    with patch("kiro_byo.acp_server.home_dir", return_value=tmp_path / "home"):
        out = StringIO()
        server = AcpServer(stdin=StringIO(), stdout=out)
        session = Session("session", str(tmp_path), "kiro-byo/echo")
        server._persist_session(session)
        server._put_session(session)
        store = DurableRunStore(tmp_path / "runs")
        server._kiro_durable_run_store = store
        run_ids = []

        def turn(self, rid, params, current, sid, *rest):
            run_id = self._begin_durable_run(
                current, None, selection=current.model, provider_id="local", model_id="echo",
            )
            run_ids.append(run_id)
            self._finish_durable_run(current, outcome="success", provider_id="local", model_id="echo")
            self.result(rid, {"stopReason": "end_turn"})

        server._execute_prompt_request_locked = MethodType(turn, server)
        try:
            for rid in (1, 2):
                if rid == 2:
                    ledger.binding = None
                params = prepared(ledger, {"sessionId": session.session_id, "prompt": [{"type": "text", "text": "work"}]})
                server.handle({"jsonrpc": "2.0", "id": rid, "method": "session/prompt", "params": params})
                response = next(json.loads(x) for x in reversed(out.getvalue().splitlines()) if json.loads(x).get("id") == rid)
                assert response.get("result") == {"stopReason": "end_turn"}, response
            assert store.get(run_ids[0])["metadata"]["host_work_link"]["taskId"].startswith("crew-work:")
            assert store.get(run_ids[1])["metadata"].get("host_work_link") in ({}, None)
            assert derive_work_link(server._load_persisted_record(session.session_id)["events"]) == {}
            assert session.host_work_link == {}
        finally:
            server._executor.shutdown(wait=True)


def test_old_acp_client_without_extension_does_not_gain_metadata():
    ledger = Ledger()
    plain = {"sessionId": "session", "prompt": []}
    out = prepare_crew_work_ledger_prompt(
        plain, backend="kiro-byo", initialize_response={},
        worker_session_key=WORKER, ledger=ledger,
    )
    assert out == plain and ledger.reads == []
