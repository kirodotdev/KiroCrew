"""Parse the dashboard's local ``/goal`` command without side effects."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class GoalCommandAction(str, Enum):
    """The four outcomes the local dashboard command can produce."""

    STATUS = "status"
    CLEAR = "clear"
    SET = "set"
    USAGE = "usage"


@dataclass(frozen=True)
class GoalCommand:
    """Canonical interpretation shared by validation and execution."""

    action: GoalCommandAction
    objective: str = ""
    max_cycles: int = 50

    @property
    def mutates_automation(self) -> bool:
        return self.action in {GoalCommandAction.CLEAR, GoalCommandAction.SET}


def parse_goal_command(message: str) -> GoalCommand | None:
    """Parse an exact ``/goal`` command, preserving live-command semantics.

    ``None`` means ordinary text or another slash command. Empty and ``status``
    are reads; ``clear`` and a valid objective mutate the session automation.
    Malformed ``--max`` forms show usage and therefore mutate nothing.
    """
    parts = message.split(None, 1)
    if not parts or parts[0] != "/goal":
        return None
    rest = parts[1].strip() if len(parts) > 1 else ""
    if rest in ("", "status"):
        return GoalCommand(GoalCommandAction.STATUS)
    if rest == "clear":
        return GoalCommand(GoalCommandAction.CLEAR)

    max_cycles = 50
    objective = rest
    match = re.match(r"--max\s+(\d+)\s+(.*)", rest, re.DOTALL)
    if match:
        try:
            parsed_max = int(match.group(1))
        except ValueError:
            # CPython bounds decimal-to-int conversion. A longer digit string
            # is still regex-valid, but must remain a non-mutating usage error
            # rather than escaping scheduled-message validation.
            return GoalCommand(GoalCommandAction.USAGE)
        max_cycles = max(1, min(50, parsed_max))
        objective = match.group(2).strip()
    elif rest.startswith("--max"):
        objective = ""
    if not objective:
        return GoalCommand(GoalCommandAction.USAGE)
    return GoalCommand(GoalCommandAction.SET, objective=objective, max_cycles=max_cycles)


def goal_command_mutates_automation(message: str) -> bool:
    """Whether *message* is a local goal command that changes automation."""
    command = parse_goal_command(message)
    return command is not None and command.mutates_automation
