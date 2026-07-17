"""Generate the conductor SKILL.md — an always-loaded routing table.

Loaded into default kirocrew agent so delegation is transparent.
Auto-seeds metadata files for agents that lack one.
"""

from __future__ import annotations

import logging
from pathlib import Path

from kiro_crew.agent_metadata import load, load_all, save
from kiro_crew.aim_agents import list_agents

logger = logging.getLogger(__name__)

# Agents to exclude from the roster (self-references).
_EXCLUDE = {"kirocrew", "kirocrew-conductor"}


def generate_conductor_skill(skills_loader) -> Path:
    """Write conductor/SKILL.md under skills_loader._dir.

    1. Discover all installed agents
    2. Auto-seed metadata for agents without a .md file
    3. Build rich SKILL.md with delegation guidelines + roster
    """
    agents = [a for a in list_agents(include_project=False) if a.name not in _EXCLUDE]

    # Auto-seed metadata from agent description if missing.
    for a in agents:
        if not load(a.name) and a.description:
            save(a.name, a.description)
            logger.info("Auto-seeded metadata for %s from description", a.name)

    metadata = load_all()

    # Build roster section.
    roster_lines: list[str] = []
    for a in agents:
        desc = metadata.get(a.name) or a.description or "No description available"
        roster_lines.append(f"### {a.name}\n\n{desc}\n")

    skill_content = _SKILL_TEMPLATE.format(
        roster="\n".join(roster_lines) if roster_lines else "_No specialist agents installed._\n",
    )

    skill_dir: Path = skills_loader._dir / "conductor"
    skill_dir.mkdir(parents=True, exist_ok=True)
    out = skill_dir / "SKILL.md"
    out.write_text(skill_content, encoding="utf-8")
    return out


_SKILL_TEMPLATE = """\
---
always: true
---
# Agent Delegation

You have access to specialist agents via `spawn_run(agent="<name>", task="<description>")`.

## Default behavior

You (kirocrew) are the default agent and can handle most tasks directly.
Only delegate when you are highly confident a specialist is a better fit.
When in doubt, handle it yourself.

## When to delegate

- The task clearly and specifically matches a specialist's description below
- The specialist has domain expertise or tools you lack for this exact task
- The user explicitly asks to use a specific agent

## When NOT to delegate

- You can handle the task yourself (this is the common case)
- The match to a specialist is only partial or vague
- Simple questions, general coding, file operations, or conversational tasks
- The user is in a back-and-forth conversation (don't break the flow)
- No specialist below is a strong match — handle it yourself

## Effort scaling

- Most requests → handle yourself directly
- Needs specialist tools → spawn 1 agent
- Complex multi-part task → up to 3 agents in parallel (max concurrent limit)

## Delegation quality

Write specific task descriptions. Include context the specialist needs.
- Bad: "review the code"
- Good: "Review CR-12345 for security issues, focusing on auth token handling in session.py"

## Available Agents

{roster}\
"""
