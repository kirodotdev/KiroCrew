"""Agent SDK surface for the ``agent.env`` overlay an agent child starts with.

The overlay is read by the shared ACP launch tail. Application code that builds an
environment the way an agent child gets it (``kirocrew sandbox explain``) reads the
same overlay here, without importing the ACP implementation directly.
"""

from __future__ import annotations

from kiro_crew.acp.agent_env import agent_env_overlay

__all__ = ["agent_env_overlay"]
