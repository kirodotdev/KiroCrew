"""The ``kirocrew-commands`` skill lists every command the CLI offers.

The skill is the agent's own CLI reference: it loads when someone asks for
help, setup or "what can you do", and the agent answers from its tables. A
command the CLI has but the skill leaves out is one the agent does not know
it can suggest. This test reads the real command tree from the parser
``kirocrew`` builds and fails on any user-facing command or sub-command the
skill never names as ``kirocrew <command>``.

User-facing means what argparse lists: the top-level commands in
``cli_help.SUMMARIES`` and the sub-commands registered with a ``help`` text.
Internal ``mcp-*`` servers and ``_``-prefixed verbs are registered without one,
so they stay out.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pytest

from kiro_crew import cli_help

ROOT = Path(__file__).parent.parent
SKILL = ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-commands" / "SKILL.md"


class _ParserBuilt(Exception):
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        super().__init__()
        self.parser = parser


def _cli_parser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> argparse.ArgumentParser:
    """Build the parser ``main()`` builds, stopping before it parses anything.

    ``KIROCREW_PROJECT_DIR`` is pinned so ``main()``'s project auto-detection
    does not walk (and then export) the checkout it happens to run in.
    """
    original = argparse.ArgumentParser.parse_args

    def _stop_at_top_level(self, args=None, namespace=None):
        if self.prog == "kirocrew":
            raise _ParserBuilt(self)
        return original(self, args, namespace)

    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["kirocrew", "--help"])
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", _stop_at_top_level)
    from kiro_crew.cli import main

    with pytest.raises(_ParserBuilt) as built:
        main()
    return built.value.parser


def _listed_subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    found: dict[str, argparse.ArgumentParser] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            listed = {choice.dest for choice in action._choices_actions}
            for name, child in action._name_parser_map.items():
                if name in listed and not name.startswith("_"):
                    found[name] = child
    return found


def _user_facing_paths(parser: argparse.ArgumentParser) -> list[str]:
    top = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    paths = []
    for name in cli_help.SUMMARIES:
        paths.append(name)
        stack = [([name], top._name_parser_map[name])]
        while stack:
            path, child = stack.pop()
            for sub_name, grandchild in _listed_subcommands(child).items():
                paths.append(" ".join(path + [sub_name]))
                stack.append((path + [sub_name], grandchild))
    return sorted(paths)


def _named_in_skill(path: str, text: str) -> bool:
    return re.search(rf"kirocrew {re.escape(path)}(?![\w-])", text) is not None


def test_the_walk_finds_nested_commands(monkeypatch, tmp_path) -> None:
    paths = _user_facing_paths(_cli_parser(monkeypatch, tmp_path))
    for expected in ("gateway", "cron add", "memory export", "pod up"):
        assert expected in paths
    assert not [p for p in paths if p.startswith("mcp-") or " _" in p]


def test_named_in_skill_needs_the_whole_command() -> None:
    text = "| `kirocrew memory export -o f` | ... | `kirocrew app install-x` |"
    assert _named_in_skill("memory export", text)
    assert _named_in_skill("memory", text)
    assert not _named_in_skill("app install", text)


def test_commands_skill_names_every_cli_command(monkeypatch, tmp_path) -> None:
    text = SKILL.read_text(encoding="utf-8")
    missing = [
        p
        for p in _user_facing_paths(_cli_parser(monkeypatch, tmp_path))
        if not _named_in_skill(p, text)
    ]
    assert not missing, (
        f"{SKILL.relative_to(ROOT)} does not name these commands; add a row for each "
        "(`kirocrew <command>` and what it does):\n" + "\n".join(missing)
    )
