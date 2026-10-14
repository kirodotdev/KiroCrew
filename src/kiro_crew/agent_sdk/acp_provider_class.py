"""The ACP provider class, for the config layer's provider factory.

``KiroCrewConfig.create_provider_factory`` constructs
:class:`kiro_crew.providers.acp.AcpProvider` sessions. It reaches the class through
this module so the config package imports no ACP-layer module itself, which is the
line ``scripts/check_agent_sdk_boundary.py`` holds application code to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.providers.acp import AcpProvider


def acp_provider_class() -> type[AcpProvider]:
    """Return ``kiro_crew.providers.acp.AcpProvider`` as that module holds it now.

    Imported when called, not at module scope: ``providers.acp`` imports
    ``acp.client``, whose import chain reaches ``session`` and then
    ``config.loader``. Read at each call, so a patch of
    ``kiro_crew.providers.acp.AcpProvider`` reaches the factory that calls this.
    """
    from kiro_crew.providers.acp import AcpProvider

    return AcpProvider
