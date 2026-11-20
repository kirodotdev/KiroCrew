"""Which People/setup mode this Crew is in, and each connection source it has.

A Crew is in exactly one mode, which the session list and the error state
follow, and it reports every connection source at once (:func:`sources_view`),
which the People page shows side by side:

* ``personal``: no team path is configured and nobody but the local operator has
  been seen. The list hides creator bylines and the All/Mine control;
* ``network``: a network path (Tailscale or SSM) is configured, this Crew is
  shared as a team Crew over the Kiro relay, or a person other than the local
  operator has been seen;
* ``invalid``: tailnet identity trust is configured but its login allowlist
  could not be read. Team mode is refused (:func:`team_config_problem`, enforced
  by ``lifecycle.team_config_middleware``) and the UI shows an error, never the
  personal surface.

Detection happens at gateway start (:func:`validate_at_startup` logs the
machine-readable reason) and again on each read, from state already in memory.

Nothing here grants or removes access. Observed people are observations, not
members, and no mode invents a membership list.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Optional

from kiro_crew.shared_crew.access import tailnet_identity_config_unreadable
from kiro_crew.shared_crew.codec import SCHEMA_VERSION
from kiro_crew.shared_crew.contracts import CapabilityState, Principal, PrincipalKind, Provenance
from kiro_crew.shared_crew.identity import PROVIDER_LOCAL, local_operator_principal
from kiro_crew.shared_crew.owner import LOCAL_BOOTSTRAP_SUBJECTS, writes_as_ids

logger = logging.getLogger(__name__)

MODE_PERSONAL = "personal"
MODE_NETWORK = "network"
MODE_INVALID = "invalid"
MODES: tuple[str, ...] = (MODE_PERSONAL, MODE_NETWORK, MODE_INVALID)

TEAM_CONFIG_NONE = "not_configured"
TEAM_CONFIG_INVALID = "invalid"

#: Tailnet identity trust is configured but its login allowlist could not be read.
REASON_NETWORK_IDENTITY_CONFIG_UNREADABLE = "network_identity_config_unreadable"

#: The error code a refused shared-access request carries while the team
#: configuration is invalid.
ERROR_TEAM_CONFIG_INVALID = "team_config_invalid"

_CONFIGURED_STATES = frozenset({CapabilityState.AVAILABLE.value, CapabilityState.DEGRADED.value})

#: The ``workload_identity_ref`` the mode body and audit records carry: this
#: build has no Crew workload identity.
WORKLOAD_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class TeamConfigProblem:
    """Why a team configuration is refused."""

    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"reason": self.reason, "capability": "", "cause": ""}


def network_configured(setup_paths: Mapping[str, Mapping[str, str]]) -> bool:
    """Whether any network sharing path is on (available or degraded)."""
    return any(
        str(row.get("state", "")) in _CONFIGURED_STATES
        for row in setup_paths.values()
        if isinstance(row, Mapping)
    )


def team_config_problem(
    *, network_identity_unreadable: Optional[bool] = None
) -> Optional[TeamConfigProblem]:
    """Why the configured team mode is unusable, or ``None`` when it is not refused."""
    if network_identity_unreadable is None:
        network_identity_unreadable = tailnet_identity_config_unreadable()
    if network_identity_unreadable:
        return TeamConfigProblem(REASON_NETWORK_IDENTITY_CONFIG_UNREADABLE)
    return None


def operator_ids(viewer: Principal, owner_id: str = "") -> frozenset[str]:
    """The principal ids that name *viewer* themself.

    The viewer's own id, and, for a local operator holding a machine-local
    bootstrap subject (:data:`LOCAL_BOOTSTRAP_SUBJECTS`) or the configured
    *owner_id*, the ids of those subjects too: they are the one operator of
    this machine signing in through different local tokens. Derived from the
    viewer the auth middleware published, never from a stored record.
    """
    if viewer.is_unknown:
        return frozenset()
    ids = {viewer.id}
    if viewer.provenance is Provenance.LOCAL_OPERATOR and viewer.provider == PROVIDER_LOCAL:
        subjects = set(LOCAL_BOOTSTRAP_SUBJECTS)
        owner = owner_id.strip()
        if owner:
            subjects.add(owner)
        if viewer.subject in subjects:
            ids.update(local_operator_principal(subject).id for subject in subjects)
    return frozenset(ids)


def others_observed(
    viewer: Principal,
    observed: Iterable[Principal],
    *,
    creators: Iterable[Principal] = (),
    owner_id: str = "",
) -> bool:
    """Whether anyone but the local operator viewing now has been seen.

    *observed* holds principals the server derived (their provenance is
    trusted). A person who reached the Crew over a network path counts, the
    viewer included, because their being here means a team path exists.

    *creators* are session creators read back from public attribution, which
    carries no provenance. One counts unless its id is one of
    :func:`operator_ids`; a stored record never makes anyone the operator.
    """
    mine = operator_ids(viewer, owner_id)
    for principal in observed:
        if principal.kind in (PrincipalKind.UNKNOWN, PrincipalKind.SYSTEM):
            continue
        if principal.provenance is not Provenance.LOCAL_OPERATOR:
            return True
        if mine and principal.id not in mine:
            return True
    for creator in creators:
        if creator.kind in (PrincipalKind.UNKNOWN, PrincipalKind.SYSTEM):
            continue
        if creator.id not in mine:
            return True
    return False


def resolve_mode(
    setup_paths: Mapping[str, Mapping[str, str]],
    *,
    others_seen: bool,
    network_identity_unreadable: Optional[bool] = None,
    relay_team: bool = False,
) -> tuple[str, Optional[TeamConfigProblem]]:
    """The Crew's mode and, for ``invalid``, the problem behind it.

    *relay_team* says this Crew is shared as a team Crew over the Kiro relay,
    which is a team path of its own.
    """
    problem = team_config_problem(network_identity_unreadable=network_identity_unreadable)
    if problem is not None:
        return MODE_INVALID, problem
    if network_configured(setup_paths) or relay_team or others_seen:
        return MODE_NETWORK, None
    return MODE_PERSONAL, None


#: The connection sources, in the order the People page shows them.
SOURCE_TAILSCALE = "tailscale"
SOURCE_SSM = "ssm"
SOURCE_ORGANIZATION = "organization"
SOURCE_KIRO_LOGIN = "kiro_login"
SOURCES: tuple[str, ...] = (SOURCE_TAILSCALE, SOURCE_SSM, SOURCE_ORGANIZATION, SOURCE_KIRO_LOGIN)

_UNAVAILABLE = CapabilityState.UNAVAILABLE.value


def sources_view(
    setup_paths: Mapping[str, Mapping[str, str]],
    observed: Iterable[Principal],
    *,
    relay_team: bool = False,
    console_offered: bool = False,
) -> list[dict[str, Any]]:
    """Every connection source at once: ``{id, state, reason, people}`` each.

    Tailscale and SSM report their setup path's state. ``organization`` is
    ``available`` only while an organization console manages access
    (*console_offered*, never in this build). ``kiro_login`` is ``available``
    while this Crew is shared as a team Crew over the Kiro relay (*relay_team*).

    ``people`` counts the distinct principals this process saw come in through
    the source, read from the provenance the server derived (:func:`viewer_via`).
    A person the Tailscale daemon lists who never connected has not come in, and
    a session creator carries no provenance, so neither is counted. Display only.
    """
    counted: dict[str, set[str]] = {source: set() for source in SOURCES}
    for principal in observed:
        if principal.kind in (PrincipalKind.UNKNOWN, PrincipalKind.SYSTEM):
            continue
        via = viewer_via(principal)
        if via in counted:
            counted[via].add(principal.id)

    def _path(name: str) -> tuple[str, str]:
        row = setup_paths.get(name)
        if not isinstance(row, Mapping):
            return _UNAVAILABLE, "not_configured"
        return str(row.get("state", "") or _UNAVAILABLE), str(row.get("reason", "") or "")

    states = {
        SOURCE_TAILSCALE: _path(SOURCE_TAILSCALE),
        SOURCE_SSM: _path(SOURCE_SSM),
        SOURCE_ORGANIZATION: (
            (CapabilityState.AVAILABLE.value, "")
            if console_offered
            else (_UNAVAILABLE, "not_offered")
        ),
        SOURCE_KIRO_LOGIN: (
            (CapabilityState.AVAILABLE.value, "")
            if relay_team
            else (_UNAVAILABLE, "not_configured")
        ),
    }
    return [
        {
            "id": source,
            "state": states[source][0],
            "reason": states[source][1],
            "people": len(counted[source]),
        }
        for source in SOURCES
    ]


def mode_view(
    viewer: Principal,
    observed: Iterable[Principal],
    *,
    setup_paths: Mapping[str, Mapping[str, str]],
    crew_id: str,
    network_identity_unreadable: Optional[bool] = None,
    creators: Iterable[Principal] = (),
    owner_id: str = "",
    owner_label: str = "",
    relay_team: bool = False,
) -> dict[str, Any]:
    """The ``GET /api/shared-crew/mode`` body.

    *observed* and *creators* are read as :func:`others_observed` reads them.
    ``crew_id`` keys the browser's remembered list filter. ``team_config``
    states whether the team configuration is refused. ``console`` is always
    ``not_offered`` and ``workload_identity_ref`` always ``unavailable`` in
    this build. ``adapters`` reports each network setup path as the gateway
    sees it.

    ``viewer_via`` names the path the viewer came in on (:func:`viewer_via`) and
    ``owner_label`` the owner's name (``owner.owner_name``), so a teammate's
    surfaces can say whom to ask. ``sources`` lists every connection source at
    once (:func:`sources_view`; *relay_team* is the Kiro relay's). Display only:
    none of these grants or refuses anything.
    """
    observed = list(observed)
    mode, problem = resolve_mode(
        setup_paths,
        others_seen=others_observed(viewer, observed, creators=creators, owner_id=owner_id),
        network_identity_unreadable=network_identity_unreadable,
        relay_team=relay_team,
    )
    if problem is not None:
        team = {"state": TEAM_CONFIG_INVALID, **problem.to_dict()}
    else:
        team = {"state": TEAM_CONFIG_NONE, "reason": "", "capability": "", "cause": ""}
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "crew_id": crew_id,
        "viewer": viewer.to_public_dict(),
        # Every principal id the viewer writes as (``owner.writes_as_ids``): the
        # owner is one person under each id in ``owner.owner_principal_ids``,
        # anyone else has only their own.
        "viewer_ids": sorted(writes_as_ids(viewer, owner_id)),
        "team_config": team,
        "console": {"state": "not_offered", "url": ""},
        "adapters": {k: dict(v) for k, v in setup_paths.items() if isinstance(v, Mapping)},
        "sources": sources_view(setup_paths, observed, relay_team=relay_team),
        "workload_identity_ref": WORKLOAD_UNAVAILABLE,
        "viewer_via": viewer_via(viewer),
        "owner_label": owner_label,
    }


#: The ``viewer_via`` value for each provenance a person can arrive with.
_VIA_BY_PROVENANCE = {
    Provenance.KIRO_RELAY: "kiro_login",
    Provenance.TAILSCALE_SERVE: "tailscale",
    Provenance.TAILSCALE_DIRECT: "tailscale",
    Provenance.TAILSCALE_LISTENER: "tailscale",
    Provenance.SSM_INVITE: "ssm",
    Provenance.LOCAL_OPERATOR: "local",
}


def viewer_via(viewer: Principal) -> str:
    """The path *viewer* reached this Crew on: ``kiro_login``, ``tailscale``,
    ``ssm``, ``local`` or ``unknown``."""
    return _VIA_BY_PROVENANCE.get(viewer.provenance, "unknown")


def validate_at_startup() -> Optional[TeamConfigProblem]:
    """Check the team configuration once at gateway start and log a refusal.

    Returns the problem, or ``None``. A refused configuration keeps the gateway
    running for the local operator, who sees the error state in People.
    """
    problem = team_config_problem()
    if problem is not None:
        logger.error(
            "shared crew team configuration refused: %s; "
            "shared access is refused until the configuration is fixed",
            problem.reason,
        )
    return problem


__all__ = [
    "ERROR_TEAM_CONFIG_INVALID",
    "LOCAL_BOOTSTRAP_SUBJECTS",
    "MODES",
    "MODE_INVALID",
    "MODE_NETWORK",
    "MODE_PERSONAL",
    "SOURCES",
    "TeamConfigProblem",
    "WORKLOAD_UNAVAILABLE",
    "mode_view",
    "network_configured",
    "viewer_via",
    "operator_ids",
    "others_observed",
    "resolve_mode",
    "sources_view",
    "team_config_problem",
    "validate_at_startup",
]
