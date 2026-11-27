"""The lane's two platform-seam pieces in the stock build.

* :class:`ManagedEnvProvisionerProvider` wraps the core's remote-provisioner provider: it
  adds the "Kiro Managed Environments" row while the lane is on (the ``crew_client`` preview,
  read on every call) and hands out :class:`.engine.SandboxLaunchEngine` for it.
* :class:`ManagedEnvDashboardContributor` is the core's default dashboard contributor
  plus the lane's routes and the lane's Identity Center sign-in profile
  (:func:`.settings.sign_in_profile`, None while the lane is off or unset).

An edition that installs its own contributor or provider replaces these, as it replaces
the defaults they wrap.
"""

from __future__ import annotations

from typing import Any, Dict, List

from kiro_crew.managed_env import PROVISIONER_ID, PROVISIONER_KIND
from kiro_crew.platform.defaults import (
    DefaultDashboardContributor,
    DefaultRemoteProvisionerProvider,
)
from kiro_crew.platform.interfaces import RemoteProvisioner

MANAGED_ENV_PROVISIONER = RemoteProvisioner(
    id=PROVISIONER_ID,
    kind=PROVISIONER_KIND,
    label="Kiro Managed Environments",
    posix_only=True,
    step_labels=(
        ("preflight", "Check your managed environments"),
        ("provision", "Create the environment and install Crew"),
        ("signin", "Confirm its machine sign-in"),
        ("connect", "Add it to your crews"),
    ),
)


def _offered() -> bool:
    """The lane is on, and this release has the machine mode an environment's Crew needs."""
    from kiro_crew.managed_env import machine
    from kiro_crew.managed_env import settings as lane_settings

    return lane_settings.lane_enabled() and machine.shipped()


class ManagedEnvProvisionerProvider(DefaultRemoteProvisionerProvider):
    """The core's lanes (or *base*'s, for tests), plus the managed-environment lane while
    it is on. A subclass of the default, so the stock build still reads as composing it."""

    def __init__(self, base: Any = None) -> None:
        self._base = base

    def provisioners(self) -> List[RemoteProvisioner]:
        rows = list(self._base.provisioners() if self._base else super().provisioners())
        if _offered() and not any(row.id == PROVISIONER_ID for row in rows):
            rows.append(MANAGED_ENV_PROVISIONER)
        return rows

    def engine_for(self, provisioner_id: str, *, confirmed_recipient: str = "") -> Any:
        if provisioner_id == PROVISIONER_ID:
            if not _offered():
                # The same KeyError an unknown id raises: with the lane off there is no lane.
                raise KeyError(provisioner_id)
            from kiro_crew.managed_env.engine import SandboxLaunchEngine

            return SandboxLaunchEngine()
        if self._base is None:
            return super().engine_for(provisioner_id, confirmed_recipient=confirmed_recipient)
        return self._base.engine_for(provisioner_id, confirmed_recipient=confirmed_recipient)


class ManagedEnvDashboardContributor(DefaultDashboardContributor):
    """The core's default contributor, plus the lane's routes and sign-in profile."""

    def contribute_routes(self, app: Any) -> None:
        super().contribute_routes(app)
        # Mounted always, so turning the preview on needs no restart; every handler runs the
        # owner check, then answers 404 while the lane is off.
        from kiro_crew.managed_env.routes import (
            SandboxRoutes,
            register_sandbox_routes,
        )

        register_sandbox_routes(app, SandboxRoutes())

    def idc_sign_in_profiles(self) -> Dict[str, Dict[str, Any]]:
        profiles = dict(super().idc_sign_in_profiles())
        from kiro_crew.managed_env import settings as lane_settings

        profile = lane_settings.sign_in_profile()
        if profile is not None:
            profiles[profile["issuer_url"]] = profile
        return profiles
