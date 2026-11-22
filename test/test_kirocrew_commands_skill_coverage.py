"""Guard: the ``kirocrew-commands`` skill names every user-facing CLI command.

The skill is the agent's CLI reference. It loads on help, setup and "what can you
do", and the agent answers from its tables, so a command the skill never names is a
command the agent cannot tell a user about. Seventeen commands drifted out this
way (the ``kas`` family, ``gateway-pid``, ``open``, ``desktop tunnel``, ``pod
restart``, ``memory forget``, ``knowledge stats``, ``app import``, ``secrets
authorize`` ...) because nothing connected the parser to the document.

"User-facing" has the same meaning the top-level help gives it: a top-level command
listed in ``cli_help.COMMAND_GROUPS``, or a sub-command registered with a help text.
Internal commands (the ``mcp-*`` servers the agent backend spawns) register with no
help and stay out, exactly as they stay out of ``kirocrew --help``.

The parser is captured from ``kiro_crew.cli.main()`` itself rather than rebuilt, so
the test walks what a user can actually run.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew import cli_help

SKILL = (
    Path(kiro_crew.__file__).resolve().parent / "builtin_skills" / "kirocrew-commands" / "SKILL.md"
)


def _capture_parser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> argparse.ArgumentParser:
    """Return the top-level parser ``main()`` builds, without running a command.

    ``main()`` builds the parser inline and then calls ``parse_args`` on it, so the
    call is intercepted and ended before anything dispatches. ``monkeypatch`` undoes
    the patch, and the environment writes ``main()`` makes, when the test ends.
    """
    captured: list[argparse.ArgumentParser] = []

    def _grab(self: argparse.ArgumentParser, *args: object, **kwargs: object) -> None:
        captured.append(self)
        raise SystemExit(0)

    # ``main()`` pops these before it parses; registering them first makes
    # monkeypatch restore whatever the host had.
    monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "0")
    monkeypatch.setenv("KIROCREW_SANDBOX_LEVEL", "0")
    # Pinned so ``main()``'s project auto-detection does not walk (and then export)
    # the checkout this runs in.
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["kirocrew"])
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", _grab)

    from kiro_crew.cli import main

    with pytest.raises(SystemExit):
        main()
    assert captured, "main() never reached parse_args, so there is no parser to walk"
    return captured[0]


def _user_facing_subcommands(
    parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()
) -> set[tuple[str, ...]]:
    """Every sub-command below ``parser`` that was registered with a help text."""
    found: set[tuple[str, ...]] = set()
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        helped = {
            choice.dest
            for choice in action._choices_actions
            if choice.help not in (None, argparse.SUPPRESS)
        }
        for name, sub in action.choices.items():
            if name in helped:
                found.add((*prefix, name))
            found |= _user_facing_subcommands(sub, (*prefix, name))
    return found


def _is_named(command: str, skill_text: str) -> bool:
    """Whether the skill shows ``kirocrew <command>`` in a code span.

    A leading ``VAR=value`` is allowed because debug-only rows are written
    ``KIROCREW_DEBUG=1 kirocrew perf sample``. The trailing lookahead stops
    ``kirocrew kas`` from being satisfied by ``kirocrew kas-bundle`` or ``kirocrew kasten``.
    """
    pattern = r"`(?:[A-Z_][A-Z0-9_]*=\S+ )*kirocrew " + re.escape(command) + r"(?![\w-])"
    return re.search(pattern, skill_text) is not None


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


class TestEveryUserFacingCommandIsNamed:
    def test_top_level_commands(self, skill_text: str) -> None:
        missing = sorted(name for name in cli_help.SUMMARIES if not _is_named(name, skill_text))
        assert not missing, (
            f"{SKILL.relative_to(SKILL.parents[3])} never names these top-level commands: "
            f"{missing}. Add one row per command, worded from its help text in "
            "cli_help.COMMAND_GROUPS."
        )

    def test_sub_commands(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, skill_text: str
    ) -> None:
        commands = _user_facing_subcommands(_capture_parser(monkeypatch, tmp_path))
        missing = sorted(
            " ".join(path) for path in commands if not _is_named(" ".join(path), skill_text)
        )
        assert not missing, (
            f"{SKILL.relative_to(SKILL.parents[3])} never names these sub-commands: {missing}. "
            "Add one row per command, worded from its argparse help."
        )


class TestTheCoverageCheckCannotPassVacuously:
    """The guard above is only worth having if it can fail."""

    def test_the_walk_finds_known_nested_commands(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        commands = _user_facing_subcommands(_capture_parser(monkeypatch, tmp_path))
        assert {("pod", "restart"), ("kas", "login"), ("secrets", "authorize")} <= commands

    def test_the_walk_leaves_internal_commands_out(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        commands = _user_facing_subcommands(_capture_parser(monkeypatch, tmp_path))
        assert not [path for path in commands if path[0].startswith("mcp-")]

    @pytest.mark.parametrize(
        ("text", "command", "expected"),
        [
            ("| `kirocrew kas login` | x |", "kas login", True),
            ("| `kirocrew kas login --provider idc` | x |", "kas login", True),
            ("| `KIROCREW_DEBUG=1 kirocrew perf sample` | x |", "perf sample", True),
            ("| `kirocrew kas-bundle` | x |", "kas", False),
            ("| `kirocrew kasten` | x |", "kas", False),
            ("| `kirocrew kas login` | x |", "kas logout", False),
            ("kirocrew kas login, outside a code span", "kas login", False),
        ],
    )
    def test_the_matcher(self, text: str, command: str, expected: bool) -> None:
        assert _is_named(command, text) is expected
