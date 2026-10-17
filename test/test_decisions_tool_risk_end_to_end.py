"""One trusting session's tool call, all the way across the seam: decide, show, judge.

The halves of this feature were each built against a contract rather than against
each other: the point writes a row and hands it back, the tool-call branch stamps
whatever it is handed, and the verdict route files an answer against a ``turn_id``
it never saw produced. Each side has its own suite
(``test_decisions_tool_risk.py``, ``test_decisions_tool_risk_card.py``,
``test_decisions_feedback_route.py``), and none of them can see the JOIN -- a
``turn_id`` that reached the badge but not the log would leave the thumbs posting
verdicts about nothing, with no error anywhere.

So this drives the real chain once with nothing stubbed between the links: the
real ``_run_chat`` on a trusting slot, the real point, the real gate with the real
scrub, the real day-file, and the real feedback handler. Only the provider is a
stand-in, because the one thing this must not do is send anything.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_decisions_tool_risk_card import (
    _quiet_sel,
    _runner,
    _scripts,
    _settle,
    _slot,
    _tool_call,
    _tool_rows,
)

from kiro_crew import decisions as core
from kiro_crew.dashboard.handlers.decisions import api_decisions_feedback
from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions.points import tool_risk as tr
from kiro_crew.decisions.types import Answer

from kiro_crew.dashboard import chat_runner  # isort: skip


@pytest.fixture(autouse=True)
def _generous_append_deadline(monkeypatch):
    """Take every production append deadline off the critical path of this test.

    The assertions read rows back off the day-file, so they depend on the real
    writer beating three budgets that exist to stop an observation occupying a
    caller: ``platform_log_append._APPEND_TIMEOUT_SECONDS`` (0.5 s, lock
    contention) and the two 50 ms ``asyncio.wait_for`` ceilings around the
    off-loop append -- ``gate._LOG_BUDGET_SECS`` for the CALL row and
    ``tool_risk.LOG_BUDGET_SECS`` for the OUTCOME row, whose expiry returns
    ``None`` and leaves the card unannotated. The Windows shard lost the outcome
    row's 50 ms on 14 unrelated heads in two days (``the card must carry the
    annotation`` / ``a redacted argument is a question, not a refusal``), so the
    chain under test was never reached. Raised, not removed: a genuinely stuck
    writer still fails, by name, inside the suite's ``--timeout``.
    """
    from kiro_crew import platform_log_append

    monkeypatch.setattr(platform_log_append, "_APPEND_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(core.gate, "_LOG_BUDGET_SECS", 20.0)
    monkeypatch.setattr(tr, "LOG_BUDGET_SECS", 20.0)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """One decision log for the whole chain: the call row, the outcome row, the verdict."""
    directory = tmp_path / "decisions"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(log_mod, "log_dir", lambda: directory)
    return directory


ENDPOINT = "https://api.typesafe.ai/v1/systemone"


@pytest.fixture
def sampled(tmp_path, monkeypatch):
    """The seam on for this session, with a budget the test outlives.

    ``core.decide`` is NOT patched, and neither is the consent read: a REAL keystone
    is written, so the gate's own consent check, the tool-argument scope, the bucket,
    the scrub and its call row are all part of the chain under test. Writing the file
    rather than stubbing ``_consented_for`` is the difference between covering the
    scope and asserting around it -- a stub of that function would make this test
    pass with the scope check deleted.

    Only two things are replaced: the governance probe, which reaches the platform
    profile store, and the transport, because the one thing this must not do is send
    anything.

    The config stand-in is a plain namespace rather than a ``MagicMock``: the gate
    renders ``provider.model`` onto the wire and refuses anything that is not a
    short identifier, so a mock's ``repr`` reads as an egress channel and the whole
    request is scrubbed -- a refusal that has nothing to do with what is under test.
    """
    keystone = tmp_path / "decisions_consent.json"
    keystone.write_text(
        json.dumps({"enabled": True, "endpoint": ENDPOINT, "tool_args": True}), encoding="utf-8"
    )
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: keystone)
    monkeypatch.setattr(
        "kiro_crew.decisions.capability.is_decisions_denied", lambda *_a, **_kw: False
    )
    provider = SimpleNamespace(endpoint=ENDPOINT, model="", timeout_ms=5000, api_key="")
    snapshot = SimpleNamespace(
        decisions=SimpleNamespace(bucket=100, provider=provider, history_budget_chars=0)
    )
    monkeypatch.setattr(core.gate, "_snapshot", lambda: snapshot)

    class _Oracle:
        def __init__(self, _provider):
            pass

        async def ask(self, _state, _questions):
            return {tr.QUESTION_ID: Answer(id=tr.QUESTION_ID, value=tr.TIER_RISKY, p=0.88)}

    monkeypatch.setattr("kiro_crew.decisions.impl_jev.JevOracle", _Oracle)
    return keystone


def _rows():
    path = log_mod.log_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _owner_request(body):
    """A request shaped like a real dashboard OWNER call to the verdict route."""
    request = MagicMock()
    request.path = "/api/decisions/feedback"
    store = {"app": "", "user": "owner-1"}
    request.get = lambda key, default=None: store.get(key, default)
    request.__contains__ = lambda _self, key: key in store
    request.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = "owner-1"
    request.app = {"state": state}
    request.query = {}
    request.json = AsyncMock(return_value=body)
    return request


@pytest.mark.asyncio
async def test_a_trusted_tool_call_is_flagged_on_its_card_and_takes_a_verdict(
    tmp_path, home, sampled, monkeypatch
):
    """decide -> the tool card carries it -> a verdict is filed against the same turn."""
    import kiro_crew.dashboard.handlers as handlers_pkg

    monkeypatch.setattr(handlers_pkg, "sel", lambda: MagicMock())
    state, client = _runner(tmp_path)
    slot = _slot("chat-risk-e2e")
    state.is_yolo_active = MagicMock(return_value=False)
    _scripts(client, [_tool_call()])

    with _quiet_sel():
        await chat_runner._run_chat(state, slot, "wipe /data please")
    await _settle(slot)

    rows = _tool_rows(slot)
    assert len(rows) == 1, f"expected one tool card, got {slot.messages}"
    record = (rows[0].get("meta") or {}).get("decisions_tool_risk")
    assert record is not None, "the card must carry the annotation the oracle answered"
    assert record["tier"] == tr.TIER_RISKY
    assert record["p"] == 0.88
    assert record["tool"] == "bash"
    assert record["policy"] == "trust"
    assert record["flagged"] is True

    # The gate wrote the CALL row and the point wrote the OUTCOME row, and they
    # share the turn the badge names. A turn id the log never saw is exactly the
    # silent failure this test exists for.
    logged = _rows()
    call_rows = [row for row in logged if row.get("call_index") is not None]
    outcome_rows = [row for row in logged if row.get("tier") is not None]
    assert len(call_rows) == 1 and len(outcome_rows) == 1
    assert call_rows[0]["point"] == tr.POINT
    assert call_rows[0]["scrubbed"] is False, "the arguments were redacted, not refused"
    assert {call_rows[0]["turn_id"], outcome_rows[0]["turn_id"]} == {record["turn_id"]}
    assert outcome_rows[0] == record

    response = await api_decisions_feedback(
        _owner_request({"turn_id": record["turn_id"], "verdict": "wrong", "side": "jev"})
    )

    assert response.status == 200
    verdicts = [row for row in _rows() if row.get("kind") == "feedback"]
    assert len(verdicts) == 1
    assert verdicts[0]["turn_id"] == record["turn_id"]
    assert (verdicts[0]["verdict"], verdicts[0]["side"]) == ("wrong", "jev")
    # Appended, never rewritten: the row this judges is still there, unchanged.
    assert [row for row in _rows() if row.get("tier") is not None] == outcome_rows


@pytest.mark.asyncio
async def test_a_credential_in_the_command_is_annotated_rather_than_refused(
    tmp_path, home, sampled
):
    """Through the REAL scrub: a secret in a tool argument must not silence the seam.

    The gate refuses a state carrying a credential, and tool arguments carry them
    routinely. The point redacts before the gate sees them, so this is the case
    that proves the annotation survives it -- and that nothing unredacted was in
    the request the gate cleared.
    """
    state, client = _runner(tmp_path)
    slot = _slot("chat-risk-e2e-secret")
    state.is_yolo_active = MagicMock(return_value=False)
    _scripts(
        client,
        [
            _tool_call(
                arguments=(
                    '{"command": "aws configure set aws_access_key_id AKIAIOSFODNN7EXAMPLE"}'
                )
            )
        ],
    )

    with _quiet_sel():
        await chat_runner._run_chat(state, slot, "set up the profile")
    await _settle(slot)

    record = (_tool_rows(slot)[0].get("meta") or {}).get("decisions_tool_risk")
    assert record is not None, "a redacted argument is a question, not a refusal"
    assert record["tier"] == tr.TIER_RISKY
    assert all(row["scrubbed"] is False for row in _rows())


@pytest.mark.asyncio
async def test_revoking_the_tool_argument_scope_silences_the_whole_chain(tmp_path, home, sampled):
    """The same real keystone, with the one field an owner controls set back to false.

    This is the state every install consented before the scope existed is in, driven
    through the real ``_run_chat`` rather than asserted at the gate: nothing is sent,
    nothing is written, and the tool card is exactly the row this build appends
    without the seam.
    """
    sampled.write_text(
        json.dumps({"enabled": True, "endpoint": ENDPOINT, "tool_args": False}), encoding="utf-8"
    )
    state, client = _runner(tmp_path)
    slot = _slot("chat-risk-e2e-unscoped")
    # PINNED, so `model.route` does not also run on this turn and the assertion
    # below can stay an exact whole-log comparison. A filter would have let any
    # other point's unexpected row through unnoticed, which is the opposite of what
    # this assertion is for. The pin is inert for this test: the tool-risk path
    # reads the call, never the slot's model.
    slot.model = "model-pinned-so-nothing-routes"
    state.is_yolo_active = MagicMock(return_value=False)
    _scripts(client, [_tool_call()])

    with _quiet_sel():
        await chat_runner._run_chat(state, slot, "wipe /data please")
    await _settle(slot)

    rows = _tool_rows(slot)
    assert rows, f"expected a tool card, got {slot.messages}"
    assert "decisions_tool_risk" not in (rows[0].get("meta") or {})
    assert _rows() == [], "no call row and no outcome row for a point that never ran"
