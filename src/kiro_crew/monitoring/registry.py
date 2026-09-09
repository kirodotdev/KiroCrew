"""The catalogue of monitored kinds, each registered as DATA.

The four boundaries that gate a kind read this table instead of naming a kind
themselves: an enum in the tool schema, a frozenset in the validator, an ``if`` in
the HTTP handler, and the ``if`` on the persistence-only path. Without it, adding a
kind means finding all four, every one of them a place to forget.

The claim is scoped to those four deliberately. Other spellings of a kind or an
objective survive elsewhere -- a reason code in the GitHub observation, the irq
subsystem's own dispatch -- and this module does not reach them.

This module holds one entry per kind and the boundaries ask it. Two properties are
worth separating carefully, because collapsing them is how a new kind acquires a
claim nobody checked:

**Objectives are declared BY THE KIND, not drawn from a shared vocabulary.** There
is no global objective enum here. ``review_ready`` means "the pull request is ready
for a human to look at", which is not a sentence about a calendar or a ticket, and a
shared list would make every new kind edit a common set -- the same defect as a
dispatch branch, moved to a different file.

**A capability is something a kind ANSWERS FOR, not something the registry polices.**
``supports_shadow`` says the persistence-only path is implemented for this kind. That
is a statement about what code exists, not about what a caller may ask for, so it
belongs to the kind. Modelled as an allowlist instead, a kind added later would
silently inherit a claim about a path it has never run.

Deliberately DATA only: no probe factories and no imports of any kind's
implementation. ``kiro_crew.validation`` imports this module, and it is imported by
almost everything, so pulling a provider's transitive dependencies in here would
make a validator drag a GitHub client behind it.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The structured monitor a caller arms through ``monitor_watch``. Its subject is a
#: public GitHub pull request and its objective is review readiness.
GITHUB_PULL_REQUEST = "github_pull_request"

#: The observation-gated babysit watch. Armed only INTERNALLY, from a loop's own
#: message text via ``probes.targets.infer``, never by a caller naming it, which is
#: why it is registered but not publicly armable.
#: Spelled here as data rather than imported: ``probes/__init__.py`` owns the same
#: string for the irq subsystem, and the two packages have no import edge in either
#: direction today. Introducing one for a constant would couple them ahead of the
#: porting work that unifies the vocabularies. ``test_monitor_kind_registry`` asserts
#: the two spellings agree, so a drift fails loudly instead of silently.
GH_PR = "gh-pr"

REVIEW_READY = "review_ready"


@dataclass(frozen=True)
class MonitorKind:
    """One monitored kind and what it answers for."""

    name: str
    #: The objectives THIS kind supports. Not a slice of a shared enum.
    objectives: frozenset[str]
    #: Whether a caller may name this kind when arming a monitor. A kind derived
    #: internally from a loop's message is registered without being requestable.
    publicly_armable: bool
    #: Whether the persistence-only probe path is implemented for this kind.
    supports_shadow: bool


_KINDS: dict[str, MonitorKind] = {
    GITHUB_PULL_REQUEST: MonitorKind(
        name=GITHUB_PULL_REQUEST,
        objectives=frozenset({REVIEW_READY}),
        publicly_armable=True,
        supports_shadow=True,
    ),
    GH_PR: MonitorKind(
        name=GH_PR,
        objectives=frozenset({REVIEW_READY}),
        publicly_armable=False,
        supports_shadow=False,
    ),
}


def monitor_kind(name: object) -> MonitorKind | None:
    """The entry for *name*, or None when nothing is registered under it."""
    if not isinstance(name, str):
        return None
    return _KINDS.get(name)


def publicly_armable_kinds() -> frozenset[str]:
    """The kinds a caller may name. This is what an entry allowlist should expose."""
    return frozenset(entry.name for entry in _KINDS.values() if entry.publicly_armable)


def publicly_armable_objectives() -> frozenset[str]:
    """Every objective reachable through a publicly armable kind.

    A union, because a JSON-Schema ``enum`` and a validator's ``allowed`` set are
    both flat: neither can express "this objective only with that kind". The pairing
    is enforced by :func:`kind_supports_objective` at the boundary that knows both,
    so the flat set is a first filter and never the whole check.
    """
    return frozenset(
        objective
        for entry in _KINDS.values()
        if entry.publicly_armable
        for objective in entry.objectives
    )


def kind_supports_objective(kind: object, objective: object) -> bool:
    """Whether *kind* is registered AND declares *objective*."""
    entry = monitor_kind(kind)
    return entry is not None and isinstance(objective, str) and objective in entry.objectives


def kind_supports_shadow(kind: object) -> bool:
    """Whether the persistence-only probe path is implemented for *kind*.

    An unregistered kind answers False: a path nobody registered is a path nobody
    implemented.
    """
    entry = monitor_kind(kind)
    return entry is not None and entry.supports_shadow
