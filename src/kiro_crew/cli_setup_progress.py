"""Which ``kirocrew setup`` steps a run did not finish, for its abort message.

A closed stdin ends the wizard at whichever prompt reads it first. The steps
after that prompt never ran, so the abort line names them and the command that
finishes them, instead of leaving the user to guess what is still unconfigured.
"""

from __future__ import annotations

from collections.abc import Iterable


class SetupProgress:
    """The wizard's planned steps, in order, and the one it is on."""

    def __init__(self) -> None:
        self._planned: list[str] = []
        self._current = 0

    def plan(self, steps: Iterable[str]) -> None:
        """Record the steps this run will go through; none of them is done yet."""
        self._planned = list(steps)
        self._current = 0

    def begin(self, step: str) -> None:
        """Mark ``step`` as the one running: every step before it is finished."""
        self._current = self._planned.index(step)

    def unfinished(self) -> list[str]:
        """The running step and every planned step after it."""
        return self._planned[self._current :]


def rerun_command(*, agent_only: bool, clean: bool, slack: bool, whatsapp: bool) -> str:
    """The ``kirocrew setup`` command line with the flags this run was given."""
    flags = (
        ("--agent-only", agent_only),
        ("--clean", clean),
        ("--slack", slack),
        ("--whatsapp", whatsapp),
    )
    return " ".join(["kirocrew setup", *(flag for flag, on in flags if on)])


def abort_message(reason: str, progress: SetupProgress, command: str) -> str:
    """The lines printed when the wizard stops on a closed stdin."""
    lines = [f"\n⏭  Setup aborted: {reason}."]
    unfinished = progress.unfinished()
    if unfinished:
        lines.append(f"   Not finished: {', '.join(unfinished)}.")
    lines.append(f"   To finish, run `{command}` again in a terminal and answer its questions.")
    return "\n".join(lines)
