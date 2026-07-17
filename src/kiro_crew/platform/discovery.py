"""Fail-closed companion discovery via ``importlib.metadata`` entry points.

A non-standalone edition is supplied by a companion package that registers a
``kirocrew.plugins`` entry point pointing at its composition root
(``build_amazon_context``).  Discovery is **fail-closed**: when the profile says
amazon but no compatible companion is found, boot aborts rather than silently
running with the open-source defaults (which could drop a security overlay).
"""

from __future__ import annotations

import importlib.metadata
import logging
from typing import TYPE_CHECKING, List

from kiro_crew.platform.admission import (
    AdmissionPolicy,
    evaluate_admission,
    load_admission_policy,
    seed_default_policy,
)
from kiro_crew.platform.context import (
    PROFILE_STANDALONE,
    PlatformCompositionError,
)

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform.context import PlatformContext

logger = logging.getLogger(__name__)

# The entry-point group a companion registers its composition root under.
PLUGIN_GROUP = "kirocrew.plugins"


class PluginAdmissionError(PlatformCompositionError):
    """Raised when a plugin is rejected by the fleet admission policy.

    A subclass of :class:`PlatformCompositionError` so a rejected plugin aborts
    boot fail-closed, the same as any other composition failure.
    """


def plugin_entry_points() -> List[importlib.metadata.EntryPoint]:
    """Return the registered ``kirocrew.plugins`` entry points (may be empty).

    Handles the ``importlib.metadata.entry_points`` API split across versions:
    the ``group=`` keyword only exists on Python 3.10+.  On 3.9 (the declared
    minimum) ``entry_points()`` returns a dict keyed by group, so we select the
    group from it.  Without this branch a 3.9 host silently sees zero plugins,
    and an Amazon host then fails closed at boot even with the companion
    installed.
    """
    try:
        eps = importlib.metadata.entry_points()
        if hasattr(eps, "select"):
            # Python 3.10+: SelectableGroups / EntryPoints.select(group=...)
            return list(eps.select(group=PLUGIN_GROUP))
        # Python 3.9: entry_points() returns a dict {group: [EntryPoint, ...]}.
        return list(eps.get(PLUGIN_GROUP, []))  # type: ignore[attr-defined]
    except Exception:
        logger.debug("entry-point discovery failed", exc_info=True)
        return []


def discover_companion_context(
    profile: str, cfg: "KiroCrewConfig", *, policy: "AdmissionPolicy | None" = None
) -> "PlatformContext | None":
    """Load the companion-composed context for a non-standalone profile.

    Returns ``None`` only for the standalone profile.  For any non-standalone
    profile it either returns the companion's :class:`PlatformContext` or raises
    :class:`PlatformCompositionError` (fail-closed).

    Before importing any plugin code, every candidate entry point is run through
    the fleet :class:`AdmissionPolicy` (``policy``, default-loaded from the
    fleet-controlled source).  A plugin the policy rejects raises
    :class:`PluginAdmissionError` — the supply-chain / kill-switch gate.
    """
    if profile == PROFILE_STANDALONE:
        return None

    eps = plugin_entry_points()
    if not eps:
        raise PlatformCompositionError(
            f"profile={profile!r} but no {PLUGIN_GROUP!r} entry point is installed; "
            "refusing to boot with open-source defaults (fail-closed). Install the "
            "companion package, or set KIROCREW_PROFILE=standalone to run the "
            "open-source edition (e.g. on a host with a stale ~/.midway)."
        )
    if len(eps) > 1:
        names = [e.name for e in eps]
        raise PlatformCompositionError(
            f"ambiguous {PLUGIN_GROUP!r} entry points: {names}; expected exactly one."
        )

    ep = eps[0]

    # ── Admission gate (verify-before-run) ──
    # Decide allow/reject BEFORE ep.load() so a rejected plugin's code never
    # runs.  The policy is the fleet trust root, never sourced from the plugin.
    if policy is None:
        # Seed the permissive first-run default BEFORE consulting the policy.  A
        # fleet's FIRST boot runs bootstrap → discovery (here) BEFORE the
        # gateway's ``run_first_run_setup`` seed, so without this a companion
        # would be rejected by the fail-closed default and boot would abort.
        # Marker-guarded + idempotent: a later DELETION is NOT re-seeded, so
        # tampering still fails closed (AVP-23427).
        seed_default_policy()
        policy = load_admission_policy()
    decision = evaluate_admission(ep, policy)
    if not decision.allowed:
        logger.error("plugin %r rejected by admission policy: %s", ep.name, decision.reason)
        raise PluginAdmissionError(
            f"plugin {ep.name!r} rejected by admission policy: {decision.reason}"
        )
    logger.info("plugin %r admitted: %s", ep.name, decision.reason)

    try:
        build_context = ep.load()
    except Exception as exc:  # pragma: no cover - import-time companion failure
        raise PlatformCompositionError(
            f"failed to load companion entry point {ep.name!r}: {exc}"
        ) from exc

    # Normalize ANY composition-root failure to PlatformCompositionError so the
    # fail-closed boot guard recognizes it. A companion that raises a plain
    # ValueError/ImportError/KeyError here would otherwise propagate un-wrapped
    # and be swallowed by the cli.main boot handler (which only re-raises
    # PlatformCompositionError), silently downgrading the edition.
    try:
        ctx = build_context(cfg)
    except PlatformCompositionError:
        raise
    except Exception as exc:
        raise PlatformCompositionError(
            f"companion entry point {ep.name!r} failed to compose a context: {exc}"
        ) from exc
    if ctx is None:
        raise PlatformCompositionError(
            f"companion entry point {ep.name!r} returned no PlatformContext."
        )
    return ctx
