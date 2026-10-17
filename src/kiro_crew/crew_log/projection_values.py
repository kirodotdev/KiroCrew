"""Bounded coercions every fold applies to the ``data`` of an entry it reads.

Each caps what a fold retains: a label is cut to ``TEXT_LIMIT``, an id past ``ID_LIMIT``
identifies nothing, and a count is never a bool. ``_rows_in`` is the container count the
warm budget charges a fold's state by.
"""

from __future__ import annotations

from typing import Any, Final

#: Characters of any retained LABEL -- a tool or model name, a server, an
#: approval's tool or reason, a decision. Capping the COUNT of retained values
#: bounds nothing on its own: every one of these strings comes off the wire, and
#: a handful of near-64-KiB ones dwarf the budget they are counted against. A
#: label is a display value, so the honest bound is to keep its head and let the
#: tail go.
TEXT_LIMIT: Final[int] = 200

#: Characters of a retained IDENTITY -- a tool ``call_id``, an ``approval_id``.
#: These are NOT truncated like a label: two distinct ids sharing a 200-character
#: head would become one identity, and one completion would then close a
#: different call's frame. Past this length an id identifies NOTHING, exactly like
#: an absent one, and is counted and left unpaired.
ID_LIMIT: Final[int] = 200


def _as_int(value: Any) -> int:
    """*value* when it is a real int, else 0 -- a bool is not a count."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _keyable(label: str) -> bool:
    """Whether *label* is safe to use as a per-thing KEY.

    ``_as_str`` cuts a retained label to ``TEXT_LIMIT``, and a label sitting
    exactly at that length cannot be told apart from one that was cut -- so two
    different things sharing a head would land on one key and report each other's
    totals, which is a wrong answer rather than a big one. A label at the limit is
    therefore not keyed. It goes where a label past the COUNT budget goes: counted
    in the whole-session totals, which stay exact, and reported as omitted detail.
    """
    return len(label) < TEXT_LIMIT


def _note_omitted(
    state: dict[str, Any],
    label: str,
    *,
    seen_key: str,
    count_key: str,
    saturated_key: str,
    budget: int,
) -> None:
    """Record *label* as detail this fold left out, counting each one once.

    The count is of DISTINCT labels, so it is deduplicated against a list -- and
    that list is itself retained, so it is capped like everything else here. With
    the cap reached, a label cannot be recognised as one already counted, and
    counting it again would count one label once per APPEARANCE rather than once:
    a tool name reaches this path from its call and again from its completion, and
    a model reaches it once per turn. The count therefore stops at the budget and
    *saturated_key* says it has become a floor rather than a total.
    """
    seen: list[str] = state[seen_key]
    if label in seen:
        return
    if len(seen) < budget:
        seen.append(label)
        state[count_key] += 1
    else:
        state[saturated_key] = True


def _as_text_or_none(value: Any) -> str | None:
    """*value* cut to ``TEXT_LIMIT`` when it is a string, else ``None``.

    The nullable sibling of :func:`_as_str`, for a retained field whose ABSENCE is
    meaningful -- a close reason, a stop reason, an error -- where the empty string
    would assert a reason of no characters instead of no reason at all. The size
    bound is the same, because the field is retained either way.
    """
    if not isinstance(value, str):
        return None
    return value[:TEXT_LIMIT]


def _as_str(value: Any) -> str:
    """*value* when it is a string, cut to ``TEXT_LIMIT``, else the empty one.

    Every ``data`` field these folds read comes off bytes a reader does not
    control, so the shape is checked here rather than trusted from the type
    declaration: a declaration binds the WRITER, and a damaged or planted line is
    exactly the input that ignores it.

    The LENGTH is part of that shape. Every caller here retains what it gets --
    as a ``by_name`` key, a server in a row, an approval's reason -- and a count
    cap bounds how MANY are kept, never how big each one is, so one coercion
    point is where the size bound belongs rather than at each of the dozen
    retention sites that would each have to remember it.
    """
    if not isinstance(value, str):
        return ""
    return value[:TEXT_LIMIT]


def _as_id(value: Any) -> str:
    """*value* when it is a string short enough to PAIR on, else the empty one.

    An identity is not a label and is deliberately not truncated: two distinct
    ids sharing a ``TEXT_LIMIT``-character head would collapse into one identity,
    and a completion would then close a different call's frame -- turning a
    bounded-memory fix into a wrong answer. Past ``ID_LIMIT`` an id identifies
    nothing, which is the same thing an absent one does, so it takes the same
    path: counted, and left unpaired.
    """
    if not isinstance(value, str) or not value or len(value) > ID_LIMIT:
        return ""
    return value


def _rows_in(value: Any) -> int:
    """How many rows *value* holds, for a list or a dict; 0 for anything else.

    Tolerant on purpose. These run over a state that may have come off disk from an
    older build, and a charge that raised would take down the read that was storing the
    cell -- where answering 0 only under-counts a container the fold does not use.
    """
    return len(value) if isinstance(value, (list, dict)) else 0
