"""The ``skills authority-retire`` subcommand and the floor that denies it name one path.

The CLI registers the recovery as the subcommand path ``("skills",
"authority-retire")``; the self-protection floor
``self-protection-skills-authority-retire`` denies that same path from an agent
shell, and the operator hint in ``skills.py`` prints it. Three spellings of one
command, in three modules none of which imports the others for it. These tests
read each spelling where it lives and hold them equal, so a rename on any side
fails a test instead of leaving a floor that denies a command nobody can type
or a hint that names a command the floor does not cover.

The floor lands ahead of the subcommand (it is a sensitive-path change a
maintainer lands from a repository branch; this change rebases onto it). On a
base without the floor these tests skip and say so; they run in full once both
sides are present.
"""

from __future__ import annotations

import argparse
import sys
from unittest.mock import patch

import pytest

from kiro_crew import skills
from kiro_crew.security import denied_rules

FLOOR_ID = "self-protection-skills-authority-retire"
REGISTERED_PATH = ("skills", "authority-retire")


def _floor_on_this_base() -> bool:
    return FLOOR_ID in denied_rules._SELF_PROTECTION_UNGATED_FLOOR_IDS


floor_required = pytest.mark.skipif(
    not _floor_on_this_base(),
    reason=(
        f"{FLOOR_ID} is not on this base: the floor lands first from a repository "
        "branch, and this change rebases onto it"
    ),
)


def _dispatched_subcommand(argv: list[str]) -> argparse.Namespace | None:
    """Run the CLI on *argv* and return the namespace the skills handler received."""
    seen: list[argparse.Namespace] = []
    with (
        patch.object(sys, "argv", ["kirocrew", *argv]),
        patch("kiro_crew.cli._skills_cmd", lambda ns: seen.append(ns)),
    ):
        from kiro_crew.cli import main

        main()
    return seen[0] if seen else None


def test_the_cli_registers_the_recovery_under_the_expected_path(monkeypatch):
    """The parser dispatches exactly ``skills authority-retire`` to the handler."""
    monkeypatch.setenv("KIROCREW_PORT", "5477")
    ns = _dispatched_subcommand(list(REGISTERED_PATH))
    assert ns is not None, "the skills handler was not reached"
    assert ns.skills_action == REGISTERED_PATH[1]


def test_the_operator_hint_names_the_registered_path():
    assert skills._AUTHORITY_RETIRE_COMMAND == "kirocrew " + " ".join(REGISTERED_PATH)


@floor_required
def test_the_floor_denies_the_registered_path_and_nothing_shorter():
    """The floor's tuple is the registered path, read through the public evaluator."""
    from kiro_crew import security

    command = "kirocrew " + " ".join(REGISTERED_PATH)
    assert security.is_denied(command), f"the floor does not deny {command!r}"
    assert security._is_self_skills_authority_retire(command.lower())
    # The parent subcommand alone is ordinary CLI surface, not the recovery.
    assert not security._is_self_skills_authority_retire("kirocrew skills")
    assert not security._is_self_skills_authority_retire("kirocrew skills list")


@floor_required
def test_the_floor_is_ungated_and_its_note_names_the_command():
    """The floor has no catalog row to opt out of, and its note names the command."""
    assert FLOOR_ID in denied_rules._SELF_PROTECTION_UNGATED_FLOOR_IDS
    assert FLOOR_ID not in {rule.id for rule in denied_rules.BUILTIN_DENIED_RULES}
    note = denied_rules._SELF_PROTECTION_FLOOR_NOTES[FLOOR_ID]
    assert "retire the auto-skill authority" in note
