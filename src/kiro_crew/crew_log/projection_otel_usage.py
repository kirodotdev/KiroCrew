"""A session's own OTel usage facts: which agent kind opened it, and who started each
turn -- the SESSION-keyed fold the OTel export subscribes to turn into counters.

WHY A SEPARATE FOLD RATHER THAN READING ``status`` OR ``usage``. Those folds' renders
carry fields this export must never pass to an attribute -- ``status`` keeps ``owner``,
``slot`` and ``cwd`` (``projection_side_panel.py``'s ``_status_render``); ``usage``
keeps per-turn context detail no closed enum describes. Reading them and "picking out"
the safe fields is a boundary a later edit to either fold could silently break. This
fold reads the two raw entry types directly and keeps nothing but small counts, so its
own render is already the shape the export may emit.

WHAT THIS FOLD IS NOT. Not advertised to the side panel (nothing draws it) and not
pushed by the growth feed -- it is registered the way ``class`` is, in
``INTERNAL_PROJECTION_NAMES``, so it shares the eager worker's one pass over a unit's
file without shipping a frame per turn to every owner socket for a reader that is not a
browser.

THE COUNTS ARE CUMULATIVE OVER THE SESSION, NOT A ONE-SHOT EVENT. ``turn/started`` can
land many times in one session's life, and each commit publishes its own
``FoldAdvanced`` for this fold on the crew-log bus (``kiro_crew.crew_log.bus``). The OTel
subscriber (``kiro_crew.metrics.crew_log_export``) is the one that turns a rendered
total into a counter delta, by remembering the LAST total it exported per (session,
field) and adding only the difference -- which is what makes this fold's own state safe
to describe as "a few numbers, step adds one": nothing here decides what reaches an
instrument, it only counts what happened in this unit's own file.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

from kiro_crew.crew_log.projection_values import _as_int
from kiro_crew.crew_log.schema import Entry

#: This fold's registry name. Spelled out here, the way ``DASHBOARD_FOLD_NAME`` is
#: spelled in ``entry_types_surfaces.py``, so the registry and the export module import
#: one constant instead of a literal each could drift from.
OTEL_USAGE_FOLD_NAME: Final[str] = "otel_usage"

#: Agent FILE STEM (the name off ``OWNED_KIRO_AGENT_FILES``, minus ``.json``) to the
#: closed ``agent_kind`` value. One row per owned file, so a 15th file this table has
#: not been told about falls through to ``other`` in :func:`_agent_kind_by_name` rather
#: than leaking its own stem. ``kirocrew`` (the bare default agent) is handled
#: separately there, because it maps to ``default`` rather than to its own stem.
_AGENT_KIND_BY_STEM: Final[dict[str, str]] = {
    "kirocrew-lite": "lite",
    "kirocrew-guest": "guest",
    "kirocrew-conductor": "conductor",
    "kirocrew-pipeline-conductor": "pipeline_conductor",
    "kirocrew-ledger-conductor": "ledger_conductor",
    "kirocrew-security-conductor": "security_conductor",
    "kirocrew-worker": "worker",
    "kirocrew-dashboard-author": "dashboard_author",
    "kirocrew-knowledge": "knowledge",
    "kirocrew-research": "research",
    "kirocrew-team-lead": "team_lead",
    "kirocrew-heartbeat": "heartbeat",
    "kirocrew-dashboard-manager": "dashboard_manager",
    # A crewmate's conductor and its own worker (Crewmate 2.0).
    "kirocrew-mate-conductor": "mate_conductor",
    "kirocrew-mate-worker": "mate_worker",
}

#: The ``agent_kind`` value a session whose opening entry this fold has not yet folded
#: carries. ``session/opened`` has not landed, so there is no agent to classify; the
#: export reads it only for a session the fold also marks ``opened``.
_AGENT_KIND_UNSET: Final[str] = ""


def _check_agent_kind_table_current() -> None:
    """Raise if :data:`_AGENT_KIND_BY_STEM` has drifted from
    :data:`~kiro_crew.agent_files.OWNED_KIRO_AGENT_FILES`.

    A drift guard: a 15th owned file added there and not here would otherwise fall
    through to ``other`` silently, which is a wrong answer rather than a loud failure.
    Run from a test rather than at module import, so a drift reds a test rather than
    failing an eager fold pass. Every stem but the bare default is expected here; the
    default itself (``AGENT_FILENAME``) is handled by the ``raw_agent == "kirocrew"``
    branch in :func:`_agent_kind_by_name`, not by this table, so it is excluded.
    """
    from kiro_crew.agent_files import AGENT_FILENAME, OWNED_KIRO_AGENT_FILES

    expected = {
        filename[: -len(".json")]
        for filename in OWNED_KIRO_AGENT_FILES
        if filename.endswith(".json") and filename != AGENT_FILENAME
    }
    actual = set(_AGENT_KIND_BY_STEM)
    if expected != actual:
        raise RuntimeError(
            "projection_otel_usage._AGENT_KIND_BY_STEM has drifted from "
            f"agent_files.OWNED_KIRO_AGENT_FILES: missing {expected - actual}, "
            f"extra {actual - expected}"
        )


def _agent_kind(raw_agent: str, raw_slot: str) -> str:
    """The closed ``agent_kind`` value for one opening entry -- folded HERE, in the
    fold's own ``step``, so neither raw string is kept.

    TWO FIELDS, BECAUSE ONE KIND LIVES IN EACH. *raw_slot* is the opening entry's
    ``slot``, the session's slot KEY, and it is read first: a crewmate's pinned DM
    session is identified by that key's shape (``members.is_member_session_key``, which
    accepts the key under every prefix it travels under), and the slot key is the only
    field that carries it. The ``agent`` of such a session is the crewmate's own crew
    name -- ``handlers/members_thread.py`` mints the DM slot with ``agent=member_name`` and
    ``chat_runner`` writes ``agent=slot.agent``, ``slot=slot.key`` -- so asking the
    agent name for a slot-key shape is asking the wrong field and can only answer no.

    *raw_agent* answers the rest, from :data:`_AGENT_KIND_BY_STEM` (one row per filename
    in :data:`~kiro_crew.agent_files.OWNED_KIRO_AGENT_FILES`, the files Kiro Crew itself
    owns) plus the bare literal ``kirocrew``, the emitter's own fallback agent name
    (``kiro_crew.crew_log.emit``'s ``_DEFAULT_AGENT``), which maps to ``default``.
    Everything else -- a third-party agent, a user-authored template -- folds to
    ``other``: the raw name is a free-form string with no cardinality bound, and this
    function's whole job is to fold both inputs to the closed set before either is
    stored, so no raw name and no slug ever reaches this fold's state, render or
    savepoint.

    Imports are function-local so classifying a session costs nothing at import time
    and the crew-log package takes no module-scope dependency on ``agent_files`` or
    ``members``.
    """
    try:
        from kiro_crew.members import is_member_session_key
    except Exception as exc:  # pragma: no cover - a classifier miss must never break a fold
        # Log the exception TYPE only -- never ``exc_info``, and never either raw
        # string. Both are locals in this frame, so a traceback would carry the agent
        # name and the slot key into the log, which is exactly the leak the closed-enum
        # fold exists to prevent.
        logging.getLogger("kiro_crew.crew_log.projection").debug(
            "members import failed while classifying agent_kind (%s)", type(exc).__name__
        )
        return _agent_kind_by_name(raw_agent)
    if is_member_session_key(raw_slot):
        return "crewmate"
    return _agent_kind_by_name(raw_agent)


def _agent_kind_by_name(raw_agent: str) -> str:
    """The closed ``agent_kind`` value an AGENT NAME alone can answer: the owned-file
    stems, the bare default, and ``other`` for everything else. Split out so the
    crewmate check in :func:`_agent_kind` is the one thing that reads the slot key, and
    so a ``members`` import that fails still answers from the table.
    """
    if not raw_agent:
        return "other"
    if raw_agent == "kirocrew":
        return "default"
    return _AGENT_KIND_BY_STEM.get(raw_agent, "other")


#: The two entry types this fold answers to. ``session/opened`` fires once per unit;
#: ``turn/started`` fires once per turn. Both are declared, required fields on their
#: types (``entry_types_lifecycle.py``), so a line missing either is a damaged or
#: planted entry and is skipped the way every fold in this package skips one.
SESSION_OPENED_TYPE: Final[str] = "session/opened"
TURN_STARTED_TYPE: Final[str] = "turn/started"

#: Entry types this fold's ``step`` can move. Spelled out, as the registry's own
#: docstring requires of an eager fold: a fold that is woken by type and narrows its own
#: set must say which.
OTEL_USAGE_TYPES: Final[frozenset[str]] = frozenset({SESSION_OPENED_TYPE, TURN_STARTED_TYPE})


def _otel_usage_start() -> dict[str, Any]:
    """One session's usage facts: whether it opened, and turns by actor.

    ``opened_agent_kind`` is the CLOSED ``agent_kind`` value, never the raw agent name
    and never the slot key either. ``step`` folds ``session/opened.agent`` and that
    entry's ``slot`` through :func:`_agent_kind` the moment the opening entry lands and
    stores only the enum result, so the free-form name and the slug inside the key are
    each read once and reach neither this fold's state, nor its render, nor its
    savepoint. ``turns`` is keyed by
    the entry's OWN closed ``actor`` value (``ACTOR_VALUES``, 8 members), so this fold
    adds no vocabulary of its own for that half.

    ``opened_ts`` is the ``session/opened`` entry's OWN timestamp (``Entry.time``,
    epoch ms off the log), so it folds to the same value on every replay and is a
    number, not an id -- it keeps this fold's "only fixed numbers" contract. The export
    subscriber reads it to tell a session OPENED after this process began exporting (a
    genuinely new session, whose opening it should count once) from one whose opening
    predates this process (a resumed session, which it must not re-count). ``0`` until
    the opening entry is folded.
    """
    return {"opened": False, "opened_agent_kind": _AGENT_KIND_UNSET, "opened_ts": 0, "turns": {}}


def _otel_usage_step(state: dict[str, Any], entry: Entry) -> None:
    if entry.type == SESSION_OPENED_TYPE:
        if state["opened"]:
            # A re-attach to an existing crew log (``resumed=True``) still carries
            # ``session/opened`` with the SAME agent the unit was created with -- this
            # fold counts the unit's opening once, not once per claim that reattaches to
            # it, so a second one is skipped rather than double-opening the state.
            return
        agent = entry.data.get("agent")
        # The session's slot KEY, read for its SHAPE only: a crewmate's pinned DM
        # session is the one kind this entry states in ``slot`` rather than in
        # ``agent``, so the classifier is handed both fields and decides which one
        # answers. The key itself is a local that goes out of scope with this call --
        # it is never written to state, render or savepoint, and the slug inside it
        # therefore never leaves this frame.
        slot = entry.data.get("slot")
        state["opened"] = True
        # Fold both raw strings to the closed ``agent_kind`` set right here, and store
        # only the enum: each is a local that goes out of scope with this call, so
        # neither is ever written to state, render or savepoint.
        state["opened_agent_kind"] = _agent_kind(
            agent if isinstance(agent, str) else "",
            slot if isinstance(slot, str) else "",
        )
        # The opening entry's own timestamp, off the log, so a replay folds the same
        # value. The subscriber compares it to when this process began exporting.
        state["opened_ts"] = _as_int(getattr(entry, "time", 0))
        return
    if entry.type == TURN_STARTED_TYPE:
        actor = entry.data.get("actor")
        if not isinstance(actor, str) or not actor:
            # The writer clamps ``actor`` into the closed set before it ever reaches
            # the log (``entry_types.py``'s module docstring), so a line with no
            # string here is a damaged or planted entry -- skipped, not counted.
            return
        turns: dict[str, int] = state["turns"]
        turns[actor] = _as_int(turns.get(actor)) + 1
        return


def _otel_usage_render(state: dict[str, Any]) -> dict[str, Any]:
    """This session's usage facts, as the fixed shape the export subscriber reads.

    ``turns`` is copied rather than handed out live: the export subscriber keeps its
    own last-seen copy per session across separate ``FoldAdvanced`` deliveries, and a
    shared dict would let this fold's next ``step`` mutate a snapshot the subscriber
    has not yet compared against.
    """
    turns: Mapping[str, int] = state["turns"]
    return {
        "opened": bool(state.get("opened")),
        "opened_agent_kind": str(state.get("opened_agent_kind") or _AGENT_KIND_UNSET),
        "opened_ts": _as_int(state.get("opened_ts")),
        "turns": {actor: _as_int(count) for actor, count in turns.items()},
    }


def _otel_usage_copy(state: dict[str, Any]) -> dict[str, Any]:
    """*state* copied one level deep -- the only container ``step`` reaches into is
    ``turns``, so a shallow copy of it is enough to keep ``step`` off the original.
    """
    return {
        "opened": state.get("opened", False),
        "opened_ts": state.get("opened_ts", 0),
        "opened_agent_kind": state.get("opened_agent_kind", _AGENT_KIND_UNSET),
        "turns": dict(state.get("turns") or {}),
    }
