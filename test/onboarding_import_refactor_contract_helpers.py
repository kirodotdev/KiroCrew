"""The onboarding engine's module list, read by both facade contract test files.

``test_onboarding_import_refactor_contract.py`` imports each module first and reads
what each binds; ``test_onboarding_import_refactor_contract_patch_guards.py`` reads the
same list to find a rebound name's other holders.
"""

from __future__ import annotations

#: Every module of the engine, facade included.
_ENGINE_MODULES = (
    "kiro_crew.onboarding_import",
    "kiro_crew.onboarding_scan",
    "kiro_crew.onboarding_plan",
    "kiro_crew.onboarding_apply",
    "kiro_crew.onboarding_sources",
    "kiro_crew.onboarding_sources.amazon_quick",
    "kiro_crew.onboarding_sources.claude_code",
    "kiro_crew.onboarding_sources.codex",
    "kiro_crew.onboarding_sources.gemini",
    "kiro_crew.onboarding_sources.hermes",
    "kiro_crew.onboarding_sources.lineage",
    "kiro_crew.onboarding_sources.openclaw",
)
