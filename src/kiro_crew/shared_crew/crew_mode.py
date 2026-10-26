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
* ``console``: an enterprise provider backs human credentials and individual
  grants. People shows a read-only Team Access list and, when the provider
  offers one that validates, a Console link. No share, Remote Control or
  Tailscale setup surface;
* ``invalid``: a team configuration is present but unusable. Team mode is
  refused (:func:`team_config_problem`, enforced by the enterprise gate) and the
  UI shows an error, never the personal surface.

Detection happens at gateway start (:func:`validate_at_startup` logs the
machine-readable reason) and again on each read, from state already in memory.

Nothing here grants or removes access. Observed people are observations, not
members, and no mode invents a membership list.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

from kiro_crew.shared_crew.access import tailnet_identity_config_unreadable
from kiro_crew.shared_crew.codec import SCHEMA_VERSION
from kiro_crew.shared_crew.contracts import (
    CapabilityState,
    Principal,
    PrincipalKind,
    Provenance,
    is_channel_principal,
)
from kiro_crew.shared_crew.enterprise import (
    CAP_HUMAN_CREDENTIALS,
    CAP_HUMAN_SIGN_IN,
    CAP_INDIVIDUAL_GRANTS,
    ENTERPRISE_CAPABILITY_KEYS,
    EnterpriseSeams,
    UnavailableReason,
)
from kiro_crew.shared_crew.identity import PROVIDER_LOCAL, local_operator_principal

logger = logging.getLogger(__name__)

MODE_PERSONAL = "personal"
MODE_NETWORK = "network"
MODE_CONSOLE = "console"
MODE_INVALID = "invalid"
MODES: tuple[str, ...] = (MODE_PERSONAL, MODE_NETWORK, MODE_CONSOLE, MODE_INVALID)

TEAM_CONFIG_NONE = "not_configured"
TEAM_CONFIG_VALID = "valid"
TEAM_CONFIG_INVALID = "invalid"

#: The enterprise provider is composed but none of its slots can be used.
REASON_CONSOLE_PROVIDER_UNUSABLE = "console_provider_unusable"
#: The enterprise provider is composed but a capability team access needs is missing.
REASON_CONSOLE_CAPABILITY_MISSING = "console_capability_missing"
#: A Console team and a network sharing path are both configured.
REASON_CONFLICTING_TEAM_MODES = "conflicting_team_modes"
#: Tailnet identity trust is configured but its login allowlist could not be read.
REASON_NETWORK_IDENTITY_CONFIG_UNREADABLE = "network_identity_config_unreadable"
TEAM_CONFIG_REASONS: tuple[str, ...] = (
    REASON_CONSOLE_PROVIDER_UNUSABLE,
    REASON_CONSOLE_CAPABILITY_MISSING,
    REASON_CONFLICTING_TEAM_MODES,
    REASON_NETWORK_IDENTITY_CONFIG_UNREADABLE,
)

#: The error code a refused shared-access request carries while the team
#: configuration is invalid.
ERROR_TEAM_CONFIG_INVALID = "team_config_invalid"

#: Either sign-in seam: a presented-credential verifier or the relying-party sign-in.
SIGN_IN_KEYS: tuple[str, ...] = (CAP_HUMAN_CREDENTIALS, CAP_HUMAN_SIGN_IN)
#: A Console team needs one sign-in seam from :data:`SIGN_IN_KEYS` plus these.
CONSOLE_REQUIRED: tuple[str, ...] = (CAP_INDIVIDUAL_GRANTS,)

_UNUSABLE_CAUSES = frozenset(
    {UnavailableReason.PROVIDER_FAILED.value, UnavailableReason.TEST_PROVIDER_REFUSED.value}
)
_CONFIGURED_STATES = frozenset({CapabilityState.AVAILABLE.value, CapabilityState.DEGRADED.value})

#: Console link answers.
LINK_AVAILABLE = "available"
LINK_NOT_OFFERED = "not_offered"
LINK_INVALID = "invalid"
_LINK_MAX = 512

#: The ``workload_identity_ref`` an audit record carries when the Crew has none.
WORKLOAD_UNAVAILABLE = "unavailable"

#: Token subjects of the gateway's machine-local bootstrap tokens, minted as
#: ``owner_id or <subject>``: the one local operator when no owner is configured.
LOCAL_BOOTSTRAP_SUBJECTS: tuple[str, ...] = ("local-app", "local-startup")


@dataclass(frozen=True)
class TeamConfigProblem:
    """Why a team configuration is refused."""

    reason: str
    capability: str = ""
    cause: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"reason": self.reason, "capability": self.capability, "cause": self.cause}


def _cap(seams: EnterpriseSeams, key: str) -> tuple[str, str]:
    row = seams.capabilities_dict().get(key, {})
    return str(row.get("state", "")), str(row.get("reason", ""))


def console_composed(seams: EnterpriseSeams) -> bool:
    """Whether an enabled enterprise provider was composed at all."""
    return bool(seams.capabilities)


def _sign_in_available(seams: EnterpriseSeams) -> bool:
    return any(_cap(seams, key)[0] == CapabilityState.AVAILABLE.value for key in SIGN_IN_KEYS)


def console_ready(seams: EnterpriseSeams) -> bool:
    """Whether the provider backs a sign-in path and per-person grants."""
    return (
        console_composed(seams)
        and _sign_in_available(seams)
        and _cap(seams, CAP_INDIVIDUAL_GRANTS)[0] == CapabilityState.AVAILABLE.value
    )


def network_configured(setup_paths: Mapping[str, Mapping[str, str]]) -> bool:
    """Whether any network sharing path is on (available or degraded)."""
    return any(
        str(row.get("state", "")) in _CONFIGURED_STATES
        for row in setup_paths.values()
        if isinstance(row, Mapping)
    )


def team_config_problem(
    seams: EnterpriseSeams,
    setup_paths: Mapping[str, Mapping[str, str]],
    *,
    network_identity_unreadable: Optional[bool] = None,
) -> Optional[TeamConfigProblem]:
    """Why the configured team mode is unusable, or ``None`` when it is not refused."""
    if network_identity_unreadable is None:
        network_identity_unreadable = tailnet_identity_config_unreadable()
    if network_identity_unreadable:
        return TeamConfigProblem(REASON_NETWORK_IDENTITY_CONFIG_UNREADABLE)
    if not console_composed(seams):
        return None
    causes = [_cap(seams, key)[1] for key in ENTERPRISE_CAPABILITY_KEYS]
    states = [_cap(seams, key)[0] for key in ENTERPRISE_CAPABILITY_KEYS]
    if CapabilityState.AVAILABLE.value not in states and _UNUSABLE_CAUSES & set(causes):
        cause = next(c for c in causes if c in _UNUSABLE_CAUSES)
        return TeamConfigProblem(REASON_CONSOLE_PROVIDER_UNUSABLE, cause=cause)
    for key in (*SIGN_IN_KEYS, CAP_INDIVIDUAL_GRANTS):
        state, reason = _cap(seams, key)
        if state == CapabilityState.AVAILABLE.value:
            continue
        # A slot the provider left empty on purpose is no Console team yet, unless
        # sign-in is on without per-person grants, which would admit everyone
        # who can sign in. A slot that failed or was refused is a broken team.
        broken = reason != UnavailableReason.NOT_CONFIGURED.value
        signin_without_grants = key == CAP_INDIVIDUAL_GRANTS and _sign_in_available(seams)
        if broken or signin_without_grants:
            return TeamConfigProblem(
                REASON_CONSOLE_CAPABILITY_MISSING, capability=key, cause=reason
            )
    if console_ready(seams) and network_configured(setup_paths):
        return TeamConfigProblem(REASON_CONFLICTING_TEAM_MODES)
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

    A channel sender (a Slack person) counts in neither: the channel's own
    allowlist admitted them, so their being attributed says nothing about a
    Shared Crew path, and a personal Crew stays personal.
    """
    mine = operator_ids(viewer, owner_id)
    for principal in observed:
        if principal.kind in (PrincipalKind.UNKNOWN, PrincipalKind.SYSTEM):
            continue
        if is_channel_principal(principal):
            continue
        if principal.provenance is not Provenance.LOCAL_OPERATOR:
            return True
        if mine and principal.id not in mine:
            return True
    for creator in creators:
        if creator.kind in (PrincipalKind.UNKNOWN, PrincipalKind.SYSTEM):
            continue
        if is_channel_principal(creator):
            continue
        if creator.id not in mine:
            return True
    return False


def resolve_mode(
    seams: EnterpriseSeams,
    setup_paths: Mapping[str, Mapping[str, str]],
    *,
    others_seen: bool,
    network_identity_unreadable: Optional[bool] = None,
) -> tuple[str, Optional[TeamConfigProblem]]:
    """The Crew's mode and, for ``invalid``, the problem behind it."""
    problem = team_config_problem(
        seams, setup_paths, network_identity_unreadable=network_identity_unreadable
    )
    if problem is not None:
        return MODE_INVALID, problem
    if console_ready(seams):
        return MODE_CONSOLE, None
    if network_configured(setup_paths) or others_seen:
        return MODE_NETWORK, None
    return MODE_PERSONAL, None


def validate_console_link(raw: object) -> str:
    """*raw* as a Console link the UI may open, or ``""`` when it does not validate.

    An absolute ``https`` URL to a named host on the default port, with no
    credentials, no whitespace and no control characters. An address literal or
    a single-label host is refused, so a link can never point at a local service.
    """
    if not isinstance(raw, str) or not raw or len(raw) > _LINK_MAX:
        return ""
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return ""
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return ""
    host = (parts.hostname or "").rstrip(".")
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return ""
    if port not in (None, 443) or "." not in host or host.endswith(".localhost"):
        return ""
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return raw
    return ""


def _console_provider() -> Optional[object]:
    from kiro_crew.platform import safe_context_call
    from kiro_crew.platform.context import current_context

    return safe_context_call(
        lambda: current_context().shared_crew_enterprise,
        fallback=None,
        log_message="shared crew enterprise seam unreadable; no console link",
    )


def console_link(provider: Optional[object] = None) -> tuple[str, str]:
    """``(state, url)`` for the provider's Console link.

    Read from the composed enterprise provider's optional ``console_link()``
    and validated by :func:`validate_console_link`; a test-only provider never
    supplies one. Never a URL from config, a request or a peer.
    """
    source = _console_provider() if provider is None else provider
    if source is None:
        return LINK_NOT_OFFERED, ""
    if provider is None and getattr(source, "TEST_ONLY", False) is True:
        return LINK_NOT_OFFERED, ""
    reader = getattr(source, "console_link", None)
    if not callable(reader):
        return LINK_NOT_OFFERED, ""
    try:
        raw = reader()
    except Exception:  # noqa: BLE001 -- a failed read offers no link
        logger.warning("shared crew console link unreadable", exc_info=True)
        return LINK_INVALID, ""
    if raw in (None, ""):
        return LINK_NOT_OFFERED, ""
    url = validate_console_link(raw)
    return (LINK_AVAILABLE, url) if url else (LINK_INVALID, "")


def crew_identity_refs(seams: EnterpriseSeams) -> tuple[str, str]:
    """``(crew_id, workload_identity_ref)`` from the Crew binding.

    ``("", "unavailable")`` when no binding is composed or it names none.
    """
    if seams.crew_binding is None:
        return "", WORKLOAD_UNAVAILABLE
    try:
        binding = seams.crew_binding.current_binding()
    except Exception:  # noqa: BLE001 -- an unreadable binding names no workload
        logger.info("crew binding unreadable", exc_info=True)
        return "", WORKLOAD_UNAVAILABLE
    if binding is None:
        return "", WORKLOAD_UNAVAILABLE
    return binding.crew.crew_id, binding.crew.workload_identity_ref or WORKLOAD_UNAVAILABLE


def current_workload_identity_ref() -> str:
    """The Crew's workload identity reference now, or ``unavailable``. Never raises."""
    try:
        from kiro_crew.shared_crew.enterprise import resolve_enterprise

        return crew_identity_refs(resolve_enterprise())[1]
    except Exception:  # noqa: BLE001 -- an unreadable seam has no workload
        logger.debug("workload identity reference unreadable", exc_info=True)
        return WORKLOAD_UNAVAILABLE


def mode_view(
    viewer: Principal,
    observed: Iterable[Principal],
    *,
    seams: EnterpriseSeams,
    setup_paths: Mapping[str, Mapping[str, str]],
    crew_id: str,
    provider: Optional[object] = None,
    network_identity_unreadable: Optional[bool] = None,
    creators: Iterable[Principal] = (),
    owner_id: str = "",
) -> dict[str, Any]:
    """The ``GET /api/shared-crew/mode`` body.

    *observed* and *creators* are read as :func:`others_observed` reads them.
    ``crew_id`` keys the browser's remembered list filter. ``team_config``
    states whether a team configuration exists and whether it is refused.
    ``console`` carries a validated link only in ``console`` mode.
    ``adapters`` reports each network setup path as the gateway sees it, and
    ``systems_manager_attribution`` is always ``unavailable``: a Session Manager
    connection reaches the Crew as a local connection with no verified person.
    """
    mode, problem = resolve_mode(
        seams,
        setup_paths,
        others_seen=others_observed(viewer, observed, creators=creators, owner_id=owner_id),
        network_identity_unreadable=network_identity_unreadable,
    )
    if problem is not None:
        team = {"state": TEAM_CONFIG_INVALID, **problem.to_dict()}
    elif mode == MODE_CONSOLE:
        team = {"state": TEAM_CONFIG_VALID, "reason": "", "capability": "", "cause": ""}
    else:
        team = {"state": TEAM_CONFIG_NONE, "reason": "", "capability": "", "cause": ""}
    link_state, link = (LINK_NOT_OFFERED, "")
    if mode == MODE_CONSOLE:
        link_state, link = console_link(provider)
    bound_crew, workload_ref = crew_identity_refs(seams)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "crew_id": bound_crew or crew_id,
        "viewer": viewer.to_public_dict(),
        "team_config": team,
        "console": {"state": link_state, "url": link},
        "adapters": {k: dict(v) for k, v in setup_paths.items() if isinstance(v, Mapping)},
        "systems_manager_attribution": CapabilityState.UNAVAILABLE.value,
        "workload_identity_ref": workload_ref,
    }


def validate_at_startup(
    seams: Optional[EnterpriseSeams] = None,
    setup_paths: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> Optional[TeamConfigProblem]:
    """Check the team configuration once at gateway start and log a refusal.

    Returns the problem, or ``None``. A refused configuration keeps the gateway
    running for the local operator, who sees the error state in People.
    """
    if seams is None:
        from kiro_crew.shared_crew.enterprise import resolve_enterprise

        seams = resolve_enterprise()
    if setup_paths is None:
        from kiro_crew.shared_crew.access import setup_paths_dict

        setup_paths = setup_paths_dict()
    problem = team_config_problem(seams, setup_paths)
    if problem is not None:
        logger.error(
            "shared crew team configuration refused: %s (capability=%s cause=%s); "
            "shared access is refused until the configuration is fixed",
            problem.reason,
            problem.capability or "-",
            problem.cause or "-",
        )
    return problem


__all__ = [
    "CONSOLE_REQUIRED",
    "ERROR_TEAM_CONFIG_INVALID",
    "LOCAL_BOOTSTRAP_SUBJECTS",
    "MODES",
    "MODE_CONSOLE",
    "MODE_INVALID",
    "MODE_NETWORK",
    "MODE_PERSONAL",
    "TEAM_CONFIG_REASONS",
    "TeamConfigProblem",
    "WORKLOAD_UNAVAILABLE",
    "console_link",
    "console_ready",
    "crew_identity_refs",
    "current_workload_identity_ref",
    "mode_view",
    "network_configured",
    "operator_ids",
    "others_observed",
    "resolve_mode",
    "team_config_problem",
    "validate_at_startup",
    "validate_console_link",
]
