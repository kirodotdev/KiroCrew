"""The conductor's verify step without the copied document, and a close that carries
its verdict.

Three things are pinned here:

* ``work_verify.select_done``: which items the evaluator is handed when
  ``accept_eval`` is called with no document. A board in, a selection out.
* ``accept_eval`` with no ``items``: it reads the caller's own ledger once, as the
  caller, and evaluates what ``select_done`` picked through the same evaluator and
  path screen the document form uses.
* ``work_ledger_record action=close`` with a ``verdict``: the verdict write, then the
  close, and no close when the verdict is refused.

Every effect is a recorded call on a stand-in for the gateway route, so each case
asserts what was sent and in which order.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import conductor_patrol, ledger_wake, mcp_core, mcp_work, work_verify
from kiro_crew.validation import ValidationError

ITEM_A = "it_0000000a"
ITEM_B = "it_0000000b"
ITEM_C = "it_0000000c"


def _row(item_id: str, status: str | None = "done", **fields: Any) -> dict[str, Any]:
    row = {
        "item_id": item_id,
        "state": "open",
        "status": status,
        "acceptance": {"kind": "file", "path": f"/work/{item_id}.txt"},
        "worker_session_key": f"chat-{item_id}",
    }
    row.update(fields)
    return row


def _board(*rows: dict[str, Any], not_concrete: tuple[str, ...] = ()) -> dict[str, Any]:
    """A full ledger read: the rows, and the batch the read route composes from them
    (every open item whose bar is concrete, whatever its status)."""
    batch = [
        {"id": row["item_id"], "accept": row["acceptance"], "status": row["status"]}
        for row in rows
        if row["state"] == "open" and row["item_id"] not in not_concrete
    ]
    return {"conductor": {}, "items": list(rows), "accept_batch": {"items": batch}}


def _ids(entries: list[dict[str, Any]]) -> list[str]:
    return [entry["id"] for entry in entries]


# ── which items the evaluator is handed ──────────────────────────────────────


def test_the_done_items_are_selected_in_ledger_order():
    board = _board(_row(ITEM_A), _row(ITEM_B, "progress"), _row(ITEM_C))
    entries, skipped = work_verify.select_done(board)
    assert _ids(entries) == [ITEM_A, ITEM_C]
    assert skipped == []


@pytest.mark.parametrize("status", ["progress", "blocked", "question", None])
def test_an_item_whose_worker_has_not_said_done_is_never_selected(status):
    """A worker that is still going can have made its bar true early: a stub file, a
    pull request that is green before the last commit. Its item is not handed to the
    evaluator, so that early answer cannot be read as an acceptance."""
    entries, skipped = work_verify.select_done(_board(_row(ITEM_A, status)))
    assert entries == [] and skipped == []


def test_the_entry_handed_over_is_the_boards_own_batch_entry():
    """The bar is the entry the read composed from the stored acceptance. Nothing the
    worker claimed in its report rides along."""
    board = _board(_row(ITEM_A, pr=999, artifacts={"pr": "999"}))
    entries, _skipped = work_verify.select_done(board)
    assert entries == [
        {"id": ITEM_A, "accept": {"kind": "file", "path": f"/work/{ITEM_A}.txt"}, "status": "done"}
    ]


def test_a_done_item_with_no_concrete_bar_is_named_not_evaluated():
    """A ``TBD`` bar is absent from the batch. The item is left out and the reply
    says to promote the value first."""
    board = _board(_row(ITEM_A), _row(ITEM_B), not_concrete=(ITEM_A,))
    entries, skipped = work_verify.select_done(board)
    assert _ids(entries) == [ITEM_B]
    assert skipped == [
        {
            "id": ITEM_A,
            "why": work_verify.NOT_CONCRETE,
            "acceptance": {"kind": "file", "path": f"/work/{ITEM_A}.txt"},
        }
    ]


def test_a_human_approval_item_is_left_for_the_person():
    """The evaluator can only answer ``pending`` for it, which reads as "keep
    waiting" for an item only a person can settle."""
    board = _board(_row(ITEM_A, acceptance={"kind": "human_approval"}))
    entries, skipped = work_verify.select_done(board)
    assert entries == []
    assert skipped == [{"id": ITEM_A, "why": work_verify.NEEDS_PERSON}]


def test_a_closed_item_is_not_selected_again():
    board = _board(_row(ITEM_A, state="accepted"), _row(ITEM_B))
    entries, skipped = work_verify.select_done(board)
    assert _ids(entries) == [ITEM_B] and skipped == []


def test_naming_an_item_selects_only_that_item():
    entries, skipped = work_verify.select_done(_board(_row(ITEM_A), _row(ITEM_B)), ITEM_B)
    assert _ids(entries) == [ITEM_B] and skipped == []


@pytest.mark.parametrize(
    "board, why",
    [
        (_board(_row(ITEM_A, "progress")), "its status is progress, not done"),
        (_board(_row(ITEM_A, None)), "its status is not reported yet, not done"),
        (_board(_row(ITEM_A, state="accepted")), "it is already accepted"),
        (_board(_row(ITEM_B)), "no item with this id is on your ledger"),
    ],
    ids=["still-working", "never-reported", "closed", "unknown"],
)
def test_a_named_item_that_cannot_be_checked_says_why(board, why):
    """Asked about one item, the answer is never an empty list: a caller that named
    an item reads why it got no verdict for it."""
    entries, skipped = work_verify.select_done(board, ITEM_A)
    assert entries == []
    assert skipped == [{"id": ITEM_A, "why": why}]


@pytest.mark.parametrize("board", [{}, {"items": None}, {"items": [1, "x"], "accept_batch": 3}])
def test_a_board_of_an_unexpected_shape_selects_nothing(board):
    assert work_verify.select_done(board) == ([], [])


# ── what rides back beside an answer ─────────────────────────────────────────


def test_the_facts_of_a_row_are_what_its_worker_reported_and_what_the_ledger_holds():
    board = _board(_row(ITEM_A, summary="round 1 written", pr=41, fails=2))
    assert work_verify.row_facts(board) == {
        ITEM_A: {
            "reported": {"summary": "round 1 written", "pr": 41},
            "fails": 2,
            "worker_session_key": f"chat-{ITEM_A}",
        }
    }


def test_a_field_the_row_does_not_carry_is_left_out():
    """An empty summary, no claimed pull request and a count that is not a number
    say nothing, so nothing is written for them; a row with no fact at all is absent."""
    board = _board(
        _row(ITEM_A, summary="", pr=None, fails=True),
        _row(ITEM_B, worker_session_key=None, fails=0),
        _row(ITEM_C, worker_session_key=""),
    )
    assert work_verify.row_facts(board) == {
        ITEM_A: {"worker_session_key": f"chat-{ITEM_A}"},
        ITEM_B: {"fails": 0},
    }


def test_an_overdue_item_carries_the_flag_and_the_callers_last_decision():
    """The pending budget is settled from the flag and from the marker the caller
    left in ``decision``, and re-arming writes the stored bar again, so that comes
    back too. A decision on an item that is not overdue is not sent."""
    board = _board(
        _row(ITEM_A, pending_overdue=True, decision="pending overdue: re-armed once"),
        _row(ITEM_B, pending_overdue=False, decision="keep going"),
        _row(ITEM_C, pending_overdue=True, decision=None),
    )
    facts = work_verify.row_facts(board)
    assert facts[ITEM_A]["pending_overdue"] is True
    assert facts[ITEM_A]["decision"] == "pending overdue: re-armed once"
    assert facts[ITEM_A]["acceptance"] == {"kind": "file", "path": f"/work/{ITEM_A}.txt"}
    assert "acceptance" not in facts[ITEM_B]
    assert "pending_overdue" not in facts[ITEM_B] and "decision" not in facts[ITEM_B]
    assert facts[ITEM_C]["pending_overdue"] is True and "decision" not in facts[ITEM_C]


@pytest.mark.parametrize("board", [{}, {"items": None}, {"items": [1, "x"]}])
def test_a_board_of_an_unexpected_shape_has_no_facts(board):
    assert work_verify.row_facts(board) == {}


# ── accept_eval with no document ─────────────────────────────────────────────


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    """A caller whose ledger read answers a board of real ``file`` bars.

    Returns ``(board, reads)``: the board the read hands back (replace its contents to
    change the case) and every path read, with the key it was read as.
    """
    present = tmp_path / "present.txt"
    present.write_text("x", encoding="utf-8")
    board = _board(
        _row(ITEM_A, acceptance={"kind": "file", "path": str(present)}),
        _row(ITEM_B, "progress", acceptance={"kind": "file", "path": str(present)}),
        _row(ITEM_C, acceptance={"kind": "file", "path": str(tmp_path / "absent.txt")}),
    )
    reads: list[tuple[str, str]] = []

    def _get(path: str, *, session_key: str) -> dict[str, Any]:
        reads.append((path, session_key))
        return board

    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-conductor")
    monkeypatch.setattr(mcp_work, "_get", _get)
    return board, reads


def _call(name: str, args: dict[str, Any]) -> str:
    return mcp_work._call_tool_inner(name, mcp_work._validate_args(name, args))


def test_with_no_document_the_callers_done_items_are_evaluated(ledger):
    """One read of the caller's own ledger, as the caller, feeds the evaluator: the
    two ``done`` items get a verdict each and the one still in progress gets none,
    though its bar is already true."""
    _board_doc, reads = ledger
    out = json.loads(_call("accept_eval", {}))
    assert reads == [("/api/work-ledger", "chat-conductor")]
    assert [(r["id"], r["verdict"]) for r in out["results"]] == [(ITEM_A, "pass"), (ITEM_C, "fail")]
    assert "skipped" not in out, "nothing was left out that the caller is owed a reason for"


def test_with_no_document_one_item_can_be_named(ledger):
    out = json.loads(_call("accept_eval", {"item_id": ITEM_C}))
    assert [(r["id"], r["verdict"]) for r in out["results"]] == [(ITEM_C, "fail")]


def test_a_named_item_still_in_progress_gets_a_reason_and_no_verdict(ledger):
    out = json.loads(_call("accept_eval", {"item_id": ITEM_B}))
    assert out["results"] == []
    assert out["skipped"] == [
        {
            "id": ITEM_B,
            "why": "its status is progress, not done",
            "worker_session_key": f"chat-{ITEM_B}",
        }
    ]


def test_a_failed_bar_comes_back_with_what_the_worker_reported(ledger):
    """The caller made no read of its own, so the answer carries what that read shows.
    A ``done`` that finished one step of a longer bar fails the bar, and its summary
    is how the caller tells that from a failed attempt; the count so far is what a
    failed attempt is recorded on top of."""
    board, _reads = ledger
    board["items"][2].update(summary="round 1 file written; the final file is next", fails=1)
    out = json.loads(_call("accept_eval", {"item_id": ITEM_C}))
    assert out["results"] == [
        {
            "id": ITEM_C,
            "verdict": "fail",
            "evidence": out["results"][0]["evidence"],
            "reported": {"summary": "round 1 file written; the final file is next"},
            "fails": 1,
            "worker_session_key": f"chat-{ITEM_C}",
        }
    ]


def test_an_item_with_no_concrete_bar_comes_back_with_the_claimed_pull_request(ledger):
    """Promoting the bar rewrites the whole stored object with the number the worker
    claimed, so both come back: the stored bar as ``acceptance`` and the claim under
    ``reported``, apart from it. The evaluator was handed neither."""
    board, _reads = ledger
    board["items"] = [_row(ITEM_A, acceptance={"kind": "pr_checks", "pr": "TBD"}, pr=77)]
    board["accept_batch"] = {"items": []}
    out = json.loads(_call("accept_eval", {}))
    assert out == {
        "results": [],
        "skipped": [
            {
                "id": ITEM_A,
                "why": work_verify.NOT_CONCRETE,
                "acceptance": {"kind": "pr_checks", "pr": "TBD"},
                "reported": {"pr": 77},
                "worker_session_key": f"chat-{ITEM_A}",
            }
        ],
    }


def test_a_document_of_the_callers_own_gets_no_row_facts(ledger, tmp_path):
    """The document form judges entries the caller wrote, which need not be board
    items, so nothing from the board is attached to its answers."""
    board, _reads = ledger
    board["items"][0].update(summary="all done", fails=3)
    entry = {"id": ITEM_A, "accept": {"kind": "file", "path": str(tmp_path / "present.txt")}}
    out = json.loads(_call("accept_eval", {"items": [entry]}))
    assert [sorted(r) for r in out["results"]] == [["evidence", "id", "verdict"]]


def test_a_reply_made_large_by_stored_bars_is_fitted_and_stays_whole_json(ledger):
    """A stored bar can be tens of thousands of characters. Four of them beside their
    answers are over what a tool result may carry, and a cut there would tear the
    JSON and lose the last item. The bars are left out and named; what the workers
    reported stays, because it still fits."""
    board, _reads = ledger
    ids = [f"it_0000001{n}" for n in range(4)]
    bar = {"kind": "pr_checks", "pr": "TBD", "note": "n" * 30_000}
    board["items"] = [_row(i, acceptance=bar, pr=7, summary="opened") for i in ids]
    board["accept_batch"] = {"items": []}
    text = _call("accept_eval", {})
    assert len(text) <= mcp_work._READ_BUDGET_CHARS
    out = json.loads(text)
    assert out["truncated"] is True and out["truncation_hint"] == mcp_work._ANSWERS_HINT
    assert out["elided_acceptance_for"] == ids
    assert "row_facts_omitted" not in out
    assert [entry["id"] for entry in out["skipped"]] == ids
    for entry in out["skipped"]:
        assert "acceptance" not in entry
        assert entry["why"] == work_verify.NOT_CONCRETE
        assert entry["reported"] == {"summary": "opened", "pr": 7}


def test_a_reply_made_large_by_many_items_keeps_every_answer(ledger, tmp_path):
    """A full board of items with long reports is over the budget with no stored bar
    in it. Everything carried beside the answers is left out and the reply says so;
    each answer's own id, verdict and evidence are all still there."""
    board, _reads = ledger
    path = str(tmp_path / "present.txt")
    ids = [f"it_{n:08x}" for n in range(256)]
    board["items"] = [
        _row(i, acceptance={"kind": "file", "path": path}, summary="s" * 500) for i in ids
    ]
    board["accept_batch"] = {
        "items": [
            {"id": i, "accept": {"kind": "file", "path": path}, "status": "done"} for i in ids
        ]
    }
    text = _call("accept_eval", {})
    assert len(text) <= mcp_work._READ_BUDGET_CHARS
    out = json.loads(text)
    assert out["truncated"] is True and out["row_facts_omitted"] is True
    assert "elided_acceptance_for" not in out, "no answer carried a stored bar"
    assert [(r["id"], r["verdict"]) for r in out["results"]] == [(i, "pass") for i in ids]
    for result in out["results"]:
        assert sorted(result) == ["evidence", "id", "verdict"]


def test_a_reply_too_large_even_without_row_facts_names_the_items_it_left_out(ledger, tmp_path):
    """A full board of failing items is over the budget on its evidence alone. The
    reply keeps as many whole answers as fit and lists the rest by id, so each one
    can be asked for by itself; nothing is cut in the middle."""
    board, _reads = ledger
    deep = tmp_path / ("d" * 200) / ("e" * 200)
    ids = [f"it_{n:08x}" for n in range(256)]
    bars = {i: {"kind": "file", "path": str(deep / f"{i}.txt")} for i in ids}
    board["items"] = [_row(i, acceptance=bars[i]) for i in ids]
    board["accept_batch"] = {"items": [{"id": i, "accept": bars[i], "status": "done"} for i in ids]}
    text = _call("accept_eval", {})
    assert len(text) <= mcp_work._READ_BUDGET_CHARS
    out = json.loads(text)
    assert out["truncated"] is True and out["row_facts_omitted"] is True
    kept = [result["id"] for result in out["results"]]
    assert kept and out["omitted"], "some answers fit and some did not"
    assert kept + out["omitted"] == ids
    for result in out["results"]:
        assert sorted(result) == ["evidence", "id", "verdict"]
        assert result["verdict"] == "fail"


def test_the_keys_a_fitted_reply_removes_are_the_ones_put_beside_an_answer():
    """The fit removes what ``row_facts`` and ``select_done`` add, by name. A fact
    added to one without the other would survive the fit and could tear the reply."""
    board = _board(
        _row(ITEM_A, summary="s", pr=1, fails=1, pending_overdue=True, decision="d"),
        _row(ITEM_B),
        not_concrete=(ITEM_B,),
    )
    added = set()
    for facts in work_verify.row_facts(board).values():
        added |= set(facts)
    _entries, skipped = work_verify.select_done(board)
    for entry in skipped:
        added |= set(entry) - {"id", "why"}
    assert added == set(work_verify.ROW_FACT_KEYS)


def test_every_text_that_skips_the_read_for_a_done_says_the_report_comes_back():
    """A wake for a ``done`` item sends the conductor to the evaluator with no read
    first. The texts that say so also say where the worker's report is, or the rule
    that reads that report has nothing to read."""
    assert "returns the worker's report beside the verdict" in ledger_wake.WAKE_READ_POINTER
    assert "returns the worker's report beside each verdict" in conductor_patrol.PATROL_MESSAGE
    described = next(t for t in mcp_work._list_tools() if t["name"] == "accept_eval")
    for field in (
        "'reported'",
        "'fails'",
        "'worker_session_key'",
        "'pending_overdue'",
        "'decision'",
        "'acceptance'",
    ):
        assert field in described["description"]


def test_a_worker_that_never_reports_leaves_nothing_to_evaluate(ledger):
    board, _reads = ledger
    board["items"] = [_row(ITEM_A, None)]
    board["accept_batch"] = {"items": [{"id": ITEM_A, "accept": {"kind": "file"}, "status": None}]}
    assert json.loads(_call("accept_eval", {})) == {"results": []}


def test_the_path_screen_guards_a_bar_read_from_the_ledger(ledger, monkeypatch):
    """A ``file`` bar that names a sensitive path is refused before anything stats it,
    exactly as when the same entry arrives in a document."""
    from kiro_crew import hooks

    monkeypatch.setattr(hooks, "validate_file_path", lambda _path: None)
    out = json.loads(_call("accept_eval", {"item_id": ITEM_A}))
    assert [(r["id"], r["verdict"]) for r in out["results"]] == [(ITEM_A, "refused")]


def test_a_document_is_still_evaluated_as_given_and_reads_only_for_admission(ledger, tmp_path):
    """The document form is unchanged: the caller's entries, and the compact read that
    only admits the call."""
    _board_doc, reads = ledger
    entry = {"id": "x", "accept": {"kind": "file", "path": str(tmp_path / "present.txt")}}
    out = json.loads(_call("accept_eval", {"items": [entry]}))
    assert reads == [("/api/work-ledger?compact=true", "chat-conductor")]
    assert [(r["id"], r["verdict"]) for r in out["results"]] == [("x", "pass")]


def test_an_item_id_beside_a_document_is_refused(ledger):
    """``item_id`` picks an item of the ledger. Beside a document it would be read as
    a filter the document form does not apply, so the call says so and runs nothing."""
    out = _call("accept_eval", {"items": [], "item_id": ITEM_A})
    assert out.startswith("Error: item_id picks one item of your own ledger")


def test_a_caller_with_no_ledger_is_refused_before_anything_is_evaluated(monkeypatch):
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    refusal = {"error": "this session holds no work ledger", "code": "no_ledger"}
    monkeypatch.setattr(mcp_work, "_get", lambda _path, **_kw: refusal)
    out = _call("accept_eval", {})
    assert out.startswith("Error: accept_eval runs only for a session that holds a work ledger")
    assert "[no_ledger]" in out


def test_accept_eval_still_takes_no_field_that_names_a_command():
    defs = {t["name"]: t for t in mcp_work._list_tools()}
    assert set(defs["accept_eval"]["inputSchema"]["properties"]) == {"items", "item_id"}
    assert "required" not in defs["accept_eval"]["inputSchema"]
    with pytest.raises(ValidationError):
        mcp_work._validate_args("accept_eval", {"item_id": "../../etc"})
    with pytest.raises(ValidationError):
        mcp_work._validate_args("accept_eval", {"argv": ["rm"]})


# ── a close that carries its verdict ─────────────────────────────────────────


@pytest.fixture
def writes(monkeypatch):
    """Every record write, as the route saw it; ``refuse`` maps an action to the
    refusal that write gets instead."""
    seen: list[tuple[str, dict[str, Any], str]] = []
    refuse: dict[str, dict[str, Any]] = {}

    def _post(path: str, payload: dict[str, Any], *, session_key: str) -> dict[str, Any]:
        refusal = refuse.get(payload["action"])
        if refusal is not None:
            return refusal
        seen.append((path, dict(payload), session_key))
        state = payload.get("state", "open")
        return {"action": payload["action"], "item": {"item_id": ITEM_A, "state": state}}

    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-conductor")
    monkeypatch.setattr(mcp_work, "_post", _post)
    return seen, refuse


def test_a_close_with_a_verdict_records_the_verdict_then_closes(writes):
    """The two writes the conductor made a model round trip apart, in their one valid
    order, each on the record route as the caller."""
    seen, _refuse = writes
    out = _call(
        "work_ledger_record",
        {"action": "close", "item_id": ITEM_A, "state": "accepted", "verdict": "pass"},
    )
    assert seen == [
        (
            "/api/work-ledger/record",
            {"action": "verdict", "item_id": ITEM_A, "verdict": "pass"},
            "chat-conductor",
        ),
        (
            "/api/work-ledger/record",
            {"action": "close", "item_id": ITEM_A, "state": "accepted"},
            "chat-conductor",
        ),
    ]
    assert out.startswith("Recorded close.")


def test_the_fail_count_travels_with_the_verdict_not_the_close(writes):
    seen, _refuse = writes
    _call(
        "work_ledger_record",
        {"action": "close", "item_id": ITEM_A, "state": "rejected", "verdict": "fail", "fails": 3},
    )
    assert [payload for _path, payload, _key in seen] == [
        {"action": "verdict", "item_id": ITEM_A, "verdict": "fail", "fails": 3},
        {"action": "close", "item_id": ITEM_A, "state": "rejected"},
    ]


def test_a_refused_verdict_leaves_the_item_open(writes):
    """An item must not be closed on an answer that did not land."""
    seen, refuse = writes
    refuse["verdict"] = {"error": "the ledger cache is dirty", "code": "cache_dirty"}
    out = _call(
        "work_ledger_record",
        {"action": "close", "item_id": ITEM_A, "state": "accepted", "verdict": "pass"},
    )
    assert seen == [], "no close followed the refused verdict"
    assert out.startswith("Error: could not record the verdict, so the item was not closed:")
    assert "[cache_dirty]" in out


def test_a_refused_close_says_the_verdict_already_landed(writes):
    """The retry after this refusal is the close alone. A reply that read like any
    refused write would have the caller send the pair again and record the verdict
    twice."""
    seen, refuse = writes
    refuse["close"] = {"error": f"item {ITEM_A!r} is accepted", "code": "item_closed"}
    out = _call(
        "work_ledger_record",
        {"action": "close", "item_id": ITEM_A, "state": "accepted", "verdict": "pass"},
    )
    assert [payload["action"] for _path, payload, _key in seen] == ["verdict"]
    assert out.startswith("Error: the verdict was recorded but the item was not closed:")
    assert "[item_closed]" in out


def test_a_refused_plain_close_keeps_its_plain_refusal(writes):
    _seen, refuse = writes
    refuse["close"] = {"error": f"item {ITEM_A!r} is accepted", "code": "item_closed"}
    out = _call("work_ledger_record", {"action": "close", "item_id": ITEM_A, "state": "accepted"})
    assert out.startswith("Error: could not write the work ledger:") and "[item_closed]" in out


def test_a_close_without_a_verdict_is_one_write_as_before(writes):
    seen, _refuse = writes
    _call("work_ledger_record", {"action": "close", "item_id": ITEM_A, "state": "abandoned"})
    assert [payload for _path, payload, _key in seen] == [
        {"action": "close", "item_id": ITEM_A, "state": "abandoned"}
    ]


def test_a_verdict_on_any_other_action_is_forwarded_untouched(writes):
    """Only ``close`` splits. A plain ``verdict`` write is the one call it always was."""
    seen, _refuse = writes
    _call("work_ledger_record", {"action": "verdict", "item_id": ITEM_A, "verdict": "pending"})
    assert [payload for _path, payload, _key in seen] == [
        {"action": "verdict", "item_id": ITEM_A, "verdict": "pending"}
    ]


# ── the shapes a conductor writes a bar from ─────────────────────────────────


def test_every_acceptance_argument_shows_the_condition_objects():
    """A conductor writes the bar from the tool descriptions alone. The record tool
    lists every kind the evaluator judges with the fields it reads, once, and the
    dispatch tool, which takes the same object, says where that list is."""
    from kiro_crew import work_ledger
    from kiro_crew.mcp_dashboard_session_tools import _session_tools
    from kiro_crew.work_vocab import ACCEPTANCE_SHAPES

    record = next(t for t in mcp_work._list_tools() if t["name"] == "work_ledger_record")
    dispatch = next(t for t in _session_tools() if t.name == "work_dispatch")
    assert ACCEPTANCE_SHAPES in record["inputSchema"]["properties"]["acceptance"]["description"]
    pointer = dispatch.schema["properties"]["acceptance"]["description"]
    assert "work_ledger_record action=create" in pointer
    assert "lists the object for each kind" in pointer
    for kind, fields in work_ledger.ACCEPTANCE_READ_FIELDS.items():
        if kind == "cmd":
            assert '"cmd"' not in ACCEPTANCE_SHAPES, "the evaluator refuses it"
            continue
        assert f'"kind": "{kind}"' in ACCEPTANCE_SHAPES
        for field in fields:
            assert f'"{field}"' in ACCEPTANCE_SHAPES
