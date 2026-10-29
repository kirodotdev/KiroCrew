"""Aggregate what this Crew can truthfully back into one :class:`Capabilities`.

Behaviour modules do not set capability fields directly. Each registers a
source with :func:`register_capability_source`, declaring the fields it speaks
for, and the gateway wires those registrations at startup. With nothing
registered every field stays at the contract default, ``unavailable``, so a
build that wires nothing claims nothing.

Aggregation is conservative by construction:

* a source answers only for the fields it declared; anything else it returns
  is ignored;
* a source that raises contributes ``unavailable`` for every field it declared;
* when two sources answer for one field, the weaker answer wins;
* ``containment_level`` is ``none`` unless ``containment`` itself is available
  or degraded, so a level is never claimed for containment that is not running;
* two sources naming different membership links leave the link empty.

Sources run on the request path. A source answers from state it already holds
and performs no I/O.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional

from kiro_crew.shared_crew.access import setup_paths_dict, tailnet_direct
from kiro_crew.shared_crew.contracts import (
    MEMBERSHIP_LINKS,
    Capabilities,
    CapabilityState,
    EnforcementLevel,
)

logger = logging.getLogger(__name__)

#: ``Capabilities`` fields whose value is a :class:`CapabilityState`.
STATE_FIELDS: frozenset[str] = frozenset(
    {
        "attribution",
        "presence",
        "queueing",
        "containment",
        "authoritative_directory",
        "remote_actions",
    }
)
FIELD_CONTAINMENT_LEVEL = "containment_level"
FIELD_MEMBERSHIP_LINK = "membership_link"
CAPABILITY_FIELDS: frozenset[str] = STATE_FIELDS | {FIELD_CONTAINMENT_LEVEL, FIELD_MEMBERSHIP_LINK}

REASON_NO_SOURCE = "no_source"
REASON_SOURCE_FAILED = "source_failed"

_STATE_RANK = {
    CapabilityState.UNAVAILABLE: 0,
    CapabilityState.DISABLED: 1,
    CapabilityState.DEGRADED: 2,
    CapabilityState.AVAILABLE: 3,
}
_LEVEL_RANK = {EnforcementLevel.NONE: 0, EnforcementLevel.BEST_EFFORT: 1, EnforcementLevel.HARD: 2}
_RUNNING_STATES = frozenset({CapabilityState.AVAILABLE, CapabilityState.DEGRADED})


@dataclass(frozen=True)
class CapabilityContribution:
    """One source's answer for the fields it declared."""

    states: Mapping[str, CapabilityState] = field(default_factory=dict)
    containment_level: Optional[EnforcementLevel] = None
    membership_link: str = ""
    #: Why a state field is not ``available``; reported when this answer is the weakest.
    reasons: Mapping[str, str] = field(default_factory=dict)


CapabilitySource = Callable[[], CapabilityContribution]


@dataclass(frozen=True)
class _Registration:
    fields: frozenset[str]
    source: CapabilitySource


_REGISTRY: dict[str, _Registration] = {}
_REGISTRY_LOCK = threading.Lock()


def register_capability_source(name: str, fields: Iterable[str], source: CapabilitySource) -> None:
    """Register *source* as the answer for *fields*.

    A name registers once; a second registration under the same name is a
    wiring error and raises, so two modules cannot silently fight over a slot.
    """
    declared = frozenset(fields)
    if not name:
        raise ValueError("a capability source needs a name")
    if not declared:
        raise ValueError(f"capability source {name!r} declares no fields")
    unknown = declared - CAPABILITY_FIELDS
    if unknown:
        raise ValueError(f"capability source {name!r} declares unknown fields {sorted(unknown)}")
    with _REGISTRY_LOCK:
        if name in _REGISTRY:
            raise ValueError(f"capability source {name!r} is already registered")
        _REGISTRY[name] = _Registration(declared, source)


def unregister_capability_source(name: str) -> None:
    with _REGISTRY_LOCK:
        _REGISTRY.pop(name, None)


def registered_capability_sources() -> dict[str, frozenset[str]]:
    with _REGISTRY_LOCK:
        return {name: reg.fields for name, reg in _REGISTRY.items()}


def _reset_capability_sources_for_tests() -> None:
    with _REGISTRY_LOCK:
        _REGISTRY.clear()


@dataclass(frozen=True)
class CapabilityReport:
    capabilities: Capabilities
    reasons: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        """The wire body: the frozen fields plus the additive report keys.

        ``setup_paths`` is each shared-access setup path's availability.
        """
        body = self.capabilities.to_dict()
        body["reasons"] = dict(self.reasons)
        body["setup_paths"] = setup_paths_dict()
        direct = tailnet_direct()
        body["tailnet_direct"] = direct.to_dict()
        # Which verified path names people, when attribution is available.
        body["attribution_via"] = "tailscale_direct" if direct.active else ""
        return body


def _weaker_state(a: Optional[CapabilityState], b: CapabilityState) -> CapabilityState:
    if a is None:
        return b
    return a if _STATE_RANK[a] <= _STATE_RANK[b] else b


def aggregate_capabilities() -> CapabilityReport:
    """Combine every registered source into one truthful report."""
    with _REGISTRY_LOCK:
        registrations = list(_REGISTRY.items())

    states: dict[str, CapabilityState] = {}
    level: Optional[EnforcementLevel] = None
    links: set[str] = set()
    link_claimed = False
    reasons: dict[str, str] = {}
    answered: set[str] = set()

    for name, reg in registrations:
        try:
            contribution = reg.source()
            if not isinstance(contribution, CapabilityContribution):
                raise TypeError("capability source returned a non-contribution")
        except Exception:  # noqa: BLE001 -- a failed source claims nothing
            logger.warning("capability source %s failed", name, exc_info=True)
            for f in reg.fields:
                reasons[f] = REASON_SOURCE_FAILED
                if f in STATE_FIELDS:
                    states[f] = CapabilityState.UNAVAILABLE
                elif f == FIELD_CONTAINMENT_LEVEL:
                    level = EnforcementLevel.NONE
                elif f == FIELD_MEMBERSHIP_LINK:
                    link_claimed = True
                    links.add("")
            continue
        for f, value in contribution.states.items():
            if f not in reg.fields or f not in STATE_FIELDS:
                logger.warning("capability source %s answered undeclared field %s", name, f)
                continue
            if not isinstance(value, CapabilityState):
                value = CapabilityState.UNAVAILABLE
            prior = states.get(f)
            if prior is None or _STATE_RANK[value] < _STATE_RANK[prior]:
                # The weakest answer owns the field, and its reason with it.
                reason = contribution.reasons.get(f)
                if value is not CapabilityState.AVAILABLE and isinstance(reason, str) and reason:
                    reasons[f] = reason
                else:
                    reasons.pop(f, None)
            states[f] = _weaker_state(prior, value)
            answered.add(f)
        if FIELD_CONTAINMENT_LEVEL in reg.fields:
            offered = contribution.containment_level or EnforcementLevel.NONE
            if level is None or _LEVEL_RANK[offered] < _LEVEL_RANK[level]:
                level = offered
            answered.add(FIELD_CONTAINMENT_LEVEL)
        if FIELD_MEMBERSHIP_LINK in reg.fields:
            link_claimed = True
            link = contribution.membership_link
            links.add(link if link in MEMBERSHIP_LINKS else "")
            answered.add(FIELD_MEMBERSHIP_LINK)

    for f in CAPABILITY_FIELDS:
        if f not in reasons and f not in answered:
            reasons[f] = REASON_NO_SOURCE

    containment = states.get("containment", CapabilityState.UNAVAILABLE)
    effective_level = level or EnforcementLevel.NONE
    if containment not in _RUNNING_STATES:
        effective_level = EnforcementLevel.NONE
    membership_link = next(iter(links)) if link_claimed and len(links) == 1 else ""

    caps = Capabilities(
        attribution=states.get("attribution", CapabilityState.UNAVAILABLE),
        presence=states.get("presence", CapabilityState.UNAVAILABLE),
        queueing=states.get("queueing", CapabilityState.UNAVAILABLE),
        containment=containment,
        containment_level=effective_level,
        authoritative_directory=states.get("authoritative_directory", CapabilityState.UNAVAILABLE),
        membership_link=membership_link,
        remote_actions=states.get("remote_actions", CapabilityState.UNAVAILABLE),
    )
    return CapabilityReport(capabilities=caps, reasons=reasons)
