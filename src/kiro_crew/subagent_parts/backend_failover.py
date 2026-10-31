"""ACP backend failover: the config policy and the pure next-backend resolver.

This is a LEAF module — it imports nothing from ``kiro_crew.acp`` or the
dashboard, so the spawn admission surface and the config loader can both depend
on it without a cycle. It owns three things and nothing else:

* :data:`FAILOVER_TRIGGERS` — the terminal availability verdicts a failover
  chain may react to. The set is deliberately small and TERMINAL-ONLY: every
  entry is a condition that the SAME backend reproduces on a retry (a spent
  allowance, a rejected login, a backend that cannot start), so switching to a
  sibling backend is the only recovery. Transient verdicts (``throttle``,
  ``http_5xx``, ``connection``) are NOT here, by design: those are the
  same-backend retry ladder's job, and failing over on one would both race that
  ladder and move spend to another vendor for a fault that clears on its own.
* :class:`AcpFailoverConfig` — the parsed ``agent.acp_failover`` block.
* :func:`next_failover_backend` — a pure function from (current backend, chain,
  configured triggers, observed verdict) to the next backend to try, or ``None``
  when there is no failover to make.

The policy is opt-in and off by default: an absent or disabled block yields a
config whose :attr:`AcpFailoverConfig.enabled` is ``False``, and
:func:`next_failover_backend` returns ``None`` for it, so every spawn behaves
exactly as it did before this module existed.

The RFC this implements (ACP Backend Failover) is kept outside the repo; the
behaviour is specified in the subagent module spec.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

#: The verdicts a failover chain may react to. Each is a TERMINAL availability
#: condition — reproduced by the same backend on retry — so the only recovery is
#: a different backend. Two come from the spawn-time probe refusal codes
#: (``/api/spawn`` in ``dashboard/messaging_api/spawn.py``) and two from the
#: terminal verdicts the error taxonomy already names
#: (``acp.transport_errors.classify_provider_error`` /
#: ``AcpError.usage_limit`` / ``AcpError.auth_required``). This module does NOT
#: detect any of them; it only decides what to do once a caller has classified
#: one. Keeping the authority here means a config file naming an ineligible
#: trigger is rejected at load (see :meth:`AcpFailoverConfig.from_raw`), not
#: silently ignored at spawn time.
FAILOVER_TRIGGERS: frozenset[str] = frozenset(
    {
        "usage_limit",
        "auth",
        "backend_not_installed",
        "backend_restart_required",
    }
)

#: The default triggers when a block is enabled but names none. The full
#: terminal set: an operator who turns failover on without narrowing it gets
#: failover for every availability-terminal reason, which is the useful default.
_DEFAULT_TRIGGERS: frozenset[str] = FAILOVER_TRIGGERS


@dataclass(frozen=True)
class AcpFailoverConfig:
    """The parsed ``agent.acp_failover`` block.

    ``enabled`` is the master switch; ``chain`` is the ordered list of EXPLICIT
    backend ids to try (each validated against the selectable set by the config
    loader, not here — this dataclass is pure and does not import the registry);
    ``triggers`` is the subset of :data:`FAILOVER_TRIGGERS` that causes a hop.

    An absent block is :meth:`disabled`, whose :func:`next_failover_backend`
    always answers ``None``.
    """

    enabled: bool = False
    chain: tuple[str, ...] = ()
    triggers: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def disabled(cls) -> "AcpFailoverConfig":
        """The off state — the default when no block is configured."""
        return cls(enabled=False, chain=(), triggers=frozenset())

    @classmethod
    def from_raw(cls, raw: object) -> "AcpFailoverConfig":
        """Parse a raw ``agent.acp_failover`` value, rejecting an invalid shape.

        Rules, each a :class:`ValueError` on violation so a bad block fails the
        config load with a named reason rather than silently disabling failover:

        * a non-mapping (and a value that is not a mapping) is a config error;
        * ``enabled`` must be a bool when present;
        * ``chain`` must be a list of non-empty strings when present;
        * ``triggers``, when present, must be a list of strings drawn ENTIRELY
          from :data:`FAILOVER_TRIGGERS` — a trigger outside the terminal set
          (e.g. ``"throttle"``) is rejected, because failing over on a transient
          fault is exactly what this policy must not do.

        Backend-id spelling and selectable-set membership are NOT checked here:
        this module is a pure leaf with no registry import, and the config
        loader validates the chain ids against ``selectable_backend_values()``
        before constructing this object. An absent/empty block yields
        :meth:`disabled`.
        """
        if raw is None:
            return cls.disabled()
        if isinstance(raw, cls):
            # The config reader hands back the field default (a disabled
            # instance) when the section omits the key; pass an already-parsed
            # value through unchanged instead of re-parsing it as a mapping.
            return raw
        if not isinstance(raw, dict):
            raise ValueError(f"agent.acp_failover must be an object, got {type(raw).__name__}")

        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError(f"agent.acp_failover.enabled must be a boolean, got {enabled!r}")

        raw_chain = raw.get("chain", [])
        if not isinstance(raw_chain, list) or any(
            not isinstance(item, str) or not item.strip() for item in raw_chain
        ):
            raise ValueError("agent.acp_failover.chain must be a list of non-empty backend ids")
        chain = tuple(item.strip() for item in raw_chain)

        raw_triggers = raw.get("triggers", None)
        if raw_triggers is None:
            triggers = set(_DEFAULT_TRIGGERS)
        else:
            if not isinstance(raw_triggers, list) or any(
                not isinstance(item, str) for item in raw_triggers
            ):
                raise ValueError("agent.acp_failover.triggers must be a list of strings")
            triggers = {item.strip() for item in raw_triggers}
            unknown = triggers - FAILOVER_TRIGGERS
            if unknown:
                raise ValueError(
                    "agent.acp_failover.triggers may only contain terminal "
                    f"availability verdicts {sorted(FAILOVER_TRIGGERS)}; "
                    f"rejected {sorted(unknown)} (a transient fault is the "
                    "same-backend retry ladder's job, not failover's)"
                )

        return cls(enabled=enabled, chain=chain, triggers=frozenset(triggers))


def next_failover_backend(
    *,
    current: str,
    chain: Sequence[str],
    triggers: frozenset[str],
    verdict: str,
) -> str | None:
    """The next backend to try after *current* failed with *verdict*, or ``None``.

    Pure. Returns a backend id when ALL of the following hold, else ``None``
    ("no failover — surface the original failure"):

    * *verdict* is one of the configured *triggers* (and, defensively, one of
      :data:`FAILOVER_TRIGGERS` — a caller cannot smuggle a non-terminal verdict
      in by listing it as a trigger, because a config carrying one is rejected at
      load, and this re-checks);
    * *current* appears in *chain*, and there is a later member;
    * a later member exists to return.

    The returned id is the chain member immediately AFTER *current*'s FIRST
    occurrence. The caller is responsible for probing that member and, if it too
    is unavailable, calling again with it as *current* to walk the chain — this
    function makes one hop, so the walk is bounded by the caller to the chain
    length and can never loop.

    When *current* is not in the chain at all, the result is ``None``: the chain
    describes an ordered preference among its own members, and a backend outside
    it has no defined successor. (The caller starts the walk from a chain member,
    so this guards only a misconfiguration.)
    """
    if verdict not in triggers or verdict not in FAILOVER_TRIGGERS:
        return None
    try:
        idx = list(chain).index(current)
    except ValueError:
        return None
    rest = list(chain)[idx + 1 :]
    return rest[0] if rest else None
