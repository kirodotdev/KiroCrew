"""The ``reflex`` op's action space: what ONE decision request offers, and what it means.

One request asks two kinds of Choice together (the design of browser-use/jev-ultrafast,
MIT, Copyright (c) 2026 Browser Use; see NOTICE): an OPERATION head over the
moves this call allows (a key, a scroll, a drag, a wait, or an operation that needs
a target: click, hover, type, select) and, per targeted operation, a speculative
TARGET head over the observed elements that operation fits. Only the target head
matching the picked operation is used; the others are discarded unread, so a
target can never be applied to an operation it does not fit.

Everything here is pure: it reads the walker's element table (``observe``) and the
call's plan, and names options. Questions are built in ``browser_harness.decide``;
input is run by ``browser_harness.reflex``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

#: Operation-head option names. Keys, drags and bound typing get their own names.
WAIT = "WAIT"
SCROLL_UP = "SCROLL_UP"
SCROLL_DOWN = "SCROLL_DOWN"
#: Operations that need a target, by the walker's ``ops`` word.
TARGETED = {"click": "CLICK", "hover": "HOVER", "type": "TYPE_TEXT", "select": "SELECT"}
#: Their plain-words labels on the operation head.
_TARGETED_LABELS = {
    "click": "Click one of the elements: a button, link, option, suggestion or day.",
    "hover": "Move the pointer over one of the elements.",
    "type": "Replace the text in one of the editable fields with one of the given values.",
    "select": "Pick one of the offered values in a dropdown.",
}
#: Elements offered to one target head; the walker sends at most 250.
TARGET_CAP = 200


@dataclass
class Move:
    """One thing that can be done: an option on the operation head, or one target.

    *key* names it for the risk and no-op books (``"click e12"``, ``"press Enter"``);
    *label* is the plain words shown on the live card and in risk questions.
    """

    key: str
    kind: str
    label: str
    args: dict[str, Any] = field(default_factory=dict)
    #: The walker's guard for a targeted move: input happens only while it matches.
    guard: str = ""
    #: Risk is judged by code (``0.0``) or by one Noul, once; ``None`` until then.
    risky: float | None = None


@dataclass
class Space:
    """One request's offer: operation options, target options per operation, values."""

    operations: dict[str, str]
    #: Operation option -> {target option -> its Move}.
    targets: dict[str, dict[str, Move]]
    #: Operation options with no target -> their Move (WAIT is ``kind == "wait"``).
    moves: dict[str, Move]
    values: list[str]
    #: Each offered target's guard as THIS request saw it. A Move is shared across
    #: requests and refreshed by later ones, so the executor checks against this:
    #: an answer about an element that has changed since is refused as stale.
    guards: dict[str, str] = field(default_factory=dict)

    def target_criteria(self) -> dict[str, dict[str, str]]:
        return {
            op: {opt: m.label for opt, m in cands.items()} for op, cands in self.targets.items()
        }

    def resolve(self, operation: str | None, picks: dict[str, str | None]) -> Move | None:
        """The one Move the answers name, or ``None`` when they name nothing offered."""
        if operation in self.moves:
            return self.moves[operation]
        cands = self.targets.get(operation or "")
        if not cands:
            return None
        return cands.get(picks.get(operation or "") or "")


def describe(row: dict[str, Any]) -> str:
    """One element as the model reads it: role, name, current value and state."""
    text = f'{row.get("role") or "generic"} "{row.get("name") or ""}"'
    if row.get("value"):
        text += f' value "{row["value"]}"'
    flags = [
        f"{key}={row[key]}"
        for key in ("checked", "selected", "expanded")
        if row.get(key) is not None
    ]
    if flags:
        text += " [" + ", ".join(flags) + "]"
    return text[:240]


def element_state(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The table as page state: one entry per element, refs kept, guards dropped."""
    out = []
    for row in rows:
        entry = {
            k: row[k]
            for k in ("ref", "role", "name", "value", "checked", "selected", "expanded")
            if row.get(k) not in (None, "")
        }
        if row.get("options"):
            entry["options"] = [o.get("label", "") for o in row["options"]]
        out.append(entry)
    return out


def build(
    plan_moves: list[Move],
    targeted: tuple[str, ...],
    allowed_refs: frozenset[str] | None,
    rows: list[dict[str, Any]] | None,
    values: list[str],
    known: dict[str, Move],
    *,
    wait: bool,
    offer: Callable[[Move], bool] = lambda _m: True,
) -> Space:
    """This step's offer. *known* carries Moves (and their risk) across steps by key.

    *plan_moves* are the call's fixed moves (keys, scrolls, drags, bound typing);
    *targeted* the operations whose target comes from *rows*, the walker's table,
    limited to *allowed_refs* when the call named refs. *offer* is the caller's
    filter (a move judged risky, or withdrawn as a no-op, is not offered).
    """
    operations: dict[str, str] = {}
    moves: dict[str, Move] = {}
    for move in plan_moves:
        move = known.setdefault(move.key, move)
        if not offer(move):
            continue
        operations[_option(move)] = move.label
        moves[_option(move)] = move
    targets: dict[str, dict[str, Move]] = {}
    guards: dict[str, str] = {}
    for op in targeted:
        cands: dict[str, Move] = {}
        for row in rows or []:
            if len(cands) >= TARGET_CAP:
                break
            ref = row.get("ref")
            if not isinstance(ref, str) or op not in (row.get("ops") or []):
                continue
            if allowed_refs is not None and ref not in allowed_refs:
                continue
            if op == "type" and not values:
                continue
            for option, move in _targets(op, row):
                held = known.get(move.key)
                if held is not None:
                    if held.label != move.label:
                        # The same element now reads differently ("Next" became
                        # "Place order"): its risk is judged again, as it reads now.
                        held.risky = move.risky
                    held.guard, held.args, held.label = move.guard, move.args, move.label
                    move = held
                else:
                    known[move.key] = move
                if not offer(move):
                    continue
                cands[option] = move
                guards[move.key] = move.guard
        if cands:
            targets[TARGETED[op]] = cands
            operations[TARGETED[op]] = _TARGETED_LABELS[op]
    if wait:
        moves[WAIT] = known.setdefault("wait", Move("wait", "wait", "Wait", risky=0.0))
        operations[WAIT] = "Do nothing now and let the page move on."
    return Space(operations, targets, moves, values, guards)


def _option(move: Move) -> str:
    return move.key.upper().replace(" ", "_")


def _targets(op: str, row: dict[str, Any]) -> list[tuple[str, Move]]:
    ref, guard, label = str(row["ref"]), str(row.get("g") or ""), describe(row)
    if op == "select":
        out = []
        for opt in row.get("options") or []:
            index = opt.get("i")
            if not isinstance(index, int):
                continue
            option = f"{ref}:{index}"
            text = f'{label} -> "{opt.get("label", "")}"'
            out.append(
                (
                    option,
                    Move(
                        f"select {option}",
                        "select",
                        f"Select {text}",
                        {"ref": ref, "index": index},
                        guard,
                    ),
                )
            )
        return out
    words = {"click": "Click", "hover": "Hover", "type": "Type into"}[op]
    # Hover and typing (a replaced field value) can be undone: code judges them safe.
    risky = 0.0 if op in ("hover", "type") else None
    return [(ref, Move(f"{op} {ref}", op, f"{words} {label}", {"ref": ref}, guard, risky))]
