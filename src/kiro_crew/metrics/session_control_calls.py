"""``kirocrew.crew.session_control.calls`` -- how agents drive other sessions.

Session control is the set of MCP tools an agent uses to act on ANOTHER live
conversation: open one, send into it, stop its turn, read its tail, adopt it,
close it. People use the dashboard for all of that, so every caller of these
routes is an agent, and the question this counter answers is which operations
agents actually reach for and how often they fail.

``kirocrew.tool.call.duration`` cannot answer it. That instrument labels a call
by its ACP ``tool_kind``, and an MCP-served call is labelled ``mcp`` whatever
kind it claimed -- which is the right trade there and exactly the wrong grain
here, because it folds all nineteen session-control verbs, every other MCP
server's tools, and their outcomes into one series.

**Three closed attributes, no fourth.**

* ``op`` -- the verb, from :data:`SESSION_CONTROL_OPS`. The route table is
  Kiro Crew's own, so the list is closed by construction rather than by trust;
  :func:`classify_op` still folds an unlisted value to ``other`` so a route
  added without a row here cannot mint a series.
* ``caller`` -- the kind of agent that called, from
  :data:`SESSION_CONTROL_CALLER_KINDS`. DERIVED from
  :data:`~kiro_crew.agent_files.OWNED_KIRO_AGENT_FILES` rather than listed, so a
  new owned agent file joins the enum with no table to update here. An agent
  name outside that set -- a third-party agent, a user-authored template -- is
  ``other``: the raw name is free-form with no cardinality bound, and folding it
  before it is ever passed to the recorder is what keeps the series bounded. A
  blank agent on a resolved slot is ``default``, not ``other``, because that is
  what an ordinary dashboard chat carries; an UNRESOLVED caller is ``other``.
  :func:`caller_kind_for_agent` and :func:`classify_caller` hold that line.

  **``caller`` is the axis that makes this counter exist.** The gateway already
  records ``kirocrew.gateway.request.duration`` for every route it serves,
  labelled ``method`` x ``route_template`` x ``status_class``, so per-route
  volume and per-route failures for these twenty paths are readable there
  today. What no instrument carries is WHO called: that histogram has no
  caller-identity label, and adding one would multiply every route in the
  gateway's bounded template set by sixteen rather than only these twenty.
  ``op`` and ``outcome`` are kept here as the dimensions ``caller`` is crossed
  with -- "which verbs does a conductor drive, and which of them fail for it" is
  one question, not three -- and not as a second way to count a request.
* ``outcome`` -- ``ok`` or ``error``, decided by the HTTP status the route
  answered with (:func:`outcome_for_status`), or ``error`` for a handler that
  raised.

At most 20 x 18 x 2 = 720 series: twenty ``op`` values (the nineteen named
verbs plus ``other``, which the registered ``revive`` and ``history`` routes
reach), eighteen ``caller`` values and two outcomes. A backend that should not
carry all of them can drop ``outcome`` from its dimensions.

**One count per completed call.** The emit site is a wrapper around each route
handler in ``dashboard/handlers/session_control.py``, placed OUTSIDE the
handler's own internal-auth gate, so a call refused before the handler body runs
is still counted once -- with ``outcome=error``, which is what it was. Nothing
inside the handler emits, so the layering cannot double-count, and a call that
never reaches a handler at all (a middleware refusal, a routing 404) is not a
session-control call and contributes nothing.

Recording is an in-memory add behind ``telemetry.enabled``: the recorder a
telemetry-disabled process holds has no meter and returns without touching an
instrument, so nothing here reads a file, writes one or makes a network call on
the request path. Every function is best-effort and swallows its own failures --
a session-control call must never fail because its telemetry did.
"""

from __future__ import annotations

import logging
from typing import Final

from kiro_crew.agent_files import AGENT_FILENAME, OWNED_KIRO_AGENT_FILES

logger = logging.getLogger(__name__)

#: Registered nowhere else: a counter needs no bucket family, unlike the
#: duration instruments in ``metrics/provider.py``'s view table.
SESSION_CONTROL_CALLS_METRIC: Final[str] = "kirocrew.crew.session_control.calls"

#: The ``op`` value for a session-control route this list has no name for.
#: Reachable: ``/revive`` and ``/history`` are registered routes :data:`SESSION_CONTROL_OPS`
#: does not name, so both fold here rather than widening the enum without a
#: decision. Their per-route volume stays readable off
#: ``kirocrew.gateway.request.duration``, which labels every route by its own
#: template, so folding them loses nothing that is only available here.
OP_OTHER: Final[str] = "other"

#: Every ``op`` this counter names, one per session-control tool verb. This
#: module defines the list; no other file holds a copy of it.
#: The spelling is the TOOL's verb, not the route's path segment, so
#: ``/api/session-control/read`` is ``read_message`` (the ``session_read_message``
#: tool) and ``/set-model`` is ``set_model``: the question being asked is which
#: tool an agent reached for, and the tool verb is the stable name of the two --
#: a route path is free to be spelled differently from the verb it serves.
SESSION_CONTROL_OPS: Final[frozenset[str]] = frozenset(
    {
        "create",
        "send",
        "fork",
        "stop",
        "close",
        "continue",
        "broadcast",
        "status",
        "read_message",
        "summary",
        "retry",
        "reload",
        "rename",
        "set_model",
        "end_wait",
        "adopt",
        "handoff",
        "release",
        "generate_title",
    }
)

#: ``caller`` for an agent this build does not own -- a third-party agent, a
#: user-authored template, or a caller whose slot could not be resolved at all.
CALLER_OTHER: Final[str] = "other"

#: ``caller`` for the bare default agent, which maps to this rather than to its
#: own file stem. Carried by BOTH spellings the product uses for it: the literal
#: ``kirocrew``, and a blank agent on a resolved slot -- see
#: :func:`caller_kind_for_agent`.
CALLER_DEFAULT: Final[str] = "default"

#: ``caller`` for a crewmate's pinned DM session. Identified by the SLOT KEY's
#: shape, never by an agent name: ``handlers/members_thread.py`` mints a DM slot with
#: ``agent=<the crewmate's crew name>``, so the agent field of such a session
#: carries a user-chosen name and can only answer ``other``.
CALLER_CREWMATE: Final[str] = "crewmate"

#: The ``outcome`` values, and the only two: a route either answered below 400 or
#: it did not.
OUTCOME_OK: Final[str] = "ok"
OUTCOME_ERROR: Final[str] = "error"

#: Prefix every owned agent file stem but the bare default carries.
_OWNED_STEM_PREFIX: Final[str] = "kirocrew-"


def _kind_from_stem(stem: str) -> str:
    """The ``caller`` value for one owned agent file stem.

    ``kirocrew-pipeline-conductor`` -> ``pipeline_conductor``. Derived, not
    tabulated: a hand-written row per file is a table that drifts silently the
    first time a file is added without it, and the derivation is total -- every
    owned stem has exactly one answer.
    """
    body = stem[len(_OWNED_STEM_PREFIX) :] if stem.startswith(_OWNED_STEM_PREFIX) else stem
    return body.replace("-", "_")


def _owned_kinds() -> dict[str, str]:
    """Agent NAME (an owned file's stem) to its ``caller`` value.

    The bare default is excluded: it is answered by :data:`CALLER_DEFAULT` in
    :func:`caller_kind_for_agent`, not from this mapping.
    """
    default_stem = AGENT_FILENAME[: -len(".json")]
    mapping: dict[str, str] = {}
    for filename in OWNED_KIRO_AGENT_FILES:
        stem = filename[: -len(".json")] if filename.endswith(".json") else filename
        if stem == default_stem:
            continue
        mapping[stem] = _kind_from_stem(stem)
    return mapping


#: Built once at import. ``agent_files`` is a module of plain string constants
#: with no imports of its own, so this costs nothing and forms no cycle -- unlike
#: ``members``, whose import is function-local below for exactly that reason.
_CALLER_BY_AGENT: Final[dict[str, str]] = _owned_kinds()

#: Every value ``caller`` can carry. Pinned as data so a test can assert the set
#: rather than re-deriving it, and so a reader sees the series bound without
#: running the derivation in their head.
SESSION_CONTROL_CALLER_KINDS: Final[frozenset[str]] = frozenset(
    {CALLER_DEFAULT, CALLER_CREWMATE, CALLER_OTHER, *_CALLER_BY_AGENT.values()}
)


def classify_op(op: str | None) -> str:
    """Map *op* onto a member of :data:`SESSION_CONTROL_OPS`, else :data:`OP_OTHER`."""
    if op and op in SESSION_CONTROL_OPS:
        return op
    return OP_OTHER


def caller_kind_for_agent(agent: str | None) -> str:
    """The ``caller`` value an AGENT NAME alone can answer.

    Split out from :func:`classify_caller` so the crewmate check is the one thing
    that reads a slot key, and so this half is testable without a slot.

    **A BLANK agent is the default agent, not an unknown one.** ``_ChatSlot``
    takes ``agent: str = ""`` and an ordinary dashboard chat is minted without
    passing one, so the blank is what the product's most common caller actually
    carries -- and the product already reads it that way everywhere else:
    ``crew_log/emit_session.py`` writes ``agent or _DEFAULT_AGENT`` with
    ``_DEFAULT_AGENT = "kirocrew"``. Answering ``other`` here would file the
    single largest caller population under the bucket that means "an agent this
    build does not own", which is a wrong answer rather than a cautious one.

    This is why a caller whose SLOT could not be resolved must not be passed
    through this function with ``agent=""``: an absent slot is genuinely unknown
    and belongs in ``other``. :func:`classify_caller` draws that line.
    """
    if not agent or agent == AGENT_FILENAME[: -len(".json")]:
        return CALLER_DEFAULT
    return _CALLER_BY_AGENT.get(agent, CALLER_OTHER)


def classify_caller(
    *, session_key: str = "", slot_key: str = "", agent: str = "", slot_found: bool = True
) -> str:
    """The ``caller`` value for one session-control call.

    Three identity inputs, because the crewmate kind lives in a KEY and every
    other kind lives in the agent name. *session_key* is what the caller
    authenticated as (the ``X-Session-Key`` header, a history key) and *slot_key*
    is the live slot it resolved to; either spelling can carry a crewmate DM
    slot's shape, so both are offered to ``members.is_member_session_key``, which
    accepts the key under every prefix it travels under.

    *slot_found* is what separates "this caller runs the default agent" from
    "this caller could not be identified at all". Both arrive with ``agent=""``
    -- a dashboard chat is minted without an agent, and an unresolvable caller
    has no slot to read one from -- and they are different answers:
    :data:`CALLER_DEFAULT` and :data:`CALLER_OTHER` respectively. Pass
    ``slot_found=False`` for the second. The crewmate check runs FIRST either
    way, because a member DM key identifies its caller with no slot at all.

    Never raises, and never returns a value outside
    :data:`SESSION_CONTROL_CALLER_KINDS`: a ``members`` import that fails still
    answers from the agent name, which is a narrower answer rather than a wrong
    one -- a crewmate would read as ``default`` or ``other`` instead of as
    another crewmate.
    """
    try:
        from kiro_crew.members import is_member_session_key

        if is_member_session_key(session_key) or is_member_session_key(slot_key):
            return CALLER_CREWMATE
    except Exception as exc:  # pragma: no cover - a classifier miss must not break a call
        # The exception TYPE only. Both keys and the agent name are locals in
        # this frame, so ``exc_info`` would carry a slot key and an agent name
        # into the log -- the leak the closed enum exists to prevent.
        logger.debug(
            "members import failed while classifying a session-control caller (%s)",
            type(exc).__name__,
        )
    if not slot_found:
        return CALLER_OTHER
    return caller_kind_for_agent(agent)


def outcome_for_status(status: int | None) -> str:
    """``ok`` for a status below 400, ``error`` otherwise.

    An absent status is ``error``: the only way a route answers without one is a
    path that did not produce a response, which is not a completed call.
    """
    if status is not None and 0 < status < 400:
        return OUTCOME_OK
    return OUTCOME_ERROR


def record_session_control_call(op: str, *, caller: str, outcome: str) -> None:
    """Add 1 to :data:`SESSION_CONTROL_CALLS_METRIC`; never raises.

    Each attribute is folded to its closed set HERE rather than trusted from the
    caller, so this is the only place that has to be right about the series
    bound. A no-op when telemetry is off: the recorder a disabled process holds
    has no meter and returns before touching an instrument.
    """
    try:
        from kiro_crew.metrics.provider import get_recorder

        get_recorder().counter(
            SESSION_CONTROL_CALLS_METRIC,
            attrs={
                "op": classify_op(op),
                "caller": caller if caller in SESSION_CONTROL_CALLER_KINDS else CALLER_OTHER,
                "outcome": OUTCOME_OK if outcome == OUTCOME_OK else OUTCOME_ERROR,
            },
            description="Session-control calls by operation, calling agent kind and outcome.",
        )
    except Exception:  # telemetry must never break the instrumented path
        logger.debug("session-control call counter emit failed", exc_info=True)
