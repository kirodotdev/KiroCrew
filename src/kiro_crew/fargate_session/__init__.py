"""AWS Fargate session environments: a session's workspace in an ECS task.

The engine stays on the gateway, as for Coder; the task holds the workspace.
:mod:`.bootstrap` speaks to the guest bootstrap, :mod:`.tasks` runs and stops
ECS tasks, :mod:`.forward` keeps the SSM port forward, and :mod:`.launch` ties
them into the same ``prepare`` seam Coder's provider offers.
"""

from __future__ import annotations

from typing import Any, Optional


def fargate_profile(section: Any, profile_id: str) -> Optional[dict[str, Any]]:
    """The saved Fargate profile, refusing any ambiguous cross-provider id."""
    from kiro_crew.config.session_environments import provider_for_profile

    if provider_for_profile(section, profile_id) != "fargate":
        return None
    for profile in getattr(section, "fargate_profiles", None) or []:
        if isinstance(profile, dict) and profile.get("id") == profile_id:
            return profile
    return None
