"""ACP backend failover: the config policy and the pure next-backend resolver.

This is a LEAF module: it imports nothing from ``kiro_crew.acp`` or the
dashboard, so the spawn admission surface and the config loader can both depend
on it without a cycle. It owns:

* :data:`FAILOVER_TRIGGERS`, the verdicts a failover chain may react to;
* :class:`AcpFailoverConfig`, the parsed ``agent.acp_failover`` block;
* :func:`next_failover_backend`, the chain member to try after one that could
  not start;
* :func:`failover_policy`, the never-raising reader the spawn path uses.

A backend is swapped only when BOTH hold: the operator enabled the block, and a
configured trigger fired. Absent or disabled, the policy is off and every
spawn behaves as it would without this module. The design is the ACP backend
failover RFC (kirodotdev/KiroCrew#27581).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

logger = logging.getLogger(__name__)

#: The verdicts a failover chain may react to: the two refusal codes of the
#: spawn-time probe in ``dashboard/messaging_api/spawn.py``. A spent allowance
#: (``usage_limit``) or a rejected login (``auth``) surfaces only once a run's
#: first request reaches the vendor, and nothing in this phase reads them, so
#: accepting them here would publish a key that does nothing. They join this set
#: in the same change that adds their consumer. Transient faults (``throttle``,
#: ``http_5xx``, ``connection``) never belong here: they are the same-backend
#: retry ladder's job.
FAILOVER_TRIGGERS: tuple[str, ...] = ("backend_not_installed", "backend_restart_required")

#: Accepted spellings that are not active yet, named in the refusal so an
#: operator reading the error learns why rather than guessing a typo.
_NOT_YET_ACTIVE = ("auth", "usage_limit")


@dataclass(frozen=True)
class AcpFailoverConfig:
    """The parsed ``agent.acp_failover`` block.

    ``enabled`` turns the policy on; ``chain`` is the ordered list of backend
    names in the config-file spelling (``"kiro"`` for Kiro, the same spelling a
    ``spawn_run`` ``backend`` uses); ``triggers`` is the subset of
    :data:`FAILOVER_TRIGGERS` that causes a hop. Every field is a tuple of
    strings so the block stays plain data.
    """

    enabled: bool = False
    chain: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()

    @classmethod
    def disabled(cls) -> "AcpFailoverConfig":
        """The off state, used when no block is configured."""
        return cls()

    @classmethod
    def from_raw(cls, raw: object) -> "AcpFailoverConfig":
        """Parse a raw ``agent.acp_failover`` value, raising ``ValueError`` on a bad shape.

        ``chain`` must be a list of distinct non-empty names: a repeated name has
        no defined successor. ``triggers``, when given, must come from
        :data:`FAILOVER_TRIGGERS`; omitted, it is all of them. Whether a chain
        name is selectable is NOT checked here (this leaf has no registry
        import, and the selectable set narrows at runtime), and the spawn path
        skips any name that cannot start.
        """
        if raw is None:
            return cls.disabled()
        if not isinstance(raw, dict):
            raise ValueError(f"agent.acp_failover must be an object, got {type(raw).__name__}")

        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError(f"agent.acp_failover.enabled must be a boolean, got {enabled!r}")

        raw_chain = raw.get("chain", [])
        if not isinstance(raw_chain, list) or any(
            not isinstance(item, str) or not item.strip() for item in raw_chain
        ):
            raise ValueError("agent.acp_failover.chain must be a list of non-empty backend names")
        chain = tuple(item.strip() for item in raw_chain)
        repeated = sorted({name for name in chain if chain.count(name) > 1})
        if repeated:
            raise ValueError(f"agent.acp_failover.chain names {repeated} more than once")

        raw_triggers = raw.get("triggers")
        if raw_triggers is None:
            triggers = FAILOVER_TRIGGERS
        else:
            if not isinstance(raw_triggers, list) or any(
                not isinstance(item, str) for item in raw_triggers
            ):
                raise ValueError("agent.acp_failover.triggers must be a list of strings")
            wanted = {item.strip() for item in raw_triggers}
            unknown = sorted(wanted - set(FAILOVER_TRIGGERS))
            if unknown:
                raise ValueError(
                    f"agent.acp_failover.triggers may only contain {list(FAILOVER_TRIGGERS)};"
                    f" rejected {unknown}. {list(_NOT_YET_ACTIVE)} are not active yet,"
                    " and a transient fault is the same-backend retry ladder's job"
                )
            triggers = tuple(t for t in FAILOVER_TRIGGERS if t in wanted)

        return cls(enabled=enabled, chain=chain, triggers=triggers)


def failover_policy(raw: object) -> AcpFailoverConfig:
    """The policy *raw* describes, or the disabled one when it is invalid.

    Never raises: an invalid block must not refuse a spawn the operator never
    asked to fail over, nor stop the gateway, so it is logged and treated as off.
    """
    try:
        return AcpFailoverConfig.from_raw(raw if raw != {} else None)
    except ValueError as exc:
        logger.warning("agent.acp_failover ignored: %s", exc)
        return AcpFailoverConfig.disabled()


def next_failover_backend(
    *,
    current: str,
    chain: Sequence[str],
    triggers: Sequence[str],
    verdict: str,
) -> str | None:
    """The chain member right after *current*, when *verdict* is a configured trigger.

    ``None`` when there is no hop to make: *verdict* is not a configured trigger
    (or not one this phase consumes), *current* is not in *chain*, or it is the
    last member. One hop per call; the caller walks the chain by index, never
    revisits a member, and stops after ``len(chain)`` hops.
    """
    if verdict not in triggers or verdict not in FAILOVER_TRIGGERS:
        return None
    names = list(chain)
    if current not in names:
        return None
    idx = names.index(current) + 1
    return names[idx] if idx < len(names) else None
