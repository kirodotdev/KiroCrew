"""Which of a conductor's items the acceptance evaluator may judge right now.

The selection behind ``accept_eval`` called with no document. Without it, a conductor
woken by a worker's ``done`` reads the board, copies the ``accept_batch`` out of the
reply, drops every entry that is not ``done`` and hands the rest back as the
evaluator's arguments: two model round trips, and a filter applied by hand. The filter
is a rule, not a judgement, so it is applied here, to the board the caller's own read
returned.

THE RULE, the same one a conductor applies to a document it hands over itself:

* Only an OPEN item whose worker reported ``done`` is evaluated. The evaluator answers
  a world-state question, which a worker that is still going can have made true early,
  so the ``done`` is what turns its answer into an acceptance.
* The bar is the ``accept_batch`` entry, which the read route composes from the stored
  acceptance alone. A worker's claimed ``pr`` is not read. An item whose bar is not
  concrete yet is absent from the batch, so it is named instead of evaluated.
* A ``human_approval`` bar is a person's answer. The evaluator can only say
  ``pending`` for it, so it is named instead of evaluated.

WHAT RIDES BACK BESIDE AN ANSWER. A caller that skips its own read still has to act
on the item: a ``done`` that only finished one step of a longer bar is told apart
from a failed attempt by the worker's summary, a bar that is not concrete is promoted
from the worker's claimed ``pr`` into the stored bar, a failed attempt is recorded
as the count so far plus one, an answer that has read ``pending`` past its budget is
settled by the overdue flag and the caller's own last decision, and the worker's
session is the one to close or to send the next step.
:func:`row_facts` names those fields per item, so the reply that carries a verdict
also carries what the read would have shown about that item.

PURE: a board in, a selection out. It reads no store and runs no evaluator, so the
order of the calls around it stays with the tool that owns them.
"""

from __future__ import annotations

from typing import Any

#: What a conductor is told about a ``done`` item the evaluator was not given.
NOT_CONCRETE = (
    "its acceptance is not concrete yet: promote the value the worker reported with "
    "work_ledger_record action=accept, then check it again"
)
NEEDS_PERSON = "a person accepts this item: ask them, then record the verdict yourself"

#: Every key :func:`row_facts` and :func:`select_done` put beside an answer. A reply
#: that has to be made smaller removes exactly these and nothing an answer owns.
ROW_FACT_KEYS: tuple[str, ...] = (
    "acceptance",
    "reported",
    "fails",
    "worker_session_key",
    "pending_overdue",
    "decision",
)


def select_done(
    board: dict[str, Any], item_id: str | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(entries, skipped)`` for *board*, a full ``work_ledger_read`` payload.

    ``entries`` are the ``accept_batch`` entries of the open items whose status is
    ``done``, in ledger order: what the evaluator is handed. ``skipped`` names each
    item left out that the caller would otherwise expect an answer for, as
    ``{"id", "why"}``: a ``done`` item with no concrete bar (which also carries the
    stored ``acceptance``, the object a promotion rewrites) or with a
    ``human_approval`` bar, and, when *item_id* names one item, that item when it is
    unknown, closed or not ``done``.

    With *item_id* only that item is considered. Without it, an open item that is not
    ``done`` is simply not this call's business and is not listed.
    """
    rows = [row for row in board.get("items") or [] if isinstance(row, dict)]
    batch = board.get("accept_batch")
    listed = batch.get("items") if isinstance(batch, dict) else None
    concrete = {str(entry.get("id")): entry for entry in listed or [] if isinstance(entry, dict)}

    entries: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    found = False
    for row in rows:
        row_id = str(row.get("item_id") or "")
        if item_id is not None and row_id != item_id:
            continue
        found = True
        if row.get("state") != "open":
            if item_id is not None:
                skipped.append({"id": row_id, "why": f"it is already {row.get('state')}"})
            continue
        status = row.get("status")
        if status != "done":
            if item_id is not None:
                said = status or "not reported yet"
                skipped.append({"id": row_id, "why": f"its status is {said}, not done"})
            continue
        acceptance = row.get("acceptance")
        if isinstance(acceptance, dict) and acceptance.get("kind") == "human_approval":
            skipped.append({"id": row_id, "why": NEEDS_PERSON})
        elif row_id not in concrete:
            skipped.append({"id": row_id, "why": NOT_CONCRETE, "acceptance": acceptance})
        else:
            entries.append(concrete[row_id])
    if item_id is not None and not found:
        skipped.append({"id": item_id, "why": "no item with this id is on your ledger"})
    return entries, skipped


def row_facts(board: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per item id, the row fields a caller acts on beside that item's answer.

    ``reported`` holds the worker's own ``summary`` and claimed ``pr``: a claim to
    read, never the bar, which is why it sits under its own key. ``fails`` is the
    count the ledger holds and ``worker_session_key`` the session the item is bound
    to. An item whose ``pending_overdue`` flag is set carries the flag, the
    caller's own last ``decision``, which is where the re-armed marker lives, and
    the stored ``acceptance``, which re-arming writes again. A field the row does
    not carry is left out, and so is an item with none.
    """
    facts: dict[str, dict[str, Any]] = {}
    for row in board.get("items") or []:
        if not isinstance(row, dict):
            continue
        entry: dict[str, Any] = {}
        reported = {key: row[key] for key in ("summary", "pr") if row.get(key)}
        if reported:
            entry["reported"] = reported
        fails = row.get("fails")
        if isinstance(fails, int) and not isinstance(fails, bool):
            entry["fails"] = fails
        worker = row.get("worker_session_key")
        if isinstance(worker, str) and worker:
            entry["worker_session_key"] = worker
        if row.get("pending_overdue") is True:
            entry["pending_overdue"] = True
            decision = row.get("decision")
            if isinstance(decision, str) and decision:
                entry["decision"] = decision
            if isinstance(row.get("acceptance"), dict):
                entry["acceptance"] = row["acceptance"]
        if entry:
            facts[str(row.get("item_id") or "")] = entry
    return facts
