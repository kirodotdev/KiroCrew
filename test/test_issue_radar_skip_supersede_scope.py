"""A human answer supersedes only an ANSWERABLE pass in the shared skip index.

Crew A holds a scope-gate pass (``architecture``) on an issue while crew B's
``needs-decision`` pass on it is answered. A later pass that supersedes that answer
must not replace A's scope-gate row in ``read_skips``: a scope gate stays the first
decision whoever removes the label.
"""

from unittest import mock

from kiro_crew.apps.builtins.issue_radar.backend import crew_store_ledger as ledger

_ANSWER_AT = "2026-11-01T12:00:00.000000Z"


def _row(crew_id: str, scope: str, decided_at: str) -> dict:
    return {
        "number": 7,
        "reason": f"{scope} pass",
        "scope": scope,
        "crew_id": crew_id,
        "decided_at": decided_at,
        "deferred": False,
    }


def _answer(superseded: bool) -> dict:
    row: dict = {"number": 7, "actor": "alice", "at": _ANSWER_AT, "crew_id": "c_bbbbbbbb"}
    if superseded:
        row["superseded_at"] = "2026-11-02T10:00:00.000000Z"
        row["pass"] = {
            "crew_id": "c_aaaaaaaa",
            "reason": "still needs a decision",
            "scope": "needs-decision",
            "decided_at": "2026-11-02T10:00:00.000000Z",
        }
    return row


def _skips(rows: list[dict], answer: dict) -> dict:
    ledgers = [{"skips": {"7": row}} for row in rows]
    with (
        mock.patch.object(ledger, "_all_ledgers", return_value=ledgers),
        mock.patch.object(ledger, "read_answers", return_value={"7": answer}),
    ):
        return ledger.read_skips("o", "r")


def test_an_answer_does_not_replace_a_scope_gate_pass():
    gate = _row("c_aaaaaaaa", "architecture", "2026-11-01T10:00:00.000000Z")
    asked = _row("c_bbbbbbbb", "needs-decision", "2026-11-01T11:00:00.000000Z")
    for superseded in (False, True):
        row = _skips([gate, asked], _answer(superseded))["7"]
        assert row["scope"] == "architecture"
        assert row["reason"] == "architecture pass"


def test_an_answer_still_replaces_an_answerable_pass():
    asked = _row("c_bbbbbbbb", "needs-decision", "2026-11-01T11:00:00.000000Z")
    assert "7" not in _skips([asked], _answer(False))  # answered: handed back
    row = _skips([asked], _answer(True))["7"]  # passed on again: the new pass stands
    assert row["reason"] == "still needs a decision"
    assert row["answered_at"] == _ANSWER_AT
