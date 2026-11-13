"""The feature-request skill must be findable when a user asks to report a problem.

Per-message trigger matching is off by default (``skills.max_triggered = 0``),
so mid-session the agent finds this skill through its one line in the skill
index or a ``skill_search`` hit. Two things keep that working:

(1) The description fits the index line. ``_short_desc`` cuts at
    ``_SHORT_DESC_CHARS``, and the text past the cut is the part no agent reads.

(2) The triggers fire on the phrases a user reaches for ("report a problem",
    "send feedback") and stay quiet on ordinary prose that merely shares a word,
    so a user who has turned trigger matching on does not get this skill
    injected into an essay review or a security audit.
"""

from __future__ import annotations

import argparse
import functools
import re
from pathlib import Path

import pytest

from kiro_crew import cli_parser_gateway
from kiro_crew.skills import _SHORT_DESC_CHARS, SkillsLoader
from kiro_crew.trigger_match import MIN_TRIGGER_OVERLAP, trigger_score, words_of

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "src" / "kiro_crew" / "builtin_skills" / "feature-request" / "SKILL.md"


@pytest.fixture(scope="module")
def meta() -> dict[str, str]:
    # The loader's own parser, not a reimplementation of it.
    return SkillsLoader._parse_frontmatter(SKILL)


def test_description_fits_the_index_line(meta: dict[str, str]) -> None:
    description = " ".join(meta["description"].split())
    assert len(description) <= _SHORT_DESC_CHARS, (
        f"description is {len(description)} chars; the skill index cuts at "
        f"{_SHORT_DESC_CHARS}, so the tail is never shown"
    )


@pytest.mark.parametrize("text", ["I want to report a problem", "how do I send feedback"])
def test_report_phrases_fire(meta: dict[str, str], text: str) -> None:
    score, negated = trigger_score(meta["triggers"], words_of(text))
    assert not negated
    assert score >= MIN_TRIGGER_OVERLAP, f"{text!r} scored {score}"


@pytest.mark.parametrize(
    "text", ["can you give feedback on my essay", "review the security of my login code"]
)
def test_ordinary_prose_does_not_fire(meta: dict[str, str], text: str) -> None:
    score, _negated = trigger_score(meta["triggers"], words_of(text))
    assert score < MIN_TRIGGER_OVERLAP, f"{text!r} scored {score}"


# Each ``kirocrew <command>`` the skill cites is parsed by the registrar that defines
# that command, with abbreviations off, so a renamed, dropped or misspelt flag fails here.
def test_cited_kirocrew_commands_parse() -> None:
    commands = re.findall(r"`kirocrew ([^`]+)`", SKILL.read_text(encoding="utf-8"))
    assert commands, "SKILL.md cites no `kirocrew ...` command"
    for command in commands:
        parser = argparse.ArgumentParser(prog="kirocrew", exit_on_error=False)
        exact = functools.partial(argparse.ArgumentParser, allow_abbrev=False)
        cli_parser_gateway.register_doctor_parser(
            parser.add_subparsers(dest="command", parser_class=exact)
        )
        args, unknown = parser.parse_known_args(command.split())
        assert args.command == "doctor", f"{command!r} is not a kirocrew doctor command"
        assert unknown == [], f"{command!r}: {unknown} are not options of kirocrew doctor"
