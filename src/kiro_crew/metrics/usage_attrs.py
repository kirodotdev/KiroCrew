"""Closed attribute lists shared by the usage metrics.

Three attributes are read by several instruments, so they are defined once here:

* ``trigger_source`` -- WHO started a piece of work: a person, a schedule, a
  monitor wake, a sub-agent, and so on.
* ``origin`` -- WHERE the input arrived: a messaging channel, the dashboard, the
  CLI, a webhook, the API, or the gateway itself.
* ``session_kind`` -- a coarse grouping of the session a metric is about.

Every value is drawn from a closed tuple below, and every normaliser maps an
unrecognised input to ``other``. A metric attribute that could take an open value
would mint one series per distinct input and could carry user data; the tests in
``test/metrics/test_usage_attrs.py`` pin each tuple and each mapping.

**``trigger_source`` is passed in, never guessed.** The code that starts the work
knows who asked for it, so each caller states it. A caller that states nothing
records ``other`` -- never ``user``. Defaulting to ``user`` would count every
unattended turn as a person's, which is the exact error these metrics exist to
avoid.

This module is a leaf: it imports only the session-key classifier and the channel
namespace constant, so any recorder can import it without a cycle.
"""

from __future__ import annotations

import contextvars

from kiro_crew.constants import CHANNEL_SESSION_NAMESPACES
from kiro_crew.messaging.link import telemetry_channel_of

# -- trigger_source -----------------------------------------------------------

TRIGGER_USER = "user"
TRIGGER_CRON = "cron"
TRIGGER_HEARTBEAT = "heartbeat"
TRIGGER_MONITOR = "monitor"
TRIGGER_SUBAGENT = "subagent"
TRIGGER_WORKFLOW = "workflow"
TRIGGER_WEBHOOK = "webhook"
TRIGGER_APP = "app"
TRIGGER_AGENT = "agent"
TRIGGER_SYSTEM = "system"
TRIGGER_OTHER = "other"

#: Every value ``trigger_source`` can carry.
TRIGGER_SOURCE_VALUES: tuple[str, ...] = (
    TRIGGER_USER,
    TRIGGER_CRON,
    TRIGGER_HEARTBEAT,
    TRIGGER_MONITOR,
    TRIGGER_SUBAGENT,
    TRIGGER_WORKFLOW,
    TRIGGER_WEBHOOK,
    TRIGGER_APP,
    TRIGGER_AGENT,
    TRIGGER_SYSTEM,
    TRIGGER_OTHER,
)

ATTR_TRIGGER_SOURCE = "trigger_source"

_TRIGGER_SET = frozenset(TRIGGER_SOURCE_VALUES)


def trigger_source(value: object) -> str:
    """Close *value* onto :data:`TRIGGER_SOURCE_VALUES`; anything else is ``other``.

    A missing or empty value is ``other``, never ``user``.
    """
    if isinstance(value, str) and value in _TRIGGER_SET:
        return value
    return TRIGGER_OTHER


#: The dashboard turn actor (``crew_log.entry_types_lifecycle.ACTOR_VALUES``)
#: mapped to ``trigger_source``. ``user`` is NOT in this table on purpose: a
#: ``user`` actor is the dashboard's fallback for a turn no dispatch claimed, so
#: it becomes ``user`` only when the turn also carries a person's send -- see
#: :func:`trigger_from_turn_actor`.
_ACTOR_TO_TRIGGER: dict[str, str] = {
    "app": TRIGGER_APP,
    "crew": TRIGGER_AGENT,
    "cron": TRIGGER_CRON,
    "autonudge": TRIGGER_MONITOR,
    "subagent": TRIGGER_SUBAGENT,
    "gateway": TRIGGER_SYSTEM,
    "other": TRIGGER_OTHER,
}


def trigger_from_turn_actor(actor: object, *, person_send: bool) -> str:
    """``trigger_source`` for a dashboard turn from its recorded actor.

    *person_send* is True only when the turn carries a person's send: the
    dashboard's authenticated-human origin or a channel-origin directive, and not
    a queue row restored after a restart (whose provenance is lost). A ``user``
    actor without it is ``other``.
    """
    if actor == "user":
        return TRIGGER_USER if person_send else TRIGGER_OTHER
    if isinstance(actor, str):
        return _ACTOR_TO_TRIGGER.get(actor, TRIGGER_OTHER)
    return TRIGGER_OTHER


def trigger_for_dashboard_turn(
    actor: object, *, user_origin: bool, channel_origin: bool, provenance_restored: bool
) -> str:
    """``trigger_source`` for a dashboard turn from its entry state.

    A person's send is the authenticated-human or channel-origin directive on a
    turn that is not a queue row restored after a restart (whose provenance is
    lost). Same predicate the dashboard's crew-log gates use for a person's turn.
    """
    person_send = bool(user_origin or channel_origin) and not provenance_restored
    return trigger_from_turn_actor(actor, person_send=person_send)


# The trigger of the turn running in this context. Set by the turn entry points
# that know it, and read when that turn opens a backend session, so the session
# start can say who caused it without threading an argument through every
# allocation layer. Unset reads as ``None`` -- callers then fall back to the
# session-key derivation, which never yields ``user``.
_CURRENT_TRIGGER: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kirocrew_current_trigger", default=None
)


def set_current_trigger(value: str) -> contextvars.Token:
    """Mark the turn running in this context as started by *value*."""
    return _CURRENT_TRIGGER.set(trigger_source(value))


def current_trigger() -> str | None:
    """The trigger the running turn declared, or ``None`` when none did."""
    return _CURRENT_TRIGGER.get()


# -- origin -------------------------------------------------------------------

ORIGIN_DASHBOARD = "dashboard"
ORIGIN_CLI = "cli"
ORIGIN_WEBHOOK = "webhook"
ORIGIN_API = "api"
ORIGIN_INTERNAL = "internal"
ORIGIN_OTHER = "other"

#: Every value ``origin`` can carry: the channel namespaces, then the local ones.
ORIGIN_VALUES: tuple[str, ...] = tuple(CHANNEL_SESSION_NAMESPACES) + (
    ORIGIN_DASHBOARD,
    ORIGIN_CLI,
    ORIGIN_WEBHOOK,
    ORIGIN_API,
    ORIGIN_INTERNAL,
    ORIGIN_OTHER,
)

ATTR_ORIGIN = "origin"

_ORIGIN_SET = frozenset(ORIGIN_VALUES)

#: ``telemetry_channel_of`` labels that are not channel namespaces, mapped to
#: ``origin``. Every member of ``TELEMETRY_CHANNELS`` must be either a channel
#: namespace or a key here; the test enforces that, so a new label cannot ship
#: unmapped.
ORIGIN_OF_SESSION_SOURCE: dict[str, str] = {
    "dashboard": ORIGIN_DASHBOARD,
    "thread": ORIGIN_DASHBOARD,
    "side": ORIGIN_DASHBOARD,
    "secretary": ORIGIN_DASHBOARD,
    "channel": ORIGIN_DASHBOARD,
    "cli": ORIGIN_CLI,
    "cron": ORIGIN_INTERNAL,
    "subagent": ORIGIN_INTERNAL,
    "taskrunner": ORIGIN_INTERNAL,
    "workflow": ORIGIN_INTERNAL,
    "workflow_pool": ORIGIN_INTERNAL,
    "workflow_author": ORIGIN_INTERNAL,
    "heartbeat": ORIGIN_INTERNAL,
    "consolidation": ORIGIN_INTERNAL,
    "background": ORIGIN_INTERNAL,
    "other": ORIGIN_OTHER,
    "unknown": ORIGIN_OTHER,
}

_CHANNEL_SET = frozenset(CHANNEL_SESSION_NAMESPACES)


def origin_of_source(session_source: str) -> str:
    """Roll a ``telemetry_channel_of`` label up to an ``origin`` value."""
    if session_source in _CHANNEL_SET:
        return session_source
    return ORIGIN_OF_SESSION_SOURCE.get(session_source, ORIGIN_OTHER)


def origin_of(session_key: str | None, *, explicit: str | None = None) -> str:
    """``origin`` for a turn on *session_key*.

    *explicit* wins when it is a member of :data:`ORIGIN_VALUES`: a webhook and
    the API name themselves, because their session keys do not say where the
    input came from. Never raises.
    """
    if explicit and explicit in _ORIGIN_SET:
        return explicit
    try:
        return origin_of_source(telemetry_channel_of(session_key))
    except Exception:
        return ORIGIN_OTHER


# -- session_kind -------------------------------------------------------------

SESSION_KIND_VALUES: tuple[str, ...] = (
    "chat",
    "channel",
    "cron",
    "subagent",
    "workflow_step",
    "task_runner",
    "heartbeat",
    "background",
    "other",
)

ATTR_SESSION_KIND = "session_kind"

#: ``telemetry_channel_of`` labels that are not channel namespaces, mapped to
#: ``session_kind``. Same completeness rule as :data:`ORIGIN_OF_SESSION_SOURCE`.
SESSION_KIND_OF_SOURCE: dict[str, str] = {
    "dashboard": "chat",
    "thread": "chat",
    "side": "chat",
    "secretary": "chat",
    "cli": "chat",
    # ``channel:`` is the reply-token-bound send namespace: a channel
    # conversation, so it groups with the channel namespaces.
    "channel": "channel",
    "cron": "cron",
    "subagent": "subagent",
    "workflow": "workflow_step",
    "workflow_pool": "workflow_step",
    "workflow_author": "workflow_step",
    "taskrunner": "task_runner",
    "heartbeat": "heartbeat",
    "consolidation": "background",
    "background": "background",
    "other": "other",
    "unknown": "other",
}


def session_kind_of_source(session_source: str) -> str:
    """Roll a ``telemetry_channel_of`` label up to a ``session_kind`` value."""
    if session_source in _CHANNEL_SET:
        return "channel"
    return SESSION_KIND_OF_SOURCE.get(session_source, "other")


#: The trigger a session kind implies when nothing else says. Only kinds whose
#: opener is fixed by the key are listed; a chat or channel session is opened by
#: whoever sent its first turn, which the key cannot tell, so it is absent and
#: falls back to ``other``.
_TRIGGER_OF_SESSION_KIND: dict[str, str] = {
    "cron": TRIGGER_CRON,
    "subagent": TRIGGER_SUBAGENT,
    "workflow_step": TRIGGER_WORKFLOW,
    "task_runner": TRIGGER_WORKFLOW,
    "heartbeat": TRIGGER_HEARTBEAT,
    "background": TRIGGER_SYSTEM,
}


def session_start_trigger(session_kind: str, *, explicit: str | None = None) -> str:
    """``trigger_source`` for a session start of *session_kind*.

    A kind whose opener the key fixes (cron, sub-agent, workflow, heartbeat,
    background) always wins, so a sub-agent opened during a person's turn is
    still ``subagent``. Otherwise *explicit* (the opener's own statement, or the
    running turn's trigger) is used, and without it ``other``.
    """
    implied = _TRIGGER_OF_SESSION_KIND.get(session_kind)
    if implied is not None:
        return implied
    if explicit:
        return trigger_source(explicit)
    return TRIGGER_OTHER
