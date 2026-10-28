"""Which of the mutually exclusive People/setup modes this Crew is in.

A Crew is in exactly one mode, and the People surface, the setup guidance and
the session list follow it:

* ``personal``: no team path is configured and nobody but the local operator has
  been seen. The list hides creator bylines and the All/Mine control, and People
  shows the setup guidance plus personal Remote Control;
* ``network``: a network path (Tailscale, Systems Manager) is configured, or a
  person other than the local operator has been seen. People shows each
  network adapter read-only, pointing to where that network manages access, plus
  personal Remote Control;
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

#: Token subjects of the gateway's machine-local bootstrap tokens, minted as
#: ``owner_id or <subject>``: the one local operator when no owner is configured.
LOCAL_BOOTSTRAP_SUBJECTS: tuple[str, ...] = ("local-app", "local-startup")


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
) -> tuple[str, Optional[TeamConfigProblem]]:
    """The Crew's mode and, for ``invalid``, the problem behind it."""
    problem = team_config_problem(network_identity_unreadable=network_identity_unreadable)
    if problem is not None:
        return MODE_INVALID, problem
    if network_configured(setup_paths) or others_seen:
        return MODE_NETWORK, None
    return MODE_PERSONAL, None


def mode_view(
    viewer: Principal,
    observed: Iterable[Principal],
    *,
    setup_paths: Mapping[str, Mapping[str, str]],
    crew_id: str,
    network_identity_unreadable: Optional[bool] = None,
    creators: Iterable[Principal] = (),
    owner_id: str = "",
) -> dict[str, Any]:
    """The ``GET /api/shared-crew/mode`` body.

    *observed* and *creators* are read as :func:`others_observed` reads them.
    ``crew_id`` keys the browser's remembered list filter. ``team_config``
    states whether the team configuration is refused. ``console`` is always
    ``not_offered`` and ``workload_identity_ref`` always ``unavailable`` in
    this build. ``adapters`` reports each network setup path as the gateway
    sees it, and ``systems_manager_attribution`` is always ``unavailable``: a
    Session Manager connection reaches the Crew as a local connection with no
    verified person.
    """
    mode, problem = resolve_mode(
        setup_paths,
        others_seen=others_observed(viewer, observed, creators=creators, owner_id=owner_id),
        network_identity_unreadable=network_identity_unreadable,
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
        "team_config": team,
        "console": {"state": "not_offered", "url": ""},
        "adapters": {k: dict(v) for k, v in setup_paths.items() if isinstance(v, Mapping)},
        "systems_manager_attribution": CapabilityState.UNAVAILABLE.value,
        "workload_identity_ref": WORKLOAD_UNAVAILABLE,
    }


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
    "TeamConfigProblem",
    "WORKLOAD_UNAVAILABLE",
    "mode_view",
    "network_configured",
    "operator_ids",
    "others_observed",
    "resolve_mode",
    "team_config_problem",
    "validate_at_startup",
]
