"""Multi-provider skill discovery and installation.

This package provides a pluggable interface for searching and installing
skills from external registries. Each provider (skills.sh, PromptFarm, etc.)
implements the ``SkillProvider`` protocol and registers itself in the
``ProviderRegistry``.
"""

from kiro_crew.skill_providers.base import (
    SkillProvider,
    SkillSearchResult,
    ProviderRegistry,
)
from kiro_crew.skill_providers.skillsh import SkillsShProvider

__all__ = [
    "SkillProvider",
    "SkillSearchResult",
    "ProviderRegistry",
    "SkillsShProvider",
]
