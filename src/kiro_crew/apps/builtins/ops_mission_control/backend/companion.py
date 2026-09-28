"""Companion adapter discovery — the door handle on the ADD-only registry.

``registry.py`` documents itself as "the seam an out-of-tree companion package
plugs into", and its ADD-only rule was already enforced and tested. But
``get_registry()`` installed *only* the public adapters and nothing ever looked for a
companion — so the seam was a door with no handle: an out-of-tree package could
implement every Protocol correctly and still never be reached. This module is the
handle.

**Why entry points and not a config path.** A filesystem path to import would be a
new, unaudited code-loading channel in an app whose whole security story is that the
agent cannot reach its own configuration. ``importlib.metadata`` entry points mean
the only way to contribute an adapter is to *install a package* — an action outside
the agent's reach and visible to ``pip list``. This deliberately mirrors
``platform/discovery.py`` (``kirocrew.plugins``), including its version-split
handling, because a second convention for the same job is a second thing to get
wrong.

**The admission policy is reused, not reinvented.** Importing a separately-installed
package's code into the gateway process is a supply-chain decision, and governance
guidance on third-party packages is explicit that 3P code must come through a
reviewed channel rather than being pulled in directly. This app is in no position to
adjudicate that itself, so every candidate is run through the SAME fleet
``AdmissionPolicy`` that gates platform plugins — evaluated BEFORE ``ep.load()``, so
a rejected package's code never executes — and each decision lands on the SEL audit
trail. A companion is not more trusted for being ours.

**Fail-OPEN here, unlike platform discovery.** This is the one deliberate divergence
and it is a product decision, not an oversight. ``platform/discovery.py`` fails
CLOSED because a missing companion there could silently drop a *security overlay* —
running without it is less safe. Here the companion only ADDS signal sources: a
missing one means fewer alarms are watched, which is visible on the Signals tab, and
aborting gateway boot over it would take down a working public install (chat, crons,
every other app) to punish an optional integration. So a broken companion is logged,
audited, and skipped — never fatal. The failure is loud in the log and visible in the
UI, which is where an ops operator looks.

Contract for a companion package::

    # pyproject.toml
    [project.entry-points."kirocrew.ops_providers"]
    my-company = "my_pkg.ops:register_adapters"

    # my_pkg/ops.py
    def register_adapters(registry) -> None:
        registry.register_signal_source(MyTicketSource())

``register_adapters`` receives the live ``OpsProviderRegistry`` and calls the same
public ``register_*`` methods the core uses. ADD-only still applies: an id that
collides with a core adapter is refused and the core wins, so what the public core
does stays auditable on its own.

See ``docs/system-specs/modules/ops-mission-control.md`` § Companion adapters.
"""

from __future__ import annotations

import importlib.metadata
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.platform.admission import (
    evaluate_admission,
    load_admission_policy,
    seed_default_policy,
)
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.apps.builtins.ops_mission_control.backend.registry import (
        OpsProviderRegistry,
    )

logger = logging.getLogger(__name__)

#: Entry-point group a companion registers its adapter installer under. Distinct
#: from ``platform.discovery.PLUGIN_GROUP`` (``kirocrew.plugins``, which composes a
#: whole PlatformContext): contributing an ops adapter must not require — or imply —
#: authority over the platform edition seam.
PROVIDER_GROUP = "kirocrew.ops_providers"


def provider_entry_points() -> list[importlib.metadata.EntryPoint]:
    """Registered ``kirocrew.ops_providers`` entry points (may be empty).

    Mirrors ``platform.discovery.plugin_entry_points``, including the API split:
    the ``group=`` keyword only exists on Python 3.10+, while 3.9 (this project's
    declared minimum) returns a dict keyed by group. Without the 3.9 branch a
    companion would be silently invisible on the oldest supported interpreter —
    the worst kind of bug, because everything appears to work.
    """
    try:
        eps = importlib.metadata.entry_points()
        if hasattr(eps, "select"):
            return list(eps.select(group=PROVIDER_GROUP))
        return list(eps.get(PROVIDER_GROUP, []))  # type: ignore[attr-defined]
    except Exception:
        logger.debug("ops-mission-control: provider entry-point discovery failed", exc_info=True)
        return []


def _audit(ep_name: str, outcome: str, *, detail: str = "", error: str = "") -> None:
    """Record one admission/registration decision on the immutable audit trail.

    Loading third-party code into the gateway is exactly the kind of decision that
    must be reconstructable after the fact, so this is audited whether it succeeds
    or fails — a silent skip would make a missing adapter indistinguishable from a
    never-installed one.
    """
    try:
        sel().log_api_access(
            caller="core:ops-mission-control",
            operation="ops_provider.admission",
            outcome=outcome,
            resources=f"entry_point={ep_name} {detail}".strip(),
            error=error,
        )
    except Exception:  # pragma: no cover - SEL may be unwired in a CLI process
        logger.debug("ops-mission-control: provider-admission SEL emit unavailable")


def _admitted(ep: importlib.metadata.EntryPoint) -> tuple[bool, str]:
    """Run the fleet admission policy against one candidate, before loading it.

    Reuses ``platform.admission`` rather than defining a second trust root: a fleet
    that has banned a package must not be able to have that decision bypassed by
    installing it as an ops adapter instead of as a platform plugin.

    On an unexpected evaluator failure this returns *not admitted*. That is the one
    place inside this module that behaves fail-closed, and it should: "the gate
    broke" must never read as "the gate said yes".
    """
    try:
        seed_default_policy()
        decision = evaluate_admission(ep, load_admission_policy())
        return bool(decision.allowed), str(decision.reason)
    except Exception as exc:
        logger.warning(
            "ops-mission-control: admission check failed for %r — refusing to load it: %s",
            ep.name,
            exc,
        )
        return False, f"admission check failed: {exc}"


def install_companion_adapters(registry: "OpsProviderRegistry") -> int:
    """Let every admitted companion package add its adapters. Returns how many ran.

    Never raises. A companion that is rejected, fails to import, or throws while
    registering is logged, audited, and skipped — see the module docstring for why
    this fails open where platform discovery fails closed.
    """
    eps = provider_entry_points()
    if not eps:
        return 0

    installed = 0
    for ep in eps:
        allowed, reason = _admitted(ep)
        if not allowed:
            logger.error(
                "ops-mission-control: companion %r rejected by admission policy: %s",
                ep.name,
                reason,
            )
            _audit(ep.name, "denied", error=reason)
            continue

        try:
            register = ep.load()
        except Exception as exc:
            logger.warning("ops-mission-control: could not load companion %r: %s", ep.name, exc)
            _audit(ep.name, "failure", detail="load failed", error=str(exc))
            continue

        if not callable(register):
            logger.warning(
                "ops-mission-control: companion %r is not callable — expected "
                "register_adapters(registry)",
                ep.name,
            )
            _audit(ep.name, "failure", detail="not callable")
            continue

        try:
            register(registry)
        except Exception as exc:
            # Partial registration is possible here: the companion may have added
            # some adapters before raising. That is acceptable and is why this is
            # audited — an operator can see which companion misbehaved, and the
            # adapters that did register are individually visible on the Signals
            # tab rather than silently half-present.
            logger.warning(
                "ops-mission-control: companion %r failed while registering: %s", ep.name, exc
            )
            _audit(ep.name, "failure", detail="register raised", error=str(exc))
            continue

        installed += 1
        logger.info("ops-mission-control: companion %r registered adapters (%s)", ep.name, reason)
        _audit(ep.name, "success", detail=reason)

    return installed


def companion_summary() -> list[dict[str, Any]]:
    """What companions are installed, for the Settings surface.

    Read-only and does NOT load any plugin code — it reports what is *installed*,
    which is a different question from what was admitted at boot. Shown so an
    operator can tell "no companion installed" from "companion installed but
    rejected", which have completely different fixes.
    """
    rows: list[dict[str, Any]] = []
    for ep in provider_entry_points():
        rows.append({"name": ep.name, "target": getattr(ep, "value", "")})
    return rows
