"""Conductor work ledger store — Phase 1 exit criteria, one test per criterion.

Pins what the conductor-work-ledger RFC §Migration plan
Phase 1 lists: every enum and cap fails a test if its value changes; two concurrent
writers against one item leave a parseable record and an uninterleaved event log; a
torn, truncated or oversized file reads as absent; a refused cap leaves the prior
bytes untouched; ``depth`` at the cap refuses ``create``; consecutive ``progress``
reports coalesce; a duplicated event line collapses on read; and only the Phase 2
routes module imports the store (an allowlist that was an empty set while Phase 1
stood alone, so that phase reverted by deleting two files).

The store's other criteria live in sibling files, split by behaviour: the caps and
input bounds in ``test_work_ledger_caps.py``, corruption and transient read errors in
``test_work_ledger_corruption.py``, concurrent writers, the lock files and the undo in
``test_work_ledger_concurrency.py``, and the maintenance purge in
``test_work_ledger_purge.py``. Their shared fixtures are in ``work_ledger_helpers.py``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
import work_ledger_helpers
from work_ledger_helpers import CONDUCTOR, WORKER, _bytes_on_disk, _new_item

from kiro_crew import work_ledger as wl
from kiro_crew.work_vocab import WORK_CLOSED_WINDOW, worker_statuses

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated_home = work_ledger_helpers._isolated_home


# ── vocabularies and caps, pinned ─────────────────────────────────────────


def test_item_states_are_exactly_the_four_dispositions():
    assert wl.ITEM_STATES == {"open", "accepted", "rejected", "abandoned"}
    assert wl.TERMINAL_ITEM_STATES == {"accepted", "rejected", "abandoned"}
    assert "open" not in wl.TERMINAL_ITEM_STATES


def test_verdicts_are_accept_evals_five_values():
    assert wl.VERDICTS == {"pass", "fail", "pending", "refused", "error"}


def test_worker_statuses_are_exactly_four_and_separate_blocked_from_question():
    """Four a report may be written with; ``reply`` is stored only under the preview."""
    assert wl.WORKER_STATUSES == {"progress", "done", "blocked", "question", "reply"}
    assert set(worker_statuses(False)) == {"progress", "done", "blocked", "question"}


def test_event_kinds_are_exactly_seven():
    assert wl.EVENT_KINDS == {
        "create",
        "bind",
        "report",
        "decision",
        "verdict",
        "close",
        "steer",
    }


def test_conductor_actions_are_exactly_six():
    assert wl.CONDUCTOR_ACTIONS == {
        "create",
        "bind",
        "decide",
        "verdict",
        "close",
        "goal",
    }


def test_caps_hold_their_rfc_values():
    assert wl.MAX_ITEMS_PER_CONDUCTOR == 32
    assert WORK_CLOSED_WINDOW == 32
    assert wl.MAX_GOAL_ITEM_CAP == 256
    assert wl.MAX_EVENTS_PER_ITEM == 200
    assert wl.MAX_DEPTH == 2
    assert wl.MAX_GOAL_CHARS == 2000
    assert wl.MAX_TITLE_CHARS == 200
    assert wl.MAX_DECISION_CHARS == 2000
    assert wl.MAX_SUMMARY_CHARS == 500
    assert wl.MAX_EVENT_TEXT_CHARS == 500
    assert wl.MAX_ARTIFACT_KEYS == 16
    assert wl.MAX_ARTIFACT_KEY_CHARS == 64
    assert wl.MAX_ARTIFACT_VALUE_CHARS == 512
    assert (wl.MIN_PR, wl.MAX_PR) == (1, 1_000_000_000)
    assert wl.SCHEMA_VERSION == 1


def test_the_fold_holds_far_less_than_its_ceiling():
    """Open fan-out plus the closed window sits far below the fold's held-item
    ceiling, so a board the store admits never reaches it."""
    from kiro_crew.crew_log import projection

    assert wl.MAX_ITEMS_PER_CONDUCTOR + WORK_CLOSED_WINDOW < projection.WORK_ITEM_LIMIT


# ── paths and ids ─────────────────────────────────────────────────────────


def test_item_id_is_server_minted_and_shape_checked():
    minted = wl.mint_item_id()
    assert wl._ITEM_ID_RE.match(minted)
    assert wl.mint_item_id() != minted


@pytest.mark.parametrize(
    "bad",
    ["", "it_", "it_XYZ", "it_1a2b3c4", "../etc/passwd", "/abs/it_1a2b3c4d", "it_1a2b3c4d.json"],
)
def test_a_model_supplied_string_cannot_reach_a_path_component(bad):
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.item_path(CONDUCTOR, bad)
    assert caught.value.code == wl.CODE_INVALID_VALUE


def test_directory_name_is_session_ledgers_fold_not_a_copy():
    from kiro_crew import session_ledger

    assert wl._store_name is session_ledger._store_name
    assert not hasattr(wl, "_STORE_NAME_UNSAFE")


def test_directory_name_is_readable_plus_digest_and_distinguishes_case():
    name = wl._store_name("chat-1-abc")
    assert name.startswith("chat-1-abc-")
    assert len(name.rsplit("-", 1)[1]) == 8
    assert wl._store_name("Chat") != wl._store_name("chat")
    assert "/" not in wl._store_name("slack:C1/x")


@pytest.mark.parametrize("bad", ["", "a/b", "a\\b", "a\0b"])
def test_conductor_dir_refuses_a_key_that_could_escape_the_root(bad):
    with pytest.raises(wl.WorkLedgerError):
        wl.conductor_dir(bad)


def test_binding_path_shares_the_conductor_naming_scheme():
    assert wl.binding_path(WORKER).name == f"{wl._store_name(WORKER)}.json"
    assert wl.binding_path(WORKER).parent == wl.bindings_dir()
    with pytest.raises(wl.WorkLedgerError):
        wl.binding_path("has\0null")


# ── conductor record ──────────────────────────────────────────────────────


def test_ensure_conductor_is_idempotent_and_never_resets_progress():
    first = wl.ensure_conductor(CONDUCTOR, goal="ship it")
    wl.apply_conductor_action(CONDUCTOR, "goal", goal="ship it", round_number=4)
    again = wl.ensure_conductor(CONDUCTOR, goal="something else")
    assert again.round == 4
    assert again.goal == "ship it"
    assert again.created_at == first.created_at
    assert (wl.conductor_dir(CONDUCTOR) / "slot_key").read_text().strip() == CONDUCTOR


def test_read_conductor_is_none_before_anything_is_written():
    assert wl.read_conductor(CONDUCTOR) is None
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "create", title="x", acceptance={})
    assert caught.value.code == wl.CODE_NO_LEDGER


def test_conductor_record_has_no_item_roster_field():
    wl.ensure_conductor(CONDUCTOR)
    stored = json.loads((wl.conductor_dir(CONDUCTOR) / "conductor.json").read_text())
    assert "items" not in stored
    assert "item_roster" not in stored


def test_goal_action_leaves_the_goal_alone_when_only_the_round_moves():
    wl.ensure_conductor(CONDUCTOR, goal="keep me")
    record = wl.apply_conductor_action(CONDUCTOR, "goal", round_number=2)["conductor"]
    assert (record.goal, record.round) == ("keep me", 2)


def test_goal_and_round_partial_updates_merge_under_the_lock(monkeypatch):
    """A goal-only write racing a round-only write must not restore the other's
    stale value: omitted fields come from the record read inside the lock."""
    wl.ensure_conductor(CONDUCTOR, goal="old")
    real_lock = wl.conductor_lock
    interleaved: list[str] = []

    from contextlib import contextmanager

    @contextmanager
    def racing_lock(slot_key, **kwargs):
        # First entrant: while it waits, another writer moves the round to 9.
        if not interleaved:
            interleaved.append("x")
            monkeypatch.setattr(wl, "conductor_lock", real_lock)
            wl.apply_conductor_action(CONDUCTOR, "goal", round_number=9)
        with real_lock(slot_key, **kwargs):
            yield

    monkeypatch.setattr(wl, "conductor_lock", racing_lock)
    record = wl.apply_conductor_action(CONDUCTOR, "goal", goal="new")["conductor"]
    assert (record.goal, record.round) == ("new", 9)


def test_ensure_conductor_refuses_a_depth_past_the_cap():
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.ensure_conductor("deep", depth=3)
    assert caught.value.code == wl.CODE_DEPTH_EXCEEDED


def test_ensure_conductor_refuses_the_first_depth_that_may_not_conduct():
    """A ledger at ``MAX_DEPTH`` could create nothing, so it must not open at all."""
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.ensure_conductor("sterile", goal="g", depth=wl.MAX_DEPTH)
    assert caught.value.code == wl.CODE_DEPTH_EXCEEDED
    assert wl.read_conductor("sterile") is None


def test_ensure_conductor_records_a_parent_item_and_checks_its_shape():
    parent = wl.mint_item_id()
    record = wl.ensure_conductor("child", depth=1, parent_item=parent)
    assert (record.depth, record.parent_item) == (1, parent)
    with pytest.raises(wl.WorkLedgerError):
        wl.ensure_conductor("child2", depth=1, parent_item="not-an-id")


# ── depth ─────────────────────────────────────────────────────────────────


def test_child_depth_advances_one_level_and_refuses_the_last_conducting_level():
    """A root may dispatch a conductor; that conductor's children may only work.

    The refusal lands on the PARENT's side of the dispatch: ``child_depth(1)`` is
    what a depth-1 conductor's child would be born at, and refusing there is what
    keeps a ledger from opening at a depth where every create is refused.
    """
    assert wl.child_depth(0) == 1
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.child_depth(1)
    assert caught.value.code == wl.CODE_DEPTH_EXCEEDED
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.child_depth(2)
    assert caught.value.code == wl.CODE_DEPTH_EXCEEDED


def _header_at_depth(slot_key: str, depth: int) -> None:
    """Put a ledger on disk whose stored depth no entry guard admitted.

    ``ensure_conductor`` refuses such a depth, so the header is rewritten after a
    legal open -- the shape a rebuild from the crew log can leave, since it keeps
    the depth the stored header holds.
    """
    wl.ensure_conductor(slot_key, goal="g", depth=0)
    header_path = wl.conductor_dir(slot_key) / "conductor.json"
    stored = json.loads(header_path.read_text(encoding="utf-8"))
    stored["depth"] = depth
    header_path.write_text(json.dumps(stored), encoding="utf-8")
    assert wl.read_conductor(slot_key).depth == depth


def test_create_is_refused_when_a_stored_header_sits_at_a_depth_that_may_not_conduct():
    _header_at_depth("capped", wl.MAX_DEPTH)
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action("capped", "create", title="t", acceptance={})
    assert caught.value.code == wl.CODE_DEPTH_EXCEEDED
    assert wl.list_work_items("capped") == []


@pytest.mark.parametrize("depth", range(0, 4))
def test_the_three_depth_guards_agree_at_every_depth(depth: int):
    """One predicate, three guards: a depth a dispatch admits is a depth a ledger
    opens at is a depth that may create. No depth is let in by one and refused by
    another."""
    allowed = wl.may_conduct(depth)
    assert allowed == (depth < wl.MAX_DEPTH)

    def refused(call) -> bool:
        try:
            call()
        except wl.WorkLedgerError as exc:
            assert exc.code == wl.CODE_DEPTH_EXCEEDED
            return True
        return False

    def dispatch() -> None:
        wl.child_depth(depth - 1)

    def open_ledger() -> None:
        wl.ensure_conductor(f"open-{depth}", goal="g", depth=depth)

    slot = f"stored-{depth}"

    def create() -> None:
        wl.apply_conductor_action(slot, "create", title="t", acceptance={})

    # Guard 1: the dispatch that would create a conductor at *depth*.
    if depth > 0:
        assert refused(dispatch) == (not allowed)
    # Guard 2: opening a ledger at *depth*.
    assert refused(open_ledger) == (not allowed)
    # Guard 3: creating an item from a header stored at *depth*.
    _header_at_depth(slot, depth)
    assert refused(create) == (not allowed)


def test_a_conductor_one_below_the_cap_may_still_dispatch():
    wl.ensure_conductor("mid", goal="g", depth=1)
    result = wl.apply_conductor_action("mid", "create", title="t", acceptance={})
    assert result["item"].item_id.startswith("it_")


# ── item lifecycle ────────────────────────────────────────────────────────


def test_create_mints_an_item_and_appends_exactly_one_create_event():
    item_id = _new_item()
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    assert (item.state, item.status, item.verdict) == ("open", None, None)
    events = wl.read_events(CONDUCTOR, item_id)
    assert [e.kind for e in events] == ["create"]


def test_items_are_derived_from_the_directory_and_sorted_oldest_first():
    first = _new_item(title="one")
    second = _new_item(title="two")
    listed = [item.item_id for item in wl.list_work_items(CONDUCTOR)]
    assert set(listed) == {first, second}
    assert wl.list_work_items("never-existed") == []


def test_bind_writes_the_binding_and_refuses_a_second_one():
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_id, worker_session_key=WORKER)
    assert wl.read_binding(WORKER) == (CONDUCTOR, item_id)
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_id, worker_session_key="chat-3")
    assert caught.value.code == wl.CODE_ALREADY_BOUND


def test_a_worker_with_an_open_item_cannot_be_bound_to_a_second_one():
    """GPT F2: a worker holds ONE binding file, so a second bind would silently
    repoint its only report channel and strand the first item forever."""
    first = _new_item(title="first")
    second = _new_item(title="second")
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=first, worker_session_key=WORKER)
    before = (_bytes_on_disk(second), wl.binding_path(WORKER).read_bytes())
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "bind", item_id=second, worker_session_key=WORKER)
    assert caught.value.code == wl.CODE_ALREADY_BOUND
    assert first in str(caught.value)
    # Nothing moved: the binding still names the first item, the second is unbound.
    assert wl.read_binding(WORKER) == (CONDUCTOR, first)
    assert (_bytes_on_disk(second), wl.binding_path(WORKER).read_bytes()) == before
    second_item = wl.read_work_item(CONDUCTOR, second)
    assert second_item is not None and second_item.worker_session_key is None


def test_a_worker_may_be_rebound_once_its_prior_item_is_terminal():
    first = _new_item(title="first")
    second = _new_item(title="second")
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=first, worker_session_key=WORKER)
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=first, state="accepted")
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=second, worker_session_key=WORKER)
    assert wl.read_binding(WORKER) == (CONDUCTOR, second)


def test_a_worker_may_be_rebound_when_its_prior_binding_is_stale():
    """A binding pointing at an item that does not read is stale, not open."""
    first = _new_item(title="first")
    second = _new_item(title="second")
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=first, worker_session_key=WORKER)
    wl.item_path(CONDUCTOR, first).unlink()
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=second, worker_session_key=WORKER)
    assert wl.read_binding(WORKER) == (CONDUCTOR, second)


def test_a_worker_bound_by_another_conductor_is_refused_too():
    other = "chat-9-other-conductor"
    mine = _new_item(title="mine")
    wl.ensure_conductor(other, goal="g")
    theirs = wl.apply_conductor_action(other, "create", title="theirs", acceptance={})[
        "item"
    ].item_id
    wl.apply_conductor_action(other, "bind", item_id=theirs, worker_session_key=WORKER)
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "bind", item_id=mine, worker_session_key=WORKER)
    assert caught.value.code == wl.CODE_ALREADY_BOUND
    assert wl.read_binding(WORKER) == (other, theirs)


def test_a_failed_item_write_during_bind_restores_the_prior_binding(monkeypatch):
    """A bind that fails between its two writes must leave neither a
    half-bound item (which would refuse every retry) nor a dangling new binding."""
    first = _new_item(title="first")
    second = _new_item(title="second")
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=first, worker_session_key=WORKER)
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=first, state="accepted")
    binding_before = wl.binding_path(WORKER).read_bytes()
    item_before = wl.item_path(CONDUCTOR, second).read_bytes()
    real_write = wl._write_record

    def boom_on_item(path, payload):
        if path.name == f"{second}.json":
            raise OSError("disk full")
        real_write(path, payload)

    monkeypatch.setattr(wl, "_write_record", boom_on_item)
    with pytest.raises(OSError):
        wl.apply_conductor_action(CONDUCTOR, "bind", item_id=second, worker_session_key=WORKER)
    monkeypatch.setattr(wl, "_write_record", real_write)
    assert wl.binding_path(WORKER).read_bytes() == binding_before
    assert wl.item_path(CONDUCTOR, second).read_bytes() == item_before
    # And the retry succeeds -- the item was never half-bound.
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=second, worker_session_key=WORKER)
    assert wl.read_binding(WORKER) == (CONDUCTOR, second)


def test_a_failed_first_bind_leaves_no_binding_file(monkeypatch):
    item_id = _new_item()
    real_write = wl._write_record

    def boom_on_item(path, payload):
        if path.name == f"{item_id}.json":
            raise OSError("disk full")
        real_write(path, payload)

    monkeypatch.setattr(wl, "_write_record", boom_on_item)
    with pytest.raises(OSError):
        wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_id, worker_session_key=WORKER)
    assert not wl.binding_path(WORKER).exists()
    assert wl.read_binding(WORKER) is None


def test_read_binding_is_none_when_absent_or_malformed():
    assert wl.read_binding(WORKER) is None
    path = wl.binding_path(WORKER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"conductor_slot_key": CONDUCTOR, "item_id": "../x"}))
    assert wl.read_binding(WORKER) is None
    path.write_text("not json")
    assert wl.read_binding(WORKER) is None


def test_decide_verdict_and_close_each_move_one_field_and_log_one_event():
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="retry once")
    wl.apply_conductor_action(CONDUCTOR, "verdict", item_id=item_id, verdict="fail", fails=1)
    mid = wl.read_work_item(CONDUCTOR, item_id)
    assert mid is not None
    assert (mid.decision, mid.verdict, mid.fails, mid.state) == ("retry once", "fail", 1, "open")
    wl.apply_conductor_action(
        CONDUCTOR, "close", item_id=item_id, state="accepted", decision="landed"
    )
    closed = wl.read_work_item(CONDUCTOR, item_id)
    assert closed is not None
    assert (closed.state, closed.decision) == ("accepted", "landed")
    assert closed.closed_at
    assert [e.kind for e in wl.read_events(CONDUCTOR, item_id)] == [
        "create",
        "decision",
        "verdict",
        "close",
    ]


def test_a_terminal_item_refuses_every_further_write():
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="abandoned")
    for call in (
        lambda: wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="again"),
        lambda: wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="still here"),
    ):
        with pytest.raises(wl.WorkLedgerError) as caught:
            call()
        assert caught.value.code == wl.CODE_ITEM_CLOSED


def test_close_refuses_open_as_a_terminal_state():
    item_id = _new_item()
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="open")
    assert caught.value.field == "state"


def test_an_unknown_item_and_an_unknown_action_are_named_distinctly():
    wl.ensure_conductor(CONDUCTOR)
    with pytest.raises(wl.WorkLedgerError) as unknown:
        wl.apply_conductor_action(CONDUCTOR, "decide", item_id=wl.mint_item_id(), decision="x")
    assert unknown.value.code == wl.CODE_UNKNOWN_ITEM
    with pytest.raises(wl.WorkLedgerError) as action:
        wl.apply_conductor_action(CONDUCTOR, "teleport")
    assert action.value.code == wl.CODE_INVALID_ACTION


def test_round_rides_along_with_a_conductor_action():
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="d", round_number=7)
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.round == 7


# ── writer ownership ──────────────────────────────────────────────────────


def test_the_worker_path_writes_only_worker_fields():
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="conductor said")
    wl.apply_worker_report(
        CONDUCTOR,
        item_id,
        status="done",
        summary="tests pass",
        artifacts={"pr": "8842"},
        pr=8842,
    )
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    assert (item.status, item.summary, item.pr) == ("done", "tests pass", 8842)
    assert item.artifacts == {"pr": "8842"}
    assert item.last_report_at
    # Untouched by the worker: the conductor still owns the bar and the disposition.
    assert (item.decision, item.state, item.verdict) == ("conductor said", "open", None)


def test_apply_worker_report_has_no_conductor_field_parameter():
    import inspect

    names = set(inspect.signature(wl.apply_worker_report).parameters)
    assert names == {"slot_key", "item_id", "status", "summary", "artifacts", "pr", "reason"}
    assert not names & {"verdict", "state", "acceptance", "decision", "fails", "round_number"}


def _ledger_conductor_accept_eval() -> Path:
    """The evaluator copy the conductor actually runs.

    ``goal-conductor`` is the skill that consumes ``accept_batch``, so the mirror in
    :func:`work_ledger.is_acceptance_concrete` is pinned against ITS copy. The
    deprecated ``goal-ledger-conductor`` ships a byte-identical copy for one release
    (held so by ``test_ledger_conductor_agent.py``), and this helper names the live
    consumer rather than that one.
    """
    script = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "kiro_crew"
        / "builtin_skills"
        / "goal-conductor"
        / "scripts"
        / "accept_eval.py"
    )
    assert script.is_file(), script
    return script


def _claimed_pr_reaches_a_bar(batch: dict, claimed: int) -> bool:
    """Whether *claimed* appears anywhere in any entry's ``accept`` bar.

    Scoped at the bar, NOT at the whole serialized batch. ``accept_batch`` composes an
    entry from three fields — ``item_id``, ``acceptance`` and ``status`` — and only
    ``acceptance`` is one a worker's report can contaminate, so the bar is the only
    place a leaked claim can land. ``item_id`` is ``it_`` plus eight HEX characters,
    whose digits spell any decimal sentinel a test plants about one id in 739, so a
    whole-document substring check answers yes on an ordinary id. Scoping it also names
    WHICH field leaked when one really does.
    """
    return any(str(claimed) in json.dumps(entry["accept"]) for entry in batch["items"])


def test_accept_batch_is_built_from_acceptance_and_never_from_a_claimed_pr():
    """The bar in the batch is the stored one, whatever number the worker claims."""
    bar = {"kind": "pr_checks", "pr": 123, "repo": "owner/name"}
    item_id = _new_item(acceptance=dict(bar))
    wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="s", pr=999)
    batch = wl.accept_batch(wl.list_work_items(CONDUCTOR))
    assert batch == {"items": [{"id": item_id, "accept": bar, "status": "done"}]}
    assert not _claimed_pr_reaches_a_bar(batch, 999)


def test_a_minted_id_that_spells_the_claimed_pr_is_not_a_leak():
    """``it_3bcc999e`` is an ordinary id, and the check above must not read it as a leak.

    ``mint_item_id`` returns ``it_`` + ``secrets.token_hex(4)``, whose alphabet includes
    ``9``, so about one minted id in 739 carries ``999`` — the same digits the test above
    plants as a worker's claim. Forcing such an id checks the distinction on every run
    rather than leaving it to the mint.

    Both halves are asserted on purpose. The first pins that the whole serialized
    document does carry the digits, so a check scoped there cannot tell an id from a
    leak; the second pins that the bar-scoped check answers no. Widening
    :func:`_claimed_pr_reaches_a_bar` to the whole document turns the second assertion
    red here rather than once in 739 runs somewhere else.
    """
    token = "3bcc999e"
    assert set(token) <= set("0123456789abcdef"), "the forced token is token_hex-legal"

    # One forced id, then the real minter: the create path re-mints on a collision, so
    # a constant would spin if this id were ever already on disk.
    forced = [f"it_{token}"]
    real_mint = wl.mint_item_id
    bar = {"kind": "pr_checks", "pr": 123, "repo": "owner/name"}
    with mock.patch.object(
        wl, "mint_item_id", side_effect=lambda: forced.pop() if forced else real_mint()
    ):
        item_id = _new_item(acceptance=dict(bar))
    assert item_id == f"it_{token}"
    assert wl._ITEM_ID_RE.match(item_id), "the forced id is a legal minted id"

    wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="s", pr=999)
    batch = wl.accept_batch(wl.list_work_items(CONDUCTOR))

    assert "999" in json.dumps(batch), "the id puts the digits in the document"
    assert not _claimed_pr_reaches_a_bar(batch, 999)
    assert batch["items"][0]["accept"] == bar


def test_accept_batch_leaves_out_an_item_whose_bar_is_not_concrete_yet():
    """A ``"TBD"`` pull request number is not a bar ``accept_eval.py`` can evaluate —
    it answers ``error`` — so the item stays out until an ``accept`` promotion fills
    the number in, which is the two-phase acceptance the skill promises."""
    pending = _new_item(acceptance={"kind": "pr_checks", "pr": "TBD", "repo": "owner/name"})
    lowercase = _new_item(acceptance={"kind": "pr_checks", "pr": "tbd", "repo": "owner/name"})
    blank = _new_item(acceptance={"kind": "file", "path": ""})
    unrepoed = _new_item(acceptance={"kind": "pr_checks", "pr": 9, "repo": "TBD"})
    unnumbered = _new_item(acceptance={"kind": "pr_checks", "repo": "owner/name"})
    mistyped = _new_item(acceptance={"kind": "file", "path": 17})
    inverted = _new_item(acceptance={"kind": "file", "path": "/p", "exists": "false"})
    unknown = _new_item(acceptance={"kind": "tests_pass", "suite": "backend"})
    ready = _new_item(acceptance={"kind": "pr_checks", "pr": 7, "repo": "owner/name"})
    # Concrete: the placeholder sits in a field the evaluator never reads, so it cannot
    # affect the verdict and must not cost the item its place in the batch.
    annotated = _new_item(acceptance={"kind": "file", "path": "/p", "meta": {"br": "TBD"}})
    ids = [entry["id"] for entry in wl.accept_batch(wl.list_work_items(CONDUCTOR))["items"]]
    # A set: ``list_work_items`` does not promise creation order, and this test is about
    # membership, not sequence.
    assert set(ids) == {ready, annotated}
    for absent in (pending, lowercase, blank, unrepoed, unnumbered, mistyped, inverted, unknown):
        assert absent not in ids

    # And the promotion puts it back, which is what makes the omission temporary
    # rather than a way to lose an item.
    wl.apply_acceptance_update(
        CONDUCTOR, pending, acceptance={"kind": "pr_checks", "pr": 4321, "repo": "owner/name"}
    )
    promoted = [entry["id"] for entry in wl.accept_batch(wl.list_work_items(CONDUCTOR))["items"]]
    assert pending in promoted


def test_a_non_positive_or_boolean_pr_is_not_a_concrete_bar():
    """``accept_eval.py`` refuses a bool as an int and cannot check pull request 0, so
    neither counts as filled in."""
    assert wl.is_acceptance_concrete({"kind": "pr_checks", "pr": 1}) is True
    assert wl.is_acceptance_concrete({"kind": "pr_checks", "pr": 0}) is False
    assert wl.is_acceptance_concrete({"kind": "pr_checks", "pr": -3}) is False
    assert wl.is_acceptance_concrete({"kind": "pr_checks", "pr": True}) is False
    assert wl.is_acceptance_concrete({"kind": "pr_checks", "pr": "12"}) is False
    assert wl.is_acceptance_concrete({}) is False
    # The pr rule is ``pr_checks``-specific; another kind is judged on its own fields.
    assert wl.is_acceptance_concrete({"kind": "human_approval"}) is True
    assert wl.is_acceptance_concrete({"kind": "file", "path": "/p", "exists": None}) is False


def test_a_mistyped_or_unknown_kind_is_not_a_concrete_bar():
    """The other guards ``accept_eval.py`` can only answer ``error`` to, mirrored: a
    ``file`` whose ``path`` is not a string or whose ``exists`` is not a bool, and any
    ``kind`` that script does not dispatch on at all."""
    assert wl.is_acceptance_concrete({"kind": "file", "path": "/p"}) is True
    assert wl.is_acceptance_concrete({"kind": "file", "path": "/p", "exists": False}) is True
    assert wl.is_acceptance_concrete({"kind": "file", "path": 17}) is False
    assert wl.is_acceptance_concrete({"kind": "file"}) is False
    # ``1``/``0`` are refused as ``exists`` there too, and coercing a truthy ``"false"``
    # would invert an absence check into a presence check.
    assert wl.is_acceptance_concrete({"kind": "file", "path": "/p", "exists": 1}) is False
    assert wl.is_acceptance_concrete({"kind": "file", "path": "/p", "exists": "false"}) is False
    assert wl.is_acceptance_concrete({"kind": "tests_pass", "suite": "backend"}) is False
    assert wl.is_acceptance_concrete({"pr": 7, "repo": "owner/name"}) is False
    assert wl.ACCEPTANCE_KINDS == frozenset(wl.ACCEPTANCE_READ_FIELDS)
    # ``cmd`` stays concrete on purpose: that script always REFUSES it, and ``refused``
    # is a message the conductor must receive (re-express the condition) rather than an
    # item silently missing from its batch.
    assert wl.is_acceptance_concrete({"kind": "cmd", "argv": ["git", "status"]}) is True
    assert "cmd" in wl.ACCEPTANCE_KINDS


def test_only_the_fields_the_evaluator_reads_can_cost_an_item_its_place():
    """The judgement is field-by-field over what ``accept_eval.py`` consumes, never a
    walk of the stored object. An acceptance carries whatever the conductor wrote — a
    branch name, a note, a ``cmd`` argv that mentions the word TBD — and a placeholder
    in a field the evaluator never reads cannot change its verdict, so it must not drop
    the item from the batch."""
    assert wl.is_acceptance_concrete({"kind": "file", "path": "/p", "note": "TBD"}) is True
    assert wl.is_acceptance_concrete({"kind": "file", "path": "/p", "m": {"b": "TBD"}}) is True
    assert wl.is_acceptance_concrete({"kind": "cmd", "argv": ["grep", "TBD", "-r"]}) is True
    # ...while a placeholder in a field it DOES read still costs the item its place.
    assert wl.is_acceptance_concrete({"kind": "file", "path": "TBD"}) is False
    assert wl.is_acceptance_concrete({"kind": "pr_checks", "pr": 9, "repo": "TBD"}) is False
    # An absent optional read field is not a placeholder: the evaluator defaults both.
    assert wl.is_acceptance_concrete({"kind": "pr_checks", "pr": 9}) is True
    assert wl.is_acceptance_concrete({"kind": "file", "path": "/p"}) is True
    # Nor is an explicit null in one: the evaluator omits ``--repo`` for a falsy repo,
    # so such a bar is evaluable and must keep its place. Where the field is REQUIRED,
    # the type rules refuse ``None`` — that is where the judgement belongs.
    assert wl.is_acceptance_concrete({"kind": "pr_checks", "pr": 9, "repo": None}) is True
    assert wl.is_acceptance_concrete({"kind": "pr_checks", "pr": None}) is False
    assert wl.is_acceptance_concrete({"kind": "file", "path": None}) is False


def test_a_deeply_nested_acceptance_does_not_break_the_read():
    """A bar is stored verbatim and its nesting is caller-supplied, so a read-path walk
    over it was a recursion an untrusted depth could exhaust — and one stored record
    would then fail every later batch read of that slot, not just its own item."""
    deep: dict[str, object] = {"leaf": "TBD"}
    for _ in range(500):
        deep = {"nested": deep}
    bar = {"kind": "file", "path": "/p", "meta": deep}
    assert wl.is_acceptance_concrete(bar) is True
    item_id = _new_item(acceptance=bar)
    batch = wl.accept_batch(wl.list_work_items(CONDUCTOR))
    assert [entry["id"] for entry in batch["items"]] == [item_id]


def test_the_kind_vocabulary_is_derived_from_accept_eval_not_remembered():
    """``accept_eval.py`` dispatches on an inline ``if kind == "..."`` chain, so a kind
    added there would otherwise make every item using it vanish from every batch under
    a misleading "not filled in yet". Read the chain and require agreement, so drift
    fails here instead of silently dropping work items."""
    source = _ledger_conductor_accept_eval().read_text(encoding="utf-8")
    dispatched = set(re.findall(r'kind == "([a-z_]+)"', source))
    assert dispatched, "the dispatch chain could not be read — the pattern moved"
    assert dispatched == set(wl.ACCEPTANCE_READ_FIELDS), (dispatched, wl.ACCEPTANCE_KINDS)


def test_the_concreteness_rules_are_exactly_accept_evals_error_only_guards():
    """The predicate duplicates that script's guards across a process boundary, so pin
    the two against each other rather than against a remembered reading of it: every
    spec this store calls non-concrete must come back ``error``, and every spec it
    passes must come back something else.

    Only specs that evaluate WITHOUT network are used — a valid ``pr_checks`` would
    shell out to ``gh``, so it is asserted concrete here and evaluated nowhere.
    """
    import subprocess
    import sys

    script = _ledger_conductor_accept_eval()
    specs = [
        {"kind": "pr_checks", "pr": "TBD", "repo": "owner/name"},
        {"kind": "pr_checks", "repo": "owner/name"},
        {"kind": "pr_checks", "pr": True, "repo": "owner/name"},
        {"kind": "file", "path": 17},
        {"kind": "file"},
        {"kind": "file", "path": "/nowhere", "exists": 1},
        {"kind": "tests_pass", "suite": "backend"},
        {"kind": "file", "path": "/nowhere-at-all", "exists": False},
        {"kind": "human_approval"},
        {"kind": "cmd", "argv": ["git", "status"]},
        {"kind": "pr_merged", "pr": "TBD", "repo": "owner/name"},
        {"kind": "pr_merged", "pr": 0, "repo": "owner/name"},
        {"kind": "pr_merged", "pr": True, "repo": "owner/name"},
        {"kind": "pr_merged", "pr": 7},
        {"kind": "pr_merged", "pr": 7, "repo": "TBD"},
        {"kind": "pr_merged", "pr": 7, "repo": "-owner/name"},
    ]
    batch = {"items": [{"id": f"it_{n:08d}", "accept": spec} for n, spec in enumerate(specs)]}
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(batch),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    verdicts = [row["verdict"] for row in json.loads(proc.stdout)["results"]]
    assert len(verdicts) == len(specs)
    for spec, verdict in zip(specs, verdicts):
        concrete = wl.is_acceptance_concrete(spec)
        assert concrete is (verdict != "error"), (spec, verdict, concrete)
    assert wl.is_acceptance_concrete({"kind": "pr_checks", "pr": 7, "repo": "owner/name"}) is True
    # ``pr_merged`` shells out too, so its valid form is asserted concrete and
    # evaluated nowhere, as for ``pr_checks`` above.
    assert wl.is_acceptance_concrete({"kind": "pr_merged", "pr": 7, "repo": "owner/name"}) is True


def test_accept_batch_carries_status_and_does_not_filter_on_it():
    """The "only ``done`` items" filter is the conductor's to apply — the batch makes
    it applyable without a second lookup, and applies nothing itself."""
    moving = _new_item(acceptance={"kind": "human_approval"})
    finished = _new_item(acceptance={"kind": "human_approval"})
    silent = _new_item(acceptance={"kind": "human_approval"})
    wl.apply_worker_report(CONDUCTOR, moving, status="progress", summary="s")
    wl.apply_worker_report(CONDUCTOR, finished, status="done", summary="s")
    by_id = {e["id"]: e for e in wl.accept_batch(wl.list_work_items(CONDUCTOR))["items"]}
    assert by_id[moving]["status"] == "progress"
    assert by_id[finished]["status"] == "done"
    assert by_id[silent]["status"] is None


def test_a_done_item_is_never_stale_however_long_it_stays_quiet():
    """``stale`` means the WORKER went quiet. After ``done`` the move belongs to the
    conductor or a human, so silence is the expected end of the work."""
    item_id = _new_item(acceptance={"kind": "human_approval"})
    wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="green")
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    much_later = datetime.now().astimezone() + timedelta(seconds=wl.DEFAULT_STALE_WINDOW_SECS * 10)
    assert wl.is_stale(item, worker_running=False, now=much_later) is False
    wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="s")
    still_working = wl.read_work_item(CONDUCTOR, item_id)
    assert still_working is not None
    assert wl.is_stale(still_working, worker_running=False, now=much_later) is True
    assert "done" not in wl.STALE_ELIGIBLE_STATUSES
    assert wl.STALE_ELIGIBLE_STATUSES < wl.WORKER_STATUSES


@pytest.mark.parametrize("status", ["blocked", "question"])
def test_a_blocked_or_question_item_is_never_stale_however_long_it_stays_quiet(status):
    """Both reports already woke the conductor, and the next move is the conductor's
    or a person's. The worker waiting in silence is the expected shape of that wait,
    so the flag must not point at it -- with the worker idle, closed or neither."""
    item_id = _new_item(acceptance={"kind": "human_approval"})
    wl.apply_worker_report(CONDUCTOR, item_id, status=status, summary="waiting")
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    much_later = datetime.now().astimezone() + timedelta(seconds=wl.DEFAULT_STALE_WINDOW_SECS * 10)
    assert wl.is_stale(item, worker_running=False, now=much_later) is False
    assert status not in wl.STALE_ELIGIBLE_STATUSES


def test_decide_stamps_decided_at_and_it_round_trips():
    item_id = _new_item(acceptance={"kind": "human_approval"})
    fresh = wl.read_work_item(CONDUCTOR, item_id)
    assert fresh is not None and fresh.decided_at is None
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="go ahead")
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.decided_at
    assert wl.WorkItem.from_dict(item.to_dict()).decided_at == item.decided_at


@pytest.mark.parametrize("status", ["blocked", "question"])
def test_a_decide_hands_a_waiting_item_back_to_the_worker(status):
    """After ``blocked`` / ``question`` the move is the conductor's. Its ``decide``
    hands it back, so the worker's silence is measured again -- from the decision,
    under every liveness rule a ``progress`` item gets."""
    item_id = _new_item(acceptance={"kind": "human_approval"})
    wl.apply_worker_report(CONDUCTOR, item_id, status=status, summary="waiting")
    waiting = wl.read_work_item(CONDUCTOR, item_id)
    assert waiting is not None
    much_later = datetime.now(timezone.utc) + timedelta(seconds=wl.DEFAULT_STALE_WINDOW_SECS * 10)
    assert wl.is_stale(waiting, worker_running=False, now=much_later) is False

    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="go ahead")
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.status == status
    decided = wl.parse_stamp(item.decided_at or "")
    assert decided is not None
    inside = decided + timedelta(seconds=wl.DEFAULT_STALE_WINDOW_SECS / 2)
    past = decided + timedelta(seconds=wl.DEFAULT_STALE_WINDOW_SECS * 2)
    # The window runs from the decision, not from the older report.
    assert wl.is_stale(item, worker_running=False, now=inside) is False
    assert wl.is_stale(item, worker_running=False, now=past) is True
    assert wl.is_stale(item, worker_running=True, now=past) is False
    assert wl.is_stale(item, worker_running=False, worker_looping=True, now=past) is False
    # A worker gone after the hand-back is flagged without waiting out the window.
    assert wl.is_stale(item, worker_running=False, worker_closed=True, now=inside) is True


def test_a_report_after_the_decide_takes_the_move_back():
    """The worker answering with ``question`` again hands the move to the conductor."""
    item_id = _new_item(acceptance={"kind": "human_approval"})
    wl.apply_worker_report(CONDUCTOR, item_id, status="question", summary="a or b?")
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="a")
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    item.last_report_at = "2999-01-01T00:00:00+00:00"
    much_later = datetime(3000, 1, 1, tzinfo=timezone.utc)
    assert wl.is_stale(item, worker_running=False, now=much_later) is False


@pytest.mark.parametrize("status", ["blocked", "question"])
def test_a_waiting_item_whose_worker_closed_is_stale_without_a_decide(status):
    """A worker that died while waiting cannot act on any answer, so the conductor
    learns of it at once -- whether or not it has written a ``decide`` yet."""
    item_id = _new_item(acceptance={"kind": "human_approval"})
    wl.apply_worker_report(CONDUCTOR, item_id, status=status, summary="waiting")
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.decided_at is None
    assert wl.is_stale(item, worker_running=False, worker_closed=True) is True
    # Open and silent, it stays the conductor's, so it is not flagged.
    assert wl.is_stale(item, worker_running=False) is False
    assert wl.is_stale(item, worker_running=True, worker_closed=True) is False


def test_a_decide_on_a_progress_item_does_not_restart_its_window():
    """The hand-back is for ``blocked`` / ``question``. A ``progress`` item already
    belongs to the worker, so its silence is measured from its last report."""
    item_id = _new_item(acceptance={"kind": "human_approval"})
    wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="half way")
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="carry on")
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.decided_at
    item.last_report_at = "2026-01-01T00:00:00+00:00"
    item.decided_at = "2026-01-01T00:20:00+00:00"
    now = datetime(2026, 1, 1, 0, 25, tzinfo=timezone.utc)
    assert wl.is_stale(item, worker_running=False, now=now, window_secs=900) is True


def test_a_done_item_whose_worker_closed_is_still_not_stale():
    item_id = _new_item(acceptance={"kind": "human_approval"})
    wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="green")
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    assert wl.is_stale(item, worker_running=False, worker_closed=True) is False


def test_a_worker_parked_in_its_own_loop_is_not_stale():
    """A worker waiting on CI inside its own monitor loop has no turn in flight between
    cycles, yet it is alive: the loop wakes it. Only a loop that is still ACTIVE counts,
    and the caller answers that; ``False``, the default, flags as an unwired caller does."""
    item_id = _new_item(acceptance={"kind": "human_approval"})
    wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="waiting on CI")
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    much_later = datetime.now(timezone.utc) + timedelta(seconds=wl.DEFAULT_STALE_WINDOW_SECS * 10)
    assert wl.is_stale(item, worker_running=False, worker_looping=True, now=much_later) is False
    assert wl.is_stale(item, worker_running=False, worker_looping=False, now=much_later) is True
    assert wl.is_stale(item, worker_running=False, now=much_later) is True
    # A session that is gone cannot be woken by a loop record it left behind.
    assert (
        wl.is_stale(
            item, worker_running=False, worker_looping=True, worker_closed=True, now=much_later
        )
        is True
    )


def test_a_looping_worker_on_a_handed_back_item_is_not_stale():
    """``verdict: fail`` hands the move back to the worker; a worker already looping on
    that retry is alive, and one that is not is still flagged."""
    item_id = _new_item(acceptance={"kind": "human_approval"})
    wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="claimed")
    wl.apply_conductor_action(CONDUCTOR, "verdict", item_id=item_id, verdict="fail", fails=1)
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    much_later = datetime.now(timezone.utc) + timedelta(seconds=wl.DEFAULT_STALE_WINDOW_SECS * 10)
    assert wl.is_stale(item, worker_running=False, now=much_later) is True
    assert wl.is_stale(item, worker_running=False, worker_looping=True, now=much_later) is False


def test_a_done_item_the_conductor_handed_back_is_stale_again():
    """The flag follows who owns the next move, not the last report's word. A ``fail``
    verdict on an item left OPEN is a retry the worker owns, so its silence is a gap
    again — otherwise a worker that vanished mid-retry could never be surfaced."""
    item_id = _new_item(acceptance={"kind": "human_approval"})
    wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="claimed")
    much_later = datetime.now(timezone.utc) + timedelta(seconds=wl.DEFAULT_STALE_WINDOW_SECS * 10)
    waiting = wl.read_work_item(CONDUCTOR, item_id)
    assert waiting is not None
    assert wl.is_stale(waiting, worker_running=False, now=much_later) is False

    wl.apply_conductor_action(CONDUCTOR, "verdict", item_id=item_id, verdict="fail", fails=1)
    handed_back = wl.read_work_item(CONDUCTOR, item_id)
    assert handed_back is not None
    assert handed_back.status == "done" and handed_back.state == "open"
    assert wl.is_stale(handed_back, worker_running=False, now=much_later) is True
    # A pass verdict does not hand it back: the conductor closes it next.
    wl.apply_conductor_action(CONDUCTOR, "verdict", item_id=item_id, verdict="pass")
    verified = wl.read_work_item(CONDUCTOR, item_id)
    assert verified is not None
    assert wl.is_stale(verified, worker_running=False, now=much_later) is False


def test_accept_batch_drops_terminal_items_and_items_with_no_bar():
    kept = _new_item(acceptance={"kind": "human_approval"})
    bare = _new_item(acceptance={})
    closed = _new_item(acceptance={"kind": "human_approval"})
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=closed, state="accepted")
    ids = [entry["id"] for entry in wl.accept_batch(wl.list_work_items(CONDUCTOR))["items"]]
    assert ids == [kept]
    assert bare not in ids


def test_accept_batch_is_what_accept_eval_reads_end_to_end():
    """The keys are accept_eval.py's, not this store's: pipe the batch through the
    real script and every item must come back under its own id, not a positional
    fallback like ``#0``."""
    import subprocess
    import sys

    script = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "kiro_crew"
        / "builtin_skills"
        / "goal-conductor"
        / "scripts"
        / "accept_eval.py"
    )
    item_id = _new_item(acceptance={"kind": "human_approval"})
    batch = wl.accept_batch(wl.list_work_items(CONDUCTOR))
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(batch),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    results = json.loads(proc.stdout)["results"]
    assert [r["id"] for r in results] == [item_id]
    assert results[0]["verdict"] in wl.VERDICTS
    assert results[0]["verdict"] != "error"


def test_work_brief_shows_the_item_and_not_the_conductors_goal():
    item_id = _new_item(title="one job")
    brief = wl.read_work_brief(CONDUCTOR, item_id)
    assert brief is not None
    assert brief["title"] == "one job"
    assert set(brief) == {
        "item_id",
        "title",
        "acceptance",
        "round",
        "decision",
        "status",
        "summary",
    }
    assert "goal" not in brief
    assert "worker_session_key" not in brief
    assert wl.read_work_brief(CONDUCTOR, wl.mint_item_id()) is None


# ── events ────────────────────────────────────────────────────────────────


def test_event_id_is_content_addressed_and_sixteen_hex():
    first = wl.event_id("2026-01-01T00:00:00+00:00", "it_1a2b3c4d", "report", "hello")
    assert len(first) == 16
    assert first == wl.event_id("2026-01-01T00:00:00+00:00", "it_1a2b3c4d", "report", "hello")
    assert first != wl.event_id("2026-01-01T00:00:00+00:00", "it_1a2b3c4d", "report", "hi")


def test_event_id_includes_status_so_a_transition_survives_dedupe():
    ts = "2026-01-01T00:00:00+00:00"
    progress = wl.event_id(ts, "it_1a2b3c4d", "report", "same", status="progress")
    blocked = wl.event_id(ts, "it_1a2b3c4d", "report", "same", status="blocked")
    assert progress != blocked
    # No status renders as the empty string, so non-report kinds keep one formula.
    assert wl.event_id(ts, "it_1a2b3c4d", "create", "t") == wl.event_id(
        ts, "it_1a2b3c4d", "create", "t", status=None
    )


def test_same_summary_status_change_in_one_second_keeps_both_lines(monkeypatch):
    """GPT F1: seconds-precision timestamps plus a byte-identical summary must not
    let a ``blocked`` transition collapse into the ``progress`` line before it."""
    item_id = _new_item()
    monkeypatch.setattr(wl, "_now_iso", lambda: "2026-01-01T00:00:00+00:00")
    wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="same words")
    wl.apply_worker_report(CONDUCTOR, item_id, status="blocked", summary="same words")
    reports = [e for e in wl.read_events(CONDUCTOR, item_id) if e.kind == "report"]
    assert [e.status for e in reports] == ["progress", "blocked"]
    assert len({e.id for e in reports}) == 2


def test_event_id_separators_keep_two_different_tuples_apart():
    # Bare concatenation would make these two identical.
    assert wl.event_id("a", "b", "create", "cd") != wl.event_id("a", "bc", "create", "d")


def test_a_duplicated_event_line_collapses_on_read():
    item_id = _new_item()
    path = wl.item_events_path(CONDUCTOR, item_id)
    line = path.read_text(encoding="utf-8").strip()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.write(line + "\n")
    assert len(wl.read_events(CONDUCTOR, item_id)) == 1


def test_a_torn_event_line_is_skipped_and_the_history_before_it_survives():
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="keep me")
    path = wl.item_events_path(CONDUCTOR, item_id)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"id": "abc", "kind": "rep')
    kinds = [event.kind for event in wl.read_events(CONDUCTOR, item_id)]
    assert kinds == ["create", "decision"]


def test_an_event_line_the_parser_refuses_is_skipped():
    """Ten thousand nested arrays on one line raise ``RecursionError`` from inside
    ``json.loads``, and a 5,000-digit integer raises a bare ``ValueError`` (the
    interpreter's digit limit) — neither is a ``JSONDecodeError``, and each line is
    as unreadable as a torn one: skipped, with its neighbours intact."""
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="keep me")
    path = wl.item_events_path(CONDUCTOR, item_id)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("[" * 10_000 + "]" * 10_000 + "\n")
        handle.write('{"id": "big", "kind": "decision", "text": "", "n": ' + "1" * 5_000 + "}\n")
    assert [event.kind for event in wl.read_events(CONDUCTOR, item_id)] == ["create", "decision"]


def test_a_line_with_an_unknown_kind_or_a_non_object_is_skipped():
    item_id = _new_item()
    path = wl.item_events_path(CONDUCTOR, item_id)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": "x", "kind": "teleport", "text": ""}) + "\n")
        handle.write("[1, 2, 3]\n")
        handle.write("\n")
    assert [event.kind for event in wl.read_events(CONDUCTOR, item_id)] == ["create"]


def test_a_failed_item_write_rolls_the_event_log_back(monkeypatch):
    """State and its event must never disagree. Event goes first;
    if the item write fails the log is restored byte-for-byte, and a retry works."""
    item_id = _new_item()
    item_before, log_before = _bytes_on_disk(item_id)
    real_write = wl._write_record

    def boom_on_item(path, payload):
        if path.name == f"{item_id}.json":
            raise OSError("disk full")
        real_write(path, payload)

    monkeypatch.setattr(wl, "_write_record", boom_on_item)
    with pytest.raises(OSError):
        wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    with pytest.raises(OSError):
        wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="x")
    assert _bytes_on_disk(item_id) == (item_before, log_before)
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.state == "open" and item.status is None
    monkeypatch.setattr(wl, "_write_record", real_write)
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    assert [e.kind for e in wl.read_events(CONDUCTOR, item_id)] == ["create", "close"]


def test_a_failed_event_write_leaves_the_item_untouched(monkeypatch):
    item_id = _new_item()
    before = _bytes_on_disk(item_id)
    real_atomic = wl.atomic_write

    def boom_on_log(path, content, **kw):
        if str(path).endswith(".jsonl"):
            raise OSError("disk full")
        real_atomic(path, content, **kw)

    monkeypatch.setattr(wl, "atomic_write", boom_on_log)
    with pytest.raises(OSError):
        wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="d")
    assert _bytes_on_disk(item_id) == before


def test_create_default_round_is_read_under_the_lock(monkeypatch):
    """A round bump that lands while create waits for the lock must
    be the round the new item is assigned to."""
    wl.ensure_conductor(CONDUCTOR, goal="g")
    real_lock = wl.conductor_lock
    fired: list[str] = []
    from contextlib import contextmanager

    @contextmanager
    def racing_lock(slot_key, **kwargs):
        if not fired:
            fired.append("x")
            monkeypatch.setattr(wl, "conductor_lock", real_lock)
            wl.apply_conductor_action(CONDUCTOR, "goal", round_number=5)
        with real_lock(slot_key, **kwargs):
            yield

    monkeypatch.setattr(wl, "conductor_lock", racing_lock)
    item = wl.apply_conductor_action(CONDUCTOR, "create", title="t", acceptance={})["item"]
    assert item.round == 5


def test_an_acceptance_that_indents_past_the_read_ceiling_is_refused(monkeypatch):
    """The compact-form check is not enough; the stored form is
    indented and must fit the ceiling too, or a successful create reads as absent."""
    wl.ensure_conductor(CONDUCTOR, goal="g")
    monkeypatch.setattr(wl, "MAX_RECORD_BYTES", 2000)
    # Compact size ~600 bytes (under 1000 = ceiling // 2); indent=2 nesting blows past 2000.
    nested: dict = {"k": "v"}
    for _ in range(60):
        nested = {"n": nested}
    assert len(json.dumps(nested).encode()) < 1000
    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.apply_conductor_action(CONDUCTOR, "create", title="t", acceptance=nested)
    assert caught.value.code == wl.CODE_FIELD_TOO_LONG
    assert wl.list_work_items(CONDUCTOR) == []


def test_the_event_cap_drops_the_oldest_line():
    item_id = _new_item()
    for index in range(wl.MAX_EVENTS_PER_ITEM + 5):
        wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision=f"round {index}")
    events = wl.read_events(CONDUCTOR, item_id)
    assert len(events) == wl.MAX_EVENTS_PER_ITEM
    assert events[0].kind != "create"
    assert events[-1].text == f"round {wl.MAX_EVENTS_PER_ITEM + 4}"


def test_read_events_limit_keeps_the_newest():
    item_id = _new_item()
    for index in range(4):
        wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision=str(index))
    assert [e.text for e in wl.read_events(CONDUCTOR, item_id, limit=2)] == ["2", "3"]
    assert wl.read_events(CONDUCTOR, item_id, limit=0) == []


def test_event_text_is_bounded_on_the_line_without_refusing_the_write():
    item_id = _new_item()
    wl.apply_conductor_action(
        CONDUCTOR, "decide", item_id=item_id, decision="d" * wl.MAX_DECISION_CHARS
    )
    item = wl.read_work_item(CONDUCTOR, item_id)
    events = wl.read_events(CONDUCTOR, item_id)
    assert item is not None and len(item.decision) == wl.MAX_DECISION_CHARS
    assert len(events[-1].text) == wl.MAX_EVENT_TEXT_CHARS


def test_appending_an_unknown_event_kind_is_refused():
    item_id = _new_item()
    with wl.item_lock(CONDUCTOR, item_id):
        with pytest.raises(wl.WorkLedgerError):
            wl._append_event_locked(CONDUCTOR, item_id, "teleport", "x")


def test_read_events_is_empty_for_an_absent_or_oversized_log(monkeypatch):
    item_id = _new_item()
    assert wl.read_events(CONDUCTOR, wl.mint_item_id()) == []
    monkeypatch.setattr(wl, "MAX_RECORD_BYTES", 1)
    assert wl.read_events(CONDUCTOR, item_id) == []


# ── the progress coalescing rule (RFC Q6) ─────────────────────────────────


def test_consecutive_progress_reports_collapse_to_the_newest():
    item_id = _new_item()
    for step in ("first", "second", "third"):
        wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary=step)
    events = wl.read_events(CONDUCTOR, item_id)
    assert [(e.kind, e.text) for e in events] == [
        ("create", "port the gate"),
        ("report", "third"),
    ]
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.summary == "third"


def test_a_status_change_between_two_progress_reports_stops_the_merge():
    item_id = _new_item()
    wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="a")
    wl.apply_worker_report(CONDUCTOR, item_id, status="blocked", summary="b")
    wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="c")
    texts = [e.text for e in wl.read_events(CONDUCTOR, item_id) if e.kind == "report"]
    assert texts == ["a", "b", "c"]


def test_a_conductor_event_between_two_progress_reports_stops_the_merge():
    item_id = _new_item()
    wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="a")
    wl.apply_conductor_action(CONDUCTOR, "decide", item_id=item_id, decision="carry on")
    wl.apply_worker_report(CONDUCTOR, item_id, status="progress", summary="b")
    assert [e.kind for e in wl.read_events(CONDUCTOR, item_id)] == [
        "create",
        "report",
        "decision",
        "report",
    ]


def test_only_progress_reports_merge_and_never_done_reports():
    item_id = _new_item()
    wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="a")
    wl.apply_worker_report(CONDUCTOR, item_id, status="done", summary="b")
    texts = [e.text for e in wl.read_events(CONDUCTOR, item_id) if e.kind == "report"]
    assert texts == ["a", "b"]


# ── derived flags ─────────────────────────────────────────────────────────


def test_orphaned_is_derived_and_is_never_a_stored_field():
    item_id = _new_item()
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    assert "orphaned" not in item.to_dict()
    assert "stale" not in item.to_dict()
    assert wl.is_orphaned(item, conductor_slot_exists=False) is True
    assert wl.is_orphaned(item, conductor_slot_exists=True) is False


def test_a_terminal_item_is_neither_orphaned_nor_stale():
    item_id = _new_item()
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item_id, state="accepted")
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None
    assert wl.is_orphaned(item, conductor_slot_exists=False) is False
    assert wl.is_stale(item, worker_running=False, window_secs=0) is False


def test_stale_needs_both_a_quiet_item_and_a_stopped_worker():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    quiet = wl.WorkItem(
        item_id=wl.mint_item_id(),
        created_at=(now - timedelta(hours=2)).isoformat(),
        last_report_at=(now - timedelta(hours=1)).isoformat(),
    )
    assert wl.is_stale(quiet, worker_running=False, now=now, window_secs=60) is True
    # Running is the whole point of the conjunction: a long build is never stale.
    assert wl.is_stale(quiet, worker_running=True, now=now, window_secs=60) is False
    assert wl.is_stale(quiet, worker_running=False, now=now, window_secs=7200) is False


def test_an_item_with_no_report_yet_is_measured_from_creation():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    fresh = wl.WorkItem(item_id=wl.mint_item_id(), created_at=now.isoformat())
    assert wl.is_stale(fresh, worker_running=False, now=now, window_secs=60) is False
    old = wl.WorkItem(item_id=wl.mint_item_id(), created_at=(now - timedelta(hours=1)).isoformat())
    assert wl.is_stale(old, worker_running=False, now=now, window_secs=60) is True


def test_an_unparseable_or_missing_timestamp_reads_as_stale():
    assert wl.is_stale(wl.WorkItem(), worker_running=False) is True
    assert wl.is_stale(wl.WorkItem(last_report_at="not a date"), worker_running=False) is True


def test_naive_timestamps_are_compared_without_raising():
    naive_now = datetime(2026, 1, 1, 12, 0)
    item = wl.WorkItem(last_report_at="2026-01-01T10:00:00")
    assert wl.is_stale(item, worker_running=False, now=naive_now, window_secs=60) is True


def test_stale_uses_a_default_window_when_none_is_given():
    assert wl.DEFAULT_STALE_WINDOW_SECS > 0
    recent = wl.WorkItem(last_report_at=datetime.now().astimezone().isoformat())
    assert wl.is_stale(recent, worker_running=False) is False


# ── parse_stamp: a read filter never raises ───────────────────────────────


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00", "9999-12-31T23:59:59"])
def test_parse_stamp_reads_an_extreme_naive_stamp_as_an_aware_moment(stamp):
    """The two ends of the calendar are what a caller spells for "since the
    beginning of time" / "until the end of it". Shifting either into a local zone
    can run off the calendar, and the platform reports that as ``ValueError`` or
    ``OverflowError`` -- which end trips depends on the host's zone, and on some
    platforms both do. Neither may escape a read: the stamp is a sentinel and is
    read as a moment, whatever the zone."""
    parsed = wl.parse_stamp(stamp)
    assert parsed is not None
    assert parsed.tzinfo is not None and parsed.utcoffset() is not None
    # Whether the local shift succeeded or UTC was the fallback, the fields stand.
    assert parsed.replace(tzinfo=None) == datetime.fromisoformat(stamp)
    # Comparable against the store's own aware stamps in both directions.
    real = wl.parse_stamp(wl._now_iso())
    assert real is not None
    assert (parsed < real) == stamp.startswith("0001")


@pytest.mark.parametrize(
    "failure", [OverflowError("date value out of range"), ValueError("year 0")]
)
def test_parse_stamp_falls_back_to_utc_when_the_local_shift_fails(monkeypatch, failure):
    """The overflow path, forced, so it is covered on every host zone: the naive
    stamp comes back aware in UTC with its fields intact, and nothing raises."""

    class _Unshiftable(datetime):
        def astimezone(self, tz=None):  # type: ignore[override]
            raise failure

    real_parse = wl._parse_iso
    monkeypatch.setattr(
        wl,
        "_parse_iso",
        lambda value: (
            _Unshiftable(1, 1, 1) if value == "0001-01-01T00:00:00" else real_parse(value)
        ),
    )
    parsed = wl.parse_stamp("0001-01-01T00:00:00")
    assert parsed == datetime(1, 1, 1, tzinfo=timezone.utc)
    assert parsed.tzinfo == timezone.utc
    # An ordinary naive stamp still takes the local reading.
    ordinary = wl.parse_stamp("2026-01-01T10:00:00")
    assert ordinary is not None and ordinary.tzinfo is not None
    assert ordinary.replace(tzinfo=None) == datetime(2026, 1, 1, 10, 0)


def test_is_stale_reads_an_extreme_naive_stamp_without_raising():
    """A hand-edited ``created_at`` of year 1 reaches ``is_stale`` on every read;
    shifting it into the local zone runs off the calendar. Through ``parse_stamp``
    it reads as UTC: year 1 is long past any window, so the item is stale — and
    nothing raises."""
    item = wl.WorkItem(
        item_id=wl.mint_item_id(),
        created_at="0001-01-01T00:00:00",
        worker_session_key="chat-w",
    )
    assert wl.is_stale(item, worker_running=False) is True
    assert wl.is_stale(item, worker_running=True) is False
    far = wl.WorkItem(
        item_id=wl.mint_item_id(),
        created_at="9999-12-31T23:59:59",
        worker_session_key="chat-w",
    )
    assert wl.is_stale(far, worker_running=False) is False, "the far end is not past yet"


# ── revertability ─────────────────────────────────────────────────────────


#: The ONLY modules that may import the store. Phase 1 asserted the set was empty,
#: which made that phase revertable by deleting two files; the check becomes an
#: allowlist rather than disappearing, because the intent it enforces outlived the
#: empty set. The bar for each entry is the same one Phase 1's emptiness stood for:
#: even ``mcp_work.py``, the server whose four tools this store exists for, does not
#: import it — it reaches the store over the dashboard HTTP API like every other
#: consumer, which is what keeps identity resolved server-side and lets the Crew
#: page read the same rows. Every entry below is therefore a design decision that
#: has to argue for itself HERE, in its own comment, rather than arrive with a
#: passing suite — which is why the allowlist carries a justification per line and a
#: module that reaches the store only for a constant (as ``ledger_wake.py`` once did,
#: for one int) belongs OUT of this set, mirroring the value instead.
_PERMITTED_STORE_IMPORTERS = frozenset(
    {
        # The four tools' HTTP routes -- the facade and the owners their code lives
        # in -- and the ONLY modules that touch the store directly: identity comes
        # from X-Session-Key, never from the body.
        "dashboard/handlers/work_ledger.py",
        "dashboard/handlers/work_ledger_caller.py",
        "dashboard/handlers/work_ledger_conductor.py",
        "dashboard/handlers/work_ledger_handback.py",
        "dashboard/handlers/work_ledger_read.py",
        "dashboard/handlers/work_ledger_recording.py",
        "dashboard/handlers/work_ledger_refusals.py",
        "dashboard/handlers/work_ledger_worker.py",
        # The crew-log relay reads ONE thing, the binding of the session whose closer
        # just landed, to know which board's log to copy the charge into. The worker
        # named there is the unit's own header slot, never a caller-supplied value,
        # and the relay writes nothing to the store.
        "crew_log/work_relay.py",
        # The Crew panel's steer route reads ONE thing, the binding of the crew
        # member the person typed into, to land the ``steer`` event on that member's
        # item. Same principal argument as the Crew page below: the caller is the
        # dashboard OWNER (cookie, owner-gated), the member is a live session of the
        # root's own tree the route resolved itself, and no key reaches a payload.
        "dashboard/handlers/crew_workers.py",
        # The operator-run cleanup sweep behind ``kirocrew ledger-sweep``.
        # It is a seam deliberately, and it does not weaken the rule the
        # allowlist exists for: it resolves NO caller identity — there is no
        # request and no session to attribute — and it reads the store by
        # enumerating its directories rather than by folding a key someone
        # supplied. It is also not model-reachable: no MCP tool routes to it,
        # because the deletion it performs is irreversible.
        "ledger_sweep.py",
        # Remote dispatch's peer mailbox (rfc-conductor-remote-dispatch §2, §3),
        # behind the ``work_ledger.remote_dispatch`` Feature Preview. The same
        # principal argument as the Crew page below: its only callers are the
        # OWNER's slot create and the owner-gated per-slot routes, reached by a hub
        # over its proxy with the owner token. No agent session names a ledger
        # through it: the board is ``hub:`` plus the hub's key, the item is bound to
        # the slot this create just minted, and the per-slot routes resolve the
        # item from that slot's own binding. The worker itself still reports
        # through the agent routes above, by its own X-Session-Key.
        "remote_dispatch_mailbox.py",
        "dashboard/handlers/remote_work_item.py",
        # The Crew page's masked, cookie-authenticated projection and its action
        # route. A third seam, and the argument is about WHICH PRINCIPAL rather
        # than about convenience.
        #
        # The rule above exists so that one AGENT session cannot name another's
        # ledger: that is a privilege claim, and deriving identity from
        # X-Session-Key is what refuses it. This module's caller is the dashboard
        # OWNER, who already reads every session on this gateway and can stop any
        # of them from the Stop button, and a browser carries a cookie rather than
        # a session key -- so it cannot name a conductor through the agent route at
        # all. An operator naming their own conductor is not the thing the rule
        # forbids.
        #
        # What the rule does still buy here is kept: no payload this module emits
        # carries ``worker_session_key`` (the field is masked and the key-bearing
        # ``bind`` event text is blanked), liveness is joined server-side so the
        # page never needs the key, and its action route resolves the key from the
        # store rather than accepting one from the body.
        #
        # Its ``closed_items`` reader also serves the crewmate goal reports, so
        # ``dashboard/goal_reminder.py`` reaches the ledger through this seam
        # instead of importing the store: the slot there is the crewmate's own
        # goal binding, never a supplied key.
        "dashboard/handlers/work_ledger_board.py",
        # The work-ledger PROBE. A monitor whose subject is a conductor's own
        # ledger has to READ that ledger to observe it — folding its items into a
        # terminal/quiet verdict — and no HTTP route exists for the in-process
        # driver to reach the store the way the tools' handler does. It resolves
        # no external caller's identity (the subject is the slot's own conductor,
        # taken from the loop, not from a supplied key) and is read-only. This is
        # the single new store seam this PR adds.
        "probes/work_ledger.py",
        # The undelivered-call watch. A dispatched worker whose ``work_report`` the
        # harness never delivered (KAS answers a bare tool name with "not available"
        # and never calls the server) has no route to the store at all: the call it
        # meant to make is the one that failed. So the gateway, which sees that
        # failure in the worker's own turn stream, records it. It resolves no
        # supplied identity: the key is the one the turn consumer (dashboard turn,
        # messaging driver) already authenticated for that turn, and the store
        # writes only the derived undelivered-call record for an open item that
        # key is bound to, never an item and never another session's. Reads nothing
        # but the worker's own binding and item.
        "undelivered_call_watch.py",
        # The hub half of remote dispatch (rfc-conductor-remote-dispatch wave 4). A
        # remote worker reports into its OWN crew's store, so the hub files those
        # reports on the conductor's read: it reads the conductor's own board (the
        # key the read route resolved) and writes worker-owned fields only through
        # ``apply_remote_report``, on an item already bound to that crew. It takes
        # no supplied identity; ``create_bound`` binds the caller's own item.
        "dashboard/handlers/remote_ingest.py",
        # ``conductor_wake.py`` was listed here while it resolved a worker slot to its
        # conductor's binding for the close and turn-end triggers. Both triggers are
        # gone and the one that remains is a crew-log bus subscription whose event
        # already names the conductor's board, so the module reads no store at all and
        # the seam is closed again. The guard checks both directions, so leaving the
        # entry would fail this test rather than sit unused.
    }
)


#: An import of THE STORE, spelled by its own module path. A bare
#: ``import work_ledger`` substring is not enough: ``dashboard/handlers/work_ledger.py``
#: shares the basename, so ``from kiro_crew.dashboard.handlers import work_ledger``
#: (``server.py``'s deferred route binder) matched and was reported as a store
#: importer. Anchoring on ``kiro_crew.work_ledger`` / ``from kiro_crew import ...
#: work_ledger`` tells the two apart, and the guard's intent is unchanged.
_STORE_IMPORT_RE = re.compile(
    r"^\s*(?:"
    r"from\s+kiro_crew\.work_ledger\s+import"
    r"|import\s+kiro_crew\.work_ledger"
    r"|from\s+kiro_crew\s+import\s+[^\n]*\bwork_ledger\b"
    r")",
    re.MULTILINE,
)


def test_only_the_phase_2_seams_import_the_module():
    """The store reaches the product through two named modules and no others.

    Asserted on IMPORT statements rather than any mention of the name, and the
    candidate set is asserted non-empty so a moved source tree fails this test
    instead of hollowing it out. Both directions are checked: an unlisted importer
    fails, and a listed module that does not import the store fails too, so the
    allowlist is data rather than lore.
    """
    package = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    # Excluded by PATH, not by basename: the routes module is also called
    # ``work_ledger.py``, and a basename filter skipped it — hiding the very seam
    # this allowlist exists to name.
    store = package / "work_ledger.py"
    sources = [path for path in package.rglob("*.py") if path != store]
    assert len(sources) > 100, f"expected the package tree, found {len(sources)} files"
    assert any(
        path.relative_to(package).as_posix() == "dashboard/handlers/work_ledger.py"
        for path in sources
    ), "the routes module was filtered out of the scan"
    importers = set()
    for path in sources:
        text = path.read_text(encoding="utf-8", errors="replace")
        if _STORE_IMPORT_RE.search(text):
            importers.add(path.relative_to(package).as_posix())
    unlisted = importers - _PERMITTED_STORE_IMPORTERS
    assert not unlisted, (
        f"module(s) import the work-ledger store directly: {sorted(unlisted)}. "
        "Reach it through the dashboard API so identity stays server-resolved, or "
        "argue for a new seam in review and add it to _PERMITTED_STORE_IMPORTERS."
    )
    stale = _PERMITTED_STORE_IMPORTERS - importers
    assert not stale, (
        f"allowlisted module(s) no longer import the store: {sorted(stale)}. Either a "
        "seam moved (fix the entry) or it is gone (delete it)."
    )
