"""Multi-provider skill discovery and installation.

This package provides a pluggable interface for searching and installing
skills from external registries. Each provider (skills.sh, a GitHub repository,
PromptFarm, etc.) implements the ``SkillProvider`` protocol and registers itself
in the ``ProviderRegistry``.

Every provider's network layer comes from ``_http``: the SSRF screen, the
redirect allowlist and the bounded body read are one implementation, so a new
provider inherits the trust boundary instead of restating it.
"""

from kiro_crew.skill_providers.base import (
    ProviderRegistry,
    SkillProvider,
    SkillSearchResult,
)
from kiro_crew.skill_providers.skillsh import SkillsShProvider

#: ``GitHubRepoProvider`` is deliberately NOT re-exported here. Importing any
#: submodule runs this file first, so a re-export would pull ``github`` in for every
#: consumer of the package -- which is exactly the gateway-boot-path import that
#: ``_build_registry`` defers behind its discovery-policy check. Import it from
#: ``kiro_crew.skill_providers.github`` directly.
__all__ = [
    "SkillProvider",
    "SkillSearchResult",
    "ProviderRegistry",
    "SkillsShProvider",
]
