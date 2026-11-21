"""Phase 5 Crew-native Work Ledger mapping; deliberately no Crew source edits."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_byo.crew_ledger_bridge import (
    CrewLedgerCorrelationError,
    byo_work_correlation_advertised,
    add_crew_work_link_to_prompt,
    work_link_from_crew_ledger,
)
from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.turn_runner import TurnRunnerMixin
from kiro_byo.work_trace import work_trace_snapshot


SLOT = "dashboard:conductor-1"
WORKER = "dashboard:worker-1"


def records(*, item_id="it_0badc0de", gen="board-generation-1"):
    conductor = {"slot_key": SLOT, "generation": gen, "round": 3, "schema": 1}
    item = {"item_id": item_id, "worker_session_key": WORKER, "round": 3,
            "state": "open", "schema": 1, "title": "sensitive project notes"}
    return conductor, item, (SLOT, item_id)


def build(conductor=None, item=None, binding=None, worker=WORKER):
    c, i, b = records()
    return work_link_from_crew_ledger(
        conductor=conductor if conductor is not None else c,
        item=item if item is not None else i,
        binding=binding if binding is not None else b,
        worker_session_key=worker,
    )


def test_link_scoped_to_work_ledger_board_and_item_not_round():
    link = build()
    assert link["taskId"].startswith("crew-work:")
    assert link == build()  # deterministic across worker restart
    assert link["role"] == "worker"
    assert "assignmentId" not in link  # Crew round != Assignment identifier
    c, i, b = records()
    c["round"] = 100
    i["round"] = 100
    assert build(c, i, b) == link
    assert link != build(*records(item_id="it_aaaaaaaa"))
    assert link != build(*records(gen="board-generation-2"))
    assert "dashboard" not in json.dumps(link)
    assert "sensitive" not in json.dumps(link)


@pytest.mark.parametrize("change", [
    "unbound", "moved", "closed", "invalid_id", "missing_generation", "missing_worker",
    "wrong_schema", "incorrect_slot", "inconsistent_item", "dangerous_slot",
])
def test_no_false_task_link_when_crew_evidence_is_insufficient(change):
    c, i, b = records()
    worker = WORKER
    if change == "unbound": b = ("", "")
    if change == "moved": b = (SLOT, "it_ffffffff")
    if change == "closed": i["state"] = "accepted"
    if change == "invalid_id": i["item_id"] = "../../etc"
    if change == "missing_generation": c["generation"] = ""
    if change == "missing_worker": worker = "dashboard:other-worker"
    if change == "wrong_schema": i["schema"] = 2
    if change == "incorrect_slot": c["slot_key"] = "dashboard:another"
    if change == "inconsistent_item": i["worker_session_key"] = "another"
    if change == "dangerous_slot": c["slot_key"] = "../attack"
    with pytest.raises(CrewLedgerCorrelationError):
        work_link_from_crew_ledger(conductor=c, item=i, worker_session_key=worker, binding=b)


def test_prompt_extension_preserves_crew_payload_and_never_mutates_original():
    link = build()
    request = {"sessionId": "sid-1", "prompt": [{"type": "text", "text": "hello"}],
               "_meta": {"kiro": {"customAgents": ["safe-projection"]}, "another": 1}}
    actual = add_crew_work_link_to_prompt(request, backend="kiro-byo",
                                          extension_advertised=True, work_link=link)
    assert request["_meta"]["kiro"] == {"customAgents": ["safe-projection"]}
    assert actual["_meta"]["kiro"]["customAgents"] == ["safe-projection"]
    assert actual["_meta"]["kiro"]["byoWorkLink"] == link
    assert actual["_meta"]["another"] == 1
    # Reuse is idempotent (no duplicate changes to the same identity).
    assert add_crew_work_link_to_prompt(actual, backend="kiro-byo",
                                        extension_advertised=True, work_link=link) == actual
    for backend in ("kiro", "kas", "claude", "unknown"):
        assert add_crew_work_link_to_prompt(request, backend=backend,
                                            extension_advertised=False, work_link=link) == request


def test_capability_and_conflicting_identity_fail_closed():
    link = build()
    with pytest.raises(CrewLedgerCorrelationError, match="not negotiated"):
        add_crew_work_link_to_prompt({}, backend="kiro-byo", extension_advertised=False, work_link=link)
    with pytest.raises(CrewLedgerCorrelationError, match="conflicting"):
        add_crew_work_link_to_prompt({"_meta": {"kiro": {"byoWorkLink": {"taskId": "other"}}}},
                                     backend="kiro-byo", extension_advertised=True, work_link=link)
    for candidate in ({}, {"role": "worker"}, {"taskId": "x", "role": "conductor"}):
        with pytest.raises(CrewLedgerCorrelationError):
            add_crew_work_link_to_prompt({}, backend="kiro-byo", extension_advertised=True,
                                         work_link=candidate)


def test_durable_run_uses_immutable_per_item_link_even_when_worker_rebound(tmp_path):
    class Runner(TurnRunnerMixin):
        runtime_trace = None
        def trace(self, _event):
            pass
    runner = Runner()
    runner._kiro_durable_run_store = DurableRunStore(tmp_path / "runs")
    link1 = build()
    link2 = build(*records(item_id="it_00dd0011"))
    session = SimpleNamespace(
        session_id="same-worker-session", events=[], agent=SimpleNamespace(agent_id="kirocrew-mate-worker"),
        host_work_link=link1, active_agent_plan_id="", active_run_id="", active_run_step_id="", active_run_attempt_id="",
    )
    id1 = runner._begin_durable_run(session, None, selection="fixed", provider_id="provider", model_id="model")
    session.host_work_link = link2
    id2 = runner._begin_durable_run(session, None, selection="fixed", provider_id="provider", model_id="model")
    ledger = runner._kiro_durable_run_store
    assert ledger.get(id1)["metadata"]["host_work_link"] == link1
    assert ledger.get(id2)["metadata"]["host_work_link"] == link2
    output = work_trace_snapshot(store=ledger)
    assert output["summary"]["linkedTasksInWindow"] == 2
    assert output["summary"]["linkedRunsInWindow"] == 2
    assert output["unlinkedCrewRuns"] == []


def test_source_preflight_requires_true_upstream_owners(tmp_path):
    import importlib.util
    p = Path(__file__).resolve().parents[1] / "scripts/verify_crew_work_ledger.py"
    spec = importlib.util.spec_from_file_location("_phase5_preflight", p)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    assert module.inspect(tmp_path)["work_ledger_source_surface_checked"] is False
    for name, symbols in module.REQUIRED.items():
        (tmp_path / name).write_text("\n".join("def " + n + "(): pass" if n != "WorkItem" and n != "ConductorRecord" else "class " + n + ": pass" for n in symbols) + "\n", encoding="utf-8")
    (tmp_path / "mate_worker.py").write_text("# MATE_WORKER_AGENT session_create session_send\n", encoding="utf-8")
    (tmp_path / "mcp_work.py").write_text("# WORKER_TOOLS CONDUCTOR_TOOLS work_brief work_report\n", encoding="utf-8")
    result = module.inspect(tmp_path)
    assert result["work_ledger_source_surface_checked"] is True
    assert result["crew_host_patch_installed"] is False
    assert result["windows_gateway_e2e_verified"] is False


def test_capability_check_matches_real_byo_initialize_contract():
    from kiro_byo.protocol import initialize_result
    assert byo_work_correlation_advertised(initialize_result())
    assert not byo_work_correlation_advertised({})
    assert not byo_work_correlation_advertised({"agentCapabilities": {"_meta": {
        "kiro": {"workCorrelation": {"version": True, "scope": "diagnostic_only",
            "promptMetadata": "_meta.kiro.byoWorkLink"}}}}})
    assert not byo_work_correlation_advertised({"agentCapabilities": {"_meta": {
        "kiro": {"workCorrelation": {"version": 2, "scope": "diagnostic_only",
            "promptMetadata": "_meta.kiro.byoWorkLink"}}}}})
    # A matching version and metadata path alone are not enough: the host
    # must not send a bind to a child advertising another RPC method.
    from copy import deepcopy
    incorrect = deepcopy(initialize_result())
    incorrect["agentCapabilities"]["_meta"]["kiro"]["workCorrelation"]["bindMethod"] = "_kiro/other/bind"
    assert not byo_work_correlation_advertised(incorrect)
    del incorrect["agentCapabilities"]["_meta"]["kiro"]["workCorrelation"]["bindMethod"]
    assert not byo_work_correlation_advertised(incorrect)



def test_crew_host_reader_adapter_wires_only_byo_and_preserves_legacy():
    from kiro_byo.crew_ledger_bridge import prepare_crew_work_ledger_prompt
    from kiro_byo.protocol import initialize_result

    class Ledger:
        def __init__(self):
            self.c, self.i, self.b = records()
            self.calls = []
        def read_binding(self, worker):
            self.calls.append(("binding", worker))
            return self.b
        def read_conductor(self, slot, *, strict=False):
            assert strict is True
            self.calls.append(("conductor", slot))
            return SimpleNamespace(**self.c)
        def read_work_item(self, slot, item_id, *, strict=False):
            assert strict is True
            self.calls.append(("item", item_id))
            return SimpleNamespace(**self.i)

    ledger = Ledger()
    params = {"sessionId": "sid", "prompt": []}
    for backend in ("kas", "kiro"):
        assert prepare_crew_work_ledger_prompt(
            params, backend=backend, initialize_response=initialize_result(),
            worker_session_key=WORKER, ledger=ledger, expected_work_item=True,
        ) == params
    assert ledger.calls == []
    actual = prepare_crew_work_ledger_prompt(
        params, backend="kiro-byo", initialize_response=initialize_result(),
        worker_session_key=WORKER, ledger=ledger, expected_work_item=True,
    )
    assert actual["_meta"]["kiro"]["byoWorkLink"] == build()
    assert ledger.calls == [
        ("binding", WORKER), ("conductor", SLOT), ("item", "it_0badc0de"),
        ("binding", WORKER),
    ]
    assert "_meta" not in params


def test_crew_host_reader_adapter_rejects_rebound_or_missing_expected_item():
    from kiro_byo.crew_ledger_bridge import prepare_crew_work_ledger_prompt
    from kiro_byo.protocol import initialize_result
    c, i, b = records()

    class ChangingLedger:
        read_count = 0
        def read_binding(self, _worker):
            self.read_count += 1
            return b if self.read_count == 1 else (SLOT, "it_eeeeeeee")
        def read_conductor(self, *args, **kwargs):
            return c
        def read_work_item(self, *args, **kwargs):
            return i

    with pytest.raises(CrewLedgerCorrelationError, match="changed"):
        prepare_crew_work_ledger_prompt(
            {}, backend="kiro-byo", initialize_response=initialize_result(),
            worker_session_key=WORKER, ledger=ChangingLedger(), expected_work_item=True,
        )

    class Unbound:
        def read_binding(self, _worker):
            return None

    assert prepare_crew_work_ledger_prompt(
        {}, backend="kiro-byo", initialize_response=initialize_result(),
        worker_session_key=WORKER, ledger=Unbound(), expected_work_item=False,
    ) == {"_meta": {"kiro": {"byoWorkLink": {}}}}
    with pytest.raises(CrewLedgerCorrelationError, match="no active"):
        prepare_crew_work_ledger_prompt(
            {}, backend="kiro-byo", initialize_response=initialize_result(),
            worker_session_key=WORKER, ledger=Unbound(), expected_work_item=True,
        )
