"""The lane's two platform-seam pieces in the stock build.

* :class:`ManagedEnvProvisionerProvider` wraps the core's remote-provisioner provider: it
  adds the "Managed environment" row while the lane is on (the ``crew_client`` preview,
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
from kiro_crew.managed_env import settings as lane_settings
from kiro_crew.platform.defaults import DefaultDashboardContributor
from kiro_crew.platform.interfaces import RemoteProvisioner

MANAGED_ENV_PROVISIONER = RemoteProvisioner(
    id=PROVISIONER_ID,
    kind=PROVISIONER_KIND,
    label="Managed environment",
    posix_only=True,
    step_labels=(
        ("preflight", "Check your managed environments"),
        ("provision", "Create the environment and install Crew"),
        ("signin", "Confirm its machine sign-in"),
        ("connect", "Add it to your crews"),
    ),
)


class ManagedEnvProvisionerProvider:
    """*base*'s lanes, plus the managed-environment lane while it is on."""

    def __init__(self, base: Any) -> None:
        self._base = base

    def provisioners(self) -> List[RemoteProvisioner]:
        rows = list(self._base.provisioners())
        if lane_settings.lane_enabled() and not any(row.id == PROVISIONER_ID for row in rows):
            rows.append(MANAGED_ENV_PROVISIONER)
        return rows

    def engine_for(self, provisioner_id: str, *, confirmed_recipient: str = "") -> Any:
        if provisioner_id == PROVISIONER_ID:
            if not lane_settings.lane_enabled():
                # The same KeyError an unknown id raises: with the lane off there is no lane.
                raise KeyError(provisioner_id)
            from kiro_crew.managed_env.engine import SandboxLaunchEngine

            return SandboxLaunchEngine()
        return self._base.engine_for(provisioner_id, confirmed_recipient=confirmed_recipient)


class ManagedEnvDashboardContributor(DefaultDashboardContributor):
    """The core's default contributor, plus the lane's routes and sign-in profile."""

    def contribute_routes(self, app: Any) -> None:
        super().contribute_routes(app)
        # Mounted always, so turning the preview on needs no restart; every handler runs the
        # owner check, then answers 404 while the lane is off.
        from kiro_crew.managed_env.routes import (
            CREWS_WATCH_S,
            SandboxRoutes,
            register_sandbox_routes,
        )

        register_sandbox_routes(app, SandboxRoutes(crews_watch_s=CREWS_WATCH_S))

    def idc_sign_in_profiles(self) -> Dict[str, Dict[str, Any]]:
        profiles = dict(super().idc_sign_in_profiles())
        profile = lane_settings.sign_in_profile()
        if profile is not None:
            profiles[lane_settings.read_settings().start_url.rstrip("/")] = profile
        return profiles
