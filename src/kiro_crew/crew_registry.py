"""Built-in role-agent registration for private Kiro Crew workers.

The role packages own namespaced kiro-cli agent specs and prompts.  This module
materializes those executable workers during the existing install lifecycle and
provides a private lookup predicate for runtime dispatch and public-visibility
filters; it never creates user-facing aliases.
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Role specs are executable runtime workers only.  Their names are deliberately
# namespaced and never become config aliases, normal picker rows, or Crew cards.
INTERNAL_CREW_AGENT_NAMES: frozenset[str] = frozenset(
    {
        "kirocrew-software-delivery-architect",
        "kirocrew-software-delivery-engineer",
        "kirocrew-software-delivery-validator",
        "kirocrew-software-delivery-security",
        "kirocrew-knowledge-quality-researcher",
        "kirocrew-knowledge-quality-validator",
        "kirocrew-knowledge-quality-security",
        "kirocrew-quality-engineering-qa",
        "kirocrew-quality-engineering-e2e",
        "kirocrew-quality-engineering-ux",
    }
)

# Historical short aliases are also hidden if an older config still contains
# one.  They are not accepted for new lookup and are never materialized.
_LEGACY_CREW_ROLE_ALIASES: frozenset[str] = frozenset(
    {
        "software-delivery-architect",
        "software-delivery-engineer",
        "software-delivery-validator",
        "software-delivery-security",
        "knowledge-quality-researcher",
        "knowledge-quality-validator",
        "knowledge-quality-security",
    }
)

_CREW_PACKAGE_MODULES = (
    "kiro_crew.crews.software_delivery",
    "kiro_crew.crews.knowledge_quality",
    "kiro_crew.crews.quality_engineering",
)


def is_internal_crew_worker(agent_name: object) -> bool:
    """Return whether *agent_name* belongs to the private Crew worker surface."""

    return isinstance(agent_name, str) and (
        agent_name in INTERNAL_CREW_AGENT_NAMES or agent_name in _LEGACY_CREW_ROLE_ALIASES
    )


def internal_crew_agent_names() -> frozenset[str]:
    """Private lookup set used by execution paths and visibility filters."""

    return INTERNAL_CREW_AGENT_NAMES


def _package_is_materialized(package: Any, agents_dir: Path, prompt_dir: Path) -> bool:
    """Check whether every package-owned spec/prompt pair is already present."""

    prompt_files = getattr(package, "_PROMPT_FILES", {})
    for role_id in package.AGENT_SPEC_FILES:
        expected = package.load_agent_spec(role_id)
        name = expected.get("name")
        prompt_name = prompt_files.get(role_id)
        if not isinstance(name, str) or not isinstance(prompt_name, str):
            return False

        spec_path = agents_dir / f"{name}.json"
        prompt_path = prompt_dir / prompt_name
        if not spec_path.is_file() or not prompt_path.is_file():
            return False
        try:
            actual = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(actual, dict):
            return False
        if any(actual.get(key) != value for key, value in expected.items()):
            return False
        if actual.get("prompt") != prompt_path.resolve().as_uri():
            return False

    return True


def materialize_builtin_crew_agents(
    agents_dir: Path,
    prompt_dir: Path,
) -> tuple[Path, ...]:
    """Install the seven role specs into explicit, collision-safe targets.

    The role packages retain ownership of their resource validation and atomic
    writes.  This bridge is intentionally best-effort: a user-owned file with a
    colliding name must not make the primary Kiro Crew agent installation fail.
    Existing package-identical files are treated as an idempotent no-op.
    """

    target_agents = Path(agents_dir).expanduser().resolve()
    target_prompts = Path(prompt_dir).expanduser().resolve()
    written: list[Path] = []

    for module_name in _CREW_PACKAGE_MODULES:
        try:
            package = importlib.import_module(module_name)
            if _package_is_materialized(package, target_agents, target_prompts):
                continue
            written.extend(
                package.materialize_agent_specs(
                    target_agents,
                    target_prompts,
                )
            )
        except Exception as exc:  # noqa: BLE001 - optional roster entry must not break chat
            logger.warning(
                "Built-in Crew agent materialization skipped for %s: %s",
                module_name.rsplit(".", 1)[-1],
                type(exc).__name__,
            )

    return tuple(written)


__all__ = [
    "INTERNAL_CREW_AGENT_NAMES",
    "internal_crew_agent_names",
    "is_internal_crew_worker",
    "materialize_builtin_crew_agents",
]
