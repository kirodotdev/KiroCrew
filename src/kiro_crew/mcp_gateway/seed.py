"""Turn cached verdicts into config, once per server, at start.

Two decisions shape this module, and both are about not surprising the operator.

**It applies at start, never mid-run.** Nothing changes under a live session, so
the failure this whole feature exists to prevent — a chat losing a server's tools
because its backend was recycled — cannot happen as a side effect of seeding. It
also means no live re-apply path is needed: ``refresh_defaults`` deliberately
does not retrofit running sessions anyway.

**It writes the value into config rather than acting on it implicitly.** The MCP
Management page renders ``mcp_gateway.stub_servers``, so materialising the
verdict there is what makes the row's toggle show the real state. The operator
sees what was decided, in a file they own, and can switch it off — which is a
different thing from a gateway that quietly routes differently than the page
claims.

**And it applies to each server exactly once.** After the first seed the config
is the operator's. A later start that found the same recommendation must not
re-flip a switch they turned off; the applied marker in ``verdict_cache`` is what
makes "off" stick.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kiro_crew.mcp_gateway.verdict_cache import VerdictCache

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeedPlan:
    """What seeding would change. Empty is the steady state, not a failure."""

    #: Server names to add to ``mcp_gateway.stub_servers``.
    add_stub: tuple[str, ...] = ()
    #: Names seeded this pass, to be marked applied once the write succeeds.
    mark_applied: tuple[str, ...] = ()
    #: Whether any recommendation asked for sharing. Reported so a caller can
    #: decide about ``mcp_gateway.enabled`` separately — this module never turns
    #: the global sharing switch on by itself, because that is a topology change
    #: over servers the operator may have stubbed for other reasons entirely.
    wants_share: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.add_stub and not self.mark_applied


def plan_seed(
    *,
    cache: VerdictCache,
    verdicts: dict[str, tuple[bool, bool]],
    current_stub: set[str],
) -> SeedPlan:
    """Decide what to seed.

    *verdicts* maps server name -> (recommend_stub, recommend_share), already
    resolved by the caller from the cache and the hazard ledger. Pure: it reads
    the applied markers and returns a plan, so the decision is testable without
    a config file, a gateway, or a clock.
    """
    add: list[str] = []
    mark: list[str] = []
    share: list[str] = []
    for name in sorted(verdicts):
        recommend_stub, recommend_share = verdicts[name]
        if cache.was_applied(name):
            # Already the operator's call. Not re-examined, not re-flipped.
            continue
        if not recommend_stub:
            # Nothing to write, but record that this server was considered, so a
            # server that is merely "not recommended" is not re-evaluated for
            # seeding on every single start.
            mark.append(name)
            continue
        mark.append(name)
        if name not in current_stub:
            add.append(name)
        if recommend_share:
            share.append(name)
    return SeedPlan(
        add_stub=tuple(add), mark_applied=tuple(mark), wants_share=tuple(share)
    )


def apply_seed(plan: SeedPlan, section: dict, cache: VerdictCache) -> bool:
    """Apply *plan* to a loaded ``mcp_gateway`` config *section*. True if changed.

    The caller owns reading, locking and writing the config file; this function
    only mutates the section in memory and records the markers, so the same logic
    is exercised by tests and by the gateway without a second implementation.
    """
    if plan.is_empty:
        return False
    changed = False
    if plan.add_stub:
        current = section.get("stub_servers")
        existing = [s for s in current if isinstance(s, str)] if isinstance(current, list) else []
        merged = sorted(set(existing) | set(plan.add_stub))
        if merged != existing:
            section["stub_servers"] = merged
            changed = True
            logger.info(
                "mcp seeding: stubbing %s on first evaluation (edit "
                "mcp_gateway.stub_servers to change)",
                ", ".join(plan.add_stub),
            )
    for name in plan.mark_applied:
        cache.mark_applied(name)
    return changed
