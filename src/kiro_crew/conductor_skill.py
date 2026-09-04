"""Generate the conductor SKILL.md — an always-loaded delegation guide.

Loaded into the default kirocrew agent so delegation is transparent. The crew
roster is NOT inlined here: the model discovers and binds crews at decision time
via the ``select_crew`` MCP tool, whose roster lists only crews that define
``triggers`` (a crew without triggers is not a routing candidate). Keeping the
roster in the tool — rather than injected prose — keeps this always-on skill
small and single-sources the roster from config.
"""

from __future__ import annotations

from pathlib import Path


def generate_conductor_skill(skills_loader) -> Path:
    """Write conductor/SKILL.md under ``skills_loader._dir``.

    Static content: the roster is resolved at call time via ``select_crew``, so
    this does not read config and does not need regenerating when crews change.
    """
    skill_dir: Path = skills_loader._dir / "conductor"
    skill_dir.mkdir(parents=True, exist_ok=True)
    out = skill_dir / "SKILL.md"
    out.write_text(_SKILL_CONTENT, encoding="utf-8")
    return out


_SKILL_CONTENT = """\
---
always: true
---
# Agent Delegation

You can route a task to a specialist **crew** — a refined agent with its own
prompt, tools, workspace, and memory.

## How to delegate

1. Call `select_crew` with no argument to list the selectable crews and their
   triggers. Only crews that define triggers appear — a crew with no triggers is
   never a routing candidate.
2. Select a crew ONLY when its triggers clearly and specifically match the task
   with **high confidence**. Then `select_crew(crew="<name>")` binds it — the
   response returns its resolved workspace, memory store, kiro agent, and model.
3. Run the work with `spawn_run(agent="<name>", task="<specific description>")`.
4. If no crew is a strong match (or the roster is empty), do NOT route — fall
   back to the default crew and handle it yourself.

## Default behavior

You (kirocrew) are the default crew and handle most tasks directly. Delegate only
on a high-confidence, specific match; otherwise the default crew is the fallback.
When in doubt, handle it yourself.

## When NOT to delegate

- You can handle the task yourself (this is the common case)
- The match to a crew is only partial or vague (confidence is not high)
- Simple questions, general coding, file operations, or conversational tasks
- The user is in a back-and-forth conversation (don't break the flow)

## Delegation quality

Write specific task descriptions. Include context the specialist needs.
- Bad: "review the code"
- Good: "Review CR-12345 for security issues, focusing on auth token handling in session.py"
"""
